"""Two-way ticketing: Jira and ServiceNow for approvals, PagerDuty and Opsgenie for incidents.

Systems are reviewed YAML files in ``config/ticketing`` (or ``CAIRN_TICKETING``); ``${ENV}`` values
are read from the environment, so secrets stay out of the files::

    systems:
      - {id: jira-it, type: jira, url: https://acme.atlassian.net, project: DATA, user: ${JIRA_USER},
         token: ${JIRA_TOKEN}, inbound_secret: ${JIRA_WEBHOOK_SECRET}}
      - {id: snow, type: servicenow, url: https://acme.service-now.com, table: sc_request,
         user: ${SNOW_USER}, password: ${SNOW_PASSWORD}, inbound_secret: ${SNOW_WEBHOOK_SECRET}}
      - {id: pagerduty, type: pagerduty, routing_key: ${PD_ROUTING_KEY}, min_severity: HIGH}
      - {id: opsgenie, type: opsgenie, api_key: ${OPSGENIE_KEY}, min_severity: HIGH}

A notification route with ``channel: ticket`` and ``target: <system id>`` sends matching events
there. Outbound: an access request opens a Jira issue or ServiceNow request; a quality incident
opens a PagerDuty or Opsgenie incident and resolves it when the monitor recovers. Inbound: the
ticket's webhook (``POST /cairn/api/notify/inbound/<system id>``) approves or rejects the Cairn
request as the person who decided there, mapped by email, through Cairn's own approval rules.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Any, Callable, Literal, Optional

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, String, Table, and_, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

ticket_links = Table(
    "cairn_ticket_links", metadata,
    Column("system_id", String(64), primary_key=True),
    Column("external_id", String(128), primary_key=True),
    Column("kind", String(32), nullable=False),            # access_request | incident | …
    Column("object_id", String(512), nullable=False, index=True),
    Column("state", String(16), nullable=False),           # OPEN | APPROVED | REJECTED | RESOLVED
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
PD_SEVERITY = {"LOW": "info", "MEDIUM": "warning", "HIGH": "error", "CRITICAL": "critical"}
OG_PRIORITY = {"LOW": "P4", "MEDIUM": "P3", "HIGH": "P2", "CRITICAL": "P1"}
_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class System(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{0,62}$")
    type: Literal["jira", "servicenow", "pagerduty", "opsgenie"]
    url: Optional[str] = None
    project: Optional[str] = None                 # jira
    issue_type: str = "Task"                      # jira
    table: str = "sc_request"                     # servicenow
    user: Optional[str] = None
    token: Optional[str] = None                   # jira API token
    password: Optional[str] = None                # servicenow
    routing_key: Optional[str] = None             # pagerduty
    api_key: Optional[str] = None                 # opsgenie
    min_severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    approve_statuses: list[str] = Field(default_factory=lambda: ["Done", "Approved", "Resolved"])
    reject_statuses: list[str] = Field(default_factory=lambda: ["Won't Do", "Rejected", "Declined"])
    inbound_secret: Optional[str] = None

    def base(self) -> str:
        default = {"pagerduty": "https://events.pagerduty.com", "opsgenie": "https://api.opsgenie.com"}
        return (self.url or default.get(self.type, "")).rstrip("/")


def load_systems(path: Optional[str] = None) -> dict[str, System]:
    import yaml

    path = path or os.environ.get("CAIRN_TICKETING", "/etc/cairn/config/ticketing")
    if not path or not os.path.exists(path):
        return {}
    files = [path] if os.path.isfile(path) else sorted(
        os.path.join(path, f) for f in os.listdir(path) if f.endswith((".yaml", ".yml")) and ".example." not in f)
    out: dict[str, System] = {}
    for f in files:
        with open(f) as fh:
            for raw in (yaml.safe_load(fh) or {}).get("systems") or []:
                s = System.model_validate({k: _expand(v) for k, v in raw.items()})
                out[s.id] = s
    return out


def _now() -> int:
    return int(time.time() * 1000)


def _subject(row: dict) -> tuple[Optional[str], Optional[str]]:
    """(kind, object id) an event is about: its first action, or the monitor of a quality incident."""
    payload = row.get("payload") or {}
    for a in payload.get("actions") or []:
        if a.get("kind") and a.get("object_id"):
            return a["kind"], a["object_id"]
    if payload.get("request_id"):
        return "access_request", payload["request_id"]
    if payload.get("monitor_id"):
        return "incident", payload["monitor_id"]
    return None, None


class TicketSender:
    """The ``ticket`` notification channel: ``row['target']`` names the system."""

    def __init__(self, engine: Engine, systems: Callable[[], dict[str, System]] = load_systems,
                 http: Optional[httpx.Client] = None):
        self.engine = engine
        self.systems = systems
        self.http = http or httpx.Client(timeout=20)

    def _link(self, system_id: str, kind: str, object_id: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(ticket_links).where(and_(
                ticket_links.c.system_id == system_id, ticket_links.c.kind == kind,
                ticket_links.c.object_id == object_id)).order_by(ticket_links.c.created_ms.desc())).first()
        return dict(r._mapping) if r else None

    def _save(self, system_id: str, external_id: str, kind: str, object_id: str, state: str = "OPEN") -> None:
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(ticket_links.insert().values(system_id=system_id, external_id=external_id, kind=kind,
                                                      object_id=object_id, state=state, created_ms=now, updated_ms=now))

    def _state(self, system_id: str, external_id: str, state: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(ticket_links).where(and_(ticket_links.c.system_id == system_id,
                                                         ticket_links.c.external_id == external_id))
                         .values(state=state, updated_ms=_now()))

    def send(self, row: dict, hub=None) -> str:
        system = self.systems().get(row.get("target") or "")
        if system is None:
            raise RuntimeError(f"ticketing system {row.get('target')!r} is not configured")
        kind, object_id = _subject(row)
        if system.type in ("pagerduty", "opsgenie"):
            return self._incident(system, row, object_id or row.get("entity_urn") or row["id"])
        if not kind:
            return "skipped: nothing to track"
        existing = self._link(system.id, kind, object_id)
        url = (row.get("payload") or {}).get("url")
        if existing:      # one ticket per request; later events about it become comments
            self._comment(system, existing["external_id"], f"{row['title']}\n\n{row['body']}")
            return existing["external_id"]
        external = self._create(system, row, kind, object_id, url)
        self._save(system.id, external, kind, object_id)
        return external

    # ---- approvals ------------------------------------------------------------------------
    def _create(self, system: System, row: dict, kind: str, object_id: str, url: Optional[str]) -> str:
        text = f"{row['body']}\n\nDecide in this ticket, or in Cairn: {url or ''}\nCairn reference: {kind}:{object_id}"
        if system.type == "jira":
            r = self.http.post(f"{system.base()}/rest/api/3/issue", auth=(system.user or "", system.token or ""), json={
                "fields": {"project": {"key": system.project}, "summary": row["title"][:250],
                           "issuetype": {"name": system.issue_type}, "labels": ["cairn", kind.replace("_", "-")],
                           "description": {"type": "doc", "version": 1, "content": [
                               {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}}})
            r.raise_for_status()
            return r.json()["key"]
        r = self.http.post(f"{system.base()}/api/now/table/{system.table}", auth=(system.user or "", system.password or ""),
                           json={"short_description": row["title"][:160], "description": text,
                                 "correlation_id": f"cairn:{kind}:{object_id}", "correlation_display": "Cairn"})
        r.raise_for_status()
        return r.json()["result"]["sys_id"]

    def _comment(self, system: System, external_id: str, text: str) -> None:
        try:
            if system.type == "jira":
                self.http.post(f"{system.base()}/rest/api/3/issue/{external_id}/comment",
                               auth=(system.user or "", system.token or ""),
                               json={"body": {"type": "doc", "version": 1, "content": [
                                   {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}}).raise_for_status()
            elif system.type == "servicenow":
                self.http.patch(f"{system.base()}/api/now/table/{system.table}/{external_id}",
                                auth=(system.user or "", system.password or ""), json={"work_notes": text}).raise_for_status()
        except httpx.HTTPError as e:   # a comment is best effort; the ticket already exists
            logger.warning("comment on %s %s failed: %s", system.id, external_id, e)

    # ---- incidents ------------------------------------------------------------------------
    def _incident(self, system: System, row: dict, key: str) -> str:
        payload = row.get("payload") or {}
        severity = str(payload.get("severity") or "MEDIUM").upper()
        resolved = row["type"].endswith(".resolved")
        if not resolved and SEVERITY_RANK.get(severity, 1) < SEVERITY_RANK[system.min_severity]:
            return f"skipped: {severity} is below {system.min_severity}"
        alias = f"cairn-{key}"[:250]
        if system.type == "pagerduty":
            body: dict[str, Any] = {"routing_key": system.routing_key, "dedup_key": alias,
                                    "event_action": "resolve" if resolved else "trigger"}
            if not resolved:
                body["payload"] = {"summary": row["title"][:1000], "source": row.get("entity_urn") or "cairn",
                                   "severity": PD_SEVERITY.get(severity, "warning"),
                                   "custom_details": {"details": row["body"], "url": payload.get("url")}}
                body["links"] = [{"href": payload["url"], "text": "Open in Cairn"}] if payload.get("url") else []
            r = self.http.post(f"{system.base()}/v2/enqueue", json=body)
            r.raise_for_status()
            return alias
        headers = {"Authorization": f"GenieKey {system.api_key}"}
        if resolved:
            r = self.http.post(f"{system.base()}/v2/alerts/{alias}/close", params={"identifierType": "alias"},
                               headers=headers, json={"note": row["body"][:1000], "source": "Cairn"})
        else:
            r = self.http.post(f"{system.base()}/v2/alerts", headers=headers, json={
                "message": row["title"][:130], "alias": alias, "description": row["body"][:15000],
                "priority": OG_PRIORITY.get(severity, "P3"), "source": "Cairn", "tags": ["cairn", "data-quality"],
                "details": {"url": payload.get("url") or "", "dataset": row.get("entity_urn") or ""}})
        r.raise_for_status()
        return alias


class InboundError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def verify_inbound(system: System, raw: bytes, secret: Optional[str], signature: Optional[str]) -> None:
    """A shared secret (``?secret=`` or ``X-Cairn-Secret``) or an HMAC-SHA256 signature of the body
    (``X-Hub-Signature: sha256=…``, as Jira sends it) must match the system's ``inbound_secret``."""
    if not system.inbound_secret:
        raise InboundError("this system has no inbound_secret; inbound updates are off", 403)
    if secret and hmac.compare_digest(secret, system.inbound_secret):
        return
    if signature and signature.startswith("sha256="):
        want = hmac.new(system.inbound_secret.encode(), raw, hashlib.sha256).hexdigest()
        if hmac.compare_digest(want, signature.split("=", 1)[1]):
            return
    raise InboundError("bad inbound secret or signature", 401)


def parse_inbound(system: System, body: dict) -> tuple[str, Optional[str], Optional[str], str]:
    """(external id, decision approve|reject|None, decider email, comment) from a webhook body."""
    if system.type == "jira":
        issue = body.get("issue") or {}
        status = str(((issue.get("fields") or {}).get("status") or {}).get("name") or "")
        decision = ("approve" if status in system.approve_statuses else
                    "reject" if status in system.reject_statuses else None)
        email = (body.get("user") or {}).get("emailAddress")
        comment = ((body.get("comment") or {}).get("body") if isinstance((body.get("comment") or {}).get("body"), str)
                   else f"{status} in Jira {issue.get('key')}")
        return str(issue.get("key") or ""), decision, email, comment
    if system.type == "servicenow":
        approval = str(body.get("approval") or body.get("state") or "").lower()
        decision = "approve" if approval in ("approved", "approve") else "reject" if approval in ("rejected", "reject") else None
        return (str(body.get("sys_id") or ""), decision, body.get("approver_email") or body.get("email"),
                body.get("comment") or f"{approval} in ServiceNow {body.get('number') or ''}".strip())
    raise InboundError(f"{system.type} does not send decisions to Cairn")


class TicketingService:
    def __init__(self, engine: Engine, hub, actor_for_email: Callable[[str], Optional[str]],
                 systems: Callable[[], dict[str, System]] = load_systems, http: Optional[httpx.Client] = None,
                 audit=None):
        self.engine = engine
        self.hub = hub
        self.actor_for_email = actor_for_email
        self.systems = systems
        self.sender = TicketSender(engine, systems, http)
        self.audit = audit

    def overview(self) -> list[dict]:
        out = []
        for s in self.systems().values():
            with self.engine.connect() as conn:
                n = conn.execute(select(ticket_links.c.state).where(ticket_links.c.system_id == s.id)).all()
            out.append({"id": s.id, "type": s.type, "url": s.base(), "inbound": bool(s.inbound_secret),
                        "tickets": len(n), "open": sum(1 for (st,) in n if st == "OPEN")})
        return out

    def inbound(self, system_id: str, raw: bytes, body: dict, secret: Optional[str], signature: Optional[str]) -> dict:
        system = self.systems().get(system_id)
        if system is None:
            raise InboundError("unknown system", 404)
        verify_inbound(system, raw, secret, signature)
        external, decision, email, comment = parse_inbound(system, body)
        if not decision:
            return {"ignored": "no decision in this update"}
        with self.engine.connect() as conn:
            link = conn.execute(select(ticket_links).where(and_(ticket_links.c.system_id == system.id,
                                                                ticket_links.c.external_id == external))).first()
        if link is None:
            raise InboundError(f"{external} is not a Cairn ticket", 404)
        if link.state != "OPEN":
            return {"ignored": f"already {link.state.lower()}"}
        actor = self.actor_for_email(email) if email else None
        if not actor:
            raise InboundError(f"no Cairn user has the email {email!r}; decide in Cairn instead", 403)
        handler = self.hub.action_handlers.get(link.kind)
        if handler is None:
            raise InboundError(f"cannot act on {link.kind}", 409)
        try:
            message = handler(decision, link.object_id, actor, comment)
        except PermissionError as e:
            raise InboundError(str(e), 403)
        except Exception as e:  # noqa: BLE001 - e.g. already decided in Cairn
            raise InboundError(str(e), 409)
        self.sender._state(system.id, external, "APPROVED" if decision == "approve" else "REJECTED")
        if self.audit is not None:
            self.audit.record("notify", "ticket.decided", actor, f"{link.kind}:{link.object_id}",
                              {"system": system.id, "ticket": external, "decision": decision})
        return {"ticket": external, "decision": decision, "actor": actor, "message": message}
