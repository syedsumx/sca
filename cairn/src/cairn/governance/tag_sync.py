"""Push catalog classifications to warehouse tags, so the warehouse's own tag-based masking and
access policies protect data queried directly (not only through Cairn).

Targets are reviewed YAML files (``CAIRN_TAG_SYNC``, default ``/etc/cairn/config/tag-sync``)::

    targets:
      - id: snowflake-prod
        dialect: snowflake               # or databricks (Unity Catalog)
        connection: snowflake_prod       # a named warehouse connection (CAIRN_QUALITY_CONNECTIONS)
        platform: snowflake              # datasets of this platform are synced
        tag_schema: GOVERNANCE.TAGS      # Snowflake: where the tags are defined
        mappings:                        # first matching mapping wins per warehouse tag
          - {label: "urn:li:glossaryTerm:Classification.PII.Email", tag: PII, value: email}
          - {label: "urn:li:glossaryTerm:Classification.PII.*", tag: PII, value: other}
          - {label: "urn:li:tag:Confidential", tag: CONFIDENTIALITY, value: high}

Cairn only ever sets and unsets the tags it applied itself (kept in ``cairn_tag_sync_state``);
tags set by people in the warehouse are left alone. Run it on a role that may only apply tags.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Column, String, Table, and_, delete, select
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

tag_sync_state = Table(
    "cairn_tag_sync_state", metadata,
    Column("target_id", String(64), primary_key=True),
    Column("object_key", String(1024), primary_key=True),     # dataset urn, or "urn|fieldPath"
    Column("tag", String(256), primary_key=True),
    Column("value", String(256), nullable=False),
    Column("synced_ms", BigInteger, nullable=False),
)

tag_sync_runs = Table(
    "cairn_tag_sync_runs", metadata,
    Column("id", String(40), primary_key=True),
    Column("target_id", String(64), nullable=False, index=True),
    Column("actor", String(512), nullable=False),
    Column("dry_run", String(5), nullable=False),
    Column("summary", JSONText, nullable=False),
    Column("started_ms", BigInteger, nullable=False),
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class Mapping(BaseModel):
    label: str = Field(min_length=1)          # tag or glossary term urn; a trailing * matches a prefix
    tag: str
    value: str = Field(min_length=1, max_length=256)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not v.startswith(("urn:li:tag:", "urn:li:glossaryTerm:")):
            raise ValueError("label must be a tag or glossary term urn")
        return v

    @field_validator("tag")
    @classmethod
    def _tag(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"tag {v!r} must be a plain identifier")
        return v

    def matches(self, urn: str) -> bool:
        return fnmatch.fnmatchcase(urn, self.label) if "*" in self.label else urn == self.label


class Target(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{0,62}$")
    dialect: Literal["snowflake", "databricks"]
    connection: Optional[str] = None         # no connection: plans only
    platform: str
    tag_schema: Optional[str] = None         # Snowflake: DB.SCHEMA holding the tag objects
    mappings: list[Mapping] = Field(min_length=1)
    tables: bool = True                      # dataset-level labels become table tags
    dry_run: bool = False                    # plan on every sweep, never write

    @field_validator("tag_schema")
    @classmethod
    def _schema(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not all(_IDENT.match(p) for p in v.split(".")):
            raise ValueError("tag_schema must be DB.SCHEMA")
        return v


class TagSyncConfig(BaseModel):
    targets: list[Target] = Field(default_factory=list)


def load_targets(path: Optional[str] = None) -> TagSyncConfig:
    """Targets from a YAML file or every ``*.yaml``/``*.yml`` in a directory. Read on every sweep."""
    import yaml

    path = path or os.environ.get("CAIRN_TAG_SYNC", "/etc/cairn/config/tag-sync")
    if not path or not os.path.exists(path):
        return TagSyncConfig()
    files = [path] if os.path.isfile(path) else sorted(
        os.path.join(path, f) for f in os.listdir(path) if f.endswith((".yaml", ".yml")))
    targets: list = []
    for f in files:
        with open(f) as fh:
            targets += (yaml.safe_load(fh) or {}).get("targets") or []
    return TagSyncConfig.model_validate({"targets": targets})


# --------------------------------------------------------------------------- desired state
def _labels(info: dict) -> list[str]:
    tags = [t.get("tag") for t in ((info.get("globalTags") or {}).get("tags") or [])]
    terms = [t.get("urn") for t in ((info.get("glossaryTerms") or {}).get("terms") or [])]
    return [u for u in tags + terms if u]


def dataset_name(urn: str) -> tuple[str, str]:
    """(platform, name) from urn:li:dataset:(urn:li:dataPlatform:p,name,ENV)."""
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),[^,]+\)$", urn)
    if not m:
        raise ValueError(f"not a dataset urn: {urn}")
    return m.group(1), m.group(2)


def desired_tags(target: Target, urn: str, aspects: dict) -> tuple[dict[tuple[str, str], str], list[str]]:
    """{(object_key, tag): value} for one dataset, plus conflict notes. Columns take labels from
    both the ingested schema and the catalog's edits."""
    labels: dict[str, list[str]] = {}
    if target.tables:
        labels[urn] = _labels({"globalTags": aspects.get("globalTags"), "glossaryTerms": aspects.get("glossaryTerms")})
    for f in (aspects.get("schemaMetadata") or {}).get("fields") or []:
        if f.get("fieldPath"):
            labels.setdefault(f"{urn}|{f['fieldPath']}", []).extend(_labels(f))
    for f in (aspects.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or []:
        if f.get("fieldPath"):
            labels.setdefault(f"{urn}|{f['fieldPath']}", []).extend(_labels(f))
    out: dict[tuple[str, str], str] = {}
    conflicts: list[str] = []
    for key, found in labels.items():
        for m in target.mappings:                      # mapping order is priority
            hit = next((label for label in found if m.matches(label)), None)
            if hit is None:
                continue
            if (key, m.tag) in out:
                if out[(key, m.tag)] != m.value and "*" not in m.label:   # a wildcard is a fallback
                    conflicts.append(f"{key}: {m.tag} = {out[(key, m.tag)]!r} kept over {m.value!r} ({hit})")
                continue
            out[(key, m.tag)] = m.value
    return out, conflicts


# --------------------------------------------------------------------------- SQL
def _quote(part: str, dialect: str) -> str:
    return f"`{part.replace('`', '``')}`" if dialect == "databricks" else '"' + part.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def table_ref(name: str, dialect: str) -> str:
    """db.schema.table → quoted three-part name. Snowflake ingestion lower-cases names, and quoted
    identifiers are case-sensitive there, so names are upper-cased unless they are mixed case."""
    parts = name.split(".")
    if len(parts) != 3:
        raise ValueError(f"expected database.schema.table, got {name!r}")
    if dialect == "snowflake":
        parts = [p.upper() if p == p.lower() else p for p in parts]
    return ".".join(_quote(p, dialect) for p in parts)


def statement(target: Target, op: dict) -> str:
    """One ALTER for a SET or UNSET of one tag on a table or column."""
    _, name = dataset_name(op["urn"])
    table = table_ref(name, target.dialect)
    field = op.get("field")
    tag, value = op["tag"], op.get("value")
    if target.dialect == "snowflake":
        tag_ref = f"{target.tag_schema}.{tag}" if target.tag_schema else tag
        col = field.upper() if field and field == field.lower() else field
        head = f"ALTER TABLE {table} MODIFY COLUMN {_quote(col, 'snowflake')}" if field else f"ALTER TABLE {table}"
        return f"{head} SET TAG {tag_ref} = {_literal(value)}" if op["op"] == "SET" else f"{head} UNSET TAG {tag_ref}"
    head = f"ALTER TABLE {table} ALTER COLUMN {_quote(field, 'databricks')}" if field else f"ALTER TABLE {table}"
    return (f"{head} SET TAGS ({_literal(tag)} = {_literal(value)})" if op["op"] == "SET"
            else f"{head} UNSET TAGS ({_literal(tag)})")


# --------------------------------------------------------------------------- service
class TagSyncError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class TagSyncService:
    def __init__(self, engine: Engine, datasets: Callable[[], list[tuple[str, dict, dict]]],
                 execute: Callable[[str, list[str]], list[Optional[str]]],
                 targets: Callable[[], TagSyncConfig] = load_targets, audit=None):
        """``execute(connection, statements)`` runs each statement and returns an error per statement
        (``None`` on success)."""
        self.engine = engine
        self.datasets = datasets
        self.execute = execute
        self.targets = targets
        self.audit = audit

    def target(self, target_id: str) -> Target:
        for t in self.targets().targets:
            if t.id == target_id:
                return t
        raise TagSyncError("unknown tag-sync target", 404)

    def _state(self, target_id: str) -> dict[tuple[str, str], str]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(tag_sync_state).where(tag_sync_state.c.target_id == target_id)).all()
        return {(r.object_key, r.tag): r.value for r in rows}

    def plan(self, target: Target, urns: Optional[list[str]] = None) -> dict:
        desired: dict[tuple[str, str], str] = {}
        conflicts: list[str] = []
        seen: set[str] = set()
        for urn, _resource, aspects in self.datasets():
            if urns is not None and urn not in urns:
                continue
            try:
                platform, _ = dataset_name(urn)
            except ValueError:
                continue
            if platform != target.platform:
                continue
            seen.add(urn)
            want, notes = desired_tags(target, urn, aspects)
            desired.update(want)
            conflicts += notes
        state = self._state(target.id)
        if urns is not None:   # a scoped plan never unsets tags on datasets outside its scope
            state = {k: v for k, v in state.items() if k[0].split("|", 1)[0] in seen}
        ops = []
        for (key, tag), value in sorted(desired.items()):
            if state.get((key, tag)) != value:
                ops.append({"op": "SET", "key": key, "tag": tag, "value": value})
        for (key, tag), value in sorted(state.items()):
            if (key, tag) not in desired:
                ops.append({"op": "UNSET", "key": key, "tag": tag, "value": value})
        for op in ops:
            urn, _, field = op["key"].partition("|")
            op.update(urn=urn, field=field or None)
            op["sql"] = statement(target, op)
        return {"target": target.id, "datasets": len(seen), "tagged_objects": len({k for k, _ in desired}),
                "operations": ops, "conflicts": conflicts}

    def sync(self, target_id: str, actor: str, dry_run: bool = False, urns: Optional[list[str]] = None) -> dict:
        target = self.target(target_id)
        out = self.plan(target, urns)
        dry_run = dry_run or target.dry_run or not target.connection
        out["dry_run"] = dry_run
        if not dry_run and out["operations"]:
            errors = self.execute(target.connection, [op["sql"] for op in out["operations"]])
            now = int(time.time() * 1000)
            with self.engine.begin() as conn:
                for op, err in zip(out["operations"], errors):
                    op["error"] = err
                    if err:
                        continue
                    where = and_(tag_sync_state.c.target_id == target.id, tag_sync_state.c.object_key == op["key"],
                                 tag_sync_state.c.tag == op["tag"])
                    conn.execute(delete(tag_sync_state).where(where))
                    if op["op"] == "SET":
                        conn.execute(tag_sync_state.insert().values(
                            target_id=target.id, object_key=op["key"], tag=op["tag"], value=op["value"], synced_ms=now))
        failed = sum(1 for op in out["operations"] if op.get("error"))
        out["applied"] = 0 if dry_run else len(out["operations"]) - failed
        out["failed"] = failed
        summary = {k: out[k] for k in ("datasets", "tagged_objects", "applied", "failed")}
        summary.update(planned=len(out["operations"]), conflicts=len(out["conflicts"]))
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            conn.execute(tag_sync_runs.insert().values(id=f"tsr_{uuid.uuid4().hex[:16]}", target_id=target.id,
                                                       actor=actor, dry_run=str(dry_run).lower(), summary=summary,
                                                       started_ms=now))
        if self.audit is not None and not dry_run:
            self.audit.record("governance", "tag_sync.applied", actor, target.id, summary)
        return out

    def sweep(self, actor: str = "tag-sync") -> dict[str, Any]:
        """Sync every target (dry-run targets only plan). Errors on one target don't stop the others."""
        results: dict[str, Any] = {}
        for t in self.targets().targets:
            try:
                r = self.sync(t.id, actor)
                results[t.id] = {k: r[k] for k in ("applied", "failed", "dry_run")} | {"planned": len(r["operations"])}
            except Exception as e:  # noqa: BLE001 - one bad target must not stop the sweep
                logger.warning("tag-sync %s failed: %s", t.id, e)
                results[t.id] = {"error": str(e)}
        return results

    def overview(self) -> list[dict]:
        out = []
        for t in self.targets().targets:
            with self.engine.connect() as conn:
                last = conn.execute(select(tag_sync_runs).where(tag_sync_runs.c.target_id == t.id)
                                    .order_by(tag_sync_runs.c.started_ms.desc()).limit(1)).first()
            out.append({"id": t.id, "dialect": t.dialect, "platform": t.platform, "connection": t.connection,
                        "dry_run": t.dry_run or not t.connection, "mappings": [m.model_dump() for m in t.mappings],
                        "managed_tags": len(self._state(t.id)),
                        "last_run": None if last is None else {"at_ms": last.started_ms, "actor": last.actor,
                                                               "dry_run": last.dry_run == "true", **last.summary}})
        return out


def sqlalchemy_executor(registry) -> Callable[[str, list[str]], list[Optional[str]]]:
    """Run each statement on its own, so one failure (a missing tag, a dropped table) does not
    block the rest."""
    from sqlalchemy import text

    def run(connection: str, statements: list[str]) -> list[Optional[str]]:
        engine = registry.engine(connection)
        errors: list[Optional[str]] = []
        for sql in statements:
            try:
                with engine.begin() as conn:
                    conn.execute(text(sql))
                errors.append(None)
            except Exception as e:  # noqa: BLE001 - reported per statement
                errors.append(str(e).splitlines()[0][:500])
        return errors

    return run
