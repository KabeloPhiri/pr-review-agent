---
title: Harden the /apply accept-a-suggestion path before deploy
type: fix
agent: claude-code
date: 2026-10-06
branch: feature/apply-suggested-fix
status: implemented
targets: []
relatedTables: []
---

## Context

A pre-deploy code review (`master...HEAD`, high effort) of
`feature/apply-suggested-fix` found seven defects in `POST /apply`, the
GitHub-only path that pushes a bot suggestion as a commit when the PR author
replies `/apply`. Several let it push code nobody approved or silently revert
the author's work. All seven were fixed in commit `2ea5651`.

## What changed

- `app/core/pipeline.py` — `apply()` resolves config from the PR's **target**
  commit; checks comment author against `apply_trusted_authors`; refuses fork
  PRs; skips comments already carrying `prreview:applied:<comment_id>`;
  maps a push conflict to `reason=file_changed_since_review`; post-push
  comment/resolve failures become `ApplyResult.warnings`.
- `app/services/scm/github.py` — `update_file` sends the blob sha at
  `pr.source_commit` (was the branch tip, so the 409 check never fired); 409
  raises `ScmConflictError`; `get_pull_request` sets `is_fork`;
  `get_comment` returns `None` for a comment from another PR.
- `app/core/errors.py` — `ScmConflictError` (409).
- `app/core/models.py` — `PullRequest.is_fork`, `ApplyResult.warnings`.
- `app/core/config.py`, `app/defaults/config.yaml` — `apply_trusted_authors`
  (default `["github-actions[bot]"]`).
- `app/api/routes.py` — `GET /apply/{job_id}`; shared `_poll` returns 404
  (not a 500 validation error) when a job of the other kind is polled.
- `pipelines/github/pr-apply.yml` — pushes with `PR_REVIEW_APPLY_TOKEN`
  (pushes made with `GITHUB_TOKEN` do not trigger workflows, so CI never ran on
  the fix); `mode: auto` plus polling instead of `sync` (~120s app cutoff);
  `contents: read` on the job token.
- `README.md`, `CLAUDE.md` — trust model, target-branch opt-in, token setup,
  and the planned GitHub App token switch.
- Tests: `tests/test_apply.py` (7 new, via a test-registered `apply-fake`
  connector), `tests/test_github.py` (4 new), `tests/test_api.py` (1 new);
  `tests/fixtures/apply-pr/comments.json` author → `github-actions[bot]`.

## Design decisions

- **Opt-in read from the target branch, not server-only.** Keeps the
  documented per-repo opt-in, but only maintainers (who merge to the target)
  can set it. Request-body `config` is still honoured: the caller already
  holds the app's OAuth token, and a same-repo PR author can push to the
  branch anyway.
- **Trusted-author allow-list rather than "the token's own identity".**
  `GITHUB_TOKEN` cannot call `GET /user`, so the identity cannot be looked up
  reliably; a configurable list is explicit and works for PAT-posting setups.
- **Forks refused, not supported.** Pushing to a fork needs maintainer-edit
  permission and a token with access to the fork; out of scope.
- **Idempotency via a marker in the confirmation comment.** Consistent with
  "the PR is the state store". Gap: if the push lands but the confirmation
  fails to post, a retry is not protected — the result reports `applied=true`
  with a warning, so nothing prompts a retry.

## Data touched

n/a — no tables read or written.

## Verification

- `python -m pytest -q` → 121 passed (was 109).
- With the logic files (`pipeline.py`, `github.py`, `routes.py`) reverted,
  all 12 new tests fail; restored, all pass.
- `ruff check app tests` → all checks passed.
- Secret scan: `tools/Find-Secrets.ps1` does not exist in this repo; the diff
  was grepped for token/key patterns instead — no findings.
- `bash -n` on the extracted `pr-apply.yml` run script → OK; YAML parses.
- `databricks bundle validate -t dev` → `Validation OK!` (run after the
  commit, with `DATABRICKS_AUTH_STORAGE=plaintext` — the container has no OS
  keyring, so the default secure token storage fails).

## Deployment

Not deployed. Commits `2ea5651` (fix) and `206e394` (this note) are local on
`feature/apply-suggested-fix`, not yet pushed.

## Follow-ups

- Add the `PR_REVIEW_APPLY_TOKEN` repo secret (fine-grained PAT, Contents +
  Pull requests read & write, preferably a machine user) in each repo that
  uses `pr-apply.yml`; see README "The apply token".
- If `pr-review.yml` posts with a PAT instead of `GITHUB_TOKEN`, add that login
  to `apply_trusted_authors`.
- Make the reviewed repo's test job a required status check so a failing
  applied fix cannot merge.
- Planned: switch to a GitHub App token (`actions/create-github-app-token`);
  steps in the README.
- The dev container needs `DATABRICKS_AUTH_STORAGE=plaintext` for every
  Databricks CLI call (no D-Bus/keyring); consider setting it in
  `.devcontainer/devcontainer.json` `containerEnv`.

## Update 2026-10-06

Enabled `/apply` on this repository itself:

- `.github/workflows/pr-apply.yml` — copy of `pipelines/github/pr-apply.yml`
  (identical content).
- `.prreview/config.yaml` — `allow_apply_fixes: true`, `applier: llm`.
  Resolves cleanly against `EffectiveConfig`. Read from the target branch for
  `/apply`, so it applies only to PRs opened after this merges to `master`.
- Reviews here are posted by `pr-pipeline.yml` with `GITHUB_TOKEN`
  (`github-actions[bot]`), matching the default `apply_trusted_authors`.

Manual step still required: add the `PR_REVIEW_APPLY_TOKEN` Actions secret.
