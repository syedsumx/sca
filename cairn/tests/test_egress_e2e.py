"""Egress rules enforced on the Docker runtime (L136), with real containers: a job runs on an internal network whose
only way out is Cairn's egress proxy (its own container, on that network and the outside one). The job reaches the
allowed host, is refused the other one (and cannot go around the proxy), every refusal is in the proxy's log, and with
the rule gone nothing outside is reachable at all.

Needs docker and the python:3.12-slim image."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid

import pytest

from cairn.common.db import get_engine, init_db
from cairn.network.egress import Egress, EgressRule
from cairn.pipelines.jobs import DockerRunner, Jobs

IMAGE = os.environ.get("CAIRN_EGRESS_TEST_IMAGE", "python:3.12-slim")
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
KEY = "egress-test-key-" + "q" * 24
OWNER = "urn:li:corpuser:ann"


def _ready() -> bool:
    if not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason=f"needs docker and {IMAGE}")


def docker(*args, check=True):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    tag = uuid.uuid4().hex[:6]
    out, inner = f"cairn-egress-out-{tag}", f"cairn-egress-in-{tag}"
    allowed, blocked, proxy = f"allowed-{tag}", f"blocked-{tag}", f"cairn-egress-proxy-{tag}"
    d = tmp_path_factory.mktemp("egress")
    (d / "key").write_text(KEY)
    os.chmod(d / "key", 0o644)
    try:
        docker("network", "create", out)
        docker("network", "create", "--internal", inner)
        for name in (allowed, blocked):
            docker("run", "-d", "--name", name, "--network", out, IMAGE, "python", "-m", "http.server", "8000")
        # The proxy: Cairn's own module, nothing else (only the standard library), with the key from a file.
        docker("run", "-d", "--name", proxy, "--network", out, "-v", f"{SRC}/cairn:/opt/src/cairn:ro",
               "-v", f"{d}/key:/run/egress/key:ro", "-e", "PYTHONPATH=/opt/src", "-e", "CAIRN_EGRESS_ALLOW_PRIVATE=true",
               "--user", "65534:65534", "--read-only", IMAGE, "python", "-m", "cairn.network.proxy",
               "--listen", "0.0.0.0:3128", "--key-file", "/run/egress/key")
        docker("network", "connect", inner, proxy)
        end = time.time() + 60
        while "egress.listening" not in docker("logs", proxy, check=False) + \
                subprocess.run(["docker", "logs", proxy], capture_output=True, text=True).stderr:
            assert time.time() < end, "the proxy did not start"
            time.sleep(0.5)
        yield {"out": out, "inner": inner, "allowed": allowed, "blocked": blocked, "proxy": proxy, "dir": d}
    finally:
        for c in (allowed, blocked, proxy):
            docker("rm", "-f", c, check=False)
        for n in (inner, out):
            docker("network", "rm", n, check=False)


CODE = """
import json, os, socket, urllib.request, urllib.error
p = json.loads(os.environ["CAIRN_JOB_PARAMS"])
res = {}
for k in ("allowed", "blocked"):
    try:
        res[k] = urllib.request.urlopen(f"http://{p[k]}:8000/", timeout=15).status
    except urllib.error.HTTPError as e:
        res[k] = e.code
    except Exception as e:
        res[k] = type(e).__name__
for k in ("allowed", "blocked"):      # around the proxy: straight to the host
    try:
        socket.create_connection((p[k], 8000), 5).close()
        res["direct_" + k] = "connected"
    except Exception as e:
        res["direct_" + k] = type(e).__name__
print(json.dumps(res))
"""


def test_a_job_reaches_only_what_the_rules_allow(net, tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/e.db")
    init_db(engine)
    egress = Egress(engine, enforce=True, proxy=f"http://{net['proxy']}:3128", key=KEY.encode(),
                    docker_network=net["inner"])
    egress.put(EgressRule(name="allowed", host=net["allowed"], ports=[8000]), "urn:li:corpuser:admin")
    jobs = Jobs(engine, DockerRunner(IMAGE, memory="256m", cpus="1"), lambda a: True, lambda a: False)
    jobs.egress = egress
    params = {"allowed": net["allowed"], "blocked": net["blocked"]}

    status, output, log, error = jobs.contained(OWNER, "python", CODE, params, 120, "egress", "run-allowed")
    assert status == "succeeded", (error, log)
    res = json.loads(output.strip().splitlines()[-1])
    assert res["allowed"] == 200, res                              # through the proxy, allowed
    assert res["blocked"] == 403, res                              # through the proxy, refused
    assert res["direct_allowed"] != "connected" and res["direct_blocked"] != "connected", res   # no way around it

    logs = subprocess.run(["docker", "logs", net["proxy"]], capture_output=True, text=True).stderr
    denied = [json.loads(ln) for ln in logs.splitlines() if '"egress.denied"' in ln]
    assert any(d["target"] == f"{net['blocked']}:8000" and d["run"] == "run-allowed" and d["reason"] == "no rule allows this host"
               for d in denied), logs

    # No rule: nothing outside, the host allowed a moment ago included (fail closed).
    egress.delete("allowed", "urn:li:corpuser:admin")
    status, output, log, error = jobs.contained(OWNER, "python", CODE, params, 120, "egress", "run-norule")
    res = json.loads(output.strip().splitlines()[-1])
    assert res["allowed"] == 403 and res["blocked"] == 403, res
    logs = subprocess.run(["docker", "logs", net["proxy"]], capture_output=True, text=True).stderr
    assert any(json.loads(ln).get("run") == "run-norule" and "only Cairn and the lakehouse" in json.loads(ln)["reason"]
               for ln in logs.splitlines() if '"egress.denied"' in ln), logs
