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
    def __init__(self, engine, secret: str, audit=None, viewer_token: Optional[Callable[[str], Optional[str]]] = None):
        if not secret:
            raise AppError("data apps need CAIRN_APPS_SECRET (or the service token)", 503)
        self.engine, self.secret, self.audit, self.viewer_token = engine, secret.encode(), audit, viewer_token
        self._running: dict[str, _Running] = {}
        self._lock = asyncio.Lock()
        self._swept = time.monotonic()

    @staticmethod
    def urn(name: str) -> str:
        return f"urn:li:dataApp:{name}"

    def key(self, name: str) -> str:
        return hmac.new(self.secret, f"cairn-app:{name}".encode(), hashlib.sha256).hexdigest()

    # ---- definitions --------------------------------------------------------------------
    def _check(self, body: dict) -> dict:
        url, command = body.get("url"), body.get("command")
        if bool(url) == bool(command):
            raise AppError("give url (an app running elsewhere) or command (one Cairn starts), not both")
        if url and not re.match(r"^https?://[^/\s]+(/\S*)?$", str(url)):
            raise AppError("url: the app's http(s):// address")
        if command is not None and (not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command)):
            raise AppError("command: a list of strings, the program first")
        idle = body.get("idle_minutes", 30)
        if isinstance(idle, bool) or not isinstance(idle, int) or not 1 <= idle <= 1440:
            raise AppError("idle_minutes is 1-1440")
        return {"title": str(body.get("title") or body.get("name") or "")[:256], "url": str(url).rstrip("/") if url else None,
                "command": json.dumps(command) if command else None, "workdir": body.get("workdir") or None,
                "idle_minutes": idle}

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
        self.get(name)
        values = self._check({**body, "name": name})
        with self.engine.begin() as conn:
            conn.execute(update(data_apps).where(data_apps.c.name == name).values(updated_ms=int(time.time() * 1000), **values))
            if self.audit is not None:
                self.audit.record("apps", "app.updated", actor, self.urn(name), values, conn=conn)
        self.stop(name)
        return self.get(name)

    def delete(self, actor: str, name: str) -> None:
        self.get(name)
        self.stop(name)
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
        d["running"] = name in self._running and self._running[name].proc.poll() is None
        return d

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
        async with self._lock:
            run = self._running.get(name)
            if run is None or run.proc.poll() is not None:
                port = _free_port()
                base = f"apps/{name}"
                cmd = [c.replace("{port}", str(port)).replace("{base}", base) for c in app["command"]]
                env = {**os.environ, "PORT": str(port), "CAIRN_APP_BASE": f"/{base}/", "CAIRN_APP_KEY": self.key(name)}
                for k in ("CAIRN_APPS_SECRET", "CAIRN_SERVICE_TOKEN"):
                    env.pop(k, None)                  # the app gets its own key, never Cairn's secrets
                proc = subprocess.Popen(cmd, cwd=app["workdir"] or None, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        return [{k: v for k, v in a.items() if k != "command"} for a in apps.list() if may_use(p, a["name"])]

    @router.post("/v1/apps", status_code=201)
    def create(body: dict = Body(...), p=Depends(principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: apps.create(p.urn, body))

    @router.put("/v1/apps/{name}")
    def change(name: str, body: dict = Body(...), p=Depends(principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: apps.update(p.urn, name, body))

    @router.delete("/v1/apps/{name}", status_code=204)
    def remove(name: str, p=Depends(principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        wrap(lambda: apps.delete(p.urn, name))

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
