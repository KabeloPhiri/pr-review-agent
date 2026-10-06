"""Console settings pages over HTTP: who may change what, and how.

Each test gets a fresh `LocalDirStore` in a temp directory, so what a page
writes can be checked on disk, history included.
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.console import build_console_router, editing
from app.core.jobs import InMemoryJobStore, JobRunner
from app.core.settings_store import LocalDirStore, NullStore, set_settings_store

ADMIN = {"X-Forwarded-Email": "admin@example.com"}
VIEWER = {"X-Forwarded-Email": "viewer@example.com"}
REPO = "github/acme__payments"
MODELS = "databricks-claude-sonnet-5-5,databricks-claude-opus-5-5"

ORNAMENT = re.compile(
    "[\U0001f000-\U0001faff←-⇿⌀-⏿■-➿⤀-⥿"
    "⬀-⯿️]"
)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("PRREVIEW_CONSOLE_ADMINS", "admin@example.com")
    monkeypatch.setenv("PRREVIEW_CONSOLE_VIEWERS", "viewer@example.com")
    monkeypatch.setenv("PRREVIEW_ALLOWED_MODELS", MODELS)
    monkeypatch.setattr(editing, "_endpoint_state", lambda name: "Ready")


@pytest.fixture
def store(tmp_path):
    s = LocalDirStore(tmp_path)
    set_settings_store(s)
    yield s
    set_settings_store(None)


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(
        build_console_router(JobRunner(InMemoryJobStore()), trace_source=lambda s, e: [])
    )
    return TestClient(app, follow_redirects=False)


def _post(client, path, data, headers=ADMIN):
    return client.post(path, data=data, headers=headers)


# --- access ----------------------------------------------------------------


@pytest.mark.parametrize("headers", [VIEWER, {"X-Forwarded-Email": "stranger@example.com"}, {}])
def test_only_admins_can_change_settings(client, store, headers):
    response = _post(client, "/console/config", {"text": "max_files: 3"}, headers)
    assert response.status_code == 403
    assert store.raw("config.yaml") is None


@pytest.mark.parametrize(
    "extra",
    [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}],
)
def test_a_change_submitted_from_another_site_is_refused(client, store, extra):
    response = _post(
        client, f"/console/config?repo={REPO}", {"text": "max_files: 3"}, {**ADMIN, **extra}
    )
    assert response.status_code == 403
    assert "not submitted from the console" in response.text


def test_same_origin_changes_are_accepted(client, store):
    headers = {**ADMIN, "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    response = _post(client, f"/console/config?repo={REPO}", {"text": "max_files: 3"}, headers)
    assert response.status_code == 303


def test_nothing_is_editable_without_a_store(client):
    set_settings_store(NullStore())
    page = client.get("/console/config", headers=ADMIN).text
    assert "Settings storage is not configured" in page
    assert "<textarea" not in page
    assert _post(client, "/console/config", {"text": "max_files: 3"}).status_code == 403


def test_viewers_see_settings_without_forms(client, store):
    page = client.get("/console/prompts", headers=VIEWER).text
    assert "Your access is read-only" in page
    assert "<form" not in page.split("</header>")[1]


# --- config ----------------------------------------------------------------


def test_a_global_change_needs_confirmation(client, store):
    first = _post(client, "/console/config", {"text": "max_files: 3", "scope": "global"})
    assert first.status_code == 200
    assert "Change configuration for every repository" in first.text
    assert store.raw("config.yaml") is None

    confirmed = _post(
        client, "/console/config", {"text": "max_files: 3", "scope": "global", "confirm": "yes"}
    )
    assert confirmed.status_code == 303
    assert store.raw("config.yaml") == "max_files: 3"
    [entry] = store.history("config.yaml")
    assert entry.actor == "admin@example.com"


def test_a_repository_change_applies_to_that_repository(client, store):
    response = _post(client, f"/console/config?repo={REPO}", {"text": "severity_gate: none"})
    assert response.status_code == 303
    assert store.raw("repos/github/acme__payments/config.yaml") == "severity_gate: none"
    assert store.raw("config.yaml") is None
    page = client.get(f"/console/config?repo={REPO}", headers=ADMIN).text
    assert "Console (this repository)" in page


@pytest.mark.parametrize(
    "text, message",
    [
        ("not_a_key: 1", "Unknown configuration key"),
        ("severity_gate: sometimes", "Invalid configuration"),
        ("max_files: [", "Not valid YAML"),
        ("- a list", "one setting per line"),
    ],
)
def test_invalid_config_is_explained_and_not_saved(client, store, text, message):
    response = _post(client, f"/console/config?repo={REPO}", {"text": text})
    assert response.status_code == 200
    assert message in response.text
    assert store.raw("repos/github/acme__payments/config.yaml") is None


# --- model -----------------------------------------------------------------


def test_model_page_lists_allowed_models_with_state(client, store):
    page = client.get("/console/model", headers=ADMIN).text
    for name in MODELS.split(","):
        assert name in page
    assert "Ready" in page


def test_only_allowed_models_can_be_chosen(client, store):
    response = _post(client, f"/console/model?repo={REPO}", {"model": "some-other-model"})
    assert "is not an allowed model" in response.text
    assert store.raw("repos/github/acme__payments/config.yaml") is None


def test_changing_the_model_for_one_repository(client, store):
    store.write("repos/github/acme__payments/config.yaml", "max_files: 4\n", actor="x")
    response = _post(client, f"/console/model?repo={REPO}", {"model": "databricks-claude-opus-5-5"})
    assert response.status_code == 303
    overrides = store.config_overrides(editing.repo_key(REPO))
    assert overrides == {"max_files": 4, "model_endpoint": "databricks-claude-opus-5-5"}


def test_changing_the_global_model_states_its_effect_first(client, store):
    response = _post(client, "/console/model", {"model": "databricks-claude-opus-5-5"})
    assert "Every repository without its own model setting" in response.text
    assert store.raw("config.yaml") is None


def test_model_test_shows_results_without_saving(client, store, monkeypatch):
    async def fake_dry_run(model):
        return {"ok": True, "seconds": 9.5, "tokens": 4321, "findings": 2, "errors": 1,
                "warnings": 1, "infos": 0, "notes": [], "sample": []}

    monkeypatch.setattr(editing, "_dry_run", fake_dry_run)
    page = _post(client, "/console/model/test", {"model": "databricks-claude-opus-5-5"}).text
    assert "Test result" in page and "4,321" in page
    assert store.history() == []


def test_prices_are_saved_and_validated(client, store):
    bad = _post(client, "/console/model/prices", {"input:databricks-claude-opus-5-5": "cheap"})
    assert "Prices must be numbers" in bad.text
    ok = _post(
        client,
        "/console/model/prices",
        {"input:databricks-claude-opus-5-5": "15", "output:databricks-claude-opus-5-5": "75"},
    )
    assert ok.status_code == 303
    assert store.console_settings()["prices"] == {
        "databricks-claude-opus-5-5": {"input": 15.0, "output": 75.0}
    }


# --- standards ---------------------------------------------------------------


def test_bundled_standards_can_be_copied_into_the_console(client, store):
    page = client.get("/console/standards", headers=ADMIN).text
    assert "Bundled with the app" in page
    assert _post(client, "/console/standards/import", {}).status_code == 200  # confirm page
    assert _post(client, "/console/standards/import", {"confirm": "yes"}).status_code == 303
    assert "python" in store.standards_names()
    assert "Source: Console." in client.get("/console/standards", headers=ADMIN).text


@pytest.mark.parametrize(
    "name, text, message",
    [
        ("Bad Name", "x", "Name must be lowercase"),
        ("ok", "", "cannot be empty"),
        ("ok", "---\napplies_to: [1]\n---\nx", "applies_to must be a mapping"),
        ("ok", "---\napplies_to:\n  langs: [python]\n---\nx", "Unknown applies_to"),
        ("ok", "---\napplies_to:\n  content_match: '('\n---\nx", "not a valid regular expression"),
        ("ok", "---\napplies_to: {}\nx", "not closed"),
    ],
)
def test_invalid_standards_are_explained(client, store, name, text, message):
    response = _post(client, f"/console/standards?repo={REPO}", {"name": name, "text": text})
    assert response.status_code == 200
    assert message in response.text
    assert store.standards_names(editing.repo_key(REPO)) == []


def test_repository_standard_lifecycle(client, store):
    key = editing.repo_key(REPO)
    saved = _post(client, f"/console/standards?repo={REPO}", {"name": "payments", "text": "No PANs."})
    assert saved.status_code == 303
    assert store.standards_names(key) == ["payments"]

    confirm = _post(client, f"/console/standards/delete?repo={REPO}", {"name": "payments"})
    assert "Delete the standard payments" in confirm.text
    gone = _post(
        client, f"/console/standards/delete?repo={REPO}", {"name": "payments", "confirm": "yes"}
    )
    assert gone.status_code == 303
    assert store.standards_names(key) == []


# --- prompts, history, audit ---------------------------------------------------


def test_prompt_guidance_cannot_include_the_output_format(client, store):
    response = _post(
        client, "/console/prompts", {"name": "review_guidance", "text": "Be brief. Return JSON only"}
    )
    assert "Leave out the output format" in response.text
    assert store.prompt("review_guidance") is None


def test_prompt_save_and_reset(client, store):
    form = {"name": "review_guidance", "text": "Be brief.", "confirm": "yes"}
    assert _post(client, "/console/prompts", form).status_code == 303
    assert store.prompt("review_guidance") == "Be brief."
    reset = {"name": "review_guidance", "confirm": "yes"}
    assert _post(client, "/console/prompts/reset", reset).status_code == 303
    assert store.prompt("review_guidance") is None


def test_history_compares_and_restores(client, store):
    store.write("prompts/apply_guidance.md", "first\nline", actor="a@x")
    store.write("prompts/apply_guidance.md", "second\nline", actor="b@x")
    old = store.history("prompts/apply_guidance.md")[-1]

    page = client.get(
        f"/console/history?path=prompts/apply_guidance.md&version={old.version}", headers=ADMIN
    ).text
    assert "first" in page and "second" in page and "diff-replace" in page

    form = {"path": "prompts/apply_guidance.md", "version": old.version, "confirm": "yes"}
    assert _post(client, "/console/history/restore", form).status_code == 303
    assert store.prompt("apply_guidance") == "first\nline"
    assert store.history("prompts/apply_guidance.md")[0].action == "rollback"


def test_audit_log_lists_every_change(client, store):
    _post(client, f"/console/config?repo={REPO}", {"text": "max_files: 3"})
    _post(client, "/console/prompts", {"name": "apply_guidance", "text": "x", "confirm": "yes"})
    page = client.get("/console/audit", headers=ADMIN).text
    assert "repos/github/acme__payments/config.yaml" in page
    assert "prompts/apply_guidance.md" in page


@pytest.mark.parametrize(
    "path",
    ["/console/model", f"/console/config?repo={REPO}", "/console/standards?name=python",
     "/console/prompts", "/console/audit", "/console/history?path=config.yaml"],
)
def test_settings_pages_carry_no_icons_or_emoji(client, store, path):
    html = client.get(path, headers=ADMIN).text
    assert not ORNAMENT.findall(html), path


async def test_a_slow_settings_store_does_not_stall_other_requests(store, monkeypatch):
    """Every console handler runs off the server's event loop, so slow volume
    I/O on a settings page cannot hold up the review API."""
    import asyncio
    import time

    import httpx

    real_read = store._read

    def slow_read(path):
        time.sleep(0.8)  # a slow volume
        return real_read(path)

    monkeypatch.setattr(store, "_read", slow_read)
    app = FastAPI()
    app.include_router(
        build_console_router(JobRunner(InMemoryJobStore()), trace_source=lambda s, e: [])
    )

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.monotonic()
        page = asyncio.create_task(client.get(f"/console/config?repo={REPO}", headers=ADMIN))
        await asyncio.sleep(0.1)
        assert (await client.get("/ping")).status_code == 200
        ping_seconds = time.monotonic() - started
        assert (await page).status_code == 200

    assert ping_seconds < 0.5, f"/ping waited {ping_seconds:.2f}s behind the settings store"


def test_form_posts_still_work_when_handled_off_the_event_loop(client, store):
    """The body is read on the server loop and reused by the handler."""
    response = _post(client, f"/console/config?repo={REPO}", {"text": "max_files: 6"})
    assert response.status_code == 303
    assert store.raw("repos/github/acme__payments/config.yaml") == "max_files: 6"
