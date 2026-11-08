"""Agent trace intelligence (L101): per agent over a window, runs, tool calls, error rate, loops, slow calls, model
cost and outliers against the agent's own baseline, as findings with severity.

Through the real services, decision point, database and HTTP API. The traces are real: tool calls made through
Cairn's MCP tools with a principal resolved from a real agent key, and a custom agent's real run (its model is a
scripted stand-in that asks for the tool calls a stuck model would, metered by Cairn's real usage meter). The
baseline is made of real recorded calls whose timestamps are moved back (the only way to have history in a test)."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from cairn.ai.mcp_server import _principal, build_mcp_server
from cairn.ai.traces import agent_calls
from cairn.ai.usage import MeteredProvider
from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.identity.agents import AgentIn
from cairn.services import build_services
from conftest import DS

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
DAY = 86_400_000


class Model:
    """Shaped like the Anthropic provider; each answer reports token usage, as the real API does."""

    name = "anthropic"

    def __init__(self):
        self.script: list[list[dict]] = []
        model = self

        class _Messages:
            def create(self, **kw):
                content = model.script.pop(0) if model.script else [{"type": "text", "text": "done"}]
                stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
                usage = type("U", (), {"input_tokens": 20_000, "output_tokens": 2_000})()
                return type("R", (), {"content": content, "stop_reason": stop, "model": "claude-sonnet-5-5",
                                      "usage": usage})()

        self.client = type("C", (), {"messages": _Messages()})()


@pytest.fixture
def stack(settings, graph, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    model = Model()
    s.assistant.llm = MeteredProvider(model, s.ai_usage)
    return s, model


def mcp(server, principal, tool, args):
    token = _principal.set(principal)
    try:
        return asyncio.run(server.call_tool(tool, args))
    except Exception:  # noqa: BLE001 - a tool that fails is traced as an error; that is what is checked
        return None
    finally:
        _principal.reset(token)


def test_insights_find_loops_errors_cost_and_outliers_scoped_to_owners(stack):
    s, model = stack
    server, _ = build_mcp_server(s)
    c = TestClient(create_api_app(s))
    admin, ana, bob = ({"X-Cairn-Actor": u} for u in (ADMIN, ANA, BOB))
    helper = s.agents.create_key_agent(AgentIn(name="Helper", scopes=["read"], owner=ANA), ADMIN)
    bot = s.identity.resolve({"authorization": f"Bearer {helper['key']}"}, {})
    assert bot.via == "agent" and bot.agent == helper["id"] and bot.run is None

    # Its baseline: two healthy calls a day, 8 to 20 days ago (real calls, moved back in time).
    for i in range(26):
        mcp(server, bot, "get_quality_status", {"dataset_urn": f"{DS[:-6]}_{i},PROD)"})
    with s.engine.begin() as conn:
        ids = [r.id for r in conn.execute(select(agent_calls.c.id).where(agent_calls.c.agent == helper["id"]))]
        assert len(ids) == 26
        for i, cid in enumerate(ids):
            conn.execute(update(agent_calls).where(agent_calls.c.id == cid)
                         .values(created_ms=agent_calls.c.created_ms - (8 + i // 2) * DAY))
    # This week: three times the volume, and half of it failing (catalog search is not configured here).
    for i in range(20):
        mcp(server, bot, "get_quality_status", {"dataset_urn": f"{DS[:-6]}_n{i},PROD)"})
        mcp(server, bot, "search_assets", {"query": f"revenue {i}"})

    # A custom agent stuck calling the same tool with the same arguments in one run.
    s.custom_agents.create(ADMIN, {"name": "looper", "description": "", "instructions": "Check quality.",
                                   "tools": ["get_quality_status"], "scopes": ["read"], "max_steps": 8})
    s.custom_agents.deploy(ADMIN, "looper")
    looper = s.custom_agents.get(ADMIN, "looper")["agent_id"]
    step = {"type": "tool_use", "id": "tu_1", "name": "get_quality_status", "input": {"dataset_urn": DS}}
    model.script = [[step]] * 5 + [[{"type": "text", "text": "Quality is fine."}]]
    run = s.custom_agents.run(ADMIN, "looper", "how is orders?")
    assert run["status"] == "ok" and len(run["calls"]) == 5
    assert {a["run"] for a in s.traces.activity(agent=looper)} == {run["run"]}       # each call names its run

    out = c.get("/v1/agent-traces/insights?days=7", headers=admin)
    assert out.status_code == 200, out.text
    body = out.json()
    agents = {a["agent"]: a for a in body["agents"]}
    assert set(agents) == {helper["id"], looper}
    h = agents[helper["id"]]
    assert (h["tool_calls"], h["errors"], h["error_rate"], h["loops"]) == (40, 20, 0.5, 0)
    assert h["runs"] == 1 and h["baseline"]["tool_calls"] == 26 and h["baseline"]["error_rate"] == 0
    lp = agents[looper]
    assert (lp["tool_calls"], lp["runs"], lp["loops"]) == (5, 1, 1)
    # Six model turns of 20k in / 2k out at $2 / $10 per million tokens.
    assert lp["model"]["calls"] == 6 and lp["model"]["cost_usd"] == pytest.approx(6 * 0.06)
    assert lp["cost_per_run_usd"] == pytest.approx(0.36)
    kinds = {(f["agent"], f["kind"]): f for f in body["findings"]}
    assert kinds[(helper["id"], "error_rate")]["severity"] == "critical"
    assert kinds[(helper["id"], "error_outlier")]["severity"] == "critical"
    vol = kinds[(helper["id"], "volume_outlier")]
    # 26 calls over the 21 days before the window (1.24 a day) against 40 in 7 days (5.71 a day): 4.6x.
    assert vol["severity"] == "critical" and vol["baseline"] == pytest.approx(26 / 21, abs=0.01)
    loop = kinds[(looper, "loop")]
    assert loop["severity"] == "warning" and loop["run"] == run["run"] and loop["value"] == 5
    assert kinds[(looper, "no_baseline")]["severity"] == "info"
    assert body["findings"][0]["severity"] == "critical"                   # most severe first
    assert body["totals"]["tool_calls"] == 45 and body["totals"]["critical"] >= 2

    # One agent in detail: per tool, its runs with loops first, the loop's arguments (redacted), a daily series.
    one = c.get(f"/v1/agent-traces/insights/{looper}?days=7", headers=admin).json()
    assert one["tools"] == [{"tool": "get_quality_status", "calls": 5, "errors": 0, "refused": 0,
                             "p95_ms": one["tools"][0]["p95_ms"], "avg_ms": one["tools"][0]["avg_ms"]}]
    assert one["runs_detail"][0]["run"] == run["run"] and one["runs_detail"][0]["loops"] == 1
    assert DS in one["loops_detail"][0]["args"] and len(one["daily"]) == 7
    assert sum(d["cost_usd"] for d in one["daily"]) == pytest.approx(0.36)

    # Owners see their own agents; others see none; agents never read it.
    assert [a["agent"] for a in c.get("/v1/agent-traces/insights", headers=ana).json()["agents"]] == [helper["id"]]
    assert c.get(f"/v1/agent-traces/insights/{looper}", headers=ana).status_code == 404
    assert c.get("/v1/agent-traces/insights", headers=bob).json()["agents"] == []
    assert c.get(f"/v1/agent-traces/insights/{helper['id']}", headers=bob).status_code == 404
    assert c.get("/v1/agent-traces/insights",
                 headers={"Authorization": f"Bearer {helper['key']}"}).status_code == 403
    assert c.get("/v1/agent-traces/insights?days=0", headers=admin).status_code == 422

    # Thresholds: administrators only, validated, audited, and they change the analysis.
    assert c.put("/v1/agent-traces/settings", json={"loop_threshold": 6}, headers=ana).status_code == 403
    assert c.put("/v1/agent-traces/settings", json={"loop_threshold": 1}, headers=admin).status_code == 422
    assert c.put("/v1/agent-traces/settings", json={"nope": 1}, headers=admin).status_code == 422
    got = c.put("/v1/agent-traces/settings", json={"loop_threshold": 6, "slow_call_ms": 1}, headers=admin)
    assert got.status_code == 200 and got.json()["loop_threshold"] == 6
    assert c.get("/v1/agent-traces/settings", headers=ana).json()["slow_call_ms"] == 1
    again = c.get(f"/v1/agent-traces/insights/{looper}", headers=admin).json()
    assert again["loops"] == 0 and not [f for f in again["findings"] if f["kind"] == "loop"]
    slow = [a for a in s.traces.activity(agent=looper) if a["duration_ms"] >= 1]
    assert again["slow_calls"] == len(slow)
    assert bool([f for f in again["findings"] if f["kind"] == "slow_calls"]) == bool(slow)
    entries = [e for e in s.audit.export(category="agents") if e["action"] == "agent_traces.settings_changed"]
    assert len(entries) == 1 and entries[0]["actor"] == ADMIN and entries[0]["details"]["loop_threshold"] == 6


def test_run_tokens_and_oauth_tokens_name_their_run(stack):
    s, _ = stack
    server, _ = build_mcp_server(s)
    s.custom_agents.create(ADMIN, {"name": "runner", "description": "", "instructions": "x",
                                   "tools": ["get_quality_status"], "scopes": ["read"], "max_steps": 2})
    agent_id = s.custom_agents.get(ADMIN, "runner")["agent_id"]
    tokens = [s.agents.issue_run_token(agent_id, ANA) for _ in range(2)]
    principals = [s.identity.resolve({"authorization": f"Bearer {t}"}, {}) for t in tokens]
    assert all(p.run and p.run.startswith("run_") for p in principals) and principals[0].run != principals[1].run
    for p in principals:
        for _ in range(4):
            mcp(server, p, "get_quality_status", {"dataset_urn": DS})
    body = s.trace_insights.agent(ADMIN, agent_id, 1)
    assert body["runs"] == 2 and body["loops"] == 2                      # four identical calls in each run
    assert {r["run"] for r in body["runs_detail"]} == {p.run for p in principals}
