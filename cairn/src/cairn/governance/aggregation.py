"""Aggregation and projection policies: tables whose rows may only be read in aggregate, and columns that may be used
but not shown.

Both are policy obligations on reading a table (``VIEW_DATASET_PROFILE``), so they follow the same subjects,
conditions and exemptions as every other policy:

* ``{"type": "aggregation", "params": {"min_group": 5, "entity_key": "customer_id"}}``. Every query block that
  reads the table must aggregate (``GROUP BY`` or aggregates only; no ``*``, no window functions). Cairn adds
  ``HAVING count(*) >= min_group`` to that block, so no group smaller than ``min_group`` rows is released. With
  ``entity_key``, the floor counts distinct entities (``count(DISTINCT <alias>.<key>)``) and the block may join
  other tables. Without one, joining is refused, because a join can multiply one person's rows past the floor.
* ``{"type": "projection", "params": {"columns": ["email"]}}``. The columns may be used in ``WHERE``, ``JOIN``,
  ``GROUP BY``, ``HAVING`` and ``ORDER BY``, and counted (``count(email)``), but never returned: not as a column,
  not inside an expression, not through a subquery. A ``*`` over the table is refused.

**Where they hold.** On a Trino connection a direct read of an aggregation-protected table is refused, and so is a
direct read that names a projection-protected column. Trino's access control sees which tables and columns a
query reads, not the shape of the query. Such queries run through Cairn's query endpoint (``/v1/data/query``, the
SQL editor, dashboards and ask-your-data), which checks the SQL here, adds the floors, and runs the rewritten
query on Trino as a one-query identity (``checked-<id>``, registered for ten minutes with the person and the
tables it was checked for). Trino lets that identity read those tables and nothing more under these policies,
with the person's own row filters and masks. On other connections Cairn runs the rewritten SQL itself.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, delete, insert, select
from sqlglot import exp

from cairn.common.db import JSONText, metadata

checked_queries = Table(
    "cairn_checked_queries", metadata,
    Column("id", String(64), primary_key=True),
    Column("actor", String(512), nullable=False),
    Column("groups", JSONText, nullable=False),
    Column("tables", JSONText, nullable=False),            # urns the query was checked for
    Column("expires_ms", BigInteger, nullable=False, index=True),
)

IDENTITY_PREFIX = "checked-"
# The aggregates that reduce a group to a figure no single row can be read back from. min, max, arbitrary,
# array_agg, listagg, percentiles, min_by and their kind return a row's own value: never allowed over a protected
# table, and neither is a function Cairn does not know (it may be an aggregate of that kind).
SAFE_AGGREGATES = (exp.Count, exp.CountIf, exp.Sum, exp.Avg, exp.ApproxDistinct, exp.Stddev, exp.StddevPop,
                   exp.StddevSamp, exp.Variance, exp.VariancePop)
CHECKED_TTL_MS = 10 * 60_000


class PolicyViolation(Exception):
    """The query does not satisfy an aggregation or projection policy (the message says how to fix it)."""


@dataclass
class TablePolicy:
    min_group: Optional[int] = None          # aggregation: the smallest group released
    entity_key: Optional[str] = None
    projection: set[str] = field(default_factory=set)   # columns used but never returned (lower case)
    policies: list[str] = field(default_factory=list)

    @property
    def aggregation(self) -> bool:
        return self.min_group is not None


def from_obligations(obligations) -> Optional[TablePolicy]:
    """The table policy a read decision's obligations add up to (None: neither kind). A misconfigured obligation
    raises PolicyViolation: it is never read as no policy."""
    pol, found = TablePolicy(), False
    for o in obligations or []:
        typ = getattr(o, "type", None)
        params = getattr(o, "params", None) or {}
        if typ == "aggregation":
            found = True
            k = params.get("min_group")
            if not isinstance(k, int) or isinstance(k, bool) or k < 2:
                raise PolicyViolation(f"policy {o.policy_id}: an aggregation policy's min_group is a whole number of "
                                      "2 or more")
            pol.min_group = max(pol.min_group or 0, k)
            key = params.get("entity_key")
            if key is not None:
                if not isinstance(key, str) or not key.strip():
                    raise PolicyViolation(f"policy {o.policy_id}: entity_key names one column")
                if pol.entity_key not in (None, key.strip().lower()):
                    raise PolicyViolation("two aggregation policies name different entity keys for this table")
                pol.entity_key = key.strip().lower()
        elif typ == "projection":
            found = True
            cols = params.get("columns")
            if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and c.strip() for c in cols):
                raise PolicyViolation(f"policy {o.policy_id}: a projection policy lists its columns")
            pol.projection |= {c.strip().lower() for c in cols}
        else:
            continue
        if getattr(o, "policy_id", None):
            pol.policies.append(o.policy_id)
    return pol if found else None


def _is_star(e: exp.Expression) -> bool:
    return isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))


def _direct_tables(sel: exp.Select) -> list[exp.Table]:
    """The tables a query block reads itself (not those of its subqueries or CTEs)."""
    out = []
    for part in [sel.args.get("from_") or sel.args.get("from")] + list(sel.args.get("joins") or []):
        if part is None:
            continue
        for t in part.find_all(exp.Table):
            if t.find_ancestor(exp.Select) is sel:
                out.append(t)
    return out


def _alias(t: exp.Table) -> str:
    return (t.alias_or_name or "").lower()


def _count_only(col: exp.Column, top: exp.Expression) -> bool:
    """Is the column only counted (inside count(...)) within ``top``?"""
    node = col.parent
    while node is not None and node is not top.parent:
        if isinstance(node, exp.Count):
            return True
        node = node.parent
    return False


def enforce(tree: exp.Expression, policy_of: Callable[[exp.Table], Optional[TablePolicy]]) -> exp.Expression:
    """``tree`` (a copy) checked against the policies of the tables it reads, with the aggregation floors added.
    ``policy_of(table)`` is the policy for a table node (None for none, and for CTE names)."""
    tree = tree.copy()
    for sel in list(tree.find_all(exp.Select)):
        scoped = [(t, p) for t in _direct_tables(sel) if (p := policy_of(t)) is not None]
        if not scoped:
            continue
        names = ", ".join(sorted({t.name for t, _ in scoped}))
        if any(_is_star(e) for e in sel.expressions):
            raise PolicyViolation(f"name the columns you select: {names} has an aggregation or projection policy")
        # Projection: protected columns are never returned from the block that reads them.
        hidden = {(_alias(t), c) for t, p in scoped for c in p.projection}
        if hidden:
            hidden_names = {c for _, c in hidden}
            aliases_with = {a for a, _ in hidden}
            for proj in sel.expressions:
                for col in proj.find_all(exp.Column):
                    name, qual = col.name.lower(), (col.table or "").lower()
                    if name not in hidden_names or (qual and qual not in aliases_with):
                        continue
                    if qual and (qual, name) not in hidden:
                        continue
                    if not _count_only(col, proj):
                        raise PolicyViolation(f"{name} may be used to filter, join or group, but not returned "
                                              f"(projection policy on {names})")
        aggregated = [(t, p) for t, p in scoped if p.aggregation]
        if not aggregated:
            continue
        # Aggregation: the block aggregates, and its groups are at least min_group rows (or entities).
        for part in ("expressions", "having", "order"):
            val = sel.args.get(part)
            for node in (val if isinstance(val, list) else [val]):
                if node is None:
                    continue
                if node.find(exp.Window):
                    raise PolicyViolation(f"no window functions over {names}: it may only be read in aggregate")
                for agg in node.find_all(exp.AggFunc):
                    if not isinstance(agg, SAFE_AGGREGATES):
                        raise PolicyViolation(f"{agg.sql()[:60]} returns a row's own value: over {names} use count, "
                                              "sum, avg, approx_distinct, stddev or variance")
                if node.find(exp.Anonymous):
                    raise PolicyViolation(f"{node.find(exp.Anonymous).sql()[:60]}: a function Cairn does not know is "
                                          f"not allowed over {names} (it may return a row's own value)")
        group = sel.args.get("group")
        grouped = {g.sql().lower() for g in (group.expressions if group is not None else [])}
        positions = {int(g.name) for g in (group.expressions if group is not None else [])
                     if isinstance(g, exp.Literal) and g.is_int}
        has_agg = False
        for i, proj in enumerate(sel.expressions, start=1):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if inner.find(exp.AggFunc) is not None:
                has_agg = True
                continue
            if i in positions or inner.sql().lower() in grouped or proj.alias_or_name.lower() in grouped \
                    or isinstance(inner, (exp.Literal, exp.Null, exp.Boolean)):
                continue
            raise PolicyViolation(f"{inner.sql()[:80]} is neither aggregated nor grouped: {names} may only be read "
                                  "in aggregate")
        if not has_agg and group is None:
            raise PolicyViolation(f"{names} may only be read in aggregate (count, sum, avg...)")
        joined = len(_direct_tables(sel)) > 1
        floors = []
        for t, p in aggregated:
            if p.entity_key is None:
                if joined:
                    raise PolicyViolation(f"{t.name} is read in aggregate without an entity key, so it cannot be "
                                          "joined here (a join can repeat one person's rows): aggregate it in a "
                                          "subquery first, or give the policy an entity_key")
                floors.append(exp.GTE(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(p.min_group)))
            else:
                key = exp.column(p.entity_key, table=t.alias_or_name, quoted=False)
                floors.append(exp.GTE(this=exp.Count(this=exp.Distinct(expressions=[key])),
                                      expression=exp.Literal.number(p.min_group)))
        having = sel.args.get("having")
        cond = having.this if having is not None else None
        for f in floors:
            cond = f if cond is None else exp.and_(cond, f)
        sel.set("having", exp.Having(this=cond))
    return tree


class CheckedQueries:
    """One-query identities: Cairn checked a query for a person and the tables in it; Trino lets the identity read
    those tables under their aggregation and projection policies, as that person, for ten minutes."""

    def __init__(self, engine, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine = engine
        self.clock = clock

    def register(self, actor: str, groups: list[str], tables: list[str]) -> str:
        qid = secrets.token_hex(12)
        now = self.clock()
        with self.engine.begin() as c:
            c.execute(delete(checked_queries).where(checked_queries.c.expires_ms < now))
            c.execute(insert(checked_queries).values(id=qid, actor=actor, groups=list(groups), tables=sorted(set(tables)),
                                                     expires_ms=now + CHECKED_TTL_MS))
        return f"urn:li:corpuser:{IDENTITY_PREFIX}{qid}"

    def lookup(self, user: str) -> Optional[dict]:
        """The registration behind a Trino user ``checked-<id>`` (None: not one, unknown or expired)."""
        if not user.startswith(IDENTITY_PREFIX):
            return None
        qid = user.removeprefix(IDENTITY_PREFIX)
        with self.engine.connect() as c:
            row = c.execute(select(checked_queries).where(checked_queries.c.id == qid)).first()
        if row is None or row.expires_ms < self.clock():
            return None
        return {"actor": row.actor, "groups": list(row.groups or []), "tables": set(row.tables or [])}


def describe(pol: TablePolicy) -> dict:
    return json.loads(json.dumps({"min_group": pol.min_group, "entity_key": pol.entity_key,
                                  "projection": sorted(pol.projection), "policies": pol.policies}))
