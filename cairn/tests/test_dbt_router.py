"""The dbt router (cairn.pipelines.dbt.build_dbt_router): what a pipeline user may publish and what viewers get."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from cairn.common.core import InMemoryGraph  # noqa: E402
from cairn.common.db import metadata  # noqa: E402
from cairn.common.security import Principal  # noqa: E402
from cairn.pipelines import dbt as dbtmod  # noqa: E402
from cairn.pipelines.dbt import DbtRuns, build_dbt_router  # noqa: E402

DEV = "urn:li:corpuser:svc-dbt"
BOB = "urn:li:corpuser:bob"


def urn_of(db, schema, table):
    return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{schema}.{table},PROD)"


class Quality:
    def ingest(self, *a, **k):
        return {"checks": 0}


def client(grants: dict):
    """``grants``: user -> {(privilege, urn or '*')}."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine, tables=[dbtmod.dbt_runs, dbtmod.dbt_state, dbtmod.dbt_docs])
    g = InMemoryGraph()
    runs = DbtRuns(g, Quality(), None, urn_of)
    runs.engine = engine

    def principal(request: Request) -> Principal:
        return Principal(request.headers.get("x-user", BOB), "token")

    def authorize(p, privilege, urn, raise_on_deny=True):
        mine = grants.get(p.urn, set())
        if (privilege, "*") in mine or (privilege, urn) in mine:
            return True
        raise HTTPException(403, f"{p.urn} may not {privilege} {urn}")

    app = FastAPI()
    app.include_router(build_dbt_router(runs, principal, authorize))
    return TestClient(app), g


EVIL = ('<html><script>fetch("/cairn/api/v1/me")</script><script>var m = "MANIFEST.JSON INLINE DATA";'
        'var c = "CATALOG.JSON INLINE DATA";</script></html>')


def docs_manifest():
    return {"metadata": {"adapter_type": "trino", "project_name": "shop"},
            "nodes": {"model.shop.a": {"database": "lake", "schema": "analytics", "name": "a"},
                      "model.shop.secret": {"database": "lake", "schema": "hr", "name": "secret",
                                            "description": "</script><script>alert(1)</script>"}},
            "sources": {}}


def test_published_docs_run_sandboxed_with_only_the_viewers_models_inlined():
    """The docs page is HTML a pipeline user uploaded: it must never run as Cairn's origin (with the
    viewer's session). It is served sandboxed (an opaque origin, no network), with the viewer's filtered
    manifest and catalog inlined, so it needs no request back to Cairn."""
    a = urn_of("lake", "analytics", "a")
    c, _ = client({DEV: {("EDIT_LINEAGE", "*")}, BOB: {("VIEW_ENTITY_PAGE", a)}})
    r = c.post("/pipelines/dbt/docs", headers={"x-user": DEV},
               json={"project": "shop", "index_html": EVIL, "manifest": docs_manifest(),
                     "catalog": {"nodes": {"model.shop.a": {}, "model.shop.secret": {}}, "sources": {}}})
    assert r.status_code == 200, r.text
    page = c.get("/pipelines/dbt/docs/shop/", headers={"x-user": BOB})
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "sandbox" in csp and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp
    body = page.text
    assert "MANIFEST.JSON INLINE DATA" not in body and "CATALOG.JSON INLINE DATA" not in body
    assert "model.shop.a" in body and "model.shop.secret" not in body
    assert page.headers.get("x-content-type-options") == "nosniff"


def test_inlined_docs_cannot_close_their_script():
    a = urn_of("lake", "analytics", "a")
    s = urn_of("lake", "hr", "secret")
    c, _ = client({DEV: {("EDIT_LINEAGE", "*")}, BOB: {("VIEW_ENTITY_PAGE", a), ("VIEW_ENTITY_PAGE", s)}})
    c.post("/pipelines/dbt/docs", headers={"x-user": DEV},
           json={"project": "shop", "index_html": EVIL, "manifest": docs_manifest(), "catalog": {}})
    body = c.get("/pipelines/dbt/docs/shop/index.html", headers={"x-user": BOB}).text
    assert "model.shop.secret" in body
    assert "</script><script>alert(1)" not in body
