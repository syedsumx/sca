"""The AI builder (cairn.sqlworkspace.builder) without a model or Trino: the model's calls are the person's, held to
their daily AI allowance (the whole flow, on real Trino, is in tests/test_trino_e2e.py)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cairn.ai.usage import MeteredProvider, UsageMeter, current_actor
from cairn.ai.llm import LLMUsage
from cairn.common.db import metadata
from cairn.sqlworkspace.builder import Builder, BuilderError

ANA = "urn:li:corpuser:ana"
URN = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.orders,PROD)"
PLAN = {"title": "Orders", "controls": [],
        "tiles": [{"kind": "element", "title": "Orders by region",
                   "element": {"source": {"table": "sales.orders"}, "group_by": ["region"],
                               "aggregates": [{"name": "n", "formula": "Count()"}]}}]}


class _Model:
    name = "static"

    def __init__(self):
        self.callers: list[str] = []

    def generate_json(self, system, user, schema, *, model=None, max_tokens=16000, **kw):
        self.callers.append(current_actor())
        return PLAN, LLMUsage(model="m", input_tokens=600, output_tokens=400)


class _Ask:
    def _tables(self, question, urns, can_view):
        return SimpleNamespace(connection="lake"), {URN: ("sales.orders", ["region", "amount"])}


class _Boards:
    def _tile(self, actor, t):
        return t

    def _controls(self, raw, tiles):
        return list(raw)

    def _run_element(self, actor, t, filters, path):
        return {**t, "result": {"columns": ["region", "n"], "rows": []}}


@pytest.fixture
def built():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata.create_all(engine)
    model = _Model()
    meter = UsageMeter(engine, user_daily_tokens=1500, prices={})
    b = Builder(MeteredProvider(model, meter), None, _Ask(), None, _Boards(), lambda c, t: URN)
    return b, model, meter


def test_the_builders_model_calls_are_the_persons_and_held_to_their_allowance(built):
    b, model, meter = built
    out = b.build(ANA, "orders by region", lambda urn: True)
    assert [t["title"] for t in out["tiles"]] == ["Orders by region"]
    assert model.callers == [ANA]                                   # attributed to Ana, not to "unknown"
    assert meter.tokens_today(ANA) == 1000
    b.build(ANA, "orders by region", lambda urn: True)              # 2,000 tokens now: over her 1,500 a day
    with pytest.raises(BuilderError) as e:
        b.build(ANA, "orders by region", lambda urn: True)
    assert e.value.status == 429
    assert current_actor() == "unknown"                             # and nothing left behind for the next request
