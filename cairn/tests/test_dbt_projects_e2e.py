"""dbt developed and run in Cairn (L172), end to end with the real dbt (dbt-core with dbt-trino) on a real Trino,
Cairn Catalog and Cairn Store: a project's files are kept and edited in Cairn; dbt parses, compiles and builds them
on Trino as the person who asks; a model is previewed through the governed query path without being built; a
state-aware run builds only what changed; and a person's policies hold in all of it.

Needs docker, the store and catalog binaries, and dbt with dbt-trino (CAIRN_DBT_BIN, or dbt beside this Python)."""

import json
import shutil

import httpx
import pytest

from cairn.pipelines.dbt_projects import dbt_command
from test_trino_e2e import _ready, cairn, lake, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and shutil.which("docker") and dbt_command()),
                                reason="needs docker, the binaries and dbt with dbt-trino")

FILES = {
    "dbt_project.yml": 'name: shop\nversion: "1.0"\nprofile: shop\nmodel-paths: ["models"]\n'
                       "models:\n  shop:\n    +materialized: table\n",
    "models/sources.yml": "version: 2\nsources:\n  - name: sales\n    database: lake\n    schema: sales\n"
                          "    tables:\n      - name: orders\n",
    "models/staging/stg_orders.sql": "{{ config(materialized='ephemeral') }}\n"
                                     "select id, email, region from {{ source('sales', 'orders') }}\n",
    "models/orders_by_region.sql": "select region, count(*) as orders from {{ ref('stg_orders') }} group by region\n",
    "models/regions.sql": "select distinct region from {{ ref('stg_orders') }}\n",
    "models/schema.yml": "version: 2\nmodels:\n  - name: orders_by_region\n    description: Orders per region\n"
                         "    columns:\n      - name: region\n        data_tests: [not_null, unique]\n",
}


def test_a_dbt_project_is_edited_compiled_previewed_and_built_as_the_person(trino, cairn):  # noqa: F811
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    if s.dbt_runs.engine is None:
        s.dbt_runs.engine = s.engine
    admin, ana, bob = ({"X-Cairn-Actor": f"urn:li:corpuser:{u}"} for u in ("admin", "ana", "bob"))
    api = lambda who, method, path="", **kw: httpx.request(method, f"{url}/v1/dbt/projects{path}", headers=who,  # noqa: E731
                                                           timeout=900, **kw)

    # The project and its files, kept in Cairn. No profile, nothing outside the project, nothing of dbt's own output.
    assert api(ana, "POST", json={"name": "shop", "files": FILES}).status_code == 403            # needs MANAGE_INGESTION
    r = api(admin, "POST", json={"name": "shop", "description": "the shop", "files": FILES})
    assert r.status_code == 201 and [f["path"] for f in r.json()["files"]] == sorted(FILES), r.text
    assert api(admin, "POST", json={"name": "shop"}).status_code == 409
    assert api(bob, "GET", "/shop").status_code == 404 and api(bob, "GET").json() == []
    for bad in ("profiles.yml", "models/../../etc/passwd.sql", "target/run.sql", "models/x.exe", "models//x.sql"):
        assert api(admin, "PUT", f"/shop/files/{bad}", json={"content": "select 1"}).status_code in (404, 422), bad
    assert api(admin, "GET", "/shop/files/models/regions.sql").json()["content"] == FILES["models/regions.sql"]
    assert api(admin, "DELETE", "/shop/files/dbt_project.yml").status_code == 422

    # An environment: one of Cairn's Trino connections and a schema.
    assert api(admin, "PUT", "/shop/environments/dev", json={"connection": "nowhere", "schema": "dbt_dev"}).status_code == 422
    r = api(admin, "PUT", "/shop/environments/dev", json={"connection": "lakehouse", "schema": "dbt_dev", "threads": 2})
    assert r.status_code == 200 and r.json() == {"project": "shop", "name": "dev", "connection": "lakehouse",
                                                 "schema": "dbt_dev", "threads": 2}, r.text
    run = lambda who, **body: api(who, "POST", "/shop/commands", json={"environment": "dev", **body})  # noqa: E731
    assert run(admin, command="parse", environment="prod").status_code == 404
    assert run(admin, command="rm -rf").status_code == 422
    assert run(admin, command="run", select="--profiles-dir /tmp").status_code == 422

    # dbt itself parses and compiles the files.
    parsed = run(admin, command="parse")
    assert parsed.status_code == 200 and parsed.json()["ok"], parsed.text
    compiled = run(admin, command="compile").json()
    assert compiled["ok"] and '"lake"."sales"."orders"' in compiled["compiled"]["models/orders_by_region.sql"], compiled["log"]

    # Preview: a model's rows before anything is built, through Cairn's query path.
    pv = api(admin, "POST", "/shop/preview", json={"environment": "dev", "model": "orders_by_region"})
    assert pv.status_code == 200, pv.text
    assert sorted(map(tuple, pv.json()["rows"])) == [("EU", 2), ("US", 1)]
    assert not trino("SHOW SCHEMAS FROM lake LIKE 'dbt_dev'")                              # nothing was built
    assert api(admin, "POST", "/shop/preview", json={"environment": "dev", "model": "no_such"}).status_code == 422

    # Build: the models and their tests, on Trino as the admin; recorded as a dbt run (docs and lineage included).
    built = run(admin, command="build").json()
    assert built["ok"], built["log"]
    status = {x["unique_id"]: x["status"] for x in built["results"]}
    assert status["model.shop.orders_by_region"] == "success" and status["model.shop.regions"] == "success"
    assert sum(1 for k, v in status.items() if k.startswith("test.") and v == "pass") == 2
    assert built["recorded"] and built["recorded"]["models"] == 2
    assert trino("SELECT region, orders FROM lake.dbt_dev.orders_by_region ORDER BY region") == [
        {"region": "EU", "orders": 2}, {"region": "US", "orders": 1}]
    model = s.trino_access.urn("lake", "dbt_dev", "orders_by_region")
    assert (s.graph.get_aspect(model, "editableDatasetProperties") or s.graph.get_aspect(model, "datasetProperties")) is not None
    # The Trino token Cairn minted for the run is in no answer.
    assert "eyJ" not in json.dumps(built) and "eyJ" not in json.dumps(compiled)
    # A query over built models, compiled by dbt.
    pv = api(admin, "POST", "/shop/preview", json={"environment": "dev",
                                                   "sql": "select sum(orders) as n from {{ ref('orders_by_region') }}"})
    assert pv.status_code == 200 and pv.json()["rows"] == [[3]], pv.text

    # State-aware: nothing changed, nothing runs; one model edited, that model alone (and what depends on it).
    again = run(admin, command="build", state_aware=True).json()
    assert again["ok"] and again["state_aware"] and again["results"] == [], again
    edited = "select region, count(*) as orders, max(id) as last_id from {{ ref('stg_orders') }} group by region\n"
    assert api(admin, "PUT", "/shop/files/models/orders_by_region.sql", json={"content": edited}).status_code == 200
    changed = run(admin, command="build", state_aware=True).json()
    ran = {x["unique_id"] for x in changed["results"]}
    assert changed["ok"] and "model.shop.orders_by_region" in ran and "model.shop.regions" not in ran, changed
    assert trino("SELECT last_id FROM lake.dbt_dev.orders_by_region WHERE region = 'EU'") == [{"last_id": 3}]
    assert run(admin, command="build", state_aware=True, select="regions").status_code == 422

    # A pull request's build: its files laid over the project (not saved), built in a schema of its own against the
    # environment's last run, so only what it changes is built; the schema is dropped whatever happens.
    ci = lambda **body: api(admin, "POST", "/shop/ci", json={"environment": "dev", **body})  # noqa: E731
    flagged = "select distinct region, 'x' as flag from {{ ref('stg_orders') }}\n"
    pr = ci(label="pr_42", files={"models/regions.sql": flagged})
    assert pr.status_code == 200, pr.text
    pr = pr.json()
    assert pr["ok"] and pr["compared_with_state"] and pr["schema"].startswith("dbt_dev_ci_pr_42_") and pr["schema_dropped"]
    assert [x["unique_id"] for x in pr["results"]] == ["model.shop.regions"], pr["results"]
    assert "### dbt build for `pr_42`: passed" in pr["report"] and "`model.shop.regions`" in pr["report"]
    assert not trino("SHOW SCHEMAS FROM lake LIKE 'dbt_dev_ci%'")                          # dropped
    assert [c["Column"] for c in trino("SHOW COLUMNS FROM lake.dbt_dev.regions")] == ["region"]   # the environment is untouched
    assert api(admin, "GET", "/shop/files/models/regions.sql").json()["content"] == FILES["models/regions.sql"]
    # A change that breaks a test fails the build, says which, and still leaves nothing behind.
    strict = FILES["models/schema.yml"].replace("data_tests: [not_null, unique]",
                                                "data_tests:\n          - accepted_values:\n              values: ['EU']")
    bad = ci(label="pr_43", files={"models/schema.yml": strict}).json()
    assert not bad["ok"] and bad["schema_dropped"] and "failed" in bad["report"].splitlines()[0]
    assert any(x["status"] == "fail" and "accepted_values" in x["unique_id"] for x in bad["results"]), bad["results"]
    assert not trino("SHOW SCHEMAS FROM lake LIKE 'dbt_dev_ci%'")
    same = ci(label="pr_44").json()
    assert same["ok"] and same["results"] == [] and "Nothing in this change" in same["report"]
    assert ci(label="Bad Label!").status_code == 422 and ci(files={"profiles.yml": "x"}).status_code == 422
    assert "eyJ" not in json.dumps([pr, bad])

    # Lint: every model's compiled SQL, and its documentation.
    for path, sql in (("models/messy.sql", "select * from {{ ref('orders_by_region') }} a, {{ ref('regions') }} b order by 1\n"),
                      ("models/unnamed.sql", "select region, count(*) from {{ ref('stg_orders') }} group by 1\n")):
        assert api(admin, "PUT", f"/shop/files/{path}", json={"content": sql}).status_code == 200
    lint = api(admin, "POST", "/shop/lint", json={"environment": "dev"})
    assert lint.status_code == 200, lint.text
    found = {(f["model"], f["rule"]) for f in lint.json()["findings"]}
    assert {("messy", "S002"), ("messy", "S003"), ("messy", "S004"), ("unnamed", "S005"), ("regions", "D001"),
            ("regions", "D002")} <= found, found
    assert not [f for f in found if f[0] == "orders_by_region"] and ("stg_orders", "D002") not in found
    assert lint.json()["ok"] and lint.json()["summary"]["error"] == 0 and lint.json()["models"] == 5
    for path in ("models/messy.sql", "models/unnamed.sql"):
        assert api(admin, "DELETE", f"/shop/files/{path}").status_code == 204

    # As the person: Ana's own project. Her preview is masked as her queries are, and dbt cannot build where she
    # may not write: the run fails on Trino's refusal and nothing is made.
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="ana-dbt", effect="allow", actions=["MANAGE_INGESTION"],
        condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ana"})]))
    try:
        mine = {**FILES, "dbt_project.yml": FILES["dbt_project.yml"].replace("shop", "anas")}
        assert api(ana, "POST", json={"name": "anas", "files": mine}).status_code == 201
        assert api(ana, "PUT", "/anas/environments/dev", json={"connection": "lakehouse", "schema": "dbt_ana"}).status_code == 200
        assert api(ana, "GET", "/shop").status_code == 404                                   # the admin's is not hers
        pv = httpx.post(f"{url}/v1/dbt/projects/anas/preview", headers=ana, timeout=900, json={
            "environment": "dev", "sql": "select email from {{ source('sales', 'orders') }} order by id"})
        assert pv.status_code == 200, pv.text
        emails = [row[0] for row in pv.json()["rows"]]
        assert emails == [x["email"] for x in trino("SELECT email FROM lake.sales.orders ORDER BY id", user="ana")]
        assert "jane@acme.com" not in emails
        hers = httpx.post(f"{url}/v1/dbt/projects/anas/commands", headers=ana, timeout=900,
                          json={"environment": "dev", "command": "run"}).json()
        assert not hers["ok"] and "Access Denied" in hers["log"], hers["log"]
        assert not trino("SHOW SCHEMAS FROM lake LIKE 'dbt_ana'")
    finally:
        s.pdp.set_bundle(b)

    # Audited (commands and counts, not SQL), and deleted with its files and environments.
    acts = {e["action"] for e in s.audit.entries("pipelines", limit=100)}
    assert {"dbt_project.created", "dbt_project.environment_set", "dbt_project.command"} <= acts
    assert api(bob, "DELETE", "/shop").status_code == 404
    assert api(admin, "DELETE", "/shop").status_code == 204 and api(admin, "GET", "/shop").status_code == 404
