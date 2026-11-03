"""Feature tables: lakehouse tables declared as features for machine learning, as catalog assets.

``POST /v1/ml/feature-tables`` with ``name``, ``table`` (``namespace.table``), ``keys`` (the entity's key
columns) and optionally ``features`` (default: every other column) and ``description``. Declaring one needs
``MANAGE_DATASET`` on the table: its owners decide what it offers as features.

Cairn publishes the feature table, one feature per column and the primary keys, each with the source
table as its lineage, and the declaring person as owner. A model trained on the table (MLflow, through
Cairn) links to the same table, so the catalog shows table → features → model. Reading feature values is
reading the table: through Trino, Spark or pyiceberg as yourself, under the table's policies, masks and
row filters. ``GET /v1/ml/feature-tables`` lists those whose source table the caller may see.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PLATFORM = "urn:li:dataPlatform:iceberg"
TYPES = {"string": "TEXT", "varchar": "TEXT", "char": "TEXT", "int": "ORDINAL", "integer": "ORDINAL", "bigint": "ORDINAL",
         "long": "ORDINAL", "double": "CONTINUOUS", "float": "CONTINUOUS", "decimal": "CONTINUOUS", "boolean": "BINARY",
         "date": "TIME", "timestamp": "TIME"}


class FeatureError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def table_urn(name: str) -> str:
    return f"urn:li:mlFeatureTable:({PLATFORM},{name})"


def feature_urn(table: str, col: str) -> str:
    return f"urn:li:mlFeature:({table},{col})"


def key_urn(table: str, col: str) -> str:
    return f"urn:li:mlPrimaryKey:({table},{col})"


def _kind(native: str) -> str:
    base = re.split(r"[(<\s]", (native or "").lower(), maxsplit=1)[0]
    return TYPES.get(base, "UNKNOWN")


class FeatureTables:
    def __init__(self, graph, dataset_urn: Callable[[list[str], str], str], may: Callable[[Any, str, str], bool], audit=None):
        """``dataset_urn(namespace, table)``: the lakehouse table's dataset; ``may(principal, privilege, urn)``."""
        self.graph, self.dataset_urn, self.may, self.audit = graph, dataset_urn, may, audit

    def register(self, p, body: dict) -> dict:
        from cairn.common.proposals import make_mcp

        name, table = str(body.get("name") or ""), str(body.get("table") or "")
        keys, features = body.get("keys") or [], body.get("features")
        if not NAME.match(name):
            raise FeatureError("name: lowercase letters, digits and _")
        if table.count(".") < 1:
            raise FeatureError("table: namespace.table")
        ns, _, t = table.rpartition(".")
        ds = self.dataset_urn(ns.split("."), t)
        if not self.may(p, "MANAGE_DATASET", ds):
            raise FeatureError(f"declaring features of {table} needs MANAGE_DATASET on it", 403)
        fields = {f.get("fieldPath"): f for f in ((self.graph.get_aspect(ds, "schemaMetadata") or {}).get("fields") or [])}
        if not fields:
            raise FeatureError(f"{table} is not in the catalog (or has no columns yet)", 404)
        if not isinstance(keys, list) or not keys:
            raise FeatureError("keys: the entity's key columns")
        if features is None:
            features = [c for c in fields if c not in keys]
        unknown = [c for c in [*keys, *features] if c not in fields]
        if unknown:
            raise FeatureError(f"{table} has no column {unknown[0]}")
        if set(keys) & set(features):
            raise FeatureError("a key column is not also a feature")
        now = int(time.time() * 1000)
        owner = {"owners": [{"owner": p.urn, "type": "TECHNICAL_OWNER"}], "lastModified": {"time": now, "actor": p.urn}}
        mcps = [make_mcp(table_urn(name), "mlFeatureTableProperties", {
            "description": body.get("description") or None, "mlFeatures": [feature_urn(name, c) for c in features],
            "mlPrimaryKeys": [key_urn(name, c) for c in keys], "customProperties": {"source": ds, "table": table}}),
            make_mcp(table_urn(name), "ownership", owner), make_mcp(table_urn(name), "status", {"removed": False})]
        for c in features:
            mcps += [make_mcp(feature_urn(name, c), "mlFeatureProperties", {
                "description": fields[c].get("description") or None, "dataType": _kind(fields[c].get("nativeDataType")),
                "sources": [ds]}), make_mcp(feature_urn(name, c), "status", {"removed": False})]
        for c in keys:
            mcps += [make_mcp(key_urn(name, c), "mlPrimaryKeyProperties", {
                "description": fields[c].get("description") or None, "dataType": _kind(fields[c].get("nativeDataType")),
                "sources": [ds]}), make_mcp(key_urn(name, c), "status", {"removed": False})]
        self.graph.emit(mcps)
        if self.audit is not None:
            self.audit.record("ml", "ml.feature_table_registered", p.urn, table_urn(name),
                              {"table": table, "keys": keys, "features": features})
        return self.get(p, name)

    def get(self, p, name: str) -> dict:
        props = self.graph.get_aspect(table_urn(name), "mlFeatureTableProperties")
        if not props:
            raise FeatureError("no such feature table", 404)
        source = (props.get("customProperties") or {}).get("source")
        if source and not self.may(p, "VIEW_ENTITY_PAGE", source):
            raise FeatureError("no such feature table", 404)
        return {"name": name, "urn": table_urn(name), "table": (props.get("customProperties") or {}).get("table"),
                "source": source, "keys": [k.rsplit(",", 1)[1].rstrip(")") for k in props.get("mlPrimaryKeys") or []],
                "features": [f.rsplit(",", 1)[1].rstrip(")") for f in props.get("mlFeatures") or []],
                "description": props.get("description")}

    def list(self, p) -> list[dict]:
        out = []
        for row in self.graph.scroll_entities("mlFeatureTable", ["mlFeatureTableProperties"]):
            name = row["urn"].rsplit(",", 1)[1].rstrip(")")
            try:
                out.append(self.get(p, name))
            except FeatureError:
                continue
        return out


def build_features_router(ft: FeatureTables, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/ml/feature-tables", tags=["ml"])

    def wrap(fn):
        try:
            return fn()
        except FeatureError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_(p=Depends(current_principal)):
        return ft.list(p)

    @router.post("", status_code=201)
    def register(body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: ft.register(p, body))

    @router.get("/{name}")
    def get(name: str, p=Depends(current_principal)):
        return wrap(lambda: ft.get(p, name))

    return router
