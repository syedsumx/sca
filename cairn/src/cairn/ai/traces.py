"""Observability for AI agents: a trace of every tool call, and people's feedback on answers.

* **Tool calls** made through MCP or the in-product assistant are recorded: the agent (if any), the
  person, the tool, a redacted summary of the arguments, the outcome, the time taken and whether
  catalog text in the result was flagged. Administrators see any agent's activity; people see the
  activity of the agents acting for them. Prometheus gets ``cairn_agent_tool_calls_total``,
  ``cairn_agent_tool_seconds`` and ``cairn_ai_injection_flagged_total``.
* **Feedback**: a thumbs up or down (and a comment) on an assistant answer or an ask-your-data
  result. Down-voted questions export as evaluation cases (``cairn ai eval``), so what people found
  wrong becomes a regression test.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, func, select

from cairn.ai.guardrails import redact
from cairn.common.db import JSONText, metadata

agent_calls = Table(
    "cairn_agent_calls", metadata,
    Column("id", String(40), primary_key=True),
    Column("agent", String(40), nullable=True, index=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("channel", String(12), nullable=False),            # mcp | assistant
    Column("tool", String(64), nullable=False),
    Column("args", String(600), nullable=False),
    Column("status", String(12), nullable=False),             # ok | error | refused | forbidden
    Column("duration_ms", Integer, nullable=False),
    Column("flagged", Integer, nullable=False),
    Column("created_ms", BigInteger, nullable=False, index=True),
)
ai_feedback = Table(
    "cairn_ai_feedback", metadata,
    Column("id", String(40), primary_key=True),
    Column("actor", String(512), nullable=False),
    Column("kind", String(12), nullable=False),               # chat | ask
    Column("rating", String(4), nullable=False),              # up | down
    Column("question", Text, nullable=False),
    Column("answer", Text, nullable=False),
    Column("comment", String(2000), nullable=False),
    Column("context", JSONText, nullable=False),              # conversation id, sql, tables
    Column("created_ms", BigInteger, nullable=False, index=True),
)

KEEP_CALLS_DAYS = 30

try:  # pragma: no cover - metrics are optional
    from prometheus_client import Counter, Histogram

    TOOL_CALLS = Counter("cairn_agent_tool_calls_total", "Tool calls by AI agents and the assistant",
                         ["channel", "tool", "status"])
    TOOL_SECONDS = Histogram("cairn_agent_tool_seconds", "Tool call duration", ["tool"],
                             buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
    INJECTIONS = Counter("cairn_ai_injection_flagged_total",
                         "Catalog strings flagged as instructions to an AI in tool results", ["channel"])
except Exception:  # noqa: BLE001
    TOOL_CALLS = TOOL_SECONDS = INJECTIONS = None


def _now() -> int:
    return int(time.time() * 1000)


def summarize(args: dict) -> str:
    """Arguments as a short, redacted string (no personal data, nothing long)."""
    try:
        text = json.dumps(args, default=str, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(args)
    text = redact(text)
    return text if len(text) <= 500 else text[:497] + "..."


def outcome(result: Any) -> str:
    if isinstance(result, dict) and result.get("error") in ("forbidden", "unauthenticated"):
        return "forbidden"
    if isinstance(result, dict) and result.get("error") == "refused":
        return "refused"
    if isinstance(result, dict) and result.get("error"):
        return "error"
    return "ok"


class FeedbackIn(BaseModel):
    kind: Literal["chat", "ask"]
    rating: Literal["up", "down"]
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(default="", max_length=20_000)
    comment: str = Field(default="", max_length=2000)
    context: dict = Field(default_factory=dict)


class TraceService:
    def __init__(self, engine, audit=None):
        self.engine = engine
        self.audit = audit
        self._purged = 0

    # ---- tool calls ---------------------------------------------------------------------------
    def record(self, actor: str, agent: Optional[str], channel: str, tool: str, args: dict, status: str,
               duration_ms: int, flagged: int = 0) -> None:
        if TOOL_CALLS is not None:
            TOOL_CALLS.labels(channel, tool, status).inc()
            TOOL_SECONDS.labels(tool).observe(duration_ms / 1000)
            if flagged:
                INJECTIONS.labels(channel).inc(flagged)
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(agent_calls.insert().values(
                id=f"call_{uuid.uuid4().hex[:14]}", agent=agent, actor=actor, channel=channel, tool=tool,
                args=summarize(args), status=status, duration_ms=int(duration_ms), flagged=int(flagged),
                created_ms=now))
            if now - self._purged > 3_600_000:              # keep a month, purged at most hourly
                self._purged = now
                conn.execute(delete(agent_calls).where(agent_calls.c.created_ms < now - KEEP_CALLS_DAYS * 86_400_000))

    def activity(self, agent: Optional[str] = None, actor: Optional[str] = None, limit: int = 100) -> list[dict]:
        q = select(agent_calls).order_by(agent_calls.c.created_ms.desc()).limit(min(max(limit, 1), 500))
        if agent:
            q = q.where(agent_calls.c.agent == agent)
        if actor:
            q = q.where(agent_calls.c.actor == actor)
        with self.engine.connect() as conn:
            return [{"id": r.id, "agent": r.agent, "actor": r.actor, "channel": r.channel, "tool": r.tool,
                     "args": r.args, "status": r.status, "duration_ms": r.duration_ms, "flagged": r.flagged,
                     "created_ms": r.created_ms} for r in conn.execute(q)]

    def stats(self, since_ms: Optional[int] = None) -> dict:
        """Per agent and per tool over the last day (or since ``since_ms``): calls, errors, p95-ish time."""
        since = since_ms or _now() - 86_400_000
        with self.engine.connect() as conn:
            rows = conn.execute(select(agent_calls.c.agent, agent_calls.c.tool, agent_calls.c.status,
                                       func.count(), func.max(agent_calls.c.duration_ms),
                                       func.avg(agent_calls.c.duration_ms), func.sum(agent_calls.c.flagged))
                                .where(agent_calls.c.created_ms >= since)
                                .group_by(agent_calls.c.agent, agent_calls.c.tool, agent_calls.c.status)).all()
        by_agent: dict[str, dict] = {}
        by_tool: dict[str, dict] = {}
        for agent, tool, status, n, mx, avg, flagged in rows:
            for key, table in ((agent or "assistant", by_agent), (tool, by_tool)):
                t = table.setdefault(key, {"calls": 0, "errors": 0, "refused": 0, "flagged": 0, "max_ms": 0,
                                           "avg_ms": 0.0})
                t["avg_ms"] = (t["avg_ms"] * t["calls"] + float(avg or 0) * n) / (t["calls"] + n)
                t["calls"] += n
                t["errors"] += n if status == "error" else 0
                t["refused"] += n if status in ("refused", "forbidden") else 0
                t["flagged"] += int(flagged or 0)
                t["max_ms"] = max(t["max_ms"], int(mx or 0))
        for table in (by_agent, by_tool):
            for t in table.values():
                t["avg_ms"] = round(t["avg_ms"], 1)
        return {"since_ms": since, "by_agent": by_agent, "by_tool": by_tool}

    # ---- feedback -----------------------------------------------------------------------------------
    def feedback(self, actor: str, fb: FeedbackIn) -> dict:
        fid = f"fb_{uuid.uuid4().hex[:14]}"
        with self.engine.begin() as conn:
            conn.execute(ai_feedback.insert().values(
                id=fid, actor=actor, kind=fb.kind, rating=fb.rating, question=redact(fb.question)[:4000],
                answer=redact(fb.answer)[:20_000], comment=redact(fb.comment)[:2000], context=fb.context,
                created_ms=_now()))
        if self.audit is not None:
            self.audit.record("ai", "ai.feedback", actor, fid, {"kind": fb.kind, "rating": fb.rating})
        return {"id": fid}

    def feedback_list(self, rating: Optional[str] = None, kind: Optional[str] = None, limit: int = 200) -> list[dict]:
        q = select(ai_feedback).order_by(ai_feedback.c.created_ms.desc()).limit(min(max(limit, 1), 1000))
        if rating:
            q = q.where(ai_feedback.c.rating == rating)
        if kind:
            q = q.where(ai_feedback.c.kind == kind)
        with self.engine.connect() as conn:
            return [{"id": r.id, "actor": r.actor, "kind": r.kind, "rating": r.rating, "question": r.question,
                     "answer": r.answer, "comment": r.comment, "context": r.context, "created_ms": r.created_ms}
                    for r in conn.execute(q)]

    def feedback_summary(self) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(select(ai_feedback.c.kind, ai_feedback.c.rating, func.count())
                                .group_by(ai_feedback.c.kind, ai_feedback.c.rating)).all()
        out: dict[str, dict] = {}
        for kind, rating, n in rows:
            out.setdefault(kind, {"up": 0, "down": 0})[rating] = n
        return out

    def eval_cases(self) -> str:
        """Down-voted questions as ``cairn ai eval`` cases (YAML), to fill in the expected answer."""
        import yaml

        cases = []
        for f in self.feedback_list(rating="down", limit=500):
            case: dict[str, Any] = {"id": f["id"], "feature": "nlq" if f["kind"] == "chat" else "text2sql",
                                    "question": f["question"],
                                    "notes": f"down-voted: {f['comment'] or 'no comment'}; answer was: "
                                             f"{f['answer'][:300]}"}
            if f["kind"] == "ask" and (f["context"] or {}).get("sql"):
                case["rejected_sql"] = f["context"]["sql"]
            case["expect"] = {"TODO": "fill in what a right answer must contain"}
            cases.append(case)
        return yaml.safe_dump({"cases": cases}, sort_keys=False, allow_unicode=True)
