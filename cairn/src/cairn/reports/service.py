"""Reports: is the catalog used, and is the estate getting governed?

A snapshot is computed nightly (and on demand) into ``cairn_report_snapshots``, one row per day:

* **coverage** by domain and platform: the share of datasets with a description, an owner, a
  classification, certification and at least one monitor;
* **adoption**: weekly active users and asset views, access requests submitted and approved, the
  median time to a decision;
* **quality**: open incidents, monitored share, mean time to resolve over 30 days;
* **governance**: personas, purposes, active grants and grants expiring within 7 days.

Every coverage figure can be drilled into: :meth:`ReportService.assets` lists the datasets behind it.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import BigInteger, Column, String, Table, and_, func, select
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.governance.attributes import resource_from_aspects

logger = logging.getLogger(__name__)

report_snapshots = Table(
    "cairn_report_snapshots", metadata,
    Column("day", String(10), primary_key=True),         # YYYY-MM-DD (UTC)
    Column("data", JSONText, nullable=False),
    Column("computed_ms", BigInteger, nullable=False),
)

DAY_MS = 86_400_000
FACETS = ("description", "owner", "classification", "certified", "monitored")
SCAN_ASPECTS = ["datasetProperties", "editableDatasetProperties", "ownership", "globalTags", "glossaryTerms",
                "domains", "schemaMetadata", "editableSchemaMetadata", "structuredProperties", "status"]


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def asset_facts(urn: str, aspects: dict, monitored: set[str]) -> dict:
    r = resource_from_aspects(urn, aspects)
    desc = ((aspects.get("editableDatasetProperties") or {}).get("description")
            or (aspects.get("datasetProperties") or {}).get("description") or "").strip()
    domain = (r.get("domains") or [None])[0]
    return {"urn": urn, "domain": domain, "platform": r.get("platform") or "unknown",
            "description": bool(desc), "owner": bool(r.get("owners")), "classification": bool(r.get("classification")),
            "certified": (r.get("certification") or "").upper() == "CERTIFIED", "monitored": urn in monitored}


def _coverage(facts: list[dict], key: Callable[[dict], Optional[str]]) -> list[dict]:
    groups: dict[Optional[str], list[dict]] = {}
    for f in facts:
        groups.setdefault(key(f), []).append(f)
    rows = []
    for k, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        n = len(items)
        rows.append({"key": k, "assets": n, **{f: round(100 * sum(1 for i in items if i[f]) / n, 1) for f in FACETS}})
    return rows


def incident_durations(evals: Iterable[tuple[int, str]], since_ms: int) -> list[float]:
    """Hours from the first FAIL to the next PASS, for incidents that resolved after ``since_ms``.
    ``evals`` are (ts_ms, status) in time order."""
    out, opened = [], None
    for ts, status in evals:
        if status == "FAIL" and opened is None:
            opened = ts
        elif status == "PASS" and opened is not None:
            if ts >= since_ms:
                out.append((ts - opened) / 3_600_000)
            opened = None
    return out


class ReportService:
    def __init__(self, engine: Engine, graph, quality_store, personas=None):
        self.engine = engine
        self.graph = graph
        self.quality_store = quality_store
        self.personas = personas

    # ---- inputs -----------------------------------------------------------------------
    def _facts(self) -> list[dict]:
        monitored = {m.dataset_urn for m in self.quality_store.list_monitors()}
        out = []
        for row in self.graph.scroll_entities("dataset", SCAN_ASPECTS):
            if (row.get("status") or {}).get("removed"):
                continue
            out.append(asset_facts(row["urn"], row, monitored))
        return out

    def _adoption(self, now: int) -> dict:
        from cairn.collab.service import views
        from cairn.marketplace.service import access_requests

        week = now - 7 * DAY_MS
        month = now - 30 * DAY_MS
        with self.engine.connect() as conn:
            wau = conn.execute(select(func.count(func.distinct(views.c.actor))).where(views.c.ts_ms >= week)).scalar() or 0
            mau = conn.execute(select(func.count(func.distinct(views.c.actor))).where(views.c.ts_ms >= month)).scalar() or 0
            viewed = conn.execute(select(func.count()).select_from(views).where(views.c.ts_ms >= week)).scalar() or 0
            reqs = conn.execute(select(access_requests.c.state, access_requests.c.created_at_ms,
                                       access_requests.c.updated_at_ms)
                                .where(access_requests.c.created_at_ms >= month)).all()
        decided = [(r.updated_at_ms - r.created_at_ms) / 3_600_000 for r in reqs if r.state in ("APPROVED", "REJECTED")]
        return {"weekly_active_users": int(wau), "monthly_active_users": int(mau), "asset_views_7d": int(viewed),
                "requests_30d": len(reqs), "approved_30d": sum(1 for r in reqs if r.state == "APPROVED"),
                "pending_requests": sum(1 for r in reqs if r.state == "PENDING"),
                "median_hours_to_decision": round(median(decided), 2) if decided else None}

    def _quality(self, facts: list[dict], now: int) -> dict:
        from cairn.quality.store import evaluations, monitor_state

        since = now - 30 * DAY_MS
        with self.engine.connect() as conn:
            open_incidents = conn.execute(select(func.count()).select_from(monitor_state)
                                          .where(monitor_state.c.open_incident_urn.is_not(None))).scalar() or 0
            rows = conn.execute(select(evaluations.c.monitor_id, evaluations.c.ts_ms, evaluations.c.status)
                                .where(evaluations.c.ts_ms >= since - 7 * DAY_MS)
                                .order_by(evaluations.c.monitor_id, evaluations.c.ts_ms)).all()
        by_monitor: dict[str, list[tuple[int, str]]] = {}
        for r in rows:
            by_monitor.setdefault(r.monitor_id, []).append((r.ts_ms, r.status))
        durations = [d for evs in by_monitor.values() for d in incident_durations(evs, since)]
        n = len(facts)
        return {"open_incidents": int(open_incidents),
                "monitored_pct": round(100 * sum(1 for f in facts if f["monitored"]) / n, 1) if n else 0.0,
                "resolved_30d": len(durations),
                "mean_hours_to_resolve": round(sum(durations) / len(durations), 2) if durations else None}

    def _governance(self, now: int) -> dict:
        if self.personas is None:
            return {}
        from cairn.governance.personas import grants_table

        with self.engine.connect() as conn:
            live = and_(grants_table.c.revoked.is_(False),
                        (grants_table.c.expires_at_ms.is_(None)) | (grants_table.c.expires_at_ms > now))
            active = conn.execute(select(func.count()).select_from(grants_table).where(live)).scalar() or 0
            expiring = conn.execute(select(func.count()).select_from(grants_table).where(and_(
                live, grants_table.c.expires_at_ms.is_not(None), grants_table.c.expires_at_ms <= now + 7 * DAY_MS))
            ).scalar() or 0
        return {"personas": len(self.personas.list_personas()), "purposes": len(self.personas.list_purposes()),
                "active_grants": int(active), "grants_expiring_7d": int(expiring)}

    # ---- snapshots ----------------------------------------------------------------------
    def compute(self, now_ms: Optional[int] = None) -> dict:
        now = now_ms or int(time.time() * 1000)
        facts = self._facts()
        n = len(facts)
        data: dict[str, Any] = {
            "day": _day(now), "computed_ms": now, "assets": n,
            "coverage": {
                "overall": {f: round(100 * sum(1 for i in facts if i[f]) / n, 1) if n else 0.0 for f in FACETS},
                "by_domain": _coverage(facts, lambda f: f["domain"]),
                "by_platform": _coverage(facts, lambda f: f["platform"]),
            },
            "adoption": self._adoption(now),
            "quality": self._quality(facts, now),
            "governance": self._governance(now),
        }
        with self.engine.begin() as conn:
            conn.execute(report_snapshots.delete().where(report_snapshots.c.day == data["day"]))
            conn.execute(report_snapshots.insert().values(day=data["day"], data=data, computed_ms=now))
        return data

    def latest(self) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(report_snapshots).order_by(report_snapshots.c.day.desc()).limit(1)).first()
        return r.data if r else None

    def history(self, days: int = 90) -> list[dict]:
        """Headline figures per day, oldest first, for trend lines."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(report_snapshots).order_by(report_snapshots.c.day.desc()).limit(days)).all()
        return [{"day": r.day, "assets": r.data["assets"], **r.data["coverage"]["overall"],
                 "weekly_active_users": r.data["adoption"]["weekly_active_users"],
                 "open_incidents": r.data["quality"]["open_incidents"]} for r in reversed(rows)]

    def due(self, now_ms: int) -> bool:
        latest = self.latest()
        return latest is None or latest["day"] != _day(now_ms)

    def assets(self, missing: Optional[str] = None, domain: Optional[str] = None, platform: Optional[str] = None,
               has: Optional[str] = None, limit: int = 500) -> list[dict]:
        """The datasets behind a figure, e.g. ``missing=owner&domain=urn:li:domain:finance``."""
        for f in (missing, has):
            if f is not None and f not in FACETS:
                raise ValueError(f"unknown facet {f!r}; one of {', '.join(FACETS)}")
        out = []
        for f in self._facts():
            if domain is not None and (f["domain"] or "") != domain:
                continue
            if platform is not None and f["platform"] != platform:
                continue
            if missing and f[missing]:
                continue
            if has and not f[has]:
                continue
            out.append(f)
            if len(out) >= limit:
                break
        return out


def to_csv(snapshot: dict) -> str:
    """Coverage by domain and by platform, one row per group, for spreadsheets."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["day", "group", "key", "assets", *[f"{f}_pct" for f in FACETS]])
    for group in ("by_domain", "by_platform"):
        for r in snapshot["coverage"][group]:
            w.writerow([snapshot["day"], group.removeprefix("by_"), r["key"] or "(none)", r["assets"],
                        *[r[f] for f in FACETS]])
    return buf.getvalue()
