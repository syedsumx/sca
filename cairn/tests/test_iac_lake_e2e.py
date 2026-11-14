"""Everything as code (L137) for the configuration that lives in Cairn Store and Cairn Catalog: a bucket's quota, a
feature table served online, and how long a system table keeps. Real binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN),
a lakehouse table written with pyiceberg, and each resource checked where it takes effect:

* a bucket quota: the store's own admin API reports it (and forgets it when it is removed);
* an online feature table: filled from the Iceberg table, an entity looked up by key; removed, it is not served;
* a system table's retention: the Iceberg table's property in the catalog.

Each through a bundle (planned, applied, planned again with no changes, exported and planned with no changes,
changed in place, pruned) and by real Terraform with the provider built from source (created, no drift, changed in
place, imported, destroyed). Needs docker, Go, the hashicorp/terraform image, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading

import httpx
import pytest
import yaml

from test_trino_e2e import CATALOG_TOKEN, KEY, STORE_KEY, _ready, lake, port, wait  # noqa: F401

TERRAFORM = os.environ.get("CAIRN_TERRAFORM_IMAGE", "hashicorp/terraform:1.13.4")
PROVIDER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "terraform-provider-cairn")
TOKEN = "iac-lake-" + "t" * 24
ADMIN = "urn:li:corpuser:admin"


def _all_ready() -> bool:
    if not _ready() or not shutil.which("go"):
        return False
    return subprocess.run(["docker", "image", "inspect", TERRAFORM], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _all_ready(), reason=f"needs docker, go, {TERRAFORM}, CAIRN_STORE_BIN and "
                                                         "CAIRN_CATALOG_BIN")


@pytest.fixture(scope="module")
def cairn(lake, tmp_path_factory):  # noqa: F811
    """The Cairn API on HTTP: its store administration, online features and system tables on the store and catalog
    above, and a lakehouse table of customers (three rows) declared in the catalog."""
    import pyarrow as pa
    import uvicorn
    from pyiceberg.catalog.rest import RestCatalog

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    props = {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
             "s3.path-style-access": "true"}
    cat = RestCatalog("iac-lake", **props)
    cat.create_namespace(("ml",))
    t = cat.create_table(("ml", "customers"), pa.schema([("id", pa.int64()), ("score", pa.float64()),
                                                         ("segment", pa.string())]))
    t.append(pa.table({"id": [1, 2, 3], "score": [0.5, 0.9, 0.1], "segment": ["a", "b", "a"]}))
    # The store's admin key reaches Cairn through its environment (set only while the services are built).
    env = {"CAIRN_STORE_URL": lake["store"], "CAIRN_STORE_ADMIN_ACCESS_KEY": STORE_KEY[0],
           "CAIRN_STORE_ADMIN_SECRET_KEY": STORE_KEY[1],
           "CAIRN_PIPES_CATALOG": json.dumps(props), "CAIRN_SYSTEM_TABLES_CATALOG": json.dumps(props),
           # System tables come with Trino access (Trino reads them); Trino itself is not needed to set retention.
           "CAIRN_TRINO_ACCESS_KEY": KEY,
           "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "iceberg", "database": ""}})}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        graph = InMemoryGraph()
        s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                                    gateway={"key_dir": str(tmp / "keys")}), graph=graph, with_ai=False,
                           with_gateway=False)
    finally:
        for k, v in saved.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    for name in ("store_admin", "feature_tables", "online_features", "system_tables"):
        assert getattr(s, name, None) is not None, f"{name} is off"
    # The table as the catalog knows it (its dataset, named as Cairn's lakehouse names it).
    graph.aspects[(s.feature_tables.dataset_urn(["ml"], "customers"), "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "score", "segment")]}
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    real = s.identity._me_lookup
    s.identity._me_lookup = lambda u, h, c: ADMIN if (h.get("Authorization") or "") == f"Bearer {TOKEN}" else real(u, h, c)
    s.is_admin = lambda urn: urn == ADMIN
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, timeout_keep_alive=75,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    prov = tmp / "prov"
    prov.mkdir()
    subprocess.run(["go", "build", "-o", str(prov / "terraform-provider-cairn"), "."], cwd=PROVIDER_SRC, check=True,
                   env={**os.environ, "CGO_ENABLED": "0"}, timeout=900)
    work = tmp / "work"
    work.mkdir()
    (work / "tf.rc").write_text('provider_installation {\n  dev_overrides { "cairn/cairn" = "/prov" }\n  direct {}\n}\n')
    yield {"s": s, "url": f"http://127.0.0.1:{p}", "work": work, "prov": prov, "catalog": cat}
    server.should_exit = True


def tf(w, *args, ok=(0,)):
    """Terraform in its image; the token reaches it through the environment only."""
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{w['work']}:/work",
                        "-v", f"{w['prov']}:/prov:ro", "-w", "/work", "-e", "TF_CLI_CONFIG_FILE=/work/tf.rc",
                        "-e", "TF_IN_AUTOMATION=1", "-e", f"CAIRN_URL={w['url']}", "-e", "CAIRN_TOKEN", TERRAFORM,
                        *args, "-no-color"], capture_output=True, text=True, timeout=900,
                       env={**os.environ, "CAIRN_TOKEN": TOKEN})
    assert r.returncode in ok, (r.returncode, r.stdout[-3000:], r.stderr[-3000:])
    return r


def quota(s, bucket: str):
    return next(b for b in s.store_admin.buckets() if b["name"] == bucket).get("quota")


def retention(w, schema: str, table: str) -> str:
    return w["catalog"].load_table((schema, table)).properties.get("system-table.retention-days")


def lookup(w, name: str):
    return httpx.post(f"{w['url']}/v1/ml/feature-tables/{name}/online/lookup", timeout=60,
                      headers={"Authorization": f"Bearer {TOKEN}"}, json={"entities": [{"id": 2}]})


BUNDLE = {
    "bucket_quotas": [{"bucket": "trino-exchange", "max_bytes": 10 ** 12}],
    "feature_tables": [{"name": "customer_features", "table": "ml.customers", "keys": ["id"]}],
    "online_feature_tables": [{"feature_table": "customer_features", "ttl_seconds": 3600}],
    "system_table_retention": [{"schema": "audit", "table": "events", "days": 30}],
}


def test_store_and_catalog_configuration_through_a_bundle(cairn, tmp_path):
    from cairn.iac import bundle as b
    from cairn.iac import export

    w, s = cairn, cairn["s"]
    c = b.Client(w["url"], TOKEN)
    path = tmp_path / "cairn.yaml"
    doc = {"bundle": "lake", **json.loads(json.dumps(BUNDLE))}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    changes, state = b.plan(c, b.load(str(path)))
    assert sorted((x.kind, x.action) for x in changes) == [               # a system table is there already
        ("bucket_quotas", "create"), ("feature_tables", "create"), ("online_feature_tables", "create"),
        ("system_table_retention", "update")]
    b.apply(c, b.load(str(path)), changes, state, log=lambda _: None)
    assert quota(s, "trino-exchange")["max_bytes"] == 10 ** 12                         # the store has it
    got = lookup(w, "customer_features").json()["results"][0]                          # filled from the table
    assert got["status"] == "found" and got["features"]["score"] == 0.9 and got["features"]["segment"] == "b"
    assert retention(w, "audit", "events") == "30"                                      # the catalog has it
    assert [x.line() for x in b.plan(c, b.load(str(path)))[0] if x.action != "keep"] == []

    # Exported (as code from the live workspace): planned with no changes.
    text = export.export(c.call, "yaml", list(BUNDLE), bundle="lake")
    (tmp_path / "exported.yaml").write_text(text)
    assert [x.line() for x in b.plan(c, b.load(str(tmp_path / "exported.yaml")))[0] if x.action != "keep"] == []

    # Changed in git: in place.
    doc["bucket_quotas"][0]["max_objects"] = 5000
    doc["online_feature_tables"][0]["ttl_seconds"] = 60
    doc["system_table_retention"][0]["days"] = 14
    doc["feature_tables"][0]["description"] = "Customer scores"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    changes, state = b.plan(c, b.load(str(path)))
    assert sorted((x.kind, x.action) for x in changes if x.action != "keep") == [
        ("bucket_quotas", "update"), ("feature_tables", "update"), ("online_feature_tables", "update"),
        ("system_table_retention", "update")]
    b.apply(c, b.load(str(path)), changes, state, log=lambda _: None)
    assert quota(s, "trino-exchange") == {"max_bytes": 10 ** 12, "max_objects": 5000}
    assert retention(w, "audit", "events") == "14"
    assert s.online_features._config("customer_features")["ttl_seconds"] == 60
    assert lookup(w, "customer_features").json()["results"][0]["status"] == "found"   # refilled whole
    assert [x.line() for x in b.plan(c, b.load(str(path)))[0] if x.action != "keep"] == []

    # Pruned: the quota and the online store go, the feature table leaves the catalog (its table stays); a system
    # table always keeps some retention, so that one is only forgotten.
    path.write_text(yaml.safe_dump({"bundle": "lake"}))
    changes, state = b.plan(c, b.load(str(path)), prune=True)
    assert {x.kind for x in changes if x.action == "delete"} == set(BUNDLE)
    b.apply(c, b.load(str(path)), changes, state, log=lambda _: None)
    assert quota(s, "trino-exchange") is None
    assert lookup(w, "customer_features").status_code == 404
    assert s.feature_tables.definition("customer_features") is None
    assert w["catalog"].load_table(("ml", "customers")).scan().to_arrow().num_rows == 3
    assert retention(w, "audit", "events") == "14"


CONFIG = """
terraform {
  required_providers {
    cairn = { source = "cairn/cairn" }
  }
}
provider "cairn" {}

resource "cairn_bucket_quota" "exchange" {
  bucket    = "trino-exchange"
  max_bytes = MAX_BYTES
}

resource "cairn_feature_table" "customers" {
  name  = "customer_features"
  table = "ml.customers"
  keys  = ["id"]
}

resource "cairn_online_feature_table" "customers" {
  feature_table = cairn_feature_table.customers.name
  ttl_seconds   = TTL
}

resource "cairn_system_table_retention" "audit" {
  schema = "audit"
  table  = "events"
  days   = DAYS
}
"""


def test_store_and_catalog_configuration_by_terraform(cairn):
    from cairn.iac import registry
    from cairn.iac.bundle import Client
    from cairn.iac.engine import Engine, tf_id

    w, s = cairn, cairn["s"]

    def config(max_bytes=10 ** 9, ttl=3600, days=45):
        (w["work"] / "main.tf").write_text(CONFIG.replace("MAX_BYTES", str(max_bytes)).replace("TTL", str(ttl))
                                           .replace("DAYS", str(days)))

    config()
    tf(w, "apply", "-auto-approve")
    assert quota(s, "trino-exchange")["max_bytes"] == 10 ** 9
    assert lookup(w, "customer_features").json()["results"][0]["status"] == "found"
    assert retention(w, "audit", "events") == "45"
    tf(w, "plan", "-detailed-exitcode")                                                 # no drift
    config(max_bytes=2 * 10 ** 9, ttl=120, days=60)
    out = tf(w, "apply", "-auto-approve").stdout
    assert "0 added, 3 changed, 0 destroyed" in out, out[-2000:]
    assert quota(s, "trino-exchange")["max_bytes"] == 2 * 10 ** 9 and retention(w, "audit", "events") == "60"
    assert s.online_features._config("customer_features")["ttl_seconds"] == 120
    # Forgotten and imported again by their ids: no change.
    e = Engine(Client(w["url"], TOKEN).call)
    for name, addr in (("bucket_quotas", "cairn_bucket_quota.exchange"), ("feature_tables", "cairn_feature_table.customers"),
                       ("online_feature_tables", "cairn_online_feature_table.customers"),
                       ("system_table_retention", "cairn_system_table_retention.audit")):
        k = registry.kind(name)
        rec = next(r for r in e.records(k) if r.get(k.id) in ("trino-exchange", "customer_features")
                   or (r.get("schema"), r.get("table")) == ("audit", "events"))
        tf(w, "state", "rm", addr)
        tf(w, "import", addr, tf_id(k, rec))
    tf(w, "plan", "-detailed-exitcode")
    tf(w, "destroy", "-auto-approve")
    assert quota(s, "trino-exchange") is None
    assert lookup(w, "customer_features").status_code == 404
    assert s.feature_tables.definition("customer_features") is None
    assert retention(w, "audit", "events") == "60"                                      # forgotten, not reset
