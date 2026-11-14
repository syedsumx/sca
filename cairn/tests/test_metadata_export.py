"""Metadata export: the seven tables, their stable schema, daily partitions and Iceberg snapshots."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from cairn.export.metadata import SCHEMA_VERSION, TABLES, MetadataExporter  # noqa: E402
from cairn.marketplace.service import access_requests  # noqa: E402
from cairn.quality.models import Evaluation, MonitorSpec  # noqa: E402
from cairn.quality.store import QualityStore  # noqa: E402

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,shop.public.orders,PROD)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:postgres,app.public.orders,PROD)"
PRODUCT = "urn:li:dataProduct:orders"
ANA = "urn:li:corpuser:ana"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
CERT = "urn:li:structuredProperty:io.cairn.certification.status"
DAY = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def exporter(graph, engine):
    a = graph.aspects
    a[(ORDERS, "datasetProperties")] = {"name": "orders", "description": "One row per order."}
    a[(ORDERS, "editableDatasetProperties")] = {"description": "Orders, deduplicated."}
    a[(ORDERS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id", "nativeDataType": "NUMBER", "nullable": False},
        {"fieldPath": "email", "nativeDataType": "VARCHAR", "nullable": True}]}
    a[(ORDERS, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [
        {"fieldPath": "email", "description": "Buyer email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}]}
    a[(ORDERS, "ownership")] = {"owners": [{"owner": ANA, "type": "TECHNICAL_OWNER"}]}
    a[(ORDERS, "globalTags")] = {"tags": [{"tag": "urn:li:tag:gold"}]}
    a[(ORDERS, "domains")] = {"domains": ["urn:li:domain:sales"]}
    a[(ORDERS, "structuredProperties")] = {"properties": [{"propertyUrn": CERT, "values": [{"string": "CERTIFIED"}]}]}
    a[(ORDERS, "upstreamLineage")] = {"upstreams": [{"dataset": RAW, "type": "TRANSFORMED"}]}
    a[(RAW, "datasetProperties")] = {"name": "orders"}
    a[(PRODUCT, "dataProductProperties")] = {"name": "Orders", "assets": [{"destinationUrn": ORDERS}]}

    store = QualityStore(engine)
    m = store.create_monitor(MonitorSpec(name="orders volume", dataset_urn=ORDERS, type="volume"), None)
    store.add_evaluation(Evaluation(id="e1", monitor_id=m.id, ts="", ts_ms=int(DAY.timestamp() * 1000),
                                    run_id="r1", metric_value=1200.0, expected_low=None, expected_high=None,
                                    status="FAIL", score=None, message=""), None)
    now = int(time.time() * 1000)
    with engine.begin() as conn:
        conn.execute(access_requests.insert().values(
            id="ar1", product_urn=PRODUCT, requester=ANA, state="APPROVED", doc={}, created_at_ms=now,
            updated_at_ms=now, expires_at_ms=now + 86_400_000, version=1))
        conn.execute(access_requests.insert().values(
            id="ar2", product_urn=PRODUCT, requester="urn:li:corpuser:bob", state="PENDING", doc={},
            created_at_ms=now, updated_at_ms=now, expires_at_ms=None, version=1))
    return MetadataExporter(graph, store, engine, now=lambda: DAY)


def test_tables_hold_the_catalog_facts(exporter):
    t = {k: v.to_pylist() for k, v in exporter.tables().items()}
    assert set(t) == set(TABLES)
    orders = next(r for r in t["assets"] if r["urn"] == ORDERS)
    assert orders | {"exported_at": None} == {
        "urn": ORDERS, "entity_type": "dataset", "platform": "snowflake", "name": "orders",
        "description": "Orders, deduplicated.", "domain_urn": "urn:li:domain:sales", "certification": "CERTIFIED",
        "classification": ["PII", "PII.Email"], "data_products": [], "owner_count": 1, "exported_at": None}
    assert {r["entity_type"] for r in t["assets"]} == {"dataset", "dataProduct"}
    email = next(c for c in t["columns"] if c["field_path"] == "email")
    assert email["description"] == "Buyer email" and EMAIL in email["labels"] and email["nullable"] is True
    assert {(r["kind"], r["label_urn"], r["field_path"]) for r in t["labels"]} == {
        ("tag", "urn:li:tag:gold", None), ("term", EMAIL, "email")}
    assert t["owners"] == [{"asset_urn": ORDERS, "owner_urn": ANA, "owner_type": "TECHNICAL_OWNER",
                            "exported_at": t["owners"][0]["exported_at"]}]
    assert [(r["upstream_urn"], r["downstream_urn"]) for r in t["lineage"]] == [(RAW, ORDERS)]
    assert [(r["status"], r["metric_value"]) for r in t["monitor_results"]] == [("FAIL", 1200.0)]
    assert [(r["grantee_urn"], r["state"]) for r in t["access_grants"]] == [(ANA, "ACTIVE")]   # approved only


def test_parquet_partitions_by_day_and_reruns_replace(exporter, tmp_path):
    root = tmp_path / "export"
    first = exporter.write_parquet(str(root))
    assert first.rows["assets"] == 3 and first.export_date == "2026-11-02"
    exporter.write_parquet(str(root))                                       # same day: replaced, not doubled
    got = pq.read_table(root / "assets").to_pylist()                        # read like a warehouse would
    assert len(got) == 3 and {r["export_date"] for r in got} == {"2026-11-02"}
    manifest = json.loads((root / "_manifest.json").read_text())
    assert manifest["schema_version"] == SCHEMA_VERSION and manifest["rows"]["columns"] == 2
    assert [c["name"] for c in manifest["tables"]["owners"]] == ["asset_urn", "owner_urn", "owner_type",
                                                                 "exported_at"]


def test_old_partitions_are_pruned(exporter, tmp_path):
    root = tmp_path / "export"
    old = root / "assets" / "export_date=2026-09-01"
    old.mkdir(parents=True)
    (old / "assets.parquet").write_bytes(b"")
    exporter.write_parquet(str(root), keep_days=30)
    assert not old.exists() and (root / "assets" / "export_date=2026-11-02").exists()


def test_iceberg_keeps_one_snapshot_per_run(exporter, tmp_path):
    pytest.importorskip("pyiceberg")
    props = {"type": "sql", "uri": f"sqlite:///{tmp_path}/catalog.db", "warehouse": f"file://{tmp_path}/wh"}
    exporter.write_iceberg("test", "cairn", props)
    exporter.write_iceberg("test", "cairn", props)
    from pyiceberg.catalog import load_catalog

    t = load_catalog("test", **props).load_table("cairn.assets")
    assert t.scan().to_arrow().num_rows == 3
    assert len(t.snapshots()) >= 2                                           # time travel across runs
