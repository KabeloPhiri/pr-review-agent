---
title: Switch the reviewer model to Claude Sonnet 5.5
type: fix
agent: claude-code
date: 2026-10-06
branch: feature/claude-sonnet-reviewer
status: deployed-dev
targets: [dev]
relatedTables: []
---

## Context

Testing `/apply` on PR #10 showed `databricks-llama-4-maverick` producing
false positives (it re-flagged the already-fixed mutable default as an
error) and renaming `rule_id`s between runs, which posts duplicate comments
(see CLAUDE.md, marker caveat). PR #9's review had the same false-positive
pattern. Asked to move to a model that reviews better.

## What changed

- `app.yaml`, `databricks.yml` (env + the `review-model` CAN_QUERY resource),
  `app/defaults/config.yaml`, `app/core/config.py`, `README.md` —
  `databricks-llama-4-maverick` → `databricks-claude-sonnet-5-5`.
- `app/core/llm_client.py` — `complete()` now drops whichever optional
  parameter the endpoint rejects: `temperature` only when the error names it,
  `response_format` on any other error (the previous behaviour).
- `tests/test_llm_client.py` — 4 tests for the fallback.

## Design decisions

- **Sonnet over Opus:** a review is one call per chunk (66 on PR #9), so
  cost and latency per call matter; Sonnet 5.5 is the strongest model at that
  price point among the workspace's ready endpoints. Not benchmarked beyond
  the fixture comparison below.
- **Temperature is no longer 0 on this model.** Sonnet 5.5 on Databricks
  rejects both `response_format` ("structured output … requires forced tool
  use, which newer Anthropic models reject") and `temperature`. Without
  temperature 0, `rule_id` renames between runs — the duplicate-comment
  caveat in CLAUDE.md — may become more likely, not less. The fallback keeps
  temperature for every endpoint that accepts it.

## Data touched

n/a

## Verification

- `pytest -q` → 125 passed; `ruff check app tests` → clean;
  `databricks bundle validate -t dev` → OK.
- Before the fallback fix, every Sonnet call failed (400 on `temperature`)
  and the review correctly raised instead of passing.
- Same fixture (`tests/fixtures/sample-pr`, `publish: false`) on the deployed
  app: Llama — 10 findings, 5s, 3 low-value infos; Sonnet — 9 findings, 10s,
  adds duplicated table read, null-unsafe `sum`, missing three-level names,
  and cites the repo standards in each message.
- A direct probe of which Claude endpoints accept `temperature=0` was not run
  (blocked by the session's permission policy).

## Deployment

`databricks bundle deploy -t dev` + `bundle run pr_review_agent -t dev`; app
running on Sonnet 5.5. Not yet pushed or PR'd.

## Follow-ups

- Watch the next few real PR reviews for duplicate comments from `rule_id`
  renames; if they appear, consider hashing markers without `rule_id`, or an
  older Claude endpoint that accepts `temperature=0`.
- `tests/fixtures/sample-pr` pins `model_endpoint: databricks-gpt-5-2`, which
  does not exist in this workspace — override it when smoke-testing.
