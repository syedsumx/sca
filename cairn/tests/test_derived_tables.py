"""Derived tables (cairn.pipelines.derived) without Trino: the statements each mode runs, who may see and
run which, one run at a time, and what the query reads. The real refreshes are in tests/test_trino_e2e.py."""

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.common.security import Principal
from cairn.pipelines.derived import DerivedError, DerivedTables, build_derived_router, tables_read

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"


@pytest.fixture()
def dt(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    ran = []

    def run(owner, sql):
        ran.append((owner, sql))
        if "information_schema.tables" in sql:
            return [[1 if any("CREATE OR REPLACE" in s for _, s in ran[:-1]) else 0]]
        if "information_schema.columns" in sql:
            return [["id"], ["amount"], ["seq"]]
        return []
    d = DerivedTables(engine, run, "lake")
    d.ran = ran
    return d


def test_what_each_mode_runs(dt):
    a = dt.create({"name": "a", "target": "s.a", "mode": "append", "watermark": "seq", "every_minutes": 5,
                   "sql": "SELECT * FROM lake.s.src"}, ANA, ANA)
    assert dt.refresh(a["id"])["kind"] == "create"
    assert [s for o, s in dt.ran if o == ANA][-1] == 'CREATE OR REPLACE TABLE "lake"."s"."a" AS SELECT * FROM lake.s.src'
    assert dt.refresh(a["id"])["kind"] == "append"
    assert dt.ran[-1][1] == ('INSERT INTO "lake"."s"."a" SELECT * FROM (SELECT * FROM lake.s.src) AS src WHERE '
                             '(SELECT max("seq") FROM "lake"."s"."a") IS NULL OR src."seq" > (SELECT max("seq") FROM "lake"."s"."a")')
    m = dt.create({"name": "m", "target": "s.m", "mode": "merge", "key": "id", "every_minutes": 5,
                   "sql": "SELECT id, amount, seq FROM lake.s.src"}, ANA, ANA)
    assert dt.refresh(m["id"])["kind"] == "merge"
    assert dt.ran[-1][1] == ('MERGE INTO "lake"."s"."m" AS t USING (SELECT * FROM (SELECT id, amount, seq FROM lake.s.src) AS src) '
                             'AS s ON t."id" = s."id" WHEN MATCHED THEN UPDATE SET "amount" = s."amount", "seq" = s."seq" '
                             'WHEN NOT MATCHED THEN INSERT ("id", "amount", "seq") VALUES (s."id", s."amount", s."seq")')
    assert all(o == ANA for o, _ in dt.ran)              # every statement as the owner


def test_one_run_at_a_time(dt):
    d = dt.create({"name": "a", "target": "s.a", "every_minutes": 5, "sql": "SELECT 1 AS one"}, ANA, ANA)
    from cairn.pipelines.derived import derived_tables
    with dt.engine.begin() as conn:                       # as if another process were refreshing it
        conn.execute(derived_tables.update().values(lease_until=2**62))
    with pytest.raises(DerivedError) as e:
        dt.refresh(d["id"])
    assert e.value.status == 409
    assert dt.run_due() == 0
    with dt.engine.begin() as conn:
        conn.execute(derived_tables.update().values(lease_until=0))
    assert dt.run_due() == 1 and dt.run_due() == 0        # due every 5 minutes, then not


def test_what_the_query_reads():
    assert tables_read("WITH x AS (SELECT * FROM lake.s.a) SELECT * FROM x JOIN s.b ON true JOIN other.s.c ON true "
                       "WHERE id IN (SELECT id FROM lake.t.d)", "lake") == [("s", "a"), ("s", "b"), ("t", "d")]
    for bad in ["DELETE FROM lake.s.a", "SELECT 1; SELECT 2", "SELEC 1 FROM"]:
        with pytest.raises(DerivedError):
            tables_read(bad, "lake")


def test_owners_see_and_run_their_own(dt):
    def who(request: Request):
        return Principal(request.headers["x-user"], "token")

    def authorize(p, privilege, urn):
        if p.urn != ADMIN:
            raise HTTPException(403, "no")
    app = FastAPI()
    app.include_router(build_derived_router(dt, who, authorize))
    c = TestClient(app)
    body = {"name": "mine", "target": "s.mine", "every_minutes": 5, "sql": "SELECT 1 AS one"}
    r = c.post("/v1/derived", json=body, headers={"x-user": ANA})
    assert r.status_code == 201 and r.json()["owner"] == ANA
    did = r.json()["id"]
    assert c.post("/v1/derived", json={**body, "name": "x", "target": "s.x", "owner": BOB},
                  headers={"x-user": ANA}).status_code == 403            # not as someone else
    assert c.post("/v1/derived", json={**body, "name": "y", "target": "s.y", "owner": BOB},
                  headers={"x-user": ADMIN}).status_code == 201          # operators may
    assert [d["name"] for d in c.get("/v1/derived", headers={"x-user": ANA}).json()] == ["mine"]
    assert {d["name"] for d in c.get("/v1/derived", headers={"x-user": ADMIN}).json()} == {"mine", "y"}
    assert c.get(f"/v1/derived/{did}", headers={"x-user": BOB}).status_code == 404
    assert c.post(f"/v1/derived/{did}/refresh", headers={"x-user": BOB}).status_code == 404
    assert c.post(f"/v1/derived/{did}/refresh", json={"full": True}, headers={"x-user": ANA}).json()["status"] == "ok"
    assert len(c.get(f"/v1/derived/{did}/runs", headers={"x-user": ANA}).json()) == 1
    assert c.post("/v1/derived", json={**body, "sql": "DROP TABLE x"}, headers={"x-user": ANA}).status_code == 400
    assert c.delete(f"/v1/derived/{did}", headers={"x-user": BOB}).status_code == 404
    assert c.delete(f"/v1/derived/{did}", headers={"x-user": ANA}).status_code == 204
