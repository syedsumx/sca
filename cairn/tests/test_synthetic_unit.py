"""Synthetic test data, without Trino: how a sensitive column's numbers are drawn from its statistics.
(The whole run, on real Trino and Cairn Catalog, is in tests/test_trino_e2e.py.)"""

from __future__ import annotations

import math
import random

from cairn.pipelines.synthetic import QUANTILES, Synthetic


def _quantiles(values: list[float]) -> list[float]:
    """Nearest-rank percentiles, as Trino's approx_percentile gives them on a small table."""
    s = sorted(values)
    return [s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))] for q in QUANTILES]


def _synthetic(min_count: int = 10) -> Synthetic:
    syn = Synthetic({}, lambda *a: True, lambda ns, t: f"{ns}.{t}", lambda *a: ([], [], False), "lake",
                    lambda u: set(), lambda a, u: None)
    syn.min_count = min_count
    return syn


def _profile(values: list[float]) -> dict:
    return {"kind": "float", "rows": len(values), "nonnull": len(values), "distinct": len(values), "hidden": True,
            "quantiles": _quantiles(values)}


def test_a_sensitive_columns_extremes_stay_hidden_in_a_small_table():
    """Review finding: a sensitive column's numbers were drawn between its 5th and 95th percentiles. In a table of
    fewer than 20 rows the 5th percentile is the smallest value itself, so the drawn values reached the extremes
    the doc says never appear: in 15 salaries, the lowest and the highest. Now at least CAIRN_SYNTHETIC_MIN_COUNT
    values of the source lie beyond the lowest and the highest drawn value, and a column too small for that gets no
    values from its numbers."""
    syn = _synthetic()
    salaries = [30000.0 + 1000 * i for i in range(15)]            # the lowest 30000, the highest 44000
    values, method = syn._column(random.Random(1), _profile(salaries), 2000, None)
    drawn = [v for v in values if v is not None]
    assert not drawn or (min(drawn) > salaries[0] + 500 and max(drawn) < salaries[-1] - 500), (method, min(drawn), max(drawn))

    # Enough rows: values come from the middle, with at least min_count source values below and above them.
    salaries = [30000.0 + 100 * i for i in range(60)]
    values, method = syn._column(random.Random(1), _profile(salaries), 2000, None)
    drawn = [v for v in values if v is not None]
    assert method == "shape" and drawn
    s = sorted(salaries)
    assert min(drawn) >= s[9] and max(drawn) <= s[-10], (min(drawn), max(drawn))

    # A large table keeps the 5th to 95th percentiles, as before.
    salaries = [30000.0 + i for i in range(1000)]
    values, _ = syn._column(random.Random(1), _profile(salaries), 2000, None)
    drawn = [v for v in values if v is not None]
    assert min(drawn) < 30000 + 0.07 * 1000 and max(drawn) > 30000 + 0.93 * 1000
