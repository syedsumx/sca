"""Embedded dashboards' tokens and sessions (the page and its tiles run on real Trino in tests/test_trino_e2e.py)."""

import time

import pytest
from sqlalchemy import create_engine

from cairn.common.db import metadata
from cairn.embed.dashboards import EmbeddedDashboards, EmbedRefused, sign

SECRET = b"s" * 40


class _Boards:
    def _granted(self, did):
        return {"id": did, "name": "Sales"}


def _ed(users, user_status=None):
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    clients = {"portal": {"secret": SECRET, "origins": ["https://portal.example.com"], "dashboards": ["d1"],
                          "users": users}}
    return EmbeddedDashboards(engine, _Boards(), clients, session_ttl=60, user_status=user_status)


def _token(sub, jti="jti-00000001"):
    return sign(SECRET, {"client": "portal", "sub": sub, "dashboard": "d1", "origin": "https://portal.example.com",
                         "exp": int(time.time()) + 60, "jti": jti})


def test_a_portal_user_gets_a_session_for_their_dashboard():
    ed = _ed(["urn:li:corpuser:portal-*"])
    s = ed.open(_token("urn:li:corpuser:portal-ana"), "d1")
    assert ed.session(s["session"], "d1")["sub"] == "urn:li:corpuser:portal-ana"
    with pytest.raises(EmbedRefused):
        ed.open(_token("urn:li:corpuser:portal-ana"), "d1")                    # the jti was used


@pytest.mark.parametrize("sub", ["urn:li:corpuser:cairn-observability", "urn:li:corpuser:Cairn-Observability",
                                 "urn:li:corpuser:cleanroom-abc", "urn:li:corpuser:cairnapp-x"])
def test_a_portal_never_signs_for_cairns_own_identities(sub):
    # Even a client allowed every user ("*") cannot act as an identity Cairn keeps for itself.
    ed = _ed(["urn:li:corpuser:*"])
    with pytest.raises(EmbedRefused):
        ed.open(_token(sub), "d1")


def test_a_suspended_user_neither_starts_nor_keeps_an_embed_session():
    status = {"urn:li:corpuser:portal-ana": "ACTIVE", "urn:li:corpuser:portal-bob": "SUSPENDED"}
    ed = _ed(["urn:li:corpuser:portal-*"], user_status=lambda u: status[u])
    with pytest.raises(EmbedRefused):
        ed.open(_token("urn:li:corpuser:portal-bob"), "d1")
    s = ed.open(_token("urn:li:corpuser:portal-ana", "jti-00000002"), "d1")
    assert ed.session(s["session"], "d1")["sub"] == "urn:li:corpuser:portal-ana"
    status["urn:li:corpuser:portal-ana"] = "SUSPENDED"                       # deactivated (SCIM, an administrator)
    with pytest.raises(EmbedRefused):
        ed.session(s["session"], "d1")


def test_an_embed_session_fails_closed_when_the_users_state_cannot_be_read():
    state = {"up": True}

    def user_status(u):
        if not state["up"]:
            raise ConnectionError("core is down")
        return "ACTIVE"
    ed = _ed(["urn:li:corpuser:portal-*"], user_status=user_status)
    s = ed.open(_token("urn:li:corpuser:portal-ana"), "d1")
    state["up"] = False
    with pytest.raises(EmbedRefused):
        ed.session(s["session"], "d1")
    with pytest.raises(EmbedRefused):
        ed.open(_token("urn:li:corpuser:portal-ana", "jti-00000003"), "d1")
