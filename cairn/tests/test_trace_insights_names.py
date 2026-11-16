"""Agent trace intelligence: a custom agent's model use is metered by its name (review finding on L101; the analysis
itself is tested end to end in tests/test_agent_trace_insights.py)."""

from __future__ import annotations

import time

from sqlalchemy import create_engine, insert

from cairn.ai.custom_agents import custom_agents
from cairn.ai.trace_insights import DAY_MS, TraceInsights
from cairn.ai.usage import usage_table
from cairn.common.db import metadata
from cairn.identity import agents as _agents  # noqa: F401 - its tables
from cairn.ai import traces as _traces  # noqa: F401

ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"


class Hub:
    def __init__(self, agents):
        self.agents = agents

    def list(self):
        return self.agents


def test_a_reused_name_does_not_inherit_the_earlier_agents_model_use(tmp_path):
    """Review finding: Ana's deleted custom agent ``sales`` had model use metered as ``agent.sales``; Bob's new agent of
    the same name showed it (calls, tokens, cost) as its own."""
    e = create_engine(f"sqlite:///{tmp_path}/t.db")
    metadata.create_all(e)
    now = int(time.time() * 1000)
    born = now - DAY_MS
    with e.begin() as c:
        c.execute(insert(custom_agents).values(id="b1", name="sales", owner=BOB, agent_id="agt_bob", deployed=True,
                                               version=1, created_ms=born, updated_ms=born, description="",
                                               instructions="i", tools=["search"], scopes=["read"], max_steps=8))
        for ts, actor, cost in ((born - 3 * DAY_MS, ANA, 5.0), (born + 1000, BOB, 0.25)):
            c.execute(insert(usage_table).values(ts_ms=ts, actor=actor, task="agent.sales", provider="anthropic",
                                                 model="m", input_tokens=10, output_tokens=10, cost_usd=cost))
    ti = TraceInsights(e, Hub([{"id": "agt_bob", "name": "sales", "kind": "built", "owner": BOB, "enabled": True}]),
                       lambda actor, privilege: False)
    got = ti.agent(BOB, "agt_bob", days=7)
    assert got["model"]["calls"] == 1 and got["model"]["cost_usd"] == 0.25
    assert got["baseline"] is None


def test_an_owner_does_not_read_other_peoples_arguments(tmp_path):
    """Review finding: an agent's owner read, in the loops of ``/v1/agent-traces/insights/<id>``, the arguments of
    other people's calls (their search text, SQL), which the owner's activity view (``/me/agents/<id>/activity``)
    keeps to the owner's own calls. Administrators still see them."""
    from cairn.ai.traces import agent_calls

    e = create_engine(f"sqlite:///{tmp_path}/t.db")
    metadata.create_all(e)
    now = int(time.time() * 1000)
    with e.begin() as c:
        for who, run in ((BOB, "run_bob"), (ANA, "run_ana")):
            for i in range(4):
                c.execute(insert(agent_calls).values(
                    id=f"{run}{i}", actor=who, agent="agt_x", channel="mcp", tool="search_assets",
                    args=f'{{"query": "{who} secret"}}', status="ok", duration_ms=5, created_ms=now - 1000 + i,
                    run=run, flagged=0))
    hub = Hub([{"id": "agt_x", "name": "helper", "kind": "key", "owner": ANA, "enabled": True}])
    owner = TraceInsights(e, hub, lambda actor, privilege: False).agent(ANA, "agt_x")
    got = {lp["run"]: lp["args"] for lp in owner["loops_detail"]}
    assert got == {"run_bob": None, "run_ana": f'{{"query": "{ANA} secret"}}'}
    admin = TraceInsights(e, hub, lambda actor, privilege: True).agent(ANA, "agt_x")
    assert all(lp["args"] for lp in admin["loops_detail"])
