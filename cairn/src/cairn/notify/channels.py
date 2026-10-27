"""Channel senders and message renderers (Slack Block Kit, Teams Adaptive Cards, email, webhooks)."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import smtplib
import socket
import time
from email.message import EmailMessage
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
# Block Kit limits (https://api.slack.com/reference/block-kit): over them Slack rejects the whole message.
SECTION_TEXT_MAX, BUTTON_TEXT_MAX, BUTTON_VALUE_MAX, ACTION_ID_MAX, URL_MAX = 3000, 75, 2000, 255, 3000
# Teams Workflows / connector webhooks live on Microsoft hosts; anything else is refused (a personal
# webhook URL is user input, and the notifier must not be pointed at internal services).
DEFAULT_TEAMS_HOSTS = (".webhook.office.com", ".logic.azure.com", ".powerplatform.com", ".powerautomate.com")


def slack_api_url() -> str:
    return os.environ.get("CAIRN_SLACK_API_URL", SLACK_API).rstrip("/")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _actions(row: dict) -> list[dict]:
    return (row.get("payload") or {}).get("actions") or []


def _url(row: dict) -> Optional[str]:
    return (row.get("payload") or {}).get("url")


# --------------------------------------------------------------------------- Slack
def slack_blocks(row: dict) -> list[dict]:
    title = _esc(_clip(row["title"], 300))
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": _clip(f"*{title}*\n{_esc(row['body'])}", SECTION_TEXT_MAX)}},
    ]
    elements: list[dict] = []
    for a in _actions(row):
        btn = {"type": "button", "text": {"type": "plain_text", "text": _clip(a["label"], BUTTON_TEXT_MAX)},
               "action_id": f"cairn:{a['kind']}:{a['action']}"[:ACTION_ID_MAX],
               "value": a["object_id"][:BUTTON_VALUE_MAX]}
        if a.get("style") in ("primary", "danger"):
            btn["style"] = a["style"]
        if a["action"] == "reject":
            btn["confirm"] = {"title": {"type": "plain_text", "text": "Reject?"},
                              "text": {"type": "mrkdwn", "text": "The requester will be notified."},
                              "confirm": {"type": "plain_text", "text": "Reject"},
                              "deny": {"type": "plain_text", "text": "Cancel"}}
        elements.append(btn)
    if _url(row) and len(_url(row) or "") <= URL_MAX:
        elements.append({"type": "button", "text": {"type": "plain_text", "text": "Open in Cairn"},
                         "url": _url(row), "action_id": "cairn:open"})
    if elements:
        blocks.append({"type": "actions", "elements": elements[:25]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Cairn · `{row['type']}`"}]})
    return blocks


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def validate_blocks(blocks: list[dict]) -> list[str]:
    """Block Kit rules Slack enforces (``invalid_blocks``) for the blocks Cairn sends."""
    problems: list[str] = []
    if len(blocks) > 50:
        problems.append(f"{len(blocks)} blocks (max 50)")
    action_ids: set[str] = set()
    for i, b in enumerate(blocks):
        kind = b.get("type")
        if kind == "section":
            text = (b.get("text") or {}).get("text", "")
            if not text or len(text) > SECTION_TEXT_MAX:
                problems.append(f"block {i}: section text length {len(text)} (1..{SECTION_TEXT_MAX})")
        elif kind == "actions":
            els = b.get("elements") or []
            if not 1 <= len(els) <= 25:
                problems.append(f"block {i}: {len(els)} action elements (1..25)")
            for e in els:
                label = (e.get("text") or {}).get("text", "")
                if e.get("type") != "button" or (e.get("text") or {}).get("type") != "plain_text":
                    problems.append(f"block {i}: only plain_text buttons are sent")
                if not label or len(label) > BUTTON_TEXT_MAX:
                    problems.append(f"block {i}: button text {label!r}")
                aid = e.get("action_id", "")
                if not aid or len(aid) > ACTION_ID_MAX or aid in action_ids:
                    problems.append(f"block {i}: action_id {aid!r} missing, too long or repeated")
                action_ids.add(aid)
                if len(e.get("value", "")) > BUTTON_VALUE_MAX or len(e.get("url", "")) > URL_MAX:
                    problems.append(f"block {i}: button value/url too long")
                if e.get("style") not in (None, "primary", "danger"):
                    problems.append(f"block {i}: style {e.get('style')!r}")
        elif kind == "context":
            if not 1 <= len(b.get("elements") or []) <= 10:
                problems.append(f"block {i}: context needs 1..10 elements")
        else:
            problems.append(f"block {i}: unexpected type {kind!r}")
    return problems


class SlackSender:
    """Bot-token sender (``chat.postMessage``). DMs resolve the user via settings or email lookup."""

    def __init__(self, bot_token: str, email_of: Optional[Callable[[str], Optional[str]]] = None,
                 http: Optional[httpx.Client] = None, api_url: Optional[str] = None, max_retry_after: float = 10.0):
        self.token = bot_token
        self.email_of = email_of or (lambda actor: None)
        self.http = http or httpx.Client(timeout=15)
        self.api = (api_url or slack_api_url()).rstrip("/")
        self.max_retry_after = max_retry_after

    def _request(self, verb: str, method: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        if verb == "POST":
            headers["Content-Type"] = "application/json; charset=utf-8"
        for attempt in (1, 2):
            resp = self.http.request(verb, f"{self.api}/{method}", headers=headers, **kw)
            if resp.status_code == 429 and attempt == 1:
                # Rate limited: wait as asked when it is short, otherwise leave it to the next delivery round.
                wait = float(resp.headers.get("Retry-After") or 1)
                if wait > self.max_retry_after:
                    raise RuntimeError(f"slack {method} rate limited for {wait:.0f}s")
                time.sleep(wait)
                continue
            break
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(f"slack {method} returned HTTP {resp.status_code} without a JSON body") from None
        if not body.get("ok"):
            raise RuntimeError(f"slack {method} failed: {body.get('error') or resp.status_code}")
        return body

    def _call(self, method: str, payload: dict) -> dict:
        return self._request("POST", method, json=payload)

    def _lookup(self, method: str, params: dict) -> dict:
        return self._request("GET", method, params=params)

    def resolve_channel(self, row: dict) -> str:
        if row.get("target"):
            return row["target"]
        email = self.email_of(row["recipient"])
        if not email:
            raise RuntimeError(f"no Slack id or email known for {row['recipient']}")
        return self._lookup("users.lookupByEmail", {"email": email})["user"]["id"]

    def send(self, row: dict, hub=None) -> str:
        channel = self.resolve_channel(row)
        blocks = slack_blocks(row)
        problems = validate_blocks(blocks)
        if problems:   # never expected: the renderer clips to the limits
            raise RuntimeError(f"refusing to send invalid Slack blocks: {problems}")
        body = self._call("chat.postMessage", {"channel": channel, "text": _clip(f"{row['title']}: {row['body']}", 3000),
                                                "blocks": blocks, "unfurl_links": False})
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
        from cairn.notify.hub import Action

        try:
            link = hub.action_link(Action(**a), row["recipient"]) if not str(row["recipient"]).startswith("route:") \
                else _url(row)
        except RuntimeError:
            link = _url(row)
        if link:
            actions.append({"type": "Action.OpenUrl", "title": a["label"], "url": link})
    if _url(row):
        actions.append({"type": "Action.OpenUrl", "title": "Open in Cairn", "url": _url(row)})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{"type": "TextBlock", "text": _clip(row["title"], 300), "weight": "Bolder", "wrap": True},
                         {"type": "TextBlock", "text": _clip(row["body"], 4000), "wrap": True},
                         {"type": "TextBlock", "text": f"Cairn · {row['type']}", "isSubtle": True, "size": "Small"}],
                "actions": actions[:6],
            },
        }],
    }


def validate_adaptive_card(message: dict) -> list[str]:
    """The Adaptive Card 1.4 shape Teams Workflows accept in a webhook message."""
    problems: list[str] = []
    atts = message.get("attachments") or []
    if message.get("type") != "message" or len(atts) != 1:
        return ["expected a message with exactly one attachment"]
    att = atts[0]
    if att.get("contentType") != "application/vnd.microsoft.card.adaptive":
        problems.append(f"contentType {att.get('contentType')!r}")
    card = att.get("content") or {}
    if card.get("type") != "AdaptiveCard" or card.get("version") not in ("1.0", "1.2", "1.3", "1.4", "1.5"):
        problems.append("content must be an AdaptiveCard of a version Teams renders")
    body = card.get("body") or []
    if not body:
        problems.append("card body is empty")
    for i, el in enumerate(body):
        if el.get("type") != "TextBlock" or not isinstance(el.get("text"), str) or not el["text"]:
            problems.append(f"body[{i}] must be a non-empty TextBlock")
    for i, a in enumerate(card.get("actions") or []):
        if a.get("type") != "Action.OpenUrl" or not a.get("title"):
            problems.append(f"actions[{i}] must be a titled Action.OpenUrl")
        elif urlparse(a.get("url", "")).scheme not in ("https", "http"):
            problems.append(f"actions[{i}] url {a.get('url')!r}")
    if len(card.get("actions") or []) > 6:
        problems.append("Teams shows at most 6 actions")
    return problems


def _allowed_entries(env: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(env)
    return [e.strip() for e in raw.split(",") if e.strip()] if raw is not None else list(default)


def teams_webhook_allowed(url: Optional[str]) -> bool:
    """https on a Microsoft webhook host (``CAIRN_TEAMS_WEBHOOK_HOSTS``: host suffixes such as
    ``.logic.azure.com``, or exact origins such as ``https://hooks.internal:8443``)."""
    if not url:
        return False
    u = urlparse(url)
    origin = f"{u.scheme}://{u.netloc}"
    host = (u.hostname or "").lower()
    for entry in _allowed_entries("CAIRN_TEAMS_WEBHOOK_HOSTS", DEFAULT_TEAMS_HOSTS):
        if "://" in entry:
            if origin == entry.rstrip("/"):
                return True
        elif u.scheme == "https" and (host == entry.lstrip(".") or host.endswith(entry if entry.startswith(".")
                                                                                  else "." + entry)):
            return True
    return False


class TeamsSender:
    def __init__(self, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=15)

    def send(self, row: dict, hub) -> str:
        url = row.get("target")
        if not teams_webhook_allowed(url):
            raise RuntimeError("Teams target must be an https webhook on a Microsoft host "
                               "(see CAIRN_TEAMS_WEBHOOK_HOSTS)")
        resp = self.http.post(url, json=teams_card(row, hub))
        if resp.status_code >= 300:
            raise RuntimeError(f"teams webhook returned {resp.status_code}")
        return "teams"


# --------------------------------------------------------------------------- email / webhook
class EmailSender:
    def __init__(self, smtp_url: str, sender: str = "cairn@localhost",
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
            from cairn.notify.hub import Action

            lines.append(f"{a['label']}: {hub.action_link(Action(**a), row['recipient'])}")
        if _url(row):
            lines.append(f"Open in Cairn: {_url(row)}")
        msg.set_content("\n".join(lines))
        smtp_cls = smtplib.SMTP_SSL if self.ssl else smtplib.SMTP
        with smtp_cls(self.host, self.port, timeout=15) as s:
            if not self.ssl:
                s.starttls()
            if self.user:
                s.login(self.user, self.password or "")
            s.send_message(msg)
        return "email"


def webhook_target_problem(url: str, resolve: Callable[[str], list[str]] = None) -> Optional[str]:
    """Why a webhook URL must not be called, or None. Loopback, link-local (cloud metadata) and
    unspecified addresses are always refused; private networks only with
    ``CAIRN_WEBHOOK_ALLOW_PRIVATE=true`` (the default, for on-premises receivers)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return "webhook URLs must be http(s) with a host"
    resolve = resolve or (lambda h: [ai[4][0] for ai in socket.getaddrinfo(h, None)])
    try:
        addresses = {ipaddress.ip_address(a.split("%")[0]) for a in resolve(u.hostname)}
    except (OSError, ValueError) as e:
        return f"cannot resolve {u.hostname}: {e}"
    allow_private = os.environ.get("CAIRN_WEBHOOK_ALLOW_PRIVATE", "true").lower() == "true"
    for ip in addresses:
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return f"{u.hostname} resolves to {ip}, which webhooks may not reach"
        if ip.is_private and not allow_private:
            return f"{u.hostname} resolves to private address {ip} (CAIRN_WEBHOOK_ALLOW_PRIVATE=false)"
    return None


class WebhookSender:
    def __init__(self, http: Optional[httpx.Client] = None, resolve: Optional[Callable[[str], list[str]]] = None):
        self.http = http or httpx.Client(timeout=15, follow_redirects=False)
        self.resolve = resolve

    def send(self, row: dict, hub) -> str:
        problem = webhook_target_problem(row["target"], self.resolve)
        if problem:
            raise RuntimeError(problem)
        payload: dict[str, Any] = {k: row[k] for k in ("id", "type", "title", "body", "entity_urn")}
        payload["url"] = _url(row)
        resp = self.http.post(row["target"], content=json.dumps(payload), headers={"Content-Type": "application/json"})
        if resp.status_code >= 300:
            raise RuntimeError(f"webhook returned {resp.status_code}")
        return "webhook"
