"""A clean room's pull from a partner's install follows no redirect: the partner names the addresses, and a redirect
from them could lead to a service inside the room's network."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

pytest.importorskip("pyiceberg")

from cairn.common.db import metadata  # noqa: E402
from cairn.dataaccess.cleanrooms import CleanRooms, RoomError  # noqa: E402


def serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_redirect_from_the_partners_addresses_is_not_followed():
    hits = []

    class Inside(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        do_POST = do_GET

        def log_message(self, *a):
            pass
    inside = serve(Inside)

    class Partner(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{inside.server_port}/internal")
            self.end_headers()
        do_POST = do_GET

        def log_message(self, *a):
            pass
    partner = serve(Partner)
    try:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        metadata.create_all(engine)
        written = []
        cr = CleanRooms(engine, lambda actor, sql: ([], []), lambda actor, party: actor == party, lambda actor, table: True,
                        write_table=lambda target, data: written.append(target))
        cr.pull_http = cr.pull_private = True
        room = cr.create("urn:li:corpuser:ana", {"name": "r", "parties": ["urn:li:corpuser:ana", "partner:north"]})
        assert room["partner_secrets"]["partner:north"]
        there = f"http://127.0.0.1:{partner.server_port}"
        with pytest.raises(RoomError) as e:
            cr.pull("partner:north", "r", "orders", {"catalog": there, "token_url": f"{there}/token",
                                                     "credential": "id:secret", "source": "a.b"})
        assert e.value.status == 502 and "secret" not in str(e.value)
        assert hits == [] and written == []
    finally:
        partner.shutdown()
        inside.shutdown()
