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
import subprocess
import threading
import time

import httpx
import pytest

from test_airflow_e2e import IMAGE, PAT, ROOT, USER, airflow_user  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn, port  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and IMAGE), reason="needs docker, CAIRN_AIRFLOW_IMAGE and the binaries")
DEV = os.environ.get("CAIRN_AIRFLOW_DEV_SRC") == "1"
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
def airflow(cairn, airflow_user, tmp_path_factory):  # noqa: F811
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
    password = secrets.token_urlsafe(24)
    (work / "passwords.json").write_text(json.dumps({"cairn": password}))
    os.chmod(work / "passwords.json", 0o666)
    (work / "cairn-password").write_text(password)          # Cairn reads its Airflow password from a file
    ap = port()
    conn = {"conn_type": "cairn", "host": url, "password": PAT}
    env = {"AIRFLOW_CONN_CAIRN_DEFAULT": json.dumps(conn),
           "AIRFLOW__CORE__AUTH_MANAGER": "airflow.api_fastapi.auth.managers.simple.simple_auth_manager.SimpleAuthManager",
           "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "cairn:admin",
           "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": "/work/passwords.json",
           "AIRFLOW__API__PORT": str(ap), "AIRFLOW__API__BASE_URL": f"http://127.0.0.1:{ap}",
           "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": f"http://127.0.0.1:{ap}/execution/",
           "AIRFLOW__API_AUTH__JWT_SECRET": secrets.token_hex(32), "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
           "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags", "AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL": "5",
           "AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL": "5", "AIRFLOW__SCHEDULER__TASK_QUEUED_TIMEOUT": "600"}
    dev = ["-v", f"{ROOT}/airflow-provider/src:/opt/cairn-provider:ro", "-e", "PYTHONPATH=/opt/cairn-provider"] if DEV else []
    name = f"cairn-flows-airflow-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host", "-v", f"{work}:/work",
                    "-v", f"{dags}:/opt/airflow/dags:ro", *dev, *[a for k, v in env.items() for a in ("-e", f"{k}={v}")],
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
    yield {"url": base, "name": name, "dags": dags, "work": work, "s": s, "api": url}
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
