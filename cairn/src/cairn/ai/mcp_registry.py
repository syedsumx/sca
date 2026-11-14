"""Cairn's AI gateway as an MCP tool registry (L155): external MCP servers (GitHub, Jira, ServiceNow, Slack, a
team's own) registered once, reached through Cairn as the person (or agent) asking, with the credentials kept in
Cairn.

``/v1/ai/mcp-servers``: registering, changing and removing a server needs ``MANAGE_MCP_SERVERS``; using one (listing
its tools, calling them) needs ``USE_MCP_SERVER`` on ``urn:li:mcpServer:<name>``, decided by the policy engine for
each call (fail closed). Through Cairn's own MCP server the registered tools appear as ``<server>__<tool>`` on a
connection that asks for them (``?servers=github,jira`` or ``all``), and ``mcp_servers`` / ``mcp_server_call`` reach
them from any connection: one endpoint for Cairn's tools and every approved external one.

A registration (:func:`check`):

* ``url``: the server's streamable HTTP endpoint, https and on a public address unless the operator allows private
  networks (``CAIRN_MCP_REGISTRY_ALLOW_PRIVATE``); checked again at every connection;
* ``auth``: ``none``; ``bearer`` or ``header`` with a value from a **secret scope** (``{"scope", "key"}``), read as
  the server's owner at each connection and never stored, returned or logged; ``header`` names the header;
* ``allow_tools`` / ``deny_tools``: glob patterns over the server's tool names (deny wins; no allow list: all);
* ``read_only``: only tools the server marks read-only (``readOnlyHint``) are offered or called;
* ``rpm``: calls a minute per person to this server (``CAIRN_MCP_REGISTRY_RPM`` by default, 60);
* ``team``, ``description``, ``enabled``.

Every call is audited (``mcp_server.called``: server, tool, outcome, time; never its arguments or answer), an
agent's arguments go through abuse detection, and answers are screened for planted instructions and have
credential-looking values redacted before they reach a model.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import JSONText, metadata

mcp_servers = Table(
    "cairn_mcp_servers", metadata,
    Column("name", String(40), primary_key=True),
    Column("url", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("auth", JSONText, nullable=False),          # {"type": none|bearer|header, "header", "scope", "key"}: refs
    Column("allow_tools", JSONText, nullable=True),
    Column("deny_tools", JSONText, nullable=True),
    Column("read_only", Boolean, nullable=False),
    Column("rpm", Integer, nullable=True),
    Column("team", String(512), nullable=True),
    Column("enabled", Boolean, nullable=False),
    Column("owner", String(512), nullable=False),      # whose secret scopes the credential is read from
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,63}$")
FIELDS = ("url", "description", "auth", "allow_tools", "deny_tools", "read_only", "rpm", "team", "enabled")
MANAGE, USE = "MANAGE_MCP_SERVERS", "USE_MCP_SERVER"
TOOL_SEP = "__"
TOOLS_TTL_S = 60
CALL_ROUTE = "/v1/ai/mcp-servers/{name}/tools/{tool}/call"


class RegistryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def urn(name: str) -> str:
    return f"urn:li:mcpServer:{name}"


def check(body: dict, base: Optional[dict] = None) -> dict:
    """A registration from ``body`` over ``base`` (a change) or the defaults."""
    extra = set(body) - set(FIELDS) - {"name"}
    if extra:
        raise RegistryError(f"unknown fields: {', '.join(sorted(extra))}")
    out = dict(base or {"description": "", "auth": {"type": "none"}, "allow_tools": None, "deny_tools": None,
                        "read_only": False, "rpm": None, "team": None, "enabled": True})
    out.update({k: body[k] for k in FIELDS if k in body})
    url = out.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")) or len(url) > 2000:
        raise RegistryError("url is the server's streamable HTTP endpoint (https://...)")
    out["description"] = str(out.get("description") or "")[:2000]
    auth = out.get("auth") or {"type": "none"}
    if not isinstance(auth, dict) or auth.get("type") not in ("none", "bearer", "header"):
        raise RegistryError('auth is {"type": "none"}, {"type": "bearer", "scope", "key"} or '
                            '{"type": "header", "header", "scope", "key"} (the value from a secret scope)')
    if set(auth) - {"type", "header", "scope", "key"}:
        raise RegistryError("auth holds a secret scope reference (scope, key), never the value itself")
    if auth["type"] != "none":
        if not all(isinstance(auth.get(k), str) and auth[k] for k in ("scope", "key")):
            raise RegistryError("auth needs the secret scope and key its value is read from")
        if auth["type"] == "header" and not HEADER.match(str(auth.get("header") or "")):
            raise RegistryError("auth.header is the header's name")
        if auth["type"] == "header" and str(auth["header"]).lower() in ("host", "cookie", "content-length"):
            raise RegistryError(f"auth.header cannot be {auth['header']}")
    out["auth"] = {k: auth[k] for k in ("type", "header", "scope", "key") if k in auth}
    for k in ("allow_tools", "deny_tools"):
        v = out.get(k)
        if v is not None and (not isinstance(v, list) or len(v) > 200
                              or not all(isinstance(x, str) and 0 < len(x) <= 128 for x in v)):
            raise RegistryError(f"{k} is a list of tool names or glob patterns")
    for k in ("read_only", "enabled"):
        if not isinstance(out.get(k), bool):
            raise RegistryError(f"{k} is true or false")
    if out.get("rpm") is not None and (not isinstance(out["rpm"], int) or isinstance(out["rpm"], bool)
                                       or not 1 <= out["rpm"] <= 10_000):
        raise RegistryError("rpm is calls a minute per person, 1 to 10000")
    if out.get("team") is not None and not str(out["team"]).startswith("urn:li:corpGroup:"):
        raise RegistryError("team is a group urn (urn:li:corpGroup:...)")
    return out


def offered(row: dict, tool: dict) -> bool:
    """Whether the registration lets this tool through (allow and deny lists, read-only)."""
    name = str(tool.get("name") or "")
    if any(fnmatch.fnmatchcase(name, p) for p in row.get("deny_tools") or []):
        return False
    allow = row.get("allow_tools")
    if allow is not None and not any(fnmatch.fnmatchcase(name, p) for p in allow):
        return False
    if row.get("read_only") and (tool.get("annotations") or {}).get("readOnlyHint") is not True:
        return False
    return True


class McpRegistry:
    def __init__(self, engine, authorize: Callable[[Any, str, Optional[str]], bool], read_secret=None, audit=None,
                 abuse=None, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        """``authorize(principal, privilege, urn)``; ``read_secret(owner, scope, key)``."""
        self.engine, self.authorize, self.read_secret, self.audit, self.abuse = engine, authorize, read_secret, audit, abuse
        self.intents = None                               # cairn.identity.intents.IntentGuard, when built
        self.clock = clock
        self.allow_private = os.environ.get("CAIRN_MCP_REGISTRY_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")
        self.allow_http = os.environ.get("CAIRN_MCP_REGISTRY_ALLOW_HTTP", "").lower() in ("1", "true", "yes")
        self.default_rpm = int(os.environ.get("CAIRN_MCP_REGISTRY_RPM", "60"))
        self.max_bytes = int(os.environ.get("CAIRN_MCP_REGISTRY_MAX_BYTES", "200000"))
        self.timeout_s = float(os.environ.get("CAIRN_MCP_REGISTRY_TIMEOUT_S", "60"))
        self._tools: dict[str, tuple[float, list[dict]]] = {}
        self._calls: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    # ---- registrations -------------------------------------------------------------------------------------------
    def _need(self, p, privilege: str, target: Optional[str]) -> None:
        try:
            ok = bool(self.authorize(p, privilege, target))
        except Exception:  # noqa: BLE001 - fail closed
            ok = False
        if not ok:
            raise RegistryError(f"{privilege} is needed" + (f" on {target}" if target else ""), 403)

    def _row(self, name: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(mcp_servers).where(mcp_servers.c.name == name)).mappings().first()
        if r is None:
            raise RegistryError("no such MCP server", 404)
        return dict(r)

    @staticmethod
    def view(row: dict) -> dict:
        return {"name": row["name"], **{k: row[k] for k in FIELDS}, "owner": row["owner"],
                "urn": urn(row["name"]), "created_ms": row["created_ms"], "updated_ms": row["updated_ms"]}

    def _may_use(self, p, row: dict) -> bool:
        try:
            return bool(self.authorize(p, USE, urn(row["name"])))
        except Exception:  # noqa: BLE001 - fail closed
            return False

    def list(self, p) -> list[dict]:
        """The servers the caller may use (all of them for whoever manages the registry)."""
        with self.engine.connect() as c:
            rows = [dict(r) for r in c.execute(select(mcp_servers).order_by(mcp_servers.c.name)).mappings()]
        try:
            manager = bool(self.authorize(p, MANAGE, None))
        except Exception:  # noqa: BLE001
            manager = False
        return [self.view(r) for r in rows if manager or self._may_use(p, r)]

    def get(self, p, name: str) -> dict:
        row = self._row(name)
        if not self._may_use(p, row):
            try:
                manager = bool(self.authorize(p, MANAGE, None))
            except Exception:  # noqa: BLE001
                manager = False
            if not manager:
                raise RegistryError("no such MCP server", 404)
        return self.view(row)

    def put(self, p, name: str, body: dict) -> dict:
        self._need(p, MANAGE, None)
        if not NAME.match(name or ""):
            raise RegistryError("a server's name is lowercase letters, digits and '-' (2 to 40)")
        if TOOL_SEP in name:
            raise RegistryError(f"a server's name cannot contain {TOOL_SEP}")
        with self.engine.connect() as c:
            old = c.execute(select(mcp_servers).where(mcp_servers.c.name == name)).mappings().first()
        spec = check({k: v for k, v in body.items() if k != "name"}, {k: old[k] for k in FIELDS} if old else None)
        try:
            self._public(spec["url"])
        except RegistryError as e:
            if e.status != 502:                           # a host that does not resolve yet is checked at each connection
                raise
        from cairn.agentkit.scan import blocking, check_mcp_server

        unsafe = blocking(check_mcp_server(name, spec))      # L164: an unsafe registration is never stored
        if unsafe:
            raise RegistryError("refused by the agent security scan: " + "; ".join(f"{f.rule}: {f.message}" for f in unsafe),
                                422)
        if spec["auth"]["type"] != "none":                # the owner must be able to read it now, not at first use
            self._secret(p.urn, spec["auth"])
        now = self.clock()
        with self.engine.begin() as c:
            if old is None:
                c.execute(insert(mcp_servers).values(name=name, owner=p.urn, created_ms=now, updated_ms=now, **spec))
            else:
                c.execute(update(mcp_servers).where(mcp_servers.c.name == name).values(updated_ms=now, owner=p.urn,
                                                                                        **spec))
        with self._lock:
            self._tools.pop(name, None)
        self._audit(p, "mcp_server.saved", name, {"url_host": _host(spec["url"]), "auth": spec["auth"]["type"],
                                                   "read_only": spec["read_only"], "enabled": spec["enabled"]})
        return self.view(self._row(name))

    def delete(self, p, name: str) -> None:
        self._need(p, MANAGE, None)
        self._row(name)
        with self.engine.begin() as c:
            c.execute(delete(mcp_servers).where(mcp_servers.c.name == name))
        with self._lock:
            self._tools.pop(name, None)
        self._audit(p, "mcp_server.deleted", name, {})

    # ---- reaching a server ---------------------------------------------------------------------------------------
    def _public(self, url: str) -> None:
        import socket

        from cairn.common.egress import EgressRefused, check_public

        try:
            check_public(url, "the MCP server's url", allow_http=self.allow_http, allow_private=self.allow_private,
                         resolve=socket.getaddrinfo)
        except EgressRefused as e:
            hint = " (an operator allows private networks with CAIRN_MCP_REGISTRY_ALLOW_PRIVATE=true)" \
                if "private" in str(e) else ""
            raise RegistryError(str(e) + hint, e.status)

    def _secret(self, owner: str, auth: dict) -> str:
        if self.read_secret is None:
            raise RegistryError("secret scopes are not configured here (CAIRN_SECRETS_KEY)", 503)
        try:
            return str(self.read_secret(owner, auth["scope"], auth["key"]))
        except Exception as e:  # noqa: BLE001 - fail closed, without the value
            raise RegistryError(f"the credential {auth['scope']}/{auth['key']} cannot be read by the server's owner "
                                f"{owner} ({type(e).__name__})", 409)

    def _headers(self, row: dict) -> dict:
        auth = row["auth"] or {"type": "none"}
        if auth.get("type") == "bearer":
            return {"Authorization": f"Bearer {self._secret(row['owner'], auth)}"}
        if auth.get("type") == "header":
            return {auth["header"]: self._secret(row["owner"], auth)}
        return {}

    async def _session(self, row: dict, work):
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        self._public(row["url"])                          # again at every connection: DNS can change
        client = create_mcp_http_client(headers=self._headers(row))
        client.follow_redirects = False                   # a redirect would carry the credential elsewhere
        async with client:
            async with streamable_http_client(row["url"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await asyncio.wait_for(s.initialize(), self.timeout_s)
                    return await asyncio.wait_for(work(s), self.timeout_s)

    def _usable(self, p, name: str) -> dict:
        row = self._row(name)
        if not self._may_use(p, row):
            raise RegistryError(f"{USE} is needed on {urn(name)}", 403)
        if not row["enabled"]:
            raise RegistryError(f"{name} is disabled", 409)
        return row

    async def tools(self, p, name: str, fresh: bool = False, max_age_s: float = TOOLS_TTL_S) -> list[dict]:
        """The server's tools that the registration lets through (cached a minute)."""
        row = self._usable(p, name)
        with self._lock:
            hit = self._tools.get(name)
        if hit and not fresh and time.time() - hit[0] < max_age_s:
            listed = hit[1]
        else:
            async def work(s):
                out, cursor = [], None
                for _ in range(20):
                    page = await s.list_tools(cursor=cursor) if cursor else await s.list_tools()
                    out += [t.model_dump(by_alias=True, exclude_none=True) for t in page.tools]
                    cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
                    if not cursor:
                        break
                return out
            try:
                listed = await self._session(row, work)
            except RegistryError:
                raise
            except Exception as e:  # noqa: BLE001 - the server's own failure, named without its details
                raise RegistryError(f"{name} did not answer ({type(e).__name__})", 502)
            with self._lock:
                self._tools[name] = (time.time(), listed)
        from cairn.ai.guardrails import screen

        flagged: list[str] = []
        out = [screen(_tool_view(t), flagged) for t in listed if offered(row, t)]   # their text is a third party's
        if flagged:
            self._audit(p, "mcp_server.tools_flagged", name, {"samples": flagged[:3]})
        return out

    def _rate(self, p, row: dict) -> None:
        limit = row["rpm"] or self.default_rpm
        now = time.time()
        with self._lock:
            if len(self._calls) > 1000:                   # forget people who stopped calling
                self._calls = {k: v for k, v in self._calls.items() if v and now - v[-1] < 60}
            calls = [t for t in self._calls.get((p.urn, row["name"]), []) if now - t < 60]
            if len(calls) >= limit:
                raise RegistryError(f"more than {limit} calls a minute to {row['name']}: wait a moment", 429)
            calls.append(now)
            self._calls[(p.urn, row["name"])] = calls

    async def call(self, p, name: str, tool: str, arguments: Optional[dict] = None) -> dict:
        """One tool of a registered server, as ``p``: allowed by the registration, within the rate, through abuse
        detection for agents; the answer screened, redacted and capped; audited, whatever the outcome."""
        started, status = time.monotonic(), "refused"
        try:
            out = await self._call(p, name, tool, arguments)
            status = "error" if out.get("is_error") else "ok"
            return out
        except RegistryError as e:
            status = {403: "refused", 404: "not_offered", 429: "rate_limited", 502: "failed"}.get(e.status, "refused")
            raise
        finally:
            self._audit(p, "mcp_server.called", name, {"tool": tool, "status": status,
                                                        "ms": int((time.monotonic() - started) * 1000)})

    async def _call(self, p, name: str, tool: str, arguments: Optional[dict]) -> dict:
        from cairn.ai.guardrails import screen
        from cairn.ai.mcp_api import redact_answer

        row = self._usable(p, name)
        # The offered tools as listed within ten minutes: a tool newly allowed or added is found on a fresh listing.
        known = {t["name"]: t for t in await self.tools(p, name, max_age_s=600)}
        if tool not in known:
            known = {t["name"]: t for t in await self.tools(p, name, fresh=True)}
        if tool not in known:
            raise RegistryError(f"{name} offers no tool {tool} here (the registration may not allow it)", 404)
        args = arguments or {}
        if not isinstance(args, dict):
            raise RegistryError("arguments is an object")
        if getattr(p, "via", None) == "agent":
            # Whatever the server says of its tool, a call leaves Cairn and may change something: an agent needs its
            # write scope, as on the REST route, and its declared intent is checked like that route's.
            if "write" not in (getattr(p, "scopes", None) or ()):
                raise RegistryError(f"this agent's scopes do not include write (needed to call {tool})", 403)
            if self.intents is not None:
                from cairn.identity.intents import IntentDenied, classify_request

                try:
                    self.intents.check(p, classify_request("POST", CALL_ROUTE, set()), [], tool=f"{name}{TOOL_SEP}{tool}")
                except IntentDenied as e:
                    raise RegistryError(str(e), 403)
        self._rate(p, row)
        if self.abuse is not None and getattr(p, "via", None) == "agent":
            from cairn.ai.abuse import AbuseBlocked

            try:
                self.abuse.inspect(p.urn, p.agent, getattr(p, "run", None), "mcp-registry",
                                   tool_calls=[(f"{name}{TOOL_SEP}{tool}", args)])
            except AbuseBlocked as e:
                raise RegistryError(str(e), 403)
        try:
            res = await self._session(row, lambda s: s.call_tool(tool, args))
            if res.structured_content is not None:
                body: Any = res.structured_content
            else:
                body = "\n".join(getattr(c, "text", "") or f"[{getattr(c, 'type', 'content')}]" for c in res.content)
            body = redact_answer(body)
            flagged: list[str] = []
            body = screen(body, flagged)
            if self.abuse is not None and getattr(p, "via", None) == "agent":
                self.abuse.inspect(p.urn, p.agent, getattr(p, "run", None), "mcp-registry",
                                   [("tool_result", json.dumps(body, default=str)[:200_000])], block=False,
                                   cap="warning")
            text = json.dumps(body, default=str)
            out: dict[str, Any] = {"server": name, "tool": tool, "is_error": bool(res.is_error)}
            if len(text) > self.max_bytes:
                out.update(truncated=True, preview=text[:self.max_bytes])
            else:
                out["result"] = body
            if flagged:
                out["flagged"] = len(flagged)
            return out
        except RegistryError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RegistryError(f"{name} failed to run {tool} ({type(e).__name__})", 502)

    def _audit(self, p, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ai", action, p.urn, urn(name),
                              {**details, **({"agent": p.agent} if getattr(p, "agent", None) else {})})


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def _tool_view(t: dict) -> dict:
    return {"name": t.get("name"), "title": t.get("title"), "description": str(t.get("description") or "")[:2000],
            "input_schema": t.get("inputSchema") or t.get("input_schema") or {"type": "object"},
            "annotations": t.get("annotations") or {}}


def build_mcp_registry_router(reg: McpRegistry, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/ai/mcp-servers", tags=["ai"])

    async def run(fn):
        try:
            out = fn()
            return await out if asyncio.iscoroutine(out) else out
        except RegistryError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_servers(p=Depends(current_principal)):
        """The external MCP servers registered in Cairn that you may use."""
        return await run(lambda: {"servers": reg.list(p)})

    @router.get("/{name}")
    async def get_server(name: str, p=Depends(current_principal)):
        """One registered MCP server (its credential is a secret scope reference, never a value)."""
        return await run(lambda: reg.get(p, name))

    @router.put("/{name}")
    async def put_server(name: str, body: dict = Body(...), p=Depends(current_principal)):
        """Register or change an external MCP server (MANAGE_MCP_SERVERS)."""
        return await run(lambda: reg.put(p, name, body))

    @router.delete("/{name}", status_code=204)
    async def delete_server(name: str, p=Depends(current_principal)):
        """Remove a registered MCP server (MANAGE_MCP_SERVERS)."""
        await run(lambda: reg.delete(p, name))

    @router.get("/{name}/tools")
    async def server_tools(name: str, fresh: bool = False, p=Depends(current_principal)):
        """The tools of a registered server that its registration lets you use."""
        return {"tools": await run(lambda: reg.tools(p, name, fresh))}

    @router.post("/{name}/tools/{tool}/call")
    async def call_tool(name: str, tool: str, body: dict = Body(default={}), p=Depends(current_principal)):
        """Call a tool of a registered server, as you: {"arguments": {...}}."""
        return await run(lambda: reg.call(p, name, tool, body.get("arguments") or {}))

    return router


__all__ = ["McpRegistry", "RegistryError", "build_mcp_registry_router", "check", "mcp_servers", "offered", "urn"]
