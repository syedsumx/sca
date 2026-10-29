"""Privacy operations: requests from data subjects, retention, and the record of processing.

**Subject requests.** A data protection officer (``MANAGE_POLICIES``) opens a request for one person,
identified by email, phone or a customer id. Cairn finds the columns classified for that kind of
identifier (``PII.Email``, ``PII.Phone``, or the ``identifiers`` configured below) and, where a warehouse
connection serves the dataset, counts that person's rows through data access, as the officer. Datasets
without a connection are listed for a manual check.

* An **access** request ends with the report (``GET …/{id}``, or CSV).
* A **deletion** request also gives each dataset with matches (or that could not be checked) to its
  owners, with the ``DELETE`` to review and run: Cairn never deletes data itself. Owners mark their
  part done (or not applicable); the request is complete when every part is. It is due in 30 days
  (``due_days``), owners are reminded, and overdue requests show on the report.

The person's identifier is stored only as a salted hash and a masked hint (``j•••@acme.com``);
statements and reports carry it masked, and the officer sees it once, in the response that opens it.

**Retention.** Policies say how long data with a classification may be kept: ``{"label":
"Classification.PII*", "days": 730}``. Each covered dataset that a connection serves is checked: the oldest
value of its time column (``created_at``, ``event_time``… or ``time_column``) must be newer than the limit.
The notifier checks daily as ``CAIRN_PRIVACY_ACTOR`` (an identity that may read the covered datasets;
without one, the daily check is skipped), and an officer can check now, as themselves. Breaches tell the
owners once and show on the privacy page.

**Record of processing.** ``GET /governance/privacy/ropa`` (and CSV): per domain, the datasets holding
personal data and which kinds, their owners, the purposes allowed to use them, who has access through
grants, the retention that applies, and the AI systems that use them.

Identifier kinds are configurable in ``CAIRN_PRIVACY`` (YAML)::

    identifiers:
      email:       {labels: [Classification.PII.Email], columns: [email, email_address]}
      phone:       {labels: [Classification.PII.Phone], columns: [phone, mobile]}
      customer_id: {labels: [Classification.PII.CustomerId], columns: [customer_id, user_id]}
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, Integer, String, Table, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

privacy_requests = Table(
    "cairn_privacy_requests", metadata,
    Column("id", String(40), primary_key=True),
    Column("kind", String(12), nullable=False),               # access | deletion
    Column("identifier_kind", String(32), nullable=False),
    Column("identifier_hash", String(64), nullable=False, index=True),
    Column("identifier_hint", String(200), nullable=False),
    Column("state", String(16), nullable=False, index=True),  # OPEN | COMPLETED | CLOSED
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("due_ms", BigInteger, nullable=False),
    Column("doc", JSONText, nullable=False),                  # reference, findings, parts
)
retention_policies = Table(
    "cairn_retention_policies", metadata,
    Column("id", String(40), primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
retention_state = Table(
    "cairn_retention_state", metadata,
    Column("dataset", String(700), primary_key=True),
    Column("policy", String(40), primary_key=True),
    Column("state", String(12), nullable=False),              # ok | breach | unchecked
    Column("oldest_ms", BigInteger, nullable=True),
    Column("detail", String(500), nullable=False),
    Column("checked_ms", BigInteger, nullable=False),
    Column("notified", Integer, nullable=False),
)

DEFAULT_IDENTIFIERS = {
    "email": {"labels": ["urn:li:glossaryTerm:Classification.PII.Email"], "columns": ["email", "email_address"]},
    "phone": {"labels": ["urn:li:glossaryTerm:Classification.PII.Phone"], "columns": ["phone", "phone_number", "mobile"]},
    "customer_id": {"labels": ["urn:li:glossaryTerm:Classification.PII.CustomerId"],
                    "columns": ["customer_id", "user_id", "client_id"]},
}
TIME_COLUMNS = ("created_at", "created", "event_time", "event_timestamp", "inserted_at", "updated_at", "ts", "timestamp",
                "date", "event_date", "order_date")
ACTOR = "urn:li:corpuser:cairn-privacy"


class PrivacyError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class RequestIn(BaseModel):
    kind: Literal["access", "deletion"]
    identifier_kind: str = Field(pattern=r"^[a-z_]{2,32}$")
    identifier: str = Field(min_length=1, max_length=300)
    reference: str = Field(default="", max_length=200, description="your ticket or case number")
    due_days: int = Field(default=30, ge=1, le=90)


class RetentionPolicy(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,38}$")
    label: str = Field(description="a tag or glossary term urn, or a classification such as Classification.PII*")
    days: int = Field(ge=1, le=36500)
    time_column: Optional[str] = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    description: str = ""


def mask(value: str) -> str:
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}•••@{domain}"
    return value[:2] + "•" * max(3, len(value) - 4) + value[-2:] if len(value) > 4 else "•" * len(value)


def load_identifiers(path: Optional[str] = None) -> dict[str, dict]:
    path = path or os.environ.get("CAIRN_PRIVACY", "")
    if not path or not os.path.exists(path):
        return DEFAULT_IDENTIFIERS
    import yaml

    with open(path) as fh:
        found = (yaml.safe_load(fh) or {}).get("identifiers") or {}
    out = {}
    for kind, spec in found.items():
        out[kind] = {"labels": [lab if lab.startswith("urn:li:") else f"urn:li:glossaryTerm:{lab}"
                                for lab in spec.get("labels") or []],
                     "columns": [c.lower() for c in spec.get("columns") or []]}
    return out or DEFAULT_IDENTIFIERS


def _label_matches(pattern: str, labels: list[str]) -> bool:
    p = pattern if pattern.startswith("urn:li:") else f"urn:li:glossaryTerm:{pattern}"
    return any(fnmatch.fnmatchcase(lab, p) or lab == p for lab in labels)


class PrivacyService:
    def __init__(self, engine, datasets: Callable[[], list[tuple[str, dict, dict]]], data_access=None,
                 notify=None, audit=None, personas=None, ai_systems=None, salt: str = "",
                 identifiers: Optional[dict] = None):
        """``datasets()``: ``(urn, resource attributes, aspects)`` for every dataset (``Planner.datasets``)."""
        self.engine = engine
        self.datasets = datasets
        self.data = data_access
        self.notify = notify
        self.audit = audit
        self.personas = personas
        self.ai_systems = ai_systems
        self.salt = salt or os.environ.get("CAIRN_PRIVACY_SALT", "cairn-privacy")
        self.identifiers = identifiers or load_identifiers()

    # ---- where a person's data is ------------------------------------------------------------
    def columns_for(self, kind: str) -> list[tuple[str, str, dict]]:
        """``(dataset, column, resource)`` for every column holding this kind of identifier."""
        spec = self.identifiers.get(kind)
        if spec is None:
            raise PrivacyError(f"identifier_kind is one of {sorted(self.identifiers)}")
        out = []
        for urn, res, _aspects in self.datasets():
            for path, labels in (res.get("field_labels") or {}).items():
                if any(lab in spec["labels"] for lab in labels):
                    out.append((urn, path, res))
            labelled = {path for u, path, _r in out if u == urn}
            for path in (res.get("sensitive_fields") or {}):
                leaf = path.split(".")[-1].lower()
                if path not in labelled and leaf in spec["columns"]:
                    out.append((urn, path, res))
        return out

    def _count(self, actor: str, urn: str, column: str, value: str, kind: str = "") -> dict:
        from sqlglot import exp

        if self.data is None:
            return {"searched": False, "reason": "no warehouse connections are configured"}
        from cairn.dataaccess.service import DataAccessError

        try:
            source, parts = self.data.source_for(urn)
        except DataAccessError as e:
            return {"searched": False, "reason": str(e)}
        table_parts = parts[-2:] if source.database or len(parts) < 3 else parts
        col = column.split(".")[-1]
        sql = exp.select(exp.alias_(exp.func("COUNT", exp.Star()), "n")) \
            .from_(exp.table_(table_parts[-1], db=table_parts[-2] if len(table_parts) > 1 else None,
                              catalog=table_parts[-3] if len(table_parts) > 2 else None)) \
            .where(exp.EQ(this=exp.func("LOWER", exp.column(col)), expression=exp.Literal.string(value.lower()))
                   if kind == "email" else exp.EQ(this=exp.column(col), expression=exp.Literal.string(value))) \
            .sql(dialect=source.dialect)
        try:
            got = self.data.query(actor, sql, urn=urn, limit=1)
        except DataAccessError as e:
            return {"searched": False, "reason": str(e)}
        rows = got.get("rows") or [[0]]
        return {"searched": True, "rows": int(rows[0][0] or 0)}

    def _statement(self, urn: str, columns: list[str], hint: str) -> str:
        m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[^,]+\)$", urn)
        table = m.group(1) if m else urn
        cond = " OR ".join(f"{c.split('.')[-1]} = '<{hint}>'" for c in columns)
        return f"DELETE FROM {table} WHERE {cond};  -- review first; replace the placeholder with the person's identifier"

    # ---- requests ------------------------------------------------------------------------------
    def _hash(self, kind: str, value: str) -> str:
        return hashlib.sha256(f"{self.salt}|{kind}|{value.strip().lower()}".encode()).hexdigest()

    def open(self, body: RequestIn, actor: str) -> dict:
        value = body.identifier.strip()
        found = self.columns_for(body.identifier_kind)
        by_ds: dict[str, dict] = {}
        for urn, path, res in found:
            entry = by_ds.setdefault(urn, {"dataset": urn, "columns": [], "owners": res.get("owners") or [],
                                           "domains": res.get("domains") or [], "rows": 0, "searched": True,
                                           "reason": None})
            entry["columns"].append(path)
        for urn, entry in by_ds.items():
            for col in entry["columns"]:
                r = self._count(actor, urn, col, value, body.identifier_kind)
                if r["searched"]:
                    entry["rows"] += r["rows"]
                else:
                    entry["searched"], entry["reason"] = False, r["reason"]
        findings = sorted(by_ds.values(), key=lambda e: (-e["rows"], not e["searched"], e["dataset"]))
        rid, now = f"psr_{uuid.uuid4().hex[:12]}", int(time.time() * 1000)
        hint = mask(value)
        parts = []
        if body.kind == "deletion":
            for e in findings:
                if e["rows"] > 0 or not e["searched"]:
                    parts.append({"dataset": e["dataset"], "owners": e["owners"], "state": "PENDING",
                                  "statement": self._statement(e["dataset"], e["columns"], hint), "note": None,
                                  "done_by": None})
        state = "OPEN" if body.kind == "deletion" and parts else "COMPLETED"
        doc = {"reference": body.reference, "findings": findings, "parts": parts, "reminded_ms": None}
        with self.engine.begin() as conn:
            conn.execute(privacy_requests.insert().values(
                id=rid, kind=body.kind, identifier_kind=body.identifier_kind, identifier_hash=self._hash(body.identifier_kind, value),
                identifier_hint=hint, state=state, created_by=actor,
                created_ms=now, due_ms=now + body.due_days * 86_400_000, doc=doc))
        due = time.strftime("%Y-%m-%d", time.gmtime((now + body.due_days * 86_400_000) / 1000))
        for p in parts:
            self._tell("privacy.deletion_requested", p["owners"], f"Delete one person's data from {_name(p['dataset'])}",
                       f"Subject request {rid}{' (' + body.reference + ')' if body.reference else ''}: delete the rows for "
                       f"{hint} in {_name(p['dataset'])} by {due}, "
                       "then mark your part done in Cairn.", p["dataset"])
        self._audit(f"privacy.{body.kind}_opened", actor, rid, {"kind": body.identifier_kind, "datasets": len(findings),
                                                               "parts": len(parts)})
        return {**self.get(rid), "identifier": value}

    def _public(self, r) -> dict:
        now = int(time.time() * 1000)
        return {"id": r.id, "kind": r.kind, "identifier_kind": r.identifier_kind, "identifier": r.identifier_hint,
                "state": r.state, "created_by": r.created_by, "created_ms": r.created_ms, "due_ms": r.due_ms,
                "overdue": r.state == "OPEN" and now > r.due_ms, **r.doc}

    def get(self, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(privacy_requests).where(privacy_requests.c.id == rid)).first()
        if r is None:
            raise PrivacyError("no such request", 404)
        return self._public(r)

    def list(self, state: Optional[str] = None) -> list[dict]:
        q = select(privacy_requests).order_by(privacy_requests.c.created_ms.desc()).limit(500)
        if state:
            q = q.where(privacy_requests.c.state == state)
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(q)]

    def find(self, kind: str, value: str) -> list[dict]:
        """Earlier requests for the same person (by hash), e.g. to answer a follow-up."""
        h = self._hash(kind, value)
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(select(privacy_requests).where(
                privacy_requests.c.identifier_hash == h))]

    def mark(self, rid: str, dataset: str, state: Literal["DONE", "NOT_APPLICABLE"], actor: str,
             note: str = "", is_officer: bool = False) -> dict:
        with self.engine.begin() as conn:
            r = conn.execute(select(privacy_requests).where(privacy_requests.c.id == rid)).first()
            if r is None:
                raise PrivacyError("no such request", 404)
            doc = dict(r.doc)
            part = next((p for p in doc.get("parts") or [] if p["dataset"] == dataset), None)
            if part is None:
                raise PrivacyError("this request has no part for that dataset", 404)
            if not is_officer and actor not in part["owners"]:
                raise PrivacyError("only the dataset's owners (or a privacy officer) can mark it", 403)
            part.update(state=state, note=note[:500] or None, done_by=actor, done_ms=int(time.time() * 1000))
            complete = all(p["state"] != "PENDING" for p in doc["parts"])
            conn.execute(update(privacy_requests).where(privacy_requests.c.id == rid).values(
                doc=doc, state="COMPLETED" if complete else r.state))
        self._audit("privacy.part_marked", actor, rid, {"dataset": dataset, "state": state})
        if complete:
            self._tell("privacy.request_completed", [r.created_by], f"Subject request {rid} is complete",
                       "Every dataset owner has marked their part.", None)
        return self.get(rid)

    def my_parts(self, actor: str) -> list[dict]:
        out = []
        for r in self.list("OPEN"):
            for p in r.get("parts") or []:
                if actor in p["owners"] and p["state"] == "PENDING":
                    out.append({"request": r["id"], "identifier": r["identifier"], "due_ms": r["due_ms"],
                                "overdue": r["overdue"], **p})
        return out

    def remind(self, now_ms: Optional[int] = None) -> int:
        """Remind owners of pending parts once, a week before the due date."""
        now, n = now_ms or int(time.time() * 1000), 0
        for r in self.list("OPEN"):
            if r.get("reminded_ms") or r["due_ms"] - now > 7 * 86_400_000:
                continue
            for p in r.get("parts") or []:
                if p["state"] == "PENDING":
                    self._tell("privacy.deletion_requested", p["owners"], f"Reminder: subject request {r['id']} is due",
                               f"Delete the rows for {r['identifier']} in {_name(p['dataset'])} and mark it done.",
                               p["dataset"])
                    n += 1
            with self.engine.begin() as conn:
                row = conn.execute(select(privacy_requests.c.doc).where(privacy_requests.c.id == r["id"])).scalar()
                conn.execute(update(privacy_requests).where(privacy_requests.c.id == r["id"]).values(
                    doc={**row, "reminded_ms": now}))
        return n

    def report_csv(self, rid: str) -> str:
        r = self.get(rid)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["request", "identifier", "dataset", "columns", "rows", "searched", "reason", "owners", "part"])
        parts = {p["dataset"]: p["state"] for p in r.get("parts") or []}
        for f in r["findings"]:
            w.writerow([r["id"], r["identifier"], f["dataset"], " ".join(f["columns"]), f["rows"], f["searched"],
                        f["reason"] or "", " ".join(f["owners"]), parts.get(f["dataset"], "")])
        return buf.getvalue()

    # ---- retention -----------------------------------------------------------------------------
    def policies(self) -> list[RetentionPolicy]:
        with self.engine.connect() as conn:
            return [RetentionPolicy.model_validate(r.doc) for r in conn.execute(select(retention_policies))]

    def put_policy(self, p: RetentionPolicy, actor: str) -> RetentionPolicy:
        with self.engine.begin() as conn:
            conn.execute(retention_policies.delete().where(retention_policies.c.id == p.id))
            conn.execute(retention_policies.insert().values(id=p.id, doc=p.model_dump(), updated_ms=int(time.time() * 1000)))
        self._audit("privacy.retention_set", actor, p.id, p.model_dump())
        return p

    def delete_policy(self, pid: str, actor: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(retention_policies.delete().where(retention_policies.c.id == pid))
            conn.execute(retention_state.delete().where(retention_state.c.policy == pid))
        self._audit("privacy.retention_removed", actor, pid, {})

    def _covered(self, policy: RetentionPolicy, res: dict) -> bool:
        labels = list(res.get("all_labels") or []) + [lab for ls in (res.get("field_labels") or {}).values() for lab in ls]
        labels += [f"urn:li:glossaryTerm:Classification.{c}" for c in res.get("classification") or []]
        return _label_matches(policy.label, labels)

    def _time_column(self, policy: RetentionPolicy, aspects: dict) -> Optional[str]:
        fields = [f.get("fieldPath", "") for f in (aspects.get("schemaMetadata") or {}).get("fields") or []]
        if policy.time_column:
            return policy.time_column if any(f.split(".")[-1] == policy.time_column for f in fields) else None
        names = {f.split(".")[-1].lower(): f.split(".")[-1] for f in fields}
        return next((names[c] for c in TIME_COLUMNS if c in names), None)

    def check_retention(self, actor: Optional[str] = None, now_ms: Optional[int] = None) -> dict:
        from sqlglot import exp

        from cairn.dataaccess.service import DataAccessError

        now = now_ms or int(time.time() * 1000)
        stats = {"checked": 0, "breaches": 0, "unchecked": 0}
        actor = actor or os.environ.get("CAIRN_PRIVACY_ACTOR")
        policies = self.policies()
        if not policies or not actor:
            return {**stats, "skipped": not actor and bool(policies)}
        for urn, res, aspects in self.datasets():
            for p in policies:
                if not self._covered(p, res):
                    continue
                col = self._time_column(p, aspects)
                state, oldest, detail = "unchecked", None, ""
                if col is None:
                    detail = "no time column found; set time_column on the policy"
                elif self.data is None:
                    detail = "no warehouse connections are configured"
                else:
                    try:
                        source, parts = self.data.source_for(urn)
                        tp = parts[-2:] if source.database or len(parts) < 3 else parts
                        sql = exp.select(exp.alias_(exp.func("MIN", exp.column(col)), "oldest")).from_(
                            exp.table_(tp[-1], db=tp[-2] if len(tp) > 1 else None,
                                       catalog=tp[-3] if len(tp) > 2 else None)).sql(dialect=source.dialect)
                        got = self.data.query(actor, sql, urn=urn, limit=1)
                        oldest = _to_ms(((got.get("rows") or [[None]])[0] or [None])[0])
                        if oldest is None:
                            state, detail = "ok", "the table is empty"
                        elif now - oldest > p.days * 86_400_000:
                            state = "breach"
                            detail = f"oldest {col} is {(now - oldest) // 86_400_000} days old; the limit is {p.days}"
                        else:
                            state, detail = "ok", f"oldest {col} is {(now - oldest) // 86_400_000} days old"
                    except DataAccessError as e:
                        detail = str(e)[:300]
                stats["checked" if state != "unchecked" else "unchecked"] += 1
                stats["breaches"] += state == "breach"
                self._record(urn, p, state, oldest, detail, res, now)
        return stats

    def _record(self, urn: str, p: RetentionPolicy, state: str, oldest: Optional[int], detail: str, res: dict,
                now: int) -> None:
        with self.engine.begin() as conn:
            prev = conn.execute(select(retention_state).where(retention_state.c.dataset == urn,
                                                              retention_state.c.policy == p.id)).first()
            notified = int(prev.notified) if prev is not None and prev.state == "breach" and state == "breach" else 0
            tell = state == "breach" and not notified
            conn.execute(retention_state.delete().where(retention_state.c.dataset == urn, retention_state.c.policy == p.id))
            conn.execute(retention_state.insert().values(dataset=urn, policy=p.id, state=state, oldest_ms=oldest,
                                                         detail=detail[:500], checked_ms=now, notified=int(tell or notified)))
        if tell:                                          # after the commit: never hold it open across a publish
            self._tell("privacy.retention_breach", res.get("owners") or [], f"{_name(urn)} keeps data too long",
                       f"Retention policy {p.id} ({p.days} days): {detail}.", urn)

    def retention_report(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(retention_state).order_by(retention_state.c.state, retention_state.c.dataset)).all()
        return [{"dataset": r.dataset, "policy": r.policy, "state": r.state, "oldest_ms": r.oldest_ms,
                 "detail": r.detail, "checked_ms": r.checked_ms} for r in rows]

    # ---- record of processing --------------------------------------------------------------------
    def ropa(self) -> dict:
        policies = self.policies()
        purposes = []
        if self.personas is not None:
            try:
                purposes = [{"id": pu.id, "name": pu.name, "tags": list(pu.tags), "enabled": pu.enabled}
                            for pu in self.personas.list_purposes()]
            except Exception:  # noqa: BLE001
                purposes = []
        systems = self.ai_systems.list() if self.ai_systems is not None else []
        domains: dict[str, list[dict]] = {}
        for urn, res, _aspects in self.datasets():
            kinds = sorted({c for c in res.get("classification") or [] if c.startswith("PII")})
            if not kinds:
                continue
            labels = [f"Classification.{k}" for k in kinds]
            allowed = [pu["name"] for pu in purposes if pu["enabled"] and any(
                _label_matches(f"urn:li:glossaryTerm:{t}" if not t.startswith("urn:li:") else t,
                               [f"urn:li:glossaryTerm:{lab}" for lab in labels]) or any(lab.startswith(t) for lab in labels)
                for t in pu["tags"])]
            entry = {"dataset": urn, "personal_data": kinds, "owners": res.get("owners") or [], "purposes": allowed,
                     "retention": [f"{p.id} ({p.days} days)" for p in policies if self._covered(p, res)],
                     "ai_systems": [s["name"] for s in systems if urn in (s.get("datasets") or [])
                                    or urn in (s.get("undeclared_datasets") or [])]}
            for d in (res.get("domains") or ["(no domain)"]):
                domains.setdefault(d, []).append(entry)
        return {"generated_ms": int(time.time() * 1000), "domains": [{"domain": d, "datasets": v}
                                                                       for d, v in sorted(domains.items())]}

    def ropa_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["domain", "dataset", "personal data", "owners", "purposes", "retention", "AI systems"])
        for d in self.ropa()["domains"]:
            for e in d["datasets"]:
                w.writerow([d["domain"], e["dataset"], " ".join(e["personal_data"]), " ".join(e["owners"]),
                            "; ".join(e["purposes"]), "; ".join(e["retention"]), "; ".join(e["ai_systems"])])
        return buf.getvalue()

    # ---- helpers -----------------------------------------------------------------------------------
    def _tell(self, etype: str, recipients: list[str], title: str, body: str, urn: Optional[str]) -> None:
        if self.notify is None or not recipients:
            return
        from cairn.notify.hub import Event

        try:
            self.notify.publish(Event(type=etype, title=title, body=body, actor=ACTOR, entity_urn=urn,
                                      recipients=list(recipients)))
        except Exception:  # noqa: BLE001 - the request stands even if nobody could be told
            logger.exception("privacy: could not notify")

    def _audit(self, action: str, actor: str, subject: Optional[str], details: dict) -> None:
        if self.audit is not None:
            self.audit.record("privacy", action, actor, subject, details)


def _name(urn: str) -> str:
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[^,]+\)$", urn)
    return m.group(1) if m else urn


def _to_ms(v: Any) -> Optional[int]:
    from datetime import date, datetime, timezone

    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v if v > 1e11 else v * 1000)
    if isinstance(v, datetime):
        return int((v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    if isinstance(v, date):
        return int(datetime(v.year, v.month, v.day, tzinfo=timezone.utc).timestamp() * 1000)
    try:
        s = str(v).replace("Z", "+00:00")
        d = datetime.fromisoformat(s if "T" in s or " " in s else s + "T00:00:00")
        return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    except ValueError:
        return None
