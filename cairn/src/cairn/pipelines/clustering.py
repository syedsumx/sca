"""Automatic clustering: a lakehouse table's data kept sorted by the columns its queries filter on, so Trino
skips the files a query cannot need.

Table maintenance (``cairn.pipelines.maintenance``) runs this for tables whose policy has ``cluster``:

* **Which columns.** ``"auto"``: the columns that the table's queries of the last
  ``CAIRN_CLUSTER_LOOKBACK_DAYS`` (default 14) filter or join on, from Trino's query history (each query's
  SQL, parsed), weighted by the bytes the query read. A column counts when at least 10% of the table's
  queries (and two) use it; the first two by weight are chosen, in that order. Or an explicit list.
  Types that do not sort usefully (booleans, binary, arrays, maps, rows, JSON) are never chosen.
* **The sort order** is set on the Iceberg table (``sorted_by``): every later write through Trino sorts
  its files by it.
* **Clustering depth**: how many files, on average, a file's range of the first column overlaps (itself
  included), from the files' column bounds (``$files``). 1 is perfectly clustered; a table whose files all
  span every value has a depth equal to its file count. When the sort order changed, or the depth is above
  the policy's ``cluster_depth`` (default 4), the table's files are rewritten in order (``optimize`` over
  every file). Each step and the depth before and after are recorded.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable, Optional

UNSORTABLE = ("boolean", "varbinary", "array", "map", "row", "json", "uuid")
MIN_SHARE = 0.10
MAX_COLUMNS = 2


def filter_columns(sql: str, schema: str, table: str, columns: Iterable[str]) -> set[str]:
    """The columns of ``schema.table`` that ``sql`` filters or joins on (WHERE, JOIN ... ON, HAVING)."""
    import sqlglot
    from sqlglot import exp

    cols = {c.lower() for c in columns}
    try:
        trees = sqlglot.parse(sql, read="trino")
    except Exception:  # noqa: BLE001 - a statement sqlglot cannot read tells nothing
        return set()
    out: set[str] = set()
    for tree in trees:
        if tree is None:
            continue
        for select in tree.find_all(exp.Select):
            sources: dict[str, bool] = {}           # alias or name -> is it our table
            for t in select.find_all(exp.Table):
                if t.find_ancestor(exp.Select) is not select:
                    continue
                ours = t.name.lower() == table.lower() and (not t.db or t.db.lower() == schema.lower())
                sources[(t.alias_or_name or t.name).lower()] = ours
                sources[t.name.lower()] = sources.get(t.name.lower(), False) or ours
            if not any(sources.values()):
                continue
            only_ours = all(sources.values())            # an unqualified column is ours only if nothing else is read
            preds = [select.args.get("where"), select.args.get("having")]
            preds += [j.args.get("on") for j in select.args.get("joins") or []]
            for pred in preds:
                if pred is None:
                    continue
                for c in pred.find_all(exp.Column):
                    if c.find_ancestor(exp.Select) is not select or c.find_ancestor(exp.Subquery):
                        continue
                    name = c.name.lower()
                    if name not in cols:
                        continue
                    q = (c.table or "").lower()
                    if (q and sources.get(q)) or (not q and only_ours):
                        out.add(name)
    return out


def choose(queries: list[tuple[str, int]], schema: str, table: str, types: dict[str, str]) -> tuple[list[str], dict]:
    """The columns to cluster by, from ``(sql, bytes read)`` of the table's queries, and the evidence."""
    sortable = {c for c, t in types.items() if not t.lower().startswith(UNSORTABLE)}
    used: dict[str, int] = defaultdict(int)
    weight: dict[str, int] = defaultdict(int)
    for sql, read in queries:
        for c in filter_columns(sql, schema, table, sortable):
            used[c] += 1
            weight[c] += max(1, int(read or 0))
    floor = max(2, int(len(queries) * MIN_SHARE + 0.999))
    ranked = sorted((c for c in used if used[c] >= floor), key=lambda c: (-weight[c], -used[c], c))
    return ranked[:MAX_COLUMNS], {"queries": len(queries), "filters": dict(used), "bytes": dict(weight)}


def depth(files: list[dict], column: str) -> Optional[float]:
    """Average overlap of each file's [lower, upper] range of ``column`` with the others (itself included)."""
    ranges = []
    for f in files:
        m = f.get("readable_metrics")
        m = json.loads(m) if isinstance(m, str) else (m or {})
        c = m.get(column) or {}
        lo, hi = c.get("lower_bound"), c.get("upper_bound")
        if lo is None or hi is None:
            continue
        ranges.append((lo, hi))
    if not ranges:
        return None
    try:
        total = sum(sum(1 for a, b in ranges if not (b < lo or a > hi)) for lo, hi in ranges)
    except TypeError:                                   # bounds of different kinds: nothing to compare
        return None
    return round(total / len(ranges), 2)


def sort_columns(sort_order: Optional[str]) -> list[str]:
    """``id ASC NULLS FIRST, day DESC NULLS LAST`` -> ``["id", "day"]``."""
    if not sort_order:
        return []
    return [part.strip().split(" ")[0].strip('"').lower() for part in sort_order.split(",") if part.strip()]


def as_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)
