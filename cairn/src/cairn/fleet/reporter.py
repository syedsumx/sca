"""An install's check-in to its vendor's fleet console (L161): outbound only, signed, and a fixed list of facts.

Configuration (Helm ``cairn.fleet``): ``CAIRN_FLEET_URL`` (the console), ``CAIRN_FLEET_INSTALL_ID`` and
``CAIRN_FLEET_KEY_FILE`` (the install's check-in key, from a Kubernetes Secret), and ``CAIRN_FLEET_HEALTH_URLS``
(``{"api": "http://…/health", …}``: what the scheduler asks to report components' health; the status page's
components are used when one is configured). The scheduler's leader checks in every
``CAIRN_FLEET_EVERY_MINUTES`` (60).

What is sent is :data:`FIELDS`, and only that: the version and channel, the update the feed offers and the last
update's outcome, the licence's id, edition, state, expiry and seats in use, each component's health, and the
Kubernetes and Helm versions. Never data, names of data or of people, queries or settings. ``GET
/v1/system/fleet`` (``MANAGE_SYSTEM_OPERATIONS``) shows the exact body the next check-in sends and how the last one
went, so an operator can see for themselves what leaves their network.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, select
from sqlalchemy.engine import Engine

from cairn.common.db import metadata
from cairn.fleet.common import sign

logger = logging.getLogger(__name__)

FIELDS = ("install", "sent_ms", "version", "channel", "licence", "updates", "health", "platform")

state_table = Table(
    "cairn_fleet_report", metadata,
    Column("id", Integer, primary_key=True),             # one row, id 1
    Column("sent_ms", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),        # sent | refused | unreachable
    Column("detail", Text, nullable=True),
)

OK_STATES = {"ok": "ok", "operational": "ok", "up": "ok", "healthy": "ok", "degraded": "degraded",
             "partial_outage": "down", "major_outage": "down", "outage": "down", "down": "down"}


def _now_ms() -> int:
    return int(time.time() * 1000)


class FleetReporter:
    def __init__(self, engine: Engine, url: str, install_id: str, key: str, services=None,
                 health_urls: Optional[dict] = None, every_minutes: int = 60, http=None):
        from urllib.parse import urlsplit

        u = urlsplit(url)
        if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))):
            raise ValueError("the fleet console is an https:// URL (plain http only to this machine)")
        self.engine, self.url, self.install_id, self.key = engine, url.rstrip("/"), install_id, key
        self.services, self.health_urls = services, dict(health_urls or {})
        self.every_ms = max(5, every_minutes) * 60 * 1000
        if http is None:
            import httpx

            http = httpx.Client(timeout=20, follow_redirects=False, headers={"User-Agent": "cairn-fleet"})
        self.http = http

    @classmethod
    def from_env(cls, engine: Engine, services=None) -> Optional["FleetReporter"]:
        url = os.environ.get("CAIRN_FLEET_URL", "").strip()
        if not url:
            return None
        iid = os.environ.get("CAIRN_FLEET_INSTALL_ID", "").strip()
        key_file = os.environ.get("CAIRN_FLEET_KEY_FILE", "").strip()
        if not iid or not key_file:
            raise ValueError("CAIRN_FLEET_URL needs CAIRN_FLEET_INSTALL_ID and CAIRN_FLEET_KEY_FILE")
        return cls(engine, url, iid, Path(key_file).read_text().strip(), services,
                   json.loads(os.environ.get("CAIRN_FLEET_HEALTH_URLS") or "{}"),
                   int(os.environ.get("CAIRN_FLEET_EVERY_MINUTES", "60") or 60))

    # ---- what is sent --------------------------------------------------------------------------------------
    def _health(self) -> dict:
        s = self.services
        page = getattr(s, "status_page", None) if s is not None else None
        if page is not None:
            try:
                return {c["name"]: OK_STATES.get(c["state"], "degraded") for c in page.summary()["components"]}
            except Exception:   # noqa: BLE001 - a status page that cannot answer is not a reason to stay silent
                logger.exception("fleet: the status page could not be read")
        out = {}
        for name, url in self.health_urls.items():
            try:
                r = self.http.get(url, timeout=5)
                out[name] = "ok" if r.status_code == 200 and r.json().get("status", "ok") == "ok" else "down"
            except Exception:   # noqa: BLE001 - unreachable is the answer
                out[name] = "down"
        return out

    def body(self) -> dict:
        from cairn import __version__

        s = self.services
        lic_state = s.licence.state() if s is not None and getattr(s, "licence", None) is not None else {}
        lic = lic_state.get("licence") or {}
        feed = s.release_feed.state() if s is not None and getattr(s, "release_feed", None) is not None else {}
        plans = s.release_updates.plans(20) if s is not None and getattr(s, "release_updates", None) is not None else []
        finished = next((p for p in plans if p["status"] in ("succeeded", "rolled_back", "failed")), None)
        return {
            "install": self.install_id, "sent_ms": _now_ms(), "version": __version__,
            "channel": feed.get("channel") or os.environ.get("CAIRN_RELEASE_CHANNEL", "stable"),
            "licence": {"id": lic.get("id"), "edition": lic.get("edition"), "status": lic_state.get("status"),
                        "expires_at": lic.get("expires_at"), "days_left": lic_state.get("days_left"),
                        "seats_used": lic_state.get("seats_used"), "seats_limit": lic_state.get("seats_limit"),
                        "over_seats": bool(lic_state.get("over_seats")), "read_only": bool(lic_state.get("read_only"))},
            "updates": {"target": feed.get("target"), "severity": feed.get("severity"),
                        "checked_at": feed.get("checked_at"), "error": (feed.get("error") or None) and "feed did not verify",
                        "blocked": bool(feed.get("blocked")) or None,
                        "last_plan": ({"target": finished["target"], "status": finished["status"],
                                       "finished_at": finished["finished_at"]} if finished else {})},
            "health": {"components": self._health()},
            "platform": {"runtime": "kubernetes" if os.environ.get("KUBERNETES_SERVICE_HOST") else "other"},
        }

    # ---- sending -------------------------------------------------------------------------------------------
    def _record(self, status: str, detail: str) -> None:
        with self.engine.begin() as c:
            values = dict(sent_ms=_now_ms(), status=status, detail=detail[:2000])
            if c.execute(select(state_table.c.id).where(state_table.c.id == 1)).first():
                c.execute(state_table.update().where(state_table.c.id == 1).values(**values))
            else:
                c.execute(state_table.insert().values(id=1, **values))

    def last(self) -> Optional[dict]:
        with self.engine.connect() as c:
            r = c.execute(select(state_table).where(state_table.c.id == 1)).mappings().first()
        return dict(r) if r else None

    def send(self) -> str:
        raw = json.dumps(self.body(), separators=(",", ":")).encode()
        try:
            r = self.http.post(f"{self.url}/fleet/v1/checkin", content=raw,
                               headers={"Content-Type": "application/json", "X-Cairn-Signature": sign(self.key, raw)})
        except Exception as e:   # noqa: BLE001 - it tries again next time
            self._record("unreachable", type(e).__name__)
            return "unreachable"
        if r.status_code != 200:
            self._record("refused", f"{r.status_code} {r.text[:300]}")
            return "refused"
        self._record("sent", ", ".join(r.json().get("attention") or []) or "nothing needs attention")
        return "sent"

    def run_due(self) -> Optional[str]:
        """The scheduler's tick: check in when the last one is older than the interval."""
        last = self.last()
        if last is not None and _now_ms() - last["sent_ms"] < self.every_ms:
            return None
        return self.send()


def build_router(reporter: FleetReporter, current_principal, authorize):
    from fastapi import APIRouter, Depends, HTTPException

    router = APIRouter(tags=["updates"])

    @router.get("/v1/system/fleet")
    def fleet(p=Depends(current_principal)) -> dict[str, Any]:
        """Exactly what this install tells its vendor's fleet console, and how the last check-in went."""
        if authorize is None or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "this needs MANAGE_SYSTEM_OPERATIONS")
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        last = reporter.last()
        return {"console": reporter.url, "install": reporter.install_id, "next_body": reporter.body(),
                "last": {"status": last["status"], "detail": last["detail"],
                         "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last["sent_ms"] / 1000))}
                if last else None}

    return router
