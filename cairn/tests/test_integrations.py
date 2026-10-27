"""Slack, Teams and webhook delivery: payloads Slack and Teams accept, rate limits, and targets
the notifier refuses to call."""

import json

import httpx
import pytest

from cairn.notify.channels import (SlackSender, TeamsSender, WebhookSender, slack_blocks, teams_card,
                                   teams_webhook_allowed, validate_adaptive_card, validate_blocks,
                                   webhook_target_problem)
from cairn.notify.hub import Action, Event, NotificationHub, NotifySettings, Route
from cairn.notify.slack import response_url_allowed

CAROL = "urn:li:corpuser:carol"


def row(title="Access requested", body="Bob asks for Customer 360", actions=None, url="https://cairn.example.com/x"):
    return {"id": "ntf_1", "type": "access.requested", "title": title, "body": body, "recipient": CAROL,
            "target": None, "entity_urn": None,
            "payload": {"url": url, "actions": actions if actions is not None else [
                {"action": "approve", "kind": "access_request", "object_id": "ar_1", "label": "Approve", "style": "primary"},
                {"action": "reject", "kind": "access_request", "object_id": "ar_1", "label": "Reject", "style": "danger"}]}}


# --------------------------------------------------------------------------- Slack payloads
def test_slack_blocks_are_valid_block_kit():
    blocks = slack_blocks(row())
    assert validate_blocks(blocks) == []
    buttons = blocks[1]["elements"]
    assert [b["action_id"] for b in buttons] == ["cairn:access_request:approve", "cairn:access_request:reject",
                                                 "cairn:open"]
    assert buttons[1]["confirm"]["confirm"]["text"] == "Reject"


def test_oversized_and_hostile_text_still_renders_valid_blocks():
    long_body = "x" * 10_000 + " <!channel> <@U123> & done"
    blocks = slack_blocks(row(title="T" * 1000, body=long_body,
                              actions=[{"action": "approve", "kind": "k", "object_id": "o" * 5000, "label": "L" * 200}],
                              url="https://cairn.example.com/" + "p" * 4000))
    assert validate_blocks(blocks) == []
    text = blocks[0]["text"]["text"]
    assert len(text) <= 3000 and text.endswith("…") and "<!channel>" not in text
    assert all(e.get("action_id") != "cairn:open" for e in blocks[1]["elements"])   # url too long for a button
    # Mentions in user-written text are escaped, so a title cannot ping a channel.
    assert "&lt;!channel&gt;" in slack_blocks(row(body="hi <!channel>"))[0]["text"]["text"]


def test_validator_catches_what_slack_rejects():
    bad = [{"type": "section", "text": {"type": "mrkdwn", "text": ""}},
           {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "a"},
                                             "action_id": "x"}] * 2},
           {"type": "image"}]
    problems = validate_blocks(bad)
    assert any("section text" in p for p in problems) and any("repeated" in p for p in problems)
    assert any("unexpected type" in p for p in problems)


class FakeSlack:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, verb, url, **kw):
        self.calls.append((verb, url, kw))
        status, body, headers = self.responses.pop(0)
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=content, headers=headers)


def test_slack_sender_honours_rate_limits_and_reports_errors(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    http = FakeSlack([(429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "2"}),
                      (200, {"ok": True, "channel": "D1", "ts": "1.2"}, {})])
    sender = SlackSender("xoxb-t", http=http, api_url="https://slack.internal/api")
    r = row()
    r["target"] = "D1"
    assert sender.send(r) == "D1:1.2" and slept == [2.0]
    verb, url, kw = http.calls[0]
    assert url == "https://slack.internal/api/chat.postMessage" and kw["headers"]["Authorization"] == "Bearer xoxb-t"
    assert validate_blocks(kw["json"]["blocks"]) == []
    # A long rate limit is left to the next delivery round instead of blocking the notifier.
    with pytest.raises(RuntimeError, match="rate limited for 120s"):
        SlackSender("t", http=FakeSlack([(429, {}, {"Retry-After": "120"})])).send(r)
    with pytest.raises(RuntimeError, match="HTTP 502 without a JSON body"):
        SlackSender("t", http=FakeSlack([(502, b"<html>bad gateway</html>", {})])).send(r)
    with pytest.raises(RuntimeError, match="channel_not_found"):
        SlackSender("t", http=FakeSlack([(200, {"ok": False, "error": "channel_not_found"}, {})])).send(r)


def test_slack_api_url_from_the_environment(monkeypatch):
    monkeypatch.setenv("CAIRN_SLACK_API_URL", "https://slack-gov.com/api/")
    assert SlackSender("t").api == "https://slack-gov.com/api"


def test_slack_replies_only_go_to_slack(monkeypatch):
    assert response_url_allowed("https://hooks.slack.com/actions/T/1/x")
    assert not response_url_allowed("https://hooks.slack.com.evil.example/x")
    assert not response_url_allowed("http://hooks.slack.com/x")
    assert not response_url_allowed("http://cairn-e2e-sink:8099/slack/response/1")
    monkeypatch.setenv("CAIRN_SLACK_RESPONSE_HOSTS", "http://cairn-e2e-sink:8099")
    assert response_url_allowed("http://cairn-e2e-sink:8099/slack/response/1")


# --------------------------------------------------------------------------- Teams
class _Hub:
    def action_link(self, action, recipient):
        return f"https://cairn.example.com/cairn/api/notify/actions/signed-{action.action}"


def test_teams_card_is_a_valid_adaptive_card():
    card = teams_card(row(), _Hub())
    assert validate_adaptive_card(card) == []
    actions = card["attachments"][0]["content"]["actions"]
    assert [a["title"] for a in actions] == ["Approve", "Reject", "Open in Cairn"]
    assert actions[0]["url"].endswith("signed-approve")
    long = teams_card(row(title="T" * 900, body="b" * 9000), _Hub())
    body = long["attachments"][0]["content"]["body"]
    assert len(body[0]["text"]) <= 300 and len(body[1]["text"]) <= 4000
    assert validate_adaptive_card({"type": "message", "attachments": []})


def test_teams_webhooks_must_be_microsoft_hosts(monkeypatch):
    ok = ["https://prod-12.westeurope.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=x",
          "https://acme.webhook.office.com/webhookb2/abc", "https://default123.environment.api.powerplatform.com/x"]
    bad = ["http://acme.webhook.office.com/x", "https://169.254.169.254/latest", "https://evil.example/logic.azure.com",
           "https://logic.azure.com.evil.example/x", "https://internal-api:8443/admin", None, ""]
    assert all(teams_webhook_allowed(u) for u in ok)
    assert not any(teams_webhook_allowed(u) for u in bad)
    monkeypatch.setenv("CAIRN_TEAMS_WEBHOOK_HOSTS", ".logic.azure.com,https://teams-proxy.corp:8443")
    assert teams_webhook_allowed("https://teams-proxy.corp:8443/hook")
    assert not teams_webhook_allowed("https://acme.webhook.office.com/webhookb2/abc")
    with pytest.raises(RuntimeError, match="Microsoft host"):
        TeamsSender().send({**row(), "target": "https://internal-api:8443/admin"}, _Hub())


def test_users_cannot_save_a_teams_webhook_pointing_elsewhere(engine):
    hub = NotificationHub(engine, "https://cairn.example.com", "s")
    s = NotifySettings()
    s.channels["teams"].enabled = True
    s.channels["teams"].webhook_url = "https://169.254.169.254/latest/meta-data"
    with pytest.raises(ValueError, match="Microsoft webhook host"):
        hub.put_settings(CAROL, s)
    s.channels["teams"].webhook_url = "https://acme.webhook.office.com/webhookb2/abc"
    assert hub.put_settings(CAROL, s).channels["teams"].webhook_url.endswith("/abc")
    with pytest.raises(ValueError, match="route r1"):
        hub.put_routes([Route(id="r1", channel="teams", target="http://localhost:9000/x")])


# --------------------------------------------------------------------------- webhooks
def test_webhooks_never_reach_loopback_or_metadata(monkeypatch):
    resolve = {"metadata.example": ["169.254.169.254"], "local.example": ["127.0.0.1"],
               "v6.example": ["::1"], "corp.example": ["10.1.2.3"], "public.example": ["93.184.216.34"]}.get
    assert "169.254.169.254" in webhook_target_problem("http://metadata.example/latest", resolve)
    assert webhook_target_problem("http://local.example/x", resolve)
    assert webhook_target_problem("http://v6.example/x", resolve)
    assert webhook_target_problem("ftp://public.example/x", resolve)
    assert webhook_target_problem("http://public.example/x", resolve) is None
    assert webhook_target_problem("http://corp.example/x", resolve) is None             # on-premises receivers
    monkeypatch.setenv("CAIRN_WEBHOOK_ALLOW_PRIVATE", "false")
    assert "private address" in webhook_target_problem("http://corp.example/x", resolve)
    assert "cannot resolve" in webhook_target_problem("http://nxdomain.invalid/x",
                                                      lambda h: (_ for _ in ()).throw(OSError("no such host")))
    with pytest.raises(RuntimeError, match="may not reach"):
        WebhookSender(resolve=resolve).send({**row(), "target": "http://metadata.example/x"}, None)


def test_refused_targets_fail_the_delivery_not_the_notifier(engine):
    hub = NotificationHub(engine, "https://cairn.example.com", "s")
    hub.channels["webhook"] = WebhookSender(resolve=lambda h: ["127.0.0.1"])
    hub.put_routes([Route(id="wh", channel="webhook", target="http://cairn.internal/hook")])
    hub.publish(Event(type="context.drift", title="t", body="b", actor="urn:li:corpuser:x",
                      actions=[Action("ack", "finding", "f1", "Ack")]))
    stats = hub.dispatch_pending(max_attempts=1)
    assert stats == {"sent": 0, "failed": 1, "skipped": 0}


# --------------------------------------------------------------------------- Slack user → Cairn user
def test_slack_users_are_matched_by_profile_email_not_username():
    from cairn.common.core import InMemoryGraph
    from cairn.services import actor_for_email

    graph = InMemoryGraph()
    people = [("urn:li:corpuser:carol", "Carol@Example.com"), ("urn:li:corpuser:dup1", "shared@example.com"),
              ("urn:li:corpuser:dup2", "shared@example.com")]

    def handler(query, variables):
        q = variables["i"]["query"].strip('"').lower()
        return {"searchAcrossEntities": {"searchResults": [
            {"entity": {"urn": u, "properties": {"email": e}}} for u, e in people if q in e.lower() or q in u]}}
    graph.graphql_handler = handler
    assert actor_for_email(graph, "carol@example.com") == "urn:li:corpuser:carol"     # username is not the email
    assert actor_for_email(graph, "shared@example.com") is None                       # ambiguous: refuse
    assert actor_for_email(graph, "nobody@example.com") is None
    graph.aspects[("urn:li:corpuser:ana@example.com", "corpUserKey")] = {"username": "ana@example.com"}
    assert actor_for_email(graph, "ana@example.com") == "urn:li:corpuser:ana@example.com"
    assert actor_for_email(None, "carol@example.com") is None
