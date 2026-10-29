"""Workspaces: business units with delegated admins. A workspace's admins govern its domains and its
members, not the rest of the catalog; only a global admin assigns domains and admins."""

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.common.security import Principal
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.workspaces import WorkspaceError, WorkspaceIn
from cairn.services import build_services

ADMIN, ANA, BOB, CY = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cy"
RISK = "urn:li:corpGroup:risk"
FIN, MKT = "urn:li:domain:finance", "urn:li:domain:marketing"
LEDGER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.ledger,PROD)"
CAMPAIGNS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,mkt.campaigns,PROD)"
PRODUCT = "urn:li:dataProduct:revenue"


@pytest.fixture
def s(settings, graph):
    graph.aspects[(LEDGER, "domains")] = {"domains": [FIN]}
    graph.aspects[(LEDGER, "ownership")] = {"owners": [{"owner": ANA}]}
    graph.aspects[(CAMPAIGNS, "domains")] = {"domains": [MKT]}
    graph.aspects[(PRODUCT, "domains")] = {"domains": [FIN]}
    graph.aspects[(CY, "groupMembership")] = {"groups": [RISK]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    return s


def can(s, actor, privilege, urn):
    return s.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False)


def finance(**kw):
    return WorkspaceIn(**{"id": "finance", "name": "Finance", "domains": [FIN], "admins": [ANA], "members": [RISK], **kw})


def test_delegated_admins_govern_only_their_domains(s):
    ws = s.workspaces
    ws.put(finance(), ADMIN, is_admin=True)
    assert can(s, ANA, "EDIT_ENTITY_DOCS", LEDGER) and can(s, ANA, "CAIRN_APPROVE_ACCESS", LEDGER)
    assert not can(s, ANA, "EDIT_ENTITY_DOCS", CAMPAIGNS)                   # not their domain
    assert not can(s, ANA, "MANAGE_POLICIES", None) and not s.is_admin(ANA)  # not a global admin
    assert can(s, CY, "VIEW_ENTITY_PAGE", LEDGER) and not can(s, CY, "EDIT_ENTITY_DOCS", LEDGER)   # a member, by group
    assert not can(s, BOB, "VIEW_ENTITY_PAGE", LEDGER)
    assert ws.roles(ANA) == {"finance": "admin"} and ws.roles(CY) == {"finance": "member"} and ws.roles(BOB) == {}
    assert ws.workspace_of(LEDGER) == "finance" and ws.workspace_of(CAMPAIGNS) is None


def test_who_may_change_what(s):
    ws = s.workspaces
    ws.put(finance(), ADMIN, is_admin=True)
    ws.put(finance(members=[RISK, BOB], description="Ledger and revenue"), ANA, is_admin=False)   # members: fine
    assert can(s, BOB, "VIEW_ENTITY_PAGE", LEDGER)
    with pytest.raises(WorkspaceError) as e:
        ws.put(finance(domains=[FIN, MKT]), ANA, is_admin=False)            # domains: global admins only
    assert e.value.status == 403
    with pytest.raises(WorkspaceError):
        ws.put(finance(admins=[ANA, BOB]), ANA, is_admin=False)
    with pytest.raises(WorkspaceError) as e:
        ws.put(finance(), BOB, is_admin=False)                              # a member is not an admin
    assert e.value.status == 403
    with pytest.raises(WorkspaceError) as e:
        ws.put(WorkspaceIn(id="money", name="Money", domains=[FIN]), ADMIN, is_admin=True)   # a domain has one home
    assert e.value.status == 409
    with pytest.raises(ValueError):
        WorkspaceIn(id="x1", name="X", domains=["finance"])
    with pytest.raises(ValueError):
        WorkspaceIn(id="x1", name="X", domains=[FIN], admin_permissions=["everything"])
    ws.delete("finance", ADMIN)
    assert not can(s, ANA, "EDIT_ENTITY_DOCS", LEDGER) and ws.list() == []


def test_api(s):
    c = TestClient(create_api_app(s))
    h = lambda a: {"X-Cairn-Actor": a}  # noqa: E731
    body = finance().model_dump()
    assert c.put("/workspaces/finance", json=body, headers=h(ANA)).status_code == 403
    assert c.put("/workspaces/finance", json=body, headers=h(ADMIN)).status_code == 200
    assert c.put("/workspaces/other", json=body, headers=h(ADMIN)).status_code == 422
    assert c.get("/workspaces", headers=h(CY)).json() == [{"id": "finance", "name": "Finance", "domains": [FIN],
                                                          "role": "member"}]
    assert c.get("/workspaces", headers=h(BOB)).json() == []
    assert c.get("/workspaces/finance", headers=h(BOB)).status_code == 404
    got = c.get("/workspaces/finance", headers=h(ANA)).json()
    assert got["role"] == "admin" and got["assets"] == {"dataset": 1, "dataProduct": 1} and got["unowned"] == 1
    items = c.get("/workspaces/finance/assets", params={"type": "dataset"}, headers=h(CY)).json()
    assert items == {"total": 1, "items": [{"urn": LEDGER, "type": "dataset", "domain": FIN, "owners": [ANA]}]}
    assert c.get("/ui/config", headers=h(ANA)).json()["workspaces"][0]["role"] == "admin"
    assert c.delete("/workspaces/finance", headers=h(ANA)).status_code == 403
    assert c.delete("/workspaces/finance", headers=h(ADMIN)).status_code == 204
