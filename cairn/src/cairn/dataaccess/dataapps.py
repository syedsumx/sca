"""Data apps: Streamlit (or any web) apps served by Cairn, each viewer signed in and governed as themselves.

An operator (``MANAGE_SYSTEM_OPERATIONS``) defines an app with ``POST /v1/apps``: ``name``, ``title``, and
where it runs:

* ``url``: the app runs elsewhere (a Deployment beside Cairn, say) and Cairn only fronts it; or
* ``command``: Cairn starts it on the first visit (``{port}`` and ``{base}`` in the command are filled in) and
  stops it after ``idle_minutes`` without traffic, for single-node installs. For Streamlit:
  ``["streamlit", "run", "app.py", "--server.port", "{port}", "--server.baseUrlPath", "{base}",
  "--server.headless", "true"]``.

People open ``/apps/<name>/``. Every request, page loads and WebSockets alike, goes through Cairn:

* **Signed in.** The viewer is who Cairn says (their session or token). Without one: 401.
* **Allowed.** Opening an app needs ``USE_APP`` on ``urn:li:dataApp:<name>``. Others get 403, and the app
  never sees their request.
* **Told who is viewing.** Cairn removes any ``X-Cairn-*`` header the browser sent, then adds
  ``X-Cairn-User`` (the viewer's urn), ``X-Cairn-App-Key`` (the app's own key, so an app reached by ``url``
  can refuse traffic that did not come through Cairn) and, where Trino is set up, ``X-Cairn-Trino-Token``:
  a token for Trino as the viewer, valid for ten minutes. Queries the app runs with it are the viewer's,
  under their policies, masks and row filters, never the app's.

**From git.** ``git`` ({``url`` or ``repo``, ``branch``, ``path``}) names the app's code: a repository
configured for Cairn's git integration (its token is used) or a URL the operator allows
(``CAIRN_APP_GIT_URLS``, URL prefixes). Cairn deploys the branch's current commit when the app is created,
and again on ``POST /v1/apps/<name>/deploy``; the app records the commit it runs. ``command`` runs in
``path`` of that commit.

**Runtimes.** ``runtime: local`` (the default) runs ``command`` beside Cairn. ``runtime: kubernetes`` runs
it in ``image`` on Kubernetes, a Deployment per app that is started on the first visit and stopped when
idle (``cairn.dataaccess.apps_k8s``).

Each app's key is ``HMAC(CAIRN_APPS_SECRET, name)``: nothing to store, and it changes with the secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import subprocess
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, select, update
from starlette.requests import Request
from starlette.websockets import WebSocket

from cairn.common.db import metadata

logger = logging.getLogger(__name__)
PRIVILEGE = "USE_APP"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
SUBPATH = re.compile(r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]*$")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,511}$")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade", "host", "content-length"}

data_apps = Table(
    "cairn_data_apps", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("title", String(256), nullable=False),
    Column("url", String(1024), nullable=True),
    Column("command", Text, nullable=True),            # JSON list
    Column("workdir", String(1024), nullable=True),
    Column("idle_minutes", Integer, nullable=False, default=30),
    Column("runtime", String(16), nullable=False, default="local", server_default="local"),
    Column("git", Text, nullable=True),                # JSON: {url | repo, branch, path}
    Column("image", String(512), nullable=True),       # the kubernetes runtime's image
    Column("port", Integer, nullable=True),
    Column("commit", String(64), nullable=True),       # what is deployed
    Column("deployed_ms", BigInteger, nullable=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Running:
    def __init__(self, proc: subprocess.Popen, port: int):
        self.proc, self.port, self.last = proc, port, time.monotonic()


class DataApps:
    def __init__(self, engine, secret: str, audit=None, viewer_token: Optional[Callable[[str], Optional[str]]] = None,
                 git=None, kube=None, workdir: Optional[str] = None, git_urls: Optional[list[str]] = None):
        """``git``: Cairn's GitService (configured repositories); ``kube``: the Kubernetes runtime (KubeApps);
        ``git_urls``: URL prefixes apps may be deployed from."""
        if not secret:
            raise AppError("data apps need CAIRN_APPS_SECRET (or the service token)", 503)
        self.engine, self.secret, self.audit, self.viewer_token = engine, secret.encode(), audit, viewer_token
        self.git, self.kube = git, kube
        self.workdir = workdir or os.path.join(os.environ.get("CAIRN_DATA_DIR", "/tmp/cairn"), "apps")
        self.git_urls = [u for u in (git_urls or []) if u]
        self._running: dict[str, _Running] = {}
        # Apps installed from an app package (cairn.dataaccess.app_packages): another party's code. They run with
        # a minimal environment, never Cairn's, and get a Trino token of their own (app_token(name)).
        self.isolated: Callable[[str], bool] = lambda name: False
        self.app_token: Optional[Callable[[str], Optional[str]]] = None
        self._lock = asyncio.Lock()
        self._swept = time.monotonic()

    @staticmethod
    def urn(name: str) -> str:
        return f"urn:li:dataApp:{name}"

    def key(self, name: str) -> str:
        return hmac.new(self.secret, f"cairn-app:{name}".encode(), hashlib.sha256).hexdigest()

    # ---- definitions --------------------------------------------------------------------
    def _check_git(self, raw: Any) -> Optional[str]:
        if raw in (None, {}):
            return None
        if not isinstance(raw, dict):
            raise AppError("git: {url or repo, branch, path}")
        url, repo = raw.get("url"), raw.get("repo")
        if bool(url) == bool(repo):
            raise AppError("git: give url (one the operator allows) or repo (a configured repository), not both")
        if repo:
            self._repo(str(repo))
        elif not any(str(url).startswith(p) for p in self.git_urls):
            raise AppError("git: that URL is not one apps may use (CAIRN_APP_GIT_URLS)", 403)
        branch, path = str(raw.get("branch") or "main"), str(raw.get("path") or "").strip("/")
        if not BRANCH.match(branch) or ".." in branch or branch.startswith(("-", "/")):
            raise AppError("git.branch: a branch name")
        if not SUBPATH.match(path) or ".." in path.split("/"):
            raise AppError("git.path: a folder in the repository")
        commit = raw.get("commit")
        if commit is not None and not re.fullmatch(r"[0-9a-f]{40,64}", str(commit)):
            raise AppError("git.commit: a full commit id")
        return json.dumps({"url": str(url) if url else None, "repo": str(repo) if repo else None, "branch": branch, "path": path,
                           **({"commit": str(commit)} if commit else {})})

    def _check(self, body: dict) -> dict:
        url, command = body.get("url"), body.get("command")
        runtime = str(body.get("runtime") or "local")
        if runtime not in ("local", "kubernetes"):
            raise AppError("runtime is local or kubernetes")
        git = self._check_git(body.get("git"))
        if bool(url) == bool(command):
            raise AppError("give url (an app running elsewhere) or command (one Cairn starts), not both")
        if url and (git or runtime != "local"):
            raise AppError("an app at a url runs elsewhere: it takes neither git nor a runtime")
        image, port = body.get("image"), body.get("port", 8501)
        if runtime == "kubernetes":
            if self.kube is None:
                raise AppError("Kubernetes is not set up for data apps (CAIRN_APPS_K8S_NAMESPACE)", 503)
            if not image or not IMAGE.match(str(image)):
                raise AppError("image: the container image the app runs in")
            if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
                raise AppError("port: the port the app listens on (1024-65535)")
            if body.get("workdir"):
                raise AppError("workdir: a Kubernetes app runs in its image (or in git.path of its code)")
        elif image:
            raise AppError("image is for runtime kubernetes")
        if git and body.get("workdir"):
            raise AppError("workdir: an app from git runs in git.path")
        if url and not re.match(r"^https?://[^/\s]+(/\S*)?$", str(url)):
            raise AppError("url: the app's http(s):// address")
        if command is not None and (not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command)):
            raise AppError("command: a list of strings, the program first")
        idle = body.get("idle_minutes", 30)
        if isinstance(idle, bool) or not isinstance(idle, int) or not 1 <= idle <= 1440:
            raise AppError("idle_minutes is 1-1440")
        return {"title": str(body.get("title") or body.get("name") or "")[:256], "url": str(url).rstrip("/") if url else None,
                "command": json.dumps(command) if command else None, "workdir": body.get("workdir") or None,
                "idle_minutes": idle, "runtime": runtime, "git": git,
                "image": str(image) if runtime == "kubernetes" else None, "port": port if runtime == "kubernetes" else None}

    def create(self, actor: str, body: dict) -> dict:
        from sqlalchemy.exc import IntegrityError

        name = str(body.get("name") or "")
        if not NAME.match(name):
            raise AppError("name: lowercase letters, digits and -, starting with a letter")
        values = self._check(body)
        now = int(time.time() * 1000)
        try:
            with self.engine.begin() as conn:
                conn.execute(data_apps.insert().values(id=uuid.uuid4().hex, name=name, created_by=actor, created_ms=now,
                                                       updated_ms=now, **values))
                if self.audit is not None:
                    self.audit.record("apps", "app.created", actor, self.urn(name), values, conn=conn)
        except IntegrityError:
            raise AppError("an app of that name exists", 409)
        return self.get(name)

    def update(self, actor: str, name: str, body: dict) -> dict:
        old = self.get(name)
        values = self._check({**body, "name": name})
        if old["runtime"] != values["runtime"]:
            raise AppError("runtime: delete the app and create it again to move it", 409)
        with self.engine.begin() as conn:
            conn.execute(update(data_apps).where(data_apps.c.name == name).values(updated_ms=int(time.time() * 1000), **values))
            if self.audit is not None:
                self.audit.record("apps", "app.updated", actor, self.urn(name), values, conn=conn)
        self.stop(name)
        return self.get(name)

    def needs_deploy(self, name: str) -> bool:
        app = self.get(name)
        return bool(app["git"]) or app["runtime"] == "kubernetes"

    async def delete(self, actor: str, name: str) -> None:
        app = self.get(name)
        self.stop(name)
        if app["runtime"] == "kubernetes" and self.kube is not None:
            await self._kube(self.kube.delete(name))
        with self.engine.begin() as conn:
            conn.execute(delete(data_apps).where(data_apps.c.name == name))
            if self.audit is not None:
                self.audit.record("apps", "app.deleted", actor, self.urn(name), {}, conn=conn)

    def get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(data_apps).where(data_apps.c.name == name)).first()
        if r is None:
            raise AppError("no such app", 404)
        d = dict(r._mapping)
        d["command"] = json.loads(d["command"]) if d["command"] else None
        d["git"] = json.loads(d["git"]) if d["git"] else None
        d["running"] = (name in self._running and self._running[name].proc.poll() is None) if d["runtime"] == "local" else None
        return d

    # ---- code from git ----------------------------------------------------------------------
    def _repo(self, rid: str):
        if self.git is None:
            raise AppError("no git repositories are configured", 404)
        try:
            return self.git.repo(rid)
        except Exception:
            raise AppError(f"no repository {rid!r}", 404)

    def _remote(self, git: dict) -> tuple[str, Optional[str]]:
        """The clone URL and the header that signs in to it (None: none needed)."""
        if git.get("repo"):
            from cairn.sqlworkspace.gitfolders import repo_remote

            return repo_remote(self._repo(git["repo"]))
        return git["url"], None

    def _git(self, cwd: Optional[str], *args: str, header: Optional[str] = None, timeout: int = 300) -> str:
        protocols = ["https", "ssh"] + [p for p in ("file", "git") if any(u.startswith(f"{p}://") for u in self.git_urls)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.workdir, "GIT_TERMINAL_PROMPT": "0",
               "GIT_ALLOW_PROTOCOL": ":".join(protocols), "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C"}
        if header:      # through the environment: never on a command line, never in the clone's config
            env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": header})
        r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise AppError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[-500:]}", 502)
        return r.stdout

    def _checkout(self, name: str, url: str, header: Optional[str], branch: str, commit: Optional[str] = None) -> str:
        """The app's clone, at the tip of its branch (or at ``commit`` on it); its commit."""
        import shutil

        d = os.path.join(self.workdir, name)
        os.makedirs(self.workdir, exist_ok=True)
        if not os.path.isdir(os.path.join(d, ".git")):
            shutil.rmtree(d, ignore_errors=True)
            self._git(None, "clone", "--quiet", "--no-tags", "--branch", branch, "--", url, d, header=header)
        else:
            self._git(d, "remote", "set-url", "origin", url)
            self._git(d, "fetch", "--quiet", "--no-tags", "origin", branch, header=header)
        self._git(d, "checkout", "--quiet", "--force", "-B", branch, f"origin/{branch}")
        if commit:
            # A pinned commit must be on the branch (a package version cannot point anywhere else).
            self._git(d, "merge-base", "--is-ancestor", commit, "HEAD")
            self._git(d, "checkout", "--quiet", "--force", commit)
        self._git(d, "clean", "-fdqx")
        return self._git(d, "rev-parse", "HEAD").strip()

    def _head(self, url: str, header: Optional[str], branch: str) -> str:
        out = self._git(None, "ls-remote", "--", url, f"refs/heads/{branch}", header=header).split()
        if not out or not re.fullmatch(r"[0-9a-f]{40,64}", out[0]):
            raise AppError(f"git: no branch {branch!r} there", 404)
        return out[0]

    @staticmethod
    async def _kube(coro):
        from cairn.dataaccess.apps_k8s import KubeError

        try:
            return await coro
        except KubeError as e:
            raise AppError(str(e), e.status)

    async def deploy(self, actor: str, name: str) -> dict:
        """Deploys the branch's current commit (for an app from git) and, on Kubernetes, the app's objects."""
        app = self.get(name)
        if not app["git"] and app["runtime"] != "kubernetes":
            raise AppError("only an app from git, or one on Kubernetes, is deployed")
        git, commit = app["git"], None
        info = None
        if git:
            url, header = self._remote(git)
            if app["runtime"] == "local":
                self.stop(name)                          # the next visit starts the new commit
                commit = await asyncio.to_thread(self._checkout, name, url, header, git["branch"], git.get("commit"))
            else:
                commit = git.get("commit") or await asyncio.to_thread(self._head, url, header, git["branch"])
            info = {"url": url, "branch": git["branch"], "commit": commit, "path": git["path"], "header": header}
        if app["runtime"] == "kubernetes":
            await self._kube(self.kube.deploy(app, self.key(name), info))
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            conn.execute(update(data_apps).where(data_apps.c.name == name).values(commit=commit, deployed_ms=now))
            if self.audit is not None:
                self.audit.record("apps", "app.deployed", actor, self.urn(name), {"commit": commit, "runtime": app["runtime"]},
                                  conn=conn)
        return self.get(name)

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            names = [r.name for r in conn.execute(select(data_apps.c.name).order_by(data_apps.c.name))]
        return [self.get(n) for n in names]

    # ---- running ----------------------------------------------------------------------------
    async def upstream(self, name: str) -> str:
        """Where the app answers, started if Cairn runs it."""
        app = self.get(name)
        if app["url"]:
            return app["url"]
        if app["runtime"] == "kubernetes":
            return await self._kube(self.kube.upstream(app))
        if app["git"] and not app["commit"]:
            raise AppError(f"{name} is not deployed yet", 503)
        cwd = os.path.join(self.workdir, name, app["git"]["path"]) if app["git"] else (app["workdir"] or None)
        async with self._lock:
            run = self._running.get(name)
            if run is None or run.proc.poll() is not None:
                port = _free_port()
                base = f"apps/{name}"
                cmd = [c.replace("{port}", str(port)).replace("{base}", base) for c in app["command"]]
                if self.isolated(name):              # another party's code: nothing of Cairn's environment
                    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
                    env.update({"HOME": cwd or self.workdir, "PORT": str(port), "CAIRN_APP_BASE": f"/{base}/",
                                "CAIRN_APP_KEY": self.key(name)})
                else:
                    env = {**os.environ, "PORT": str(port), "CAIRN_APP_BASE": f"/{base}/", "CAIRN_APP_KEY": self.key(name)}
                    for k in ("CAIRN_APPS_SECRET", "CAIRN_SERVICE_TOKEN"):
                        env.pop(k, None)              # the app gets its own key, never Cairn's secrets
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                run = _Running(proc, port)
                self._running[name] = run
                end = time.monotonic() + 60
                while True:
                    try:
                        _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
                        w.close()
                        break
                    except (OSError, asyncio.TimeoutError):
                        if proc.poll() is not None or time.monotonic() > end:
                            self._running.pop(name, None)
                            raise AppError(f"{name} did not start", 502)
                        await asyncio.sleep(0.2)
            run.last = time.monotonic()
            return f"http://127.0.0.1:{run.port}"

    def stop(self, name: str) -> None:
        run = self._running.pop(name, None)
        if run and run.proc.poll() is None:
            run.proc.terminate()
            try:
                run.proc.wait(10)
            except subprocess.TimeoutExpired:
                run.proc.kill()

    def maybe_sweep(self) -> None:
        """Stops idle apps, at most once a minute (apps run beside the API, so the API sweeps)."""
        if time.monotonic() - self._swept > 60:
            self._swept = time.monotonic()
            self.stop_idle()
            if self.kube is not None:
                asyncio.get_running_loop().create_task(self.stop_idle_kube())

    async def stop_idle_kube(self) -> list[str]:
        idle = {a["name"]: a["idle_minutes"] for a in self.list() if a["runtime"] == "kubernetes"}
        try:
            return await self.kube.stop_idle(idle) if idle else []
        except Exception as e:  # noqa: BLE001 - the next sweep tries again
            logger.warning("data apps: stopping idle Kubernetes apps failed: %s", e)
            return []

    def stop_idle(self) -> list[str]:
        stopped = []
        for name, run in list(self._running.items()):
            try:
                idle = self.get(name)["idle_minutes"]
            except AppError:
                idle = 0
            if time.monotonic() - run.last > idle * 60 or run.proc.poll() is not None:
                self.stop(name)
                stopped.append(name)
        return stopped

    def headers_for(self, name: str, viewer: str, incoming: dict) -> dict:
        out = {k: v for k, v in incoming.items() if k.lower() not in HOP and not k.lower().startswith("x-cairn-")
               and k.lower() != "authorization"}
        out["X-Cairn-User"] = viewer
        out["X-Cairn-App-Key"] = self.key(name)
        if self.viewer_token is not None:
            tok = self.viewer_token(viewer)
            if tok:
                out["X-Cairn-Trino-Token"] = tok
        if self.app_token is not None:
            tok = self.app_token(name)                 # an installed app: its own identity, its bound tables only
            if tok:
                out["X-Cairn-App-Trino-Token"] = tok
        return out


def build_apps_router(apps: DataApps, services) -> Any:
    import httpx
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.responses import StreamingResponse

    from cairn.common.security import AuthenticationError

    router = APIRouter(tags=["apps"])

    def viewer_of(headers: dict, cookies: dict):
        try:
            return services.identity.resolve(headers, cookies)
        except AuthenticationError as e:
            raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"})

    def principal(request: Request):
        return viewer_of(dict(request.headers), dict(request.cookies))

    def may_use(p, name: str) -> bool:
        return services.authorize(p, PRIVILEGE, apps.urn(name), raise_on_deny=False)

    def wrap(fn):
        try:
            return fn()
        except AppError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/v1/apps")
    def list_apps(p=Depends(principal)):
        hidden = ("command", "git", "image", "workdir")                  # how it runs is the operators' business
        return [{k: v for k, v in a.items() if k not in hidden} for a in apps.list() if may_use(p, a["name"])]

    async def awrap(coro):
        try:
            return await coro
        except AppError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/v1/apps", status_code=201)
    async def create(body: dict = Body(...), p=Depends(principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        app = wrap(lambda: apps.create(p.urn, body))
        if apps.needs_deploy(app["name"]):
            try:
                app = await apps.deploy(p.urn, app["name"])
            except AppError as e:                         # nothing half-made: the app is not kept
                await apps.delete(p.urn, app["name"])
                raise HTTPException(e.status, f"the app could not be deployed: {e}")
        return app

    @router.put("/v1/apps/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        app = wrap(lambda: apps.update(p.urn, name, body))
        if apps.needs_deploy(name):
            app = await awrap(apps.deploy(p.urn, name))
        return app

    @router.post("/v1/apps/{name}/deploy")
    async def deploy(name: str, p=Depends(principal)):
        """Deploys the app's branch as it is now (and its Kubernetes objects)."""
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return await awrap(apps.deploy(p.urn, name))

    @router.delete("/v1/apps/{name}", status_code=204)
    async def remove(name: str, p=Depends(principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        await awrap(apps.delete(p.urn, name))

    client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False)

    @router.get("/apps/{name}", include_in_schema=False)
    async def bare(name: str):
        from starlette.responses import RedirectResponse

        return RedirectResponse(f"/apps/{name}/", status_code=307)

    @router.api_route("/apps/{name}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def proxy(name: str, path: str, request: Request):
        apps.maybe_sweep()
        p = principal(request)
        try:
            apps.get(name)
        except AppError:
            raise HTTPException(404, "no such app")
        if not may_use(p, name):
            raise HTTPException(403, f"opening {name} needs {PRIVILEGE} on it")
        try:
            base = await apps.upstream(name)
        except AppError as e:
            raise HTTPException(e.status, str(e))
        url = f"{base}/apps/{name}/{path}" + (f"?{request.url.query}" if request.url.query else "")
        req = client.build_request(request.method, url, headers=apps.headers_for(name, p.urn, dict(request.headers)),
                                   content=await request.body())
        resp = await client.send(req, stream=True)
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}

        async def body():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
        return StreamingResponse(body(), status_code=resp.status_code, headers=headers)

    @router.websocket("/apps/{name}/{path:path}")
    async def proxy_ws(ws: WebSocket, name: str, path: str):
        import websockets

        try:
            p = viewer_of(dict(ws.headers), dict(ws.cookies))
        except HTTPException:
            await ws.close(code=4401)
            return
        try:
            apps.get(name)
        except AppError:
            await ws.close(code=4404)
            return
        if not may_use(p, name):
            await ws.close(code=4403)
            return
        base = await apps.upstream(name)
        target = base.replace("http", "ws", 1) + f"/apps/{name}/{path}" + (f"?{ws.url.query}" if ws.url.query else "")
        headers = {k: v for k, v in apps.headers_for(name, p.urn, dict(ws.headers)).items()
                   if not k.lower().startswith("sec-websocket") and k.lower() not in ("origin", "cookie")}
        protocols = [s.strip() for s in ws.headers.get("sec-websocket-protocol", "").split(",") if s.strip()]
        async with websockets.connect(target, additional_headers=headers, subprotocols=protocols or None,
                                      max_size=None, origin=None) as up:
            await ws.accept(subprotocol=up.subprotocol)

            async def down():
                try:
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            break
                        await up.send(m["bytes"] if m.get("bytes") is not None else m["text"])
                finally:
                    await up.close()

            async def upward():
                async for m in up:
                    await (ws.send_bytes(m) if isinstance(m, bytes) else ws.send_text(m))
            tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
        try:
            await ws.close()
        except RuntimeError:
            pass

    return router
