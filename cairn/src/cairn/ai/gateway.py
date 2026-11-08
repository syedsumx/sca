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

**More upstreams** (``CAIRN_AI_GATEWAY_UPSTREAMS="backup=anthropic@https://…"``, keys in
``CAIRN_AI_GATEWAY_UPSTREAM_<NAME>_KEY`` or ``…_FILE``) serve the models ``CAIRN_AI_GATEWAY_MODEL_UPSTREAMS`` assigns
them (``model=backup``). **Routes** (virtual model names such as ``cairn-auto``, mapping to ordered candidates with
rules and fallback) are described in :mod:`cairn.ai.gateway_routes`; ``GET|PUT|DELETE /v1/ai/gateway/routes``.
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
                 rpm: Optional[int] = None, http=None, named: Optional[dict[str, tuple[str, str, str]]] = None,
                 model_upstreams: Optional[dict[str, str]] = None):
        """``upstreams``: provider -> (base URL, key). ``models``: the models the gateway serves. ``named``: more
        upstreams, name -> (provider, base URL, key); ``model_upstreams``: model -> the upstream that serves it
        (default: the upstream of the protocol it is called on)."""
        for p, (url, _) in upstreams.items():
            if p not in PROVIDERS or not url.startswith(("https://", "http://")):
                raise ValueError(f"AI gateway upstream for {p}: a provider URL")
        self.meter, self.upstreams = meter, {p: (u.rstrip("/"), k) for p, (u, k) in upstreams.items()}
        # Every upstream by name: the providers' defaults under their own names, then the operator's named ones.
        self.endpoints: dict[str, tuple[str, str, str]] = {p: (p, u, k) for p, (u, k) in self.upstreams.items()}
        for name, (provider, url, key) in (named or {}).items():
            if provider not in PROVIDERS or not url.startswith(("https://", "http://")) or name in PROVIDERS:
                raise ValueError(f"AI gateway upstream {name}: a name of its own, a provider "
                                 f"({', '.join(PROVIDERS)}) and a URL")
            self.endpoints[name] = (provider, url.rstrip("/"), key)
        self.model_upstreams = dict(model_upstreams or {})
        for m, name in self.model_upstreams.items():
            if name not in self.endpoints:
                raise ValueError(f"AI gateway model {m}: no upstream called {name}")
        self.models = [m.strip() for m in models if m and m.strip()]
        self.rpm, self.http = rpm, http
        self.routes = None           # GatewayRoutes (cairn.ai.gateway_routes), set by the services
        self.audit = None
        self.abuse = None            # AbuseGuard (cairn.ai.abuse), set by the services
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
        # More upstreams: CAIRN_AI_GATEWAY_UPSTREAMS="backup=anthropic@https://…,local=openai@http://…", each one's key
        # in CAIRN_AI_GATEWAY_UPSTREAM_<NAME>_KEY (or …_FILE); CAIRN_AI_GATEWAY_MODEL_UPSTREAMS="model=backup,…".
        named: dict[str, tuple[str, str, str]] = {}
        for item in filter(None, (x.strip() for x in os.environ.get("CAIRN_AI_GATEWAY_UPSTREAMS", "").split(","))):
            name, _, rest = item.partition("=")
            provider, _, url = rest.partition("@")
            env = "CAIRN_AI_GATEWAY_UPSTREAM_" + name.strip().upper().replace("-", "_") + "_KEY"
            named[name.strip()] = (provider.strip(), url.strip(), _key(env))
        model_ups = {}
        for item in filter(None, (x.strip() for x in os.environ.get("CAIRN_AI_GATEWAY_MODEL_UPSTREAMS", "").split(","))):
            m, _, name = item.partition("=")
            model_ups[m.strip()] = name.strip()
        if not (ups or named) or meter is None:
            return None
        rpm = os.environ.get("CAIRN_AI_GATEWAY_RPM", "").strip()
        return cls(meter, ups, os.environ.get("CAIRN_AI_GATEWAY_MODELS", "").split(","), int(rpm) if rpm else None,
                   named=named, model_upstreams=model_ups)

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
        return self.admit_limits(actor)

    def admit_limits(self, actor: str) -> Optional[tuple[int, str]]:
        try:
            self.meter.check(actor)
        except LLMBudgetExceeded as e:
            return 429, str(e)
        if not self._rate_ok(actor):
            return 429, f"more than {self.rpm} model calls a minute: try again shortly"
        return None

    def route(self, name: str) -> Optional[dict]:
        """The route a virtual model name stands for (an operator's, or the built-in ``cairn-auto``), or None."""
        from cairn.ai.gateway_routes import AUTO, builtin_auto

        if not name or name in self.models:
            return None
        got = self.routes.get(name) if self.routes is not None else None
        if got is None and name == AUTO and self.models:
            got = builtin_auto(self.models)
        return got

    def routes_list(self) -> list[dict]:
        from cairn.ai.gateway_routes import AUTO, builtin_auto

        out = self.routes.list() if self.routes is not None else []
        if self.models and not any(r["name"] == AUTO for r in out):
            out.append(builtin_auto(self.models))
        return sorted(out, key=lambda r: r["name"])

    def served(self, may_use) -> list[dict]:
        out = [{"id": m, "object": "model"} for m in self.models if may_use(m)]
        for r in self.routes_list():                  # routes the caller may use at least one candidate of
            if any(may_use(c["model"]) for c in r["candidates"]):
                out.append({"id": r["name"], "object": "model", "cairn_route": True,
                            "candidates": [c["model"] for c in r["candidates"] if may_use(c["model"])]})
        return out

    def endpoint_for(self, provider: str, model: str) -> Optional[str]:
        """The upstream that serves ``model`` on ``provider``'s protocol, or None."""
        name = self.model_upstreams.get(model)
        if name is not None:
            return name if self.endpoints[name][0] == provider else None
        return provider if provider in self.endpoints else None

    # ---- forwarding -----------------------------------------------------------------------------
    def _headers(self, provider: str, incoming: dict, key: str) -> dict:
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
        model = str(body.get("model") or "")
        endpoint = self.endpoint_for(provider, model)
        if endpoint is None:
            return 404, _error(provider, f"the AI gateway has no {provider} upstream for {model or 'this model'}")
        status, out, _ = await self._attempt(endpoint, provider, path, body, incoming, actor, task, None)
        return status, out

    async def _attempt(self, endpoint: str, provider: str, path: str, body: dict, incoming: dict, actor: str,
                       task: str, timeout: Optional[float]):
        """One call to one upstream: (status, JSON or stream, failure kind: None, "timeout" or "unreachable")."""
        import httpx

        _, base, key = self.endpoints[endpoint]
        model = str(body.get("model") or "")
        stream = bool(body.get("stream"))
        if stream and provider == "openai":
            body = {**body, "stream_options": {**(body.get("stream_options") or {}), "include_usage": True}}
        client = self.http or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        req = client.build_request("POST", f"{base}{path}", json=body, headers=self._headers(provider, incoming, key))
        if timeout is not None:
            req.extensions["timeout"] = httpx.Timeout(timeout).as_dict()
        try:
            r = await client.send(req, stream=stream)
        except httpx.TimeoutException as e:
            if self.http is None:
                await client.aclose()
            return 504, _error(provider, f"the model provider did not answer in time: {type(e).__name__}"), "timeout"
        except httpx.HTTPError as e:
            if self.http is None:
                await client.aclose()
            return 502, _error(provider, f"the model provider is not reachable: {type(e).__name__}"), "unreachable"
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
            return r.status_code, out, None
        if r.status_code != 200:
            data = await r.aread()
            await r.aclose()
            if self.http is None:
                await client.aclose()
            try:
                return r.status_code, json.loads(data), None
            except ValueError:
                return r.status_code, _error(provider, f"the model provider answered {r.status_code}"), None
        return 200, self._relay(r, client, provider, model, actor, task), None

    async def route_call(self, route: dict, provider: str, path: str, body: dict, incoming: dict, actor: str,
                         task: str, may_use) -> tuple[int, Any, dict]:
        """A call to a route: (status, JSON or stream, response headers). Candidates are tried in turn, falling back
        on provider errors, timeouts and rate limits; see :mod:`cairn.ai.gateway_routes`."""
        from cairn.ai.gateway_routes import plan, request_needs
        from cairn.ai.usage import estimate_cost

        needs = request_needs(provider, body)

        def provider_of(m: str) -> Optional[str]:
            return provider if self.endpoint_for(provider, m) is not None else None

        def cost_of(m: str) -> float:
            return estimate_cost(m, needs["input_tokens"], needs["output_tokens"], provider, self.meter.prices)
        todo, skipped = plan(route, needs, may_use, provider_of, cost_of)
        attempts: list[dict] = list(skipped)
        headers = {"X-Cairn-Route": route["name"]}
        if not todo:
            permitted = [s for s in skipped if s["skipped"] != "not permitted"]
            why = "; ".join(f"{s['model']}: {s['skipped']}" for s in skipped)
            self._audit_route(actor, route["name"], None, attempts, task)
            if not permitted:
                return 403, _error(provider, f"you may not use any model of route {route['name']} ({PRIVILEGE})"), headers
            return 422, _error(provider, f"no model of route {route['name']} fits this request ({why})"), headers
        budget = route.get("latency_budget_ms")
        start = time.monotonic()
        status, out = 502, _error(provider, "no model answered")
        for c in todo:
            m = c["model"]
            timeout = c.get("timeout_s")
            if budget is not None:
                left = budget / 1000 - (time.monotonic() - start)
                if left <= 0.05:
                    attempts.append({"model": m, "skipped": "the route's latency budget is spent"})
                    status, out = 504, _error(provider, f"route {route['name']}: its latency budget of {budget} ms "
                                              "was spent before a model answered")
                    continue
                timeout = min(timeout, left) if timeout is not None else left
            t0 = time.monotonic()
            status, out, failure = await self._attempt(self.endpoint_for(provider, m), provider, path,
                                                       {**body, "model": m}, incoming, actor, task, timeout)
            ms = int((time.monotonic() - t0) * 1000)
            retry = failure is not None or status == 429 or status >= 500
            attempts.append({"model": m, "status": status, "ms": ms,
                             "outcome": failure or ("ok" if status == 200 else "error")})
            if not retry:
                headers["X-Cairn-Model"] = m
                self._audit_route(actor, route["name"], m if status == 200 else None, attempts, task)
                return status, out, headers
        self._audit_route(actor, route["name"], None, attempts, task)
        if isinstance(out, dict):
            msg = "; ".join(f"{a['model']}: {a.get('outcome') or a.get('skipped')}"
                            + (f" ({a['status']})" if a.get("status") else "") for a in attempts)
            out = _error(provider, f"route {route['name']}: no model answered ({msg})")
        return status, out, headers

    def _audit_route(self, actor: str, route: str, model: Optional[str], attempts: list[dict], task: str) -> None:
        if self.audit is not None:
            self.audit.record("ai", "gateway.routed", actor, model_urn(route),
                              {"model": model, "attempts": attempts, "task": task})

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
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.concurrency import run_in_threadpool

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
        model = str(body.get("model") or "")
        incoming = {k.lower(): v for k, v in request.headers.items()}
        route = gw.route(model)                  # a virtual model name: routed (cairn.ai.gateway_routes)
        headers: dict = {}
        refused = gw.admit_limits(actor) if route is not None else gw.admit(actor, model, may_use(p))
        if refused:
            logger.info("AI gateway refused %s: %s", actor, refused[1])
            return JSONResponse(_error(provider, refused[1]), status_code=refused[0])
        guard = getattr(gw, "abuse", None)       # agentic abuse detection (cairn.ai.abuse)
        agent, run = getattr(p, "agent", None), getattr(p, "run", None)
        if guard is not None:
            from cairn.ai.abuse import AbuseBlocked, request_inputs

            try:                                 # what reaches the model: injection, above all in tool results
                await run_in_threadpool(guard.inspect, actor, agent, run, "gateway", request_inputs(provider, body))
            except AbuseBlocked as e:
                return JSONResponse(_error(provider, str(e)), status_code=403)
        if route is not None:
            status, out, headers = await gw.route_call(route, provider, path, body, incoming, actor, task, may_use(p))
        else:
            status, out = await gw.call(provider, path, body, incoming, actor, task)
        if guard is not None and status == 200:
            from cairn.ai.abuse import AbuseBlocked, offered, response_outputs, stream_outputs

            chunks: Optional[list] = None
            if isinstance(out, dict):
                text, calls = response_outputs(provider, out)
            elif agent is not None and guard.settings()["enabled"]:
                chunks = [c async for c in out]  # an agent's stream is held until it is checked
                text, calls = stream_outputs(provider, chunks)
            else:
                text, calls = "", []
            if text or calls:
                try:                             # what leaves the model: secrets, PII dumps, far destinations
                    await run_in_threadpool(lambda: guard.inspect(actor, agent, run, "gateway", output=text,
                                                                  tool_calls=calls,
                                                                  offered_tools=offered(provider, body)))
                except AbuseBlocked as e:
                    return JSONResponse(_error(provider, str(e)), status_code=403, headers=headers)
            if chunks is not None:
                held = chunks

                async def replay():
                    for c in held:
                        yield c
                out = replay()
        if isinstance(out, dict):
            return JSONResponse(out, status_code=status, headers=headers)
        return StreamingResponse(out, status_code=status, media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", **headers})

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

    # Routes (cairn.ai.gateway_routes): anyone who may call the gateway reads them; operators change them.
    def operator(p) -> str:
        if p.is_service or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "routes are defined by operators, not agents")
        if not authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False):
            raise HTTPException(403, "defining gateway routes needs MANAGE_SYSTEM_OPERATIONS")
        if gw.routes is None:
            raise HTTPException(503, "gateway routes are not available")
        return p.urn

    @router.get("/routes")
    def routes(p=Depends(current_principal)):
        who(p)
        return {"routes": gw.routes_list()}

    @router.put("/routes/{name}")
    def save_route(name: str, body: dict = Body(...), p=Depends(current_principal)):
        from cairn.ai.gateway_routes import RouteError

        actor = operator(p)
        try:
            return gw.routes.save(actor, name, body, gw.models)
        except RouteError as e:
            raise HTTPException(e.status, str(e))

    @router.delete("/routes/{name}", status_code=204)
    def delete_route(name: str, p=Depends(current_principal)):
        from cairn.ai.gateway_routes import RouteError

        actor = operator(p)
        try:
            gw.routes.delete(actor, name)
        except RouteError as e:
            raise HTTPException(e.status, str(e))

    return router
