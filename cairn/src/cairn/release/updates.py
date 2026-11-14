"""Scheduled updates (L160): an install takes the releases its feed offers (cairn.release.feed) when its operators
say so, inside the windows they set, and never otherwise.

**Policy** (``GET/PUT /v1/system/updates/policy``, ``MANAGE_SYSTEM_OPERATIONS``; IaC kind ``update_policy``)::

    {"mode": "manual" | "auto",              # auto: releases of auto_severities are planned by themselves
     "auto_severities": ["security", "critical"],
     "windows": [{"days": ["sat", "sun"], "start": "02:00", "hours": 4}],   # empty: any time
     "timezone": "Europe/Berlin",
     "require_approval": true,               # a planned update waits for an operator's approval
     "separate_approver": false,             # the approver is not the one who planned it (four eyes)
     "require_backup": true,                 # the updater refuses to start without a backup step configured
     "check_every_hours": 6}

**Plans** (``/v1/system/updates/plans``): ``POST`` plans the way to a version (the feed's target by default) as the
feed verified it just now; ``…/{id}/approve``, ``…/{id}/cancel``. A plan is ``pending_approval``, ``scheduled``,
``running``, then ``succeeded``, ``rolled_back`` (the update failed and the install is back where it was) or
``failed`` (it could not be put back by itself: the plan says what to do); ``cancelled``. One plan is open at a
time. The scheduler's leader starts a scheduled plan when a window is open (and not before its ``not_before``), by
starting the updater (cairn.release.runner) as its own Kubernetes Job, so the update outlives the pods it replaces.
Every change is audited (``updates.planned``, ``updates.approved``, ``updates.cancelled``, ``updates.started``,
``updates.finished``).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, select
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.release.manifest import SEVERITIES, ReleaseError, version_key

logger = logging.getLogger(__name__)

SYSTEM = "urn:li:corpuser:__cairn_updates__"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OPEN = ("pending_approval", "scheduled", "running")
DEFAULT_POLICY = {"mode": "manual", "auto_severities": ["security", "critical"], "windows": [], "timezone": "UTC",
                  "require_approval": True, "separate_approver": False, "require_backup": False,
                  "check_every_hours": 6}

policy_table = Table(
    "cairn_update_policy", metadata,
    Column("id", Integer, primary_key=True),          # one row, id 1
    Column("policy", JSONText, nullable=False),
    Column("updated_by", String(512), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

plans_table = Table(
    "cairn_update_plans", metadata,
    Column("id", String(40), primary_key=True),
    Column("from_version", String(64), nullable=False),
    Column("target", String(64), nullable=False),
    Column("steps", JSONText, nullable=False),        # the verified steps: version, chart, images, migrations, requires
    Column("severity", String(16), nullable=True),
    Column("status", String(24), nullable=False, index=True),
    Column("auto", Boolean, nullable=False),
    Column("not_before_ms", BigInteger, nullable=True),
    Column("requested_by", String(512), nullable=False),
    Column("approved_by", String(512), nullable=True),
    Column("job", String(128), nullable=True),        # the updater Job running it
    Column("log", JSONText, nullable=False),          # [{t, step, msg}], newest last
    Column("error", Text, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("started_ms", BigInteger, nullable=True),
    Column("finished_ms", BigInteger, nullable=True),
)


class UpdateError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_policy(body: dict, base: Optional[dict] = None) -> dict:
    p = {**DEFAULT_POLICY, **(base or {})}
    unknown = set(body) - set(DEFAULT_POLICY)
    if unknown:
        raise UpdateError(f"unknown fields: {', '.join(sorted(unknown))}")
    p.update(body)
    if p["mode"] not in ("manual", "auto"):
        raise UpdateError("mode is manual or auto")
    if not isinstance(p["auto_severities"], list) or not set(p["auto_severities"]) <= set(SEVERITIES):
        raise UpdateError(f"auto_severities: some of {', '.join(SEVERITIES)}")
    try:
        ZoneInfo(str(p["timezone"]))
    except (ZoneInfoNotFoundError, ValueError):
        raise UpdateError(f"timezone {p['timezone']!r} is not an IANA time zone")
    if not isinstance(p["windows"], list) or len(p["windows"]) > 14:
        raise UpdateError("windows: a list (up to 14) of {days, start: HH:MM, hours}")
    clean = []
    for w in p["windows"]:
        if not isinstance(w, dict) or set(w) - {"days", "start", "hours"}:
            raise UpdateError("windows: {days, start: HH:MM, hours} each")
        days = w.get("days") or list(DAYS)
        if not isinstance(days, list) or not days or not set(days) <= set(DAYS):
            raise UpdateError(f"windows.days: some of {', '.join(DAYS)}")
        try:
            hh, mm = (int(x) for x in str(w.get("start", "")).split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except (ValueError, AssertionError):
            raise UpdateError("windows.start is HH:MM")
        hours = w.get("hours")
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 24:
            raise UpdateError("windows.hours: 1 to 24")
        clean.append({"days": [d for d in DAYS if d in days], "start": f"{hh:02d}:{mm:02d}", "hours": hours})
    p["windows"] = clean
    for f in ("require_approval", "separate_approver", "require_backup"):
        if not isinstance(p[f], bool):
            raise UpdateError(f"{f} is true or false")
    if not isinstance(p["check_every_hours"], int) or not 1 <= p["check_every_hours"] <= 168:
        raise UpdateError("check_every_hours: 1 to 168")
    p["auto_severities"] = [s for s in SEVERITIES if s in p["auto_severities"]]
    return p


def window_open(policy: dict, now: datetime) -> bool:
    """Is ``now`` inside one of the policy's windows (in its time zone)? No windows: always."""
    if not policy["windows"]:
        return True
    tz = ZoneInfo(policy["timezone"])
    local = now.astimezone(tz)
    for w in policy["windows"]:
        hh, mm = (int(x) for x in w["start"].split(":"))
        for back in (0, 1):          # a window that started yesterday may still be open (02:00 + 26h is not allowed,
            day = local.date() - timedelta(days=back)   # but 22:00 + 4h runs past midnight)
            if DAYS[day.weekday()] not in w["days"]:
                continue
            start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if start <= local < start + timedelta(hours=w["hours"]):
                return True
    return False


def next_window(policy: dict, now: datetime) -> Optional[datetime]:
    """When the next window opens (``now`` if one is open)."""
    if window_open(policy, now):
        return now
    tz = ZoneInfo(policy["timezone"])
    local = now.astimezone(tz)
    best = None
    for w in policy["windows"]:
        hh, mm = (int(x) for x in w["start"].split(":"))
        for ahead in range(0, 8):
            day = local.date() + timedelta(days=ahead)
            if DAYS[day.weekday()] not in w["days"]:
                continue
            start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if start > local and (best is None or start < best):
                best = start
                break
    return best.astimezone(timezone.utc) if best else None


class UpdateManager:
    def __init__(self, engine: Engine, feed, audit=None, launcher=None,
                 authorize: Optional[Callable[[Any, str], bool]] = None):
        self.engine, self.feed, self.audit, self.launcher = engine, feed, audit, launcher
        self._lock = threading.Lock()
        self._last_auto_check = 0

    # ---- policy -------------------------------------------------------------------------------------------
    def policy(self) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(policy_table.c.policy).where(policy_table.c.id == 1)).first()
        return check_policy({}, r[0] if r else None)

    def set_policy(self, actor: str, body: dict) -> dict:
        p = check_policy(body, self.policy())
        with self.engine.begin() as c:
            if c.execute(select(policy_table.c.id).where(policy_table.c.id == 1)).first():
                c.execute(policy_table.update().where(policy_table.c.id == 1)
                          .values(policy=p, updated_by=actor, updated_ms=_now_ms()))
            else:
                c.execute(policy_table.insert().values(id=1, policy=p, updated_by=actor, updated_ms=_now_ms()))
            self._audit(actor, "updates.policy_set", "policy", p, c)
        return p

    # ---- plans --------------------------------------------------------------------------------------------
    def _row(self, plan_id: str, conn=None) -> dict:
        q = select(plans_table).where(plans_table.c.id == plan_id)
        if conn is None:
            with self.engine.connect() as c:
                r = c.execute(q).mappings().first()
        else:
            r = conn.execute(q).mappings().first()
        if r is None:
            raise UpdateError(f"no update plan {plan_id}", 404)
        return dict(r)

    def get(self, plan_id: str) -> dict:
        return self._public(self._row(plan_id))

    def plans(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(plans_table).order_by(plans_table.c.created_ms.desc()).limit(limit)).mappings()
            return [self._public(dict(r)) for r in rows]

    def _open(self, conn) -> Optional[dict]:
        r = conn.execute(select(plans_table).where(plans_table.c.status.in_(OPEN))).mappings().first()
        return dict(r) if r else None

    def plan(self, actor: str, version: Optional[str] = None, not_before: Optional[str] = None,
             auto: bool = False) -> dict:
        """Plan the way to ``version`` (the channel's newest by default), as the feed verifies it now."""
        state = self.feed.check(actor)
        if state.get("error"):
            raise UpdateError(f"the release feed did not verify, so nothing can be planned: {state['error']}", 409)
        steps = state.get("steps") or []
        if not steps:
            raise UpdateError(state.get("blocked") or f"{state['current']} is the newest release on "
                              f"{state['channel']}", 409)
        if version is not None:
            version_key(version)
            names = [s["version"] for s in steps]
            if version not in names:
                raise UpdateError(f"{version} is not on the way up from {state['current']} ({', '.join(names)})")
            steps = steps[:names.index(version) + 1]
        nb = None
        if not_before:
            try:
                nb = int(datetime.fromisoformat(not_before.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                raise UpdateError("not_before is an ISO 8601 time")
        pol = self.policy()
        target = steps[-1]["version"]
        covered = [a for a in state.get("available") or []
                   if version_key(a["version"]) <= version_key(target)]
        severity = max((a["severity"] for a in covered), key=SEVERITIES.index, default="normal")
        status = "pending_approval" if pol["require_approval"] else "scheduled"
        row = {"id": "upd_" + uuid.uuid4().hex[:16], "from_version": state["current"], "target": target,
               "steps": steps, "severity": severity, "status": status, "auto": auto, "not_before_ms": nb,
               "requested_by": actor, "approved_by": None if pol["require_approval"] else actor, "job": None,
               "log": [{"t": _iso(_now_ms()), "step": "plan", "msg": f"planned {' -> '.join(s['version'] for s in steps)}"}],
               "error": None, "created_ms": _now_ms(), "started_ms": None, "finished_ms": None}
        with self._lock, self.engine.begin() as c:
            other = self._open(c)
            if other is not None:
                raise UpdateError(f"update {other['id']} to {other['target']} is {other['status']}: finish or cancel "
                                  "it first", 409)
            c.execute(plans_table.insert().values(**row))
            self._audit(actor, "updates.planned", row["id"], {"target": target, "auto": auto, "status": status}, c)
        return self._public(row)

    def approve(self, actor: str, plan_id: str) -> dict:
        pol = self.policy()
        with self._lock, self.engine.begin() as c:
            r = self._row(plan_id, c)
            if r["status"] != "pending_approval":
                raise UpdateError(f"the plan is {r['status']}, not waiting for approval", 409)
            if pol["separate_approver"] and r["requested_by"] == actor:
                raise UpdateError("this install's policy needs someone other than who planned it to approve", 403)
            c.execute(plans_table.update().where(plans_table.c.id == plan_id)
                      .values(status="scheduled", approved_by=actor, log=r["log"] + [
                          {"t": _iso(_now_ms()), "step": "approve", "msg": f"approved by {actor}"}]))
            self._audit(actor, "updates.approved", plan_id, {"target": r["target"]}, c)
        return self.get(plan_id)

    def cancel(self, actor: str, plan_id: str) -> dict:
        with self._lock, self.engine.begin() as c:
            r = self._row(plan_id, c)
            if r["status"] not in ("pending_approval", "scheduled"):
                raise UpdateError(f"the plan is {r['status']}: only a plan that has not started can be cancelled", 409)
            c.execute(plans_table.update().where(plans_table.c.id == plan_id).values(
                status="cancelled", finished_ms=_now_ms(),
                log=r["log"] + [{"t": _iso(_now_ms()), "step": "cancel", "msg": f"cancelled by {actor}"}]))
            self._audit(actor, "updates.cancelled", plan_id, {"target": r["target"]}, c)
        return self.get(plan_id)

    # ---- what the updater reports -------------------------------------------------------------------------
    def note(self, plan_id: str, step: str, msg: str) -> None:
        with self.engine.begin() as c:
            r = self._row(plan_id, c)
            c.execute(plans_table.update().where(plans_table.c.id == plan_id).values(
                log=(r["log"] + [{"t": _iso(_now_ms()), "step": step, "msg": msg[:4000]}])[-500:]))

    def finish(self, plan_id: str, status: str, error: Optional[str] = None) -> None:
        assert status in ("succeeded", "rolled_back", "failed")
        with self.engine.begin() as c:
            r = self._row(plan_id, c)
            if r["status"] != "running":
                return
            c.execute(plans_table.update().where(plans_table.c.id == plan_id).values(
                status=status, error=(error or None) and error[:8000], finished_ms=_now_ms(),
                log=r["log"] + [{"t": _iso(_now_ms()), "step": "finish", "msg": status + (f": {error}" if error else "")}]))
            self._audit(SYSTEM, "updates.finished", plan_id, {"target": r["target"], "status": status}, c)

    # ---- the scheduler's tick ------------------------------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> Optional[str]:
        """The leader's periodic work: an automatic check and plan, starting a plan whose window is open, and
        noticing an updater that died without reporting. Returns what it did."""
        now = now or datetime.now(timezone.utc)
        pol = self.policy()
        did = None
        if pol["mode"] == "auto" and time.time() - self._last_auto_check > pol["check_every_hours"] * 3600:
            self._last_auto_check = time.time()
            did = self._auto_plan(pol)
        with self.engine.connect() as c:
            r = self._open(c)
        if r is None:
            return did
        if r["status"] == "running":
            return self._watch(r, now) or did
        if r["status"] != "scheduled":
            return did
        if r["not_before_ms"] and now.timestamp() * 1000 < r["not_before_ms"]:
            return did
        if not window_open(pol, now):
            return did
        return self._start(r)

    def _auto_plan(self, pol: dict) -> Optional[str]:
        state = self.feed.check(SYSTEM)
        target, sev = state.get("target"), state.get("severity")
        if state.get("error") or not target or sev not in pol["auto_severities"]:
            return None
        with self.engine.connect() as c:
            if self._open(c) is not None:
                return None
            tried = c.execute(select(plans_table.c.id).where(plans_table.c.target == target)
                              .where(plans_table.c.status.in_(("failed", "rolled_back", "cancelled")))).first()
        if tried is not None:       # it failed or someone cancelled it: an operator decides what next
            return None
        try:
            p = self.plan(SYSTEM, auto=True)
        except UpdateError as e:
            logger.info("automatic update not planned: %s", e)
            return None
        return f"planned {p['id']}"

    def _start(self, r: dict) -> Optional[str]:
        if self.launcher is None:
            self.note(r["id"], "start", "no updater is configured here (cairn.updates.agent on Kubernetes): apply "
                      "this release with your deployment tooling")
            return None
        with self._lock, self.engine.begin() as c:
            cur = self._row(r["id"], c)
            if cur["status"] != "scheduled":
                return None
            c.execute(plans_table.update().where(plans_table.c.id == r["id"]).values(
                status="running", started_ms=_now_ms()))
        try:
            job = self.launcher.start(r["id"])
        except Exception as e:      # noqa: BLE001 - the install is unchanged: the plan fails, nothing ran
            self.finish(r["id"], "failed", f"the updater could not be started (nothing was changed): {e}")
            return f"failed {r['id']}"
        with self.engine.begin() as c:
            c.execute(plans_table.update().where(plans_table.c.id == r["id"]).values(job=job))
        self.note(r["id"], "start", f"updater job {job} started")
        self._audit(SYSTEM, "updates.started", r["id"], {"target": r["target"], "job": job})
        return f"started {r['id']}"

    def _watch(self, r: dict, now: datetime) -> Optional[str]:
        if self.launcher is None or not r.get("job"):
            return None
        state = self.launcher.state(r["job"])
        if state in ("failed", "missing", "succeeded"):     # it ended, and the plan is still running: it never said
            self.finish(r["id"], "failed", f"the updater job {r['job']} ended ({state}) without reporting; read its "
                        "log, then check the release (helm history) before planning again")
            return f"failed {r['id']}"
        return None

    # ---- misc ---------------------------------------------------------------------------------------------
    def _audit(self, actor: str, action: str, plan_id: str, details: dict, conn=None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("system", action, actor, f"urn:li:cairnUpdate:{plan_id}", details, conn=conn)
        except Exception:   # noqa: BLE001
            logger.exception("audit of %s failed", action)

    def _public(self, r: dict) -> dict:
        return {"id": r["id"], "from": r["from_version"], "target": r["target"],
                "path": [s["version"] for s in r["steps"]], "severity": r["severity"], "status": r["status"],
                "auto": bool(r["auto"]), "not_before": _iso(r["not_before_ms"]), "requested_by": r["requested_by"],
                "approved_by": r["approved_by"], "job": r["job"], "error": r["error"], "log": r["log"],
                "created_at": _iso(r["created_ms"]), "started_at": _iso(r["started_ms"]),
                "finished_at": _iso(r["finished_ms"])}

    def overview(self) -> dict:
        pol = self.policy()
        nxt = next_window(pol, datetime.now(timezone.utc)) if pol["windows"] else None
        return {"policy": pol, "window_open": window_open(pol, datetime.now(timezone.utc)),
                "next_window": nxt.strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None,
                "updater": self.launcher is not None, "plans": self.plans(20)}


def build_router(mgr: UpdateManager, current_principal, authorize):
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(tags=["updates"])

    def admin(p) -> None:
        if authorize is None or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "updates need MANAGE_SYSTEM_OPERATIONS")
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)

    def run(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except UpdateError as e:
            raise HTTPException(e.status, str(e))
        except ReleaseError as e:
            raise HTTPException(409, str(e))

    @router.get("/v1/system/updates/policy")
    def get_policy(p=Depends(current_principal)) -> dict:
        """When this install takes updates: windows, manual or automatic, approval, backups."""
        admin(p)
        return mgr.policy()

    @router.put("/v1/system/updates/policy")
    def put_policy(body: dict = Body(...), p=Depends(current_principal)) -> dict:
        admin(p)
        return run(mgr.set_policy, p.urn, body)

    @router.get("/v1/system/updates/plans")
    def list_plans(p=Depends(current_principal)) -> dict:
        admin(p)
        return {**mgr.overview()}

    @router.post("/v1/system/updates/plans")
    def make_plan(body: dict = Body(default={}), p=Depends(current_principal)) -> dict:
        """Plan the update to a version (the channel's newest by default), as the feed verifies it now."""
        admin(p)
        if set(body) - {"version", "not_before"}:
            raise HTTPException(400, "a plan takes version and not_before")
        return run(mgr.plan, p.urn, body.get("version"), body.get("not_before"))

    @router.get("/v1/system/updates/plans/{plan_id}")
    def get_plan(plan_id: str, p=Depends(current_principal)) -> dict:
        admin(p)
        return run(mgr.get, plan_id)

    @router.post("/v1/system/updates/plans/{plan_id}/approve")
    def approve(plan_id: str, p=Depends(current_principal)) -> dict:
        admin(p)
        return run(mgr.approve, p.urn, plan_id)

    @router.post("/v1/system/updates/plans/{plan_id}/cancel")
    def cancel(plan_id: str, p=Depends(current_principal)) -> dict:
        admin(p)
        return run(mgr.cancel, p.urn, plan_id)

    return router
