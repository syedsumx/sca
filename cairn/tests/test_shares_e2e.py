"""Sharing lakehouse tables with a recipient outside the organisation, end to end: Cairn makes the share,
the recipient reads with pyiceberg from Cairn Catalog (an OAuth client credential, scoped read-only
storage keys), sees only what is shared, and loses access at once when the share is revoked or expires.

Needs docker-free binaries only: Cairn Store (with STS) and Cairn Catalog (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import time

import httpx
import pytest

pa = pytest.importorskip("pyarrow")

from test_spark_e2e import ANA, BOB, lake, people  # noqa: E402, F401  (fixtures)
from test_trino_e2e import _ready, cairn  # noqa: E402, F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")


def recipient(cairn_url, catalog, credential):
    from pyiceberg.catalog.rest import RestCatalog

    return RestCatalog("partner", uri=catalog, credential=credential, scope="catalog",
                       **{"oauth2-server-uri": f"{cairn_url}/v1/shares/oauth/tokens"})


def api(url, token, method, path, **kw):
    return httpx.request(method, f"{url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30, **kw)


def test_a_partner_reads_only_what_is_shared_until_it_is_revoked(cairn, lake):  # noqa: F811
    url, s = cairn
    admin = lake["admin"]
    admin.create_table("analytics.private", schema=pa.schema([("x", pa.int64())]))
    # Only what the person may manage is shared, and the credential is shown once.
    r = api(url, ANA, "POST", "/v1/shares", json={"name": "partner-a", "recipient": "Partner A (data@partner.example)",
                                                  "tables": ["sales.orders"], "expires_days": 7})
    assert r.status_code == 403 and "MANAGE" in r.text
    r = api(url, ANA, "POST", "/v1/shares", json={"name": "partner-a", "recipient": "Partner A (data@partner.example)",
                                                  "tables": ["analytics.products"], "expires_days": 7})
    assert r.status_code == 200, r.text
    share = r.json()
    assert share["state"] == "active" and share["credential"].startswith(share["id"] + ":")
    assert "credential" not in api(url, ANA, "GET", f"/v1/shares/{share['id']}").json()
    assert api(url, ANA, "POST", "/v1/shares", json={"name": "partner-a", "recipient": "x", "tables": ["analytics.products"],
                                                     "expires_days": 7}).status_code == 409

    # The recipient sees the shared table and its namespace, nothing else.
    cat = recipient(url, lake["catalog"], share["credential"])
    assert cat.list_namespaces() == [("analytics",)]
    assert cat.list_tables("analytics") == [("analytics", "products")]
    t = cat.load_table("analytics.products")
    assert sorted(r["sku"] for r in t.scan().to_arrow().to_pylist()) == ["a", "b", "c"]
    io = t.io.properties
    assert io.get("s3.session-token") and io.get("s3.access-key-id") != lake["env"]["CAIRN_CATALOG_S3_ACCESS_KEY"]
    from pyiceberg.exceptions import AuthorizationExpiredError, ForbiddenError, NoSuchTableError, OAuthError, UnauthorizedError
    for name in ("analytics.private", "sales.orders", "hr.salaries"):
        with pytest.raises((ForbiddenError, NoSuchTableError)):
            cat.load_table(name)
    # Read-only: the storage keys cannot write, and the catalog takes no commit.
    with pytest.raises((ForbiddenError, OSError)):
        t.append(pa.table({"sku": ["z"], "price": pa.array([1], pa.int64())}))
    sid, secret = share["credential"].split(":", 1)
    got = httpx.post(f"{url}/v1/shares/oauth/tokens", auth=(sid, secret), data={"grant_type": "client_credentials"}, timeout=10)
    assert got.status_code == 200 and got.json()["expires_in"] <= 900
    token = "Bearer " + got.json()["access_token"]
    r = httpx.post(f"{lake['catalog']}/v1/namespaces/analytics/tables/products", headers={"Authorization": token},
                   json={"requirements": [], "updates": [{"action": "set-properties", "updates": {"x": "y"}}]}, timeout=10)
    assert r.status_code == 403, r.text

    # Someone else neither sees nor revokes it; its maker does, and the recipient is refused at once.
    assert api(url, BOB, "GET", "/v1/shares").json()["shares"] == []
    assert api(url, BOB, "POST", f"/v1/shares/{share['id']}/revoke").status_code == 404
    assert [x["name"] for x in api(url, ANA, "GET", "/v1/shares").json()["shares"]] == ["partner-a"]
    r = api(url, ANA, "POST", f"/v1/shares/{share['id']}/revoke")
    assert r.status_code == 200 and r.json()["state"] == "revoked"
    with pytest.raises((UnauthorizedError, OAuthError, AuthorizationExpiredError)):
        cat.load_table("analytics.products")
    r = httpx.post(f"{url}/v1/shares/oauth/tokens", data={"grant_type": "client_credentials",
                   "client_id": share["id"], "client_secret": share["credential"].split(":", 1)[1]}, timeout=10)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # The catalog refuses the token the recipient still holds, before its expiry.
    held = s.trino_tokens.mint(f"urn:li:corpuser:share:{share['id']}", 600, audience="lakehouse")["token"]
    r = httpx.get(f"{lake['catalog']}/v1/namespaces/analytics/tables/products",
                  headers={"Authorization": f"Bearer {held}"}, timeout=10)
    assert r.status_code == 401 and "ended" in r.text

    # Everything is audited.
    actions = {e["action"] for e in s.audit.entries(category="shares", limit=200)}
    assert {"share.created", "share.token_issued", "share.revoked"} <= actions


def test_expiry_masks_and_bad_credentials_end_a_share(cairn, lake):  # noqa: F811
    url, s = cairn
    r = api(url, ANA, "POST", "/v1/shares", json={"name": "partner-b", "recipient": "Partner B",
                                                  "tables": ["analytics.products"], "expires_days": 1})
    share = r.json()
    cat = recipient(url, lake["catalog"], share["credential"])
    assert cat.load_table("analytics.products") is not None
    # A mask or row filter added later stops the table from being read through the share.
    real = s.lakehouse._restricted
    s.lakehouse._restricted = lambda actor, groups, urn: "products" in urn or real(actor, groups, urn)
    try:
        from pyiceberg.exceptions import ForbiddenError, NoSuchTableError
        with pytest.raises((ForbiddenError, NoSuchTableError)):
            cat.load_table("analytics.products")
        r = api(url, ANA, "POST", "/v1/shares", json={"name": "partner-c", "recipient": "C",
                                                      "tables": ["analytics.products"], "expires_days": 1})
        assert r.status_code == 409 and "mask" in r.text
    finally:
        s.lakehouse._restricted = real
    # Expired: no token, no request.
    from sqlalchemy import update

    from cairn.dataaccess.shares import lakehouse_shares
    with s.engine.begin() as conn:
        conn.execute(update(lakehouse_shares).where(lakehouse_shares.c.id == share["id"]).values(expires_s=int(time.time()) - 1))
    assert api(url, ANA, "GET", f"/v1/shares/{share['id']}").json()["state"] == "expired"
    from pyiceberg.exceptions import OAuthError, UnauthorizedError
    with pytest.raises((OAuthError, UnauthorizedError)):
        recipient(url, lake["catalog"], share["credential"]).list_namespaces()
    # A wrong secret is an unknown client; ordinary users cannot pose as a share.
    r = httpx.post(f"{url}/v1/shares/oauth/tokens", data={"grant_type": "client_credentials",
                   "client_id": share["id"], "client_secret": "nope"}, timeout=10)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    assert api(url, ANA, "POST", "/v1/shares", json={"name": "Bad Name", "recipient": "x", "tables": ["analytics.products"],
                                                     "expires_days": 1}).status_code == 422
    assert api(url, ANA, "POST", "/v1/shares", json={"name": "too-long", "recipient": "x", "tables": ["analytics.products"],
                                                     "expires_days": 5000}).status_code == 422
