"""Integration doubles for the e2e suite (port 8099, on the stack network as cairn-e2e-sink).

* ``POST /<anything>``: a webhook receiver; the JSON body is recorded.
* ``/slack/api/<method>``: the Slack Web API calls Cairn makes (chat.postMessage, chat.update,
  users.lookupByEmail, users.info), answered like Slack does and recorded.
* ``POST /slack/response/<id>``: a Slack ``response_url``.
* ``POST /teams/<id>``: a Teams Workflows webhook (answers 202 like Teams).
* ``GET /embed.html?edge=&urn=&token=&kind=``: a third-party page embedding Cairn (web component and
  iframe), so a browser can load it from an allowed and a disallowed origin.
* ``GET /``: everything recorded, oldest first; ``GET /slack``, ``/teams`` and ``/webhooks`` filter it.
"""
import hashlib
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

EVENTS = []
EMAILS = {}          # Slack user id -> email, from users.lookupByEmail


def slack_id(email):
    uid = "U" + hashlib.sha1(email.encode()).hexdigest()[:9].upper()
    EMAILS[uid] = email
    return uid


class H(BaseHTTPRequestHandler):
    def _send(self, status, body=None, ctype="application/json"):
        data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.send_response(status)
        if data:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw.decode(errors="replace")}

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path.startswith("/slack/api/"):
            method = path.rsplit("/", 1)[1]
            EVENTS.append({"_double": "slack", "method": method, "body": body,
                           "auth": self.headers.get("Authorization", "")[:12]})
            if method == "chat.postMessage":
                return self._send(200, {"ok": True, "channel": body.get("channel"), "ts": f"1700000000.{len(EVENTS):06d}"})
            return self._send(200, {"ok": True})
        if path.startswith("/slack/response/"):
            EVENTS.append({"_double": "slack_response", "path": path, "body": body})
            return self._send(200, b"ok", "text/plain")
        if path.startswith("/teams/"):
            EVENTS.append({"_double": "teams", "path": path, "body": body})
            return self._send(202)
        EVENTS.append(body)
        self._send(204)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.startswith("/slack/api/"):
            method = u.path.rsplit("/", 1)[1]
            EVENTS.append({"_double": "slack", "method": method, "query": q})
            if method == "users.lookupByEmail":
                return self._send(200, {"ok": True, "user": {"id": slack_id(q.get("email", ""))}})
            if method == "users.info":
                email = EMAILS.get(q.get("user", ""))
                return self._send(200, {"ok": bool(email), "error": None if email else "user_not_found",
                                        "user": {"id": q.get("user"), "profile": {"email": email}}})
            return self._send(200, {"ok": False, "error": "unknown_method"})
        if u.path == "/embed.html":
            edge, urn, token = (html.escape(q.get(k, ""), quote=True) for k in ("edge", "urn", "token"))
            kind = {"card": "cairn-asset-card", "quality": "cairn-quality-badge"}.get(q.get("kind", "card"))
            page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Partner portal</title>
<script src="{edge}/cairn-ui/embed/sdk.js"></script></head><body>
<h1>Partner portal</h1>
<{kind} id="component" urn="{urn}" token="{token}" base-url="{edge}/cairn/api/embed/v1"></{kind}>
<iframe id="frame" title="Cairn card" width="480" height="260"
  src="{edge}/cairn/api/embed/v1/frame/card?urn={urn}&amp;token={token}"></iframe>
</body></html>"""
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        kinds = {"/slack": ("slack", "slack_response"), "/teams": ("teams",), "/webhooks": (None,)}
        if u.path in kinds:
            return self._send(200, [e for e in EVENTS if e.get("_double") in kinds[u.path]])
        self._send(200, EVENTS)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("0.0.0.0", 8099), H).serve_forever()
