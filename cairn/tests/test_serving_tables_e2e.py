"""Serving tables, end to end: a lakehouse table written through a real Trino (on the real Cairn Store and Cairn
Catalog) is kept as a row store in a real PostgreSQL that Cairn manages, synced incrementally from the table's
snapshots (appends, then an update and a delete, then a schema change), with its freshness lag; lookups by key,
key range and filter answer in milliseconds and are governed exactly as Trino governs a read of the table (read
access, the column mask and the row filter of a second person, the same masked values); owners write rows that
are readable at once and land in the Iceberg table on the next sync. p50/p95 lookup latencies are measured.

Needs docker (Trino and PostgreSQL images) and the store and catalog binaries."""

import json
import os
import statistics
import subprocess
import time

import pytest

from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")

ACCOUNTS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.accounts,PROD)"
PII = "urn:li:glossaryTerm:Classification.PII.Email"
ANA, BOB, CAL, EVE = (f"urn:li:corpuser:{u}" for u in ("ana", "bob", "cal", "eve"))


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    """A PostgreSQL server Cairn administers; the serving database is made on it by Cairn."""
    d = tmp_path_factory.mktemp("serving-pg")
    password = "serving-" + os.urandom(8).hex()
    (d / "pg.env").write_text(f"POSTGRES_PASSWORD={password}\n")
    name, p = f"cairn-serving-pg-{os.getpid()}", port()
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--env-file", str(d / "pg.env"),
                    "-p", f"127.0.0.1:{p}:5432", "postgres:16-alpine"], check=True, capture_output=True, timeout=300)
    try:
        end = time.time() + 90
        while subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
                             capture_output=True).returncode:
            assert time.time() < end, "PostgreSQL did not start"
            time.sleep(1)
        time.sleep(1)
        admin = d / "admin-url"
        admin.write_text(f"postgresql://postgres:{password}@127.0.0.1:{p}/postgres")
        os.chmod(admin, 0o600)
        yield str(admin)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def serving(lake, cairn, trino, pg):  # noqa: F811
    from fastapi.testclient import TestClient

    from cairn.app import create_api_app
    from cairn.services import _build_serving

    _, s = cairn
    props = {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
             "s3.path-style-access": "true"}
    s.graph.aspects[(ACCOUNTS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": PII}]}}, {"fieldPath": "region"},
        {"fieldPath": "balance"}, {"fieldPath": "opened"}, {"fieldPath": "updated_at"}]}
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    anyof = lambda *us: {"any": [sub(u) for u in us]}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub("urn:li:corpuser:admin")),
        Policy(id="ana-owns", effect="allow", actions=["MANAGE_DATASET", "VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"]), condition=sub(ANA)),
        Policy(id="readers", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"]), condition=anyof(BOB, CAL)),
        Policy(id="eve-sees", effect="allow", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=["*sales.*"]),
               condition=sub(EVE)),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"], condition=anyof(BOB, CAL),
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})]),
        Policy(id="cal-eu", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*sales.*"]),
               obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})], condition=sub(CAL))]))
    trino("CREATE TABLE lake.sales.accounts (id bigint, email varchar, region varchar, balance decimal(12, 2), "
          "opened date, updated_at timestamp(6) with time zone)")
    trino("INSERT INTO lake.sales.accounts SELECT n, 'user' || CAST(n AS varchar) || '@acme.com', "
          "IF(n % 2 = 1, 'EU', 'US'), CAST(n * 10.25 AS decimal(12, 2)), date_add('day', n, DATE '2024-01-01'), "
          "TIMESTAMP '2026-11-01 12:00:00 UTC' FROM UNNEST(sequence(1, 500)) AS t(n)")
    s.lakehouse.trino_catalog = "lake"              # the Trino catalog of Cairn's lakehouse is "lake" here
    os.environ["CAIRN_SERVING_CATALOG"] = json.dumps(props)
    os.environ["CAIRN_DATABASES_ADMIN_URL_FILE"] = pg
    try:
        _build_serving(s)
    finally:
        os.environ.pop("CAIRN_SERVING_CATALOG")
        os.environ.pop("CAIRN_DATABASES_ADMIN_URL_FILE")
    assert s.serving_tables is not None
    app = create_api_app(s)
    clients = {u: TestClient(app, headers={"X-Cairn-Actor": u}) for u in (ANA, BOB, CAL, EVE)}
    yield s, clients, trino


def rows(c, *params, **kw):
    r = c.get("/v1/serving/accounts/rows", params=list(params) + list(kw.items()))
    assert r.status_code == 200, r.text
    return r.json()


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))]


def test_serving_tables_end_to_end(serving, tmp_path):
    s, c, trino = serving  # noqa: F811
    body = {"name": "accounts", "table": "sales.accounts", "primary_key": ["id"],
            "indexes": [["region"], ["email"]], "writable": True}

    # ---- declaring: owners (or operators) only; nothing half-made when it cannot be filled
    assert c[BOB].post("/v1/serving", json=body).status_code == 403
    bad = c[ANA].post("/v1/serving", json={**body, "primary_key": ["nope"]})
    assert bad.status_code == 400 and "no column nope" in bad.json()["detail"], bad.text
    assert c[ANA].get("/v1/serving/accounts").status_code == 404
    r = c[ANA].post("/v1/serving", json=body)
    assert r.status_code == 201, r.text
    st = r.json()
    assert st["rows"] == 500 and st["last_sync"]["mode"] == "full" and st["lag_ms"] == 0
    assert [x["name"] for x in st["columns"]] == ["id", "email", "region", "balance", "opened", "updated_at"]
    assert c[ANA].post("/v1/serving", json=body).status_code == 409
    assert [t["name"] for t in c[BOB].get("/v1/serving").json()["serving_tables"]] == ["accounts"]

    # ---- reads, governed as Trino governs a read of the table
    raw = rows(c[ANA], key="1")
    assert raw["rows"] == [{"id": 1, "email": "user1@acme.com", "region": "EU", "balance": "10.25",
                            "opened": "2024-01-02", "updated_at": "2026-11-01T12:00:00+00:00"}]
    assert raw["masked"] == [] and raw["row_filtered"] is False
    bob = rows(c[BOB], ("key", "1"), ("key", "2"))
    assert [r["id"] for r in bob["rows"]] == [1, 2] and bob["masked"] == ["email"] and not bob["row_filtered"]
    cal = rows(c[CAL], ("key", "1"), ("key", "2"), ("key", "3"))
    assert [r["id"] for r in cal["rows"]] == [1, 3] and cal["row_filtered"] and cal["masked"] == ["email"]
    via_trino = trino("SELECT id, email FROM sales.accounts WHERE id IN (1, 2, 3) ORDER BY id", user="cal")
    assert [(r["id"], r["email"]) for r in cal["rows"]] == [(r["id"], r["email"]) for r in via_trino]
    assert cal["rows"][0]["email"] != "user1@acme.com"
    via_trino_bob = trino("SELECT id, email FROM sales.accounts WHERE id IN (1, 2) ORDER BY id", user="bob")
    assert [r["email"] for r in bob["rows"]] == [r["email"] for r in via_trino_bob]
    # a key the row filter hides reads as missing
    look = c[CAL].post("/v1/serving/accounts/lookup", json={"keys": [{"id": 1}, {"id": 2}, {"id": "999"}],
                                                            "columns": ["balance"]}).json()
    assert [x["status"] for x in look["results"]] == ["found", "missing", "missing"]
    assert look["results"][0]["row"] == {"balance": "10.25"}
    # ranges and filters
    assert [r["id"] for r in rows(c[CAL], **{"from": "10", "to": "20"})["rows"]] == [11, 13, 15, 17, 19]
    assert [r["id"] for r in rows(c[ANA], ("from", "10"), ("to", "13"), ("order", "desc"))["rows"]] == [12, 11, 10]
    assert rows(c[CAL], filter="region:eq:US")["rows"] == []
    f = rows(c[ANA], ("filter", "region:eq:US"), ("filter", "balance:ge:5000"), ("limit", "3"), ("columns", "id,balance"))
    assert f["rows"] == [{"id": 488, "balance": "5002.00"}, {"id": 490, "balance": "5022.50"},
                         {"id": 492, "balance": "5043.00"}] and f["truncated"]
    assert [r["id"] for r in rows(c[ANA], filter="email:prefix:user49")["rows"]] == [49, *range(490, 500)]
    assert [r["id"] for r in rows(c[ANA], filter="id:in:4|8|15")["rows"]] == [4, 8, 15]
    # comparing a masked column would reveal it; unknown columns, bad values; no read access; services
    for who, params, status, msg in [
            (BOB, {"filter": "email:eq:user1@acme.com"}, 403, "masked"),
            (CAL, {"filter": "email:prefix:u"}, 403, "masked"),
            (ANA, {"filter": "nope:eq:1"}, 400, "no column nope"),
            (ANA, {"key": "x"}, 400, "not a long"),
            (ANA, {"filter": "region:like:EU"}, 400, "operator"),
            (ANA, {"columns": "id,nope"}, 400, "no column nope"),
            (EVE, {"key": "1"}, 403, "read access")]:
        r = c[who].get("/v1/serving/accounts/rows", params=params)
        assert r.status_code == status and msg in r.json()["detail"], (who, params, r.text)
    svc = c[ANA].get("/v1/serving/accounts/rows", params={"key": "1"}, headers={"Authorization": "Bearer svc-secret"})
    assert svc.status_code == 403

    # ---- fail closed: a decision that cannot be made refuses; row filters that cannot be built pass nothing
    da = s.data_access
    can_read, row_filters = da.can_read, da.row_filters
    da.can_read = lambda actor, urn: 1 / 0
    try:
        r = c[ANA].get("/v1/serving/accounts/rows", params={"key": "1"})
        assert r.status_code == 503
    finally:
        da.can_read = can_read
    da.row_filters = lambda actor, urn, dialect: 1 / 0
    try:
        assert rows(c[CAL], key="1")["rows"] == []
    finally:
        da.row_filters = row_filters
    da.row_filters = None
    try:
        assert c[ANA].get("/v1/serving/accounts/rows", params={"key": "1"}).status_code == 503
    finally:
        da.row_filters = row_filters

    # ---- latency: key lookups, measured through the API (decision, row filter, masks included)
    lat = {}
    for who in (ANA, CAL):
        times = []
        for i in range(300):
            t0 = time.perf_counter()
            r = c[who].get("/v1/serving/accounts/rows", params={"key": str(i % 500 + 1)})
            times.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200
        lat[who.rsplit(":", 1)[-1]] = {"p50_ms": round(statistics.median(times), 2), "p95_ms": round(pct(times, 95), 2)}
    times = []
    for i in range(100):
        t0 = time.perf_counter()
        c[CAL].post("/v1/serving/accounts/lookup", json={"keys": [{"id": i * 5 + j} for j in range(1, 6)]})
        times.append((time.perf_counter() - t0) * 1000)
    lat["cal_lookup_5_keys"] = {"p50_ms": round(statistics.median(times), 2), "p95_ms": round(pct(times, 95), 2)}
    print("\nserving table lookup latency:", json.dumps(lat))
    (tmp_path / "latency.json").write_text(json.dumps(lat))
    assert lat["cal"]["p95_ms"] < 250, lat

    # ---- incremental sync: new rows appended; freshness lag while behind
    assert s.serving_tables.run_due() == 0                               # nothing moved: nothing read
    trino("INSERT INTO lake.sales.accounts VALUES (501, 'new501@acme.com', 'EU', 1.00, DATE '2026-11-02', "
          "TIMESTAMP '2026-11-02 08:00:00 UTC'), (502, 'new502@acme.com', 'US', 2.00, DATE '2026-11-02', "
          "TIMESTAMP '2026-11-02 08:00:00 UTC'), (503, 'new503@acme.com', 'EU', 3.00, DATE '2026-11-02', "
          "TIMESTAMP '2026-11-02 08:00:00 UTC')")
    time.sleep(0.2)
    st = c[ANA].get("/v1/serving/accounts").json()
    assert st["behind"] and st["lag_ms"] >= 200 and st["behind_snapshots"] == 1, st
    assert st["source_snapshot"] != st["synced_snapshot"]
    lag_before = st["lag_ms"]
    assert rows(c[ANA], key="501")["rows"] == []
    assert s.serving_tables.run_due() == 1
    st = c[ANA].get("/v1/serving/accounts").json()
    assert st["last_sync"]["mode"] == "append" and st["last_sync"]["written"] == 3, st
    assert st["rows"] == 503 and st["lag_ms"] == 0 and not st["behind"]
    assert st["source_snapshot"] == st["synced_snapshot"]
    assert rows(c[CAL], ("key", "501"), ("key", "502"))["rows"][0]["id"] == 501
    print("freshness lag before the sync:", lag_before, "ms; sync took", st["last_sync"]["duration_ms"], "ms")

    # ---- an update and a delete: rewritten files, only the changed rows written
    trino("UPDATE lake.sales.accounts SET balance = 12345.67 WHERE id = 5")
    trino("DELETE FROM lake.sales.accounts WHERE id = 7")
    out = c[ANA].post("/v1/serving/accounts/sync").json()
    assert out["mode"] == "changed" and out["written"] == 1 and out["removed"] == 1 and out["rows"] == 502, out
    assert rows(c[ANA], key="5")["rows"][0]["balance"] == "12345.67"
    assert rows(c[ANA], key="7")["rows"] == []

    # ---- a schema change: a full resync into a new row store
    trino("ALTER TABLE lake.sales.accounts ADD COLUMN tier varchar")
    assert s.serving_tables.run_due() == 1, c[ANA].get("/v1/serving/accounts").json()
    st = c[ANA].get("/v1/serving/accounts").json()
    assert st["last_sync"]["mode"] == "full" and st["rows"] == 502 and st["columns"][-1] == {"name": "tier", "type": "string"}
    assert rows(c[ANA], key="1")["rows"][0]["tier"] is None

    # ---- writes (hybrid): owners only; readable at once, governed; in the Iceberg table after the next sync
    new = {"rows": [{"id": 9001, "email": "fresh@acme.com", "region": "EU", "balance": "10.5", "tier": "gold",
                     "updated_at": "2026-11-08T09:00:00+01:00"}, {"id": 3, "balance": 999.99}]}
    assert c[BOB].post("/v1/serving/accounts/rows", json=new).status_code == 403
    bad = c[ANA].post("/v1/serving/accounts/rows", json={"rows": [{"email": "x"}]})
    assert bad.status_code == 400 and "key" in bad.json()["detail"]
    w = c[ANA].post("/v1/serving/accounts/rows", json=new)
    assert w.status_code == 200 and w.json() == {"serving_table": "accounts", "written": 2, "pending_writes": 2}, w.text
    got = rows(c[ANA], ("key", "3"), ("key", "9001"))["rows"]
    assert got[0]["balance"] == "999.99" and got[0]["email"] == "user3@acme.com"           # merged into the row
    assert got[1] == {"id": 9001, "email": "fresh@acme.com", "region": "EU", "balance": "10.50", "opened": None,
                      "updated_at": "2026-11-08T08:00:00+00:00", "tier": "gold"}
    assert rows(c[CAL], key="9001")["rows"][0]["email"] != "fresh@acme.com"                # masked for Cal
    assert trino("SELECT count(*) AS n FROM sales.accounts WHERE id = 9001") == [{"n": 0}]
    assert c[ANA].get("/v1/serving/accounts").json()["pending_writes"] == 2
    assert s.serving_tables.run_due() == 1
    assert trino("SELECT id, tier FROM sales.accounts WHERE id = 9001") == [{"id": 9001, "tier": "gold"}]
    assert trino("SELECT CAST(balance AS varchar) AS b FROM sales.accounts WHERE id = 3") == [{"b": "999.99"}]
    st = c[ANA].get("/v1/serving/accounts").json()
    assert st["pending_writes"] == 0 and st["rows"] == 503 and st["lag_ms"] == 0
    assert st["last_sync"]["written"] == 0, st                          # the row store already had them
    assert s.serving_tables.run_due() == 0
    actions = {e["action"] for e in s.audit.entries("data", limit=50)}
    assert {"serving.created", "serving.rows_written", "serving.written_back", "serving.synced"} <= actions

    # ---- dropping
    assert c[BOB].delete("/v1/serving/accounts").status_code == 403
    assert c[ANA].delete("/v1/serving/accounts").status_code == 204
    assert c[ANA].get("/v1/serving/accounts").status_code == 404
    write, _ = s.serving_tables._engines()
    from sqlalchemy import text
    with write.connect() as conn:
        left = conn.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'cairn_serving' "
                                 "AND table_name LIKE 'st_accounts%'")).scalar()
    assert left == 0
