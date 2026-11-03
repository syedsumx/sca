"""The AI-functions gateway (cairn.ai.sql_gateway) without Trino: models, budgets, rate limit, usage, the
provider's key, and per-person daily caps. The path through real Trino is in tests/test_trino_e2e.py."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cairn.ai.sql_gateway import SqlAiGateway, build_sql_ai_router
from cairn.ai.usage import UsageMeter
from cairn.common.db import init_db


def provider(answer):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=answer)
    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def meter(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(e)
    return UsageMeter(e)


def test_anthropic_calls_carry_cairns_key_and_are_metered(meter):
    seen, http = provider({"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "positive"}],
                           "usage": {"input_tokens": 120, "output_tokens": 3}})
    g = SqlAiGateway(meter, "https://api.anthropic.com", "sk-cairn", "anthropic", ["claude-haiku-5-5"], http=http)
    status, out = asyncio.run(g.forward("/v1/messages", {"model": "claude-haiku-5-5", "messages": []}))
    assert status == 200 and out["content"][0]["text"] == "positive"
    assert seen[0].headers["x-api-key"] == "sk-cairn" and str(seen[0].url) == "https://api.anthropic.com/v1/messages"
    row = meter.summary(group_by="actor")["groups"][0]
    assert (row["key"], row["input_tokens"], row["output_tokens"]) == ("trino-sql", 120, 3) and row["cost_usd"] > 0


def test_models_budget_and_rate(meter):
    seen, http = provider({"model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    g = SqlAiGateway(meter, "http://llm:8000", "k", "openai", ["m"], rpm=2, http=http)
    assert asyncio.run(g.forward("/v1/chat/completions", {"model": "other"}))[0] == 403
    assert [asyncio.run(g.forward("/v1/chat/completions", {"model": "m"}))[0] for _ in range(3)] == [200, 200, 429]
    meter.daily_budget_usd = 0.0
    status, out = asyncio.run(g.forward("/v1/chat/completions", {"model": "m"}))
    assert status == 429 and "budget" in out["error"]["message"]
    assert len(seen) == 2                                  # refused calls never reached the provider


def test_people_have_a_daily_cap(meter):
    g = SqlAiGateway(meter, "http://llm:8000", "k", "openai", user_daily_queries=2)
    assert [g.admit("urn:li:corpuser:ana") for _ in range(3)] == [True, True, False]
    assert g.admit("urn:li:corpuser:bob") is True
    meter.user_daily_tokens = 0                            # a person's token allowance applies too
    assert g.admit("urn:li:corpuser:bob") is False


def test_the_route_takes_only_trinos_key_and_an_unreachable_provider_is_502(meter):
    def down(request):
        raise httpx.ConnectError("refused")
    g = SqlAiGateway(meter, "http://llm:8000", "k", "openai", http=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    app = FastAPI()
    app.include_router(build_sql_ai_router(g, "trino-key"))
    c = TestClient(app)
    assert c.post("/v1/trino/wrong/ai/v1/chat/completions", json={"model": "m"}).status_code == 404
    r = c.post("/v1/trino/trino-key/ai/v1/chat/completions", json={"model": "m"})
    assert r.status_code == 502 and "not reachable" in r.json()["error"]["message"]
    with pytest.raises(ValueError):
        SqlAiGateway(meter, "llm:8000", "k")
