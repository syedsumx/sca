"""dbt's whole project in Cairn, on a real Trino, Cairn Catalog and Cairn Store: governance (contracts,
access, groups, versions, deprecation), exposures, MetricFlow metrics, unit tests and source freshness;
the run history and the state Slim CI defers to; the docs site, filtered per viewer; and packages from a
private hub mirror. Needs docker, dbt-trino (or CAIRN_DBT_IMAGE) and the binaries."""

import io
import json
import os
import shutil
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from test_dbt_e2e import PAT, _dbt, cairn_dbt, pipeline_user  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and _dbt()), reason="needs docker, dbt-trino and the binaries")
GOVERNED = os.path.join(os.path.dirname(__file__), "dbt_projects", "governed")


def env_for(url, trino, **extra):  # noqa: F811
    return {**os.environ, "CAIRN_URL": url, "CAIRN_TOKEN": PAT, "CAIRN_CA_FILE": f"{trino.dir}/ca.pem",
            "CAIRN_TRINO_HOST": "127.0.0.1", "CAIRN_TRINO_PORT": str(trino.tls), "CAIRN_TRINO_CATALOG": "lake",
            "CAIRN_DBT_SCHEMA": "analytics", "CAIRN_DBT_THREADS": "2", **extra}


def events(trino, stale=False):  # noqa: F811
    """lake.sales.events, loaded now (fresh) or 40+ days ago (stale)."""
    ages = (40, 41, 42) if stale else (0, 0, 0)
    rows = ", ".join(f"('{k}', CAST(current_timestamp AS timestamp(6)) - INTERVAL '{d}' DAY)" for k, d in zip("aab", ages))
    for stmt in ("CREATE SCHEMA IF NOT EXISTS lake.analytics", "DROP TABLE IF EXISTS lake.sales.events",
                 "CREATE TABLE lake.sales.events (kind varchar, loaded_at timestamp(6))",
                 f"INSERT INTO lake.sales.events VALUES {rows}"):
        trino(stmt)


def test_a_governed_project_lands_in_cairn_whole(trino, cairn, pipeline_user, tmp_path):  # noqa: F811
    url, s = cairn
    events(trino)
    proj = tmp_path / "shop"
    shutil.copytree(GOVERNED, proj)
    env = env_for(url, trino, CAIRN_DBT_ENVIRONMENT="prod")
    out = cairn_dbt(env, proj, "build")
    log = out.stdout + out.stderr
    assert out.returncode == 0 and "cairn dbt: recorded" in log, log[-5000:]

    counts, old = s.trino_access.urn("lake", "analytics", "event_counts"), s.trino_access.urn("lake", "analytics", "event_counts_old")
    props = s.graph.get_aspect(counts, "datasetProperties")["customProperties"]
    assert (props["dbt.contract"], props["dbt.access"], props["dbt.group"], props["dbt.group_owner"]) == \
        ("enforced", "public", "finance", "ana")
    assert s.graph.get_aspect(old, "datasetProperties")["customProperties"]["dbt.access"] == "protected"
    dep = s.graph.get_aspect(old, "deprecation")
    assert dep["deprecated"] is True and dep["decommissionTime"] > 0 and dep["note"].startswith("dbt:")
    # The exposure is a dashboard downstream of the model, owned by its owner.
    dash = "urn:li:dashboard:(dbt,shop.events_dashboard)"
    info = s.graph.get_aspect(dash, "dashboardInfo")
    assert info["title"] == "Events dashboard" and info["datasets"] == [counts] and info["externalUrl"].endswith("/events")
    assert {"owner": "urn:li:corpuser:ana", "type": "BUSINESS_OWNER"} in s.graph.get_aspect(dash, "ownership")["owners"]
    # The unit test is a check on the model; the metric waits in the Context inbox, mapped to its dataset.
    checks = {c["name"]: c["status"] for c in s.external_quality.for_dataset(counts)}
    assert checks.get("unit test: counts_events_per_kind") == "PASS", checks
    metric = [f for f in s.context.list(type_="METRIC_DEFINITION") if "total_events" in f["title"]]
    assert metric and counts in metric[0]["entities"], [f["title"] for f in s.context.list(type_="METRIC_DEFINITION")]

    # Source freshness: a check on the source, PASS while fresh and FAIL once stale.
    events_urn = s.trino_access.urn("lake", "sales", "events")
    out = cairn_dbt(env, proj, "source", "freshness")
    assert out.returncode == 0, (out.stdout + out.stderr)[-3000:]
    assert {c["name"]: c["status"] for c in s.external_quality.for_dataset(events_urn)}["source freshness"] == "PASS"
    events(trino, stale=True)
    out = cairn_dbt(env, proj, "source", "freshness")
    assert out.returncode != 0
    fresh = [c for c in s.external_quality.for_dataset(events_urn) if c["name"] == "source freshness"][0]
    assert fresh["status"] == "FAIL" and "error after 30 day" in fresh["message"], fresh

    # The run history: svc-dbt sees its own runs (an operator sees every run).
    runs = httpx.get(f"{url}/pipelines/dbt/runs", params={"project": "shop"}, headers={"Authorization": f"Bearer {PAT}"},
                     timeout=10).json()
    build = [r for r in runs if r["command"] == "build"][0]
    assert build["status"] == "success" and build["environment"] == "prod" and build["nodes"] >= 5 and build["tests_failed"] == 0
    assert {r["command"] for r in runs} >= {"build", "source freshness"}
    assert all(r["actor"] == "urn:li:corpuser:svc-dbt" for r in runs)

    # Slim CI: a change to one model builds only it (and what depends on it), deferring to prod's state.
    state = httpx.get(f"{url}/pipelines/dbt/state", params={"project": "shop", "environment": "prod"},
                      headers={"Authorization": f"Bearer {PAT}"}, timeout=10).json()
    assert state["invocation"] == build["invocation"] and "model.shop.event_counts" in state["manifest"]["nodes"]
    (proj / "models" / "event_counts_old.sql").write_text(
        "{{ config(materialized='view') }}\nselect kind, events, 1 as v from {{ ref('event_counts') }}\n")
    target = tmp_path / "ci-target"
    ci = env_for(url, trino, CAIRN_DBT_ENVIRONMENT="ci", CAIRN_DBT_DEFER_TO="prod", CAIRN_DBT_TARGET_PATH=str(target))
    out = cairn_dbt(ci, proj, "build", "--select", "state:modified+")
    log = out.stdout + out.stderr
    assert out.returncode == 0 and f"deferring to prod (run {build['invocation']})" in log, log[-4000:]
    ran = {r["unique_id"] for r in json.loads((target / "run_results.json").read_text())["results"]}
    assert ran == {"model.shop.event_counts_old"}, ran
    after = httpx.get(f"{url}/pipelines/dbt/state", params={"project": "shop", "environment": "prod"},
                      headers={"Authorization": f"Bearer {PAT}"}, timeout=10).json()
    assert after["invocation"] == build["invocation"]  # a CI run never becomes prod's state

    # The docs site, published by `dbt docs generate`, filtered for each viewer.
    out = cairn_dbt(env, proj, "docs", "generate")
    assert out.returncode == 0 and "docs published" in out.stdout + out.stderr, (out.stdout + out.stderr)[-3000:]
    r = httpx.get(f"{url}/pipelines/dbt/docs/shop/", headers={"Authorization": f"Bearer {PAT}"}, timeout=10)
    assert r.status_code == 200 and "<html" in r.text.lower()
    mine = httpx.get(f"{url}/pipelines/dbt/docs/shop/manifest.json", headers={"Authorization": f"Bearer {PAT}"},
                     timeout=10).json()
    assert "model.shop.event_counts" in mine["nodes"] and "source.shop.sales.events" in mine["sources"]
    bob = httpx.get(f"{url}/pipelines/dbt/docs/shop/manifest.json", headers={"X-Cairn-Actor": "urn:li:corpuser:bob"},
                    timeout=10).json()
    assert "model.shop.event_counts" not in bob["nodes"] and not bob["exposures"], sorted(bob["nodes"])
    cat = httpx.get(f"{url}/pipelines/dbt/docs/shop/catalog.json", headers={"X-Cairn-Actor": "urn:li:corpuser:bob"},
                    timeout=10).json()
    assert "model.shop.event_counts" not in cat["nodes"]


class Hub(BaseHTTPRequestHandler):
    """A private mirror of the dbt package hub, with one package: acme/util 1.0.0."""
    tarball = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}/"
        if self.path == "/api/v1/index.json":
            body = json.dumps(["acme/util"]).encode()
        elif self.path == "/api/v1/acme/util.json":
            body = json.dumps({"name": "util", "namespace": "acme", "latest": "1.0.0", "versions": {"1.0.0": {
                "name": "util", "packages": [], "downloads": {"tarball": base + "util-1.0.0.tar.gz"},
                "require_dbt_version": [">=1.0.0"]}}}).encode()
        elif self.path == "/util-1.0.0.tar.gz":
            body = Hub.tarball
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _package() -> bytes:
    files = {"util-1.0.0/dbt_project.yml": "name: util\nversion: '1.0.0'\nconfig-version: 2\n",
             "util-1.0.0/macros/shout.sql": "{% macro shout(x) %}upper({{ x }}){% endmacro %}\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_packages_come_from_a_private_hub(trino, cairn, pipeline_user, tmp_path):  # noqa: F811
    url, _ = cairn
    events(trino)
    Hub.tarball = _package()
    hub = ThreadingHTTPServer(("0.0.0.0", port()), Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    proj = tmp_path / "shouty"
    (proj / "models").mkdir(parents=True)
    (proj / "dbt_project.yml").write_text("name: shouty\nversion: '1.0'\nprofile: shouty\n")
    (proj / "packages.yml").write_text("packages:\n  - package: acme/util\n    version: 1.0.0\n")
    (proj / "models" / "loud_kinds.sql").write_text(
        "{{ config(materialized='table') }}\nselect distinct {{ util.shout('kind') }} as kind from lake.sales.events\n")
    env = env_for(url, trino, CAIRN_DBT_PACKAGE_HUB_URL=f"http://127.0.0.1:{hub.server_port}/",
                  DBT_PACKAGES_INSTALL_PATH=str(tmp_path / "pkgs"))
    try:
        out = cairn_dbt(env, proj, "deps")
        assert out.returncode == 0 and "Installed from version 1.0.0" in out.stdout + out.stderr, (out.stdout + out.stderr)[-3000:]
        out = cairn_dbt(env, proj, "build")
        assert out.returncode == 0, (out.stdout + out.stderr)[-3000:]
        assert sorted(r["kind"] for r in trino("SELECT kind FROM lake.analytics.loud_kinds")) == ["A", "B"]
    finally:
        hub.shutdown()
