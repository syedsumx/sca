"""SaaS connectors (L61) end to end: Salesforce accounts and HubSpot contacts land in governed Iceberg tables in
Cairn Catalog (on Cairn Store), incrementally, and read through Trino under Cairn's policies.

The SaaS side is a local server that answers the documented endpoints the connectors call (Salesforce: the
client-credentials token and the REST query API with nextRecordsUrl paging; HubSpot: CRM v3 search with
filterGroups and after paging), so the test covers the connectors' HTTP, paging, cursors and upserts; it is not
a substitute for a run against a real Salesforce org or HubSpot account. The keys are in a real secret scope."""

from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, the store and catalog binaries")
OWNER, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:bob"


class Saas:
    """Records the two APIs serve, and what they were asked."""

    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.asked: list[str] = []
        self.token = "sf-access-" + "t" * 20
        self.secret = "sf-client-secret-" + "s" * 16
        self.hub_token = "pat-na1-" + "h" * 24
        self.pages: dict[str, list] = {}
        self.fail_hubspot = False
        # Google Sheets: spreadsheets by id and sheet name; a service account (its public key checks the JWT).
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.sheets: dict[str, dict[str, list]] = {}
        self.sa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.sa_pem = self.sa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()).decode()
        self.google_token = "ya29." + "g" * 30
        self.api_key = "AIza" + "k" * 35
        self.google_claims: list[dict] = []


def handler(saas: Saas):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, token):
            return self.headers.get("Authorization") == f"Bearer {token}"

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            u = urlsplit(self.path)
            if u.path == "/google/token":                         # a service account's JWT, checked as Google does
                f = parse_qs(body)
                try:
                    from cryptography.hazmat.primitives import hashes
                    from cryptography.hazmat.primitives.asymmetric import padding

                    head, claims, sig = f["assertion"][0].split(".")
                    pad = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))  # noqa: E731
                    saas.sa_key.public_key().verify(pad(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
                    c = json.loads(pad(claims))
                    saas.google_claims.append(c)
                    ok = (f["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
                          and c["aud"].endswith("/google/token") and c["scope"].endswith("spreadsheets.readonly")
                          and c["iss"] == "cairn-loader@example.iam.gserviceaccount.com" and c["exp"] > c["iat"])
                except Exception:  # noqa: BLE001 - a bad signature or shape
                    ok = False
                return self._send(200, {"access_token": saas.google_token, "expires_in": 3600}) if ok else \
                    self._send(400, {"error": "invalid_grant"})
            if u.path == "/services/oauth2/token":
                f = parse_qs(body)
                ok = f.get("grant_type") == ["client_credentials"] and f.get("client_secret") == [saas.secret]
                return self._send(200, {"access_token": saas.token, "token_type": "Bearer"}) if ok else \
                    self._send(400, {"error": "invalid_client"})
            m = re.fullmatch(r"/crm/v3/objects/(\w+)/search", u.path)
            if m:
                if not self._authed(saas.hub_token):
                    return self._send(401, {"message": "Authentication credentials not found"})
                if saas.fail_hubspot:
                    return self._send(500, {"message": "internal"})
                q = json.loads(body)
                saas.asked.append("hubspot " + json.dumps(q.get("filterGroups") or []))
                since = int(q["filterGroups"][0]["filters"][0]["value"]) if q.get("filterGroups") else 0

                def ms(r):          # HubSpot filters hs_lastmodifieddate in epoch ms; it returns ISO times
                    return int(datetime.fromisoformat(r["modified"].replace("Z", "+00:00")).timestamp() * 1000)
                rows = sorted((r for r in saas.contacts.values() if ms(r) >= since), key=lambda r: (ms(r), r["id"]))
                start, limit = int(q.get("after") or 0), int(q.get("limit") or 10)
                page = rows[start:start + 2]                         # small pages, to page
                out = {"results": [{"id": r["id"], "properties": {**{p: r.get(p) for p in q["properties"]},
                                                                   "hs_lastmodifieddate": r["modified"]},
                                    "updatedAt": r["modified"]} for r in page], "total": len(rows)}
                assert limit <= 100
                if start + 2 < len(rows):
                    out["paging"] = {"next": {"after": str(start + 2)}}
                return self._send(200, out)
            self._send(404, {})

        def do_GET(self):
            u = urlsplit(self.path)
            m = re.fullmatch(r"/v4/spreadsheets/([^/]+)/values/([^/]+)", u.path)
            if m:
                from urllib.parse import unquote

                q = parse_qs(u.query)
                if not (self._authed(saas.google_token) or q.get("key") == [saas.api_key]):
                    return self._send(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
                assert q.get("valueRenderOption") == ["UNFORMATTED_VALUE"]
                sheet = saas.sheets.get(unquote(m.group(1)), {}).get(unquote(m.group(2)))
                if sheet is None:
                    return self._send(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
                saas.asked.append(f"sheets {unquote(m.group(2))}")
                return self._send(200, {"range": unquote(m.group(2)), "majorDimension": "ROWS", "values": sheet})
            if not self._authed(saas.token):
                return self._send(401, [{"errorCode": "INVALID_SESSION_ID"}])
            if u.path == "/services/data/v61.0/query":
                soql = parse_qs(u.query)["q"][0]
                saas.asked.append("salesforce " + soql)
                m = re.fullmatch(r"SELECT (.+) FROM (\w+)(?: WHERE SystemModstamp >= (\S+))? ORDER BY SystemModstamp ASC", soql)
                assert m, soql
                fields = [f.strip() for f in m.group(1).split(",")]
                since = m.group(3) or ""
                rows = sorted((r for r in saas.accounts.values() if r["SystemModstamp"][:19] >= since[:19]),
                              key=lambda r: (r["SystemModstamp"], r["Id"]))
                recs = [{"attributes": {"type": "Account"}, **{f: r.get(f) for f in fields}} for r in rows]
                key = f"p{len(saas.pages)}"
                saas.pages[key] = recs[2:]
                return self._send(200, {"totalSize": len(recs), "done": len(recs) <= 2, "records": recs[:2],
                                        **({"nextRecordsUrl": f"/services/data/v61.0/query/{key}"} if len(recs) > 2 else {})})
            m = re.fullmatch(r"/services/data/v61.0/query/(p\d+)", u.path)
            if m:
                rest = saas.pages.pop(m.group(1))
                key = f"p{len(saas.pages) + 100}"
                saas.pages[key] = rest[2:]
                return self._send(200, {"totalSize": len(rest), "done": len(rest) <= 2, "records": rest[:2],
                                        **({"nextRecordsUrl": f"/services/data/v61.0/query/{key}"} if len(rest) > 2 else {})})
            self._send(404, {})
    return H


@pytest.fixture
def world(cairn, lake, monkeypatch, tmp_path):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.identity.secret_scopes import SecretScopes
    from cairn.pipelines.saas import SaasSyncs

    monkeypatch.setenv("CAIRN_SAAS_ALLOW_HTTP", "1")
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"k" * 32).decode())
    saas = Saas()
    p = port()
    server = ThreadingHTTPServer(("127.0.0.1", p), handler(saas))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine = create_engine(f"sqlite:///{tmp_path}/saas.db")
    init_db(engine)
    scopes = SecretScopes(engine, lambda a: [], lambda a: False)
    scopes.create(OWNER, {"name": "crm-keys"})                 # Salesforce: a connected app's client credentials
    for k, v in {"instance_url": f"http://127.0.0.1:{p}", "client_id": "cairn", "client_secret": saas.secret}.items():
        scopes.put(OWNER, "crm-keys", k, v)
    scopes.create(OWNER, {"name": "hubspot-keys"})             # HubSpot: a private app's token
    for k, v in {"access_token": saas.hub_token, "base_url": f"http://127.0.0.1:{p}"}.items():
        scopes.put(OWNER, "hubspot-keys", k, v)
    scopes.create(OWNER, {"name": "sheets-keys"})              # Google Sheets: a service account's JSON key
    scopes.put(OWNER, "sheets-keys", "service_account_json", json.dumps({
        "type": "service_account", "client_email": "cairn-loader@example.iam.gserviceaccount.com",
        "private_key": saas.sa_pem, "token_uri": f"http://127.0.0.1:{p}/google/token"}))
    scopes.put(OWNER, "sheets-keys", "base_url", f"http://127.0.0.1:{p}")
    scopes.create(OWNER, {"name": "sheets-public"})            # a sheet shared with anyone with the link: an API key
    for k, v in {"api_key": saas.api_key, "base_url": f"http://127.0.0.1:{p}"}.items():
        scopes.put(OWNER, "sheets-public", k, v)
    _, s = cairn
    audited, told = [], []

    class Audit:
        def record(self, *a):
            audited.append(a)
    syncs = SaasSyncs(engine, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                               "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                               "s3.region": "us-east-1", "s3.path-style-access": "true"},
                      scopes.read, lambda owner, urn: owner == OWNER, audit=Audit(),
                      notify=lambda *a: told.append(a))
    yield {"saas": saas, "syncs": syncs, "scopes": scopes, "audited": audited, "told": told, "s": s}
    server.shutdown()


def account(i, region, modified, name=None):
    return {"Id": f"001{i:015d}", "Name": name or f"Acme {i}", "Region": region,
            "SystemModstamp": f"2026-11-0{modified}T10:00:00.000+0000"}


def test_salesforce_accounts_land_incrementally_in_a_governed_table(world, trino):  # noqa: F811
    saas, syncs = world["saas"], world["syncs"]
    for i, (region, day) in enumerate([("EU", 1), ("US", 1), ("EU", 2), ("US", 2), ("EU", 3)]):
        saas.accounts[str(i)] = account(i, region, day)
    trino("DROP TABLE IF EXISTS lake.sales.sf_accounts")
    syncs.save(OWNER, {"name": "sf-accounts", "kind": "salesforce", "object": "Account",
                       "fields": ["Name", "Region"], "target": "sales.sf_accounts", "secret_scope": "crm-keys",
                       "cron": "0 * * * *"})
    first = syncs.run_now(OWNER, "sf-accounts", wait=True)
    assert first["status"] == "ok" and first["rows"] == 5, first
    assert saas.asked[-1].endswith("FROM Account ORDER BY SystemModstamp ASC")   # first run: everything (in 3 pages)
    rows = trino("SELECT id, name, region FROM lake.sales.sf_accounts ORDER BY id")
    assert [r["name"] for r in rows] == [f"Acme {i}" for i in range(5)]
    # Governed: bob's row filter (region = 'EU') applies to the loaded table like any other sales table.
    assert sorted(r["name"] for r in trino("SELECT name FROM lake.sales.sf_accounts", user="bob")) == \
        ["Acme 0", "Acme 2", "Acme 4"]
    # One account changes and one is added: the next run asks only from the last change and upserts.
    saas.accounts["1"] = account(1, "US", 4, name="Acme One (renamed)")
    saas.accounts["9"] = account(9, "EU", 4)
    second = syncs.run_now(OWNER, "sf-accounts", wait=True)
    # The two changes, and the record at the cursor's own second again (asked with >=, upserted harmlessly).
    assert second["status"] == "ok" and second["rows"] == 3, second
    assert "WHERE SystemModstamp >= 2026-11-03T10:00:00Z" in saas.asked[-1]
    rows = trino("SELECT id, name FROM lake.sales.sf_accounts ORDER BY id")
    assert len(rows) == 6 and {r["name"] for r in rows} >= {"Acme One (renamed)", "Acme 9"}
    assert len({r["id"] for r in rows}) == 6                                   # upserted, not appended twice
    # Nothing new: the cursor's own second is asked again and upserted again; the table is unchanged.
    assert syncs.run_now(OWNER, "sf-accounts", wait=True)["rows"] == 2
    assert len(trino("SELECT id FROM lake.sales.sf_accounts")) == 6
    sync = syncs.get(OWNER, "sf-accounts")
    assert sync["last_status"] == "ok" and sync["cursor"].startswith("2026-11-04T10:00:00")
    assert "sf-client-secret" not in json.dumps(world["audited"], default=str)  # keys never audited
    trino("DROP TABLE lake.sales.sf_accounts")


def test_hubspot_contacts_land_and_failures_are_told(world, trino):  # noqa: F811
    saas, syncs = world["saas"], world["syncs"]
    for i in range(5):
        saas.contacts[str(i)] = {"id": str(100 + i), "email": f"c{i}@acme.com", "firstname": f"C{i}",
                                 "modified": f"2026-11-01T10:00:0{i}.000Z"}
    trino("DROP TABLE IF EXISTS lake.sales.hs_contacts")
    syncs.save(OWNER, {"name": "hs-contacts", "kind": "hubspot", "object": "contacts", "fields": ["email", "firstname"],
                       "target": "sales.hs_contacts", "secret_scope": "hubspot-keys"})
    assert syncs.run_now(OWNER, "hs-contacts", wait=True)["rows"] == 5
    assert [r["firstname"] for r in trino("SELECT firstname FROM lake.sales.hs_contacts ORDER BY id")] == \
        [f"C{i}" for i in range(5)]
    cursor = syncs.get(OWNER, "hs-contacts")["cursor"]
    assert cursor == str(int(__import__("datetime").datetime(2026, 11, 1, 10, 0, 4).replace(
        tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000))
    # A field added to the sync becomes a column.
    for c in saas.contacts.values():
        c["lifecyclestage"] = "lead"
    syncs.save(OWNER, {"name": "hs-contacts", "kind": "hubspot", "object": "contacts",
                       "fields": ["email", "firstname", "lifecyclestage"], "target": "sales.hs_contacts",
                       "secret_scope": "hubspot-keys"}, "hs-contacts")
    saas.contacts["2"].update(modified="2026-11-02T10:00:00.000Z")
    # The contact at the cursor's own time again (>=), and the changed one.
    assert syncs.run_now(OWNER, "hs-contacts", wait=True)["rows"] == 2
    assert len(trino("SELECT id FROM lake.sales.hs_contacts")) == 5          # upserted: still one row each
    assert trino("SELECT lifecyclestage FROM lake.sales.hs_contacts WHERE id = '102'")[0]["lifecyclestage"] == "lead"
    # A failing API: recorded, audited, told to the owner; the cursor stays.
    saas.fail_hubspot = True
    r = syncs.run_now(OWNER, "hs-contacts", wait=True)
    assert r["status"] == "failed" and "500" in r["error"]
    assert world["told"][-1][0] == "saas.sync_failed" and world["told"][-1][3] == [OWNER]
    assert syncs.get(OWNER, "hs-contacts")["cursor"] is not None
    trino("DROP TABLE lake.sales.hs_contacts")


def test_rights_and_keys_are_checked(world):
    from cairn.pipelines.saas import SaasError

    syncs = world["syncs"]
    with pytest.raises(SaasError) as e:                     # bob may not write the table
        syncs.save(BOB, {"name": "bob-sync", "kind": "salesforce", "object": "Account", "fields": ["Name"],
                         "target": "sales.anything", "secret_scope": "crm-keys"})
    assert e.value.status == 403
    for bad, msg in [({"object": "Account WHERE 1=1"}, "object"), ({"fields": ["Name, Id FROM X"]}, "fields"),
                     ({"target": "Sales.X"}, "target"), ({"kind": "zendesk"}, "kind")]:
        with pytest.raises(SaasError, match=msg):
            syncs.save(OWNER, {"name": "bad", "kind": "salesforce", "object": "Account", "fields": ["Name"],
                               "target": "sales.x", "secret_scope": "crm-keys", **bad})
    # The owner can no longer read the scope: the run fails closed, before any request.
    syncs.save(OWNER, {"name": "no-keys", "kind": "salesforce", "object": "Account", "fields": ["Name"],
                       "target": "sales.no_keys", "secret_scope": "someone-elses"})
    asked = len(world["saas"].asked)
    r = syncs.run_now(OWNER, "no-keys", wait=True)
    assert r["status"] == "failed" and len(world["saas"].asked) == asked
    # Others don't see a sync.
    with pytest.raises(SaasError) as e:
        syncs.get(BOB, "no-keys")
    assert e.value.status == 404


SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"


def test_a_google_sheet_lands_whole_and_follows_the_sheet(world, trino):  # noqa: F811
    """Queue row 27: a Google Sheet's rows land in a governed table, signed in as a service account (a JWT it
    signs) or with an API key; each run replaces the table with the sheet as it is now."""
    saas, syncs = world["saas"], world["syncs"]
    saas.sheets[SHEET_ID] = {"Q4 Targets": [["Region", "Target (EUR)", "Owner", "Notes"],
                                            ["EU", 120000, "ana", "stretch"], ["US", 95000.5, "bob"],
                                            [], ["APAC", 40000, "", "new"]]}
    trino("DROP TABLE IF EXISTS lake.sales.q4_targets")
    syncs.save(OWNER, {"name": "q4-targets", "kind": "google_sheets", "object": f"{SHEET_ID}/Q4 Targets",
                       "fields": ["region", "target_eur", "owner"], "target": "sales.q4_targets",
                       "secret_scope": "sheets-keys", "cron": "*/15 * * * *"})
    first = syncs.run_now(OWNER, "q4-targets", wait=True)
    assert first["status"] == "ok" and first["rows"] == 3, first
    assert saas.google_claims and saas.google_claims[-1]["scope"] == "https://www.googleapis.com/auth/spreadsheets.readonly"
    rows = trino("SELECT id, region, target_eur, owner FROM lake.sales.q4_targets ORDER BY id")
    assert [(r["id"], r["region"], r["target_eur"], r["owner"]) for r in rows] == \
        [("2", "EU", "120000", "ana"), ("3", "US", "95000.5", "bob"), ("5", "APAC", "40000", None)]
    # The sheet changes (a row deleted, one edited): the table follows it, nothing left behind.
    saas.sheets[SHEET_ID]["Q4 Targets"] = [["Region", "Target (EUR)", "Owner"], ["EU", 125000, "ana"]]
    assert syncs.run_now(OWNER, "q4-targets", wait=True)["rows"] == 1
    assert trino("SELECT region, target_eur FROM lake.sales.q4_targets") == [{"region": "EU", "target_eur": "125000"}]
    # Emptied: an empty table, not the old rows.
    saas.sheets[SHEET_ID]["Q4 Targets"] = [["Region", "Target (EUR)", "Owner"]]
    assert syncs.run_now(OWNER, "q4-targets", wait=True)["status"] == "ok"
    assert trino("SELECT count(*) AS n FROM lake.sales.q4_targets") == [{"n": 0}]
    # A public sheet, by API key; a column that is not there is reported, not guessed.
    saas.sheets[SHEET_ID]["Public"] = [["Region", "Target (EUR)"], ["EU", 1]]
    syncs.save(OWNER, {"name": "public-targets", "kind": "google_sheets", "object": f"{SHEET_ID}/Public",
                       "fields": ["region", "owner"], "target": "sales.public_targets", "secret_scope": "sheets-public"})
    bad = syncs.run_now(OWNER, "public-targets", wait=True)
    assert bad["status"] == "failed" and "no column owner" in bad["error"], bad
    # A key the sheet does not know is refused there, and the run says so.
    world["scopes"].put(OWNER, "sheets-public", "api_key", "AIza-not-the-key")
    syncs.save(OWNER, {"name": "public-targets", "kind": "google_sheets", "object": f"{SHEET_ID}/Public",
                       "fields": ["region"], "target": "sales.public_targets", "secret_scope": "sheets-public"},
               name="public-targets")
    denied = syncs.run_now(OWNER, "public-targets", wait=True)
    assert denied["status"] == "failed" and "403" in denied["error"], denied
    from cairn.pipelines.saas import SaasError

    with pytest.raises(SaasError, match="spreadsheet id"):
        syncs.save(OWNER, {"name": "bad-sheet", "kind": "google_sheets", "object": "no-slash", "fields": ["region"],
                           "target": "sales.x", "secret_scope": "sheets-keys"})
