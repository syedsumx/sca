"""Security posture: Cairn checks its own configuration and state for the mistakes that expose data, and says how
to fix each one (the counterpart of Snowflake's Trust Center scanners).

``POST /governance/posture/scan`` (``MANAGE_SYSTEM_OPERATIONS``; also daily on the scheduler) runs every check and
keeps the result; ``GET /governance/posture`` returns the latest, ``…/history`` the scores over time. Each finding has
a ``check``, a ``severity`` (critical, high, medium, low), what was found (``evidence``) and how to fix it
(``fix``). The score starts at 100 and loses 25 per critical, 10 per high, 4 per medium and 1 per low finding.
A finding that is new since the previous scan, at high or critical, is sent to ``CAIRN_SECURITY_CONTACTS``
(people or groups, comma-separated; ``security.posture_finding``); every scan is audited (``posture.scanned``).

The checks:

* ``trusted_header``: ``CAIRN_DEV_TRUST_ACTOR_HEADER`` is on, so anyone who reaches the API can say who they are;
* ``authz_mode``: the policy decision point only adds to Core's own grants (``additive``), not authoritative;
* ``open_policy``: an allow policy for every action, or for reading data, with no condition and no subjects;
* ``pii_unmasked``: columns classified sensitive exist and no policy masks anything;
* ``audit_chain``: the audit log's hash chain does not verify (an entry changed or went missing);
* ``long_shares`` / ``idle_shares``: active shares to outside recipients that last over 180 days, or have not
  been used for 90;
* ``no_access_review``: grants exist and no access review closed in the last 180 days;
* ``residency_region``: namespaces are pinned to regions and the deployment's region (``CAIRN_REGION``) is unset;
* ``ai_budget``: a model is configured and there is no daily AI budget (``CAIRN_AI_DAILY_BUDGET_USD``);
* ``secrets_key``: secret scopes hold values and their key (``CAIRN_SECRETS_KEY``) has no rotation partner set
  while a previous key is still needed (``CAIRN_SECRETS_KEY_PREVIOUS`` lingering after a rotation finished).

A check that cannot run is reported as such (``status: error``), never as passed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, func, or_, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
WEIGHTS = {"critical": 25, "high": 10, "medium": 4, "low": 1}
DATA_ACTIONS = {"VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET", "EXPORT_DATASET", "DETOKENIZE"}

posture_scans = Table(
    "cairn_posture_scans", metadata,
    Column("id", String(32), primary_key=True),
    Column("ts_ms", BigInteger, nullable=False, index=True),
    Column("score", Integer, nullable=False),
    Column("findings", Text, nullable=False),          # JSON list
    Column("checks", Text, nullable=False),            # JSON {check: "ok" | "finding" | "error: …"}
)


@dataclass
class Finding:
    check: str
    severity: str
    title: str
    evidence: dict = field(default_factory=dict)
    fix: str = ""

    def as_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity, "title": self.title, "evidence": self.evidence,
                "fix": self.fix}


def _now() -> int:
    return int(time.time() * 1000)


class Posture:
    def __init__(self, engine, settings, pdp, audit=None, residency=None, sensitive_datasets: Optional[Callable[[], int]] = None,
                 llm_configured: bool = False, notify: Optional[Callable[[list[dict]], None]] = None, clock=_now):
        """``sensitive_datasets()``: how many datasets have columns classified sensitive (None: not known)."""
        self.engine, self.settings, self.pdp, self.audit = engine, settings, pdp, audit
        self.residency, self.sensitive_datasets, self.llm_configured = residency, sensitive_datasets, llm_configured
        self.notify, self.clock = notify, clock

    # ---- the checks ------------------------------------------------------------------------------
    def _bundle(self):
        b = self.pdp.bundle
        return b() if callable(b) else b

    def check_trusted_header(self) -> list[Finding]:
        if getattr(self.settings, "dev_trust_actor_header", False):
            return [Finding("trusted_header", "critical", "The API believes the X-Cairn-Actor header",
                            {"CAIRN_DEV_TRUST_ACTOR_HEADER": True},
                            "Unset CAIRN_DEV_TRUST_ACTOR_HEADER: it is for development only; people sign in through SSO.")]
        return []

    def check_authz_mode(self) -> list[Finding]:
        mode = getattr(self.pdp, "mode", None)
        if mode and mode != "authoritative":
            return [Finding("authz_mode", "medium", "Cairn's policies only add to Core's own permissions",
                            {"CAIRN_AUTHZ_MODE": mode},
                            "Set CAIRN_AUTHZ_MODE=authoritative once Cairn's policies cover every grant.")]
        return []

    def check_open_policy(self) -> list[Finding]:
        out = []
        for p in self._bundle().policies:
            if p.effect != "allow" or p.condition or (p.target and p.target.subjects):
                continue
            actions = set(p.actions)
            broad = not p.target or not p.target.resource_urns or any(u in ("*", "**") for u in p.target.resource_urns)
            if "*" in actions:
                out.append(Finding("open_policy", "critical", f"Policy {p.id} allows every action to everyone",
                                   {"policy": p.id, "actions": sorted(actions)},
                                   "Give it a condition or subjects (a group), or remove it."))
            elif actions & DATA_ACTIONS and broad:
                out.append(Finding("open_policy", "high", f"Policy {p.id} lets everyone {', '.join(sorted(actions & DATA_ACTIONS))} "
                                   "on every dataset", {"policy": p.id, "actions": sorted(actions)},
                                   "Narrow it to the datasets or groups it is meant for."))
        return out

    def check_pii_unmasked(self) -> list[Finding]:
        if self.sensitive_datasets is None:
            return []
        masks = [p for p in self._bundle().policies if p.effect == "obligation"
                 and any(o.type == "mask_columns" for o in p.obligations)]
        n = self.sensitive_datasets()
        if n and not masks:
            return [Finding("pii_unmasked", "high", f"{n} dataset(s) have sensitive columns and no policy masks anything",
                            {"datasets": n, "mask_policies": 0},
                            'Add a mask policy (obligation mask_columns, e.g. labels ["PII"]) for everyone who should not '
                            "see the values.")]
        return []

    def check_audit_chain(self) -> list[Finding]:
        if self.audit is None:
            return []
        r = self.audit.verify_chain()
        if not r.get("valid", False):
            return [Finding("audit_chain", "critical", "The audit log's hash chain does not verify",
                            {k: v for k, v in r.items() if k != "valid"},
                            "Investigate: an entry was changed or removed. Restore the audit table from a backup and "
                            "compare.")]
        return []

    def check_shares(self) -> list[Finding]:
        from cairn.dataaccess.shares import lakehouse_shares

        now = self.clock() // 1000
        out = []
        with self.engine.connect() as conn:
            rows = conn.execute(select(lakehouse_shares).where(lakehouse_shares.c.revoked_s.is_(None),
                                                               lakehouse_shares.c.expires_s > now)).all()
        long = [r.name for r in rows if r.expires_s - now > 180 * 86400]
        idle = [r.name for r in rows if (r.last_used_s or r.created_s) < now - 90 * 86400]
        if long:
            out.append(Finding("long_shares", "medium", f"{len(long)} share(s) to outside recipients last over 180 more days",
                               {"shares": sorted(long)[:50]}, "Shorten them, or review them: shares are renewed, not open-ended."))
        if idle:
            out.append(Finding("idle_shares", "low", f"{len(idle)} active share(s) unused for 90 days",
                               {"shares": sorted(idle)[:50]}, "Revoke the shares nobody reads."))
        return out

    def check_access_reviews(self) -> list[Finding]:
        from cairn.governance.access_reviews import reviews as access_reviews
        from cairn.governance.personas import grants_table

        since = self.clock() - 180 * DAY_MS
        with self.engine.connect() as conn:
            grants = conn.execute(select(func.count()).select_from(grants_table).where(
                grants_table.c.revoked.is_(False), or_(grants_table.c.expires_at_ms.is_(None),
                                                       grants_table.c.expires_at_ms > self.clock()))).scalar() or 0
            done = conn.execute(select(func.count()).select_from(access_reviews).where(
                access_reviews.c.closed_ms >= since)).scalar() or 0
        if grants and not done:
            return [Finding("no_access_review", "medium", f"{grants} active grant(s) and no access review in 180 days",
                            {"grants": grants}, "Start an access review (POST /governance/access-reviews).")]
        return []

    def check_residency(self) -> list[Finding]:
        r = self.residency
        if r is not None and r.active and not r.region:
            return [Finding("residency_region", "high", "Namespaces are pinned to regions and CAIRN_REGION is unset",
                            {"pins": len(r.pins)},
                            "Set CAIRN_REGION to the region this deployment runs in (pinned tables are unreadable until "
                            "then).")]
        return []

    def check_ai_budget(self) -> list[Finding]:
        if self.llm_configured and not os.environ.get("CAIRN_AI_DAILY_BUDGET_USD", "").strip():
            return [Finding("ai_budget", "low", "A model is configured and there is no daily AI budget", {},
                            "Set CAIRN_AI_DAILY_BUDGET_USD (and CAIRN_AI_USER_DAILY_TOKENS) to cap spend.")]
        return []

    def check_secrets_key(self) -> list[Finding]:
        from cairn.identity.secret_scopes import _key_id, _master, secrets_table

        try:
            current, previous = _master("CAIRN_SECRETS_KEY"), _master("CAIRN_SECRETS_KEY_PREVIOUS")
        except RuntimeError as e:
            return [Finding("secrets_key", "high", "The secret scopes' key is not usable", {"error": str(e)},
                            "Set CAIRN_SECRETS_KEY to 32 random bytes, base64.")]
        if current is None or previous is None:
            return []
        with self.engine.connect() as conn:
            old = conn.execute(select(func.count()).select_from(secrets_table).where(
                secrets_table.c.key_id == _key_id(previous))).scalar() or 0
        if old == 0:
            return [Finding("secrets_key", "low", "The previous secrets key is still set though nothing needs it", {},
                            "Remove CAIRN_SECRETS_KEY_PREVIOUS: the rotation is finished.")]
        return [Finding("secrets_key", "medium", f"{old} secret(s) are still under the previous key", {"secrets": old},
                        "Finish the rotation: POST /v1/secret-scopes/rotate.")]

    CHECKS = ("trusted_header", "authz_mode", "open_policy", "pii_unmasked", "audit_chain", "shares",
              "access_reviews", "residency", "ai_budget", "secrets_key")

    # ---- scans -----------------------------------------------------------------------------------------
    def scan(self, actor: str = "urn:li:corpuser:cairn-scheduler") -> dict:
        findings: list[Finding] = []
        checks: dict[str, str] = {}
        for name in self.CHECKS:
            try:
                got = getattr(self, f"check_{name}")()
                findings += got
                checks[name] = "finding" if got else "ok"
            except Exception as e:  # noqa: BLE001 - a check that cannot run is reported, never passed
                logger.warning("posture check %s failed: %s", name, e)
                checks[name] = f"error: {type(e).__name__}: {str(e)[:200]}"
        order = list(WEIGHTS)
        findings.sort(key=lambda f: (order.index(f.severity), f.check, f.title))
        score = max(0, 100 - sum(WEIGHTS[f.severity] for f in findings))
        previous = self.latest()
        seen = {(f["check"], f["title"]) for f in (previous or {}).get("findings", [])}
        new = [f.as_dict() for f in findings if f.severity in ("critical", "high") and (f.check, f.title) not in seen]
        row = {"id": uuid.uuid4().hex, "ts_ms": self.clock(), "score": score,
               "findings": json.dumps([f.as_dict() for f in findings]), "checks": json.dumps(checks)}
        with self.engine.begin() as conn:
            conn.execute(posture_scans.insert().values(**row))
            if self.audit is not None:
                self.audit.record("security", "posture.scanned", actor, None,
                                  {"score": score, "findings": len(findings), "new": [f["check"] for f in new]}, conn=conn)
        if new and self.notify is not None:
            try:
                self.notify(new)
            except Exception:  # noqa: BLE001 - the scan is kept whatever the notifier does
                logger.exception("could not send posture findings")
        return {**self._public(row), "new": new}

    @staticmethod
    def _public(r) -> dict:
        m = r if isinstance(r, dict) else dict(r._mapping)
        return {"id": m["id"], "ts_ms": m["ts_ms"], "score": m["score"], "findings": json.loads(m["findings"]),
                "checks": json.loads(m["checks"])}

    def latest(self) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(posture_scans).order_by(posture_scans.c.ts_ms.desc()).limit(1)).first()
        return self._public(r) if r else None

    def history(self, limit: int = 90) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(posture_scans.c.ts_ms, posture_scans.c.score, posture_scans.c.findings)
                                .order_by(posture_scans.c.ts_ms.desc()).limit(limit)).all()
        return [{"ts_ms": r.ts_ms, "score": r.score, "findings": len(json.loads(r.findings))} for r in rows]


def build_posture_router(posture: Posture, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException

    router = APIRouter(prefix="/governance/posture", tags=["security"])

    @router.get("")
    def latest(p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        out = posture.latest()
        if out is None:
            raise HTTPException(404, "no scan yet: POST /governance/posture/scan")
        return out

    @router.post("/scan")
    def scan(p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return posture.scan(p.urn)

    @router.get("/history")
    def history(p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return {"scans": posture.history()}

    return router
