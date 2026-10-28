"""Anomaly detectors for the native data-quality engine.

All detectors are pure functions over plain Python numbers so they are easy to test,
reason about, and port. None of them pages during cold start: they return
``TRAINING`` until enough history exists, unless a hard threshold is configured.

Detectors
---------
* :func:`detect_static`: hard min/max bounds.
* :func:`detect_seasonal_robust_z`: median/MAD z-score with hour-of-week or
  day-of-week seasonality buckets (volume, custom metrics).
* :func:`detect_ewma`: exponentially weighted mean/variance band (field metrics).
* :func:`detect_learned`: trend-adjusted seasonal baseline that learns for a few weeks before it
  alerts, with automatic seasonality and a band people can widen or narrow.
* :func:`detect_freshness`: learned update cadence (p95 of inter-arrival gaps) or SLA.
* :func:`detect_distribution`: Population Stability Index + two-sample KS statistic.
* :func:`detect_schema_change`: breaking vs additive schema diffs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Literal, Optional, Sequence

Status = Literal["PASS", "WARN", "FAIL", "TRAINING", "ERROR"]
Sensitivity = Literal["low", "medium", "high"]

# z-score thresholds per sensitivity; WARN fires at 75% of the FAIL threshold.
Z_THRESHOLDS: dict[str, float] = {"low": 4.0, "medium": 3.0, "high": 2.5}
FRESHNESS_MULTIPLIERS: dict[str, float] = {"low": 3.0, "medium": 2.0, "high": 1.5}
WARN_FRACTION = 0.75
MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class Point:
    ts_ms: int
    value: float


@dataclass
class Detection:
    status: Status
    message: str
    expected_low: Optional[float] = None
    expected_high: Optional[float] = None
    score: Optional[float] = None
    details: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "FAIL"


def _merge_static(det: Detection, value: float, min_value: Optional[float], max_value: Optional[float]) -> Detection:
    """Hard thresholds always win over learned bands."""
    if min_value is not None and value < min_value:
        return Detection("FAIL", f"value {value:g} below hard minimum {min_value:g}",
                         min_value, max_value, det.score, det.details)
    if max_value is not None and value > max_value:
        return Detection("FAIL", f"value {value:g} above hard maximum {max_value:g}",
                         min_value, max_value, det.score, det.details)
    if det.status == "TRAINING" and (min_value is not None or max_value is not None):
        return Detection("PASS", f"value {value:g} within hard bounds (baseline training)",
                         min_value, max_value, None, det.details)
    return det


def detect_static(value: float, min_value: Optional[float] = None, max_value: Optional[float] = None) -> Detection:
    base = Detection("PASS", f"value {value:g} within bounds", min_value, max_value)
    return _merge_static(base, value, min_value, max_value)


# --------------------------------------------------------------------------- seasonal
def _season_key(ts_ms: int, seasonality: Optional[str]) -> Optional[int]:
    if seasonality is None:
        return None
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    if seasonality == "hour_of_week":
        return dt.weekday() * 24 + dt.hour
    if seasonality == "day_of_week":
        return dt.weekday()
    if seasonality == "hour_of_day":
        return dt.hour
    raise ValueError(f"unknown seasonality {seasonality!r}")


def _robust_center_scale(values: Sequence[float]) -> tuple[float, float]:
    med = median(values)
    mad = median([abs(v - med) for v in values])
    scale = mad * MAD_TO_SIGMA
    if scale == 0:
        # Constant series: allow a small tolerance so integer jitter is not an anomaly
        # but any real change is.
        scale = max(abs(med) * 0.001, 0.5)
    return med, scale


def detect_seasonal_robust_z(
    history: Sequence[Point],
    current: Point,
    sensitivity: Sensitivity = "medium",
    min_history: int = 14,
    seasonality: Optional[str] = "hour_of_week",
    min_bucket: int = 4,
    window: int = 90,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    max_change_pct: Optional[float] = None,
) -> Detection:
    recent = sorted(history, key=lambda p: p.ts_ms)[-window:]
    if max_change_pct is not None and recent:
        prev = recent[-1].value
        if prev != 0:
            change = abs(current.value - prev) / abs(prev) * 100
            if change > max_change_pct:
                return _merge_static(
                    Detection("FAIL", f"changed {change:.1f}% vs previous {prev:g} (limit {max_change_pct:g}%)",
                              score=change), current.value, min_value, max_value)
    if len(recent) < min_history:
        det = Detection("TRAINING", f"collecting baseline ({len(recent)}/{min_history} points)")
        return _merge_static(det, current.value, min_value, max_value)

    key = _season_key(current.ts_ms, seasonality)
    bucket = [p.value for p in recent if key is not None and _season_key(p.ts_ms, seasonality) == key]
    values = bucket if len(bucket) >= min_bucket else [p.value for p in recent]
    basis = "seasonal" if values is bucket else "trailing"
    center, scale = _robust_center_scale(values)
    z_fail = Z_THRESHOLDS[sensitivity]
    z = (current.value - center) / scale
    low, high = center - z_fail * scale, center + z_fail * scale
    if abs(z) > z_fail:
        status: Status = "FAIL"
    elif abs(z) > z_fail * WARN_FRACTION:
        status = "WARN"
    else:
        status = "PASS"
    det = Detection(
        status,
        f"value {current.value:g} vs {basis} baseline {center:g} (z={z:+.2f}, threshold ±{z_fail:g})",
        low, high, round(z, 4), {"basis": basis, "n": len(values)},
    )
    return _merge_static(det, current.value, min_value, max_value)


# --------------------------------------------------------------------------- learned
DAY_MS = 86_400_000
MAX_TREND_POINTS = 200


def theil_sen_slope(points: Sequence[Point]) -> float:
    """Median of pairwise slopes (value per ms): robust to outliers, unlike least squares."""
    pts = sorted(points, key=lambda p: p.ts_ms)
    if len(pts) > MAX_TREND_POINTS:   # evenly thinned: O(n²) pairs stay cheap
        step = len(pts) / MAX_TREND_POINTS
        pts = [pts[int(i * step)] for i in range(MAX_TREND_POINTS)]
    slopes = [(b.value - a.value) / (b.ts_ms - a.ts_ms)
              for i, a in enumerate(pts) for b in pts[i + 1:] if b.ts_ms > a.ts_ms]
    return median(slopes) if slopes else 0.0


def pick_seasonality(points: Sequence[Point], requested: Optional[str] = "auto") -> Optional[str]:
    """``auto`` picks the finest cycle the history can support: hour of week once there are two
    weeks of sub-daily points, hour of day after three days of them, day of week after three
    weeks of daily points; otherwise none."""
    if requested != "auto":
        return requested
    pts = sorted(p.ts_ms for p in points)
    if len(pts) < 2:
        return None
    span_days = (pts[-1] - pts[0]) / DAY_MS
    gap_h = median([b - a for a, b in zip(pts, pts[1:])]) / 3_600_000
    if gap_h <= 6 and span_days >= 14:
        return "hour_of_week"
    if gap_h <= 6 and span_days >= 3:
        return "hour_of_day"
    if span_days >= 21:
        return "day_of_week"
    return None


def detect_learned(
    history: Sequence[Point],
    current: Point,
    sensitivity: Sensitivity = "medium",
    band: Optional[float] = None,
    learn_days: float = 21,
    observed_since_ms: Optional[int] = None,
    min_points: int = 14,
    seasonality: Optional[str] = "auto",
    trend: bool = True,
    min_bucket: int = 3,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> Detection:
    """Learned threshold: stays quiet while it learns, then alerts outside
    ``center ± band × robust scale`` where the center follows the trend and the season.

    ``band`` (in robust standard deviations) overrides the sensitivity preset. ``observed_since_ms``
    is when the monitor first saw data, so a baseline reset after a "new normal" needs only
    ``min_points`` fresh points rather than another learning period.
    """
    ordered = sorted(history, key=lambda p: p.ts_ms)
    since = observed_since_ms if observed_since_ms is not None else (ordered[0].ts_ms if ordered else current.ts_ms)
    days = max(0.0, (current.ts_ms - since) / DAY_MS)
    if days < learn_days or len(ordered) < min_points:
        det = Detection("TRAINING", f"learning normal behaviour ({min(days, learn_days):.0f}/{learn_days:g} days, "
                        f"{len(ordered)}/{min_points} points)", details={"model": "learned", "days": round(days, 2)})
        return _merge_static(det, current.value, min_value, max_value)
    slope = theil_sen_slope(ordered) if trend else 0.0
    # Project every past point to "now" along the trend, so growth is not mistaken for an anomaly.
    projected = [Point(p.ts_ms, p.value + slope * (current.ts_ms - p.ts_ms)) for p in ordered]
    season = pick_seasonality(ordered, seasonality)
    key = _season_key(current.ts_ms, season)
    bucket = [p.value for p in projected if key is not None and _season_key(p.ts_ms, season) == key]
    values = bucket if len(bucket) >= min_bucket else [p.value for p in projected]
    basis = season if values is bucket else "all"
    center, scale = _robust_center_scale(values)
    width = float(band) if band else Z_THRESHOLDS[sensitivity]
    z = (current.value - center) / scale
    low, high = center - width * scale, center + width * scale
    status: Status = "FAIL" if abs(z) > width else "WARN" if abs(z) > width * WARN_FRACTION else "PASS"
    trend_per_day = slope * DAY_MS
    msg = f"value {current.value:g}, expected {low:g} – {high:g} ({basis} baseline"
    msg += f", trend {trend_per_day:+.3g}/day)" if trend and abs(trend_per_day) > 0 else ")"
    det = Detection(status, msg, low, high, round(z, 4),
                    {"model": "learned", "basis": basis, "seasonality": season, "n": len(values),
                     "trend_per_day": round(trend_per_day, 6), "band": width, "center": center})
    return _merge_static(det, current.value, min_value, max_value)


# --------------------------------------------------------------------------- EWMA
def detect_ewma(
    history: Sequence[Point],
    current: Point,
    sensitivity: Sensitivity = "medium",
    alpha: float = 0.3,
    min_history: int = 10,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> Detection:
    ordered = sorted(history, key=lambda p: p.ts_ms)
    if len(ordered) < min_history:
        det = Detection("TRAINING", f"collecting baseline ({len(ordered)}/{min_history} points)")
        return _merge_static(det, current.value, min_value, max_value)
    mean = ordered[0].value
    var = 0.0
    for p in ordered[1:]:
        diff = p.value - mean
        incr = alpha * diff
        mean += incr
        var = (1 - alpha) * (var + diff * incr)
    std = math.sqrt(var)
    if std == 0:
        std = max(abs(mean) * 0.001, 1e-9)
    z_fail = Z_THRESHOLDS[sensitivity]
    z = (current.value - mean) / std
    status: Status = "FAIL" if abs(z) > z_fail else "WARN" if abs(z) > z_fail * WARN_FRACTION else "PASS"
    det = Detection(status, f"value {current.value:g} vs EWMA {mean:g}±{std:g} (z={z:+.2f})",
                    mean - z_fail * std, mean + z_fail * std, round(z, 4))
    return _merge_static(det, current.value, min_value, max_value)


# --------------------------------------------------------------------------- freshness
def _quantile(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    if not s:
        raise ValueError("empty")
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def detect_freshness(
    update_times_ms: Sequence[int],
    now_ms: int,
    sensitivity: Sensitivity = "medium",
    sla_seconds: Optional[float] = None,
    min_history: int = 5,
) -> Detection:
    """``update_times_ms``: observed data-update timestamps (any order, duplicates ok)."""
    updates = sorted(set(update_times_ms))
    if not updates:
        if sla_seconds is not None:
            return Detection("FAIL", "no update has ever been observed", None, sla_seconds)
        return Detection("TRAINING", "no updates observed yet")
    age_s = max(0.0, (now_ms - updates[-1]) / 1000)
    gaps = [(b - a) / 1000 for a, b in zip(updates, updates[1:]) if b > a]
    if sla_seconds is not None:
        threshold = float(sla_seconds)
        basis = "sla"
    elif len(gaps) >= min_history:
        threshold = _quantile(gaps, 0.95) * FRESHNESS_MULTIPLIERS[sensitivity]
        basis = "learned"
    else:
        return Detection("TRAINING", f"learning update cadence ({len(gaps)}/{min_history} intervals); "
                         f"last update {age_s:.0f}s ago", score=age_s)
    ratio = age_s / threshold if threshold > 0 else math.inf
    status: Status = "FAIL" if ratio > 1 else "WARN" if ratio > 0.8 else "PASS"
    return Detection(
        status,
        f"last update {age_s:.0f}s ago; {basis} threshold {threshold:.0f}s",
        0.0, threshold, round(ratio, 4),
        {"basis": basis, "age_seconds": age_s, "last_update_ms": updates[-1]},
    )


# --------------------------------------------------------------------------- distribution
def population_stability_index(reference: dict[str, float], current: dict[str, float], eps: float = 1e-4) -> float:
    keys = set(reference) | set(current)
    ref_total = sum(reference.values()) or 1.0
    cur_total = sum(current.values()) or 1.0
    psi = 0.0
    for k in keys:
        r = max(reference.get(k, 0.0) / ref_total, eps)
        c = max(current.get(k, 0.0) / cur_total, eps)
        psi += (c - r) * math.log(c / r)
    return psi


def ks_statistic(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Two-sample Kolmogorov–Smirnov statistic D and asymptotic p-value."""
    if not a or not b:
        raise ValueError("samples must be non-empty")
    xs, ys = sorted(a), sorted(b)
    n, m = len(xs), len(ys)
    i = j = 0
    d = 0.0
    while i < n and j < m:
        v = min(xs[i], ys[j])
        while i < n and xs[i] <= v:
            i += 1
        while j < m and ys[j] <= v:
            j += 1
        d = max(d, abs(i / n - j / m))
    en = math.sqrt(n * m / (n + m))
    lam = (en + 0.12 + 0.11 / en) * d
    # Kolmogorov distribution tail: Q(λ) = 2 Σ (-1)^{k-1} e^{-2k²λ²}
    p = 0.0
    for k in range(1, 101):
        term = 2 * (-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam)
        p += term
        if abs(term) < 1e-10:
            break
    return d, min(max(p, 0.0), 1.0)


def histogram(values: Sequence[float], edges: Sequence[float]) -> dict[str, float]:
    """Bucket numeric values into ``len(edges)+1`` buckets keyed by bucket index."""
    counts = {str(i): 0.0 for i in range(len(edges) + 1)}
    for v in values:
        idx = 0
        while idx < len(edges) and v > edges[idx]:
            idx += 1
        counts[str(idx)] += 1
    return counts


def quantile_edges(values: Sequence[float], buckets: int = 10) -> list[float]:
    if not values:
        return []
    return sorted({_quantile(values, i / buckets) for i in range(1, buckets)})


def detect_distribution(
    reference: dict[str, float],
    current: dict[str, float],
    psi_warn: float = 0.2,
    psi_fail: float = 0.3,
    reference_samples: Optional[Sequence[float]] = None,
    current_samples: Optional[Sequence[float]] = None,
    ks_alpha: float = 0.01,
) -> Detection:
    if not reference:
        return Detection("TRAINING", "no reference distribution yet; current snapshot stored as reference")
    psi = population_stability_index(reference, current)
    details: dict = {"psi": round(psi, 6)}
    status: Status = "FAIL" if psi > psi_fail else "WARN" if psi > psi_warn else "PASS"
    msg = f"PSI={psi:.4f} (warn>{psi_warn:g}, fail>{psi_fail:g})"
    if reference_samples and current_samples:
        d, p = ks_statistic(reference_samples, current_samples)
        details.update({"ks_d": round(d, 6), "ks_p": p})
        msg += f"; KS D={d:.4f} p={p:.3g}"
        if p < ks_alpha and status == "PASS":
            status = "WARN"
    # Surface the categories that moved most, for the UI and incident text.
    ref_t = sum(reference.values()) or 1.0
    cur_t = sum(current.values()) or 1.0
    movers = sorted(
        ((k, current.get(k, 0) / cur_t - reference.get(k, 0) / ref_t) for k in set(reference) | set(current)),
        key=lambda kv: -abs(kv[1]),
    )[:5]
    details["top_shifts"] = [{"bucket": k, "delta_share": round(v, 4)} for k, v in movers]
    return Detection(status, msg, 0.0, psi_fail, round(psi, 6), details)


# --------------------------------------------------------------------------- schema
def detect_schema_change(
    baseline: dict[str, str], current: dict[str, str], allow_additions: bool = True
) -> Detection:
    """``baseline``/``current`` map fieldPath → native type."""
    if not baseline:
        return Detection("TRAINING", "baseline schema captured")
    removed = sorted(set(baseline) - set(current))
    added = sorted(set(current) - set(baseline))
    changed = sorted(f for f in set(baseline) & set(current) if baseline[f] != current[f])
    details = {"removed": removed, "added": added, "type_changed": changed}
    if removed or changed:
        return Detection("FAIL", f"breaking schema change: removed={removed} type_changed={changed}",
                         score=float(len(removed) + len(changed)), details=details)
    if added and not allow_additions:
        return Detection("FAIL", f"columns added: {added}", score=float(len(added)), details=details)
    if added:
        return Detection("WARN", f"non-breaking: columns added {added}", score=float(len(added)), details=details)
    return Detection("PASS", "schema unchanged", details=details)
