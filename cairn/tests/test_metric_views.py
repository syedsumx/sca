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
