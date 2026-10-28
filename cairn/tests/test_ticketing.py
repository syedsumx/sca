"""Ticketing: Jira and ServiceNow requests that decide Cairn approvals, PagerDuty and Opsgenie
incidents that follow quality incidents, and signed inbound webhooks."""

import hashlib
import hmac
import json

import httpx
import pytest

from cairn.notify.ticketing import InboundError, System, TicketingService

SYSTEMS = {s.id: s for s in (
    System(id="jira", type="jira", url="https://jira.test", project="DATA", user="bot", token="t", inbound_secret="s3"),
    System(id="snow", type="servicenow", url="https://snow.test", user="bot", password="p", inbound_secret="s4"),
    System(id="pd", type="pagerduty", routing_key="rk", min_severity="HIGH"),
    System(id="og", type="opsgenie", api_key="k"),
)}


class FakeHub:
    def __init__(self):
        self.decided = []
        self.action_handlers = {"access_request": self.handle}

    def handle(self, action, object_id, actor, comment):
        if actor == "urn:li:corpuser:mallory":
            raise PermissionError("not an approver")
        self.decided.append((action, object_id, actor))
        return f"request {object_id} {action}d"


@pytest.fixture
def svc(engine):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url), json.loads(req.content or b"{}"), req.headers.get("authorization", "")))
        if req.url.path == "/rest/api/3/issue":
            return httpx.Response(201, json={"key": f"DATA-{len(calls)}"})
        if req.url.path.startswith("/api/now/table/"):
            return httpx.Response(201, json={"result": {"sys_id": "abc123", "number": "REQ0001"}})
        return httpx.Response(202, json={"status": "success"})

    hub = FakeHub()
    emails = {"carol@example.com": "urn:li:corpuser:carol", "mallory@example.com": "urn:li:corpuser:mallory"}
    s = TicketingService(engine, hub, emails.get, lambda: SYSTEMS, httpx.Client(transport=httpx.MockTransport(handler)))
    return s, hub, calls


def row(target, type_="access.requested", **payload):
    base = {"id": "ntf_1", "type": type_, "title": "Access request: Customer 360", "body": "ana asks for 30 days",
            "target": target, "entity_urn": "urn:li:dataProduct:c360",
            "payload": {"url": "https://cairn.test/cairn/requests?request=req_1",
                        "actions": [{"action": "approve", "kind": "access_request", "object_id": "req_1"}], **payload}}
    if type_.startswith("quality"):
        base["payload"] = {"monitor_id": "mon_9", "url": "https://cairn.test/x", **payload}
    return base


def test_jira_issue_once_per_request_then_comments(svc):
    s, hub, calls = svc
    key = s.sender.send(row("jira"))
    assert key == "DATA-1"
    method, url, body, auth = calls[0]
    assert url == "https://jira.test/rest/api/3/issue" and body["fields"]["project"] == {"key": "DATA"}
    assert "access_request:req_1" in json.dumps(body["fields"]["description"]) and auth.startswith("Basic ")
    later = row("jira", "access.granted")
    later["payload"] = {"request_id": "req_1"}
    assert s.sender.send(later) == "DATA-1" and calls[-1][1] == "https://jira.test/rest/api/3/issue/DATA-1/comment"
    assert s.overview()[0] == {"id": "jira", "type": "jira", "url": "https://jira.test", "inbound": True,
                               "tickets": 1, "open": 1}


def test_inbound_decisions_go_through_cairn_approval(svc):
    s, hub, _ = svc
    key = s.sender.send(row("jira"))
    body = {"issue": {"key": key, "fields": {"status": {"name": "Done"}}}, "user": {"emailAddress": "carol@example.com"}}
    raw = json.dumps(body).encode()
    with pytest.raises(InboundError) as e:
        s.inbound("jira", raw, body, "wrong", None)
    assert e.value.status == 401
    sig = "sha256=" + hmac.new(b"s3", raw, hashlib.sha256).hexdigest()
    out = s.inbound("jira", raw, body, None, sig)
    assert out["decision"] == "approve" and hub.decided == [("approve", "req_1", "urn:li:corpuser:carol")]
    assert s.inbound("jira", raw, body, "s3", None) == {"ignored": "already approved"}
    in_progress = {"issue": {"key": key, "fields": {"status": {"name": "In Progress"}}}}
    assert "ignored" in s.inbound("jira", b"{}", in_progress, "s3", None)


def test_inbound_refuses_unknown_people_and_non_approvers(svc):
    s, hub, _ = svc
    sys_id = s.sender.send(row("snow"))
    assert sys_id == "abc123"
    for email, status in (("nobody@example.com", 403), ("mallory@example.com", 403)):
        with pytest.raises(InboundError) as e:
            s.inbound("snow", b"{}", {"sys_id": sys_id, "approval": "approved", "approver_email": email}, "s4", None)
        assert e.value.status == status
    out = s.inbound("snow", b"{}", {"sys_id": sys_id, "approval": "rejected", "approver_email": "carol@example.com"},
                    "s4", None)
    assert out["decision"] == "reject" and hub.decided == [("reject", "req_1", "urn:li:corpuser:carol")]
    with pytest.raises(InboundError, match="no inbound_secret"):
        s.inbound("pd", b"{}", {}, "x", None)


def test_pagerduty_and_opsgenie_follow_the_incident(svc):
    s, _, calls = svc
    assert s.sender.send(row("pd", "quality.incident.raised", severity="MEDIUM")).startswith("skipped")
    assert s.sender.send(row("pd", "quality.incident.raised", severity="CRITICAL")) == "cairn-mon_9"
    trigger = calls[-1][2]
    assert trigger["event_action"] == "trigger" and trigger["payload"]["severity"] == "critical"
    assert trigger["dedup_key"] == "cairn-mon_9" and trigger["routing_key"] == "rk"
    s.sender.send(row("pd", "quality.incident.resolved", severity="CRITICAL"))
    assert calls[-1][2] == {"routing_key": "rk", "dedup_key": "cairn-mon_9", "event_action": "resolve"}
    s.sender.send(row("og", "quality.incident.raised", severity="HIGH"))
    assert calls[-1][1] == "https://api.opsgenie.com/v2/alerts" and calls[-1][2]["priority"] == "P2"
    assert calls[-1][3] == "GenieKey k"
    s.sender.send(row("og", "quality.incident.resolved"))
    assert calls[-1][1] == "https://api.opsgenie.com/v2/alerts/cairn-mon_9/close?identifierType=alias"
