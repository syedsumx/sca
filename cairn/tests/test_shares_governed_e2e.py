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
    """The connection Cairn's shares run on (CAIRN_SHARES_CONNECTION, here "lakehouse"): Trino over TLS, each
    statement signed in as the person it runs for. ``build_services`` wired governed copies and functions to it."""
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    before = s.data_access.registry, s.shares.governed.catalog
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    s.shares.governed.catalog = "lake"                  # the lakehouse's catalog in this Trino
    yield s
    s.data_access.registry, s.shares.governed.catalog = before


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


def test_a_stored_functions_definition_is_shared_and_works_for_the_recipient(cairn, trino, lake, governed):  # noqa: F811
    """A stored function is added to a share by someone who may manage it; the recipient reads its definition with
    the share's credential, and the statement it is given defines a working function (here: created again under
    another schema on the same Trino, standing in for the recipient's engine)."""
    url, s = cairn
    trino("CREATE SCHEMA IF NOT EXISTS routines.pricing")
    trino("CREATE OR REPLACE FUNCTION routines.pricing.margin(price double, cost double) RETURNS double "
          "COMMENT 'Margin as a share of price' RETURN (price - cost) / price")
    trino("CREATE OR REPLACE FUNCTION routines.pricing.margin(price bigint) RETURNS double RETURN 0.25 * price")

    share = api(url, ADMIN, "POST", "/v1/shares", json={"name": "partner-fn", "recipient": "Partner Fn", "expires_days": 7}).json()
    sid, secret = share["credential"].split(":", 1)
    partner = httpx.Client(base_url=url, auth=(sid, secret), timeout=60)
    assert partner.get("/v1/shares/recipient/functions").json() == {"functions": []}

    # Ana has a share but may not manage the function; a table or a built-in is not a stored function.
    api(url, ANA, "POST", "/v1/shares", json={"name": "ana-fn", "recipient": "x", "tables": ["analytics.products"], "expires_days": 7})
    assert api(url, ANA, "POST", "/v1/shares/ana-fn/functions", json={"function": "routines.pricing.margin"}).status_code == 404
    assert api(url, ANA, "POST", "/v1/shares/partner-fn/functions", json={"function": "routines.pricing.margin"}).status_code == 404
    for bad, status in (("routines.pricing.nope", 404), ("lake.sales.orders", 404), ("margin", 422),
                        ('routines.pricing."x; DROP TABLE y"', 422)):
        r = api(url, ADMIN, "POST", "/v1/shares/partner-fn/functions", json={"function": bad})
        assert r.status_code == status, (bad, r.text)
    r = api(url, ADMIN, "POST", "/v1/shares/partner-fn/functions", json={"function": "routines.pricing.margin"})
    assert r.status_code == 200 and [(f["function"], f["overloads"]) for f in r.json()["functions"]] == [
        ("routines.pricing.margin", 2)], r.text

    (fn,) = partner.get("/v1/shares/recipient/functions").json()["functions"]
    assert fn["function"] == "routines.pricing.margin" and fn["name"] == "margin" and len(fn["overloads"]) == 2
    two = next(o for o in fn["overloads"] if "cost" in o["arguments"])
    assert two["returns"] == "double" and two["language"] == "SQL" and "price - cost" in two["body"].replace("(", "").replace(")", "")
    assert two["comment"] == "Margin as a share of price"
    # The statements define the function for the recipient: under its own schema, it computes the same.
    trino("CREATE SCHEMA IF NOT EXISTS routines.partner")
    try:
        for o in fn["overloads"]:
            assert o["statement"].count("routines.pricing.margin") == 1, o["statement"]
            trino(o["statement"].replace("routines.pricing.margin", "routines.partner.margin"))
        assert trino("SELECT routines.partner.margin(200e0, 150e0) AS a, routines.partner.margin(8) AS b") == [{"a": 0.25, "b": 2.0}]
    finally:
        for sig in ("double, double", "bigint"):
            try:
                trino(f"DROP FUNCTION IF EXISTS routines.partner.margin({sig})")
            except RuntimeError:
                pass

    # What is shared is the definition kept when it was attached: a change reaches the recipient on attaching again.
    trino("CREATE OR REPLACE FUNCTION routines.pricing.margin(price bigint) RETURNS double RETURN 0.5 * price")
    one = lambda: next(o for o in partner.get("/v1/shares/recipient/functions").json()["functions"][0]["overloads"]  # noqa: E731
                       if "cost" not in o["arguments"])
    assert "0.25" in one()["body"]
    assert api(url, ADMIN, "POST", "/v1/shares/partner-fn/functions", json={"function": "routines.pricing.margin"}).status_code == 200
    assert "0.5" in one()["body"]

    # On Trino itself, a function's definition is read by who may manage it, and by no one else.
    assert "price - cost" in str(trino("SHOW CREATE FUNCTION routines.pricing.margin")).replace("(", "").replace(")", "")
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SHOW CREATE FUNCTION routines.pricing.margin", user="ana")
    # A share's credential calls nothing on the provider's Trino, and a person's session is not a share's.
    with pytest.raises(RuntimeError):
        trino("SELECT routines.pricing.margin(8)", user=f"share:{sid}")
    assert api(url, ADMIN, "GET", "/v1/shares/recipient/functions").status_code == 401
    actions = {e["action"] for e in s.audit.entries("shares", limit=200)}
    assert {"share.function_attached", "share.functions_fetched"} <= actions

    # Detached, then revoked.
    assert api(url, ANA, "DELETE", "/v1/shares/partner-fn/functions/routines.pricing.margin").status_code == 404
    r = api(url, ADMIN, "DELETE", "/v1/shares/partner-fn/functions/routines.pricing.margin")
    assert r.status_code == 200 and r.json()["functions"] == []
    assert partner.get("/v1/shares/recipient/functions").json() == {"functions": []}
    assert api(url, ADMIN, "POST", "/v1/shares/partner-fn/functions", json={"function": "routines.pricing.margin"}).status_code == 200
    assert api(url, ADMIN, "POST", f"/v1/shares/{sid}/revoke").json()["state"] == "revoked"
    assert partner.get("/v1/shares/recipient/functions").status_code in (401, 403)


def test_a_clean_room_party_brings_its_table_from_a_share_in_its_own_install(cairn, trino, lake, governed):  # noqa: F811
    """A partner with an install of its own shares a table there (a governed copy: its addresses masked by its own
    policies) and has the room fetch it, instead of exporting a file. Here one Cairn plays both installs: the
    protocol between them is the real one (OAuth client credentials, Iceberg REST, the catalog's scoped storage keys).
    The room reads what the partner's policies let leave, and nothing after the partner revokes its share."""
    import json

    from test_trino_e2e import CATALOG_TOKEN, STORE_KEY

    from cairn.dataaccess.cleanrooms import catalog_writer

    url, s = cairn
    # The partner's install: a share of its orders, as a governed copy.
    share = api(url, ADMIN, "POST", "/v1/shares", json={"name": "room-feed", "recipient": "The joint room",
                                                        "governed": ["sales.orders"], "expires_days": 7}).json()
    feed = {"catalog": lake["catalog"], "token_url": f"{url}/v1/shares/oauth/tokens", "credential": share["credential"],
            "source": "shared_room_feed.orders"}
    masked = [r["email"] for r in trino("SELECT email FROM lake.sales.orders ORDER BY id", user="ana")]

    # The room's install: Ana and the partner, and Ana's own table.
    cr, ana, north = s.clean_rooms, "urn:li:corpuser:ana", "partner:north"
    was = cr.default_catalog, cr.write_table, cr.pull_http, cr.pull_private
    cr.default_catalog = "lake"
    cr.write_table = catalog_writer({"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                     "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                     "s3.region": "us-east-1", "s3.path-style-access": "true"})
    trino("CREATE TABLE IF NOT EXISTS lake.sales.tiers AS SELECT * FROM (VALUES (1, 'gold'), (2, 'gold'), (3, 'basic'), "
          "(4, 'basic')) AS t(id, tier)")
    try:
        room = cr.create(ana, {"name": "joint", "parties": [ana, north], "min_group": 2})
        cr.contribute(ana, "joint", "lake.sales.tiers")
        base, auth = f"{url}/v1/clean-rooms/joint/partner", ("north", room["partner_secrets"][north])
        pull = lambda body, **kw: httpx.post(f"{base}/data/orders/pull", auth=kw.get("auth", auth), json=body, timeout=120)  # noqa: E731

        # Addresses must be public https ones: this test's are neither, until the operator allows it. A link-local
        # address (a cloud's metadata service) is never reached.
        r = pull(feed)
        assert r.status_code == 400 and "https" in r.text, r.text
        cr.pull_http = True
        assert pull(feed).status_code == 403
        cr.pull_private = True
        assert pull({**feed, "catalog": "http://169.254.169.254/latest"}).status_code == 403
        assert pull({**feed, "token_url": "http://169.254.169.254/latest"}).status_code == 403

        # Only the partner pulls, with a share that holds the table.
        assert httpx.post(f"{base}/data/orders/pull", json=feed).status_code == 401
        assert pull({**feed, "credential": share["id"] + ":not-the-secret"}).status_code == 502
        assert pull({**feed, "source": "sales.orders"}).status_code == 502          # the source itself is not shared
        assert pull({**feed, "source": "orders"}).status_code == 400
        r = pull(feed)
        assert r.status_code == 200, r.text
        table = r.json()["table"]
        assert table == "lake.cleanrooms.joint__north__orders" and r.json()["rows"] == 3
        assert r.json()["columns"] == ["id", "email", "region"]

        # The room holds what the partner's policies let leave (masked addresses); only the room reads it.
        got = trino(f"SELECT email FROM {table} ORDER BY id", user="cleanroom-joint")
        assert [x["email"] for x in got] == masked and masked != RAW
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino(f"SELECT count(*) AS n FROM {table}", user="ana")

        # The parties join in aggregate, once both approve.
        sql = f"SELECT t.tier, count(*) AS orders FROM lake.sales.tiers t JOIN {table} o ON t.id = o.id GROUP BY t.tier"
        assert httpx.put(f"{base}/templates/by_tier", auth=auth, json={"sql": sql}).status_code == 200
        cr.approve(ana, "joint", "by_tier")
        run = httpx.post(f"{base}/templates/by_tier/run", auth=auth, timeout=120)
        assert run.status_code == 200 and {r[0]: r[1] for r in run.json()["rows"]} == {"gold": 2}, run.text   # basic: 1 < k

        # Pulling again takes the table as it is then.
        trino("INSERT INTO lake.sales.orders VALUES (4, 'ada@acme.com', 'EU')")
        try:
            assert api(url, ADMIN, "POST", f"/v1/shares/{share['id']}/refresh").status_code == 200
            assert pull(feed).json()["rows"] == 4
            run = httpx.post(f"{base}/templates/by_tier/run", auth=auth, timeout=120)
            assert {r[0]: r[1] for r in run.json()["rows"]} == {"gold": 2, "basic": 2}, run.text
        finally:
            trino("DELETE FROM lake.sales.orders WHERE id = 4")

        # Recorded, without the credential.
        acts = [e for e in s.audit.entries("sharing", limit=100) if e["action"] == "clean_room.data_pulled"]
        assert acts and acts[0]["actor"] == north and acts[0]["details"]["source"] == "shared_room_feed.orders"
        assert share["credential"].split(":", 1)[1] not in json.dumps(s.audit.entries("sharing", limit=100))

        # The partner ends it in its own install: its share revoked, nothing more is fetched.
        assert api(url, ADMIN, "POST", f"/v1/shares/{share['id']}/revoke").json()["state"] == "revoked"
        assert pull(feed).status_code == 502
    finally:
        cr.default_catalog, cr.write_table, cr.pull_http, cr.pull_private = was
