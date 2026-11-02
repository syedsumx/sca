"""Lakehouse decisions for engines that read files (cairn.dataaccess.lakehouse): restricted when a mask or row
filter applies, everything false on any error, and OpenLineage datasets renamed from storage paths to the
lakehouse tables their symlinks name."""

from types import SimpleNamespace

from cairn.dataaccess.lakehouse import LakehouseAccess
from cairn.dataaccess.trino import MANAGE, READ, SYSTEM, VIEW, WRITE, TrinoAccess, parse_catalogs


def access(grants, filters=(), masked=()):
    """A TrinoAccess whose decisions come from ``grants`` {(user, privilege, table)}."""
    def decide(actor, groups, privilege, urn):
        user = actor.removeprefix("urn:li:corpuser:")
        table = (urn or "").split(",")[1] if urn else None
        ok = (user, privilege, table) in grants or (user, privilege, "*") in grants
        obligations = [SimpleNamespace(type="row_filter", params={"expression": "x"})] if (user, table) in filters else []
        return SimpleNamespace(decision="ALLOW" if ok else "DENY", obligations=obligations)

    def plan_for(actor, urn):
        return SimpleNamespace(active=(actor.removeprefix("urn:li:corpuser:"), urn.split(",")[1]) in masked)

    return TrinoAccess(decide, plan_for, parse_catalogs({}))


def test_decisions_and_restrictions():
    grants = {("ana", p, "lakehouse.sales.orders") for p in (VIEW, READ)} | {
        ("ana", p, "lakehouse.analytics.t") for p in (VIEW, READ, WRITE, MANAGE)} | {("bob", READ, "lakehouse.sales.orders"),
                                                                                    ("bob", VIEW, "lakehouse.sales.orders")}
    lake = LakehouseAccess(access(grants, filters={("bob", "lakehouse.sales.orders")}, masked={("ana", "lakehouse.sales.orders")}),
                           lambda urn: [])
    out = lake.decide("ana", [{"namespace": ["sales"], "name": "orders"}, {"namespace": ["analytics"], "name": "t"},
                              {"namespace": ["hr"], "name": "salaries"}])
    assert out["schemas"] is False
    orders, t, hr = out["tables"]
    assert orders == {"view": True, "read": True, "write": False, "manage": False, "restricted": True}  # masked
    assert t == {"view": True, "read": True, "write": True, "manage": True, "restricted": False}
    assert hr == {"view": False, "read": False, "write": False, "manage": False, "restricted": False}
    assert lake.decide("bob", [{"namespace": ["sales"], "name": "orders"}])["tables"][0]["restricted"] is True  # row filter


def test_any_error_decides_nothing():
    lake = LakehouseAccess(access(set()), lambda urn: (_ for _ in ()).throw(RuntimeError("down")))
    out = lake.decide("ana", [{"namespace": ["sales"], "name": "orders"}])
    assert out["schemas"] is False and out["tables"][0]["read"] is False and out["tables"][0]["restricted"] is True

    def boom(*a):
        raise RuntimeError("pdp down")
    lake = LakehouseAccess(TrinoAccess(boom, boom, parse_catalogs({})), lambda urn: [])
    d = lake.decide("ana", [{"namespace": ["s"], "name": "t"}])["tables"][0]
    assert not (d["view"] or d["read"] or d["write"] or d["manage"])  # the policy engine is down: nothing


def test_schema_management_is_a_system_privilege():
    lake = LakehouseAccess(access({("admin", SYSTEM, "*")}), lambda urn: [])
    assert lake.decide("admin", [])["schemas"] is True


def test_spark_lineage_is_renamed_to_lakehouse_tables():
    lake = LakehouseAccess(access(set()), lambda urn: [], catalog_uris=("http://catalog:8181/",))
    sym = lambda name: {"symlinks": {"identifiers": [{"namespace": "http://catalog:8181", "name": name, "type": "TABLE"}]}}  # noqa: E731
    event = {"inputs": [{"namespace": "s3://lakehouse", "name": "warehouse/analytics/products-1", "facets": sym("analytics.products")}],
             "outputs": [{"namespace": "s3://lakehouse", "name": "warehouse/analytics/cheap-2", "facets": {
                 **sym("analytics.cheap"),
                 "columnLineage": {"fields": {"price": {"inputFields": [
                     {"namespace": "s3://lakehouse", "name": "warehouse/analytics/products-1", "field": "price"}]}}}}}],
             "job": {"namespace": "spark", "name": "j"}}
    out = lake.remap_lineage(event)
    assert (out["inputs"][0]["namespace"], out["inputs"][0]["name"]) == ("trino://", "lakehouse.analytics.products")
    assert out["outputs"][0]["name"] == "lakehouse.analytics.cheap"
    assert out["outputs"][0]["facets"]["columnLineage"]["fields"]["price"]["inputFields"][0]["name"] == "lakehouse.analytics.products"
    # Another catalog's tables, and events without symlinks, are left alone.
    other = {**event, "inputs": [{**event["inputs"][0], "facets": {"symlinks": {"identifiers": [
        {"namespace": "http://elsewhere", "name": "a.b", "type": "TABLE"}]}}}], "outputs": []}
    assert lake.remap_lineage(other) == other
    assert LakehouseAccess(access(set()), lambda urn: []).remap_lineage(event) == event


def _sessions(tmp_path, **kw):
    from sqlalchemy import create_engine

    from cairn.common.audit import AuditLog
    from cairn.common.db import init_db
    from cairn.dataaccess.lakehouse import LakehouseSessions
    from cairn.dataaccess.trino_auth import TrinoTokens

    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    audit = AuditLog(engine)
    return LakehouseSessions(TrinoTokens(engine, "k" * 32, "cairn"), engine, audit, **kw), audit, engine


def test_sessions_renew_within_their_bounds(tmp_path):
    import time

    import pytest

    from cairn.dataaccess.lakehouse import SessionRefused

    status = {"urn:li:corpuser:ana": "ACTIVE"}
    s, audit, _ = _sessions(tmp_path, user_status=lambda u: status.get(u, "ACTIVE"), ttl=600, max_session=3600)
    first = s.issue("urn:li:corpuser:ana")
    claims = s.tokens.verify(first["token"], "lakehouse")
    assert claims["sub"] == "ana" and claims["sid"] == first["session"] and claims["exp"] - claims["iat"] == 600
    sid, secret = first["credential"].split(":", 1)
    assert sid == first["session"] and secret not in str(s._session(sid))  # only its hash is kept
    # Renewed with the token (exchange) or the credential, in the same session.
    again = s.refresh(first["token"])
    c2 = s.tokens.verify(again["token"], "lakehouse")
    assert (c2["sid"], c2["sst"]) == (claims["sid"], claims["sst"])
    assert s.tokens.verify(s.with_credential(sid, secret)["token"], "lakehouse")["sid"] == sid
    with pytest.raises(SessionRefused) as e:
        s.with_credential(sid, secret + "x")
    assert e.value.error == "invalid_client"
    # Never beyond the session's end: the last token is cut to fit.
    s.max_session = int(time.time()) - claims["sst"] + 120
    assert s.refresh(again["token"])["expires_in"] <= 120
    s.max_session = 3600
    # Suspended users and ended sessions are not renewed; a new session is.
    status["urn:li:corpuser:ana"] = "SUSPENDED"
    with pytest.raises(SessionRefused, match="suspended"):
        s.with_credential(sid, secret)
    status["urn:li:corpuser:ana"] = "ACTIVE"
    assert s.revoke("ana", "urn:li:corpuser:admin")["sessions_ended"] == 1
    for renew in (lambda: s.refresh(again["token"]), lambda: s.with_credential(sid, secret)):
        with pytest.raises(SessionRefused, match="ended"):
            renew()
    fresh = s.issue("urn:li:corpuser:ana")
    assert s.with_credential(*fresh["credential"].split(":", 1))
    actions = [e["action"] for e in audit.entries("lakehouse")]
    assert actions.count("lakehouse.token_refreshed") == 4 and actions.count("lakehouse.token_issued") == 2
    assert "lakehouse.sessions_revoked" in actions and audit.verify_chain()["valid"]


def test_renewal_fails_closed(tmp_path):
    import pytest

    from cairn.dataaccess.lakehouse import SessionRefused

    s, _, _ = _sessions(tmp_path, user_status=lambda u: (_ for _ in ()).throw(RuntimeError("graph down")))
    out = s.issue("urn:li:corpuser:ana")
    with pytest.raises(SessionRefused) as e:
        s.with_credential(*out["credential"].split(":", 1))
    assert e.value.error == "temporarily_unavailable"
    s.user_status = None
    for bad in (s.tokens.mint("urn:li:corpuser:ana")["token"],                      # Trino's audience
                s.tokens.mint("urn:li:corpuser:ana", audience="lakehouse")["token"],  # no session
                s.tokens.mint("urn:li:corpuser:bob", audience="lakehouse",           # another user's session
                              extra={"sid": out["session"], "sst": 1})["token"],
                out["token"][:-3] + "abc", "garbage"):
        with pytest.raises(SessionRefused):
            s.refresh(bad)
    with pytest.raises(ValueError):
        s.issue("urn:li:corpGroup:analysts")


def test_issuing_and_revoking_commit_with_their_audit_entries(tmp_path):
    import pytest

    s, audit, engine = _sessions(tmp_path)
    s.issue("urn:li:corpuser:ana")

    def boom(conn, body):
        raise RuntimeError("audit store failed")
    real = audit._append
    audit._append = boom
    with pytest.raises(RuntimeError):
        s.revoke("ana", "urn:li:corpuser:admin")
    with pytest.raises(RuntimeError):
        s.issue("urn:li:corpuser:bob")
    audit._append = real
    from sqlalchemy import select

    from cairn.dataaccess.lakehouse import lakehouse_sessions
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(lakehouse_sessions))]
    assert [(r["user"], r["ended_s"]) for r in rows] == [("ana", None)]  # both rolled back


def test_the_token_endpoint_speaks_oauth(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cairn.dataaccess.lakehouse import build_lakehouse_router

    s, _, _ = _sessions(tmp_path)
    app = FastAPI()
    app.include_router(build_lakehouse_router(None, s, lambda: None))
    c = TestClient(app)
    out = s.issue("urn:li:corpuser:ana")
    sid, secret = out["credential"].split(":", 1)
    url = "/v1/lakehouse/oauth/tokens"
    for kw in ({"data": {"grant_type": "client_credentials", "client_id": sid, "client_secret": secret, "scope": "catalog"}},
               {"data": {"grant_type": "client_credentials"}, "auth": (sid, secret)},
               {"data": {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": out["token"],
                         "subject_token_type": "urn:ietf:params:oauth:token-type:access_token"}}):
        r = c.post(url, **kw)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token_type"] == "Bearer" and body["expires_in"] > 0 and r.headers["cache-control"] == "no-store"
        assert s.tokens.verify(body["access_token"], "lakehouse")["sid"] == sid
    r = c.post(url, data={"grant_type": "client_credentials"}, auth=(sid, "wrong"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_client" and "www-authenticate" in r.headers
    assert c.post(url, data={"grant_type": "password"}).json()["error"] == "unsupported_grant_type"
    r = c.post(url, data={"grant_type": "client_credentials", "client_id": sid, "client_secret": secret,
                          "requested_token_type": "urn:ietf:params:oauth:token-type:refresh_token"})
    assert r.json()["error"] == "invalid_request"
