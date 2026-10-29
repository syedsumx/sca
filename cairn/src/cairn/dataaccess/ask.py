"""Ask your data: a question in plain words, answered with governed SQL.

1. **Tables**: the datasets the caller named, or the best matches for the question among the
   datasets they may see that a warehouse connection serves (all on one connection).
2. **Context**: their columns, the approved metric and join definitions over them, and the SQL that
   answered similar questions before (:mod:`cairn.context.layer`).
3. **SQL**: one read-only SELECT from the model (:mod:`cairn.ai.text2sql`).
4. **Run**: through :class:`DataAccessService`, so the caller's table permissions, purposes, column
   masking, row limit and audit apply exactly as for SQL they wrote themselves. When the warehouse
   rejects the statement, the model gets the error and one more try; a permission refusal is final.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from cairn.ai.guardrails import redact
from cairn.ai.llm import LLMError
from cairn.ai.text2sql import TextToSQL
from cairn.ai.usage import LLMBudgetExceeded
from cairn.dataaccess.service import DataAccessError, DataAccessService, Source

MAX_TABLES = 6
MAX_COLUMNS = 80


class AskError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _column(field_path: str) -> str:
    """``[version=2.0].[type=struct].address.[type=string].city`` → ``address.city``; plain paths unchanged."""
    return ".".join(p for p in field_path.split(".") if not p.startswith("["))


class AskService:
    def __init__(self, data: DataAccessService, graph, llm, context_layer=None,
                 search: Optional[Callable[[str, Callable[[str], bool], int], list[str]]] = None,
                 model: Optional[str] = None, audit=None):
        """``search(question, can_view, k)``: dataset urns ranked for the question."""
        self.data = data
        self.graph = graph
        self.layer = context_layer
        self.search = search
        self.audit = audit
        self.sql = TextToSQL(llm, model)

    @property
    def configured(self) -> bool:
        return self.sql.llm is not None

    # ---- context ------------------------------------------------------------------------
    def _served(self, urn: str) -> Optional[tuple[Source, list[str]]]:
        try:
            return self.data.source_for(urn)
        except DataAccessError:
            return None

    def _tables(self, question: str, urns: list[str], can_view: Callable[[str], bool]) -> tuple[Source, dict]:
        """``{urn: (table reference, columns)}`` on one connection."""
        candidates = list(dict.fromkeys(urns)) or (self.search(question, can_view, MAX_TABLES * 2)
                                                  if self.search else [])
        if not candidates:
            raise AskError("name the datasets to ask about; no dataset matched the question", 404)
        source, chosen = None, {}
        for urn in candidates:
            if urns and not can_view(urn):
                raise AskError("you may not see one of the datasets", 403)
            if not can_view(urn):
                continue
            served = self._served(urn)
            if served is None:
                if urns:
                    raise AskError(f"no warehouse connection serves {urn}", 404)
                continue
            src, parts = served
            if source is not None and src.connection != source.connection:
                if urns:
                    raise AskError("the datasets live on different connections; ask about one at a time")
                continue
            schema = self.graph.get_aspect(urn, "schemaMetadata") if self.graph is not None else None
            cols = [_column(f["fieldPath"]) for f in (schema or {}).get("fields", [])][:MAX_COLUMNS]
            if not cols:
                continue
            ref = ".".join(parts[-2:] if src.database or len(parts) < 3 else parts)
            source = source or src
            chosen[urn] = (ref, cols)
            if len(chosen) >= MAX_TABLES:
                break
        if source is None:
            raise AskError("none of the matching datasets is in a warehouse Cairn can query", 404)
        return source, chosen

    def _context(self, question: str, urns: set[str], can_view) -> tuple[list[dict], list[dict]]:
        if self.layer is None:
            return [], []
        defs = [d for d in self.layer.definitions() if set(d["doc"].get("datasets") or []) & urns]
        examples = [e for e in self.layer.answers(question, can_view, 5) if set(e["tables"]) & urns][:3]
        return defs, examples

    # ---- asking ---------------------------------------------------------------------------
    def ask(self, actor: str, question: str, can_view: Callable[[str], bool], urns: Optional[list[str]] = None,
            run: bool = True, limit: int = 200) -> dict:
        if not self.configured:
            raise AskError("asking questions of data needs an LLM provider (not configured)", 503)
        source, tables = self._tables(question, urns or [], can_view)
        defs, examples = self._context(question, set(tables), can_view)
        schema = {ref: cols for ref, cols in tables.values()}
        out: dict[str, Any] = {
            "question": question, "connection": source.connection, "dialect": source.dialect,
            "tables": list(tables), "definitions": [{"name": d["name"], "kind": d["kind"], "version": d["version"]}
                                                    for d in defs],
            "examples": [e["title"] for e in examples], "attempts": 0, "result": None, "error": None}
        asked = question
        for attempt in (1, 2):
            out["attempts"] = attempt
            try:
                gen = self.sql.generate(asked, schema, source.dialect, defs, examples)
            except LLMBudgetExceeded as e:
                raise AskError(f"AI budget reached: {e}", 429)
            except LLMError as e:
                raise AskError(f"the model failed: {e}", 502)
            except ValueError as e:          # not one read-only SELECT
                out.update(sql=None, explanation=None, error=f"the model's SQL was refused: {e}")
                break
            out.update(sql=gen["sql"], explanation=gen["explanation"])
            if not run:
                break
            try:
                out["result"], out["error"] = self.data.query(actor, gen["sql"], source.connection, limit=limit), None
                break
            except DataAccessError as e:
                out["error"] = str(e)
                if e.status != 400 or attempt == 2:
                    break
                asked = (f"{question}\n\nA previous attempt failed. SQL:\n{gen['sql']}\n"
                         f"Error: {str(e)[:500]}\nWrite a corrected statement.")
        if self.audit is not None:
            self.audit.record("data", "data.asked", actor, source.connection,
                              {"question": redact(question)[:500], "tables": out["tables"], "ran": run,
                               "answered": out["result"] is not None, "attempts": out["attempts"],
                               "definitions": [d["name"] for d in out["definitions"]]})
        return out
