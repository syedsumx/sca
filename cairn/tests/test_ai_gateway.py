"""Cairn's AI gateway (the counterpart of Databricks' Unity Gateway for agent code), through the real services, policy
decision point, database and HTTP API, with the official Anthropic and OpenAI SDKs as clients and a local stand-in for
the providers' APIs (which checks it gets Cairn's provider key, never the caller's token). Checked:
- a person's calls, plain and streamed, on both protocols, answered and metered under them (streams included);
- an agent key needs the "model" scope and is metered to its owner, as the agent;
- a model not served, a model the person may not use, the budget and the rate limit are refused before the provider."""

import http.server
import json
import threading

import httpx
import pytest
import uvicorn

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.identity.agents import AgentIn
from cairn.services import build_services
from test_trino_e2e import port, wait

ANA, BOB, ADMIN = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:admin"
SONNET, LOCAL = "claude-sonnet-5-5", "local-llama"
pytest.importorskip("anthropic")
pytest.importorskip("openai")


class Provider(http.server.BaseHTTPRequestHandler):
    """Anthropic's /v1/messages and an OpenAI-compatible /v1/chat/completions, plain and streamed."""
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Provider.seen.append({"path": self.path, "x-api-key": self.headers.get("x-api-key"),
                              "authorization": self.headers.get("authorization"), "body": body})
        anthropic = self.path == "/v1/messages"
        # The key as an API key header, or (newer Anthropic SDKs, used directly by Cairn's own model calls) a bearer.
        if anthropic and "sk-ant-upstream" not in (self.headers.get("x-api-key"),
                                                   (self.headers.get("authorization") or "").removeprefix("Bearer ")):
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
        if not anthropic and self.headers.get("authorization") != "Bearer sk-oai-upstream":
            return self._json(401, {"error": {"message": "bad key"}})
        text = self.reply(body)
        if not body.get("stream"):
            if anthropic:
                return self._json(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                        "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 5}})
            return self._json(200, {"id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                                 "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if anthropic:
            events = [("message_start", {"type": "message_start", "message": {
                          "id": "msg_2", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                          "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 20, "output_tokens": 1}}}),
                      ("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": text}}),
                      ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                      ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                            "stop_sequence": None},
                                         "usage": {"output_tokens": 7}}),
                      ("message_stop", {"type": "message_stop"})]
            for name, data in events:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        else:
            assert body.get("stream_options", {}).get("include_usage") is True     # Cairn asks for usage
            for i, part in enumerate(text.split(" ")):
                chunk = {"id": "c2", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                         "choices": [{"index": 0, "delta": {"content": ("" if i == 0 else " ") + part},
                                      "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(("data: " + json.dumps({"id": "c2", "object": "chat.completion.chunk", "created": 1,
                                                     "model": body["model"], "choices": [],
                                                     "usage": {"prompt_tokens": 11, "completion_tokens": 6,
                                                               "total_tokens": 17}}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def reply(self, body) -> str:
        """The model's text (other tests script it in a subclass)."""
        return "Hello from the model"

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def stack(settings, graph, monkeypatch):
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{up.server_address[1]}"
    for k, v in {"CAIRN_AI_GATEWAY_ANTHROPIC_URL": base, "CAIRN_AI_GATEWAY_ANTHROPIC_KEY": "sk-ant-upstream",
                 "CAIRN_AI_GATEWAY_OPENAI_URL": base, "CAIRN_AI_GATEWAY_OPENAI_KEY": "sk-oai-upstream",
                 "CAIRN_AI_GATEWAY_MODELS": f"{SONNET},{LOCAL}", "CAIRN_AI_GATEWAY_RPM": "8"}.items():
        monkeypatch.setenv(k, v)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)                       # the SDKs read these; only what the test passes
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="ana-models", effect="allow", actions=["USE_AI_GATEWAY"],
               target=Target(resource_urns=[f"urn:li:aiModel:{SONNET}", f"urn:li:aiModel:{LOCAL}"]),
               condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    Provider.seen.clear()
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield s, f"http://127.0.0.1:{p}"
    server.should_exit = True
    up.shutdown()


def usage(s, actor):
    from sqlalchemy import select

    from cairn.ai.usage import usage_table

    with s.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(usage_table).where(usage_table.c.actor == actor))]


def test_a_persons_calls_go_through_cairn_on_both_protocols(stack):
    import anthropic
    import openai

    s, url = stack
    as_ana = {"X-Cairn-Actor": ANA}
    # The SDKs send their key as a bearer token: here Cairn keys of Ana's (agent keys with the "model" scope);
    # a person's own Cairn token works the same way.
    dev = s.agents.create_key_agent(AgentIn(name="Ana dev", scopes=["model"], owner=ANA), ADMIN)
    claude = anthropic.Anthropic(base_url=f"{url}/v1/ai/gateway/anthropic", api_key=dev["key"], max_retries=0)
    msg = claude.messages.create(model=SONNET, max_tokens=50, messages=[{"role": "user", "content": "Hi"}])
    assert msg.content[0].text == "Hello from the model"
    with claude.messages.stream(model=SONNET, max_tokens=50, messages=[{"role": "user", "content": "Hi"}]) as st:
        assert "".join(st.text_stream) == "Hello from the model"
    bot = s.agents.create_key_agent(AgentIn(name="Local bot", scopes=["model"], owner=ANA), ADMIN)
    oai = openai.OpenAI(base_url=f"{url}/v1/ai/gateway/openai/v1", api_key=bot["key"], max_retries=0)
    out = oai.chat.completions.create(model=LOCAL, messages=[{"role": "user", "content": "Hi"}])
    assert out.choices[0].message.content == "Hello from the model"
    chunks = oai.chat.completions.create(model=LOCAL, messages=[{"role": "user", "content": "Hi"}], stream=True)
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello from the model"
    # The provider saw Cairn's keys, never the caller's token.
    assert {x["x-api-key"] for x in Provider.seen if x["path"] == "/v1/messages"} == {"sk-ant-upstream"}
    assert {x["authorization"] for x in Provider.seen if x["path"] != "/v1/messages"} == {"Bearer sk-oai-upstream"}
    # Metered under Ana, streams included.
    rows = sorted((r["provider"], r["input_tokens"], r["output_tokens"]) for r in usage(s, ANA))
    assert rows == [("anthropic", 12, 5), ("anthropic", 20, 7), ("openai", 9, 4), ("openai", 11, 6)], rows
    assert sorted({r["task"] for r in usage(s, ANA)}) == sorted([f"gateway:{dev['id']}", f"gateway:{bot['id']}"])
    models = httpx.get(f"{url}/v1/ai/gateway/models", headers=as_ana).json()["data"]
    assert [m["id"] for m in models] == [SONNET, LOCAL]


def test_refusals_happen_before_the_provider(stack):
    import anthropic

    s, url = stack

    def call(headers, model=SONNET):
        return httpx.post(f"{url}/v1/ai/gateway/anthropic/v1/messages", headers=headers, timeout=30,
                          json={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]})
    bob = call({"X-Cairn-Actor": BOB})
    assert bob.status_code == 403 and "may not use" in bob.json()["error"]["message"]
    other = call({"X-Cairn-Actor": ANA}, "claude-opus-5-5")
    assert other.status_code == 403 and "not served" in other.json()["error"]["message"]
    assert call({}).status_code == 401
    assert Provider.seen == []
    # The rate limit: 8 calls a minute per person.
    codes = [call({"X-Cairn-Actor": ANA}).status_code for _ in range(9)]
    assert codes[:8] == [200] * 8 and codes[8] == 429
    # The SDK sees the refusal as the provider's own error type.
    bobs = s.agents.create_key_agent(AgentIn(name="Bob dev", scopes=["model"], owner=BOB), ADMIN)
    claude = anthropic.Anthropic(base_url=f"{url}/v1/ai/gateway/anthropic", api_key=bobs["key"], max_retries=0)
    with pytest.raises(anthropic.PermissionDeniedError):
        claude.messages.create(model=SONNET, max_tokens=10, messages=[{"role": "user", "content": "Hi"}])


def test_the_budget_is_shared_and_holds(stack, monkeypatch):
    s, url = stack
    s.ai_gateway.meter.user_daily_tokens = 15            # one call (12 + 5 tokens) uses it up
    h = {"X-Cairn-Actor": ANA}
    body = {"model": SONNET, "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]}
    assert httpx.post(f"{url}/v1/ai/gateway/anthropic/v1/messages", headers=h, json=body).status_code == 200
    r = httpx.post(f"{url}/v1/ai/gateway/anthropic/v1/messages", headers=h, json=body)
    assert r.status_code == 429 and "allowance" in r.json()["error"]["message"]
    assert len(Provider.seen) == 1


def test_an_agent_key_needs_the_model_scope_and_is_metered_to_its_owner(stack):
    import anthropic

    s, url = stack
    reader = s.agents.create_key_agent(AgentIn(name="Reader", scopes=["read"], owner=ANA), ADMIN)
    bot = s.agents.create_key_agent(AgentIn(name="Triage bot", scopes=["read", "model"], owner=ANA), ADMIN)
    body = {"model": SONNET, "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]}
    r = httpx.post(f"{url}/v1/ai/gateway/anthropic/v1/messages", json=body,
                   headers={"Authorization": f"Bearer {reader['key']}"})
    assert r.status_code == 403 and "model" in r.text
    claude = anthropic.Anthropic(base_url=f"{url}/v1/ai/gateway/anthropic", auth_token=bot["key"], max_retries=0)
    assert claude.messages.create(**body).content[0].text == "Hello from the model"
    [row] = usage(s, ANA)
    assert row["task"] == f"gateway:{bot['id']}" and (row["input_tokens"], row["output_tokens"]) == (12, 5)
