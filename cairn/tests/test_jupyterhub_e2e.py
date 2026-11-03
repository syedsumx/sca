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
    api = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=cp, log_level="warning"))
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
