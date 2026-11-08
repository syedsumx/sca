"""Intent-based security for agents (L109): an agent run or credential declares an intent (a purpose, the action
categories it allows, the tables and domains it concerns); every agent request is classified and refused (403,
audited as ``agent.intent_denied``) when it falls outside. Through the real services, decision point, database, a
real SQLite warehouse and the HTTP API, with real agent keys, run tokens and a custom agent's run (its model is a
scripted stand-in asking for the tool calls a model would)."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cairn.ai.mcp_server import _principal, build_mcp_server
from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.identity.agents import AgentIn
from cairn.identity.intents import IntentError, classify_request, classify_sql, parse_intent
from cairn.services import build_services

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
WEEKLY = {"purpose": "weekly revenue summary", "actions": ["read", "query"], "tables": ["main.orders"]}


@pytest.fixture
def stack(settings, graph, monkeypatch, tmp_path):
    wh = tmp_path / "wh.db"
    e = create_engine(f"sqlite:///{wh}")
    with e.begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, amount REAL)"))
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT)"))
        c.execute(text("INSERT INTO orders VALUES (1, 9.5), (2, 3.0)"))
        c.execute(text("INSERT INTO users VALUES (1, 'a@example.com')"))
    conns = tmp_path / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    monkeypatch.setenv("CAIRN_QUALITY_CONNECTIONS", str(conns))
    for urn, name in ((ORDERS, "orders"), (USERS, "users")):
        graph.aspects[(urn, "datasetProperties")] = {"name": name}
        graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    graph.aspects[(USERS, "domains")] = {"domains": ["urn:li:domain:crm"]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="people", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "EDIT_ENTITY_DOCS",
                                                     "EDIT_ENTITY_TAGS", "EXPORT_DATASET"],
               condition={"attr": "subject.urn", "op": "exists"})]))
    return s, TestClient(create_api_app(s))


def denials(s):
    return [e for e in s.audit.export(category="agents") if e["action"] == "agent.intent_denied"]


def test_classification():
    assert classify_sql("SELECT * FROM a.b") == "query"
    assert classify_sql("-- note; DROP\nWITH x AS (SELECT 1) SELECT * FROM x") == "query"
    assert classify_sql("insert into a.b values (1)") == "write"
    assert classify_sql("SELECT 1; DELETE FROM a.b") == "write"                 # the most powerful statement counts
    assert classify_sql("GRANT SELECT ON a.b TO bob") == "admin"
    assert classify_sql("UNLOAD ('select 1') TO 's3://x/'") == "export"
    assert classify_sql("SELECT 'drop table x' AS s FROM a.b") == "query"       # words inside strings do not count
    assert classify_sql("frobnicate the warehouse") == "admin"                  # unknown: never through by accident
    assert classify_request("GET", "/v1/datasets/{urn}") == "read"
    assert classify_request("POST", "/data/query") == "query"
    assert classify_request("PUT", "/collab/bookmarks/{urn:path}") == "write"
    assert classify_request("GET", "/context/definitions/export") == "export"
    assert classify_request("POST", "/ai/artifacts/{aid}/share") == "share"
    assert classify_request("POST", "/v1/shares") == "share"
    assert classify_request("POST", "/v1/ai/gateway/anthropic/v1/messages") == "model"
    assert classify_request("POST", "/governance/policies") == "admin"
    with pytest.raises(IntentError, match="actions"):
        parse_intent({"purpose": "x y z", "actions": ["fly"]})
    with pytest.raises(IntentError, match="purpose"):
        parse_intent({"actions": ["read"]})
    assert parse_intent(WEEKLY).actions == ("read", "query")


def test_key_agent_intent_allows_reads_refuses_writes_exports_and_other_tables(stack):
    s, c = stack
    admin, ana, bob = ({"X-Cairn-Actor": u} for u in (ADMIN, ANA, BOB))
    made = c.post("/admin/agents", json={"name": "Revenue bot", "scopes": ["read", "query_data", "write"],
                                         "owner": ANA}, headers=admin).json()
    h = {"Authorization": f"Bearer {made['key']}"}
    # Without an intent: today's behaviour (a write it is scoped for works).
    assert c.put(f"/collab/bookmarks/{ORDERS}", headers=h).status_code == 200
    bad = c.patch(f"/admin/agents/{made['id']}", json={"intent": {"purpose": "x", "actions": ["read"]}}, headers=admin)
    assert bad.status_code == 422
    got = c.patch(f"/admin/agents/{made['id']}", json={"intent": WEEKLY}, headers=admin)
    assert got.status_code == 200 and got.json()["intent"]["actions"] == ["read", "query"]

    # Allowed: a catalog read and a query of the table it concerns.
    assert c.get("/me", headers=h).json()["urn"] == ANA
    q = c.post("/data/query", json={"sql": "SELECT id, amount FROM main.orders ORDER BY id", "connection": "wh"},
               headers=h)
    assert q.status_code == 200 and q.json()["rows"] == [[1, 9.5], [2, 3.0]], q.text
    # Refused: a write, an export, another table, and SQL whose statement kind is a write or a grant.
    w = c.put(f"/collab/bookmarks/{ORDERS}", headers=h)
    assert w.status_code == 403 and "write is outside this run's declared intent" in w.text
    x = c.get("/context/definitions/export", headers=h)
    assert x.status_code == 403 and "export is outside" in x.text
    t = c.post("/data/query", json={"sql": "SELECT * FROM main.users", "connection": "wh"}, headers=h)
    assert t.status_code == 403 and "main.users is outside" in t.text
    p = c.get("/data/preview", params={"urn": USERS}, headers=h)
    assert p.status_code == 403 and "main.users is outside" in p.text
    d = c.post("/data/query", json={"sql": "DELETE FROM main.orders", "connection": "wh"}, headers=h)
    assert d.status_code == 403 and "write is outside" in d.text
    g = c.post("/data/query", json={"sql": "GRANT SELECT ON main.orders TO bob", "connection": "wh"}, headers=h)
    assert g.status_code == 403 and "admin is outside" in g.text
    with create_engine(f"sqlite:///{s.connections.definitions()['wh']['url'][10:]}").connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == 2      # nothing was deleted

    # Each refusal is audited with the agent, the category, the reason and the intent.
    got = denials(s)
    assert [e["details"]["category"] for e in got] == ["write", "export", "query", "query", "write", "admin"]
    assert {e["subject"] for e in got} == {made["id"]} and {e["actor"] for e in got} == {ANA}
    assert got[2]["details"]["dataset"] == USERS and got[0]["details"]["intent"]["purpose"] == WEEKLY["purpose"]
    assert got[0]["details"]["route"] == "/collab/bookmarks/{urn:path}"

    # Denials: administrators see all, the person (and the agent's owner) theirs, others none.
    assert len(c.get("/v1/agent-intents/denials", headers=admin).json()) == 6
    mine = c.get("/v1/agent-intents/denials", headers=ana).json()
    assert len(mine) == 6 and mine[0]["category"] == "admin" and mine[0]["agent"] == made["id"]
    assert c.get("/v1/agent-intents/denials", headers=bob).json() == []
    assert c.get("/v1/agent-intents/denials", headers=h).status_code == 403     # not for agents

    # A domain in the intent admits that domain's datasets.
    c.patch(f"/admin/agents/{made['id']}", json={"intent": {**WEEKLY, "domains": ["urn:li:domain:crm"]}}, headers=admin)
    assert c.post("/data/query", json={"sql": "SELECT id FROM main.users", "connection": "wh"},
                  headers=h).status_code == 200
    # Clearing it brings back today's behaviour.
    assert c.patch(f"/admin/agents/{made['id']}", json={"intent": {}}, headers=admin).json()["intent"] is None
    assert c.put(f"/collab/bookmarks/{ORDERS}", headers=h).status_code == 200


def test_required_intents_fail_closed_and_run_tokens_carry_theirs(stack):
    s, c = stack
    admin, ana = {"X-Cairn-Actor": ADMIN}, {"X-Cairn-Actor": ANA}
    plain = s.agents.create_key_agent(AgentIn(name="Plain", scopes=["read"], owner=ANA), ADMIN)
    h = {"Authorization": f"Bearer {plain['key']}"}
    assert c.get("/me", headers=h).status_code == 200
    assert c.put("/v1/agent-intents/settings", json={"required": True}, headers=ana).status_code == 403
    assert c.put("/v1/agent-intents/settings", json={"required": "yes"}, headers=admin).status_code == 422
    assert c.put("/v1/agent-intents/settings", json={"required": True}, headers=admin).json()["required"] is True
    r = c.get("/me", headers=h)
    assert r.status_code == 403 and "requires a declared intent" in r.text
    assert any(e["action"] == "agents.intents_required" for e in s.audit.export(category="agents"))

    # A run token declaring an intent works within it (HTTP and MCP), and only there.
    s.custom_agents.create(ADMIN, {"name": "checker", "description": "", "instructions": "Check quality.",
                                   "tools": ["get_quality_status"], "scopes": ["read"], "max_steps": 4})
    agent_id = s.custom_agents.get(ADMIN, "checker")["agent_id"]
    token = s.agents.issue_run_token(agent_id, ANA, intent=parse_intent(
        {"purpose": "check orders quality", "actions": ["read"], "tables": ["main.orders"]}))
    th = {"Authorization": f"Bearer {token}"}
    assert c.get("/me", headers=th).status_code == 200
    assert c.post("/data/query", json={"sql": "SELECT id FROM main.orders", "connection": "wh"},
                  headers=th).status_code == 403                                 # query is not in it
    p = s.identity.resolve({"authorization": f"Bearer {token}"}, {})
    assert p.intent.purpose == "check orders quality" and p.run.startswith("run_")
    server, _ = build_mcp_server(s)
    ctx = _principal.set(p)
    try:
        ok = asyncio.run(server.call_tool("get_quality_status", {"dataset_urn": ORDERS})).structured_content
        no = asyncio.run(server.call_tool("get_quality_status", {"dataset_urn": USERS})).structured_content
    finally:
        _principal.reset(ctx)
    assert ok["dataset_urn"] == ORDERS and no["error"] == "forbidden" and "main.users is outside" in no["message"]
    assert denials(s)[-1]["details"]["tool"] == "get_quality_status"

    # A custom agent: with intents required, one that declares none does not run; one that does runs within it.
    from cairn.ai.custom_agents import AgentBuildError
    from test_custom_agents import Model, text as say, tool

    model = Model()
    s.assistant.llm = model
    s.custom_agents.deploy(ADMIN, "checker")
    with pytest.raises(AgentBuildError) as e:
        s.custom_agents.run(ADMIN, "checker", "how is orders?")
    assert e.value.status == 403 and "intent" in str(e.value)
    with pytest.raises(AgentBuildError, match="intent.actions"):
        s.custom_agents.update(ADMIN, "checker", {"instructions": "Check quality.", "tools": ["get_quality_status"],
                                                  "scopes": ["read"], "intent": {"purpose": "abc", "actions": []}})
    s.custom_agents.update(ADMIN, "checker", {"instructions": "Check quality.", "tools": ["get_quality_status"],
                                              "scopes": ["read"], "max_steps": 4,
                                              "intent": {"purpose": "orders quality", "actions": ["read"],
                                                         "tables": ["main.orders"]}})
    s.custom_agents.deploy(ADMIN, "checker")
    assert s.custom_agents.get(ADMIN, "checker")["intent"]["purpose"] == "orders quality"
    model.say([tool("get_quality_status", dataset_urn=ORDERS)], [tool("get_quality_status", dataset_urn=USERS)],
              [say("Orders is fine; I may not look at users.")])
    r = s.custom_agents.run(ADMIN, "checker", "how are orders and users?")
    assert [x["name"] for x in r["calls"]] == ["get_quality_status", "get_quality_status"]
    assert "main.users is outside" in r["calls"][1]["summary"] and "outside" not in r["calls"][0]["summary"]
    last = denials(s)[-1]
    assert last["actor"] == ADMIN and last["details"]["run"] == r["run"] and last["details"]["dataset"] == USERS

    # Turned off again: credentials without an intent work as before.
    c.put("/v1/agent-intents/settings", json={"required": False}, headers=admin)
    assert c.get("/me", headers=h).status_code == 200
