"""A daily lakehouse pipeline: SQL on Trino, then the dbt project, both as the pipeline's Cairn user
(the `cairn_default` connection). Runs and lineage show up in Cairn."""

from datetime import datetime

from airflow.sdk import DAG

from cairn_airflow.operators import CairnDbtOperator, CairnTrinoOperator

with DAG("lakehouse_daily", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False, tags=["lakehouse"],
         default_args={"retries": 1}):
    daily = CairnTrinoOperator(
        task_id="daily_orders",
        schema="analytics",
        sql="""
            CREATE TABLE IF NOT EXISTS daily_orders (day date, region varchar, orders bigint);
            DELETE FROM daily_orders WHERE day = DATE '{{ ds }}';
            INSERT INTO daily_orders
              SELECT DATE '{{ ds }}', region, count(*) FROM sales.orders GROUP BY region
        """,
    )
    build = CairnDbtOperator(task_id="dbt_build", project_dir="/opt/airflow/dbt/lakehouse", command="build")
    daily >> build
