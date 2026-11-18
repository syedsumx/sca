"""The prompt registry (L171), through the real services, decision point, database and HTTP API, with a deployed agent
(its real code in its own process) fetching its instructions from the registry on every run, and the model behind
Cairn's AI gateway (the suite's local stand-in, which records what it was sent):
- a prompt has versions that never change, and aliases that name one;
- an agent's run uses the production alias, so moving the alias releases a version and moving it back rolls it back,
  with no redeploy;
- a prompt is read by its owner and those allowed USE_PROMPT (an agent fetches as the person it runs for), and
  changed by its owner alone, never by an agent's credential."""

import httpx
import pytest

from test_agent_apps import ADMIN, ANA, BOB, CAL, stack, sub  # noqa: F401  (fixture)
from test_ai_gateway import Provider

pytest.importorskip("anthropic")

AGENT = '''
MODEL = "claude-sonnet-5-5"


def respond(ctx, messages):
    system = ctx.prompt("support", team="billing")        # the version in production, as the person asking
    msg = ctx.anthropic().messages.create(model=MODEL, max_tokens=256, system=system, messages=[dict(m) for m in messages])
    return "".join(b.text for b in msg.content if b.type == "text")
'''


def test_prompts_are_versioned_released_by_alias_and_fetched_by_agents(stack):  # noqa: F811
    from cairn.agentkit.project import deploy, init
    from cairn.governance.abac import Policy, PolicyBundle, Target

    s, url, tmp = stack
    api = lambda who, method, path="", **kw: httpx.request(method, f"{url}/v1/prompts{path}",  # noqa: E731
                                                           headers={"X-Cairn-Actor": who}, timeout=60, **kw)

    # Made by someone who builds agents; version 1.
    v1 = "You are the {{team}} team's assistant. Answer in one sentence."
    assert api(BOB, "POST", json={"name": "support", "template": v1}).status_code == 403
    r = api(ANA, "POST", json={"name": "support", "template": v1, "description": "support agent"})
    assert r.status_code == 201 and r.json()["version"] == 1 and r.json()["variables"] == ["team"], r.text
    assert api(ANA, "POST", json={"name": "support", "template": v1}).status_code == 409
    for bad in ({"name": "Bad Name", "template": "x"}, {"name": "empty", "template": "  "}):
        assert api(ANA, "POST", json=bad).status_code == 422

    # Declared again with the same text: nothing. With another: version 2, and version 1 is still what it was.
    assert api(ANA, "PUT", "/support", json={"template": v1}).json()["version"] == 1
    v2 = "You are the {{team}} team's assistant. Answer in one sentence, and always name the invoice."
    r = api(ANA, "PUT", "/support", json={"template": v2, "note": "name the invoice"})
    assert r.json()["version"] == 2 and r.json()["latest"] == 2
    assert api(ANA, "GET", "/support", params={"version": 1}).json()["template"] == v1
    assert [(v["version"], v["note"]) for v in api(ANA, "GET", "/support/versions").json()] == [(1, ""), (2, "name the invoice")]
    assert api(ANA, "GET", "/support", params={"version": 9}).status_code == 404

    # Rendering: every variable given, a value put in as it is, nothing extra.
    out = api(ANA, "POST", "/support/render", json={"version": 1, "values": {"team": "{{team}} & co"}}).json()
    assert out == {"name": "support", "version": 1, "text": "You are the {{team}} & co team's assistant. Answer in one sentence."}
    assert api(ANA, "POST", "/support/render", json={"values": {}}).status_code == 422
    assert api(ANA, "POST", "/support/render", json={"values": {"team": "x", "other": "y"}}).status_code == 422

    # Others: not theirs to see, until USE_PROMPT; never theirs to change.
    assert api(BOB, "GET", "/support").status_code == 404 and api(BOB, "GET").json() == []
    assert api(BOB, "PUT", "/support", json={"template": "mine now"}).status_code == 404

    # An agent whose instructions are the prompt, deployed once.
    proj = init(str(tmp / "triage"), template="basic")
    (proj / "agent.py").write_text(AGENT)
    assert deploy(str(proj), url, {"X-Cairn-Actor": ANA})["status"] == "running"

    def ask(who):
        Provider.seen.clear()
        r = httpx.post(f"{url}/v1/agent-apps/triage/invocations", json={"input": "Where is my invoice?", "session_id": "s"},
                       headers={"X-Cairn-Actor": who}, timeout=120)
        return r, (Provider.seen[-1]["body"].get("system") if Provider.seen else None)

    # No alias yet: the latest version. Then production is pinned to 1, released to 2, and rolled back, with no redeploy.
    r, system = ask(ANA)
    assert r.status_code == 200 and "always name the invoice" in str(system) and "billing team" in str(system)
    assert api(ANA, "PUT", "/support/aliases/production", json={"version": 1}).json()["version"] == 1
    assert "always name the invoice" not in str(ask(ANA)[1])
    assert api(ANA, "PUT", "/support/aliases/production", json={"version": 2}).status_code == 200
    assert "always name the invoice" in str(ask(ANA)[1])
    assert api(ANA, "PUT", "/support/aliases/production", json={"version": 1}).status_code == 200      # the rollback
    assert "always name the invoice" not in str(ask(ANA)[1])
    assert api(ANA, "GET", "/support").json()["aliases"] == [{"alias": "production", "version": 1}]
    assert api(ANA, "PUT", "/support/aliases/production", json={"version": 7}).status_code == 422
    assert api(ANA, "PUT", "/support/aliases/Bad!", json={"version": 1}).status_code == 422

    # The agent fetches as the person it runs for: Bob may use the agent, but not read its prompt, so his run fails
    # before any model call. Allowed USE_PROMPT, it works, and he still changes nothing.
    r, system = ask(BOB)
    assert r.status_code >= 400 and system is None
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="bob-reads-support", effect="allow", actions=["USE_PROMPT"],
        target=Target(resource_urns=["urn:li:cairnPrompt:support"]), condition=sub(BOB))]))
    r, system = ask(BOB)
    assert r.status_code == 200 and "billing team" in str(system)
    assert api(BOB, "GET", "/support").json()["version"] == 1
    assert api(BOB, "PUT", "/support", json={"template": "mine now"}).status_code == 403
    assert api(BOB, "PUT", "/support/aliases/production", json={"version": 2}).status_code == 403
    assert api(BOB, "DELETE", "/support").status_code == 403

    # Audited with versions and names, never the text; deleted with its versions and aliases.
    events = [e for e in s.audit.entries("ai", limit=200) if e["action"].startswith("prompt.")]
    assert [e["action"] for e in reversed(events)][:3] == ["prompt.created", "prompt.version_added", "prompt.alias_set"]
    moved = [e["details"] for e in events if e["action"] == "prompt.alias_set"]
    assert {"alias": "production", "version": 1, "was": 2} in moved
    assert "invoice" not in str(events)
    assert api(ANA, "DELETE", "/support/aliases/staging").status_code == 404
    assert api(ANA, "DELETE", "/support").status_code == 204
    assert api(ANA, "GET", "/support").status_code == 404
    assert api(ANA, "POST", json={"name": "support", "template": "new"}).json()["version"] == 1


def test_an_agents_credential_reads_prompts_with_the_model_scope_and_changes_none():
    from cairn.identity.agents import required_scope

    assert required_scope("POST", "/v1/prompts/{name}/render", set()) == "model"
    assert required_scope("GET", "/v1/prompts/{name}", set()) == "model"
