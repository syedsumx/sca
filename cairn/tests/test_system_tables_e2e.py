"""System tables (L134): Cairn's operational data as Trino tables in ``cairn_system``, materialised into a Cairn
Catalog of their own on Cairn Store and read by a real Trino that asks Cairn before every action.

Activity first (queries through Trino, a refused read, a flow run, posted costs, AI usage, a warehouse's usage
report, a write that becomes lineage), then each table through Trino: an operator sees every row and the SQL; a
person sees only their own rows with the SQL masked; someone without access to the system catalog is refused;
writes are refused for everyone; rows arrive within the documented freshness; retention deletes; and each table
is in the catalog with its descriptions. Needs docker and the store and catalog binaries."""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time

import httpx
import pytest

pa = pytest.importorskip("pyarrow")

from test_trino_e2e import CATALOG_TOKEN, S3_ON, STORE_KEY, TRINO, _ready, lake, port, wait  # noqa: E402, F401

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

KEY = "system-tables-e2e-" + "k" * 24
SYS_ADMIN = "systables-admin-" + "a" * 24      # Cairn writes the system tables with it
SYS_READER = "systables-trino-" + "r" * 24     # Trino reads them with it (read-only)
INTERVAL = 3
FRESHNESS = INTERVAL + 60                       # the documented bound: the interval plus a minute
MARK = "sys-tables-marker-7731"


@pytest.fixture(scope="module")
def syscat(lake):  # noqa: F811
    """A Cairn Catalog of its own for the system tables, on the same store."""
    d = tempfile.mkdtemp(prefix="syscat-")
    cp = port()
    tokens = f"{d}/tokens.json"
    with open(tokens, "w") as fh:
        json.dump([{"token": SYS_READER, "name": "trino", "namespaces": [], "read_only": True}], fh)
    proc = subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": SYS_ADMIN,
        "CAIRN_CATALOG_TOKENS_FILE": tokens, "CAIRN_CATALOG_S3_ENDPOINT": lake["store"],
        "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0], "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1],
        "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/cairn-system", "CAIRN_CATALOG_FSYNC": "false"})
    wait(cp)
    yield f"http://127.0.0.1:{cp}"
    proc.terminate()
    proc.wait(15)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def cairn(lake, syscat, tmp_path_factory):  # noqa: F811
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    env = {"CAIRN_TRINO_ACCESS_KEY": KEY,
           "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "iceberg", "database": ""}}),
           "CAIRN_SYSTEM_TABLES_CATALOG": json.dumps({
               "uri": syscat, "token": SYS_ADMIN, "s3.endpoint": lake["store"], "s3.access-key-id": STORE_KEY[0],
               "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1", "s3.path-style-access": "true"}),
           "CAIRN_SYSTEM_TABLES_INTERVAL_SECONDS": str(INTERVAL),
           "CAIRN_COST_TRINO_USD_PER_CPU_HOUR": "5", "CAIRN_COST_TRINO_USD_PER_TB_SCANNED": "5",
           "CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR": "2",
           "CAIRN_WAREHOUSES_URL": "http://127.0.0.1:9", "CAIRN_WAREHOUSE_PORTS": json.dumps({"sales": 9})}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                            dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=InMemoryGraph(), with_ai=False, with_gateway=False)
        p = port()
        app = create_api_app(s)
    finally:
        for k, v in saved.items():
            if not k.startswith("CAIRN_COST_"):        # prices are read when costs are computed: kept below
                os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    assert s.system_tables is not None and s.trino_access.system_tables is s.system_tables
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": f"urn:li:corpuser:{u}"}  # noqa: E731
    people = lambda *us: {"attr": "subject.urn", "op": "in", "value": [f"urn:li:corpuser:{u}" for u in us]}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub("admin")),
        Policy(id="sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"]), condition=people("ana", "bob", "eve")),
        # The system tables: granted like any table, here to ana and bob (not eve).
        Policy(id="system-readers", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["urn:li:dataset:(urn:li:dataPlatform:iceberg,cairn_system.*"]),
               condition=people("ana", "bob")),
        Policy(id="no-hr", effect="deny", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*hr.*"]), condition=people("ana", "bob", "eve")),
    ]))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    wait(p)
    # The scheduler's leader: run_due every tick (the sync itself runs every INTERVAL seconds).
    stop = threading.Event()

    def tick():
        while not stop.wait(1):
            try:
                s.system_tables.run_due()
            except Exception:  # noqa: BLE001 - reported by the tests that wait on it
                pass
    ticker = threading.Thread(target=tick, daemon=True)
    s.start_ticker = ticker.start
    yield f"http://127.0.0.1:{p}", s
    stop.set()
    if ticker.is_alive():
        ticker.join(120)                              # a sync in progress finishes before the catalog goes
    server.should_exit = True
    t.join(30)
    for k, v in saved.items():
        if k.startswith("CAIRN_COST_"):
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture(scope="module")
def trino(lake, syscat, cairn):  # noqa: F811
    cairn_url, s = cairn
    d = tempfile.mkdtemp(prefix="trino-systables-")
    os.makedirs(f"{d}/catalog")
    tp = port()
    s3 = [S3_ON, f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
          f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}"]
    open(f"{d}/catalog/lake.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={lake['catalog']}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={CATALOG_TOKEN}", *s3, ""]))
    open(f"{d}/catalog/cairn_system.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={syscat}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={SYS_READER}", *s3, ""]))
    base = f"{cairn_url}/v1/trino/{KEY}"
    open(f"{d}/access-control.properties", "w").write("\n".join([
        "access-control.name=opa", f"opa.policy.uri={base}/allow", f"opa.policy.batched-uri={base}/batch",
        f"opa.policy.row-filters-uri={base}/row-filters", f"opa.policy.column-masking-uri={base}/column-mask", ""]))
    open(f"{d}/lineage-listener.properties", "w").write("\n".join([
        "event-listener.name=openlineage", "openlineage-event-listener.transport.type=HTTP",
        f"openlineage-event-listener.transport.url={cairn_url}",
        f"openlineage-event-listener.transport.endpoint=/v1/trino/{KEY}/lineage",
        f"openlineage-event-listener.trino.uri=http://127.0.0.1:{tp}", ""]))
    open(f"{d}/usage-listener.properties", "w").write("\n".join([
        "event-listener.name=http", "http-event-listener.log-completed=true",
        f"http-event-listener.connect-ingest-uri={cairn_url}/v1/trino/{KEY}/queries", ""]))
    open(f"{d}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}",
        "event-listener.config-files=/etc/trino/lineage-listener.properties,/etc/trino/usage-listener.properties", ""]))
    os.chmod(d, 0o755)
    name = f"cairn-systables-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    try:
        mounts = []
        for f in ("config.properties", "access-control.properties", "lineage-listener.properties", "usage-listener.properties"):
            mounts += ["-v", f"{d}/{f}:/etc/trino/{f}:ro"]
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                        "-v", f"{d}/catalog:/etc/trino/catalog:ro", *mounts, TRINO],
                       check=True, capture_output=True, timeout=600)
        end = time.time() + 240
        while "SERVER STARTED" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr:
            assert time.time() < end, "Trino did not start"
            time.sleep(1)

        def sql(statement, user="admin"):
            out = subprocess.run(["docker", "exec", "-e", "LANG=C.UTF-8", name, "trino", "--server", f"http://127.0.0.1:{tp}",
                                  "--user", user, "--catalog", "lake", "--output-format", "JSON", "--execute", statement],
                                 capture_output=True, text=True, timeout=300)
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip()[-800:] or out.stdout)
            return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
        yield sql
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        shutil.rmtree(d, ignore_errors=True)


def until(fn, timeout=120.0, every=1.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except RuntimeError as e:          # a table not created yet
            last = e
        time.sleep(every)
    raise AssertionError(f"timed out: {last}")


@pytest.fixture(scope="module")
def activity(trino, cairn):
    """Activity of every kind, then the first sync; returns when it was done."""
    url, s = cairn
    from cairn.ai.llm import LLMUsage
    from cairn.ai.usage import UsageMeter

    hdr = lambda u: {"X-Cairn-Actor": f"urn:li:corpuser:{u}"}  # noqa: E731
    # A query from ten days ago (for retention), recorded before the first sync.
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10 * 86400))
    s.query_history.record({"metadata": {"queryId": "20250101_old_query", "query": "SELECT 'from long ago'",
                                         "queryState": "FINISHED"}, "context": {"user": "ana", "source": "test"},
                            "createTime": old, "endTime": old, "statistics": {"cpuTime": 1.0}})
    # Tables, and a write that becomes lineage.
    trino("CREATE SCHEMA IF NOT EXISTS lake.sales")
    trino("CREATE SCHEMA IF NOT EXISTS lake.hr")
    trino("CREATE TABLE IF NOT EXISTS lake.sales.orders AS SELECT * FROM (VALUES (1, 'EU', 10.0), (2, 'US', 20.0)) "
          "AS t(id, region, amount)")
    trino("CREATE TABLE IF NOT EXISTS lake.hr.salaries AS SELECT 'jane' AS name, 100 AS salary")
    trino("CREATE TABLE lake.sales.by_region AS SELECT region, sum(amount) AS total FROM lake.sales.orders GROUP BY region")
    # People's queries, with literals in them; a refused read (a deny policy: recorded).
    t0 = time.time()
    trino(f"SELECT count(*) AS n FROM sales.orders WHERE region <> '{MARK}-ana'", user="ana")
    trino(f"SELECT count(*) AS n FROM sales.orders WHERE region <> '{MARK}-bob'", user="bob")
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM hr.salaries", user="ana")
    # A flow run, by ana.
    flow = textwrap.dedent("""
        kind: flow
        name: sys-tables-flow
        steps:
          - {name: hello, type: notify, message: "hello"}
    """)
    s.flows.create("urn:li:corpuser:ana", flow, publish=True)
    rid = s.flows.run("urn:li:corpuser:ana", "sys-tables-flow")["id"]
    assert s.flows.wait_for(rid, 60)["status"] == "succeeded"
    # Costs, posted for a table; AI usage by ana; a warehouse's usage report (as the proxy sends it).
    day = time.strftime("%Y-%m-%d", time.gmtime())
    r = httpx.post(f"{url}/reports/costs", headers=hdr("admin"), json={"rows": [
        {"urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)", "day": day, "storage_bytes": 1000,
         "storage_usd": 1.5, "compute_usd": 2.5, "reads": 3}]}, timeout=30)
    assert r.status_code == 200, r.text
    UsageMeter(s.engine).record("urn:li:corpuser:ana", "sql_assist", "openai",
                                LLMUsage(model="gpt-test", input_tokens=120, output_tokens=30))
    now_ms = int(time.time() * 1000)
    r = httpx.post(f"{url}/v1/warehouses/usage", headers={"X-Cairn-Trino-Key": KEY}, timeout=30, json={
        "id": "rep-0001", "warehouse": "sales", "start_ms": now_ms - 600_000, "end_ms": now_ms, "node_seconds": 1800.0})
    assert r.status_code == 200 and r.json()["recorded"], r.text
    s.start_ticker()
    return {"t0": t0}


def _n(trino, sql, user="admin"):
    return trino(sql, user=user)[0]["n"]


def test_rows_arrive_within_the_documented_freshness(trino, activity):
    t0 = activity["t0"]
    until(lambda: _n(trino, f"SELECT count(*) AS n FROM cairn_system.query.history WHERE statement LIKE '%{MARK}-bob%'"),
          timeout=FRESHNESS + 30)
    took = time.time() - t0
    assert took <= FRESHNESS, f"{took:.0f} s, the documented bound is {FRESHNESS} s"
    # Every table synced, and says what it is.
    for t in ("audit.events", "access.decisions", "query.history", "billing.usage", "lineage.table_lineage",
              "lineage.column_lineage", "compute.warehouse_events", "jobs.runs"):
        until(lambda t=t: _n(trino, f"SELECT count(*) AS n FROM cairn_system.{t}"), timeout=FRESHNESS)


def test_an_operator_sees_every_row_and_the_sql(trino, activity):
    rows = trino("SELECT user_name, statement, statement_hash, array_join(tables, ',') AS tables, cost_usd "
                 f"FROM cairn_system.query.history WHERE statement LIKE '%{MARK}%' AND user_name <> 'admin' ORDER BY user_name")
    assert [r["user_name"] for r in rows] == ["ana", "bob"]
    assert f"{MARK}-ana" in rows[0]["statement"] and len(rows[0]["statement_hash"]) == 64
    assert rows[0]["tables"] == "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
    assert rows[0]["cost_usd"] is not None
    # The refused read, from the audit log, and the chain's hashes with it.
    dec = trino("SELECT user_name, privilege, resource_urn, array_join(policies, ',') AS policies "
                "FROM cairn_system.access.decisions")
    assert {"user_name": "ana", "privilege": "VIEW_DATASET_PROFILE",
            "resource_urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)", "policies": "no-hr"} in dec
    ev = trino("SELECT seq, prev_hash, hash FROM cairn_system.audit.events ORDER BY seq")
    assert len(ev) >= 2 and all(b["prev_hash"] == a["hash"] for a, b in zip(ev, ev[1:]) if b["seq"] == a["seq"] + 1)
    # Costs: the posted table, the warehouse report, ana's queries and AI usage, per team.
    kinds = {r["kind"] for r in trino("SELECT DISTINCT kind FROM cairn_system.billing.usage")}
    assert {"query", "table", "run", "team", "ai"} <= kinds
    t = trino("SELECT cost_usd, storage_bytes FROM cairn_system.billing.usage WHERE kind = 'table'")
    assert t == [{"cost_usd": 4.0, "storage_bytes": 1000}]
    ai = trino("SELECT user_name, model, input_tokens, output_tokens FROM cairn_system.billing.usage WHERE kind = 'ai'")
    assert ai == [{"user_name": "ana", "model": "gpt-test", "input_tokens": 120, "output_tokens": 30}]
    wh = trino("SELECT warehouse, event, nodes, node_seconds, cost_usd FROM cairn_system.compute.warehouse_events")
    assert wh == [{"warehouse": "sales", "event": "usage", "nodes": 3.0, "node_seconds": 1800.0, "cost_usd": 1.0}]
    runs = trino("SELECT kind, name, user_name, status FROM cairn_system.jobs.runs")
    assert {"kind": "flow", "name": "sys-tables-flow", "user_name": "ana", "status": "succeeded"} in runs
    # Lineage, from the write Trino reported: table and columns.
    tl = trino("SELECT upstream_urn FROM cairn_system.lineage.table_lineage "
               "WHERE downstream_urn = 'urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.by_region,PROD)'")
    assert tl == [{"upstream_urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"}]
    cl = trino("SELECT downstream_column, upstream_column FROM cairn_system.lineage.column_lineage "
               "WHERE downstream_urn = 'urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.by_region,PROD)'")
    assert {"downstream_column": "total", "upstream_column": "amount"} in cl


def test_a_person_sees_only_their_own_rows_with_the_sql_masked(trino, activity):
    rows = trino("SELECT user_name, statement, error, statement_hash FROM cairn_system.query.history", user="ana")
    assert rows and {r["user_name"] for r in rows} == {"ana"}
    assert {r["statement"] for r in rows} == {"••••"}             # masked, even her own
    assert all(len(r["statement_hash"]) == 64 for r in rows)     # but the hash is there: the same statement matches
    # Filtering on the masked column finds nothing: the mask applies before WHERE.
    assert _n(trino, f"SELECT count(*) AS n FROM cairn_system.query.history WHERE statement LIKE '%{MARK}%'", "ana") == 0
    for t in ("billing.usage", "jobs.runs", "audit.events", "access.decisions"):
        names = {r["user_name"] for r in trino(f"SELECT DISTINCT user_name FROM cairn_system.{t}", user="ana")}
        assert names <= {"ana"}, (t, names)
    assert {r["kind"] for r in trino("SELECT DISTINCT kind FROM cairn_system.billing.usage", user="ana")} == {"query", "ai"}
    assert trino("SELECT name FROM cairn_system.jobs.runs", user="ana") == [{"name": "sys-tables-flow"}]
    # bob sees his, not hers.
    assert {r["user_name"] for r in trino("SELECT user_name FROM cairn_system.query.history", user="bob")} == {"bob"}
    assert trino("SELECT name FROM cairn_system.jobs.runs", user="bob") == []
    # The user guide's examples (docs/site/system-tables.md) run as written, on her own rows.
    top = trino("SELECT created_at, cost_usd, wall_ms, input_bytes, warehouse, statement_hash FROM cairn_system.query.history "
                "WHERE created_at >= date_trunc('week', current_timestamp) ORDER BY cost_usd DESC NULLS LAST LIMIT 10", user="ana")
    assert top and all(len(r["statement_hash"]) == 64 for r in top)
    spend = trino("SELECT day, kind, sum(cost_usd) AS usd FROM cairn_system.billing.usage WHERE day >= "
                  "date_trunc('month', current_date) GROUP BY day, kind ORDER BY day, kind", user="ana")
    assert {r["kind"] for r in spend} == {"query", "ai"}
    assert trino("SELECT fingerprint, count(*) AS runs, approx_percentile(wall_ms, 0.5) AS median_ms FROM "
                 "cairn_system.query.history WHERE created_at >= current_timestamp - INTERVAL '30' DAY GROUP BY fingerprint "
                 "ORDER BY runs DESC LIMIT 10", user="ana")
    assert trino("SELECT kind, name, started_at, error FROM cairn_system.jobs.runs WHERE status = 'failed' AND "
                 "created_at >= current_timestamp - INTERVAL '7' DAY ORDER BY started_at DESC", user="ana") == []


def test_without_access_to_the_system_catalog_refused_and_writes_refused_for_all(trino, activity):
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM cairn_system.query.history", user="eve")
    assert trino("SHOW TABLES FROM cairn_system.query", user="eve") == []
    assert {r["Table"] for r in trino("SHOW TABLES FROM cairn_system.query", user="ana")} == {"history"}
    for user in ("admin", "ana"):
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("INSERT INTO cairn_system.audit.events (seq) VALUES (1)", user=user)
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("DELETE FROM cairn_system.query.history", user=user)
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("DROP TABLE cairn_system.jobs.runs", user=user)
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("CREATE SCHEMA cairn_system.mine", user=user)


def test_the_tables_are_in_the_catalog_with_descriptions(trino, cairn, activity):
    url, s = cairn
    cols = trino("SHOW COLUMNS FROM cairn_system.query.history")
    assert {c["Column"]: c["Comment"] for c in cols}["statement"].startswith("The SQL as typed")
    assert "Queries Trino ran" in trino("SELECT comment FROM system.metadata.table_comments WHERE catalog_name = "
                                        "'cairn_system' AND schema_name = 'query'")[0]["comment"]
    s.system_tables.publish_catalog()
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,cairn_system.query.history,PROD)"
    assert "Queries Trino ran" in s.graph.get_aspect(urn, "datasetProperties")["description"]
    fields = {f["fieldPath"]: f for f in s.graph.get_aspect(urn, "schemaMetadata")["fields"]}
    assert fields["statement"]["description"].startswith("The SQL as typed")
    hdr = {"X-Cairn-Actor": "urn:li:corpuser:ana"}
    listed = httpx.get(f"{url}/v1/system-tables", headers=hdr, timeout=30).json()
    assert listed["freshness_seconds"] == FRESHNESS and len(listed["tables"]) == 8
    q = next(t for t in listed["tables"] if t["name"] == "cairn_system.query.history")
    assert q["row_filtered"] and q["masked_columns"] == ["error", "statement"] and q["written_ms"]


def test_retention_is_a_setting_and_deletes_older_rows(trino, cairn, activity):
    url, s = cairn
    old = "SELECT count(*) AS n FROM cairn_system.query.history WHERE query_id = '20250101_old_query'"
    assert until(lambda: _n(trino, old))                    # ten days old: kept under the default (90)
    hdr = lambda u: {"X-Cairn-Actor": f"urn:li:corpuser:{u}"}  # noqa: E731
    r = httpx.put(f"{url}/v1/system-tables/query/history/retention", headers=hdr("ana"), json={"days": 5}, timeout=60)
    assert r.status_code == 403                             # operators only
    r = httpx.put(f"{url}/v1/system-tables/query/history/retention", headers=hdr("admin"), json={"days": 5}, timeout=60)
    assert r.status_code == 200 and r.json()["retention_days"] == 5, r.text
    assert _n(trino, old) == 0
    # Syncs after the deletion append only what is new: nothing twice, the old row not back.
    assert httpx.post(f"{url}/v1/system-tables/sync", headers=hdr("ana"), timeout=120).status_code == 403
    r = httpx.post(f"{url}/v1/system-tables/sync", headers=hdr("admin"), timeout=120)
    assert r.status_code == 200 and all(t["ok"] for t in r.json()["tables"].values()), r.text
    assert _n(trino, old) == 0
    assert _n(trino, "SELECT count(*) - count(DISTINCT query_id) AS n FROM cairn_system.query.history") == 0
    assert _n(trino, "SELECT count(*) - count(DISTINCT seq) AS n FROM cairn_system.audit.events") == 0
    assert _n(trino, "SELECT count(*) AS n FROM cairn_system.query.history WHERE statement LIKE '%sales.orders WHERE region%' "
              "AND user_name <> 'admin'") == 2           # this week's: kept
    listed = httpx.get(f"{url}/v1/system-tables", headers=hdr("admin"), timeout=30).json()
    assert next(t for t in listed["tables"] if t["name"] == "cairn_system.query.history")["retention_days"] == 5
    assert s.audit.entries("governance", 5)[0]["action"] == "system_tables.retention"
