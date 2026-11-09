"""Cairn Desktop's workspace settings (``/v1/desktop/policy``, ``/admin/desktop/policy``), over the real API.

People read them (the desktop app asks with their session); only administrators (``MANAGE_GLOBAL_SETTINGS``) change
them; changes are checked and audited. The desktop app's side (what it does with them) is tested in
``desktop/test`` (unit) and ``tests/test_desktop_e2e.py`` (the real app)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"
ANA = "urn:li:corpuser:ana"


@pytest.fixture
def api(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="people", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    return s, TestClient(create_api_app(s))


def test_defaults_change_and_audit(api):
    s, c = api
    admin, ana = {"X-Cairn-Actor": ADMIN}, {"X-Cairn-Actor": ANA}
    got = c.get("/v1/desktop/policy", headers=ana)
    assert got.status_code == 200
    assert {k: got.json()[k] for k in ("offline_cache", "offline_cache_pages", "offline_cache_max_age_days", "updates",
                                       "pinned_version", "update_feed_url", "sign_in_origins")} == {
        "offline_cache": True, "offline_cache_pages": 20, "offline_cache_max_age_days": 14, "updates": "auto",
        "pinned_version": None, "update_feed_url": None, "sign_in_origins": []}
    assert c.get("/v1/desktop/policy").status_code == 401           # people only: signed in

    # Only administrators change it (or read the admin view).
    assert c.put("/admin/desktop/policy", json={"offline_cache": False}, headers=ana).status_code == 403
    assert c.get("/admin/desktop/policy", headers=ana).status_code == 403
    body = {"offline_cache": False, "offline_cache_pages": 5, "updates": "notify", "pinned_version": "v1.2.3",
            "update_feed_url": "https://updates.example.com/cairn/", "sign_in_origins": ["https://Login.Example.com/x"]}
    put = c.put("/admin/desktop/policy", json=body, headers=admin)
    assert put.status_code == 200, put.text
    seen = c.get("/v1/desktop/policy", headers=ana).json()
    assert seen["offline_cache"] is False and seen["offline_cache_pages"] == 5 and seen["updates"] == "notify"
    assert seen["pinned_version"] == "1.2.3" and seen["update_feed_url"] == "https://updates.example.com/cairn"
    assert seen["sign_in_origins"] == ["https://login.example.com"] and seen["changed_by"] == ADMIN
    assert seen["offline_cache_max_age_days"] == 14                  # not given: the default
    entries = [e for e in s.audit.entries("identity", 20) if e.get("action") == "desktop.policy_changed"]
    assert entries and entries[0]["actor"] == ADMIN

    # A PUT states the whole policy: what it leaves out goes back to the default.
    assert c.put("/admin/desktop/policy", json={"updates": "off"}, headers=admin).json()["offline_cache"] is True


@pytest.mark.parametrize("bad", [
    {"offline_cache": "no"}, {"offline_cache_pages": 0}, {"offline_cache_pages": 101}, {"offline_cache_pages": True},
    {"offline_cache_max_age_days": 91}, {"updates": "sometimes"}, {"pinned_version": "latest"},
    {"update_feed_url": "http://updates.example.com"}, {"update_feed_url": "https://u:p@updates.example.com"},
    {"sign_in_origins": "https://login.example.com"}, {"sign_in_origins": ["http://login.example.com"]},
    {"surprise": 1}, [],
])
def test_bad_settings_are_refused(api, bad):
    s, c = api
    r = c.put("/admin/desktop/policy", json=bad, headers={"X-Cairn-Actor": ADMIN})
    assert r.status_code == 422, (bad, r.text)
    assert c.get("/v1/desktop/policy", headers={"X-Cairn-Actor": ANA}).json()["changed_by"] is None
