"""Admin console pages, mounted under `/console`.

Server-rendered HTML (Jinja2), no client-side framework and no build step,
so `uv run start-server` stays the only entry point. Every page shares the
repository filter (`?repo=`) and date range (`?days=`) from the top bar, and
every page checks access first (`auth.current_user`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic_core import PydanticUndefined

from app.console import analytics
from app.console.auth import ConsoleAccessDenied, ConsoleUser, current_user
from app.core.config import EffectiveConfig, env_overrides, load_defaults
from app.core.jobs import JobRunner
from app.core.settings_store import RepoKey, get_settings_store
from app.services.apply import prompts as apply_prompts
from app.services.policy import standards
from app.services.review import prompts as review_prompts

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
DAY_CHOICES = (1, 7, 30, 90)
DEFAULT_DAYS = 7

NAV = (
    ("overview", "Overview", "/console"),
    ("repositories", "Repositories", "/console/repositories"),
    ("reviews", "Reviews", "/console/reviews"),
    ("config", "Config", "/console/config"),
    ("standards", "Standards", "/console/standards"),
    ("prompts", "Prompts", "/console/prompts"),
)

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
    source = analytics.CachedSource(trace_source or analytics.mlflow_trace_source())
    templates = _templates()

    def load(days: int) -> tuple[list[analytics.TraceRecord], str | None]:
        """Traces for the range, or an explanation of why they are missing."""
        try:
            return source.last_days(days), None
        except Exception as exc:
            logger.warning("Console could not read traces", exc_info=True)
            return [], f"Could not read review traces from MLflow: {exc}"

    def page(request: Request, name: str, user: ConsoleUser, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            f"{name}.html",
            {"user": user, "nav": NAV, "active": name, **context},
        )

    def common(request: Request) -> tuple[str | None, int]:
        repo = request.query_params.get("repo") or None
        try:
            days = int(request.query_params.get("days", DEFAULT_DAYS))
        except ValueError:
            days = DEFAULT_DAYS
        return repo, days if days in DAY_CHOICES else DEFAULT_DAYS

    def filters(records, repo: str | None, days: int) -> dict[str, Any]:
        known = set(analytics.repositories_in(records))
        known |= {key.path for key in get_settings_store().repos()}
        if repo:
            known.add(repo)
        return {
            "repo": repo,
            "days": days,
            "day_choices": DAY_CHOICES,
            "repos": sorted(known),
        }

    def guarded(handler):
        async def wrapper(request: Request) -> Response:
            try:
                user = current_user(request)
            except ConsoleAccessDenied as denied:
                return templates.TemplateResponse(
                    request, "forbidden.html", {"email": denied.email}, status_code=403
                )
            return await handler(request, user)

        wrapper.__name__ = handler.__name__
        return wrapper

    @router.get("/", include_in_schema=False)
    async def slash() -> RedirectResponse:
        return RedirectResponse("/console", status_code=307)

    @router.get("")
    @guarded
    async def overview_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = common(request)
        records, error = load(days)
        prices = _prices()
        return page(
            request,
            "overview",
            user,
            error=error,
            data=analytics.overview(records, repo=repo, prices=prices),
            running_jobs=runner.running_count,
            priced=bool(prices),
            **filters(records, repo, days),
        )

    @router.get("/repositories")
    @guarded
    async def repositories_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = common(request)
        records, error = load(days)
        sort = request.query_params.get("sort", "last_review_ms")
        descending = request.query_params.get("dir", "desc") != "asc"
        rows = analytics.repositories(
            records,
            prices=_prices(),
            custom={key.path for key in get_settings_store().repos()},
        )
        return page(
            request,
            "repositories",
            user,
            error=error,
            rows=analytics.sort_rows(rows, sort, descending, REPO_SORT_KEYS),
            sort=sort,
            descending=descending,
            **filters(records, repo, days),
        )

    @router.get("/reviews")
    @guarded
    async def reviews_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = common(request)
        records, error = load(days)
        return page(
            request,
            "reviews",
            user,
            error=error,
            reviews=analytics.recent_reviews(records, repo=repo),
            **filters(records, repo, days),
        )

    @router.get("/config")
    @guarded
    async def config_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = common(request)
        records, error = load(days)
        repo_key = _repo_key(repo)
        return page(
            request,
            "config",
            user,
            error=error,
            rows=_config_rows(get_settings_store().config_overrides(repo_key)),
            **filters(records, repo, days),
        )

    @router.get("/standards")
    @guarded
    async def standards_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = common(request)
        records, error = load(days)
        store_dir = get_settings_store().standards_dir()
        catalog = standards.discover(store_dir)
        selected = request.query_params.get("name")
        return page(
            request,
            "standards",
            user,
            error=error,
            docs=[catalog.docs[name] for name in catalog.names()],
            selected=catalog.docs.get(selected) if selected else None,
            source="Console" if store_dir else "Bundled with the app",
            **filters(records, repo, days),
        )

    @router.get("/prompts")
    @guarded
    async def prompts_page(request: Request, user: ConsoleUser) -> Response:
        repo, days = common(request)
        records, error = load(days)
        store = get_settings_store()
        prompts = [
            {
                "title": "Review",
                "name": "review_guidance",
                "guidance": store.prompt("review_guidance") or review_prompts.GUIDANCE.strip(),
                "overridden": store.prompt("review_guidance") is not None,
                "contract": review_prompts.OUTPUT_CONTRACT.strip(),
            },
            {
                "title": "Apply",
                "name": "apply_guidance",
                "guidance": store.prompt("apply_guidance") or apply_prompts.GUIDANCE.strip(),
                "overridden": store.prompt("apply_guidance") is not None,
                "contract": apply_prompts.OUTPUT_CONTRACT.strip(),
            },
        ]
        return page(
            request, "prompts", user, error=error, prompts=prompts, **filters(records, repo, days)
        )

    @router.get("/static/console.css")
    async def stylesheet() -> FileResponse:
        return FileResponse(HERE / "static" / "console.css", media_type="text/css")

    return router


# -- helpers -----------------------------------------------------------------


def _prices() -> Mapping[str, Mapping[str, float]]:
    return get_settings_store().console_settings().get("prices", {}) or {}


def _repo_key(repo: str | None) -> RepoKey | None:
    if not repo:
        return None
    try:
        return RepoKey.parse(repo)
    except ValueError:
        return None


def _config_rows(console: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each config key with its effective value and the layer that set it.

    Repo `.prreview/config.yaml` and request overrides are per review, so they
    are not shown here; this is what a repo without its own config gets.
    """
    defaults = load_defaults()
    env = env_overrides()
    rows = []
    for name, field in EffectiveConfig.model_fields.items():
        if name in console:
            value, source = console[name], "Console"
        elif name in env:
            value, source = env[name], "Environment (databricks.yml)"
        elif name in defaults:
            value, source = defaults[name], "app/defaults/config.yaml"
        else:
            default = field.default
            if default is PydanticUndefined and field.default_factory is not None:
                default = field.default_factory()
            value, source = default, "Built-in default"
        rows.append(
            {
                "name": name,
                "value": getattr(value, "value", value),
                "source": source,
                "help": field.description or "",
            }
        )
    return rows


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.filters["num"] = _num
    env.filters["pct"] = lambda v: "n/a" if v is None else f"{v * 100:.1f}%"
    env.filters["secs"] = lambda v: "n/a" if v is None else f"{v:,.1f}"
    env.filters["usd"] = lambda v: "Not priced" if v is None else f"{v:,.2f}"
    env.filters["ts"] = _timestamp
    env.globals["query"] = _query
    env.globals["pr_url"] = _pr_url
    env.globals["trace_url"] = _trace_url
    return templates


def _num(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return f"{value:,}"


def _timestamp(ms: int | None) -> str:
    if not ms:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _query(current: Mapping[str, Any], **changes: Any) -> str:
    """The current filters with some changed, as a query string."""
    merged = {**current, **changes}
    return urlencode({k: v for k, v in merged.items() if v not in (None, "")})


def _pr_url(repo: str, number: str) -> str | None:
    key = _repo_key(repo)
    if key is None or not number:
        return None
    if key.scm == "github":
        return f"https://github.com/{key.slug}/pull/{number}"
    return None


def _trace_url(trace_id: str) -> str | None:
    host = os.getenv("DATABRICKS_HOST", "").strip().rstrip("/")
    experiment = os.getenv("MLFLOW_EXPERIMENT_ID", "").strip()
    if not host or not experiment or not trace_id:
        return None
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    query = urlencode({"selectedEvaluationId": trace_id})
    return f"{host}/ml/experiments/{quote(experiment, safe='')}/traces?{query}"
