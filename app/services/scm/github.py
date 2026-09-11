"""GitHub connector (REST API 2022-11-28).

Authenticates with the token supplied on the request — a bearer token, which
is how GitHub accepts PATs, fine-grained tokens and the Actions
`GITHUB_TOKEN` alike. Nothing is cached between requests and no credential is
ever stored.

Unlike Azure DevOps, GitHub hands back a per-file `patch` already in unified
form, so this connector only has to re-attach a `diff --git` header before
handing it to the shared parser. Hunk handling therefore stays identical to
every other connector.

API reference (verified against the 2022-11-28 docs):
- pull request:  GET  {base}/repos/{owner}/{repo}/pulls/{number}
- changed files: GET  {base}/repos/{owner}/{repo}/pulls/{number}/files
- file content:  GET  {base}/repos/{owner}/{repo}/contents/{path}?ref=
- inline thread: GET/POST {base}/repos/{owner}/{repo}/pulls/{number}/comments
- summary:       GET/POST {base}/repos/{owner}/{repo}/issues/{number}/comments
- resolve:       POST {graphql} minimizeComment
- status:        POST {base}/repos/{owner}/{repo}/statuses/{sha}
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.core.errors import ScmAuthError, ScmError
from app.core.models import (
    ChangeType,
    CommentDraft,
    Diff,
    DiffFile,
    ExistingComment,
    PullRequest,
    PullRequestRef,
    Verdict,
)
from app.services.scm.base import ScmConnector, scm_registry
from app.services.scm.diffparse import language_for, parse_unified_diff

logger = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
DEFAULT_BASE_URL = "https://api.github.com"
REQUEST_TIMEOUT = 30.0
FILE_PAGE_SIZE = 100
MAX_FILE_PAGES = 30
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 10
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

_MARKER_RE = re.compile(r"<!--\s*(prreview:[^\s>]+)\s*-->")

#: GitHub's `status` field on a changed file.
_CHANGE_TYPES = {
    "added": ChangeType.ADD,
    "modified": ChangeType.EDIT,
    "changed": ChangeType.EDIT,
    "removed": ChangeType.DELETE,
    "renamed": ChangeType.RENAME,
    "copied": ChangeType.ADD,
}

#: `thread_id` is opaque to the publisher, so it carries the kind alongside
#: the GraphQL node id that `minimizeComment` needs.
_REVIEW_PREFIX = "review:"
_ISSUE_PREFIX = "issue:"

_MINIMIZE_MUTATION = """
mutation($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: RESOLVED}) {
    minimizedComment { isMinimized }
  }
}
"""


@scm_registry.register("github")
class GitHubConnector(ScmConnector):
    name = "github"

    def __init__(self, token: str | None = None, **options) -> None:
        super().__init__(token, **options)
        self._client: httpx.AsyncClient | None = None

    # -- plumbing ---------------------------------------------------------

    def _require_token(self) -> str:
        if not self.token:
            raise ScmAuthError(
                "No GitHub token was supplied",
                detail="Send the token in the X-SCM-Token header (or scm_token in the body).",
            )
        return self.token

    @staticmethod
    def _owner_repo(ref: PullRequestRef) -> tuple[str, str]:
        """Accept either `repository: "owner/repo"` or owner in `organization`.

        The `owner/repo` form is how GitHub is written everywhere else, so it
        is worth supporting even though `PullRequestRef` was shaped for Azure
        DevOps's three-part addressing.
        """
        repository = (ref.repository or "").strip("/")
        if "/" in repository:
            owner, _, repo = repository.partition("/")
            return owner, repo
        owner = ref.organization or ref.project
        if not owner:
            raise ScmError(
                "GitHub needs a repository owner",
                detail='Pass `repository: "owner/repo"`, or set `organization`.',
            )
        if not repository:
            raise ScmError("GitHub needs a `repository`")
        return owner, repository

    def _base(self, ref: PullRequestRef) -> str:
        host = (ref.base_url or DEFAULT_BASE_URL).rstrip("/")
        owner, repo = self._owner_repo(ref)
        return f"{host}/repos/{owner}/{repo}"

    def _graphql_url(self, ref: PullRequestRef) -> str:
        host = (ref.base_url or DEFAULT_BASE_URL).rstrip("/")
        # GitHub Enterprise serves GraphQL from /api/graphql, github.com from /graphql.
        if host.endswith("/api/v3"):
            return f"{host[: -len('/api/v3')]}/api/graphql"
        return f"{host}/graphql"

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                headers={
                    "Authorization": f"Bearer {self._require_token()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                follow_redirects=True,
                # Tests inject an httpx.MockTransport here.
                transport=self.options.get("transport"),
            )
        return self._client

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("%s %s failed (attempt %d): %s", method, url, attempt, exc)
            else:
                if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(2**attempt)
                    continue
                self._raise_for_status(response, method, url)
                return response
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(2**attempt)
        raise ScmError(
            f"{method} {url} failed after {MAX_ATTEMPTS} attempts", detail=str(last_error)
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response, method: str, url: str) -> None:
        if response.status_code < 400:
            return

        # A 403 with the rate-limit budget spent is not an auth problem, and
        # saying "check your token" there sends people down the wrong path.
        remaining = response.headers.get("x-ratelimit-remaining")
        if response.status_code in (403, 429) and remaining == "0":
            raise ScmError(
                "GitHub rate limit exceeded",
                detail=f"Resets at epoch {response.headers.get('x-ratelimit-reset', 'unknown')}.",
            )
        if response.status_code in (401, 403):
            raise ScmAuthError(
                "GitHub rejected the token",
                detail=(
                    "The token is missing, expired, or lacks scope. A classic PAT needs `repo`; "
                    "a fine-grained token needs Pull requests (Read & write), Contents (Read) "
                    "and Commit statuses (Read & write)."
                ),
            )
        raise ScmError(
            f"GitHub returned {response.status_code} for {method} {url}",
            detail=response.text[:500],
        )

    # -- reading ----------------------------------------------------------

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequest:
        url = f"{self._base(ref)}/pulls/{ref.pull_request_id}"
        data = (await self._request("GET", url)).json()

        head = data.get("head") or {}
        base = data.get("base") or {}
        source = head.get("sha", "")
        target = base.get("sha", "")
        if not source:
            raise ScmError(
                "The pull request has no head commit",
                detail="GitHub returned a pull request without head.sha.",
            )

        return PullRequest(
            ref=ref,
            title=data.get("title", "") or "",
            description=data.get("body", "") or "",
            author=(data.get("user") or {}).get("login", ""),
            source_branch=head.get("ref", ""),
            target_branch=base.get("ref", ""),
            source_commit=source,
            target_commit=target,
            url=data.get("html_url"),
            is_draft=bool(data.get("draft", False)),
        )

    async def get_diff(self, pr: PullRequest) -> Diff:
        """Page through the changed files, re-heading each `patch` as a diff."""
        url = f"{self._base(pr.ref)}/pulls/{pr.ref.pull_request_id}/files"
        files: list[DiffFile] = []

        for page in range(1, MAX_FILE_PAGES + 1):
            params = {"per_page": FILE_PAGE_SIZE, "page": page}
            batch = (await self._request("GET", url, params=params)).json()
            if not isinstance(batch, list):
                raise ScmError("GitHub returned an unexpected changed-files payload")
            for entry in batch:
                parsed = self._to_diff_file(entry)
                if parsed is not None:
                    files.append(parsed)
            if len(batch) < FILE_PAGE_SIZE:
                break
        else:
            logger.warning(
                "Stopped after %d pages of changed files for %s", MAX_FILE_PAGES, pr.ref.slug()
            )

        return Diff(files=files)

    @staticmethod
    def _to_diff_file(entry: dict) -> DiffFile | None:
        path = entry.get("filename") or ""
        if not path:
            return None
        change_type = _CHANGE_TYPES.get(str(entry.get("status", "modified")), ChangeType.EDIT)
        previous_path = entry.get("previous_filename") or None

        patch = entry.get("patch")
        if not patch:
            # No patch means binary, too large for GitHub to render, or a pure
            # rename. None of those are reviewable, but the file still belongs
            # in the diff so it can be reported as skipped.
            return DiffFile(
                path=path,
                previous_path=previous_path,
                change_type=change_type,
                is_binary=True,
                language=language_for(path),
            )

        header = f"diff --git a/{previous_path or path} b/{path}\n"
        body = patch if patch.endswith("\n") else patch + "\n"
        parsed = parse_unified_diff(header + body)
        if not parsed.files:
            return None
        file = parsed.files[0]
        # The `diff --git` header is all the parser saw; GitHub's own metadata
        # is more reliable than inferring add/delete from the patch text.
        file.path = path
        file.previous_path = previous_path
        file.change_type = change_type
        file.language = language_for(path)
        return file

    async def get_file_text(self, pr: PullRequest, path: str) -> str | None:
        """Read a file from the pull request's head commit."""
        url = f"{self._base(pr.ref)}/contents/{path.lstrip('/')}"
        try:
            response = await self._request(
                "GET",
                url,
                params={"ref": pr.source_commit},
                headers={"Accept": "application/vnd.github.raw"},
            )
        except ScmError as exc:
            # A missing .prreview/config.yaml is the normal case, not an error.
            logger.info("No content for %s@%s: %s", path, pr.source_commit[:8], exc.message)
            return None
        if "\x00" in response.text[:8000]:
            return None
        return response.text

    async def list_comments(self, pr: PullRequest) -> list[ExistingComment]:
        """Both inline review comments and the issue comment the summary uses."""
        number = pr.ref.pull_request_id
        base = self._base(pr.ref)
        comments: list[ExistingComment] = []

        for url, prefix, inline in (
            (f"{base}/pulls/{number}/comments", _REVIEW_PREFIX, True),
            (f"{base}/issues/{number}/comments", _ISSUE_PREFIX, False),
        ):
            for page in range(1, MAX_COMMENT_PAGES + 1):
                params = {"per_page": COMMENT_PAGE_SIZE, "page": page}
                batch = (await self._request("GET", url, params=params)).json()
                if not isinstance(batch, list):
                    break
                for entry in batch:
                    match = _MARKER_RE.search(entry.get("body") or "")
                    node_id = entry.get("node_id") or str(entry.get("id", ""))
                    comments.append(
                        ExistingComment(
                            thread_id=f"{prefix}{node_id}",
                            marker=match.group(1) if match else None,
                            # REST does not expose whether a thread is resolved
                            # or minimised — only GraphQL does. A null `line`
                            # means GitHub has marked the comment outdated
                            # because the diff moved past it, which is the one
                            # "no longer live" signal available here.
                            is_closed=bool(inline and entry.get("line") is None),
                            file=entry.get("path") if inline else None,
                            line=entry.get("line") if inline else None,
                        )
                    )
                if len(batch) < COMMENT_PAGE_SIZE:
                    break

        return comments

    # -- writing ----------------------------------------------------------

    async def post_comment(self, pr: PullRequest, comment: CommentDraft) -> str:
        number = pr.ref.pull_request_id
        base = self._base(pr.ref)

        if comment.file and comment.line:
            body: dict[str, Any] = {
                "body": comment.body,
                "commit_id": pr.source_commit,
                "path": comment.file.lstrip("/"),
                "line": comment.line,
                "side": "RIGHT",
            }
            if comment.end_line and comment.end_line > comment.line:
                body["start_line"] = comment.line
                body["start_side"] = "RIGHT"
                body["line"] = comment.end_line
            try:
                response = await self._request("POST", f"{base}/pulls/{number}/comments", json=body)
                return f"{_REVIEW_PREFIX}{_node_id(response)}"
            except ScmError as exc:
                # GitHub refuses an inline comment whose line is not part of
                # the diff it computed. Rather than lose the finding, fall
                # back to the conversation tab — the marker travels in the
                # body either way, so re-runs stay idempotent.
                logger.warning(
                    "Inline comment rejected for %s:%s; posting it as a PR comment instead (%s)",
                    comment.file,
                    comment.line,
                    exc.message,
                )

        response = await self._request(
            "POST", f"{base}/issues/{number}/comments", json={"body": comment.body}
        )
        return f"{_ISSUE_PREFIX}{_node_id(response)}"

    async def close_comment(self, pr: PullRequest, thread_id: str) -> None:
        """Minimise the comment as RESOLVED.

        GitHub's REST API cannot resolve a review thread — only GraphQL can —
        and minimising is the non-destructive equivalent of Azure DevOps's
        "fixed" status: the comment collapses instead of disappearing.
        """
        node_id = thread_id.split(":", 1)[-1]
        if not node_id:
            return
        response = await self._request(
            "POST",
            self._graphql_url(pr.ref),
            json={"query": _MINIMIZE_MUTATION, "variables": {"id": node_id}},
        )
        errors = (response.json() or {}).get("errors")
        if errors:
            # Already minimised, or a token without discussion write access.
            logger.info("Could not minimise %s: %s", thread_id, errors)

    async def set_status(
        self,
        pr: PullRequest,
        verdict: Verdict,
        description: str,
        *,
        name: str = "ai-code-review",
        genre: str = "pr-review-agent",
    ) -> None:
        url = f"{self._base(pr.ref)}/statuses/{pr.source_commit}"
        body: dict[str, Any] = {
            "state": "success" if verdict.passed else "failure",
            # GitHub shows one status per context, and branch protection
            # matches on it, so it has to be stable across runs.
            "context": f"{genre}/{name}",
            "description": description[:140],
        }
        if pr.url:
            body["target_url"] = pr.url
        await self._request("POST", url, json=body)

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _node_id(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("node_id") or payload.get("id") or "")
