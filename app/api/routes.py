"""HTTP surface.

Three endpoints, all mounted on the MLflow `AgentServer` FastAPI app:

    POST /review            run a review (sync, async, or auto)
    GET  /review/{job_id}   poll a background review
    POST /config/effective  show the configuration a review would use
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.auth import token_from_request
from app.api.schemas import (
    ConfigRequest,
    ConfigResponse,
    ReviewMode,
    ReviewRequest,
    ReviewResponse,
)
from app.core.config import resolve
from app.core.jobs import JobRunner, JobStatus
from app.core.pipeline import ReviewPipeline
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
        job = await runner.store.get(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "unknown",
                    "job_id": job_id,
                    "error": (
                        "No such job. Job state is per-process and expires, so this can also "
                        "mean the app restarted or another replica served the request."
                    ),
                },
            )
        if job.status is JobStatus.FAILED:
            return JSONResponse(status_code=500, content=ReviewResponse.from_job(job).model_dump())
        return ReviewResponse.from_job(job)

    @router.post("/config/effective", response_model=ConfigResponse)
    async def effective_config(body: ConfigRequest, request: Request):
        ref = body.to_ref()
        scm = get_connector(ref.scm, token=token_from_request(request, body.scm_token))
        try:
            pr = await scm.get_pull_request(ref)
            config = await pipeline.resolve_config(scm, pr, body.config)
            raw = await scm.get_file_text(pr, config.config_path)
            bundle = await standards.load(scm, pr, config)
        finally:
            await scm.aclose()

        return ConfigResponse(
            config=config,
            fingerprint=config.fingerprint(),
            repo_config_path=config.config_path,
            repo_config_found=bool(raw and raw.strip()),
            standards_sources=bundle.sources,
            available_plugins={
                "scm": known_scm_connectors(),
                "quality": known_quality_connectors(),
                "reviewer": known_reviewers(),
            },
        )

    return router


def _accepted(job) -> JSONResponse:
    return JSONResponse(status_code=202, content=ReviewResponse.from_job(job).model_dump())


def _sync_wait(body: ReviewRequest) -> float:
    """Auto-mode wait, overridable per request without a repo round-trip."""
    override = body.config.get("sync_wait_seconds")
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid sync_wait_seconds: %r", override)
    return resolve().sync_wait_seconds
