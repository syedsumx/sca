"""The Cairn Terraform provider (terraform-provider-cairn), built from source and run by real Terraform
against the Cairn API: create, no drift, change in place (same ids), recreate what was deleted outside
Terraform, import, destroy. Needs Go, docker and the hashicorp/terraform image."""

import json
import os
import shutil
import subprocess
import textwrap
import threading

import pytest

from test_trino_e2e import port, wait

TERRAFORM = os.environ.get("CAIRN_TERRAFORM_IMAGE", "hashicorp/terraform:1.13.4")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "terraform-provider-cairn")
TOKEN = "tf-token-" + "t" * 24
ADMIN = "urn:li:corpuser:admin"


def _ready():
    if not (shutil.which("go") and shutil.which("docker")):
        return False
    return subprocess.run(["docker", "image", "inspect", TERRAFORM], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason=f"needs go, docker and {TERRAFORM}")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    d = tmp_path_factory.mktemp("tf")
    prov = d / "prov"
    prov.mkdir()
    subprocess.run(["go", "build", "-o", str(prov / "terraform-provider-cairn"), "."], cwd=SRC, check=True,
                   env={**os.environ, "CGO_ENABLED": "0"}, timeout=900)
    env = {"CAIRN_PIPES_STORE": json.dumps({"endpoint": "http://127.0.0.1:1"}),
           "CAIRN_PIPES_CATALOG": json.dumps({"uri": "http://127.0.0.1:1"}),
           "CAIRN_STREAMS_ENABLED": "true", "CAIRN_MAINTENANCE_CONNECTION": "lakehouse"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        s = build_services(Settings(database_url=f"sqlite:///{d}/cairn.db", service_token="svc-secret",
                                    gateway={"key_dir": str(d / "keys")}), graph=InMemoryGraph(), with_ai=False,
                           with_gateway=False)
    finally:
        for k, v in old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    # A small warehouse, so metrics (checked by compiling them) have a real table to be checked against.
    import sqlalchemy

    with sqlalchemy.create_engine(f"sqlite:///{d}/wh.db").begin() as c:
        c.execute(sqlalchemy.text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
    from cairn.quality.collectors import ConnectionRegistry

    s.data_access.registry = ConnectionRegistry({"wh": {"url": f"sqlite:///{d}/wh.db", "catalog": {"platform": "sqlite"}}})
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    real = s.identity._me_lookup
    s.identity._me_lookup = lambda url, h, c: ADMIN if (h.get("Authorization") or "") == f"Bearer {TOKEN}" else real(url, h, c)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    work = d / "work"
    work.mkdir()
    (work / "tf.rc").write_text('provider_installation {\n  dev_overrides { "cairn/cairn" = "/prov" }\n  direct {}\n}\n')
    yield {"s": s, "work": work, "prov": prov, "url": f"http://127.0.0.1:{p}"}
    server.should_exit = True


def tf(w, *args, ok=(0,)):
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{w['work']}:/work", "-v", f"{w['prov']}:/prov:ro",
                        "-w", "/work", "-e", "TF_CLI_CONFIG_FILE=/work/tf.rc", "-e", "TF_IN_AUTOMATION=1",
                        "-e", f"CAIRN_URL={w['url']}", "-e", f"CAIRN_TOKEN={TOKEN}", TERRAFORM, *args, "-no-color"],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode in ok, (r.returncode, r.stdout[-4000:], r.stderr[-4000:])
    return r


CONFIG = """
terraform {
  required_providers {
    cairn = { source = "cairn/cairn" }
  }
}
provider "cairn" {}

resource "cairn_pipe" "orders" {
  name   = "orders-files"
  bucket = "landing"
  prefix = "orders/"
  format = "csv"
  target = "raw.orders"
  create_table = true
}

resource "cairn_stream" "customers" {
  name         = "customers-cdc"
  topic        = "pg.public.customers"
  format       = "debezium"
  key          = ["id"]
  target       = "cdc.customers"
  create_table = true
  batch_rows   = BATCH
}

resource "cairn_derived_table" "revenue" {
  name      = "revenue"
  target    = "marts.revenue"
  on_change = true
  sql       = "SELECT region, sum(amount) AS total FROM raw.orders WHERE WHERE_ GROUP BY region"
}

resource "cairn_maintenance_policy" "all" {
  scope       = "*"
  expire_days = 7
  orphan_days = 7
}
"""


def config(w, batch=5000, where="amount > 0"):
    (w["work"] / "main.tf").write_text(textwrap.dedent(CONFIG.replace("BATCH", str(batch)).replace("WHERE_", where)))


def test_terraform_manages_cairn_resources(world):
    w, s = world, world["s"]
    config(w)
    tf(w, "apply", "-auto-approve")
    assert [p["name"] for p in s.pipes.list()] == ["orders-files"]
    st = s.streams.list()[0]
    assert (st["mode"], st["key"], st["batch_rows"]) == ("upsert", ["id"], 5000)        # Cairn decided the mode
    d = s.derived.list()[0]
    assert d["owner"] == ADMIN and "amount > 0" in d["sql"]
    assert s.maintenance.policy_for("raw", "orders")["expire_days"] == 7
    # No drift: a plan right after has nothing to do.
    tf(w, "plan", "-detailed-exitcode")
    # A change is made in place: the same ids (the stream keeps its offsets).
    config(w, batch=100, where="amount > 1")
    out = tf(w, "apply", "-auto-approve").stdout
    assert "0 added, 2 changed, 0 destroyed" in out, out
    assert s.streams.list()[0]["id"] == st["id"] and s.streams.list()[0]["batch_rows"] == 100
    assert s.derived.list()[0]["id"] == d["id"] and "amount > 1" in s.derived.list()[0]["sql"]
    # Deleted outside Terraform: the next plan creates it again.
    s.pipes.delete(s.pipes.list()[0]["id"], ADMIN)
    tf(w, "plan", "-detailed-exitcode", ok=(2,))
    tf(w, "apply", "-auto-approve")
    assert [p["name"] for p in s.pipes.list()] == ["orders-files"]
    # Cairn's refusals come back as Terraform errors.
    (w["work"] / "bad.tf").write_text('resource "cairn_derived_table" "bad" {\n  name = "bad"\n  target = "marts.bad"\n'
                                      '  every_minutes = 5\n  sql = "DELETE FROM raw.orders"\n}\n')
    r = tf(w, "apply", "-auto-approve", ok=(1,))
    assert "one SELECT" in r.stderr, r.stderr
    (w["work"] / "bad.tf").unlink()
    # Something made by hand is imported, then managed.
    s.maintenance.set_policy("raw", {"every_hours": 12}, ADMIN)
    (w["work"] / "raw.tf").write_text('resource "cairn_maintenance_policy" "raw" {\n  scope = "raw"\n  every_hours = 12\n}\n')
    tf(w, "import", "cairn_maintenance_policy.raw", "raw")
    tf(w, "plan", "-detailed-exitcode")
    # Destroy removes everything Terraform manages, and nothing else.
    s.pipes.create({"name": "by-hand", "bucket": "landing", "format": "json", "target": "raw.x"}, ADMIN)
    tf(w, "destroy", "-auto-approve")
    assert [p["name"] for p in s.pipes.list()] == ["by-hand"]
    assert s.streams.list() == [] and s.derived.list() == [] and s.maintenance.policies() == []


SQL_CONFIG = """
terraform {
  required_providers {
    cairn = { source = "cairn/cairn" }
  }
}
provider "cairn" {}

data "cairn_saved_query" "by_hand" {
  name = "by-hand"
}

resource "cairn_derived_table" "revenue" {
  name      = "revenue"
  target    = "marts.revenue"
  on_change = true
  sql       = "SELECT region, sum(amount) AS total FROM raw.orders GROUP BY region"
}

resource "cairn_saved_query" "revenue" {
  name       = "revenue-by-region"
  connection_name = "lakehouse"
  sql        = "SELECT * FROM marts.revenue"
  chart      = jsonencode({ type = "bar", x = "region", y = ["total"] })
}

resource "cairn_workbook" "orders" {
  name            = "orders-wb"
  connection_name = "wh"
  spec = jsonencode({ elements = [{
    source = { table = "main.orders" }, group_by = ["region"], aggregates = [{ name = "n", formula = "Count()" }]
  }] })
}

resource "cairn_metric" "revenue" {
  name            = "tf_revenue"
  label           = "Revenue"
  connection_name = "wh"
  table           = "main.orders"
  measure         = "Sum([amount])"
  dimensions      = ["region"]
}

resource "cairn_dashboard" "sales" {
  name   = "sales"
  shared = true
  tiles  = jsonencode([{ query = cairn_saved_query.revenue.id, width = 2 }, { query = data.cairn_saved_query.by_hand.id, title = "TITLE" },
                       { metric = { metrics = [cairn_metric.revenue.name], dimensions = ["region"] }, title = "Revenue" }])
}

resource "cairn_sql_alert" "drop" {
  name  = "revenue-drop"
  query = cairn_saved_query.revenue.id
  measure = "total"
  op    = "<"
  value = VALUE
}

resource "cairn_sql_workflow" "nightly" {
  name = "nightly"
  cron = "0 2 * * *"
  steps = jsonencode([
    { name = "refresh", type = "derived", table = cairn_derived_table.revenue.name },
    { name = "not-empty", type = "check", query = cairn_saved_query.revenue.id, op = ">", value = 0, after = ["refresh"] },
  ])
}

resource "cairn_vector_index" "articles" {
  name          = "articles"
  source        = "support.articles"
  key           = "id"
  text          = "body"
  every_minutes = 60
}
"""


def test_terraform_manages_sql_resources_and_vector_indexes(world):
    from pyiceberg.exceptions import NoSuchTableError

    w, s = world, world["s"]
    for f in w["work"].glob("*.tf"):
        f.unlink()
    hand = s.sql_workspace.save(ADMIN, {"name": "by-hand", "connection": "lakehouse", "sql": "SELECT 1"})

    def config2(value=1000, title="Hand"):
        (w["work"] / "main.tf").write_text(SQL_CONFIG.replace("VALUE", str(value)).replace("TITLE", title))

    config2()
    tf(w, "apply", "-auto-approve")
    q = next(x for x in s.sql_workspace.saved(ADMIN) if x["name"] == "revenue-by-region")
    assert q["chart"] == {"type": "bar", "x": "region", "y": ["total"]}
    board = s.sql_boards.dashboards(ADMIN)[0]
    assert [(t.get("query"), t["title"], t["width"]) for t in board["tiles"]] == [
        (q["id"], "revenue-by-region", 2), (hand["id"], "Hand", 1), (None, "Revenue", 1)]   # the data source found the hand-made query
    assert board["tiles"][2]["metric"]["metrics"] == ["tf_revenue"]
    wb = next(x for x in s.workbooks.list(ADMIN) if x["name"] == "orders-wb")
    assert wb["connection"] == "wh" and wb["spec"]["elements"][0]["group_by"] == ["region"]
    assert s.metrics.get("tf_revenue")["measure"] == "Sum([amount])"
    assert s.sql_boards.alerts(ADMIN)[0]["value"] == 1000
    wf = s.sql_workflows.list(ADMIN)[0]
    assert [x["name"] for x in wf["steps"]] == ["refresh", "not-empty"] and wf["cron"] == "0 2 * * *"
    assert s.vectors.get("articles")["every_minutes"] == 60
    # Cairn's additions to the JSON (ids, default titles, measures) are not drift.
    tf(w, "plan", "-detailed-exitcode")
    config2(value=500, title="By hand")
    out = tf(w, "apply", "-auto-approve").stdout
    assert "0 added, 2 changed, 0 destroyed" in out, out
    assert s.sql_boards.dashboards(ADMIN)[0]["id"] == board["id"] and s.sql_boards.alerts(ADMIN)[0]["value"] == 500
    # Destroy removes what Terraform made; the hand-made query stays. (This world has no catalog: the
    # index's vectors table, which destroy also drops, was never built.)
    class NoCatalog:
        def purge_table(self, ident):
            raise NoSuchTableError(ident)
    s.vectors._catalog = NoCatalog
    tf(w, "destroy", "-auto-approve")
    assert [x["name"] for x in s.sql_workspace.saved(ADMIN)] == ["by-hand"]
    assert not s.sql_boards.dashboards(ADMIN) and not s.sql_boards.alerts(ADMIN) and not s.sql_workflows.list(ADMIN)
    assert not s.workbooks.list(ADMIN) and not [m for m in s.metrics.list() if m["name"] == "tf_revenue"]
    assert s.vectors.list() == []


ACCESS_CONFIG = """
terraform {
  required_providers {
    cairn = { source = "cairn/cairn" }
  }
}
provider "cairn" {}

resource "cairn_policy" "analysts" {
  policy_id = "analysts-read"
  effect    = "allow"
  priority  = PRIORITY
  actions   = ["VIEW_DATASET_PROFILE", "USE_WAREHOUSE"]
  target    = jsonencode({resource_types = ["dataset", "warehouse"]})
  condition = jsonencode({attr = "subject.groups", op = "contains", value = "analysts"})
}

resource "cairn_connection" "finance" {
  name      = "finance-db"
  connector = "postgres"
  values    = jsonencode({host_port = "finance-db:5432", database = "DATABASE", username = "reader", password = "FINANCE_PG_PASSWORD"})
  schedule  = jsonencode({interval = "0 2 * * *", timezone = "UTC"})
}

data "cairn_policy" "admin" {
  policy_id = "admin"
}

data "cairn_connection" "finance" {
  name       = "finance-db"
  depends_on = [cairn_connection.finance]
}

output "admin_actions" {
  value = data.cairn_policy.admin.actions
}

output "finance_urn" {
  value = data.cairn_connection.finance.id
}
"""


def test_terraform_manages_policies_and_connections(world):
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_sources import FakeCore

    from cairn.governance.abac import PolicyBundle

    w, s = world, world["s"]
    core = FakeCore()
    s.graph.graphql_handler = core
    for f in w["work"].glob("*.tf"):
        f.unlink()

    def config(priority=10, database="finance"):
        (w["work"] / "main.tf").write_text(ACCESS_CONFIG.replace("PRIORITY", str(priority)).replace("DATABASE", database))

    config()
    out = tf(w, "apply", "-auto-approve").stdout
    assert '"*"' in out and "urn:li:dataHubIngestionSource:" in out, out       # both data sources read
    pol = next(x for x in s.pdp.bundle.policies if x.id == "analysts-read")
    assert pol.priority == 10 and pol.target.resource_types == ["dataset", "warehouse"]
    assert [x.id for x in s.pdp.bundle.policies] == ["admin", "analysts-read"]           # the rest stays
    src = next(iter(core.sources.values()))
    assert json.loads(src["config"]["recipe"])["source"]["config"]["password"] == "${FINANCE_PG_PASSWORD}"
    tf(w, "plan", "-detailed-exitcode")                                                  # Cairn's defaults are not drift
    # Changed in place: the policy keeps its id, the connection its URN.
    config(priority=20, database="finance_v2")
    out = tf(w, "apply", "-auto-approve").stdout
    assert "0 added, 2 changed, 0 destroyed" in out, out
    assert next(x for x in s.pdp.bundle.policies if x.id == "analysts-read").priority == 20
    assert list(core.sources) == [src["urn"]]
    assert json.loads(core.sources[src["urn"]]["config"]["recipe"])["source"]["config"]["database"] == "finance_v2"
    # Deleted outside Terraform: put back. Imported by id: no change.
    s.pdp.set_bundle(PolicyBundle(policies=[x for x in s.pdp.bundle.policies if x.id != "analysts-read"]))
    s.policy_repo.save(s.pdp.bundle, ADMIN)
    tf(w, "plan", "-detailed-exitcode", ok=(2,))
    tf(w, "apply", "-auto-approve")
    assert any(x.id == "analysts-read" for x in s.pdp.bundle.policies)
    tf(w, "state", "rm", "cairn_policy.analysts")
    tf(w, "import", "cairn_policy.analysts", "analysts-read")
    tf(w, "plan", "-detailed-exitcode")
    tf(w, "destroy", "-auto-approve")
    assert [x.id for x in s.pdp.bundle.policies] == ["admin"] and core.sources == {}


# ---- every family: the registry's examples, by Terraform; and an exported workspace imported ------------------
@pytest.fixture(scope="module")
def iac(tmp_path_factory, world):
    """A Cairn with as many services as run here (tests/iac_world.py), on HTTP, for Terraform."""
    import sys

    import httpx
    import uvicorn

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import iac_world

    from cairn.app import create_api_app
    from cairn.iac.bundle import Client
    from cairn.iac.engine import Engine

    d = tmp_path_factory.mktemp("tf-iac")
    s = iac_world.build(d)
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, timeout_keep_alive=75,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    url = f"http://127.0.0.1:{p}"
    client = Client(url, "", http=httpx.Client(headers={"Authorization": f"Bearer {iac_world.TOKEN}"}, timeout=60))
    iac_world.prerequisites(Engine(client.call))
    work = d / "work"
    work.mkdir()
    (work / "tf.rc").write_text('provider_installation {\n  dev_overrides { "cairn/cairn" = "/prov" }\n  direct {}\n}\n')
    yield {"s": s, "work": work, "prov": world["prov"], "url": url, "client": client, "token": iac_world.TOKEN,
           "admin": iac_world.ADMIN}
    server.should_exit = True


def tfx(w, *args, ok=(0,), env=None):
    """Terraform against the IaC world. Secrets (TF_VAR_...) and the token reach it through the environment only,
    never on a command line."""
    names = []
    for k in sorted(env or {}):
        names += ["-e", k]
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{w['work']}:/work",
                        "-v", f"{w['prov']}:/prov:ro", "-w", "/work", "-e", "TF_CLI_CONFIG_FILE=/work/tf.rc",
                        "-e", "TF_IN_AUTOMATION=1", "-e", f"CAIRN_URL={w['url']}", "-e", "CAIRN_TOKEN", *names,
                        TERRAFORM, *args, "-no-color"],
                       capture_output=True, text=True, timeout=900,
                       env={**os.environ, "CAIRN_TOKEN": w["token"], **(env or {})})
    changes = [x for x in r.stdout.splitlines() if "~" in x or "->" in x or "will be" in x or "must be" in x]
    assert r.returncode in ok, (r.returncode, changes[:80], r.stdout[-3000:], r.stderr[-6000:])
    return r


def runnable(s):
    from cairn.iac import registry

    return [k for k in registry.kinds() if k.example and getattr(s, k.service, None) is not None]


def mine(e, k, spec):
    """The example's resource in Cairn (a setting: the one there is)."""
    from cairn.iac import bundle

    rows = e.records(k, {p: spec[p] for p in k.parents} if k.parents else None)
    return [r for r in rows if k.singleton or bundle._rec_key(k, r) == bundle.key_of(k, spec)]


SECRETS = {"TF_VAR_secret_finance_db_password_value": "pw-1",
           "TF_VAR_notification_route_finance_incidents_target": "#finance-data"}


def test_terraform_manages_every_family_from_the_registry(iac):
    """Create, no drift, change in place, import and destroy, for every kind that runs here (all six families)."""
    from cairn.iac import export, registry
    from cairn.iac.engine import Engine, tf_id

    w, s = iac, iac["s"]
    for f in w["work"].glob("*.tf"):
        f.unlink()
    kinds = [k for k in runnable(s) if k.name != "secret_scopes"]       # the scope the others name is there already
    assert {k.family for k in kinds} == set(registry.FAMILIES) and len(kinds) >= 35, [k.name for k in kinds]

    def config(update: bool):
        items = [(k, {**k.example, **((k.example_update or {}) if update else {})}) for k in kinds]
        (w["work"] / "main.tf").write_text(export.to_terraform(items, declared_values=True))

    config(False)
    tfx(w, "apply", "-auto-approve", env=SECRETS)
    e = Engine(w["client"].call)
    for k in kinds:
        assert mine(e, k, k.example), k.name
    assert s.secret_scopes.read(w["admin"], "finance-db", "password") == "pw-1"
    tfx(w, "plan", "-detailed-exitcode", env=SECRETS)                  # no drift
    assert "pw-1" not in tfx(w, "show", "-json").stdout                # write-only: never in the state
    # Changed in place: one change per kind with an example change, nothing replaced.
    config(True)
    later = {**SECRETS, "TF_VAR_notification_route_finance_incidents_target": "#finance-alerts"}   # its change
    out = tfx(w, "apply", "-auto-approve", env=later).stdout
    n = sum(1 for k in kinds if k.example_update)
    assert f"0 added, {n} changed, 0 destroyed" in out, out[-3000:]
    tfx(w, "plan", "-detailed-exitcode", env=later)
    for k in kinds:
        if k.example_update and not k.singleton:
            got = registry.kind(k.name)
            rec = mine(e, got, k.example)[0]
            from cairn.iac.engine import declared

            for f, v in k.example_update.items():
                assert declared(k, rec).get(f) == v or float(declared(k, rec).get(f)) == v, (k.name, f)
    # A secret rotated by raising its version.
    main = w["work"] / "main.tf"
    main.write_text(main.read_text().replace("value = var.secret_finance_db_password_value",
                                             "value = var.secret_finance_db_password_value\n  value_version = 2"))
    later["TF_VAR_secret_finance_db_password_value"] = "pw-2"
    tfx(w, "apply", "-auto-approve", env=later)
    assert s.secret_scopes.read(w["admin"], "finance-db", "password") == "pw-2"
    # One of each family forgotten and imported again by its id: no change.
    def reimport(name):
        k = registry.kind(name)
        rec = mine(e, k, k.example)[0]
        addr = f"cairn_{k.tf}.{export.label(k, export.as_record(k, k.example), set())}"
        tfx(w, "state", "rm", addr)
        tfx(w, "import", addr, tf_id(k, rec), env=later)

    for name in ("personas", "secret_scope_acls", "medallion_layers", "budgets", "procedures", "jobs",
                 "custom_agents", "agent_abuse_settings", "data_apps"):
        reimport(name)
    tfx(w, "plan", "-detailed-exitcode", env=later)
    # An imported secret's value is unknown to Terraform: with a version declared, it is sent once more.
    reimport("secrets")
    out = tfx(w, "plan", "-detailed-exitcode", ok=(2,), env=later).stdout
    assert "Plan: 0 to add, 1 to change, 0 to destroy." in out and "+ value_version = 2" in out, out[-2000:]
    tfx(w, "apply", "-auto-approve", env=later)
    tfx(w, "plan", "-detailed-exitcode", env=later)
    # Destroyed: everything Terraform made goes (settings only leave the state).
    tfx(w, "destroy", "-auto-approve", env=later)
    for k in kinds:
        if not k.singleton:
            assert not mine(e, k, k.example), k.name


def test_an_exported_workspace_imports_without_changes(iac, tmp_path):
    """cairn export --format terraform, then terraform plan: only imports; applied: no changes. The same workspace
    exported as a bundle plans with no changes too."""
    import re

    from cairn.iac import bundle, export
    from cairn.iac.engine import Engine

    w, s = iac, iac["s"]
    for f in [*w["work"].glob("*.tf"), *w["work"].glob("terraform.tfstate*")]:
        f.unlink()
    e = Engine(w["client"].call)
    kinds = runnable(s)
    for k in kinds:
        if not mine(e, k, k.example):
            e.create(k, dict(k.example))
    text = export.export(w["client"].call, "terraform")
    (w["work"] / "main.tf").write_text(text)
    env = {f"TF_VAR_{v}": SECRETS.get(f"TF_VAR_{v}", "x") for v in re.findall(r'variable "([a-z0-9_]+)"', text)}
    out = tfx(w, "plan", env=env).stdout
    n = text.count("\nimport {")
    assert n >= len(kinds) and f"Plan: {n} to import, 0 to add, 0 to change, 0 to destroy." in out, \
        [x for x in out.splitlines() if "~" in x or "->" in x or "updated" in x]
    tfx(w, "apply", "-auto-approve", env=env)
    tfx(w, "plan", "-detailed-exitcode", env=env)
    y = export.export(w["client"].call, "yaml", bundle="exported")
    (tmp_path / "cairn.yaml").write_text(y)
    names = {v: SECRETS.get("TF_VAR_" + v.lower(), "x") for v in re.findall(r"\$\{([A-Z0-9_]+)\}", y)}
    changes, _ = bundle.plan(w["client"], bundle.load(str(tmp_path / "cairn.yaml"), names))
    assert [c.line() for c in changes if c.action != "keep"] == []
    assert {c.kind for c in changes} >= {k.name for k in kinds}

