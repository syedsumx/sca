"""Quality gates: let a DAG go on only when Cairn says its data is good.

Cairn's status for a dataset combines its monitors (volume, freshness, schema, drift, ...) and other
tools' checks (dbt tests, Great Expectations, Soda): PASS, WARN, FAIL, ERROR, TRAINING or UNMONITORED.

* :class:`CairnQualityGateOperator` checks once and fails the task (no retries) when a dataset is not
  good enough, naming what failed: put it before publishing.
* :class:`CairnQualitySensor` waits until the datasets are good (e.g. after a load, until the monitors
  have seen it), in reschedule mode by default, so it holds no worker slot.

``allow`` is the statuses that pass (PASS and WARN by default); ``allow_unmonitored`` lets datasets with
no checks pass (False by default: a gate on unchecked data is no gate); ``fail_on_incident`` also
refuses datasets with open incidents (True by default).
"""

from __future__ import annotations

from typing import Sequence

from airflow.exceptions import AirflowFailException
from airflow.models import BaseOperator
from airflow.sensors.base import BaseSensorOperator

from cairn_airflow.hooks import CairnHook


def _verdict(hook: CairnHook, datasets: Sequence[str], allow: Sequence[str], allow_unmonitored: bool,
             fail_on_incident: bool) -> tuple[bool, list[str]]:
    problems = []
    for d in datasets:
        h = hook.dataset_health(d)
        status = h.get("status", "ERROR")
        if status == "UNMONITORED" and not allow_unmonitored:
            problems.append(f"{d}: no checks (UNMONITORED)")
        elif status not in allow and status != "UNMONITORED":
            failing = [m["monitor"]["name"] for m in h.get("monitors") or []
                       if (m.get("last_evaluation") or {}).get("status") not in (*allow, None)]
            failing += [x.get("name", "check") for x in h.get("external") or [] if x.get("status") not in allow]
            problems.append(f"{d}: {status}" + (f" ({', '.join(failing[:5])})" if failing else ""))
        if fail_on_incident and h.get("open_incidents"):
            problems.append(f"{d}: {h['open_incidents']} open incident(s)")
    return not problems, problems


class CairnQualityGateOperator(BaseOperator):
    template_fields: Sequence[str] = ("datasets",)
    ui_color = "#2f9e44"

    def __init__(self, *, datasets: Sequence[str], allow: Sequence[str] = ("PASS", "WARN"),
                 allow_unmonitored: bool = False, fail_on_incident: bool = True, cairn_conn_id: str = "cairn_default",
                 **kwargs):
        super().__init__(**kwargs)
        self.datasets, self.allow = list(datasets), tuple(allow)
        self.allow_unmonitored, self.fail_on_incident, self.cairn_conn_id = allow_unmonitored, fail_on_incident, cairn_conn_id

    def execute(self, context) -> list[str]:
        ok, problems = _verdict(CairnHook(self.cairn_conn_id), self.datasets, self.allow, self.allow_unmonitored,
                                self.fail_on_incident)
        if not ok:
            raise AirflowFailException("Cairn quality gate: " + "; ".join(problems))
        self.log.info("Cairn quality gate passed for %s", ", ".join(self.datasets))
        return self.datasets


class CairnQualitySensor(BaseSensorOperator):
    template_fields: Sequence[str] = ("datasets",)
    ui_color = "#40c057"

    def __init__(self, *, datasets: Sequence[str], allow: Sequence[str] = ("PASS", "WARN"),
                 allow_unmonitored: bool = False, fail_on_incident: bool = True, cairn_conn_id: str = "cairn_default",
                 mode: str = "reschedule", **kwargs):
        super().__init__(mode=mode, **kwargs)
        self.datasets, self.allow = list(datasets), tuple(allow)
        self.allow_unmonitored, self.fail_on_incident, self.cairn_conn_id = allow_unmonitored, fail_on_incident, cairn_conn_id

    def poke(self, context) -> bool:
        ok, problems = _verdict(CairnHook(self.cairn_conn_id), self.datasets, self.allow, self.allow_unmonitored,
                                self.fail_on_incident)
        if not ok:
            self.log.info("Waiting for Cairn: %s", "; ".join(problems))
        return ok
