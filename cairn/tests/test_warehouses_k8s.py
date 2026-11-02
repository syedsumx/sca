"""The warehouse proxy's Kubernetes scaler against a real API server, with the chart's own RBAC: its
account scales its warehouses' Deployments (0 and back) and nothing else. Needs CAIRN_K8S_BIN_DIR and helm."""

import asyncio
import os
import subprocess

import pytest

from test_workload_identity_e2e import BIN, k8s  # noqa: F401  (fixture)

HELM = os.environ.get("CAIRN_HELM", "helm")
CHART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "helm", "cairn")
pytestmark = pytest.mark.skipif(not (BIN and os.path.exists(os.path.join(BIN, "kube-apiserver"))) or
                                subprocess.run([HELM, "version"], capture_output=True).returncode != 0,
                                reason="needs CAIRN_K8S_BIN_DIR and helm")


def test_the_proxy_scales_its_warehouses_and_nothing_else(k8s, tmp_path):  # noqa: F811
    from cairn.dataaccess.warehouses import KubernetesScaler, WarehouseError

    kubectl, d = k8s["kubectl"], k8s["dir"]
    kubectl("create", "namespace", "lake")
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(CHART)))
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", "lake", "-s", "templates/warehouses.yaml",
                          "--set", "cairn.trino.enabled=true", "--set", "cairn.catalog.enabled=true",
                          "--set", "cairn.store.enabled=true", "--set", "cairn.trino.allowInsecureHttp=true",
                          "--set", "cairn.trino.warehouses.list[0].name=sales", "--set", "cairn.trino.warehouses.list[0].port=8443"],
                         cwd=cwd, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    (tmp_path / "wh.yaml").write_text(out.stdout)
    kubectl("-n", "lake", "apply", "-f", str(tmp_path / "wh.yaml"))
    for name in ("t-cairn-trino-wh-sales", "not-a-warehouse"):
        kubectl("-n", "lake", "create", "deployment", name, "--image=trinodb/trino:476", "--replicas=0")
    token = tmp_path / "token"
    token.write_text(kubectl("-n", "lake", "create", "token", "t-cairn-warehouses"))
    api = k8s["issuer"]

    def scaler(dep):
        return KubernetesScaler("lake", dep, api=api, token_file=str(token), ca_file=f"{d}/ca.crt")
    s = scaler("t-cairn-trino-wh-sales")

    async def flow():
        assert not await s.running()
        await s.start()
        assert await s.state() == (1, 0)              # wanted 1; nothing runs pods in this cluster
        await s.stop()
        assert not await s.running()
        with pytest.raises(WarehouseError, match="403"):
            await scaler("not-a-warehouse").start()      # its Role names only the warehouses
        with pytest.raises(WarehouseError, match="403"):
            await scaler("not-a-warehouse").running()
    asyncio.run(flow())
    assert kubectl("-n", "lake", "get", "deployment", "not-a-warehouse", "-o", "jsonpath={.spec.replicas}") == "0"
