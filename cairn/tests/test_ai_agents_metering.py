"""The pipeline agent's and the semantic model autopilot's model calls are metered to the person asking, so the
per-person daily allowance (CAIRN_AI_USER_DAILY_TOKENS) holds for them as for the assistant. Trino and the model
are stand-ins here; tests/test_trino_e2e.py runs both against a real Trino."""

import threading

import pytest
from sqlalchemy import select

from cairn.ai.llm import LLMUsage
from cairn.ai.usage import MeteredProvider, UsageMeter, set_actor, usage_table
from cairn.common.db import get_engine, init_db

ANA = "urn:li:corpuser:ana"


class Model:
    name = "anthropic"

    def __init__(self, reply):
        self.reply = reply

    def generate_json(self, system, user, schema, *, model=None, max_tokens=16000, images=None):
        return self.reply, LLMUsage(model="m", input_tokens=400, output_tokens=200)


@pytest.fixture
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(e)
    return e


def actors(engine) -> list[str]:
    with engine.connect() as c:
        return [r.actor for r in c.execute(select(usage_table.c.actor))]


def in_a_request(fn):
    """As the API runs it: on a worker thread, with no actor set beforehand."""
    out: dict = {}

    def go():
        set_actor(None)
        try:
            out["value"] = fn()
        except Exception as e:  # noqa: BLE001 - handed to the test
            out["error"] = e
    t = threading.Thread(target=go)
    t.start()
    t.join()
    if "error" in out:
        raise out["error"]
    return out["value"]


def test_the_pipeline_agent_is_metered_to_the_person(engine):
    from cairn.ai.pipeline_agent import PipelineAgent, PipelineAgentError
    from cairn.pipelines.derived import DerivedTables

    plan = {"sql": "SELECT region, sum(amount) AS revenue FROM lakehouse.sales.orders GROUP BY region",
            "mode": "full", "watermark": None, "key": [], "every_minutes": 60, "on_change": False, "checks": [],
            "explanation": "revenue by region"}
    llm = MeteredProvider(Model(plan), UsageMeter(engine, user_daily_tokens=500))
    derived = DerivedTables(engine, lambda owner, sql: [], "lakehouse")

    def run(actor, sql, limit):
        return ["region", "revenue"], [["eu", 10]], False
    agent = PipelineAgent(engine, llm, None, derived, run, lambda actor, goal, urns, cv: {"sales.orders": ["region", "amount"]})
    body = {"goal": "Revenue per region", "target": "analytics.revenue"}
    first = in_a_request(lambda: agent.create(ANA, body, lambda urn: True))
    assert first["status"] == "ready"
    assert actors(engine) == [ANA]
    with pytest.raises(PipelineAgentError) as e:                     # 600 tokens used of 500: the next is refused
        in_a_request(lambda: agent.create(ANA, body, lambda urn: True))
    assert e.value.status == 429


class Workbooks:
    """A table sales.orders (region, amount) as the person reads it."""

    def columns_of(self, actor, connection, table):
        return ["region", "amount"]

    def run(self, actor, connection, element, cache=True):
        if element.get("group_by") and element["group_by"] == ["region"]:
            return {"columns": ["region", "__n"], "rows": [["eu", 5], ["us", 5]]}
        names = [a["name"] for a in element.get("aggregates") or []] + [c["name"] for c in element.get("columns") or []]
        value = {"lo0": "eu", "hi0": "us", "lo1": 1, "hi1": 100, "d0": 2, "d1": 10}
        return {"columns": names, "rows": [[value.get(n, 10) for n in names]]}


class Metrics:
    def list(self):
        return []


def test_the_autopilot_is_metered_to_the_person(engine):
    from cairn.sqlworkspace.autopilot import Autopilot, AutopilotError

    llm = MeteredProvider(Model({"metrics": []}), UsageMeter(engine, user_daily_tokens=500))
    a = Autopilot(engine, Workbooks(), Metrics(), lambda: None,
                  lambda connection, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{t},PROD)", llm)
    body = {"connection": "lakehouse", "tables": ["sales.orders"]}
    in_a_request(lambda: a.draft(ANA, body, lambda urn: True))
    assert actors(engine) == [ANA]
    with pytest.raises(AutopilotError) as e:
        in_a_request(lambda: a.draft(ANA, body, lambda urn: True))
    assert e.value.status == 429
