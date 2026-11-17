"""Workbook formulas and elements compiled to SQL: what spreadsheet users write becomes safe Trino SQL (the queries
themselves run on real Trino, as the viewer, in tests/test_trino_e2e.py)."""

import re

import pytest

from cairn.sqlworkspace.formulas import Compiler, FormulaError
from cairn.sqlworkspace.workbooks import WorkbookError, compile_element

COLS = {"Amount": '"Amount"', "Region": '"Region"', "Ordered at": '"Ordered at"', "Discount": '"Discount"'}


def sql(formula: str) -> str:
    return Compiler(COLS).compile(formula).sql


def test_formulas_compile_to_sql():
    assert sql("[Amount] * 1.2") == '("Amount" * 1.2)'
    assert sql("[amount] - [Discount]") == '("Amount" - "Discount")'                     # names ignore case
    assert sql("[Amount] / 2") == '(CAST("Amount" AS double) / NULLIF(2, 0))'             # no integer division
    assert sql('If([Region] = "EU", "Europe", "Other")') == \
        "CASE WHEN (\"Region\" = 'EU') THEN 'Europe' ELSE 'Other' END"
    assert sql('[Region] & " / " & Text([Amount])') == \
        "(CAST((CAST(\"Region\" AS varchar) || CAST(' / ' AS varchar)) AS varchar) || CAST(CAST(\"Amount\" AS varchar) AS varchar))"
    assert sql('DateTrunc("month", [Ordered at])') == "date_trunc('month', \"Ordered at\")"
    assert sql('DatePart("year", [Ordered at])') == 'extract(YEAR FROM "Ordered at")'
    assert sql("Not IsNull([Amount]) And [Amount] >= 10 Or [Region] <> 'US'") == \
        "(((NOT (\"Amount\" IS NULL)) AND (\"Amount\" >= 10)) OR (\"Region\" <> 'US'))"
    assert sql("-[Amount] + 2 * 3") == '((-"Amount") + (2 * 3))'                           # precedence
    assert sql("CountDistinct([Region])") == 'count(DISTINCT "Region")'
    c = Compiler(COLS).compile("Sum([Amount]) / Count()")
    assert c.aggregate and c.columns == {"Amount"}


def test_formulas_cannot_inject_sql():
    # Text stays text, however it is quoted; brackets name columns that must exist.
    assert sql("\"x'); DROP TABLE t; --\"") == "'x''); DROP TABLE t; --'"
    with pytest.raises(FormulaError, match="no column"):
        sql('[Amount"; DROP TABLE t; --]')
    for bad, msg in [("Amount + 1", "square brackets"), ("Exec([Amount])", "no function exec"),
                     ("[Amount]; DROP TABLE t", "unexpected"), ("Sum(Sum([Amount]))", "inside another aggregate"),
                     ('DateTrunc("fortnight", [Ordered at])', "unit"), ("Round([Amount], [Discount])", "whole number"),
                     ("If([Amount])", "takes 2 to 3"), ("(1 + 2", "expected ')'"), ("1 +", "unexpected"),
                     ("[Amount] -- comment", "square brackets"), ("`x`", "unexpected")]:
        with pytest.raises(FormulaError, match=re.escape(msg)):
            sql(bad)
    with pytest.raises(FormulaError, match="deeply"):
        sql("(" * 150 + "1" + ")" * 150)
    # Long chains nest as deeply without a bracket: refused as a formula error, not a crash of the compiler.
    for deep in ("+".join(["1"] * 1500), "-" * 1500 + "1", "Not " * 900 + "TRUE",
                 "Abs(" * 120 + "1" + ")" * 120):
        with pytest.raises(FormulaError, match="deeply"):
            sql(deep)
    assert sql("+".join(["1"] * 150)).count("+") == 149              # a long sum is still fine


SRC = ["id", "Region", "Amount", "Discount", "Ordered at"]


def test_elements_compile_to_one_query():
    el = {"source": {"table": "lake.sales.orders"},
          "columns": [{"name": "Net", "formula": "[Amount] * (1 - [Discount])"},
                      {"name": "Big", "formula": "[Net] > 100"}],
          "filters": [{"column": "Region", "op": "in", "values": ["EU", "US"]}, {"column": "Big", "op": "=", "value": True}],
          "sort": [{"column": "Net", "desc": True}], "limit": 50}
    q = compile_element(el, SRC)
    assert q.startswith('SELECT "id", "Region", "Amount", "Discount", "Ordered at", (("Amount" * (1 - "Discount"))) AS "Net", '
                        '(((("Amount" * (1 - "Discount"))) > 100)) AS "Big" FROM "lake"."sales"."orders" WHERE "Region" IN '
                        "('EU', 'US') AND ((((\"Amount\" * (1 - \"Discount\"))) > 100)) = TRUE"), q
    assert q.endswith('ORDER BY (("Amount" * (1 - "Discount"))) DESC LIMIT 50')

    grouped = {**el, "group_by": ["Region"], "aggregates": [{"name": "Revenue", "formula": "Sum([Net])"},
                                                            {"name": "Orders", "formula": "Count()"}],
               "filters": [{"column": "Revenue", "op": ">", "value": 10, "on": "groups"}],
               "sort": [{"column": "Revenue", "desc": True}]}
    g = compile_element(grouped, SRC)
    assert 'SELECT "Region", sum("Net") AS "Revenue", count(*) AS "Orders" FROM (SELECT' in g
    assert 'GROUP BY "Region" HAVING sum("Net") > 10 ORDER BY "Revenue" DESC LIMIT 50' in g

    piv = {"source": {"table": "lake.sales.orders"}, "group_by": ["Region"],
           "pivot": {"column": "Discount", "aggregate": "Sum([Amount])", "values": [0, 0.1]}}
    p = compile_element(piv, SRC)
    assert 'sum(CASE WHEN "Discount" = 0 THEN "Amount" END) AS "0"' in p
    assert 'sum(CASE WHEN "Discount" = 0.1 THEN "Amount" END) AS "0.1"' in p
    cnt = compile_element({**piv, "pivot": {**piv["pivot"], "aggregate": "Count()"}}, SRC)
    assert 'count_if("Discount" = 0) AS "0"' in cnt


def test_elements_refuse_what_cannot_be_right():
    base = {"source": {"table": "lake.sales.orders"}}
    for el, msg in [
        ({"source": {"table": "orders; drop table x"}}, "catalog.schema.table"),
        ({**base, "columns": [{"name": "Amount", "formula": "1"}]}, "exists"),
        ({**base, "columns": [{"name": "S", "formula": "Sum([Amount])"}]}, "row by row"),
        ({**base, "group_by": ["Region"], "aggregates": [{"name": "A", "formula": "[Amount]"}]}, "aggregate uses"),
        ({**base, "group_by": ["Region"], "aggregates": [{"name": "A", "formula": "Sum([Amount]) + [Discount]"}]},
         "neither grouped"),
        ({**base, "filters": [{"column": "Region", "op": "like", "value": "x"}]}, "filter op"),
        ({**base, "filters": [{"column": "Nope", "op": "=", "value": 1}]}, "no column Nope"),
        ({**base, "filters": [{"column": "Region", "op": "=", "value": 1, "on": "groups"}]}, "needs grouping"),
        ({**base, "limit": 100_000}, "limit"),
        ({**base, "columns": [{"name": 'x"y', "formula": "1"}]}, "no double quotes"),
    ]:
        with pytest.raises(WorkbookError, match=re.escape(msg)):
            compile_element(el, SRC)
    # A filter value is a literal, never SQL.
    q = compile_element({**base, "filters": [{"column": "Region", "op": "=", "value": "EU' OR 1=1 --"}]}, SRC)
    assert "\"Region\" = 'EU'' OR 1=1 --'" in q


def test_an_aggregate_with_where_counts_only_those_rows():
    el = {"source": {"table": "lake.sales.orders"}, "group_by": ["Region"],
          "aggregates": [{"name": "Paid", "formula": "Sum([Amount])", "where": [{"column": "Discount", "op": "=", "value": 0}]},
                         {"name": "N", "formula": "Count()", "where": [{"column": "Region", "op": "in", "values": ["EU"]}]},
                         {"name": "All", "formula": "Sum([Amount])"}]}
    q = compile_element(el, SRC)
    assert 'sum(CASE WHEN "Discount" = 0 THEN "Amount" END) AS "Paid"' in q
    assert "count_if(\"Region\" IN ('EU')) AS \"N\"" in q and 'sum("Amount") AS "All"' in q
    with pytest.raises(WorkbookError, match="no column Nope"):
        nope = [{"column": "Nope", "op": "=", "value": 1}]
        compile_element({**el, "aggregates": [{"name": "X", "formula": "Count()", "where": nope}]}, SRC)



def test_a_filtered_count_never_rewrites_text_in_the_formula():
    """Count() under a ``where`` (a metric's own filters, a pivot value) counts only those rows. That is done in the
    compiler, not by rewriting "count(*)" in the SQL text, so a literal or a column name that reads count(*) is left
    alone and a filter value can never land outside its quotes (a metric's author cannot put SQL into the queries
    every viewer runs)."""
    import sqlglot
    from sqlglot import exp

    from cairn.sqlworkspace.metrics import sql_expression

    payload = "x) OR (SELECT max(salary) FROM hr.pay) > 0 OR (1=1"
    where = [{"column": "Region", "op": "=", "value": payload}]
    el = {"source": {"table": "lake.sales.orders"}, "group_by": ["Region"],
          "aggregates": [{"name": "N", "formula": 'CountIf([Region] = "count(*)")', "where": where},
                         {"name": "M", "formula": "Count()", "where": where}]}
    q = compile_element(el, SRC)
    tree = sqlglot.parse_one(q, read="trino")
    assert not list(tree.find_all(exp.Or))                       # the payload stayed a literal
    assert {t.name for t in tree.find_all(exp.Table)} == {"orders"}
    assert "'count(*)'" in q and "count_if(\"Region\" = 'x) OR" in q
    # The same for the pivot's per-value aggregates, whose values come from the data.
    pv = {"source": {"table": "lake.sales.orders"}, "group_by": ["Region"],
          "pivot": {"column": "Region", "aggregate": 'CountIf([Region] = "count(*)")', "values": [payload]}}
    tree = sqlglot.parse_one(compile_element(pv, SRC), read="trino")
    assert not list(tree.find_all(exp.Or)) and {t.name for t in tree.find_all(exp.Table)} == {"orders"}
    # And a metric's expression as Ask-your-data is given it.
    m = {"name": "m", "measure": 'CountIf([Region] = "count(*)")', "filters": where}
    tree = sqlglot.parse_one(f"SELECT {sql_expression(m)} FROM t", read="trino")
    assert not list(tree.find_all(exp.Or)) and {t.name for t in tree.find_all(exp.Table)} == {"t"}

def test_a_metric_query_is_one_workbook_element():
    from cairn.sqlworkspace.metrics import MetricError, element_for

    rev = {"name": "revenue", "connection": "c", "table": "sales.orders", "measure": "Sum([Amount])",
           "filters": [{"column": "Discount", "op": "=", "value": 0}], "time_column": "Ordered at", "dimensions": ["Region"]}
    n = {**rev, "name": "orders", "measure": "Count()", "filters": []}
    el = element_for([rev, n], {"dimensions": ["Region"], "grain": "month"})
    assert el["group_by"] == ["Region", "period"] and el["sort"] == [{"column": "period"}]
    q = compile_element(el, SRC)
    assert "date_trunc('month', \"Ordered at\")" in q and 'count(*) AS "orders"' in q
    assert 'sum(CASE WHEN "Discount" = 0 THEN "Amount" END) AS "revenue"' in q
    for defs, query, msg in [([rev], {"dimensions": ["id"]}, "not broken down by id"),
                             ([rev, {**n, "table": "sales.other"}], {}, "same table"),
                             ([rev, {**n, "time_column": None}], {"grain": "month"}, "same time column"),
                             ([rev], {"grain": "hour"}, "grain"), ([], {}, "at least one")]:
        with pytest.raises(MetricError, match=msg):
            element_for(defs, query)


def test_a_quoted_table_name_with_a_dot_stays_one_name():
    # A quoted part may hold a dot; splitting on every dot left the text between dots outside any quotes, as SQL.
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp

    from cairn.sqlworkspace.workbooks import _table

    assert _table('"lake.house".sales.orders') == '"lake.house"."sales"."orders"'
    assert _table('lake."my schema"."t"') == '"lake"."my schema"."t"'
    q = compile_element({"source": {"table": '"x. CROSS JOIN secret.pay -- ".y'}}, SRC)
    assert 'FROM "x. CROSS JOIN secret.pay -- "."y" LIMIT' in q
    tree = sqlglot.parse_one(q, read="trino")
    assert [t.name for t in tree.find_all(exp.Table)] == ["y"]


def test_a_value_that_is_not_a_finite_number_is_refused():
    # JSON bodies may carry NaN and Infinity; repr() of them is a bare word that SQL reads as a column name.
    base = {"source": {"table": "lake.sales.orders"}}
    for v in (float("nan"), float("inf"), float("-inf")):
        for el in ({**base, "filters": [{"column": "Amount", "op": "=", "value": v}]},
                   {**base, "filters": [{"column": "Amount", "op": "in", "values": [1, v]}]},
                   {**base, "group_by": ["Region"], "pivot": {"column": "Amount", "aggregate": "Count()", "values": [v]}}):
            with pytest.raises(WorkbookError, match="finite"):
                compile_element(el, SRC)
    q = compile_element({**base, "filters": [{"column": "Amount", "op": ">", "value": 1e300}]}, SRC)
    assert '"Amount" > 1e+300' in q


def test_an_element_of_the_wrong_shape_is_a_400_not_a_crash():
    base = {"source": {"table": "lake.sales.orders"}}
    for el, msg in [({**base, "sort": ["Amount"]}, "sort"), ({**base, "group_by": ["Region"], "pivot": "Amount"}, "pivot"),
                    ({**base, "group_by": ["Region"], "aggregates": ["x"]}, "aggregates"), ({**base, "columns": ["x"]}, "columns"),
                    ({**base, "filters": ["x"]}, "filter"), ({"source": "lake.sales.orders"}, "source"),
                    ({**base, "group_by": "Region"}, "group_by"), ({**base, "group_by": [{"x": 1}]}, "group_by")]:
        with pytest.raises(WorkbookError, match=msg):
            compile_element(el, SRC)
    # Nor before compiling: the service reads the source (and may look up the pivot's values) first.
    from cairn.sqlworkspace.workbooks import Workbooks

    wb = Workbooks(None, None, lambda *a: pytest.fail("nothing is read for an element of the wrong shape"))
    for el in (["x"], {"source": "lake.sales.orders"}, {**base, "pivot": "Amount"}):
        with pytest.raises(WorkbookError):
            wb.compile("urn:li:corpuser:ann", "c", el)
    with pytest.raises(WorkbookError, match="an element is an object"):
        wb.save("urn:li:corpuser:ann", {"name": "w", "connection": "c", "spec": {"elements": ["x"]}})
    # An aggregate's name is used trimmed, also where the sort finds it.
    q = compile_element({**base, "group_by": ["Region"], "aggregates": [{"name": " Revenue ", "formula": "Sum([Amount])"}],
                         "sort": [{"column": "Revenue", "desc": True}]}, SRC)
    assert q.endswith('AS "Revenue" FROM (SELECT "id", "Region", "Amount", "Discount", "Ordered at" FROM '
                      '"lake"."sales"."orders") AS rows GROUP BY "Region" ORDER BY "Revenue" DESC LIMIT 1000')


def test_pivot_values_found_in_the_data_are_compared_as_the_data_gave_them():
    # Results come back as JSON: a date, a decimal or a timestamp is text there. Compared with the column as it is
    # ("Ordered at" = '2024-01-01'), Trino refuses (date = varchar), so a pivot over a date column never ran.
    from cairn.sqlworkspace.workbooks import Workbooks

    ran = []

    class Workspace:
        def run(self, actor, sql, connection, limit, cache=False):
            ran.append(sql)
            return {"columns": ["Ordered at", "__pivot_value", "__n"],
                    "rows": [["2024-01-01", "2024-01-01", 3], [None, None, 1]]}

    wb = Workbooks(None, Workspace(), lambda *a: SRC)
    el = {"source": {"table": "lake.sales.orders"}, "group_by": ["Region"],
          "pivot": {"column": "Ordered at", "aggregate": "Sum([Amount])"}}
    q = wb.compile("urn:li:corpuser:ann", "c", el)["sql"]
    assert 'CAST("Ordered at" AS varchar)) AS "__pivot_value"' in ran[0]           # the probe reads them as text
    assert ran[0].index('"Ordered at", "__pivot_value"') < ran[0].index("ORDER BY")
    assert "CASE WHEN CAST(\"Ordered at\" AS varchar) = '2024-01-01' THEN \"Amount\" END" in q
    assert 'CASE WHEN "Ordered at" IS NULL THEN "Amount" END' in q
    # Values listed in the element keep their type.
    listed = compile_element({**el, "pivot": {**el["pivot"], "column": "Amount", "values": [10, 20]}}, SRC)
    assert 'CASE WHEN "Amount" = 10 THEN "Amount" END' in listed


def test_a_small_element_cannot_compile_to_a_huge_query():
    # Each calculated column is written into the ones that use it: twenty doublings of a few bytes are 20 MB of
    # SQL, thirty would be gigabytes, built in the server's memory from a request of a few hundred bytes.
    base = {"source": {"table": "lake.sales.orders"}}
    cols = [{"name": "c0", "formula": "[Amount] + [Amount]"}] + \
        [{"name": f"c{i}", "formula": f"[c{i - 1}] + [c{i - 1}]"} for i in range(1, 22)]
    with pytest.raises(WorkbookError, match="too long"):
        compile_element({**base, "columns": cols}, SRC)
    # A filtered aggregate writes its condition into every column it reads.
    where = [{"column": "Region", "op": "in", "values": ["x" * 100] * 1000}]
    agg = {"name": "A", "formula": "Sum(" + "+".join(["[Amount]"] * 150) + ")", "where": where}
    with pytest.raises(WorkbookError, match="too long"):
        compile_element({**base, "group_by": ["Region"], "aggregates": [agg]}, SRC)
    # A filter on groups writes the aggregate it filters on again, each time.
    big = {"name": "A", "formula": "Sum(" + "+".join(["[Amount]"] * 150) + ")"}
    having = [{"column": "A", "op": "not_null", "on": "groups"}] * 2_000
    with pytest.raises(WorkbookError, match="too long"):
        compile_element({**base, "group_by": ["Region"], "aggregates": [big], "filters": having}, SRC)
    # As does a row filter on a calculated column.
    calc = [{"name": "N", "formula": "+".join(["[Amount]"] * 150)}]
    rows = [{"column": "N", "op": "not_null"}] * 2_000
    with pytest.raises(WorkbookError, match="too long"):
        compile_element({**base, "columns": calc, "filters": rows}, SRC)
