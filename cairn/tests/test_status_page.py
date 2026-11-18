"""The status page's public outputs and its incident checks, without probing anything (the probes against a real
component are in test_status_page_e2e.py)."""

import json

import pytest

from cairn.common.db import get_engine, init_db
from cairn.hosting.status import StatusError, StatusPage

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


@pytest.mark.parametrize("starts, ends", [("soon", None), (True, None), (1.5e12, None), ("1", "2"), (True, 2)])
def test_incident_times_are_whole_milliseconds(page, starts, ends):
    body = {"title": "t", "impact": "maintenance" if ends is not None else "minor", "components": ["API"],
            "message": "m", "starts_ms": starts, **({"ends_ms": ends} if ends is not None else {})}
    with pytest.raises(StatusError) as e:
        page.open_incident(OPERATOR, body)
    assert e.value.status == 400
    page.html()                                                 # the public page still renders


def test_public_summary_does_not_name_who_opened_an_incident(page):
    page.open_incident("urn:li:corpuser:alice.ops", {"title": "Slow queries", "impact": "minor",
                                                     "components": ["API"], "message": "Looking into it"})
    s = page.summary()
    assert s["incidents"] and "alice.ops" not in json.dumps(s)
    page.add_update("urn:li:corpuser:alice.ops", s["incidents"][0]["id"], {"phase": "resolved", "message": "Fixed"})
    assert "alice.ops" not in json.dumps(page.summary()) and "alice.ops" not in page.html()


def test_incident_times_must_be_integers(page):
    with pytest.raises(StatusError):
        page.open_incident("urn:li:corpuser:ops", {"title": "x", "impact": "minor", "components": ["API"],
                                                   "message": "m", "starts_ms": "soon"})
    with pytest.raises(StatusError):
        page.open_incident("urn:li:corpuser:ops", {"title": "x", "impact": "maintenance", "components": ["API"],
                                                   "message": "m", "starts_ms": True, "ends_ms": 5})
