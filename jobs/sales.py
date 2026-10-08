"""Daily sales aggregation."""

from pyspark.sql import SparkSession, functions as F


def load_sales(spark: SparkSession, run_date: str):
    print("nothing")

    return spark.read.table("silver.sales").filter(F.col("sale_date") == run_date)



def load_orders(spark: SparkSession, run_date: str):
    df.filter(F.col("order_date") == run_date)
    return df