"""Synthetic test data: a table shaped like a real one, with none of its people in it.

``POST /v1/synthetic`` with ``{"source": "sales.orders", "target": "dev.orders_synthetic", "rows": 10000, "seed": 7}``
writes a new lakehouse table with the source's columns and types, and values drawn column by column from the
source's statistics:

* **Statistics come from Trino as the caller**, so their masks and row filters apply: what they may not see does
  not shape the data either. Reading the source needs ``VIEW_DATASET_PROFILE`` on it; making the target needs
  ``MANAGE_DATASET`` on it.
* **No real value is copied from a sensitive column.** A column classified sensitive (PII and the like) or masked
  for the caller gets values made from its type and shape alone (an e-mail column gets ``user123@example.com``),
  and its numbers stay within the middle of the range (5th to 95th percentile), never the extremes.
* **Categories are kept only when common.** A column with few distinct values keeps its values and their shares,
  but only values that occur at least ``CAIRN_SYNTHETIC_MIN_COUNT`` times (10): rare values, which identify people,
  never appear.
* **Shapes kept:** the share of nulls, numeric distributions (by percentiles), date and time ranges, text lengths,
  and unique columns stay unique. Relations between columns are not kept: each column is drawn on its own.
* The same ``seed`` gives the same table. The table is marked ``cairn.synthetic.source`` (its source), and the run
  is audited with what each column was made from.
"""

from __future__ import annotations

import bisect
import datetime as dt
import logging
import os
import random
import re
import string
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
READ, MANAGE = "VIEW_DATASET_PROFILE", "MANAGE_DATASET"
MAX_ROWS = 1_000_000
CATEGORIES = 50
QUANTILES = [i / 20 for i in range(21)]       # 0, 0.05, ..., 1


class SyntheticError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _split(table: str) -> tuple[str, str]:
    parts = (table or "").split(".")
    if len(parts) != 2 or not all(IDENT.match(p) for p in parts):
        raise SyntheticError("tables are namespace.table")
    return parts[0], parts[1]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _kind(t) -> str:
    n = type(t).__name__
    return {"IntegerType": "int", "LongType": "int", "FloatType": "float", "DoubleType": "float", "DecimalType": "decimal",
            "StringType": "string", "BooleanType": "bool", "DateType": "date", "TimestampType": "timestamp",
            "TimestamptzType": "timestamptz"}.get(n, "other")


def _interp(qs: list[float], u: float) -> float:
    """The value at quantile ``u`` from evenly spaced quantiles."""
    pos = u * (len(qs) - 1)
    i = min(int(pos), len(qs) - 2)
    return qs[i] + (qs[i + 1] - qs[i]) * (pos - i)


class Synthetic:
    def __init__(self, catalog_props: dict, authorize: Callable[[str, str, str], bool], urn: Callable[[str, str], str],
                 run_sql: Callable[[str, str, int], tuple[list[str], list[list[Any]], bool]], trino_catalog: str,
                 sensitive: Callable[[str], set], plan_for: Callable[[str, str], Any], audit=None):
        """``sensitive(urn)``: the columns classified sensitive. ``plan_for(actor, urn)``: their masking plan."""
        self.catalog_props, self.authorize, self.urn, self.run_sql = catalog_props, authorize, urn, run_sql
        self.trino_catalog, self.sensitive, self.plan_for, self.audit = trino_catalog, sensitive, plan_for, audit
        self.min_count = int(os.environ.get("CAIRN_SYNTHETIC_MIN_COUNT", "10"))

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("cairn-synthetic", **self.catalog_props)

    def _need(self, actor: str, privilege: str, ns: str, t: str) -> None:
        try:
            ok = bool(self.authorize(actor, privilege, self.urn(ns, t)))
        except Exception:  # noqa: BLE001 - fail closed
            ok = False
        if not ok:
            raise SyntheticError(f"{privilege} on {ns}.{t} is needed", 403)

    def _one(self, actor: str, sql: str) -> list:
        _, rows, _ = self.run_sql(actor, sql, 1)
        return rows[0] if rows else []

    # ---- statistics -----------------------------------------------------------------------------------
    def _profile(self, actor: str, rel: str, name: str, kind: str, hidden: bool) -> dict:
        c = _q(name)
        total, nonnull, distinct = self._one(actor, f"SELECT count(*), count({c}), approx_distinct({c}) FROM {rel}")
        p: dict[str, Any] = {"kind": kind, "rows": int(total or 0), "nonnull": int(nonnull or 0),
                             "distinct": int(distinct or 0), "hidden": hidden}
        if not p["nonnull"]:
            return p
        if kind in ("int", "float", "decimal", "date", "timestamp", "timestamptz"):
            v = {"date": f"date_diff('day', DATE '1970-01-01', {c})",
                 "timestamp": f"to_unixtime(CAST({c} AS timestamp(6) with time zone))",
                 "timestamptz": f"to_unixtime({c})"}.get(kind, f"CAST({c} AS double)")
            arr = ", ".join(str(q) for q in QUANTILES)
            (qs,) = self._one(actor, f"SELECT approx_percentile({v}, ARRAY[{arr}]) FROM {rel}")
            p["quantiles"] = [float(x) for x in qs]
        if kind == "string":
            mn, mx, at = self._one(actor, f"SELECT min(length({c})), max(length({c})), "
                                          f"count_if(strpos({c}, '@') > 0) FROM {rel}")
            p.update(min_len=int(mn or 0), max_len=int(mx or 0), email=int(at or 0) >= 0.8 * p["nonnull"])
        if kind == "bool":
            (true,) = self._one(actor, f"SELECT count_if({c}) FROM {rel}")
            p["true_share"] = int(true or 0) / p["nonnull"]
        if not hidden and kind in ("string", "int", "bool", "date") and p["distinct"] <= CATEGORIES:
            _, rows, _ = self.run_sql(actor, f"SELECT {c}, count(*) FROM {rel} WHERE {c} IS NOT NULL GROUP BY {c} "
                                             f"HAVING count(*) >= {int(self.min_count)} ORDER BY 2 DESC LIMIT {CATEGORIES}",
                                      CATEGORIES)
            if rows:
                p["categories"] = [(r[0], int(r[1])) for r in rows]
        return p

    # ---- values -----------------------------------------------------------------------------------------
    @staticmethod
    def _method(p: dict) -> str:
        if not p["nonnull"]:
            return "null"
        if p.get("categories"):
            return "categories"
        if p["kind"] in ("int", "string") and p["distinct"] >= 0.97 * p["nonnull"] and p["nonnull"] > 1:
            return "unique"
        return ("shape" if p["hidden"] else "distribution") if p["kind"] != "other" else "null"

    def _column(self, rnd: random.Random, p: dict, n: int, field_type) -> tuple[list, str]:
        method = self._method(p)
        null_share = 1 - p["nonnull"] / p["rows"] if p["rows"] else 1.0
        kind = p["kind"]
        if method == "null":
            return [None] * n, method
        if method == "categories":
            values = [v for v, _ in p["categories"]]
            cum, total = [], 0
            for _, k in p["categories"]:
                total += k
                cum.append(total)
            draw = lambda: values[bisect.bisect_right(cum, rnd.random() * total)]  # noqa: E731
        elif method == "unique":
            if kind == "int":
                start = rnd.randint(1, 1000)
                seq = iter(range(start, start + n))
                draw = lambda: next(seq)  # noqa: E731
            else:
                seen: set = set()

                def draw():
                    while True:
                        v = self._text(rnd, p)
                        if v not in seen:
                            seen.add(v)
                            return v
        elif kind == "string":
            draw = lambda: self._text(rnd, p)  # noqa: E731
        elif kind == "bool":
            draw = lambda: rnd.random() < p.get("true_share", 0.5)  # noqa: E731
        else:
            qs = p["quantiles"]
            lo, hi = (0.05, 0.95) if p["hidden"] else (0.0, 1.0)     # a sensitive column's extremes never appear

            def raw():
                return _interp(qs, lo + (hi - lo) * rnd.random())
            if kind == "int":
                draw = lambda: int(round(raw()))  # noqa: E731
            elif kind == "decimal":
                from decimal import Decimal

                scale = getattr(field_type, "scale", 2)
                draw = lambda: Decimal(str(round(raw(), scale)))  # noqa: E731
            elif kind == "float":
                draw = raw
            elif kind == "date":
                draw = lambda: dt.date(1970, 1, 1) + dt.timedelta(days=int(round(raw())))  # noqa: E731
            else:
                tz = dt.timezone.utc if kind == "timestamptz" else None
                draw = lambda: dt.datetime.fromtimestamp(round(raw(), 3), dt.timezone.utc).replace(  # noqa: E731
                    tzinfo=tz)
        return [None if rnd.random() < null_share else draw() for _ in range(n)], method

    @staticmethod
    def _text(rnd: random.Random, p: dict) -> str:
        if p.get("email"):
            return f"user{rnd.randint(1, 10 ** 9)}@example.com"
        lo, hi = max(1, p.get("min_len", 1)), max(1, p.get("max_len", 8))
        return "".join(rnd.choice(string.ascii_lowercase) for _ in range(rnd.randint(lo, max(lo, hi))))

    # ---- the run ----------------------------------------------------------------------------------------
    def generate(self, actor: str, source: str, target: str, rows: int, seed: Optional[int] = None,
                 replace: bool = False) -> dict:
        import pyarrow as pa
        from pyiceberg.exceptions import NoSuchTableError
        from pyiceberg.schema import Schema
        from pyiceberg.types import NestedField

        sns, st = _split(source)
        tns, tt = _split(target)
        if (sns, st) == (tns, tt):
            raise SyntheticError("the target is another table than the source")
        if not isinstance(rows, int) or not 1 <= rows <= MAX_ROWS:
            raise SyntheticError(f"rows: 1 to {MAX_ROWS}")
        self._need(actor, READ, sns, st)
        self._need(actor, MANAGE, tns, tt)
        cat = self._catalog()
        try:
            src = cat.load_table((sns, st))
        except NoSuchTableError:
            raise SyntheticError(f"no table {source}", 404)
        exists = cat.table_exists((tns, tt))
        if exists and not replace:
            raise SyntheticError(f"{target} exists (replace: true to write over it)", 409)
        urn = self.urn(sns, st)
        hidden = {c.lower() for c in (self.sensitive(urn) or set())}
        plan = self.plan_for(actor, urn)
        rel = f"{_q(self.trino_catalog)}.{_q(sns)}.{_q(st)}"
        rnd = random.Random(seed)
        fields, arrays, methods = [], [], {}
        arrow = src.schema().as_arrow()
        for i, f in enumerate(src.schema().fields, start=1):
            kind = _kind(f.field_type)
            secret = f.name.lower() in hidden or bool(getattr(plan, "all_fields", False)) or plan.masks_field(f.name)
            prof = self._profile(actor, rel, f.name, kind, secret)
            values, method = self._column(rnd, prof, rows, f.field_type)
            methods[f.name] = method
            fields.append(NestedField(i, f.name, f.field_type, required=False, doc=f.doc))
            arrays.append(pa.array(values, type=arrow.field(f.name).type))
        schema = Schema(*fields)
        data = pa.Table.from_arrays(arrays, schema=schema.as_arrow())
        try:
            cat.create_namespace((tns,))
        except Exception:  # noqa: BLE001 - it exists
            pass
        if exists:
            cat.drop_table((tns, tt))
        table = cat.create_table((tns, tt), schema=schema, properties={"cairn.synthetic.source": source})
        table.append(data)
        out = {"source": source, "target": target, "rows": rows, "seed": seed, "columns": methods}
        if self.audit is not None:
            self.audit.record("lakehouse", "synthetic.generated", actor, self.urn(tns, tt), out)
        return out


def build_synthetic_router(s: Synthetic, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/synthetic", tags=["lakehouse"])

    @router.post("", status_code=201)
    def generate(source: str = Body(...), target: str = Body(...), rows: int = Body(1000), seed: Optional[int] = Body(None),
                 replace: bool = Body(False), p=Depends(current_principal)):
        """A table shaped like ``source``, with none of its sensitive values, at ``target``."""
        try:
            return s.generate(p.urn, source, target, rows, seed, replace)
        except SyntheticError as e:
            raise HTTPException(e.status, str(e))

    return router
