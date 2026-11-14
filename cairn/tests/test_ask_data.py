"""Ask your data: questions answered with governed SQL (tables, context, one repair, permissions)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cairn.common.core import InMemoryGraph
from cairn.common.security import Principal
from cairn.coreproxy.masking import DatasetPlan
from cairn.dataaccess.api import build_router
from cairn.dataaccess.ask import AskError, AskService
from cairn.dataaccess.service import DataAccessService
from cairn.quality.collectors import ConnectionRegistry

USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
ELSEWHERE = "urn:li:dataset:(urn:li:dataPlatform:snowflake,shop.public.orders,PROD)"
SECRET = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.salaries,PROD)"
ANA, EVE = "urn:li:corpuser:ana", "urn:li:corpuser:eve"


class Audit:
    def __init__(self):
        self.events = []

    def record(self, category, action, actor, subject, details):
        self.events.append((action, actor, subject, details))


class ScriptedLLM:
    """Returns the queued SQL answers in order and keeps every prompt."""

    def __init__(self, *sqls):
        self.sqls, self.prompts = list(sqls), []

    def generate_json(self, system, prompt, schema, model=None):
        self.prompts.append(prompt)
        return {"sql": self.sqls.pop(0), "explanation": "orders per region"}, None


class Layer:
    def definitions(self):
        return [{"name": "revenue", "kind": "metric", "version": 2,
                 "doc": {"expression": "sum(o.amount)", "description": "booked revenue", "datasets": [ORDERS]}},
                {"name": "unrelated", "kind": "metric", "version": 1,
                 "doc": {"expression": "count(*)", "datasets": [ELSEWHERE]}}]

    def answers(self, question, can_view, limit):
        return [{"title": "Revenue by region", "sql": "select region, sum(amount) from main.orders o join ...",
                 "tables": [ORDERS, USERS]}]


@pytest.fixture
def stack(tmp_path):
    url = f"sqlite:///{tmp_path}/wh.db"
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT, region TEXT)"))
        c.execute(text("CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL)"))
        c.execute(text("INSERT INTO users VALUES (1, 'a@x.com', 'EU'), (2, 'b@x.com', 'US')"))
        c.execute(text("INSERT INTO orders VALUES (1, 1, 9.5), (2, 1, 3.0), (3, 2, 7.0)"))
    graph = InMemoryGraph()
    for urn, cols in ((USERS, ["id", "email", "region"]), (ORDERS, ["id", "user_id", "amount"]),
                      (SECRET, ["id", "salary"]), (ELSEWHERE, ["id"])):
        graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in cols]}
    graph.aspects[(USERS, "schemaMetadata")]["fields"].append(
        {"fieldPath": "[version=2.0].[type=struct].address.[type=string].city"})
    reg = ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}})
    audit = Audit()
    data = DataAccessService(reg, lambda a, u: DatasetPlan(fields={"email"}, strategy="redact") if u == USERS
                             else DatasetPlan(), lambda a, u: not (a == EVE and u == ORDERS), audit,
                             known=lambda u: u in (USERS, ORDERS, SECRET))
    return data, graph, audit


def can_view(actor):
    return lambda u: u != SECRET and not (actor == EVE and u == ORDERS)


def test_question_answered_with_definitions_and_past_queries(stack):
    data, graph, audit = stack
    llm = ScriptedLLM("SELECT u.region, sum(o.amount) AS revenue FROM main.orders o "
                      "JOIN main.users u ON u.id = o.user_id GROUP BY u.region ORDER BY u.region")
    svc = AskService(data, graph, llm, Layer(), audit=audit)
    out = svc.ask(ANA, "revenue by region", can_view(ANA), [ORDERS, USERS])
    prompt = llm.prompts[0]
    assert "main.orders (id, user_id, amount)" in prompt and "address.city" in prompt
    assert "metric revenue = sum(o.amount)" in prompt and "unrelated" not in prompt
    assert "Revenue by region" in prompt
    assert out["result"]["rows"] == [["EU", 12.5], ["US", 7.0]] and out["error"] is None
    assert out["definitions"] == [{"name": "revenue", "kind": "metric", "version": 2}]
    assert out["connection"] == "wh" and out["attempts"] == 1 and out["sql"].startswith("SELECT u.region")
    actions = [e[0] for e in audit.events]
    assert actions[-2:] == ["data.queried", "data.asked"] and audit.events[-1][3]["answered"] is True


def test_a_warehouse_error_gets_one_repair(stack):
    data, graph, audit = stack
    llm = ScriptedLLM("SELECT total FROM main.orders", "SELECT sum(amount) AS total FROM main.orders")
    out = AskService(data, graph, llm, audit=audit).ask(ANA, "total order amount", can_view(ANA), [ORDERS])
    assert out["attempts"] == 2 and out["result"]["rows"] == [[19.5]]
    assert "A previous attempt failed" in llm.prompts[1] and "no such column" in llm.prompts[1]


def test_masking_applies_and_permission_refusal_is_final(stack):
    data, graph, audit = stack
    llm = ScriptedLLM("SELECT email FROM main.users ORDER BY id")
    out = AskService(data, graph, llm).ask(ANA, "user emails", can_view(ANA), [USERS])
    assert out["result"]["masked_columns"] == ["email"] and out["result"]["rows"][0] != ["a@x.com"]
    # Eve may see users but not orders: the model joining orders anyway is refused, without a retry.
    llm = ScriptedLLM("SELECT count(*) FROM main.users u JOIN main.orders o ON o.user_id = u.id", "SELECT 1")
    out = AskService(data, graph, llm).ask(EVE, "orders per user", can_view(EVE), [USERS])
    assert out["result"] is None and "may not query main.orders" in out["error"] and out["attempts"] == 1
    with pytest.raises(AskError) as e:
        AskService(data, graph, ScriptedLLM("SELECT 1")).ask(EVE, "revenue", can_view(EVE), [ORDERS])
    assert e.value.status == 403


def test_tables_found_by_search_skip_hidden_and_unserved(stack):
    data, graph, _ = stack
    llm = ScriptedLLM("SELECT count(*) AS n FROM main.users")
    ranked = [SECRET, ELSEWHERE, USERS, ORDERS]
    svc = AskService(data, graph, llm, search=lambda q, cv, k: ranked)
    out = svc.ask(ANA, "how many users", can_view(ANA))
    assert out["tables"] == [USERS, ORDERS] and out["result"]["rows"] == [[2]]
    assert "salaries" not in llm.prompts[0] and "shop.public" not in llm.prompts[0]
    with pytest.raises(AskError, match="no warehouse connection"):
        svc.ask(ANA, "x", can_view(ANA), [ELSEWHERE])
    with pytest.raises(AskError, match="no dataset matched"):
        AskService(data, graph, llm, search=lambda q, cv, k: []).ask(ANA, "x", can_view(ANA))


def test_unsafe_sql_is_refused_and_never_run(stack):
    data, graph, audit = stack
    out = AskService(data, graph, ScriptedLLM("DELETE FROM main.users"), audit=audit).ask(
        ANA, "remove users", can_view(ANA), [USERS])
    assert out["sql"] is None and "refused" in out["error"] and out["result"] is None
    assert [e[0] for e in audit.events] == ["data.asked"]
    plan = AskService(data, graph, ScriptedLLM("SELECT count(*) FROM main.users")).ask(
        ANA, "how many", can_view(ANA), [USERS], run=False)
    assert plan["sql"] and plan["result"] is None and plan["error"] is None


def test_api(stack):
    data, graph, _ = stack
    app = FastAPI()
    principal = lambda: Principal(urn=ANA, via="token")  # noqa: E731
    svc = AskService(data, graph, ScriptedLLM("SELECT count(*) AS n FROM main.orders"))
    app.include_router(build_router(data, principal, svc, lambda p: can_view(p.urn)))
    c = TestClient(app)
    r = c.post("/data/ask", json={"question": "how many orders", "urns": [ORDERS]})
    assert r.status_code == 200 and r.json()["result"]["rows"] == [[3]]
    assert c.post("/data/ask", json={"question": "x", "urns": [ELSEWHERE]}).status_code == 422   # too short
    assert c.post("/data/ask", json={"question": "count them", "urns": [ELSEWHERE]}).status_code == 404
    off = FastAPI()
    off.include_router(build_router(data, principal, AskService(data, graph, None), lambda p: can_view(p.urn)))
    assert TestClient(off).post("/data/ask", json={"question": "how many orders"}).status_code == 503
