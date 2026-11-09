"""Flow steps on other platforms (L138), in Cairn's flow engine with real secret scopes (encrypted, read as the owner):
Databricks (Jobs API 2.2: run-now with job parameters, runs/submit, a personal access token and OAuth M2M, the run
page, a failed run, cancel on flow cancel and on time out), Snowflake (SQL API v2: a key-pair JWT with an encrypted
key, an async statement polled to its rows, EXECUTE TASK, a compilation error, cancel) and BigQuery (a service
account's token, jobs.insert polled with jobs.get, the results, a failed job, cancel), each against a stand-in that
answers with the documented request and response shapes and checks what the service checks
(tests/platform_standins.py; neither Databricks nor Snowflake has an emulator and there are no accounts here: the
real BigQuery emulator is in tests/test_flows_platforms_e2e.py). Fail closed: an owner who cannot read the scope, or a
missing key, sends nothing; publishing refuses a scope the owner cannot read. No secret is in a run's output, log or
definition. Definitions are checked (credentials in headers or the URL, missing auth fields, JSONPaths), and the flow
exports to an Airflow DAG with Airflow's own providers and named connections."""

from __future__ import annotations

import base64
import json
import os
import threading
import time

import pytest

from cairn.common.db import get_engine, init_db
from cairn.flows import model
from cairn.flows.engine import Flows
from cairn.flows.model import FlowError
from cairn.flows.steps import Backends
from platform_standins import Platforms

ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"


@pytest.fixture(scope="module")
def platforms(tmp_path_factory):
    p = Platforms(str(tmp_path_factory.mktemp("platforms")))
    yield p
    p.close()


@pytest.fixture
def env(tmp_path, monkeypatch, platforms):
    from cairn.identity.secret_scopes import SecretScopes

    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("SSL_CERT_FILE", platforms.ca)            # the stand-ins' private CA, verified like any other
    monkeypatch.setenv("CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS", "true")   # the stand-ins listen on 127.0.0.1
    monkeypatch.setenv("CAIRN_FLOWS_MIN_INTERVAL_MINUTES", "1")
    meta = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(meta)
    ss = SecretScopes(meta, lambda a: [], lambda a: False)
    for scope, values in {"dbx": {"token": platforms.pat, "client_id": platforms.client[0],
                                  "client_secret": platforms.client[1]},
                          "sf": {"private_key": platforms.sf_key_pem, "passphrase": "open sesame"},
                          "gcp": {"service_account_json": platforms.sa_json()}}.items():
        ss.create(ANA, {"name": scope})
        for k, v in values.items():
            ss.put(ANA, scope, k, v)
    told: list = []
    f = Flows(meta, Backends(secrets=ss, notify=lambda *a: told.append(a)), None, lambda *a: told.append(a),
              lambda a: False, lambda a: [], None, None, None, None, "one")
    stop = threading.Event()

    def tick():                                     # the scheduler's tick: resumes parked runs
        while not stop.wait(0.3):
            try:
                f.run_due()
            except Exception:  # noqa: BLE001 - shown by the assertions
                pass
    threading.Thread(target=tick, daemon=True).start()
    f.secrets, f.told, f.stand_in = ss, told, platforms
    yield f
    stop.set()


def wait_until(fn, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("timed out")


def steps_of(run):
    return {s["key"]: s for s in run["steps"]}


def run_flow(env, flow, timeout=60):
    env.create(ANA, flow, publish=True)
    run = env.run(ANA, flow["name"])
    return env.wait_for(run["id"], timeout)


def everything(env, rid) -> str:
    """A run as the API shows it: its steps' outputs, errors and logs."""
    run = env.run_detail(ANA, rid)
    logs = [env.step_log(ANA, rid, s["key"]) for s in run["steps"]]
    return json.dumps([run, logs], default=str)


def secrets_in(env, text: str) -> list[str]:
    sp = env.stand_in
    values = [sp.pat, sp.client[1], "open sesame", *sp.sf_key_pem.splitlines()[1:3], *sp.sa_pem.splitlines()[1:3],
              *sp.oauth_tokens, *sp.google_tokens]
    return [v for v in values if v and v in text]


DBX = {"type": "databricks", "secret_scope": "dbx", "poll_s": 1}


def test_databricks_run_now_with_a_token_and_a_one_time_run_with_oauth(env):
    sp = env.stand_in
    flow = {"name": "dbx", "params": [{"name": "day", "default": "2026-11-01"}], "steps": [
        {"name": "score", **DBX, "host": sp.url, "job_id": "811", "job_parameters": {"day": "{{ params.day }}", "n": 3}},
        {"name": "once", **DBX, "after": ["score"], "host": sp.url, "auth": "oauth",
         "run": {"tasks": [{"task_key": "main", "notebook_task": {"notebook_path": "/Repos/x/score"}}]}},
        {"name": "tell", "type": "notify", "after": ["once"], "message": "{{ steps.score.run_page_url }}"}]}
    done = run_flow(env, flow)
    assert done["status"] == "succeeded", [(s["key"], s["status"], s["error"]) for s in done["steps"]]
    out = steps_of(done)["score"]["output"]
    assert out["result_state"] == "SUCCESS" and out["run_page_url"].endswith(f"/run/{out['run_id']}"), out
    assert out["tasks"] == [{"task_key": "main", "result_state": "SUCCESS"}] and out["duration_ms"] == 4200
    now = [r for r in sp.requests if r["path"] == "/api/2.2/jobs/run-now"][-1]
    assert now["body"]["job_id"] == 811 and now["body"]["job_parameters"] == {"day": "2026-11-01", "n": "3"}
    assert now["headers"]["authorization"] == f"Bearer {sp.pat}"
    submit = [r for r in sp.requests if r["path"] == "/api/2.2/jobs/runs/submit"][-1]
    assert submit["body"]["run_name"] == "cairn dbx.once" and submit["headers"]["authorization"].startswith("Bearer dbx-oauth-")
    assert env.told[-1][2] == out["run_page_url"]
    log = env.step_log(ANA, done["id"], "score")["log"]
    assert "run page: " in log and "Databricks run-now: run" in log
    assert not secrets_in(env, everything(env, done["id"]))


def test_databricks_failures_cancel_and_time_out(env):
    sp = env.stand_in
    done = run_flow(env, {"name": "dbx-fail", "steps": [{"name": "score", **DBX, "host": sp.url, "job_id": 666}]})
    err = steps_of(done)["score"]["error"]
    assert done["status"] == "failed" and "FAILED" in err and "division by zero" in err and "/run/" in err, err
    done = run_flow(env, {"name": "dbx-404", "steps": [{"name": "score", **DBX, "host": sp.url, "job_id": 404}]})
    assert "Job 404 does not exist" in steps_of(done)["score"]["error"]
    # Cancelling the flow's run cancels the Databricks run.
    env.create(ANA, {"name": "dbx-long", "steps": [{"name": "score", **DBX, "host": sp.url, "job_id": 777}]}, publish=True)
    run = env.run(ANA, "dbx-long")
    wait_until(lambda: (steps_of(env._run_public(run["id"]))["score"]["status"] == "waiting"))
    rid_remote = env._step_rows(run["id"])[0]["state"]["run_id"]
    env.cancel(ANA, run["id"])
    assert env.wait_for(run["id"], 30)["status"] == "cancelled"
    assert ("databricks", rid_remote) in sp.cancelled
    # Out of time: cancelled there too, and the step says so.
    done = run_flow(env, {"name": "dbx-slow", "steps": [{"name": "score", **DBX, "host": sp.url, "job_id": 777,
                                                         "timeout_s": 3}]})
    err = steps_of(done)["score"]["error"]
    assert done["status"] == "failed" and "timed out" in err and "cancelled" in err, err
    assert sp.cancelled[-1][0] == "databricks"


SF = {"type": "snowflake", "account": "myorg-acct", "user": "cairn_flows", "secret_scope": "sf",
      "passphrase_key": "passphrase", "poll_s": 1}


def test_snowflake_statements_and_tasks_with_a_key_pair(env):
    sp = env.stand_in
    done = run_flow(env, {"name": "sf", "steps": [
        {"name": "stage", **SF, "host": sp.url, "warehouse": "WH", "database": "SALES", "schema": "STAGING",
         "role": "TRANSFORMER", "statement": "SELECT region, count(*) AS n FROM orders GROUP BY 1"},
        {"name": "kick", **SF, "after": ["stage"], "host": sp.url, "task": "SALES.STAGING.NIGHTLY"}]})
    assert done["status"] == "succeeded", [(s["key"], s["status"], s["error"]) for s in done["steps"]]
    out = steps_of(done)["stage"]["output"]
    assert out["rows"] == 2 and out["columns"] == ["REGION", "N"] and out["preview"] == [["EU", "3"], ["US", "1"]]
    posts = [r for r in sp.requests if r["path"] == "/api/v2/statements" and r["method"] == "POST"][-2:]
    assert posts[0]["body"] == {"statement": "SELECT region, count(*) AS n FROM orders GROUP BY 1", "timeout": 3600,
                                "database": "SALES", "schema": "STAGING", "warehouse": "WH", "role": "TRANSFORMER"}
    assert posts[1]["body"]["statement"] == "EXECUTE TASK SALES.STAGING.NIGHTLY"
    assert posts[0]["headers"]["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"
    assert not secrets_in(env, everything(env, done["id"]))
    # A compilation error fails the step with Snowflake's message and SQL state.
    done = run_flow(env, {"name": "sf-bad", "steps": [{"name": "q", **SF, "host": sp.url,
                                                       "statement": "SELECT * FROM MISSING_TABLE"}]})
    err = steps_of(done)["q"]["error"]
    assert "422" in err and "42S02" in err and "does not exist" in err, err
    # Cancelled: the statement is cancelled in Snowflake.
    env.create(ANA, {"name": "sf-long", "steps": [{"name": "q", **SF, "host": sp.url, "statement": "CALL SLEEP(600)"}]},
               publish=True)
    run = env.run(ANA, "sf-long")
    wait_until(lambda: (steps_of(env._run_public(run["id"]))["q"]["status"] == "waiting"))
    handle = env._step_rows(run["id"])[0]["state"]["handle"]
    env.cancel(ANA, run["id"])
    assert env.wait_for(run["id"], 30)["status"] == "cancelled"
    assert ("snowflake", handle) in sp.cancelled


BQ = {"type": "bigquery", "project": "proj", "secret_scope": "gcp", "location": "EU", "poll_s": 1}


def test_bigquery_jobs_with_a_service_account(env):
    sp = env.stand_in
    done = run_flow(env, {"name": "bq", "steps": [
        {"name": "q", **BQ, "api_url": sp.url, "query": "SELECT region, count(*) n FROM ds.orders GROUP BY 1",
         "destination_table": "proj.marts.by_region", "write_disposition": "WRITE_TRUNCATE",
         "max_bytes_billed": 10 ** 9, "labels": {"Team": "Analytics"}}]})
    assert done["status"] == "succeeded", [(s["key"], s["status"], s["error"]) for s in done["steps"]]
    out = steps_of(done)["q"]["output"]
    assert out["rows"] == 2 and out["columns"] == ["region", "n"] and out["preview"] == [["EU", "3"], ["US", "1"]]
    assert out["bytes_processed"] == 1024 and "j=bq%3AEU%3Acairn_bq_q_" in out["console_url"]
    ins = [r for r in sp.requests if r["path"] == "/bigquery/v2/projects/proj/jobs" and r["method"] == "POST"][-1]
    cfg = ins["body"]["configuration"]
    assert cfg["query"]["destinationTable"] == {"projectId": "proj", "datasetId": "marts", "tableId": "by_region"}
    assert cfg["query"]["writeDisposition"] == "WRITE_TRUNCATE" and cfg["query"]["maximumBytesBilled"] == "1000000000"
    assert cfg["labels"] == {"cairn_flow": "bq", "cairn_step": "q", "team": "analytics"}
    assert ins["body"]["jobReference"]["location"] == "EU"
    assert not secrets_in(env, everything(env, done["id"]))
    done = run_flow(env, {"name": "bq-bad", "steps": [{"name": "q", **BQ, "api_url": sp.url,
                                                       "query": "SELECT * FROM ds.missing"}]})
    err = steps_of(done)["q"]["error"]
    assert "notFound" in err and "ds.missing" in err, err
    env.create(ANA, {"name": "bq-long", "steps": [{"name": "q", **BQ, "api_url": sp.url, "query": "SELECT SLEEP"}]},
               publish=True)
    run = env.run(ANA, "bq-long")
    wait_until(lambda: (steps_of(env._run_public(run["id"]))["q"]["status"] == "waiting"))
    env.cancel(ANA, run["id"])
    assert env.wait_for(run["id"], 30)["status"] == "cancelled"
    assert sp.cancelled[-1][0] == "bigquery"


def test_fail_closed_on_secrets(env):
    sp = env.stand_in
    # Publishing refuses a scope the owner cannot read.
    env.secrets.create(BOB, {"name": "bobs"})
    with pytest.raises(FlowError) as e:
        env.create(ANA, {"name": "nope", "steps": [{"name": "q", **BQ, "secret_scope": "bobs", "query": "SELECT 1"}]},
                   publish=True)
    assert "cannot read the secret scope 'bobs'" in str(e.value.errors)
    # A key that is not there: the step fails before anything is sent.
    before = len(sp.requests)
    done = run_flow(env, {"name": "nokey", "steps": [{"name": "score", **DBX, "host": sp.url, "token_key": "nothing",
                                                      "job_id": 1}]})
    err = steps_of(done)["score"]["error"]
    assert done["status"] == "failed" and "'nothing'" in err and "secret scope 'dbx'" in err, err
    assert len(sp.requests) == before
    # The scope taken away from the owner after publishing: the same.
    env.create(ANA, {"name": "revoked", "steps": [{"name": "q", **BQ, "api_url": sp.url, "query": "SELECT 1"}]},
               publish=True)
    from sqlalchemy import delete

    from cairn.identity.secret_scopes import secret_acls

    with env.engine.begin() as c:
        c.execute(delete(secret_acls).where(secret_acls.c.scope == "gcp"))
    run = env.run(ANA, "revoked")
    done = env.wait_for(run["id"], 30)
    assert done["status"] == "failed" and "needs READ" in steps_of(done)["q"]["error"]
    assert len(sp.requests) == before
    # No secret scopes in this Cairn at all.
    env.b.secrets, saved = None, env.b.secrets
    try:
        with pytest.raises(FlowError) as e:                   # publishing says so
            env.create(ANA, {"name": "noscopes", "steps": [{"name": "score", **DBX, "host": sp.url, "job_id": 1}]},
                       publish=True)
        assert "secret scopes (CAIRN_SECRETS_KEY) is not set up" in str(e.value)
        run = env.run(ANA, "revoked")                          # and a published flow's step fails, sending nothing
        done = env.wait_for(run["id"], 30)
    finally:
        env.b.secrets = saved
    assert "secret scopes are not set up" in steps_of(done)["q"]["error"]
    assert len(sp.requests) == before


def test_private_and_metadata_addresses_are_refused_without_the_egress_proxy(env, monkeypatch):
    sp = env.stand_in
    monkeypatch.delenv("CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS")
    before = len(sp.requests)
    done = run_flow(env, {"name": "private", "steps": [
        {"name": "inside", "type": "http", "url": f"{sp.url}/api/2.2/jobs/runs/get?run_id=1"},
        {"name": "metadata", "type": "http", "url": "http://169.254.169.254/computeMetadata/v1/"},
        {"name": "dbx", **DBX, "host": sp.url, "job_id": 1}]})
    s = steps_of(done)
    assert "resolves to a private address (127.0.0.1)" in s["inside"]["error"], s["inside"]
    assert "CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS" in s["dbx"]["error"]
    assert "link-local" in s["metadata"]["error"]
    assert len(sp.requests) == before                          # nothing reached the private service
    monkeypatch.setenv("CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS", "true")   # the metadata service: never, even then
    done = run_flow(env, {"name": "metadata", "steps": [{"name": "m", "type": "http", "url": "http://169.254.169.254/"}]})
    assert "link-local" in steps_of(done)["m"]["error"]


def test_definitions_are_checked():
    def errors(step):
        return {(e["path"], e["message"].split(" (")[0].split(":")[0])
                for e in model.validate({"name": "x", "steps": [{"name": "s", **step}]})["errors"]}
    assert ("steps[0].headers", "Authorization carries a credential") in errors(
        {"type": "http", "url": "https://a.example.com", "headers": {"Authorization": "Bearer abc"}})
    assert ("steps[0].url", "has credentials in it") in errors({"type": "http", "url": "https://u:p@a.example.com/"})
    assert ("steps[0]", "auth bearer needs secret_scope and secret_key") in errors(
        {"type": "http", "url": "https://a.example.com", "auth": "bearer"})
    assert ("steps[0].username", "is required for basic auth") in errors(
        {"type": "http", "url": "https://a.example.com", "auth": "basic", "secret_scope": "s", "secret_key": "k"})
    assert ("steps[0].status_path", "$x is not a JSONPath") in {(p, m.replace("'", "")) for p, m in errors(
        {"type": "http", "url": "https://a.example.com", "status_path": "$x", "success_values": ["ok"]})}
    assert ("steps[0].body", "a GET has no body") in errors({"type": "http", "url": "https://a.example.com", "body": {}})
    assert ("steps[0]", "needs either job_id") in errors({"type": "databricks", "host": "https://h", "secret_scope": "s"})
    assert ("steps[0]", "needs either statement") in errors(
        {"type": "snowflake", "account": "a", "user": "u", "secret_scope": "s"})
    assert ("steps[0].task", "is a task name") in errors(
        {"type": "snowflake", "account": "a", "user": "u", "secret_scope": "s", "task": "x; DROP TABLE y"})
    assert ("steps[0].destination_table", "is project.dataset.table") in errors(
        {"type": "bigquery", "project": "p", "query": "SELECT 1", "secret_scope": "s", "destination_table": "t"})
    good = {"type": "http", "method": "POST", "url": "https://a.example.com/v1/x", "auth": "header", "auth_header": "X-Api-Key",
            "secret_scope": "s", "secret_key": "k", "body": {"a": "{{ params.p }}"}, "poll_url": "/v1/x/{$.id}",
            "status_path": "$.state", "success_values": ["done"]}
    assert model.validate({"name": "x", "params": [{"name": "p"}], "steps": [{"name": "s", **good}]})["valid"]
    # A for-each may repeat platform steps; YAML round-trips.
    d = model.normalize({"name": "x", "steps": [{"name": "each", "type": "for_each", "items": ["a", "b"],
                                                 "step": {"type": "http", "url": "https://a.example.com/{{ item }}"}}]})
    assert model.normalize(model.to_yaml(d)) == d
    assert {t["type"] for t in model.catalog() if t["group"] == "Platforms"} == {"http", "databricks", "snowflake", "bigquery"}


def test_the_flow_exports_with_airflows_own_providers():
    from cairn.flows.airflow_dag import to_dag

    root = os.path.join(os.path.dirname(__file__), "..")
    d = model.normalize(open(os.path.join(root, "examples", "flows", "cross-platform.yaml")).read())
    text = to_dag(d, owner=ANA, version=3)
    compile(text, "orders_scoring.py", "exec")
    # tell-team reads the Databricks run's page and the partner's import id, which Cairn keeps: those two steps (and
    # the notification) run in Cairn as the owner; Snowflake and the check are native.
    assert "SnowflakeSqlApiOperator(" in text and "step='score'" in text and "step='tell-partner'" in text
    assert "step='tell-team'" in text and "DatabricksRunNowOperator" not in text
    d["steps"][-1]["message"] = "Orders scored."
    text = to_dag(model.normalize(model.compact(d)), owner=ANA, version=3)
    compile(text, "orders_scoring.py", "exec")
    for needle in ("SnowflakeSqlApiOperator(", "DatabricksRunNowOperator(", "HttpOperator(", "HttpSensor(",
                   "snowflake_conn_id='snowflake_default'", "databricks_conn_id='databricks_default'",
                   "http_conn_id='http_tell_partner'", "with TaskGroup(group_id='tell-partner')", "job_id=811234567890",
                   "_cairn_poll('$.state', ['done'], ['failed', 'rejected'])", "statement_count=1",
                   "# http_tell_partner (type http): host https://api.partner.example.com"):
        assert needle in text, needle
    flat = " ".join(line.lstrip("# ") for line in text.splitlines())
    assert "extra private_key_content: <private_key from secret scope 'snowflake-prod'>" in flat  # where it is, not it
    d2 = model.normalize({"name": "bq", "steps": [{"name": "q", **BQ, "query": "SELECT 1", "airflow_conn_id": "gcp_prod"},
                                                  {"name": "once", **DBX, "host": "https://h", "after": ["q"],
                                                   "run": {"tasks": [{"task_key": "t"}]}}]})
    text = to_dag(d2, owner=ANA)
    assert "BigQueryInsertJobOperator(" in text and "gcp_conn_id='gcp_prod'" in text and "location='EU'" in text
    assert "DatabricksSubmitRunOperator(" in text and "'run_name': 'cairn once'" in text
    # Steps whose outputs other steps read stay in Cairn (Airflow's XCom is not the flow's output).
    d3 = model.normalize({"name": "r", "steps": [{"name": "q", **BQ, "query": "SELECT 1"},
                                                 {"name": "t", "type": "notify", "after": ["q"], "message": "{{ steps.q.rows }}"}]})
    assert "CairnFlowStepOperator(\n        task_id='q'" in to_dag(d3, owner=ANA) or "step='q'" in to_dag(d3, owner=ANA)
