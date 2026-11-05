"""BI folders, comments and usage over HTTP, against a Cairn server and a real warehouse (SQLite): a folder shows
each viewer only what they may open; comments with mentions tell those who may open the asset; views are counted
as people look, and only owners and editors see them."""

import socket
import tempfile
import threading
import time

import httpx
import pytest
from sqlalchemy import create_engine, text

from cairn.governance.abac import Policy, PolicyBundle

ANA, BOB, CAROL = (f"urn:li:corpuser:{n}" for n in ("ana", "bob", "carol"))


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def api():
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.services import build_services

    d = tempfile.mkdtemp(prefix="collab-")
    with create_engine(f"sqlite:///{d}/wh.db").begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 10), (2, 'US', 20)"))
    graph = InMemoryGraph()
    graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    s = build_services(Settings(database_url=f"sqlite:///{d}/cairn.db", service_token="svc-secret",
                                dev_trust_actor_header=True, gateway={"key_dir": f"{d}/keys"}),
                       graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"]),
                                            Policy(id="no-ops", effect="deny", actions=["MANAGE_SYSTEM_OPERATIONS"])]))
    s.data_access.registry = ConnectionRegistry({"wh": {"url": f"sqlite:///{d}/wh.db", "catalog": {"platform": "sqlite"}}})
    p = _port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{p}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    yield s, httpx.Client(base_url=url, timeout=30)
    server.should_exit = True


def h(who):
    return {"X-Cairn-Actor": who}


def assets(s):
    q = s.sql_workspace.save(ANA, {"name": "Orders", "connection": "wh", "sql": "SELECT id, region FROM main.orders ORDER BY id"})
    private = s.sql_workspace.save(ANA, {"name": "Private", "connection": "wh", "sql": "SELECT 1 AS one"})
    board = s.sql_boards.save_dashboard(ANA, {"name": "Sales", "tiles": [{"query": q["id"]}]})
    return q, private, board


def test_folders_show_each_viewer_only_what_they_may_open(api):
    s, c = api
    q, private, board = assets(s)
    top = c.post("/sql/folders", headers=h(ANA), json={"name": "Sales team", "shared": True}).json()
    sub = c.post("/sql/folders", headers=h(ANA), json={"name": "Weekly", "parent": top["id"]}).json()
    assert c.post("/sql/folders", headers=h(BOB), json={"name": "x", "parent": top["id"]}).status_code == 403
    for kind, aid in (("dashboard", board["id"]), ("query", private["id"])):
        assert c.put(f"/sql/folders/{top['id']}/items/{kind}/{aid}", headers=h(ANA)).status_code == 200
    # Bob cannot file what he may not open, nor into Ana's folder.
    assert c.put(f"/sql/folders/{top['id']}/items/query/{q['id']}", headers=h(BOB)).status_code == 403
    mine = c.get(f"/sql/folders/{top['id']}", headers=h(ANA)).json()
    assert {(i["kind"], i["id"]) for i in mine["items"]} == {("dashboard", board["id"]), ("query", private["id"])}
    assert [f["name"] for f in mine["folders"]] == ["Weekly"]                  # hers, though not shared
    # The dashboard is shared with Bob; the private query is not: the folder shows him the dashboard only.
    c.put(f"/sql/shares/dashboard/{board['id']}", headers=h(ANA), json={"principal": BOB, "role": "VIEWER"})
    his = c.get(f"/sql/folders/{top['id']}", headers=h(BOB)).json()
    assert his["items"] == [{"kind": "dashboard", "id": board["id"]}] and his["folders"] == []   # Weekly is not shared
    assert c.get(f"/sql/folders/{sub['id']}", headers=h(BOB)).status_code == 404
    assert "Sales team" in [f["name"] for f in c.get("/sql/folders", headers=h(BOB)).json()]
    assert c.patch(f"/sql/folders/{top['id']}", headers=h(BOB), json={"name": "mine"}).status_code == 403
    assert c.delete(f"/sql/folders/{top['id']}", headers=h(ANA)).status_code == 409            # has a folder in it
    assert c.delete(f"/sql/folders/{sub['id']}", headers=h(ANA)).status_code == 204
    assert c.delete(f"/sql/folders/{top['id']}", headers=h(ANA)).status_code == 204


def test_comments_threads_mentions_and_who_may_delete(api):
    s, c = api
    q, private, board = assets(s)
    c.put(f"/sql/shares/dashboard/{board['id']}", headers=h(ANA), json={"principal": BOB, "role": "VIEWER"})
    path = f"/sql/comments/dashboard/{board['id']}"
    assert c.post(path, headers=h(CAROL), json={"body": "hi"}).status_code == 404               # not shared with her
    first = c.post(path, headers=h(BOB), json={"body": "Why is US so high? @ana @carol", "tile": 0})
    assert first.status_code == 201, first.text
    told_ana = [n for n in s.notify.inbox(ANA) if n["type"] == "bi.mentioned"]
    assert told_ana and "Why is US so high" in told_ana[0]["body"]
    assert not [n for n in s.notify.inbox(CAROL) if n["type"] == "bi.mentioned"]                # may not open it
    reply = c.post(path, headers=h(ANA), json={"body": "A large order on the 2nd", "parent": first.json()["id"]}).json()
    assert reply["parent_id"] == first.json()["id"]
    assert c.post(path, headers=h(ANA), json={"body": "x", "parent": "nope"}).status_code == 404
    assert c.patch(f"/sql/comments/{reply['id']}", headers=h(BOB), json={"body": "edited"}).status_code == 403
    assert c.patch(f"/sql/comments/{reply['id']}", headers=h(ANA), json={"body": "A large order on 2 Nov"}).json()["edited_ms"]
    # The owner may delete anyone's comment; the thread keeps its place, without the text.
    assert c.delete(f"/sql/comments/{first.json()['id']}", headers=h(ANA)).status_code == 204
    listed = c.get(path, headers=h(BOB)).json()
    assert [(x["deleted"], x["body"]) for x in listed] == [(True, None), (False, "A large order on 2 Nov")]
    assert c.delete(f"/sql/comments/{reply['id']}", headers=h(BOB)).status_code == 403


def test_views_are_counted_as_people_look_and_shown_to_owners_and_editors(api):
    s, c = api
    q, private, board = assets(s)
    c.put(f"/sql/shares/dashboard/{board['id']}", headers=h(ANA), json={"principal": BOB, "role": "VIEWER"})
    c.put(f"/sql/shares/dashboard/{board['id']}", headers=h(ANA), json={"principal": CAROL, "role": "EDITOR"})
    for who in (ANA, BOB, BOB):
        r = c.post(f"/sql/dashboards/{board['id']}/render", headers=h(who), json={})
        assert r.status_code == 200 and r.json()["tiles"][0]["result"]["rows"] == [[1, "EU"], [2, "US"]], r.text
    assert c.post(f"/sql/saved/{q['id']}/run", headers=h(ANA)).status_code == 200
    u = c.get(f"/sql/usage/dashboard/{board['id']}", headers=h(CAROL)).json()               # an editor
    assert u["views"] == 3 and u["viewers"] == 2 and u["days"][0]["views"] == 3, u
    assert c.get(f"/sql/usage/dashboard/{board['id']}", headers=h(BOB)).status_code == 403   # a viewer
    pop = c.get("/sql/usage/popular", headers=h(BOB)).json()
    assert pop[0] == {"kind": "dashboard", "id": board["id"], "views": 3, "viewers": 2}
    assert ("query", q["id"]) not in {(x["kind"], x["id"]) for x in pop}                     # not his to open
    stale = {(x["kind"], x["id"]) for x in c.get("/sql/usage/stale", headers=h(ANA)).json()}
    assert ("query", private["id"]) in stale and ("dashboard", board["id"]) not in stale and ("query", q["id"]) not in stale
