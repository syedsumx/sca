"""Agentic abuse detection: prompt injection and data exfiltration through agents, detected and blocked (L114).

Rule-based detectors run where text crosses into or out of a model on an agent's behalf:

* **Inputs reaching models** through the AI gateway (every message; tool results most of all), and **tool results
  flowing back into agents** (custom agents' tools, the MCP server's tools): ``injection``, text that tells an AI to
  ignore its instructions or policies, to exfiltrate, or to call tools, and invisible characters used to smuggle it.
  In a tool result (data an attacker may have planted) it is ``critical``; in a person's own message ``warning``.
  What the MCP server's tools return is already flagged in place before it leaves Cairn
  (:func:`cairn.ai.guardrails.screen`), so there it is recorded as a ``warning``, not blocked.
* **Outputs** of models through the gateway and of custom agents, and **tool arguments** (the gateway's ``tool_use``
  blocks, custom agents' and MCP tool calls): ``secret`` (keys, tokens, private keys, passwords: ``critical``),
  ``pii_dump`` (at least ``pii_dump_threshold`` values of the kinds masking protects, found with the column
  classifier's validated value detectors: ``critical``; a third of it: ``warning``), ``destination`` (a URL or email
  address in a tool argument whose domain is not on ``allowed_domains``: ``critical``) and ``tool_scope`` (a model
  asking for a tool the request did not offer: ``critical``).
* An optional **model judge** (``CAIRN_ABUSE_JUDGE_URL``: ``POST {"direction", "text"}`` answering
  ``{"severity", "reason"}``) can add a finding the rules missed; it only ever raises severity, and when it cannot be
  reached the rules' verdict stands.

A finding at or above ``block_at`` (``critical``) blocks: the gateway answers 403 without calling the provider (or
without returning its answer), a custom agent's run stops (status ``blocked``), an MCP call to a far destination is refused. Every
finding is kept (``cairn_agent_abuse_findings``: agent, person, run, source, detector, severity, a redacted excerpt,
never secrets) and audited (``agent_abuse.finding``). Per agent, ``auto_pause_after`` critical findings within
``window_hours`` disable the agent in the hub (its credentials stop working at once, audited
``agent_abuse.auto_paused``); its counters feed the agent trace insights (:mod:`cairn.ai.trace_insights`).

HTTP: ``GET /v1/agent-abuse/findings``, ``POST /v1/agent-abuse/findings/{id}/dismiss``,
``GET|PUT /v1/agent-abuse/settings``, ``POST /v1/agent-abuse/scan`` (a dry run). Administrators
(``MANAGE_GLOBAL_SETTINGS``) and operators see every agent's findings; owners their agents'.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Index, String, Table, Text, func, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

abuse_findings = Table(
    "cairn_agent_abuse_findings", metadata,
    Column("id", String(40), primary_key=True),
    Column("agent", String(40), nullable=True),            # the agent hub id; None for a person's own call
    Column("actor", String(512), nullable=False),           # the person it acted for
    Column("run", String(64), nullable=True),
    Column("source", String(16), nullable=False),           # gateway | custom_agent | mcp
    Column("direction", String(8), nullable=False),         # input | output
    Column("detector", String(16), nullable=False),         # injection | secret | pii_dump | destination | tool_scope | judge
    Column("severity", String(8), nullable=False),          # info | warning | critical
    Column("blocked", Boolean, nullable=False),
    Column("summary", Text, nullable=False),                # what was found, redacted; never a secret or the data
    Column("status", String(10), nullable=False),           # open | dismissed
    Column("created_ms", BigInteger, nullable=False),
)
Index("ix_cairn_agent_abuse_findings_agent", abuse_findings.c.agent, abuse_findings.c.created_ms)
Index("ix_cairn_agent_abuse_findings_created_ms", abuse_findings.c.created_ms)

SEVERITIES = ("info", "warning", "critical")
RANK = {s: i for i, s in enumerate(SEVERITIES)}
SETTINGS_KEY = "abuse_detection"
ABUSE_ACTOR = "urn:li:corpuser:__cairn_abuse_detection"
DEFAULTS: dict[str, Any] = {"enabled": True, "block_at": "critical", "auto_pause_after": 3, "window_hours": 24,
                            "pii_dump_threshold": 10, "allowed_domains": []}

# Beyond the guardrails' instruction patterns: steering an agent off its policies, out of its tools, or out the door.
_POLICY = re.compile("|".join([
    r"\b(?:ignore|bypass|override|circumvent)\s+(?:all\s+|any\s+|your\s+|the\s+)+"
    r"(?:polic(?:y|ies)|guardrails?|safety (?:rules|checks)|restrictions?|masking|row filters?|governance)",
    r"\bwithout (?:telling|asking|notifying) (?:the )?(?:user|person|owner|admin)",
    r"\bfrom now on,? you (?:are|will|must)\b",
]), re.IGNORECASE)
_EXFIL = re.compile(r"\b(?:send|email|e-mail|post|upload|forward|transmit|copy|leak|exfiltrate)\b[^.\n]{0,80}?"
                    r"(?:https?://\S+|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|webhook|pastebin)",
                    re.IGNORECASE)
_URL = re.compile(r"https?://([A-Za-z0-9.\-]+)", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


class AbuseBlocked(Exception):
    def __init__(self, findings: list[dict]):
        self.findings = findings
        worst = findings[0] if findings else {"detector": "abuse", "summary": ""}
        super().__init__(f"blocked by Cairn's abuse detection: {worst['detector']} ({worst['summary']})")


def _excerpt(text: str, m: Optional[re.Match] = None) -> str:
    from cairn.ai.guardrails import redact, strip_invisible

    if m is not None:
        text = text[max(0, m.start() - 20): m.end() + 20]
    return redact(strip_invisible(text)).replace("\n", " ")[:160]


def _finding(direction: str, detector: str, severity: str, summary: str) -> dict:
    return {"direction": direction, "detector": detector, "severity": severity, "summary": summary}


# ---- the detectors -------------------------------------------------------------------------------------------
def scan_input(text: str, origin: str) -> list[dict]:
    """Findings on text reaching a model: ``origin`` ``tool_result`` (data, maybe planted) or ``user``."""
    from cairn.ai.guardrails import _INSTRUCTIONS, _INVISIBLE, strip_invisible

    if not text:
        return []
    clean = strip_invisible(text)
    hits = [m for rx in (_INSTRUCTIONS, _POLICY, _EXFIL) for m in rx.finditer(clean)]
    out = []
    if hits:
        severity = "critical" if origin == "tool_result" else "warning"
        out.append(_finding("input", "injection", severity,
                            f"instructions to an AI in a {origin.replace('_', ' ')}: {_excerpt(clean, hits[0])!r}"))
    elif _INVISIBLE.search(text):
        out.append(_finding("input", "injection", "warning", f"invisible characters in a {origin.replace('_', ' ')}"))
    return out


def _domain_allowed(domain: str, allowed: Iterable[str]) -> bool:
    d = domain.lower().rstrip(".")
    return any(d == a or d.endswith("." + a) for a in (x.lower().lstrip("*.").rstrip(".") for x in allowed) if a)


_DEST_KEYS = re.compile(r"(?i)^(?:to|cc|bcc|recipients?|emails?|e_?mail_?address(?:es)?|address(?:es)?|"
                        r"destination|dest|target|url|uri|endpoint|webhook(?:_url)?|callback(?:_url)?|send_to|"
                        r"notify|forward_to|reply_to)$")


def destinations(args: Any, key: str = "", _depth: int = 0) -> list[str]:
    """The domains a tool call would send something to: every http(s) URL in its arguments, and email addresses
    given as a recipient (a field such as ``to`` or ``email``, or a value that is nothing but addresses). An address
    mentioned in prose (a rationale, a description) is not a destination."""
    if _depth > 12:
        return []
    if isinstance(args, dict):
        return [d for k, v in args.items() for d in destinations(v, str(k), _depth + 1)]
    if isinstance(args, (list, tuple)):
        return [d for v in args for d in destinations(v, key, _depth + 1)]
    if not isinstance(args, str):
        return []
    out = _URL.findall(args)
    emails = _EMAIL.findall(args)
    if emails and (_DEST_KEYS.match(key) or re.fullmatch(r"[\s,;]*(?:[^\s,;@]+@[^\s,;@]+[\s,;]*)+", args)):
        out += emails
    return out


def scan_output(text: str, tool_calls: list[tuple[str, Any]], offered_tools: Optional[set[str]],
                settings: dict) -> list[dict]:
    """Findings on what leaves a model or an agent: ``text`` and the ``(tool, arguments)`` it asks for;
    ``offered_tools``: the tools the request offered (None: not known)."""
    from cairn.ai.guardrails import pii_values, secret_kinds

    out: list[dict] = []
    args_text = "\n".join(json.dumps(a, default=str) if not isinstance(a, str) else a for _, a in tool_calls)
    whole = f"{text}\n{args_text}"
    kinds = secret_kinds(whole)
    if kinds:
        out.append(_finding("output", "secret", "critical", f"credential-like values: {', '.join(kinds)}"))
    pii = pii_values(whole)
    total, limit = sum(pii.values()), int(settings.get("pii_dump_threshold") or DEFAULTS["pii_dump_threshold"])
    if total >= limit:
        out.append(_finding("output", "pii_dump", "critical",
                            f"{total} values of kinds masking protects ({', '.join(f'{k} {n}' for k, n in sorted(pii.items()))})"))
    elif total >= max(2, limit // 3):
        out.append(_finding("output", "pii_dump", "warning", f"{total} values of kinds masking protects "
                                                             f"({', '.join(sorted(pii))})"))
    allowed = settings.get("allowed_domains") or []
    for tool, args in tool_calls:
        if offered_tools is not None and tool not in offered_tools:
            out.append(_finding("output", "tool_scope", "critical", f"asked for tool {tool!r}, which it was not offered"))
        far = sorted({d.lower() for d in destinations(args) if not _domain_allowed(d, allowed)})
        if far:
            out.append(_finding("output", "destination", "critical",
                                f"tool {tool!r} was given destinations outside the allowlist: {', '.join(far[:5])}"))
    return out


# ---- what crosses the gateway ------------------------------------------------------------------------------
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(b.get("text") if isinstance(b, dict) and b.get("type") == "text" else
                                  b.get("content") if isinstance(b, dict) else b) for b in content)
    return "" if content is None else json.dumps(content, default=str)


def request_inputs(provider: str, body: dict) -> list[tuple[str, str]]:
    """``(origin, text)`` for each message of a gateway request."""
    out = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if provider == "openai":
            if role == "tool":
                out.append(("tool_result", _text_of(content)))
            elif role == "user":
                out.append(("user", _text_of(content)))
            continue
        if role != "user":
            continue
        if isinstance(content, str):
            out.append(("user", content))
            continue
        for b in content or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append(("tool_result", _text_of(b.get("content"))))
            elif isinstance(b, dict) and b.get("type") == "text":
                out.append(("user", b.get("text") or ""))
    return out


def response_outputs(provider: str, out: dict) -> tuple[str, list[tuple[str, Any]]]:
    """The text and tool calls of a gateway answer."""
    texts, calls = [], []
    if provider == "anthropic":
        for b in out.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text":
                texts.append(b.get("text") or "")
            elif isinstance(b, dict) and b.get("type") == "tool_use":
                calls.append((str(b.get("name")), b.get("input")))
    else:
        for c in out.get("choices") or []:
            msg = (c or {}).get("message") or {}
            texts.append(_text_of(msg.get("content")))
            for t in msg.get("tool_calls") or []:
                fn = (t or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = fn.get("arguments")
                calls.append((str(fn.get("name")), args))
    return "\n".join(texts), calls


def offered(provider: str, body: dict) -> Optional[set[str]]:
    tools = body.get("tools")
    if not tools:
        return set()
    names = set()
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name") if provider == "anthropic" else (t.get("function") or {}).get("name")
        if name:
            names.add(str(name))
    return names


def stream_outputs(provider: str, chunks: list[bytes]) -> tuple[str, list[tuple[str, Any]]]:
    """The text and tool calls of a buffered stream."""
    texts: list[str] = []
    tools: dict[int, list] = {}
    for line in b"".join(chunks).split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        try:
            ev = json.loads(line[5:].strip() or b"null")
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if provider == "anthropic":
            if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
                tools[ev.get("index", 0)] = [ev["content_block"].get("name"), ""]
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta":
                texts.append(d.get("text") or "")
            elif d.get("type") == "input_json_delta" and ev.get("index", 0) in tools:
                tools[ev.get("index", 0)][1] += d.get("partial_json") or ""
        else:
            for c in ev.get("choices") or []:
                d = (c or {}).get("delta") or {}
                texts.append(d.get("content") or "")
                for t in d.get("tool_calls") or []:
                    x = tools.setdefault(t.get("index", 0), ["", ""])
                    fn = t.get("function") or {}
                    x[0] = fn.get("name") or x[0]
                    x[1] += fn.get("arguments") or ""
    return "".join(texts), [(str(n), a) for n, a in tools.values()]


# ---- the service ------------------------------------------------------------------------------------------
class AbuseGuard:
    def __init__(self, engine, agents, may: Callable[[str, str], bool], audit=None,
                 judge: Optional[Callable[[str, str], Optional[dict]]] = None, clock=time.time):
        """``agents``: the agent hub; ``may(actor, privilege)``: the decision point; ``judge(direction, text)``: an
        optional model judge answering ``{"severity", "reason"}`` or None."""
        self.engine, self.agents, self.may, self.audit, self.judge, self.clock = engine, agents, may, audit, judge, clock
        self._settings_cache: tuple[float, dict] = (0.0, {})

    @classmethod
    def judge_from_env(cls) -> Optional[Callable[[str, str], Optional[dict]]]:
        url = os.environ.get("CAIRN_ABUSE_JUDGE_URL", "").strip()
        if not url:
            return None

        def judge(direction: str, text: str) -> Optional[dict]:
            import httpx

            r = httpx.post(url, json={"direction": direction, "text": text[:20_000]}, timeout=10)
            r.raise_for_status()
            return r.json()
        return judge

    # ---- settings -------------------------------------------------------------------------------------
    def settings(self) -> dict:
        at, cached = self._settings_cache
        if time.monotonic() - at < 5 and cached:
            return cached
        from cairn.identity.agents import agent_settings

        with self.engine.connect() as c:
            v = c.execute(select(agent_settings.c.value).where(agent_settings.c.key == SETTINGS_KEY)).scalar()
        cur = {**DEFAULTS, **{k: v[k] for k in DEFAULTS if isinstance(v, dict) and k in v}}
        self._settings_cache = (time.monotonic(), cur)
        return cur

    def set_settings(self, actor: str, body: dict) -> dict:
        from cairn.identity.agents import agent_settings

        if not self.may(actor, "MANAGE_GLOBAL_SETTINGS"):
            raise AbuseError("changing abuse detection needs MANAGE_GLOBAL_SETTINGS", 403)
        unknown = sorted(set(body) - set(DEFAULTS))
        if unknown:
            raise AbuseError(f"unknown settings {unknown}; settings are {sorted(DEFAULTS)}", 422)
        cur = dict(self.settings())
        for k, v in body.items():
            if k == "enabled" and not isinstance(v, bool):
                raise AbuseError("enabled: true or false", 422)
            if k == "block_at" and v not in ("warning", "critical"):
                raise AbuseError("block_at: warning or critical", 422)
            if k in ("auto_pause_after", "window_hours", "pii_dump_threshold") and (
                    isinstance(v, bool) or not isinstance(v, int) or not (0 if k == "auto_pause_after" else 1)
                    <= v <= 100_000):
                raise AbuseError(f"{k}: a whole number" + (" (0: never)" if k == "auto_pause_after" else ""), 422)
            if k == "allowed_domains" and (not isinstance(v, list) or len(v) > 500 or not all(
                    isinstance(d, str) and re.fullmatch(r"(\*\.)?[A-Za-z0-9.\-]{1,253}", d) for d in v)):
                raise AbuseError("allowed_domains: domain names (example.com, *.example.com)", 422)
            cur[k] = v
        with self.engine.begin() as c:
            q = update(agent_settings).where(agent_settings.c.key == SETTINGS_KEY)
            if c.execute(q.values(value=cur)).rowcount == 0:
                c.execute(agent_settings.insert().values(key=SETTINGS_KEY, value=cur))
            if self.audit is not None:
                self.audit.record("agents", "agent_abuse.settings_changed", actor, "agent-abuse",
                                  {k: body[k] for k in sorted(body)}, conn=c)
        self._settings_cache = (0.0, {})
        return cur

    # ---- inspecting -----------------------------------------------------------------------------------
    def _judged(self, direction: str, text: str, found: list[dict]) -> list[dict]:
        if self.judge is None or not text.strip():
            return found
        try:
            verdict = self.judge(direction, text)
        except Exception as e:  # noqa: BLE001 - the judge only adds findings; without it the rules stand
            logger.warning("abuse detection: the model judge could not be reached: %s", type(e).__name__)
            return found
        if isinstance(verdict, dict) and verdict.get("severity") in SEVERITIES and verdict["severity"] != "info":
            found = found + [_finding(direction, "judge", verdict["severity"],
                                      str(verdict.get("reason") or "the model judge flagged it")[:200])]
        return found

    def inspect(self, actor: str, agent: Optional[str], run: Optional[str], source: str,
                inputs: Iterable[tuple[str, str]] = (), output: str = "",
                tool_calls: Optional[list[tuple[str, Any]]] = None, offered_tools: Optional[set[str]] = None,
                block: bool = True, cap: Optional[str] = None) -> list[dict]:
        """Scans, records and (at ``block_at``) raises :class:`AbuseBlocked`. Returns the findings. ``block=False``
        only records; ``cap`` lowers every finding to at most that severity (what Cairn neutralises itself)."""
        cfg = self.settings()
        if not cfg["enabled"]:
            return []
        found: list[dict] = []
        inputs = [(o, t) for o, t in inputs if t]
        for origin, text in inputs:
            found += scan_input(text, origin)
        if inputs:
            found = self._judged("input", "\n".join(t for _, t in inputs), found)
        if output or tool_calls:
            found += scan_output(output, tool_calls or [], offered_tools, cfg)
            found = self._judged("output", output + "\n" + json.dumps([a for _, a in tool_calls or []],
                                                                      default=str), found)
        if not found:
            return []
        if cap is not None:
            for f in found:
                f["severity"] = cap if RANK[f["severity"]] > RANK[cap] else f["severity"]
        found.sort(key=lambda f: -RANK[f["severity"]])
        blocking = block and RANK[found[0]["severity"]] >= RANK[cfg["block_at"]]
        self._record(actor, agent, run, source, found, blocking, cfg)
        if blocking:
            raise AbuseBlocked([f for f in found if RANK[f["severity"]] >= RANK[cfg["block_at"]]])
        return found

    def _record(self, actor: str, agent: Optional[str], run: Optional[str], source: str, found: list[dict],
                blocked: bool, cfg: dict) -> None:
        now = int(self.clock() * 1000)
        with self.engine.begin() as c:
            for f in found:
                fid = f"abf_{uuid.uuid4().hex[:16]}"
                f["id"] = fid
                c.execute(abuse_findings.insert().values(
                    id=fid, agent=agent, actor=actor, run=run, source=source, direction=f["direction"],
                    detector=f["detector"], severity=f["severity"], blocked=blocked, summary=f["summary"],
                    status="open", created_ms=now))
                if self.audit is not None:
                    self.audit.record("agents", "agent_abuse.finding", actor, agent or "person",
                                      {"id": fid, "source": source, "detector": f["detector"],
                                       "severity": f["severity"], "blocked": blocked, "run": run}, conn=c)
        if agent and any(f["severity"] == "critical" for f in found):
            self._maybe_pause(agent, cfg, now)

    def _maybe_pause(self, agent: str, cfg: dict, now: int) -> None:
        limit = int(cfg["auto_pause_after"])
        if limit <= 0:
            return
        since = now - int(cfg["window_hours"]) * 3600_000
        with self.engine.connect() as c:
            n = c.execute(select(func.count()).select_from(abuse_findings).where(
                abuse_findings.c.agent == agent, abuse_findings.c.severity == "critical",
                abuse_findings.c.status == "open", abuse_findings.c.created_ms >= since)).scalar()
        if n < limit:
            return
        from cairn.identity.agents import AgentError, AgentPatch

        try:
            if not self.agents.get(agent)["enabled"]:
                return
            self.agents.update(agent, AgentPatch(enabled=False), ABUSE_ACTOR)
        except AgentError:
            return
        if self.audit is not None:
            self.audit.record("agents", "agent_abuse.auto_paused", ABUSE_ACTOR, agent,
                              {"critical_findings": n, "window_hours": cfg["window_hours"], "threshold": limit})
        logger.warning("abuse detection paused agent %s after %d critical findings", agent, n)

    # ---- reading --------------------------------------------------------------------------------------
    def _visible_agents(self, actor: str) -> Optional[set[str]]:
        """None: every agent (administrators, operators); else the agents ``actor`` owns."""
        if self.may(actor, "MANAGE_GLOBAL_SETTINGS") or self.may(actor, "MANAGE_SYSTEM_OPERATIONS"):
            return None
        return {a["id"] for a in self.agents.list() if a.get("owner") == actor}

    @staticmethod
    def _public(r) -> dict:
        return {"id": r.id, "agent": r.agent, "actor": r.actor, "run": r.run, "source": r.source,
                "direction": r.direction, "detector": r.detector, "severity": r.severity, "blocked": r.blocked,
                "summary": r.summary, "status": r.status, "created_ms": r.created_ms}

    def findings(self, actor: str, agent: Optional[str] = None, severity: Optional[str] = None,
                 status: Optional[str] = None, limit: int = 200) -> list[dict]:
        if severity is not None and severity not in SEVERITIES:
            raise AbuseError(f"severity: one of {list(SEVERITIES)}", 422)
        if not 1 <= limit <= 1000:
            raise AbuseError("limit: 1 to 1000", 422)
        q = select(abuse_findings)
        visible = self._visible_agents(actor)
        if visible is not None:
            if not visible:
                return []
            q = q.where(abuse_findings.c.agent.in_(sorted(visible)))
        if agent:
            q = q.where(abuse_findings.c.agent == agent)
        if severity:
            q = q.where(abuse_findings.c.severity == severity)
        if status:
            q = q.where(abuse_findings.c.status == status)
        with self.engine.connect() as c:
            return [self._public(r) for r in c.execute(q.order_by(abuse_findings.c.created_ms.desc()).limit(limit))]

    def dismiss(self, actor: str, fid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(abuse_findings).where(abuse_findings.c.id == fid)).first()
        visible = self._visible_agents(actor)
        if r is None or (visible is not None and r.agent not in visible):
            raise AbuseError("no such finding", 404)
        with self.engine.begin() as c:
            c.execute(update(abuse_findings).where(abuse_findings.c.id == fid).values(status="dismissed"))
            if self.audit is not None:
                self.audit.record("agents", "agent_abuse.dismissed", actor, r.agent or "person", {"id": fid}, conn=c)
        return {**self._public(r), "status": "dismissed"}

    def counts(self, agent_ids: list[str], since_ms: int, until_ms: int) -> dict[str, dict]:
        """Per agent: open findings by severity and how many were blocked, for the trace insights."""
        if not agent_ids:
            return {}
        q = (select(abuse_findings.c.agent, abuse_findings.c.severity, abuse_findings.c.blocked, func.count())
             .where(abuse_findings.c.agent.in_(agent_ids), abuse_findings.c.status == "open",
                    abuse_findings.c.created_ms >= since_ms, abuse_findings.c.created_ms < until_ms)
             .group_by(abuse_findings.c.agent, abuse_findings.c.severity, abuse_findings.c.blocked))
        out: dict[str, dict] = {}
        with self.engine.connect() as c:
            for agent, sev, blocked, n in c.execute(q):
                x = out.setdefault(agent, {"critical": 0, "warning": 0, "info": 0, "blocked": 0})
                x[sev] += n
                x["blocked"] += n if blocked else 0
        return out

    def scan(self, actor: str, body: dict) -> dict:
        """A dry run: what the detectors say about ``text`` (nothing recorded)."""
        if not (self.may(actor, "MANAGE_GLOBAL_SETTINGS") or self.may(actor, "MANAGE_SYSTEM_OPERATIONS")):
            raise AbuseError("a dry run needs MANAGE_GLOBAL_SETTINGS or MANAGE_SYSTEM_OPERATIONS", 403)
        text, direction = str(body.get("text") or ""), body.get("direction") or "input"
        if direction == "input":
            found = scan_input(text, str(body.get("origin") or "tool_result"))
        elif direction == "output":
            calls = body.get("tool_calls") or []
            found = scan_output(text, [(str(c.get("name")), c.get("input")) for c in calls if isinstance(c, dict)],
                                None, self.settings())
        else:
            raise AbuseError("direction: input or output", 422)
        return {"findings": sorted(found, key=lambda f: -RANK[f["severity"]])}


class AbuseError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def build_abuse_router(guard: AbuseGuard, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/agent-abuse", tags=["agents"])

    def person(p) -> str:
        if p.is_service or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "abuse findings are for people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except AbuseError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/findings")
    async def findings(agent: Optional[str] = None, severity: Optional[str] = None, status: Optional[str] = None,
                       limit: int = 200, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: {"findings": guard.findings(actor, agent, severity, status, limit)})

    @router.post("/findings/{finding_id}/dismiss")
    async def dismiss(finding_id: str, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: guard.dismiss(actor, finding_id))

    @router.get("/settings")
    async def get_settings(p=Depends(current_principal)):
        person(p)
        return await call(guard.settings)

    @router.put("/settings")
    async def put_settings(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: guard.set_settings(actor, body))

    @router.post("/scan")
    async def scan(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: guard.scan(actor, body))

    return router
