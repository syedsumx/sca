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


@pytest.mark.parametrize("names,release", [
    (("sales", "sales-workers"), "t"),        # sales's workers and the warehouse sales-workers
    (("sales", "sales"), "t"),                # the same team twice
    (("data-science-team-a", "data-science-team-b"), "x" * 40),   # cut to 63 characters: the same name
])
def test_two_warehouses_never_share_a_kubernetes_object(names, release):
    """Review finding: the chart named each warehouse's objects trino-wh-<team> (and -workers), cut to 63 characters,
    and never checked them against each other: warehouse sales's workers and a warehouse called sales-workers, a team
    listed twice, or two long names under a long release name rendered one Deployment, Service, ConfigMap and
    NetworkPolicy for two warehouses, so one team's Trino replaced the other's. Such a list is refused."""
    sets = []
    for i, n in enumerate(names):
        sets += [f"cairn.trino.warehouses.list[{i}].name={n}", f"cairn.trino.warehouses.list[{i}].port={8443 + i}",
                 f"cairn.trino.warehouses.list[{i}].workers.max=2"]
    out = render(*sets, "cairn.networkPolicy.enabled=true", release=release)
    assert out.returncode != 0
    assert "cairn.trino.warehouses.list" in out.stderr and "same" in out.stderr, out.stderr


def test_the_proxy_asks_cairn_before_it_starts_a_warehouse():
    out = docs(render("cairn.trino.warehouses.list[0].name=sales", "cairn.trino.warehouses.list[0].port=8443"))
    proxy = [d for d in out if d["kind"] == "Deployment" and d["metadata"]["name"] == "t-cairn-warehouses"][0]
    env = {e["name"]: e for e in proxy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CAIRN_WAREHOUSES_ADMIT_URL"]["value"].endswith("/v1/trino")
    assert "CAIRN_TRINO_ACCESS_KEY" in env


def test_distinct_warehouses_render_distinct_objects():
    out = docs(render("cairn.trino.warehouses.list[0].name=sales", "cairn.trino.warehouses.list[0].port=8443",
                      "cairn.trino.warehouses.list[0].workers.max=2", "cairn.trino.warehouses.list[1].name=finance",
                      "cairn.trino.warehouses.list[1].port=8444", "cairn.networkPolicy.enabled=true"))
    keys = [(d["kind"], d["metadata"]["name"]) for d in out]
    assert len(keys) == len(set(keys)), [k for k in keys if keys.count(k) > 1]
    assert ("Deployment", "t-cairn-trino-wh-sales-workers") in keys and ("Deployment", "t-cairn-trino-wh-finance") in keys
