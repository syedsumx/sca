"""Warehouses: a Trino per team that stops when idle and starts on the first query (``cairn warehouses``).

Each warehouse is its own Trino (coordinator, optionally workers): its own size, its own queue, its own
bill. ``cairn warehouses`` listens on one port per warehouse and forwards Trino's protocol to it:

* **Scale to zero.** After ``idle_minutes`` with no client traffic (a running query is polled by its
  client, so it counts as traffic), the warehouse is stopped. The next request starts it, waits until
  Trino answers (``start_timeout_seconds``), and goes through; clients see a slower first query.
* **How it is stopped and started.** On Kubernetes, the warehouse's Deployment is scaled to 0 and back
  (``scale`` subresource, with the pod's service account). With Docker (Compose), its container is
  stopped and started through the Docker API.
* **Who may use it.** Trino asks Cairn about every query; each warehouse's Trino asks as that warehouse
  (``/v1/trino/<key>/warehouse/<name>/allow``), and Cairn admits ``ExecuteQuery`` only for members of the
  warehouse's groups (``CAIRN_TRINO_WAREHOUSES``: ``{"<name>": {"groups": ["urn:li:corpGroup:sales"]}}``)
  or operators. Data access is decided as everywhere else.

Trino builds the URLs it hands back from the forwarded headers (``http-server.process-forwarded=true``),
so clients keep talking to the proxy.

Settings: ``CAIRN_WAREHOUSES`` (JSON or a file): ``{"warehouses": [{"name", "port", "upstream",
"idle_minutes", "start_timeout_seconds", "kubernetes": {"namespace", "deployment"} | "docker":
{"container"}}]}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)
NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade", "host", "content-length"}


class WarehouseError(Exception):
    pass


class KubernetesScaler:
    """A Deployment's replicas, through the API server, with the pod's service account."""

    def __init__(self, namespace: str, deployment: str, api: str = "", token_file: str = "", ca_file: str = ""):
        sa = "/var/run/secrets/kubernetes.io/serviceaccount"
        self.namespace, self.deployment = namespace, deployment
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        self.token_file = token_file or f"{sa}/token"
        self.verify: Any = ca_file or (f"{sa}/ca.crt" if os.path.exists(f"{sa}/ca.crt") else True)
        if not self.api:
            raise WarehouseError("kubernetes: no API server (not in a cluster, and no api given)")

    def _client(self):
        import httpx

        token = open(self.token_file).read().strip()
        return httpx.AsyncClient(base_url=self.api, verify=self.verify, timeout=30,
                                 headers={"Authorization": f"Bearer {token}"})

    def _path(self, sub: str = "") -> str:
        return f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}{sub}"

    async def scale(self, replicas: int) -> None:
        async with self._client() as c:
            r = await c.patch(self._path("/scale"), json={"spec": {"replicas": replicas}},
                              headers={"Content-Type": "application/merge-patch+json"})
            if r.status_code >= 300:
                raise WarehouseError(f"kubernetes: scaling {self.deployment}: {r.status_code} {r.text[:300]}")

    async def state(self) -> tuple[int, int]:
        """(wanted replicas, ready replicas)."""
        async with self._client() as c:
            r = await c.get(self._path())
            if r.status_code >= 300:
                raise WarehouseError(f"kubernetes: reading {self.deployment}: {r.status_code} {r.text[:300]}")
            d = r.json()
            return int((d.get("spec") or {}).get("replicas") or 0), int((d.get("status") or {}).get("readyReplicas") or 0)

    async def start(self) -> None:
        await self.scale(1)

    async def stop(self) -> None:
        await self.scale(0)

    async def running(self) -> bool:
        return (await self.state())[0] > 0


class DockerScaler:
    """A container, through the Docker Engine API (Compose)."""

    def __init__(self, container: str, socket: str = "/var/run/docker.sock"):
        self.container, self.socket = container, socket

    def _client(self):
        import httpx

        return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket), base_url="http://docker", timeout=60)

    async def _post(self, action: str) -> None:
        async with self._client() as c:
            r = await c.post(f"/containers/{self.container}/{action}")
            if r.status_code not in (204, 304):          # 304: already in that state
                raise WarehouseError(f"docker: {action} {self.container}: {r.status_code} {r.text[:300]}")

    async def start(self) -> None:
        await self._post("start")

    async def stop(self) -> None:
        await self._post("stop?t=30")

    async def running(self) -> bool:
        async with self._client() as c:
            r = await c.get(f"/containers/{self.container}/json")
            if r.status_code >= 300:
                raise WarehouseError(f"docker: inspecting {self.container}: {r.status_code} {r.text[:300]}")
            return bool(((r.json().get("State") or {}).get("Running")))


@dataclass
class Warehouse:
    name: str
    port: int
    upstream: str
    scaler: Any
    idle_minutes: float = 10
    start_timeout: float = 300
    tls_cert: str = ""        # the proxy's certificate and key: sign-in tokens pass through it
    tls_key: str = ""
    upstream_ca: Any = True   # a CA file for the warehouse's certificate
    warmup: float = 10        # after a start: Trino answers /v1/info a moment before its node takes queries
    last_seen: float = field(default_factory=time.monotonic)
    starts: int = 0
    stops: int = 0
    inflight: int = 0         # requests being answered (waiting for a start included): never stopped under them
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def ready(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=3, verify=self.upstream_ca) as c:
                r = await c.get(f"{self.upstream}/v1/info")
                return r.status_code == 200 and not r.json().get("starting", True)
        except (httpx.HTTPError, ValueError):
            return False

    async def wake(self) -> None:
        """Starts the warehouse if it is stopped and waits until Trino takes queries."""
        if await self.ready():
            return
        async with self._lock:                         # one start at a time, every waiter shares it
            if await self.ready():
                return
            if not await self.scaler.running():
                logger.info("warehouse %s: starting", self.name)
                await self.scaler.start()
                self.starts += 1
            end = time.monotonic() + self.start_timeout
            while not await self.ready():
                if time.monotonic() > end:
                    raise WarehouseError(f"warehouse {self.name} did not start within {self.start_timeout:.0f}s")
                await asyncio.sleep(1)
            await asyncio.sleep(self.warmup)

    async def reap(self, now: Optional[float] = None) -> bool:
        """Stops the warehouse when it has been idle long enough; True when it did."""
        now = now if now is not None else time.monotonic()
        if self.idle_minutes <= 0 or self.inflight or now - self.last_seen < self.idle_minutes * 60:
            return False
        async with self._lock:
            now = time.monotonic()
            if self.inflight or now - self.last_seen < self.idle_minutes * 60 or not await self.scaler.running():
                return False
            logger.info("warehouse %s: idle for %.0f minutes, stopping", self.name, (now - self.last_seen) / 60)
            await self.scaler.stop()
            self.stops += 1
            return True


def load(raw: Optional[dict] = None) -> list[Warehouse]:
    if raw is None:
        text = os.environ.get("CAIRN_WAREHOUSES", "").strip()
        if not text:
            raise WarehouseError("set CAIRN_WAREHOUSES")
        raw = json.loads(open(text).read() if not text.startswith("{") else text)
    out, ports, names = [], set(), set()
    for w in raw.get("warehouses") or []:
        name, port, upstream = str(w.get("name") or ""), w.get("port"), str(w.get("upstream") or "").rstrip("/")
        if not NAME.match(name) or name in names:
            raise WarehouseError(f"warehouse name {name!r}: lowercase letters, digits and '-', once each")
        if not isinstance(port, int) or not 1 <= port <= 65535 or port in ports:
            raise WarehouseError(f"warehouse {name}: a port of its own")
        if not upstream.startswith(("http://", "https://")):
            raise WarehouseError(f"warehouse {name}: upstream is the warehouse's Trino URL")
        if "kubernetes" in w:
            k = w["kubernetes"] or {}
            scaler: Any = KubernetesScaler(str(k.get("namespace") or ""), str(k.get("deployment") or ""), str(k.get("api") or ""),
                                           str(k.get("token_file") or ""), str(k.get("ca_file") or ""))
        elif "docker" in w:
            d = w["docker"] or {}
            scaler = DockerScaler(str(d.get("container") or ""), str(d.get("socket") or "/var/run/docker.sock"))
        else:
            raise WarehouseError(f"warehouse {name}: give kubernetes or docker (how it is stopped and started)")
        names.add(name)
        ports.add(port)
        tls = w.get("tls") or {}
        if bool(tls.get("cert")) != bool(tls.get("key")):
            raise WarehouseError(f"warehouse {name}: tls needs cert and key")
        out.append(Warehouse(name, port, upstream, scaler, float(w.get("idle_minutes", 10)),
                             float(w.get("start_timeout_seconds", 300)), str(tls.get("cert") or ""),
                             str(tls.get("key") or ""), str(w.get("upstream_ca") or "") or True,
                             float(w.get("warmup_seconds", 10))))
    if not out:
        raise WarehouseError("no warehouses")
    return out


def app_for(w: Warehouse):
    """The proxy in front of one warehouse (an ASGI app)."""
    import httpx
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=600, pool=30), verify=w.upstream_ca)

    async def health(_request: Request):
        return JSONResponse({"warehouse": w.name, "ready": await w.ready(), "starts": w.starts, "stops": w.stops})

    async def forward(request: Request):
        w.inflight += 1
        done = False
        try:
            resp = await _forward(request)
            done = not isinstance(resp, StreamingResponse)
            if not done:
                inner = resp.body_iterator

                async def counted():
                    try:
                        async for chunk in inner:
                            yield chunk
                    finally:
                        w.inflight -= 1
                        w.last_seen = time.monotonic()
                resp.body_iterator = counted()
            return resp
        finally:
            if done:
                w.inflight -= 1
                w.last_seen = time.monotonic()

    async def _forward(request: Request):
        w.last_seen = time.monotonic()
        try:
            await w.wake()
        except WarehouseError as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        host = request.headers.get("host", "")
        headers["X-Forwarded-Host"] = host                        # Trino's nextUri points back here
        headers["X-Forwarded-Proto"] = "https" if w.tls_cert else "http"
        url = w.upstream + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        req = client.build_request(request.method, url, headers=headers, content=await request.body())
        try:
            up = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"warehouse {w.name}: {e}"}, status_code=502)
        w.last_seen = time.monotonic()
        out_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP}
        if request.method == "HEAD":
            await up.aclose()
            return Response(status_code=up.status_code, headers=out_headers)

        async def body():
            try:
                async for chunk in up.aiter_raw():
                    w.last_seen = time.monotonic()
                    yield chunk
            finally:
                await up.aclose()
        return StreamingResponse(body(), status_code=up.status_code, headers=out_headers)

    return Starlette(routes=[Route("/cairn/warehouse", health),
                             Route("/{path:path}", forward, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])])


async def serve(warehouses: list[Warehouse], host: str = "0.0.0.0", reap_every: float = 30) -> None:
    import uvicorn

    servers = [uvicorn.Server(uvicorn.Config(app_for(w), host=host, port=w.port, log_level="warning", proxy_headers=False,
                                             ssl_certfile=w.tls_cert or None, ssl_keyfile=w.tls_key or None))
               for w in warehouses]

    async def reaper():
        while True:
            await asyncio.sleep(reap_every)
            for w in warehouses:
                try:
                    await w.reap()
                except Exception:  # noqa: BLE001 - try again next round
                    logger.exception("warehouse %s: stopping failed", w.name)
    await asyncio.gather(*(s.serve() for s in servers), reaper())
