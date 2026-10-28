"""No-code automations: "every dataset matching this condition gets this domain, owner, tag or term",
kept true as new datasets appear.

An automation is a selector (the policy condition language over dataset attributes), a schedule,
and actions. Each run writes only what is missing, as one undoable batch attributed to the
automation's own identity (``urn:li:corpuser:cairn-automation-<id>``), so audit and history show
which rule made each change.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, delete, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.governance.abac import evaluate_condition, validate_condition
from cairn.governance.bulk import ASPECTS, BulkError, _current

logger = logging.getLogger(__name__)

automations = Table(
    "cairn_automations", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("spec", JSONText, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    Column("last_run_ms", BigInteger, nullable=True),
)

automation_runs = Table(
    "cairn_automation_runs", metadata,
    Column("id", String(40), primary_key=True),
    Column("automation_id", String(40), nullable=False, index=True),
    Column("started_ms", BigInteger, nullable=False),
    Column("trigger", String(16), nullable=False),          # schedule | manual
    Column("matched", Integer, nullable=False),
    Column("changed", Integer, nullable=False),
    Column("batch_id", String(40), nullable=True),
    Column("state", String(16), nullable=False),            # APPLIED | NOOP | UNDONE | ERROR
    Column("detail", JSONText, nullable=True),
)

ActionType = Literal["add_tag", "add_term", "add_owner", "set_domain", "notify"]
_PREFIX = {"add_tag": "urn:li:tag:", "add_term": "urn:li:glossaryTerm:", "set_domain": "urn:li:domain:"}


class AutomationAction(BaseModel):
    type: ActionType
    value: Optional[str] = None            # tag, term, owner or domain; urn or short name
    recipients: list[str] = Field(default_factory=list)    # notify

    @model_validator(mode="after")
    def _check(self) -> "AutomationAction":
        if self.type == "notify":
            if not self.recipients:
                raise ValueError("notify needs recipients")
            self.recipients = [r if r.startswith("urn:li:") else f"urn:li:corpuser:{r}" for r in self.recipients]
            return self
        if not self.value:
            raise ValueError(f"{self.type} needs a value")
        v = self.value.strip()
        if self.type == "add_owner":
            self.value = v if v.startswith("urn:li:corp") else f"urn:li:corpuser:{v}"
        else:
            self.value = v if v.startswith("urn:li:") else _PREFIX[self.type] + v
        return self


class AutomationSpec(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    select: dict
    every_minutes: int = Field(default=60, ge=10, le=7 * 24 * 60)
    actions: list[AutomationAction] = Field(min_length=1)
    max_assets: int = Field(default=2000, ge=1, le=20000)

    @field_validator("select")
    @classmethod
    def _select(cls, v: dict) -> dict:
        validate_condition(v)
        return v

    @model_validator(mode="after")
    def _writes(self) -> "AutomationSpec":
        if len([a for a in self.actions if a.type == "set_domain"]) > 1:
            raise ValueError("one set_domain action at most")
        return self


class AutomationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def identity(automation_id: str) -> str:
    return f"urn:li:corpuser:cairn-automation-{automation_id}"


def _now() -> int:
    return int(time.time() * 1000)


def changes_for(spec: AutomationSpec, urn: str, aspects: dict) -> list[dict]:
    """The writes this automation still needs on one dataset (bulk change format), nothing if it
    already complies. Adds never remove what people set."""
    a = {name: aspects.get(name) or {} for name in ASPECTS + ("domains", "ownership", "globalTags", "glossaryTerms")}
    cur = _current(urn, "", a)
    out = []
    want = {"tags": [x.value for x in spec.actions if x.type == "add_tag"],
            "glossary_terms": [x.value for x in spec.actions if x.type == "add_term"],
            "owners": [x.value for x in spec.actions if x.type == "add_owner"]}
    for what, values in want.items():
        missing = [v for v in values if v not in cur[what]]
        if missing:
            to = sorted(dict.fromkeys(cur[what] + missing))
            out.append({"urn": urn, "field": "", "what": what, "from": sorted(cur[what]), "to": to,
                        "add": sorted(missing), "remove": []})
    domain = next((x.value for x in spec.actions if x.type == "set_domain"), None)
    if domain and cur["domain"] != domain:
        out.append({"urn": urn, "field": "", "what": "domain", "from": cur["domain"], "to": domain})
    return out


class AutomationService:
    def __init__(self, engine: Engine, bulk, datasets: Callable[[], list[tuple[str, dict, dict]]],
                 notify: Optional[Callable[[dict, list[str], list[str]], None]] = None, audit=None):
        """``bulk`` is the BulkService (it writes and undoes batches); ``notify(row, recipients, urns)``
        tells people about assets a run changed."""
        self.engine = engine
        self.bulk = bulk
        self.datasets = datasets
        self.notify = notify
        self.audit = audit

    # ---- definitions -------------------------------------------------------------------
    def create(self, spec: AutomationSpec, actor: str, enabled: bool = True) -> dict:
        aid = f"auto_{uuid.uuid4().hex[:10]}"
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(automations.insert().values(id=aid, name=spec.name, spec=spec.model_dump(mode="json"),
                                                     enabled=enabled, created_by=actor, created_ms=now, updated_ms=now))
        if self.audit is not None:
            self.audit.record("governance", "automation.created", actor, aid, {"name": spec.name})
        return self.get(aid)

    def update(self, aid: str, spec: AutomationSpec, actor: str, enabled: Optional[bool] = None) -> dict:
        self.get(aid)
        values: dict[str, Any] = {"name": spec.name, "spec": spec.model_dump(mode="json"), "updated_ms": _now()}
        if enabled is not None:
            values["enabled"] = enabled
        with self.engine.begin() as conn:
            conn.execute(update(automations).where(automations.c.id == aid).values(**values))
        if self.audit is not None:
            self.audit.record("governance", "automation.updated", actor, aid, {"name": spec.name, "enabled": enabled})
        return self.get(aid)

    def set_enabled(self, aid: str, enabled: bool, actor: str) -> dict:
        row = self.get(aid)
        return self.update(aid, AutomationSpec.model_validate(row["spec"]), actor, enabled)

    def delete(self, aid: str, actor: str) -> None:
        self.get(aid)
        with self.engine.begin() as conn:
            conn.execute(delete(automations).where(automations.c.id == aid))
        if self.audit is not None:
            self.audit.record("governance", "automation.deleted", actor, aid, {})

    def get(self, aid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(automations).where(automations.c.id == aid)).first()
        if r is None:
            raise AutomationError("automation not found", 404)
        return self._public(r)

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(automations).order_by(automations.c.created_ms)).all()
        return [self._public(r) for r in rows]

    def _public(self, r) -> dict:
        with self.engine.connect() as conn:
            last = conn.execute(select(automation_runs).where(automation_runs.c.automation_id == r.id)
                                .order_by(automation_runs.c.started_ms.desc()).limit(1)).first()
        return {"id": r.id, "name": r.name, "spec": r.spec, "enabled": r.enabled, "created_by": r.created_by,
                "created_ms": r.created_ms, "updated_ms": r.updated_ms, "identity": identity(r.id),
                "last_run": self._run(last) if last else None}

    @staticmethod
    def _run(r) -> dict:
        return {"id": r.id, "automation_id": r.automation_id, "started_ms": r.started_ms, "trigger": r.trigger,
                "matched": r.matched, "changed": r.changed, "batch_id": r.batch_id, "state": r.state,
                "detail": r.detail or {}}

    # ---- running -----------------------------------------------------------------------
    def preview(self, spec: AutomationSpec, sample: int = 50) -> dict:
        matched, changes = self._plan(spec)
        assets = sorted({c["urn"] for c in changes})
        return {"matched": len(matched), "would_change": len(assets), "changes": changes[:sample * 4],
                "sample_matched": matched[:sample], "truncated": len(matched) > spec.max_assets}

    def _plan(self, spec: AutomationSpec) -> tuple[list[str], list[dict]]:
        matched, changes = [], []
        for urn, resource, aspects in self.datasets():
            if not evaluate_condition(spec.select, {"resource": resource}):
                continue
            matched.append(urn)
            if len(matched) > spec.max_assets:
                break
            changes += changes_for(spec, urn, aspects)
        return matched[:spec.max_assets], changes

    def run(self, aid: str, trigger: str = "manual", now_ms: Optional[int] = None) -> dict:
        row = self.get(aid)
        spec = AutomationSpec.model_validate(row["spec"])
        rid, now = f"arun_{uuid.uuid4().hex[:12]}", now_ms or _now()
        try:
            matched, changes = self._plan(spec)
            batch = self.bulk.record(changes, identity(aid), "automation",
                                     {"automation": aid, "name": spec.name, "run": rid}) if changes else None
            changed = sorted({c["urn"] for c in changes})
            notified = []
            for action in spec.actions:
                if action.type == "notify" and changed and self.notify is not None:
                    self.notify(row, action.recipients, changed)
                    notified += action.recipients
            state = "APPLIED" if batch else "NOOP"
            detail = {"assets": changed[:200], "notified": sorted(set(notified))}
        except Exception as e:  # noqa: BLE001 - recorded on the run, the sweep goes on
            logger.exception("automation %s failed", aid)
            matched, changed, batch, state, detail = [], [], None, "ERROR", {"error": str(e)[:1000]}
        with self.engine.begin() as conn:
            conn.execute(automation_runs.insert().values(
                id=rid, automation_id=aid, started_ms=now, trigger=trigger, matched=len(matched),
                changed=len(changed), batch_id=batch["batch_id"] if batch else None, state=state, detail=detail))
            conn.execute(update(automations).where(automations.c.id == aid).values(last_run_ms=now))
        return self.get_run(rid)

    def get_run(self, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(automation_runs).where(automation_runs.c.id == rid)).first()
        if r is None:
            raise AutomationError("run not found", 404)
        return self._run(r)

    def runs(self, aid: str, limit: int = 50) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(automation_runs).where(automation_runs.c.automation_id == aid)
                                .order_by(automation_runs.c.started_ms.desc()).limit(limit)).all()
        return [self._run(r) for r in rows]

    def undo(self, rid: str, actor: str) -> dict:
        run = self.get_run(rid)
        if run["state"] != "APPLIED":
            raise AutomationError(f"run is {run['state'].lower()}", 409)
        try:
            out = self.bulk.undo(run["batch_id"], identity(run["automation_id"]))
        except BulkError as e:
            raise AutomationError(str(e), e.status)
        with self.engine.begin() as conn:
            conn.execute(update(automation_runs).where(automation_runs.c.id == rid).values(state="UNDONE"))
        if self.audit is not None:
            self.audit.record("governance", "automation.undone", actor, run["automation_id"], {"run": rid})
        return {**self.get_run(rid), "restored": out["restored"]}

    def sweep(self, now_ms: Optional[int] = None) -> dict[str, str]:
        """Run every enabled automation that is due."""
        now_ms = now_ms or _now()
        out = {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(automations).where(automations.c.enabled.is_(True))).all()
        for r in rows:
            every = int((r.spec or {}).get("every_minutes", 60)) * 60_000
            if r.last_run_ms is None or now_ms - r.last_run_ms >= every:
                out[r.id] = self.run(r.id, "schedule", now_ms)["state"]
        return out
