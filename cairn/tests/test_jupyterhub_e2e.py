"""JupyterHub on Cairn, end to end, with a real JupyterHub (oauthenticator's GenericOAuthenticator,
configurable-http-proxy) and a real Jupyter server: a person signs in to the hub through Cairn's OAuth2
server; their server starts with a Cairn token of their own (exchanged from their sign-in); saving a
notebook publishes it to the catalog, owned by them, with lineage from the tables its %%sql cells read.

Needs CAIRN_JUPYTERHUB_BIN (the jupyterhub executable, with oauthenticator and jupyter_server beside it)
and CAIRN_CHP_BIN (configurable-http-proxy)."""

import json
import os
import socket
import subprocess
import tempfile
import textwrap
import threading
import time

import httpx
import pytest

HUB = os.environ.get("CAIRN_JUPYTERHUB_BIN", "")
CHP = os.environ.get("CAIRN_CHP_BIN", "")
pytestmark = pytest.mark.skipif(not (HUB and os.path.exists(HUB) and CHP and os.path.exists(CHP)),
                                reason="needs CAIRN_JUPYTERHUB_BIN and CAIRN_CHP_BIN")

SECRET = "jupyterhub-oauth-secret-" + "j" * 24
SERVICE = "hub-test-service-token-" + "s" * 24
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lakehouse-client", "src")


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(url, seconds=60, ok=lambda r: True):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if ok(httpx.get(url, timeout=2)):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def stack():
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.services import build_services

    d = tempfile.mkdtemp(prefix="hub-")
    cp, hp, hub_api, proxy_api = port(), port(), port(), port()
    hub_url = f"http://127.0.0.1:{hp}"
    env = {"CAIRN_TRINO_ACCESS_KEY": "hub-e2e-" + "k" * 24,
           "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "iceberg", "database": ""}}),
           "CAIRN_JUPYTERHUB_OAUTH_SECRET": SECRET,
           "CAIRN_JUPYTERHUB_OAUTH_REDIRECT_URIS": f"{hub_url}/hub/oauth_callback",
           "CAIRN_NOTEBOOK_DEFAULT_CATALOG": "lake"}
    os.environ.update(env)
    try:
        graph = InMemoryGraph()
        s = build_services(Settings(database_url=f"sqlite:///{d}/cairn.db", service_token="svc-secret",
                                    dev_trust_actor_header=True, gateway={"key_dir": f"{d}/keys"}),
                           graph=graph, with_ai=False, with_gateway=False)
    finally:
        for k in env:
            os.environ.pop(k)
    api = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=cp, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=api.run, daemon=True).start()
    wait(f"http://127.0.0.1:{cp}/healthz")
    cairn_url = f"http://127.0.0.1:{cp}"

    bindir = os.path.dirname(HUB)
    config = textwrap.dedent(f'''
        from cairn_lakehouse.jupyter import configure_hub
        configure_hub(c, cairn_url={cairn_url!r}, hub_url={hub_url!r}, client_secret={SECRET!r})
        c.JupyterHub.bind_url = {hub_url!r}
        c.JupyterHub.hub_bind_url = "http://127.0.0.1:{hub_api}"
        c.ConfigurableHTTPProxy.command = [{CHP!r}]
        c.ConfigurableHTTPProxy.api_url = "http://127.0.0.1:{proxy_api}"
        c.JupyterHub.db_url = "sqlite:///{d}/hub.sqlite"
        c.JupyterHub.cookie_secret_file = "{d}/cookie_secret"
        c.JupyterHub.spawner_class = "jupyterhub.spawner.SimpleLocalProcessSpawner"
        c.SimpleLocalProcessSpawner.home_dir_template = "{d}/home/{{username}}"
        c.Spawner.cmd = ["{bindir}/jupyterhub-singleuser"]
        c.Spawner.args = ["--allow-root"]
        c.Spawner.http_timeout = 60
        c.JupyterHub.services = [{{"name": "test", "api_token": {SERVICE!r}}}]
        c.JupyterHub.load_roles = [{{"name": "test", "services": ["test"],
                                     "scopes": ["admin:users", "admin:servers", "access:servers"]}}]
    ''')
    open(f"{d}/jupyterhub_config.py", "w").write(config)
    os.makedirs(f"{d}/home", exist_ok=True)
    hub_env = {**os.environ, "PYTHONPATH": os.path.abspath(SRC), "JUPYTERHUB_CRYPT_KEY": "a" * 64,
               "PATH": f"{bindir}:{os.environ['PATH']}"}
    hub = subprocess.Popen([HUB, "-f", f"{d}/jupyterhub_config.py"], cwd=d, env=hub_env,
                           stdout=open(f"{d}/hub.log", "w"), stderr=subprocess.STDOUT)
    try:
        wait(f"{hub_url}/hub/health", 90, ok=lambda r: r.status_code == 200)   # the proxy answers before the hub
        yield {"cairn": cairn_url, "hub": hub_url, "s": s, "graph": graph, "dir": d}
    finally:
        hub.terminate()
        hub.wait(30)
        api.should_exit = True


def sign_in(st, user):
    """A browser: the hub sends it to Cairn, Cairn (signed in as `user`) back with a code, the hub signs it in."""
    c = httpx.Client(headers={"X-Cairn-Actor": f"urn:li:corpuser:{user}"}, follow_redirects=True, timeout=60)
    r = c.get(f"{st['hub']}/hub/oauth_login")
    assert r.status_code == 200, (r.status_code, r.url, r.text[:300])
    return c


def hub_api(st, method, path, **kw):
    return httpx.request(method, f"{st['hub']}/hub/api{path}", headers={"Authorization": f"token {SERVICE}"}, timeout=120, **kw)


NOTEBOOK = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
    {"cell_type": "markdown", "metadata": {}, "source": "# Orders by region"},
    {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
     "source": "%load_ext cairn_lakehouse.notebook"},
    {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
     "source": ["%%sql\n", "WITH eu AS (SELECT * FROM sales.orders WHERE region = 'EU')\n",
                "SELECT c.name, count(*) FROM eu JOIN lake.sales.customers c ON c.id = eu.customer_id GROUP BY 1"]},
]}


def test_sign_in_through_cairn_a_server_as_the_person_and_notebooks_in_the_catalog(stack):
    st = stack
    browser = sign_in(st, "ana")
    user = hub_api(st, "GET", "/users/ana").json()
    assert user["name"] == "ana"
    # Her server starts with a Cairn token of her own.
    r = hub_api(st, "POST", "/users/ana/server")
    assert r.status_code in (201, 202), r.text
    end = time.time() + 120
    while time.time() < end and not hub_api(st, "GET", "/users/ana").json().get("servers", {}).get("", {}).get("ready"):
        time.sleep(1)
    assert hub_api(st, "GET", "/users/ana").json()["servers"][""]["ready"], open(f"{st['dir']}/hub.log").read()[-3000:]
    # Saving a notebook in her server publishes it to the catalog, as her.
    r = httpx.put(f"{st['hub']}/user/ana/api/contents/orders.ipynb", headers={"Authorization": f"token {SERVICE}"},
                  json={"type": "notebook", "format": "json", "content": NOTEBOOK}, timeout=60)
    assert r.status_code in (200, 201), r.text
    from cairn.identity.notebooks import notebook_urn

    urn = notebook_urn("ana", "orders.ipynb")
    g = st["graph"]
    end = time.time() + 30
    while time.time() < end and g.get_aspect(urn, "notebookInfo") is None:
        time.sleep(0.5)
    info = g.get_aspect(urn, "notebookInfo")
    assert info and info["title"] == "orders", open(f"{st['dir']}/hub.log").read()[-3000:]
    assert [o["owner"] for o in g.get_aspect(urn, "ownership")["owners"]] == ["urn:li:corpuser:ana"]
    job = next(m["entityUrn"] for m in g.emitted if m["aspectName"] == "dataJobInputOutput"
               and json.dumps(g.get_aspect(m["entityUrn"], "dataJobInfo")).find(urn) >= 0)
    assert g.get_aspect(job, "dataJobInputOutput")["inputDatasets"] == [
        "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.customers,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"]          # the CTE is not a table
    actions = {e["action"] for e in st["s"].audit.entries(limit=100)}
    assert {"jupyterhub.sign_in", "notebook.token_issued", "notebook.published"} <= actions
    browser.close()


def test_notebook_tokens_are_the_persons_and_end_with_them(stack):
    st = stack
    s = st["s"]
    hub_tok = s.trino_tokens.mint("urn:li:corpuser:bob", 300, audience="jupyterhub")["token"]
    trino_tok = s.trino_tokens.mint("urn:li:corpuser:bob", 300)["token"]
    # Only JupyterHub sign-in tokens are exchanged; the result works as bob on Cairn's API.
    assert httpx.post(f"{st['cairn']}/v1/notebooks/token", headers={"Authorization": f"Bearer {trino_tok}"}).status_code == 401
    assert httpx.get(f"{st['cairn']}/v1/notebooks/userinfo", headers={"Authorization": f"Bearer {trino_tok}"}).status_code == 401
    assert httpx.get(f"{st['cairn']}/v1/notebooks/userinfo",
                     headers={"Authorization": f"Bearer {hub_tok}"}).json()["sub"] == "bob"
    nb = httpx.post(f"{st['cairn']}/v1/notebooks/token", headers={"Authorization": f"Bearer {hub_tok}"}).json()["token"]
    r = httpx.post(f"{st['cairn']}/v1/trino/token", headers={"Authorization": f"Bearer {nb}"})
    assert r.status_code == 200 and r.json()["user"] == "bob"
    # The JupyterHub sign-in token itself is not a Cairn API credential.
    assert httpx.post(f"{st['cairn']}/v1/trino/token", headers={"Authorization": f"Bearer {hub_tok}"}).status_code == 401
    # Suspended: the token stops working, and no new one is given.
    st["graph"].aspects[("urn:li:corpuser:bob", "corpUserStatus")] = {"status": "SUSPENDED"}
    s.notebooks._status.clear()
    assert httpx.post(f"{st['cairn']}/v1/trino/token", headers={"Authorization": f"Bearer {nb}"}).status_code == 401
    assert httpx.post(f"{st['cairn']}/v1/notebooks/token", headers={"Authorization": f"Bearer {hub_tok}"}).status_code == 403
    # Publishing needs a person.
    r = httpx.post(f"{st['cairn']}/v1/notebooks/publish", headers={"Authorization": "Bearer svc-secret"},
                   json={"path": "x.ipynb", "notebook": NOTEBOOK})
    assert r.status_code == 403


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@x",
                               "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@x"}).stdout


def _start(st, user):
    if hub_api(st, "GET", f"/users/{user}").status_code == 404:
        sign_in(st, user).close()
    r = hub_api(st, "POST", f"/users/{user}/server")
    assert r.status_code in (201, 202, 400), r.text                   # 400: already running
    end = time.time() + 120
    while time.time() < end and not hub_api(st, "GET", f"/users/{user}").json().get("servers", {}).get("", {}).get("ready"):
        time.sleep(1)
    assert hub_api(st, "GET", f"/users/{user}").json()["servers"][""]["ready"], open(f"{st['dir']}/hub.log").read()[-3000:]


def _contents(st, user, path, method="GET", **kw):
    return httpx.request(method, f"{st['hub']}/user/{user}/api/contents/{path}",
                         headers={"Authorization": f"token {SERVICE}"}, timeout=60, **kw)


def _src(cell):
    src = cell["source"]
    return "".join(src) if isinstance(src, list) else src


def test_git_folder_notebooks_round_trip_through_jupyter(stack):
    """A git folder's notebooks appear in the person's Jupyter server; what they save there is pushed back to
    git (outputs cleared) on a branch of its own; a notebook changed in both places is a conflict."""
    st = stack
    s, d = st["s"], st["dir"]
    import pathlib

    work = pathlib.Path(d) / "git-e2e"
    bare, dev = work / "origin.git", work / "dev"
    work.mkdir()
    _git(work, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    _git(work, "clone", "--quiet", str(bare), str(dev))
    (dev / "nb").mkdir()
    (dev / "nb" / "eda.ipynb").write_text(json.dumps(NOTEBOOK))
    (dev / "nb" / "other.ipynb").write_text(json.dumps({**NOTEBOOK, "cells": NOTEBOOK["cells"][:1]}))
    _git(dev, "add", "-A")
    _git(dev, "commit", "--quiet", "-m", "notebooks")
    _git(dev, "push", "--quiet", "origin", "main")
    s.git_folders.allowed = ["file://"]
    s.git_folders.workdir = str(work / "clones")
    carl = {"X-Cairn-Actor": "urn:li:corpuser:carl"}
    r = httpx.post(f"{st['cairn']}/v1/git-folders", headers=carl,
                   json={"name": "Team notebooks", "url": f"file://{bare}", "path": "nb"})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    pulled = httpx.post(f"{st['cairn']}/v1/git-folders/{fid}/pull", headers=carl, json={}, timeout=60).json()
    assert sorted(pulled["notebooks"]) == ["eda.ipynb", "other.ipynb"], pulled
    # The same folder name twice would share a directory in Jupyter: refused.
    assert httpx.post(f"{st['cairn']}/v1/git-folders", headers=carl,
                      json={"name": "team notebooks", "url": f"file://{bare}"}).status_code == 409

    # Carl's server starts: the folder's notebooks are there, written by the extension.
    _start(st, "carl")
    end = time.time() + 60
    while time.time() < end and _contents(st, "carl", "git/Team-notebooks/eda.ipynb").status_code != 200:
        time.sleep(0.5)
    r = _contents(st, "carl", "git/Team-notebooks/eda.ipynb")
    assert r.status_code == 200, open(f"{d}/hub.log").read()[-3000:]
    assert _src(r.json()["content"]["cells"][2]).startswith("%%sql")

    # He edits it in Jupyter (with an output, as after running it) and adds a new notebook; both are saves.
    nb = r.json()["content"]
    nb["cells"][0]["source"] = "# Orders by region, EU first"
    nb["cells"][2]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": "secret-result-row\n"}]
    nb["cells"][2]["execution_count"] = 7
    assert _contents(st, "carl", "git/Team-notebooks/eda.ipynb", "PUT",
                     json={"type": "notebook", "format": "json", "content": nb}).status_code == 200
    assert _contents(st, "carl", "git/Team-notebooks/sub", "PUT", json={"type": "directory"}).status_code in (200, 201)
    assert _contents(st, "carl", "git/Team-notebooks/sub/new.ipynb", "PUT",
                     json={"type": "notebook", "format": "json", "content": NOTEBOOK}).status_code in (200, 201)
    end = time.time() + 30
    while time.time() < end and len(s.git_folders.jupyter_notebooks("urn:li:corpuser:carl", fid)) < 3:
        time.sleep(0.5)
    listed = {n["file"]: n for n in s.git_folders.jupyter_notebooks("urn:li:corpuser:carl", fid)}
    assert listed["eda.ipynb"]["saved_in_jupyter"] and listed["sub/new.ipynb"]["saved_in_jupyter"], listed

    # Push: both go to a new branch, outputs cleared; main is untouched.
    r = httpx.post(f"{st['cairn']}/v1/git-folders/{fid}/push", headers=carl, json={"message": "EU first"}, timeout=60)
    assert r.status_code == 200 and r.json()["changed"] == 2, r.text
    branch = r.json()["branch"]
    pushed = json.loads(_git(bare, "show", f"{branch}:nb/eda.ipynb"))
    assert _src(pushed["cells"][0]) == "# Orders by region, EU first"
    assert pushed["cells"][2]["outputs"] == [] and pushed["cells"][2]["execution_count"] is None
    assert "secret-result-row" not in _git(bare, "show", f"{branch}:nb/eda.ipynb")
    assert _src(json.loads(_git(bare, "show", f"{branch}:nb/sub/new.ipynb"))["cells"][0]) == "# Orders by region"
    assert "EU first" not in _git(bare, "show", "main:nb/eda.ipynb")
    # Pushing again proposes nothing new.
    assert httpx.post(f"{st['cairn']}/v1/git-folders/{fid}/push", headers=carl, json={}, timeout=60).json()["changed"] == 0

    # A colleague changes other.ipynb in git while Carl changes it in Jupyter: the pull reports a conflict
    # and keeps Carl's version; force takes git's.
    _git(dev, "pull", "--quiet", "origin", "main")
    (dev / "nb" / "other.ipynb").write_text(json.dumps({**NOTEBOOK, "cells": [{**NOTEBOOK["cells"][0], "source": "# from git"}]}))
    _git(dev, "commit", "--quiet", "-am", "colleague")
    _git(dev, "push", "--quiet", "origin", "main")
    mine = _contents(st, "carl", "git/Team-notebooks/other.ipynb").json()["content"]
    mine["cells"][0]["source"] = "# from Jupyter"
    _contents(st, "carl", "git/Team-notebooks/other.ipynb", "PUT", json={"type": "notebook", "format": "json", "content": mine})
    time.sleep(1)
    out = httpx.post(f"{st['cairn']}/v1/git-folders/{fid}/pull", headers=carl, json={}, timeout=60).json()
    assert [c["file"] for c in out["conflicts"]] == ["other.ipynb"], out
    # Syncing Jupyter keeps Carl's file (Cairn has his save, which is what the sync serves).
    r = httpx.post(f"{st['hub']}/user/carl/cairn/git/sync", headers={"Authorization": f"token {SERVICE}"}, timeout=60)
    assert r.status_code == 200, r.text
    assert _src(_contents(st, "carl", "git/Team-notebooks/other.ipynb").json()["content"]["cells"][0]) == "# from Jupyter"
    out = httpx.post(f"{st['cairn']}/v1/git-folders/{fid}/pull", headers=carl, json={"force": True}, timeout=60).json()
    assert out["conflicts"] == [] and "other.ipynb" in out["notebooks"], out
    # Taking git's version is Carl's choice to drop his edit: the sync brings his file in line.
    r = httpx.post(f"{st['hub']}/user/carl/cairn/git/sync", headers={"Authorization": f"token {SERVICE}"}, timeout=60).json()
    assert r["written"] == ["git/Team-notebooks/other.ipynb"], r
    assert _src(_contents(st, "carl", "git/Team-notebooks/other.ipynb").json()["content"]["cells"][0]) == "# from git"
    # An edit Cairn never received (written past Jupyter, so no save reached Cairn) is never overwritten.
    home = pathlib.Path(d) / "home" / "carl" / "git" / "Team-notebooks" / "other.ipynb"
    home.write_text(json.dumps({**NOTEBOOK, "cells": [{**NOTEBOOK["cells"][0], "source": "# offline edit"}]}))
    r = httpx.post(f"{st['hub']}/user/carl/cairn/git/sync", headers={"Authorization": f"token {SERVICE}"}, timeout=60).json()
    assert r["kept"] == ["git/Team-notebooks/other.ipynb"], r
    assert "# offline edit" in home.read_text()
    # Another person's Cairn token cannot read Carl's folder.
    assert httpx.get(f"{st['cairn']}/v1/git-folders/{fid}/notebooks", headers={"X-Cairn-Actor": "urn:li:corpuser:bob"}).status_code == 404
