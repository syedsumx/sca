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

* **Several nodes, scaled with the load.** A warehouse may have workers (``workers``): a Kubernetes
  Deployment, or a pool of containers with Docker. The proxy counts the queries running and queued
  through it (from the statement URLs their clients poll) and keeps ``ceil(queries / queries_per_worker)``
  workers, between ``min_workers`` and ``max_workers``: more at once when the load rises, fewer after
  ``scale_down_minutes`` of less load. A stopped warehouse has no workers.
* **Start now.** ``POST /cairn/warehouse/start`` on the warehouse's port, with ``X-Cairn-Warehouse-Key``
  (``CAIRN_WAREHOUSES_CONTROL_KEY``), starts it without a query (Cairn's ``POST /v1/warehouses/<name>/start``).
* **What it costs.** Node-seconds (the coordinator and each ready worker) are reported to Cairn every
  ``CAIRN_WAREHOUSES_REPORT_SECONDS`` (300) at ``CAIRN_WAREHOUSES_REPORT_URL``
  (``<api>/v1/warehouses/usage``, with ``CAIRN_TRINO_ACCESS_KEY``) and appear in the cost report, priced at
  ``CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR``. A report that fails is sent again with the same id, so
  nothing is counted twice.

* **Adaptive warehouses.** An adaptive warehouse (``"adaptive": [{"name", "port", "tiers": ["small", "large"],
  "large_when": {"cpu_ms", "input_bytes", "peak_memory_bytes"}, "overflow_at"}]``) has no size of its own: it is
  one port in front of several warehouses, smallest first, and picks one for each query. Cairn says how earlier
  runs of a query of the same shape went (``POST <api>/v1/warehouses/hint``: median CPU time, bytes read and
  peak memory from query history, by a fingerprint with the literals left out), and a query whose earlier runs
  reached any ``large_when`` figure goes to the largest tier. Every other query goes to the smallest, unless
  that one already has ``overflow_at`` queries running or queued and the largest has fewer: then it overflows
  there. A query stays on the tier it started on: its follow-up requests are routed by its query id. Each tier
  starts, scales and stops on its own as before, and is billed as itself. A routing hint that cannot be had
  (Cairn away, ``CAIRN_WAREHOUSES_HINT_URL`` unset) routes by load alone.

Trino builds the URLs it hands back from the forwarded headers (``http-server.process-forwarded=true``),
so clients keep talking to the proxy.

Settings: ``CAIRN_WAREHOUSES`` (JSON or a file): ``{"warehouses": [{"name", "port", "upstream",
"idle_minutes", "start_timeout_seconds", "kubernetes": {"namespace", "deployment"} | "docker":
{"container"}, "workers": {"min", "max", "queries_per_worker", "scale_down_minutes", "kubernetes":
{"namespace", "deployment"} | "docker": {"containers": [...]}}}]}``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)
NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
STATEMENT = re.compile(r"^/v1/statement/(queued|executing)/([A-Za-z0-9_]+)/")
QUERY_PATH = re.compile(r"^/v1/query/([A-Za-z0-9_]+)")
ACTIVE_SECONDS = 10   # a query whose client has not polled for this long is over (clients poll every second)
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


class DockerPool:
    """Workers as a fixed pool of containers (Compose): the first N run, the rest are stopped."""

    def __init__(self, containers: list[str], socket: str = "/var/run/docker.sock"):
        if not containers:
            raise WarehouseError("docker workers: name their containers")
        self.members = [DockerScaler(c, socket) for c in containers]

    async def scale(self, replicas: int) -> None:
        for i, m in enumerate(self.members):
            if i < replicas:
                await m.start()
            else:
                await m.stop()

    async def state(self) -> tuple[int, int]:
        """(running containers, running containers): Trino tells when each node is ready."""
        n = sum([await m.running() for m in self.members])
        return n, n


@dataclass
class Workers:
    scaler: Any                       # KubernetesScaler (a Deployment) or DockerPool: scale(n), state()
    min: int = 0
    max: int = 1
    queries_per_worker: float = 4
    scale_down_minutes: float = 5
    wanted: int = 0
    ready: int = 0
    low_since: Optional[float] = None
    scale_ups: int = 0
    scale_downs: int = 0


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
    workers: Optional[Workers] = None
    control_key: str = ""     # for POST /cairn/warehouse/start
    queries: dict = field(default_factory=dict)        # query id -> ("queued" | "executing", last polled)
    node_seconds: float = 0.0                          # not yet reported
    usage_since: float = field(default_factory=time.time)
    usage_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _usage_tick: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _scale_lock: asyncio.Lock = field(default_factory=asyncio.Lock)   # never waits behind a start

    def saw(self, path: str) -> None:
        m = STATEMENT.match(path)
        if m:
            self.queries[m.group(2)] = (m.group(1), time.monotonic())

    def load(self, now: Optional[float] = None) -> tuple[int, int]:
        """(running, queued) queries whose clients polled in the last ACTIVE_SECONDS."""
        now = now if now is not None else time.monotonic()
        for q in [q for q, (_, t) in self.queries.items() if now - t > ACTIVE_SECONDS]:
            del self.queries[q]
        queued = sum(1 for k, _ in self.queries.values() if k == "queued")
        return len(self.queries) - queued, queued

    async def autoscale(self, now: Optional[float] = None) -> Optional[int]:
        """Keeps the workers the load needs; the new count when it changed."""
        wk = self.workers
        if wk is None:
            return None
        now = now if now is not None else time.monotonic()
        async with self._scale_lock:
            wk.wanted, wk.ready = await wk.scaler.state()
            if wk.wanted == 0 and not await self.scaler.running():
                return None                            # stopped: no workers until it starts
            running, queued = self.load(now)
            need = min(wk.max, max(wk.min, math.ceil((running + queued) / wk.queries_per_worker)))
            if need > wk.wanted:
                logger.info("warehouse %s: %d queries, %d -> %d workers", self.name, running + queued, wk.wanted, need)
                await wk.scaler.scale(need)
                wk.wanted, wk.low_since, wk.scale_ups = need, None, wk.scale_ups + 1
                return need
            if need < wk.wanted:
                if wk.low_since is None:
                    wk.low_since = now
                elif now - wk.low_since >= wk.scale_down_minutes * 60:
                    logger.info("warehouse %s: %d queries, %d -> %d workers", self.name, running + queued, wk.wanted, need)
                    await wk.scaler.scale(need)
                    wk.wanted, wk.low_since, wk.scale_downs = need, None, wk.scale_downs + 1
                    return need
            else:
                wk.low_since = None
            return None

    async def count_usage(self) -> None:
        """Adds the node-seconds since the last count: the coordinator and each ready worker."""
        now = time.monotonic()
        dt, self._usage_tick = now - self._usage_tick, now
        try:
            nodes = 1 if await self.scaler.running() else 0
            if nodes and self.workers is not None:
                nodes += (await self.workers.scaler.state())[1]
        except Exception:  # noqa: BLE001 - unknown for this tick: not counted
            return
        self.node_seconds += nodes * dt

    async def report_usage(self, url: str, key: str) -> bool:
        """Sends what was counted since the last report; kept (same id) until Cairn has it."""
        import httpx

        if self.node_seconds <= 0:
            self.usage_since = time.time()
            return True
        end = time.time()
        body = {"id": self.usage_id, "warehouse": self.name, "start_ms": int(self.usage_since * 1000),
                "end_ms": int(end * 1000), "node_seconds": round(self.node_seconds, 1)}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(url, json=body, headers={"X-Cairn-Trino-Key": key})
            if r.status_code >= 300:
                raise WarehouseError(f"{r.status_code} {r.text[:200]}")
        except Exception as e:  # noqa: BLE001 - sent again next time, same id
            logger.warning("warehouse %s: usage report failed: %s", self.name, e)
            return False
        self.node_seconds, self.usage_since, self.usage_id = 0.0, end, uuid.uuid4().hex
        return True

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
            if self.workers is not None and self.workers.min and (await self.workers.scaler.state())[0] < self.workers.min:
                await self.workers.scaler.scale(self.workers.min)
                self.workers.wanted = self.workers.min
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
            if self.workers is not None:
                async with self._scale_lock:
                    await self.workers.scaler.scale(0)
                    self.workers.wanted, self.workers.low_since = 0, None
            await self.scaler.stop()
            self.stops += 1
            return True


def read_config() -> dict:
    """``CAIRN_WAREHOUSES``: JSON, or a file holding it."""
    text = os.environ.get("CAIRN_WAREHOUSES", "").strip()
    if not text:
        raise WarehouseError("set CAIRN_WAREHOUSES")
    return json.loads(open(text).read() if not text.startswith("{") else text)


def load(raw: Optional[dict] = None) -> list[Warehouse]:
    if raw is None:
        raw = read_config()
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
        workers = None
        if w.get("workers"):
            wk = w["workers"]
            if "kubernetes" in wk:
                k = wk["kubernetes"] or {}
                ws: Any = KubernetesScaler(str(k.get("namespace") or ""), str(k.get("deployment") or ""),
                                           str(k.get("api") or ""), str(k.get("token_file") or ""), str(k.get("ca_file") or ""))
            elif "docker" in wk:
                d = wk["docker"] or {}
                ws = DockerPool([str(c) for c in d.get("containers") or []], str(d.get("socket") or "/var/run/docker.sock"))
            else:
                raise WarehouseError(f"warehouse {name}: workers need kubernetes or docker")
            lo, hi = int(wk.get("min", 0)), int(wk.get("max", 1))
            if not 0 <= lo <= hi or hi < 1 or (isinstance(ws, DockerPool) and hi > len(ws.members)):
                raise WarehouseError(f"warehouse {name}: workers need 0 <= min <= max, max >= 1 (and that many containers)")
            qpw = float(wk.get("queries_per_worker", 4))
            if qpw <= 0:
                raise WarehouseError(f"warehouse {name}: queries_per_worker must be positive")
            workers = Workers(ws, lo, hi, qpw, float(wk.get("scale_down_minutes", 5)))
        out.append(Warehouse(name, port, upstream, scaler, float(w.get("idle_minutes", 10)),
                             float(w.get("start_timeout_seconds", 300)), str(tls.get("cert") or ""),
                             str(tls.get("key") or ""), str(w.get("upstream_ca") or "") or True,
                             float(w.get("warmup_seconds", 10)), workers=workers,
                             control_key=os.environ.get("CAIRN_WAREHOUSES_CONTROL_KEY", "")))
    if not out:
        raise WarehouseError("no warehouses")
    return out


@dataclass
class Adaptive:
    """One port in front of several warehouses (smallest first), choosing one per query."""
    name: str
    port: int
    tiers: list
    large_when: dict
    overflow_at: int = 8
    hint: Any = None                                   # async (sql) -> profile dict or None
    tls_cert: str = ""
    tls_key: str = ""
    routes: dict = field(default_factory=dict)         # query id -> (tier index, last seen)
    routed: dict = field(default_factory=dict)         # "<tier>:<reason>" -> count

    def big(self, profile: Optional[dict]) -> bool:
        if not profile or not profile.get("runs"):
            return False
        return any(float(profile.get(k) or 0) >= float(v) for k, v in self.large_when.items())

    @staticmethod
    def busy(w: Warehouse) -> int:
        running, queued = w.load()
        return running + queued

    async def choose(self, sql: str) -> tuple[int, str]:
        """(tier index, why) for a new query."""
        profile = None
        if self.hint is not None:
            try:
                profile = await self.hint(sql)
            except Exception as e:  # noqa: BLE001 - routing without history: by load alone
                logger.info("adaptive %s: no routing hint (%s)", self.name, e)
        last = len(self.tiers) - 1
        if self.big(profile):
            return last, "history"
        if last and self.busy(self.tiers[0]) >= self.overflow_at and self.busy(self.tiers[last]) < self.overflow_at:
            return last, "overflow"
        return 0, "default" if not profile or not profile.get("runs") else "history"

    def remember(self, qid: str, tier: int) -> None:
        now = time.monotonic()
        if len(self.routes) > 10_000:
            for q in [q for q, (_, t) in self.routes.items() if now - t > 3600]:
                del self.routes[q]
        self.routes[qid] = (tier, now)

    def tier_of(self, path: str) -> int:
        m = STATEMENT.match(path) or QUERY_PATH.match(path)
        if m:
            qid = m.group(m.lastindex)
            if qid in self.routes:
                tier, _ = self.routes[qid]
                self.routes[qid] = (tier, time.monotonic())
                return tier
        return 0


def http_hint(url: str, key: str, timeout: float = 1.0):
    """The routing hint from Cairn (``POST <api>/v1/warehouses/hint``), waited for at most ``timeout`` seconds."""
    import httpx

    async def hint(sql: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json={"sql": sql}, headers={"X-Cairn-Trino-Key": key})
        r.raise_for_status()
        return r.json()
    return hint


def load_adaptive(raw: dict, warehouses: list[Warehouse]) -> list[Adaptive]:
    by_name = {w.name: w for w in warehouses}
    ports = {w.port for w in warehouses}
    out = []
    url, key = os.environ.get("CAIRN_WAREHOUSES_HINT_URL", ""), os.environ.get("CAIRN_TRINO_ACCESS_KEY", "")
    for a in raw.get("adaptive") or []:
        name, port = str(a.get("name") or ""), a.get("port")
        if not NAME.match(name) or name in by_name or any(x.name == name for x in out):
            raise WarehouseError(f"adaptive warehouse name {name!r}: lowercase letters, digits and '-', not a warehouse's")
        if not isinstance(port, int) or not 1 <= port <= 65535 or port in ports:
            raise WarehouseError(f"adaptive warehouse {name}: a port of its own")
        tiers = a.get("tiers") or []
        if len(tiers) < 2 or len(set(tiers)) != len(tiers) or any(t not in by_name for t in tiers):
            raise WarehouseError(f"adaptive warehouse {name}: tiers name two or more warehouses, smallest first")
        large = a.get("large_when") or {}
        if not large or set(large) - {"cpu_ms", "input_bytes", "peak_memory_bytes", "wall_ms"} or \
                not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in large.values()):
            raise WarehouseError(f"adaptive warehouse {name}: large_when gives cpu_ms, input_bytes, peak_memory_bytes "
                                 "or wall_ms, each above 0")
        overflow = int(a.get("overflow_at", 8))
        if overflow < 1:
            raise WarehouseError(f"adaptive warehouse {name}: overflow_at is 1 or more")
        tls = a.get("tls") or {}
        ports.add(port)
        out.append(Adaptive(name, port, [by_name[t] for t in tiers], dict(large), overflow,
                            http_hint(url, key) if url and key else None, str(tls.get("cert") or ""),
                            str(tls.get("key") or "")))
    return out


def forwarder(w: Warehouse, client):
    """Forwards a request to ``w`` (starting it if stopped), counting it as in flight until its answer is sent."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse

    import httpx

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
        w.saw(request.url.path)
        try:
            await w.wake()
        except WarehouseError as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        host = request.headers.get("host", "")
        headers["X-Forwarded-Host"] = host                        # Trino's nextUri points back here
        headers["X-Forwarded-Proto"] = "https" if w.tls_cert or request.url.scheme == "https" else "http"
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
    return forward


def _client(ca):
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=600, pool=30), verify=ca)


def app_for(w: Warehouse):
    """The proxy in front of one warehouse (an ASGI app)."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    forward = forwarder(w, _client(w.upstream_ca))

    async def health(_request: Request):
        try:
            running = await w.scaler.running()
        except Exception:  # noqa: BLE001 - shown as unknown
            running = None
        q_running, q_queued = w.load()
        out = {"warehouse": w.name, "ready": await w.ready(), "running": running, "starts": w.starts,
               "stops": w.stops, "inflight": w.inflight, "idle_minutes": w.idle_minutes,
               "idle_seconds": round(time.monotonic() - w.last_seen), "queries_running": q_running,
               "queries_queued": q_queued}
        if w.workers is not None:
            wk = w.workers
            out["workers"] = {"wanted": wk.wanted, "ready": wk.ready, "min": wk.min, "max": wk.max,
                              "scale_ups": wk.scale_ups, "scale_downs": wk.scale_downs}
        return JSONResponse(out)

    async def start(request: Request):
        given = request.headers.get("x-cairn-warehouse-key", "")
        if not w.control_key or not hmac.compare_digest(given.encode(), w.control_key.encode()):
            return JSONResponse({"error": "not allowed"}, status_code=403)
        w.last_seen = time.monotonic()                 # counts as use: not stopped before its idle time

        async def go():
            try:
                await w.wake()
            except Exception:  # noqa: BLE001 - shown by the status
                logger.exception("warehouse %s: start failed", w.name)
        asyncio.get_running_loop().create_task(go())
        return JSONResponse({"warehouse": w.name, "starting": True}, status_code=202)

    return Starlette(routes=[Route("/cairn/warehouse", health), Route("/cairn/warehouse/start", start, methods=["POST"]),
                             Route("/{path:path}", forward, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])])


def adaptive_app(a: Adaptive):
    """The proxy of an adaptive warehouse: each new query to the tier chosen for it, its follow-ups to the same."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    forwards = [forwarder(w, _client(w.upstream_ca)) for w in a.tiers]

    async def health(_request: Request):
        tiers = []
        for w in a.tiers:
            r, q = w.load()
            tiers.append({"warehouse": w.name, "queries_running": r, "queries_queued": q, "ready": await w.ready()})
        return JSONResponse({"adaptive": a.name, "tiers": tiers, "routed": a.routed, "large_when": a.large_when,
                             "overflow_at": a.overflow_at})

    async def statement(request: Request):
        body = await request.body()
        tier, why = await a.choose(body.decode("utf-8", "replace"))
        key = f"{a.tiers[tier].name}:{why}"
        a.routed[key] = a.routed.get(key, 0) + 1
        resp = await forwards[tier](request)
        if not hasattr(resp, "body_iterator"):
            return resp
        chunks = [c async for c in resp.body_iterator]          # a new query's first answer is small
        content = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        try:
            raw = content
            if (resp.headers.get("content-encoding") or "").lower() == "gzip":
                import gzip

                raw = gzip.decompress(content)          # passed on as it came; read here for the query id only
            qid = json.loads(raw).get("id")
        except (ValueError, AttributeError, OSError):
            qid = None
        if qid:
            a.remember(str(qid), tier)
            # The tier's own count of the query starts now, not at the client's first poll.
            a.tiers[tier].queries[str(qid)] = ("queued", time.monotonic())
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP}
        return Response(content, status_code=resp.status_code, headers=headers)

    async def follow(request: Request):
        return await forwards[a.tier_of(request.url.path)](request)

    return Starlette(routes=[Route("/cairn/warehouse", health), Route("/v1/statement", statement, methods=["POST"]),
                             Route("/{path:path}", follow, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])])


async def serve(warehouses: list[Warehouse], host: str = "0.0.0.0", reap_every: float = 30,
                adaptive: Optional[list[Adaptive]] = None) -> None:
    import uvicorn

    from cairn.cli import keep_alive

    servers = [uvicorn.Server(uvicorn.Config(app_for(w), host=host, port=w.port, log_level="warning", proxy_headers=False,
                                             timeout_keep_alive=keep_alive(),
                                             ssl_certfile=w.tls_cert or None, ssl_keyfile=w.tls_key or None))
               for w in warehouses]
    servers += [uvicorn.Server(uvicorn.Config(adaptive_app(a), host=host, port=a.port, log_level="warning",
                                              proxy_headers=False, timeout_keep_alive=keep_alive(),
                                              ssl_certfile=a.tls_cert or None, ssl_keyfile=a.tls_key or None))
                for a in adaptive or []]

    report_url = os.environ.get("CAIRN_WAREHOUSES_REPORT_URL", "")
    report_key = os.environ.get("CAIRN_TRINO_ACCESS_KEY", "")
    report_every = float(os.environ.get("CAIRN_WAREHOUSES_REPORT_SECONDS", "300"))
    last_report = time.monotonic()

    async def reaper():
        nonlocal last_report
        while True:
            await asyncio.sleep(reap_every)
            for w in warehouses:
                try:
                    await w.count_usage()
                    await w.autoscale()
                    await w.reap()
                except Exception:  # noqa: BLE001 - try again next round
                    logger.exception("warehouse %s: scaling or stopping failed", w.name)
            if report_url and report_key and time.monotonic() - last_report >= report_every:
                last_report = time.monotonic()
                for w in warehouses:
                    await w.report_usage(report_url, report_key)
    await asyncio.gather(*(s.serve() for s in servers), reaper())
