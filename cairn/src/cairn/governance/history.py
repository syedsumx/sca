"""Metadata change history: who changed what on an asset, the diff, and a guarded one-click revert.

Cairn Core publishes a MetadataChangeLog event for every aspect write, with the new value, the
previous one and who made the change. Cairn keeps those for the aspects people govern (descriptions,
column docs, tags, terms, owners, domains, links, structured properties, deprecation, lineage), so
an asset's history survives however the change was made: the core UI, Cairn, an API client or an
automation.

* **Diff.** Each change is shown field by field: lists are matched by what identifies their items
  (a tag, a term, an owner, a column's ``fieldPath``), so "added tag PII on column email" reads as
  that, not as two JSON blobs. Audit stamps are left out.
* **Revert.** Writing the previous value back is one click for anyone who may make that kind of
  edit on the asset (``EDIT_ENTITY_TAGS`` for tags, and so on). It is refused when the aspect has
  changed again since (revert the later change first, or force it), and when there is no earlier
  value to go back to for an aspect that can't be emptied. The revert is itself a change in the
  history, attributed to the person who asked for it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, and_, select, update

from cairn.common.db import JSONText, metadata
from cairn.common.proposals import make_mcp

logger = logging.getLogger(__name__)

change_history = Table(
    "cairn_change_history", metadata,
    Column("id", String(40), primary_key=True),
    Column("urn", String(512), nullable=False, index=True),
    Column("aspect", String(128), nullable=False),
    Column("actor", String(256), nullable=False),
    Column("ts_ms", BigInteger, nullable=False, index=True),
    Column("before", JSONText, nullable=True),
    Column("after", JSONText, nullable=True),
    Column("after_hash", String(40), nullable=False),
    Column("revert_of", String(40), nullable=True),
    Column("reverted_by", String(40), nullable=True),
    Column("confirmed", Boolean, nullable=False, default=True),
)

PRIVILEGES = {
    "globalTags": "EDIT_ENTITY_TAGS",
    "glossaryTerms": "EDIT_ENTITY_GLOSSARY_TERMS",
    "ownership": "EDIT_ENTITY_OWNERS",
    "domains": "EDIT_ENTITY_DOMAINS",
    "deprecation": "EDIT_ENTITY_DEPRECATION",
    "institutionalMemory": "EDIT_ENTITY_DOC_LINKS",
    "editableSchemaMetadata": "EDIT_DATASET_COL_DESCRIPTION",
    "editableDatasetProperties": "EDIT_ENTITY_DOCS",
    "editableContainerProperties": "EDIT_ENTITY_DOCS",
    "editableDashboardProperties": "EDIT_ENTITY_DOCS",
    "editableChartProperties": "EDIT_ENTITY_DOCS",
    "editableDataFlowProperties": "EDIT_ENTITY_DOCS",
    "editableDataJobProperties": "EDIT_ENTITY_DOCS",
    "editableMlModelProperties": "EDIT_ENTITY_DOCS",
    "glossaryTermInfo": "EDIT_ENTITY_DOCS",
    "domainProperties": "EDIT_ENTITY_DOCS",
    "dataProductProperties": "EDIT_ENTITY_DOCS",
    "structuredProperties": "EDIT_ENTITY_PROPERTIES",
    "upstreamLineage": "EDIT_LINEAGE",
}
TRACKED = frozenset(PRIVILEGES)
# What an aspect looks like with nothing in it, for reverting a change that created it.
EMPTY: dict[str, dict] = {
    "globalTags": {"tags": []},
    "glossaryTerms": {"terms": [], "auditStamp": {"time": 0, "actor": "urn:li:corpuser:unknown"}},
    "ownership": {"owners": []},
    "domains": {"domains": []},
    "institutionalMemory": {"elements": []},
    "editableSchemaMetadata": {"editableSchemaFieldInfo": []},
    "editableDatasetProperties": {},
    "editableDashboardProperties": {},
    "editableChartProperties": {},
    "structuredProperties": {"properties": []},
    "deprecation": {"deprecated": False, "note": "", "actor": "urn:li:corpuser:unknown"},
}
IGNORED = {"lastModified", "created", "auditStamp", "createStamp", "lastObserved", "updateStamp"}
IDENTITY = ("fieldPath", "tag", "urn", "owner", "url", "dataset", "propertyUrn", "destinationUrn")


class HistoryError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _hash(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _decode(blob: Any) -> Optional[dict]:
    if not isinstance(blob, dict):
        return None
    raw = blob.get("value")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _strip(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if k not in IGNORED}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


def _identity(items: list) -> Optional[str]:
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    return next((k for k in IDENTITY if all(k in i for i in items)), None)


def diff(before: Any, after: Any, path: str = "") -> list[dict]:
    """Field-by-field changes between two aspect values: ``{field, op, before, after}``."""
    before, after = _strip(before), _strip(after)
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after)):
            out += diff(before.get(k), after.get(k), f"{path}.{k}" if path else k)
        return out
    if isinstance(before, list) and isinstance(after, list) or (before is None and isinstance(after, list)) \
            or (after is None and isinstance(before, list)):
        b, a = before or [], after or []
        key = _identity(b + a)
        if key:
            bi, ai = {i[key]: i for i in b}, {i[key]: i for i in a}
            out = []
            for k in sorted(set(bi) | set(ai), key=str):
                where = f"{path}[{k}]"
                if k not in bi:
                    out.append({"field": where, "op": "added", "before": None, "after": ai[k]})
                elif k not in ai:
                    out.append({"field": where, "op": "removed", "before": bi[k], "after": None})
                else:
                    out += diff({x: y for x, y in bi[k].items() if x != key},
                                {x: y for x, y in ai[k].items() if x != key}, where)
            return out
        if all(not isinstance(x, (dict, list)) for x in b + a):
            return ([{"field": path, "op": "added", "before": None, "after": x} for x in a if x not in b]
                    + [{"field": path, "op": "removed", "before": x, "after": None} for x in b if x not in a])
    op = "added" if before in (None, "", [], {}) else "removed" if after in (None, "", [], {}) else "changed"
    return [{"field": path, "op": op, "before": before, "after": after}]


class ChangeHistory:
    def __init__(self, engine, graph=None, audit=None, actor: str = "urn:li:corpuser:cairn"):
        self.engine = engine
        self.graph = graph
        self.audit = audit
        self.actor = actor

    # ---- recording -----------------------------------------------------------------------------
    def handle(self, event: dict) -> bool:
        """Record one MetadataChangeLog event; True when it was a new change to a tracked aspect."""
        aspect, urn = event.get("aspectName"), event.get("entityUrn")
        if aspect not in TRACKED or not urn:
            return False
        after = _decode(event.get("aspect"))
        before = _decode(event.get("previousAspectValue"))
        if event.get("changeType") == "DELETE":
            after = None
        if not diff(before, after):
            return False
        created = event.get("created") or {}
        actor = created.get("actor") or "urn:li:corpuser:unknown"
        ts = int(created.get("time") or time.time() * 1000)
        after_hash = _hash(after)
        cid = hashlib.sha1(f"{urn}|{aspect}|{ts}|{after_hash}".encode()).hexdigest()
        with self.engine.begin() as conn:
            if conn.execute(select(change_history.c.id).where(change_history.c.id == cid)).first():
                return False
            pending = conn.execute(select(change_history.c.id).where(and_(
                change_history.c.urn == urn, change_history.c.aspect == aspect,
                change_history.c.after_hash == after_hash, change_history.c.confirmed.is_(False)))).first()
            if pending is not None:                 # the revert Cairn wrote: already recorded, for its person
                conn.execute(update(change_history).where(change_history.c.id == pending.id)
                             .values(confirmed=True, before=before))
                return False
            conn.execute(change_history.insert().values(id=cid, urn=urn, aspect=aspect, actor=actor, ts_ms=ts,
                                                        before=before, after=after, after_hash=after_hash,
                                                        confirmed=True))
        return True

    # ---- reading -------------------------------------------------------------------------------
    @staticmethod
    def _row(r, with_values: bool = False) -> dict:
        changes = diff(r.before, r.after)
        out = {"id": r.id, "urn": r.urn, "aspect": r.aspect, "actor": r.actor, "ts_ms": r.ts_ms,
               "summary": _summary(r.aspect, changes), "changes": changes, "revert_of": r.revert_of,
               "reverted_by": r.reverted_by, "revertible": r.aspect in PRIVILEGES and (r.before is not None
                                                                                       or r.aspect in EMPTY)}
        if with_values:
            out.update(before=r.before, after=r.after)
        return out

    def list(self, urn: str, aspect: Optional[str] = None, limit: int = 100) -> list[dict]:
        q = select(change_history).where(change_history.c.urn == urn)
        if aspect:
            q = q.where(change_history.c.aspect == aspect)
        q = q.order_by(change_history.c.ts_ms.desc(), change_history.c.id).limit(min(max(limit, 1), 500))
        with self.engine.connect() as conn:
            return [self._row(r) for r in conn.execute(q)]

    def recent(self, actor: Optional[str] = None, since_ms: Optional[int] = None, limit: int = 100) -> list[dict]:
        q = select(change_history)
        if actor:
            q = q.where(change_history.c.actor == actor)
        if since_ms:
            q = q.where(change_history.c.ts_ms >= since_ms)
        q = q.order_by(change_history.c.ts_ms.desc(), change_history.c.id).limit(min(max(limit, 1), 500))
        with self.engine.connect() as conn:
            return [self._row(r) for r in conn.execute(q)]

    def get(self, cid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(change_history).where(change_history.c.id == cid)).first()
        if r is None:
            raise HistoryError("no such change", 404)
        return self._row(r, with_values=True)

    # ---- reverting -----------------------------------------------------------------------------
    def revert(self, cid: str, actor: str, force: bool = False) -> dict:
        change = self.get(cid)
        if change["reverted_by"]:
            raise HistoryError("this change was already reverted", 409)
        if not change["revertible"]:
            raise HistoryError(f"there is nothing earlier to restore for {change['aspect']}", 409)
        urn, aspect = change["urn"], change["aspect"]
        current = self.graph.get_aspect(urn, aspect)
        if not force and diff(current, change["after"]):
            raise HistoryError(f"{aspect} has changed since; revert the later change first, or force it", 409)
        target = change["before"] if change["before"] is not None else EMPTY[aspect]
        now = int(time.time() * 1000)
        rid = hashlib.sha1(f"{urn}|{aspect}|{now}|revert|{cid}".encode()).hexdigest()
        # Recorded before the write: the change log can deliver the write's event before emit returns.
        with self.engine.begin() as conn:
            conn.execute(change_history.insert().values(id=rid, urn=urn, aspect=aspect, actor=actor, ts_ms=now,
                                                        before=current, after=target, after_hash=_hash(target),
                                                        revert_of=cid, confirmed=False))
        try:
            self.graph.emit([make_mcp(urn, aspect, target)])
        except Exception:
            with self.engine.begin() as conn:
                conn.execute(change_history.delete().where(change_history.c.id == rid))
            raise
        with self.engine.begin() as conn:
            conn.execute(update(change_history).where(change_history.c.id == cid).values(reverted_by=rid))
        if self.audit is not None:
            self.audit.record("governance", "metadata.reverted", actor, urn,
                              {"change": cid, "aspect": aspect, "forced": force})
        return self.get(rid)

    def prune(self, days: int = 365, now_ms: Optional[int] = None) -> int:
        cutoff = (now_ms or int(time.time() * 1000)) - days * 86_400_000
        with self.engine.begin() as conn:
            return conn.execute(change_history.delete().where(change_history.c.ts_ms < cutoff)).rowcount or 0


def _label(v: Any) -> str:
    if isinstance(v, dict):
        for k in IDENTITY:
            if k in v:
                return str(v[k]).rsplit(":", 1)[-1]
        return ", ".join(f"{k}={_label(x)}" for k, x in list(v.items())[:3])
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "…"


def _summary(aspect: str, changes: list[dict]) -> str:
    """One line for a change: ``added tag PII on email; description changed``."""
    parts = []
    for c in changes[:4]:
        field = c["field"] or aspect
        if c["op"] == "changed":
            parts.append(f"{field} changed")
        else:
            parts.append(f"{c['op']} {_label(c['after'] if c['op'] == 'added' else c['before'])} ({field})")
    if len(changes) > 4:
        parts.append(f"{len(changes) - 4} more")
    return "; ".join(parts) or f"{aspect} rewritten"
