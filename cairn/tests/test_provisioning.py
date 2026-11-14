"""Access provisioning: approval grants real access on configured targets, ending access takes it
away, failures are told to approvers and retried, and nothing but plain names reaches SQL."""

import json
import time

import httpx
import pytest

from cairn.governance.attributes import AttributeResolver
from cairn.governance.pdp import PolicyDecisionPoint
from cairn.governance.personas import PersonaEngine
from cairn.marketplace.provisioning import (MAX_ATTEMPTS, ProvisionError, Provisioner, ProvisionStep, Target,
                                            statements)
from cairn.marketplace.service import AccessRequestIn, MarketplaceError, MarketplaceService, RequestPolicy
from cairn.notify.hub import NotificationHub

ALICE, CAROL = "urn:li:corpuser:alice", "urn:li:corpuser:carol"
PRODUCT = "urn:li:dataProduct:orders"
SF = Target(id="sf", type="snowflake", connection="wh", user="{username_upper}")


def test_statements_per_target_and_names_only():
    step = ProvisionStep(target="sf", role="ORDERS_READ")
    assert statements(SF, step, "ALICE", True) == ['GRANT ROLE "ORDERS_READ" TO USER "ALICE"']
    assert statements(SF, step, "ALICE", False) == ['REVOKE ROLE "ORDERS_READ" FROM USER "ALICE"']
    pg = Target(id="pg", type="postgres", connection="pg")
    assert statements(pg, ProvisionStep(target="pg", role="readers"), "alice", True) == ['GRANT "readers" TO "alice"']
    dbx = Target(id="d", type="databricks", connection="d")
    g = ProvisionStep(target="d", grant={"privilege": "select", "on": "TABLE main.sales.orders"})
    assert statements(dbx, g, "alice@acme.com", True) == \
        ["GRANT SELECT ON TABLE `main`.`sales`.`orders` TO `alice@acme.com`"]
    bq = Target(id="b", type="bigquery", connection="b")
    assert statements(bq, ProvisionStep(target="b", grant={"role": "roles/bigquery.dataViewer", "on": "acme.sales"}),
                      "alice@acme.com", False) == \
        ['REVOKE `roles/bigquery.dataViewer` ON SCHEMA `acme`.`sales` FROM "user:alice@acme.com"']
    with pytest.raises(ValueError):
        ProvisionStep(target="sf", role='R"; DROP TABLE x; --')
    with pytest.raises(ValueError):
        ProvisionStep(target="sf", role="R", group="G")
    with pytest.raises(ProvisionError, match="read privilege"):
        statements(dbx, ProvisionStep(target="d", grant={"privilege": "MODIFY", "on": "TABLE a.b.c"}), "u", True)
    with pytest.raises(ProvisionError, match="roles/bigquery"):
        statements(bq, ProvisionStep(target="b", grant={"role": "roles/owner", "on": "a.b"}), "u", True)
    with pytest.raises(ProvisionError, match="cannot grant"):
        statements(SF, ProvisionStep(target="sf", group="analysts"), "u", True)


class Warehouse:
    """A connection registry whose engine records the SQL it is asked to run."""

    def __init__(self):
        self.sql, self.fail = [], False
        outer = self

        class Conn:
            def execute(self, stmt):
                if outer.fail:
                    raise RuntimeError("Insufficient privileges to operate on role ORDERS_READ")
                outer.sql.append(str(stmt))

        class Engine:
            def begin(self):
                class Ctx:
                    def __enter__(self):
                        return Conn()

                    def __exit__(self, *a):
                        return False
                return Ctx()
        self.engine_ = Engine()

    def engine(self, name):
        assert name == "wh"
        return self.engine_


def okta_transport(calls):
    def handle(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == "SSWS okta-token"
        if req.url.path == "/api/v1/users/alice@acme.com":
            return httpx.Response(200, json={"id": "00u1"})
        if req.url.path == "/api/v1/groups/00g9/users/00u1":
            return httpx.Response(204)
        return httpx.Response(404)
    return httpx.MockTransport(handle)


@pytest.fixture
def market(engine, graph):
    def handler(query, variables):
        product = {"urn": PRODUCT, "properties": {"name": "Orders", "description": "", "numAssets": 1,
                                                  "customProperties": []},
                   "ownership": {"owners": [{"owner": {"urn": CAROL}}]}, "entities": {"searchResults": []}}
        return {"dataProduct": product} if "dataProduct(urn" in query else \
            {"searchAcrossEntities": {"searchResults": [{"entity": product}]}}
    graph.graphql_handler = handler
    graph.aspects[(PRODUCT, "ownership")] = {"owners": [{"owner": CAROL}]}
    attrs = AttributeResolver(graph)
    personas = PersonaEngine(engine, PolicyDecisionPoint(attrs, "authoritative", [], 0), None, attrs)
    hub = NotificationHub(engine, "https://cairn", "s", owners=lambda urn: [CAROL])
    mp = MarketplaceService(engine, graph, personas, hub, None, attrs)
    wh, calls = Warehouse(), []
    okta = Target(id="okta", type="okta", url="https://acme.okta.com", token="okta-token", user="{email}")
    mp.provisioner = Provisioner({"sf": SF, "okta": okta}, wh,
                                 lambda urn: {"username": urn.split(":")[-1], "email": "alice@acme.com"},
                                 http=httpx.Client(transport=okta_transport(calls)))
    return mp, hub, wh, calls


def test_approval_grants_and_expiry_takes_away(market):
    mp, hub, wh, calls = market
    with pytest.raises(MarketplaceError, match="no provisioning target"):
        mp.put_policy(PRODUCT, RequestPolicy(provision=[{"target": "nope", "role": "R"}]), CAROL)
    with pytest.raises(MarketplaceError, match="grants groups"):
        mp.put_policy(PRODUCT, RequestPolicy(provision=[{"target": "okta", "role": "R"}]), CAROL)
    mp.put_policy(PRODUCT, RequestPolicy(provision=[{"target": "sf", "role": "ORDERS_READ"},
                                                    {"target": "okta", "group": "00g9"}]), CAROL)
    r = mp.create_request(ALICE, AccessRequestIn(product_urn=PRODUCT, purpose="Analytics",
                                                 justification="quarterly numbers", duration_days=7))
    assert not wh.sql                                                       # nothing before approval
    out = mp.act(r["id"], "approve", CAROL)
    assert [p["status"] for p in out["provisioning"]] == ["granted", "granted"]
    assert wh.sql == ['GRANT ROLE "ORDERS_READ" TO USER "ALICE"'] and ("PUT", "/api/v1/groups/00g9/users/00u1") in calls
    mp.sweep(int(time.time() * 1000) + 8 * 86_400_000)
    ended = mp.get_request(r["id"])
    assert ended["state"] == "EXPIRED" and [p["status"] for p in ended["provisioning"]] == ["revoked", "revoked"]
    assert wh.sql[-1] == 'REVOKE ROLE "ORDERS_READ" FROM USER "ALICE"' and calls[-1][0] == "DELETE"


def test_failures_are_told_retried_and_revocation_undoes_only_what_was_granted(market):
    mp, hub, wh, _ = market
    mp.put_policy(PRODUCT, RequestPolicy(provision=[{"target": "sf", "role": "ORDERS_READ"}]), CAROL)
    r = mp.create_request(ALICE, AccessRequestIn(product_urn=PRODUCT, purpose="Analytics",
                                                 justification="quarterly numbers", duration_days=7))
    wh.fail = True
    out = mp.act(r["id"], "approve", CAROL)
    assert out["state"] == "APPROVED" and out["provisioning"][0]["status"] == "failed"   # approval stands
    told = [n for n in hub.inbox(CAROL) if n["type"] == "access.provisioning_failed"]
    assert len(told) == 1 and "Insufficient privileges" in told[0]["body"]
    mp.retry_provisioning()
    assert mp.get_request(r["id"])["provisioning"][0]["attempts"] == 2
    assert len([n for n in hub.inbox(CAROL) if n["type"] == "access.provisioning_failed"]) == 1   # told once
    for _ in range(MAX_ATTEMPTS):
        mp.retry_provisioning()
    assert mp.get_request(r["id"])["provisioning"][0]["attempts"] == MAX_ATTEMPTS                # then it stops
    revoked = mp.act(r["id"], "revoke", CAROL)
    assert revoked["provisioning"][0]["status"] == "skipped" and not wh.sql                  # never granted
    wh.fail = False
    r2 = mp.create_request(ALICE, AccessRequestIn(product_urn=PRODUCT, purpose="Analytics",
                                                  justification="again, for Q4 numbers", duration_days=7))
    mp.act(r2["id"], "approve", CAROL)
    wh.fail = True
    gone = mp.act(r2["id"], "revoke", CAROL)
    assert gone["provisioning"][0]["status"] == "failed"
    wh.fail = False
    mp.retry_provisioning()
    assert mp.get_request(r2["id"])["provisioning"][0]["status"] == "revoked"
    assert wh.sql[-1].startswith("REVOKE ROLE")
    assert json.dumps(mp.get_request(r2["id"])).count("okta-token") == 0
