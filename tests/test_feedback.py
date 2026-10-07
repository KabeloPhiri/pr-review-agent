"""`/fp`: flag one of the bot's findings as a false positive.

Runs `ReviewPipeline.feedback()` against `tests/fixtures/apply-pr/`, whose
`comment-1` is a bot finding on a PR authored by dev@example.com.
"""

from pathlib import Path

import mlflow
import pytest

from app.core.models import ExistingComment, PullRequestRef
from app.core.pipeline import ReviewPipeline
from app.services.scm.base import scm_registry
from app.services.scm.fake import FakeScmConnector

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "apply-pr"
AUTHOR = "dev@example.com"


@scm_registry.register("feedback-fake")
class FeedbackScm(FakeScmConnector):
    """Remembers replies across instances, like a real pull request does."""

    replies_seen: list = []
    with_model: str | None = None
    last: "FeedbackScm | None" = None

    def __init__(self, token=None, **options):
        super().__init__(token, **options)
        FeedbackScm.last = self

    async def get_comment(self, pr, comment_id):
        comment = await super().get_comment(pr, comment_id)
        if comment is not None and FeedbackScm.with_model:
            body = comment.body + f"\n<!-- prreview:model:{FeedbackScm.with_model} -->"
            comment = comment.model_copy(update={"body": body})
        return comment

    async def reply_to_comment(self, pr, comment_id, body):
        await super().reply_to_comment(pr, comment_id, body)
        FeedbackScm.replies_seen.append((comment_id, body))

    async def list_comments(self, pr):
        existing = await super().list_comments(pr)
        return existing + [
            ExistingComment(thread_id=f"reply-{i}", body=body)
            for i, (_, body) in enumerate(FeedbackScm.replies_seen)
        ]


@pytest.fixture
def ref():
    FeedbackScm.replies_seen = []
    FeedbackScm.with_model = None
    return PullRequestRef(
        scm="feedback-fake",
        repository="platform",
        pull_request_id="42",
        extra={"fixture_dir": str(FIXTURE)},
    )


async def _flag(ref, requester=AUTHOR, association=None, comment_id="comment-1", reason=None):
    return await ReviewPipeline().feedback(
        ref, comment_id=comment_id, requester=requester, association=association, reason=reason
    )


async def test_the_author_can_flag_a_finding(ref):
    result = await _flag(ref, reason="this is generated code")

    assert result.recorded is True
    assert result.rule_id == "python.style.spacing"
    [(comment_id, body)] = FeedbackScm.replies_seen
    assert comment_id == "comment-1"
    assert "Recorded as a false positive: this is generated code" in body
    assert "<!-- prreview:fp:comment-1 -->" in body
    assert FeedbackScm.last.closed == ["comment-1"]


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "member"])
async def test_people_with_write_access_can_flag(ref, association):
    result = await _flag(ref, requester="reviewer", association=association)
    assert result.recorded is True


@pytest.mark.parametrize("association", [None, "NONE", "CONTRIBUTOR", "FIRST_TIMER"])
async def test_others_cannot_flag_and_get_no_reply(ref, association):
    result = await _flag(ref, requester="stranger", association=association)
    assert (result.recorded, result.reason) == (False, "unauthorized_requester")
    assert FeedbackScm.replies_seen == []


async def test_only_the_bots_own_comments_can_be_flagged(ref):
    result = await _flag(ref, comment_id="not-a-comment")
    assert result.reason == "comment_not_found_or_unsupported"


async def test_flagging_twice_records_once(ref):
    assert (await _flag(ref)).recorded is True
    second = await _flag(ref, requester="reviewer", association="MEMBER")
    assert (second.recorded, second.reason) == (False, "already_recorded")
    assert len(FeedbackScm.replies_seen) == 1


async def test_the_model_that_wrote_the_finding_is_recorded(ref):
    FeedbackScm.with_model = "databricks-claude-sonnet-5-5"
    result = await _flag(ref, reason="  fine here  ")
    assert result.model == "databricks-claude-sonnet-5-5"

    mlflow.flush_trace_async_logging()
    tags = mlflow.get_trace(mlflow.get_last_active_trace_id()).info.tags
    assert tags["prreview.feedback"] == "false_positive"
    assert tags["prreview.rule"] == "python.style.spacing"
    assert tags["prreview.finding_model"] == "databricks-claude-sonnet-5-5"
    assert tags["prreview.fp_reason"] == "fine here"
    assert tags["prreview.flagged_by"] == AUTHOR
