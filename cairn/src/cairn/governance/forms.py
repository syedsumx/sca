"""Compliance forms: required metadata assigned to sets of assets, completed by their owners.

A form has questions (each maps to catalog metadata: description, owners, glossary terms, tags,
domain, a structured property, or a free-text / yes-no answer kept in Cairn), a selector (the
policy condition language over dataset attributes, e.g. every dataset classified PII in the
Finance domain) and an assignee rule (owners of the asset, optionally only some ownership types,
else named people). Assigning a form creates one task per asset; submitting a task writes the
answers to the catalog as the submitting user and marks it completed. A verification form adds a
sign-off: the task is VERIFIED once completed and confirmed.

An asset counts as compliant only when every form assigned to it is completed (and verified where
the form asks for it).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Column, String, Table, and_, func, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.common.proposals import audit_stamp, make_mcp
from cairn.governance.abac import evaluate_condition, validate_condition

forms_table = Table(
    "cairn_forms", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("spec", JSONText, nullable=False),
    Column("state", String(16), nullable=False),            # ACTIVE | ARCHIVED
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

form_tasks = Table(
    "cairn_form_tasks", metadata,
    Column("form_id", String(40), primary_key=True),
    Column("urn", String(512), primary_key=True),
    Column("assignees", JSONText, nullable=False),          # actor urns who may complete it
    Column("state", String(16), nullable=False, index=True),  # PENDING | COMPLETED | VERIFIED
    Column("answers", JSONText, nullable=True),
    Column("completed_by", String(512), nullable=True),
    Column("completed_ms", BigInteger, nullable=True),
    Column("verified_by", String(512), nullable=True),
    Column("verified_ms", BigInteger, nullable=True),
    Column("domain", String(512), nullable=True),
    Column("assigned_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

QuestionType = Literal["description", "owners", "glossary_terms", "tags", "domain", "structured_property",
                       "text", "yes_no"]
OWNERSHIP_TYPES = {"TECHNICAL_OWNER", "BUSINESS_OWNER", "DATA_STEWARD", "DATAOWNER", "PRODUCER", "DEVELOPER"}


class FormError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Question(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,62}$")
    type: QuestionType
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    required: bool = True
    allowed: list[str] = Field(default_factory=list, description="choices: term/tag/domain urns or property values")
    multiple: bool = True
    property_urn: Optional[str] = Field(default=None, description="structured_property questions")
    ownership_type: str = "DATAOWNER"

    @model_validator(mode="after")
    def _consistent(self) -> "Question":
        if self.type == "structured_property" and not (self.property_urn or "").startswith(
                "urn:li:structuredProperty:"):
            raise ValueError(f"question {self.id}: structured_property needs property_urn")
        if self.type == "owners" and self.ownership_type not in OWNERSHIP_TYPES:
            raise ValueError(f"question {self.id}: ownership_type must be one of {sorted(OWNERSHIP_TYPES)}")
        if self.type == "domain":
            self.multiple = False
        return self


class Assignees(BaseModel):
    owners: bool = True
    ownership_types: list[str] = Field(default_factory=list, description="empty = every owner")
    users: list[str] = Field(default_factory=list, description="fallback when an asset has no matching owner")


class FormSpec(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    kind: Literal["completion", "verification"] = "completion"
    questions: list[Question] = Field(min_length=1, max_length=50)
    select: dict = Field(description="policy condition over resource attributes")
    assignees: Assignees = Field(default_factory=Assignees)
    due_days: Optional[int] = Field(default=None, ge=1, le=365)
    max_assets: int = Field(default=5000, ge=1, le=100_000)

    @field_validator("select")
    @classmethod
    def _valid_select(cls, v: dict) -> dict:
        validate_condition(v)
        return v

    @model_validator(mode="after")
    def _unique_ids(self) -> "FormSpec":
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return self


def _now() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- answers ↔ metadata
def current_answer(q: Question, aspects: dict) -> Any:
    """What the catalog already says, so assignees confirm rather than retype."""
    if q.type == "description":
        return ((aspects.get("editableDatasetProperties") or {}).get("description")
                or (aspects.get("datasetProperties") or {}).get("description") or None)
    if q.type == "owners":
        return [o["owner"] for o in (aspects.get("ownership") or {}).get("owners", []) if o.get("owner")] or None
    if q.type == "glossary_terms":
        found = [t["urn"] for t in (aspects.get("glossaryTerms") or {}).get("terms", []) if t.get("urn")]
        return [t for t in found if not q.allowed or t in q.allowed] or None
    if q.type == "tags":
        found = [t["tag"] for t in (aspects.get("globalTags") or {}).get("tags", []) if t.get("tag")]
        return [t for t in found if not q.allowed or t in q.allowed] or None
    if q.type == "domain":
        return ((aspects.get("domains") or {}).get("domains") or [None])[0]
    if q.type == "structured_property":
        for p in (aspects.get("structuredProperties") or {}).get("properties", []):
            if p.get("propertyUrn") == q.property_urn:
                vals = [next(iter(v.values())) for v in p.get("values", []) if v]
                return vals if q.multiple else (vals[0] if vals else None)
    return None


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == []


def validate_answers(spec: FormSpec, answers: dict) -> dict:
    out: dict[str, Any] = {}
    for q in spec.questions:
        value = answers.get(q.id)
        if _blank(value):
            if q.required:
                raise FormError(f"{q.title}: an answer is required")
            continue
        if q.type in ("description", "text"):
            if not isinstance(value, str) or len(value) > 20_000:
                raise FormError(f"{q.title}: expected text")
            value = value.strip()
        elif q.type == "yes_no":
            if not isinstance(value, bool):
                raise FormError(f"{q.title}: expected yes or no")
        elif q.type == "domain":
            if not isinstance(value, str) or not value.startswith("urn:li:domain:"):
                raise FormError(f"{q.title}: expected a domain")
        else:
            values = value if isinstance(value, list) else [value]
            if not q.multiple and len(values) > 1:
                raise FormError(f"{q.title}: pick one")
            prefix = {"owners": "urn:li:corp", "glossary_terms": "urn:li:glossaryTerm:",
                      "tags": "urn:li:tag:"}.get(q.type)
            for v in values:
                if not isinstance(v, str) or (prefix and not v.startswith(prefix)):
                    raise FormError(f"{q.title}: {v!r} is not a valid choice")
            value = values
        if q.allowed and q.type not in ("description", "text", "yes_no", "owners"):
            bad = [v for v in (value if isinstance(value, list) else [value]) if v not in q.allowed]
            if bad:
                raise FormError(f"{q.title}: {bad} not among the allowed choices")
        out[q.id] = value
    return out


def answer_mcps(urn: str, spec: FormSpec, answers: dict, actor: str, get: Callable[[str, str], Optional[dict]],
                form_id: str) -> list[dict]:
    """Catalog writes for submitted answers; existing values are kept unless the question replaces them."""
    stamp = audit_stamp(actor)
    props = {"cairn_source": "compliance-form", "cairn_form": form_id, "cairn_submitted_by": actor}
    staged: dict[str, dict] = {}

    def aspect(name: str) -> dict:
        if name not in staged:
            staged[name] = dict(get(urn, name) or {})
        return staged[name]

    for q in spec.questions:
        if q.id not in answers:
            continue
        value = answers[q.id]
        if q.type == "description":
            a = aspect("editableDatasetProperties")
            a["description"] = value
            a["lastModified"] = stamp
            a.setdefault("created", stamp)
        elif q.type == "owners":
            a = aspect("ownership")
            have = {(o.get("owner"), o.get("type")) for o in a.get("owners", [])}
            owners = list(a.get("owners", []))
            for o in value:
                if (o, q.ownership_type) not in have:
                    owners.append({"owner": o, "type": q.ownership_type})
            a["owners"] = owners
            a["lastModified"] = stamp
        elif q.type == "glossary_terms":
            a = aspect("glossaryTerms")
            have = {t.get("urn") for t in a.get("terms", [])}
            a["terms"] = list(a.get("terms", [])) + [{"urn": t} for t in value if t not in have]
            a["auditStamp"] = stamp
        elif q.type == "tags":
            a = aspect("globalTags")
            have = {t.get("tag") for t in a.get("tags", [])}
            a["tags"] = list(a.get("tags", [])) + [{"tag": t} for t in value if t not in have]
        elif q.type == "domain":
            staged["domains"] = {"domains": [value]}
        elif q.type == "structured_property":
            a = aspect("structuredProperties")
            rest = [p for p in a.get("properties", []) if p.get("propertyUrn") != q.property_urn]
            vals = value if isinstance(value, list) else [value]
            typed = [{"double": v} if isinstance(v, (int, float)) and not isinstance(v, bool) else {"string": str(v)}
                     for v in vals]
            a["properties"] = rest + [{"propertyUrn": q.property_urn, "values": typed}]
    return [make_mcp(urn, name, value, system_properties=props) for name, value in staged.items()]


# --------------------------------------------------------------------------- service
class FormService:
    def __init__(self, engine: Engine, graph=None, audit=None, notify: Optional[Callable[..., None]] = None,
                 datasets: Optional[Callable[[], list[tuple[str, dict, dict]]]] = None):
        self.engine = engine
        self.graph = graph
        self.audit = audit
        self.notify = notify          # (form_row, recipients, urns) → None
        self._datasets = datasets

    # ---- forms ---------------------------------------------------------------------------
    def create(self, spec: FormSpec, actor: str) -> dict:
        fid = f"form_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(forms_table.insert().values(id=fid, name=spec.name, spec=spec.model_dump(), state="ACTIVE",
                                                     created_by=actor, created_ms=now, updated_ms=now))
        self._audit("form.created", actor, fid, {"name": spec.name, "questions": len(spec.questions)})
        return self.get(fid)

    def update(self, form_id: str, spec: FormSpec, actor: str) -> dict:
        self._row(form_id)
        with self.engine.begin() as conn:
            conn.execute(update(forms_table).where(forms_table.c.id == form_id).values(
                name=spec.name, spec=spec.model_dump(), updated_ms=_now()))
        self._audit("form.updated", actor, form_id, {})
        return self.get(form_id)

    def archive(self, form_id: str, actor: str) -> None:
        self._row(form_id)
        with self.engine.begin() as conn:
            conn.execute(update(forms_table).where(forms_table.c.id == form_id).values(state="ARCHIVED",
                                                                                         updated_ms=_now()))
        self._audit("form.archived", actor, form_id, {})

    def _row(self, form_id: str):
        with self.engine.connect() as conn:
            row = conn.execute(select(forms_table).where(forms_table.c.id == form_id)).first()
        if row is None:
            raise FormError("form not found", 404)
        return row

    def spec(self, form_id: str) -> FormSpec:
        return FormSpec.model_validate(self._row(form_id).spec)

    def get(self, form_id: str) -> dict:
        r = self._row(form_id)
        return {"id": r.id, "name": r.name, "state": r.state, "created_by": r.created_by,
                "created_ms": r.created_ms, "spec": r.spec, "progress": self.progress(form_id)["total"]}

    def list(self, include_archived: bool = False) -> list[dict]:
        with self.engine.connect() as conn:
            q = select(forms_table).order_by(forms_table.c.created_ms.desc())
            if not include_archived:
                q = q.where(forms_table.c.state == "ACTIVE")
            rows = conn.execute(q).all()
        return [self.get(r.id) for r in rows]

    # ---- assignment ----------------------------------------------------------------------
    def _assignees(self, spec: FormSpec, aspects: dict) -> list[str]:
        out: list[str] = []
        if spec.assignees.owners:
            for o in (aspects.get("ownership") or {}).get("owners", []):
                kind = (o.get("type") or "").upper()
                type_urn = o.get("typeUrn") or ""
                if not spec.assignees.ownership_types or kind in spec.assignees.ownership_types or any(
                        type_urn.endswith(t.lower()) for t in spec.assignees.ownership_types):
                    if o.get("owner"):
                        out.append(o["owner"])
        return list(dict.fromkeys(out or spec.assignees.users))

    def assign(self, form_id: str, actor: str, dry_run: bool = False) -> dict:
        """Create tasks for every dataset the selector matches now; tasks for assets that no longer
        match and were never started are withdrawn. Re-running is safe."""
        row = self._row(form_id)
        if row.state != "ACTIVE":
            raise FormError("form is archived", 409)
        spec = FormSpec.model_validate(row.spec)
        if self._datasets is None:
            raise FormError("catalog access is not configured", 503)
        matched = [(u, r, a) for u, r, a in self._datasets() if evaluate_condition(spec.select, {"resource": r})]
        matched = sorted(matched, key=lambda m: m[0])[: spec.max_assets]
        now = _now()
        with self.engine.connect() as conn:
            existing = {r.urn: r for r in conn.execute(select(form_tasks).where(form_tasks.c.form_id == form_id))}
        new, refreshed, unassigned_assets = [], [], []
        want = {}
        for urn, resource, aspects in matched:
            people = self._assignees(spec, aspects)
            if not people:
                unassigned_assets.append(urn)
            want[urn] = (people, (resource.get("domains") or [None])[0])
        withdrawn = [u for u, r in existing.items() if u not in want and r.state == "PENDING"]
        if not dry_run:
            with self.engine.begin() as conn:
                for urn, (people, domain) in want.items():
                    if urn in existing:
                        if existing[urn].state == "PENDING" and sorted(existing[urn].assignees) != sorted(people):
                            conn.execute(update(form_tasks).where(and_(form_tasks.c.form_id == form_id,
                                                                       form_tasks.c.urn == urn)).values(
                                assignees=people, domain=domain, updated_ms=now))
                            refreshed.append(urn)
                        continue
                    conn.execute(form_tasks.insert().values(
                        form_id=form_id, urn=urn, assignees=people, state="PENDING", domain=domain,
                        assigned_ms=now, updated_ms=now))
                    new.append(urn)
                for urn in withdrawn:
                    conn.execute(form_tasks.delete().where(and_(form_tasks.c.form_id == form_id,
                                                                form_tasks.c.urn == urn)))
            if new and self.notify is not None:
                by_person: dict[str, list[str]] = {}
                for urn in new:
                    for person in want[urn][0]:
                        by_person.setdefault(person, []).append(urn)
                for person, urns in by_person.items():
                    self.notify(row, [person], urns)
            self._audit("form.assigned", actor, form_id, {"new": len(new), "withdrawn": len(withdrawn)})
        else:
            new = [u for u in want if u not in existing]
        return {"form_id": form_id, "matched": len(want), "assigned": new, "reassigned": refreshed,
                "withdrawn": withdrawn, "without_assignee": unassigned_assets, "dry_run": dry_run}

    # ---- tasks ---------------------------------------------------------------------------
    def tasks_for(self, actor: str, groups: Optional[list[str]] = None, state: str = "PENDING") -> list[dict]:
        me = {actor, *(groups or [])}
        with self.engine.connect() as conn:
            rows = conn.execute(select(form_tasks, forms_table.c.name, forms_table.c.spec)
                                .join(forms_table, forms_table.c.id == form_tasks.c.form_id)
                                .where(and_(form_tasks.c.state == state, forms_table.c.state == "ACTIVE"))
                                .order_by(form_tasks.c.assigned_ms)).all()
        out = []
        for r in rows:
            if not me & set(r.assignees or []):
                continue
            due = r.spec.get("due_days")
            out.append({"form_id": r.form_id, "form_name": r.name, "urn": r.urn, "state": r.state,
                        "kind": r.spec.get("kind", "completion"), "assigned_ms": r.assigned_ms,
                        "due_ms": r.assigned_ms + due * 86_400_000 if due else None})
        return out

    def task(self, form_id: str, urn: str) -> dict:
        spec = self.spec(form_id)
        with self.engine.connect() as conn:
            r = conn.execute(select(form_tasks).where(and_(form_tasks.c.form_id == form_id,
                                                           form_tasks.c.urn == urn))).first()
        if r is None:
            raise FormError("this asset has no task for this form", 404)
        aspects = {}
        if self.graph is not None:
            for name in ("editableDatasetProperties", "datasetProperties", "ownership", "glossaryTerms",
                         "globalTags", "domains", "structuredProperties"):
                aspects[name] = self.graph.get_aspect(urn, name)
        prefill = {q.id: current_answer(q, aspects) for q in spec.questions}
        return {"form_id": form_id, "urn": urn, "state": r.state, "assignees": r.assignees, "kind": spec.kind,
                "questions": [q.model_dump() for q in spec.questions], "form_name": spec.name,
                "answers": r.answers or {k: v for k, v in prefill.items() if not _blank(v)},
                "completed_by": r.completed_by, "verified_by": r.verified_by}

    def submit(self, form_id: str, urn: str, answers: dict, actor: str, verify: bool = False,
               may_act: Optional[Callable[[str], bool]] = None) -> dict:
        """Complete (and for verification forms, optionally sign off) one asset's task as ``actor``.
        ``may_act(urn)`` lets non-assignees with edit rights (stewards) complete it too."""
        spec = self.spec(form_id)
        with self.engine.connect() as conn:
            r = conn.execute(select(form_tasks).where(and_(form_tasks.c.form_id == form_id,
                                                           form_tasks.c.urn == urn))).first()
        if r is None:
            raise FormError("this asset has no task for this form", 404)
        if actor not in (r.assignees or []) and not (may_act and may_act(urn)):
            raise FormError("this task is assigned to someone else", 403)
        if r.state == "VERIFIED" or (r.state == "COMPLETED" and spec.kind == "completion" and not answers):
            raise FormError(f"already {r.state.lower()}", 409)
        clean = validate_answers(spec, answers)
        if self.graph is not None:
            mcps = answer_mcps(urn, spec, clean, actor, self.graph.get_aspect, form_id)
            if mcps:
                self.graph.emit(mcps)
        now = _now()
        state = "VERIFIED" if spec.kind == "verification" and verify else "COMPLETED"
        values: dict[str, Any] = dict(state=state, answers=clean, completed_by=actor, completed_ms=now,
                                      updated_ms=now)
        if state == "VERIFIED":
            values.update(verified_by=actor, verified_ms=now)
        with self.engine.begin() as conn:
            conn.execute(update(form_tasks).where(and_(form_tasks.c.form_id == form_id, form_tasks.c.urn == urn))
                         .values(**values))
        self._audit(f"form.{state.lower()}", actor, urn, {"form_id": form_id, "answered": sorted(clean)})
        return {"form_id": form_id, "urn": urn, "state": state}

    # ---- status and progress -------------------------------------------------------------
    def asset_status(self, urn: str) -> dict:
        """COMPLIANT when every assigned form is done (verified where required); else what is open."""
        return self.asset_statuses([urn])[urn]

    def asset_statuses(self, urns: list[str]) -> dict[str, dict]:
        """:meth:`asset_status` for many assets in one query."""
        out: dict[str, dict] = {u: {"urn": u, "status": None, "forms": []} for u in urns}
        if not urns:
            return out
        with self.engine.connect() as conn:
            rows = conn.execute(select(form_tasks, forms_table.c.name, forms_table.c.spec)
                                .join(forms_table, forms_table.c.id == form_tasks.c.form_id)
                                .where(and_(form_tasks.c.urn.in_(urns), forms_table.c.state == "ACTIVE"))).all()
        for r in rows:
            need = "VERIFIED" if r.spec.get("kind") == "verification" else "COMPLETED"
            ok = r.state == "VERIFIED" or (need == "COMPLETED" and r.state == "COMPLETED")
            out[r.urn]["forms"].append({"form_id": r.form_id, "form_name": r.name, "state": r.state, "done": ok})
        for st in out.values():
            if st["forms"]:
                st["status"] = "COMPLIANT" if all(f["done"] for f in st["forms"]) else "INCOMPLETE"
        return out

    def progress(self, form_id: str) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(select(form_tasks.c.state, form_tasks.c.domain, form_tasks.c.assignees)
                                .where(form_tasks.c.form_id == form_id)).all()
        def tally(items) -> dict:
            t = {"assets": 0, "PENDING": 0, "COMPLETED": 0, "VERIFIED": 0}
            for state in items:
                t["assets"] += 1
                t[state] += 1
            t["done_pct"] = round(100 * (t["COMPLETED"] + t["VERIFIED"]) / t["assets"], 1) if t["assets"] else 0.0
            return t
        by_domain: dict[str, list[str]] = {}
        by_assignee: dict[str, list[str]] = {}
        for r in rows:
            by_domain.setdefault(r.domain or "(no domain)", []).append(r.state)
            for a in r.assignees or ["(unassigned)"]:
                by_assignee.setdefault(a, []).append(r.state)
        return {"form_id": form_id, "total": tally([r.state for r in rows]),
                "by_domain": {k: tally(v) for k, v in sorted(by_domain.items())},
                "by_assignee": {k: tally(v) for k, v in sorted(by_assignee.items())}}

    def overdue(self, now_ms: Optional[int] = None) -> list[dict]:
        """Pending tasks past their form's due date (for reminders)."""
        now = now_ms or _now()
        with self.engine.connect() as conn:
            rows = conn.execute(select(form_tasks, forms_table.c.name, forms_table.c.spec)
                                .join(forms_table, forms_table.c.id == form_tasks.c.form_id)
                                .where(and_(form_tasks.c.state == "PENDING", forms_table.c.state == "ACTIVE"))).all()
        return [{"form_id": r.form_id, "form_name": r.name, "urn": r.urn, "assignees": r.assignees}
                for r in rows if r.spec.get("due_days") and r.assigned_ms + r.spec["due_days"] * 86_400_000 < now]

    def counts(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(form_tasks.c.state, func.count()).group_by(form_tasks.c.state)).all()
        return {s: int(n) for s, n in rows}

    def _audit(self, action: str, actor: str, subject: Optional[str], details: dict) -> None:
        if self.audit is not None:
            self.audit.record("governance", action, actor, subject, details)
