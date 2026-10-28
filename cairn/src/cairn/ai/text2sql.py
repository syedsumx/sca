"""Text to SQL, with and without Cairn's context, so the context layer's value can be measured.

The prompt always has the schema of the tables involved. With context it also has the approved
metric and join definitions for those tables and the SQL that answered similar questions before
(see :mod:`cairn.context.layer`). ``cairn ai eval`` runs ``text2sql`` cases both ways against a
small warehouse and reports accuracy with and without context.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from cairn.ai.guardrails import UNTRUSTED_NOTICE, untrusted
from cairn.ai.llm import LLMProvider, validate_against_schema
from cairn.ai.usage import llm_task
from cairn.quality.models import validate_readonly_sql

SQL_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sql", "explanation"],
    "properties": {
        "sql": {"type": "string", "description": "one read-only SELECT statement"},
        "explanation": {"type": "string", "description": "one sentence: which tables, filters and definitions"},
    },
}

SYSTEM = f"""You write one read-only SQL SELECT statement that answers the question, for the dialect given.
Use only the tables and columns listed. When approved definitions are given, they are the business's agreed
meaning: use their expressions exactly. Past queries show how similar questions were answered; follow their
joins and filters when they fit the question.
{UNTRUSTED_NOTICE}"""

CONTEXT_MARKER = "<approved_definitions>"


def build_prompt(question: str, schema: dict[str, list[str]], dialect: str,
                 definitions: Optional[list[dict]] = None, examples: Optional[list[dict]] = None) -> str:
    parts = [f"Dialect: {dialect}", f"Question: {question}", "Tables:"]
    for table, cols in schema.items():
        parts.append(f"- {table} ({', '.join(cols)})")
    if definitions is not None:
        parts.append(CONTEXT_MARKER)
        for d in definitions:
            if d["kind"] == "metric":
                parts.append(f"- metric {d['name']} = {d['doc']['expression']}" +
                             (f" ({d['doc']['description']})" if d["doc"].get("description") else ""))
            else:
                v = d["doc"]
                parts.append(f"- join {v['source'].split(',')[1]}.{v['source_field']} = "
                             f"{v['destination'].split(',')[1]}.{v['destination_field']}")
        parts.append("</approved_definitions>")
        for e in examples or []:
            parts.append(f"<past_query title=\"{e['title']}\">\n{e['sql']}\n</past_query>")
    return untrusted("\n".join(parts))


class TextToSQL:
    def __init__(self, llm: Optional[LLMProvider], model: Optional[str] = None,
                 on_llm_call: Optional[Callable[[str, Any], None]] = None):
        self.llm = llm
        self.model = model
        self.on_llm_call = on_llm_call

    def generate(self, question: str, schema: dict[str, list[str]], dialect: str = "ansi",
                 definitions: Optional[list[dict]] = None, examples: Optional[list[dict]] = None) -> dict:
        if self.llm is None:
            raise RuntimeError("no LLM provider configured")
        prompt = build_prompt(question, schema, dialect, definitions, examples)
        with llm_task("text2sql.generate"):
            out, usage = self.llm.generate_json(SYSTEM, prompt, SQL_SCHEMA, model=self.model)
        validate_against_schema(out, SQL_SCHEMA)
        validate_readonly_sql(out["sql"])
        if self.on_llm_call:
            self.on_llm_call("text2sql.generate", usage)
        return {"sql": out["sql"].strip().rstrip(";"), "explanation": out["explanation"],
                "with_context": definitions is not None}


def same_result(a: list[tuple], b: list[tuple], ordered: bool = False) -> bool:
    """Rows equal as sets (or in order), numbers compared to 6 decimal places."""
    def norm(rows):
        out = [tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in rows]
        return out if ordered else sorted(out, key=repr)
    return norm(a) == norm(b)
