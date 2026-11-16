"""Input-table approvals (L42) hold for every way of writing: a dashboard action (L36) that adds or changes a row of
a reviewed input table makes a change request, as the API does, instead of writing at once."""

from cairn.sqlworkspace.actions import Actions


class _Approvals:
    def __init__(self, reviewers):
        self._reviewers, self.requests = reviewers, []

    def reviewers(self, tid):
        return self._reviewers

    def write(self, actor, tid, kind, payload):
        self.requests.append((actor, tid, kind, payload))
        return 202, {"pending": "c1", "state": "pending"}


class _Inputs:
    def __init__(self, reviewers):
        self.approvals, self.writes = _Approvals(reviewers), []

    def add(self, actor, tid, rows, dry_run=False):
        self.writes.append(("add", actor, tid, rows))
        return {"added": len(rows)}

    def change(self, actor, tid, row_id, values, expected, dry_run=False):
        self.writes.append(("change", actor, tid, row_id, values))
        return {"changed": 1}


ADD = {"input_table": "t1", "table": "ops.targets", "constants": {"region": "EU"}, "fields": []}
SET = {"input_table": "t1", "table": "ops.targets", "set": {"status": "approved"}, "fields": []}
ROW = {"_row_id": "r1", "_updated_at": "2026-11-15 10:00:00"}


def test_actions_on_a_reviewed_input_table_wait_for_a_reviewer():
    it = _Inputs(["urn:li:corpuser:rev"])
    a = Actions(None, None, it)
    out = a._run("urn:li:corpuser:ana", "input_row", ADD, {}, None)
    assert out["pending"] == "c1" and "review" in out["detail"]
    out = a._run("urn:li:corpuser:ana", "input_update", SET, {}, ROW)
    assert out["pending"] == "c1"
    assert it.writes == []
    assert [r[2] for r in it.approvals.requests] == ["add", "change"]
    assert it.approvals.requests[1][3] == {"row_id": "r1", "values": {"status": "approved"},
                                           "expected_updated_at": ROW["_updated_at"]}


def test_actions_on_an_unreviewed_input_table_write_at_once():
    it = _Inputs([])
    a = Actions(None, None, it)
    assert a._run("urn:li:corpuser:ana", "input_row", ADD, {}, None)["rows"] == 1
    a._run("urn:li:corpuser:ana", "input_update", SET, {}, ROW)
    assert [w[0] for w in it.writes] == ["add", "change"] and it.approvals.requests == []
