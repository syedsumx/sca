import random

import pytest

from datahub_enterprise.quality import detectors as d

HOUR = 3_600_000
T0 = 1_760_000_000_000  # fixed epoch for deterministic seasonality


def hourly(values, start=T0):
    return [d.Point(start + i * HOUR, float(v)) for i, v in enumerate(values)]


def test_static_bounds():
    assert d.detect_static(5, 1, 10).status == "PASS"
    assert d.detect_static(0, 1, 10).status == "FAIL"
    assert d.detect_static(11, None, 10).status == "FAIL"


def test_seasonal_training_then_detects_spike():
    rng = random.Random(7)
    hist = hourly([1000 + rng.randint(-20, 20) for _ in range(30)])
    assert d.detect_seasonal_robust_z(hist[:5], d.Point(T0 + 5 * HOUR, 1000)).status == "TRAINING"
    normal = d.detect_seasonal_robust_z(hist, d.Point(T0 + 30 * HOUR, 1005), seasonality=None)
    assert normal.status == "PASS"
    assert normal.expected_low < 1005 < normal.expected_high
    spike = d.detect_seasonal_robust_z(hist, d.Point(T0 + 30 * HOUR, 5000), seasonality=None)
    assert spike.status == "FAIL" and spike.score > 3


def test_seasonality_uses_matching_bucket():
    # Weekly pattern: hour 0 of each day is 10x larger. Using day-of-week buckets, a
    # big value at the busy slot is normal, but the same value at a quiet slot is not.
    pts = []
    for day in range(28):
        for hour in range(24):
            ts = T0 + (day * 24 + hour) * HOUR
            v = 10_000 if hour == 0 else 1000
            pts.append(d.Point(ts, v + (day % 3)))
    busy_ts = T0 + 28 * 24 * HOUR  # hour 0 again
    det = d.detect_seasonal_robust_z(pts, d.Point(busy_ts, 10_001), seasonality="hour_of_week", window=24 * 28)
    assert det.status == "PASS", det.message
    quiet = d.detect_seasonal_robust_z(pts, d.Point(busy_ts + 5 * HOUR, 10_001), seasonality="hour_of_week",
                                       window=24 * 28)
    assert quiet.status == "FAIL"


def test_hard_threshold_overrides_training():
    det = d.detect_seasonal_robust_z([], d.Point(T0, 10), min_value=100)
    assert det.status == "FAIL"
    ok = d.detect_seasonal_robust_z([], d.Point(T0, 500), min_value=100)
    assert ok.status == "PASS"


def test_max_change_pct():
    hist = hourly([100] * 20)
    det = d.detect_seasonal_robust_z(hist, d.Point(T0 + 21 * HOUR, 40), max_change_pct=50)
    assert det.status == "FAIL" and "changed" in det.message


def test_ewma():
    hist = hourly([5.0, 5.1, 4.9, 5.0, 5.2, 4.8, 5.0, 5.1, 4.9, 5.0, 5.05])
    assert d.detect_ewma(hist, d.Point(T0 + 12 * HOUR, 5.02)).status == "PASS"
    assert d.detect_ewma(hist, d.Point(T0 + 12 * HOUR, 9.0)).status == "FAIL"


def test_freshness_learned_and_sla():
    updates = [T0 + i * HOUR for i in range(10)]
    on_time = d.detect_freshness(updates, T0 + 9 * HOUR + 30 * 60_000)
    assert on_time.status == "PASS"
    late = d.detect_freshness(updates, T0 + 9 * HOUR + 3 * HOUR)
    assert late.status == "FAIL" and late.details["basis"] == "learned"
    assert d.detect_freshness(updates[:3], T0 + 100 * HOUR).status == "TRAINING"
    sla = d.detect_freshness(updates[:2], T0 + 3 * HOUR, sla_seconds=3600)
    assert sla.status == "FAIL" and sla.details["basis"] == "sla"
    assert d.detect_freshness([], T0, sla_seconds=60).status == "FAIL"


def test_psi_and_distribution():
    ref = {"card": 700, "paypal": 200, "bank": 100}
    assert d.population_stability_index(ref, ref) == pytest.approx(0.0, abs=1e-9)
    same = d.detect_distribution(ref, {"card": 690, "paypal": 210, "bank": 100})
    assert same.status == "PASS"
    shifted = d.detect_distribution(ref, {"card": 200, "paypal": 200, "bank": 600})
    assert shifted.status == "FAIL"
    assert shifted.details["top_shifts"][0]["bucket"] in ("bank", "card")
    assert d.detect_distribution({}, ref).status == "TRAINING"


def test_ks_statistic():
    rng = random.Random(1)
    a = [rng.gauss(0, 1) for _ in range(500)]
    b = [rng.gauss(0, 1) for _ in range(500)]
    c = [rng.gauss(1.0, 1) for _ in range(500)]
    d_same, p_same = d.ks_statistic(a, b)
    d_diff, p_diff = d.ks_statistic(a, c)
    assert p_same > 0.01 and p_diff < 1e-6 and d_diff > d_same


def test_numeric_histogram_drift():
    rng = random.Random(3)
    ref = [rng.gauss(50, 5) for _ in range(2000)]
    cur = [rng.gauss(65, 5) for _ in range(2000)]
    edges = d.quantile_edges(ref, 10)
    det = d.detect_distribution(d.histogram(ref, edges), d.histogram(cur, edges),
                                reference_samples=ref, current_samples=cur)
    assert det.status == "FAIL" and det.details["ks_p"] < 0.001


def test_schema_change():
    base = {"id": "INT", "email": "VARCHAR"}
    assert d.detect_schema_change({}, base).status == "TRAINING"
    assert d.detect_schema_change(base, base).status == "PASS"
    assert d.detect_schema_change(base, {**base, "x": "INT"}).status == "WARN"
    assert d.detect_schema_change(base, {**base, "x": "INT"}, allow_additions=False).status == "FAIL"
    assert d.detect_schema_change(base, {"id": "BIGINT", "email": "VARCHAR"}).status == "FAIL"
    assert d.detect_schema_change(base, {"id": "INT"}).status == "FAIL"
