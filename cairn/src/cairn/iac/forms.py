"""Form schemas for the web UI's platform areas: every create, change and run action as fields, not raw JSON.

The UI's platform areas (``ui/src/consumer/resources/catalog.ts``) each list their actions as a method, a path
and an example body. This module turns every action that sends a body into a form schema, from what Cairn already
knows about the request:

1. the resource registry (:mod:`cairn.iac.registry`), when the action is a kind's create or update call: field
   types, required, defaults, descriptions, sensitive and write-only fields, references to other kinds, which
   fields go in the path and which in the body;
2. FastAPI's OpenAPI document (the routes' Pydantic request models): types, enums, ranges, patterns, nested
   objects, for the actions the registry does not cover (run, approve, ask, grant…) and to sharpen registry fields;
3. the action's example body, for request bodies the API takes as a plain JSON object, so the form still has the
   right fields and types.

On top of that, field names and :data:`HINTS` choose the widget: SQL, cron, durations, units, pickers that search
the API (tables, people and groups, connections, warehouses, secret scopes and references, git folders, flows,
agents), passwords for secrets.

``python -m cairn.iac.forms`` writes ``ui/src/consumer/resources/forms.generated.json`` (bundled with the UI, so
the offline demo has every form too); ``--check`` fails when that file is not current (CI runs it).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from typing import Any, Optional

from cairn.iac import registry

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CATALOG = os.path.join(ROOT, "ui", "src", "consumer", "resources", "catalog.ts")
OUT = os.path.join(ROOT, "ui", "src", "consumer", "resources", "forms.generated.json")
PLACE = re.compile(r"\{(\w+)\}")
DURATION = re.compile(r"(?:^|_)(seconds|minutes|hours|days|s|ms)$")
DURATION_UNIT = {"s": "seconds", "ms": "milliseconds"}
TEXTAREA = {"description", "instructions", "prompt", "question", "input", "justification", "content", "comment",
            "notes", "goal", "reason", "answer", "summary"}
PASSWORD = {"password", "token", "client_secret", "api_key", "secret"}
# Field name -> the picker that searches the API for its value (see ui/src/consumer/resources/pickers.tsx).
PICKERS = {
    "connection": "connection", "warehouse": "warehouse",
    "table": "table", "tables": "table", "source_table": "table", "datasets": "table",
    "dataset": "dataset", "dataset_urn": "dataset",
    "principal": "principal", "owner": "user", "owners": "user", "reviewers": "user", "admins": "principal",
    "members": "principal", "recipients": "principal", "approvers": "principal", "user": "user", "users": "user",
    "team": "group", "teams": "group", "groups": "group",
    "secret_scope": "secret_scope", "credential_ref": "secret_ref", "secret_ref": "secret_ref",
    "git_folder": "git_folder", "folder": "git_folder", "flow": "flow", "agent": "agent", "app": "agent",
}
# A namespace.table field is a table picker when the registry or the example says so.
TABLE_HINT = re.compile(r"(namespace|schema)\.table")


def _hint(**kw) -> dict:
    return kw


# Observability (cairn.observability): severities and languages to choose from, conditions as text.
_SEVERITY = _hint(type="select", enum=["critical", "warning", "info"])
# An SLO's indicator: a ratio of bad to all events (PromQL over [$window]), or a latency threshold on a histogram.
_OBS_INDICATOR = _hint(type="group", description="what a good event is", fields=[
    {"name": "type", "type": "select", "enum": ["ratio", "latency"], "required": True,
     "description": "ratio: bad events over all events; latency: requests slower than a threshold"},
    {"name": "errors", "type": "textarea", "description": "ratio: PromQL for bad events, with [$window]"},
    {"name": "total", "type": "textarea", "description": "ratio: PromQL for all events, with [$window]"},
    {"name": "metric", "type": "text", "description": "latency: the histogram, without _bucket"},
    {"name": "selector", "type": "text", "description": 'latency: label matchers, e.g. service_name="cairn-api"'},
    {"name": "threshold", "type": "number", "description": "latency: the bucket bound (le) a good request is under"},
])
_OBS_SLO = {"indicator": _OBS_INDICATOR,
            "routes": _hint(type="list", description="notification route ids (Administration -> Notifications)"),
            "recipients": _hint(picker="principal", type="list"), "window": _hint(pattern=r"^(\d+[smhdw])+$")}
_OBS_RULE = {"severity": _SEVERITY, "language": _hint(type="select", enum=["promql", "logql"]),
             "expr": _hint(type="textarea"), "summary": _hint(type="textarea"),
             "routes": _hint(type="list", description="notification route ids (Administration -> Notifications)"),
             "recipients": _hint(picker="principal", type="list"),
             "pending_for": _hint(pattern=r"^(\d+[smhdw])+$"), "repeat_interval": _hint(pattern=r"^(\d+[smhdw])+$")}

# A budget covers one thing (cairn.reports.budgets.SCOPES): a group of which exactly one is filled in.
BUDGET_SCOPE = _hint(type="group", one_of=True, description="what the budget covers: fill in one", fields=[
    {"name": "all", "type": "boolean", "description": "everything on the platform"},
    {"name": "domain", "type": "text", "pattern": r"^urn:li:domain:.+", "description": "a domain (urn:li:domain:…)"},
    {"name": "owner", "type": "picker", "picker": "principal", "description": "what a person or a group owns"},
    {"name": "platform", "type": "text", "pattern": r"^[a-z0-9_-]{1,64}$", "description": "a platform, such as snowflake"},
    {"name": "tables", "type": "picker", "picker": "dataset", "multiple": True, "description": "these tables"},
    {"name": "warehouse", "type": "picker", "picker": "warehouse", "description": "a Cairn warehouse's node-hours"},
    {"name": "ai", "type": "boolean", "description": "AI spend"},
])
BUDGET = {"scope": BUDGET_SCOPE, "period": _hint(type="select", enum=["month", "week", "day"]),
          "thresholds": _hint(item="integer", unit="%"), "notify": _hint(picker="principal")}

# Per action ("METHOD path" as the catalog writes it), per field: what the request itself cannot say.
HINTS: dict[str, dict[str, dict]] = {
    "POST /v1/sandboxes/runs": {"language": _hint(type="select", enum=["python"]), "code": _hint(type="code")},
    "POST /ai/scheduled-tasks": {"cron": _hint(type="cron"), "prompt": _hint(type="textarea")},
    "POST /v1/jobs": {"code": _hint(type="code")},
    "PUT /v1/jobs/{name}": {"code": _hint(type="code")},
    "POST /quality/monitors": {"config": _hint(type="json")},
    "PUT /quality/monitors/{id}": {"config": _hint(type="json")},
    "POST /v1/budgets": BUDGET,
    "PATCH /v1/budgets/{id}": BUDGET,
    "PUT /v1/admin/limits": {"users": _hint(type="keyvalue", value="integer", key_picker="user"),
                             "ip_allowlist": _hint(pattern=r"^[0-9a-fA-F:.]+(/\d{1,3})?$"),
                             "trusted_proxies": _hint(pattern=r"^[0-9a-fA-F:.]+(/\d{1,3})?$")},
    "PUT /v1/agent-abuse/settings": {"block_at": _hint(type="select", enum=["warning", "critical"])},
    "POST /v1/me/agent-memories": {"scope": _hint(type="select", enum=["person", "team", "shared"]), "content": _hint(type="textarea"),
                                   "agent": _hint(picker="agent")},
    "POST /v1/documents/parse": {"content": _hint(type="file")},
    "POST /v1/agent-apps": {"bundle": _hint(type="file", accept=".tar.gz,.tgz")},
    "POST /governance/consent/purposes": {"mode": _hint(type="select", enum=["opt_out", "opt_in"])},
    "PUT /governance/consent/purposes/{id}": {"mode": _hint(type="select", enum=["opt_out", "opt_in"])},
    "POST /governance/consent/purposes/{id}/records": {},
    "PUT /v1/volumes/{name}/grants": {"access": _hint(type="select", enum=["read", "write"])},
    "PUT /v1/secret-scopes/{name}/acl": {"permission": _hint(type="select", enum=["READ", "WRITE", "MANAGE"])},
    "POST /storage/v1/keys": {"buckets": _hint(type="list")},
    "PUT /storage/v1/buckets/{bucket}/quota": {"max_bytes": _hint(unit="bytes"), "max_objects": _hint(unit="objects")},
    "PUT /maintenance/policies/{scope}": {"optimize_threshold": _hint(pattern=r"^\d+(KB|MB|GB)$"),
                                           "cluster": _hint(type="select", enum=["auto", "off"], open=True)},
    "POST /v1/databases/{name}/credentials": {"access": _hint(type="select", enum=["read", "write"])},
    "POST /v1/pipeline-agent/tasks": {"goal": _hint(type="textarea"), "target": _hint(picker="table")},
    "POST /ai/steward/decide": {"action": _hint(type="select", enum=["accept", "reject"])},
    "POST /v1/streams": {"bad_rows": _hint(type="select", enum=["fail", "quarantine"]),
                         "transform": _hint(type="sql")},
    "POST /v1/derived": {"mode": _hint(type="select", enum=["full", "append", "merge", "scd2"]),
                         "layer": _hint(type="select", enum=["bronze", "silver", "gold"])},
    "PUT /v1/medallion/{table}": {"layer": _hint(type="select", enum=["bronze", "silver", "gold"])},
    "POST /marketplace/listings/{id}/access-requests": {"duration_days": _hint(min=1)},
    "PUT /v1/ai/gateway/routes/{name}": {"strategy": _hint(type="select", enum=["ordered", "cheapest", "fastest"], open=True)},
    "PUT /quality/contracts": {"dataset": _hint(picker="dataset"), "owner": _hint(picker="user"),
                               "version": _hint(pattern=r"^\d+\.\d+\.\d+$")},
    "POST /quality/contracts/check": {"dataset": _hint(picker="dataset")},
    "POST /v1/people/users": {"manager": _hint(picker="user"), "attributes": _hint(type="keyvalue")},
    "PUT /v1/people/users/{id}": {"manager": _hint(picker="user"), "attributes": _hint(type="keyvalue")},
    "POST /v1/people/groups/{group}/members": {"person": _hint(picker="user")},
    "POST /governance/access-reviews": {"personas": _hint(type="list")},
    "POST /v1/chargeback/statements": {"month": _hint(type="month")},
    "POST /v1/observability/slos": _OBS_SLO, "PUT /v1/observability/slos/{id}": _OBS_SLO,
    "POST /v1/observability/alert-rules": _OBS_RULE, "PUT /v1/observability/alert-rules/{id}": _OBS_RULE,
    "POST /v1/observability/alert-rules/test": {"language": _hint(type="select", enum=["promql", "logql"]),
                                                "expr": _hint(type="textarea")},
    "PUT /notify/routes/{id}": {"channel": _hint(type="select",
                                                 enum=["slack", "teams", "webhook", "ticket", "kafka", "eventbridge",
                                                       "pubsub"])},
    "PUT /workspaces/{id}": {"domains": _hint(type="list"), "admin_permissions": _hint(type="list"),
                             "member_permissions": _hint(type="list")},
    "PUT /marketplace/slos/{id}": {"kind": _hint(type="select", enum=["freshness", "volume", "quality", "all"]),
                                   "objective": _hint(unit="%", min=0, max=100)},
    "POST /v1/apps/{name}/chat": {"view": _hint(type="keyvalue")},
    # The other organisation's Iceberg warehouse, not one of Cairn's: typed, not picked from Cairn's warehouses.
    "POST /v1/external-catalogs": {"warehouse": _hint(no_picker=True, description="the warehouse name the other "
                                                      "catalog expects, if it asks for one")},
}


# ---- the catalog --------------------------------------------------------------------------------------------------
def catalog_specs(path: str = CATALOG) -> dict:
    """The UI's ``RESOURCE_SPECS`` (the object literal in catalog.ts is JSON)."""
    src = open(path, encoding="utf-8").read()
    start = src.index("{", src.index("=", src.index("RESOURCE_SPECS")))
    depth, in_str, i = 0, False, start
    while True:
        c = src[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return json.loads(src[start:i + 1])


def form_key(action: dict) -> str:
    """The form an action draws: its ``form`` when it names another action's, else its own method and path."""
    return action.get("form") or f"{action['method']} {action['path']}"


def needs_form(action: dict) -> bool:
    """A create, change or run that sends a body the person fills in (DELETEs and fixed bodies are confirmations)."""
    return action["method"] in ("POST", "PUT", "PATCH") and action.get("body") is not None and not action.get("fixed")


# ---- OpenAPI ------------------------------------------------------------------------------------------------------
def openapi_document() -> dict:
    """The API's OpenAPI with every optional router mounted (only the route table is read; nothing is served).

    Optional modules mount their routes only when their service is configured; here each one that is off is stood in
    for, so their request models are in the document too."""
    import dataclasses
    import logging
    import warnings
    from unittest import mock

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.services import build_services

    logging.disable(logging.WARNING)
    try:
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
            warnings.simplefilter("ignore")
            settings = Settings(database_url=f"sqlite:///{d}/forms.db", service_token="forms",
                                gateway={"key_dir": os.path.join(d, "keys")})
            services = build_services(settings, graph=InMemoryGraph(), with_ai=True, with_gateway=True)
            for f in dataclasses.fields(services):
                if getattr(services, f.name) is None:
                    setattr(services, f.name, mock.MagicMock(name=f.name))
            return create_api_app(services).openapi()
    finally:
        logging.disable(logging.NOTSET)


def _norm(path: str) -> str:
    return PLACE.sub("{}", path.split("?")[0])


def _deref(schema: Optional[dict], doc: dict) -> dict:
    schema = schema or {}
    while "$ref" in schema:
        schema = doc["components"]["schemas"][schema["$ref"].split("/")[-1]]
    for k in ("anyOf", "oneOf"):
        if k in schema:
            options = [o for o in schema[k] if o.get("type") != "null"]
            if len(options) == 1:
                merged = {**{x: y for x, y in schema.items() if x != k}, **_deref(options[0], doc)}
                return merged
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return {**{x: y for x, y in schema.items() if x != "allOf"}, **_deref(schema["allOf"][0], doc)}
    return schema


def request_schema(doc: dict, method: str, path: str) -> Optional[dict]:
    """The JSON request body schema of ``METHOD path`` (placeholder names may differ), or None."""
    want = _norm(path)
    for p, ops in doc.get("paths", {}).items():
        if _norm(p) == want and method.lower() in ops:
            body = ops[method.lower()].get("requestBody") or {}
            return _deref(body.get("content", {}).get("application/json", {}).get("schema"), doc) or None
    return None


def route_exists(doc: dict, method: str, path: str) -> bool:
    want = _norm(path)
    return any(_norm(p) == want and method.lower() in ops for p, ops in doc.get("paths", {}).items())


# ---- fields -------------------------------------------------------------------------------------------------------
def humanize(name: str) -> str:
    s = re.sub(r"_(ms|usd)$", "", name)
    s = s.replace("_", " ").strip()
    s = re.sub(r"\burn\b", "URN", s)
    s = re.sub(r"\bid\b", "ID", s)
    s = re.sub(r"\b(sql|url|uri|ip|ttl|cdc|ai|pii)\b", lambda m: m.group(1).upper(), s)
    return s[:1].upper() + s[1:]


def _enum_from_description(desc: str) -> tuple[list[str], bool]:
    """``"csv, json (lines) or parquet"`` -> the options; ``"a, b, ..."`` -> options that are suggestions only."""
    text = re.sub(r"\s*\([^)]*\)", "", desc or "").strip()
    open_ = text.endswith("...") or text.endswith("…")
    text = text.rstrip(".… ").rstrip(",")
    if " or " not in text and not open_:
        return [], False
    parts = [p.strip() for p in re.split(r",\s*|\s+or\s+", text) if p.strip()]
    if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z_][\w-]*", p) for p in parts):
        return [], False
    return parts, open_


def _enum_from_pattern(pattern: str) -> list[str]:
    m = re.fullmatch(r"\^\(([\w|-]+)\)\$", pattern or "")
    return m.group(1).split("|") if m else []


def _example_field(name: str, value: Any) -> dict:
    """A field inferred from an example value."""
    f: dict[str, Any] = {"name": name}
    if isinstance(value, bool):
        f["type"] = "boolean"
    elif isinstance(value, int):
        f["type"] = "integer"
    elif isinstance(value, float):
        f["type"] = "number"
    elif isinstance(value, str):
        if "base64" in value:
            f["type"] = "file"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            f["type"] = "date"
        elif re.fullmatch(r"\d{4}-\d{2}", value):
            f["type"] = "month"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", value):
            f["type"] = "datetime"
        else:
            f["type"] = "textarea" if ("\n" in value or len(value) > 90) else "text"
    elif isinstance(value, list):
        if all(isinstance(x, dict) for x in value) and value:
            keys: dict[str, Any] = {}
            for x in value:
                for k, v in x.items():
                    keys.setdefault(k, v)
            if all(not isinstance(v, (dict, list)) or _flat_list(v) for v in keys.values()) and \
                    all(re.fullmatch(r"[a-z_][a-z0-9_]*", k) for k in keys):
                f["type"] = "objects"
                f["fields"] = [_example_field(k, v) for k, v in keys.items()]
            else:
                f["type"] = "json"
        elif all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value) and value:
            f["type"] = "list"
            f["item"] = "integer" if all(isinstance(x, int) for x in value) else "number"
        elif all(isinstance(x, str) for x in value):
            f["type"] = "list"
            if value and all(("\n" in x or len(x) > 90) for x in value):
                f["item"] = "textarea"
        else:
            f["type"] = "json"
    elif isinstance(value, dict):
        if value and all(re.fullmatch(r"[a-z_][a-z0-9_]*", k) for k in value) and \
                all(not isinstance(v, dict) for v in value.values()) and len(value) <= 8 and \
                not all(isinstance(v, str) for v in value.values()):
            f["type"] = "group"
            f["fields"] = [_example_field(k, v) for k, v in value.items()]
        elif all(isinstance(v, (str, int, float, bool)) for v in value.values()):
            f["type"] = "keyvalue"
            vals = list(value.values())
            if vals and all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
                f["value"] = "integer"
        else:
            f["type"] = "json"
    else:
        f["type"] = "text"
    return f


def _flat_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v)


def _openapi_field(name: str, schema: dict, doc: dict, required: bool) -> dict:
    s = _deref(schema, doc)
    nullable = any(o.get("type") == "null" for o in schema.get("anyOf", []))
    f: dict[str, Any] = {"name": name}
    t = s.get("type")
    enum = s.get("enum") or _enum_from_pattern(s.get("pattern", ""))
    if enum:
        f["type"], f["enum"] = "select", [e for e in enum if e is not None]
    elif t == "boolean":
        f["type"] = "boolean"
    elif t == "integer":
        f["type"] = "integer"
    elif t == "number":
        f["type"] = "number"
    elif t == "array":
        item = _deref(s.get("items"), doc)
        if item.get("enum"):
            f["type"], f["enum"] = "multiselect", item["enum"]
        elif item.get("type") == "object" and item.get("properties"):
            f["type"] = "objects"
            req = set(item.get("required", []))
            f["fields"] = [_openapi_field(k, v, doc, k in req) for k, v in item["properties"].items()]
        elif item.get("type") in ("string", None) and "properties" not in item and item.get("type") != "array":
            f["type"] = "list"
        elif item.get("type") in ("integer", "number"):
            f["type"], f["item"] = "list", item["type"]
        else:
            f["type"] = "json"
    elif t == "object" or "properties" in s:
        if s.get("properties"):
            f["type"] = "group"
            req = set(s.get("required", []))
            f["fields"] = [_openapi_field(k, v, doc, k in req) for k, v in s["properties"].items()]
        else:
            f["type"] = "json"
    else:
        f["type"] = "text"
        if s.get("format") == "date":
            f["type"] = "date"
        elif s.get("format") == "date-time":
            f["type"] = "datetime"
        if s.get("pattern") and not enum:
            f["pattern"] = s["pattern"]
    for src, dst in (("minimum", "min"), ("maximum", "max"), ("minLength", "min_length"), ("maxLength", "max_length")):
        if src in s:
            f[dst] = s[src]
    if "default" in s and s["default"] is not None:
        f["default"] = s["default"]
    if s.get("description"):
        f["description"] = s["description"]
    if required and not nullable:
        f["required"] = True
    return f


def _kind_field(k: registry.Kind, field: registry.Field) -> dict:
    f: dict[str, Any] = {"name": field.name}
    t = {"string": "text", "bool": "boolean", "int": "integer", "float": "number", "list": "list", "json": "json"}[field.type]
    f["type"] = t
    if t == "text":
        options, open_ = _enum_from_description(field.description)
        if options:
            f["type"], f["enum"] = "select", options
            if open_:
                f["open"] = True
    if field.required:
        f["required"] = True
    if field.default is not registry.UNSET and field.default is not None and field.default != "" and \
            field.default != [] and field.default != {}:
        f["default"] = field.default
    if field.description:
        f["description"] = field.description
    if field.write_only:
        f["write_only"] = True
    if field.sensitive:
        f["sensitive"] = True
    if field.read:
        f["read_as"] = field.read
    if field.force_new:
        f["immutable"] = True
    if field.computed:
        f["computed"] = True
    if field.ref:
        other, attr = field.ref.split(".")
        ref = registry.kind(other)
        f["picker"] = "ref"
        f["ref"] = {"kind": other, "read": ref.read, "list_key": ref.list_key if ref.list_key != "." else "",
                    "value": attr if attr != "id" else ref.id, "label": ref.key[-1] if ref.key else ref.id}
    return f


def _merge(base: dict, extra: dict) -> dict:
    """Sharpen ``base`` with what ``extra`` knows and base does not (types only when base is generic)."""
    out = dict(base)
    if out.get("type") in ("json", "text", "list") and extra.get("type") not in (None, "text", "json") and \
            not (out.get("type") == "list" and extra.get("type") == "list"):
        out["type"] = extra["type"]
        for k in ("fields", "item", "value", "enum"):
            if k in extra:
                out[k] = extra[k]
    if extra.get("enum") and out.get("type") in ("select", "multiselect") and out.get("enum") != extra["enum"]:
        out["enum"] = extra["enum"]
        out.pop("open", None)
    for k in ("enum", "min", "max", "pattern", "min_length", "max_length", "item", "value"):
        if k in extra and k not in out and out.get("type") not in ("json",):
            out[k] = extra[k]
    if not out.get("description") and extra.get("description"):
        out["description"] = extra["description"]
    if out.get("type") in ("group", "objects") and extra.get("fields") and out.get("fields"):
        theirs = {x["name"]: x for x in extra["fields"]}
        out["fields"] = [_merge(x, theirs[x["name"]]) if x["name"] in theirs else x for x in out["fields"]]
        known = {x["name"] for x in out["fields"]}
        out["fields"] += [x for x in extra["fields"] if x["name"] not in known]
    return out


def _widget(f: dict, path_has_secret: bool) -> dict:
    """Choose the widget from the field's name, when the type alone says only text or number."""
    name, t = f["name"], f["type"]
    if f.get("write_only") or (f.get("sensitive") and t in ("text", "textarea")) or \
            (t == "text" and (name in PASSWORD or name.endswith("_password") or name.endswith("_secret"))) or \
            (path_has_secret and name == "value"):
        f["type"] = "password"
        f.pop("picker", None)
        return f
    if f.get("sensitive"):
        f.pop("picker", None)
        return f
    if t in ("text", "textarea") and (name in ("sql", "transform", "query_sql") or name.endswith("_sql")):
        f["type"] = "sql"
    elif t == "list" and name == "statements":
        f["item"] = "sql"
    elif t == "text" and name in ("cron", "schedule_cron") or (t == "text" and "five fields" in f.get("description", "")):
        f["type"] = "cron"
    elif t in ("text",) and name in TEXTAREA:
        f["type"] = "textarea"
    elif t in ("integer", "number"):
        m = DURATION.search(name)
        if m and not name.endswith(("max_rows", "_rows")):
            f["type"] = "duration"
            f["unit"] = DURATION_UNIT.get(m.group(1), m.group(1))
            f["number"] = t
        elif name.endswith("_usd") or name.startswith("amount"):
            f.setdefault("unit", "USD")
        elif name.endswith("bytes"):
            f.setdefault("unit", "bytes")
        elif name.endswith("_chars"):
            f.setdefault("unit", "characters")
        elif name.endswith("_rows") or name == "rows":
            f.setdefault("unit", "rows")
        elif name.endswith("per_minute"):
            f.setdefault("unit", "per minute")
        elif name in ("objective",) or name.endswith("_percent") or name.endswith("_pct"):
            f.setdefault("unit", "%")
    picker = None if f.pop("no_picker", False) else PICKERS.get(name)
    if f.get("picker") and f["type"] in ("text", "list") and f["picker"] != "ref":     # a hint named the picker
        if f["type"] == "list":
            f["multiple"] = True
        f["type"] = "picker"
    elif picker and "picker" not in f and f["type"] in ("text", "list", "select"):
        if picker == "table" and f["type"] == "text" and name in ("source", "target") and \
                not TABLE_HINT.search(f.get("description", "")):
            picker = ""
        if picker:
            f["picker"] = picker
            if f["type"] == "list":
                f["multiple"] = True
            f["type"] = "picker"
            f.pop("enum", None)
    elif f["type"] == "text" and name in ("target", "source") and TABLE_HINT.search(f.get("description", "")):
        f["type"], f["picker"] = "picker", "table"
    elif f["type"] == "list" and name in ("tables",):
        f["type"], f["picker"], f["multiple"] = "picker", "table", True
    if f.get("picker") == "ref" and f["type"] != "picker":
        f["type"] = "picker"
    for sub in f.get("fields", []):
        _widget(sub, False)
    return f


# A duration's label without its unit (the unit is beside the number): every_minutes -> "Every".
DURATION_LABELS = {"every": "Every", "idle": "Stop when idle for", "due": "Due in", "expire": "Expire after",
                   "orphan": "Remove orphans after", "ttl": "Time to live", "timeout": "Time limit", "window": "Window",
                   "duration": "Duration", "interval": "Interval", "batch": "Batch every", "slow_call": "Slow call after",
                   "max_age": "Maximum age", "latency_budget": "Latency budget"}


def _clean(f: dict) -> dict:
    """Stable key order and a label."""
    f["label"] = humanize(f["name"])
    if f["type"] == "duration":
        stem = DURATION.sub("", f["name"])
        f["label"] = DURATION_LABELS.get(stem, humanize(stem) if stem else f["label"])
    order = ["name", "label", "type"]
    return {**{k: f[k] for k in order if k in f}, **{k: f[k] for k in sorted(f) if k not in order}}


def _finish(fields: list[dict]) -> list[dict]:
    out = []
    for f in fields:
        if f.get("fields"):
            f["fields"] = _finish(f["fields"])
        out.append(_clean(f))
    return out


# ---- one action ---------------------------------------------------------------------------------------------------
def _kind_match(method: str, path: str, preferred: str = "") -> Optional[tuple[registry.Kind, str]]:
    want = (method, _norm(path))
    ks = registry.kinds()
    ordered = sorted(ks, key=lambda k: k.name != preferred)
    for k in ordered:
        for which in ("create", "update"):
            o = getattr(k, which)
            if o is not None and (o.method, _norm(o.path)) == want:
                return k, which
    return None


def _placeholders(path: str) -> list[str]:
    return PLACE.findall(path.split("?")[0])


def build_form(spec: dict, action: dict, doc: dict, seen: Optional[dict] = None) -> dict:
    method, path = action["method"], action["path"]
    example = action.get("body") if isinstance(action.get("body"), dict) else {}
    row_action = bool(_placeholders(path)) and action.get("scope") != "page"
    form: dict[str, Any] = {}
    fields: list[dict] = []
    path_fields: dict[str, str] = {}      # field -> the catalog placeholder it fills
    body_out: set[str] = set()            # fields that are not sent in the body
    hit = _kind_match(method, path, (spec.get("iac") or {}).get("kind", ""))
    schema = request_schema(doc, method, path)
    if hit:
        k, which = hit
        o = getattr(k, which)
        kind_places, ours = _placeholders(o.path), _placeholders(path)
        mapping = dict(zip(kind_places, ours)) if len(kind_places) == len(ours) else {}
        for kp, cp in mapping.items():
            path_fields[k.id if kp == "id" else kp] = cp
        same = k.create is not None and k.update is not None and \
            (k.create.method, _norm(k.create.path)) == (k.update.method, _norm(k.update.path))
        form["source"] = f"registry:{k.name}"
        form["mode"] = "upsert" if same else ("update" if which == "update" else "create")
        if not row_action and form["mode"] == "update":
            form["mode"] = "create"
        for field in k.fields:
            in_path = field.name in path_fields
            if o.only and field.name not in o.only and not in_path:
                continue
            if field.name in o.omit and not in_path:
                continue
            fields.append(_kind_field(k, field))
            if (o.only and field.name not in o.only) or field.name in o.omit:
                body_out.add(field.name)
        if form["mode"] in ("update", "upsert") and "{" not in k.read.replace("{id}", ""):
            form["prefill"] = {"read": k.read, "list_key": "" if k.list_key == "." else k.list_key, "id": k.id,
                               "singleton": k.singleton}
        props = {}
        if schema and schema.get("properties"):
            req = set(schema.get("required", []))
            props = {n: _openapi_field(n, s, doc, n in req) for n, s in schema["properties"].items()}
        fields = [_merge(f, props[f["name"]]) if f["name"] in props else f for f in fields]
        examples = {**(seen or {}).get(k.name, {}), **(k.example or {})}
        for i, f in enumerate(fields):
            if f["type"] in ("json", "list") and f["name"] in examples:
                fields[i] = _merge(f, _example_field(f["name"], examples[f["name"]]))
    elif schema and schema.get("properties"):
        req = set(schema.get("required", []))
        form["source"] = "openapi"
        fields = [_openapi_field(n, s, doc, n in req) for n, s in schema["properties"].items()]
    else:
        form["source"] = "example" if example else ("openapi" if route_exists(doc, method, path) else "example")
    if not hit:
        listing = (spec.get("path") or "").split("?")[0]
        if row_action and method in ("PUT", "PATCH") and _norm(path) == _norm(f"{listing}/{{}}"):
            form["mode"] = "update"
        elif not row_action and method == "POST" and path == listing:
            form["mode"] = "create"
        else:
            form["mode"] = "action"
    # What the example shows that the schema does not (a plain JSON object body), and nested structure for json.
    known = {f["name"]: i for i, f in enumerate(fields)}
    for name, value in example.items():
        guess = _example_field(name, value)
        if name in known:
            fields[known[name]] = _merge(fields[known[name]], guess)
        elif not hit or not (hit[0].create.only or (hit[0].update and hit[0].update.only)):
            if name in path_fields or name in _placeholders(path):
                continue
            fields.append(guess)
            known[name] = len(fields) - 1
    # Placeholders the person names (a policy's scope, a secret's key) are fields; a row action's first is the row's.
    for i, cp in enumerate(_placeholders(path)):
        if cp in path_fields.values() or (row_action and i == 0):
            continue
        if cp not in known:
            fields.insert(0, {"name": cp, "type": "text", "required": True})
            known = {f["name"]: i for i, f in enumerate(fields)}
        path_fields[cp] = cp
        if cp not in example:
            body_out.add(cp)
    hints = HINTS.get(f"{method} {path}", {})
    for f in fields:
        if f["name"] in path_fields:
            f["path"] = path_fields[f["name"]]
            if f["name"] in body_out:
                f["in_body"] = False
        h = hints.get(f["name"])
        if h:
            if h.get("type") in ("json", "keyvalue", "list"):
                f.pop("fields", None)
            f.update(copy.deepcopy(h))
            if "enum" in h and f["type"] not in ("select", "multiselect"):
                f["type"] = "select"
    path_secret = "/secrets/" in path
    fields = [_widget(f, path_secret) for f in fields]
    # Rarely used: optional fields the example does not show.
    shown = set(example) | set((hit[0].example or {}) if hit else ()) | set((seen or {}).get(hit[0].name if hit else "", ()))
    for f in fields:
        if not f.get("required") and f["name"] not in shown and not f.get("path") and len(fields) > 3:
            f["advanced"] = True
    form["fields"] = _finish(fields)
    return form


def build(catalog: Optional[dict] = None, doc: Optional[dict] = None) -> dict:
    catalog = catalog if catalog is not None else catalog_specs()
    doc = doc if doc is not None else openapi_document()
    forms: dict[str, dict] = {}
    seen: dict[str, dict] = {}     # a kind's fields that any of the catalog's examples shows, with a value
    for spec in catalog.values():
        for action in spec.get("actions") or []:
            hit = _kind_match(action["method"], action["path"], (spec.get("iac") or {}).get("kind", ""))
            if hit and isinstance(action.get("body"), dict):
                for name, value in action["body"].items():
                    seen.setdefault(hit[0].name, {}).setdefault(name, value)
    for key in sorted(catalog):
        spec = catalog[key]
        for action in spec.get("actions") or []:
            if not needs_form(action):
                continue
            fk = form_key(action)
            if fk in forms:
                continue
            forms[fk] = build_form(spec, action, doc, seen)
    return {"version": 1, "generated_by": "python -m cairn.iac.forms", "forms": forms}


def render(data: dict) -> str:
    """One form per line: small to bundle, and a change shows as the forms it touches."""
    head = json.dumps({k: v for k, v in data.items() if k != "forms"}, ensure_ascii=False)[:-1]
    lines = [f" {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
             for k, v in data["forms"].items()]
    return head + ', "forms": {\n' + ",\n".join(lines) + "\n}}\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cairn.iac.forms", description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="fail when the generated file is not current")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    text = render(build())
    if args.check:
        current = open(args.out, encoding="utf-8").read() if os.path.exists(args.out) else ""
        if current != text:
            print(f"{os.path.relpath(args.out, ROOT)} is stale: run python -m cairn.iac.forms", file=sys.stderr)
            return 1
        print(f"{os.path.relpath(args.out, ROOT)} is current")
        return 0
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {os.path.relpath(args.out, ROOT)} ({len(json.loads(text)['forms'])} forms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
