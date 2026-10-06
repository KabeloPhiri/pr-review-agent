---
name: cicd-devops
description: Branching, pull-request and CI/CD rules for this repo and for Databricks Asset Bundle projects - feature/bugfix branches, no force merges, validate-test-scan-deploy pipelines, pinned dependencies. Use before creating a branch, committing, opening a PR, or touching any deployment workflow.
---

# CI/CD & DevOps rules

CLAUDE.md > Dev ops: new feature -> new branch; bug or CBT -> new branch; **no force merge**; CI/CD is mandatory in every design.

## Branching

| Work | Branch name | Base |
|---|---|---|
| Feature | `feature/<short-kebab>` | `main` |
| Bug | `bugfix/<ticket-or-short>` | `main` (or the release branch it affects) |
| CBT / change request | `cbt/<ticket>` | `main` |
| Hotfix to prod | `hotfix/<ticket>` | the deployed tag; PR back to `main` |

Procedure before any edit that will be committed:
```bash
git status --short            # must be clean or only the intended files
git switch -c feature/<name>  # never commit on main
```
Commit only when the user asks. Commit messages: imperative, <= 72-char subject, body explains *why*; end with the attribution line given in the session.

## Pull requests

- One concern per PR; include: what/why, test evidence (pytest output, `bundle validate` output), secret-scan result, and screenshots/links for job runs in dev.
- Required checks (CI): `Find-Secrets.ps1 -FailOnFindings`, unit tests, `databricks bundle validate -t dev`, lint if configured.
- Merge strategy: merge commit or squash via the PR UI only. **Never** `git push --force` to shared branches, never `git merge` locally into `main` and push, never bypass required checks or branch protection.
- Use `gh pr create` (see the `pr-description` skill for the body) and let reviewers merge.

## Pipeline stages (any CI system)

1. **validate** - checkout, pinned Python, `pip install -r requirements.txt`, secret scan, pytest, `bundle validate`.
2. **deploy-dev** - on merge to `main`; `bundle deploy -t dev`; optional `bundle run` smoke job.
3. **deploy-uat** - on tag `v*`; separate service principal and secrets.
4. **deploy-prod** - after uat, behind an approval gate (GitHub environment reviewers / ADO approvals).
Reference implementation: `templates/dab/.github/workflows/dab-deploy.yml`. Credentials are pipeline secrets, hosts pipeline variables, both injected as env vars/`--var` - never committed.

## Dependency pinning

- `requirements.txt`: exact `==` pins, one bump per PR with changelog link.
- Init scripts (`init_scripts/*.sh`): `pip install "pkg==x.y.z"`; live in the bundle repo and are referenced from the job cluster YAML.
- `spark_version` pinned to an LTS runtime; upgrades are their own PR with a dev run attached.

## Local hygiene checklist (run before pushing)

```powershell
powershell -File tools/Find-Secrets.ps1 -Path . -FailOnFindings -Quiet
pytest -q
databricks bundle validate -t dev
```

## Things an agent must not do

- Deploy to uat/prod from a local session (CI only).
- Rewrite history on shared branches, delete branches it did not create, or edit branch-protection settings.
- Add `.bak`, reports, `.databrickscfg`, `.env`, or `dist/` to commits - keep/extend `.gitignore` instead.
