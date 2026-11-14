"""Airflow and Cairn together, in the cairn/airflow image, against a real Cairn API, Trino, Cairn Catalog and
Cairn Store:

* a quality gate holds a DAG back until Cairn says its data is good;
* a failed task marks the tables it writes with an incident (and tells their owners), resolved by the
  next successful run;
* the dataset-change trigger fires when a table it watches is written;
* the secrets backend gives Airflow's own SQL operators a fresh Trino token as the pipeline user;
* a dbt project runs as one task per model;
* Cairn groups become Airflow team roles that see only their DAGs.

Needs docker, the cairn/airflow image (CAIRN_AIRFLOW_IMAGE) and the store and catalog binaries."""

import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from test_airflow_e2e import IMAGE, PAT, ROOT, airflow_user  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn, lake, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and IMAGE), reason="needs docker, CAIRN_AIRFLOW_IMAGE and the binaries")
DEV = os.environ.get("CAIRN_AIRFLOW_DEV_SRC") == "1"  # run the provider from the source tree (while developing)


def airflow(tmp_path, cairn_url, trino, script, dags=None, env=None, timeout=1200):  # noqa: F811
    d = tmp_path / "dags"
    d.mkdir(exist_ok=True)
    for name, text in (dags or {}).items():
        (d / f"{name}.py").write_text(text)
    project = tmp_path / "project"
    if not project.exists():
        shutil.copytree(os.path.join(ROOT, "examples", "dbt", "lakehouse"), project)
    for p in (d, project, tmp_path):
        os.chmod(p, 0o777)
    conn = {"conn_type": "cairn", "host": cairn_url, "password": PAT,
            "extra": {"trino_host": "127.0.0.1", "trino_port": trino.tls, "trino_catalog": "lake",
                      "ca_file": "/etc/cairn/ca.pem"}}
    transport = {"type": "http", "url": cairn_url, "endpoint": "openlineage/api/v1/lineage",
                 "auth": {"type": "api_key", "apiKey": PAT}}
    base = {"AIRFLOW_CONN_CAIRN_DEFAULT": json.dumps(conn), "AIRFLOW__OPENLINEAGE__TRANSPORT": json.dumps(transport),
            "CAIRN_DBT_SCHEMA": "analytics", "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags", "CAIRN_URL": cairn_url,
            **(env or {})}
    dev = ["-v", f"{ROOT}/airflow-provider/src:/opt/cairn-provider:ro", "-e", "PYTHONPATH=/opt/cairn-provider"] if DEV else []
    args = ["docker", "run", "--rm", "--network", "host", "-v", f"{d}:/opt/airflow/dags:ro", "-v", f"{project}:/project:ro",
            "-v", f"{tmp_path}:/work", "-v", f"{trino.dir}/ca.pem:/etc/cairn/ca.pem:ro", *dev,
            *[a for k, v in base.items() for a in ("-e", f"{k}={v}")], IMAGE, "bash", "-c", script]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def setup_schema(trino):  # noqa: F811
    for stmt in ("CREATE SCHEMA lake.analytics",
                 "CREATE TABLE lake.analytics.daily_orders AS SELECT region, count(*) AS orders FROM lake.sales.orders GROUP BY region"):
        try:
            trino(stmt)
        except RuntimeError:
            pass


GATE = '''
from datetime import datetime
from airflow.sdk import DAG
from cairn_airflow.quality import CairnQualityGateOperator

with DAG("gate", start_date=datetime(2026, 1, 1), schedule=None, catchup=False):
    CairnQualityGateOperator(task_id="gate", datasets=["lake.analytics.daily_orders"])
'''

LOAD = '''
from datetime import datetime
from airflow.sdk import DAG
from cairn_airflow.operators import CairnTrinoOperator

with DAG("load", start_date=datetime(2026, 1, 1), schedule=None, catchup=False):
    CairnTrinoOperator(task_id="refresh", schema="analytics", sql="INSERT INTO daily_orders {select}")
'''

WATCH = '''
import asyncio, json, sys
from cairn_airflow.triggers import CairnDatasetChangedTrigger
from airflow.sdk import DAG, Asset, AssetWatcher

# An asset that Airflow's event-driven scheduling accepts.
orders = Asset("lake.analytics.daily_orders", watchers=[AssetWatcher(
    name="orders_changed", trigger=CairnDatasetChangedTrigger(["lake.analytics.daily_orders"], poll_interval=5))])

async def main():
    trig = CairnDatasetChangedTrigger(["lake.analytics.daily_orders"], poll_interval=5)
    print("WATCHING", flush=True)
    async for ev in trig.run():
        print("FIRED " + json.dumps(ev.payload), flush=True)
        return
asyncio.run(asyncio.wait_for(main(), 120))
'''

SECRETS_DAG = '''
from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

with DAG("plain_sql", start_date=datetime(2026, 1, 1), schedule=None, catchup=False):
    SQLExecuteQueryOperator(task_id="count", conn_id="trino_lakehouse", sql="{sql}", show_return_value_in_logs=True)
'''


def test_gates_failures_triggers_and_plain_operators(trino, cairn, airflow_user, tmp_path):  # noqa: F811
    url, s = cairn
    setup_schema(trino)
    orders = s.trino_access.urn("lake", "analytics", "daily_orders")

    # 1. A quality gate: refused while a check fails or nothing checks the table, passes when it is good.
    s.external_quality.ingest("generic", {"results": [{"dataset": orders, "check": "row_count", "status": "FAIL",
                                                        "message": "0 rows"}]})
    code, log = airflow(tmp_path, url, trino, "airflow db migrate >/dev/null && airflow dags test gate 2026-11-01",
                        {"gate": GATE})
    assert code != 0 and "Cairn quality gate" in log and "FAIL (row_count)" in log, log[-4000:]
    s.external_quality.ingest("generic", {"results": [{"dataset": orders, "check": "row_count", "status": "PASS"}]})
    code, log = airflow(tmp_path, url, trino, "airflow db migrate >/dev/null && airflow dags test gate 2026-11-01")
    assert code == 0 and "Cairn quality gate passed" in log, log[-4000:]

    # 2. A failed task: an incident on the table it writes, owners told; resolved by the next good run.
    from cairn.pipelines.changes import _incident_urn
    job = "urn:li:dataJob:(urn:li:dataFlow:(airflow,load,PROD),refresh)"
    code, log = airflow(tmp_path, url, trino, "airflow db migrate >/dev/null && airflow dags test load 2026-11-01",
                        {"load": LOAD.replace("{select}", "SELECT name, salary FROM hr.salaries")})
    assert code != 0 and "Access Denied" in log, log[-4000:]
    end = time.time() + 30
    while time.time() < end and not s.graph.get_aspect(_incident_urn(job, orders), "incidentInfo"):
        time.sleep(0.5)
    inc = s.graph.get_aspect(_incident_urn(job, orders), "incidentInfo")
    assert inc and inc["status"]["state"] == "ACTIVE" and orders in inc["entities"], \
        sorted(u for (u, a) in s.graph.aspects if a == "incidentInfo")
    head = s.dataset_changes.head()
    code, log = airflow(tmp_path, url, trino, "airflow db migrate >/dev/null && airflow dags test load 2026-11-01",
                        {"load": LOAD.replace("{select}", "SELECT region, count(*) FROM sales.orders GROUP BY region")})
    assert code == 0, log[-4000:]
    end = time.time() + 30
    while time.time() < end and s.graph.get_aspect(_incident_urn(job, orders), "incidentInfo")["status"]["state"] != "RESOLVED":
        time.sleep(0.5)
    assert s.graph.get_aspect(_incident_urn(job, orders), "incidentInfo")["status"]["state"] == "RESOLVED"
    # ... and the good run is a change to the table (Airflow's lineage and Trino's).
    assert s.dataset_changes.since([orders], head), "no change recorded"

    # 3. The trigger fires when the table is written, as the connection's user (who may see it).
    (tmp_path / "watch.py").write_text(WATCH)
    result = {}

    def watch():
        result["code"], result["log"] = airflow(tmp_path, url, trino, "python /work/watch.py", timeout=300)
    t = threading.Thread(target=watch)
    t.start()
    time.sleep(25)  # the container is up and the trigger has its cursor
    s.dataset_changes.record([orders], "openlineage", "urn:li:dataJob:(urn:li:dataFlow:(spark,x,PROD),y)", "r9")
    t.join(240)
    assert result.get("code") == 0 and "FIRED" in result["log"], result.get("log", "")[-4000:]
    fired = json.loads(result["log"].split("FIRED ", 1)[1].splitlines()[0])
    assert fired["datasets"] == [orders] and fired["changes"][-1]["run"] == "r9"

    # 4. Airflow's own SQL operator through the secrets backend: a fresh Trino token as svc-airflow.
    backend = {"AIRFLOW__SECRETS__BACKEND": "cairn_airflow.secrets.CairnSecretsBackend",
               "AIRFLOW__SECRETS__BACKEND_KWARGS": json.dumps({
                   "cairn_url": url, "token_env": "CAIRN_TOKEN", "ca_file": "/etc/cairn/ca.pem",
                   "trino_host": "127.0.0.1", "trino_port": trino.tls, "trino_catalog": "lake"}),
               "CAIRN_TOKEN": PAT}
    code, log = airflow(tmp_path, url, trino, "airflow db migrate >/dev/null && airflow dags test plain_sql 2026-11-01",
                        {"plain_sql": SECRETS_DAG.replace("{sql}", "SELECT count(*) FROM lake.sales.orders")}, env=backend)
    assert code == 0 and "[(3,)]" in log.replace(" ", "").replace("[[3]]", "[(3,)]"), log[-4000:]
    code, log = airflow(tmp_path, url, trino, "airflow db migrate >/dev/null && airflow dags test plain_sql 2026-11-01",
                        {"plain_sql": SECRETS_DAG.replace("{sql}", "SELECT * FROM lake.hr.salaries")}, env=backend)
    assert code != 0 and "Access Denied" in log, log[-4000:]


DBT_DAG = '''
from datetime import datetime
from airflow.sdk import DAG
from cairn_airflow.dbt import CairnDbtTaskGroup

with DAG("dbt_by_model", start_date=datetime(2026, 1, 1), schedule=None, catchup=False):
    CairnDbtTaskGroup(group_id="lakehouse", project_dir="/project", manifest_path="/work/target/manifest.json")
'''


def test_a_dbt_project_runs_as_one_task_per_model(trino, cairn, airflow_user, tmp_path):  # noqa: F811
    url, s = cairn
    setup_schema(trino)
    parse = ("CAIRN_DBT_TARGET_PATH=/work/target DBT_LOG_PATH=/tmp/l DBT_PACKAGES_INSTALL_PATH=/tmp/p "
             "CAIRN_TOKEN=$(python -c \"import json,os; print(json.loads(os.environ['AIRFLOW_CONN_CAIRN_DEFAULT'])['password'])\") "
             "CAIRN_TRINO_HOST=127.0.0.1 CAIRN_TRINO_PORT=" + str(trino.tls) + " CAIRN_TRINO_CATALOG=lake CAIRN_CA_FILE=/etc/cairn/ca.pem "
             "/opt/cairn-dbt/bin/cairn dbt parse --project-dir /project")
    code, log = airflow(tmp_path, url, trino, f"airflow db migrate >/dev/null && {parse} && airflow dags test dbt_by_model 2026-11-01",
                        {"dbt_by_model": DBT_DAG})
    assert code == 0, log[-6000:]
    manifest = json.loads((tmp_path / "target" / "manifest.json").read_text())
    models = sorted(n["name"] for u, n in manifest["nodes"].items() if u.split(".")[0] in ("model", "seed", "snapshot"))
    for name in models:  # each node ran as its own task, and each run was recorded in Cairn
        assert f"lakehouse.{name}" in log, (name, log[-3000:])
    assert log.count("cairn dbt: recorded") >= len(models), log[-3000:]
    model = s.trino_access.urn("lake", "analytics", "region_names")
    assert s.graph.get_aspect(model, "datasetProperties")["customProperties"]["dbt.unique_id"].startswith("model.")


TEAMS = '''
import os
os.environ.setdefault("CAIRN_AIRFLOW_TEAM_ROLES", "true")
from airflow.providers.fab.www.app import create_app
app = create_app(enable_plugins=False)  # with the deployment's webserver_config.py (sign-in through Cairn)
with app.app_context():
    sm = app.appbuilder.sm
    print("SM", type(sm).__name__)
    # The role calculation sign-in runs, for someone in Cairn's group finance.
    roles = sm._oauth_calculate_user_roles({"username": "fin", "role_keys": ["finance"]})
    names = sorted(r.name for r in roles)
    print("ROLES", names)
    member = sm.find_role("CairnMember")
    print("MEMBER_DAGS", any(p.resource.name == "DAGs" for p in member.permissions))
    print("MEMBER_RUNS", any(p.resource.name == "DAG Runs" and p.action.name == "can_read" for p in member.permissions))
    user = sm.add_user("fin", "Fin", "Ance", "fin@example.com", roles, password="x" * 20)
    from airflow.models.dagbag import DagBag
    bag = DagBag("/opt/airflow/dags")
    for dag in bag.dags.values():
        sm.sync_perm_for_dag(dag.dag_id, dag.access_control)
    from airflow.providers.fab.auth_manager.fab_auth_manager import FabAuthManager
    am = FabAuthManager()
    am.appbuilder = app.appbuilder
    from airflow.api_fastapi.auth.managers.models.resource_details import DagDetails
    for dag_id in ("finance_daily", "hr_daily"):
        print("CAN", dag_id, am.is_authorized_dag(method="GET", details=DagDetails(id=dag_id), user=user))
'''

TEAM_DAGS = {
    "finance_daily": '''
from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
with DAG("finance_daily", start_date=datetime(2026, 1, 1), schedule=None,
         access_control={"team:finance": {"DAGs": {"can_read", "can_edit"}}}):
    EmptyOperator(task_id="x")
''',
    "hr_daily": '''
from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
with DAG("hr_daily", start_date=datetime(2026, 1, 1), schedule=None, access_control={"team:hr": {"DAGs": {"can_read"}}}):
    EmptyOperator(task_id="x")
'''}


def test_cairn_groups_become_team_roles_that_see_only_their_dags(trino, cairn, tmp_path):  # noqa: F811
    url, _ = cairn
    (tmp_path / "teams.py").write_text(TEAMS)
    shutil.copy(os.path.join(ROOT, "deploy", "airflow", "webserver_config.py"), tmp_path / "webserver_config.py")
    code, log = airflow(tmp_path, url, trino,
                        "cp /work/webserver_config.py /opt/airflow/webserver_config.py && airflow db migrate >/dev/null "
                        "&& python /work/teams.py", TEAM_DAGS,
                        env={"CAIRN_AIRFLOW_TEAM_ROLES": "true", "CAIRN_PUBLIC_URL": "https://cairn.example.com",
                             "CAIRN_AIRFLOW_OAUTH_SECRET": "s" * 32, "CAIRN_AIRFLOW_ROLES": "cairn-admins=Admin"})
    assert code == 0, log[-6000:]
    assert "SM CairnSecurityManager" in log, log[-3000:]
    assert "ROLES ['CairnMember', 'Public', 'team:finance']" in log, log[-3000:]
    assert "MEMBER_DAGS False" in log and "MEMBER_RUNS True" in log, log[-3000:]
    assert "CAN finance_daily True" in log and "CAN hr_daily False" in log, log[-3000:]


def test_lineage_reaches_cairn_through_the_token_provider(trino, cairn, airflow_user, tmp_path):  # noqa: F811
    url, s = cairn
    setup_schema(trino)
    transport = {"type": "http", "url": url, "endpoint": "openlineage/api/v1/lineage",
                 "auth": {"type": "cairn_airflow.lineage.CairnTokenProvider", "token_env": "CAIRN_LINEAGE_TOKEN"}}
    dag = LOAD.replace('"load"', '"lineage_via_provider"').replace(
        "{select}", "SELECT region, count(*) FROM sales.orders GROUP BY region")
    code, log = airflow(tmp_path, url, trino, "airflow db migrate >/dev/null && airflow dags test lineage_via_provider 2026-11-01",
                        {"lineage_via_provider": dag},
                        env={"AIRFLOW__OPENLINEAGE__TRANSPORT": json.dumps(transport), "CAIRN_LINEAGE_TOKEN": PAT})
    assert code == 0, log[-4000:]
    job = "urn:li:dataJob:(urn:li:dataFlow:(airflow,lineage_via_provider,PROD),refresh)"
    end = time.time() + 30
    while time.time() < end and not s.graph.get_aspect(job, "dataJobInputOutput"):
        time.sleep(0.5)
    io = s.graph.get_aspect(job, "dataJobInputOutput")
    # The task's own lineage (from its SQL), named as the catalog names the tables.
    assert io and s.trino_access.urn("lake", "analytics", "daily_orders") in io["outputDatasets"], io
    assert s.trino_access.urn("lake", "sales", "orders") in io["inputDatasets"], io
