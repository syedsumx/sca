"""Cairn Desktop's workspace settings: what an administrator lets the desktop app do with this Cairn.

Cairn Desktop (``cairn/desktop``) reads ``GET /v1/desktop/policy`` with the person's own session each time it
connects. Administrators (``MANAGE_GLOBAL_SETTINGS``) change it with ``PUT /admin/desktop/policy``; each change is
audited. Settings (all optional; the defaults are shown):

- ``offline_cache`` (true): may the app keep the dashboards and reports a person viewed, encrypted on their computer,
  to read them while this Cairn cannot be reached. False: nothing is kept, and what was kept is deleted when the app
  next connects.
- ``offline_cache_pages`` (20, 1-100) and ``offline_cache_max_age_days`` (14, 1-90): how many pages, and for how long.
- ``updates`` ("auto"): ``auto`` installs updates, ``notify`` only says one is out, ``off`` never checks.
- ``pinned_version`` (none): the app does not update past this version (``1.4.2``).
- ``update_feed_url`` (none): an https feed (electron-updater's generic provider) to update from instead of GitHub
  Releases, for self-hosting.
- ``sign_in_origins`` ([]): https origins of the identity provider that the app's window may show during sign-in
  (single sign-on redirects there are followed anyway; these are for links into it, such as "forgot password").

Stored as one row of ``cairn_agent_settings`` (a key-value table, key ``desktop``): no migration.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request

SETTINGS_KEY = "desktop"
UPDATES = ("auto", "notify", "off")
DEFAULTS: dict[str, Any] = {"offline_cache": True, "offline_cache_pages": 20, "offline_cache_max_age_days": 14,
                            "updates": "auto", "pinned_version": None, "update_feed_url": None,
                            "sign_in_origins": []}
VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")


class DesktopPolicyError(ValueError):
    pass


def _origin(value: str, what: str) -> str:
    parts = urlsplit(str(value or "").strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise DesktopPolicyError(f"{what}: an https address without a user name or password")
    return f"https://{parts.netloc.lower()}"


def validate(body: Any) -> dict:
    """The settings from a request body, checked; unknown keys are refused."""
    if not isinstance(body, dict):
        raise DesktopPolicyError("send a JSON object")
    unknown = sorted(set(body) - set(DEFAULTS) - {"changed_by", "changed_ms"})
    if unknown:
        raise DesktopPolicyError(f"unknown settings: {', '.join(unknown)}")
    out = dict(DEFAULTS)
    if "offline_cache" in body:
        if not isinstance(body["offline_cache"], bool):
            raise DesktopPolicyError("offline_cache: true or false")
        out["offline_cache"] = body["offline_cache"]
    for key, low, high in (("offline_cache_pages", 1, 100), ("offline_cache_max_age_days", 1, 90)):
        if key in body:
            v = body[key]
            if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
                raise DesktopPolicyError(f"{key}: a whole number from {low} to {high}")
            out[key] = v
    if "updates" in body:
        if body["updates"] not in UPDATES:
            raise DesktopPolicyError("updates: auto, notify or off")
        out["updates"] = body["updates"]
    if body.get("pinned_version") is not None:
        v = str(body["pinned_version"]).strip().removeprefix("v")
        if not VERSION.match(v):
            raise DesktopPolicyError("pinned_version: a version such as 1.4.2")
        out["pinned_version"] = v
    if body.get("update_feed_url") is not None:
        url = str(body["update_feed_url"]).strip()
        _origin(url, "update_feed_url")
        out["update_feed_url"] = url.rstrip("/")
    if body.get("sign_in_origins") is not None:
        origins = body["sign_in_origins"]
        if not isinstance(origins, list) or len(origins) > 20:
            raise DesktopPolicyError("sign_in_origins: a list of at most 20 https origins")
        out["sign_in_origins"] = sorted({_origin(o, "sign_in_origins") for o in origins})
    return out


class DesktopPolicy:
    """The desktop app's settings for this Cairn (one row of ``cairn_agent_settings``)."""

    def __init__(self, engine, audit=None):
        self.engine, self.audit = engine, audit

    def get(self) -> dict:
        from sqlalchemy import select

        from cairn.identity.agents import agent_settings

        with self.engine.connect() as conn:
            v = conn.execute(select(agent_settings.c.value).where(agent_settings.c.key == SETTINGS_KEY)).scalar()
        stored = v if isinstance(v, dict) else {}
        out = {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}
        out["changed_by"], out["changed_ms"] = stored.get("changed_by"), stored.get("changed_ms")
        return out

    def put(self, actor: str, body: Any) -> dict:
        from sqlalchemy import update

        from cairn.identity.agents import agent_settings

        value = {**validate(body), "changed_by": actor, "changed_ms": int(time.time() * 1000)}
        with self.engine.begin() as conn:
            q = update(agent_settings).where(agent_settings.c.key == SETTINGS_KEY)
            if conn.execute(q.values(value=value)).rowcount == 0:
                conn.execute(agent_settings.insert().values(key=SETTINGS_KEY, value=value))
            if self.audit is not None:
                self.audit.record("identity", "desktop.policy_changed", actor, "desktop",
                                  {k: value[k] for k in DEFAULTS}, conn=conn)
        return self.get()


def build_router(policy: DesktopPolicy, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException

    router = APIRouter(tags=["identity"])

    @router.get("/v1/desktop/policy")
    def desktop_policy(p=Depends(current_principal)):
        """What Cairn Desktop may do with this Cairn (any signed-in person; nothing in it is secret)."""
        return policy.get()

    @router.get("/admin/desktop/policy")
    def admin_desktop_policy(p=Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return policy.get()

    @router.put("/admin/desktop/policy")
    async def put_desktop_policy(request: Request, p=Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON")
        try:
            return policy.put(p.urn, body)
        except DesktopPolicyError as e:
            raise HTTPException(422, str(e))

    return router

