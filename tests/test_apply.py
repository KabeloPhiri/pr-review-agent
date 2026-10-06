"""End-to-end apply-fix pipeline test: fixture SCM, stub applier, no network."""

from pathlib import Path

import pytest

from app.core.models import PullRequest, PullRequestRef
from app.core.pipeline import ReviewPipeline
from app.services.apply.base import Applier, applier_registry
from app.services.scm.fake import FakeScmConnector

APPLY_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "apply-pr"


@applier_registry.register("stub")
class StubApplier(Applier):
    """Fixes exactly the bug the fixture comment describes."""

    name = "stub"
    requested: list[str] = []

    async def generate_patch(self, *, file, current_text, finding):
        StubApplier.requested.append(file)
        return current_text.replace("a+b", "a + b")


@pytest.fixture
def apply_ref() -> PullRequestRef:
    return PullRequestRef(
        scm="fake",
        repository="platform",
        pull_request_id="42",
        extra={"fixture_dir": str(APPLY_FIXTURE_DIR)},
    )


async def test_apply_is_disabled_by_default(apply_ref):
    result = await ReviewPipeline().apply(
        apply_ref, comment_id="comment-1", requester="dev@example.com"
    )
    assert result.applied is False
    assert result.reason == "apply_fixes_disabled"


async def test_apply_requires_the_pr_author(apply_ref):
    result = await ReviewPipeline().apply(
        apply_ref,
        comment_id="comment-1",
        requester="someone-else",
        overrides={"allow_apply_fixes": True, "applier": "stub"},
    )
    assert result.applied is False
    assert result.reason == "unauthorized_requester"


async def test_apply_pushes_a_commit(apply_ref):
    StubApplier.requested = []
    result = await ReviewPipeline().apply(
        apply_ref,
        comment_id="comment-1",
        requester="dev@example.com",
        overrides={"allow_apply_fixes": True, "applier": "stub"},
    )
    assert result.applied is True
    assert result.file == "example.py"
    assert result.commit_sha
    assert StubApplier.requested == ["example.py"]


async def test_apply_is_a_no_op_for_an_unknown_comment(apply_ref):
    result = await ReviewPipeline().apply(
        apply_ref,
        comment_id="not-a-real-comment",
        requester="dev@example.com",
        overrides={"allow_apply_fixes": True, "applier": "stub"},
    )
    assert result.applied is False
    assert result.reason == "comment_not_found_or_unsupported"


async def test_apply_is_a_no_op_when_nothing_changes(apply_ref):
    """A no-op applier (e.g. the `noop` default) must not push an empty commit."""
    result = await ReviewPipeline().apply(
        apply_ref,
        comment_id="comment-1",
        requester="dev@example.com",
        overrides={"allow_apply_fixes": True, "applier": "noop"},
    )
    assert result.applied is False
    assert result.reason == "no_change_generated"


# --- connector-level checks, independent of the pipeline --------------------


async def test_fake_connector_get_comment_round_trips_a_posted_comment():
    from app.core.models import CommentDraft

    scm = FakeScmConnector()
    pr = PullRequest(ref=PullRequestRef(scm="fake", repository="r", pull_request_id="1"))

    thread_id = await scm.post_comment(
        pr, CommentDraft(body="hello", marker="prreview:x", file="a.py", line=3)
    )
    comment = await scm.get_comment(pr, thread_id)

    assert comment is not None
    assert comment.body == "hello"
    assert comment.file == "a.py"
    assert comment.line == 3


async def test_fake_connector_update_file_records_the_push():
    scm = FakeScmConnector()
    pr = PullRequest(ref=PullRequestRef(scm="fake", repository="r", pull_request_id="1"))

    sha = await scm.update_file(pr, "a.py", "new content", "fix it")

    assert sha
    assert scm.updated_files == [("a.py", "new content")]


async def test_default_connector_refuses_to_push():
    """Azure DevOps (and any connector that hasn't implemented it) must raise,
    not silently no-op, so `ReviewPipeline.apply` knows the push did not
    happen."""
    from app.core.errors import ScmError
    from app.services.scm.azure_devops import AzureDevOpsConnector

    scm = AzureDevOpsConnector(token="fake-pat")
    pr = PullRequest(ref=PullRequestRef(scm="azure_devops", repository="r", pull_request_id="1"))

    with pytest.raises(ScmError):
        await scm.update_file(pr, "a.py", "new content", "fix it")
