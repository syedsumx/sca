"""Quality gates: let a DAG go on only when Cairn says its data is good.

Cairn's status for a dataset combines its monitors (volume, freshness, schema, drift, ...) and other
tools' checks (dbt tests, Great Expectations, Soda): PASS, WARN, FAIL, ERROR, TRAINING or UNMONITORED.

* :class:`CairnQualityGateOperator` checks once and fails the task (no retries) when a dataset is not
  good enough, naming what failed: put it before publishing.
* :class:`CairnQualitySensor` waits until the datasets are good (e.g. after a load, until the monitors
  have seen it), in reschedule mode by default, so it holds no worker slot.
* :class:`CairnCheckOperator` checks a measure of a query on Trino (its row count, or a column of its first row)
  against a value, as the connection's Cairn user: the same check as a Cairn flow's ``check`` step.

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
from cairn_airflow.operators import CairnTrinoOperator

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


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


class CairnCheckOperator(CairnTrinoOperator):
    """Fails (without retries) unless ``measure`` of the SQL's result holds ``op value``: ``rows`` (how many rows the
    last statement returned, at most 1000 counted) or a column of its first row. Runs on Trino as the ``cairn``
    connection's user, like :class:`~cairn_airflow.operators.CairnTrinoOperator`."""

    ui_color = "#2f9e44"

    def __init__(self, *, sql: str, op: str, value: float, measure: str = "rows", **kwargs):
        if op not in OPS:
            raise ValueError(f"op is one of {', '.join(OPS)}")
        super().__init__(sql=sql, **kwargs)
        self.op, self.value, self.measure = op, float(value), measure

    def execute(self, context) -> dict:
        hook = CairnHook(self.cairn_conn_id)
        conn, user = hook.trino_connection(self.schema, self.token_ttl)
        self.log.info("Checking on Trino as %s", user)
        try:
            from cairn_airflow.operators import split_statements

            cur = conn.cursor()
            rows, names = [], []
            for stmt in split_statements(self.sql):
                cur.execute(stmt)
                rows = cur.fetchmany(1000)
                names = [d[0] for d in (cur.description or [])]
        finally:
            conn.close()
        if self.measure == "rows":
            got = float(len(rows))
        elif self.measure in names and rows:
            try:
                got = float(rows[0][names.index(self.measure)])
            except (TypeError, ValueError):
                raise AirflowFailException(f"{self.measure} is not a number: {rows[0][names.index(self.measure)]!r}")
        else:
            raise AirflowFailException(f"the query has no column {self.measure} (it has {', '.join(names) or 'no rows'})")
        if not OPS[self.op](got, self.value):
            raise AirflowFailException(f"check failed: {self.measure} is {got:g}, not {self.op} {self.value:g}")
        self.log.info("check passed: %s is %g (%s %g)", self.measure, got, self.op, self.value)
        return {"measure": self.measure, "value": got, "op": self.op, "threshold": self.value}
