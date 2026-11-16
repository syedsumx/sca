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

    # Intents (L109): the invocation declares what the run is for; the app's own requests outside it are refused.
    def ask_for(intent):
        return httpx.post(f"{url}/v1/agent-apps/triage/invocations", json={"input": "Open?", "intent": intent},
                          headers={"X-Cairn-Actor": BOB}, timeout=120)
    assert ask_for({"purpose": "x", "actions": ["read"]}).status_code == 400            # not a valid intent
    no_model = ask_for({"purpose": "list open tickets", "actions": ["read"]})
    assert no_model.status_code == 502 and "model is outside" in no_model.text           # its model call was refused
    denied = [e for e in s.audit.export(category="agents") if e["action"] == "agent.intent_denied"][-1]
    assert denied["actor"] == BOB and denied["subject"] == app["agent_id"] and denied["details"]["category"] == "model"
    assert ask_for({"purpose": "list open tickets", "actions": ["read", "model"]}).status_code == 200
    s.intents.set_required(ADMIN, True)
    assert ask(BOB, "No intent?").status_code == 403                                     # required: refused up front
    s.intents.set_required(ADMIN, False)
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


def test_a_share_recipients_run_needs_the_sponsor_to_still_use_the_app(monkeypatch):
    """An agent attached to a share runs for the recipient with the attaching provider as sponsor: once the provider
    may no longer use the app (USE_AGENT taken away), the recipient's invocations are refused too."""
    import types

    from sqlalchemy import create_engine

    from cairn.ai import agent_apps as aa
    from cairn.common.db import metadata

    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[aa.agent_apps, aa.agent_app_sessions])
    with engine.begin() as c:
        c.execute(aa.agent_apps.insert().values(name="triage", agent_id="agt_x", owner=ANA, manifest="{}", bundle="",
                                                version=1, status="running", error=None, created_ms=0, updated_ms=0))
    grants = {(BOB, "USE_AGENT", "urn:li:cairnAgent:triage")}
    agents = types.SimpleNamespace(runnable=lambda _a: None, admit=lambda _a: None, revoke_token=lambda _t: None,
                                   issue_run_token=lambda *a, **k: "cat_run")
    runtime = types.SimpleNamespace(endpoint=lambda *_a: ("http://agent.invalid", "secret"))
    apps = aa.AgentApps(engine, agents, runtime, lambda a, p, u: (a, p, u) in grants, lambda _a: False)
    called = []

    def post(url, json=None, headers=None, timeout=None):
        called.append(json)
        return types.SimpleNamespace(status_code=200, json=lambda: {"output": "ok"})
    monkeypatch.setattr("httpx.post", post)
    recipient = "urn:li:corpuser:share:abc"
    body = {"input": "hi", "session_id": "s1"}
    out = apps.invoke_as(recipient, "triage", body, identity="share-recipient", sponsor=BOB, scopes=["model"])
    assert out["output"] == "ok" and called[-1]["identity"] == "share-recipient"
    grants.clear()                                   # Bob may no longer use the app
    with pytest.raises(aa.AgentAppError) as e:
        apps.invoke_as(recipient, "triage", body, identity="share-recipient", sponsor=BOB, scopes=["model"])
    assert e.value.status == 403 and len(called) == 1
    # The app's owner attaching it keeps working (the owner may always use their app).
    assert apps.invoke_as(recipient, "triage", body, identity="share-recipient", sponsor=ANA,
                          scopes=["model"])["output"] == "ok"


def test_a_bundle_that_unpacks_to_too_much_is_refused_before_anything_is_written(stack):
    """The deploy caps the compressed bundle (5 MB); gzip turns that into gigabytes on Cairn's disk unless what it
    unpacks to is capped too."""
    import base64
    import io
    import tarfile

    s, url, tmp = stack
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        code = b"def respond(ctx, messages):\n    return 'hi'\n"
        info = tarfile.TarInfo("agent.py")
        info.size = len(code)
        tar.addfile(info, io.BytesIO(code))
        info = tarfile.TarInfo("data.bin")
        info.size = 150 * 1024 * 1024                  # 150 MB of zeros: a few hundred KB compressed
        tar.addfile(info, io.BytesIO(bytes(info.size)))
    raw = buf.getvalue()
    assert len(raw) < 1024 * 1024
    r = httpx.post(f"{url}/v1/agent-apps", headers={"X-Cairn-Actor": ANA}, timeout=120,
                   json={"manifest": {"name": "bomb", "scopes": ["model"]}, "bundle": base64.b64encode(raw).decode()})
    assert r.status_code == 400 and "unpacks to more than" in r.text, r.text
    written = [p for p in (tmp / "apps").rglob("*") if p.is_file()] if (tmp / "apps").exists() else []
    assert written == []
    assert all(a["name"] != "bomb" for a in s.agents.list())
