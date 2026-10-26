"""``cairn core-proxy``: masking middleware in the Cairn Core request path.

    browser → cairn-web → cairn-core-proxy → Cairn Core
    SDK/BI  ───────────────────→ cairn-core-proxy → Cairn Core   (optional)

* Requests are forwarded unchanged (bodies streamed), so Cairn Core still authenticates and authorizes
  every call. The proxy never alters writes.
* Responses stream through untouched unless they are JSON from a read API *and* contain a
  data-bearing key (field profiles, SQL text). Those are decoded, masked for the calling user
  per PDP obligations (Purposes), and re-encoded.
* The caller is identified locally by verifying the Cairn Core session/PAT JWT (HS256, signing key
  shared read-only with the proxy). Without a key it falls back to Cairn Core ``me`` (cached).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import contextlib
import json
import logging
import threading
import time
from typing import Optional
from urllib.parse import unquote

import httpx

from cairn.coreproxy.masking import (
    DatasetPlan,
    build_plan,
    fail_closed_plan,
    mask_document,
    needs_inspection,
)
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef

logger = logging.getLogger(__name__)

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
INSPECT_PREFIXES = ("/api/graphql", "/openapi/", "/aspects", "/entities", "/api/v2/graphql")
UNMASKED_ACTORS = {"urn:li:corpuser:__datahub_system", "urn:li:corpuser:__cairn_service"}

try:
    from prometheus_client import Counter, Histogram

    _MASKED = Counter("cairn_proxy_masked_total", "Values masked by the Cairn Core proxy", ["policy"])
    _INSPECTED = Counter("cairn_proxy_inspected_total", "Responses decoded for masking", ["masked"])
    _LATENCY = Histogram("cairn_proxy_overhead_seconds", "Proxy processing overhead (excl. upstream)", ["mode"])
except ImportError:  # pragma: no cover
    _MASKED = _INSPECTED = _LATENCY = None


# --------------------------------------------------------------------------- identity
def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_hs256(token: str, key: bytes, now: Optional[float] = None) -> Optional[dict]:
    """Verify a compact HS256 JWT; returns claims or ``None``."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "HS256":
            return None
        expected = hmac.new(key, f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return None
        claims = json.loads(_b64url_decode(payload_b64))
        exp = claims.get("exp")
        if exp is not None and float(exp) < (now or time.time()):
            return None
        return claims
    except (ValueError, json.JSONDecodeError):
        return None


class ProxyIdentity:
    def __init__(self, signing_key: Optional[str], upstream: str, ttl: int = 60):
        # Cairn Core's token service signs with the configured key's raw bytes.
        self.key = signing_key.encode() if signing_key else None
        self.upstream = upstream.rstrip("/")
        self.ttl = ttl
        self._cache: dict[str, tuple[float, Optional[str]]] = {}
        self._lock = threading.Lock()

    def actor_from_claims(self, claims: dict) -> Optional[str]:
        actor_id = claims.get("actorId") or claims.get("sub")
        if not actor_id:
            return None
        return actor_id if str(actor_id).startswith("urn:li:") else f"urn:li:corpuser:{actor_id}"

    async def resolve(self, authorization: Optional[str], client: httpx.AsyncClient) -> Optional[str]:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        token = authorization[7:].strip()
        if self.key:
            claims = verify_hs256(token, self.key)
            return self.actor_from_claims(claims) if claims else None
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(digest)
            if hit and hit[0] > now:
                return hit[1]
        actor = None
        try:
            resp = await client.post(f"{self.upstream}/api/graphql", json={"query": "{ me { corpUser { urn } } }"},
                                     headers={"Authorization": authorization, "Content-Type": "application/json"})
            if resp.status_code == 200:
                actor = ((((resp.json() or {}).get("data") or {}).get("me") or {}).get("corpUser") or {}).get("urn")
        except httpx.HTTPError:
            actor = None
        with self._lock:
            self._cache[digest] = (now + self.ttl, actor)
        return actor


# --------------------------------------------------------------------------- masking service
class MaskingService:
    def __init__(self, pdp: PolicyDecisionPoint, attributes):
        self.pdp = pdp
        self.attributes = attributes

    def plan(self, actor: str, dataset_urn: str) -> DatasetPlan:
        if actor in UNMASKED_ACTORS:
            return DatasetPlan()
        resource = None
        try:
            resource = self.attributes.resource(dataset_urn)
            decision = self.pdp.decide(AuthorizeRequest(
                actor=actor, privilege="VIEW_DATASET_PROFILE",
                resource=ResourceRef(type="dataset", urn=dataset_urn), environment={"channel": "core-proxy"}))
        except Exception as e:  # fail closed: never leak values because the PDP hiccupped
            logger.warning("masking decision failed for %s on %s (%s); failing closed", actor, dataset_urn, e)
            return fail_closed_plan(resource)
        if decision.decision == "DENY":
            # The caller cannot see profile data at all; Cairn Core would normally refuse, but mask anyway.
            return DatasetPlan(all_fields=True, strategy="redact", mask_queries=True,
                               policies=set(decision.policy_ids) or {"no-profile-access"})
        return build_plan([o.model_dump() for o in decision.obligations], resource or {})


def _context_urn(path: str) -> Optional[str]:
    # /openapi/v3/entity/dataset/<urn>[/...]
    marker = "/entity/dataset/"
    if marker in path:
        rest = unquote(path.split(marker, 1)[1])
        return rest.split("/", 1)[0] if rest.startswith("urn:li:dataset:") else rest.split("?")[0]
    return None


# --------------------------------------------------------------------------- ASGI app
def create_proxy_app(upstream: str, masking: MaskingService, identity: ProxyIdentity,
                     timeout: float = 120.0, transport: Optional[httpx.AsyncBaseTransport] = None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    upstream = upstream.rstrip("/")
    client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)

    async def health(_request: Request):
        try:
            resp = await client.get(f"{upstream}/health", timeout=5)
            ok = resp.status_code == 200
        except httpx.HTTPError:
            ok = False
        return JSONResponse({"status": "ok" if ok else "degraded", "upstream": ok}, status_code=200 if ok else 503)

    async def proxy(request: Request):
        started = time.perf_counter()
        path = request.url.path
        url = f"{upstream}{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
        inspect = request.method in ("GET", "POST") and path.startswith(INSPECT_PREFIXES)
        if inspect:
            # Ask for an uncompressed body so it can be inspected without re-compressing.
            headers = [(k, v) for k, v in headers if k.lower() != "accept-encoding"]
        body = await request.body()
        upstream_req = client.build_request(request.method, url, headers=headers, content=body)
        resp = await client.send(upstream_req, stream=True)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}

        is_json = "json" in resp.headers.get("content-type", "")
        if not (inspect and is_json):
            if _LATENCY:
                _LATENCY.labels("stream").observe(time.perf_counter() - started)
            return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out_headers,
                                     background=_close(resp))
        raw = await resp.aread()
        await resp.aclose()
        out_headers.pop("content-encoding", None)
        actor = await identity.resolve(request.headers.get("authorization"), client)
        if resp.status_code >= 400 or not actor or not needs_inspection(raw):
            return Response(raw, status_code=resp.status_code, headers=out_headers)
        t0 = time.perf_counter()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return Response(raw, status_code=resp.status_code, headers=out_headers)
        result = mask_document(doc, lambda urn: masking.plan(actor, urn), _context_urn(path))
        if _INSPECTED:
            _INSPECTED.labels(str(result.masked_values > 0).lower()).inc()
            for p in result.policies or {"none"}:
                if result.masked_values:
                    _MASKED.labels(p).inc(result.masked_values)
            _LATENCY.labels("inspect").observe(time.perf_counter() - t0)
        if result.masked_values:
            out_headers["X-Cairn-Masked"] = str(result.masked_values)
            out_headers["X-Cairn-Masking-Policies"] = ",".join(sorted(result.policies))
            raw = json.dumps(doc, separators=(",", ":")).encode()
        return Response(raw, status_code=resp.status_code, headers=out_headers)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route("/__cairn/health", health, methods=["GET"]),
                             Route("/{path:path}", proxy, methods=methods)],
                     lifespan=lifespan)


def _close(resp: httpx.Response):
    from starlette.background import BackgroundTask

    return BackgroundTask(resp.aclose)
