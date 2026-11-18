"""The store's CSI driver (``cairn.store.csi``) as rendered by ``helm template``.

* The node plugin runs privileged on every node, so it gets nothing else: no Kubernetes API rights (no
  service-account token), only the host paths a CSI node plugin needs, Bidirectional propagation only on the
  pods directory. The provisioner (which may create and delete PersistentVolumes) runs once, unprivileged,
  in a Deployment of its own.
* Mounts live in the node plugin's container, so an upgrade must not restart it under running pods: the
  DaemonSet updates OnDelete unless asked otherwise.
* Cluster-wide names carry the namespace, so releases of the same name in two namespaces do not collide.

Needs helm (CAIRN_HELM, or helm on PATH)."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="needs helm (CAIRN_HELM, or helm on PATH)")
BASE = ["cairn.store.enabled=true", "cairn.store.csi.enabled=true"]


def helm(*sets: str, namespace: str = "data"):
    args = [a for kv in (*BASE, *sets) for a in ("--set", kv)]
    return subprocess.run([HELM, "template", "t", "helm/cairn", "-n", namespace, "--show-only", "templates/store-csi.yaml", *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)


def docs(*sets: str, namespace: str = "data") -> list[dict]:
    out = helm(*sets, namespace=namespace)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def one(ds: list[dict], kind: str) -> dict:
    found = [d for d in ds if d["kind"] == kind]
    assert len(found) == 1, [d["metadata"]["name"] for d in found]
    return found[0]


def bound_accounts(ds: list[dict]) -> set[str]:
    return {s["name"] for d in ds if d["kind"] in ("RoleBinding", "ClusterRoleBinding") for s in d["subjects"]}


def test_node_plugin_has_no_api_rights():
    ds = docs()
    node = one(ds, "DaemonSet")
    spec = node["spec"]["template"]["spec"]
    assert spec.get("automountServiceAccountToken") is False
    assert spec["serviceAccountName"] not in bound_accounts(ds)
    assert {c["name"] for c in spec["containers"]} == {"driver", "registrar"}  # no provisioner on every node
    assert not spec.get("hostNetwork") and not spec.get("hostPID") and not spec.get("hostIPC")


def test_node_plugin_host_access_is_minimal():
    spec = one(docs(), "DaemonSet")["spec"]["template"]["spec"]
    hp = {v["name"]: v["hostPath"]["path"] for v in spec["volumes"] if "hostPath" in v}
    assert sorted(hp.values()) == sorted(["/dev/fuse", "/var/lib/kubelet/pods", "/var/lib/kubelet/plugins/csi.cairn.store",
                                          "/var/lib/kubelet/plugins_registry"])
    for c in spec["containers"]:
        for m in c.get("volumeMounts", []):
            if m.get("mountPropagation") == "Bidirectional":
                assert (c["name"], hp.get(m["name"])) == ("driver", "/var/lib/kubelet/pods"), (c["name"], m)
        if c["name"] != "driver":
            sc = c.get("securityContext") or {}
            assert not sc.get("privileged") and sc.get("allowPrivilegeEscalation") is False, c["name"]


def test_provisioner_runs_once_unprivileged():
    ds = docs()
    dep = one(ds, "Deployment")
    spec = dep["spec"]["template"]["spec"]
    assert dep["spec"]["replicas"] == 1
    assert "provisioner" in {c["name"] for c in spec["containers"]}
    assert not any("hostPath" in v for v in spec.get("volumes", []))
    for c in spec["containers"]:
        sc = c["securityContext"]
        assert not sc.get("privileged") and sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    crb = one(ds, "ClusterRoleBinding")
    assert [s["name"] for s in crb["subjects"]] == [spec["serviceAccountName"]]
    rules = one(ds, "ClusterRole")["rules"]
    assert not any("leases" in r["resources"] for r in rules)  # leases: in the release's namespace only
    role = one(ds, "Role")
    assert role["metadata"]["namespace"] == "data" and role["rules"][0]["resources"] == ["leases"]


def test_upgrades_do_not_restart_the_node_plugin_under_pods():
    assert one(docs(), "DaemonSet")["spec"]["updateStrategy"] == {"type": "OnDelete"}
    rolling = one(docs("cairn.store.csi.updateStrategy=RollingUpdate"), "DaemonSet")["spec"]["updateStrategy"]
    assert rolling["type"] == "RollingUpdate"


def test_cluster_wide_names_carry_the_namespace():
    a, b = docs(namespace="team-a"), docs(namespace="team-b")
    for kind in ("ClusterRole", "ClusterRoleBinding"):
        assert one(a, kind)["metadata"]["name"] != one(b, kind)["metadata"]["name"]
