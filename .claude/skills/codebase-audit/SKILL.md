---
name: codebase-audit
description: Project-init audit of an existing Databricks codebase - secrets, naming conventions, classes, function definitions/inputs/outputs/connectors, table references - producing <project_root>/context.md as the canvas for all later work. Use when onboarding onto an existing repo or when asked to "audit", "canvas" or "map" a codebase.
---

# Codebase audit -> context.md

Goal (CLAUDE.md > Project init): understand the codebase and the tables it touches, and keep that canvas in `<project_root>/context.md` so every later agent starts from it instead of re-reading the repo.

## Procedure

1. **Generate the mechanical inventory**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools/Get-CodeInventory.ps1 -Path <project_root> -Convention camelCase
   ```
   This writes `context.md` with: file inventory, imports, Unity Catalog table references, classes, functions (inputs / outputs / docstring / calls / called-by), naming violations, and an embedded secret scan (masked). Nothing is executed, no data read.
2. **If secrets were found**, stop and run the `secret-scan` skill before doing anything else - the user should not be pushing further commits with credentials in the tree.
3. **Enrich by reading code** (Read/Grep - source files only, never config with credentials). Add to `context.md`, under the generated sections, a hand-written **"9. Narrative"**:
   - Medallion mapping: which tables are bronze / silver / gold; managed vs external; volumes used for raw exploration.
   - Entry points: which functions/notebooks are job tasks (look for `databricks.yml`, `resources/*.yml`, `# Databricks notebook source`, `if __name__ == "__main__"`).
   - Data flow: source -> bronze -> silver -> gold as a short mermaid or bullet chain.
   - SCD handling: is SCD2 implemented, partially, or absent? (search for `MERGE INTO`, `isCurrent`, `effectiveTo`, `APPLY CHANGES`).
   - Modelling: star / snowflake / 3NF / none - name the fact and dimension tables.
   - Partitioning & clustering: what is declared (`PARTITIONED BY`, `CLUSTER BY`, `partitionBy(`) - flag tables with none and high volume.
   - Library pinning: `requirements.txt` / init scripts present and pinned? Unpinned = finding.
   - DAB / CI-CD: bundle present? pipelines present? manual deploy artefacts (e.g. `dbfs cp` scripts)?
   - Risks & quick wins (max 10 bullets, ordered by impact).
4. **Naming**: project rule is camelCase for functions, variables and columns; PascalCase for classes. Report violation counts and a representative handful - don't paste all 300. Offer, don't apply, a rename.
5. **Keep `context.md` current**: re-run step 1 after any refactor; the narrative section is preserved manually (the tool overwrites the file, so copy section 9 back or write it to `context.narrative.md` and reference it).

## Output expectations

- `context.md` in the project root (or the path the user names).
- A short chat summary: counts (files / functions / tables / violations / secrets), the top 5 risks, and what you need from the engineer (the "Open questions" section).
- Never include masked-or-not secret values, environment variable values or `.databrickscfg` contents in `context.md`.

## Tool limits to state when relevant

- The inventory is regex-based: decorators, nested functions, dynamically built table names (`f"{catalog}.silver.x"`) are only partially captured - the `Tables` section lists what was literal.
- Call graph is by function-name match within the repo; same-named functions in different modules will be merged.
