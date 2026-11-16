"""SCD type 2 and medallion layers without Trino (review findings on L110; both are tested end to end on Trino with the
real store and catalog in tests/test_scd2_medallion_e2e.py)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cairn.common.db import metadata
from cairn.common.security import Principal
from cairn.pipelines.derived import DerivedTables
from cairn.pipelines.medallion import Medallion, build_medallion_router


def test_scd2_keys_and_tracked_columns_in_any_case():
    """Review finding: Trino names columns in lower case, and the check compared the definition's key and tracked
    columns as given: a key ``CustomerId`` failed every refresh after the first with "the table has no column"."""
    def run(owner, sql):
        if "information_schema.tables" in sql:
            return [[1]]
        return [["customerid"], ["tier"], ["city"], ["valid_from"], ["valid_to"], ["is_current"]]
    dt = DerivedTables(None, run)
    d = {"target": "dims.customers", "sql": "SELECT CustomerId, Tier, City FROM raw.customers",
         "key": ["CustomerId"], "tracked": ["Tier"], "mode": "scd2", "owner": "urn:li:corpuser:ana"}
    kind, [merge] = dt.scd2(d)
    assert kind == "scd2"
    assert 'cur."tier" IS DISTINCT FROM src."tier"' in merge                    # tracked: a new version
    assert 'THEN UPDATE SET "city" = s."city"' in merge                         # untracked: in place
    assert '"tier" = s."tier"' not in merge


def test_layers_are_listed_only_for_tables_the_caller_may_see(tmp_path):
    """Review finding: ``GET /v1/medallion`` (and ``/v1/medallion/<table>``) answered anyone with every layered table,
    who set it and why it is not gold (its contract's and monitors' state), even tables hidden from them."""
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    metadata.create_all(engine)
    m = Medallion(engine)
    urn = lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)"  # noqa: E731
    m.set(urn("hr", "salaries"), "hr.salaries", "silver", "urn:li:corpuser:op")
    m.set(urn("sales", "orders"), "sales.orders", "bronze", "urn:li:corpuser:op")
    who = {"p": Principal(urn="urn:li:corpuser:cal", via="session")}

    def authorize(p, privilege, resource, raise_on_deny=True):
        return p.urn == "urn:li:corpuser:op"
    app = FastAPI()
    app.include_router(build_medallion_router(m, lambda: who["p"], authorize, urn,
                                              lambda p: (lambda u: "sales.orders" in u)))
    c = TestClient(app)
    assert [r["table"] for r in c.get("/v1/medallion").json()] == ["sales.orders"]
    assert c.get("/v1/medallion/hr.salaries").status_code == 404
    assert c.get("/v1/medallion/sales.orders").status_code == 200
    who["p"] = Principal(urn="urn:li:corpuser:op", via="session")             # who marks layers sees them all
    assert [r["table"] for r in c.get("/v1/medallion").json()] == ["hr.salaries", "sales.orders"]
