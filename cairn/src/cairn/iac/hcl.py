"""Terraform files (the subset ``cairn export --format terraform`` writes) read as a bundle, for change requests.

What is read: ``resource "cairn_<type>" "<label>" { ... }`` blocks with literal values (strings, numbers, booleans,
null, lists, objects), ``jsonencode(...)``, ``var.<name>`` (kept as the reference ``${<name>}``: a secret's value is
given when the change is applied, never written in it) and references to other resources in the same files
(``cairn_saved_query.revenue.id``: the resource it names, as a bundle names it). ``terraform``, ``provider``,
``variable``, ``import``, ``output`` and ``locals`` blocks are skipped. Anything else (interpolation, data sources,
modules, ``count``, functions other than ``jsonencode``) is refused with its line, since a change request is reviewed
as plain values.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from cairn.iac import registry


class HclError(Exception):
    pass


TOKEN = re.compile(r"""
    (?P<ws>[ \t\r]+) | (?P<nl>\n) | (?P<comment>\#[^\n]*|//[^\n]*|/\*.*?\*/) |
    (?P<heredoc><<-?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\n) |
    (?P<string>"(?:[^"\\\n]|\\.)*") | (?P<number>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?) |
    (?P<ident>[A-Za-z_][A-Za-z0-9_-]*) | (?P<punct>[{}\[\](),=:.])
""", re.X | re.S)
SKIPPED = {"terraform", "provider", "variable", "import", "output", "locals", "moved", "removed", "check"}


class _Ref:
    def __init__(self, parts: list[str], line: int):
        self.parts, self.line = parts, line


def _lex(text: str) -> list[tuple[str, Any, int]]:
    out, i, line = [], 0, 1
    while i < len(text):
        m = TOKEN.match(text, i)
        if not m:
            raise HclError(f"line {line}: cannot read {text[i:i + 20]!r}")
        kind = m.lastgroup if m.lastgroup != "tag" else "heredoc"
        if m.group("heredoc"):
            tag, indent = m.group("tag"), m.group(0).startswith("<<-")
            end = re.compile(rf"^[ \t]*{re.escape(tag)}[ \t]*$", re.M)
            e = end.search(text, m.end())
            if not e:
                raise HclError(f"line {line}: heredoc {tag} has no end")
            body = text[m.end():e.start()]
            if indent:
                lines = body.split("\n")
                pad = min((len(x) - len(x.lstrip()) for x in lines if x.strip()), default=0)
                body = "\n".join(x[pad:] for x in lines)
            out.append(("string", _unescape_plain(body, line), line))
            line += text[m.start():e.end()].count("\n")
            i = e.end()
            continue
        val = m.group(0)
        if kind == "string":
            out.append(("string", _unescape(val, line), line))
        elif kind == "number":
            out.append(("number", float(val) if any(c in val for c in ".eE") else int(val), line))
        elif kind in ("ident", "punct"):
            out.append((kind, val, line))
        elif kind == "nl":
            out.append(("nl", "\n", line))
        line += val.count("\n") if kind != "nl" else 1
        i = m.end()
    return out


def _unescape(raw: str, line: int) -> str:
    try:
        s = json.loads(raw)
    except ValueError:
        raise HclError(f"line {line}: a string Cairn cannot read: {raw[:40]}")
    return _unescape_plain(s, line)


def _unescape_plain(s: str, line: int) -> str:
    probe = s.replace("$${", "").replace("%%{", "")
    if "${" in probe or "%{" in probe:
        raise HclError(f"line {line}: string templates (${{...}}) are not read in a change: write the value itself, or "
                       "var.<name> for a secret")
    return s.replace("$${", "${").replace("%%{", "%{")


class _Parser:
    def __init__(self, tokens: list):
        self.t, self.i = tokens, 0

    def peek(self, skip_nl: bool = True):
        j = self.i
        while skip_nl and j < len(self.t) and self.t[j][0] == "nl":
            j += 1
        return self.t[j] if j < len(self.t) else ("eof", None, self.t[-1][2] if self.t else 1)

    def take(self, skip_nl: bool = True):
        while skip_nl and self.i < len(self.t) and self.t[self.i][0] == "nl":
            self.i += 1
        if self.i >= len(self.t):
            raise HclError("the file ends early")
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, kind: str, value: Optional[str] = None):
        tok = self.take()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise HclError(f"line {tok[2]}: expected {value or kind}, found {tok[1]!r}")
        return tok

    def blocks(self) -> list[dict]:
        out = []
        while self.peek()[0] != "eof":
            out.append(self.block())
        return out

    def block(self) -> dict:
        kind, name, line = self.expect("ident")
        labels = []
        while self.peek()[0] == "string":
            labels.append(self.take()[1])
        self.expect("punct", "{")
        return {"type": name, "labels": labels, "line": line, **self.body()}

    def body(self) -> dict:
        attrs: dict[str, Any] = {}
        blocks: list[dict] = []
        while True:
            tok = self.peek()
            if tok[0] == "punct" and tok[1] == "}":
                self.take()
                return {"attrs": attrs, "blocks": blocks}
            if tok[0] != "ident":
                raise HclError(f"line {tok[2]}: expected an attribute, found {tok[1]!r}")
            name = self.take()[1]
            nxt = self.peek()
            if nxt[0] == "punct" and nxt[1] == "=":
                self.take()
                attrs[name] = self.expr()
            else:
                self.i -= 1
                blocks.append(self.block())

    def expr(self) -> Any:
        tok = self.take()
        kind, val, line = tok
        if kind == "string" or kind == "number":
            return val
        if kind == "punct" and val == "[":
            items = []
            while True:
                if self.peek()[0] == "punct" and self.peek()[1] == "]":
                    self.take()
                    return items
                items.append(self.expr())
                if self.peek()[0] == "punct" and self.peek()[1] == ",":
                    self.take()
        if kind == "punct" and val == "{":
            obj: dict[str, Any] = {}
            while True:
                if self.peek()[0] == "punct" and self.peek()[1] == "}":
                    self.take()
                    return obj
                k = self.take()
                if k[0] not in ("string", "ident"):
                    raise HclError(f"line {k[2]}: an object key, found {k[1]!r}")
                sep = self.take()
                if sep[0] != "punct" or sep[1] not in ("=", ":"):
                    raise HclError(f"line {sep[2]}: expected = after {k[1]}")
                obj[str(k[1])] = self.expr()
                if self.peek()[0] == "punct" and self.peek()[1] == ",":
                    self.take()
        if kind == "ident":
            if val in ("true", "false"):
                return val == "true"
            if val == "null":
                return None
            nxt = self.peek(skip_nl=False)
            if nxt[0] == "punct" and nxt[1] == "(":
                if val != "jsonencode":
                    raise HclError(f"line {line}: the function {val}() is not read in a change (only jsonencode)")
                self.take()
                inner = self.expr()
                self.expect("punct", ")")
                return inner
            parts = [val]
            while self.peek(skip_nl=False)[0] == "punct" and self.peek(skip_nl=False)[1] == ".":
                self.take()
                parts.append(self.expect("ident")[1])
            return _Ref(parts, line)
        raise HclError(f"line {line}: expected a value, found {val!r}")


def parse(files: dict[str, str]) -> list[dict]:
    """Every block of the files: {type, labels, attrs, blocks, line, file}."""
    out = []
    for name, text in sorted(files.items()):
        if not name.endswith(".tf"):
            raise HclError(f"{name}: Terraform files end in .tf")
        try:
            for b in _Parser(_lex(text)).blocks():
                out.append({**b, "file": name})
        except HclError as e:
            raise HclError(f"{name}: {e}")
    return out


def to_bundle(files: dict[str, str], name: str = "change") -> dict:
    """The Terraform files as a bundle document (a mapping, as ``cairn bundle`` reads it)."""
    blocks = parse(files)
    by_tf = {k.tf: k for k in registry.kinds()}
    resources: dict[tuple[str, str], dict] = {}
    for b in blocks:
        where = f"{b['file']} line {b['line']}"
        if b["type"] in SKIPPED:
            continue
        if b["type"] != "resource":
            raise HclError(f"{where}: {b['type']} blocks are not read in a change (only resources)")
        if len(b["labels"]) != 2 or not b["labels"][0].startswith("cairn_"):
            raise HclError(f"{where}: a resource is resource \"cairn_<type>\" \"<name>\"")
        tf, label = b["labels"][0][len("cairn_"):], b["labels"][1]
        if tf not in by_tf:
            raise HclError(f"{where}: Cairn has no resource type cairn_{tf}")
        if b["blocks"]:
            raise HclError(f"{where}: nested blocks ({b['blocks'][0]['type']}) are not read in a change")
        for meta in ("count", "for_each", "depends_on", "lifecycle", "provider"):
            if meta in b["attrs"]:
                raise HclError(f"{where}: {meta} is not read in a change")
        resources[(tf, label)] = {"kind": by_tf[tf], "attrs": b["attrs"], "where": where}
    doc: dict[str, Any] = {"bundle": name}

    def value(v: Any, where: str, field: Optional[registry.Field]) -> Any:
        if isinstance(v, _Ref):
            if v.parts[0] == "var" and len(v.parts) == 2:
                return "${" + v.parts[1] + "}"
            if v.parts[0].startswith("cairn_") and len(v.parts) == 3:
                key = (v.parts[0][len("cairn_"):], v.parts[1])
                target = resources.get(key)
                if target is None:
                    raise HclError(f"{where}: {'.'.join(v.parts)} names no resource in these files")
                k = target["kind"]
                attr = v.parts[2]
                if attr == "id" and k.key:                 # a bundle names a resource by its key
                    return "/".join(str(plain(target["attrs"].get(k.field(f).tf_name), where)) for f in k.key)
                f = next((x for x in k.fields if x.tf_name == attr), None)
                if f is None or f.tf_name not in target["attrs"]:
                    raise HclError(f"{where}: {'.'.join(v.parts)} is not set in those files")
                return plain(target["attrs"][f.tf_name], where)
            raise HclError(f"line {v.line}: {'.'.join(v.parts)} is not read in a change (var.<name> or another "
                           "resource's attribute)")
        if isinstance(v, list):
            return [value(x, where, None) for x in v]
        if isinstance(v, dict):
            return {k: value(x, where, None) for k, x in v.items()}
        return v

    def plain(v: Any, where: str) -> Any:
        if isinstance(v, _Ref):
            raise HclError(f"{where}: a key made of a reference ({'.'.join(v.parts)}) is not read in a change")
        return v

    for (tf, _label), r in resources.items():
        k = r["kind"]
        spec: dict[str, Any] = {}
        by_tf_name = {f.tf_name: f for f in k.fields}
        for attr, v in r["attrs"].items():
            f = by_tf_name.get(attr)
            if f is None:
                raise HclError(f"{r['where']}: cairn_{tf} has no attribute {attr}")
            spec[f.name] = value(v, r["where"], f)
        doc.setdefault(k.name, []).append(spec)
    return doc
