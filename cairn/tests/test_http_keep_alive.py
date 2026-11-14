"""Idle HTTP connections stay open longer than clients pool them. Trino's HTTP client (the OPA access checks) reuses a
connection that sat idle for many seconds; uvicorn's default closes it after 5, and a request sent as it closes
fails ("Failed to query OPA backend"). This holds a connection idle past 5 seconds and uses it again."""

import socket
import threading
import time

import uvicorn

from cairn.cli import keep_alive


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})


def test_an_idle_connection_is_still_open_after_uvicorns_default(monkeypatch):
    monkeypatch.delenv("CAIRN_HTTP_KEEP_ALIVE_SECONDS", raising=False)
    assert keep_alive() >= 60
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", timeout_keep_alive=keep_alive()))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        req = b"POST /v1/data/cairn/allow HTTP/1.1\r\nHost: cairn\r\nContent-Length: 2\r\n\r\n{}"
        with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
            c.sendall(req)
            assert c.recv(1000).startswith(b"HTTP/1.1 200")
            time.sleep(6)                                    # longer than uvicorn's default of 5 seconds
            c.sendall(req)
            assert c.recv(1000).startswith(b"HTTP/1.1 200")  # the same connection still answers
    finally:
        server.should_exit = True
