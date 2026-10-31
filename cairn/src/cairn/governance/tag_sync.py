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

Dialects and what a mapping's ``tag`` and ``value`` mean there:

* ``snowflake``: tag objects (``SET TAG schema.tag = 'value'``) on tables and columns;
* ``databricks``: Unity Catalog tags (``SET TAGS ('tag' = 'value')``);
* ``bigquery``: on columns, the policy tag whose resource name is ``value``
  (``projects/p/locations/l/taxonomies/t/policyTags/x``; ``tag`` names the taxonomy for bookkeeping);
  on tables, labels (``tag = value``). Written through the BigQuery API with the connection's credentials;
* ``redshift``: on columns, dynamic data masking: ``tag`` is a masking policy, attached ``TO PUBLIC``
  (or ``value: ROLE name``);
* ``lakeformation``: LF-Tags (``tag = value``) on Glue tables and columns, through the AWS API in
  ``region`` (the tags must exist in Lake Formation);
* ``postgres``: descriptions only.

``descriptions: true`` also writes the descriptions people edited in Cairn as the warehouse's table and
column comments (BigQuery: ``description`` options; Lake Formation targets skip it).

Cairn only ever sets and unsets the tags and comments it applied itself (kept in
``cairn_tag_sync_state``); what people set in the warehouse is left alone. Run it on a role that may
only apply tags and comments.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Column, String, Table, and_, delete, func, select
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


COMMENT = "__comment__"                       # a description, kept in the state as a hash


class Target(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{0,62}$")
    dialect: Literal["snowflake", "databricks", "bigquery", "redshift", "lakeformation", "postgres"]
    connection: Optional[str] = None         # no connection: plans only
    platform: str
    tag_schema: Optional[str] = None         # Snowflake: DB.SCHEMA holding the tag objects
    mappings: list[Mapping] = Field(default_factory=list)
    tables: bool = True                      # dataset-level labels become table tags
    descriptions: bool = False               # Cairn-edited descriptions become table and column comments
    region: Optional[str] = None             # lakeformation: the AWS region
    catalog_id: Optional[str] = None         # lakeformation: the AWS account, when not the caller's
    dry_run: bool = False                    # plan on every sweep, never write

    @model_validator(mode="after")
    def _something(self) -> "Target":
        if not self.mappings and not self.descriptions:
            raise ValueError("a target needs mappings, descriptions: true, or both")
        if self.dialect == "postgres" and self.mappings:
            raise ValueError("postgres has no tags: use descriptions only")
        if self.dialect == "lakeformation" and not self.region:
            raise ValueError("a lakeformation target needs its region")
        return self

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
    if target.descriptions and target.dialect != "lakeformation":
        table_doc = (aspects.get("editableDatasetProperties") or {}).get("description")
        if table_doc:
            out[(urn, COMMENT)] = table_doc
        for f in (aspects.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or []:
            if f.get("fieldPath") and f.get("description"):
                out[(f"{urn}|{f['fieldPath']}", COMMENT)] = f["description"]
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


def stored(tag: str, value: str) -> str:
    """What the state keeps: a description's hash (it may be long), a tag's value."""
    return "sha1:" + hashlib.sha1(value.encode()).hexdigest() if tag == COMMENT else value


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


def _field(target: Target, field: str) -> str:
    """The warehouse column for a schema field path (the last part of a nested ``[version=2.0]…`` path)."""
    name = field.split(".")[-1] if field.startswith("[") else field
    return name.upper() if target.dialect == "snowflake" and name == name.lower() else name


def statement(target: Target, op: dict) -> str:
    """One statement (or, for API dialects, one ``api:{json}`` instruction) for one operation."""
    _, name = dataset_name(op["urn"])
    field = _field(target, op["field"]) if op.get("field") else None
    tag, value, kind = op["tag"], op.get("value"), op["op"]
    if tag == COMMENT:
        return _comment(target, name, field, value if kind == "SET" else None)
    if target.dialect == "bigquery":
        return "api:" + json.dumps({"api": "bigquery", "table": name, "field": field, "tag": tag, "value": value,
                                    "op": kind}, sort_keys=True)
    if target.dialect == "lakeformation":
        db, _, table = name.rpartition(".")
        return "api:" + json.dumps({"api": "lakeformation", "region": target.region, "catalog_id": target.catalog_id,
                                    "database": db.split(".")[-1], "table": table, "field": field, "tag": tag,
                                    "value": value, "op": kind}, sort_keys=True)
    if target.dialect == "redshift":
        if not field:
            raise ValueError("Redshift masking policies attach to columns")
        to = value if (value or "").upper().startswith("ROLE ") else "PUBLIC"
        if to != "PUBLIC":
            role = to[5:].strip()
            if not _IDENT.match(role):
                raise ValueError(f"role {role!r} must be a plain identifier")
            to = f"ROLE {_quote(role, 'redshift')}"
        table = ".".join(_quote(p, "redshift") for p in name.split(".")[-2:])
        return (f"ATTACH MASKING POLICY {_quote(tag, 'redshift')} ON {table}({_quote(field, 'redshift')}) TO {to}"
                if kind == "SET" else
                f"DETACH MASKING POLICY {_quote(tag, 'redshift')} ON {table}({_quote(field, 'redshift')}) FROM {to}")
    table = table_ref(name, target.dialect)
    if target.dialect == "snowflake":
        tag_ref = f"{target.tag_schema}.{tag}" if target.tag_schema else tag
        head = f"ALTER TABLE {table} MODIFY COLUMN {_quote(field, 'snowflake')}" if field else f"ALTER TABLE {table}"
        return f"{head} SET TAG {tag_ref} = {_literal(value)}" if kind == "SET" else f"{head} UNSET TAG {tag_ref}"
    head = f"ALTER TABLE {table} ALTER COLUMN {_quote(field, 'databricks')}" if field else f"ALTER TABLE {table}"
    return (f"{head} SET TAGS ({_literal(tag)} = {_literal(value)})" if kind == "SET"
            else f"{head} UNSET TAGS ({_literal(tag)})")


def _comment(target: Target, name: str, field: Optional[str], text: Optional[str]) -> str:
    d = target.dialect
    if d == "bigquery":
        table = "`" + name.replace("`", "") + "`"
        opt = f"description={_literal(text) if text is not None else 'NULL'}"
        return (f"ALTER TABLE {table} ALTER COLUMN {_quote(field, 'databricks')} SET OPTIONS ({opt})" if field
                else f"ALTER TABLE {table} SET OPTIONS ({opt})")
    if d in ("postgres", "redshift"):
        table = ".".join(_quote(p, d) for p in name.split(".")[-2:])
        obj = f"COLUMN {table}.{_quote(field, d)}" if field else f"TABLE {table}"
        return f"COMMENT ON {obj} IS {_literal(text) if text is not None else 'NULL'}"
    table = table_ref(name, d)
    if d == "databricks" and field:
        return f"ALTER TABLE {table} ALTER COLUMN {_quote(field, d)} COMMENT {_literal(text or '')}"
    obj = f"COLUMN {table}.{_quote(field, d)}" if field else f"TABLE {table}"
    return f"COMMENT ON {obj} IS {_literal(text) if text is not None else 'NULL'}"


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
            if state.get((key, tag)) != stored(tag, value):
                ops.append({"op": "SET", "key": key, "tag": tag, "value": value})
        for (key, tag), value in sorted(state.items()):
            if (key, tag) not in desired:
                ops.append({"op": "UNSET", "key": key, "tag": tag, "value": value})
        for op in ops:
            urn, _, field = op["key"].partition("|")
            op.update(urn=urn, field=field or None)
            try:
                op["sql"] = statement(target, op)
            except ValueError as e:                       # e.g. a table-level Redshift label: not possible
                op["sql"], op["skip"] = None, str(e)
        skipped = [op for op in ops if op.get("skip")]
        ops = [op for op in ops if not op.get("skip")]
        conflicts += [f"{op['key']}: {op['skip']}" for op in skipped]
        return {"target": target.id, "datasets": len(seen), "tagged_objects": len({k for k, _ in desired}),
                "operations": ops, "conflicts": conflicts}

    def sync(self, target_id: str, actor: str, dry_run: bool = False, urns: Optional[list[str]] = None) -> dict:
        target = self.target(target_id)
        out = self.plan(target, urns)
        dry_run = dry_run or target.dry_run or (not target.connection and target.dialect != "lakeformation")
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
                            target_id=target.id, object_key=op["key"], tag=op["tag"],
                            value=stored(op["tag"], op["value"]), synced_ms=now))
        failed = sum(1 for op in out["operations"] if op.get("error"))
        out["applied"] = 0 if dry_run else len(out["operations"]) - failed
        out["failed"] = failed
        summary = {k: out[k] for k in ("datasets", "tagged_objects", "applied", "failed")}
        summary.update(planned=len(out["operations"]), conflicts=len(out["conflicts"]))
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            # Runs are ordered by started_ms: keep it strictly increasing per target, so two runs in the same
            # millisecond still have a well-defined latest one.
            prev = conn.execute(select(func.max(tag_sync_runs.c.started_ms)).where(tag_sync_runs.c.target_id == target.id)).scalar()
            now = max(now, (prev or 0) + 1)
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
                        "descriptions": t.descriptions,
                        "dry_run": t.dry_run or (not t.connection and t.dialect != "lakeformation"),
                        "mappings": [m.model_dump() for m in t.mappings],
                        "managed_tags": len(self._state(t.id)),
                        "last_run": None if last is None else {"at_ms": last.started_ms, "actor": last.actor,
                                                               "dry_run": last.dry_run == "true", **last.summary}})
        return out


def sqlalchemy_executor(registry) -> Callable[[str, list[str]], list[Optional[str]]]:
    """Run each statement on its own, so one failure (a missing tag, a dropped table) does not
    block the rest."""
    from sqlalchemy import text

    def run(connection: str, statements: list[str]) -> list[Optional[str]]:
        engine = registry.engine(connection) if connection else None
        errors: list[Optional[str]] = []
        for sql in statements:
            try:
                if sql.startswith("api:"):
                    run_api(json.loads(sql[4:]), engine)
                else:
                    with engine.begin() as conn:
                        conn.execute(text(sql))
                errors.append(None)
            except Exception as e:  # noqa: BLE001 - reported per statement
                errors.append(str(e).splitlines()[0][:500])
        return errors

    return run


def run_api(op: dict, engine=None, clients: Optional[dict] = None) -> None:
    """One tag operation through a warehouse API (``clients`` replaces the real client in tests)."""
    clients = clients or {}
    if op["api"] == "bigquery":
        client = clients.get("bigquery") or engine.raw_connection().connection._client   # google.cloud.bigquery
        from google.cloud import bigquery

        table = client.get_table(op["table"])
        if op["field"] is None:
            labels = dict(table.labels or {})
            if op["op"] == "SET":
                labels[op["tag"].lower()] = (op["value"] or "").lower()
            else:
                labels[op["tag"].lower()] = None                 # the API removes a label set to None
            table.labels = labels
            client.update_table(table, ["labels"])
            return
        schema = []
        for f in table.schema:
            if f.name == op["field"]:
                names = [n for n in ((f.policy_tags.names if f.policy_tags else []) or []) if n != op["value"]]
                if op["op"] == "SET":
                    names.append(op["value"])
                f = bigquery.SchemaField(f.name, f.field_type, mode=f.mode, description=f.description,
                                         fields=f.fields, policy_tags=bigquery.PolicyTagList(names))
            schema.append(f)
        table.schema = schema
        client.update_table(table, ["schema"])
        return
    if op["api"] == "lakeformation":
        lf = clients.get("lakeformation")
        if lf is None:
            import boto3

            lf = boto3.client("lakeformation", region_name=op["region"])
        table: dict[str, Any] = {"DatabaseName": op["database"], "Name": op["table"]}
        if op.get("catalog_id"):
            table["CatalogId"] = op["catalog_id"]
        resource = ({"TableWithColumns": {**table, "ColumnNames": [op["field"]]}} if op["field"]
                    else {"Table": table})
        pair = {"TagKey": op["tag"], "TagValues": [op["value"]]}
        if op.get("catalog_id"):
            pair["CatalogId"] = op["catalog_id"]
        call = lf.add_lf_tags_to_resource if op["op"] == "SET" else lf.remove_lf_tags_from_resource
        out = call(Resource=resource, LFTags=[pair])
        if out.get("Failures"):
            raise RuntimeError(str(out["Failures"][0].get("Error") or out["Failures"][0])[:300])
        return
    raise ValueError(f"unknown API {op['api']}")
