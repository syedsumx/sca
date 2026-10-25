"""LLM provider abstraction with schema-constrained JSON output.

DHE asks LLMs for *structured* results only (search plans, documentation drafts), so
the single provider primitive is :meth:`LLMProvider.generate_json`, which returns a
dict validated against a JSON schema.

* ``AnthropicProvider``: Claude via the official ``anthropic`` SDK, using structured
  outputs (``output_config.format``), adaptive thinking and an explicit effort level.
  On Claude Opus 5.5 the server-side refusal fallback is enabled by default
  (``DHE_AI_SERVER_SIDE_FALLBACK=false`` turns it off).
* ``OllamaProvider``: fully in-VPC inference via Ollama's native ``/api/chat`` with a
  JSON-schema ``format``, for air-gapped deployments.
* ``StaticProvider``: deterministic responses for tests and offline demos.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


@dataclass
class LLMUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    def generate_json(self, system: str, user: str, schema: dict, *, model: Optional[str] = None,
                      max_tokens: int = 16000) -> tuple[dict, LLMUsage]: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "high", server_side_fallback: bool = True,
                 client: Any = None):
        import anthropic

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.server_side_fallback = server_side_fallback

    def generate_json(self, system: str, user: str, schema: dict, *, model: Optional[str] = None,
                      max_tokens: int = 16000) -> tuple[dict, LLMUsage]:
        anthropic = self._anthropic
        model = model or self.model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # The system prompt is static per task, so cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        }
        try:
            if self.server_side_fallback and model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"):
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited by Anthropic API: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"cannot reach Anthropic API: {e}") from e

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMRefusal(f"model declined the request ({getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            raise LLMError("response truncated at max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text block in response")
        usage = LLMUsage(model=response.model, input_tokens=response.usage.input_tokens,
                         output_tokens=response.usage.output_tokens,
                         extra={"request_id": getattr(response, "_request_id", None)})
        return json.loads(text), usage


class OllamaProvider:
    name = "ollama"

    def __init__(self, url: str = "http://localhost:11434", model: str = "llama3.1", timeout: float = 300.0):
        self.url = url.rstrip("/")
        self.model = model
        self.http = httpx.Client(timeout=timeout)

    def generate_json(self, system: str, user: str, schema: dict, *, model: Optional[str] = None,
                      max_tokens: int = 16000) -> tuple[dict, LLMUsage]:
        model = model if model and not model.startswith("claude") else self.model
        resp = self.http.post(f"{self.url}/api/chat", json={
            "model": model, "stream": False, "format": schema,
            "options": {"num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        if resp.status_code >= 400:
            raise LLMError(f"ollama error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        try:
            data = json.loads(body["message"]["content"])
        except (KeyError, json.JSONDecodeError) as e:
            raise LLMError(f"ollama returned non-JSON content: {e}") from e
        return data, LLMUsage(model=model, input_tokens=body.get("prompt_eval_count", 0),
                              output_tokens=body.get("eval_count", 0))


class StaticProvider:
    """Returns ``responder(system, user, schema)``; used in tests and demos."""

    name = "static"

    def __init__(self, responder: Callable[[str, str, dict], dict]):
        self.responder = responder
        self.calls: list[dict] = []

    def generate_json(self, system: str, user: str, schema: dict, *, model: Optional[str] = None,
                      max_tokens: int = 16000) -> tuple[dict, LLMUsage]:
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model})
        return self.responder(system, user, schema), LLMUsage(model=model or "static")


def build_provider(settings) -> Optional[LLMProvider]:
    ai = settings.ai
    if ai.llm_provider == "anthropic":
        return AnthropicProvider(ai.model, ai.effort, ai.server_side_fallback)
    if ai.llm_provider == "ollama":
        return OllamaProvider(ai.ollama_url, ai.ollama_model)
    return None


def validate_against_schema(data: Any, schema: dict, path: str = "$") -> None:
    """Minimal JSON-schema validation (type/required/enum/properties/items/additionalProperties).

    Structured outputs already guarantee conformance for Claude; this protects the
    Ollama path and any provider that only does best-effort JSON mode.
    """
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    py = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int,
          "boolean": bool, "null": type(None)}
    if types and not any(isinstance(data, py[x]) and not (x in ("number", "integer") and isinstance(data, bool))
                         for x in types):
        raise LLMError(f"{path}: expected {types}, got {type(data).__name__}")
    if "enum" in schema and data not in schema["enum"]:
        raise LLMError(f"{path}: {data!r} not in {schema['enum']}")
    if isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                raise LLMError(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {})
        for k, v in data.items():
            if k in props:
                validate_against_schema(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                raise LLMError(f"{path}: unexpected property {k!r}")
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            validate_against_schema(item, schema["items"], f"{path}[{i}]")
