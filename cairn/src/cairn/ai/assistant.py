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

from cairn.ai.guardrails import UNTRUSTED_NOTICE, redact
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
- Be concise: short paragraphs or bullets, markdown allowed. If tools return nothing, say so.
{UNTRUSTED_NOTICE}"""

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
    {"name": "list_data_products", "description": "Marketplace data products matching a query, with access status.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
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
    def _run_tool(self, principal, name: str, args: dict) -> tuple[dict, str]:
        tools = self.tools_factory(principal)
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
        out = fn(**allowed)
        return out, f"{name.replace('_', ' ')}({', '.join(f'{k}={str(v)[:40]}' for k, v in allowed.items())})"

    # ---- main entry ---------------------------------------------------------------------
    def chat(self, principal, message: str, conversation_id: Optional[str] = None) -> dict:
        cid, history = self._load(conversation_id, principal.urn)
        clean = redact(message.strip())[:4000]
        if self.llm is None or getattr(self.llm, "name", "") != "anthropic":
            reply, citations, calls = self._fallback(principal, clean)
            history += [{"role": "user", "content": clean}, {"role": "assistant", "content": reply}]
            self._save(cid, principal.urn, history[-40:])
            return self._response(cid, reply, citations, calls)

        client = self.llm.client
        messages = history + [{"role": "user", "content": clean}]
        tool_calls: list[dict] = []
        reply = ""
        for _ in range(self.max_iterations):
            kwargs: dict[str, Any] = {"model": self.model, "max_tokens": 16000, "system": SYSTEM, "tools": TOOL_SPECS,
                                      "messages": messages, "thinking": {"type": "adaptive"},
                                      "output_config": {"effort": self.effort}}
            if getattr(self.llm, "server_side_fallback", False):
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                   **kwargs)
            else:
                resp = client.messages.create(**kwargs)
            content = [_block_to_dict(b) for b in resp.content]
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
                    out, summary = self._run_tool(principal, block["name"], block.get("input") or {})
                    text = json.dumps(out, default=str)[:_MAX_TOOL_RESULT]
                    results.append({"type": "tool_result", "tool_use_id": block["id"], "content": redact(text)})
                    tool_calls.append({"name": block["name"], "summary": summary})
                except Exception as e:  # report to the model, do not crash the turn
                    results.append({"type": "tool_result", "tool_use_id": block["id"], "is_error": True,
                                    "content": f"{type(e).__name__}: {e}"})
                    tool_calls.append({"name": block["name"], "summary": f"failed: {e}"})
            messages.append({"role": "user", "content": results})
        else:
            reply = reply or "I ran out of steps before finishing; try a more specific question."
        self._save(cid, principal.urn, messages[-60:])
        return self._response(cid, reply, sorted(set(_URN.findall(reply))), tool_calls)

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
