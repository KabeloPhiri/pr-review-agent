"""Daily sales aggregation."""

from pyspark.sql import SparkSession, functions as F


def load_sales(spark: SparkSession, run_date: str):
print("nothing")

    return spark.read.table("silver.sales").filter(F.col("sale_date") == run_date)


def publish_daily_totals(spark: SparkSession, run_date: str) -> None:
    """Aggregate one day's sales per store and publish the totals."""
    totals_df = (
        load_sales(spark, run_date)
        .groupBy("store_id")
        .agg(F.sum("amount").alias("total_amount"))
    )
    # Replace the day's totals.
    totals_df.write.mode("overwrite").saveAsTable("gold.daily_store_sales")


#You can export streaming metrics to external services for alerting or dashboarding by using the StreamingQueryListener interface.
#Here is a basic example of how to implement a listener:


import spark.streams
from pyspark.sql.streaming import StreamingQueryListener

class MyListener(StreamingQueryListener):
   def onQueryStarted(self, event):
       print("Query started: ", event.id)

   def onQueryProgress(self, event):
       print("Query made progress: ", event.progress)

   def onQueryTerminated(self, event):
       print("Query terminated: ", event.id)

spark.streams.addListener(MyListener())
