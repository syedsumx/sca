"""Cross-platform orchestration with Airflow (L138), against a real Airflow 3.3 and a real Cairn:

* **Airflow drives Cairn.** A DAG of ``cairn-airflow-provider``'s ``CairnFlowRunOperator`` (deferrable: the waiting is
  the triggerer's) starts a Cairn flow with parameters as the ``cairn`` connection's Cairn identity (svc-airflow, an
  editor of the flow), waits, and succeeds with the run's id as its XCom; the flow ran as its owner. A flow that fails
  fails its task with the failed step's error. A task marked failed while it waits cancels the Cairn run (the
  trigger's ``on_kill``); a worker-held wait (``deferrable=False``) does the same through the operator's ``on_kill``.
  ``CairnFlowRunSensor`` waits for a run another task started (``wait=False``) and for the latest run of a flow.
* **Airflow's own providers.** A flow with ``http``, ``databricks`` (run-now and a one-time run), ``snowflake`` and
  ``bigquery`` steps exported as a DAG imports with no errors in Airflow 3.3 with the pinned
  apache-airflow-providers-http, -databricks, -snowflake and -google (installed in a throwaway container from
  Airflow's constraints): ``DagBag`` and ``airflow dags list-import-errors``, with every task's operator and order.

Needs docker, the image apache/airflow:slim-3.3.2-python3.12 (CAIRN_AIRFLOW_SLIM_IMAGE; cairn-airflow-provider is
mounted from this tree), the store and catalog binaries (the Cairn API fixture of tests/test_trino_e2e.py), and for the
providers, the network to PyPI."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time

import httpx
import pytest

from test_airflow_e2e import PAT, ROOT, USER, airflow_user  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn, port  # noqa: F401  (fixtures)

SLIM = os.environ.get("CAIRN_AIRFLOW_SLIM_IMAGE", "apache/airflow:slim-3.3.2-python3.12")
CONSTRAINTS = os.environ.get("CAIRN_AIRFLOW_CONSTRAINTS",
                             "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt")
# Pinned as Airflow 3.3.2's constraints pin them for Python 3.12.
PROVIDERS = ["apache-airflow-providers-http==6.1.0", "apache-airflow-providers-databricks==7.20.0",
             "apache-airflow-providers-snowflake==6.17.0", "apache-airflow-providers-google==22.5.0"]
ANA = "urn:li:corpuser:ana"


def _image() -> bool:
    return subprocess.run(["docker", "image", "inspect", SLIM], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not (_ready() and _image()), reason=f"needs docker, {SLIM} and the binaries")

DAGS = '''
from datetime import datetime, timedelta
from airflow.sdk import DAG
from cairn_airflow.flow_runs import CairnFlowRunOperator, CairnFlowRunSensor

common = dict(start_date=datetime(2026, 1, 1), schedule=None, catchup=False, is_paused_upon_creation=False)

with DAG("drive_cairn", params={"n": 3}, **common):
    run = CairnFlowRunOperator(task_id="run", flow="af-driven", flow_params={"n": "{{ params.n }}"}, deferrable=True,
                               poll_interval=2)
    kick = CairnFlowRunOperator(task_id="kick", flow="af-driven", flow_params={"n": 1}, wait=False)
    watch = CairnFlowRunSensor(task_id="watch", run_id="{{ ti.xcom_pull(task_ids='kick') }}", poke_interval=2,
                               mode="reschedule", timeout=300)
    latest = CairnFlowRunSensor(task_id="latest", flow="af-driven", poke_interval=2, deferrable=True, timeout=300)
    run >> kick >> watch >> latest

with DAG("drive_cairn_fails", **common):
    CairnFlowRunOperator(task_id="run", flow="af-fails", deferrable=True, poll_interval=2)

with DAG("drive_cairn_kill", **common):
    CairnFlowRunOperator(task_id="run", flow="af-long", deferrable=True, poll_interval=2)

with DAG("drive_cairn_kill_worker", **common):
    CairnFlowRunOperator(task_id="run", flow="af-long", deferrable=False, poll_interval=2)
'''
FLOWS = [
    {"name": "af-driven", "params": [{"name": "n", "type": "int", "default": 2}],
     "steps": [{"name": "hello", "type": "notify", "message": "n={{ params.n }}"},
               {"name": "pick", "type": "branch", "after": ["hello"], "condition": "params.n > 2", "then": ["big"]},
               {"name": "big", "type": "wait", "after": ["pick"], "seconds": 1}]},
    # Fails closed: an address on a private network, without CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS.
    {"name": "af-fails", "steps": [{"name": "call", "type": "http", "url": "https://127.0.0.1:9/v1/nothing"}]},
    {"name": "af-long", "steps": [{"name": "nap", "type": "wait", "seconds": 600}]},
]


@pytest.fixture(scope="module")
def airflow(cairn, airflow_user, tmp_path_factory):  # noqa: F811
    url, s = cairn
    work = tmp_path_factory.mktemp("airflow-drives")
    dags = work / "dags"
    dags.mkdir()
    for p in (work, dags):
        os.chmod(p, 0o777)
    (dags / "drive_cairn.py").write_text(DAGS)
    password = secrets.token_urlsafe(24)
    (work / "passwords.json").write_text(json.dumps({"cairn": password}))
    os.chmod(work / "passwords.json", 0o666)
    (work / "cairn-password").write_text(password)
    ap = port()
    env = {"AIRFLOW_CONN_CAIRN_DEFAULT": json.dumps({"conn_type": "cairn", "host": url, "password": PAT}),
           "AIRFLOW__CORE__AUTH_MANAGER": "airflow.api_fastapi.auth.managers.simple.simple_auth_manager.SimpleAuthManager",
           "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "cairn:admin",
           "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": "/work/passwords.json",
           "AIRFLOW__API__PORT": str(ap), "AIRFLOW__API__BASE_URL": f"http://127.0.0.1:{ap}",
           "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": f"http://127.0.0.1:{ap}/execution/",
           "AIRFLOW__API_AUTH__JWT_SECRET": secrets.token_hex(32), "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
           "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags", "AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL": "5",
           "AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL": "5", "AIRFLOW__SCHEDULER__TASK_QUEUED_TIMEOUT": "600",
           "PYTHONPATH": "/opt/cairn-provider"}
    env_file = work / "airflow.env"                            # the token and secrets through a file, not argv
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in env.items()))
    name = f"cairn-airflow-drives-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host", "--env-file", str(env_file),
                    "-v", f"{work}:/work", "-v", f"{dags}:/opt/airflow/dags:ro",
                    "-v", f"{ROOT}/airflow-provider/src:/opt/cairn-provider:ro", SLIM, "bash", "-c", "airflow standalone"],
                   check=True, capture_output=True, timeout=120)
    base = f"http://127.0.0.1:{ap}"
    end = time.time() + 300
    while True:
        try:
            h = httpx.get(f"{base}/api/v2/monitor/health", timeout=5, trust_env=False).json()
            if all((h.get(k) or {}).get("status") == "healthy" for k in ("scheduler", "dag_processor", "metadatabase",
                                                                         "triggerer")):
                break
        except (httpx.HTTPError, ValueError):
            pass
        if time.time() > end:
            logs = subprocess.run(["docker", "logs", "--tail", "80", name], capture_output=True, text=True)
            raise AssertionError(logs.stdout[-4000:] + logs.stderr[-4000:])
        time.sleep(3)
    from cairn.flows.steps import Airflow

    client = Airflow({"url": base, "api": "v2", "username": "cairn", "password_file": str(work / "cairn-password")})
    for flow in FLOWS:                                          # Ana's flows; svc-airflow may run them (an editor)
        made = httpx.post(f"{url}/v1/flows?publish=true", headers={"X-Cairn-Actor": ANA}, timeout=60,
                          json={"definition": flow, "editors": [USER]})
        assert made.status_code == 201, made.text
    stop = threading.Event()

    def tick():                                                 # the scheduler's tick (cairn scheduler)
        while not stop.wait(1):
            try:
                s.flows.run_due()
            except Exception:  # noqa: BLE001 - shown by the assertions
                pass
    threading.Thread(target=tick, daemon=True).start()
    yield {"url": base, "name": name, "client": client, "api": url, "s": s}
    stop.set()
    if os.environ.get("CAIRN_TEST_KEEP_AIRFLOW") != "1":
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def dag_ready(af, dag_id, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        try:
            return af["client"].call("GET", f"/dags/{dag_id}")
        except Exception:  # noqa: BLE001 - not parsed yet
            time.sleep(2)
    logs = subprocess.run(["docker", "logs", "--tail", "60", af["name"]], capture_output=True, text=True)
    raise AssertionError(f"Airflow never loaded {dag_id}\n" + logs.stdout[-3000:] + logs.stderr[-3000:])


def trigger(af, dag_id, conf=None) -> str:
    dag_ready(af, dag_id)
    run_id = f"t-{dag_id}-{int(time.time() * 1000)}"
    af["client"].call("POST", f"/dags/{dag_id}/dagRuns", {"dag_run_id": run_id, "logical_date": None, "conf": conf or {}})
    return run_id


def tis(af, dag_id, run_id) -> dict:
    return {t["task_id"]: t for t in af["client"].call("GET", f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances")
            ["task_instances"]}


def until(fn, timeout=300, what=""):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(2)
    raise AssertionError(f"timed out: {what}")


def runs_of(af, flow):
    return httpx.get(f"{af['api']}/v1/flows/{flow}/runs", headers={"X-Cairn-Actor": ANA}, timeout=30).json()


def task_log(af, dag_id, run_id, task) -> str:
    try:
        doc = af["client"].call("GET", f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task}/logs/1?full_content=true")
        return json.dumps(doc)
    except Exception as e:  # noqa: BLE001 - only for the assertion message
        return str(e)


def test_a_dag_starts_a_cairn_flow_and_waits_for_it(airflow):
    af = airflow
    run_id = trigger(af, "drive_cairn")
    done = until(lambda: (lambda d: d if d["state"] in ("success", "failed") else None)(
        af["client"].call("GET", f"/dags/drive_cairn/dagRuns/{run_id}")), 420, "drive_cairn")
    states = {k: t["state"] for k, t in tis(af, "drive_cairn", run_id).items()}
    assert done["state"] == "success", (states, task_log(af, "drive_cairn", run_id, "run")[-3000:])
    assert states == {"run": "success", "kick": "success", "watch": "success", "latest": "success"}
    runs = runs_of(af, "af-driven")
    assert len(runs) == 2 and {r["status"] for r in runs} == {"succeeded"}, runs
    xcom = af["client"].call("GET", f"/dags/drive_cairn/dagRuns/{run_id}/taskInstances/run/xcomEntries/return_value")
    first = next(r for r in runs if r["id"] == str(xcom["value"]).strip('"'))
    detail = httpx.get(f"{af['api']}/v1/flows/runs/{first['id']}", headers={"X-Cairn-Actor": ANA}, timeout=30).json()
    steps = {s["key"]: s for s in detail["steps"]}
    # Started by svc-airflow (the connection's identity) with the DAG's parameter; run as Ana, the flow's owner.
    assert detail["params"]["n"] == 3 and detail["run_as"] == ANA and detail["triggered_by"] == USER, detail
    assert steps["pick"]["output"]["chosen"] == "then" and steps["big"]["status"] == "ok"


def test_a_failed_flow_fails_its_task(airflow):
    af = airflow
    run_id = trigger(af, "drive_cairn_fails")
    until(lambda: tis(af, "drive_cairn_fails", run_id)["run"]["state"] in ("failed", "success"), 300, "fails")
    assert tis(af, "drive_cairn_fails", run_id)["run"]["state"] == "failed"
    log = task_log(af, "drive_cairn_fails", run_id, "run")
    assert "af-fails failed" in log and "call failed" in log and "private address" in log, log[-3000:]
    assert runs_of(af, "af-fails")[0]["status"] == "failed"


@pytest.mark.parametrize("dag_id", ["drive_cairn_kill", "drive_cairn_kill_worker"])
def test_killing_the_task_cancels_the_flow_run(airflow, dag_id):
    af = airflow
    before = {r["id"] for r in runs_of(af, "af-long")}
    run_id = trigger(af, dag_id)
    want = "deferred" if dag_id == "drive_cairn_kill" else "running"
    until(lambda: tis(af, dag_id, run_id)["run"]["state"] == want, 300, f"{dag_id} {want}")
    def why() -> str:      # what Airflow said, for a failure: the task's log and the triggerer's and scheduler's lines
        logs = subprocess.run(["docker", "logs", "--tail", "3000", af["name"]], capture_output=True, text=True)
        mine = [ln for ln in (logs.stdout + logs.stderr).splitlines()
                if ("trigger" in ln.lower() or "scheduler" in ln.lower()) and "dag-processor" not in ln]
        return (f"; runs: {runs_of(af, 'af-long')}; task log: {task_log(af, dag_id, run_id, 'run')[-2000:]}; "
                f"airflow (triggerer, scheduler): {chr(10).join(mine[-60:])[-4000:]}")
    # A shared CI runner is slow: the waits are generous, and a timeout says what Airflow and Cairn were doing.
    try:
        new = until(lambda: [r for r in runs_of(af, "af-long") if r["id"] not in before and r["status"] == "waiting"],
                    300, "the flow run waiting")
    except AssertionError as e:
        raise AssertionError(str(e) + why()) from None
    if want == "deferred":
        # Killed once the triggerer follows the run (a task killed before its trigger is picked up has no trigger to
        # tell; Airflow then just drops it): the trigger says so in the task's log.
        try:
            until(lambda: "following the run" in task_log(af, dag_id, run_id, "run"), 300, "the trigger following the run")
        except AssertionError as e:
            raise AssertionError(str(e) + why()) from None
    af["client"].call("PATCH", f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances/run", {"new_state": "failed"})
    try:
        gone = until(lambda: (lambda r: r if r["status"] in ("cancelled", "failed", "succeeded") else None)(
            httpx.get(f"{af['api']}/v1/flows/runs/{new[0]['id']}", headers={"X-Cairn-Actor": ANA}, timeout=30).json()),
            420, "the flow run cancelled")
    except AssertionError as e:
        raise AssertionError(str(e) + why()) from None
    assert gone["status"] == "cancelled", (gone["status"], task_log(af, dag_id, run_id, "run")[-3000:])
    started = [e for e in af["s"].audit.entries(category="flows", limit=2000)
               if e["action"] == "flow.run_cancel_requested" and e["details"].get("run") == new[0]["id"]]
    assert started and started[0]["actor"] == USER


# ---- an exported flow on Airflow's own providers ----------------------------------------------------------------
PLATFORM_FLOW = {"kind": "flow", "name": "cross-platform", "params": [{"name": "day", "default": "2026-11-09"}],
                 "steps": [
    {"name": "stage", "type": "snowflake", "account": "myorg-analytics", "user": "CAIRN", "secret_scope": "sf",
     "warehouse": "WH", "database": "SALES", "statement": "INSERT INTO D SELECT * FROM R WHERE day = '{{ params.day }}'"},
    {"name": "task", "type": "snowflake", "after": ["stage"], "account": "myorg-analytics", "user": "CAIRN",
     "secret_scope": "sf", "task": "SALES.STAGING.NIGHTLY", "multi_statement": True},
    {"name": "score", "type": "databricks", "after": ["stage"], "host": "https://adb-1.7.azuredatabricks.net",
     "secret_scope": "dbx", "job_id": 811, "job_parameters": {"day": "{{ params.day }}"}, "retries": 1},
    {"name": "once", "type": "databricks", "after": ["score"], "host": "https://adb-1.7.azuredatabricks.net",
     "secret_scope": "dbx", "auth": "oauth", "run": {"tasks": [{"task_key": "t", "notebook_task": {"notebook_path": "/x"}}]}},
    {"name": "bq", "type": "bigquery", "after": ["score"], "project": "proj", "location": "EU", "secret_scope": "gcp",
     "query": "SELECT 1", "destination_table": "proj.marts.t", "write_disposition": "WRITE_TRUNCATE",
     "airflow_conn_id": "gcp_prod"},
    {"name": "partner", "type": "http", "after": ["bq", "once", "task"], "method": "POST",
     "url": "https://api.partner.example.com/v1/imports", "auth": "bearer", "secret_scope": "partner",
     "secret_key": "token", "body": {"day": "{{ params.day }}"}, "poll_url": "/v1/imports/{$.id}",
     "status_path": "$.state", "success_values": ["done"], "failure_values": ["failed"]},
    {"name": "ping", "type": "http", "after": ["partner"], "url": "https://api.partner.example.com/health",
     "ok_status": [200, 204]}]}


def test_an_exported_flow_imports_with_airflows_own_providers(tmp_path):
    from cairn.flows import model
    from cairn.flows.airflow_dag import to_dag

    text = to_dag(model.normalize(PLATFORM_FLOW), owner=ANA, version=1)
    assert "SnowflakeSqlApiOperator(" in text and "BigQueryInsertJobOperator(" in text
    for secret_looking in ("BEGIN", "Bearer ey", "dapi"):
        assert secret_looking not in text
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "cross_platform.py").write_text(text)
    os.chmod(dags, 0o755)
    os.chmod(dags / "cross_platform.py", 0o644)
    env = []
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        if os.environ.get(k):
            env += ["-e", f"{k}={os.environ[k]}"]
    mounts = ["-v", f"{dags}:/opt/airflow/dags:ro", "-v", f"{ROOT}/airflow-provider/src:/opt/cairn-provider:ro"]
    ca = os.environ.get("PIP_CERT") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca and os.path.exists(ca):                              # a TLS-inspecting proxy's CA, for pip only
        mounts += ["-v", f"{ca}:/etc/pip-ca.pem:ro"]
        env += ["-e", "PIP_CERT=/etc/pip-ca.pem"]
    check = (
        "import json\nfrom airflow.dag_processing.dagbag import DagBag\n"
        "b = DagBag('/opt/airflow/dags')\n"
        "d = b.dags.get('cross-platform')\n"
        "print('BAG ' + json.dumps({'errors': {k: str(v)[:800] for k, v in b.import_errors.items()},"
        " 'tasks': {t.task_id: [type(t).__name__, sorted(t.downstream_task_ids)] for t in (d.tasks if d else [])}}))")
    script = (f"pip install -q --no-cache-dir --constraint {CONSTRAINTS} {' '.join(PROVIDERS)} >/tmp/pip.log 2>&1 "
              "|| { tail -30 /tmp/pip.log; exit 3; }; "
              "airflow db migrate >/dev/null 2>&1; "
              f"python -c \"$CHECK\" || exit 4; echo IMPORT-ERRORS; airflow dags list-import-errors -o json")
    name = f"cairn-airflow-providers-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    try:
        out = subprocess.run(["docker", "run", "--name", name, "--network", "host", *env, *mounts,
                              "-e", "PYTHONPATH=/opt/cairn-provider", "-e", "AIRFLOW__CORE__LOAD_EXAMPLES=false",
                              "-e", f"CHECK={check}", SLIM, "bash", "-c", script],
                             capture_output=True, text=True, timeout=1500)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)    # throwaway: nothing kept
    assert out.returncode == 0 and "BAG " in out.stdout, out.stdout[-3000:] + out.stderr[-3000:]
    bag = json.loads(out.stdout.split("BAG ", 1)[1].splitlines()[0])
    assert bag["errors"] == {}, bag["errors"]
    listed = [line for line in out.stdout.split("IMPORT-ERRORS", 1)[1].splitlines() if line.strip()]
    assert listed and json.loads(listed[-1]) == [], listed                # the CLI's JSON is its last line
    tasks = bag["tasks"]
    assert {k: v[0] for k, v in tasks.items()} == {
        "stage": "SnowflakeSqlApiOperator", "task": "SnowflakeSqlApiOperator", "score": "DatabricksRunNowOperator",
        "once": "DatabricksSubmitRunOperator", "bq": "BigQueryInsertJobOperator", "partner.call": "HttpOperator",
        "partner.done": "HttpSensor", "ping": "HttpOperator"}, tasks
    assert tasks["stage"][1] == ["score", "task"] and tasks["partner.call"][1] == ["partner.done"]
    assert tasks["partner.done"][1] == ["ping"] and tasks["bq"][1] == ["partner.call"]
