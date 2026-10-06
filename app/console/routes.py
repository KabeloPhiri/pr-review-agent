"""Admin console, mounted under `/console`.

Server-rendered HTML (Jinja2), no client-side framework and no build step,
so `uv run start-server` stays the only entry point. Every page shares the
repository filter (`?repo=`) and date range (`?days=`) from the top bar, and
every page checks access first (`ConsoleContext.guarded`).

This module holds the read-only views; `editing.py` holds the settings pages.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.console import analytics, editing
from app.console.auth import ConsoleUser
from app.console.context import HERE, ConsoleContext, prices
from app.core.jobs import JobRunner
from app.core.settings_store import get_settings_store

REPO_SORT_KEYS = {
    "repo",
    "reviews",
    "pass_rate",
    "findings_per_review",
    "total_tokens",
    "cost_usd",
    "applied",
    "last_review_ms",
}


def build_console_router(
    runner: JobRunner, *, trace_source: analytics.TraceSource | None = None
) -> APIRouter:
    router = APIRouter(prefix="/console", include_in_schema=False)
    ctx = ConsoleContext(runner, trace_source or analytics.mlflow_trace_source())

    @router.get("/", include_in_schema=False)
    async def slash() -> RedirectResponse:
        return RedirectResponse("/console", status_code=307)

    async def overview_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = ctx.common(request)
        records, error = ctx.load(days)
        model_prices = prices()
        return ctx.page(
            request,
            "overview",
            user,
            error=error,
            data=analytics.overview(records, repo=repo, prices=model_prices),
            running_jobs=runner.running_count,
            priced=bool(model_prices),
            **ctx.filters(records, repo, days),
        )

    async def repositories_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = ctx.common(request)
        records, error = ctx.load(days)
        sort = request.query_params.get("sort", "last_review_ms")
        descending = request.query_params.get("dir", "desc") != "asc"
        rows = analytics.repositories(
            records,
            prices=prices(),
            custom={key.path for key in get_settings_store().repos()},
        )
        return ctx.page(
            request,
            "repositories",
            user,
            error=error,
            rows=analytics.sort_rows(rows, sort, descending, REPO_SORT_KEYS),
            sort=sort,
            descending=descending,
            **ctx.filters(records, repo, days),
        )

    async def reviews_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = ctx.common(request)
        records, error = ctx.load(days)
        return ctx.page(
            request,
            "reviews",
            user,
            error=error,
            reviews=analytics.recent_reviews(records, repo=repo),
            **ctx.filters(records, repo, days),
        )

    router.get("")(ctx.guarded(overview_page))
    router.get("/repositories")(ctx.guarded(repositories_page))
    router.get("/reviews")(ctx.guarded(reviews_page))

    editing.register(router, ctx)

    @router.get("/static/console.css")
    async def stylesheet() -> FileResponse:
        return FileResponse(HERE / "static" / "console.css", media_type="text/css")

    return router
