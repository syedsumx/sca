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
