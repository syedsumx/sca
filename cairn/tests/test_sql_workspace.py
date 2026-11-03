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


def test_dashboards_render_each_tile_as_the_viewer(ws):
    from cairn.sqlworkspace.boards import Boards

    b = Boards(ws.engine, ws)
    totals = ws.save(ANA, {"name": "Totals", "connection": "wh", "shared": True,
                           "sql": "SELECT user_id, sum(amount) AS total FROM main.orders GROUP BY user_id ORDER BY user_id",
                           "chart": {"type": "bar", "x": "user_id", "y": ["total"]}})
    people = ws.save(ANA, {"name": "People", "connection": "wh", "shared": True, "sql": "SELECT count(*) AS n FROM main.users"})
    board = b.save_dashboard(ANA, {"name": "Sales", "shared": True, "tiles": [{"query": totals["id"], "width": 2},
                                                                              {"query": people["id"]}]})
    mine = b.render(ANA, board["id"])
    assert [t["title"] for t in mine["tiles"]] == ["Totals", "People"]
    assert mine["tiles"][0]["result"]["rows"] == [[1, 12.5], [2, 7.0]] and mine["tiles"][0]["chart"]["type"] == "bar"
    eves = b.render(EVE, board["id"])                     # shared: eve sees it, each tile as eve
    assert eves["tiles"][0]["status"] == 403 and eves["tiles"][1]["result"]["rows"] == [[2]]
    with pytest.raises(WorkspaceError):                   # a private query of someone else cannot be a tile
        private = ws.save(ANA, {"name": "Private", "connection": "wh", "sql": "SELECT count(*) AS n FROM main.users"})
        b.save_dashboard(EVE, {"name": "x", "tiles": [{"query": private["id"]}]})
    with pytest.raises(WorkspaceError):
        b.delete_dashboard(EVE, board["id"])


def test_alerts_tell_when_their_condition_starts_and_stops(ws):
    from sqlalchemy import text as sqltext

    from cairn.sqlworkspace.boards import Boards

    told = []
    b = Boards(ws.engine, ws, lambda *a: told.append(a))
    q = ws.save(ANA, {"name": "Big orders", "connection": "wh", "sql": "SELECT count(*) AS n FROM main.orders WHERE amount > 8"})
    a = b.save_alert(ANA, {"query": q["id"], "measure": "n", "op": ">=", "value": 2, "every_minutes": 5,
                           "recipients": ["urn:li:corpuser:bob"]})
    assert b.run_due() == 1 and b.alert(ANA, a["id"])["state"] == "ok" and told == []   # first check: fine, quiet
    assert b.run_due() == 0                                                               # not due again for 5 minutes
    wh = ws.data.registry.engine("wh")
    with wh.begin() as c:
        c.execute(sqltext("INSERT INTO orders VALUES (4, 2, 20.0)"))
    later = int(__import__("time").time() * 1000) + 6 * 60_000
    assert b.run_due(later) == 1
    st = b.alert(ANA, a["id"])
    assert st["state"] == "triggered" and st["last_value"] == 2
    assert told[-1][0] == "sql.alert_triggered" and told[-1][3] == [ANA, "urn:li:corpuser:bob"] and "n is 2" in told[-1][2]
    b.check(a["id"])                                     # still triggered: told once
    assert len(told) == 1
    with wh.begin() as c:
        c.execute(sqltext("DELETE FROM orders WHERE id = 4"))
    b.check(a["id"])
    assert told[-1][0] == "sql.alert_resolved" and len(told) == 2
    bad = b.save_alert(ANA, {"query": q["id"], "measure": "nope", "op": ">", "value": 0, "every_minutes": 5})
    assert b.check(bad["id"])["state"] == "error" and "no column" in told[-1][2]
    for wrong in ({"query": q["id"], "op": "~", "value": 1}, {"query": q["id"], "op": ">", "value": "x"},
                  {"query": q["id"], "op": ">", "value": 1, "every_minutes": 1}):
        with pytest.raises(WorkspaceError):
            b.save_alert(ANA, wrong)
    with pytest.raises(WorkspaceError):
        b.alert(EVE, a["id"])


def test_workflows_run_steps_in_order_as_their_owner_and_report(ws):
    from sqlalchemy import text as sqltext

    from cairn.sqlworkspace.workflows import Workflows

    told = []
    wf = Workflows(ws.engine, ws, None, lambda *a: told.append(a))
    orders = ws.save(ANA, {"name": "Orders", "connection": "wh", "sql": "SELECT count(*) AS n FROM main.orders"})
    emails = ws.save(ANA, {"name": "Emails", "connection": "wh", "sql": "SELECT id, email FROM main.users ORDER BY id"})
    w = wf.save(ANA, {"name": "Nightly", "cron": "15 2 * * *", "recipients": ["urn:li:corpuser:bob"], "notify": "always",
                      "steps": [{"name": "report", "type": "query", "query": emails["id"], "after": ["enough"]},
                                {"name": "enough", "type": "check", "query": orders["id"], "measure": "n", "op": ">=", "value": 3}]})
    assert w["next_run_ms"] and w["steps"][0]["after"] == ["enough"]
    run = wf.run(w["id"])
    assert run["status"] == "ok", run
    by = {s["name"]: s for s in run["steps"]}
    assert by["enough"]["detail"] == "n is 3" and by["report"]["rows"] == 2
    # Run as its owner: her masks apply to what the report keeps and sends.
    assert by["report"]["preview"][0][1] != "a@x.com"
    assert told[-1][0] == "sql.workflow_finished" and told[-1][3] == [ANA, "urn:li:corpuser:bob"]
    # A failing check stops what depends on it; the owner is told.
    with ws.data.registry.engine("wh").begin() as c:
        c.execute(sqltext("DELETE FROM orders WHERE id = 3"))
    run = wf.run(w["id"])
    by = {s["name"]: s for s in run["steps"]}
    assert run["status"] == "failed" and "check failed: n is 2" in by["enough"]["error"]
    assert by["report"]["status"] == "skipped" and told[-1][0] == "sql.workflow_failed"
    assert [r["status"] for r in wf.runs(ANA, w["id"])] == ["failed", "ok"]
    # Due on its cron: the scheduler runs it once, then not until the next time.
    later = w["next_run_ms"] + 1000
    assert wf.run_due(later) == 1 and wf.run_due(later) == 0
    assert wf.runs(ANA, w["id"])[0]["trigger"] == "schedule"


def test_workflow_definitions_are_checked_and_private(ws):
    from cairn.sqlworkspace.workflows import Workflows

    wf = Workflows(ws.engine, ws, None)
    q = ws.save(ANA, {"name": "Q", "connection": "wh", "sql": "SELECT 1 AS x"})
    good = {"name": "x", "type": "query", "query": q["id"]}
    for steps, msg in ([[], "1 to 50"], [[good, good], "name of its own"],
                       [[{**good, "after": ["nope"]}], "no step 'nope'"],
                       [[{**good, "name": "a", "after": ["b"]}, {**good, "name": "b", "after": ["a"]}], "cycle"],
                       [[{**good, "type": "check", "op": "~", "value": 1}], "op is"],
                       [[{"name": "d", "type": "derived", "table": "t"}], "derived tables are not set up"]):
        with pytest.raises(WorkspaceError, match=msg):
            wf.save(ANA, {"name": "bad", "steps": steps})
    with pytest.raises(WorkspaceError, match="every five minutes"):
        wf.save(ANA, {"name": "bad", "cron": "* * * * *", "steps": [good]})
    w = wf.save(ANA, {"name": "mine", "steps": [good]})
    with pytest.raises(WorkspaceError):
        wf.get(EVE, w["id"])                                 # someone else's: not found
    assert wf.list(EVE) == [] and [x["name"] for x in wf.list(ANA)] == ["mine"]
    with pytest.raises(WorkspaceError):                      # ana's private query is not eve's to schedule
        wf.save(EVE, {"name": "q of ana", "steps": [{"name": "x", "type": "query", "query": q["id"]}]})
