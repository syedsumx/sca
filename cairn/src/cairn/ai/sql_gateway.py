"""AI functions in SQL through Cairn: Trino's ``ai`` connector calls the model through this gateway.

Trino's AI functions (``llm.ai.ai_gen`` …) call a model provider themselves, and they send no user with
each call. So Cairn sits in the path (``ai.<provider>.endpoint`` points here) and:

* **holds the provider's key** (``CAIRN_AI_SQL_API_KEY``); Trino only has its own access key, which is in
  the path like every other Trino call to Cairn;
* allows only the models in ``CAIRN_AI_SQL_MODELS`` (comma-separated; empty: any);
* counts every call against the deployment's **daily AI budget** (``CAIRN_AI_DAILY_BUDGET_USD``, shared
  with Cairn's own AI features) and records its tokens and estimated cost in Cairn's AI usage, as
  ``trino-sql`` (feature ``sql_ai``);
* caps calls per minute (``CAIRN_AI_SQL_RPM``, per API replica).

Per person, the access decision for an AI function (``ExecuteFunction``, which names the user) admits at
most ``CAIRN_AI_SQL_USER_DAILY_QUERIES`` queries a day and records each under that person, so the usage
report shows who ran AI functions and how often; tokens are the gateway's, for the deployment as a whole.
When a limit is reached the query fails with the reason; nothing reaches the model.

Upstream: ``CAIRN_AI_SQL_UPSTREAM`` (``https://api.anthropic.com`` or ``https://api.openai.com``, or any
OpenAI-compatible server) and ``CAIRN_AI_SQL_PROVIDER`` (``anthropic`` | ``openai``).
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Optional

from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.ai.llm import LLMUsage
from cairn.ai.usage import LLMBudgetExceeded, UsageMeter

logger = logging.getLogger(__name__)
ACTOR = "trino-sql"
DAY_MS = 86_400_000


class SqlAiGateway:
    def __init__(self, meter: UsageMeter, upstream: str, api_key: str, provider: str = "anthropic",
                 models: Optional[list[str]] = None, rpm: Optional[int] = None, user_daily_queries: Optional[int] = None,
                 http=None):
        if provider not in ("anthropic", "openai"):
            raise ValueError("CAIRN_AI_SQL_PROVIDER is anthropic or openai")
        if not upstream.startswith(("https://", "http://")):
            raise ValueError("CAIRN_AI_SQL_UPSTREAM is the provider's URL")
        self.meter, self.upstream, self.api_key, self.provider = meter, upstream.rstrip("/"), api_key, provider
        self.models = [m for m in (models or []) if m]
        self.rpm, self.user_daily_queries = rpm, user_daily_queries
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self.http = http

    @classmethod
    def from_env(cls, meter: UsageMeter) -> Optional["SqlAiGateway"]:
        upstream = os.environ.get("CAIRN_AI_SQL_UPSTREAM", "")
        if not upstream:
            return None
        key = os.environ.get("CAIRN_AI_SQL_API_KEY", "")
        if os.environ.get("CAIRN_AI_SQL_API_KEY_FILE"):
            key = open(os.environ["CAIRN_AI_SQL_API_KEY_FILE"]).read().strip()
        n = lambda v: int(v) if v and v.strip() else None  # noqa: E731
        return cls(meter, upstream, key, os.environ.get("CAIRN_AI_SQL_PROVIDER", "anthropic"),
                   [m.strip() for m in os.environ.get("CAIRN_AI_SQL_MODELS", "").split(",")],
                   n(os.environ.get("CAIRN_AI_SQL_RPM")), n(os.environ.get("CAIRN_AI_SQL_USER_DAILY_QUERIES")))

    # ---- per person, at the access decision --------------------------------------------------
    def queries_today(self, actor: str) -> int:
        from sqlalchemy import func, select

        from cairn.ai.usage import usage_table

        now = int(time.time() * 1000)
        with self.meter.engine.connect() as conn:
            return int(conn.execute(select(func.count()).where(
                usage_table.c.actor == actor, usage_table.c.task == "sql_ai_query",
                usage_table.c.ts_ms >= now - now % DAY_MS)).scalar() or 0)

    def admit(self, actor: str) -> bool:
        """Whether ``actor`` may run one more query with AI functions today; records it when so."""
        if self.user_daily_queries is not None and self.queries_today(actor) >= self.user_daily_queries:
            logger.info("AI functions refused for %s: %d queries today", actor, self.user_daily_queries)
            return False
        try:
            self.meter.check(actor)                     # the deployment's daily budget, and the person's allowance
        except LLMBudgetExceeded as e:
            logger.info("AI functions refused for %s: %s", actor, e)
            return False
        self.meter.record(actor, "sql_ai_query", self.provider, LLMUsage(model="(query)"))
        return True

    # ---- each model call ----------------------------------------------------------------------
    def _rate_ok(self) -> bool:
        if not self.rpm:
            return True
        now = time.monotonic()
        with self._lock:
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= self.rpm:
                return False
            self._calls.append(now)
            return True

    async def forward(self, path: str, body: dict) -> tuple[int, Any]:
        """One call from Trino: checked, forwarded with Cairn's key, recorded. (status, JSON body)."""
        import httpx

        model = str(body.get("model") or "")
        if self.models and model not in self.models:
            return 403, _error(self.provider, f"model {model!r} is not allowed for AI functions in SQL "
                                              f"(allowed: {', '.join(self.models)})")
        try:
            self.meter.check(ACTOR)
        except LLMBudgetExceeded as e:
            return 429, _error(self.provider, str(e))
        if not self._rate_ok():
            return 429, _error(self.provider, f"more than {self.rpm} AI function calls a minute: try again shortly")
        headers = {"content-type": "application/json"}
        if self.provider == "anthropic":
            headers.update({"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        else:
            headers["authorization"] = f"Bearer {self.api_key}"
        client = self.http or httpx.AsyncClient(timeout=120)
        try:
            r = await client.post(f"{self.upstream}{path}", json=body, headers=headers)
        except httpx.HTTPError as e:
            return 502, _error(self.provider, f"the model provider is not reachable: {e}")
        finally:
            if self.http is None:
                await client.aclose()
        try:
            out = r.json()
        except ValueError:
            return 502, _error(self.provider, f"the model provider answered {r.status_code} without JSON")
        if r.status_code == 200:
            u = out.get("usage") or {}
            usage = LLMUsage(model=str(out.get("model") or model),
                             input_tokens=int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0),
                             output_tokens=int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0))
            self.meter.record(ACTOR, "sql_ai", self.provider, usage)
        return r.status_code, out


def _error(provider: str, message: str) -> dict:
    # Trino shows its own "request to AI provider failed"; the reason is here, for operators.
    logger.warning("AI function call refused: %s", message)
    if provider == "anthropic":
        return {"type": "error", "error": {"type": "permission_error", "message": message}}
    return {"error": {"message": message, "type": "cairn_refused"}}


def build_sql_ai_router(gateway: SqlAiGateway, key: str) -> Any:
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/v1/trino", tags=["trino"], include_in_schema=False)

    def check(given: str):
        if not key or not hmac.compare_digest(given.encode(), key.encode()):
            raise HTTPException(404)

    async def handle(given: str, request: Request, path: str):
        check(given)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "expected JSON")
        status, out = await gateway.forward(path, body if isinstance(body, dict) else {})
        return JSONResponse(out, status_code=status)

    @router.post("/{given}/ai/v1/messages")
    async def messages(given: str, request: Request):
        return await handle(given, request, "/v1/messages")

    @router.post("/{given}/ai/v1/chat/completions")
    async def chat(given: str, request: Request):
        return await handle(given, request, "/v1/chat/completions")

    return router
