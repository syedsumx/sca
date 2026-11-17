"""Budget checks on their own (the API, scopes and notifications are in test_budgets.py): a threshold is crossed
when the spend reaches it, not when a rounded percentage does."""

from datetime import datetime

import pytest
from sqlalchemy import insert

from cairn.common.db import get_engine, init_db
from cairn.common.security import Principal
from cairn.reports.budgets import Budgets
from cairn.reports.costs import asset_costs

GL = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.ledger,PROD)"
OPS = Principal(urn="urn:li:corpuser:ops", via="dev")


def ms(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T12:00:00+00:00").timestamp() * 1000)


@pytest.fixture
def budgets(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    sent = []
    b = Budgets(engine, notify=lambda *a: sent.append(a), may_operate=lambda p: True)
    return engine, b, sent


def test_a_threshold_is_not_crossed_by_rounding(budgets):
    engine, b, sent = budgets
    bid = b.create(OPS, {"name": "ledger", "amount_usd": 10_000, "period": "month", "scope": {"tables": [GL]},
                         "forecast": False})["id"]
    with engine.begin() as conn:
        conn.execute(insert(asset_costs).values(urn=GL, day="2026-11-02", platform="snowflake", storage_usd=0,
                                                compute_usd=9_996.0, source="api"))
    out = b.check(bid, ms("2026-11-03"))
    assert out["spent_usd"] == 9_996.0
    assert out["sent"] == ["80"]                         # $4 under the budget is not "100% spent"
    with engine.begin() as conn:
        conn.execute(insert(asset_costs).values(urn=GL, day="2026-11-03", platform="snowflake", storage_usd=0,
                                                compute_usd=4.0, source="api"))
    assert b.check(bid, ms("2026-11-03"))["sent"] == ["100"]


def test_the_audit_names_the_budget_changed_not_one_in_the_body(budgets):
    from cairn.common.audit import AuditLog

    engine, b, _ = budgets
    b.audit = AuditLog(engine)
    real = b.create(OPS, {"name": "real", "amount_usd": 5, "scope": {"all": True}, "id": "decoy"})["id"]
    b.update(OPS, real, {"amount_usd": 50, "id": "decoy"})
    ids = [e["details"]["id"] for e in b.audit.export() if e["action"].startswith("budget.")]
    assert ids == [real, real]
