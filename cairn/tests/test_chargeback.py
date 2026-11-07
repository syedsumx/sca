"""Chargeback statements: every cost of a month charged to cost centres (SQL by who ran it, AI by who asked, tables by
their owners, runs charged to no table), what cannot be placed shown as unallocated with the reason, the statement
adding up to the month's total, frozen when issued, and each cost centre seeing only its own lines. On SQLite and,
with CAIRN_TEST_PG_URL, PostgreSQL."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, insert

from cairn.ai.usage import usage_table
from cairn.common.audit import AuditLog
from cairn.common.core import InMemoryGraph
from cairn.common.db import init_db
from cairn.dataaccess.query_history import query_history
from cairn.reports.chargeback import Chargeback, ChargebackError
from cairn.reports.costs import asset_costs, run_costs

FIN, MKT = "urn:li:corpGroup:finance", "urn:li:corpGroup:marketing"
ANA, BOB, EVE = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:eve"
LEDGER = "urn:li:dataset:(urn:li:dataPlatform:iceberg,fin.ledger,PROD)"
CAMPAIGNS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,mkt.campaigns,PROD)"
ORPHAN = "urn:li:dataset:(urn:li:dataPlatform:iceberg,tmp.scratch,PROD)"
GROUPS = {ANA: [FIN], BOB: [FIN, MKT], EVE: []}


def ms(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _check(engine, monkeypatch):
    init_db(engine)
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_CPU_HOUR", "2.0")
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_TB_SCANNED", "5.0")
    graph = InMemoryGraph()
    graph.aspects[(LEDGER, "ownership")] = {"owners": [{"owner": FIN, "type": "DATAOWNER"}]}
    graph.aspects[(CAMPAIGNS, "ownership")] = {"owners": [{"owner": BOB, "type": "DATAOWNER"}]}   # a person: their teams
    graph.aspects[("urn:li:warehouse:bi", "ownership")] = {"owners": [{"owner": MKT}]}
    cb = Chargeback(engine, lambda u: GROUPS.get(u, []), graph, AuditLog(engine))
    cb.put_centre(ANA, "CC-4100", "Finance", [FIN])
    cb.put_centre(ANA, "CC-5200", "Marketing", [MKT])
    with pytest.raises(ChargebackError) as e:
        cb.put_centre(ANA, "CC-9", "Dup", [FIN])                       # a team pays from one cost centre
    assert e.value.status == 409
    with engine.begin() as c:
        def query(qid, user, day, cpu_hours, tb):
            c.execute(insert(query_history).values(
                query_id=qid, user=user, state="FINISHED", create_ms=ms(day), end_ms=ms(day) + 1000,
                cpu_ms=int(cpu_hours * 3_600_000), input_bytes=int(tb * 1e12), tables="[]", sql="SELECT 1"))
        query("q1", "ana", "2026-10-03", 1.0, 0.0)          # $2.00, finance
        query("q2", "bob", "2026-10-04", 2.0, 0.2)          # $5.00, half finance, half marketing
        query("q3", "eve", "2026-10-05", 0.5, 0.0)          # $1.00, no cost centre: unallocated
        query("q4", "ana", "2026-11-01", 9.0, 0.0)          # November: not in October's statement
        for actor, usd in ((ANA, 0.30), (EVE, 0.20)):
            c.execute(insert(usage_table).values(ts_ms=ms("2026-10-10"), actor=actor, task="assistant", provider="anthropic",
                                                 model="m", input_tokens=1, output_tokens=1, cost_usd=usd))
        for urn, storage, compute in ((LEDGER, 3.0, 1.0), (CAMPAIGNS, 2.0, 0.0), (ORPHAN, 0.5, 0.0)):
            c.execute(insert(asset_costs).values(urn=urn, day="2026-10-15", platform="iceberg", storage_usd=storage,
                                                 compute_usd=compute, reads=0, source="api"))
        c.execute(insert(run_costs).values(run="r1", parent=None, job="urn:li:warehouse:bi", engine="trino",
                                           day="2026-10-20", cores=4, seconds=3600, usd=4.0, outputs="[]"))
        c.execute(insert(run_costs).values(run="r2", parent=None, job="urn:li:job:spark-x", engine="spark",
                                           day="2026-10-21", cores=4, seconds=3600, usd=9.0, outputs=json.dumps([LEDGER])))

    pre = cb.compute("2026-10")
    totals = {c["cost_centre"]: c["usd"] for c in pre["cost_centres"]}
    # Finance: SQL 2 + 2.5, AI 0.3, ledger 3 + 1, campaigns (Bob's teams) 1. Marketing: SQL 2.5, campaigns 1, runs 4.
    assert totals == {"CC-4100": 9.8, "CC-5200": 7.5, "unallocated": 1.7}, pre
    assert pre["total_usd"] == 19.0 == round(sum(totals.values()), 2)       # everything, once (r2 is in the table's figures)
    lines = {(ln["cost_centre"], ln["source"]): ln for ln in pre["lines"]}
    assert lines[("CC-4100", "sql")]["cpu_hours"] == 2.0 and lines[("CC-5200", "runs")]["usd"] == 4.0
    assert any("eve" in r for r in pre["unallocated_reasons"]["sql"]) and any("scratch" in r for r in pre["unallocated_reasons"]["storage"])

    # Issued only once the month is over; frozen; a re-issue is a new version.
    with pytest.raises(ChargebackError) as e:
        cb.issue(ANA, "2026-10", now_ms=ms("2026-10-31", 23))
    assert e.value.status == 409
    v1 = cb.issue(ANA, "2026-10", now_ms=ms("2026-11-02"))
    with engine.begin() as c:                                   # a late figure arrives
        c.execute(insert(asset_costs).values(urn=LEDGER, day="2026-10-30", platform="iceberg", storage_usd=10.0,
                                             compute_usd=0.0, reads=0, source="api"))
    assert cb.statement(v1["id"], operator=True)["total_usd"] == "19.00"   # unchanged
    v2 = cb.issue(ANA, "2026-10", now_ms=ms("2026-11-03"))
    assert (v2["version"], v2["total_usd"]) == (2, "29.00")
    assert [(s["month"], s["version"]) for s in cb.statements()] == [("2026-10", 2), ("2026-10", 1)]
    # A cost centre's members see their own lines only; someone in none sees nothing.
    mine = cb.statement(v1["id"], viewer=ANA)
    assert {ln["cost_centre"] for ln in mine["lines"]} == {"CC-4100"} and mine["total_usd"] == "9.80" and mine["partial"]
    assert "unallocated_reasons" not in mine
    with pytest.raises(ChargebackError):
        cb.statement(v1["id"], viewer=EVE)
    csv_text = Chargeback.to_csv(cb.statement(v1["id"], operator=True))
    assert csv_text.splitlines()[0].startswith("month,version,cost_centre,source,usd")
    assert "2026-10,1,unallocated,sql,1.0000" in csv_text
    assert {"chargeback.issued", "chargeback.centre_saved"} <= {e["action"] for e in AuditLog(engine).entries(category="chargeback")}
    for bad in ("2026-13", "26-10", ""):
        with pytest.raises(ChargebackError):
            cb.compute(bad)


def test_chargeback_on_sqlite(tmp_path, monkeypatch):
    _check(create_engine(f"sqlite:///{tmp_path}/cb.db"), monkeypatch)


def test_chargeback_on_postgres(monkeypatch):
    url = os.environ.get("CAIRN_TEST_PG_URL")
    if not url:
        pytest.skip("CAIRN_TEST_PG_URL (a PostgreSQL to use) not set")
    from sqlalchemy import text

    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    _check(engine, monkeypatch)
