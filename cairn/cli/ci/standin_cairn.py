"""A stand-in for the two Cairn endpoints `cairn login --token-stdin` and `cairn whoami` use (/health and /me), for
the release builds' keychain smoke test (keychain-smoke.sh) on runners where a real Cairn can't run (macOS,
Windows). Python's standard library only; listens on 127.0.0.1.

    python standin_cairn.py --token-file TOKEN --port-file PORT

It accepts one bearer token, read from --token-file (never from the command line), and writes the port it listens on
to --port-file once it is ready. It logs nothing: no request, and never the token.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import socketserver
import json
import os

USER = "urn:li:corpuser:keychain-smoke"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--port-file", required=True)
    a = ap.parse_args()
    with open(a.token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token:
        raise SystemExit("standin_cairn: the token file is empty")
    expected = f"Bearer {token}".encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict) -> None:
            out = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):  # noqa: N802 - http.server's name
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/cairn/api/health"):
                return self._json(200, {"status": "ok", "service": "api"})
            if path in ("/me", "/cairn/api/me"):
                given = (self.headers.get("Authorization") or "").encode()
                if not hmac.compare_digest(given, expected):
                    return self._json(401, {"detail": "not signed in"})
                return self._json(200, {"urn": USER, "groups": [], "via": "token"})
            return self._json(404, {"detail": "not found"})

        def log_message(self, *args):  # nothing is logged
            pass

    class Server(http.server.ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind looks the address up by name (socket.getfqdn), which stalls for a long time on
            # macOS runners; only the port matters here.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    server = Server(("127.0.0.1", 0), Handler)
    tmp = a.port_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(str(server.server_address[1]))
    os.replace(tmp, a.port_file)                     # whole, once listening
    server.serve_forever()


if __name__ == "__main__":
    main()
