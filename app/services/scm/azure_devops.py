"""Azure DevOps connector (REST API 7.1).

Authenticates with the PAT supplied on the request — basic auth with an empty
username, which is how Azure DevOps accepts PATs. Nothing is cached between
requests and no credential is ever stored.

Azure DevOps does not serve a unified diff. It returns a list of changed
items, so this connector fetches both blob versions and builds the diff with
`difflib`, then hands it to the shared parser. That keeps hunk handling
identical to every other connector.

API reference (verified against the 7.1 docs):
- pull request:  GET  {base}/pullrequests/{id}
- changed files: GET  {base}/diffs/commits?diffCommonCommit=true&...
- file content:  GET  {base}/items?path=...&versionDescriptor.version=...
- threads:       GET/POST {base}/pullRequests/{id}/threads
- thread status: PATCH {base}/pullRequests/{id}/threads/{threadId}
- status:        POST {base}/pullRequests/{id}/statuses
"""

from __future__ import annotations

import asyncio
import base64
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
from app.services.scm.diffparse import build_unified_diff, parse_unified_diff

logger = logging.getLogger(__name__)

API_VERSION = "7.1"
DEFAULT_BASE_URL = "https://dev.azure.com"
REQUEST_TIMEOUT = 30.0
CHANGE_PAGE_SIZE = 200
MAX_CHANGE_PAGES = 10
BLOB_CONCURRENCY = 8
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

_MARKER_RE = re.compile(r"<!--\s*(prreview:[^\s>]+)\s*-->")

#: Azure DevOps returns comma-joined flags, e.g. "edit, rename".
_CHANGE_TYPES = {
    "add": ChangeType.ADD,
    "edit": ChangeType.EDIT,
    "delete": ChangeType.DELETE,
    "rename": ChangeType.RENAME,
    "sourcerename": ChangeType.RENAME,
    "undelete": ChangeType.ADD,
}

_CLOSED_THREAD_STATUSES = {"closed", "fixed", "wontfix", "bydesign"}


@scm_registry.register("azure_devops")
class AzureDevOpsConnector(ScmConnector):
    name = "azure_devops"

    def __init__(self, token: str | None = None, **options) -> None:
        super().__init__(token, **options)
        self._client: httpx.AsyncClient | None = None
        self._ref: PullRequestRef | None = None

    # -- plumbing ---------------------------------------------------------

    def _require_token(self) -> str:
        if not self.token:
            raise ScmAuthError(
                "No Azure DevOps token was supplied",
                detail="Send the PAT in the X-SCM-Token header (or scm_token in the body).",
            )
        return self.token

    def _base(self, ref: PullRequestRef) -> str:
        host = (ref.base_url or DEFAULT_BASE_URL).rstrip("/")
        if not ref.organization:
            raise ScmError("Azure DevOps needs an `organization`")
        if not ref.project:
            raise ScmError("Azure DevOps needs a `project`")
        return f"{host}/{ref.organization}/{ref.project}/_apis/git/repositories/{ref.repository}"

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            credentials = base64.b64encode(f":{self._require_token()}".encode()).decode()
            self._client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Accept": "application/json",
                },
                follow_redirects=False,
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
        # A bad or expired PAT gets an HTML sign-in page, sometimes as 203.
        if response.status_code in (401, 403) or (
            response.status_code == 203 or "text/html" in response.headers.get("content-type", "")
        ):
            raise ScmAuthError(
                "Azure DevOps rejected the token",
                detail=(
                    "The PAT is missing, expired, or lacks scope. Required scopes: "
                    "Code (Read), Pull Request Threads (Read & Write), Code Status (Read & Write)."
                ),
            )
        if response.status_code >= 400:
            raise ScmError(
                f"Azure DevOps returned {response.status_code} for {method} {url}",
                detail=response.text[:500],
            )

    def _params(self, **extra: Any) -> dict[str, Any]:
        return {"api-version": API_VERSION, **{k: v for k, v in extra.items() if v is not None}}

    # -- reading ----------------------------------------------------------

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequest:
        self._ref = ref
        url = f"{self._base(ref)}/pullrequests/{ref.pull_request_id}"
        data = (await self._request("GET", url, params=self._params())).json()

        source = (data.get("lastMergeSourceCommit") or {}).get("commitId", "")
        target = (data.get("lastMergeTargetCommit") or {}).get("commitId", "")
        if not source or not target:
            raise ScmError(
                "The pull request has no merge commits yet",
                detail="Azure DevOps is still computing the merge; retry in a few seconds.",
            )

        return PullRequest(
            ref=ref,
            title=data.get("title", ""),
            description=data.get("description", "") or "",
            author=(data.get("createdBy") or {}).get("uniqueName", ""),
            source_branch=data.get("sourceRefName", ""),
            target_branch=data.get("targetRefName", ""),
            source_commit=source,
            target_commit=target,
            url=(data.get("_links", {}).get("web", {}) or {}).get("href"),
            is_draft=bool(data.get("isDraft", False)),
        )

    async def get_diff(self, pr: PullRequest) -> Diff:
        changes = await self._list_changes(pr)
        semaphore = asyncio.Semaphore(BLOB_CONCURRENCY)

        async def build(change: dict) -> DiffFile | None:
            async with semaphore:
                return await self._diff_one_file(pr, change)

        files = await asyncio.gather(*(build(change) for change in changes), return_exceptions=True)
        out: list[DiffFile] = []
        for change, file in zip(changes, files, strict=True):
            if isinstance(file, Exception):
                logger.warning("Could not diff %s: %s", change.get("item", {}).get("path"), file)
                continue
            if file is not None:
                out.append(file)
        return Diff(files=out)

    async def _list_changes(self, pr: PullRequest) -> list[dict]:
        """Changed blobs between the merge base and the source commit."""
        url = f"{self._base(pr.ref)}/diffs/commits"
        collected: list[dict] = []
        for page in range(MAX_CHANGE_PAGES):
            params = self._params(
                diffCommonCommit="true",
                baseVersion=pr.target_commit,
                baseVersionType="commit",
                targetVersion=pr.source_commit,
                targetVersionType="commit",
                **{"$top": CHANGE_PAGE_SIZE, "$skip": page * CHANGE_PAGE_SIZE},
            )
            payload = (await self._request("GET", url, params=params)).json()
            batch = payload.get("changes") or []
            collected.extend(
                change
                for change in batch
                if not (change.get("item") or {}).get("isFolder")
                and (change.get("item") or {}).get("gitObjectType") in (None, "blob")
            )
            if len(batch) < CHANGE_PAGE_SIZE:
                break
        else:
            logger.warning(
                "Stopped after %d pages of changes for %s", MAX_CHANGE_PAGES, pr.ref.slug()
            )
        return collected

    async def _diff_one_file(self, pr: PullRequest, change: dict) -> DiffFile | None:
        item = change.get("item") or {}
        path = (item.get("path") or "").lstrip("/")
        if not path:
            return None

        change_type = _parse_change_type(change.get("changeType", "edit"))
        previous_path = (change.get("originalPath") or change.get("sourceServerItem") or "").lstrip(
            "/"
        )
        previous_path = previous_path or None

        old_text: str | None = ""
        new_text: str | None = ""
        if change_type is not ChangeType.ADD:
            old_text = await self._get_item_text(pr, previous_path or path, pr.target_commit)
        if change_type is not ChangeType.DELETE:
            new_text = await self._get_item_text(pr, path, pr.source_commit)

        # `None` means the item API refused to hand back text: binary, or a
        # blob this token cannot read. Either way it is not reviewable.
        if (
            old_text is None
            or new_text is None
            or _looks_binary(old_text)
            or _looks_binary(new_text)
        ):
            return DiffFile(path=path, change_type=change_type, is_binary=True)

        patch = build_unified_diff(old_text, new_text, path, previous_path=previous_path)
        if not patch:
            return None
        parsed = parse_unified_diff(patch)
        if not parsed.files:
            return None
        file = parsed.files[0]
        file.change_type = change_type
        file.previous_path = previous_path if previous_path != path else None
        return file

    async def _get_item_text(self, pr: PullRequest, path: str, commit: str) -> str | None:
        url = f"{self._base(pr.ref)}/items"
        params = self._params(
            path=f"/{path.lstrip('/')}",
            includeContent="true",
            **{
                "versionDescriptor.version": commit,
                "versionDescriptor.versionType": "commit",
                "$format": "json",
            },
        )
        try:
            response = await self._request("GET", url, params=params)
        except ScmError as exc:
            logger.info("No content for %s@%s: %s", path, commit[:8], exc.message)
            return None
        try:
            payload = response.json()
        except ValueError:
            return response.text
        content = payload.get("content")
        if content is None:
            return None
        if payload.get("contentMetadata", {}).get("isBinary"):
            return None
        return content

    async def get_file_text(self, pr: PullRequest, path: str) -> str | None:
        """Read a file from the pull request's source commit."""
        return await self._get_item_text(pr, path, pr.source_commit)

    async def list_comments(self, pr: PullRequest) -> list[ExistingComment]:
        url = f"{self._base(pr.ref)}/pullRequests/{pr.ref.pull_request_id}/threads"
        payload = (await self._request("GET", url, params=self._params())).json()

        comments: list[ExistingComment] = []
        for thread in payload.get("value") or []:
            if thread.get("isDeleted"):
                continue
            body = " ".join(
                (comment.get("content") or "") for comment in (thread.get("comments") or [])
            )
            match = _MARKER_RE.search(body)
            context = thread.get("threadContext") or {}
            comments.append(
                ExistingComment(
                    thread_id=str(thread.get("id")),
                    marker=match.group(1) if match else None,
                    is_closed=str(thread.get("status", "")).lower() in _CLOSED_THREAD_STATUSES,
                    file=(context.get("filePath") or "").lstrip("/") or None,
                    line=(context.get("rightFileStart") or {}).get("line"),
                )
            )
        return comments

    # -- writing ----------------------------------------------------------

    async def post_comment(self, pr: PullRequest, comment: CommentDraft) -> str:
        url = f"{self._base(pr.ref)}/pullRequests/{pr.ref.pull_request_id}/threads"
        body: dict[str, Any] = {
            "comments": [{"parentCommentId": 0, "content": comment.body, "commentType": "text"}],
            "status": "active",
        }
        if comment.file and comment.line:
            end_line = comment.end_line or comment.line
            body["threadContext"] = {
                "filePath": f"/{comment.file.lstrip('/')}",
                "rightFileStart": {"line": comment.line, "offset": 1},
                "rightFileEnd": {"line": max(end_line, comment.line), "offset": 1},
            }
        response = await self._request("POST", url, json=body, params=self._params())
        return str(response.json().get("id", ""))

    async def close_comment(self, pr: PullRequest, thread_id: str) -> None:
        url = f"{self._base(pr.ref)}/pullRequests/{pr.ref.pull_request_id}/threads/{thread_id}"
        await self._request("PATCH", url, json={"status": "fixed"}, params=self._params())

    async def set_status(
        self,
        pr: PullRequest,
        verdict: Verdict,
        description: str,
        *,
        name: str = "ai-code-review",
        genre: str = "pr-review-agent",
    ) -> None:
        url = f"{self._base(pr.ref)}/pullRequests/{pr.ref.pull_request_id}/statuses"
        body: dict[str, Any] = {
            "state": "succeeded" if verdict.passed else "failed",
            "description": description[:400],
            "context": {"name": name, "genre": genre},
        }
        if pr.url:
            body["targetUrl"] = pr.url
        await self._request("POST", url, json=body, params=self._params())

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parse_change_type(raw: str) -> ChangeType:
    """`"edit, rename"` -> RENAME; unknown flags fall back to EDIT."""
    flags = [flag.strip().lower() for flag in str(raw).split(",") if flag.strip()]
    for flag in ("delete", "rename", "sourcerename", "add"):
        if flag in flags:
            return _CHANGE_TYPES[flag]
    return ChangeType.EDIT


def _looks_binary(text: str | None) -> bool:
    return bool(text) and "\x00" in text[:8000]
