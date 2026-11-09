"""Airflow drives Cairn: start a Cairn flow from a DAG and wait for it, or wait for a run something else started.

::

    from cairn_airflow.flow_runs import CairnFlowRunOperator, CairnFlowRunSensor

    score = CairnFlowRunOperator(task_id="score", flow="orders-scoring", flow_params={"day": "{{ ds }}"},
                                 deferrable=True)
    kick = CairnFlowRunOperator(task_id="kick", flow="refresh-marts", wait=False)      # returns the run's id
    marts = CairnFlowRunSensor(task_id="marts", run_id="{{ ti.xcom_pull(task_ids='kick') }}", mode="reschedule")

* **Who.** The ``cairn`` connection's Cairn identity (a pipeline user's token, or the workload token the connection
  exchanges): it must be the flow's owner, an editor or an admin to start it, and may see the run to wait for it. The
  flow's steps run as the flow's **owner**, as every Cairn run does; Airflow's identity never widens what they may do.
* **Start.** ``POST /v1/flows/{flow}/runs`` with ``flow_params`` (templated) and, optionally, ``version``. The run's
  id is the task's return value (XCom), and the log names it.
* **Wait** (``wait=True``, the default): ``deferrable=True`` hands the waiting to the triggerer
  (:class:`CairnFlowRunTrigger`, no worker slot held); otherwise the task looks every ``poll_interval`` seconds. The
  task succeeds when the run succeeds, and fails when it fails, is cancelled or is skipped, with the failed steps and
  their errors in the message.
* **Kill.** A task that is killed (marked failed or success, cleared, or timed out while it waits on a worker) cancels
  the Cairn run (``POST /v1/flows/runs/{id}/cancel``) unless ``cancel_on_kill=False``; deferred, the trigger's
  ``on_kill`` does it (Airflow 3.3 and later).
* :class:`CairnFlowRunSensor` waits for a run by ``run_id``, or for the latest run of ``flow``; ``deferrable=True``
  uses the same trigger.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, AsyncIterator, Optional, Sequence
from urllib.parse import quote

try:  # Airflow 3
    from airflow.sdk import BaseOperator, BaseSensorOperator
except ImportError:  # pragma: no cover
    from airflow.models import BaseOperator
    from airflow.sensors.base import BaseSensorOperator

try:  # Airflow 3
    from airflow.sdk.exceptions import AirflowException
except ImportError:  # pragma: no cover - older 3.x layouts
    from airflow.exceptions import AirflowException

from airflow.triggers.base import BaseTrigger, TriggerEvent

from cairn_airflow.hooks import CairnError, CairnHook

DONE = ("succeeded", "failed", "cancelled", "skipped")


def _run_path(run_id: str) -> str:
    return f"/v1/flows/runs/{quote(str(run_id), safe='')}"


def _summary(run: dict) -> dict:
    """What a task needs of a run: its status, error and the steps that did not succeed."""
    bad = [{"step": s.get("key"), "status": s.get("status"), "error": (s.get("error") or "")[:500]}
           for s in run.get("steps") or [] if s.get("status") in ("failed", "cancelled")]
    return {"run": run.get("id"), "flow": run.get("flow"), "status": run.get("status"), "error": run.get("error"),
            "failed_steps": bad[:20]}


def _outcome(event: dict, flow: str) -> str:
    """The run's id when it succeeded, else AirflowException saying why."""
    if event.get("status") == "succeeded":
        return str(event["run"])
    if event.get("status") == "lost":
        raise AirflowException(f"Cairn: the run {event.get('run')} of {flow} could not be followed: {event.get('error')}")
    steps = "; ".join(f"{s['step']} {s['status']}: {s['error']}" for s in event.get("failed_steps") or [])
    raise AirflowException(f"Cairn: the run {event.get('run')} of {flow} {event.get('status')}"
                           + (f": {event['error']}" if event.get("error") else "") + (f" ({steps})" if steps else ""))


class CairnFlowRunTrigger(BaseTrigger):
    """Follows a Cairn flow run until it ends, in the triggerer; cancels it when the deferred task is killed."""

    def __init__(self, run_id: str, cairn_conn_id: str = "cairn_default", poll_interval: float = 15.0,
                 cancel_on_kill: bool = True, max_errors: int = 20):
        super().__init__()
        self.run_id, self.cairn_conn_id = str(run_id), cairn_conn_id
        self.poll_interval, self.cancel_on_kill, self.max_errors = max(1.0, float(poll_interval)), cancel_on_kill, max_errors

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return ("cairn_airflow.flow_runs.CairnFlowRunTrigger",
                {"run_id": self.run_id, "cairn_conn_id": self.cairn_conn_id, "poll_interval": self.poll_interval,
                 "cancel_on_kill": self.cancel_on_kill, "max_errors": self.max_errors})

    def _get(self) -> dict:
        return CairnHook(self.cairn_conn_id).api("GET", _run_path(self.run_id))

    async def run(self) -> AsyncIterator[TriggerEvent]:
        errors = 0
        while True:
            try:
                run = await asyncio.to_thread(self._get)
                errors = 0
            except Exception as e:  # noqa: BLE001 - Cairn unreachable for a moment: look again, a while
                errors += 1
                self.log.warning("Cairn: run %s could not be read (%s/%s): %s", self.run_id, errors, self.max_errors, e)
                if errors >= self.max_errors or (isinstance(e, CairnError) and " 404 " in f" {e} "):
                    yield TriggerEvent({"run": self.run_id, "status": "lost", "error": str(e)[:500]})
                    return
                await asyncio.sleep(self.poll_interval)
                continue
            if run.get("status") in DONE:
                yield TriggerEvent(_summary(run))
                return
            await asyncio.sleep(self.poll_interval)

    async def on_kill(self) -> None:
        if self.cancel_on_kill:
            await asyncio.to_thread(_cancel, self.cairn_conn_id, self.run_id, self.log)


def _cancel(conn_id: str, run_id: str, log: Any) -> None:
    try:
        CairnHook(conn_id).api("POST", f"{_run_path(run_id)}/cancel")
        log.info("Cairn: cancelled the run %s", run_id)
    except CairnError as e:
        if " 409 " in f" {e} ":
            log.info("Cairn: the run %s had already finished", run_id)
        else:
            log.warning("Cairn: could not cancel the run %s: %s", run_id, e)


class CairnFlowRunOperator(BaseOperator):
    """Starts a Cairn flow (as the connection's Cairn identity; its steps run as the flow's owner) and, with ``wait``,
    waits for the run to end: the task fails when the run does. Returns the run's id."""

    template_fields: Sequence[str] = ("flow", "flow_params", "version")
    template_fields_renderers = {"flow_params": "json"}
    ui_color = "#e7f0ff"

    def __init__(self, *, flow: str, flow_params: Optional[dict] = None, version: Optional[Any] = None,
                 wait: bool = True, deferrable: bool = False, poll_interval: float = 15.0,
                 cancel_on_kill: bool = True, cairn_conn_id: str = "cairn_default", **kwargs: Any):
        super().__init__(**kwargs)
        self.flow, self.flow_params, self.version = flow, flow_params or {}, version
        self.wait, self.deferrable, self.cancel_on_kill = wait, deferrable, cancel_on_kill
        self.poll_interval, self.cairn_conn_id = max(1.0, float(poll_interval)), cairn_conn_id
        self.run_id: Optional[str] = None

    def execute(self, context: Any) -> str:
        hook = CairnHook(self.cairn_conn_id)
        body: dict[str, Any] = {"params": dict(self.flow_params or {})}
        if self.version not in (None, ""):
            body["version"] = self.version
        run = hook.api("POST", f"/v1/flows/{quote(self.flow, safe='')}/runs", json=body)
        self.run_id = str(run["id"])
        self.log.info("Cairn: started the run %s of the flow %s (version %s, %s)", self.run_id, self.flow,
                      run.get("version"), run.get("status"))
        if not self.wait:
            return self.run_id
        if self.deferrable:
            self.defer(trigger=CairnFlowRunTrigger(self.run_id, self.cairn_conn_id, self.poll_interval,
                                                   self.cancel_on_kill),
                       method_name="execute_complete",
                       timeout=self.execution_timeout if isinstance(self.execution_timeout, timedelta) else None)
        said = None
        while True:
            got = hook.api("GET", _run_path(self.run_id))
            if got.get("status") != said:
                said = got.get("status")
                self.log.info("Cairn: the run %s of %s is %s", self.run_id, self.flow, said)
            if said in DONE:
                return _outcome(_summary(got), self.flow)
            time.sleep(self.poll_interval)

    def execute_complete(self, context: Any, event: Optional[dict] = None) -> str:
        event = event or {}
        self.run_id = str(event.get("run") or self.run_id or "")
        self.log.info("Cairn: the run %s of %s is %s", self.run_id, self.flow, event.get("status"))
        return _outcome(event, self.flow)

    def on_kill(self) -> None:
        if self.cancel_on_kill and self.run_id:
            _cancel(self.cairn_conn_id, self.run_id, self.log)


class CairnFlowRunSensor(BaseSensorOperator):
    """Waits for a Cairn flow run to end: ``run_id`` (templated, e.g. from :class:`CairnFlowRunOperator` with
    ``wait=False``) or the latest run of ``flow``. True when it succeeded; the task fails when it failed, was cancelled
    or was skipped."""

    template_fields: Sequence[str] = ("run_id", "flow")
    ui_color = "#e7f0ff"

    def __init__(self, *, run_id: Optional[str] = None, flow: Optional[str] = None, deferrable: bool = False,
                 cairn_conn_id: str = "cairn_default", **kwargs: Any):
        super().__init__(**kwargs)
        if not run_id and not flow:
            raise ValueError("CairnFlowRunSensor needs run_id or flow")
        self.run_id, self.flow, self.deferrable, self.cairn_conn_id = run_id, flow, deferrable, cairn_conn_id

    def _which(self, hook: CairnHook) -> str:
        if self.run_id:
            return str(self.run_id)
        runs = hook.api("GET", f"/v1/flows/{quote(str(self.flow), safe='')}/runs", params={"limit": 1})
        if not runs:
            raise AirflowException(f"Cairn: the flow {self.flow} has no runs")
        return str(runs[0]["id"])

    def poke(self, context: Any) -> bool:
        hook = CairnHook(self.cairn_conn_id)
        run = hook.api("GET", _run_path(self._which(hook)))
        status = run.get("status")
        self.log.info("Cairn: the run %s of %s is %s", run.get("id"), run.get("flow"), status)
        if status in DONE:
            _outcome(_summary(run), str(run.get("flow")))
            return True
        return False

    def execute(self, context: Any) -> Any:
        if not self.deferrable:
            return super().execute(context)
        hook = CairnHook(self.cairn_conn_id)
        run_id = self._which(hook)
        if self.poke(context):
            return run_id
        self.defer(trigger=CairnFlowRunTrigger(run_id, self.cairn_conn_id, self.poke_interval, cancel_on_kill=False),
                   method_name="execute_complete", timeout=timedelta(seconds=self.timeout) if self.timeout else None)

    def execute_complete(self, context: Any, event: Optional[dict] = None) -> str:
        event = event or {}
        return _outcome(event, str(self.flow or event.get("flow") or "the flow"))
