"""Side by side: the same question asked of the source warehouse and of Cairn, and the answers compared (L170).

``compare(a, b)`` takes two results (columns and rows) and says whether they hold the same rows: as multisets unless
``ordered``, numbers within ``tolerance`` of each other, dates and times by their instant, column names ignored (the
positions decide; a difference in names is reported, not failed). What differs is shown as counts and a few rows from
each side only, never the whole result.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
from collections import Counter
from typing import Any

SHOWN = 5


def _norm(v: Any, digits: int) -> Any:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float, decimal.Decimal)):
        f = float(v)
        if math.isnan(f):
            return "NaN"
        if f == int(f) and abs(f) < 1e15:
            return int(f)
        return float(f"{f:.{digits}g}")
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return v.isoformat(sep=" ", timespec="microseconds").removesuffix(".000000").removesuffix(" 00:00:00")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return tuple(_norm(x, digits) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((str(k), _norm(x, digits)) for k, x in v.items()))
    s = str(v)
    # A warehouse's API may hand dates, times and numbers back as text: read them as what they are.
    if len(s) >= 19 and s[4:5] == "-" and s[10:11] in (" ", "T"):
        try:
            return _norm(dt.datetime.fromisoformat(s.replace("Z", "+00:00")), digits)
        except ValueError:
            pass
    return s


def compare(a_columns: list, a_rows: list, b_columns: list, b_rows: list, ordered: bool = False,
            significant_digits: int = 12) -> dict:
    """``a``: the source's answer; ``b``: Cairn's."""
    out: dict = {"match": False, "source_rows": len(a_rows), "target_rows": len(b_rows), "differences": []}
    if len(a_columns) != len(b_columns):
        out["differences"].append(f"the source returns {len(a_columns)} columns, Cairn {len(b_columns)}")
        return out
    names = [(str(x), str(y)) for x, y in zip(a_columns, b_columns) if str(x).lower() != str(y).lower()]
    if names:
        out["renamed_columns"] = [f"{x} → {y}" for x, y in names]
    a = [tuple(_norm(v, significant_digits) for v in r) for r in a_rows]
    b = [tuple(_norm(v, significant_digits) for v in r) for r in b_rows]
    if len(a) != len(b):
        out["differences"].append(f"the source returns {len(a)} rows, Cairn {len(b)}")
    if ordered:
        wrong = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if wrong:
            out["differences"].append(f"{len(wrong)} rows differ, the first at row {wrong[0] + 1}")
            out["only_in_source"] = [list(map(str, a[i])) for i in wrong[:SHOWN]]
            out["only_in_target"] = [list(map(str, b[i])) for i in wrong[:SHOWN]]
    else:
        ca, cb = Counter(map(repr, a)), Counter(map(repr, b))
        only_a, only_b = ca - cb, cb - ca
        if only_a or only_b:
            out["differences"].append(f"{sum(only_a.values())} rows are only in the source's answer, "
                                      f"{sum(only_b.values())} only in Cairn's")
            out["only_in_source"] = list(only_a)[:SHOWN]
            out["only_in_target"] = list(only_b)[:SHOWN]
    out["match"] = not out["differences"]
    return out
