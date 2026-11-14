"""System tables (L134), without Trino: how Trino's questions about ``cairn_system`` tables are answered (read-only,
a person's own rows, statement text masked, failing closed), the settings, the table definitions and their
catalog entries. The end-to-end test on a real Trino, store and catalog is tests/test_system_tables_e2e.py."""

import json

import pytest

from cairn.dataaccess.system_tables import BY_NAME, SPECS, SystemTables
from cairn.dataaccess.trino import CatalogMapping, TrinoAccess
from cairn.governance.pdp import Decision

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
HISTORY = "urn:li:dataset:(urn:li:dataPlatform:iceberg,cairn_system.query.history,PROD)"


class Plan:
    def masks_field(self, name):
        return False


def access(decide):
    st = SystemTables(None, {"uri": "http://catalog.invalid"})
    a = TrinoAccess(decide, lambda actor, urn: Plan(), {"cairn_system": CatalogMapping("iceberg", "cairn_system")})
    a.system_tables = st
    return a


def grants(actor, groups, privilege, urn):
    allowed = actor == ADMIN or privilege in ("VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE")
    return Decision(decision="ALLOW" if allowed else "DENY", reason="test")


def inp(user, op, **resource):
    return {"context": {"identity": {"user": user, "groups": []}}, "action": {"operation": op, "resource": resource}}


def table(t="history", schema="query"):
    return {"catalogName": "cairn_system", "schemaName": schema, "tableName": t}


def column(name, t="history", schema="query"):
    return {"column": {**table(t, schema), "columnName": name, "columnType": "varchar"}}


def test_a_person_gets_their_own_rows_and_the_statement_masked():
    a = access(grants)
    assert a.urn("cairn_system", "query", "history") == HISTORY
    assert a.row_filters(inp("ana", "GetRowFilters", table=table())) == [{"expression": "user_name = 'ana'"}]
    assert a.row_filters(inp("admin", "GetRowFilters", table=table())) == []
    # Tables without a person's rows: no filter beyond the policies'.
    assert a.row_filters(inp("ana", "GetRowFilters", table=table("table_lineage", "lineage"))) == []
    masked = a.column_mask(inp("ana", "GetColumnMask", **column("statement")))
    assert masked and "••••" in masked["expression"]
    assert a.column_mask(inp("ana", "GetColumnMask", **column("error"))) is not None
    assert a.column_mask(inp("ana", "GetColumnMask", **column("statement_hash"))) is None
    assert a.column_mask(inp("admin", "GetColumnMask", **column("statement"))) is None


def test_read_only_for_everyone_and_reads_go_through_the_policies():
    a = access(grants)
    assert a.allow(inp("ana", "SelectFromColumns", table={**table(), "columns": ["user_name"]}))
    for user in ("admin", "ana"):
        for op in ("InsertIntoTable", "DeleteFromTable", "DropTable", "ExecuteTableProcedure", "SetTableProperties"):
            assert not a.allow(inp(user, op, table=table())), (user, op)
        assert not a.allow(inp(user, "CreateSchema", schema={"catalogName": "cairn_system", "schemaName": "mine"}))
    deny = access(lambda *a: Decision(decision="DENY", reason="no policy"))
    assert not deny.allow(inp("eve", "SelectFromColumns", table={**table(), "columns": ["user_name"]}))


def test_fails_closed_when_no_decision_can_be_had():
    def broken(actor, groups, privilege, urn):
        if privilege in ("MANAGE_SYSTEM_OPERATIONS", "VIEW_QUERY_TEXT"):
            raise RuntimeError("the policy engine is away")
        return Decision(decision="ALLOW", reason="test")
    a = access(broken)
    assert {"expression": "user_name = 'admin'"} in a.row_filters(inp("admin", "GetRowFilters", table=table()))
    assert "••••" in a.column_mask(inp("admin", "GetColumnMask", **column("statement")))["expression"]


def test_definitions_and_catalog_entries():
    assert sorted(BY_NAME) == ["access.decisions", "audit.events", "billing.usage", "compute.warehouse_events",
                               "jobs.runs", "lineage.column_lineage", "lineage.table_lineage", "query.history"]
    owned = {s.name for s in SPECS if s.owner_filtered}
    assert {"query.history", "billing.usage", "jobs.runs"} <= owned and "lineage.table_lineage" not in owned
    for spec in SPECS:
        assert spec.description and all(c.doc for c in spec.columns) and spec.columns[-1].name == "synced_at"
        schema = SystemTables.iceberg_schema(spec)
        assert [f.name for f in schema.fields] == [c.name for c in spec.columns]
        assert len(SystemTables.arrow_schema(spec)) == len(spec.columns)
    st = SystemTables(None, {"uri": "http://catalog.invalid"})
    mcps = st.catalog_mcps()
    props = next(m for m in mcps if m["entityUrn"] == HISTORY and m["aspectName"] == "datasetProperties")
    value = json.loads(props["aspect"]["value"])
    assert value["qualifiedName"] == "cairn_system.query.history" and value["customProperties"]["masked_columns"] == "error,statement"


def test_settings(monkeypatch):
    assert SystemTables.from_env(None) is None
    monkeypatch.setenv("CAIRN_SYSTEM_TABLES_CATALOG", json.dumps({"token": "x"}))
    with pytest.raises(ValueError, match="uri"):
        SystemTables.from_env(None)
    monkeypatch.setenv("CAIRN_SYSTEM_TABLES_CATALOG", json.dumps({"uri": "http://c"}))
    monkeypatch.setenv("CAIRN_SYSTEM_TABLES_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("CAIRN_SYSTEM_TABLES_RETENTION_DAYS", "14")
    st = SystemTables.from_env(None)
    assert (st.trino_catalog, st.interval_s, st.default_retention) == ("cairn_system", 30.0, 14)
    assert st.run_due(now=0) == 0                    # not due yet at the start of the clock
