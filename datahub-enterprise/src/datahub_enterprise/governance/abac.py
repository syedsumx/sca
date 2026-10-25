"""Attribute-Based Access Control: policy model and evaluator.

A policy matches when (a) the requested privilege matches one of ``actions`` (glob
patterns), (b) the ``target`` pre-filter matches, and (c) the ``condition`` tree
evaluates to true. Combining is **deny-overrides** (see :mod:`pdp`).

Condition grammar::

    condition := {"all": [condition...]} | {"any": [condition...]} | {"not": condition} | leaf
    leaf      := {"attr": "<path>", "op": "<operator>", "value": <literal | {"ref": "<path>"}>}

Paths address the request context: ``subject.urn``, ``subject.groups``,
``subject.attributes.<k>``, ``resource.type``, ``resource.tags``,
``resource.classification``, ``resource.structured_properties.<qualifiedName>``,
``environment.ip``, ``environment.time`` …
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from datetime import datetime, time
from functools import lru_cache
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

OPERATORS = {
    "eq", "neq", "in", "not_in", "contains", "contains_any", "contains_all", "starts_with",
    "matches", "gt", "gte", "lt", "lte", "exists", "not_exists", "time_between", "cidr",
}

_MISSING = object()


class Obligation(BaseModel):
    type: Literal["mask_columns", "require_justification", "watermark", "row_filter", "notify", "max_rows"]
    params: dict[str, Any] = Field(default_factory=dict)


class Target(BaseModel):
    resource_types: list[str] = Field(default_factory=list)
    resource_urns: list[str] = Field(default_factory=list, description="glob patterns")
    subjects: list[str] = Field(default_factory=list, description="user/group urns, or '*'")


class Policy(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:\-]{1,128}$")
    description: str = ""
    effect: Literal["allow", "deny"]
    priority: int = 0
    actions: list[str] = Field(min_length=1)
    target: Target = Field(default_factory=Target)
    condition: Optional[dict[str, Any]] = None
    obligations: list[Obligation] = Field(default_factory=list)
    enabled: bool = True
    source: Literal["dhe", "datahub-native", "rbac"] = "dhe"

    @field_validator("condition")
    @classmethod
    def _validate_condition(cls, v):
        if v is not None:
            validate_condition(v)
        return v


class PolicyBundle(BaseModel):
    policies: list[Policy] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "PolicyBundle":
        seen: set[str] = set()
        for p in self.policies:
            if p.id in seen:
                raise ValueError(f"duplicate policy id {p.id!r}")
            seen.add(p.id)
        return self


def validate_condition(node: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("condition nesting too deep")
    if not isinstance(node, dict):
        raise ValueError(f"condition node must be an object, got {type(node).__name__}")
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        children = node[key]
        if not isinstance(children, list) or not children:
            raise ValueError(f"'{key}' requires a non-empty list")
        for c in children:
            validate_condition(c, depth + 1)
        return
    if "not" in node:
        validate_condition(node["not"], depth + 1)
        return
    op = node.get("op")
    if "attr" not in node or op not in OPERATORS:
        raise ValueError(f"leaf requires 'attr' and an 'op' in {sorted(OPERATORS)}; got {node}")
    if op == "matches":
        _compile_safe_regex(str(node.get("value", "")))
    if op == "cidr":
        values = node.get("value")
        for v in values if isinstance(values, list) else [values]:
            ipaddress.ip_network(str(v), strict=False)
    if op == "time_between":
        v = node.get("value")
        if not (isinstance(v, list) and len(v) == 2):
            raise ValueError("time_between requires [\"HH:MM\", \"HH:MM\"]")
        for t in v:
            time.fromisoformat(t)


_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")


@lru_cache(maxsize=512)
def _compile_safe_regex(pattern: str) -> re.Pattern:
    # Policies are authored by admins, but still guard the PDP against catastrophic
    # backtracking: bounded length and no nested quantifiers like (a+)+.
    if len(pattern) > 256:
        raise ValueError("regex too long (max 256)")
    if _NESTED_QUANTIFIER.search(pattern):
        raise ValueError("regex with nested quantifiers is not allowed")
    return re.compile(pattern)


def resolve_path(ctx: dict, path: str) -> Any:
    """Dotted-path lookup. Keys may themselves contain dots (structured property names
    such as ``io.dhe.department``), so at each level the longest matching key wins."""
    cur: Any = ctx
    parts = path.split(".")
    i = 0
    while i < len(parts):
        if not isinstance(cur, dict):
            return _MISSING
        for j in range(len(parts), i, -1):
            key = ".".join(parts[i:j])
            if key in cur:
                cur = cur[key]
                i = j
                break
        else:
            return _MISSING
    return cur


def _as_list(v: Any) -> list:
    if v is _MISSING or v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _norm(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


def _cmp(a: Any, b: Any, op: str) -> bool:
    try:
        a_f, b_f = float(a), float(b)
    except (TypeError, ValueError):
        a_f, b_f = a, b  # type: ignore[assignment]
    try:
        return {"gt": a_f > b_f, "gte": a_f >= b_f, "lt": a_f < b_f, "lte": a_f <= b_f}[op]
    except TypeError:
        return False


def evaluate_leaf(node: dict, ctx: dict) -> bool:
    actual = resolve_path(ctx, node["attr"])
    op = node["op"]
    expected = node.get("value")
    if isinstance(expected, dict) and "ref" in expected:
        expected = resolve_path(ctx, expected["ref"])
        if expected is _MISSING:
            return False
    if op == "exists":
        return actual is not _MISSING and actual not in (None, [], "")
    if op == "not_exists":
        return actual is _MISSING or actual in (None, [], "")
    if actual is _MISSING:
        # Missing attributes never satisfy a positive test, but do satisfy negative ones.
        return op in ("neq", "not_in")

    actual_l = [_norm(x) for x in _as_list(actual)]
    expected_l = [_norm(x) for x in _as_list(expected)]
    if op == "eq":
        return _norm(actual) == _norm(expected) if not isinstance(actual, list) else actual_l == expected_l
    if op == "neq":
        return _norm(actual) != _norm(expected) if not isinstance(actual, list) else actual_l != expected_l
    if op == "in":
        return bool(actual_l) and all(a in expected_l for a in actual_l)
    if op == "not_in":
        return not any(a in expected_l for a in actual_l)
    if op == "contains":
        if isinstance(actual, str):
            return str(_norm(expected)) in actual.lower()
        return _norm(expected) in actual_l
    if op == "contains_any":
        return any(e in actual_l for e in expected_l)
    if op == "contains_all":
        return all(e in actual_l for e in expected_l)
    if op == "starts_with":
        return any(isinstance(a, str) and a.startswith(str(expected).lower()) for a in actual_l)
    if op == "matches":
        rx = _compile_safe_regex(str(node["value"]))
        return any(isinstance(a, str) and rx.fullmatch(a) for a in _as_list(actual))
    if op in ("gt", "gte", "lt", "lte"):
        return _cmp(actual, expected, op)
    if op == "time_between":
        start, end = (time.fromisoformat(t) for t in node["value"])
        t = actual if isinstance(actual, time) else datetime.fromisoformat(str(actual)).time()
        t = t.replace(tzinfo=None)
        return start <= t <= end if start <= end else (t >= start or t <= end)
    if op == "cidr":
        try:
            ip = ipaddress.ip_address(str(actual))
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(str(n), strict=False) for n in _as_list(node["value"]))
    raise ValueError(f"unknown operator {op}")


def evaluate_condition(node: Optional[dict], ctx: dict, trace: Optional[list] = None) -> bool:
    if node is None:
        return True
    if "all" in node:
        return all(evaluate_condition(c, ctx, trace) for c in node["all"])
    if "any" in node:
        return any(evaluate_condition(c, ctx, trace) for c in node["any"])
    if "not" in node:
        return not evaluate_condition(node["not"], ctx, trace)
    result = evaluate_leaf(node, ctx)
    if trace is not None:
        actual = resolve_path(ctx, node["attr"])
        trace.append({"attr": node["attr"], "op": node["op"], "value": node.get("value"),
                      "actual": None if actual is _MISSING else actual, "result": result})
    return result


def action_matches(patterns: list[str], privilege: str) -> bool:
    return any(fnmatch.fnmatchcase(privilege, p) for p in patterns)


def target_matches(target: Target, ctx: dict) -> tuple[bool, str]:
    res = ctx.get("resource") or {}
    subj = ctx.get("subject") or {}
    if target.resource_types and (res.get("type") or "").lower() not in {t.lower() for t in target.resource_types}:
        return False, f"resource type {res.get('type')!r} not in {target.resource_types}"
    if target.resource_urns and not any(fnmatch.fnmatchcase(res.get("urn") or "", p) for p in target.resource_urns):
        return False, "resource urn does not match target patterns"
    if target.subjects and "*" not in target.subjects:
        ids = {subj.get("urn")} | set(subj.get("groups") or []) | set(subj.get("roles") or [])
        if not ids.intersection(target.subjects):
            return False, "subject not in target"
    return True, "target matched"


class RuleResult(BaseModel):
    policy_id: str
    effect: str
    matched: bool
    reason: str
    priority: int = 0
    source: str = "dhe"
    details: Optional[list[dict]] = None


def evaluate_policy(policy: Policy, privilege: str, ctx: dict, explain: bool = False) -> RuleResult:
    base = {"policy_id": policy.id, "effect": policy.effect, "priority": policy.priority, "source": policy.source}
    if not policy.enabled:
        return RuleResult(**base, matched=False, reason="disabled")
    if not action_matches(policy.actions, privilege):
        return RuleResult(**base, matched=False, reason=f"action {privilege} not in {policy.actions}")
    ok, why = target_matches(policy.target, ctx)
    if not ok:
        return RuleResult(**base, matched=False, reason=why)
    trace: Optional[list] = [] if explain else None
    matched = evaluate_condition(policy.condition, ctx, trace)
    return RuleResult(**base, matched=matched, reason="condition matched" if matched else "condition not satisfied",
                      details=trace)


ConditionNode = Union[dict, None]
