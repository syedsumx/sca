"""Sharing a table that has a column mask or a row filter, end to end (L169): the share lists it under ``governed``,
Trino builds a copy as the share's maker with the recipient's mask and filter applied, and the recipient reads the
copy with pyiceberg from Cairn Catalog. A policy change on the source closes the copy until it is rebuilt; a table
under an aggregation policy is not shared at all; revoking the share drops the copy.

Needs docker (Trino) and the binaries: Cairn Store (with STS) and Cairn Catalog (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import shutil

import httpx
import pytest

pa = pytest.importorskip("pyarrow")

from test_spark_e2e import ANA, BOB, PATS, lake, people  # noqa: E402, F401  (fixtures)
from test_trino_e2e import ORDERS, _ready, cairn, trino  # noqa: E402, F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and shutil.which("docker")), reason="needs docker, the store and the catalog")

ADMIN = "pat-admin-" + "d" * 20
PATS[ADMIN] = "urn:li:corpuser:admin"          # the maker: sees sales.orders as it is, and may manage it
RAW = ["jane@acme.com", "bo@example.org", "li@acme.com"]


def recipient(cairn_url, catalog, credential):
    from pyiceberg.catalog.rest import RestCatalog

    return RestCatalog("partner", uri=catalog, credential=credential, scope="catalog",
                       **{"oauth2-server-uri": f"{cairn_url}/v1/shares/oauth/tokens"})


def api(url, token, method, path, **kw):
    return httpx.request(method, f"{url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=300, **kw)


@pytest.fixture(scope="module")
def governed(cairn, trino, lake):  # noqa: F811
    """Governed copies wired as ``build_services`` does when a connection is named: statements run on Trino as the
    share's maker, through the registry's connection."""
    from cairn.dataaccess.governed_shares import GovernedCopies
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    before = s.data_access.registry
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})

    def run(actor, sql):
        return s.data_access.run_on_trino(s.data_access.registry.definitions()["lakehouse"], actor, sql, 100_000)[1]
    s.shares.governed = GovernedCopies(s.trino_access, run, "lake")
    yield s
    s.shares.governed, s.data_access.registry = None, before


def rows(cat, name):
    return sorted(cat.load_table(name).scan().to_arrow().to_pylist(), key=lambda r: r["id"])


def test_a_masked_table_is_shared_as_a_copy_with_the_recipients_policies_applied(cairn, trino, lake, governed):  # noqa: F811
    from pyiceberg.exceptions import ForbiddenError, NoSuchTableError

    from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target

    url, s = cairn
    not_admin = {"attr": "subject.urn", "op": "neq", "value": "urn:li:corpuser:admin"}
    # As it is, the table is not shared: its files hold the addresses the mask hides. The refusal says what to do.
    r = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-g", "recipient": "Partner G", "tables": ["sales.orders"],
                                                    "expires_days": 7})
    assert r.status_code == 409 and "governed" in r.text, r.text
    # Only who may manage the source shares it, governed or not; and never both ways at once.
    r = api(url, ANA, "POST", "/v1/shares", json={"name": "partner-g", "recipient": "Partner G", "governed": ["sales.orders"],
                                                  "expires_days": 7})
    assert r.status_code == 403 and "MANAGE" in r.text, r.text
    r = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-g", "recipient": "Partner G", "tables": ["sales.orders"],
                                                    "governed": ["sales.orders"], "expires_days": 7})
    assert r.status_code == 422, r.text

    r = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-g", "recipient": "Partner G", "governed": ["sales.orders"],
                                                    "tables": ["analytics.products"], "expires_days": 7, "refresh_minutes": 60})
    assert r.status_code == 200, r.text
    share = r.json()
    sid = share["id"]
    assert share["tables"] == ["analytics.products", "shared_partner_g.orders"]
    assert [(g["source"], g["target"]) for g in share["governed"]] == [("sales.orders", "shared_partner_g.orders")]
    assert share["refresh_minutes"] == 60

    # The recipient reads the copy: every row, the addresses masked as Trino masks them for anyone but the admin.
    cat = recipient(url, lake["catalog"], share["credential"])
    assert cat.list_namespaces() == [("analytics",), ("shared_partner_g",)]
    masked = [r["email"] for r in trino("SELECT email FROM lake.sales.orders ORDER BY id", user="ana")]
    assert masked != RAW and len(masked) == 3
    got = rows(cat, "shared_partner_g.orders")
    assert [r["email"] for r in got] == masked and [r["region"] for r in got] == ["EU", "US", "EU"]
    with pytest.raises((ForbiddenError, NoSuchTableError)):       # the source itself stays closed
        cat.load_table("sales.orders")
    # The files hold no address: what the recipient is handed is all there is.
    files = cat.load_table("shared_partner_g.orders").scan().plan_files()
    assert files and all("shared_partner_g" in f.file.file_path for f in files)

    # Another share of the same name cannot be made, and so cannot rebuild or drop this share's copy.
    r = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-g", "recipient": "x", "governed": ["hr.salaries"],
                                                    "expires_days": 7})
    assert r.status_code == 409, r.text
    assert len(rows(cat, "shared_partner_g.orders")) == 3

    # A policy changes on the source (a row filter for everyone but the admin): the copy holds rows the recipient
    # may no longer see, so it is refused at once, until it has been rebuilt.
    bundle = s.pdp.bundle
    bundle = bundle() if callable(bundle) else bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(
        id="orders-eu-only", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=[ORDERS]),
        condition=not_admin, obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})])]))
    try:
        with pytest.raises((ForbiddenError, NoSuchTableError)):
            cat.load_table("shared_partner_g.orders")
        assert sorted(r["sku"] for r in cat.load_table("analytics.products").scan().to_arrow().to_pylist()) == ["a", "b", "c"]
        assert api(url, BOB, "POST", f"/v1/shares/{sid}/refresh").status_code == 404      # not his share
        r = api(url, ADMIN, "POST", f"/v1/shares/{sid}/refresh")
        assert r.status_code == 200 and not r.json()["governed"][0]["error"], r.text
        got = rows(cat, "shared_partner_g.orders")
        assert [r["id"] for r in got] == [1, 3] and [r["email"] for r in got] == [masked[0], masked[2]]

        # Under an aggregation policy only aggregates may leave the table: a copy is rows, so it is refused, and
        # the copy made before the policy closes as well (and stays closed: a refresh cannot rebuild it).
        s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(
            id="orders-groups", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=[ORDERS]),
            condition=not_admin, obligations=[Obligation(type="aggregation", params={"min_group": 2})])]))
        r = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-h", "recipient": "Partner H",
                                                        "governed": ["sales.orders"], "expires_days": 7})
        assert r.status_code == 409 and "aggregat" in r.text, r.text
        assert [x["name"] for x in api(url, ADMIN, "GET", "/v1/shares").json()["shares"]] == ["partner-g"]   # nothing kept
        assert not lake["admin"].table_exists("shared_partner_h.orders")
        with pytest.raises((ForbiddenError, NoSuchTableError)):
            cat.load_table("shared_partner_g.orders")
        r = api(url, ADMIN, "POST", f"/v1/shares/{sid}/refresh")
        assert r.status_code == 200 and "aggregat" in r.json()["governed"][0]["error"], r.text
        with pytest.raises((ForbiddenError, NoSuchTableError)):
            cat.load_table("shared_partner_g.orders")
    finally:
        s.pdp.set_bundle(bundle)

    # Back under the first policies, the scheduler's sweep rebuilds what is due (here: everything, with no wait).
    with pytest.raises((ForbiddenError, NoSuchTableError)):       # built under the row filter: not what holds now
        cat.load_table("shared_partner_g.orders")
    s.shares.refresh(sid, "urn:li:corpuser:admin", False)
    assert len(rows(cat, "shared_partner_g.orders")) == 3

    # Revoked: the copy is dropped with the share.
    assert lake["admin"].table_exists("shared_partner_g.orders")
    assert api(url, ADMIN, "POST", f"/v1/shares/{sid}/revoke").json()["state"] == "revoked"
    assert not lake["admin"].table_exists("shared_partner_g.orders")
    assert lake["admin"].table_exists("sales.orders")


def test_copies_are_rebuilt_when_due_and_dropped_when_the_share_expires(cairn, trino, lake, governed):  # noqa: F811
    from sqlalchemy import update

    from cairn.dataaccess.shares import lakehouse_shares

    url, s = cairn
    r = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-s", "recipient": "Partner S", "governed": ["sales.orders"],
                                                    "expires_days": 7, "refresh_minutes": 5})
    assert r.status_code == 200, r.text
    share = r.json()
    cat = recipient(url, lake["catalog"], share["credential"])
    assert len(rows(cat, "shared_partner_s.orders")) == 3
    # Nothing is due yet; new rows in the source reach the copy when it is.
    assert s.shares.sweep_governed() == 0
    trino("INSERT INTO lake.sales.orders VALUES (4, 'ada@acme.com', 'EU')")
    try:
        assert len(rows(cat, "shared_partner_s.orders")) == 3
        import json as _json
        with s.engine.begin() as conn:               # as if the last rebuild were ten minutes ago
            row = conn.execute(lakehouse_shares.select().where(lakehouse_shares.c.id == share["id"])).mappings().one()
            entries = [{**g, "refreshed_s": g["refreshed_s"] - 600} for g in _json.loads(row["governed"])]
            conn.execute(update(lakehouse_shares).where(lakehouse_shares.c.id == share["id"]).values(governed=_json.dumps(entries)))
        assert s.shares.sweep_governed() == 1
        got = rows(cat, "shared_partner_s.orders")
        assert [r["id"] for r in got] == [1, 2, 3, 4] and "ada@acme.com" not in [r["email"] for r in got]
        # Expired: the sweep drops the copy.
        with s.engine.begin() as conn:
            conn.execute(update(lakehouse_shares).where(lakehouse_shares.c.id == share["id"]).values(expires_s=1))
        s.shares.sweep_governed()
        assert not lake["admin"].table_exists("shared_partner_s.orders")
    finally:
        trino("DELETE FROM lake.sales.orders WHERE id = 4")
