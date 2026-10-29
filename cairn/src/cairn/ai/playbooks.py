"""Agent playbooks: multi-step jobs Cairn runs when something happens, each step recorded.

Built in (off until an administrator turns them on):

* ``incident_triage`` (quality incident raised): likely root causes upstream, the people to contact,
  what is affected downstream; posted as a discussion on the dataset and sent to its owners.
* ``change_impact`` (schema changed): downstream assets affected, certified ones first; their owners
  are told which columns were added or removed.
* ``access_review`` (access requested): a recommendation for the approvers, with its reasons
  (auto-approvable, sensitive data in the product, the justification, the duration).
* ``dataset_onboarding`` (dataset appears): AI suggestions (owners, classification, description)
  queued for review, the default quality monitors created, and the owners told.

Playbooks only read, notify, discuss and propose; nothing they produce changes metadata without a
person's review. A failing step is recorded and the next one still runs. Runs are listed with
their steps under Governance → Playbooks.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

PLAYBOOK_ACTOR = "urn:li:corpuser:cairn-playbooks"

playbooks = Table(
    "cairn_playbooks", metadata,
    Column("id", String(40), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("config", JSONText, nullable=False),
    Column("updated_by", String(512), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
playbook_runs = Table(
    "cairn_playbook_runs", metadata,
    Column("id", String(40), primary_key=True),
    Column("playbook", String(40), nullable=False, index=True),
    Column("subject", String(1024), nullable=False),
    Column("event", JSONText, nullable=False),
    Column("steps", JSONText, nullable=False),
    Column("state", String(12), nullable=False),           # DONE | PARTIAL | FAILED
    Column("created_ms", BigInteger, nullable=False, index=True),
    Column("finished_ms", BigInteger, nullable=True),
)

CATALOG = {
    "incident_triage": {"name": "Incident triage", "trigger": "quality.incident.raised",
                        "description": "Likely root causes, people to contact and downstream impact for a quality "
                                       "incident; posted on the dataset and sent to its owners."},
    "change_impact": {"name": "Schema change impact", "trigger": "schema.changed",
                      "description": "Which downstream assets a schema change affects, certified first; their "
                                     "owners are told which columns changed."},
    "access_review": {"name": "Access request review", "trigger": "access.requested",
                      "description": "A recommendation for the approvers of an access request, with its reasons."},
    "dataset_onboarding": {"name": "Dataset onboarding", "trigger": "dataset.created",
                           "description": "AI suggestions queued for review, default quality monitors, and a note "
                                          "to the owners when a dataset appears."},
}


def _now() -> int:
    return int(time.time() * 1000)


def _name(urn: str) -> str:
    return urn.split(",")[-2].split(".")[-1] if "," in urn else urn.split(":")[-1]


class PlaybookError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PlaybookService:
    def __init__(self, engine, services, audit=None):
        """``services``: the Services (catalog, graph, explainer, steward, notify, collab, marketplace,
        quality); each step uses what is configured and is skipped otherwise."""
        self.engine = engine
        self.s = services
        self.audit = audit
        self.steps: dict[str, list[tuple[str, Callable[[str, dict, dict], dict]]]] = {
            "incident_triage": [("root causes", self._root_causes), ("people to contact", self._contacts),
                                ("downstream impact", self._downstream), ("discussion", self._discuss),
                                ("notify owners", self._notify)],
            "change_impact": [("downstream impact", self._downstream), ("people to contact", self._impacted_owners),
                              ("notify owners", self._notify), ("discussion", self._discuss)],
            "access_review": [("request", self._load_request), ("assessment", self._assess),
                              ("notify approvers", self._notify_approvers)],
            "dataset_onboarding": [("AI suggestions", self._suggest), ("quality monitors", self._monitors),
                                   ("people to contact", self._contacts), ("notify owners", self._notify)],
        }

    # ---- configuration ------------------------------------------------------------------------
    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = {r.id: r for r in conn.execute(select(playbooks))}
        out = []
        for pid, meta in CATALOG.items():
            r = rows.get(pid)
            out.append({"id": pid, **meta, "enabled": bool(r and r.enabled), "config": (r.config if r else {}) or {},
                        "steps": [n for n, _ in self.steps[pid]], "last_run": (self.runs(pid, 1) or [None])[0]})
        return out

    def set_enabled(self, pid: str, enabled: bool, actor: str) -> dict:
        if pid not in CATALOG:
            raise PlaybookError("no such playbook", 404)
        with self.engine.begin() as conn:
            done = conn.execute(update(playbooks).where(playbooks.c.id == pid)
                                .values(enabled=enabled, updated_by=actor, updated_ms=_now())).rowcount
            if not done:
                conn.execute(playbooks.insert().values(id=pid, enabled=enabled, config={}, updated_by=actor,
                                                       updated_ms=_now()))
        if self.audit is not None:
            self.audit.record("governance", "playbook.enabled" if enabled else "playbook.disabled", actor, pid, {})
        return next(p for p in self.list() if p["id"] == pid)

    def enabled_for(self, event_type: str) -> list[str]:
        with self.engine.connect() as conn:
            on = {r.id for r in conn.execute(select(playbooks.c.id).where(playbooks.c.enabled.is_(True)))}
        return [pid for pid, meta in CATALOG.items() if meta["trigger"] == event_type and pid in on]

    # ---- running --------------------------------------------------------------------------------
    def handle(self, event: dict) -> list[dict]:
        """Run every enabled playbook the event triggers (from the automation event queue)."""
        return [self.run(pid, event["urn"], event) for pid in self.enabled_for(event["type"])]

    def run(self, pid: str, subject: str, event: Optional[dict] = None) -> dict:
        if pid not in CATALOG:
            raise PlaybookError("no such playbook", 404)
        event = event or {"type": CATALOG[pid]["trigger"], "urn": subject, "details": {}}
        rid, ctx, steps = f"pbr_{uuid.uuid4().hex[:12]}", {"event": event, "playbook": pid}, []
        for name, fn in self.steps[pid]:
            started = _now()
            try:
                out = fn(subject, event.get("details") or {}, ctx) or {}
                steps.append({"name": name, "status": out.pop("_status", "ok"), "summary": out.pop("_summary", ""),
                              "data": out, "ms": _now() - started})
            except Exception as e:  # noqa: BLE001 - recorded; the next step still runs
                logger.exception("playbook %s step %s failed on %s", pid, name, subject)
                steps.append({"name": name, "status": "failed", "summary": f"{type(e).__name__}: {e}"[:500],
                              "data": {}, "ms": _now() - started})
        failed = [s for s in steps if s["status"] == "failed"]
        state = "FAILED" if len(failed) == len(steps) else "PARTIAL" if failed else "DONE"
        with self.engine.begin() as conn:
            conn.execute(playbook_runs.insert().values(
                id=rid, playbook=pid, subject=subject, event={"type": event.get("type"), "id": event.get("id")},
                steps=steps, state=state, created_ms=_now(), finished_ms=_now()))
        if self.audit is not None:
            self.audit.record("governance", "playbook.ran", PLAYBOOK_ACTOR, subject, {"playbook": pid, "run": rid,
                                                                                     "state": state})
        return self.get_run(rid)

    def get_run(self, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(playbook_runs).where(playbook_runs.c.id == rid)).first()
        if r is None:
            raise PlaybookError("no such run", 404)
        return self._run(r)

    @staticmethod
    def _run(r) -> dict:
        return {"id": r.id, "playbook": r.playbook, "subject": r.subject, "event": r.event, "steps": r.steps,
                "state": r.state, "created_ms": r.created_ms}

    def runs(self, pid: Optional[str] = None, limit: int = 50) -> list[dict]:
        q = select(playbook_runs).order_by(playbook_runs.c.created_ms.desc()).limit(limit)
        if pid:
            q = q.where(playbook_runs.c.playbook == pid)
        with self.engine.connect() as conn:
            return [self._run(r) for r in conn.execute(q)]

    # ---- steps ------------------------------------------------------------------------------------
    def _owners(self, urn: str) -> list[str]:
        own = (self.s.graph.get_aspect(urn, "ownership") or {}) if self.s.graph is not None else {}
        return [o["owner"] for o in own.get("owners") or [] if o.get("owner")]

    def _visible_to_all(self, people: list[str]) -> Callable[[str], bool]:
        """Only assets every recipient may see go into what the playbook says (no leaks to anyone)."""
        checks = [self.s.can_view_as(p) for p in people]
        return lambda urn: bool(checks) and all(c(urn) for c in checks)

    def _root_causes(self, urn: str, details: dict, ctx: dict) -> dict:
        from cairn.ai.lineage_explainer import LineageExplainer

        people = self._owners(urn)
        explainer = LineageExplainer(self.s.catalog, self.s.quality.store, self.s.context, None,
                                     can_view=self._visible_to_all(people) if people else (lambda u: u == urn))
        out = explainer.explain(urn)
        causes = [{"urn": c["urn"], "name": c.get("name") or _name(c["urn"]), "score": c.get("score"),
                   "evidence": (c.get("evidence") or [])[:3]} for c in (out.get("likely_causes") or [])[:3]]
        ctx["causes"] = causes
        return {"causes": causes, "_summary": "; ".join(f"{c['name']} ({', '.join(c['evidence'][:1])})"
                                                       for c in causes) or "no upstream cause found"}

    def _contacts(self, urn: str, details: dict, ctx: dict) -> dict:
        people = self._owners(urn)
        for c in ctx.get("causes") or []:
            people += [p for p in self._owners(c["urn"]) if p not in people]
        if not people and self.s.graph is not None:              # no owner yet: the domain's owners
            for d in (self.s.graph.get_aspect(urn, "domains") or {}).get("domains") or []:
                people += [p for p in self._owners(d) if p not in people]
        ctx["people"] = people
        return {"people": people, "_status": "ok" if people else "skipped",
                "_summary": ", ".join(p.split(":")[-1] for p in people) or "nobody owns this dataset or its domain"}

    def _downstream(self, urn: str, details: dict, ctx: dict) -> dict:
        if self.s.catalog is None:
            return {"_status": "skipped", "_summary": "lineage needs the catalog connection"}
        rows = self.s.catalog.lineage(urn, "DOWNSTREAM", 3, 200)
        attrs = self.s.attributes
        impacted = []
        for r in rows:
            cert = (attrs.resource(r["urn"]).get("certification") if attrs is not None else None)
            impacted.append({"urn": r["urn"], "name": r.get("name") or _name(r["urn"]), "degree": r.get("degree"),
                             "certified": cert == "CERTIFIED"})
        impacted.sort(key=lambda i: (not i["certified"], i.get("degree") or 9, i["name"]))
        ctx["impacted"] = impacted
        certified = [i for i in impacted if i["certified"]]
        return {"impacted": impacted[:50], "count": len(impacted), "certified": len(certified),
                "_summary": f"{len(impacted)} downstream asset(s), {len(certified)} certified"}

    def _impacted_owners(self, urn: str, details: dict, ctx: dict) -> dict:
        people = []
        for i in [i for i in ctx.get("impacted") or [] if i["certified"]] + (ctx.get("impacted") or [])[:20]:
            people += [p for p in self._owners(i["urn"]) if p not in people]
        people += [p for p in self._owners(urn) if p not in people]
        ctx["people"] = people
        return {"people": people, "_status": "ok" if people else "skipped",
                "_summary": ", ".join(p.split(":")[-1] for p in people) or "nobody to tell"}

    def _report(self, urn: str, details: dict, ctx: dict) -> tuple[str, str]:
        pid, name = ctx["playbook"], _name(urn)
        visible = self._visible_to_all(ctx.get("people") or [])
        lines = []
        if pid == "incident_triage":
            title = f"Incident on {name}: likely causes"
            causes = [c for c in ctx.get("causes") or [] if c["urn"] == urn or visible(c["urn"])]
            lines += [f"- **{c['name']}**: {'; '.join(c['evidence']) or 'no evidence'}" for c in causes] or \
                     ["- No upstream cause stands out yet."]
        elif pid == "change_impact":
            title = f"Schema change on {name}"
            if details.get("removed"):
                lines.append(f"Removed columns: {', '.join(details['removed'])}")
            if details.get("added"):
                lines.append(f"Added columns: {', '.join(details['added'])}")
        else:
            title = f"New dataset {name}"
            lines.append(ctx.get("onboarding") or "It needs an owner, a description and a classification.")
        impacted = [i for i in ctx.get("impacted") or [] if visible(i["urn"])]
        if impacted:
            lines.append(f"Downstream: {len(impacted)} asset(s)" +
                         (f", certified: {', '.join(i['name'] for i in impacted if i['certified'])}"
                          if any(i["certified"] for i in impacted) else ""))
        return title, "\n".join(lines)

    def _discuss(self, urn: str, details: dict, ctx: dict) -> dict:
        from cairn.collab.service import ThreadIn

        if self.s.collab is None:
            return {"_status": "skipped", "_summary": "discussions are not configured"}
        if ctx["playbook"] == "change_impact" and not (details.get("removed") or any(
                i["certified"] for i in ctx.get("impacted") or [])):
            return {"_status": "skipped", "_summary": "nothing removed and nothing certified downstream"}
        title, body = self._report(urn, details, ctx)
        t = self.s.collab.create_thread(PLAYBOOK_ACTOR, ThreadIn(entity_urn=urn, body_md=f"**{title}**\n\n{body}\n\n"
                                                                 "_Posted by the Cairn playbook._"))
        return {"thread": t["id"], "_summary": "posted on the dataset"}

    def _notify(self, urn: str, details: dict, ctx: dict) -> dict:
        from cairn.notify.hub import Event

        people = ctx.get("people") or []
        if self.s.notify is None or not people:
            return {"_status": "skipped", "_summary": "nobody to notify"}
        if ctx["playbook"] == "change_impact" and not (details.get("removed") or any(
                i["certified"] for i in ctx.get("impacted") or [])):
            return {"_status": "skipped", "_summary": "nothing removed and nothing certified downstream"}
        title, body = self._report(urn, details, ctx)
        self.s.notify.publish(Event(type="playbook.report", title=title, body=body[:3500], actor=PLAYBOOK_ACTOR,
                                    recipients=people, entity_urn=urn, payload={"playbook": ctx["playbook"]}))
        return {"notified": people, "_summary": f"told {len(people)} people"}

    # access review
    def _load_request(self, product: str, details: dict, ctx: dict) -> dict:
        if self.s.marketplace is None:
            raise PlaybookError("the marketplace is not configured")
        rid = details.get("request_id")
        if not rid:
            raise PlaybookError("the event names no request")
        r = self.s.marketplace.get_request(rid)
        ctx["request"] = r
        return {"request": rid, "requester": r["requester"], "_summary": f"{r['requester'].split(':')[-1]} for "
                f"“{r['purpose']}”, {r['duration_days']} days"}

    def _assess(self, product: str, details: dict, ctx: dict) -> dict:
        from cairn.ai.guardrails import injection_signals
        from cairn.marketplace.service import AccessRequestIn

        r, reasons, risk = ctx["request"], [], 0
        subject = self.s.subject_of(r["requester"])
        preview = self.s.marketplace.preview(r["requester"], AccessRequestIn(
            product_urn=r["product_urn"], purpose=r["purpose"], justification=r.get("justification") or "",
            duration_days=r["duration_days"]), subject)
        if preview["auto_approve"]:
            reasons.append("the product's policy would approve this automatically")
        assets = self.s._product_assets(r["product_urn"]) if hasattr(self.s, "_product_assets") else []
        restricted = sorted({c for u in assets for c in (self.s.attributes.resource(u).get("classification") or [])
                             if c and ("RESTRICTED" in c.upper() or "PII" in c.upper())})
        if restricted:
            risk += 2
            reasons.append(f"the product holds sensitive data ({', '.join(restricted[:3])})")
        just = r.get("justification") or ""
        if len(just.strip()) < 20:
            risk += 1
            reasons.append("the justification is short")
        if injection_signals(just):
            risk += 3
            reasons.append("the justification contains text addressed to an AI; read it carefully")
        if r["duration_days"] > 60:
            risk += 1
            reasons.append(f"a long grant ({r['duration_days']} days)")
        verdict = "approve" if risk == 0 else "review carefully" if risk <= 2 else "reject unless justified"
        ctx["assessment"] = {"recommendation": verdict, "reasons": reasons or ["nothing unusual"], "risk": risk}
        return {**ctx["assessment"], "_summary": f"{verdict}: {'; '.join(reasons) or 'nothing unusual'}"}

    def _notify_approvers(self, product: str, details: dict, ctx: dict) -> dict:
        from cairn.notify.hub import Event

        r, a = ctx["request"], ctx["assessment"]
        if self.s.notify is None or not r.get("approvers"):
            return {"_status": "skipped", "_summary": "no approvers to tell"}
        body = f"Recommendation: **{a['recommendation']}**\n" + "\n".join(f"- {x}" for x in a["reasons"])
        self.s.notify.publish(Event(type="playbook.report", title=f"Review: access to {r['product_name']}",
                                    body=body, actor=PLAYBOOK_ACTOR, recipients=list(r["approvers"]),
                                    entity_urn=r["product_urn"],
                                    url=f"{self.s.notify.public_url}/cairn/requests?request={r['id']}",
                                    payload={"playbook": "access_review", "request_id": r["id"],
                                             "recommendation": a["recommendation"]}))
        return {"notified": list(r["approvers"]), "_summary": f"told {len(r['approvers'])} approver(s)"}

    # onboarding
    def _suggest(self, urn: str, details: dict, ctx: dict) -> dict:
        if self.s.steward is None:
            return {"_status": "skipped", "_summary": "the AI steward is not configured"}
        out = self.s.steward.run(actor=PLAYBOOK_ACTOR, urns=[urn], limit=1, max_drafts=1)
        ctx["onboarding"] = f"{out['proposed']} suggestion(s) wait for review in Governance → AI steward."
        return {"proposed": out["proposed"], "_summary": ctx["onboarding"]}

    def _monitors(self, urn: str, details: dict, ctx: dict) -> dict:
        from cairn.governance.attributes import resource_from_aspects
        from cairn.quality.auto import _SCROLL_ASPECTS, CREATED_BY, Planner, load_rules

        if self.s.graph is None:
            return {"_status": "skipped", "_summary": "no catalog"}
        aspects = {a: v for a in _SCROLL_ASPECTS if (v := self.s.graph.get_aspect(urn, a))}
        planner, rules = Planner(self.s.graph), load_rules()
        desired = planner.plan(rules, planner.matches(rules, [(urn, resource_from_aspects(urn, aspects), aspects)]))
        store, made = self.s.quality.store, []
        existing = {m.id for m in store.list_monitors(urn)}
        for mid, spec in desired.items():
            if mid not in existing:
                self.s.quality.sync_monitor(store.create_monitor(spec, CREATED_BY, monitor_id=mid))
                made.append(spec.name)
        return {"created": made, "_status": "ok" if made else "skipped",
                "_summary": f"{len(made)} monitor(s) created" if made else "no default monitor applies"}
