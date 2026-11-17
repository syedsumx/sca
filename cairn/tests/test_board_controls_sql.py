"""A control or cross-filter on a tile whose saved query ends in a ``--`` comment still runs (it used to comment out
the wrapper's closing parenthesis)."""
from __future__ import annotations

import sqlglot

from cairn.sqlworkspace.boards import Boards


class _Workspace:
    def __init__(self):
        self.ran: list[str] = []

    def get(self, actor, qid, through_dashboard=False):
        return {"sql": "SELECT region, amount FROM sales -- the sales tile", "connection": "trino", "chart": None}

    def run(self, actor, sql, connection, limit, cache=True):
        sqlglot.parse_one(sql, read="trino")    # raises if the comment swallowed the ")"
        self.ran.append(sql)
        return {"columns": ["region", "amount"], "rows": []}


def test_a_filter_on_a_query_ending_in_a_comment_keeps_the_wrapper_intact():
    b = object.__new__(Boards)
    b.ws = _Workspace()
    out = b._run_query("ann", {"query": "q1"}, [{"column": "region", "op": "=", "value": "EU"}])
    assert out["applied"] and "WHERE" in b.ws.ran[-1]
    assert sqlglot.parse_one(b.ws.ran[-1], read="trino").find(sqlglot.exp.Where) is not None
