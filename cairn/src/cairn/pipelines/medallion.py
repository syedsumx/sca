"""Medallion layers: bronze, silver and gold on the lakehouse tables Cairn keeps (pipes, streams, derived tables).

A table's ``layer`` is set with the resource that writes it (``"layer"`` in the body of ``POST``/``PUT`` on
``/v1/pipes``, ``/v1/streams`` and ``/v1/derived``) or on its own (``PUT /v1/medallion/<namespace.table>``). Cairn
keeps it (``cairn_medallion_layers``) and shows it in the catalog as the tag ``urn:li:tag:medallion.<layer>`` on the
table's dataset (one medallion tag at a time; the table's other tags are kept).

Promotion to **gold** is checked: the table must have an owner in the catalog, a description, and either a data
contract that is met or quality monitors whose latest evaluations all pass (at least one). A table that does not
qualify is refused (409) with what it lacks. ``GET /v1/medallion`` lists every layered table with those checks run
again, so a gold table that stopped qualifying (its contract broke) shows it.

Bronze and silver have no conditions. Marking needs ``MANAGE_INGESTION`` (or the resource's own rights when set with
it).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, delete, select

from cairn.common.db import metadata
from cairn.pipelines.pipes import TARGET, PipeError

medallion_layers = Table(
    "cairn_medallion_layers", metadata,
    Column("dataset_urn", String(512), primary_key=True),
    Column("table_name", String(512), nullable=False),        # namespace.table
    Column("layer", String(16), nullable=False),             # bronze | silver | gold
    Column("resource", String(300), nullable=True),          # pipe:<name> | stream:<name> | derived:<name>
    Column("set_by", String(256), nullable=False),
    Column("set_ms", BigInteger, nullable=False),
)

LAYERS = ("bronze", "silver", "gold")
TAG = "urn:li:tag:medallion."


class MedallionError(PipeError):
    pass


def requested(body: dict) -> Optional[str]:
    """The layer a definition asks for (None: not given), checked."""
    if body.get("layer") in (None, ""):
        return None
    layer = str(body["layer"]).lower()
    if layer not in LAYERS:
        raise MedallionError(f"layer is one of {', '.join(LAYERS)}")
    return layer


class Medallion:
    def __init__(self, engine, graph=None, audit=None, contract_status: Optional[Callable[[str], Optional[str]]] = None,
                 quality=None):
        """``contract_status(urn)``: the dataset's contract status (MET, BROKEN, PENDING) or None; ``quality``: the
        quality engine (its store's monitors and evaluations)."""
        self.engine, self.graph, self.audit = engine, graph, audit
        self.contract_status, self.quality = contract_status, quality

    # ---- the gold rules -------------------------------------------------------------------------
    def problems(self, urn: str) -> list[str]:
        """Why ``urn`` may not be gold (empty: it may)."""
        if self.graph is None:
            return ["gold needs the catalog (Cairn Core) to check the table's owner and description"]
        out = []
        owners = (self.graph.get_aspect(urn, "ownership") or {}).get("owners") or []
        if not owners:
            out.append("it has no owner in the catalog")
        desc = ((self.graph.get_aspect(urn, "editableDatasetProperties") or {}).get("description")
                or (self.graph.get_aspect(urn, "datasetProperties") or {}).get("description") or "")
        if not str(desc).strip():
            out.append("it has no description")
        contract = self.contract_status(urn) if self.contract_status is not None else None
        checks = self._monitors(urn)
        if contract == "MET" or (checks and all(s == "PASS" for s in checks)):
            return out
        if contract is not None:
            why = f"its data contract is {contract}"
        elif checks:
            why = f"its quality monitors are not all passing ({', '.join(sorted(set(checks)))})"
        else:
            why = "it has neither a data contract nor a quality monitor"
        out.append(why + " (gold needs a met contract or passing monitors)")
        return out

    def _monitors(self, urn: str) -> list[str]:
        """The latest evaluation status of each of the dataset's monitors (NONE: not evaluated yet)."""
        if self.quality is None:
            return []
        store = self.quality.store
        out = []
        for m in store.list_monitors(urn):
            last = store.evaluations(m.id, limit=1)
            out.append(last[0].status if last else "NONE")
        return out

    # ---- layers ---------------------------------------------------------------------------------
    def check(self, urn: str, layer: str) -> None:
        if layer == "gold":
            why = self.problems(urn)
            if why:
                raise MedallionError("not gold: " + "; ".join(why), 409)

    def set(self, urn: str, table: str, layer: str, actor: str, resource: Optional[str] = None) -> dict:
        """Marks ``table`` (its dataset ``urn``) as ``layer``, after the gold rules when it is gold."""
        if layer not in LAYERS:
            raise MedallionError(f"layer is one of {', '.join(LAYERS)}")
        if not TARGET.match(table):
            raise MedallionError("the table is namespace.table")
        self.check(urn, layer)
        row = {"dataset_urn": urn, "table_name": table, "layer": layer, "set_by": actor,
               "set_ms": int(time.time() * 1000)}
        with self.engine.begin() as conn:
            cur = conn.execute(select(medallion_layers).where(medallion_layers.c.dataset_urn == urn)).first()
            if cur is not None:
                row["resource"] = resource or cur.resource
                conn.execute(medallion_layers.update().where(medallion_layers.c.dataset_urn == urn).values(**row))
            else:
                row["resource"] = resource
                conn.execute(medallion_layers.insert().values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "medallion.set", actor, urn,
                                  {"table": table, "layer": layer, "from": cur.layer if cur else None,
                                   "resource": row["resource"]}, conn=conn)
        self._tag(urn, layer, actor)
        return self.get(urn)

    def remove(self, urn: str, actor: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(medallion_layers).where(medallion_layers.c.dataset_urn == urn)).rowcount:
                raise MedallionError("that table has no layer", 404)
            if self.audit is not None:
                self.audit.record("lakehouse", "medallion.removed", actor, urn, {}, conn=conn)
        self._tag(urn, None, actor)

    def _tag(self, urn: str, layer: Optional[str], actor: str) -> None:
        """The catalog shows the layer: one ``medallion.<layer>`` tag on the dataset, its other tags kept."""
        if self.graph is None:
            return
        from cairn.common.proposals import make_mcp

        tags = [t for t in (self.graph.get_aspect(urn, "globalTags") or {}).get("tags", [])
                if not str(t.get("tag", "")).startswith(TAG)]
        if layer:
            tags.append({"tag": TAG + layer, "attribution": {"time": int(time.time() * 1000), "actor": actor,
                                                             "source": "urn:li:dataHubAction:cairn-medallion"}})
        self.graph.emit([make_mcp(urn, "globalTags", {"tags": tags}, system_properties={"cairn_source": "medallion"})])

    def get(self, urn: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(medallion_layers).where(medallion_layers.c.dataset_urn == urn)).first()
        if r is None:
            raise MedallionError("that table has no layer", 404)
        return self._public(r)

    def layers(self, urns: list[str]) -> dict[str, str]:
        if not urns:
            return {}
        with self.engine.connect() as conn:
            return {r.dataset_urn: r.layer for r in conn.execute(
                select(medallion_layers.c.dataset_urn, medallion_layers.c.layer)
                .where(medallion_layers.c.dataset_urn.in_(urns)))}

    def list(self, layer: Optional[str] = None) -> list[dict]:
        q = select(medallion_layers).order_by(medallion_layers.c.table_name)
        if layer:
            q = q.where(medallion_layers.c.layer == layer)
        with self.engine.connect() as conn:
            rows = [self._public(r) for r in conn.execute(q)]
        for r in rows:
            r["gold_ready"], r["problems"] = self._ready(r["dataset"])
        return rows

    def _ready(self, urn: str) -> tuple[Optional[bool], list[str]]:
        try:
            why = self.problems(urn)
        except Exception as e:  # noqa: BLE001 - shown, never hidden as ready
            return False, [f"could not be checked: {e}"]
        return not why, why

    @staticmethod
    def _public(r) -> dict:
        return {"dataset": r.dataset_urn, "table": r.table_name, "layer": r.layer, "resource": r.resource,
                "tag": TAG + r.layer, "set_by": r.set_by, "set_ms": r.set_ms}

    # ---- used by pipes, streams and derived tables ----------------------------------------------
    def before(self, body: dict, urn: str) -> Optional[str]:
        """The layer a definition asks for, checked against the gold rules before anything is written."""
        layer = requested(body)
        if layer:
            self.check(urn, layer)
        return layer

    def after(self, layer: Optional[str], urn: str, table: str, actor: str, resource: str) -> None:
        if layer:
            self.set(urn, table, layer, actor, resource)


def layer_before(svc, body: dict, target: str) -> Optional[str]:
    """For a pipe's or stream's definition (``svc`` has ``medallion`` and ``urn``): the layer it asks for, checked."""
    m = getattr(svc, "medallion", None)
    if m is None:
        if requested(body):
            raise MedallionError("medallion layers are not available on this deployment", 503)
        return None
    return m.before(body, svc.urn(*target.split(".")))


def layer_after(svc, layer: Optional[str], target: str, actor: str, resource: str) -> None:
    if layer:
        svc.medallion.after(layer, svc.urn(*target.split(".")), target, actor, resource)


def with_layers(svc, rows: list[dict]) -> list[dict]:
    """``rows`` (definitions with a ``target``) with the ``layer`` of each target."""
    m = getattr(svc, "medallion", None)
    if m is not None:
        layers = m.layers([svc.urn(*r["target"].split(".")) for r in rows])
        for r in rows:
            r["layer"] = layers.get(svc.urn(*r["target"].split(".")))
    return rows


def build_medallion_router(m: Medallion, current_principal, authorize, urn_of: Callable[[str, str], str]) -> Any:
    """``urn_of(namespace, table)``: the lakehouse table's dataset URN (as pipes, streams and derived tables name it)."""
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/medallion", tags=["lakehouse"])

    def wrap(fn):
        try:
            return fn()
        except PipeError as e:
            raise HTTPException(e.status, str(e))

    def urn(table: str) -> str:
        if not TARGET.match(table):
            raise HTTPException(400, "the table is namespace.table")
        ns, _, name = table.rpartition(".")
        return urn_of(ns, name)

    @router.get("")
    def list_layers(layer: Optional[str] = None, p=Depends(current_principal)):
        return m.list(layer)

    @router.get("/{table}")
    def get(table: str, p=Depends(current_principal)):
        u = urn(table)
        out = wrap(lambda: m.get(u))
        out["gold_ready"], out["problems"] = m._ready(u)
        return out

    @router.put("/{table}")
    def set_layer(table: str, body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return wrap(lambda: m.set(urn(table), table, str(body.get("layer") or ""), p.urn))

    @router.delete("/{table}", status_code=204)
    def remove(table: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        wrap(lambda: m.remove(urn(table), p.urn))

    return router
