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

- PR 2 (Control): `VolumeStore` in a UC volume, console
  config layer, editable model/config/standards/prompts with history and
  rollback, per-repository settings.
- PR 3 (Quality): `/fp` feedback and the False positives page.
- Token figures cover only reviews run after this deploys; earlier traces
  have no usage spans.

## Update 2026-10-06 — PR 2 of 3, Control (`feature/admin-console-control`)

- **Store:** `app/core/settings_store.py` gains `FileStore` (history,
  audit, path allowlist, 30s cache, degrade-on-failure), `LocalDirStore`
  and `VolumeStore` (Files API).
- **Config:** `resolve()` adds the console layers (global, then
  per-repository) above repo and request, plus internal `pinned`;
  `resolve_with_sources` reports each key's layer, also returned by
  `POST /config/effective` as `sources`. Unknown console keys are dropped
  at review time.
- **Reviews:** standards come from the console when set, repository
  standards are appended, prompt guidance from the console; a store outage
  adds a warning to `ReviewResult`.
- **Console:** `context.py` (shared access/rendering), `editing.py` with
  Model (allowed models with state, test on the sample PR, prices), Config,
  Standards (import bundled, edit, per-repository), Prompts, History
  (side-by-side compare, restore), Audit log. Global changes, deletes and
  rollbacks need a confirmation; edits are admins only and refused when
  `Sec-Fetch-Site`/`Origin` show another site.
- **Bundle:** schema + volume, `uc_securable` `WRITE_VOLUME` for the app,
  `PRREVIEW_SETTINGS_*`, `PRREVIEW_ALLOWED_MODELS` with a `CAN_QUERY`
  resource per model.

**Catalog change:** the plan said `main.pr_review`. Deploy failed with
`User does not have CREATE SCHEMA on Catalog 'main'` (only `USE_CATALOG`);
on the user's choice it moved to `dia_ai_agent_solution`, where they have
`ALL_PRIVILEGES`. Dev: `dia_ai_agent_solution.dev_k_phiri_pr_review.console`.

**Plan deviations:** no HTMX/Chart.js (forms post normally; CSRF is checked
with `Sec-Fetch-Site`/`Origin` instead of an `HX-Request` header); form
bodies are parsed with `parse_qsl` rather than adding `python-multipart`.

**Verification:**
- `pytest -q` → 257 passed (196 before); ruff clean; `bundle validate` OK.
- Dev: schema, volume and grant created; all settings pages 200 with the
  volume readable; all six allowed models Ready.
- Set `databricks-claude-haiku-4-5` for `fake/platform` only through the
  console: its review used Haiku (4,471 tokens) — overriding even the
  fixture repo's own pin — while `fake/platform-two` used Sonnet 5.5.
- In-console model test, Opus 5.5 on the sample PR: 16.7s, 6,612 tokens,
  10 findings, nothing posted.
- Audit log recorded both changes with the admin's email; the test setting
  was then removed.

## Update 2026-10-06 — PR 3 of 3, Quality (`feature/admin-console-quality`)

- `/fp [reason]` → `pr-apply.yml` → `POST /feedback` →
  `ReviewPipeline.feedback()`: a `pr_feedback` trace tagged with rule,
  severity, file, model and reason; a reply in the thread; the thread
  resolved. PR author or GitHub OWNER/MEMBER/COLLABORATOR only; others get
  no reply; a second flag on the same comment is `already_recorded`.
- `Finding.model` (set by `LlmReviewer`) is rendered as a second hidden
  marker, `<!-- prreview:model:<endpoint> -->`, after the finding's marker
  so first-marker lookups are unchanged; `parse_finding_comment` returns it.
- Reviews tag `prreview.rules` (findings per rule) and suspected duplicates
  (`Publisher.duplicates`: new comment on a line with an open comment under
  a different rule id); applies tag the accepted rule(s).
- Console: False positives page — rates (flags ÷ posted) by rule, rule
  family (linked to the standard), model and repository; `/apply`
  acceptances; recent flags with reasons; suspected duplicates.

**Verification:** `pytest -q` → 279 passed (257 before); ruff clean.
Deployed to dev. Live on throwaway PR #18 (targeting the part 3 branch with
a test-only review trigger): both bot comments carried the model marker;
`/fp` with a reason got the bot's reply and the thread was resolved; the
console then showed 2 posted, 1 flagged, 50% overall,
`python.missing-type-hint` 100%, and the flag with reason, file, model and
who. PR #18 closed and its branch deleted.

**Limitations:** flags count only comments posted since this deploy (older
comments carry no model and older reviews no per-rule counts, so those show
"unknown"/are not in the denominator). Rule families are the first segment
of the model-invented rule id, which usually but not always names a
standard.
