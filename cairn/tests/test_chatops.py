"""The assistant in Slack and Teams: signed requests, private answers unless a channel identity is
set, one conversation per thread, retries ignored, Teams' five-second window."""

import base64
import hashlib
import hmac
import json
import time

import pytest

from cairn.notify.chatops import (ChatAnswerer, SlackEvents, TeamsAuthError, TeamsOutgoing, slack_mrkdwn,
                                  verify_teams_hmac)
from cairn.notify.slack import SlackAuthError

SECRET = "slack-signing-secret"
ANA, CHANNEL = "urn:li:corpuser:ana", "urn:li:corpuser:slack-channel"
DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.orders,PROD)"


class Sender:
    def __init__(self):
        self.calls = []

    def _call(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True}


class Chat:
    def __init__(self, delay=0.0):
        self.calls, self.delay = [], delay

    def __call__(self, actor, question, conversation):
        time.sleep(self.delay)
        self.calls.append((actor, question, conversation))
        return {"conversation_id": "conv_1", "reply_md": f"**Orders** are owned by Ana ({actor.split(':')[-1]})",
                "citations": [DS]}


def signed(payload: dict, headers: dict = None):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, **(headers or {})}, body


def events(channel_actor=None, linked=True):
    sender, chat = Sender(), Chat()
    ev = SlackEvents(SECRET, sender, lambda u: ANA if linked else None,
                     ChatAnswerer(chat, "https://cairn.example.com"), channel_actor)
    ev.async_ = False
    return ev, sender, chat


def test_mrkdwn():
    assert slack_mrkdwn("**Bold** see [docs](https://x.io/a)\n## Title") == "*Bold* see <https://x.io/a|docs>\n*Title*"


def test_dm_answered_privately_as_the_person_and_threads_continue():
    ev, sender, chat = events()
    h, b = signed({"type": "url_verification", "challenge": "c123"})
    assert ev.handle(h, b) == {"challenge": "c123"}
    with pytest.raises(SlackAuthError):
        ev.handle({**h, "X-Slack-Signature": "v0=bad"}, b)
    dm = {"type": "event_callback", "event_id": "Ev1", "event": {
        "type": "message", "channel_type": "im", "channel": "D1", "user": "U1", "text": "who owns orders?", "ts": "1.0"}}
    ev.handle(*signed(dm))
    method, payload = sender.calls[-1]
    assert method == "chat.postMessage" and payload["channel"] == "D1" and payload["thread_ts"] == "1.0"
    assert payload["text"].startswith("*Orders* are owned by Ana (ana)") and "/cairn/asset/" in payload["text"]
    assert chat.calls[-1] == (ANA, "who owns orders?", None)
    follow = {**dm, "event_id": "Ev2", "event": {**dm["event"], "text": "and its quality?", "thread_ts": "1.0", "ts": "2.0"}}
    ev.handle(*signed(follow))
    assert chat.calls[-1] == (ANA, "and its quality?", "conv_1")            # same thread, same conversation
    ev.handle(*signed(dm))                                                  # the same event again: ignored
    ev.handle(*signed({**dm, "event_id": "Ev3"}, {"X-Slack-Retry-Num": "1"}))   # a Slack retry: ignored
    ev.handle(*signed({**dm, "event_id": "Ev4", "event": {**dm["event"], "bot_id": "B1"}}))   # our own reply
    assert len(chat.calls) == 2


def test_channel_mentions_never_post_a_persons_access_to_the_channel():
    mention = {"type": "event_callback", "event_id": "Ev9", "event": {
        "type": "app_mention", "channel": "C1", "user": "U1", "text": "<@UBOT> who owns orders?", "ts": "5.0"}}
    ev, sender, chat = events()
    ev.handle(*signed(mention))
    method, payload = sender.calls[-1]
    assert method == "chat.postEphemeral" and payload["user"] == "U1" and "Only you can see" in payload["text"]
    assert chat.calls[-1][0] == ANA and chat.calls[-1][1] == "who owns orders?"
    ev, sender, chat = events(channel_actor=CHANNEL)
    ev.handle(*signed(mention))
    method, payload = sender.calls[-1]
    assert method == "chat.postMessage" and payload["thread_ts"] == "5.0" and chat.calls[-1][0] == CHANNEL
    ev, sender, chat = events(linked=False)
    ev.handle(*signed({**mention, "event": {**mention["event"], "channel_type": "im", "type": "message"}}))
    assert "not linked" in sender.calls[-1][1]["text"] and chat.calls == []


def teams_signed(payload: dict, secret_b64: str):
    body = json.dumps(payload).encode()
    mac = base64.b64encode(hmac.new(base64.b64decode(secret_b64), body, hashlib.sha256).digest()).decode()
    return {"Authorization": f"HMAC {mac}"}, body


class Http:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None):
        self.posts.append((url, json))


def test_teams_outgoing_webhook():
    secret = base64.b64encode(b"teams-shared-secret").decode()
    msg = {"type": "message", "text": "<at>Cairn</at> who owns <b>orders</b>&nbsp;?", "from": {"aadObjectId": "oid-1"},
           "conversation": {"id": "19:abc"}}
    with pytest.raises(TeamsAuthError):
        verify_teams_hmac(secret, b"{}", "HMAC wrong")
    off = TeamsOutgoing(secret, None, ChatAnswerer(Chat(), "https://c"))
    assert "CAIRN_TEAMS_CHANNEL_ACTOR" in off.handle(*teams_signed(msg, secret))["text"]
    chat = Chat()
    fast = TeamsOutgoing(secret, CHANNEL, ChatAnswerer(chat, "https://c"), resolve_actor=lambda s: ANA)
    out = fast.handle(*teams_signed(msg, secret))
    assert out["type"] == "message" and "owned by Ana" in out["text"]
    assert chat.calls[0][:2] == (CHANNEL, "who owns orders ?")
    http, slow_chat = Http(), Chat(delay=0.3)
    slow = TeamsOutgoing(secret, CHANNEL, ChatAnswerer(slow_chat, "https://c"), reply_webhook="https://hook/x",
                         wait_seconds=0.05, http=http)
    first = slow.handle(*teams_signed(msg, secret))
    assert first["text"].startswith("Looking into") and "in a moment" in first["text"]
    time.sleep(0.6)
    assert http.posts and http.posts[0][0] == "https://hook/x" and "owned by Ana" in http.posts[0][1]["text"]


def test_routes_say_when_not_configured(settings, graph):
    from fastapi.testclient import TestClient

    from cairn.app import create_api_app
    from cairn.services import build_services

    c = TestClient(create_api_app(build_services(settings, graph=graph, with_ai=False, with_gateway=False)))
    assert c.post("/integrations/slack/events", content=b"{}").status_code == 404
    assert c.post("/integrations/teams/messages", content=b"{}").status_code == 404
