"""``ai_search`` in Trino SQL: a vector index searched from a query, as the person running it.

Cairn's Trino functions plugin (``plugins/java/cairn-trino-functions``, installed into Trino by Helm with
``cairn.trino.functions.enabled``) adds ``ai_search(index, query[, k])`` to Trino. It returns
``array(row(key varchar, chunk integer, text varchar, score double))``, closest first, so a query reads it with
``UNNEST``::

    SELECT r.key, r.text, r.score
    FROM UNNEST(ai_search('help', 'how do refunds work', 5)) AS r(key, chunk, text, score)

and can join the keys back to the source table. Each call comes here (``POST /v1/trino/functions/ai-search``, with
Trino's access key in ``X-Cairn-Trino-Key``) naming the query's user, and Cairn searches as that person:

* **The same refusals as ``GET /v1/vectors/<name>/search``.** READ on the index's source table, and no mask or row
  filter on it for the person (the index holds the raw text). Cairn's own identities (the catalog crawler, clean
  rooms, installed apps, share recipients, checked queries) are refused: only a person searches.
* **Metered.** Every search counts against the deployment's daily AI budget and the person's allowance
  (``CAIRN_AI_DAILY_BUDGET_USD``, ``CAIRN_AI_USER_DAILY_TOKENS``) and is recorded in AI usage under the person
  (task ``sql_ai_search``, the query's estimated tokens); ``CAIRN_AI_SEARCH_USER_DAILY`` caps searches a person a
  day. A refused search fails the query with the reason; nothing is searched.
"""

from __future__ import annotations

import hmac
import logging
import math
import os
import re
import time
from typing import Any, Optional

from starlette.requests import Request  # module level: FastAPI resolves the handler's annotations here

from cairn.ai.llm import LLMUsage
from cairn.ai.usage import LLMBudgetExceeded, UsageMeter

logger = logging.getLogger(__name__)
TASK = "sql_ai_search"
DAY_MS = 86_400_000
USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,255}$")
# Identities that are not a person: Cairn's crawler and catalog administrator, clean rooms, installed apps,
# checked queries and share recipients (cairn.dataaccess.trino, shares).
NOT_PEOPLE = ("cleanroom-", "cairnapp-", "checked-", "share:")
MAX_QUERY = 4000


class SearchRefused(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class _Person:
    def __init__(self, urn: str):
        self.urn = urn


class SqlSearch:
    def __init__(self, vectors, meter: Optional[UsageMeter], user_daily: Optional[int] = None):
        self.vectors, self.meter, self.user_daily = vectors, meter, user_daily

    @classmethod
    def from_env(cls, vectors, meter) -> "SqlSearch":
        v = os.environ.get("CAIRN_AI_SEARCH_USER_DAILY", "").strip()
        return cls(vectors, meter, int(v) if v else None)

    def _searches_today(self, actor: str) -> int:
        from sqlalchemy import func, select

        from cairn.ai.usage import usage_table

        now = int(time.time() * 1000)
        with self.meter.engine.connect() as conn:
            return int(conn.execute(select(func.count()).where(
                usage_table.c.actor == actor, usage_table.c.task == TASK,
                usage_table.c.ts_ms >= now - now % DAY_MS)).scalar() or 0)

    def search(self, user: str, index: str, query: str, k: int) -> dict:
        from cairn.dataaccess.trino import CATALOG_ADMIN, CRAWLER
        from cairn.pipelines.pipes import PipeError

        if not USER.match(user or "") or user in (CRAWLER, CATALOG_ADMIN) or user.startswith(NOT_PEOPLE):
            raise SearchRefused(f"ai_search runs as a person; {user!r} is not one", 403)
        if not isinstance(index, str) or not index.strip():
            raise SearchRefused("ai_search: the index's name", 400)
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
            raise SearchRefused(f"ai_search: what to search for (1 to {MAX_QUERY} characters)", 400)
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
            raise SearchRefused("ai_search: k is 1 to 100", 400)
        actor = f"urn:li:corpuser:{user}"
        if self.meter is None:
            raise SearchRefused("ai_search needs Cairn's AI usage metering, which is not set up", 503)
        try:
            self.meter.check(actor)                    # the deployment's daily budget and the person's allowance
        except LLMBudgetExceeded as e:
            raise SearchRefused(str(e), 429)
        if self.user_daily is not None and self._searches_today(actor) >= self.user_daily:
            raise SearchRefused(f"{self.user_daily} ai_search calls a day reached for {user}", 429)
        try:
            found = self.vectors.search(_Person(actor), index, query, k)    # read access, no mask or row filter
        except PipeError as e:
            raise SearchRefused(str(e), e.status)
        embedder = getattr(self.vectors.embedder, "name", "embeddings")
        self.meter.record(actor, TASK, str(embedder), LLMUsage(model=f"embeddings:{embedder}",
                                                               input_tokens=max(1, math.ceil(len(query) / 4))))
        return {"index": index, "method": found.get("method"),
                "results": [{"key": None if r.get("key") is None else str(r["key"]), "chunk": r.get("chunk"),
                             "text": r.get("text"), "score": r.get("score")} for r in found.get("results", [])]}


def build_sql_search_router(search: SqlSearch, key: str) -> Any:
    from fastapi import APIRouter, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/trino/functions", tags=["trino"], include_in_schema=False)

    @router.post("/ai-search")
    async def ai_search(request: Request):
        given = request.headers.get("x-cairn-trino-key", "")
        if not key or not hmac.compare_digest(given.encode(), key.encode()):
            raise HTTPException(404)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "expected JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        try:
            return await run_in_threadpool(search.search, str(body.get("user") or ""), body.get("index"),
                                           body.get("query"), body.get("k", 10))
        except SearchRefused as e:
            logger.info("ai_search refused for %s on %s: %s", body.get("user"), body.get("index"), e)
            raise HTTPException(e.status, str(e))

    return router
