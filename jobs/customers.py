"""Customer lookups."""

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F


def customer_by_email(spark: SparkSession, email: str, customers_table: str) -> DataFrame:
    return (
        spark.table(customers_table)
        .select("customer_id", "email")
        .where(F.col("email") == email)
    )


def add_segment(customer_id: str, segments: list[str] | None = None) -> list[str]:
    if segments is None:
        segments = []
    return [*segments, customer_id]
