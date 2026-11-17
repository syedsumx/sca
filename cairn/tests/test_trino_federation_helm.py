"""The chart's federated Trino catalogs (``cairn.trino.federation``), as rendered by ``helm template``.

* A catalog given no ``database`` is named to Cairn by its catalog name (``crm.sales.customers``), as
  ``CAIRN_TRINO_CATALOGS`` does when the key is left out: two PostgreSQL catalogs never share dataset names, so a
  grant on one database's table is never a grant on another's.

Needs helm (CAIRN_HELM, or helm on PATH)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
import yaml

from cairn.dataaccess.trino import TrinoAccess, parse_catalogs

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="needs helm (CAIRN_HELM, or helm on PATH)")
BASE = ["cairn.store.enabled=true", "cairn.catalog.enabled=true", "cairn.trino.enabled=true",
        "cairn.trino.tls.secretName=trino-tls"]


def helm(federation: list[dict]):
    values = {"cairn": {"trino": {"federation": federation}}}
    return subprocess.run([HELM, "template", "t", "helm/cairn", "-f", "-",
                           *[a for kv in BASE for a in ("--set", kv)]], input=yaml.safe_dump(values), cwd=ROOT,
                          capture_output=True, text=True, timeout=300)


def render(federation: list[dict]) -> list[dict]:
    out = helm(federation)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def api_env(docs: list[dict]) -> dict[str, str]:
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "t-cairn-api":
            return {e["name"]: e.get("value") for e in d["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    raise AssertionError("no API deployment")


def pg(name: str, **extra) -> dict:
    return {"name": name, "connector": "postgresql", "platform": "postgres",
            "properties": {"connection-url": f"jdbc:postgresql://{name}-db:5432/{name}"}, **extra}


def test_a_catalog_without_a_database_is_named_by_its_catalog():
    env = api_env(render([pg("crm"), pg("hr"), pg("erp", database="erp_main")]))
    access = TrinoAccess(lambda *a: None, lambda *a: None, parse_catalogs(json.loads(env["CAIRN_TRINO_CATALOGS"])))
    assert access.dataset_name("crm", "public", "customers") == ("postgres", "crm.public.customers", "PROD")
    assert access.dataset_name("hr", "public", "customers") == ("postgres", "hr.public.customers", "PROD")
    assert access.dataset_name("erp", "public", "orders") == ("postgres", "erp_main.public.orders", "PROD")
