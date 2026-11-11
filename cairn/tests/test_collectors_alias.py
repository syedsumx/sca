"""L149: remote ingestion agents are called collectors. `cairn collectors` is the command and `cairn agent` still
works (and says the new name); the admin API answers at /collectors as at /agents, with the same decisions."""

import pytest
from fastapi.testclient import TestClient

from cairn.agent.gateway import PoolSpec
from cairn.app import create_api_app
from cairn.cli import build_parser
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"
ALICE = {"X-Cairn-Actor": "urn:li:corpuser:alice"}
MALLORY = {"X-Cairn-Actor": "urn:li:corpuser:mallory"}


@pytest.mark.parametrize("name", ["collectors", "agent"])
def test_both_commands_run_the_collector(name, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr("cairn.agent.worker.main", lambda config: seen.append(config))
    a = build_parser().parse_args([name, "run", "--config", "/tmp/collector.yml"])
    a.fn(a)
    assert seen == ["/tmp/collector.yml"]
    err = capsys.readouterr().err
    if name == "agent":
        assert "`cairn agent` is now `cairn collectors`" in err
    else:
        assert err == ""


def test_the_help_names_collectors():
    commands = build_parser()._subparsers._group_actions[0]
    helps = {a.dest: a.help for a in commands._choices_actions}
    assert "formerly the remote agent" in helps["collectors"]
    assert "old name of `cairn collectors`" in helps["agent"]


@pytest.fixture
def api(settings, graph):
    services = build_services(settings, graph=graph, with_ai=False)
    services.pdp.set_bundle(PolicyBundle(policies=[Policy(
        id="alice-all", effect="allow", actions=["*"],
        condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:alice"})]))
    services.gateway.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"], max_concurrency=2), ADMIN)
    return TestClient(create_api_app(services))


def test_collectors_answer_as_agents_do(api):
    old = api.get("/agents/pools", headers=ALICE)
    new = api.get("/collectors/pools", headers=ALICE)
    assert old.status_code == new.status_code == 200
    assert new.json() == old.json()
    assert [p["name"] for p in new.json()] == ["finance"]
    # The same decisions: someone without the privilege is refused on both paths (fail closed), a stranger too.
    assert api.get("/collectors/pools", headers=MALLORY).status_code == api.get("/agents/pools", headers=MALLORY).status_code
    assert api.get("/collectors/pools", headers=MALLORY).status_code in (401, 403)
    assert api.get("/collectors/pools").status_code in (401, 403)
    # The alias is not documented twice: /agents stays the API's path in the schema.
    paths = api.get("/openapi.json").json()["paths"]
    assert "/agents/pools" in paths and "/collectors/pools" not in paths
