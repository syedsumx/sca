"""Metric alert rules (L53): a change on the period before, a usual range, a threshold; the period still filling is
never judged."""

from datetime import date

import pytest

from cairn.sqlworkspace.metric_alerts import judge, period_start

WEEKS = [date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12)]


def s(*vals):
    return list(zip(WEEKS[-len(vals):], vals))


def test_a_change_on_the_period_before():
    rule = {"kind": "change", "drop_pct": 20}
    assert judge(rule, s(100, 79), "week")[0] == "triggered"          # -21%
    assert judge(rule, s(100, 81), "week")[0] == "ok"                 # -19%
    assert judge(rule, s(100, 300), "week")[0] == "ok"                # rises are not watched here
    assert judge({"kind": "change", "rise_pct": 50}, s(100, 160), "week")[0] == "triggered"
    assert judge(rule, s(100), "week")[0] == "learning"
    state, value, msg = judge(rule, s(200, 150), "week")
    assert (state, value) == ("triggered", 150) and "-25.0%" in msg and "2026-10-12" in msg


def test_a_usual_range_learns_first():
    rule = {"kind": "range", "periods": 8, "band": 3}
    assert judge(rule, s(100, 102, 98, 50), "week")[0] == "learning"            # three periods to learn from
    assert judge(rule, s(100, 102, 98, 101, 99, 100), "week")[0] == "ok"
    assert judge(rule, s(100, 102, 98, 101, 99, 60), "week")[0] == "triggered"
    assert judge(rule, s(100, 100, 100, 100, 100, 100), "week")[0] == "ok"       # no spread: still a band


def test_a_threshold():
    assert judge({"kind": "threshold", "op": "<", "value": 1000}, s(999), "week")[0] == "triggered"
    assert judge({"kind": "threshold", "op": "<", "value": 1000}, s(1000), "week")[0] == "ok"


@pytest.mark.parametrize("grain,day,start", [
    ("day", date(2026, 11, 5), date(2026, 11, 5)), ("week", date(2026, 11, 5), date(2026, 11, 2)),
    ("month", date(2026, 11, 5), date(2026, 11, 1)), ("quarter", date(2026, 11, 5), date(2026, 10, 1))])
def test_the_period_still_filling(grain, day, start):
    from datetime import datetime, timezone
    ms = int(datetime(day.year, day.month, day.day, 13, tzinfo=timezone.utc).timestamp() * 1000)
    assert period_start(grain, ms) == start
