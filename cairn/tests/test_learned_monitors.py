"""Learned thresholds: a learning period, trend and seasonality, an adjustable band, and a
baseline reset when someone marks a step change as the new normal."""

import math

import pytest

from cairn.quality.detectors import DAY_MS, Point, detect_learned, pick_seasonality, theil_sen_slope
from cairn.quality.models import MonitorSpec

from conftest import DS
from test_quality_engine import _engine, _fill, warehouse  # noqa: F401  (fixture)

HOUR = 3_600_000
T0 = 1_760_000_000_000


def growing(days: int, start: float = 10_000, per_day: float = 500, noise: float = 30) -> list[Point]:
    """Hourly row counts that grow every day and swing with the hour (busier in the afternoon)."""
    pts = []
    for h in range(days * 24):
        daily = 200 * math.sin(2 * math.pi * ((h % 24) - 6) / 24)
        jitter = noise * math.sin(h * 2.17)
        pts.append(Point(T0 + h * HOUR, start + per_day * h / 24 + daily + jitter))
    return pts


def test_silent_while_learning_even_with_many_points():
    hist = growing(10)
    det = detect_learned(hist, Point(hist[-1].ts_ms + HOUR, 0))
    assert det.status == "TRAINING" and "10/21 days" in det.message
    # hard bounds still apply while it learns
    assert detect_learned(hist, Point(hist[-1].ts_ms + HOUR, 0), min_value=1).status == "FAIL"


def test_growth_and_daily_swing_are_normal_but_a_drop_is_not():
    hist = growing(28)
    nxt = T0 + len(hist) * HOUR
    h = len(hist)
    normal = 10_000 + 500 * h / 24 + 200 * math.sin(2 * math.pi * ((h % 24) - 6) / 24)
    det = detect_learned(hist, Point(nxt, normal))
    assert det.status == "PASS", det.message
    assert det.details["seasonality"] == "hour_of_week" and det.details["trend_per_day"] == pytest.approx(500, rel=0.05)
    assert det.expected_low < normal < det.expected_high
    assert detect_learned(hist, Point(nxt, normal * 0.9)).status == "FAIL"
    # without the trend the band has to stretch over a month of growth, and goes blind
    flat = detect_learned(hist, Point(nxt, normal), trend=False)
    assert det.expected_high - det.expected_low < 1_000 < 10_000 < flat.expected_high - flat.expected_low
    assert detect_learned(hist, Point(nxt, normal * 0.9), trend=False).status == "PASS"


def test_band_widens_and_narrows():
    hist = growing(28)
    nxt = T0 + len(hist) * HOUR
    base = detect_learned(hist, Point(nxt, 0))
    center, scale = base.details["center"], (base.expected_high - base.details["center"]) / 3.0
    value = center + 4 * scale                                    # 4 robust SDs out
    assert detect_learned(hist, Point(nxt, value)).status == "FAIL"           # default band 3
    assert detect_learned(hist, Point(nxt, value), band=6).status == "PASS"
    assert detect_learned(hist, Point(nxt, center + 1.8 * scale), band=2).status == "WARN"


def test_seasonality_is_picked_from_the_history():
    assert pick_seasonality(growing(15)) == "hour_of_week"
    assert pick_seasonality(growing(5)) == "hour_of_day"
    daily = [Point(T0 + d * DAY_MS, 1.0) for d in range(30)]
    assert pick_seasonality(daily) == "day_of_week"
    assert pick_seasonality(daily[:10]) is None
    assert pick_seasonality(daily, "hour_of_day") == "hour_of_day"
    assert theil_sen_slope([Point(0, 0), Point(1, 1), Point(2, 2), Point(3, 3), Point(4, 100)]) == pytest.approx(1.0)


def test_spec_validation_for_learned():
    MonitorSpec(name="v", dataset_urn=DS, type="volume", config={"model": "learned", "band": 4})
    for cfg, kind in (({"model": "magic"}, "volume"), ({"model": "learned", "band": 0.5}, "volume"),
                      ({"model": "learned"}, "freshness"), ({"model": "learned", "seasonality": "yearly"}, "volume")):
        with pytest.raises(ValueError):
            MonitorSpec(name="v", dataset_urn=DS, type=kind, config=cfg)


def test_new_normal_resets_the_baseline_without_another_learning_period(engine, graph, warehouse):  # noqa: F811
    url, wh = warehouse
    qe = _engine(engine, graph, url)
    m = qe.store.create_monitor(MonitorSpec(name="v", dataset_urn=DS, type="volume", config={
        "source": "sql", "connection": "wh", "table": "orders", "model": "learned", "seasonality": None}), None)
    for i in range(22 * 24):                       # 22 days of ~100 rows
        qe.store.add_point(m.id, T0 + i * HOUR, 100 + (i % 3))
    now = T0 + 22 * 24 * HOUR
    _fill(wh, 300)                                 # the table tripled on purpose
    jump = qe.evaluate(qe.store.get_monitor(m.id), now=now)
    assert jump.status == "FAIL" and jump.details["model"] == "learned"
    qe.store.set_feedback(jump.id, "NEW_NORMAL")
    assert len(qe.store.history(m.id)) == 1 and qe.store.get_evaluation(jump.id).feedback == "NEW_NORMAL"
    for i in range(1, 14):
        _fill(wh, 300 + (i % 3))
        ev = qe.evaluate(qe.store.get_monitor(m.id), now=now + i * HOUR)
    assert ev.status == "TRAINING" and "14 points" in ev.message   # needs points, not another 3 weeks
    _fill(wh, 301)
    assert qe.evaluate(qe.store.get_monitor(m.id), now=now + 14 * HOUR).status == "PASS"
    _fill(wh, 100)
    assert qe.evaluate(qe.store.get_monitor(m.id), now=now + 15 * HOUR).status == "FAIL"
