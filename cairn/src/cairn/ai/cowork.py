"""CoWork: the assistant working for one person on a schedule, and sending to Slack or email only once they approve.

* **Scheduled tasks** (``/ai/scheduled-tasks``): a person asks for recurring work in plain words ("every Monday,
  summarise last week's revenue by region and anything unusual"), on a cron schedule (at most hourly). Each run is
  a deep-research run (:mod:`cairn.ai.workspace`) as that person: their policies, masks and row filters apply, their
  AI budget pays, and the report is saved as their artifact (with the datasets it read). They are told in Cairn and
  on their own channels. A person keeps up to ``CAIRN_COWORK_MAX_TASKS`` tasks (20); a task whose owner's account is
  suspended fails rather than runs, and one that fails three times in a row is paused.
* **Sending out, approved each time** (``/ai/outbox``): a task may name where its report goes: a Slack channel from
  ``CAIRN_COWORK_SLACK_CHANNELS`` or email addresses in ``CAIRN_COWORK_EMAIL_DOMAINS`` (neither set: nothing is sent
  anywhere). A run never sends by itself: it puts the report in the person's outbox and asks them, in Cairn and as a
  notification with approve and reject buttons (Slack) or signed links (email). On approval, by the owner only, Cairn
  checks again that the target is allowed, that the owner may still read the report and may export every dataset it
  was made from (``EXPORT_DATASET``, fail closed), redacts PII from the text, and sends it. Unanswered items expire
  after ``CAIRN_COWORK_OUTBOX_HOURS`` (72).

Audited: ``cowork.task.saved``, ``cowork.task.deleted``, ``cowork.task.ran``, ``cowork.outbound.sent``,
``cowork.outbound.rejected``, ``cowork.outbound.refused`` (targets and ids, never the text).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

cowork_tasks = Table(
    "cairn_cowork_tasks", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(512), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("prompt", Text, nullable=False),
    Column("cron", String(100), nullable=False),
    Column("max_steps", Integer, nullable=False),
    Column("send", Text, nullable=True),                     # {"slack": "#channel"} | {"email": [addresses]}
    Column("enabled", Boolean, nullable=False),
    Column("failures", Integer, nullable=False, default=0),
    Column("next_run_ms", BigInteger, nullable=False, index=True),
    Column("last_run_ms", BigInteger, nullable=True),
    Column("last_status", String(16), nullable=True),
    Column("created_ms", BigInteger, nullable=False),
)
cowork_runs = Table(
    "cairn_cowork_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("task_id", String(32), nullable=False, index=True),
    Column("owner", String(512), nullable=False),
    Column("started_ms", BigInteger, nullable=False),
    Column("finished_ms", BigInteger, nullable=True),
    Column("status", String(16), nullable=False),            # done | failed
    Column("research_id", String(40), nullable=True),
    Column("artifact_id", String(40), nullable=True),
    Column("outbox_id", String(32), nullable=True),
    Column("error", Text, nullable=True),
)
cowork_outbox = Table(
    "cairn_cowork_outbox", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(512), nullable=False, index=True),
    Column("task_id", String(32), nullable=True),
    Column("kind", String(8), nullable=False),               # slack | email
    Column("target", Text, nullable=False),
    Column("subject", String(300), nullable=False),
    Column("artifact_id", String(40), nullable=False),
    Column("artifact_version", Integer, nullable=True),
    Column("status", String(16), nullable=False),            # pending | sent | rejected | refused | failed | expired
    Column("created_ms", BigInteger, nullable=False),
    Column("decided_ms", BigInteger, nullable=True),
    Column("error", Text, nullable=True),
)

EMAIL = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")
MIN_INTERVAL_S = 3600
MAX_FAILURES = 3
SLACK_TEXT = 35_000


class CoWorkError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _list_env(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


class CoWork:
    def __init__(self, engine, workspace, hub, authorize: Callable[[str, str, Optional[str]], bool], audit=None,
                 clock: Callable[[], float] = time.time, active: Optional[Callable[[str], bool]] = None):
        """``workspace``: the assistant's workspace (research, artifacts); ``hub``: notifications (and the Slack and
        email senders, ``hub.channels``); ``authorize(actor, privilege, urn)``: a policy decision; ``active(person)``:
        False for a suspended account (its tasks fail instead of running)."""
        self.engine, self.ws, self.hub, self.authorize, self.audit, self.clock = engine, workspace, hub, authorize, audit, clock
        self.active = active or (lambda person: True)
        self.max_tasks = int(os.environ.get("CAIRN_COWORK_MAX_TASKS", "20"))
        self.outbox_hours = float(os.environ.get("CAIRN_COWORK_OUTBOX_HOURS", "72"))
        if hub is not None:
            hub.register_action("cowork_outbox", self._hub_action)

    def _ms(self) -> int:
        return int(self.clock() * 1000)

    # ---- tasks -----------------------------------------------------------------------------------------
    def _next(self, cron: str, after_ms: int) -> int:
        from croniter import croniter

        base = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)
        return int(croniter(cron, base).get_next(datetime).timestamp() * 1000)

    def _send(self, raw) -> Optional[dict]:
        if raw in (None, {}):
            return None
        if not isinstance(raw, dict) or len(raw) != 1:
            raise CoWorkError('send: {"slack": "<channel>"} or {"email": [addresses]}')
        if "slack" in raw:
            allowed = _list_env("CAIRN_COWORK_SLACK_CHANNELS")
            if str(raw["slack"]) not in allowed:                  # fail closed: only channels an operator listed
                raise CoWorkError("that Slack channel is not one reports may be sent to (CAIRN_COWORK_SLACK_CHANNELS)", 403)
            return {"slack": str(raw["slack"])}
        if "email" in raw:
            to = raw["email"]
            if not isinstance(to, list) or not 1 <= len(to) <= 20 or not all(isinstance(x, str) for x in to):
                raise CoWorkError("email: 1 to 20 addresses")
            domains = {d.lower() for d in _list_env("CAIRN_COWORK_EMAIL_DOMAINS")}
            for x in to:
                m = EMAIL.match(x)
                if not m or m.group(1).lower() not in domains:
                    raise CoWorkError(f"{x} is not in a domain reports may be sent to (CAIRN_COWORK_EMAIL_DOMAINS)", 403)
            return {"email": sorted(set(to))}
        raise CoWorkError('send: {"slack": "<channel>"} or {"email": [addresses]}')

    def save_task(self, actor: str, body: dict, tid: Optional[str] = None) -> dict:
        from croniter import croniter

        if not isinstance(body, dict):
            raise CoWorkError("a task is an object")
        cur = self._task(actor, tid) if tid else None
        name = str(body.get("name", cur["name"] if cur else "") or "").strip()[:200]
        prompt = str(body.get("prompt", cur["prompt"] if cur else "") or "").strip()
        cron = str(body.get("cron", cur["cron"] if cur else "") or "").strip()
        steps = body.get("max_steps", cur["max_steps"] if cur else 4)
        if not name or not 5 <= len(prompt) <= 4000:
            raise CoWorkError("a task has a name and a prompt of 5 to 4,000 characters")
        if not croniter.is_valid(cron):
            raise CoWorkError("cron: a five-field cron expression (UTC)")
        now = self._ms()
        first = self._next(cron, now)
        if (self._next(cron, first) - first) / 1000 < MIN_INTERVAL_S:
            raise CoWorkError("a task runs at most once an hour")
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 8:
            raise CoWorkError("max_steps: 1 to 8")
        send = self._send(body["send"]) if "send" in body else (cur["send"] if cur else None)
        enabled = bool(body.get("enabled", cur["enabled"] if cur else True))
        values = {"name": name, "prompt": prompt, "cron": cron, "max_steps": steps,
                  "send": json.dumps(send) if send else None, "enabled": enabled, "next_run_ms": first}
        with self.engine.begin() as conn:
            if cur is None:
                n = len(conn.execute(select(cowork_tasks.c.id).where(cowork_tasks.c.owner == actor)).all())
                if n >= self.max_tasks:
                    raise CoWorkError(f"at most {self.max_tasks} scheduled tasks per person", 429)
                tid = uuid.uuid4().hex
                conn.execute(insert(cowork_tasks).values(id=tid, owner=actor, failures=0, created_ms=now, **values))
            else:
                conn.execute(update(cowork_tasks).where(cowork_tasks.c.id == tid).values(
                    failures=0 if enabled and not cur["enabled"] else cur["failures"], **values))
        self._audit("cowork.task.saved", actor, f"cowork_task:{tid}", {"cron": cron, "send": send, "enabled": enabled})
        return self.task(actor, tid)

    def _task(self, actor: str, tid: Optional[str]) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(cowork_tasks).where(cowork_tasks.c.id == tid)).first()
        if r is None or r.owner != actor:
            raise CoWorkError("no such scheduled task", 404)
        d = dict(r._mapping)
        d["send"] = json.loads(d["send"]) if d["send"] else None
        d["enabled"] = bool(d["enabled"])
        return d

    def task(self, actor: str, tid: str) -> dict:
        t = self._task(actor, tid)
        with self.engine.connect() as conn:
            runs = conn.execute(select(cowork_runs).where(cowork_runs.c.task_id == tid)
                                .order_by(cowork_runs.c.started_ms.desc()).limit(20)).all()
        return {**t, "runs": [dict(r._mapping) for r in runs]}

    def tasks(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(cowork_tasks).where(cowork_tasks.c.owner == actor)
                                .order_by(cowork_tasks.c.created_ms)).all()
        return [{**dict(r._mapping), "send": json.loads(r.send) if r.send else None, "enabled": bool(r.enabled)}
                for r in rows]

    def delete_task(self, actor: str, tid: str) -> None:
        self._task(actor, tid)
        with self.engine.begin() as conn:
            conn.execute(delete(cowork_tasks).where(cowork_tasks.c.id == tid))
        self._audit("cowork.task.deleted", actor, f"cowork_task:{tid}", {})

    # ---- running -----------------------------------------------------------------------------------------
    def run_due(self, now_ms: Optional[int] = None) -> int:
        now = now_ms if now_ms is not None else self._ms()
        self.expire(now)
        with self.engine.connect() as conn:
            due = conn.execute(select(cowork_tasks.c.id, cowork_tasks.c.next_run_ms, cowork_tasks.c.cron).where(
                cowork_tasks.c.enabled.is_(True), cowork_tasks.c.next_run_ms <= now)).all()
        n = 0
        for r in due:
            with self.engine.begin() as conn:               # claimed by moving its next run: one runner wins
                got = conn.execute(update(cowork_tasks).where(cowork_tasks.c.id == r.id,
                                                              cowork_tasks.c.next_run_ms == r.next_run_ms)
                                   .values(next_run_ms=self._next(r.cron, now))).rowcount
            if got:
                self._run(r.id)
                n += 1
        return n

    def run_now(self, actor: str, tid: str) -> dict:
        self._task(actor, tid)
        return self._run(tid)

    def _run(self, tid: str) -> dict:
        from cairn.common.security import Principal

        with self.engine.connect() as conn:
            t = conn.execute(select(cowork_tasks).where(cowork_tasks.c.id == tid)).first()
        rid = uuid.uuid4().hex
        started = self._ms()
        run: dict[str, Any] = {"id": rid, "task_id": tid, "owner": t.owner, "started_ms": started, "status": "failed",
                               "research_id": None, "artifact_id": None, "outbox_id": None, "error": None}
        try:
            if not self.active(t.owner):
                raise CoWorkError("the owner's account is suspended", 403)
            res = self.ws.start_research(Principal(urn=t.owner, via="delegated"), t.prompt, t.max_steps, wait=True)
            res = self.ws.research(t.owner, res["id"])
            run["research_id"] = res["id"]
            if res["status"] != "done":
                raise CoWorkError(f"the research run {res['status']}: {res.get('error') or ''}"[:1000])
            run.update(status="done", artifact_id=res["artifact_id"])
        except Exception as e:  # noqa: BLE001 - recorded on the run; three in a row pause the task
            run["error"] = (str(e) or type(e).__name__)[:2000]
            logger.warning("scheduled task %s failed: %s", tid, run["error"])
        run["finished_ms"] = self._ms()
        send = json.loads(t.send) if t.send else None
        if run["status"] == "done" and send:
            run["outbox_id"] = self._queue(t.owner, tid, send, t.name, run["artifact_id"])
        failures = 0 if run["status"] == "done" else (t.failures or 0) + 1
        with self.engine.begin() as conn:
            conn.execute(insert(cowork_runs).values(**run))
            conn.execute(update(cowork_tasks).where(cowork_tasks.c.id == tid).values(
                last_run_ms=started, last_status=run["status"], failures=failures,
                enabled=t.enabled and failures < MAX_FAILURES))
        self._audit("cowork.task.ran", t.owner, f"cowork_task:{tid}", {"status": run["status"], "run": rid,
                                                                       "artifact": run["artifact_id"]})
        self._notify(t.owner, t.name, run, paused=failures >= MAX_FAILURES)
        return run

    def _notify(self, owner: str, name: str, run: dict, paused: bool) -> None:
        if self.hub is None:
            return
        from cairn.notify.hub import Action, Event

        if run["status"] == "done":
            title, body = f"{name}: ready", "Your scheduled task finished; its report is in your artifacts."
            event = "job.finished"
        else:
            title = f"{name}: failed" + (" (paused after three failures)" if paused else "")
            body, event = (run["error"] or "")[:500], "job.failed"
        actions = []
        if run.get("outbox_id"):
            body += " It is waiting for your approval before it is sent."
            actions = [Action("approve", "cowork_outbox", run["outbox_id"], "Send", "primary"),
                       Action("reject", "cowork_outbox", run["outbox_id"], "Don't send", "danger")]
        try:
            self.hub.publish(Event(type=event, title=title, body=body, recipients=[owner], actions=actions,
                                   payload={"cowork_task": run["task_id"], "artifact": run.get("artifact_id")}))
        except Exception:  # noqa: BLE001 - a notification never fails a run
            logger.exception("could not notify about scheduled task %s", run["task_id"])

    # ---- the outbox --------------------------------------------------------------------------------------
    def _queue(self, owner: str, tid: Optional[str], send: dict, subject: str, artifact_id: str) -> str:
        art = self.ws.artifact(owner, artifact_id)
        oid = uuid.uuid4().hex
        kind = "slack" if "slack" in send else "email"
        with self.engine.begin() as conn:
            conn.execute(insert(cowork_outbox).values(
                id=oid, owner=owner, task_id=tid, kind=kind, target=json.dumps(send[kind]), subject=subject[:300],
                artifact_id=artifact_id, artifact_version=art.get("version"), status="pending",
                created_ms=self._ms(), decided_ms=None, error=None))
        return oid

    def propose(self, actor: str, body: dict) -> dict:
        """An outbound message the person (or the assistant, for them) asks for: an artifact to a target."""
        if not isinstance(body, dict):
            raise CoWorkError("an outbound message is an object")
        send = self._send(body.get("send"))
        if not send:
            raise CoWorkError("send: where it goes")
        aid = str(body.get("artifact_id") or "")
        try:
            art = self.ws.artifact(actor, aid)
        except Exception as e:  # noqa: BLE001 - the workspace answers 404 for what the person cannot read
            raise CoWorkError(str(e) or "no such artifact", getattr(e, "status", 404))
        oid = self._queue(actor, None, send, str(body.get("subject") or art["title"]), aid)
        return self.item(actor, oid)

    def _item(self, actor: str, oid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(cowork_outbox).where(cowork_outbox.c.id == oid)).first()
        if r is None or r.owner != actor:                     # only the owner sees or decides an item
            raise CoWorkError("no such outbound message", 404)
        return {**dict(r._mapping), "target": json.loads(r.target)}

    def item(self, actor: str, oid: str) -> dict:
        d = self._item(actor, oid)
        if d["status"] == "pending":
            try:
                art = self.ws.artifact(actor, d["artifact_id"], d["artifact_version"])
                d["preview"] = self._text(art)[:2000]
            except Exception:  # noqa: BLE001 - the report is gone or unreadable: approval will refuse
                d["preview"] = None
        return d

    def outbox(self, actor: str, status: Optional[str] = None) -> list[dict]:
        q = select(cowork_outbox).where(cowork_outbox.c.owner == actor)
        if status:
            q = q.where(cowork_outbox.c.status == status)
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(cowork_outbox.c.created_ms.desc()).limit(200)).all()
        return [{**dict(r._mapping), "target": json.loads(r.target)} for r in rows]

    @staticmethod
    def _text(art: dict) -> str:
        from cairn.ai.guardrails import redact

        return redact(str(art.get("content") or ""))

    def _decide(self, oid: str, status: str, error: Optional[str] = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(cowork_outbox).where(cowork_outbox.c.id == oid).values(
                status=status, decided_ms=self._ms(), error=error))

    def approve(self, actor: str, oid: str) -> dict:
        d = self._item(actor, oid)
        if d["status"] != "pending":
            raise CoWorkError(f"this message is {d['status']}", 409)
        with self.engine.begin() as conn:                     # claimed, so a double click sends once
            got = conn.execute(update(cowork_outbox).where(cowork_outbox.c.id == oid,
                                                           cowork_outbox.c.status == "pending")
                               .values(status="sending")).rowcount
        if not got:
            raise CoWorkError("this message is being decided", 409)
        try:
            if self._ms() - d["created_ms"] > self.outbox_hours * 3600_000:
                raise CoWorkError("this message expired; run the task again", 410)
            self._send({d["kind"]: d["target"]})                 # still an allowed target
            art = self.ws.artifact(actor, d["artifact_id"], d["artifact_version"])
            denied = [u for u in (art.get("sources") or []) if not self.authorize(actor, "EXPORT_DATASET", u)]
            if denied:
                raise CoWorkError(f"you may not send data out of {denied[0]} (EXPORT_DATASET)", 403)
        except Exception as e:  # noqa: BLE001 - refused, recorded
            self._decide(oid, "refused", str(e)[:1000])
            self._audit("cowork.outbound.refused", actor, f"cowork_outbox:{oid}", {"reason": str(e)[:300]})
            raise e if isinstance(e, CoWorkError) else CoWorkError(str(e), getattr(e, "status", 403))
        text = self._text(art)
        try:
            if d["kind"] == "slack":
                sender = (getattr(self.hub, "channels", {}) or {}).get("slack")
                if sender is None:
                    raise CoWorkError("Slack is not configured (CAIRN_SLACK_BOT_TOKEN)", 503)
                body = f"*{d['subject']}*\n{text}"
                sender._call("chat.postMessage", {"channel": d["target"], "text": body[:SLACK_TEXT], "mrkdwn": True})
            else:
                self._email(d["target"], d["subject"], text)
        except Exception as e:  # noqa: BLE001 - delivery failed: recorded, not retried silently
            self._decide(oid, "failed", str(e)[:1000])
            raise CoWorkError(f"sending failed: {e}", getattr(e, "status", 502))
        self._decide(oid, "sent")
        self._audit("cowork.outbound.sent", actor, f"cowork_outbox:{oid}",
                    {"kind": d["kind"], "target": d["target"], "artifact": d["artifact_id"],
                     "version": d["artifact_version"], "sources": art.get("sources") or []})
        return self.item(actor, oid)

    def _email(self, to: list[str], subject: str, text: str) -> None:
        import smtplib
        from email.message import EmailMessage

        sender = (getattr(self.hub, "channels", {}) or {}).get("email")
        if sender is None:
            raise CoWorkError("email is not configured (CAIRN_SMTP_URL)", 503)
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, sender.sender, ", ".join(to)
        msg.set_content(text)
        smtp_cls = smtplib.SMTP_SSL if sender.ssl else smtplib.SMTP
        with smtp_cls(sender.host, sender.port, timeout=15) as s:
            if sender.starttls:
                s.starttls()
            if sender.user:
                s.login(sender.user, sender.password or "")
            s.send_message(msg)

    def reject(self, actor: str, oid: str) -> dict:
        d = self._item(actor, oid)
        if d["status"] != "pending":
            raise CoWorkError(f"this message is {d['status']}", 409)
        self._decide(oid, "rejected")
        self._audit("cowork.outbound.rejected", actor, f"cowork_outbox:{oid}", {"kind": d["kind"]})
        return self.item(actor, oid)

    def expire(self, now_ms: Optional[int] = None) -> int:
        now = now_ms if now_ms is not None else self._ms()
        with self.engine.begin() as conn:
            return conn.execute(update(cowork_outbox).where(
                cowork_outbox.c.status == "pending",
                cowork_outbox.c.created_ms < now - int(self.outbox_hours * 3600_000)).values(
                status="expired", decided_ms=now)).rowcount

    def _hub_action(self, action: str, object_id: str, actor: str, comment: Optional[str]) -> str:
        try:
            if action == "approve":
                self.approve(actor, object_id)
                return "Sent."
            if action == "reject":
                self.reject(actor, object_id)
                return "Not sent."
        except CoWorkError as e:
            return f"Not sent: {e}"
        return "Unknown action."

    def _audit(self, action: str, actor: str, subject: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ai", action, actor, subject, details)


def build_cowork_router(c: CoWork, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(tags=["ai"])

    def person(p) -> str:
        if p.is_service or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "scheduled tasks and the outbox are a person's own")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except CoWorkError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/ai/scheduled-tasks")
    async def tasks(p=Depends(current_principal)):
        return await call(lambda: c.tasks(person(p)))

    @router.post("/ai/scheduled-tasks", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: c.save_task(person(p), body))

    @router.get("/ai/scheduled-tasks/{tid}")
    async def get(tid: str, p=Depends(current_principal)):
        return await call(lambda: c.task(person(p), tid))

    @router.put("/ai/scheduled-tasks/{tid}")
    async def change(tid: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: c.save_task(person(p), body, tid))

    @router.delete("/ai/scheduled-tasks/{tid}", status_code=204)
    async def delete_(tid: str, p=Depends(current_principal)):
        await call(lambda: c.delete_task(person(p), tid))

    @router.post("/ai/scheduled-tasks/{tid}/run")
    async def run(tid: str, p=Depends(current_principal)):
        return await call(lambda: c.run_now(person(p), tid))

    @router.get("/ai/outbox")
    async def outbox(status: Optional[str] = None, p=Depends(current_principal)):
        return await call(lambda: c.outbox(person(p), status))

    @router.post("/ai/outbox", status_code=201)
    async def propose(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: c.propose(person(p), body))

    @router.get("/ai/outbox/{oid}")
    async def item(oid: str, p=Depends(current_principal)):
        return await call(lambda: c.item(person(p), oid))

    @router.post("/ai/outbox/{oid}/approve")
    async def approve(oid: str, p=Depends(current_principal)):
        return await call(lambda: c.approve(person(p), oid))

    @router.post("/ai/outbox/{oid}/reject")
    async def reject(oid: str, p=Depends(current_principal)):
        return await call(lambda: c.reject(person(p), oid))

    return router
