"""Persistence for monitors, metric history, evaluations and per-monitor state."""

from __future__ import annotations

import uuid
import zlib
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, String, Table, and_, delete, select, update
from sqlalchemy.engine import Engine

from aegis.common.db import JSONText, metadata
from aegis.quality.models import Evaluation, Monitor, MonitorSpec

monitors = Table(
    "aegis_monitors",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("dataset_urn", String(1024), nullable=False, index=True),
    Column("type", String(32), nullable=False),
    Column("spec", JSONText, nullable=False),
    Column("status", String(16), nullable=False, default="TRAINING"),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("next_run_at_ms", BigInteger, nullable=True, index=True),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("created_by", String(512), nullable=True),
)

metric_points = Table(
    "aegis_metric_points",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("monitor_id", String(64), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False, index=True),
    Column("value", Float, nullable=True),
    Column("payload", JSONText, nullable=True),
    Column("excluded", Boolean, nullable=False, default=False),
)

evaluations = Table(
    "aegis_evaluations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("monitor_id", String(64), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False, index=True),
    Column("run_id", String(200), nullable=False, unique=True),
    Column("metric_value", Float, nullable=True),
    Column("expected_low", Float, nullable=True),
    Column("expected_high", Float, nullable=True),
    Column("status", String(16), nullable=False),
    Column("score", Float, nullable=True),
    Column("message", String(2000), nullable=False),
    Column("details", JSONText, nullable=True),
    Column("feedback", String(16), nullable=True),
    Column("emitted", Boolean, nullable=False, default=False),
    Column("metric_point_id", Integer, nullable=True),
)

monitor_state = Table(
    "aegis_monitor_state",
    metadata,
    Column("monitor_id", String(64), primary_key=True),
    Column("consecutive_failures", Integer, nullable=False, default=0),
    Column("open_incident_urn", String(512), nullable=True),
    Column("reference", JSONText, nullable=True),
)


def _iso(ts_ms: Optional[int] = None) -> str:
    dt = datetime.now(timezone.utc) if ts_ms is None else datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.isoformat()


def _eval_from_row(r: Any) -> Evaluation:
    m = r._mapping
    return Evaluation(
        id=m["id"], monitor_id=m["monitor_id"], ts=_iso(m["ts_ms"]), ts_ms=m["ts_ms"], run_id=m["run_id"],
        metric_value=m["metric_value"], expected_low=m["expected_low"], expected_high=m["expected_high"],
        status=m["status"], score=m["score"], message=m["message"], details=m["details"] or {},
        feedback=m["feedback"], emitted=m["emitted"],
    )


class QualityStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ---- monitors -----------------------------------------------------------------
    def create_monitor(self, spec: MonitorSpec, actor: Optional[str], monitor_id: Optional[str] = None) -> Monitor:
        now = _iso()
        mid = monitor_id or f"mon_{uuid.uuid4().hex[:16]}"
        with self.engine.begin() as conn:
            conn.execute(monitors.insert().values(
                id=mid, name=spec.name, dataset_urn=spec.dataset_urn, type=spec.type,
                spec=spec.model_dump(mode="json"), status="TRAINING", enabled=spec.enabled,
                next_run_at_ms=None, created_at=now, updated_at=now, created_by=actor,
            ))
            conn.execute(monitor_state.insert().values(monitor_id=mid, consecutive_failures=0))
        return self.get_monitor(mid)  # type: ignore[return-value]

    def update_monitor(self, monitor_id: str, spec: MonitorSpec) -> Optional[Monitor]:
        with self.engine.begin() as conn:
            res = conn.execute(update(monitors).where(monitors.c.id == monitor_id).values(
                name=spec.name, dataset_urn=spec.dataset_urn, type=spec.type,
                spec=spec.model_dump(mode="json"), enabled=spec.enabled, updated_at=_iso(),
                next_run_at_ms=None,
            ))
        return self.get_monitor(monitor_id) if res.rowcount else None

    def delete_monitor(self, monitor_id: str) -> bool:
        with self.engine.begin() as conn:
            res = conn.execute(delete(monitors).where(monitors.c.id == monitor_id))
            conn.execute(delete(monitor_state).where(monitor_state.c.monitor_id == monitor_id))
        return bool(res.rowcount)

    def set_status(self, monitor_id: str, status: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(monitors).where(monitors.c.id == monitor_id).values(status=status))

    def set_next_run(self, monitor_id: str, next_run_at_ms: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(monitors).where(monitors.c.id == monitor_id).values(next_run_at_ms=next_run_at_ms))

    def _monitor_from_row(self, r: Any, with_last: bool = True) -> Monitor:
        m = r._mapping
        spec = dict(m["spec"])
        spec["enabled"] = m["enabled"]
        monitor = Monitor(
            id=m["id"], status="PAUSED" if not m["enabled"] else m["status"],
            created_at=m["created_at"], updated_at=m["updated_at"], created_by=m["created_by"],
            next_run_at_ms=m["next_run_at_ms"], **spec,
        )
        if with_last:
            last = self.evaluations(m["id"], limit=1)
            monitor.last_evaluation = last[0] if last else None
        return monitor

    def get_monitor(self, monitor_id: str) -> Optional[Monitor]:
        with self.engine.connect() as conn:
            row = conn.execute(select(monitors).where(monitors.c.id == monitor_id)).first()
        return self._monitor_from_row(row) if row else None

    def list_monitors(self, dataset_urn: Optional[str] = None) -> list[Monitor]:
        stmt = select(monitors).order_by(monitors.c.created_at)
        if dataset_urn:
            stmt = stmt.where(monitors.c.dataset_urn == dataset_urn)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [self._monitor_from_row(r) for r in rows]

    def due_monitors(self, now_ms: int, partition: int = 0, partitions: int = 1) -> list[Monitor]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(monitors).where(and_(
                monitors.c.enabled.is_(True),
                (monitors.c.next_run_at_ms.is_(None)) | (monitors.c.next_run_at_ms <= now_ms),
            ))).fetchall()
        out = [self._monitor_from_row(r, with_last=False) for r in rows]
        if partitions > 1:
            out = [m for m in out if zlib.crc32(m.id.encode()) % partitions == partition]
        return out

    # ---- metric history -----------------------------------------------------------
    def add_point(self, monitor_id: str, ts_ms: int, value: Optional[float], payload: Optional[dict] = None) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(metric_points.insert().values(
                monitor_id=monitor_id, ts_ms=ts_ms, value=value, payload=payload, excluded=False))
            return int(res.inserted_primary_key[0])

    def history(self, monitor_id: str, before_ms: Optional[int] = None, limit: int = 500) -> list[dict]:
        stmt = (select(metric_points)
                .where(and_(metric_points.c.monitor_id == monitor_id, metric_points.c.excluded.is_(False)))
                .order_by(metric_points.c.ts_ms.desc()).limit(limit))
        if before_ms is not None:
            stmt = stmt.where(metric_points.c.ts_ms < before_ms)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [dict(r._mapping) for r in reversed(rows)]

    # ---- evaluations --------------------------------------------------------------
    def add_evaluation(self, ev: Evaluation, metric_point_id: Optional[int]) -> bool:
        """Insert idempotently on ``run_id``; returns False if it already existed."""
        with self.engine.begin() as conn:
            exists = conn.execute(select(evaluations.c.id).where(evaluations.c.run_id == ev.run_id)).first()
            if exists:
                return False
            conn.execute(evaluations.insert().values(
                id=ev.id, monitor_id=ev.monitor_id, ts_ms=ev.ts_ms, run_id=ev.run_id,
                metric_value=ev.metric_value, expected_low=ev.expected_low, expected_high=ev.expected_high,
                status=ev.status, score=ev.score, message=ev.message[:2000], details=ev.details,
                feedback=None, emitted=ev.emitted, metric_point_id=metric_point_id,
            ))
        return True

    def mark_emitted(self, evaluation_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(evaluations).where(evaluations.c.id == evaluation_id).values(emitted=True))

    def unemitted(self, limit: int = 500) -> list[Evaluation]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(evaluations).where(evaluations.c.emitted.is_(False))
                                .order_by(evaluations.c.ts_ms).limit(limit)).fetchall()
        return [_eval_from_row(r) for r in rows]

    def evaluations(self, monitor_id: str, limit: int = 100) -> list[Evaluation]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(evaluations).where(evaluations.c.monitor_id == monitor_id)
                                .order_by(evaluations.c.ts_ms.desc()).limit(limit)).fetchall()
        return [_eval_from_row(r) for r in rows]

    def get_evaluation(self, evaluation_id: str) -> Optional[Evaluation]:
        with self.engine.connect() as conn:
            row = conn.execute(select(evaluations).where(evaluations.c.id == evaluation_id)).first()
        return _eval_from_row(row) if row else None

    def set_feedback(self, evaluation_id: str, feedback: str) -> Optional[Evaluation]:
        """EXPECTED feedback removes the point from future baselines."""
        with self.engine.begin() as conn:
            row = conn.execute(select(evaluations).where(evaluations.c.id == evaluation_id)).first()
            if not row:
                return None
            conn.execute(update(evaluations).where(evaluations.c.id == evaluation_id).values(feedback=feedback))
            point_id = row._mapping["metric_point_id"]
            if point_id is not None:
                # "EXPECTED" = this was a legitimate change → keep it in the baseline so the
                # model adapts. "REAL" = genuine incident → exclude it so it does not poison
                # the baseline.
                conn.execute(update(metric_points).where(metric_points.c.id == point_id)
                             .values(excluded=(feedback == "REAL")))
        return self.get_evaluation(evaluation_id)

    # ---- state --------------------------------------------------------------------
    def get_state(self, monitor_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(monitor_state).where(monitor_state.c.monitor_id == monitor_id)).first()
        if not row:
            return {"monitor_id": monitor_id, "consecutive_failures": 0, "open_incident_urn": None, "reference": None}
        return dict(row._mapping)

    def save_state(self, monitor_id: str, **values: Any) -> None:
        with self.engine.begin() as conn:
            exists = conn.execute(select(monitor_state.c.monitor_id)
                                  .where(monitor_state.c.monitor_id == monitor_id)).first()
            if exists:
                conn.execute(update(monitor_state).where(monitor_state.c.monitor_id == monitor_id).values(**values))
            else:
                conn.execute(monitor_state.insert().values(monitor_id=monitor_id, **{
                    "consecutive_failures": 0, **values}))
