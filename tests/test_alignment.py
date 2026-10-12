"""Alignment check: parsing, the pipeline step, the report comment and the API.

A `stub` checker stands in for the model, the same trick `test_pipeline.py`
uses for the reviewer — no mocking of internals, no network.
"""

import json
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import build_router
from app.core.config import EffectiveConfig
from app.core.errors import ConfigError, PrReviewError, ReviewerError
from app.core.jobs import InMemoryJobStore, JobRunner
from app.core.models import (
    AlignmentResult,
    AlignmentVerdict,
    ChangeIntent,
    ChangeType,
    Diff,
    DiffFile,
    ExistingComment,
    Hunk,
    PullRequest,
    PullRequestRef,
)
from app.core.pipeline import ReviewPipeline, _alignment_input
from app.services.alignment.base import AlignmentChecker, AlignmentInput, alignment_registry
from app.services.alignment.llm_checker import parse_assessment
from app.services.publish.formatter import alignment_marker, render_alignment
from app.services.publish.publisher import Publisher
from app.services.scm.base import scm_registry
from app.services.scm.fake import FakeScmConnector

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workspace-pr"
REQUIREMENT = "Total completed order revenue per customer per day.\nIgnore cancelled orders."


@alignment_registry.register("stub-align")
class StubChecker(AlignmentChecker):
    """Says the PR is aligned, and remembers what it was shown."""

    name = "stub-align"
    seen: list[AlignmentInput] = []

    async def check(self, request):
        StubChecker.seen.append(request)
        return parse_assessment(
            json.dumps(
                {
                    "verdict": "aligned",
                    "summary": "Totals completed revenue per customer per day.",
                    "criteria": [
                        {"text": "Revenue per customer per day", "status": "met",
                         "evidence": ["jobs/revenue.py:11"]},
                        {"text": "Ignore cancelled orders", "status": "met",
                         "evidence": ["jobs/revenue.py:9"]},
                    ],
                    "unrelated_changes": [{"file": "README.md", "reason": "doc wording"}],
                    "intent_matches": True,
                    "detected_intent": "feature",
                }
            ),
            request,
        )


@alignment_registry.register("down-align")
class DownChecker(AlignmentChecker):
    name = "down-align"

    async def check(self, request):
        raise ReviewerError("Model endpoint 'm' call failed")


@scm_registry.register("align-fake")
class CapturingScm(FakeScmConnector):
    last: "CapturingScm | None" = None

    def __init__(self, token=None, **options):
        super().__init__(token, **options)
        CapturingScm.last = self


STUB = {"alignment_checker": "stub-align"}


@pytest.fixture
def ref(tmp_path) -> PullRequestRef:
    StubChecker.seen = []
    CapturingScm.last = None
    fixture = tmp_path / "pr"
    shutil.copytree(FIXTURE, fixture)
    return PullRequestRef(
        scm="align-fake",
        repository="platform",
        pull_request_id="7",
        extra={"fixture_dir": str(fixture)},
    )


def _request(**extra) -> AlignmentInput:
    pr = PullRequest(ref=PullRequestRef(scm="fake", repository="r", pull_request_id="1"))
    return AlignmentInput(
        pr=pr, intent=ChangeIntent.BUG_FIX, requirement="x", diff_text="d", files=["a.py"], **extra
    )


# -- parsing -------------------------------------------------------------------------


def test_parse_reads_every_field():
    reply = json.dumps(
        {
            "verdict": "partial",
            "summary": " Half done. ",
            "criteria": [
                {"text": "A", "status": "MET", "evidence": ["a.py:3"], "note": "ok"},
                {"text": "B", "status": "missing"},
            ],
            "unrelated_changes": [{"file": "b.py", "reason": "refactor"}, {"reason": "no file"}],
            "intent_matches": False,
            "detected_intent": "feature",
        }
    )
    result = parse_assessment(reply, _request())
    assert result.verdict is AlignmentVerdict.PARTIAL
    assert result.summary == "Half done."
    assert [c.status.value for c in result.criteria] == ["met", "missing"]
    assert result.criteria[0].evidence == ["a.py:3"]
    assert [u.file for u in result.unrelated_changes] == ["b.py"]
    assert result.intent is ChangeIntent.BUG_FIX
    assert result.intent_matches is False
    assert result.detected_intent is ChangeIntent.FEATURE
    assert result.files_considered == 1


def test_parse_accepts_a_fenced_reply():
    result = parse_assessment('```json\n{"verdict": "aligned"}\n```', _request())
    assert result.verdict is AlignmentVerdict.ALIGNED


@pytest.mark.parametrize(
    "reply",
    [
        "",  # silence is not a verdict, unlike an empty review
        '{"verdict": "aligned", "summary": "trunc',  # cut off mid-JSON
        '{"verdict": "probably"}',  # not a verdict we know
        '["aligned"]',  # not an object
        "Looks good to me!",
    ],
)
def test_parse_never_turns_a_bad_reply_into_a_verdict(reply):
    with pytest.raises(ReviewerError):
        parse_assessment(reply, _request())


def test_parse_drops_a_malformed_criterion_with_a_warning():
    reply = json.dumps(
        {
            "verdict": "aligned",
            "criteria": [{"text": "A", "status": "met"}, {"text": "B", "status": "sort of"}],
            "detected_intent": "something new",
        }
    )
    result = parse_assessment(reply, _request())
    assert [c.text for c in result.criteria] == ["A"]
    assert result.warnings
    assert result.detected_intent is None


# -- the pipeline step -------------------------------------------------------------------


async def test_check_alignment_uses_the_checker(ref):
    result = await ReviewPipeline().check_alignment(
        ref, intent=ChangeIntent.FEATURE, requirement=REQUIREMENT, overrides=STUB
    )
    assert result.verdict is AlignmentVerdict.ALIGNED
    assert result.ref == ref
    assert result.comment_posted is False
    [seen] = StubChecker.seen
    assert seen.requirement == REQUIREMENT
    assert seen.pr.title == "Add daily revenue per customer"
    # Every changed file, not only languages a standard claims: README.md too.
    assert seen.files == ["jobs/revenue.py", "README.md"]
    assert "=== README.md (edit) ===" in seen.diff_text
    # New-file line numbers, as the reviewer sees them.
    assert "     9 + |     completed = orders.filter" in seen.diff_text
    assert CapturingScm.last.posted == []


async def test_an_empty_requirement_is_a_400(ref):
    with pytest.raises(ConfigError) as caught:
        await ReviewPipeline().check_alignment(
            ref, intent=ChangeIntent.FEATURE, requirement="   ", overrides=STUB
        )
    assert caught.value.status_code == 400


async def test_a_model_outage_raises_rather_than_reporting_a_verdict(ref):
    with pytest.raises(ReviewerError):
        await ReviewPipeline().check_alignment(
            ref,
            intent=ChangeIntent.FEATURE,
            requirement=REQUIREMENT,
            overrides={"alignment_checker": "down-align"},
        )


async def test_post_comment_posts_one_report(ref):
    result = await ReviewPipeline().check_alignment(
        ref,
        intent=ChangeIntent.BUG_FIX,
        requirement=REQUIREMENT,
        post_comment=True,
        overrides=STUB,
    )
    assert result.comment_posted is True
    [report] = CapturingScm.last.posted
    assert report.marker == alignment_marker(ChangeIntent.BUG_FIX, REQUIREMENT)
    assert report.file is None
    assert "Requirement alignment" in report.body
    assert "| Revenue per customer per day | Met | `jobs/revenue.py:11` |" in report.body


async def test_post_comment_is_idempotent_per_requirement(ref):
    fixture = Path(ref.extra["fixture_dir"])
    comments = json.loads((fixture / "comments.json").read_text(encoding="utf-8"))
    # Same requirement with different whitespace: the marker must still match.
    marker = alignment_marker(ChangeIntent.FEATURE, "  " + REQUIREMENT.replace("\n", "\n\n"))
    comments.append({"thread_id": "report-1", "marker": marker, "body": f"<!-- {marker} -->"})
    (fixture / "comments.json").write_text(json.dumps(comments), encoding="utf-8")

    result = await ReviewPipeline().check_alignment(
        ref, intent=ChangeIntent.FEATURE, requirement=REQUIREMENT, post_comment=True,
        overrides=STUB,
    )
    assert result.comment_posted is True
    assert CapturingScm.last.posted == []


async def test_a_pr_with_no_text_changes_is_unclear_not_aligned(ref):
    fixture = Path(ref.extra["fixture_dir"])
    (fixture / "diff.patch").write_text(
        "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n",
        encoding="utf-8",
    )
    result = await ReviewPipeline().check_alignment(
        ref, intent=ChangeIntent.FEATURE, requirement=REQUIREMENT, overrides=STUB
    )
    assert result.verdict is AlignmentVerdict.UNCLEAR
    assert StubChecker.seen == []


def test_alignment_input_omits_excluded_binary_and_oversized_files():
    hunk = Hunk(new_start=1, new_lines=1, lines=["+" + "x" * 80])
    diff = Diff(
        files=[
            DiffFile(path="a.py", hunks=[hunk]),
            DiffFile(path="logo.png", is_binary=True),
            DiffFile(path="vendor/x.min.js", hunks=[hunk]),
            DiffFile(path="b.py", change_type=ChangeType.ADD, hunks=[hunk]),
        ]
    )
    config = EffectiveConfig(exclude_paths=["**/*.min.js"], alignment_max_chars=150)
    pr = PullRequest(ref=PullRequestRef(scm="fake", repository="r", pull_request_id="1"))
    request, warnings = _alignment_input(pr, diff, config, ChangeIntent.OTHER, "req")
    assert request.files == ["a.py", "logo.png", "vendor/x.min.js", "b.py"]
    assert "=== a.py (edit) ===" in request.diff_text
    assert request.omitted == ["logo.png", "vendor/x.min.js", "b.py"]
    assert warnings == ["1 file(s) were too large to include and were judged by name only"]


# -- the report and the publisher ---------------------------------------------------------


async def test_a_re_review_never_resolves_an_alignment_report():
    marker = alignment_marker(ChangeIntent.FEATURE, "req")
    existing = [ExistingComment(thread_id="t1", marker=marker, body=f"<!-- {marker} -->")]
    scm = FakeScmConnector()
    pr = PullRequest(ref=PullRequestRef(scm="fake", repository="r", pull_request_id="1"))

    await Publisher(scm, EffectiveConfig())._close_resolved(pr, existing, current_markers=set())
    assert scm.closed == []


def test_render_alignment_flags_a_mismatched_intent():
    result = AlignmentResult(
        ref=PullRequestRef(scm="fake", repository="r", pull_request_id="1"),
        intent=ChangeIntent.BUG_FIX,
        verdict=AlignmentVerdict.NOT_ALIGNED,
        intent_matches=False,
        detected_intent=ChangeIntent.FEATURE,
    )
    body = render_alignment(result, "Fix | the crash").body
    assert "Not aligned" in body
    assert "Reads more like: _Feature_" in body
    assert "> Fix | the crash" in body


# -- REST ---------------------------------------------------------------------------------


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(build_router(JobRunner(InMemoryJobStore())))

    @app.exception_handler(PrReviewError)
    async def _handler(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    return TestClient(app)


def _body(ref, **extra):
    return {
        "scm": ref.scm,
        "repository": ref.repository,
        "pull_request_id": ref.pull_request_id,
        "extra": ref.extra,
        "config": STUB,
        "requirement": REQUIREMENT,
        "intent": "change_ticket",
        **extra,
    }


def test_rest_alignment_sync(client, ref):
    response = client.post("/alignment", json=_body(ref, mode="sync"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["result"]["verdict"] == "aligned"
    assert payload["result"]["intent"] == "change_ticket"


def test_rest_alignment_async_is_polled(client, ref):
    response = client.post("/alignment", json=_body(ref, mode="async"))
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    for _ in range(50):
        polled = client.get(f"/alignment/{job_id}")
        if polled.json()["status"] == "completed":
            break
    assert polled.json()["result"]["verdict"] == "aligned"
    # Another kind's poll route does not answer for it.
    assert client.get(f"/apply/{job_id}").status_code == 404


def test_rest_alignment_rejects_an_unknown_intent(client, ref):
    response = client.post("/alignment", json=_body(ref, intent="chore", mode="sync"))
    assert response.status_code == 422
