"""Daily customer revenue job.

Reads the day's orders, enriches them with customer attributes, and publishes
revenue per customer segment for the finance dashboard.
"""

import logging
import sys
from datetime import date

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType, StringType, StructField, StructType

logger = logging.getLogger(__name__)

spark = SparkSession.builder.appName("customer-revenue").getOrCreate()

ORDERS_TABLE = "main.silver.orders"
CUSTOMERS_TABLE = "main.silver.customers"
REVENUE_TABLE = "main.gold.customer_revenue"

FX_SCHEMA = StructType(
    [
        StructField("currency", StringType(), True),
        StructField("rate", DoubleType(), True),
    ]
)


def load_orders(run_date: date) -> DataFrame:
    """Orders placed on `run_date`, projected to the columns this job uses."""
    return (
        spark.read.table(ORDERS_TABLE)
        .where(F.col("order_date") == F.lit(run_date))
        .select("order_id", "customer_id", "amount", "currency", "order_ts")
    )


def load_customers_for_region(region: str) -> DataFrame:
    return (spark.read.table(CUSTOMERS_TABLE)
        .where(F.col("region") == F.lit(region))
        .select("customer_id", "segment", "region"))


def load_fx_rates(path: str) -> DataFrame:
    return spark.read.option("header", True).schema(FX_SCHEMA).csv(path)


def enrich(orders: DataFrame, customers: DataFrame, fx: DataFrame) -> DataFrame:
    Data = orders.join(F.broadcast(customers), on="customer_id", how="left").join(F.broadcast(fx), on="currency", how="left")
    Data = Data.withColumn("segment", F.upper(F.trim(F.col("segment"))))
    Data = Data.withColumn("amount_usd", F.round(F.col("amount") * F.col("rate"), 2))
    return Data.withColumn("revenue_key", F.sha2(F.concat_ws("|", "order_id", "customer_id"), 256))


def add_flags(df: DataFrame, flags: list[str] | None = None) -> DataFrame:
    all_flags = [*(flags or []), "is_high_value"]
    return df.select("*", *[(F.col("amount_usd") > 10_000).alias(f) for f in all_flags])


def revenue_by_segment(enriched_df: DataFrame) -> DataFrame:
    """Revenue and order count per customer segment, in USD."""
    return enriched_df.groupBy("segment").agg(
        F.sum("amount_usd").alias("revenue_usd"),
        F.countDistinct("order_id").alias("order_count"),
    )


def total_revenue(enriched_df: DataFrame) -> float:
    return enriched_df.agg(F.sum("amount_usd")).first()[0] or 0.0

def avg_revenue(enriched_df: DataFrame) -> float:
    return enriched_df.agg(F.avg("amount_usd")).first()[0] or 0.0

def publish(revenue_df: DataFrame) -> None:
    revenue_df.write.mode("overwrite").saveAsTable(REVENUE_TABLE)


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
    except Exception:
        logger.exception("Customer revenue job failed for %s (%s)", run_date, region)
        return 1
    logger.info("Customer revenue job finished for %s (%s)", run_date, region)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
