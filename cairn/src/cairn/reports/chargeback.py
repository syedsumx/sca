"""Chargeback: a monthly statement of what each cost centre used, from every cost Cairn knows, frozen when issued.

**Cost centres** (``POST /v1/chargeback/cost-centres``, ``MANAGE_SYSTEM_OPERATIONS``) have a ``code`` (as finance
knows it, e.g. ``CC-4100``), a ``name`` and the ``teams`` (groups) whose use they pay for. A team belongs to one
cost centre.

**A statement** (``POST /v1/chargeback/statements`` with ``{"month": "2026-10"}``) charges each cost of the month, in
UTC, to cost centres:

* **SQL** (Trino, Cairn's warehouse): each query's CPU and scanned bytes at Cairn's Trino rates
  (``CAIRN_COST_TRINO_USD_PER_CPU_HOUR``, ``CAIRN_COST_TRINO_USD_PER_TB_SCANNED``), to the cost centres of the person
  who ran it (split evenly when their teams pay from several);
* **AI**: each model call's cost (``cairn_ai_usage``), to the cost centres of the person it was made for;
* **Tables** (storage and engine compute from the warehouse collectors and Spark runs, ``cairn_asset_costs``): to the
  cost centres of the table's owners in the catalog (a group's own, or a person's teams), split evenly;
* **Runs** charged to no table (warehouse node-hours, Spark runs that wrote nothing): to the cost centres of the
  run's owner where the catalog names one, else unallocated.

What cannot be placed (nobody in a cost centre, no owner) is a line of its own, **unallocated**, with the reason, so
a statement always adds up to everything the month cost. Nothing is counted twice: these are the same figures the
cost report and budgets use.

A statement is **frozen** when issued: its lines and totals are kept, audited (``chargeback.issued``) and do not
change as late figures arrive; issuing the month again makes a new version, the old one kept. ``GET …/statements/<id>``
(``.csv`` with ``?format=csv``) shows it: to operators whole, and to the members of a cost centre's teams their own
lines only.
"""

from __future__ import annotations

import calendar
import csv
import io
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, insert, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
GROUP = re.compile(r"^urn:li:corpGroup:[^\s]{1,250}$")
UNALLOCATED = "unallocated"

cost_centres = Table(
    "cairn_cost_centres", metadata,
    Column("code", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("teams", Text, nullable=False),                   # JSON list of group urns
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
chargeback_statements = Table(
    "cairn_chargeback_statements", metadata,
    Column("id", String(32), primary_key=True),
    Column("month", String(7), nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("issued_by", String(256), nullable=False),
    Column("issued_ms", BigInteger, nullable=False),
    Column("total_usd", Text, nullable=False),               # as text: exact as issued
    Column("doc", Text, nullable=False),                     # JSON: lines, per cost centre and source
)


class ChargebackError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def month_bounds(month: str) -> tuple[int, int, str, str]:
    """(start ms, end ms, first day, last day) of a calendar month in UTC."""
    m = MONTH.match(month or "")
    if not m:
        raise ChargebackError("month: YYYY-MM")
    y, mo = int(m.group(1)), int(m.group(2))
    last = calendar.monthrange(y, mo)[1]
    start = datetime(y, mo, 1, tzinfo=timezone.utc)
    end = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), f"{month}-01", f"{month}-{last:02d}"


class Chargeback:
    def __init__(self, engine, groups_of: Callable[[str], list[str]], graph=None, audit=None):
        """``groups_of(user urn)``: a person's groups. ``graph``: the catalog, for tables' and runs' owners."""
        self.engine, self.groups_of, self.graph, self.audit = engine, groups_of, graph, audit

    # ---- cost centres -----------------------------------------------------------------------------
    def centres(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(cost_centres).order_by(cost_centres.c.code)).all()
        return [{"code": r.code, "name": r.name, "teams": json.loads(r.teams), "updated_by": r.updated_by,
                 "updated_ms": r.updated_ms} for r in rows]

    def put_centre(self, actor: str, code: str, name: str, teams: list) -> dict:
        if not CODE.match(code or "") or code == UNALLOCATED:
            raise ChargebackError("code: letters, digits, '.', '_' and '-' (not 'unallocated')")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ChargebackError("name: 1 to 200 characters")
        teams = sorted(set(teams or []))
        if not teams or not all(isinstance(t, str) and GROUP.match(t) for t in teams):
            raise ChargebackError("teams: one or more group urns (urn:li:corpGroup:…)")
        taken = {t: c["code"] for c in self.centres() if c["code"] != code for t in c["teams"]}
        clash = sorted(t for t in teams if t in taken)
        if clash:
            raise ChargebackError(f"{', '.join(clash)} already pays from {taken[clash[0]]}: a team has one cost centre", 409)
        row = {"code": code, "name": name.strip(), "teams": json.dumps(teams), "updated_by": actor, "updated_ms": _now()}
        with self.engine.begin() as conn:
            conn.execute(delete(cost_centres).where(cost_centres.c.code == code))
            conn.execute(insert(cost_centres).values(**row))
            if self.audit is not None:
                self.audit.record("chargeback", "chargeback.centre_saved", actor, None,
                                  {"code": code, "teams": teams}, conn=conn)
        return {**row, "teams": teams}

    def delete_centre(self, actor: str, code: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(cost_centres).where(cost_centres.c.code == code)).rowcount:
                raise ChargebackError("no such cost centre", 404)
            if self.audit is not None:
                self.audit.record("chargeback", "chargeback.centre_deleted", actor, None, {"code": code}, conn=conn)

    # ---- attribution ------------------------------------------------------------------------------
    def _resolver(self):
        team_to_centre = {t: c["code"] for c in self.centres() for t in c["teams"]}
        person: dict[str, list[str]] = {}
        owners: dict[str, list[str]] = {}

        def of_person(user: str) -> tuple[list[str], Optional[str]]:
            urn = user if user.startswith("urn:li:") else f"urn:li:corpuser:{user}"
            if urn not in person:
                try:
                    groups = self.groups_of(urn) or []
                except Exception:  # noqa: BLE001 - unknown groups: unallocated, with the reason
                    logger.warning("chargeback: groups of %s unavailable", urn)
                    groups = []
                person[urn] = sorted({team_to_centre[g] for g in groups if g in team_to_centre})
            return person[urn], (None if person[urn] else f"{urn} is in no cost centre's team")

        def of_owned(urn: str) -> tuple[list[str], Optional[str]]:
            if urn not in owners:
                found: set[str] = set()
                try:
                    aspect = (self.graph.get_aspect(urn, "ownership") or {}) if self.graph is not None else {}
                except Exception:  # noqa: BLE001 - the catalog is unreachable
                    logger.warning("chargeback: owners of %s unavailable", urn)
                    aspect = {}
                for o in aspect.get("owners") or []:
                    owner = str(o.get("owner") or "")
                    if owner in team_to_centre:
                        found.add(team_to_centre[owner])
                    elif owner.startswith("urn:li:corpuser:"):
                        found.update(of_person(owner)[0])
                owners[urn] = sorted(found)
            return owners[urn], (None if owners[urn] else f"{urn} has no owner in a cost centre")
        return of_person, of_owned

    def compute(self, month: str) -> dict:
        """The month's costs by cost centre and source; not stored (``issue`` freezes it)."""
        from cairn.ai.usage import usage_table
        from cairn.dataaccess.query_history import price, query_history, trino_rates
        from cairn.reports.costs import asset_costs, run_costs

        start, end, first, last = month_bounds(month)
        of_person, of_owned = self._resolver()
        lines: dict[tuple[str, str], dict] = {}
        why: dict[str, set] = defaultdict(set)

        def charge(centres: list[str], reason: Optional[str], source: str, usd: float, quantity: dict) -> None:
            if not usd:
                return
            targets = centres or [UNALLOCATED]
            if not centres and reason:
                why[source].add(reason)
            share = 1 / len(targets)
            for c in targets:
                ln = lines.setdefault((c, source), {"cost_centre": c, "source": source, "usd": 0.0, "items": 0, **{
                    k: 0.0 for k in quantity}})
                ln["usd"] += usd * share
                ln["items"] += 1
                for k, v in quantity.items():
                    ln[k] = ln.get(k, 0.0) + v * share
        rates = trino_rates()
        with self.engine.connect() as conn:
            if rates != (None, None):
                for user, cpu, scanned in conn.execute(select(query_history.c.user, query_history.c.cpu_ms,
                                                              query_history.c.input_bytes).where(
                        query_history.c.create_ms >= start, query_history.c.create_ms < end)):
                    usd = price(int(cpu or 0), int(scanned or 0), rates) or 0.0
                    charge(*of_person(user), "sql", usd, {"cpu_hours": (cpu or 0) / 3_600_000,
                                                          "scanned_tb": (scanned or 0) / 1e12})
            for actor, usd in conn.execute(select(usage_table.c.actor, usage_table.c.cost_usd).where(
                    usage_table.c.ts_ms >= start, usage_table.c.ts_ms < end)):
                charge(*of_person(actor), "ai", usd or 0.0, {})
            for urn, storage, compute in conn.execute(select(asset_costs.c.urn, asset_costs.c.storage_usd,
                                                             asset_costs.c.compute_usd).where(
                    asset_costs.c.day >= first, asset_costs.c.day <= last)):
                charge(*of_owned(urn), "storage", storage or 0.0, {})
                charge(*of_owned(urn), "table_compute", compute or 0.0, {})
            for job, usd in conn.execute(select(run_costs.c.job, run_costs.c.usd).where(
                    run_costs.c.day >= first, run_costs.c.day <= last, run_costs.c.parent.is_(None),
                    run_costs.c.outputs == "[]", run_costs.c.usd > 0)):          # charged to no table: once only
                charge(*of_owned(job), "runs", usd or 0.0, {})
        rows = sorted(lines.values(), key=lambda r: (r["cost_centre"] == UNALLOCATED, r["cost_centre"], r["source"]))
        for r in rows:
            r["usd"] = round(r["usd"], 4)
            for k in ("cpu_hours", "scanned_tb"):
                if k in r:
                    r[k] = round(r[k], 6)
        centres = {c["code"]: c["name"] for c in self.centres()}
        totals: dict[str, float] = defaultdict(float)
        for r in rows:
            totals[r["cost_centre"]] += r["usd"]
        return {"month": month, "first_day": first, "last_day": last,
                "rates": {"trino_usd_per_cpu_hour": rates[0], "trino_usd_per_tb_scanned": rates[1]},
                "lines": rows,
                "cost_centres": [{"cost_centre": c, "name": centres.get(c, "Unallocated" if c == UNALLOCATED else c),
                                  "usd": round(v, 2)} for c, v in sorted(totals.items(), key=lambda kv: (
                                      kv[0] == UNALLOCATED, kv[0]))],
                "unallocated_reasons": {s: sorted(r)[:20] for s, r in why.items()},
                "total_usd": round(sum(r["usd"] for r in rows), 2)}

    # ---- statements -------------------------------------------------------------------------------
    def issue(self, actor: str, month: str, now_ms: Optional[int] = None) -> dict:
        _, end, _, _ = month_bounds(month)
        if (now_ms or _now()) < end:
            raise ChargebackError(f"{month} is not over yet: preview it, issue it once it has ended", 409)
        doc = self.compute(month)
        with self.engine.begin() as conn:
            version = 1 + (conn.execute(select(chargeback_statements.c.version).where(
                chargeback_statements.c.month == month).order_by(chargeback_statements.c.version.desc())).scalar() or 0)
            sid = uuid.uuid4().hex
            row = {"id": sid, "month": month, "version": version, "issued_by": actor, "issued_ms": now_ms or _now(),
                   "total_usd": f"{doc['total_usd']:.2f}", "doc": json.dumps(doc, sort_keys=True)}
            conn.execute(insert(chargeback_statements).values(**row))
            if self.audit is not None:
                self.audit.record("chargeback", "chargeback.issued", actor, None,
                                  {"statement": sid, "month": month, "version": version, "total_usd": row["total_usd"],
                                   "cost_centres": {c["cost_centre"]: c["usd"] for c in doc["cost_centres"]}}, conn=conn)
        return self._public(row)

    @staticmethod
    def _public(r) -> dict:
        m = r if isinstance(r, dict) else dict(r._mapping)
        return {**json.loads(m["doc"]), "id": m["id"], "month": m["month"], "version": m["version"],
                "issued_by": m["issued_by"], "issued_ms": m["issued_ms"], "total_usd": m["total_usd"]}

    def statements(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(chargeback_statements.c.id, chargeback_statements.c.month,
                                       chargeback_statements.c.version, chargeback_statements.c.issued_by,
                                       chargeback_statements.c.issued_ms, chargeback_statements.c.total_usd)
                                .order_by(chargeback_statements.c.month.desc(), chargeback_statements.c.version.desc())).all()
        return [dict(r._mapping) for r in rows]

    def statement(self, sid: str, viewer: Optional[str] = None, operator: bool = False) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(chargeback_statements).where(chargeback_statements.c.id == sid)).first()
        if r is None:
            raise ChargebackError("no such statement", 404)
        out = self._public(r)
        if operator:
            return out
        mine = set(self._resolver()[0](viewer)[0]) if viewer else set()
        if not mine:
            raise ChargebackError("no such statement", 404)       # nothing of it is the viewer's
        out["lines"] = [ln for ln in out["lines"] if ln["cost_centre"] in mine]
        out["cost_centres"] = [c for c in out["cost_centres"] if c["cost_centre"] in mine]
        out.pop("unallocated_reasons", None)
        out["total_usd"] = f"{sum(c['usd'] for c in out['cost_centres']):.2f}"
        out["partial"] = True                                     # the viewer's cost centres only
        return out

    @staticmethod
    def to_csv(statement: dict) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["month", "version", "cost_centre", "source", "usd", "items", "cpu_hours", "scanned_tb"])
        for ln in statement["lines"]:
            w.writerow([statement["month"], statement["version"], ln["cost_centre"], ln["source"], f"{ln['usd']:.4f}",
                        ln["items"], ln.get("cpu_hours", ""), ln.get("scanned_tb", "")])
        return out.getvalue()


def build_chargeback_router(cb: Chargeback, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import PlainTextResponse

    router = APIRouter(prefix="/v1/chargeback", tags=["costs"])

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def call(fn, *a):
        try:
            return fn(*a)
        except ChargebackError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/cost-centres")
    def centres(p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return {"cost_centres": cb.centres()}

    @router.put("/cost-centres/{code}")
    def put_centre(code: str, name: str = Body(...), teams: list[str] = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return call(cb.put_centre, p.urn, code, name, teams)

    @router.delete("/cost-centres/{code}", status_code=204)
    def delete_centre(code: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        call(cb.delete_centre, p.urn, code)

    @router.get("/preview/{month}")
    def preview(month: str, p=Depends(current_principal)):
        """The month so far (or a past month) as it would be issued now; nothing is kept."""
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return call(cb.compute, month)

    @router.post("/statements", status_code=201)
    def issue(month: str = Body(..., embed=True), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return call(cb.issue, p.urn, month)

    @router.get("/statements")
    def statements(p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return {"statements": cb.statements()}

    @router.get("/statements/{sid}")
    def statement(sid: str, format: str = "json", p=Depends(current_principal)):
        s = call(cb.statement, sid, p.urn, operator(p))
        if format == "csv":
            return PlainTextResponse(cb.to_csv(s), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="chargeback-{s["month"]}-v{s["version"]}.csv"'})
        return s

    return router
