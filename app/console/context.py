"""What every console page shares: access checks, filters, rendering.

`ConsoleContext` is built once per app by `build_console_router` and handed
to the page modules (`routes.py` for the read-only views, `editing.py` for
settings), so the top bar, access rules and error handling are identical on
every page.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.console import analytics
from app.console.auth import ConsoleAccessDenied, ConsoleUser, current_user
from app.core.jobs import JobRunner
from app.core.settings_store import RepoKey, get_settings_store

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
DAY_CHOICES = (1, 7, 30, 90)
DEFAULT_DAYS = 7

NAV = (
    ("overview", "Overview", "/console"),
    ("repositories", "Repositories", "/console/repositories"),
    ("reviews", "Reviews", "/console/reviews"),
    ("model", "Model", "/console/model"),
    ("config", "Config", "/console/config"),
    ("standards", "Standards", "/console/standards"),
    ("prompts", "Prompts", "/console/prompts"),
    ("audit", "Audit log", "/console/audit"),
)

Handler = Callable[[Request, ConsoleUser], Awaitable[Response]]


class ConsoleContext:
    def __init__(self, runner: JobRunner, source: analytics.TraceSource) -> None:
        self.runner = runner
        self.source = analytics.CachedSource(source)
        self.templates = _templates()

    # -- request helpers -----------------------------------------------------

    def common(self, request: Request) -> tuple[str | None, int]:
        repo = request.query_params.get("repo") or None
        try:
            days = int(request.query_params.get("days", DEFAULT_DAYS))
        except ValueError:
            days = DEFAULT_DAYS
        return repo, days if days in DAY_CHOICES else DEFAULT_DAYS

    def load(self, days: int) -> tuple[list[analytics.TraceRecord], str | None]:
        """Traces for the range, or an explanation of why they are missing."""
        try:
            return self.source.last_days(days), None
        except Exception as exc:
            logger.warning("Console could not read traces", exc_info=True)
            return [], f"Could not read review traces from MLflow: {exc}"

    def filters(self, records, repo: str | None, days: int) -> dict[str, Any]:
        known = set(analytics.repositories_in(records))
        known |= {key.path for key in get_settings_store().repos()}
        if repo:
            known.add(repo)
        return {"repo": repo, "days": days, "day_choices": DAY_CHOICES, "repos": sorted(known)}

    def page(
        self, request: Request, name: str, user: ConsoleUser, *, status_code: int = 200, **context
    ) -> HTMLResponse:
        """Render a page with the top bar. Loads traces only if the page did not."""
        if "repos" not in context:
            repo, days = self.common(request)
            records, error = self.load(days)
            context = {**self.filters(records, repo, days), **context}
            context.setdefault("error", error)
        store = get_settings_store()
        return self.templates.TemplateResponse(
            request,
            f"{name}.html",
            {
                "user": user,
                "nav": NAV,
                "active": context.pop("active", name),
                "store_editable": bool(getattr(store, "editable", False)),
                "can_edit": user.can_edit and bool(getattr(store, "editable", False)),
                "store_location": getattr(store, "location", None),
                "store_problem": store.status(),
                **context,
            },
            status_code=status_code,
        )

    def guarded(self, handler: Handler, *, edit: bool = False):
        """Check access before `handler`. `edit=True`: admins only, same-origin
        only (a cross-site form post is refused), and a store must exist."""

        async def wrapper(request: Request) -> Response:
            try:
                user = current_user(request)
            except ConsoleAccessDenied as denied:
                return self.templates.TemplateResponse(
                    request, "forbidden.html", {"email": denied.email}, status_code=403
                )
            if edit:
                problem = _edit_refusal(request, user)
                if problem:
                    return self.page(
                        request, "message", user, status_code=403, title="Not allowed",
                        message=problem, active="",
                    )
            return await handler(request, user)

        wrapper.__name__ = handler.__name__
        return wrapper


def _edit_refusal(request: Request, user: ConsoleUser) -> str | None:
    if not user.can_edit:
        return "Your console access is read-only. Ask an administrator to make this change."
    if not getattr(get_settings_store(), "editable", False):
        return "Settings storage is not configured, so nothing can be saved."
    # Cross-site request forgery: browsers mark where a request came from.
    # Refuse anything a page on another site could have submitted.
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return "This change was not submitted from the console and was refused."
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if urlparse(origin).netloc != host:
            return "This change was not submitted from the console and was refused."
    return None


def repo_key(repo: str | None) -> RepoKey | None:
    if not repo:
        return None
    try:
        return RepoKey.parse(repo)
    except ValueError:
        return None


def prices() -> Mapping[str, Mapping[str, float]]:
    return get_settings_store().console_settings().get("prices", {}) or {}


# -- templates -------------------------------------------------------------------


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
    key = repo_key(repo)
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
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/ml/experiments/{experiment}/traces?selectedEvaluationId={trace_id}"
