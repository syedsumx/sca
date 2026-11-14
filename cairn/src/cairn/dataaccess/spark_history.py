"""Spark's history server, behind Cairn's sign-in.

The history server has no sign-in of its own, so it is never exposed; Cairn serves it at
``/v1/spark/history/`` (CAIRN_SPARK_HISTORY_URL: where Cairn reaches it). Each person sees the
applications they ran (``spark.cairn.user``, which cairn_spark records) and nothing else; people with
MANAGE_SYSTEM_OPERATIONS see every application. Only GET is passed on: the history server is read-only.

Run the history server with ``-Dspark.ui.proxyBase=<Cairn's public API path>/v1/spark/history`` so its
pages link back through Cairn.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)
APP = re.compile(r"^(?:api/v1/applications|history)/([A-Za-z0-9_.-]+)(?:/|$)")


class SparkHistory:
    def __init__(self, base_url: str, http: Optional[httpx.Client] = None, owner_ttl: int = 300):
        self.base = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self.owner_ttl = owner_ttl
        self._owners: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def owner(self, app_id: str) -> str:
        """The Cairn user who ran ``app_id`` ("" if unknown: then only operators see it)."""
        now = time.monotonic()
        with self._lock:
            hit = self._owners.get(app_id)
            if hit and hit[0] > now:
                return hit[1]
        r = self.http.get(f"{self.base}/api/v1/applications/{app_id}/environment")
        user = ""
        if r.status_code == 200:
            props = dict((r.json() or {}).get("sparkProperties") or [])
            user = str(props.get("spark.cairn.user") or "")
        with self._lock:
            self._owners[app_id] = (now + self.owner_ttl, user)
        return user

    def get(self, path: str, query: str) -> httpx.Response:
        return self.http.get(f"{self.base}/{path}" + (f"?{query}" if query else ""))


def build_spark_history_router(history: SparkHistory, current_principal, is_operator: Callable) -> APIRouter:
    router = APIRouter(prefix="/v1/spark/history", tags=["spark"])

    def fetch(path: str, query: str, user: str, operator: bool) -> Response:
        m = APP.match(path)
        if m and not operator and history.owner(m.group(1)) != user:
            raise HTTPException(404, "no such application")  # not "forbidden": others' runs are not listed
        r = history.get(path, query)
        body, ctype = r.content, r.headers.get("content-type", "application/octet-stream")
        if path.rstrip("/") == "api/v1/applications" and r.status_code == 200 and not operator:
            import json

            apps = [a for a in r.json() if history.owner(a.get("id", "")) == user]
            body, ctype = json.dumps(apps).encode(), "application/json"
        return Response(body, status_code=r.status_code, media_type=ctype.split(";")[0],
                        headers={"Cache-Control": "no-store"})

    @router.get("/{path:path}")
    async def proxy(path: str, request: Request, p=Depends(current_principal)):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "the Spark history server is for people")
        operator = is_operator(p)
        user = p.urn.removeprefix("urn:li:corpuser:")
        try:
            return await run_in_threadpool(fetch, path.lstrip("/"), request.url.query, user, operator)
        except httpx.HTTPError as e:
            logger.warning("spark history server unreachable: %s", e)
            raise HTTPException(503, "the Spark history server is unavailable")

    return router
