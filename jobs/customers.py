"""Customer lookups."""

from pyspark.sql import DataFrame, SparkSession


def customer_by_email(spark: SparkSession, email: str) -> DataFrame:
    return spark.sql(f"SELECT * FROM gold.customers WHERE email = '{email}'")


def add_segment(customer_id: str, segments: list = []) -> list:
    segments.append(customer_id)
    return segments
