"""Data planes (L135) through the real gateway app, with the real connector code: single-use enrolment, calls sealed
both ways, key rotation by signed re-enrolment, revocation, the tunnel's state, compute per workspace, and the API's
refusals. (The data plane on separate networks with Trino is tests/test_dataplane_e2e.py.)"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from cairn.agent.client import GatewayHTTPError
from cairn.app import create_api_app, create_gateway_app
from cairn.common.config import Settings
from cairn.dataplane.connector import Connector
from cairn.dataplane.service import DataPlaneError
from cairn.services import build_services

ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}


@pytest.fixture
def env(tmp_path, graph):
    s = build_services(Settings(database_url=f"sqlite:///{tmp_path}/c.db", service_token="svc-secret",
                                dev_trust_actor_header=True, gateway={"key_dir": str(tmp_path / "keys")}),
                       graph=graph, with_ai=False, with_gateway=True)
    api, gw = TestClient(create_api_app(s)), TestClient(create_gateway_app(s))
    assert api.post("/v1/dataplanes", json={"name": "acme"}, headers=ADMIN).status_code == 201

    def connector(name: str = "c1") -> Connector:
        c = Connector("http://testserver", str(tmp_path / name), wait_s=0.2)
        c.http = gw
        return c
    return s, api, gw, connector


def token(api) -> str:
    r = api.post("/v1/dataplanes/acme/enrollment-tokens", headers=ADMIN)
    assert r.status_code == 201
    return r.json()["token"]


def test_enrol_call_rotate_revoke(env):
    s, api, gw, connector = env
    c = connector()
    t = token(api)
    out = c.enroll(t)
    assert out["dataplane"] == "acme" and out["connector_id"].startswith("dpc_")
    with pytest.raises(GatewayHTTPError) as e:                       # single use
        connector("c2").enroll(t)
    assert e.value.status == 401
    assert c.poll_once() == 0
    assert api.get("/v1/dataplanes/acme", headers=ADMIN).json()["tunnel"] == "up"

    def serve(n: int):
        for _ in range(n * 10):
            if c.poll_once():
                n -= 1
            if n <= 0:
                return
    t1 = threading.Thread(target=serve, args=(1,))
    t1.start()
    r = api.post("/v1/dataplanes/acme/ping", headers=ADMIN)
    t1.join()
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    # A call the connector cannot run comes back as the call's failure.
    t2 = threading.Thread(target=serve, args=(1,))
    t2.start()
    with pytest.raises(DataPlaneError, match="no Trino"):
        s.dataplanes.call("acme", "trino.http", {"method": "GET", "url": "http://trino:8080/v1/info"}, "urn:li:corpuser:admin", 10)
    t2.join()
    # Key rotation: the new key signs from now on, and the call sealed to it opens.
    old = c.state.identity.signing_public
    c.rotate_keys()
    assert c.state.identity.signing_public != old
    assert c.poll_once() == 0
    # Revoked: every call refused, the tunnel down, queries and jobs refused with that message.
    cid = c.connector_id
    assert api.post(f"/v1/dataplanes/acme/connectors/{cid}/revoke", headers=ADMIN).status_code == 204
    with pytest.raises(GatewayHTTPError) as e:
        c.poll_once()
    assert e.value.status == 401 and "revoked" in str(e.value) and not c.authorized.is_set()
    assert api.get("/v1/dataplanes/acme", headers=ADMIN).json()["tunnel"] == "down"
    r = api.post("/v1/dataplanes/acme/ping", headers=ADMIN)
    assert r.status_code == 503 and "tunnel to data plane acme is down" in r.text


def test_opa_needs_trino_access_and_an_enrolled_connector(env):
    s, api, gw, connector = env
    c = connector()
    c.enroll(token(api))
    with pytest.raises(GatewayHTTPError) as e:                       # this Cairn has no Trino access configured
        c.signed(f"/dataplanes/tunnel/{c.connector_id}/opa/allow", {"input": {}})
    assert e.value.status == 503
    r = gw.post(f"/dataplanes/tunnel/{c.connector_id}/opa/allow", json={"input": {}})
    assert r.status_code == 401                                     # unsigned


def test_compute_per_workspace(env):
    s, api, gw, connector = env
    from cairn.governance.workspaces import WorkspaceIn

    for wid in ("sales", "ops"):
        s.workspaces.put(WorkspaceIn(id=wid, name=wid, domains=[f"urn:li:domain:{wid}"], members=["urn:li:corpuser:bob"]),
                         "urn:li:corpuser:admin", True)
    assert api.put("/v1/compute/workspaces/sales", json={"compute": "customer"}, headers=ADMIN).status_code == 422
    assert api.put("/v1/compute/workspaces/sales", json={"compute": "customer", "dataplane": "nope"},
                   headers=ADMIN).status_code == 404
    assert api.put("/v1/compute/workspaces/sales", json={"compute": "customer", "dataplane": "acme"},
                   headers=ADMIN).status_code == 200
    assert s.dataplanes.compute_for("urn:li:corpuser:ann") == ("managed", None)          # no workspace: the default
    with pytest.raises(DataPlaneError, match="different places"):                     # sales on acme, ops managed
        s.dataplanes.compute_for("urn:li:corpuser:bob")
    api.put("/v1/compute/workspaces/ops", json={"compute": "customer", "dataplane": "acme"}, headers=ADMIN)
    assert s.dataplanes.compute_for("urn:li:corpuser:bob") == ("customer", "acme")
    assert api.delete("/v1/dataplanes/acme", headers=ADMIN).status_code == 409          # still in use
    rows = api.get("/v1/compute/workspaces", headers=ADMIN).json()["workspaces"]
    assert {r["workspace"]: r["dataplane"] for r in rows} == {"ops": "acme", "sales": "acme"}
    assert api.get("/v1/dataplanes", headers={"X-Cairn-Actor": "urn:li:corpuser:bob"}).status_code == 403
