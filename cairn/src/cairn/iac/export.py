"""``cairn export``: a live Cairn as code, so an existing workspace comes under git.

Reads every registry kind (:mod:`cairn.iac.registry`) through Cairn's API as the token's user and writes either a
bundle (YAML, for ``cairn bundle``) or Terraform (``.tf`` with ``resource`` and ``import`` blocks, Terraform 1.5+).
Applying the result changes nothing: ``terraform plan`` shows only the imports, ``cairn bundle plan`` shows every
resource unchanged (and adopts them). What only Cairn knows stays out: ids it made, timestamps, state.

Secrets never appear as values. Write-only fields (a secret's value) and sensitive fields (webhook URLs) are
written as variables: ``${NAME}`` in a bundle, ``var.<name>`` with a ``sensitive`` variable block in Terraform; the
values are given at apply time, from the environment or a variables file kept out of git.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Optional

from cairn.iac import registry
from cairn.iac.engine import Engine, EngineError, declared, key_values, tf_id
from cairn.iac.registry import UNSET, Kind

SKIPPED_STATUS = (403, 404, 405, 501, 503)       # a kind that is off here, or not the caller's to read


class ExportError(Exception):
    pass


def _status(e: Exception) -> Optional[int]:
    return getattr(e, "status", None) or getattr(e, "status_code", None)


def collect(call: Callable[..., Any], kinds: Optional[Iterable[str]] = None, me: Optional[str] = None,
            errors: Optional[list] = None) -> list[tuple[Kind, dict]]:
    """Every resource of the kinds (all by default) the caller may read: (kind, record), in creation order."""
    by = registry.by_name()
    wanted = [registry.kind(k) for k in kinds] if kinds else registry.kinds()
    engine = Engine(call)
    if me is None and any(k.mine for k in wanted):
        me = (call("GET", "/me") or {}).get("urn")
    out: list[tuple[Kind, dict]] = []
    for k in wanted:
        try:
            rows = engine.all(k, by)
        except Exception as e:  # noqa: BLE001 - a kind that is switched off here is skipped, and said so
            if _status(e) in SKIPPED_STATUS or isinstance(e, EngineError):
                if errors is not None:
                    errors.append((k.name, str(e)))
                continue
            raise
        if k.mine:
            rows = [r for r in rows if r.get(k.owner_field) == me]
        out += [(k, r) for r in rows]
    return out


def spec(k: Kind, rec: dict, names: Optional[dict] = None) -> dict:
    """A record as its bundle declaration: its fields, defaults left out, saved queries by name."""
    from cairn.iac.bundle import _as_declared

    d = declared(k, rec)
    d = {f: v for f, v in _as_declared(k.name, {**rec, **d}, names or {}).items() if f in d}
    out = {}
    for f in k.fields:
        if f.name not in d or f.write_only or f.once:
            continue
        v = d[f.name]
        if not f.required and f.default is not UNSET and v == f.default:
            continue
        if v is None and not f.required:
            continue
        out[f.name] = v
    return out


def as_record(k: Kind, values: dict) -> dict:
    """Declared values as Cairn would hold them (fields under their read names, dotted ones nested)."""
    out: dict = {}
    for f in k.fields:
        if f.name not in values:
            continue
        cur = out
        *path, last = f.read_name.split(".")
        for part in path:
            cur = cur.setdefault(part, {})
        cur[last] = values[f.name]
    return out


def variable_name(k: Kind, rec: dict, field: str) -> str:
    base = "_".join([k.tf, *key_values(k, rec), field]) if k.key else f"{k.tf}_{field}"
    return re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")


# ---- bundles ----------------------------------------------------------------------------------------------------
def to_bundle(items: list[tuple[Kind, dict]], name: str = "exported", names: Optional[dict] = None) -> str:
    """The resources as a bundle; secrets as ``${VARIABLES}``."""
    import yaml

    doc: dict[str, Any] = {"bundle": name}
    for k, rec in items:
        s = spec(k, rec, names)
        for f in k.fields:
            if f.write_only or (f.sensitive and f.name in s):
                s[f.name] = "${" + variable_name(k, rec, f.name).upper() + "}"
        doc.setdefault(k.name, []).append(s)
    head = ("# Exported from Cairn by `cairn export`. Secrets are ${VARIABLES}: give them with --var NAME=value or\n"
            "# the environment when planning and applying (`cairn bundle plan|apply`).\n")
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)


# ---- Terraform --------------------------------------------------------------------------------------------------
def hcl_string(s: str) -> str:
    out = json.dumps(s, ensure_ascii=False)
    return out.replace("${", "$${").replace("%{", "%%{")


def hcl_value(v: Any, indent: int = 0) -> str:
    pad = "  " * (indent + 1)
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return hcl_string(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        if all(not isinstance(x, (dict, list)) for x in v):
            return "[" + ", ".join(hcl_value(x) for x in v) + "]"
        return "[\n" + ",\n".join(pad + hcl_value(x, indent + 1) for x in v) + ",\n" + "  " * indent + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{\n" + "\n".join(f"{pad}{hcl_string(str(a))} = {hcl_value(b, indent + 1)}" for a, b in v.items()) + \
            "\n" + "  " * indent + "}"
    raise ExportError(f"cannot write {type(v).__name__} as HCL")


def label(k: Kind, rec: dict, seen: set) -> str:
    base = "_".join(key_values(k, rec)) if k.key else k.tf
    base = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or k.tf
    if base[0].isdigit():
        base = "r_" + base
    out, n = base, 2
    while (k.tf, out) in seen:
        out, n = f"{base}_{n}", n + 1
    seen.add((k.tf, out))
    return out


def resource_block(k: Kind, rec: dict, name: str, variables: dict, values: Optional[dict] = None,
                   refs: Optional[dict] = None) -> str:
    """One resource block, from Cairn's record (or from declared ``values``: a configuration to create from).
    ``refs``: (kind, field, value) -> the address of the resource it names, written as a reference."""
    d = declared(k, rec) if values is None else values
    if values is not None:
        rec = as_record(k, values)
    lines = [f'resource "cairn_{k.tf}" "{name}" {{']
    for f in k.fields:
        if f.once:
            continue
        if f.write_only or (f.sensitive and d.get(f.name) not in (None, "", [])):
            var = variable_name(k, rec, f.name)
            variables[var] = (f, "list(string)" if f.type == "list" else "string")
            lines.append(f"  {f.tf_name} = var.{var}")
            continue
        if f.name not in d:
            continue
        v = d[f.name]
        if v is None or (not f.required and f.default is not UNSET and v == f.default) or (f.only_set and v in ([], {})):
            continue
        if f.ref and refs and (f.ref, str(v)) in refs and not refs[(f.ref, str(v))].startswith(f"cairn_{k.tf}.{name}."):
            lines.append(f"  {f.tf_name} = {refs[(f.ref, str(v))]}")
        elif f.type == "json":
            lines.append(f"  {f.tf_name} = jsonencode({hcl_value(v, 1)})")
        elif f.type == "list":
            lines.append(f"  {f.tf_name} = {hcl_value([str(x) for x in v], 1)}")
        else:
            lines.append(f"  {f.tf_name} = {hcl_value(v, 1)}")
    lines.append("}")
    return "\n".join(lines)


def to_terraform(items: list[tuple[Kind, dict]], declared_values: bool = False) -> str:
    """The resources as Terraform: a resource block and an import block each; secrets as sensitive variables.
    With ``declared_values``, ``items`` are (kind, declared values) to create: resource blocks alone."""
    seen: set = set()
    variables: dict[str, tuple] = {}
    blocks = []
    named = [(k, rec, label(k, as_record(k, rec) if declared_values else rec, seen)) for k, rec in items]
    refs: dict = {}                       # what other resources name, as references (so Terraform orders them)
    for k, rec, name in named:
        vals = rec if declared_values else {**declared(k, rec), "id": rec.get(k.id)}
        for f in [*k.fields, registry.Field("id")]:
            tf = "id" if f.name == "id" and f not in k.fields else f.tf_name
            if vals.get(f.name) is not None:
                refs.setdefault((f"{k.name}.{f.name}", str(vals[f.name])), f"cairn_{k.tf}.{name}.{tf}")
    for k, rec, name in named:
        if declared_values:
            blocks.append(resource_block(k, {}, name, variables, values=rec, refs=refs))
            continue
        blocks.append(resource_block(k, rec, name, variables, refs=refs))
        blocks.append(f'import {{\n  to = cairn_{k.tf}.{name}\n  id = {hcl_string(tf_id(k, rec))}\n}}')
    head = ['# Exported from Cairn by `cairn export --format terraform`. `terraform plan` shows the imports and no',
            '# changes. Secrets are sensitive variables: give them in TF_VAR_<name> or a variables file kept out of git.',
            'terraform {', '  required_version = ">= 1.11"',
            '  required_providers {', '    cairn = { source = "cairn/cairn" }', '  }', '}', '',
            'provider "cairn" {}   # CAIRN_URL and CAIRN_TOKEN (or CAIRN_TOKEN_FILE)', '']
    for var, (f, typ) in sorted(variables.items()):
        head.append(f'variable "{var}" {{\n  type        = {typ}\n  sensitive   = true\n'
                    f'  description = {hcl_string(f.description or f.name)}\n}}\n')
    return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"


def query_names(call: Callable[..., Any], items: list[tuple[Kind, dict]]) -> dict:
    """Saved query ids -> names, so a bundle names them (dashboards, alerts, workflows)."""
    return {r["id"]: str(r["name"]) for k, r in items if k.name == "queries"}


def export(call: Callable[..., Any], fmt: str = "terraform", kinds: Optional[Iterable[str]] = None,
           bundle: str = "exported", errors: Optional[list] = None, ident: Optional[str] = None) -> str:
    items = collect(call, kinds, errors=errors)
    if ident is not None:
        items = [(k, r) for k, r in items if tf_id(k, r) == ident]
        if not items:
            raise ExportError("no such resource that you may read")
    if fmt in ("yaml", "bundle"):
        names = query_names(call, items) if any(k.name == "queries" for k, _ in items) else \
            ({r["id"]: str(r["name"]) for r in (call("GET", "/sql/saved") or [])}
             if any(k.name in ("dashboards", "alerts", "workflows") for k, _ in items) else {})
        return to_bundle(items, bundle, names)
    if fmt == "terraform":
        return to_terraform(items)
    raise ExportError("format: terraform or yaml")


def main(args) -> int:
    """``cairn export [--format terraform|yaml] [--kind K ...] [--out FILE]``."""
    import os

    from cairn.iac.bundle import BundleError, Client

    url = args.url or os.environ.get("CAIRN_URL", "")
    token = args.token or os.environ.get("CAIRN_TOKEN", "")
    if os.environ.get("CAIRN_TOKEN_FILE") and not token:
        token = open(os.environ["CAIRN_TOKEN_FILE"]).read().strip()
    if not url or not token:
        print("cairn export: set CAIRN_URL and CAIRN_TOKEN (or --url and --token)")
        return 1
    client = Client(url, token, os.environ.get("CAIRN_CA_FILE") or True)
    errors: list = []
    try:
        text = export(client.call, args.format, args.kind or None, args.bundle, errors)
    except (BundleError, ExportError, EngineError) as e:
        print(f"cairn export: {e}")
        return 1
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        print(text, end="")
    for kind, why in errors:
        print(f"cairn export: skipped {kind}: {why}", file=__import__("sys").stderr)
    return 0
