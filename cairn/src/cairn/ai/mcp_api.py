"""Cairn's whole API as MCP tools (L154): every operation of the API, generated from its OpenAPI description,
grouped into toolsets, and called in-process as the person (or agent) signed in to MCP.

A client chooses what it sees:

* the curated tools (:mod:`cairn.ai.mcp_server`) and three that reach everything: ``api_search`` (find an
  operation by words), ``api_describe`` (its arguments) and ``api_call`` (call it). Always there;
* **toolsets**, one tool per operation: ``CAIRN_MCP_TOOLSETS`` (comma-separated names, or ``all``) for the server,
  narrowed per connection with ``?toolsets=catalog,flows`` on the MCP URL (or the ``X-Cairn-Toolsets`` header).
  Clients cope badly with hundreds of tools (GitHub Copilot uses at most 128 at once), so no toolset holds more.

Every call:

* goes through Cairn's own API, in-process, **with the caller's credentials**: the same sign-in, policy decision
  point, agent scopes (an agent key reaches only what its scopes allow, ``cairn.identity.agents.required_scope``),
  quotas and audit as any request. MCP adds nothing a person could not do in the UI or the CLI;
* is announced to the client for what it is: reads as read-only, deletions as destructive (clients ask the person
  before running those);
* never hands a credential to a model: operations that issue or reveal one (:data:`EXCLUDED`) are not tools, and
  every answer has credential-looking values and fields redacted, catalog text screened for planted instructions,
  and its size capped (``CAIRN_MCP_API_MAX_BYTES``).

Coverage is enforced: every operation of the API is in a toolset or in :data:`EXCLUDED` with a reason
(``tests/test_mcp_api.py`` fails otherwise), so a new endpoint is a new tool the day it ships.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

METHODS = ("get", "post", "put", "patch", "delete")
MAX_TOOLS_PER_SET = 128

# Toolsets: the first segment of the path (after /v1) -> toolset. A new segment must be placed here (or excluded).
TOOLSETS: dict[str, tuple[str, ...]] = {
    "catalog": ("catalog", "assets", "datasets", "sources", "openlineage", "context", "collab", "history",
                "samples", "documents", "lineage", "glossary", "domains", "ontology", "search", "products"),
    "governance": ("governance",),
    "quality": ("quality", "marketplace", "contracts"),
    "sql": ("sql", "sql-workflows", "procedures", "statements", "data"),
    "bi": ("reports", "embed", "dashboards", "workbooks", "metrics"),
    "lakehouse": ("storage", "volumes", "medallion", "lakehouse", "warehouses", "external-catalogs", "shares",
                  "tables", "branches"),
    "pipelines": ("flows", "pipelines", "jobs", "airflow", "airflow-deployments", "compute", "workspaces", "dataplanes",
                  "sandboxes", "ml", "git-folders", "bundles", "streams", "pipes", "mirrors", "saas", "nifi",
                  "connector-syncs",
                  "spark", "dbt", "notebooks", "derived", "databases"),
    "apps": ("apps", "app-installs", "app-packages", "extensions", "integrations", "forms", "automations"),
    "ai": ("ai", "custom-agents", "agent-abuse", "agent-intents", "agent-traces", "agent-memory", "agent-guard",
           "agent-projects", "agents"),
    "operations": ("observability", "sre", "chargeback", "budgets", "network", "notify", "status", "metering",
                   "deprecations", "ui", "desktop"),
    "iac": ("iac",),
    "administration": ("admin", "people", "secret-scopes", "me", "personas", "purposes"),
}
_SET_OF = {root: name for name, roots in TOOLSETS.items() for root in roots}

# Operations that are never tools, and why. (method or "*", path template or prefix ending in "/") -> reason.
EXCLUDED: dict[tuple[str, str], str] = {
    ("*", "/scim/"): "SCIM is for identity providers provisioning people, not for people or agents",
    ("*", "/oauth/"): "the OAuth sign-in protocol itself (an MCP client signs in through it, not with it)",
    ("*", "/v1/passkeys/"): "passkeys need the person's own authenticator in a browser",
    ("*", "/v1/passkeys"): "passkeys need the person's own authenticator in a browser",
    ("GET", "/v1/secret-scopes/{scope}/secrets/{key}"): "returns a secret's value: never into a model's context",
    ("PUT", "/v1/secret-scopes/{scope}/secrets/{key}"): "a secret's value would pass through the model: set it in "
                                                        "the UI or the CLI",
    ("POST", "/v1/volumes/{name}/credentials"): "issues storage credentials",
    ("POST", "/v1/storage/credentials"): "takes a cloud key's secret in its body",
    ("PATCH", "/v1/storage/credentials/{name}"): "takes a cloud key's secret in its body",
    ("POST", "/v1/dataplanes/{name}/enrollment-tokens"): "issues a data plane's enrollment token",
    ("POST", "/embed/v1/tokens"): "issues an embedding token",
    ("*", "/notify/actions/"): "signed one-click links from notifications, for the person who received them",
    ("POST", "/governance/tokens/domains/{domain}/detokenize"): "returns the raw values behind tokens: never into a "
                                                                "model's context",
    ("POST", "/admin/agents"): "returns a new agent key",
    ("POST", "/admin/agents/{agent_id}/rotate"): "returns a new agent key",
    ("POST", "/v1/me/tokens"): "returns a new personal access token",
    ("POST", "/v1/lakehouse/sessions"): "returns a lakehouse session's credential",
    ("POST", "/v1/lakehouse/oauth/tokens"): "issues lakehouse tokens",
    ("*", "/v1/trino/"): "Trino's own access-control and sign-in endpoints (Trino calls them, not people)",
    ("*", "/v1/nifi/deployments/{name}/ui/"): "NiFi's canvas, proxied for a browser",
    ("*", "/v1/airflow/deployments/{name}/ui/"): "Airflow's UI, proxied for a browser",
}
# Roots no tool is made for, and why (whole areas).
EXCLUDED_ROOTS: dict[str, str] = {
    "scim": EXCLUDED[("*", "/scim/")],
    "oauth": EXCLUDED[("*", "/oauth/")],
    "passkeys": EXCLUDED[("*", "/v1/passkeys/")],
    ".well-known": "discovery documents for clients",
    "health": "liveness, for orchestrators",
    "metrics": "Prometheus scrapes",
}


def _excluded(method: str, path: str) -> Optional[str]:
    for (m, p), why in EXCLUDED.items():
        if m not in ("*", method):
            continue
        if path == p or (p.endswith("/") and path.startswith(p)):
            return why
    root = _root(path)
    return EXCLUDED_ROOTS.get(root)


def _root(path: str) -> str:
    segs = [s for s in path.split("/") if s]
    if segs[:1] == ["v1"]:
        segs = segs[1:]
    if segs[:1] == ["embed"] and segs[1:2] == ["v1"]:
        return "embed"
    return segs[0] if segs else ""


@dataclass
class Operation:
    name: str
    method: str
    path: str
    toolset: str
    summary: str
    description: str
    params: list[dict] = field(default_factory=list)      # path and query parameters (OpenAPI)
    body: Optional[dict] = None                           # the JSON request body's schema
    body_required: bool = False

    @property
    def read_only(self) -> bool:
        return self.method == "GET"

    def input_schema(self) -> dict:
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in self.params:
            schema = dict(p.get("schema") or {"type": "string"})
            if p.get("description"):
                schema["description"] = p["description"]
            props[p["name"]] = schema
            if p.get("required") or p.get("in") == "path":
                required.append(p["name"])
        if self.body is not None:
            props["body"] = {**self.body, "description": "the request body (JSON)"}
            if self.body_required:
                required.append("body")
        out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            out["required"] = required
        return out

    def describe(self) -> dict:
        return {"name": self.name, "toolset": self.toolset, "method": self.method, "path": self.path,
                "summary": self.summary, "description": self.description[:2000], "read_only": self.read_only,
                "input_schema": self.input_schema()}


class Catalog:
    """The API's operations as tools, from its OpenAPI description."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.ops: dict[str, Operation] = {}
        self.excluded: list[tuple[str, str, str]] = []
        self.unplaced: list[tuple[str, str]] = []
        comps = (spec.get("components") or {}).get("schemas") or {}
        names: set[str] = set()
        for path, item in sorted((spec.get("paths") or {}).items()):
            for method in METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                m = method.upper()
                why = _excluded(m, path)
                if why is None and _is_catch_all(path, op):
                    why = "works on any path below it (files, or a UI proxied for a browser): the CLI and the UI do this"
                body, body_required = None, False
                rb = op.get("requestBody") or {}
                content = rb.get("content") or {}
                if why is None and content and "application/json" not in content:
                    why = "takes a file or a form, not JSON"
                if "application/json" in content:
                    body = _inline(content["application/json"].get("schema") or {}, comps)
                    body_required = bool(rb.get("required"))
                if why is not None:
                    self.excluded.append((m, path, why))
                    continue
                toolset = _SET_OF.get(_root(path))
                if toolset is None:
                    self.unplaced.append((m, path))
                    continue
                name = _name(m, path, names)
                names.add(name)
                params = [{**p, "schema": _inline(p.get("schema") or {}, comps)}
                          for p in (op.get("parameters") or []) if p.get("in") in ("path", "query")]
                self.ops[name] = Operation(name, m, path, toolset, str(op.get("summary") or "").strip(),
                                           str(op.get("description") or "").strip(), params, body, body_required)

    def toolset(self, name: str) -> list[Operation]:
        return [o for o in self.ops.values() if o.toolset == name]

    def search(self, query: str, toolset: Optional[str] = None, limit: int = 20) -> list[dict]:
        words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
        scored = []
        for op in self.ops.values():
            if toolset and op.toolset != toolset:
                continue
            hay = f"{op.name} {op.path} {op.summary} {op.description}".lower()
            score = sum(hay.count(w) + (3 if w in op.path.lower() else 0) for w in words)
            if score:
                scored.append((score, op))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [{"name": o.name, "toolset": o.toolset, "method": o.method, "path": o.path, "summary": o.summary,
                 "read_only": o.read_only} for _, o in scored[:max(1, min(limit, 50))]]


def _is_catch_all(path: str, op: dict) -> bool:
    for p in op.get("parameters") or []:
        if p.get("in") == "path" and p.get("name") in ("path", "rest", "subpath") and path.endswith("{" + p["name"] + "}"):
            return True
    return False


def _name(method: str, path: str, taken: set[str]) -> str:
    """``GET /v1/flows/{name}/runs`` -> ``get_flows_by_name_runs`` (unique, at most 64 characters)."""
    segs = [s for s in path.split("/") if s and s != "v1"]
    words = [("by_" + s[1:-1]) if s.startswith("{") else s for s in segs]
    base = re.sub(r"[^a-z0-9]+", "_", f"{method.lower()}_{'_'.join(words)}".lower()).strip("_")
    import hashlib

    # A clash (or a name too long) is told apart by a digest of the route itself, never by order, so adding a route
    # never renames an existing tool.
    digest = hashlib.sha1(f"{method} {path}".encode()).hexdigest()[:8]
    if len(base) > 64:
        base = base[:55].rstrip("_") + "_" + digest
    return base if base not in taken else base[:55].rstrip("_") + "_" + digest


def _inline(schema: Any, comps: dict, depth: int = 0) -> Any:
    """``$ref``s replaced by what they name (a few levels; deeper: any object), for clients that do not resolve them."""
    if isinstance(schema, list):
        return [_inline(s, comps, depth) for s in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        if depth >= 6:                                    # deeper than clients need: any object
            return {"type": "object"}
        target = comps.get(str(schema["$ref"]).rsplit("/", 1)[-1]) or {"type": "object"}
        return _inline(target, comps, depth + 1)
    # A schema's "title" annotation goes (noise for a model); a property *named* title is a schema and stays.
    return {k: _inline(v, comps, depth) for k, v in schema.items() if not (k == "title" and isinstance(v, str))}


def chosen(value: Optional[str], catalog: Catalog) -> set[str]:
    """The toolsets named in ``value`` (``all``: every one); unknown names are ignored."""
    names = {v.strip() for v in (value or "").split(",") if v.strip()}
    if "all" in names:
        return set(TOOLSETS)
    return names & set(TOOLSETS)


# ---- calling -------------------------------------------------------------------------------------------------------
_SECRET_FIELD = re.compile(r"(?i)^(.*[_-])?(token|access_token|refresh_token|id_token|secret|client_secret|password|"
                           r"passwd|api_?key|private_?key|secret_?key|secret_access_key|session_token|credential)$")


_PSEUDONYM = re.compile(r"^tok_[a-z2-7]{26}$")          # what tokenization returns for a value: not a credential


def redact_answer(value: Any) -> Any:
    """Credentials out of an answer before a model sees it: values of credential-named fields (strings of 8 or more
    characters) and credential-looking strings anywhere (bearer tokens, JWTs, Cairn's key formats, AWS keys,
    private keys, ``password=...``)."""
    from cairn.observability.redact import REDACTED, scrub_all

    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, str) and len(v) >= 8 and _SECRET_FIELD.match(str(k)) and not _PSEUDONYM.match(v):
                out[k] = REDACTED
            else:
                out[k] = redact_answer(v)
        return out
    if isinstance(value, list):
        return [redact_answer(v) for v in value]
    if isinstance(value, str):
        return scrub_all(value)
    return value


class ApiCallError(ValueError):
    pass


async def call(app, op: Operation, arguments: dict, headers: dict, max_bytes: Optional[int] = None) -> dict:
    """``op`` with ``arguments``, through ``app`` in-process with ``headers`` (the caller's credentials)."""
    import httpx

    args = dict(arguments or {})
    path = op.path
    for p in op.params:
        if p.get("in") != "path":
            continue
        if p["name"] not in args:
            raise ApiCallError(f"{p['name']} is required")
        path = path.replace("{" + p["name"] + "}", quote(str(args.pop(p["name"])), safe=""))
    query = {p["name"]: args.pop(p["name"]) for p in op.params if p.get("in") == "query" and p["name"] in args}
    query = {k: (json.dumps(v) if isinstance(v, (dict,)) else v) for k, v in query.items() if v is not None}
    body = args.pop("body", None)
    if args:
        raise ApiCallError(f"unknown arguments: {', '.join(sorted(args))} (api_describe shows what {op.name} takes)")
    if body is not None and op.body is None:
        raise ApiCallError(f"{op.name} takes no body")
    limit = max_bytes or int(os.environ.get("CAIRN_MCP_API_MAX_BYTES", "200000"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cairn.internal", timeout=300) as c:
        r = await c.request(op.method, path, params=query, json=body, headers=headers)
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        try:
            data: Any = r.json()
        except ValueError:
            data = r.text
    elif ctype.startswith("text/"):
        data = r.text
    else:
        data = {"content_type": ctype, "bytes": len(r.content), "note": "not text: read it through the API or the UI"}
    data = redact_answer(data)
    out: dict[str, Any] = {"status": r.status_code}
    if r.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else data
        text = json.dumps(detail, default=str)
        if len(text) > 4000:                              # an error is read, not processed: kept short
            detail = text[:4000] + " ..."
        out.update(error={401: "unauthenticated", 403: "forbidden", 404: "not_found", 409: "conflict",
                          422: "invalid"}.get(r.status_code, "failed"), detail=detail)
        return out
    text = json.dumps(data, default=str)
    if len(text) > limit:
        out.update(truncated=True, size=len(text),
                   preview=text[:limit], note="cut at CAIRN_MCP_API_MAX_BYTES: narrow the request (filters, limit)")
        return out
    out["result"] = data
    return out


# ---- registering ---------------------------------------------------------------------------------------------------
def register(server, catalog: Catalog, app: Callable[[], Any], headers: Callable[[], dict],
             observe: Callable[[str, dict, dict, float], dict], allowed: Callable[[str, dict], Optional[str]],
             enabled: set[str]) -> int:
    """One MCP tool per operation of the toolsets in ``enabled``. Every call, through its own tool or ``api_call``,
    goes through one path: the arguments checked against the operation's schema, ``allowed(tool, args)`` (abuse
    detection for agents: a message refuses it), the API in-process, then ``observe(tool, args, result, started)``,
    which screens, records and returns what the client gets. Refusals are observed too."""
    from mcp.server.mcpserver.tools import Tool
    from mcp.types import ToolAnnotations

    class ApiTool(Tool):
        async def run(self, arguments, context, convert_result=False):
            result = await _run(self.name, arguments or {})
            return self.fn_metadata.convert_result(result) if convert_result else result

    async def _run(name: str, arguments: dict) -> dict:
        import jsonschema

        op = catalog.ops.get(name)
        if op is None:
            return {"error": "not_found", "message": f"no operation {name}: find it with api_search"}
        started = time.monotonic()
        try:
            jsonschema.validate(arguments, op.input_schema())
        except jsonschema.ValidationError as e:
            return observe(name, arguments, {"error": "invalid", "message": e.message[:500]}, started)
        refused = allowed(name, arguments)
        if refused:
            return observe(name, arguments, {"error": "forbidden", "message": refused}, started)
        try:
            result = await call(app(), op, arguments, headers())
        except ApiCallError as e:
            result = {"error": "refused", "message": str(e)}
        return observe(name, arguments, result, started)

    def template() -> dict[str, Any]:                    # only its return type is used (structured output)
        return {}

    n = 0
    for setname in sorted(enabled):
        for op in catalog.toolset(setname):
            base = Tool.from_function(template, name=op.name, title=f"{op.method} {op.path}",
                                      description=_description(op), structured_output=True,
                                      annotations=ToolAnnotations(
                                          title=f"{op.method} {op.path}", readOnlyHint=op.read_only,
                                          destructiveHint=op.method == "DELETE",
                                          idempotentHint=op.method in ("GET", "PUT", "DELETE"), openWorldHint=False),
                                      meta={"cairn/toolset": op.toolset, "cairn/path": op.path})
            tool = ApiTool(**{k: getattr(base, k) for k in Tool.model_fields})
            tool.parameters = op.input_schema()
            server._tool_manager._tools[op.name] = tool
            n += 1
    server._cairn_api_run = _run                          # api_call goes through the same checks
    return n


def _description(op: Operation) -> str:
    text = op.summary or op.description.split("\n\n", 1)[0] or f"{op.method} {op.path}"
    return f"{text.strip()} ({op.method} {op.path}; toolset {op.toolset})"[:1024]


def toolset_of_tool(name: str, catalog: Catalog) -> Optional[str]:
    op = catalog.ops.get(name)
    return op.toolset if op else None


__all__ = ["Catalog", "Operation", "TOOLSETS", "EXCLUDED", "EXCLUDED_ROOTS", "call", "chosen", "redact_answer",
           "register", "MAX_TOOLS_PER_SET"]
