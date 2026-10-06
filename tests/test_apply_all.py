"""`/apply all`: every open bot suggestion, grouped per file, one commit each.

Runs against `tests/fixtures/apply-all-pr/`, whose comments.json covers each
case once: two applicable suggestions in example.py and one in other.py, a
human lookalike, a finding without a suggestion, an outdated comment, one
already applied, and the summary.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import ReviewerError
from app.core.models import ExistingComment, Finding, PullRequestRef, Severity
from app.core.pipeline import ReviewPipeline, _already_applied
from app.services.apply.base import Applier, applier_registry
from app.services.apply.llm_applier import LlmApplier
from app.services.scm.base import scm_registry
from app.services.scm.fake import FakeScmConnector

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "apply-all-pr"
AUTHOR = "dev@example.com"

#: What each fixture rule changes, so a test can see every fix landed.
EDITS = {
    "python.style.spacing": ("a+b", "a + b"),
    "naming.function": ("def add(", "def add_numbers("),
    "python.style.comma": ("sub(a,b)", "sub(a, b)"),
}


@applier_registry.register("rules")
class RulesApplier(Applier):
    """Single-finding only, so `generate_patch_many` uses the base chaining."""

    name = "rules"
    fail_on: str | None = None

    async def generate_patch(self, *, file, current_text, finding):
        if file == RulesApplier.fail_on:
            raise ReviewerError(f"Model call failed for {file}")
        old, new = EDITS[finding.rule_id]
        return current_text.replace(old, new)


@applier_registry.register("fail-everywhere")
class FailingApplier(Applier):
    name = "fail-everywhere"

    async def generate_patch(self, *, file, current_text, finding):
        raise ReviewerError("endpoint down")


@scm_registry.register("bulk-fake")
class BulkScm(FakeScmConnector):
    last: "BulkScm | None" = None

    def __init__(self, token=None, **options):
        super().__init__(token, **options)
        BulkScm.last = self


ENABLED = {"allow_apply_fixes": True, "applier": "rules"}


@pytest.fixture
def ref() -> PullRequestRef:
    RulesApplier.fail_on = None
    return PullRequestRef(
        scm="bulk-fake",
        repository="platform",
        pull_request_id="42",
        extra={"fixture_dir": str(FIXTURE_DIR)},
    )


async def _apply_all(ref, overrides=ENABLED, requester=AUTHOR):
    return await ReviewPipeline().apply_all(ref, requester=requester, overrides=overrides)


async def test_applies_every_open_suggestion_one_commit_per_file(ref):
    result = await _apply_all(ref)

    assert result.applied is True
    assert sorted(result.applied_comment_ids) == ["c1", "c2", "c3"]
    assert len(result.commit_shas) == 2  # example.py and other.py
    pushed = dict(BulkScm.last.updated_files)
    assert pushed["example.py"] == "def add_numbers(a, b):\n    return a + b\n"
    assert pushed["other.py"] == "def sub(a, b):\n    return a - b\n"


async def test_skips_lookalikes_suggestionless_outdated_and_applied(ref):
    result = await _apply_all(ref)
    # c4 human lookalike, c5 no suggestion, c6 outdated: silently ignored.
    # c7 was applied earlier: reported, not redone.
    assert [(s.comment_id, s.reason) for s in result.skipped] == [("c7", "already_applied")]


async def test_one_confirmation_marks_each_suggestion_applied(ref):
    await _apply_all(ref)
    scm = BulkScm.last

    [confirmation] = scm.posted
    assert "Applied 3 suggestion(s) in 2 commit(s)" in confirmation.body
    assert sorted(scm.closed) == ["c1", "c2", "c3"]
    # A later single /apply on any of them must see it as already applied.
    existing = [ExistingComment(thread_id="t", marker=confirmation.marker, body=confirmation.body)]
    assert all(_already_applied(existing, cid) for cid in ("c1", "c2", "c3"))
    assert not _already_applied(existing, "c4")


async def test_a_failing_file_is_skipped_and_the_rest_still_land(ref):
    RulesApplier.fail_on = "other.py"
    result = await _apply_all(ref)

    assert result.applied is True
    assert sorted(result.applied_comment_ids) == ["c1", "c2"]
    assert ("c3", "failed: Model call failed for other.py") in [
        (s.comment_id, s.reason) for s in result.skipped
    ]


async def test_nothing_applied_because_of_failures_raises(ref):
    with pytest.raises(ReviewerError):
        await _apply_all(ref, {**ENABLED, "applier": "fail-everywhere"})


@pytest.mark.parametrize(
    "overrides, requester, reason",
    [
        ({"allow_apply_fixes": False, "applier": "rules"}, AUTHOR, "apply_fixes_disabled"),
        (ENABLED, "someone-else", "unauthorized_requester"),
        ({**ENABLED, "apply_trusted_authors": []}, AUTHOR, "nothing_to_apply"),
    ],
)
async def test_refusals(ref, overrides, requester, reason):
    result = await _apply_all(ref, overrides, requester)
    assert result.applied is False
    assert result.reason == reason
    assert BulkScm.last.updated_files == []


async def test_llm_applier_fixes_a_file_in_one_call():
    """One model call per file, listing every finding with its line."""
    prompts: list[str] = []

    def create(**kwargs):
        prompts.append(kwargs["messages"][1]["content"])
        content = json.dumps({"file_content": "fixed\n"})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    applier = LlmApplier(SimpleNamespace(model_endpoint="m", temperature=0.0), client=client)
    findings = [
        Finding(file="a.py", line=3, severity=Severity.ERROR, rule_id="r1", message="m1"),
        Finding(file="a.py", line=9, severity=Severity.WARNING, rule_id="r2", message="m2"),
    ]

    assert await applier.generate_patch_many(file="a.py", current_text="x\n", findings=findings)
    [prompt] = prompts
    assert "Finding 1 of 2" in prompt and "Finding 2 of 2" in prompt
    assert "Line: 3" in prompt and "Line: 9" in prompt
