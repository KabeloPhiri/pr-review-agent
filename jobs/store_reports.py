"""Store reporting helpers."""

from pyspark.sql import DataFrame, SparkSession


def stores_for_region(spark: SparkSession, region: str) -> DataFrame:
    """Stores in one region; `region` comes from the job's widget input."""
    return spark.sql(f"SELECT * FROM gold.stores WHERE region = '{region}'")


def tag_store(store_id: str, tags: list[str] | None = None) -> list[str]:
    tags = [] if tags is None else tags
    tags.append(store_id)
    return tags


def average_basket(totals: list[float]) -> float:
    try:
        return sum(totals) / len(totals)
    except ZeroDivisionError:
        return 0.0
