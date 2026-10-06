---
title: Several /apply replies per PR, and /apply all
type: feature
agent: claude-code
date: 2026-10-06
branch: feature/apply-many
status: deployed-dev
targets: [dev]
relatedTables: []
---

## Context

Several `/apply` replies on one file raced: the first push won and the rest
were refused (409 → `file_changed_since_review`) with no message on the PR.
PR #12 also showed fixes to CRLF files rewriting every line. Asked to fix
that (retry, reply on refusal, line endings), test it, then add `/apply all`.

## What changed

- `app/core/pipeline.py` — `_push_fix` retries a 409 up to
  `APPLY_MAX_ATTEMPTS` (3), re-reading the PR and regenerating; replies in
  the suggestion's thread on verified refusals and failures
  (`_reply_quietly`); `_match_line_endings` restores newline style and the
  final newline. New `apply_all()`; gates shared via `_apply_refusal`,
  `_bot_finding`, `_already_applied`.
- `app/services/scm/{base,github,fake}.py` — `reply_to_comment` (GitHub:
  `POST …/pulls/{n}/comments/{id}/replies`); `list_comments` now carries
  `body`, `author`, `comment_id`.
- `app/services/apply/{base,llm_applier,prompts}.py` —
  `generate_patch_many` (one model call per file in `llm`; the base chains
  `generate_patch`); the prompt now lists each finding with its line.
- `app/services/publish/publisher.py` — never auto-resolves
  `prreview:applied:` / `prreview:reply:` comments (it was collapsing the
  bot's own "✅ Applied" after every re-review).
- `app/api/` — `POST /apply/all`; `GET /apply/{job_id}` polls both kinds;
  shared `_run_apply`.
- `.github/workflows/pr-apply.yml`, `pipelines/github/pr-apply.yml` —
  `issue_comment` trigger for `/apply all` on the PR; `/apply all` also
  accepted as a thread reply.
- Tests: `tests/test_apply_all.py` + `tests/fixtures/apply-all-pr/` (9),
  `tests/test_apply.py` (+10), `test_github.py`, `test_review_and_publish.py`,
  `test_api.py` (+1 each).

## Design decisions

- **Retry in the app, not `concurrency:` in the workflow.** GitHub keeps one
  pending run per group and cancels earlier pending ones, so 3+ quick
  replies would silently drop the middle ones.
- **Replies only after the requester and comment are verified**, so a
  stranger typing `/apply` cannot make the bot post.
- **`/apply all`: one model call and one commit per file**, so fixes in one
  file cannot conflict. A failing file is skipped and reported; the request
  raises only when nothing landed because of a real failure.
- One summary comment embeds a `prreview:applied:<id>` marker per
  suggestion; `_already_applied` checks bodies as well as markers.

## Data touched

n/a

## Verification

- `pytest -q` → 146 passed (was 125); ruff clean; `bundle validate` OK.
  The 11 new tests for part 1 fail against the previous app code.
- PR #13 (live): three simultaneous `/apply` on one CRLF file → 3 stacked
  commits (+2/−1, +1/−1, +2/−1), CRLF kept on all lines, each thread
  confirmed, re-review passed. **The retry path did not trigger live** (the
  runs serialised; no 409 in the app logs) — it is covered by unit tests.
- PR #14 (live): `/apply all` replied in a thread → 10 suggestions, 2 commits
  (one per file), CRLF and LF files each kept their endings, all four
  planted bugs fixed correctly, re-review "No findings". PR #14 targeted
  `feature/apply-many` (with a test-only branch filter edit) so its diff was
  just the two test files.

## Deployment

`bundle deploy -t dev` + `bundle run pr_review_agent -t dev`. Branch pushed
to `KabeloPhiri/pr-review-agent`; not yet PR'd.

## Follow-ups

- The `/apply all` summary lists every suggestion in a committed file as
  applied, even one the model could not act on (PR #14's
  `tests.missing-coverage` added no test). Per-finding confirmation would
  need the model to report which findings it addressed.
- Duplicate findings from `rule_id` renames appeared on PR #14 (two
  overlapping review runs, `python.bare-except` vs `python.no-bare-except`);
  expected since Sonnet runs without temperature 0. Candidate fix: drop
  `rule_id` from the marker hash.
- PR-level `/apply all` (the `issue_comment` trigger) is untested live; it
  needs this workflow on `master` first.
- Close throwaway PRs #12, #13, #14.
