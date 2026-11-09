"""Scale (5.3), against real Cairn processes: `cairn pdp` replicas at a fixed 2,000 requests a second for a minute,
cached and uncached decisions, and `cairn gateway` with 100 real agents moving 1 GB of metadata in one run.

The measurements go to ``CAIRN_LOAD_REPORT`` (a directory: scale.json and summary.md). The run fails on a
regression: errors, or numbers past the floors in ``tests/load/scale-limits.yaml``. The published targets (PDP p99
under 5 ms cached and 25 ms uncached at 2,000 requests a second) are reported against, met or not; the floors are
what this kind of machine has been measured to do, with headroom.

Heavy: runs only with ``CAIRN_LOAD_SCALE=1`` (CI job python). ``CAIRN_LOAD_RATE``, ``CAIRN_LOAD_DURATION``,
``CAIRN_LOAD_PDP_REPLICAS``, ``CAIRN_LOAD_AGENTS`` and ``CAIRN_LOAD_MEGABYTES`` change the shape;
``CAIRN_TEST_PG_URL`` puts the gateway's database on PostgreSQL."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
import yaml

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_LOAD_SCALE") != "1", reason="set CAIRN_LOAD_SCALE=1 (heavy)")

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("scale", os.path.join(HERE, "load", "scale.py"))
scale = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scale)

RATE = float(os.environ.get("CAIRN_LOAD_RATE", "2000"))
DURATION = float(os.environ.get("CAIRN_LOAD_DURATION", "60"))
REPLICAS = int(os.environ.get("CAIRN_LOAD_PDP_REPLICAS", "3"))
AGENTS = int(os.environ.get("CAIRN_LOAD_AGENTS", "100"))
MEGABYTES = float(os.environ.get("CAIRN_LOAD_MEGABYTES", "1024"))
TOKEN = "load-svc-" + os.urandom(12).hex()


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(cmd: str, tmp, name: str, extra: dict) -> tuple:
    p = _port()
    env = {**os.environ, "CAIRN_SERVICE_TOKEN": TOKEN, "CAIRN_DATABASE_URL": f"sqlite:///{tmp}/{name}.db",
           "PYTHONPATH": os.path.join(HERE, "..", "src"), "CAIRN_LOG_LEVEL": "WARNING", **extra}
    log = open(tmp / f"{name}.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "cairn.cli", cmd, "--host", "127.0.0.1", "--port", str(p),
                             *(["--no-dispatcher"] if cmd == "gateway" else [])], env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    end = time.time() + 120
    while True:
        assert proc.poll() is None, (tmp / f"{name}.log").read_text()[-3000:]
        try:
            httpx.get(f"http://127.0.0.1:{p}/health", timeout=2)
            return proc, f"http://127.0.0.1:{p}"
        except httpx.HTTPError:
            assert time.time() < end, f"{name} did not start"
            time.sleep(0.3)


@pytest.fixture(scope="module")
def report():
    out: dict = {"machine": {"cpus": os.cpu_count()}, "targets": {}}
    yield out
    d = os.environ.get("CAIRN_LOAD_REPORT")
    if d:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "scale.json"), "w") as f:
            json.dump(out, f, indent=2)
        lines = ["| measurement | offered | achieved | p50 ms | p99 ms | errors | target |", "|---|---|---|---|---|---|---|"]
        for k in ("pdp_cached", "pdp_uncached"):
            if k in out:
                r = out[k]
                lines.append(f"| {k} ({r['replicas']} replicas, {r['duration_s']:.0f} s) | {r['offered_rps']:.0f}/s | "
                             f"{r['achieved_rps']:.0f}/s | {r['p50_ms']} | {r['p99_ms']} | {r['errors']} | "
                             f"{out['targets'].get(k, '')} |")
        if "gateway" in out:
            g = out["gateway"]
            lines.append(f"| gateway ({g['agents']} agents) | {g['megabytes']} MiB | {g['mib_per_second']} MiB/s in "
                         f"{g['seconds']} s | {g['batch_p50_ms']} | {g['batch_p99_ms']} | {g['errors']} | "
                         f"{out['targets'].get('gateway', '')} |")
        with open(os.path.join(d, "summary.md"), "w") as f:
            f.write("\n".join(lines) + "\n")


def limits() -> dict:
    with open(os.path.join(HERE, "load", "scale-limits.yaml")) as f:
        return yaml.safe_load(f)


class CoreSink:
    """Cairn Core's API as the PDP and the gateway use it, answered at once: aspect reads find nothing (404), ingest
    proposals are counted. What is measured is Cairn's own work, not the catalog's reads and writes."""

    def __init__(self):
        import http.server
        import threading

        sink = self
        self.proposals, self.lock = 0, threading.Lock()

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                with sink.lock:
                    sink.proposals += len(body.get("proposals") or []) + (1 if "proposal" in body else 0)
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", _port()), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_the_pdp_at_a_fixed_rate_cached_and_uncached(tmp_path, report):
    policy_dir = os.path.join(HERE, "..", "config", "policies")
    core = CoreSink()
    procs = []
    try:
        urls = []
        for i in range(REPLICAS):
            proc, url = _start("pdp", tmp_path, f"pdp{i}", {"CAIRN_POLICY_DIR": policy_dir,
                                                                "CAIRN_CORE_URL": core.url})
            procs.append(proc)
            urls.append(url)
        tf = tmp_path / "token"
        tf.write_text(TOKEN)
        # The decisions are real: a contractor is refused a PII profile by the baseline bundle.
        r = httpx.post(f"{urls[0]}/governance/v1/authorize", headers={"Authorization": f"Bearer {TOKEN}"}, json={
            "actor": "urn:li:corpuser:c", "privilege": "VIEW_DATASET_PROFILE",
            "resource": {"type": "dataset", "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t,PROD)"},
            "subject_attributes": {"employment_type": "contractor"}, "resource_attributes": {"classification": ["PII"]}},
            timeout=10)
        assert r.status_code == 200 and r.json()["decision"] == "DENY", r.text
        lim = limits()["pdp"]
        modes = os.environ.get("CAIRN_LOAD_PDP_MODES", "cached,uncached").split(",")
        for mode, target_p99 in [m for m in (("cached", 5.0), ("uncached", 25.0)) if m[0] in modes]:
            rate = float(lim[mode].get("rate", RATE))     # a rate the machine sustains: past it, p99 is the backlog
            got = scale.pdp_load(urls, TOKEN, rate, DURATION, mode)
            report[f"pdp_{mode}"] = got
            met = got["achieved_rps"] >= 0.98 * 2000 and got["p99_ms"] <= target_p99
            report["targets"][f"pdp_{mode}"] = (f"{'met' if met else 'not met'}: p99 < {target_p99:g} ms at 2000/s "
                                                f"(measured at {rate:.0f}/s offered)")
            print(f"pdp {mode}: {json.dumps(got)}")
            assert got["errors"] == 0, got
            floor = lim[mode]["min_rps"] if "min_rps" in lim[mode] else lim[mode]["min_fraction"] * rate
            assert got["achieved_rps"] >= floor, (got, lim[mode])
            assert got["p99_ms"] <= lim[mode]["max_p99_ms"], (got, lim[mode])
    finally:
        for p in procs:
            p.terminate()
            p.wait(30)
        core.server.shutdown()


def test_the_gateway_with_100_agents_moving_1_gb(tmp_path, report):
    db = os.environ.get("CAIRN_TEST_PG_URL") or f"sqlite:///{tmp_path}/gateway.db"
    core = CoreSink()
    proc, url = _start("gateway", tmp_path, "gateway", {"CAIRN_DATABASE_URL": db, "CAIRN_CORE_URL": core.url,
                                                        "CAIRN_GATEWAY_KEY_DIR": str(tmp_path / "keys")})
    try:
        got = scale.gateway_volume(url, db, AGENTS, MEGABYTES)
    finally:
        proc.terminate()
        proc.wait(30)
        core.server.shutdown()
    got["forwarded_to_core"] = core.proposals
    report["gateway"] = got
    lim = limits()["gateway"]
    report["targets"]["gateway"] = (f"{'met' if got['megabytes'] >= 0.99 * MEGABYTES and not got['errors'] else 'not met'}"
                                    f": {AGENTS} agents, {MEGABYTES:.0f} MiB in one run")
    print(f"gateway: {json.dumps(got)}")
    assert got["errors"] == 0 and got["rejected"] == 0 and got["runs_succeeded"] == AGENTS, got
    assert got["megabytes"] >= 0.99 * MEGABYTES and got["accepted"] == got["mcps"] == got["forwarded_to_core"], got
    assert got["mib_per_second"] >= lim["min_mib_per_second"], (got, lim)
    assert got["batch_p99_ms"] <= lim["max_batch_p99_ms"], (got, lim)
