"""An example PySpark job on the Cairn lakehouse.

Runs as the Cairn user whose token is CAIRN_TOKEN (CAIRN_SPARK_TOKEN in Compose): Cairn decides every table
it reads or writes, the catalog vends temporary credentials for one table at a time, and the job's lineage
(tables and columns) appears in Cairn on the same datasets Trino and dbt use.

    docker compose -f docker-compose.cairn.yml --profile spark run --rm cairn-spark /jobs/lakehouse_products.py
"""

import sys

from cairn_spark import lakehouse_session

source = sys.argv[1] if len(sys.argv) > 1 else "analytics.products"
target = sys.argv[2] if len(sys.argv) > 2 else "analytics.products_by_price_band"

spark = lakehouse_session("lakehouse-products")
spark.sql(f"""
    CREATE OR REPLACE TABLE lakehouse.{target} USING iceberg AS
    SELECT CASE WHEN price < 10 THEN 'low' WHEN price < 100 THEN 'mid' ELSE 'high' END AS price_band,
           count(*) AS products, min(price) AS min_price, max(price) AS max_price
    FROM lakehouse.{source}
    GROUP BY 1
""")
for row in spark.sql(f"SELECT * FROM lakehouse.{target} ORDER BY price_band").collect():
    print(row.asDict())
spark.stop()
