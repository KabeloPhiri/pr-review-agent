# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                  # install (Python >= 3.11)
uv run start-server                      # local server on :8000
uv run quickstart --profile <profile>    # MLflow experiment + .env + databricks.yml
pytest                                   # whole suite; no network, no credentials
pytest tests/test_azure_devops.py -v     # one file
databricks bundle deploy --target dev
databricks bundle run pr_review_agent --target dev   # deploy alone does NOT restart the app
```

There is no lint gate; `[tool.ruff]` in `pyproject.toml` sets the house style
(100 columns, py311).

## Architecture

A Databricks App that reviews pull requests. One FastAPI process, built as a
modular monolith: four plugin families behind ABCs, each with a `Registry` in
`app/core/registry.py`, resolved by name from configuration and imported
lazily via the `_MODULES` map in each package's `__init__.py`. Nothing outside
a plugin's own package imports it.

```
app/server.py          composition root: AgentServer() -> app, routes, lifespan
app/agent.py           @invoke() handler behind POST /invocations
app/api/               routes, request/response schemas, SCM-token extraction
app/core/pipeline.py   the ONLY place that knows the order of operations
app/core/{config,models,jobs,registry,errors}.py
app/services/scm/      ScmConnector: azure_devops, fake (+ shared diffparse)
app/services/quality/  QualityConnector: noop (SonarQube goes here)
app/services/review/   Reviewer: llm, plus chunking and prompt assembly
app/services/policy/   standards loading + the severity gate
app/services/publish/  comment rendering and idempotent posting
app/defaults/          config.yaml and the bundled standards markdown
```

Two facts explain most of the design:

1. **Databricks Apps accept only OAuth bearer tokens inbound and cut
   connections at ~120s.** Hence the async job path, and hence the *outbound*
   Azure DevOps PAT arriving per request in `X-SCM-Token` (never stored, never
   logged, held in memory for the life of a background job).
2. **The pull request is the state store.** Comments carry a hidden
   `<!-- prreview:<hash> -->` marker derived from file + line + rule, so
   re-runs diff against what is already posted. There is no database.

### Things that will bite you

- `app` in `app/server.py` must stay a module-level name — uvicorn starts by
  import string (`app.server:app`) to allow multiple workers.
- `import app.agent` must happen before `AgentServer()` is constructed;
  importing it is what registers the `@invoke()` handler.
- `load_dotenv()` runs before any import that builds a Databricks client.
- Diff line numbers are **new-file** numbers throughout. `Hunk.new_start` plus
  the `+`/` ` prefixes is the only source of truth for where a comment lands;
  `ReviewChunk.render()` prints them for the model, and
  `_snap_to_changed_line` in `llm_reviewer.py` refuses to anchor a finding to
  a line the pull request did not touch.
- Azure DevOps returns changed *items*, not a unified diff. The connector
  fetches both blobs and builds one with `difflib`, then parses it with the
  shared parser — so every connector produces identical `Hunk`s.
- Config keys are validated against `EffectiveConfig`; an unknown key in a
  repo's `.prreview/config.yaml` is a 400 with the valid list, not a silent
  ignore.

### Adding a plugin

Subclass the ABC, decorate with `@<family>_registry.register("name")`, add the
module to `_MODULES` in that package's `__init__.py`. It becomes selectable as
`scm:` / `quality:` / `reviewer:` in config, and shows up in
`POST /config/effective`. Adding an analyzer is just a markdown file in
`app/defaults/standards/` plus an entry in `_BUNDLED` in
`app/services/policy/standards.py`.

## Testing

Tests run offline. `app/services/scm/fake.py` replays the fixtures in
`tests/fixtures/sample-pr/` (pr.json, diff.patch, `files/` for repo config and
standards), and `tests/test_azure_devops.py` drives the real connector against
an `httpx.MockTransport` — that file is where the REST 7.1 request shapes are
pinned, so change it deliberately.

`tests/test_pipeline.py` registers a `stub` reviewer to exercise the whole
pipeline without a model. Use the same trick rather than mocking internals.
