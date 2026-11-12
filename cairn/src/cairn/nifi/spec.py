"""What a managed NiFi deployment and a NiFi flow may be asked to be. Everything a caller sends is checked here before
a runtime or NiFi sees it; names, sizes and the rules about secrets are the managed Airflow ones
(:mod:`cairn.airflow_deploy.spec`): a value that looks like a credential is refused, secrets are references to Cairn
secret scopes."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from cairn.airflow_deploy.spec import (SECRET_VALUE, SpecError, _number, check_environment, check_name)

PINNED = "2.12.0"                                  # apache/nifi, the upstream image (deploy/versions/pins.json)
PARAM = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]{0,254}$")
SCOPE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
URN = re.compile(r"^urn:li:[A-Za-z]+:.{1,1000}$")
RESOURCE_DEFAULTS = {"cpu": 1.0, "memory_mb": 4096, "heap_mb": 2048, "storage_gb": 20}
DEPLOYMENT_DEFAULTS: dict[str, Any] = {"workspace": None, "team": None, "environment": "dev", "nifi_version": PINNED,
                                       "resources": None, "domain": None, "host_access": False}
FLOW_DEFAULTS: dict[str, Any] = {"deployment": None, "description": "", "domain": None, "definition": None,
                                 "parameters": None, "secrets": None, "sink": None, "lineage": None, "running": True}
# Parameters Cairn sets on every flow that lands in the lakehouse: never from the spec.
RESERVED_PREFIX = "cairn."


def supported_versions() -> tuple[str, ...]:
    """The NiFi versions deployments may run: pins.json's ``nifi.deployments`` (the versions tested), else the pin."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.environ.get("CAIRN_VERSION_PINS", ""),
                 os.path.join(here, "..", "..", "..", "deploy", "versions", "pins.json")):
        if path and os.path.exists(path):
            try:
                with open(path) as fh:
                    got = tuple(str(v) for v in ((json.load(fh).get("nifi") or {}).get("deployments") or ()))
                if got:
                    return got
            except (OSError, ValueError):
                pass
    return (PINNED,)


def _group(v: Any, what: str) -> Optional[str]:
    if v is None:
        return None
    g = str(v).removeprefix("urn:li:corpGroup:")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._@ -]{0,127}$", g):
        raise SpecError(f"{what} is a Cairn group")
    return g


def _domain(v: Any) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str) or not v.startswith("urn:li:domain:") or len(v) > 300:
        raise SpecError("domain is a domain URN (urn:li:domain:...)")
    return v


def check_resources(r: Any, base: Optional[dict] = None) -> dict:
    out = dict(base or RESOURCE_DEFAULTS)
    if r is None:
        return out
    if not isinstance(r, dict) or set(r) - set(RESOURCE_DEFAULTS):
        raise SpecError("resources is {cpu, memory_mb, heap_mb, storage_gb}")
    if "cpu" in r:
        out["cpu"] = _number(r["cpu"], "resources.cpu", 0.5, 64)
    if "memory_mb" in r:
        out["memory_mb"] = _number(r["memory_mb"], "resources.memory_mb", 2048, 512 * 1024, True)
    if "heap_mb" in r:
        out["heap_mb"] = _number(r["heap_mb"], "resources.heap_mb", 1024, 256 * 1024, True)
    if "storage_gb" in r:
        out["storage_gb"] = _number(r["storage_gb"], "resources.storage_gb", 5, 10_000, True)
    if out["heap_mb"] > out["memory_mb"] * 3 // 4:
        raise SpecError("resources.heap_mb is at most three quarters of memory_mb (NiFi needs memory beside its heap)")
    if base is not None and out["storage_gb"] < base["storage_gb"]:
        raise SpecError("resources.storage_gb only grows (a volume is not shrunk)")
    return out


def check_deployment(body: dict, base: Optional[dict] = None, versions: Optional[tuple[str, ...]] = None) -> dict:
    """A deployment's spec from ``body`` over ``base`` (a change: scaling, an upgrade) or the defaults (a create)."""
    extra = set(body) - set(DEPLOYMENT_DEFAULTS) - {"name"}
    if extra:
        raise SpecError(f"unknown fields: {', '.join(sorted(extra))}")
    out = dict(base or DEPLOYMENT_DEFAULTS)
    for k in DEPLOYMENT_DEFAULTS:
        if k in body and k != "resources":
            out[k] = body[k]
    out["environment"] = check_environment(out["environment"])
    allowed = versions or supported_versions()
    if str(out["nifi_version"]) not in allowed:
        raise SpecError(f"nifi_version is one of {', '.join(allowed)} (the versions Cairn is tested with)")
    out["nifi_version"] = str(out["nifi_version"])
    if out["workspace"] is not None and (not isinstance(out["workspace"], str)
                                         or not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", out["workspace"])):
        raise SpecError("workspace is a name (lowercase letters, digits, '-')")
    out["team"] = _group(out["team"], "team")
    out["domain"] = _domain(out["domain"])
    if not isinstance(out.get("host_access", False), bool):
        raise SpecError("host_access is true or false (true: one team's NiFi, whose flows may run commands and "
                        "scripts and reach NiFi's files)")
    out["host_access"] = bool(out.get("host_access", False))
    out["resources"] = check_resources(body.get("resources"), (base or {}).get("resources"))
    return out


def check_flow_name(name: Any) -> str:
    if not isinstance(name, str) or not re.match(r"^[a-z][a-z0-9_-]{0,62}$", name):
        raise SpecError("a flow name is lowercase letters, digits, '-' and '_' (up to 63), starting with a letter")
    return name


def check_parameters(p: Any) -> dict[str, str]:
    """Non-sensitive parameter values: anything that looks like a credential is refused (it goes in secrets)."""
    if p is None:
        return {}
    if not isinstance(p, dict) or len(p) > 500:
        raise SpecError("parameters is {name: value} (up to 500)")
    out = {}
    for k, v in p.items():
        if not isinstance(k, str) or not PARAM.match(k):
            raise SpecError(f"parameters: {k!r} is not a parameter name")
        if k.startswith(RESERVED_PREFIX):
            raise SpecError(f"parameters: {k} is set by Cairn")
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise SpecError(f"parameters: {k} is text")
        s = str(v)
        if len(s) > 65536 or "\x00" in s:
            raise SpecError(f"parameters: {k} is up to 64 KiB of text")
        if SECRET_VALUE.search(s):
            raise SpecError(f"parameters: {k}'s value looks like a credential: put it in a secret scope and reference it "
                            "in secrets (a sensitive parameter)")
        out[k] = s
    return out


def check_secrets(s: Any, parameters: dict) -> list[dict]:
    """Sensitive parameters: each a reference to a secret scope's key, read as the flow's owner when it is deployed."""
    if s is None:
        return []
    if not isinstance(s, list) or len(s) > 100:
        raise SpecError('secrets is a list (up to 100) of {"parameter": ..., "scope": ..., "key": ...}')
    out, seen = [], set()
    for i, ref in enumerate(s):
        if not isinstance(ref, dict):
            raise SpecError(f"secrets[{i}] is an object")
        if "value" in ref:
            raise SpecError(f"secrets[{i}]: a flow never holds a secret's value; reference a secret scope's key")
        if set(ref) - {"parameter", "scope", "key"}:
            raise SpecError(f"secrets[{i}] is {{parameter, scope, key}}")
        name = ref.get("parameter")
        if not isinstance(name, str) or not PARAM.match(name) or name.startswith(RESERVED_PREFIX):
            raise SpecError(f"secrets[{i}].parameter is a parameter name Cairn does not set itself")
        if name in parameters or name in seen:
            raise SpecError(f"secrets: {name} is given twice")
        for k in ("scope", "key"):
            if not isinstance(ref.get(k), str) or not SCOPE_KEY.match(ref[k]):
                raise SpecError(f"secrets[{i}].{k} names a secret scope's {k}")
        seen.add(name)
        out.append({"parameter": name, "scope": ref["scope"], "key": ref["key"]})
    return out


def check_sink(s: Any) -> Optional[dict]:
    """Where the flow lands: ``{"type": "lakehouse", "namespace": ..., "table": ...}`` (an Iceberg table in Cairn's
    lakehouse, written as the flow's owner) or nothing."""
    if s is None:
        return None
    if not isinstance(s, dict) or s.get("type") != "lakehouse" or set(s) - {"type", "namespace", "table"}:
        raise SpecError('sink is {"type": "lakehouse", "namespace": "<schema>", "table": "<table>"}')
    ns, t = s.get("namespace"), s.get("table")
    if not isinstance(ns, str) or not re.match(r"^[a-z_][a-z0-9_]{0,127}(\.[a-z_][a-z0-9_]{0,127}){0,3}$", ns):
        raise SpecError("sink.namespace is a lakehouse schema (lowercase, dots between levels)")
    if not isinstance(t, str) or not re.match(r"^[a-z_][a-z0-9_]{0,127}$", t):
        raise SpecError("sink.table is a table name (lowercase letters, digits, _)")
    return {"type": "lakehouse", "namespace": ns, "table": t}


def check_lineage(v: Any) -> dict:
    """Datasets the flow reads and writes that Cairn cannot tell from its processors' settings (URNs)."""
    if v is None:
        return {"inputs": [], "outputs": []}
    if not isinstance(v, dict) or set(v) - {"inputs", "outputs"}:
        raise SpecError('lineage is {"inputs": [<dataset urn>...], "outputs": [...]}')
    out = {}
    for side in ("inputs", "outputs"):
        items = v.get(side) or []
        if not isinstance(items, list) or len(items) > 100 or not all(
                isinstance(u, str) and u.startswith("urn:li:dataset:") and URN.match(u) for u in items):
            raise SpecError(f"lineage.{side} is a list of dataset URNs (up to 100)")
        out[side] = sorted(set(items))
    return out


def check_flow(body: dict, base: Optional[dict] = None) -> dict:
    """A flow's spec (its definition checked separately, by :func:`cairn.nifi.flowdef.check_definition`)."""
    extra = set(body) - set(FLOW_DEFAULTS) - {"name"}
    if extra:
        raise SpecError(f"unknown fields: {', '.join(sorted(extra))}")
    out = dict(base or FLOW_DEFAULTS)
    for k in FLOW_DEFAULTS:
        if k in body:
            out[k] = body[k]
    if base is not None and out["deployment"] != base["deployment"]:
        raise SpecError("a flow's deployment is fixed: make the flow again on the other deployment")
    out["deployment"] = check_name(out["deployment"], "deployment")
    if not isinstance(out["description"], str) or len(out["description"]) > 5000:
        raise SpecError("description is text (up to 5000 characters)")
    out["domain"] = _domain(out["domain"])
    out["parameters"] = check_parameters(out["parameters"])
    out["secrets"] = check_secrets(out["secrets"], out["parameters"])
    out["sink"] = check_sink(out["sink"])
    out["lineage"] = check_lineage(out["lineage"])
    if not isinstance(out["running"], bool):
        raise SpecError("running is true or false")
    if out["definition"] is None:
        raise SpecError("definition is the flow: a NiFi flow definition (Download flow definition, or "
                        "GET /v1/nifi/flow-templates/lakehouse)")
    return out
