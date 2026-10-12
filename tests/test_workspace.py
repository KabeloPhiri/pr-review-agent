"""PR workspace: pages, the JSON API, and who may do what.

Mounted on a bare FastAPI app against the fake SCM and a copy of
`tests/fixtures/workspace-pr/`, with stub plugins — no network, no model.
The fake connector treats the token as the login it belongs to, so a test
is whoever it sends in `X-SCM-Token`.
"""

import re
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.jobs import InMemoryJobStore, JobRunner
from app.services.alignment.base import AlignmentChecker, alignment_registry
from app.services.alignment.llm_checker import parse_assessment
from app.services.apply.base import Applier, applier_registry
from app.workspace import build_workspace_router

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workspace-pr"
EMAIL = "dev@example.com"
AUTHOR = "dev"  # pr.json's author

#: Emoji and pictographic/symbol blocks, as the console tests define them.
ORNAMENT = re.compile(
    "[\U0001f000-\U0001faff←-⇿⌀-⏿■-➿⤀-⥿"
    "⬀-⯿️]"
)


@applier_registry.register("ws-stub")
class WsApplier(Applier):
    name = "ws-stub"

    async def generate_patch(self, *, file, current_text, finding):
        before = "orders.filter(\"status = 'completed'\")"
        return current_text.replace(before, 'orders.filter(F.col("status") == "completed")')


@alignment_registry.register("ws-align")
class WsChecker(AlignmentChecker):
    name = "ws-align"

    async def check(self, request):
        return parse_assessment('{"verdict": "partial", "summary": "Half."}', request)


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for name in ("DATABRICKS_APP_NAME", "PRREVIEW_WORKSPACE_USERS", "PRREVIEW_WORKSPACE_DEV_EMAIL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fixture(tmp_path) -> Path:
    target = tmp_path / "pr"
    shutil.copytree(FIXTURE, target)
    (target / "files" / ".prreview" / "config.yaml").write_text(
        "allow_apply_fixes: true\napplier: ws-stub\nalignment_checker: ws-align\n",
        encoding="utf-8",
    )
    return target


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(build_workspace_router(JobRunner(InMemoryJobStore())))
    with TestClient(app) as client:
        yield client


def headers(token=AUTHOR, email=EMAIL, **extra):
    out = {"x-forwarded-email": email, **extra}
    if token:
        out["x-scm-token"] = token
    return out


def ref(fixture, **extra):
    return {"scm": "fake", "repository": "platform", "pull_request_id": "7",
            "fixture_dir": str(fixture), **extra}


def query(fixture):
    return "&".join(f"{k}={v}" for k, v in ref(fixture).items())


def finish(client, job, email=EMAIL):
    """Poll a workspace job until it ends."""
    if "job_id" not in job:
        return job
    for _ in range(100):
        polled = client.get(f"/workspace/api/jobs/{job['job_id']}", headers=headers(email=email))
        assert polled.status_code == 200, polled.text
        if polled.json()["status"] in ("completed", "failed"):
            return polled.json()
    raise AssertionError("job did not finish")


# -- pages ----------------------------------------------------------------------------

PAGES = ["/workspace", "/workspace/pr", "/workspace/m/pr", "/workspace/m/redeem"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render_with_their_security_headers(client, path):
    response = client.get(path, headers=headers())
    assert response.status_code == 200
    assert '<script src="/workspace/static/workspace.js" defer></script>' in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert EMAIL in response.text


@pytest.mark.parametrize("path", PAGES)
def test_pages_carry_no_icons_or_emoji(client, path):
    found = ORNAMENT.findall(client.get(path, headers=headers()).text)
    assert not found, f"{path} contains {found}"


def test_static_assets_are_served_and_nothing_else(client):
    css = client.get("/workspace/static/workspace.css", headers=headers())
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    js = client.get("/workspace/static/workspace.js", headers=headers())
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/workspace/static/routes.py").status_code == 404
    # The client never builds markup from strings: comment bodies are untrusted.
    assert not re.search(r"\.(innerHTML|outerHTML)|insertAdjacentHTML|document\.write", js.text)


def test_the_script_and_styles_carry_no_icons_or_emoji(client):
    for asset in ("workspace.js", "workspace.css"):
        text = client.get(f"/workspace/static/{asset}").text
        assert not ORNAMENT.findall(text), asset


def test_anonymous_visitors_are_refused(client):
    response = client.get("/workspace")
    assert response.status_code == 403
    assert "not signed in" in response.text
    api = client.get("/workspace/api/me?scm=fake", headers={"x-scm-token": "dev"})
    assert api.status_code == 403


def test_the_allowlist_narrows_access(client, monkeypatch):
    monkeypatch.setenv("PRREVIEW_WORKSPACE_USERS", "Someone@Example.com")
    assert client.get("/workspace", headers=headers(email="someone@example.com")).status_code == 200
    refused = client.get("/workspace", headers=headers())
    assert refused.status_code == 403
    assert EMAIL in refused.text


def test_the_dev_email_works_only_outside_databricks(client, monkeypatch):
    monkeypatch.setenv("PRREVIEW_WORKSPACE_DEV_EMAIL", "local@example.com")
    assert client.get("/workspace").status_code == 200
    monkeypatch.setenv("DATABRICKS_APP_NAME", "pr-review-agent")
    assert client.get("/workspace").status_code == 403


def test_the_fixture_provider_is_offered_only_in_development(client, monkeypatch):
    assert 'value="fake"' in client.get("/workspace", headers=headers()).text
    monkeypatch.setenv("DATABRICKS_APP_NAME", "pr-review-agent")
    assert 'value="fake"' not in client.get("/workspace", headers=headers()).text


# -- reading --------------------------------------------------------------------------------


def test_me_returns_the_token_owner(client):
    response = client.get("/workspace/api/me?scm=fake", headers=headers(token="octo"))
    assert response.json() == {"login": "octo", "email": EMAIL}


def test_me_needs_a_token_for_github(client):
    response = client.get("/workspace/api/me?scm=github", headers=headers(token=None))
    assert response.status_code == 401
    assert response.json()["type"] == "ScmAuthError"


def test_pulls_lists_the_fixture(client, fixture):
    response = client.get(
        f"/workspace/api/pulls?scm=fake&repository=platform&fixture_dir={fixture}",
        headers=headers(),
    )
    [pr] = response.json()["pulls"]
    assert pr["title"] == "Add daily revenue per customer"
    assert pr["author"] == AUTHOR


def test_the_snapshot_classifies_every_comment(client, fixture):
    response = client.get(f"/workspace/api/pr?{query(fixture)}", headers=headers())
    assert response.status_code == 200, response.text
    data = response.json()
    kinds = {v["comment"]["thread_id"]: v["kind"] for v in data["comments"]}
    assert kinds == {
        "comment-1": "finding",
        "comment-2": "finding",
        "comment-3": "human",
        "comment-4": "finding",
        "summary-1": "bot",
        "general-1": "human",
    }
    first = next(v for v in data["comments"] if v["comment"]["thread_id"] == "comment-1")
    assert first["finding"]["rule_id"] == "pyspark.filter.string-expression"
    assert first["finding"]["suggestion"].strip().startswith("completed = orders.filter(F.col")
    assert data["viewer"] == AUTHOR
    assert data["apply_refusal"] is None
    # comment-2 has no suggestion and comment-4 is outdated: one left to apply.
    assert data["open_suggestions"] == 1
    assert [f["path"] for f in data["diff"]["files"]] == ["jobs/revenue.py", "README.md"]
    assert {i["value"] for i in data["intents"]} >= {"change_ticket", "bug_fix", "feature"}


def test_the_snapshot_explains_why_a_non_author_cannot_apply(client, fixture):
    response = client.get(f"/workspace/api/pr?{query(fixture)}", headers=headers(token="reviewer"))
    data = response.json()
    assert data["viewer"] == "reviewer"
    assert data["apply_refusal"] == "unauthorized_requester"


def test_the_snapshot_trusts_only_the_bot_identity(client, fixture):
    (fixture / "files" / ".prreview" / "config.yaml").write_text(
        "allow_apply_fixes: true\napply_trusted_authors: [someone-else]\n", encoding="utf-8"
    )
    data = client.get(f"/workspace/api/pr?{query(fixture)}", headers=headers()).json()
    first = next(v for v in data["comments"] if v["comment"]["thread_id"] == "comment-1")
    # Still the bot's marker, so not a human — but not an applicable finding either.
    assert first["kind"] == "bot"
    assert data["open_suggestions"] == 0


def test_fixture_dir_is_refused_for_a_real_provider(client, fixture):
    response = client.get(
        f"/workspace/api/pr?scm=github&repository=a/b&pull_request_id=1&fixture_dir={fixture}",
        headers=headers(),
    )
    assert response.status_code == 400


def test_the_fixture_provider_is_refused_once_deployed(client, fixture, monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_NAME", "pr-review-agent")
    response = client.get(f"/workspace/api/pr?{query(fixture)}", headers=headers())
    assert response.status_code == 400


# -- acting -----------------------------------------------------------------------------


def test_apply_pushes_as_the_token_owner(client, fixture):
    started = client.post(
        "/workspace/api/apply", json=ref(fixture, comment_id="comment-1"), headers=headers()
    )
    assert started.status_code == 200, started.text
    job = finish(client, started.json())
    assert job["status"] == "completed"
    assert job["result"]["applied"] is True
    assert job["result"]["commit_sha"] == "fake-commit-0"
    assert job["result"]["file"] == "jobs/revenue.py"


def test_apply_by_someone_else_is_a_soft_refusal(client, fixture):
    started = client.post(
        "/workspace/api/apply",
        json=ref(fixture, comment_id="comment-1"),
        headers=headers(token="reviewer"),
    )
    job = finish(client, started.json())
    assert job["result"] == {**job["result"], "applied": False, "reason": "unauthorized_requester"}


def test_a_claimed_requester_is_rejected_outright(client, fixture):
    # The requester is always the token's owner; the body cannot name one.
    response = client.post(
        "/workspace/api/apply",
        json=ref(fixture, comment_id="comment-1", requester=AUTHOR),
        headers=headers(token="reviewer"),
    )
    assert response.status_code == 400


def test_apply_without_a_verifiable_login_never_starts_a_job(client, fixture):
    response = client.post(
        "/workspace/api/apply",
        json=ref(fixture, comment_id="comment-1"),
        headers=headers(token=None),
    )
    assert response.json() == {
        "status": "completed",
        "result": {"applied": False, "reason": "requester_unverified"},
    }


def test_apply_all_applies_every_open_suggestion(client, fixture):
    started = client.post("/workspace/api/apply/all", json=ref(fixture), headers=headers())
    job = finish(client, started.json())
    assert job["result"]["applied"] is True
    assert job["result"]["applied_comment_ids"] == ["comment-1"]


def test_a_job_is_visible_only_to_whoever_started_it(client, fixture):
    started = client.post(
        "/workspace/api/apply", json=ref(fixture, comment_id="comment-1"), headers=headers()
    ).json()
    other = client.get(
        f"/workspace/api/jobs/{started['job_id']}", headers=headers(email="other@example.com")
    )
    assert other.status_code == 404
    assert "no longer known" in other.json()["error"]
    finish(client, started)


@pytest.mark.parametrize(
    "path", ["/workspace/api/apply", "/workspace/api/apply/all", "/workspace/api/alignment",
             "/workspace/api/feedback", "/workspace/api/handoff"],
)
def test_writes_from_another_site_are_refused(client, fixture, path):
    single = path in ("/workspace/api/apply", "/workspace/api/feedback")
    body = ref(fixture, comment_id="comment-1") if single else ref(fixture)
    response = client.post(path, json=body, headers=headers(**{"sec-fetch-site": "cross-site"}))
    assert response.status_code == 403
    response = client.post(path, json=body, headers=headers(origin="https://evil.example"))
    assert response.status_code == 403


def test_feedback_dismisses_a_finding(client, fixture):
    response = client.post(
        "/workspace/api/feedback",
        json=ref(fixture, comment_id="comment-1", reason="the filter is fine"),
        headers=headers(),
    )
    result = response.json()["result"]
    assert result["recorded"] is True
    assert result["rule_id"] == "pyspark.filter.string-expression"


def test_feedback_from_a_reviewer_is_refused_softly(client, fixture):
    response = client.post(
        "/workspace/api/feedback",
        json=ref(fixture, comment_id="comment-1"),
        headers=headers(token="reviewer"),
    )
    assert response.json()["result"]["reason"] == "unauthorized_requester"


def test_alignment_runs_as_a_job(client, fixture):
    started = client.post(
        "/workspace/api/alignment",
        json=ref(fixture, intent="change_ticket", requirement="Daily revenue per customer"),
        headers=headers(token="reviewer"),  # anyone may check; nothing is pushed
    )
    job = finish(client, started.json())
    assert job["result"]["verdict"] == "partial"
    assert job["result"]["intent"] == "change_ticket"


def test_alignment_needs_a_requirement(client, fixture):
    response = client.post(
        "/workspace/api/alignment", json=ref(fixture, requirement="  "), headers=headers()
    )
    assert response.status_code == 400
    assert "requirement" in response.json()["error"]
