"""SCIM 2.0 provisioning against an in-memory catalog: the request sequences Okta and Entra ID send."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cairn.common.audit import AuditLog
from cairn.common.core import InMemoryGraph
from cairn.common.db import get_engine, init_db
from cairn.identity.scim import ScimService, build_router, install_error_handler

TOKEN = "scim-test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@pytest.fixture
def scim(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/scim.db")
    init_db(engine)
    graph = InMemoryGraph()
    changed = []
    svc = ScimService(engine, graph, AuditLog(engine), "https://cairn.example.com/cairn/api/scim/v2",
                      on_change=changed.append)
    app = FastAPI()
    app.include_router(build_router(svc, token=lambda: TOKEN))
    install_error_handler(app)
    with TestClient(app) as c:
        yield c, graph, changed


def _user(name="dana", **extra):
    return {"schemas": [USER], "userName": name, "externalId": f"okta-{name}", "active": True,
            "name": {"givenName": "Dana", "familyName": "Lee"}, "displayName": "Dana Lee",
            "emails": [{"value": f"{name}@example.com", "primary": True, "type": "work"}], **extra}


def test_token_required(scim):
    c, _, _ = scim
    assert c.get("/scim/v2/Users").status_code == 401
    assert c.get("/scim/v2/Users", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/scim/v2/ServiceProviderConfig", headers=H)
    assert r.status_code == 200 and r.json()["patch"]["supported"]
    assert r.headers["content-type"].startswith("application/scim+json")


def test_user_lifecycle(scim):
    c, graph, changed = scim
    # Okta: look up by userName first, then create.
    assert c.get('/scim/v2/Users?filter=userName eq "dana"', headers=H).json()["totalResults"] == 0
    r = c.post("/scim/v2/Users", json=_user(), headers=H)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["id"] == "dana" and doc["active"] and doc["externalId"] == "okta-dana"
    assert doc["meta"]["location"] == "https://cairn.example.com/cairn/api/scim/v2/Users/dana"
    assert r.headers["ETag"] == 'W/"1"'
    info = graph.aspects[("urn:li:corpuser:dana", "corpUserInfo")]
    assert info["email"] == "dana@example.com" and info["fullName"] == "Dana Lee" and info["active"]
    assert c.post("/scim/v2/Users", json=_user(), headers=H).status_code == 409
    assert c.get('/scim/v2/Users?filter=externalId eq "okta-dana"', headers=H).json()["totalResults"] == 1

    # Entra ID: PATCH with paths; Okta: PATCH with a value object.
    r = c.patch("/scim/v2/Users/dana", headers=H, json={"schemas": [PATCH], "Operations": [
        {"op": "Replace", "path": "title", "value": "Analyst"},
        {"op": "replace", "path": "name.familyName", "value": "Park"}]})
    assert r.status_code == 200 and r.json()["title"] == "Analyst" and r.json()["name"]["familyName"] == "Park"
    r = c.patch("/scim/v2/Users/dana", headers=H, json={"schemas": [PATCH], "Operations": [
        {"op": "replace", "value": {"active": False}}]})
    assert r.json()["active"] is False
    assert graph.aspects[("urn:li:corpuser:dana", "corpUserStatus")]["status"] == "SUSPENDED"
    assert "urn:li:corpuser:dana" in changed                                      # PDP cache dropped

    # PUT re-activates; DELETE deactivates (history and ownership stay).
    r = c.put("/scim/v2/Users/dana", json=_user(active=True), headers=H)
    assert r.json()["active"] and r.headers["ETag"] == 'W/"4"'
    assert c.delete("/scim/v2/Users/dana", headers=H).status_code == 204
    assert c.get("/scim/v2/Users/dana", headers=H).json()["active"] is False
    assert c.get("/scim/v2/Users/nobody", headers=H).status_code == 404


def test_existing_sso_user_is_found_by_username(scim):
    c, graph, _ = scim
    graph.aspects[("urn:li:corpuser:erin", "corpUserInfo")] = {"active": True, "displayName": "Erin"}
    found = c.get('/scim/v2/Users?filter=userName eq "erin"', headers=H).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["displayName"] == "Erin"
    assert c.put("/scim/v2/Users/erin", json=_user("erin"), headers=H).status_code == 200


def test_groups_and_membership(scim):
    c, graph, _ = scim
    for n in ("dana", "erin"):
        c.post("/scim/v2/Users", json=_user(n), headers=H)
    r = c.post("/scim/v2/Groups", headers=H, json={"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                                                   "displayName": "Data Stewards", "members": [{"value": "dana"}]})
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "data-stewards" and [m["value"] for m in r.json()["members"]] == ["dana"]
    assert graph.aspects[("urn:li:corpuser:dana", "groupMembership")]["groups"] == ["urn:li:corpGroup:data-stewards"]
    assert graph.aspects[("urn:li:corpGroup:data-stewards", "origin")]["externalType"] == "SCIM"

    # Entra ID add/remove member operations.
    c.patch("/scim/v2/Groups/data-stewards", headers=H, json={"schemas": [PATCH], "Operations": [
        {"op": "Add", "path": "members", "value": [{"value": "erin"}]}]})
    assert {m["value"] for m in c.get("/scim/v2/Groups/data-stewards", headers=H).json()["members"]} == {"dana", "erin"}
    c.patch("/scim/v2/Groups/data-stewards", headers=H, json={"schemas": [PATCH], "Operations": [
        {"op": "remove", "path": 'members[value eq "dana"]'}]})
    assert [m["value"] for m in c.get("/scim/v2/Groups/data-stewards", headers=H).json()["members"]] == ["erin"]
    assert graph.aspects[("urn:li:corpuser:dana", "groupMembership")]["groups"] == []
    # The user's view lists the group.
    assert [g["value"] for g in c.get("/scim/v2/Users/erin", headers=H).json()["groups"]] == ["data-stewards"]

    # Okta: lookup by displayName, rename, PUT with full member list, delete.
    assert c.get('/scim/v2/Groups?filter=displayName eq "Data Stewards"', headers=H).json()["totalResults"] == 1
    c.patch("/scim/v2/Groups/data-stewards", headers=H, json={"schemas": [PATCH], "Operations": [
        {"op": "replace", "value": {"id": "data-stewards", "displayName": "Stewards"}}]})
    assert c.get("/scim/v2/Groups/data-stewards", headers=H).json()["displayName"] == "Stewards"
    r = c.put("/scim/v2/Groups/data-stewards", headers=H, json={"displayName": "Stewards", "members": [{"value": "dana"}]})
    assert [m["value"] for m in r.json()["members"]] == ["dana"]
    assert c.delete("/scim/v2/Groups/data-stewards", headers=H).status_code == 204
    assert graph.aspects[("urn:li:corpuser:dana", "groupMembership")]["groups"] == []
    assert c.get("/scim/v2/Groups", headers=H).json()["totalResults"] == 0


def test_errors_use_scim_format(scim):
    c, _, _ = scim
    r = c.get('/scim/v2/Users?filter=name.familyName co "x"', headers=H)
    assert r.status_code == 400 and r.json()["scimType"] == "invalidFilter"
    assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert c.post("/scim/v2/Users", json={"userName": "bad name!"}, headers=H).json()["scimType"] == "invalidValue"
    r = c.post("/scim/v2/Groups", json={"displayName": "G", "members": [{"value": "ghost"}]}, headers=H)
    assert r.status_code == 400


def test_pagination(scim):
    c, _, _ = scim
    for n in ("a1", "a2", "a3"):
        c.post("/scim/v2/Users", json=_user(n), headers=H)
    page = c.get("/scim/v2/Users?startIndex=2&count=1", headers=H).json()
    assert page["totalResults"] == 3 and page["itemsPerPage"] == 1 and page["Resources"][0]["id"] == "a2"
