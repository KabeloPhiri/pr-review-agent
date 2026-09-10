"""Composition root.

Builds the MLflow `AgentServer` (which brings tracing, `/health` and
`/invocations`) and mounts this app's REST routes on the same FastAPI
application. `app` must stay a module-level name: uvicorn is started by import
string so the server can run multiple workers.
"""

# ruff: noqa: E402
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing anything that builds a Databricks client.
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

from fastapi import Request
from fastapi.responses import JSONResponse
from mlflow.genai.agent_server import AgentServer, setup_mlflow_git_based_version_tracking

# Importing the agent module registers the @invoke() handler with the server.
import app.agent  # noqa: F401
from app.api.routes import build_router
from app.core.errors import PrReviewError
from app.core.jobs import InMemoryJobStore, JobRunner

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

agent_server = AgentServer()
app = agent_server.app

job_runner = JobRunner(InMemoryJobStore())
app.state.job_runner = job_runner
app.include_router(build_router(job_runner))


@app.exception_handler(PrReviewError)
async def _handle_pr_review_error(request: Request, exc: PrReviewError) -> JSONResponse:
    """Domain errors carry their own status code and a fixable message."""
    logger.warning("%s on %s: %s", type(exc).__name__, request.url.path, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "detail": exc.detail, "type": type(exc).__name__},
    )


_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(fastapi_app):
    """Give background reviews a chance to finish before the process exits."""
    async with _original_lifespan(fastapi_app):
        try:
            yield
        finally:
            await job_runner.drain(timeout=float(os.getenv("PRREVIEW_DRAIN_SECONDS", "30")))


app.router.lifespan_context = _lifespan

setup_mlflow_git_based_version_tracking()


def main() -> None:
    agent_server.run(app_import_string="app.server:app")


if __name__ == "__main__":
    main()
