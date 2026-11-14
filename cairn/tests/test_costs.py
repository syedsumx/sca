"""Cost per asset: warehouse result rows priced and attributed, daily figures, idle and expensive
tables, and deprecation proposals."""

import pytest

from cairn.reports.costs import CostRow, CostService, CostSource, bigquery_rows, dataset_urn, snowflake_rows

NOW = 1_800_000_000_000          # 2027-01-15
LEDGER = dataset_urn("snowflake", "FIN.PUBLIC.LEDGER")
OLD = dataset_urn("snowflake", "FIN.PUBLIC.OLD_LEDGER")
FEED = dataset_urn("snowflake", "FIN.PUBLIC.FEED")


def test_snowflake_and_bigquery_rows_are_priced():
    src = CostSource(platform="snowflake", connection="x", credit_usd=2.0, storage_usd_per_tb_month=30.0)
    rows = {r.urn: r for r in snowflake_rows(src, "2027-01-15",
                                             [{"name": "FIN.PUBLIC.LEDGER", "bytes": 10**12}],
                                             [{"name": "fin.public.ledger", "credits": 1.5, "reads": 40}])}
    assert LEDGER == "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.public.ledger,PROD)"
    assert rows[LEDGER].storage_usd == 1.0 and rows[LEDGER].compute_usd == 3.0 and rows[LEDGER].reads == 40
    bq = CostSource(platform="bigquery", connection="x", usd_per_tib=5.0, storage_usd_per_gib_month=0.03)
    b = bigquery_rows(bq, "2027-01-15", [{"name": "p.d.t", "bytes": 2**30 * 1000}], [{"name": "p.d.t", "bytes_billed": 2**40, "reads": 3}])
    assert b[0].storage_usd == 1.0 and b[0].compute_usd == 5.0


@pytest.fixture
def svc(engine):
    sent = []
    s = CostService(engine, None, lambda urn: urn == FEED, lambda urn, actor, reason, fig: sent.append((urn, reason, fig)))
    days = [f"2027-01-{d:02d}" for d in range(1, 15)]
    s.ingest([CostRow(urn=LEDGER, day=d, storage_bytes=10**12, storage_usd=1.0, compute_usd=3.0, reads=40) for d in days])
    s.ingest([CostRow(urn=OLD, day=d, storage_bytes=5 * 10**12, storage_usd=5.0, reads=0) for d in days])
    s.ingest([CostRow(urn=FEED, day=d, storage_usd=2.0, reads=0) for d in days])       # feeds a downstream table
    return s, sent


def test_idle_tables_skip_read_and_feeding_tables(svc):
    s, _ = svc
    idle = s.idle(days=30, now_ms=NOW)
    assert [t["urn"] for t in idle] == [OLD]
    assert idle[0]["storage_usd"] == 70.0 and idle[0]["monthly_storage_usd"] == 70.0


def test_expensive_tables_rank_by_cost_per_read(svc):
    s, _ = svc
    ranked = s.expensive(days=30, now_ms=NOW)
    assert [t["urn"] for t in ranked][:2] == [OLD, FEED]          # never read: whole cost per "read"
    ledger = next(t for t in ranked if t["urn"] == LEDGER)
    assert ledger["cost_usd"] == 56.0 and ledger["reads"] == 560 and ledger["cost_per_read_usd"] == 0.1


def test_series_reingest_and_deprecation(svc):
    s, sent = svc
    s.ingest([CostRow(urn=LEDGER, day="2027-01-14", storage_usd=9.0, reads=1)])       # replaces that day
    a = s.asset(LEDGER)
    assert len(a["series"]) == 14 and a["series"][-1]["storage_usd"] == 9.0
    assert a["last_30d"] == {"storage_usd": 22.0, "compute_usd": 39.0, "reads": 521}
    out = s.propose_deprecation(OLD, "urn:li:corpuser:admin", "unread for 30 days")
    assert out["proposed"] and sent == [(OLD, "unread for 30 days", {"storage_usd": 70.0, "compute_usd": 0.0, "reads": 0})]


def test_bigquery_editions_databricks_and_redshift_rows():
    from cairn.reports.costs import databricks_rows, redshift_rows

    ed = CostSource(platform="bigquery", connection="x", pricing="editions", usd_per_slot_hour=0.06)
    [b] = bigquery_rows(ed, "2027-01-15", [], [{"name": "p.d.t", "bytes_billed": 2**40, "slot_ms": 36_000_000, "reads": 2}])
    assert b.compute_usd == 0.6                                      # 10 slot-hours, bytes ignored
    dbx = CostSource(platform="databricks", connection="x")
    [d] = databricks_rows(dbx, "2027-01-15", [{"name": "main.sales.orders", "usd": 12.34567891, "reads": 9}])
    assert d.urn == dataset_urn("databricks", "main.sales.orders") and d.compute_usd == 12.345679 and d.storage_usd == 0
    rs = CostSource(platform="redshift", connection="x", daily_compute_usd=200.0, storage_usd_per_gb_month=0.03)
    rows = {r.urn: r for r in redshift_rows(rs, "2027-01-15", [{"name": "dev.public.orders", "mb": 30 * 1024}],
                                            [{"name": "dev.public.orders", "share": 0.25, "reads": 7}])}
    o = rows[dataset_urn("redshift", "dev.public.orders")]
    assert o.compute_usd == 50.0 and o.storage_usd == 0.03 and o.storage_bytes == 30 * 2**30 and o.reads == 7
    sv = CostSource(platform="redshift", connection="x", serverless=True, usd_per_rpu_hour=0.36)
    [s] = redshift_rows(sv, "2027-01-15", [], [{"name": "dev.public.orders", "share": 0.5, "reads": 1}], rpu_seconds=36_000)
    assert s.compute_usd == 1.8                                      # 10 RPU-hours × 0.36 × half the time


def test_collector_sql_parses_in_its_dialect():
    import sqlglot

    from cairn.reports import costs

    for sql, dialect in ((costs.DATABRICKS_COMPUTE, "databricks"), (costs.REDSHIFT_COMPUTE, "redshift"),
                         (costs.REDSHIFT_STORAGE, "redshift"), (costs.REDSHIFT_SERVERLESS_USAGE, "redshift"),
                         (costs.BIGQUERY_COMPUTE.format(region="region-us"), "bigquery"),
                         (costs.SNOWFLAKE_COMPUTE, "snowflake")):
        assert sqlglot.parse_one(sql, read=dialect) is not None
