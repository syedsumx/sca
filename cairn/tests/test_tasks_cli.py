"""``cairn tasks`` (L108): CoWork scheduled tasks and their outbox from the command line. The real CLI runs in a
subprocess against a real Cairn API server (uvicorn) with the real services, decision point and database. The model
is scripted (shaped like the Anthropic provider), a local HTTP server stands in for Slack's Web API, and Cairn Core's
``me`` lookup (the external system that vouches for personal access tokens) is replaced by a table of two tokens.
Checked:
- the URL and token come from the environment only (CAIRN_TOKEN or CAIRN_TOKEN_FILE), never an argument;
- create, list, show, run now (the report is the owner's artifact), output, pause and resume, delete;
- the outbox: list, approve (sent to Slack), reject (not sent); someone else cannot decide your items;
- agent credentials are refused; the API's refusals become exit status 1 with the reason."""

import http.server
import json
import os
import subprocess
import sys
import threading

import pytest
import uvicorn

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.identity.agents import AgentIn
from cairn.services import build_services
from conftest import DS
from test_ai_workspace import Model, text, tool
from test_trino_e2e import port, wait

ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
SRC = os.path.join(os.path.dirname(__file__), "..", "src")
TOKENS = {"pat-ana-0123456789": ANA, "pat-bob-0123456789": BOB}


@pytest.fixture
def stack(settings, graph, monkeypatch, tmp_path):
    from cairn.notify.channels import SlackSender

    posted = []

    class Slack(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posted.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps({"ok": True, "ts": "1.2"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    slack = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slack)
    threading.Thread(target=slack.serve_forever, daemon=True).start()
    monkeypatch.setenv("CAIRN_COWORK_SLACK_CHANNELS", "#sales-weekly")
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "amount"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "EXPORT_DATASET"],
               condition={"attr": "subject.urn", "op": "exists"})]))
    model = Model()
    s.assistant.llm = model
    s.notify.channels["slack"] = SlackSender("xoxb-test", api_url=f"http://127.0.0.1:{slack.server_address[1]}/api")
    real = s.identity._me_lookup
    s.identity._me_lookup = lambda url, h, c: TOKENS.get((h.get("Authorization") or "")[7:]) or real(url, h, c)
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield s, model, f"http://127.0.0.1:{p}", posted, tmp_path
    server.should_exit = True
    slack.shutdown()


def cairn(url, token, *args, token_file=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CAIRN_", "ANTHROPIC"))}
    env["PYTHONPATH"] = os.path.abspath(SRC)
    if url:
        env["CAIRN_URL"] = url
    if token:
        env["CAIRN_TOKEN"] = token
    if token_file:
        env["CAIRN_TOKEN_FILE"] = str(token_file)
    r = subprocess.run([sys.executable, "-m", "cairn.cli", "tasks", *args], env=env, capture_output=True, text=True,
                       timeout=300)
    return r.returncode, r.stdout, r.stderr


def research(model, report):
    model.say([text('{"steps": ["Which tables hold orders?"]}')], [tool("search_assets", query="orders")],
              [text(f"Orders live in [{DS}].")], [text(report)])


def test_tasks_cli_against_a_real_server(stack):
    s, model, url, posted, tmp = stack
    ana, bob = "pat-ana-0123456789", "pat-bob-0123456789"

    # Configuration comes from the environment; a token is never an argument.
    code, _, err = cairn(url, None, "list")
    assert code == 2 and "CAIRN_TOKEN" in err
    code, _, err = cairn(None, ana, "list")
    assert code == 2 and "CAIRN_URL" in err
    code, _, err = cairn(url, ana, "list", "--token", ana)
    assert code == 2 and "unrecognized arguments" in err
    assert cairn(url, ana, "list") == (0, "no scheduled tasks\n", "")

    # Create (the API's own checks apply), list and show.
    code, _, err = cairn(url, ana, "create", "--name", "Weekly", "--prompt", "Summarise orders", "--cron", "*/5 * * * *")
    assert code == 1 and "once an hour" in err
    code, _, err = cairn(url, ana, "create", "--name", "Weekly", "--prompt", "Summarise orders", "--cron", "0 7 * * 1",
                         "--slack", "#random")
    assert code == 1 and "403" in err
    code, out, err = cairn(url, ana, "--json", "create", "--name", "Weekly orders", "--prompt",
                           "Summarise last week's orders", "--cron", "0 7 * * 1", "--slack", "#sales-weekly")
    assert code == 0, err
    task = json.loads(out)
    tid = task["id"]
    assert task["owner"] == ANA and task["send"] == {"slack": "#sales-weekly"} and task["enabled"] is True
    code, out, _ = cairn(url, ana, "list")
    assert code == 0 and tid in out and "Weekly orders" in out and "slack: #sales-weekly" in out
    assert cairn(url, bob, "list")[1] == "no scheduled tasks\n"            # a person's own
    code, _, err = cairn(url, bob, "show", tid)
    assert code == 1 and "404" in err

    # Run now: the research runs as Ana, the report is her artifact and waits in her outbox.
    research(model, "Orders grew 4% week on week; ana@corp.example asked for this.")
    code, out, err = cairn(url, ana, "run", tid)
    assert code == 0 and "done: report" in out and "waiting for approval" in out, out + err
    code, out, _ = cairn(url, ana, "--json", "show", tid)
    shown = json.loads(out)
    assert shown["last_status"] == "done" and len(shown["runs"]) == 1
    run = shown["runs"][0]
    assert s.ai_workspace.artifact(ANA, run["artifact_id"])["owner"] == ANA
    code, out, _ = cairn(url, ana, "output", tid)
    assert code == 0 and "Orders grew 4%" in out and run["id"] in out
    code, out, _ = cairn(url, ana, "show", tid)
    assert code == 0 and run["id"] in out and "done" in out

    # The outbox: listed, someone else cannot decide it, approval sends it to Slack (PII redacted).
    code, out, _ = cairn(url, ana, "--json", "outbox", "--status", "pending")
    items = json.loads(out)
    assert [i["id"] for i in items] == [run["outbox_id"]] and items[0]["target"] == "#sales-weekly"
    code, _, err = cairn(url, bob, "approve", run["outbox_id"])
    assert code == 1 and "404" in err and posted == []
    assert cairn(url, ana, "approve", run["outbox_id"]) == (0, f"{run['outbox_id']} sent\n", "")
    assert len(posted) == 1 and posted[0]["channel"] == "#sales-weekly"
    assert "Orders grew 4%" in json.dumps(posted[0]) and "ana@corp.example" not in json.dumps(posted[0])

    # A second run, rejected: nothing more is sent. The token can come from a file too.
    research(model, "Orders were flat.")
    (tmp / "token").write_text(ana + "\n")
    code, out, _ = cairn(url, None, "--json", "run", tid, token_file=tmp / "token")
    second = json.loads(out)
    assert code == 0 and second["status"] == "done"
    assert cairn(url, ana, "reject", second["outbox_id"]) == (0, f"{second['outbox_id']} rejected\n", "")
    assert len(posted) == 1
    code, out, _ = cairn(url, ana, "output", tid, "--run", second["id"])
    assert "Orders were flat." in out

    # Pause and resume; delete.
    assert cairn(url, ana, "pause", tid)[1] == f"Weekly orders ({tid}) paused\n"
    assert s.cowork.task(ANA, tid)["enabled"] is False
    assert cairn(url, ana, "resume", tid)[1] == f"Weekly orders ({tid}) resumed\n"
    assert s.cowork.task(ANA, tid)["enabled"] is True

    # Agent credentials are refused: scheduled tasks are a person's own.
    key = s.agents.create_key_agent(AgentIn(name="Bot", scopes=["read"], owner=ANA), ANA)["key"]
    code, _, err = cairn(url, key, "list")
    assert code == 1 and "403" in err

    assert cairn(url, ana, "delete", tid) == (0, f"deleted {tid}\n", "")
    assert cairn(url, ana, "list")[1] == "no scheduled tasks\n"
    actions = [e["action"] for e in s.audit.export(category="ai") if e["actor"] == ANA]
    for a in ("cowork.task.saved", "cowork.task.ran", "cowork.outbound.sent", "cowork.outbound.rejected",
              "cowork.task.deleted"):
        assert a in actions, a
