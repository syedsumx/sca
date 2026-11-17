"""Published metric views (L56) through Trino access control: a read of ``<catalog>.metrics.<name>`` is decided on
the metric's table only in the catalog its view is made in (its connection's); a same-named table in another
catalog's ``metrics`` schema is decided on its own terms, so publishing a metric never opens someone else's table."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.quality.collectors import ConnectionRegistry
from cairn.services import build_services
from cairn.sqlworkspace.metrics import metrics

KEY = "trino-key-" + "k" * 24
ADMIN, BOB = (f"urn:li:corpuser:{u}" for u in ("admin", "bob"))


@pytest.fixture
def client(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", KEY)
    # Two catalogs over the same Iceberg data: sales.orders is the same dataset in either.
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""},'
                                               ' "lake2": {"platform": "iceberg", "database": ""}}')
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="read-sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.orders*"])),
    ]))
    s.data_access.registry = ConnectionRegistry({"lake": {"url": "trino://trino.test:8443/lakehouse/sales?protocol=https",
                                                           "run_as_user": True}})
    with s.engine.begin() as conn:
        conn.execute(insert(metrics).values(
            id=uuid.uuid4().hex, name="payroll", label="Payroll", description="", connection="lake",
            table_name="sales.orders", measure="Sum([amount])", filters="[]", time_column=None, dimensions="[]",
            owner=ADMIN, certified=False, version=1, updated_ms=int(time.time() * 1000), published=True, synonyms="[]"))
    c = TestClient(create_api_app(s))
    c.services = s
    return c


def reads(c, user, catalog, schema, table):
    body = {"input": {"context": {"identity": {"user": user, "groups": []}, "softwareStack": {"trinoVersion": "476"}},
                      "action": {"operation": "SelectFromColumns",
                                 "resource": {"table": {"catalogName": catalog, "schemaName": schema, "tableName": table,
                                                        "columns": ["amount"]}}}}}
    r = c.post(f"/v1/trino/{KEY}/allow", json=body)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_the_view_is_read_on_its_tables_terms_in_its_own_catalog_only(client):
    c = client
    # The published view, in its connection's catalog: whoever reads sales.orders reads it.
    assert reads(c, "bob", "lakehouse", "metrics", "payroll") is True
    assert reads(c, "bob", "lakehouse", "sales", "orders") is True
    # metrics.payroll in another catalog is not the metric's view (it is never made there): a table of that name
    # is decided on its own terms, which give Bob nothing.
    assert reads(c, "bob", "lake2", "metrics", "payroll") is False
    assert reads(c, "admin", "lake2", "metrics", "payroll") is True


def test_a_view_whose_connection_names_no_catalog_is_decided_on_its_own_terms(client):
    c, s = client, client.services
    s.data_access.registry = ConnectionRegistry({"lake": {"url": "trino://trino.test:8443?protocol=https", "run_as_user": True}})
    assert reads(c, "bob", "lakehouse", "metrics", "payroll") is False


# ---- only Cairn makes or changes views in the metrics schema ------------------------------------------------------

CAROL = "urn:li:corpuser:carol"


def asks(c, user, op, catalog, schema, table, target=None):
    action = {"operation": op, "resource": {"table": {"catalogName": catalog, "schemaName": schema, "tableName": table}}}
    if target is not None:
        action["targetResource"] = {"table": dict(zip(("catalogName", "schemaName", "tableName"), target))}
    body = {"input": {"context": {"identity": {"user": user, "groups": []}, "softwareStack": {"trinoVersion": "476"}},
                      "action": action}}
    r = c.post(f"/v1/trino/{KEY}/allow", json=body)
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture
def guarded(client):
    s = client.services
    # Carol may manage the metric view's dataset and read a secret table: what a metric publisher may have.
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="read-sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.orders*"])),
        Policy(id="carol", effect="allow", actions=["MANAGE_DATASET", "VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*metrics.*", "*secret.*", "*scratch.*"]),
               condition={"attr": "subject.urn", "op": "eq", "value": CAROL}),
    ]))
    return client


def test_nobody_but_cairn_makes_or_swaps_a_view_in_the_metrics_schema(guarded):
    """Publishing metric payroll over sales.orders and then replacing lakehouse.metrics.payroll in Trino directly
    with a SECURITY DEFINER view over a secret table would give every reader of sales.orders the secret table: a
    person (even one with MANAGE_DATASET there, even an admin) makes, drops, renames or re-owns no view there."""
    c = guarded
    for user in ("carol", "admin"):
        for op in ("CreateView", "DropView", "SetViewAuthorization", "SetViewComment", "CreateTable", "DropTable",
                   "CreateMaterializedView", "InsertIntoTable", "SetTableAuthorization"):
            assert asks(c, user, op, "lakehouse", "metrics", "payroll") is False, (user, op)
        # Renamed into the metrics schema, or out of it.
        for op in ("RenameView", "RenameTable"):
            assert asks(c, user, op, "lakehouse", "scratch", "x", ("lakehouse", "metrics", "payroll")) is False
            assert asks(c, user, op, "lakehouse", "metrics", "payroll", ("lakehouse", "scratch", "x")) is False
        # The same schema in another catalog: no metric view is made there, and nobody makes one there either.
        assert asks(c, user, "CreateView", "lake2", "metrics", "payroll") is False
    # A schema renamed into place is refused too.
    body = {"input": {"context": {"identity": {"user": "admin", "groups": []}}, "action": {
        "operation": "RenameSchema", "resource": {"schema": {"catalogName": "lakehouse", "schemaName": "scratch"}},
        "targetResource": {"schema": {"catalogName": "lakehouse", "schemaName": "metrics"}}}}}
    assert c.post(f"/v1/trino/{KEY}/allow", json=body).json()["result"] is False
    # Elsewhere nothing changes: Carol makes a view in her own schema, the admin anywhere.
    assert asks(c, "carol", "CreateView", "lakehouse", "scratch", "v") is True
    assert asks(c, "admin", "CreateView", "lakehouse", "sales", "v") is True
    assert asks(c, "admin", "RenameView", "lakehouse", "sales", "v", ("lakehouse", "sales", "w")) is True
    # Reads are as before: the view on its table's terms.
    assert reads(c, "bob", "lakehouse", "metrics", "payroll") is True
    assert reads(c, "carol", "lakehouse", "metrics", "payroll") is True


def test_cairns_service_identity_makes_and_drops_the_views_only(guarded):
    c, s = guarded, guarded.services
    # The connection's service identity (default cairn-quality) creates (or replaces) and drops views in its catalog.
    assert asks(c, "cairn-quality", "CreateView", "lakehouse", "metrics", "payroll") is True
    assert asks(c, "cairn-quality", "DropView", "lakehouse", "metrics", "payroll") is True
    # Nothing else there, and nothing in a catalog no connection of its signs in to.
    for op in ("SetViewAuthorization", "CreateTable", "DropTable", "SetViewComment"):
        assert asks(c, "cairn-quality", op, "lakehouse", "metrics", "payroll") is False, op
    assert asks(c, "cairn-quality", "RenameView", "lakehouse", "metrics", "payroll", ("lakehouse", "metrics", "p2")) is False
    assert asks(c, "cairn-quality", "CreateView", "lake2", "metrics", "payroll") is False
    # A connection's own service_user is the one.
    s.data_access.registry = ConnectionRegistry({"lake": {
        "url": "trino://trino.test:8443/lakehouse/sales?protocol=https", "run_as_user": True,
        "service_user": "urn:li:corpuser:cairn-metrics"}})
    assert asks(c, "cairn-metrics", "CreateView", "lakehouse", "metrics", "payroll") is True
    assert asks(c, "cairn-quality", "CreateView", "lakehouse", "metrics", "payroll") is False


def test_a_location_that_cannot_be_looked_up_is_changed_by_nobody(guarded):
    c, s = guarded, guarded.services

    def broken(catalog, schema):
        raise RuntimeError("registry unreadable")
    s.metric_view_writers = broken
    assert asks(c, "cairn-quality", "CreateView", "lakehouse", "metrics", "payroll") is False
    assert asks(c, "admin", "CreateView", "lakehouse", "sales", "v") is False


@pytest.fixture
def ran(guarded, monkeypatch):
    """Statements Cairn runs on Trino, with the identity each runs as (no Trino here)."""
    s = guarded.services
    out: list[tuple[str, str]] = []

    def run_on_trino(definition, actor, sql, limit):
        out.append((actor, sql))
        return ["amount"], [], False
    monkeypatch.setattr(s.data_access, "run_on_trino", run_on_trino)
    monkeypatch.setattr(s.workbooks, "columns_of", lambda actor, connection, table: ["amount"])
    with s.engine.begin() as conn:
        conn.execute(insert(metrics).values(
            id=uuid.uuid4().hex, name="bobs", label="Bob's", description="", connection="lake",
            table_name="sales.orders", measure="Sum([amount])", filters="[]", time_column=None, dimensions="[]",
            owner=BOB, certified=False, version=1, updated_ms=int(time.time() * 1000), published=False, synonyms="[]"))
    return out


def test_publish_makes_the_view_as_cairns_service_identity_after_checking_the_person(guarded, ran):
    c = guarded
    admin = {"X-Cairn-Actor": ADMIN}
    r = c.post("/sql/metrics/payroll/publish", headers=admin)
    assert r.status_code == 200, r.text
    views = [(who, sql) for who, sql in ran if "VIEW" in sql]
    assert [who for who, _ in views] == ["urn:li:corpuser:cairn-quality"], ran
    assert views[0][1].startswith("CREATE OR REPLACE VIEW metrics.payroll SECURITY INVOKER AS SELECT"), views
    # The person is checked first: Bob owns his metric but may not make views in the metrics schema.
    ran.clear()
    r = c.post("/sql/metrics/bobs/publish", headers={"X-Cairn-Actor": BOB})
    assert r.status_code == 403 and "MANAGE_DATASET" in r.text, r.text
    assert not [sql for _, sql in ran if "VIEW" in sql], ran
    assert c.post("/sql/metrics/payroll/publish", headers={"X-Cairn-Actor": BOB}).status_code == 403   # not his
    # Unpublishing drops it as the service identity too.
    ran.clear()
    assert c.delete("/sql/metrics/payroll/publish", headers=admin).status_code == 204
    assert ran == [("urn:li:corpuser:cairn-quality", "DROP VIEW IF EXISTS metrics.payroll")]


def test_without_cairn_as_trinos_access_control_nothing_is_published(guarded, ran):
    c, s = guarded, guarded.services
    s.trino_access = None
    r = c.post("/sql/metrics/payroll/publish", headers={"X-Cairn-Actor": ADMIN})
    assert r.status_code == 503, r.text
    assert not [sql for _, sql in ran if "VIEW" in sql], ran
