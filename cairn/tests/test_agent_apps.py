"""Code-first agents (the counterpart of Databricks' Agent Bricks CLI): ``cairn agents init`` makes a project, ``dev`` runs
it locally with the developer's Cairn token, ``deploy`` sends it to Cairn, which runs it as an agent app invoked as
the person asking. Through the real services, decision point, database and HTTP API, the agent's real code in its own
process, and the model behind Cairn's AI gateway (a local stand-in for the provider). Checked:
- the scaffold's own tests pass offline;
- dev mode: the chat page, sessions kept locally, model calls through Cairn as the developer;
- deploy needs BUILD_AGENTS; the app gets an identity of its own; a project needing a missing package is refused;
- invoking needs USE_AGENT; each run is the caller's (metered to them as the app), with a short-lived token revoked
  after the run; Cairn keeps each person's sessions apart and sends the history;
- a redeploy makes a new version; pausing agents stops the app; deleting it stops the runtime and its identity."""

import json
import os
import subprocess
import sys
import threading

import httpx
import pytest
import uvicorn

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.identity.agents import AgentIn
from cairn.services import build_services
from test_ai_gateway import SONNET, Provider
from test_trino_e2e import port, wait

ANA, BOB, CAL, ADMIN = ("urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cal", "urn:li:corpuser:admin")
pytest.importorskip("anthropic")


def sub(who):
    return {"attr": "subject.urn", "op": "eq", "value": who}


@pytest.fixture
def stack(settings, graph, monkeypatch, tmp_path):
    import http.server

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{up.server_address[1]}"
    p = port()
    for k, v in {"CAIRN_AI_GATEWAY_ANTHROPIC_URL": base, "CAIRN_AI_GATEWAY_ANTHROPIC_KEY": "sk-ant-upstream",
                 "CAIRN_AI_GATEWAY_MODELS": SONNET, "CAIRN_AGENT_APPS_RUNTIME": "process",
                 "CAIRN_AGENT_APPS_DIR": str(tmp_path / "apps"),
                 "CAIRN_AGENT_APPS_CAIRN_URL": f"http://127.0.0.1:{p}"}.items():
        monkeypatch.setenv(k, v)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    model = Target(resource_urns=[f"urn:li:aiModel:{SONNET}"])
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="builders", effect="allow", actions=["BUILD_AGENTS"], condition=sub(ANA)),
        Policy(id="models", effect="allow", actions=["USE_AI_GATEWAY"], target=model,
               condition={"attr": "subject.urn", "op": "in", "value": [ANA, BOB]}),
        Policy(id="bob-uses-triage", effect="allow", actions=["USE_AGENT"],
               target=Target(resource_urns=["urn:li:cairnAgent:triage"]), condition=sub(BOB))]))
    Provider.seen.clear()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield s, f"http://127.0.0.1:{p}", tmp_path
    for name in list(s.agent_apps.runtime.procs):
        s.agent_apps.runtime.stop(name)
    server.should_exit = True
    up.shutdown()


def usage(s, actor):
    from sqlalchemy import select

    from cairn.ai.usage import usage_table

    with s.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(usage_table).where(usage_table.c.actor == actor))]


def test_init_dev_deploy_invoke_delete(stack):
    from sqlalchemy import select

    from cairn.agentkit.project import ProjectError, deploy, init
    from cairn.identity.agents import agent_tokens

    s, url, tmp = stack
    proj = init(str(tmp / "triage"), template="basic")
    # The scaffold's own tests pass offline.
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=proj,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr

    # dev: on the developer's machine, every model call through Cairn as them (here a Cairn key of Ana's).
    dev_key = s.agents.create_key_agent(AgentIn(name="Ana dev", scopes=["model", "query_data"], owner=ANA), ADMIN)
    dp = port()
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("CAIRN_", "ANTHROPIC"))},
           "CAIRN_URL": url, "CAIRN_TOKEN": dev_key["key"], "CAIRN_USER": ANA}
    dev = subprocess.Popen([sys.executable, "-m", "cairn.agentkit.runtime", "--dir", str(proj), "--mode", "dev",
                            "--port", str(dp)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        wait(dp)
        assert "triage" in httpx.get(f"http://127.0.0.1:{dp}/").text
        for q in ("Hi", "And?"):
            out = httpx.post(f"http://127.0.0.1:{dp}/chat", json={"message": q, "session_id": "s1"}, timeout=60)
            assert out.status_code == 200 and out.json()["output"] == "Hello from the model", out.text
        assert [m["role"] for m in Provider.seen[-1]["body"]["messages"]] == ["user", "assistant", "user"]
        assert (proj / ".cairn" / "sessions.db").is_file()
        assert {r["task"] for r in usage(s, ANA)} == {f"gateway:{dev_key['id']}"}
    finally:
        dev.terminate()
        dev.wait(10)

    # deploy: BUILD_AGENTS needed.
    with pytest.raises(ProjectError, match="403"):
        deploy(str(proj), url, {"X-Cairn-Actor": BOB})
    app = deploy(str(proj), url, {"X-Cairn-Actor": ANA})
    assert app["status"] == "running" and app["version"] == 1 and app["scopes"] == ["model", "query_data"]
    assert s.agents.get(app["agent_id"])["kind"] == "app"
    assert not (tmp / "apps" / "triage" / "1" / "project" / ".cairn").exists()      # local state is not shipped

    # invoke: as Bob (USE_AGENT), his session, metered to him as the app; Cal may not.
    def ask(who, text, session="s1"):
        return httpx.post(f"{url}/v1/agent-apps/triage/invocations", json={"input": text, "session_id": session},
                          headers={"X-Cairn-Actor": who}, timeout=120)
    first = ask(BOB, "Which tickets are open?")
    assert first.status_code == 200 and first.json()["output"] == "Hello from the model", first.text
    assert ask(BOB, "And the oldest?").status_code == 200
    assert [m["role"] for m in Provider.seen[-1]["body"]["messages"]] == ["user", "assistant", "user"]
    assert ask(CAL, "Hi").status_code == 404
    bob_rows = usage(s, BOB)
    assert len(bob_rows) == 2 and {r["task"] for r in bob_rows} == {f"gateway:{app['agent_id']}"}
    sess = httpx.get(f"{url}/v1/agent-apps/triage/sessions/s1", headers={"X-Cairn-Actor": BOB}).json()
    assert [m["content"] for m in sess] == ["Which tickets are open?", "Hello from the model", "And the oldest?",
                                            "Hello from the model"]
    assert httpx.get(f"{url}/v1/agent-apps/triage/sessions/s1", headers={"X-Cairn-Actor": ANA}).json() == []
    with s.engine.connect() as c:                       # every run's token was revoked when the run ended
        runs = c.execute(select(agent_tokens).where(agent_tokens.c.client_id == app["agent_id"])).all()
    assert len(runs) == 2 and all(t.revoked for t in runs) and {t.actor for t in runs} == {BOB}

    # Redeploy: a new version; a project needing a package Cairn's environment lacks is refused, naming it.
    (proj / "agent.py").write_text((proj / "agent.py").read_text().replace("Answer briefly", "Answer in one line"))
    assert deploy(str(proj), url, {"X-Cairn-Actor": ANA})["version"] == 2
    assert ask(BOB, "Again?").status_code == 200
    bad = init(str(tmp / "needy"), template="basic")
    (bad / "requirements.txt").write_text("anthropic\nno-such-package-cairn-test\n")
    with pytest.raises(ProjectError, match="no-such-package-cairn-test"):
        deploy(str(bad), url, {"X-Cairn-Actor": ANA})
    assert httpx.get(f"{url}/v1/agent-apps/needy", headers={"X-Cairn-Actor": ANA}).status_code == 404

    # Pausing every agent stops the app too.
    s.agents.pause(True, ADMIN)
    assert ask(BOB, "Paused?").status_code == 403
    s.agents.pause(False, ADMIN)

    # Deleting: only its owner; the runtime stops and the identity goes.
    assert httpx.delete(f"{url}/v1/agent-apps/triage", headers={"X-Cairn-Actor": BOB}).status_code == 403
    proc = s.agent_apps.runtime.procs["triage"][0]
    assert httpx.delete(f"{url}/v1/agent-apps/triage", headers={"X-Cairn-Actor": ANA}).status_code == 204
    assert proc.poll() is not None and ask(BOB, "Hi").status_code == 404
    from cairn.identity.agents import AgentError
    with pytest.raises(AgentError):
        s.agents.get(app["agent_id"])
    audit = [e["action"] for e in s.audit.entries("ai", limit=200)]
    assert {"agent_app.deployed", "agent_app.invoked", "agent_app.deleted"} <= set(audit)
    assert "Which tickets are open?" not in json.dumps(s.audit.entries("ai", limit=200))
