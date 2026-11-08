"""Intent-based security for agents: each agent run declares what it is for, and requests outside it are refused (L109).

Scopes (:mod:`cairn.identity.agents`) say what an agent may ever do. An **intent** narrows one run (or one credential)
to what the task in hand needs: a ``purpose`` in words, the **action categories** it allows, and optionally the
**tables** and **domains** it concerns::

    {"purpose": "weekly revenue summary", "actions": ["read", "query"],
     "tables": ["finance.public.*", "urn:li:dataset:(...)"], "domains": ["urn:li:domain:finance"]}

Categories: ``read`` (catalog reads), ``query`` (previews, SQL, ask-your-data, metric queries), ``propose`` (things
that wait for a person's review), ``write`` (direct changes), ``share`` (shares, clean rooms, grants, publishing,
the outbox, embedding), ``export`` (exports, downloads, detokenizing), ``model`` (the AI gateway), ``admin``.

Every agent request is classified: HTTP requests by method and route (:func:`classify_request`), SQL by statement
kind (:func:`classify_sql`, so a ``SELECT`` is ``query``, an ``INSERT`` ``write``, a ``GRANT`` ``admin``, an
``UNLOAD`` ``export``), MCP and assistant tools by the tool. A request outside the declared intent, or naming a
dataset outside its tables and domains, is refused with 403 and audited as ``agent.intent_denied``.

Where intents come from: agent app invocations may pass ``intent`` (it rides on the run's short-lived token);
custom agents declare it in their definition; an administrator may set one on a key agent. Credentials without an
intent keep today's behaviour, unless an operator requires intents (``PUT /v1/agent-intents/settings
{"required": true}``): then every agent request without one is refused (fail closed).
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

ACTIONS = ("read", "query", "propose", "write", "share", "export", "model", "admin")
SETTINGS_KEY = "intents"
SCOPE_CATEGORY = {"read": "read", "query_data": "query", "propose": "propose", "write": "write", "model": "model"}

_SHARE_PREFIXES = ("/v1/shares", "/sql/shares", "/v1/clean-rooms", "/ai/outbox", "/embed", "/v1/reverse-syncs")
_SHARE_SEGMENTS = re.compile(r"/(share|shares|grants|publish|subscription|recipients)(/|$)")
_EXPORT_SEGMENTS = re.compile(r"/(export|exports|download|detokenize|unload)(/|$)")
_ADMIN_PREFIXES = ("/admin", "/oauth", "/me/agents", "/scim", "/v1/admin", "/v1/sso", "/v1/agent-intents",
                   "/v1/agent-traces/settings")
_ADMIN_WRITES = ("/governance/policies", "/governance/ai-systems", "/governance/personas", "/governance/purposes",
                 "/v1/secret-scopes", "/governance/residency", "/governance/posture", "/governance/tokens")


class IntentError(ValueError):
    pass


class IntentDenied(Exception):
    pass


@dataclass(frozen=True)
class Intent:
    purpose: str
    actions: tuple[str, ...]
    tables: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"purpose": self.purpose, "actions": list(self.actions), "tables": list(self.tables),
                "domains": list(self.domains)}


def parse_intent(raw: Any) -> Optional[Intent]:
    """An intent from its JSON form, validated; None for none."""
    if raw is None or isinstance(raw, Intent):
        return raw
    if not isinstance(raw, dict):
        raise IntentError('intent: {"purpose": "...", "actions": [...], "tables": [...], "domains": [...]}')
    unknown = sorted(set(raw) - {"purpose", "actions", "tables", "domains"})
    if unknown:
        raise IntentError(f"intent: unknown fields {unknown}")
    purpose = raw.get("purpose")
    if not isinstance(purpose, str) or not 3 <= len(purpose.strip()) <= 200:
        raise IntentError("intent.purpose: what the run is for, 3 to 200 characters")
    actions = raw.get("actions")
    if not isinstance(actions, list) or not actions or not all(isinstance(a, str) for a in actions):
        raise IntentError(f"intent.actions: some of {list(ACTIONS)}")
    bad = sorted(set(actions) - set(ACTIONS))
    if bad:
        raise IntentError(f"intent.actions: unknown {bad}; actions are {list(ACTIONS)}")
    out = {}
    for key, prefix in (("tables", None), ("domains", "urn:li:domain:")):
        v = raw.get(key) or []
        if not isinstance(v, list) or len(v) > 100 or not all(isinstance(x, str) and 1 <= len(x) <= 700 for x in v):
            raise IntentError(f"intent.{key}: up to 100 names")
        if prefix and any(not x.startswith(prefix) for x in v):
            raise IntentError(f"intent.{key}: domain urns ({prefix}…)")
        out[key] = tuple(dict.fromkeys(v))
    return Intent(purpose.strip(), tuple(a for a in ACTIONS if a in actions), out["tables"], out["domains"])


def _under(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def classify_request(method: str, route: str, read_posts: Iterable[str] = ()) -> str:
    """The action category of ``method route`` (the route template, e.g. ``/x/{id}``)."""
    from cairn.identity.agents import MEMORY_PATHS, MODEL_PATHS, PROPOSE_ROUTES, QUERY_DATA_PATHS

    reading = method in ("GET", "HEAD", "OPTIONS")
    if _under(route, _ADMIN_PREFIXES) or (not reading and _under(route, _ADMIN_WRITES)):
        return "admin"
    if _EXPORT_SEGMENTS.search(route):
        return "export"
    if _under(route, MODEL_PATHS):
        return "model"
    if _under(route, MEMORY_PATHS):          # agent memory: recalling reads, remembering and forgetting write
        return "read" if reading or route.endswith("/recall") else "write"
    if route in QUERY_DATA_PATHS:
        return "query"
    if not reading and (_under(route, _SHARE_PREFIXES) or _SHARE_SEGMENTS.search(route)):
        return "share"
    if reading or route in set(read_posts):
        return "read"
    if method == "POST" and route in PROPOSE_ROUTES:
        return "propose"
    return "write"


_SQL_KIND = {"SELECT": "query", "WITH": "query", "VALUES": "query", "SHOW": "query", "DESCRIBE": "query",
             "DESC": "query", "EXPLAIN": "query", "TABLE": "query",
             "INSERT": "write", "UPDATE": "write", "DELETE": "write", "MERGE": "write", "CREATE": "write",
             "DROP": "write", "ALTER": "write", "TRUNCATE": "write", "CALL": "write", "REPLACE": "write",
             "UPSERT": "write", "COMMENT": "write", "REFRESH": "write", "OPTIMIZE": "write", "VACUUM": "write",
             "ANALYZE": "write", "COPY": "write", "SET": "write", "RESET": "write", "USE": "query",
             "GRANT": "admin", "REVOKE": "admin", "DENY": "admin",
             "UNLOAD": "export", "EXPORT": "export"}


def classify_sql(sql: str) -> str:
    """The action category of a SQL text by its statement kinds (the most powerful one wins); unknown kinds and
    text that cannot be read are ``admin``, so an intent never lets them through by accident."""
    order = {c: i for i, c in enumerate(("query", "write", "share", "export", "admin"))}
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S)
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    kinds = []
    for stmt in (s.strip() for s in text.split(";")):
        if not stmt:
            continue
        word = re.match(r"\(*\s*([A-Za-z]+)", stmt)
        kind = _SQL_KIND.get(word.group(1).upper(), "admin") if word else "admin"
        upper = stmt.upper()
        if kind == "write" and re.search(r"\bCOPY\s+INTO\s+'|\bLOCATION\s*=|\bEXTERNAL\s+LOCATION\b", upper):
            kind = "export"                        # writing to a storage location outside Cairn's tables
        if kind == "query" and re.search(r"\bINTO\s+OUTFILE\b|\bINTO\s+DUMPFILE\b", upper):
            kind = "export"
        if kind == "query" and re.search(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", upper) and word.group(1).upper() == "WITH":
            kind = "write"                         # WITH … INSERT
        kinds.append(kind)
    if not kinds:
        return "admin"
    return max(kinds, key=lambda k: order.get(k, len(order)))


def tool_category(tool: str) -> str:
    from cairn.identity.agents import tool_scope

    return SCOPE_CATEGORY.get(tool_scope(tool), "write")


def _dataset_name(urn: str) -> Optional[str]:
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[A-Z]+\)$", urn)
    return m.group(1) if m else None


class IntentGuard:
    """Checks agent requests against their declared intent, and audits the refusals."""

    def __init__(self, agents, audit=None, domain_of: Optional[Callable[[str], list[str]]] = None,
                 may: Optional[Callable[[str, str], bool]] = None):
        """``agents``: the agent hub (settings storage); ``domain_of(urn)``: the domain urns of a dataset;
        ``may(actor, privilege)``: the decision point (for the settings and the denials list)."""
        self.agents, self.audit, self.domain_of, self.may = agents, audit, domain_of, may
        self._cache: tuple[float, bool] = (0.0, False)

    # ---- settings ------------------------------------------------------------------------------------
    def settings(self) -> dict:
        from sqlalchemy import select

        from cairn.identity.agents import agent_settings

        with self.agents.engine.connect() as conn:
            v = conn.execute(select(agent_settings.c.value).where(agent_settings.c.key == SETTINGS_KEY)).scalar()
        return {"required": bool((v or {}).get("required")), "changed_by": (v or {}).get("changed_by"),
                "changed_ms": (v or {}).get("changed_ms")}

    def required(self) -> bool:
        now = time.monotonic()
        at, value = self._cache
        if now - at > 5:                     # read errors propagate: the request fails rather than pass
            value = self.settings()["required"]
            self._cache = (now, value)
        return value

    def set_required(self, actor: str, required: bool) -> dict:
        from sqlalchemy import update

        from cairn.identity.agents import agent_settings

        value = {"required": bool(required), "changed_by": actor, "changed_ms": int(time.time() * 1000)}
        with self.agents.engine.begin() as conn:
            q = update(agent_settings).where(agent_settings.c.key == SETTINGS_KEY)
            if conn.execute(q.values(value=value)).rowcount == 0:
                conn.execute(agent_settings.insert().values(key=SETTINGS_KEY, value=value))
            if self.audit is not None:
                self.audit.record("agents", "agents.intents_required" if required else "agents.intents_optional",
                                  actor, "all", {"required": bool(required)}, conn=conn)
        self._cache = (time.monotonic(), bool(required))
        return self.settings()

    # ---- checks --------------------------------------------------------------------------------------
    def _matches(self, intent: Intent, urn: str) -> bool:
        if not intent.tables and not intent.domains:
            return True
        name = _dataset_name(urn) or ""
        for t in intent.tables:
            if t == urn or (name and fnmatch.fnmatchcase(name.lower(), t.lower())):
                return True
        if intent.domains and self.domain_of is not None:
            try:
                domains = set(self.domain_of(urn) or [])
            except Exception:  # noqa: BLE001 - its domains cannot be known: not inside the intent
                domains = set()
            if domains & set(intent.domains):
                return True
        return False

    def _deny(self, p, category: str, reason: str, where: dict) -> None:
        if self.audit is not None:
            self.audit.record("agents", "agent.intent_denied", p.urn, p.agent,
                              {"category": category, "reason": reason, "run": getattr(p, "run", None),
                               "intent": p.intent.as_dict() if getattr(p, "intent", None) else None, **where})
        raise IntentDenied(reason)

    def check(self, p, category: str, urns: Iterable[str] = (), **where: Any) -> None:
        """Raise :class:`IntentDenied` (audited) when an agent's ``category`` request, naming ``urns``, is outside
        its intent. People and services pass untouched."""
        if getattr(p, "via", None) != "agent":
            return
        intent: Optional[Intent] = getattr(p, "intent", None)
        if intent is None:
            if self.required():
                self._deny(p, category, "an operator requires a declared intent for agent requests, and this "
                                        "credential has none", where)
            return
        if category not in intent.actions:
            self._deny(p, category, f"{category} is outside this run's declared intent ({intent.purpose}: "
                                    f"{', '.join(intent.actions)})", where)
        for urn in dict.fromkeys(u for u in urns if isinstance(u, str) and u.startswith("urn:li:dataset:")):
            if not self._matches(intent, urn):
                self._deny(p, category, f"{_dataset_name(urn) or urn} is outside this run's declared intent "
                                        f"({intent.purpose})", {**where, "dataset": urn})

    def check_tool(self, p, tool: str, args: dict) -> None:
        from cairn.ai.traces import datasets_in

        self.check(p, tool_category(tool), datasets_in(args), tool=tool)

    def check_sql(self, p, sql: str, urns: Iterable[str] = ()) -> None:
        self.check(p, classify_sql(sql), urns, route="sql", statement=classify_sql(sql))

    # ---- what was refused ----------------------------------------------------------------------------
    def denials(self, actor: str, days: int = 7, limit: int = 200) -> list[dict]:
        """Refusals over the last ``days``: every one for administrators and operators, else those made for the
        person or by the agents they own; newest first."""
        if not 1 <= days <= 400:
            raise IntentError("days: 1 to 400")
        everyone = self.may is not None and (self.may(actor, "MANAGE_GLOBAL_SETTINGS")
                                             or self.may(actor, "MANAGE_SYSTEM_OPERATIONS"))
        owned = set() if everyone else {a["id"] for a in self.agents.list() if a.get("owner") == actor}
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        out = []
        for e in self.agents.audit.export(since=since, category="agents") if self.agents.audit is not None else []:
            if e["action"] != "agent.intent_denied":
                continue
            if not everyone and e["actor"] != actor and e["subject"] not in owned:
                continue
            d = e.get("details") or {}
            out.append({"id": e["id"], "ts": e["ts"], "agent": e["subject"], "actor": e["actor"],
                        "category": d.get("category"), "reason": d.get("reason"), "route": d.get("route"),
                        "tool": d.get("tool"), "dataset": d.get("dataset"), "run": d.get("run"),
                        "purpose": (d.get("intent") or {}).get("purpose")})
        return list(reversed(out))[:limit]


def build_intents_router(guard: IntentGuard, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/agent-intents", tags=["agents"])

    def person(p) -> str:
        if p.is_service or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "agent intents are managed by people")
        return p.urn

    @router.get("/settings")
    def get_settings(p=Depends(current_principal)):
        person(p)
        return {**guard.settings(), "actions": list(ACTIONS)}

    @router.put("/settings")
    def put_settings(body: dict = Body(...), p=Depends(current_principal)):
        """``{"required": true}``: refuse every agent request whose credential declares no intent."""
        actor = person(p)
        if guard.may is None or not guard.may(actor, "MANAGE_GLOBAL_SETTINGS"):
            raise HTTPException(403, "requiring intents needs MANAGE_GLOBAL_SETTINGS")
        if set(body) != {"required"} or not isinstance(body["required"], bool):
            raise HTTPException(422, 'body: {"required": true|false}')
        return guard.set_required(actor, body["required"])

    @router.get("/denials")
    def denials(days: int = 7, p=Depends(current_principal)):
        """Agent requests refused for being outside their intent, newest first."""
        try:
            return guard.denials(person(p), days)
        except IntentError as e:
            raise HTTPException(422, str(e))

    @router.post("/classify")
    def classify(body: dict = Body(...), p=Depends(current_principal)):
        """How Cairn classifies a request (``method`` and ``path`` as a route template) or a ``sql`` text."""
        person(p)
        if isinstance(body.get("sql"), str):
            return {"category": classify_sql(body["sql"])}
        if isinstance(body.get("method"), str) and isinstance(body.get("path"), str):
            from cairn.common.licence import READ_POSTS

            return {"category": classify_request(body["method"].upper(), body["path"], READ_POSTS)}
        raise HTTPException(422, 'body: {"sql": "..."} or {"method": "GET", "path": "/x/{id}"}')

    return router
