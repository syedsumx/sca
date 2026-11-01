"""The lakehouse for engines that read files themselves (Spark, pyiceberg, DuckDB), under Cairn's policies.

Trino enforces Cairn's masks and row filters inside each query. Engines that read a table's data files
directly cannot, so Cairn Catalog asks Cairn about every table they touch, as the person or pipeline user
they sign in as, and refuses what they may not do:

* ``POST /v1/lakehouse/token`` gives a signed-in person, or a pipeline user (its personal access token), a
  short-lived token for Cairn Catalog (audience ``lakehouse``, signed with the key Trino's tokens use,
  published at ``/v1/trino/jwks.json``).
* ``POST /v1/lakehouse/decide`` (Cairn Catalog only: a Cairn service token) answers, for a user and a list
  of tables, what the user may do with each: ``view``, ``read``, ``write``, ``manage``, and whether
  reading it is ``restricted`` (a column mask or a row filter applies to the user). A restricted table
  is never handed to such an engine: its files hold what the mask or filter would hide, so the catalog
  refuses it and the user queries it through Trino, which applies them. Any error decides nothing
  (everything false): the catalog then refuses.

Tables are the datasets Trino names for the lakehouse catalog (``CAIRN_LAKEHOUSE_TRINO_CATALOG``,
default ``lakehouse``), so one policy governs a table in Trino and in Spark alike.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from cairn.dataaccess.trino import MANAGE, READ, SYSTEM, VIEW, WRITE, TrinoAccess

logger = logging.getLogger(__name__)

AUDIENCE = "lakehouse"
NOTHING = {"view": False, "read": False, "write": False, "manage": False, "restricted": True}


class LakehouseAccess:
    def __init__(self, access: TrinoAccess, groups_of: Callable[[str], list[str]], trino_catalog: str = "lakehouse",
                 catalog_uris: tuple[str, ...] = ()):
        self.access, self.groups_of, self.trino_catalog = access, groups_of, trino_catalog
        # Cairn Catalog's addresses, as engines name it in OpenLineage (``CAIRN_LAKEHOUSE_CATALOG_URIS``).
        self.catalog_uris = tuple(u.rstrip("/") for u in catalog_uris if u.strip())

    def remap_lineage(self, event: dict) -> dict:
        """Engines that read files (Spark) name lakehouse tables by their storage paths in OpenLineage, and
        add a ``symlinks`` facet naming the table in the catalog. Tables of Cairn Catalog are renamed to
        the datasets Trino and the catalog crawler name, so lineage from every engine meets on one dataset."""
        if not isinstance(event, dict) or not self.catalog_uris:
            return event
        alias: dict[tuple[str, str], tuple[str, str]] = {}
        for side in ("inputs", "outputs"):
            for d in event.get(side) or []:
                if not isinstance(d, dict):
                    continue
                for ident in ((d.get("facets") or {}).get("symlinks") or {}).get("identifiers") or []:
                    ns = str(ident.get("namespace", "")).rstrip("/")
                    parts = str(ident.get("name", "")).split(".")
                    if ident.get("type") == "TABLE" and ns in self.catalog_uris and len(parts) >= 2:
                        platform, name, _ = self.access.dataset_name(self.trino_catalog, ".".join(parts[:-1]), parts[-1])
                        alias[(d.get("namespace"), d.get("name"))] = (f"{platform}://", name)
                        break
        if not alias:
            return event

        def one(d: dict) -> dict:
            hit = alias.get((d.get("namespace"), d.get("name")))
            return {**d, "namespace": hit[0], "name": hit[1]} if hit else d

        ev = dict(event)
        for side in ("inputs", "outputs"):
            out = []
            for d in event.get(side) or []:
                if not isinstance(d, dict):
                    out.append(d)
                    continue
                nd = one(d)
                cl = (d.get("facets") or {}).get("columnLineage")
                if isinstance(cl, dict) and isinstance(cl.get("fields"), dict):
                    fields = {k: {**v, "inputFields": [one(i) if isinstance(i, dict) else i for i in v.get("inputFields") or []]}
                              if isinstance(v, dict) else v for k, v in cl["fields"].items()}
                    nd = {**nd, "facets": {**(d.get("facets") or {}), "columnLineage": {**cl, "fields": fields}}}
                out.append(nd)
            ev[side] = out
        return ev

    def urn(self, namespace: list[str], name: str) -> str:
        return self.access.urn(self.trino_catalog, ".".join(namespace), name)

    def decide(self, user: str, tables: list[dict]) -> dict:
        actor = user if user.startswith("urn:li:") else f"urn:li:corpuser:{user}"
        try:
            groups = list(self.groups_of(actor))
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("lakehouse: groups of %s unavailable: %s", actor, e)
            return {"schemas": False, "tables": [dict(NOTHING) for _ in tables]}
        allowed = lambda priv, urn: self.access.allowed(actor, groups, priv, urn)  # noqa: E731
        try:
            schemas = allowed(SYSTEM, None) or allowed(MANAGE, None)
        except Exception:  # noqa: BLE001
            schemas = False
        out = []
        for t in tables:
            try:
                urn = self.urn(list(t.get("namespace") or []), str(t.get("name") or ""))
                r = {"view": allowed(VIEW, urn), "read": allowed(READ, urn), "write": allowed(WRITE, urn),
                     "manage": allowed(MANAGE, urn), "restricted": False}
                if r["read"]:
                    r["restricted"] = self._restricted(actor, groups, urn)
                out.append(r)
            except Exception as e:  # noqa: BLE001 - fail closed
                logger.warning("lakehouse decision failed for %s on %s: %s", actor, t, e)
                out.append(dict(NOTHING))
        return {"schemas": bool(schemas), "tables": out}

    def _restricted(self, actor: str, groups: list[str], urn: str) -> bool:
        d = self.access.decide(actor, groups, READ, urn)
        if any(o.type == "row_filter" for o in d.obligations):
            return True
        return bool(self.access.plan_for(actor, urn).active)


def build_lakehouse_router(lake: LakehouseAccess, tokens, current_principal, audit=None):
    from fastapi import APIRouter, Body, Depends, HTTPException

    from cairn.dataaccess.trino import CRAWLER

    router = APIRouter(prefix="/v1/lakehouse", tags=["lakehouse"])

    @router.post("/token")
    def token(ttl_seconds: Optional[int] = Body(None, embed=True), p=Depends(current_principal)):
        """A token for Cairn Catalog (Spark, pyiceberg) as the caller: a person, or a pipeline user."""
        if p.is_service or p.via in ("agent", "extension") or p.urn == f"urn:li:corpuser:{CRAWLER}":
            raise HTTPException(403, "lakehouse tokens are for people and pipeline users")
        try:
            out = tokens.mint(p.urn, ttl_seconds, audience=AUDIENCE)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if audit is not None:
            audit.record("lakehouse", "lakehouse.token_issued", p.urn, None,
                         {"token_id": out["token_id"], "expires_at": out["expires_at"]})
        return out

    @router.post("/decide")
    def decide(user: str = Body(...), tables: list[dict] = Body(default_factory=list), p=Depends(current_principal)):
        """Cairn Catalog asks what ``user`` may do with each table."""
        if not p.is_service:
            raise HTTPException(403, "only Cairn Catalog asks for lakehouse decisions")
        if len(tables) > 1000:
            raise HTTPException(413, "at most 1000 tables per decision")
        return lake.decide(user, tables)

    return router
