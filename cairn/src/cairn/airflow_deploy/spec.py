"""What a managed Airflow deployment may be asked to be: its environment, Airflow version, executor, resources, DAG
source, environment variables, secret references, hibernation and role mapping. Everything a caller sends is checked
here before any runtime sees it; a value that looks like a credential is refused (secrets are references to Cairn
secret scopes, resolved when the deployment starts)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any, Optional


class SpecError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


NAME = re.compile(r"^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$")         # cairn-af-<name>-<component> stays a DNS label
ENV_NAME = re.compile(r"^[a-z]([a-z0-9-]{0,14}[a-z0-9])?$")
VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
CONN = re.compile(r"^[a-z0-9_]{1,128}$")
STANDARD_ENVIRONMENTS = ("dev", "stage", "prod")
EXECUTORS = ("KubernetesExecutor", "CeleryExecutor", "LocalExecutor")
ROLES = ("Admin", "Op", "User", "Viewer")
COMPONENTS = ("scheduler", "api_server", "dag_processor", "triggerer", "worker")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CHART_VERSION = "1.20.0"                                            # the official Apache Airflow chart (pinned)
PINNED = "3.3.2"
# Names Cairn sets on every deployment (its identity, database, keys, sign-in): never from the spec.
RESERVED = re.compile(r"^(CAIRN_.*|AIRFLOW__CORE__(FERNET_KEY|EXECUTOR|AUTH_MANAGER|DAGS_FOLDER|"
                      r"EXECUTION_API_SERVER_URL)|AIRFLOW__DATABASE__.*|AIRFLOW__API_AUTH__.*|AIRFLOW__API__.*|"
                      r"AIRFLOW__SECRETS__.*|AIRFLOW__FAB__.*|AIRFLOW__OPENLINEAGE__.*|AIRFLOW__METRICS__OTEL.*|"
                      r"AIRFLOW__TRACES__.*|AIRFLOW_CONN_CAIRN_DEFAULT|AIRFLOW__WEBSERVER__SECRET_KEY|"
                      r"PYTHONPATH|PATH|HOME)$")
SECRETISH = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE|API_?KEY|ACCESS_?KEY|CREDENTIAL|_PWD$|^AIRFLOW_CONN_)",
                       re.I)
# A value that looks like a credential, whatever its name: keys, tokens, URLs with a password.
SECRET_VALUE = re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[abp]-|"
                          r"sk-[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{10,}\.eyJ|://[^/\s:@]+:[^/\s@]+@)")

RESOURCE_DEFAULTS = {
    "scheduler": {"cpu": 1.0, "memory_mb": 2048, "replicas": 1},
    "api_server": {"cpu": 0.5, "memory_mb": 1024, "replicas": 1},
    "dag_processor": {"cpu": 0.5, "memory_mb": 1024, "replicas": 1},
    "triggerer": {"cpu": 0.25, "memory_mb": 512, "replicas": 1},
    "worker": {"cpu": 0.5, "memory_mb": 1024, "replicas": 1},    # task pods (Kubernetes), or Celery workers
}
DEFAULTS: dict[str, Any] = {
    "workspace": None, "team": None, "environment": "dev", "airflow_version": PINNED, "executor": None,
    "resources": None, "dag_source": None, "env": None, "secrets": None, "hibernation": None, "roles": None,
    "promotes_to": None, "approval": None,
}


def supported_versions() -> tuple[str, ...]:
    """The Airflow versions deployments may run: pins.json's ``airflow.deployments`` (the versions tested), else the
    pinned one."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.environ.get("CAIRN_VERSION_PINS", ""),
                 os.path.join(here, "..", "..", "..", "deploy", "versions", "pins.json")):
        if path and os.path.exists(path):
            try:
                with open(path) as fh:
                    pins = json.load(fh)
                got = tuple(str(v) for v in (pins.get("airflow") or {}).get("deployments") or ())
                if got:
                    return got
            except (OSError, ValueError):
                pass
    return (PINNED,)


def check_name(name: Any, what: str = "a deployment name") -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise SpecError(f"{what} is lowercase letters, digits and '-' (up to 24), starting with a letter")
    return name


def check_environment(env: Any) -> str:
    if not isinstance(env, str) or not ENV_NAME.match(env):
        raise SpecError("environment is dev, stage, prod or a name of your own (lowercase letters, digits, '-', up to 16)")
    return env


def _number(v: Any, what: str, lo: float, hi: float, integer: bool = False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise SpecError(f"{what} is a {'whole ' if integer else ''}number")
    if not lo <= v <= hi:
        raise SpecError(f"{what} is {lo:g} to {hi:g}")
    return int(v) if integer else float(v)


def check_resources(r: Any, base: Optional[dict] = None) -> dict:
    out = {k: dict(v) for k, v in (base or RESOURCE_DEFAULTS).items()}
    if r is None:
        return out
    if not isinstance(r, dict) or set(r) - set(COMPONENTS):
        raise SpecError(f"resources is {{component: {{cpu, memory_mb, replicas}}}} for {', '.join(COMPONENTS)}")
    for comp, v in r.items():
        if not isinstance(v, dict) or set(v) - {"cpu", "memory_mb", "replicas"}:
            raise SpecError(f"resources.{comp} is {{cpu, memory_mb, replicas}}")
        if "cpu" in v:
            out[comp]["cpu"] = _number(v["cpu"], f"resources.{comp}.cpu", 0.1, 32)
        if "memory_mb" in v:
            out[comp]["memory_mb"] = _number(v["memory_mb"], f"resources.{comp}.memory_mb", 256, 256 * 1024, True)
        if "replicas" in v:
            out[comp]["replicas"] = _number(v["replicas"], f"resources.{comp}.replicas", 1, 10, True)
    return out


def check_dag_source(d: Any) -> dict:
    if d is None:
        return {"type": "bundle"}
    if not isinstance(d, dict) or d.get("type") not in ("bundle", "git"):
        raise SpecError('dag_source is {"type": "bundle"} (deploys from a project) or {"type": "git", "folder": '
                        '"<git folder id>", "path": "dags"}')
    if d["type"] == "bundle":
        if set(d) - {"type"}:
            raise SpecError("a bundle source has no other settings")
        return {"type": "bundle"}
    if set(d) - {"type", "folder", "path"} or not isinstance(d.get("folder"), str) or not d["folder"]:
        raise SpecError("a git source names its git folder (folder) and optionally a path in it")
    path = str(d.get("path") or "").strip("/")
    if ".." in path.split("/") or not re.match(r"^[A-Za-z0-9._/-]{0,256}$", path):
        raise SpecError("dag_source.path is a folder in the repository")
    return {"type": "git", "folder": d["folder"], "path": path}


def check_env(e: Any) -> dict:
    if e is None:
        return {}
    if not isinstance(e, dict) or len(e) > 200:
        raise SpecError("env is {NAME: value} (up to 200)")
    out = {}
    for k, v in e.items():
        if not isinstance(k, str) or not VAR.match(k):
            raise SpecError(f"env: {k!r} is not a variable name")
        if RESERVED.match(k):
            raise SpecError(f"env: {k} is set by Cairn")
        if SECRETISH.search(k):
            raise SpecError(f"env: {k} looks like a secret: put it in a secret scope and reference it in secrets")
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise SpecError(f"env: {k} is text")
        s = str(v)
        if len(s) > 4096 or "\n" in s or "\x00" in s:
            raise SpecError(f"env: {k} is one line, up to 4096 characters")
        if SECRET_VALUE.search(s):
            raise SpecError(f"env: {k}'s value looks like a credential: put it in a secret scope and reference it")
        out[k] = s
    return out


def secret_target(ref: dict) -> str:
    """The environment variable a secret reference becomes in the deployment."""
    if ref.get("connection"):
        return "AIRFLOW_CONN_" + ref["connection"].upper()
    if ref.get("variable"):
        return "AIRFLOW_VAR_" + ref["variable"].upper()
    return ref["env"]


def check_secrets(s: Any) -> list[dict]:
    if s is None:
        return []
    if not isinstance(s, list) or len(s) > 100:
        raise SpecError("secrets is a list (up to 100) of references: {\"env\" | \"connection\" | \"variable\": ..., "
                        "\"scope\": ..., \"key\": ...}")
    out, seen = [], set()
    for i, ref in enumerate(s):
        if not isinstance(ref, dict):
            raise SpecError(f"secrets[{i}] is an object")
        if "value" in ref:
            raise SpecError(f"secrets[{i}]: a deployment never holds a secret's value; reference a secret scope's key")
        targets = [k for k in ("env", "connection", "variable") if ref.get(k)]
        if len(targets) != 1 or set(ref) - {"env", "connection", "variable", "scope", "key"}:
            raise SpecError(f"secrets[{i}] names one of env, connection or variable, plus scope and key")
        t = targets[0]
        v = ref[t]
        if t == "env" and (not isinstance(v, str) or not VAR.match(v) or RESERVED.match(v)):
            raise SpecError(f"secrets[{i}].env is a variable name Cairn does not set itself")
        if t == "connection" and (not isinstance(v, str) or not CONN.match(v) or v == "cairn_default"):
            raise SpecError(f"secrets[{i}].connection is a connection id (lowercase, digits, _), not cairn_default")
        if t == "variable" and (not isinstance(v, str) or not VAR.match(v)):
            raise SpecError(f"secrets[{i}].variable is a variable name")
        for k in ("scope", "key"):
            if not isinstance(ref.get(k), str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", ref[k]):
                raise SpecError(f"secrets[{i}].{k} names a secret scope's {k}")
        item = {t: v, "scope": ref["scope"], "key": ref["key"]}
        target = secret_target(item)
        if target in seen:
            raise SpecError(f"secrets: {target} is given twice")
        seen.add(target)
        out.append(item)
    return out


def _hhmm(v: Any, what: str) -> str:
    if not isinstance(v, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$", v):
        raise SpecError(f"{what} is HH:MM")
    return v


def _days(v: Any, what: str) -> list[str]:
    if isinstance(v, str):
        if "-" in v:
            a, _, b = v.partition("-")
            if a not in DAYS or b not in DAYS:
                raise SpecError(f"{what} is like mon-fri, or a list of days")
            i, j = DAYS.index(a), DAYS.index(b)
            return [DAYS[(i + k) % 7] for k in range((j - i) % 7 + 1)]
        v = [v]
    if not isinstance(v, list) or not v or any(d not in DAYS for d in v):
        raise SpecError(f"{what} is like mon-fri, or a list of days ({', '.join(DAYS)})")
    return [d for d in DAYS if d in v]


def check_hibernation(h: Any, environment: str) -> Optional[dict]:
    if h is None or h is False:
        return None
    if environment == "prod":
        raise SpecError("prod deployments do not hibernate")
    if not isinstance(h, dict) or set(h) - {"awake", "timezone", "wake_minutes"}:
        raise SpecError('hibernation is {"awake": [{"days": "mon-fri", "from": "08:00", "to": "19:00"}], '
                        '"timezone": "Europe/Berlin", "wake_minutes": 60}')
    awake = h.get("awake")
    if not isinstance(awake, list) or not 1 <= len(awake) <= 14:
        raise SpecError("hibernation.awake lists the hours it runs (1 to 14 entries); outside them it scales to zero")
    out_awake = []
    for i, w in enumerate(awake):
        if not isinstance(w, dict) or set(w) - {"days", "from", "to"}:
            raise SpecError(f"hibernation.awake[{i}] is {{days, from, to}}")
        f, t = _hhmm(w.get("from"), f"hibernation.awake[{i}].from"), _hhmm(w.get("to"), f"hibernation.awake[{i}].to")
        if t <= f:
            raise SpecError(f"hibernation.awake[{i}]: to comes after from (same day)")
        out_awake.append({"days": _days(w.get("days", "mon-fri"), f"hibernation.awake[{i}].days"), "from": f, "to": t})
    tz = h.get("timezone", "UTC")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(str(tz))
    except Exception:  # noqa: BLE001 - any unknown zone
        raise SpecError("hibernation.timezone is an IANA time zone, e.g. Europe/Berlin")
    wake = h.get("wake_minutes", 60)
    wake = _number(wake, "hibernation.wake_minutes", 5, 24 * 60, True)
    return {"awake": out_awake, "timezone": str(tz), "wake_minutes": wake}


def awake_now(h: Optional[dict], now: dt.datetime) -> bool:
    """Whether a hibernation schedule says the deployment runs at ``now`` (UTC-aware); no schedule: always."""
    if not h:
        return True
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(h["timezone"]))
    day, hm = DAYS[local.weekday()], local.strftime("%H:%M")
    return any(day in w["days"] and w["from"] <= hm < w["to"] for w in h["awake"])


def check_roles(r: Any) -> dict:
    if r is None:
        return {}
    if not isinstance(r, dict) or len(r) > 50:
        raise SpecError('roles is {"<Cairn group>": "Admin" | "Op" | "User" | "Viewer"}')
    out = {}
    for g, role in r.items():
        g = str(g).removeprefix("urn:li:corpGroup:")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._@ -]{0,127}$", g):
            raise SpecError(f"roles: {g!r} is not a group name")
        if role not in ROLES:
            raise SpecError(f"roles: {g}'s role is one of {', '.join(ROLES)}")
        out[g] = role
    return out


def check_approval(a: Any, environment: str) -> dict:
    if a is None:
        return {"required": environment == "prod"}
    if not isinstance(a, dict) or set(a) - {"required"} or not isinstance(a.get("required", False), bool):
        raise SpecError('approval is {"required": true|false}: promotions into this deployment wait for an approver')
    return {"required": bool(a.get("required"))}


def check_spec(body: dict, base: Optional[dict] = None, versions: Optional[tuple[str, ...]] = None) -> dict:
    """A full deployment spec from ``body`` over ``base`` (an update) or the defaults (a create)."""
    extra = set(body) - set(DEFAULTS) - {"name"}
    if extra:
        raise SpecError(f"unknown fields: {', '.join(sorted(extra))}")
    out = dict(base or DEFAULTS)
    for k in DEFAULTS:
        if k in body:
            out[k] = body[k]
    out["environment"] = check_environment(out["environment"])
    v = str(out["airflow_version"])
    allowed = versions or supported_versions()
    if v not in allowed:
        raise SpecError(f"airflow_version is one of {', '.join(allowed)} (the versions Cairn is tested with)")
    out["airflow_version"] = v
    if out["executor"] is not None and out["executor"] not in EXECUTORS:
        raise SpecError(f"executor is one of {', '.join(EXECUTORS)}")
    for k in ("workspace", "promotes_to"):
        if out[k] is not None and (not isinstance(out[k], str) or not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", out[k])):
            raise SpecError(f"{k} is a name (lowercase letters, digits, '-')")
    if out["team"] is not None:
        t = str(out["team"]).removeprefix("urn:li:corpGroup:")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._@ -]{0,127}$", t):
            raise SpecError("team is a Cairn group")
        out["team"] = t
    out["resources"] = check_resources(body.get("resources"), (base or {}).get("resources"))
    out["dag_source"] = check_dag_source(out["dag_source"])
    out["env"] = check_env(out["env"])
    out["secrets"] = check_secrets(out["secrets"])
    out["hibernation"] = check_hibernation(out["hibernation"], out["environment"])
    out["roles"] = check_roles(out["roles"])
    if not out["roles"] and out["team"]:
        out["roles"] = {out["team"]: "User"}
    out["approval"] = check_approval(out["approval"], out["environment"]) if "approval" in body or not base \
        else base["approval"]
    return out
