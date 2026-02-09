"""Webhook and notification management endpoints."""

from __future__ import annotations

import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.notifications.engine import (
    NotificationEvent,
    SlackNotifier,
    TeamsNotifier,
    EmailNotifier,
    WebhookDelivery,
    WebhookTarget,
)

router = APIRouter()

# In-memory store (persisted to DB in production)
_webhook_configs: list[dict] = []
_notification_config: dict = {}


class WebhookCreate(BaseModel):
    url: str
    secret: str = ""
    events: list[str] = []
    active: bool = True


class WebhookUpdate(BaseModel):
    url: Optional[str] = None
    secret: Optional[str] = None
    events: Optional[list[str]] = None
    active: Optional[bool] = None


class NotificationConfig(BaseModel):
    slack_webhook_url: str = ""
    teams_webhook_url: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_to: list[str] = []
    email_use_tls: bool = True


class TestNotification(BaseModel):
    channel: str  # slack, teams, email, webhook
    webhook_id: Optional[int] = None


@router.get("/webhooks")
async def list_webhooks():
    """List all configured webhooks."""
    return [
        {**w, "id": i, "secret": "***" if w.get("secret") else ""}
        for i, w in enumerate(_webhook_configs)
    ]


@router.post("/webhooks")
async def create_webhook(req: WebhookCreate):
    """Register a new webhook."""
    webhook = req.dict()
    _webhook_configs.append(webhook)
    return {"id": len(_webhook_configs) - 1, **webhook, "secret": "***" if webhook["secret"] else ""}


@router.put("/webhooks/{webhook_id}")
async def update_webhook(webhook_id: int, req: WebhookUpdate):
    """Update an existing webhook."""
    if webhook_id < 0 or webhook_id >= len(_webhook_configs):
        raise HTTPException(404, "Webhook not found")
    for k, v in req.dict(exclude_none=True).items():
        _webhook_configs[webhook_id][k] = v
    return {"id": webhook_id, **_webhook_configs[webhook_id]}


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int):
    """Delete a webhook."""
    if webhook_id < 0 or webhook_id >= len(_webhook_configs):
        raise HTTPException(404, "Webhook not found")
    _webhook_configs.pop(webhook_id)
    return {"message": "Webhook deleted"}


@router.get("/notifications/config")
async def get_notification_config():
    """Get notification channel configuration (secrets masked)."""
    safe = dict(_notification_config)
    for key in ("email_password", "slack_webhook_url", "teams_webhook_url"):
        if safe.get(key):
            safe[key] = safe[key][:12] + "***"
    return safe


@router.put("/notifications/config")
async def update_notification_config(config: NotificationConfig):
    """Update notification channel configuration."""
    global _notification_config
    _notification_config = config.dict()
    return {"message": "Notification config updated"}


@router.post("/notifications/test")
async def test_notification(req: TestNotification):
    """Send a test notification to the specified channel."""
    event = NotificationEvent(
        event_type="test",
        project_name="Test Project",
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        severity="info",
        summary="This is a test notification from CodeScope.",
        details={"issues": 0, "vulnerabilities": 0, "quality_gate": "passed"},
    )

    if req.channel == "slack":
        url = _notification_config.get("slack_webhook_url", "")
        if not url:
            raise HTTPException(400, "Slack webhook URL not configured")
        ok = SlackNotifier(url).send(event)

    elif req.channel == "teams":
        url = _notification_config.get("teams_webhook_url", "")
        if not url:
            raise HTTPException(400, "Teams webhook URL not configured")
        ok = TeamsNotifier(url).send(event)

    elif req.channel == "email":
        cfg = _notification_config
        if not cfg.get("email_smtp_host"):
            raise HTTPException(400, "Email SMTP not configured")
        ok = EmailNotifier(
            cfg["email_smtp_host"], cfg.get("email_smtp_port", 587),
            cfg.get("email_username", ""), cfg.get("email_password", ""),
            cfg.get("email_from", ""), cfg.get("email_use_tls", True),
        ).send(event, cfg.get("email_to", []))

    elif req.channel == "webhook":
        if req.webhook_id is None or req.webhook_id >= len(_webhook_configs):
            raise HTTPException(400, "Invalid webhook ID")
        w = _webhook_configs[req.webhook_id]
        target = WebhookTarget(url=w["url"], secret=w.get("secret", ""), events=w.get("events", []))
        ok = WebhookDelivery(target).deliver(event)
    else:
        raise HTTPException(400, f"Unknown channel: {req.channel}")

    return {"success": ok, "channel": req.channel}


@router.get("/webhooks/events")
async def list_event_types():
    """List available webhook event types."""
    return [
        {"id": "scan_completed", "name": "Scan Completed", "description": "Fires when a code scan finishes"},
        {"id": "quality_gate_failed", "name": "Quality Gate Failed", "description": "Fires when quality gate check fails"},
        {"id": "new_vulnerability", "name": "New Vulnerability", "description": "Fires when new vulnerabilities are found"},
        {"id": "scan_failed", "name": "Scan Failed", "description": "Fires when a scan encounters an error"},
        {"id": "dependency_alert", "name": "Dependency Alert", "description": "Fires when vulnerable dependencies are detected"},
    ]
