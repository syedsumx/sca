"""Cairn Desktop (L131): the real Electron app, under a virtual display, against real Cairns.

Two Cairns (Cairn's real API and the built web UI, as the ingress serves them: ``/cairn/api`` and ``/cairn-ui`` on
one origin) on two origins, with a stand-in for Cairn Core's sign-in (a ``/login`` page setting ``PLAY_SESSION``,
and Core's GraphQL ``me`` that Cairn asks who a session is), so the app signs in with a browser session as people do.
The app is driven by ``desktop/test/e2e/desktop.e2e.js`` (Playwright's Electron support) inside a D-Bus session with
an unlocked GNOME Keyring: Electron's safeStorage and the command line's keyring are the real OS keychain. Checked
there: adding a workspace (shared with the CLI's ``config.toml``), signing in, two workspaces with separate sessions,
a deep link opening a page and an unknown one asking to add, a page's notification and an inbox notification reaching
the main process, a blocked navigation opening in the browser, "Sign in the command line too" ending with ``cairn
whoami`` as the person, and going offline with a dashboard kept (encrypted) and shown read-only, cleared on sign-out.

Needs: Node with ``npm install`` done in desktop/ (Electron, Playwright core), the built UI bundle (``npm run
build:mfe`` in ui), xvfb-run, dbus-run-session and gnome-keyring-daemon. The CLI is the PyInstaller binary in
CAIRN_CLI_BIN, else ``python -m cairn_cli.main`` behind a small script."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
from fastapi import Request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, "desktop")
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
SCRIPT = os.path.join(DESKTOP, "test", "e2e", "desktop.e2e.js")
ANA = "urn:li:corpuser:ana"
BOB = "urn:li:corpuser:bob"
DS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"


def _missing() -> str:
    if not os.path.exists(os.path.join(BUNDLE, "app.html")):
        return "the built UI bundle (npm run build:mfe in ui)"
    for module in ("electron", "playwright-core", "electron-updater"):
        if not os.path.isdir(os.path.join(DESKTOP, "node_modules", module)):
            return f"{module} (npm install in desktop)"
    for tool in ("node", "xvfb-run", "dbus-run-session", "gnome-keyring-daemon"):
        if not shutil.which(tool):
            return tool
    return ""


pytestmark = pytest.mark.skipif(bool(_missing()), reason=f"needs {_missing()}")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    end = time.time() + 30
    while not server.started:
        assert time.time() < end, "uvicorn did not start"
        time.sleep(0.05)
    return server, thread


@pytest.fixture
def stack(settings, graph, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from sqlalchemy import create_engine, text

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.notify.hub import Event
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.services import build_services

    # Cairn Core's part: who a session cookie is (Cairn asks its GraphQL `me`), on its own address.
    sessions: dict[str, str] = {}
    core = FastAPI()

    @core.post("/api/v2/graphql")
    async def me(request: Request):
        user = sessions.get(request.cookies.get("PLAY_SESSION", ""))
        if not user:
            return JSONResponse({"errors": [{"message": "unauthorized"}]}, status_code=401)
        return {"data": {"me": {"corpUser": {"urn": user, "username": user.rsplit(":", 1)[1]}}}}

    core_port = _port()
    core_server, _ = _serve(core, core_port)

    wh = tmp_path / "wh.db"
    with create_engine(f"sqlite:///{wh}").begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 9.5), (2, 'US', 3.0)"))
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    graph.aspects[(DS, "datasetProperties")] = {"name": "orders"}
    settings.core.frontend_url = f"http://127.0.0.1:{core_port}"
    settings.dev_trust_actor_header = False                  # people sign in with a session, as in production
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"]),
                                            Policy(id="no-ops", effect="deny", actions=["MANAGE_SYSTEM_OPERATIONS"])]))
    s.data_access.registry = ConnectionRegistry({"wh": {"url": f"sqlite:///{wh}", "catalog": {"platform": "sqlite"}}})
    s.device_logins.interval_s = 1
    query = s.sql_workspace.save(ANA, {"name": "Orders by region", "connection": "wh",
                                       "sql": "SELECT id, region FROM main.orders ORDER BY id"})
    board = s.sql_boards.save_dashboard(ANA, {"name": "Regional sales L131", "shared": True, "tiles": [{"query": query["id"]}]})

    servers: dict[str, tuple] = {}

    def root_app(name: str):
        root = FastAPI()

        @root.get("/login")
        def login_page(redirect_uri: str = "/cairn-ui/app.html"):
            # The stand-in for single sign-on: a form, as an identity provider shows one.
            safe = redirect_uri if redirect_uri.startswith("/") and not redirect_uri.startswith("//") else "/"
            return HTMLResponse(
                "<!doctype html><title>Sign in</title><h1>Sign in to Cairn</h1>"
                f"<form method=post action='/login'><input type=hidden name=redirect_uri value='{safe}'>"
                "<label>User <input name=username autocomplete=username></label>"
                "<button type=submit>Sign in</button></form>")

        @root.post("/login")
        async def login(request: Request):
            form = await request.form()
            token = secrets.token_urlsafe(24)
            sessions[token] = f"urn:li:corpuser:{form['username']}"
            target = str(form.get("redirect_uri") or "/")
            r = RedirectResponse(target if target.startswith("/") and not target.startswith("//") else "/", status_code=303)
            r.set_cookie("PLAY_SESSION", token, httponly=True, samesite="lax", path="/")
            return r

        @root.get("/logout")
        def logout(request: Request):
            sessions.pop(request.cookies.get("PLAY_SESSION", ""), None)
            r = RedirectResponse("/login", status_code=303)
            r.delete_cookie("PLAY_SESSION", path="/")
            return r

        # The test's own controls (the Node script drives the app; these change the world around it).
        @root.post("/__test/notify")
        async def notify(request: Request):
            body = await request.json()
            ids = s.notify.publish(Event(type="discussion.mention", title=body["title"], body=body.get("body", ""),
                                         recipients=[body["to"]], url=body.get("url")))
            return {"ids": ids}

        @root.post("/__test/stop")
        def stop():
            def later():
                time.sleep(0.3)
                servers[name][0].should_exit = True
            threading.Thread(target=later, daemon=True).start()
            return {"stopping": name}

        root.mount("/cairn/api", create_api_app(s))
        root.mount("/cairn-ui", StaticFiles(directory=BUNDLE))
        return root

    for name in ("alpha", "beta"):
        port = _port()
        servers[name] = (*_serve(root_app(name), port), port)
    yield {"s": s, "board": board, "alpha": f"http://127.0.0.1:{servers['alpha'][2]}",
           "beta": f"http://localhost:{servers['beta'][2]}", "sessions": sessions}
    for server, thread, _ in servers.values():
        server.should_exit = True
        thread.join(10)
    core_server.should_exit = True


def _cli(tmp_path) -> str:
    binary = os.environ.get("CAIRN_CLI_BIN")
    if binary:
        return binary
    wrapper = tmp_path / "cairn"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} -m cairn_cli.main \"$@\"\n")
    wrapper.chmod(0o755)
    return str(wrapper)


def test_the_desktop_app_against_real_cairns(stack, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy") and not k.startswith(("CAIRN_", "DBUS_"))}
    env.update({
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
        "CAIRN_E2E_ALPHA": stack["alpha"], "CAIRN_E2E_BETA": stack["beta"], "CAIRN_E2E_BOARD": stack["board"]["id"],
        "CAIRN_E2E_CLI": _cli(tmp_path), "CAIRN_E2E_HOME": str(home),
        "CAIRN_E2E_SHOTS": os.environ.get("CAIRN_E2E_SHOTS", ""), "PYTHONPATH": os.path.join(ROOT, "src"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.SecretService.Keyring",
    })
    # A D-Bus session with an unlocked GNOME Keyring (the OS keychain), a virtual display, then the app.
    inner = ("printf '%s' \"$KEYRING_PASSWORD\" | gnome-keyring-daemon --unlock --components=secrets >/dev/null && "
             f"exec xvfb-run -a -s '-screen 0 1440x960x24' node --test --test-reporter=spec {SCRIPT}")
    env["KEYRING_PASSWORD"] = secrets.token_hex(12)
    p = subprocess.run(["dbus-run-session", "--", "sh", "-c", inner], env=env, cwd=DESKTOP, capture_output=True,
                       text=True, timeout=600)
    out = p.stdout + p.stderr
    print(out[-20000:])
    assert p.returncode == 0, out[-6000:]
    assert "✔ Cairn Desktop against real Cairns" in out and "✖" not in out and "﹣" not in out

    # What the app left: the CLI's config.toml has both workspaces, and nothing else of it is readable plaintext.
    config = (home / ".config" / "cairn" / "config.toml").read_text()
    assert '[profiles."alpha"]' in config and '[profiles."beta"]' in config and "token" not in config.lower()
    for path in (home / ".config").rglob("*"):
        if path.is_file() and "offline" in path.parts:
            assert b"Regional sales L131" not in path.read_bytes()
