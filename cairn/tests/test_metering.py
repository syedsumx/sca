"""Metering's bookkeeping on the control plane, without Stripe (tests/test_metering_e2e.py has stripe-mock)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from cairn.common.db import get_engine, init_db
from cairn.hosting.metering import Metering, sign, tenant_key, tenant_usage


class FakeStripe:
    def __init__(self):
        self.events = []

    def post(self, path, data, idempotency_key=None):
        self.events.append(data)
        return {}


def report(m, master, tenant, day, metrics):
    body = json.dumps({"tenant": tenant, "day": day, "metrics": metrics}).encode()
    return m.receive(body, sign(tenant_key(master, tenant), body))


def test_an_empty_metering_key_stops_the_control_plane(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    with pytest.raises(ValueError, match="empty"):
        Metering(engine, "", {}, FakeStripe(), lambda t: "cus_1")
    with pytest.raises(ValueError, match="empty"):
        tenant_key("", "acme")


def test_a_settled_days_usage_is_what_gets_billed(tmp_path):
    """bill() sends settled days; a report arriving for one meanwhile must not change the value it sent."""
    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    stripe = FakeStripe()
    m = Metering(engine, "master", {"query_cpu_seconds": "cpu"}, stripe, lambda t: "cus_1")
    day = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()      # long settled
    assert report(m, "master", "acme", day, {"query_cpu_seconds": 100})["kept"] == 1
    assert report(m, "master", "acme", day, {"query_cpu_seconds": 999})["kept"] == 0
    assert m.bill() == 1 and stripe.events[0]["payload"]["value"] == "100"
    with engine.connect() as conn:
        assert conn.execute(select(tenant_usage.c.value)).scalar() == 100
    # A day still settling takes the latest report.
    m.settle_hours = 24 * 5
    assert report(m, "master", "acme", day, {"warehouse_node_seconds": 1})["kept"] == 1
    assert report(m, "master", "acme", day, {"warehouse_node_seconds": 2})["kept"] == 1
