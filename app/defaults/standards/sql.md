---
applies_to:
  languages: [sql]
---
# SQL standards

Applies to `.sql` files and to SQL strings embedded in Python.

## Correctness and safety (these are usually `error`)
- String-interpolated SQL built from request or user input — require parameter binding.
- `DELETE` or `UPDATE` without a `WHERE` clause.
- `SELECT *` in anything other than an ad-hoc exploration script.
- Implicit cross joins (comma-separated tables in `FROM`, or a join with no predicate).
- `MERGE` without a matching condition on a unique key.

## Readability
- Keywords in a consistent case (follow the file's existing convention).
- CTEs over deeply nested subqueries; each CTE named for what it produces.
- Explicit column lists on `INSERT`.
- Aliases that mean something: `orders o`, not `orders a`.

## Performance
- Filters pushed as early as possible, before joins where semantics allow.
- No functions applied to a filtered column when the raw column would be sargable
  (for example `WHERE date(ts) = ...` instead of a range on `ts`).
- `DISTINCT` used to mask a join that fans out — flag the join instead.
- Partition or cluster columns used in the predicate when the table is partitioned.

## Unity Catalog
- Objects referenced with full three-level names (`catalog.schema.table`) in production code.
- No hard-coded environment names; they belong in configuration or a variable.
