"""Status page: whether each part of the platform is up, how it has been over 90 days, and the incidents and
maintenance its operators announce.

Components are probed from ``CAIRN_STATUS`` (JSON, or a file holding it)::

    {"title": "Cairn status", "components": [
      {"name": "API", "url": "https://acme.cairn.example/cairn/api/health"},
      {"name": "Lakehouse storage", "url": "http://cairn-store:9000/-/healthy", "slow_ms": 1500},
      {"name": "Catalog", "url": "http://cairn-catalog:8181/-/ready"}]}

* **Probes.** The notifier checks each component every ``every_seconds`` (60): an HTTP GET that must answer
  2xx within ``timeout_ms`` (5,000). A component is *operational*, *degraded* (it answered, slower than
  ``slow_ms``, or one of its last three checks failed) or in *outage* (its last two checks failed). Checks are
  kept 90 days; each day's uptime is the share of its checks that succeeded.
* **Incidents and maintenance** (``MANAGE_SYSTEM_OPERATIONS``): ``POST /v1/status/incidents`` with ``title``,
  ``components``, ``impact`` (``minor``, ``major``, ``critical`` or ``maintenance``, with ``starts_ms`` and
  ``ends_ms`` for maintenance) and a first ``message``; ``POST /v1/status/incidents/<id>/updates`` adds an
  update (``investigating``, ``identified``, ``monitoring``, ``resolved``). An open incident sets its
  components' state at least to its impact; maintenance does while it runs.
* **Public**, without signing in: ``GET /status`` (a page that needs no script), ``GET /status.json`` (the
  overall state, each component's state and 90-day uptime, open incidents and maintenance) and
  ``GET /status/feed.atom`` (incidents and their updates, to subscribe to). Nothing about the platform beyond
  what is configured here and what operators write is shown.
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

IMPACTS = ("minor", "major", "critical", "maintenance")
PHASES = ("investigating", "identified", "monitoring", "resolved", "scheduled", "in_progress", "completed")
RANK = {"operational": 0, "maintenance": 1, "degraded": 2, "partial_outage": 3, "outage": 4}
IMPACT_STATE = {"minor": "degraded", "major": "partial_outage", "critical": "outage", "maintenance": "maintenance"}
KEEP_DAYS = 90

status_checks = Table(
    "cairn_status_checks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("component", String(128), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False, index=True),
    Column("ok", Boolean, nullable=False),
    Column("latency_ms", Integer, nullable=True),
    Column("error", String(500), nullable=True),
)

status_incidents = Table(
    "cairn_status_incidents", metadata,
    Column("id", String(32), primary_key=True),
    Column("title", String(200), nullable=False),
    Column("impact", String(16), nullable=False),
    Column("components", Text, nullable=False),               # JSON list of component names
    Column("starts_ms", BigInteger, nullable=False),
    Column("ends_ms", BigInteger, nullable=True),             # maintenance: its planned end
    Column("resolved_ms", BigInteger, nullable=True),
    Column("created_by", String(256), nullable=False),
    Column("updates", Text, nullable=False),                  # JSON list of {ts_ms, phase, message, by}
)


class StatusError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def load_config(raw: Optional[str] = None) -> Optional[dict]:
    raw = (raw if raw is not None else os.environ.get("CAIRN_STATUS", "")).strip()
    if not raw:
        return None
    cfg = json.loads(open(raw).read() if not raw.startswith("{") else raw)
    comps = cfg.get("components")
    if not isinstance(comps, list) or not comps:
        raise ValueError("CAIRN_STATUS: components")
    names = set()
    for c in comps:
        if not isinstance(c.get("name"), str) or not c["name"] or c["name"] in names or len(c["name"]) > 128:
            raise ValueError("CAIRN_STATUS: each component has a unique name")
        if not str(c.get("url", "")).startswith(("http://", "https://")):
            raise ValueError(f"CAIRN_STATUS: {c['name']}: url")
        names.add(c["name"])
    return cfg


class StatusPage:
    def __init__(self, engine, cfg: dict, http=None, audit=None):
        self.engine, self.cfg, self.http, self.audit = engine, cfg, http, audit
        self.components = [c["name"] for c in cfg["components"]]
        self._last_probe = 0

    # ---- probes ---------------------------------------------------------------------------------
    def probe(self, now_ms: Optional[int] = None, force: bool = False) -> dict:
        import httpx

        now = now_ms or _now()
        if not force and now - self._last_probe < int(self.cfg.get("every_seconds", 60)) * 1000:
            return {}
        self._last_probe = now
        out = {}
        client = self.http or httpx.Client(timeout=int(self.cfg.get("timeout_ms", 5000)) / 1000, follow_redirects=False)
        try:
            for c in self.cfg["components"]:
                started = time.monotonic()
                ok, err = False, None
                try:
                    r = client.get(c["url"])
                    ok = 200 <= r.status_code < 300
                    err = None if ok else f"HTTP {r.status_code}"
                except Exception as e:  # noqa: BLE001 - a failed check, recorded
                    err = type(e).__name__ + (f": {e}" if str(e) else "")
                latency = int((time.monotonic() - started) * 1000)
                with self.engine.begin() as conn:
                    conn.execute(insert(status_checks).values(component=c["name"], ts_ms=now, ok=ok, latency_ms=latency,
                                                              error=(err or None) and err[:500]))
                out[c["name"]] = {"ok": ok, "latency_ms": latency, "error": err}
        finally:
            if self.http is None:
                client.close()
        with self.engine.begin() as conn:
            conn.execute(delete(status_checks).where(status_checks.c.ts_ms < now - (KEEP_DAYS + 1) * 86_400_000))
        return out

    def _measured(self, name: str, slow_ms: Optional[int]) -> str:
        with self.engine.connect() as conn:
            last = conn.execute(select(status_checks.c.ok, status_checks.c.latency_ms).where(status_checks.c.component == name)
                                .order_by(status_checks.c.ts_ms.desc(), status_checks.c.id.desc()).limit(3)).all()
        if not last:
            return "operational"
        if len(last) >= 2 and not last[0].ok and not last[1].ok:
            return "outage"
        if not all(r.ok for r in last) or (slow_ms and last[0].latency_ms and last[0].latency_ms > slow_ms):
            return "degraded"
        return "operational"

    def uptime(self, name: str, now_ms: int, days: int = KEEP_DAYS) -> list[dict]:
        since = now_ms - days * 86_400_000
        per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        with self.engine.connect() as conn:
            for ok, ts in conn.execute(select(status_checks.c.ok, status_checks.c.ts_ms).where(
                    status_checks.c.component == name, status_checks.c.ts_ms >= since)):
                d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
                per[d][0] += 1 if ok else 0
                per[d][1] += 1
        return [{"day": d, "uptime": round(100 * a / n, 3), "checks": n} for d, (a, n) in sorted(per.items())]

    # ---- incidents --------------------------------------------------------------------------------
    def _incident(self, r) -> dict:
        d = dict(r._mapping)
        return {**d, "components": json.loads(d["components"]), "updates": json.loads(d["updates"])}

    def incidents(self, now_ms: Optional[int] = None, days: int = KEEP_DAYS) -> list[dict]:
        now = now_ms or _now()
        with self.engine.connect() as conn:
            rows = conn.execute(select(status_incidents).where(status_incidents.c.starts_ms >= now - days * 86_400_000)
                                .order_by(status_incidents.c.starts_ms.desc())).all()
        return [self._incident(r) for r in rows]

    def open_incident(self, actor: str, body: dict) -> dict:
        title, impact = str(body.get("title") or "").strip(), body.get("impact")
        comps, message = body.get("components") or [], str(body.get("message") or "").strip()
        if not 1 <= len(title) <= 200:
            raise StatusError("title: up to 200 characters")
        if impact not in IMPACTS:
            raise StatusError(f"impact: one of {', '.join(IMPACTS)}")
        if not isinstance(comps, list) or not comps or any(c not in self.components for c in comps):
            raise StatusError(f"components: some of {', '.join(self.components)}")
        if not message:
            raise StatusError("message: what people should know")
        now = _now()
        starts, ends = body.get("starts_ms") or now, body.get("ends_ms")
        if impact == "maintenance":
            if not (isinstance(starts, int) and isinstance(ends, int) and starts < ends):
                raise StatusError("maintenance: starts_ms and ends_ms")
        elif ends is not None:
            raise StatusError("only maintenance has ends_ms")
        phase = "scheduled" if impact == "maintenance" else "investigating"
        iid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(status_incidents).values(
                id=iid, title=title, impact=impact, components=json.dumps(comps), starts_ms=starts, ends_ms=ends,
                created_by=actor, updates=json.dumps([{"ts_ms": now, "phase": phase, "message": message}])))
        if self.audit is not None:
            self.audit.record("status", "status.incident_opened", actor, None, {"id": iid, "title": title, "impact": impact})
        return self.get(iid)

    def get(self, iid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(status_incidents).where(status_incidents.c.id == iid)).first()
        if r is None:
            raise StatusError("no such incident", 404)
        return self._incident(r)

    def add_update(self, actor: str, iid: str, body: dict) -> dict:
        inc = self.get(iid)
        phase, message = body.get("phase"), str(body.get("message") or "").strip()
        if phase not in PHASES or not message:
            raise StatusError(f"phase (one of {', '.join(PHASES)}) and message")
        if inc["resolved_ms"]:
            raise StatusError("the incident is closed", 409)
        now = _now()
        updates = inc["updates"] + [{"ts_ms": now, "phase": phase, "message": message}]
        done = phase in ("resolved", "completed")
        with self.engine.begin() as conn:
            conn.execute(update(status_incidents).where(status_incidents.c.id == iid).values(
                updates=json.dumps(updates), resolved_ms=now if done else None))
        if self.audit is not None:
            self.audit.record("status", "status.incident_updated", actor, None, {"id": iid, "phase": phase})
        return self.get(iid)

    # ---- the page ---------------------------------------------------------------------------------
    def summary(self, now_ms: Optional[int] = None) -> dict:
        now = now_ms or _now()
        incidents = self.incidents(now)
        active = [i for i in incidents if not i["resolved_ms"] and (
            i["impact"] != "maintenance" or i["starts_ms"] <= now < (i["ends_ms"] or 0))]
        upcoming = [i for i in incidents if i["impact"] == "maintenance" and not i["resolved_ms"] and i["starts_ms"] > now]
        comps = []
        for c in self.cfg["components"]:
            state = self._measured(c["name"], c.get("slow_ms"))
            for i in active:
                if c["name"] in i["components"] and RANK[IMPACT_STATE[i["impact"]]] > RANK[state]:
                    state = IMPACT_STATE[i["impact"]]
            days = self.uptime(c["name"], now)
            checks = sum(d["checks"] for d in days)
            up = round(sum(d["uptime"] * d["checks"] for d in days) / checks, 3) if checks else None
            comps.append({"name": c["name"], "state": state, "uptime_90d": up, "days": days})
        worst = max((c["state"] for c in comps), key=lambda s: RANK[s], default="operational")
        return {"title": self.cfg.get("title", "Status"), "state": worst, "updated_ms": now, "components": comps,
                "incidents": [i for i in active if i["impact"] != "maintenance"],
                "maintenance": [i for i in active if i["impact"] == "maintenance"] + upcoming,
                "history": [i for i in incidents if i["resolved_ms"]][:20]}

    def html(self, now_ms: Optional[int] = None) -> str:
        s = self.summary(now_ms)
        e = html.escape
        words = {"operational": "Operational", "degraded": "Degraded performance", "partial_outage": "Partial outage",
                 "outage": "Major outage", "maintenance": "Under maintenance"}
        colour = {"operational": "#1a7f37", "degraded": "#9a6700", "partial_outage": "#bc4c00", "outage": "#cf222e",
                  "maintenance": "#0969da"}
        when = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")  # noqa: E731
        overall = "All systems operational" if s["state"] == "operational" else words[s["state"]]
        out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
               f"<title>{e(s['title'])}</title><link rel=alternate type=application/atom+xml href='status/feed.atom'>"
               "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1f2328}"
               ".bar{display:flex;gap:1px}.bar i{flex:1;height:28px;border-radius:2px}.c{border:1px solid #d0d7de;border-radius:8px;"
               "padding:12px 16px;margin:12px 0}.st{float:right;font-weight:600}h1{font-size:1.6rem}"
               "small{color:#59636e}</style></head><body>",
               f"<h1>{e(s['title'])}</h1>",
               f"<div class=c style='background:{colour[s['state']]};color:#fff;font-weight:600' role=status>{e(overall)}</div>"]
        for group, label in ((s["incidents"], "Incidents"), (s["maintenance"], "Maintenance")):
            for i in group:
                last = i["updates"][-1]
                what = f"{label.lower()[:-1]}, {', '.join(i['components'])}"
                out.append(f"<div class=c><strong>{e(i['title'])}</strong> <small>({e(what)})</small>"
                           f"<p><b>{e(last['phase'].replace('_', ' ').title())}</b> — {e(last['message'])} "
                           f"<small>{when(last['ts_ms'])}</small></p></div>")
        today = datetime.fromtimestamp(s["updated_ms"] / 1000, tz=timezone.utc).date()
        span = [(today - timedelta(days=n)).isoformat() for n in range(KEEP_DAYS - 1, -1, -1)]
        for c in s["components"]:
            by_day = {d["day"]: d for d in c["days"]}

            def cell(day: str, by_day: dict = by_day) -> str:
                d = by_day.get(day)
                if d is None:
                    return f"<i title='{day}: no data' style='background:#d0d7de'></i>"
                tone = "#1a7f37" if d["uptime"] >= 99.9 else "#9a6700" if d["uptime"] >= 99 else "#cf222e"
                return f"<i title='{day}: {d['uptime']}%' style='background:{tone}'></i>"
            cells = "".join(cell(day) for day in span)
            up = f"{c['uptime_90d']}% uptime" if c["uptime_90d"] is not None else "no checks yet"
            out.append(f"<div class=c><span class=st style='color:{colour[c['state']]}'>{e(words[c['state']])}</span>"
                       f"<strong>{e(c['name'])}</strong><div class=bar aria-label='{e(c['name'])} daily uptime'>{cells}</div>"
                       f"<small>{e(up)}, last 90 days</small></div>")
        if s["history"]:
            out.append("<h2>Past incidents</h2>")
            for i in s["history"]:
                out.append(f"<div class=c><strong>{e(i['title'])}</strong> <small>{when(i['starts_ms'])}</small><ul>"
                           + "".join(f"<li><b>{e(u['phase'].title())}</b> — {e(u['message'])} <small>{when(u['ts_ms'])}</small></li>"
                                     for u in reversed(i["updates"])) + "</ul></div>")
        out.append(f"<p><small>Updated {when(s['updated_ms'])} · <a href='status.json'>JSON</a> · "
                   "<a href='status/feed.atom'>Subscribe</a></small></p></body></html>")
        return "".join(out)

    def atom(self, base_url: str, now_ms: Optional[int] = None) -> str:
        e = html.escape
        iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        items = self.incidents(now_ms)
        updated = max([u["ts_ms"] for i in items for u in i["updates"]] or [now_ms or _now()])
        out = ['<?xml version="1.0" encoding="utf-8"?>', '<feed xmlns="http://www.w3.org/2005/Atom">',
               f"<title>{e(self.cfg.get('title', 'Status'))}</title>", f"<id>{e(base_url)}/status</id>",
               f'<link href="{e(base_url)}/status"/>', f"<updated>{iso(updated)}</updated>"]
        for i in items:
            body = "".join(f"<p><b>{e(u['phase'])}</b> ({iso(u['ts_ms'])}): {e(u['message'])}</p>" for u in reversed(i["updates"]))
            out += ["<entry>", f"<title>{e(i['title'])}</title>", f"<id>urn:cairn:incident:{i['id']}</id>",
                    f"<updated>{iso(i['updates'][-1]['ts_ms'])}</updated>", f'<link href="{e(base_url)}/status"/>',
                    f'<content type="html">{e(body)}</content>', "</entry>"]
        out.append("</feed>")
        return "\n".join(out)


def build_status_router(page: StatusPage, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import HTMLResponse, Response

    router = APIRouter(tags=["status"])

    def wrap(fn):
        try:
            return fn()
        except StatusError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/status", response_class=HTMLResponse, include_in_schema=False)
    def page_html():
        return HTMLResponse(page.html(), headers={"Cache-Control": "public, max-age=30"})

    @router.get("/status.json")
    def summary():
        out = page.summary()
        for c in out["components"]:
            c.pop("days", None)
        return out

    @router.get("/status/feed.atom", include_in_schema=False)
    def feed():
        base = page.cfg.get("public_url", "").rstrip("/")
        return Response(page.atom(base), media_type="application/atom+xml")

    @router.post("/v1/status/incidents", status_code=201)
    def open_(body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: page.open_incident(p.urn, body))

    @router.post("/v1/status/incidents/{iid}/updates")
    def add(iid: str, body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: page.add_update(p.urn, iid, body))

    return router
