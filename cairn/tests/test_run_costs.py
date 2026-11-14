"""Spark compute costs from OpenLineage runs (cairn.reports.costs.CostService.on_lineage_run), fed by the
real OpenLineage receiver with events shaped like openlineage-spark's: an application run, and a run
per query inside it that names the tables it wrote."""

import pytest
from sqlalchemy import create_engine

from cairn.common.core import InMemoryGraph
from cairn.common.db import init_db
from cairn.reports.costs import CostService, spark_cores
from cairn.sources.openlineage import OpenLineageReceiver

APP, Q1, Q2 = "0193a000-0000-7000-8000-000000000001", "0193a000-0000-7000-8000-000000000002", \
    "0193a000-0000-7000-8000-000000000003"
PROPS = {"spark.master": "k8s://https://kubernetes.default.svc", "spark.app.name": "orders",
         "spark.driver.cores": "1", "spark.executor.instances": "3", "spark.executor.cores": "2"}
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.analytics.orders,PROD)"
ITEMS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.analytics.items,PROD)"


def ev(state, run, at, name="orders", parent=None, outputs=(), engine="spark"):
    facets = {"processing_engine": {"name": engine, "version": "3.5.6"},
              "spark_properties": {"properties": PROPS}}
    if parent:
        facets["parent"] = {"run": {"runId": parent}, "job": {"namespace": "spark", "name": "orders"}}
    return {"eventType": state, "eventTime": f"2026-11-02T10:{at}Z", "run": {"runId": run, "facets": facets},
            "job": {"namespace": "spark", "name": name}, "inputs": [],
            "outputs": [{"namespace": "iceberg", "name": o} for o in outputs]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_COST_SPARK_USD_PER_CORE_HOUR", "0.05")
    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    graph = InMemoryGraph()
    costs = CostService(engine, graph, lambda u: False)
    ol = OpenLineageReceiver(graph)
    ol.on_finished = costs.on_lineage_run
    return ol, costs


def test_an_application_is_charged_once_to_what_its_queries_wrote(world):
    ol, costs = world
    ol.handle_many([
        ev("START", APP, "00:00"),
        ev("START", Q1, "01:00", "orders.append_orders", APP), ev("COMPLETE", Q1, "05:00", "orders.append_orders", APP,
                                                                  ["lakehouse.analytics.orders"]),
        ev("START", Q2, "06:00", "orders.append_items", APP), ev("COMPLETE", Q2, "09:00", "orders.append_items", APP,
                                                                 ["lakehouse.analytics.items"]),
        ev("COMPLETE", APP, "10:00"),
    ])
    # 7 cores (1 driver + 3 x 2) for 10 minutes at $0.05 a core-hour: $0.058333, half to each table.
    jobs = costs.jobs(days=3650, now_ms=1793700000000)
    assert len(jobs) == 1 and jobs[0]["runs"] == 1 and jobs[0]["seconds"] == 600.0
    assert jobs[0]["usd"] == pytest.approx(7 * 600 / 3600 * 0.05, abs=1e-4)
    for urn in (ORDERS, ITEMS):
        s = costs.asset(urn)["series"]
        assert [r["day"] for r in s] == ["2026-11-02"]
        assert s[0]["compute_usd"] == pytest.approx(7 * 600 / 3600 * 0.05 / 2, abs=1e-5)

    # A redelivered event charges nothing more.
    ol.handle(ev("COMPLETE", APP, "10:00"))
    assert costs.asset(ORDERS)["series"][0]["compute_usd"] == pytest.approx(7 * 600 / 3600 * 0.05 / 2, abs=1e-5)
    assert costs.jobs(days=3650, now_ms=1793700000000)[0]["runs"] == 1

    # A second run the same day adds to the day's figure.
    ol.handle_many([ev("START", "0193a000-0000-7000-8000-00000000000a", "20:00"),
                    ev("COMPLETE", "0193a000-0000-7000-8000-00000000000a", "21:00", outputs=["lakehouse.analytics.orders"])])
    assert costs.asset(ORDERS)["series"][0]["compute_usd"] == pytest.approx(
        7 * 600 / 3600 * 0.05 / 2 + 7 * 60 / 3600 * 0.05, abs=1e-5)


def test_only_spark_runs_are_priced_and_only_when_a_rate_is_set(world, monkeypatch):
    ol, costs = world
    ol.handle_many([ev("START", APP, "00:00", engine="Airflow"),
                    ev("COMPLETE", APP, "10:00", engine="Airflow", outputs=["lakehouse.analytics.orders"])])
    assert not costs.jobs(days=3650, now_ms=1793700000000) and not costs.asset(ORDERS)["series"]
    monkeypatch.delenv("CAIRN_COST_SPARK_USD_PER_CORE_HOUR")
    ol.handle_many([ev("START", Q1, "00:00"), ev("COMPLETE", Q1, "10:00", outputs=["lakehouse.analytics.orders"])])
    assert not costs.jobs(days=3650, now_ms=1793700000000)


def test_cores_reserved():
    assert spark_cores({"spark.master": "local[3]"}) == 3
    assert spark_cores({"spark.master": "spark://m:7077", "spark.executor.instances": "4", "spark.executor.cores": "4",
                        "spark.driver.cores": "2"}) == 18
    assert spark_cores({"spark.master": "k8s://x", "spark.dynamicAllocation.enabled": "true",
                        "spark.dynamicAllocation.maxExecutors": "10", "spark.executor.cores": "2"}) == 21
    assert spark_cores({"spark.master": "yarn"}, default=8) == 8   # nothing to go by: the configured default
