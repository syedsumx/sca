"""Models from the hyperscalers' own model services, through Cairn's AI gateway (L153): Amazon Bedrock, Azure AI
Foundry and Google Vertex AI. Cairn deploys no model: the cloud serves it, in the customer's own account, region and
network; Cairn signs in to it with the workload's cloud identity and keeps everything it does for every upstream
(who may use which model, budgets, rates, metering, audit, abuse detection, routes with fallback).

Agent code does not change. It keeps calling the gateway with the Anthropic or OpenAI SDK
(``/v1/ai/gateway/anthropic`` and ``/v1/ai/gateway/openai/v1``); the gateway turns each call into the cloud's own
request and the cloud's answer back into the protocol it was asked on, streams included.

``CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS`` (or ``…_FILE``): a JSON list of upstreams,

``{"name": "bedrock-us", "kind": "bedrock", "region": "us-east-1",
"models": {"large": "global.anthropic.<model>-v1:0", "open-weights": "openai.<model>-1:0"}}``

``{"name": "foundry", "kind": "foundry", "endpoint": "https://<resource>.services.ai.azure.com", "auth": "entra",
"models": {"medium": "<deployment>", "chat": "<another deployment>"}}``

``{"name": "vertex", "kind": "vertex", "project": "my-project", "region": "us-east5", "auth": "metadata",
"models": {"large-on-vertex": "<anthropic model id>", "gemini": "google/<model id>"}}``

``models`` maps the name callers use to the cloud's model or deployment id; each becomes a model the gateway serves
(``USE_AI_GATEWAY`` on ``urn:li:aiModel:<name>`` as for any other). Per kind:

* **bedrock**: Anthropic models over the Anthropic protocol (``InvokeModel`` and ``InvokeModelWithResponseStream``,
  whose AWS event stream Cairn turns back into the Anthropic stream); other models over the OpenAI protocol
  (Bedrock's OpenAI-compatible ``/openai/v1/chat/completions``). Requests are signed with SigV4 (``bedrock``) with
  the standard AWS credential chain: IAM roles for service accounts on EKS, EKS Pod Identity, the instance profile,
  or the environment. No key is configured in Cairn. ``endpoint``: a VPC endpoint or another partition.
* **foundry**: Claude through Foundry's Anthropic endpoint (``/anthropic/v1/messages``) and other models through its
  OpenAI v1 endpoint (``/openai/v1/chat/completions``, ``/openai/v1/embeddings``). ``auth``: ``entra`` (AKS workload
  identity: ``AZURE_CLIENT_ID``, ``AZURE_TENANT_ID``, ``AZURE_FEDERATED_TOKEN_FILE``, exchanged for an Entra token
  for Cognitive Services) or ``key`` (``key_env``: the environment variable, or its ``_FILE``, that holds the key).
* **vertex**: Claude through ``rawPredict`` / ``streamRawPredict`` and other models (Gemini) through Vertex's
  OpenAI-compatible endpoint. ``auth``: ``metadata`` (GKE workload identity, through the metadata server) or
  ``service_account`` (``key_file``: a service account key's path).

Cloud tokens are kept in memory until a minute before they end and never logged; the configuration holds no secret
(a key is named by its environment variable, never given).
"""

from __future__ import annotations

import base64
import binascii
import json
import asyncio
import os
import re
import struct
import threading
import time
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

KINDS = ("bedrock", "foundry", "vertex")
PROTOCOLS = {"bedrock": ("anthropic", "openai"), "foundry": ("anthropic", "openai"), "vertex": ("anthropic", "openai")}
BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"
VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


KEY_ENV = re.compile(r"^(CAIRN_AI_GATEWAY_[A-Z0-9_]+|(?!CAIRN_)[A-Z][A-Z0-9_]{1,63})$")


def _secure(url: str) -> bool:
    """https, or plain http only to this machine (a sidecar, a test)."""
    from urllib.parse import urlsplit

    u = urlsplit(url)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"))


class CloudError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def load(raw: Optional[str] = None) -> list["CloudUpstream"]:
    """The cloud upstreams from ``CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS`` (JSON, or ``…_FILE``)."""
    if raw is None:
        path = os.environ.get("CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS_FILE")
        raw = open(path).read() if path else os.environ.get("CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS", "")
    if not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        raise ValueError("CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS is a JSON list of upstreams")
    if not isinstance(items, list):
        raise ValueError("CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS is a JSON list of upstreams")
    out, names = [], set()
    for item in items:
        up = CloudUpstream.from_config(item)
        if up.name in names:
            raise ValueError(f"cloud upstream {up.name} is named twice")
        names.add(up.name)
        out.append(up)
    return out


class CloudUpstream:
    def __init__(self, name: str, kind: str, models: dict[str, str], region: str = "", project: str = "",
                 endpoint: str = "", auth: str = "", key_env: str = "", key_file: str = ""):
        self.name, self.kind, self.models = name, kind, dict(models)
        self.region, self.project, self.auth, self.key_env, self.key_file = region, project, auth, key_env, key_file
        self.endpoint = endpoint.rstrip("/")
        self._token: Optional[tuple[str, float]] = None
        self._lock = threading.Lock()
        self._fetching: Optional[asyncio.Lock] = None    # one token request at a time when the cache is cold

    @classmethod
    def from_config(cls, c: Any) -> "CloudUpstream":
        if not isinstance(c, dict):
            raise ValueError("a cloud upstream is an object")
        name, kind = str(c.get("name") or ""), str(c.get("kind") or "")
        what = f"cloud upstream {name or '?'}"
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"{what}: a name (letters, digits, - and _)")
        if kind not in KINDS:
            raise ValueError(f"{what}: kind is one of {', '.join(KINDS)}")
        secretish = {"key", "api_key", "secret", "client_secret", "password", "token", "private_key"} & set(c)
        if secretish:
            raise ValueError(f"{what}: no secret in the configuration ({', '.join(sorted(secretish))}): name the "
                             "environment variable that holds it (key_env) or use the workload's identity")
        extra = set(c) - {"name", "kind", "models", "region", "project", "endpoint", "auth", "key_env", "key_file"}
        if extra:
            raise ValueError(f"{what}: unknown fields {', '.join(sorted(extra))}")
        models = c.get("models") or {}
        if not isinstance(models, dict) or not models or not all(isinstance(k, str) and isinstance(v, str) and k and v
                                                                 for k, v in models.items()):
            raise ValueError(f"{what}: models maps the names callers use to the cloud's model ids")
        region, project, endpoint = str(c.get("region") or ""), str(c.get("project") or ""), str(c.get("endpoint") or "")
        auth = str(c.get("auth") or {"bedrock": "aws", "foundry": "entra", "vertex": "metadata"}[kind])
        if endpoint and not _secure(endpoint):
            raise ValueError(f"{what}: endpoint is an https:// URL (plain http only to this machine): requests carry "
                             "signatures and tokens")
        if kind == "bedrock":
            if not region and not endpoint:
                raise ValueError(f"{what}: region (or endpoint)")
            if auth != "aws":
                raise ValueError(f"{what}: Bedrock signs in with the AWS credential chain (auth: aws)")
        elif kind == "foundry":
            if not endpoint:
                raise ValueError(f"{what}: endpoint is the resource's https URL")
            if auth not in ("entra", "key"):
                raise ValueError(f"{what}: auth is entra or key")
            if auth == "key" and not c.get("key_env"):
                raise ValueError(f"{what}: key_env names the environment variable holding the key")
            if auth == "key" and not KEY_ENV.match(str(c["key_env"])):
                raise ValueError(f"{what}: key_env is an environment variable of its own (CAIRN_AI_GATEWAY_... or a name "
                                 "outside CAIRN_*), never one of Cairn's own settings")
        else:
            if not project or not region:
                raise ValueError(f"{what}: project and region")
            if auth not in ("metadata", "service_account"):
                raise ValueError(f"{what}: auth is metadata or service_account")
            if auth == "service_account" and not c.get("key_file"):
                raise ValueError(f"{what}: key_file is the service account key's path")
        return cls(name, kind, models, region, project, endpoint, auth, str(c.get("key_env") or ""),
                   str(c.get("key_file") or ""))

    def serves(self, protocol: str) -> bool:
        return protocol in PROTOCOLS[self.kind]

    # ---- the cloud's own request ----------------------------------------------------------------------------------
    async def request(self, protocol: str, path: str, body: dict, model: str,
                      incoming: Optional[dict] = None) -> tuple[str, dict, bytes, str]:
        """(url, headers, body bytes, stream format: "sse" or "aws-eventstream") for one call. The caller's
        ``anthropic-beta`` reaches the cloud as it takes it: Foundry as the header, Bedrock and Vertex as the body's
        ``anthropic_beta``."""
        target = self.models.get(model, model)
        stream = bool(body.get("stream"))
        betas = [b.strip() for b in str((incoming or {}).get("anthropic-beta") or "").split(",") if b.strip()]
        if self.kind == "bedrock":
            base = self.endpoint or f"https://bedrock-runtime.{self.region}.amazonaws.com"
            if protocol == "anthropic":
                payload = {k: v for k, v in body.items() if k not in ("model", "stream")}
                payload.setdefault("anthropic_version", BEDROCK_ANTHROPIC_VERSION)
                if betas:
                    payload.setdefault("anthropic_beta", betas)
                url = f"{base}/model/{quote(target, safe='')}/" + ("invoke-with-response-stream" if stream else "invoke")
                data = json.dumps(payload).encode()
                headers = {"content-type": "application/json",
                           "accept": "application/vnd.amazon.eventstream" if stream else "application/json"}
                return url, await self._sign(url, data, headers), data, "aws-eventstream" if stream else "sse"
            url = f"{base}/openai/v1{path.removeprefix('/v1')}"
            data = json.dumps({**body, "model": target}).encode()
            headers = {"content-type": "application/json"}
            return url, await self._sign(url, data, headers), data, "sse"
        if self.kind == "foundry":
            if protocol == "anthropic":
                url = f"{self.endpoint}/anthropic{path}"
                data = json.dumps({**body, "model": target}).encode()
                headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
                if betas:
                    headers["anthropic-beta"] = ",".join(betas)
                if self.auth == "key":
                    headers["x-api-key"] = self._key()
                else:
                    headers["authorization"] = f"Bearer {await self._entra()}"
                return url, headers, data, "sse"
            url = f"{self.endpoint}/openai/v1{path.removeprefix('/v1')}"
            data = json.dumps({**body, "model": target}).encode()
            headers = {"content-type": "application/json"}
            if self.auth == "key":
                headers["api-key"] = self._key()
            else:
                headers["authorization"] = f"Bearer {await self._entra()}"
            return url, headers, data, "sse"
        # vertex
        base = self.endpoint or ("https://aiplatform.googleapis.com" if self.region == "global"
                                 else f"https://{self.region}-aiplatform.googleapis.com")
        where = f"{base}/v1/projects/{quote(self.project, safe='')}/locations/{quote(self.region, safe='')}"
        headers = {"content-type": "application/json", "authorization": f"Bearer {await self._google()}"}
        if protocol == "anthropic":
            payload = {k: v for k, v in body.items() if k != "model"}
            payload.setdefault("anthropic_version", VERTEX_ANTHROPIC_VERSION)
            if betas:
                payload.setdefault("anthropic_beta", betas)
            verb = "streamRawPredict" if stream else "rawPredict"
            url = f"{where}/publishers/anthropic/models/{quote(target, safe='')}:{verb}"
            return url, headers, json.dumps(payload).encode(), "sse"
        url = f"{where}/endpoints/openapi{path.removeprefix('/v1')}"
        return url, headers, json.dumps({**body, "model": target}).encode(), "sse"

    # ---- signing in ---------------------------------------------------------------------------------------------
    async def _sign(self, url: str, data: bytes, headers: dict) -> dict:
        """SigV4 for Bedrock with the AWS credential chain (IRSA, Pod Identity, instance profile, environment)."""
        import anyio

        def sign() -> dict:
            try:
                import botocore.session
                from botocore.auth import SigV4Auth
                from botocore.awsrequest import AWSRequest
            except ImportError:
                raise CloudError("Bedrock needs botocore in Cairn's image", 503)
            with self._lock:
                if not hasattr(self, "_aws"):
                    self._aws = botocore.session.get_session()
            creds = self._aws.get_credentials()
            if creds is None:
                raise CloudError(f"{self.name}: no AWS credentials (a role for Cairn's service account, Pod Identity "
                                 "or an instance profile)", 503)
            region = self.region or self._aws.get_config_variable("region") or "us-east-1"
            req = AWSRequest(method="POST", url=url, data=data, headers=dict(headers))
            SigV4Auth(creds.get_frozen_credentials(), "bedrock", region).add_auth(req)
            return dict(req.headers.items())
        return await anyio.to_thread.run_sync(sign)

    def _key(self) -> str:
        path = os.environ.get(f"{self.key_env}_FILE")
        value = open(path).read().strip() if path else os.environ.get(self.key_env, "")
        if not value:
            raise CloudError(f"{self.name}: no key in {self.key_env} (or {self.key_env}_FILE)", 503)
        return value

    def _cached(self) -> Optional[str]:
        with self._lock:
            if self._token and self._token[1] - 60 > time.time():
                return self._token[0]
        return None

    def _keep(self, token: str, expires_in: Any) -> str:
        with self._lock:
            self._token = (token, time.time() + float(expires_in or 3600))
        return token

    async def _once(self, fetch) -> str:
        hit = self._cached()
        if hit:
            return hit
        if self._fetching is None:
            self._fetching = asyncio.Lock()
        async with self._fetching:
            return self._cached() or await fetch()

    async def _entra(self) -> str:
        return await self._once(self._entra_fetch)

    async def _google(self) -> str:
        return await self._once(self._google_fetch)

    async def _entra_fetch(self) -> str:
        """An Entra token for Cognitive Services, by AKS workload identity (a federated token for Cairn's account)."""
        import httpx

        client_id, tenant = os.environ.get("AZURE_CLIENT_ID"), os.environ.get("AZURE_TENANT_ID")
        token_file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        if not (client_id and tenant and token_file):
            raise CloudError(f"{self.name}: Entra workload identity is not set up (AZURE_CLIENT_ID, AZURE_TENANT_ID, "
                             "AZURE_FEDERATED_TOKEN_FILE)", 503)
        authority = os.environ.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/")
        assertion = open(token_file).read().strip()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{authority}/{quote(tenant, safe='')}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": client_id, "scope": COGNITIVE_SCOPE,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion})
        if r.status_code != 200:
            raise CloudError(f"{self.name}: Entra refused the workload's token ({r.status_code})", 502)
        out = r.json()
        return self._keep(out["access_token"], out.get("expires_in"))

    async def _google_fetch(self) -> str:
        """A Google access token: GKE workload identity through the metadata server, or a service account key."""
        import httpx

        async with httpx.AsyncClient(timeout=30) as c:
            if self.auth == "metadata":
                host = os.environ.get("GCE_METADATA_HOST", "metadata.google.internal")
                r = await c.get(f"http://{host}/computeMetadata/v1/instance/service-accounts/default/token",
                                headers={"Metadata-Flavor": "Google"})
                if r.status_code != 200:
                    raise CloudError(f"{self.name}: the metadata server gave no token ({r.status_code}): is workload "
                                     "identity set up for Cairn's service account?", 503)
                out = r.json()
                return self._keep(out["access_token"], out.get("expires_in"))
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            try:
                sa = json.loads(open(self.key_file).read())
                key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
                email, token_uri = sa["client_email"], sa.get("token_uri") or "https://oauth2.googleapis.com/token"
            except Exception as e:  # noqa: BLE001 - never echo the key
                raise CloudError(f"{self.name}: key_file is not a service account key ({type(e).__name__})", 503)

            def b64(b: bytes) -> str:
                return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
            now = int(time.time())
            head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            claims = b64(json.dumps({"iss": email, "scope": GOOGLE_SCOPE, "aud": token_uri, "iat": now,
                                     "exp": now + 3600}).encode())
            sig = key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
            r = await c.post(token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                              "assertion": f"{head}.{claims}.{b64(sig)}"})
            if r.status_code != 200:
                raise CloudError(f"{self.name}: Google refused the service account ({r.status_code})", 502)
            out = r.json()
            return self._keep(out["access_token"], out.get("expires_in"))


# ---- AWS event streams (Bedrock's response streams) back into Anthropic's server-sent events ---------------------
class EventStreamError(ValueError):
    pass


def eventstream_messages(buf: bytearray) -> list[tuple[dict, bytes]]:
    """Whole messages at the start of ``buf`` (removed from it): (headers, payload) each, CRCs checked."""
    out = []
    while len(buf) >= 12:
        total, headers_len = struct.unpack(">II", bytes(buf[:8]))
        if total < 16 or total > 16 * 1024 * 1024:
            raise EventStreamError("a malformed event stream message")
        if len(buf) < total:
            break
        msg = bytes(buf[:total])
        del buf[:total]
        if binascii.crc32(msg[:8]) & 0xFFFFFFFF != struct.unpack(">I", msg[8:12])[0]:
            raise EventStreamError("an event stream prelude's checksum does not match")
        if binascii.crc32(msg[:-4]) & 0xFFFFFFFF != struct.unpack(">I", msg[-4:])[0]:
            raise EventStreamError("an event stream message's checksum does not match")
        headers, i, end = {}, 12, 12 + headers_len
        while i < end:
            n = msg[i]
            name = msg[i + 1:i + 1 + n].decode()
            i += 1 + n
            kind = msg[i]
            i += 1
            if kind == 7:                                   # string
                ln = struct.unpack(">H", msg[i:i + 2])[0]
                headers[name] = msg[i + 2:i + 2 + ln].decode()
                i += 2 + ln
            elif kind in (0, 1):                            # booleans
                headers[name] = kind == 0
            elif kind == 2:
                i += 1
            elif kind == 3:
                i += 2
            elif kind == 4:
                i += 4
            elif kind in (5, 8):
                i += 8
            elif kind in (6,):
                ln = struct.unpack(">H", msg[i:i + 2])[0]
                i += 2 + ln
            elif kind == 9:
                i += 16
            else:
                raise EventStreamError("an unknown event stream header type")
        out.append((headers, msg[end:-4]))
    return out


async def bedrock_anthropic_sse(raw: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Bedrock's ``InvokeModelWithResponseStream`` answer as the Anthropic stream it carries (``event: <type>``,
    ``data: <json>``); an exception the stream reports becomes an Anthropic ``error`` event."""
    buf = bytearray()
    async for chunk in raw:
        buf += chunk
        try:
            messages = eventstream_messages(buf)
        except EventStreamError as e:                   # ended as an error event, not a broken connection
            ev = {"type": "error", "error": {"type": "api_error", "message": f"Bedrock's stream: {e}"}}
            yield f"event: error\ndata: {json.dumps(ev)}\n\n".encode()
            return
        for headers, payload in messages:
            if headers.get(":message-type") == "exception":
                kind = headers.get(":exception-type", "error")
                try:
                    message = json.loads(payload or b"{}").get("message") or kind
                except ValueError:
                    message = kind
                ev = {"type": "error", "error": {"type": "api_error", "message": f"Bedrock: {message}"}}
                yield f"event: error\ndata: {json.dumps(ev)}\n\n".encode()
                return
            if headers.get(":event-type") != "chunk":
                continue
            try:
                inner = base64.b64decode(json.loads(payload)["bytes"])
                ev = json.loads(inner)
            except (ValueError, KeyError, TypeError):
                ev = {"type": "error", "error": {"type": "api_error",
                                                 "message": "Bedrock's stream: a chunk that is not an Anthropic event"}}
                yield f"event: error\ndata: {json.dumps(ev)}\n\n".encode()
                return
            yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev)}\n\n".encode()


def encode_eventstream(headers: dict[str, str], payload: bytes) -> bytes:
    """One AWS event stream message (string headers only): for tests and stand-ins."""
    hb = b"".join(bytes([len(k)]) + k.encode() + b"\x07" + struct.pack(">H", len(v)) + v.encode()
                  for k, v in headers.items())
    total = 12 + len(hb) + len(payload) + 4
    prelude = struct.pack(">II", total, len(hb))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + hb + payload
    return body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)


__all__ = ["CloudError", "CloudUpstream", "KINDS", "bedrock_anthropic_sse", "encode_eventstream", "eventstream_messages",
           "load"]
