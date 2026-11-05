"""API quotas and the tenant's IP allowlist.

* **Quotas.** Requests a minute for the whole tenant (``tenant_per_minute``) and for each person
  (``user_per_minute``, with ``users`` overriding it for named people or service accounts). Over a quota, the API
  answers 429 with ``Retry-After`` (seconds to the next minute) and ``X-RateLimit-Limit``/``-Remaining``. Counts
  are kept in Cairn's database, so every replica shares them. Cairn's own service token is not counted against a
  person's quota (it is against the tenant's).
* **IP allowlist.** With ``ip_allowlist`` (CIDRs) set, requests from anywhere else are refused (403) before
  anything else happens, sign-in included. The client's address is the connection's, or, when the connection
  comes from one of ``trusted_proxies``, the first address in ``X-Forwarded-For`` not itself a trusted proxy.
  ``/health`` is open (probes). Include the addresses of Cairn's own callers (Trino, Spark, the
  web UI's proxy) in the list.

Defaults come from the environment (``CAIRN_API_TENANT_PER_MINUTE``, ``CAIRN_API_USER_PER_MINUTE``,
``CAIRN_IP_ALLOWLIST``, ``CAIRN_TRUSTED_PROXIES``, comma-separated); an operator changes them at run time
(``PUT /v1/admin/limits``), which every replica picks up within seconds. A change that would refuse the operator's
own address is refused.
"""

import ipaddress
import os
import threading
import time
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, Integer, PrimaryKeyConstraint, String, Table, select, update
from sqlalchemy.exc import IntegrityError

from cairn.common.db import JSONText, metadata

OPEN_PATHS = ("/health",)
MAX_RATE = 1_000_000

api_limits = Table(
    "cairn_api_limits", metadata,
    Column("id", String(16), primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("updated_by", String(512), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
api_usage = Table(
    "cairn_api_usage", metadata,
    Column("key", String(600), nullable=False),
    Column("minute", BigInteger, nullable=False),
    Column("count", Integer, nullable=False),
    PrimaryKeyConstraint("key", "minute"),
)


class LimitsError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _nets(values) -> list:
    out = []
    for v in values or []:
        try:
            out.append(ipaddress.ip_network(str(v).strip(), strict=False))
        except ValueError:
            raise LimitsError(f"not a network: {v!r} (use CIDRs such as 10.0.0.0/8 or 203.0.113.7/32)")
    return out


def _csv(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _rate(v: Any, what: str) -> Optional[int]:
    if v in (None, "", 0, "0"):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise LimitsError(f"{what}: requests a minute (a whole number), or none")
    if not 1 <= n <= MAX_RATE:
        raise LimitsError(f"{what}: between 1 and {MAX_RATE} requests a minute")
    return n


class ApiLimits:
    def __init__(self, engine, audit=None):
        self.engine, self.audit = engine, audit
        self.defaults = {"tenant_per_minute": _rate(os.environ.get("CAIRN_API_TENANT_PER_MINUTE"), "tenant_per_minute"),
                         "user_per_minute": _rate(os.environ.get("CAIRN_API_USER_PER_MINUTE"), "user_per_minute"),
                         "users": {}, "ip_allowlist": _csv("CAIRN_IP_ALLOWLIST"),
                         "trusted_proxies": _csv("CAIRN_TRUSTED_PROXIES")}
        _nets(self.defaults["ip_allowlist"])
        _nets(self.defaults["trusted_proxies"])
        self._lock = threading.Lock()
        self._cached: tuple[float, dict, list, list] = (0.0, {}, [], [])
        self._pruned = 0

    # ---- settings -----------------------------------------------------------------------------
    def settings(self) -> dict:
        return self._current()[0]

    def _current(self) -> tuple[dict, list, list]:
        now = time.monotonic()
        with self._lock:
            at, doc, allow, proxies = self._cached
            if now - at < 5:
                return doc, allow, proxies
        try:
            with self.engine.connect() as conn:
                row = conn.execute(select(api_limits.c.doc).where(api_limits.c.id == "current")).first()
        except Exception:
            # The database is briefly away: keep what was last read (never loosen to nothing); with nothing
            # read yet, the caller refuses.
            with self._lock:
                if self._cached[1]:
                    self._cached = (now - 4, *self._cached[1:])         # try again in a second
                    return self._cached[1], self._cached[2], self._cached[3]
            raise
        doc = {**self.defaults, **(row.doc if row is not None else {})}
        allow, proxies = _nets(doc.get("ip_allowlist")), _nets(doc.get("trusted_proxies"))
        with self._lock:
            self._cached = (now, doc, allow, proxies)
        return doc, allow, proxies

    def put(self, body: dict, actor: str, peer: Optional[str], forwarded_for: Optional[str] = None) -> dict:
        if not isinstance(body, dict):
            raise LimitsError("limits are an object")
        unknown = set(body) - {"tenant_per_minute", "user_per_minute", "users", "ip_allowlist", "trusted_proxies"}
        if unknown:
            raise LimitsError(f"unknown settings: {', '.join(sorted(unknown))}")
        users = body.get("users") or {}
        if not isinstance(users, dict) or not all(str(k).startswith("urn:li:corpuser:") for k in users):
            raise LimitsError("users: {urn:li:corpuser:…: requests a minute}")
        doc = {"tenant_per_minute": _rate(body.get("tenant_per_minute"), "tenant_per_minute"),
               "user_per_minute": _rate(body.get("user_per_minute"), "user_per_minute"),
               "users": {k: _rate(v, k) for k, v in users.items()},
               "ip_allowlist": [str(n) for n in _nets(body.get("ip_allowlist"))],
               "trusted_proxies": [str(n) for n in _nets(body.get("trusted_proxies"))]}
        allow = _nets(doc["ip_allowlist"])
        caller_ip = _client(peer, forwarded_for, _nets(doc["trusted_proxies"]))   # as the new settings will see it
        if allow and not (caller_ip and _inside(caller_ip, allow)):
            raise LimitsError(f"this allowlist would refuse your own address ({caller_ip}); include it", 409)
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            if not conn.execute(update(api_limits).where(api_limits.c.id == "current")
                                .values(doc=doc, updated_by=actor, updated_ms=now)).rowcount:
                conn.execute(api_limits.insert().values(id="current", doc=doc, updated_by=actor, updated_ms=now))
        with self._lock:
            self._cached = (0.0, *self._cached[1:])                  # read again now, keep the old as fallback
        if self.audit is not None:
            self.audit.record("security", "limits.changed", actor, None, {
                k: doc[k] for k in ("tenant_per_minute", "user_per_minute", "ip_allowlist", "trusted_proxies")}
                | {"users": len(doc["users"])})
        return self.settings()

    # ---- the client's address -------------------------------------------------------------------
    def client_ip(self, peer: Optional[str], forwarded_for: Optional[str]) -> Optional[str]:
        return _client(peer, forwarded_for, self._current()[2])

    def ip_allowed(self, ip: Optional[str]) -> bool:
        _doc, allow, _proxies = self._current()
        if not allow:
            return True
        return bool(ip) and _inside(ip, allow)

    # ---- quotas -----------------------------------------------------------------------------------
    def _hit(self, key: str, limit: int) -> tuple[bool, int, int]:
        """Counts one request on ``key`` this minute: (allowed, remaining, seconds to the next minute)."""
        now = time.time()
        minute = int(now // 60)
        with self.engine.begin() as conn:
            q = update(api_usage).where(api_usage.c.key == key, api_usage.c.minute == minute)
            if not conn.execute(q.values(count=api_usage.c.count + 1)).rowcount:
                try:
                    with conn.begin_nested():
                        conn.execute(api_usage.insert().values(key=key, minute=minute, count=1))
                except IntegrityError:                    # another replica counted first
                    conn.execute(q.values(count=api_usage.c.count + 1))
            used = conn.execute(select(api_usage.c.count).where(api_usage.c.key == key, api_usage.c.minute == minute)).scalar()
        if minute != self._pruned:
            self._pruned = minute
            with self.engine.begin() as conn:
                conn.execute(api_usage.delete().where(api_usage.c.minute < minute - 1))
        return used <= limit, max(0, limit - used), max(1, int(60 - now % 60) + 1)

    def admit_tenant(self) -> Optional[tuple[bool, int, int, int]]:
        limit = self.settings().get("tenant_per_minute")
        if not limit:
            return None
        ok, remaining, retry = self._hit("tenant", int(limit))
        return ok, int(limit), remaining, retry

    def admit_user(self, urn: str) -> Optional[tuple[bool, int, int, int]]:
        doc = self.settings()
        limit = (doc.get("users") or {}).get(urn) or doc.get("user_per_minute")
        if not limit:
            return None
        ok, remaining, retry = self._hit(f"user:{urn}", int(limit))
        return ok, int(limit), remaining, retry


def _client(peer: Optional[str], forwarded_for: Optional[str], proxies: list) -> Optional[str]:
    if not peer:
        return None
    if not proxies or not _inside(peer, proxies) or not forwarded_for:
        return peer
    # Right to left: the first address that is not one of our proxies is the client.
    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        if not _inside(hop, proxies):
            return hop
    return peer


def _inside(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip().strip("[]"))
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    return any(addr in n for n in nets)


class LimitsMiddleware:
    """Pure ASGI, outermost: the IP allowlist, then the tenant's quota."""

    def __init__(self, app, limits: ApiLimits):
        self.app, self.limits = app, limits

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        peer = (scope.get("client") or (None, None))[0]
        try:
            ip = self.limits.client_ip(peer, headers.get("x-forwarded-for"))
            allowed = self.limits.ip_allowed(ip)
        except Exception:  # noqa: BLE001 - fail closed: the allowlist cannot be read
            allowed, ip = False, peer
        if not allowed:
            await JSONResponse({"detail": "requests from this address are not allowed"}, status_code=403)(scope, receive, send)
            return
        scope.setdefault("state", {})["client_ip"] = ip
        try:
            verdict = await run_in_threadpool(self.limits.admit_tenant)
        except Exception:  # noqa: BLE001 - the database is away: requests fail there anyway; do not add a refusal
            verdict = None
        if verdict is not None and not verdict[0]:
            _ok, limit, _remaining, retry = verdict
            await JSONResponse({"detail": f"this tenant is over its limit of {limit} requests a minute"}, status_code=429,
                               headers={"Retry-After": str(retry), "X-RateLimit-Limit": str(limit),
                                        "X-RateLimit-Remaining": "0", "X-RateLimit-Scope": "tenant"})(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_limits_router(limits: ApiLimits, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Request

    # (no "from __future__ import annotations" in this module: Request must stay a real annotation)
    router = APIRouter(prefix="/v1/admin/limits", tags=["admin"])

    @router.get("")
    def get(p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return limits.settings()

    @router.put("")
    def put(request: Request, body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        peer = request.client.host if request.client else None
        try:
            return limits.put(body, p.urn, peer, request.headers.get("x-forwarded-for"))
        except LimitsError as e:
            raise HTTPException(e.status, str(e))

    return router
