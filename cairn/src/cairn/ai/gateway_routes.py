"""Dynamic model routing for Cairn's AI gateway (L113).

A **route** is a virtual model name a caller puts in ``model`` (``"model": "cairn-auto"``) that maps to an ordered
list of real models, the **candidates**, with rules:

* **capabilities**: a candidate lists what it supports (``tools``, ``vision``); a request using tools or carrying
  images skips the candidates that lack them;
* **input size**: a candidate's ``max_input_tokens``; a larger request (estimated at four characters a token) skips it;
* **cost ceiling**: the route's ``max_cost_usd``; a candidate whose estimated cost for the request (its input
  estimate plus ``max_tokens`` of output, at Cairn's prices, ``CAIRN_AI_PRICES``) is higher is skipped;
* **strategy**: ``ordered`` (the operator's order is the preference) or ``cheapest`` (by that estimate);
* **latency budget**: the route's ``latency_budget_ms`` across every attempt, and a candidate's own ``timeout_s``;
* **the caller's permissions**: a candidate the caller may not use (``USE_AI_GATEWAY`` on ``urn:li:aiModel:<model>``)
  is skipped; with none left the call is refused (403), before any provider is reached.

The gateway tries the remaining candidates in turn and **falls back** to the next on a provider error (5xx), a
timeout, an unreachable provider or a rate limit (429); any other answer (a 400 from the provider, say) is the
answer. The call is metered on the model that answered, the response says which (``X-Cairn-Model``,
``X-Cairn-Route``, and the provider's own ``model`` field), and each routed call is audited
(``gateway.routed``: route, model, every attempt's outcome and time; no content).

``cairn-auto`` is built in (every model the gateway serves, in ``CAIRN_AI_GATEWAY_MODELS`` order) until an operator
defines a route of that name. Operators (``MANAGE_SYSTEM_OPERATIONS``) define routes at
``PUT /v1/ai/gateway/routes/{name}`` (audited ``gateway.route_saved``, ``gateway.route_deleted``).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, delete, insert, select, update

from cairn.common.db import JSONText, metadata

gateway_routes = Table(
    "cairn_ai_gateway_routes", metadata,
    Column("name", String(64), primary_key=True),
    Column("description", Text, nullable=False),
    Column("spec", JSONText, nullable=False),          # candidates, strategy, max_cost_usd, latency_budget_ms
    Column("created_by", String(512), nullable=False),
    Column("updated_by", String(512), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

AUTO = "cairn-auto"
NAME = re.compile(r"^[a-z][a-z0-9._-]{1,62}$")
CAPABILITIES = ("tools", "vision")
STRATEGIES = ("ordered", "cheapest")
MAX_CANDIDATES = 10


class RouteError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def check_spec(name: str, body: dict, served: list[str]) -> dict:
    """A route's spec, validated: candidates must be models the gateway serves."""
    if not NAME.match(name or ""):
        raise RouteError("name: lowercase letters, digits and . _ -, up to 63")
    if name in served:
        raise RouteError(f"{name} is a model the gateway serves; a route needs a name of its own")
    if not isinstance(body, dict):
        raise RouteError("a JSON object")
    unknown = sorted(set(body) - {"description", "candidates", "strategy", "max_cost_usd", "latency_budget_ms"})
    if unknown:
        raise RouteError(f"unknown fields {unknown}")
    cands = body.get("candidates")
    if not isinstance(cands, list) or not 1 <= len(cands) <= MAX_CANDIDATES:
        raise RouteError(f"candidates: 1 to {MAX_CANDIDATES} models, in order of preference")
    out = []
    for c in cands:
        c = {"model": c} if isinstance(c, str) else c
        if not isinstance(c, dict) or not isinstance(c.get("model"), str):
            raise RouteError("each candidate: a model name, or {model, capabilities, max_input_tokens, timeout_s}")
        extra = sorted(set(c) - {"model", "capabilities", "max_input_tokens", "timeout_s"})
        if extra:
            raise RouteError(f"candidate {c['model']}: unknown fields {extra}")
        if c["model"] not in served:
            raise RouteError(f"candidate {c['model']} is not a model the gateway serves "
                             f"({', '.join(served) or 'none'})")
        caps = c.get("capabilities", list(CAPABILITIES))
        if not isinstance(caps, list) or not set(caps) <= set(CAPABILITIES):
            raise RouteError(f"candidate {c['model']}: capabilities, some of {list(CAPABILITIES)}")
        cand: dict[str, Any] = {"model": c["model"], "capabilities": [x for x in CAPABILITIES if x in caps]}
        mit = c.get("max_input_tokens")
        if mit is not None:
            if isinstance(mit, bool) or not isinstance(mit, int) or not 1 <= mit <= 10_000_000:
                raise RouteError(f"candidate {c['model']}: max_input_tokens, a positive number")
            cand["max_input_tokens"] = mit
        t = c.get("timeout_s")
        if t is not None:
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0.1 <= t <= 600:
                raise RouteError(f"candidate {c['model']}: timeout_s, 0.1 to 600")
            cand["timeout_s"] = float(t)
        out.append(cand)
    if len({c["model"] for c in out}) != len(out):
        raise RouteError("a model appears twice among the candidates")
    spec: dict[str, Any] = {"candidates": out, "strategy": body.get("strategy") or "ordered"}
    if spec["strategy"] not in STRATEGIES:
        raise RouteError(f"strategy: one of {list(STRATEGIES)}")
    mc = body.get("max_cost_usd")
    if mc is not None:
        if isinstance(mc, bool) or not isinstance(mc, (int, float)) or not 0 < mc <= 1000:
            raise RouteError("max_cost_usd: the most one call may cost, in USD (above 0)")
        spec["max_cost_usd"] = float(mc)
    lb = body.get("latency_budget_ms")
    if lb is not None:
        if isinstance(lb, bool) or not isinstance(lb, int) or not 100 <= lb <= 600_000:
            raise RouteError("latency_budget_ms: 100 to 600000")
        spec["latency_budget_ms"] = lb
    desc = body.get("description") or ""
    if not isinstance(desc, str) or len(desc) > 1000:
        raise RouteError("description: text, up to 1000 characters")
    spec["description"] = desc
    return spec


class GatewayRoutes:
    """The operators' routes, kept in the database."""

    def __init__(self, engine, audit=None):
        self.engine, self.audit = engine, audit

    def get(self, name: str) -> Optional[dict]:
        with self.engine.connect() as c:
            r = c.execute(select(gateway_routes).where(gateway_routes.c.name == name)).first()
        return None if r is None else self._public(r)

    @staticmethod
    def _public(r) -> dict:
        return {"name": r.name, **{k: v for k, v in r.spec.items() if k != "description"},
                "description": r.description, "builtin": False, "updated_by": r.updated_by,
                "updated_ms": r.updated_ms}

    def list(self) -> list[dict]:
        with self.engine.connect() as c:
            return [self._public(r) for r in c.execute(select(gateway_routes).order_by(gateway_routes.c.name))]

    def save(self, actor: str, name: str, body: dict, served: list[str]) -> dict:
        spec = check_spec(name, body, served)
        now = _now()
        with self.engine.begin() as c:
            cur = c.execute(select(gateway_routes.c.name).where(gateway_routes.c.name == name)).first()
            values = {"description": spec["description"], "spec": spec, "updated_by": actor, "updated_ms": now}
            if cur is None:
                c.execute(insert(gateway_routes).values(name=name, created_by=actor, **values))
            else:
                c.execute(update(gateway_routes).where(gateway_routes.c.name == name).values(**values))
            if self.audit is not None:
                self.audit.record("ai", "gateway.route_saved", actor, f"urn:li:aiModel:{name}",
                                  {"candidates": [x["model"] for x in spec["candidates"]],
                                   "strategy": spec["strategy"], "max_cost_usd": spec.get("max_cost_usd"),
                                   "latency_budget_ms": spec.get("latency_budget_ms"), "created": cur is None},
                                  conn=c)
        return self.get(name)

    def delete(self, actor: str, name: str) -> None:
        with self.engine.begin() as c:
            if c.execute(delete(gateway_routes).where(gateway_routes.c.name == name)).rowcount == 0:
                raise RouteError("no such route", 404)
            if self.audit is not None:
                self.audit.record("ai", "gateway.route_deleted", actor, f"urn:li:aiModel:{name}", {}, conn=c)


def builtin_auto(served: list[str]) -> dict:
    return {"name": AUTO, "candidates": [{"model": m, "capabilities": list(CAPABILITIES)} for m in served],
            "strategy": "ordered", "description": "Every model the gateway serves, in the operator's order, falling "
            "back to the next on errors, timeouts and rate limits", "builtin": True}


# ---- what a request needs ----------------------------------------------------------------------
def request_needs(provider: str, body: dict) -> dict:
    """The request's estimated input tokens, output tokens and the capabilities it uses."""
    text = json.dumps({k: body.get(k) for k in ("system", "messages", "tools")}, default=str)
    caps = set()
    if body.get("tools"):
        caps.add("tools")
    image = '"type": "image"' if provider == "anthropic" else '"type": "image_url"'
    if image in text or '"type": "input_image"' in text:
        caps.add("vision")
    out = body.get("max_tokens") or body.get("max_completion_tokens") or 1024
    try:
        out = int(out)
    except (TypeError, ValueError):
        out = 1024
    return {"input_tokens": max(1, len(text) // 4), "output_tokens": max(1, out), "capabilities": caps}


def plan(route: dict, needs: dict, may_use, provider_of, cost_of) -> tuple[list[dict], list[dict]]:
    """(candidates to try in order, skipped with the reason). ``provider_of(model)``: the protocol its upstream
    speaks, or None when no upstream serves it; ``cost_of(model)``: the request's estimated cost on it."""
    keep, skipped = [], []
    for c in route["candidates"]:
        m = c["model"]
        why = None
        if provider_of(m) is None:
            why = "no upstream serves it on this protocol"
        elif not may_use(m):
            why = "not permitted"
        elif not needs["capabilities"] <= set(c.get("capabilities", CAPABILITIES)):
            why = f"lacks {', '.join(sorted(needs['capabilities'] - set(c.get('capabilities', []))))}"
        elif c.get("max_input_tokens") and needs["input_tokens"] > c["max_input_tokens"]:
            why = f"input of about {needs['input_tokens']} tokens is over its {c['max_input_tokens']}"
        elif route.get("max_cost_usd") is not None and cost_of(m) > route["max_cost_usd"]:
            why = f"estimated ${cost_of(m):.4f} is over the route's ${route['max_cost_usd']}"
        if why:
            skipped.append({"model": m, "skipped": why})
        else:
            keep.append(c)
    if route.get("strategy") == "cheapest":
        keep.sort(key=lambda c: cost_of(c["model"]))
    return keep, skipped
