"""Reverse ETL (L62) on real Trino: a governed segment table synced out to HubSpot and Salesforce as its owner.

The table is read on Trino as the sync's owner, so their row filters decide which rows leave; a column masked for
them is never sent (the sync is refused before anything is read); only new and changed rows go out; records the
app rejects are tried again. The apps are a local server answering the documented batch-upsert endpoints
(HubSpot CRM v3 ``batch/upsert``, Salesforce composite ``sobjects`` upsert by external id): a test of the
connector's requests and bookkeeping, not a substitute for a run against a real account."""

from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_trino_e2e import EMAIL, _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, the store and catalog binaries")
ADMIN, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:bob"
URN = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.segment,PROD)"


class Apps:
    def __init__(self):
        self.hubspot: dict[str, dict] = {}            # by email (the key property)
        self.salesforce: dict[str, dict] = {}         # by external id
        self.calls: list[tuple[str, int]] = []
        self.reject: set[str] = set()                 # HubSpot keys to reject once
        self.token = "pat-na1-" + "r" * 24


def server(apps: Apps):
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

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

        def do_POST(self):
            if self.headers.get("Authorization") != f"Bearer {apps.token}":
                return self._send(401, {"message": "no"})
            m = re.fullmatch(r"/crm/v3/objects/(\w+)/batch/upsert", self.path)
            if not m:
                return self._send(404, {})
            inputs = self._body()["inputs"]
            assert len(inputs) <= 100
            apps.calls.append(("hubspot", len(inputs)))
            bad = [i["id"] for i in inputs if i["id"] in apps.reject]
            for i in inputs:
                assert i["idProperty"] == "email"
                if i["id"] not in apps.reject:
                    apps.hubspot[i["id"]] = i["properties"]
            apps.reject -= set(bad)
            if bad:
                return self._send(207, {"status": "COMPLETE", "results": [],
                                        "errors": [{"category": "VALIDATION_ERROR", "message": "Property invalid",
                                                    "context": {"ids": bad}}]})
            return self._send(200, {"status": "COMPLETE", "results": [{"id": i["id"]} for i in inputs]})

        def do_PATCH(self):
            if self.headers.get("Authorization") != f"Bearer {apps.token}":
                return self._send(401, [{"errorCode": "INVALID_SESSION_ID"}])
            m = re.fullmatch(r"/services/data/v61\.0/composite/sobjects/(\w+)/(\w+)", self.path)
            if not m:
                return self._send(404, {})
            body = self._body()
            assert body["allOrNone"] is False and len(body["records"]) <= 200
            apps.calls.append(("salesforce", len(body["records"])))
            out = []
            for r in body["records"]:
                assert r["attributes"]["type"] == m.group(1)
                apps.salesforce[r[m.group(2)]] = {k: v for k, v in r.items() if k not in ("attributes", m.group(2))}
                out.append({"id": "003" + r[m.group(2)], "success": True, "errors": [], "created": True})
            return self._send(200, out)
    return H


@pytest.fixture
def world(cairn, monkeypatch, tmp_path, trino):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.identity.secret_scopes import SecretScopes
    from cairn.pipelines.reverse_etl import ReverseSyncs
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    monkeypatch.setenv("CAIRN_SAAS_ALLOW_HTTP", "1")
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"r" * 32).decode())
    apps = Apps()
    p = port()
    httpd = ThreadingHTTPServer(("127.0.0.1", p), server(apps))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]
    # The segment: email is PII (masked for everyone but the admin); bob's row filter shows him EU only.
    s.graph.aspects[(URN, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}},
        {"fieldPath": "region"}, {"fieldPath": "score"}]}
    trino("DROP TABLE IF EXISTS lake.sales.segment")
    trino("CREATE TABLE lake.sales.segment (id varchar, email varchar, region varchar, score integer)")
    trino("INSERT INTO lake.sales.segment VALUES ('1', 'kim@acme.com', 'EU', 90), ('2', 'lee@acme.com', 'US', 70), "
          "('3', 'max@acme.com', 'EU', 50)")
    engine = create_engine(f"sqlite:///{tmp_path}/rev.db")
    init_db(engine)
    scopes = SecretScopes(engine, lambda a: [], lambda a: False)
    for who in (ADMIN, BOB):
        name = "keys-" + who.rsplit(":", 1)[1]
        scopes.create(who, {"name": name})
        for k, v in {"access_token": apps.token, "base_url": f"http://127.0.0.1:{p}",
                     "instance_url": f"http://127.0.0.1:{p}"}.items():
            scopes.put(who, name, k, v)
    exporters = {ADMIN, BOB}
    audited, told = [], []

    class Audit:
        def record(self, *a):
            audited.append(a)
    rs = ReverseSyncs(engine, lambda owner, sql, limit: s.data_access.run_on_trino(definition, owner, sql, limit),
                      s.data_access.plan_for, lambda owner, urn: owner in exporters, scopes.read,
                      lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)", "lake", Audit(),
                      lambda *a: told.append(a))
    yield {"apps": apps, "rs": rs, "exporters": exporters, "audited": audited, "told": told}
    httpd.shutdown()
    trino("DROP TABLE IF EXISTS lake.sales.segment")


def hubspot_sync(owner, mapping, key=None):
    return {"name": "seg-" + owner.rsplit(":", 1)[1], "source": "sales.segment", "kind": "hubspot", "object": "contacts",
            "key": key or {"column": "email", "field": "email"}, "mapping": mapping,
            "secret_scope": "keys-" + owner.rsplit(":", 1)[1]}


def test_a_segment_syncs_to_a_crm_as_its_owner_and_only_changes_go(world, trino):  # noqa: F811
    apps, rs = world["apps"], world["rs"]
    rs.save(ADMIN, hubspot_sync(ADMIN, {"region": "country", "score": "lead_score"}))
    first = rs.run_now(ADMIN, "seg-admin", wait=True)
    assert first == {"sync": "seg-admin", "status": "ok", "sent": 3, "failed": 0, "error": None}
    assert apps.hubspot == {"kim@acme.com": {"country": "EU", "lead_score": "90"},
                            "lee@acme.com": {"country": "US", "lead_score": "70"},
                            "max@acme.com": {"country": "EU", "lead_score": "50"}}
    # Nothing changed: nothing sent.
    calls = len(apps.calls)
    assert rs.run_now(ADMIN, "seg-admin", wait=True)["sent"] == 0 and len(apps.calls) == calls
    # One score changes and one row is added: just those two go.
    trino("UPDATE lake.sales.segment SET score = 95 WHERE id = '1'")
    trino("INSERT INTO lake.sales.segment VALUES ('4', 'ola@acme.com', 'US', 10)")
    apps.reject = {"ola@acme.com"}                       # the app rejects one record once
    r = rs.run_now(ADMIN, "seg-admin", wait=True)
    assert (r["status"], r["sent"], r["failed"]) == ("partial", 1, 1) and "Property invalid" in r["error"]
    assert apps.calls[-1] == ("hubspot", 2) and apps.hubspot["kim@acme.com"]["lead_score"] == "95"
    assert world["told"][-1][0] == "saas.sync_failed"
    # The rejected record is tried again, alone.
    assert rs.run_now(ADMIN, "seg-admin", wait=True)["sent"] == 1 and apps.calls[-1] == ("hubspot", 1)
    assert "ola@acme.com" in apps.hubspot
    assert "kim@acme.com" not in json.dumps(world["audited"], default=str)        # values are never audited


def test_masked_columns_are_never_sent_and_row_filters_decide_the_rows(world):
    apps, rs = world["apps"], world["rs"]
    from cairn.pipelines.saas import SaasError

    # Email is masked for bob: a sync keyed on it, or sending it, is refused before anything is read.
    for body in (hubspot_sync(BOB, {"region": "country"}), hubspot_sync(BOB, {"email": "email", "region": "country"},
                                                                         key={"column": "id", "field": "cairn_id"})):
        with pytest.raises(SaasError, match="masked") as e:
            rs.save(BOB, body)
        assert e.value.status == 403
    # Without email, by id: only his EU rows leave.
    rs.save(BOB, hubspot_sync(BOB, {"region": "country", "score": "lead_score"}, key={"column": "id", "field": "email"}))
    r = rs.run_now(BOB, "seg-bob", wait=True)
    assert r["sent"] == 2 and set(apps.hubspot) == {"1", "3"}
    assert all(v["country"] == "EU" for v in apps.hubspot.values())
    # Losing EXPORT_DATASET stops the next run before any request.
    world["exporters"].discard(BOB)
    calls = len(apps.calls)
    r = rs.run_now(BOB, "seg-bob", wait=True)
    assert r["status"] == "failed" and "EXPORT_DATASET" in r["error"] and len(apps.calls) == calls
    with pytest.raises(SaasError) as e:
        rs.get(ADMIN, "seg-bob")                          # another person's sync does not exist for them
    assert e.value.status == 404


def test_salesforce_upsert_by_external_id(world):
    apps, rs = world["apps"], world["rs"]
    rs.save(ADMIN, {"name": "seg-sf", "source": "sales.segment", "kind": "salesforce", "object": "Contact",
                    "key": {"column": "id", "field": "Cairn_Id__c"},
                    "mapping": {"email": "Email", "score": "Lead_Score__c"}, "secret_scope": "keys-admin"})
    r = rs.run_now(ADMIN, "seg-sf", wait=True)
    assert r["status"] == "ok" and r["sent"] == 3 and apps.calls[-1] == ("salesforce", 3)
    assert apps.salesforce["2"] == {"Email": "lee@acme.com", "Lead_Score__c": "70"}
