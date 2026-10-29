"""Notification hub: one ``publish`` call → in-app inbox + Slack / Teams / email / webhooks.

Producers (marketplace, workflows, quality, discussions, context agents) publish domain events;
the hub resolves recipients (explicit users, asset followers, asset owners, domain routes),
applies per-user channel settings, persists one row per (recipient, channel) and lets the
``cairn notifier`` worker deliver them with retries. In-app rows are delivered immediately.

Actionable events carry ``actions`` (e.g. approve/reject). Chat surfaces render them as
buttons (Slack) or signed one-click links (Teams/email); both dispatch through
:meth:`NotificationHub.execute_action`, which re-authorizes the *acting* user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, Integer, String, Table, and_, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from cairn.common import metrics
from cairn.common.audit import AuditLog
from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

EVENT_TYPES = [
    "access.requested", "access.granted", "access.rejected", "access.expiring", "access.revoked",
    "certification.requested", "certification.decided", "asset.deprecated",
    "quality.incident.raised", "quality.incident.resolved", "discussion.mention", "discussion.reply",
    "context.drift", "subscription.update", "form.assigned", "form.due", "contract.broken", "contract.restored",
    "automation.applied", "asset.deprecation_proposed", "ai_system.review_requested", "ai_system.decided",
    "product.listing_outdated",
]
CHANNELS = ("in_app", "slack", "teams", "email", "webhook")

notifications = Table(
    "cairn_notifications", metadata,
    Column("id", String(40), primary_key=True),
    Column("type", String(64), nullable=False, index=True),
    Column("recipient", String(512), nullable=False, index=True),
    Column("channel", String(16), nullable=False),
    Column("target", String(1024), nullable=True),
    Column("title", String(500), nullable=False),
    Column("body", String(4000), nullable=False),
    Column("entity_urn", String(1024), nullable=True),
    Column("payload", JSONText, nullable=True),
    Column("status", String(16), nullable=False, index=True),  # PENDING|SENT|FAILED|SKIPPED
    Column("attempts", Integer, nullable=False, default=0),
    Column("error", String(1000), nullable=True),
    Column("created_at_ms", BigInteger, nullable=False, index=True),
    Column("read_at_ms", BigInteger, nullable=True),
    Column("external_ref", String(512), nullable=True),
)
notify_settings = Table(
    "cairn_notify_settings", metadata,
    Column("actor", String(512), primary_key=True),
    Column("doc", JSONText, nullable=False),
)
notify_routes = Table(
    "cairn_notify_routes", metadata,
    Column("id", String(64), primary_key=True),
    Column("doc", JSONText, nullable=False),
)
action_tokens = Table(
    "cairn_action_tokens", metadata,
    Column("jti", String(40), primary_key=True),
    Column("used_at_ms", BigInteger, nullable=False),
)


class ChannelSettings(BaseModel):
    enabled: bool = False
    user_id: Optional[str] = None       # Slack member id
    webhook_url: Optional[str] = None   # Teams personal webhook (Workflows)


class NotifySettings(BaseModel):
    channels: dict[str, ChannelSettings] = Field(default_factory=lambda: {
        "in_app": ChannelSettings(enabled=True), "slack": ChannelSettings(enabled=True),
        "teams": ChannelSettings(enabled=False), "email": ChannelSettings(enabled=False)})
    events: dict[str, bool] = Field(default_factory=lambda: {t: True for t in EVENT_TYPES})


class Route(BaseModel):
    """Channel route, e.g. every quality incident in the Finance domain → #finance-data."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,62}$")
    event_types: list[str] = Field(default_factory=list, description="empty = all")
    domains: list[str] = Field(default_factory=list, description="empty = all domains")
    channel: str = Field(pattern=r"^(slack|teams|webhook|ticket)$")
    target: str = Field(description="Slack channel id/name, Teams or generic webhook URL, or a ticketing system id")


@dataclass
class Action:
    action: str            # approve | reject | ack | accept | dismiss | open
    kind: str              # access_request | workflow | incident | finding
    object_id: str
    label: str
    style: str = "default"  # primary | danger | default


@dataclass
class Event:
    type: str
    title: str
    body: str
    actor: Optional[str] = None
    entity_urn: Optional[str] = None
    recipients: list[str] = field(default_factory=list)
    include_followers: bool = False
    include_owners: bool = False
    url: Optional[str] = None
    actions: list[Action] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


ActionHandler = Callable[[str, str, str, Optional[str]], str]   # (action, object_id, actor, comment) → message


def _now_ms() -> int:
    return int(time.time() * 1000)


class NotificationHub:
    def __init__(self, engine: Engine, public_url: str = "http://localhost:9000", link_secret: Optional[str] = None,
                 audit: Optional[AuditLog] = None, followers: Optional[Callable[[str], list[str]]] = None,
                 owners: Optional[Callable[[str], list[str]]] = None,
                 domains_of: Optional[Callable[[str], list[str]]] = None):
        self.engine = engine
        self.public_url = public_url.rstrip("/")
        self.link_secret = (link_secret or "").encode()
        self.audit = audit
        self.followers = followers or (lambda urn: [])
        self.owners = owners or (lambda urn: [])
        self.domains_of = domains_of or (lambda urn: [])
        self.action_handlers: dict[str, ActionHandler] = {}
        self.channels: dict[str, Any] = {}   # channel name → sender (see channels.py)
        # Routes owned by something else (installed extensions); not editable through the routes API.
        self.route_providers: list[Callable[[], list[Route]]] = []
        # Called with every published event (automations queue the ones they trigger on).
        self.listeners: list[Callable[["Event"], None]] = []

    # ---- configuration --------------------------------------------------------------
    def register_action(self, kind: str, handler: ActionHandler) -> None:
        self.action_handlers[kind] = handler

    def get_settings(self, actor: str) -> NotifySettings:
        with self.engine.connect() as conn:
            row = conn.execute(select(notify_settings).where(notify_settings.c.actor == actor)).first()
        if not row:
            return NotifySettings()
        merged = NotifySettings()
        stored = NotifySettings.model_validate(row.doc)
        merged.channels.update(stored.channels)
        merged.events.update(stored.events)
        return merged

    def put_settings(self, actor: str, settings: NotifySettings) -> NotifySettings:
        from cairn.notify.channels import teams_webhook_allowed

        teams = settings.channels.get("teams")
        if teams and teams.webhook_url and not teams_webhook_allowed(teams.webhook_url):
            raise ValueError("The Teams webhook URL must be an https address on a Microsoft webhook host "
                             "(from Teams: Workflows → 'Post to a chat when a webhook request is received').")
        doc = settings.model_dump(mode="json")
        with self.engine.begin() as conn:
            if conn.execute(select(notify_settings.c.actor).where(notify_settings.c.actor == actor)).first():
                conn.execute(update(notify_settings).where(notify_settings.c.actor == actor).values(doc=doc))
            else:
                conn.execute(notify_settings.insert().values(actor=actor, doc=doc))
        return settings

    def slack_user_for(self, actor: str) -> Optional[str]:
        return self.get_settings(actor).channels.get("slack", ChannelSettings()).user_id

    def actor_for_slack_user(self, slack_user_id: str) -> Optional[str]:
        with self.engine.connect() as conn:
            for row in conn.execute(select(notify_settings)):
                if ((row.doc.get("channels") or {}).get("slack") or {}).get("user_id") == slack_user_id:
                    return row.actor
        return None

    def list_routes(self) -> list[Route]:
        with self.engine.connect() as conn:
            return [Route.model_validate(r.doc) for r in conn.execute(select(notify_routes))]

    def put_routes(self, routes: list[Route]) -> list[Route]:
        from cairn.notify.channels import teams_webhook_allowed

        for r in routes:
            if r.channel == "teams" and not teams_webhook_allowed(r.target):
                raise ValueError(f"route {r.id}: Teams target must be an https Microsoft webhook URL")
        with self.engine.begin() as conn:
            conn.execute(notify_routes.delete())
            for r in routes:
                conn.execute(notify_routes.insert().values(id=r.id, doc=r.model_dump(mode="json")))
        return routes

    # ---- publishing -----------------------------------------------------------------
    def publish(self, event: Event) -> list[str]:
        if event.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event.type}")
        for listen in self.listeners:
            try:
                listen(event)
            except Exception:  # noqa: BLE001 - a listener never stops a notification
                logger.exception("event listener failed for %s", event.type)
        recipients: list[str] = list(dict.fromkeys(event.recipients))
        if event.entity_urn and event.include_followers:
            recipients += [r for r in self.followers(event.entity_urn) if r not in recipients]
        if event.entity_urn and event.include_owners:
            recipients += [r for r in self.owners(event.entity_urn) if r not in recipients]
        # Never notify people about their own action.
        recipients = [r for r in recipients if r != event.actor]
        url = event.url or (f"{self.public_url}/cairn/asset/{event.entity_urn}" if event.entity_urn else None)
        payload = {**event.payload, "url": url, "actor": event.actor,
                   "actions": [a.__dict__ for a in event.actions]}
        now = _now_ms()
        ids = []
        rows = []
        for r in recipients:
            settings = self.get_settings(r)
            if not settings.events.get(event.type, True):
                continue
            for ch in CHANNELS:
                cs = settings.channels.get(ch)
                if ch == "webhook" or not cs or not cs.enabled:
                    continue
                target = cs.user_id if ch == "slack" else cs.webhook_url if ch == "teams" else None
                if ch == "teams" and not target:
                    continue  # Teams DMs need the user's own Workflows webhook
                rows.append(dict(recipient=r, channel=ch, target=target,
                                 status="SENT" if ch == "in_app" else "PENDING"))
        domains = set(self.domains_of(event.entity_urn)) if event.entity_urn else set()
        for route in [*self.list_routes(), *(r for provide in self.route_providers for r in provide())]:
            if route.event_types and event.type not in route.event_types:
                continue
            if route.domains and not domains & set(route.domains):
                continue
            rows.append(dict(recipient=f"route:{route.id}", channel=route.channel, target=route.target,
                             status="PENDING"))
        with self.engine.begin() as conn:
            for row in rows:
                nid = f"ntf_{uuid.uuid4().hex[:20]}"
                ids.append(nid)
                conn.execute(notifications.insert().values(
                    id=nid, type=event.type, title=event.title[:500], body=event.body[:4000],
                    entity_urn=event.entity_urn, payload=payload, attempts=0, created_at_ms=now, **row))
        return ids

    # ---- inbox -------------------------------------------------------------------------
    def inbox(self, actor: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
        stmt = (select(notifications).where(and_(notifications.c.recipient == actor, notifications.c.channel == "in_app"))
                .order_by(notifications.c.created_at_ms.desc()).limit(limit))
        if unread_only:
            stmt = stmt.where(notifications.c.read_at_ms.is_(None))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [{"id": r.id, "type": r.type, "title": r.title, "body": r.body, "entity_urn": r.entity_urn,
                 "created_at": _iso(r.created_at_ms), "read_at": _iso(r.read_at_ms) if r.read_at_ms else None,
                 "action_url": (r.payload or {}).get("url")} for r in rows]

    def unread_count(self, actor: str) -> int:
        return len(self.inbox(actor, unread_only=True, limit=1000))

    def mark_read(self, actor: str, ids: list[str]) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(update(notifications).where(and_(
                notifications.c.recipient == actor, notifications.c.id.in_(ids or [""]),
                notifications.c.read_at_ms.is_(None))).values(read_at_ms=_now_ms()))
        return res.rowcount

    # ---- delivery ----------------------------------------------------------------------
    def dispatch_pending(self, limit: int = 200, max_attempts: int = 5) -> dict[str, int]:
        stats = {"sent": 0, "failed": 0, "skipped": 0}
        with self.engine.connect() as conn:
            rows = conn.execute(select(notifications).where(notifications.c.status == "PENDING")
                                .order_by(notifications.c.created_at_ms).limit(limit)).fetchall()
        for r in rows:
            sender = self.channels.get(r.channel)
            if sender is None:
                self._set(r.id, status="SKIPPED", error=f"channel {r.channel} not configured")
                stats["skipped"] += 1
                metrics.NOTIFICATIONS.labels(r.channel, "skipped").inc()
                continue
            try:
                ref = sender.send(dict(r._mapping), self)
                self._set(r.id, status="SENT", attempts=r.attempts + 1, external_ref=ref)
                stats["sent"] += 1
                metrics.NOTIFICATIONS.labels(r.channel, "sent").inc()
            except Exception as e:
                attempts = r.attempts + 1
                self._set(r.id, status="FAILED" if attempts >= max_attempts else "PENDING", attempts=attempts,
                          error=str(e)[:1000])
                stats["failed"] += 1
                metrics.NOTIFICATIONS.labels(r.channel, "failed").inc()
                logger.warning("notification %s via %s failed: %s", r.id, r.channel, e)
        with self.engine.connect() as conn:
            metrics.NOTIFICATIONS_PENDING.set(conn.execute(select(func.count()).select_from(notifications)
                                                           .where(notifications.c.status == "PENDING")).scalar() or 0)
        return stats

    def _set(self, nid: str, **values: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(notifications).where(notifications.c.id == nid).values(**values))

    # ---- actions -----------------------------------------------------------------------
    def sign_action(self, action: Action, recipient: str, ttl_seconds: int = 72 * 3600) -> str:
        if not self.link_secret:
            raise RuntimeError("CAIRN_ACTION_LINK_SECRET is not configured")
        claims = {"a": action.action, "k": action.kind, "o": action.object_id, "r": recipient,
                  "exp": int(time.time()) + ttl_seconds, "jti": uuid.uuid4().hex}
        body = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(hmac.new(self.link_secret, body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return f"{body}.{sig}"

    def action_link(self, action: Action, recipient: str) -> str:
        return f"{self.public_url}/cairn/api/notify/actions/{self.sign_action(action, recipient)}"

    def verify_action(self, token: str) -> dict:
        try:
            body, sig = token.split(".")
        except ValueError:
            raise PermissionError("malformed action token")
        expected = base64.urlsafe_b64encode(hmac.new(self.link_secret, body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not self.link_secret or not hmac.compare_digest(expected, sig):
            raise PermissionError("invalid action token signature")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if claims["exp"] < time.time():
            raise PermissionError("action link expired")
        return claims

    def consume_action_token(self, token: str, actor: str, comment: Optional[str] = None) -> str:
        """Execute a signed one-click action. The logged-in ``actor`` must be the intended recipient."""
        claims = self.verify_action(token)
        if claims["r"] != actor:
            raise PermissionError("this link was issued to a different user")
        try:
            with self.engine.begin() as conn:
                conn.execute(action_tokens.insert().values(jti=claims["jti"], used_at_ms=_now_ms()))
        except IntegrityError:
            raise PermissionError("this link has already been used")
        return self.execute_action(claims["k"], claims["a"], claims["o"], actor, comment, channel="link")

    def execute_action(self, kind: str, action: str, object_id: str, actor: str, comment: Optional[str] = None,
                       channel: str = "in_app") -> str:
        handler = self.action_handlers.get(kind)
        if handler is None:
            raise ValueError(f"no handler for action kind {kind}")
        message = handler(action, object_id, actor, comment)
        if self.audit:
            self.audit.record("notify", f"action.{kind}.{action}", actor, object_id, {"channel": channel})
        return message


def _iso(ms: Optional[int]) -> Optional[str]:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None
