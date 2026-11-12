"""Models from Amazon Bedrock, Azure AI Foundry and Google Vertex AI through Cairn's AI gateway (L153).

The callers are the official Anthropic and OpenAI SDKs, pointed at the gateway as agent code is. The clouds are
stand-ins built to their published APIs, each checking what the real service checks:

* Bedrock: ``InvokeModel``, ``InvokeModelWithResponseStream`` (answered in AWS's binary event stream) and the
  OpenAI-compatible endpoint, with every request's SigV4 signature recomputed and compared (botocore);
* Azure AI Foundry: Entra's token endpoint (the workload's federated token as client assertion), then the Anthropic
  and OpenAI v1 endpoints with that token or a key;
* Vertex AI: the GKE metadata server's token, ``rawPredict``/``streamRawPredict`` and the OpenAI-compatible endpoint.

A live call to each cloud needs an account and is not made here (see the roadmap row)."""

from __future__ import annotations

import base64
import http.server
import json
import socket
import threading
import time

import pytest
import uvicorn

from cairn.ai.cloud_providers import (CloudUpstream, EventStreamError, encode_eventstream, eventstream_messages,
                                      load)
from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.identity.agents import AgentIn
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
AWS = ("AKIAEXAMPLESTANDIN01", "aws-secret-standin-value-0123456789abcd")
FEDERATED = "eyJhbGciOiJSUzI1NiJ9.federated-token-of-cairns-service-account.sig"
TEXT = "Hello from the cloud"


def port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(p: int) -> None:
    end = time.time() + 30
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listens on {p}")


def anthropic_message(model: str) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": TEXT}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 13, "output_tokens": 4}}


def anthropic_events(model: str) -> list[dict]:
    return [{"type": "message_start", "message": {**anthropic_message(model), "content": [],
                                                   "usage": {"input_tokens": 13, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": TEXT}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 4}},
            {"type": "message_stop"}]


def openai_completion(model: str) -> dict:
    return {"id": "c1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": TEXT}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}


class Cloud(http.server.BaseHTTPRequestHandler):
    """Bedrock, Entra, Foundry, the GCP metadata server and Vertex, by path."""
    seen: list[dict] = []
    token_calls = {"entra": 0, "metadata": 0}

    def log_message(self, *a):
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, doc: dict) -> None:
        self._send(status, json.dumps(doc).encode())

    def do_GET(self):
        if self.path == "/computeMetadata/v1/instance/service-accounts/default/token":
            if self.headers.get("Metadata-Flavor") != "Google":
                return self._json(403, {"error": "no metadata flavor"})
            Cloud.token_calls["metadata"] += 1
            return self._json(200, {"access_token": "gcp-token", "expires_in": 3599, "token_type": "Bearer"})
        self._json(404, {"error": self.path})

    def _sigv4_ok(self, body: bytes) -> bool:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        auth = self.headers.get("Authorization") or ""
        try:
            cred = auth.split("Credential=")[1].split(",")[0]
            signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
            got = auth.split("Signature=")[1]
            akid, _date, region, service, _ = cred.split("/")
        except IndexError:
            return False
        if akid != AWS[0] or service != "bedrock":
            return False
        req = AWSRequest(method="POST", url=f"http://{self.headers['Host']}{self.path}", data=body,
                         headers={h: self.headers[h] for h in signed if h != "host"})
        req.context["timestamp"] = self.headers["X-Amz-Date"]
        signer = SigV4Auth(Credentials(*AWS), "bedrock", region)
        return signer.signature(signer.string_to_sign(req, signer.canonical_request(req)), req) == got

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        p = self.path
        rec = {"path": p, "authorization": self.headers.get("Authorization"), "api-key": self.headers.get("api-key"),
               "x-api-key": self.headers.get("x-api-key")}
        try:
            doc = json.loads(body or b"{}")
        except ValueError:
            doc = {}
        rec["body"] = doc
        Cloud.seen.append(rec)
        # ---- Entra
        if p == "/tenant-1/oauth2/v2.0/token":
            from urllib.parse import parse_qs

            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            ok = (form.get("client_assertion") == FEDERATED and form.get("client_id") == "client-1"
                  and form.get("scope") == "https://cognitiveservices.azure.com/.default"
                  and form.get("grant_type") == "client_credentials")
            Cloud.token_calls["entra"] += 1
            return self._json(200 if ok else 401, {"access_token": "entra-token", "expires_in": 3600} if ok
                              else {"error": "invalid_client"})
        # ---- Bedrock
        signed = (self.headers.get("Authorization") or "").startswith("AWS4-HMAC-SHA256")
        if p.startswith("/model/") or (p.startswith("/openai/v1/") and signed):   # Bedrock (one host per cloud really)
            if not self._sigv4_ok(body):
                return self._json(403, {"message": "The request signature we calculated does not match"})
            if p == "/model/denied.model/invoke":
                return self._json(403, {"message": "You don't have access to the model with the specified model ID."})
            if p.startswith("/openai/v1/chat/completions"):
                return self._json(200, openai_completion(doc.get("model")))
            model = p.split("/")[2]
            if doc.get("model") is not None or doc.get("anthropic_version") != "bedrock-2023-05-31":
                return self._json(400, {"message": "invalid body for Bedrock"})
            if p.endswith("/invoke"):
                return self._json(200, {**anthropic_message(model)})
            out = b"".join(encode_eventstream(
                {":event-type": "chunk", ":content-type": "application/json", ":message-type": "event"},
                json.dumps({"bytes": base64.b64encode(json.dumps(ev).encode()).decode()}).encode())
                for ev in anthropic_events(model))
            return self._send(200, out, "application/vnd.amazon.eventstream")
        # ---- Foundry
        if p == "/anthropic/v1/messages":
            if self.headers.get("Authorization") != "Bearer entra-token":
                return self._json(401, {"error": {"message": "no Entra token"}})
            return self._stream_or(doc, anthropic_message(doc.get("model")), anthropic_events(doc.get("model")))
        if p == "/openai/v1/chat/completions":
            if self.headers.get("api-key") != "foundry-key-from-env":
                return self._json(401, {"error": {"message": "no key"}})
            return self._json(200, openai_completion(doc.get("model")))
        # ---- Vertex
        if p.startswith("/v1/projects/proj-1/locations/us-east5/"):
            if self.headers.get("Authorization") != "Bearer gcp-token":
                return self._json(401, {"error": {"message": "no Google token"}})
            if p.endswith(":rawPredict") or p.endswith(":streamRawPredict"):
                if doc.get("model") is not None or doc.get("anthropic_version") != "vertex-2023-10-16":
                    return self._json(400, {"error": {"message": "invalid body for Vertex"}})
                model = p.rsplit("/", 1)[1].split(":")[0]
                return self._stream_or(doc, anthropic_message(model), anthropic_events(model))
            if p.endswith("/endpoints/openapi/chat/completions"):
                return self._json(200, openai_completion(doc.get("model")))
        self._json(404, {"message": f"no route {p}"})

    def _stream_or(self, doc: dict, message: dict, events: list[dict]) -> None:
        if not doc.get("stream"):
            return self._json(200, message)
        data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self._send(200, data, "text/event-stream")


@pytest.fixture
def stack(settings, graph, monkeypatch, tmp_path):
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Cloud)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    cloud = f"http://127.0.0.1:{up.server_address[1]}"
    token_file = tmp_path / "azure-token"
    token_file.write_text(FEDERATED)
    clouds = [
        {"name": "bedrock-us", "kind": "bedrock", "region": "us-east-1", "endpoint": cloud,
         "models": {"claude-on-bedrock": "global.anthropic.example-large-v1:0", "gpt-oss": "openai.example-open-1:0",
                    "denied": "denied.model"}},
        {"name": "foundry", "kind": "foundry", "endpoint": cloud, "auth": "entra",
         "models": {"claude-on-foundry": "example-medium"}},
        {"name": "foundry-key", "kind": "foundry", "endpoint": cloud, "auth": "key", "key_env": "FOUNDRY_KEY",
         "models": {"gpt-on-foundry": "chat-deployment"}},
        {"name": "vertex", "kind": "vertex", "project": "proj-1", "region": "us-east5", "endpoint": cloud,
         "auth": "metadata", "models": {"claude-on-vertex": "example-large", "gemini": "google/example-gemini"}}]
    env = {"CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS": json.dumps(clouds), "CAIRN_AI_GATEWAY_MODELS": "",
           "AWS_ACCESS_KEY_ID": AWS[0], "AWS_SECRET_ACCESS_KEY": AWS[1], "AWS_DEFAULT_REGION": "us-east-1",
           "AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": str(tmp_path / "none"),
           "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "none"),
           "AZURE_CLIENT_ID": "client-1", "AZURE_TENANT_ID": "tenant-1", "AZURE_FEDERATED_TOKEN_FILE": str(token_file),
           "AZURE_AUTHORITY_HOST": cloud, "GCE_METADATA_HOST": f"127.0.0.1:{up.server_address[1]}",
           "FOUNDRY_KEY": "foundry-key-from-env"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("CAIRN_AI_GATEWAY_ANTHROPIC_KEY", "CAIRN_AI_GATEWAY_ANTHROPIC_KEY_FILE", "CAIRN_AI_GATEWAY_OPENAI_URL",
              "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "AWS_PROFILE",
              "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="ana", effect="allow", actions=["USE_AI_GATEWAY"],
               condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    Cloud.seen.clear()
    Cloud.token_calls.update(entra=0, metadata=0)
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    key = s.agents.create_key_agent(AgentIn(name="Ana's agent", scopes=["model"], owner=ANA), ADMIN)["key"]
    yield s, f"http://127.0.0.1:{p}", key
    server.should_exit = True
    up.shutdown()


def sdks(url: str, key: str):
    import anthropic
    import openai

    return (anthropic.Anthropic(base_url=f"{url}/v1/ai/gateway/anthropic", api_key=key, max_retries=0),
            openai.OpenAI(base_url=f"{url}/v1/ai/gateway/openai/v1", api_key=key, max_retries=0))


def ask(claude, model: str) -> tuple[str, str]:
    msg = claude.messages.create(model=model, max_tokens=50, messages=[{"role": "user", "content": "Hi"}])
    with claude.messages.stream(model=model, max_tokens=50, messages=[{"role": "user", "content": "Hi"}]) as st:
        streamed = "".join(st.text_stream)
    return msg.content[0].text, streamed


def metered(s, actor: str) -> list[tuple[str, int, int]]:
    from sqlalchemy import select

    from cairn.ai.usage import usage_table

    with s.engine.connect() as c:
        return sorted((r.model, r.input_tokens, r.output_tokens)
                      for r in c.execute(select(usage_table).where(usage_table.c.actor == actor)))


def test_claude_and_openai_models_on_bedrock_signed_with_the_workloads_aws_identity(stack):
    s, url, key = stack
    claude, oai = sdks(url, key)
    assert ask(claude, "claude-on-bedrock") == (TEXT, TEXT)               # invoke, and the event stream turned back
    out = oai.chat.completions.create(model="gpt-oss", messages=[{"role": "user", "content": "Hi"}])
    assert out.choices[0].message.content == TEXT
    bedrock = [x for x in Cloud.seen if x["path"].startswith(("/model/", "/openai/"))]
    assert {x["path"] for x in bedrock} == {
        "/model/global.anthropic.example-large-v1%3A0/invoke",
        "/model/global.anthropic.example-large-v1%3A0/invoke-with-response-stream",
        "/openai/v1/chat/completions"}
    assert all(x["authorization"].startswith("AWS4-HMAC-SHA256 Credential=" + AWS[0]) for x in bedrock)
    assert next(x for x in bedrock if x["path"].endswith("/chat/completions"))["body"]["model"] == "openai.example-open-1:0"
    # Metered under Ana, the stream included (its usage read from the events inside AWS's stream).
    rows = metered(s, ANA)
    assert sum(1 for r in rows if r[1] == 13 and r[2] == 4) == 2 and any(r[1:] == (9, 3) for r in rows), rows
    # The cloud's refusal reaches the caller as an error of its own protocol.
    import anthropic

    with pytest.raises(anthropic.PermissionDeniedError, match="bedrock-us"):
        claude.messages.create(model="denied", max_tokens=5, messages=[{"role": "user", "content": "Hi"}])


def test_foundry_with_entra_workload_identity_or_a_key(stack):
    s, url, key = stack
    claude, oai = sdks(url, key)
    assert ask(claude, "claude-on-foundry") == (TEXT, TEXT)
    assert Cloud.token_calls["entra"] == 1                                # one token, kept until it nearly ends
    sent = [x for x in Cloud.seen if x["path"] == "/anthropic/v1/messages"]
    assert {x["body"]["model"] for x in sent} == {"example-medium"} and all(x["x-api-key"] is None for x in sent)
    out = oai.chat.completions.create(model="gpt-on-foundry", messages=[{"role": "user", "content": "Hi"}])
    assert out.choices[0].message.content == TEXT
    assert [x["body"]["model"] for x in Cloud.seen if x["path"] == "/openai/v1/chat/completions"
            and x["api-key"]] == ["chat-deployment"]


def test_vertex_with_gke_workload_identity(stack):
    s, url, key = stack
    claude, oai = sdks(url, key)
    assert ask(claude, "claude-on-vertex") == (TEXT, TEXT)
    out = oai.chat.completions.create(model="gemini", messages=[{"role": "user", "content": "Hi"}])
    assert out.choices[0].message.content == TEXT
    paths = {x["path"] for x in Cloud.seen if x["path"].startswith("/v1/projects/")}
    assert paths == {"/v1/projects/proj-1/locations/us-east5/publishers/anthropic/models/example-large:rawPredict",
                     "/v1/projects/proj-1/locations/us-east5/publishers/anthropic/models/example-large:streamRawPredict",
                     "/v1/projects/proj-1/locations/us-east5/endpoints/openapi/chat/completions"}
    assert Cloud.token_calls["metadata"] == 1


def test_the_models_are_governed_like_any_other(stack):
    import httpx

    s, url, key = stack
    data = httpx.get(f"{url}/v1/ai/gateway/models", headers={"X-Cairn-Actor": ANA}).json()["data"]
    listed = [m["id"] for m in data]
    assert {"claude-on-bedrock", "gpt-oss", "claude-on-foundry", "gpt-on-foundry", "claude-on-vertex",
            "gemini"} <= set(listed)
    # Each says where it runs (shown in the UI), and nothing about how Cairn signs in there.
    by = {m["id"]: m.get("served_by") for m in data}
    assert by["claude-on-bedrock"]["kind"] == "bedrock" and by["claude-on-bedrock"]["region"] == "us-east-1"
    assert by["gpt-on-foundry"]["kind"] == "foundry" and by["gpt-on-foundry"]["model_id"] == "chat-deployment"
    assert by["gemini"]["model_id"] == "google/example-gemini"
    assert all(set(v) == {"upstream", "kind", "region", "model_id"} for v in by.values() if v is not None)
    # Someone without USE_AI_GATEWAY is refused before any cloud is reached.
    before = len(Cloud.seen)
    r = httpx.post(f"{url}/v1/ai/gateway/anthropic/v1/messages", headers={"X-Cairn-Actor": "urn:li:corpuser:eve"},
                   json={"model": "claude-on-bedrock", "max_tokens": 5, "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 403 and len(Cloud.seen) == before


def test_configuration_holds_no_secret_and_is_checked():
    for bad, why in (([{"name": "b", "kind": "bedrock", "region": "us-east-1", "models": {"m": "x"}, "secret": "s"}],
                      "no secret"),
                     ([{"name": "f", "kind": "foundry", "endpoint": "https://x", "auth": "key", "models": {"m": "x"}}],
                      "key_env"),
                     ([{"name": "v", "kind": "vertex", "region": "us-east5", "models": {"m": "x"}}], "project"),
                     ([{"name": "x", "kind": "openrouter", "models": {"m": "x"}}], "kind"),
                     ([{"name": "b", "kind": "bedrock", "region": "r", "models": {}}], "models")):
        with pytest.raises(ValueError, match=why):
            load(json.dumps(bad))
    up = CloudUpstream.from_config({"name": "v", "kind": "vertex", "project": "p", "region": "global", "models": {"m": "x"}})
    assert up.auth == "metadata" and up.serves("anthropic") and up.serves("openai")


def test_a_damaged_event_stream_is_refused():
    msg = bytearray(encode_eventstream({":event-type": "chunk"}, b'{"bytes": ""}'))
    assert eventstream_messages(bytearray(msg))[0][0][":event-type"] == "chunk"
    msg[-6] ^= 0xFF
    with pytest.raises(EventStreamError, match="checksum"):
        eventstream_messages(msg)


def test_endpoints_are_https_and_keys_never_cairns_own_settings():
    base = {"name": "v", "kind": "vertex", "project": "p", "region": "r", "models": {"m": "x"}}
    with pytest.raises(ValueError, match="https"):
        CloudUpstream.from_config({**base, "endpoint": "http://proxy.internal:8080"})
    CloudUpstream.from_config({**base, "endpoint": "http://127.0.0.1:9"})          # this machine (a sidecar)
    key = {"name": "f", "kind": "foundry", "endpoint": "https://x", "auth": "key", "models": {"m": "x"}}
    for bad in ("CAIRN_SERVICE_TOKEN", "CAIRN_SECRETS_KEY", "lower"):
        with pytest.raises(ValueError, match="key_env"):
            CloudUpstream.from_config({**key, "key_env": bad})
    for ok in ("CAIRN_AI_GATEWAY_FOUNDRY_KEY", "FOUNDRY_KEY"):
        CloudUpstream.from_config({**key, "key_env": ok})


def test_betas_reach_each_cloud_as_it_takes_them(stack):
    import asyncio

    s, url, key = stack
    gw = s.ai_gateway
    body = {"model": "x", "max_tokens": 5, "messages": []}
    beta = {"anthropic-beta": "a-beta,b-beta"}
    _, _, data, _ = asyncio.run(gw.clouds["bedrock-us"].request("anthropic", "/v1/messages", body, "claude-on-bedrock", beta))
    assert json.loads(data)["anthropic_beta"] == ["a-beta", "b-beta"]
    _, _, data, _ = asyncio.run(gw.clouds["vertex"].request("anthropic", "/v1/messages", body, "claude-on-vertex", beta))
    assert json.loads(data)["anthropic_beta"] == ["a-beta", "b-beta"]
    _, headers, _, _ = asyncio.run(gw.clouds["foundry"].request("anthropic", "/v1/messages", body, "claude-on-foundry", beta))
    assert headers["anthropic-beta"] == "a-beta,b-beta"


def test_a_damaged_bedrock_stream_ends_with_an_error_event():
    import asyncio

    from cairn.ai.cloud_providers import bedrock_anthropic_sse

    good = encode_eventstream({":event-type": "chunk"}, json.dumps({"bytes": base64.b64encode(
        json.dumps({"type": "message_stop"}).encode()).decode()}).encode())
    bad = bytearray(good)
    bad[-6] ^= 0xFF

    async def run(chunks):
        async def raw():
            for c in chunks:
                yield c
        return [x async for x in bedrock_anthropic_sse(raw())]
    assert asyncio.run(run([good]))[0].startswith(b"event: message_stop")
    out = asyncio.run(run([bytes(bad)]))
    assert len(out) == 1 and out[0].startswith(b"event: error") and b"checksum" in out[0]


def test_an_explicit_model_list_stays_the_list(monkeypatch):
    from cairn.ai.cloud_providers import load as load_clouds
    from cairn.ai.gateway import AiGateway

    clouds = load_clouds(json.dumps([{"name": "b", "kind": "bedrock", "region": "us-east-1",
                                      "models": {"one": "x", "two": "y"}}]))
    gw = AiGateway(object(), {}, ["one"], clouds=clouds)
    assert gw.models == ["one"] and gw.endpoint_for("anthropic", "two") == "b"   # routed if listed, served if listed
    assert AiGateway(object(), {}, [], clouds=clouds).models == ["one", "two"]
