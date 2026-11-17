"""The status page's public outputs and its incident checks, without probing anything (the probes against a real
component are in test_status_page_e2e.py)."""

import json

import pytest

from cairn.common.db import get_engine, init_db
from cairn.hosting.status import StatusPage

OPERATOR = "urn:li:corpuser:alice.oncall@corp.example"


@pytest.fixture
def page(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    return StatusPage(engine, {"title": "Status", "public_url": "https://status.example",
                               "components": [{"name": "API", "url": "http://127.0.0.1:1/health"}]})


def test_public_outputs_do_not_name_the_operator(page):
    inc = page.open_incident(OPERATOR, {"title": "Slow queries", "impact": "minor", "components": ["API"],
                                        "message": "Looking into it"})
    assert inc["created_by"] == OPERATOR                        # the operators' API still says who opened it
    page.add_update(OPERATOR, inc["id"], {"phase": "resolved", "message": "Fixed"})
    page.open_incident(OPERATOR, {"title": "Outage", "impact": "major", "components": ["API"], "message": "Down"})
    public = json.dumps(page.summary()) + page.html() + page.atom("https://status.example")
    assert "alice" not in public and "created_by" not in public
    assert "Slow queries" in public and "Outage" in public
