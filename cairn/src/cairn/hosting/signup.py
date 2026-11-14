"""Self-service signup and Stripe billing for hosted Cairn: a team signs up, pays, and gets its own tenant,
whose licence follows the subscription.

This runs on the hosting control plane (``CAIRN_SIGNUP``, a JSON object or a file), not in tenants::

    {"domain": "cairn.example", "public_url": "https://signup.cairn.example",
     "smtp": "smtps://user@mail.example:465", "smtp_password_file": "/run/secrets/smtp", "from": "hello@cairn.example",
     "stripe_key_file": "/run/secrets/stripe", "stripe_webhook_secret_file": "/run/secrets/stripe-webhook",
     "licence_key_file": "/run/secrets/licence.pem", "licence_kid": "cairn-2026",
     "provision": ["deploy/hosting/tenant.sh"],
     "plans": {"team": {"price": "price_...", "edition": "team", "max_users": 25},
               "business": {"price": "price_...", "edition": "enterprise", "max_users": 250}}}

1. ``POST /v1/signup`` (public) with ``email``, ``organization``, ``tenant`` (the workspace's name:
   ``<tenant>.<domain>``) and ``plan``. The name must be free and not reserved. Cairn emails a link to
   confirm the address; a link is good for a day and used once. Attempts are limited per address and per
   client.
2. ``GET /v1/signup/verify?token=`` confirms the address, creates the Stripe customer and a Checkout
   session for the plan's price, and sends the browser to Stripe's payment page.
3. Stripe's webhook (``POST /v1/billing/stripe/webhook``, its ``Stripe-Signature`` checked against the
   endpoint's secret, five minutes' tolerance, each event handled once):
   * ``checkout.session.completed``: the tenant is provisioned (``provision`` create ``<tenant>``
     ``--licence <file>``, by default ``deploy/hosting/tenant.sh``) with a licence for the plan's edition
     and users, valid until the paid period ends (plus the licence's grace), and its owner is emailed the
     address. Provisioning that fails is retried by the next tick and shown as ``failed`` until it works.
   * ``invoice.paid``: the licence is renewed to the new period's end (``provision`` upgrade).
   * ``customer.subscription.updated``: a new plan reissues the licence for its edition and users.
   * ``customer.subscription.deleted``: nothing is renewed: the licence runs out, its grace passes and the
     tenant becomes read-only, keeping its data. ``invoice.payment_failed`` emails the owner; Stripe retries.
4. ``POST /v1/billing/portal-link`` with ``email`` emails the owner a link to Stripe's billing portal
   (``GET /v1/billing/portal?token=``), where they change the plan, the card or cancel.

Secrets (Stripe's key and webhook secret, the licence signing key, SMTP's password) are read from files.
Licence files are written 0600 to a private directory and removed after use. With ``metering_key_file``,
each new tenant also gets its usage-metering key (``--metering-key``; see ``cairn.hosting.metering``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import subprocess
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from sqlalchemy import BigInteger, Column, String, Table, Text, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

TENANT = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")      # as deploy/hosting/tenant.sh takes it
EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")
RESERVED = {"www", "api", "app", "admin", "signup", "billing", "status", "mail", "docs", "help", "support", "cairn",
            "static", "assets", "auth", "login", "trino", "grafana", "prometheus", "kube", "system"}
TOKEN_TTL_MS = 24 * 3_600_000
SIGNATURE_TOLERANCE_S = 300

signups = Table(
    "cairn_signups", metadata,
    Column("id", String(32), primary_key=True),
    Column("tenant", String(32), nullable=False, unique=True),
    Column("email", String(320), nullable=False, index=True),
    Column("organization", String(200), nullable=False),
    Column("plan", String(64), nullable=False),
    Column("status", String(16), nullable=False),       # pending, verified, paid, active, failed, canceled
    Column("token_hash", String(64), nullable=True, index=True),
    Column("token_expires_ms", BigInteger, nullable=True),
    Column("stripe_customer", String(64), nullable=True, index=True),
    Column("stripe_subscription", String(64), nullable=True, index=True),
    Column("paid_until", String(10), nullable=True),     # the end of the paid period (a date)
    Column("licence_id", String(64), nullable=True),
    Column("error", Text, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

stripe_events = Table(
    "cairn_stripe_events", metadata,
    Column("id", String(255), primary_key=True),
    Column("type", String(128), nullable=False),
    Column("received_ms", BigInteger, nullable=False),
)


class SignupError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _read(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    with open(path) as f:
        return f.read().strip()


def load_config(raw: Optional[str] = None) -> Optional[dict]:
    raw = (raw if raw is not None else os.environ.get("CAIRN_SIGNUP", "")).strip()
    if not raw:
        return None
    cfg = json.loads(open(raw).read() if not raw.startswith("{") else raw)
    for k in ("domain", "public_url", "smtp", "from", "stripe_key_file", "stripe_webhook_secret_file",
              "licence_key_file", "licence_kid", "plans"):
        if not cfg.get(k):
            raise ValueError(f"CAIRN_SIGNUP: {k} is required")
    for name, p in cfg["plans"].items():
        if not (isinstance(p, dict) and str(p.get("price", "")).startswith("price_") and p.get("edition")
                and isinstance(p.get("max_users"), int)):
            raise ValueError(f"CAIRN_SIGNUP: plan {name}: price (price_...), edition and max_users")
    for k in ("stripe_key_file", "stripe_webhook_secret_file", "licence_key_file"):
        if not os.path.isfile(cfg[k]):
            raise ValueError(f"CAIRN_SIGNUP: {k} {cfg[k]} is not readable")
    return cfg


# --------------------------------------------------------------------------- Stripe
def stripe_signature_ok(payload: bytes, header: str, secret: str, now: Optional[float] = None) -> bool:
    """Stripe's webhook signature: ``t=<ts>,v1=<hex hmac-sha256 of "<ts>.<payload>">``, within the tolerance."""
    parts: dict[str, list[str]] = defaultdict(list)
    for item in (header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts[k].append(v)
    try:
        ts = int(parts["t"][0])
    except (IndexError, ValueError):
        return False
    if abs((now or time.time()) - ts) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for v in parts["v1"])


def stripe_signature(payload: bytes, secret: str, ts: Optional[int] = None) -> str:
    ts = ts or int(time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()


def _form(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding of nested objects and lists (``line_items[0][price]=...``)."""
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, dict):
            out += _form(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += _form(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", str(item))]
        elif v is not None:
            out.append((key, "true" if v is True else "false" if v is False else str(v)))
    return out


class Stripe:
    def __init__(self, key: str, base: str = "https://api.stripe.com", http=None):
        self.key, self.base, self.http = key, base.rstrip("/"), http

    def post(self, path: str, data: dict, idempotency_key: Optional[str] = None) -> dict:
        import httpx

        client = self.http or httpx.Client(timeout=30)
        try:
            r = client.post(f"{self.base}{path}", content=urlencode(_form(data)),
                            headers={"Authorization": f"Bearer {self.key}", "Stripe-Version": "2024-06-20",
                                     "Content-Type": "application/x-www-form-urlencoded",
                                     **({"Idempotency-Key": idempotency_key} if idempotency_key else {})})
        finally:
            if self.http is None:
                client.close()
        if r.status_code >= 300:
            raise SignupError(f"Stripe answered {r.status_code}: {r.text[:300]}", 502)
        return r.json()


# --------------------------------------------------------------------------- the service
class Signup:
    def __init__(self, engine, cfg: dict, stripe: Optional[Stripe] = None, audit=None):
        self.engine, self.cfg, self.audit = engine, cfg, audit
        self.stripe = stripe or Stripe(_read(cfg["stripe_key_file"]) or "", cfg.get("stripe_api", "https://api.stripe.com"))
        self._limits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    # ---- email ----------------------------------------------------------------------------------
    def mail(self, to: str, subject: str, body: str) -> None:
        u = urlparse(self.cfg["smtp"])
        password = _read(self.cfg.get("smtp_password_file"))
        port = u.port or (465 if u.scheme == "smtps" else 587)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.cfg["from"], to
        msg.set_content(body)
        cls = smtplib.SMTP_SSL if u.scheme == "smtps" else smtplib.SMTP
        with cls(u.hostname, port, timeout=15) as s:
            if u.scheme == "smtp" and "starttls=false" not in (u.query or ""):
                s.starttls()
            if u.username:
                s.login(u.username, password or "")
            s.send_message(msg)

    def _limit(self, key: str, n: int = 5, window_s: int = 3600) -> None:
        now = time.time()
        with self._lock:
            q = self._limits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= n:
                raise SignupError("too many attempts; try again later", 429)
            q.append(now)

    # ---- 1. signing up ---------------------------------------------------------------------------
    def start(self, body: dict, client_ip: str) -> dict:
        email, org = str(body.get("email") or "").strip().lower(), str(body.get("organization") or "").strip()
        tenant, plan = str(body.get("tenant") or "").strip().lower(), str(body.get("plan") or "")
        if not EMAIL.match(email):
            raise SignupError("email: an email address")
        if not 1 <= len(org) <= 200:
            raise SignupError("organization: your organization's name")
        if not TENANT.match(tenant) or tenant in RESERVED or "--" in tenant:
            raise SignupError("tenant: 3 to 30 lowercase letters, digits and dashes, starting with a letter")
        if plan not in self.cfg["plans"]:
            raise SignupError(f"plan: one of {', '.join(self.cfg['plans'])}")
        self._limit(f"ip:{client_ip}", 10)
        self._limit(f"email:{email}", 5)
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(select(signups).where(signups.c.tenant == tenant)).first()
            if row is not None and not (row.status == "pending" and row.email == email):
                raise SignupError(f"{tenant}.{self.cfg['domain']} is taken", 409)
            values = {"email": email, "organization": org, "plan": plan, "status": "pending",
                      "token_hash": _hash(token), "token_expires_ms": now + TOKEN_TTL_MS, "updated_ms": now}
            if row is None:
                sid = uuid.uuid4().hex
                conn.execute(insert(signups).values(id=sid, tenant=tenant, created_ms=now, **values))
            else:
                sid = row.id
                conn.execute(update(signups).where(signups.c.id == sid).values(**values))
        link = f"{self.cfg['public_url'].rstrip('/')}/v1/signup/verify?token={token}"
        self.mail(email, "Confirm your Cairn workspace",
                  f"Confirm {email} for {org}'s workspace {tenant}.{self.cfg['domain']} ({plan} plan):\n\n{link}\n\n"
                  "The link works once, for 24 hours. If you did not sign up, ignore this message.")
        return {"tenant": tenant, "status": "pending", "message": f"we sent a link to {email}"}

    # ---- 2. confirming, then paying -------------------------------------------------------------
    def verify(self, token: str) -> str:
        """Confirms the address and returns Stripe Checkout's URL for the plan."""
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(select(signups).where(signups.c.token_hash == _hash(token or ""))).first()
            if row is None or row.status != "pending" or (row.token_expires_ms or 0) < now:
                raise SignupError("this link has expired or was used; sign up again", 410)
            conn.execute(update(signups).where(signups.c.id == row.id).values(
                status="verified", token_hash=None, token_expires_ms=None, updated_ms=now))
        customer = row.stripe_customer or self.stripe.post("/v1/customers", {
            "email": row.email, "name": row.organization, "metadata": {"tenant": row.tenant, "signup": row.id}},
            idempotency_key=f"customer-{row.id}")["id"]
        base = self.cfg["public_url"].rstrip("/")
        session = self.stripe.post("/v1/checkout/sessions", {
            "mode": "subscription", "customer": customer, "client_reference_id": row.id,
            "line_items": [{"price": self.cfg["plans"][row.plan]["price"], "quantity": 1}],
            "subscription_data": {"metadata": {"tenant": row.tenant, "signup": row.id}},
            "metadata": {"tenant": row.tenant, "signup": row.id},
            "success_url": f"{base}/signup/welcome?tenant={row.tenant}", "cancel_url": f"{base}/signup?canceled=1"},
            idempotency_key=f"checkout-{row.id}-{now // 60_000}")
        with self.engine.begin() as conn:
            conn.execute(update(signups).where(signups.c.id == row.id).values(stripe_customer=customer, updated_ms=_now()))
        if self.audit is not None:
            self.audit.record("hosting", "signup.verified", f"mailto:{row.email}", f"tenant:{row.tenant}",
                              {"plan": row.plan, "customer": customer})
        return session["url"]

    # ---- 3. Stripe's events ---------------------------------------------------------------------
    def webhook(self, payload: bytes, signature: str) -> dict:
        secret = _read(self.cfg["stripe_webhook_secret_file"]) or ""
        if not secret or not stripe_signature_ok(payload, signature, secret):
            raise SignupError("bad signature", 400)
        event = json.loads(payload)
        eid, kind, obj = event.get("id"), event.get("type"), (event.get("data") or {}).get("object") or {}
        if not isinstance(eid, str) or not eid:
            raise SignupError("an event without an id")
        try:                                    # claimed first, so two deliveries at once are handled once
            with self.engine.begin() as conn:
                conn.execute(insert(stripe_events).values(id=eid, type=str(kind)[:128], received_ms=_now()))
        except IntegrityError:
            return {"received": True, "duplicate": True}
        try:
            handled = self._handle(kind, obj)
        except Exception:
            with self.engine.begin() as conn:   # not handled: Stripe delivers it again
                conn.execute(delete(stripe_events).where(stripe_events.c.id == eid))
            raise
        return {"received": True, "handled": handled}

    def _handle(self, kind: str, obj: dict) -> bool:
        handled = True
        if kind == "checkout.session.completed":
            self._paid(obj)
        elif kind == "invoice.paid":
            self._renewed(obj)
        elif kind == "customer.subscription.updated":
            self._changed(obj)
        elif kind == "customer.subscription.deleted":
            self._canceled(obj)
        elif kind == "invoice.payment_failed":
            self._payment_failed(obj)
        else:
            handled = False
        return handled

    def _by(self, column, value) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(select(signups).where(column == value)).first()

    def _set(self, sid: str, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(signups).where(signups.c.id == sid).values(updated_ms=_now(), **values))

    @staticmethod
    def _period_end(obj: dict) -> Optional[str]:
        end = obj.get("current_period_end")
        if end is None:
            lines = ((obj.get("lines") or {}).get("data") or [])
            end = max([(ln.get("period") or {}).get("end") or 0 for ln in lines] or [0]) or None
        return datetime.fromtimestamp(int(end), tz=timezone.utc).date().isoformat() if end else None

    def _paid(self, obj: dict) -> None:
        row = self._by(signups.c.id, obj.get("client_reference_id") or (obj.get("metadata") or {}).get("signup"))
        if row is None:
            raise SignupError("a checkout for no signup", 404)
        sub = obj.get("subscription")
        until = self._period_end(obj) or (date.today() + timedelta(days=31)).isoformat()
        self._set(row.id, status="paid", stripe_subscription=sub, stripe_customer=obj.get("customer") or row.stripe_customer,
                  paid_until=until)
        self.provision(row.id)

    def _renewed(self, obj: dict) -> None:
        row = self._by(signups.c.stripe_subscription, obj.get("subscription"))
        if row is None:
            return
        until = self._period_end(obj)
        if until and (row.paid_until or "") < until:
            self._set(row.id, paid_until=until)
            if row.status == "active":
                self._licence(row.id, "upgrade")

    def _changed(self, obj: dict) -> None:
        row = self._by(signups.c.stripe_subscription, obj.get("id"))
        if row is None:
            return
        price = (((obj.get("items") or {}).get("data") or [{}])[0].get("price") or {}).get("id")
        plan = next((n for n, p in self.cfg["plans"].items() if p["price"] == price), None)
        if plan and plan != row.plan:
            self._set(row.id, plan=plan)
            if row.status == "active":
                self._licence(row.id, "upgrade")

    def _canceled(self, obj: dict) -> None:
        row = self._by(signups.c.stripe_subscription, obj.get("id"))
        if row is not None:
            self._set(row.id, status="canceled")       # nothing renews it: the licence runs out
            self.mail(row.email, "Your Cairn subscription has ended",
                      f"{row.tenant}.{self.cfg['domain']} stays available until {row.paid_until}, then becomes "
                      "read-only after the licence's grace period. Your data is kept.")

    def _payment_failed(self, obj: dict) -> None:
        row = self._by(signups.c.stripe_customer, obj.get("customer"))
        if row is not None:
            self.mail(row.email, "A Cairn payment did not go through",
                      f"We could not take the payment for {row.tenant}.{self.cfg['domain']}. Stripe will try again; "
                      f"update your card from the billing portal ({self.cfg['public_url'].rstrip('/')}/signup/billing).")

    # ---- provisioning ---------------------------------------------------------------------------
    def _issue(self, row) -> tuple[str, str]:
        from cryptography.hazmat.primitives import serialization

        from cairn.common.licence import issue

        with open(self.cfg["licence_key_file"], "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        plan = self.cfg["plans"][row.plan]
        lid = f"lic_{secrets.token_hex(8)}"
        text = issue(key, self.cfg["licence_kid"], id=lid, customer=row.organization, tenant=row.tenant,
                     edition=plan["edition"], features=list(plan.get("features") or []), max_users=plan["max_users"],
                     issued_at=date.today().isoformat(), expires_at=row.paid_until,
                     grace_days=int(self.cfg.get("grace_days", 14)))
        return lid, text

    def _run(self, action: str, tenant: str, licence: str) -> None:
        d = tempfile.mkdtemp(prefix="cairn-licence-")
        path = os.path.join(d, f"{tenant}.licence")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(licence)
            cmd = [*self.cfg.get("provision", ["deploy/hosting/tenant.sh"]), action, tenant, "--licence", path]
            env = dict(os.environ)
            if self.cfg.get("metering_key_file") and action == "create":
                from cairn.hosting.metering import tenant_key

                kpath = os.path.join(d, f"{tenant}.metering")
                fd = os.open(kpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(tenant_key(_read(self.cfg["metering_key_file"]) or "", tenant))
                cmd += ["--metering-key", kpath]
                env["CAIRN_METERING_URL"] = self.cfg["public_url"]
            r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                               timeout=int(self.cfg.get("provision_timeout", 1800)))
            if r.returncode != 0:
                raise SignupError(f"provisioning {tenant} ({action}) failed: {(r.stderr or r.stdout)[-1500:]}", 502)
        finally:
            for name in os.listdir(d):
                os.remove(os.path.join(d, name))
            os.rmdir(d)

    def _licence(self, sid: str, action: str) -> None:
        row = self._by(signups.c.id, sid)
        lid, text = self._issue(row)
        self._run(action, row.tenant, text)
        self._set(sid, licence_id=lid, error=None)
        if self.audit is not None:
            self.audit.record("hosting", f"tenant.licence_{'issued' if action == 'create' else 'renewed'}",
                              "urn:li:corpuser:cairn-signup", f"tenant:{row.tenant}",
                              {"licence": lid, "until": row.paid_until, "plan": row.plan})

    def provision(self, sid: str) -> None:
        row = self._by(signups.c.id, sid)
        try:
            self._licence(sid, "create")
        except Exception as e:  # noqa: BLE001 - kept as failed, retried by the next tick
            self._set(sid, status="failed", error=str(e)[:4000])
            logger.warning("provisioning %s failed: %s", row.tenant, e)
            return
        self._set(sid, status="active")
        self.mail(row.email, f"Your Cairn workspace {row.tenant} is ready",
                  f"Sign in at https://{row.tenant}.{self.cfg['domain']} with {row.email}.")

    def retry_failed(self) -> int:
        with self.engine.connect() as conn:
            ids = [r.id for r in conn.execute(select(signups.c.id).where(signups.c.status == "failed"))]
        for sid in ids:
            self.provision(sid)
        return len(ids)

    # ---- 4. the billing portal ------------------------------------------------------------------
    def portal_link(self, email: str, client_ip: str) -> dict:
        email = (email or "").strip().lower()
        self._limit(f"ip:{client_ip}", 10)
        self._limit(f"portal:{email}", 5)
        with self.engine.connect() as conn:
            rows = conn.execute(select(signups).where(signups.c.email == email, signups.c.stripe_customer.is_not(None),
                                                      signups.c.status.in_(("active", "paid", "failed", "canceled")))).all()
        for row in rows:                                      # silent either way: no account fishing
            token = secrets.token_urlsafe(32)
            self._set(row.id, token_hash=_hash(token), token_expires_ms=_now() + 3_600_000)
            self.mail(email, f"Manage the billing of {row.tenant}",
                      f"{self.cfg['public_url'].rstrip('/')}/v1/billing/portal?token={token}\n\nThe link works once, for an hour.")
        return {"message": f"if {email} owns a workspace, we sent it a link"}

    def portal(self, token: str) -> str:
        now = _now()
        row = self._by(signups.c.token_hash, _hash(token or ""))
        if row is None or (row.token_expires_ms or 0) < now or not row.stripe_customer or row.status == "pending":
            raise SignupError("this link has expired or was used", 410)
        self._set(row.id, token_hash=None, token_expires_ms=None)
        return self.stripe.post("/v1/billing_portal/sessions", {
            "customer": row.stripe_customer, "return_url": f"https://{row.tenant}.{self.cfg['domain']}"})["url"]



def build_signup_router(svc: Signup) -> Any:
    from fastapi import APIRouter, Body, HTTPException
    from fastapi.responses import RedirectResponse

    router = APIRouter(tags=["hosting"])

    def ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def wrap(fn):
        try:
            return fn()
        except SignupError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/v1/signup", status_code=202)
    def start(request: Request, body: dict = Body(...)):
        return wrap(lambda: svc.start(body, ip(request)))

    @router.get("/v1/signup/available/{tenant}")
    def available(tenant: str):
        t = tenant.lower()
        ok = bool(TENANT.match(t)) and t not in RESERVED and svc._by(signups.c.tenant, t) is None
        return {"tenant": t, "available": ok}

    @router.get("/v1/signup/verify")
    def verify(token: str):
        return RedirectResponse(wrap(lambda: svc.verify(token)), status_code=303)

    @router.post("/v1/billing/stripe/webhook")
    async def webhook(request: Request):
        payload = await request.body()
        from starlette.concurrency import run_in_threadpool

        try:
            return await run_in_threadpool(svc.webhook, payload, request.headers.get("stripe-signature", ""))
        except SignupError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/v1/billing/portal-link", status_code=202)
    def portal_link(request: Request, email: str = Body(..., embed=True)):
        return wrap(lambda: svc.portal_link(email, ip(request)))

    @router.get("/v1/billing/portal")
    def portal(token: str):
        return RedirectResponse(wrap(lambda: svc.portal(token)), status_code=303)

    return router
