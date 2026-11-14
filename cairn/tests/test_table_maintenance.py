"""Table maintenance (cairn.pipelines.maintenance) with a recording executor: the statements issued, which
policy applies, failures recorded without stopping the rest, the schedule, and validation. Against real
Trino and Cairn Catalog: tests/test_trino_e2e.py."""

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines.maintenance import MaintenanceError, TableMaintenance

ADMIN = "urn:li:corpuser:admin"


@pytest.fixture
def m(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(e)
    ran = []

    def execute(sql):
        ran.append(sql)
        if "information_schema" in sql:
            return [("sales", "orders"), ("sales", "items"), ("hr", "pay")]
        if "remove_orphan_files" in sql and "items" in sql:
            raise RuntimeError("Access Denied: Cannot execute table procedure")
        return []
    mt = TableMaintenance(e, execute, "lakehouse")
    mt.ran = ran
    return mt


def test_policies_apply_by_table_then_schema_then_default(m):
    m.set_policy("*", {"optimize": False, "expire_days": 30, "every_hours": 24}, ADMIN)
    m.set_policy("sales", {"optimize": True, "expire_days": 7, "orphan_days": 7, "every_hours": 6}, ADMIN)
    m.set_policy("sales.items", {"optimize": True, "optimize_threshold": "64MB", "expire_days": 3, "orphan_days": 3,
                                 "every_hours": 1}, ADMIN)
    assert m.policy_for("sales", "items")["scope"] == "sales.items"
    assert m.policy_for("sales", "orders")["scope"] == "sales"
    assert m.policy_for("hr", "pay")["scope"] == "*"
    out = m.maintain("sales", "items")
    assert [(r["operation"], r["status"]) for r in out] == [("optimize", "ok"), ("expire_snapshots", "ok"),
                                                             ("remove_orphan_files", "error")]
    assert "Access Denied" in out[2]["detail"]
    assert m.ran[-3:] == [
        """ALTER TABLE "lakehouse"."sales"."items" EXECUTE optimize(file_size_threshold => '64MB')""",
        """ALTER TABLE "lakehouse"."sales"."items" EXECUTE expire_snapshots(retention_threshold => '3d')""",
        """ALTER TABLE "lakehouse"."sales"."items" EXECUTE remove_orphan_files(retention_threshold => '3d')"""]
    assert [r["operation"] for r in m.maintain("hr", "pay")] == ["expire_snapshots"]   # the default: no optimize


def test_the_schedule_runs_what_is_due(m):
    assert m.run_due() == 0                                   # no policy: nothing
    m.set_policy("*", {"expire_days": 7, "every_hours": 24}, ADMIN)
    m.set_policy("hr", {"enabled": False}, ADMIN)
    assert m.run_due() == 2                                   # sales.orders and sales.items; hr is off
    assert m.run_due() == 0                                   # done: not due again for a day
    later = int(__import__("time").time() * 1000) + 25 * 3_600_000
    assert m.run_due(later) == 2


def test_validation(m):
    for scope, body in (("a.b.c", {}), ("x y", {}), ("s", {"expire_days": -1}), ("s", {"every_hours": 0}),
                        ("s", {"optimize_threshold": "big"}), ("s", {"orphan_days": "7"})):
        with pytest.raises(MaintenanceError):
            m.set_policy(scope, body, ADMIN)
    with pytest.raises(MaintenanceError):
        m.maintain("sales", "orders; DROP TABLE x")
    with pytest.raises(MaintenanceError):                      # nothing applies
        m.maintain("sales", "orders")
    with pytest.raises(MaintenanceError):
        m.delete_policy("*", ADMIN)


def test_the_routes_are_for_operators(m):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from cairn.common.security import Principal
    from cairn.pipelines.maintenance import build_maintenance_router

    def authorize(p, priv, urn):
        if p.urn != ADMIN:
            raise HTTPException(403, "no")
    def who(request: Request):
        return Principal(request.headers["x-who"], "token")
    app = FastAPI()
    app.include_router(build_maintenance_router(m, who, authorize))
    c = TestClient(app)
    assert c.put("/maintenance/policies/sales", json={"expire_days": 7}, headers={"x-who": "urn:li:corpuser:ana"}).status_code == 403
    assert c.put("/maintenance/policies/sales", json={"expire_days": 7}, headers={"x-who": ADMIN}).status_code == 200
    r = c.post("/maintenance/run", json={"schema": "sales", "table": "orders"}, headers={"x-who": ADMIN})
    assert r.status_code == 200 and [x["operation"] for x in r.json()] == ["optimize", "expire_snapshots"]
    assert len(c.get("/maintenance/runs", params={"table": "sales.orders"}, headers={"x-who": ADMIN}).json()) == 2
