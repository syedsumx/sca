"""Cairn's egress proxy for user code: the only way out of jobs, notebooks, flows' steps, sandboxes and agent apps
when egress is enforced (``python -m cairn.network.proxy``).

User code runs where it has no route out (Docker: an internal network; Kubernetes: a NetworkPolicy that allows DNS,
Cairn's namespace and this proxy only) and gets ``HTTP_PROXY``/``HTTPS_PROXY`` pointing here with a credential in the
URL. The credential is signed by Cairn (HMAC-SHA256 with the proxy key, ``CAIRN_EGRESS_PROXY_KEY``) and carries the
workspace, the run and that workspace's allowlist at the time the run started, with an expiry: the proxy keeps no
state and needs no access to Cairn's database.

* ``CONNECT host:port`` (HTTPS and any TLS) and plain ``http://`` requests are let through only to a host the
  credential allows (``api.example.com`` or ``*.example.com``, on its ports; 80 and 443 when it names none) or to an
  address inside an allowed CIDR. Names are resolved here and the connection is made to the address checked (no second
  lookup to rebind); a name that resolves to a private or loopback address is refused unless a CIDR rule allows that
  address (or ``CAIRN_EGRESS_ALLOW_PRIVATE=true``, for allowed hosts on a private network), and one that resolves to a
  link-local address (the cloud metadata service) always is, unless a CIDR rule allows it.
* For TLS through ``CONNECT``, the server name the client asks for (SNI) must be the host it connected to: a client
  cannot connect to an allowed host and ask for another (domain fronting).
* The proxy's own ``always`` list (``CAIRN_EGRESS_ALWAYS``: ``host[:port]`` or CIDRs) is Cairn itself and the
  lakehouse (store, catalog, Trino): reachable by every run whatever its rules.
* Fail closed: no credential, a forged or expired one, or a credential without rules, and only the ``always`` list
  is reachable. Every refusal is logged as one JSON line (``"event": "egress.denied"``, with the workspace, run, client
  address, target and reason) on the ``cairn.egress`` logger (stderr).

Only the standard library: the proxy runs in a minimal image (``docker/egress-proxy.Dockerfile``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("cairn.egress")
USER = "egress"
DEFAULT_PORTS = (80, 443)


# --------------------------------------------------------------------------- credentials
def issue(key: bytes, workspace: str, run: str, rules: list[dict], ttl_s: int) -> str:
    """A credential for one run: ``base64url(json).hexhmac``."""
    body = base64.urlsafe_b64encode(json.dumps({"w": workspace, "r": run, "rules": rules, "e": int(time.time()) + ttl_s},
                                               separators=(",", ":")).encode()).decode().rstrip("=")
    return body + "." + hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def check(key: bytes, credential: str) -> Optional[dict]:
    """The credential's content when Cairn signed it and it has not expired, else None."""
    body, _, sig = credential.partition(".")
    if not body or not hmac.compare_digest(sig, hmac.new(key, body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        doc = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if not isinstance(doc, dict) or int(doc.get("e") or 0) < time.time():
        return None
    return doc


def proxy_url(base: str, credential: str) -> str:
    """``http://egress:<credential>@host:port`` for HTTP(S)_PROXY."""
    u = urlsplit(base)
    return f"{u.scheme or 'http'}://{USER}:{credential}@{u.netloc}"


# --------------------------------------------------------------------------- rules
def _host_matches(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def _port_ok(rule: dict, port: int) -> bool:
    ports = rule.get("ports") or list(DEFAULT_PORTS)
    return port in ports


def _private(ip: Any) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def parse_always(raw: str) -> list[dict]:
    """``CAIRN_EGRESS_ALWAYS``: comma-separated ``host``, ``host:port`` or CIDR entries."""
    out = []
    for item in (x.strip() for x in raw.split(",")):
        if not item:
            continue
        try:
            out.append({"cidr": str(ipaddress.ip_network(item, strict=False)), "ports": []})
            continue
        except ValueError:
            pass
        host, _, port = item.rpartition(":") if item.count(":") == 1 else (item, "", "")
        out.append({"host": host.lower(), "ports": [int(port)] if port.isdigit() else [], "any_port": not port})
    return out


def decide(rules: list[dict], always: list[dict], host: str, port: int,
           addresses: list[str], allow_private: bool = False) -> tuple[bool, str]:
    """May a run with ``rules`` reach ``host:port`` (resolved to ``addresses``)? Returns (allowed, why)."""
    ips = []
    for a in addresses:
        try:
            ips.append(ipaddress.ip_address(a))
        except ValueError:
            return False, f"{a} is not an address"
    if not ips:
        return False, f"{host} does not resolve"

    def in_cidrs(entries: list[dict]) -> bool:
        for r in entries:
            if r.get("cidr") and (r.get("any_port") or not r.get("ports") or port in r["ports"]):
                net = ipaddress.ip_network(r["cidr"], strict=False)
                if all(ip in net for ip in ips):
                    return True
        return False

    for r in always:                                   # Cairn and the lakehouse: any address, the ports listed
        if r.get("host") and _host_matches(r["host"], host) and (r.get("any_port") or port in (r.get("ports") or [])):
            return True, "always"
    if in_cidrs(always):
        return True, "always"
    cidr_rules = [r for r in rules if r.get("cidr")]
    for r in cidr_rules:
        if _port_ok(r, port) and all(ip in ipaddress.ip_network(r["cidr"], strict=False) for ip in ips):
            return True, f"cidr {r['cidr']}"
    host_rules = [r for r in rules if r.get("host") and _host_matches(r["host"], host)]
    if not host_rules:
        return False, "no rule allows this host" if rules else "no egress rule: only Cairn and the lakehouse"
    if not any(_port_ok(r, port) for r in host_rules):
        return False, f"port {port} is not allowed for {host}"
    if any(ip.is_link_local for ip in ips):
        return False, f"{host} resolves to a link-local address (the metadata service): allow it with a CIDR rule"
    if not allow_private and any(_private(ip) for ip in ips):
        return False, f"{host} resolves to a private address (allow it with a CIDR rule)"
    return True, f"host {host_rules[0]['host']}"


# --------------------------------------------------------------------------- TLS server name
def server_name(hello: bytes) -> Optional[str]:
    """The SNI host name in a TLS ClientHello record, or None when there is none (or it is not a ClientHello)."""
    try:
        if len(hello) < 5 or hello[0] != 0x16:
            return None
        p = 5
        if hello[p] != 0x01:                            # handshake type: ClientHello
            return None
        p += 4 + 2 + 32                                 # handshake header, version, random
        p += 1 + hello[p]                               # session id
        p += 2 + int.from_bytes(hello[p:p + 2], "big")  # cipher suites
        p += 1 + hello[p]                               # compression methods
        end = p + 2 + int.from_bytes(hello[p:p + 2], "big")
        p += 2
        while p + 4 <= end:
            ext, size = int.from_bytes(hello[p:p + 2], "big"), int.from_bytes(hello[p + 2:p + 4], "big")
            p += 4
            if ext == 0:                                 # server_name
                q = p + 2
                while q + 3 <= p + size:
                    kind, n = hello[q], int.from_bytes(hello[q + 1:q + 3], "big")
                    if kind == 0:
                        return hello[q + 3:q + 3 + n].decode("ascii").lower()
                    q += 3 + n
            p += size
    except (IndexError, UnicodeDecodeError):
        return None
    return None


# --------------------------------------------------------------------------- the proxy
def _deny(c: socket.socket, code: int, text: str) -> None:
    body = text.encode()
    reason = {400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required", 502: "Bad Gateway"}.get(code, "Error")
    extra = 'Proxy-Authenticate: Basic realm="cairn-egress"\r\n' if code == 407 else ""
    try:
        c.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n{extra}"
                  f"Connection: close\r\n\r\n".encode() + body)
    except OSError:
        pass


def _splice(a: socket.socket, b: socket.socket) -> None:
    def one(x, y):
        try:
            while True:
                d = x.recv(65536)
                if not d:
                    break
                y.sendall(d)
        except OSError:
            pass
        try:
            y.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    ts = [threading.Thread(target=one, args=(a, b), daemon=True), threading.Thread(target=one, args=(b, a), daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


class EgressProxy:
    def __init__(self, listen: str, key: bytes, always: list[dict], resolve=None, connect_timeout: float = 30.0,
                 allow_private: bool = False):
        if len(key) < 16:
            raise ValueError("the egress proxy needs a key of at least 16 bytes (CAIRN_EGRESS_PROXY_KEY)")
        host, _, port = listen.rpartition(":")
        self.key, self.always, self.connect_timeout = key, always, connect_timeout
        self.allow_private = allow_private
        self.resolve = resolve or (lambda h, p: [i[4][0] for i in socket.getaddrinfo(h, p, type=socket.SOCK_STREAM)])
        self.srv = socket.create_server((host or "0.0.0.0", int(port)))
        self.port = self.srv.getsockname()[1]
        self.denied = 0
        self._stop = False

    def serve_forever(self) -> None:
        while not self._stop:
            try:
                c, addr = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c, addr[0]), daemon=True).start()

    def start(self) -> "EgressProxy":
        threading.Thread(target=self.serve_forever, daemon=True, name="cairn-egress-proxy").start()
        return self

    def close(self) -> None:
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass

    def _log_denied(self, doc: Optional[dict], client: str, target: str, reason: str) -> None:
        self.denied += 1
        logger.warning(json.dumps({"event": "egress.denied", "workspace": (doc or {}).get("w"), "run": (doc or {}).get("r"),
                                   "client": client, "target": target, "reason": reason,
                                   "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, sort_keys=True))

    def _credential(self, headers: dict) -> Optional[dict]:
        h = headers.get("proxy-authorization", "")
        if not h.lower().startswith("basic "):
            return None
        try:
            user, _, cred = base64.b64decode(h[6:].strip()).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        return check(self.key, cred) if user == USER else None

    def _open(self, doc: Optional[dict], client: str, host: str, port: int, c: socket.socket) -> Optional[socket.socket]:
        target = f"{host}:{port}"
        try:
            addresses = self.resolve(host, port)
        except OSError:
            addresses = []
        ok, why = decide(list((doc or {}).get("rules") or []), self.always, host, port, addresses, self.allow_private)
        if not ok:
            if doc is None:                             # without a credential only the always list
                why = "no valid credential: only Cairn and the lakehouse"
            self._log_denied(doc, client, target, why)
            _deny(c, 403, f"egress to {target} is not allowed: {why}")
            return None
        try:
            up = socket.create_connection((addresses[0], port), self.connect_timeout)
            up.settimeout(None)
            return up
        except OSError as e:
            _deny(c, 502, f"{target}: {e}")
            return None

    def handle(self, c: socket.socket, client: str) -> None:
        try:
            c.settimeout(30)
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(65536)
                if not d:
                    c.close()
                    return
                buf += d
                if len(buf) > 65536:
                    _deny(c, 400, "request head too large")
                    c.close()
                    return
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            try:
                method, target, version = lines[0].split(" ", 2)
            except ValueError:
                _deny(c, 400, "not an HTTP request")
                c.close()
                return
            headers: list[tuple[str, str]] = []
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                headers.append((k.strip(), v.strip()))
            hmap = {k.lower(): v for k, v in headers}
            doc = self._credential(hmap)
            if method.upper() == "CONNECT":
                host, _, port_s = target.rpartition(":")
                host = host.strip("[]").lower()
                if not host or not port_s.isdigit():
                    _deny(c, 400, "CONNECT host:port")
                    c.close()
                    return
                up = self._open(doc, client, host, int(port_s), c)
                if up is None:
                    c.close()
                    return
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                first = rest
                if not first:
                    try:
                        c.settimeout(10)
                        first = c.recv(16384)
                    except OSError:
                        first = b""
                    c.settimeout(None)
                sni = server_name(first)
                if sni is not None and sni != host:
                    self._log_denied(doc, client, f"{host}:{port_s}", f"TLS server name {sni} is not {host}")
                    up.close()
                    c.close()
                    return
                if first:
                    up.sendall(first)
                _splice(c, up)
                return
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                _deny(c, 400, "send absolute http:// URLs, or CONNECT for https")
                c.close()
                return
            up = self._open(doc, client, u.hostname.lower(), u.port or 80, c)
            if up is None:
                c.close()
                return
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            keep = [(k, v) for k, v in headers if k.lower() not in ("host", "connection", "keep-alive", "proxy-connection",
                                                                       "proxy-authorization", "te", "upgrade")]
            out = f"{method} {path} {version}\r\nHost: {u.netloc}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in keep) + \
                "Connection: close\r\n\r\n"
            up.sendall(out.encode("latin-1") + rest)
            _splice(c, up)
        except Exception as e:  # noqa: BLE001 - one connection's failure, never the proxy's
            logger.debug("egress connection failed: %s", e)
            try:
                c.close()
            except OSError:
                pass


def _read_key(path: str, env: str) -> bytes:
    if path:
        with open(path, "rb") as fh:
            return fh.read().strip()
    return env.encode()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cairn-egress-proxy", description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", default=os.environ.get("CAIRN_EGRESS_PROXY_LISTEN", "0.0.0.0:3128"))
    ap.add_argument("--key-file", default=os.environ.get("CAIRN_EGRESS_PROXY_KEY_FILE", ""),
                    help="file holding the key Cairn signs credentials with (else CAIRN_EGRESS_PROXY_KEY)")
    ap.add_argument("--always", default=os.environ.get("CAIRN_EGRESS_ALWAYS", ""),
                    help="Cairn and the lakehouse: host[:port] or CIDR, comma-separated")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    key = _read_key(a.key_file, os.environ.get("CAIRN_EGRESS_PROXY_KEY", ""))
    private = os.environ.get("CAIRN_EGRESS_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on")
    p = EgressProxy(a.listen, key, parse_always(a.always), allow_private=private)
    logger.info(json.dumps({"event": "egress.listening", "port": p.port, "always": parse_always(a.always)}))
    p.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
