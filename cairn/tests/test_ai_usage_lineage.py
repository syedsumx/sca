"""AI usage lineage: datasets read by AI (MCP tools, the assistant, agent keys over the API) are
recorded per day; a dataset shows which agents use it, and an AI system shows what its agents read
compared with what it declared."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cairn.ai.mcp_server import _principal, build_mcp_server
from cairn.ai.traces import TraceService, ai_channel, datasets_in
from cairn.app import create_api_app
from cairn.common.security import Principal
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.ai_systems import AISystemIn
from cairn.identity.agents import AgentIn, current_agent
from cairn.quality.collectors import ConnectionRegistry
from cairn.services import build_services
from conftest import DS

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
WH = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"


def test_reads_are_counted_per_day_and_grouped(engine):
    t = TraceService(engine)
    for _ in range(3):
        t.record_reads(ANA, "agt_a", "mcp", [DS, DS, "urn:li:glossaryTerm:x"], "metadata")
    t.record_reads(BOB, "agt_a", "api", [DS], "data")
    t.record_reads(BOB, None, "assistant", [DS, WH], "data")
    [a, person] = sorted(t.readers(DS), key=lambda r: r["agent"] or "~")
    assert a["agent"] == "agt_a" and a["metadata_reads"] == 3 and a["data_reads"] == 1 and a["people"] == 2
    assert a["days"] == 1 and a["actor"] is None
    assert person["agent"] is None and person["actor"] == BOB and person["channel"] == "assistant"
    assert t.datasets_read(agent="agt_a") == [{"dataset": DS, "data_reads": 1, "metadata_reads": 3, "days": 1,
                                               "last_ms": a["last_ms"]}]
    assert [d["dataset"] for d in t.datasets_read(actor=BOB)] == [DS, WH]
    assert t.datasets_read(agents=[]) == []
    assert datasets_in({"urn": DS, "x": [{"urns": [WH, "nope"]}], "n": 3}) == [DS, WH]


@pytest.fixture
def stack(settings, graph, tmp_path):
    url = f"sqlite:///{tmp_path}/wh.db"
    with create_engine(url).begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 2.5)"))
    for urn in (DS, WH):
        graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               condition={"attr": "subject.urn", "op": "exists"})]))
    s.data_access.registry = ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}})
    return s


def call(server, principal, tool, args):
    token = _principal.set(principal)
    try:
        return asyncio.run(server.call_tool(tool, args)).structured_content
    finally:
        _principal.reset(token)


def test_mcp_api_and_people_reads_feed_lineage_but_plain_use_does_not(stack):
    s = stack
    server, _ = build_mcp_server(s)
    bot = Principal(ANA, "agent", agent="agt_bot", scopes=("read",))
    call(server, bot, "get_quality_status", {"dataset_urn": DS})
    call(server, Principal(ANA, "agent", agent="agt_bot", scopes=()), "get_quality_status", {"dataset_urn": DS})
    s.data_access.query(BOB, "SELECT id FROM main.orders", "wh")                  # a person in the editor: not AI
    assert s.traces.readers(WH) == []
    token = current_agent.set("agt_key")                                      # an agent key over the REST API
    try:
        s.data_access.preview(BOB, WH)
    finally:
        current_agent.reset(token)
    token = ai_channel.set("assistant")                                       # the assistant reading data
    try:
        s.data_access.query(ANA, "SELECT count(*) FROM main.orders", "wh")
    finally:
        ai_channel.reset(token)
    assert [(r["agent"], r["metadata_reads"]) for r in s.traces.readers(DS)] == [("agt_bot", 1)]   # refused call: none
    by = {(r["agent"], r["channel"]): r for r in s.traces.readers(WH)}
    assert by[("agt_key", "api")]["data_reads"] == 1 and by[(None, "assistant")]["actor"] == ANA


def test_dataset_usage_api_and_the_ai_system_register(stack):
    s = stack
    system = s.ai_systems.create(AISystemIn(name="Order bot", purpose="answers order questions",
                                            datasets=[DS]), ANA)
    agent = s.agents.create_key_agent(AgentIn(name="Order bot key", scopes=["read"], owner=ANA,
                                              ai_system=system["id"]), ADMIN)
    s.traces.record_reads(ANA, agent["id"], "api", [DS, WH], "data")
    s.traces.record_reads(BOB, None, "mcp", [WH], "metadata")
    got = s.ai_systems.get(system["id"])
    assert {o["dataset"] for o in got["observed_datasets"]} == {DS, WH} and got["undeclared_datasets"] == [WH]
    assert s.ai_systems.report()["undeclared_use"] == [{"system": system["id"], "name": "Order bot", "datasets": [WH]}]

    c = TestClient(create_api_app(s))
    usage = c.get("/ai/lineage/readers", params={"urn": WH}, headers={"X-Cairn-Actor": ANA}).json()["readers"]
    bot = next(r for r in usage if r["agent"] == agent["id"])
    assert bot["agent_name"] == "Order bot key" and bot["ai_system"]["name"] == "Order bot"
    assert next(r for r in usage if r["agent"] is None)["actor"] is None     # other people stay anonymous
    admin_view = c.get("/ai/lineage/readers", params={"urn": WH}, headers={"X-Cairn-Actor": ADMIN}).json()["readers"]
    assert next(r for r in admin_view if r["agent"] is None)["actor"] == BOB
    assert c.get("/ai/lineage/readers", params={"urn": "urn:li:corpuser:x"}, headers={"X-Cairn-Actor": ANA}).status_code == 422
    assert c.get(f"/admin/agents/{agent['id']}/datasets", headers={"X-Cairn-Actor": ANA}).status_code == 403
    assert len(c.get(f"/admin/agents/{agent['id']}/datasets", headers={"X-Cairn-Actor": ADMIN}).json()) == 2
    assert len(c.get(f"/me/agents/{agent['id']}/datasets", headers={"X-Cairn-Actor": BOB}).json()) == 0
