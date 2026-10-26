"""Channel senders and message renderers (Slack Block Kit, Teams Adaptive Cards, email, webhooks)."""

from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

SLACK_API = "https://slack.com/api"


def _actions(row: dict) -> list[dict]:
    return (row.get("payload") or {}).get("actions") or []


def _url(row: dict) -> Optional[str]:
    return (row.get("payload") or {}).get("url")


# --------------------------------------------------------------------------- Slack
def slack_blocks(row: dict) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_esc(row['title'])}*\n{_esc(row['body'])}"}},
    ]
    elements: list[dict] = []
    for a in _actions(row):
        btn = {"type": "button", "text": {"type": "plain_text", "text": a["label"][:75]},
               "action_id": f"aegis:{a['kind']}:{a['action']}", "value": a["object_id"]}
        if a.get("style") in ("primary", "danger"):
            btn["style"] = a["style"]
        if a["action"] == "reject":
            btn["confirm"] = {"title": {"type": "plain_text", "text": "Reject?"},
                              "text": {"type": "mrkdwn", "text": "The requester will be notified."},
                              "confirm": {"type": "plain_text", "text": "Reject"},
                              "deny": {"type": "plain_text", "text": "Cancel"}}
        elements.append(btn)
    if _url(row):
        elements.append({"type": "button", "text": {"type": "plain_text", "text": "Open in Aegis"},
                         "url": _url(row), "action_id": "aegis:open"})
    if elements:
        blocks.append({"type": "actions", "elements": elements[:25]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Aegis · `{row['type']}`"}]})
    return blocks


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackSender:
    """Bot-token sender (``chat.postMessage``). DMs resolve the user via settings or email lookup."""

    def __init__(self, bot_token: str, email_of: Optional[Callable[[str], Optional[str]]] = None,
                 http: Optional[httpx.Client] = None):
        self.token = bot_token
        self.email_of = email_of or (lambda actor: None)
        self.http = http or httpx.Client(timeout=15)

    def _call(self, method: str, payload: dict) -> dict:
        resp = self.http.post(f"{SLACK_API}/{method}", json=payload,
                              headers={"Authorization": f"Bearer {self.token}",
                                       "Content-Type": "application/json; charset=utf-8"})
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"slack {method} failed: {body.get('error')}")
        return body

    def _lookup(self, method: str, params: dict) -> dict:
        resp = self.http.get(f"{SLACK_API}/{method}", params=params,
                             headers={"Authorization": f"Bearer {self.token}"})
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"slack {method} failed: {body.get('error')}")
        return body

    def resolve_channel(self, row: dict) -> str:
        if row.get("target"):
            return row["target"]
        email = self.email_of(row["recipient"])
        if not email:
            raise RuntimeError(f"no Slack id or email known for {row['recipient']}")
        return self._lookup("users.lookupByEmail", {"email": email})["user"]["id"]

    def send(self, row: dict, hub=None) -> str:
        channel = self.resolve_channel(row)
        body = self._call("chat.postMessage", {"channel": channel, "text": f"{row['title']}: {row['body']}"[:3000],
                                                "blocks": slack_blocks(row), "unfurl_links": False})
        return f"{body.get('channel')}:{body.get('ts')}"

    def update(self, channel: str, ts: str, text: str) -> None:
        self._call("chat.update", {"channel": channel, "ts": ts, "text": text,
                                   "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": _esc(text)}}]})

    def user_email(self, slack_user_id: str) -> Optional[str]:
        return ((self._lookup("users.info", {"user": slack_user_id}).get("user") or {}).get("profile") or {}).get("email")


# --------------------------------------------------------------------------- Teams
def teams_card(row: dict, hub) -> dict:
    actions = []
    for a in _actions(row):
        from aegis.notify.hub import Action

        try:
            link = hub.action_link(Action(**a), row["recipient"]) if not str(row["recipient"]).startswith("route:") \
                else _url(row)
        except RuntimeError:
            link = _url(row)
        if link:
            actions.append({"type": "Action.OpenUrl", "title": a["label"], "url": link})
    if _url(row):
        actions.append({"type": "Action.OpenUrl", "title": "Open in Aegis", "url": _url(row)})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{"type": "TextBlock", "text": row["title"], "weight": "Bolder", "wrap": True},
                         {"type": "TextBlock", "text": row["body"], "wrap": True},
                         {"type": "TextBlock", "text": f"Aegis · {row['type']}", "isSubtle": True, "size": "Small"}],
                "actions": actions[:6],
            },
        }],
    }


class TeamsSender:
    def __init__(self, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=15)

    def send(self, row: dict, hub) -> str:
        url = row.get("target")
        if not url or urlparse(url).scheme != "https":
            raise RuntimeError("Teams target must be an https webhook URL")
        resp = self.http.post(url, json=teams_card(row, hub))
        if resp.status_code >= 300:
            raise RuntimeError(f"teams webhook returned {resp.status_code}")
        return "teams"


# --------------------------------------------------------------------------- email / webhook
class EmailSender:
    def __init__(self, smtp_url: str, sender: str = "aegis@localhost",
                 email_of: Optional[Callable[[str], Optional[str]]] = None):
        u = urlparse(smtp_url)  # smtp://user:pass@host:587 or smtps://...
        self.host, self.port = u.hostname, u.port or (465 if u.scheme == "smtps" else 587)
        self.user, self.password, self.ssl = u.username, u.password, u.scheme == "smtps"
        self.sender = sender
        self.email_of = email_of or (lambda actor: None)

    def send(self, row: dict, hub) -> str:
        to = self.email_of(row["recipient"])
        if not to:
            raise RuntimeError(f"no email for {row['recipient']}")
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = row["title"], self.sender, to
        lines = [row["body"], ""]
        for a in _actions(row):
            from aegis.notify.hub import Action

            lines.append(f"{a['label']}: {hub.action_link(Action(**a), row['recipient'])}")
        if _url(row):
            lines.append(f"Open in Aegis: {_url(row)}")
        msg.set_content("\n".join(lines))
        smtp_cls = smtplib.SMTP_SSL if self.ssl else smtplib.SMTP
        with smtp_cls(self.host, self.port, timeout=15) as s:
            if not self.ssl:
                s.starttls()
            if self.user:
                s.login(self.user, self.password or "")
            s.send_message(msg)
        return "email"


class WebhookSender:
    def __init__(self, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=15)

    def send(self, row: dict, hub) -> str:
        payload: dict[str, Any] = {k: row[k] for k in ("id", "type", "title", "body", "entity_urn")}
        payload["url"] = _url(row)
        resp = self.http.post(row["target"], content=json.dumps(payload), headers={"Content-Type": "application/json"})
        if resp.status_code >= 300:
            raise RuntimeError(f"webhook returned {resp.status_code}")
        return "webhook"
