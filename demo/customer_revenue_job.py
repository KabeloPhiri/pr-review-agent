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
from pyspark.sql.types import DecimalType, StringType, StructField, StructType

logger = logging.getLogger(__name__)

# Defaults can be overridden via environment variables to target dev/test.
ORDERS_TABLE = os.environ.get("ORDERS_TABLE", "main.silver.orders")
CUSTOMERS_TABLE = os.environ.get("CUSTOMERS_TABLE", "main.silver.customers")
REVENUE_TABLE = os.environ.get("REVENUE_TABLE", "main.gold.customer_revenue")
FX_RATES_PATH = os.environ.get(
    "FX_RATES_PATH", "/Volumes/main/finance/reference/fx_rates.csv"
)

HIGH_VALUE_THRESHOLD_USD = 10_000

FX_SCHEMA = StructType(
    [
        StructField("currency", StringType()),
        StructField("rate", DecimalType(18, 8)),
    ]
)


def load_orders(spark: SparkSession, run_date: date, table: str) -> DataFrame:
    """Orders placed on `run_date`, projected to the columns this job uses."""
    return (
        spark.read.table(table)
        .where(F.col("order_date") == F.lit(run_date))
        .select("order_id", "customer_id", "amount", "currency", "order_ts")
    )


def load_customers_for_region(spark: SparkSession, region: str, table: str) -> DataFrame:
    return (
        spark.read.table(table)
        .where(F.col("region") == F.lit(region))
        .select("customer_id", "segment")
    )


def load_fx_rates(spark: SparkSession, path: str) -> DataFrame:
    return spark.read.option("header", True).schema(FX_SCHEMA).csv(path)


def enrich(orders: DataFrame, customers: DataFrame, fx: DataFrame) -> DataFrame:
    orders_joined = orders.join(customers, on="customer_id", how="inner").join(
        F.broadcast(fx), on="currency", how="left"
    )
    # Orders without an fx rate cannot be converted to USD; exclude them explicitly
    # rather than letting a null amount_usd silently vanish from the sums.
    orders_rated = orders_joined.where(F.col("rate").isNotNull())
    orders_segmented = orders_rated.withColumn("segment", F.upper(F.trim(F.col("segment"))))
    orders_priced = orders_segmented.withColumn(
        "amount_usd", F.round(F.col("amount") * F.col("rate"), 2)
    )
    return orders_priced.withColumn(
        "revenue_key", F.sha2(F.concat_ws("|", "order_id", "customer_id"), 256)
    )


def add_flags(df: DataFrame) -> DataFrame:
    return df.withColumn(
        "is_high_value", F.col("amount_usd") > HIGH_VALUE_THRESHOLD_USD
    )


def revenue_by_segment(enriched_df: DataFrame, run_date: date, region: str) -> DataFrame:
    """Revenue and order count per customer segment, in USD, for one run_date and region."""
    return (
        enriched_df.groupBy("segment")
        .agg(
            F.sum("amount_usd").alias("revenue_usd"),
            F.countDistinct("order_id").alias("order_count"),
        )
        .withColumn("run_date", F.lit(run_date))
        .withColumn("region", F.lit(region))
    )


def publish(revenue_df: DataFrame, table: str, run_date: date, region: str) -> None:
    safe_region = region.replace("'", "''")
    (
        revenue_df.write.mode("overwrite")
        .option(
            "replaceWhere",
            f"run_date = '{run_date.isoformat()}' AND region = '{safe_region}'",
        )
        .saveAsTable(table)
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "Usage: customer_revenue_job.py <run_date YYYY-MM-DD> <region>",
            file=sys.stderr,
        )
        return 2
    run_date = date.fromisoformat(argv[1])
    region = argv[2]
    spark = SparkSession.builder.appName("customer-revenue").getOrCreate()
    try:
        orders = load_orders(spark, run_date, ORDERS_TABLE)
        customers = load_customers_for_region(spark, region, CUSTOMERS_TABLE)
        fx = load_fx_rates(spark, FX_RATES_PATH)
        enriched = add_flags(enrich(orders, customers, fx))
        publish(
            revenue_by_segment(enriched, run_date, region),
            REVENUE_TABLE,
            run_date,
            region,
        )
        logger.info("Published revenue for %s", run_date)
    except Exception:
        logger.exception(
            "Customer revenue job failed for %s (%s); check input tables and the FX file",
            run_date,
            region,
        )
        return 1
    finally:
        spark.stop()
    logger.info("Customer revenue job finished for %s (%s)", run_date, region)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
