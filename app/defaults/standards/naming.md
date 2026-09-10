# Naming conventions

Applies to every language below unless the reviewed repo overrides it.

## Python
- `snake_case` for modules, functions, variables; `PascalCase` for classes;
  `UPPER_SNAKE_CASE` for module-level constants.
- A leading underscore means "private to this module" — flag external use of `_name`.
- No single-letter names except loop counters and comprehension variables.
- Booleans read as predicates: `is_active`, `has_rows`, `should_retry`.
- Collections are plural (`orders`), scalars are singular (`order`).
- Avoid abbreviations that are not already used in the repository.

## PySpark
- DataFrames carry their grain in the name: `orders_df`, `daily_sales_df`.
- Intermediate DataFrames say what stage they are: `orders_raw`, `orders_clean`, `orders_agg`.
- Column names are `snake_case` regardless of the source system's casing.
- Timestamp columns end in `_ts`, dates in `_date`, flags in `is_` / `has_`.

## SQL
- Tables are plural nouns; views are prefixed `vw_` only if the repo already does that.
- Columns are `snake_case`; primary keys are `<entity>_id`.
- CTEs are named for their output (`monthly_totals`), never `tmp1` or `a`.
- Medallion layers keep their prefix or schema: `bronze`, `silver`, `gold`.

## Files
- Python modules are `snake_case.py`; one primary class or cohesive function group per module.
- SQL files are named for the object or transformation they produce.
