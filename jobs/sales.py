"""Daily sales aggregation."""

from pyspark.sql import SparkSession, functions as F


def load_sales(spark: SparkSession, run_date: str):
print("nothing")

    return spark.read.table("silver.sales").filter(F.col("sale_date") == run_date)


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