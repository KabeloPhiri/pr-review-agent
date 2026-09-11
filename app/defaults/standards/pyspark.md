---
# Narrowed to Spark code: these rules would be noise on a plain script. The
# pattern is the one that used to be hardcoded as _PYSPARK_HINTS in
# chunking.py. Single-quoted so YAML keeps the backslashes literal.
applies_to:
  languages: [python]
  path_globs: ['*spark*']
  content_match: '\b(pyspark|SparkSession|spark\.(read|sql|table|createDataFrame)|DataFrame|withColumn|groupBy|dbutils|delta)\b'
---
# PySpark standards

Applies to `.py` files that import `pyspark`, use a `SparkSession`, or manipulate DataFrames.

## Performance and correctness (these are usually `error`)
- `collect()`, `toPandas()`, or `count()` on an unbounded DataFrame in production code.
  Suggest `limit()`, aggregation, or writing to a table instead.
- `SELECT *` semantics: `df.select("*")` or reading a wide table without projecting the
  columns actually needed.
- Joins without an explicit `on` condition, or a join that silently becomes a cross join.
- Missing broadcast hint on a small-to-large join where the small side is clearly a lookup.
- UDFs (especially Python UDFs) where a built-in `pyspark.sql.functions` equivalent exists.
- `.repartition()` / `.coalesce()` with a magic number and no comment explaining it.
- Chained `withColumn` calls in a loop — suggest a single `select` with all expressions.
- Non-deterministic operations (`monotonically_increasing_id`, `rand`) used as a stable key.

## Schema and data handling
- Reading with `inferSchema` in production paths; declare an explicit `StructType`.
- Implicit casts between numeric and string types; make casts explicit.
- Null handling stated explicitly on joins and aggregations that can produce nulls.
- Writes declare `mode` explicitly; flag any write that could silently overwrite data.

## Structure
- Spark session creation lives in one place, not per-module.
- Transformations are pure functions of `DataFrame -> DataFrame` so they can be unit tested.
- Hard-coded table names, paths, or catalog names should come from configuration.
- No `display()` or `dbutils` calls in library code that is not a notebook.
