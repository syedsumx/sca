"""People & roles (L139) against the live stack: Cairn's API writing people and groups into Cairn Core itself (as the
provisioning account), reading them back from Core's search and scroll, the policy engine deciding with the group and
attributes Core holds, deactivation suspending the person in Core, and SCIM's people refused."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from test_stack_smoke import API, TOKEN, eventually, gql

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1" or not TOKEN,
                                reason="needs a live stack (CAIRN_E2E=1, CAIRN_E2E_TOKEN)")

RUN = uuid.uuid4().hex[:6]
UID, GID = f"people.e2e{RUN}", f"people-e2e-{RUN}"
URN = f"urn:li:corpuser:{UID}"
DS = f"urn:li:dataset:(urn:li:dataPlatform:postgres,people_e2e_{RUN}.public.t,PROD)"


@pytest.fixture(scope="module")
def root():
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60) as c:
        yield c


def test_a_manual_person_and_group_live_in_cairn_core(root):
    r = root.post("/v1/people/users", json={"id": UID, "display_name": f"People E2E {RUN}", "email": f"{UID}@example.com",
                                            "department": "Finance", "country": "DE", "employment_type": "contractor",
                                            "attributes": {"clearance": "high"}})
    assert r.status_code == 201, r.text
    data = gql("""query($u: String!) { corpUser(urn: $u) { status properties { displayName email departmentName active }
                  structuredProperties { properties { structuredProperty { urn } values { ... on StringValue
                  { stringValue } } } } } }""", {"u": URN})
    props = data["corpUser"]["properties"]
    assert props["displayName"] == f"People E2E {RUN}" and props["departmentName"] == "Finance" and props["active"]
    values = {p["structuredProperty"]["urn"]: [v.get("stringValue") for v in p["values"]]
              for p in (data["corpUser"].get("structuredProperties") or {}).get("properties") or []}
    assert values["urn:li:structuredProperty:io.cairn.employment_type"] == ["contractor"]
    assert values["urn:li:structuredProperty:io.cairn.clearance"] == ["high"]

    assert root.post("/v1/people/groups", json={"id": GID, "name": f"People E2E {RUN}"}).status_code == 201
    assert root.post(f"/v1/people/groups/{GID}/members", json={"person": UID}).status_code == 201
    # The membership is native: Core's own relationship shows it.
    members = lambda: {r["entity"]["urn"] for r in (gql(  # noqa: E731
        """query($g: String!) { corpGroup(urn: $g) { relationships(input: {types: ["IsMemberOfNativeGroup"],
           direction: INCOMING, start: 0, count: 50}) { relationships { entity { urn } } } } }""",
        {"g": f"urn:li:corpGroup:{GID}"})["corpGroup"]["relationships"] or {}).get("relationships") or []}
    assert eventually(lambda: URN in members(), timeout=60)
    # The directory reads it back from Core's scroll, with its source.
    found = eventually(lambda: [p for p in root.get(f"/v1/people/users?q={UID}").json()["items"] if p["id"] == UID],
                       timeout=60)
    assert found and found[0]["source"] == "manual" and f"urn:li:corpGroup:{GID}" in found[0]["groups"]


def test_the_access_check_decides_with_what_core_holds(root):
    pid = root.post("/governance/personas", json={"name": f"People E2E {RUN}", "permissions": ["view"],
                                                  "members": {"groups": [f"urn:li:corpGroup:{GID}"]},
                                                  "scope": {"asset_urns": [DS]}}).json()["id"]
    try:
        check = lambda: root.post("/v1/people/access-check", json={"person": UID, "privilege": "VIEW_ENTITY_PAGE",  # noqa: E731
                                                                   "resource": DS}).json()
        answer = eventually(lambda: (lambda a: a if a.get("allowed") else None)(check()), timeout=60)
        # The persona grants it (Core's default native view policy may allow it as well: both are listed).
        assert answer and "persona" in [b["kind"] for b in answer["because"]]
        page = root.get(f"/v1/people/users/{UID}").json()
        why = {p["id"]: [r["kind"] for r in p["reasons"]] for p in page["personas"]}
        assert why[pid] == ["group"]
    finally:
        root.delete(f"/governance/personas/{pid}")


def test_deactivation_suspends_in_core(root):
    assert root.delete(f"/v1/people/users/{UID}").status_code == 204
    data = gql("""query($u: String!) { corpUser(urn: $u) { status properties { active } } }""", {"u": URN})
    assert data["corpUser"]["status"] == "SUSPENDED" and data["corpUser"]["properties"]["active"] is False
    assert root.delete(f"/v1/people/groups/{GID}").status_code == 204


def test_scim_people_are_refused():
    token = os.environ.get("CAIRN_E2E_SCIM_TOKEN", "")
    if not token:
        pytest.skip("needs CAIRN_SCIM_TOKEN on the stack")
    from test_stack_smoke import EDGE

    uid = f"people.scim{RUN}"
    with httpx.Client(base_url=f"{EDGE}/cairn/api/scim/v2", timeout=60,
                      headers={"Authorization": f"Bearer {token}"}) as scim:
        assert scim.post("/Users", json={"userName": uid, "active": True, "displayName": "SCIM person"}).status_code == 201
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60) as root:
        r = root.put(f"/v1/people/users/{uid}", json={"display_name": "Changed by hand"})
        assert r.status_code == 409 and "identity provider" in r.json()["detail"]
        assert root.delete(f"/v1/people/users/{uid}").status_code == 409
