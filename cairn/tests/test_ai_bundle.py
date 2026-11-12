"""The AI setup as code (L158), end to end through Cairn's real API (in-process): examples/bundles/ai-agents.yaml,
over a Cairn whose gateway serves models from cloud upstreams configured as the Helm chart and the Terraform modules
render them, is planned (all new), applied, planned again (nothing to do), rotated (a secret's version), and pruned.
What it made is checked where it lives: the policies decide, the MCP server's credential is a reference and the
value is in the secret scope, the route's candidates, and the agent, which matches examples/agents/quality-triage."""

import json
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.pdp import AuthorizeRequest, ResourceRef
from cairn.iac import bundle as b
from iac_world import ADMIN, TOKEN, build

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(HERE, "..", "examples", "bundles", "ai-agents.yaml")
AGENT = os.path.join(HERE, "..", "examples", "agents", "quality-triage", "agent.yaml")
# What helm/cairn renders from cairn.aiGateway.cloudUpstreams (and the Terraform modules pass to it). Nothing here
# calls a cloud: the gateway only has to serve these names for routes to be declared over them.
CLOUDS = [{"name": "bedrock-us", "kind": "bedrock", "region": "us-east-1",
           "models": {"fast-small": "anthropic.fast-small-v1:0", "big-model": "anthropic.big-model-v1:0"}}]
VARS = {"ENV": "test", "FAST_MODEL": "fast-small", "FALLBACK_MODEL": "big-model",
        "TRACKER_MCP_URL": "https://mcp.example.com/mcp", "TRACKER_MCP_TOKEN": "tracker-token-1"}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    # The cloud upstreams are what is served (an empty model list: each cloud's models are listed).
    s = build(tmp_path_factory.mktemp("ai-bundle"),
              env={"CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS": json.dumps(CLOUDS), "CAIRN_AI_GATEWAY_MODELS": ""})
    http = TestClient(create_api_app(s), headers={"Authorization": f"Bearer {TOKEN}"})
    return s, b.Client("", "unused", http=http), http


def pending(changes) -> list[str]:
    return [x.line() for x in changes if x.action != "keep"]


def allowed(s, privilege: str, urn: str, groups: list[str]) -> bool:
    req = AuthorizeRequest(actor="urn:li:corpuser:dee", privilege=privilege, actor_groups=groups,
                           resource=ResourceRef(type=urn.split(":")[2], urn=urn))
    return s.pdp.decide(req, record=False).decision == "ALLOW"


def test_the_bundle_agent_is_the_example_agent():
    doc = yaml.safe_load(open(BUNDLE))
    agent = yaml.safe_load(open(AGENT))
    (entry,) = doc["custom_agents"]
    assert entry == agent, "examples/bundles/ai-agents.yaml and examples/agents/quality-triage/agent.yaml differ"


def test_the_ai_setup_round_trips_as_a_bundle(world, tmp_path):
    s, c, http = world
    assert {"fast-small", "big-model"} <= set(s.ai_gateway.models)     # served from the cloud upstream

    spec = b.load(BUNDLE, VARS)
    assert spec["bundle"] == "ai-agents-test"
    changes, state = b.plan(c, spec)
    made = sorted((x.kind, x.action) for x in changes if x.action != "keep")
    assert ("mcp_servers", "create") in made and ("ai_gateway_routes", "create") in made
    assert ("custom_agents", "create") in made and ("secrets", "create") in made
    assert sum(1 for k, _ in made if k == "policies") == 3, made
    b.apply(c, spec, changes, state, log=lambda _: None)
    assert pending(b.plan(c, spec)[0]) == []                        # applied: nothing left to do

    # Who may use what: the bundle's policies, decided by the policy engine.
    for privilege, urn in (("USE_AI_GATEWAY", "urn:li:aiModel:fast-small"),
                           ("USE_MCP_SERVER", "urn:li:mcpServer:tracker"),
                           ("USE_AGENT", "urn:li:cairnAgent:quality-triage")):
        assert allowed(s, privilege, urn, ["data-engineers"]), privilege
        assert not allowed(s, privilege, urn, ["sales"]), privilege
    assert not allowed(s, "USE_MCP_SERVER", "urn:li:mcpServer:other", ["data-engineers"])

    # The MCP server holds a reference; the token is in the secret scope, never in Cairn's record of the server.
    server = http.get("/v1/ai/mcp-servers/tracker").json()
    assert server["auth"] == {"type": "bearer", "scope": "mcp-credentials", "key": "tracker"}
    assert "tracker-token-1" not in json.dumps(server)
    assert s.secret_scopes.read(ADMIN, "mcp-credentials", "tracker") == "tracker-token-1"
    assert server["deny_tools"] == ["delete_*"] and server["rpm"] == 120

    # The route over the cloud's models; the agent as in examples/agents.
    routes = {r["name"]: r for r in http.get("/v1/ai/gateway/routes").json()["routes"]}
    assert [x["model"] for x in routes["fast"]["candidates"]] == ["fast-small", "big-model"]
    agent = http.get("/v1/custom-agents/quality-triage").json()
    example = yaml.safe_load(open(AGENT))
    for f in ("instructions", "tools", "scopes", "max_steps", "description"):
        assert agent[f] == example[f], f

    # A rotated token: only the secret's version changes, and Cairn holds the new value.
    doc = yaml.safe_load(open(BUNDLE))
    doc["secrets"][0]["value_version"] = 2
    path = tmp_path / "ai.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    spec = b.load(str(path), {**VARS, "TRACKER_MCP_TOKEN": "tracker-token-2"})
    changes, state = b.plan(c, spec)
    assert [(x.kind, sorted(x.diff)) for x in changes if x.action != "keep"] == [("secrets", ["value_version"])]
    b.apply(c, spec, changes, state, log=lambda _: None)
    assert s.secret_scopes.read(ADMIN, "mcp-credentials", "tracker") == "tracker-token-2"

    # Another environment's models are a variable away: the route changes in place.
    spec = b.load(str(path), {**VARS, "TRACKER_MCP_TOKEN": "tracker-token-2", "FAST_MODEL": "big-model",
                              "FALLBACK_MODEL": "fast-small"})
    changes, state = b.plan(c, spec)
    assert [(x.kind, x.action) for x in changes if x.action != "keep"] == [("ai_gateway_routes", "update")]
    b.apply(c, spec, changes, state, log=lambda _: None)

    # Dropped from git and pruned: the agent and the server go; nothing made by hand is touched.
    doc["custom_agents"], doc["mcp_servers"] = [], []
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    spec = b.load(str(path), {**VARS, "TRACKER_MCP_TOKEN": "tracker-token-2", "FAST_MODEL": "big-model",
                              "FALLBACK_MODEL": "fast-small"})
    changes, state = b.plan(c, spec, prune=True)
    assert sorted((x.kind, x.action) for x in changes if x.action != "keep") == [
        ("custom_agents", "delete"), ("mcp_servers", "delete")]
    b.apply(c, spec, changes, state, log=lambda _: None)
    assert http.get("/v1/ai/mcp-servers/tracker").status_code == 404
    assert http.get("/v1/custom-agents/quality-triage").status_code == 404
    assert pending(b.plan(c, spec, prune=True)[0]) == []


def test_a_route_over_a_model_nobody_serves_is_refused(world):
    _, c, http = world
    spec = b.parse({"bundle": "ai-bad", "ai_gateway_routes": [{"name": "nowhere", "candidates": [{"model": "not-served"}]}]})
    changes, state = b.plan(c, spec)
    with pytest.raises(Exception) as e:
        b.apply(c, spec, changes, state, log=lambda _: None)
    assert "not-served" in str(e.value)
    assert "nowhere" not in {r["name"] for r in http.get("/v1/ai/gateway/routes").json()["routes"]}
