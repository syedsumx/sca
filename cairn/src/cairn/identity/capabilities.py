"""What the signed-in person may do on the platform (``GET /v1/me/capabilities``), for role-aware navigation.

Each platform privilege (one not about a particular asset) is decided by the policy engine for the caller, now. A
privilege the engine cannot decide (an error, a timeout) is reported as not held: the UI then leaves the area out,
and the area's endpoints refuse anyway. Hiding is never the control; every endpoint still decides for itself.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# The platform privileges the API's areas check without a resource (the ones a navigation entry can depend on).
PLATFORM_PRIVILEGES = (
    "MANAGE_POLICIES",            # policies, personas, governance studio; Cairn's operators (is_admin)
    "MANAGE_GLOBAL_SETTINGS",     # workspace settings, budgets, limits, SIEM, residency, the AI gateway's models
    "MANAGE_SYSTEM_OPERATIONS",   # warehouses, maintenance, storage, external catalogs, chargeback
    "MANAGE_INGESTION",           # mirrors, document sources, SaaS and reverse syncs, pipes, remote agents
    "MANAGE_GLOSSARIES",
    "MANAGE_DATABASE",
    "MANAGE_ML_MODEL",
    "MANAGE_FINETUNE",
    "MANAGE_DATA_APP",
    "MANAGE_VOLUMES",
    "USE_AI_GATEWAY",
    "USE_AGENT",
    "RUN_JOBS",
)


def capabilities(authorize: Callable[..., bool], principal: Any, features: dict | None = None) -> dict:
    """``authorize(principal, privilege, None, raise_on_deny=False)`` for each platform privilege, fail closed."""
    held: dict[str, bool] = {}
    for privilege in PLATFORM_PRIVILEGES:
        try:
            held[privilege] = bool(authorize(principal, privilege, None, raise_on_deny=False))
        except Exception:  # noqa: BLE001 - fail closed: what cannot be decided is not held
            logger.warning("could not decide %s for %s", privilege, principal.urn, exc_info=True)
            held[privilege] = False
    return {"actor": principal.urn, "is_admin": held["MANAGE_POLICIES"], "privileges": held,
            "features": dict(features or {})}
