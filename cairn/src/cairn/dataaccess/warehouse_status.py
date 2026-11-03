"""Warehouse status for Cairn's UI (``GET /v1/warehouses``): asks the warehouse proxy about each team
warehouse (running or stopped, idle for how long, how often started and stopped).

Settings: ``CAIRN_WAREHOUSES_URL`` (the proxy, e.g. ``https://cairn-warehouses``; each warehouse answers on
its own port) and ``CAIRN_WAREHOUSE_PORTS`` (``{"<name>": <port>}``), set by the chart. People see the
warehouses they may use (``USE_WAREHOUSE``); operators see all.

``POST /v1/warehouses/<name>/start`` starts one now (``USE_WAREHOUSE``), through the proxy with
``CAIRN_WAREHOUSES_CONTROL_KEY``. ``POST /v1/warehouses/usage`` is where the proxy reports node-seconds
(``X-Cairn-Trino-Key``); each report is one row of the cost report (engine ``warehouse``, job
``urn:li:warehouse:<name>``) at ``CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR``, recorded once by its id.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from typing import Any, Optional
from urllib.parse import urlsplit


class WarehouseStatus:
    def __init__(self, proxy_url: str, ports: dict[str, int], verify: Any = True, control_key: str = ""):
        self.proxy_url, self.ports, self.verify = proxy_url.rstrip("/"), dict(ports), verify
        self.control_key = control_key

    @classmethod
    def from_env(cls) -> Optional["WarehouseStatus"]:
        url, raw = os.environ.get("CAIRN_WAREHOUSES_URL", ""), os.environ.get("CAIRN_WAREHOUSE_PORTS", "")
        if not url or not raw:
            return None
        return cls(url, {str(k): int(v) for k, v in json.loads(raw).items()}, os.environ.get("CAIRN_WAREHOUSES_CA_FILE") or True,
                   os.environ.get("CAIRN_WAREHOUSES_CONTROL_KEY", ""))

    def _url(self, port: int) -> str:
        u = urlsplit(self.proxy_url)
        return f"{u.scheme}://{u.hostname}:{port}/cairn/warehouse"

    async def one(self, client, name: str, port: int) -> dict:
        base = {"name": name, "port": port}
        try:
            r = await client.get(self._url(port), timeout=5)
            r.raise_for_status()
            d = r.json()
        except Exception as e:  # noqa: BLE001 - the proxy is down or the port is wrong: shown as such
            return {**base, "state": "unreachable", "error": str(e)[:300]}
        state = "running" if d.get("ready") else ("starting" if d.get("running") else "stopped")
        out = {**base, "state": state, "starts": d.get("starts"), "stops": d.get("stops"), "inflight": d.get("inflight"),
               "idle_minutes": d.get("idle_minutes"), "idle_seconds": d.get("idle_seconds"),
               "queries_running": d.get("queries_running"), "queries_queued": d.get("queries_queued")}
        if d.get("workers"):
            out["workers"] = d["workers"]
        return out

    async def start(self, name: str) -> dict:
        import httpx

        if not self.control_key:
            raise ValueError("starting warehouses by hand needs CAIRN_WAREHOUSES_CONTROL_KEY")
        async with httpx.AsyncClient(verify=self.verify) as c:
            r = await c.post(self._url(self.ports[name]) + "/start", headers={"X-Cairn-Warehouse-Key": self.control_key},
                             timeout=15)
        if r.status_code != 202:
            raise RuntimeError(f"the warehouse proxy answered {r.status_code}: {r.text[:200]}")
        return {"name": name, "starting": True}

    async def all(self, names: list[str]) -> list[dict]:
        import httpx

        async with httpx.AsyncClient(verify=self.verify) as c:
            return list(await asyncio.gather(*(self.one(c, n, self.ports[n]) for n in sorted(names) if n in self.ports)))


USAGE_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def build_warehouses_router(status: WarehouseStatus, current_principal, authorize, audit=None, costs=None,
                            key: str = "") -> Any:
    from fastapi import APIRouter, Body, Depends, Header, HTTPException

    router = APIRouter(prefix="/v1/warehouses", tags=["trino"])

    def may(p, privilege: str, urn: Optional[str]) -> bool:
        try:
            authorize(p, privilege, urn)
            return True
        except HTTPException:
            return False

    @router.get("")
    async def list_warehouses(p=Depends(current_principal)):
        operator = may(p, "MANAGE_SYSTEM_OPERATIONS", None)
        names = [n for n in status.ports if operator or may(p, "USE_WAREHOUSE", f"urn:li:warehouse:{n}")]
        return await status.all(names)

    @router.post("/usage")
    def usage(id: str = Body(...), warehouse: str = Body(...), start_ms: int = Body(...), end_ms: int = Body(...),
              node_seconds: float = Body(...), x_cairn_trino_key: str = Header("")):
        """The warehouse proxy reports node-seconds; one cost row per report, recorded once by its id."""
        if not key or not hmac.compare_digest(x_cairn_trino_key.encode(), key.encode()):
            raise HTTPException(404)
        if warehouse not in status.ports or not USAGE_ID.match(id) or not 0 <= start_ms <= end_ms or node_seconds < 0:
            raise HTTPException(422, "a report names a known warehouse, an id, and its period")
        if costs is None:
            return {"recorded": False}
        seconds = max(1e-3, (end_ms - start_ms) / 1000)
        rate = float(os.environ.get("CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR", "0") or 0)
        out = costs.record_run(f"warehouse:{id}", None, f"urn:li:warehouse:{warehouse}", "warehouse", start_ms, end_ms,
                               [], node_seconds / seconds, rate)
        return {"recorded": out is not None}

    @router.post("/{name}/start")
    async def start(name: str, p=Depends(current_principal)):
        """Starts a stopped warehouse now, so the first query does not wait for it."""
        if name not in status.ports:
            raise HTTPException(404, "no such warehouse")
        if not (may(p, "MANAGE_SYSTEM_OPERATIONS", None) or may(p, "USE_WAREHOUSE", f"urn:li:warehouse:{name}")):
            raise HTTPException(403, f"starting {name} needs USE_WAREHOUSE on it")
        try:
            out = await status.start(name)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except Exception as e:  # noqa: BLE001 - the proxy is down
            raise HTTPException(502, f"the warehouse proxy: {e}")
        if audit is not None:
            audit.record("trino", "warehouse.started", p.urn, f"urn:li:warehouse:{name}", {"by_hand": True})
        return out

    return router
