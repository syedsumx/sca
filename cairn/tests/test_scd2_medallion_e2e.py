"""SCD type 2 derived tables and medallion layers (L110), on a real Trino with Cairn Catalog and Cairn Store and the Cairn
API. Checked: an scd2 table keeps every version of each key with valid_from / valid_to / is_current; a refresh closes
changed and removed keys and inserts new versions in one MERGE, writes untracked changes in place, and changes nothing
when run again; a full rebuild is refused once there is history. Layers set with a derived table (and a stream) are
kept and tagged in the catalog; gold is refused until the table has an owner, a description and a met contract, and
the listing shows when a gold table stops qualifying. Needs docker and the store and catalog binaries."""

import httpx
import pytest

from test_trino_e2e import _ready, cairn, lake, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

ADMIN = "urn:li:corpuser:admin"


@pytest.fixture(scope="module")
def world(trino, cairn):  # noqa: F811
    from cairn.pipelines.derived import DerivedTables
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]

    def run(owner, sql):
        return s.data_access.run_on_trino(definition, owner, sql, 100_000)[1]
    for stmt in ["CREATE SCHEMA IF NOT EXISTS lake.dim",
                 "CREATE TABLE lake.dim.crm_customers (id integer, name varchar, tier varchar, email varchar)",
                 "INSERT INTO lake.dim.crm_customers VALUES (1, 'Ana', 'basic', 'ana@a.io'), (2, 'Bo', 'gold', 'bo@b.io'), "
                 "(3, 'Cy', 'basic', 'cy@c.io')"]:
        run(ADMIN, stmt)
    dt = DerivedTables(s.engine, run, "lake", s.audit, urn=s.medallion_urn)
    dt.medallion = s.medallion
    return {"dt": dt, "run": run, "s": s, "url": url}


def versions(trino):  # noqa: F811
    rows = trino("SELECT id, name, tier, email, valid_from, valid_to, is_current FROM lake.dim.customers ORDER BY id, valid_from")
    return [(r["id"], r["name"], r["tier"], r["email"], r["valid_to"] is not None, r["is_current"]) for r in rows], rows


def test_scd2_keeps_every_version_and_is_idempotent(world, trino):  # noqa: F811
    from cairn.pipelines.derived import DerivedError

    dt, run = world["dt"], world["run"]
    with pytest.raises(DerivedError, match="needs the key"):
        dt.create({"name": "x", "target": "dim.x", "mode": "scd2", "every_minutes": 60, "sql": "SELECT 1 AS id"}, ADMIN, ADMIN)
    with pytest.raises(DerivedError, match="history's own"):
        dt.create({"name": "x", "target": "dim.x", "mode": "scd2", "key": ["valid_from"], "every_minutes": 60,
                   "sql": "SELECT 1 AS id"}, ADMIN, ADMIN)
    with pytest.raises(DerivedError, match="tracked columns are for scd2"):
        dt.create({"name": "x", "target": "dim.x", "mode": "merge", "key": ["id"], "tracked": ["name"],
                   "every_minutes": 60, "sql": "SELECT 1 AS id"}, ADMIN, ADMIN)
    d = dt.create({"name": "customers", "target": "dim.customers", "mode": "scd2", "key": ["id"],
                   "tracked": ["name", "tier"], "every_minutes": 60, "layer": "silver",
                   "sql": "SELECT id, name, tier, email FROM lake.dim.crm_customers"}, ADMIN, ADMIN)
    assert d["mode"] == "scd2" and d["tracked"] == ["name", "tier"]
    assert dt.refresh(d["id"])["kind"] == "create"
    got, _ = versions(trino)
    assert got == [(1, "Ana", "basic", "ana@a.io", False, True), (2, "Bo", "gold", "bo@b.io", False, True),
                   (3, "Cy", "basic", "cy@c.io", False, True)]

    # A tracked change (1), an untracked change (2), a key removed (3) and a new key (4).
    run(ADMIN, "UPDATE lake.dim.crm_customers SET tier = 'gold' WHERE id = 1")
    run(ADMIN, "UPDATE lake.dim.crm_customers SET email = 'bo@new.io' WHERE id = 2")
    run(ADMIN, "DELETE FROM lake.dim.crm_customers WHERE id = 3")
    run(ADMIN, "INSERT INTO lake.dim.crm_customers VALUES (4, 'Di', 'basic', 'di@d.io')")
    r = dt.refresh(d["id"])
    assert (r["kind"], r["status"]) == ("scd2", "ok"), r
    got, rows = versions(trino)
    assert got == [(1, "Ana", "basic", "ana@a.io", True, False),        # closed
                   (1, "Ana", "gold", "ana@a.io", False, True),         # its new current version
                   (2, "Bo", "gold", "bo@new.io", False, True),         # untracked: in place, no new version
                   (3, "Cy", "basic", "cy@c.io", True, False),          # gone from the source: closed
                   (4, "Di", "basic", "di@d.io", False, True)]          # new
    old, new = rows[0], rows[1]
    assert old["valid_to"] == new["valid_from"]                         # one MERGE: the versions meet exactly

    # The same refresh again changes nothing; so does a run after it.
    for _ in range(2):
        assert dt.refresh(d["id"])["status"] == "ok"
        assert versions(trino)[1] == rows
    # A key that comes back starts a new version.
    run(ADMIN, "INSERT INTO lake.dim.crm_customers VALUES (3, 'Cy', 'gold', 'cy@c.io')")
    dt.refresh(d["id"])
    got, _ = versions(trino)
    assert [g for g in got if g[0] == 3] == [(3, "Cy", "basic", "cy@c.io", True, False), (3, "Cy", "gold", "cy@c.io", False, True)]
    # The history is the table: a full rebuild is refused (the run says why) and the rows stay.
    r = dt.refresh(d["id"], full=True)
    assert r["status"] == "error" and "keeps its history" in r["error"]
    assert len(versions(trino)[0]) == 6
    # Changing what is tracked keeps the history (no rebuild).
    dt.update(d["id"], {"name": "customers", "target": "dim.customers", "mode": "scd2", "key": ["id"],
                        "tracked": ["tier"], "every_minutes": 60, "sql": "SELECT id, name, tier, email FROM lake.dim.crm_customers"},
              ADMIN)
    run(ADMIN, "UPDATE lake.dim.crm_customers SET name = 'Ana Lima' WHERE id = 1")
    assert dt.refresh(d["id"])["kind"] == "scd2"
    got, _ = versions(trino)
    assert len(got) == 6 and (1, "Ana Lima", "gold", "ana@a.io", False, True) in got


def test_layers_are_tagged_and_gold_is_earned(world):
    from cairn.common.proposals import make_mcp
    from cairn.pipelines.derived import DerivedError
    from cairn.quality.contracts import ContractSpec

    s, dt, url = world["s"], world["dt"], world["url"]
    urn = s.medallion_urn("dim", "customers")
    admin_h, ana_h = {"X-Cairn-Actor": ADMIN}, {"X-Cairn-Actor": "urn:li:corpuser:ana"}
    d = next(x for x in dt.list() if x["name"] == "customers")
    assert d["layer"] == "silver"
    tags = [t["tag"] for t in (s.graph.get_aspect(urn, "globalTags") or {}).get("tags", [])]
    assert tags == ["urn:li:tag:medallion.silver"]                     # shown in the catalog

    # Gold is earned: an owner, a description and a met contract (or passing monitors).
    r = httpx.put(f"{url}/v1/medallion/dim.customers", json={"layer": "gold"}, headers=admin_h, timeout=30)
    assert r.status_code == 409, r.text
    for why in ("no owner", "no description", "neither a data contract nor a quality monitor"):
        assert why in r.json()["detail"]
    with pytest.raises(DerivedError, match="not gold") as e:          # nor through the derived table's definition
        dt.update(d["id"], {**{k: d[k] for k in ("name", "target", "mode", "key", "tracked", "every_minutes", "sql")},
                            "layer": "gold"}, ADMIN)
    assert e.value.status == 409
    assert httpx.put(f"{url}/v1/medallion/dim.customers", json={"layer": "gold"}, headers=ana_h,
                     timeout=30).status_code == 403                    # marking is for those who manage ingestion
    s.graph.emit([make_mcp(urn, "ownership", {"owners": [{"owner": "urn:li:corpGroup:crm-team", "type": "DATAOWNER"}]}),
                  make_mcp(urn, "editableDatasetProperties", {"description": "Customers with their full history."}),
                  make_mcp(urn, "schemaMetadata", {"fields": [{"fieldPath": "id", "nativeDataType": "integer"},
                                                              {"fieldPath": "tier", "nativeDataType": "varchar"}]})])
    r = httpx.put(f"{url}/v1/medallion/dim.customers", json={"layer": "gold"}, headers=admin_h, timeout=30)
    assert r.status_code == 409 and "no owner" not in r.json()["detail"] and "contract" in r.json()["detail"]
    s.contracts.upsert(ContractSpec.model_validate({"dataset": urn, "version": "1.0.0", "schema": {
        "columns": [{"name": "id", "type": "integer"}, {"name": "tier", "type": "string"}]}}), ADMIN)
    assert s.contracts.get(urn)["status"] == "MET"
    r = httpx.put(f"{url}/v1/medallion/dim.customers", json={"layer": "gold"}, headers=admin_h, timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()["layer"] == "gold" and r.json()["tag"] == "urn:li:tag:medallion.gold"
    assert [t["tag"] for t in s.graph.get_aspect(urn, "globalTags")["tags"]] == ["urn:li:tag:medallion.gold"]
    listed = httpx.get(f"{url}/v1/medallion", headers=ana_h, timeout=30).json()
    mine = next(x for x in listed if x["table"] == "dim.customers")
    assert mine["gold_ready"] is True and mine["resource"] == "derived:customers"

    # The contract breaks: the table stays gold, and the listing says it no longer qualifies.
    s.graph.emit([make_mcp(urn, "schemaMetadata", {"fields": [{"fieldPath": "id", "nativeDataType": "integer"}]})])
    assert s.contracts.refresh(urn)["status"] == "BROKEN"
    mine = next(x for x in httpx.get(f"{url}/v1/medallion?layer=gold", headers=ana_h, timeout=30).json()
                if x["table"] == "dim.customers")
    assert mine["gold_ready"] is False and "BROKEN" in " ".join(mine["problems"])
    assert httpx.put(f"{url}/v1/medallion/dim.customers", json={"layer": "platinum"}, headers=admin_h,
                     timeout=30).status_code == 400
    entries = [e for e in s.audit.entries("lakehouse", 200) if e["action"] == "medallion.set" and e["subject"] == urn]
    assert [e["details"]["layer"] for e in entries][:2] == ["gold", "silver"]


def test_streams_and_pipes_take_a_layer(world, lake, tmp_path):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.medallion import Medallion, MedallionError
    from cairn.pipelines.streams import Streams
    from test_trino_e2e import CATALOG_TOKEN, STORE_KEY

    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    st = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                              "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                              "s3.region": "us-east-1", "s3.path-style-access": "true"})
    with pytest.raises(MedallionError, match="not available"):
        st.create({"name": "raw", "format": "http", "target": "raw.events", "layer": "bronze"}, ADMIN)
    st.medallion = Medallion(engine, world["s"].graph)
    with pytest.raises(MedallionError, match="layer is one of"):
        st.create({"name": "raw", "format": "http", "target": "raw.events", "layer": "tin"}, ADMIN)
    with pytest.raises(MedallionError, match="not gold"):
        st.create({"name": "raw", "format": "http", "target": "raw.events", "layer": "gold"}, ADMIN)
    assert st.list() == []                                              # refused before anything was written
    st.create({"name": "raw", "format": "http", "target": "raw.events", "create_table": True, "layer": "bronze"}, ADMIN)
    assert st.list()[0]["layer"] == "bronze"
    st.ingest("raw", "c1", [{"id": 1}])
    assert st.medallion.list()[0]["table"] == "raw.events"
