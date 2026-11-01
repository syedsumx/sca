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
    assert mask_expression("salary", "decimal(10,2)", "partial") == "CAST(NULL AS decimal(10,2))"
    assert mask_expression("e", "varchar", "null") == "CAST(NULL AS varchar)"
    assert "sha256" in mask_expression("e", "varchar(255)", "hash")
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


def test_lineage_reports_an_unavailable_metadata_store(client, graph, monkeypatch):
    def down(_mcps):
        raise OSError("Name or service not known")

    monkeypatch.setattr(graph, "emit", down)
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
