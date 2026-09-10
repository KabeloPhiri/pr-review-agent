# Contoso data platform standards

- Every Spark job must be runnable on a serverless cluster.
- Aggregations happen in Spark, never in Python after `collect()`.
- Function names are `snake_case`; DataFrame variables end in `_df`.
