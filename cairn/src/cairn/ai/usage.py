"""AI usage metering, cost estimates and budgets.

Every model call made on behalf of a user goes through :class:`MeteredProvider`, which

* refuses the call with :class:`LLMBudgetExceeded` (an :class:`LLMError`, so every feature falls
  back to its non-AI path) when the deployment's daily spend or the caller's daily token
  allowance is used up, and
* records who asked, for which feature, on which model, the tokens used and an estimated cost
  in ``cairn_ai_usage``, and writes the same facts to the audit log.

The caller and the feature are carried in context variables (:func:`set_actor`, :func:`llm_task`)
rather than on shared objects, so concurrent requests never attribute usage to each other.

Prices are list prices in USD per million tokens; ``CAIRN_AI_PRICES`` (JSON object of
``{"model-prefix": [input, output]}``) overrides or extends them. Self-hosted models cost 0.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import threading
import time
from typing import Any, Iterator, Optional

from sqlalchemy import BigInteger, Column, Float, Integer, String, Table, func, select
from sqlalchemy.engine import Engine

from cairn.common import metrics
from cairn.ai.llm import LLMError, LLMUsage
from cairn.common.db import metadata

logger = logging.getLogger(__name__)

usage_table = Table(
    "cairn_ai_usage", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts_ms", BigInteger, nullable=False, index=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("task", String(64), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("model", String(128), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_read_tokens", Integer, nullable=False, default=0),
    Column("cache_write_tokens", Integer, nullable=False, default=0),
    Column("cost_usd", Float, nullable=False),
)

# USD per million tokens (input, output), longest matching prefix wins.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
FREE_PROVIDERS = {"ollama", "static"}
CACHE_READ_FACTOR, CACHE_WRITE_FACTOR = 0.1, 1.25    # of the input price (5-minute cache)
DAY_MS = 86_400_000

_actor: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cairn_ai_actor", default=None)
_task: contextvars.ContextVar[str] = contextvars.ContextVar("cairn_ai_task", default="ai")


class LLMBudgetExceeded(LLMError):
    """The daily AI budget (deployment-wide or per user) is used up."""


@contextlib.contextmanager
def llm_task(name: str) -> Iterator[None]:
    """Attribute model calls made inside the block to feature ``name`` (e.g. ``nlq.plan``)."""
    token = _task.set(name)
    try:
        yield
    finally:
        _task.reset(token)


def set_actor(actor: Optional[str]) -> None:
    """Attribute later model calls in this request (context) to ``actor``."""
    _actor.set(actor)


def current_actor() -> str:
    return _actor.get() or "unknown"


def current_task() -> str:
    return _task.get()


def load_prices(raw: Optional[str] = None) -> dict[str, tuple[float, float]]:
    prices = dict(DEFAULT_PRICES)
    raw = raw if raw is not None else os.environ.get("CAIRN_AI_PRICES")
    if raw:
        try:
            for k, v in json.loads(raw).items():
                prices[str(k)] = (float(v[0]), float(v[1]))
        except (ValueError, TypeError, IndexError, AttributeError) as e:
            raise ValueError(f"CAIRN_AI_PRICES must map model prefixes to [input, output] USD/MTok: {e}") from e
    return prices


def estimate_cost(model: str, input_tokens: int, output_tokens: int, provider: str = "anthropic",
                  prices: Optional[dict[str, tuple[float, float]]] = None, cache_read_tokens: int = 0,
                  cache_write_tokens: int = 0) -> float:
    if provider in FREE_PROVIDERS:
        return 0.0
    prices = prices if prices is not None else DEFAULT_PRICES
    match = max((p for p in prices if model.startswith(p)), key=len, default=None)
    if match is None:
        return 0.0
    pin, pout = prices[match]
    billed_input = input_tokens + cache_read_tokens * CACHE_READ_FACTOR + cache_write_tokens * CACHE_WRITE_FACTOR
    return round((billed_input * pin + output_tokens * pout) / 1_000_000, 6)


def _float_env(name: str) -> Optional[float]:
    v = os.environ.get(name, "").strip()
    return float(v) if v else None


class UsageMeter:
    def __init__(self, engine: Engine, audit=None, daily_budget_usd: Optional[float] = None,
                 user_daily_tokens: Optional[int] = None, prices: Optional[dict] = None, clock=time.time):
        self.engine = engine
        self.audit = audit
        self.daily_budget_usd = daily_budget_usd
        self.user_daily_tokens = user_daily_tokens
        self.prices = prices if prices is not None else load_prices()
        self.clock = clock
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, engine: Engine, audit=None) -> "UsageMeter":
        tokens = _float_env("CAIRN_AI_USER_DAILY_TOKENS")
        return cls(engine, audit, _float_env("CAIRN_AI_DAILY_BUDGET_USD"), int(tokens) if tokens else None)

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _day_start(self) -> int:
        now = self._now_ms()
        return now - now % DAY_MS

    def spent_today(self) -> float:
        with self.engine.connect() as conn:
            row = conn.execute(select(func.coalesce(func.sum(usage_table.c.cost_usd), 0.0))
                               .where(usage_table.c.ts_ms >= self._day_start())).first()
        return float(row[0])

    def tokens_today(self, actor: str) -> int:
        with self.engine.connect() as conn:
            row = conn.execute(select(func.coalesce(func.sum(usage_table.c.input_tokens + usage_table.c.output_tokens),
                                                    0)).where(usage_table.c.ts_ms >= self._day_start(),
                                                              usage_table.c.actor == actor)).first()
        return int(row[0])

    def check(self, actor: str) -> None:
        if self.daily_budget_usd is not None:
            spent = self.spent_today()
            if spent >= self.daily_budget_usd:
                metrics.AI_BUDGET_REFUSALS.inc()
                raise LLMBudgetExceeded(f"daily AI budget of ${self.daily_budget_usd:.2f} reached "
                                        f"(${spent:.2f} spent today)")
        if self.user_daily_tokens is not None and actor != "unknown":
            used = self.tokens_today(actor)
            if used >= self.user_daily_tokens:
                metrics.AI_BUDGET_REFUSALS.inc()
                raise LLMBudgetExceeded(f"daily AI allowance of {self.user_daily_tokens} tokens reached for {actor}")

    def record(self, actor: str, task: str, provider: str, usage: LLMUsage) -> float:
        cost = estimate_cost(usage.model or "", usage.input_tokens, usage.output_tokens, provider, self.prices,
                             usage.cache_read_tokens, usage.cache_write_tokens)
        with self.engine.begin() as conn:
            conn.execute(usage_table.insert().values(
                ts_ms=self._now_ms(), actor=actor, task=task[:64], provider=provider[:32],
                model=(usage.model or "unknown")[:128], input_tokens=int(usage.input_tokens or 0),
                output_tokens=int(usage.output_tokens or 0), cache_read_tokens=int(usage.cache_read_tokens or 0),
                cache_write_tokens=int(usage.cache_write_tokens or 0), cost_usd=cost))
        metrics.AI_TOKENS.labels(task[:64], "input").inc(int(usage.input_tokens or 0))
        metrics.AI_TOKENS.labels(task[:64], "output").inc(int(usage.output_tokens or 0))
        metrics.AI_COST.labels(task[:64]).inc(cost)
        if self.audit is not None:
            self.audit.record("ai", task, actor, None, {"model": usage.model, "input_tokens": usage.input_tokens,
                                                        "output_tokens": usage.output_tokens,
                                                        "cache_read_tokens": usage.cache_read_tokens, "cost_usd": cost})
        return cost

    def summary(self, since_ms: Optional[int] = None, until_ms: Optional[int] = None, group_by: str = "task",
                actor: Optional[str] = None) -> dict:
        cols = {"task": usage_table.c.task, "actor": usage_table.c.actor, "model": usage_table.c.model,
                "provider": usage_table.c.provider, "day": (usage_table.c.ts_ms - usage_table.c.ts_ms % DAY_MS)}
        if group_by not in cols:
            raise ValueError(f"group_by must be one of {sorted(cols)}")
        since_ms = since_ms if since_ms is not None else self._now_ms() - 30 * DAY_MS
        key = cols[group_by].label("key")
        q = (select(key, func.count().label("calls"), func.sum(usage_table.c.input_tokens).label("input_tokens"),
                    func.sum(usage_table.c.output_tokens).label("output_tokens"),
                    func.sum(usage_table.c.cost_usd).label("cost_usd"))
             .where(usage_table.c.ts_ms >= since_ms).group_by(key))
        if until_ms is not None:
            q = q.where(usage_table.c.ts_ms < until_ms)
        if actor is not None:
            q = q.where(usage_table.c.actor == actor)
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        groups = [{"key": (time.strftime("%Y-%m-%d", time.gmtime(r.key / 1000)) if group_by == "day" else r.key),
                   "calls": int(r.calls), "input_tokens": int(r.input_tokens or 0),
                   "output_tokens": int(r.output_tokens or 0), "cost_usd": round(float(r.cost_usd or 0), 6)}
                  for r in rows]
        groups.sort(key=lambda g: (g["key"] if group_by == "day" else -g["cost_usd"], str(g["key"])))
        spent = self.spent_today()
        return {"group_by": group_by, "since_ms": since_ms, "groups": groups,
                "total": {"calls": sum(g["calls"] for g in groups),
                          "input_tokens": sum(g["input_tokens"] for g in groups),
                          "output_tokens": sum(g["output_tokens"] for g in groups),
                          "cost_usd": round(sum(g["cost_usd"] for g in groups), 6)},
                "budget": {"daily_usd": self.daily_budget_usd, "spent_today_usd": round(spent, 6),
                           "user_daily_tokens": self.user_daily_tokens}}


class MeteredProvider:
    """Wraps an :class:`LLMProvider`: budget check before, usage record after, every call."""

    def __init__(self, inner, meter: UsageMeter):
        self.inner = inner
        self.meter = meter

    @property
    def name(self) -> str:
        return self.inner.name

    def __getattr__(self, item: str) -> Any:   # client, model, server_side_fallback, ...
        return getattr(self.inner, item)

    def check(self) -> None:
        self.meter.check(current_actor())

    def record(self, usage: LLMUsage, task: Optional[str] = None) -> float:
        try:
            return self.meter.record(current_actor(), task or current_task(), self.inner.name, usage)
        except Exception:  # noqa: BLE001 - metering must never fail the user's request
            logger.exception("could not record AI usage")
            return 0.0

    def generate_json(self, system: str, user: str, schema: dict, *, model: Optional[str] = None,
                      max_tokens: int = 16000) -> tuple[dict, LLMUsage]:
        self.check()
        data, usage = self.inner.generate_json(system, user, schema, model=model, max_tokens=max_tokens)
        usage.extra = {**(usage.extra or {}), "cost_usd": self.record(usage)}
        return data, usage
