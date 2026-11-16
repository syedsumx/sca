"""Serving tables (cairn.dataaccess.serving) without PostgreSQL or Trino: how a read is decided for the caller. A
SQLite database (attached as the serving schema) stands in for the row store; the end-to-end test against
PostgreSQL, Trino and the real store and catalog is tests/test_serving_tables_e2e.py."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, insert

from cairn.common.db import init_db
from cairn.coreproxy.masking import DatasetPlan
from cairn.dataaccess.serving import HIDDEN, ServingError, ServingTables, _row_hash, serving_tables
from cairn.governance.residency import Residency

URN = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.accounts,PROD)"
BOB = SimpleNamespace(urn="urn:li:corpuser:bob", is_service=False)


class Access:
    """The data preview's decisions, as ServingTables asks for them: BOB may read, with ``plan``'s masks."""

    def __init__(self, plan):
        self.plan, self.row_filters = plan, (lambda *a: [])

    def known(self, urn):
        return True

    def can_read(self, actor, urn):
        return True

    def _policies(self, actor, urns):
        return {}

    def _conditions(self, actor, urn, dialect):
        return None

    def plan_for(self, actor, urn):
        return self.plan


@pytest.fixture()
def make(tmp_path):
    def build(types, pk, rows, plan):
        engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
        init_db(engine)
        store = create_engine(f"sqlite:///{tmp_path}/store.db")

        @event.listens_for(store, "connect")
        def attach(dbapi, _):
            dbapi.execute(f"ATTACH DATABASE '{tmp_path}/serving.db' AS cairn_serving")
        svc = ServingTables(engine, lambda: None, lambda: "", Access(plan), lambda p, priv, urn: True,
                            lambda t: URN)
        svc._write = svc._read = store
        with engine.begin() as conn:
            conn.execute(insert(serving_tables).values(
                name="accounts", source="sales.accounts", source_urn=URN, primary_key=json.dumps(pk), indexes="[]",
                writable=False, enabled=True, created_by="urn:li:corpuser:ana", created_ms=1,
                pg_table="st_accounts_0000abcd", types=json.dumps(types), synced_ms=1, synced_snapshot=1))
        c = svc._get("accounts")
        phys = svc._phys(c)
        cols = [n for n, _ in types]
        with store.begin() as conn:
            phys.create(conn)
            conn.execute(insert(phys), [{**r, HIDDEN: _row_hash(cols, r)} for r in rows])
        return svc
    return build


def test_a_mixed_case_column_is_masked_as_trino_masks_its_lower_case_name(make):
    """Trino (and the data preview) decide masks on lower-case column names; an Iceberg column written in mixed case
    (by Spark or pyiceberg) must not come out raw from the row store."""
    plan = DatasetPlan(fields={"ssn"}, field_strategies={"ssn": "redact"}, strategy="redact")
    svc = make([["id", "long"], ["SSN", "string"], ["region", "string"]], ["id"],
               [{"id": i, "SSN": f"123-45-{i:04d}", "region": "EU"} for i in range(1, 4)], plan)
    out = svc.rows(BOB, "accounts", [], None, None, [], None)
    assert out["masked"] == ["SSN"]
    assert all("123-45" not in str(r["SSN"]) for r in out["rows"]), out["rows"]
    found = svc.lookup(BOB, "accounts", [{"id": 2}])["results"][0]
    assert found["status"] == "found" and "123-45" not in str(found["row"]["SSN"])
    with pytest.raises(ServingError) as e:                  # and it cannot be compared either
        svc.rows(BOB, "accounts", [], None, None, ["SSN:eq:123-45-0001"], None)
    assert e.value.status == 403


def test_rows_with_a_masked_key_are_not_ordered_by_its_raw_values(make):
    emails = [f"{c}@acme.com" for c in "abcdefghij"]
    plan = DatasetPlan(fields={"email"}, field_strategies={"email": "hash"}, strategy="hash")
    svc = make([["email", "string"], ["n", "long"]], ["email"],
               [{"email": e, "n": i} for i, e in enumerate(emails)], plan)
    asc = [r["n"] for r in svc.rows(BOB, "accounts", [], None, None, [], None)["rows"]]
    desc = [r["n"] for r in svc.rows(BOB, "accounts", [], None, None, [], None, order="desc")["rows"]]
    assert sorted(asc) == list(range(10)) and desc == asc[::-1]
    assert asc != list(range(10)), "the order is the masked key's raw order"
    svc.access.plan = DatasetPlan()                          # a reader without the mask: ordered by key
    assert [r["n"] for r in svc.rows(BOB, "accounts", [], None, None, [], None)["rows"]] == list(range(10))


def test_a_pinned_table_is_served_only_where_it_may_be_processed(make):
    svc = make([["id", "long"], ["region", "string"]], ["id"], [{"id": 1, "region": "EU"}], DatasetPlan())
    rules = {"regions": {"eu": {"location": "s3://lake-eu/warehouse"}, "us": {"location": "s3://lake-us/warehouse"}},
             "pins": [{"namespaces": ["sales"], "region": "eu"}]}
    svc.residency = Residency(rules, "eu")
    assert svc.rows(BOB, "accounts", [], None, None, [], None)["rows"] == [{"id": 1, "region": "EU"}]
    svc.residency = Residency(rules, "us")                   # a deployment the pin does not allow
    for read in (lambda: svc.rows(BOB, "accounts", [], None, None, [], None),
                 lambda: svc.lookup(BOB, "accounts", [{"id": 1}]),
                 lambda: svc.sync("accounts"),
                 lambda: svc.create(BOB, {"name": "again", "table": "sales.accounts", "primary_key": ["id"]})):
        with pytest.raises(ServingError) as e:
            read()
        assert e.value.status == 403 and "residency" in str(e.value)
