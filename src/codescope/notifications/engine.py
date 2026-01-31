"""Notification delivery for Slack, Teams, Email, and generic webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import time
import urllib.request
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

_SEVERITY_COLORS = {"info": "#36a64f", "warning": "#ff9800", "critical": "#e53935"}


@dataclass
class NotificationEvent:
    """An event to be delivered to notification targets."""

    event_type: str  # scan_completed, quality_gate_failed, new_vulnerability, scan_failed
    project_name: str
    timestamp: str
    severity: str  # info, warning, critical
    summary: str
    details: dict = field(default_factory=dict)
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "project_name": self.project_name,
            "timestamp": self.timestamp,
            "severity": self.severity,
            "summary": self.summary,
            "details": self.details,
            "url": self.url,
        }


@dataclass
class WebhookTarget:
    """A generic webhook endpoint."""

    url: str
    secret: str = ""
    events: list[str] = field(default_factory=list)
    active: bool = True


class SlackNotifier:
    """Send notifications via Slack incoming webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, event: NotificationEvent) -> bool:
        color = _SEVERITY_COLORS.get(event.severity, "#439FE0")
        detail_fields = [
            {"type": "mrkdwn", "text": f"*{k}:* {v}"}
            for k, v in event.details.items()
        ]
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"CodeScope — {event.event_type.replace('_', ' ').title()}"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{event.project_name}*\n{event.summary}"},
                },
            ],
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {"type": "section", "fields": detail_fields[:10]} if detail_fields else
                        {"type": "section", "text": {"type": "mrkdwn", "text": "_No additional details_"}},
                    ],
                }
            ],
        }
        if event.url:
            payload["blocks"].append({
                "type": "actions",
                "elements": [{"type": "button", "text": {"type": "plain_text", "text": "View in Dashboard"}, "url": event.url}],
            })
        return self._post(payload)

    def _post(self, payload: dict) -> bool:
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as exc:
            logger.error("Slack notification failed: %s", exc)
            return False


class TeamsNotifier:
    """Send notifications via Microsoft Teams incoming webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, event: NotificationEvent) -> bool:
        color = _SEVERITY_COLORS.get(event.severity, "#439FE0")
        facts = [{"name": k, "value": str(v)} for k, v in event.details.items()]
        payload = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
                         "text": f"CodeScope — {event.event_type.replace('_', ' ').title()}", "color": "Accent"},
                        {"type": "TextBlock", "text": event.project_name, "weight": "Bolder"},
                        {"type": "TextBlock", "text": event.summary, "wrap": True},
                        {"type": "FactSet", "facts": facts[:10]} if facts else
                        {"type": "TextBlock", "text": "_No additional details_", "isSubtle": True},
                    ],
                    "actions": [{"type": "Action.OpenUrl", "title": "View in Dashboard", "url": event.url}] if event.url else [],
                },
            }],
        }
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 202)
        except Exception as exc:
            logger.error("Teams notification failed: %s", exc)
            return False


class EmailNotifier:
    """Send notifications via SMTP email."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        username: str = "",
        password: str = "",
        from_addr: str = "",
        use_tls: bool = True,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr or username
        self.use_tls = use_tls

    def send(self, event: NotificationEvent, to_addrs: list[str]) -> bool:
        if not to_addrs:
            return False
        subject = f"[CodeScope] {event.event_type.replace('_', ' ').title()} — {event.project_name}"
        rows = "".join(
            f"<tr><td style='padding:4px 8px;font-weight:bold'>{k}</td><td style='padding:4px 8px'>{v}</td></tr>"
            for k, v in event.details.items()
        )
        color = _SEVERITY_COLORS.get(event.severity, "#439FE0")
        html = f"""<div style="font-family:sans-serif;max-width:600px">
<div style="background:{color};color:#fff;padding:12px 16px;border-radius:6px 6px 0 0">
  <h2 style="margin:0">CodeScope — {event.event_type.replace('_', ' ').title()}</h2>
</div>
<div style="border:1px solid #ddd;border-top:none;padding:16px;border-radius:0 0 6px 6px">
  <p><strong>Project:</strong> {event.project_name}</p>
  <p>{event.summary}</p>
  <table style="border-collapse:collapse;width:100%">{rows}</table>
  {f'<p><a href="{event.url}">View in Dashboard</a></p>' if event.url else ''}
  <p style="color:#999;font-size:12px">{event.timestamp}</p>
</div></div>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(to_addrs)
        msg.attach(MIMEText(event.summary, "plain"))
        msg.attach(MIMEText(html, "html"))
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.sendmail(self.from_addr, to_addrs, msg.as_string())
            return True
        except Exception as exc:
            logger.error("Email notification failed: %s", exc)
            return False


class WebhookDelivery:
    """Deliver events to a generic webhook URL with HMAC signing."""

    def __init__(self, target: WebhookTarget) -> None:
        self.target = target

    def deliver(self, event: NotificationEvent) -> bool:
        if not self.target.active:
            return True
        if self.target.events and event.event_type not in self.target.events:
            return True  # not subscribed to this event type

        body = json.dumps(event.to_dict()).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.target.secret:
            sig = hmac.new(self.target.secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-CodeScope-Signature"] = f"sha256={sig}"

        for attempt in range(3):
            try:
                req = urllib.request.Request(self.target.url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status < 300:
                        return True
            except Exception as exc:
                logger.warning("Webhook delivery attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)

        logger.error("Webhook delivery to %s failed after 3 attempts", self.target.url)
        return False


class NotificationEngine:
    """Central manager for all notification channels."""

    def __init__(self) -> None:
        self._slack: list[SlackNotifier] = []
        self._teams: list[TeamsNotifier] = []
        self._email: list[tuple[EmailNotifier, list[str]]] = []
        self._webhooks: list[WebhookDelivery] = []

    def add_slack(self, webhook_url: str) -> None:
        self._slack.append(SlackNotifier(webhook_url))

    def add_teams(self, webhook_url: str) -> None:
        self._teams.append(TeamsNotifier(webhook_url))

    def add_email(
        self, smtp_host: str, smtp_port: int, username: str, password: str,
        from_addr: str, to_addrs: list[str], use_tls: bool = True,
    ) -> None:
        notifier = EmailNotifier(smtp_host, smtp_port, username, password, from_addr, use_tls)
        self._email.append((notifier, to_addrs))

    def add_webhook(self, target: WebhookTarget) -> None:
        self._webhooks.append(WebhookDelivery(target))

    def notify(self, event: NotificationEvent) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for i, s in enumerate(self._slack):
            results[f"slack_{i}"] = s.send(event)
        for i, t in enumerate(self._teams):
            results[f"teams_{i}"] = t.send(event)
        for i, (e, addrs) in enumerate(self._email):
            results[f"email_{i}"] = e.send(event, addrs)
        for i, w in enumerate(self._webhooks):
            results[f"webhook_{i}"] = w.deliver(event)
        return results

    @classmethod
    def from_config(cls, config: dict) -> NotificationEngine:
        engine = cls()
        if config.get("slack_webhook_url"):
            engine.add_slack(config["slack_webhook_url"])
        if config.get("teams_webhook_url"):
            engine.add_teams(config["teams_webhook_url"])
        if config.get("email_smtp_host"):
            engine.add_email(
                config["email_smtp_host"], config.get("email_smtp_port", 587),
                config.get("email_username", ""), config.get("email_password", ""),
                config.get("email_from", ""), config.get("email_to", []),
                config.get("email_use_tls", True),
            )
        return engine
