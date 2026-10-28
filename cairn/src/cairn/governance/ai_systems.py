"""AI systems: an inventory of the AI the organisation runs, its risk tier and its approval.

An AI system names its purpose, owner, models (catalog urns) and the datasets it uses. Its risk tier
comes from a risk assessment (``config/ai-governance/risk-assessment.yaml``; the default follows the
EU AI Act's categories): the worst tier any answer points to, ``minimal`` when none does.

States: DRAFT → IN_REVIEW → APPROVED or REJECTED; RETIRED at any time. Changing what a system is
(models, datasets, answers, purpose) puts an approved system back to DRAFT. Approval is a second
person's decision (MANAGE_POLICIES, never the owner) and is refused when:

* the tier is ``unacceptable`` (a prohibited practice);
* the assessment is incomplete (every question answered, and ``required_for`` texts for its tier);
* the system is high-risk and uses a dataset classified as restricted (``restricted_labels``) that
  its approved purpose does not cover.

The last rule is checked again on every read against the datasets' current classification, so a
dataset classified as restricted after approval shows up as a violation in the inventory report.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Column, String, Table, select, update

from cairn.common.db import JSONText, metadata

TIERS = ("unacceptable", "high", "limited", "minimal")          # worst first
STATES = ("DRAFT", "IN_REVIEW", "APPROVED", "REJECTED", "RETIRED")
DEFAULT_TEMPLATE = "/etc/cairn/config/ai-governance/risk-assessment.yaml"

ai_systems = Table(
    "cairn_ai_systems", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("owner", String(512), nullable=False, index=True),
    Column("state", String(16), nullable=False, index=True),
    Column("tier", String(16), nullable=True, index=True),
    Column("doc", JSONText, nullable=False),
    Column("decided_by", String(512), nullable=True),
    Column("decided_ms", BigInteger, nullable=True),
    Column("updated_ms", BigInteger, nullable=False),
)


class AISystemError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ---- the assessment template -------------------------------------------------------------
class Option(BaseModel):
    value: str
    label: str
    tier: Literal["unacceptable", "high", "limited", "minimal"]


class Question(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,63}$")
    title: str
    type: Literal["multi", "boolean", "text"]
    options: list[Option] = Field(default_factory=list)
    tier_if_yes: Optional[Literal["unacceptable", "high", "limited", "minimal"]] = None
    required_for: list[str] = Field(default_factory=list, description="text: required for these tiers")


class Template(BaseModel):
    name: str
    restricted_labels: list[str] = Field(default_factory=lambda: ["PII", "Confidential", "Restricted"])
    questions: list[Question] = Field(min_length=1)

    def tier(self, answers: dict[str, Any]) -> str:
        found = ["minimal"]
        for q in self.questions:
            a = answers.get(q.id)
            if q.type == "multi":
                found += [o.tier for o in q.options if o.value in (a or [])]
            elif q.type == "boolean" and a is True and q.tier_if_yes:
                found.append(q.tier_if_yes)
        return min(found, key=TIERS.index)

    def missing(self, answers: dict[str, Any], tier: str) -> list[str]:
        out = []
        for q in self.questions:
            a = answers.get(q.id)
            if q.type == "text":
                if tier in q.required_for and not (a or "").strip():
                    out.append(q.id)
            elif a is None:
                out.append(q.id)
        return out

    def check(self, answers: dict[str, Any]) -> None:
        for qid, a in answers.items():
            q = next((x for x in self.questions if x.id == qid), None)
            if q is None:
                raise AISystemError(f"no question {qid!r} in the assessment")
            if q.type == "multi":
                allowed = {o.value for o in q.options}
                if not isinstance(a, list) or not set(a) <= allowed:
                    raise AISystemError(f"{qid}: choose from {sorted(allowed)}")
            elif q.type == "boolean" and not isinstance(a, bool):
                raise AISystemError(f"{qid}: answer true or false")
            elif q.type == "text" and not isinstance(a, str):
                raise AISystemError(f"{qid}: answer with text")


def load_template(path: Optional[str] = None) -> Template:
    p = Path(path or DEFAULT_TEMPLATE)
    if not p.exists():
        p = Path(__file__).resolve().parents[3] / "config/ai-governance/risk-assessment.yaml"
    return Template.model_validate(yaml.safe_load(p.read_text()))


# ---- systems -------------------------------------------------------------------------------
class AISystemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=2000, description="what it is for, in plain words")
    owner: Optional[str] = None
    models: list[str] = Field(default_factory=list, description="catalog urns of the models it runs")
    datasets: list[str] = Field(default_factory=list, description="datasets it is trained on or reads")
    vendor: Optional[str] = Field(default=None, max_length=200, description="for bought-in systems")
    data_purpose_id: Optional[str] = Field(default=None, description="approved purpose for restricted data")
    answers: dict[str, Any] = Field(default_factory=dict)

    @field_validator("models", "datasets")
    @classmethod
    def _urns(cls, v: list[str]) -> list[str]:
        if bad := [u for u in v if not u.startswith("urn:li:")]:
            raise ValueError(f"not catalog urns: {bad}")
        return list(dict.fromkeys(v))


def _now() -> int:
    return int(time.time() * 1000)


def _covers(pattern: str, label: str) -> bool:
    return fnmatch.fnmatchcase(label, pattern) or label.startswith(pattern + ".")


class AISystemService:
    def __init__(self, engine, attributes, personas=None, audit=None, notify=None, template: Optional[Template] = None,
                 can_approve: Optional[Callable[[str], bool]] = None):
        self.engine = engine
        self.attributes = attributes
        self.personas = personas
        self.audit = audit
        self.notify = notify
        self.template = template or load_template()
        self.can_approve = can_approve or (lambda actor: False)

    # ---- reading ----------------------------------------------------------------------
    def _row(self, sid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(ai_systems).where(ai_systems.c.id == sid)).first()
        if r is None:
            raise AISystemError(f"no AI system {sid!r}", 404)
        return dict(r._mapping)

    def _public(self, r: dict) -> dict:
        doc = r["doc"]
        tier = self.template.tier(doc.get("answers") or {})
        return {"id": r["id"], "name": r["name"], "owner": r["owner"], "state": r["state"], "tier": tier,
                "purpose": doc.get("purpose"), "models": doc.get("models", []), "datasets": doc.get("datasets", []),
                "vendor": doc.get("vendor"), "data_purpose_id": doc.get("data_purpose_id"),
                "answers": doc.get("answers", {}), "missing": self.template.missing(doc.get("answers") or {}, tier),
                "violations": self.violations(doc, tier), "decided_by": r["decided_by"],
                "decided_ms": r["decided_ms"], "decision_note": doc.get("decision_note"), "updated_ms": r["updated_ms"]}

    def get(self, sid: str) -> dict:
        return self._public(self._row(sid))

    def list(self, state: Optional[str] = None) -> list[dict]:
        stmt = select(ai_systems).order_by(ai_systems.c.name)
        if state:
            stmt = stmt.where(ai_systems.c.state == state)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
        return [self._public(r) for r in rows]

    # ---- the purpose rule -------------------------------------------------------------
    def _restricted(self, labels: list[str]) -> list[str]:
        return sorted(lab for lab in labels if any(_covers(r, lab) for r in self.template.restricted_labels))

    def violations(self, doc: dict, tier: str) -> list[dict]:
        """Restricted datasets a high-risk system uses without an approved purpose covering them."""
        if tier not in ("high", "unacceptable"):
            return []
        purpose = None
        if doc.get("data_purpose_id") and self.personas is not None:
            purpose = self.personas.get_purpose(doc["data_purpose_id"])
            if purpose is not None and not purpose.enabled:
                purpose = None
        out = []
        for urn in doc.get("datasets", []):
            labels = self._restricted(list(self.attributes.resource(urn).get("classification") or []))
            # the most specific labels only: PII.Email, not also its parent PII
            leaves = [lab for lab in labels if not any(o.startswith(lab + ".") for o in labels)]
            uncovered = [lab for lab in leaves if not (purpose and any(_covers(t, lab) for t in purpose.tags))]
            if uncovered:
                out.append({"dataset": urn, "labels": uncovered,
                            "reason": "no approved purpose" if purpose is None else
                            f"purpose {purpose.name!r} does not cover {', '.join(uncovered)}"})
        return out

    # ---- writing ----------------------------------------------------------------------
    def create(self, body: AISystemIn, actor: str) -> dict:
        self.template.check(body.answers)
        sid = f"ais_{uuid.uuid4().hex[:12]}"
        doc = body.model_dump(exclude={"name", "owner"})
        with self.engine.begin() as conn:
            conn.execute(ai_systems.insert().values(
                id=sid, name=body.name, owner=body.owner or actor, state="DRAFT",
                tier=self.template.tier(body.answers), doc=doc, updated_ms=_now()))
        self._audit("ai_system.created", actor, sid, {"name": body.name})
        return self.get(sid)

    def update(self, sid: str, body: AISystemIn, actor: str) -> dict:
        r = self._row(sid)
        self._may_edit(r, actor)
        if r["state"] == "RETIRED":
            raise AISystemError("a retired system cannot be changed", 409)
        self.template.check(body.answers)
        doc = {**r["doc"], **body.model_dump(exclude={"name", "owner"})}
        material = any(doc.get(k) != r["doc"].get(k) for k in ("models", "datasets", "answers", "data_purpose_id"))
        state = "DRAFT" if material and r["state"] in ("APPROVED", "IN_REVIEW", "REJECTED") else r["state"]
        with self.engine.begin() as conn:
            conn.execute(update(ai_systems).where(ai_systems.c.id == sid).values(
                name=body.name, owner=body.owner or r["owner"], state=state, tier=self.template.tier(body.answers),
                doc=doc, updated_ms=_now()))
        self._audit("ai_system.updated", actor, sid, {"state": state, "material": material})
        return self.get(sid)

    def _may_edit(self, r: dict, actor: str) -> None:
        if actor != r["owner"] and not self.can_approve(actor):
            raise AISystemError("only the owner or an AI governance approver can change this system", 403)

    def submit(self, sid: str, actor: str) -> dict:
        r = self._row(sid)
        self._may_edit(r, actor)
        if r["state"] not in ("DRAFT", "REJECTED"):
            raise AISystemError(f"a system {r['state'].lower().replace('_', ' ')} cannot be submitted", 409)
        cur = self._public(r)
        if cur["missing"]:
            raise AISystemError(f"answer the assessment first: {', '.join(cur['missing'])}")
        self._set_state(sid, "IN_REVIEW")
        self._audit("ai_system.submitted", actor, sid, {"tier": cur["tier"]})
        self._notify("ai_system.review_requested", f"Review AI system {r['name']} ({cur['tier']} risk)",
                     cur["purpose"] or "", [])
        return self.get(sid)

    def decide(self, sid: str, approve: bool, actor: str, note: str = "") -> dict:
        r = self._row(sid)
        if not self.can_approve(actor):
            raise AISystemError("approving AI systems needs MANAGE_POLICIES", 403)
        if actor == r["owner"]:
            raise AISystemError("the owner cannot approve their own system", 403)
        if r["state"] != "IN_REVIEW":
            raise AISystemError("only systems in review can be decided", 409)
        cur = self._public(r)
        if approve:
            if cur["tier"] == "unacceptable":
                raise AISystemError("the assessment points to a prohibited practice: it cannot be approved")
            if cur["violations"]:
                raise AISystemError("high-risk systems need an approved purpose for restricted data: "
                                    + "; ".join(f"{v['dataset']}: {v['reason']}" for v in cur["violations"]))
        state = "APPROVED" if approve else "REJECTED"
        with self.engine.begin() as conn:
            conn.execute(update(ai_systems).where(ai_systems.c.id == sid).values(
                state=state, decided_by=actor, decided_ms=_now(), doc={**r["doc"], "decision_note": note},
                updated_ms=_now()))
        self._audit(f"ai_system.{state.lower()}", actor, sid, {"tier": cur["tier"], "note": note})
        self._notify("ai_system.decided", f"AI system {r['name']} {state.lower()}", note, [r["owner"]])
        return self.get(sid)

    def retire(self, sid: str, actor: str) -> dict:
        r = self._row(sid)
        self._may_edit(r, actor)
        self._set_state(sid, "RETIRED")
        self._audit("ai_system.retired", actor, sid, {})
        return self.get(sid)

    def _set_state(self, sid: str, state: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(ai_systems).where(ai_systems.c.id == sid).values(state=state, updated_ms=_now()))

    def _audit(self, action: str, actor: str, sid: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ai_governance", action, actor, sid, details)

    def _notify(self, etype: str, title: str, body: str, recipients: list[str]) -> None:
        if self.notify is None:
            return
        from cairn.notify.hub import Event

        try:
            self.notify.publish(Event(type=etype, title=title, body=body or title, recipients=recipients))
        except Exception:  # noqa: BLE001 - never block a decision on a notification
            pass

    # ---- the inventory ----------------------------------------------------------------
    def report(self) -> dict:
        systems = [s for s in self.list() if s["state"] != "RETIRED"]
        by = {"tier": {t: 0 for t in TIERS}, "state": {s: 0 for s in STATES if s != "RETIRED"}}
        for s in systems:
            by["tier"][s["tier"]] += 1
            by["state"][s["state"]] += 1
        return {
            "assessment": self.template.name, "total": len(systems), "by_tier": by["tier"], "by_state": by["state"],
            "high_risk_not_approved": [s["id"] for s in systems if s["tier"] in ("high", "unacceptable")
                                       and s["state"] != "APPROVED"],
            "violations": [{"system": s["id"], "name": s["name"], "state": s["state"], **v}
                           for s in systems for v in s["violations"]],
            "systems": [{k: s[k] for k in ("id", "name", "owner", "tier", "state", "purpose", "vendor")}
                        | {"datasets": len(s["datasets"]), "models": len(s["models"]),
                           "violations": len(s["violations"])} for s in systems],
        }

    def report_csv(self) -> str:
        rows = self.report()["systems"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["id", "name", "owner", "tier", "state", "purpose", "vendor", "models",
                                            "datasets", "violations"])
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()
