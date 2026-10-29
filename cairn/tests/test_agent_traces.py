"""Agent observability: every tool call traced (redacted), per-agent activity and stats, and
feedback that turns into evaluation cases."""

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

from cairn.ai.mcp_server import _principal, build_mcp_server
from cairn.app import create_api_app
from cairn.common.security import Principal
from cairn.governance.abac import Policy, PolicyBundle
from cairn.identity.agents import AgentIn
from cairn.services import build_services
from conftest import DS

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
HIDDEN = "urn:li:dataset:(urn:li:dataPlatform:postgres,hr.salaries,PROD)"


@pytest.fixture
def stack(settings, graph):
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="hide", effect="deny", actions=["VIEW_ENTITY_PAGE"],
               condition={"attr": "resource.urn", "op": "eq", "value": HIDDEN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    return s


def call(server, principal, tool, args):
    token = _principal.set(principal)
    try:
        return asyncio.run(server.call_tool(tool, args)).structured_content
    finally:
        _principal.reset(token)


def test_tool_calls_are_traced_with_outcomes(stack):
    s = stack
    server, _ = build_mcp_server(s)
    bot = Principal(ANA, "agent", agent="agt_bot", scopes=("read", "propose"))
    call(server, bot, "get_quality_status", {"dataset_urn": DS})
    call(server, bot, "suggest_tags", {"urn": DS, "tags": ["Gold"], "rationale": "asked by ana@example.com"})
    call(server, bot, "get_quality_status", {"dataset_urn": HIDDEN})
    call(server, Principal(ANA, "agent", agent="agt_bot", scopes=("read",)), "suggest_tags", {"urn": DS, "tags": ["x"]})
    call(server, Principal(BOB, "session"), "get_quality_status", {"dataset_urn": DS})
    acts = s.traces.activity(agent="agt_bot")
    assert [a["tool"] for a in acts][::-1] == ["get_quality_status", "suggest_tags", "get_quality_status"]
    assert [a["status"] for a in acts][::-1] == ["ok", "ok", "forbidden"]
    assert "[REDACTED_EMAIL]" in acts[1]["args"] and "ana@example.com" not in acts[1]["args"]   # no personal data
    # A call refused by scope never reaches the tool (and is not traced as a tool call).
    stats = s.traces.stats()
    assert stats["by_agent"]["agt_bot"]["calls"] == 3 and stats["by_agent"]["agt_bot"]["refused"] == 1
    assert stats["by_agent"]["assistant"]["calls"] == 1 and stats["by_tool"]["get_quality_status"]["calls"] == 3


def test_activity_api_admins_see_all_people_see_their_own(stack):
    s = stack
    server, _ = build_mcp_server(s)
    agent = s.agents.create_key_agent(AgentIn(name="Helper", scopes=["read"], owner=ANA), ADMIN)
    call(server, Principal(ANA, "agent", agent=agent["id"], scopes=("read",)), "get_quality_status", {"dataset_urn": DS})
    call(server, Principal(BOB, "agent", agent=agent["id"], scopes=("read",)), "get_quality_status", {"dataset_urn": DS})
    c = TestClient(create_api_app(s))
    assert c.get(f"/admin/agents/{agent['id']}/activity", headers={"X-Cairn-Actor": ANA}).status_code == 403
    assert len(c.get(f"/admin/agents/{agent['id']}/activity", headers={"X-Cairn-Actor": ADMIN}).json()) == 2
    mine = c.get(f"/me/agents/{agent['id']}/activity", headers={"X-Cairn-Actor": ANA}).json()
    assert [m["actor"] for m in mine] == [ANA]
    assert c.get("/admin/agents/stats", headers={"X-Cairn-Actor": ADMIN}).json()["by_agent"][agent["id"]]["calls"] == 2


def test_feedback_becomes_eval_cases(stack):
    s = stack
    c = TestClient(create_api_app(s))
    ana, admin = {"X-Cairn-Actor": ANA}, {"X-Cairn-Actor": ADMIN}
    assert c.post("/ai/feedback", json={"kind": "chat", "rating": "up", "question": "who owns orders?",
                                         "answer": "Ana."}, headers=ana).status_code == 201
    assert c.post("/ai/feedback", json={"kind": "ask", "rating": "down", "question": "revenue last week for bob@x.io",
                                         "answer": "42", "comment": "used gross, not net",
                                         "context": {"sql": "select sum(amount) from public.orders"}},
                  headers=ana).status_code == 201
    assert c.post("/ai/feedback", json={"kind": "chat", "rating": "meh", "question": "q"}, headers=ana).status_code == 422
    assert c.get("/ai/feedback", headers=ana).status_code == 403
    out = c.get("/ai/feedback", headers=admin).json()
    assert out["summary"] == {"chat": {"up": 1, "down": 0}, "ask": {"up": 0, "down": 1}}
    cases = yaml.safe_load(c.get("/ai/feedback/eval-cases", headers=admin).text)["cases"]
    assert len(cases) == 1 and cases[0]["feature"] == "text2sql" and "bob@x.io" not in cases[0]["question"]
    assert cases[0]["rejected_sql"].startswith("select sum") and "used gross" in cases[0]["notes"]
    assert any(e["action"] == "ai.feedback" for e in s.audit.export(category="ai"))
