"""Airflow 3 on the lakehouse, end to end, in the cairn/airflow image: a DAG runs SQL on Trino and a dbt
build as a pipeline user (``svc-airflow``, through a Cairn token) under Cairn's policies; Airflow's
OpenLineage events put the DAG, its tasks and their runs in Cairn; and people sign in to Airflow's UI
through Cairn, with their Cairn groups mapped to Airflow roles.

Needs docker, the cairn/airflow image (CAIRN_AIRFLOW_IMAGE) and the store and catalog binaries."""

import json
import os
import shutil
import subprocess
import time

import pytest

from test_trino_e2e import _ready, cairn, lake, trino  # noqa: F401  (fixtures)

IMAGE = os.environ.get("CAIRN_AIRFLOW_IMAGE", "")
pytestmark = pytest.mark.skipif(not (_ready() and IMAGE), reason="needs docker, CAIRN_AIRFLOW_IMAGE and the binaries")

PAT = "pat-svc-airflow-" + "y" * 20
USER = "urn:li:corpuser:svc-airflow"
ROOT = os.path.join(os.path.dirname(__file__), "..")

DAG = '''
from datetime import datetime

from airflow.sdk import DAG

from cairn_airflow.operators import CairnDbtOperator, CairnTrinoOperator

with DAG("lakehouse_daily", start_date=datetime(2026, 1, 1), schedule=None, catchup=False, tags=["lakehouse"]):
    daily = CairnTrinoOperator(
        task_id="daily_orders", schema="analytics",
        sql="DROP TABLE IF EXISTS daily_orders; "
            "CREATE TABLE daily_orders AS SELECT region, count(*) AS orders FROM sales.orders GROUP BY region; "
            "SELECT count(*) FROM daily_orders")
    build = CairnDbtOperator(task_id="dbt_build", project_dir="/project", command="build")
    daily >> build
'''


@pytest.fixture(scope="module")
def airflow_user(cairn):  # noqa: F811
    from cairn.governance.abac import Policy, PolicyBundle, Target

    _, s = cairn
    real = s.identity._me_lookup
    s.identity._me_lookup = lambda url, h, c: USER if h.get("Authorization") == f"Bearer {PAT}" else real(url, h, c)
    me = {"attr": "subject.urn", "op": "eq", "value": USER}
    bundle = s.pdp.bundle
    bundle = bundle() if callable(bundle) else bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies,
        Policy(id="af-build", effect="allow", condition=me,
               actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET"],
               target=Target(resource_urns=["*analytics.*"])),
        Policy(id="af-read", effect="allow", condition=me, actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"])),
        Policy(id="af-record", effect="allow", condition=me, actions=["EDIT_LINEAGE", "EDIT_ENTITY_ASSERTIONS"])]))
    yield USER
    s.identity._me_lookup = real


def run_airflow(tmp_path, cairn_url, trino, *cmd, env=None, detach=False, name=None, dag=DAG):  # noqa: F811
    dags = tmp_path / "dags"
    dags.mkdir(exist_ok=True)
    (dags / "lakehouse_daily.py").write_text(dag)
    project = tmp_path / "project"
    if not project.exists():
        shutil.copytree(os.path.join(ROOT, "examples", "dbt", "lakehouse"), project)
    for p in (dags, project):
        os.chmod(p, 0o755)
    conn = {"conn_type": "cairn", "host": cairn_url, "password": PAT,
            "extra": {"trino_host": "127.0.0.1", "trino_port": trino.tls, "trino_catalog": "lake", "ca_file": "/etc/cairn/ca.pem"}}
    transport = {"type": "http", "url": cairn_url, "endpoint": "openlineage/api/v1/lineage",
                 "auth": {"type": "api_key", "apiKey": PAT}}
    base = {"AIRFLOW_CONN_CAIRN_DEFAULT": json.dumps(conn), "AIRFLOW__OPENLINEAGE__TRANSPORT": json.dumps(transport),
            "CAIRN_DBT_SCHEMA": "analytics", "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags", **(env or {})}
    args = ["docker", "run", "--rm", "--network", "host", *(["-d", "--name", name] if detach else []),
            "-v", f"{dags}:/opt/airflow/dags:ro", "-v", f"{project}:/project:ro",
            "-v", f"{trino.dir}/ca.pem:/etc/cairn/ca.pem:ro", *[a for k, v in base.items() for a in ("-e", f"{k}={v}")],
            IMAGE, "bash", "-c", " && ".join(cmd)]
    return subprocess.run(args, capture_output=True, text=True, timeout=1200)


def test_a_dag_runs_on_the_lakehouse_as_its_user_with_lineage_in_cairn(trino, cairn, airflow_user, tmp_path):  # noqa: F811
    url, s = cairn
    try:
        trino("CREATE SCHEMA lake.analytics")
    except RuntimeError:
        pass
    out = run_airflow(tmp_path, url, trino, "airflow db migrate >/dev/null", "airflow dags test lakehouse_daily 2026-11-01")
    log = out.stdout + out.stderr
    assert out.returncode == 0, log[-6000:]
    assert "Running on Trino as svc-airflow" in log and "cairn dbt: recorded" in log, log[-6000:]
    # Both tasks built their tables, as svc-airflow.
    assert trino("SELECT region, orders FROM analytics.daily_orders ORDER BY region") == [
        {"region": "EU", "orders": 2}, {"region": "US", "orders": 1}]
    assert len(trino("SELECT * FROM analytics.region_names")) == 2
    # The dbt run was recorded in Cairn.
    model = s.trino_access.urn("lake", "analytics", "region_names")
    assert s.graph.get_aspect(model, "datasetProperties")["customProperties"]["dbt.unique_id"] == "model.lakehouse_example.region_names"
    # Airflow's OpenLineage events: the DAG is a pipeline, its tasks are jobs with successful runs.
    flow = "urn:li:dataFlow:(airflow,lakehouse_daily,PROD)"
    end = time.time() + 30
    while time.time() < end and not s.graph.get_aspect(flow, "dataFlowInfo"):
        time.sleep(0.5)
    assert s.graph.get_aspect(flow, "dataFlowInfo"), sorted({u for u, _ in s.graph.aspects if "dataFlow" in u})
    for task in ("daily_orders", "dbt_build"):
        job = f"urn:li:dataJob:({flow},{task})"
        assert s.graph.get_aspect(job, "dataJobInfo"), job
    runs = [(u, a) for (u, a), v in s.graph.aspects.items() if u.startswith("urn:li:dataProcessInstance:")
            and a == "dataProcessInstanceRunEvent"]
    assert runs or any(u.startswith("urn:li:dataProcessInstance:") for u, _ in s.graph.aspects)
    # Trino's own lineage: the table the task built reads sales.orders (datasets as the catalog names them).
    built = s.trino_access.urn("lake", "analytics", "daily_orders")

    def trino_jobs():
        return [v for (u, a), v in list(s.graph.aspects.items()) if a == "dataJobInputOutput" and built in json.dumps(v)]
    end = time.time() + 30
    while time.time() < end and not trino_jobs():
        time.sleep(0.5)
    assert trino_jobs(), [u for (u, a) in s.graph.aspects if a == "dataJobInputOutput"]
    assert s.trino_access.urn("lake", "sales", "orders") in json.dumps(trino_jobs())


def test_a_task_is_held_to_its_users_policies(trino, cairn, airflow_user, tmp_path):  # noqa: F811
    url, _ = cairn
    try:
        trino("CREATE SCHEMA lake.analytics")
    except RuntimeError:
        pass
    dag = DAG.replace('"lakehouse_daily"', '"forbidden"').replace(
        "SELECT region, count(*) AS orders FROM sales.orders GROUP BY region", "SELECT name, salary FROM hr.salaries")
    out = run_airflow(tmp_path, url, trino, "airflow db migrate >/dev/null", "airflow dags test forbidden 2026-11-01", dag=dag)
    log = out.stdout + out.stderr
    assert "Access Denied" in log, "\n".join(ln for ln in log.splitlines() if "rror" in ln or "denied" in ln.lower())[-4000:]
    assert "cairn dbt:" not in log and out.returncode != 0, log[-4000:]  # the dbt task never ran


def test_people_sign_in_to_airflow_through_cairn(trino, cairn, tmp_path):  # noqa: F811
    """Airflow's UI sends people to Cairn; Cairn returns a code; Airflow redeems it with its client secret
    and maps the person's Cairn groups to roles. Someone in no mapped group gets no role with access."""
    import requests

    url, s = cairn
    secret = "airflow-oauth-secret-" + "z" * 24
    from test_trino_e2e import port
    ap = port()
    callback = f"https://127.0.0.1:{ap}/auth/oauth-authorized/cairn"
    s.trino_oauth.add_client("airflow", secret, [callback], "airflow")
    s.graph.aspects[("urn:li:corpuser:ana", "groupMembership")] = {"groups": ["urn:li:corpGroup:data-engineers"]}
    s.attributes.invalidate("urn:li:corpuser:ana")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "webserver_config.py").write_text("from cairn_airflow.auth import configure\nconfigure(globals())\n")
    shutil.copy(f"{trino.dir}/tls.pem", tmp_path / "web" / "tls.pem")
    os.chmod(tmp_path / "web" / "tls.pem", 0o644)
    name = f"cairn-airflow-sso-{os.getpid()}"
    env = {"CAIRN_URL": url, "CAIRN_PUBLIC_URL": url, "CAIRN_AIRFLOW_ALLOW_HTTP": "true", "CAIRN_AIRFLOW_OAUTH_SECRET": secret,
           "CAIRN_AIRFLOW_ROLES": json.dumps({"data-engineers": ["User"]}),
           "AIRFLOW__API__BASE_URL": f"https://127.0.0.1:{ap}", "AIRFLOW__API__PORT": str(ap),
           "AIRFLOW__API__SSL_CERT": "/opt/airflow/web/tls.pem", "AIRFLOW__API__SSL_KEY": "/opt/airflow/web/tls.pem",
           "AIRFLOW__FAB__CONFIG_FILE": "/opt/airflow/web/webserver_config.py",
           "AIRFLOW__API_AUTH__JWT_SECRET": "j" * 32, "AIRFLOW__CORE__FERNET_KEY": "", "PYTHONWARNINGS": "ignore"}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    web = tmp_path / "web"
    args = ["docker", "run", "-d", "--name", name, "--network", "host", "-v", f"{web}:/opt/airflow/web:ro",
            *[a for k, v in env.items() for a in ("-e", f"{k}={v}")], IMAGE, "bash", "-c",
            "airflow db migrate >/dev/null && airflow api-server"]
    subprocess.run(args, check=True, capture_output=True, timeout=120)
    try:
        sess = requests.Session()
        sess.trust_env = False
        sess.verify = False
        base = f"https://127.0.0.1:{ap}"
        end = time.time() + 240
        while True:
            try:
                if sess.get(f"{base}/api/v2/monitor/health", timeout=5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if time.time() > end:
                logs = subprocess.run(["docker", "logs", "--tail", "60", name], capture_output=True, text=True)
                raise AssertionError(logs.stdout + logs.stderr)
            time.sleep(2)
        def sign_in(who):
            """Airflow sends the browser to Cairn (client "airflow"); signed in to Cairn as ``who`` (the test API
            trusts this header), the browser comes back with a code; Airflow redeems it and checks Cairn's
            signature."""
            sess = requests.Session()
            sess.trust_env = False
            sess.verify = False
            r = sess.get(f"{base}/auth/login/cairn", allow_redirects=False, timeout=15)
            assert r.status_code in (302, 303), r.text[:500]
            auth = r.headers["location"]
            assert auth.startswith(f"{url}/cairn/api/v1/trino/oauth/authorize?") and "client_id=airflow" in auth, auth
            r = requests.get(auth.replace(f"{url}/cairn/api", url), headers={"X-Cairn-Actor": f"urn:li:corpuser:{who}"},
                             allow_redirects=False, timeout=15)
            assert r.status_code == 302 and r.headers["location"].startswith(callback + "?"), r.text[:500]
            r = sess.get(r.headers["location"], allow_redirects=False, timeout=30)
            assert r.status_code in (302, 303), r.text[:2000]
            assert "/auth/login" not in r.headers.get("location", ""), r.headers.get("location")  # signed in

        def roles(who):
            users = subprocess.run(["docker", "exec", name, "airflow", "users", "list", "--output", "json"],
                                   capture_output=True, text=True, timeout=120)
            listed = json.loads(users.stdout[users.stdout.index("["):])
            u = next(u for u in listed if u["username"] == who)
            return {r.strip() for r in (u["roles"] if isinstance(u["roles"], list) else u["roles"].split(","))}

        sign_in("ana")
        assert "User" in roles("ana") and "Admin" not in roles("ana")  # from her Cairn group data-engineers
        sign_in("bob")
        assert roles("bob") == {"Public"}  # in no mapped group: no access
        assert any(e["action"] == "airflow.sign_in" for e in s.audit.entries(category="identity"))
        # A forged token (signed by anything but Cairn) never signs anyone in.
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        forged = pyjwt.encode({"sub": "mallory", "aud": "airflow", "iss": "cairn", "exp": int(time.time()) + 600,
                               "groups": ["data-engineers"]}, rsa.generate_private_key(public_exponent=65537, key_size=2048),
                              algorithm="RS256", headers={"kid": "nope"})
        check = subprocess.run(["docker", "exec", "-e", f"CAIRN_URL={url}", name, "python", "-c",
                                f"from cairn_airflow.auth import user_info; user_info({forged!r})"],
                               capture_output=True, text=True, timeout=60)
        assert check.returncode != 0 and "InvalidTokenError" in check.stderr + check.stdout
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
