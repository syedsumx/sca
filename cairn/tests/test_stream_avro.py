"""Avro streams' Schema Registry client (cairn.pipelines.streams.Registry): schemas from a registry that anyone who
may register a subject can write to. A schema whose arrays hold values encoded in no bytes (null, an empty record,
a fixed of size 0) lets a five-byte message claim billions of items, which the decoder builds in memory; such a
schema, or one that does not parse, sets the message aside instead of taking the worker down.

The registry here is a small HTTP server answering the Confluent API's one call the client makes; the end-to-end
test with Redpanda's registry is tests/test_streams_e2e.py."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

fastavro = pytest.importorskip("fastavro")

from cairn.pipelines.streams import Registry, Reject  # noqa: E402

SCHEMAS = {
    1: {"type": "record", "name": "bomb", "fields": [{"name": "a", "type": {"type": "array", "items": "null"}}]},
    2: {"type": "record", "name": "bomb2", "fields": [{"name": "a", "type": {"type": "array", "items": {
        "type": "record", "name": "empty", "fields": [{"name": "z", "type": {"type": "fixed", "name": "f0", "size": 0}}]}}}]},
    3: {"type": "record", "name": "ok", "fields": [{"name": "id", "type": "long"},
                                                   {"name": "tags", "type": {"type": "array", "items": "string"}},
                                                   {"name": "next", "type": ["null", "ok"], "default": None}]},
    4: "this is not a schema",
}


@pytest.fixture(scope="module")
def registry():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            sid = int(self.path.rsplit("/", 1)[-1])
            body = json.dumps({"schema": SCHEMAS[sid] if isinstance(SCHEMAS[sid], str) else json.dumps(SCHEMAS[sid])})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Registry({"url": f"http://127.0.0.1:{srv.server_address[1]}"})
    srv.shutdown()


def varint(n: int) -> bytes:
    n = (n << 1) ^ (n >> 63)
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            return out


def test_an_array_of_zero_byte_items_is_set_aside(registry):
    for sid in (1, 2):
        msg = b"\x00" + sid.to_bytes(4, "big") + varint(10_000_000) + varint(0)
        with pytest.raises(Reject, match="no bytes"):
            registry.decode(msg, "value")


def test_a_schema_that_does_not_parse_is_set_aside(registry):
    with pytest.raises(Reject, match="schema 4"):
        registry.decode(b"\x00" + (4).to_bytes(4, "big") + b"\x02", "value")


def test_an_ordinary_recursive_schema_decodes(registry):
    import io

    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, fastavro.parse_schema(SCHEMAS[3]),
                               {"id": 1, "tags": ["a"], "next": {"id": 2, "tags": [], "next": None}})
    v = registry.decode(b"\x00" + (3).to_bytes(4, "big") + buf.getvalue(), "value")
    assert v == {"id": 1, "tags": ["a"], "next": {"id": 2, "tags": [], "next": None}}
