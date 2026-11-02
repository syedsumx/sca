"""The SQL workspace (cairn.sqlworkspace): runs through the data access service as the caller (tables
authorized and masked per person), a history per person, saved queries that are private or shared and
run as whoever opens them, and a schema browser that lists only what the caller may see."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cairn.common.core import InMemoryGraph
from cairn.common.db import init_db
from cairn.common.security import Principal
from cairn.coreproxy.masking import DatasetPlan
from cairn.dataaccess.service import DataAccessService
from cairn.quality.collectors import ConnectionRegistry
from cairn.sqlworkspace import SqlWorkspace, WorkspaceError, build_router

USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
ANA, EVE = "urn:li:corpuser:ana", "urn:li:corpuser:eve"


@pytest.fixture
def ws(tmp_path):
    url = f"sqlite:///{tmp_path}/wh.db"
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT, region TEXT)"))
        c.execute(text("CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL)"))
        c.execute(text("INSERT INTO users VALUES (1, 'a@x.com', 'EU'), (2, 'b@y.com', 'US')"))
        c.execute(text("INSERT INTO orders VALUES (1, 1, 9.5), (2, 1, 3.0), (3, 2, 7.0)"))
    reg = ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}})
    plans = {USERS: DatasetPlan(fields={"email"}, strategy="partial")}
    denied = {(EVE, ORDERS)}
    data = DataAccessService(reg, lambda actor, urn: plans.get(urn, DatasetPlan()),
                             lambda actor, urn: (actor, urn) not in denied, None, known=lambda urn: urn in (USERS, ORDERS))
    meta = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(meta)
    graph = InMemoryGraph()
    for urn, cols in ((USERS, ["id", "email", "region"]), (ORDERS, ["id", "user_id", "amount"])):
        graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c, "nativeDataType": "TEXT"} for c in cols]}
    return SqlWorkspace(meta, data, graph)


def test_runs_as_the_caller_and_keeps_a_history(ws):
    out = ws.run(ANA, "SELECT id, email FROM main.users ORDER BY id", "wh")
    assert out["masked_columns"] == ["email"] and out["rows"][0][0] == 1 and out["rows"][0][1] != "a@x.com"
    with pytest.raises(WorkspaceError) as e:
        ws.run(EVE, "SELECT * FROM main.orders", "wh")
    assert e.value.status == 403
    with pytest.raises(WorkspaceError):
        ws.run(ANA, "DELETE FROM main.users", "wh")
    h = ws.history(ANA)
    assert [x["status"] for x in h] == ["error", "ok"] and h[1]["rows"] == 2 and "SELECT" in h[0]["error"]
    assert ws.history(EVE)[0]["status"] == "error" and len(ws.history(EVE)) == 1   # each their own


def test_saved_queries_are_private_or_shared_and_run_as_whoever_opens_them(ws):
    q = ws.save(ANA, {"name": "Orders by user", "connection": "wh", "sql": "SELECT user_id, sum(amount) AS total "
                      "FROM main.orders GROUP BY user_id ORDER BY user_id", "chart": {"type": "bar", "x": "user_id",
                                                                                         "y": ["total"]}})
    assert q["chart"] == {"type": "bar", "x": "user_id", "y": ["total"]} and not q["shared"]
    assert ws.saved(EVE) == []
    with pytest.raises(WorkspaceError):
        ws.get(EVE, q["id"])
    ws.save(ANA, {**q, "shared": True}, q["id"])
    assert [s["id"] for s in ws.saved(EVE)] == [q["id"]]
    assert ws.run_saved(ANA, q["id"])["rows"] == [[1, 12.5], [2, 7.0]]
    with pytest.raises(WorkspaceError) as e:     # shared, but eve may not read orders: it runs as eve
        ws.run_saved(EVE, q["id"])
    assert e.value.status == 403
    with pytest.raises(WorkspaceError):          # only its owner changes or deletes it
        ws.save(EVE, {**q, "name": "mine now"}, q["id"])
    with pytest.raises(WorkspaceError):
        ws.delete(EVE, q["id"])
    for bad in ({"name": "", "connection": "wh", "sql": "SELECT 1"}, {"name": "x", "connection": "wh", "sql": "DROP TABLE t"},
                {"name": "x", "connection": "wh", "sql": "SELECT 1 FROM main.users", "chart": {"type": "pie"}}):
        with pytest.raises(WorkspaceError):
            ws.save(ANA, bad)
    ws.delete(ANA, q["id"])
    assert ws.saved(ANA) == []


def test_the_schema_browser_shows_what_the_caller_may_see(ws):
    can = lambda actor: (lambda urn: not (actor == EVE and urn == ORDERS))  # noqa: E731
    assert [t["name"] for t in ws.schema("wh", can(ANA))] == ["main.orders", "main.users"]
    eve = ws.schema("wh", can(EVE))
    assert [t["name"] for t in eve] == ["main.users"] and [c["name"] for c in eve[0]["columns"]] == ["id", "email", "region"]
    with pytest.raises(WorkspaceError):
        ws.schema("nope", can(ANA))


def test_the_routes(ws):
    app = FastAPI()

    def who(request: Request):
        return Principal(request.headers["x-who"], request.headers.get("x-via", "token"))
    app.include_router(build_router(ws, who, lambda p: (lambda urn: True)))
    c = TestClient(app)
    h = {"x-who": ANA}
    r = c.post("/sql/run", json={"sql": "SELECT count(*) AS n FROM main.orders", "connection": "wh"}, headers=h)
    assert r.status_code == 200 and r.json()["rows"] == [[3]]
    assert c.post("/sql/run", json={"sql": "SELECT * FROM main.orders", "connection": "wh"},
                  headers={"x-who": EVE}).status_code == 403
    assert c.get("/sql/history", headers=h).json()[0]["rows"] == 1
    saved = c.post("/sql/saved", json={"name": "n", "connection": "wh", "sql": "SELECT count(*) AS n FROM main.orders"},
                   headers=h)
    assert saved.status_code == 201
    assert c.post(f"/sql/saved/{saved.json()['id']}/run", headers=h).json()["rows"] == [[3]]
    assert [t["name"] for t in c.get("/sql/schema", params={"connection": "wh"}, headers=h).json()] == ["main.orders",
                                                                                                      "main.users"]
    assert c.post("/sql/run", json={"sql": "SELECT 1 FROM main.orders", "connection": "wh"},
                  headers={"x-who": "urn:li:corpuser:svc", "x-via": "agent"}).status_code == 403   # for people
    assert c.delete(f"/sql/saved/{saved.json()['id']}", headers=h).status_code == 204
