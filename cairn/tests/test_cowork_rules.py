"""CoWork's rules that need no model, mail server or Slack: schedules, who may send, and the scheduler process.
(tests/test_cowork.py covers the whole flow with Mailpit and a Slack stand-in.)"""

from datetime import datetime, timezone

import pytest

from cairn.ai.cowork import CoWork, CoWorkError
from cairn.common.db import get_engine, init_db

ANA = "urn:li:corpuser:ana"


@pytest.fixture
def cowork(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(e)
    return CoWork(e, None, None, lambda actor, privilege, urn: True)


@pytest.mark.parametrize("cron", ["0,30 9 * * *", "0,59 * * * *", "*/20 9 * * 1", "* 9 * * *"])
def test_a_schedule_runs_at_most_hourly_wherever_the_clock_is(cowork, cron):
    # The clock is between two firings of the same hour, so the next two firings can be a day apart: the task
    # would still run twice within an hour every day.
    cowork.clock = lambda: datetime(2026, 1, 5, 9, 10, 30, tzinfo=timezone.utc).timestamp()
    with pytest.raises(CoWorkError, match="once an hour"):
        cowork.save_task(ANA, {"name": "x", "prompt": "summarise orders", "cron": cron})


def test_a_seconds_field_is_not_a_schedule(cowork):
    cowork.clock = lambda: datetime(2026, 1, 5, 9, 0, 10, tzinfo=timezone.utc).timestamp()
    with pytest.raises(CoWorkError, match="five-field"):
        cowork.save_task(ANA, {"name": "x", "prompt": "summarise orders", "cron": "0 9 * * * 0,30"})


@pytest.mark.parametrize("cron", ["0 * * * *", "15 7 * * *", "0 8 * * 1", "30 */2 * * *", "@daily", "@hourly"])
def test_hourly_or_rarer_schedules_are_kept(cowork, cron):
    t = cowork.save_task(ANA, {"name": "x", "prompt": "summarise orders", "cron": cron})
    assert t["cron"] == cron and t["enabled"]


class _Workspace:
    """An artifact store with one report, readable by its owner."""

    def artifact(self, actor, aid, version=None):
        return {"id": aid, "owner": ANA, "title": "Orders", "content": "Orders look normal.", "version": 1,
                "sources": ["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"]}


def test_a_suspended_owner_cannot_send_from_a_button_they_still_have(tmp_path, monkeypatch):
    # Slack buttons outlive a Cairn account: a suspended person still has the notification in Slack. Approving
    # from it must not send their report out.
    monkeypatch.setenv("CAIRN_COWORK_SLACK_CHANNELS", "C0SALES")
    e = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(e)
    suspended = set()
    c = CoWork(e, _Workspace(), None, lambda actor, privilege, urn: True, active=lambda p: p not in suspended)
    oid = c.propose(ANA, {"artifact_id": "a1", "send": {"slack": "C0SALES"}})["id"]
    suspended.add(ANA)
    with pytest.raises(CoWorkError) as err:
        c.approve(ANA, oid)
    assert err.value.status == 403 and "suspended" in str(err.value)
    assert c.item(ANA, oid)["status"] == "refused"
