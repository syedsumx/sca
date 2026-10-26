"""Slack interactivity: signed requests → DataHub actions as the clicking user.

* Every request is verified with the app's signing secret: ``v0=HMAC_SHA256(secret, "v0:{ts}:{body}")``
  with |now - ts| ≤ 300 s.
* The Slack user is mapped to a DataHub user (explicit link in notification settings, otherwise
  Slack profile email → DataHub corpUser). Unmapped users are refused.
* Button clicks (``action_id = aegis:<kind>:<action>``, ``value = object id``) dispatch through
  :meth:`NotificationHub.execute_action`, whose handlers re-authorize the actor; the original
  message is then replaced via ``response_url``.
* ``/aegis search <text>`` and ``/aegis ask <question>`` answer ephemerally.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Callable, Optional
from urllib.parse import parse_qs

import httpx

logger = logging.getLogger(__name__)


class SlackAuthError(Exception):
    pass


def verify_slack_signature(secret: str, timestamp: str, body: bytes, signature: str, now: Optional[float] = None,
                           max_skew: int = 300) -> None:
    if not secret:
        raise SlackAuthError("Slack signing secret not configured")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SlackAuthError("missing or bad X-Slack-Request-Timestamp")
    if abs((now or time.time()) - ts) > max_skew:
        raise SlackAuthError("stale Slack request")
    base = f"v0:{ts}:".encode() + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise SlackAuthError("invalid Slack signature")


class SlackInteractions:
    def __init__(self, hub, signing_secret: str, resolve_actor: Callable[[str], Optional[str]],
                 search: Optional[Callable[[str, str], list[dict]]] = None,
                 ask: Optional[Callable[[str, str], str]] = None, http: Optional[httpx.Client] = None):
        self.hub = hub
        self.secret = signing_secret
        self.resolve_actor = resolve_actor
        self.search = search
        self.ask = ask
        self.http = http or httpx.Client(timeout=10)

    def _verify(self, headers: dict, body: bytes) -> None:
        h = {k.lower(): v for k, v in headers.items()}
        verify_slack_signature(self.secret, h.get("x-slack-request-timestamp", ""), body,
                               h.get("x-slack-signature", ""))

    def _actor(self, slack_user_id: str) -> str:
        actor = self.hub.actor_for_slack_user(slack_user_id) or self.resolve_actor(slack_user_id)
        if not actor:
            raise PermissionError("Your Slack account is not linked to a DataHub user. "
                                  "Link it under Aegis → Workspace → Settings → Notifications.")
        return actor

    # ---- buttons -----------------------------------------------------------------------
    def handle_interaction(self, headers: dict, body: bytes) -> dict:
        self._verify(headers, body)
        payload = json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
        if payload.get("type") != "block_actions":
            return {"ok": True}
        action = (payload.get("actions") or [{}])[0]
        action_id = action.get("action_id", "")
        if action_id == "aegis:open" or not action_id.startswith("aegis:"):
            return {"ok": True}
        _, kind, verb = (action_id.split(":", 2) + ["", ""])[:3]
        slack_user = (payload.get("user") or {}).get("id", "")
        response_url = payload.get("response_url")
        try:
            actor = self._actor(slack_user)
            message = self.hub.execute_action(kind, verb, action.get("value", ""), actor, None, channel="slack")
            text = f"✅ {message} — by <@{slack_user}>"
            replace = True
        except PermissionError as e:
            text, replace = f"⛔ {e}", False
        except Exception as e:  # handler-level failures (wrong state, etc.)
            text, replace = f"⚠️ Could not complete the action: {e}", False
        if response_url:
            self._respond(response_url, {"replace_original": replace, "response_type": "ephemeral" if not replace
                                         else "in_channel", "text": text})
        return {"ok": True, "text": text}

    # ---- slash commands ----------------------------------------------------------------
    def handle_command(self, headers: dict, body: bytes) -> dict:
        self._verify(headers, body)
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        text = (form.get("text") or "").strip()
        verb, _, rest = text.partition(" ")
        try:
            actor = self._actor(form.get("user_id", ""))
        except PermissionError as e:
            return {"response_type": "ephemeral", "text": f"⛔ {e}"}
        if verb == "search" and rest and self.search:
            results = self.search(actor, rest)[:5]
            if not results:
                return {"response_type": "ephemeral", "text": f"No assets you can access match “{rest}”."}
            lines = [f"• *{r.get('name')}* ({r.get('platform') or r.get('type')}) — {(r.get('description') or '')[:90]}"
                     for r in results]
            return {"response_type": "ephemeral", "text": "\n".join(lines)}
        if verb == "ask" and rest and self.ask:
            response_url = form.get("response_url")

            def answer() -> None:
                try:
                    reply = self.ask(actor, rest)
                except Exception as e:
                    reply = f"⚠️ {e}"
                if response_url:
                    self._respond(response_url, {"response_type": "ephemeral", "text": reply})

            threading.Thread(target=answer, daemon=True).start()
            return {"response_type": "ephemeral", "text": f"🔎 Looking into “{rest}”…"}
        return {"response_type": "ephemeral", "text": "Usage: `/aegis search <text>` · `/aegis ask <question>`"}

    def _respond(self, url: str, payload: dict) -> None:
        if not url.startswith("https://hooks.slack.com/"):
            logger.warning("refusing to post to non-Slack response_url")
            return
        try:
            self.http.post(url, json=payload)
        except httpx.HTTPError as e:
            logger.warning("slack response_url post failed: %s", e)
