"""Workspaces (L163): the editor saves a file as it is typed (autosave), and its version history stays readable:
a run of autosaves by one person makes one version, a save by hand always makes a new one, and another person's
save or a pause longer than CAIRN_BI_AUTOSAVE_MINUTES starts a new one. Through the API, as the editor calls it."""

from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"


def test_autosaves_coalesce_into_one_version(settings, graph, monkeypatch):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    app = create_api_app(s)
    ana, bob = (TestClient(app, headers={"X-Cairn-Actor": a}) for a in (ANA, BOB))
    base = {"name": "Orders", "connection": "lakehouse", "sql": "SELECT 1", "shared": False}
    q = ana.post("/sql/saved", json=base).json()
    versions = lambda: ana.get(f"/sql/versions/query/{q['id']}").json()   # noqa: E731
    assert [v["version"] for v in versions()] == [1]
    # Typing: three autosaves, one new version that holds the last text.
    for n in (2, 3, 4):
        assert ana.put(f"/sql/saved/{q['id']}", json={**base, "sql": f"SELECT {n}", "autosave": True}).status_code == 200
    assert [v["version"] for v in versions()] == [2, 1]
    assert ana.get(f"/sql/versions/query/{q['id']}/2").json()["doc"]["sql"] == "SELECT 4"
    # A save by hand: a version of its own; the next autosave starts a new one.
    ana.put(f"/sql/saved/{q['id']}", json={**base, "sql": "SELECT 5"})
    ana.put(f"/sql/saved/{q['id']}", json={**base, "sql": "SELECT 6", "autosave": True})
    assert [v["version"] for v in versions()] == [4, 3, 2, 1]
    # Another person's autosave (an editor) never folds into Ana's.
    assert ana.put(f"/sql/shares/query/{q['id']}", json={"principal": BOB, "role": "EDITOR"}).status_code == 200
    assert bob.put(f"/sql/saved/{q['id']}", json={**base, "sql": "SELECT 7", "autosave": True}).status_code == 200
    assert [(v["version"], v["saved_by"]) for v in versions()][:2] == [(5, BOB), (4, ANA)]
    # A pause longer than the window: a new version even for the same person.
    s.bi_versions.autosave_ms = 0
    bob.put(f"/sql/saved/{q['id']}", json={**base, "sql": "SELECT 8", "autosave": True})
    assert versions()[0]["version"] == 6
    # Restoring a version still works through the save.
    assert ana.post(f"/sql/versions/query/{q['id']}/2/restore").status_code == 200
    assert ana.get(f"/sql/saved/{q['id']}").json()["sql"] == "SELECT 4"


def test_restoring_an_old_version_keeps_who_may_see_the_asset(settings, graph):
    """An owner who stopped sharing a query with everyone, then restores the SQL of a version from when it was
    shared, must not make it everyone's again: restore brings back the content, not the sharing."""
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    app = create_api_app(s)
    ana, bob = (TestClient(app, headers={"X-Cairn-Actor": a}) for a in (ANA, BOB))
    base = {"name": "Orders", "connection": "lakehouse", "sql": "SELECT 1", "shared": True}
    q = ana.post("/sql/saved", json=base).json()
    assert bob.get(f"/sql/saved/{q['id']}").status_code == 200
    assert ana.put(f"/sql/saved/{q['id']}", json={**base, "sql": "SELECT 2", "shared": False}).status_code == 200
    assert bob.get(f"/sql/saved/{q['id']}").status_code == 404
    assert ana.post(f"/sql/versions/query/{q['id']}/1/restore").status_code == 200
    got = ana.get(f"/sql/saved/{q['id']}").json()
    assert got["sql"] == "SELECT 1" and got["shared"] is False
    assert bob.get(f"/sql/saved/{q['id']}").status_code == 404
    # The other way too: a version from before it was shared does not stop sharing it.
    tiles = [{"query": q["id"]}]
    d = ana.post("/sql/dashboards", json={"name": "Sales", "tiles": tiles}).json()
    assert ana.put(f"/sql/dashboards/{d['id']}", json={"name": "Sales 2", "tiles": tiles, "shared": True}).status_code == 200
    assert bob.get(f"/sql/dashboards/{d['id']}").status_code == 200
    assert ana.post(f"/sql/versions/dashboard/{d['id']}/1/restore").status_code == 200
    assert bob.get(f"/sql/dashboards/{d['id']}").status_code == 200
