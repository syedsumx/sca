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
    real = s.identity._me_lookup
    s.identity._me_lookup = lambda url, h, c: ADMIN if (h.get("Authorization") or "") == f"Bearer {TOKEN}" else real(url, h, c)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
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
