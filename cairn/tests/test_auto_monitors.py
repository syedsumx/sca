"""Auto-monitors: rules keep monitors on matching datasets, never touching people's monitors."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.quality.auto import AUTO_PREFIX, DETACHED, AutoRules, load_rules, sweep
from cairn.quality.models import MonitorSpec
from cairn.services import build_services

ALICE = "urn:li:corpuser:alice"
CERT = "urn:li:structuredProperty:io.cairn.certification.status"


def ds(name: str, platform: str = "postgres") -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},PROD)"


ORDERS, CUSTOMERS, SCRATCH = ds("shop.public.orders"), ds("shop.public.customers"), ds("tmp.scratch", "snowflake")

RULES = AutoRules.model_validate({"rules": [
    {"id": "certified", "select": {"attr": "resource.certification", "op": "eq", "value": "CERTIFIED"},
     "monitors": [{"type": "freshness", "severity": "HIGH"}, {"type": "volume"}]},
    {"id": "pii-nulls", "select": {"attr": "resource.classification", "op": "contains", "value": "PII"},
     "monitors": [{"type": "field_metric", "fields": {"labelled": ["PII"]}, "config": {"metric": "null_pct", "max": 5}}]},
]})


@pytest.fixture
def stack(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)

    def certify(urn):
        graph.aspects[(urn, "structuredProperties")] = {"properties": [{"propertyUrn": CERT, "values": [{"string": "CERTIFIED"}]}]}
    for urn in (ORDERS, CUSTOMERS, SCRATCH):
        graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "email"}]}
    certify(ORDERS)
    certify(CUSTOMERS)
    graph.aspects[(CUSTOMERS, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [{
        "fieldPath": "email", "glossaryTerms": {"terms": [{"urn": "urn:li:glossaryTerm:Classification.PII.Email"}]}}]}
    return s, graph


def auto(s):
    return sorted((m.dataset_urn, m.type, m.config.get("field")) for m in s.quality.store.list_monitors()
                  if m.id.startswith(AUTO_PREFIX))


def test_sweep_creates_monitors_for_matching_datasets_and_is_idempotent(stack):
    s, graph = stack
    report = sweep(s.quality.store, s.quality, graph, RULES)
    assert len(report["created"]) == 5
    assert auto(s) == sorted([(ORDERS, "freshness", None), (ORDERS, "volume", None),
                              (CUSTOMERS, "freshness", None), (CUSTOMERS, "volume", None),
                              (CUSTOMERS, "field_metric", "email")])
    fresh = next(m for m in s.quality.store.list_monitors() if m.type == "freshness" and m.dataset_urn == ORDERS)
    assert fresh.severity == "HIGH" and fresh.created_by == "auto-monitors" and "auto: certified" in fresh.name
    again = sweep(s.quality.store, s.quality, graph, RULES)
    assert again == {"created": [], "updated": [], "removed": [], "detached": [], "profiled": [], "profile_errors": []}


def test_template_changes_update_and_unmatched_datasets_lose_monitors(stack):
    s, graph = stack
    sweep(s.quality.store, s.quality, graph, RULES)
    changed = RULES.model_copy(deep=True)
    changed.rules[0].monitors[0].severity = "CRITICAL"
    report = sweep(s.quality.store, s.quality, graph, changed)
    assert len(report["updated"]) == 2 and not report["created"] and not report["removed"]
    del graph.aspects[(ORDERS, "structuredProperties")]                 # de-certified
    report = sweep(s.quality.store, s.quality, graph, changed)
    assert len(report["removed"]) == 2
    assert (ORDERS, "freshness", None) not in auto(s)
    graph.aspects[(CUSTOMERS, "status")] = {"removed": True}           # soft-deleted dataset
    assert len(sweep(s.quality.store, s.quality, graph, changed)["removed"]) == 3


def test_people_monitors_are_never_touched(stack):
    s, graph = stack
    mine = s.quality.store.create_monitor(MonitorSpec(name="mine", dataset_urn=ORDERS, type="volume"), ALICE)
    sweep(s.quality.store, s.quality, graph, RULES)
    sweep(s.quality.store, s.quality, graph, AutoRules())                # all rules gone
    assert [m.id for m in s.quality.store.list_monitors()] == [mine.id]


def _api(s):
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="alice", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ALICE})]))
    return TestClient(create_api_app(s)), {"X-Cairn-Actor": ALICE}


def test_edited_or_deleted_auto_monitors_are_left_alone(stack, tmp_path, monkeypatch):
    s, graph = stack
    sweep(s.quality.store, s.quality, graph, RULES)
    c, h = _api(s)
    vol = next(m for m in s.quality.store.list_monitors() if m.type == "volume" and m.dataset_urn == ORDERS)
    body = MonitorSpec(**vol.model_dump(include=set(MonitorSpec.model_fields))).model_dump(mode="json")
    body["config"]["min_rows"] = 10
    assert c.put(f"/quality/monitors/{vol.id}", json=body, headers=h).status_code == 200
    fresh = next(m for m in s.quality.store.list_monitors() if m.type == "freshness" and m.dataset_urn == ORDERS)
    assert c.delete(f"/quality/monitors/{fresh.id}", headers=h).status_code == 204
    report = sweep(s.quality.store, s.quality, graph, RULES)
    assert sorted(report["detached"]) == sorted([vol.id, fresh.id]) and not report["created"]
    assert s.quality.store.get_monitor(vol.id).config["min_rows"] == 10
    assert s.quality.store.get_monitor(fresh.id).enabled is False and s.quality.store.get_monitor(fresh.id).config[DETACHED]
    # The API runs the same sweep, with a dry run to preview.
    path = tmp_path / "auto.yaml"
    path.write_text("rules:\n  - id: everything\n    select: {attr: resource.platform, op: eq, value: snowflake}\n"
                    "    monitors: [{type: volume}]\n")
    monkeypatch.setenv("CAIRN_AUTO_MONITORS", str(path))
    assert [r["id"] for r in c.get("/quality/auto/rules", headers=h).json()["rules"]] == ["everything"]
    preview = c.post("/quality/auto/sweep?dry_run=true", headers=h).json()
    assert len(preview["created"]) == 1 and (SCRATCH, "volume", None) not in auto(s)
    assert len(c.post("/quality/auto/sweep", headers=h).json()["created"]) == 1
    assert (SCRATCH, "volume", None) in auto(s)
    assert c.post("/quality/auto/sweep", headers={"X-Cairn-Actor": "urn:li:corpuser:bob"}).status_code == 403


def test_rule_validation_and_caps(stack):
    s, graph = stack
    with pytest.raises(ValueError):
        AutoRules.model_validate({"rules": [{"id": "Bad Id", "select": {}, "monitors": [{"type": "volume"}]}]})
    with pytest.raises(ValueError):
        AutoRules.model_validate({"rules": [{"id": "x", "select": {"attr": "resource.urn", "op": "nope", "value": 1},
                                             "monitors": [{"type": "volume"}]}]})
    capped = AutoRules.model_validate({"rules": [{"id": "all", "max_datasets": 1,
                                                  "select": {"attr": "resource.type", "op": "eq", "value": "dataset"},
                                                  "monitors": [{"type": "volume"}]}]})
    assert len(sweep(s.quality.store, s.quality, graph, capped)["created"]) == 1
    assert load_rules("/nonexistent.yaml").rules == []


def test_rules_directory_is_merged(tmp_path):
    (tmp_path / "a.yaml").write_text("rules:\n  - {id: a, select: {attr: resource.urn, op: eq, value: x}, monitors: [{type: volume}]}\n")
    (tmp_path / "b.yml").write_text("rules:\n  - {id: b, select: {attr: resource.urn, op: eq, value: y}, monitors: [{type: volume}]}\n")
    (tmp_path / "notes.txt").write_text("ignored")
    assert [r.id for r in load_rules(str(tmp_path)).rules] == ["a", "b"]
    (tmp_path / "c.yaml").write_text("rules:\n  - {id: a, select: {attr: resource.urn, op: eq, value: z}, monitors: [{type: volume}]}\n")
    with pytest.raises(ValueError):
        load_rules(str(tmp_path))                                  # duplicate ids across files


# --------------------------------------------------------------------------- profiling
def _warehouse(tmp_path):
    import sqlite3

    db = tmp_path / "wh.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders (id INTEGER, amount REAL, email TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)",
                    [(1, 10.0, "a@x.com"), (2, 30.0, None), (3, 20.0, "c@x.com"), (4, None, "c@x.com")])
    con.commit()
    con.close()
    from cairn.quality.collectors import ConnectionRegistry

    return ConnectionRegistry({"wh": {"url": f"sqlite:///{db}"}})


def test_profile_table_statistics(tmp_path):
    from cairn.quality.profiler import profile_table

    reg = _warehouse(tmp_path)
    p = profile_table(reg.engine("wh"), ds("main.orders", "sqlite"), drop_database=False)
    assert p["rowCount"] == 4 and p["columnCount"] == 3
    f = {x["fieldPath"]: x for x in p["fieldProfiles"]}
    assert f["amount"]["nullCount"] == 1 and float(f["amount"]["mean"]) == 20.0
    assert float(f["amount"]["min"]) == 10.0 and float(f["amount"]["max"]) == 30.0
    assert abs(float(f["amount"]["stdev"]) - (200 / 3) ** 0.5) < 1e-9          # population stddev
    assert f["email"]["uniqueCount"] == 2 and f["email"]["nullProportion"] == 0.25
    assert "sampleValues" not in f["email"]                                     # no data values by default
    with_samples = profile_table(reg.engine("wh"), ds("main.orders", "sqlite"), drop_database=False, sample_values=5)
    assert sorted({x["fieldPath"]: x for x in with_samples["fieldProfiles"]}["email"]["sampleValues"]) == ["a@x.com", "c@x.com"]


def test_unsafe_or_unknown_tables_are_refused(tmp_path):
    from cairn.quality.collectors import CollectorError
    from cairn.quality.profiler import profile_table, table_parts

    assert table_parts(ds("finance.public.orders")) == ("public", "orders")
    with pytest.raises(CollectorError):
        table_parts(ds('x.y";drop table t;--'))
    with pytest.raises(CollectorError):
        profile_table(_warehouse(tmp_path).engine("wh"), ds("main.nope", "sqlite"), drop_database=False)


def test_rules_profile_what_is_due_and_monitors_then_read_it(stack, tmp_path):
    s, graph = stack
    reg = _warehouse(tmp_path)
    target = ds("main.orders", "sqlite")
    graph.aspects[(target, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    rules = AutoRules.model_validate({"rules": [{
        "id": "wh", "select": {"attr": "resource.platform", "op": "eq", "value": "sqlite"},
        "profile": {"connection": "wh", "every_hours": 24, "drop_database": False},
        "monitors": [{"type": "volume"}]}]})
    report = sweep(s.quality.store, s.quality, graph, rules, connections=reg, now=10**13)
    assert report["profiled"] == [target] and not report["profile_errors"]
    assert graph.get_timeseries(target, "datasetProfile")[0]["rowCount"] == 4
    # Recent profile in the catalog (from us or from ingestion): nothing is due.
    assert sweep(s.quality.store, s.quality, graph, rules, connections=reg)["profiled"] == []
    # The auto volume monitor reads the profile through the catalog.
    vol = next(m for m in s.quality.store.list_monitors() if m.dataset_urn == target)
    assert s.quality.evaluate(vol).metric_value == 4
    with pytest.raises(ValueError):
        AutoRules.model_validate({"rules": [{"id": "empty", "select": {"attr": "resource.urn", "op": "eq", "value": "x"}}]})


@pytest.mark.skipif(not __import__("os").environ.get("CAIRN_TEST_PG_URL"), reason="CAIRN_TEST_PG_URL not set")
def test_profile_on_postgres():
    import os

    from sqlalchemy import create_engine, text

    from cairn.quality.profiler import profile_table

    engine = create_engine(os.environ["CAIRN_TEST_PG_URL"])
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS prof_test CASCADE; CREATE SCHEMA prof_test"))
        c.execute(text("CREATE TABLE prof_test.orders (id bigint, amount numeric(10,2), placed timestamptz, note text)"))
        c.execute(text("INSERT INTO prof_test.orders VALUES (1, 10.5, now(), 'a'), (2, 20.5, now(), NULL), (3, NULL, NULL, 'a')"))
    try:
        p = profile_table(engine, ds("shop.prof_test.orders"))          # db.schema.table → schema.table
        f = {x["fieldPath"]: x for x in p["fieldProfiles"]}
        assert p["rowCount"] == 3 and float(f["amount"]["mean"]) == 15.5 and f["note"]["uniqueCount"] == 1
        assert "mean" not in f["placed"]                                  # timestamps are not averaged
    finally:
        with engine.begin() as c:
            c.execute(text("DROP SCHEMA prof_test CASCADE"))
