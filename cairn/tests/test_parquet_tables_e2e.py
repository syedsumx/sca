"""Zero-copy Parquet (L115), on the real Cairn Store, Cairn Catalog and Trino: Parquet files written to the store
under Hive-style partition directories are registered as an Iceberg table without copying them (the table's
manifests reference the same object keys, and nothing is written under the table's location but metadata), the
schema is inferred and checked across files, partition values come from the paths, files outside the allowed
buckets or that the person may not read are refused, lineage runs from the files, and the table is then queried
through Trino as a governed user (a masked column). More files are added incrementally, on demand and on the
schedule. Needs docker and the store and catalog binaries."""

import json
import os
import threading

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from test_ai_search_e2e import run_trino  # noqa: E402
from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port, wait  # noqa: E402, F401

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

KEY = "parquet-e2e-" + "k" * 24
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
EVENTS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,files.events,PROD)"
SCHEMA = pa.schema([("id", pa.int64()), ("email", pa.string()), ("amount", pa.float64())])


def fs(lake):  # noqa: F811
    from pyarrow import fs as pafs

    return pafs.S3FileSystem(access_key=STORE_KEY[0], secret_key=STORE_KEY[1], region="us-east-1",
                             endpoint_override=lake["store"].split("://", 1)[1], scheme="http")


def put(lake, key, rows, schema=SCHEMA):  # noqa: F811
    with fs(lake).open_output_stream(f"lakehouse/{key}") as out:
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), out)


def objects(lake, prefix):  # noqa: F811
    from pyarrow import fs as pafs

    infos = fs(lake).get_file_info(pafs.FileSelector(f"lakehouse/{prefix}", recursive=True, allow_not_found=True))
    return sorted(i.path.split("/", 1)[1] for i in infos if i.type == pafs.FileType.File)


@pytest.fixture(scope="module")
def cairn(lake, tmp_path_factory):  # noqa: F811
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    catalog = {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
               "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
               "s3.path-style-access": "true"}
    env = {"CAIRN_TRINO_ACCESS_KEY": KEY,
           "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "iceberg", "database": ""}}),
           "CAIRN_LAKEHOUSE_TRINO_CATALOG": "lake",
           "CAIRN_PIPES_CATALOG": json.dumps(catalog),
           "CAIRN_PIPES_STORE": json.dumps({"endpoint": lake["store"], "access_key": STORE_KEY[0],
                                            "secret_key": STORE_KEY[1], "region": "us-east-1"}),
           "CAIRN_PARQUET_BUCKETS": "lakehouse"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        graph = InMemoryGraph()
        graph.aspects[(EVENTS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}},
            {"fieldPath": "amount"}, {"fieldPath": "region"}]}
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                            dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    finally:
        for k, v in saved.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    assert s.parquet_tables is not None
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": f"urn:li:corpuser:{u}"}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub("admin")),
        Policy(id="ingest", effect="allow", actions=["MANAGE_INGESTION"],
               condition={"attr": "subject.urn", "op": "in", "value": ["urn:li:corpuser:carol", "urn:li:corpuser:dave"]}),
        Policy(id="landing", effect="allow", actions=["READ_FILES"], target=Target(resource_urns=["urn:li:storagePath:lakehouse/raw/*"]),
               condition=sub("carol")),
        Policy(id="files", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*files.*"])),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})],
               condition={"attr": "subject.urn", "op": "neq", "value": "urn:li:corpuser:admin"}),
    ]))
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="0.0.0.0", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    wait(p)
    yield f"http://127.0.0.1:{p}", s
    server.should_exit = True
    t.join(30)


@pytest.fixture(scope="module")
def trino(lake, cairn):  # noqa: F811
    yield from run_trino(lake, cairn[0], KEY, tag="parquet")


def api(cairn, method, path, user, body=None):
    import httpx

    return httpx.request(method, f"{cairn[0]}{path}", json=body, headers={"X-Cairn-Actor": f"urn:li:corpuser:{user}"},
                         timeout=120)


def test_parquet_files_are_registered_in_place_and_queried_governed(lake, cairn, trino):  # noqa: F811
    _, s = cairn
    put(lake, "raw/events/region=EU/part-0.parquet", [{"id": 1, "email": "jane@acme.com", "amount": 10.5},
                                                      {"id": 2, "email": "li@acme.com", "amount": 7.0}])
    put(lake, "raw/events/region=US/part-0.parquet", [{"id": 3, "email": "bo@example.org", "amount": 3.25}])
    with fs(lake).open_output_stream("lakehouse/raw/events/_SUCCESS") as out:     # a marker: skipped
        out.write(b"")
    body = {"name": "events", "bucket": "lakehouse", "prefix": "raw/events/", "target": "files.events",
            "every_minutes": 60}
    # Who may: MANAGE_INGESTION and read access to the files; only from the allowed buckets.
    assert api(cairn, "POST", "/v1/lakehouse/parquet-tables", "ana", body).status_code == 403
    r = api(cairn, "POST", "/v1/lakehouse/parquet-tables", "dave", body)
    assert r.status_code == 403 and "may not read lakehouse/raw/events/" in r.json()["detail"]
    r = api(cairn, "POST", "/v1/lakehouse/parquet-tables", "carol", {**body, "bucket": "trino-exchange"})
    assert r.status_code == 403 and "CAIRN_PARQUET_BUCKETS" in r.json()["detail"]
    # Files that disagree on their schema are refused, naming the one that differs.
    put(lake, "raw/mixed/a.parquet", [{"id": 1, "email": "x@y.z", "amount": 1.0}])
    put(lake, "raw/mixed/b.parquet", [{"id": "one"}], pa.schema([("id", pa.string())]))
    r = api(cairn, "POST", "/v1/lakehouse/parquet-tables", "carol", {**body, "name": "mixed", "prefix": "raw/mixed/",
                                                                     "target": "files.mixed"})
    assert r.status_code == 400 and "raw/mixed/b.parquet has (id string)" in r.json()["detail"]

    before = objects(lake, "")
    r = api(cairn, "POST", "/v1/lakehouse/parquet-tables", "carol", body)
    assert r.status_code == 201, r.text
    got = r.json()
    assert (got["files"], got["rows"], got["added"]) == (2, 3, 2)
    assert got["partition_columns"] == [["region", "string"]] and got["created_by"] == "urn:li:corpuser:carol"
    assert got["file_schema"] == [["id", "int64"], ["email", "string"], ["amount", "double"]]
    # Zero copy: the table references the very objects written above, and the store holds nothing new but metadata.
    table = s.parquet_tables._catalog().load_table(("files", "events"))
    assert sorted(table.inspect.files().column("file_path").to_pylist()) == [
        "s3://lakehouse/raw/events/region=EU/part-0.parquet", "s3://lakehouse/raw/events/region=US/part-0.parquet"]
    new_objects = sorted(set(objects(lake, "")) - set(before))
    assert new_objects and all("/metadata/" in k for k in new_objects), new_objects
    assert table.properties["gc.enabled"] == "false" and "schema.name-mapping.default" in table.properties
    assert [f.name for f in table.spec().fields] == ["region"]

    # Queried through Trino: the partition values come from the paths; the email column is masked for ana.
    rows = trino("SELECT region, count(*) AS n, sum(amount) AS total FROM lake.files.events GROUP BY region ORDER BY region",
                 "admin")
    assert rows == [{"region": "EU", "n": 2, "total": 17.5}, {"region": "US", "n": 1, "total": 3.25}]
    raw = {r["id"]: r["email"] for r in trino("SELECT id, email FROM lake.files.events", "admin")}
    masked = {r["id"]: r["email"] for r in trino("SELECT id, email FROM lake.files.events", "ana")}
    assert raw[1] == "jane@acme.com" and masked[1] != "jane@acme.com" and set(masked) == {1, 2, 3}
    assert trino("SELECT count(*) AS n FROM lake.files.events WHERE region = 'US'", "ana") == [{"n": 1}]

    # Lineage from the files: the store prefix is the table's upstream.
    ups = s.graph.get_aspect(EVENTS, "upstreamLineage")["upstreams"]
    assert [u["dataset"] for u in ups] == ["urn:li:dataset:(urn:li:dataPlatform:s3,lakehouse/raw/events,PROD)"]
    # Registered once: the same name or table again is refused.
    assert api(cairn, "POST", "/v1/lakehouse/parquet-tables", "carol", body).status_code == 409
    assert api(cairn, "POST", "/v1/lakehouse/parquet-tables", "carol", {**body, "name": "again"}).status_code == 409


def test_new_files_are_added_incrementally(lake, cairn, trino):  # noqa: F811
    _, s = cairn
    # A file whose schema differs from the table's is refused, and nothing is added.
    put(lake, "raw/events/region=US/bad.parquet", [{"id": 9}], pa.schema([("id", pa.int64())]))
    r = api(cairn, "POST", "/v1/lakehouse/parquet-tables/events/refresh", "carol")
    assert r.status_code == 400 and "raw/events/region=US/bad.parquet has (id int64)" in r.json()["detail"]
    assert api(cairn, "GET", "/v1/lakehouse/parquet-tables/events", "carol").json()["error"].startswith("the files disagree")
    fs(lake).delete_file("lakehouse/raw/events/region=US/bad.parquet")
    # New files, one in a new partition: added by reference, the old ones left as they are.
    put(lake, "raw/events/region=APAC/part-0.parquet", [{"id": 4, "email": "ken@acme.jp", "amount": 12.0}])
    put(lake, "raw/events/region=EU/part-1.parquet", [{"id": 5, "email": "eva@acme.com", "amount": 1.5}])
    r = api(cairn, "POST", "/v1/lakehouse/parquet-tables/events/refresh", "carol")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "events", "added": 2, "rows_added": 2, "files": 4, "rows": 5, "problems": []}
    assert api(cairn, "POST", "/v1/lakehouse/parquet-tables/events/refresh", "carol").json()["added"] == 0
    assert trino("SELECT region, count(*) AS n FROM lake.files.events GROUP BY region ORDER BY region", "admin") == [
        {"region": "APAC", "n": 1}, {"region": "EU", "n": 3}, {"region": "US", "n": 1}]
    paths = s.parquet_tables._catalog().load_table(("files", "events")).inspect.files().column("file_path").to_pylist()
    assert sorted(paths) == sorted(f"s3://lakehouse/{k}" for k in objects(lake, "raw/events/") if k.endswith(".parquet"))
    # On the schedule: a due registration picks up a new file by itself.
    put(lake, "raw/events/region=US/part-1.parquet", [{"id": 6, "email": "al@example.org", "amount": 2.0}])
    from sqlalchemy import update

    from cairn.pipelines.parquet_tables import parquet_tables

    with s.engine.begin() as conn:
        conn.execute(update(parquet_tables).where(parquet_tables.c.name == "events").values(registered_ms=1))
    assert s.parquet_tables.run_due() == 1
    assert trino("SELECT count(*) AS n FROM lake.files.events", "ana") == [{"n": 6}]
    # A file that disappears is reported, not hidden.
    fs(lake).delete_file("lakehouse/raw/events/region=APAC/part-0.parquet")
    out = api(cairn, "POST", "/v1/lakehouse/parquet-tables/events/refresh", "carol").json()
    assert out["added"] == 0 and out["problems"] == ["raw/events/region=APAC/part-0.parquet is gone from the store but "
                                                     "still in the table"]
    # Listed, changed and removed (the table and its files stay).
    listed = api(cairn, "GET", "/v1/lakehouse/parquet-tables", "carol").json()
    assert [(x["name"], x["files"], x["rows"], len(x["problems"])) for x in listed] == [("events", 5, 6, 1)]
    assert api(cairn, "PUT", "/v1/lakehouse/parquet-tables/events", "carol", {"every_minutes": 15}).json()["every_minutes"] == 15
    assert api(cairn, "PUT", "/v1/lakehouse/parquet-tables/events", "carol", {"prefix": "x/"}).status_code == 400
    assert api(cairn, "DELETE", "/v1/lakehouse/parquet-tables/events", "carol").status_code == 204
    assert api(cairn, "GET", "/v1/lakehouse/parquet-tables", "carol").json() == []
    assert s.parquet_tables._catalog().load_table(("files", "events")) is not None
    assert [e["action"] for e in s.audit.entries("lakehouse", 50) if e["action"].startswith("parquet_table.")][:2] == [
        "parquet_table.deleted", "parquet_table.updated"]
