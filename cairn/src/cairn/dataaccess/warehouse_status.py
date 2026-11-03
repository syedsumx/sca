"""Warehouse status for Cairn's UI (``GET /v1/warehouses``): asks the warehouse proxy about each team
warehouse (running or stopped, idle for how long, how often started and stopped).

Settings: ``CAIRN_WAREHOUSES_URL`` (the proxy, e.g. ``https://cairn-warehouses``; each warehouse answers on
its own port) and ``CAIRN_WAREHOUSE_PORTS`` (``{"<name>": <port>}``), set by the chart. People see the
warehouses they may use (``USE_WAREHOUSE``); operators see all.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional
from urllib.parse import urlsplit


class WarehouseStatus:
    def __init__(self, proxy_url: str, ports: dict[str, int], verify: Any = True):
        self.proxy_url, self.ports, self.verify = proxy_url.rstrip("/"), dict(ports), verify

    @classmethod
    def from_env(cls) -> Optional["WarehouseStatus"]:
        url, raw = os.environ.get("CAIRN_WAREHOUSES_URL", ""), os.environ.get("CAIRN_WAREHOUSE_PORTS", "")
        if not url or not raw:
            return None
        return cls(url, {str(k): int(v) for k, v in json.loads(raw).items()}, os.environ.get("CAIRN_WAREHOUSES_CA_FILE") or True)

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
        return {**base, "state": state, "starts": d.get("starts"), "stops": d.get("stops"), "inflight": d.get("inflight"),
                "idle_minutes": d.get("idle_minutes"), "idle_seconds": d.get("idle_seconds")}

    async def all(self, names: list[str]) -> list[dict]:
        import httpx

        async with httpx.AsyncClient(verify=self.verify) as c:
            return list(await asyncio.gather(*(self.one(c, n, self.ports[n]) for n in sorted(names) if n in self.ports)))


def build_warehouses_router(status: WarehouseStatus, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException

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

    return router
