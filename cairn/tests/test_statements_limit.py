"""Statements read page by page keep their own time limit (CAIRN_QUERY_MAX_RUN_TIME): Cairn cancels a statement in
Trino once it has run longer, rather than sending query_max_run_time, which Trino does not cap at the cluster's limit
and which therefore needs the system privilege (tests/test_trino_access.py). Trino itself is in
tests/test_cli_e2e.py."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from cairn.sqlworkspace.statements import StatementError, Statements, duration_s


def test_durations_read_as_trino_writes_them():
    assert duration_s("1h") == 3600 and duration_s("30m") == 1800 and duration_s("1.5h") == 5400
    assert duration_s("90s") == 90 and duration_s("2d") == 172800 and duration_s("") == 0 and duration_s("0") == 0
    with pytest.raises(ValueError, match="CAIRN_QUERY_MAX_RUN_TIME"):
        duration_s("an hour")


class _Http:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def delete(self, url, headers):
        self.calls.append(("DELETE", url))
        return SimpleNamespace(status_code=204)

    def get(self, url, headers):
        self.calls.append(("GET", url))
        return SimpleNamespace(status_code=200, json=lambda: {"id": "q1", "nextUri": url, "data": [[1]]})


def test_a_statement_past_its_limit_is_cancelled_in_trino_and_no_session_limit_is_sent():
    audits, calls = [], []
    data = SimpleNamespace(_audit=lambda *a: audits.append(a), _mask=lambda *a: [])
    tokens = SimpleNamespace(mint=lambda who, ttl: {"token": "t"})
    s = Statements(data, tokens, "k" * 32, max_run_time="30m")
    s._endpoint = lambda c, w, a: ("http://trino:8080", True, {})
    s._client = lambda verify, c, a: _Http(calls)
    assert "X-Trino-Session" not in s._headers("urn:li:corpuser:ana", {})
    state = {"a": "urn:li:corpuser:ana", "r": "urn:li:corpuser:ana", "b": "http://trino:8080", "c": "trino",
             "s": {}, "u": [], "src": [], "id": "q1", "n": "http://trino:8080/v1/statement/q1/2",
             "e": int(time.time()) + 600, "t0": int(time.time()) - 60}
    assert s.next("urn:li:corpuser:ana", s._seal(state), 0)["rows"] == [[1]]    # within its limit: read on
    state["t0"] = int(time.time()) - 31 * 60
    with pytest.raises(StatementError, match="ran longer than 30m") as e:
        s.next("urn:li:corpuser:ana", s._seal(state), 0)
    assert e.value.status == 409 and calls[-1] == ("DELETE", "http://trino:8080/v1/statement/q1/2")
    assert audits[-1][1] == "data.query_cancelled" and audits[-1][3]["reason"] == "max_run_time"
