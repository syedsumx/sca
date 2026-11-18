"""The Kafka listener as an untrusted-network parser, through raw sockets: frames and fields that lie about
their sizes, what a client that has not signed in can make the store hold, connection limits and idle
timeouts, sign-in failures, keys deleted while their connections are open, and malformed requests of
every offered API (the store answers, refuses or closes; it never dies)."""

import json
import os
import random
import socket
import struct
import time

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, REGION, _free_port

kafka = pytest.importorskip("kafka")
from kafka.record.util import calc_crc32c  # noqa: E402

APIS = {0: (3, 8), 1: (4, 11), 2: (1, 5), 3: (0, 8), 8: (2, 7), 9: (1, 5), 10: (0, 2), 11: (0, 5), 12: (0, 3), 13: (0, 3),
        14: (0, 3), 17: (0, 1), 18: (0, 2), 19: (0, 4), 36: (0, 1)}


def api(st, method, path, body=None, prefix="queues"):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/{prefix}/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=60)
    return r.status_code, (r.json() if r.content else None)


def s16(s):
    b = s.encode()
    return struct.pack(">h", len(b)) + b


def by(b):
    return struct.pack(">i", len(b)) + b


def varint(v):
    z = (v << 1) ^ (v >> 63)
    z &= (1 << 64) - 1
    out = b""
    while True:
        b = z & 0x7F
        z >>= 7
        if z == 0:
            return out + bytes([b])
        out += bytes([b | 0x80])


class Conn:
    def __init__(self, port, timeout=10):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.corr = 0

    def send(self, key, ver, body):
        self.corr += 1
        hdr = struct.pack(">hhi", key, ver, self.corr) + s16("wire-test")
        msg = hdr + body
        self.s.sendall(struct.pack(">i", len(msg)) + msg)
        return self.corr

    def recv_exact(self, n):
        out = b""
        while len(out) < n:
            chunk = self.s.recv(min(n - len(out), 1 << 20))
            if not chunk:
                raise ConnectionError("closed")
            out += chunk
        return out

    def recv(self):
        (n,) = struct.unpack(">i", self.recv_exact(4))
        data = self.recv_exact(n)
        return struct.unpack(">i", data[:4])[0], data[4:]

    def call(self, key, ver, body):
        corr = self.send(key, ver, body)
        got, data = self.recv()
        assert got == corr
        return data

    def sign_in(self, user=ADMIN[0], password=ADMIN[1]):
        r = self.call(17, 1, s16("PLAIN"))
        assert struct.unpack(">h", r[:2])[0] == 0
        r = self.call(36, 0, by(b"\0" + user.encode() + b"\0" + password.encode()))
        return struct.unpack(">h", r[:2])[0]

    def closed(self, within=5.0):
        """True when the store closes the connection within the time."""
        self.s.settimeout(within)
        try:
            while True:
                if not self.s.recv(65536):
                    return True
        except (socket.timeout, TimeoutError):
            return False
        except OSError:
            return True

    def close(self):
        self.s.close()


def batch(records, base_ts=1_700_000_000_000):
    """A record batch (magic 2) of raw record bodies (each without its length prefix)."""
    recs = b"".join(varint(len(r)) + r for r in records)
    body = struct.pack(">hiqqqhii", 0, len(records) - 1, base_ts, base_ts, -1, -1, -1, len(records)) + recs
    crc = calc_crc32c(body)
    after_len = struct.pack(">ib", 0, 2) + struct.pack(">I", crc) + body
    return struct.pack(">qi", 0, len(after_len)) + after_len


def record(value=b"v", key=None, headers=(), header_count=None):
    r = b"\0" + varint(0) + varint(0)
    r += varint(-1) if key is None else varint(len(key)) + key
    r += varint(len(value)) + value
    r += varint(len(headers) if header_count is None else header_count)
    for k, v in headers:
        r += varint(len(k)) + k + varint(len(v)) + v
    return r


def produce(topic, part, records_bytes, acks=1):
    # v3: no transactional id, acks, timeout, one topic with one partition.
    return struct.pack(">hhii", -1, acks, 10000, 1) + s16(topic) + struct.pack(">ii", 1, part) + by(records_bytes)


def produce_errors(resp):
    """(partition, error, base offset) of a v3 produce answer with one topic."""
    n = struct.unpack(">i", resp[:4])[0]
    assert n == 1
    tl = struct.unpack(">h", resp[4:6])[0]
    i = 6 + tl
    np = struct.unpack(">i", resp[i:i + 4])[0]
    i += 4
    out = []
    for _ in range(np):
        p, e, base = struct.unpack(">ihq", resp[i:i + 14])
        out.append((p, e, base))
        i += 14 + 8
    return out


def rss_hwm_kb(pid):
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    return 0


@pytest.fixture
def ks(custom_store):
    def start(**env):
        port = _free_port()
        st = custom_store({"CAIRN_STORE_KAFKA_ADDR": f"127.0.0.1:{port}", "CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT": "true", **env})
        return st, port
    return start


def alive(port):
    c = Conn(port)
    try:
        return c.sign_in() == 0
    finally:
        c.close()


def test_a_client_that_has_not_signed_in_cannot_make_the_store_hold_a_large_frame(ks):
    st, port = ks()
    c = Conn(port)
    c.s.sendall(struct.pack(">i", 60 << 20) + b"\0" * 1024)  # claims 60 MiB before signing in
    assert c.closed(within=5)
    c.close()
    # Signed in, large frames are taken (a produce of a few MiB).
    api(st, "PUT", "topics/big", {})
    c = Conn(port)
    assert c.sign_in() == 0
    big = batch([record(value=b"x" * (900 << 10)) for _ in range(3)])
    assert produce_errors(c.call(0, 3, produce("big", 0, big)))[0][1] == 0
    c.close()
