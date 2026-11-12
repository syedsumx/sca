"""The updater on a real Kubernetes cluster (L160): releases of a small app (tests/fixtures/updater-app), signed and
published to a real feed, their charts in a real OCI registry, applied to a kind cluster by cairn.release.runner.

* 1.0.0 -> 1.1.0 succeeds: the chart's digest checked, the image pinned to the signed digest, the health answer
  checked through the Kubernetes API.
* 1.1.0 -> 1.2.0 never becomes ready: the database did not move, so the release is rolled back by itself.
* The same failure after the database moved: with no restore step the updater refuses to run old code on a newer
  schema and says what to restore; with one, it restores and rolls back.
* The updater Job: started through the Kubernetes API from a suspended CronJob template, with the plan's id.

Runs where there is a kind cluster, helm and docker (CI's terraform-modules job): CAIRN_KIND_E2E=1."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import sqlalchemy
import yaml

from cairn.common.db import metadata
from cairn.release import publish as pub
from cairn.release.feed import ReleaseFeed, state_table
from cairn.release.runner import JobLauncher, Runner
from cairn.release.updates import UpdateManager, plans_table, policy_table
from test_release_feed import keypair, manifest, served  # noqa: F401 - the served fixture

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_KIND_E2E") != "1", reason="needs a kind cluster (CI)")
FIXTURE = Path(__file__).parent / "fixtures" / "updater-app"
NS, REL, IMAGE = "updater-e2e", "app", "python:3.12-alpine"
HELM, KUBECTL = os.environ.get("HELM", "helm"), os.environ.get("KUBECTL", "kubectl")


def sh(*args, check=True, **kw) -> str:
    r = subprocess.run(list(args), capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args)}: {r.stderr or r.stdout}")
    return r.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def registry():
    port = free_port()
    name = f"cairn-updater-registry-{port}"
    sh("docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:5000", "registry:2")
    for _ in range(30):
        if subprocess.run(["curl", "-fsS", f"http://127.0.0.1:{port}/v2/"], capture_output=True).returncode == 0:
            break
        time.sleep(1)
    yield f"127.0.0.1:{port}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def image_digest() -> str:
    sh("docker", "pull", "-q", IMAGE)
    ref = sh("docker", "inspect", "--format", "{{index .RepoDigests 0}}", IMAGE).strip()   # python@sha256:...
    return ref.split("@", 1)[1]


def release(tmp: Path, version: str, min_from: str, registry: str, digest: str, broken: bool = False) -> dict:
    """Package the fixture at ``version``, push it, and describe it as a signed release would."""
    src = tmp / f"chart-{version}"
    shutil.copytree(FIXTURE, src)
    values = yaml.safe_load((src / "values.yaml").read_text())
    values["broken"] = broken
    (src / "values.yaml").write_text(yaml.safe_dump(values))
    out = tmp / f"pkg-{version}"
    out.mkdir()
    sh(HELM, "package", str(src), "--version", version, "--app-version", version, "--destination", str(out))
    tgz = next(out.glob("*.tgz"))
    sh(HELM, "push", str(tgz), f"oci://{registry}/charts", "--plain-http")
    m = manifest(version, min_from)
    m["chart"] = {"name": "updater-app", "version": version, "url": f"oci://{registry}/charts/updater-app",
                  "digest": "sha256:" + hashlib.sha256(tgz.read_bytes()).hexdigest()}
    m["images"] = {"app": f"docker.io/library/python@{digest}"}
    m["requires"] = {"kubernetes": "1.28", "helm": "3.14"}
    return m


def health():
    r = subprocess.run([KUBECTL, "get", "--raw", f"/api/v1/namespaces/{NS}/services/http:{REL}:80/proxy/health"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return 503, {}
    try:
        return 200, json.loads(r.stdout)
    except ValueError:
        return 502, {}


def deployed_image() -> str:
    return sh(KUBECTL, "-n", NS, "get", "deploy", REL, "-o", "jsonpath={.spec.template.spec.containers[0].image}")


def test_updates_on_kind(served, registry, tmp_path):  # noqa: F811
    feed_dir, url = served
    digest = image_digest()
    signer, keys = keypair("rel-1")
    for v, min_from, broken in (("1.1.0", "1.0.0", False), ("1.2.0", "1.1.0", True)):
        m = release(tmp_path, v, min_from, registry, digest, broken)
        pub.publish(feed_dir, pub.sign_manifest(m, [signer]), [signer])

    subprocess.run([KUBECTL, "delete", "namespace", NS, "--wait=true"], capture_output=True)
    sh(KUBECTL, "create", "namespace", NS)
    base = tmp_path / "pkg-base"
    base.mkdir()
    sh(HELM, "package", str(FIXTURE), "--version", "1.0.0", "--app-version", "1.0.0", "--destination", str(base))
    sh(HELM, "install", REL, str(next(base.glob("*.tgz"))), "-n", NS, "--wait", "--timeout", "180s")
    assert health()[1]["version"] == "1.0.0" and deployed_image().endswith(":3.12-alpine")

    e = sqlalchemy.create_engine(f"sqlite:///{tmp_path}/plans.db")
    metadata.create_all(e, tables=[state_table, policy_table, plans_table])

    def runner(at: str, head=lambda: "0147", restore: str = "", timeout: int = 180) -> tuple[Runner, dict]:
        feed = ReleaseFeed(e, url, "stable", at, keys=keys, dev_keys={})
        mgr = UpdateManager(e, feed)
        mgr.set_policy("urn:li:corpuser:ops", {"require_approval": False})
        return Runner(REL, NS, "", feed=feed, head=head, helm=HELM, kubectl=KUBECTL, restore_command=restore,
                      timeout_s=timeout, plain_http=True, health_get=health), mgr

    # 1.0.0 -> 1.1.0: pinned to the signed digest, healthy.
    r, mgr = runner("1.0.0")
    plan = mgr.plan("urn:li:corpuser:ops", version="1.1.0")
    status, why = r.apply({"from": "1.0.0", "steps": mgr._row(plan["id"])["steps"]})
    assert (status, why) == ("succeeded", None)
    assert deployed_image() == f"docker.io/library/python@{digest}" and health()[1]["version"] == "1.1.0"
    with e.begin() as c:   # the scheduler would have marked it running and the updater finished it
        c.execute(plans_table.update().values(status="succeeded"))

    # 1.1.0 -> 1.2.0 never becomes ready; the database did not move: rolled back by itself.
    r, mgr = runner("1.1.0", timeout=60)
    plan = mgr.plan("urn:li:corpuser:ops", version="1.2.0")
    steps = mgr._row(plan["id"])["steps"]
    status, why = r.apply({"from": "1.1.0", "steps": steps})
    assert status == "rolled_back", why
    assert json.loads(sh(HELM, "status", REL, "-n", NS, "-o", "json"))["info"]["status"] == "deployed"
    assert health()[1]["version"] == "1.1.0"
    with e.begin() as c:
        c.execute(plans_table.update().values(status="rolled_back"))

    # The same failure after the database moved, and no restore step: left as it is, with what to do.
    heads = iter(["0147", "0148", "0148"])
    r, _ = runner("1.1.0", head=lambda: next(heads), timeout=60)
    status, why = r.apply({"from": "1.1.0", "steps": steps})
    assert status == "failed" and "no restore step" in why and "helm rollback app" in why
    sh(HELM, "rollback", REL, "-n", NS, "--wait")              # what the operator does after restoring

    # With a restore step: restored, then rolled back.
    heads = iter(["0147", "0148", "0148"])
    logged = []
    r, _ = runner("1.1.0", head=lambda: next(heads), restore='echo "restored $CAIRN_UPDATE_BACKUP_ID"', timeout=60)
    r.report = lambda step, msg: logged.append((step, msg))
    status, why = r.apply({"from": "1.1.0", "steps": steps})
    assert status == "rolled_back", why
    assert ("restore", "restored") in [(s, m.strip()) for s, m in logged]
    assert health()[1]["version"] == "1.1.0"


def test_the_updater_job_starts_from_its_template(tmp_path):
    subprocess.run([KUBECTL, "create", "namespace", NS], capture_output=True)
    cron = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "app-updater", "namespace": NS},
            "spec": {"schedule": "0 0 31 2 *", "suspend": True, "jobTemplate": {"spec": {"template": {"spec": {
                "restartPolicy": "Never",
                "containers": [{"name": "updater", "image": IMAGE, "command": ["python", "-c",
                                "import os; print('applying', os.environ['CAIRN_UPDATE_PLAN'])"]}]}}}}}}
    (tmp_path / "cron.yaml").write_text(yaml.safe_dump(cron))
    sh(KUBECTL, "apply", "-f", str(tmp_path / "cron.yaml"))
    port = free_port()
    proxy = subprocess.Popen([KUBECTL, "proxy", f"--port={port}"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)
        launcher = JobLauncher("app-updater", NS, api=f"http://127.0.0.1:{port}")
        job = launcher.start("upd_0123456789abcdef")
        for _ in range(90):
            if launcher.state(job) in ("succeeded", "failed"):
                break
            time.sleep(2)
        assert launcher.state(job) == "succeeded"
        logs = sh(KUBECTL, "-n", NS, "logs", f"job/{job}")
        assert "applying upd_0123456789abcdef" in logs
        assert launcher.state("no-such-job") == "missing"
    finally:
        proxy.terminate()
        subprocess.run([KUBECTL, "delete", "namespace", NS, "--wait=false"], capture_output=True)
