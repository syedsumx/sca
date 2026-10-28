"""Bulk metadata editing: export, diff preview, per-change authorization, apply, undo."""

import pytest

from cairn.governance.bulk import BulkError, BulkService, diff, export_rows, parse_csv, to_csv

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.orders,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.secret,PROD)"
ANA = "urn:li:corpuser:ana"
QDS, QOTHER = f'"{DS}"', f'"{OTHER}"'     # urns hold commas, so CSV quotes them


def seed(graph):
    graph.aspects[(DS, "datasetProperties")] = {"name": "orders", "description": "Orders"}
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "email"}]}
    graph.aspects[(DS, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [
        {"fieldPath": "email", "description": "Buyer email", "globalTags": {"tags": [{"tag": "urn:li:tag:pii"}]}}]}
    graph.aspects[(DS, "ownership")] = {"owners": [{"owner": ANA, "type": "DATAOWNER"}]}
    graph.aspects[(DS, "globalTags")] = {"tags": [{"tag": "urn:li:tag:gold"}]}
    graph.aspects[(OTHER, "schemaMetadata")] = {"fields": [{"fieldPath": "salary"}]}


@pytest.fixture
def svc(engine, graph):
    seed(graph)
    return BulkService(engine, graph), graph


def test_export_round_trips_without_changes(svc):
    bulk, graph = svc
    text = to_csv(export_rows([DS], graph.get_aspect, lambda u: ["PII"]))
    lines = text.strip().splitlines()
    assert lines[0] == "urn,field,name,platform,description,owners,tags,glossary_terms,domain,classification"
    assert len(lines) == 4                                  # the dataset + 2 columns
    assert "ana" in lines[1] and "gold" in lines[1] and "PII" in lines[1]
    assert ",email,email,postgres,Buyer email,,pii,," in lines[3]
    changes, errors = diff(parse_csv(text), graph.get_aspect)
    assert changes == [] and errors == []


def test_preview_lists_changes_errors_and_denials(svc):
    bulk, graph = svc
    csv = ("urn,field,description,tags,owners,domain\n"
           f"{QDS},,One row per order,gold;certified,ana;bob,finance\n"
           f"{QDS},email,,-,,\n"
           f"{QDS},id,Primary key,,,\n"
           f"{QDS},nope,x,,,\n"
           '"urn:li:chart:(x,y)",,x,,,\n'
           f"{QOTHER},salary,Pay,,,\n")
    allowed = lambda priv, urn: not (urn == OTHER or priv == "EDIT_ENTITY_DOMAINS")   # noqa: E731
    out = bulk.preview(csv, ANA, allowed)
    what = {(c["field"], c["what"]): c for c in out["changes"]}
    assert set(what) == {("", "description"), ("", "tags"), ("", "owners"), ("email", "tags"), ("id", "description")}
    assert what[("", "tags")]["add"] == ["urn:li:tag:certified"] and what[("", "owners")]["add"] == ["urn:li:corpuser:bob"]
    assert what[("email", "tags")]["remove"] == ["urn:li:tag:pii"]
    assert {(c["urn"], c["what"]) for c in out["denied"]} == {(DS, "domain"), (OTHER, "description")}
    assert any("no column 'nope'" in e for e in out["errors"]) and any("not a dataset urn" in e for e in out["errors"])
    assert out["rows"] == 6


def test_apply_writes_one_batch_and_undo_restores(svc):
    bulk, graph = svc
    csv = (f"urn,field,description,tags,glossary_terms\n{QDS},,One row per order,gold;certified,\n"
           f"{QDS},id,Primary key,,Finance.OrderId\n{QDS},email,,-,\n")
    out = bulk.preview(csv, ANA, lambda p, u: True)
    with pytest.raises(BulkError):
        bulk.apply(out["batch_id"], "urn:li:corpuser:mallory", lambda p, u: True)    # not their batch
    res = bulk.apply(out["batch_id"], ANA, lambda p, u: True)
    assert res["applied"] == 5 and res["aspects"] == 3
    assert graph.aspects[(DS, "editableDatasetProperties")]["description"] == "One row per order"
    esm = {f["fieldPath"]: f for f in graph.aspects[(DS, "editableSchemaMetadata")]["editableSchemaFieldInfo"]}
    assert esm["id"]["description"] == "Primary key"
    assert esm["id"]["glossaryTerms"]["terms"] == [{"urn": "urn:li:glossaryTerm:Finance.OrderId"}]
    assert esm["email"]["globalTags"] == {"tags": []} and esm["email"]["description"] == "Buyer email"
    assert graph.emitted[-1]["systemMetadata"]["properties"]["cairn_batch"] == out["batch_id"]
    with pytest.raises(BulkError, match="applied"):
        bulk.apply(out["batch_id"], ANA, lambda p, u: True)
    assert bulk.undo(out["batch_id"], ANA)["restored"] == 3
    assert graph.aspects[(DS, "globalTags")] == {"tags": [{"tag": "urn:li:tag:gold"}]}
    esm = {f["fieldPath"]: f for f in graph.aspects[(DS, "editableSchemaMetadata")]["editableSchemaFieldInfo"]}
    assert "id" not in esm and esm["email"]["globalTags"] == {"tags": [{"tag": "urn:li:tag:pii"}]}
    assert bulk.history(ANA)[0]["state"] == "UNDONE"


def test_undo_refuses_when_someone_changed_it_since(svc):
    bulk, graph = svc
    out = bulk.preview(f"urn,field,tags\n{QDS},,gold;new\n", ANA, lambda p, u: True)
    bulk.apply(out["batch_id"], ANA, lambda p, u: True)
    graph.aspects[(DS, "globalTags")] = {"tags": [{"tag": "urn:li:tag:someone-else"}]}
    with pytest.raises(BulkError, match="changed since"):
        bulk.undo(out["batch_id"], ANA)


def test_rights_are_checked_again_at_apply_and_stale_changes_skipped(svc):
    bulk, graph = svc
    out = bulk.preview(f"urn,field,description,tags\n{QDS},,New text,gold;x\n", ANA, lambda p, u: True)
    graph.aspects[(DS, "editableDatasetProperties")] = {"description": "New text"}    # someone did it already
    res = bulk.apply(out["batch_id"], ANA, lambda p, u: p != "EDIT_ENTITY_TAGS")       # lost tag rights
    assert res == {"batch_id": out["batch_id"], "applied": 0, "already_current": 1, "aspects": 0}


def test_parse_errors():
    with pytest.raises(BulkError, match="header"):
        parse_csv("name,description\nx,y\n")
    assert parse_csv("﻿urn,description\n,\n" + f"{QDS},x\n") == [{"urn": DS, "description": "x"}]
