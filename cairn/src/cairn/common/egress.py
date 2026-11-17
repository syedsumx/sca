"""Outbound URLs Cairn fetches on someone's say-so (a SaaS provider's files, an MCP server an operator registered):
checked before the request, so nobody can point Cairn at an internal service and read its answer.

:func:`check_public` refuses a URL that is not https (unless the caller allows plain http), carries credentials,
or names a host that resolves to a link-local address (the cloud metadata service: always refused) or, unless the
operator allowed it with ``allow_private``, a private, loopback, reserved, unspecified or multicast one.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
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
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        mapped: Optional[ipaddress.IPv4Address] = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if ip.is_link_local:
            raise EgressRefused(f"{what}: {u.hostname} resolves to a link-local address: never reached")
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_unspecified
                                  or ip.is_multicast):
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
