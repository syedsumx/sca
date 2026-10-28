"""Warehouse tag sync: mapping catalog labels to warehouse tags, SQL per dialect, and only ever
undoing tags Cairn applied."""

import pytest

from cairn.governance.tag_sync import (
    TagSyncConfig, TagSyncError, TagSyncService, Target, desired_tags, load_targets, statement, table_ref)

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.customers,PROD)"
PG = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.orders,PROD)"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
PHONE = "urn:li:glossaryTerm:Classification.PII.Phone"

TARGET = {"id": "sf", "dialect": "snowflake", "connection": "wh", "platform": "snowflake", "tag_schema": "GOV.TAGS",
          "mappings": [{"label": EMAIL, "tag": "PII", "value": "email"},
                       {"label": "urn:li:glossaryTerm:Classification.PII.*", "tag": "PII", "value": "other"},
                       {"label": "urn:li:tag:Confidential", "tag": "CONFIDENTIALITY", "value": "high"}]}


def aspects(email_terms=(EMAIL,), table_tags=("urn:li:tag:Confidential",)):
    return {
        "globalTags": {"tags": [{"tag": t} for t in table_tags]},
        "schemaMetadata": {"fields": [
            {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": u} for u in email_terms]}},
            {"fieldPath": "id"},
        ]},
        "editableSchemaMetadata": {"editableSchemaFieldInfo": [
            {"fieldPath": "phone", "glossaryTerms": {"terms": [{"urn": PHONE}]}}]},
    }


class Warehouse:
    def __init__(self, fail_on=()):
        self.statements: list[str] = []
        self.fail_on = fail_on

    def __call__(self, connection, statements):
        assert connection == "wh"
        self.statements += statements
        return [("tag does not exist" if any(f in s for f in self.fail_on) else None) for s in statements]


@pytest.fixture
def svc(engine):
    catalog = {SF: aspects(), PG: aspects()}
    wh = Warehouse()
    s = TagSyncService(engine, lambda: [(u, {}, a) for u, a in catalog.items()], wh,
                       lambda: TagSyncConfig.model_validate({"targets": [TARGET]}))
    return s, wh, catalog


def test_mapping_priority_and_conflicts():
    t = Target.model_validate(TARGET)
    want, conflicts = desired_tags(t, SF, aspects(email_terms=(PHONE, EMAIL)))
    assert want == {(f"{SF}|email", "PII"): "email", (f"{SF}|phone", "PII"): "other", (SF, "CONFIDENTIALITY"): "high"}
    assert conflicts == []          # first matching mapping wins without a conflict note
    t2 = Target.model_validate({**TARGET, "mappings": [{"label": EMAIL, "tag": "PII", "value": "email"},
                                                       {"label": PHONE, "tag": "PII", "value": "phone"}]})
    _, conflicts = desired_tags(t2, SF, aspects(email_terms=(EMAIL, PHONE)))
    assert conflicts and "kept over 'phone'" in conflicts[0]


def test_sql_per_dialect_quotes_and_escapes():
    sf = Target.model_validate(TARGET)
    op = {"op": "SET", "urn": SF, "field": "email", "tag": "PII", "value": "o'brien"}
    assert statement(sf, op) == ('ALTER TABLE "ANALYTICS"."PUBLIC"."CUSTOMERS" MODIFY COLUMN "EMAIL" '
                                 "SET TAG GOV.TAGS.PII = 'o''brien'")
    assert statement(sf, {**op, "op": "UNSET", "field": None}) == 'ALTER TABLE "ANALYTICS"."PUBLIC"."CUSTOMERS" UNSET TAG GOV.TAGS.PII'
    db = Target.model_validate({**TARGET, "dialect": "databricks", "platform": "databricks", "tag_schema": None})
    dbu = "urn:li:dataset:(urn:li:dataPlatform:databricks,main.sales.orders,PROD)"
    assert statement(db, {**op, "urn": dbu, "field": "Email"}) == \
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `Email` SET TAGS ('PII' = 'o''brien')"
    assert statement(db, {**op, "urn": dbu, "op": "UNSET"}) == \
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `email` UNSET TAGS ('PII')"
    assert table_ref("db.Mixed.t", "snowflake") == '"DB"."Mixed"."T"'
    with pytest.raises(ValueError):
        table_ref("just_a_table", "snowflake")
    with pytest.raises(ValueError):
        Target.model_validate({**TARGET, "mappings": [{"label": EMAIL, "tag": "PII; DROP", "value": "x"}]})


def test_sync_applies_then_only_changes_and_unsets_its_own(svc):
    s, wh, catalog = svc
    first = s.sync("sf", "urn:li:corpuser:admin")
    assert first["applied"] == 3 and first["datasets"] == 1          # only the snowflake dataset
    assert all("SET TAG" in q for q in wh.statements)
    assert s.sync("sf", "urn:li:corpuser:admin")["operations"] == []  # nothing changed: nothing to do
    catalog[SF] = aspects(email_terms=(), table_tags=())             # someone removed the labels
    wh.statements.clear()
    out = s.sync("sf", "urn:li:corpuser:admin")
    assert sorted(op["op"] + " " + (op["field"] or "table") for op in out["operations"]) == ["UNSET email", "UNSET table"]
    assert all("UNSET TAG" in q for q in wh.statements)
    assert s.overview()[0]["managed_tags"] == 1                       # phone is still tagged


def test_failures_are_reported_and_retried(engine):
    wh = Warehouse(fail_on=("CONFIDENTIALITY",))
    s = TagSyncService(engine, lambda: [(SF, {}, aspects())], wh, lambda: TagSyncConfig.model_validate({"targets": [TARGET]}))
    out = s.sync("sf", "x")
    assert out["applied"] == 2 and out["failed"] == 1
    assert [op["error"] for op in out["operations"] if op["error"]] == ["tag does not exist"]
    assert [op["tag"] for op in s.sync("sf", "x", dry_run=True)["operations"]] == ["CONFIDENTIALITY"]   # retried


def test_dry_run_and_scoped_plans_write_nothing(svc):
    s, wh, catalog = svc
    out = s.sync("sf", "x", dry_run=True)
    assert out["dry_run"] and len(out["operations"]) == 3 and wh.statements == [] and out["applied"] == 0
    s.sync("sf", "x")
    other = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.other,PROD)"
    catalog[other] = aspects()
    scoped = s.sync("sf", "x", dry_run=True, urns=[other])
    assert {op["urn"] for op in scoped["operations"]} == {other}      # SF's tags are not touched
    assert s.overview()[0]["last_run"]["dry_run"] is True
    with pytest.raises(TagSyncError):
        s.sync("nope", "x")


def test_targets_without_a_connection_only_plan(engine, tmp_path):
    (tmp_path / "a.yaml").write_text("targets:\n  - {id: plan-only, dialect: snowflake, platform: snowflake, "
                                     "mappings: [{label: 'urn:li:tag:Confidential', tag: C, value: 'y'}]}\n")
    cfg = load_targets(str(tmp_path))
    assert [t.id for t in cfg.targets] == ["plan-only"]
    s = TagSyncService(engine, lambda: [(SF, {}, aspects())], Warehouse(), lambda: cfg)
    assert s.sweep() == {"plan-only": {"applied": 0, "failed": 0, "dry_run": True, "planned": 1}}
