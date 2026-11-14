"""Consent management (L66): withdrawn consent takes a person's rows out of the purposes that need it.

Real services and real warehouses: SQLite always, PostgreSQL when CAIRN_TEST_PG_URL is set (CI), each through a
direct connection, where Cairn rewrites the query; Trino is covered in test_trino_e2e.py. Also the fix that came
with it: policy row filters now apply on direct connections too, not only through Trino."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cairn.app import create_api_app
from cairn.governance import consent as consent_mod
from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.governance.consent import PurposeIn, RecordsIn, subject_hash
from cairn.quality.collectors import ConnectionRegistry
from cairn.services import build_services

ADMIN, ANA, BOB, DPO = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:dpo"
SYNC = "urn:li:corpuser:consent-platform"
MARKETING = "urn:li:corpGroup:marketing"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
PEOPLE = [(1, "kim@acme.com", "EU"), (2, "Lee@Acme.com ", "US"), (3, "max@acme.com", "EU"), (4, None, "US")]


def _load(url: str, schema: str) -> None:
    e = create_engine(url)
    with e.begin() as c:
        if e.dialect.name == "postgresql":
            c.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            c.execute(text(f"CREATE SCHEMA {schema}"))
        c.execute(text(f"CREATE TABLE {schema}.users (id INTEGER, email TEXT, region TEXT)"))
        c.execute(text(f"CREATE TABLE {schema}.orders (id INTEGER, user_id INTEGER)"))
        for p in PEOPLE:
            c.execute(text(f"INSERT INTO {schema}.users VALUES (:i, :e, :r)"), {"i": p[0], "e": p[1], "r": p[2]})
        for i, u in enumerate([1, 1, 2, 3, 4]):
            c.execute(text(f"INSERT INTO {schema}.orders VALUES (:i, :u)"), {"i": i, "u": u})
    e.dispose()


def _stack(settings, graph, url: str, platform: str, schema: str, database: str = ""):
    for t, fields in (("users", [{"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}},
                                 {"fieldPath": "region"}]),
                      ("orders", [{"fieldPath": "id"}, {"fieldPath": "user_id"}])):
        name = f"{database}.{schema}.{t}" if database else f"{schema}.{t}"
        graph.aspects[(f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},PROD)", "schemaMetadata")] = {"fields": fields}
    graph.aspects[(ANA, "groupMembership")] = {"groups": [MARKETING]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    catalog = {"platform": platform, **({"database": database} if database else {})}
    s.data_access.registry = ConnectionRegistry({"wh": {"url": url, "catalog": catalog}})
    users = f"urn:li:dataset:(urn:li:dataPlatform:{platform},{database + '.' if database else ''}{schema}.users,PROD)"
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "in", "value": [ADMIN, DPO]}),
        Policy(id="read", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="consent-platform", effect="allow", actions=["MANAGE_CONSENT"],
               condition={"attr": "subject.urn", "op": "eq", "value": SYNC}),
        Policy(id="bob-eu", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=[users]),
               obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})],
               condition={"attr": "subject.urn", "op": "eq", "value": BOB}),
    ]))
    s.consent.ttl = 0                                     # every query sees the latest records
    return s


def ids(s, actor, sql):
    return [r[0] for r in s.data_access.query(actor, sql, connection="wh")["rows"]]


def _check(s, schema: str):
    cs = s.consent
    cs.put_purpose(DPO, PurposeIn(name="Marketing", identifier="email", members={"groups": [MARKETING]}))
    # A second purpose of the same name never takes over the first (its members, its records): 409.
    from cairn.governance.consent import ConsentError

    with pytest.raises(ConsentError) as dup:
        cs.put_purpose(DPO, PurposeIn(name="Marketing", identifier="email", mode="opt_in", members={"users": [ADMIN]}))
    assert dup.value.status == 409
    cs.record(SYNC, "marketing", RecordsIn(subjects=[{"value": " KIM@acme.com", "state": "withdrawn"},
                                                     {"value": "lee@acme.com", "state": "granted"}], source="crm"))
    q = f"SELECT id FROM {schema}.users ORDER BY id"
    # Ana works in marketing: kim withdrew, so kim's row is not hers to use; the row without an email stays.
    assert ids(s, ANA, q) == [2, 3, 4]
    assert ids(s, ADMIN, q) == [1, 2, 3, 4]                # not a member: every row
    # Through a join and an alias; the orders table links to nobody by itself, so only users is filtered.
    joined = f"SELECT u.id, count(o.id) AS n FROM {schema}.users u JOIN {schema}.orders o ON o.user_id = u.id " \
             "GROUP BY u.id ORDER BY u.id"
    assert ids(s, ANA, joined) == [2, 3, 4]
    assert len(s.data_access.query(ANA, f"SELECT id FROM {schema}.orders", connection="wh")["rows"]) == 5
    assert sorted(r[0] for r in s.data_access.preview(ANA, s.data_access.sources()[0].urn(schema, "users"))["rows"]) \
        == [2, 3, 4]
    # Consent given again: back in.
    cs.record(SYNC, "marketing", RecordsIn(subjects=[{"hash": subject_hash("kim@acme.com"), "state": "granted"}]))
    assert ids(s, ANA, q) == [1, 2, 3, 4]

    # Opt-in: only the people who said yes.
    cs.put_purpose(DPO, PurposeIn(name="Research", identifier="email", mode="opt_in", members={"users": [ANA]}))
    assert ids(s, ANA, q) == []
    cs.record(DPO, "research", RecordsIn(subjects=[{"value": "max@acme.com", "state": "granted"},
                                                   {"value": "lee@acme.com", "state": "granted"}]))
    assert ids(s, ANA, q) == [2, 3]
    cs.delete_purpose(DPO, "research")

    # Policy row filters now apply on direct connections as well (they used to apply through Trino only).
    assert ids(s, BOB, q) == [1, 3]


def test_consent_on_sqlite(settings, graph, tmp_path):
    url = f"sqlite:///{tmp_path}/wh.db"
    _load(url, "main")
    _check(_stack(settings, graph, url, "sqlite", "main"), "main")


@pytest.mark.skipif(not os.environ.get("CAIRN_TEST_PG_URL"), reason="CAIRN_TEST_PG_URL (a PostgreSQL to use) not set")
def test_consent_on_postgres(settings, graph):
    url = os.environ["CAIRN_TEST_PG_URL"]
    schema = f"consent_{uuid.uuid4().hex[:8]}"
    _load(url, schema)
    try:
        db = create_engine(url).url.database
        _check(_stack(settings, graph, url, "postgres", schema, db), schema)
    finally:
        e = create_engine(url)
        with e.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        e.dispose()


def test_fails_closed_and_the_api(settings, graph, tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/wh.db"
    _load(url, "main")
    s = _stack(settings, graph, url, "sqlite", "main")
    c = TestClient(create_api_app(s))
    as_ = lambda who: {"X-Cairn-Actor": who}    # noqa: E731
    body = {"name": "Marketing", "identifier": "email", "members": {"groups": [MARKETING]}}
    assert c.post("/governance/consent/purposes", json=body, headers=as_(ANA)).status_code == 403
    assert c.post("/governance/consent/purposes", json={**body, "identifier": "shoe_size"}, headers=as_(DPO)).status_code == 422
    assert c.post("/governance/consent/purposes", json=body, headers=as_(DPO)).status_code == 201
    rec = {"subjects": [{"value": "kim@acme.com", "state": "withdrawn"}, {"value": "max@acme.com", "state": "withdrawn"}],
           "source": "crm"}
    assert c.post("/governance/consent/purposes/marketing/records", json=rec, headers=as_(ANA)).status_code == 403
    r = c.post("/governance/consent/purposes/marketing/records", json=rec, headers=as_(SYNC))
    assert r.status_code == 200 and r.json()["records"] == {"granted": 0, "withdrawn": 2}
    bad = c.post("/governance/consent/purposes/marketing/records", json={"subjects": [{"hash": "nothex", "state": "granted"}]},
                 headers=as_(SYNC))
    assert bad.status_code == 422
    st = c.post("/governance/consent/purposes/marketing/check", json={"value": "KIM@acme.com"}, headers=as_(SYNC)).json()
    assert (st["state"], st["included"]) == ("withdrawn", False)
    assert c.get("/governance/consent/mine", headers=as_(ANA)).json()[0]["id"] == "marketing"
    assert c.get("/governance/consent/mine", headers=as_(BOB)).json() == []
    q = "SELECT id FROM main.users ORDER BY id"
    assert c.post("/sql/run", json={"sql": q, "connection": "wh"}, headers=as_(ANA)).json()["rows"] == [[2], [4]]

    # Identifiers are never stored or audited: only hashes and counts.
    with s.engine.connect() as conn:
        stored = [tuple(r) for r in conn.execute(text("SELECT * FROM cairn_consents"))]
    assert stored and all("kim" not in str(r) and "max" not in str(r) for r in stored)
    with s.engine.connect() as conn:
        audits = " ".join(str(tuple(r)) for r in conn.execute(text("SELECT * FROM cairn_audit_log")))
    assert "consent.recorded" in audits and "kim@acme.com" not in audits

    # More records than a filter can carry: rows are withheld, and the purpose says so.
    monkeypatch.setattr(consent_mod, "MAX_INLINE", 1)
    assert ids(s, ANA, q) == []
    assert s.consent.purpose("marketing")["filter_state"].startswith("over_limit")
    monkeypatch.setattr(consent_mod, "MAX_INLINE", 10_000)
    # An engine with no hash function Cairn knows: nothing passes.
    assert s.consent.filters(ANA, s.data_access.sources()[0].urn("main", "users"), "oracle") == ["1 = 0"]
    # A broken lookup on a direct connection: nothing passes either.
    monkeypatch.setattr(s.consent, "resource", lambda urn: (_ for _ in ()).throw(RuntimeError("catalog down")))
    assert ids(s, ANA, q) == []

    # Through Trino's access control (the OPA protocol), the same purpose gives Trino a row filter.
    if s.trino_access is not None:
        monkeypatch.undo()
        s.consent.ttl = 0
        f = s.trino_access.consent(ANA, s.data_access.sources()[0].urn("main", "users"), "trino", [])
        assert len(f) == 1 and "to_hex(sha256(to_utf8(lower(trim(CAST(\"email\" AS varchar))))))" in f[0]
