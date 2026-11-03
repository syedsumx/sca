"""Notebooks: JupyterHub with Cairn sign-in, kernels that act as the person, notebooks as catalog assets.

* **Sign-in.** JupyterHub is a client of Cairn's OAuth2 server (``CAIRN_JUPYTERHUB_OAUTH_SECRET``,
  ``CAIRN_JUPYTERHUB_OAUTH_REDIRECT_URIS``; its tokens' audience is ``jupyterhub``). JupyterHub's
  ``GenericOAuthenticator`` reads who signed in from ``GET /v1/notebooks/userinfo``.
* **Kernels act as the person.** When JupyterHub starts someone's server it exchanges their sign-in token
  at ``POST /v1/notebooks/token`` for a Cairn token (audience ``cairn``, ``via=notebook``, lasting
  ``CAIRN_NOTEBOOK_TOKEN_TTL``, 8 hours) and gives it to the server as ``CAIRN_TOKEN``. It works wherever a
  personal access token does, as that person (``%%sql`` on Trino, ``%cairn spark``, MLflow), and stops
  working when they are suspended. Each exchange is audited.
* **Notebooks are catalog assets.** On every save, the Jupyter server sends the notebook to
  ``POST /v1/notebooks/publish`` (``cairn_lakehouse.jupyter.post_save_hook``). Cairn records it as a
  notebook owned by the person, with its cells, and with lineage from the tables its ``%%sql`` cells read.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

logger = logging.getLogger(__name__)

HUB_AUDIENCE = "jupyterhub"
API_AUDIENCE = "cairn"
VIA = "notebook"
SQL_CELL = re.compile(r"^\s*%%sql\b[^\n]*\n(.*)", re.S)
PATH = re.compile(r"^[^\x00]{1,1024}$")


class NotebookError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class NotebookSessions:
    def __init__(self, tokens, audit=None, user_status: Optional[Callable[[str], str]] = None, ttl: Optional[int] = None):
        self.tokens, self.audit, self.user_status = tokens, audit, user_status
        self.ttl = ttl or int(os.environ.get("CAIRN_NOTEBOOK_TOKEN_TTL", str(8 * 3600)))
        if not 300 <= self.ttl <= 12 * 3600:
            raise ValueError("CAIRN_NOTEBOOK_TOKEN_TTL must be between 300 and 43200 seconds")
        self._status: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def _active(self, urn: str, fresh: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            hit = self._status.get(urn)
        if hit and not fresh and now - hit[0] < 60:
            status = hit[1]
        else:
            try:
                status = (self.user_status(urn) if self.user_status else "ACTIVE") or "ACTIVE"
            except Exception as e:  # noqa: BLE001 - fail closed
                logger.warning("notebooks: cannot check %s: %s", urn, e)
                return False
            with self._lock:
                self._status[urn] = (now, status)
        return status.upper() != "SUSPENDED"

    def userinfo(self, hub_token: str) -> dict:
        try:
            claims = self.tokens.verify(hub_token, HUB_AUDIENCE, leeway=0)
        except ValueError as e:
            raise NotebookError(f"not a JupyterHub sign-in token: {e}", 401)
        return {"sub": claims["sub"], "name": claims["sub"], "preferred_username": claims["sub"],
                "groups": list(claims.get("groups") or [])}

    def exchange(self, hub_token: str) -> dict:
        """A Cairn token for the person a JupyterHub sign-in token belongs to."""
        user = self.userinfo(hub_token)["sub"]
        urn = f"urn:li:corpuser:{user}"
        if not self._active(urn, fresh=True):
            raise NotebookError("the user is suspended (or Cairn cannot check them now)", 403)
        out = self.tokens.mint(urn, self.ttl, audience=API_AUDIENCE, extra={"via": VIA})
        if self.audit is not None:
            self.audit.record("identity", "notebook.token_issued", urn, None,
                              {"token_id": out["token_id"], "expires_at": out["expires_at"]})
        return {"token": out["token"], "expires_in": out["expires_in"], "expires_at": out["expires_at"], "user": user}

    def principal_of(self, token: str) -> Optional[str]:
        """The person a notebook token belongs to; None if it is not one; ValueError if it is but is not valid."""
        import jwt

        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None
        if claims.get("aud") != API_AUDIENCE or claims.get("via") != VIA or claims.get("iss") != self.tokens.issuer:
            return None
        verified = self.tokens.verify(token, API_AUDIENCE, leeway=0)
        urn = f"urn:li:corpuser:{verified['sub']}"
        if not self._active(urn):
            raise ValueError("the user is suspended")
        return urn


def notebook_urn(user: str, path: str) -> str:
    digest = hashlib.sha256(f"{user}:{path}".encode()).hexdigest()[:24]
    return f"urn:li:notebook:(jupyter,{digest})"


class NotebookCatalog:
    """Saved notebooks as catalog assets, with lineage from what their SQL cells read."""

    def __init__(self, graph, table_urn: Callable[[str, str, str], Optional[str]], default_catalog: str = "lakehouse",
                 audit=None):
        """``table_urn(catalog, schema, table)``: the dataset a Trino table is in the catalog."""
        self.graph, self.table_urn, self.default_catalog, self.audit = graph, table_urn, default_catalog, audit

    def tables(self, sql: str) -> list[str]:
        import sqlglot
        from sqlglot import exp

        out = set()
        for stmt in sqlglot.parse(sql, read="trino"):
            if stmt is None:
                continue
            ctes = {c.alias_or_name for c in stmt.find_all(exp.CTE)}
            for t in stmt.find_all(exp.Table):
                if not t.name or t.name in ctes:
                    continue
                parts = [p for p in (t.catalog, t.db, t.name) if p]
                if len(parts) == 2:
                    parts = [self.default_catalog, *parts]
                if len(parts) == 3:
                    urn = self.table_urn(*parts)
                    if urn:
                        out.add(urn)
        return sorted(out)

    def publish(self, actor: str, path: str, notebook: dict) -> dict:
        from cairn.common.proposals import make_mcp

        if not isinstance(path, str) or not PATH.match(path) or not path.endswith(".ipynb"):
            raise NotebookError("path: the notebook's path in Jupyter, ending in .ipynb")
        cells = notebook.get("cells") if isinstance(notebook, dict) else None
        if not isinstance(cells, list) or len(cells) > 5000:
            raise NotebookError("notebook: a Jupyter notebook (nbformat 4) with its cells")
        user = actor.removeprefix("urn:li:corpuser:")
        urn = notebook_urn(user, path)
        reads: set[str] = set()
        content = []
        for i, c in enumerate(cells):
            src = c.get("source", "")
            src = "".join(src) if isinstance(src, list) else str(src)
            kind = c.get("cell_type", "code")
            m = SQL_CELL.match(src) if kind == "code" else None
            if m:
                try:
                    reads |= set(self.tables(m.group(1)))
                except Exception as e:  # noqa: BLE001 - a cell that does not parse adds no lineage
                    logger.info("notebooks: cell %d of %s does not parse: %s", i, path, e)
            content.append({"type": "QUERY_CELL" if m else ("TEXT_CELL" if kind == "markdown" else "CHART_CELL"),
                            "text": src[:20000]})
        now = int(time.time() * 1000)
        name = path.rsplit("/", 1)[-1].removesuffix(".ipynb")
        flow = "urn:li:dataFlow:(jupyter,notebooks,PROD)"
        job = f"urn:li:dataJob:({flow},{urn.rsplit(',', 1)[-1].rstrip(')')})"
        mcps = [
            make_mcp(urn, "notebookInfo", {"title": name, "description": f"Jupyter notebook {path}",
                                           "customProperties": {"path": path, "user": user, "reads": str(len(reads))},
                                           "changeAuditStamps": {"created": {"time": now, "actor": actor},
                                                                 "lastModified": {"time": now, "actor": actor}}}),
            make_mcp(urn, "notebookContent", {"cells": [
                {"type": "TEXT_CELL", "textCell": {"cellId": str(i), "text": c["text"],
                                                   "changeAuditStamps": {"created": {"time": now, "actor": actor},
                                                                         "lastModified": {"time": now, "actor": actor}}}}
                for i, c in enumerate(content)]}),
            make_mcp(urn, "ownership", {"owners": [{"owner": actor, "type": "TECHNICAL_OWNER"}],
                                        "lastModified": {"time": now, "actor": actor}}),
            make_mcp(urn, "status", {"removed": False}),
            # Lineage: the notebook's queries as a job reading their tables.
            make_mcp(flow, "dataFlowInfo", {"name": "Jupyter notebooks", "customProperties": {}}),
            make_mcp(job, "dataJobInfo", {"name": f"{user}: {path}", "flowUrn": flow,
                                          "type": {"com.linkedin.datajob.azkaban.AzkabanJobType": "COMMAND"},
                                          "customProperties": {"notebook": urn, "path": path, "user": user}}),
            make_mcp(job, "dataJobInputOutput", {"inputDatasets": sorted(reads), "outputDatasets": []}),
            make_mcp(job, "ownership", {"owners": [{"owner": actor, "type": "TECHNICAL_OWNER"}],
                                        "lastModified": {"time": now, "actor": actor}}),
        ]
        self.graph.emit(mcps)
        if self.audit is not None:
            self.audit.record("notebooks", "notebook.published", actor, urn, {"path": path, "reads": sorted(reads)})
        return {"urn": urn, "job": job, "reads": sorted(reads)}


def build_notebooks_router(sessions: NotebookSessions, catalog: Optional[NotebookCatalog], current_principal):
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/notebooks", tags=["notebooks"])

    def bearer(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "a bearer token is required")
        return auth[7:].strip()

    def call(fn, *a):
        try:
            return fn(*a)
        except NotebookError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/userinfo")
    def userinfo(request: Request):
        """Who a JupyterHub sign-in token belongs to (JupyterHub's GenericOAuthenticator)."""
        return call(sessions.userinfo, bearer(request))

    @router.post("/token")
    def token(request: Request):
        """JupyterHub, when it starts someone's server: their sign-in token for a Cairn token for kernels."""
        return call(sessions.exchange, bearer(request))

    @router.post("/publish")
    def publish(path: str = Body(...), notebook: dict = Body(...), p=Depends(current_principal)):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "notebooks are published by the person who saved them")
        if catalog is None:
            raise HTTPException(503, "the catalog is not connected")
        return call(catalog.publish, p.urn, path, notebook)

    return router


def table_urn_for(trino_access) -> Callable[[str, str, str], Optional[str]]:
    def one(catalog: str, schema: str, table: str) -> Optional[str]:
        return trino_access.urn(catalog, schema, table)    # named as Trino's own lineage names it
    return one


__all__: list[Any] = ["NotebookSessions", "NotebookCatalog", "build_notebooks_router", "table_urn_for"]
