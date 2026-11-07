"""Data residency end to end: namespaces pinned to a region stay in that region's storage, are read and written
only by a deployment in a region the pin allows, and leave only for such regions.

Cairn Catalog reads the same rules file as Cairn. Real Cairn Store (with STS) and Cairn Catalog asking the Cairn
API about every table; pyiceberg as the engine, as an administrator (the catalog's static token) and as a person
(a Cairn-issued lakehouse token). Needs the store and catalog binaries."""

import json
import os
import tempfile

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


@pytest.fixture(scope="module", autouse=True)
def residency_env():
    """Cairn (CAIRN_RESIDENCY_FILE, CAIRN_REGION) and Cairn Catalog (CAIRN_CATALOG_RESIDENCY_FILE) read the same
    file; this deployment runs in eu. Set before the module's Cairn and catalog start, removed after."""
    keys = {"CAIRN_RESIDENCY_FILE": _rules_file, "CAIRN_CATALOG_RESIDENCY_FILE": _rules_file, "CAIRN_REGION": "eu"}
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
    from test_trino_e2e import cairn  # noqa: E402, F401  (fixtures)


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
    ctx = {"identity": {"user": "ana", "groups": []}}

    def trino(op, schema):
        return s.trino_access.allow({"context": ctx, "action": {"operation": op, "resource": {"table": {
            "catalogName": s.lakehouse.trino_catalog, "schemaName": schema, "tableName": "people", "columns": ["id"]}}}})
    assert trino("SelectFromColumns", "crm_eu") and trino("InsertIntoTable", "crm_eu")
    assert not trino("SelectFromColumns", "vault_us") and not trino("InsertIntoTable", "vault_us")
    assert trino("ShowColumns", "vault_us")                                      # its metadata is not its data


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
