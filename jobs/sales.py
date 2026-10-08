"""Daily sales aggregation."""

import logging

from pyspark.sql import DataFrame, SparkSession, functions as F

logger = logging.getLogger(__name__)


def load_sales(spark: SparkSession, run_date: str):
    logger.info("Loading silver.sales for %s", run_date)

    return spark.read.table("silver.sales").filter(F.col("sale_date") == run_date)



def load_orders(spark: SparkSession, run_date: str, orders_table: str) -> DataFrame:
    orders_df = spark.read.table(orders_table)
    return orders_df.filter(F.col("order_date") == run_date)