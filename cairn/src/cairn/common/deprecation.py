"""The API deprecation policy (L65): how a route is retired without surprising the people who call it.

* A route is deprecated by adding it to ``DEPRECATIONS`` (method, path template, the day it was deprecated, the
  day it stops answering, its replacement). The sunset is at least ``MIN_NOTICE_DAYS`` after the deprecation.
* From the deprecation day every answer from that route carries ``Deprecation: @<unix time>`` (RFC 9745),
  ``Sunset: <HTTP date>`` (RFC 8594) and, when there is one, ``Link: <replacement>; rel="successor-version"``.
  The OpenAPI document marks the operation ``deprecated`` and gives the sunset as ``x-sunset``.
* From the sunset day the route answers ``410 Gone`` with the same headers, so a client learns where to go.
* ``GET /deprecations`` lists them; the Python, JavaScript and Java clients warn once per route when they see the
  header. ``cairn_api_deprecated_requests_total{route}`` counts calls still made to deprecated routes.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import format_datetime
from typing import Optional

MIN_NOTICE_DAYS = 180


@dataclass(frozen=True)
class Deprecated:
    method: str                      # GET, POST, … or * for every method
    path: str                        # the route template, e.g. /sql/saved/{qid}/run
    deprecated: date
    sunset: date
    successor: Optional[str] = None  # the path (or URL) that replaces it
    note: str = ""

    def __post_init__(self):
        if (self.sunset - self.deprecated).days < MIN_NOTICE_DAYS:
            raise ValueError(f"{self.method} {self.path}: the sunset must be at least {MIN_NOTICE_DAYS} days after "
                             "the deprecation")

    @property
    def pattern(self) -> re.Pattern:
        return re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(self.path)) + "/?$")

    def headers(self) -> list[tuple[str, str]]:
        at = int(datetime(self.deprecated.year, self.deprecated.month, self.deprecated.day, tzinfo=timezone.utc).timestamp())
        sunset = datetime(self.sunset.year, self.sunset.month, self.sunset.day, tzinfo=timezone.utc)
        out = [("deprecation", f"@{at}"), ("sunset", format_datetime(sunset, usegmt=True))]
        if self.successor:
            out.append(("link", f'<{self.successor}>; rel="successor-version"'))
        return out

    def describe(self) -> dict:
        return {"method": self.method, "path": self.path, "deprecated": self.deprecated.isoformat(),
                "sunset": self.sunset.isoformat(), "successor": self.successor, "note": self.note}


# Routes being retired. Each one also goes in the change log of the operator manual (administration.md).
DEPRECATIONS: list[Deprecated] = []


def registry() -> list[Deprecated]:
    return list(DEPRECATIONS)


def _today() -> date:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).date()


class DeprecationMiddleware:
    """Pure ASGI: adds the headers to deprecated routes and answers 410 once they are past their sunset."""

    def __init__(self, app, entries: list[Deprecated], counter=None):
        self.app, self.entries, self.counter = app, [(e, e.pattern) for e in entries], counter

    def match(self, method: str, path: str) -> Optional[Deprecated]:
        for e, rx in self.entries:
            if e.method in ("*", method) and rx.match(path):
                return e
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.entries:
            await self.app(scope, receive, send)
            return
        path, root = scope.get("path", ""), scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):] or "/"
        e = self.match(scope.get("method", "GET"), path)
        if e is None or _today() < e.deprecated:
            await self.app(scope, receive, send)
            return
        if self.counter is not None:
            self.counter.labels(e.path).inc()
        extra = [(k.encode(), v.encode()) for k, v in e.headers()]
        if _today() >= e.sunset:
            body = json.dumps({"detail": f"{e.method} {e.path} was retired on {e.sunset.isoformat()}"
                                         + (f"; use {e.successor}" if e.successor else "")}).encode()
            await send({"type": "http.response.start", "status": 410,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                    (b"cache-control", b"no-store"), *extra]})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)
        await self.app(scope, receive, send_with_headers)


def mark_openapi(schema: dict, entries: list[Deprecated]) -> dict:
    """Mark deprecated operations in an OpenAPI document."""
    for e in entries:
        for method, op in (schema.get("paths", {}).get(e.path) or {}).items():
            if e.method in ("*", method.upper()) and isinstance(op, dict):
                op["deprecated"] = True
                op["x-sunset"] = e.sunset.isoformat()
                if e.successor:
                    op["x-successor"] = e.successor
    return schema


def install(app, entries: Optional[list[Deprecated]] = None, principal=None) -> None:
    """Wire the policy into a FastAPI app: the middleware, the OpenAPI marks and ``GET /deprecations`` (for signed-in
    callers when ``principal``, the app's caller dependency, is given)."""
    from fastapi import Depends

    entries = registry() if entries is None else entries
    counter = None
    try:
        from prometheus_client import REGISTRY, Counter

        name = "cairn_api_deprecated_requests"
        counter = REGISTRY._names_to_collectors.get(name + "_total") or Counter(  # noqa: SLF001 - one per process
            name, "Requests to deprecated API routes", ["route"])
    except Exception:  # noqa: BLE001 - metrics are optional
        counter = None
    from starlette.middleware import Middleware

    # Inside the IP allowlist and quotas (LimitsMiddleware), so a retired route is not an answer from outside them.
    at = next((i + 1 for i, m in enumerate(app.user_middleware) if getattr(m.cls, "__name__", "") == "LimitsMiddleware"), 0)
    app.user_middleware.insert(at, Middleware(DeprecationMiddleware, entries=entries, counter=counter))
    original = app.openapi

    def openapi():
        if app.openapi_schema is None:
            app.openapi_schema = mark_openapi(original(), entries)
        return app.openapi_schema
    app.openapi = openapi

    deps = [Depends(principal)] if principal is not None else []

    @app.get("/deprecations", tags=["meta"], dependencies=deps)
    def deprecations():
        return {"min_notice_days": MIN_NOTICE_DAYS, "routes": [e.describe() for e in entries]}
