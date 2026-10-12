---
title: PR workspace - browse, apply, alignment check, scan-to-phone handoff
type: feature
agent: claude-code
date: 2026-10-11
branch: demo/ai_reviewer
status: implemented
targets: []
relatedTables: []
---

## Context

Asked for: an easy UI where a user can see a pull request, the bot's and other
people's comments, use `/apply` and `/apply all`, and optionally confirm that the
PR matches its requirement (requirement change, change/business ticket (CR),
feature, bug fix). Design per the `apple-design` skill. Added during planning:
a scan-QR route that opens the same PR in a mobile UI.

Decisions taken with the user: the user's own GitHub PAT per browser tab (never
stored); requirement pasted in the UI; "CBT" = change/business ticket; a new
`/workspace` package (the `/console` no-script rule stays untouched); QR
handoff by one-time code, not by putting the token in the QR.

## What changed

- `app/workspace/` (new): `routes.py` (pages + JSON API under `/workspace/api`),
  `auth.py` (signed-in users, optional `PRREVIEW_WORKSPACE_USERS`, dev email
  only off-Databricks), `handoff.py` (`HandoffStore`, 120s single-use codes),
  `templates/` (index, pr, mobile_pr, redeem, forbidden), `static/workspace.js`
  + `workspace.css` (springs, drag-to-dismiss sheets, swipe cards, a11y fallbacks).
- `app/services/alignment/` (new plugin family): `base.py`, `llm_checker.py`,
  `prompts.py` (`alignment_guidance`, console-editable).
- `app/core/pipeline.py`: `snapshot()` (read view using the apply gates) and
  `check_alignment()`; `_has_marker` shared helper.
- `app/core/models.py`: `ChangeIntent`, `AlignmentResult` (+ criterion types),
  `CommentView`, `PullRequestSnapshot`.
- `app/api/routes.py`, `schemas.py`: `POST /alignment`, `GET /alignment/{job_id}`;
  `_run_apply` generalised to `_run_job`.
- `app/services/scm/`: optional `whoami()` and `list_pull_requests()` (GitHub, fake).
- `app/services/publish/`: `render_alignment()`; publisher never resolves
  `prreview:alignment:` comments.
- `app/core/origin.py`: cross-site check shared by console and workspace.
- Config: `alignment_checker`, `alignment_max_chars`; `segno` dependency (QR SVG,
  server-side); ruff excludes `tests/fixtures`; `databricks.yml` comment for
  `PRREVIEW_WORKSPACE_USERS`; `CLAUDE.md` sections for both features.
- Tests: `test_workspace.py`, `test_handoff.py`, `test_alignment.py`, GitHub
  `whoami`/list tests; fixture `tests/fixtures/workspace-pr/`.

## Design decisions

- Apply requester = `whoami()` of the caller's token, never a client value, so
  the existing author-only gate is verified. A body naming `requester` is a 400.
- Snapshot uses target-branch config, so the UI's disabled-Apply reason always
  matches what `/apply` would refuse.
- Alignment failure policy mirrors `_analyze`: empty or unparseable model reply
  raises; never reported as `aligned`. Trace records requirement length only.
- Handoff codes stored by SHA-256, bound to the creator's email, burned on any
  redeem attempt; redeem is a tap-to-POST page so link scanners can't spend it.
  Per-process like `InMemoryJobStore` (documented limitation).
- All DOM built with `textContent` (comment bodies are untrusted); strict CSP.

## Data touched

n/a - no tables. Reads PR metadata/diff/comments via the SCM connector.

## Verification

- `pytest -q`: 372 passed (297 before; 75 new). `ruff check app tests`: clean.
- Browser walk-through in Edge (Playwright, scratch harness with the fake SCM
  and canned demo applier/alignment checker; no network, no model): desktop
  light/dark, 390px phone, reduced motion; connect, list, apply, apply result,
  alignment form/result/evidence reveal, QR sheet, phone redeem, swipe-to-apply
  confirm/cancel, files and conversation tabs. Bugs found and fixed during it:
  empty-store falsiness replaced the injected `HandoffStore`; `null` rendered
  as text by `replaceChildren`; swiped card widened the phone layout viewport
  (sheet off-screen); code-block indentation trimmed; result colour specificity.
- Not run: a real GitHub PR end to end with a real PAT, and a real phone scan
  against a deployed app (localhost isn't reachable from a phone).

## Deployment

Not deployed. Ships with the next `databricks bundle deploy` + `bundle run`.

## Follow-ups

- Try on a real GitHub PR with a fine-grained PAT (Pull requests RW, Contents RW,
  Commit statuses R) and a real phone scan on the deployed app.
- Dismiss from the workspace is PR-author only (no `author_association`
  lookup); reviewers still use `/fp` on GitHub.
- Azure DevOps: viewing works by number, but listing/whoami/apply are GitHub-only.
- `mlflow.db` (tracked) is modified by local test runs; not part of this change.
