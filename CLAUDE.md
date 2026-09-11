# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                  # install (Python >= 3.11)
uv run start-server                      # local server on :8000
uv run quickstart --profile <profile>    # MLflow experiment + .env + databricks.yml
pytest                                   # whole suite; no network, no credentials
pytest tests/test_azure_devops.py -v     # one file
pytest -k publish_is_idempotent          # one test
databricks bundle deploy --target dev
databricks bundle run pr_review_agent --target dev   # deploy alone does NOT restart the app
```

There is no lint gate; `[tool.ruff]` in `pyproject.toml` sets the house style
(100 columns, py311, `select = ["I", "B", "F401", "E722", "F403"]`).

Review a fixture pull request with no Azure DevOps access at all — this is the
fastest way to exercise a change end to end:

```bash
curl -X POST localhost:8000/review -H 'Content-Type: application/json' -d '{
  "scm": "fake", "repository": "platform", "pull_request_id": "42", "mode": "sync",
  "extra": {"fixture_dir": "tests/fixtures/sample-pr"}
}'
```

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
app/services/scm/      ScmConnector: github, azure_devops, fake (+ diffparse)
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

### Configuration is four layers, and it is the whole control surface

`app/core/config.py::resolve()` merges, lowest precedence first:
`app/defaults/config.yaml` → `PRREVIEW_<FIELD>` env vars (set in
`databricks.yml`) → `.prreview/config.yaml` read from the PR's **source
branch** → the `config` object in the request body. Everything the reviewer
does is driven by the resulting `EffectiveConfig`, so `POST /config/effective`
explains a misbehaving repo without reading server logs, and tests override
behaviour by passing `overrides=` to the pipeline rather than by patching.

### Request modes and job state

`sync` blocks; `async` returns `202 {job_id}`; `auto` (the default) waits
`sync_wait_seconds` (90) and downgrades to a job id — one execution either
way. `InMemoryJobStore` is per-process: `GET /review/{job_id}` can legitimately
404 after a restart or on another replica while the review is still running.
Comments and the PR status land regardless — publishing does not go through
the job store. A shared store is a new class implementing the `JobStore`
protocol in `app/core/jobs.py`.

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
  a line the pull request did not touch (>2 lines away becomes `line=None`,
  which the summary comment carries instead of an inline one).
- Azure DevOps returns changed *items*, not a unified diff. The connector
  fetches both blobs and builds one with `difflib`; GitHub returns a per-file
  `patch` that only needs a `diff --git` header re-attached. Both then go
  through the shared parser, so every connector produces identical `Hunk`s.
- Model replies are not always a string. Reasoning endpoints (the `gpt-oss`
  family) return `content` as a list of parts and only the `text` part is the
  answer — `_message_text` in `llm_reviewer.py` normalises this. Feeding the
  reasoning part to the JSON parser breaks every review against those models.
- GitHub's REST API cannot resolve a review thread; `close_comment` minimises
  it through GraphQL instead. REST also cannot report whether a thread is
  resolved, so `is_closed` falls back to "GitHub unset the line anchor".
- Config keys are validated against `EffectiveConfig`; an unknown key in a
  repo's `.prreview/config.yaml` is a 400 with the valid list, not a silent
  ignore.
- Reviewable languages are derived from the standards on disk, not a constant:
  `catalog.reviewable_languages()` is the union of every `languages:` declared
  in `app/defaults/standards/*.md`. Everything else is skipped with a reason
  that surfaces in the summary comment — a "why didn't it review my file" bug
  usually ends in `_skip_reason`.
- Front matter is routing metadata and is stripped before the text reaches the
  prompt. A malformed block (or a bad `content_match` regex) is logged and the
  standard degrades to "applies everywhere" rather than taking reviews down.

### Failure policy — deliberate, and asymmetric

A review that reached nothing must never report a clean pass: when
`context.failed_chunks >= len(chunks)`, `pipeline._analyze` raises
`ReviewerError` rather than returning zero findings, so a model outage cannot
green-light every pull request. `tests/test_pipeline.py` guards this — do not
"fix" it into a soft pass. An unparseable reply counts toward `failed_chunks`
for the same reason: a reply truncated mid-JSON is indistinguishable from no
review, so a wholly garbled run must fail rather than report a clean pass. An
*empty* reply is not a failure — that is the model saying the code is fine.
Everything softer degrades instead: a quality connector that throws, or a
comment that fails to post, becomes a warning on `ReviewResult`.

One caveat the marker scheme does not cover: `marker_for()` hashes
`file|line|rule_id`, and `rule_id` is invented by the model. At temperature 0
it is *mostly* stable, but a rename between runs (`python.typing.x` ->
`python.correctness.x`) reads as a brand-new finding and posts a second
comment on the same line. Wording changes are already immune; rule renames are
not.

`PrReviewError` subclasses in `app/core/errors.py` carry their own
`status_code` (`ConfigError` 400, `ScmAuthError` 401, `ScmError`/`ReviewerError`
502); one handler in `server.py` turns them into responses. Raise these rather
than `HTTPException`.

### Adding a plugin

Subclass the ABC, decorate with `@<family>_registry.register("name")`, add the
module to `_MODULES` in that package's `__init__.py`. It becomes selectable as
`scm:` / `quality:` / `reviewer:` in config, and shows up in
`POST /config/effective`.

Adding an analyzer is **only** a markdown file in `app/defaults/standards/` —
there is no registry to update. `standards.discover()` globs the directory and
each file declares its own scope in YAML front matter:

```markdown
---
applies_to:
  languages: [python]              # what makes a file type reviewable at all
  path_globs: ['*spark*']          # optional narrowing, OR-ed with content_match
  content_match: 'pyspark'     # optional regex over the changed lines
---
```

Omit `applies_to` and the standard is appended to every review (that is how
`naming.md` works). `languages` is load-bearing: `_skip_reason` skips any file
whose language no standard claims, so a `terraform.md` declaring
`languages: [terraform]` is the whole change needed to start reviewing `.tf`.
Standards with narrowing sort after general ones so the model reads the
specific rules last. `analyzers: []` in config means "everything discovered";
set an explicit list to narrow.

## Testing

Tests run offline. `app/services/scm/fake.py` replays the fixtures in
`tests/fixtures/sample-pr/` (pr.json, diff.patch, `files/` for repo config and
standards), and `tests/test_azure_devops.py` drives the real connector against
an `httpx.MockTransport` — that file is where the REST 7.1 request shapes are
pinned, so change it deliberately.

`tests/test_pipeline.py` registers a `stub` reviewer to exercise the whole
pipeline without a model. Use the same trick rather than mocking internals.
