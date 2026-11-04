"""The AI builder: a question in plain words becomes a dashboard, built from governed parts.

``POST /sql/builder`` with ``{"question": "Revenue by region this year, and orders by status"}`` (optionally
``datasets``, ``save`` and ``name``):

1. **What may be used**: the metrics whose tables the person may see (:mod:`cairn.sqlworkspace.metrics`), and the
   tables Ask-your-data would choose for the question among those they may see, with their columns.
2. **A plan**: the model proposes tiles (metric queries, or workbook elements over those tables) and filter controls,
   as structured output. It never writes SQL: tiles are compiled by Cairn, so a formula reaches SQL only as
   parsed, quoted columns and literals.
3. **Checked as the person**: each tile is compiled as the person asking (a metric that does not exist, a column the
   table lacks, a dimension the metric does not allow, a table they may not see: refused). The model sees the
   refusals and gets one more try; tiles still refused are reported, not shown.
4. **A preview**, every tile run as the person (their masks and row filters apply), and with ``save`` a dashboard
   of theirs (private until they share it). Audited as ``bi.built`` (the question redacted, the tiles' kinds and
   tables).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from cairn.ai.guardrails import UNTRUSTED_NOTICE, redact, untrusted
from cairn.ai.llm import LLMError, validate_against_schema
from cairn.ai.usage import LLMBudgetExceeded, llm_task
from cairn.sqlworkspace.workspace import WorkspaceError

MAX_TILES = 8

SYSTEM = f"""You design a dashboard that answers the person's question, from the parts listed: metrics (defined
once, preferred whenever one fits) and tables with their columns. You never write SQL.

A tile is either
- {{"kind": "metric", "title": ..., "metric": {{"metrics": [metric names], "dimensions": [columns the metrics allow],
  "grain": "day"|"week"|"month"|"quarter"|"year" or omitted}}}} (metrics in one tile share a table), or
- {{"kind": "element", "title": ..., "element": {{"source": {{"table": "schema.table"}}, "group_by": [columns],
  "aggregates": [{{"name": "Revenue", "formula": "Sum([amount])"}}], "filters": [{{"column": c, "op": "=", "value": v}}],
  "sort": [{{"column": c, "desc": true}}], "limit": 100}}}}. Formulas are spreadsheet formulas over columns in square
  brackets: Sum, Count(), CountDistinct, Avg, Min, Max, Median, If, DateTrunc("month", [col]); text in double quotes.
Each tile may have "chart": {{"type": "bar"|"line"|"area"|"pie"|"number"|"table"|"stacked_bar"|"scatter", "x": column,
"y": [columns]}}. Controls filter every tile with their column: {{"id": "region", "label": "Region", "column": c,
"kind": "values"|"range"|"date_range"|"text"}}; a values control also names "source": {{"table": "schema.table"}}.
Use only the metric names, tables and columns listed. At most {MAX_TILES} tiles. {UNTRUSTED_NOTICE}"""

SCHEMA: dict = {
    "type": "object", "additionalProperties": False, "required": ["title", "tiles", "controls"],
    "properties": {
        "title": {"type": "string"},
        "tiles": {"type": "array", "items": {
            "type": "object", "required": ["kind", "title"],
            "properties": {"kind": {"type": "string", "enum": ["metric", "element"]}, "title": {"type": "string"},
                           "metric": {"type": "object"}, "element": {"type": "object"}, "chart": {"type": "object"}}}},
        "controls": {"type": "array", "items": {"type": "object"}},
    },
}


class BuilderError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Builder:
    def __init__(self, llm, model: Optional[str], ask, metrics, boards, urn_of: Callable[[str, str], Optional[str]],
                 audit=None):
        """``ask``: :class:`cairn.dataaccess.ask.AskService` (it chooses the tables); ``boards``: dashboards (tiles are
        checked, run and saved through it); ``urn_of(connection, table)``: a table's dataset urn."""
        self.llm, self.model, self.ask, self.metrics, self.boards = llm, model, ask, metrics, boards
        self.urn_of, self.audit = urn_of, audit

    def _parts(self, question: str, can_view: Callable[[str], bool], urns: Optional[list[str]]) -> tuple[Optional[str], dict, list]:
        from cairn.dataaccess.ask import AskError

        connection, tables = None, {}
        try:
            source, chosen = self.ask._tables(question, urns or [], can_view)
            connection, tables = source.connection, {ref: cols for ref, cols in chosen.values()}
        except AskError as e:
            if urns:
                raise BuilderError(str(e), e.status)
        metrics = []
        for m in self.metrics.list() if self.metrics is not None else []:
            urn = self.urn_of(m["connection"], m["table"])
            if urn and can_view(urn):
                metrics.append(m)
        if not tables and not metrics:
            raise BuilderError("no metric or table you may see matches the question; name the datasets", 404)
        return connection, tables, metrics

    def _prompt(self, question: str, tables: dict, metrics: list, refused: Optional[list] = None) -> str:
        parts = [f"Question: {question}", "Metrics:"]
        for m in metrics:
            parts.append(f"- {m['name']} ({m['label']}{', certified' if m['certified'] else ''}) on {m['table']}: "
                         f"{m['measure']}" + (f", dimensions {', '.join(m['dimensions'])}" if m["dimensions"] else "")
                         + (f", over time by {m['time_column']}" if m["time_column"] else "")
                         + (f". {m['description']}" if m["description"] else ""))
        parts.append("Tables:")
        for ref, cols in tables.items():
            parts.append(f"- {ref} ({', '.join(cols)})")
        for r in refused or []:
            parts.append(f"A previous tile was refused: {json.dumps(r['tile'])[:600]} -> {r['error']}")
        return untrusted("\n".join(parts))

    def _check(self, actor: str, plan: dict, connection: Optional[str], allowed: set[str]) -> tuple[list[dict], list[dict], list[dict]]:
        tiles, refused = [], []
        for t in (plan.get("tiles") or [])[:MAX_TILES]:
            if t.get("kind") == "metric":
                names = (t.get("metric") or {}).get("metrics") or []
                off = [n for n in names if n not in allowed] if isinstance(names, list) else ["?"]
                if off:
                    refused.append({"tile": t, "error": f"metric {off[0]} is not one of the metrics listed"})
                    continue
            tile = {"title": str(t.get("title") or "")[:256], "width": 1}
            if t.get("chart") is not None:
                tile["chart"] = t["chart"]
            if t.get("kind") == "metric":
                tile["metric"] = t.get("metric") or {}
            else:
                tile["element"], tile["connection"] = t.get("element") or {}, connection or ""
            try:
                tiles.append(self.boards._tile(actor, tile))
            except WorkspaceError as e:
                refused.append({"tile": t, "error": str(e)})
        controls = []
        for c in plan.get("controls") or []:
            if isinstance(c, dict) and isinstance(c.get("source"), dict) and connection:
                c = {**c, "source": {"connection": connection, **c["source"]}}
            try:
                controls += self.boards._controls([c], len(tiles))
            except WorkspaceError as e:
                refused.append({"control": c, "error": str(e)})
        return tiles, controls, refused

    def build(self, actor: str, question: str, can_view: Callable[[str], bool], urns: Optional[list[str]] = None,
              save: bool = False, name: Optional[str] = None) -> dict:
        if self.llm is None:
            raise BuilderError("building dashboards from a question needs an LLM provider (not configured)", 503)
        question = str(question or "").strip()
        if not 3 <= len(question) <= 2000:
            raise BuilderError("question: 3 to 2,000 characters")
        connection, tables, metrics = self._parts(question, can_view, urns)
        refused: list[dict] = []
        tiles: list[dict] = []
        controls: list[dict] = []
        plan: dict = {}
        for _attempt in (1, 2):
            try:
                with llm_task("bi.build"):
                    plan, _ = self.llm.generate_json(SYSTEM, self._prompt(question, tables, metrics, refused), SCHEMA,
                                                     model=self.model)
                validate_against_schema(plan, SCHEMA)
            except LLMBudgetExceeded as e:
                raise BuilderError(f"AI budget reached: {e}", 429)
            except LLMError as e:
                raise BuilderError(f"the model failed: {e}", 502)
            except ValueError as e:
                raise BuilderError(f"the model's plan was not a dashboard: {e}", 502)
            tiles, controls, refused = self._check(actor, plan, connection or (metrics[0]["connection"] if metrics else None),
                                                   {m["name"] for m in metrics})
            if not refused:
                break
        if not tiles:
            raise BuilderError("no tile of the plan could be built: " + "; ".join(r["error"] for r in refused)[:600], 422)
        preview = []
        for t in tiles:
            try:
                preview.append(self.boards._run_metric(actor, t, []) if t.get("metric") is not None
                               else self.boards._run_element(actor, t, [], []))
            except Exception as e:  # noqa: BLE001 - a tile that fails to run shows its error
                if not hasattr(e, "status"):
                    raise
                preview.append({**t, "error": str(e), "status": e.status})
        title = str(plan.get("title") or question)[:256]
        out: dict[str, Any] = {"title": title, "tiles": preview, "controls": controls, "refused": refused,
                               "metrics": [m["name"] for m in metrics], "tables": list(tables), "dashboard": None}
        if save:
            out["dashboard"] = self.boards.save_dashboard(actor, {"name": (name or title)[:256], "tiles": tiles,
                                                                  "controls": controls, "shared": False})
        if self.audit is not None:
            self.audit.record("sql", "bi.built", actor, out["dashboard"]["id"] if out["dashboard"] else None,
                              {"question": redact(question)[:500], "tiles": [("metric" if t.get("metric") else "element")
                                                                           for t in tiles],
                               "refused": len(refused), "tables": list(tables), "saved": bool(save)})
        return out


def build_builder_router(b: Builder, current_principal, can_view) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql/builder", tags=["sql"])

    @router.post("")
    async def build(body: dict = Body(...), p=Depends(current_principal)):
        if p.is_service:
            raise HTTPException(403, "dashboards are built for people")
        datasets = body.get("datasets")
        if datasets is not None and (not isinstance(datasets, list) or not all(isinstance(d, str) for d in datasets)):
            raise HTTPException(400, "datasets: a list of dataset urns")
        try:
            return await run_in_threadpool(lambda: b.build(p.urn, body.get("question"), can_view(p), datasets,
                                                           bool(body.get("save")), body.get("name")))
        except BuilderError as e:
            raise HTTPException(e.status, str(e))

    return router
