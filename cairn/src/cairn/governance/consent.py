"""Consent management (L66): a data subject's withdrawn consent takes their rows out of the purposes that need it.

* **Purposes that need consent.** A data protection officer (``MANAGE_POLICIES``) defines one:
  ``{"id": "marketing", "name": "Marketing", "identifier": "email", "mode": "opt_out", "members": {"users": [...],
  "groups": [...], "personas": [...]}}``. The members are who processes data for that purpose (the marketing team,
  the owner of a reverse ETL sync to the CRM). ``identifier`` is a kind of identifier from the privacy settings
  (``email``, ``phone``, ``customer_id`` or one configured in ``CAIRN_PRIVACY``), which says which columns link a row
  to a person.
* **Consent records.** ``POST /governance/consent/{purpose}/records`` with ``{"subjects": [{"value":
  "kim@acme.com", "state": "withdrawn"}]}`` (or ``"hash"`` instead of ``"value"``), from the consent platform or a
  person. Cairn keeps only the SHA-256 of the normalised identifier (trimmed, lower case): never the identifier.
  The hash is unsalted on purpose, because each warehouse computes the same hash of its own column to match it.
* **Rows.** For a member of a purpose, every dataset with a column of the purpose's identifier kind is filtered,
  wherever they query it, through Trino (a row filter) or a direct warehouse connection (the query is rewritten):
  ``opt_out`` keeps rows unless that person withdrew (rows without an identifier are kept); ``opt_in`` keeps only
  rows of people who granted consent. A filter that cannot be built (an engine without a hash function, more than
  ``MAX_INLINE`` records, a lookup failure) lets no row through.
* **Who changes what.** Purposes need ``MANAGE_POLICIES``; records need ``MANAGE_POLICIES`` or ``MANAGE_CONSENT``
  (for the consent platform's service identity). Every change is audited, without the identifier.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, delete, func, insert, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

MANAGE, RECORD = "MANAGE_POLICIES", "MANAGE_CONSENT"
MAX_INLINE = 10_000            # records a row filter carries inline; past it, nothing passes (and operators are told)
NOTHING = "1 = 0"
HEX = re.compile(r"^[0-9a-f]{64}$")

consent_purposes = Table(
    "cairn_consent_purposes", metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("description", Text, nullable=False),
    Column("identifier", String(64), nullable=False),
    Column("mode", String(8), nullable=False),              # opt_out | opt_in
    Column("members", JSONText, nullable=False),            # {"users": [...], "groups": [...], "personas": [...]}
    Column("enabled", Boolean, nullable=False),
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
consents = Table(
    "cairn_consents", metadata,
    Column("purpose", String(64), primary_key=True),
    Column("subject_hash", String(64), primary_key=True),
    Column("state", String(10), nullable=False),            # granted | withdrawn
    Column("source", String(120), nullable=False),
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False, index=True),
)

# SHA-256 (lower-case hex) of the trimmed, lower-cased value of column {c}, as each engine spells it.
HASH_SQL = {
    "trino": "lower(to_hex(sha256(to_utf8(lower(trim(CAST({c} AS varchar)))))))",
    "postgres": "encode(sha256(convert_to(lower(trim(CAST({c} AS text))), 'UTF8')), 'hex')",
    "duckdb": "sha256(lower(trim(CAST({c} AS VARCHAR))))",
    "snowflake": "lower(sha2(lower(trim(CAST({c} AS VARCHAR))), 256))",
    "redshift": "lower(sha2(lower(trim(CAST({c} AS VARCHAR))), 256))",
    "bigquery": "to_hex(sha256(lower(trim(CAST({c} AS STRING)))))",
    "databricks": "lower(sha2(lower(trim(CAST({c} AS STRING))), 256))",
    "mysql": "lower(sha2(lower(trim(CAST({c} AS CHAR))), 256))",
    "sqlite": "cairn_sha256(lower(trim(CAST({c} AS TEXT))))",   # registered on each connection by data access
}


def subject_hash(value: str) -> str:
    """As the warehouses compute it: SQL ``trim`` removes spaces only, so only spaces are stripped here."""
    return hashlib.sha256(value.strip(" ").lower().encode()).hexdigest()


def sqlite_sha256(value: Any) -> Optional[str]:
    return None if value is None else hashlib.sha256(str(value).encode()).hexdigest()


class ConsentError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class Members(BaseModel):
    users: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    personas: list[str] = Field(default_factory=list)


class PurposeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    identifier: str = Field(min_length=1, max_length=64)
    mode: Literal["opt_out", "opt_in"] = "opt_out"
    members: Members = Field(default_factory=Members)
    enabled: bool = True


class SubjectIn(BaseModel):
    value: Optional[str] = Field(default=None, min_length=1, max_length=512)
    hash: Optional[str] = None
    state: Literal["granted", "withdrawn"]

    @field_validator("hash")
    @classmethod
    def _hex(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not HEX.match(v.lower()):
            raise ValueError("hash: the SHA-256 of the trimmed, lower-cased identifier, as 64 hex characters")
        return v.lower() if v else v


class RecordsIn(BaseModel):
    subjects: list[SubjectIn] = Field(min_length=1, max_length=10_000)
    source: str = Field(default="api", max_length=120)


def _now() -> int:
    return int(time.time() * 1000)


def identifier_columns(res: dict, spec: dict) -> list[str]:
    """The columns of a dataset (its resource attributes) that hold this kind of identifier."""
    out = [path for path, labels in (res.get("field_labels") or {}).items() if any(lab in spec["labels"] for lab in labels)]
    for path in (res.get("sensitive_fields") or {}):
        if path not in out and path.split(".")[-1].lower() in spec["columns"]:
            out.append(path)
    return sorted(out)


class ConsentService:
    def __init__(self, engine, resource: Callable[[str], dict], membership: Callable[[str], tuple[list[str], list[str]]],
                 identifiers: dict[str, dict], audit=None, notify: Optional[Callable[[str, str], None]] = None):
        """``resource(urn)``: a dataset's resource attributes (``field_labels``, ``sensitive_fields``);
        ``membership(actor)``: their (groups, personas); ``identifiers``: the privacy settings' identifier kinds."""
        self.engine, self.resource, self.membership = engine, resource, membership
        self.identifiers, self.audit, self.notify = identifiers, audit, notify
        self._cache: tuple[float, list[dict], dict[str, tuple[list[str], list[str]]]] = (0.0, [], {})
        self.ttl = 5.0

    # ---- purposes -----------------------------------------------------------------------------------
    def _slug(self, name: str) -> str:
        return (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "purpose")[:64]

    def put_purpose(self, actor: str, body: PurposeIn, pid: Optional[str] = None) -> dict:
        if body.identifier not in self.identifiers:
            raise ConsentError(f"identifier: one of {sorted(self.identifiers)}")
        pid = pid or self._slug(body.name)
        row = {"name": body.name, "description": body.description, "identifier": body.identifier, "mode": body.mode,
               "members": body.members.model_dump(), "enabled": body.enabled, "updated_by": actor,
               "updated_ms": _now()}
        with self.engine.begin() as conn:
            if conn.execute(select(consent_purposes.c.id).where(consent_purposes.c.id == pid)).first():
                conn.execute(update(consent_purposes).where(consent_purposes.c.id == pid).values(**row))
            else:
                conn.execute(insert(consent_purposes).values(id=pid, **row))
        self._cache = (0.0, [], {})
        self._audit(actor, "consent.purpose_saved", pid, {"identifier": body.identifier, "mode": body.mode,
                                                           "members": body.members.model_dump()})
        return self.purpose(pid)

    def purpose(self, pid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(consent_purposes).where(consent_purposes.c.id == pid)).first()
            if r is None:
                raise ConsentError(f"no purpose {pid}", 404)
            counts = dict(conn.execute(select(consents.c.state, func.count()).where(consents.c.purpose == pid)
                                       .group_by(consents.c.state)).all())
        out = dict(r._mapping)
        out["records"] = {"granted": counts.get("granted", 0), "withdrawn": counts.get("withdrawn", 0)}
        out["inline_limit"] = MAX_INLINE
        relevant = out["records"]["withdrawn" if out["mode"] == "opt_out" else "granted"]
        out["filter_state"] = "ok" if relevant <= MAX_INLINE else "over_limit: rows are withheld until it is reduced"
        return out

    def purposes(self) -> list[dict]:
        with self.engine.connect() as conn:
            ids = [r[0] for r in conn.execute(select(consent_purposes.c.id).order_by(consent_purposes.c.name))]
        return [self.purpose(i) for i in ids]

    def delete_purpose(self, actor: str, pid: str) -> None:
        self.purpose(pid)
        with self.engine.begin() as conn:
            conn.execute(delete(consents).where(consents.c.purpose == pid))
            conn.execute(delete(consent_purposes).where(consent_purposes.c.id == pid))
        self._cache = (0.0, [], {})
        self._audit(actor, "consent.purpose_deleted", pid, {})

    # ---- records ------------------------------------------------------------------------------------
    def record(self, actor: str, pid: str, body: RecordsIn) -> dict:
        self.purpose(pid)
        now, changed = _now(), {"granted": 0, "withdrawn": 0}
        with self.engine.begin() as conn:
            for s in body.subjects:
                if (s.value is None) == (s.hash is None):
                    raise ConsentError("each subject has a value or a hash")
                h = s.hash or subject_hash(s.value or "")
                row = {"state": s.state, "source": body.source, "updated_by": actor, "updated_ms": now}
                done = conn.execute(update(consents).where(consents.c.purpose == pid, consents.c.subject_hash == h)
                                    .values(**row)).rowcount
                if not done:
                    conn.execute(insert(consents).values(purpose=pid, subject_hash=h, **row))
                changed[s.state] += 1
        self._cache = (0.0, [], {})
        self._audit(actor, "consent.recorded", pid, {"source": body.source, **changed})   # counts, no identifiers
        state = self.purpose(pid)
        if state["filter_state"] != "ok" and self.notify is not None:
            self.notify(pid, state["filter_state"])
        return {"purpose": pid, **changed, "records": state["records"], "filter_state": state["filter_state"]}

    def state_of(self, pid: str, value: Optional[str] = None, hash_: Optional[str] = None) -> dict:
        p = self.purpose(pid)
        h = (hash_ or "").lower() or subject_hash(value or "")
        with self.engine.connect() as conn:
            r = conn.execute(select(consents.c.state, consents.c.updated_ms)
                             .where(consents.c.purpose == pid, consents.c.subject_hash == h)).first()
        state = r.state if r else ("granted" if p["mode"] == "opt_out" else "none")
        return {"purpose": pid, "state": state, "recorded": r is not None,
                "included": state == "granted", "updated_ms": r.updated_ms if r else None}

    # ---- filters ------------------------------------------------------------------------------------
    def _active(self) -> tuple[list[dict], dict[str, tuple[list[str], list[str]]]]:
        at, purposes, lists = self._cache
        if time.monotonic() - at < self.ttl:
            return purposes, lists
        with self.engine.connect() as conn:
            purposes = [dict(r._mapping) for r in conn.execute(
                select(consent_purposes).where(consent_purposes.c.enabled.is_(True)))]
            lists = {}
            for p in purposes:
                rows = conn.execute(select(consents.c.subject_hash, consents.c.state)
                                    .where(consents.c.purpose == p["id"])).all()
                lists[p["id"]] = ([h for h, st in rows if st == "withdrawn"], [h for h, st in rows if st == "granted"])
        self._cache = (time.monotonic(), purposes, lists)
        return purposes, lists

    def purposes_of(self, actor: str, groups: Optional[list[str]] = None) -> list[dict]:
        purposes, _ = self._active()
        if not purposes:
            return []
        g, personas = self.membership(actor)
        g = set(g) | set(groups or [])
        out = []
        for p in purposes:
            m = p["members"] or {}
            if actor in (m.get("users") or []) or g & set(m.get("groups") or []) or set(personas) & set(m.get("personas") or []):
                out.append(p)
        return out

    def filters(self, actor: str, urn: str, dialect: str, groups: Optional[list[str]] = None) -> list[str]:
        """Row filters (SQL in ``dialect``) that take out the rows this actor may not use for their purposes."""
        mine = self.purposes_of(actor, groups)
        if not mine:
            return []
        res = self.resource(urn) or {}
        _, lists = self._active()
        out = []
        for p in mine:
            spec = self.identifiers.get(p["identifier"])
            if spec is None:
                out.append(NOTHING)                       # a purpose whose identifier kind is gone: fail closed
                continue
            cols = identifier_columns(res, spec)
            if not cols:
                continue                                  # no column links this dataset's rows to a person
            template = HASH_SQL.get(dialect)
            withdrawn, granted = lists.get(p["id"], ([], []))
            hashes = withdrawn if p["mode"] == "opt_out" else granted
            if template is None or len(hashes) > MAX_INLINE:
                logger.warning("consent filter for %s on %s withheld rows: %s", p["id"], urn,
                               "no hash function for " + dialect if template is None else "too many records")
                out.append(NOTHING)
                continue
            for col in cols:
                quoted = _quote(col.split(".")[-1], dialect)
                hashed = template.format(c=quoted)
                values = ", ".join(f"'{h}'" for h in sorted(hashes))   # 64 hex characters each: nothing to escape
                if p["mode"] == "opt_out":
                    out.append(f"({quoted} IS NULL OR {hashed} NOT IN ({values}))" if hashes else "")
                else:
                    out.append(f"({hashed} IN ({values}))" if hashes else NOTHING)
        return [f for f in out if f]

    def _audit(self, actor: str, action: str, pid: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("privacy", action, actor, f"urn:li:consentPurpose:{pid}", details)


def _quote(name: str, dialect: str) -> str:
    from sqlglot import exp

    return exp.column(name, quoted=True).sql(dialect="tsql" if dialect == "sqlserver" else dialect)


def build_consent_router(cs: ConsentService, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/governance/consent", tags=["governance"])

    def officer(p):
        if not authorize(p, MANAGE, None):
            raise HTTPException(403, f"consent purposes need {MANAGE}")
        return p.urn

    def recorder(p):
        if not (authorize(p, MANAGE, None) or authorize(p, RECORD, None)):
            raise HTTPException(403, f"recording consent needs {MANAGE} or {RECORD}")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except ConsentError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/purposes")
    async def list_(p=Depends(current_principal)):
        officer(p)
        return await call(cs.purposes)

    @router.post("/purposes", status_code=201)
    async def create(body: PurposeIn, p=Depends(current_principal)):
        return await call(lambda: cs.put_purpose(officer(p), body))

    @router.get("/purposes/{pid}")
    async def get(pid: str, p=Depends(current_principal)):
        officer(p)
        return await call(lambda: cs.purpose(pid))

    @router.put("/purposes/{pid}")
    async def change(pid: str, body: PurposeIn, p=Depends(current_principal)):
        actor = officer(p)
        await call(lambda: cs.purpose(pid))
        return await call(lambda: cs.put_purpose(actor, body, pid))

    @router.delete("/purposes/{pid}", status_code=204)
    async def drop(pid: str, p=Depends(current_principal)):
        await call(lambda: cs.delete_purpose(officer(p), pid))

    @router.post("/purposes/{pid}/records")
    async def record(pid: str, body: RecordsIn, p=Depends(current_principal)):
        return await call(lambda: cs.record(recorder(p), pid, body))

    @router.post("/purposes/{pid}/check")
    async def check(pid: str, value: Optional[str] = Body(None, embed=True), hash: Optional[str] = Body(None, embed=True),
                    p=Depends(current_principal)):
        recorder(p)                                        # by POST: the identifier stays out of URLs and logs
        if (value is None) == (hash is None):
            raise HTTPException(422, "a value or a hash")
        return await call(lambda: cs.state_of(pid, value, hash))

    @router.get("/mine")
    async def mine(p=Depends(current_principal)):
        """The purposes whose consent filters apply to the caller."""
        return [{"id": x["id"], "name": x["name"], "identifier": x["identifier"], "mode": x["mode"]}
                for x in await run_in_threadpool(cs.purposes_of, p.urn, list(p.groups or ()))]

    return router
