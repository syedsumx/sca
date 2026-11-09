"""Command-line sign-in (cairn.identity.device) on a real database: the device flow's answers, rotation and reuse of
refresh tokens, sessions ending, suspended people, SCIM deactivation, and the principal a CLI token resolves to."""

import pytest
from sqlalchemy import update

from cairn.common.audit import AuditLog
from cairn.common.security import IdentityResolver
from cairn.identity.agents import agent_tokens
from cairn.identity.device import CLIENT_ID, DeviceError, DeviceLogins, normalize_user_code

ANA = "urn:li:corpuser:ana"


@pytest.fixture
def svc(engine, monkeypatch):
    monkeypatch.setenv("CAIRN_DEVICE_POLL_SECONDS", "1")
    suspended: set = set()
    d = DeviceLogins(engine, AuditLog(engine), "https://cairn.example.com/cairn/device",
                     lambda a: "suspended" if a in suspended else None)
    d.suspended = suspended
    return d


def error_of(fn, *a) -> str:
    with pytest.raises(DeviceError) as e:
        fn(*a)
    return e.value.oauth_error


def test_the_flow_and_its_answers(svc):
    start = svc.start(CLIENT_ID, "laptop <script>")
    assert start["verification_uri_complete"] == f"https://cairn.example.com/cairn/device?code={start['user_code']}"
    assert len(normalize_user_code(start["user_code"])) == 8 and start["expires_in"] == 600
    assert error_of(svc.start, "someone-else") == "invalid_client"
    assert error_of(svc.poll, start["device_code"], CLIENT_ID) == "authorization_pending"
    assert error_of(svc.poll, start["device_code"], CLIENT_ID) == "slow_down"      # sooner than the interval
    assert svc.describe(ANA, start["user_code"])["name"] == "laptop script"        # what the device said, cleaned
    assert error_of(svc.describe, ANA, "BCDF-GHJK") == "invalid_grant"
    svc.decide(ANA, start["user_code"], True)
    assert error_of(svc.decide, ANA, start["user_code"], True) == "invalid_grant"   # once only
    import time

    time.sleep(1.1)
    tokens = svc.poll(start["device_code"], CLIENT_ID)
    assert tokens["access_token"].startswith("cla_") and tokens["refresh_token"].startswith("clr_")
    assert tokens["expires_in"] == 3600 and tokens["refresh_expires_in"] == 30 * 86400
    assert error_of(svc.poll, start["device_code"], CLIENT_ID) == "invalid_grant"   # a device code works once
    grant = svc.resolve(tokens["access_token"])
    assert grant.actor == ANA and grant.session == tokens["session"]
    # The principal a CLI token is: the person, via "cli", the device's session.
    ir = IdentityResolver("http://core.invalid", None, None)
    ir.cli_lookup = svc.resolve
    p = ir.resolve({"authorization": f"Bearer {tokens['access_token']}"}, {})
    assert (p.urn, p.via, p.run) == (ANA, "cli", tokens["session"])
    from cairn.common.security import AuthenticationError

    with pytest.raises(AuthenticationError, match="cairn login"):           # never sent on to Cairn Core
        ir.resolve({"authorization": f"Bearer {tokens['refresh_token']}"}, {})


def test_denied_expired_and_wrong_codes(svc):
    a = svc.start(CLIENT_ID)
    svc.decide(ANA, a["user_code"], False)
    assert error_of(svc.poll, a["device_code"], CLIENT_ID) == "access_denied"
    b = svc.start(CLIENT_ID)
    with svc.engine.begin() as c:
        c.execute(update(agent_tokens).where(agent_tokens.c.kind.in_(("cli_dev", "cli_uc"))).values(expires_ms=1))
    assert error_of(svc.poll, b["device_code"], CLIENT_ID) == "expired_token"
    assert error_of(svc.decide, ANA, b["user_code"], True) == "invalid_grant"
    svc.lookups = 3
    for _ in range(3):
        assert error_of(svc.describe, "urn:li:corpuser:mallory", "ZZZZ-ZZZZ") == "invalid_grant"
    assert error_of(svc.describe, "urn:li:corpuser:mallory", "ZZZZ-ZZZZ") == "slow_down"   # guessing is throttled
    svc.starts = 2                                                        # the public endpoint, per address
    svc.start(CLIENT_ID, "x", "203.0.113.9")
    svc.start(CLIENT_ID, "x", "203.0.113.9")
    assert error_of(svc.start, CLIENT_ID, "x", "203.0.113.9") == "slow_down"
    svc.start(CLIENT_ID, "x", "203.0.113.10")
    svc.suspended.add("urn:li:corpuser:sam")
    c = svc.start(CLIENT_ID, "x", "198.51.100.1")
    assert error_of(svc.decide, "urn:li:corpuser:sam", c["user_code"], True) == "access_denied"
    svc.enabled = False
    assert error_of(svc.start, CLIENT_ID) == "unauthorized_client"


def _approved(svc, actor=ANA):
    import time

    start = svc.start(CLIENT_ID, "laptop")
    svc.decide(actor, start["user_code"], True)
    time.sleep(1.05)
    return svc.poll(start["device_code"], CLIENT_ID)


def test_refresh_rotates_and_a_reused_refresh_token_ends_the_session(svc):
    first = _approved(svc)
    second = svc.refresh(first["refresh_token"], CLIENT_ID)
    assert second["session"] == first["session"] and second["refresh_token"] != first["refresh_token"]
    assert svc.resolve(second["access_token"]) is not None
    assert error_of(svc.refresh, first["refresh_token"], CLIENT_ID) == "invalid_grant"      # replayed
    assert svc.resolve(second["access_token"]) is None and svc.sessions(ANA) == []          # the session ended
    third = _approved(svc)
    svc.suspended.add(ANA)
    assert error_of(svc.refresh, third["refresh_token"], CLIENT_ID) == "invalid_grant"      # suspended since
    assert svc.resolve(third["access_token"]) is None


def test_sessions_are_listed_and_ended(svc):
    one, two = _approved(svc), _approved(svc)
    listed = svc.sessions(ANA)
    assert {d["id"] for d in listed} == {one["session"], two["session"]} and listed[0]["name"] == "laptop"
    with pytest.raises(DeviceError):
        svc.end_session(one["session"], "urn:li:corpuser:bob", "urn:li:corpuser:bob")       # not bob's
    svc.end_session(one["session"], ANA, ANA)
    assert svc.resolve(one["access_token"]) is None and svc.resolve(two["access_token"]) is not None
    assert svc.revoke_token(two["refresh_token"]) and svc.resolve(two["access_token"]) is None   # cairn logout
    assert not svc.revoke_token("cat_an-agent-token")
    three = _approved(svc)
    assert svc.end_all(ANA, "scim") >= 2 and svc.resolve(three["access_token"]) is None
    actions = [e["action"] for e in svc.audit.entries("identity", 50)]
    assert "device.signed_in" in actions and "device.signed_out" in actions and "device.all_signed_out" in actions


def test_scim_deactivation_signs_every_device_out(svc, engine, graph):
    from cairn.identity.scim import ScimService

    scim = ScimService(engine, graph, None, on_deactivate=lambda urn: svc.end_all(urn, "scim"))
    scim.create_user({"userName": "ana", "active": True})
    tokens = _approved(svc)
    scim.delete_user("ana")
    assert svc.resolve(tokens["access_token"]) is None
