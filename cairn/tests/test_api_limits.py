"""API quotas and the IP allowlist, over HTTP against two Cairn replicas sharing one database: per-person and
tenant-wide limits answered with 429 and Retry-After and counted across replicas; addresses outside the allowlist
refused; X-Forwarded-For believed only from a trusted proxy; changes made at run time by an operator, who cannot
lock themselves out."""

import socket
import tempfile
import threading
import time

import httpx
import pytest

from cairn.governance.abac import Policy, PolicyBundle

OPS, ANA, BOB = (f"urn:li:corpuser:{n}" for n in ("ops", "ana", "bob"))


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def replicas():
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.services import build_services

    d = tempfile.mkdtemp(prefix="limits-")
    urls, servers = [], []
    for _ in range(2):
        s = build_services(Settings(database_url=f"sqlite:///{d}/cairn.db", service_token="svc-secret",
                                    dev_trust_actor_header=True, gateway={"key_dir": f"{d}/keys"}),
                           graph=InMemoryGraph(), with_ai=False, with_gateway=False)
        s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="ops", effect="allow", actions=["*"],
                                                       condition={"attr": "subject.urn", "op": "eq", "value": OPS})]))
        p = _port()
        server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, timeout_keep_alive=75, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        urls.append(f"http://127.0.0.1:{p}")
        servers.append((server, s))
    for u in urls:
        for _ in range(100):
            try:
                if httpx.get(f"{u}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    yield urls, [s for _srv, s in servers]
    for srv, _s in servers:
        srv.should_exit = True


def h(who: str, **extra) -> dict:
    return {"X-Cairn-Actor": who, **extra}


def put(url, body, who=OPS, **headers):
    return httpx.put(f"{url}/v1/admin/limits", json=body, headers=h(who, **headers), timeout=10)


def fresh_minute():
    """Start at the beginning of a minute, so a test's requests share one window."""
    if time.time() % 60 > 45:
        time.sleep(61 - time.time() % 60)


def test_person_quota_is_shared_by_replicas_and_says_when_to_retry(replicas):
    (a, b), _ = replicas
    r = put(a, {"user_per_minute": 4})
    assert r.status_code == 200, r.text
    assert put(a, {"user_per_minute": 4}, who="urn:li:corpuser:eve").status_code == 403   # operators only
    time.sleep(5.5)                                                              # every replica sees the change
    fresh_minute()
    codes = [httpx.get(f"{u}/me", headers=h(ANA), timeout=10).status_code for u in (a, b, a, b, a)]
    assert codes == [200, 200, 200, 200, 429], codes
    r = httpx.get(f"{b}/me", headers=h(ANA), timeout=10)
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 61
    assert r.headers["x-ratelimit-limit"] == "4" and r.headers["x-ratelimit-scope"] == "user"
    assert httpx.get(f"{a}/me", headers=h(BOB), timeout=10).status_code == 200    # someone else's quota
    assert httpx.get(f"{a}/me", headers={"Authorization": "Bearer svc-secret"}, timeout=10).status_code == 200
    # A named person gets their own limit.
    carol = "urn:li:corpuser:carol"
    assert put(a, {"user_per_minute": 4, "users": {carol: 1}}).status_code == 200
    time.sleep(5.5)
    fresh_minute()
    assert httpx.get(f"{b}/me", headers=h(carol), timeout=10).status_code == 200
    assert httpx.get(f"{a}/me", headers=h(carol), timeout=10).status_code == 429
    # After the minute, the quota is back.
    time.sleep(61 - time.time() % 60)
    assert httpx.get(f"{a}/me", headers=h(ANA), timeout=10).status_code == 200
    assert put(a, {}).status_code == 200


def test_tenant_quota_counts_everyone(replicas):
    (a, b), _ = replicas
    assert put(a, {"tenant_per_minute": 6}).status_code == 200
    time.sleep(5.5)
    fresh_minute()
    codes = [httpx.get(f"{u}/me", headers=h(who), timeout=10).status_code
             for u, who in ((a, ANA), (b, BOB), (a, OPS), (b, ANA), (a, BOB), (b, OPS), (a, ANA))]
    assert codes[-1] == 429 and codes.count(200) <= 6, codes
    r = httpx.get(f"{b}/me", headers=h(BOB), timeout=10)
    assert r.status_code == 429 and r.headers["x-ratelimit-scope"] == "tenant" and r.headers["retry-after"]
    assert httpx.get(f"{a}/health", timeout=10).status_code == 200                  # probes are not counted
    time.sleep(61 - time.time() % 60)
    assert put(a, {}).status_code == 200


def test_ip_allowlist_and_trusted_proxies(replicas):
    (a, b), (sa, _sb) = replicas
    # An allowlist that leaves out the operator's own address is refused.
    r = put(a, {"ip_allowlist": ["10.0.0.0/8"]})
    assert r.status_code == 409 and "your own address" in r.text
    assert put(a, {"ip_allowlist": ["not-a-network"]}).status_code == 422
    # Behind a trusted proxy (here: 127.0.0.1), the client is the last hop the proxy added.
    assert put(a, {"ip_allowlist": ["10.0.0.0/8"], "trusted_proxies": ["127.0.0.1/32"]},
               **{"X-Forwarded-For": "10.1.2.3"}).status_code == 200
    time.sleep(5.5)
    for u in (a, b):
        assert httpx.get(f"{u}/me", headers=h(ANA), timeout=10).status_code == 403           # from 127.0.0.1
        assert httpx.get(f"{u}/me", headers=h(ANA, **{"X-Forwarded-For": "10.9.9.9"}), timeout=10).status_code == 200
        # Spoofing an allowed address in front of one's own: the proxy appends the real one, which is checked.
        assert httpx.get(f"{u}/me", headers=h(ANA, **{"X-Forwarded-For": "10.9.9.9, 198.51.100.7"}),
                         timeout=10).status_code == 403
        assert httpx.get(f"{u}/health", timeout=10).status_code == 200                  # probes stay open
    # Without trusting the proxy, X-Forwarded-For is not believed at all (sent through the proxy, from 10.1.2.3).
    assert put(a, {"ip_allowlist": ["10.0.0.0/8", "127.0.0.1/32"]}, **{"X-Forwarded-For": "10.1.2.3"}).status_code == 200
    time.sleep(5.5)
    with sa.engine.begin() as conn:
        from cairn.common.limits import api_limits

        doc = conn.execute(api_limits.select()).first().doc
    assert doc["trusted_proxies"] == []
    assert httpx.get(f"{b}/me", headers=h(ANA), timeout=10).status_code == 200
    assert put(a, {"ip_allowlist": []}).status_code == 200
    entries = [e for e in sa.audit.entries(limit=500) if e.get("action") == "limits.changed"]
    assert entries and entries[0]["actor"] == OPS


def test_settings_survive_a_database_blip_and_a_fresh_replica_refuses(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    from cairn.common.db import init_db
    from cairn.common.limits import ApiLimits

    e = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(e)
    lim = ApiLimits(e)
    lim.put({"ip_allowlist": ["10.0.0.0/8"]}, OPS, "10.0.0.1")
    assert not lim.ip_allowed("127.0.0.1") and lim.ip_allowed("10.2.3.4")
    lim.engine = create_engine("sqlite:////nonexistent/dir/x.db")              # the database goes away
    lim._cached = (time.monotonic() - 10, *lim._cached[1:])
    assert not lim.ip_allowed("127.0.0.1") and lim.ip_allowed("10.2.3.4")      # the last settings still apply
    with pytest.raises(OperationalError):
        ApiLimits(lim.engine).ip_allowed("10.2.3.4")                         # nothing read yet: refused upstream


def test_websockets_are_held_to_the_allowlist_and_the_tenant_quota(tmp_path):
    """A data app's live connection is a WebSocket: refused from outside the allowlist, counted in the tenant's quota."""
    import asyncio

    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.common.limits import ApiLimits, LimitsMiddleware

    e = create_engine(f"sqlite:///{tmp_path}/w.db")
    init_db(e)
    limits = ApiLimits(e)
    limits.put({"ip_allowlist": ["10.0.0.0/8"], "tenant_per_minute": 2}, OPS, "10.0.0.1")
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["client"][0])
        await send({"type": "websocket.accept"})

    def connect(ip: str) -> list:
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)
        scope = {"type": "websocket", "path": "/apps/demo/stream", "headers": [], "client": (ip, 5000),
                 "query_string": b"", "root_path": "", "scheme": "ws", "server": ("cairn", 80), "subprotocols": []}
        asyncio.run(LimitsMiddleware(app, limits)(scope, receive, send))
        return sent

    assert connect("203.0.113.9")[0]["type"] == "websocket.close" and reached == []
    assert connect("10.1.2.3")[0]["type"] == "websocket.accept"
    assert connect("10.1.2.3")[0]["type"] == "websocket.accept"
    assert connect("10.1.2.3")[0]["type"] == "websocket.close"       # the third this minute: over the tenant's 2
    assert reached == ["10.1.2.3", "10.1.2.3"]


def test_the_mcp_server_is_held_to_the_allowlist_and_the_persons_quota(tmp_path):
    """The MCP server runs on its own port and ingress: the API's middleware is not in front of it."""
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import create_engine

    from cairn.ai import mcp_server
    from cairn.common.db import init_db
    from cairn.common.limits import ApiLimits
    from cairn.common.security import Principal

    e = create_engine(f"sqlite:///{tmp_path}/m.db")
    init_db(e)
    limits = ApiLimits(e)
    limits.put({"ip_allowlist": ["10.0.0.0/8"], "users": {ANA: 1}}, OPS, "10.0.0.1")
    services = SimpleNamespace(limits=limits, agents=None,
                               identity=SimpleNamespace(resolve=lambda headers, cookies: Principal(ANA, "token")))
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["client"][0])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    serve = mcp_server.http_app(inner, services)

    def call(ip: str) -> int:
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"authorization", b"Bearer t")],
                 "client": (ip, 5000), "query_string": b"", "root_path": "", "scheme": "http", "server": ("cairn", 80)}
        asyncio.run(serve(scope, receive, send))
        return sent[0]["status"]

    assert call("203.0.113.9") == 403 and reached == []         # outside the allowlist
    assert call("10.1.2.3") == 200
    assert call("10.1.2.3") == 429                              # ana's own limit is 1 a minute
    assert reached == ["10.1.2.3"]


def test_trinos_access_checks_are_not_refused_by_the_tenant_quota(tmp_path):
    """Trino asks Cairn about every table of every query (/v1/trino/<key>/...). Counted against the tenant's quota,
    any burst of other requests would make Trino refuse every query for everyone; they stay held to the allowlist."""
    import asyncio

    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.common.limits import ApiLimits, LimitsMiddleware

    e = create_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(e)
    limits = ApiLimits(e)
    limits.put({"ip_allowlist": ["10.0.0.0/8"], "tenant_per_minute": 2}, OPS, "10.0.0.1")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    def call(path: str, ip: str = "10.1.2.3") -> int:
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "POST", "path": path, "headers": [], "client": (ip, 5000),
                 "query_string": b"", "root_path": "", "scheme": "http", "server": ("cairn", 80)}
        asyncio.run(LimitsMiddleware(app, limits)(scope, receive, send))
        return sent[0]["status"]

    assert [call("/me"), call("/me"), call("/me")] == [200, 200, 429]           # the tenant is over its quota
    assert call("/v1/trino/k3y/allow") == 200 and call("/v1/trino/k3y/allow") == 200
    assert call("/v1/trino/k3y/allow", ip="203.0.113.9") == 403                  # still held to the allowlist
