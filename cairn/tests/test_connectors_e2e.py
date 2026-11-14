"""Connector syncs (L165, part 2) end to end: a connector image run in a hardened container, its records landed in
governed Iceberg tables in Cairn Catalog (on Cairn Store) and read through Trino under Cairn's policies.

The connector is a real image speaking the Airbyte protocol, built here with Docker, pushed to a real registry and
pulled by digest (``tests/connectors/standin-source``); its key is in a real secret scope. It is a stand-in, not a
vendor's connector: no third-party connector image is run here. What a run refuses, its limits and its network are
in ``tests/test_connectors.py``."""

from __future__ import annotations

import base64
import json

import pytest

from connector_standin import standin_image  # noqa: F401  (fixture)
from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, lake, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, the store and catalog binaries")
OWNER, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:bob"
SECRET = "sk-live-" + "s" * 24


def catalog_props(lake):  # noqa: F811
    return {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
            "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
            "s3.path-style-access": "true"}


@pytest.fixture
def world(cairn, lake, standin_image, monkeypatch, tmp_path):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.identity.secret_scopes import SecretScopes
    from cairn.pipelines.connectors import ConnectorSyncs, DockerConnectorRuntime

    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.setenv("CAIRN_CONNECTOR_IMAGES", standin_image)
    engine = create_engine(f"sqlite:///{tmp_path}/connectors.db")
    init_db(engine)
    scopes = SecretScopes(engine, lambda a: [], lambda a: False)
    scopes.create(OWNER, {"name": "source-keys"})
    scopes.put(OWNER, "source-keys", "api_key", SECRET)
    audited, told, changed, writers = [], [], [], {OWNER}

    class Audit:
        def record(self, *a):
            audited.append(a)

    class Changes:
        def record(self, *a):
            changed.append(a)
    syncs = ConnectorSyncs(engine, catalog_props(lake), scopes.read, lambda owner, urn: owner in writers,
                           DockerConnectorRuntime(), audit=Audit(), changes=Changes(), notify=lambda *a: told.append(a))
    return {"syncs": syncs, "scopes": scopes, "audited": audited, "told": told, "changed": changed, "writers": writers,
            "image": standin_image}


def sync(image, config=None, streams=None, **over):
    return {"name": "customers", "image": image, "secret_scope": "source-keys",
            "config": {"api_key": {"secret": "api_key"}, "count": 5, "page": 2, **(config or {})},
            "streams": streams or [{"name": "customers", "target": "sales.cx_customers", "mode": "upsert"}], **over}


def test_a_connector_s_records_land_in_governed_tables_and_follow_the_source(world, trino):  # noqa: F811
    syncs, image = world["syncs"], world["image"]
    for t in ("cx_customers", "cx_regions"):
        trino(f"DROP TABLE IF EXISTS lake.sales.{t}")
    streams = [{"name": "customers", "target": "sales.cx_customers", "mode": "upsert"},
               {"name": "regions", "target": "sales.cx_regions", "mode": "replace"}]
    # (workers: the connector starts worker processes, as some do; the hardened container must let it.)
    saved = syncs.save(OWNER, sync(image, {"regions": ["EU", "US", "APAC"], "workers": 2}, streams, cron="0 * * * *"))
    assert saved["has_state"] is False and saved["last_status"] is None and saved["next_run_ms"]
    first = syncs.run_now(OWNER, "customers", wait=True)
    assert first["status"] == "ok" and first["rows"] == 8, first
    assert first["stats"]["records"] == 8 and first["stats"]["states"] == 3 and first["stats"]["logs"] == 1
    rows = trino("SELECT _key, id, name, region, seq, vip, address, tags, _synced_ms FROM lake.sales.cx_customers ORDER BY id")
    assert [r["id"] for r in rows] == ["1", "2", "3", "4", "5"] and [r["_key"] for r in rows] == [r["id"] for r in rows]
    # Top-level fields as text: objects and lists as JSON, true and false as JSON writes them.
    assert rows[0]["name"] == "Customer 1" and rows[0]["address"] == '{"city":"Lyon"}' and rows[0]["tags"] == '["a",1]'
    assert rows[2]["vip"] == "true" and rows[0]["vip"] == "false" and rows[0]["_synced_ms"] > 1_700_000_000_000
    assert sorted(r["name"] for r in trino("SELECT name FROM lake.sales.cx_regions")) == ["APAC", "EU", "US"]
    # Governed like any sales table: bob's row filter (region = 'EU') applies to what was loaded.
    assert sorted(r["name"] for r in trino("SELECT name FROM lake.sales.cx_customers", user="bob")) == \
        ["Customer 1", "Customer 3", "Customer 5"]
    assert syncs.get(OWNER, "customers")["has_state"] is True

    # The source changes: one customer renamed, one region gone. The next run reads from the saved state (only the
    # changed customer), upserts it by its key, and replaces the regions with what is there now.
    syncs.save(OWNER, sync(image, {"regions": ["EU", "US"], "changes": [[2, "Customer Two (renamed)"]]}, streams), "customers")
    second = syncs.run_now(OWNER, "customers", wait=True)
    assert second["status"] == "ok" and second["rows"] == 3 and second["stats"]["records"] == 3, second
    rows = trino("SELECT id, name FROM lake.sales.cx_customers ORDER BY id")
    assert [r["id"] for r in rows] == ["1", "2", "3", "4", "5"] and rows[1]["name"] == "Customer Two (renamed)"
    assert sorted(r["name"] for r in trino("SELECT name FROM lake.sales.cx_regions")) == ["EU", "US"]
    # Nothing new: no customer is read again; the regions are replaced with the same two.
    third = syncs.run_now(OWNER, "customers", wait=True)
    assert third["status"] == "ok" and third["rows"] == 2, third
    assert trino("SELECT count(*) AS n FROM lake.sales.cx_customers") == [{"n": 5}]
    assert trino("SELECT count(*) AS n FROM lake.sales.cx_regions") == [{"n": 2}]
    # A field the records did not have before becomes a column.
    syncs.save(OWNER, sync(image, {"regions": [], "extra_field": True, "changes": [[2, "Customer Two (renamed)"],
                                                                                   [4, "Customer 4 (moved)"]]}, streams),
               "customers")
    assert syncs.run_now(OWNER, "customers", wait=True)["rows"] == 1
    rows = trino("SELECT id, name, segment FROM lake.sales.cx_customers ORDER BY id")
    assert [(r["id"], r["segment"]) for r in rows] == [("1", None), ("2", None), ("3", None), ("4", "new"), ("5", None)]
    assert trino("SELECT count(*) AS n FROM lake.sales.cx_regions") == [{"n": 0}]       # the source has none now
    # Audited: who, which image, how much; never the key, the config or the records.
    said = json.dumps([world["audited"], world["told"]], default=str)
    assert SECRET not in said and "Customer" not in said and '"count"' not in said
    runs = [a for a in world["audited"] if a[1] == "connector.sync_run"]
    assert len(runs) == 4 and runs[0][2] == OWNER and runs[0][4]["rows"] == 8 and runs[0][4]["image"] == image
    assert world["changed"][0][0] == ["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.cx_customers,PROD)",
                                      "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.cx_regions,PROD)"]
    sync_row = syncs.get(OWNER, "customers")
    assert sync_row["last_status"] == "ok" and sync_row["last_rows"] == 1 and not sync_row["running"]
    for t in ("cx_customers", "cx_regions"):
        trino(f"DROP TABLE lake.sales.{t}")


@pytest.mark.parametrize("mode", ["upsert", "append"])
def test_a_run_that_fails_part_way_resumes_from_its_last_committed_state(world, trino, mode):  # noqa: F811
    """The connector crashes after five records, having sent a state after the second and the fourth. What was
    committed stays, with the state of the fourth; the fifth, read but not committed, is dropped and read again: the
    next run leaves no gap and no duplicate."""
    syncs, image = world["syncs"], world["image"]
    table = f"cx_resume_{mode}"
    trino(f"DROP TABLE IF EXISTS lake.sales.{table}")
    streams = [{"name": "customers", "target": f"sales.{table}", "mode": mode}]
    syncs.batch = 2                                      # commit at every state
    syncs.save(OWNER, sync(image, {"count": 7, "misbehave": {"crash_after": 5}}, streams))
    failed = syncs.run_now(OWNER, "customers", wait=True)
    assert failed["status"] == "failed" and failed["rows"] == 4 and failed["stats"]["records"] == 5, failed
    assert failed["error"] == "the connector's read exited with status 1: the source went away"
    assert [r["id"] for r in trino(f"SELECT id FROM lake.sales.{table} ORDER BY id")] == ["1", "2", "3", "4"]
    assert world["told"][-1][0] == "saas.sync_failed" and world["told"][-1][3] == [OWNER]
    kept = syncs.get(OWNER, "customers")
    assert kept["last_status"] == "failed" and kept["last_rows"] == 4 and kept["has_state"] and not kept["running"]
    # The next run resumes after the fourth: the three it had not committed, and nothing twice. (The connector
    # would crash again after five records of a run; there are only three left to read.)
    ok = syncs.run_now(OWNER, "customers", wait=True)
    assert ok["status"] == "ok" and ok["rows"] == 3 and ok["stats"]["records"] == 3, ok
    ids = [r["id"] for r in trino(f"SELECT id FROM lake.sales.{table} ORDER BY id")]
    assert ids == ["1", "2", "3", "4", "5", "6", "7"]                         # no gap, no duplicate
    nothing = syncs.run_now(OWNER, "customers", wait=True)
    assert nothing["status"] == "ok" and nothing["rows"] == 0 and nothing["stats"]["records"] == 0, nothing
    assert trino(f"SELECT count(*) AS n FROM lake.sales.{table}") == [{"n": 7}]
    cols = {r["Column"] for r in trino(f"SHOW COLUMNS FROM lake.sales.{table}")}
    assert ("_key" in cols) == (mode == "upsert") and "_synced_ms" in cols
    trino(f"DROP TABLE lake.sales.{table}")


def test_a_sync_is_checked_again_at_every_run(world, trino):  # noqa: F811
    syncs, image = world["syncs"], world["image"]
    for t in ("cx_plain", "cx_legacy"):
        trino(f"DROP TABLE IF EXISTS lake.sales.{t}")
    # A connector with the older, single state (no per-stream messages) resumes the same way.
    legacy = [{"name": "customers", "target": "sales.cx_legacy", "mode": "append"}]
    syncs.save(OWNER, sync(image, {"count": 3, "misbehave": {"legacy_state": True}}, legacy))
    assert syncs.run_now(OWNER, "customers", wait=True)["rows"] == 3
    syncs.save(OWNER, sync(image, {"count": 4, "misbehave": {"legacy_state": True}}, legacy), "customers")
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "ok" and out["rows"] == 1, out
    assert trino("SELECT count(*) AS n, count(DISTINCT id) AS d FROM lake.sales.cx_legacy") == [{"n": 4, "d": 4}]
    # The owner lost the right to write the table since: the run is refused, and nothing is read.
    world["writers"].clear()
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "failed" and "WRITE_DATASET" in out["error"] and out["stats"] == {}, out
    world["writers"].add(OWNER)
    # An upsert needs a key: one the sync names when the catalog has none; a record without it fails the run.
    regions = [{"name": "regions", "target": "sales.cx_plain", "mode": "upsert"}]
    syncs.save(OWNER, sync(image, {"regions": ["EU", "US", "EU"]}, regions), "customers")
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "failed" and "no primary key to upsert by" in out["error"], out
    regions[0]["primary_key"] = ["code"]
    syncs.save(OWNER, sync(image, {"regions": ["EU", "US", "EU"]}, regions), "customers")
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "failed" and "a record without its primary key" in out["error"], out
    regions[0]["primary_key"] = ["name"]
    syncs.save(OWNER, sync(image, {"regions": ["EU", "US", "EU"]}, regions), "customers")
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "ok" and out["rows"] == 2, out                   # EU twice in the batch: once in the table
    assert sorted(r["_key"] for r in trino("SELECT _key FROM lake.sales.cx_plain")) == ["EU", "US"]
    # A table that an append made has no key column: an upsert into it is refused rather than guessed.
    syncs.save(OWNER, sync(image, {"count": 4}, [{"name": "customers", "target": "sales.cx_legacy", "mode": "upsert"}]),
               "customers")
    out = syncs.run_now(OWNER, "customers", wait=True)
    assert out["status"] == "failed" and "was not made by an upsert" in out["error"], out
    assert trino("SELECT count(*) AS n FROM lake.sales.cx_legacy") == [{"n": 4}]
    # A stream the connector does not offer.
    syncs.save(OWNER, sync(image, {}, [{"name": "invoices", "target": "sales.cx_plain"}]), "customers")
    assert "offers 0 streams named invoices" in syncs.run_now(OWNER, "customers", wait=True)["error"]
    for t in ("cx_plain", "cx_legacy"):
        trino(f"DROP TABLE lake.sales.{t}")


def test_the_api_runs_a_sync_as_it_is_deployed(lake, trino, standin_image, monkeypatch, tmp_path):  # noqa: F811
    """The service as Cairn builds it from its environment, over HTTP: the image's spec, a sync made, run and gone."""
    from fastapi.testclient import TestClient

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"k" * 32).decode())
    monkeypatch.setenv("CAIRN_SAAS_CATALOG", json.dumps(catalog_props(lake)))
    monkeypatch.setenv("CAIRN_CONNECTOR_IMAGES", standin_image)
    monkeypatch.setenv("CAIRN_CONNECTOR_RUNTIME", "docker")
    settings = Settings(database_url=f"sqlite:///{tmp_path}/cairn.db", service_token="svc-secret",
                        dev_trust_actor_header=True, gateway={"key_dir": str(tmp_path / "keys")})
    s = build_services(settings, graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    assert s.connector_syncs is not None and s.connector_syncs.runtime is not None
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"], condition={
        "attr": "subject.urn", "op": "eq", "value": OWNER})]))
    s.secret_scopes.create(OWNER, {"name": "source-keys"})
    s.secret_scopes.put(OWNER, "source-keys", "api_key", SECRET)
    http, admin, bob = TestClient(create_api_app(s)), {"X-Cairn-Actor": OWNER}, {"X-Cairn-Actor": BOB}
    trino("DROP TABLE IF EXISTS lake.sales.cx_api")
    spec = http.post("/v1/connector-syncs/spec", json={"image": standin_image}, headers=admin)
    assert spec.status_code == 200 and "api_key" in spec.json()["spec"]["connectionSpecification"]["properties"], spec.text
    body = sync(standin_image, {"count": 3}, [{"name": "customers", "target": "sales.cx_api", "mode": "upsert"}])
    refused = http.post("/v1/connector-syncs", json=body, headers=bob)             # bob may not write sales.cx_api
    assert refused.status_code == 403 and "WRITE_DATASET" in refused.text
    assert http.post("/v1/connector-syncs", json={**body, "image": "airbyte/source-faker:latest"},
                     headers=admin).status_code == 400
    made = http.post("/v1/connector-syncs", json=body, headers=admin)
    assert made.status_code == 201 and made.json()["owner"] == OWNER, made.text
    assert http.get("/v1/connector-syncs/customers", headers=bob).status_code == 404
    assert http.post("/v1/connector-syncs/check", json={"sync": "customers"}, headers=admin).json()["status"] == "SUCCEEDED"
    ran = http.post("/v1/connector-syncs/customers/run?wait=true", headers=admin)
    assert ran.status_code == 202 and ran.json()["status"] == "ok" and ran.json()["rows"] == 3, ran.text
    assert trino("SELECT count(*) AS n FROM lake.sales.cx_api") == [{"n": 3}]
    listed = http.get("/v1/connector-syncs", headers=admin).json()
    assert [x["name"] for x in listed] == ["customers"] and listed[0]["last_rows"] == 3 and SECRET not in json.dumps(listed)
    assert http.delete("/v1/connector-syncs/customers", headers=admin).status_code == 204
    assert http.get("/v1/connector-syncs", headers=admin).json() == []
    trino("DROP TABLE lake.sales.cx_api")
