"""The warehouse proxy's start and stop, with a stand-in for the scaler and Trino (no containers): what happens to a
request that arrives while the warehouse is being stopped. The real scalers are in tests/test_warehouses_e2e.py
(Docker) and tests/test_warehouses_k8s.py (Kubernetes)."""

import asyncio

from cairn.dataaccess.warehouses import Warehouse


class Scaler:
    """A warehouse that answers while it runs; stopping takes until ``gate`` opens."""

    def __init__(self):
        self.up, self.stopping, self.gate, self.calls = True, asyncio.Event(), asyncio.Event(), []

    async def start(self):
        self.calls.append("start")
        self.up = True

    async def stop(self):
        self.calls.append("stop")
        self.stopping.set()
        await self.gate.wait()
        self.up = False

    async def running(self):
        return self.up


def warehouse(scaler):
    w = Warehouse("sales", 1, "http://127.0.0.1:1", scaler, idle_minutes=1, start_timeout=5, warmup=0)

    async def ready():
        return scaler.up
    w.ready = ready
    w.last_seen = -10**9                                  # idle for long
    return w


def test_a_query_that_arrives_while_the_warehouse_stops_waits_and_starts_it_again():
    """Review finding: wake() returned at once whenever Trino still answered, without the lock the idle stop holds,
    so a query arriving while the proxy was stopping the warehouse went to a Trino that was going away (and failed)
    instead of waiting and starting it again."""
    async def flow():
        s = Scaler()
        w = warehouse(s)
        reap = asyncio.create_task(w.reap())
        await s.stopping.wait()                           # the idle stop has begun
        w.inflight += 1                                   # a query comes in (as the forwarder counts it) ...
        wake = asyncio.create_task(w.wake())
        await asyncio.sleep(0.05)
        assert not wake.done()                            # ... and waits for the stop, not sent to the dying Trino
        s.gate.set()
        assert await reap is True
        await asyncio.wait_for(wake, 5)
        assert s.up and s.calls == ["stop", "start"] and w.starts == 1 and w.stops == 1
    asyncio.run(flow())


def test_a_query_after_the_stop_starts_the_warehouse_even_if_it_still_answers():
    """After a stop the old Trino can answer for a moment (a Kubernetes pod shutting down): the next query starts
    the warehouse instead of trusting that answer."""
    async def flow():
        s = Scaler()
        s.gate.set()
        w = warehouse(s)
        assert await w.reap() is True

        async def still_answers():
            return True
        w.ready = still_answers                           # the stopped pod has not gone yet
        await w.wake()
        assert s.calls == ["stop", "start"] and w.starts == 1
        await w.wake()                                    # started: no second start
        assert w.starts == 1
    asyncio.run(flow())


def test_only_someone_cairn_admits_starts_a_warehouse_or_keeps_it_up():
    """Review finding: the proxy started a stopped warehouse, and counted as use, any request on its port: no token,
    a bad one, or a token of someone who may not use the warehouse (Trino refused their query only after the start).
    Anyone who could reach the port could keep a team's warehouse running, billed to the team. Now Cairn admits the
    request's token first (as the warehouse's Trino would decide its ExecuteQuery); others are refused untouched."""
    from starlette.testclient import TestClient

    from cairn.dataaccess.warehouses import app_for

    s = Scaler()
    s.up = False
    asked = []

    async def admit(token):
        asked.append(token)
        if token == "boom.boom.boom":
            raise RuntimeError("Cairn is away")
        return token == "ana.sales.ok", 2**40
    w = warehouse(s)
    w.admit = admit
    seen = w.last_seen
    with TestClient(app_for(w)) as c:
        assert c.get("/v1/info").status_code == 401                                          # no token
        assert c.get("/v1/info", headers={"Authorization": "Basic YTpi"}).status_code == 403  # not a Cairn token
        assert c.post("/v1/statement", content="SELECT 1",
                      headers={"Authorization": "Bearer eve.other.team"}).status_code == 403
        assert c.post("/v1/statement", content="SELECT 1",
                      headers={"Authorization": "Bearer eve.other.team"}).status_code == 403  # kept: asked once
        assert c.get("/v1/info", headers={"Authorization": "Bearer boom.boom.boom"}).status_code == 503   # fail closed
        assert s.calls == [] and w.last_seen == seen and w.inflight == 0                     # not started, not use
        assert asked == ["eve.other.team", "boom.boom.boom"]
        # Ana may: the warehouse starts for her (the request then goes to the stand-in's address, which is closed).
        r = c.get("/v1/info", headers={"Authorization": "Bearer ana.sales.ok"})
        assert s.calls == ["start"] and w.starts == 1 and r.status_code == 502
        assert w.last_seen > seen and w.inflight == 0


def test_the_client_cannot_say_where_it_connects_from():
    """Review finding: the proxy passed the client's own Forwarded and X-Forwarded-For/-Port/-Prefix headers to the
    warehouse's Trino, which processes forwarded headers (it trusts the proxy), so a client could put any address in
    Trino's record of its queries (query history, the audit) or rewrite the addresses Trino hands back. The proxy now
    drops them and says itself who connected."""
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from cairn.dataaccess.warehouses import forwarder

    seen = {}

    def upstream(request: httpx.Request):
        seen.update(request.headers)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'))
    s = Scaler()
    s.gate.set()
    w = warehouse(s)
    fwd = forwarder(w, httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    app = Starlette(routes=[Route("/{path:path}", fwd, methods=["GET", "POST"])])
    with TestClient(app, client=("10.1.2.3", 5000)) as c:
        r = c.post("/v1/statement", content="SELECT 1", headers={
            "Forwarded": "for=6.6.6.6;host=evil.example;proto=https", "X-Forwarded-For": "6.6.6.6",
            "X-Forwarded-Port": "1", "X-Forwarded-Prefix": "/evil", "X-Real-IP": "6.6.6.6", "X-Trino-User": "ana"})
    assert r.status_code == 200
    assert "forwarded" not in seen and "x-forwarded-port" not in seen and "x-forwarded-prefix" not in seen
    assert "x-real-ip" not in seen
    assert seen["x-forwarded-for"] == "10.1.2.3" and seen["x-forwarded-proto"] == "http"
    assert seen["x-trino-user"] == "ana"                  # Trino's own headers go through


def test_a_busy_warehouse_is_not_stopped():
    async def flow():
        s = Scaler()
        s.gate.set()
        w = warehouse(s)
        w.inflight = 1
        assert await w.reap() is False and s.calls == []
    asyncio.run(flow())
