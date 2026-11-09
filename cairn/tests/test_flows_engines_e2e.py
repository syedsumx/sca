"""Cairn Flows' engine steps against the real engines (L122), always as the flow's owner and failing closed:

* a ``stream`` step on a real Kafka broker: it notes where the topic ends, waits (parked, holding no thread) while the
  stream's worker has not written that far into its Iceberg table, then lets the flow go on, and the next step reads
  the rows on Trino; with no broker the step fails (it never counts as caught up), and one that does not catch up in
  time fails at its time limit;
* a ``dbt`` step in the cairn/dbt image (a project's image built on it): ``dbt build`` on Trino as the flow's owner (a
  Trino token minted for them), the seed and the model built in the lakehouse; an owner without the grant to write
  there is refused by Trino, and dbt's own output says so in the step's log.

The Spark step is in tests/test_flows_spark_e2e.py (it needs the Spark test's lakehouse); the Airflow step and the
exported DAGs run in tests/test_flows_airflow_e2e.py.

Needs docker (apache/kafka), CAIRN_STORE_BIN and CAIRN_CATALOG_BIN (Trino's fixtures) and, for dbt, CAIRN_DBT_IMAGE
(the cairn/dbt image; the pipelines CI job builds it)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from test_flows_e2e import flows  # noqa: F401  (fixtures)
from test_streams_e2e import kafka  # noqa: F401  (fixtures)
from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, lake, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")
DBT_IMAGE = os.environ.get("CAIRN_DBT_IMAGE", "")
ROOT = os.path.join(os.path.dirname(__file__), "..")
ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"


def steps_of(run):
    return {x["key"]: x for x in run["steps"]}


@pytest.fixture(scope="module")
def ticking(flows):  # noqa: F811
    """The scheduler's tick (``cairn scheduler`` in production): parked runs are resumed when they are due."""
    stop = threading.Event()

    def tick():
        while not stop.wait(1):
            try:
                flows.run_due()
            except Exception:  # noqa: BLE001 - shown by the assertions
                pass
    threading.Thread(target=tick, daemon=True).start()
    yield flows
    stop.set()


@pytest.fixture(scope="module")
def streams(flows, lake, kafka):  # noqa: F811
    from cairn.pipelines.streams import Streams

    s = Streams(flows.engine, {"bootstrap_servers": kafka["bootstrap"]},
                {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                 "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
                 "s3.path-style-access": "true"})
    from kafka.admin import NewTopic

    kafka["admin"].create_topics([NewTopic("flow-clicks", 2, 1)])
    flows.b.streams = s
    yield s
    flows.b.streams = None


def send(kafka, n0, n1):  # noqa: F811
    for i in range(n0, n1):
        kafka["producer"].send("flow-clicks", key=str(i).encode(), value=json.dumps({"n": i, "page": f"/p{i}"}).encode())
    kafka["producer"].flush()


def wait_until(fn, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return fn()


def test_a_stream_step_waits_for_kafka_then_the_flow_goes_on(ticking, streams, kafka, trino, cairn):  # noqa: F811
    from cairn.pipelines.streams import StreamWorker

    flows = ticking  # noqa: F811
    cairn[1].graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,flows_streams.clicks,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": "n"}, {"fieldPath": "page"}]}               # in Cairn's catalog, as a sync puts it
    st = streams.create({"name": "flow-clicks", "topic": "flow-clicks", "format": "json", "target": "flows_streams.clicks",
                         "create_table": True, "batch_seconds": 1}, ADMIN)
    send(kafka, 0, 6)
    flows.create(ADMIN, {"name": "after-clicks", "steps": [
        {"name": "caught-up", "type": "stream", "stream": "flow-clicks", "poll_s": 5},
        {"name": "count", "type": "sql", "after": ["caught-up"], "connection": "lakehouse",
         "sql": "SELECT count(*) AS n, max(n) AS top FROM flows_streams.clicks"}]}, publish=True)
    rid = flows.run(ADMIN, "after-clicks")["id"]
    # No worker yet: the step noted where the topic ends and waits, parked; the next step has not run.
    waiting = wait_until(lambda: steps_of(flows._run_public(rid))["caught-up"]["status"] == "waiting"
                         and flows._run_public(rid))
    assert waiting, flows._run_public(rid)
    assert steps_of(waiting)["count"]["status"] == "pending" and waiting["status"] in ("waiting", "running")
    assert sum(streams.offsets(streams.by_name("flow-clicks")).values()) < 6
    time.sleep(7)                                               # it looks again (poll_s) and still waits
    assert steps_of(flows._run_public(rid))["caught-up"]["status"] == "waiting"
    # The stream's worker writes the topic into its table; the step sees the offsets land and the flow goes on.
    w = StreamWorker(streams, {k: v for k, v in st.items() if k != "status"})
    w.start()
    try:
        done = flows.wait_for(rid, 180)
    finally:
        w.stop.set()
        w.join(60)
    by = steps_of(done)
    assert done["status"] == "succeeded", [(k, v["status"], v["error"]) for k, v in by.items()]
    out = by["caught-up"]["output"]
    assert sum(out["target"].values()) == 6 and all(out["offsets"][p] >= o for p, o in out["target"].items())
    assert by["count"]["output"]["preview"] == [[6, 5]]                     # read on Trino, after the wait
    assert done["run_as"] == ADMIN
    # More messages, no worker: the step does not catch up within its time limit, and fails.
    send(kafka, 6, 9)
    flows.create(ADMIN, {"name": "clicks-in-time", "steps": [
        {"name": "caught-up", "type": "stream", "stream": "flow-clicks", "poll_s": 5, "timeout_s": 12}]}, publish=True)
    late = flows.wait_for(flows.run(ADMIN, "clicks-in-time")["id"], 120)
    assert late["status"] == "failed" and "timed out after 12 s" in steps_of(late)["caught-up"]["error"], late
    # No broker: the step fails (never caught up by default), saying Kafka could not be reached.
    real = streams.kafka
    streams.kafka = {"bootstrap_servers": "127.0.0.1:1", "api_version_auto_timeout_ms": 3000}
    try:
        down = flows.wait_for(flows.run(ADMIN, "after-clicks")["id"], 120)
    finally:
        streams.kafka = real
    assert down["status"] == "failed", down
    assert "Kafka could not be reached for flow-clicks" in steps_of(down)["caught-up"]["error"]
    assert steps_of(down)["count"]["status"] == "skipped"
    # A stream that is not there: the flow is not published.
    from cairn.flows.model import FlowError

    with pytest.raises(FlowError, match="no such stream"):
        flows.create(ADMIN, {"name": "no-stream", "steps": [{"name": "s", "type": "stream", "stream": "nope"}]}, publish=True)


@pytest.fixture(scope="module")
def dbt_image(tmp_path_factory):
    """A project's image on cairn/dbt: the example project at /project (how a team ships its dbt project)."""
    if not DBT_IMAGE:
        pytest.skip("needs CAIRN_DBT_IMAGE (the cairn/dbt image; built in the pipelines CI job)")
    d = tmp_path_factory.mktemp("dbt-image")
    shutil.copytree(os.path.join(ROOT, "examples", "dbt", "lakehouse"), d / "project")
    (d / "Dockerfile").write_text(f"FROM {DBT_IMAGE}\nCOPY project /project\n")
    tag = f"cairn-test/flows-dbt:{os.getpid()}"
    b = subprocess.run(["docker", "build", "-q", "-t", tag, str(d)], capture_output=True, text=True, timeout=600)
    assert b.returncode == 0, b.stdout + b.stderr
    yield tag
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)


def test_a_dbt_step_builds_on_trino_as_the_owner(ticking, dbt_image, cairn, trino):  # noqa: F811
    from cairn.governance.abac import Policy, PolicyBundle

    flows = ticking  # noqa: F811
    _, s = cairn
    flows.b.dbt_image, flows.b.trino_catalog = dbt_image, "lake"
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,analytics.region_names,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": "region"}, {"fieldPath": "name"}]}
    bundle = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(     # ana may run jobs, not write analytics
        id="ana-jobs-dbt", effect="allow", actions=["RUN_JOBS"], condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    spec = {"steps": [{"name": "build", "type": "dbt", "command": "build", "project_dir": "/project", "timeout_s": 900},
                      {"name": "names", "type": "sql", "after": ["build"], "connection": "lakehouse",
                       "sql": "SELECT region, name FROM analytics.region_names ORDER BY region"}]}
    # Ana's flow first: the project, as ana, who may not create or write analytics. Trino refuses; the step fails,
    # and dbt's own output says why in the step's log.
    flows.create(ANA, {"name": "dbt-anas", **spec}, publish=True)
    run = flows.run(ANA, "dbt-anas", wait=True, timeout_s=900)
    by = steps_of(run)
    assert run["status"] == "failed" and run["run_as"] == ANA, run
    assert by["build"]["status"] == "failed" and by["names"]["status"] == "skipped"
    said = flows.step_log(ANA, run["id"], "build")["log"]
    assert "Access Denied" in said, said[-3000:]
    assert not trino("SELECT schema_name FROM lake.information_schema.schemata WHERE schema_name = 'analytics'")
    # The admin's flow builds it, as admin.
    flows.create(ADMIN, {"name": "dbt-nightly", **spec}, publish=True)
    run = flows.run(ADMIN, "dbt-nightly", wait=True, timeout_s=900)
    by = steps_of(run)
    assert run["status"] == "succeeded", (run, flows.step_log(ADMIN, run["id"], "build"))
    out = by["build"]["output"]["output"]
    assert "Completed successfully" in out and "region_names" in out, out[-3000:]
    assert by["names"]["output"]["preview"] == [["EU", "Europe"], ["US", "United States"]]
    assert trino("SELECT count(*) AS n FROM lake.analytics.regions") == [{"n": 2}]        # the seed, too
    # It ran on Trino as the owner: Trino's query history names admin, from the dbt image.
    users = {r["user"] for r in trino("SELECT \"user\" FROM system.runtime.queries WHERE query LIKE '%region_names%'")}
    assert "admin" in users, users
    log = flows.step_log(ADMIN, run["id"], "build")["log"]
    assert "Bearer" not in log and "jwt_token" not in log + out                    # nothing secret in its log
    # No dbt image configured: refused before anything runs.
    flows.b.dbt_image = ""
    try:
        run = flows.run(ADMIN, "dbt-nightly", wait=True, timeout_s=120)
    finally:
        flows.b.dbt_image = dbt_image
    assert run["status"] == "failed" and "CAIRN_FLOWS_DBT_IMAGE" in steps_of(run)["build"]["error"]
