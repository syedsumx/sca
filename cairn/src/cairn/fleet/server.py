"""The vendor's fleet console (L161): ``cairn fleet serve``, a service of its own (its own database, its own sign-in),
never part of a customer's Cairn.

**Installs** are registered by the vendor's operators: ``POST /fleet/v1/installs`` ``{name, customer, channel,
licence_id}`` answers the install's id and its **check-in key**, which the customer puts in a Kubernetes Secret
(Helm ``cairn.fleet``). The key is derived from the console's master key (``CAIRN_FLEET_MASTER_KEY_FILE``), the
install's id and a salt, so no customer's key is stored; ``…/{id}/rotate`` changes the salt (the old key stops
working at once).

**Check-ins** (``POST /fleet/v1/checkin``) come from installs, outbound only: a JSON body with the install's id and
the time it was sent, signed ``X-Cairn-Signature: sha256=<hex HMAC-SHA256 of the body>`` with its key. A body sent
more than 10 minutes from now, or not newer than the last one accepted, is refused (a replay). What an install
sends is a fixed list (cairn.fleet.reporter.FIELDS): its version and channel, the update it is offered and its last
update's outcome, its licence's state and seats, and its components' health. Never data, names of data, people or
queries; anything else in a body is dropped before it is stored.

**The console** (``GET /fleet/v1/installs``) lists every install with what needs attention, worst first: ``silent``
(no check-in for ``CAIRN_FLEET_STALE_HOURS``, 3 by default), ``unhealthy`` (a component down), ``update_failed``
(its last update rolled back or failed), ``security_update`` / ``update`` (a newer release it has not taken),
``licence_expired`` / ``licence_expiring`` (30 days) and ``over_seats``. ``GET /fleet/v1/installs/{id}`` adds its
last 50 check-ins. The page itself is served at ``/``.

**Sign-in** is the console's own: operators' bearer tokens, kept as SHA-256 digests in ``CAIRN_FLEET_OPERATORS_FILE``
(``{"operators": [{"name": "ops-anna", "token_sha256": "…"}]}``, written by ``cairn fleet operator``); a customer's
Cairn identity means nothing here. Every operator action is logged with the operator's name.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Request  # module level: with postponed annotations a local import becomes a query parameter
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from cairn.common.db import metadata
from cairn.fleet.common import checkins_table, install_key, installs_table, sign  # noqa: F401 - re-exported

logger = logging.getLogger(__name__)

SKEW_MS = 10 * 60 * 1000
MAX_BODY = 64 * 1024
KEEP = 500                  # check-ins kept per install
NAME = re.compile(r"^[\w .,&'()-]{1,200}$")
SEVERITY_ORDER = {"silent": 0, "unhealthy": 1, "update_failed": 2, "licence_expired": 3, "security_update": 4,
                  "over_seats": 5, "licence_expiring": 6, "update": 7}


class FleetError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(body: dict) -> dict:
    """What a check-in may carry, and nothing else (cairn.fleet.reporter builds the same shape)."""
    def pick(d: Any, keys: tuple) -> dict:
        return {k: d[k] for k in keys if isinstance(d, dict) and k in d and isinstance(d[k], (str, int, float, bool,
                                                                                             type(None)))}
    lic = body.get("licence") if isinstance(body.get("licence"), dict) else {}
    upd = body.get("updates") if isinstance(body.get("updates"), dict) else {}
    health = body.get("health") if isinstance(body.get("health"), dict) else {}
    comps = health.get("components") if isinstance(health.get("components"), dict) else {}
    return {
        "version": str(body.get("version") or "")[:64], "channel": str(body.get("channel") or "")[:32],
        "licence": pick(lic, ("id", "edition", "status", "expires_at", "days_left", "seats_used", "seats_limit",
                              "over_seats", "read_only")),
        "updates": {**pick(upd, ("target", "severity", "checked_at", "error", "blocked")),
                    "last_plan": pick(upd.get("last_plan"), ("target", "status", "finished_at"))},
        "health": {"components": {str(k)[:64]: str(v)[:16] for k, v in list(comps.items())[:50]
                                  if isinstance(v, str)}},
        "platform": pick(body.get("platform"), ("kubernetes", "helm", "runtime")),
    }


def attention(row: dict, now_ms: int, stale_hours: float) -> list[str]:
    """What needs a vendor's attention about one install, worst first."""
    out = []
    last = row.get("last") or {}
    if row.get("last_seen_ms") is None or now_ms - row["last_seen_ms"] > stale_hours * 3600 * 1000:
        out.append("silent")
    comps = (last.get("health") or {}).get("components") or {}
    if any(v not in ("ok", "up", "healthy") for v in comps.values()):
        out.append("unhealthy")
    upd = last.get("updates") or {}
    if (upd.get("last_plan") or {}).get("status") in ("failed", "rolled_back"):
        out.append("update_failed")
    if upd.get("target"):
        out.append("security_update" if upd.get("severity") in ("security", "critical") else "update")
    lic = last.get("licence") or {}
    if lic.get("status") in ("expired", "invalid", "missing"):
        out.append("licence_expired")
    elif lic.get("expires_at"):
        try:
            if (date.fromisoformat(str(lic["expires_at"])[:10]) - datetime.now(timezone.utc).date()).days <= 30:
                out.append("licence_expiring")
        except ValueError:
            pass
    if lic.get("over_seats"):
        out.append("over_seats")
    return sorted(set(out), key=SEVERITY_ORDER.get)


class Fleet:
    def __init__(self, engine: Engine, master_key: str, stale_hours: float = 3.0):
        if len(master_key) < 32:
            raise FleetError("the fleet master key is at least 32 characters")
        self.engine, self.master, self.stale_hours = engine, master_key, stale_hours

    # ---- installs ------------------------------------------------------------------------------------------
    def _row(self, install_id: str, conn=None) -> dict:
        q = select(installs_table).where(installs_table.c.id == install_id)
        if conn is None:
            with self.engine.connect() as c:
                r = c.execute(q).mappings().first()
        else:
            r = conn.execute(q).mappings().first()
        if r is None:
            raise FleetError(f"no install {install_id}", 404)
        return dict(r)

    def register(self, operator: str, body: dict) -> dict:
        name, customer = str(body.get("name") or ""), str(body.get("customer") or "")
        if not NAME.match(name) or not NAME.match(customer):
            raise FleetError("name and customer: up to 200 letters, digits, spaces and . , & ' ( ) -")
        channel = str(body.get("channel") or "stable")
        if not re.match(r"^[a-z][a-z0-9-]{0,31}$", channel):
            raise FleetError("channel: a channel name")
        lic = body.get("licence_id")
        if lic is not None and not re.match(r"^[\w.-]{1,64}$", str(lic)):
            raise FleetError("licence_id: the licence's id")
        if set(body) - {"name", "customer", "channel", "licence_id"}:
            raise FleetError("an install takes name, customer, channel and licence_id")
        iid, salt = "inst_" + uuid.uuid4().hex[:16], secrets.token_hex(16)
        with self.engine.begin() as c:
            same = c.execute(select(installs_table.c.id).where(installs_table.c.customer == customer)
                             .where(installs_table.c.name == name)).first()
            if same is not None:
                raise FleetError(f"{customer} already has an install called {name} ({same[0]}): rotate its key "
                                 "instead of registering it again", 409)
            c.execute(installs_table.insert().values(id=iid, name=name, customer=customer, channel=channel,
                                                     licence_id=lic, salt=salt, created_by=operator,
                                                     created_ms=_now_ms()))
        logger.info("fleet: %s registered %s (%s)", operator, iid, customer)
        return {**self.get(iid, history=0), "key": install_key(self.master, iid, salt)}

    def rotate(self, operator: str, install_id: str) -> dict:
        self._row(install_id)
        salt = secrets.token_hex(16)
        with self.engine.begin() as c:
            c.execute(installs_table.update().where(installs_table.c.id == install_id).values(salt=salt))
        logger.info("fleet: %s rotated the key of %s", operator, install_id)
        return {"id": install_id, "key": install_key(self.master, install_id, salt)}

    def remove(self, operator: str, install_id: str) -> None:
        self._row(install_id)
        with self.engine.begin() as c:
            c.execute(delete(checkins_table).where(checkins_table.c.install_id == install_id))
            c.execute(delete(installs_table).where(installs_table.c.id == install_id))
        logger.info("fleet: %s removed %s", operator, install_id)

    def _public(self, r: dict, now_ms: int) -> dict:
        last = r.get("last") or {}
        return {"id": r["id"], "name": r["name"], "customer": r["customer"], "channel": r["channel"],
                "licence_id": r["licence_id"], "created_at": _iso(r["created_ms"]),
                "last_seen_at": _iso(r["last_seen_ms"]), "version": last.get("version"), "last": last,
                "attention": attention(r, now_ms, self.stale_hours)}

    def installs(self) -> list[dict]:
        now = _now_ms()
        with self.engine.connect() as c:
            rows = [dict(r) for r in c.execute(select(installs_table)).mappings()]
        out = [self._public(r, now) for r in rows]
        return sorted(out, key=lambda x: (min((SEVERITY_ORDER[a] for a in x["attention"]), default=99),
                                          x["customer"].lower(), x["name"].lower()))

    def get(self, install_id: str, history: int = 50) -> dict:
        r = self._row(install_id)
        out = self._public(r, _now_ms())
        if history:
            with self.engine.connect() as c:
                rows = c.execute(select(checkins_table).where(checkins_table.c.install_id == install_id)
                                 .order_by(checkins_table.c.seq.desc()).limit(history)).mappings()
                out["history"] = [{"received_at": _iso(x["received_ms"]), **x["body"]} for x in rows]
        return out

    def summary(self) -> dict:
        items = self.installs()
        versions: dict[str, int] = {}
        for i in items:
            versions[i["version"] or "unknown"] = versions.get(i["version"] or "unknown", 0) + 1
        counts: dict[str, int] = {}
        for i in items:
            for a in i["attention"]:
                counts[a] = counts.get(a, 0) + 1
        return {"installs": len(items), "healthy": sum(1 for i in items if not i["attention"]),
                "attention": counts, "versions": versions}

    # ---- check-ins -----------------------------------------------------------------------------------------
    def checkin(self, raw: bytes, signature: str) -> dict:
        if len(raw) > MAX_BODY:
            raise FleetError("a check-in is at most 64 KiB", 413)
        try:
            body = json.loads(raw)
            iid, sent = str(body["install"]), int(body["sent_ms"])
        except (ValueError, KeyError, TypeError):
            raise FleetError("a check-in is JSON with install and sent_ms")
        try:
            r = self._row(iid)
        except FleetError:
            # The same answer and the same work as a wrong signature, so neither tells which ids exist.
            hmac.compare_digest(sign(install_key(self.master, iid, "-"), raw), signature or "")
            raise FleetError("unknown install or wrong signature", 401)
        if not hmac.compare_digest(sign(install_key(self.master, iid, r["salt"]), raw), signature or ""):
            raise FleetError("unknown install or wrong signature", 401)
        now = _now_ms()
        if abs(now - sent) > SKEW_MS:
            raise FleetError("the check-in's time is more than 10 minutes from the console's: check the clock", 400)
        stored = clean(body)
        with self.engine.begin() as c:
            done = c.execute(installs_table.update().where(installs_table.c.id == iid).where(
                (installs_table.c.last_sent_ms.is_(None)) | (installs_table.c.last_sent_ms < sent))
                .values(last_sent_ms=sent, last_seen_ms=now, last=stored))
            if not done.rowcount:
                raise FleetError("a check-in no newer than one already received (a replay)", 409)
            c.execute(checkins_table.insert().values(install_id=iid, received_ms=now, body=stored))
            old = [x[0] for x in c.execute(select(checkins_table.c.seq).where(checkins_table.c.install_id == iid)
                                           .order_by(checkins_table.c.seq.desc()).offset(KEEP))]
            if old:
                c.execute(delete(checkins_table).where(checkins_table.c.seq.in_(old)))
        return {"ok": True, "attention": attention({**r, "last_seen_ms": now, "last": stored}, now, self.stale_hours)}


# ---- operators (the console's own sign-in) ---------------------------------------------------------------------
class Operators:
    def __init__(self, path: Optional[str]):
        self.path = path
        self._digests: dict[str, str] = {}
        self._mtime = None

    def _load(self) -> None:
        if not self.path:
            return
        try:
            m = os.stat(self.path).st_mtime
        except FileNotFoundError:                 # no operators: nobody signs in (fail closed, not a server error)
            self._digests, self._mtime = {}, None
            return
        if m != self._mtime:
            doc = json.loads(Path(self.path).read_text())
            self._digests = {o["token_sha256"]: o["name"] for o in doc.get("operators") or []
                             if re.match(r"^[0-9a-f]{64}$", o.get("token_sha256", ""))}
            self._mtime = m

    def who(self, authorization: str) -> str:
        """The operator a bearer token belongs to; refused (fail closed) without a configured, matching token."""
        self._load()
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not token or not self._digests:
            raise FleetError("sign in with a fleet operator token", 401)
        digest = hashlib.sha256(token.encode()).hexdigest()
        for d, name in self._digests.items():
            if hmac.compare_digest(d, digest):
                return name
        raise FleetError("sign in with a fleet operator token", 401)


def new_operator(name: str) -> tuple[str, dict]:
    """A token (shown once) and its operators-file entry."""
    if not re.match(r"^[\w.@-]{1,64}$", name):
        raise FleetError("operator name: letters, digits, . @ _ -")
    token = "cfo_" + secrets.token_urlsafe(32)
    return token, {"name": name, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}


def create_app(fleet: Fleet, operators: Operators, ui_dir: Optional[str] = None):
    from fastapi import Body, FastAPI, Header, HTTPException
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="Cairn fleet console", docs_url=None, redoc_url=None)

    @app.exception_handler(FleetError)
    async def _err(_request, e: FleetError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    def op(authorization: str) -> str:
        return operators.who(authorization or "")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "fleet"}

    @app.post("/fleet/v1/checkin")
    async def checkin(request: Request) -> dict:
        """From an install: its signed check-in (outbound from the install; no operator sign-in)."""
        # Anyone can reach this: read at most the largest check-in, never a whole oversized body.
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            raise FleetError(f"a check-in is at most {MAX_BODY // 1024} KiB", 413)
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                raise FleetError(f"a check-in is at most {MAX_BODY // 1024} KiB", 413)
        return fleet.checkin(bytes(raw), request.headers.get("x-cairn-signature", ""))

    @app.get("/fleet/v1/summary")
    def summary(authorization: str = Header("")) -> dict:
        op(authorization)
        return fleet.summary()

    @app.get("/fleet/v1/installs")
    def installs(authorization: str = Header("")) -> dict:
        op(authorization)
        return {"installs": fleet.installs()}

    @app.post("/fleet/v1/installs")
    def register(body: dict = Body(...), authorization: str = Header("")) -> dict:
        return fleet.register(op(authorization), body)

    @app.get("/fleet/v1/installs/{install_id}")
    def get(install_id: str, authorization: str = Header("")) -> dict:
        op(authorization)
        return fleet.get(install_id)

    @app.post("/fleet/v1/installs/{install_id}/rotate")
    def rotate(install_id: str, authorization: str = Header("")) -> dict:
        return fleet.rotate(op(authorization), install_id)

    @app.delete("/fleet/v1/installs/{install_id}")
    def remove(install_id: str, authorization: str = Header("")) -> dict:
        fleet.remove(op(authorization), install_id)
        return {"removed": install_id}

    if ui_dir and Path(ui_dir, "index.html").exists():
        @app.get("/")
        def page():
            return FileResponse(Path(ui_dir, "index.html"), headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/")
        def no_page():
            raise HTTPException(404, "the console page is not built here (ui: npm run build:fleet)")

    return app


def from_env() -> tuple[Fleet, Operators]:
    import sqlalchemy

    url = os.environ.get("CAIRN_FLEET_DATABASE_URL", "")
    key_file = os.environ.get("CAIRN_FLEET_MASTER_KEY_FILE", "")
    if not url or not key_file:
        raise SystemExit("CAIRN_FLEET_DATABASE_URL and CAIRN_FLEET_MASTER_KEY_FILE configure the fleet console")
    engine = sqlalchemy.create_engine(url, pool_pre_ping=True)
    metadata.create_all(engine, tables=[installs_table, checkins_table])
    fleet = Fleet(engine, Path(key_file).read_text().strip(), float(os.environ.get("CAIRN_FLEET_STALE_HOURS", "3")))
    return fleet, Operators(os.environ.get("CAIRN_FLEET_OPERATORS_FILE") or None)
