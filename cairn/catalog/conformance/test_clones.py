"""Zero-copy clones, undrop, branches and tags on Cairn Catalog (real store and catalog, pyiceberg).

A clone is a new table on the same data files: no data is copied, new writes go to the clone's own
location, and neither table may expire the snapshots they share (expiring one deletes data files the
other still reads). A dropped table can be brought back for CAIRN_CATALOG_UNDROP_DAYS. Branches and tags
are Iceberg's own (write, audit, publish)."""

import uuid

import httpx
import pyarrow as pa
import pytest

from conftest import ADMIN_TOKEN


def http(stack, method, path, **kw):
    return httpx.request(method, f"{stack.uris[0]}{path}", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=30, **kw)


def rows(t):
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: r["id"])


def table(cat, ns, name="orders"):
    data = pa.table({"id": pa.array([1, 2], pa.int64()), "region": ["EU", "US"]})
    t = cat.create_table(f"{ns}.{name}", schema=data.schema)
    t.append(data)
    t.append(pa.table({"id": pa.array([3], pa.int64()), "region": ["EU"]}))
    return t


def commit(stack, ns, name, updates):
    return http(stack, "POST", f"/v1/namespaces/{ns}/tables/{name}", json={"requirements": [], "updates": updates})


def test_a_clone_shares_the_data_and_protects_the_snapshots_it_shares(stack, cat, ns):
    src = table(cat, ns)
    # Order the snapshots by lineage: the catalog's list is not in commit order.
    first = next(s.snapshot_id for s in src.snapshots() if s.parent_snapshot_id is None)
    current = src.current_snapshot().snapshot_id
    r = http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/clone", json={"name": "orders_copy"})
    assert r.status_code == 200, r.text
    clone = cat.load_table(f"{ns}.orders_copy")
    assert rows(clone) == rows(src) and clone.metadata.table_uuid != src.metadata.table_uuid
    assert clone.metadata.location != src.metadata.location
    # No data was copied: the clone's files are the source's.
    src_files = {f.file.file_path for f in src.scan().plan_files()}
    assert {f.file.file_path for f in clone.scan().plan_files()} == src_files
    # Each changes on its own; the clone's new files go to its own location.
    clone.append(pa.table({"id": pa.array([9], pa.int64()), "region": ["US"]}))
    assert [r["id"] for r in rows(clone)] == [1, 2, 3, 9] and [r["id"] for r in rows(cat.load_table(f"{ns}.orders"))] == [1, 2, 3]
    new_files = {f.file.file_path for f in clone.scan().plan_files()} - src_files
    assert new_files and all(p.startswith(clone.metadata.location) for p in new_files)
    # Neither may expire what they share; the catalog's own properties are not for clients.
    for name in ("orders", "orders_copy"):
        r = commit(stack, ns, name, [{"action": "remove-snapshots", "snapshot-ids": [first]}])
        assert r.status_code == 409 and "shared with a clone" in r.text, (name, r.text)
    r = commit(stack, ns, "orders", [{"action": "set-properties", "updates": {"cairn.clones": "{}"}}])
    assert r.status_code == 403
    r = commit(stack, ns, "orders", [{"action": "remove-properties", "removals": ["cairn.clones"]}])
    assert r.status_code == 403
    # Once the clone is dropped, the source may expire its old snapshots again.
    cat.drop_table(f"{ns}.orders_copy")
    r = commit(stack, ns, "orders", [{"action": "remove-snapshots", "snapshot-ids": [first]}])
    assert r.status_code == 200, r.text
    assert [s.snapshot_id for s in cat.load_table(f"{ns}.orders").snapshots()] == [current]


def test_clones_go_to_other_namespaces_and_names_are_checked(stack, cat, ns):
    table(cat, ns)
    other = "ns_" + uuid.uuid4().hex[:8]
    cat.create_namespace(other)
    r = http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/clone", json={"namespace": [other], "name": "dev_orders"})
    assert r.status_code == 200, r.text
    assert [x["id"] for x in rows(cat.load_table(f"{other}.dev_orders"))] == [1, 2, 3]
    assert http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/clone", json={"namespace": [other], "name": "dev_orders"}).status_code == 409
    assert http(stack, "POST", f"/v1/namespaces/{ns}/tables/missing/clone", json={"name": "x"}).status_code == 404
    assert http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/clone", json={"name": "a/b"}).status_code == 400


def test_a_dropped_table_comes_back_a_purged_one_does_not(stack, cat, ns):
    table(cat, ns)
    cat.drop_table(f"{ns}.orders")
    listed = http(stack, "GET", f"/v1/namespaces/{ns}/dropped").json()
    assert [d["name"] for d in listed["dropped"]] == ["orders"] and listed["retention-days"] == 7
    assert "cairn.dropped.orders" not in cat.load_namespace_properties(ns)            # the catalog's own
    r = http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/undrop")
    assert r.status_code == 200, r.text
    assert [x["id"] for x in rows(cat.load_table(f"{ns}.orders"))] == [1, 2, 3]
    assert http(stack, "GET", f"/v1/namespaces/{ns}/dropped").json()["dropped"] == []
    assert http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/undrop").status_code == 404   # not dropped now
    cat.purge_table(f"{ns}.orders")
    assert http(stack, "GET", f"/v1/namespaces/{ns}/dropped").json()["dropped"] == []
    assert http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/undrop").status_code == 404
    r = http(stack, "POST", f"/v1/namespaces/{ns}/properties", json={"updates": {"cairn.dropped.x": "{}"}, "removals": []})
    assert r.status_code == 403


def test_branches_and_tags_write_audit_publish(stack, cat, ns):
    t = table(cat, ns)
    v1 = t.current_snapshot().snapshot_id
    t.manage_snapshots().create_tag(v1, "release-1").create_branch(v1, "audit").commit()
    t = cat.load_table(f"{ns}.orders")
    assert set(t.refs()) == {"main", "release-1", "audit"}
    # Write to the branch: main does not see it until it is published.
    t.append(pa.table({"id": pa.array([4], pa.int64()), "region": ["US"]}), branch="audit")
    t = cat.load_table(f"{ns}.orders")
    assert [r["id"] for r in rows(t)] == [1, 2, 3]
    audit = t.refs()["audit"].snapshot_id
    assert sorted(r["id"] for r in t.scan(snapshot_id=audit).to_arrow().to_pylist()) == [1, 2, 3, 4]
    t.manage_snapshots().set_current_snapshot(audit).commit()                       # publish
    t = cat.load_table(f"{ns}.orders")
    assert [r["id"] for r in rows(t)] == [1, 2, 3, 4]
    assert sorted(r["id"] for r in t.scan(snapshot_id=t.snapshot_by_name("release-1").snapshot_id).to_arrow().to_pylist()) == [1, 2, 3]


def test_a_clone_comes_back_only_while_its_source_holds_what_they_share(stack, cat, ns):
    src = table(cat, ns)
    first = next(s.snapshot_id for s in src.snapshots() if s.parent_snapshot_id is None)
    assert http(stack, "POST", f"/v1/namespaces/{ns}/tables/orders/clone", json={"name": "c1"}).status_code == 200
    # Dropped and brought back: the source holds the shared snapshots again.
    cat.drop_table(f"{ns}.c1")
    assert http(stack, "POST", f"/v1/namespaces/{ns}/tables/c1/undrop").status_code == 200
    r = commit(stack, ns, "orders", [{"action": "remove-snapshots", "snapshot-ids": [first]}])
    assert r.status_code == 409, r.text
    # Dropped, then the source expires what they shared: the clone's files may be gone, so it stays dropped.
    cat.drop_table(f"{ns}.c1")
    assert commit(stack, ns, "orders", [{"action": "remove-snapshots", "snapshot-ids": [first]}]).status_code == 200
    r = http(stack, "POST", f"/v1/namespaces/{ns}/tables/c1/undrop")
    assert r.status_code == 409 and "no longer holds" in r.text, r.text
    assert "c1" not in [t[-1] for t in cat.list_tables(ns)]
