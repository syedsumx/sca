"""AI usage metering, cost estimates and budgets."""

import contextvars
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cairn.ai.llm import LLMError, LLMUsage, StaticProvider
from cairn.ai.usage import (LLMBudgetExceeded, MeteredProvider, UsageMeter, estimate_cost, llm_task, load_prices,
                            set_actor, usage_table)
from cairn.app import create_api_app
from cairn.common.security import Principal
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

from test_ai import ALICE, graphql_handler  # noqa: F401 - reuse the catalog fixture data

BOB = "urn:li:corpuser:bob"


class _Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _provider(model: str = "claude-opus-5-5", tokens=(1000, 200), name: str = "anthropic"):
    p = StaticProvider(lambda s, u, sch: {"ok": True})
    p.name = name
    inner = p.generate_json

    def gen(system, user, schema, *, model=None, max_tokens=16000):
        data, _ = inner(system, user, schema, model=model, max_tokens=max_tokens)
        return data, LLMUsage(model=model_name, input_tokens=tokens[0], output_tokens=tokens[1])

    model_name = model
    p.generate_json = gen
    return p


def test_prices_and_cost_estimate():
    assert estimate_cost("claude-opus-5-5", 1_000_000, 1_000_000) == 24.0
    assert estimate_cost("claude-haiku-5-5", 2_000_000, 0) == 0.2
    # Longest prefix wins: an Opus 5 snapshot is not priced as Opus 5.5.
    assert estimate_cost("claude-opus-5-20260101", 1_000_000, 0) == 5.0
    assert estimate_cost("llama3.1", 10**6, 10**6, provider="ollama") == 0.0
    assert estimate_cost("unknown-model", 10**6, 10**6) == 0.0
    # Cache reads cost a tenth of input, cache writes a premium.
    assert estimate_cost("claude-opus-5-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.4)
    assert estimate_cost("claude-opus-5-5", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(5.0)
    prices = load_prices(json.dumps({"claude-opus-5-5": [3, 15], "my-gateway-model": [1, 2]}))
    assert estimate_cost("claude-opus-5-5", 10**6, 10**6, prices=prices) == 18.0
    assert estimate_cost("my-gateway-model", 10**6, 10**6, prices=prices) == 3.0
    with pytest.raises(ValueError):
        load_prices('{"x": 1}')


def test_metered_calls_are_recorded_per_actor_and_task(engine):
    clock = _Clock()
    meter = UsageMeter(engine, clock=clock)
    llm = MeteredProvider(_provider(), meter)
    set_actor(ALICE)
    with llm_task("nlq.plan"):
        _, usage = llm.generate_json("s", "u", {})
    assert usage.extra["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)
    set_actor(BOB)
    with llm_task("docgen.draft"):
        llm.generate_json("s", "u", {})
        llm.generate_json("s", "u", {})
    by_task = {g["key"]: g for g in meter.summary(group_by="task")["groups"]}
    assert by_task["docgen.draft"]["calls"] == 2 and by_task["nlq.plan"]["calls"] == 1
    by_actor = {g["key"]: g for g in meter.summary(group_by="actor")["groups"]}
    assert by_actor[BOB]["input_tokens"] == 2000 and by_actor[ALICE]["output_tokens"] == 200
    total = meter.summary(group_by="model")["total"]
    assert total["calls"] == 3 and total["cost_usd"] == pytest.approx(3 * 0.008)
    assert meter.summary(group_by="day")["groups"][0]["key"] == "2027-01-15"
    assert meter.summary(actor=ALICE)["total"]["calls"] == 1
    with pytest.raises(ValueError):
        meter.summary(group_by="nope")


def test_actor_does_not_leak_between_concurrent_requests(engine):
    meter = UsageMeter(engine)
    llm = MeteredProvider(_provider(), meter)
    barrier = threading.Barrier(2)

    def request(actor):
        set_actor(actor)
        barrier.wait()            # both requests have set their actor before either calls the model
        llm.generate_json("s", "u", {})

    threads = [threading.Thread(target=contextvars.copy_context().run, args=(request, a)) for a in (ALICE, BOB)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    actors = sorted(g["key"] for g in meter.summary(group_by="actor")["groups"])
    assert actors == sorted([ALICE, BOB])


def test_daily_budget_and_per_user_allowance(engine):
    clock = _Clock()
    meter = UsageMeter(engine, daily_budget_usd=0.02, clock=clock)
    llm = MeteredProvider(_provider(), meter)       # 0.008 per call
    set_actor(ALICE)
    for _ in range(3):
        llm.generate_json("s", "u", {})
    with pytest.raises(LLMBudgetExceeded) as e:
        llm.generate_json("s", "u", {})
    assert isinstance(e.value, LLMError) and "$0.02" in str(e.value)
    clock.t += 86_400                                 # a new day resets the budget
    llm.generate_json("s", "u", {})

    meter = UsageMeter(engine, user_daily_tokens=2000, clock=clock)
    llm = MeteredProvider(_provider(), meter)       # 1200 tokens per call; Alice already used 1200 today
    llm.generate_json("s", "u", {})
    with pytest.raises(LLMBudgetExceeded):
        llm.generate_json("s", "u", {})
    set_actor(BOB)
    llm.generate_json("s", "u", {})                 # Bob has his own allowance


def test_self_hosted_models_cost_nothing(engine):
    meter = UsageMeter(engine, daily_budget_usd=0.0001)
    llm = MeteredProvider(_provider("llama3.1", name="ollama"), meter)
    for _ in range(3):
        llm.generate_json("s", "u", {})
    assert meter.summary()["total"]["cost_usd"] == 0.0


@pytest.fixture
def services(settings, graph, monkeypatch):
    graph.graphql_handler = graphql_handler

    def responder(system, user, schema):
        if "citations" in schema.get("properties", {}):
            return {"answer": "orders holds order data.", "citations": []}
        return {"intent": "find", "query": "orders", "entity_types": ["dataset"], "filters": [],
                "lineage": {"direction": "NONE", "target": "", "max_hops": 1}, "limit": 5}

    llm = StaticProvider(responder)
    s = build_services(settings, graph=graph, llm=llm, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="all-view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="ops", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS"],
               condition={"attr": "subject.urn", "op": "eq", "value": ALICE})]))
    return s


def test_feature_calls_are_attributed_and_audited(services):
    p = Principal(BOB, "dev")
    services.ai_actor(p)
    out = services.nlq.ask("show me orders tables", services.can_view(p))
    assert out["planner"] == "llm"
    tasks = {g["key"] for g in services.ai_usage.summary(actor=BOB)["groups"]}
    assert tasks == {"nlq.plan", "nlq.answer"}
    audit = [e for e in services.audit.entries(category="ai")]
    assert {e["action"] for e in audit} >= {"nlq.plan", "nlq.answer"} and all(e["actor"] == BOB for e in audit)


def test_over_budget_features_fall_back(services):
    services.ai_usage.daily_budget_usd = 0.0
    services.llm.inner.name = "anthropic"            # priced, so the zero budget applies
    p = Principal(BOB, "dev")
    services.ai_actor(p)
    out = services.nlq.ask("show me orders tables", services.can_view(p))
    assert out["planner"] == "heuristic" and out["results"]
    with services.engine.connect() as conn:
        assert conn.execute(usage_table.select()).first() is None


def test_usage_endpoint(services):
    services.ai_actor(Principal(BOB, "dev"))
    services.nlq.ask("show me orders tables", services.can_view(Principal(BOB, "dev")))
    c = TestClient(create_api_app(services))
    bob = {"X-Cairn-Actor": BOB}
    assert c.get("/ai/usage", headers=bob).status_code == 403
    mine = c.get("/ai/usage?mine=true", headers=bob).json()
    assert mine["total"]["calls"] == 2
    alice = {"X-Cairn-Actor": ALICE}
    assert c.get("/ai/usage?mine=true", headers=alice).json()["total"]["calls"] == 0
    everyone = c.get("/ai/usage?group_by=actor", headers=alice).json()
    assert [g["key"] for g in everyone["groups"]] == [BOB] and "budget" in everyone
    assert c.get("/ai/usage?group_by=bogus", headers=alice).status_code == 400


class _Resp(SimpleNamespace):
    pass


class _FakeClient:
    def __init__(self):
        self.messages = SimpleNamespace(create=self.create)
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        return _Resp(model="claude-opus-5-5", stop_reason="end_turn",
                     content=[{"type": "text", "text": "Orders live in [urn:li:dataset:(x)]."}],
                     usage=SimpleNamespace(input_tokens=500, output_tokens=50, cache_read_input_tokens=1000,
                                           cache_creation_input_tokens=0))


def test_assistant_is_metered_and_falls_back_over_budget(services):
    fake = _FakeClient()
    services.llm.inner.name = "anthropic"
    services.llm.inner.client = fake
    services.llm.inner.server_side_fallback = False
    services.assistant.llm = services.llm
    p = Principal(BOB, "dev")
    out = services.assistant.chat(p, "where are orders?")
    assert fake.calls == 1 and "Orders live" in out["reply_md"]
    g = services.ai_usage.summary(actor=BOB)["groups"]
    assert [x["key"] for x in g] == ["assistant.chat"]
    assert g[0]["cost_usd"] == pytest.approx((500 * 4 + 1000 * 0.4 + 50 * 20) / 1e6)

    services.ai_usage.daily_budget_usd = 0.0
    out = services.assistant.chat(p, "where are orders?")
    assert fake.calls == 1 and "AI answers are paused" in out["reply_md"]
