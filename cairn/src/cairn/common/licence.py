"""Licence keys: who may run Cairn, which edition, for how many people, until when.

A licence is a signed token, ``CAIRN1.<payload>.<signature>`` (base64url). The payload is JSON::

    {"id": "lic_…", "kid": "cairn-2026", "customer": "Acme", "tenant": "acme",
     "edition": "enterprise", "features": [], "max_users": 250,
     "issued_at": "2026-11-01", "expires_at": "2027-11-01", "grace_days": 14}

The signature is Ed25519 over the payload bytes, checked against public keys baked into the image
(:data:`KEYS_PATH`), never against keys from configuration. Licences signed with the development key
that ships in the source tree are accepted for development only: at most 5 users and 31 days.

Without a licence Cairn runs in evaluation mode with every feature, unless the deployment sets
``CAIRN_LICENCE_REQUIRED`` (hosted tenants), which makes it read-only until one is installed. With a
licence:

* past ``expires_at`` it keeps working for ``grace_days`` with a warning, then turns read-only;
* people beyond ``max_users`` (active in the last 30 days, in order of first use) are read-only;
* features outside the edition answer 402.

Read-only means writes are refused with 402; reads, sign-in, user provisioning (SCIM) and installing
a licence keep working.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, func, select
from sqlalchemy.engine import Engine

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

PREFIX = "CAIRN1"
KEYS_PATH = Path("/usr/share/cairn/licence-keys.json")
DEV_KEYS_PATH = Path(__file__).with_name("licence_dev_keys.json")
DEV_MAX_USERS = 5
DEV_MAX_DAYS = 31
SEAT_WINDOW_DAYS = 30

# Features beyond the core catalog, governance and quality; "standard" has none of them.
FEATURES = {
    "ai": "AI search, assistant, documentation and steward",
    "automations": "no-code automations",
    "tag_sync": "warehouse tag sync",
    "ticketing": "two-way ticketing",
    "data_access": "data preview and SQL",
    "costs": "cost per asset",
}
EDITIONS = {"standard": set(), "enterprise": set(FEATURES)}
FEATURE_ROUTES = (("/ai/", "ai"), ("/governance/automations", "automations"), ("/governance/tag-sync", "tag_sync"),
                  ("/notify/ticketing", "ticketing"), ("/data/", "data_access"), ("/reports/costs", "costs"))
# Requests that change nothing although they are POSTs; allowed when Cairn is read-only.
READ_POSTS = {
    "/collab/enrich", "/ai/search/semantic", "/ai/ask", "/ai/chat", "/ai/explain/lineage",
    "/governance/v1/authorize", "/governance/simulate", "/governance/policies/validate",
    "/governance/pii/propagation/plan", "/data/query", "/data/ask", "/context/metrics/{name}/query",
    "/marketplace/access-requests/preview", "/sources/test",
    "/quality/contracts/check", "/notify/inbox/read", "/governance/automations/preview",
}
ALWAYS = ("/admin/licence", "/me", "/scim/")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

licences = Table(
    "cairn_licence", metadata,
    Column("slot", Integer, primary_key=True),          # always 1: one licence per deployment
    Column("token", Text, nullable=False),
    Column("installed_by", String(512), nullable=False),
    Column("installed_ms", BigInteger, nullable=False),
)
seats = Table(
    "cairn_licence_seats", metadata,
    Column("actor", String(512), primary_key=True),
    Column("first_seen_ms", BigInteger, nullable=False),
    Column("last_seen_ms", BigInteger, nullable=False, index=True),
)


class LicenceError(ValueError):
    pass


class LicenceRefused(Exception):
    """A request the licence does not allow (HTTP 402)."""


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _day(s: str) -> date:
    return date.fromisoformat(s[:10])


def load_keys(path: Path = KEYS_PATH) -> dict[str, Ed25519PublicKey]:
    """``{"keys": {"<kid>": "<base64 raw public key>"}}``; a missing file means no production keys."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {kid: Ed25519PublicKey.from_public_bytes(base64.b64decode(k)) for kid, k in (raw.get("keys") or {}).items()}


@dataclass
class Licence:
    id: str
    customer: str
    edition: str
    expires_at: date
    issued_at: date
    max_users: Optional[int] = None
    tenant: Optional[str] = None
    features: set[str] = field(default_factory=set)
    grace_days: int = 14
    development: bool = False
    kid: str = ""

    @property
    def allowed(self) -> set[str]:
        return EDITIONS.get(self.edition, set()) | self.features

    def public(self) -> dict:
        return {"id": self.id, "customer": self.customer, "tenant": self.tenant, "edition": self.edition,
                "features": sorted(self.allowed), "max_users": self.max_users,
                "issued_at": self.issued_at.isoformat(), "expires_at": self.expires_at.isoformat(),
                "grace_days": self.grace_days, "development": self.development}


def verify(token: str, keys: Optional[dict] = None, dev_keys: Optional[dict] = None,
           tenant: Optional[str] = None) -> Licence:
    """The licence in ``token``, or :class:`LicenceError` saying why it is not valid here."""
    keys = load_keys() if keys is None else keys
    dev_keys = load_keys(DEV_KEYS_PATH) if dev_keys is None else dev_keys
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenceError("not a Cairn licence key")
    try:
        payload_bytes, sig = _b64d(parts[1]), _b64d(parts[2])
        doc = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError):
        raise LicenceError("the licence key is damaged")
    kid = doc.get("kid", "")
    key, development = keys.get(kid), False
    if key is None and kid in dev_keys:
        key, development = dev_keys[kid], True
    if key is None:
        raise LicenceError(f"the licence was signed with an unknown key ({kid!r})")
    try:
        key.verify(sig, payload_bytes)
    except InvalidSignature:
        raise LicenceError("the licence signature does not match: it was altered or not issued for Cairn")
    try:
        lic = Licence(id=doc["id"], customer=doc["customer"], edition=doc.get("edition", "standard"),
                      issued_at=_day(doc["issued_at"]), expires_at=_day(doc["expires_at"]),
                      max_users=doc.get("max_users"), tenant=doc.get("tenant"),
                      features=set(doc.get("features") or []), grace_days=int(doc.get("grace_days", 14)),
                      development=development, kid=kid)
    except (KeyError, ValueError) as e:
        raise LicenceError(f"the licence is incomplete: {e}")
    if lic.edition not in EDITIONS:
        raise LicenceError(f"unknown edition {lic.edition!r}")
    if unknown := lic.features - set(FEATURES):
        raise LicenceError(f"unknown features: {', '.join(sorted(unknown))}")
    if development:
        if (lic.expires_at - lic.issued_at).days > DEV_MAX_DAYS:
            raise LicenceError(f"development licences last at most {DEV_MAX_DAYS} days")
        lic.max_users = min(lic.max_users or DEV_MAX_USERS, DEV_MAX_USERS)
    if tenant and lic.tenant and lic.tenant != tenant:
        raise LicenceError(f"the licence is for tenant {lic.tenant!r}, this deployment is {tenant!r}")
    return lic


def issue(private_key, kid: str, **fields) -> str:
    """Sign a licence (vendor side; see deploy/ops/licence.py)."""
    payload = json.dumps({"kid": kid, **fields}, separators=(",", ":"), sort_keys=True).encode()
    return f"{PREFIX}.{_b64e(payload)}.{_b64e(private_key.sign(payload))}"


def _now_ms() -> int:
    return int(time.time() * 1000)


class LicenceService:
    SEAT_WRITE_INTERVAL_MS = 5 * 60 * 1000          # refresh last-seen at most this often per person

    def __init__(self, engine: Engine, required: bool = False, tenant: Optional[str] = None, audit=None,
                 keys: Optional[dict] = None, dev_keys: Optional[dict] = None, today=None):
        self.engine = engine
        self.required = required
        self.tenant = tenant
        self.audit = audit
        self.keys = keys
        self.dev_keys = dev_keys
        self.today = today or (lambda: datetime.now(timezone.utc).date())
        self._lock = threading.Lock()
        self._cache: Optional[tuple[float, Optional[Licence], Optional[str]]] = None
        self._seen: dict[str, int] = {}

    # ---- the installed licence --------------------------------------------------------
    def _verify(self, token: str) -> Licence:
        return verify(token, self.keys, self.dev_keys, self.tenant)

    def current(self) -> tuple[Optional[Licence], Optional[str]]:
        """The installed licence and, when it no longer verifies, why (cached for a minute)."""
        with self._lock:
            if self._cache and self._cache[0] > time.monotonic():
                return self._cache[1], self._cache[2]
        with self.engine.connect() as conn:
            row = conn.execute(select(licences.c.token).where(licences.c.slot == 1)).first()
        lic, problem = None, None
        if row:
            try:
                lic = self._verify(row.token)
            except LicenceError as e:
                problem = str(e)
        with self._lock:
            self._cache = (time.monotonic() + 60, lic, problem)
        return lic, problem

    def install(self, token: str, actor: str) -> Licence:
        lic = self._verify(token)
        with self.engine.begin() as conn:
            conn.execute(delete(licences))
            conn.execute(licences.insert().values(slot=1, token=token.strip(), installed_by=actor,
                                                  installed_ms=_now_ms()))
        self._cache = None
        if self.audit:
            self.audit.record("licence", "licence.installed", actor, lic.id, lic.public())
        return lic

    def install_file(self, path: str) -> None:
        token = Path(path).read_text().strip()
        with self.engine.connect() as conn:
            row = conn.execute(select(licences.c.token).where(licences.c.slot == 1)).first()
        if row and row.token == token:
            return
        try:
            self.install(token, "urn:li:corpuser:cairn-service")
        except LicenceError as e:
            logger.error("licence in %s not installed: %s", path, e)

    def remove(self, actor: str) -> None:
        """Remove the licence; seats are counted afresh from the next one."""
        with self.engine.begin() as conn:
            conn.execute(delete(licences))
            conn.execute(delete(seats))
        self._cache = None
        self._seen.clear()
        if self.audit:
            self.audit.record("licence", "licence.removed", actor, None, {})

    # ---- state ------------------------------------------------------------------------
    def state(self) -> dict:
        """``status``: evaluation, valid, expiring (in grace), expired, invalid or missing (required)."""
        lic, problem = self.current()
        used = self.seats_used()
        out: dict = {"required": self.required, "tenant": self.tenant, "seats_used": used, "licence": None,
                     "read_only": False, "days_left": None, "problem": problem}
        if lic is None:
            out["status"] = "invalid" if problem else ("missing" if self.required else "evaluation")
            out["read_only"] = bool(problem) or self.required
            return out
        days_left = (lic.expires_at - self.today()).days
        out.update(licence=lic.public(), days_left=days_left, seats_limit=lic.max_users,
                   over_seats=bool(lic.max_users and used > lic.max_users))
        if days_left >= 0:
            out["status"] = "valid"
        elif -days_left <= lic.grace_days:
            out["status"] = "expiring"
        else:
            out["status"], out["read_only"] = "expired", True
        return out

    # ---- seats ------------------------------------------------------------------------
    def seats_used(self) -> int:
        since = _now_ms() - SEAT_WINDOW_DAYS * 86_400_000
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(seats).where(seats.c.last_seen_ms >= since)).scalar()

    def _touch(self, actor: str) -> None:
        now = _now_ms()
        if now - self._seen.get(actor, 0) < self.SEAT_WRITE_INTERVAL_MS:
            return
        self._seen[actor] = now
        with self.engine.begin() as conn:
            if conn.execute(seats.update().where(seats.c.actor == actor).values(last_seen_ms=now)).rowcount == 0:
                conn.execute(seats.insert().values(actor=actor, first_seen_ms=now, last_seen_ms=now))

    def has_seat(self, actor: str, limit: int) -> bool:
        since = _now_ms() - SEAT_WINDOW_DAYS * 86_400_000
        with self.engine.connect() as conn:
            seated = conn.execute(select(seats.c.actor).where(seats.c.last_seen_ms >= since)
                                  .order_by(seats.c.first_seen_ms, seats.c.actor).limit(limit)).scalars().all()
        return actor in seated

    # ---- enforcement ------------------------------------------------------------------
    def check(self, actor: str, is_service: bool, method: str, route: str) -> None:
        """Raise :class:`LicenceRefused` when the licence does not allow this request."""
        if is_service or route.startswith(ALWAYS):
            return
        lic, _ = self.current()
        if lic is None and not self.required and not self.current()[1]:
            return                                              # evaluation: everything, no seats
        self._touch(actor)
        feature = next((f for prefix, f in FEATURE_ROUTES if route.startswith(prefix)), None)
        if lic is not None and feature and feature not in lic.allowed:
            raise LicenceRefused(f"{FEATURES[feature]} is not part of the {lic.edition} edition")
        if method not in WRITE_METHODS or (method == "POST" and route in READ_POSTS):
            return
        st = self.state()
        if st["read_only"]:
            raise LicenceRefused({"missing": "Cairn has no licence installed: it is read-only until one is",
                                  "invalid": f"the licence is not valid ({st['problem']}): Cairn is read-only",
                                  "expired": "the licence has expired: Cairn is read-only"}[st["status"]])
        if lic is not None and lic.max_users and st["seats_used"] > lic.max_users \
                and not self.has_seat(actor, lic.max_users):
            raise LicenceRefused(f"all {lic.max_users} seats are taken: you can read but not change anything")


def from_env(engine: Engine, audit=None) -> LicenceService:
    """``CAIRN_LICENCE_REQUIRED``, ``CAIRN_TENANT_ID``, and ``CAIRN_LICENCE_FILE``: a key to install at
    start when it differs from the installed one (hosting provisions licences this way)."""
    svc = LicenceService(engine, required=os.environ.get("CAIRN_LICENCE_REQUIRED", "").lower() in ("1", "true", "yes"),
                         tenant=os.environ.get("CAIRN_TENANT_ID") or None, audit=audit)
    path = os.environ.get("CAIRN_LICENCE_FILE")
    if path and os.path.exists(path):
        svc.install_file(path)
    return svc
