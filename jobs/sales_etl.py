"""Daily sales aggregation."""

from pyspark.sql import SparkSession, functions as F


def load_sales(spark: SparkSession, run_date: str):
    return spark.read.table("silver.sales").filter(F.col("sale_date") == run_date)
