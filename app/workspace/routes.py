"""PR workspace, mounted under `/workspace`.

Pages are thin HTML shells; `static/workspace.js` fills them from the JSON
API below. Every API call carries the caller's own SCM token in
`X-SCM-Token` — held by the browser tab, never stored here — and the apply
requester is always the login that token belongs to (`ScmConnector.whoami`),
never a name the browser sends. Nothing here decides anything a pull request
comment could not: apply, feedback and alignment go through `ReviewPipeline`
exactly as their REST and `/apply` counterparts do.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import segno
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from app.api.auth import token_from_request
from app.core.errors import ConfigError, PrReviewError, ScmAuthError
from app.core.jobs import JobRunner
from app.core.models import ChangeIntent, PullRequestRef
from app.core.origin import is_cross_site
from app.core.pipeline import ReviewPipeline
from app.services.scm import get_connector
from app.workspace.auth import WorkspaceAccessDenied, WorkspaceUser, current_user, is_deployed
from app.workspace.handoff import HandoffStore

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
#: Remembered so a job can only be polled by whoever started it.
MAX_JOB_OWNERS = 2000

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    # Redeem URLs carry a one-time code; never hand it to another site.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class RefBody(BaseModel):
    """Which pull request. The same addressing as `ReviewRequest`, minus the
    review knobs, plus `fixture_dir` for local development."""

    model_config = {"extra": "forbid"}

    scm: str = "github"
    organization: str | None = None
    project: str | None = None
    repository: str
    pull_request_id: str = ""
    base_url: str | None = None
    fixture_dir: str | None = None

    def to_ref(self) -> PullRequestRef:
        extra: dict[str, str] = {}
        if self.scm == "fake" and is_deployed():
            raise ConfigError("The fixture connector is for local development only")
        if self.fixture_dir:
            if self.scm != "fake":
                raise ConfigError("fixture_dir only applies to the fake connector")
            extra["fixture_dir"] = self.fixture_dir
        return PullRequestRef(
            scm=self.scm,
            organization=self.organization or None,
            project=self.project or None,
            repository=self.repository.strip(),
            pull_request_id=str(self.pull_request_id),
            base_url=self.base_url or None,
            extra=extra,
        )


class CommentBody(RefBody):
    comment_id: str


class FeedbackBody(CommentBody):
    reason: str | None = Field(default=None, max_length=500)


class AlignmentBody(RefBody):
    intent: ChangeIntent = ChangeIntent.FEATURE
    requirement: str = Field(max_length=20000)
    post_comment: bool = False


class RedeemBody(BaseModel):
    model_config = {"extra": "forbid"}

    code: str = Field(min_length=10, max_length=200)


class _Forbidden(PrReviewError):
    status_code = 403


def build_workspace_router(
    runner: JobRunner, *, handoffs: HandoffStore | None = None
) -> APIRouter:
    router = APIRouter(prefix="/workspace", include_in_schema=False)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    pipeline = ReviewPipeline()
    # Not `handoffs or ...`: an empty store has len 0 and would be replaced.
    store = handoffs if handoffs is not None else HandoffStore()
    job_owners: dict[str, str] = {}

    # -- plumbing ----------------------------------------------------------------

    def page(name: str, title: str):
        async def handler(request: Request) -> Response:
            try:
                user = current_user(request)
            except WorkspaceAccessDenied as denied:
                response = templates.TemplateResponse(
                    request, "forbidden.html", {"email": denied.email}, status_code=403
                )
            else:
                response = templates.TemplateResponse(
                    request,
                    f"{name}.html",
                    {"user": user, "title": title, "dev": not is_deployed()},
                )
            response.headers.update(_SECURITY_HEADERS)
            return response

        handler.__name__ = f"{name}_page"
        return handler

    Api = Callable[[Request, WorkspaceUser], Awaitable[Any]]

    def api(handler: Api, *, write: bool = False):
        """Access check, cross-site refusal for writes, and every failure as
        JSON — the same `{error, detail, type}` shape as the REST API."""

        async def wrapper(request: Request) -> Response:
            try:
                user = current_user(request)
                if write and is_cross_site(request):
                    raise _Forbidden("This request did not come from the PR workspace")
                result = await handler(request, user)
            except WorkspaceAccessDenied:
                return _error(_Forbidden("You do not have access to the PR workspace"))
            except ValidationError as exc:
                return _error(ConfigError("Invalid request", detail=str(exc)[:500]))
            except PrReviewError as exc:
                logger.info("Workspace %s: %s %s", request.url.path, type(exc).__name__,
                            exc.message)
                return _error(exc)
            if isinstance(result, Response):
                return result
            return JSONResponse(result, headers={"Cache-Control": "no-store"})

        wrapper.__name__ = handler.__name__
        return wrapper

    async def body_of(request: Request, model: type[BaseModel]) -> Any:
        try:
            data = await request.json()
        except ValueError as exc:
            raise ConfigError("The request body must be JSON") from exc
        return model.model_validate(data)

    def query_ref(request: Request) -> PullRequestRef:
        params = {k: v for k, v in request.query_params.items() if k in RefBody.model_fields}
        return RefBody.model_validate(params).to_ref()

    async def viewer_login(ref: PullRequestRef, token: str | None) -> str | None:
        scm = get_connector(ref.scm, token=token)
        try:
            return await scm.whoami(ref)
        finally:
            await scm.aclose()

    async def submit(user: WorkspaceUser, ref: PullRequestRef, work) -> dict[str, Any]:
        job = await runner.submit(ref.slug(), work)
        job_owners[job.job_id] = user.email
        while len(job_owners) > MAX_JOB_OWNERS:
            job_owners.pop(next(iter(job_owners)))
        return {"status": job.status.value, "job_id": job.job_id}

    # -- pages ---------------------------------------------------------------------

    router.get("")(page("index", "Pull requests"))
    router.get("/pr")(page("pr", "Pull request"))
    router.get("/m/pr")(page("mobile_pr", "Pull request"))
    router.get("/m/redeem")(page("redeem", "Continue on this phone"))

    @router.get("/static/{name}")
    async def static(name: str) -> Response:
        types = {"workspace.css": "text/css", "workspace.js": "text/javascript"}
        if name not in types:
            return Response(status_code=404)
        return FileResponse(
            HERE / "static" / name,
            media_type=types[name],
            headers={"Cache-Control": "no-cache"},
        )

    # -- reading -------------------------------------------------------------------

    async def me(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        scm = request.query_params.get("scm", "github")
        ref = RefBody(
            scm=scm, repository="-", base_url=request.query_params.get("base_url")
        ).to_ref()
        token = token_from_request(request)
        if not token and scm != "fake":
            raise ScmAuthError("Paste a token to connect")
        login = await viewer_login(ref, token)
        return {"login": login, "email": user.email}

    async def pulls(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        ref = query_ref(request)
        scm = get_connector(ref.scm, token=token_from_request(request))
        try:
            found = await scm.list_pull_requests(ref)
        finally:
            await scm.aclose()
        return {
            "pulls": [
                {
                    "number": pr.ref.pull_request_id,
                    "title": pr.title,
                    "author": pr.author,
                    "source_branch": pr.source_branch,
                    "target_branch": pr.target_branch,
                    "is_draft": pr.is_draft,
                    "url": pr.url,
                }
                for pr in found
            ]
        }

    async def pull_request(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        snapshot = await pipeline.snapshot(
            query_ref(request), scm_token=token_from_request(request)
        )
        payload = snapshot.model_dump(mode="json")
        payload["intents"] = [{"value": i.value, "label": i.label} for i in ChangeIntent]
        return payload

    async def job(request: Request, user: WorkspaceUser) -> Response | dict[str, Any]:
        job_id = request.path_params["job_id"]
        found = await runner.store.get(job_id)
        if found is None or job_owners.get(job_id) != user.email:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "unknown",
                    "error": (
                        "This job is no longer known. The app may have restarted, or another "
                        "replica answered. Anything already pushed or posted is on the pull "
                        "request; reload it to see."
                    ),
                },
            )
        return {
            "status": found.status.value,
            "job_id": found.job_id,
            "result": found.result.model_dump(mode="json") if found.result else None,
            "error": found.error,
        }

    # -- acting --------------------------------------------------------------------

    async def apply_one(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        body = await body_of(request, CommentBody)
        ref, token = body.to_ref(), token_from_request(request)
        login = await viewer_login(ref, token)
        if not login:
            return _soft("requester_unverified")

        async def work():
            return await pipeline.apply(
                ref, scm_token=token, comment_id=body.comment_id, requester=login
            )

        return await submit(user, ref, work)

    async def apply_all(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        body = await body_of(request, RefBody)
        ref, token = body.to_ref(), token_from_request(request)
        login = await viewer_login(ref, token)
        if not login:
            return _soft("requester_unverified")

        async def work():
            return await pipeline.apply_all(ref, scm_token=token, requester=login)

        return await submit(user, ref, work)

    async def feedback(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        body = await body_of(request, FeedbackBody)
        ref, token = body.to_ref(), token_from_request(request)
        login = await viewer_login(ref, token)
        if not login:
            return _soft("requester_unverified")
        # No `association`: only the PR author can flag from here. Reviewers
        # with write access still can by replying `/fp` on the pull request.
        result = await pipeline.feedback(
            ref, scm_token=token, comment_id=body.comment_id, requester=login, reason=body.reason
        )
        return {"status": "completed", "result": result.model_dump(mode="json")}

    async def alignment(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        body = await body_of(request, AlignmentBody)
        if not body.requirement.strip():
            raise ConfigError("Paste the requirement or acceptance criteria first")
        ref, token = body.to_ref(), token_from_request(request)

        async def work():
            return await pipeline.check_alignment(
                ref,
                scm_token=token,
                intent=body.intent,
                requirement=body.requirement,
                post_comment=body.post_comment,
            )

        return await submit(user, ref, work)

    # -- phone handoff ---------------------------------------------------------------

    async def handoff_create(request: Request, user: WorkspaceUser) -> dict[str, Any]:
        ref = (await body_of(request, RefBody)).to_ref()
        token = token_from_request(request)
        if not token:
            raise ScmAuthError("Connect a token on this computer first")
        code = store.create(token=token, ref=ref, email=user.email)
        target = f"{_origin(request)}/workspace/m/redeem?{urlencode({'code': code})}"
        svg = segno.make(target, error="m").svg_inline(
            scale=1, border=2, dark="#000", light="#fff", omitsize=True
        )
        return {"code": code, "svg": svg, "expires_in": int(store.ttl)}

    async def handoff_redeem(request: Request, user: WorkspaceUser) -> Response | dict[str, Any]:
        body = await body_of(request, RedeemBody)
        entry, reason = store.redeem(body.code, email=user.email)
        if entry is None:
            message = {
                "expired_or_unknown": (
                    "This code has expired or was already used. Show a new QR code on your "
                    "computer and scan again."
                ),
                "different_user": (
                    "This code was made for a different account. Sign in to Databricks on "
                    "this phone as the person who showed the QR code."
                ),
            }[reason]
            return JSONResponse(
                status_code=410,
                content={"error": message, "reason": reason},
                headers={"Cache-Control": "no-store"},
            )
        params = {
            k: v
            for k, v in {
                "scm": entry.ref.scm,
                "organization": entry.ref.organization,
                "project": entry.ref.project,
                "repository": entry.ref.repository,
                "pull_request_id": entry.ref.pull_request_id,
                "base_url": entry.ref.base_url,
                "fixture_dir": entry.ref.extra.get("fixture_dir"),
            }.items()
            if v
        }
        return {
            "token": entry.token,
            "scm": entry.ref.scm,
            "next": "/workspace/m/pr?" + urlencode(params),
        }

    async def handoff_discard(request: Request, user: WorkspaceUser) -> Response:
        store.discard(request.path_params["code"], email=user.email)
        return Response(status_code=204)

    router.get("/api/me")(api(me))
    router.get("/api/pulls")(api(pulls))
    router.get("/api/pr")(api(pull_request))
    router.get("/api/jobs/{job_id}")(api(job))
    router.post("/api/apply")(api(apply_one, write=True))
    router.post("/api/apply/all")(api(apply_all, write=True))
    router.post("/api/feedback")(api(feedback, write=True))
    router.post("/api/alignment")(api(alignment, write=True))
    router.post("/api/handoff")(api(handoff_create, write=True))
    router.post("/api/handoff/redeem")(api(handoff_redeem, write=True))
    router.delete("/api/handoff/{code}")(api(handoff_discard, write=True))

    return router


def _error(exc: PrReviewError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "detail": exc.detail, "type": type(exc).__name__},
        headers={"Cache-Control": "no-store"},
    )


def _soft(reason: str) -> dict[str, Any]:
    """A refusal shaped like a finished job, so the UI explains it the same way."""
    return {"status": "completed", "result": {"applied": False, "reason": reason}}


def _origin(request: Request) -> str:
    """This app's public origin as the browser sees it, behind the proxy."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"{proto}://{host}"
