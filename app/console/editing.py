"""Settings pages: model, config, standards, prompts, history and audit log.

Everything here writes through the `SettingsStore` (a UC volume in
production), never to the app's files, so a change applies to the next
review without a redeploy and every save lands in `history/`.

Scope: when the top bar has a repository selected, edits default to that
repository's own settings (`repos/<scm>/<key>/...`); "Global" applies to
every repository without its own value. Global changes, deletions and
rollbacks go through a confirmation step that states the effect.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.console import analytics
from app.console.auth import ConsoleUser
from app.console.context import ConsoleContext, prices, repo_key
from app.core.config import EffectiveConfig, resolve_with_sources
from app.core.errors import ConfigError
from app.core.models import PullRequestRef
from app.core.settings_store import (
    CONFIG_FILE,
    CONSOLE_FILE,
    FileStore,
    RepoKey,
    get_settings_store,
)
from app.services.apply import prompts as apply_prompts
from app.services.policy import standards
from app.services.review import prompts as review_prompts

logger = logging.getLogger(__name__)

ALLOWED_MODELS_ENV = "PRREVIEW_ALLOWED_MODELS"
TEST_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sample-pr"
_STANDARD_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_APPLIES_TO_KEYS = {"languages", "path_globs", "content_match"}

PROMPTS = {
    "review_guidance": ("Review", review_prompts.GUIDANCE, review_prompts.OUTPUT_CONTRACT),
    "apply_guidance": ("Apply", apply_prompts.GUIDANCE, apply_prompts.OUTPUT_CONTRACT),
}

LAYER_LABELS = {
    "defaults": "app/defaults/config.yaml",
    "environment": "Environment (databricks.yml)",
    "repository": "Repository .prreview/config.yaml",
    "request": "Request",
    "console": "Console",
    "pinned": "Test",
}


def register(router: APIRouter, ctx: ConsoleContext) -> None:
    """Add the settings pages to the console router."""

    # -- model ----------------------------------------------------------------

    async def model_page(request: Request, user: ConsoleUser, **extra: Any) -> Response:
        scope, key = _scope(request)
        config, sources = _effective(key)
        current = config.model_endpoint
        repo, days = ctx.common(request)
        records, error = ctx.load(days)
        return ctx.page(
            request,
            "model",
            user,
            error=error,
            scope=scope,
            current=current,
            current_source=LAYER_LABELS.get(sources.get("model_endpoint", ""), "Built-in default"),
            models=_allowed_models(current),
            model_prices=prices(),
            baseline=_baseline(records, repo, current),
            saved=_last_change(_config_path(key)),
            **ctx.filters(records, repo, days),
            **extra,
        )

    async def model_save(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        scope, key = _scope(request, form)
        model = form.get("model", "")
        if model not in _allowed_models(None):
            return await model_page(request, user, form_error=f"{model!r} is not an allowed model.")
        if key is None and form.get("confirm") != "yes":
            return _confirm(
                ctx, request, user, form,
                title="Change the model for every repository",
                effect=(
                    f"Every repository without its own model setting will be reviewed with "
                    f"{model} from the next review on."
                ),
                action="/console/model",
            )
        store = _store()
        overrides = _yaml_or_empty(store.raw(_config_path(key)))
        overrides["model_endpoint"] = model
        store.write(_config_path(key), _dump(overrides), actor=user.email)
        return _back(request, "/console/model", scope)

    async def model_test(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        model = form.get("model", "")
        if model not in _allowed_models(None):
            return await model_page(request, user, form_error=f"{model!r} is not an allowed model.")
        return await model_page(request, user, test=await _dry_run(model), tested_model=model)

    async def prices_save(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        table: dict[str, dict[str, float]] = {}
        try:
            for model in _allowed_models(None):
                cells = {k: form.get(f"{k}:{model}", "").strip() for k in ("input", "output")}
                if any(cells.values()):
                    table[model] = {k: float(v or 0) for k, v in cells.items()}
                    # float() accepts "nan" and "inf"; NaN also slips past `< 0`.
                    if not all(math.isfinite(v) and v >= 0 for v in table[model].values()):
                        raise ValueError
        except ValueError:
            return await model_page(
                request, user, price_error="Prices must be finite numbers of zero or more."
            )
        if form.get("confirm") != "yes":
            return _confirm(
                ctx, request, user, form,
                title="Change model prices",
                effect="Cost figures for every repository are calculated with these prices.",
                action="/console/model/prices",
            )
        store = _store()
        console = _yaml_or_empty(store.raw(CONSOLE_FILE))
        console["prices"] = table
        store.write(CONSOLE_FILE, _dump(console), actor=user.email)
        return _back(request, "/console/model", "global")

    # -- config ---------------------------------------------------------------

    async def config_page(request: Request, user: ConsoleUser, **extra: Any) -> Response:
        scope, key = _scope(request)
        _, sources = _effective(key)
        store = get_settings_store()
        path = _config_path(key)
        text = store.raw(path) if isinstance(store, FileStore) else None
        return ctx.page(
            request,
            "config",
            user,
            scope=scope,
            rows=_config_rows(key, sources),
            path=path,
            text=extra.pop("text", text or ""),
            saved=_last_change(path),
            **extra,
        )

    async def config_save(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        scope, key = _scope(request, form)
        text = form.get("text", "")
        try:
            overrides = _parse_overrides(text)
            resolve_with_sources(console_overrides=overrides)
        except (ValueError, ConfigError) as exc:
            message = getattr(exc, "message", None) or str(exc)
            detail = getattr(exc, "detail", None)
            return await config_page(
                request, user, text=text, form_error=f"{message}{': ' + detail if detail else ''}"
            )
        if key is None and form.get("confirm") != "yes":
            return _confirm(
                ctx, request, user, form,
                title="Change configuration for every repository",
                effect="These values apply to every repository that does not set its own.",
                action="/console/config",
            )
        _store().write(_config_path(key), text, actor=user.email)
        return _back(request, "/console/config", scope)

    # -- standards --------------------------------------------------------------

    async def standards_page(request: Request, user: ConsoleUser, **extra: Any) -> Response:
        scope, key = _scope(request)
        store = _store_or_none()
        console_names = store.standards_names(key) if store else []
        # Global scope with nothing in the console yet: show the bundled set.
        bundled = key is None and not console_names
        if bundled:
            raws = {
                p.stem: p.read_text(encoding="utf-8")
                for p in sorted(standards.STANDARDS_DIR.glob("*.md"))
            }
        else:
            raws = {n: store.raw(_standard_path(n, key)) or "" for n in console_names}
        selected = extra.pop("name", None) or request.query_params.get("name")
        creating = bool(extra.pop("creating", False)) or "new" in request.query_params
        text = extra.pop("text", None)
        if text is None:
            text = raws.get(selected, "") if selected else ""
        path = _standard_path(selected, key) if selected and not bundled else None
        return ctx.page(
            request,
            "standards",
            user,
            scope=scope,
            source=(
                "Bundled with the app" if bundled
                else "Console" if key is None
                else "Console, this repository only"
            ),
            bundled=bundled,
            items=[_describe(name, raw) for name, raw in raws.items()],
            selected=selected if selected in raws or creating else None,
            creating=creating,
            text=text,
            path=path,
            saved=_last_change(path),
            **extra,
        )

    async def standards_save(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        scope, key = _scope(request, form)
        name, text = form.get("name", "").strip(), form.get("text", "")
        problem = _check_standard(name, text)
        if problem:
            return await standards_page(
                request, user, name=name, text=text, form_error=problem, creating=True
            )
        _store().write(_standard_path(name, key), text, actor=user.email)
        return _back(request, "/console/standards", scope, name=name)

    async def standards_delete(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        scope, key = _scope(request, form)
        name = form.get("name", "")
        if not _STANDARD_NAME.fullmatch(name):
            return _back(request, "/console/standards", scope)
        if form.get("confirm") != "yes":
            where = "every repository" if key is None else str(key)
            return _confirm(
                ctx, request, user, form,
                title=f"Delete the standard {name}",
                effect=f"Reviews for {where} will no longer use it. It stays in the history.",
                action="/console/standards/delete",
            )
        _store().delete(_standard_path(name, key), actor=user.email)
        return _back(request, "/console/standards", scope)

    async def standards_import(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        if form.get("confirm") != "yes":
            return _confirm(
                ctx, request, user, form,
                title="Copy the bundled standards into the console",
                effect=(
                    "From then on reviews use the console's copies, which you can edit. "
                    "Later changes to the bundled files will not apply until re-imported."
                ),
                action="/console/standards/import",
            )
        store = _store()
        for path in sorted(standards.STANDARDS_DIR.glob("*.md")):
            store.write(
                f"standards/{path.stem}.md", path.read_text(encoding="utf-8"),
                actor=user.email, action="import",
            )
        return _back(request, "/console/standards", "global")

    # -- prompts ----------------------------------------------------------------

    async def prompts_page(request: Request, user: ConsoleUser, **extra: Any) -> Response:
        store = get_settings_store()
        drafts = extra.pop("drafts", {})
        items = []
        for name, (title, guidance, contract) in PROMPTS.items():
            override = store.prompt(name)
            items.append(
                {
                    "name": name,
                    "title": title,
                    "guidance": drafts.get(name, override or guidance.strip()),
                    "overridden": override is not None,
                    "contract": contract.strip(),
                    "path": f"prompts/{name}.md",
                    "saved": _last_change(f"prompts/{name}.md"),
                }
            )
        return ctx.page(request, "prompts", user, prompts=items, **extra)

    async def prompts_save(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        name, text = form.get("name", ""), form.get("text", "")
        if name not in PROMPTS:
            return _back(request, "/console/prompts", "global")
        if "Return JSON only" in text:
            return await prompts_page(
                request, user, drafts={name: text}, errors={
                    name: "Leave out the output format; it is added automatically and cannot be edited."
                },
            )
        if not text.strip():
            return await prompts_page(
                request, user, drafts={name: text},
                errors={name: "Guidance cannot be empty. Use Reset to go back to the bundled text."},
            )
        if form.get("confirm") != "yes":
            return _confirm(
                ctx, request, user, form,
                title=f"Change the {PROMPTS[name][0].lower()} prompt guidance",
                effect="Every review from the next one on is sent this guidance.",
                action="/console/prompts",
            )
        _store().write(f"prompts/{name}.md", text, actor=user.email)
        return _back(request, "/console/prompts", "global")

    async def prompts_reset(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        name = form.get("name", "")
        if name not in PROMPTS:
            return _back(request, "/console/prompts", "global")
        if form.get("confirm") != "yes":
            return _confirm(
                ctx, request, user, form,
                title=f"Reset the {PROMPTS[name][0].lower()} prompt guidance",
                effect="Reviews go back to the guidance bundled with the app.",
                action="/console/prompts/reset",
            )
        _store().delete(f"prompts/{name}.md", actor=user.email)
        return _back(request, "/console/prompts", "global")

    # -- history and audit ---------------------------------------------------------

    async def history_page(request: Request, user: ConsoleUser) -> Response:
        store = _store_or_none()
        path = request.query_params.get("path", "")
        version = request.query_params.get("version")
        entries, problem = [], None
        if store and path:
            try:
                entries = store.history(path)
            except Exception as exc:
                logger.warning("Could not read history for %s", path, exc_info=True)
                problem = f"Could not read the history: {exc}"
        compare = None
        if store and version:
            try:
                old = store.read_version(version) or ""
                current = store.raw(path) or ""
            except ValueError as exc:
                problem = str(exc)
            else:
                compare = {"version": version, "rows": _side_by_side(old, current)}
        return ctx.page(
            request, "history", user, active="audit", path=path, entries=entries,
            compare=compare, error=problem,
        )

    async def history_restore(request: Request, user: ConsoleUser) -> Response:
        form = await _form(request)
        path, version = form.get("path", ""), form.get("version", "")
        store = _store()
        if form.get("confirm") != "yes":
            return _confirm(
                ctx, request, user, form,
                title=f"Restore {path}",
                effect=f"The version saved as {version} replaces the current content.",
                action="/console/history/restore",
            )
        try:
            text = store.read_version(version)
        except ValueError as exc:
            return ctx.page(
                request, "message", user, status_code=400, title="Cannot restore",
                message=str(exc), active="audit",
            )
        if text:
            store.write(path, text, actor=user.email, action="rollback")
        else:
            store.delete(path, actor=user.email)
        return RedirectResponse(
            f"/console/history?{urlencode({'path': path})}", status_code=303
        )

    async def audit_page(request: Request, user: ConsoleUser) -> Response:
        store = _store_or_none()
        entries, problem = [], None
        if store:
            try:
                entries = store.history()
            except Exception as exc:
                logger.warning("Could not read the audit log", exc_info=True)
                problem = f"Could not read the audit log from {store.location}: {exc}"
        return ctx.page(request, "audit", user, entries=entries, error=problem)

    for path, handler in (
        ("/model", model_page),
        ("/config", config_page),
        ("/standards", standards_page),
        ("/prompts", prompts_page),
        ("/history", history_page),
        ("/audit", audit_page),
    ):
        router.get(path)(ctx.guarded(handler))
    for path, handler in (
        ("/model", model_save),
        ("/model/test", model_test),
        ("/model/prices", prices_save),
        ("/config", config_save),
        ("/standards", standards_save),
        ("/standards/delete", standards_delete),
        ("/standards/import", standards_import),
        ("/prompts", prompts_save),
        ("/prompts/reset", prompts_reset),
        ("/history/restore", history_restore),
    ):
        router.post(path)(ctx.guarded(handler, edit=True))


# -- helpers ------------------------------------------------------------------------


async def _form(request: Request) -> dict[str, str]:
    """The console's forms are URL-encoded text only; no multipart parser needed."""
    body = (await request.body()).decode("utf-8")
    return dict(parse_qsl(body, keep_blank_values=True))


def _scope(request: Request, form: dict[str, str] | None = None) -> tuple[str, RepoKey | None]:
    """`repo` when a repository is selected (unless 'global' was chosen)."""
    key = repo_key(request.query_params.get("repo"))
    wanted = (form or {}).get("scope") or request.query_params.get("scope")
    if key is None or wanted == "global":
        return "global", None
    return "repo", key


def _store() -> FileStore:
    store = get_settings_store()
    if not isinstance(store, FileStore):  # guarded() refuses edits before this
        raise RuntimeError("Settings storage is not configured")
    return store


def _store_or_none() -> FileStore | None:
    store = get_settings_store()
    return store if isinstance(store, FileStore) else None


def _config_path(key: RepoKey | None) -> str:
    return f"{key.settings_dir}/{CONFIG_FILE}" if key else CONFIG_FILE


def _standard_path(name: str, key: RepoKey | None) -> str:
    return f"{key.settings_dir}/standards/{name}.md" if key else f"standards/{name}.md"


def _last_change(path: str | None):
    store = _store_or_none()
    if not store or not path:
        return None
    try:
        entries = store.history(path, limit=1)
    except Exception:
        logger.warning("Could not read the last change to %s", path, exc_info=True)
        return None
    return entries[0] if entries else None


def _back(request: Request, page: str, scope: str, **extra: str) -> RedirectResponse:
    params = {k: v for k, v in request.query_params.items() if k in ("repo", "days")}
    if scope == "global":
        params["scope"] = "global"
    params.update(extra)
    return RedirectResponse(f"{page}?{urlencode(params)}", status_code=303)


def _confirm(ctx, request, user, form, *, title: str, effect: str, action: str) -> Response:
    hidden = {k: v for k, v in form.items() if k != "confirm"}
    return ctx.page(
        request,
        "confirm",
        user,
        active="",
        title=title,
        effect=effect,
        action=f"{action}?{request.url.query}" if request.url.query else action,
        hidden=hidden,
        cancel=request.headers.get("referer") or "/console",
    )


def _yaml_or_empty(text: str | None) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False)


def _parse_overrides(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Write one setting per line, as `key: value`.")
    return data


def _effective(key: RepoKey | None) -> tuple[EffectiveConfig, dict[str, str]]:
    return resolve_with_sources(console_overrides=get_settings_store().config_overrides(key))


def _config_rows(key: RepoKey | None, sources: dict[str, str]) -> list[dict[str, Any]]:
    config, _ = _effective(key)
    store = get_settings_store()
    global_console = store.config_overrides(None)
    repo_console = (
        _yaml_or_empty(store.raw(_config_path(key))) if key and isinstance(store, FileStore) else {}
    )
    rows = []
    for name in EffectiveConfig.model_fields:
        source = sources.get(name)
        label = LAYER_LABELS.get(source, "Built-in default") if source else "Built-in default"
        if source == "console":
            label = "Console (this repository)" if name in repo_console else "Console (global)"
        value = getattr(config, name)
        rows.append(
            {
                "name": name,
                "value": getattr(value, "value", value),
                "source": label,
                "overrides_global": name in repo_console and name in global_console,
            }
        )
    return rows


def _allowed_models(current: str | None) -> list[dict[str, str]] | list[str]:
    """Models the app has CAN_QUERY on (from databricks.yml). With `current`,
    return rows with endpoint state for the page; without, just the names."""
    names = [m.strip() for m in os.getenv(ALLOWED_MODELS_ENV, "").split(",") if m.strip()]
    if current and current not in names:
        names.insert(0, current)
    if current is None:
        return names
    return [{"name": n, "state": _endpoint_state(n)} for n in names]


_STATE_CACHE: dict[str, tuple[float, str]] = {}


def _endpoint_state(name: str) -> str:
    hit = _STATE_CACHE.get(name)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    try:
        from databricks.sdk import WorkspaceClient

        endpoint = WorkspaceClient().serving_endpoints.get(name)
        ready = getattr(getattr(endpoint, "state", None), "ready", None)
        state = "Ready" if getattr(ready, "value", ready) == "READY" else "Not ready"
    except Exception:
        logger.warning("Could not read the state of serving endpoint %s", name, exc_info=True)
        state = "Unknown"
    _STATE_CACHE[name] = (time.monotonic(), state)
    return state


def _baseline(records, repo: str | None, model: str) -> dict[str, Any] | None:
    """Recent reviews on the current model in this scope, to compare a test with."""
    rows = [
        r for r in analytics.recent_reviews(records, repo=repo, limit=500)
        if r.model == model and not r.failed and r.total_tokens
    ]
    if not rows:
        return None
    return {
        "reviews": len(rows),
        "tokens": sum(r.total_tokens for r in rows) / len(rows),
        "seconds": sum(r.duration_ms for r in rows) / len(rows) / 1000,
    }


async def _dry_run(model: str) -> dict[str, Any]:
    """Review the bundled sample pull request with `model`; nothing is posted."""
    import mlflow

    from app.core.pipeline import ReviewPipeline

    ref = PullRequestRef(
        scm="fake",
        repository="console-model-test",
        pull_request_id="0",
        extra={"fixture_dir": str(TEST_FIXTURE)},
    )
    started = time.monotonic()
    try:
        result = await ReviewPipeline().run(
            ref, publish=False, pinned={"model_endpoint": model}
        )
    except Exception as exc:
        logger.warning("Console model test failed for %s", model, exc_info=True)
        return {"ok": False, "error": getattr(exc, "message", None) or str(exc)}
    seconds = time.monotonic() - started
    tokens = None
    try:
        mlflow.flush_trace_async_logging()
        usage = mlflow.get_trace(result.trace_id).info.token_usage if result.trace_id else None
        tokens = (usage or {}).get("total_tokens")
    except Exception:
        logger.debug("No token usage for the model test", exc_info=True)
    counts = result.counts_by_severity()
    return {
        "ok": True,
        "seconds": seconds,
        "tokens": tokens,
        "findings": len(result.findings),
        "errors": counts["error"],
        "warnings": counts["warning"],
        "infos": counts["info"],
        "notes": result.warnings,
        "sample": [
            {"file": f.file, "line": f.line, "severity": f.severity.value, "message": f.message}
            for f in result.findings[:5]
        ],
    }


def _check_standard(name: str, text: str) -> str | None:
    if not _STANDARD_NAME.fullmatch(name):
        return "Name must be lowercase letters, digits, '-' or '_', starting with a letter or digit."
    if not text.strip():
        return "A standard cannot be empty."
    match = _FRONT_MATTER.match(text)
    if text.lstrip().startswith("---") and not match:
        return "The front matter block is not closed with a '---' line."
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            return f"Front matter is not valid YAML: {exc}"
        if not isinstance(meta, dict):
            return "Front matter must be a mapping."
        applies = meta.get("applies_to")
        if applies is not None:
            if not isinstance(applies, dict):
                return "applies_to must be a mapping."
            unknown = set(applies) - _APPLIES_TO_KEYS
            if unknown:
                return f"Unknown applies_to key(s): {', '.join(sorted(map(str, unknown)))}."
            pattern = applies.get("content_match")
            if pattern:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    return f"content_match is not a valid regular expression: {exc}"
    return None


def _describe(name: str, raw: str) -> dict[str, Any]:
    """A standard's routing metadata, read the same way reviews read it."""
    doc = standards._build_doc(name, raw)
    narrowed = []
    if doc.path_globs:
        narrowed.append("Paths: " + ", ".join(doc.path_globs))
    if doc.content_pattern is not None:
        narrowed.append("Content: " + doc.content_pattern.pattern)
    return {
        "name": name,
        "languages": ", ".join(sorted(doc.languages)) or "All",
        "narrowed": "; ".join(narrowed) or "n/a",
        "applies": "Every reviewed file" if doc.unscoped else "Matching files",
    }


def _side_by_side(old: str, new: str) -> list[dict[str, Any]]:
    """Rows for a two-column diff: unchanged, changed, removed or added lines."""
    a, b = old.splitlines(), new.splitlines()
    rows: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        width = max(i2 - i1, j2 - j1)
        for k in range(width):
            left = a[i1 + k] if i1 + k < i2 else None
            right = b[j1 + k] if j1 + k < j2 else None
            rows.append(
                {
                    "kind": tag,
                    "left_no": i1 + k + 1 if left is not None else None,
                    "left": left,
                    "right_no": j1 + k + 1 if right is not None else None,
                    "right": right,
                }
            )
    return rows

