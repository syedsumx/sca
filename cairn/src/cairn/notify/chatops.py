"""The assistant in Slack and Microsoft Teams.

**Slack** (Events API, ``/integrations/slack/events``, signed like every Slack request):

* a direct message to the app is answered in the DM, as the person (mapped like buttons are);
  follow-ups in the same thread continue the conversation;
* an @mention in a channel is answered in the thread, as ``CAIRN_SLACK_CHANNEL_ACTOR`` when one
  is set (an identity whose access suits everyone in a channel); otherwise the answer goes to the
  person alone (ephemeral), with their own access. Nothing a person may see is posted for a channel.

**Teams** (outgoing webhook, ``/integrations/teams/messages``, HMAC-signed with the webhook's security
token, ``CAIRN_TEAMS_OUTGOING_SECRET``): an @mention is answered in the channel as
``CAIRN_TEAMS_CHANNEL_ACTOR`` (required: Teams replies are visible to the whole channel). Teams waits
five seconds for the reply; a slower answer is posted to ``CAIRN_TEAMS_REPLY_WEBHOOK`` (an incoming
webhook or Workflows URL for that channel) when one is set.

Every question is audited (``assistant.chat_question``) with the channel and who asked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Optional

import httpx

from cairn.notify.slack import SlackAuthError, verify_slack_signature

logger = logging.getLogger(__name__)

_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")
_TEAMS_AT = re.compile(r"<at>[^<]*</at>\s*", re.IGNORECASE)


def slack_mrkdwn(md: str) -> str:
    """Markdown from the assistant → Slack mrkdwn (bold, links, headings)."""
    out = re.sub(r"\*\*(.+?)\*\*", r"*\1*", md)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", out)
    out = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", out, flags=re.MULTILINE)
    return out


class ChatAnswerer:
    """``answer(actor, question, conversation_key) -> (reply_md, citations)``, keeping one assistant
    conversation per chat thread."""

    def __init__(self, chat: Callable[[str, str, Optional[str]], dict], public_url: str, audit=None):
        self.chat = chat
        self.public_url = public_url.rstrip("/")
        self.audit = audit
        self._threads: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def answer(self, actor: str, question: str, thread_key: str, channel: str, asked_by: str) -> str:
        with self._lock:
            now = time.time()
            self._threads = {k: v for k, v in self._threads.items() if now - v[0] < 86_400}
            conversation = self._threads.get(f"{actor}|{thread_key}", (0, None))[1]
        out = self.chat(actor, question, conversation)
        with self._lock:
            self._threads[f"{actor}|{thread_key}"] = (time.time(), out.get("conversation_id"))
        if self.audit is not None:
            self.audit.record("ai", "assistant.chat_question", asked_by, None,
                              {"channel": channel, "answered_as": actor, "chars": len(question)})
        reply = out.get("reply_md") or "I could not find an answer."
        cites = out.get("citations") or []
        if cites:
            links = " · ".join(f"[{c.split(',')[-2] if ',' in c else c.split(':')[-1]}]"
                               f"({self.public_url}/cairn/asset/{c})" for c in cites[:5])
            reply += f"\n\nSources: {links}"
        return reply


class SlackEvents:
    def __init__(self, signing_secret: str, sender, resolve_actor: Callable[[str], Optional[str]],
                 answerer: ChatAnswerer, channel_actor: Optional[str] = None):
        """``sender``: the bot-token Slack sender (posts as the app)."""
        self.secret = signing_secret
        self.sender = sender
        self.resolve_actor = resolve_actor
        self.answerer = answerer
        self.channel_actor = channel_actor
        self._seen: dict[str, float] = {}
        self.async_ = True

    def handle(self, headers: dict, body: bytes) -> dict:
        h = {k.lower(): v for k, v in headers.items()}
        verify_slack_signature(self.secret, h.get("x-slack-request-timestamp", ""), body, h.get("x-slack-signature", ""))
        payload = json.loads(body or b"{}")
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}
        if payload.get("type") != "event_callback" or h.get("x-slack-retry-num"):
            return {"ok": True}                         # Slack retries an event it thinks we missed: ignore
        eid = payload.get("event_id") or ""
        now = time.time()
        self._seen = {k: t for k, t in self._seen.items() if now - t < 3600}
        if eid in self._seen:
            return {"ok": True}
        self._seen[eid] = now
        ev = payload.get("event") or {}
        if ev.get("bot_id") or ev.get("subtype"):        # our own replies, edits, joins
            return {"ok": True}
        is_dm = ev.get("type") == "message" and ev.get("channel_type") == "im"
        if not (is_dm or ev.get("type") == "app_mention"):
            return {"ok": True}
        work = lambda: self._reply(ev, is_dm)  # noqa: E731
        if self.async_:                                  # Slack wants the ack within three seconds
            threading.Thread(target=work, daemon=True).start()
        else:
            work()
        return {"ok": True}

    def _reply(self, ev: dict, is_dm: bool) -> None:
        user, channel = ev.get("user", ""), ev.get("channel", "")
        thread = ev.get("thread_ts") or ev.get("ts")
        question = _MENTION.sub("", ev.get("text") or "").strip()
        if not question:
            return
        person = self.resolve_actor(user)
        try:
            if is_dm or not self.channel_actor:
                if person is None:
                    text = ("Your Slack account is not linked to a Cairn user. Link it under Cairn → Settings → "
                            "Notifications.")
                else:
                    text = slack_mrkdwn(self.answerer.answer(person, question, f"slack:{channel}:{thread}",
                                                             "slack-dm" if is_dm else "slack-ephemeral", person))
                if is_dm:
                    self.sender._call("chat.postMessage", {"channel": channel, "thread_ts": thread, "text": text})
                else:
                    self.sender._call("chat.postEphemeral", {"channel": channel, "user": user, "thread_ts": thread,
                                                             "text": text + "\n_Only you can see this answer: it uses "
                                                                            "your own access._"})
            else:
                text = slack_mrkdwn(self.answerer.answer(self.channel_actor, question, f"slack:{channel}:{thread}",
                                                         "slack-channel", person or f"slack:{user}"))
                self.sender._call("chat.postMessage", {"channel": channel, "thread_ts": thread, "text": text})
        except Exception:  # noqa: BLE001 - a failed answer must not crash the event handler
            logger.exception("slack: could not answer in %s", channel)


class TeamsAuthError(Exception):
    pass


def verify_teams_hmac(secret_b64: str, body: bytes, authorization: str) -> None:
    """Outgoing webhook: ``Authorization: HMAC <base64(HMAC-SHA256(base64-decoded secret, body))>``."""
    if not secret_b64:
        raise TeamsAuthError("Teams outgoing webhook secret not configured")
    if not authorization.startswith("HMAC "):
        raise TeamsAuthError("missing HMAC authorization")
    try:
        key = base64.b64decode(secret_b64)
    except ValueError:
        raise TeamsAuthError("the Teams secret is not base64") from None
    expected = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, authorization[5:].strip()):
        raise TeamsAuthError("bad HMAC signature")


class TeamsOutgoing:
    def __init__(self, secret_b64: str, channel_actor: Optional[str], answerer: ChatAnswerer,
                 reply_webhook: Optional[str] = None, resolve_actor: Optional[Callable[[dict], Optional[str]]] = None,
                 wait_seconds: float = 4.0, http: Optional[httpx.Client] = None):
        self.secret = secret_b64
        self.channel_actor = channel_actor
        self.answerer = answerer
        self.reply_webhook = reply_webhook
        self.resolve_actor = resolve_actor or (lambda sender: None)
        self.wait = wait_seconds
        self.http = http or httpx.Client(timeout=10)

    def handle(self, headers: dict, body: bytes) -> dict:
        h = {k.lower(): v for k, v in headers.items()}
        verify_teams_hmac(self.secret, body, h.get("authorization", ""))
        activity = json.loads(body or b"{}")
        question = " ".join(html.unescape(re.sub(r"<[^>]+>", "", _TEAMS_AT.sub("", activity.get("text") or ""))).split())
        if not question:
            return {"type": "message", "text": "Ask me about data: who owns a table, what a metric means, where data "
                                               "comes from."}
        if not self.channel_actor:
            return {"type": "message", "text": "Cairn answers in Teams channels once an administrator sets "
                                               "CAIRN_TEAMS_CHANNEL_ACTOR (the access answers here may use)."}
        sender = activity.get("from") or {}
        asked_by = self.resolve_actor(sender) or f"teams:{sender.get('aadObjectId') or sender.get('id') or 'unknown'}"
        thread = (activity.get("conversation") or {}).get("id") or "teams"
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["text"] = self.answerer.answer(self.channel_actor, question, f"teams:{thread}", "teams",
                                                      asked_by)
            except Exception as e:  # noqa: BLE001
                logger.exception("teams: could not answer")
                result["text"] = f"I could not answer that: {e}"
            if result.get("late") and self.reply_webhook:
                try:
                    self.http.post(self.reply_webhook, json={"text": result["text"]})
                except httpx.HTTPError as e:
                    logger.warning("teams: reply webhook failed: %s", e)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(self.wait)
        if "text" in result:
            return {"type": "message", "text": result["text"]}
        result["late"] = True
        later = "I'll post the answer here in a moment." if self.reply_webhook else \
            "This one takes longer than Teams waits for; ask the Cairn assistant in the product."
        return {"type": "message", "text": f"Looking into “{question[:120]}”. {later}"}


__all__ = ["ChatAnswerer", "SlackEvents", "TeamsOutgoing", "SlackAuthError", "TeamsAuthError", "slack_mrkdwn",
           "verify_teams_hmac"]
