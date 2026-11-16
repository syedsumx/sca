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
