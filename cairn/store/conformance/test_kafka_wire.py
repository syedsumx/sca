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


def test_idle_connections_and_slow_sign_ins_are_closed(ks):
    st, port = ks(CAIRN_STORE_KAFKA_IDLE_SECONDS="2")
    silent = Conn(port)
    assert silent.closed(within=6)  # never signs in
    signed = Conn(port)
    assert signed.sign_in() == 0
    assert signed.call(18, 0, b"")  # in use: answered
    assert signed.closed(within=6)  # then idle
    # A frame dribbled a byte at a time does not keep a connection open.
    slow = Conn(port)
    slow.s.sendall(struct.pack(">i", 200))
    end = time.time() + 8
    gone = False
    while time.time() < end and not gone:
        try:
            slow.s.sendall(b"\0")
        except OSError:
            break
        slow.s.settimeout(0.5)
        try:
            gone = slow.s.recv(1) == b""
        except (socket.timeout, TimeoutError):
            pass
        except OSError:
            gone = True
    assert gone or time.time() < end
    for c in (silent, signed, slow):
        c.close()


def test_a_failed_sign_in_closes_the_connection(ks):
    st, port = ks()
    c = Conn(port)
    assert c.sign_in(password="wrong-secret") == 58  # SASL_AUTHENTICATION_FAILED, then closed as Kafka brokers do
    assert c.closed(within=3)
    c.close()


def metadata_error(conn, topic):
    r = conn.call(3, 1, struct.pack(">i", 1) + s16(topic))
    i = 4  # brokers
    for _ in range(struct.unpack(">i", r[:4])[0]):
        i += 4
        i += 2 + struct.unpack(">h", r[i:i + 2])[0] + 4
        i += 2 + max(0, struct.unpack(">h", r[i:i + 2])[0])
    i += 4  # controller
    assert struct.unpack(">i", r[i:i + 4])[0] == 1
    return struct.unpack(">h", r[i + 4:i + 6])[0]


def test_key_changes_reach_open_connections(ks):
    st, port = ks()
    api(st, "PUT", "topics/orders-eu", {})
    code, k = api(st, "POST", "keys", {"buckets": ["orders-*"]}, prefix="admin")
    assert code == 201
    c = Conn(port)
    assert c.sign_in(k["access_key"], k["secret_key"]) == 0
    assert metadata_error(c, "orders-eu") == 0
    one = batch([record(value=b"x")])
    assert produce_errors(c.call(0, 3, produce("orders-eu", 0, one)))[0][1] == 0
    # A narrower scope applies to the next request, as on the HTTP API.
    assert api(st, "PATCH", f"keys/{k['access_key']}", {"read_only": True}, prefix="admin")[0] == 200
    assert produce_errors(c.call(0, 3, produce("orders-eu", 0, one)))[0][1] == 29  # TOPIC_AUTHORIZATION_FAILED
    # A rotated secret ends the connections signed in with the old one.
    code, r = api(st, "POST", f"keys/{k['access_key']}/rotate", prefix="admin")
    assert code == 200
    c.send(3, 1, struct.pack(">i", 1) + s16("orders-eu"))
    assert c.closed(within=3)
    c.close()
    # A deleted key's open connections end at their next request.
    d = Conn(port)
    assert d.sign_in(k["access_key"], r["secret_key"]) == 0
    assert metadata_error(d, "orders-eu") == 0
    assert api(st, "DELETE", f"keys/{k['access_key']}", prefix="admin")[0] == 204
    d.send(3, 1, struct.pack(">i", 1) + s16("orders-eu"))
    assert d.closed(within=3)
    d.close()


def test_record_counts_that_lie_are_refused_without_running_away(ks):
    st, port = ks()
    api(st, "PUT", "topics/bomb", {})
    c = Conn(port)
    assert c.sign_in() == 0
    before = rss_hwm_kb(st.proc.pid)
    # One record that claims 20 million headers and carries none (the batch's CRC is right).
    bad = batch([record(value=b"x", header_count=20_000_000)])
    t = time.time()
    assert produce_errors(c.call(0, 3, produce("bomb", 0, bad)))[0][1] == 2  # CORRUPT_MESSAGE
    assert time.time() - t < 2
    assert rss_hwm_kb(st.proc.pid) - before < 100 << 10, "the store grew by more than 100 MiB"
    # Negative and huge counts and lengths elsewhere in a record.
    for r in (record(value=b"x", header_count=-5), b"\0" + varint(0) + varint(0) + varint(1 << 40),
              b"\0" + varint(0) + varint(0) + varint(-1) + varint(-(1 << 40)), b"\0" + varint(1 << 62)):
        assert produce_errors(c.call(0, 3, produce("bomb", 0, batch([r]))))[0][1] in (0, 2)
    assert api(st, "GET", "topics/bomb")[1]["messages"] <= 2
    c.close()


def test_malformed_requests_never_take_the_store_down(ks):
    st, port = ks()
    api(st, "PUT", "topics/fuzz", {"partitions": 2})
    rnd = random.Random(41)
    good = {
        0: produce("fuzz", 1, batch([record(value=b"ok", key=b"k", headers=[(b"h", b"v")])])),
        1: struct.pack(">iiiib", -1, 0, 1, 1 << 20, 0) + struct.pack(">i", 1) + s16("fuzz") + struct.pack(">i", 1)
        + struct.pack(">iqqi", 0, 0, 0, 1 << 20),
        3: struct.pack(">i", 1) + s16("fuzz"),
    }
    sizes = [b"\xff\xff", b"\x7f\xff", b"\xff\xff\xff\xff", b"\x7f\xff\xff\xff", b"\x80\x00\x00\x00"]
    for key, (lo, hi) in APIS.items():
        for ver in range(lo, hi + 1):
            base = good.get(key, struct.pack(">i", 1) + s16("fuzz") + struct.pack(">i", 1) + struct.pack(">i", 0))
            bodies = [b"", base[: len(base) // 2]]
            for _ in range(6):
                b = bytearray(base + bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 24))))
                if b:
                    i = rnd.randrange(len(b))
                    s = rnd.choice(sizes)
                    b[i:i + len(s)] = s
                bodies.append(bytes(b))
            for body in bodies:
                c = Conn(port, timeout=40)
                assert c.sign_in() == 0
                try:
                    c.call(key, ver, body)
                except (ConnectionError, OSError, AssertionError, struct.error):
                    pass  # refused by closing: fine
                c.close()
    # Unknown APIs and versions out of range are refused by closing; ApiVersions answers any version.
    for key, ver in ((99, 0), (0, 0), (0, 99), (1, -1)):
        c = Conn(port)
        assert c.sign_in() == 0
        c.send(key, ver, b"")
        assert c.closed(within=3)
        c.close()
    c = Conn(port)
    assert struct.unpack(">h", c.call(18, 7, b"")[:2])[0] == 35  # UNSUPPORTED_VERSION, with the versions offered
    c.close()
    assert st.proc.poll() is None and alive(port)


def test_a_fetch_answer_is_bounded(ks):
    st, port = ks()
    api(st, "PUT", "topics/wide", {})
    c = Conn(port)
    assert c.sign_in() == 0
    big = batch([record(value=b"x" * (900 << 10)) for _ in range(3)])
    assert produce_errors(c.call(0, 3, produce("wide", 0, big)))[0][1] == 0
    # The same partition 40 times over, asking for as many bytes as the protocol can express.
    parts = struct.pack(">iqi", 0, 0, 0x7FFFFFFF) * 40
    body = struct.pack(">iiiib", -1, 0, 1, 0x7FFFFFFF, 0) + struct.pack(">i", 1) + s16("wide") + struct.pack(">i", 40) + parts
    c.send(1, 4, body)
    (n,) = struct.unpack(">i", c.recv_exact(4))
    assert n <= (64 << 20) + (1 << 20), f"a fetch answer of {n >> 20} MiB"
    c.recv_exact(n)
    c.close()


def test_connections_are_limited(ks):
    st, port = ks(CAIRN_STORE_KAFKA_MAX_CONNECTIONS="2")
    a, b = Conn(port), Conn(port)
    assert a.sign_in() == 0 and b.sign_in() == 0
    c = Conn(port)
    assert c.closed(within=3)  # over the limit
    c.close()
    a.close()
    time.sleep(0.3)
    d = Conn(port)
    assert d.sign_in() == 0  # a slot came free
    for x in (b, d):
        x.close()
