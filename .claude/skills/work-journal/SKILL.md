---
name: work-journal
description: Mandatory write-up of every feature, fix, design decision or deployment an agent completes - one markdown note under <project_root>/docs/ plus an index line. Use at the end of any task that changed code, configuration, a data model, or the state of a Databricks workspace.
---

# Work journal

Every agent finishes its work by writing a markdown note. The note is part of the deliverable, not an optional extra: a task is not complete until the note exists and the index line points at it.

## Where the note goes

```
<project_root>/docs/
├── INDEX.md              # one line per note, newest first
├── features/             # new capability: pipeline, job, table, tool, app
├── fixes/                # bug, defect, CBT, incident remediation
├── design/               # data model, partitioning, architecture, review findings
└── deployments/          # bundle deploys, CI/CD changes, infra/workspace changes
```

`<project_root>` is the repo root of the code you touched (the folder holding `databricks.yml` or `.git`), **not** the toolbox root, unless you changed the toolbox itself. Create the folder you need if it is missing; never create the other three pre-emptively.

File name: `YYYY-MM-DD-<kebab-slug>.md` - date the work was done, slug from the deliverable (`2026-09-22-bronze-bank-statement-autoloader.md`). If a note for the same work already exists, **append a new `## Update YYYY-MM-DD` section to it** rather than creating a second file.

## Front matter (required on every note)

```yaml
---
title: <one line, what was delivered>
type: feature | fix | design | deployment
agent: <your agent name>
date: YYYY-MM-DD
branch: <git branch the work sits on>
status: proposed | implemented | deployed-dev | merged
targets: [dev]            # environments actually touched; [] for design/review only
relatedTables: [catalog.schema.table, ...]
---
```

## Body sections

Keep every section, write "n/a" where it does not apply. Be concrete and short - file paths, table names, commands, numbers.

| Section | Content |
|---|---|
| `## Context` | What was asked and why. Link the ticket/branch/PR if there is one. |
| `## What changed` | Bullet list of files added/modified, with one-line purpose each. Resources (`resources/*.yml`), tables, and jobs named explicitly. |
| `## Design decisions` | Grain, keys, SCD2 columns, partitioning/clustering, streaming trigger, retry policy - and the **evidence** behind each (row counts, cardinality, skew from the data sample). Say when a decision was a default, not evidence-backed. |
| `## Data touched` | Tables read and written, layer (bronze/silver/gold), PII columns and how they were masked. Never paste sample values. |
| `## Verification` | Commands actually run and their outcome: `pytest -q`, `databricks bundle validate -t dev`, secret scan result, row-count checks. Failures and skips stated plainly. |
| `## Deployment` | Target(s), bundle/job/pipeline name, deploy command, run id, and what is CI-only. "Not deployed" is a valid entry. |
| `## Follow-ups` | What was left undone, manual steps for the user (secret scopes, warehouse ids, CI variables), and known risks. |

## Index line

Append (or move to the top of) one row in `<project_root>/docs/INDEX.md`:

```markdown
| Date | Type | Title | Agent | Status | Note |
|---|---|---|---|---|---|
| 2026-09-22 | feature | Bronze Auto Loader for bank statements | data-engineer-streaming | deployed-dev | [note](features/2026-09-22-bronze-bank-statement-autoloader.md) |
```

Create `INDEX.md` with that header row if it does not exist.

## Rules

- **No secrets, ever.** Key Rule 5 applies to the note as much as to code: no tokens, no env-var values, no connection strings, no unmasked PII sample rows. Reference the secret scope and key name instead.
- **Write what happened, not what should have happened.** If tests failed, a validate errored, or a step was skipped, the note says so. A note that claims a verification you did not run is worse than no note.
- The note is committed with the work on the same `feature/*` or `fix/*` branch, so it lands in the PR diff.
- Read-only agents (`security-reviewer`, `workspace-monitor`, `data-modeller` on review tasks) still write notes - under `design/` - and write **nothing else**.
- Keep it to one page. Detail that belongs in code comments or `context.md` stays there; link, don't duplicate.
