"""Agent apps and custom agents share one name space, ``urn:li:cairnAgent:<name>`` (review findings on L100; the apps
themselves are tested end to end in tests/test_agent_apps.py)."""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import create_engine, insert

from cairn.ai.agent_apps import AgentAppError, AgentApps, agent_apps
from cairn.ai.custom_agents import AgentBuildError, CustomAgents, custom_agents
from cairn.common.db import metadata

ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"


class Hub:
    def create_app_agent(self, name, owner, scopes, description, actor):
        return {"id": f"agt_{name}"}

    def create_built_agent(self, name, owner, scopes, ai_system, description, actor):
        return {"id": f"agt_{name}"}

    def delete(self, agent_id, actor):
        pass


class Runtime:
    def start(self, name, version, data):
        pass


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/c.db")
    metadata.create_all(e)
    return e


def apps(engine):
    return AgentApps(engine, Hub(), Runtime(), lambda *a: True, lambda actor: False)


def body(name, scopes=("model",)):
    return {"manifest": {"name": name, "scopes": list(scopes)}, "bundle": base64.b64encode(b"x").decode()}


def test_an_app_cannot_take_a_custom_agents_name(engine):
    """Review finding: an agent app could be deployed under a custom agent's name; the marketplace then saw the app's
    deployer as the owner of ``urn:li:cairnAgent:<name>`` and could grant USE_AGENT, which runs Ana's agent."""
    with engine.begin() as c:
        c.execute(insert(custom_agents).values(id="a1", name="sales", owner=ANA, agent_id="agt_x", deployed=True,
                                               version=1, created_ms=0, updated_ms=0, description="", instructions="i",
                                               tools=["search"], scopes=["read"], max_steps=8))
    with pytest.raises(AgentAppError) as e:
        apps(engine).deploy(BOB, body("sales"))
    assert e.value.status == 409


def test_a_custom_agent_cannot_take_an_apps_name(engine):
    with engine.begin() as c:
        c.execute(insert(agent_apps).values(name="sales", agent_id="agt_s", owner=BOB, manifest="{}", bundle="",
                                            version=1, status="running", created_ms=0, updated_ms=0))
    built = CustomAgents(engine, Hub(), None, [{"name": "search"}], lambda *a: True, lambda a: [])
    with pytest.raises(AgentBuildError) as e:
        built.create(ANA, {"name": "sales", "tools": ["search"], "instructions": "Answer about sales."})
    assert e.value.status == 409


def test_scopes_that_are_not_names_are_a_400(engine):
    """Review finding: a scope that was not text (an object) failed with a 500."""
    with pytest.raises(AgentAppError) as e:
        apps(engine).deploy(ANA, body("triage", scopes=[{"a": 1}]))
    assert e.value.status == 400
