"""Airflow 2 as a flow's runner (L124), through Airflow 2's stable REST API (``/api/v1``, ``CAIRN_AIRFLOW_API=v1``):
basic authentication with Cairn's Airflow user (its password from a file), the DAG file Cairn deploys for
``runner: airflow`` loading on Airflow 2 (its ``DAG`` and ``Param`` imports fall back to Airflow 2's modules), a run
started in Cairn carried out by Airflow 2 (DAG run made without a logical date; task instances followed into Cairn's
run history), each task running its step in Cairn as the flow's owner, the branch's other side skipped, an ``airflow``
step starting and waiting for another DAG; Airflow 2's DAGs shown in Cairn (runs ordered by ``execution_date``) and a
DAG of provider operators converted (its source read by the DAG's file token). Wrong credentials fail closed.

Needs docker, an Airflow 2 image (CAIRN_AIRFLOW2_IMAGE, e.g. apache/airflow:slim-2.10.5-python3.12; the pipelines CI
job pulls it) and the store and catalog binaries. cairn-airflow-provider is mounted from this tree."""

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

IMAGE2 = os.environ.get("CAIRN_AIRFLOW2_IMAGE", "")
pytestmark = pytest.mark.skipif(not (_ready() and IMAGE2),
                                reason="needs docker, CAIRN_AIRFLOW2_IMAGE (an Airflow 2 image) and the binaries")
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"

DAGS2 = '''
from datetime import datetime
from airflow import DAG
from airflow.operators.empty import EmptyOperator
from cairn_airflow.operators import CairnTrinoOperator

with DAG("af2_target", start_date=datetime(2026, 1, 1), schedule=None, is_paused_upon_creation=False,
         default_args={"owner": "airflow"}):
    EmptyOperator(task_id="done")

with DAG("orders2", start_date=datetime(2026, 1, 1), schedule="0 6 * * *", catchup=False, is_paused_upon_creation=True,
         default_args={"owner": "bob"}, description="Orders on Airflow 2"):
    load = CairnTrinoOperator(task_id="load", sql="INSERT INTO marts.daily SELECT * FROM raw.orders", retries=1)
    check = CairnTrinoOperator(task_id="check", sql="SELECT count(*) FROM marts.daily")
    load >> check
'''
FLOW = {"kind": "flow", "name": "af2-hello", "runner": "airflow",
        "params": [{"name": "n", "type": "int", "default": 2}],
        "steps": [{"name": "hello", "type": "notify", "message": "hello {{ params.n }} from Airflow 2"},
                  {"name": "pick", "type": "branch", "after": ["hello"], "condition": "params.n > 1",
                   "then": ["big"], "else": ["small"]},
                  {"name": "big", "type": "wait", "after": ["pick"], "seconds": 1},
                  {"name": "small", "type": "notify", "after": ["pick"], "message": "small"},
                  {"name": "kick", "type": "airflow", "after": ["hello"], "dag_id": "af2_target", "wait": False}]}


@pytest.fixture(scope="module")
def airflow2(cairn, airflow_user, tmp_path_factory):  # noqa: F811
    from cairn.flows.airflow_interop import DagFolder

    url, s = cairn
    work = tmp_path_factory.mktemp("airflow2")
    dags = work / "dags"
    dags.mkdir()
    for p in (work, dags):
        os.chmod(p, 0o777)
    (dags / "af2_dags.py").write_text(DAGS2)
    password = secrets.token_urlsafe(24)
    (work / "cairn-password").write_text(password)            # Cairn reads its Airflow password from a file
    ap = port()
    env = {"AIRFLOW_CONN_CAIRN_DEFAULT": json.dumps({"conn_type": "cairn", "host": url, "password": PAT}),
           "AIRFLOW__API__AUTH_BACKENDS": "airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session",
           "AIRFLOW__WEBSERVER__WEB_SERVER_PORT": str(ap), "AIRFLOW__WEBSERVER__SECRET_KEY": secrets.token_hex(16),
           "AIRFLOW__CORE__LOAD_EXAMPLES": "false", "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags",
           "AIRFLOW__SCHEDULER__DAG_DIR_LIST_INTERVAL": "5", "AIRFLOW__SCHEDULER__MIN_FILE_PROCESS_INTERVAL": "5",
           "PYTHONPATH": "/opt/cairn-provider",
           # The official image's entrypoint migrates the database and makes Cairn's user (an Admin) first.
           "_AIRFLOW_DB_MIGRATE": "true", "_AIRFLOW_WWW_USER_CREATE": "true", "_AIRFLOW_WWW_USER_USERNAME": "cairn",
           "_AIRFLOW_WWW_USER_PASSWORD": password}
    env_file = work / "airflow.env"                           # the password through a file, never a command line
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in env.items()))
    name = f"cairn-flows-airflow2-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host", "--env-file", str(env_file),
                    "-v", f"{dags}:/opt/airflow/dags:ro", "-v", f"{ROOT}/airflow-provider/src:/opt/cairn-provider:ro",
                    IMAGE2, "bash", "-c", "airflow scheduler & exec airflow webserver"],
                   check=True, capture_output=True, timeout=300)
    base = f"http://127.0.0.1:{ap}"
    end = time.time() + 400
    while True:
        try:
            h = httpx.get(f"{base}/health", timeout=5).json()
            if all((h.get(k) or {}).get("status") == "healthy" for k in ("scheduler", "metadatabase")):
                break
        except (httpx.HTTPError, ValueError):
            pass
        if time.time() > end:
            logs = subprocess.run(["docker", "logs", "--tail", "80", name], capture_output=True, text=True)
            raise AssertionError(logs.stdout[-4000:] + logs.stderr[-4000:])
        time.sleep(3)
    # The image's entrypoint makes Cairn's user but ignores a failure: check the login, and if Airflow refuses it,
    # make the user again (the password on stdin, never a command line) and say why if it still refuses.
    def signs_in():
        return httpx.get(f"{base}/api/v1/dags?limit=1", auth=("cairn", password), timeout=10).status_code == 200
    end = time.time() + 60
    while not signs_in() and time.time() < end:
        time.sleep(3)
    if not signs_in():
        made = subprocess.run(["docker", "exec", "-i", name, "airflow", "users", "create", "--username", "cairn",
                               "--role", "Admin", "--firstname", "Cairn", "--lastname", "Flows",
                               "--email", "cairn@example.invalid"], input=f"{password}\n{password}\n",
                              capture_output=True, text=True, timeout=180)
        if not signs_in():
            logs = subprocess.run(["docker", "logs", "--tail", "120", name], capture_output=True, text=True)
            raise AssertionError("Airflow 2 refuses Cairn's user:\n" + made.stdout[-2000:] + made.stderr[-2000:]
                                 + logs.stdout[-4000:] + logs.stderr[-4000:])
    saved = (dict(s.flows.b.airflow), s.flows.dag_folder, s.flows.airflow_poll_ms)
    s.flows.b.airflow.update(url=base, api="v1", username="cairn", password_file=str(work / "cairn-password"),
                             token_file="")
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
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(     # ana's flow starts a DAG: a compute step
        id="ana-jobs-af2", effect="allow", actions=["RUN_JOBS"], condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    for who in (ANA, BOB):
        s.graph.aspects[(who, "groupMembership")] = {"groups": []}
        s.attributes.invalidate(who)
    yield {"url": base, "name": name, "dags": dags, "work": work, "s": s, "api": url}
    stop.set()
    s.flows.b.airflow.clear()
    s.flows.b.airflow.update(saved[0])
    s.flows.dag_folder, s.flows.airflow_poll_ms = saved[1], saved[2]
    if os.environ.get("CAIRN_TEST_KEEP_AIRFLOW") != "1":
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def api(af, method, path, who, **kw):
    return httpx.request(method, f"{af['api']}{path}", headers={"X-Cairn-Actor": who}, timeout=60, **kw)


def has(af, dag_id, timeout=240):
    from cairn.flows.steps import Airflow

    client = Airflow(af["s"].flows.b.airflow)
    end = time.time() + timeout
    while time.time() < end:
        try:
            return client.call("GET", f"/dags/{dag_id}")
        except Exception:  # noqa: BLE001 - not parsed yet
            time.sleep(2)
    logs = subprocess.run(["docker", "logs", "--tail", "60", af["name"]], capture_output=True, text=True)
    print(logs.stdout[-3000:], logs.stderr[-3000:])
    errors = client.call("GET", "/importErrors")
    raise AssertionError(f"Airflow 2 never loaded {dag_id}: {errors}\n" + logs.stdout[-3000:] + logs.stderr[-3000:])


def test_airflow_2_runs_a_flow_through_its_v1_api(airflow2):
    af = airflow2
    s = af["s"]
    has(af, "af2_target")
    made = api(af, "POST", "/v1/flows", ANA, json={"definition": FLOW, "editors": [USER]})
    assert made.status_code == 201, made.text
    pub = api(af, "POST", "/v1/flows/af2-hello/publish", ANA, json={"note": "on Airflow 2"})
    assert pub.status_code == 200, pub.text
    text = (af["dags"] / "cairn__af2-hello.py").read_text()
    assert "except ImportError:  # Airflow 2" in text
    dag = has(af, "cairn__af2-hello")
    assert not dag.get("has_import_errors") and "cairn-flow-version:1" in {t["name"] for t in dag["tags"]}
    run = api(af, "POST", "/v1/flows/af2-hello/runs", ANA, json={"params": {"n": 5}})
    assert run.status_code == 201, run.text
    rid = run.json()["id"]
    end = time.time() + 600
    while time.time() < end:
        detail = api(af, "GET", f"/v1/flows/runs/{rid}", ANA).json()
        if detail["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(2)
    steps = {x["key"]: x for x in detail["steps"]}
    logs = subprocess.run(["docker", "logs", "--tail", "80", af["name"]], capture_output=True, text=True).stdout
    assert detail["status"] == "succeeded", ({k: (v["status"], v["error"]) for k, v in steps.items()}, logs[-4000:])
    assert {k: v["status"] for k, v in steps.items()} == {"hello": "ok", "pick": "ok", "big": "ok", "small": "skipped",
                                                           "kick": "ok"}
    assert steps["hello"]["output"]["sent_to"] == [ANA] and steps["pick"]["output"]["chosen"] == "then"
    assert any(n["type"] == "flow.notice" and n["body"] == "hello 5 from Airflow 2" for n in s.notify.inbox(ANA))
    # Airflow 2 carried it out: its DAG run (made by Cairn over /api/v1) and task instances agree with Cairn.
    from cairn.flows.steps import Airflow

    client = Airflow(s.flows.b.airflow)
    dag_run_id = detail["detail"]["airflow"]["dag_run_id"]
    dr = client.call("GET", f"/dags/cairn__af2-hello/dagRuns/{dag_run_id}")
    assert dr["state"] == "success" and dr["conf"]["cairn_run"] == rid and dr["conf"]["cairn_user"] == ANA
    tis = {t["task_id"]: t["state"] for t in client.call(
        "GET", f"/dags/cairn__af2-hello/dagRuns/{dag_run_id}/taskInstances")["task_instances"]}
    assert tis == {"hello": "success", "pick": "success", "big": "success", "small": "skipped", "kick": "success"}
    # The airflow step (run in Cairn as ana) started af2_target over /api/v1 (without waiting here: this Airflow's
    # SequentialExecutor runs one task at a time, so a task waiting for another DAG would hold its only slot).
    kicked = client.call("GET", f"/dags/af2_target/dagRuns/{steps['kick']['output']['dag_run_id']}")
    assert kicked["conf"]["cairn_user"] == ANA and kicked["conf"]["cairn_flow"] == "af2-hello"
    # Each step ran in Cairn when its task asked, as svc-airflow (an editor), for ana.
    started = [e for e in s.audit.entries(category="flows", limit=2000) if e["action"] == "flow.step_started"
               and e["details"].get("run") == rid]
    assert {e["details"]["step"] for e in started} == {"hello", "pick", "big", "kick"}
    assert {e["actor"] for e in started} == {USER}
    # A flow Cairn runs, whose airflow step starts af2_target over /api/v1 and waits until its DAG run succeeds.
    flow = {"name": "af2-step", "steps": [{"name": "run-dag", "type": "airflow", "dag_id": "af2_target", "poll_s": 5}]}
    assert api(af, "POST", "/v1/flows", ANA, json={"definition": flow}).status_code == 201
    assert api(af, "POST", "/v1/flows/af2-step/publish", ANA, json={}).status_code == 200
    rid = api(af, "POST", "/v1/flows/af2-step/runs", ANA, json={}).json()["id"]
    end = time.time() + 300
    while time.time() < end:
        detail = api(af, "GET", f"/v1/flows/runs/{rid}", ANA).json()
        if detail["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(2)
    assert detail["status"] == "succeeded", detail
    out = detail["steps"][0]["output"]
    assert out["state"] == "success" and client.call("GET", f"/dags/af2_target/dagRuns/{out['dag_run_id']}")["state"] == "success"


def test_airflow_2_dags_show_in_cairn_and_convert(airflow2):
    af = airflow2
    has(af, "orders2")
    seen = {d["dag_id"]: d for d in api(af, "GET", "/v1/airflow/dags", BOB).json()}
    assert set(seen) == {"orders2"}, seen                                    # bob owns it; the others are not his
    assert seen["orders2"]["schedule"] == "0 6 * * *"
    # Its page: Airflow 2's DAG details leave owners empty, so Cairn reads them from the DAG (still bob's only).
    d = api(af, "GET", "/v1/airflow/dags/orders2", BOB).json()
    assert "conversion" in d and d["conversion"]["convertible"] is True, d
    r = api(af, "POST", "/v1/airflow/dags/orders2/convert", BOB, json={"name": "orders2"})
    assert r.status_code == 201, r.text
    flow = api(af, "GET", "/v1/flows/orders2", BOB).json()
    steps = {x["name"]: x for x in flow["draft"]["steps"]}
    assert steps["load"]["type"] == "sql" and steps["load"]["retries"] == 1 and steps["check"]["after"] == ["load"]
    assert flow["draft"]["triggers"][0]["cron"] == "0 6 * * *"
    # Ana sees her flow's DAG with its last run (Airflow 2 orders runs by execution_date).
    mine = api(af, "GET", "/v1/airflow/dags/cairn__af2-hello", ANA).json()
    assert mine["run_state"] == "success" and mine["statuses"]["small"] == "skipped"
    assert api(af, "GET", "/v1/airflow/dags/orders2", ANA).status_code == 404


def test_wrong_airflow_2_credentials_fail_closed(airflow2, tmp_path):
    from cairn.flows.steps import Airflow, StepFailed

    af = airflow2
    bad = tmp_path / "wrong"
    bad.write_text("not-the-password")
    client = Airflow({**af["s"].flows.b.airflow, "password_file": str(bad)})
    with pytest.raises(StepFailed, match="said 401"):
        client.call("GET", "/dags")
    assert httpx.get(f"{af['url']}/api/v1/dags", timeout=30).status_code == 401      # no credentials: refused
