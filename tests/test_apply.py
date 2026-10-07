"""End-to-end apply-fix pipeline test: fixture SCM, stub applier, no network."""

from pathlib import Path

import pytest

from app.core.errors import ReviewerError, ScmConflictError, ScmError
from app.core.models import ExistingComment, PullRequest, PullRequestRef
from app.core.pipeline import APPLY_MAX_ATTEMPTS, ReviewPipeline, _match_line_endings
from app.services.apply.base import Applier, applier_registry
from app.services.scm.base import scm_registry
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


@applier_registry.register("exploding")
class ExplodingApplier(Applier):
    name = "exploding"

    async def generate_patch(self, *, file, current_text, finding):
        raise ReviewerError("Model endpoint 'm' call failed")


@applier_registry.register("lf")
class LfApplier(Applier):
    """Answers the way a model does: LF newlines and a final newline."""

    name = "lf"

    async def generate_patch(self, *, file, current_text, finding):
        return current_text.replace("\r\n", "\n").replace("a+b", "a + b") + "\n"


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


# --- safety checks ------------------------------------------------------------


@scm_registry.register("apply-fake")
class TrickyScm(FakeScmConnector):
    """Fixture connector with knobs for the cases the plain fixture can't show.

    `source_config` is served only when reading at the PR's source commit, so
    a test can tell which branch the pipeline took its config from.
    """

    source_config: str | None = None
    target_config: str | None = None
    author: str | None = None
    is_fork = False
    #: How many pushes to refuse with a 409 before accepting one.
    conflicts = 0
    fail_follow_ups = False
    #: Overrides example.py's content, e.g. to serve CRLF text.
    file_text: str | None = None
    pr_reads = 0
    last: "TrickyScm | None" = None
    #: Comments posted by earlier instances — a real PR keeps them between runs.
    remembered: list = []

    def __init__(self, token=None, **options):
        super().__init__(token, **options)
        TrickyScm.last = self

    async def get_pull_request(self, ref):
        TrickyScm.pr_reads += 1
        pr = await super().get_pull_request(ref)
        return pr.model_copy(update={"is_fork": TrickyScm.is_fork})

    async def get_file_text(self, pr, path):
        if path == ".prreview/config.yaml":
            source = pr.source_commit == "cccc3333"
            return TrickyScm.source_config if source else TrickyScm.target_config
        if path == "example.py" and TrickyScm.file_text is not None:
            return TrickyScm.file_text
        return await super().get_file_text(pr, path)

    async def get_comment(self, pr, comment_id):
        comment = await super().get_comment(pr, comment_id)
        if comment is not None and TrickyScm.author is not None:
            comment = comment.model_copy(update={"author": TrickyScm.author})
        return comment

    async def update_file(self, pr, path, new_content, message):
        if TrickyScm.conflicts > 0:
            TrickyScm.conflicts -= 1
            raise ScmConflictError("GitHub returned 409")
        return await super().update_file(pr, path, new_content, message)

    async def list_comments(self, pr):
        existing = await super().list_comments(pr)
        existing += [
            ExistingComment(thread_id=f"old-{i}", marker=c.marker)
            for i, c in enumerate(TrickyScm.remembered)
        ]
        return existing

    async def post_comment(self, pr, comment):
        if TrickyScm.fail_follow_ups:
            raise ScmError("GitHub returned 403")
        TrickyScm.remembered.append(comment)
        return await super().post_comment(pr, comment)

    async def close_comment(self, pr, thread_id):
        if TrickyScm.fail_follow_ups:
            raise ScmError("GraphQL refused")
        await super().close_comment(pr, thread_id)


ENABLED = {"allow_apply_fixes": True, "applier": "stub"}


@pytest.fixture
def tricky_ref(apply_ref):
    for knob, default in (
        ("source_config", None),
        ("target_config", None),
        ("author", None),
        ("is_fork", False),
        ("conflicts", 0),
        ("fail_follow_ups", False),
        ("file_text", None),
        ("pr_reads", 0),
    ):
        setattr(TrickyScm, knob, default)
    TrickyScm.remembered = []
    return apply_ref.model_copy(update={"scm": "apply-fake"})


async def _apply(ref, overrides=None):
    return await ReviewPipeline().apply(
        ref, comment_id="comment-1", requester="dev@example.com", overrides=overrides
    )


async def test_pull_request_cannot_opt_itself_in(tricky_ref):
    TrickyScm.source_config = "allow_apply_fixes: true\napplier: stub\n"
    result = await _apply(tricky_ref)
    assert result.reason == "apply_fixes_disabled"
    assert TrickyScm.last.updated_files == []


async def test_target_branch_config_opts_in(tricky_ref):
    TrickyScm.target_config = "allow_apply_fixes: true\napplier: stub\n"
    result = await _apply(tricky_ref)
    assert result.applied is True


async def test_comment_from_an_untrusted_author_is_not_applied(tricky_ref):
    """A human can paste a comment in the bot's exact format."""
    TrickyScm.author = "drive-by-commenter"
    result = await _apply(tricky_ref, ENABLED)
    assert result.reason == "not_a_bot_suggestion"
    assert TrickyScm.last.updated_files == []


async def test_fork_pull_request_is_not_applied(tricky_ref):
    TrickyScm.is_fork = True
    result = await _apply(tricky_ref, ENABLED)
    assert result.reason == "fork_pull_request_unsupported"


async def test_conflict_is_retried_against_the_new_branch_state(tricky_ref):
    """Another /apply on the same file landed first: re-read, regenerate, push."""
    TrickyScm.conflicts = 1
    result = await _apply(tricky_ref, ENABLED)
    assert result.applied is True
    assert TrickyScm.pr_reads == 2  # the PR head was re-read before the retry
    assert len(TrickyScm.last.updated_files) == 1


async def test_branch_that_keeps_moving_is_a_soft_refusal_with_a_reply(tricky_ref):
    TrickyScm.conflicts = 99
    result = await _apply(tricky_ref, ENABLED)
    assert result.applied is False
    assert result.reason == "file_changed_since_review"
    assert TrickyScm.pr_reads == APPLY_MAX_ATTEMPTS
    assert TrickyScm.last.updated_files == []
    [(comment_id, body)] = TrickyScm.last.replies
    assert comment_id == "comment-1"
    assert "Not applied" in body and "/apply" in body


async def test_no_change_replies_in_the_thread(tricky_ref):
    result = await _apply(tricky_ref, {"allow_apply_fixes": True, "applier": "noop"})
    assert result.reason == "no_change_generated"
    [(_, body)] = TrickyScm.last.replies
    assert "Not applied" in body


async def test_model_failure_replies_in_the_thread_and_still_raises(tricky_ref):
    with pytest.raises(ReviewerError):
        await _apply(tricky_ref, {"allow_apply_fixes": True, "applier": "exploding"})
    [(_, body)] = TrickyScm.last.replies
    assert "Could not apply" in body


async def test_unverified_requests_get_no_reply(tricky_ref):
    """Anyone can type /apply; only the author's verified requests get answers."""
    TrickyScm.author = "drive-by-commenter"
    await _apply(tricky_ref, ENABLED)
    assert TrickyScm.last.replies == []


async def test_crlf_file_keeps_its_line_endings(tricky_ref):
    """The model answers in LF; pushing that would rewrite every line."""
    TrickyScm.file_text = "def add(a, b):\r\n    return a+b"
    result = await _apply(tricky_ref, {"allow_apply_fixes": True, "applier": "lf"})
    assert result.applied is True
    [(_, pushed)] = TrickyScm.last.updated_files
    assert pushed == "def add(a, b):\r\n    return a + b"


async def test_failed_follow_ups_do_not_fail_a_landed_commit(tricky_ref):
    TrickyScm.fail_follow_ups = True
    result = await _apply(tricky_ref, ENABLED)
    assert result.applied is True
    assert result.commit_sha
    assert len(result.warnings) == 2


async def test_second_apply_of_the_same_comment_is_a_no_op(tricky_ref):
    """A repeated `/apply` or a redelivered webhook must not patch twice."""
    first = await _apply(tricky_ref, ENABLED)
    assert first.applied is True
    confirmation = TrickyScm.remembered[-1]
    assert f"<!-- {confirmation.marker} -->" in confirmation.body

    second = await _apply(tricky_ref, ENABLED)
    assert second.reason == "already_applied"
    assert TrickyScm.last.updated_files == []


# Deleted on #15 by an /apply that took "private name used outside its module"
# literally; restored because they are the only direct tests of line-ending
# preservation. Testing a private helper is fine.
@pytest.mark.parametrize(
    "original, new, expected",
    [
        ("a\r\nb\r\n", "a\nB\n", "a\r\nB\r\n"),
        ("a\r\nb", "a\nB\n", "a\r\nB"),  # no final newline stays that way
        ("a\nb\n", "a\nB", "a\nB\n"),  # a final newline is restored
        ("a\nb\n", "a\r\nB\r\n", "a\nB\n"),  # LF file stays LF
    ],
)
def test_match_line_endings(original, new, expected):
    assert _match_line_endings(original, new) == expected
