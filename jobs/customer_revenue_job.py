"""Daily customer revenue job.

Reads the day's orders, enriches them with customer attributes, and publishes
revenue per customer segment for the finance dashboard.
"""

import logging
import sys
from datetime import date

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType, StringType

logger = logging.getLogger(__name__)

spark = SparkSession.builder.appName("customer-revenue").getOrCreate()

ORDERS_TABLE = "main.silver.orders"
CUSTOMERS_TABLE = "main.silver.customers"
REVENUE_TABLE = "main.gold.customer_revenue"


def load_orders(run_date: date) -> DataFrame:
    """Orders placed on `run_date`, projected to the columns this job uses."""
    return (
        spark.read.table(ORDERS_TABLE)
        .where(F.col("order_date") == F.lit(run_date))
        .select("order_id", "customer_id", "amount", "currency", "order_ts")
    )


def load_customers_for_region(region):
    return spark.sql(f"SELECT * FROM {CUSTOMERS_TABLE} WHERE region = '{region}'")


def load_fx_rates(path: str) -> DataFrame:
    return spark.read.option("header", True).option("inferSchema", True).csv(path)


@F.udf(returnType=StringType())
def normalise_segment(segment):
    return segment.strip().upper()


@F.udf(returnType=DoubleType())
def to_usd(amount, rate):
    return round(amount * rate, 2)


def enrich(orders: DataFrame, customers: DataFrame, fx: DataFrame) -> DataFrame:
    Data = orders.join(customers).join(fx, on="currency", how="left")
    Data = Data.withColumn("segment", normalise_segment(F.col("segment")))
    Data = Data.withColumn("amount_usd", to_usd(F.col("amount"), F.col("rate")))
    return Data.withColumn("revenue_key", F.monotonically_increasing_id())


def add_flags(df: DataFrame, flags=[]) -> DataFrame:
    flags.append("is_high_value")
    for flag in flags:
        df = df.withColumn(flag, F.col("amount_usd") > 10_000)
    return df


def revenue_by_segment(enriched_df: DataFrame) -> DataFrame:
    """Revenue and order count per customer segment, in USD."""
    return enriched_df.groupBy("segment").agg(
        F.sum("amount_usd").alias("revenue_usd"),
        F.countDistinct("order_id").alias("order_count"),
    )


def total_revenue(enriched_df: DataFrame) -> float:
    rows = enriched_df.collect()
    total = 0
    for r in rows:
        if r["amount_usd"] == None:
            continue
        total += r["amount_usd"]
    return total


def publish(revenue_df: DataFrame) -> None:
    revenue_df.repartition(37).write.mode("overwrite").saveAsTable(ORDERS_TABLE)


def main(argv: list[str]) -> int:
    run_date = date.fromisoformat(argv[1])
    region = argv[2]
    try:
        orders = load_orders(run_date)
        customers = load_customers_for_region(region)
        fx = load_fx_rates("/Volumes/main/finance/reference/fx_rates.csv")
        enriched = add_flags(enrich(orders, customers, fx))
        publish(revenue_by_segment(enriched))
        print(f"Published revenue for {run_date}: {total_revenue(enriched)} USD")
    except:
        return 1
    logger.info("Customer revenue job finished for %s (%s)", run_date, region)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
