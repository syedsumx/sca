"""Agent memory: governed long-term memory for agents, about the people they work for (L111).

An agent keeps chat sessions per conversation; memory is what it keeps *across* them: a person's preferences,
facts about their work, decisions made. Each memory belongs to one agent (its id in the agent hub) and is scoped:

* ``person``: about one person (``subject`` = their urn). Written by the agent during a run for that person
  (through its run token, ``ctx.memory.remember`` in ``cairn.agentkit``) or by the person themselves. Recalled only
  in runs for that same person: an agent never recalls one person's memories while working for another.
* ``team``: shared with the members of a group (``subject`` = the group urn). Written only by an explicit action of
  a member (never by an agent, whose run acts for one person); recalled in runs for any member.
* ``shared``: shared with everyone who may use the agent. Written only by the agent's owner or an operator.

Recall is by meaning plus recency: the query and each memory are embedded with the embedder Cairn uses for search
by meaning (:mod:`cairn.ai.embeddings`), and the score is ``0.8 * similarity + 0.2 * recency`` (recency halves
every 30 days since the memory was written or last recalled).

Governed:

* **Refused content.** A memory holding values of a kind masking policies protect (emails, SSNs, card numbers,
  IBANs, phone numbers, IP addresses: the column classifier's validated value detectors,
  :func:`cairn.ai.guardrails.pii_values`), a credential (:func:`cairn.ai.guardrails.secret_kinds`), or text that
  reads like instructions to an AI (a tool result trying to plant one) is refused (422) and the refusal audited.
* **Limits.** ``CAIRN_AGENT_MEMORY_MAX_CHARS`` (2,000) per memory; ``CAIRN_AGENT_MEMORY_MAX_PER_SUBJECT`` (200) per
  agent and person, team or shared pool (the agent's oldest, least recently recalled memory goes when it writes one
  more; a person's own write is refused instead); ``CAIRN_AGENT_MEMORY_RETENTION_DAYS`` (180): a memory not recalled
  for that long expires and is purged.
* **The person decides.** ``GET /v1/me/agent-memories`` lists the memories about the caller (and the team and
  shared ones they can see), each with the run that wrote it; ``DELETE /v1/me/agent-memories/{id}`` (or ``?agent=``
  for all of an agent's) deletes them, under their own identity only.
* **Audited:** ``agent_memory.saved``, ``.refused``, ``.recalled``, ``.deleted``, ``.evicted``, ``.purged`` (who,
  agent, scope, run; never the content).

Agents (scope ``memory``, run token or key): ``POST /v1/agent-memory`` ``{"content"}``,
``POST /v1/agent-memory/recall`` ``{"query", "k"}``, ``DELETE /v1/agent-memory/{id}``.
"""

from __future__ import annotations

import math
import os
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Index, String, Table, Text, and_, delete, insert, or_, select, update

from cairn.common.db import JSONText, metadata

agent_memories = Table(
    "cairn_agent_memories", metadata,
    Column("id", String(40), primary_key=True),
    Column("agent", String(40), nullable=False),              # the agent hub id (agt_…)
    Column("scope", String(8), nullable=False),               # person | team | shared
    Column("subject", String(512), nullable=False),           # person urn | group urn | "" (shared)
    Column("content", Text, nullable=False),
    Column("embedding", JSONText, nullable=False),            # the content's vector (the search-by-meaning embedder)
    Column("source", String(8), nullable=False),              # agent | person
    Column("source_run", String(64), nullable=True),          # the run that wrote it (agent writes)
    Column("created_by", String(512), nullable=False),        # the person the write was made as or by
    Column("created_ms", BigInteger, nullable=False),
    Column("last_used_ms", BigInteger, nullable=False),       # written or last recalled
)
Index("ix_cairn_agent_memories_agent_subject", agent_memories.c.agent, agent_memories.c.scope,
      agent_memories.c.subject)
Index("ix_cairn_agent_memories_subject", agent_memories.c.subject)

SCOPES = ("person", "team", "shared")
DAY_MS = 86_400_000


class AgentMemoryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _int_env(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class AgentMemory:
    def __init__(self, engine, agents, embed: Callable[[str], list[float]],
                 may_use: Callable[[str, dict], bool], is_operator: Callable[[str], bool],
                 groups_of: Callable[[str], list[str]], audit=None, clock=time.time):
        """``agents``: the agent hub; ``may_use(actor, agent)``: may this person run the agent;
        ``groups_of(actor)``: the groups they are in."""
        self.engine, self.agents, self.embed, self.may_use = engine, agents, embed, may_use
        self.is_operator, self.groups_of, self.audit, self.clock = is_operator, groups_of, audit, clock
        self.max_chars = _int_env("CAIRN_AGENT_MEMORY_MAX_CHARS", 2000)
        self.max_per_subject = _int_env("CAIRN_AGENT_MEMORY_MAX_PER_SUBJECT", 200)
        self.retention_days = _int_env("CAIRN_AGENT_MEMORY_RETENTION_DAYS", 180)

    def _now(self) -> int:
        return int(self.clock() * 1000)

    def _audit(self, action: str, actor: str, agent: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("agents", action, actor, agent, details)

    # ---- which agent ---------------------------------------------------------------------------
    def resolve_agent(self, ref: str) -> dict:
        """An agent by hub id (``agt_…``) or by name (an agent app's or custom agent's name)."""
        from cairn.identity.agents import AgentError

        ref = str(ref or "").strip()
        if not ref:
            raise AgentMemoryError("agent: the agent's id or name")
        if ref.startswith("agt_"):
            try:
                return self.agents.get(ref)
            except AgentError:
                raise AgentMemoryError("no such agent", 404)
        named = [a for a in self.agents.list() if a["name"] == ref and a["kind"] in ("app", "built")]
        if len(named) != 1:
            raise AgentMemoryError("no such agent" if not named else f"more than one agent is called {ref}; use its id",
                               404 if not named else 409)
        return named[0]

    # ---- content checks --------------------------------------------------------------------------
    def _check(self, actor: str, agent_id: str, content: Any, run: Optional[str]) -> str:
        from cairn.ai.guardrails import injection_signals, pii_values, secret_kinds, strip_invisible

        if not isinstance(content, str) or not content.strip():
            raise AgentMemoryError("content: what to remember")
        if len(content) > self.max_chars:
            raise AgentMemoryError(f"content: at most {self.max_chars} characters", 413)
        pii, secrets_, signals = pii_values(content), secret_kinds(content), injection_signals(content)
        why = None
        if pii:
            why = (f"it holds values of a kind masking protects ({', '.join(sorted(pii))}); keep the fact, not the "
                   "value")
        elif secrets_:
            why = f"it holds what looks like a credential ({', '.join(secrets_)})"
        elif signals:
            why = "it reads like instructions to an AI"
        if why:
            self._audit("agent_memory.refused", actor, agent_id, {
                "run": run, "pii": sorted(pii), "secrets": secrets_, "instructions": bool(signals)})
            raise AgentMemoryError(f"not remembered: {why}", 422)
        return strip_invisible(content).strip()

    # ---- writing ---------------------------------------------------------------------------------
    def remember(self, actor: str, agent_id: str, content: Any, scope: str = "person", team: Optional[str] = None,
                 source: str = "agent", run: Optional[str] = None) -> dict:
        if scope not in SCOPES:
            raise AgentMemoryError(f"scope: one of {', '.join(SCOPES)}")
        agent = self.agents.get(agent_id)
        if source == "agent" and scope != "person":           # a run acts for one person: it cannot share
            raise AgentMemoryError("an agent remembers only for the person it works for; team and shared memories are "
                               "written by people", 403)
        if scope == "person":
            subject = actor
        elif scope == "team":
            if not team or team not in self.groups_of(actor):
                raise AgentMemoryError("team: a group you are a member of", 403)
            subject = team
        else:
            if agent.get("owner") != actor and not self.is_operator(actor):
                raise AgentMemoryError("only the agent's owner or an operator writes its shared memories", 403)
            subject = ""
        clean = self._check(actor, agent_id, content, run)
        now = self._now()
        vec = self.embed(clean)
        mid = f"mem_{uuid.uuid4().hex[:16]}"
        evicted: list[str] = []
        where = and_(agent_memories.c.agent == agent_id, agent_memories.c.scope == scope,
                     agent_memories.c.subject == subject)
        with self.engine.begin() as c:
            held = c.execute(select(agent_memories.c.id).where(where)
                             .order_by(agent_memories.c.last_used_ms, agent_memories.c.created_ms)).all()
            over = len(held) + 1 - self.max_per_subject
            if over > 0:
                if source != "agent":
                    raise AgentMemoryError(f"this agent already holds {self.max_per_subject} memories here; delete "
                                       "some first", 409)
                evicted = [r.id for r in held[:over]]
                c.execute(delete(agent_memories).where(agent_memories.c.id.in_(evicted)))
            c.execute(insert(agent_memories).values(
                id=mid, agent=agent_id, scope=scope, subject=subject, content=clean, embedding=vec, source=source,
                source_run=run, created_by=actor, created_ms=now, last_used_ms=now))
        if evicted:
            self._audit("agent_memory.evicted", actor, agent_id, {"ids": evicted, "scope": scope})
        self._audit("agent_memory.saved", actor, agent_id, {"id": mid, "scope": scope, "source": source,
                                                            "run": run, "chars": len(clean)})
        return self._get(mid)

    # ---- reading ---------------------------------------------------------------------------------
    @staticmethod
    def _public(r, actor: Optional[str] = None, deletable: Optional[bool] = None) -> dict:
        d = {"id": r.id, "agent": r.agent, "scope": r.scope, "subject": r.subject or None, "content": r.content,
             "source": r.source, "source_run": r.source_run, "created_by": r.created_by, "created_ms": r.created_ms,
             "last_used_ms": r.last_used_ms}
        if deletable is not None:
            d["deletable"] = deletable
        return d

    def _get(self, mid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(agent_memories).where(agent_memories.c.id == mid)).first()
        if r is None:
            raise AgentMemoryError("no such memory", 404)
        return self._public(r)

    def _expired_before(self) -> int:
        return self._now() - self.retention_days * DAY_MS

    def _visible_where(self, actor: str, agent_id: Optional[str]):
        """Memories a run for ``actor`` may recall: theirs, their teams', the agent's shared ones."""
        groups = [g for g in self.groups_of(actor) if g]
        cond = or_(and_(agent_memories.c.scope == "person", agent_memories.c.subject == actor),
                   and_(agent_memories.c.scope == "shared"),
                   *([and_(agent_memories.c.scope == "team", agent_memories.c.subject.in_(groups))] if groups else []))
        parts = [cond, agent_memories.c.last_used_ms >= self._expired_before()]
        if agent_id is not None:
            parts.append(agent_memories.c.agent == agent_id)
        return and_(*parts)

    def recall(self, actor: str, agent_id: str, query: Any = "", k: Any = 5, run: Optional[str] = None) -> list[dict]:
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 50:
            raise AgentMemoryError("k: 1 to 50")
        query = str(query or "").strip()[:4000]
        with self.engine.connect() as c:
            rows = c.execute(select(agent_memories).where(self._visible_where(actor, agent_id))).all()
        now = self._now()
        qv = self.embed(query) if query else None
        scored = []
        for r in rows:
            vec = r.embedding or []
            if qv is not None and len(vec) != len(qv):          # the embedder changed: embed it again
                vec = self.embed(r.content)
                with self.engine.begin() as c:
                    c.execute(update(agent_memories).where(agent_memories.c.id == r.id).values(embedding=vec))
            sim = _cos(qv, vec) if qv is not None else 0.0
            recency = 0.5 ** (max(0, now - r.last_used_ms) / (30 * DAY_MS))
            if qv is not None and sim < 0.1:
                continue
            score = 0.8 * sim + 0.2 * recency if qv is not None else recency
            scored.append((score, sim, r))
        scored.sort(key=lambda t: (-t[0], -t[2].created_ms))
        top = scored[:k]
        if top:
            with self.engine.begin() as c:
                c.execute(update(agent_memories).where(agent_memories.c.id.in_([t[2].id for t in top]))
                          .values(last_used_ms=now))
        self._audit("agent_memory.recalled", actor, agent_id, {"run": run, "count": len(top)})
        return [{**self._public(r), "score": round(score, 4), "similarity": round(sim, 4)} for score, sim, r in top]

    def forget(self, actor: str, agent_id: str, mid: str, run: Optional[str] = None) -> None:
        """An agent forgets one of its memories about the person it works for."""
        with self.engine.begin() as c:
            n = c.execute(delete(agent_memories).where(
                agent_memories.c.id == mid, agent_memories.c.agent == agent_id,
                agent_memories.c.scope == "person", agent_memories.c.subject == actor)).rowcount
        if not n:
            raise AgentMemoryError("no such memory", 404)
        self._audit("agent_memory.deleted", actor, agent_id, {"id": mid, "by": "agent", "run": run})

    # ---- the person's own view -------------------------------------------------------------------
    def _may_delete(self, actor: str, r) -> bool:
        if r.scope == "person":
            return r.subject == actor
        try:
            owner = self.agents.get(r.agent).get("owner")
        except Exception:  # noqa: BLE001 - the agent is gone: its memories are its writers' and operators'
            owner = None
        if owner == actor or self.is_operator(actor):
            return True
        return r.scope == "team" and r.subject in self.groups_of(actor)

    def mine(self, actor: str, agent: Optional[str] = None) -> list[dict]:
        agent_id = self.resolve_agent(agent)["id"] if agent else None
        with self.engine.connect() as c:
            rows = c.execute(select(agent_memories).where(self._visible_where(actor, agent_id))
                             .order_by(agent_memories.c.created_ms.desc()).limit(2000)).all()
        out = []
        for r in rows:
            if r.scope == "shared":
                try:
                    if not self.may_use(actor, self.agents.get(r.agent)):
                        continue
                except Exception:  # noqa: BLE001 - an agent that is gone shows none of its shared memories
                    continue
            out.append(self._public(r, deletable=self._may_delete(actor, r)))
        return out

    def add(self, actor: str, body: dict) -> dict:
        """A person tells an agent something to remember (about them, their team, or for everyone)."""
        a = self.resolve_agent(body.get("agent"))
        if not self.may_use(actor, a):
            raise AgentMemoryError("no such agent", 404)
        return self.remember(actor, a["id"], body.get("content"), str(body.get("scope") or "person"),
                             body.get("team"), source="person")

    def delete(self, actor: str, mid: str) -> None:
        with self.engine.connect() as c:
            r = c.execute(select(agent_memories).where(agent_memories.c.id == mid)).first()
        if r is None or not self._may_delete(actor, r):
            raise AgentMemoryError("no such memory", 404)
        with self.engine.begin() as c:
            c.execute(delete(agent_memories).where(agent_memories.c.id == mid))
        self._audit("agent_memory.deleted", actor, r.agent, {"id": mid, "by": "person", "scope": r.scope})

    def delete_all(self, actor: str, agent: str) -> int:
        """Every memory an agent holds about the caller."""
        agent_id = self.resolve_agent(agent)["id"]
        with self.engine.begin() as c:
            n = c.execute(delete(agent_memories).where(
                agent_memories.c.agent == agent_id, agent_memories.c.scope == "person",
                agent_memories.c.subject == actor)).rowcount
        self._audit("agent_memory.deleted", actor, agent_id, {"count": n, "by": "person", "scope": "person"})
        return n

    def purge(self) -> int:
        """Drops memories past retention (not written or recalled for ``CAIRN_AGENT_MEMORY_RETENTION_DAYS``)."""
        with self.engine.begin() as c:
            n = c.execute(delete(agent_memories).where(agent_memories.c.last_used_ms < self._expired_before())).rowcount
        if n:
            self._audit("agent_memory.purged", "cairn", "agent-memory", {"count": n,
                                                                         "retention_days": self.retention_days})
        return n

    def drop_agent(self, agent_id: str) -> None:
        """An agent deleted: its memories go with it."""
        with self.engine.begin() as c:
            c.execute(delete(agent_memories).where(agent_memories.c.agent == agent_id))


def build_agent_memory_router(m: AgentMemory, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(tags=["ai"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except AgentMemoryError as e:
            raise HTTPException(e.status, str(e))

    def agent_of(p) -> tuple[str, str]:
        if p.via != "agent" or not p.agent:
            raise HTTPException(403, "agent memory is written and recalled by agents, with their run token")
        return p.urn, p.agent

    def person(p) -> str:
        if p.is_service or p.via == "agent":
            raise HTTPException(403, "only the person a memory is about manages it")
        return p.urn

    # The agent's side: what its scope "memory" allows, always for the person its credential acts for.
    @router.post("/v1/agent-memory", status_code=201)
    async def remember(body: dict = Body(...), p=Depends(current_principal)):
        actor, agent = agent_of(p)
        return await call(lambda: m.remember(actor, agent, body.get("content"), str(body.get("scope") or "person"),
                                             source="agent", run=p.run))

    @router.post("/v1/agent-memory/recall")
    async def recall(body: dict = Body(default={}), p=Depends(current_principal)):
        actor, agent = agent_of(p)
        return await call(lambda: {"memories": m.recall(actor, agent, body.get("query"), body.get("k", 5), p.run)})

    @router.delete("/v1/agent-memory/{memory_id}", status_code=204)
    async def forget(memory_id: str, p=Depends(current_principal)):
        actor, agent = agent_of(p)
        await call(lambda: m.forget(actor, agent, memory_id, p.run))

    # The person's side.
    @router.get("/v1/me/agent-memories")
    async def mine(agent: Optional[str] = None, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: {"memories": m.mine(actor, agent)})

    @router.post("/v1/me/agent-memories", status_code=201)
    async def add(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: m.add(actor, body))

    @router.delete("/v1/me/agent-memories/{memory_id}", status_code=204)
    async def drop(memory_id: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: m.delete(actor, memory_id))

    @router.delete("/v1/me/agent-memories")
    async def drop_all(agent: str, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: {"deleted": m.delete_all(actor, agent)})

    return router
