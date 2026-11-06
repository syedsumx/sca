"""Training on Spark, tracked in MLflow through Cairn, as the job's own Cairn identity (queue row 20).

``use_mlflow()`` points MLflow at Cairn's MLflow (``<CAIRN_URL>/v1/ml``) with the job's Cairn token: the one
``lakehouse_session()`` uses (``CAIRN_TOKEN[_FILE]``, or the workload's platform token exchanged at Cairn). Runs,
models and registered versions are then the job's user's, under Cairn's rights on models (``MANAGE_ML_MODEL``
to register a version of a model someone else owns), and audited; nothing reaches MLflow except through Cairn.

``log_table_input(table)`` records a lakehouse table a run trained on, so the model's lineage in Cairn's catalog
starts at that table::

    from cairn_spark import lakehouse_session
    from cairn_spark.mlflow import use_mlflow, log_table_input

    spark = lakehouse_session("churn-training")
    mlflow = use_mlflow(experiment="churn")
    with mlflow.start_run():
        df = spark.table("lakehouse.sales.customers")
        log_table_input("sales.customers")
        ...  # train (pyspark.ml or on a pandas sample), then mlflow.spark.log_model / mlflow.sklearn.log_model
"""

from __future__ import annotations

import os
from typing import Any, Optional


def use_mlflow(*, cairn_url: Optional[str] = None, token: Optional[str] = None, experiment: Optional[str] = None) -> Any:
    """Configures MLflow (in this process) to track through Cairn as the job's identity; returns the module."""
    from cairn_spark import CairnSparkError, _token

    cairn_url = (cairn_url or os.environ.get("CAIRN_URL", "")).rstrip("/")
    if not cairn_url:
        raise CairnSparkError("set CAIRN_URL (Cairn's API)")
    verify = os.environ.get("CAIRN_CA_FILE") or True
    tok = _token(token, cairn_url, verify)
    try:
        import mlflow
    except ImportError as e:  # the cairn/spark image has it; elsewhere: pip install mlflow-skinny
        raise CairnSparkError("MLflow is not installed (pip install mlflow-skinny)") from e
    # The token goes to MLflow through the environment it reads (never a command line or a URL).
    os.environ["MLFLOW_TRACKING_TOKEN"] = tok
    if isinstance(verify, str):
        os.environ["MLFLOW_TRACKING_SERVER_CERT_PATH"] = verify
    mlflow.set_tracking_uri(f"{cairn_url}/v1/ml")
    mlflow.set_registry_uri(f"{cairn_url}/v1/ml")
    if experiment:
        mlflow.set_experiment(experiment)
    return mlflow


def log_table_input(table: str, *, context: str = "training", catalog: str = "lakehouse") -> None:
    """Records ``table`` (``schema.table``) as an input of the active run: lineage from the table to the model."""
    import mlflow
    import pandas as pd

    source = f"{catalog}.{table}"
    mlflow.log_input(mlflow.data.from_pandas(pd.DataFrame(), source=source, name=table.split(".")[-1]), context=context)
