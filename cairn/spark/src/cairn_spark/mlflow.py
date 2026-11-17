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

import logging
import os
import threading
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def use_mlflow(*, cairn_url: Optional[str] = None, token: Optional[str] = None, experiment: Optional[str] = None) -> Any:
    """Configures MLflow (in this process) to track through Cairn as the job's identity; returns the module."""
    from cairn_spark import CairnSparkError, _token, workload_token

    cairn_url = (cairn_url or os.environ.get("CAIRN_URL", "")).rstrip("/")
    if not cairn_url:
        raise CairnSparkError("set CAIRN_URL (Cairn's API)")
    verify = os.environ.get("CAIRN_CA_FILE") or True
    workload = None if token else os.environ.get("CAIRN_WORKLOAD_TOKEN_FILE")
    if workload:
        tok, lasts = workload_token(cairn_url, workload, verify)
    else:
        tok = _token(token, cairn_url, verify)
    try:
        import mlflow
    except ImportError as e:  # the cairn/spark image has it; elsewhere: pip install mlflow-skinny
        raise CairnSparkError("MLflow is not installed (pip install mlflow-skinny)") from e
    # The token goes to MLflow through the environment it reads (never a command line or a URL). MLflow reads it
    # again for every request, so a token exchanged from the workload's (an hour, by default) is renewed there
    # before it expires: a training run longer than that still logs its model at the end.
    os.environ["MLFLOW_TRACKING_TOKEN"] = tok
    if workload:
        _keep_fresh(cairn_url, workload, verify, lasts)
    if isinstance(verify, str):
        os.environ["MLFLOW_TRACKING_SERVER_CERT_PATH"] = verify
    mlflow.set_tracking_uri(f"{cairn_url}/v1/ml")
    mlflow.set_registry_uri(f"{cairn_url}/v1/ml")
    if experiment:
        mlflow.set_experiment(experiment)
    return mlflow


_renewer: Optional[threading.Thread] = None
_renew = threading.Event()        # set: stop renewing (a later use_mlflow starts its own)


def _keep_fresh(cairn_url: str, path: str, verify, lasts: int) -> None:
    """Exchanges the workload's token again at three quarters of each Cairn token's life (the workload's own token
    is read again each time, as the platform rotates it), in a daemon thread, and gives MLflow the new one."""
    global _renewer, _renew
    from cairn_spark import CairnSparkError, workload_token

    _renew.set()                                       # one renewer per process: the newest settings win
    stop = _renew = threading.Event()

    def run(lasts: int) -> None:
        while not stop.wait(max(1.0, lasts * 0.75)):
            try:
                tok, lasts = workload_token(cairn_url, path, verify)
            except (CairnSparkError, OSError, httpx.HTTPError) as e:   # tried again soon; the old token may still do
                logger.warning("cairn_spark.mlflow: the Cairn token could not be renewed (%s)", e)
                lasts = 60
                continue
            os.environ["MLFLOW_TRACKING_TOKEN"] = tok

    _renewer = threading.Thread(target=run, args=(lasts,), name="cairn-mlflow-token", daemon=True)
    _renewer.start()


def log_table_input(table: str, *, context: str = "training", catalog: str = "lakehouse") -> None:
    """Records ``table`` (``schema.table``) as an input of the active run: lineage from the table to the model."""
    import mlflow
    import pandas as pd

    source = f"{catalog}.{table}"
    mlflow.log_input(mlflow.data.from_pandas(pd.DataFrame(), source=source, name=table.split(".")[-1]), context=context)
