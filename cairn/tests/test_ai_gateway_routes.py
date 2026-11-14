"""Dynamic model routing in the AI gateway (L113), through the real services, decision point, database and HTTP API,
with the official Anthropic SDK as the client and two provider stand-ins (the gateway's primary Anthropic upstream and
a second, named upstream). Checked:
- a route falls back to the next model on a 429, a 5xx, a timeout, and before a stream starts; not on a 400;
- metered on the model that answered, which the response names; every routed call audited with its attempts;
- candidates the caller may not use are skipped (never sent to the provider), none left is refused (403);
- the cost ceiling, the cheapest-first strategy, required capabilities, input size and the latency budget;
- the built-in cairn-auto; the operator API (MANAGE_SYSTEM_OPERATIONS, validated, audited; agents refused)."""

import http.server
import json
import threading
import time

import httpx
import pytest
import uvicorn

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.identity.agents import AgentIn
from cairn.services import build_services
from test_ai_gateway import Provider
from test_trino_e2e import port, wait

ANA, BOB, CAL, ADMIN = ("urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cal", "urn:li:corpuser:admin")
BIG, MID, SMALL = "acme-large", "acme-medium", "acme-small"        # BIG and MID on the primary, SMALL on the backup
pytest.importorskip("anthropic")


class Primary(Provider):
    """The primary upstream: answers, or fails as the test says (429, 500, 400, slow)."""
    mode = "ok"
    hits: list = []

    def do_POST(self):
        Primary.hits.append(Primary.mode)
        if Primary.mode == "slow":
            time.sleep(3)
            return super().do_POST()
        if Primary.mode == "ok":
            return super().do_POST()
        self.rfile.read(int(self.headers["Content-Length"]))
        code, kind = {"429": (429, "rate_limit_error"), "500": (500, "api_error"),
                      "400": (400, "invalid_request_error")}[Primary.mode]
        self._json(code, {"type": "error", "error": {"type": kind, "message": f"primary says {code}"}})


class Backup(Provider):
    hits: list = []

    def do_POST(self):
        Backup.hits.append(1)
        return super().do_POST()


@pytest.fixture
def stack(settings, graph, monkeypatch):
    servers = []
    for handler in (Primary, Backup):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    primary, backup = (f"http://127.0.0.1:{s.server_address[1]}" for s in servers)
    for k, v in {"CAIRN_AI_GATEWAY_ANTHROPIC_URL": primary, "CAIRN_AI_GATEWAY_ANTHROPIC_KEY": "sk-ant-upstream",
                 "CAIRN_AI_GATEWAY_UPSTREAMS": f"backup=anthropic@{backup}",
                 "CAIRN_AI_GATEWAY_UPSTREAM_BACKUP_KEY": "sk-ant-upstream",
                 "CAIRN_AI_GATEWAY_MODEL_UPSTREAMS": f"{SMALL}=backup",
                 "CAIRN_AI_GATEWAY_MODELS": f"{BIG},{MID},{SMALL}",
                 "CAIRN_AI_PRICES": json.dumps({BIG: [10, 50], MID: [2, 10], SMALL: [0.1, 0.5]})}.items():
        monkeypatch.setenv(k, v)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    models = [f"urn:li:aiModel:{m}" for m in (BIG, MID, SMALL)]
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="ana", effect="allow", actions=["USE_AI_GATEWAY"], target=Target(resource_urns=models),
               condition={"attr": "subject.urn", "op": "eq", "value": ANA}),
        Policy(id="bob", effect="allow", actions=["USE_AI_GATEWAY"],
               target=Target(resource_urns=[f"urn:li:aiModel:{SMALL}"]),
               condition={"attr": "subject.urn", "op": "eq", "value": BOB})]))
    Primary.mode, Primary.hits[:], Backup.hits[:] = "ok", [], []
    Provider.seen.clear()
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield s, f"http://127.0.0.1:{p}"
    server.should_exit = True
    for srv in servers:
        srv.shutdown()


def put_route(url, name, body, who=ADMIN):
    return httpx.put(f"{url}/v1/ai/gateway/routes/{name}", json=body, headers={"X-Cairn-Actor": who})


def ask(url, who, model, **extra):
    body = {"model": model, "max_tokens": 1000, "messages": [{"role": "user", "content": "Hi"}], **extra}
    return httpx.post(f"{url}/v1/ai/gateway/anthropic/v1/messages", json=body, headers={"X-Cairn-Actor": who},
                      timeout=30)


def usage_models(s, actor):
    from sqlalchemy import select

    from cairn.ai.usage import usage_table

    with s.engine.connect() as c:
        return [r.model for r in c.execute(select(usage_table).where(usage_table.c.actor == actor)
                                           .order_by(usage_table.c.id))]


def routed(s):
    return [e for e in s.audit.entries("ai", limit=500) if e["action"] == "gateway.routed"]


def test_a_route_falls_back_and_says_which_model_answered(stack):
    import anthropic

    s, url = stack
    assert put_route(url, "resilient", {"candidates": [{"model": BIG, "timeout_s": 1}, SMALL]}).status_code == 200
    key = s.agents.create_key_agent(AgentIn(name="Ana dev", scopes=["model"], owner=ANA), ADMIN)["key"]
    claude = anthropic.Anthropic(base_url=f"{url}/v1/ai/gateway/anthropic", api_key=key, max_retries=0)

    # The primary rate-limits: the backup's model answers; the response, the meter and the audit say so.
    Primary.mode = "429"
    raw = claude.messages.with_raw_response.create(model="resilient", max_tokens=100,
                                                   messages=[{"role": "user", "content": "Hi"}])
    msg = raw.parse()
    assert msg.content[0].text == "Hello from the model" and msg.model == SMALL
    assert raw.headers["x-cairn-model"] == SMALL and raw.headers["x-cairn-route"] == "resilient"
    assert Primary.hits == ["429"] and len(Backup.hits) == 1
    assert usage_models(s, ANA) == [SMALL]
    last = routed(s)[0]["details"]
    assert last["model"] == SMALL and [a["model"] for a in last["attempts"]] == [BIG, SMALL]
    assert last["attempts"][0]["status"] == 429 and last["attempts"][1]["outcome"] == "ok"

    # A server error, and a timeout (the candidate's 1 s against a provider taking 3 s), fall back too.
    Primary.mode = "500"
    assert claude.messages.create(model="resilient", max_tokens=10, messages=[{"role": "user", "content": "Hi"}]
                                  ).model == SMALL
    Primary.mode = "slow"
    t0 = time.monotonic()
    assert claude.messages.create(model="resilient", max_tokens=10, messages=[{"role": "user", "content": "Hi"}]
                                  ).model == SMALL
    assert time.monotonic() - t0 < 2.5
    assert routed(s)[0]["details"]["attempts"][0]["outcome"] == "timeout"

    # Streams fall back before the first byte.
    Primary.mode = "429"
    with claude.messages.stream(model="resilient", max_tokens=10, messages=[{"role": "user", "content": "Hi"}]) as st:
        assert "".join(st.text_stream) == "Hello from the model"
        assert st.get_final_message().model == SMALL
    assert usage_models(s, ANA) == [SMALL] * 4

    # A request the provider rejects (400) is the answer: retrying elsewhere would not help.
    Primary.mode, before = "400", len(Backup.hits)
    r = ask(url, ANA, "resilient")
    assert r.status_code == 400 and "primary says 400" in r.text and len(Backup.hits) == before

    # Every candidate failing: the caller sees each attempt's outcome.
    put_route(url, "fragile", {"candidates": [BIG, MID]})
    Primary.mode = "500"
    r = ask(url, ANA, "fragile")
    assert r.status_code == 500 and f"{BIG}: error (500)" in r.json()["error"]["message"]

    # The built-in cairn-auto: every served model in order (BIG and MID rate-limited, SMALL answers).
    Primary.mode = "429"
    r = ask(url, ANA, "cairn-auto")
    assert r.status_code == 200 and r.json()["model"] == SMALL and r.headers["x-cairn-route"] == "cairn-auto"


def test_permissions_cost_capabilities_size_and_latency(stack):
    s, url = stack
    put_route(url, "resilient", {"candidates": [BIG, SMALL]})

    # Bob may use SMALL only: BIG is skipped without reaching the provider. Cal may use none: refused.
    r = ask(url, BOB, "resilient")
    assert r.status_code == 200 and r.json()["model"] == SMALL and Primary.hits == []
    assert routed(s)[0]["details"]["attempts"][0] == {"model": BIG, "skipped": "not permitted"}
    r = ask(url, CAL, "resilient")
    assert r.status_code == 403 and "may not use any model" in r.text and Primary.hits == [] and len(Backup.hits) == 1
    assert usage_models(s, BOB) == [SMALL] and usage_models(s, CAL) == []

    # Cost ceiling: 1,000 output tokens cost about $0.05 on BIG, $0.01 on MID, $0.0005 on SMALL.
    put_route(url, "thrifty", {"candidates": [BIG, MID, SMALL], "max_cost_usd": 0.005})
    r = ask(url, ANA, "thrifty")
    assert r.status_code == 200 and r.json()["model"] == SMALL and Primary.hits == []
    skipped = routed(s)[0]["details"]["attempts"]
    assert [a["model"] for a in skipped if "over the route" in a.get("skipped", "")] == [BIG, MID]
    put_route(url, "tight", {"candidates": [BIG, MID], "max_cost_usd": 0.005})
    r = ask(url, ANA, "tight")
    assert r.status_code == 422 and "fits this request" in r.text

    # Cheapest first, whatever the order.
    put_route(url, "cheap-first", {"candidates": [BIG, SMALL], "strategy": "cheapest"})
    assert ask(url, ANA, "cheap-first").json()["model"] == SMALL and Primary.hits == []

    # A request using tools skips candidates without them; a long one skips small context windows.
    put_route(url, "tooling", {"candidates": [{"model": SMALL, "capabilities": []},
                                              {"model": MID, "capabilities": ["tools"]}]})
    tools = [{"name": "lookup", "description": "Look up", "input_schema": {"type": "object", "properties": {}}}]
    assert ask(url, ANA, "tooling", tools=tools).json()["model"] == MID
    assert ask(url, ANA, "tooling").json()["model"] == SMALL
    put_route(url, "short", {"candidates": [{"model": SMALL, "max_input_tokens": 50}, MID]})
    long = [{"role": "user", "content": "word " * 400}]
    assert ask(url, ANA, "short", messages=long).json()["model"] == MID
    assert ask(url, ANA, "short").json()["model"] == SMALL

    # The latency budget covers every attempt: spent on a slow first model, the route gives up (504).
    put_route(url, "hasty", {"candidates": [BIG, SMALL], "latency_budget_ms": 800})
    Primary.mode, before = "slow", len(Backup.hits)
    t0 = time.monotonic()
    r = ask(url, ANA, "hasty")
    assert r.status_code == 504 and time.monotonic() - t0 < 2.5 and len(Backup.hits) == before
    assert "latency budget" in r.text
    Primary.mode = "ok"


def test_operators_define_routes(stack):
    s, url = stack
    assert put_route(url, "resilient", {"candidates": [BIG, SMALL]}, who=BOB).status_code == 403
    bad = put_route(url, "resilient", {"candidates": ["no-such-model"]})
    assert bad.status_code == 422 and "not a model the gateway serves" in bad.text
    assert put_route(url, SMALL, {"candidates": [BIG]}).status_code == 422          # a served model's name
    assert put_route(url, "resilient", {"candidates": [BIG], "max_cost_usd": -1}).status_code == 422
    assert put_route(url, "resilient", {"candidates": [BIG], "strategy": "random"}).status_code == 422
    ok = put_route(url, "resilient", {"description": "Large, then small", "candidates": [
        {"model": BIG, "capabilities": ["tools"], "timeout_s": 20}, SMALL], "latency_budget_ms": 30000})
    assert ok.status_code == 200 and ok.json()["candidates"][0] == {"model": BIG, "capabilities": ["tools"],
                                                                    "timeout_s": 20.0}
    # An agent's credential (scope "model") reads routes but never changes them.
    bot = s.agents.create_key_agent(AgentIn(name="Bot", scopes=["model"], owner=ADMIN), ADMIN)["key"]
    bearer = {"Authorization": f"Bearer {bot}"}
    assert httpx.put(f"{url}/v1/ai/gateway/routes/x1", json={"candidates": [BIG]}, headers=bearer).status_code == 403
    names = [r["name"] for r in httpx.get(f"{url}/v1/ai/gateway/routes", headers=bearer).json()["routes"]]
    assert names == ["cairn-auto", "resilient"]
    # The models list shows each person the routes they may use, with the candidates open to them.
    bob = httpx.get(f"{url}/v1/ai/gateway/models", headers={"X-Cairn-Actor": BOB}).json()["data"]
    assert [m["id"] for m in bob] == [SMALL, "cairn-auto", "resilient"]
    assert bob[-1]["candidates"] == [SMALL] and bob[-1]["cairn_route"] is True
    assert httpx.delete(f"{url}/v1/ai/gateway/routes/resilient", headers={"X-Cairn-Actor": BOB}).status_code == 403
    assert httpx.delete(f"{url}/v1/ai/gateway/routes/resilient", headers={"X-Cairn-Actor": ADMIN}).status_code == 204
    assert ask(url, ANA, "resilient").status_code == 403                             # no longer a model
    actions = [e["action"] for e in s.audit.entries("ai", limit=100)]
    assert actions.count("gateway.route_saved") == 1 and actions.count("gateway.route_deleted") == 1
