"""Delta Sharing end to end: a share made in Cairn is read by a recipient in another organisation with the real
``delta-sharing`` client (pandas), from pre-signed Cairn Store URLs of the table's Parquet files, sees only what is
shared, and is refused at once when the share is revoked.

The recipient runs as its own process, in its own Python environment (as another organisation would):
``CAIRN_DELTA_SHARING_PYTHON`` is a Python with ``delta-sharing==1.4.3`` installed (Apache-2.0). Real Cairn Store
(with STS) and Cairn Catalog (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import json
import os
import subprocess
import textwrap
import time

import httpx
import pytest

pa = pytest.importorskip("pyarrow")

from test_spark_e2e import ANA, BOB, lake, people  # noqa: E402, F401  (fixtures)
from test_trino_e2e import _ready, cairn  # noqa: E402, F401  (fixtures)

CLIENT = os.environ.get("CAIRN_DELTA_SHARING_PYTHON", "")
pytestmark = pytest.mark.skipif(not (_ready() and CLIENT), reason="needs the binaries and CAIRN_DELTA_SHARING_PYTHON")

RECIPIENT = textwrap.dedent('''
    import json, sys
    import delta_sharing

    profile, what = sys.argv[1], sys.argv[2]
    try:
        if what == "tables":
            out = sorted(f"{t.share}.{t.schema}.{t.name}" for t in delta_sharing.SharingClient(profile).list_all_tables())
        else:
            df = delta_sharing.load_as_pandas(f"{profile}#{what}")
            out = sorted(df.to_dict("records"), key=lambda r: json.dumps(r, sort_keys=True, default=str))
        print(json.dumps({"ok": out}, default=str))
    except Exception as e:  # what the client reports
        print(json.dumps({"error": f"{type(e).__name__}: {e}"[:800]}))
''')


def api(url, token, method, path, **kw):
    return httpx.request(method, f"{url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30, **kw)


def recipient(tmp_path, profile, what):
    """The real delta-sharing client, in the recipient's own environment, with the profile file it was given."""
    pf, script = tmp_path / "share.json", tmp_path / "recipient.py"
    pf.write_text(json.dumps(profile))
    script.write_text(RECIPIENT)
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    out = subprocess.run([CLIENT, "-I", str(script), str(pf), what], capture_output=True, text=True, timeout=300,
                         env=env, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_recipient_reads_a_share_with_the_delta_sharing_client_until_it_is_revoked(cairn, lake, tmp_path):  # noqa: F811
    url, s = cairn
    ds = s.shares.delta
    ds.catalog_url = lake["catalog"]                 # where Cairn reaches Cairn Catalog (CAIRN_SHARES_CATALOG_INTERNAL_URL)
    admin = lake["admin"]
    more = pa.table({"sku": ["d", "e"], "price": pa.array([11, 13], pa.int64())})
    admin.create_table("analytics.catalogue", schema=more.schema).append(more)
    r = api(url, ANA, "POST", "/v1/shares", json={"name": "delta-partner", "recipient": "Partner D (data@partner.example)",
                                                  "tables": ["analytics.products", "analytics.catalogue"], "expires_days": 7})
    assert r.status_code == 200, r.text
    share = r.json()
    profile = share["delta_sharing"]
    assert profile["shareCredentialsVersion"] == 1 and profile["endpoint"].endswith("/v1/delta-sharing")
    assert profile["bearerToken"].startswith(share["id"] + ".") and profile["expirationTime"].endswith("Z")
    assert "delta_sharing" not in api(url, ANA, "GET", f"/v1/shares/{share['id']}").json()     # shown once

    # The recipient lists and reads with the real client: only what is shared.
    got = recipient(tmp_path, profile, "tables")
    assert got == {"ok": ["delta-partner.analytics.catalogue", "delta-partner.analytics.products"]}, got
    got = recipient(tmp_path, profile, "delta-partner.analytics.products")
    assert got == {"ok": [{"price": 5, "sku": "a"}, {"price": 7, "sku": "b"}, {"price": 9, "sku": "c"}]}, got

    # What the client got: pre-signed URLs of the table's own Parquet files, signed with scoped keys, short-lived.
    bearer = {"Authorization": f"Bearer {profile['bearerToken']}"}
    base = f"{url}/v1/delta-sharing/shares/delta-partner/schemas/analytics/tables"
    q = httpx.post(f"{base}/products/query", json={}, headers=bearer, timeout=60)
    assert q.status_code == 200 and q.headers["delta-table-version"].isdigit(), q.text
    lines = [json.loads(x) for x in q.text.splitlines()]
    assert lines[0] == {"protocol": {"minReaderVersion": 1}} and lines[1]["metaData"]["format"]["provider"] == "parquet"
    files = [x["file"] for x in lines[2:]]
    assert files and all("X-Amz-Security-Token=" in f["url"] and "X-Amz-Signature=" in f["url"] for f in files)
    assert all(f["expirationTimestamp"] <= (time.time() + ds.url_seconds + 5) * 1000 for f in files)
    assert sum(json.loads(f["stats"])["numRecords"] for f in files) == 3
    body = httpx.get(files[0]["url"], timeout=30)
    assert body.status_code == 200 and body.content[:4] == b"PAR1"
    meta = httpx.get(f"{base}/products/metadata", headers=bearer, timeout=30)
    assert [f["name"] for f in json.loads(json.loads(meta.text.splitlines()[1])["metaData"]["schemaString"])["fields"]] \
        == ["sku", "price"]
    # Nothing else: unshared tables, other shares' names, time travel, a wrong token.
    for path in ("/shares/delta-partner/schemas/sales/tables/orders/query", "/shares/other/schemas/analytics/tables"):
        r = httpx.request("POST" if path.endswith("query") else "GET", f"{url}/v1/delta-sharing{path}", json={},
                          headers=bearer, timeout=30)
        assert r.status_code == 404, (path, r.text)
    assert httpx.post(f"{base}/products/query", json={"version": 1}, headers=bearer, timeout=30).status_code == 400
    for headers in ({}, {"Authorization": f"Bearer {share['id']}.wrong"}, {"Authorization": "Bearer nonsense"}):
        r = httpx.get(f"{url}/v1/delta-sharing/shares", headers=headers, timeout=30)
        assert r.status_code == 401 and r.json()["errorCode"] == "UNAUTHENTICATED", r.text

    # A mask or row filter added later takes the table out of the share at once (its files hold what they hide).
    real = s.lakehouse._restricted
    s.lakehouse._restricted = lambda actor, groups, urn: "catalogue" in urn or real(actor, groups, urn)
    try:
        assert recipient(tmp_path, profile, "tables") == {"ok": ["delta-partner.analytics.products"]}
        assert httpx.post(f"{base}/catalogue/query", json={}, headers=bearer, timeout=30).status_code == 404
    finally:
        s.lakehouse._restricted = real

    # A column renamed after its files were written: the files carry the old name, so the table is not served.
    with admin.load_table("analytics.catalogue").update_schema() as u:
        u.rename_column("price", "list_price")
    r = httpx.post(f"{base}/catalogue/query", json={}, headers=bearer, timeout=30)
    assert r.status_code == 400 and "renamed" in r.json()["message"], r.text

    # Someone else cannot revoke it; its maker does, and the recipient is refused at once.
    assert api(url, BOB, "POST", f"/v1/shares/{share['id']}/revoke").status_code == 404
    assert api(url, ANA, "POST", f"/v1/shares/{share['id']}/revoke").json()["state"] == "revoked"
    got = recipient(tmp_path, profile, "delta-partner.analytics.products")
    assert "error" in got and "403" in got["error"], got
    r = httpx.post(f"{base}/products/query", json={}, headers=bearer, timeout=30)
    assert r.status_code == 403 and r.json()["errorCode"] == "PERMISSION_DENIED", r.text

    s.shares.flush_usage()
    usage = api(url, ANA, "GET", f"/v1/shares/{share['id']}/usage").json()
    assert "analytics.products" in usage["tables_read"] and usage["totals"]["refusals"] >= 1, usage
    actions = [e for e in s.audit.entries(category="shares", limit=500) if e["action"] == "share.delta_query"]
    assert actions and actions[0]["details"]["table"] in ("analytics.products", "analytics.catalogue")
