"""Webhook sink for the e2e suite: records JSON POSTs, returns them on GET (port 8099)."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

EVENTS = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            EVENTS.append(json.loads(body))
        except Exception:
            EVENTS.append({"raw": body.decode(errors="replace")})
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        out = json.dumps(EVENTS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 8099), H).serve_forever()
