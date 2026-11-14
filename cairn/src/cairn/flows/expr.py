"""Expressions and templates in flow definitions: data only, never code.

A branch's ``condition``, a for-each's ``items`` and any ``{{ ... }}`` inside a step's text fields are
expressions over what the run knows:

* ``params.<name>``: the run's parameters (typed, defaults applied);
* ``steps.<name>.<field>``: an earlier step's output (``steps.count.rows``, ``steps.triage.output``), and
  ``steps.<name>.status``;
* ``run.id``, ``run.logical_date`` (``YYYY-MM-DD``), ``run.trigger``, ``flow.name``;
* inside a for-each: ``item`` and ``index``.

The language is a small subset of Python's expression syntax, parsed with :mod:`ast` and walked here (never
``eval``): literals, lists, ``and or not``, comparisons (``== != < <= > >= in``), ``+ - * / %``, field access
on mappings (``a.b`` or ``a["b"]``), indexing lists, and the functions ``len int float str bool round abs min
max lower upper``. Anything else is refused when the flow is validated, with the position.

A template that is exactly one ``{{ expr }}`` keeps the value's type (a list for ``items``); otherwise each
``{{ expr }}`` is replaced by its value as text.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

MAX_EXPR = 1000
TEMPLATE = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.S)
FUNCS = {
    "len": len, "int": int, "float": float, "str": str, "bool": bool, "round": round, "abs": abs,
    "min": min, "max": max, "lower": lambda s: str(s).lower(), "upper": lambda s: str(s).upper(),
}
ROOTS = ("params", "steps", "run", "flow", "item", "index")
_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b, ast.Mod: lambda a, b: a % b}
_CMP = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


class ExprError(ValueError):
    pass


STEP_REF = re.compile(r"\bsteps\.([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)+)")


def _prep(text: str) -> str:
    """Step names may hold '-' (``steps.full-report.rows``): such a reference is read as ``steps["full-report"]``.
    Subtraction after a step's field is written with spaces (``steps.a.rows - 1``)."""
    out, quote, i = [], None, 0
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1:i + 2])
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            out.append(c)
        else:
            word_start = i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
            m = STEP_REF.match(text, i) if word_start and text.startswith("steps.", i) else None
            if m:
                out.append(f'steps["{m.group(1)}"]')
                i = m.end()
                continue
            out.append(c)
        i += 1
    return "".join(out)


def parse(text: str) -> ast.AST:
    """The expression's tree, or ExprError naming what is not allowed."""
    if not isinstance(text, str) or not text.strip():
        raise ExprError("an empty expression")
    if len(text) > MAX_EXPR:
        raise ExprError(f"an expression is at most {MAX_EXPR} characters")
    try:
        tree = ast.parse(_prep(text.strip()), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"{text.strip()!r} is not an expression: {e.msg} (column {e.offset})")
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
                               ast.BinOp, ast.Compare, ast.Constant, ast.Name, ast.Attribute, ast.Subscript,
                               ast.List, ast.Tuple, ast.Load, ast.Call, *_BIN, *_CMP))
        if not ok:
            raise ExprError(f"{type(node).__name__} is not allowed in {text.strip()!r}")
        if isinstance(node, ast.Name) and node.id not in ROOTS and node.id not in FUNCS \
                and node.id not in ("True", "False", "None", "true", "false", "null"):
            raise ExprError(f"unknown name {node.id!r} in {text.strip()!r} (use params., steps., run., flow., item)")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords):
            raise ExprError(f"only the functions {', '.join(FUNCS)} may be called, in {text.strip()!r}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExprError(f"no field {node.attr!r} in {text.strip()!r}")
    return tree


def references(text: str) -> set[str]:
    """The step names an expression reads (``steps.<name>``)."""
    out = set()
    for node in ast.walk(parse(text)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "steps":
            out.add(node.attr)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "steps" \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            out.add(node.slice.value)
    return out


def _field(obj: Any, key: Any, text: str) -> Any:
    if isinstance(obj, dict):
        if key not in obj:
            raise ExprError(f"no {key!r} in {text!r} (it has {', '.join(map(str, list(obj)[:12])) or 'nothing'})")
        return obj[key]
    if isinstance(obj, (list, tuple, str)) and isinstance(key, int) and not isinstance(key, bool):
        try:
            return obj[key]
        except IndexError:
            raise ExprError(f"index {key} is out of range in {text!r}")
    raise ExprError(f"cannot take {key!r} of {type(obj).__name__} in {text!r}")


def evaluate(text: str, ctx: dict) -> Any:
    """The expression's value in ``ctx`` (a mapping of the roots: params, steps, run, flow, item, index)."""
    tree = parse(text)
    src = text.strip()

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in ("True", "true"):
                return True
            if n.id in ("False", "false"):
                return False
            if n.id in ("None", "null"):
                return None
            if n.id in FUNCS:
                raise ExprError(f"{n.id} is a function: call it, in {src!r}")
            if n.id not in ctx:
                raise ExprError(f"{n.id} is not known here, in {src!r}")
            return ctx[n.id]
        if isinstance(n, ast.Attribute):
            return _field(ev(n.value), n.attr, src)
        if isinstance(n, ast.Subscript):
            return _field(ev(n.value), ev(n.slice), src)
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(x) for x in n.elts]
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                out: Any = True
                for v in n.values:
                    out = ev(v)
                    if not out:
                        return out
                return out
            out = False
            for v in n.values:
                out = ev(v)
                if out:
                    return out
            return out
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.Not):
                return not v
            try:
                return -v if isinstance(n.op, ast.USub) else +v
            except TypeError:
                raise ExprError(f"cannot negate {type(v).__name__} in {src!r}")
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            try:
                out = _BIN[type(n.op)](a, b)
            except (TypeError, ZeroDivisionError) as e:
                raise ExprError(f"{src!r}: {e}")
            if isinstance(out, (str, list)) and len(out) > 1_000_000:
                raise ExprError(f"{src!r} makes a value over a million long")
            return out
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, right_n in zip(n.ops, n.comparators):
                right = ev(right_n)
                try:
                    if not _CMP[type(op)](left, right):
                        return False
                except TypeError:
                    raise ExprError(f"cannot compare {type(left).__name__} with {type(right).__name__} in {src!r}")
                left = right
            return True
        if isinstance(n, ast.Call):
            args = [ev(a) for a in n.args]
            try:
                return FUNCS[n.func.id](*args)  # type: ignore[union-attr]
            except (TypeError, ValueError) as e:
                raise ExprError(f"{src!r}: {e}")
        raise ExprError(f"{type(n).__name__} is not allowed in {src!r}")

    return ev(tree)


def check_template(text: Any) -> list[str]:
    """The problems with each ``{{ }}`` in a text field (empty when it is fine)."""
    if not isinstance(text, str):
        return []
    out = []
    for m in TEMPLATE.finditer(text):
        try:
            parse(m.group(1))
        except ExprError as e:
            out.append(str(e))
    return out


def template_refs(value: Any) -> set[str]:
    """Steps named by the templates anywhere in a value (strings, lists, mappings)."""
    if isinstance(value, str):
        refs: set[str] = set()
        for m in TEMPLATE.finditer(value):
            try:
                refs |= references(m.group(1))
            except ExprError:
                pass
        return refs
    if isinstance(value, list):
        return set().union(*(template_refs(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(template_refs(v) for v in value.values())) if value else set()
    return set()


def render(value: Any, ctx: dict) -> Any:
    """``value`` with its templates filled in (recursively through lists and mappings)."""
    if isinstance(value, str):
        whole = TEMPLATE.fullmatch(value.strip())
        if whole and value.strip() == value.strip() and value.strip().startswith("{{") \
                and value.count("{{") == 1:
            return evaluate(whole.group(1), ctx)

        def one(m):
            v = evaluate(m.group(1), ctx)
            return v if isinstance(v, str) else json.dumps(v, default=str)
        return TEMPLATE.sub(one, value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value
