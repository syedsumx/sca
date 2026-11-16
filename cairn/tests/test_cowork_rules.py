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


def test_the_scheduler_that_runs_tasks_has_the_model(monkeypatch):
    # `cairn scheduler` runs CoWork's due tasks; each is a deep-research run, which needs the model provider.
    # Built without AI, every scheduled task failed ("deep research is not configured") and paused after three.
    import types

    from cairn import cli

    seen = {}

    class Stop(Exception):
        pass

    def services(**kw):
        seen.update(kw)
        raise Stop()
    monkeypatch.setattr(cli, "_services", services)
    with pytest.raises(Stop):
        cli.cmd_scheduler(types.SimpleNamespace(tick=60, workers=1, partition=0, partitions=1))
    assert seen.get("with_ai", True) is True


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


def test_a_reject_while_an_approval_is_sending_is_refused(tmp_path, monkeypatch):
    # An approval claims the message ("sending") and then sends it; a reject read before that claim must not mark
    # it rejected, or the approval's "sent" would hide that a message the person rejected went out (or the reverse).
    from sqlalchemy import update

    from cairn.ai.cowork import cowork_outbox

    monkeypatch.setenv("CAIRN_COWORK_SLACK_CHANNELS", "C0SALES")
    e = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(e)
    c = CoWork(e, _Workspace(), None, lambda actor, privilege, urn: True)
    oid = c.propose(ANA, {"artifact_id": "a1", "send": {"slack": "C0SALES"}})["id"]
    seen = c._item(ANA, oid)                                   # the reject's read: still pending
    with e.begin() as conn:                                    # the approval claims it meanwhile
        conn.execute(update(cowork_outbox).where(cowork_outbox.c.id == oid).values(status="sending"))
    monkeypatch.setattr(c, "_item", lambda actor, i: seen)
    with pytest.raises(CoWorkError) as err:
        c.reject(ANA, oid)
    assert err.value.status == 409
    with e.connect() as conn:
        assert conn.execute(cowork_outbox.select().where(cowork_outbox.c.id == oid)).first().status == "sending"


def test_a_task_deleted_after_it_was_claimed_does_not_stop_the_others(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(e)
    c = CoWork(e, _Workspace(), None, lambda actor, privilege, urn: True)
    with pytest.raises(CoWorkError) as err:
        c._run("gone")
    assert err.value.status == 404
