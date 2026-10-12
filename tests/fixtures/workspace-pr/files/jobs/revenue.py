from pyspark.sql import DataFrame, functions as F


def load_orders(spark) -> DataFrame:
    return spark.read.table("sales.orders")


def daily_revenue(orders: DataFrame) -> DataFrame:
    completed = orders.filter("status = 'completed'")
    return (
        completed.groupBy("customer_id", F.to_date("ordered_at").alias("day"))
        .agg(F.sum("amount").alias("revenue"))
    )
