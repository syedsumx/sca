"""Everything as code (L125) for the ten resource types that need a system outside Cairn: workload bindings,
Parquet tables, mirrors, serving tables, volumes and their grants, external catalogs, model deployments, SLOs and
report schedules. Each is declared in a bundle (planned, applied, planned again with no changes, changed in place,
pruned) and by real Terraform with the provider built from source (created, no drift, changed in place, imported,
destroyed), and each time checked in the outside system itself:

* workload bindings: a real kube-apiserver (and etcd) issues a service account token that Cairn exchanges only
  while the binding exists;
* Parquet tables: files in the real Cairn Store, registered in the real Cairn Catalog, queried through Trino;
* mirrors: a PostgreSQL 16 server with wal_level = logical (the publication and replication slot come and go);
* serving tables: a lakehouse table copied into PostgreSQL, read by key;
* volumes: a bucket in Cairn Store; their grants: what the grantee sees;
* external catalogs: another organisation's Cairn Catalog and Store, attached to Trino and queried through it;
* model deployments: a model trained and registered in MLflow through Cairn, served by MLflow's model server;
* SLOs: a data product's objective, measured;
* report schedules: a dashboard on Trino, run and mailed to a real SMTP server (Mailpit).

Needs docker (Trino, PostgreSQL, Mailpit, the Terraform image), Go, CAIRN_STORE_BIN, CAIRN_CATALOG_BIN,
CAIRN_K8S_BIN_DIR (kube-apiserver, etcd, kubectl) and CAIRN_MLFLOW_PYTHON (mlflow and scikit-learn)."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import textwrap
import threading
import time

import httpx
import pytest
import yaml

from test_external_catalogs_e2e import partner  # noqa: F401  (fixture)
from test_trino_e2e import (CATALOG_TOKEN, KEY, OAUTH_SECRET, STORE_KEY, ModelStub, _ready, lake,  # noqa: F401
                            port, trino, wait)
from test_workload_identity_e2e import BIN as K8S_BIN
from test_workload_identity_e2e import k8s  # noqa: F401  (fixture)

TERRAFORM = os.environ.get("CAIRN_TERRAFORM_IMAGE", "hashicorp/terraform:1.13.4")
PROVIDER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "terraform-provider-cairn")
MLFLOW_PY = os.environ.get("CAIRN_MLFLOW_PYTHON", "")
MAILPIT = os.environ.get("CAIRN_MAILPIT_IMAGE", "axllent/mailpit:v1.21.8")
TOKEN = "iac-outside-" + "t" * 24
ADMIN = "urn:li:corpuser:admin"
ANA = "urn:li:corpuser:ana"
SA = "system:serviceaccount:pipelines:svc-dbt"
PRODUCT = "urn:li:dataProduct:finance-orders"
ACCOUNTS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.accounts,PROD)"
TYPES = ("workload_bindings", "parquet_tables", "mirrors", "serving_tables", "volumes", "volume_grants",
         "external_catalogs", "model_deployments", "slos", "report_schedules")


def _all_ready() -> bool:
    if not _ready() or not shutil.which("go"):
        return False
    if not (K8S_BIN and os.path.exists(os.path.join(K8S_BIN, "kube-apiserver"))):
        return False
    if not (MLFLOW_PY and os.path.exists(MLFLOW_PY)):
        return False
    return subprocess.run(["docker", "image", "inspect", TERRAFORM], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _all_ready(), reason=(
    f"needs docker, go, {TERRAFORM}, CAIRN_STORE_BIN, CAIRN_CATALOG_BIN, CAIRN_K8S_BIN_DIR and CAIRN_MLFLOW_PYTHON"))


def _http_up(url: str, seconds: float = 90) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"{url} did not come up")


# ---- the outside systems ----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def outside(lake, tmp_path_factory):  # noqa: F811
    """PostgreSQL (logical decoding on: mirrors' source and the serving tables' database), Mailpit and MLflow."""
    d = tmp_path_factory.mktemp("outside")
    pg, mail = f"cairn-iac-pg-{os.getpid()}", f"cairn-iac-mail-{os.getpid()}"
    password = "iac-" + os.urandom(8).hex()
    (d / "pg.env").write_text(f"POSTGRES_PASSWORD={password}\n")       # never on a command line
    pgp, smtp, mailp, mlp = port(), port(), port(), port()
    subprocess.run(["docker", "rm", "-f", pg, mail], capture_output=True, timeout=60)
    procs: list = []
    try:
        subprocess.run(["docker", "run", "-d", "--rm", "--name", pg, "--env-file", str(d / "pg.env"),
                        "-p", f"127.0.0.1:{pgp}:5432", "postgres:16-alpine", "-c", "wal_level=logical"],
                       check=True, capture_output=True, timeout=300)
        subprocess.run(["docker", "run", "-d", "--rm", "--name", mail, "-p", f"127.0.0.1:{mailp}:8025",
                        "-p", f"127.0.0.1:{smtp}:1025", MAILPIT], check=True, capture_output=True, timeout=300)
        bindir = os.path.dirname(MLFLOW_PY)
        procs.append(subprocess.Popen(
            [f"{bindir}/mlflow", "server", "--backend-store-uri", f"sqlite:///{d}/mlflow.db",
             "--artifacts-destination", f"{d}/artifacts", "--serve-artifacts", "--host", "127.0.0.1",
             "--port", str(mlp), "--workers", "1"], stdout=open(d / "mlflow.log", "w"), stderr=subprocess.STDOUT))
        import psycopg

        url = f"postgresql://postgres:{password}@127.0.0.1:{pgp}/postgres"
        end = time.time() + 90
        while True:
            try:
                psycopg.connect(url, connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                assert time.time() < end, "PostgreSQL did not start"
                time.sleep(0.5)
        admin = d / "admin-url"
        admin.write_text(url)
        os.chmod(admin, 0o600)
        _http_up(f"http://127.0.0.1:{mailp}/api/v1/messages", 60)
        end = time.time() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", smtp), 2).close()
                break
            except OSError:
                assert time.time() < end, "Mailpit's SMTP did not start"
                time.sleep(0.5)
        _http_up(f"http://127.0.0.1:{mlp}/health")
        yield {"pg_url": url, "pg_admin_file": str(admin), "smtp": f"smtp://127.0.0.1:{smtp}?starttls=false",
               "mailpit": f"http://127.0.0.1:{mailp}", "mlflow": f"http://127.0.0.1:{mlp}", "dir": d}
    finally:
        for p in procs:
            p.terminate()
            p.wait(15)
        subprocess.run(["docker", "rm", "-f", pg, mail], capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def cairn(k8s, lake, outside, tmp_path_factory):  # noqa: F811
    """The Cairn API on HTTP with every service these types need switched on, pointed at the systems above (the
    workload issuer is the kube-apiserver the k8s fixture runs). Trino (the trino fixture) is Cairn's lakehouse."""
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    catalog = {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
               "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
               "s3.path-style-access": "true"}
    (tmp / "store-secret").write_text(STORE_KEY[1])
    model = ModelStub()
    env = {"CAIRN_TRINO_ACCESS_KEY": KEY, "CAIRN_TRINO_OAUTH_SECRET": OAUTH_SECRET,
           "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "iceberg", "database": ""}}),
           "CAIRN_TRINO_OAUTH_REDIRECT_URIS": "https://127.0.0.1/oauth2/callback",
           "CAIRN_AI_SQL_UPSTREAM": f"http://127.0.0.1:{model.port}", "CAIRN_AI_SQL_PROVIDER": "openai",
           "CAIRN_AI_SQL_API_KEY": "provider-key-held-by-cairn", "CAIRN_AI_SQL_MODELS": "stub-model",
           "CAIRN_LAKEHOUSE_TRINO_CATALOG": "lake",
           "CAIRN_PIPES_CATALOG": json.dumps(catalog),
           "CAIRN_PIPES_STORE": json.dumps({"endpoint": lake["store"], "access_key": STORE_KEY[0],
                                            "secret_key": STORE_KEY[1], "region": "us-east-1"}),
           "CAIRN_PARQUET_BUCKETS": "lakehouse",
           "CAIRN_STREAMS_ENABLED": "true",
           "CAIRN_STORE_URL": lake["store"], "CAIRN_STORE_ADMIN_ACCESS_KEY": STORE_KEY[0],
           "CAIRN_STORE_ADMIN_SECRET_KEY_FILE": str(tmp / "store-secret"),
           "CAIRN_SERVING_CATALOG": json.dumps(catalog), "CAIRN_DATABASES_ADMIN_URL_FILE": outside["pg_admin_file"],
           "CAIRN_MLFLOW_URL": outside["mlflow"], "CAIRN_ML_SERVING_RUNTIME": "process",
           "CAIRN_ML_SERVING_MLFLOW": os.path.join(os.path.dirname(MLFLOW_PY), "mlflow"),
           "CAIRN_ML_SERVING_LOG_DIR": str(tmp),
           "CAIRN_EXTERNAL_CATALOGS_TRINO_CONNECTION": "lakehouse"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        graph = InMemoryGraph()
        graph.aspects[(ADMIN, "corpUserInfo")] = {"email": "admin@test.example", "displayName": "Admin"}
        graph.aspects[(ACCOUNTS, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "email", "region")]}
        s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                                    dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")},
                                    active={"smtp_url": outside["smtp"], "email_from": "cairn@test.example"}),
                           graph=graph, with_ai=False, with_gateway=False)
    finally:
        for k, v in saved.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    s.model_stub = model
    for name in ("workload", "parquet_tables", "mirrors", "serving_tables", "volumes", "external_catalogs",
                 "ml", "ml_serving", "slos", "report_schedules"):
        assert getattr(s, name, None) is not None, f"{name} is off"
    s.ml_serving.tick_s, s.ml_serving.cold_s = 1.0, 150.0
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    real = s.identity._me_lookup
    s.identity._me_lookup = lambda u, h, c: ADMIN if (h.get("Authorization") or "") == f"Bearer {TOKEN}" else real(u, h, c)
    s.mirrors.source_url = {"crm": outside["pg_url"]}.get       # the connection the mirrors name
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="0.0.0.0", port=p, timeout_keep_alive=75,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    url = f"http://127.0.0.1:{p}"
    s.external_catalogs.broker_url = url
    yield url, s
    for d in list(getattr(s.ml_serving.runtime, "procs", {}) or {}):
        s.ml_serving.runtime.scale({"id": d}, 0)
    server.should_exit = True


TRAIN = textwrap.dedent('''
    import json, mlflow, pandas as pd
    from sklearn.linear_model import LogisticRegression
    df = pd.DataFrame({"tenure": [1, 2, 3, 20, 30, 40], "spend": [5, 7, 6, 50, 60, 70], "churned": [1, 1, 1, 0, 0, 0]})
    mlflow.set_experiment("churn")
    with mlflow.start_run():
        model = LogisticRegression().fit(df[["tenure", "spend"]], df["churned"])
        mlflow.sklearn.log_model(model, name="model", registered_model_name="churn",
                                 input_example=df[["tenure", "spend"]].head(2))
''')


@pytest.fixture(scope="module")
def world(k8s, outside, cairn, trino, partner, lake):  # noqa: F811
    """What the ten declarations refer to, made in the outside systems: Parquet files in the store, a PostgreSQL
    table with a primary key, a lakehouse table, a registered model, a dashboard, another organisation's catalog."""
    import psycopg
    import pyarrow as pa
    import pyarrow.parquet as pq
    from pyarrow import fs as pafs

    from cairn.iac.bundle import Client
    from cairn.notify.hub import ChannelSettings
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    # Parquet files already in the store (registered in place, never copied).
    store = pafs.S3FileSystem(access_key=STORE_KEY[0], secret_key=STORE_KEY[1], region="us-east-1",
                              endpoint_override=lake["store"].split("://", 1)[1], scheme="http")
    schema = pa.schema([("id", pa.int64()), ("amount", pa.float64())])
    for i in range(2):
        with store.open_output_stream(f"lakehouse/drop/events/part-{i}.parquet") as out:
            pq.write_table(pa.Table.from_pylist([{"id": i * 10 + j, "amount": 1.5 * j} for j in range(5)], schema=schema), out)
    # The mirror's source: a PostgreSQL table with a primary key.
    with psycopg.connect(outside["pg_url"], autocommit=True) as c:
        c.execute("CREATE TABLE public.customers (id int PRIMARY KEY, name text)")
        c.execute("INSERT INTO public.customers VALUES (1, 'Ana'), (2, 'Bob')")
    # The serving table's source: a lakehouse table.
    trino("CREATE SCHEMA IF NOT EXISTS lake.sales")
    trino("CREATE TABLE lake.sales.accounts AS SELECT * FROM (VALUES (1, 'a@acme.com', 'EU'), (2, 'b@acme.com', 'US'), "
          "(3, 'c@acme.com', 'EU')) AS t(id, email, region)")
    # A model trained and registered in MLflow through Cairn, as the admin.
    r = subprocess.run([MLFLOW_PY, "-c", TRAIN], cwd=str(outside["dir"]), capture_output=True, text=True, timeout=600,
                       env={**os.environ, "MLFLOW_TRACKING_URI": f"{url}/v1/ml", "MLFLOW_TRACKING_TOKEN": TOKEN,
                            "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0"})
    assert r.returncode == 0, r.stderr[-3000:]
    http = httpx.Client(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=300)
    # A dashboard on Trino, for the report; its owner reads email.
    board = http.post(f"{url}/sql/dashboards", json={
        "name": "Accounts by region", "shared": True,
        "tiles": [{"element": {"source": {"table": "sales.accounts"}, "group_by": ["region"],
                               "aggregates": [{"name": "Accounts", "formula": "Count([id])"}],
                               "sort": [{"column": "region"}]},
                   "connection": "lakehouse", "title": "By region"}]})
    assert board.status_code < 300, board.text
    st = s.notify.get_settings(ADMIN)
    st.channels["email"] = ChannelSettings(enabled=True)
    s.notify.put_settings(ADMIN, st)
    os.environ["CAIRN_EXTCAT_PARTNER"] = "partner-catalog-" + "t" * 24     # the partner's token, by reference
    w = {"url": url, "s": s, "trino": trino, "http": http, "client": Client(url, "", http=http), "board": board.json()["id"],
         "partner": partner, "lake": lake, "outside": outside, "k8s": k8s}
    yield w
    os.environ.pop("CAIRN_EXTCAT_PARTNER", None)


def declarations(w) -> dict[str, tuple[dict, dict | None]]:
    """Each type's resource and its change in place (None: every field forces a new one)."""
    return {
        "workload_bindings": ({"issuer": "k8s", "subject": SA, "user": "urn:li:corpuser:svc-dbt",
                               "conditions": {"kubernetes.io.namespace": "pipelines"}}, None),
        "parquet_tables": ({"name": "events", "bucket": "lakehouse", "prefix": "drop/events/", "target": "files.events"},
                           {"every_minutes": 30}),
        "mirrors": ({"name": "customers", "connection": "crm", "table": "public.customers",
                     "target": "crm_mirror.customers"}, None),
        "serving_tables": ({"name": "accounts", "table": "sales.accounts", "primary_key": ["id"]}, None),
        "volumes": ({"name": "finance-files", "description": "Finance's files"}, None),
        "volume_grants": ({"volume": "finance-files", "principal": ANA, "access": "read"}, {"access": "write"}),
        "external_catalogs": ({"name": "partner", "uri": w["partner"]["catalog"], "auth": "token",
                               "credential_ref": "env:CAIRN_EXTCAT_PARTNER",
                               "store_endpoint": w["partner"]["store"]}, {"writable": True}),
        "model_deployments": ({"name": "churn-prod", "model": "churn", "version": "1", "min_replicas": 0,
                               "max_replicas": 1, "target_concurrency": 1.0, "idle_minutes": 5},
                              {"max_replicas": 2}),
        "slos": ({"id": "orders-fresh", "product_urn": PRODUCT, "name": "Orders are fresh", "kind": "freshness",
                  "objective": 99.0}, {"objective": 95.0}),
        "report_schedules": ({"name": "Accounts weekly", "target": {"dashboard": w["board"]}, "format": "csv",
                              "every_minutes": 10080}, {"every_minutes": 1440}),
    }


# ---- what each declaration did in the outside system --------------------------------------------------------------
def _exchange(w) -> int:
    sa = w["k8s"]["kubectl"]("-n", "pipelines", "create", "token", "svc-dbt", "--audience", "cairn", "--duration", "10m")
    return httpx.post(f"{w['url']}/v1/auth/workload-token", timeout=20, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": sa,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"}).status_code


def _pg(w, sql: str) -> list:
    import psycopg

    with psycopg.connect(w["outside"]["pg_url"], autocommit=True) as c:
        return c.execute(sql).fetchall()


def _catalogs(w) -> set:
    return {r["Catalog"] for r in w["trino"]("SHOW CATALOGS")}


def _buckets(w) -> set:
    from pyarrow import fs as pafs

    store = pafs.S3FileSystem(access_key=STORE_KEY[0], secret_key=STORE_KEY[1], region="us-east-1",
                              endpoint_override=w["lake"]["store"].split("://", 1)[1], scheme="http")
    return {i.path for i in store.get_file_info(pafs.FileSelector("/"))}


def _files(w, prefix: str) -> list:
    from pyarrow import fs as pafs

    store = pafs.S3FileSystem(access_key=STORE_KEY[0], secret_key=STORE_KEY[1], region="us-east-1",
                              endpoint_override=w["lake"]["store"].split("://", 1)[1], scheme="http")
    return [i.path for i in store.get_file_info(pafs.FileSelector(prefix, recursive=True))
            if i.type == pafs.FileType.File]


def _mails(w) -> list:
    return httpx.get(f"{w['outside']['mailpit']}/api/v1/messages", timeout=10).json()["messages"]


def present(w, changed: bool = False) -> None:
    """Every declared resource is real where it lives."""
    s, http, url = w["s"], w["http"], w["url"]
    assert _exchange(w) == 200                                                        # workload binding
    assert w["trino"]("SELECT count(*) AS n FROM lake.files.events") == [{"n": 10}]   # Parquet table
    assert len(_pg(w, "SELECT 1 FROM pg_publication WHERE pubname LIKE '%'")) >= 1    # mirror
    assert len(_pg(w, "SELECT slot_name FROM pg_replication_slots")) == 1
    r = http.get(f"{url}/v1/serving/accounts/rows", params={"key": "2"})               # serving table
    assert r.status_code == 200 and r.json()["rows"][0]["email"] == "b@acme.com", r.text
    assert "cairn-vol-finance-files" in _buckets(w)                                   # volume
    ana = httpx.get(f"{url}/v1/volumes/finance-files", headers={"X-Cairn-Actor": ANA}, timeout=20)   # its grant
    assert ana.status_code == 200 and ana.json()["access"] == ("write" if changed else "read"), ana.text
    assert "partner" in _catalogs(w)                                                  # external catalog
    assert w["trino"]("SELECT count(*) AS n FROM partner.sales.orders") == [{"n": 3}]
    dep = http.get(f"{url}/v1/ml/deployments/churn-prod")                             # model deployment
    assert dep.status_code == 200 and dep.json()["max_replicas"] == (2 if changed else 1), dep.text
    slo = http.get(f"{url}/marketplace/slos", params={"product": PRODUCT})        # SLO, measured
    assert slo.status_code == 200 and "orders-fresh" in slo.text, slo.text
    reports = http.get(f"{url}/sql/reports").json()                                   # report schedule
    (rep,) = [x for x in (reports.get("reports") if isinstance(reports, dict) else reports) if x["name"] == "Accounts weekly"]
    assert rep["every_minutes"] == (1440 if changed else 10080)
    before = len(_mails(w))
    sent = http.post(f"{url}/sql/reports/{rep['id']}/run")
    assert sent.status_code == 200 and len(sent.json()["sent"]) == 1, sent.text
    end = time.time() + 30
    while len(_mails(w)) == before:                   # Cairn's notification dispatcher sends what is pending
        assert time.time() < end, "the report was not mailed"
        s.notify.dispatch_pending()
        time.sleep(0.5)
    msg = _mails(w)[0]
    assert msg["To"][0]["Address"] == "admin@test.example" and msg["Attachments"] == 1, msg


def absent(w) -> None:
    """Nothing is left where the resources lived."""
    http, url = w["http"], w["url"]
    assert _exchange(w) != 200
    # A Parquet table's registration goes; the table and its files stay (zero copy, by design). Dropped from the
    # catalog here (metadata only) so the next declaration registers it again; the files are still there.
    assert "events" not in http.get(f"{url}/v1/lakehouse/parquet-tables").text
    assert w["trino"]("SELECT count(*) AS n FROM lake.files.events") == [{"n": 10}]
    from pyiceberg.catalog.rest import RestCatalog

    RestCatalog("t", uri=w["lake"]["catalog"], token=CATALOG_TOKEN).drop_table("files.events")
    assert len(_files(w, "lakehouse/drop/events/")) == 2
    assert _pg(w, "SELECT slot_name FROM pg_replication_slots") == []
    assert _pg(w, "SELECT pubname FROM pg_publication") == []
    assert http.get(f"{url}/v1/serving/accounts").status_code == 404
    assert "cairn-vol-finance-files" not in _buckets(w)
    assert "partner" not in _catalogs(w)
    assert http.get(f"{url}/v1/ml/deployments/churn-prod").status_code == 404
    assert "orders-fresh" not in http.get(f"{url}/marketplace/slos", params={"product": PRODUCT}).text
    reports = http.get(f"{url}/sql/reports").json()
    assert not [x for x in (reports.get("reports") if isinstance(reports, dict) else reports) if x["name"] == "Accounts weekly"]


def _invoke(w) -> None:
    """The declared deployment serves the registered model (MLflow's model server, started on the first call)."""
    r = w["http"].post(f"{w['url']}/v1/ml/deployments/churn-prod/invocations",
                       json={"dataframe_split": {"columns": ["tenure", "spend"], "data": [[2, 6], [35, 65]]}})
    assert r.status_code == 200 and r.json()["predictions"] == [1, 0], r.text


# ---- through a bundle ---------------------------------------------------------------------------------------------
def test_the_outside_types_round_trip_through_a_bundle(world, tmp_path):
    from cairn.iac import bundle as b
    from cairn.iac import registry

    w = world
    decl = declarations(w)
    assert set(decl) == set(TYPES) and all(registry.kind(t) for t in TYPES)
    path = tmp_path / "cairn.yaml"
    doc = {"bundle": "outside", **{k: [spec] for k, (spec, _) in decl.items()}}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    changes, state = b.plan(w["client"], b.load(str(path)))
    assert {x.kind for x in changes if x.action == "create"} == set(TYPES), [x.line() for x in changes]
    b.apply(w["client"], b.load(str(path)), changes, state, log=lambda _: None)
    present(w)
    _invoke(w)
    # Planned again: nothing to do; the export of these types plans the same.
    assert [x.line() for x in b.plan(w["client"], b.load(str(path)))[0] if x.action != "keep"] == []
    from cairn.iac import export

    exported = tmp_path / "exported.yaml"
    exported.write_text(export.export(w["client"].call, "yaml", list(TYPES), bundle="outside"))
    assert [x.line() for x in b.plan(w["client"], b.load(str(exported)))[0] if x.action != "keep"] == []
    # Changed in git: the changes are applied in place.
    for k, (_, upd) in decl.items():
        if upd:
            doc[k][0].update(upd)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    changes, state = b.plan(w["client"], b.load(str(path)))
    assert {x.kind for x in changes if x.action == "update"} == {k for k, (_, u) in decl.items() if u}, \
        [x.line() for x in changes if x.action != "keep"]
    assert not [x for x in changes if x.action in ("create", "delete", "replace")]
    b.apply(w["client"], b.load(str(path)), changes, state, log=lambda _: None)
    present(w, changed=True)
    assert {x.action for x in b.plan(w["client"], b.load(str(path)))[0]} == {"keep"}
    # Dropped from git and pruned: gone from Cairn and from the outside systems.
    path.write_text(yaml.safe_dump({"bundle": "outside"}))
    changes, state = b.plan(w["client"], b.load(str(path)), prune=True)
    assert {x.kind for x in changes if x.action == "delete"} == set(TYPES), [x.line() for x in changes]
    b.apply(w["client"], b.load(str(path)), changes, state, log=lambda _: None)
    absent(w)


# ---- through Terraform --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tf(world, tmp_path_factory):
    d = tmp_path_factory.mktemp("tf-outside")
    prov, work = d / "prov", d / "work"
    prov.mkdir()
    work.mkdir()
    subprocess.run(["go", "build", "-o", str(prov / "terraform-provider-cairn"), "."], cwd=PROVIDER_SRC, check=True,
                   env={**os.environ, "CGO_ENABLED": "0"}, timeout=900)
    (work / "tf.rc").write_text('provider_installation {\n  dev_overrides { "cairn/cairn" = "/prov" }\n  direct {}\n}\n')

    def run(*args, ok=(0,)):
        r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{work}:/work", "-v", f"{prov}:/prov:ro",
                            "-w", "/work", "-e", "TF_CLI_CONFIG_FILE=/work/tf.rc", "-e", "TF_IN_AUTOMATION=1",
                            "-e", f"CAIRN_URL={world['url']}", "-e", "CAIRN_TOKEN", TERRAFORM, *args, "-no-color"],
                           capture_output=True, text=True, timeout=900, env={**os.environ, "CAIRN_TOKEN": TOKEN})
        changes = [x for x in r.stdout.splitlines() if "~" in x or "->" in x or "will be" in x or "must be" in x]
        assert r.returncode in ok, (r.returncode, changes[:60], r.stdout[-3000:], r.stderr[-6000:])
        return r.stdout
    run.work = work
    return run


def test_the_outside_types_are_managed_by_terraform(world, tf):
    from cairn.iac import export, registry
    from cairn.iac.engine import Engine, tf_id

    w = world
    decl = declarations(w)
    kinds = {t: registry.kind(t) for t in TYPES}

    def config(update: bool) -> None:
        items = [(kinds[t], {**spec, **((upd or {}) if update else {})}) for t, (spec, upd) in decl.items()]
        (tf.work / "main.tf").write_text(export.to_terraform(items, declared_values=True))

    config(False)
    # The grant names its volume by reference: Terraform makes the volume first and removes it last.
    assert "= cairn_volume.finance_files.name" in (tf.work / "main.tf").read_text()
    out = tf("apply", "-auto-approve")
    assert f"{len(TYPES)} added, 0 changed, 0 destroyed" in out, out[-2000:]
    present(w)
    tf("plan", "-detailed-exitcode")                                  # no drift
    config(True)
    out = tf("apply", "-auto-approve")
    n = sum(1 for _, u in decl.values() if u)
    assert f"0 added, {n} changed, 0 destroyed" in out, out[-2000:]
    present(w, changed=True)
    tf("plan", "-detailed-exitcode")
    # Forgotten by Terraform and imported again by id: no change.
    e = Engine(w["client"].call)
    state = json.loads(tf("show", "-json"))
    addrs = {r["type"]: r["address"] for r in state["values"]["root_module"]["resources"]}
    for t in TYPES:
        k = kinds[t]
        spec = decl[t][0]
        recs = e.records(k, {p: spec[p] for p in k.parents} if k.parents else None)
        from cairn.iac import bundle as b

        (rec,) = [r for r in recs if b._rec_key(k, r) == b.key_of(k, spec)]
        addr = addrs[f"cairn_{k.tf}"]
        tf("state", "rm", addr)
        tf("import", addr, tf_id(k, rec))
    tf("plan", "-detailed-exitcode")
    tf("destroy", "-auto-approve")
    absent(w)
    assert not re.search(r"cairn-vol-", " ".join(_buckets(w)))
