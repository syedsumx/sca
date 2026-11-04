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
