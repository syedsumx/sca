"""Trino usage statistics (cairn.dataaccess.trino_usage) under the conditions several API replicas and a
real Core produce: two queries counted at the same moment, a bucket changed while it is being written,
a bucket Core never takes, and events with names longer than the database keeps."""

import copy
import json
import os

import pytest
from sqlalchemy import event

from cairn.common.core import InMemoryGraph
from cairn.dataaccess.trino_usage import TrinoUsage

HERE = os.path.dirname(__file__)
NATION = "urn:li:dataset:(urn:li:dataPlatform:trino,tpch.tiny.nation,PROD)"


def urn_of(cat, sch, tab):
    return f"urn:li:dataset:(urn:li:dataPlatform:trino,{cat}.{sch}.{tab},PROD)"


def completed(qid="q-1", user="ana", inputs=None):
    with open(os.path.join(HERE, "data", "trino_query_completed.json")) as f:
        e = json.load(f)
    e = copy.deepcopy(e)
    e["metadata"]["queryId"], e["context"]["user"] = qid, user
    if inputs is not None:
        e["ioMetadata"]["inputs"] = inputs
    return e


def nation(*cols, table="nation"):
    return {"catalogName": "tpch", "schema": "tiny", "table": table, "columns": [{"name": c} for c in cols]}


@pytest.fixture
def graph():
    return InMemoryGraph()


@pytest.fixture
def usage(engine, graph):
    return TrinoUsage(engine, graph, urn_of)


def latest(graph, urn=NATION):
    return graph.get_timeseries(urn, "datasetUsageStatistics", 1)[0]


def another_replica_inserts_first(engine, table: str, values: dict):
    """Simulates a second replica whose transaction adds the same ``table`` row just after this one found no
    row to update (the moment two replicas count the first read of a bucket together)."""
    done = []

    @event.listens_for(engine, "after_cursor_execute")
    def race(conn, cursor, statement, params, context, executemany):
        s = statement.lstrip().upper()
        hit = (s.startswith(f"UPDATE {table.upper()} ") and cursor.rowcount == 0) or \
              (table == "cairn_trino_usage_pending" and s.startswith(f"DELETE FROM {table.upper()} "))
        if hit and not done:
            done.append(1)
            cols = ", ".join(f'"{k}"' for k in values)
            conn.connection.dbapi_connection.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in values)})", list(values.values()))

    return race


def test_two_replicas_counting_the_same_bucket_both_count(engine, usage, graph):
    day = usage.reads(completed())[2]
    # The other replica's query (bob's) is counted at the same moment as ana's.
    another_replica_inserts_first(engine, "cairn_trino_usage",
                                  {"day_ms": day, "urn": NATION, "user": "urn:li:corpuser:ana", "field": "", "count": 1})
    r = usage.record(completed("q-ana"))
    assert not r.get("duplicate"), r
    assert latest(graph)["totalSqlQueries"] == 2


def test_two_replicas_marking_the_same_bucket_waiting_both_count(engine, usage, graph):
    day = usage.reads(completed())[2]
    another_replica_inserts_first(engine, "cairn_trino_usage_pending", {"day_ms": day, "urn": NATION})
    r = usage.record(completed("q-ana"))
    assert not r.get("duplicate"), r
    assert latest(graph)["totalSqlQueries"] == 1


def test_a_read_counted_while_its_bucket_is_written_is_written_too(engine, graph):
    first = TrinoUsage(engine, graph, urn_of)
    other_core = InMemoryGraph()
    other_core.emit = lambda _m: (_ for _ in ()).throw(OSError("down"))      # the other replica cannot reach Core
    other = TrinoUsage(engine, other_core, urn_of)
    real, calls = graph.emit, []

    def emit(mcps):
        real(mcps)
        if not calls:                                  # after reading the bucket, before marking it written
            calls.append(1)
            other.record(completed("q-bob", "bob", [nation("name")]))
    graph.emit = emit
    first.record(completed("q-ana", "ana", [nation("name")]))
    assert latest(graph)["totalSqlQueries"] == 1
    first.flush()                                      # the retry loop
    assert latest(graph)["totalSqlQueries"] == 2


