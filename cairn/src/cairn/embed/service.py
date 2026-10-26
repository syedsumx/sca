"""Token-scoped embeds for BI tools and portals (viewers need no Cairn Core session).

A logged-in user (or service) mints a token for specific assets and scopes. Embed endpoints
return a **minimal, read-only** view computed *as the minting user*: lineage nodes that user
cannot see are collapsed into ``hidden_count``. Tokens are HMAC-SHA256 signed, expire (≤ 30 days),
are bound to an audience origin, and are revocable by ``jti``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import time
import uuid
from typing import Callable, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, Column, String, Table, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata

SCOPES = ("card", "quality", "lineage")
MAX_TTL = 30 * 86_400

embed_tokens = Table(
    "cairn_embed_tokens", metadata,
    Column("jti", String(40), primary_key=True),
    Column("sub", String(512), nullable=False, index=True),
    Column("urns", JSONText, nullable=False),
    Column("scopes", JSONText, nullable=False),
    Column("aud", String(512), nullable=True),
    Column("exp", BigInteger, nullable=False),
    Column("revoked", Boolean, nullable=False, default=False),
    Column("created_at", BigInteger, nullable=False),
)


class TokenRequest(BaseModel):
    urn: Optional[str] = Field(default=None, pattern=r"^urn:li:")
    lookup_url: Optional[str] = None
    scopes: list[str] = Field(default_factory=lambda: ["card", "quality", "lineage"])
    ttl_seconds: int = Field(default=7 * 86_400, ge=60, le=MAX_TTL)
    audience_origin: Optional[str] = None


class EmbedError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def origin_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else None


def origin_allowed(origin: str, allowlist: list[str]) -> bool:
    """Allowlist entries are origins, optionally with a leading wildcard host (``https://*.looker.com``)."""
    for entry in allowlist:
        if entry == "*" or entry == origin:
            return True
        if "*." in entry:
            scheme, _, host = entry.partition("://*.")
            o = urlparse(origin)
            if o.scheme == scheme and (o.hostname or "").endswith("." + host):
                return True
    return False


class EmbedService:
    def __init__(self, engine: Engine, secret: Optional[str], allowed_origins: list[str], public_url: str,
                 catalog=None, attributes=None, quality_store=None,
                 can_view_as: Optional[Callable[[str], Callable[[str], bool]]] = None):
        self.engine = engine
        self.secret = (secret or "").encode()
        self.allowed_origins = allowed_origins
        self.public_url = public_url.rstrip("/")
        self.catalog = catalog
        self.attributes = attributes
        self.quality_store = quality_store
        self.can_view_as = can_view_as or (lambda actor: (lambda urn: True))

    # ---- tokens ----------------------------------------------------------------------
    def mint(self, actor: str, req: TokenRequest, urn: str) -> dict:
        if not self.secret:
            raise EmbedError("CAIRN_EMBED_SIGNING_SECRET is not configured", 503)
        scopes = [s for s in req.scopes if s in SCOPES]
        if not scopes:
            raise EmbedError(f"scopes must be a subset of {SCOPES}", 422)
        if req.audience_origin and self.allowed_origins and not origin_allowed(req.audience_origin,
                                                                               self.allowed_origins):
            raise EmbedError(f"origin {req.audience_origin} is not in CAIRN_EMBED_ALLOWED_ORIGINS", 422)
        now = int(time.time())
        claims = {"sub": actor, "urns": [urn], "scopes": scopes, "aud": req.audience_origin,
                  "exp": now + req.ttl_seconds, "jti": uuid.uuid4().hex}
        body = _b64(json.dumps(claims, separators=(",", ":")).encode())
        token = f"{body}.{_b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())}"
        with self.engine.begin() as conn:
            conn.execute(embed_tokens.insert().values(jti=claims["jti"], sub=actor, urns=claims["urns"], scopes=scopes,
                                                      aud=req.audience_origin, exp=claims["exp"], revoked=False,
                                                      created_at=now))
        base = f"{self.public_url}/cairn/api/embed/v1"
        kind = "lineage" if "lineage" in scopes else scopes[0]
        iframe = f"{base}/frame/{kind}?urn={urn}&token={token}"
        tag = {"card": "cairn-asset-card", "quality": "cairn-quality-badge", "lineage": "cairn-lineage"}[kind]
        snippet = (f'<script src="{self.public_url}/cairn-ui/embed/sdk.js" defer></script>\n'
                   f'<{tag} urn="{html.escape(urn)}" token="{token}" base-url="{base}"></{tag}>')
        return {"token": token, "jti": claims["jti"], "expires_at": claims["exp"], "snippet_html": snippet,
                "iframe_url": iframe}

    def verify(self, token: str, urn: str, scope: str, origin: Optional[str] = None) -> dict:
        try:
            body, sig = token.split(".")
        except ValueError:
            raise EmbedError("malformed embed token")
        expected = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        if not self.secret or not hmac.compare_digest(expected, sig):
            raise EmbedError("invalid embed token")
        claims = json.loads(_unb64(body))
        if claims["exp"] < time.time():
            raise EmbedError("embed token expired")
        if urn not in claims["urns"]:
            raise EmbedError("token not valid for this asset", 403)
        if scope not in claims["scopes"]:
            raise EmbedError(f"token lacks scope '{scope}'", 403)
        if claims.get("aud") and origin and origin != claims["aud"]:
            raise EmbedError("token bound to a different origin", 403)
        if origin and self.allowed_origins and not origin_allowed(origin, self.allowed_origins):
            raise EmbedError(f"origin {origin} is not allowed to embed Cairn", 403)
        with self.engine.connect() as conn:
            row = conn.execute(select(embed_tokens.c.revoked).where(embed_tokens.c.jti == claims["jti"])).first()
        if row is None or row.revoked:
            raise EmbedError("embed token revoked")
        return claims

    def list_tokens(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(embed_tokens).where(embed_tokens.c.sub == actor)
                                .order_by(embed_tokens.c.created_at.desc())).fetchall()
        return [{"jti": r.jti, "urns": r.urns, "scopes": r.scopes, "aud": r.aud, "expires_at": r.exp,
                 "revoked": r.revoked} for r in rows]

    def revoke(self, actor: str, jti: str) -> bool:
        with self.engine.begin() as conn:
            return bool(conn.execute(update(embed_tokens).where(
                (embed_tokens.c.jti == jti) & (embed_tokens.c.sub == actor)).values(revoked=True)).rowcount)

    # ---- payloads ----------------------------------------------------------------------
    def card(self, claims: dict, urn: str) -> dict:
        if not self.can_view_as(claims["sub"])(urn):
            raise EmbedError("the token owner can no longer view this asset", 403)
        s = (self.catalog.summaries([urn]) if self.catalog else {}).get(urn, {"urn": urn, "name": urn})
        res = self.attributes.resource(urn) if self.attributes else {}
        return {"urn": urn, "name": s.get("name"), "type": s.get("type"), "platform": s.get("platform"),
                "description": (s.get("description") or "")[:400], "owners": res.get("owners", []),
                "certification": res.get("certification"), "quality_status": self._status(urn),
                "freshness": {"last_updated": self._last_update(urn)},
                "url": f"{self.public_url}/cairn/asset/{urn}"}

    def quality(self, claims: dict, urn: str) -> dict:
        if not self.can_view_as(claims["sub"])(urn):
            raise EmbedError("the token owner can no longer view this asset", 403)
        monitors = []
        for m in (self.quality_store.list_monitors(urn) if self.quality_store else []):
            ev = m.last_evaluation
            monitors.append({"name": m.name, "type": m.type, "status": ev.status if ev else "NEVER_RUN",
                             "at": ev.ts if ev else None})
        return {"urn": urn, "status": self._status(urn), "monitors": monitors}

    def lineage(self, claims: dict, urn: str, depth: int = 2) -> dict:
        check = self.can_view_as(claims["sub"])
        if not check(urn):
            raise EmbedError("the token owner can no longer view this asset", 403)
        depth = max(1, min(depth, 3))
        nodes = {urn: {"urn": urn, "name": (self.catalog.summaries([urn]).get(urn) or {}).get("name", urn)
                       if self.catalog else urn, "type": None, "platform": None, "quality_status": self._status(urn)}}
        edges, hidden = [], 0
        for direction in ("UPSTREAM", "DOWNSTREAM"):
            for item in (self.catalog.lineage(urn, direction, depth, 50) if self.catalog else []):
                if not check(item["urn"]):
                    hidden += 1
                    continue
                nodes.setdefault(item["urn"], {"urn": item["urn"], "name": item.get("name"), "type": item.get("type"),
                                               "platform": item.get("platform"),
                                               "quality_status": self._status(item["urn"])})
                # Search-across-lineage returns degrees, not paths: connect degree-1 nodes to the root and
                # deeper nodes to the root with a dashed "indirect" hint the UI can render.
                deg = int(item.get("degree") or 1)
                a, b = (item["urn"], urn) if direction == "UPSTREAM" else (urn, item["urn"])
                edges.append({"source": a, "target": b, "indirect": deg > 1})
        return {"nodes": list(nodes.values()), "edges": edges, "hidden_count": hidden}

    def lookup(self, url: str) -> Optional[str]:
        if self.catalog is None:
            return None
        base = url.split("?")[0].split("#")[0]
        for condition, values in (("EQUAL", [url, base]), ("CONTAIN", [base])):
            data = self.catalog.graph.graphql(
                "query($i: SearchAcrossEntitiesInput!){ searchAcrossEntities(input:$i){ searchResults { entity { urn } } } }",
                {"i": {"query": "*", "start": 0, "count": 2, "orFilters": [
                    {"and": [{"field": f, "values": values, "condition": condition}]}
                    for f in ("externalUrl", "chartUrl", "dashboardUrl")]}})
            hits = [r["entity"]["urn"] for r in ((data.get("searchAcrossEntities") or {}).get("searchResults") or [])]
            if len(hits) == 1:
                return hits[0]
        return None

    def _status(self, urn: str) -> Optional[str]:
        if self.quality_store is None:
            return None
        rank = {"ERROR": 4, "FAIL": 3, "WARN": 2, "TRAINING": 1, "PASS": 0}
        worst = None
        for m in self.quality_store.list_monitors(urn):
            ev = m.last_evaluation
            if m.enabled and ev and (worst is None or rank[ev.status] > rank[worst]):
                worst = ev.status
        return {"PASS": "HEALTHY", "WARN": "WARNING", "FAIL": "FAILING", "ERROR": "ERROR",
                "TRAINING": "LEARNING"}.get(worst or "", None)

    def _last_update(self, urn: str) -> Optional[str]:
        if self.quality_store is None:
            return None
        for m in self.quality_store.list_monitors(urn):
            if m.type == "freshness" and m.last_evaluation:
                last = (m.last_evaluation.details or {}).get("last_update_ms")
                if last:
                    from datetime import datetime, timezone

                    return datetime.fromtimestamp(last / 1000, tz=timezone.utc).isoformat()
        return None

    def frame_html(self, kind: str, urn: str, token: str) -> str:
        tag = {"card": "cairn-asset-card", "quality": "cairn-quality-badge", "lineage": "cairn-lineage"}[kind]
        base = f"{self.public_url}/cairn/api/embed/v1"
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cairn · {kind}</title>
<style>html,body{{margin:0;padding:0;background:transparent;font-family:system-ui,sans-serif}}</style>
<script src="{self.public_url}/cairn-ui/embed/sdk.js" defer></script></head>
<body><{tag} urn="{html.escape(urn)}" token="{html.escape(token)}" base-url="{base}" depth="2"></{tag}></body></html>"""
