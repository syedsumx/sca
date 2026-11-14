"""The SQL editor's assistant (L163): write a statement from a question, explain a statement in plain words, and fix
one that failed. Governed like everything else in the editor:

* **What the model sees.** The schema (table and column names) of the tables the statement names that the person may
  see on the editor's connection (:meth:`SqlWorkspace.schema`, filtered by ``can_view``), the connection's dialect,
  and the person's own text: their SQL, their question, the error their run got. Never rows: the assistant reads no
  data. The person's text goes in as untrusted (:func:`cairn.ai.guardrails.untrusted`), so instructions in a comment
  or a string literal are data to the model, not orders.
* **What comes back.** A statement the assistant proposes is checked to be one read-only statement before it is
  shown; it is never run by the assistant. The person reads it, and runs it (as themselves, checked and masked as any
  statement) if they want to.
* **Write** is Ask-your-data (:mod:`cairn.dataaccess.ask`) without running: its tables, approved definitions, metrics
  and past answers, for the editor's connection.
* Model calls go through the AI gateway's budgets and usage like every other (``llm_task``), and each request is
  audited (``sql.assisted``) with the action and the tables, never the text.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from cairn.ai.guardrails import UNTRUSTED_NOTICE, untrusted
from cairn.ai.llm import LLMError, validate_against_schema
from cairn.ai.usage import LLMBudgetExceeded, llm_task
from cairn.quality.models import validate_readonly_sql

MAX_TABLES = 8
MAX_COLUMNS = 80

EXPLAIN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "steps"],
    "properties": {
        "summary": {"type": "string", "description": "one or two sentences: what the statement answers"},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "what it does, in order: tables read, joins, filters, groups, ordering, limits"},
        "cautions": {"type": "array", "items": {"type": "string"},
                     "description": "things that may surprise: fan-out joins, NULL handling, missing filters"},
    },
}

FIX_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sql", "explanation"],
    "properties": {
        "sql": {"type": "string", "description": "the corrected statement: one read-only SELECT"},
        "explanation": {"type": "string", "description": "one or two sentences: what was wrong and what changed"},
    },
}

EXPLAIN_SYSTEM = f"""You explain a SQL statement to the person who wrote or opened it, in plain words for someone who
knows their data but not every SQL detail. Use the table and column names as they are. Say what the result answers,
then the steps in order. Add cautions only for real risks in this statement. Do not invent tables or columns.
{UNTRUSTED_NOTICE}"""

FIX_SYSTEM = f"""You fix a SQL statement that failed, for the dialect given. Use only the tables and columns listed.
Keep the person's intent, their aliases and their formatting where you can; change only what the error needs. The
result must be one read-only SELECT (or WITH ... SELECT) statement.
{UNTRUSTED_NOTICE}"""


class AssistError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _names_in(sql: str) -> set[str]:
    """The lower-cased words and dotted names in ``sql`` (a statement that does not parse is still read)."""
    return {m.lower() for m in re.findall(r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*", sql.replace('"', "").replace("`", ""))}


class SqlAssistant:
    def __init__(self, workspace, llm, model: Optional[str] = None, audit=None, ask=None):
        """``workspace``: the SqlWorkspace (its schema browser says what the person may see); ``ask``: the
        AskService, for writing from a question."""
        self.ws, self.llm, self.model, self.audit, self.ask = workspace, llm, model, audit, ask

    @property
    def configured(self) -> bool:
        return self.llm is not None

    # ---- what the model is told ------------------------------------------------------------------------------
    def _tables(self, sql: str, connection: str, can_view: Callable[[str], bool]) -> tuple[str, dict[str, list[str]]]:
        """The connection's dialect, and ``{table: columns}`` for the tables ``sql`` names that the person may see
        (as ``schema.table``, or ``table`` alone when only that is written)."""
        src = next((s for s in self.ws.data.sources() if s.connection == connection), None)
        if src is None:
            raise AssistError(f"unknown connection {connection!r}", 404)
        words = _names_in(sql)
        chosen: dict[str, list[str]] = {}
        for t in self.ws.schema(connection, can_view):            # only what the person may see
            name = t["name"].lower()
            parts = name.split(".")
            if name in words or ".".join(parts[-2:]) in words or any(w.endswith("." + parts[-1]) or w == parts[-1]
                                                                    for w in words):
                chosen[t["name"]] = [c["name"] for c in t["columns"]][:MAX_COLUMNS]
            if len(chosen) >= MAX_TABLES:
                break
        return src.dialect, chosen

    @staticmethod
    def _prompt(dialect: str, tables: dict[str, list[str]], **parts: str) -> str:
        lines = [f"Dialect: {dialect}", "Tables you may use:"]
        lines += [f"- {t} ({', '.join(cols)})" for t, cols in tables.items()] or ["- (none of the statement's tables)"]
        for k, v in parts.items():
            lines.append(f"<{k}>\n{v}\n</{k}>")
        return untrusted("\n".join(lines))

    def _call(self, task: str, system: str, prompt: str, schema: dict) -> dict:
        if not self.configured:
            raise AssistError("the SQL assistant needs an LLM provider (not configured)", 503)
        try:
            with llm_task(task):
                out, _ = self.llm.generate_json(system, prompt, schema, model=self.model, max_tokens=4000)
            validate_against_schema(out, schema)
        except LLMBudgetExceeded as e:
            raise AssistError(f"AI budget reached: {e}", 429)
        except LLMError as e:
            raise AssistError(f"the model failed: {e}", 502)
        except ValueError as e:
            raise AssistError(f"the model's answer was not usable: {e}", 502)
        return out

    def _audit(self, actor: str, action: str, connection: str, tables: list[str], **extra: Any) -> None:
        if self.audit is not None:
            self.audit.record("sql", "sql.assisted", actor, connection, {"action": action, "tables": tables, **extra})

    # ---- the actions ------------------------------------------------------------------------------------------
    def explain(self, actor: str, sql: str, connection: str, can_view: Callable[[str], bool]) -> dict:
        dialect, tables = self._tables(sql, connection, can_view)
        out = self._call("sql.assist.explain", EXPLAIN_SYSTEM, self._prompt(dialect, tables, statement=sql), EXPLAIN_SCHEMA)
        self._audit(actor, "explain", connection, list(tables))
        return {"summary": out["summary"], "steps": out["steps"], "cautions": out.get("cautions") or [],
                "tables": list(tables)}

    def fix(self, actor: str, sql: str, error: str, connection: str, can_view: Callable[[str], bool]) -> dict:
        dialect, tables = self._tables(sql, connection, can_view)
        if not tables:
            raise AssistError("none of the statement's tables is one you may query on this connection: the assistant "
                              "has nothing to fix it with", 422)
        out = self._call("sql.assist.fix", FIX_SYSTEM,
                         self._prompt(dialect, tables, statement=sql, error=error[:2000]), FIX_SCHEMA)
        fixed = out["sql"].strip().rstrip(";").strip()
        try:
            validate_readonly_sql(fixed)
        except ValueError as e:
            self._audit(actor, "fix", connection, list(tables), refused=True)
            raise AssistError(f"the model's statement was refused: {e}", 422)
        self._audit(actor, "fix", connection, list(tables))
        return {"sql": fixed, "explanation": out["explanation"], "tables": list(tables)}

    def write(self, actor: str, question: str, connection: str, can_view: Callable[[str], bool]) -> dict:
        """A statement for ``question`` (Ask-your-data, not run), on the editor's connection when the tables that
        answer it are there; ``connection`` in the answer says where it was written for."""
        if self.ask is None or not self.ask.configured:
            raise AssistError("writing SQL from a question needs an LLM provider (not configured)", 503)
        from cairn.dataaccess.ask import AskError

        try:
            answer = self.ask.ask(actor, question, can_view, None, run=False)
        except AskError as e:
            raise AssistError(str(e), e.status)
        out = {k: answer.get(k) for k in ("sql", "explanation", "connection", "dialect", "tables", "definitions",
                                          "examples", "error", "ontology")}
        out["same_connection"] = answer.get("connection") == connection
        self._audit(actor, "write", answer.get("connection") or connection, answer.get("tables") or [])
        return out


class ExplainIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    connection: str = Field(min_length=1)


class FixIn(ExplainIn):
    error: str = Field(min_length=1, max_length=4_000)


class WriteIn(BaseModel):
    question: str = Field(min_length=3, max_length=2_000)
    connection: str = Field(min_length=1)


def build_router(assistant: SqlAssistant, current_principal, can_view) -> Any:
    from fastapi import APIRouter, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql/assist", tags=["sql"])

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "the SQL assistant is for people")
        return p

    async def call(fn, *a):
        try:
            return await run_in_threadpool(fn, *a)
        except AssistError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/explain")
    async def explain(body: ExplainIn, p=Depends(current_principal)):
        return await call(assistant.explain, person(p).urn, body.sql, body.connection, can_view(p))

    @router.post("/fix")
    async def fix(body: FixIn, p=Depends(current_principal)):
        return await call(assistant.fix, person(p).urn, body.sql, body.error, body.connection, can_view(p))

    @router.post("/write")
    async def write(body: WriteIn, p=Depends(current_principal)):
        return await call(assistant.write, person(p).urn, body.question, body.connection, can_view(p))

    return router
