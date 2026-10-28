"""Load test for a running Cairn: drives the hot read paths at a fixed concurrency and checks the
results against the published limits (``limits.yaml``, docs: Administration → Limits).

    python tests/load/loadtest.py --api http://localhost:8800 --token "$TOKEN" --users 10 --duration 30

Each scenario runs for ``--duration`` seconds with ``--users`` concurrent clients. Exit code 1 if
any scenario breaks its limit. ``--json out.json`` writes the measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from typing import Awaitable, Callable

import httpx
import yaml

Scenario = Callable[[httpx.AsyncClient, dict], Awaitable[httpx.Response]]


async def pdp_authorize(c: httpx.AsyncClient, ctx: dict) -> httpx.Response:
    urn = random.choice(ctx["urns"])
    return await c.post("/governance/v1/authorize", json={
        "actor": ctx["me"], "privilege": random.choice(["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "EDIT_ENTITY_DOCS"]),
        "resource": {"type": "dataset", "urn": urn}})


async def enrich(c: httpx.AsyncClient, ctx: dict) -> httpx.Response:
    return await c.post("/collab/enrich", json={"urns": random.sample(ctx["urns"], min(20, len(ctx["urns"])))})


async def dataset_health(c: httpx.AsyncClient, ctx: dict) -> httpx.Response:
    return await c.get(f"/quality/datasets/{random.choice(ctx['urns'])}/health")


async def marketplace(c: httpx.AsyncClient, ctx: dict) -> httpx.Response:
    return await c.get("/marketplace/products", params={"q": "*"})


async def inbox(c: httpx.AsyncClient, ctx: dict) -> httpx.Response:
    return await c.get("/notify/inbox", params={"limit": 20})


async def contract_status(c: httpx.AsyncClient, ctx: dict) -> httpx.Response:
    return await c.get("/quality/contracts")


SCENARIOS: dict[str, Scenario] = {"pdp_authorize": pdp_authorize, "enrich": enrich, "dataset_health": dataset_health,
                                  "marketplace": marketplace, "inbox": inbox, "contracts": contract_status}


async def _worker(c: httpx.AsyncClient, fn: Scenario, ctx: dict, stop: float, out: list) -> None:
    while time.monotonic() < stop:
        started = time.perf_counter()
        try:
            r = await fn(c, ctx)
            ok = r.status_code < 500 and r.status_code != 429
        except httpx.HTTPError:
            ok = False
        out.append(((time.perf_counter() - started) * 1000, ok))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))], 1)


async def run(api: str, token: str, users: int, duration: float, only: list[str]) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    limits = httpx.Limits(max_connections=users * 2, max_keepalive_connections=users * 2)
    async with httpx.AsyncClient(base_url=api.rstrip("/"), headers=headers, timeout=30, limits=limits) as c:
        async def setup_get(path: str, **kw):
            r = await c.get(path, **kw)
            if r.status_code != 200:
                raise SystemExit(f"GET {path}: {r.status_code} {r.text[:200]} (the token needs admin rights)")
            return r.json()

        me = (await setup_get("/me"))["urn"]
        urns = [a["urn"] for a in await setup_get("/reports/assets", params={"limit": 200})] or []
        if not urns:
            raise SystemExit("no datasets visible to this token: seed the catalog first")
        ctx = {"me": me, "urns": urns}
        results = {}
        for name in only:
            fn = SCENARIOS[name]
            await fn(c, ctx)                                        # warm caches and connections
            samples: list = []
            stop = time.monotonic() + duration
            await asyncio.gather(*(_worker(c, fn, ctx, stop, samples) for _ in range(users)))
            lat = [ms for ms, _ in samples]
            errors = sum(1 for _, ok in samples if not ok)
            results[name] = {"requests": len(samples), "rps": round(len(samples) / duration, 1),
                             "p50_ms": _pct(lat, 0.5), "p95_ms": _pct(lat, 0.95), "p99_ms": _pct(lat, 0.99),
                             "mean_ms": round(statistics.fmean(lat), 1) if lat else 0.0,
                             "error_rate": round(errors / len(samples), 4) if samples else 1.0}
        return {"users": users, "duration_s": duration, "datasets_sampled": len(urns), "scenarios": results}


def check(results: dict, limits: dict) -> list[str]:
    problems = []
    for name, r in results["scenarios"].items():
        lim = limits.get(name) or {}
        if "p95_ms" in lim and r["p95_ms"] > lim["p95_ms"]:
            problems.append(f"{name}: p95 {r['p95_ms']} ms > {lim['p95_ms']} ms")
        if "min_rps" in lim and r["rps"] < lim["min_rps"]:
            problems.append(f"{name}: {r['rps']} req/s < {lim['min_rps']}")
        if r["error_rate"] > lim.get("max_error_rate", 0.01):
            problems.append(f"{name}: error rate {r['error_rate']:.2%} > {lim.get('max_error_rate', 0.01):.2%}")
    return problems


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--api", default="http://localhost:8800")
    p.add_argument("--token", required=True)
    p.add_argument("--users", type=int, default=10)
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--scenarios", default=",".join(SCENARIOS))
    p.add_argument("--limits", default=__file__.replace("loadtest.py", "limits.yaml"))
    p.add_argument("--json")
    a = p.parse_args(argv)
    results = asyncio.run(run(a.api, a.token, a.users, a.duration, a.scenarios.split(",")))
    limits = (yaml.safe_load(open(a.limits)) or {}).get("scenarios", {}) if a.limits else {}
    print(f"{'scenario':<16}{'req/s':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'errors':>9}   limit p95")
    for name, r in results["scenarios"].items():
        print(f"{name:<16}{r['rps']:>8}{r['p50_ms']:>8}{r['p95_ms']:>8}{r['p99_ms']:>8}{r['error_rate']:>9.2%}"
              f"   {limits.get(name, {}).get('p95_ms', '-')}")
    problems = check(results, limits)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({**results, "problems": problems}, f, indent=2)
    for x in problems:
        print(f"LIMIT {x}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
