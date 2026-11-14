"""Conversational metadata assistant (Claude tool use over permission-filtered Cairn tools).

* Tools are the same caller-scoped functions the MCP server exposes (``CairnTools``), so the
  assistant can never see more than the user can.
* User text is PII-redacted *before* it is sent or stored; the stored history is append-only,
  so thinking blocks returned by the model are replayed unchanged on the next turn.
* Without an LLM, a deterministic router answers with NLQ search or the lineage explainer.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, select, update
from sqlalchemy.engine import Engine

from cairn.ai.guardrails import UNTRUSTED_NOTICE, redact, screen
from cairn.ai.llm import LLMError, LLMUsage
from cairn.ai.usage import set_actor
from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

conversations = Table(
    "cairn_conversations", metadata,
    Column("id", String(40), primary_key=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("messages", JSONText, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
)

SYSTEM = f"""You are the Cairn data assistant for business and technical users. Answer questions about data assets,
metrics, ownership, quality, access and lineage using the tools. Rules:
- Ground every factual claim in tool results; cite assets by urn in square brackets, e.g. [urn:li:dataset:(...)].
- Prefer certified, healthy assets when recommending data. Mention masking or access limits plainly.
- For "why did X change/break" questions call explain_lineage first.
- For a known business metric call list_metrics and query_metric (governed); for other questions about the
  data itself (how many, how much, trends) call ask_data. Show the SQL either way.
- Only suggest changes or request access when the user asks; suggestions are queued for review, so say so.
- Be concise: short paragraphs or bullets, markdown allowed. If tools return nothing, say so.
{UNTRUSTED_NOTICE}"""

WORKSPACE_RULES = """
- Save a note with remember only when the person asks you to remember something or states a lasting preference,
  and say what you saved. Save an output with save_artifact only when they ask to save or keep it."""

TOOL_SPECS: list[dict] = [
    {"name": "search_assets", "description": "Keyword search over the catalog (datasets, dashboards, charts, products).",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}, "entity_types": {"type": "array", "items": {"type": "string"}},
         "platform": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "semantic_search", "description": "Meaning-based search for conceptual questions.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["query"]}},
    {"name": "get_asset", "description": "Full description of one asset: schema, docs, owners, classification.",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"}}, "required": ["urn"]}},
    {"name": "get_lineage", "description": "Upstream or downstream lineage (direction UPSTREAM|DOWNSTREAM).",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"},
                      "direction": {"type": "string", "enum": ["UPSTREAM", "DOWNSTREAM"]},
                      "max_hops": {"type": "integer"}}, "required": ["urn"]}},
    {"name": "impact_analysis", "description": "Downstream assets affected if this asset changes.",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"}}, "required": ["urn"]}},
    {"name": "get_quality_status", "description": "Latest data-quality monitor results for a dataset.",
     "input_schema": {"type": "object", "properties": {"dataset_urn": {"type": "string"}},
                      "required": ["dataset_urn"]}},
    {"name": "explain_lineage", "description": "Rank likely root causes of an anomaly on an asset using upstream "
                                               "quality, incidents, schema changes and job runs.",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"}}, "required": ["urn"]}},
    {"name": "check_access", "description": "Whether the user holds a privilege (e.g. VIEW_DATASET_PROFILE) on an asset.",
     "input_schema": {"type": "object", "properties": {"privilege": {"type": "string"},
                      "resource_urn": {"type": "string"}}, "required": ["privilege"]}},
    {"name": "ask_data", "description": "Answer a question about the data itself (totals, counts, trends) with one "
                                        "governed read-only SQL query; returns the SQL, an explanation and rows.",
     "input_schema": {"type": "object", "properties": {"question": {"type": "string"},
                      "dataset_urns": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]}},
    {"name": "search_knowledge", "description": "Search the team's documentation (Confluence, Notion, Google Docs, "
                                               "SharePoint) for pages that explain a dataset, metric or process.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "list_metrics", "description": "Governed metrics (approved definitions) with their dimensions.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "query_metric", "description": "Compute a governed metric by name, by its dimensions, with filters on "
                                           "them and a time grain (day, week, month, quarter, year).",
     "input_schema": {"type": "object", "properties": {"metric": {"type": "string"},
                      "dimensions": {"type": "array", "items": {"type": "string"}},
                      "filters": {"type": "array", "items": {"type": "object"}},
                      "time_grain": {"type": "string"}}, "required": ["metric"]}},
    {"name": "suggest_description", "description": "Suggest a description for a dataset or column. It waits for a "
                                                   "person's review; tell the user it was queued, not applied.",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"}, "description": {"type": "string"},
                      "column": {"type": "string"}, "rationale": {"type": "string"}}, "required": ["urn", "description"]}},
    {"name": "suggest_tags", "description": "Suggest tags for a dataset or column, queued for review.",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"},
                      "tags": {"type": "array", "items": {"type": "string"}}, "column": {"type": "string"},
                      "rationale": {"type": "string"}}, "required": ["urn", "tags"]}},
    {"name": "suggest_glossary_terms", "description": "Suggest glossary terms for a dataset or column, queued for review.",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"},
                      "terms": {"type": "array", "items": {"type": "string"}}, "column": {"type": "string"},
                      "rationale": {"type": "string"}}, "required": ["urn", "terms"]}},
    {"name": "suggest_owner", "description": "Suggest an owner for a dataset, queued for review.",
     "input_schema": {"type": "object", "properties": {"urn": {"type": "string"}, "owner": {"type": "string"},
                      "rationale": {"type": "string"}}, "required": ["urn", "owner"]}},
    {"name": "request_access", "description": "Request access to a data product for the user (only when they ask).",
     "input_schema": {"type": "object", "properties": {"product_urn": {"type": "string"}, "purpose": {"type": "string"},
                      "justification": {"type": "string"}, "duration_days": {"type": "integer"}},
                      "required": ["product_urn", "purpose", "justification"]}},
    {"name": "list_data_products", "description": "Marketplace data products matching a query, with access status.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "run_python", "description": "Run Python in an isolated sandbox (no network, limited CPU, memory and "
                                         "time). Put the answer in a variable named result; files written to out/ "
                                         "come back. files (name -> text) are copied into the working directory. "
                                         "With cairn_data, CAIRN_URL and CAIRN_TOKEN let the code query Cairn "
                                         "(POST {CAIRN_URL}/data/query) as the user, masked for them.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string"},
                      "files": {"type": "object", "additionalProperties": {"type": "string"}},
                      "cairn_data": {"type": "boolean"}, "timeout_s": {"type": "integer"}}, "required": ["code"]}},
]

_URN = re.compile(r"urn:li:[A-Za-z]+:(?:\([^\s\]]+\)|[^\s\]\)\,]+)")
_MAX_TOOL_RESULT = 12_000


def _block_to_dict(block: Any) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(block)


class Assistant:
    def __init__(self, engine: Engine, tools_factory: Callable[[Any], Any], llm=None,
                 explainer_factory: Optional[Callable[[Any], Any]] = None, nlq=None, marketplace=None,
                 model: str = "claude-opus-5-5", effort: str = "medium", max_iterations: int = 8):
        self.engine = engine
        self.tools_factory = tools_factory          # principal → CairnTools-like
        self.explainer_factory = explainer_factory  # principal → LineageExplainer
        self.llm = llm
        self.nlq = nlq
        self.marketplace = marketplace
        self.model = model
        self.effort = effort
        self.max_iterations = max_iterations
        self.workspace = None                      # cairn.ai.workspace.Workspace: memory, artifacts, research

    # ---- persistence ------------------------------------------------------------------
    def _load(self, cid: Optional[str], actor: str) -> tuple[str, list[dict]]:
        if cid:
            with self.engine.connect() as conn:
                row = conn.execute(select(conversations).where(conversations.c.id == cid)).first()
            if row and row.actor == actor:
                return cid, list(row.messages)
        return f"conv_{uuid.uuid4().hex[:16]}", []

    def _save(self, cid: str, actor: str, messages: list[dict]) -> None:
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            if conn.execute(select(conversations.c.id).where(conversations.c.id == cid)).first():
                conn.execute(update(conversations).where(conversations.c.id == cid)
                             .values(messages=messages, updated_at_ms=now))
            else:
                conn.execute(conversations.insert().values(id=cid, actor=actor, messages=messages, updated_at_ms=now))

    def purge_older_than(self, days: int = 30) -> int:
        cutoff = int(time.time() * 1000) - days * 86_400_000
        with self.engine.begin() as conn:
            return conn.execute(conversations.delete().where(conversations.c.updated_at_ms < cutoff)).rowcount

    # ---- tools ----------------------------------------------------------------------------
    def _run_tool(self, principal, name: str, args: dict, allowed_tools: Optional[set[str]] = None) -> tuple[dict, str]:
        from cairn.identity.agents import tool_scope

        if allowed_tools is not None and name not in allowed_tools:
            raise ValueError(f"tool {name} is not one of this agent's tools")
        if getattr(principal, "via", "") == "agent" and tool_scope(name) not in principal.scopes:
            raise PermissionError(f"this agent's scopes do not include {tool_scope(name)}")
        from cairn.ai.workspace import _DATA_TOOLS, turn_reads

        reads = turn_reads.get()
        tools = self.tools_factory(principal)
        guard = getattr(getattr(tools, "s", None), "intents", None)
        if getattr(principal, "via", "") == "agent" and guard is not None:
            from cairn.identity.intents import IntentDenied

            try:
                guard.check_tool(principal, name, args)
            except IntentDenied as e:
                raise PermissionError(str(e))
        if name == "explain_lineage":
            if self.explainer_factory is None:
                raise RuntimeError("lineage explanation unavailable")
            out = self.explainer_factory(principal).explain(args["urn"])
            return out, f"explained lineage for {args['urn'].split(',')[-2] if ',' in args['urn'] else args['urn']}"
        if name == "list_data_products":
            if self.marketplace is None:
                raise RuntimeError("marketplace unavailable")
            cards = self.marketplace(principal, args.get("query") or "*")
            return {"products": cards[:10]}, f"searched data products for “{args.get('query')}”"
        fn = getattr(tools, name, None)
        if fn is None:
            raise ValueError(f"unknown tool {name}")
        params = inspect.signature(fn).parameters
        allowed = {k: v for k, v in args.items() if k in params}
        from cairn.ai.traces import ai_channel, datasets_in
        from cairn.services import Forbidden

        started, flagged, status = time.monotonic(), [], "error"
        channel = ai_channel.set("assistant")
        try:
            out = screen(fn(**allowed), flagged)
            status = "refused" if isinstance(out, dict) and out.get("refused") and not out.get("queued") else "ok"
        except Forbidden:
            status = "forbidden"
            raise
        finally:
            ai_channel.reset(channel)
            traces = getattr(getattr(tools, "s", None), "traces", None)
            if traces is not None:
                traces.record(principal.urn, principal.agent, "assistant", name, allowed, status,
                              int((time.monotonic() - started) * 1000), len(flagged))
                if status == "ok" and tool_scope(name) == "read" and (read := datasets_in(allowed)):
                    traces.record_reads(principal.urn, principal.agent, "assistant", read, "metadata")
        if reads is not None:   # the datasets this turn used: an artifact saved from it is made from them
            reads.extend(datasets_in(allowed) + (datasets_in(out) if name in _DATA_TOOLS else []))
        return out, f"{name.replace('_', ' ')}({', '.join(f'{k}={str(v)[:40]}' for k, v in allowed.items())})"

    # ---- main entry ---------------------------------------------------------------------
    def chat(self, principal, message: str, conversation_id: Optional[str] = None) -> dict:
        cid, history = self._load(conversation_id, principal.urn)
        clean = redact(message.strip())[:4000]
        set_actor(principal.urn)
        metered = hasattr(self.llm, "check") and hasattr(self.llm, "record")
        budget_note = ""
        if metered and getattr(self.llm, "name", "") == "anthropic":
            try:
                self.llm.check()
            except LLMError as e:   # over budget: answer without the model rather than fail
                budget_note = f"\n\n_AI answers are paused: {e}. This answer comes from catalog search._"
        if self.llm is None or getattr(self.llm, "name", "") != "anthropic" or budget_note:
            reply, citations, calls = self._fallback(principal, clean)
            reply += budget_note
            history += [{"role": "user", "content": clean}, {"role": "assistant", "content": reply}]
            self._save(cid, principal.urn, history[-40:])
            return self._response(cid, reply, citations, calls)

        from cairn.ai.workspace import WORKSPACE_TOOL_SPECS, current_conversation, turn_reads

        messages = history + [{"role": "user", "content": clean}]
        system, specs = SYSTEM, TOOL_SPECS
        person = getattr(principal, "via", "") not in ("agent", "service")
        if self.workspace is not None and person:
            system = SYSTEM + WORKSPACE_RULES + self.workspace.person_prompt(principal.urn)
            specs = TOOL_SPECS + WORKSPACE_TOOL_SPECS
        reads_token, conv_token = turn_reads.set([]), current_conversation.set(cid)
        try:
            reply, messages, tool_calls = self.run_loop(principal, system, specs, messages, self.max_iterations,
                                                        "assistant.chat")
        finally:
            turn_reads.reset(reads_token)
            current_conversation.reset(conv_token)
        self._save(cid, principal.urn, messages[-60:])
        out = self._response(cid, reply, sorted(set(_URN.findall(reply))), tool_calls)
        out["saved"] = [c["saved"] for c in tool_calls if c.get("saved")]   # notes and artifacts, shown to the person
        return out

    def run_loop(self, principal, system: str, tool_specs: list[dict], messages: list[dict], max_iterations: int,
                 purpose: str, allowed_tools: Optional[set[str]] = None, model: Optional[str] = None,
                 abuse_check: Optional[Callable[..., Any]] = None) -> tuple[str, list[dict], list[dict]]:
        """The model's tool-use loop: (reply, messages, tool calls). Tools run as ``principal``; with
        ``allowed_tools``, only those (custom agents). ``abuse_check`` (abuse detection, :mod:`cairn.ai.abuse`) sees
        each answer and its tool calls before they run (``abuse_check("output", text, calls)``) and each tool result
        before the model does (``abuse_check("input", text)``); it stops the loop by raising
        :class:`~cairn.ai.abuse.AbuseBlocked`."""
        from cairn.ai.abuse import AbuseBlocked
        from cairn.ai.workspace import WORKSPACE_TOOLS, current_conversation

        client = self.llm.client
        metered = hasattr(self.llm, "check") and hasattr(self.llm, "record")
        tool_calls: list[dict] = []
        reply = ""
        for _ in range(max_iterations):
            kwargs: dict[str, Any] = {"model": model or self.model, "max_tokens": 16000, "system": system,
                                      "messages": messages, "thinking": {"type": "adaptive"},
                                      "output_config": {"effort": self.effort}}
            if tool_specs:
                kwargs["tools"] = tool_specs
            if getattr(self.llm, "server_side_fallback", False):
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                   **kwargs)
            else:
                resp = client.messages.create(**kwargs)
            if metered:
                usage = getattr(resp, "usage", None)
                self.llm.record(LLMUsage(model=getattr(resp, "model", model or self.model),
                                         input_tokens=getattr(usage, "input_tokens", 0) or 0,
                                         output_tokens=getattr(usage, "output_tokens", 0) or 0,
                                         cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                                         cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0),
                                purpose)
            content = [_block_to_dict(b) for b in resp.content]
            if abuse_check is not None:              # what the model says and the tools it asks for, before they run
                try:
                    abuse_check("output", "\n".join(b.get("text", "") for b in content if b.get("type") == "text"),
                                [(b.get("name"), b.get("input")) for b in content if b.get("type") == "tool_use"])
                except AbuseBlocked as e:
                    e.calls = tool_calls             # what ran before it was stopped
                    raise
            messages.append({"role": "assistant", "content": content})
            if resp.stop_reason == "refusal":
                reply = "I can't help with that request."
                break
            if resp.stop_reason != "tool_use":
                reply = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                break
            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                try:
                    saved = None
                    if block["name"] in WORKSPACE_TOOLS and allowed_tools is None and self.workspace is not None:
                        out, summary = self.workspace.run_tool(principal, block["name"], block.get("input") or {},
                                                               current_conversation.get())
                        saved = {"kind": "note" if block["name"] == "remember" else "artifact", **out}
                    else:
                        out, summary = self._run_tool(principal, block["name"], block.get("input") or {},
                                                      allowed_tools)
                    text = json.dumps(out, default=str)[:_MAX_TOOL_RESULT]
                    if abuse_check is not None:      # a tool result is data, maybe planted: checked before the model
                        abuse_check("input", text)
                    results.append({"type": "tool_result", "tool_use_id": block["id"], "content": redact(text)})
                    tool_calls.append({"name": block["name"], "summary": summary, **({"saved": saved} if saved else {})})
                except AbuseBlocked as e:
                    tool_calls.append({"name": block["name"], "summary": "blocked: abuse detection"})
                    e.calls = tool_calls
                    raise
                except Exception as e:  # report to the model, do not crash the turn
                    results.append({"type": "tool_result", "tool_use_id": block["id"], "is_error": True,
                                    "content": f"{type(e).__name__}: {e}"})
                    tool_calls.append({"name": block["name"], "summary": f"failed: {e}"})
            messages.append({"role": "user", "content": results})
        else:
            reply = reply or "I ran out of steps before finishing; try a more specific question."
        return reply, messages, tool_calls

    def _response(self, cid: str, reply: str, citations: list[str], calls: list[dict]) -> dict:
        actions = []
        for urn in citations[:3]:
            actions.append({"label": "Open", "kind": "open", "urn": urn})
        if citations:
            actions.append({"label": "Explain changes", "kind": "explain", "urn": citations[0]})
            actions.append({"label": "Follow", "kind": "follow", "urn": citations[0]})
        return {"conversation_id": cid, "reply_md": reply, "citations": citations, "tool_calls": calls,
                "suggested_actions": actions}

    def _fallback(self, principal, message: str) -> tuple[str, list[str], list[dict]]:
        """No LLM: route to the lineage explainer for 'why' questions, otherwise to NLQ search."""
        urns = _URN.findall(message)
        why = re.search(r"\b(why|broken|drop(ped)?|spike|wrong|stale|missing|changed?)\b", message, re.I)
        if why and self.explainer_factory is not None:
            target = urns[0] if urns else None
            if target is None and self.nlq is not None:
                plan = self.nlq.plan(message)[0]
                hits = self.nlq.execute(plan, None)
                target = hits[0]["urn"] if hits else None
            if target:
                from cairn.ai.lineage_explainer import explain_markdown

                result = self.explainer_factory(principal).explain(target)
                cites = [target] + [c["urn"] for c in result["likely_causes"][:3]]
                return explain_markdown(result), list(dict.fromkeys(cites)), [
                    {"name": "explain_lineage", "summary": f"explained lineage for {target}"}]
        if self.nlq is None:
            return "The assistant is not configured with catalog access.", [], []
        tools = self.tools_factory(principal)
        out = self.nlq.ask(message, lambda u: tools.s.can_view(principal)(u))
        return out["answer"], out["citations"], [{"name": "ask_metadata", "summary": f"planner={out['planner']}"}]
