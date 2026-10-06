---
name: data-modelling
description: Design star/snowflake models, SCD Type 2 dimensions, partitioning and clustering strategy, and join-efficient queries on Databricks Delta/Unity Catalog. Use when creating or reviewing tables, dimensions, facts, MERGE logic, or when asked "how should this be partitioned/modelled/joined".
---

# Data modelling on Databricks (project conventions)

Source of truth: CLAUDE.md > *Key Design Decisions* and *Best practices*. This skill turns those into a checklist.

## 1. Canvas before deciding (non-negotiable)

Partitioning, clustering and indexing decisions require data evidence. Use the `data-sampling` skill:
- `DESCRIBE DETAIL` - size, file count, existing layout.
- Cardinality + skew of candidate columns (`templates/sql/table_canvas.sql` section 2-3).
- Growth per day (section 4) and query patterns (ask the user; check `system.query.history` via `workspace-monitoring` if available).
Write the evidence into the design note before proposing a layout.

## 2. Model shape

- Prefer **star schema**; snowflake only where a dimension is large and shared. Never propose 3NF for new lakehouse work (CLAUDE.md > Normalized data models 8-9).
- Wide denormalised facts are fine: file-level statistics skip data best when filter columns live on the fact.
- Keys: PK/FK constraints are informational only - declare them (`ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY ... NOT ENFORCED`) for lineage/BI tools, but enforce uniqueness in pipeline code/tests.
- Surrogate keys: `BIGINT GENERATED ALWAYS AS IDENTITY` on dimensions; facts hold the dimension surrogate key + business key for late-arriving handling.
- Complex data: keep semi-structured payloads as `STRUCT`/`MAP`/`VARIANT` in bronze; flatten only what silver/gold need.

## 3. SCD Type 2 (preferred for dimensions)

Standard columns (camelCase): `effectiveFrom TIMESTAMP, effectiveTo TIMESTAMP, isCurrent BOOLEAN, rowHash STRING, loadedAt TIMESTAMP`.
- Template code: `templates/dab/src/utils/scd2.py` (PySpark) and `templates/sql/scd2_merge.sql` (SQL). Lakeflow/DLT: `APPLY CHANGES INTO ... STORED AS SCD TYPE 2`.
- Change detection via `rowHash` over tracked columns; untracked columns (audit cols) must be excluded from the hash.
- Two MERGEs = two transactions on one table; note the consistency window in docs (CLAUDE.md > Transactions 3).
- Fact loads join to the dimension on `businessKey AND eventTime BETWEEN effectiveFrom AND COALESCE(effectiveTo, '9999-12-31')`.

## 4. Storage layout

- **External tables preferred** (CLAUDE.md), with `LOCATION` under a governed external location; raw files explored through **Volumes**, never mounted paths.
- Partitioning: only when a partition has >= 1 GB and the column is low-cardinality and always filtered (date is the usual candidate). Otherwise use **liquid clustering** (`CLUSTER BY (colA, colB)`) - up to 4 columns, ordered by filter frequency. Never partition on high-cardinality IDs.
- "Indexing" on Databricks = data skipping statistics + clustering/Z-order + Bloom filters (rare). Keep stats fresh: enable predictive optimization for managed tables; for external tables schedule `ANALYZE TABLE ... COMPUTE STATISTICS FOR ALL COLUMNS` and `OPTIMIZE` in a DAB job.
- Streaming + batch: bronze append-only (Auto Loader / Structured Streaming), silver MERGE (SCD2), gold aggregates as materialized views where join compatible.

## 5. Joins (CLAUDE.md > Optimizing join performance)

1. No cross joins in latency-sensitive or frequently recomputed workloads.
2. Join smallest tables first; broadcast dimensions < ~100 MB (`/*+ BROADCAST(dim) */`).
3. Many joins + aggregations -> materialise intermediate results (temp table / materialized view) rather than one giant query.
4. Fresh statistics before tuning anything else.
5. Filters on one table used to skip data in another do NOT push through joins - put the filter column on the fact or use dynamic file pruning-friendly patterns.

## 6. Naming & schema discipline

- camelCase columns and table names (`dimCustomer`, `factSales`, `effectiveFrom`); PascalCase classes in code.
- Strict schema: explicit `CREATE TABLE` DDL with types and comments; `mergeSchema` only in bronze with a documented reason. Schema changes go through a PR and a migration script, never `overwriteSchema` on prod.
- Every table: `COMMENT`, owner group, `TBLPROPERTIES ('delta.enableChangeDataFeed'='true')` where downstream incremental consumers exist.

## Deliverable format

A design note (markdown) with: purpose, grain, keys, columns with types, SCD behaviour, layout decision + the canvas evidence that justified it, load pattern (stream/batch), consumers, and the DDL. Then the code under `src/` and the resource under `resources/` for the DAB - never a manual `CREATE TABLE` run against uat/prod.
