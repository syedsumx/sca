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
from cairn.common.proposals import make_mcp  # noqa: E402
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


def governance_manifest():
    """A project that builds one model in its own schema, and claims another team's PII table too."""
    node = {"package_name": "shop", "config": {"meta": {"owner": "svc-dbt"}}, "tags": ["public"],
            "deprecation_date": "2026-01-01", "description": "mine now", "depends_on": {"nodes": []}}
    return {"metadata": {"adapter_type": "trino", "project_name": "shop", "invocation_id": "inv-g"},
            "nodes": {"model.shop.a": {**node, "database": "lake", "schema": "analytics", "name": "a"},
                      "model.shop.salaries": {**node, "database": "lake", "schema": "hr", "name": "salaries"}},
            "sources": {}}


def test_a_run_governs_only_the_datasets_it_manages():
    """A pipeline user with the documented privileges (EDIT_LINEAGE and EDIT_ENTITY_ASSERTIONS to record runs,
    MANAGE_DATASET on its own schema) must not make itself owner of, tag, deprecate or describe another team's
    dataset by naming it in a crafted manifest: a tag may drive an ABAC grant or a mask, an owner approves access."""
    mine, theirs = urn_of("lake", "analytics", "a"), urn_of("lake", "hr", "salaries")
    c, g = client({DEV: {("EDIT_LINEAGE", "*"), ("EDIT_ENTITY_ASSERTIONS", "*"), ("MANAGE_DATASET", mine)}})
    hr = {"owners": [{"owner": "urn:li:corpuser:hr-lead", "type": "DATAOWNER"}]}
    g.emit([make_mcp(theirs, "ownership", hr)])
    results = {"results": [{"unique_id": "model.shop.a", "status": "success"},
                           {"unique_id": "model.shop.salaries", "status": "success"}]}
    r = c.post("/pipelines/dbt/runs", headers={"x-user": DEV},
               json={"manifest": governance_manifest(), "run_results": results})
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == [theirs]
    # Its own model: described, tagged, owned and deprecated as dbt says.
    assert g.get_aspect(mine, "globalTags") == {"tags": [{"tag": "urn:li:tag:public"}]}
    assert {"owner": "urn:li:corpuser:svc-dbt", "type": "TECHNICAL_OWNER"} in g.get_aspect(mine, "ownership")["owners"]
    assert g.get_aspect(mine, "deprecation")["deprecated"] is True
    assert g.get_aspect(mine, "datasetProperties")["description"] == "mine now"
    # The other team's table: untouched.
    assert g.get_aspect(theirs, "globalTags") is None
    assert g.get_aspect(theirs, "ownership") == hr
    assert g.get_aspect(theirs, "deprecation") is None
    assert g.get_aspect(theirs, "datasetProperties") is None


def test_a_metadata_privilege_on_a_dataset_is_enough_for_that_metadata():
    theirs = urn_of("lake", "hr", "salaries")
    c, g = client({DEV: {("EDIT_LINEAGE", "*"), ("EDIT_ENTITY_ASSERTIONS", "*"), ("EDIT_ENTITY_TAGS", theirs)}})
    r = c.post("/pipelines/dbt/runs", headers={"x-user": DEV},
               json={"manifest": governance_manifest(),
                     "run_results": {"results": [{"unique_id": "model.shop.salaries", "status": "success"}]}})
    assert r.status_code == 200, r.text
    assert g.get_aspect(theirs, "globalTags") == {"tags": [{"tag": "urn:li:tag:public"}]}
    assert g.get_aspect(theirs, "ownership") is None


MALLORY = "urn:li:corpuser:svc-other"
OPS = "urn:li:corpuser:ops"


def run_body(inv: str, schema: str):
    """A successful build of project shop whose one model lives in ``schema``."""
    return {"manifest": {"metadata": {"adapter_type": "trino", "project_name": "shop", "invocation_id": inv},
                         "nodes": {"model.shop.a": {"database": "lake", "schema": schema, "name": "a",
                                                    "depends_on": {"nodes": []}}}, "sources": {}},
            "run_results": {"results": [{"unique_id": "model.shop.a", "status": "success"}]},
            "command": "build", "environment": "prod"}


def test_a_projects_state_and_docs_stay_its_own():
    """Another pipeline user (same documented privileges) must not replace prod's state of a project it does not
    run (a deferred CI build reads unselected models from where that state says), nor the project's docs, nor
    rewrite one of its runs under its own name. An operator (MANAGE_INGESTION) may."""
    record = {("EDIT_LINEAGE", "*"), ("EDIT_ENTITY_ASSERTIONS", "*")}
    c, _ = client({DEV: record, MALLORY: record, OPS: record | {("MANAGE_INGESTION", None)}})
    assert c.post("/pipelines/dbt/runs", headers={"x-user": DEV}, json=run_body("inv-1", "analytics")).json()["state"] == "saved"
    r = c.post("/pipelines/dbt/runs", headers={"x-user": MALLORY}, json=run_body("inv-2", "mallory"))
    assert r.status_code == 200 and r.json()["state"].startswith("not saved")
    state = c.get("/pipelines/dbt/state", params={"project": "shop"}, headers={"x-user": DEV}).json()
    assert state["invocation"] == "inv-1" and state["manifest"]["nodes"]["model.shop.a"]["schema"] == "analytics"
    # Its own runs replace it.
    assert c.post("/pipelines/dbt/runs", headers={"x-user": DEV}, json=run_body("inv-3", "analytics")).json()["state"] == "saved"
    # Another identity's run is not rewritten as Mallory's.
    c.post("/pipelines/dbt/runs", headers={"x-user": MALLORY}, json=run_body("inv-3", "mallory"))
    runs = c.get("/pipelines/dbt/runs", headers={"x-user": DEV}).json()
    assert {r["invocation"] for r in runs} == {"inv-1", "inv-3"}
    # Docs: the publisher's, or an operator's.
    docs = {"project": "shop", "index_html": "<html></html>", "manifest": {"nodes": {}}, "catalog": {}}
    assert c.post("/pipelines/dbt/docs", headers={"x-user": DEV}, json=docs).status_code == 200
    assert c.post("/pipelines/dbt/docs", headers={"x-user": MALLORY}, json=docs).status_code == 403
    assert c.post("/pipelines/dbt/docs", headers={"x-user": DEV}, json=docs).status_code == 200
    assert c.post("/pipelines/dbt/docs", headers={"x-user": OPS}, json=docs).status_code == 200
    # An operator may replace a state.
    assert c.post("/pipelines/dbt/runs", headers={"x-user": OPS}, json=run_body("inv-4", "fixed")).json()["state"] == "saved"
