"""The Iceberg REST catalog through pyiceberg's RestCatalog, with Cairn Store holding the data and
metadata: namespaces, table lifecycle, schema evolution, time travel, row deletes, staged creates,
optimistic concurrency, multi-table transactions, renames, registration and token scopes."""

import json
import threading
import uuid

import pyarrow as pa
import pytest
import requests
from pyiceberg.exceptions import (CommitFailedException, ForbiddenError, NamespaceAlreadyExistsError, NamespaceNotEmptyError,
                                  NoSuchNamespaceError, NoSuchTableError, OAuthError, TableAlreadyExistsError)
from pyiceberg.expressions import EqualTo
from pyiceberg.types import StringType

from conftest import ADMIN_TOKEN, READER_TOKEN, SALES_TOKEN

ORDERS = pa.table({"order_id": pa.array([1, 2, 3], pa.int64()), "region": ["EMEA", "AMER", "EMEA"],
                   "amount": pa.array([10.0, 20.0, 30.0])})


def rows(n0: int, n: int) -> pa.Table:
    return pa.table({"order_id": pa.array(range(n0, n0 + n), pa.int64()), "region": ["APAC"] * n,
                     "amount": pa.array([1.0] * n)})


def test_config_and_auth(stack):
    cfg = requests.get(f"{stack.uri}/v1/config", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).json()
    assert "POST /v1/{prefix}/transactions/commit" in cfg["endpoints"]
    assert requests.get(f"{stack.uri}/v1/config").status_code == 401
    r = requests.get(f"{stack.uri}/v1/namespaces", headers={"Authorization": "Bearer wrong-token-0000000000"})
    assert r.status_code == 401 and r.json()["error"]["type"] == "NotAuthorizedException"
    with pytest.raises((OAuthError, Exception)):
        stack.catalog(token="wrong-token-0000000000").list_namespaces()


def test_namespaces(cat):
    root = "n_" + uuid.uuid4().hex[:8]
    cat.create_namespace(root, {"owner": "ana"})
    with pytest.raises(NamespaceAlreadyExistsError):
        cat.create_namespace(root)
    cat.create_namespace((root, "eu"))
    assert (root,) in cat.list_namespaces()
    assert cat.list_namespaces(root) == [(root, "eu")]
    assert cat.load_namespace_properties(root) == {"owner": "ana"}
    summary = cat.update_namespace_properties(root, removals={"owner", "nope"}, updates={"tier": "1"})
    assert summary.removed == ["owner"] and summary.missing == ["nope"] and summary.updated == ["tier"]
    with pytest.raises(NamespaceNotEmptyError):
        cat.drop_namespace(root)
    cat.drop_namespace((root, "eu"))
    cat.drop_namespace(root)
    with pytest.raises(NoSuchNamespaceError):
        cat.load_namespace_properties(root)


def test_table_lifecycle_time_travel_evolution_and_deletes(cat, ns):
    table = cat.create_table(f"{ns}.orders", schema=ORDERS.schema, properties={"owner": "sales"})
    assert table.metadata_location.startswith("s3://lakehouse/warehouse/")
    with pytest.raises(TableAlreadyExistsError):
        cat.create_table(f"{ns}.orders", schema=ORDERS.schema)
    table.append(ORDERS)
    first = table.current_snapshot().snapshot_id
    table.append(rows(4, 1))
    table = cat.load_table(f"{ns}.orders")
    assert table.scan().to_arrow().num_rows == 4
    assert table.scan(snapshot_id=first).to_arrow().num_rows == 3            # time travel
    with table.update_schema() as upd:                                         # schema evolution
        upd.add_column("channel", StringType())
    table = cat.load_table(f"{ns}.orders")
    table.append(pa.table({"order_id": pa.array([5], pa.int64()), "region": ["EMEA"], "amount": pa.array([50.0]),
                           "channel": ["web"]}))
    assert cat.load_table(f"{ns}.orders").scan().to_arrow().column("channel").null_count == 4
    table.delete(EqualTo("region", "AMER"))                                    # row-level delete
    left = cat.load_table(f"{ns}.orders").scan().to_arrow()
    assert sorted(left.column("order_id").to_pylist()) == [1, 3, 4, 5]
    assert len(cat.load_table(f"{ns}.orders").history()) >= 4
    assert cat.load_table(f"{ns}.orders").properties["owner"] == "sales"
    assert (ns, "orders") in cat.list_tables(ns)
    assert cat.table_exists(f"{ns}.orders")


def test_stale_writer_gets_commit_failed(stack, cat, ns):
    """Optimistic concurrency: a commit based on an old snapshot is refused with 409 (pyiceberg then
    refreshes and retries by itself, so both appends land)."""
    cat.create_table(f"{ns}.events", schema=ORDERS.schema)
    a, b = cat.load_table(f"{ns}.events"), cat.load_table(f"{ns}.events")
    a.append(rows(0, 2))
    stale = {"requirements": [{"type": "assert-ref-snapshot-id", "ref": "main", "snapshot-id": None}],
             "updates": [{"action": "set-properties", "updates": {"x": "1"}}]}
    r = requests.post(f"{stack.uri}/v1/namespaces/{ns}/tables/events", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                      data=json.dumps(stale))
    assert r.status_code == 409 and r.json()["error"]["type"] == "CommitFailedException"
    b.append(rows(10, 2))                       # b's base is stale: refused, refreshed, retried
    table = cat.load_table(f"{ns}.events")
    assert table.scan().to_arrow().num_rows == 4 and len(table.snapshots()) == 2
    assert "x" not in table.properties


def test_concurrent_appends_never_lose_a_commit(stack, ns):
    """Many writers retrying on conflict: every append lands exactly once."""
    stack.catalog().create_table(f"{ns}.busy", schema=ORDERS.schema)
    errors = []

    def writer(i):
        cat = stack.catalog(name=f"w{i}")
        for _attempt in range(40):
            try:
                cat.load_table(f"{ns}.busy").append(rows(i * 100, 3))
                return
            except CommitFailedException:
                continue
            except Exception as e:  # noqa: BLE001 - reported below
                errors.append(e)
                return
        errors.append(RuntimeError(f"writer {i} gave up"))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    table = stack.catalog().load_table(f"{ns}.busy")
    assert table.scan().to_arrow().num_rows == 18 and len(table.snapshots()) == 6


def test_staged_create_transaction(cat, ns):
    tx = cat.create_table_transaction(f"{ns}.staged", schema=ORDERS.schema)
    assert not cat.table_exists(f"{ns}.staged")                                # not visible until committed
    tx.append(ORDERS)
    tx.commit_transaction()
    assert cat.load_table(f"{ns}.staged").scan().to_arrow().num_rows == 3


def test_rename_drop_and_register(cat, ns):
    t = cat.create_table(f"{ns}.a", schema=ORDERS.schema)
    t.append(ORDERS)
    cat.rename_table(f"{ns}.a", f"{ns}.b")
    with pytest.raises(NoSuchTableError):
        cat.load_table(f"{ns}.a")
    location = cat.load_table(f"{ns}.b").metadata_location
    cat.drop_table(f"{ns}.b")
    assert not cat.table_exists(f"{ns}.b")
    back = cat.register_table(f"{ns}.restored", location)                    # the files outlive the entry
    assert back.scan().to_arrow().num_rows == 3


def test_multi_table_transaction_is_all_or_nothing(stack, cat, ns):
    """POST /v1/transactions/commit: both tables change, or neither does."""
    cat.create_table(f"{ns}.t1", schema=ORDERS.schema)
    cat.create_table(f"{ns}.t2", schema=ORDERS.schema)
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def change(name, uuid_):
        return {"identifier": {"namespace": [ns], "name": name}, "requirements": [{"type": "assert-table-uuid", "uuid": uuid_}],
                "updates": [{"action": "set-properties", "updates": {"batch": "1"}}]}

    u1 = str(cat.load_table(f"{ns}.t1").metadata.table_uuid)
    u2 = str(cat.load_table(f"{ns}.t2").metadata.table_uuid)
    stale = {"table-changes": [change("t1", u1), change("t2", str(uuid.uuid4()))]}
    r = requests.post(f"{stack.uri}/v1/transactions/commit", headers=h, data=json.dumps(stale))
    assert r.status_code == 409 and r.json()["error"]["type"] == "CommitFailedException"
    assert "batch" not in cat.load_table(f"{ns}.t1").properties                # nothing applied
    good = {"table-changes": [change("t1", u1), change("t2", u2)]}
    assert requests.post(f"{stack.uri}/v1/transactions/commit", headers=h, data=json.dumps(good)).status_code == 204
    assert cat.load_table(f"{ns}.t1").properties["batch"] == cat.load_table(f"{ns}.t2").properties["batch"] == "1"


def test_bad_updates_are_rejected(stack, cat, ns):
    cat.create_table(f"{ns}.v", schema=ORDERS.schema)
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    bad = {"requirements": [], "updates": [{"action": "set-current-schema", "schema-id": 42}]}
    r = requests.post(f"{stack.uri}/v1/namespaces/{ns}/tables/v", headers=h, data=json.dumps(bad))
    assert r.status_code == 400 and r.json()["error"]["type"] == "BadRequestException"
    r = requests.post(f"{stack.uri}/v1/namespaces/{ns}/tables/v", headers=h, data=json.dumps({"updates": [{"action": "warp"}]}))
    assert r.status_code == 400


def test_token_scopes(stack):
    admin, sales, reader = stack.catalog(), stack.catalog(SALES_TOKEN, "s"), stack.catalog(READER_TOKEN, "r")
    sales.create_namespace("sales")
    sales.create_table("sales.orders", schema=ORDERS.schema).append(ORDERS)
    with pytest.raises(ForbiddenError):
        sales.create_namespace("hr")
    admin.create_namespace("hr")
    assert ("hr",) not in sales.list_namespaces() and ("sales",) in sales.list_namespaces()
    assert reader.load_table("sales.orders").scan().to_arrow().num_rows == 3
    with pytest.raises(ForbiddenError):
        reader.create_namespace("anything")
