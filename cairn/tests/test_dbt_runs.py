"""How a dbt run's artifacts merge into Cairn's metadata (cairn.pipelines.dbt): docs only where there are none,
tags and owners added, dbt's lineage replaced on each run while other lineage stays, nodes outside the run
left alone."""

import json

import pytest

from cairn.common.core import InMemoryGraph
from cairn.pipelines.dbt import DbtError, DbtRuns


def urn_of(db, schema, table):
    return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{schema}.{table},PROD)"


class Quality:
    def __init__(self):
        self.calls = []

    def ingest(self, tool, body, platform, dataset, actor, urn_of=None):
        self.calls.append((tool, platform, actor, urn_of))
        return {"checks": len([r for r in body["run_results"]["results"] if r["unique_id"].startswith("test.")])}


class Audit:
    def __init__(self):
        self.entries = []

    def record(self, *a):
        self.entries.append(a)


def manifest(model_doc="Orders per region.", deps=("source.shop.sales.orders",), tags=("finance",), owner="ana"):
    return {
        "metadata": {"adapter_type": "trino", "project_name": "shop", "invocation_id": "inv-1"},
        "nodes": {
            "model.shop.by_region": {"database": "lake", "schema": "analytics", "name": "by_region", "alias": "by_region",
                                     "description": model_doc, "tags": list(tags), "package_name": "shop",
                                     "original_file_path": "models/by_region.sql",
                                     "config": {"materialized": "table", "meta": {"owner": owner} if owner else {}},
                                     "columns": {"region": {"description": "The region."}},
                                     "depends_on": {"nodes": list(deps)}},
            "model.shop.other": {"database": "lake", "schema": "analytics", "name": "other", "config": {},
                                 "depends_on": {"nodes": []}},
            "test.shop.not_null_by_region_region.1": {"depends_on": {"nodes": ["model.shop.by_region"]}, "name": "nn"},
        },
        "sources": {"source.shop.sales.orders": {"database": "lake", "schema": "sales", "name": "orders", "identifier": "orders"}},
    }


RESULTS = {"metadata": {"generated_at": "2026-11-01T10:00:00Z"},
           "results": [{"unique_id": "model.shop.by_region", "status": "success"},
                       {"unique_id": "test.shop.not_null_by_region_region.1", "status": "pass"}]}
MODEL = urn_of("lake", "analytics", "by_region")
ORDERS = urn_of("lake", "sales", "orders")


@pytest.fixture
def runs():
    g = InMemoryGraph()
    return DbtRuns(g, Quality(), Audit(), urn_of), g


def test_a_run_lands_docs_tags_owners_lineage_and_tests(runs):
    r, g = runs
    out = r.ingest({"manifest": manifest(), "run_results": RESULTS}, "urn:li:corpuser:svc-dbt")
    assert out["models"] == 1 and out["lineage_edges"] == 1 and out["tests"] == {"checks": 1}  # "other" was not run
    props = g.get_aspect(MODEL, "datasetProperties")
    assert props["description"] == "Orders per region."
    assert props["customProperties"]["dbt.unique_id"] == "model.shop.by_region"
    assert props["customProperties"]["dbt.last_status"] == "success"
    assert g.get_aspect(urn_of("lake", "analytics", "other"), "datasetProperties") is None
    assert g.get_aspect(MODEL, "globalTags") == {"tags": [{"tag": "urn:li:tag:finance"}]}
    assert g.get_aspect(MODEL, "ownership")["owners"] == [{"owner": "urn:li:corpuser:ana", "type": "TECHNICAL_OWNER"}]
    assert [u["dataset"] for u in g.get_aspect(MODEL, "upstreamLineage")["upstreams"]] == [ORDERS]
    assert r.external.calls[0][0] == "dbt" and r.external.calls[0][3] is urn_of
    assert r.audit.entries[0][1] == "dbt.run_recorded" and r.audit.entries[0][2] == "urn:li:corpuser:svc-dbt"


def test_docs_never_overwrite_and_tags_owners_only_add(runs):
    r, g = runs
    g.aspects[(MODEL, "datasetProperties")] = {"name": "by_region", "description": "Written by a person.",
                                               "customProperties": {"trino_table": "by_region"}}
    g.aspects[(MODEL, "schemaMetadata")] = {"fields": [{"fieldPath": "region"}, {"fieldPath": "n", "description": "kept"}]}
    g.aspects[(MODEL, "globalTags")] = {"tags": [{"tag": "urn:li:tag:pii"}]}
    g.aspects[(MODEL, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:bo", "type": "DATAOWNER"}]}
    r.ingest({"manifest": manifest(), "run_results": RESULTS}, "urn:li:corpuser:svc-dbt")
    props = g.get_aspect(MODEL, "datasetProperties")
    assert props["description"] == "Written by a person." and props["customProperties"]["trino_table"] == "by_region"
    fields = {f["fieldPath"]: f.get("description") for f in g.get_aspect(MODEL, "schemaMetadata")["fields"]}
    assert fields == {"region": "The region.", "n": "kept"}
    assert [t["tag"] for t in g.get_aspect(MODEL, "globalTags")["tags"]] == ["urn:li:tag:pii", "urn:li:tag:finance"]
    assert [o["owner"] for o in g.get_aspect(MODEL, "ownership")["owners"]] == ["urn:li:corpuser:bo", "urn:li:corpuser:ana"]


def test_dbt_lineage_is_replaced_and_other_lineage_kept(runs):
    r, g = runs
    elsewhere = {"dataset": "urn:li:dataset:(urn:li:dataPlatform:kafka,clicks,PROD)", "type": "TRANSFORMED"}
    g.aspects[(MODEL, "upstreamLineage")] = {"upstreams": [elsewhere]}
    r.ingest({"manifest": manifest(), "run_results": RESULTS}, "a")
    assert len(g.get_aspect(MODEL, "upstreamLineage")["upstreams"]) == 2
    r.ingest({"manifest": manifest(deps=()), "run_results": RESULTS}, "a")  # the ref was removed
    assert g.get_aspect(MODEL, "upstreamLineage")["upstreams"] == [elsewhere]


def test_an_exposure_links_only_to_a_web_address(runs):
    """An exposure's url becomes the dashboard's link in Cairn: only http(s), never javascript: or data:."""
    r, g = runs
    m = manifest()
    m["exposures"] = {f"exposure.shop.e{i}": {"name": f"e{i}", "url": url, "depends_on": {"nodes": ["model.shop.by_region"]}}
                      for i, url in enumerate(["javascript:alert(document.cookie)", " JavaScript:alert(1)",
                                               "data:text/html,<script>alert(1)</script>", "https://bi.example.com/d/1"])}
    r.ingest({"manifest": m, "run_results": RESULTS}, "a")
    links = [g.get_aspect(f"urn:li:dashboard:(dbt,shop.e{i})", "dashboardInfo").get("externalUrl") for i in range(4)]
    assert links == [None, None, None, "https://bi.example.com/d/1"]


def test_bad_artifacts_are_refused(runs):
    r, _ = runs
    with pytest.raises(DbtError):
        r.ingest({"run_results": RESULTS}, "a")
    with pytest.raises(DbtError):
        r.ingest({"manifest": {"nodes": "nope"}}, "a")


def test_the_runner_writes_a_private_jwt_profile(tmp_path, monkeypatch):
    from cairn.pipelines import dbt_runner

    monkeypatch.setenv("CAIRN_TRINO_HOST", "trino.example.com")
    monkeypatch.setenv("CAIRN_CA_FILE", "/etc/ca.pem")
    p = dbt_runner.profile("shop", {"token": "t0k", "user": "svc-dbt"})
    out = p["shop"]["outputs"]["cairn"]
    got = (out["method"], out["jwt_token"], out["user"], out["cert"], out["http_scheme"])
    assert got == ("jwt", "t0k", "svc-dbt", "/etc/ca.pem", "https")
    assert json.dumps(p)  # serializable for profiles.yml


# The published docs site: HTML one principal wrote, served to every viewer.
DBT_INDEX = ('<html><body><script>var n = { manifest: "MANIFEST.JSON INLINE DATA", '
             'catalog: "CATALOG.JSON INLINE DATA" };</script></body></html>')
PRIVS = {"urn:li:corpuser:ana": {"EDIT_LINEAGE", "VIEW_ENTITY_PAGE"},
         "urn:li:corpuser:mallory": {"EDIT_LINEAGE", "VIEW_ENTITY_PAGE"},
         "urn:li:corpuser:ops": {"EDIT_LINEAGE", "MANAGE_INGESTION", "VIEW_ENTITY_PAGE"},
         "urn:li:corpuser:bob": set()}


@pytest.fixture
def docs_api(runs):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from cairn.common.db import metadata
    from cairn.pipelines.dbt import build_dbt_router

    r, _ = runs
    r.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(r.engine)
    r.audit.record = lambda *a, **k: r.audit.entries.append(a)

    class P:
        def __init__(self, urn):
            self.urn = urn

    def current(request: Request):
        return P(request.headers["x-actor"])

    def authorize(p, priv, resource):
        if priv not in PRIVS[p.urn] or (priv == "VIEW_ENTITY_PAGE" and resource == ORDERS
                                        and p.urn != "urn:li:corpuser:ana"):
            raise HTTPException(403, "no")

    app = FastAPI()
    app.include_router(build_dbt_router(r, current, authorize))
    return TestClient(app), r


def _publish(client, who, html=DBT_INDEX, model_doc="Orders per region."):
    return client.post("/pipelines/dbt/docs", headers={"x-actor": f"urn:li:corpuser:{who}"},
                       json={"project": "shop", "index_html": html, "manifest": manifest(model_doc=model_doc),
                             "catalog": {"nodes": {}, "sources": {}}})


def _page(client, who):
    return client.get("/pipelines/dbt/docs/shop/", headers={"x-actor": f"urn:li:corpuser:{who}"})


def test_docs_run_sandboxed_with_the_viewers_filtered_data_inlined(docs_api):
    client, _ = docs_api
    assert _publish(client, "ana").status_code == 200
    page = _page(client, "mallory")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "sandbox" in csp and "allow-scripts" in csp and "allow-same-origin" not in csp, csp
    assert "MANIFEST.JSON INLINE DATA" not in page.text and "model.shop.by_region" in page.text
    assert "\"source.shop.sales.orders\": {" not in page.text  # mallory may not see the source
    assert "\"source.shop.sales.orders\": {" in _page(client, "ana").text
    # A description cannot close the script element it is inlined into.
    assert _publish(client, "ana", model_doc="</script><script>alert(1)</script>").status_code == 200
    page = _page(client, "ana").text
    assert page.count("</script>") == 1 and "alert(1)" in page


def test_only_the_publisher_or_an_operator_replaces_a_projects_docs(docs_api):
    client, r = docs_api
    assert _publish(client, "ana").status_code == 200
    evil = _publish(client, "mallory", "<html><script>fetch('/x', {method: 'POST'})</script></html>")
    assert evil.status_code == 403, evil.text
    assert "fetch(" not in _page(client, "ana").text
    assert _publish(client, "ana").status_code == 200
    assert _publish(client, "ops").status_code == 200  # a pipeline operator may; ana stays the publisher
    assert _publish(client, "ana").status_code == 200
    assert _publish(client, "bob").status_code == 403  # no EDIT_LINEAGE at all
    assert r.audit.entries[-1][1] == "dbt.docs_published"
