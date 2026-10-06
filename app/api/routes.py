"""HTTP surface.

Mounted on the MLflow `AgentServer` FastAPI app:

    POST /review            run a review (sync, async, or auto)
    GET  /review/{job_id}   poll a background review
    POST /apply             accept one bot suggestion and push it as a commit
    POST /apply/all         accept every open bot suggestion (one commit per file)
    GET  /apply/{job_id}    poll a background apply (single or all)
    POST /config/effective  show the configuration a review would use
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.auth import token_from_request
from app.api.schemas import (
    ApplyAllRequest,
    ApplyRequest,
    ApplyResponse,
    ConfigRequest,
    ConfigResponse,
    ReviewMode,
    ReviewRequest,
    ReviewResponse,
)
from app.core.config import resolve
from app.core.jobs import JobRunner, JobStatus
from app.core.models import ApplyResult, BulkApplyResult, ReviewResult
from app.core.pipeline import ReviewPipeline
from app.services.apply import known_appliers
from app.services.policy import standards
from app.services.quality import known_quality_connectors
from app.services.review import known_reviewers
from app.services.scm import get_connector, known_scm_connectors

logger = logging.getLogger(__name__)


def build_router(runner: JobRunner) -> APIRouter:
    router = APIRouter(tags=["pr-review"])
    pipeline = ReviewPipeline()

    @router.post("/review", response_model=ReviewResponse)
    async def review(body: ReviewRequest, request: Request):
        ref = body.to_ref()
        token = token_from_request(request, body.scm_token)

        async def work():
            return await pipeline.run(
                ref,
                scm_token=token,
                overrides=body.config,
                publish=body.publish,
            )

        if body.mode is ReviewMode.SYNC:
            logger.info("Reviewing %s synchronously", ref.slug())
            return ReviewResponse.completed(await work())

        job = await runner.submit(ref.slug(), work)
        logger.info("Queued review %s for %s (mode=%s)", job.job_id, ref.slug(), body.mode.value)

        if body.mode is ReviewMode.ASYNC:
            return _accepted(job)

        # auto: wait a bounded time, then hand the caller a job id instead of
        # holding a connection the platform will cut at ~120s.
        finished = await runner.wait_for(job.job_id, timeout=_sync_wait(body))
        if finished and finished.status is JobStatus.COMPLETED:
            return ReviewResponse.from_job(finished)
        if finished and finished.status is JobStatus.FAILED:
            return JSONResponse(
                status_code=500, content=ReviewResponse.from_job(finished).model_dump()
            )
        return _accepted(finished or job)

    @router.get("/review/{job_id}", response_model=ReviewResponse)
    async def get_review(job_id: str):
        return await _poll(runner, job_id, ReviewResponse, ReviewResult)

    @router.post("/apply", response_model=ApplyResponse)
    async def apply(body: ApplyRequest, request: Request):
        ref = body.to_ref()
        token = token_from_request(request, body.scm_token)

        async def work():
            return await pipeline.apply(
                ref,
                scm_token=token,
                comment_id=body.comment_id,
                requester=body.requester,
                overrides=body.config,
            )

        return await _run_apply(runner, ref, body, work, "a suggestion")

    @router.post("/apply/all", response_model=ApplyResponse)
    async def apply_all(body: ApplyAllRequest, request: Request):
        ref = body.to_ref()
        token = token_from_request(request, body.scm_token)

        async def work():
            return await pipeline.apply_all(
                ref, scm_token=token, requester=body.requester, overrides=body.config
            )

        return await _run_apply(runner, ref, body, work, "all suggestions")

    @router.get("/apply/{job_id}", response_model=ApplyResponse)
    async def get_apply(job_id: str):
        return await _poll(runner, job_id, ApplyResponse, (ApplyResult, BulkApplyResult))

    @router.post("/config/effective", response_model=ConfigResponse)
    async def effective_config(body: ConfigRequest, request: Request):
        ref = body.to_ref()
        scm = get_connector(ref.scm, token=token_from_request(request, body.scm_token))
        try:
            pr = await scm.get_pull_request(ref)
            config, sources = await pipeline.resolve_config_sources(scm, pr, body.config)
            raw = await scm.get_file_text(pr, config.config_path)
            bundle = await standards.load(scm, pr, config)
        finally:
            await scm.aclose()

        return ConfigResponse(
            config=config,
            fingerprint=config.fingerprint(),
            sources=sources,
            repo_config_path=config.config_path,
            repo_config_found=bool(raw and raw.strip()),
            standards_sources=bundle.sources,
            available_plugins={
                "scm": known_scm_connectors(),
                "quality": known_quality_connectors(),
                "reviewer": known_reviewers(),
                "applier": known_appliers(),
            },
        )

    return router


async def _run_apply(runner: JobRunner, ref, body: ReviewRequest, work, what: str):
    """sync / async / auto handling shared by both apply routes."""
    if body.mode is ReviewMode.SYNC:
        logger.info("Applying %s on %s synchronously", what, ref.slug())
        return ApplyResponse.completed(await work())

    job = await runner.submit(ref.slug(), work)
    logger.info("Queued apply %s for %s (mode=%s)", job.job_id, ref.slug(), body.mode.value)

    if body.mode is ReviewMode.ASYNC:
        return _accepted(job, ApplyResponse)

    finished = await runner.wait_for(job.job_id, timeout=_sync_wait(body))
    if finished and finished.status is JobStatus.COMPLETED:
        return ApplyResponse.from_job(finished)
    if finished and finished.status is JobStatus.FAILED:
        return JSONResponse(status_code=500, content=ApplyResponse.from_job(finished).model_dump())
    return _accepted(finished or job, ApplyResponse)


async def _poll(runner: JobRunner, job_id: str, response_cls: type, result_cls):
    """Shared by both poll routes; reviews and applies share one job store."""
    job = await runner.store.get(job_id)
    if job is None or (job.result is not None and not isinstance(job.result, result_cls)):
        # A job of the other kind is reported as missing here rather than
        # failing response validation with a 500.
        return JSONResponse(
            status_code=404,
            content={
                "status": "unknown",
                "job_id": job_id,
                "error": (
                    "No such job. Job state is per-process and expires, so this can also "
                    "mean the app restarted or another replica served the request. "
                    "Reviews are polled at /review/{job_id}, applies at /apply/{job_id}."
                ),
            },
        )
    if job.status is JobStatus.FAILED:
        return JSONResponse(status_code=500, content=response_cls.from_job(job).model_dump())
    return response_cls.from_job(job)


def _accepted(job, response_cls: type = ReviewResponse) -> JSONResponse:
    return JSONResponse(status_code=202, content=response_cls.from_job(job).model_dump())


def _sync_wait(body: ReviewRequest) -> float:
    """Auto-mode wait, overridable per request without a repo round-trip."""
    override = body.config.get("sync_wait_seconds")
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid sync_wait_seconds: %r", override)
    return resolve().sync_wait_seconds
