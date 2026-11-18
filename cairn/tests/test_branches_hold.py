"""Write-audit-publish under a legal hold: publishing a branch that removes or rewrites rows, and removing a
branch or tag, wait for the hold to be released; a branch that only appends still publishes."""

from __future__ import annotations

import types

import pytest

from cairn.pipelines.branches import BranchError, Branches


def _table(ops: list[str]):
    """main at snapshot 1; the branch "etl" adds snapshots 2.. with the given operations."""
    snaps = {1: types.SimpleNamespace(parent_snapshot_id=None, summary=types.SimpleNamespace(operation="append"))}
    for i, op in enumerate(ops, start=2):
        snaps[i] = types.SimpleNamespace(parent_snapshot_id=i - 1, summary=types.SimpleNamespace(operation=op))
    ref = lambda sid: types.SimpleNamespace(snapshot_id=sid, snapshot_ref_type="branch")  # noqa: E731
    published = []

    class MS:
        def set_current_snapshot(self, snapshot_id):
            published.append(snapshot_id)
            md.refs["main"] = ref(snapshot_id)
            return self

        def remove_branch(self, name):
            md.refs.pop(name)
            return self

        def commit(self):
            pass
    md = types.SimpleNamespace(refs={"main": ref(1), "etl": ref(len(snaps))}, snapshot_by_id=snaps.get)
    return types.SimpleNamespace(metadata=md, manage_snapshots=MS), published


def _branches(tbl, held):
    b = Branches({}, lambda *a: True, lambda ns, t: f"urn:{ns}.{t}", lambda *a: ([], [], False), "lake")
    b._table = lambda ns, t: tbl
    b.held = held
    return b


@pytest.mark.parametrize("op", ["delete", "overwrite"])
def test_a_branch_that_removes_rows_is_not_published_over_a_hold(op):
    tbl, published = _table(["append", op])
    with pytest.raises(BranchError, match="legal hold") as e:
        _branches(tbl, lambda t: t == "wap.orders").publish("u", "wap.orders", "etl")
    assert e.value.status == 409 and not published
    with pytest.raises(BranchError, match="legal hold"):
        _branches(tbl, lambda t: (_ for _ in ()).throw(RuntimeError("db down"))).publish("u", "wap.orders", "etl")
    assert _branches(tbl, lambda t: False).publish("u", "wap.orders", "etl")["published"]


def test_an_append_only_branch_publishes_and_refs_stay_while_held():
    tbl, published = _table(["append", "append"])
    assert _branches(tbl, lambda t: True).publish("u", "wap.orders", "etl")["published"] and published == [3]
    with pytest.raises(BranchError, match="legal hold"):
        _branches(tbl, lambda t: True).drop_ref("u", "wap.orders", "etl")
    assert _branches(tbl, lambda t: False).drop_ref("u", "wap.orders", "etl") == {"removed": "etl"}
