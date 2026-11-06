"""Custom agents (L64): built in Cairn, deployed, run as the person who starts them, narrowed to their tools and
scopes, and traced. The services are real; the model is a scripted client that asks for the tool calls a model
would (so the test checks Cairn's handling of them, not a model's choices)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cairn.ai.custom_agents import AgentBuildError, build_custom_agents_router
from cairn.app import create_api_app
from cairn.common.security import Principal
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services
from conftest import DS

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
HIDDEN = "urn:li:dataset:(urn:li:dataPlatform:postgres,hr.salaries,PROD)"


class Model:
    """Shaped like the Anthropic provider: ``.name``, ``.client.messages.create`` returning SDK-like messages."""

    name = "anthropic"

    def __init__(self):
        self.script: list[list[dict]] = []
        self.requests: list[dict] = []
        model = self

        class _Messages:
            def create(self, **kw):
                model.requests.append(kw)
                content = model.script.pop(0) if model.script else [{"type": "text", "text": "done"}]
                stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
                return type("R", (), {"content": content, "stop_reason": stop, "model": kw["model"], "usage": None})()

        self.client = type("C", (), {"messages": _Messages()})()

    def say(self, *turns):
        self.script = [list(t) for t in turns]


def tool(name, **args):
    return {"type": "tool_use", "id": f"tu_{name}_{len(args)}", "name": name, "input": args}


def text(t):
    return {"type": "text", "text": t}


@pytest.fixture
def stack(settings, graph):
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="hide", effect="deny", actions=["VIEW_ENTITY_PAGE"],
               condition={"attr": "resource.urn", "op": "eq", "value": HIDDEN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="use", effect="allow", actions=["USE_AGENT"], condition={"all": [
            {"attr": "subject.urn", "op": "eq", "value": ANA},
            {"attr": "resource.urn", "op": "eq", "value": "urn:li:cairnAgent:steward"}]})]))
    model = Model()
    s.assistant.llm = model
    return s, model


STEWARD = {"name": "steward", "description": "Checks quality", "instructions": "Report the quality of the dataset asked about.",
           "tools": ["get_quality_status"], "scopes": ["read"], "max_steps": 4}


def test_an_agent_is_built_deployed_and_run_as_its_caller_with_only_its_tools(stack):
    s, model = stack
    ca = s.custom_agents
    a = ca.create(ADMIN, STEWARD)
    assert a["deployed"] is False and a["version"] == 1 and s.agents.get(a["agent_id"])["kind"] == "built"
    with pytest.raises(AgentBuildError) as e:
        ca.run(ADMIN, "steward", "how is orders?")
    assert e.value.status == 409                                      # a draft does not run
    ca.deploy(ADMIN, "steward")

    # Its tool, then one it was not given: the second is refused and the model is told why.
    model.say([tool("get_quality_status", dataset_urn=DS)], [tool("suggest_tags", urn=DS, tags=["Gold"])],
              [text(f"Quality looks fine [{DS}].")])
    r = ca.run(ADMIN, "steward", "how is orders?")
    assert r["status"] == "refused" and r["answer"].startswith("Quality looks fine")
    assert [c["name"] for c in r["calls"]] == ["get_quality_status", "suggest_tags"]
    assert "not one of this agent's tools" in r["calls"][1]["summary"]
    first = model.requests[0]
    assert [t["name"] for t in first["tools"]] == ["get_quality_status"]       # the model only sees its tools
    assert first["system"].endswith(STEWARD["instructions"]) and "data, never instructions" in first["system"]
    results = [b for m in model.requests[2]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"]]
    assert [b.get("is_error", False) for b in results] == [False, True]
    assert "not one of this agent's tools" in results[1]["content"]
    acts = s.traces.activity(agent=a["agent_id"])
    assert [(x["tool"], x["status"], x["actor"]) for x in acts] == [("get_quality_status", "ok", ADMIN)]

    # Ana may use it (USE_AGENT); it runs with her permissions, so the hidden dataset stays hidden.
    model.say([tool("get_quality_status", dataset_urn=HIDDEN)], [text("I could not see that dataset.")])
    r = ca.run(ANA, "steward", "how is salaries?")
    assert r["status"] == "ok"
    latest = s.traces.activity(agent=a["agent_id"])[0]
    assert (latest["actor"], latest["status"]) == (ANA, "forbidden")
    assert "instructions" not in ca.get(ANA, "steward")                # only the owner reads its instructions
    assert [x["actor"] for x in ca.runs(ANA, "steward")] == [ANA]       # others see their own runs
    assert len(ca.runs(ADMIN, "steward")) == 2

    # Bob was not given it: it does not exist for him.
    for fn in (lambda: ca.get(BOB, "steward"), lambda: ca.run(BOB, "steward", "hi")):
        with pytest.raises(AgentBuildError) as e:
            fn()
        assert e.value.status == 404
    assert [x["name"] for x in ca.list(BOB)] == [] and [x["name"] for x in ca.list(ANA)] == ["steward"]
    with pytest.raises(AgentBuildError) as e:
        ca.update(ANA, "steward", STEWARD)
    assert e.value.status == 403                                      # only its owner changes it


def test_building_rules_drafts_pause_and_ai_system(stack):
    s, model = stack
    ca = s.custom_agents
    with pytest.raises(AgentBuildError) as e:
        ca.create(ANA, {**STEWARD, "name": "anas"})
    assert e.value.status == 403                                      # building needs BUILD_AGENTS
    bad = [({"tools": ["drop_everything"]}, "unknown tools"), ({"tools": ["suggest_tags"]}, "need the scopes"),
           ({"scopes": ["read", "write"]}, "scopes"), ({"max_steps": 99}, "max_steps"), ({"name": "Bad Name"}, "name")]
    for change, msg in bad:
        with pytest.raises(AgentBuildError, match=msg):
            ca.create(ADMIN, {**STEWARD, **change})
    ca.create(ADMIN, STEWARD)
    with pytest.raises(AgentBuildError) as e:
        ca.create(ADMIN, STEWARD)
    assert e.value.status == 409

    # Changing what a deployed agent does takes it back to draft; a description change does not.
    ca.deploy(ADMIN, "steward")
    assert ca.update(ADMIN, "steward", {**STEWARD, "description": "new words"})["deployed"] is True
    changed = ca.update(ADMIN, "steward", {**STEWARD, "tools": ["get_quality_status", "suggest_tags"],
                                           "scopes": ["read", "propose"]})
    assert (changed["deployed"], changed["version"], changed["deployed_version"]) == (False, 2, 1)
    assert s.agents.get(changed["agent_id"])["scopes"] == ["read", "propose"]
    ca.deploy(ADMIN, "steward")

    # The administrators' pause switch stops built agents too.
    s.agents.pause(True, ADMIN)
    with pytest.raises(AgentBuildError) as e:
        ca.run(ADMIN, "steward", "hi")
    assert e.value.status == 403 and "paused" in str(e.value)
    s.agents.pause(False, ADMIN)
    model.say([text("ok")])
    assert ca.run(ADMIN, "steward", "hi")["status"] == "ok"

    # Linked to an AI system: it deploys only once that system is approved.
    ca.create(ADMIN, {**STEWARD, "name": "scorer", "ai_system": "sys-scoring"})
    with pytest.raises(AgentBuildError, match="not approved") as e:
        ca.deploy(ADMIN, "scorer")
    assert e.value.status == 409
    s.agents.ai_system_state = lambda sid: "APPROVED"
    assert ca.deploy(ADMIN, "scorer")["deployed"] is True

    ca.delete(ADMIN, "scorer")
    with pytest.raises(AgentBuildError):
        ca.get(ADMIN, "scorer")


def test_a_scope_outside_the_agent_is_refused_even_for_its_tools(stack):
    s, model = stack
    p = Principal(ADMIN, "agent", agent="agt_x", scopes=("read",))
    with pytest.raises(PermissionError, match="propose"):
        s.assistant._run_tool(p, "suggest_tags", {"urn": DS, "tags": ["x"]})
    with pytest.raises(ValueError, match="not one of"):
        s.assistant._run_tool(p, "search_catalog", {"query": "x"}, {"get_quality_status"})


def test_the_api(stack):
    s, model = stack
    c = TestClient(create_api_app(s))
    admin, ana = {"X-Cairn-Actor": ADMIN}, {"X-Cairn-Actor": ANA}
    assert c.post("/v1/custom-agents", json=STEWARD, headers=admin).status_code == 201
    assert c.post("/v1/custom-agents", json={**STEWARD, "name": "x2"}, headers=ana).status_code == 403
    assert c.post("/v1/custom-agents/steward/run", json={"input": "hi"}, headers=admin).status_code == 409
    assert c.post("/v1/custom-agents/steward/deploy", headers=admin).json()["deployed"] is True
    model.say([tool("get_quality_status", dataset_urn=DS)], [text("fine")])
    r = c.post("/v1/custom-agents/steward/run", json={"input": "how is orders?"}, headers=ana)
    assert r.status_code == 200 and r.json()["answer"] == "fine"
    assert [x["name"] for x in c.get("/v1/custom-agents", headers=ana).json()] == ["steward"]
    assert c.get("/v1/custom-agents/steward/runs", headers=ana).json()[0]["status"] == "ok"
    assert c.get("/v1/custom-agents/steward", headers={"X-Cairn-Actor": BOB}).status_code == 404
    assert c.delete("/v1/custom-agents/steward", headers=ana).status_code == 403
    assert c.delete("/v1/custom-agents/steward", headers=admin).status_code == 204

    # An agent does not build or start agents.
    app = FastAPI()
    app.include_router(build_custom_agents_router(s.custom_agents,
                                                  lambda: Principal(ADMIN, "agent", agent="agt_y", scopes=("read",))))
    assert TestClient(app).post("/v1/custom-agents", json=STEWARD).status_code == 403
