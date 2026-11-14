"""Privacy operations: one person's data found through classified columns and counted through data
access; deletions handed to owners and tracked; retention checked against the data; the record of
processing; the identifier never stored in clear."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.privacy import RequestIn, RetentionPolicy, mask
from cairn.quality.collectors import ConnectionRegistry
from cairn.services import build_services

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
LAKE = "urn:li:dataset:(urn:li:dataPlatform:s3,lake/events,PROD)"
DAY = 86_400_000


def schema(*cols, labelled=()):
    return {"fields": [{"fieldPath": c, **({"glossaryTerms": {"terms": [{"urn": EMAIL}]}} if c in labelled else {})}
                       for c in cols]}


@pytest.fixture
def stack(settings, graph, tmp_path):
    url = f"sqlite:///{tmp_path}/wh.db"
    with create_engine(url).begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT, created_at TEXT)"))
        c.execute(text("CREATE TABLE orders (id INTEGER, buyer_email TEXT, created_at TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'jane@acme.com', '2020-01-01'), (2, 'bo@acme.com', '2026-01-01')"))
        c.execute(text("INSERT INTO orders VALUES (1, 'jane@acme.com', '2026-10-01'), (2, 'jane@acme.com', '2026-10-02')"))
    graph.aspects[(USERS, "schemaMetadata")] = schema("id", "email", "created_at", labelled=("email",))
    graph.aspects[(USERS, "ownership")] = {"owners": [{"owner": ANA}]}
    graph.aspects[(USERS, "domains")] = {"domains": ["urn:li:domain:crm"]}
    graph.aspects[(ORDERS, "schemaMetadata")] = schema("id", "buyer_email", "created_at", labelled=("buyer_email",))
    graph.aspects[(ORDERS, "ownership")] = {"owners": [{"owner": BOB}]}
    graph.aspects[(LAKE, "schemaMetadata")] = schema("user_email", "ts", labelled=("user_email",))
    graph.aspects[(LAKE, "ownership")] = {"owners": [{"owner": BOB}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    s.data_access.registry = ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}})
    return s


def test_access_request_finds_and_counts_without_keeping_the_identifier(stack):
    s = stack
    out = s.privacy.open(RequestIn(kind="access", identifier_kind="email", identifier="Jane@acme.com",
                                   reference="DSAR-17"), ADMIN)
    assert out["identifier"] == "Jane@acme.com" and out["state"] == "COMPLETED"
    by = {f["dataset"]: f for f in out["findings"]}
    assert by[ORDERS]["rows"] == 2 and by[USERS]["rows"] == 1, by
    assert by[ORDERS]["columns"] == ["buyer_email"]
    assert by[LAKE]["searched"] is False and "connection" in by[LAKE]["reason"]
    stored = s.privacy.get(out["id"])
    assert stored["identifier"] == "J•••@acme.com" and "jane" not in json.dumps(stored).lower()
    assert [r["id"] for r in s.privacy.find("email", "jane@ACME.com ")] == [out["id"]]    # found again by hash
    csv = s.privacy.report_csv(out["id"])
    assert "buyer_email" in csv and "jane" not in csv.lower()


def test_deletion_goes_to_owners_and_completes(stack):
    s = stack
    out = s.privacy.open(RequestIn(kind="deletion", identifier_kind="email", identifier="jane@acme.com"), ADMIN)
    parts = {p["dataset"]: p for p in out["parts"]}
    assert set(parts) == {USERS, ORDERS, LAKE}                         # matches, plus the one Cairn could not search
    assert "DELETE FROM main.orders WHERE buyer_email = '<j•••@acme.com>'" in parts[ORDERS]["statement"]
    told = [n for n in s.notify.inbox(BOB) if n["type"] == "privacy.deletion_requested"]
    assert len(told) == 2 and "jane" not in json.dumps(told).lower()
    assert {p["dataset"] for p in s.privacy.my_parts(BOB)} == {ORDERS, LAKE}
    with pytest.raises(ValueError):
        s.privacy.mark(out["id"], ORDERS, "DONE", ANA)                   # not ana's dataset
    s.privacy.mark(out["id"], ORDERS, "DONE", BOB)
    s.privacy.mark(out["id"], LAKE, "NOT_APPLICABLE", BOB, "no rows for this person in the lake")
    assert s.privacy.get(out["id"])["state"] == "OPEN"
    assert s.privacy.remind(out["due_ms"] - 3 * DAY) == 1               # ana is reminded once
    assert s.privacy.remind(out["due_ms"] - 2 * DAY) == 0
    done = s.privacy.mark(out["id"], USERS, "DONE", ANA)
    assert done["state"] == "COMPLETED"
    assert [n for n in s.notify.inbox(ADMIN) if n["type"] == "privacy.request_completed"]


def test_retention_and_record_of_processing(stack):
    s = stack
    s.privacy.put_policy(RetentionPolicy(id="pii-2y", label="Classification.PII*", days=730), ADMIN)
    now = int(time.mktime(time.strptime("2026-10-30", "%Y-%m-%d")) * 1000)
    assert s.privacy.check_retention(now_ms=now)["skipped"] is True        # the notifier needs an identity
    stats = s.privacy.check_retention(ADMIN, now_ms=now)
    assert stats == {"checked": 2, "breaches": 1, "unchecked": 1}
    report = {r["dataset"]: r for r in s.privacy.retention_report()}
    assert report[USERS]["state"] == "breach" and "limit is 730" in report[USERS]["detail"]
    assert report[ORDERS]["state"] == "ok" and report[LAKE]["state"] == "unchecked"
    s.privacy.check_retention(ADMIN, now_ms=now)
    assert len([n for n in s.notify.inbox(ANA) if n["type"] == "privacy.retention_breach"]) == 1   # told once
    ropa = s.privacy.ropa()
    crm = next(d for d in ropa["domains"] if d["domain"] == "urn:li:domain:crm")
    assert crm["datasets"][0]["dataset"] == USERS and crm["datasets"][0]["retention"] == ["pii-2y (730 days)"]
    assert "PII.Email" in crm["datasets"][0]["personal_data"]
    assert "urn:li:domain:crm" in s.privacy.ropa_csv()


def test_api(stack):
    c = TestClient(create_api_app(stack))
    body = {"kind": "deletion", "identifier_kind": "email", "identifier": "jane@acme.com"}
    assert c.post("/governance/privacy/requests", json=body, headers={"X-Cairn-Actor": ANA}).status_code == 403
    assert c.post("/governance/privacy/requests", json={**body, "identifier_kind": "passport"},
                  headers={"X-Cairn-Actor": ADMIN}).status_code == 422
    r = c.post("/governance/privacy/requests", json=body, headers={"X-Cairn-Actor": ADMIN})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    mine = c.get("/governance/privacy/my-parts", headers={"X-Cairn-Actor": ANA}).json()
    assert [p["dataset"] for p in mine] == [USERS]
    assert c.post(f"/governance/privacy/requests/{rid}/parts", json={"dataset": USERS, "state": "DONE"},
                  headers={"X-Cairn-Actor": ANA}).json()["parts"][0]["state"] in ("DONE", "PENDING")
    assert c.get(f"/governance/privacy/requests/{rid}", params={"format": "csv"},
                 headers={"X-Cairn-Actor": ADMIN}).headers["content-type"].startswith("text/csv")
    assert c.put("/governance/privacy/retention/pii", json={"id": "pii", "label": "Classification.PII*", "days": 365},
                 headers={"X-Cairn-Actor": ADMIN}).status_code == 200
    assert c.get("/governance/privacy/ropa", headers={"X-Cairn-Actor": ADMIN}).json()["domains"]
    assert mask("jane@acme.com") == "j•••@acme.com" and mask("4155551234") == "41••••••34"
