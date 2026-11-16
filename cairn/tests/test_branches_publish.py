"""Write-audit-publish: a publish that races a write to main is refused as a conflict (409) and leaves main where the
other writer put it. On pyiceberg's SQL catalog (the same commit requirements the REST catalog checks); the
end-to-end test on Trino and Cairn Catalog is in tests/test_trino_e2e.py."""

from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")
pytest.importorskip("pyiceberg")

from cairn.pipelines.branches import BranchError, Branches  # noqa: E402

ACTOR = "urn:li:corpuser:ana"


@pytest.fixture()
def catalog(tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog

    cat = SqlCatalog("t", uri=f"sqlite:///{tmp_path}/cat.db", warehouse=f"file://{tmp_path}/wh")
    cat.create_namespace("sales")
    t = cat.create_table(("sales", "orders"), schema=pa.schema([pa.field("id", pa.int64())]))
    t.append(pa.table({"id": pa.array([1], pa.int64())}))
    return cat


def test_a_publish_racing_a_write_to_main_is_a_conflict(catalog):
    def run_sql(actor, sql, limit):            # the audit check runs while someone else writes to main
        catalog.load_table(("sales", "orders")).append(pa.table({"id": pa.array([99], pa.int64())}))
        return ["id"], [], False

    b = Branches({}, lambda a, p, u: True, lambda ns, t: f"urn:{ns}.{t}", run_sql, "lakehouse")
    b._catalog = lambda: catalog
    b.create_ref(ACTOR, "sales.orders", "staging")
    catalog.load_table(("sales", "orders")).append(pa.table({"id": pa.array([2], pa.int64())}), branch="staging")
    with pytest.raises(BranchError) as e:
        b.publish(ACTOR, "sales.orders", "staging", ["SELECT * FROM {table} WHERE id < 0"])
    assert e.value.status == 409
    main = catalog.load_table(("sales", "orders")).scan().to_arrow().column("id").to_pylist()
    assert sorted(main) == [1, 99]                                     # the other writer's row; not the branch


def test_a_publish_with_main_left_alone_fast_forwards_it(catalog):
    audit = []
    b = Branches({}, lambda a, p, u: True, lambda ns, t: f"urn:{ns}.{t}", lambda a, s, n: (["id"], [], False),
                 "lakehouse", type("A", (), {"record": lambda self, *a: audit.append(a[1])})())
    b._catalog = lambda: catalog
    b.create_ref(ACTOR, "sales.orders", "staging")
    catalog.load_table(("sales", "orders")).append(pa.table({"id": pa.array([2], pa.int64())}), branch="staging")
    assert b.publish(ACTOR, "sales.orders", "staging", ["SELECT * FROM {table} WHERE id < 0"])["published"]
    assert sorted(catalog.load_table(("sales", "orders")).scan().to_arrow().column("id").to_pylist()) == [1, 2]
    assert audit[-1] == "table.published"
