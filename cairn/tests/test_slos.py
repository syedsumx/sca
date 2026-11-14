"""Data product SLOs: the SLI counts the product's monitor evaluations of the SLO's kind in the window,
the error budget and burn rate follow from the objective, and owners hear about state changes once."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.marketplace.slos import SLOIn
from cairn.quality.models import Evaluation, MonitorSpec
from cairn.services import build_services

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
PRODUCT = "urn:li:dataProduct:orders"
DS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,shop.orders,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,shop.other,PROD)"
HOUR, DAY = 3_600_000, 86_400_000
NOW = int(time.time() * 1000)


def evaluate(store, monitor, statuses_and_ages):
    for status, age in statuses_and_ages:
        ts = NOW - age
        store.add_evaluation(Evaluation(id=f"ev_{uuid.uuid4().hex[:8]}", monitor_id=monitor.id, ts="", ts_ms=ts,
                                        run_id=uuid.uuid4().hex, metric_value=1.0, expected_low=None, expected_high=None,
                                        status=status, score=None, message=""), None)


@pytest.fixture
def stack(settings, graph):
    graph.aspects[(PRODUCT, "dataProductProperties")] = {"assets": [{"destinationUrn": DS}]}
    graph.aspects[(PRODUCT, "ownership")] = {"owners": [{"owner": ANA}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    store = s.quality.store
    fresh = store.create_monitor(MonitorSpec(name="fresh", dataset_urn=DS, type="freshness",
                                             config={"source": "catalog", "max_age_hours": 24}), ADMIN)
    vol = store.create_monitor(MonitorSpec(name="rows", dataset_urn=DS, type="volume", config={"source": "catalog"}), ADMIN)
    elsewhere = store.create_monitor(MonitorSpec(name="x", dataset_urn=OTHER, type="freshness",
                                                 config={"source": "catalog", "max_age_hours": 24}), ADMIN)
    # 99 good and 1 bad freshness checks in the window, one more bad one long ago (outside it).
    evaluate(store, fresh, [("PASS", i * HOUR) for i in range(2, 101)] + [("FAIL", 30 * HOUR), ("FAIL", 40 * DAY),
                                                                           ("TRAINING", HOUR)])
    evaluate(store, vol, [("PASS", 2 * HOUR), ("FAIL", 3 * HOUR)])
    evaluate(store, elsewhere, [("FAIL", HOUR)] * 5)
    return s


def test_sli_budget_and_states(stack):
    slos = stack.slos
    met = slos.status(SLOIn(id="fresh", product_urn=PRODUCT, name="Fresh", kind="freshness", objective=98, window_days=28),
                      NOW)
    assert (met["good"], met["total"], met["sli"], met["state"]) == (99, 100, 99.0, "met")
    assert met["budget_remaining"] == 50.0 and met["burn_rate"] == 0.0     # 1% of a 2% budget used, none today
    risky = slos.status(SLOIn(id="f2", product_urn=PRODUCT, name="Fresh", kind="freshness", objective=98.9), NOW)
    assert risky["state"] == "at_risk" and risky["budget_remaining"] < 25
    broken = slos.status(SLOIn(id="vol", product_urn=PRODUCT, name="Rows", kind="volume", objective=99), NOW)
    assert broken["sli"] == 50.0 and broken["state"] == "breached" and broken["burn_rate"] == 50.0
    empty = slos.status(SLOIn(id="qual", product_urn=PRODUCT, name="Quality", kind="quality", objective=99), NOW)
    assert empty["state"] == "no_data" and empty["sli"] is None


def test_owners_told_on_changes_and_api(stack):
    s = stack
    c = TestClient(create_api_app(s))
    body = {"id": "orders-rows", "product_urn": PRODUCT, "name": "Order rows arrive", "kind": "volume", "objective": 99}
    assert c.put("/marketplace/slos/orders-rows", json=body, headers={"X-Cairn-Actor": BOB}).status_code == 403
    r = c.put("/marketplace/slos/orders-rows", json=body, headers={"X-Cairn-Actor": ANA})
    assert r.status_code == 200 and r.json()["state"] == "breached", r.text
    assert s.slos.check(NOW)["told"] == 1 and s.slos.check(NOW)["told"] == 0           # once per change
    told = [n for n in s.notify.inbox(ANA) if n["type"] == "product.slo_changed"]
    assert told and "is breached" in told[0]["title"] and "50.0%" in told[0]["body"]
    listed = c.get("/marketplace/slos", params={"product": PRODUCT}, headers={"X-Cairn-Actor": BOB}).json()
    assert [x["id"] for x in listed] == ["orders-rows"]
    assert c.get("/marketplace/slos", headers={"X-Cairn-Actor": BOB}).json()[0]["state"] == "breached"
    assert c.delete("/marketplace/slos/orders-rows", headers={"X-Cairn-Actor": ANA}).status_code == 204
