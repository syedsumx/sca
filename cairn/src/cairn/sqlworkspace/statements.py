"""Statements read page by page: SQL on Trino as the signed-in person, for results of any size (``cairn query``).

``POST /v1/statements`` checks the SQL exactly as the SQL workspace does (:meth:`DataAccessService.prepare`: one
read-only statement over catalogued tables the person may read, rewritten for their aggregation and projection
policies), then starts it on Trino signed in as the person with a short Cairn token, so Trino's own access control
asks Cairn again and applies the person's masks and row filters. Each answer carries the rows Trino has ready and a
``next`` cursor; ``POST /v1/statements/next`` with it reads on, and ``POST /v1/statements/cancel`` stops the query in
Trino (Ctrl-C in ``cairn query``). Nothing is kept on the API: the cursor holds Trino's next-page address, sealed
(AES-GCM) for the person who started the query, so any replica serves any page and nobody else can read or stop it.

* ``connection``: which Trino (a ``trino://`` connection with ``run_as_user``); by default ``CAIRN_QUERY_CONNECTION``
  or the only such connection. ``warehouse``: run it on a team's warehouse instead (``USE_WAREHOUSE`` on
  ``urn:li:warehouse:<name>``; the warehouse's Trino asks Cairn as that warehouse too).
* ``limit``: at most this many rows (Trino stops there). Without it the result streams to the end.
* ``CAIRN_QUERY_MAX_RUN_TIME`` (``1h``): Trino's ``query_max_run_time`` for these statements.
* ``workspace``: run it where that workspace computes (``/v1/compute/workspaces``): on its data plane's Trino, through
  the data plane's tunnel (a connection whose URL has ``?dataplane=<name>``), or on Cairn's managed pool. A data plane
  whose tunnel is down refuses the statement (503).

People only: agents query through ``/data/query`` within their scopes. Every statement is audited
(``data.queried`` with ``streamed: true``) and counted as a read of its tables.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

CURSOR_TTL_S = 3600
SOURCE = "cairn-cli"

logger = logging.getLogger(__name__)


class StatementError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Statements:
    def __init__(self, data, tokens, key: str, warehouses=None, authorize=None, default_connection: str = "",
                 max_run_time: str = "1h"):
        """``data``: the DataAccessService; ``tokens``: TrinoTokens; ``key``: the secret cursors are sealed with;
        ``warehouses``: WarehouseStatus or None; ``authorize(actor, privilege, urn)``."""
        self.data, self.tokens, self.warehouses, self.authorize = data, tokens, warehouses, authorize
        self.default_connection = default_connection
        self.max_run_time = max_run_time
        self._key = hashlib.sha256(b"cairn-statements|" + key.encode()).digest() if key else None
        self.planes = None          # cairn.dataplane.service.DataPlanes: connections on a data plane go through its tunnel

    # ---- where it runs -------------------------------------------------------------------------------
    def _connections(self) -> dict[str, dict]:
        defs = (self.data.registry.definitions() if self.data.registry else {}) or {}
        return {n: d for n, d in defs.items() if str(d.get("url", "")).startswith("trino://") and d.get("run_as_user")}

    def targets(self, actor: str) -> dict:
        names = sorted(self._connections())
        wh = []
        if self.warehouses is not None:
            wh = [n for n in sorted(self.warehouses.ports) if self._may(actor, "USE_WAREHOUSE", f"urn:li:warehouse:{n}")]
        return {"connections": names, "default": self._default(), "warehouses": wh}

    def _default(self) -> Optional[str]:
        conns = self._connections()
        if self.default_connection:
            return self.default_connection if self.default_connection in conns else None
        if len(conns) == 1:
            return next(iter(conns))
        return "lakehouse" if "lakehouse" in conns else None

    def _may(self, actor: str, privilege: str, urn: Optional[str]) -> bool:
        if self.authorize is None:
            return False
        try:
            return bool(self.authorize(actor, privilege, urn))
        except Exception:  # noqa: BLE001 - fail closed
            return False

    def _dataplane(self, connection: str) -> Optional[str]:
        from cairn.quality.collectors import _expand_env

        definition = self._connections().get(connection) or {}
        q = parse_qs(urlsplit(_expand_env(str(definition.get("url", "")))).query)
        return q["dataplane"][-1] if q.get("dataplane") else None

    def _for_workspace(self, workspace: str, connection: Optional[str]) -> Optional[str]:
        if self.planes is None:
            raise StatementError("this Cairn has no data planes: run without a workspace", 422)
        from cairn.dataplane.service import DataPlaneError

        try:
            where = self.planes.get_compute(workspace)
            if where["compute"] == "customer":
                dp = self.planes.get(str(where["dataplane"]))
                if not dp.get("connection"):
                    raise StatementError(f"data plane {dp['name']} has no Trino connection set", 503)
                if connection and connection != dp["connection"]:
                    raise StatementError(f"workspace {workspace} computes on data plane {dp['name']} "
                                         f"(connection {dp['connection']}), not {connection}", 409)
                return dp["connection"]
        except DataPlaneError as e:
            raise StatementError(str(e), e.status)
        if connection and self._dataplane(connection):
            raise StatementError(f"workspace {workspace} computes on Cairn's managed pool, not on a data plane", 409)
        return connection

    def _endpoint(self, connection: str, warehouse: Optional[str], actor: str) -> tuple[str, Any, dict]:
        from cairn.quality.collectors import _expand_env

        definition = self._connections().get(connection)
        if definition is None:
            raise StatementError(f"{connection} is not a Trino connection that runs queries as the person", 404)
        url = urlsplit(_expand_env(definition["url"]))
        if url.password:
            raise StatementError("statements read page by page sign in to Trino with Cairn's tokens; this connection "
                                 "signs in with a password", 503)
        opts = {k: v[-1] for k, v in parse_qs(url.query).items()}
        scheme = opts.get("protocol", "http")
        if scheme not in ("http", "https"):
            raise StatementError("protocol must be http or https", 400)
        verify: Any = opts.get("verify", "true")
        verify = False if verify == "false" else (True if verify == "true" else verify)
        path = [unquote(p) for p in url.path.split("/") if p]
        session = {"catalog": path[0] if path else None, "schema": path[1] if len(path) > 1 else None}
        base = f"{scheme}://{url.hostname}:{url.port or (443 if scheme == 'https' else 8080)}"
        if opts.get("dataplane") and self.planes is None:
            raise StatementError(f"{connection} is on a data plane, and this Cairn has no data planes", 503)
        if warehouse:
            if self.warehouses is None or warehouse not in self.warehouses.ports:
                raise StatementError(f"no warehouse {warehouse}", 404)
            if not self._may(actor, "USE_WAREHOUSE", f"urn:li:warehouse:{warehouse}"):
                raise StatementError(f"you may not use warehouse {warehouse}: it needs USE_WAREHOUSE on "
                                     f"urn:li:warehouse:{warehouse}", 403)
            proxy = urlsplit(self.warehouses.proxy_url)
            base = f"{proxy.scheme}://{proxy.hostname}:{self.warehouses.ports[warehouse]}"
            verify = self.warehouses.verify
        return base, verify, session

    # ---- the cursor ----------------------------------------------------------------------------------
    def _seal(self, doc: dict) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        raw = AESGCM(self._key).encrypt(nonce, json.dumps(doc, separators=(",", ":")).encode(), b"cairn-statement")
        return base64.urlsafe_b64encode(nonce + raw).decode().rstrip("=")

    def _open(self, cursor: str, actor: str) -> dict:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            doc = json.loads(AESGCM(self._key).decrypt(raw[:12], raw[12:], b"cairn-statement"))
        except (ValueError, InvalidTag, TypeError):
            raise StatementError("not a cursor this Cairn made", 400)
        if doc.get("a") != actor:
            raise StatementError("this statement is someone else's", 403)
        if doc.get("e", 0) < time.time():
            raise StatementError("the statement expired: run it again", 410)
        return doc

    # ---- running -------------------------------------------------------------------------------------
    def _client(self, verify, connection: str = "", actor: str = ""):
        import httpx

        dp = self._dataplane(connection) if connection else None
        if dp:                                         # the data plane's Trino, only through its tunnel
            from cairn.dataplane.service import tunnel_client

            return tunnel_client(self.planes, dp, actor)
        return httpx.Client(verify=verify, timeout=httpx.Timeout(60.0, connect=10.0))

    def _headers(self, run_as: str, session: dict) -> dict:
        user = run_as.removeprefix("urn:li:corpuser:")
        h = {"Authorization": "Bearer " + self.tokens.mint(run_as, 300)["token"], "X-Trino-User": user,
             "X-Trino-Source": SOURCE, "Accept": "application/json"}
        if session.get("catalog"):
            h["X-Trino-Catalog"] = session["catalog"]
        if session.get("schema"):
            h["X-Trino-Schema"] = session["schema"]
        if self.max_run_time:
            h["X-Trino-Session"] = f"query_max_run_time={self.max_run_time}"
        from cairn.observability.telemetry import outgoing_headers, trino_client_tags

        if trino_client_tags():                         # the query in Trino's history leads back to its trace
            h["X-Trino-Client-Tags"] = ",".join(trino_client_tags())
        h.update(outgoing_headers())                    # Trino joins the trace (also through httpx's instrumentation)
        return h

    def start(self, actor: str, sql: str, connection: Optional[str] = None, warehouse: Optional[str] = None,
              limit: Optional[int] = None, workspace: Optional[str] = None) -> dict:
        from cairn.coreproxy.masking import mask_sql
        from cairn.dataaccess.service import DataAccessError, capped, output_sources

        if self._key is None or self.tokens is None:
            raise StatementError("statements are not configured: they need Trino sign-in through Cairn "
                                 "(CAIRN_TRINO_ACCESS_KEY)", 503)
        if workspace:
            connection = self._for_workspace(workspace, connection)
        connection = connection or self._default()
        if not connection:
            raise StatementError("name a connection (--connection); this Cairn has "
                                 f"{sorted(self._connections()) or 'no Trino connection that runs as the person'}", 422)
        base, verify, session = self._endpoint(connection, warehouse, actor)
        try:
            prepared = self.data.prepare(actor, sql, connection)
        except DataAccessError as e:
            raise StatementError(str(e), e.status)
        dialect = prepared.source.dialect
        text = capped(prepared.tree, limit, dialect) if limit else prepared.tree.sql(dialect=dialect)
        sources = [sorted(s) if s is not None else None for s in output_sources(prepared.tree)]
        import httpx

        try:
            with self._client(verify, connection, actor) as http:
                r = http.post(f"{base}/v1/statement", content=text.encode(), headers=self._headers(prepared.run_as, session))
                doc = self._answer(r)
        except httpx.TransportError as e:
            raise StatementError(f"Trino of {connection} cannot be reached ({type(e).__name__})", 502)
        self.data._audit(actor, "data.queried", connection, {"sql": mask_sql(sql), "tables": prepared.urns,
                                                           "streamed": True, "query_id": doc.get("id"),
                                                           **({"warehouse": warehouse} if warehouse else {}),
                                                           **({"query_policies": sorted(prepared.policies)}
                                                              if prepared.policies else {})})
        self.data._read(actor, prepared.urns)
        logger.info("statement %s started on %s for %s (%d tables)", doc.get("id"), connection, actor, len(prepared.urns))
        state = {"a": actor, "r": prepared.run_as, "b": base, "w": warehouse, "c": connection, "s": session,
                 "u": prepared.urns, "src": sources, "id": doc.get("id")}
        return self._page(state, doc, verify)

    def next(self, actor: str, cursor: str, wait_s: float = 10.0) -> dict:
        state = self._open(cursor, actor)
        base, verify, _ = self._endpoint(state["c"], state.get("w"), actor)
        if base != state["b"] or not str(state.get("n", "")).startswith(base + "/"):
            raise StatementError("the statement's Trino moved: run it again", 409)
        end = time.monotonic() + max(0.0, min(wait_s, 30.0))
        with self._client(verify, state["c"], actor) as http:
            while True:          # Trino's first pages are often empty while the query starts: wait a little here
                doc = self._answer(http.get(state["n"], headers=self._headers(state["r"], state["s"])))
                if doc.get("data") or not doc.get("nextUri") or doc.get("columns") and not state.get("cols") \
                        or time.monotonic() > end:
                    break
                state["n"] = doc["nextUri"]
        return self._page(state, doc, verify)

    def cancel(self, actor: str, cursor: str) -> dict:
        state = self._open(cursor, actor)
        base, verify, _ = self._endpoint(state["c"], state.get("w"), actor)
        if base != state["b"] or not str(state.get("n", "")).startswith(base + "/"):
            raise StatementError("the statement's Trino moved", 409)
        with self._client(verify, state["c"], actor) as http:
            r = http.delete(state["n"], headers=self._headers(state["r"], state["s"]))
        self.data._audit(actor, "data.query_cancelled", state["c"], {"query_id": state.get("id")})
        return {"id": state.get("id"), "cancelled": r.status_code in (200, 204, 404, 410)}

    def _answer(self, r) -> dict:
        if r.status_code == 401:
            raise StatementError("Trino did not accept Cairn's sign-in (check http-server.authentication.jwt points at "
                                 "Cairn's /v1/trino/jwks.json)", 502)
        if r.status_code in (404, 410):
            raise StatementError("Trino no longer has this query (it ended, or was idle too long): run it again", 410)
        if r.status_code >= 300:
            raise StatementError(f"Trino answered {r.status_code}: {r.text[:300]}", 502)
        try:
            doc = r.json()
        except ValueError:
            raise StatementError("Trino's answer was not JSON", 502)
        err = doc.get("error")
        if err:
            name, message = err.get("errorName") or "", str(err.get("message") or "")[:500]
            if name == "PERMISSION_DENIED":
                raise StatementError(f"Trino refused the query as you: {message}", 403)
            if name in ("USER_CANCELED", "ADMINISTRATIVELY_KILLED"):
                raise StatementError(f"the query was cancelled: {message}", 409)
            if err.get("errorType") == "USER_ERROR":
                raise StatementError(f"the query failed: {message}", 400)
            raise StatementError(f"Trino could not run the query: {message}", 502)
        return doc

    def _page(self, state: dict, doc: dict, verify) -> dict:
        columns = doc.get("columns") or None
        rows = doc.get("data") or []
        masked: list[str] = []
        if columns:
            names = [c.get("name") for c in columns]
            sources = [set(s) if s is not None else None for s in state.get("src") or []]
            if rows:
                masked = self.data._mask(state["a"], state["u"], names, rows, sources + [None] * (len(names) - len(sources)))
            state["cols"] = True
        nxt = doc.get("nextUri")
        cursor = None
        if nxt:
            state = {**state, "n": nxt, "e": int(time.time()) + CURSOR_TTL_S}
            cursor = self._seal(state)
        stats = doc.get("stats") or {}
        return {"id": doc.get("id"), "state": stats.get("state"), "next": cursor,
                "columns": [{"name": c.get("name"), "type": c.get("type")} for c in columns] if columns else None,
                "rows": rows, "masked_columns": masked,
                "stats": {k: stats.get(k) for k in ("processedRows", "processedBytes", "elapsedTimeMillis", "queuedTimeMillis")
                          if k in stats}}


def build_statements_router(services, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    from cairn.common.security import Principal
    from cairn.dataaccess.warehouse_status import WarehouseStatus

    def may(actor: str, privilege: str, urn: Optional[str]) -> bool:
        return services.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False)

    svc = Statements(services.data_access, getattr(services, "trino_tokens", None),
                     getattr(services, "trino_key", "") or (services.settings.service_token or ""),
                     WarehouseStatus.from_env(), may, os.environ.get("CAIRN_QUERY_CONNECTION", "").strip(),
                     os.environ.get("CAIRN_QUERY_MAX_RUN_TIME", "1h").strip())
    svc.planes = getattr(services, "dataplanes", None)
    services.statements = svc
    router = APIRouter(prefix="/v1/statements", tags=["sql"])

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "statements are for people; agents query through /data/query")
        return p

    async def call(fn, *a):
        from cairn.dataplane.service import DataPlaneError

        try:
            return await run_in_threadpool(fn, *a)
        except StatementError as e:
            raise HTTPException(e.status, str(e))
        except DataPlaneError as e:                   # a data plane's tunnel down, or its Trino refused: fail closed
            raise HTTPException(e.status, str(e))

    @router.get("/targets")
    async def targets(p=Depends(current_principal)):
        return await call(svc.targets, person(p).urn)

    @router.post("")
    async def start(sql: str = Body(..., min_length=1, max_length=200_000), connection: Optional[str] = Body(None),
                    warehouse: Optional[str] = Body(None), limit: Optional[int] = Body(None, ge=1, le=100_000_000),
                    workspace: Optional[str] = Body(None, max_length=64), p=Depends(current_principal)):
        return await call(svc.start, person(p).urn, sql, connection, warehouse, limit, workspace)

    @router.post("/next")
    async def next_page(cursor: str = Body(..., embed=True, max_length=20_000), p=Depends(current_principal)):
        return await call(svc.next, person(p).urn, cursor)

    @router.post("/cancel")
    async def cancel(cursor: str = Body(..., embed=True, max_length=20_000), p=Depends(current_principal)):
        return await call(svc.cancel, person(p).urn, cursor)

    return router
