"""What changed in the lakehouse, and pipelines that failed.

**Dataset changes.** Every time a pipeline finishes writing a dataset, Cairn records it: Spark,
Airflow tasks and Trino queries through OpenLineage (a completed run's outputs), and dbt runs (the
models, seeds and snapshots they built). ``GET /v1/datasets/changes`` serves them in order, by a
cursor, for the datasets the caller may see, so an orchestrator can run a pipeline when its inputs
change (Airflow's ``CairnDatasetChangedTrigger``). Changes are kept 30 days.

**Failed pipelines.** When a run fails, each dataset the job writes gets an incident ("[Pipeline] ...
failed") and its owners, with the job's, are told. The incident is resolved by the job's next
successful run. One incident per job and dataset: a failing job does not raise a new one each run.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, delete, func, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

KEEP_MS = 30 * 86400 * 1000
MAX_URNS = 100

dataset_changes = Table(
    "cairn_dataset_changes", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("urn", String(512), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False),
    Column("source", String(32), nullable=False),       # openlineage | dbt
    Column("job", String(512), nullable=True),
    Column("run", String(256), nullable=True),
)


class DatasetChanges:
    def __init__(self, engine):
        self.engine = engine
        self._n = 0

    def record(self, urns: list[str], source: str, job: Optional[str] = None, run: Optional[str] = None) -> None:
        urns = sorted({u for u in urns if u})
        if not urns:
            return
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            conn.execute(dataset_changes.insert(), [{"urn": u[:512], "ts_ms": now, "source": source[:32],
                                                     "job": (job or "")[:512] or None, "run": (run or "")[:256] or None}
                                                    for u in urns])
            self._n += 1
            if self._n % 500 == 0:
                conn.execute(delete(dataset_changes).where(dataset_changes.c.ts_ms < now - KEEP_MS))

    def head(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.max(dataset_changes.c.seq))).scalar() or 0)

    def since(self, urns: list[str], after: int, limit: int = 500) -> list[dict]:
        q = (select(dataset_changes).where(dataset_changes.c.urn.in_(urns), dataset_changes.c.seq > after)
             .order_by(dataset_changes.c.seq).limit(limit))
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]


def _incident_urn(job: str, dataset: str) -> str:
    return "urn:li:incident:pipeline-" + hashlib.sha256(f"{job}|{dataset}".encode()).hexdigest()[:24]


class PipelineHealth:
    """Incidents and notifications for failed pipeline runs, resolved by the next successful one."""

    def __init__(self, graph, notify=None, changes: Optional[DatasetChanges] = None, actor: str = "urn:li:corpuser:cairn"):
        self.graph, self.notify, self.changes, self.actor = graph, notify, changes, actor

    def on_run(self, job_urn: str, state: str, outputs: list[str], run_id: Optional[str] = None,
               message: str = "") -> None:
        """Called for each OpenLineage run event (state COMPLETE, FAIL, ABORT, ...)."""
        state = (state or "").upper()
        if state == "COMPLETE":
            if self.changes is not None and outputs:
                self.changes.record(outputs, "openlineage", job_urn, run_id)
            self._resolve(job_urn)
        elif state == "FAIL":
            self._fail(job_urn, outputs, run_id, message)

    def _targets(self, job_urn: str, outputs: list[str]) -> list[str]:
        known = (self.graph.get_aspect(job_urn, "dataJobInputOutput") or {}).get("outputDatasets") or []
        return sorted(set(outputs) | set(known))

    def _fail(self, job_urn: str, outputs: list[str], run_id: Optional[str], message: str) -> None:
        from cairn.common.proposals import make_mcp

        now = int(time.time() * 1000)
        name = job_urn.rsplit(",", 1)[-1].rstrip(")")
        mcps, raised = [], []
        for ds in self._targets(job_urn, outputs):
            urn = _incident_urn(job_urn, ds)
            cur = self.graph.get_aspect(urn, "incidentInfo") or {}
            if (cur.get("status") or {}).get("state") == "ACTIVE":
                continue  # already raised: not again for every failing run
            mcps.append(make_mcp(urn, "incidentInfo", {
                "type": "OPERATIONAL",
                "title": f"[Pipeline] {name} failed",
                "description": (f"The pipeline {name} that writes this dataset failed"
                                + (f" (run {run_id})" if run_id else "") + (f": {message}" if message else ".")
                                + " The data may be stale or incomplete.")[:4000],
                "entities": [ds, job_urn],
                "priority": 1,
                "status": {"state": "ACTIVE", "stage": "TRIAGE", "message": "Raised by Cairn: the pipeline failed",
                           "lastUpdated": {"time": now, "actor": self.actor}},
                "source": {"type": "MANUAL"},
                "customProperties": {"cairn_pipeline": job_urn},
                "startedAt": now,
                "created": {"time": now, "actor": self.actor},
            }))
            raised.append(ds)
        if mcps:
            self.graph.emit(mcps)
        if self.notify is not None and raised:
            from cairn.notify.hub import Event

            try:
                self.notify.publish(Event(type="pipeline.run_failed", title=f"Pipeline failed: {name}",
                                          body=f"{name} failed; {len(raised)} dataset(s) it writes may be stale.",
                                          entity_urn=job_urn, include_owners=True,
                                          payload={"job": job_urn, "run": run_id, "datasets": raised}))
                for ds in raised:
                    self.notify.publish(Event(type="pipeline.run_failed", title=f"Stale data: {name} failed",
                                              body=f"The pipeline {name} that writes this dataset failed.",
                                              entity_urn=ds, include_owners=True, payload={"job": job_urn, "run": run_id}))
            except Exception:  # noqa: BLE001 - the incidents are raised; a notification failure must not undo that
                logger.exception("could not notify about the failed pipeline %s", job_urn)

    def _resolve(self, job_urn: str) -> None:
        from cairn.common.proposals import make_mcp

        now = int(time.time() * 1000)
        mcps = []
        for ds in self._targets(job_urn, []):
            urn = _incident_urn(job_urn, ds)
            cur = self.graph.get_aspect(urn, "incidentInfo") or {}
            if (cur.get("status") or {}).get("state") == "ACTIVE":
                mcps.append(make_mcp(urn, "incidentInfo", {**cur, "status": {
                    "state": "RESOLVED", "stage": "FIXED", "message": "Auto-resolved: the pipeline succeeded",
                    "lastUpdated": {"time": now, "actor": self.actor}}}))
        if mcps:
            self.graph.emit(mcps)


def build_changes_router(changes: DatasetChanges, current_principal, authorize, resolve=None):
    """``resolve(catalog, schema, table)`` names a lakehouse table's dataset (as Trino names it), so callers
    can give ``catalog.schema.table`` instead of a URN."""
    from fastapi import APIRouter, Depends, HTTPException, Query

    router = APIRouter(prefix="/v1/datasets", tags=["pipelines"])

    def to_urn(name: str) -> str:
        if name.startswith("urn:li:"):
            return name
        parts = name.split(".")
        if resolve is None or len(parts) != 3 or not all(parts):
            raise HTTPException(422, f"{name!r}: a dataset URN, or a lakehouse table as catalog.schema.table")
        return resolve(*parts)

    @router.get("/resolve")
    def resolve_name(name: str, p=Depends(current_principal)):
        """The dataset URN of ``catalog.schema.table`` (as Trino, Spark and dbt write it)."""
        urn = to_urn(name)
        authorize(p, "VIEW_ENTITY_PAGE", urn)
        return {"name": name, "urn": urn}

    @router.get("/changes")
    def list_changes(urn: list[str] = Query(...), after: Optional[int] = None, limit: int = 500,
                     p=Depends(current_principal)):
        """Changes to these datasets (URNs, or catalog.schema.table) after ``after`` (a cursor from an
        earlier answer). Without ``after``, none: just the cursor to watch from."""
        if not urn or len(urn) > MAX_URNS:
            raise HTTPException(422, f"name 1 to {MAX_URNS} datasets")
        urn = [to_urn(u) for u in urn]
        for u in urn:
            authorize(p, "VIEW_ENTITY_PAGE", u)
        if after is None:
            return {"changes": [], "cursor": changes.head()}
        rows = changes.since(urn, max(0, after), max(1, min(limit, 1000)))
        return {"changes": [{"seq": r["seq"], "urn": r["urn"], "ts": r["ts_ms"], "source": r["source"],
                             "job": r["job"], "run": r["run"]} for r in rows],
                "cursor": rows[-1]["seq"] if rows else max(0, after)}

    return router
