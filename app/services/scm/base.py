"""SCM connector contract.

Adding a provider (GitHub, GitLab, Bitbucket) means writing one subclass and
registering it — nothing else in the app changes, because everything
downstream only sees the provider-agnostic types in `app.core.models`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.errors import ScmError
from app.core.models import (
    CommentDraft,
    Diff,
    ExistingComment,
    PullRequest,
    PullRequestRef,
    SourceComment,
    Verdict,
)
from app.core.registry import Registry


class ScmConnector(ABC):
    """One instance per review. Constructed with the caller's SCM token."""

    #: Name this connector was registered under; used in log and error text.
    name: str = "scm"

    def __init__(self, token: str | None = None, **options) -> None:
        self.token = token
        self.options = options

    # -- reading ----------------------------------------------------------

    @abstractmethod
    async def get_pull_request(self, ref: PullRequestRef) -> PullRequest:
        """Fetch metadata, including the source and target commit ids."""

    @abstractmethod
    async def get_diff(self, pr: PullRequest) -> Diff:
        """Return the changes between the target and source commits."""

    @abstractmethod
    async def get_file_text(self, pr: PullRequest, path: str) -> str | None:
        """Read a file from the pull request's source branch.

        Returns `None` when the file does not exist — callers treat a missing
        `.prreview/config.yaml` as "use defaults", not as an error.
        """

    @abstractmethod
    async def list_comments(self, pr: PullRequest) -> list[ExistingComment]:
        """Existing comment threads, so re-runs stay idempotent."""

    async def get_comment(self, pr: PullRequest, comment_id: str) -> SourceComment | None:
        """Fetch one existing comment, body included, by provider-native id.

        `None` means "unsupported by this connector" or "not found" — both
        are soft outcomes for a caller like `ReviewPipeline.apply`. Optional:
        connectors that never need to re-read a comment's body may no-op.
        """
        return None

    # -- writing ----------------------------------------------------------

    @abstractmethod
    async def post_comment(self, pr: PullRequest, comment: CommentDraft) -> str:
        """Create one comment thread. Returns the new thread id."""

    async def close_comment(self, pr: PullRequest, thread_id: str) -> None:
        """Mark a thread resolved. Optional: connectors may no-op."""
        return None

    async def reply_to_comment(self, pr: PullRequest, comment_id: str, body: str) -> None:
        """Answer in the thread of the comment `comment_id` (the same id
        `get_comment` takes). Connectors without threaded replies fall back to
        a pull-request-level comment, so the answer is still visible."""
        await self.post_comment(
            pr, CommentDraft(body=body, marker=f"prreview:reply:{comment_id}")
        )

    async def update_file(
        self, pr: PullRequest, path: str, new_content: str, message: str
    ) -> str:
        """Push a commit replacing one file's content. Returns the new commit id.

        Optional: a connector that cannot push code raises rather than
        silently no-opping, since the caller (`ReviewPipeline.apply`) needs
        to know the push did not happen.
        """
        raise ScmError(f"{self.name} does not support pushing commits")

    async def set_status(
        self,
        pr: PullRequest,
        verdict: Verdict,
        description: str,
        *,
        name: str = "ai-code-review",
        genre: str = "pr-review-agent",
    ) -> None:
        """Publish a pass/fail signal a branch policy can gate on. Optional.

        `name` and `genre` come from the effective config, which is only known
        after the connector exists — hence per call rather than per instance.
        """
        return None

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Release network resources. Called in a `finally` by the pipeline."""
        return None


scm_registry: Registry[ScmConnector] = Registry("SCM connector")
