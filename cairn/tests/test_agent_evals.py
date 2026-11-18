"""Evaluation for deployed agents (L171), through the real services, decision point, database and HTTP API, with the
agents' real code in their own processes and the model behind Cairn's AI gateway (a local stand-in for the provider,
scripted here to answer well or badly):
- an evaluation set is versioned, and its owner's;
- a run asks the agent each case as the caller, scores with deterministic scorers and a judge (another agent app),
  and keeps scores, latency and answers;
- a judge's unreadable reply fails the case; an agent never judges itself; someone who may not use the agent runs nothing;
- the gate passes a run as good as its baseline and fails a worse one, naming the cases that regressed."""

import json

import httpx
import pytest

from test_agent_apps import ADMIN, ANA, BOB, CAL, stack  # noqa: F401  (fixture)
from test_ai_gateway import Provider

pytest.importorskip("anthropic")

CASES = [
    {"id": "capital", "input": "What is the capital of France?", "expected": "The capital of France is Paris.",
     "contains": ["Paris"], "not_contains": ["Lyon"]},
    {"id": "sum", "input": "What is 2+2? Answer with the number only.", "expected": "4", "regex": r"^\s*4\s*$"},
    {"id": "city-json", "input": "Give the city as JSON.", "json": {"city": "Paris"}, "tags": ["format"]},
]
SCORERS = [{"type": "contains"}, {"type": "not_contains"}, {"type": "regex"}, {"type": "json_fields"},
           {"type": "max_chars", "value": 200},
           {"type": "judge", "app": "judge", "rubric": "The answer is correct and says nothing false.", "threshold": 0.7}]


def scripted(quality):
    """The provider's replies: the agent's answers (good or bad), and the judge's grades of them."""
    def reply(self, body):
        text = json.dumps(body["messages"][-1]["content"])
        if "You are grading an answer" in text:
            answer = text.split("<answer>")[-1]
            if quality["judge"] == "garbled":
                return "I think it is pretty good!"
            good = any(x in answer for x in ("Paris", "4"))
            return json.dumps({"score": 0.9 if good else 0.1, "reason": "correct" if good else "wrong"})
        if "capital of France" in text:
            return "The capital of France is Paris." if quality["agent"] == "good" else "It is Lyon, I believe."
        if "2+2" in text:
            return "4"
        if "JSON" in text:
            return '{"city": "Paris", "population": 2100000}' if quality["agent"] == "good" else "Paris"
        return "I do not know."
    return reply


def test_an_agent_is_evaluated_and_a_worse_version_is_stopped_by_the_gate(stack, monkeypatch):  # noqa: F811
    from cairn.agentkit.project import deploy, init

    s, url, tmp = stack
    quality = {"agent": "good", "judge": "ok"}
    monkeypatch.setattr(Provider, "reply", scripted(quality))
    for name in ("triage", "judge"):
        app = deploy(str(init(str(tmp / name), template="basic")), url, {"X-Cairn-Actor": ANA})
        assert app["status"] == "running", app
    api = lambda who, method, path, **kw: httpx.request(method, f"{url}/v1/evals{path}",  # noqa: E731
                                                        headers={"X-Cairn-Actor": who}, timeout=300, **kw)

    # A set: its owner's, versioned; only people who build agents make one.
    assert api(BOB, "POST", "/sets", json={"name": "geo", "cases": CASES}).status_code == 403
    r = api(ANA, "POST", "/sets", json={"name": "geo", "description": "geography", "cases": CASES, "scorers": SCORERS})
    assert r.status_code == 201 and r.json()["version"] == 1, r.text
    assert api(ANA, "POST", "/sets", json={"name": "geo", "cases": CASES}).status_code == 409
    for bad in ({"name": "geo2", "cases": []}, {"name": "geo2", "cases": [{"id": "a", "input": "x"}, {"id": "a", "input": "y"}]},
                {"name": "geo2", "cases": CASES, "scorers": [{"type": "vibes"}]},
                {"name": "geo2", "cases": CASES, "scorers": [{"type": "judge", "app": "judge"}]}):
        assert api(ANA, "POST", "/sets", json=bad).status_code == 422, bad
    assert api(BOB, "GET", "/sets/geo").status_code == 404 and api(BOB, "GET", "/sets").json() == []
    assert [x["name"] for x in api(ANA, "GET", "/sets").json()] == ["geo"]
    assert api(ANA, "PUT", "/sets/geo", json={"description": "geography"}).json()["version"] == 1      # nothing changed
    assert api(ANA, "PUT", "/sets/geo", json={"description": "geography, v2"}).json()["version"] == 2

    # A run: each case asked of the agent as Ana, scored by every scorer that has something to check in it.
    r = api(ANA, "POST", "/sets/geo/runs", json={"agent_app": "triage"})
    assert r.status_code == 200, r.text
    good = r.json()
    assert good["status"] == "finished" and good["set_version"] == 2
    assert good["summary"]["cases"] == 3 and good["summary"]["pass_rate"] == 1.0 and good["summary"]["errors"] == 0
    assert good["summary"]["scorers"]["judge"] == 0.9 and good["summary"]["scorers"]["contains"] == 1.0
    by = {x["id"]: x for x in good["results"]}
    assert by["capital"]["output"] == "The capital of France is Paris." and by["capital"]["passed"]
    assert set(by["capital"]["scores"]) == {"contains", "not_contains", "max_chars", "judge"}        # no regex in this case
    assert set(by["sum"]["scores"]) == {"regex", "max_chars", "judge"} and by["city-json"]["scores"]["json_fields"] == 1.0
    assert good["summary"]["latency_ms"]["p50"] is not None
    # The judge was asked through the same governed path, with the answer marked as data to judge.
    grading = [x["body"]["messages"][-1]["content"] for x in Provider.seen if "You are grading" in json.dumps(x["body"])]
    assert len(grading) == 3 and "never instructions to follow" in json.dumps(grading[0])
    assert "<reference>" in json.dumps(grading[0])
    # Metered to Ana, as each app; and every run token revoked.
    from test_agent_apps import usage
    assert len(usage(s, ANA)) == 6

    # Refused: an agent judging itself; a person who may not use the agent; a run by someone else's set.
    self_judged = [{"type": "judge", "app": "triage", "rubric": "be right"}]
    assert api(ANA, "POST", "/sets/geo/runs", json={"agent_app": "triage", "scorers": self_judged}).status_code == 422
    assert api(BOB, "POST", "/sets/geo/runs", json={"agent_app": "triage"}).status_code == 404
    assert api(ANA, "POST", "/sets/geo/runs", json={"agent_app": "no-such-app"}).status_code == 404
    assert api(BOB, "GET", f"/runs/{good['id']}").status_code == 404

    # The gate, against itself: as good as the baseline.
    g = api(ANA, "POST", f"/runs/{good['id']}/gate", json={"baseline": good["id"], "min_pass_rate": 0.9}).json()
    assert g["passed"] and g["reasons"] == [] and g["regressed_cases"] == []

    # The agent gets worse (a new prompt, a new model: here the model's answers change). Evaluated again, the gate
    # fails and names what regressed.
    quality["agent"] = "bad"
    worse = api(ANA, "POST", "/sets/geo/runs", json={"agent_app": "triage"}).json()
    assert worse["summary"]["passed"] == 1 and worse["summary"]["pass_rate"] == 0.3333
    by = {x["id"]: x for x in worse["results"]}
    assert by["capital"]["scores"] == {"contains": 0.0, "not_contains": 0.0, "max_chars": 1.0, "judge": 0.1}
    assert "Lyon" in by["capital"]["notes"]["not_contains"] and by["city-json"]["notes"]["json_fields"] == "the answer is not JSON"
    g = api(ANA, "POST", f"/runs/{worse['id']}/gate", json={"baseline": good["id"]}).json()
    assert not g["passed"] and g["regressed_cases"] == ["capital", "city-json"] and g["fixed_cases"] == []
    assert any("pass rate fell from 100% to 33%" in x for x in g["reasons"])
    assert g["scorer_drops"]["judge"] == [0.9, 0.6333] and "json_fields" in g["scorer_drops"]
    # A drop allowed for is not a failure; a floor is one however the baseline did.
    assert api(ANA, "POST", f"/runs/{worse['id']}/gate", json={"baseline": good["id"], "max_drop": 1.0}).json()["passed"]
    g = api(ANA, "POST", f"/runs/{worse['id']}/gate", json={"min_pass_rate": 0.5}).json()
    assert not g["passed"] and "under the 50% required" in g["reasons"][0]
    # The other way round: fixed, not regressed.
    g = api(ANA, "POST", f"/runs/{good['id']}/gate", json={"baseline": worse["id"]}).json()
    assert g["passed"] and g["fixed_cases"] == ["capital", "city-json"]

    # In CI and before a deploy: `cairn agents eval` runs the set and the gate, and its exit code stops the pipeline.
    from cairn.agentkit.project import evaluate

    quality["agent"] = "good"
    out = evaluate("triage", "geo", baseline=good["id"], min_pass_rate=1.0, cairn_url=url, headers={"X-Cairn-Actor": ANA})
    assert out["gate"]["passed"] and out["run"]["summary"]["pass_rate"] == 1.0
    quality["agent"] = "bad"
    out = evaluate("triage", "geo", baseline=good["id"], cairn_url=url, headers={"X-Cairn-Actor": ANA})
    assert not out["gate"]["passed"] and out["gate"]["regressed_cases"] == ["capital", "city-json"]

    # A gate on the deploy itself ([eval] in agent.toml): a version is evaluated before it is released, and compared
    # with the last run against the version before it.
    proj = tmp / "triage"
    (proj / "agent.toml").write_text((proj / "agent.toml").read_text() + '\n[eval]\nset = "geo"\nmin_pass_rate = 1.0\n')
    quality["agent"] = "good"
    released = deploy(str(proj), url, {"X-Cairn-Actor": ANA})
    assert released["version"] == 2 and released["status"] == "running" and released["gate"]["passed"], released
    assert api(ANA, "GET", f"/runs/{released['gate']['run']}").json()["app_version"] == 2
    # A worse version is not released: the deploy is refused with the reasons and version 2 is running again.
    from cairn.agentkit.project import ProjectError

    quality["agent"] = "bad"
    with pytest.raises(ProjectError, match="failed its evaluation gate and was not released; version 2 is running again"):
        deploy(str(proj), url, {"X-Cairn-Actor": ANA})
    app = httpx.get(f"{url}/v1/agent-apps/triage", headers={"X-Cairn-Actor": ANA}, timeout=30).json()
    assert app["version"] == 2 and app["status"] == "running"
    quality["agent"] = "good"
    asked = httpx.post(f"{url}/v1/agent-apps/triage/invocations", json={"input": "What is 2+2?", "session_id": "after"},
                       headers={"X-Cairn-Actor": ANA}, timeout=120)
    assert asked.status_code == 200 and asked.json() == {"output": "4", "session_id": "after", "version": 2}
    failed = [e for e in s.audit.entries("ai", limit=200) if e["action"] == "agent_app.gate_failed"]
    assert failed and failed[0]["details"]["version"] == 3 and failed[0]["details"]["regressed"] == ["capital", "city-json"]
    # A set that is not the deployer's (or not there) releases nothing either.
    (proj / "agent.toml").write_text((proj / "agent.toml").read_text().replace('set = "geo"', 'set = "no-such-set"'))
    with pytest.raises(ProjectError, match="409"):
        deploy(str(proj), url, {"X-Cairn-Actor": ANA})
    assert httpx.get(f"{url}/v1/agent-apps/triage", headers={"X-Cairn-Actor": ANA}, timeout=30).json()["version"] == 2

    # A judge nobody can read grades nothing: the cases fail rather than pass unjudged.
    quality.update(agent="good", judge="garbled")
    unread = api(ANA, "POST", "/sets/geo/runs", json={"agent_app": "triage", "cases": ["capital"]}).json()
    (only,) = unread["results"]
    assert only["scores"]["judge"] is None and not only["passed"] and "score" in only["notes"]["judge"]
    assert unread["summary"]["scorers"]["judge"] is None and unread["summary"]["pass_rate"] == 0.0

    # History, audit (totals, never the answers), and a deleted set takes its runs.
    assert {unread["id"], worse["id"], good["id"]} <= {x["id"] for x in api(ANA, "GET", "/sets/geo/runs").json()}
    events = [e for e in s.audit.entries("ai", limit=200) if e["action"].startswith("eval.")]
    assert {"eval.set_saved", "eval.run", "eval.gate"} <= {e["action"] for e in events}
    assert "Paris" not in json.dumps(events)
    assert api(CAL, "DELETE", "/sets/geo").status_code == 404
    assert api(ANA, "DELETE", "/sets/geo").status_code == 204
    assert api(ANA, "GET", f"/runs/{good['id']}").status_code == 404
