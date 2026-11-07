"""Cairn's AI gateway: the model endpoint for code people and agents write (Agent Bricks' Unity Gateway, Cairn's way).

Agent code uses the providers' own SDKs, pointed at Cairn instead of the provider, with a Cairn token instead of a
provider key:

* Anthropic-compatible: ``POST /v1/ai/gateway/anthropic/v1/messages``
  (``anthropic.Anthropic(base_url=f"{cairn}/v1/ai/gateway/anthropic", auth_token=cairn_token)``);
* OpenAI-compatible: ``POST /v1/ai/gateway/openai/v1/chat/completions``
  (``openai.OpenAI(base_url=f"{cairn}/v1/ai/gateway/openai/v1", api_key=cairn_token)``), for OpenAI or any
  OpenAI-compatible server (vLLM, Ollama, a model Cairn serves);
* ``GET /v1/ai/gateway/models``: the models the caller may use.

Every call:

* is made **as someone**: a person's token, or an agent key (which acts for its owner, and needs the agent scope
  ``model``); the agent hub's pause switch and quotas apply to agents;
* needs ``USE_AI_GATEWAY`` on the model (``urn:li:aiModel:<model>``), and the model must be one of
  ``CAIRN_AI_GATEWAY_MODELS`` (comma-separated; nothing listed: the gateway refuses every model);
* is held to the deployment's daily AI budget and the person's daily token allowance (shared with Cairn's own AI
  features), and to ``CAIRN_AI_GATEWAY_RPM`` calls a minute per person;
* reaches the provider with **Cairn's** key (``CAIRN_AI_GATEWAY_ANTHROPIC_KEY`` / ``…_OPENAI_KEY``, or ``…_FILE``),
  so no provider key is ever handed to agent code;
* is metered (tokens and estimated cost, streamed responses included) under the person, as feature ``gateway``
  or ``gateway:<agent>``, and audited without its content.

Streaming (``"stream": true``) is passed through as it arrives; its usage is read from the stream's own events.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, AsyncIterator, Optional

from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.ai.llm import LLMUsage
from cairn.ai.usage import LLMBudgetExceeded, UsageMeter

logger = logging.getLogger(__name__)
PRIVILEGE = "USE_AI_GATEWAY"
PREFIX = "/v1/ai/gateway"
PROVIDERS = ("anthropic", "openai")
PASS_HEADERS = ("anthropic-version", "anthropic-beta")


def model_urn(model: str) -> str:
    return f"urn:li:aiModel:{model}"


def _key(name: str) -> str:
    if os.environ.get(f"{name}_FILE"):
        return open(os.environ[f"{name}_FILE"]).read().strip()
    return os.environ.get(name, "")


class AiGateway:
    def __init__(self, meter: UsageMeter, upstreams: dict[str, tuple[str, str]], models: list[str],
                 rpm: Optional[int] = None, http=None):
        """``upstreams``: provider -> (base URL, key). ``models``: the models the gateway serves."""
        for p, (url, _) in upstreams.items():
            if p not in PROVIDERS or not url.startswith(("https://", "http://")):
                raise ValueError(f"AI gateway upstream for {p}: a provider URL")
        self.meter, self.upstreams = meter, {p: (u.rstrip("/"), k) for p, (u, k) in upstreams.items()}
        self.models = [m.strip() for m in models if m and m.strip()]
        self.rpm, self.http = rpm, http
        self._calls: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, meter: Optional[UsageMeter]) -> Optional["AiGateway"]:
        ups = {}
        if os.environ.get("CAIRN_AI_GATEWAY_ANTHROPIC_KEY") or os.environ.get("CAIRN_AI_GATEWAY_ANTHROPIC_KEY_FILE"):
            ups["anthropic"] = (os.environ.get("CAIRN_AI_GATEWAY_ANTHROPIC_URL", "https://api.anthropic.com"),
                                _key("CAIRN_AI_GATEWAY_ANTHROPIC_KEY"))
        if os.environ.get("CAIRN_AI_GATEWAY_OPENAI_URL"):
            ups["openai"] = (os.environ["CAIRN_AI_GATEWAY_OPENAI_URL"], _key("CAIRN_AI_GATEWAY_OPENAI_KEY"))
        if not ups or meter is None:
            return None
        rpm = os.environ.get("CAIRN_AI_GATEWAY_RPM", "").strip()
        return cls(meter, ups, os.environ.get("CAIRN_AI_GATEWAY_MODELS", "").split(","), int(rpm) if rpm else None)

    # ---- admission ------------------------------------------------------------------------------
    def _rate_ok(self, actor: str) -> bool:
        if not self.rpm:
            return True
        now = time.monotonic()
        with self._lock:
            q = self._calls[actor]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.rpm:
                return False
            q.append(now)
            return True

    def admit(self, actor: str, model: str, may_use) -> Optional[tuple[int, str]]:
        """Why the call is refused, as (status, message), or None."""
        if not model:
            return 400, "the request names no model"
        if model not in self.models:
            return 403, f"model {model!r} is not served by Cairn's AI gateway (served: {', '.join(self.models) or 'none'})"
        if not may_use(model):
            return 403, f"you may not use {model} ({PRIVILEGE})"
        try:
            self.meter.check(actor)
        except LLMBudgetExceeded as e:
            return 429, str(e)
        if not self._rate_ok(actor):
            return 429, f"more than {self.rpm} model calls a minute: try again shortly"
        return None

    def served(self, may_use) -> list[dict]:
        return [{"id": m, "object": "model"} for m in self.models if may_use(m)]

    # ---- forwarding -----------------------------------------------------------------------------
    def _headers(self, provider: str, incoming: dict) -> dict:
        _, key = self.upstreams[provider]
        h = {"content-type": "application/json"}
        if provider == "anthropic":
            h["x-api-key"] = key
            for name in PASS_HEADERS:
                if incoming.get(name):
                    h[name] = incoming[name]
            h.setdefault("anthropic-version", "2023-06-01")
        elif key:
            h["authorization"] = f"Bearer {key}"
        return h

    @staticmethod
    def _usage(provider: str, model: str, out: dict) -> LLMUsage:
        u = out.get("usage") or {}
        if provider == "anthropic":
            return LLMUsage(model=str(out.get("model") or model), input_tokens=int(u.get("input_tokens") or 0),
                            output_tokens=int(u.get("output_tokens") or 0),
                            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                            cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0))
        return LLMUsage(model=str(out.get("model") or model), input_tokens=int(u.get("prompt_tokens") or 0),
                        output_tokens=int(u.get("completion_tokens") or 0))

    async def call(self, provider: str, path: str, body: dict, incoming: dict, actor: str, task: str):
        """(status, JSON) for a plain call, or (status, async byte iterator) for a stream."""
        import httpx

        if provider not in self.upstreams:
            return 404, _error(provider, f"the AI gateway has no {provider} upstream")
        base, _ = self.upstreams[provider]
        model = str(body.get("model") or "")
        stream = bool(body.get("stream"))
        if stream and provider == "openai":
            body = {**body, "stream_options": {**(body.get("stream_options") or {}), "include_usage": True}}
        client = self.http or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        req = client.build_request("POST", f"{base}{path}", json=body, headers=self._headers(provider, incoming))
        try:
            r = await client.send(req, stream=stream)
        except httpx.HTTPError as e:
            if self.http is None:
                await client.aclose()
            return 502, _error(provider, f"the model provider is not reachable: {type(e).__name__}")
        if not stream:
            try:
                out = r.json()
            except ValueError:
                out = _error(provider, f"the model provider answered {r.status_code} without JSON")
            finally:
                if self.http is None:
                    await client.aclose()
            if r.status_code == 200:
                self.meter.record(actor, task, provider, self._usage(provider, model, out))
            return r.status_code, out
        if r.status_code != 200:
            data = await r.aread()
            await r.aclose()
            if self.http is None:
                await client.aclose()
            try:
                return r.status_code, json.loads(data)
            except ValueError:
                return r.status_code, _error(provider, f"the model provider answered {r.status_code}")
        return 200, self._relay(r, client, provider, model, actor, task)

    async def _relay(self, r, client, provider: str, model: str, actor: str, task: str) -> AsyncIterator[bytes]:
        """Passes the stream through as it arrives, reading its usage from the events it carries."""
        usage = LLMUsage(model=model)
        buf = b""
        recorded = False

        def record() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            try:
                self.meter.record(actor, task, provider, usage)
            except Exception:  # noqa: BLE001 - metering never breaks a response already sent
                logger.exception("could not meter a streamed gateway call")
        try:
            async for chunk in r.aiter_raw():
                buf += chunk
                done = False
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.startswith(b"data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip() or b"null")
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    if provider == "anthropic":
                        msg = ev.get("message") or {}
                        u = msg.get("usage") or ev.get("usage") or {}
                        usage.model = str(msg.get("model") or usage.model)
                        usage.input_tokens = int(u.get("input_tokens") or usage.input_tokens)
                        usage.output_tokens = int(u.get("output_tokens") or usage.output_tokens)
                        usage.cache_read_tokens = int(u.get("cache_read_input_tokens") or usage.cache_read_tokens)
                        usage.cache_write_tokens = int(u.get("cache_creation_input_tokens") or usage.cache_write_tokens)
                        done = done or ev.get("type") == "message_stop"
                    elif ev.get("usage"):
                        usage.model = str(ev.get("model") or usage.model)
                        usage.input_tokens = int(ev["usage"].get("prompt_tokens") or 0)
                        usage.output_tokens = int(ev["usage"].get("completion_tokens") or 0)
                        done = True
                if done:
                    record()                           # metered before the last event reaches the caller
                yield chunk
        finally:
            await r.aclose()
            if self.http is None:
                await client.aclose()
            record()                                   # also for a stream the client broke off


def _error(provider: str, message: str) -> dict:
    if provider == "anthropic":
        return {"type": "error", "error": {"type": "permission_error", "message": message}}
    return {"error": {"message": message, "type": "cairn_refused"}}


def build_gateway_router(gw: AiGateway, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse, StreamingResponse

    router = APIRouter(prefix=PREFIX, tags=["ai"])

    def may_use(p):
        return lambda model: authorize(p, PRIVILEGE, model_urn(model), raise_on_deny=False)

    def who(p) -> tuple[str, str]:
        if p.is_service:
            raise HTTPException(403, "the AI gateway is for people and their agents")
        agent = getattr(p, "agent", None)
        name = str(agent).rsplit(":", 1)[-1] if agent else ""
        return p.urn, (f"gateway:{name}"[:64] if name else "gateway")

    async def handle(provider: str, path: str, request: Request, p):
        actor, task = who(p)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(_error(provider, "expected a JSON body"), status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(_error(provider, "expected a JSON object"), status_code=400)
        refused = gw.admit(actor, str(body.get("model") or ""), may_use(p))
        if refused:
            logger.info("AI gateway refused %s: %s", actor, refused[1])
            return JSONResponse(_error(provider, refused[1]), status_code=refused[0])
        status, out = await gw.call(provider, path, body, {k.lower(): v for k, v in request.headers.items()}, actor,
                                    task)
        if isinstance(out, dict):
            return JSONResponse(out, status_code=status)
        return StreamingResponse(out, status_code=status, media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})

    @router.post("/anthropic/v1/messages")
    async def anthropic_messages(request: Request, p=Depends(current_principal)):
        return await handle("anthropic", "/v1/messages", request, p)

    @router.post("/openai/v1/chat/completions")
    async def openai_chat(request: Request, p=Depends(current_principal)):
        return await handle("openai", "/v1/chat/completions", request, p)

    @router.get("/models")
    def models(p=Depends(current_principal)):
        who(p)
        return {"object": "list", "data": gw.served(may_use(p))}

    @router.get("/openai/v1/models")
    def openai_models(p=Depends(current_principal)):
        return models(p)

    return router
