"""Scale load for a running Cairn (5.3): the policy decision point at a fixed request rate, and the remote agent
gateway with many agents moving a large volume of metadata.

    python tests/load/scale.py pdp --url http://127.0.0.1:8810 [--url ...] --token-file svc.token \\
        --rate 2000 --duration 60 --mode cached --json pdp.json
    python tests/load/scale.py gateway --url http://127.0.0.1:8820 --db postgresql+psycopg://... \\
        --agents 100 --megabytes 1024 --json gateway.json

The PDP driver is open-loop: requests are scheduled at the given rate whatever the answers do, and each latency is
measured from the request's scheduled time, so a server that falls behind shows it (no coordinated omission). It
runs in several processes, each with its own keep-alive connections and a minimal HTTP/1.1 client, so the driver is
not the bottleneck. ``cached`` asks the same few hundred questions over and over (the decision cache answers);
``uncached`` never asks the same question twice. Several ``--url`` spread requests over PDP replicas, as a Service
does. The gateway driver enrols real agents (the agent's own client), gives each a task through the gateway's
database, and has each upload its share of sealed metadata batches, then complete the run."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


# ---- the PDP ----------------------------------------------------------------------------------------------------
def _question(mode: str, i: int, salt: str) -> bytes:
    if mode == "cached":                         # a working set the cache holds: 50 people x 8 datasets
        actor, ds = f"urn:li:corpuser:user{i % 50}", f"db.sales.t{i % 8}"
    else:                                        # every question new
        actor, ds = f"urn:li:corpuser:user{i % 5000}", f"db.{salt}.t{i}"
    priv = ("VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "EDIT_ENTITY_DOCS")[i % 3]
    return json.dumps({
        "actor": actor, "privilege": priv,
        "resource": {"type": "dataset", "urn": f"urn:li:dataset:(urn:li:dataPlatform:postgres,{ds},PROD)"},
        "subject_attributes": {"employment_type": "employee", "groups": ["urn:li:corpGroup:analysts"]},
        "resource_attributes": {"classification": ["PII"] if i % 4 == 0 else [], "domain": "urn:li:domain:sales"},
    }).encode()


async def _connection(host: str, port: int, token: str, mode: str, salt: str, clock: dict, out: list) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        while True:
            i = clock["next"]
            if i >= clock["total"] or time.perf_counter() > clock["start"] + clock["deadline"]:
                return                       # done, or so far behind that the rest would never be on time
            clock["next"] = i + 1
            due = clock["start"] + i / clock["rate"]
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            body = _question(mode, i * clock["procs"] + clock["index"], salt)
            writer.write(b"POST /governance/v1/authorize HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
                         b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                         % (host.encode(), token.encode(), len(body), body))
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            payload = await reader.readexactly(length)
            ok = status == 200 and b'"decision"' in payload
            out.append(((time.perf_counter() - due) * 1000.0, ok))
    finally:
        writer.close()


def _pdp_process(index: int, procs: int, urls: list, token: str, rate: float, duration: float, conns: int,
                 mode: str, salt: str, start_at: float, q) -> None:
    async def main():
        clock = {"next": 0, "total": int(rate * duration), "rate": rate, "start": 0.0, "procs": procs, "index": index,
                 "deadline": duration * 1.5 + 5}
        out: list = []
        targets = [urlsplit(u) for u in urls]
        await asyncio.sleep(max(0.0, start_at - time.time()))
        clock["start"] = time.perf_counter()
        await asyncio.gather(*(_connection(t.hostname, t.port, token, mode, salt, clock, out)
                               for n in range(conns) for t in [targets[(index * conns + n) % len(targets)]]),
                             return_exceptions=True)
        return out, time.perf_counter() - clock["start"]
    out, took = asyncio.run(main())
    q.put((out, took))


def pct(values: list, q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))], 2)


def pdp_load(urls: list, token: str, rate: float, duration: float, mode: str, procs: int = 2,
             conns: int = 32) -> dict:
    """``rate`` requests a second for ``duration`` seconds, from ``procs`` processes with ``conns`` connections each."""
    ctx = mp.get_context("fork")   # the driver is loaded from a path, not a module spawn could import
    q = ctx.Queue()
    salt = uuid.uuid4().hex[:8]
    start_at = time.time() + 2.0
    ps = [ctx.Process(target=_pdp_process, args=(i, procs, urls, token, rate / procs, duration, conns, mode, salt,
                                                 start_at, q)) for i in range(procs)]
    for p in ps:
        p.start()
    got = [q.get(timeout=duration * 4 + 120) for _ in ps]
    for p in ps:
        p.join(30)
    samples = [s for out, _ in got for s in out]
    took = max(t for _, t in got)
    lat = [ms for ms, _ in samples]
    errors = sum(1 for _, ok in samples if not ok)
    planned = int(rate / procs * duration) * procs
    return {"mode": mode, "offered_rps": rate, "duration_s": duration, "replicas": len(urls), "requests": len(samples),
            "planned": planned, "achieved_rps": round(len(samples) / took, 1), "errors": errors,
            "p50_ms": pct(lat, 0.50), "p95_ms": pct(lat, 0.95), "p99_ms": pct(lat, 0.99), "max_ms": pct(lat, 1.0)}


# ---- the gateway ------------------------------------------------------------------------------------------------
def _mcp(agent: int, n: int, fields: int) -> dict:
    urn = f"urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.agent{agent}.table{n},PROD)"
    return {"entityType": "dataset", "entityUrn": urn, "changeType": "UPSERT", "aspectName": "schemaMetadata",
            "aspect": {"json": {"schemaName": f"table{n}", "platform": "urn:li:dataPlatform:postgres", "version": 0,
                                "hash": "", "platformSchema": {"com.linkedin.schema.MySqlDDL": {"tableSchema": ""}},
                                "fields": [{"fieldPath": f"column_{c}", "nativeDataType": "varchar(255)",
                                            "type": {"type": {"com.linkedin.schema.StringType": {}}},
                                            "description": f"Column {c} of table {n}, loaded by agent {agent}; "
                                                           + "x" * 60}
                                           for c in range(fields)]}}}


def gateway_volume(url: str, db_url: str, agents: int, megabytes: float, batch_kb: int = 1024,
                   threads: int = 32) -> dict:
    """``agents`` real agents each upload ``megabytes / agents`` of metadata (MCP JSON, before sealing) in batches
    of about ``batch_kb``; the gateway decrypts, checks each against its pool's policy and accepts it."""
    from cairn.agent.client import AgentState, GatewayClient
    from cairn.agent.gateway import EnqueueRequest, GatewayKeys, GatewayService, PoolSpec
    from cairn.common.db import get_engine, init_db

    engine = get_engine(db_url)
    init_db(engine)
    tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"cairn-scale-{uuid.uuid4().hex[:8]}")
    svc = GatewayService(engine, GatewayKeys(os.path.join(tmp, "unused")), None)
    # Pools run at most 64 tasks at once: the agents are spread over pools of up to 50, as a fleet would be.
    base = f"scale-{uuid.uuid4().hex[:6]}"
    pools = [f"{base}-{n}" for n in range((agents + 49) // 50)]
    for name in pools:
        svc.create_pool(PoolSpec(name=name, allowed_platforms=["postgres"], max_concurrency=50), "urn:li:corpuser:load")
    per_agent = int(megabytes * 1024 * 1024 / agents)
    one = len(json.dumps(_mcp(0, 0, 20)))
    per_batch = max(1, batch_kb * 1024 // one)

    def enrol(i: int) -> GatewayClient:
        c = GatewayClient(url, AgentState(os.path.join(tmp, f"agent{i}")), timeout=300)
        c.enroll(svc.create_enrollment_token(pools[i // 50], "urn:li:corpuser:load")["token"], hostname=f"agent{i}")
        return c

    t0 = time.perf_counter()
    with ThreadPoolExecutor(threads) as ex:
        clients = list(ex.map(enrol, range(agents)))
    enrolled_s = time.perf_counter() - t0
    for i in range(agents):
        svc.enqueue(EnqueueRequest(pool=pools[i // 50], recipe={"source": {"type": "postgres", "config": {}}}), "urn:li:corpuser:load")
    latencies: list = []
    totals = {"bytes": 0, "mcps": 0, "accepted": 0, "rejected": 0, "batches": 0, "errors": 0}

    lock = threading.Lock()

    def add(**kw) -> None:
        with lock:
            for k, v in kw.items():
                totals[k] += v

    def run(i: int) -> None:
        c = clients[i]
        task = c.poll()
        if not task:
            add(errors=1)
            return
        run_id, sent, seq, n, last = task["run_id"], 0, 0, 0, -1
        while sent < per_agent:
            mcps = [_mcp(i, n + k, 20) for k in range(per_batch)]
            n += per_batch
            raw = len(json.dumps(mcps))
            started = time.perf_counter()
            try:
                out = c.upload_batch(run_id, seq, mcps)
            except Exception:  # noqa: BLE001 - counted; the run goes on
                add(errors=1)
                seq += 1
                continue
            with lock:
                latencies.append((time.perf_counter() - started) * 1000.0)
            add(accepted=out["accepted"], rejected=out["rejected"], batches=1, mcps=len(mcps), bytes=raw)
            sent += raw
            last = seq
            seq += 1
        c.complete(run_id, "SUCCESS", {"mcps": n}, last)

    t1 = time.perf_counter()
    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(run, range(agents)))
    took = time.perf_counter() - t1
    done = svc.list_runs(1000)
    return {"agents": agents, "enrolled_s": round(enrolled_s, 1), "megabytes": round(totals["bytes"] / 1048576, 1),
            "seconds": round(took, 1), "mib_per_second": round(totals["bytes"] / 1048576 / took, 1),
            "batches": totals["batches"], "mcps": totals["mcps"], "accepted": totals["accepted"],
            "rejected": totals["rejected"], "errors": totals["errors"],
            "runs_succeeded": sum(1 for r in done if r.get("status") == "SUCCESS" and r.get("pool") in pools),
            "batch_p50_ms": pct(latencies, 0.5), "batch_p99_ms": pct(latencies, 0.99)}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="what", required=True)
    a = sub.add_parser("pdp")
    a.add_argument("--url", action="append", required=True)
    a.add_argument("--token-file", required=True, help="a file holding the service token (never on a command line)")
    a.add_argument("--rate", type=float, default=2000)
    a.add_argument("--duration", type=float, default=60)
    a.add_argument("--mode", choices=["cached", "uncached"], default="cached")
    a.add_argument("--procs", type=int, default=2)
    a.add_argument("--conns", type=int, default=32)
    a.add_argument("--json")
    g = sub.add_parser("gateway")
    g.add_argument("--url", required=True)
    g.add_argument("--db", required=True, help="the gateway's database URL (to enqueue the runs)")
    g.add_argument("--agents", type=int, default=100)
    g.add_argument("--megabytes", type=float, default=1024)
    g.add_argument("--json")
    x = p.parse_args(argv)
    if x.what == "pdp":
        token = open(x.token_file).read().strip()
        out = pdp_load(x.url, token, x.rate, x.duration, x.mode, x.procs, x.conns)
    else:
        out = gateway_volume(x.url, x.db, x.agents, x.megabytes)
    print(json.dumps(out, indent=2))
    if x.json:
        with open(x.json, "w") as f:
            json.dump(out, f, indent=2)
    sys.exit(1 if out.get("errors") else 0)


if __name__ == "__main__":
    random.seed(7)
    main()
