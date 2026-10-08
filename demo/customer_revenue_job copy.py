"""Daily customer revenue job.

Reads the day's orders, enriches them with customer attributes, and publishes
revenue per customer segment for the finance dashboard.
"""

import logging
import os
import sys
from datetime import date

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType, StringType, StructField, StructType

logger = logging.getLogger(__name__)

ORDERS_TABLE = os.environ.get("ORDERS_TABLE", "main.silver.orders")
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "main.silver.customers")
REVENUE_TABLE = os.environ.get("REVENUE_TABLE", "main.gold.customer_revenue")
FX_RATES_PATH = os.environ.get(
    "FX_RATES_PATH", "/Volumes/main/finance/reference/fx_rates.csv"
)

FX_SCHEMA = StructType(
    [
        StructField("currency", StringType(), True),
        StructField("rate", DoubleType(), True),
    ]
)


def load_orders(spark: SparkSession, run_date: date) -> DataFrame:
    """Orders placed on `run_date`, projected to the columns this job uses."""
    return (
        spark.read.table(ORDERS_TABLE)
        .where(F.col("order_date") == F.lit(run_date))
        .select("order_id", "customer_id", "amount", "currency", "order_ts")
    )


def load_customers_for_region(spark: SparkSession, region: str) -> DataFrame:
    return (spark.read.table(CUSTOMERS_TABLE)
        .where(F.col("region") == F.lit(region))
        .select("customer_id", "segment", "region"))


def load_fx_rates(spark: SparkSession, path: str) -> DataFrame:
    return spark.read.option("header", True).schema(FX_SCHEMA).csv(path)


def enrich(orders: DataFrame, customers: DataFrame, fx: DataFrame) -> DataFrame:
    orders_enriched = orders.join(F.broadcast(customers), on="customer_id", how="inner")
    orders_enriched = orders_enriched.join(F.broadcast(fx), on="currency", how="left")
    orders_enriched = orders_enriched.withColumn("segment", F.upper(F.trim(F.col("segment"))))
    orders_enriched = orders_enriched.withColumn("amount_usd", F.round(F.col("amount") * F.col("rate"), 2))
    return orders_enriched.withColumn("revenue_key", F.sha2(F.concat_ws("|", "order_id", "customer_id"), 256))


def add_flags(df: DataFrame, flags: list[str] | None = None) -> DataFrame:
    conditions = {"is_high_value": F.col("amount_usd") > 10_000}
    all_flags = list(dict.fromkeys([*(flags or []), "is_high_value"]))
    unknown = [f for f in all_flags if f not in conditions]
    if unknown:
        raise ValueError(f"Unsupported flags: {unknown}")
    return df.withColumns({f: conditions[f] for f in all_flags})


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

def publish(revenue_df: DataFrame, run_date: date, region: str) -> None:
    output_df = (
        revenue_df.withColumn("run_date", F.lit(run_date))
        .withColumn("region", F.lit(region))
    )
    escaped_region = region.replace("'", "''")
    replace_where = f"run_date = '{run_date.isoformat()}' AND region = '{escaped_region}'"
    (
        output_df.write.mode("overwrite")
        .option("replaceWhere", replace_where)
        .saveAsTable(REVENUE_TABLE)
    )


def main(argv: list[str]) -> int:
    run_date = date.fromisoformat(argv[1])
    region = argv[2]
    spark = SparkSession.builder.appName("customer-revenue").getOrCreate()
    try:
        orders = load_orders(spark, run_date)
        customers = load_customers_for_region(spark, region)
        fx = load_fx_rates(spark, FX_RATES_PATH)
        enriched = add_flags(enrich(orders, customers, fx)).cache()
        if enriched.filter(F.col("rate").isNull()).limit(1).count() > 0:
            logger.warning(
                "Some orders for %s (%s) have no FX rate; their revenue is excluded from totals",
                run_date,
                region,
            )
        publish(revenue_by_segment(enriched), run_date, region)
        logger.info("Published revenue for %s: %s USD", run_date, total_revenue(enriched))
    except Exception:
        logger.exception("Customer revenue job failed for %s (%s)", run_date, region)
        return 1
    logger.info("Customer revenue job finished for %s (%s)", run_date, region)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
