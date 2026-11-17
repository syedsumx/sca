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


def test_a_busy_warehouse_is_not_stopped():
    async def flow():
        s = Scaler()
        s.gate.set()
        w = warehouse(s)
        w.inflight = 1
        assert await w.reap() is False and s.calls == []
    asyncio.run(flow())
