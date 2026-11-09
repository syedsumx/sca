"""Data residency end to end: namespaces pinned to a region stay in that region's storage, are read and written
only by a deployment in a region the pin allows, and leave only for such regions.

Cairn Catalog reads the same rules file as Cairn. Real Cairn Store (with STS) and Cairn Catalog asking the Cairn
API about every table; pyiceberg as the engine, as an administrator (the catalog's static token) and as a person
(a Cairn-issued lakehouse token). Needs the store and catalog binaries."""

import json
import os
import subprocess
import tempfile
import time

import httpx
import pytest

pa = pytest.importorskip("pyarrow")

from cairn.governance.residency import Residency, ResidencyError  # noqa: E402
from test_trino_e2e import STORE_KEY, _ready  # noqa: E402

RULES = {
    "regions": {"eu": {"location": "s3://lake-eu/warehouse"}, "us": {"location": "s3://lake-us/warehouse"}},
    "pins": [{"namespaces": ["crm_eu"], "region": "eu"},
             {"namespaces": ["stats_*"], "region": "us", "allow": ["us", "eu"]},
             {"namespaces": ["vault_us"], "region": "us"}],
}


def test_the_rules_hold_together_or_cairn_does_not_start():
    r = Residency(RULES, "eu")
    assert r.pin("crm_eu") == {"region": "eu", "allow": ["eu"], "location": "s3://lake-eu/warehouse/crm_eu"}
    assert r.pin("stats_daily")["allow"] == ["eu", "us"] and r.pin("raw") is None
    assert r.processing_allowed("crm_eu") and r.processing_allowed("stats_daily") and r.processing_allowed("raw")
    assert not r.processing_allowed("vault_us")
    assert not Residency(RULES, None).processing_allowed("crm_eu")       # no region: pinned data is processed nowhere
    assert Residency(RULES, None).processing_allowed("raw")
    assert r.transfer_allowed("stats_daily", "eu") and not r.transfer_allowed("crm_eu", "us")
    assert not r.transfer_allowed("crm_eu", None) and r.transfer_allowed("raw", None)
    with pytest.raises(ResidencyError, match="may not leave"):
        r.check_transfer(["crm_eu", "raw"], "us", "share")
    clash = {**RULES, "pins": [*RULES["pins"], {"namespaces": ["crm_*"], "region": "us"}]}
    assert not Residency(clash, "eu").processing_allowed("crm_eu")         # two regions: refused, never guessed
    for bad in ({"region": {}}, {"pins": [{"namespaces": ["x"], "region": "mars"}]},
                {**RULES, "pins": [{"namespaces": ["x"], "region": "eu", "allow": ["us"]}]},
                {"regions": {"eu": {"location": "/data"}}}, {"regions": {"eu": {"location": "s3://a", "x": 1}}}, []):
        with pytest.raises(ResidencyError):
            Residency(bad, "eu")
    with pytest.raises(ResidencyError, match="CAIRN_REGION"):
        Residency(RULES, "EU West")


# ---- with the real store, catalog and Cairn ------------------------------------------------------------------
pytestmark_e2e = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")

_rules_dir = tempfile.mkdtemp()
_rules_file = os.path.join(_rules_dir, "residency.json")
with open(_rules_file, "w") as f:
    json.dump(RULES, f)


STANDBY_KEY = ("standby-admin", "standby-secret")


def _start_store(d, env):
    """A Cairn Store process on a free port (the residency tests' own: the standby, and a source they restart)."""
    from test_trino_e2e import port, wait

    p = port()
    proc = subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **{k: v for k, v in os.environ.items() if not k.startswith("CAIRN_STORE_")},
        "CAIRN_STORE_ACCESS_KEY": STANDBY_KEY[0], "CAIRN_STORE_SECRET_KEY": STANDBY_KEY[1], "CAIRN_STORE_DATA": d,
        "CAIRN_STORE_ADDR": f"127.0.0.1:{p}", "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", **env})
    wait(p)
    return proc, f"http://127.0.0.1:{p}"


def _s3(endpoint, key):
    import boto3

    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=key[0], aws_secret_access_key=key[1],
                        region_name="us-east-1")


@pytest.fixture(scope="module")
def standby():
    """A second Cairn Store standing in for storage in other regions: replication targets, backup sinks, a remote
    bucket and a storage tier, each labelled with the region it keeps data in (``residency_region``)."""
    if not _ready():
        yield None                  # the unit tests here run without it
        return
    proc, endpoint = _start_store(os.path.join(_rules_dir, "standby"),
                                  {"CAIRN_STORE_BUCKETS": "dr-eu,dr-stats,dr-us,vault-us,vault-eu,mirror-us,tier-us"})
    secret = os.path.join(_rules_dir, "standby.secret")
    with open(secret, "w") as f:
        f.write(STANDBY_KEY[1])
    conn = {"endpoint": endpoint, "access_key": STANDBY_KEY[0], "secret_key_file": secret}
    files = {
        "CAIRN_STORE_REPLICATION_TARGETS_FILE": [{"id": "standby-eu", **conn, "residency_region": "eu"},
                                                 {"id": "standby-us", **conn, "residency_region": "us"},
                                                 {"id": "unlabelled", **conn}],
        "CAIRN_STORE_BACKUP_SINKS_FILE": [{"id": "vault-us", "bucket": "vault-us", **conn, "residency_region": "us"},
                                          {"id": "vault-eu", "bucket": "vault-eu", **conn, "residency_region": "eu"}],
        "CAIRN_STORE_REMOTES_FILE": [{"id": "mirror-us", "bucket": "mirror-us", **conn, "residency_region": "us"}],
        "CAIRN_STORE_TIERS_FILE": [{"storage_class": "STANDARD_IA", "bucket": "tier-us", **conn, "residency_region": "us"}],
    }
    env = {}
    for k, doc in files.items():
        env[k] = os.path.join(_rules_dir, k.lower() + ".json")
        with open(env[k], "w") as f:
            json.dump(doc, f)
    yield {"endpoint": endpoint, "s3": _s3(endpoint, STANDBY_KEY), "env": env}
    proc.terminate()
    proc.wait(15)


@pytest.fixture(scope="module", autouse=True)
def residency_env(standby):
    """Cairn (CAIRN_RESIDENCY_FILE, CAIRN_REGION), Cairn Catalog (CAIRN_CATALOG_RESIDENCY_FILE) and Cairn Store
    (CAIRN_STORE_RESIDENCY_FILE) read the same file; this deployment runs in eu. The lake's store replicates, backs
    up (continuously, to the us vault), writes back and tiers to the standby. Set before the module's Cairn, catalog
    and store start, removed after."""
    keys = {"CAIRN_RESIDENCY_FILE": _rules_file, "CAIRN_CATALOG_RESIDENCY_FILE": _rules_file, "CAIRN_REGION": "eu",
            "CAIRN_STORE_RESIDENCY_FILE": _rules_file, "CAIRN_STORE_BACKUP_CONTINUOUS": "vault-us",
            **(standby["env"] if standby else {})}
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


if _ready():
    from test_spark_e2e import ANA, lake, people  # noqa: E402, F401  (fixtures)
    from test_trino_e2e import cairn, trino  # noqa: E402, F401  (fixtures)


@pytest.fixture(scope="module")
def world(residency_env, cairn, lake):  # noqa: F811
    import boto3
    from cairn.governance.abac import Policy, PolicyBundle, Target

    url, s = cairn
    s3 = boto3.client("s3", endpoint_url=lake["store"], aws_access_key_id=STORE_KEY[0],
                      aws_secret_access_key=STORE_KEY[1], region_name="us-east-1")
    for b in ("lake-eu", "lake-us"):
        s3.create_bucket(Bucket=b)
    ana = {"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ana"}
    bundle = s.pdp.bundle
    bundle = bundle() if callable(bundle) else bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(
        id="ana-pinned", effect="allow", condition=ana,
        actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET"],
        target=Target(resource_urns=["*crm_eu.*", "*stats_daily.*", "*vault_us.*", "*raw.*"]))]))
    admin = lake["admin"]
    rows = pa.table({"id": pa.array([1, 2], pa.int64()), "name": ["Ada", "Bo"]})
    for ns in ("crm_eu", "stats_daily", "vault_us", "raw"):
        admin.create_namespace(ns)
        admin.create_table(f"{ns}.people", schema=rows.schema).append(rows)
    return {"url": url, "s": s, "admin": admin, "lake": lake, "rows": rows}


def _person(world):
    from pyiceberg.catalog.rest import RestCatalog

    token = httpx.post(f"{world['url']}/v1/lakehouse/token", headers={"Authorization": f"Bearer {ANA}"},
                       timeout=10).json()["token"]
    return RestCatalog("ana", uri=world["lake"]["catalog"], token=token)


@pytestmark_e2e
def test_pinned_tables_live_in_their_region_and_nowhere_else(world):
    from pyiceberg.exceptions import ForbiddenError

    admin, rows = world["admin"], world["rows"]
    t = admin.load_table("crm_eu.people")
    assert t.location().startswith("s3://lake-eu/warehouse/crm_eu/people-"), t.location()
    assert admin.load_table("stats_daily.people").location().startswith("s3://lake-us/warehouse/stats_daily/")
    assert admin.load_table("raw.people").location().startswith("s3://lakehouse/warehouse/raw/")
    assert admin.load_namespace_properties("crm_eu")["location"] == "s3://lake-eu/warehouse/crm_eu"
    assert t.scan().to_arrow().num_rows == 2                     # the data is there, in lake-eu
    # Even an administrator puts nothing of a pinned namespace elsewhere, nor anything else in a region's place.
    with pytest.raises(ForbiddenError, match="pinned to region eu"):
        admin.create_table("crm_eu.elsewhere", schema=rows.schema, location="s3://lakehouse/warehouse/crm_eu/x")
    with pytest.raises(ForbiddenError, match="only pinned namespaces"):
        admin.create_table("raw.sneaky", schema=rows.schema, location="s3://lake-eu/warehouse/crm_eu/people-stolen")
    with pytest.raises(ForbiddenError):
        admin.update_namespace_properties("crm_eu", updates={"location": "s3://lakehouse/warehouse/crm_eu"})
    with pytest.raises(ForbiddenError, match="pinned to region eu"):
        with t.transaction() as tx:
            tx.set_properties({"write.data.path": "s3://lakehouse/warehouse/exfil"})
    for target in ("raw.people_copy", "vault_us.people_copy"):
        with pytest.raises(ForbiddenError, match="not pinned alike"):
            admin.rename_table("crm_eu.people", target)
    with pytest.raises(ForbiddenError, match="not pinned alike"):
        admin.rename_table("raw.people", "crm_eu.from_raw")
    assert t.scan().to_arrow().num_rows == 2 and admin.load_table("raw.people").scan().to_arrow().num_rows == 2


@pytestmark_e2e
def test_a_deployment_reads_pinned_data_only_where_the_pin_allows(world):
    from pyiceberg.exceptions import ForbiddenError, NoSuchTableError

    s = world["s"]
    cat = _person(world)
    assert cat.load_table("crm_eu.people").scan().to_arrow().num_rows == 2       # eu data in an eu deployment
    assert cat.load_table("stats_daily.people").scan().to_arrow().num_rows == 2  # us data that may be used in eu
    with pytest.raises((ForbiddenError, NoSuchTableError)):
        cat.load_table("vault_us.people").scan().to_arrow()                     # us only: seen, never read
    d = s.lakehouse.decide("urn:li:corpuser:ana", [{"namespace": ["vault_us"], "name": "people"},
                                                   {"namespace": ["crm_eu"], "name": "people"}])
    assert [(t["view"], t["read"], t["write"]) for t in d["tables"]] == [(True, False, False), (True, True, True)]
    # Trino asks the same question (the OPA protocol): no rows of vault_us, writes neither.
    ctx = {"identity": {"user": "ana", "groups": []}, "queryId": "20261109_000000_00001_resid"}

    def trino(op, schema, context=ctx):
        return s.trino_access.allow({"context": context, "action": {"operation": op, "resource": {"table": {
            "catalogName": s.lakehouse.trino_catalog, "schemaName": schema, "tableName": "people", "columns": ["id"]}}}})
    assert trino("SelectFromColumns", "crm_eu") and trino("InsertIntoTable", "crm_eu")
    assert not trino("SelectFromColumns", "vault_us") and not trino("InsertIntoTable", "vault_us")
    assert trino("ShowColumns", "vault_us")                                      # its metadata is not its data
    # A pinned read Trino cannot tie to its statement (no query id) cannot be followed: refused.
    assert not trino("SelectFromColumns", "crm_eu", {"identity": ctx["identity"]})


@pytestmark_e2e
def test_pinned_tables_leave_only_for_regions_their_pin_allows(world):
    url, s = world["url"], world["s"]

    def share(name, tables, region=None):
        body = {"name": name, "recipient": "Partner", "tables": tables, "expires_days": 7}
        if region is not None:
            body["region"] = region
        return httpx.post(f"{url}/v1/shares", json=body, headers={"Authorization": f"Bearer {ANA}"}, timeout=30)
    for name, region in (("p-none", None), ("p-us", "us")):
        r = share(name, ["crm_eu.people"], region)
        assert r.status_code == 409 and "data residency" in r.text, r.text
    assert share("p-bad", ["crm_eu.people"], "EU!").status_code == 422
    assert share("p-raw", ["raw.people"]).status_code == 200                     # unpinned: no region needed
    r = share("p-eu", ["crm_eu.people", "stats_daily.people"], "eu")
    assert r.status_code == 200 and r.json()["region"] == "eu", r.text
    sid = r.json()["id"]
    # Checked again on every request: rules that change later stop what they no longer allow.
    decide = lambda: [t["read"] for t in s.shares.decide(f"share:{sid}", [  # noqa: E731
        {"namespace": ["crm_eu"], "name": "people"}, {"namespace": ["stats_daily"], "name": "people"}])["tables"]]
    assert decide() == [True, True]
    kept = s.shares.residency
    s.shares.residency = Residency({**RULES, "pins": [{"namespaces": ["crm_eu"], "region": "eu", "allow": ["eu"]},
                                                      {"namespaces": ["stats_*"], "region": "us"}]}, "eu")
    try:
        assert decide() == [True, False]
    finally:
        s.shares.residency = kept
    # Reverse syncs to a SaaS app: the app's region is declared, and checked at save and at every run.
    from cairn.pipelines.reverse_etl import ReverseSyncs
    from cairn.pipelines.saas import SaasError

    rs = ReverseSyncs(s.engine, None, lambda o, u: type("P", (), {"all_fields": False, "masks_field": lambda self, c: False})(),
                      lambda o, u: True, None, lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)",
                      catalog=s.lakehouse.trino_catalog)
    rs.may_leave = s.reverse_syncs.may_leave if getattr(s, "reverse_syncs", None) is not None else (
        lambda schema, region: s.residency.transfer_allowed(schema, region))
    body = {"name": "to-crm", "source": "crm_eu.people", "kind": "hubspot", "object": "contacts",
            "key": {"column": "id", "field": "id"}, "mapping": {"name": "firstname"}, "secret_scope": "hub"}
    for region in (None, "us"):
        with pytest.raises(SaasError, match="data residency"):
            rs.save("urn:li:corpuser:ana", {**body, **({"region": region} if region else {})})
    assert rs.save("urn:li:corpuser:ana", {**body, "region": "eu"})["region"] == "eu"
    # The rules are visible (read only) to those who manage policies.
    got = httpx.get(f"{url}/governance/residency", headers={"X-Cairn-Actor": "urn:li:corpuser:admin"}, timeout=10)
    assert httpx.get(f"{url}/governance/residency", headers={"X-Cairn-Actor": "urn:li:corpuser:ana"},
                     timeout=10).status_code == 403
    assert got.status_code == 200 and got.json()["region"] == "eu" and len(got.json()["pins"]) == 3, got.text


@pytestmark_e2e
def test_copies_made_by_sql_follow_the_pin_on_trino(world, trino):  # noqa: F811
    """A statement that reads a pinned table writes only into a namespace pinned to a region the pin allows: refused
    before it runs otherwise, an administrator's too. Real Trino asking Cairn (the OPA protocol), the real catalog
    and store."""
    from sqlalchemy import select

    from cairn.governance.residency import residency_flows

    def refused(stmt, user="ana"):
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino(stmt, user=user)

    def count(table):
        return trino(f"SELECT count(*) AS n FROM lake.{table}")[0]["n"]
    raw_before = count("raw.people")
    assert trino("SELECT count(*) AS n FROM lake.crm_eu.people", user="ana") == [{"n": 2}]   # reading stays open
    # Out of the pin: into an unpinned namespace (no region), into another region, into another catalog.
    refused("CREATE TABLE lake.raw.copied AS SELECT * FROM lake.crm_eu.people")
    refused("CREATE TABLE lake.raw.copied AS SELECT * FROM lake.crm_eu.people", user="admin")
    refused("INSERT INTO lake.raw.people SELECT * FROM lake.crm_eu.people")
    refused("INSERT INTO lake.raw.people SELECT p.id, s.name FROM lake.raw.people p JOIN lake.crm_eu.people s ON p.id = s.id")
    refused("INSERT INTO lake.raw.people SELECT id, name FROM lake.stats_daily.people")      # may go to us and eu: not nowhere
    refused("INSERT INTO lake.stats_daily.people SELECT * FROM lake.crm_eu.people", user="admin")   # eu only, not us
    refused("UPDATE lake.raw.people SET name = (SELECT max(name) FROM lake.crm_eu.people)", user="admin")
    refused("CREATE TABLE crm.public.exported AS SELECT * FROM lake.crm_eu.people", user="admin")   # PostgreSQL: no region
    assert count("raw.people") == raw_before
    assert not trino("SHOW TABLES FROM lake.raw LIKE 'copied'")
    # Within what the pins allow: the same region, a region the source allows, unpinned into pinned.
    trino("CREATE TABLE lake.crm_eu.people_copy AS SELECT * FROM lake.crm_eu.people", user="ana")
    trino("INSERT INTO lake.crm_eu.people_copy SELECT * FROM lake.stats_daily.people", user="ana")   # us data allowed in eu
    trino("INSERT INTO lake.crm_eu.people_copy SELECT * FROM lake.raw.people", user="ana")
    trino("CREATE TABLE lake.raw.people_again AS SELECT * FROM lake.raw.people", user="ana")
    assert count("crm_eu.people_copy") == 4 + raw_before
    loc = world["admin"].load_table("crm_eu.people_copy").location()
    assert loc.startswith("s3://lake-eu/warehouse/crm_eu/"), loc
    # Each statement's reads and writes were followed by its query id (shared by Cairn's replicas).
    with world["s"].engine.connect() as c:
        rows = list(c.execute(select(residency_flows)))
    assert rows and all(r.query_id for r in rows)
    assert {r.namespace for r in rows if r.side == "source"} >= {"lake.crm_eu", "lake.stats_daily"}


def _admin(endpoint, key, method, path, body=None):
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*key), "s3", "us-east-1").add_auth(req)
    r = httpx.request(method, req.url, content=data, headers=dict(req.headers), timeout=60)
    return r.status_code, r.text


def _keys(s3, bucket, prefix=""):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def _until(cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001 - not there yet
            pass
        time.sleep(0.3)
    return False


@pytestmark_e2e
def test_the_store_sends_pinned_data_only_to_regions_their_pin_allows(world, standby):
    """Replication, backups (on request and continuous), write-back mounts and storage tiers, to a second store
    standing in for the other regions: refused when configured and again for every object sent."""
    from botocore.exceptions import ClientError

    lake, admin, rows = world["lake"], world["admin"], world["rows"]
    s3, dr = _s3(lake["store"], STORE_KEY), standby["s3"]

    def denied(fn):
        with pytest.raises(ClientError) as ei:
            fn()
        assert ei.value.response["Error"]["Code"] == "AccessDenied", ei.value.response

    def replicate(bucket, target, dest, prefix=""):
        return lambda: s3.put_bucket_replication(Bucket=bucket, ReplicationConfiguration={
            "Role": f"arn:cairn:store:::{target}", "Rules": [{"ID": "r", "Priority": 1, "Status": "Enabled",
                                                               "Filter": {"Prefix": prefix},
                                                               "Destination": {"Bucket": f"arn:aws:s3:::{dest}"}}]})
    for b in ("lake-eu", "lake-us", "lakehouse"):
        s3.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})
    for b in ("dr-eu", "dr-stats", "dr-us"):
        dr.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})
    # Replication, configured: only to a region every pin it takes in allows.
    denied(replicate("lake-eu", "standby-us", "dr-us"))                 # crm_eu stays in eu
    denied(replicate("lake-eu", "unlabelled", "dr-eu"))                 # a destination with no region gets nothing
    denied(replicate("lake-us", "standby-eu", "dr-stats"))              # vault_us is us only
    replicate("lake-us", "standby-eu", "dr-stats", "warehouse/stats_daily/")()  # stats_* may go to eu
    replicate("lake-eu", "standby-eu", "dr-eu")()                       # within its region
    replicate("lakehouse", "unlabelled", "dr-us", "warehouse/raw/")()   # unpinned data: anywhere
    admin.load_table("crm_eu.people").append(rows)
    admin.load_table("stats_daily.people").append(rows)
    admin.load_table("raw.people").append(rows)
    assert _until(lambda: any("/data/" in k for k in _keys(dr, "dr-eu", "warehouse/crm_eu/")))
    assert _until(lambda: any("/data/" in k for k in _keys(dr, "dr-stats", "warehouse/stats_daily/")))
    assert _until(lambda: any("/data/" in k for k in _keys(dr, "dr-us", "warehouse/raw/")))
    # Backups of the whole store, on request: it holds eu-only and us-only data, so no single sink may have it.
    for sink in ("vault-us", "vault-eu"):
        code, body = _admin(lake["store"], STORE_KEY, "POST", "backups", {"name": f"full-{sink}", "sink": sink})
        assert code == 403 and "residency" in body.lower(), (code, body)
        assert not _keys(dr, sink, "backups/")
    # Continuous backup to the us vault (allowed at start: no pinned bucket existed yet): each change is checked,
    # so the eu table's files never reach it while us and unpinned tables' do.
    assert _until(lambda: [k for k in _keys(dr, "vault-us", "journal/")])
    time.sleep(3)
    journal = [json.loads(dr.get_object(Bucket="vault-us", Key=k)["Body"].read()) for k in _keys(dr, "vault-us", "journal/")]
    assert any(r["bucket"] == "lakehouse" for r in journal)
    assert not [r["key"] for r in journal if r["bucket"] == "lake-eu"]
    assert [r["key"] for r in journal if r["key"].startswith("warehouse/vault_us/")]   # us-only data, in the us vault
    # Write-back to a remote in us, and transitions to a storage tier in us: refused for eu data.
    code, body = _admin(lake["store"], STORE_KEY, "PUT", "buckets/lake-eu/remote", {"remote": "mirror-us", "write_back": True})
    assert code == 403 and "residency" in body.lower(), (code, body)
    denied(lambda: s3.put_bucket_lifecycle_configuration(Bucket="lake-eu", LifecycleConfiguration={"Rules": [
        {"ID": "cold", "Status": "Enabled", "Filter": {"Prefix": ""},
         "Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}]}]}))


@pytestmark_e2e
def test_replication_configured_before_a_pin_stops_when_the_pin_comes(standby):
    """Checked again for every object: a store whose rules now pin the namespace stops replicating it, even though
    its replication was configured (and allowed) before."""
    d = tempfile.mkdtemp(dir=_rules_dir)
    env = {"CAIRN_STORE_REPLICATION_TARGETS_FILE": standby["env"]["CAIRN_STORE_REPLICATION_TARGETS_FILE"],
           "CAIRN_STORE_BUCKETS": "lake-eu"}
    dr = standby["s3"]
    dr.put_bucket_versioning(Bucket="dr-us", VersioningConfiguration={"Status": "Enabled"})
    proc, endpoint = _start_store(d, env)
    src = _s3(endpoint, STANDBY_KEY)
    src.put_bucket_versioning(Bucket="lake-eu", VersioningConfiguration={"Status": "Enabled"})
    src.put_bucket_replication(Bucket="lake-eu", ReplicationConfiguration={
        "Role": "arn:cairn:store:::standby-us", "Rules": [{"ID": "r", "Priority": 1, "Status": "Enabled",
                                                          "Filter": {"Prefix": ""},
                                                          "Destination": {"Bucket": "arn:aws:s3:::dr-us"}}]})
    src.put_object(Bucket="lake-eu", Key="warehouse/crm_eu/before/data/a.parquet", Body=b"before the pin")
    assert _until(lambda: _keys(dr, "dr-us", "warehouse/crm_eu/before/"))
    proc.terminate()
    proc.wait(15)
    proc, endpoint = _start_store(d, {**env, "CAIRN_STORE_RESIDENCY_FILE": _rules_file})   # the namespace is pinned now
    try:
        src = _s3(endpoint, STANDBY_KEY)
        src.put_object(Bucket="lake-eu", Key="warehouse/crm_eu/after/data/b.parquet", Body=b"after the pin")
        src.put_object(Bucket="lake-eu", Key="elsewhere/c.txt", Body=b"not in a region's location")
        assert _until(lambda: src.head_object(Bucket="lake-eu", Key="warehouse/crm_eu/after/data/b.parquet")
                      .get("ReplicationStatus") == "FAILED")
        assert _until(lambda: _keys(dr, "dr-us", "elsewhere/"))
        assert not _keys(dr, "dr-us", "warehouse/crm_eu/after/")
    finally:
        proc.terminate()
        proc.wait(15)
