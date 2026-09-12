"""Throwaway job used to smoke-test the AI reviewer. Safe to delete."""

from pyspark.sql import functions as F


def summarise_orders(spark, region, cutoff):
    query = "SELECT * FROM silver.orders WHERE region = '" + region + "'"
    df = spark.sql(query)
    rows = df.collect()
    try:
        return {r["id"]: r["total"] for r in rows}
    except:
        return {}
