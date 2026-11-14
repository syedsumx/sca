"""Data contracts: a producer's versioned promise about one dataset (its schema, freshness, volume
and quality rules) that passes or fails as a whole.

``contract.yaml``::

    dataset: urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.orders,PROD)
    version: 1.2.0
    owner: urn:li:corpGroup:orders-team
    schema:
      allow_extra_columns: true
      columns:
        - {name: id, type: integer, required: true}
        - {name: email, type: string}
    freshness: {max_age_hours: 24}
    volume: {min_rows: 1000}
    quality:
      - {name: email nulls, type: field_metric, config: {field: email, metric: null_pct, max: 5}}

The schema part is checked against the catalog (and, in CI, against a proposed dbt model with
``cairn contract check``); the other parts become Cairn monitors owned by the contract.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Column, String, Table, delete, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.quality.models import MonitorSpec

logger = logging.getLogger(__name__)

contracts = Table(
    "cairn_contracts", metadata,
    Column("dataset_urn", String(512), primary_key=True),
    Column("version", String(64), nullable=False),
    Column("spec", JSONText, nullable=False),
    Column("owner", String(512), nullable=True),
    Column("status", String(16), nullable=False),           # MET | BROKEN | PENDING
    Column("status_detail", JSONText, nullable=True),
    Column("status_ms", BigInteger, nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

contract_versions = Table(
    "cairn_contract_versions", metadata,
    Column("dataset_urn", String(512), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("spec", JSONText, nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

PREFIX = "contract-"
_TYPE_FAMILIES = [
    ("BOOLEAN", r"^(bool|boolean)$"),
    ("INTEGER", r"^(tinyint|smallint|int|integer|bigint|int2|int4|int8|long|short|byte|number\(\d+,\s*0\))$"),
    ("NUMBER", r"^(number|numeric|decimal|float|float4|float8|double|double precision|real)(\(.*\))?$"),
    ("STRING", r"^(string|text|varchar|char|character|character varying|nvarchar|nchar|bpchar|varchar2)(\(.*\))?$"),
    ("TIMESTAMP", r"^(timestamp|datetime|timestamptz|timestamp_ntz|timestamp_ltz|timestamp_tz)(\(.*\))?"
                  r"( with(out)? time zone)?$"),
    ("DATE", r"^date$"),
    ("BINARY", r"^(binary|varbinary|bytea|blob|bytes)(\(.*\))?$"),
    ("JSON", r"^(json|jsonb|variant|object|struct.*|map.*|array.*)$"),
]


def type_family(native: Optional[str]) -> Optional[str]:
    """Warehouse-neutral type family: ``VARCHAR(255)`` and ``text`` are both STRING."""
    if not native:
        return None
    t = native.strip().lower()
    for family, pattern in _TYPE_FAMILIES:
        if re.match(pattern, t):
            return family
    return t.upper()


class ContractColumn(BaseModel):
    name: str = Field(min_length=1)
    type: Optional[str] = None
    required: bool = False          # NOT NULL


class SchemaPart(BaseModel):
    columns: list[ContractColumn] = Field(min_length=1)
    allow_extra_columns: bool = True


class FreshnessPart(BaseModel):
    max_age_hours: float = Field(gt=0, le=24 * 90)


class VolumePart(BaseModel):
    min_rows: Optional[int] = Field(default=None, ge=0)
    max_rows: Optional[int] = Field(default=None, ge=0)
    learned: bool = False


class QualityRule(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: Literal["field_metric", "custom_sql", "distribution", "volume"]
    config: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"


class ContractSpec(BaseModel):
    dataset: str = Field(pattern=r"^urn:li:dataset:\(.+\)$")
    version: str = Field(pattern=r"^[0-9A-Za-z.\-+_]{1,64}$")
    owner: Optional[str] = None
    description: str = ""
    schema_: Optional[SchemaPart] = Field(default=None, alias="schema")
    freshness: Optional[FreshnessPart] = None
    volume: Optional[VolumePart] = None
    quality: list[QualityRule] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("owner")
    @classmethod
    def _owner(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.startswith("urn:li:"):
            return f"urn:li:corpuser:{v}"
        return v

    @model_validator(mode="after")
    def _something(self) -> "ContractSpec":
        if not (self.schema_ or self.freshness or self.volume or self.quality):
            raise ValueError("a contract needs at least one of schema, freshness, volume or quality")
        names = [r.name for r in self.quality]
        if len(names) != len(set(names)):
            raise ValueError("quality rule names must be unique")
        self.monitor_specs()      # every rule must be a valid monitor
        return self

    def dump(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def monitor_specs(self) -> dict[str, MonitorSpec]:
        """Monitors this contract owns, keyed by a stable id."""
        h = hashlib.sha1(self.dataset.encode()).hexdigest()[:10]
        label = self.dataset.split(",")[-2] if "," in self.dataset else self.dataset
        out: dict[str, MonitorSpec] = {}
        tag = {"contract": self.dataset, "contract_version": self.version}
        if self.freshness:
            out[f"{PREFIX}{h}-freshness"] = MonitorSpec(
                name=f"{label} freshness (contract)", dataset_urn=self.dataset, type="freshness", severity="HIGH",
                config={**tag, "sla_seconds": self.freshness.max_age_hours * 3600})
        if self.volume:
            cfg: dict[str, Any] = {**tag}
            if self.volume.min_rows is not None:
                cfg["min_rows"] = self.volume.min_rows
            if self.volume.max_rows is not None:
                cfg["max_rows"] = self.volume.max_rows
            if self.volume.learned:
                cfg["model"] = "learned"
            out[f"{PREFIX}{h}-volume"] = MonitorSpec(name=f"{label} volume (contract)", dataset_urn=self.dataset,
                                                     type="volume", severity="HIGH", config=cfg)
        for r in self.quality:
            slug = re.sub(r"[^a-z0-9]+", "-", r.name.lower()).strip("-")[:40] or "rule"
            out[f"{PREFIX}{h}-q-{slug}"] = MonitorSpec(name=f"{label}: {r.name} (contract)", dataset_urn=self.dataset,
                                                       type=r.type, severity=r.severity, config={**r.config, **tag})
        return out


def schema_violations(part: SchemaPart, actual: dict[str, dict]) -> list[str]:
    """What breaks the schema promise. ``actual`` maps column name → {type, nullable}. Names
    compare case-insensitively, types by family."""
    by_name = {k.lower(): (k, v) for k, v in actual.items()}
    out = []
    for c in part.columns:
        hit = by_name.get(c.name.lower())
        if hit is None:
            out.append(f"column {c.name} is missing")
            continue
        _, col = hit
        if c.type and type_family(col.get("type")) and type_family(c.type) != type_family(col.get("type")):
            out.append(f"column {c.name} is {col.get('type')}, the contract says {c.type}")
        if c.required and col.get("nullable") is True:
            out.append(f"column {c.name} must not be nullable")
    if not part.allow_extra_columns:
        promised = {c.name.lower() for c in part.columns}
        extra = sorted(k for k in actual if k.lower() not in promised)
        if extra:
            out.append(f"columns not in the contract: {', '.join(extra)}")
    return out


def catalog_columns(schema_metadata: Optional[dict]) -> dict[str, dict]:
    return {f["fieldPath"].split(".")[-1]: {"type": f.get("nativeDataType"), "nullable": f.get("nullable")}
            for f in (schema_metadata or {}).get("fields") or [] if f.get("fieldPath")}


def dbt_columns(manifest: dict, model: str) -> dict[str, dict]:
    """Columns of a dbt model from ``manifest.json`` (by model name or unique id). Types come from
    the model's documented ``data_type``; a ``not_null`` test makes a column non-nullable."""
    nodes = manifest.get("nodes") or {}
    node = nodes.get(model) or next((n for n in nodes.values() if n.get("resource_type") == "model"
                                     and model in (n.get("name"), n.get("alias"))), None)
    if node is None:
        raise ValueError(f"model {model!r} not in the manifest")
    not_null = {(n.get("column_name") or (n.get("test_metadata") or {}).get("kwargs", {}).get("column_name"))
                for n in nodes.values() if n.get("resource_type") == "test"
                and (n.get("test_metadata") or {}).get("name") == "not_null" and node["unique_id"] in
                ((n.get("depends_on") or {}).get("nodes") or [])}
    return {name: {"type": c.get("data_type"), "nullable": name not in not_null}
            for name, c in (node.get("columns") or {}).items()}


def check_proposed(spec: ContractSpec, columns: dict[str, dict]) -> list[str]:
    """For CI: does a proposed schema keep the promise? An unknown type in the proposal is not a
    violation (dbt models often leave data_type out); a nullable column is one only when the
    proposal says so."""
    if spec.schema_ is None:
        return []
    return schema_violations(spec.schema_, columns)


class ContractError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


class ContractService:
    def __init__(self, engine: Engine, graph, quality, notify: Optional[Callable[[str, dict, str, str], None]] = None,
                 audit=None):
        """``quality`` is the QualityEngine (its store holds the contract's monitors). ``notify(kind,
        contract_row, old_status, new_status)`` is called when a contract breaks or recovers."""
        self.engine = engine
        self.graph = graph
        self.quality = quality
        self.notify = notify
        self.audit = audit

    # ---- definitions -------------------------------------------------------------------
    def upsert(self, spec: ContractSpec, actor: str) -> dict:
        current = self.get(spec.dataset)
        if current is not None:
            same_version = current["version"] == spec.version
            if same_version and current["spec"] != spec.dump():
                raise ContractError(f"version {spec.version} already exists with different terms; bump the version",
                                    409)
            if same_version:
                return self.status(spec.dataset)
        now = _now()
        with self.engine.begin() as conn:
            if current is None:
                conn.execute(contracts.insert().values(
                    dataset_urn=spec.dataset, version=spec.version, spec=spec.dump(), owner=spec.owner,
                    status="PENDING", status_detail=None, status_ms=now, created_by=actor, created_ms=now,
                    updated_ms=now))
            else:
                conn.execute(update(contracts).where(contracts.c.dataset_urn == spec.dataset).values(
                    version=spec.version, spec=spec.dump(), owner=spec.owner, updated_ms=now))
            published = conn.execute(select(contract_versions.c.spec).where(
                (contract_versions.c.dataset_urn == spec.dataset) & (contract_versions.c.version == spec.version))).first()
            if published is not None and published.spec != spec.dump():
                raise ContractError(f"version {spec.version} was published before with different terms; "
                                    "use a new version", 409)
            if published is None:
                conn.execute(contract_versions.insert().values(dataset_urn=spec.dataset, version=spec.version,
                                                               spec=spec.dump(), created_by=actor, created_ms=now))
        self._sync_monitors(spec)
        if self.audit is not None:
            self.audit.record("quality", "contract.published", actor, spec.dataset, {"version": spec.version})
        return self.refresh(spec.dataset)

    def _sync_monitors(self, spec: Optional[ContractSpec], dataset: Optional[str] = None) -> None:
        dataset = spec.dataset if spec else dataset
        store = self.quality.store
        h = hashlib.sha1(dataset.encode()).hexdigest()[:10]
        existing = {m.id: m for m in store.list_monitors(dataset) if m.id.startswith(f"{PREFIX}{h}-")}
        desired = spec.monitor_specs() if spec else {}
        for mid, ms in desired.items():
            if mid in existing:
                self.quality.sync_monitor(store.update_monitor(mid, ms))
            else:
                self.quality.sync_monitor(store.create_monitor(ms, "contracts", monitor_id=mid))
        for mid in existing:
            if mid not in desired:
                store.delete_monitor(mid)
                self.quality.remove_monitor(mid)

    def delete(self, dataset: str, actor: str) -> bool:
        with self.engine.begin() as conn:
            gone = conn.execute(delete(contracts).where(contracts.c.dataset_urn == dataset)).rowcount
        if gone:
            self._sync_monitors(None, dataset)
            if self.audit is not None:
                self.audit.record("quality", "contract.retired", actor, dataset, {})
        return bool(gone)

    def get(self, dataset: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(contracts).where(contracts.c.dataset_urn == dataset)).first()
        return None if r is None else dict(r._mapping)

    def versions(self, dataset: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(contract_versions).where(contract_versions.c.dataset_urn == dataset)
                                .order_by(contract_versions.c.created_ms.desc())).all()
        return [{"version": r.version, "created_by": r.created_by, "created_ms": r.created_ms, "spec": r.spec}
                for r in rows]

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(contracts).order_by(contracts.c.dataset_urn)).all()
        return [self._public(dict(r._mapping)) for r in rows]

    def statuses(self, urns: list[str]) -> dict[str, str]:
        if not urns:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(contracts.c.dataset_urn, contracts.c.status)
                                .where(contracts.c.dataset_urn.in_(urns))).all()
        return {u: s for u, s in rows}

    def any(self) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(select(contracts.c.dataset_urn).limit(1)).first() is not None

    @staticmethod
    def _public(row: dict) -> dict:
        return {"dataset": row["dataset_urn"], "version": row["version"], "owner": row["owner"],
                "status": row["status"], "parts": (row.get("status_detail") or {}).get("parts", []),
                "checked_ms": row["status_ms"], "spec": row["spec"], "created_by": row["created_by"],
                "updated_ms": row["updated_ms"]}

    # ---- status ------------------------------------------------------------------------
    def evaluate(self, row: dict) -> tuple[str, list[dict]]:
        spec = ContractSpec.model_validate(row["spec"])
        parts: list[dict] = []
        if spec.schema_:
            actual = catalog_columns(self.graph.get_aspect(spec.dataset, "schemaMetadata"))
            if not actual:
                parts.append({"part": "schema", "status": "PENDING", "detail": "no schema in the catalog yet"})
            else:
                broken = schema_violations(spec.schema_, actual)
                parts.append({"part": "schema", "status": "BROKEN" if broken else "MET",
                              "detail": "; ".join(broken) or f"{len(spec.schema_.columns)} column(s) as promised"})
        for mid, ms in spec.monitor_specs().items():
            last = self.quality.store.evaluations(mid, limit=1)
            ev = last[0] if last else None
            status = ("PENDING" if ev is None or ev.status in ("TRAINING", "ERROR") else
                      "BROKEN" if ev.status == "FAIL" else "MET")
            part = "freshness" if ms.type == "freshness" else "volume" if mid.endswith("-volume") else "quality"
            parts.append({"part": part, "name": ms.name, "monitor_id": mid, "status": status,
                          "detail": ev.message if ev else "not evaluated yet"})
        overall = ("BROKEN" if any(p["status"] == "BROKEN" for p in parts) else
                   "PENDING" if any(p["status"] == "PENDING" for p in parts) else "MET")
        return overall, parts

    def refresh(self, dataset: str) -> dict:
        row = self.get(dataset)
        if row is None:
            raise ContractError("no contract for this dataset", 404)
        overall, parts = self.evaluate(row)
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(update(contracts).where(contracts.c.dataset_urn == dataset).values(
                status=overall, status_detail={"parts": parts}, status_ms=now))
        old = row["status"]
        if self.notify is not None and old != overall and (overall == "BROKEN" or old == "BROKEN"):
            try:
                self.notify("broken" if overall == "BROKEN" else "restored", row, old, overall)
            except Exception:  # noqa: BLE001 - a notification failure must not block status
                logger.exception("contract notification failed for %s", dataset)
        return self._public({**row, "status": overall, "status_detail": {"parts": parts}, "status_ms": now})

    def status(self, dataset: str) -> dict:
        return self.refresh(dataset)

    def refresh_all(self) -> dict[str, str]:
        out = {}
        for c in self.list():
            try:
                out[c["dataset"]] = self.refresh(c["dataset"])["status"]
            except Exception as e:  # noqa: BLE001 - one bad contract must not stop the sweep
                logger.warning("contract refresh failed for %s: %s", c["dataset"], e)
        return out
