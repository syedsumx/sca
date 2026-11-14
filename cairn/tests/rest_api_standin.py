"""A small JSON API for the declarative REST source's tests: customers served with each pagination style and each
way of sending a key. It answers real HTTP on a local port (the source's requests, headers, paging and retries are
real); it is not any vendor's API."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class Api:
    def __init__(self):
        self.customers: dict[str, dict] = {}
        self.token = "rk_live_" + "r" * 24
        self.password = "pw-" + "p" * 16
        self.asked: list[str] = []
        self.fail: list[int] = []                 # statuses to answer first (each once), e.g. [429, 503]
        self.redirect_to: str | None = None
        self.evil_next: str | None = None         # a next-page URL on another host
        self.page_size = 2
        self.shuffle = False                      # answer out of modification order

    def rows(self, since: str | None) -> list[dict]:
        rows = [c for c in self.customers.values() if not since or c["updated_at"] >= since]
        rows.sort(key=lambda c: c["id"] if self.shuffle else (c["updated_at"], c["id"]),
                  reverse=self.shuffle)
        return rows


def serve(api: Api, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            api.asked.append(self.path)
            style = u.path.strip("/").split("/")[0]            # /<auth>-<paging>/customers
            auth, _, paging = style.partition("-")
            basic = "Basic " + base64.b64encode(f"loader:{api.password}".encode()).decode()
            ok = {"bearer": self.headers.get("Authorization") == f"Bearer {api.token}",
                  "header": self.headers.get("X-Api-Key") == api.token,
                  "query": q.get("api_key") == api.token,
                  "basic": self.headers.get("Authorization") == basic, "none": True}.get(auth, False)
            if not ok:
                return self._send(401, {"error": "who are you"})
            if api.fail:
                return self._send(api.fail.pop(0), {"error": "try later"}, {"Retry-After": "0"})
            if api.redirect_to:
                return self._send(302, {}, {"Location": api.redirect_to})
            if not u.path.endswith("/customers"):
                return self._send(404, {"error": "no such resource"})
            if paging == "notjson":
                return self._send(200, b"<html>maintenance</html>")
            rows = api.rows(q.get("updated_since"))
            if q.get("status"):
                rows = [r for r in rows if r.get("status") == q["status"]]
            n = api.page_size
            host = f"http://{self.headers.get('Host')}"
            if paging == "none":
                return self._send(200, rows)                                  # the response is the list
            if paging == "cursor":
                start = int(q.get("starting_after") or 0)
                nxt = start + n if start + n < len(rows) else None
                return self._send(200, {"data": {"items": rows[start:start + n]}, "meta": {"next": nxt}})
            if paging == "offset":
                start, limit = int(q.get("offset") or 0), int(q.get("limit") or n)
                return self._send(200, {"results": rows[start:start + limit]})
            if paging == "page":
                page, size = int(q.get("page") or 1), int(q.get("per_page") or n)
                return self._send(200, {"results": rows[(page - 1) * size:page * size]})
            if paging in ("link", "bodylink"):
                start = int(q.get("start") or 0)
                more = start + n < len(rows)
                keep = "&".join(f"{k}={v}" for k, v in q.items() if k not in ("start", "api_key"))
                nxt = f"{host}{u.path}?start={start + n}" + (f"&{keep}" if keep else "")
                if api.evil_next and more:
                    nxt = api.evil_next
                if paging == "link":
                    head = {"Link": f'<{nxt}>; rel="next", <{host}{u.path}>; rel="first"'} if more else {}
                    return self._send(200, {"results": rows[start:start + n]}, head)
                return self._send(200, {"results": rows[start:start + n], "next": nxt if more else None})
            self._send(404, {"error": "no such style"})

    server = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def customer(i: int, day: int, status: str = "active", city: str = "Lyon", name: str | None = None) -> dict:
    return {"id": f"cus_{i:03d}", "name": name or f"Customer {i}", "status": status,
            "address": {"city": city, "geo": {"lat": 45.7}}, "tags": ["a", "b"], "vip": i % 2 == 0,
            "updated_at": f"2026-11-{day:02d}T10:00:00Z"}
