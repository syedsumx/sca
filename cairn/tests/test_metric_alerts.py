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


def test_a_recipient_is_told_only_their_own_number_never_the_owners(tmp_path):
    """The owner sees every region; Bob (a recipient) sees only the EU, and Carol may not read the metric. The
    owner's drop is told to the owner with their number; Bob is told only if his own number drops, with his number;
    Carol is told nothing."""
    from datetime import datetime, timezone

    from sqlalchemy import create_engine

    from cairn.common.db import metadata
    from cairn.sqlworkspace.metric_alerts import MetricAlerts, metric_alerts
    from cairn.sqlworkspace.metrics import MetricError

    owner, bob, carol = (f"urn:li:corpuser:{n}" for n in ("owner", "bob", "carol"))
    weeks = {owner: [1000.0, 700.0], bob: [100.0, 99.0]}

    class Metrics:
        def compile(self, actor, query):
            return {}, []

        def query(self, actor, query):
            if actor not in weeks:
                raise MetricError("no access", 403)
            return {"columns": ["period", "revenue"],
                    "rows": [[d.isoformat(), v] for d, v in zip(WEEKS[-3:-1], weeks[actor])]}

    told = []
    engine = create_engine(f"sqlite:///{tmp_path}/a.db")
    metadata.create_all(engine, tables=[metric_alerts])
    ma = MetricAlerts(engine, Metrics(), lambda kind, title, body, who, payload: told.append((kind, body, who, payload)))
    a = ma.save(owner, {"name": "Revenue", "metric": "revenue", "grain": "week", "rule": {"kind": "change", "drop_pct": 20},
                        "recipients": [bob, carol]})
    now = int(datetime(2026, 10, 14, 12, tzinfo=timezone.utc).timestamp() * 1000)
    assert ma.check(a["id"], now)["state"] == "triggered"
    to = {tuple(w): (body, p["value"]) for _, body, w, p in told}
    assert set(to) == {(owner,)}, told                 # Bob's own EU number did not drop; Carol reads nothing
    assert to[(owner,)][1] == 700.0
    # Bob's own number drops too: he is told, with his number, never the owner's.
    told.clear()
    weeks[bob] = [100.0, 50.0]
    with engine.begin() as conn:
        conn.execute(metric_alerts.update().values(state="ok"))
    ma.check(a["id"], now)
    to = {tuple(w): (body, p["value"]) for _, body, w, p in told}
    assert set(to) == {(owner,), (bob,)}
    assert to[(bob,)][1] == 50.0 and "700" not in to[(bob,)][0] and "1,000" not in to[(bob,)][0]


def test_a_suspended_owners_metric_alert_runs_nothing(tmp_path):
    """The metric runs as the alert's owner: once the owner is suspended (or Cairn cannot tell), a check reads
    nothing as them; the alert is in error with the reason and no recipient is told."""
    from datetime import datetime, timezone

    from sqlalchemy import create_engine

    from cairn.common.db import metadata
    from cairn.sqlworkspace.metric_alerts import MetricAlerts, metric_alerts

    owner, bob = "urn:li:corpuser:owner", "urn:li:corpuser:bob"
    ran = []

    class Metrics:
        def compile(self, actor, query):
            return {}, []

        def query(self, actor, query):
            ran.append(actor)
            return {"columns": ["period", "revenue"], "rows": [[d.isoformat(), v] for d, v in zip(WEEKS[-3:-1], [1000.0, 700.0])]}

    status = {owner: None}

    def active(who):
        if status[who] == "boom":
            raise RuntimeError("graph down")
        return status[who]
    told = []
    engine = create_engine(f"sqlite:///{tmp_path}/a.db")
    metadata.create_all(engine, tables=[metric_alerts])
    ma = MetricAlerts(engine, Metrics(), lambda kind, title, body, who, payload: told.append((kind, body, who)),
                      active=active)
    a = ma.save(owner, {"name": "Revenue", "metric": "revenue", "grain": "week", "rule": {"kind": "change", "drop_pct": 20},
                        "recipients": [bob]})
    now = int(datetime(2026, 10, 14, 12, tzinfo=timezone.utc).timestamp() * 1000)
    for why in (f"{owner} is suspended", "boom"):
        status[owner] = why
        ran.clear()
        told.clear()
        with engine.begin() as conn:
            conn.execute(metric_alerts.update().values(state="ok"))
        st = ma.check(a["id"], now)
        assert st["state"] == "error" and "may not run alerts" in st["last_message"] and ran == []
        assert all(bob not in who for _, _, who in told)


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
