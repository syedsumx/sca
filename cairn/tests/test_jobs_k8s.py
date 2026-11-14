"""Scheduled jobs on a real Kubernetes cluster (L57): each run a Job in the jobs namespace, made and removed with
only the chart's Role; the executed notebook kept, a failure and the time limit recorded, the pod locked down, and
nothing (the token's Secret included) left behind.

Needs a cluster (CAIRN_KIND_KUBECONFIG), helm, and the jobs image (CAIRN_JOBS_TEST_IMAGE) loaded into it."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess

import pytest
import yaml

from cairn.common.db import get_engine, init_db
from cairn.pipelines.jobs import Jobs, KubeRunner

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
IMAGE = os.environ.get("CAIRN_JOBS_TEST_IMAGE", "cairn/jobs:ci")
NS = "cairn-jobs-test"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALICE = "urn:li:corpuser:alice"
pytestmark = pytest.mark.skipif(not (KUBECONFIG and shutil.which(HELM) and shutil.which("kubectl")),
                                reason="needs a cluster (CAIRN_KIND_KUBECONFIG), helm and kubectl")


def kubectl(*args, check=True, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def svc(tmp_path):
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    for ns in (NS, "cairn"):
        kubectl("create", "namespace", ns, check=False)
    kubectl("-n", "cairn", "create", "serviceaccount", "t-cairn", check=False)
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", "cairn", "-s", "templates/jobs-kubernetes.yaml",
                          "--set", "cairn.jobs.runtime=kubernetes", "--set", f"cairn.jobs.namespace={NS}"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kubectl("apply", "-f", "-", stdin=out.stdout)
    token = tmp_path / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", "t-cairn", "--duration", "1h"))
    cfg = yaml.safe_load(open(KUBECONFIG))["clusters"][0]["cluster"]
    ca = tmp_path / "ca.crt"
    ca.write_bytes(base64.b64decode(cfg["certificate-authority-data"]))
    engine = get_engine(f"sqlite:///{tmp_path}/j.db")
    init_db(engine)
    told = []
    s = Jobs(engine, KubeRunner(NS, IMAGE, cfg["server"], str(token), str(ca), poll=1.0), lambda a: True,
             lambda a: False, lambda owner, ttl: "tok-" + "x" * 40, "https://trino.example:8443",
             lambda *a: told.append(a))
    s.told = told
    yield s
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")


def left_behind() -> list[str]:
    return [x for x in kubectl("-n", NS, "get", "jobs,configmaps,secrets", "-l", "app.kubernetes.io/component=job",
                               "-o", "name").splitlines() if x]


def test_jobs_run_as_kubernetes_jobs(svc):
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"}},
          "cells": [{"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                     "source": "import os\nst = open('/proc/self/status').read()\n"
                               "print(os.environ['CAIRN_USER'], PARAMS['n'], os.getuid(), bool(os.environ['CAIRN_TRINO_TOKEN']),"
                               " st.split('CapEff:')[1].split()[0], st.split('NoNewPrivs:')[1].split()[0])"}]}
    svc.save(ALICE, {"name": "k-notebook", "kind": "notebook", "notebook": nb, "params": {"n": 7}})
    r = svc.run_now(ALICE, "k-notebook", wait=True)
    assert r["status"] == "succeeded", r
    out = json.loads(svc.run_detail(ALICE, "k-notebook", r["run"])["output"])
    texts = ["".join(o.get("text", "")) for c in out["cells"] for o in c.get("outputs", [])]
    assert "alice 7 1000 True 0000000000000000 1\n" in texts, texts
    assert left_behind() == []                                           # the token's Secret is gone with the rest

    svc.save(ALICE, {"name": "k-fails", "kind": "python", "code": "print('before')\nraise SystemExit('bad input')"})
    r = svc.run_now(ALICE, "k-fails", wait=True)
    assert r["status"] == "failed" and "bad input" in r["error"], r
    assert svc.run_detail(ALICE, "k-fails", r["run"])["output"].strip() == "before"
    assert svc.told[-1][0] == "job.failed"
    assert left_behind() == []


def test_a_run_over_its_time_limit_is_stopped_by_kubernetes(svc):
    svc.save(ALICE, {"name": "k-slow", "kind": "python", "code": "import time\ntime.sleep(600)", "timeout_minutes": 1})
    r = svc.run_now(ALICE, "k-slow", wait=True)
    assert r["status"] == "failed" and "time limit" in r["error"], r
    assert left_behind() == []
