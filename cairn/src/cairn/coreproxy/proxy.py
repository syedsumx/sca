"""``cairn core-proxy``: masking middleware in the Cairn Core request path.

    browser → cairn-web → cairn-core-proxy → Cairn Core
    SDK/BI  ───────────────────→ cairn-core-proxy → Cairn Core   (optional)

* Requests are forwarded unchanged (bodies streamed), so Cairn Core still authenticates and authorizes
  every call. The proxy never alters writes. A GraphQL document that aliases a governed field, or that
  cannot be parsed, is refused before it reaches Cairn Core (see :mod:`graphql_guard`).
* Responses stream through untouched unless they are JSON from a read API *and* contain a
  data-bearing key (field profiles, SQL text). Those are decoded, masked for the calling user
  per PDP obligations (Purposes), and re-encoded.
* The caller is identified locally by verifying the Cairn Core session/PAT JWT (HS256, signing key
  shared read-only with the proxy). Without a key it falls back to Cairn Core ``me`` (cached).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import contextlib
import json
import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import unquote

import httpx

from cairn.coreproxy.graphql_guard import GraphQLRefused, check_request, inject_urn, restore_injected
from cairn.coreproxy.masking import (
    DatasetPlan,
    MaskResult,
    build_plan,
    fail_closed_plan,
    mask_document,
    needs_inspection,
)
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef
from cairn.coreproxy.visibility import entity_type, restrict_document

logger = logging.getLogger(__name__)

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
GRAPHQL_PATHS = ("/api/graphql", "/api/v2/graphql")
INSPECT_PREFIXES = ("/api/graphql", "/openapi/", "/aspects", "/entities", "/api/v2/graphql")
UNMASKED_ACTORS = {"urn:li:corpuser:__datahub_system", "urn:li:corpuser:__cairn_service"}
SCHEMA_TTL = 600.0   # seconds between introspections of Cairn Core's GraphQL schema

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

    def plan(self, actor: str, dataset_urn: Optional[str]) -> DatasetPlan:
        if actor in UNMASKED_ACTORS:
            return DatasetPlan()
        if not dataset_urn:
            # Profile values or SQL text whose dataset the response does not name: nothing to decide on.
            return fail_closed_plan(None)
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

    @property
    def enforces_view(self) -> bool:
        """Authoritative mode: Cairn's view denies hold on GraphQL too (see :mod:`visibility`)."""
        return self.pdp.mode == "authoritative"

    def view_denied(self, actor: str, urn: str) -> bool:
        if actor in UNMASKED_ACTORS:
            return False
        try:
            decision = self.pdp.decide(AuthorizeRequest(
                actor=actor, privilege="VIEW_ENTITY_PAGE", resource=ResourceRef(type=entity_type(urn), urn=urn),
                environment={"channel": "core-proxy"}))
        except Exception as e:  # fail closed, like masking
            logger.warning("view decision failed for %s on %s (%s); hiding it", actor, urn, e)
            return True
        return decision.decision == "DENY"


# Ingestion run logs (Data Sources -> Run History) are free text written by the ingestion framework;
# they name its packages, paths and CLI. Rewritten for display only: report and structuredReport.
_REPORT_RULES = [
    (re.compile(r"acryl-datahub"), "cairn-ingestion"),
    (re.compile(r"/metadata-ingestion/src/datahub/"), "/opt/cairn/ingestion/"),
    (re.compile(r"/opt/datahub/"), "/opt/cairn/"),
    (re.compile(r"DataHub|Datahub"), "Cairn"),
    (re.compile(r"DATAHUB"), "CAIRN"),
    (re.compile(r"datahub"), "cairn"),
    (re.compile(r"Acryl Data|Acryl"), "Cairn"),
    (re.compile(r"acryl"), "cairn"),
]
_REPORT_FIELDS = ("report", "serializedValue")


def relabel_report_text(text: str) -> str:
    for rx, rep in _REPORT_RULES:
        text = rx.sub(rep, text)
    return text


def _relabel_reports(node) -> int:
    changed = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _REPORT_FIELDS and isinstance(value, str):
                new = relabel_report_text(value)
                if new != value:
                    node[key] = new
                    changed += 1
            else:
                changed += _relabel_reports(value)
    elif isinstance(node, list):
        for item in node:
            changed += _relabel_reports(item)
    return changed


def presentation_document(raw: bytes) -> Optional[bytes]:
    """Display rewrites for the UI: Cairn's version in appConfig, relabeled ingestion run logs."""
    if b'"appVersion"' not in raw and b'"report"' not in raw and b'"serializedValue"' not in raw:
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    changed = _relabel_reports(doc)
    app_config = (doc.get("data") or {}).get("appConfig") if isinstance(doc, dict) else None
    if isinstance(app_config, dict) and "appVersion" in app_config:
        from cairn import __version__

        app_config["appVersion"] = f"v{__version__}"
        changed += 1
    return json.dumps(doc, separators=(",", ":")).encode() if changed else None


_PATH_DATASET = re.compile(r"urn:li:dataset:\((?:[^()]|\([^()]*\))*\)")


def _context_urn(path: str, query: str = "") -> Optional[str]:
    """The dataset a REST call is about, from its path or query string (/openapi/v3/entity/dataset/<urn>,
    /aspects/<urn>?aspect=datasetProfile, ...): only when exactly one dataset is named."""
    found = set(_PATH_DATASET.findall(unquote(path))) | set(_PATH_DATASET.findall(unquote(query)))
    return found.pop() if len(found) == 1 else None


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

    schema_cache: dict = {"schema": None, "at": 0.0}
    schema_lock = asyncio.Lock()

    async def core_schema(authorization: str):
        """Cairn Core's GraphQL schema, introspected once (as the caller) and refreshed every SCHEMA_TTL seconds; a
        failed refresh keeps the last good one and is retried on the next request. None if never loaded."""
        now = time.monotonic()
        if schema_cache["schema"] is not None and now - schema_cache["at"] < SCHEMA_TTL:
            return schema_cache["schema"]
        async with schema_lock:
            if schema_cache["schema"] is not None and time.monotonic() - schema_cache["at"] < SCHEMA_TTL:
                return schema_cache["schema"]
            from graphql import build_client_schema, get_introspection_query

            try:
                resp = await client.post(f"{upstream}/api/graphql", json={"query": get_introspection_query()},
                                         headers={"Authorization": authorization, "Content-Type": "application/json"})
                resp.raise_for_status()
                schema_cache["schema"] = build_client_schema(resp.json()["data"])
                schema_cache["at"] = time.monotonic()
            except Exception as e:  # noqa: BLE001 - the caller decides (refuses) when there is no schema at all
                logger.warning("could not introspect Cairn Core's GraphQL schema: %s", e)
            return schema_cache["schema"]

    async def proxy(request: Request):
        started = time.perf_counter()
        path = request.url.path
        url = f"{upstream}{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
        inspect = request.method in ("GET", "POST") and path.startswith(INSPECT_PREFIXES)
        graphql = path.startswith(GRAPHQL_PATHS)
        if inspect:
            # Ask for an uncompressed body so it can be inspected without re-compressing.
            headers = [(k, v) for k, v in headers if k.lower() != "accept-encoding"]
        body = await request.body()
        authorization = request.headers.get("authorization")
        injected = False
        query_string = request.url.query
        if graphql and request.method in ("GET", "POST") and (authorization or "").lower().startswith("bearer "):
            # Aliases would rename the response keys masking and view restriction look for: refuse them on
            # governed fields, and refuse documents that cannot be read (fail closed). Service actors are never
            # masked, so they are not checked.
            try:
                check_request(request.method, body, request.url.query)
            except GraphQLRefused as e:
                actor = await identity.resolve(authorization, client)
                if actor not in UNMASKED_ACTORS:
                    logger.warning("refused GraphQL request from %s: %s", actor or "an unidentified caller", e)
                    return JSONResponse({"errors": [{"message": str(e), "extensions": {"code": "CAIRN_REFUSED"}}]},
                                        status_code=400)
            else:
                # View restriction finds governed entities by their urn: make sure every one in the answer has it,
                # even when the query did not ask for it (stripped again below).
                if masking.enforces_view and await identity.resolve(authorization, client) not in UNMASKED_ACTORS:
                    schema = await core_schema(authorization)
                    if schema is None:            # fail closed: without the schema the answer cannot be restricted
                        logger.warning("Cairn Core's GraphQL schema could not be loaded; refusing %s", path)
                        return JSONResponse({"errors": [{"message": "the masking proxy could not load Cairn Core's "
                                                         "GraphQL schema; try again shortly",
                                                         "extensions": {"code": "CAIRN_UNAVAILABLE"}}]},
                                            status_code=503)
                    body, query_string = inject_urn(request.method, body, query_string, schema)
                    url = f"{upstream}{path}" + (f"?{query_string}" if query_string else "")
                    injected = True
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
        relabeled = presentation_document(raw) if resp.status_code < 400 else None
        if relabeled is not None:
            raw = relabeled
        actor = await identity.resolve(authorization, client)
        # A bearer token Cairn Core answered for but the proxy could not tie to a person (a signing key that does not
        # match, Core's `me` unreachable): fail closed, as if every value were masked and every asset denied.
        unidentified = actor is None and (authorization or "").lower().startswith("bearer ")
        restrict = graphql and masking.enforces_view and (b'"urn"' in raw or injected)
        if not injected and (resp.status_code >= 400 or not (actor or unidentified)
                             or not (restrict or needs_inspection(raw))):
            return Response(raw, status_code=resp.status_code, headers=out_headers)
        if unidentified:
            logger.warning("could not identify the caller of %s %s; masking the answer (fail closed)",
                           request.method, path)
        t0 = time.perf_counter()
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return Response(raw, status_code=resp.status_code, headers=out_headers)
        added = restore_injected(doc) if injected else []
        if resp.status_code >= 400:
            restrict = False
        denied = (lambda urn: True) if unidentified else (lambda urn: masking.view_denied(actor, urn))
        planner = (lambda urn: fail_closed_plan(None)) if unidentified else (lambda urn: masking.plan(actor, urn))
        restricted = restrict_document(doc, denied).restricted if restrict else set()
        if restricted:
            out_headers["X-Cairn-Restricted"] = str(len(restricted))
        result = mask_document(doc, planner, _context_urn(path, request.url.query)) if resp.status_code < 400 \
            else MaskResult()
        if _INSPECTED:
            _INSPECTED.labels(str(result.masked_values > 0).lower()).inc()
            for p in result.policies or {"none"}:
                if result.masked_values:
                    _MASKED.labels(p).inc(result.masked_values)
            _LATENCY.labels("inspect").observe(time.perf_counter() - t0)
        if result.masked_values:
            out_headers["X-Cairn-Masked"] = str(result.masked_values)
            out_headers["X-Cairn-Masking-Policies"] = ",".join(sorted(result.policies))
        for node in added:                      # the urns the caller did not ask for
            node.pop("urn", None)
        if result.masked_values or restricted or injected:
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
