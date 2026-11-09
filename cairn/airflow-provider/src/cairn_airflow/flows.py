"""Cairn Flows steps as Airflow tasks: one step of a flow, run in Cairn as the flow's owner.

A DAG Cairn generates from a flow (``GET /v1/flows/{name}/airflow``, or the DAG Cairn deploys for a flow with
``runner: airflow``) uses these for the steps Airflow has no equivalent for (approvals, for-each, derived tables,
agents, ...) or, for ``runner: airflow``, for every step::

    from cairn_airflow.flows import CairnFlowBranchOperator, CairnFlowStepOperator

    refresh = CairnFlowStepOperator(task_id="refresh", flow="nightly", step="refresh")
    size = CairnFlowBranchOperator(task_id="size", flow="nightly", step="size", then=["report"], otherwise=["note"])

The task asks Cairn to run the step (``POST /v1/flows/runs/{run}/steps/{step}/execute``) as the ``cairn``
connection's user, who must be the flow's owner, an editor or an admin; Cairn runs it as the flow's **owner**, with
the outputs of the steps before it, records it in the flow's run history, and answers with its status. The task
asks again until the step has finished (a step that waits, such as an approval, holds no Cairn thread meanwhile).
A run Cairn started (``runner: airflow``) names its Cairn run in the DAG run's conf (``cairn_run``); a DAG run
Airflow started on its own is recorded in Cairn as a run of the flow the first time one of its tasks asks.

Airflow's retries are the step's retries: each try asks Cairn to run the step again.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence
from urllib.parse import quote

try:  # Airflow 3
    from airflow.sdk import BaseOperator
except ImportError:  # pragma: no cover
    from airflow.models import BaseOperator

try:  # Airflow 3
    from airflow.sdk.exceptions import AirflowException, AirflowSkipException
except ImportError:  # pragma: no cover - older 3.x layouts
    from airflow.exceptions import AirflowException, AirflowSkipException

try:  # the standard provider (Airflow 3)
    from airflow.providers.standard.operators.branch import BaseBranchOperator
except ImportError:  # pragma: no cover - older layouts
    from airflow.operators.branch import BaseBranchOperator  # type: ignore[no-redef]

from cairn_airflow.hooks import CairnHook

DONE = ("ok", "failed", "skipped", "cancelled")


class _CairnStep:
    """What both operators do: find the Cairn run of this DAG run, run the step there, wait for it to finish."""

    flow: str
    step: str
    cairn_conn_id: str
    poll_interval: float
    log: Any

    def _cairn_run(self, hook: CairnHook, context: Any) -> str:
        dag_run = context.get("dag_run")
        conf = dict(getattr(dag_run, "conf", None) or {})
        if conf.get("cairn_run"):
            return str(conf["cairn_run"])
        ti = context.get("ti")
        logical = context.get("logical_date") or getattr(dag_run, "logical_date", None)
        params = {k: v for k, v in dict(context.get("params") or {}).items() if v is not None}
        got = hook.api("POST", f"/v1/flows/{quote(self.flow, safe='')}/airflow-runs", json={
            "dag_id": getattr(ti, "dag_id", None) or getattr(dag_run, "dag_id", ""),
            "dag_run_id": str(context.get("run_id") or getattr(dag_run, "run_id", "")),
            "logical_date": logical.date().isoformat() if hasattr(logical, "date") else None,
            "params": params})
        return str(got["run"])

    def _run_step(self, context: Any) -> dict:
        hook = CairnHook(self.cairn_conn_id)
        run = self._cairn_run(hook, context)
        ti = context.get("ti")
        tries = int(getattr(ti, "try_number", 1) or 1)
        path = f"/v1/flows/runs/{quote(run, safe='')}/steps/{quote(self.step, safe='')}/execute"
        said = None
        while True:
            out = hook.api("POST", path, json={"try_number": tries})
            status = out.get("status")
            if status != said:
                said = status
                self.log.info("Cairn: step %s of flow %s (run %s) is %s", self.step, self.flow, run, status)
            if status == "ok":
                return out
            if status in ("failed", "cancelled"):
                raise AirflowException(f"Cairn: {self.step} {status}: {out.get('error') or 'see its log in Cairn'}")
            if status == "skipped":
                raise AirflowSkipException(out.get("error") or f"Cairn skipped {self.step}")
            time.sleep(self.poll_interval)


class CairnFlowStepOperator(_CairnStep, BaseOperator):
    """Runs one step of a Cairn flow in Cairn, as the flow's owner; returns the step's output (bounded)."""

    template_fields: Sequence[str] = ()
    ui_color = "#e7f0ff"

    def __init__(self, *, flow: str, step: str, cairn_conn_id: str = "cairn_default", poll_interval: float = 5.0,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.flow, self.step, self.cairn_conn_id = flow, step, cairn_conn_id
        self.poll_interval = max(1.0, float(poll_interval))

    def execute(self, context: Any) -> Optional[dict]:
        return self._run_step(context).get("output")


class CairnFlowBranchOperator(_CairnStep, BaseBranchOperator):
    """A flow's branch: Cairn evaluates the condition (as the flow's owner, with the outputs before it); the tasks of
    the side it chose run and Airflow skips the others."""

    ui_color = "#fff3d6"

    def __init__(self, *, flow: str, step: str, then: Sequence[str] = (), otherwise: Sequence[str] = (),
                 cairn_conn_id: str = "cairn_default", poll_interval: float = 5.0, **kwargs: Any):
        super().__init__(**kwargs)
        self.flow, self.step, self.cairn_conn_id = flow, step, cairn_conn_id
        self.then, self.otherwise = list(then), list(otherwise)
        self.poll_interval = max(1.0, float(poll_interval))

    def choose_branch(self, context: Any) -> list[str]:
        out = self._run_step(context)
        chosen = (out.get("output") or {}).get("chosen")
        self.log.info("Cairn: %s took the %s side", self.step, chosen)
        return self.then if chosen == "then" else self.otherwise
