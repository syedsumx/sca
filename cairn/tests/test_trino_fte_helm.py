"""The chart's Trino fault tolerance, spill and file cache (``cairn.trino.faultTolerance``, ``spill``, ``cache``), as
rendered by ``helm template``. ``tests/test_trino_helm.py`` runs the same render in Docker.

* Spilled pages are query data (rows of tables, read under the person's grants) written to the node's disk:
  they are encrypted.

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
BASE = ["cairn.store.enabled=true", "cairn.catalog.enabled=true", "cairn.trino.enabled=true",
        "cairn.trino.tls.secretName=trino-tls", "cairn.trino.workers=2"]


def helm(*sets: str):
    return subprocess.run([HELM, "template", "t", "helm/cairn", *[a for kv in (*BASE, *sets) for a in ("--set", kv)]],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)


def trino_config(*sets: str) -> dict:
    out = helm(*sets)
    assert out.returncode == 0, out.stderr
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    return next(d for d in docs if d["kind"] == "ConfigMap" and "worker.properties" in (d.get("data") or {}))["data"]


def test_spill_is_encrypted():
    cm = trino_config("cairn.trino.spill.enabled=true")
    for f in ("config.properties", "worker.properties"):
        assert "spill-enabled=true" in cm[f]
        assert "spill-encryption-enabled=true" in cm[f], f
    assert "spill-encryption" not in trino_config()["worker.properties"]


@pytest.mark.parametrize("bad, message", [
    ("cairn.trino.faultTolerance.exchangeBucket=", "exchangeBucket"),
    ("cairn.trino.faultTolerance.exchangeBucket=lake/exchange", "exchangeBucket"),
    ("cairn.trino.faultTolerance.exchangeBucket=a\\,s3://other", "exchangeBucket"),
    ("cairn.trino.faultTolerance.exchangeKeySecret.name=", "exchangeKeySecret"),
    ("cairn.trino.faultTolerance.exchangeKeySecret.secretKeyKey=", "exchangeKeySecret"),
])
def test_fault_tolerance_values_are_checked(bad, message):
    """A wrong exchange bucket or key Secret fails the render, not the pods: an empty bucket renders
    ``s3://``, a comma adds a second exchange location, and an empty Secret name leaves every Trino pod unable
    to start."""
    out = helm("cairn.trino.faultTolerance.enabled=true", bad)
    assert out.returncode != 0 and message in out.stderr, out.stderr
    assert "exchange.base-directories=s3://trino-exchange\n" in trino_config(
        "cairn.trino.faultTolerance.enabled=true")["exchange-manager.properties"]
