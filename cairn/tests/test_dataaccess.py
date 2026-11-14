"""Table previews and read-only SQL: per-table authorization, masking by column lineage, limits,
and an audit trail."""

import pytest
from sqlalchemy import create_engine, text

from cairn.coreproxy.masking import DatasetPlan
from cairn.dataaccess.service import DataAccessError, DataAccessService, output_sources, tables_in
from cairn.quality.collectors import ConnectionRegistry

USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"


class Audit:
    def __init__(self):
        self.events = []

    def record(self, category, action, actor, subject, details):
        self.events.append((action, actor, subject, details))


@pytest.fixture
def svc(tmp_path):
    url = f"sqlite:///{tmp_path}/wh.db"
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT, region TEXT)"))
        c.execute(text("CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL)"))
        for i in range(150):
            c.execute(text("INSERT INTO users VALUES (:i, :e, :r)"), {"i": i, "e": f"user{i}@example.com", "r": "EU"})
        c.execute(text("INSERT INTO orders VALUES (1, 1, 9.5), (2, 1, 3.0), (3, 2, 7.0)"))
    reg = ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}})
    plans = {USERS: DatasetPlan(fields={"email"}, strategy="partial")}
    denied = {("urn:li:corpuser:eve", ORDERS)}
    audit = Audit()
    s = DataAccessService(reg, lambda actor, urn: plans.get(urn, DatasetPlan()),
                          lambda actor, urn: (actor, urn) not in denied, audit, known=lambda urn: urn in (USERS, ORDERS))
    return s, audit


def test_preview_masks_labelled_columns_and_limits_rows(svc):
    s, audit = svc
    out = s.preview("urn:li:corpuser:ana", USERS)
    assert out["columns"] == ["id", "email", "region"] and len(out["rows"]) == 100 and out["truncated"]
    assert out["masked_columns"] == ["email"] and out["rows"][1][1] == "u•••@•••.com" and out["rows"][1][2] == "EU"
    assert audit.events[-1][0] == "data.previewed"
    with pytest.raises(DataAccessError, match="may not see"):          # not catalogued: never served
        s.preview("urn:li:corpuser:ana", "urn:li:dataset:(urn:li:dataPlatform:snowflake,a.b.c,PROD)")


def test_query_masks_derived_columns_too(svc):
    s, audit = svc
    out = s.query("urn:li:corpuser:ana", "SELECT u.id, lower(u.email) AS contact, o.amount FROM main.users u "
                  "JOIN main.orders o ON o.user_id = u.id ORDER BY o.amount", connection="wh")
    assert out["columns"] == ["id", "contact", "amount"] and out["masked_columns"] == ["contact"]
    assert out["rows"][0] == [1, "u•••@•••.com", 3.0] and sorted(out["tables"]) == [ORDERS, USERS]
    star = s.query("urn:li:corpuser:ana", "SELECT * FROM main.users WHERE id < 3", urn=USERS)
    assert star["masked_columns"] == ["email"] and len(star["rows"]) == 3
    with_cte = s.query("urn:li:corpuser:ana", "WITH x AS (SELECT email FROM main.users) SELECT count(*) AS n FROM x",
                       connection="wh")
    assert with_cte["rows"] == [[150]] and with_cte["masked_columns"] == []      # an aggregate count reveals no value
    logged = audit.events[-1]
    assert logged[0] == "data.queried" and logged[3]["tables"] == [USERS]


def test_only_reads_of_catalogued_permitted_tables(svc):
    s, audit = svc
    for sql, msg in (("DELETE FROM main.users", "SELECT"), ("SELECT 1; SELECT 2", "single statement|one statement"),
                     ("SELECT * FROM users", "qualify"), ("SELECT 1", "no table"), ("SELEC x", "parse|SELECT")):
        with pytest.raises(DataAccessError, match=msg):
            s.query("urn:li:corpuser:ana", sql, connection="wh")
    with pytest.raises(DataAccessError, match="may not query main.orders") as e:
        s.query("urn:li:corpuser:eve", "SELECT * FROM main.users u JOIN main.orders o ON o.user_id = u.id", connection="wh")
    assert e.value.status == 403 and audit.events[-1][0] == "data.query_denied"
    with pytest.raises(DataAccessError, match="not in the catalog"):
        s.query("urn:li:corpuser:ana", "SELECT * FROM main.sqlite_master", connection="wh")
    capped = s.query("urn:li:corpuser:ana", "SELECT id FROM main.users", connection="wh", limit=5000)
    assert len(capped["rows"]) == 150 and not capped["truncated"]
    five = s.query("urn:li:corpuser:ana", "SELECT id FROM main.users", connection="wh", limit=5)
    assert len(five["rows"]) == 5 and five["truncated"]


def test_sql_helpers():
    tree, tables = tables_in("WITH a AS (SELECT * FROM s.t) SELECT a.x, 1 AS one, * FROM a JOIN s.u ON true", "postgres")
    assert sorted(t.name for t in tables) == ["t", "u"]
    assert output_sources(tree) == [{"x"}, set(), None]
