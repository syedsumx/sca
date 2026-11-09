"""Sign-in for the command line: the OAuth 2.0 Device Authorization Grant (RFC 8628).

``cairn login`` asks for a device code (``POST /oauth/device_authorization``), shows the person a short user code
and the verification page (``<public url>/cairn/device``), and polls the token endpoint with the device code
(``grant_type=urn:ietf:params:oauth:grant-type:device_code``) until the person, signed in to Cairn in a browser,
approves it on that page. The answers while it waits are RFC 8628's: ``authorization_pending``, ``slow_down`` (the
CLI polled sooner than the interval; the interval grows by five seconds), ``expired_token`` and ``access_denied``.

What the CLI gets acts as the person who approved it, with the person's own rights (nothing more, nothing less), and
is short-lived:

* an access token ``cla_…`` (``CAIRN_CLI_ACCESS_TOKEN_SECONDS``, one hour by default, at most twelve hours);
* a rotating refresh token ``clr_…`` (``CAIRN_CLI_REFRESH_TOKEN_DAYS``, 30 days unused) for the next access token.
  A refresh token works once; one that comes back after it was used ends the whole session (it was copied). A
  session ends after ``CAIRN_CLI_SESSION_MAX_DAYS`` (90) whatever its refreshes: the person signs in again.

Each approval is a **signed-in device** (a session). People see theirs on their profile page (``GET
/v1/me/devices``) and sign one out there (``DELETE /v1/me/devices/{id}``) or with ``cairn logout`` (``POST
/oauth/revoke``); administrators can list and end anyone's (``/admin/devices``, ``MANAGE_GLOBAL_SETTINGS``), and a
person deactivated by SCIM loses every session at once. A refresh is refused for a person whose account is suspended.

Approving needs the person's browser session, never a token (a signed-in CLI cannot approve another device), with a
passkey check when the operator asks for one (``CAIRN_STEP_UP_ACTIONS`` includes ``device.approve``). User codes are
eight letters from an alphabet without vowels or look-alikes (about 34 bits), valid ten minutes
(``CAIRN_DEVICE_CODE_SECONDS``); each person gets ``CAIRN_DEVICE_LOOKUPS_PER_10_MIN`` (20) wrong codes in ten
minutes, so guessing someone else's code is not practical. ``CAIRN_DEVICE_LOGIN=off`` turns the whole flow off.

Storage reuses the agents' token table (``cairn_agent_tokens``): every token is kept only as a SHA-256 hash, and
every row of one session shares its ``client_id`` (``cli:<session>``), so ending a session is one update.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import and_, delete, select, update
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.identity.agents import agent_tokens

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_ID = "cairn-cli"
ACCESS_PREFIX, REFRESH_PREFIX = "cla_", "clr_"
KIND_DEVICE, KIND_USER_CODE, KIND_ACCESS, KIND_REFRESH = "cli_dev", "cli_uc", "cli_acc", "cli_ref"
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"            # RFC 8628 §6.1: no vowels, no 0/O 1/I
SESSION = re.compile(r"^[0-9a-f]{24}$")


class DeviceError(Exception):
    def __init__(self, message: str, status: int = 400, oauth_error: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.oauth_error = oauth_error


@dataclass(frozen=True)
class CliGrant:
    actor: str
    session: str
    expires_ms: int


def _now() -> int:
    return int(time.time() * 1000)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_user_code(code: str) -> str:
    """What the person typed, as stored: upper case, without the dash and spaces."""
    return re.sub(r"[\s-]", "", str(code or "")).upper()


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number")
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


class DeviceLogins:
    def __init__(self, engine, audit=None, verification_uri: str = "",
                 active: Optional[Callable[[str], Optional[str]]] = None, enabled: Optional[bool] = None):
        """``active(actor)``: why the person may not sign in now (suspended), or None. ``verification_uri``: the
        web page where people enter codes."""
        self.engine, self.audit = engine, audit
        self.active = active
        self.enabled = (os.environ.get("CAIRN_DEVICE_LOGIN", "on").strip().lower() not in ("off", "false", "0")
                        if enabled is None else enabled)
        self.verification_uri = verification_uri
        self.access_ttl_ms = _int_env("CAIRN_CLI_ACCESS_TOKEN_SECONDS", 3600, 300, 43_200) * 1000
        self.refresh_ttl_ms = _int_env("CAIRN_CLI_REFRESH_TOKEN_DAYS", 30, 1, 365) * 86_400_000
        self.session_max_ms = _int_env("CAIRN_CLI_SESSION_MAX_DAYS", 90, 1, 3650) * 86_400_000
        self.code_ttl_ms = _int_env("CAIRN_DEVICE_CODE_SECONDS", 600, 60, 3600) * 1000
        self.interval_s = _int_env("CAIRN_DEVICE_POLL_SECONDS", 5, 1, 60)
        self.lookups = _int_env("CAIRN_DEVICE_LOOKUPS_PER_10_MIN", 20, 1, 1000)
        self.starts = _int_env("CAIRN_DEVICE_STARTS_PER_10_MIN", 30, 1, 10_000)     # per address: the endpoint is public
        self._started: dict[str, list[float]] = {}
        self._misses: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # ---- bookkeeping -------------------------------------------------------------------------------
    def _record(self, action: str, actor: str, session: str, details: Optional[dict] = None, conn=None) -> None:
        if self.audit is not None:
            self.audit.record("identity", action, actor, f"device:{session}", details or {}, conn=conn)

    def _need_enabled(self) -> None:
        if not self.enabled:
            raise DeviceError("command-line sign-in is turned off on this Cairn (CAIRN_DEVICE_LOGIN)", 400,
                              "unauthorized_client")

    def _sweep(self, conn, now: int) -> None:
        """Codes and access tokens long past their expiry are deleted (revoked sessions keep their refresh rows until
        they expire, so a copied refresh token still ends in "revoked", not "unknown")."""
        conn.execute(delete(agent_tokens).where(
            agent_tokens.c.kind.in_((KIND_DEVICE, KIND_USER_CODE, KIND_ACCESS)), agent_tokens.c.expires_ms < now - 86_400_000))
        conn.execute(delete(agent_tokens).where(agent_tokens.c.kind == KIND_REFRESH,
                                                agent_tokens.c.expires_ms < now - 30 * 86_400_000))

    # ---- RFC 8628 §3.1-3.2: the device asks ---------------------------------------------------------
    def start(self, client_id: str, name: str = "", address: str = "") -> dict:
        self._need_enabled()
        if client_id != CLIENT_ID:
            raise DeviceError(f"unknown client {client_id!r}: the command line signs in as {CLIENT_ID}", 400,
                              "invalid_client")
        now_s = time.monotonic()
        with self._lock:
            recent = [t for t in self._started.get(address, []) if now_s - t < 600]
            if len(recent) >= self.starts:
                raise DeviceError("too many sign-ins started from this address: wait a few minutes", 429, "slow_down")
            self._started[address] = recent + [now_s]
            if len(self._started) > 10_000:            # forget quiet addresses
                self._started = {k: v for k, v in self._started.items() if v and now_s - v[-1] < 600}
        session = secrets.token_hex(12)
        device_code = secrets.token_urlsafe(32)
        user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
        now = _now()
        expires = now + self.code_ttl_ms
        details = {"status": "pending", "interval": self.interval_s, "last_poll_ms": 0,
                   "name": re.sub(r"[^\w .,@()+:/-]", "", str(name or ""))[:100] or "Cairn CLI",
                   "address": str(address or "")[:64], "user_code_hash": _hash("uc:" + user_code)}
        with self.engine.begin() as conn:
            self._sweep(conn, now)
            conn.execute(agent_tokens.insert().values(
                hash=_hash(device_code), kind=KIND_DEVICE, client_id=f"cli:{session}", actor="", scopes=[],
                expires_ms=expires, revoked=False, details=details, created_ms=now))
            conn.execute(agent_tokens.insert().values(
                hash=_hash("uc:" + user_code), kind=KIND_USER_CODE, client_id=f"cli:{session}", actor="", scopes=[],
                expires_ms=expires, revoked=False, details={}, created_ms=now))
        shown = f"{user_code[:4]}-{user_code[4:]}"
        uri = self.verification_uri
        return {"device_code": device_code, "user_code": shown, "verification_uri": uri,
                "verification_uri_complete": f"{uri}?code={shown}", "expires_in": self.code_ttl_ms // 1000,
                "interval": self.interval_s}

    # ---- the verification page (a signed-in person) ---------------------------------------------------
    def _throttle(self, actor: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._misses.get(actor, []) if now - t < 600]
            self._misses[actor] = recent
            if len(recent) >= self.lookups:
                raise DeviceError("too many wrong codes: wait ten minutes, then start again from the command line",
                                  429, "slow_down")

    def _miss(self, actor: str) -> None:
        with self._lock:
            self._misses.setdefault(actor, []).append(time.monotonic())

    def _pending(self, conn, actor: str, user_code: str):
        code = normalize_user_code(user_code)
        now = _now()
        uc = None
        if len(code) == 8 and all(c in USER_CODE_ALPHABET for c in code):
            uc = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash("uc:" + code),
                                                         agent_tokens.c.kind == KIND_USER_CODE)).first()
        dev = None
        if uc is not None:
            dev = conn.execute(select(agent_tokens).where(agent_tokens.c.client_id == uc.client_id,
                                                          agent_tokens.c.kind == KIND_DEVICE)).first()
        if dev is None or dev.expires_ms < now or dev.revoked or (dev.details or {}).get("status") != "pending":
            self._miss(actor)
            raise DeviceError("this code is not valid: it was mistyped, has expired or was already used. Start again "
                              "with `cairn login`.", 404, "invalid_grant")
        return dev

    def describe(self, actor: str, user_code: str) -> dict:
        """What the person is about to approve: shown on the page before they decide."""
        self._need_enabled()
        self._throttle(actor)
        with self.engine.connect() as conn:
            dev = self._pending(conn, actor, user_code)
        d = dev.details or {}
        return {"client": "Cairn CLI", "name": d.get("name"), "address": d.get("address"),
                "requested_ms": dev.created_ms, "expires_ms": dev.expires_ms,
                "access_token_seconds": self.access_ttl_ms // 1000, "refresh_token_days": self.refresh_ttl_ms // 86_400_000}

    def decide(self, actor: str, user_code: str, approve: bool) -> dict:
        self._need_enabled()
        self._throttle(actor)
        if approve and self.active is not None:
            why = self.active(actor)
            if why:
                raise DeviceError(f"you cannot sign in a device: {why}", 403, "access_denied")
        with self.engine.begin() as conn:
            dev = self._pending(conn, actor, user_code)
            session = dev.client_id.split(":", 1)[1]
            details = {**(dev.details or {}), "status": "approved" if approve else "denied", "decided_ms": _now()}
            done = conn.execute(update(agent_tokens).where(
                agent_tokens.c.hash == dev.hash, agent_tokens.c.actor == "").values(actor=actor, details=details)).rowcount
            if not done:
                raise DeviceError("this code was already used", 409, "invalid_grant")
            conn.execute(delete(agent_tokens).where(agent_tokens.c.client_id == dev.client_id,
                                                    agent_tokens.c.kind == KIND_USER_CODE))
            self._record("device.approved" if approve else "device.denied", actor, session,
                         {"name": details.get("name"), "address": details.get("address")}, conn)
        return {"approved": approve, "name": details.get("name")}

    # ---- RFC 8628 §3.4-3.5: the device polls ---------------------------------------------------------
    def poll(self, device_code: str, client_id: str) -> dict:
        self._need_enabled()
        if client_id != CLIENT_ID:
            raise DeviceError("client_id does not match the device code", 400, "invalid_client")
        now = _now()
        refused: Optional[DeviceError] = None        # raised after the transaction commits what it recorded
        with self.engine.begin() as conn:
            dev = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(device_code or "-"),
                                                          agent_tokens.c.kind == KIND_DEVICE)).first()
            if dev is None:
                raise DeviceError("unknown device code", 400, "invalid_grant")
            d = dict(dev.details or {})
            if dev.revoked or d.get("status") == "used":
                raise DeviceError("this device code was already used; start again with `cairn login`", 400, "invalid_grant")
            if d.get("status") == "denied":
                conn.execute(update(agent_tokens).where(agent_tokens.c.hash == dev.hash).values(revoked=True))
                refused = DeviceError("the sign-in was refused on the verification page", 400, "access_denied")
            elif dev.expires_ms < now:
                raise DeviceError("the code expired before it was approved; start again with `cairn login`", 400,
                                  "expired_token")
            elif d.get("status") == "pending":
                interval = int(d.get("interval") or self.interval_s)
                early = now - int(d.get("last_poll_ms") or 0) < interval * 1000
                d.update(last_poll_ms=now, interval=interval + 5 if early else interval)
                conn.execute(update(agent_tokens).where(agent_tokens.c.hash == dev.hash).values(details=d))
                refused = DeviceError(f"polling too fast: wait {interval + 5} seconds between polls", 400, "slow_down") \
                    if early else DeviceError("waiting for the person to approve the code", 400, "authorization_pending")
            else:      # approved: the code is spent and the session starts
                spent = conn.execute(update(agent_tokens).where(agent_tokens.c.hash == dev.hash,
                                                                agent_tokens.c.revoked.is_(False))
                                     .values(revoked=True, details={**d, "status": "used"})).rowcount
                if not spent:
                    raise DeviceError("this device code was already used", 400, "invalid_grant")
                session = dev.client_id.split(":", 1)[1]
                out = self._issue(conn, dev.actor, session, now, {"name": d.get("name"), "started_ms": now})
                self._record("device.signed_in", dev.actor, session, {"name": d.get("name")}, conn)
        if refused is not None:
            raise refused
        return out

    def _issue(self, conn, actor: str, session: str, now: int, info: dict) -> dict:
        access, refresh = ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32)
        ends = int(info.get("started_ms") or now) + self.session_max_ms
        access_exp = min(now + self.access_ttl_ms, ends)
        refresh_exp = min(now + self.refresh_ttl_ms, ends)
        cid = f"cli:{session}"
        conn.execute(agent_tokens.insert().values(
            hash=_hash(access), kind=KIND_ACCESS, client_id=cid, actor=actor, scopes=[], expires_ms=access_exp,
            revoked=False, details={}, created_ms=now))
        conn.execute(agent_tokens.insert().values(
            hash=_hash(refresh), kind=KIND_REFRESH, client_id=cid, actor=actor, scopes=[], expires_ms=refresh_exp,
            revoked=False, details={**info, "last_used_ms": now}, created_ms=now))
        return {"access_token": access, "token_type": "Bearer", "expires_in": max(1, (access_exp - now) // 1000),
                "refresh_token": refresh, "refresh_expires_in": max(1, (refresh_exp - now) // 1000),
                "session": session, "scope": ""}

    def refresh(self, token: str, client_id: str) -> dict:
        """Rotating refresh: the old refresh token stops working; one that comes back after use ends the session."""
        self._need_enabled()
        if client_id != CLIENT_ID:
            raise DeviceError("client_id does not match the refresh token", 400, "invalid_client")
        now = _now()
        why = None
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(token or "-"),
                                                          agent_tokens.c.kind == KIND_REFRESH)).first()
            if row is None:
                raise DeviceError("unknown refresh token: run `cairn login`", 400, "invalid_grant")
            session = row.client_id.split(":", 1)[1]
            reused = row.revoked and (row.details or {}).get("rotated")
            if row.revoked and not reused:
                raise DeviceError("this device was signed out: run `cairn login`", 400, "invalid_grant")
            if not reused:
                if row.expires_ms < now:
                    raise DeviceError("the sign-in expired: run `cairn login`", 400, "invalid_grant")
                why = self.active(row.actor) if self.active is not None else None
                if not why:
                    done = conn.execute(update(agent_tokens).where(agent_tokens.c.hash == row.hash,
                                                                   agent_tokens.c.revoked.is_(False))
                                        .values(revoked=True, details={**(row.details or {}), "rotated": True})).rowcount
                    if done:
                        info = {k: v for k, v in (row.details or {}).items() if k in ("name", "started_ms")}
                        return self._issue(conn, row.actor, session, now, info)
            # Suspended since, or a spent refresh token came back (copied): the whole session ends, then the refusal.
            self._end(conn, row.client_id)
            self._record("device.signed_out" if why else "device.refresh_reused", row.actor, session,
                         {"by": "suspended"} if why else {}, conn)
        if why:
            raise DeviceError(f"the account cannot sign in: {why}", 400, "invalid_grant")
        raise DeviceError("this refresh token was already used, so the device was signed out: run `cairn login`", 400,
                          "invalid_grant")

    # ---- using and ending sessions ---------------------------------------------------------------------
    def resolve(self, token: str) -> Optional[CliGrant]:
        if not self.enabled or not token.startswith(ACCESS_PREFIX):
            return None
        with self.engine.connect() as conn:
            row = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(token),
                                                          agent_tokens.c.kind == KIND_ACCESS)).first()
        if row is None or row.revoked or row.expires_ms < _now() or not row.actor:
            return None
        return CliGrant(row.actor, row.client_id.split(":", 1)[1], row.expires_ms)

    @staticmethod
    def _end(conn, client_id: str) -> int:
        return conn.execute(update(agent_tokens).where(agent_tokens.c.client_id == client_id,
                                                       agent_tokens.c.kind.in_((KIND_ACCESS, KIND_REFRESH)),
                                                       agent_tokens.c.revoked.is_(False)).values(revoked=True)).rowcount

    def revoke_token(self, token: str) -> bool:
        """RFC 7009 for CLI tokens: either token ends its whole session. False when it is not a CLI token."""
        if not token.startswith((ACCESS_PREFIX, REFRESH_PREFIX)):
            return False
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(token),
                                                          agent_tokens.c.kind.in_((KIND_ACCESS, KIND_REFRESH)))).first()
            if row is not None:
                n = self._end(conn, row.client_id)
                if n:
                    self._record("device.signed_out", row.actor, row.client_id.split(":", 1)[1], {"by": "token"}, conn)
        return True

    def sessions(self, actor: Optional[str] = None) -> list[dict]:
        """Signed-in devices: sessions with a live refresh token (one per session at a time)."""
        now = _now()
        q = select(agent_tokens).where(agent_tokens.c.kind == KIND_REFRESH, agent_tokens.c.revoked.is_(False),
                                       agent_tokens.c.expires_ms > now)
        if actor is not None:
            q = q.where(agent_tokens.c.actor == actor)
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(agent_tokens.c.created_ms.desc())).all()
        out = []
        for r in rows:
            d = r.details or {}
            out.append({"id": r.client_id.split(":", 1)[1], "actor": r.actor, "name": d.get("name") or "Cairn CLI",
                        "client": "Cairn CLI", "signed_in_ms": d.get("started_ms") or r.created_ms,
                        "last_used_ms": d.get("last_used_ms") or r.created_ms, "expires_ms": r.expires_ms})
        return out

    def end_session(self, session: str, by: str, actor: Optional[str] = None) -> int:
        """Signs a device out (its access and refresh tokens stop at once). ``actor``: only that person's."""
        if not SESSION.match(session or ""):
            raise DeviceError("no such device", 404)
        cond = [agent_tokens.c.client_id == f"cli:{session}"]
        if actor is not None:
            cond.append(agent_tokens.c.actor == actor)
        with self.engine.begin() as conn:
            owner = conn.execute(select(agent_tokens.c.actor).where(and_(*cond), agent_tokens.c.kind == KIND_REFRESH)
                                 .limit(1)).scalar()
            if owner is None:
                raise DeviceError("no such device", 404)
            n = self._end(conn, f"cli:{session}")
            self._record("device.signed_out", owner, session, {"by": by}, conn)
        return n

    def end_all(self, actor: str, by: str) -> int:
        """Every session of a person (offboarding; SCIM deactivation)."""
        with self.engine.begin() as conn:
            ids = sorted({r.client_id for r in conn.execute(select(agent_tokens.c.client_id).where(
                agent_tokens.c.actor == actor, agent_tokens.c.kind.in_((KIND_ACCESS, KIND_REFRESH)),
                agent_tokens.c.revoked.is_(False)))})
            n = sum(self._end(conn, cid) for cid in ids)
            if ids:
                self._record("device.all_signed_out", actor, "*", {"by": by, "sessions": len(ids)}, conn)
        return n


def build_device_router(svc: DeviceLogins, services, current_principal, authorize) -> Any:
    """The device endpoints; the token grant itself is on ``/oauth/token`` (:mod:`cairn.identity.agents_api`)."""
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse

    from cairn.common.security import AuthenticationError
    from cairn.identity.agents_api import _form

    router = APIRouter(tags=["identity"])

    def oauth_error(e: DeviceError) -> JSONResponse:
        return JSONResponse({"error": e.oauth_error, "error_description": str(e)}, status_code=e.status,
                            headers={"Cache-Control": "no-store"})

    def browser_person(request: Request):
        """The person signed in to Cairn in this browser: a session, never a token (a CLI does not approve devices)."""
        headers = {k: v for k, v in request.headers.items() if k.lower() != "authorization"}
        try:
            p = services.identity.resolve(headers, dict(request.cookies))
        except AuthenticationError:
            p = None
        if p is None or p.via not in ("session", "dev"):
            raise HTTPException(401, "sign in to Cairn in this browser to approve a device", headers={"WWW-Authenticate": "Bearer"})
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except DeviceError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/oauth/device_authorization")
    async def device_authorization(request: Request):
        """RFC 8628 §3.1: a device starts a sign-in (public: what it gets is useless until a person approves it)."""
        form = {k: v[0] for k, v in (await _form(request)).items() if v}
        try:
            out = svc.start(form.get("client_id", ""), form.get("device_name", ""),
                            request.client.host if request.client else "")
        except DeviceError as e:
            return oauth_error(e)
        return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @router.get("/oauth/device")
    def device_lookup(request: Request, code: str = ""):
        p = browser_person(request)
        return call(svc.describe, p.urn, code)

    @router.post("/oauth/device")
    async def device_decide(request: Request):
        p = browser_person(request)
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send JSON")      # never a cross-site form post
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON")
        if not isinstance(body, dict) or body.get("decision") not in ("approve", "deny"):
            raise HTTPException(422, "decision: approve or deny")
        if body["decision"] == "approve" and getattr(services, "passkeys", None) is not None:
            from cairn.identity.passkeys import PasskeyError

            try:
                services.passkeys.require(p, request, "device.approve")
            except PasskeyError as e:
                raise HTTPException(e.status, str(e), headers=e.headers)
        return call(svc.decide, p.urn, str(body.get("user_code") or ""), body["decision"] == "approve")

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "signed-in devices are people's")
        return p

    @router.get("/v1/me/devices")
    def my_devices(p=Depends(current_principal)):
        person(p)
        current = getattr(p, "run", None) if p.via == "cli" else None
        return {"devices": [{**d, "current": d["id"] == current} for d in svc.sessions(p.urn)]}

    @router.delete("/v1/me/devices/{session}", status_code=204)
    def sign_out_device(session: str, p=Depends(current_principal)):
        call(svc.end_session, session, person(p).urn, p.urn)

    @router.get("/admin/devices")
    def all_devices(actor: Optional[str] = None, p=Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return {"devices": svc.sessions(actor)}

    @router.delete("/admin/devices/{session}", status_code=204)
    def admin_sign_out(session: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        call(svc.end_session, session, p.urn)

    @router.post("/admin/devices/sign-out")
    def admin_sign_out_person(body: dict, p=Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        actor = str((body or {}).get("actor") or "")
        if not actor.startswith("urn:li:corpuser:"):
            raise HTTPException(422, "actor: urn:li:corpuser:<name>")
        return {"revoked": svc.end_all(actor, p.urn)}

    return router
