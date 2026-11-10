"""Managed Airflow (L144) on the Docker runtime, end to end, with a real Airflow 3.3 image, Cairn's real API, Cairn
Store (the DAG bundles), Cairn Catalog and Trino:

* a deployment whose secret scope cannot be read is refused before anything is made;
* a dev deployment comes up (metadata database, API server, scheduler, DAG processor, triggerer) with no local admin;
  people sign in through Cairn with its own OAuth client, groups mapped to roles; its code is refused by another
  deployment's client;
* a DAG-only deploy lands without restarting anything, and its DAG runs: one task calls Cairn and reads a variable
  from the secret scope, the other reads a table on Trino, both as the deployment's pipeline user (workload
  identity); the task logs come through Airflow's API; a DAG that does not import is refused; a second deploy and a
  rollback to the first;
* the deploy is promoted to a stage deployment that requires approval: the requester cannot approve it, an approver
  does, and it lands there;
* hibernation outside the deployment's hours (everything stopped) and waking it; a scope it can no longer read keeps
  it asleep;
* an upgrade whose pre-check finds a DAG that does not parse on the new version is refused, and goes through once
  the DAG is fixed; an image built on another Airflow is refused;
* deletion removes containers, volumes and the workload binding.

Needs docker, the store and catalog binaries (the Trino fixtures of tests/test_trino_e2e.py) and an Airflow image with
cairn-airflow-provider (CAIRN_AIRFLOW_IMAGE: cairn/airflow in CI)."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import secrets
import subprocess
import time

import httpx
import pytest

from test_trino_e2e import STORE_KEY, _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

IMAGE = os.environ.get("CAIRN_AIRFLOW_IMAGE", "")
pytestmark = pytest.mark.skipif(not (_ready() and IMAGE), reason="needs docker, CAIRN_AIRFLOW_IMAGE and the binaries")
NEXT = f"cairn-test/airflow-next:{os.getpid()}"         # "the next Airflow": the same image, with a removed API
TEAM = f"cairn-test/team-airflow:{os.getpid()}"         # a team image built FROM cairn/airflow
ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
DEV, STAGE = "afdev", "afstage"

LAKE_REPORT = '''
from datetime import datetime

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Variable

from cairn_airflow.hooks import CairnHook
from cairn_airflow.operators import CairnTrinoOperator


def whoami():
    me = CairnHook().api("GET", "/me")
    print("CAIRN_IDENTITY=" + me["urn"])
    print("SECRET_MATCHES=" + str(Variable.get("warehouse_password") == "s3cr3t-from-the-scope"))


with DAG("lake_report", start_date=datetime(2026, 1, 1), schedule=None, catchup=False):
    who = PythonOperator(task_id="whoami", python_callable=whoami)
    count = CairnTrinoOperator(task_id="count_orders", sql="SELECT count(*) FROM sales.orders")
    who >> count
'''
LEGACY = '''
import os
from datetime import datetime

from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

if os.environ.get("CAIRN_TEST_NEXT_AIRFLOW"):
    raise ImportError("airflow.legacy_api was removed in this Airflow")

with DAG("legacy", start_date=datetime(2026, 1, 1), schedule=None):
    EmptyOperator(task_id="noop")
'''
SECOND = '''
from datetime import datetime

from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

with DAG("second_dag", start_date=datetime(2026, 1, 1), schedule=None):
    EmptyOperator(task_id="noop")
'''


def bundle(tmp_path, files: dict) -> bytes:
    from cairn.airflow_deploy.bundle import pack

    d = tmp_path / f"dags-{secrets.token_hex(3)}"
    d.mkdir()
    for k, v in files.items():
        (d / k).write_text(v)
    return pack(str(d))


@pytest.fixture(scope="module")
def af_env(lake, tmp_path_factory):  # noqa: F811
    """Cairn's settings for managed Airflow, before the Cairn API fixture builds its services."""
    for tag, extra in ((NEXT, "ENV CAIRN_TEST_NEXT_AIRFLOW=1"), (TEAM, "LABEL team=finance")):
        subprocess.run(["docker", "build", "-q", "-t", tag, "-"], input=f"FROM {IMAGE}\n{extra}\n", text=True,
                       check=True, capture_output=True, timeout=300)
    d = tmp_path_factory.mktemp("afpins")
    (d / "pins.json").write_text(json.dumps({"airflow": {"deployments": ["3.3.2", "3.3.9"]}}))
    env = {"CAIRN_AIRFLOW_RUNTIME": "docker", "CAIRN_AIRFLOW_DEPLOY_KEY": "airflow-deploy-key-" + "q" * 24,
           "CAIRN_AIRFLOW_CAIRN_URL": "http://host.docker.internal:1",          # the real port is set below
           "CAIRN_AIRFLOW_DOCKER_IMAGES": json.dumps({"3.3.2": IMAGE, "3.3.9": NEXT}),
           "CAIRN_AIRFLOW_BUNDLE_STORE": json.dumps({"endpoint": lake["store"], "bucket": "airflow-deploys"}),
           "CAIRN_AIRFLOW_BUNDLE_ACCESS_KEY": STORE_KEY[0], "CAIRN_AIRFLOW_BUNDLE_SECRET_KEY": STORE_KEY[1],
           "CAIRN_SECRETS_KEY": base64.b64encode(os.urandom(32)).decode(), "CAIRN_VERSION_PINS": str(d / "pins.json"),
           "CAIRN_AIRFLOW_DOCKER_PORTS": f"{19000 + os.getpid() % 500}-{19600}"}
    os.environ.update(env)
    yield env
    for k in env:
        os.environ.pop(k, None)
    for tag in (NEXT, TEAM):
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def af(af_env, cairn, trino):  # noqa: F811
    from cairn.governance.abac import Policy, PolicyBundle, Target

    url, s = cairn
    svc = s.airflow_deployments
    assert svc is not None, "managed Airflow was not built"
    p = url.rsplit(":", 1)[1]
    svc.cairn_url = f"http://host.docker.internal:{p}"
    svc.public_url = url
    svc.trino = {"host": "host.docker.internal", "port": trino.tls, "catalog": "lake"}
    svc.ca_pem = open(f"{trino.dir}/ca.pem", "rb").read()
    bundle_ = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle_.policies,
        Policy(id="ana-approves", effect="allow", actions=["APPROVE_AIRFLOW_PROMOTION", "VIEW_AIRFLOW_DEPLOYMENT"],
               condition={"attr": "subject.urn", "op": "eq", "value": ANA}),
        Policy(id="pipeline-reads-sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"]),
               condition={"attr": "subject.urn", "op": "eq", "value": f"urn:li:corpuser:svc-af-{DEV}"})]))
    s.graph.aspects[(ANA, "groupMembership")] = {"groups": ["urn:li:corpGroup:data-engineers"]}
    s.attributes.invalidate(ANA)
    ss = s.secret_scopes
    ss.create(ADMIN, {"name": "af-dev"})
    ss.put(ADMIN, "af-dev", "warehouse_password", "s3cr3t-from-the-scope")
    ss.set_acl(ADMIN, "af-dev", f"urn:li:corpuser:svc-af-{DEV}", "READ")
    yield {"url": url, "s": s, "svc": svc, "trino": trino}
    for name in (DEV, STAGE, "afnone"):
        try:
            svc.delete(ADMIN, name)
        except Exception:  # noqa: BLE001 - already gone
            pass
    subprocess.run("docker ps -aq --filter label=cairn.io/airflow-deployment | xargs -r docker rm -f", shell=True,
                   capture_output=True, timeout=120)


def api(af, method, path, who=ADMIN, **kw):
    return httpx.request(method, f"{af['url']}{path}", headers={"X-Cairn-Actor": who, **kw.pop("headers", {})},
                         timeout=900, **kw)


def containers(name: str) -> list[str]:
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"label=cairn.io/airflow-deployment={name}",
                          "--format", "{{.Names}}"], capture_output=True, text=True, timeout=60).stdout
    return sorted(out.split())


def started(name: str) -> dict:
    out = {}
    for c in ("api-server", "scheduler", "dag-processor", "triggerer"):
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", f"cairn-af-{name}-{c}"],
                           capture_output=True, text=True, timeout=60)
        out[c] = r.stdout.strip()
    return out


def airflow_cli(name: str, *cmd: str, component: str = "scheduler") -> str:
    r = subprocess.run(["docker", "exec", f"cairn-af-{name}-{component}", "bash", "-c",
                        "set -a; . /opt/cairn/airflow.env; set +a; " + " ".join(cmd)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r.stdout


def wait_dag(af, name: str, dag_id: str, present: bool = True, timeout: float = 180):
    svc = af["svc"]
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            got = svc._api(svc._row(name), "GET", f"/api/v2/dags/{dag_id}")
            last = got
            if present and not got.get("is_stale"):
                return got
            if not present and got.get("is_stale"):
                return got
        except Exception as e:  # noqa: BLE001 - not parsed yet
            last = e
            if not present and "404" in str(e):
                return None
        time.sleep(3)
    raise AssertionError(f"{dag_id} on {name}: {last}")


def run_dag(af, name: str, dag_id: str, run_id: str, timeout: float = 300) -> dict:
    airflow_cli(name, "airflow dags unpause", dag_id, ">/dev/null")
    airflow_cli(name, "airflow dags trigger", dag_id, "-r", run_id, ">/dev/null")
    svc = af["svc"]
    end = time.time() + timeout
    while time.time() < end:
        r = svc._api(svc._row(name), "GET", f"/api/v2/dags/{dag_id}/dagRuns/{run_id}")
        if r.get("state") in ("success", "failed"):
            return r
        time.sleep(3)
    raise AssertionError(f"{dag_id} {run_id} did not finish")


def test_a_deployment_whose_secret_scope_cannot_be_read_is_not_made(af):
    r = api(af, "POST", "/v1/airflow/deployments", json={
        "name": "afnone", "environment": "dev",
        "secrets": [{"env": "WAREHOUSE_TOKEN", "scope": "no-such-scope", "key": "token"}]})
    assert r.status_code == 409 and "no-such-scope/token" in r.json()["detail"], r.text
    assert containers("afnone") == [] and api(af, "GET", "/v1/airflow/deployments/afnone").status_code == 404
    # A value in the spec is refused outright: secrets are references.
    r = api(af, "POST", "/v1/airflow/deployments", json={"name": "afnone", "env": {"DB_PASSWORD": "x"}})
    assert r.status_code == 400 and "secret scope" in r.json()["detail"]


def test_a_dev_deployment_end_to_end(af, tmp_path):
    s, svc = af["s"], af["svc"]
    r = api(af, "POST", "/v1/airflow/deployments", json={
        "name": DEV, "environment": "dev", "team": "data-engineers", "promotes_to": STAGE,
        "roles": {"data-engineers": "User"}, "env": {"REPORT_REGION": "EU"},
        "secrets": [{"variable": "warehouse_password", "scope": "af-dev", "key": "warehouse_password"}]})
    assert r.status_code == 201, r.text
    dep = r.json()
    assert dep["state"] == "ready" and dep["pipeline_user"] == f"urn:li:corpuser:svc-af-{DEV}", dep
    assert set(containers(DEV)) == {f"cairn-af-{DEV}-{c}" for c in ("api-server", "scheduler", "dag-processor",
                                                                    "triggerer", "db")}
    st = api(af, "GET", f"/v1/airflow/deployments/{DEV}/status").json()
    assert st["health"] == "healthy", st
    assert st["components"]["scheduler"]["status"] == "healthy" and st["scheduler_heartbeat_age_s"] < 120
    assert st["import_errors"] == [] and st["tasks"] == {"queued": 0, "running": 0}
    # The secret's value is nowhere in the container's configuration (a file only Airflow's user reads).
    inspect = subprocess.run(["docker", "inspect", f"cairn-af-{DEV}-scheduler"], capture_output=True, text=True).stdout
    assert "s3cr3t-from-the-scope" not in inspect and "FERNET" not in inspect
    # Its workload identity: bound to the pipeline user.
    assert any(b["user"] == f"urn:li:corpuser:svc-af-{DEV}" and b["issuer"] == "cairn-airflow"
               for b in s.workload.bindings())

    # ---- sign-in through Cairn, no local admin ----------------------------------------------------------------
    import requests

    base, url = dep["url"], af["url"]
    sess = requests.Session()
    sess.trust_env = False
    r = sess.get(f"{base}/auth/login/cairn", allow_redirects=False, timeout=30)
    assert r.status_code in (302, 303), r.text[:500]
    auth = r.headers["location"]
    assert f"client_id=airflow-{DEV}" in auth and auth.startswith(f"{url}/cairn/api/v1/trino/oauth/authorize?"), auth
    r = requests.get(auth.replace(f"{url}/cairn/api", url), headers={"X-Cairn-Actor": ANA}, allow_redirects=False,
                     timeout=30)
    assert r.status_code == 302 and r.headers["location"].startswith(f"{base}/auth/oauth-authorized/cairn?"), r.text
    r = sess.get(r.headers["location"], allow_redirects=False, timeout=60)
    assert r.status_code in (302, 303) and "/auth/login" not in r.headers.get("location", ""), r.text[:2000]
    users = json.loads((out := airflow_cli(DEV, "airflow users list -o json 2>/dev/null"))[out.index("["):])
    roles = {u["username"]: set(u["roles"] if isinstance(u["roles"], list) else u["roles"].split(",")) for u in users}
    ana = [u for u in roles if u.rsplit(":", 1)[-1] == "ana"]
    assert ana and "User" in roles[ana[0]], roles                       # her Cairn group's role
    assert not any("Admin" in rs for rs in roles.values()), roles          # no local admin, nobody made Admin
    assert roles["cairn-ops"] == {"Op"}                                   # Cairn's own, for status and flows
    # A code issued to this deployment's client is not redeemed by another client.
    from cairn.dataaccess.trino_oauth import OAuthError

    code = s.trino_oauth.issue_code(ANA, f"{base}/auth/oauth-authorized/cairn", "", "", f"airflow-{DEV}", ())
    with pytest.raises(OAuthError):
        s.trino_oauth.exchange(code, f"{base}/auth/oauth-authorized/cairn", "airflow", "x" * 24, "")

    # ---- a DAG-only deploy, without a restart ------------------------------------------------------------------
    before = started(DEV)
    first = bundle(tmp_path, {"lake_report.py": LAKE_REPORT, "legacy.py": LEGACY})
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys?note=first&git_sha=abc1234", content=first,
            headers={"Content-Type": "application/gzip"})
    assert r.status_code == 201, r.text
    d1 = r.json()
    assert d1["kind"] == "dags" and d1["state"] == "live" and d1["git_sha"] == "abc1234"
    assert sorted(d1["checked"]["dags"]) == ["lake_report", "legacy"] and d1["checked"]["import_errors"] == {}
    wait_dag(af, DEV, "lake_report")
    assert started(DEV) == before                        # nothing restarted
    run = run_dag(af, DEV, "lake_report", "cairn-test-1")
    if run["state"] != "success":
        logs = []
        for t in ("whoami", "count_orders"):
            try:
                logs.append(svc.task_log(ADMIN, DEV, "lake_report", "cairn-test-1", t)["log"][-3000:])
            except Exception as e:  # noqa: BLE001 - shown
                logs.append(f"{t}: {e}")
        raise AssertionError("\n".join(logs))
    who = api(af, "GET", f"/v1/airflow/deployments/{DEV}/task-logs",
              params={"dag_id": "lake_report", "run_id": "cairn-test-1", "task_id": "whoami"}).json()["log"]
    assert f"CAIRN_IDENTITY=urn:li:corpuser:svc-af-{DEV}" in who and "SECRET_MATCHES=True" in who, who[-3000:]
    assert "s3cr3t-from-the-scope" not in who
    count = api(af, "GET", f"/v1/airflow/deployments/{DEV}/task-logs",
                params={"dag_id": "lake_report", "run_id": "cairn-test-1", "task_id": "count_orders"}).json()["log"]
    assert f"Running on Trino as svc-af-{DEV}" in count, count[-3000:]
    assert any(e["action"] == "workload.token_issued" and e["actor"] == f"urn:li:corpuser:svc-af-{DEV}"
               for e in s.audit.entries(category="identity"))
    logs = api(af, "GET", f"/v1/airflow/deployments/{DEV}/logs", params={"component": "scheduler", "tail": 50}).json()
    assert logs["log"]

    # A DAG that does not import is refused; what runs stays.
    bad = bundle(tmp_path, {"lake_report.py": LAKE_REPORT, "broken.py": "from airflow.sdk import DAG\n\nwith DAG(\"broken\":\n"})
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys", content=bad, headers={"Content-Type": "application/gzip"})
    assert r.status_code == 422 and "broken.py" in r.json()["check"]["import_errors"], r.text
    assert svc._row(DEV).dags_release == d1["id"]

    # A second deploy, then a rollback to the first.
    second = bundle(tmp_path, {"lake_report.py": LAKE_REPORT, "legacy.py": LEGACY, "second_dag.py": SECOND})
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys", content=second, headers={"Content-Type": "application/gzip"})
    assert r.status_code == 201, r.text
    wait_dag(af, DEV, "second_dag")
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys/{d1['id']}/rollback")
    assert r.status_code == 201, r.text
    back = r.json()
    assert back["source"] == f"rollback:{d1['id']}" and back["sha256"] == d1["sha256"]
    ls = airflow_cli(DEV, "ls /opt/airflow/dags/current/", component="dag-processor")
    assert "second_dag.py" not in ls and "lake_report.py" in ls
    assert started(DEV) == before
    states = [(d["seq"], d["state"]) for d in api(af, "GET", f"/v1/airflow/deployments/{DEV}/deploys").json()["deploys"]]
    assert states[0][1] == "live" and ("refused" in dict(states).values())

    # ---- promotion to stage, with an approval -----------------------------------------------------------------
    r = api(af, "POST", "/v1/airflow/deployments", json={"name": STAGE, "environment": "stage",
                                                        "approval": {"required": True},
                                                        "resources": {"scheduler": {"memory_mb": 1536}}})
    assert r.status_code == 201, r.text
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/promote", json={"comment": "ready for stage"})
    assert r.status_code == 201 and r.json()["state"] == "pending", r.text
    pid = r.json()["id"]
    assert api(af, "POST", f"/v1/airflow/promotions/{pid}/approve").status_code == 403    # not the requester
    r = api(af, "POST", f"/v1/airflow/promotions/{pid}/approve", who=ANA, json={"comment": "ok"})
    assert r.status_code == 200 and r.json()["state"] == "applied", r.text
    landed = svc.deploys(ADMIN, STAGE)[0]
    assert landed["sha256"] == d1["sha256"] and landed["source"] == f"promoted:{DEV}/{back['id']}"
    assert landed["created_by"] == ANA
    wait_dag(af, STAGE, "lake_report")

    # ---- as code: a bundle and Terraform; the exported bundle plans to no change, a changed one to an update --
    y = api(af, "GET", "/v1/iac/export", params={"format": "yaml", "kind": "airflow_deployments", "id": STAGE})
    assert y.status_code == 200 and "airflow_deployments:" in y.text and STAGE in y.text, y.text[:800]
    tf = api(af, "GET", "/v1/iac/export", params={"format": "terraform", "kind": "airflow_deployments", "id": STAGE})
    assert 'resource "cairn_airflow_deployment"' in tf.text, tf.text[:800]
    plan = api(af, "POST", "/v1/iac/changes/plan", json={"bundle_yaml": y.text})
    assert plan.status_code == 200, plan.text
    mine = [c for c in plan.json()["changes"] if c["kind"] == "airflow_deployments"]
    assert all(c["action"] == "keep" for c in mine), mine                  # nothing to do (kept ones may be left out)
    import yaml

    doc = yaml.safe_load(y.text)
    doc["airflow_deployments"][0]["env"] = {"REPORT_REGION": "US"}
    plan = api(af, "POST", "/v1/iac/changes/plan", json={"bundle_yaml": yaml.safe_dump(doc)}).json()
    assert [c["action"] for c in plan["changes"] if c["kind"] == "airflow_deployments"] == ["update"], plan

    # ---- hibernation and waking --------------------------------------------------------------------------------
    today = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[dt.datetime.now(dt.timezone.utc).weekday()]
    other = "sun" if today != "sun" else "mon"
    r = api(af, "PATCH", f"/v1/airflow/deployments/{DEV}", json={
        "hibernation": {"awake": [{"days": [other], "from": "08:00", "to": "09:00"}], "timezone": "UTC"}})
    assert r.status_code == 200 and started(DEV) == before, r.text            # a schedule restarts nothing
    assert svc.run_due() >= 1
    assert svc._row(DEV).state == "hibernating"
    assert all(c["running"] is False for c in svc.runtime.state({"name": DEV})["components"])
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/wake", json={"minutes": 30})
    assert r.status_code == 200 and r.json()["state"] == "ready", r.text
    svc.run_due()
    assert svc._row(DEV).state == "ready"                                  # awake for its 30 minutes
    wait_dag(af, DEV, "lake_report")
    # A scope it can no longer read: it does not start.
    api(af, "POST", f"/v1/airflow/deployments/{DEV}/hibernate").raise_for_status()
    s.secret_scopes.set_acl(ADMIN, "af-dev", f"urn:li:corpuser:svc-af-{DEV}", None)
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/wake")
    assert r.status_code == 409 and "af-dev/warehouse_password" in r.json()["detail"], r.text
    assert svc._row(DEV).state == "hibernating"
    assert all(c["running"] is False for c in svc.runtime.state({"name": DEV})["components"])
    s.secret_scopes.set_acl(ADMIN, "af-dev", f"urn:li:corpuser:svc-af-{DEV}", "READ")
    # A flow that needs it wakes it (a flow's airflow step naming the deployment), and starts a DAG run there.
    from cairn.flows.steps import Airflow

    client = Airflow(svc.flow_airflow(ADMIN, DEV))
    assert svc._row(DEV).state == "ready"
    wait_dag(af, DEV, "lake_report")
    client.call("POST", "/dags/lake_report/dagRuns", {"dag_run_id": "from-a-flow", "logical_date": None})
    assert client.call("GET", "/dags/lake_report/dagRuns/from-a-flow")["dag_run_id"] == "from-a-flow"

    # ---- an upgrade, with its pre-check -----------------------------------------------------------------------
    r = api(af, "PATCH", f"/v1/airflow/deployments/{DEV}", json={"airflow_version": "3.3.9"})
    assert r.status_code == 409 and "legacy.py" in r.json()["check"]["import_errors"], r.text
    assert svc._row(DEV).image == IMAGE                                    # still on the old one
    fixed = bundle(tmp_path, {"lake_report.py": LAKE_REPORT})
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys?note=drop%20legacy", content=fixed,
            headers={"Content-Type": "application/gzip"})
    assert r.status_code == 201, r.text
    r = api(af, "PATCH", f"/v1/airflow/deployments/{DEV}", json={"airflow_version": "3.3.9"})
    assert r.status_code == 200 and r.json()["airflow_version"] == "3.3.9" and r.json()["image"] == NEXT, r.text
    assert api(af, "GET", f"/v1/airflow/deployments/{DEV}/status").json()["health"] == "healthy"
    wait_dag(af, DEV, "lake_report")
    assert any(e["action"] == "airflow_deployment.upgrade_refused" for e in s.audit.entries(category="pipelines"))

    # ---- image deploys: a team image on the deployment's Airflow, and one that is not -------------------------
    r = api(af, "POST", f"/v1/airflow/deployments/{DEV}/deploys", json={"image": TEAM, "note": "finance libs"})
    assert r.status_code == 409 and "3.3.2" in r.json()["detail"], r.text   # built FROM 3.3.2; dev runs 3.3.9
    r = api(af, "POST", f"/v1/airflow/deployments/{STAGE}/deploys", json={"image": TEAM, "git_sha": "def5678"})
    assert r.status_code == 201 and r.json()["kind"] == "image", r.text
    assert svc._row(STAGE).custom_image == TEAM
    assert api(af, "GET", f"/v1/airflow/deployments/{STAGE}/status").json()["health"] == "healthy"
    wait_dag(af, STAGE, "lake_report")                                     # its DAGs came back with it

    # ---- deletion ---------------------------------------------------------------------------------------------
    for name in (DEV, STAGE):
        assert api(af, "DELETE", f"/v1/airflow/deployments/{name}").status_code == 204
        assert containers(name) == []
    vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "label=cairn.io/airflow-deployment"],
                          capture_output=True, text=True).stdout.split()
    assert not [v for v in vols if DEV in v or STAGE in v]
    assert not any(b["user"].startswith("urn:li:corpuser:svc-af-") for b in s.workload.bindings())
    names = [e["action"] for e in s.audit.entries(category="pipelines")]
    for a in ("airflow_deployment.created", "airflow_deployment.deployed", "airflow_deployment.deploy_refused",
              "airflow_deployment.rolled_back", "airflow_deployment.promotion_requested",
              "airflow_deployment.promotion_approved", "airflow_deployment.promoted", "airflow_deployment.hibernated",
              "airflow_deployment.woken", "airflow_deployment.wake_refused", "airflow_deployment.woken_for_flow",
              "airflow_deployment.upgraded",
              "airflow_deployment.deleted"):
        assert a in names, a
