"""Metric alert rules (L53): a change on the period before, a usual range, a threshold; the period still filling is
never judged."""

from datetime import date, datetime, timezone

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


class _Metrics:
    """Revenue per week as each person sees it (the owner all regions, Bob only his own; Eve none)."""

    def __init__(self, seen):
        self.seen = seen

    def compile(self, actor, query):
        return {}, []

    def query(self, actor, query):
        from cairn.sqlworkspace.metrics import PERIOD, MetricError

        if actor not in self.seen:
            raise MetricError("access denied", 403)
        return {"columns": [PERIOD, "revenue"], "rows": [[d.isoformat(), v] for d, v in zip(WEEKS, self.seen[actor])]}


def _alerts(seen):
    from sqlalchemy import create_engine

    from cairn.common.db import metadata
    from cairn.sqlworkspace.metric_alerts import MetricAlerts

    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[metadata.tables["cairn_metric_alerts"]])
    told = []
    return MetricAlerts(engine, _Metrics(seen), lambda k, t, b, who, p: told.append((k, b, who, p))), told


def test_recipients_are_told_only_what_the_metric_shows_them():
    owner, bob, eve = (f"urn:li:corpuser:{n}" for n in ("owner", "bob", "eve"))
    ma, told = _alerts({owner: [100, 100, 100, 100, 1000, 500], bob: [10, 10, 10, 10, 10, 10]})
    a = ma.save(owner, {"name": "rev", "metric": "revenue", "grain": "week", "recipients": [bob, eve],
                        "rule": {"kind": "change", "drop_pct": 20}})
    assert ma.check(a["id"], now=int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000))["state"] == "triggered"
    assert [who for _, _, who, _ in told] == [[owner]]                 # Bob's own revenue did not drop; Eve sees none
    assert "1,000.00" in told[0][1] and told[0][3]["value"] == 500


def test_a_recipient_whose_view_also_trips_gets_their_own_value():
    owner, bob = "urn:li:corpuser:owner", "urn:li:corpuser:bob"
    ma, told = _alerts({owner: [100, 100, 100, 100, 1000, 500], bob: [10, 10, 10, 10, 10, 5]})
    a = ma.save(owner, {"name": "rev", "metric": "revenue", "grain": "week", "recipients": [bob],
                        "rule": {"kind": "change", "drop_pct": 20}})
    ma.check(a["id"], now=int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000))
    mine = [(b, p) for _, b, who, p in told if who == [bob]]
    assert len(mine) == 1 and mine[0][1]["value"] == 5 and "1,000" not in mine[0][0]


@pytest.mark.parametrize("value", ["nan", "inf", float("-inf")])
def test_a_threshold_is_a_finite_number(value):
    from cairn.sqlworkspace.metric_alerts import MetricAlertError

    ma, _ = _alerts({})
    with pytest.raises(MetricAlertError):
        ma.save("urn:li:corpuser:owner", {"name": "x", "metric": "revenue", "rule": {"kind": "threshold", "op": "<",
                                                                                     "value": value}})
