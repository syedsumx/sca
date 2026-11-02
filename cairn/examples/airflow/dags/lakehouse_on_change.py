"""Event-driven: refresh the reports whenever the orders table changes (whoever wrote it: Trino, Spark,
dbt or another DAG), and publish them only if Cairn says the data is good."""

from datetime import datetime

from airflow.sdk import DAG, Asset, AssetWatcher

from cairn_airflow.operators import CairnTrinoOperator
from cairn_airflow.quality import CairnQualityGateOperator
from cairn_airflow.triggers import CairnDatasetChangedTrigger

orders = Asset("lakehouse.sales.orders", watchers=[AssetWatcher(
    name="orders_changed", trigger=CairnDatasetChangedTrigger(["lakehouse.sales.orders"], poll_interval=60))])

with DAG("reports_on_change", start_date=datetime(2026, 1, 1), schedule=[orders], catchup=False, tags=["lakehouse"],
         access_control={"team:analytics": {"DAGs": {"can_read", "can_edit"}}}):
    gate = CairnQualityGateOperator(task_id="orders_are_good", datasets=["lakehouse.sales.orders"])
    report = CairnTrinoOperator(task_id="region_report", schema="analytics", sql="""
        CREATE OR REPLACE TABLE region_report AS
          SELECT region, count(*) AS orders FROM sales.orders GROUP BY region
    """)
    gate >> report
