---
title: Admin console (PR 1 of 3, Observe)
type: feature
agent: claude-code
date: 2026-10-06
branch: feature/admin-console
status: deployed-dev
targets: [dev]
relatedTables: []
---

## Context

An admin console for the reviewer: token usage per model, false-positive
tracking, model/config/standards/prompt changes without a redeploy, and a
per-repository view of all of it. Plan:
`~/.claude/plans/gentle-imagining-prism.md`. Delivered in three PRs:
Observe (this one), Control, Quality. Branched from `feature/apply-many`
because #15 was not merged yet; rebase onto `master` once it is.

## What changed

- `app/core/llm_client.py`: every model call is an `llm:<model>` span
  carrying `mlflow.chat.tokenUsage`; MLflow totals it into
  `TraceInfo.token_usage`.
- `app/core/pipeline.py`: traces get `pr.repo`/`pr.number` metadata and
  outcome tags (`prreview.passed`, `findings`, `errors`/`warnings`/`infos`,
  `applied`, `reason`, …). `apply`/`apply_all` became thin traced wrappers
  around `_apply`/`_apply_all` so every return path is tagged.
- `app/core/settings_store.py` (new): `RepoKey`, the `SettingsStore`
  interface and `NullStore` (default; changes nothing).
- `app/core/models.py`: `PullRequestRef.repo_slug()`.
- `app/core/jobs.py`: `JobRunner.running_count`.
- `app/services/{review,apply}/prompts.py`: `SYSTEM_RULES` split into
  `GUIDANCE` (editable later) + `OUTPUT_CONTRACT` (fixed); identical output,
  pinned by hash in `tests/test_prompts.py`.
- `app/console/` (new): `auth.py` (email allowlists from
  `X-Forwarded-Email`), `analytics.py` (pure aggregation over trace
  summaries, 60s cache), `routes.py`, Jinja2 templates, one stylesheet.
  Pages: Overview, Repositories, Reviews, Config, Standards, Prompts
  (the last three read-only in this PR).
- `app/server.py`: mounts the console at `/console`.
- `databricks.yml`: `PRREVIEW_CONSOLE_ADMINS`/`_VIEWERS`; `pyproject.toml`:
  `jinja2`.

## Design decisions

- **No new datastore for analytics.** A local prototype confirmed that
  usage spans inside `asyncio.to_thread` aggregate onto the parent trace and
  that metadata/tag filters work with `include_spans=False`.
- **Charts are server-rendered SVG**, not Chart.js: no third-party script,
  no SRI pinning, nothing to load. A deviation from the plan, in the
  plan's own direction (fewer moving parts).
- **Server-side table sorting** via `?sort=&dir=`, so no JavaScript at all
  in this PR.
- **No icons, emoji or symbols** in the console; `tests/test_console.py`
  scans every rendered page for emoji/symbol code points.

## Data touched

n/a (reads the app's own MLflow traces).

## Verification

- `pytest -q` → 194 passed (146 before); `ruff check app tests` → clean;
  `databricks bundle validate -t dev` → OK.
- Prompt hashes identical before and after the split.

## Deployment

Deployed to dev (`bundle deploy` + `bundle run`). Live check as the admin
(OAuth token, so Apps sends the real `X-Forwarded-Email`): all six pages
200 with role Admin and no trace-read errors; a fixture review recorded
6,056 tokens on `databricks-claude-sonnet-5-5`.

Found live and fixed (`97b71e9`): traces from before outcome tagging were
counted as refused applies (0 of 14 applied). They now read "Outcome not
recorded" and are excluded from the pass rate, and their repository is
derived from `pr.slug`, so `github/KabeloPhiri__pr-review-agent` appears on
the Repositories page. Databricks Apps rejects an empty env value, so
`PRREVIEW_CONSOLE_VIEWERS` is omitted until there are viewers.

## Follow-ups

- PR 2 (Control): `VolumeStore` in `main.pr_review.console`, console
  config layer, editable model/config/standards/prompts with history and
  rollback, per-repository settings.
- PR 3 (Quality): `/fp` feedback and the False positives page.
- Token figures cover only reviews run after this deploys; earlier traces
  have no usage spans.
