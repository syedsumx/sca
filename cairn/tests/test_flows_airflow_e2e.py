"""Cairn Flows and Airflow both ways (L124), end to end with a real Airflow 3 (the cairn/airflow image, its API server,
scheduler, DAG processor and executor) and Cairn's real API:

* a flow exported as a DAG file loads in Airflow with no import errors (DagBag), with its tasks and order;
* a flow with ``runner: airflow`` is deployed into Airflow's DAG folder on publish; a run started in Cairn is carried
  out by Airflow (started and followed over its REST API), each task runs its step in Cairn as the flow's owner (the
  Airflow connection's user is only an editor), the branch's other side is skipped, and Cairn's run history has every
  step's status;
* Airflow's DAGs are listed and shown in Cairn to the people they belong to (owners, team tags, their flows), with
  live task states; a DAG of someone else's is not there;
* a DAG of provider operators converts to a draft flow; one with an operator that has no step is refused, naming it.

Needs docker, the cairn/airflow image (CAIRN_AIRFLOW_IMAGE; CAIRN_AIRFLOW_DEV_SRC=1 mounts the provider from this tree)
and the store and catalog binaries (the Cairn API fixture of tests/test_trino_e2e.py)."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time

import httpx
import pytest

from test_airflow_e2e import IMAGE, PAT, ROOT, USER, airflow_user  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and IMAGE), reason="needs docker, CAIRN_AIRFLOW_IMAGE and the binaries")
DEV = os.environ.get("CAIRN_AIRFLOW_DEV_SRC") == "1"
# While developing on a slim Airflow image: a directory of extra packages (e.g. the trino client) on its path.
DEV_DEPS = os.environ.get("CAIRN_AIRFLOW_DEV_DEPS", "")
# git-sync, as Airflow's official chart runs it beside the DAG processor (pinned; registry.k8s.io).
GIT_SYNC = os.environ.get("CAIRN_GIT_SYNC_IMAGE", "registry.k8s.io/git-sync/git-sync:v4.8.0")
ADMIN, ANA, BOB, CARL = ("urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob",
                         "urn:li:corpuser:carl")

ORDERS_DAILY = '''
from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
from cairn_airflow.operators import CairnDbtOperator, CairnTrinoOperator

with DAG("orders_daily", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False,
         is_paused_upon_creation=True, default_args={"owner": "bob"}, description="Orders, daily"):
    start = EmptyOperator(task_id="start")
    load = CairnTrinoOperator(task_id="load", sql="INSERT INTO marts.daily SELECT * FROM raw.orders", retries=2)
    build = CairnDbtOperator(task_id="build", project_dir="/dbt", command="build", select="tag:daily")
    start >> load >> build
'''
FINANCE = '''
from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.python import PythonOperator

def report():
    return 1

with DAG("finance_report", start_date=datetime(2026, 1, 1), schedule=None, tags=["team:finance"],
         is_paused_upon_creation=True, default_args={"owner": "airflow"}):
    PythonOperator(task_id="crunch", python_callable=report)
'''
TARGETS = '''
from datetime import datetime
from airflow.sdk import DAG
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.operators.python import PythonOperator

def broken():
    raise RuntimeError("the source is not there")

with DAG("af_target", start_date=datetime(2026, 1, 1), schedule=None, is_paused_upon_creation=False,
         default_args={"owner": "airflow"}):
    EmptyOperator(task_id="done")

with DAG("af_fails", start_date=datetime(2026, 1, 1), schedule=None, is_paused_upon_creation=False,
         default_args={"owner": "airflow"}):
    PythonOperator(task_id="load", python_callable=broken)
'''
HELLO = {"kind": "flow", "name": "af-hello", "description": "Airflow carries it out", "runner": "airflow",
         "params": [{"name": "n", "type": "int", "default": 2}],
         "steps": [{"name": "hello", "type": "notify", "message": "hello {{ params.n }}"},
                   {"name": "pick", "type": "branch", "after": ["hello"], "condition": "params.n > 1",
                    "then": ["big"], "else": ["small"]},
                   {"name": "big", "type": "wait", "after": ["pick"], "seconds": 1},
                   {"name": "small", "type": "notify", "after": ["pick"], "message": "small"},
                   {"name": "kick", "type": "airflow", "after": ["hello"], "dag_id": "finance_report",
                    "conf": {"why": "a report"}, "wait": False}]}


@pytest.fixture(scope="module")
def airflow(cairn, trino, airflow_user, tmp_path_factory):  # noqa: F811
    """A real Airflow (API server, scheduler, DAG processor, executor) whose ``cairn`` connection signs in to Cairn as
    svc-airflow, and Cairn set up to deploy DAGs into its folder and to call its REST API."""
    from cairn.flows.airflow_interop import DagFolder

    url, s = cairn
    work = tmp_path_factory.mktemp("airflow")
    dags = work / "dags"
    dags.mkdir()
    for p in (work, dags):
        os.chmod(p, 0o777)
    (dags / "orders_daily.py").write_text(ORDERS_DAILY)
    (dags / "finance_report.py").write_text(FINANCE)
    (dags / "af_targets.py").write_text(TARGETS)
    project = work / "project"                              # a dbt project for the exported DAG's dbt task
    shutil.copytree(os.path.join(ROOT, "examples", "dbt", "lakehouse"), project)
    os.chmod(project, 0o755)
    gitsync = dags / "gitsync"                              # git-sync's root (its own user writes it)
    gitsync.mkdir()
    os.chmod(gitsync, 0o777)
    (dags / ".airflowignore").write_text(".worktrees/\n.git/\n")   # only git-sync's published link is read
    password = secrets.token_urlsafe(24)
    (work / "passwords.json").write_text(json.dumps({"cairn": password}))
    os.chmod(work / "passwords.json", 0o666)
    (work / "cairn-password").write_text(password)          # Cairn reads its Airflow password from a file
    ap = port()
    conn = {"conn_type": "cairn", "host": url, "password": PAT,       # native tasks: Trino as svc-airflow
            "extra": {"trino_host": "127.0.0.1", "trino_port": trino.tls, "trino_catalog": "lake",
                      "ca_file": "/etc/cairn/ca.pem"}}
    env = {"AIRFLOW_CONN_CAIRN_DEFAULT": json.dumps(conn),
           "AIRFLOW__CORE__AUTH_MANAGER": "airflow.api_fastapi.auth.managers.simple.simple_auth_manager.SimpleAuthManager",
           "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "cairn:admin",
           "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": "/work/passwords.json",
           "AIRFLOW__API__PORT": str(ap), "AIRFLOW__API__BASE_URL": f"http://127.0.0.1:{ap}",
           "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": f"http://127.0.0.1:{ap}/execution/",
           "AIRFLOW__API_AUTH__JWT_SECRET": secrets.token_hex(32), "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
           "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags", "AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL": "5",
           "CAIRN_DBT_SCHEMA": "analytics",
           "AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL": "5", "AIRFLOW__SCHEDULER__TASK_QUEUED_TIMEOUT": "600"}
    path = ["/opt/cairn-provider"] if DEV else []
    dev = ["-v", f"{ROOT}/airflow-provider/src:/opt/cairn-provider:ro"] if DEV else []
    if DEV_DEPS:
        dev += ["-v", f"{DEV_DEPS}:/opt/cairn-deps:ro"]
        path.append("/opt/cairn-deps")
    if path:
        dev += ["-e", "PYTHONPATH=" + ":".join(path)]
    dev += ["-v", f"{trino.dir}/ca.pem:/etc/cairn/ca.pem:ro"]
    name = f"cairn-flows-airflow-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host", "-v", f"{work}:/work",
                    "-v", f"{dags}:/opt/airflow/dags:ro", "-v", f"{project}:/project:ro", *dev,
                    *[a for k, v in env.items() for a in ("-e", f"{k}={v}")],
                    IMAGE, "bash", "-c", "airflow standalone"], check=True, capture_output=True, timeout=120)
    base = f"http://127.0.0.1:{ap}"
    end = time.time() + 300
    while True:
        try:
            h = httpx.get(f"{base}/api/v2/monitor/health", timeout=5).json()
            if all((h.get(k) or {}).get("status") == "healthy" for k in ("scheduler", "dag_processor", "metadatabase")):
                break
        except (httpx.HTTPError, ValueError):
            pass
        if time.time() > end:
            logs = subprocess.run(["docker", "logs", "--tail", "80", name], capture_output=True, text=True)
            raise AssertionError(logs.stdout[-4000:] + logs.stderr[-4000:])
        time.sleep(3)
    s.flows.b.airflow.update(url=base, api="v2", username="cairn", password_file=str(work / "cairn-password"))
    s.flows.dag_folder = DagFolder(f"path:{dags}")
    s.flows.airflow_poll_ms = 2000
    stop = threading.Event()

    def tick():                                           # the scheduler's tick (cairn scheduler, in production)
        while not stop.wait(1):
            try:
                s.flows.run_due()
            except Exception:  # noqa: BLE001 - shown by the assertions
                pass
    threading.Thread(target=tick, daemon=True).start()
    from cairn.governance.abac import Policy, PolicyBundle

    bundle = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(     # Ana's flow triggers a DAG: compute steps
        id="ana-jobs", effect="allow", actions=["RUN_JOBS"], condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    groups = {ANA: [], BOB: [], CARL: ["urn:li:corpGroup:finance"]}
    for who, gs in groups.items():
        s.graph.aspects[(who, "groupMembership")] = {"groups": gs}
        s.attributes.invalidate(who)
    yield {"url": base, "name": name, "dags": dags, "work": work, "s": s, "api": url, "trino": trino}
    stop.set()
    if os.environ.get("CAIRN_TEST_KEEP_AIRFLOW") != "1":
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def api(af, method, path, who, **kw):
    return httpx.request(method, f"{af['api']}{path}", headers={"X-Cairn-Actor": who}, timeout=60, **kw)


def airflow_has(af, dag_id, timeout=120):
    from cairn.flows.steps import Airflow

    client = Airflow(af["s"].flows.b.airflow)
    end = time.time() + timeout
    while time.time() < end:
        try:
            return client.call("GET", f"/dags/{dag_id}")
        except Exception:  # noqa: BLE001 - not parsed yet
            time.sleep(2)
    logs = subprocess.run(["docker", "logs", "--tail", "60", af["name"]], capture_output=True, text=True)
    raise AssertionError(f"Airflow never loaded {dag_id}\n" + logs.stdout[-3000:] + logs.stderr[-3000:])


def test_an_exported_flow_loads_in_airflow(airflow):
    af = airflow
    text = open(os.path.join(ROOT, "tests", "data", "airflow_golden", "nightly.yaml")).read()
    made = api(af, "POST", "/v1/flows", ANA, json={"yaml": text})
    assert made.status_code == 201, made.text
    r = api(af, "GET", "/v1/flows/nightly-sales/airflow", ANA, params={"version": "draft"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-python"), r.text
    assert 'filename="nightly-sales.py"' in r.headers["content-disposition"]
    assert api(af, "GET", "/v1/flows/nightly-sales/airflow", BOB, params={"version": "draft"}).status_code == 404
    (af["dags"] / "nightly_sales_export.py").write_text(r.text)
    bag = subprocess.run(["docker", "exec", af["name"], "python", "-c", (
        "import json\nfrom airflow.dag_processing.dagbag import DagBag\n"
        "b = DagBag('/opt/airflow/dags/nightly_sales_export.py')\n"
        "d = b.dags.get('nightly-sales')\n"
        "print('BAG ' + json.dumps({'errors': {k: str(v)[:500] for k, v in b.import_errors.items()},"
        " 'tasks': {t.task_id: [type(t).__name__, sorted(t.downstream_task_ids), str(t.trigger_rule)]"
        " for t in (d.tasks if d else [])}}))")], capture_output=True, text=True, timeout=300)
    out = json.loads(bag.stdout.split("BAG ", 1)[1].splitlines()[0]) if "BAG " in bag.stdout else None
    assert out is not None, bag.stdout[-3000:] + bag.stderr[-3000:]
    assert out["errors"] == {}, out["errors"]
    tasks = out["tasks"]
    assert tasks["enough"][0] == "CairnCheckOperator" and tasks["build"][0] == "CairnDbtOperator"
    assert tasks["signoff"][0] == "CairnFlowStepOperator" and tasks["big"][0] == "CairnFlowBranchOperator"
    assert tasks["build"][1] == ["export", "rows", "score"] and "all_done" in tasks["done"][2].lower()
    # The DAG processor loads it too, with no import error.
    dag = airflow_has(af, "nightly-sales")
    assert not dag.get("has_import_errors") and {"cairn", "cairn-flow:nightly-sales"} <= {t["name"] for t in dag["tags"]}
    (af["dags"] / "nightly_sales_export.py").unlink()


def test_a_flow_runs_on_airflow_and_cairn_keeps_its_history(airflow):
    af = airflow
    s = af["s"]
    made = api(af, "POST", "/v1/flows", ANA, json={"definition": HELLO, "editors": [USER]})
    assert made.status_code == 201, made.text
    pub = api(af, "POST", "/v1/flows/af-hello/publish", ANA, json={"note": "on Airflow"})
    assert pub.status_code == 200, pub.text
    assert pub.json()["airflow"]["file"] == "cairn__af-hello.py" and (af["dags"] / "cairn__af-hello.py").exists()
    run = api(af, "POST", "/v1/flows/af-hello/runs", ANA, json={"params": {"n": 5}})
    assert run.status_code == 201, run.text
    rid = run.json()["id"]
    end = time.time() + 420
    while time.time() < end:
        detail = api(af, "GET", f"/v1/flows/runs/{rid}", ANA).json()
        if detail["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(2)
    steps = {x["key"]: x for x in detail["steps"]}
    assert detail["status"] == "succeeded", (detail, subprocess.run(["docker", "logs", "--tail", "80", af["name"]],
                                                                    capture_output=True, text=True).stdout[-4000:])
    assert {k: v["status"] for k, v in steps.items()} == {"hello": "ok", "pick": "ok", "big": "ok", "small": "skipped",
                                                           "kick": "ok"}
    assert steps["pick"]["output"]["chosen"] == "then" and steps["hello"]["output"]["sent_to"] == [ANA]
    assert detail["detail"]["airflow"]["dag_id"] == "cairn__af-hello" and detail["run_as"] == ANA
    # Airflow ran it: its DAG run (started by Cairn through the REST API) and task instances agree.
    from cairn.flows.steps import Airflow

    client = Airflow(s.flows.b.airflow)
    dag_run = detail["detail"]["airflow"]["dag_run_id"]
    tis = {t["task_id"]: t["state"] for t in client.call("GET", f"/dags/cairn__af-hello/dagRuns/{dag_run}/taskInstances")
           ["task_instances"]}
    assert tis == {"hello": "success", "pick": "success", "big": "success", "small": "skipped", "kick": "success"}
    assert client.call("GET", f"/dags/cairn__af-hello/dagRuns/{dag_run}")["conf"]["cairn_run"] == rid
    # The airflow step (run in Cairn as the owner) started the other DAG through Airflow's REST API.
    kicked = client.call("GET", f"/dags/finance_report/dagRuns/{steps['kick']['output']['dag_run_id']}")
    assert kicked["conf"]["cairn_flow"] == "af-hello" and kicked["conf"]["cairn_user"] == ANA
    assert kicked["conf"]["why"] == "a report"
    # Each step was run in Cairn when its task asked, as svc-airflow (an editor), for the owner.
    started = [e for e in s.audit.entries(category="flows", limit=1000) if e["action"] == "flow.step_started"
               and e["details"].get("run") == rid]
    assert {e["details"]["step"] for e in started} == {"hello", "pick", "big", "kick"}
    assert {e["actor"] for e in started} == {USER}
    # Someone who may not run the flow cannot run its steps.
    r = api(af, "POST", f"/v1/flows/runs/{rid}/steps/hello/execute", BOB, json={"try_number": 1})
    assert r.status_code == 404, r.text


def test_airflow_dags_in_cairn_for_the_people_they_belong_to(airflow):
    af = airflow
    for dag_id in ("orders_daily", "finance_report", "cairn__af-hello"):
        airflow_has(af, dag_id)
    seen = {who: {d["dag_id"] for d in api(af, "GET", "/v1/airflow/dags", who).json()} for who in (ANA, BOB, CARL)}
    assert seen[BOB] == {"orders_daily"}, seen                              # owner bob
    assert seen[CARL] == {"finance_report"}, seen                           # tagged team:finance, his group
    assert "cairn__af-hello" in seen[ANA] and "orders_daily" not in seen[ANA], seen   # her flow's DAG
    every = {d["dag_id"] for d in api(af, "GET", "/v1/airflow/dags", ADMIN).json()}
    assert {"orders_daily", "finance_report", "cairn__af-hello"} <= every
    assert api(af, "GET", "/v1/airflow/dags/orders_daily", ANA).status_code == 404    # not hers: not there
    d = api(af, "GET", "/v1/airflow/dags/cairn__af-hello", ANA).json()
    steps = {x["name"]: x for x in d["steps"]}
    assert steps["pick"]["after"] == ["hello"] and steps["pick"]["type"] == "branch"
    assert d["run_state"] == "success" and d["statuses"] == {"hello": "ok", "pick": "ok", "big": "ok", "small": "skipped",
                                                             "kick": "ok"}
    assert d["conversion"]["convertible"] is False                      # Cairn's own: open the flow instead
    # A run started now shows live in Cairn, task by task.
    from cairn.flows.steps import Airflow

    client = Airflow(af["s"].flows.b.airflow)
    client.call("PATCH", "/dags/orders_daily", {"is_paused": False})
    client.call("POST", "/dags/orders_daily/dagRuns", {"dag_run_id": "live-1", "logical_date": None})
    live = api(af, "GET", "/v1/airflow/dags/orders_daily", BOB, params={"run": "live-1"}).json()
    assert live["run"] == "live-1" and set(live["statuses"]) <= {"start", "load", "build"}
    client.call("PATCH", "/dags/orders_daily", {"is_paused": True})


def test_a_provider_only_dag_converts_to_a_draft_flow(airflow):
    af = airflow
    airflow_has(af, "orders_daily")
    d = api(af, "GET", "/v1/airflow/dags/orders_daily", BOB).json()
    assert d["conversion"]["convertible"] is True, d["conversion"]
    r = api(af, "POST", "/v1/airflow/dags/orders_daily/convert", BOB, json={"name": "orders-daily"})
    assert r.status_code == 201, r.text
    flow = api(af, "GET", "/v1/flows/orders-daily", BOB).json()
    assert flow["owner"] == BOB and flow["published_version"] is None          # a draft, for its owner to check
    steps = {x["name"]: x for x in flow["draft"]["steps"]}
    assert set(steps) == {"load", "build"} and steps["build"]["after"] == ["load"] and steps["load"]["after"] == []
    assert steps["load"]["type"] == "sql" and steps["load"]["retries"] == 2 and steps["load"]["connection"] == "lakehouse"
    assert steps["build"]["type"] == "dbt" and steps["build"]["select"] == "tag:daily"
    assert flow["draft"]["triggers"] == [{"type": "schedule", "cron": "0 0 * * *", "timezone": "UTC", "enabled": True}]
    # A DAG with a task that has no flow step: refused, naming the task and why.
    airflow_has(af, "finance_report")
    r = api(af, "POST", "/v1/airflow/dags/finance_report/convert", CARL, json={})
    assert r.status_code == 409 and "crunch" in r.json()["detail"] and "PythonOperator" in r.json()["detail"], r.text


def run_done(af, rid, who=ANA, timeout=420):
    end = time.time() + timeout
    while time.time() < end:
        detail = api(af, "GET", f"/v1/flows/runs/{rid}", who).json()
        if detail["status"] in ("succeeded", "failed", "cancelled"):
            return detail
        time.sleep(2)
    raise AssertionError(detail)


def dag_run_done(client, dag_id, run_id, timeout=600):
    end = time.time() + timeout
    state = None
    while time.time() < end:
        state = client.call("GET", f"/dags/{dag_id}/dagRuns/{run_id}")["state"]
        if state in ("success", "failed"):
            break
        time.sleep(3)
    tis = client.call("GET", f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances")["task_instances"]
    return state, {t["task_id"]: t for t in tis}


def test_an_airflow_step_waits_for_its_dag_run_and_fails_with_it(airflow):
    """A Cairn flow (runner cairn) whose ``airflow`` step starts a DAG through Airflow's REST API, waits (parked) until
    the DAG run ends, and goes on; a DAG run that fails fails the step and stops the flow."""
    af = airflow
    for dag_id in ("af_target", "af_fails"):
        airflow_has(af, dag_id)
    flow = {"name": "af-step", "params": [{"name": "dag", "type": "string", "default": "af_target"}],
            "steps": [{"name": "run-dag", "type": "airflow", "dag_id": "{{ params.dag }}", "conf": {"day": "today"},
                       "poll_s": 5},
                      {"name": "after", "type": "notify", "after": ["run-dag"], "message": "{{ steps.run-dag.state }}"}]}
    made = api(af, "POST", "/v1/flows", ANA, json={"definition": flow})
    assert made.status_code == 201, made.text
    assert api(af, "POST", "/v1/flows/af-step/publish", ANA, json={}).status_code == 200
    r = api(af, "POST", "/v1/flows/af-step/runs", ANA, json={})
    assert r.status_code == 201, r.text
    done = run_done(af, r.json()["id"])
    steps = {x["key"]: x for x in done["steps"]}
    assert done["status"] == "succeeded", done
    out = steps["run-dag"]["output"]
    assert out["state"] == "success" and out["dag_id"] == "af_target" and steps["after"]["status"] == "ok"
    from cairn.flows.steps import Airflow

    client = Airflow(af["s"].flows.b.airflow)
    dag_run = client.call("GET", f"/dags/af_target/dagRuns/{out['dag_run_id']}")
    assert dag_run["state"] == "success" and dag_run["conf"]["cairn_user"] == ANA and dag_run["conf"]["day"] == "today"
    # The DAG fails: so does the step, and the flow stops there.
    r = api(af, "POST", "/v1/flows/af-step/runs", ANA, json={"params": {"dag": "af_fails"}})
    done = run_done(af, r.json()["id"])
    steps = {x["key"]: x for x in done["steps"]}
    assert done["status"] == "failed" and steps["after"]["status"] == "skipped", done
    assert "of af_fails failed" in steps["run-dag"]["error"]
    # A DAG Airflow does not have: the step fails, saying what Airflow said.
    r = api(af, "POST", "/v1/flows/af-step/runs", ANA, json={"params": {"dag": "no_such_dag"}})
    done = run_done(af, r.json()["id"])
    assert done["status"] == "failed" and "Airflow said 404" in {x["key"]: x for x in done["steps"]}["run-dag"]["error"]


NATIVE = {"kind": "flow", "name": "af-native", "description": "Native tasks, run by Airflow",
          "steps": [{"name": "load", "type": "sql", "connection": "lakehouse",
                     "sql": "DROP TABLE IF EXISTS analytics.af_native; CREATE TABLE analytics.af_native AS "
                            "SELECT region, count(*) AS orders FROM sales.orders GROUP BY region"},
                    {"name": "enough", "type": "check", "after": ["load"], "connection": "lakehouse",
                     "sql": "SELECT count(*) AS n FROM analytics.af_native", "measure": "n", "op": ">=", "value": 2},
                    {"name": "kick", "type": "airflow", "after": ["enough"], "dag_id": "af_target", "poll_s": 5},
                    {"name": "tell", "type": "notify", "after": ["kick"], "message": "af_native is built"}]}


# The cairn/airflow image has `cairn dbt` (its own virtualenv): there the exported DAG has a native dbt task too.
WITH_DBT = not DEV
if WITH_DBT:
    NATIVE["steps"].insert(2, {"name": "build", "type": "dbt", "after": ["enough"], "project_dir": "/project",
                               "command": "build"})
    NATIVE["steps"][3]["after"] = ["build"]


def test_an_exported_dags_native_tasks_run_in_airflow(airflow):
    """The exported DAG of a flow, run by Airflow alone: its SQL and check run natively on Trino as the Airflow
    connection's user (svc-airflow), its DAG trigger starts and waits for another DAG, its delegated step runs in Cairn
    as the owner; the table is in Trino and Cairn's run history has every task's status."""
    af = airflow
    trino = af["trino"]  # noqa: F811
    try:
        trino("CREATE SCHEMA lake.analytics")
    except RuntimeError:
        pass
    made = api(af, "POST", "/v1/flows", ANA, json={"definition": NATIVE, "editors": [USER]})
    assert made.status_code == 201, made.text
    assert api(af, "POST", "/v1/flows/af-native/publish", ANA, json={}).status_code == 200
    text = api(af, "GET", "/v1/flows/af-native/airflow", ANA).text
    assert "CairnTrinoOperator(" in text and "CairnCheckOperator(" in text and "TriggerDagRunOperator(" in text
    assert "CairnFlowStepOperator(" in text and PAT not in text
    (af["dags"] / "af_native.py").write_text(text)
    airflow_has(af, "af-native")
    from cairn.flows.steps import Airflow

    client = Airflow(af["s"].flows.b.airflow)
    client.call("PATCH", "/dags/af-native", {"is_paused": False})
    targets_before = len(client.call("GET", "/dags/af_target/dagRuns?limit=100")["dag_runs"])
    client.call("POST", "/dags/af-native/dagRuns", {"dag_run_id": "native-1", "logical_date": None})
    state, tis = dag_run_done(client, "af-native", "native-1")
    assert state == "success", ({k: v["state"] for k, v in tis.items()}, subprocess.run(
        ["docker", "logs", "--tail", "120", af["name"]], capture_output=True, text=True).stdout[-6000:])
    dbt = {"build"} if WITH_DBT else set()
    assert {k: v["state"] for k, v in tis.items()} == {k: "success" for k in {"load", "enough", "kick", "tell"} | dbt}
    assert {k: v["operator"] for k, v in tis.items()} == {
        "load": "CairnTrinoOperator", "enough": "CairnCheckOperator", "kick": "TriggerDagRunOperator",
        "tell": "CairnFlowStepOperator", **({"build": "CairnDbtOperator"} if WITH_DBT else {})}
    if WITH_DBT:                                           # dbt built its seed and model on Trino as svc-airflow
        assert trino("SELECT region, name FROM lake.analytics.region_names ORDER BY region") == [
            {"region": "EU", "name": "Europe"}, {"region": "US", "name": "United States"}]
    # The native SQL ran on Trino as svc-airflow: the table is there, and Trino's history names the user.
    assert trino("SELECT region, orders FROM lake.analytics.af_native ORDER BY region") == [
        {"region": "EU", "orders": 2}, {"region": "US", "orders": 1}]
    users = {r["user"] for r in trino("SELECT \"user\" FROM system.runtime.queries WHERE query LIKE '%af_native%' "
                                      "AND query NOT LIKE '%system.runtime%'")}
    assert "svc-airflow" in users, users
    # The trigger started af_target (and waited for it: its task succeeded only once that run had).
    runs = client.call("GET", "/dags/af_target/dagRuns?limit=100")["dag_runs"]
    assert len(runs) == targets_before + 1 and all(r["state"] == "success" for r in runs), runs
    # Cairn's run history: one run for the DAG run, every step with its task's status, the delegated one run as ana.
    s = af["s"]
    end = time.time() + 120
    while time.time() < end:
        mine = [r for r in s.flows.runs(ANA, "af-native") if r["trigger"] == "airflow"]
        if mine and mine[0]["status"] in ("succeeded", "failed"):
            break
        time.sleep(2)
    assert len(mine) == 1 and mine[0]["status"] == "succeeded", mine
    detail = api(af, "GET", f"/v1/flows/runs/{mine[0]['id']}", ANA).json()
    assert {x["key"]: x["status"] for x in detail["steps"]} == {k: "ok" for k in {"load", "enough", "kick", "tell"} | dbt}
    assert detail["run_as"] == ANA and detail["detail"]["airflow"]["dag_run_id"] == "native-1"
    assert any(n["type"] == "flow.notice" and n["body"] == "af_native is built" for n in s.notify.inbox(ANA))
    # A check that does not hold fails its task natively and Airflow skips what comes after.
    trino("DELETE FROM lake.analytics.af_native WHERE region = 'US'")
    flow = {**NATIVE, "steps": [{**NATIVE["steps"][1], "after": []},
                                *[x for x in NATIVE["steps"][2:] if x["type"] != "dbt"]]}
    flow["steps"][1] = {**flow["steps"][1], "after": ["enough"]}
    assert api(af, "PUT", "/v1/flows/af-native", ANA, json={"definition": flow}).status_code == 200
    assert api(af, "POST", "/v1/flows/af-native/publish", ANA, json={}).status_code == 200
    (af["dags"] / "af_native.py").write_text(api(af, "GET", "/v1/flows/af-native/airflow", ANA).text)
    end = time.time() + 180
    while "load" in {t["task_id"] for t in client.call("GET", "/dags/af-native/tasks")["tasks"]} and time.time() < end:
        time.sleep(2)
    client.call("POST", "/dags/af-native/dagRuns", {"dag_run_id": "native-2", "logical_date": None})
    state, tis = dag_run_done(client, "af-native", "native-2", 300)
    states = {k: v["state"] for k, v in tis.items()}
    assert state == "failed" and states["enough"] == "failed" and states["kick"] == "upstream_failed", states
    (af["dags"] / "af_native.py").unlink()


def test_git_sync_reads_the_git_folder_and_airflow_runs_the_flow(airflow, tmp_path):
    """runner: airflow with the DAG folder in a Cairn git folder: publishing commits the DAG to the folder's branch in a
    real repository (served over HTTP here), the real git-sync image syncs it into Airflow's DAG folder, Airflow loads
    it, and a run started in Cairn is carried out by Airflow with its history in Cairn."""
    import functools
    import http.server

    from cairn.flows.airflow_interop import DagFolder
    from cairn.sqlworkspace.gitfolders import GitFolders

    af = airflow
    s = af["s"]
    if subprocess.run(["docker", "image", "inspect", GIT_SYNC], capture_output=True).returncode != 0:
        pulled = subprocess.run(["docker", "pull", "-q", GIT_SYNC], capture_output=True, text=True, timeout=600)
        assert pulled.returncode == 0, f"cannot pull {GIT_SYNC}: {pulled.stderr[-500:]}"
    # The repository Cairn's git folder pushes to (file://), served read-only over HTTP for git-sync.
    served = tmp_path / "served"
    served.mkdir()
    remote = served / "dags.git"
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "--quiet", "--bare", str(remote)], check=True)
    hook = remote / "hooks" / "post-update"                 # what a dumb-HTTP server needs after each push
    hook.write_text("#!/bin/sh\nexec git update-server-info\n")
    os.chmod(hook, 0o755)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "README").write_text("Airflow DAGs\n")
    for args in (["add", "."], ["-c", "user.name=ops", "-c", "user.email=ops@example.com", "commit", "--quiet", "-m", "init"],
                 ["push", "--quiet", "origin", "HEAD:main"]):
        subprocess.run(["git", *args], cwd=seed, check=True, capture_output=True)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(served)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    gf = GitFolders(s.engine, None, workdir=str(tmp_path / "work"), allowed_urls=["file://"])
    folder = gf.create(ADMIN, {"name": "airflow-dags", "url": f"file://{remote}", "branch": "main", "path": "cairn"})
    name = f"cairn-git-sync-{os.getpid()}"
    # As the folder's owner (git refuses a repository another user owns), its home in /tmp.
    sync = subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host",
                           "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                           "-v", f"{af['dags'] / 'gitsync'}:/git", GIT_SYNC,
                           f"--repo=http://127.0.0.1:{httpd.server_address[1]}/dags.git", "--ref=main", "--root=/git",
                           "--link=current", "--period=3s", "--depth=0", "--max-failures=-1"],
                          capture_output=True, text=True, timeout=120)
    assert sync.returncode == 0, sync.stderr
    before = s.flows.dag_folder
    s.flows.dag_folder = DagFolder(f"git:{folder['id']}", gf)
    synced = af["dags"] / "gitsync" / "current" / "cairn" / "cairn__af-git.py"
    try:
        flow = {**HELLO, "name": "af-git", "steps": [x for x in HELLO["steps"] if x["name"] != "kick"]}
        made = api(af, "POST", "/v1/flows", ANA, json={"definition": flow, "editors": [USER]})
        assert made.status_code == 201, made.text
        pub = api(af, "POST", "/v1/flows/af-git/publish", ANA, json={"note": "through git"})
        assert pub.status_code == 200, pub.text
        assert pub.json()["airflow"]["commit"] and pub.json()["airflow"]["target"] == f"git:{folder['id']}"
        shown = subprocess.run(["git", "--git-dir", str(remote), "show", "main:cairn/cairn__af-git.py"],
                               capture_output=True, text=True)
        assert "dag_id='cairn__af-git'" in shown.stdout
        # git-sync puts it where Airflow reads (its published link), and Airflow loads it from there.
        end = time.time() + 120
        while not synced.exists() and time.time() < end:
            time.sleep(1)
        logs = subprocess.run(["docker", "logs", "--tail", "40", name], capture_output=True, text=True)
        assert synced.exists(), logs.stdout[-3000:] + logs.stderr[-3000:]
        dag = airflow_has(af, "cairn__af-git", timeout=180)
        assert "gitsync/current/cairn/cairn__af-git.py" in (dag.get("fileloc") or dag.get("relative_fileloc") or ""), dag
        run = api(af, "POST", "/v1/flows/af-git/runs", ANA, json={"params": {"n": 0}})
        assert run.status_code == 201, run.text
        done = run_done(af, run.json()["id"])
        assert done["status"] == "succeeded", done
        assert {x["key"]: x["status"] for x in done["steps"]} == {"hello": "ok", "pick": "ok", "big": "skipped",
                                                                   "small": "ok"}
        assert done["detail"]["airflow"]["dag_id"] == "cairn__af-git"
        # Deleting the flow removes its DAG from the branch, and git-sync from Airflow's folder.
        assert api(af, "DELETE", "/v1/flows/af-git", ANA).status_code in (200, 204)
        log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "--name-status", "main"],
                             capture_output=True, text=True).stdout
        assert log.startswith("Cairn: remove flow af-git"), log
        end = time.time() + 120
        while synced.exists() and time.time() < end:
            time.sleep(1)
        assert not synced.exists()
    finally:
        s.flows.dag_folder = before
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        httpd.shutdown()
