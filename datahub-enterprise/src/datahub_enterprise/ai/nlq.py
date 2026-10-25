"""Natural-language metadata querying: question → typed SearchPlan → execution → grounded answer.

The LLM never writes queries. It fills a strictly-typed ``SearchPlan`` (structured
outputs), which DHE validates and executes against GMS search/lineage APIs. Answers
are synthesized only from the retrieved, authorization-filtered results and cite
URNs. Without an LLM, a deterministic heuristic parser produces the plan.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from datahub_enterprise.ai.catalog import FILTER_FIELDS, Catalog
from datahub_enterprise.ai.guardrails import UNTRUSTED_NOTICE, filter_viewable, redact, untrusted
from datahub_enterprise.ai.llm import LLMError, LLMProvider, validate_against_schema

logger = logging.getLogger(__name__)

ENTITY_TYPE_ENUM = ["dataset", "dashboard", "chart", "dataJob", "dataFlow", "glossaryTerm", "domain",
                    "dataProduct", "mlModel", "container"]

SEARCH_PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "query", "entity_types", "filters", "lineage", "limit"],
    "properties": {
        "intent": {"type": "string", "enum": ["find", "lineage", "explain", "quality", "access"]},
        "query": {"type": "string", "description": "free-text keywords for search, or '*'"},
        "entity_types": {"type": "array", "items": {"type": "string", "enum": ENTITY_TYPE_ENUM}},
        "filters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["field", "condition", "values", "negated"],
            "properties": {
                "field": {"type": "string", "enum": sorted(FILTER_FIELDS)},
                "condition": {"type": "string", "enum": ["EQUAL", "CONTAIN", "START_WITH"]},
                "values": {"type": "array", "items": {"type": "string"}},
                "negated": {"type": "boolean"},
            }}},
        "lineage": {
            "type": "object", "additionalProperties": False, "required": ["direction", "target", "max_hops"],
            "properties": {
                "direction": {"type": "string", "enum": ["NONE", "UPSTREAM", "DOWNSTREAM"]},
                "target": {"type": "string", "description": "urn or name of the asset lineage is relative to"},
                "max_hops": {"type": "integer"},
            }},
        "limit": {"type": "integer"},
    },
}

ANSWER_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "citations"],
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
    },
}

PLAN_SYSTEM = f"""You translate questions about a company's data catalog (DataHub) into a search plan.
Fill every field of the schema. Rules:
- filters may only use values from the provided vocabulary when one is given; tags are urn:li:tag:<name>,
  glossary terms urn:li:glossaryTerm:<path>, owners urn:li:corpuser:<id> or urn:li:corpGroup:<id>,
  domains urn:li:domain:<id>, platform urn:li:dataPlatform:<name>.
- For questions about sensitive/PII data use field glossaryTerms, condition START_WITH,
  value urn:li:glossaryTerm:Classification.PII.
- Use lineage.direction NONE unless the user asks what feeds into / depends on an asset.
- limit between 1 and 50 (default 10). query '*' when only filters apply.
{UNTRUSTED_NOTICE}"""

ANSWER_SYSTEM = f"""You answer questions about a data catalog using ONLY the provided search results.
Be concise. Cite every asset you mention by its exact urn in `citations`. If the results do not contain the
answer, say so plainly; never invent assets, columns or owners.
{UNTRUSTED_NOTICE}"""

_PLATFORMS = ["snowflake", "bigquery", "postgres", "mysql", "redshift", "databricks", "hive", "kafka", "s3",
              "looker", "tableau", "powerbi", "dbt", "airflow", "oracle", "mssql", "glue", "trino", "clickhouse"]
_TYPE_WORDS = {"table": "dataset", "tables": "dataset", "dataset": "dataset", "datasets": "dataset",
               "view": "dataset", "views": "dataset", "topic": "dataset", "topics": "dataset",
               "dashboard": "dashboard", "dashboards": "dashboard", "chart": "chart", "charts": "chart",
               "pipeline": "dataFlow", "pipelines": "dataFlow", "job": "dataJob", "jobs": "dataJob",
               "term": "glossaryTerm", "terms": "glossaryTerm", "model": "mlModel", "models": "mlModel"}
_STOP = {"show", "me", "find", "list", "all", "the", "a", "an", "which", "what", "are", "is", "of", "with", "that",
         "in", "for", "contain", "contains", "containing", "have", "has", "data", "any", "on", "to", "from", "do",
         "we", "our", "there", "where", "by", "about", "and", "or", "please", "get", "give"}


def heuristic_plan(question: str) -> dict:
    q = question.strip()
    lower = q.lower()
    plan: dict[str, Any] = {"intent": "find", "query": "*", "entity_types": [], "filters": [],
                            "lineage": {"direction": "NONE", "target": "", "max_hops": 3}, "limit": 10}
    m = re.search(r"(downstream|depends on|depend on|consumers? of|uses?|upstream|feeds?|sources? of)\s+(?:of\s+)?"
                  r"(urn:li:\S+|[\w.\-]+)", q, re.I)
    if m:
        word = m.group(1).lower()
        direction = "UPSTREAM" if word.startswith(("upstream", "feed", "source")) else "DOWNSTREAM"
        plan["intent"] = "lineage"
        plan["lineage"] = {"direction": direction, "target": m.group(2), "max_hops": 3}
        lower = lower.replace(m.group(0).lower(), " ")
    if re.search(r"\b(pii|personal data|sensitive|gdpr)\b", lower):
        plan["filters"].append({"field": "glossaryTerms", "condition": "START_WITH",
                                "values": ["urn:li:glossaryTerm:Classification.PII"], "negated": False})
        lower = re.sub(r"\b(pii|personal data|sensitive|gdpr)\b", " ", lower)
    for t in re.findall(r"tag(?:ged)?\s+(?:with\s+)?['\"]?([\w\-]+)", lower):
        plan["filters"].append({"field": "tags", "condition": "EQUAL", "values": [f"urn:li:tag:{t}"], "negated": False})
    lower = re.sub(r"tag(?:ged)?\s+(?:with\s+)?['\"]?[\w\-]+['\"]?", " ", lower)
    for o in re.findall(r"owned by\s+([\w.\-@]+)", lower):
        plan["filters"].append({"field": "owners", "condition": "EQUAL", "values": [f"urn:li:corpuser:{o}"],
                                "negated": False})
    lower = re.sub(r"owned by\s+[\w.\-@]+", " ", lower)
    for d in re.findall(r"(?:in|from) (?:the )?domain\s+([\w\-]+)", lower):
        plan["filters"].append({"field": "domains", "condition": "CONTAIN", "values": [d], "negated": False})
    lower = re.sub(r"(?:in|from) (?:the )?domain\s+[\w\-]+", " ", lower)
    for p in _PLATFORMS:
        if re.search(rf"\b{p}\b", lower):
            plan["filters"].append({"field": "platform", "condition": "EQUAL",
                                    "values": [f"urn:li:dataPlatform:{p}"], "negated": False})
            lower = re.sub(rf"\b{p}\b", " ", lower)
    if re.search(r"\b(fail|failing|broken|stale|quality|incident)", lower):
        plan["intent"] = "quality"
    words = re.findall(r"[a-z0-9_.\-]+", lower)
    types = []
    for w in words:
        if w in _TYPE_WORDS and _TYPE_WORDS[w] not in types:
            types.append(_TYPE_WORDS[w])
    plan["entity_types"] = types
    keywords = [w for w in words if w not in _STOP and w not in _TYPE_WORDS
                and not re.fullmatch(r"(fail|failing|broken|stale|quality|incidents?)", w)]
    plan["query"] = " ".join(keywords) or "*"
    return plan


def normalize_plan(plan: dict) -> dict:
    plan["limit"] = max(1, min(int(plan.get("limit") or 10), 50))
    plan["filters"] = [f for f in plan.get("filters", []) if f.get("field") in FILTER_FIELDS and f.get("values")]
    lin = plan.get("lineage") or {}
    lin["max_hops"] = max(1, min(int(lin.get("max_hops") or 3), 3))
    plan["lineage"] = lin
    return plan


class NaturalLanguageQuery:
    def __init__(self, catalog: Catalog, llm: Optional[LLMProvider] = None, model: Optional[str] = None,
                 on_llm_call: Optional[Callable[[str, Any], None]] = None):
        self.catalog = catalog
        self.llm = llm
        self.model = model
        self.on_llm_call = on_llm_call

    def plan(self, question: str) -> tuple[dict, str]:
        if self.llm is not None:
            vocab = self.catalog.facet_vocabulary()
            user = f"Question: {question}\n\nFilter vocabulary (facet values present in the catalog):\n" + \
                   untrusted(redact(str(vocab)))
            try:
                plan, usage = self.llm.generate_json(PLAN_SYSTEM, user, SEARCH_PLAN_SCHEMA, model=self.model,
                                                     max_tokens=4000)
                validate_against_schema(plan, SEARCH_PLAN_SCHEMA)
                if self.on_llm_call:
                    self.on_llm_call("nlq.plan", usage)
                return normalize_plan(plan), "llm"
            except LLMError as e:
                logger.warning("LLM planning failed, falling back to heuristic parser: %s", e)
        return normalize_plan(heuristic_plan(question)), "heuristic"

    def execute(self, plan: dict, can_view: Optional[Callable[[str], bool]] = None) -> list[dict]:
        lin = plan["lineage"]
        if lin["direction"] != "NONE" and lin.get("target"):
            target = lin["target"]
            if not target.startswith("urn:li:"):
                hits = filter_viewable(self.catalog.search(target, plan["entity_types"] or ["dataset"], count=1),
                                       can_view)
                if not hits:
                    return []
                target = hits[0]["urn"]
            elif can_view and not can_view(target):
                return []
            results = self.catalog.lineage(target, lin["direction"], lin["max_hops"], plan["limit"] * 3)
            if plan["entity_types"]:
                results = [r for r in results if (r.get("type") or "").lower().replace("_", "")
                           in {t.lower() for t in plan["entity_types"]}]
        else:
            results = self.catalog.search(plan["query"], plan["entity_types"], plan["filters"], plan["limit"] * 2)
        return filter_viewable(results, can_view)[: plan["limit"]]

    def ask(self, question: str, can_view: Optional[Callable[[str], bool]] = None) -> dict:
        plan, planner = self.plan(question)
        results = self.execute(plan, can_view)
        answer, citations = self._answer(question, plan, results)
        api_plan = dict(plan)
        if api_plan["lineage"]["direction"] == "NONE":
            api_plan["lineage"] = None
        return {"answer": answer, "plan": api_plan, "planner": planner, "results": results, "citations": citations}

    def _answer(self, question: str, plan: dict, results: list[dict]) -> tuple[str, list[str]]:
        urns = {r["urn"] for r in results}
        if self.llm is not None and results:
            listing = "\n".join(f"- {r['urn']} | {r.get('type')} | {r.get('name')} | {r.get('platform') or ''} | "
                                f"{(r.get('description') or '')[:300]}" for r in results)
            user = f"Question: {question}\n\nSearch results:\n{untrusted(redact(listing))}"
            try:
                out, usage = self.llm.generate_json(ANSWER_SYSTEM, user, ANSWER_SCHEMA, model=self.model,
                                                    max_tokens=4000)
                validate_against_schema(out, ANSWER_SCHEMA)
                if self.on_llm_call:
                    self.on_llm_call("nlq.answer", usage)
                return out["answer"], [c for c in out["citations"] if c in urns]  # drop hallucinated urns
            except LLMError as e:
                logger.warning("answer synthesis failed: %s", e)
        if not results:
            return "No matching assets you have access to were found.", []
        names = ", ".join(r.get("name") or r["urn"] for r in results[:5])
        more = f" and {len(results) - 5} more" if len(results) > 5 else ""
        return f"Found {len(results)} matching asset(s): {names}{more}.", [r["urn"] for r in results]
