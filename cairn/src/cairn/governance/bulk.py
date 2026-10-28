"""Bulk metadata editing through a spreadsheet: export, edit, preview the changes, apply, undo.

The CSV has one row per dataset (``field`` empty) and one per column. Editable cells:

* ``description``: the dataset's or the column's description.
* ``tags`` / ``glossary_terms``: ``;``-separated; the cell is the full set, so removing a name
  removes the label. Names or urns are accepted (``gdpr`` = ``urn:li:tag:gdpr``).
* ``owners`` (datasets): ``;``-separated usernames or urns; owners missing from the cell are removed.
* ``domain`` (datasets): one domain id or urn.

``name``, ``platform`` and ``classification`` are context and are ignored on import. An empty
editable cell is left alone; ``-`` clears it. Each change is authorized as the importing person
with the privilege the catalog UI would require, and denied changes are reported and skipped.
Applying writes every change as one batch, audited, which can be undone while nobody else has
changed those aspects since.
"""

from __future__ import annotations

import csv
import io
import json
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.common.proposals import audit_stamp, make_mcp

bulk_batches = Table(
    "cairn_bulk_batches", metadata,
    Column("id", String(40), primary_key=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("state", String(16), nullable=False),          # PREVIEWED | APPLIED | UNDONE
    Column("changes", JSONText, nullable=False),
    Column("before", JSONText, nullable=True),            # {"urn|aspect": value-or-null} at apply time
    Column("after", JSONText, nullable=True),
    Column("rows", Integer, nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

COLUMNS = ["urn", "field", "name", "platform", "description", "owners", "tags", "glossary_terms", "domain",
           "classification"]
CLEAR = "-"
MAX_ROWS = 20_000
# privilege per change (dataset-level, column-level), as the catalog UI checks them
PRIVILEGES = {
    "description": ("EDIT_ENTITY_DOCS", "EDIT_DATASET_COL_DESCRIPTION"),
    "tags": ("EDIT_ENTITY_TAGS", "EDIT_DATASET_COL_TAGS"),
    "glossary_terms": ("EDIT_ENTITY_GLOSSARY_TERMS", "EDIT_DATASET_COL_GLOSSARY_TERMS"),
    "owners": ("EDIT_ENTITY_OWNERS", None),
    "domain": ("EDIT_ENTITY_DOMAINS", None),
}
ASPECTS = ("editableDatasetProperties", "datasetProperties", "editableSchemaMetadata", "schemaMetadata",
           "globalTags", "glossaryTerms", "ownership", "domains")


class BulkError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _split(cell: str) -> list[str]:
    return [p.strip() for p in cell.replace(",", ";").split(";") if p.strip()]


def _urnify(value: str, kind: str) -> str:
    prefix = {"tags": "urn:li:tag:", "glossary_terms": "urn:li:glossaryTerm:", "owners": "urn:li:corpuser:",
              "domain": "urn:li:domain:"}[kind]
    return value if value.startswith("urn:li:") else prefix + value


def _short(urn: str) -> str:
    for prefix in ("urn:li:tag:", "urn:li:glossaryTerm:", "urn:li:corpuser:", "urn:li:domain:"):
        if urn.startswith(prefix):
            return urn[len(prefix):]
    return urn


# --------------------------------------------------------------------------- export
def export_rows(urns: list[str], get: Callable[[str, str], Optional[dict]],
                classification: Callable[[str], list[str]] = lambda u: []) -> list[dict]:
    rows = []
    for urn in urns:
        a = {name: get(urn, name) or {} for name in ASPECTS}
        fallback = urn.split(",")[-2].split(".")[-1] if "," in urn else urn
        name = a["datasetProperties"].get("name") or fallback
        platform = urn.split("dataPlatform:")[1].split(",")[0] if "dataPlatform:" in urn else ""
        rows.append({
            "urn": urn, "field": "", "name": name, "platform": platform,
            "description": a["editableDatasetProperties"].get("description") or a["datasetProperties"].get(
                "description") or "",
            "owners": ";".join(_short(o["owner"]) for o in a["ownership"].get("owners", []) if o.get("owner")),
            "tags": ";".join(_short(t["tag"]) for t in a["globalTags"].get("tags", []) if t.get("tag")),
            "glossary_terms": ";".join(_short(t["urn"]) for t in a["glossaryTerms"].get("terms", []) if t.get("urn")),
            "domain": ";".join(_short(d) for d in a["domains"].get("domains", [])),
            "classification": ";".join(classification(urn)),
        })
        edits = {f["fieldPath"]: f for f in a["editableSchemaMetadata"].get("editableSchemaFieldInfo", [])}
        for f in a["schemaMetadata"].get("fields", []):
            path = f["fieldPath"]
            e = edits.get(path, {})
            tags = [t["tag"] for t in ((e.get("globalTags") or {}).get("tags") or []) if t.get("tag")]
            terms = [t["urn"] for t in ((e.get("glossaryTerms") or {}).get("terms") or []) if t.get("urn")]
            rows.append({"urn": urn, "field": path, "name": path, "platform": platform,
                         "description": e.get("description") or f.get("description") or "", "owners": "",
                         "tags": ";".join(_short(t) for t in tags), "glossary_terms": ";".join(_short(t) for t in terms),
                         "domain": "", "classification": ""})
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


# --------------------------------------------------------------------------- import
def parse_csv(text: str) -> list[dict]:
    text = text.lstrip("﻿")                      # Excel writes a byte-order mark
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "urn" not in reader.fieldnames:
        raise BulkError("the file needs a header row with at least a 'urn' column")
    rows = []
    for r in reader:
        clean = {k: (v or "").strip() for k, v in r.items() if k}
        if not any(clean.values()):
            continue
        rows.append(clean)
        if len(rows) > MAX_ROWS:
            raise BulkError(f"at most {MAX_ROWS} rows per import")
    return rows


def _current(urn: str, field: str, a: dict) -> dict:
    if not field:
        return {
            "description": a["editableDatasetProperties"].get("description") or a["datasetProperties"].get(
                "description") or "",
            "owners": [o["owner"] for o in a["ownership"].get("owners", []) if o.get("owner")],
            "tags": [t["tag"] for t in a["globalTags"].get("tags", []) if t.get("tag")],
            "glossary_terms": [t["urn"] for t in a["glossaryTerms"].get("terms", []) if t.get("urn")],
            "domain": (a["domains"].get("domains") or [None])[0],
        }
    e = next((f for f in a["editableSchemaMetadata"].get("editableSchemaFieldInfo", []) if f.get("fieldPath") == field),
             {})
    s = next((f for f in a["schemaMetadata"].get("fields", []) if f.get("fieldPath") == field), None)
    return {
        "_exists": s is not None,
        "description": e.get("description") or (s or {}).get("description") or "",
        "tags": [t["tag"] for t in ((e.get("globalTags") or {}).get("tags") or []) if t.get("tag")],
        "glossary_terms": [t["urn"] for t in ((e.get("glossaryTerms") or {}).get("terms") or []) if t.get("urn")],
    }


def diff(rows: list[dict], get: Callable[[str, str], Optional[dict]]) -> tuple[list[dict], list[str]]:
    """Changes the file asks for, against the catalog as it is now; plus row-level errors."""
    changes, errors = [], []
    cache: dict[str, dict] = {}
    for line, r in enumerate(rows, start=2):
        urn, field = r.get("urn", ""), r.get("field", "")
        if not urn.startswith("urn:li:dataset:"):
            errors.append(f"line {line}: {urn or '(no urn)'} is not a dataset urn")
            continue
        if urn not in cache:
            cache[urn] = {name: get(urn, name) or {} for name in ASPECTS}
            if not cache[urn]["schemaMetadata"] and not cache[urn]["datasetProperties"] and not cache[urn]["ownership"]:
                errors.append(f"line {line}: {urn} not found in the catalog")
        cur = _current(urn, field, cache[urn])
        if field and not cur.get("_exists"):
            errors.append(f"line {line}: {urn} has no column {field!r}")
            continue
        for key in ("description", "tags", "glossary_terms", "owners", "domain"):
            if key not in r or r[key] == "" or (field and key in ("owners", "domain")):
                continue
            cell = r[key]
            if key == "description":
                new: Any = "" if cell == CLEAR else cell
                if new != cur["description"]:
                    changes.append({"urn": urn, "field": field, "what": key, "from": cur[key], "to": new})
                continue
            if key == "domain":
                new = None if cell == CLEAR else _urnify(_split(cell)[0], "domain")
                if new != cur["domain"]:
                    changes.append({"urn": urn, "field": field, "what": key, "from": cur[key], "to": new})
                continue
            new = [] if cell == CLEAR else [_urnify(v, key) for v in _split(cell)]
            if set(new) != set(cur[key]):
                changes.append({"urn": urn, "field": field, "what": key, "from": sorted(cur[key]),
                                "to": sorted(dict.fromkeys(new)),
                                "add": sorted(set(new) - set(cur[key])), "remove": sorted(set(cur[key]) - set(new))})
    return changes, errors


def privilege_for(change: dict) -> str:
    dataset_priv, column_priv = PRIVILEGES[change["what"]]
    return column_priv if change["field"] else dataset_priv


def apply_changes(changes: list[dict], get: Callable[[str, str], Optional[dict]], actor: str,
                  batch_id: str) -> tuple[list[dict], dict, dict]:
    """MCPs for ``changes`` plus before/after snapshots of every aspect touched."""
    stamp = audit_stamp(actor)
    staged: dict[tuple[str, str], dict] = {}
    before: dict[str, Any] = {}

    def aspect(urn: str, name: str) -> dict:
        key = (urn, name)
        if key not in staged:
            value = get(urn, name)
            before[f"{urn}|{name}"] = value
            staged[key] = json.loads(json.dumps(value or {}))
        return staged[key]

    for c in changes:
        urn, field, what = c["urn"], c["field"], c["what"]
        if not field:
            if what == "description":
                a = aspect(urn, "editableDatasetProperties")
                a["description"] = c["to"]
                a["lastModified"] = stamp
                a.setdefault("created", stamp)
            elif what == "tags":
                aspect(urn, "globalTags")["tags"] = [{"tag": t} for t in c["to"]]
            elif what == "glossary_terms":
                a = aspect(urn, "glossaryTerms")
                a["terms"] = [{"urn": t} for t in c["to"]]
                a["auditStamp"] = stamp
            elif what == "owners":
                a = aspect(urn, "ownership")
                keep = [o for o in a.get("owners", []) if o.get("owner") in c["to"]]
                have = {o.get("owner") for o in keep}
                a["owners"] = keep + [{"owner": o, "type": "DATAOWNER"} for o in c["to"] if o not in have]
                a["lastModified"] = stamp
            elif what == "domain":
                aspect(urn, "domains")["domains"] = [c["to"]] if c["to"] else []
            continue
        a = aspect(urn, "editableSchemaMetadata")
        a.setdefault("created", stamp)
        a["lastModified"] = stamp
        infos = a.setdefault("editableSchemaFieldInfo", [])
        info = next((f for f in infos if f.get("fieldPath") == field), None)
        if info is None:
            info = {"fieldPath": field}
            infos.append(info)
        if what == "description":
            info["description"] = c["to"]
        elif what == "tags":
            info["globalTags"] = {"tags": [{"tag": t} for t in c["to"]]}
        elif what == "glossary_terms":
            info["glossaryTerms"] = {"terms": [{"urn": t} for t in c["to"]], "auditStamp": stamp}
    props = {"cairn_source": "bulk-edit", "cairn_batch": batch_id, "cairn_edited_by": actor}
    mcps = [make_mcp(urn, name, value, system_properties=props) for (urn, name), value in staged.items()]
    after = {f"{urn}|{name}": value for (urn, name), value in staged.items()}
    return mcps, before, after


def _same(a: Any, b: Any) -> bool:
    """Compare aspects ignoring audit stamps the catalog may rewrite."""
    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k not in ("lastModified", "created", "auditStamp")}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return strip(a or {}) == strip(b or {})


class BulkService:
    def __init__(self, engine: Engine, graph, audit=None):
        self.engine = engine
        self.graph = graph
        self.audit = audit

    def preview(self, text: str, actor: str, allowed: Callable[[str, str], bool]) -> dict:
        rows = parse_csv(text)
        changes, errors = diff(rows, self.graph.get_aspect)
        permitted, denied = [], []
        for c in changes:
            priv = privilege_for(c)
            (permitted if allowed(priv, c["urn"]) else denied).append({**c, "privilege": priv})
        bid = f"bulk_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(bulk_batches.insert().values(id=bid, actor=actor, state="PREVIEWED", changes=permitted,
                                                      rows=len(rows), created_ms=now, updated_ms=now))
        return {"batch_id": bid, "rows": len(rows), "changes": permitted, "denied": denied, "errors": errors}

    def restrict(self, bid: str, changes: list[dict]) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(bulk_batches).where(bulk_batches.c.id == bid).values(changes=changes))

    def _batch(self, bid: str, actor: str):
        with self.engine.connect() as conn:
            row = conn.execute(select(bulk_batches).where(bulk_batches.c.id == bid)).first()
        if row is None or row.actor != actor:
            raise BulkError("batch not found", 404)
        return row

    def apply(self, bid: str, actor: str, allowed: Callable[[str, str], bool]) -> dict:
        row = self._batch(bid, actor)
        if row.state != "PREVIEWED":
            raise BulkError(f"batch is {row.state.lower()}", 409)
        # Re-check rights and staleness at apply time: the preview may be old.
        changes = [c for c in row.changes if allowed(c["privilege"], c["urn"])]
        fresh, _ = diff([{"urn": c["urn"], "field": c["field"], c["what"]: _cell(c)} for c in changes],
                        self.graph.get_aspect)
        stale = [c for c in changes if not any(f["urn"] == c["urn"] and f["field"] == c["field"]
                                               and f["what"] == c["what"] for f in fresh)]
        todo = [c for c in changes if c not in stale]
        mcps, before, after = apply_changes(todo, self.graph.get_aspect, actor, bid)
        if mcps:
            self.graph.emit(mcps)
        with self.engine.begin() as conn:
            conn.execute(update(bulk_batches).where(bulk_batches.c.id == bid).values(
                state="APPLIED", before=before, after=after, changes=todo, updated_ms=_now()))
        if self.audit is not None:
            self.audit.record("governance", "bulk.applied", actor, bid,
                              {"changes": len(todo), "assets": len({c["urn"] for c in todo}), "skipped": len(stale)})
        return {"batch_id": bid, "applied": len(todo), "already_current": len(stale), "aspects": len(mcps)}

    def undo(self, bid: str, actor: str) -> dict:
        row = self._batch(bid, actor)
        if row.state != "APPLIED":
            raise BulkError(f"batch is {row.state.lower()}", 409)
        conflicts, restore = [], []
        for key, value in (row.before or {}).items():
            urn, name = key.rsplit("|", 1)
            if not _same(self.graph.get_aspect(urn, name), (row.after or {}).get(key)):
                conflicts.append(key)
            else:
                restore.append(make_mcp(urn, name, value or _empty(name),
                                        system_properties={"cairn_source": "bulk-undo", "cairn_batch": bid}))
        if conflicts:
            raise BulkError(f"changed since this batch was applied, not undone: {', '.join(conflicts[:5])}", 409)
        if restore:
            self.graph.emit(restore)
        with self.engine.begin() as conn:
            conn.execute(update(bulk_batches).where(bulk_batches.c.id == bid).values(state="UNDONE",
                                                                                     updated_ms=_now()))
        if self.audit is not None:
            self.audit.record("governance", "bulk.undone", actor, bid, {"aspects": len(restore)})
        return {"batch_id": bid, "restored": len(restore)}

    def history(self, actor: str, limit: int = 20) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(bulk_batches).where(bulk_batches.c.actor == actor)
                                .order_by(bulk_batches.c.created_ms.desc()).limit(limit)).all()
        return [{"batch_id": r.id, "state": r.state, "rows": r.rows, "changes": len(r.changes or []),
                 "created_ms": r.created_ms, "updated_ms": r.updated_ms} for r in rows]


def _cell(change: dict) -> str:
    to = change["to"]
    if isinstance(to, list):
        return ";".join(to) if to else CLEAR
    return to if to else CLEAR


def _empty(aspect: str) -> dict:
    return {"globalTags": {"tags": []}, "glossaryTerms": {"terms": []}, "ownership": {"owners": []},
            "domains": {"domains": []}, "editableSchemaMetadata": {"editableSchemaFieldInfo": []},
            "editableDatasetProperties": {}}.get(aspect, {})
