"""The flow definition: one document (``kind: flow``, YAML or JSON) that is the single source of truth.

```yaml
kind: flow
name: nightly-sales                      # lowercase letters, digits, - and _
description: Refresh, check, report
runner: cairn                            # cairn (default), or airflow: Cairn deploys the flow as a DAG and Airflow runs it
params:
  - {name: date, type: date, default: "{{ run.logical_date }}"}
triggers:
  - {type: schedule, cron: "0 2 * * *", timezone: Europe/Berlin}
  - {type: dataset, tables: [sales.orders]}
  - {type: flow, flow: ingest-orders, on: [succeeded]}
concurrency: {policy: queue, max_active: 1}   # skip | queue | allow
sla_minutes: 60
notify: {on: [failure], to: [urn:li:corpuser:ana]}
steps:
  - {name: refresh, type: derived, table: revenue}
  - {name: enough, type: check, after: [refresh], sql: "SELECT count(*) AS n FROM marts.revenue",
     connection: lakehouse, measure: n, op: ">", value: 0}
  - {name: report, type: sql, after: [enough], sql: "SELECT * FROM marts.revenue", connection: lakehouse}
layout: {refresh: {x: 0, y: 0}, enough: {x: 260, y: 0}, report: {x: 520, y: 0}}
```

:func:`normalize` checks a definition's structure on its own (step types, fields, references, cycles,
expressions) and returns it with every default filled in, or raises :class:`FlowError` with each problem's
path. What depends on other resources (a derived table, an agent app, a job) is checked when the flow is
published (:mod:`cairn.flows.engine`). :func:`to_yaml` writes it back without the defaults, so a definition
round-trips between the canvas, the code view and git.
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Optional

from cairn.flows import expr

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
STEP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
PARAM_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
URN_USER = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s]{1,250}$")
OPS = (">", ">=", "<", "<=", "==", "!=")
PARAM_TYPES = ("string", "int", "float", "bool", "date", "list")
WHEN = ("all_ok", "none_failed", "all_done", "any_failed")
POLICIES = ("skip", "queue", "allow")
TRIGGERS = ("schedule", "dataset", "flow", "manual", "api")
FLOW_EVENTS = ("succeeded", "failed", "finished")
NOTIFY_ON = ("failure", "success", "sla")
GROUPS = ("Data", "Compute", "Platforms", "AI", "Control", "Notify")
MAX_DEF_BYTES = 512 * 1024


def max_steps() -> int:
    return max(1, min(int(os.environ.get("CAIRN_FLOWS_MAX_STEPS", "200")), 1000))


# What each step type takes. A field is (kind, default, required); kinds: text (a string that may hold
# {{ }} templates), str, int (lo, hi), num, bool, list, dict, json (any JSON value; templates in its strings), enum
# (values), names (step names), people (user or group urns), expr, items (an expression or a list), child (a for-each's
# step).
F = tuple
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
HTTP_AUTH = ("none", "bearer", "basic", "header")
PLATFORM_TYPES = ("http", "databricks", "snowflake", "bigquery")
STEP_TYPES: dict[str, dict[str, Any]] = {
    "sql": {"group": "Data", "label": "SQL query", "timeout": 3600, "fields": {
        "sql": F(("text", None, False)), "query": F(("str", None, False)), "connection": F(("str", None, False)),
        "limit": F(("int", 1000, False, 1, 10_000))}},
    "derived": {"group": "Data", "label": "Refresh a derived table", "timeout": 6 * 3600, "fields": {
        "table": F(("str", None, True)), "full": F(("bool", False, False))}},
    "check": {"group": "Data", "label": "Quality check", "timeout": 3600, "fields": {
        "sql": F(("text", None, False)), "query": F(("str", None, False)), "connection": F(("str", None, False)),
        "measure": F(("str", "rows", False)), "op": F(("enum", None, True, OPS)), "value": F(("num", None, True))}},
    "pipe": {"group": "Data", "label": "Load files (pipe)", "timeout": 3600, "fields": {
        "pipe": F(("str", None, True))}},
    "stream": {"group": "Data", "label": "Wait for a stream", "timeout": 3600, "fields": {
        "stream": F(("str", None, True)), "poll_s": F(("int", 15, False, 5, 600))}},
    "notebook": {"group": "Compute", "label": "Notebook", "timeout": 3600, "fields": {
        "job": F(("str", None, False)), "notebook": F(("dict", None, False)), "params": F(("dict", {}, False))}},
    "python": {"group": "Compute", "label": "Python", "timeout": 3600, "fields": {
        "job": F(("str", None, False)), "code": F(("text", None, False)), "params": F(("dict", {}, False))}},
    "spark": {"group": "Compute", "label": "Spark job", "timeout": 4 * 3600, "fields": {
        "application": F(("str", None, True)), "class_name": F(("str", None, False)), "args": F(("list", [], False)),
        "conf": F(("dict", {}, False))}},
    "dbt": {"group": "Compute", "label": "dbt", "timeout": 4 * 3600, "fields": {
        "command": F(("enum", "build", False, ("run", "build", "test", "seed", "snapshot"))),
        "select": F(("text", "", False)), "project_dir": F(("str", "/dbt", False))}},
    # deployment: a managed Airflow deployment (cairn.airflow_deploy), woken if it hibernates; else CAIRN_AIRFLOW_URL.
    "airflow": {"group": "Compute", "label": "Airflow DAG", "timeout": 6 * 3600, "fields": {
        "dag_id": F(("str", None, True)), "conf": F(("dict", {}, False)), "wait": F(("bool", True, False)),
        "poll_s": F(("int", 30, False, 5, 600)), "deployment": F(("str", None, False))}},
    # Other platforms (cairn.flows.platforms): credentials only from a secret scope, read as the owner when the step
    # runs; TLS always verified; egress rules apply. airflow_conn_id names the Airflow connection an exported DAG uses.
    "http": {"group": "Platforms", "label": "HTTP request", "timeout": 3600, "fields": {
        "method": F(("enum", "GET", False, HTTP_METHODS)), "url": F(("text", None, True)),
        "headers": F(("dict", {}, False)), "body": F(("json", None, False)),
        "auth": F(("enum", "none", False, HTTP_AUTH)), "secret_scope": F(("str", None, False)),
        "secret_key": F(("str", None, False)), "username": F(("text", None, False)),
        "auth_header": F(("str", None, False)), "ca_key": F(("str", None, False)),
        "ok_status": F(("list", [], False)), "output_path": F(("str", None, False)),
        "poll_url": F(("text", None, False)), "status_path": F(("str", None, False)),
        "success_values": F(("list", [], False)), "failure_values": F(("list", [], False)),
        "poll_s": F(("int", 15, False, 1, 3600)), "airflow_conn_id": F(("str", None, False))}},
    "databricks": {"group": "Platforms", "label": "Databricks job", "timeout": 6 * 3600, "fields": {
        "host": F(("text", None, True)), "secret_scope": F(("str", None, True)),
        "auth": F(("enum", "pat", False, ("pat", "oauth"))), "token_key": F(("str", "token", False)),
        "client_id_key": F(("str", "client_id", False)), "client_secret_key": F(("str", "client_secret", False)),
        "job_id": F(("text", None, False)), "job_parameters": F(("dict", {}, False)), "run": F(("dict", None, False)),
        "poll_s": F(("int", 30, False, 1, 3600)), "airflow_conn_id": F(("str", None, False))}},
    "snowflake": {"group": "Platforms", "label": "Snowflake SQL", "timeout": 3600, "fields": {
        "account": F(("text", None, True)), "user": F(("text", None, True)), "secret_scope": F(("str", None, True)),
        "private_key_key": F(("str", "private_key", False)), "passphrase_key": F(("str", None, False)),
        "statement": F(("text", None, False)), "task": F(("text", None, False)),
        "multi_statement": F(("bool", False, False)), "warehouse": F(("text", "", False)),
        "database": F(("text", "", False)), "schema": F(("text", "", False)), "role": F(("text", "", False)),
        "host": F(("text", None, False)), "poll_s": F(("int", 10, False, 1, 3600)),
        "airflow_conn_id": F(("str", None, False))}},
    "bigquery": {"group": "Platforms", "label": "BigQuery query", "timeout": 3600, "fields": {
        "project": F(("text", None, True)), "query": F(("text", None, True)), "location": F(("text", "", False)),
        "secret_scope": F(("str", None, True)), "key_key": F(("str", "service_account_json", False)),
        "destination_table": F(("text", "", False)),
        "write_disposition": F(("enum", "WRITE_EMPTY", False, ("WRITE_EMPTY", "WRITE_TRUNCATE", "WRITE_APPEND"))),
        "max_bytes_billed": F(("int", None, False, 1, 10 ** 15)), "labels": F(("dict", {}, False)),
        "api_url": F(("text", "https://bigquery.googleapis.com", False)), "poll_s": F(("int", 10, False, 1, 3600)),
        "airflow_conn_id": F(("str", None, False))}},
    "agent": {"group": "AI", "label": "Agent", "timeout": 900, "fields": {
        "app": F(("str", None, True)), "input": F(("text", None, True))}},
    "approval": {"group": "Control", "label": "Approval", "timeout": 3 * 86400, "fields": {
        "approvers": F(("people", [], False)), "groups": F(("list", [], False)), "message": F(("text", "", False))}},
    "wait": {"group": "Control", "label": "Wait", "timeout": 7 * 86400, "fields": {
        "seconds": F(("int", None, False, 1, 7 * 86400)), "until": F(("text", None, False))}},
    "branch": {"group": "Control", "label": "Branch", "timeout": 60, "fields": {
        "condition": F(("expr", None, True)), "then": F(("names", [], False)), "else": F(("names", [], False))}},
    "for_each": {"group": "Control", "label": "For each", "timeout": 6 * 3600, "fields": {
        "items": F(("items", None, True)), "concurrency": F(("int", 4, False, 1, 32)), "step": F(("child", None, True))}},
    "notify": {"group": "Notify", "label": "Notify", "timeout": 60, "fields": {
        "to": F(("people", [], False)), "title": F(("text", "", False)), "message": F(("text", None, True))}},
}
# Steps a for-each may repeat (not ones that wait for people or shape the graph).
CHILD_TYPES = ("sql", "derived", "check", "pipe", "notebook", "python", "spark", "dbt", "airflow", *PLATFORM_TYPES, "agent",
               "notify")
COMMON = {"name", "type", "after", "when", "retries", "retry_delay_s", "retry_backoff", "timeout_s", "description",
          "reads", "writes"}
TOP = ("kind", "name", "description", "runner", "params", "triggers", "concurrency", "sla_minutes", "notify", "steps",
       "layout")
RUNNERS = ("cairn", "airflow")


class FlowError(Exception):
    def __init__(self, message: str, status: int = 400, errors: Optional[list[dict]] = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or [{"path": "", "message": message}]


class _Errors:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, path: str, message: str, step: Optional[str] = None) -> None:
        self.items.append({"path": path, "message": message, **({"step": step} if step else {})})

    def raise_if_any(self) -> None:
        if self.items:
            first = self.items[0]
            more = f" (and {len(self.items) - 1} more)" if len(self.items) > 1 else ""
            raise FlowError(f"{first['path'] or 'flow'}: {first['message']}{more}", 400, self.items)


def _field(errs: _Errors, path: str, step: Optional[str], name: str, spec: tuple, raw: dict) -> Any:
    kind, default, required = spec[0], spec[1], spec[2]
    v = raw.get(name)
    if v is None or (v == "" and kind not in ("text",) and default is None):
        if required:
            errs.add(f"{path}.{name}", "is required", step)
        return copy.deepcopy(default)
    p = f"{path}.{name}"
    if kind in ("text", "str"):
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            errs.add(p, "is text", step)
            return default
        v = str(v)
        if len(v) > 200_000:
            errs.add(p, "is at most 200,000 characters", step)
        for problem in expr.check_template(v) if kind == "text" else ():
            errs.add(p, problem, step)
        return v
    if kind == "int":
        lo, hi = spec[3], spec[4]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            errs.add(p, f"is a whole number from {lo} to {hi}", step)
            return default
        return v
    if kind == "num":
        if isinstance(v, bool):
            errs.add(p, "is a number", step)
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            errs.add(p, "is a number", step)
            return default
    if kind == "bool":
        if not isinstance(v, bool):
            errs.add(p, "is true or false", step)
            return default
        return v
    if kind == "enum":
        if v not in spec[3]:
            errs.add(p, f"is one of {', '.join(spec[3])}", step)
            return default
        return v
    if kind == "list":
        if not isinstance(v, list) or len(v) > 500:
            errs.add(p, "is a list (up to 500)", step)
            return default
        for problem in (pr for x in v for pr in expr.check_template(x)):
            errs.add(p, problem, step)
        return v
    if kind == "dict":
        if not isinstance(v, dict):
            errs.add(p, "is a mapping", step)
            return default
        return v
    if kind == "json":
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            errs.add(p, "is a JSON value", step)
            return default
        for problem in _templates_in(v):
            errs.add(p, problem, step)
        return v
    if kind == "names":
        v = [v] if isinstance(v, str) else v
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.add(p, "is a list of step names", step)
            return default
        return list(dict.fromkeys(v))
    if kind == "people":
        v = [v] if isinstance(v, str) else v
        if not isinstance(v, list) or not all(isinstance(x, str) and URN_USER.match(x) for x in v) or len(v) > 50:
            errs.add(p, "is a list of up to 50 people or groups (urn:li:corpuser:… or urn:li:corpGroup:…)", step)
            return default
        return list(dict.fromkeys(v))
    if kind == "expr":
        try:
            expr.parse(str(v))
        except expr.ExprError as e:
            errs.add(p, str(e), step)
        return str(v)
    if kind == "items":
        if isinstance(v, list):
            if len(v) > 1000:
                errs.add(p, "is at most 1000 items", step)
            return v
        if isinstance(v, str):
            inner = expr.TEMPLATE.fullmatch(v.strip())
            try:
                expr.parse(inner.group(1) if inner else v)
            except expr.ExprError as e:
                errs.add(p, str(e), step)
            return v
        errs.add(p, "is a list or an expression that gives one", step)
        return default
    return v  # child: checked by the caller


def _templates_in(v: Any) -> list[str]:
    if isinstance(v, str):
        return expr.check_template(v)
    if isinstance(v, list):
        return [p for x in v for p in _templates_in(x)]
    if isinstance(v, dict):
        return [p for x in v.values() for p in _templates_in(x)]
    return []


ID = re.compile(r"^[A-Za-z0-9_.-]{1,250}$")
SECRET_NAMES = ("secret_scope", "secret_key", "token_key", "client_id_key", "client_secret_key", "private_key_key",
                "passphrase_key", "key_key", "ca_key", "airflow_conn_id")


def _platform(errs: _Errors, path: str, step_name: Optional[str], kind: str, out: dict) -> None:
    """What a platform step's fields need together; no credentials in the definition."""
    from cairn.flows import platforms

    def bad(field: str, message: str) -> None:
        errs.add(f"{path}.{field}" if field else path, message, step_name)
    for f in SECRET_NAMES:
        v = out.get(f)
        if v and not ID.match(str(v)):
            bad(f, "is a name: letters, digits, '.', '_' and '-'")
    if kind == "http":
        url = str(out.get("url") or "")
        templated = bool(expr.TEMPLATE.match(url))
        if not templated and not url.lower().startswith(("https://", "http://")):
            bad("url", "is an https:// URL")
        netloc = url.split("/", 3)[2] if url.count("/") >= 2 else ""
        if "@" in netloc:
            bad("url", "has credentials in it: use auth with a secret scope")
        for h in out.get("headers") or {}:
            if str(h).lower() in platforms.CREDENTIAL_HEADERS:
                bad("headers", f"{h} carries a credential: use auth (bearer, basic or header) with a secret scope")
        auth = out.get("auth") or "none"
        if auth != "none" and not (out.get("secret_scope") and out.get("secret_key")):
            bad("", f"auth {auth} needs secret_scope and secret_key (the key in the scope that holds the credential)")
        if auth == "basic" and not out.get("username"):
            bad("username", "is required for basic auth")
        if auth == "header" and not out.get("auth_header"):
            bad("auth_header", "is required for header auth (the header's name, such as X-Api-Key)")
        elif auth == "header" and not re.match(r"^[A-Za-z0-9-]{1,100}$", str(out["auth_header"])):
            bad("auth_header", "is a header name (letters, digits and -)")
        if out.get("ca_key") and not out.get("secret_scope"):
            bad("ca_key", "is a key in secret_scope (set secret_scope)")
        if out.get("method") == "GET" and out.get("body") is not None:
            bad("body", "a GET has no body: use POST, PUT or PATCH")
        for code in out.get("ok_status") or []:
            if isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599:
                bad("ok_status", "is a list of HTTP status codes (100-599)")
                break
        for f in ("output_path", "status_path"):
            if out.get(f):
                why = platforms.check_jsonpath(str(out[f]))
                if why:
                    bad(f, why)
        for ph in platforms.placeholders(str(out.get("poll_url") or "")):
            why = platforms.check_jsonpath(ph)
            if why:
                bad("poll_url", why)
        if out.get("status_path"):
            if not (out.get("success_values") or out.get("failure_values")):
                bad("success_values", "names the values of status_path that mean done (failure_values: those that "
                                      "mean it failed)")
        elif out.get("poll_url") or out.get("success_values") or out.get("failure_values"):
            bad("status_path", "is required to poll: the JSONPath of the status in the status URL's answer")
    if kind == "databricks":
        if bool(out.get("job_id")) == bool(out.get("run")):
            bad("", "needs either job_id (run an existing job) or run (a one-time run: the runs/submit body)")
        if out.get("run") is not None and not isinstance(out["run"].get("tasks"), list):
            bad("run", "is a runs/submit body with tasks (a list)")
        if out.get("job_parameters") and not out.get("job_id"):
            bad("job_parameters", "go with job_id")
    if kind == "snowflake":
        if bool(out.get("statement")) == bool(out.get("task")):
            bad("", "needs either statement (SQL) or task (a task to EXECUTE)")
        acct = str(out.get("account") or "")
        if acct and not expr.TEMPLATE.search(acct) and not platforms.ACCOUNT.match(acct):
            bad("account", "is a Snowflake account identifier (orgname-accountname, or a locator)")
        task = str(out.get("task") or "")
        if task and not expr.TEMPLATE.search(task) and not platforms.IDENT.match(task.strip()):
            bad("task", "is a task name (task, schema.task or database.schema.task)")
    if kind == "bigquery":
        dest = str(out.get("destination_table") or "")
        if dest and not expr.TEMPLATE.search(dest) and not platforms.BQ_TABLE.match(dest):
            bad("destination_table", "is project.dataset.table")


def _step_body(errs: _Errors, path: str, step_name: Optional[str], kind: str, raw: dict, child: bool = False) -> dict:
    spec = STEP_TYPES[kind]
    out: dict[str, Any] = {}
    allowed = set(spec["fields"]) | (COMMON - ({"name", "after", "when"} if child else set()))
    for extra in sorted(set(raw) - allowed):
        errs.add(f"{path}.{extra}", f"is not a field of a {kind} step (it takes {', '.join(sorted(spec['fields']))})",
                 step_name)
    for fname, fspec in spec["fields"].items():
        if fspec[0] == "child":
            continue
        out[fname] = _field(errs, path, step_name, fname, fspec, raw)
    if kind in ("sql", "check"):
        if not out.get("query") and not (out.get("sql") and out.get("connection")):
            errs.add(path, "needs a saved query (query) or sql and a connection", step_name)
    if kind in ("notebook", "python"):
        src = "notebook" if kind == "notebook" else "code"
        if bool(out.get("job")) == bool(out.get(src)):
            errs.add(path, f"needs either job (a job's name) or {src}", step_name)
        if kind == "notebook" and out.get("notebook") is not None and not isinstance(out["notebook"].get("cells"), list):
            errs.add(f"{path}.notebook", "is a notebook (nbformat 4, with cells)", step_name)
    if kind == "wait" and (out.get("seconds") is None) == (out.get("until") is None):
        errs.add(path, "needs either seconds or until (HH:MM, or a date and time)", step_name)
    if kind == "approval" and not out["approvers"] and not out["groups"]:
        errs.add(path, "needs approvers (people) or groups who may approve", step_name)
    if kind in PLATFORM_TYPES:
        _platform(errs, path, step_name, kind, out)
    if kind == "for_each":
        c = raw.get("step")
        if not isinstance(c, dict) or c.get("type") not in CHILD_TYPES:
            errs.add(f"{path}.step", f"is the step to repeat: a mapping with a type, one of {', '.join(CHILD_TYPES)}",
                     step_name)
            out["step"] = None
        else:
            body = _step_body(errs, f"{path}.step", step_name, c["type"], {k: v for k, v in c.items() if k != "type"},
                              child=True)
            out["step"] = {"type": c["type"], **body}
    # Common settings.
    if not child:
        pass
    retries = raw.get("retries", 0)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
        errs.add(f"{path}.retries", "is 0 to 10", step_name)
        retries = 0
    delay = raw.get("retry_delay_s", 30)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 3600:
        errs.add(f"{path}.retry_delay_s", "is 0 to 3600 seconds", step_name)
        delay = 30
    backoff = raw.get("retry_backoff", 2)
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or not 1 <= backoff <= 4:
        errs.add(f"{path}.retry_backoff", "is 1 to 4 (each retry waits that many times longer)", step_name)
        backoff = 2
    timeout = raw.get("timeout_s", spec["timeout"])
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 7 * 86400:
        errs.add(f"{path}.timeout_s", "is 1 to 604800 seconds", step_name)
        timeout = spec["timeout"]
    desc = raw.get("description") or ""
    if not isinstance(desc, str) or len(desc) > 2000:
        errs.add(f"{path}.description", "is text up to 2000 characters", step_name)
        desc = ""
    tables = {}
    for key in ("reads", "writes"):
        v = raw.get(key) or []
        v = [v] if isinstance(v, str) else v
        if not isinstance(v, list) or not all(isinstance(t, str) and 0 < len(t) <= 512 for t in v) or len(v) > 100:
            errs.add(f"{path}.{key}", "is a list of tables (schema.table, or a dataset urn)", step_name)
            v = []
        tables[key] = list(dict.fromkeys(v))
    out.update(retries=retries, retry_delay_s=delay, retry_backoff=float(backoff), timeout_s=timeout,
               description=desc, **tables)
    return out


def _params(errs: _Errors, raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 50:
        errs.add("params", "is a list of up to 50 parameters")
        return []
    out, seen = [], set()
    for i, p in enumerate(raw):
        path = f"params[{i}]"
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not PARAM_NAME.match(p["name"]):
            errs.add(path, "is {name, type, default}; a name of letters, digits and _")
            continue
        if p["name"] in seen:
            errs.add(path, f"{p['name']} is declared twice")
            continue
        seen.add(p["name"])
        extra = set(p) - {"name", "type", "default", "required", "description"}
        if extra:
            errs.add(path, f"unknown fields {', '.join(sorted(extra))}")
        t = p.get("type", "string")
        if t not in PARAM_TYPES:
            errs.add(f"{path}.type", f"is one of {', '.join(PARAM_TYPES)}")
            t = "string"
        one = {"name": p["name"], "type": t, "default": p.get("default"), "required": bool(p.get("required", False)),
               "description": str(p.get("description") or "")}
        if one["default"] is not None:
            for problem in expr.check_template(one["default"]):
                errs.add(f"{path}.default", problem)
            if not (isinstance(one["default"], str) and expr.TEMPLATE.search(one["default"])):
                try:
                    one["default"] = coerce(t, one["default"])
                except ValueError as e:
                    errs.add(f"{path}.default", str(e))
        out.append(one)
    return out


def coerce(t: str, v: Any) -> Any:
    """A parameter's value as its type, or ValueError."""
    from datetime import date

    if v is None:
        return None
    if t == "string":
        if isinstance(v, (dict, list)):
            raise ValueError("is text")
        return str(v)
    if t == "int":
        if isinstance(v, bool):
            raise ValueError("is a whole number")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("is a whole number")
        if f != int(f):
            raise ValueError("is a whole number")
        return int(f)
    if t == "float":
        if isinstance(v, bool):
            raise ValueError("is a number")
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError("is a number")
    if t == "bool":
        if isinstance(v, bool):
            return v
        if str(v).lower() in ("true", "1", "yes"):
            return True
        if str(v).lower() in ("false", "0", "no"):
            return False
        raise ValueError("is true or false")
    if t == "date":
        if isinstance(v, date):
            return v.isoformat()
        try:
            return date.fromisoformat(str(v)).isoformat()
        except ValueError:
            raise ValueError("is a date (YYYY-MM-DD)")
    if t == "list":
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                out = json.loads(v)
            except ValueError:
                out = [x.strip() for x in v.split(",") if x.strip()]
            if isinstance(out, list):
                return out
        raise ValueError("is a list")
    raise ValueError(f"unknown type {t}")


def _triggers(errs: _Errors, raw: Any, flow_name: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        errs.add("triggers", "is a list of up to 20 triggers")
        return []
    out = []
    for i, t in enumerate(raw):
        path = f"triggers[{i}]"
        if not isinstance(t, dict) or t.get("type") not in TRIGGERS:
            errs.add(path, f"is a trigger with a type: {', '.join(TRIGGERS)}")
            continue
        kind = t["type"]
        allowed = {"type", "enabled"} | {"schedule": {"cron", "timezone"}, "dataset": {"tables"},
                                         "flow": {"flow", "on"}}.get(kind, set())
        extra = set(t) - allowed
        if extra:
            errs.add(path, f"unknown fields {', '.join(sorted(extra))} for a {kind} trigger")
        one: dict[str, Any] = {"type": kind, "enabled": bool(t.get("enabled", True))}
        if kind == "schedule":
            from croniter import croniter

            cron = str(t.get("cron") or "").strip()
            if not cron or not croniter.is_valid(cron) or len(cron.split()) != 5:
                errs.add(f"{path}.cron", "is a five-field cron expression, such as 0 2 * * *")
            tz = str(t.get("timezone") or "UTC")
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(tz)
            except Exception:  # noqa: BLE001 - any failure is an unknown zone
                errs.add(f"{path}.timezone", f"{tz!r} is not a time zone (such as UTC or Europe/Berlin)")
            one.update(cron=cron, timezone=tz)
        elif kind == "dataset":
            tables = t.get("tables")
            tables = [tables] if isinstance(tables, str) else tables
            if not isinstance(tables, list) or not 1 <= len(tables) <= 50 or \
                    not all(isinstance(x, str) and 0 < len(x) <= 512 for x in tables):
                errs.add(f"{path}.tables", "is a list of 1 to 50 tables (schema.table, or a dataset urn)")
                tables = []
            one["tables"] = list(dict.fromkeys(tables))
        elif kind == "flow":
            other = str(t.get("flow") or "")
            if not NAME.match(other):
                errs.add(f"{path}.flow", "is the name of another flow")
            elif other == flow_name:
                errs.add(f"{path}.flow", "a flow cannot start after itself")
            on = t.get("on") or ["succeeded"]
            on = [on] if isinstance(on, str) else on
            if not isinstance(on, list) or not on or not all(x in FLOW_EVENTS for x in on):
                errs.add(f"{path}.on", f"is a list of {', '.join(FLOW_EVENTS)}")
                on = ["succeeded"]
            one.update(flow=other, on=list(dict.fromkeys(on)))
        out.append(one)
    return out


def _ancestors(steps: dict[str, dict]) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def anc(n: str, stack: tuple = ()) -> set[str]:
        if n in memo:
            return memo[n]
        out: set[str] = set()
        for a in steps[n]["after"]:
            if a in steps and a not in stack:
                out |= {a} | anc(a, stack + (n,))
        memo[n] = out
        return out
    return {n: anc(n) for n in steps}


def find_cycle(steps: list[dict]) -> Optional[list[str]]:
    by = {s["name"]: s for s in steps}
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(n: str) -> Optional[list[str]]:
        state[n] = 1
        path.append(n)
        for a in by[n]["after"]:
            if a not in by:
                continue
            if state.get(a) == 1:
                return path[path.index(a):] + [a]
            if state.get(a) is None:
                found = visit(a)
                if found:
                    return found
        state[n] = 2
        path.pop()
        return None
    for s in steps:
        if state.get(s["name"]) is None:
            found = visit(s["name"])
            if found:
                return found[::-1]
    return None


def order(steps: list[dict]) -> list[dict]:
    """The steps, each after what it depends on (stable: definition order among peers)."""
    by = {s["name"]: s for s in steps}
    done: list[dict] = []
    seen: set[str] = set()

    def visit(n: str) -> None:
        if n in seen:
            return
        seen.add(n)
        for a in by[n]["after"]:
            if a in by:
                visit(a)
        done.append(by[n])
    for s in steps:
        visit(s["name"])
    return done


def normalize(raw: Any) -> dict:
    """The definition with its defaults, or FlowError listing every problem (path, message, step)."""
    if isinstance(raw, str):
        raw = from_text(raw)
    if not isinstance(raw, dict):
        raise FlowError("a flow is a mapping (kind: flow, name, steps)")
    if len(json.dumps(raw, default=str)) > MAX_DEF_BYTES:
        raise FlowError(f"a flow definition is at most {MAX_DEF_BYTES // 1024} KB")
    errs = _Errors()
    if raw.get("kind", "flow") != "flow":
        errs.add("kind", "is flow")
    for extra in sorted(set(raw) - set(TOP)):
        errs.add(extra, f"is not a flow field (the fields are {', '.join(TOP)})")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        errs.add("name", "is lowercase letters, digits, - and _ (starting with a letter, up to 63)")
        name = ""
    desc = raw.get("description") or ""
    if not isinstance(desc, str) or len(desc) > 4000:
        errs.add("description", "is text up to 4000 characters")
        desc = ""
    runner = raw.get("runner", "cairn")
    if runner not in RUNNERS:
        errs.add("runner", f"is one of {', '.join(RUNNERS)} (who runs the steps' order: Cairn, or Airflow from a DAG "
                 "Cairn deploys)")
        runner = "cairn"
    out: dict[str, Any] = {"kind": "flow", "name": name, "description": desc, "runner": runner,
                           "params": _params(errs, raw.get("params")),
                           "triggers": _triggers(errs, raw.get("triggers"), name)}
    conc = raw.get("concurrency") or {}
    if isinstance(conc, str):
        conc = {"policy": conc}
    if not isinstance(conc, dict) or set(conc) - {"policy", "max_active"}:
        errs.add("concurrency", "is {policy: skip | queue | allow, max_active}")
        conc = {}
    policy = conc.get("policy", "queue")
    if policy not in POLICIES:
        errs.add("concurrency.policy", f"is one of {', '.join(POLICIES)}")
        policy = "queue"
    max_active = conc.get("max_active", 1 if policy != "allow" else 10)
    if isinstance(max_active, bool) or not isinstance(max_active, int) or not 1 <= max_active <= 50:
        errs.add("concurrency.max_active", "is 1 to 50")
        max_active = 1
    out["concurrency"] = {"policy": policy, "max_active": max_active}
    sla = raw.get("sla_minutes")
    if sla is not None and (isinstance(sla, bool) or not isinstance(sla, int) or not 1 <= sla <= 7 * 1440):
        errs.add("sla_minutes", "is 1 to 10080 minutes")
        sla = None
    out["sla_minutes"] = sla
    notify = raw.get("notify") or {}
    if not isinstance(notify, dict) or set(notify) - {"on", "to"}:
        errs.add("notify", "is {on: [failure, success, sla], to: [people]}")
        notify = {}
    on = notify.get("on", ["failure", "sla"])
    on = [on] if isinstance(on, str) else on
    if not isinstance(on, list) or not all(x in NOTIFY_ON for x in on):
        errs.add("notify.on", f"is a list of {', '.join(NOTIFY_ON)}")
        on = ["failure", "sla"]
    to = notify.get("to") or []
    if not isinstance(to, list) or not all(isinstance(x, str) and URN_USER.match(x) for x in to) or len(to) > 50:
        errs.add("notify.to", "is a list of up to 50 people or groups")
        to = []
    out["notify"] = {"on": list(dict.fromkeys(on)), "to": list(dict.fromkeys(to))}

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= max_steps():
        errs.add("steps", f"is a list of 1 to {max_steps()} steps")
        raw_steps = [] if not isinstance(raw_steps, list) else raw_steps[:max_steps()]
    steps: list[dict] = []
    names: set[str] = set()
    for i, s in enumerate(raw_steps):
        path = f"steps[{i}]"
        if not isinstance(s, dict):
            errs.add(path, "is a mapping with a name and a type")
            continue
        sname = s.get("name")
        if not isinstance(sname, str) or not STEP_NAME.match(sname):
            errs.add(f"{path}.name", "is letters, digits, - and _ (starting with a letter, up to 63)")
            continue
        if sname in names:
            errs.add(f"{path}.name", f"{sname} is the name of another step", sname)
            continue
        names.add(sname)
        kind = s.get("type")
        if kind not in STEP_TYPES:
            errs.add(f"{path}.type", f"unknown step type {kind!r} (the types are {', '.join(STEP_TYPES)})", sname)
            continue
        after = s.get("after") or []
        after = [after] if isinstance(after, str) else after
        if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
            errs.add(f"{path}.after", "is a list of step names", sname)
            after = []
        when = s.get("when", "all_ok")
        if when not in WHEN:
            errs.add(f"{path}.when", f"is one of {', '.join(WHEN)}", sname)
            when = "all_ok"
        body = _step_body(errs, path, sname, kind, {k: v for k, v in s.items() if k not in ("name", "type", "after", "when")})
        steps.append({"name": sname, "type": kind, "after": list(dict.fromkeys(after)), "when": when, **body})
    by = {s["name"]: s for s in steps}
    idx = {s["name"]: i for i, s in enumerate(raw_steps) if isinstance(s, dict) and isinstance(s.get("name"), str)}
    for s in steps:
        for a in s["after"]:
            if a == s["name"]:
                errs.add(f"steps[{idx[s['name']]}].after", "a step cannot come after itself", s["name"])
            elif a not in by:
                errs.add(f"steps[{idx[s['name']]}].after", f"no step {a!r} to come after", s["name"])
    cycle = find_cycle(steps)
    if cycle:
        errs.add("steps", f"steps depend on each other in a cycle: {' -> '.join(cycle)}", cycle[0])
    else:
        anc = _ancestors(by)
        for s in steps:
            p = f"steps[{idx[s['name']]}]"
            fields = {k: v for k, v in s.items() if k not in COMMON | {"then", "else"}}
            refs = expr.template_refs(fields)
            if s["type"] == "branch" and _parses(s["condition"]):
                refs |= expr.references(s["condition"])
            if s["type"] == "for_each" and isinstance(s["items"], str):
                inner = expr.TEMPLATE.fullmatch(s["items"].strip())
                if _parses(inner.group(1) if inner else s["items"]):
                    refs |= expr.references(inner.group(1) if inner else s["items"])
            for ref in sorted(refs):
                if ref not in by:
                    errs.add(p, f"reads steps.{ref}, but there is no step {ref!r}", s["name"])
                elif ref not in anc[s["name"]]:
                    errs.add(p, f"reads steps.{ref}, which does not come before it (add {ref} to its after)", s["name"])
            if s["type"] == "branch":
                for side in ("then", "else"):
                    for t in s[side]:
                        if t not in by:
                            errs.add(f"{p}.{side}", f"no step {t!r}", s["name"])
                        elif s["name"] not in by[t]["after"]:
                            errs.add(f"{p}.{side}", f"{t} is on the {side} side, so it comes after {s['name']} "
                                     f"(add {s['name']} to its after)", s["name"])
                both = set(s["then"]) & set(s["else"])
                if both:
                    errs.add(p, f"{', '.join(sorted(both))} is on both sides", s["name"])
                if not s["then"] and not s["else"]:
                    errs.add(p, "names the steps of at least one side (then or else)", s["name"])
    layout = raw.get("layout")
    if layout is not None:
        clean = {}
        if not isinstance(layout, dict):
            errs.add("layout", "is {step: {x, y}}")
        else:
            for k, v in layout.items():
                if k not in names:
                    continue                     # a position for a step since removed: dropped, not an error
                if not isinstance(v, dict) or not all(isinstance(v.get(c), (int, float)) and not isinstance(v.get(c), bool)
                                                      for c in ("x", "y")):
                    errs.add(f"layout.{k}", "is {x, y} numbers")
                    continue
                clean[k] = {c: (int(round(float(v[c]))) if float(v[c]).is_integer() else round(float(v[c]), 1)) for c in ("x", "y")}
        if clean:
            out["layout"] = clean
    errs.raise_if_any()
    out["steps"] = steps
    order_ = list(TOP)
    return {k: out[k] for k in order_ if k in out}


def _parses(text: str) -> bool:
    try:
        expr.parse(text)
        return True
    except expr.ExprError:
        return False


def validate(raw: Any) -> dict:
    """{"valid": bool, "errors": [...], "flow": normalized or None} (never raises on a bad definition)."""
    try:
        return {"valid": True, "errors": [], "flow": normalize(raw)}
    except FlowError as e:
        return {"valid": False, "errors": e.errors, "flow": None}


# ---- text ------------------------------------------------------------------------------------------
def from_text(text: str) -> Any:
    """A YAML or JSON document, or FlowError with the line."""
    import yaml

    if len(text) > MAX_DEF_BYTES * 2:
        raise FlowError("the document is too large")
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise FlowError(f"{where}{e.problem or e}", 400, [{"path": "", "message": f"{where}{e.problem or e}",
                                                           **({"line": mark.line + 1} if mark else {})}])
    except yaml.YAMLError as e:
        raise FlowError(f"not YAML: {e}")
    docs = [d for d in docs if d is not None]
    if len(docs) != 1:
        raise FlowError("one flow per document")
    return _on_keys(docs[0])


def _on_keys(v: Any) -> Any:
    """YAML 1.1 reads a key ``on:`` as true: in a flow it is always the word (notify.on, a flow trigger's on)."""
    if isinstance(v, dict):
        return {("on" if k is True else k): _on_keys(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_on_keys(x) for x in v]
    return v


def compact(defn: dict) -> dict:
    """The definition without the values that are defaults (what a person would write)."""
    out: dict[str, Any] = {"kind": "flow", "name": defn["name"]}
    if defn.get("description"):
        out["description"] = defn["description"]
    if defn.get("runner", "cairn") != "cairn":
        out["runner"] = defn["runner"]
    if defn.get("params"):
        out["params"] = [{k: v for k, v in p.items() if not (k == "required" and v is False)
                          and not (k == "description" and not v) and not (k == "default" and v is None)
                          and not (k == "type" and v == "string")} for p in defn["params"]]
    if defn.get("triggers"):
        out["triggers"] = [{k: v for k, v in t.items() if not (k == "enabled" and v is True)
                            and not (k == "timezone" and v == "UTC") and not (k == "on" and v == ["succeeded"])}
                           for t in defn["triggers"]]
    conc = defn.get("concurrency") or {}
    if conc and conc != {"policy": "queue", "max_active": 1}:
        out["concurrency"] = conc if not (conc["policy"] == "allow" and conc["max_active"] == 10) else {"policy": "allow"}
    if defn.get("sla_minutes"):
        out["sla_minutes"] = defn["sla_minutes"]
    n = defn.get("notify") or {}
    if n and (n.get("to") or n.get("on") != ["failure", "sla"]):
        out["notify"] = {k: v for k, v in n.items() if v or k == "on"}
    out["steps"] = [compact_step(s) for s in defn["steps"]]
    if defn.get("layout"):
        out["layout"] = defn["layout"]
    return out


def compact_step(s: dict, child: bool = False) -> dict:
    spec = STEP_TYPES[s["type"]]
    out: dict[str, Any] = {} if child else {"name": s["name"]}
    out["type"] = s["type"]
    if not child and s.get("after"):
        out["after"] = s["after"]
    if not child and s.get("when", "all_ok") != "all_ok":
        out["when"] = s["when"]
    for f, fs in spec["fields"].items():
        v = s.get(f)
        if f == "step" and v:
            out[f] = compact_step(v, child=True)
        elif v is not None and v != fs[1] and not (v == "" and fs[1] in (None, "")):
            out[f] = v
    defaults = {"retries": 0, "retry_delay_s": 30, "retry_backoff": 2.0, "timeout_s": spec["timeout"], "description": "",
                "reads": [], "writes": []}
    for k, d in defaults.items():
        if k in s and s[k] != d:
            out[k] = s[k]
    return out


def to_yaml(defn: dict, full: bool = False) -> str:
    """YAML text: compact (defaults left out) unless ``full``; multi-line text as literal blocks."""
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    def text(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)
    def seq(dumper, data):
        short = len(data) <= 12 and all(isinstance(x, (str, int, float, bool)) and len(str(x)) <= 60 and "\n" not in str(x)
                                        for x in data)
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=short)

    def mapping(dumper, data):
        small = len(data) <= 3 and all(k in ("x", "y") for k in data)        # a step's position: {x: 0, y: 120}
        return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=small)
    Dumper.add_representer(str, text)
    Dumper.add_representer(list, seq)
    Dumper.add_representer(dict, mapping)
    return yaml.dump(defn if full else compact(defn), Dumper=Dumper, sort_keys=False, allow_unicode=True, width=120)


# ---- comparing versions -------------------------------------------------------------------------------
def diff(old: Optional[dict], new: dict) -> dict:
    """What changed from ``old`` to ``new``: steps added, removed and changed (with their fields), and the
    other settings that changed. Positions on the canvas are not changes."""
    old = old or {"steps": []}
    a = {s["name"]: s for s in old.get("steps") or []}
    b = {s["name"]: s for s in new.get("steps") or []}
    changed = []
    for n in b:
        if n in a and a[n] != b[n]:
            fields = sorted(k for k in set(a[n]) | set(b[n]) if a[n].get(k) != b[n].get(k))
            changed.append({"name": n, "fields": fields, "before": a[n], "after": b[n]})
    settings = sorted(k for k in set(old) | set(new) if k not in ("steps", "layout") and old.get(k) != new.get(k))
    return {"added": [n for n in b if n not in a], "removed": [n for n in a if n not in b], "changed": changed,
            "settings": settings}


def catalog() -> list[dict]:
    """The step types, for editors: group, label, fields with their defaults and whether they are required."""
    out = []
    for kind, spec in STEP_TYPES.items():
        out.append({"type": kind, "group": spec["group"], "label": spec["label"], "timeout_s": spec["timeout"],
                    "fields": [{"name": f, "kind": fs[0], "default": fs[1], "required": fs[2],
                                **({"values": list(fs[3])} if fs[0] == "enum" else {}),
                                **({"min": fs[3], "max": fs[4]} if fs[0] == "int" else {})}
                               for f, fs in spec["fields"].items()]})
    return out
