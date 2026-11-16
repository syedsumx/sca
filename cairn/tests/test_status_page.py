"""Unit checks of the public status page: what it shows to anyone, and how incidents are validated."""

import json

import pytest
from sqlalchemy import create_engine

from cairn.hosting.status import StatusError, StatusPage, status_checks, status_incidents


@pytest.fixture
def page():
    engine = create_engine("sqlite://")
    status_checks.create(engine)
    status_incidents.create(engine)
    return StatusPage(engine, {"components": [{"name": "API", "url": "http://api.test/health"}]})


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
