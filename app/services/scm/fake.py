"""Fixture-backed SCM connector.

This is what makes the whole pipeline runnable offline: point a request at
`scm: fake` and the review runs end to end against files on disk, posting
comments into memory instead of a real pull request. Used by the tests and by
local development before any Azure DevOps credentials exist.

Fixture layout::

    <fixture_dir>/
      pr.json          # optional PullRequest metadata overrides
      diff.patch       # unified diff (required)
      comments.json    # optional [{thread_id, marker, is_closed, body, author}, ...]
      files/           # optional repo files, e.g. files/.prreview/config.yaml

`body`/`author` on a `comments.json` entry are only read by `get_comment` —
`list_comments`/the publisher's idempotency scan never need them.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

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
from app.services.scm.base import ScmConnector, scm_registry
from app.services.scm.diffparse import parse_unified_diff

logger = logging.getLogger(__name__)


@scm_registry.register("fake")
class FakeScmConnector(ScmConnector):
    name = "fake"

    def __init__(self, token: str | None = None, **options) -> None:
        super().__init__(token, **options)
        self.posted: list[CommentDraft] = []
        self.closed: list[str] = []
        self.status: tuple[Verdict, str] | None = None
        self.updated_files: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str]] = []
        self._fixture_dir: Path | None = None

    # -- fixtures ---------------------------------------------------------

    def _resolve_dir(self, ref: PullRequestRef) -> Path:
        candidate = (
            self.options.get("fixture_dir")
            or ref.extra.get("fixture_dir")
            or os.getenv("PRREVIEW_FIXTURE_DIR")
        )
        if not candidate:
            raise ScmError(
                "The fake SCM connector needs a fixture directory",
                detail="Set extra.fixture_dir on the request or PRREVIEW_FIXTURE_DIR.",
            )
        path = Path(candidate).expanduser().resolve()
        if not path.is_dir():
            raise ScmError(f"Fixture directory not found: {path}")
        return path

    @property
    def fixture_dir(self) -> Path:
        if self._fixture_dir is None:
            raise ScmError("get_pull_request() must be called before other operations")
        return self._fixture_dir

    # -- reading ----------------------------------------------------------

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequest:
        self._fixture_dir = self._resolve_dir(ref)
        meta_path = self.fixture_dir / "pr.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        meta.pop("ref", None)
        return PullRequest(
            ref=ref,
            title=meta.get("title", "Fixture pull request"),
            description=meta.get("description", ""),
            author=meta.get("author", "fixture"),
            source_branch=meta.get("source_branch", "refs/heads/feature"),
            target_branch=meta.get("target_branch", "refs/heads/main"),
            source_commit=meta.get("source_commit", "source"),
            target_commit=meta.get("target_commit", "target"),
            url=meta.get("url"),
            is_draft=meta.get("is_draft", False),
        )

    async def get_diff(self, pr: PullRequest) -> Diff:
        patch = self.fixture_dir / "diff.patch"
        if not patch.exists():
            raise ScmError(f"Fixture is missing diff.patch: {self.fixture_dir}")
        return parse_unified_diff(patch.read_text(encoding="utf-8"))

    async def get_file_text(self, pr: PullRequest, path: str) -> str | None:
        if self._fixture_dir is None:
            return None
        target = self.fixture_dir / "files" / path.lstrip("/")
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    def _fixture_comments(self) -> list[dict]:
        stored = self._fixture_dir / "comments.json" if self._fixture_dir else None
        if not stored or not stored.exists():
            return []
        return json.loads(stored.read_text(encoding="utf-8"))

    async def list_comments(self, pr: PullRequest) -> list[ExistingComment]:
        # Usable as a bare comment sink (no fixtures), which is how the
        # publisher tests drive it. Extra keys (`body`, `author`) on a
        # fixture entry are ignored here and only read by `get_comment`.
        # A fixture comment's `thread_id` doubles as its id for `get_comment`.
        existing = [
            ExistingComment(**{"comment_id": item.get("thread_id"), **item})
            for item in self._fixture_comments()
        ]
        # Comments posted in this process count as existing, so a second
        # publish inside one run is a no-op just like a real re-run.
        existing.extend(
            ExistingComment(
                thread_id=f"local-{i}", marker=c.marker, file=c.file, line=c.line, body=c.body
            )
            for i, c in enumerate(self.posted)
        )
        return existing

    async def get_comment(self, pr: PullRequest, comment_id: str) -> SourceComment | None:
        for item in self._fixture_comments():
            if item.get("thread_id") == comment_id and "body" in item:
                return SourceComment(
                    thread_id=comment_id,
                    body=item["body"],
                    file=item.get("file"),
                    line=item.get("line"),
                    author=item.get("author", ""),
                )
        if comment_id.startswith("local-"):
            index = int(comment_id[len("local-") :])
            if 0 <= index < len(self.posted):
                draft = self.posted[index]
                return SourceComment(
                    thread_id=comment_id, body=draft.body, file=draft.file, line=draft.line
                )
        return None

    # -- writing ----------------------------------------------------------

    async def post_comment(self, pr: PullRequest, comment: CommentDraft) -> str:
        self.posted.append(comment)
        logger.info("[fake-scm] comment on %s:%s\n%s", comment.file, comment.line, comment.body)
        return f"local-{len(self.posted) - 1}"

    async def close_comment(self, pr: PullRequest, thread_id: str) -> None:
        self.closed.append(thread_id)

    async def reply_to_comment(self, pr: PullRequest, comment_id: str, body: str) -> None:
        self.replies.append((comment_id, body))
        logger.info("[fake-scm] reply to %s\n%s", comment_id, body)

    async def update_file(
        self, pr: PullRequest, path: str, new_content: str, message: str
    ) -> str:
        self.updated_files.append((path, new_content))
        logger.info("[fake-scm] update %s: %s", path, message)
        return f"fake-commit-{len(self.updated_files) - 1}"

    async def set_status(
        self,
        pr: PullRequest,
        verdict: Verdict,
        description: str,
        *,
        name: str = "ai-code-review",
        genre: str = "pr-review-agent",
    ) -> None:
        self.status = (verdict, description)
        logger.info(
            "[fake-scm] status=%s %s", "passed" if verdict.passed else "failed", description
        )
