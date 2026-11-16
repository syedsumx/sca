"""`cairn dr reconcile` without a store or catalog: every table is checked, nested namespaces too, at the path the
REST protocol gives it; the catalog token never has to be on the command line (the real drill is
tests/test_dr_drill.py)."""

from __future__ import annotations

import argparse
import io
import json

from cairn.pipelines import dr


class Response:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class Catalog:
    """Answers like Cairn Catalog: namespaces ("a",) and ("a", "b"), one table in each."""
    from pyiceberg.catalog.rest import RestCatalog as _R
    _split_identifier_for_path = _R._split_identifier_for_path
    _encode_namespace_path = _R._encode_namespace_path
    _identifier_to_validated_tuple = _R._identifier_to_validated_tuple
    identifier_to_tuple = staticmethod(_R.identifier_to_tuple)
    _namespace_separator = "%1F"

    def __init__(self):
        self.asked: list[str] = []
        known = {"v1/namespaces/a/tables/t": "a-t", "v1/namespaces/a%1Fb/tables/u": "ab-u"}
        outer = self

        class Session:
            def get(self, url):
                outer.asked.append(url)
                key = known.get(url)
                if key is None:
                    return Response(404, {"error": {"message": "no such table"}})
                return Response(200, {"metadata-location": f"s3://w/{key}/metadata/00001-x.metadata.json",
                                      "metadata": {"location": f"s3://w/{key}"}})
        self._session = Session()

    def url(self, endpoint, prefixed=True):
        return "v1/" + endpoint

    def list_namespaces(self, parent=()):
        return {(): [("a",)], ("a",): [("a", "b")], ("a", "b"): []}[tuple(parent)]

    def list_tables(self, ns):
        return {("a",): [("a", "t")], ("a", "b"): [("a", "b", "u")]}[tuple(ns)]


class S3:
    def head_object(self, Bucket, Key):
        return {}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(b'{"current-snapshot-id": null, "snapshots": []}')}


def test_nested_namespaces_are_reconciled_too():
    cat = Catalog()
    reports = dr.Reconciler(cat, S3()).reconcile()
    assert {r.table: (r.readable, r.action) for r in reports} == {"a.t": (True, "none"), "a.b.u": (True, "none")}
    assert "v1/namespaces/a%1Fb/tables/u" in cat.asked


def test_the_catalog_token_comes_from_a_file_or_the_environment(tmp_path, monkeypatch):
    from cairn import cli

    a = cli.build_parser().parse_args(["dr", "reconcile", "--catalog", "http://c", "--store", "http://s",
                                       "--access-key", "k"])
    assert a.token == "" and a.token_file == ""          # no longer required on the command line
    seen = {}

    class Rest:
        def __init__(self, name, uri, token, **props):
            seen["token"] = token

    import pyiceberg.catalog.rest

    monkeypatch.setattr(pyiceberg.catalog.rest, "RestCatalog", Rest)
    monkeypatch.setattr(dr.Reconciler, "reconcile", lambda self, apply=False: [])
    args = argparse.Namespace(catalog="http://c", store="http://s", access_key="k", secret_key="s", secret_key_file="",
                              region="us-east-1", apply=False, token="", token_file="")
    monkeypatch.delenv("CAIRN_DR_CATALOG_TOKEN", raising=False)
    assert dr.main(args) == 2
    (tmp_path / "tok").write_text("from-file\n")
    assert dr.main(argparse.Namespace(**{**vars(args), "token_file": str(tmp_path / "tok")})) == 0
    assert seen["token"] == "from-file"
    monkeypatch.setenv("CAIRN_DR_CATALOG_TOKEN", "from-env")
    assert dr.main(args) == 0 and seen["token"] == "from-env"
