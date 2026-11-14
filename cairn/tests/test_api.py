import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app, create_pdp_app
from cairn.common.security import IdentityResolver
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

from conftest import DS

ALICE = {"X-Cairn-Actor": "urn:li:corpuser:alice"}
MALLORY = {"X-Cairn-Actor": "urn:li:corpuser:mallory"}
SERVICE = {"Authorization": "Bearer svc-secret"}


@pytest.fixture
def client(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(
        id="alice-all", effect="allow", actions=["*"],
        condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:alice"})]))
    graph.aspects[(DS, "datasetProfile")] = {"rowCount": 10}
    graph.timeseries[(DS, "datasetProfile")] = [{"timestampMillis": 1, "rowCount": 10}]
    return TestClient(create_api_app(s)), s


def test_health_metrics_and_auth(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/metrics").status_code == 200
    assert c.get("/quality/monitors").status_code == 401
    assert c.get("/me", headers=ALICE).json()["urn"] == "urn:li:corpuser:alice"


def test_monitor_crud_and_run(client):
    c, s = client
    body = {"name": "rows", "dataset_urn": DS, "type": "volume", "config": {"min_rows": 5}}
    assert c.post("/quality/monitors", json=body, headers=MALLORY).status_code == 403
    r = c.post("/quality/monitors", json=body, headers=ALICE)
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    ev = c.post(f"/quality/monitors/{mid}/run", headers=ALICE).json()
    assert ev["status"] == "PASS" and ev["metric_value"] == 10
    assert len(c.get(f"/quality/monitors/{mid}/evaluations", headers=ALICE).json()) == 1
    health = c.get(f"/quality/datasets/{DS}/health", headers=ALICE).json()
    assert health["status"] == "PASS" and health["monitors"][0]["monitor"]["id"] == mid
    assert c.get("/quality/monitors", headers=MALLORY).json() == []  # filtered by PDP
    assert c.post(f"/quality/evaluations/{ev['id']}/feedback", json={"feedback": "EXPECTED"},
                  headers=ALICE).json()["feedback"] == "EXPECTED"
    assert c.delete(f"/quality/monitors/{mid}", headers=ALICE).status_code == 204


def test_pdp_endpoint_service_vs_user(settings, graph):
    s = build_services(settings, graph=graph, with_gateway=False, with_ai=False)
    c = TestClient(create_pdp_app(s))
    req = {"actor": "urn:li:corpuser:bob", "privilege": "VIEW_ENTITY_PAGE",
           "resource": {"type": "dataset", "urn": DS},
           "resource_attributes": {"tags": ["urn:li:tag:Public"]}}
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="public", effect="allow", actions=["VIEW_ENTITY_PAGE"],
                                                   condition={"attr": "resource.tags", "op": "contains",
                                                              "value": "urn:li:tag:Public"})]))
    d = c.post("/governance/v1/authorize", json=req, headers=SERVICE).json()
    assert d["decision"] == "ALLOW" and d["policy_ids"] == ["public"]  # plugin-supplied attrs honoured
    assert c.post("/governance/v1/authorize", json=req, headers=ALICE).status_code == 403  # not self
    own = c.post("/governance/v1/authorize", json={**req, "actor": "urn:li:corpuser:alice"}, headers=ALICE).json()
    assert own["decision"] == "ALLOW"


def test_policy_management_and_audit(client):
    c, s = client
    bundle = {"policies": [{"id": "alice-all", "effect": "allow", "actions": ["*"],
                            "condition": {"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:alice"}},
                           {"id": "x", "effect": "deny", "actions": ["EDIT_*"],
                            "condition": {"attr": "resource.tags", "op": "contains", "value": "urn:li:tag:Frozen"}}]}
    assert c.post("/governance/policies/validate", json={"policies": [{"id": "bad id!", "effect": "allow", "actions": []}]},
                  headers=ALICE).json()["valid"] is False
    assert c.put("/governance/policies", json=bundle, headers=MALLORY).status_code == 403
    assert c.put("/governance/policies", json=bundle, headers=ALICE).json()["version"] == 1
    sim = c.post("/governance/simulate", headers=ALICE, json={
        "actor": "urn:li:corpuser:alice", "privilege": "EDIT_ENTITY_DOCS", "resource_urn": DS,
        "resource_attributes": {"tags": ["urn:li:tag:Frozen"]}}).json()
    assert sim["decision"] == "DENY" and any(t["policy_id"] == "x" and t["matched"] for t in sim["trace"])
    assert c.get("/governance/audit/verify", headers=ALICE).json()["valid"]


def test_workflow_endpoints(client, graph):
    c, s = client
    graph.aspects[(DS, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:carol"}]}
    graph.aspects[(DS, "editableDatasetProperties")] = {"description": "A sufficiently long description."}
    wf = c.post("/governance/workflows", json={"entity_urn": DS}, headers=ALICE)
    assert wf.status_code == 201, wf.text
    wid = wf.json()["id"]
    assert c.post(f"/governance/workflows/{wid}/approve", headers=ALICE).status_code == 403  # SoD
    s.identity.dev_trust_actor_header = True
    carol = {"X-Cairn-Actor": "urn:li:corpuser:carol"}
    s.pdp.set_bundle(PolicyBundle(policies=[*s.pdp.bundle.policies, Policy(
        id="carol-view", effect="allow", actions=["VIEW_ENTITY_PAGE"],
        condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:carol"})]))
    out = c.post(f"/governance/workflows/{wid}/approve", json={"comment": "ok"}, headers=carol).json()
    assert out["state"] == "CERTIFIED"


def test_agents_admin(client):
    c, _ = client
    assert c.post("/agents/pools", json={"name": "eu-prod", "allowed_platforms": ["snowflake"]},
                  headers=ALICE).status_code == 201
    tok = c.post("/agents/pools/eu-prod/enrollment-tokens", headers=ALICE).json()
    assert tok["token"].startswith("cairn_enr_")
    assert c.get("/agents/pools", headers=ALICE).json()[0]["agents_online"] == 0
    assert c.get("/agents", headers=MALLORY).status_code == 403
    # protocol endpoints are not mounted on the admin API
    assert c.post("/agents/enroll", json={}).status_code in (404, 405)


def test_identity_resolver_token_and_cookie_paths():
    calls = []

    def me(url, headers, cookies):
        calls.append((url, headers, cookies))
        if headers.get("Authorization") == "Bearer good" or cookies.get("PLAY_SESSION") == "s":
            return "urn:li:corpuser:jane"
        return None

    r = IdentityResolver("http://core:8080", "http://fe:9002", "svc", me_lookup=me)
    assert r.resolve({"authorization": "Bearer svc"}, {}).is_service
    assert r.resolve({"authorization": "Bearer good"}, {}).urn == "urn:li:corpuser:jane"
    r.resolve({"authorization": "Bearer good"}, {})
    assert len(calls) == 1  # cached
    assert r.resolve({}, {"PLAY_SESSION": "s"}).via == "session"
    assert calls[-1][0] == "http://fe:9002/api/v2/graphql"
    from cairn.common.security import AuthenticationError

    with pytest.raises(AuthenticationError):
        r.resolve({"authorization": "Bearer bad"}, {})
    with pytest.raises(AuthenticationError):
        r.resolve({"x-cairn-actor": "urn:li:corpuser:root"}, {})  # dev header disabled by default


def test_plugin_wire_format_structured_properties(settings, graph):
    """Mirror of the Java plugin's request body (plugins/java/cairn-authorizer/README.md)."""
    s = build_services(settings, graph=graph, with_gateway=False, with_ai=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(
        id="dept", effect="allow", actions=["VIEW_DATASET_PROFILE"],
        condition={"attr": "resource.structured_properties.io.cairn.department", "op": "contains",
                   "value": {"ref": "subject.attributes.department"}})]))
    graph.aspects[("urn:li:corpuser:bob", "corpUserInfo")] = {"departmentName": "finance"}
    c = TestClient(create_pdp_app(s))
    body = {"actor": "urn:li:corpuser:bob", "privilege": "VIEW_DATASET_PROFILE",
            "resource": {"type": "dataset", "urn": DS}, "sub_resources": [],
            "resource_attributes": {"owners": [], "domains": [], "tags": [], "glossary_terms": [], "containers": [],
                                    "platform_instances": [],
                                    "structured_properties": ["urn:li:structuredProperty:io.cairn.department=finance"]},
            "actor_groups": [], "environment": {"channel": "core", "mode": "additive"}}
    assert c.post("/governance/v1/authorize", json=body, headers=SERVICE).json()["decision"] == "ALLOW"
    body["resource_attributes"]["structured_properties"] = ["urn:li:structuredProperty:io.cairn.department=hr"]
    assert c.post("/governance/v1/authorize", json=body, headers=SERVICE).json()["decision"] == "NOT_APPLICABLE"
