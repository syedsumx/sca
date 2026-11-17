"""The chart's team warehouses as rendered (`helm template` only): the names of the Kubernetes objects and ports it
derives from team names. Needs helm (CAIRN_HELM, or helm on PATH)."""

import os
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="needs helm")
BASE = ["cairn.trino.enabled=true", "cairn.catalog.enabled=true", "cairn.store.enabled=true",
        "cairn.trino.allowInsecureHttp=true"]
IANA_SVC_NAME = re.compile(r"^(?=.*[a-z])(?!.*--)[a-z0-9]([a-z0-9-]{0,13}[a-z0-9])?$")


def render(*sets, release="t"):
    args = [a for kv in (*BASE, *sets) for a in ("--set", kv)]
    return subprocess.run([HELM, "template", release, "helm/cairn", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=300)


def docs(out):
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def test_port_names_are_unique_and_valid_whatever_the_team_names():
    """Review finding: the proxy's container and Service ports were named "wh-<team>" cut to 15 characters, so
    teams such as data-science-a and data-science-b got the same port name (and a name with "--" an invalid
    one): Kubernetes refuses the Deployment and the Service, and no warehouse is reachable."""
    out = docs(render("cairn.trino.warehouses.list[0].name=data-science-a", "cairn.trino.warehouses.list[0].port=8443",
                      "cairn.trino.warehouses.list[1].name=data-science-b", "cairn.trino.warehouses.list[1].port=8444",
                      "cairn.trino.warehouses.list[2].name=a--b", "cairn.trino.warehouses.list[2].port=8445",
                      "cairn.trino.warehouses.adaptive[0].name=data-science-auto",
                      "cairn.trino.warehouses.adaptive[0].port=8450",
                      "cairn.trino.warehouses.adaptive[0].tiers={data-science-a,data-science-b}",
                      "cairn.trino.warehouses.adaptive[0].largeWhen.cpu_ms=1000"))
    proxy = [d for d in out if d["kind"] == "Deployment" and d["metadata"]["name"] == "t-cairn-warehouses"][0]
    svc = [d for d in out if d["kind"] == "Service" and d["metadata"]["name"] == "t-cairn-warehouses"][0]
    for ports, key in ((proxy["spec"]["template"]["spec"]["containers"][0]["ports"], "containerPort"),
                       (svc["spec"]["ports"], "port")):
        names = [p["name"] for p in ports]
        assert len(set(names)) == len(names), names
        assert all(IANA_SVC_NAME.match(n) for n in names), names
        assert sorted(p[key] for p in ports) == [8443, 8444, 8445, 8450]
