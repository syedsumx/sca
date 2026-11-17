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


def _ed(users):
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    clients = {"portal": {"secret": SECRET, "origins": ["https://portal.example.com"], "dashboards": ["d1"],
                          "users": users}}
    return EmbeddedDashboards(engine, _Boards(), clients, session_ttl=60)


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
