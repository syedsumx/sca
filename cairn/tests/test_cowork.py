"""CoWork (the counterpart of Snowflake's CoWork): a person's scheduled assistant tasks and an outbox that sends to Slack
or email only when they approve. Through the real services, policy decision point, database and HTTP API, with the
model scripted (shaped like the Anthropic provider), Mailpit as the SMTP server (docker) and a local HTTP server
standing in for Slack's Web API. Checked:
- a schedule is at most hourly, and reports go only to the Slack channels and email domains an operator listed;
- a due task runs once, as its owner (research with read-only tools), and its report is their artifact;
- nothing is sent until the owner approves, in Cairn or from the notification's button; others cannot decide it;
- approval checks again that the owner may export every dataset the report was made from, and redacts PII;
- rejected and expired items are not sent; a suspended owner's task fails and is paused after three failures."""

import http.server
import json
import os
import shutil
import subprocess
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services
from conftest import DS
from test_ai_workspace import Model, text, tool
from test_signup_e2e import MAIL_IMAGE, ready, ready_smtp
from test_trino_e2e import port, wait

ANA, BOB, CAL = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cal"

pytestmark = pytest.mark.skipif(not shutil.which("docker") or subprocess.run(
    ["docker", "image", "inspect", MAIL_IMAGE], capture_output=True).returncode != 0, reason="needs docker and Mailpit")


@pytest.fixture(scope="module")
def mail():
    name = f"cairn-cowork-mail-{os.getpid()}"
    smtp, api = port(), port()
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{smtp}:1025", "-p", f"127.0.0.1:{api}:8025",
                    MAIL_IMAGE], check=True, capture_output=True)
    wait(smtp)
    wait(api)
    ready(f"http://127.0.0.1:{api}/api/v1/messages", lambda r: r.status_code == 200)
    ready_smtp(smtp)
    yield {"smtp": smtp, "api": f"http://127.0.0.1:{api}"}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def slack():
    """Slack's Web API, as far as chat.postMessage: what was posted, with the bot token it came with."""
    posted = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            posted.append({"path": self.path, "auth": self.headers.get("Authorization"), **body})
            out = json.dumps({"ok": True, "ts": "1.2"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}/api", "posted": posted}
    srv.shutdown()


@pytest.fixture
def stack(settings, graph, mail, slack, monkeypatch):
    from cairn.notify.channels import EmailSender, SlackSender

    monkeypatch.setenv("CAIRN_COWORK_SLACK_CHANNELS", "C0SALES,#sales-weekly")
    monkeypatch.setenv("CAIRN_COWORK_EMAIL_DOMAINS", "corp.example")
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "amount"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="export-ana", effect="allow", actions=["EXPORT_DATASET"],
               condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    model = Model()
    s.assistant.llm = model
    s.notify.channels["slack"] = SlackSender("xoxb-test", api_url=slack["url"])
    s.notify.channels["email"] = EmailSender(f"smtp://127.0.0.1:{mail['smtp']}?starttls=false", "cairn@corp.example")
    httpx.delete(f"{mail['api']}/api/v1/messages")
    return s, model


def research(model, report):
    model.say([text('{"steps": ["Which tables hold orders?"]}')], [tool("search_assets", query="orders")],
              [text(f"Orders live in [{DS}].")], [text(report)])


def test_a_scheduled_task_runs_as_its_owner_and_sends_only_when_approved(stack, slack, mail):
    from cairn.ai.cowork import CoWorkError

    s, model = stack
    c = s.cowork
    with pytest.raises(CoWorkError, match="once an hour"):
        c.save_task(ANA, {"name": "x", "prompt": "summarise orders", "cron": "*/5 * * * *"})
    for send, msg in [({"slack": "#random"}, "not one reports may be sent to"),
                      ({"email": ["me@gmail.com"]}, "not in a domain"), ({"fax": "1"}, "send:")]:
        with pytest.raises(CoWorkError, match=msg):
            c.save_task(ANA, {"name": "x", "prompt": "summarise orders", "cron": "0 8 * * 1", "send": send})
    t = c.save_task(ANA, {"name": "Weekly orders", "prompt": "Summarise last week's orders and anything unusual",
                          "cron": "0 8 * * 1", "max_steps": 1, "send": {"slack": "C0SALES"}})
    assert t["enabled"] and t["next_run_ms"] > time.time() * 1000

    research(model, f"# Weekly orders\n\nOrders in [{DS}] look normal. Questions to ana.lee@corp.example.")
    # The runs happen on the schedule's clock (up to a week ahead), and so does the outbox item they make: on the
    # real clock the item looked days old to the second run_due, which expires pending items after 72 hours.
    due = t["next_run_ms"] + 1
    c._ms = lambda: due
    try:
        assert c.run_due(t["next_run_ms"] - 1) == 0                     # not due yet
        assert c.run_due(due) == 1
        assert c.run_due(due) == 0                                      # it runs once; the next run moved on
    finally:
        del c._ms
    t = c.task(ANA, t["id"])
    [run] = t["runs"]
    assert run["status"] == "done" and t["last_status"] == "done", run["error"]
    report = s.ai_workspace.artifact(ANA, run["artifact_id"])
    assert report["kind"] == "report" and DS in report["sources"]
    offered = {n for r in model.requests for n in (x["name"] for x in r.get("tools", []))}
    assert offered and not offered & {"remember", "save_artifact", "request_access", "suggest_tags"}   # read-only

    # Nothing was sent: it waits in Ana's outbox, and she was asked with buttons.
    assert slack["posted"] == []
    [item] = c.outbox(ANA, "pending")
    assert item["id"] == run["outbox_id"] and item["target"] == "C0SALES"
    from sqlalchemy import select

    from cairn.notify.hub import notifications
    assert [n["title"] for n in s.notify.inbox(ANA)] == ["Weekly orders: ready"]
    with s.engine.connect() as conn:
        payload = conn.execute(select(notifications.c.payload).where(notifications.c.recipient == ANA,
                                                                     notifications.c.channel == "in_app")).scalar()
    assert payload["cowork_task"] == t["id"] and {a["action"] for a in payload["actions"]} == {"approve", "reject"}
    # Bob can neither see nor decide it, even from a notification button.
    with pytest.raises(CoWorkError) as e:
        c.approve(BOB, item["id"])
    assert e.value.status == 404
    assert s.notify.execute_action("cowork_outbox", "approve", item["id"], BOB).startswith("Not sent")
    # Ana approves from the button: posted to the channel, PII redacted.
    assert s.notify.execute_action("cowork_outbox", "approve", item["id"], ANA) == "Sent."
    [post] = slack["posted"]
    assert post["path"] == "/api/chat.postMessage" and post["channel"] == "C0SALES" and post["auth"] == "Bearer xoxb-test"
    assert "Orders in" in post["text"] and "ana.lee@corp.example" not in post["text"]
    assert c.item(ANA, item["id"])["status"] == "sent"
    with pytest.raises(CoWorkError) as e:
        c.approve(ANA, item["id"])                                        # sent once
    assert e.value.status == 409

    # An artifact to email, approved in Cairn: delivered through the real SMTP server.
    sent = c.propose(ANA, {"artifact_id": run["artifact_id"], "send": {"email": ["sales-team@corp.example"]},
                           "subject": "Orders this week"})
    assert sent["status"] == "pending" and "Orders in" in sent["preview"]
    c.approve(ANA, sent["id"])
    deadline = time.time() + 20
    msgs = []
    while time.time() < deadline and not msgs:
        msgs = httpx.get(f"{mail['api']}/api/v1/messages").json().get("messages") or []
        time.sleep(0.3)
    assert msgs and msgs[0]["Subject"] == "Orders this week"
    assert [t["Address"] for t in msgs[0]["To"]] == ["sales-team@corp.example"]
    # Rejected and expired items are not sent.
    rej = c.propose(ANA, {"artifact_id": run["artifact_id"], "send": {"slack": "C0SALES"}})
    c.reject(ANA, rej["id"])
    old = c.propose(ANA, {"artifact_id": run["artifact_id"], "send": {"slack": "C0SALES"}})
    assert c.expire(int(time.time() * 1000) + 73 * 3600_000) == 1
    for oid in (rej["id"], old["id"]):
        with pytest.raises(CoWorkError) as e:
            c.approve(ANA, oid)
        assert e.value.status == 409
    assert len(slack["posted"]) == 1
    actions = [e["action"] for e in s.audit.entries("ai", limit=100)]
    assert {"cowork.task.saved", "cowork.task.ran", "cowork.outbound.sent", "cowork.outbound.rejected"} <= set(actions)
    assert "Orders in" not in json.dumps(s.audit.entries("ai", limit=100))      # the text is never audited


def test_sending_needs_export_rights_and_a_suspended_owner_is_paused(stack, slack):
    from cairn.ai.cowork import CoWorkError

    s, model = stack
    c = s.cowork
    t = c.save_task(BOB, {"name": "Bob's orders", "prompt": "Summarise the orders table", "cron": "0 * * * *",
                          "max_steps": 1, "send": {"email": ["bob@corp.example"]}})
    research(model, f"# Orders\n\nSee [{DS}].")
    run = c.run_now(BOB, t["id"])
    assert run["status"] == "done"
    with pytest.raises(CoWorkError) as e:
        c.approve(BOB, run["outbox_id"])                                  # Bob may read but not export the orders
    assert e.value.status == 403 and "EXPORT_DATASET" in str(e.value)
    assert c.item(BOB, run["outbox_id"])["status"] == "refused" and slack["posted"] == []

    # A suspended account's task fails rather than runs, and is paused after three failures in a row.
    s.graph.aspects[(CAL, "corpUserStatus")] = {"status": "SUSPENDED"}
    t = c.save_task(CAL, {"name": "Cal's", "prompt": "Summarise the orders table", "cron": "0 * * * *"})
    for _ in range(3):
        assert c.run_now(CAL, t["id"])["status"] == "failed"
    t = c.task(CAL, t["id"])
    assert not t["enabled"] and t["failures"] == 3 and "suspended" in t["runs"][0]["error"]
    assert len(model.requests) == 4                                       # only Bob's run asked the model


def test_the_api(stack):
    s, model = stack
    c = TestClient(create_api_app(s))
    ana, bob = {"X-Cairn-Actor": ANA}, {"X-Cairn-Actor": BOB}
    r = c.post("/ai/scheduled-tasks", json={"name": "Daily", "prompt": "Summarise orders daily", "cron": "15 7 * * *"},
               headers=ana)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert [x["id"] for x in c.get("/ai/scheduled-tasks", headers=ana).json()] == [tid]
    assert c.get(f"/ai/scheduled-tasks/{tid}", headers=bob).status_code == 404
    assert c.put(f"/ai/scheduled-tasks/{tid}", json={"enabled": False}, headers=ana).json()["enabled"] is False
    assert c.post("/ai/scheduled-tasks", json={"name": "x", "prompt": "too often please", "cron": "* * * * *"},
                  headers=ana).status_code == 400
    assert c.get("/ai/outbox", headers=ana).json() == []
    assert c.delete(f"/ai/scheduled-tasks/{tid}", headers=ana).status_code == 204
