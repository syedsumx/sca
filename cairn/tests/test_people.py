"""People & roles (cairn.identity.people) through Cairn's real API, policy decision point and database: the directory
from the catalog's people and groups, a person's page with why they hold each persona, the access check's yes or no
and why, manual people and groups (with SCIM's refused), deactivation ending sessions, and the audit trail.

The catalog here is the in-memory one Cairn's services read and write (Cairn Core itself is the stack job's:
tests/e2e/test_people_live.py)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,finance.public.ledger,PROD)"
HR_DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,hr.public.payroll,PROD)"


@pytest.fixture
def world(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_DEVICE_POLL_SECONDS", "1")
    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.governance.personas import Members, PersonaSpec, PurposeSpec, Scope
    from cairn.services import build_services

    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admins", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    graph.aspects[(ADMIN, "corpUserInfo")] = {"active": True, "displayName": "Admin"}
    graph.aspects[(BOB, "corpUserInfo")] = {"active": True, "displayName": "Bob Stone", "email": "bob@example.com",
                                            "departmentName": "Sales"}
    graph.aspects[("urn:li:corpGroup:sso-sales", "corpGroupInfo")] = {"displayName": "Sales (SSO)", "admins": [],
                                                                       "members": [], "groups": []}
    graph.aspects[("urn:li:corpGroup:sso-sales", "origin")] = {"type": "EXTERNAL", "externalType": "OIDC"}
    graph.aspects[(BOB, "groupMembership")] = {"groups": ["urn:li:corpGroup:sso-sales"]}
    graph.aspects[(DS, "domains")] = {"domains": ["urn:li:domain:finance"]}
    graph.aspects[(HR_DS, "domains")] = {"domains": ["urn:li:domain:finance"]}
    graph.aspects[(HR_DS, "glossaryTerms")] = {"terms": [{"urn": "urn:li:glossaryTerm:Classification.PII.Salary"}]}
    s.personas.upsert_persona(PersonaSpec(name="Finance analysts", permissions=["view", "data_access"],
                                          members=Members(groups=["urn:li:corpGroup:finance-analysts"]),
                                          scope=Scope(domains=["urn:li:domain:finance"])), ADMIN)
    s.personas.upsert_persona(PersonaSpec(name="Contractors", permissions=["view"],
                                          members=Members(attribute_rules=[{"attr": "employment_type", "op": "eq",
                                                                            "value": "contractor"}]),
                                          scope=Scope(platforms=["postgres"])), ADMIN)
    s.personas.upsert_persona(PersonaSpec(name="Sales", permissions=["view"], members=Members(users=[BOB])), ADMIN)
    s.personas.upsert_purpose(PurposeSpec(name="Payroll", tags=["PII.*"], deny=["view"],
                                          allowed={"groups": ["urn:li:corpGroup:hr"]}), ADMIN)
    app = create_api_app(s)
    with TestClient(app, headers={"X-Cairn-Actor": ADMIN}) as admin, \
            TestClient(app, headers={"X-Cairn-Actor": BOB}) as bob:
        yield s, graph, admin, bob


DANA = {"id": "dana.lee", "display_name": "Dana Lee", "email": "dana.lee@example.com", "first_name": "Dana",
        "last_name": "Lee", "title": "Analyst", "department": "Finance", "country": "DE",
        "employment_type": "contractor", "attributes": {"clearance": "high"}}


def test_only_administrators_see_or_change_the_directory(world):
    s, _, admin, bob = world
    for method, path, body in (("GET", "/v1/people/users", None), ("GET", "/v1/people/users/bob", None),
                               ("GET", "/v1/people/groups", None), ("GET", "/v1/people/principals", None),
                               ("POST", "/v1/people/users", DANA), ("DELETE", "/v1/people/users/admin", None),
                               ("POST", "/v1/people/access-check", {"person": "bob"})):
        r = bob.request(method, path, json=body)
        assert r.status_code == 403, (method, path, r.text)
    assert admin.get("/v1/people/users").status_code == 200
    # An agent credential never reaches the directory, not even to read.
    from cairn.common.licence import READ_POSTS
    from cairn.identity.agents import required_scope

    assert required_scope("GET", "/v1/people/users", READ_POSTS) is None
    assert required_scope("POST", "/v1/people/access-check", READ_POSTS) is None


def test_a_manual_person_is_written_to_the_catalog_and_their_attributes_drive_policies(world):
    s, graph, admin, _ = world
    r = admin.post("/v1/people/users", json=DANA)
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["source"], p["status"], p["type"]) == ("manual", "active", "person")
    urn = "urn:li:corpuser:dana.lee"
    info = graph.aspects[(urn, "corpUserInfo")]
    assert info["displayName"] == "Dana Lee" and info["countryCode"] == "DE" and info["departmentName"] == "Finance"
    assert info["fullName"] == "Dana Lee" and info["active"] is True
    props = {x["propertyUrn"]: x["values"] for x in graph.aspects[(urn, "structuredProperties")]["properties"]}
    assert props["urn:li:structuredProperty:io.cairn.employment_type"] == [{"string": "contractor"}]
    assert props["urn:li:structuredProperty:io.cairn.clearance"] == [{"string": "high"}]
    # The attributes' definitions are declared once (by the control plane), for people only.
    d = graph.aspects[("urn:li:structuredProperty:io.cairn.clearance", "propertyDefinition")]
    assert d["entityTypes"] == ["urn:li:entityType:datahub.corpuser"] and d["cardinality"] == "SINGLE"
    # ABAC reads them: subject.attributes.employment_type puts Dana in the Contractors persona (by attribute rule).
    assert s.attributes.subject(urn)["attributes"]["employment_type"] == "contractor"
    held = {x["name"]: x["reasons"] for x in p["personas"]}
    assert held["Contractors"][0]["kind"] == "attribute_rule"
    assert p["attributes"] == {"clearance": "high"} and p["employment_type"] == "contractor"

    # Changed in place: an attribute dropped is dropped from the catalog too; one Cairn does not manage stays.
    graph.aspects[(urn, "structuredProperties")]["properties"].append(
        {"propertyUrn": "urn:li:structuredProperty:acme.badge", "values": [{"string": "blue"}]})
    r = admin.put("/v1/people/users/dana.lee", json={**{k: v for k, v in DANA.items() if k != "id"},
                                                    "title": "Senior analyst", "attributes": {}})
    assert r.status_code == 200, r.text
    names = {x["propertyUrn"] for x in graph.aspects[(urn, "structuredProperties")]["properties"]}
    assert names == {"urn:li:structuredProperty:io.cairn.employment_type", "urn:li:structuredProperty:acme.badge"}
    assert r.json()["title"] == "Senior analyst" and r.json()["attributes"] == {}
    assert admin.post("/v1/people/users", json=DANA).status_code == 409                # already there
    bad = admin.post("/v1/people/users", json={**DANA, "id": "x y", "country": "Germany",
                                               "attributes": {"department": "x"}})
    assert bad.status_code == 422 and {e["loc"][-1] for e in bad.json()["detail"]} >= {"id", "country", "attributes"}
    actions = [e["action"] for e in s.audit.entries("people", 20)]
    assert actions[:2] == ["person.updated", "person.created"]


def test_the_directory_lists_searches_and_filters_people_and_groups(world):
    s, graph, admin, _ = world
    from cairn.identity.scim import ScimService

    ScimService(s.engine, graph, s.audit).create_user({"userName": "erin", "displayName": "Erin Okta", "active": True})
    admin.post("/v1/people/users", json=DANA)
    assert admin.post("/v1/people/groups", json={"id": "finance-analysts", "name": "Finance analysts"}).status_code == 201
    assert admin.post("/v1/people/groups/finance-analysts/members", json={"person": "dana.lee"}).status_code == 201
    graph.aspects[("urn:li:corpuser:cairn-service", "corpUserInfo")] = {"active": True, "displayName": "Cairn service"}

    page = admin.get("/v1/people/users").json()
    by = {p["id"]: p for p in page["items"]}
    assert {by["erin"]["source"], by["dana.lee"]["source"], by["bob"]["source"]} == {"scim", "manual", "sso"}
    assert by["cairn-service"]["type"] == "service"
    assert [p["id"] for p in admin.get("/v1/people/users?source=manual").json()["items"]] == ["dana.lee"]
    assert [p["id"] for p in admin.get("/v1/people/users?q=sales").json()["items"]] == ["bob"]   # by department
    assert [p["id"] for p in admin.get("/v1/people/users?group=finance-analysts").json()["items"]] == ["dana.lee"]
    assert admin.get("/v1/people/users?type=service").json()["total"] == 1
    two = admin.get("/v1/people/users?limit=2&offset=1").json()
    assert two["total"] == page["total"] and len(two["items"]) == 2 and two["items"][0] == page["items"][1]
    assert admin.get("/v1/people/users?status=gone").status_code == 422

    groups = {g["id"]: g for g in admin.get("/v1/people/groups").json()["items"]}
    assert groups["finance-analysts"]["source"] == "manual" and groups["finance-analysts"]["members"] == 1
    assert groups["sso-sales"]["source"] == "sso" and groups["sso-sales"]["members"] == 1
    g = admin.get("/v1/people/groups/finance-analysts").json()
    assert [m["id"] for m in g["member_list"]] == ["dana.lee"] and g["personas"] == [
        {"id": "finance-analysts", "name": "Finance analysts"}] and g["managed"]["editable"]
    # The membership is native (single sign-on's groups never overwrite it) and the policy engine sees it at once.
    assert graph.aspects[("urn:li:corpuser:dana.lee", "nativeGroupMembership")] == {
        "nativeGroups": ["urn:li:corpGroup:finance-analysts"]}
    reasons = {x["name"]: x["reasons"] for x in admin.get("/v1/people/users/dana.lee").json()["personas"]}
    assert reasons["Finance analysts"] == [{"kind": "group", "group": "urn:li:corpGroup:finance-analysts"}]
    assert admin.get("/v1/people/groups/finance-analysts/members").json() == [
        {"id": "dana.lee", "group": "finance-analysts", "person": "dana.lee"}]


def test_scim_people_and_groups_are_the_identity_providers(world):
    s, graph, admin, _ = world
    from cairn.identity.scim import ScimService

    scim = ScimService(s.engine, graph, s.audit)
    scim.create_user({"userName": "erin", "displayName": "Erin Okta", "active": True})
    scim.create_group({"displayName": "Okta Admins", "members": [{"value": "erin"}]})
    refusals = [admin.post("/v1/people/users", json={**DANA, "id": "erin"}),
                admin.put("/v1/people/users/erin", json={"display_name": "Someone else"}),
                admin.delete("/v1/people/users/erin"),
                admin.post("/v1/people/users/erin/activate"),
                admin.post("/v1/people/groups", json={"id": "okta-admins", "name": "Mine now"}),
                admin.put("/v1/people/groups/okta-admins", json={"name": "Mine now"}),
                admin.post("/v1/people/groups/okta-admins/members", json={"person": "bob"}),
                admin.delete("/v1/people/groups/okta-admins/members/erin"),
                admin.delete("/v1/people/groups/okta-admins")]
    for r in refusals:
        assert r.status_code == 409 and "managed by your identity provider" in r.json()["detail"], r.text
    assert graph.aspects[("urn:li:corpuser:erin", "corpUserInfo")]["displayName"] == "Erin Okta"   # untouched
    # A group from single sign-on is refused too; one only in the catalog is adopted.
    r = admin.post("/v1/people/groups", json={"id": "sso-sales", "name": "Sales"})
    assert r.status_code == 409 and "sign-in" in r.json()["detail"]
    assert admin.get("/v1/people/users/erin").json()["managed"] == {
        "row": False, "editable": False, "why": "managed by your identity provider (SCIM): change it there"}
    # An SSO person is adopted when created here with their id; a person not managed here cannot be edited.
    assert admin.put("/v1/people/users/bob", json={"display_name": "Bob"}).status_code == 409
    r = admin.post("/v1/people/users", json={"id": "bob", "display_name": "Bob Stone", "department": "Sales"})
    assert r.status_code == 201 and r.json()["source"] == "manual"
    assert "person.adopted" in [e["action"] for e in s.audit.entries("people", 10)]


def _approved(svc, actor):
    from cairn.identity.device import CLIENT_ID

    start = svc.start(CLIENT_ID, "laptop")
    svc.decide(actor, start["user_code"], True)
    time.sleep(1.05)
    return svc.poll(start["device_code"], CLIENT_ID)


def test_deactivation_suspends_and_ends_every_session(world):
    s, graph, admin, _ = world
    admin.post("/v1/people/users", json=DANA)
    tokens = _approved(s.device_logins, "urn:li:corpuser:dana.lee")
    assert s.device_logins.resolve(tokens["access_token"]) is not None
    assert admin.delete("/v1/people/users/dana.lee").status_code == 204
    assert s.device_logins.resolve(tokens["access_token"]) is None
    assert graph.aspects[("urn:li:corpuser:dana.lee", "corpUserStatus")]["status"] == "SUSPENDED"
    p = admin.get("/v1/people/users/dana.lee").json()
    assert p["status"] == "deactivated"
    entry = next(e for e in s.audit.entries("people", 10) if e["action"] == "person.deactivated")
    assert entry["actor"] == ADMIN and entry["details"]["sessions_ended"] >= 1
    # Destroyed configuration is gone from what Terraform reads; declared again, the person is back.
    assert admin.get("/v1/people/users?source=manual&status=active").json()["total"] == 0
    assert admin.post("/v1/people/users", json=DANA).status_code == 201
    assert graph.aspects[("urn:li:corpuser:dana.lee", "corpUserStatus")]["status"] == "ACTIVE"
    # An SSO person can be deactivated (offboarding without SCIM); nobody deactivates themselves or Cairn's accounts.
    assert admin.delete("/v1/people/users/bob").status_code == 204
    assert admin.post("/v1/people/users/bob/activate").json()["status"] == "active"
    assert admin.delete("/v1/people/users/admin").status_code == 409
    graph.aspects[("urn:li:corpuser:cairn-service", "corpUserInfo")] = {"active": True}
    assert admin.delete("/v1/people/users/cairn-service").status_code == 409
    assert admin.delete("/v1/people/users/nobody").status_code == 404


def test_a_persons_page_says_what_they_hold_and_why(world):
    s, graph, admin, _ = world
    admin.post("/v1/people/users", json=DANA)
    admin.post("/v1/people/groups", json={"id": "finance-analysts", "name": "Finance analysts"})
    admin.post("/v1/people/groups/finance-analysts/members", json={"person": "dana.lee"})
    sales = next(p for p in s.personas.list_personas() if p.name == "Sales")
    s.personas.grant(sales.id, "urn:li:corpuser:dana.lee", "manual", None, ADMIN)
    from cairn.governance.access_reviews import ReviewIn
    from cairn.governance.workspaces import WorkspaceIn
    from cairn.identity.agents import AgentIn

    s.workspaces.put(WorkspaceIn(id="finance", name="Finance", domains=["urn:li:domain:finance"],
                                 members=["urn:li:corpGroup:finance-analysts"]), ADMIN, True)
    key = s.agents.create_key_agent(AgentIn(name="dana's bot", scopes=["read"]), "urn:li:corpuser:dana.lee")
    s.access_reviews.start(ADMIN, ReviewIn(name="Q4", reviewers=[ADMIN], due_days=7))
    s.audit.record("identity", "device.signed_in", "urn:li:corpuser:dana.lee", "cli-1", {})

    p = admin.get("/v1/people/users/dana.lee").json()
    why = {x["name"]: [r["kind"] for r in x["reasons"]] for x in p["personas"]}
    assert why == {"Contractors": ["attribute_rule"], "Finance analysts": ["group"], "Sales": ["grant"]}
    assert [g["persona"] for g in p["persona_grants"]] == [sales.id]
    assert p["workspaces"] == [{"id": "finance", "name": "Finance", "role": "member",
                                "domains": ["urn:li:domain:finance"]}]
    assert [(o["kind"], o["name"]) for o in p["owns"]] == [("agent", "dana's bot")] and key["id"] == p["owns"][0]["id"]
    assert p["last_sign_in"]["action"] == "device.signed_in"
    assert [i["persona"] for i in p["open_access_reviews"]] == [sales.id]
    assert {e["action"] for e in p["recent_audit"]} >= {"person.created", "group.member_added", "device.signed_in"}
    assert [g["id"] for g in p["groups"]] == ["finance-analysts"]

    reach = admin.get("/v1/people/users/dana.lee/access").json()
    scopes = {x["name"]: x["scope_text"] for x in reach["personas"]}
    assert scopes["Finance analysts"] == "domains: finance" and scopes["Sales"] == "the whole catalog"
    fin = next(x for x in reach["personas"] if x["name"] == "Finance analysts")
    assert "CAIRN_DATA_ACCESS" in fin["privileges"]
    assert reach["purposes"] == [{"id": "payroll", "name": "Payroll", "labels": ["PII.*"], "allowed": False,
                                  "effect": "denied view; values masked (partial)"}]
    assert reach["workspaces"][0]["role"] == "member"


def test_the_access_check_says_yes_or_no_and_why(world):
    s, graph, admin, _ = world
    admin.post("/v1/people/users", json={**DANA, "employment_type": None})
    admin.post("/v1/people/groups", json={"id": "finance-analysts", "name": "Finance analysts"})
    admin.post("/v1/people/groups/finance-analysts/members", json={"person": "dana.lee"})

    yes = admin.post("/v1/people/access-check", json={"person": "dana.lee", "privilege": "CAIRN_DATA_ACCESS",
                                                     "resource": DS}).json()
    assert yes["allowed"] and yes["decision"] == "ALLOW"
    assert [(b["kind"], b["name"]) for b in yes["because"]] == [("persona", "Finance analysts")]
    assert yes["summary"].startswith("Yes: Dana Lee may CAIRN_DATA_ACCESS on") and "persona Finance analysts" in yes["summary"]
    assert "urn:li:corpGroup:finance-analysts" in yes["subject"]["groups"]

    # The payroll table carries PII: the purpose denies Dana, whatever the persona allows.
    no = admin.post("/v1/people/access-check", json={"person": "urn:li:corpuser:dana.lee",
                                                    "privilege": "VIEW_ENTITY_PAGE", "resource": HR_DS}).json()
    assert not no["allowed"] and no["decision"] == "DENY"
    assert [(b["kind"], b["name"]) for b in no["because"]] == [("purpose", "Payroll")]
    assert "denied by purpose Payroll" in no["summary"]

    # Nothing grants it: no in Cairn, and the catalog's own policies decide there (additive mode). The personas that
    # could have granted the privilege say why they did not.
    bob = admin.post("/v1/people/access-check", json={"person": "bob", "privilege": "CAIRN_DATA_ACCESS",
                                                     "resource": DS}).json()
    assert bob["decision"] == "NOT_APPLICABLE" and not bob["allowed"] and "Cairn Core" in bob["summary"]
    assert any(c["name"] == "Finance analysts" and c["why_not"] for c in bob["considered"])

    # Asking is audited as the administrator's question; the person's own deny record is not written.
    assert [e["action"] for e in s.audit.entries("authz", 10)] == []
    checks = [e for e in s.audit.entries("people", 10) if e["action"] == "access.checked"]
    assert len(checks) == 3 and checks[0]["actor"] == ADMIN and checks[0]["subject"] == "urn:li:corpuser:bob"
    assert admin.post("/v1/people/access-check", json={"person": "nobody"}).status_code == 404
    assert admin.post("/v1/people/access-check", json={"person": "bob", "privilege": "lower"}).status_code == 422


def test_service_accounts_and_agents_are_listed_with_owner_and_scopes(world):
    s, graph, admin, _ = world
    from cairn.identity.agents import AgentIn
    from cairn.identity.workload import workload_bindings

    s.agents.create_key_agent(AgentIn(name="nightly-sync", scopes=["read", "write"]), BOB)
    graph.aspects[("urn:li:corpuser:svc-dbt", "corpUserInfo")] = {"active": True, "displayName": "dbt"}
    with s.engine.begin() as c:
        c.execute(workload_bindings.insert().values(id="b1", issuer="k8s", subject="system:serviceaccount:p:svc-dbt",
                                                    conditions="{}", user="urn:li:corpuser:svc-dbt", created_by=ADMIN,
                                                    created_ms=1))
    rows = admin.get("/v1/people/principals").json()
    agent = next(r for r in rows if r["type"] == "agent")
    assert (agent["name"], agent["owner"], agent["scopes"]) == ("nightly-sync", BOB, ["read", "write"])
    wl = next(r for r in rows if r["type"] == "workload")
    assert wl["acts_as"] == "urn:li:corpuser:svc-dbt" and wl["created_by"] == ADMIN
    svc = next(r for r in rows if r["type"] == "service")
    assert svc["id"] == "svc-dbt" and "workload identity" in svc["purpose"]
    assert [r["type"] for r in admin.get("/v1/people/principals?type=agent").json()] == ["agent"]
    assert admin.get("/v1/people/users?type=person").json()["total"] == 2         # admin, bob: not the pipeline


def test_manual_groups_change_and_go(world):
    s, graph, admin, _ = world
    admin.post("/v1/people/users", json=DANA)
    admin.post("/v1/people/groups", json={"id": "finance-analysts", "name": "Finance analysts"})
    admin.post("/v1/people/groups/finance-analysts/members", json={"person": "dana.lee"})
    r = admin.put("/v1/people/groups/finance-analysts", json={"name": "Finance", "description": "FP&A"})
    assert r.json()["name"] == "Finance" and graph.aspects[("urn:li:corpGroup:finance-analysts",
                                                             "corpGroupInfo")]["description"] == "FP&A"
    assert admin.post("/v1/people/groups/finance-analysts/members", json={"person": "ghost"}).status_code == 404
    assert admin.delete("/v1/people/groups/finance-analysts/members/bob").status_code == 404
    assert admin.delete("/v1/people/groups/finance-analysts").status_code == 204
    assert graph.aspects[("urn:li:corpuser:dana.lee", "nativeGroupMembership")] == {"nativeGroups": []}
    assert graph.aspects[("urn:li:corpGroup:finance-analysts", "status")] == {"removed": True}
    assert "finance-analysts" not in {g["id"] for g in admin.get("/v1/people/groups").json()["items"]}
    assert admin.get("/v1/people/groups/finance-analysts/members").json() == []
    actions = [e["action"] for e in s.audit.entries("people", 10)]
    assert actions[:3] == ["group.deleted", "group.updated", "group.member_added"]


def test_an_unreadable_catalog_is_an_error_not_an_empty_directory(world):
    s, graph, admin, _ = world

    def broken(*a, **kw):
        raise RuntimeError("connection refused")

    graph.scroll_entities = broken
    s.people._snap = None
    r = admin.get("/v1/people/users")
    assert r.status_code == 503 and "cannot be read" in r.json()["detail"]
