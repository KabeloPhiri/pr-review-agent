---
name: workspace-monitoring
description: Monitor a Databricks workspace for cost, performance, errors, data growth and schema evolution using system tables and the CLI, read-only. Use when asked about spend, slow or failing jobs, streaming lag/backpressure, table growth, or to build monitoring scripts/jobs.
---

# Workspace monitoring

CLAUDE.md: "Scripts to monitor environment are important (Costing, performance, errors, data growth and evolution)". All monitoring is **read-only** and runs through `tools/Invoke-DbsqlQuery.ps1` or `databricks` CLI list/get commands. Deliverables that should run on a schedule become a DAB `sql_task`/job (see `dab-scaffold`), not a notebook someone runs by hand.

## Ready-made queries in this toolbox (`scripts/`)

| File | Question it answers | System tables |
|---|---|---|
| `scripts/most_expensive_job` | Top jobs by list cost, last 30 days | `system.billing.usage`, `system.billing.list_prices`, `system.lakeflow.jobs` |
| `scripts/average_batch_duration` | Streaming/batch micro-batch duration trend | `system.lakeflow.*` / listener metrics |
| `scripts/average_throughput` | Rows/sec per pipeline | listener metrics |
| `scripts/cluster_and_slots_utilization` | Cluster utilisation | `system.compute.*` |
| `scripts/pipeline_backpressure_measurement.sql` | Input rate vs processing rate (backpressure) | streaming progress |
| `scripts/streaming_query_listener.py`, `streaming_query_metrics.py` | `StreamingQueryListener` skeletons to export progress metrics | n/a (runs in the job) |
| `templates/dab/monitoring/job_run_health.sql` | Failures & duration by job, last 7 days | `system.lakeflow.job_run_timeline` |

Run one with:
```powershell
powershell -File tools/Invoke-DbsqlQuery.ps1 -SqlFile scripts/most_expensive_job -MaxRows 50 -AsTable
```

## Coverage checklist per area

**Costing** - `system.billing.usage` joined to `list_prices`; group by `usage_metadata.job_id`, `cluster_id`, `warehouse_id`, custom tags (`costCenter`). Alert on week-over-week > 25 %. Always tag resources in DAB YAML so cost is attributable.

**Performance** - `system.lakeflow.job_run_timeline` (durations, `result_state`), `system.query.history` (SQL warehouse latency, bytes scanned, `spilled_local_bytes`), `system.compute.node_timeline` (CPU/mem utilisation). Streaming: `StreamingQueryListener.onQueryProgress` -> `inputRowsPerSecond`, `processedRowsPerSecond`, `batchDuration`, `numInputRows`; backpressure = input rate > processed rate sustained.

**Errors** - failed/timed-out runs from `job_run_timeline`; `system.access.audit` for permission denials; pipeline event logs (`event_log(TABLE(...))`) for DLT expectations failures.

**Data growth & evolution** - `DESCRIBE DETAIL` (sizeInBytes, numFiles) snapshotted daily into a monitoring table by a DAB job; `DESCRIBE HISTORY` for schema changes (`operation IN ('ADD COLUMNS','CHANGE COLUMN','REPLACE TABLE')`), `system.information_schema.columns` diffed over time; small-file ratio (numFiles vs size) as an OPTIMIZE trigger.

## Procedure for a monitoring request

1. Confirm the question, environment (dev/uat/prod) and window.
2. Check `system.*` schemas are enabled and readable: `SHOW SCHEMAS IN system` via `Invoke-DbsqlQuery.ps1`. If not, tell the user which to enable (`databricks system-schemas enable`) - do not attempt it yourself unless asked.
3. Run the query with a tight `-MaxRows`; aggregate, never dump raw usage rows.
4. Report: table of findings, the SQL used (so it can be re-run), and the recommended action (right-size cluster, add autoscaling cap, schedule OPTIMIZE, pause a runaway job) - actions are proposals for the user, executed via DAB changes in a branch.
5. If it should recur: add it under `monitoring/` in the bundle with a `sql_task` on a schedule and an `email_notifications` / alert destination.

## Guardrails

- Read-only only: `Invoke-DbsqlQuery.ps1` blocks mutations; do not work around it.
- Do not read `DATABRICKS_HOST` / `DATABRICKS_TOKEN`; the CLI consumes them.
- Never `databricks jobs delete`, `clusters delete`, or `runs cancel` on uat/prod from an agent session; propose it in the report.
