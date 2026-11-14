"""SCIM 2.0 provisioning through the edge, as an identity provider would call it."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from test_stack_smoke import API, COMPOSE, EDGE, eventually, gql

SCIM_TOKEN = os.environ.get("CAIRN_E2E_SCIM_TOKEN", "")
pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1" or not SCIM_TOKEN,
                                reason="needs a live stack (CAIRN_E2E=1) with CAIRN_SCIM_TOKEN")

RUN = uuid.uuid4().hex[:6]
USER_ID = f"erin.scim{RUN}"
USER = f"urn:li:corpuser:{USER_ID}"
GROUP_NAME = f"SCIM Analysts {RUN}"


@pytest.fixture(scope="module")
def scim():
    with httpx.Client(base_url=f"{EDGE}/cairn/api/scim/v2", timeout=60,
                      headers={"Authorization": f"Bearer {SCIM_TOKEN}"}) as c:
        yield c


def test_rejects_without_token():
    assert httpx.get(f"{EDGE}/cairn/api/scim/v2/Users", timeout=30).status_code == 401


def test_provision_user_and_group_and_they_drive_decisions(scim):
    r = scim.post("/Users", json={"userName": USER_ID, "externalId": f"idp-{RUN}", "active": True,
                                  "name": {"givenName": "Erin", "familyName": "Scim"}, "displayName": "Erin Scim",
                                  "emails": [{"value": f"{USER_ID}@example.com", "primary": True}]})
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["location"].endswith(f"/cairn/api/scim/v2/Users/{USER_ID}")
    r = scim.post("/Groups", json={"displayName": GROUP_NAME, "members": [{"value": USER_ID}]})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]

    # The catalog shows the person and the membership.
    data = gql("""query($u: String!) { corpUser(urn: $u) { properties { displayName email active } } }""",
               {"u": USER})
    assert data["corpUser"]["properties"]["displayName"] == "Erin Scim"
    members = lambda: {m["value"] for m in scim.get(f"/Groups/{gid}").json().get("members", [])}  # noqa: E731
    assert eventually(lambda: USER_ID in members(), timeout=60)

    # Membership reaches the PDP at once (provisioning invalidates the cached subject).
    probe = f"CAIRN_E2E_SCIM_PROBE_{RUN.upper()}"
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {os.environ['CAIRN_E2E_TOKEN']}"},
                      timeout=60) as root:
        saved = root.get("/governance/policies").json()["policies"]
        rule = {"id": f"e2e-scim-{RUN}", "description": "e2e: SCIM group grant", "effect": "allow",
                "actions": [probe], "condition": {"attr": "subject.groups", "op": "contains",
                                                  "value": f"urn:li:corpGroup:{gid}"}}
        assert root.put("/governance/policies", json={"policies": saved + [rule]}).status_code == 200
        try:
            def decision() -> str:
                r = httpx.post(f"{API}/governance/v1/authorize", timeout=30,
                               headers={"Authorization": f"Bearer {os.environ['CAIRN_E2E_PDP_TOKEN']}"},
                               json={"actor": USER, "privilege": probe, "resource": None})
                return r.json()["decision"]
            assert eventually(lambda: decision() == "ALLOW", timeout=30, interval=1)
            # Removing the member takes effect within seconds, not the attribute TTL.
            r = scim.patch(f"/Groups/{gid}", json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                                                   "Operations": [{"op": "remove",
                                                                   "path": f'members[value eq "{USER_ID}"]'}]})
            assert r.status_code == 200, r.text
            assert eventually(lambda: decision() != "ALLOW", timeout=15, interval=1)
        finally:
            root.put("/governance/policies", json={"policies": saved})


def test_deactivation(scim):
    r = scim.patch(f"/Users/{USER_ID}", json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                                              "Operations": [{"op": "replace", "value": {"active": False}}]})
    assert r.status_code == 200 and r.json()["active"] is False
    data = gql("""query($u: String!) { corpUser(urn: $u) { status properties { active } } }""", {"u": USER})
    assert data["corpUser"]["properties"]["active"] is False
    assert data["corpUser"]["status"] == "SUSPENDED"
    assert scim.get(f'/Users?filter=userName eq "{USER_ID}"').json()["Resources"][0]["active"] is False


def _compose_cat(path: str) -> str:
    import subprocess

    root = os.path.join(os.path.dirname(__file__), "..", "..")
    out = subprocess.run([*COMPOSE,
                          "exec", "-T", "cairn-api", "cat", path], capture_output=True, text=True, timeout=60, cwd=root)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _ingest(token: str, urn: str, aspect: str, value: dict) -> int:
    from test_stack_smoke import CORE

    proposal = {"entityType": urn.split(":")[2], "entityUrn": urn, "changeType": "UPSERT", "aspectName": aspect,
                "aspect": {"contentType": "application/json", "value": __import__("json").dumps(value)}}
    return httpx.post(f"{CORE}/aspects?action=ingestProposal", json={"proposal": proposal, "async": "false"},
                      headers={"Authorization": f"Bearer {token}", "X-RestLi-Protocol-Version": "2.0.0"},
                      timeout=60).status_code


def test_provisioning_and_control_plane_accounts_are_separated():
    from test_stack_smoke import CORE

    prov = _compose_cat("/run/cairn-provisioner/provisioner_token")
    service = _compose_cat("/run/cairn/core_token")
    me = httpx.post(f"{CORE}/api/graphql", json={"query": "{ me { corpUser { urn } } }"},
                    headers={"Authorization": f"Bearer {prov}"}, timeout=30).json()
    assert me["data"]["me"]["corpUser"]["urn"] == "urn:li:corpuser:cairn-provisioner"
    ds = "urn:li:dataset:(urn:li:dataPlatform:postgres,scim_probe.public.t,PROD)"
    assert _ingest(prov, ds, "datasetProperties", {"description": "x"}) == 403       # no data
    assert _ingest(prov, f"urn:li:corpuser:probe{RUN}", "corpUserInfo", {"active": True}) == 200
    r = httpx.post(f"{CORE}/api/graphql", headers={"Authorization": f"Bearer {prov}"}, timeout=30, json={
        "query": 'mutation { createPolicy(input: {type: PLATFORM, name: "x", state: ACTIVE, privileges: ["MANAGE_POLICIES"], '
                 'actors: {allUsers: true, resourceOwners: false, allGroups: false}}) }'})
    assert r.json().get("errors"), r.text                                              # no policies
    # The control-plane account cannot change people or their groups.
    assert _ingest(service, f"urn:li:corpuser:probe{RUN}", "groupMembership", {"groups": []}) == 403
    # The provisioning token is not mounted into other services.
    import subprocess

    root = os.path.join(os.path.dirname(__file__), "..", "..")
    other = subprocess.run([*COMPOSE,
                            "exec", "-T", "cairn-mcp", "ls", "/run/cairn-provisioner"],
                           capture_output=True, text=True, timeout=60, cwd=root)
    assert other.returncode != 0
