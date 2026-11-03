"""Git folders: saved queries and notebooks kept in a real git repository (a bare repository and the git
command, as with any host). Pull makes queries and publishes notebooks; push commits to a new branch;
conflicts are reported, never overwritten; folders are their owner's."""

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
NOTEBOOK = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
    {"cell_type": "markdown", "metadata": {}, "source": "# Revenue"},
    {"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None,
     "source": "%%sql\nSELECT region, sum(amount) FROM lakehouse.marts.orders GROUP BY region"}]}


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com", *args], cwd=cwd,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def world(settings, graph, tmp_path, monkeypatch):
    bare, dev = tmp_path / "origin.git", tmp_path / "dev"
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    git(tmp_path, "clone", "--quiet", str(bare), str(dev))
    (dev / "reports").mkdir()
    (dev / "reports" / "revenue.sql").write_text(
        "-- connection: lakehouse\n-- description: Revenue by region\n-- shared: true\n"
        '-- chart: {"type": "bar", "x": "region", "y": ["total"]}\n'
        "SELECT region, sum(amount) AS total FROM marts.orders GROUP BY region\n")
    (dev / "reports" / "big.sql").write_text("-- connection: lakehouse\nSELECT * FROM marts.orders WHERE amount > 100\n")
    (dev / "reports" / "nb").mkdir()
    (dev / "reports" / "nb" / "revenue.ipynb").write_text(json.dumps(NOTEBOOK))
    (dev / "README.md").write_text("not a query\n")
    git(dev, "add", "-A")
    git(dev, "commit", "--quiet", "-m", "reports")
    git(dev, "push", "--quiet", "origin", "main")
    monkeypatch.setenv("CAIRN_GIT_FOLDER_URLS", "file://")
    monkeypatch.setenv("CAIRN_GIT_FOLDERS_DIR", str(tmp_path / "work"))
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    from cairn.identity.notebooks import NotebookCatalog

    s.git_folders.notebooks = NotebookCatalog(graph, lambda c, sc, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{sc}.{t},PROD)")
    app = create_api_app(s)
    return {"s": s, "bare": bare, "dev": dev, "graph": graph,
            "ana": TestClient(app, headers={"X-Cairn-Actor": ANA}), "bob": TestClient(app, headers={"X-Cairn-Actor": BOB})}


def test_pull_push_and_conflicts(world):
    s, ana, bob, dev = world["s"], world["ana"], world["bob"], world["dev"]
    url = f"file://{world['bare']}"
    assert ana.post("/v1/git-folders", json={"name": "r", "url": "https://evil.example.com/x.git"}).status_code == 403
    assert ana.post("/v1/git-folders", json={"name": "r", "url": url, "path": "../etc"}).status_code == 400
    r = ana.post("/v1/git-folders", json={"name": "reports", "url": url, "path": "reports"})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    # Pull: two queries made from their files' headers, the notebook published with its lineage.
    out = ana.post(f"/v1/git-folders/{fid}/pull").json()
    assert sorted(out["created"]) == ["big.sql", "revenue.sql"] and out["notebooks"] == ["nb/revenue.ipynb"], out
    mine = {q["name"]: q for q in s.sql_workspace.saved(ANA)}
    rev = mine["revenue"]
    assert (rev["connection"], rev["description"], rev["shared"], rev["chart"]["type"]) == ("lakehouse", "Revenue by region", True, "bar")
    nb = [u for (u, a) in world["graph"].aspects if a == "notebookInfo"]
    assert nb, "the notebook is a catalog asset"
    # Pulling again changes nothing.
    again = ana.post(f"/v1/git-folders/{fid}/pull").json()
    assert not (again["created"] or again["updated"] or again["deleted"] or again["conflicts"])
    # A colleague changes one file and deletes another in git: pulled into Cairn.
    git(dev, "pull", "--quiet")
    rev_file = dev / "reports" / "revenue.sql"
    rev_file.write_text(rev_file.read_text().replace("GROUP BY region", "WHERE amount > 0 GROUP BY region"))
    git(dev, "rm", "--quiet", "reports/big.sql")
    git(dev, "commit", "--quiet", "-am", "tighten")
    git(dev, "push", "--quiet", "origin", "main")
    out = ana.post(f"/v1/git-folders/{fid}/pull").json()
    assert out["updated"] == ["revenue.sql"] and out["deleted"] == ["big.sql"], out
    mine = {q["name"]: q for q in s.sql_workspace.saved(ANA) if q["owner"] == ANA}
    assert "amount > 0" in mine["revenue"]["sql"] and "big" not in mine and mine["revenue"]["id"] == rev["id"]
    # Ana changes the query in Cairn and adds one; push: a new branch with both, committed by her.
    s.sql_workspace.save(ANA, {**mine["revenue"], "sql": "SELECT region, sum(amount) AS total FROM marts.orders GROUP BY 1"}, rev["id"])
    new = s.sql_workspace.save(ANA, {"name": "Top customers", "connection": "lakehouse", "sql": "SELECT * FROM marts.customers LIMIT 10"})
    pushed = ana.post(f"/v1/git-folders/{fid}/push", json={"message": "From Cairn", "add": [new["id"]]}).json()
    assert pushed["changed"] == 2 and pushed["branch"].startswith("cairn/ana/") and pushed["pull_request"] is None, pushed
    git(dev, "fetch", "--quiet", "origin")
    shown = git(dev, "show", f"origin/{pushed['branch']}:reports/Top-customers.sql")
    assert shown.startswith("-- connection: lakehouse") and "marts.customers" in shown
    assert "GROUP BY 1" in git(dev, "show", f"origin/{pushed['branch']}:reports/revenue.sql")
    assert git(dev, "log", "-1", "--format=%an %s", f"origin/{pushed['branch']}").strip() == "ana From Cairn"
    assert "GROUP BY 1" not in git(dev, "show", "origin/main:reports/revenue.sql")     # never the folder's branch
    # Proposed already: pushing again makes no new branch.
    assert ana.post(f"/v1/git-folders/{fid}/push").json()["changed"] == 0
    # Before the pull request is merged, a pull neither conflicts nor deletes the query only pushed so far.
    out = ana.post(f"/v1/git-folders/{fid}/pull").json()
    assert not (out["conflicts"] or out["deleted"] or out["updated"]), out
    # Merged: the folder is in step again, with nothing to report.
    git(dev, "checkout", "--quiet", "main")
    git(dev, "pull", "--quiet")
    git(dev, "merge", "--quiet", "--no-edit", f"origin/{pushed['branch']}")
    git(dev, "push", "--quiet", "origin", "main")
    out = ana.post(f"/v1/git-folders/{fid}/pull").json()
    assert not (out["conflicts"] or out["deleted"] or out["created"] or out["updated"]), out
    assert ana.post(f"/v1/git-folders/{fid}/push").json()["changed"] == 0
    # Changed in git and in Cairn since the last pull: a conflict, left alone, unless forced.
    git(dev, "pull", "--quiet")
    (dev / "reports" / "revenue.sql").write_text("-- connection: lakehouse\nSELECT 1 AS colleague\n")
    git(dev, "commit", "--quiet", "-am", "theirs")
    git(dev, "push", "--quiet", "origin", "main")
    s.sql_workspace.save(ANA, {**s.sql_workspace.get(ANA, rev["id"]), "sql": "SELECT 2 AS mine"}, rev["id"])
    out = ana.post(f"/v1/git-folders/{fid}/pull").json()
    assert [c["file"] for c in out["conflicts"]] == ["revenue.sql"], out
    assert s.sql_workspace.get(ANA, rev["id"])["sql"] == "SELECT 2 AS mine"
    out = ana.post(f"/v1/git-folders/{fid}/pull", json={"force": True}).json()
    assert out["updated"] == ["revenue.sql"] and s.sql_workspace.get(ANA, rev["id"])["sql"] == "SELECT 1 AS colleague"
    # A folder is its owner's: Bob neither sees nor pulls it.
    assert bob.get("/v1/git-folders").json() == []
    assert bob.post(f"/v1/git-folders/{fid}/pull").status_code == 404
    ev = [e["action"] for e in s.audit.entries("sql", limit=50)]
    assert "git_folder.pulled" in ev and "git_folder.pushed" in ev
    # Deleting the folder keeps the queries.
    assert ana.delete(f"/v1/git-folders/{fid}").status_code == 204
    assert any(q["id"] == rev["id"] for q in s.sql_workspace.saved(ANA))
    assert not os.path.exists(os.path.join(os.environ["CAIRN_GIT_FOLDERS_DIR"], fid))


def test_a_folder_that_cannot_be_reached_records_why(world):
    ana = world["ana"]
    fid = ana.post("/v1/git-folders", json={"name": "gone", "url": f"file://{world['bare']}-missing"}).json()["id"]
    r = ana.post(f"/v1/git-folders/{fid}/pull")
    assert r.status_code == 502 and "git clone" in r.json()["detail"]
    assert "git clone" in ana.get("/v1/git-folders").json()[0]["error"]


def test_query_files_round_trip():
    from cairn.sqlworkspace.gitfolders import parse_sql, render_sql

    q = {"name": "x", "connection": "lakehouse", "description": "two\nlines", "shared": True,
         "chart": {"type": "line", "x": "day", "y": ["n"]}, "sql": "SELECT 1"}
    back = parse_sql(render_sql(q), "x")
    assert back == {**q, "description": "two lines"}
    assert parse_sql("SELECT 1", "plain")["connection"] == ""


def test_notebooks_saved_in_jupyter_are_pushed_without_outputs(world):
    """What the Jupyter save hook sends under git/<folder>/ is kept with the folder, served back to Jupyter,
    and pushed with outputs cleared; a notebook changed in git and in Jupyter is a conflict."""
    s, ana, dev = world["s"], world["ana"], world["dev"]
    fid = ana.post("/v1/git-folders", json={"name": "reports", "url": f"file://{world['bare']}", "path": "reports"}).json()["id"]
    ana.post(f"/v1/git-folders/{fid}/pull")
    listed = ana.get(f"/v1/git-folders/{fid}/notebooks")
    assert listed.status_code == 200, listed.text
    [nb] = listed.json()
    assert (nb["file"], nb["path"], nb["saved_in_jupyter"]) == ("nb/revenue.ipynb", "git/reports/nb/revenue.ipynb", False)
    assert world["bob"].get(f"/v1/git-folders/{fid}/notebooks").status_code == 404
    # A save in Jupyter (with outputs) goes to the catalog and to the folder; one outside git/ only to the catalog.
    edited = {**NOTEBOOK, "cells": [{**NOTEBOOK["cells"][0], "source": "# Revenue, edited"},
                                    {"cell_type": "code", "metadata": {}, "execution_count": 3, "source": "1 + 1",
                                     "outputs": [{"output_type": "execute_result", "execution_count": 3, "metadata": {},
                                                  "data": {"text/plain": "2"}}]}]}
    assert s.git_folders.notebook_saved(ANA, "git/reports/nb/revenue.ipynb", edited) == {"folder": fid, "file": "nb/revenue.ipynb"}
    assert s.git_folders.notebook_saved(ANA, "scratch/x.ipynb", edited) is None
    assert s.git_folders.notebook_saved(BOB, "git/reports/nb/revenue.ipynb", edited) is None       # not bob's folder
    from cairn.sqlworkspace.workspace import WorkspaceError

    with pytest.raises(WorkspaceError, match="no \\.\\."):
        s.git_folders.notebook_saved(ANA, "git/reports/../../etc/x.ipynb", edited)
    [nb] = ana.get(f"/v1/git-folders/{fid}/notebooks").json()
    assert nb["saved_in_jupyter"] and nb["notebook"]["cells"][0]["source"] == "# Revenue, edited"
    out = ana.post(f"/v1/git-folders/{fid}/push", json={"message": "edit"}).json()
    assert out["changed"] == 1, out
    pushed = json.loads(git(world["bare"], "show", f"{out['branch']}:reports/nb/revenue.ipynb"))
    assert pushed["cells"][1]["outputs"] == [] and pushed["cells"][1]["execution_count"] is None
    assert ana.post(f"/v1/git-folders/{fid}/push", json={}).json()["changed"] == 0
    # Git changes it too: a conflict, unless forced; forced, git's version replaces the save.
    git(dev, "pull", "--quiet", "origin", "main")
    (dev / "reports" / "nb" / "revenue.ipynb").write_text(json.dumps({**NOTEBOOK, "cells": NOTEBOOK["cells"][:1]}))
    git(dev, "commit", "--quiet", "-am", "theirs")
    git(dev, "push", "--quiet", "origin", "main")
    out = ana.post(f"/v1/git-folders/{fid}/pull").json()
    assert [c["file"] for c in out["conflicts"]] == ["nb/revenue.ipynb"], out
    out = ana.post(f"/v1/git-folders/{fid}/pull", json={"force": True}).json()
    assert out["conflicts"] == [] and out["notebooks"] == ["nb/revenue.ipynb"]
    [nb] = ana.get(f"/v1/git-folders/{fid}/notebooks").json()
    assert not nb["saved_in_jupyter"] and len(nb["notebook"]["cells"]) == 1
    # Two folders whose names make the same directory are refused.
    assert ana.post("/v1/git-folders", json={"name": "Reports", "url": f"file://{world['bare']}"}).status_code == 409
