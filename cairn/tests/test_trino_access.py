"""Trino access control through Cairn's policy engine (the OPA plugin's protocol): who may see, read,
change and manage which tables; masks from the caller's masking plan; row filters from row_filter
obligations; groups; system operations; the shared key; unknown operations refused."""

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.dataaccess.trino import mask_expression
from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.services import build_services

KEY = "trino-key-" + "k" * 24
OAUTH_SECRET = "oauth-secret-" + "o" * 24
AIRFLOW_SECRET = "airflow-secret-" + "a" * 24
ADMIN, ANA, BOB, EVE = (f"urn:li:corpuser:{u}" for u in ("admin", "ana", "bob", "eve"))
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
SECRET = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"


def schema(*cols, labelled=()):
    return {"fields": [{"fieldPath": c, **({"glossaryTerms": {"terms": [{"urn": EMAIL}]}} if c in labelled else {})}
                       for c in cols]}


@pytest.fixture
def client(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", KEY)
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""}, '
                                               '"delta": {"platform": "delta-lake", "database": ""}}')
    monkeypatch.setenv("CAIRN_TRINO_OAUTH_SECRET", OAUTH_SECRET)
    monkeypatch.setenv("CAIRN_TRINO_OAUTH_REDIRECT_URIS", "https://trino.test/oauth2/callback")
    monkeypatch.setenv("CAIRN_AIRFLOW_OAUTH_SECRET", AIRFLOW_SECRET)
    monkeypatch.setenv("CAIRN_AIRFLOW_OAUTH_REDIRECT_URIS", "https://airflow.test/auth/oauth-authorized/cairn")
    graph.aspects[(ORDERS, "schemaMetadata")] = schema("id", "email", "region", "amount", labelled=("email",))
    graph.aspects[(SECRET, "schemaMetadata")] = schema("name", "salary")
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="see-sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"])),
        Policy(id="analysts-write", effect="allow", actions=["WRITE_DATASET"], target=Target(resource_urns=["*sales.*"]),
               condition={"attr": "subject.groups", "op": "contains", "value": "urn:li:corpGroup:analysts"}),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})],
               condition={"attr": "subject.urn", "op": "neq", "value": ADMIN}),
        Policy(id="eu-only", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*sales.*"]),
               obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU' OR owner = {user}"})],
               condition=sub(BOB)),
        Policy(id="no-eve", effect="deny", actions=["*"], condition=sub(EVE)),
    ]))
    c = TestClient(create_api_app(s))
    c.services = s
    return c


def ask(c, path, user, op, resource=None, groups=(), **action):
    body = {"input": {"context": {"identity": {"user": user, "groups": list(groups)}, "softwareStack": {"trinoVersion": "476"}},
                      "action": {"operation": op, **({"resource": resource} if resource else {}), **action}}}
    r = c.post(f"/v1/trino/{KEY}/{path}", json=body)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def table(t, schema_="sales", **kw):
    return {"table": {"catalogName": "lakehouse", "schemaName": schema_, "tableName": t, **kw}}


def test_reads_writes_and_management(client):
    c = client
    assert ask(c, "allow", "ana", "ExecuteQuery") is True
    assert ask(c, "allow", "ana", "SelectFromColumns", table("orders", columns=["id"])) is True
    assert ask(c, "allow", "ana", "SelectFromColumns", table("salaries", "hr", columns=["salary"])) is False
    # Writes need WRITE_DATASET: analysts have it on sales, ana alone does not.
    assert ask(c, "allow", "ana", "InsertIntoTable", table("orders")) is False
    assert ask(c, "allow", "ana", "InsertIntoTable", table("orders"), groups=["analysts"]) is True
    assert ask(c, "allow", "ana", "DropTable", table("orders"), groups=["analysts"]) is False  # MANAGE_DATASET
    assert ask(c, "allow", "admin", "DropTable", table("orders")) is True
    assert ask(c, "allow", "ana", "CreateSchema", {"schema": {"catalogName": "lakehouse", "schemaName": "x"}}) is False
    assert ask(c, "allow", "admin", "CreateSchema", {"schema": {"catalogName": "lakehouse", "schemaName": "x"}}) is True
    # A rename needs the right on both names.
    assert ask(c, "allow", "admin", "RenameTable", table("orders"), targetResource=table("orders2")) is True
    # An explicit deny wins over everything; unknown operations are refused.
    assert ask(c, "allow", "eve", "SelectFromColumns", table("orders", columns=["id"])) is False
    assert ask(c, "allow", "admin", "SomeFutureOperation", table("orders")) is False
    # Trino's metadata tables are read freely (their rows are filtered table by table), never changed.
    assert ask(c, "allow", "ana", "SelectFromColumns", table("tables", "information_schema", columns=["table_name"])) is True
    assert ask(c, "allow", "ana", "DeleteFromTable", table("tables", "information_schema")) is False


def test_a_vector_indexs_table_is_read_on_its_sources_terms(monkeypatch, request):
    """``sales.orders__docs_vectors`` holds chunks of ``sales.orders``'s text, unmasked. The namespace's grant
    (``*sales.*``) covered it and no mask names its columns: ana, whose reads of orders are masked, read its text raw.
    Only someone who reads the source unmasked reads it now; other tables are decided as before."""
    monkeypatch.setenv("CAIRN_PIPES_CATALOG", '{"uri": "http://127.0.0.1:9"}')
    c = request.getfixturevalue("client")
    c.services.vectors.create({"name": "docs", "source": "sales.orders", "key": "id", "text": "email"}, ADMIN)
    vec = table("orders__docs_vectors", columns=["text", "embedding"])
    assert ask(c, "allow", "ana", "SelectFromColumns", table("orders", columns=["email"])) is True     # masked
    assert ask(c, "allow", "ana", "SelectFromColumns", vec) is False
    assert ask(c, "allow", "ana", "SelectFromColumns", table("orders__docs_vectors$files", columns=["x"])) is False
    assert ask(c, "allow", "admin", "SelectFromColumns", vec) is True
    assert ask(c, "allow", "ana", "SelectFromColumns", table("other__x_vectors", columns=["text"])) is True  # no index's
    assert c.services.lakehouse.decide("ana", [{"namespace": ["sales"], "name": "orders__docs_vectors"}])[
        "tables"][0]["restricted"] is True
    # An erasure from orders takes the person's chunks out of the index too (cairn.governance.records).
    assert c.services.records.copies(ORDERS) == [{
        "urn": "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders__docs_vectors,PROD)", "key": "key",
        "source_key": "id", "what": "vector index docs"}]
    assert c.services.records.copies(SECRET) == []


def test_a_legal_hold_stops_removing_rows_for_everyone_and_fails_closed(client):
    c, s = client, client.services
    hold = s.records.place(ADMIN, {"dataset": ORDERS, "case": "C-1", "reason": "litigation"})
    for op in ("DeleteFromTable", "TruncateTable", "UpdateTableColumns", "DropTable", "ExecuteTableProcedure",
               "DropColumn", "SetTableProperties"):
        assert ask(c, "allow", "admin", op, table("orders")) is False, op
    assert ask(c, "allow", "admin", "RenameTable", table("orders"), targetResource=table("orders2")) is False
    # CREATE OR REPLACE TABLE on the held table's name is checked as CreateTable only, and replaces its rows.
    assert ask(c, "allow", "admin", "CreateTable", table("orders")) is False
    assert ask(c, "allow", "admin", "CreateTable", table("orders_copy")) is True
    assert ask(c, "allow", "admin", "InsertIntoTable", table("orders")) is True          # adding rows is fine
    assert ask(c, "allow", "admin", "SelectFromColumns", table("orders", columns=["id"])) is True
    assert ask(c, "allow", "admin", "DeleteFromTable", table("salaries", "hr")) is True   # another table: no hold
    s.records.release(ADMIN, hold["id"], "settled")
    assert ask(c, "allow", "admin", "DeleteFromTable", table("orders")) is True
    # A hold that cannot be checked holds.
    def broken(urn):
        raise RuntimeError("database away")
    s.trino_access.held = broken
    assert ask(c, "allow", "admin", "DeleteFromTable", table("orders")) is False
    assert ask(c, "allow", "admin", "InsertIntoTable", table("orders")) is True


def test_listings_are_filtered(client):
    c = client
    tables = [table("orders"), table("salaries", "hr"), table("returns")]
    assert ask(c, "batch", "ana", "FilterTables", filterResources=tables) == [0, 2]
    assert ask(c, "batch", "admin", "FilterTables", filterResources=tables) == [0, 1, 2]
    cols = [table("orders", columns=["id", "email", "region"])]
    assert ask(c, "batch", "ana", "FilterColumns", filterResources=cols) == [0, 1, 2]
    assert ask(c, "batch", "ana", "FilterColumns", filterResources=[table("salaries", "hr", columns=["a"])]) == []
    assert ask(c, "batch", "ana", "FilterCatalogs", filterResources=[{"catalog": {"name": "lakehouse"}}]) == [0]
    # Other people's queries: only with MANAGE_SYSTEM_OPERATIONS.
    owners = [{"user": {"user": "ana"}}, {"user": {"user": "bob"}}]
    assert ask(c, "batch", "ana", "FilterViewQueryOwnedBy", filterResources=owners) == [0]
    assert ask(c, "batch", "admin", "FilterViewQueryOwnedBy", filterResources=owners) == [0, 1]
    assert ask(c, "allow", "ana", "KillQueryOwnedBy", {"user": {"user": "bob"}}) is False
    assert ask(c, "allow", "ana", "ImpersonateUser", {"user": {"user": "ana"}}) is True
    assert ask(c, "allow", "ana", "ImpersonateUser", {"user": {"user": "bob"}}) is False


def test_masks_and_row_filters(client):
    c = client
    col = lambda name, t="varchar": {"column": {"catalogName": "lakehouse", "schemaName": "sales", "tableName": "orders",  # noqa: E731
                                                "columnName": name, "columnType": t}}
    m = ask(c, "column-mask", "ana", "GetColumnMask", col("email"))
    assert m == {"expression": mask_expression("email", "varchar", "partial")}
    assert ask(c, "column-mask", "ana", "GetColumnMask", col("region")) is None
    assert ask(c, "column-mask", "admin", "GetColumnMask", col("email")) is None
    assert ask(c, "row-filters", "bob", "GetRowFilters", table("orders")) == [{"expression": "region = 'EU' OR owner = 'bob'"}]
    assert ask(c, "row-filters", "ana", "GetRowFilters", table("orders")) == []


def test_the_key_is_required(client):
    body = {"input": {"context": {"identity": {"user": "admin"}}, "action": {"operation": "ExecuteQuery"}}}
    assert client.post("/v1/trino/wrong-key/allow", json=body).status_code == 404
    assert client.post(f"/v1/trino/{KEY}/allow", json={"nope": 1}).status_code == 400
    no_user = {"input": {"context": {}, "action": {"operation": "ExecuteQuery"}}}
    assert client.post(f"/v1/trino/{KEY}/allow", json=no_user).status_code == 400


def test_mask_expressions_keep_types():
    """Every mask is cast to the column's type (Trino requires it); the matrix runs on a real Trino in
    test_trino_e2e.py::test_masks_keep_every_type."""
    assert mask_expression("salary", "decimal(10,2)", "partial").endswith("AS decimal(10,2)) END")
    assert "truncate(" in mask_expression("salary", "decimal(10,2)", "partial")
    assert "date_trunc('month'" in mask_expression("born", "date", "partial")
    assert mask_expression("e", "varchar", "null") == "CAST(NULL AS varchar)"
    assert mask_expression("flag", "boolean", "partial") == "CAST(NULL AS boolean)"
    assert mask_expression("tags", "array(varchar)", "hash") == "CAST(NULL AS array(varchar))"
    assert "sha256" in mask_expression("e", "varchar(255)", "hash") and "'sha256:'" not in mask_expression("e", "char(3)", "hash")
    assert "'••••'" in mask_expression('we"ird', "varchar", "redact") and '"we""ird"' in mask_expression('we"ird', "varchar", "redact")


def test_geo_mask_on_text_falls_back_on_the_same_column():
    """A geo-masked text column that holds no shape gets the partial mask of that column, quoted the same way:
    a name with quotes at its ends ("x" or x") never turns into another column's name."""
    import re

    for name in ('"loc"', 'loc"', '"loc'):
        expr = mask_expression(name, "varchar", "geo")
        quoted = '"' + name.replace('"', '""') + '"'
        others = set(re.findall(r'"(?:[^"]|"")*"', expr.replace(quoted, ""))) - {'"'}
        assert quoted in expr and not others, (name, others)


def test_hash_mask_fits_a_decimal_with_no_integer_digits():
    """decimal(p,p) holds values below 1: the hash mask must give one (a whole number would not cast, failing every
    query that reads the column); decimal(p,s) with integer digits keeps a whole number below 10^(p-s)."""
    expr = mask_expression("rate", "decimal(5,5)", "hash")
    assert "mod(" in expr and "/ 1e5" in expr and expr.endswith("AS decimal(5,5)) END")
    assert mask_expression("rate", "decimal(10,2)", "hash").count("100000000)") == 1
    deep = mask_expression("r", "decimal(38,38)", "hash")
    assert "/ 1e15" in deep and "AS decimal(38,38)) END" in deep


def test_lineage_jobs_are_named_by_what_they_write(client):
    ev = {"eventType": "COMPLETE", "eventTime": "2026-11-01T00:00:00Z", "run": {"runId": "q1"},
          "job": {"namespace": "trino://trino:8080", "name": "20261101_000000_00001_abcde"},
          "inputs": [{"namespace": "trino://trino:8080", "name": "lake.sales.orders"}],
          "outputs": [{"namespace": "trino://trino:8080", "name": "lake.sales.daily"}]}
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 200 and r.json()["results"][0]["job"].endswith("trino:lake.sales.daily)")
    assert client.post("/v1/trino/wrong-key/lineage", json=ev).status_code == 404
    assert client.post(f"/v1/trino/{KEY}/lineage", json={"job": {}}).status_code == 422


def _out(**facets):
    return {"namespace": "trino://t:8080", "name": "lake.s.t", **({"facets": facets} if facets else {})}


@pytest.mark.parametrize("bad", [
    {"outputs": [_out(columnLineage={"fields": {"c": {"inputFields": [{"namespace": "trino://t:8080", "field": "x"}]}}})],
     "inputs": []},
    {"outputs": [_out(columnLineage={"fields": {"c": {"inputFields": "lake.s.u"}}})]},
    {"outputs": [_out(columnLineage={"fields": {"c": "x"}})]},
    {"outputs": [_out(schema={"fields": ["id"]})]},
    {"outputs": [_out()], "run": {"runId": "r", "facets": {"parent": "p"}}},
    {"outputs": [_out()], "run": "r"},
    {"outputs": [_out()], "job": {"namespace": "trino://t:8080", "name": "q", "facets": {"sql": "SELECT 1"}}},
    {"outputs": [{"namespace": "trino://t:8080", "name": 7}]},
    {"outputs": [_out()], "eventTime": 1700000000},
])
def test_malformed_trino_lineage_is_refused_not_held(client, bad):
    """An event of the wrong shape is refused (422), not taken for Cairn Core being down: held, it would be retried for a
    week and, being the oldest, stop every round of the outbox before the lineage behind it."""
    ev = {"eventType": "COMPLETE", "job": {"namespace": "trino://t:8080", "name": "q"}, "run": {"runId": "r"}, **bad}
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 422, r.text
    assert client.app.state.services.trino_outbox.pending()["pending"] == 0


@pytest.mark.parametrize("host", ["airflow-cairn-trino:8080", "dbt-platform-trino:8080"])
def test_trino_lineage_stays_a_trino_job_whatever_the_host_is_called(client, host):
    """Trino's job namespace is its address (``trino://<release>-trino:8080``): a release or host whose name contains
    an orchestrator's (airflow, dbt, spark...) must not file Trino's jobs under that orchestrator (and, for Airflow,
    split the job name into a DAG and a task), and an event naming another orchestrator's namespace stays a Trino job."""
    ev = {"eventType": "COMPLETE", "eventTime": "2026-11-01T00:00:00Z", "run": {"runId": "q1"},
          "job": {"namespace": f"trino://{host}", "name": "20261101_000000_00001_abcde"},
          "outputs": [{"namespace": f"trino://{host}", "name": "lake.sales.daily"}]}
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 200
    assert r.json()["results"][0]["job"] == f"urn:li:dataJob:(urn:li:dataFlow:(trino,{host},PROD),trino:lake.sales.daily)"
    ev["job"]["namespace"] = "airflow://scheduler"
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.json()["results"][0]["job"].startswith("urn:li:dataJob:(urn:li:dataFlow:(trino,")


def test_lineage_is_held_while_the_metadata_store_is_down(client, graph, monkeypatch):
    """Trino sends each event once: what Core cannot take now waits in Cairn's database and is written
    when Core is back."""
    real = graph.emit

    def down(_mcps):
        raise OSError("Name or service not known")

    monkeypatch.setattr(graph, "emit", down)
    ev = {"eventType": "COMPLETE", "job": {"namespace": "trino://t:8080", "name": "q"}, "run": {"runId": "r"},
          "outputs": [{"namespace": "trino://t:8080", "name": "lake.s.t"}]}
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 202 and r.json()["events"] == 1
    box = client.app.state.services.trino_outbox
    assert box.pending()["pending"] == 1
    job = "urn:li:dataJob:(urn:li:dataFlow:(trino,t:8080,PROD),trino:lake.s.t)"
    # Not due yet; then due but Core still down: retried later, with the error kept.
    import time

    assert box.drain()["written"] == 0
    later = int(time.time() * 1000) + 20_000
    assert box.drain(now=later) == {"written": 0, "retrying": 1, "dropped": 0}
    assert box.drain(now=later + 1000)["retrying"] == 0  # backing off
    assert box.pending()["pending"] == 1
    # Core is back: written once, and gone from the outbox.
    monkeypatch.setattr(graph, "emit", real)
    assert box.drain(now=later + 3600_000) == {"written": 1, "retrying": 0, "dropped": 0}
    assert graph.get_aspect(job, "dataJobInputOutput")["outputDatasets"] == [
        "urn:li:dataset:(urn:li:dataPlatform:trino,lake.s.t,PROD)"]
    assert box.pending()["pending"] == 0


def test_held_lineage_is_dropped_and_audited_when_it_cannot_be_written(client, graph, monkeypatch):
    from cairn.sources.openlineage import OpenLineageError

    s = client.app.state.services
    box = s.trino_outbox
    box.hold([{"job": {"name": "trino:lake.a.b"}}], "down")
    import time

    soon = int(time.time() * 1000) + 60_000
    monkeypatch.setattr(box, "write", lambda _e: (_ for _ in ()).throw(OpenLineageError("not valid")))
    assert box.drain(now=soon)["dropped"] == 1
    # Too old: dropped rather than retried forever.
    box.hold([{"job": {"name": "trino:lake.c.d"}}], "down")
    monkeypatch.setattr(box, "write", lambda _e: (_ for _ in ()).throw(OSError("still down")))
    assert box.drain(now=soon + box.max_age_ms * 2)["dropped"] == 1
    reasons = [e["details"]["reason"] for e in s.audit.entries(category="lineage") if e["action"] == "trino.lineage_dropped"]
    assert any(r.startswith("rejected") for r in reasons) and any(r.startswith("expired") for r in reasons)


def test_one_replica_writes_each_held_batch(client):
    from cairn.dataaccess.trino_lineage import LineageOutbox

    s = client.app.state.services
    wrote = []
    a = LineageOutbox(s.engine, lambda e: wrote.append(("a", e)))
    b = LineageOutbox(s.engine, lambda e: wrote.append(("b", e)))
    a.hold([{"n": 1}], "down")
    now = 10**13
    claimed_by_a = a._claim(now)
    assert claimed_by_a is not None and b._claim(now) is None  # b sees it claimed


def test_held_lineage_stops_at_the_first_failure_and_goes_oldest_first(client):
    """Core still down: one attempt per round, not one per held batch (each can wait out Core's timeout, and a
    long round outlives the claims, so another replica would write the same batches). The others stay due, not
    claimed. Core back: the oldest batch is written first, whatever its backoff left in next_ms."""
    from sqlalchemy import update

    from cairn.dataaccess.trino_lineage import LineageOutbox, trino_lineage_outbox as t

    s = client.app.state.services
    tried, down = [], [True]

    def write(events):
        tried.append(events[0]["n"])
        if down[0]:
            raise OSError("timed out")

    box = LineageOutbox(s.engine, write)
    ids = [box.hold([{"n": n}], "down") for n in (1, 2, 3)]
    now = 10**13
    with s.engine.begin() as conn:  # the oldest has backed off longest: the newest comes due first
        for rid, (received, nxt) in zip(ids, [(now - 3000, now - 10), (now - 2000, now - 20), (now - 1000, now - 30)]):
            conn.execute(update(t).where(t.c.id == rid).values(received_ms=received, next_ms=nxt))
    assert box.drain(now=now) == {"written": 0, "retrying": 1, "dropped": 0}
    assert tried == [1]
    other = LineageOutbox(s.engine, write)  # another replica may take the two not tried at once
    down[0] = False
    assert other.drain(now=now + 1) == {"written": 2, "retrying": 0, "dropped": 0}
    assert tried == [1, 2, 3]


def test_lineage_is_refused_when_it_cannot_be_held_either(client, graph, monkeypatch):
    monkeypatch.setattr(graph, "emit", lambda _m: (_ for _ in ()).throw(OSError("down")))
    box = client.app.state.services.trino_outbox
    monkeypatch.setattr(box, "hold", lambda *_a: (_ for _ in ()).throw(OSError("database down")))
    ev = {"eventType": "COMPLETE", "job": {"namespace": "trino://t:8080", "name": "q"}, "run": {"runId": "r"},
          "outputs": [{"namespace": "trino://t:8080", "name": "lake.s.t"}]}
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 503 and "metadata store" in r.json()["detail"]


def test_the_outbox_is_bounded(client, graph, monkeypatch):
    """A long outage must not fill Cairn's database: past CAIRN_TRINO_LINEAGE_MAX_HELD batches, new lineage is
    refused (503, counted) rather than held."""
    from cairn.common import metrics

    box = client.app.state.services.trino_outbox
    assert box.max_held == 10_000  # the default
    monkeypatch.setattr(box, "max_held", 2)
    monkeypatch.setattr(graph, "emit", lambda _m: (_ for _ in ()).throw(OSError("down")))
    ev = {"eventType": "COMPLETE", "job": {"namespace": "trino://t:8080", "name": "q"}, "run": {"runId": "r"},
          "outputs": [{"namespace": "trino://t:8080", "name": "lake.s.t"}]}
    assert [client.post(f"/v1/trino/{KEY}/lineage", json=ev).status_code for _ in range(2)] == [202, 202]
    before = metrics.TRINO_LINEAGE_REFUSED._value.get() if metrics.ENABLED else 0
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 503 and box.pending()["pending"] == 2
    if metrics.ENABLED:
        assert metrics.TRINO_LINEAGE_REFUSED._value.get() == before + 1


def test_sign_in_tokens_verify_against_the_published_key(client):
    import jwt

    keys = client.get("/v1/trino/jwks.json").json()["keys"]  # public: Trino fetches it without credentials
    assert len(keys) == 1 and keys[0]["alg"] == "RS256" and "d" not in keys[0]
    r = client.post("/v1/trino/token", headers={"X-Cairn-Actor": ANA}, json={"ttl_seconds": 600})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["user"] == "ana" and out["expires_in"] == 600
    pub = jwt.algorithms.RSAAlgorithm.from_jwk(keys[0])
    claims = jwt.decode(out["token"], pub, algorithms=["RS256"], audience="trino", issuer="cairn")
    assert claims["sub"] == "ana" and claims["exp"] - claims["iat"] == 600
    assert jwt.get_unverified_header(out["token"])["kid"] == keys[0]["kid"]
    # Issuing is audited.
    entries = client.app.state.services.audit.entries(category="trino")
    assert any(e["action"] == "trino.token_issued" and e["actor"] == ANA for e in entries)


def test_sign_in_tokens_are_for_people(client):
    assert client.post("/v1/trino/token").status_code == 401
    svc = client.post("/v1/trino/token", headers={"Authorization": "Bearer svc-secret"})
    assert svc.status_code == 403
    assert client.post("/v1/trino/token", headers={"X-Cairn-Actor": ANA}, json={"ttl_seconds": 5}).status_code == 422
    assert client.post("/v1/trino/token", headers={"X-Cairn-Actor": ANA}, json={"ttl_seconds": 86400 * 2}).status_code == 422


def test_no_person_gets_a_token_as_a_trino_service_identity(client):
    """Trino gives Cairn's own identities rights no person has (the crawler sees every table, the catalog
    administrator creates catalogs, a governed connection's service user makes the metric views): a person who
    happens to have one of those names gets no Trino token as it, from /v1/trino/token or Trino's sign-in."""
    from cairn.quality.collectors import ConnectionRegistry

    s = client.app.state.services
    s.data_access.registry = ConnectionRegistry({
        "lake": {"url": "trino://trino.test:8443/lakehouse/sales?protocol=https", "run_as_user": True},
        "lake2": {"url": "trino://trino.test:8443/lake2?protocol=https", "run_as_user": True,
                  "service_user": "urn:li:corpuser:svc-metrics"}})
    for name in ("cairn-catalog", "cairn-catalogs", "cairn-quality", "svc-metrics"):
        urn = f"urn:li:corpuser:{name}"
        assert client.post("/v1/trino/token", headers={"X-Cairn-Actor": urn}).status_code == 403, name
        r, q = _code(client, actor=urn)
        assert r.status_code == 302 and "code" not in q, name
    # A connection's service user stops being one once no connection names it.
    s.data_access.registry = ConnectionRegistry({})
    assert client.post("/v1/trino/token", headers={"X-Cairn-Actor": "urn:li:corpuser:svc-metrics"}).status_code == 200
    # When the connections cannot be read, no token is issued (any name might be one).
    s.trino_access.service_identities = lambda: (_ for _ in ()).throw(RuntimeError("registry unreadable"))
    assert client.post("/v1/trino/token", headers={"X-Cairn-Actor": ANA}).status_code == 503
    r, q = _code(client)
    assert r.status_code == 302 and q.get("error") == "temporarily_unavailable" and "code" not in q


def test_no_person_runs_sql_on_trino_as_a_trino_service_identity(client, monkeypatch):
    """The SQL editor, explain and preview run a run_as_user connection's queries as the caller: a person who happens
    to have the name of one of Cairn's own Trino identities is refused there too (as for tokens), not run as it."""
    from cairn.dataaccess.service import DataAccessError
    from cairn.quality.collectors import ConnectionRegistry

    s = client.app.state.services
    s.data_access.registry = ConnectionRegistry({
        "lake": {"url": "trino://trino.test:8443/lakehouse?protocol=https", "run_as_user": True,
                 "catalog": {"platform": "trino", "database": "lakehouse"}},
        "lake2": {"url": "trino://trino.test:8443/lake2?protocol=https", "run_as_user": True,
                  "service_user": "urn:li:corpuser:svc-metrics"}})
    ran = []
    monkeypatch.setattr(s.data_access, "run_on_trino", lambda d, actor, sql, limit: ran.append(actor) or (["n"], [[1]], False))
    monkeypatch.setattr(s.data_access, "known", lambda urn: True)
    monkeypatch.setattr(s.data_access, "can_read", lambda actor, urn: True)
    sql = "SELECT id FROM sales.orders"
    for name in ("cairn-catalog", "cairn-catalogs", "cairn-quality", "svc-metrics"):
        urn = f"urn:li:corpuser:{name}"
        r = client.post("/data/query", headers={"X-Cairn-Actor": urn}, json={"sql": sql, "connection": "lake"})
        assert r.status_code == 403 and "Cairn's own" in r.json()["detail"], (name, r.text)
        with pytest.raises(DataAccessError) as e:
            s.data_access.explain(urn, sql, "lake")
        assert e.value.status == 403
        with pytest.raises(DataAccessError) as e:
            s.data_access.preview(urn, "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.orders,PROD)")
        assert e.value.status == 403
    assert ran == []
    assert client.post("/data/query", headers={"X-Cairn-Actor": ANA}, json={"sql": sql, "connection": "lake"}).status_code == 200
    assert ran == [ANA]
    # When Cairn cannot tell its own identities, nothing runs.
    s.trino_access.service_identities = lambda: (_ for _ in ()).throw(RuntimeError("registry unreadable"))
    r = client.post("/data/query", headers={"X-Cairn-Actor": ANA}, json={"sql": sql, "connection": "lake"})
    assert r.status_code == 503 and ran == [ANA]


class _FakeTrino:
    """A Trino coordinator speaking the client protocol, for what the real client does with its answers."""

    def __init__(self, answer):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.answer, self.deleted = answer, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, doc):
                import json

                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send(*fake.answer(self, "POST"))

            def do_GET(self):
                self._send(*fake.answer(self, "GET"))

            def do_DELETE(self):
                fake.deleted.append(self.path)
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _editor_on(s, port, **extra):
    from cairn.quality.collectors import ConnectionRegistry

    s.data_access.registry = ConnectionRegistry({"lake": {
        "url": f"trino://127.0.0.1:{port}/lakehouse", "run_as_user": True,
        "catalog": {"platform": "trino", "database": "lakehouse"}, **extra}})
    s.data_access.known = lambda urn: True
    s.data_access.can_read = lambda actor, urn: True


def test_trino_failures_do_not_show_internal_addresses_in_the_editor(client):
    """A connector's failure (Trino's EXTERNAL errors: a metastore, a bucket, a file path) and the client's own
    connection errors name hosts and paths inside the platform: the editor says what failed, with Trino's error name and
    query id for an operator, not those details. A person's own mistake (USER_ERROR) is still shown as Trino words it."""
    pytest.importorskip("trino")
    s = client.app.state.services
    secret = "Failed connecting to Hive metastore: [thrift://hms.internal.corp:9083]"
    kind = ["EXTERNAL"]

    def answer(_h, _m):
        return 200, {"id": "20261118_000000_00001_abcde", "infoUri": "http://x/ui", "stats": {"state": "FAILED"},
                     "error": {"message": secret if kind[0] == "EXTERNAL" else "line 1:8: Column 'nope' cannot be resolved",
                               "errorCode": 1, "errorName": "HIVE_METASTORE_ERROR" if kind[0] == "EXTERNAL" else
                               "COLUMN_NOT_FOUND", "errorType": kind[0]}}

    fake = _FakeTrino(answer)
    try:
        _editor_on(s, fake.port)
        r = client.post("/data/query", headers={"X-Cairn-Actor": ANA},
                        json={"sql": "SELECT id FROM sales.orders", "connection": "lake"})
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert "hms.internal" not in detail and "thrift" not in detail
        assert "HIVE_METASTORE_ERROR" in detail and "20261118_000000_00001_abcde" in detail
        kind[0] = "USER_ERROR"
        r = client.post("/data/query", headers={"X-Cairn-Actor": ANA},
                        json={"sql": "SELECT id FROM sales.orders", "connection": "lake"})
        assert r.status_code == 400 and "cannot be resolved" in r.json()["detail"]
    finally:
        fake.close()
    # Nothing listening: the client's error names the address it tried.
    r = client.post("/data/query", headers={"X-Cairn-Actor": ANA},
                    json={"sql": "SELECT id FROM sales.orders", "connection": "lake"})
    assert r.status_code == 502, r.text
    assert str(fake.port) not in r.json()["detail"] and "127.0.0.1" not in r.json()["detail"]


def test_pipeline_users_get_trino_tokens_too(client, monkeypatch):
    """A pipeline user signs in with a personal access token (or a workload token exchanged at Cairn) and
    gets a Trino token for itself, so Spark's governed reads and notebooks work for jobs as for people.
    The shared service token and agents do not."""
    s = client.app.state.services
    real = s.identity._me_lookup
    monkeypatch.setattr(s.identity, "_me_lookup",
                        lambda url, h, c: "urn:li:corpuser:svc-dbt" if h.get("Authorization") == "Bearer pat-svc-dbt" else real(url, h, c))
    r = client.post("/v1/trino/token", headers={"Authorization": "Bearer pat-svc-dbt"})
    assert r.status_code == 200, r.text
    assert r.json()["user"] == "svc-dbt"
    assert client.post("/v1/trino/token", headers={"Authorization": "Bearer svc-secret"}).status_code == 403


def test_every_replica_signs_with_the_one_key(settings, graph):
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.dataaccess.trino_auth import TrinoTokens

    engine = create_engine(settings.database_url)
    init_db(engine)
    a, b = TrinoTokens(engine, KEY, "cairn"), TrinoTokens(engine, KEY, "cairn")
    assert a.jwks() == b.jwks()
    # The key in the database is encrypted with the access key: another key cannot use it.
    with pytest.raises(RuntimeError, match="cannot be read"):
        TrinoTokens(engine, "another-key-" + "z" * 24, "cairn").jwks()
    with pytest.raises(ValueError):
        a.mint("urn:li:corpGroup:analysts")


def _completed(**changes):
    """A real QueryCompletedEvent from Trino 476's HTTP event listener (tests/data), with changes."""
    import copy
    import json
    import os

    e = json.load(open(os.path.join(os.path.dirname(__file__), "data", "trino_query_completed.json")))
    e = copy.deepcopy(e)
    for path, value in changes.items():
        obj, *keys = path.split("__")
        target = e[obj]
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    return e


NATION = "urn:li:dataset:(urn:li:dataPlatform:trino,tpch.tiny.nation,PROD)"


def test_reads_become_usage_statistics(client, graph):
    post = lambda e: client.post(f"/v1/trino/{KEY}/queries", json=e)  # noqa: E731
    r = post(_completed())
    assert r.status_code == 200 and r.json()["datasets"] == 2 and r.json()["written"] == 2, r.text
    usage = graph.get_timeseries(NATION, "datasetUsageStatistics", 1)[0]
    assert usage["totalSqlQueries"] == 1 and usage["uniqueUserCount"] == 1
    assert usage["userCounts"] == [{"user": ANA, "count": 1}]
    assert {f["fieldPath"] for f in usage["fieldCounts"]} == {"name", "regionkey"}
    assert usage["eventGranularity"] == {"unit": "DAY", "multiple": 1}
    # Trino resends an event it thinks was lost: counted once.
    assert post(_completed()).json().get("duplicate") is True
    # Bob reads it too, twice; the day's bucket adds up.
    post(_completed(metadata__queryId="q-bob-1", context__user="bob"))
    post(_completed(metadata__queryId="q-bob-2", context__user="bob"))
    usage = graph.get_timeseries(NATION, "datasetUsageStatistics", 1)[0]
    assert usage["totalSqlQueries"] == 3 and usage["uniqueUserCount"] == 2
    assert usage["userCounts"][0] == {"user": BOB, "count": 2}
    # Failed queries, EXPLAIN and Trino's own metadata are not reads.
    assert post(_completed(metadata__queryId="q-f", metadata__queryState="FAILED")).json()["datasets"] == 0
    assert post(_completed(metadata__queryId="q-e", context__queryType="EXPLAIN")).json()["datasets"] == 0
    meta = _completed(metadata__queryId="q-i")
    for i in meta["ioMetadata"]["inputs"]:
        i["schema"] = "information_schema"
    assert post(meta).json()["datasets"] == 0
    assert post({"nope": 1}).status_code == 422
    assert client.post("/v1/trino/wrong-key/queries", json=_completed()).status_code == 404


def test_usage_waits_while_the_metadata_store_is_down(client, graph, monkeypatch):
    real = graph.emit
    monkeypatch.setattr(graph, "emit", lambda _m: (_ for _ in ()).throw(OSError("down")))
    r = client.post(f"/v1/trino/{KEY}/queries", json=_completed(metadata__queryId="q-down"))
    assert r.status_code == 200 and r.json()["written"] == 0
    assert graph.get_timeseries(NATION, "datasetUsageStatistics", 1) == []
    monkeypatch.setattr(graph, "emit", real)
    assert client.app.state.services.trino_usage.flush() == 2  # the retry loop's call
    assert graph.get_timeseries(NATION, "datasetUsageStatistics", 1)[0]["totalSqlQueries"] == 1


def test_the_catalog_crawler_sees_structure_and_reads_no_rows(client):
    c = client
    assert ask(c, "allow", "cairn-catalog", "ShowColumns", table("salaries", "hr")) is True
    assert ask(c, "allow", "cairn-catalog", "SelectFromColumns", table("tables", "information_schema", columns=["x"])) is True
    assert ask(c, "allow", "cairn-catalog", "SelectFromColumns", table("salaries", "hr", columns=["salary"])) is False
    assert ask(c, "allow", "cairn-catalog", "InsertIntoTable", table("orders")) is False
    assert ask(c, "allow", "cairn-catalog", "CreateSchema", {"schema": {"catalogName": "lakehouse", "schemaName": "x"}}) is False
    tables = [table("orders"), table("salaries", "hr")]
    assert ask(c, "batch", "cairn-catalog", "FilterTables", filterResources=tables) == [0, 1]
    assert ask(c, "batch", "cairn-catalog", "FilterColumns", filterResources=[table("salaries", "hr", columns=["a", "b"])]) == [0, 1]
    # Nobody gets a token for the crawler: only Cairn signs in as it.
    r = c.post("/v1/trino/token", headers={"X-Cairn-Actor": "urn:li:corpuser:cairn-catalog"})
    assert r.status_code == 403


def test_trino_types_become_catalog_types():
    from cairn.dataaccess.trino_catalog import field_type

    assert [field_type(t) for t in ("varchar(10)", "bigint", "decimal(10,2)", "boolean", "date",
                                    "timestamp(6) with time zone", "array(integer)", "map(varchar, bigint)",
                                    "row(a integer)", "varbinary", "json")] == [
        "string", "number", "number", "boolean", "date", "time", "array", "map", "record", "bytes", "string"]


def _code(client, actor=ANA, **extra):
    from urllib.parse import parse_qs, urlsplit

    params = {"response_type": "code", "client_id": "trino", "redirect_uri": "https://trino.test/oauth2/callback",
              "state": "st-1", **extra}
    r = client.get("/v1/trino/oauth/authorize", params=params, headers={"X-Cairn-Actor": actor} if actor else {},
                   follow_redirects=False)
    return r, {k: v[0] for k, v in parse_qs(urlsplit(r.headers.get("location", "")).query).items()}


def _exchange(client, code, secret=OAUTH_SECRET, verifier="", redirect="https://trino.test/oauth2/callback"):
    return client.post("/v1/trino/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                                      "client_id": "trino", "client_secret": secret, "code_verifier": verifier})


def test_trino_signs_in_through_cairn(client):
    import jwt

    # Not signed in to Cairn: off to Cairn's sign-in, then back here.
    r, _ = _code(client, actor=None)
    assert r.status_code == 302 and r.headers["location"].startswith("/login?redirect_uri=%2Fcairn%2Fapi%2Fv1%2Ftrino%2Foauth%2Fauthorize")
    # Signed in: back to Trino with a code and its state.
    r, q = _code(client)
    assert r.status_code == 302 and r.headers["location"].startswith("https://trino.test/oauth2/callback?") and q["state"] == "st-1"
    out = _exchange(client, q["code"])
    assert out.status_code == 200, out.text
    key = jwt.algorithms.RSAAlgorithm.from_jwk(client.get("/v1/trino/jwks.json").json()["keys"][0])
    assert jwt.decode(out.json()["access_token"], key, algorithms=["RS256"], audience="trino")["sub"] == "ana"
    # One use only; the secret and the redirect address must match.
    assert _exchange(client, q["code"]).json()["error"] == "invalid_grant"
    _, q2 = _code(client)
    assert _exchange(client, q2["code"], secret="wrong" * 6).status_code == 401
    _, q3 = _code(client)
    assert _exchange(client, q3["code"], redirect="https://trino.test/oauth2/callback/other").json()["error"] == "invalid_grant"
    # Never redirects to an address Cairn does not trust.
    r, _ = _code(client, redirect_uri="https://evil.test/cb")
    assert r.status_code == 400
    # The crawler identity cannot sign in.
    r, _ = _code(client, actor="urn:li:corpuser:cairn-catalog")
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    entries = client.app.state.services.audit.entries(category="trino")
    assert any(e["action"] == "trino.sign_in" and e["actor"] == ANA for e in entries)


def test_trino_sign_in_with_pkce(client):
    import base64
    import hashlib

    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _, q = _code(client, code_challenge=challenge, code_challenge_method="S256")
    assert _exchange(client, q["code"], verifier="w" * 50).json()["error"] == "invalid_grant"
    _, q = _code(client, code_challenge=challenge, code_challenge_method="S256")
    assert _exchange(client, q["code"], verifier=verifier).status_code == 200
    r, q = _code(client, code_challenge=challenge, code_challenge_method="plain")
    assert q.get("error") == "invalid_request"


def test_airflow_signs_in_through_cairn_with_its_own_client(client, graph):
    import jwt

    graph.aspects[(ANA, "groupMembership")] = {"groups": ["urn:li:corpGroup:data-engineers"]}
    client.app.state.services.attributes.invalidate(ANA)
    cb = "https://airflow.test/auth/oauth-authorized/cairn"
    r, q = _code(client, client_id="airflow", redirect_uri=cb)
    assert r.status_code == 302 and r.headers["location"].startswith(cb + "?"), r.headers.get("location")
    # The code is Airflow's: Trino's credentials cannot redeem it, nor Airflow's at Trino's address.
    assert _exchange(client, q["code"], redirect=cb).json()["error"] == "invalid_grant"  # Trino's credentials, Airflow's code
    _, q = _code(client, client_id="airflow", redirect_uri=cb)
    out = client.post("/v1/trino/oauth/token", data={"grant_type": "authorization_code", "code": q["code"], "redirect_uri": cb,
                                                      "client_id": "airflow", "client_secret": AIRFLOW_SECRET})
    assert out.status_code == 200, out.text
    key = jwt.algorithms.RSAAlgorithm.from_jwk(client.get("/v1/trino/jwks.json").json()["keys"][0])
    claims = jwt.decode(out.json()["access_token"], key, algorithms=["RS256"], audience="airflow")
    assert claims["sub"] == "ana" and claims["groups"] == ["data-engineers"]
    # Trino never accepts an Airflow token (another audience), and Airflow's redirects are not Trino's.
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(out.json()["access_token"], key, algorithms=["RS256"], audience="trino")
    r, _ = _code(client, client_id="airflow", redirect_uri="https://trino.test/oauth2/callback")
    assert r.status_code == 400
    r, _ = _code(client, client_id="trino", redirect_uri=cb)
    assert r.status_code == 400
    # A code for Trino cannot be redeemed with Airflow's credentials either.
    _, q = _code(client)
    out = client.post("/v1/trino/oauth/token", data={"grant_type": "authorization_code", "code": q["code"],
                                                      "redirect_uri": "https://trino.test/oauth2/callback",
                                                      "client_id": "airflow", "client_secret": AIRFLOW_SECRET})
    assert out.json()["error"] == "invalid_grant"
    entries = client.app.state.services.audit.entries(category="identity")
    assert any(e["action"] == "airflow.sign_in" and e["actor"] == ANA for e in entries)


def test_ai_functions_need_their_privilege(client):
    """Trino's AI functions send rows to a model outside the lakehouse: USE_AI_FUNCTIONS, granted like any
    privilege. Built-in and inline functions (SQL, Python) stay open."""
    c = client
    fn = lambda cat, name="ai_gen": {"function": {"catalogName": cat, "schemaName": "ai", "functionName": name}}  # noqa: E731
    assert ask(c, "allow", "admin", "ExecuteFunction", fn("llm")) is True       # "*" covers it
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("llm")) is False
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("system", "abs")) is True
    assert ask(c, "allow", "cairn-catalog", "ExecuteFunction", fn("llm")) is False   # the crawler never calls models
    from cairn.governance.abac import Policy, PolicyBundle
    s = c.app.state.services
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(id="ana-ai", effect="allow", actions=["USE_AI_FUNCTIONS"],
                                                                 condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("llm")) is True
    assert ask(c, "allow", "eve", "ExecuteFunction", fn("llm")) is False


def test_stored_functions_are_granted_one_by_one(client):
    """Stored functions (the routines catalog): USE_FUNCTION on each to call it, MANAGE_DATASET on it to
    create or drop it; the crawler does neither."""
    from cairn.governance.abac import Policy, PolicyBundle, Target

    c = client
    fn = lambda name: {"function": {"catalogName": "routines", "schemaName": "shared", "functionName": name}}  # noqa: E731
    assert ask(c, "allow", "admin", "ExecuteFunction", fn("domain_of")) is True
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("domain_of")) is False
    assert ask(c, "allow", "admin", "CreateFunction", fn("x")) is True
    assert ask(c, "allow", "ana", "CreateFunction", fn("x")) is False
    assert ask(c, "allow", "cairn-catalog", "ExecuteFunction", fn("domain_of")) is False
    s = c.app.state.services
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="ana-fn", effect="allow", actions=["USE_FUNCTION"], target=Target(resource_urns=["urn:li:function:routines.shared.domain_*"]),
        condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("domain_of")) is True
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("salary_band")) is False
    assert ask(c, "allow", "ana", "DropFunction", fn("domain_of")) is False


def test_a_warehouse_takes_queries_from_its_team(client):
    """A team's warehouse asks as itself: running a query there needs USE_WAREHOUSE on it; what the query
    may read is decided as everywhere else."""
    c = client
    s = c.app.state.services
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="analysts-sales-wh", effect="allow", actions=["USE_WAREHOUSE"], target=Target(resource_urns=["urn:li:warehouse:sales"]),
        condition={"attr": "subject.groups", "op": "contains", "value": "urn:li:corpGroup:analysts"})]))
    assert ask(c, "warehouse/sales/allow", "ana", "ExecuteQuery") is False
    assert ask(c, "warehouse/sales/allow", "ana", "ExecuteQuery", groups=["analysts"]) is True
    assert ask(c, "warehouse/finance/allow", "ana", "ExecuteQuery", groups=["analysts"]) is False   # not their warehouse
    assert ask(c, "warehouse/finance/allow", "admin", "ExecuteQuery") is True                       # operators
    assert ask(c, "allow", "ana", "ExecuteQuery") is True                                           # the shared Trino
    # Inside the warehouse, tables are decided as anywhere.
    assert ask(c, "warehouse/sales/allow", "ana", "SelectFromColumns", table("salaries", "hr", columns=["salary"]),
               groups=["analysts"]) is False
    assert ask(c, "warehouse/sales/allow", "eve", "ExecuteQuery", groups=["analysts"]) is False     # deny wins
    r = c.post(f"/v1/trino/{KEY}/warehouse/Bad_Name/allow", json={"input": {}})
    assert r.status_code == 404


def test_an_app_or_clean_room_identity_needs_the_warehouse_too(client):
    """Review finding: an installed app's identity (its token is handed to the app's own code) and a clean room's
    were admitted to every team's warehouse: their ExecuteQuery was answered before the warehouse's USE_WAREHOUSE
    check. An app runs on a warehouse only when its installer may use it; a clean room is no team's member."""
    c = client
    s = c.app.state.services
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="ana-sales-wh", effect="allow", actions=["USE_WAREHOUSE"], target=Target(resource_urns=["urn:li:warehouse:sales"]),
        condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    before = (s.trino_access.app_identity, s.trino_access.cleanroom_tables)
    try:
        s.trino_access.app_identity = lambda user: {"actor": ANA, "tables": set()} if user == "cairnapp-ana-churn" else None
        s.trino_access.cleanroom_tables = lambda room: {"lakehouse.sales.orders"}
        assert ask(c, "warehouse/sales/allow", "cairnapp-ana-churn", "ExecuteQuery") is True     # Ana may use sales
        assert ask(c, "warehouse/finance/allow", "cairnapp-ana-churn", "ExecuteQuery") is False  # not finance
        assert ask(c, "allow", "cairnapp-ana-churn", "ExecuteQuery") is True                     # the shared Trino
        assert ask(c, "warehouse/sales/allow", "cleanroom-r1", "ExecuteQuery") is False
        assert ask(c, "allow", "cleanroom-r1", "ExecuteQuery") is True
    finally:
        s.trino_access.app_identity, s.trino_access.cleanroom_tables = before


def test_the_warehouse_proxy_asks_cairn_before_it_starts_a_warehouse(client):
    """The warehouse proxy admits a request only when its token is a Trino token Cairn issued, for someone the
    warehouse would run a query for; everyone else neither starts the warehouse nor keeps it up."""
    c = client
    s = c.app.state.services
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="ana-sales-wh", effect="allow", actions=["USE_WAREHOUSE"], target=Target(resource_urns=["urn:li:warehouse:sales"]),
        condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))

    def admit(wh, token, key=KEY):
        return c.post(f"/v1/trino/{key}/warehouse/{wh}/admit", json={"token": token})

    ana = s.trino_tokens.mint(ANA, 300)
    r = admit("sales", ana["token"])
    assert r.status_code == 200 and r.json() == {"result": True, "expires_at": ana["expires_at"]}
    assert admit("finance", ana["token"]).json()["result"] is False               # not her team's
    assert admit("finance", s.trino_tokens.mint(ADMIN, 300)["token"]).json()["result"] is True   # operators
    assert admit("sales", s.trino_tokens.mint(BOB, 300)["token"]).json()["result"] is False
    assert admit("sales", s.trino_tokens.mint(ANA, 300, audience="airflow")["token"]).json()["result"] is False
    assert admit("sales", ana["token"][:-4] + "AAAA").json()["result"] is False     # not Cairn's signature
    assert admit("sales", "").json()["result"] is False
    assert admit("sales", ana["token"], key="trino-key-" + "x" * 24).status_code == 404
    assert admit("Bad_Name", ana["token"]).status_code == 404


def test_iceberg_metadata_tables_and_connector_table_functions(client):
    """Metadata tables are decided on their table: snapshots, history and refs (no row values) for its readers,
    the rest (file bounds can show masked values) for its managers; none takes masks or row filters. Connector
    functions such as table_changes or a JDBC catalog's query return rows past masks and filters: operators only."""
    c = client
    snaps = table("orders$snapshots", columns=["snapshot_id"])
    assert ask(c, "allow", "ana", "SelectFromColumns", snaps) is True
    assert ask(c, "allow", "ana", "SelectFromColumns", table("salaries$snapshots", "hr", columns=["snapshot_id"])) is False
    assert ask(c, "allow", "ana", "SelectFromColumns", table("orders$files", columns=["lower_bounds"])) is False
    assert ask(c, "allow", "admin", "SelectFromColumns", table("orders$files", columns=["lower_bounds"])) is True
    assert ask(c, "allow", "ana", "DeleteFromTable", table("orders$snapshots")) is False
    assert ask(c, "batch", "ana", "FilterColumns", filterResources=[snaps]) == [0]
    assert ask(c, "batch", "ana", "FilterColumns", filterResources=[table("orders$files", columns=["lower_bounds"])]) == []
    assert ask(c, "row-filters", "bob", "GetRowFilters", table("orders$snapshots")) == []
    # Delta Lake's $history carries each commit's operation parameters: a DELETE's or MERGE's predicate, with the
    # values it named (WHERE ssn = '...'), which may be masked for the reader: the table's managers only there.
    delta = {"table": {"catalogName": "delta", "schemaName": "sales", "tableName": "orders$history",
                       "columns": ["operation_parameters"]}}
    assert ask(c, "allow", "ana", "SelectFromColumns", table("orders$history", columns=["operation"])) is True
    assert ask(c, "allow", "ana", "SelectFromColumns", {"table": {**delta["table"], "tableName": "orders"}}) is True
    assert ask(c, "allow", "ana", "SelectFromColumns", delta) is False
    assert ask(c, "batch", "ana", "FilterColumns", filterResources=[delta]) == []
    assert ask(c, "allow", "admin", "SelectFromColumns", delta) is True
    fn = lambda cat, sch, name: {"function": {"catalogName": cat, "schemaName": sch, "functionName": name}}  # noqa: E731
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("lakehouse", "system", "table_changes")) is False
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("postgres", "system", "query")) is False
    assert ask(c, "allow", "admin", "ExecuteFunction", fn("lakehouse", "system", "table_changes")) is True
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("lakehouse", "system", "bucket")) is True
    assert ask(c, "allow", "ana", "ExecuteFunction", fn("system", "builtin", "sequence")) is True


def test_session_properties_that_lift_the_clusters_limits_need_the_system_privilege(client):
    """Anyone may set ordinary session properties; the ones that lift memory, CPU, scan, priority or time limits for
    a query need SYSTEM (Trino does not cap a session's time limits at the cluster's)."""
    c = client
    prop = lambda n: {"systemSessionProperty": {"name": n}}  # noqa: E731
    assert ask(c, "allow", "ana", "SetSystemSessionProperty", prop("join_distribution_type")) is True
    for limit in ("query_max_memory", "resource_overcommit", "query_max_cpu_time", "query_priority",
                  "query_max_run_time", "query_max_execution_time", "query_max_planning_time"):
        assert ask(c, "allow", "ana", "SetSystemSessionProperty", prop(limit)) is False, limit
        assert ask(c, "allow", "admin", "SetSystemSessionProperty", prop(limit)) is True, limit


def test_a_read_only_external_catalog_refuses_its_procedures_to_everyone(client):
    """A read-only external catalog (L105) refuses every write, table and schema change, to administrators too:
    the Iceberg connector's procedures (``CALL partner.system.unregister_table(...)``, ``register_table``,
    ``rollback_to_snapshot``) change the partner's catalog as well, so they are refused there; a writable one, and
    the lakehouse, keep the system privilege's rule."""
    c = client
    ext = {"partner": {"platform": "iceberg", "database": "partner", "writable": False},
           "shared": {"platform": "iceberg", "database": "shared", "writable": True}}
    c.services.trino_access.external_catalog = ext.get
    proc = lambda cat, name: {"function": {"catalogName": cat, "schemaName": "system", "functionName": name}}  # noqa: E731
    assert ask(c, "allow", "admin", "ExecuteProcedure", proc("partner", "unregister_table")) is False
    assert ask(c, "allow", "admin", "ExecuteProcedure", proc("partner", "register_table")) is False
    assert ask(c, "allow", "admin", "ExecuteProcedure", proc("shared", "unregister_table")) is True
    assert ask(c, "allow", "admin", "ExecuteProcedure", proc("lakehouse", "register_table")) is True
    assert ask(c, "allow", "ana", "ExecuteProcedure", proc("lakehouse", "register_table")) is False

    def broken(catalog):
        raise RuntimeError("database down")
    c.services.trino_access.external_catalog = broken                 # cannot tell: refused
    assert ask(c, "allow", "admin", "ExecuteProcedure", proc("partner", "unregister_table")) is False


def test_a_suspended_person_is_refused_with_a_token_already_issued(client, graph):
    """Review finding: Trino tokens live up to 12 hours (single sign-on's an hour) and Trino checks only their
    signature, so a suspended person kept querying until theirs ran out. Every request now checks that the person is
    not suspended (the answer reused for 30 seconds); a status that cannot be read refuses."""
    c = client
    access = c.services.trino_access

    def post(path, user, op, **action):
        body = {"input": {"context": {"identity": {"user": user, "groups": []}}, "action": {"operation": op, **action}}}
        return c.post(f"/v1/trino/{KEY}/{path}", json=body)

    orders = table("orders", columns=["id"])
    assert post("allow", "ana", "SelectFromColumns", resource=orders).json()["result"] is True
    graph.aspects[(ANA, "corpUserStatus")] = {"status": "SUSPENDED"}
    access._status.clear()
    assert post("allow", "ana", "ExecuteQuery").status_code == 403
    assert post("allow", "ana", "SelectFromColumns", resource=orders).status_code == 403
    assert post("batch", "ana", "FilterTables", filterResources=[orders]).status_code == 403
    assert post("row-filters", "ana", "GetRowFilters", resource=orders).status_code == 403
    assert post("allow", "admin", "SelectFromColumns", resource=orders).json()["result"] is True   # nobody else
    graph.aspects[(ANA, "corpUserStatus")] = {"status": "ACTIVE"}
    access._status.clear()
    assert post("allow", "ana", "SelectFromColumns", resource=orders).json()["result"] is True
    before = access.user_status
    try:
        access.user_status = lambda urn: (_ for _ in ()).throw(RuntimeError("metadata store down"))
        access._status.clear()
        assert post("allow", "ana", "ExecuteQuery").status_code == 403                             # fail closed
    finally:
        access.user_status = before
        access._status.clear()


# ---- the catalog sync (TrinoCatalogSync) against a stand-in for Trino's information_schema ----------------------------
class _Trino:
    """Answers the sync's three metadata queries per Trino catalog: {catalog: {"columns": {(schema, table): [col]},
    "tables": {(schema, table): type}}}; ``during`` runs once inside the next query (another sync meanwhile)."""

    def __init__(self, connections: dict, catalogs: dict):
        from types import SimpleNamespace

        self.connections, self.catalogs, self.during = connections, catalogs, None
        self.registry = SimpleNamespace(definitions=lambda: self.connections)

    def run_on_trino(self, definition, actor, sql, limit):
        assert actor == "urn:li:corpuser:cairn-catalog"
        if self.during is not None:
            run, self.during = self.during, None
            run()
        cat = self.catalogs[definition["url"].rsplit("/", 1)[1]]
        if "information_schema.columns" in sql:
            rows = [[s, t, c, "varchar", "YES", None] for (s, t), cols in cat["columns"].items() for c in cols]
        elif "information_schema.tables" in sql:
            tables = cat.get("tables") or {k: "BASE TABLE" for k in cat["columns"]}
            rows = [[s, t, kind] for (s, t), kind in tables.items()]
        else:
            rows = []
        return [], rows, False


def _catalog_sync(engine, graph, connections, catalogs):
    from cairn.dataaccess.trino import TrinoAccess
    from cairn.dataaccess.trino_catalog import TrinoCatalogSync

    trino = _Trino(connections, catalogs)
    access = TrinoAccess(lambda *a: None, lambda *a: None, {})
    return TrinoCatalogSync(engine, graph, trino, access), trino


def _removed(graph, urn):
    return (graph.get_aspect(urn, "status") or {}).get("removed")


T_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.orders,PROD)"
T_BROKEN = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.broken_view,PROD)"


def test_catalog_sync_keeps_a_table_trino_lists_without_columns(engine, graph):
    """information_schema.columns leaves out a table whose columns Trino cannot read right now (an Iceberg table whose
    metadata file is unreachable, a Hive view): it is still in information_schema.tables, so it is not removed."""
    conns = {"lake": {"url": "trino://trino:8080/lake", "run_as_user": True}}
    cols = {("sales", "orders"): ["id"], ("sales", "broken_view"): ["a"]}
    sync, trino = _catalog_sync(engine, graph, conns, {"lake": {"columns": cols}})
    assert sync.sync("lake", ADMIN)["tables"] == 2
    trino.catalogs["lake"] = {"columns": {("sales", "orders"): ["id"]},
                              "tables": {("sales", "orders"): "BASE TABLE", ("sales", "broken_view"): "VIEW"}}
    out = sync.sync("lake", ADMIN)
    assert out["removed"] == 0 and out["tables"] == 2
    assert _removed(graph, T_BROKEN) is False
    assert graph.get_aspect(T_BROKEN, "schemaMetadata")["fields"][0]["fieldPath"] == "a"   # its schema is kept
    trino.catalogs["lake"] = {"columns": {("sales", "orders"): ["id"]}}       # now gone from both: removed
    assert sync.sync("lake", ADMIN)["removed"] == 1 and _removed(graph, T_BROKEN) is True


def test_catalog_sync_never_removes_a_table_another_connection_still_has(engine, graph):
    """Two connections reach the same Trino catalog (one per team, say). When one stops reaching it (its URL now names
    another catalog), its tables there are still the other's: they are not marked removed."""
    conns = {"a": {"url": "trino://trino:8080/lake", "run_as_user": True},
             "b": {"url": "trino://trino:8080/lake", "run_as_user": True}}
    lake = {"columns": {("sales", "orders"): ["id"]}}
    other = {"columns": {("x", "y"): ["id"]}}
    sync, trino = _catalog_sync(engine, graph, conns, {"lake": lake, "other": other})
    sync.sync("a", ADMIN)
    sync.sync("b", ADMIN)
    conns["a"]["url"] = "trino://trino:8080/other"
    assert sync.sync("a", ADMIN)["removed"] == 0 and _removed(graph, T_ORDERS) is False
    conns["b"] = {"url": "trino://trino:8080/other", "run_as_user": True}
    assert sync.sync("b", ADMIN)["removed"] == 1 and _removed(graph, T_ORDERS) is True   # nobody has it now


def test_forgetting_an_external_catalog_keeps_tables_a_trino_connection_still_has(engine, graph):
    """An external catalog unregistered: its tables are marked removed, except those a Trino connection's sync still
    has (the same Trino catalog read through a run_as_user connection)."""
    from cairn.dataaccess.external_catalogs import SYNC_PREFIX, ExternalCatalogs
    from cairn.dataaccess.trino_catalog import trino_catalog_tables as t

    only_ext = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.only_ext,PROD)"
    with engine.begin() as conn:
        conn.execute(t.insert(), [{"connection": SYNC_PREFIX + "lake", "urn": T_ORDERS},
                                  {"connection": SYNC_PREFIX + "lake", "urn": only_ext},
                                  {"connection": "lakehouse", "urn": T_ORDERS}])
    ExternalCatalogs(engine, graph, None)._forget_tables("lake")
    assert _removed(graph, only_ext) is True and _removed(graph, T_ORDERS) is None


def test_one_catalog_sync_of_a_connection_at_a_time(engine, graph):
    """A sync started while another of the same connection runs (Sync catalog pressed twice, or on two replicas, or
    with the schedule) is refused: interleaved, an older listing could be written over a newer one (a dropped table
    back as present) and the two rewrites of the connection's tables collide. The lease ends with the sync, failed or
    not, and lapses if a replica dies holding it."""
    from cairn.dataaccess.trino_catalog import CatalogSyncError, trino_catalog_runs

    conns = {"lake": {"url": "trino://trino:8080/lake", "run_as_user": True},
             "other": {"url": "trino://trino:8080/lake", "run_as_user": True}}
    sync, trino = _catalog_sync(engine, graph, conns, {"lake": {"columns": {("sales", "orders"): ["id"]}}})
    seen = {}

    def meanwhile():
        with pytest.raises(CatalogSyncError) as e:
            sync.sync("lake", ADMIN)
        seen["status"] = e.value.status
        seen["other"] = sync.sync("other", ADMIN)["tables"]        # another connection is not held up
        sync.run_due(3600)                                         # the schedule skips it too (logged, not raised)
    trino.during = meanwhile
    assert sync.sync("lake", ADMIN)["tables"] == 1
    assert seen == {"status": 409, "other": 1}
    assert sync.sync("lake", ADMIN)["tables"] == 1                  # released when done

    def fail(*a):
        raise RuntimeError("trino down")
    trino.run_on_trino, ok = fail, trino.run_on_trino
    with pytest.raises(RuntimeError):
        sync.sync("lake", ADMIN)
    trino.run_on_trino = ok
    assert sync.sync("lake", ADMIN)["tables"] == 1                  # released when it fails too
    with engine.begin() as conn:                                    # a replica died holding it: it lapses
        conn.execute(trino_catalog_runs.update().where(trino_catalog_runs.c.connection == "lake")
                     .values(lease_until=1))
    assert sync.sync("lake", ADMIN)["tables"] == 1


def test_a_token_that_is_not_json_objects_is_refused_cleanly(settings):
    """Whatever an unauthenticated caller sends as a token, verify says ValueError (refused), never another error that
    surfaces as a 500 (the warehouse proxy's admit, the lakehouse and SSO token exchanges)."""
    import base64
    import json

    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.dataaccess.trino_auth import TrinoTokens

    engine = create_engine(settings.database_url)
    init_db(engine)
    t = TrinoTokens(engine, KEY, "cairn")
    good = t.mint("urn:li:corpuser:ana")["token"]
    h, c, sig = good.split(".")

    def b64(v) -> str:
        return base64.urlsafe_b64encode(json.dumps(v).encode()).rstrip(b"=").decode()
    for bad in (f"{b64([])}.{c}.{sig}", f"{h}.{b64(['sub'])}.{sig}", f"{b64('x')}.{b64(1)}.{sig}", f"{h}.{c}.!!!",
                "a.b", None, 7):
        with pytest.raises(ValueError):
            t.verify(bad, "trino")
        with pytest.raises(ValueError):
            t.verify_document(bad, "cairn-data-package+jws")
    assert t.verify(good, "trino")["sub"] == "ana"
