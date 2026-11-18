"""Moving off Snowflake and Databricks (L170), on a real Trino over Cairn Catalog and Cairn Store: scripts written for
those warehouses are converted, and what is reported as converted is run and gives the answers the originals are
defined to give; what is not convertible is said, with its Cairn counterpart. Side by side: a query is run on a source
database (PostgreSQL here: the comparison is the same whatever the source) and on Cairn, and the answers compared.

There is no Snowflake or Databricks in CI: their side of the comparison is the documented meaning of each statement,
written out as the expected rows. Needs docker and the binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import shutil
import subprocess

import httpx
import pytest

from test_trino_e2e import _ready, cairn, lake, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and shutil.which("docker")), reason="needs docker, the store and the catalog")

SNOWFLAKE_DDL = """
-- An orders table as Snowflake has it.
CREATE OR REPLACE TABLE mig.orders (
    id NUMBER(38,0) AUTOINCREMENT, customer VARCHAR(16777216) NOT NULL, amount NUMBER(10,2), placed TIMESTAMP_NTZ,
    attrs VARIANT, region STRING COMMENT 'sales region'
) CLUSTER BY (region) COMMENT = 'orders; with a semicolon';
CREATE STREAM mig.orders_changes ON TABLE mig.orders;
CREATE TASK nightly WAREHOUSE = wh SCHEDULE = '60 MINUTE' AS DELETE FROM mig.orders WHERE amount < 0;
COPY INTO mig.orders FROM @landing/orders FILE_FORMAT = (TYPE = CSV);
GRANT SELECT ON mig.orders TO ROLE analyst;
"""
ROWS = ("(1, 'ann', 10.50, TIMESTAMP '2026-01-05 10:15:00', '{\"tier\": \"gold\", \"tags\": [\"a\", \"b\"]}', 'EU'), "
        "(2, 'bob', 20.00, TIMESTAMP '2026-01-20 23:59:00', '{\"tier\": \"basic\"}', 'EU'), "
        "(3, 'ann', 5.25, TIMESTAMP '2026-02-01 00:00:00', NULL, 'US'), "
        "(4, 'cy', NULL, TIMESTAMP '2026-02-14 08:00:00', '{\"tier\": \"gold\"}', 'US')")
# (the statement as written for Snowflake, the rows Snowflake's documentation says it returns)
SNOWFLAKE = [
    ("SELECT customer, IFF(amount > 10, 'big', 'small') AS size, ZEROIFNULL(amount) AS amt, NVL(region, '?') AS r "
     "FROM mig.orders ORDER BY id",
     [["ann", "big", "10.50", "EU"], ["bob", "big", "20.00", "EU"], ["ann", "small", "5.25", "US"], ["cy", "small", "0.00", "US"]]),
    ("SELECT region, COUNT(*) AS n, SUM(amount) AS total FROM mig.orders GROUP BY region ORDER BY region",
     [["EU", 2, "30.50"], ["US", 2, "5.25"]]),
    # The latest order of each customer.
    ("SELECT customer, id FROM mig.orders QUALIFY ROW_NUMBER() OVER (PARTITION BY customer ORDER BY placed DESC) = 1 ORDER BY customer",
     [["ann", 3], ["bob", 2], ["cy", 4]]),
    ("SELECT id, attrs:tier::string AS tier FROM mig.orders WHERE attrs:tier::string = 'gold' ORDER BY id",
     [[1, "gold"], [4, "gold"]]),
    ("SELECT TO_VARCHAR(placed, 'YYYY-MM-DD HH24:MI') AS at, DATE_TRUNC('month', placed)::date AS m, "
     "DATEADD(day, 7, placed)::date AS due, DATEDIFF(day, '2026-01-01'::date, placed::date) AS age FROM mig.orders WHERE id = 2",
     [["2026-01-20 23:59", "2026-01-01", "2026-01-27", 19]]),
    ("SELECT LISTAGG(customer, ',') WITHIN GROUP (ORDER BY id) AS names, COUNT(DISTINCT customer) AS people, "
     "MAX(LENGTH(customer)) AS longest FROM mig.orders", [["ann,bob,ann,cy", 3, 3]]),
    ("SELECT customer || '-' || region AS k, UPPER(SUBSTR(customer, 1, 1)) AS initial, DECODE(region, 'EU', 1, 'US', 2, 0) AS code, "
     "COALESCE(amount, 0) > 6 AS big FROM mig.orders WHERE id IN (1, 3) ORDER BY id",
     [["ann-EU", "A", 1, True], ["ann-US", "A", 2, False]]),
    ("WITH per AS (SELECT customer, SUM(amount) AS total FROM mig.orders GROUP BY customer) "
     "SELECT customer, total, RANK() OVER (ORDER BY total DESC NULLS LAST) AS place FROM per ORDER BY place",
     [["bob", "20.00", 1], ["ann", "15.75", 2], ["cy", None, 3]]),
]
DATABRICKS_DDL = """
CREATE TABLE mig.visits (id BIGINT GENERATED ALWAYS AS IDENTITY, person STRING, pages ARRAY<STRING>, seen DATE,
    props MAP<STRING, INT>) USING DELTA PARTITIONED BY (seen)
    TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true') LOCATION 's3://bucket/visits';
OPTIMIZE mig.visits ZORDER BY (person);
"""
VISITS = ("(1, 'ann', ARRAY['home', 'pricing'], DATE '2026-03-01', MAP(ARRAY['clicks'], ARRAY[3])), "
          "(2, 'bob', ARRAY['home'], DATE '2026-03-01', MAP(ARRAY['clicks'], ARRAY[1])), "
          "(3, 'ann', ARRAY['docs', 'home', 'docs'], DATE '2026-03-09', MAP(ARRAY['clicks'], ARRAY[7]))")
# (the statement as written for Databricks, the rows Spark SQL's documentation says it returns)
DATABRICKS = [
    ("SELECT person, size(pages) AS n, array_contains(pages, 'docs') AS docs, props['clicks'] AS clicks FROM mig.visits ORDER BY id",
     [["ann", 2, False, 3], ["bob", 1, False, 1], ["ann", 3, True, 7]]),
    ("SELECT person, page FROM mig.visits LATERAL VIEW explode(pages) p AS page WHERE id = 3 ORDER BY page",
     [["ann", "docs"], ["ann", "docs"], ["ann", "home"]]),
    ("SELECT person, date_add(seen, 7) AS due, datediff(seen, DATE'2026-02-27') AS age, date_format(seen, 'yyyy-MM') AS m, "
     "year(seen) AS y FROM mig.visits WHERE id = 3", [["ann", "2026-03-16", 10, "2026-03", 2026]]),
    ("SELECT person, count(*) AS visits, array_join(array_sort(collect_set(CAST(seen AS STRING))), ',') AS days "
     "FROM mig.visits GROUP BY person ORDER BY person", [["ann", 2, "2026-03-01,2026-03-09"], ["bob", 1, "2026-03-01"]]),
    ("SELECT concat(person, '/', CAST(id AS STRING)) AS k, nvl2(person, 'y', 'n') AS has, try_cast('x' AS INT) AS bad, "
     "regexp_replace(person, 'n+$', 'N') AS r, substring(person, 2, 2) AS mid FROM mig.visits WHERE id = 1",
     [["ann/1", "y", None, "aN", "nn"]]),
]


def run_corpus(trino, source, ddl, values, table, corpus):  # noqa: F811
    from cairn.migrate.convert import convert

    trino("CREATE SCHEMA IF NOT EXISTS lake.mig")
    result = convert(ddl, source)
    create = result["statements"][0]
    assert create["sql"] and create["status"] == "needs_review", create       # the identity column is a person's call
    assert "identity" in {n["code"] for n in create["notes"]}
    trino(f"DROP TABLE IF EXISTS lake.{table}")
    trino(create["sql"])                                                       # the translated DDL makes the table
    trino(f"INSERT INTO lake.{table} VALUES {values}")
    for sql, expected in corpus:
        (st,) = convert(sql, source)["statements"]
        assert st["status"] == "converted", (sql, st["notes"], st["sql"])
        got = [list(r.values()) for r in trino(st["sql"])]
        # Trino's client prints decimals as text and numbers as numbers: compared by what they say.
        norm = lambda rows: [[v if v is None or isinstance(v, (bool, list)) else str(v) for v in r] for r in rows]  # noqa: E731
        assert norm(got) == norm(expected), (sql, st["sql"], got)
    return result


def test_snowflake_scripts_are_converted_and_give_the_same_answers(trino):  # noqa: F811
    result = run_corpus(trino, "snowflake", SNOWFLAKE_DDL, ROWS, "mig.orders", SNOWFLAKE)
    # The table Trino made is what the DDL meant: types, the sort order from CLUSTER BY, the comments.
    made = trino("SHOW CREATE TABLE lake.mig.orders")[0]["Create Table"]
    assert "sorted_by = ARRAY['region" in made and "orders; with a semicolon" in made and "sales region" in made
    cols = {r["Column"]: r["Type"] for r in trino("SHOW COLUMNS FROM lake.mig.orders")}
    assert cols == {"id": "decimal(38,0)", "customer": "varchar", "amount": "decimal(10,2)", "placed": "timestamp(6)",
                    "attrs": "varchar", "region": "varchar"}
    # What has no counterpart is said, each with what does its job in Cairn; nothing is silently dropped.
    rest = {s["kind"]: s for s in result["statements"][1:]}
    assert set(rest) == {"stream", "task", "copy", "grant"} and all(s["status"] == "not_converted" and s["sql"] is None
                                                                  for s in rest.values())
    assert "change feed" in rest["stream"]["notes"][0]["message"] and "pipe" in rest["copy"]["notes"][0]["message"]
    assert result["summary"] == {"statements": 5, "converted": 0, "needs_review": 1, "not_converted": 4, "verified": 0,
                                 "reasons": result["summary"]["reasons"]}
    assert result["summary"]["reasons"]["identity"] == 1


def test_databricks_scripts_are_converted_and_give_the_same_answers(trino):  # noqa: F811
    result = run_corpus(trino, "databricks", DATABRICKS_DDL, VISITS, "mig.visits", DATABRICKS)
    made = trino("SHOW CREATE TABLE lake.mig.visits")[0]["Create Table"]
    assert "partitioning = ARRAY['seen']" in made and "format = 'PARQUET'" in made         # an Iceberg table, partitioned
    assert result["statements"][1]["status"] == "not_converted" and "maintenance" in result["statements"][1]["notes"][0]["message"]
    # Where the two engines differ, the translation says so rather than pass as the same: Spark's sort_array puts
    # NULLs first, Trino's array_sort last.
    from cairn.migrate.convert import convert

    (st,) = convert("SELECT sort_array(pages) FROM mig.visits", "databricks")["statements"]
    assert st["status"] == "needs_review" and "NULLs" in st["notes"][0]["message"]
    # A QUALIFY whose ORDER BY and LIMIT come after the filter keeps them after it.
    (st,) = convert("SELECT person, id FROM mig.visits QUALIFY ROW_NUMBER() OVER (PARTITION BY person ORDER BY id DESC) = 1 "
                    "ORDER BY person DESC LIMIT 1", "databricks")["statements"]
    assert st["status"] == "converted" and [list(r.values()) for r in trino(st["sql"])] == [["bob", 2]], st["sql"]
    (st,) = convert("SELECT person FROM mig.visits QUALIFY ROW_NUMBER() OVER (PARTITION BY person ORDER BY id) = 1 ORDER BY seen",
                    "databricks")["statements"]
    assert st["status"] == "needs_review" and st["notes"][0]["code"] == "qualify_order"


def test_a_translation_trino_refuses_is_never_reported_as_converted(trino, cairn):  # noqa: F811
    """With a connection, Cairn plans each converted query on Trino as the caller: what Trino refuses is marked for
    review with Trino's reason, and what it accepts is marked verified."""
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    admin = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
    body = {"source": "snowflake", "connection": "lakehouse",
            "sql": "SELECT region, COUNT(*) AS n FROM sales.orders GROUP BY region; "
                   "SELECT no_such_column FROM sales.orders; SELECT SYSTEM$TYPEOF(region) FROM sales.orders"}
    r = httpx.post(f"{url}/v1/migrate/convert", json=body, headers=admin, timeout=120)
    assert r.status_code == 200, r.text
    ok, missing, unknown = r.json()["statements"]
    assert ok["status"] == "converted" and ok["verified"] is True
    assert missing["status"] == "needs_review" and "no_such_column" in missing["notes"][-1]["message"]
    assert unknown["status"] == "needs_review" and unknown["verified"] is False
    assert r.json()["summary"]["verified"] == 1
    md = httpx.post(f"{url}/v1/migrate/convert", params={"format": "markdown"}, json=body, headers=admin, timeout=120)
    assert md.headers["content-type"].startswith("text/markdown") and "1 converted (1 checked on Trino), 2 to review" in md.text
    # Someone without MANAGE_INGESTION converts nothing.
    ana = {"X-Cairn-Actor": "urn:li:corpuser:ana"}
    assert httpx.post(f"{url}/v1/migrate/convert", json=body, headers=ana, timeout=30).status_code == 403


def test_answers_are_compared_side_by_side_between_the_source_and_cairn(trino, cairn):  # noqa: F811
    """The same data in a source database (PostgreSQL, reached directly) and in Cairn (through Trino): matching
    queries match, a translation that changes the answer is caught, and a result cut at the limit is not a match."""
    import os

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    pg = subprocess.run(["docker", "port", f"cairn-trino-pg-{os.getpid()}", "5432"], capture_output=True, text=True,
                        check=True).stdout.split(":")[-1].strip()
    s.data_access.registry = ConnectionRegistry({
        "lakehouse": {"url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem",
                      "run_as_user": True, "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}},
        "warehouse": {"url": f"postgresql://postgres:${{CAIRN_TEST_PG_PASSWORD}}@127.0.0.1:{pg}/crm",
                      "catalog": {"platform": "postgres", "database": "crm"}}})
    os.environ["CAIRN_TEST_PG_PASSWORD"] = "crm-secret"
    trino("CREATE SCHEMA IF NOT EXISTS lake.moved")
    trino("CREATE TABLE IF NOT EXISTS lake.moved.customers AS SELECT * FROM crm.sales.customers")       # the data, migrated
    admin = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
    try:
        body = {"source": "snowflake", "source_connection": "warehouse", "connection": "lakehouse", "queries": [
            {"sql": "SELECT region, COUNT(*) AS n FROM sales.customers GROUP BY region",
             "target_sql": "SELECT region, COUNT(*) AS customers FROM moved.customers GROUP BY region"},
            {"sql": "SELECT id, email FROM sales.customers ORDER BY id",
             "target_sql": "SELECT id, email FROM moved.customers ORDER BY id", "ordered": True},
            # A translation that changed the meaning: caught.
            {"sql": "SELECT COUNT(*) AS n FROM sales.customers WHERE region = 'EU'",
             "target_sql": "SELECT COUNT(*) AS n FROM moved.customers WHERE region <> 'EU' OR region = 'EU'"},
            {"sql": "SELECT id FROM sales.customers", "target_sql": "SELECT id FROM moved.no_such_table"},
        ]}
        r = httpx.post(f"{url}/v1/migrate/validate", json=body, headers=admin, timeout=300)
        assert r.status_code == 200, r.text
        same, ordered, changed, failed = r.json()["results"]
        assert same["match"] and same["renamed_columns"] == ["n → customers"] and same["source_rows"] == 2
        assert ordered["match"]
        assert not changed["match"] and "only in the source" in changed["differences"][0]
        assert not failed["match"] and failed["error"].startswith("Cairn:")
        assert r.json()["summary"] == {"queries": 4, "matching": 2, "different": 1, "failed": 1}
        # More rows than the limit: said, and not a match.
        cut = httpx.post(f"{url}/v1/migrate/validate", headers=admin, timeout=300, json={
            **body, "limit": 1, "queries": [body["queries"][1]]}).json()["results"][0]
        assert cut["partial"] and not cut["match"]
        acts = {e["action"] for e in s.audit.entries("migrate", limit=20)}
        assert {"migrate.converted", "migrate.validated"} <= acts
    finally:
        os.environ.pop("CAIRN_TEST_PG_PASSWORD", None)


def test_a_sql_procedure_becomes_a_cairn_procedure_that_runs_as_its_caller(trino, cairn):  # noqa: F811
    """A Snowflake Scripting procedure whose body is statements in order is converted to a Cairn procedure, created
    and called: it does on Trino what the original does. One with logic is said to need a flow; nothing is made."""
    from cairn.migrate.convert import convert
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.sqlworkspace.workspace import WorkspaceError

    _, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    trino("CREATE SCHEMA IF NOT EXISTS lake.mig")
    trino("CREATE TABLE IF NOT EXISTS lake.mig.adj (region varchar, amount double, day date)")
    script = """
    CREATE OR REPLACE PROCEDURE mig.set_adjustment(region VARCHAR, amount NUMBER(10,2), day DATE)
    RETURNS STRING LANGUAGE SQL AS $$
    BEGIN
      -- one adjustment a day for a region; capped
      DELETE FROM mig.adj WHERE region = :region AND day = :day;
      INSERT INTO mig.adj VALUES (:region, IFF(:amount > 100, 100, :amount), :day);
      SELECT region, SUM(amount) AS total FROM mig.adj GROUP BY region ORDER BY region;
      RETURN 'done';
    END;
    $$;
    CREATE PROCEDURE mig.looping(n INT) RETURNS INT LANGUAGE SQL AS $$
    DECLARE total INT DEFAULT 0;
    BEGIN FOR i IN 1 TO n DO total := total + i; END FOR; RETURN total; END;
    $$;
    CREATE PROCEDURE mig.js() RETURNS STRING LANGUAGE JAVASCRIPT AS $$ return 'x'; $$;
    """
    simple, logic, js = convert(script, "snowflake")["statements"]
    assert simple["status"] == "converted" and simple["kind"] == "procedure", simple["notes"]
    assert logic["status"] == "needs_review" and "procedure" not in logic and "flow" in logic["notes"][0]["message"]
    assert js["status"] == "not_converted"
    proc = simple["procedure"]
    assert proc["name"] == "set_adjustment" and proc["params"] == [
        {"name": "region", "type": "varchar"}, {"name": "amount", "type": "double"}, {"name": "day", "type": "date"}]
    assert len(proc["statements"]) == 3                                   # the RETURN is dropped, and said
    assert any(n["code"] == "procedure_return" for n in simple["notes"])

    admin = "urn:li:corpuser:admin"
    s.procedures.create(admin, {**proc, "connection": "lakehouse"})
    try:
        out = s.procedures.call(admin, "set_adjustment", {"region": "EU", "amount": 250, "day": "2026-11-01"})
        assert out["rows"] == [["EU", 100.0]], out                         # capped by the converted IFF
        s.procedures.call(admin, "set_adjustment", {"region": "EU", "amount": 7.5, "day": "2026-11-01"})   # replaced
        out = s.procedures.call(admin, "set_adjustment", {"region": "US", "amount": 3, "day": "2026-11-02"})
        assert out["rows"] == [["EU", 7.5], ["US", 3.0]]
        # As its caller: Ana may not write the table, so the converted procedure does nothing for her.
        with pytest.raises(WorkspaceError):
            s.procedures.call("urn:li:corpuser:ana", "set_adjustment", {"region": "EU", "amount": 1, "day": "2026-11-03"})
        assert trino("SELECT count(*) AS n FROM lake.mig.adj") == [{"n": 2}]
    finally:
        s.procedures.delete(admin, "set_adjustment") if hasattr(s.procedures, "delete") else None

    # Databricks: parameters are bare names there. They become :name, and the report asks a person to check.
    (db,) = convert("CREATE PROCEDURE mig.set_adj(region STRING, amount DOUBLE) LANGUAGE SQL AS BEGIN "
                    "DELETE FROM mig.adj WHERE adj.region = region; INSERT INTO mig.adj VALUES (region, amount, current_date()); END",
                    "databricks")["statements"]
    assert db["status"] == "needs_review" and db["procedure"]["statements"][0] == "DELETE FROM mig.adj WHERE adj.region = :region"
    assert any(n["code"] == "procedure_names" for n in db["notes"])
