"""Semantic model autopilot: accepting a draft checks what it is given (review finding on L93). The drafting itself is
tested on real Trino (tests/test_trino_e2e.py)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, insert

from cairn.common.db import init_db
from cairn.sqlworkspace.autopilot import Autopilot, AutopilotError, semantic_drafts

ANA = "urn:li:corpuser:ana"


class Metrics:
    def __init__(self):
        self.saved = []

    def save(self, actor, m):
        self.saved.append(m)
        return {"name": m["name"]}


@pytest.fixture
def pilot(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    model = {"table_meta": {"sales.orders": {"time_column": None, "dimensions": []}}, "relationships": [],
             "metrics": [{"name": "orders_count", "label": "Orders", "description": "", "table": "sales.orders",
                          "measure": "Count()", "filters": [], "synonyms": []}]}
    with engine.begin() as c:
        c.execute(insert(semantic_drafts).values(id="d1", owner=ANA, connection="lake", tables='["sales.orders"]',
                                                 model=json.dumps(model), refused="[]", status="draft", created_ms=1,
                                                 accepted="[]"))
    m = Metrics()
    return Autopilot(engine, None, m, None, lambda c, t: None), m


def test_edits_that_are_not_objects_are_refused_not_a_500(pilot):
    """Review finding: an edit that was not an object (``{"orders_count": "x"}``) failed with an AttributeError (500)."""
    a, m = pilot
    for edits in ({"orders_count": "renamed"}, {"orders_count": ["x"]}, {"orders_count": 3}):
        with pytest.raises(AutopilotError) as e:
            a.accept(ANA, "d1", {"edits": edits})
        assert e.value.status == 400
    assert m.saved == []
    assert a.accept(ANA, "d1", {"edits": {"orders_count": {"label": "All orders"}}})["saved"] == ["orders_count"]
    assert m.saved[0]["label"] == "All orders"
