"""dbt on the lakehouse, end to end: ``cairn dbt build`` runs a real dbt project (dbt-core with dbt-trino)
on a real Trino, Cairn Catalog and Cairn Store, signed in as a pipeline user (``svc-dbt``) through a
Cairn token, under Cairn's policies; the run's docs, tags, owners, lineage and test results land in
Cairn on the datasets Trino names.

Needs docker, dbt with dbt-trino, and the store and catalog binaries."""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from test_trino_e2e import _ready, cairn, lake, trino  # noqa: F401  (fixtures)

def _dbt():
    try:
        import dbt.adapters.trino  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(not (_ready() and _dbt()), reason="needs docker, dbt-trino and the binaries")

PAT = "pat-svc-dbt-" + "x" * 20
IMAGE = os.environ.get("CAIRN_DBT_IMAGE")  # run the cairn/dbt image instead of this Python's dbt


def cairn_dbt(env, project_dir, *args):
    if not IMAGE:
        return subprocess.run([sys.executable, "-m", "cairn.cli", "dbt", *args, "--project-dir", str(project_dir)],
                              env=env, capture_output=True, text=True, timeout=900)
    os.chmod(project_dir, 0o777)
    keep = {k: v for k, v in env.items() if k.startswith("CAIRN_")}
    keep["CAIRN_CA_FILE"] = "/etc/cairn/ca.pem"
    extra = []
    if "CAIRN_DBT_TARGET_PATH" in keep:  # a host directory: mounted where the container writes its artifacts
        os.makedirs(keep["CAIRN_DBT_TARGET_PATH"], exist_ok=True)
        os.chmod(keep["CAIRN_DBT_TARGET_PATH"], 0o777)
        extra = ["-v", f"{keep['CAIRN_DBT_TARGET_PATH']}:/target"]
        keep["CAIRN_DBT_TARGET_PATH"] = "/target"
    return subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{project_dir}:/project", *extra,
                           "-v", f"{env['CAIRN_CA_FILE']}:/etc/cairn/ca.pem:ro",
                           *[a for k, v in keep.items() for a in ("-e", f"{k}={v}")], IMAGE, *args, "--project-dir", "/project"],
                          capture_output=True, text=True, timeout=900)


@pytest.fixture(scope="module")
def pipeline_user(cairn):  # noqa: F811
    """svc-dbt: a Cairn user with a personal access token, allowed to build in the analytics schema and
    read sales, and to record its runs."""
    from cairn.governance.abac import Policy, PolicyBundle, Target

    _, s = cairn
    real = s.identity._me_lookup
    # Cairn Core validates personal access tokens; this one is svc-dbt's.
    s.identity._me_lookup = lambda url, h, c: ("urn:li:corpuser:svc-dbt" if h.get("Authorization") == f"Bearer {PAT}"
                                              else real(url, h, c))
    me = {"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:svc-dbt"}
    bundle = s.pdp.bundle
    bundle = bundle() if callable(bundle) else bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies,
        Policy(id="dbt-build", effect="allow", condition=me,
               actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET"],
               target=Target(resource_urns=["*analytics.*"])),
        Policy(id="dbt-read", effect="allow", condition=me, actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"])),
        Policy(id="dbt-record", effect="allow", condition=me, actions=["EDIT_LINEAGE", "EDIT_ENTITY_ASSERTIONS"])]))
    yield "urn:li:corpuser:svc-dbt"
    s.identity._me_lookup = real


def project(d):
    os.makedirs(f"{d}/models", exist_ok=True)
    os.makedirs(f"{d}/seeds", exist_ok=True)
    open(f"{d}/dbt_project.yml", "w").write(textwrap.dedent("""\
        name: shop
        version: "1.0"
        profile: shop
        seeds:
          shop:
            +schema: null
        """))
    open(f"{d}/seeds/customers.csv", "w").write("id,name,region\n1,Jane,EU\n2,Bo,US\n3,Li,EU\n")
    open(f"{d}/models/sources.yml", "w").write(textwrap.dedent("""\
        version: 2
        sources:
          - name: sales
            database: lake
            schema: sales
            tables:
              - name: orders
        """))
    open(f"{d}/models/orders_by_region.sql", "w").write(
        "{{ config(materialized='table', tags=['finance']) }}\n"
        "select region, count(*) as orders from {{ source('sales', 'orders') }} group by region\n")
    open(f"{d}/models/customer_regions.sql", "w").write(
        "{{ config(materialized='view') }}\n"
        "select c.region, c.name, o.orders from {{ ref('customers') }} c join {{ ref('orders_by_region') }} o on c.region = o.region\n")
    open(f"{d}/models/schema.yml", "w").write(textwrap.dedent("""\
        version: 2
        models:
          - name: orders_by_region
            description: Orders per sales region.
            config:
              meta:
                owner: ana
            columns:
              - name: region
                description: The sales region (EU or US).
                data_tests: [not_null]
              - name: orders
          - name: customer_regions
            columns:
              - name: orders
                data_tests:
                  - accepted_values:
                      arguments:
                        values: [2]
                        quote: false
        """))


def test_dbt_builds_on_the_lakehouse_as_its_user_and_records_the_run(trino, cairn, pipeline_user, tmp_path):  # noqa: F811
    url, s = cairn
    trino("CREATE SCHEMA lake.analytics")
    project(tmp_path)
    env = {**os.environ, "CAIRN_URL": url, "CAIRN_TOKEN": PAT, "CAIRN_CA_FILE": f"{trino.dir}/ca.pem",
           "CAIRN_TRINO_HOST": "127.0.0.1", "CAIRN_TRINO_PORT": str(trino.tls), "CAIRN_TRINO_CATALOG": "lake",
           "CAIRN_DBT_SCHEMA": "analytics", "CAIRN_DBT_THREADS": "2"}
    out = cairn_dbt(env, tmp_path, "build")
    log = out.stdout + out.stderr
    # One test fails (US has one order, not two): dbt's own exit code, after recording the run.
    assert out.returncode == 1, log
    assert "running as svc-dbt" in log and "cairn dbt: recorded 3 models" in log, log
    assert "Completed with 1 error" in log or "ERROR=1" in log, log
    # The tables are on the lakehouse.
    assert trino("SELECT region, orders FROM analytics.orders_by_region ORDER BY region") == [
        {"region": "EU", "orders": 2}, {"region": "US", "orders": 1}]
    assert len(trino("SELECT * FROM analytics.customer_regions")) == 3
    # The Trino token never reached dbt's artifacts or the project.
    for root, _, files in os.walk(tmp_path):
        for f in files:
            if f.endswith((".json", ".yml", ".yaml")):
                assert "eyJ" not in open(os.path.join(root, f), errors="ignore").read(), f
    # Docs, tags, owners and lineage, on the datasets Trino names.
    model = s.trino_access.urn("lake", "analytics", "orders_by_region")
    props = s.graph.get_aspect(model, "datasetProperties")
    assert props["description"] == "Orders per sales region."
    assert props["customProperties"]["dbt.unique_id"] == "model.shop.orders_by_region"
    assert props["customProperties"]["dbt.materialized"] == "table"
    assert {t["tag"] for t in s.graph.get_aspect(model, "globalTags")["tags"]} == {"urn:li:tag:finance"}
    assert [o["owner"] for o in s.graph.get_aspect(model, "ownership")["owners"]] == ["urn:li:corpuser:ana"]
    ups = s.graph.get_aspect(model, "upstreamLineage")["upstreams"]
    assert [u["dataset"] for u in ups] == [s.trino_access.urn("lake", "sales", "orders")]
    view = s.trino_access.urn("lake", "analytics", "customer_regions")
    assert {u["dataset"] for u in s.graph.get_aspect(view, "upstreamLineage")["upstreams"]} == {
        s.trino_access.urn("lake", "analytics", "customers"), model}
    # Tests are checks on the model: not_null passes, accepted_values fails.
    from sqlalchemy import select

    from cairn.quality.external import external_checks
    with s.engine.connect() as c:
        checks = {(r.dataset, r.name.split("_")[0] + "_" + r.name.split("_")[1], r.status) for r in c.execute(select(external_checks))}
    assert (model, "not_null", "PASS") in checks and (view, "accepted_values", "FAIL") in checks, checks
    # The run is in the audit trail, as svc-dbt.
    entries = [e for e in s.audit.entries(category="pipelines") if e["action"] == "dbt.run_recorded"]
    assert entries and entries[0]["actor"] == pipeline_user  # newest first
    # A catalog sync rewrites the table's properties and schema from Trino, and keeps dbt's docs (Trino has
    # no comment for this table or column) and dbt's properties.
    from cairn.quality.collectors import ConnectionRegistry

    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    s.trino_catalog.sync("lakehouse", "urn:li:corpuser:admin")
    props = s.graph.get_aspect(model, "datasetProperties")
    assert props["description"] == "Orders per sales region." and props["customProperties"]["trino_table"] == "orders_by_region"
    assert props["customProperties"]["dbt.unique_id"] == "model.shop.orders_by_region"
    # Now that the schema is catalogued, the next run documents its columns; the next sync keeps them.
    again = cairn_dbt(env, tmp_path, "run", "--select", "orders_by_region")
    assert again.returncode == 0, again.stdout + again.stderr
    s.trino_catalog.sync("lakehouse", "urn:li:corpuser:admin")
    fields = {f["fieldPath"]: f for f in s.graph.get_aspect(model, "schemaMetadata")["fields"]}
    assert fields["region"].get("description") == "The sales region (EU or US)." and not fields["orders"].get("description")


def test_the_pipeline_user_is_held_to_its_policies(trino, cairn, pipeline_user):  # noqa: F811
    _, s = cairn
    token = s.trino_tokens.mint(pipeline_user)["token"]
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("CREATE TABLE lake.hr.copy AS SELECT * FROM lake.hr.salaries", user="svc-dbt", token=token)
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM lake.hr.salaries", user="svc-dbt", token=token)
    # Masks apply to it like to anyone (it is not the admin).
    rows = trino("SELECT email FROM lake.sales.orders ORDER BY id", user="svc-dbt", token=token)
    assert rows and all("•" in r["email"] for r in rows)


def test_a_run_cannot_be_recorded_without_the_rights(cairn):  # noqa: F811
    import httpx

    url, _ = cairn
    r = httpx.post(f"{url}/pipelines/dbt/runs", json={"manifest": {"nodes": {}}}, headers={"x-cairn-actor": "urn:li:corpuser:ana"})
    assert r.status_code == 403
    r = httpx.post(f"{url}/pipelines/dbt/runs", json={"nope": 1}, headers={"x-cairn-actor": "urn:li:corpuser:admin"})
    assert r.status_code == 422


def test_the_example_project_runs_and_its_docs_live_in_the_tables(trino, cairn, pipeline_user, tmp_path):  # noqa: F811
    """examples/dbt/lakehouse, as shipped: persist_docs writes the docs into the Iceberg tables, so Trino and
    the catalog crawler see them too."""
    url, s = cairn
    try:
        trino("CREATE SCHEMA lake.analytics")
    except RuntimeError:
        pass  # made by the first test
    proj = tmp_path / "lakehouse"
    shutil.copytree(os.path.join(os.path.dirname(__file__), "..", "examples", "dbt", "lakehouse"), proj)
    env = {**os.environ, "CAIRN_URL": url, "CAIRN_TOKEN": PAT, "CAIRN_CA_FILE": f"{trino.dir}/ca.pem",
           "CAIRN_TRINO_HOST": "127.0.0.1", "CAIRN_TRINO_PORT": str(trino.tls), "CAIRN_TRINO_CATALOG": "lake",
           "CAIRN_DBT_SCHEMA": "analytics", "CAIRN_DBT_TARGET_PATH": str(tmp_path / "target")}
    out = cairn_dbt(env, proj, "build")
    assert out.returncode == 0, out.stdout + out.stderr
    assert trino("SELECT name FROM analytics.region_names ORDER BY region") == [{"name": "Europe"}, {"name": "United States"}]
    comment = trino("SELECT comment FROM system.metadata.table_comments WHERE catalog_name = 'lake' "
                    "AND schema_name = 'analytics' AND table_name = 'region_names'")
    assert comment == [{"comment": "Sales regions and their names."}]
    cols = {r["Column"]: r["Comment"] for r in trino("SHOW COLUMNS FROM analytics.region_names")}
    assert cols == {"region": "The region's code.", "name": "The region's name."}
    model = s.trino_access.urn("lake", "analytics", "region_names")
    assert [o["owner"] for o in s.graph.get_aspect(model, "ownership")["owners"]] == ["urn:li:corpuser:analytics-team"]
    assert (tmp_path / "target" / "run_results.json").exists() and not (proj / "target").exists()
