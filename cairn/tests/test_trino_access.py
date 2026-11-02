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
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""}}')
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
    return TestClient(create_api_app(s))


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


def test_lineage_jobs_are_named_by_what_they_write(client):
    ev = {"eventType": "COMPLETE", "eventTime": "2026-11-01T00:00:00Z", "run": {"runId": "q1"},
          "job": {"namespace": "trino://trino:8080", "name": "20261101_000000_00001_abcde"},
          "inputs": [{"namespace": "trino://trino:8080", "name": "lake.sales.orders"}],
          "outputs": [{"namespace": "trino://trino:8080", "name": "lake.sales.daily"}]}
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 200 and r.json()["results"][0]["job"].endswith("trino:lake.sales.daily)")
    assert client.post("/v1/trino/wrong-key/lineage", json=ev).status_code == 404
    assert client.post(f"/v1/trino/{KEY}/lineage", json={"job": {}}).status_code == 422


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
    claimed_by_a = a._claim(now, 10)
    assert len(claimed_by_a) == 1 and b._claim(now, 10) == []  # b sees it claimed


def test_lineage_is_refused_when_it_cannot_be_held_either(client, graph, monkeypatch):
    monkeypatch.setattr(graph, "emit", lambda _m: (_ for _ in ()).throw(OSError("down")))
    box = client.app.state.services.trino_outbox
    monkeypatch.setattr(box, "hold", lambda *_a: (_ for _ in ()).throw(OSError("database down")))
    ev = {"eventType": "COMPLETE", "job": {"namespace": "trino://t:8080", "name": "q"}, "run": {"runId": "r"},
          "outputs": [{"namespace": "trino://t:8080", "name": "lake.s.t"}]}
    r = client.post(f"/v1/trino/{KEY}/lineage", json=ev)
    assert r.status_code == 503 and "metadata store" in r.json()["detail"]


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
