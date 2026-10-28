"""Metadata export: catalog facts as Parquet (or Iceberg) tables in the customer's own storage.

Seven tables with a stable, versioned schema (:data:`SCHEMA_VERSION`), written to a local path or
object storage (``s3://``, ``gs://``, ``abfs://``)::

    <root>/<table>/export_date=YYYY-MM-DD/<table>.parquet     one partition per day, replaced on rerun
    <root>/_manifest.json                                      the last run: tables, rows, schema version

Register the folders as external tables in the warehouse (Hive-style partitions) and join them to
the data they describe. With ``--format iceberg`` the same tables go to an Iceberg catalog instead,
one snapshot per run (time travel instead of partitions).

Adding a column is a minor schema change; renaming or removing one bumps :data:`SCHEMA_VERSION`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select

from cairn.governance.attributes import resource_from_aspects

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# entity type -> (aspect holding name/description, name key)
ENTITY_TYPES = {
    "dataset": ("datasetProperties", "name"),
    "dataProduct": ("dataProductProperties", "name"),
    "dashboard": ("dashboardInfo", "title"),
    "chart": ("chartInfo", "title"),
    "mlModel": ("mlModelProperties", "name"),
}
COMMON_ASPECTS = ["ownership", "globalTags", "glossaryTerms", "domains", "structuredProperties",
                  "dataPlatformInstance", "dataProducts"]
DATASET_ASPECTS = ["editableDatasetProperties", "schemaMetadata", "editableSchemaMetadata", "upstreamLineage"]


def _schemas():
    import pyarrow as pa

    ts = pa.timestamp("ms", tz="UTC")
    s, ls = pa.string(), pa.list_(pa.string())
    return {
        "assets": pa.schema([("urn", s), ("entity_type", s), ("platform", s), ("name", s), ("description", s),
                             ("domain_urn", s), ("certification", s), ("classification", ls),
                             ("data_products", ls), ("owner_count", pa.int32()), ("exported_at", ts)]),
        "columns": pa.schema([("asset_urn", s), ("field_path", s), ("native_type", s), ("description", s),
                              ("nullable", pa.bool_()), ("labels", ls), ("classification", ls), ("exported_at", ts)]),
        "owners": pa.schema([("asset_urn", s), ("owner_urn", s), ("owner_type", s), ("exported_at", ts)]),
        "labels": pa.schema([("asset_urn", s), ("field_path", s), ("kind", s), ("label_urn", s),
                             ("exported_at", ts)]),
        "lineage": pa.schema([("upstream_urn", s), ("downstream_urn", s), ("lineage_type", s),
                              ("exported_at", ts)]),
        "monitor_results": pa.schema([("monitor_id", s), ("asset_urn", s), ("monitor_type", s), ("name", s),
                                      ("status", s), ("evaluated_at", ts), ("metric_value", pa.float64()),
                                      ("exported_at", ts)]),
        "access_grants": pa.schema([("product_urn", s), ("grantee_urn", s), ("source", s), ("state", s),
                                    ("granted_at", ts), ("expires_at", ts), ("exported_at", ts)]),
    }


TABLES = ("assets", "columns", "owners", "labels", "lineage", "monitor_results", "access_grants")


@dataclass
class ExportResult:
    root: str
    export_date: str
    format: str
    rows: dict[str, int]
    seconds: float

    def as_dict(self) -> dict:
        return {"root": self.root, "export_date": self.export_date, "format": self.format, "rows": self.rows,
                "seconds": round(self.seconds, 2), "schema_version": SCHEMA_VERSION}


def _text(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


class MetadataExporter:
    def __init__(self, graph, quality_store=None, engine=None, now: Callable[[], datetime] = None):
        self.graph = graph
        self.quality_store = quality_store
        self.engine = engine
        self.now = now or (lambda: datetime.now(timezone.utc))

    # ---- collecting ---------------------------------------------------------------------
    def collect(self) -> dict[str, list[dict]]:
        at = self.now()
        out: dict[str, list[dict]] = {t: [] for t in TABLES}
        for etype, (props_aspect, name_key) in ENTITY_TYPES.items():
            aspects = [props_aspect, *COMMON_ASPECTS, *(DATASET_ASPECTS if etype == "dataset" else [])]
            try:
                entities = self.graph.scroll_entities(etype, aspects)
            except Exception as e:  # noqa: BLE001 - an entity type the core does not know
                logger.warning("export: skipping %s: %s", etype, e)
                continue
            for ent in entities:
                self._entity(etype, ent, props_aspect, name_key, at, out)
        out["monitor_results"] = self._monitors(at)
        out["access_grants"] = self._grants(at)
        return out

    def _entity(self, etype: str, ent: dict, props_aspect: str, name_key: str, at: datetime,
                out: dict[str, list[dict]]) -> None:
        urn = ent["urn"]
        res = resource_from_aspects(urn, ent)
        props = ent.get(props_aspect) or {}
        editable = ent.get("editableDatasetProperties") or {}
        out["assets"].append({
            "urn": urn, "entity_type": etype, "platform": res.get("platform"),
            "name": _text(props.get(name_key)) or urn,
            "description": _text(editable.get("description")) or _text(props.get("description")),
            "domain_urn": (res.get("domains") or [None])[0], "certification": res.get("certification"),
            "classification": sorted(res.get("classification") or []), "data_products": res.get("data_products") or [],
            "owner_count": len(res.get("owners") or []), "exported_at": at})
        for o in (ent.get("ownership") or {}).get("owners", []):
            if o.get("owner"):
                out["owners"].append({"asset_urn": urn, "owner_urn": o["owner"],
                                      "owner_type": o.get("typeUrn") or o.get("type"), "exported_at": at})
        for kind, urns in (("tag", res.get("tags") or []), ("term", res.get("glossary_terms") or [])):
            for label in urns:
                out["labels"].append({"asset_urn": urn, "field_path": None, "kind": kind, "label_urn": label,
                                      "exported_at": at})
        if etype != "dataset":
            return
        editable_fields = {f.get("fieldPath"): f for f in
                           (ent.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo", [])}
        sensitive = res.get("sensitive_fields") or {}
        field_labels = res.get("field_labels") or {}
        for f in (ent.get("schemaMetadata") or {}).get("fields", []):
            path = f.get("fieldPath")
            if not path:
                continue
            ed = editable_fields.get(path) or {}
            labels = field_labels.get(path, [])
            out["columns"].append({
                "asset_urn": urn, "field_path": path, "native_type": _text(f.get("nativeDataType")),
                "description": _text(ed.get("description")) or _text(f.get("description")),
                "nullable": f.get("nullable"), "labels": labels, "classification": sensitive.get(path, []),
                "exported_at": at})
            for label in labels:
                if label.startswith(("urn:li:tag:", "urn:li:glossaryTerm:")):
                    out["labels"].append({"asset_urn": urn, "field_path": path,
                                          "kind": "tag" if label.startswith("urn:li:tag:") else "term",
                                          "label_urn": label, "exported_at": at})
        for up in (ent.get("upstreamLineage") or {}).get("upstreams", []):
            if up.get("dataset"):
                out["lineage"].append({"upstream_urn": up["dataset"], "downstream_urn": urn,
                                       "lineage_type": up.get("type") or "TRANSFORMED", "exported_at": at})

    def _monitors(self, at: datetime) -> list[dict]:
        if self.quality_store is None:
            return []
        rows = []
        for m in self.quality_store.list_monitors():
            ev = m.last_evaluation
            rows.append({"monitor_id": m.id, "asset_urn": m.dataset_urn, "monitor_type": m.type, "name": m.name,
                         "status": ev.status if ev else m.status,
                         "evaluated_at": datetime.fromtimestamp(ev.ts_ms / 1000, timezone.utc) if ev else None,
                         "metric_value": ev.metric_value if ev else None, "exported_at": at})
        return rows

    def _grants(self, at: datetime) -> list[dict]:
        if self.engine is None:
            return []
        from cairn.marketplace.service import access_requests

        def ts(ms):
            return datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else None

        now_ms = int(time.time() * 1000)
        with self.engine.connect() as conn:
            rows = conn.execute(select(access_requests).where(access_requests.c.state == "APPROVED")).all()
        return [{"product_urn": r.product_urn, "grantee_urn": r.requester, "source": "marketplace",
                 "state": "ACTIVE" if not r.expires_at_ms or r.expires_at_ms > now_ms else "EXPIRED",
                 "granted_at": ts(r.updated_at_ms), "expires_at": ts(r.expires_at_ms), "exported_at": at}
                for r in rows]

    # ---- writing ------------------------------------------------------------------------
    def tables(self, data: Optional[dict[str, list[dict]]] = None):
        import pyarrow as pa

        data = self.collect() if data is None else data
        schemas = _schemas()
        return {t: pa.Table.from_pylist(data[t], schema=schemas[t]) for t in TABLES}

    def write_parquet(self, root: str, keep_days: Optional[int] = None) -> ExportResult:
        import pyarrow.fs as pafs
        import pyarrow.parquet as pq

        started = time.monotonic()
        tables = self.tables()
        day = self.now().date().isoformat()
        fs, base = pafs.FileSystem.from_uri(root if "://" in root else f"file://{_abs(root)}")
        base = base.rstrip("/")
        for name, table in tables.items():
            folder = f"{base}/{name}/export_date={day}"
            fs.create_dir(folder, recursive=True)
            pq.write_table(table, f"{folder}/{name}.parquet", filesystem=fs, compression="zstd")
            if keep_days:
                self._prune(fs, f"{base}/{name}", keep_days)
        result = ExportResult(root, day, "parquet", {t: tables[t].num_rows for t in TABLES},
                              time.monotonic() - started)
        with fs.open_output_stream(f"{base}/_manifest.json") as f:
            f.write(json.dumps({**result.as_dict(), "tables": {
                t: [{"name": fld.name, "type": str(fld.type)} for fld in tables[t].schema] for t in TABLES}},
                indent=2).encode())
        return result

    def _prune(self, fs, folder: str, keep_days: int) -> None:
        import pyarrow.fs as pafs

        cutoff = self.now().date().toordinal() - keep_days
        for info in fs.get_file_info(pafs.FileSelector(folder)):
            name = info.base_name
            if info.type == pafs.FileType.Directory and name.startswith("export_date="):
                try:
                    old = datetime.fromisoformat(name.split("=", 1)[1]).date().toordinal() < cutoff
                except ValueError:
                    continue
                if old:
                    fs.delete_dir(info.path)

    def write_iceberg(self, catalog_name: str, namespace: str = "cairn",
                      catalog_props: Optional[dict] = None) -> ExportResult:
        """Overwrite each table in an Iceberg catalog (one snapshot per run). The catalog comes from
        pyiceberg's configuration (``.pyiceberg.yaml`` or ``PYICEBERG_CATALOG__<NAME>__*``)."""
        from pyiceberg.catalog import load_catalog

        started = time.monotonic()
        tables = self.tables()
        catalog = load_catalog(catalog_name, **(catalog_props or {}))
        catalog.create_namespace_if_not_exists(namespace)
        for name, table in tables.items():
            ice = catalog.create_table_if_not_exists(f"{namespace}.{name}", schema=table.schema)
            ice.overwrite(table)
        return ExportResult(f"{catalog_name}:{namespace}", self.now().date().isoformat(), "iceberg",
                            {t: tables[t].num_rows for t in TABLES}, time.monotonic() - started)


def _abs(path: str) -> str:
    import os

    return os.path.abspath(path)
