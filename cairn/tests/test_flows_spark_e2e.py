"""A Cairn flow's ``spark`` step in the cairn/spark image (L122), as the flow's owner, on Cairn Catalog and Cairn Store:
the step's container gets a lakehouse session for the owner (never a store key, never anyone else's), submits the
application (a project's image built on cairn/spark), and Spark reads and writes through the catalog under Cairn's
decisions for the owner: ana, who may build in analytics, builds a table there; bob, who may not see analytics, is
refused, his step fails with Spark's error in its log and nothing is written. With Spark not configured, or no session
to be had for the owner, the step fails before anything runs.

Needs docker, CAIRN_SPARK_IMAGE (the cairn/spark image; the pipelines CI job builds it) and the store and catalog
binaries (the lakehouse of tests/test_spark_e2e.py)."""

from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

from test_spark_e2e import IMAGE, lake, people  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and IMAGE), reason="needs docker, CAIRN_SPARK_IMAGE and the binaries")
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"

APP = textwrap.dedent('''
    import json, sys
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()       # the lakehouse settings come from the step (spark-submit)
    target = sys.argv[1]
    n = spark.sql("SELECT count(*) AS n FROM lakehouse.analytics.products").collect()[0].n
    spark.sql(f"CREATE OR REPLACE TABLE lakehouse.analytics.{target} AS "
              "SELECT sku, price * 2 AS price FROM lakehouse.analytics.products WHERE price > 5")
    print(json.dumps({"products": n, "built": target}))
    spark.stop()
''')


def steps_of(run):
    return {x["key"]: x for x in run["steps"]}


@pytest.fixture(scope="module")
def spark_app_image(tmp_path_factory):
    """A team's Spark image: cairn/spark with its application at /opt/cairn-apps/app.py."""
    d = tmp_path_factory.mktemp("spark-image")
    (d / "app.py").write_text(APP)
    (d / "Dockerfile").write_text(f"FROM {IMAGE}\nCOPY app.py /opt/cairn-apps/app.py\n")
    tag = f"cairn-test/flows-spark:{os.getpid()}"
    b = subprocess.run(["docker", "build", "-q", "-t", tag, str(d)], capture_output=True, text=True, timeout=600)
    assert b.returncode == 0, b.stdout + b.stderr
    yield tag
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def flows(cairn, lake, people, spark_app_image):  # noqa: F811
    from cairn.common.security import Principal
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.pipelines.jobs import DockerRunner, Jobs

    url, s = cairn
    s.lakehouse.catalog_uris = (lake["catalog"],)
    authz = lambda privilege: lambda actor: s.authorize(Principal(actor, "token"), privilege, None,  # noqa: E731
                                                         raise_on_deny=False)
    bundle = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(      # both may run jobs; their data grants differ
        id="spark-flow-jobs", effect="allow", actions=["RUN_JOBS"],
        condition={"attr": "subject.urn", "op": "in", "value": [ANA, BOB]})]))
    jobs = Jobs(s.engine, DockerRunner("python:3.12-slim", network="host", memory="3g", cpus="2"), authz("RUN_JOBS"),
                authz("MANAGE_SYSTEM_OPERATIONS"), None, "", None, s.audit, "")
    f = s.flows
    f.b.jobs, f.b.lakehouse_sessions = jobs, s.lakehouse_sessions
    f.b.spark_image, f.b.cairn_url, f.b.catalog_uri = spark_app_image, url, lake["catalog"]
    return f


SPEC = {"steps": [{"name": "build", "type": "spark", "application": "/opt/cairn-apps/app.py", "args": ["{{ params.target }}"],
                   "conf": {"spark.ui.enabled": "false", "spark.master": "local[2]"}, "timeout_s": 900}],
        "params": [{"name": "target", "type": "string", "default": "flow_prices"}]}


def test_a_spark_step_runs_as_the_owner_on_the_lakehouse(flows, lake, cairn):  # noqa: F811
    _, s = cairn
    flows.create(ANA, {"name": "spark-prices", **SPEC}, publish=True)
    run = flows.run(ANA, "spark-prices", wait=True, timeout_s=900)
    by = steps_of(run)
    assert run["status"] == "succeeded" and run["run_as"] == ANA, flows.step_log(ANA, run["id"], "build")["log"][-6000:]
    assert by["build"]["output"]["result"] == {"products": 3, "built": "flow_prices"}
    rows = lake["admin"].load_table("analytics.flow_prices").scan().to_arrow().to_pylist()
    assert sorted((r["sku"], r["price"]) for r in rows) == [("b", 14), ("c", 18)]
    # A lakehouse session was issued for ana (the step's), and nothing secret is in the step's log.
    issued = [e for e in s.audit.entries(category="lakehouse", limit=1000) if e["action"] == "lakehouse.token_issued"
              and e["actor"] == ANA]
    assert issued, "no lakehouse session for the owner"
    log = flows.step_log(ANA, run["id"], "build")["log"] + by["build"]["output"]["output"]
    assert "CAIRN_LAKEHOUSE_CREDENTIAL" not in log and issued[-1]["details"]["token_id"] not in log
    # Bob's flow: the same application, as bob, who may not see analytics. The catalog refuses Spark; the step fails.
    flows.create(BOB, {"name": "spark-bobs", **SPEC, "params": [{"name": "target", "type": "string",
                                                                 "default": "flow_prices_bob"}]}, publish=True)
    run = flows.run(BOB, "spark-bobs", wait=True, timeout_s=900)
    assert run["status"] == "failed" and run["run_as"] == BOB, run
    said = flows.step_log(BOB, run["id"], "build")["log"]
    assert "analytics" in said and ("Forbidden" in said or "do not let" in said or "NoSuchTable" in said
                                    or "TABLE_OR_VIEW_NOT_FOUND" in said), said[:3000] + "\n...\n" + said[-2000:]
    from pyiceberg.exceptions import NoSuchTableError

    with pytest.raises(NoSuchTableError):
        lake["admin"].load_table("analytics.flow_prices_bob")
    # No session for the owner (Cairn cannot issue one): the step fails before Spark starts.
    real = flows.b.lakehouse_sessions

    class NoSessions:
        def issue(self, actor, ttl):
            raise RuntimeError("the token key is not available")
    flows.b.lakehouse_sessions = NoSessions()
    try:
        run = flows.run(ANA, "spark-prices", wait=True, timeout_s=300)
    finally:
        flows.b.lakehouse_sessions = real
    assert run["status"] == "failed" and "no lakehouse session for the owner" in steps_of(run)["build"]["error"]
    # Spark not configured: refused, saying what to set.
    image, flows.b.spark_image = flows.b.spark_image, ""
    try:
        run = flows.run(ANA, "spark-prices", wait=True, timeout_s=300)
    finally:
        flows.b.spark_image = image
    assert run["status"] == "failed" and "CAIRN_FLOWS_SPARK_IMAGE" in steps_of(run)["build"]["error"]
