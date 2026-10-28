#!/usr/bin/env python3
"""Incident log: an example Cairn extension (standard library only).

    CAIRN_API_URL=http://cairn-api:8800 CAIRN_EXTENSION_TOKEN=cx_… CAIRN_SIGNING_SECRET=… python app.py

* ``POST /events``  Cairn's signed webhooks. Unsigned, altered or stale deliveries are refused (401).
* ``GET /ui``       the workspace section (``?urn=`` on asset pages): recent events, newest first.
* ``GET /health``
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API = os.environ.get("CAIRN_API_URL", "http://cairn-api:8800").rstrip("/")
TOKEN = os.environ.get("CAIRN_EXTENSION_TOKEN", "")
SECRET = os.environ.get("CAIRN_SIGNING_SECRET", "")
EVENTS: deque = deque(maxlen=500)
LOCK = threading.Lock()


def signature_ok(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    """``Cairn-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">``, at most five minutes old."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if not secret or abs(time.time() - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


def badges(urn: str) -> dict:
    """The asset's trust badges, read with the extension's own token (VIEW_ENTITY_PAGE only)."""
    if not urn or not TOKEN:
        return {}
    req = urllib.request.Request(f"{API}/collab/enrich", data=json.dumps({"urns": [urn]}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get(urn, {})
    except Exception as e:  # noqa: BLE001 - show the event even if the lookup fails
        return {"error": str(e)[:200]}


def page(urn: str | None) -> str:
    with LOCK:
        rows = [e for e in reversed(EVENTS) if not urn or e.get("entity_urn") == urn]
    cells = "".join(
        f"<tr><td>{html.escape(time.strftime('%Y-%m-%d %H:%M', time.gmtime(e['received'])))}</td>"
        f"<td>{html.escape(e.get('type', ''))}</td><td>{html.escape(e.get('title', ''))}</td>"
        f"<td>{html.escape(e.get('entity_urn') or '')}</td>"
        f"<td>{html.escape(str((e.get('badges') or {}).get('quality_status') or ''))}</td></tr>" for e in rows[:100])
    return ("<!doctype html><meta charset=utf-8><title>Incident log</title>"
            "<style>body{font:14px system-ui;margin:16px}td,th{padding:4px 8px;border-bottom:1px solid #eee;"
            "text-align:left}</style><h2>Incident log</h2>"
            + (f"<table><tr><th>When</th><th>Event</th><th>Title</th><th>Asset</th><th>Quality</th></tr>{cells}</table>"
               if rows else "<p>No events yet.</p>"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if urlparse(self.path).path != "/events":
            return self._send(404, "not found")
        body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1 << 20))
        if not signature_ok(SECRET, body, self.headers.get("Cairn-Signature", "")):
            return self._send(401, "bad signature")
        event = json.loads(body)
        event["received"] = time.time()
        event["badges"] = badges(event.get("entity_urn") or "")
        with LOCK:
            EVENTS.append(event)
        self._send(204, "")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, json.dumps({"ok": True, "events": len(EVENTS)}), "application/json")
        if u.path == "/events.json":
            with LOCK:
                return self._send(200, json.dumps(list(EVENTS)), "application/json")
        if u.path == "/ui":
            return self._send(200, page(parse_qs(u.query).get("urn", [None])[0]), "text/html; charset=utf-8")
        self._send(404, "not found")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8098"))), Handler).serve_forever()
