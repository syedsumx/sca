"""Data contracts: schema promises checked against the catalog and proposed dbt models, monitors
owned by the contract, an overall status, versioning, and breach notifications."""

import pytest

from cairn.quality.contracts import (
    ContractError, ContractService, ContractSpec, check_proposed, dbt_columns, schema_violations, type_family)
from test_quality_engine import _engine, _fill, warehouse  # noqa: F401  (fixture)

from conftest import DS

SPEC = {
    "dataset": DS, "version": "1.0.0", "owner": "orders-team",
    "schema": {"columns": [{"name": "id", "type": "integer", "required": True}, {"name": "email", "type": "string"}]},
    "freshness": {"max_age_hours": 24},
    "quality": [{"name": "email nulls", "type": "field_metric",
                 "config": {"source": "sql", "connection": "wh", "table": "orders", "field": "email", "metric": "null_pct",
                            "max": 50}}],
}


def schema(*fields):
    return {"fields": [{"fieldPath": n, "nativeDataType": t, "nullable": nl} for n, t, nl in fields]}


def test_types_compare_by_family():
    assert type_family("VARCHAR(255)") == type_family("text") == type_family("STRING") == "STRING"
    assert type_family("NUMBER(38,0)") == type_family("bigint") == "INTEGER"
    assert type_family("NUMBER(10,2)") == type_family("double precision") == "NUMBER"
    assert type_family("timestamp without time zone") == type_family("TIMESTAMP_NTZ") == "TIMESTAMP"


def test_schema_violations():
    part = ContractSpec.model_validate(SPEC).schema_
    ok = {"id": {"type": "NUMBER(38,0)", "nullable": False}, "EMAIL": {"type": "VARCHAR", "nullable": True},
          "extra": {"type": "int"}}
    assert schema_violations(part, ok) == []
    bad = {"id": {"type": "VARCHAR", "nullable": True}}
    assert schema_violations(part, bad) == ["column id is VARCHAR, the contract says integer",
                                            "column id must not be nullable", "column email is missing"]
    strict = part.model_copy(update={"allow_extra_columns": False})
    assert schema_violations(strict, ok) == ["columns not in the contract: extra"]


def test_dbt_manifest_check_for_ci():
    manifest = {"nodes": {
        "model.shop.orders": {"unique_id": "model.shop.orders", "resource_type": "model", "name": "orders",
                              "columns": {"id": {"data_type": "bigint"}, "email_address": {"data_type": "text"}}},
        "test.shop.nn": {"resource_type": "test", "column_name": "id", "test_metadata": {"name": "not_null"},
                         "depends_on": {"nodes": ["model.shop.orders"]}},
    }}
    cols = dbt_columns(manifest, "orders")
    assert cols == {"id": {"type": "bigint", "nullable": False}, "email_address": {"type": "text", "nullable": True}}
    assert check_proposed(ContractSpec.model_validate(SPEC), cols) == ["column email is missing"]
    with pytest.raises(ValueError):
        dbt_columns(manifest, "customers")


def test_spec_validation():
    with pytest.raises(ValueError):
        ContractSpec.model_validate({"dataset": DS, "version": "1"})                     # promises nothing
    with pytest.raises(ValueError):
        ContractSpec.model_validate({**SPEC, "quality": [{"name": "x", "type": "custom_sql", "config": {}}]})
    assert ContractSpec.model_validate(SPEC).owner == "urn:li:corpuser:orders-team"


@pytest.fixture
def contracts(engine, graph, warehouse):  # noqa: F811
    url, wh = warehouse
    qe = _engine(engine, graph, url)
    events = []
    svc = ContractService(engine, graph, qe, lambda kind, row, old, new: events.append((kind, old, new)))
    return svc, qe, graph, wh, events


def test_publish_creates_monitors_and_status_follows_them(contracts):
    svc, qe, graph, wh, events = contracts
    graph.aspects[(DS, "schemaMetadata")] = schema(("id", "bigint", False), ("email", "text", True))
    out = svc.upsert(ContractSpec.model_validate(SPEC), "urn:li:corpuser:ana")
    assert out["status"] == "PENDING"                       # monitors have not run yet
    mids = sorted(m.id for m in qe.store.list_monitors(DS))
    assert len(mids) == 2 and all(m.startswith("contract-") for m in mids)
    _fill(wh, 10)                                           # 10% null emails, well under 50
    for m in qe.store.list_monitors(DS):
        if m.type == "field_metric":
            qe.evaluate(m, now=1_760_000_000_000)
    graph.timeseries[(DS, "operation")] = [{"lastUpdatedTimestamp": 1_760_000_000_000 - 3_600_000,
                                             "timestampMillis": 1_760_000_000_000}]
    for m in qe.store.list_monitors(DS):
        if m.type == "freshness":
            qe.evaluate(m, now=1_760_000_000_000)
    status = svc.refresh(DS)
    assert status["status"] == "MET", status["parts"]
    graph.aspects[(DS, "schemaMetadata")] = schema(("id", "varchar", True))     # a breaking change lands
    broken = svc.refresh(DS)
    assert broken["status"] == "BROKEN" and "email is missing" in broken["parts"][0]["detail"]
    assert events == [("broken", "MET", "BROKEN")]
    graph.aspects[(DS, "schemaMetadata")] = schema(("id", "bigint", False), ("email", "text", True))
    assert svc.refresh(DS)["status"] == "MET" and events[-1] == ("restored", "BROKEN", "MET")


def test_versions_are_immutable_and_retiring_removes_monitors(contracts):
    svc, qe, graph, wh, events = contracts
    svc.upsert(ContractSpec.model_validate(SPEC), "a")
    svc.upsert(ContractSpec.model_validate(SPEC), "a")                    # same terms: no-op
    with pytest.raises(ContractError, match="bump the version"):
        svc.upsert(ContractSpec.model_validate({**SPEC, "freshness": {"max_age_hours": 6}}), "a")
    v2 = ContractSpec.model_validate({**SPEC, "version": "2.0.0", "freshness": None, "quality": []})
    svc.upsert(v2, "a")
    assert [m.type for m in qe.store.list_monitors(DS)] == []             # v2 dropped the monitors
    assert [v["version"] for v in svc.versions(DS)] == ["2.0.0", "1.0.0"]
    with pytest.raises(ContractError, match="different terms"):
        svc.upsert(ContractSpec.model_validate({**SPEC, "version": "2.0.0"}), "a")
    svc.upsert(ContractSpec.model_validate({**SPEC, "version": "3.0.0"}), "a")
    assert svc.delete(DS, "a") and qe.store.list_monitors(DS) == [] and svc.get(DS) is None


def test_cli_check_fails_ci_on_a_breaking_model(tmp_path, capsys):
    import json

    import yaml

    from cairn.cli import main

    (tmp_path / "contract.yaml").write_text(yaml.safe_dump(SPEC))
    manifest = {"nodes": {"model.shop.orders": {"unique_id": "model.shop.orders", "resource_type": "model",
                                                 "name": "orders", "columns": {"id": {"data_type": "bigint"}}}}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SystemExit) as e:
        main(["contract", "check", str(tmp_path / "contract.yaml"), "--manifest", str(tmp_path / "manifest.json"),
              "--model", "orders"])
    assert e.value.code == 1 and "column email is missing" in capsys.readouterr().out
    (tmp_path / "cols.yaml").write_text(yaml.safe_dump({"id": {"type": "int", "nullable": False},
                                                        "email": {"type": "varchar"}}))
    main(["contract", "check", str(tmp_path / "contract.yaml"), "--columns", str(tmp_path / "cols.yaml")])
    assert "keeps it (2 columns)" in capsys.readouterr().out
