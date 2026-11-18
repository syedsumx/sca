"""Outbound URLs Cairn fetches on someone's say-so (a SaaS provider's files, an MCP server an operator registered):
checked before the request, so nobody can point Cairn at an internal service and read its answer.

:func:`check_public` refuses a URL that is not https (unless the caller allows plain http), carries credentials,
or names a host that resolves to a link-local address or a cloud metadata service (always refused) or, unless the
operator allowed it with ``allow_private``, any address that is not globally routable (private, loopback, CGNAT,
reserved, unspecified, multicast, ...). IPv4 addresses carried in IPv6 ones (mapped, NAT64, 6to4, Teredo) are
checked too.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class EgressRefused(ValueError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def check_public(url: str, what: str, allow_http: bool = False, allow_private: bool = False,
                 resolve=None) -> str:
    u = urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        raise EgressRefused(f"{what} is not an https:// URL without credentials", 400)
    if u.scheme == "http" and not allow_http:
        raise EgressRefused(f"{what} must be https://", 400)
    resolve = resolve or socket.getaddrinfo                # looked up at call time (tests fake DNS)
    try:
        infos = resolve(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise EgressRefused(f"{what}: {u.hostname} could not be resolved", 502)
    for info in infos:
        for ip in _addresses(ipaddress.ip_address(str(info[4][0]).split("%")[0])):
            if ip.is_link_local or ip in METADATA:
                raise EgressRefused(f"{what}: {u.hostname} resolves to a link-local address: never reached")
            # Anything not globally routable is private here: RFC 1918, loopback, CGNAT (100.64.0.0/10), benchmarking,
            # documentation, unique local, reserved, unspecified and multicast addresses alike.
            if not allow_private and (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_reserved
                                      or ip.is_unspecified or ip.is_multicast):
                raise EgressRefused(f"{what}: {u.hostname} resolves to a private address")
    return url


def url_under(url: str, prefixes) -> bool:
    """``url`` is under one of ``prefixes`` and nowhere else: the prefix ends at a boundary (``/`` or ``:``), and
    what follows names no other host (``@``), climbs no folder (``..``, or one hidden as ``%2e``) and carries no
    query, fragment, backslash, space or control character. (A bare ``startswith`` lets ``https://git.acme.com``
    allow ``https://git.acme.com.evil.example/…`` and ``https://git.acme.com@evil.example/…``.)"""
    import re as _re

    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    for p in prefixes:
        if not p or not url.startswith(p):
            continue
        rest = url[len(p):]
        if not (p.endswith(("/", ":")) or not rest or rest[0] in "/:"):
            continue
        if any(c in rest for c in "@%?#\\") or ".." in _re.split(r"[/:]", rest):
            continue
        return True
    return False


# Cloud metadata services that are not link-local (Alibaba Cloud's, AWS's over IPv6): never reached either.
METADATA = {ipaddress.ip_address("100.100.100.200"), ipaddress.ip_address("fd00:ec2::254")}
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _addresses(ip) -> list:
    """The address and every IPv4 address an IPv6 one carries (IPv4-mapped, NAT64, 6to4, Teredo): each is checked."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return [ip]
    if ip.ipv4_mapped is not None:                                  # judged by the IPv4 address it reaches
        return [ip.ipv4_mapped]
    if ip in NAT64:
        return [ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)]
    return [ip] + [v4 for v4 in (ip.sixtofour, ip.teredo[1] if ip.teredo else None) if v4 is not None]
