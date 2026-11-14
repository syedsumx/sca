"""Semantic model autopilot: a draft semantic model built from tables and how they are queried, reviewed by a
person and published as metrics.

``POST /sql/semantic-models`` with ``{"connection": "lakehouse", "tables": ["sales.orders", "sales.customers"]}``:

1. **The tables as the person sees them.** Each table must be one the person may see. Its columns are profiled by
   one governed query run as them: rows, non-null and distinct counts, smallest and largest values. Masks and row
   filters apply, so the profile holds nothing they could not read. A column whose figures come back masked is
   neither a dimension nor a measure.
2. **How the tables are used.** Finished queries in query history that read the tables (the last
   ``CAIRN_AUTOPILOT_HISTORY_DAYS`` days, 90 by default) are parsed for their shape only: which aggregates over
   which columns, which columns are grouped by, which equality filters go with an aggregate, and which columns join
   two of the tables. Nobody's SQL text or user reaches the draft. A literal from a filter is kept only when the
   person's own read of that column (its values, up to 200) holds it.
3. **Candidates.** A row count per table; each aggregate seen in history (ranked by how often it ran, with the
   filters it ran with); a sum of each numeric column that is not a key; dimensions from history's group-bys and
   the table's columns with few values; a time column from history or the first date or time column; and the
   relationships seen in joins.
4. **Named by the model**, when one is configured: labels, descriptions and synonyms for the candidates it keeps,
   as structured output. It picks candidates by id and never writes a formula, so a measure reaches SQL only as one
   Cairn built. Without a model the names are made from the formula.
5. **Checked as the person.** Each proposal is compiled and run as the person (its total over the table), like a
   metric being saved. Refused ones are reported with why.

The draft is the person's own. ``POST /sql/semantic-models/<id>/accept`` with ``{"metrics": [names], "edits":
{name: {...}}}`` saves the chosen proposals as metrics owned by them (each checked again as they save it, an
existing name reported and skipped), synonyms included; Ask-your-data and the AI builder then use them. Audited:
``semantic_model.drafted`` (tables and counts, no values) and ``semantic_model.accepted``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import Counter
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.metrics import NAME, MetricError, element_for
from cairn.sqlworkspace.workbooks import WorkbookError, _table

semantic_drafts = Table(
    "cairn_semantic_drafts", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("connection", String(128), nullable=False),
    Column("tables", Text, nullable=False),
    Column("model", Text, nullable=False),              # {"table_meta": {...}, "metrics": [...], "relationships": [...]}
    Column("refused", Text, nullable=False, default="[]"),
    Column("status", String(16), nullable=False),       # draft | accepted
    Column("created_ms", BigInteger, nullable=False),
    Column("accepted", Text, nullable=False, default="[]"),
)

MAX_TABLES = 8
MAX_COLUMNS = 60
MAX_VALUES = 200
KEY = re.compile(r"(^id$|_id$|^id_|_key$|^key$|uuid|_sk$)", re.I)
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?")
AGGS = {"sum": "Sum", "avg": "Avg", "min": "Min", "max": "Max", "count_distinct": "CountDistinct"}
WORDS = {"Sum": "total", "Avg": "average", "Min": "lowest", "Max": "highest", "CountDistinct": "distinct"}

SYSTEM = """You name the parts of a semantic model so business people find them. You are given tables with their
columns (kind, distinct values) and candidate measures (an id, a formula Cairn built, the filters it applies, how
often history ran it). Keep the candidates a business person would use (drop near-duplicates and meaningless ones,
such as a sum of a code), and give each kept one a metric name (lowercase letters, digits and _, starting with a
letter, at most 60 characters), a short label, a one-sentence description, and up to five synonyms people would
say. You may also describe dimensions. Never write a formula: refer to candidates only by their id. """

SCHEMA: dict = {
    "type": "object", "additionalProperties": False, "required": ["metrics"],
    "properties": {
        "metrics": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["candidate", "name", "label"],
            "properties": {"candidate": {"type": "string"}, "name": {"type": "string"}, "label": {"type": "string"},
                           "description": {"type": "string"},
                           "synonyms": {"type": "array", "items": {"type": "string"}}}}},
        "dimensions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["table", "column", "description"],
            "properties": {"table": {"type": "string"}, "column": {"type": "string"},
                           "description": {"type": "string"}}}},
    },
}


class AutopilotError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _short(table: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", table.rsplit(".", 1)[-1].lower())


def _slug(*parts: str) -> str:
    s = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", "_".join(p.lower() for p in parts if p))).strip("_")
    if not s or not s[0].isalpha():
        s = "m_" + s
    return s[:60].rstrip("_")


def _kind(lo: Any, hi: Any) -> str:
    v = lo if lo is not None else hi
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str) and DATE.match(v):
        return "time"
    return "text" if v is not None else "empty"


def mine(sqls: list[str], tables: dict[str, list[str]]) -> dict:
    """The shape of how ``tables`` (``schema.table`` → columns) are used in ``sqls``: aggregates (with the
    equality filters they ran with), group-bys, filters and joins, each with how often. No SQL text is kept."""
    import sqlglot
    from sqlglot import exp

    wanted = {t.lower(): t for t in tables}
    cols_of = {t: {c.lower(): c for c in cs} for t, cs in tables.items()}
    aggs: Counter = Counter()
    groups: Counter = Counter()
    filters: Counter = Counter()
    joins: Counter = Counter()
    for sql in sqls:
        try:
            tree = sqlglot.parse_one(sql, read="trino")
        except Exception:  # noqa: BLE001 - a query that does not parse adds nothing
            continue
        if tree is None:
            continue
        alias: dict[str, str] = {}
        present = []
        for t in tree.find_all(exp.Table):
            ref = ".".join(p for p in (t.db, t.name) if p).lower()
            hit = next((wanted[w] for w in wanted if ref == w or ref.endswith("." + w) or w.endswith("." + ref)), None)
            if hit:
                alias[(t.alias_or_name or t.name).lower()] = hit
                alias[t.name.lower()] = hit
                present.append(hit)
        if not present:
            continue

        def owner(c: exp.Column, alias=alias, present=present) -> Optional[tuple[str, str]]:
            name = c.name.lower()
            if c.table:
                t = alias.get(c.table.lower())
                return (t, cols_of[t][name]) if t and name in cols_of[t] else None
            hits = [t for t in dict.fromkeys(present) if name in cols_of[t]]
            return (hits[0], cols_of[hits[0]][name]) if len(hits) == 1 else None

        def eq_filters(node) -> list[tuple[str, str, Any]]:
            out = []
            if node is None:
                return out
            for eq in node.find_all(exp.EQ):
                col, lit = eq.left, eq.right
                if isinstance(lit, exp.Column):
                    col, lit = lit, col
                if isinstance(col, exp.Column) and isinstance(lit, exp.Literal):
                    o = owner(col)
                    if o:
                        value: Any = lit.this
                        if not lit.is_string:
                            try:
                                value = float(value) if "." in value else int(value)
                            except ValueError:
                                continue
                        out.append((o[0], o[1], value))
            return out

        for sel in tree.find_all(exp.Select):
            where = eq_filters(sel.args.get("where"))
            for f in where:
                filters[f] += 1
            for g in (sel.args.get("group") or exp.Group()).expressions:
                if isinstance(g, exp.Column) and owner(g):
                    groups[owner(g)] += 1
            for e in sel.expressions:
                for fn in e.find_all(exp.AggFunc):
                    kind = None
                    arg = fn.this
                    if isinstance(fn, exp.Count) and isinstance(arg, exp.Distinct) and arg.expressions:
                        kind, arg = "count_distinct", arg.expressions[0]
                    elif isinstance(fn, exp.Sum):
                        kind = "sum"
                    elif isinstance(fn, exp.Avg):
                        kind = "avg"
                    elif isinstance(fn, exp.Min):
                        kind = "min"
                    elif isinstance(fn, exp.Max):
                        kind = "max"
                    if kind and isinstance(arg, exp.Column) and owner(arg):
                        t, c = owner(arg)
                        aggs[(t, kind, c, tuple(sorted((fc, fv) for ft, fc, fv in where if ft == t)))] += 1
            for j in sel.args.get("joins") or []:
                on = j.args.get("on")
                for eq in (on.find_all(exp.EQ) if on is not None else []):
                    if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column):
                        a, b = owner(eq.left), owner(eq.right)
                        if a and b and a[0] != b[0]:
                            joins[tuple(sorted([a, b]))] += 1
    return {"aggregates": aggs, "groups": groups, "filters": filters, "joins": joins}


class Autopilot:
    def __init__(self, engine, workbooks, metrics, history: Callable[[], Any],
                 urn_of: Callable[[str, str], Optional[str]], llm=None, model: Optional[str] = None, audit=None,
                 clock: Callable[[], float] = time.time):
        """``history()``: the query history (or None); ``urn_of(connection, table)``: a table's dataset urn."""
        self.engine, self.wb, self.metrics, self.history = engine, workbooks, metrics, history
        self.urn_of, self.llm, self.model, self.audit, self.clock = urn_of, llm, model, audit, clock
        self.days = int(os.environ.get("CAIRN_AUTOPILOT_HISTORY_DAYS", "90"))

    # ---- profiling, as the person -------------------------------------------------------------------
    def _profile(self, actor: str, connection: str, table: str) -> dict:
        cols = self.wb.columns_of(actor, connection, table)[:MAX_COLUMNS]
        aggs = [{"name": "__rows", "formula": "Count()"}]
        for i, c in enumerate(cols):
            aggs += [{"name": f"n{i}", "formula": f"Count([{c}])"},
                     {"name": f"d{i}", "formula": f"CountDistinct([{c}])"},
                     {"name": f"lo{i}", "formula": f"Min([{c}])"}, {"name": f"hi{i}", "formula": f"Max([{c}])"}]
        out = self.wb.run(actor, connection, {"source": {"table": table}, "aggregates": aggs, "limit": 1}, cache=False)
        names = [c if isinstance(c, str) else c.get("name") for c in out.get("columns") or []]
        row = dict(zip(names, (out.get("rows") or [[None] * len(names)])[0]))
        masked = set(out.get("masked_columns") or [])
        prof = {"rows": int(row.get("__rows") or 0), "columns": {}}
        for i, c in enumerate(cols):
            lo, hi = row.get(f"lo{i}"), row.get(f"hi{i}")
            prof["columns"][c] = {"kind": _kind(lo, hi), "non_null": int(row.get(f"n{i}") or 0),
                                  "distinct": int(row.get(f"d{i}") or 0),
                                  "masked": bool(masked & {f"lo{i}", f"hi{i}", f"d{i}"}), "key": bool(KEY.search(c))}
        return prof

    def _values(self, actor: str, connection: str, table: str, column: str) -> set:
        try:
            out = self.wb.run(actor, connection, {"source": {"table": table}, "group_by": [column],
                                                  "aggregates": [{"name": "__n", "formula": "Count()"}],
                                                  "sort": [{"column": "__n", "desc": True}], "limit": MAX_VALUES},
                              cache=False)
        except (WorkbookError, Exception) as e:  # noqa: BLE001 - no values: no literal filters on this column
            if not hasattr(e, "status"):
                raise
            return set()
        return {r[0] for r in out.get("rows") or []}

    # ---- the draft -------------------------------------------------------------------------------------
    def draft(self, actor: str, body: dict, can_view: Callable[[str], bool]) -> dict:
        if not isinstance(body, dict):
            raise AutopilotError("a semantic model request is an object")
        connection = str(body.get("connection") or "")
        tables = body.get("tables")
        if not connection or not isinstance(tables, list) or not 1 <= len(tables) <= MAX_TABLES or \
                not all(isinstance(t, str) for t in tables) or len(set(tables)) != len(tables):
            raise AutopilotError(f"connection, and tables: 1 to {MAX_TABLES} distinct schema.table names")
        urns = {}
        for t in tables:
            try:
                _table(t)
            except WorkbookError as e:
                raise AutopilotError(str(e))
            urn = self.urn_of(connection, t)
            if not urn or not can_view(urn):                   # fail closed: unknown or unseen tables are not used
                raise AutopilotError(f"no table {t} you may see on {connection}", 404)
            urns[t] = urn
        try:
            profiles = {t: self._profile(actor, connection, t) for t in tables}
        except (WorkbookError, Exception) as e:  # noqa: BLE001 - governed refusals keep their status
            if not hasattr(e, "status"):
                raise
            raise AutopilotError(f"the tables could not be profiled as you: {e}", e.status)
        usage = self._usage(urns, {t: list(p["columns"]) for t, p in profiles.items()})
        model, candidates = self._candidates(actor, connection, profiles, usage)
        named = self._name(model, candidates) if (self.llm is not None and body.get("use_model", True)) else None
        proposals, refused = self._check(actor, connection, candidates, named)
        for x in (named or {}).get("dimensions") or []:
            meta = model["table_meta"].get(x.get("table"))
            if meta is not None and x.get("column") in meta["dimensions"]:
                meta.setdefault("dimension_descriptions", {})[x["column"]] = str(x.get("description") or "")[:300]
        model["metrics"] = proposals
        did = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(semantic_drafts).values(
                id=did, owner=actor, connection=connection, tables=json.dumps(tables), model=json.dumps(model),
                refused=json.dumps(refused), status="draft", created_ms=int(self.clock() * 1000), accepted="[]"))
        if self.audit is not None:
            self.audit.record("sql", "semantic_model.drafted", actor, f"semantic_model:{did}",
                              {"connection": connection, "tables": tables, "metrics": len(proposals),
                               "refused": len(refused), "relationships": len(model["relationships"]),
                               "history_queries": usage["queries"], "named_by_model": named is not None})
        return self.get(actor, did)

    def _usage(self, urns: dict[str, str], columns: dict[str, list[str]]) -> dict:
        h = self.history() if self.history is not None else None
        sqls: list[str] = []
        if h is not None:
            since = int((self.clock() - self.days * 86400) * 1000)
            runs: Counter = Counter()
            for urn in urns.values():                   # a query reading two of the tables counts once
                for sql, n in Counter(sql for sql, _ in h.reading(urn, since, limit=2000)).items():
                    runs[sql] = max(runs[sql], n)
            sqls = [sql for sql, n in runs.items() for _ in range(n)]
        out = mine(sqls, columns)
        out["queries"] = len(sqls)
        return out

    def _candidates(self, actor: str, connection: str, profiles: dict, usage: dict) -> tuple[dict, list[dict]]:
        values_cache: dict[tuple[str, str], set] = {}

        def visible(t: str, c: str, v: Any) -> bool:
            if (t, c) not in values_cache:
                values_cache[(t, c)] = self._values(actor, connection, t, c)
            return v in values_cache[(t, c)] or str(v) in {str(x) for x in values_cache[(t, c)]}

        def usable(t: str, c: str) -> bool:
            col = profiles[t]["columns"].get(c)
            return bool(col) and not col["masked"]

        tables_out: dict[str, dict] = {}
        for t, p in profiles.items():
            cols = p["columns"]
            times = [c for c, x in cols.items() if x["kind"] == "time" and usable(t, c)]
            used_time = sorted((n, c) for (tt, c), n in usage["groups"].items() if tt == t and c in times)
            time_column = used_time[-1][1] if used_time else (times[0] if times else None)
            dims: list[str] = [c for (tt, c), _ in usage["groups"].most_common() if tt == t and usable(t, c)
                               and cols[c]["kind"] != "time"]
            for c, x in cols.items():
                if c in dims or not usable(t, c) or x["key"] or x["kind"] not in ("text", "boolean"):
                    continue
                # Few values (and, on a table of some size, repeated ones): a way to break figures down.
                if 0 < x["distinct"] <= MAX_VALUES and (p["rows"] < 20 or x["distinct"] <= p["rows"] // 2):
                    dims.append(c)
            tables_out[t] = {"rows": p["rows"], "time_column": time_column, "dimensions": dims[:12],
                             "columns": {c: {k: x[k] for k in ("kind", "distinct", "masked", "key")}
                                         for c, x in cols.items()}}
        cands: list[dict] = []
        seen: set[tuple] = set()

        def add(t: str, measure: str, filters: list, name: str, label: str, runs: int, why: str) -> None:
            key = (t, measure, json.dumps(filters, sort_keys=True, default=str))
            if key in seen:
                return
            seen.add(key)
            cands.append({"id": f"c{len(cands) + 1}", "table": t, "measure": measure, "filters": filters,
                          "name": name, "label": label, "runs": runs, "why": why})
        for t in profiles:
            add(t, "Count()", [], _slug(_short(t), "count"), f"{_short(t).replace('_', ' ').title()} count", 0, "rows")
        for (t, kind, c, fs), n in usage["aggregates"].most_common():
            if not usable(t, c) or (kind != "count_distinct" and profiles[t]["columns"][c]["kind"] != "number"
                                    and kind in ("sum", "avg")):
                continue
            filters = []
            for fc, fv in fs:
                if not usable(t, fc) or not visible(t, fc, fv):       # a literal the person could not read is dropped
                    filters = None
                    break
                filters.append({"column": fc, "op": "=", "value": fv})
            if filters is None:
                continue
            fn = AGGS[kind]
            suffix = "_".join(str(f["value"]) for f in filters)
            add(t, f"{fn}([{c}])", filters, _slug(_short(t), WORDS[fn], c, suffix),
                f"{WORDS[fn].title()} {c.replace('_', ' ')}" + (f" ({suffix.replace('_', ', ')})" if suffix else ""),
                n, "history")
        for t, p in profiles.items():
            for c, x in p["columns"].items():
                if x["kind"] == "number" and not x["key"] and not x["masked"] and x["distinct"] > 2:
                    add(t, f"Sum([{c}])", [], _slug(_short(t), "total", c), f"Total {c.replace('_', ' ')}", 0, "numeric")
        rels = [{"from": {"table": a[0], "column": a[1]}, "to": {"table": b[0], "column": b[1]}, "runs": n}
                for (a, b), n in usage["joins"].most_common(20)]
        return {"table_meta": tables_out, "relationships": rels}, cands[:40]

    def _name(self, model: dict, cands: list[dict]) -> Optional[dict]:
        from cairn.ai.guardrails import UNTRUSTED_NOTICE, untrusted
        from cairn.ai.llm import LLMError, validate_against_schema
        from cairn.ai.usage import LLMBudgetExceeded, llm_task

        lines = ["Tables:"]
        for t, x in model["table_meta"].items():
            cols = ", ".join(f"{c} ({v['kind']}, {v['distinct']} values)" for c, v in x["columns"].items()
                             if not v["masked"])
            lines.append(f"- {t}: {x['rows']} rows; columns {cols}; dimensions {', '.join(x['dimensions'])}")
        lines.append("Candidates:")
        for c in cands:
            f = " and ".join(f"{x['column']} = {json.dumps(x['value'])}" for x in c["filters"])
            lines.append(f"- {c['id']}: {c['measure']} on {c['table']}" + (f" where {f}" if f else "")
                         + f" (history ran it {c['runs']} times)")
        for r in model["relationships"]:
            lines.append(f"Relationship: {r['from']['table']}.{r['from']['column']} = {r['to']['table']}.{r['to']['column']}")
        try:
            with llm_task("semantic.autopilot"):
                out, _ = self.llm.generate_json(SYSTEM + UNTRUSTED_NOTICE, untrusted("\n".join(lines)), SCHEMA,
                                                model=self.model)
            validate_against_schema(out, SCHEMA)
        except LLMBudgetExceeded as e:
            raise AutopilotError(f"AI budget reached: {e}", 429)
        except (LLMError, ValueError) as e:
            raise AutopilotError(f"the model failed: {e}", 502)
        return out

    def _check(self, actor: str, connection: str, cands: list[dict], named: Optional[dict]) -> tuple[list, list]:
        by_id = {c["id"]: c for c in cands}
        chosen: list[dict] = []
        if named is None:
            chosen = [dict(c, description="", synonyms=[]) for c in cands]
        else:
            for m in named.get("metrics") or []:
                c = by_id.get(m.get("candidate"))
                if c is None:
                    continue                                    # the model refers to candidates only
                syn = [str(s)[:60] for s in (m.get("synonyms") or []) if isinstance(s, str) and s.strip()][:5]
                chosen.append(dict(c, name=str(m.get("name") or c["name"]), label=str(m.get("label") or c["label"])[:200],
                                   description=str(m.get("description") or "")[:500], synonyms=syn))
        existing = {m["name"] for m in self.metrics.list()}
        proposals, refused, names = [], [], set()
        for c in chosen:
            name = c["name"] if NAME.match(c["name"]) else _slug(c["name"])
            while name in names or name in existing:
                name = _slug(name[:56], uuid.uuid4().hex[:3])
            t = c["table"]
            d = {"name": name, "label": c["label"], "description": c["description"], "connection": connection,
                 "table": t, "measure": c["measure"], "filters": c["filters"], "synonyms": c["synonyms"],
                 "candidate": c["id"], "why": c["why"], "runs": c["runs"]}
            try:
                total = self.wb.run(actor, connection, element_for([{**d, "dimensions": [], "time_column": None}],
                                                                   {"limit": 1}), cache=False)
                d["total"] = (total.get("rows") or [[None]])[0][-1]
            except (WorkbookError, MetricError, Exception) as e:  # noqa: BLE001 - governed refusals are reported
                if not hasattr(e, "status"):
                    raise
                refused.append({"candidate": c["id"], "measure": c["measure"], "table": t, "error": str(e)})
                continue
            names.add(name)
            proposals.append(d)
        return proposals, refused

    # ---- reading, accepting --------------------------------------------------------------------------
    def _load(self, actor: str, did: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(semantic_drafts).where(semantic_drafts.c.id == did)).first()
        if r is None or r.owner != actor:                     # a draft is its owner's alone
            raise AutopilotError("no such semantic model draft", 404)
        return {"id": r.id, "owner": r.owner, "connection": r.connection, "tables": json.loads(r.tables),
                **json.loads(r.model), "refused": json.loads(r.refused), "status": r.status,
                "created_ms": r.created_ms, "accepted": json.loads(r.accepted)}

    def get(self, actor: str, did: str) -> dict:
        return self._load(actor, did)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(semantic_drafts.c.id, semantic_drafts.c.connection, semantic_drafts.c.tables,
                                       semantic_drafts.c.status, semantic_drafts.c.created_ms)
                                .where(semantic_drafts.c.owner == actor)
                                .order_by(semantic_drafts.c.created_ms.desc()).limit(200)).all()
        return [{"id": r.id, "connection": r.connection, "tables": json.loads(r.tables), "status": r.status,
                 "created_ms": r.created_ms} for r in rows]

    def delete(self, actor: str, did: str) -> None:
        self._load(actor, did)
        with self.engine.begin() as conn:
            conn.execute(delete(semantic_drafts).where(semantic_drafts.c.id == did))

    def accept(self, actor: str, did: str, body: dict) -> dict:
        d = self._load(actor, did)
        body = body if isinstance(body, dict) else {}
        want = body.get("metrics")
        edits = body.get("edits") or {}
        if want is not None and (not isinstance(want, list) or not all(isinstance(x, str) for x in want)):
            raise AutopilotError("metrics: a list of proposal names")
        if not isinstance(edits, dict):
            raise AutopilotError("edits: proposal name -> fields")
        props = {p["name"]: p for p in d["metrics"]}
        unknown = [n for n in (want or []) if n not in props]
        if unknown:
            raise AutopilotError(f"not a proposal of this draft: {unknown[0]}", 404)
        saved, skipped = [], []
        for name in (want if want is not None else list(props)):
            p = props[name]
            e = edits.get(name) or {}
            tmeta = d["table_meta"].get(p["table"]) or {}
            m = {"name": str(e.get("name") or name), "label": e.get("label", p["label"]),
                 "description": e.get("description", p["description"]), "connection": d["connection"],
                 "table": p["table"], "measure": p["measure"], "filters": p["filters"],
                 "time_column": e.get("time_column", tmeta.get("time_column")),
                 "dimensions": e.get("dimensions", tmeta.get("dimensions") or []),
                 "synonyms": e.get("synonyms", p["synonyms"])}
            try:
                saved.append(self.metrics.save(actor, m)["name"])     # checked again, as the person saving it
            except MetricError as err:
                skipped.append({"name": m["name"], "error": str(err), "status": err.status})
        with self.engine.begin() as conn:
            conn.execute(update(semantic_drafts).where(semantic_drafts.c.id == did).values(
                status="accepted" if saved else d["status"], accepted=json.dumps(sorted(set(d["accepted"]) | set(saved)))))
        if self.audit is not None:
            self.audit.record("sql", "semantic_model.accepted", actor, f"semantic_model:{did}",
                              {"saved": saved, "skipped": [s["name"] for s in skipped]})
        return {"saved": saved, "skipped": skipped}


def build_autopilot_router(a: Autopilot, current_principal, can_view) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql/semantic-models", tags=["sql"])

    def person(p) -> str:
        if p.is_service:
            raise HTTPException(403, "semantic models are drafted for people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except AutopilotError as e:
            raise HTTPException(e.status, str(e))

    @router.post("", status_code=201)
    async def draft(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: a.draft(person(p), body, can_view(p)))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: a.list(person(p)))

    @router.get("/{did}")
    async def get(did: str, p=Depends(current_principal)):
        return await call(lambda: a.get(person(p), did))

    @router.post("/{did}/accept")
    async def accept(did: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return await call(lambda: a.accept(person(p), did, body))

    @router.delete("/{did}", status_code=204)
    async def delete_(did: str, p=Depends(current_principal)):
        await call(lambda: a.delete(person(p), did))

    return router
