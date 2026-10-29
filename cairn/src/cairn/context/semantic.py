"""A semantic layer for agents: governed metrics imported from where teams already define them,
and queried by compiling SQL from the definition (no model guessing joins or aggregations).

* **Import** dbt Semantic Layer (MetricFlow) YAML, Cube schema YAML or LookML. Each metric becomes a
  ``METRIC_DEFINITION`` finding in the context inbox, with its table, dimensions and time dimension;
  approving it makes it a governed definition (versioned, like mined ones).
* **Query** an approved metric by name, with dimensions, filters on those dimensions only, and a
  time grain: the SQL is built from the definition with sqlglot and runs through data access, so the
  caller's permissions, purposes and masking apply. Filters never take SQL.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional

import sqlglot
import yaml
from sqlglot import exp

AGG = {"sum": "SUM({})", "count": "COUNT({})", "count_distinct": "COUNT(DISTINCT {})", "countdistinct": "COUNT(DISTINCT {})",
       "average": "AVG({})", "avg": "AVG({})", "min": "MIN({})", "max": "MAX({})", "sum_boolean": "SUM(CASE WHEN {} THEN 1 ELSE 0 END)"}
GRAINS = ("day", "week", "month", "quarter", "year")
OPS = {"=": exp.EQ, "!=": exp.NEQ, ">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}


class SemanticError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _metric(name: str, expression: str, table: str, dimensions: list[str], time_dimension: Optional[str],
            description: str, source: str, ref: str, filter_: Optional[str] = None) -> dict:
    return {"name": name, "expression": expression, "table": table, "dimensions": sorted(set(dimensions)),
            "time_dimension": time_dimension, "description": description or "", "source": source, "ref": ref,
            "filter": filter_}


# ---- dbt Semantic Layer (MetricFlow) ----------------------------------------------------------------
def _ref_table(model: str) -> str:
    m = re.search(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)", model or "")
    return m.group(1) if m else (model or "")


def parse_metricflow(text: str, ref: str = "semantic_models.yml") -> list[dict]:
    doc = yaml.safe_load(text) or {}
    models = {}
    for sm in doc.get("semantic_models") or []:
        measures = {}
        for m in sm.get("measures") or []:
            agg = AGG.get(str(m.get("agg", "sum")).lower())
            if agg is None:
                continue
            measures[m["name"]] = agg.format(m.get("expr") or m["name"])
        dims = [d.get("expr") if isinstance(d.get("expr"), str) and re.fullmatch(r"\w+", d["expr"]) else d["name"]
                for d in sm.get("dimensions") or []]
        time = (sm.get("defaults") or {}).get("agg_time_dimension") or next(
            (d["name"] for d in sm.get("dimensions") or [] if d.get("type") == "time"), None)
        models[sm["name"]] = {"table": _ref_table(sm.get("model", "")), "measures": measures,
                              "dimensions": [d for d in dims if d], "time": time}

    def find(measure: str) -> Optional[tuple[dict, str]]:
        for mdl in models.values():
            if measure in mdl["measures"]:
                return mdl, mdl["measures"][measure]
        return None

    out, by_name = [], {}
    for metric in doc.get("metrics") or []:
        tp, kind = metric.get("type_params") or {}, str(metric.get("type", "simple")).lower()
        measure = tp.get("measure")
        measure = measure.get("name") if isinstance(measure, dict) else measure
        if kind == "simple" and measure and (hit := find(measure)):
            mdl, expr = hit
        elif kind == "ratio" and (num := find(_name_of(tp.get("numerator")))) and \
                (den := find(_name_of(tp.get("denominator")))):
            mdl, expr = num[0], f"{num[1]} / NULLIF({den[1]}, 0)"
        elif kind == "derived" and tp.get("expr"):
            parts = {_name_of(m): by_name.get(_name_of(m)) for m in tp.get("metrics") or []}
            if not parts or any(v is None for v in parts.values()):
                continue
            expr = tp["expr"]
            for n, m in parts.items():
                expr = re.sub(rf"\b{re.escape(n)}\b", f"({m['expression']})", expr)
            mdl = {"table": next(iter(parts.values()))["table"], "dimensions": next(iter(parts.values()))["dimensions"],
                   "time": next(iter(parts.values()))["time_dimension"]}
        else:
            continue
        m = _metric(metric["name"], expr, mdl["table"], mdl["dimensions"], mdl["time"], metric.get("description", ""),
                    "dbt-semantic-layer", ref)
        by_name[m["name"]] = m
        out.append(m)
    return out


def _name_of(v: Any) -> str:
    return v.get("name", "") if isinstance(v, dict) else str(v or "")


# ---- Cube --------------------------------------------------------------------------------------------
def parse_cube(text: str, ref: str = "schema.yml") -> list[dict]:
    doc = yaml.safe_load(text) or {}
    out = []
    for cube in doc.get("cubes") or []:
        table = cube.get("sql_table") or ""
        if not table:
            continue
        dims = [re.sub(r"\{CUBE\}\.", "", d.get("sql", d["name"])) for d in cube.get("dimensions") or []]
        dims = [d for d in dims if re.fullmatch(r"\w+", d)]
        time = next((re.sub(r"\{CUBE\}\.", "", d.get("sql", d["name"])) for d in cube.get("dimensions") or []
                     if d.get("type") == "time"), None)
        for m in cube.get("measures") or []:
            t = str(m.get("type", "count")).lower()
            sql = re.sub(r"\{CUBE\}\.", "", str(m.get("sql") or "*"))
            if t == "number":
                expr = sql
            elif (agg := AGG.get(t.replace("_", ""))) is not None or (agg := AGG.get(t)) is not None:
                expr = agg.format(sql)
            else:
                continue
            out.append(_metric(f"{cube['name']}.{m['name']}", expr, table,
                               dims, time, m.get("description", ""), "cube", ref))
    return out


# ---- LookML ------------------------------------------------------------------------------------------
_BLOCK = re.compile(r"\b(measure|dimension|dimension_group)\s*:\s*(\w+)\s*\{(.*?)\n\s*\}", re.S)


def _prop(body: str, key: str) -> Optional[str]:
    m = re.search(rf"\b{key}\s*:\s*(.+?)\s*;;", body, re.S) if key == "sql" else \
        re.search(rf"\b{key}\s*:\s*\"?([^\"\n]+)\"?", body)
    return m.group(1).strip() if m else None


def parse_lookml(text: str, ref: str = "view.lkml") -> list[dict]:
    out = []
    for view in re.finditer(r"\bview\s*:\s*(\w+)\s*\{(.*?)\n\}", text, re.S):
        name, body = view.group(1), view.group(2)
        table = (_prop(body, "sql_table_name") or "").rstrip(";").strip()
        if not table:
            continue
        dims, time, measures = [], None, []
        for kind, field, fbody in _BLOCK.findall(body):
            sql = (_prop(fbody, "sql") or f"${{TABLE}}.{field}").replace("${TABLE}.", "")
            if kind in ("dimension", "dimension_group") and re.fullmatch(r"\w+", sql):
                dims.append(sql)
                if kind == "dimension_group" or _prop(fbody, "type") == "time":
                    time = time or sql
            elif kind == "measure":
                measures.append((field, _prop(fbody, "type") or "count", sql, _prop(fbody, "description") or ""))
        for field, t, sql, desc in measures:
            t = t.lower()
            if t == "number":
                expr = sql
            elif (agg := AGG.get(t)) is not None:
                expr = agg.format("*" if t == "count" and not re.fullmatch(r"\w+", sql) else sql)
            else:
                continue
            if re.search(r"\$\{\w+\}", expr):                 # references to other fields: not supported
                continue
            out.append(_metric(f"{name}.{field}", expr, table, dims, time, desc, "lookml", ref))
    return out


PARSERS: dict[str, Callable[[str, str], list[dict]]] = {"metricflow": parse_metricflow, "cube": parse_cube,
                                                        "lookml": parse_lookml}


# ---- import as findings --------------------------------------------------------------------------------
def findings_for(metrics: Iterable[dict], datasets: list[str]):
    """``METRIC_DEFINITION`` findings for imported metrics, mapped to catalog datasets."""
    from cairn.context.miner import Finding, map_table

    out = []
    for m in metrics:
        urn = map_table(m["table"], datasets)
        out.append(Finding(
            type="METRIC_DEFINITION", key=("semantic", m["source"], m["name"].lower()),
            title=f"Metric “{m['name']}” = {m['expression']}",
            summary_md=f"`{m['name']}` from {m['source']} ({m['ref']}): `{m['expression']}` over `{m['table']}`"
                       + (f", by {', '.join(m['dimensions'][:8])}" if m["dimensions"] else ""),
            confidence=0.9 if urn else 0.6, entities=[urn] if urn else [],
            evidence=[{"source": m["source"], "ref": m["ref"], "snippet": m["expression"][:300]}],
            proposed_changes=[{"kind": "create_term", "entity_urn": None, "field_path": None,
                               "value": {"name": m["name"], "definition": m["description"] or f"Computed as `{m['expression']}`.",
                                         "expression": m["expression"]}}],
            data={"name": m["name"], "canonical": m["expression"],
                  "semantic": {k: m[k] for k in ("table", "dimensions", "time_dimension", "filter", "source", "ref")},
                  "description": m["description"]}))
    return out


# ---- query ------------------------------------------------------------------------------------------------
def _lit(v: Any) -> exp.Expression:
    return exp.Literal.number(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else exp.Literal.string(str(v))


def compile_query(definition: dict, table_ref: str, dialect: str, dimensions: list[str], filters: list[dict],
                  time_grain: Optional[str], limit: int) -> str:
    """SQL for an approved metric: the definition's expression, grouped by allowed dimensions."""
    doc = definition["doc"]
    allowed = set(doc.get("dimensions") or [])
    time_dim = doc.get("time_dimension")
    bad = [d for d in dimensions if d not in allowed]
    if bad:
        raise SemanticError(f"{definition['name']} has no dimension {bad[0]!r}; it has {sorted(allowed)}")
    if time_grain and time_grain not in GRAINS:
        raise SemanticError(f"time_grain is one of {list(GRAINS)}")
    if time_grain and not time_dim:
        raise SemanticError(f"{definition['name']} has no time dimension")
    try:
        metric = sqlglot.parse_one(doc["expression"], read=dialect)
    except sqlglot.errors.ParseError as e:
        raise SemanticError(f"the definition's expression does not parse: {e}") from None
    if any(isinstance(n, (exp.Select, exp.Subquery)) for n in metric.walk()):
        raise SemanticError("the definition's expression may not contain a subquery")
    cols: list[exp.Expression] = [exp.column(d, quoted=False) for d in dimensions]
    if time_grain:
        cols.insert(0, exp.alias_(exp.func("DATE_TRUNC", exp.Literal.string(time_grain), exp.column(time_dim)), "period"))
    name = re.sub(r"\W", "_", definition["name"].split(".")[-1]).lower()
    q = exp.select(*cols, exp.alias_(metric, name)).from_(sqlglot.parse_one(table_ref, into=exp.Table, read=dialect))
    for f in filters:
        dim, op, value = f.get("dimension"), f.get("op", "="), f.get("value")
        if dim not in allowed and dim != time_dim:
            raise SemanticError(f"filters may only use the metric's dimensions; {dim!r} is not one")
        if op == "in":
            if not isinstance(value, list) or not value:
                raise SemanticError("an 'in' filter takes a non-empty list")
            q = q.where(exp.In(this=exp.column(dim), expressions=[_lit(v) for v in value[:100]]))
        elif op in OPS:
            q = q.where(OPS[op](this=exp.column(dim), expression=_lit(value)))
        else:
            raise SemanticError(f"op is one of {list(OPS) + ['in']}")
    if doc.get("filter"):
        q = q.where(sqlglot.parse_one(doc["filter"], read=dialect))
    group = list(range(1, len(cols) + 1))
    if group:
        q = q.group_by(*[exp.Literal.number(i) for i in group]).order_by(*[exp.Literal.number(i) for i in group])
    return q.limit(max(1, min(limit, 1000))).sql(dialect=dialect)


class SemanticService:
    """Imports semantic-layer definitions as findings, lists governed metrics, and queries them."""

    def __init__(self, store, data_access=None, datasets: Optional[Callable[[], list[str]]] = None, audit=None):
        self.store = store                 # ContextStore (its layer holds approved definitions)
        self.data = data_access
        self.datasets = datasets or (lambda: [])
        self.audit = audit

    def import_definitions(self, fmt: str, text: str, ref: str, actor: str) -> dict:
        parser = PARSERS.get(fmt)
        if parser is None:
            raise SemanticError(f"format is one of {sorted(PARSERS)}")
        try:
            metrics = parser(text, ref)
        except yaml.YAMLError as e:
            raise SemanticError(f"not valid YAML: {e}") from None
        if not metrics:
            raise SemanticError("no metrics found in the file")
        findings = findings_for(metrics, self.datasets())
        stats = self.store.upsert(findings)
        if self.audit is not None:
            self.audit.record("context", "semantic.imported", actor, ref, {"format": fmt, "metrics": len(metrics), **stats})
        return {"metrics": len(metrics), "mapped": sum(1 for f in findings if f.entities), **stats,
                "names": [m["name"] for m in metrics][:200]}

    def metrics(self, can_view: Callable[[str], bool], query: str = "") -> list[dict]:
        q = query.lower().strip()
        out = []
        for d in self.store.layer.definitions():
            doc = d["doc"]
            if d["kind"] != "metric" or not doc.get("datasets") or not all(can_view(u) for u in doc["datasets"]):
                continue
            if q and q not in d["name"].lower() and q not in (doc.get("description") or "").lower():
                continue
            out.append({"name": d["name"], "version": d["version"], "expression": doc["expression"],
                        "description": doc.get("description") or "", "dataset": doc["datasets"][0],
                        "dimensions": doc.get("dimensions") or [], "time_dimension": doc.get("time_dimension"),
                        "source": doc.get("source") or "mined", "queryable": True})
        return out

    def query(self, actor: str, name: str, can_view: Callable[[str], bool], dimensions: Optional[list[str]] = None,
              filters: Optional[list[dict]] = None, time_grain: Optional[str] = None, limit: int = 200) -> dict:
        from cairn.dataaccess.service import DataAccessError

        if self.data is None:
            raise SemanticError("querying metrics needs a warehouse connection", 503)
        d = next((x for x in self.store.layer.definitions() if x["kind"] == "metric" and x["name"] == name), None)
        if d is None or not d["doc"].get("datasets"):
            raise SemanticError(f"no approved metric named {name!r}", 404)
        urn = d["doc"]["datasets"][0]
        if not can_view(urn):
            raise SemanticError("you may not see this metric's dataset", 403)
        try:
            source, parts = self.data.source_for(urn)
        except DataAccessError as e:
            raise SemanticError(str(e), e.status) from None
        table_ref = ".".join(parts[-2:] if source.database or len(parts) < 3 else parts)
        sql = compile_query(d, table_ref, source.dialect, dimensions or [], filters or [], time_grain, limit)
        try:
            result = self.data.query(actor, sql, urn=urn, limit=limit)
        except DataAccessError as e:
            raise SemanticError(str(e), e.status) from None
        return {"metric": name, "version": d["version"], "sql": sql, "dataset": urn, "result": result}
