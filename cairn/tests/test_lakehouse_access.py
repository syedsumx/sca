"""Lakehouse decisions for engines that read files (cairn.dataaccess.lakehouse): restricted when a mask or row
filter applies, everything false on any error, and OpenLineage datasets renamed from storage paths to the
lakehouse tables their symlinks name."""

from types import SimpleNamespace

from cairn.dataaccess.lakehouse import LakehouseAccess
from cairn.dataaccess.trino import MANAGE, READ, SYSTEM, VIEW, WRITE, TrinoAccess, parse_catalogs


def access(grants, filters=(), masked=()):
    """A TrinoAccess whose decisions come from ``grants`` {(user, privilege, table)}."""
    def decide(actor, groups, privilege, urn):
        user = actor.removeprefix("urn:li:corpuser:")
        table = (urn or "").split(",")[1] if urn else None
        ok = (user, privilege, table) in grants or (user, privilege, "*") in grants
        obligations = [SimpleNamespace(type="row_filter", params={"expression": "x"})] if (user, table) in filters else []
        return SimpleNamespace(decision="ALLOW" if ok else "DENY", obligations=obligations)

    def plan_for(actor, urn):
        return SimpleNamespace(active=(actor.removeprefix("urn:li:corpuser:"), urn.split(",")[1]) in masked)

    return TrinoAccess(decide, plan_for, parse_catalogs({}))


def test_decisions_and_restrictions():
    grants = {("ana", p, "lakehouse.sales.orders") for p in (VIEW, READ)} | {
        ("ana", p, "lakehouse.analytics.t") for p in (VIEW, READ, WRITE, MANAGE)} | {("bob", READ, "lakehouse.sales.orders"),
                                                                                    ("bob", VIEW, "lakehouse.sales.orders")}
    lake = LakehouseAccess(access(grants, filters={("bob", "lakehouse.sales.orders")}, masked={("ana", "lakehouse.sales.orders")}),
                           lambda urn: [])
    out = lake.decide("ana", [{"namespace": ["sales"], "name": "orders"}, {"namespace": ["analytics"], "name": "t"},
                              {"namespace": ["hr"], "name": "salaries"}])
    assert out["schemas"] is False
    orders, t, hr = out["tables"]
    assert orders == {"view": True, "read": True, "write": False, "manage": False, "restricted": True}  # masked
    assert t == {"view": True, "read": True, "write": True, "manage": True, "restricted": False}
    assert hr == {"view": False, "read": False, "write": False, "manage": False, "restricted": False}
    assert lake.decide("bob", [{"namespace": ["sales"], "name": "orders"}])["tables"][0]["restricted"] is True  # row filter


def test_any_error_decides_nothing():
    lake = LakehouseAccess(access(set()), lambda urn: (_ for _ in ()).throw(RuntimeError("down")))
    out = lake.decide("ana", [{"namespace": ["sales"], "name": "orders"}])
    assert out["schemas"] is False and out["tables"][0]["read"] is False and out["tables"][0]["restricted"] is True

    def boom(*a):
        raise RuntimeError("pdp down")
    lake = LakehouseAccess(TrinoAccess(boom, boom, parse_catalogs({})), lambda urn: [])
    d = lake.decide("ana", [{"namespace": ["s"], "name": "t"}])["tables"][0]
    assert not (d["view"] or d["read"] or d["write"] or d["manage"])  # the policy engine is down: nothing


def test_schema_management_is_a_system_privilege():
    lake = LakehouseAccess(access({("admin", SYSTEM, "*")}), lambda urn: [])
    assert lake.decide("admin", [])["schemas"] is True


def test_spark_lineage_is_renamed_to_lakehouse_tables():
    lake = LakehouseAccess(access(set()), lambda urn: [], catalog_uris=("http://catalog:8181/",))
    sym = lambda name: {"symlinks": {"identifiers": [{"namespace": "http://catalog:8181", "name": name, "type": "TABLE"}]}}  # noqa: E731
    event = {"inputs": [{"namespace": "s3://lakehouse", "name": "warehouse/analytics/products-1", "facets": sym("analytics.products")}],
             "outputs": [{"namespace": "s3://lakehouse", "name": "warehouse/analytics/cheap-2", "facets": {
                 **sym("analytics.cheap"),
                 "columnLineage": {"fields": {"price": {"inputFields": [
                     {"namespace": "s3://lakehouse", "name": "warehouse/analytics/products-1", "field": "price"}]}}}}}],
             "job": {"namespace": "spark", "name": "j"}}
    out = lake.remap_lineage(event)
    assert (out["inputs"][0]["namespace"], out["inputs"][0]["name"]) == ("trino://", "lakehouse.analytics.products")
    assert out["outputs"][0]["name"] == "lakehouse.analytics.cheap"
    assert out["outputs"][0]["facets"]["columnLineage"]["fields"]["price"]["inputFields"][0]["name"] == "lakehouse.analytics.products"
    # Another catalog's tables, and events without symlinks, are left alone.
    other = {**event, "inputs": [{**event["inputs"][0], "facets": {"symlinks": {"identifiers": [
        {"namespace": "http://elsewhere", "name": "a.b", "type": "TABLE"}]}}}], "outputs": []}
    assert lake.remap_lineage(other) == other
    assert LakehouseAccess(access(set()), lambda urn: []).remap_lineage(event) == event
