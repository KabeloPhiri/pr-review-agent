---
name: data-sampling
description: Canvas Unity Catalog data without reading it wholesale - stratified random samples plus edge cases, PII masked in-query, read-only through the Databricks CLI. Use whenever you need to look at table contents, profile columns, or check data before a partitioning / modelling decision.
---

# Data sampling (read-only, PII-safe)

CLAUDE.md rules this enforces: Key Rules 1-5 (no catalog changes, CLI-only access, no reading env vars) and the *Data Sampling* section (representative samples incl. edge cases; PII identified by column name and masked/hashed).

## Tools

| Tool | What it does |
|---|---|
| `tools/Invoke-DbsqlQuery.ps1` | Runs ONE read-only statement via `databricks api post /api/2.0/sql/statements`. Rejects anything that is not `SELECT / WITH / SHOW / DESCRIBE / EXPLAIN` or that contains a mutating keyword. Caps rows (`-MaxRows`, default 200). |
| `tools/Get-DataSample.ps1` | Fetches column metadata with `databricks tables get`, flags PII by name, then pulls a stratified sample (`-PartitionBy`, `-RowsPerGroup`) + edge cases (min/max of numeric & date columns, most-NULL rows) with PII hashed (`SHA2`) or masked (`'***'`). `-Profile` adds null % and distinct counts per column from a 10 000-row `TABLESAMPLE`. |

Both delegate auth to the CLI: pass `-Profile <name>` / `-CliProfile <name>` or rely on `DATABRICKS_HOST` / `DATABRICKS_TOKEN` already being set in the shell. **Never** read, print or grep those variables or `.databrickscfg`.

## Procedure

1. Metadata first, data last:
   ```powershell
   databricks tables get <catalog.schema.table> --output json          # columns, types, partition cols, format
   powershell -File tools/Invoke-DbsqlQuery.ps1 -Sql "DESCRIBE DETAIL <catalog.schema.table>" -AsTable
   ```
2. Sample:
   ```powershell
   powershell -File tools/Get-DataSample.ps1 -Table <catalog.schema.table> -PartitionBy <categoryCol> -RowsPerGroup 10 -Profile -OutputJson <job-tmp>/sample.json
   ```
   Choose `-PartitionBy` as the column whose distribution matters for the decision at hand (region, eventType, sourceSystem...). Without it you get a plain random sample.
3. Review PII flags printed by the tool. If a column is PII but not name-detectable (e.g. `col17` holding emails), rerun with `-ExtraPiiColumns col17`. If a flagged column is not PII (e.g. `cityId` surrogate key), use `-NotPii cityId`. When unsure, leave it masked.
4. Ad-hoc canvas queries go through `Invoke-DbsqlQuery.ps1` - use `templates/sql/table_canvas.sql` for cardinality, skew, growth and history checks. Always `GROUP BY` / aggregate rather than selecting raw rows.
5. Put conclusions (distributions, skew, null rates, growth rate, candidate partition/cluster columns) into `context.md` or the design note - not the raw rows.

## Hard limits

- Never bypass the tools with `spark.sql`, notebooks, `databricks fs cat` on table locations, or raw REST calls that skip the read-only guard.
- Never raise `-MaxRows` above what the decision needs; 200-2 000 rows is the ceiling for canvasing.
- Never paste sampled PII (even hashed) into commit messages, PRs, or docs. Summaries only.
- A request to "fix" or "update" data in the catalog is out of scope for every agent - route it to pipeline code deployed via DAB and reviewed by a human.
