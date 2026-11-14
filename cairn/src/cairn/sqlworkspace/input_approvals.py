"""Input-table approvals: changes to an input table wait for a reviewer before they land.

``PUT /sql/input-tables/<id>/approval`` with ``{"reviewers": ["urn:li:corpuser:…", "urn:li:corpGroup:…"]}`` (the
table's creator or an operator; ``[]`` turns review off). From then on every added, changed or deleted row is a
**change request** (the answer is 202 with its id): checked at once (columns, types), written only when a
reviewer approves it. Nobody approves their own change, so a reviewer's change needs another reviewer.

* **Approved changes are written as their author**, on Trino, as if they had written them then: their policies
  decide, ``_updated_by`` is theirs, and a row someone changed meanwhile is a conflict, not an overwrite (the change
  ends ``failed``, with why). Rejected changes are never written.
* ``GET …/<id>/changes`` lists them (reviewers and authors see them; ``?state=pending``); ``POST
  …/changes/<cid>/approve`` and ``…/reject`` (with a ``note``) decide. Reviewers are told of a new change and the
  author of the decision (``input.change_requested``, ``input.change_decided``); each step is audited.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, and_, select, update

from cairn.common.db import JSONText, metadata

KINDS = ("add", "change", "remove")
STATES = ("pending", "applied", "rejected", "failed")

input_approval = Table(
    "cairn_input_approval", metadata,
    Column("table_id", String(32), primary_key=True),
    Column("reviewers", JSONText, nullable=False),
    Column("set_by", String(256), nullable=False),
    Column("set_ms", BigInteger, nullable=False),
)
input_changes = Table(
    "cairn_input_changes", metadata,
    Column("id", String(32), primary_key=True),
    Column("table_id", String(32), nullable=False, index=True),
    Column("kind", String(8), nullable=False),
    Column("payload", JSONText, nullable=False),
    Column("author", String(256), nullable=False),
    Column("state", String(10), nullable=False, index=True),
    Column("reviewer", String(256), nullable=True),
    Column("note", Text, nullable=True),
    Column("result", JSONText, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("decided_ms", BigInteger, nullable=True),
)


class ApprovalError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


class InputApprovals:
    def __init__(self, engine, inputs, groups_of: Callable[[str], list[str]], is_operator: Callable[[str], bool],
                 notify: Optional[Callable[[str, str, str, list[str], dict], Any]] = None, audit=None):
        self.engine, self.inputs, self.groups_of, self.is_operator = engine, inputs, groups_of, is_operator
        self.notify, self.audit = notify, audit

    # ---- who reviews ------------------------------------------------------------------------------
    def reviewers(self, tid: str) -> list[str]:
        with self.engine.connect() as conn:
            r = conn.execute(select(input_approval.c.reviewers).where(input_approval.c.table_id == tid)).first()
        return list(r.reviewers) if r is not None else []

    def is_reviewer(self, actor: str, tid: str) -> bool:
        me = {actor, *self.groups_of(actor)}
        return any(r in me for r in self.reviewers(tid))

    def set(self, actor: str, tid: str, reviewers: Any) -> dict:
        t = self.inputs._get(tid)
        if t["created_by"] != actor and not self.is_operator(actor):
            raise ApprovalError("only the table's creator or an operator sets its reviewers", 403)
        if not isinstance(reviewers, list) or len(reviewers) > 50 or not all(
                isinstance(r, str) and r.startswith(("urn:li:corpuser:", "urn:li:corpGroup:")) for r in reviewers):
            raise ApprovalError("reviewers: up to 50 of urn:li:corpuser:… or urn:li:corpGroup:… ([] turns review off)")
        with self.engine.begin() as conn:
            conn.execute(input_approval.delete().where(input_approval.c.table_id == tid))
            if reviewers:
                conn.execute(input_approval.insert().values(table_id=tid, reviewers=sorted(set(reviewers)), set_by=actor,
                                                            set_ms=_now()))
        self._audit(actor, "input.reviewers_set", t["table_name"], {"reviewers": sorted(set(reviewers))})
        return {"table": tid, "reviewers": sorted(set(reviewers))}

    # ---- writes go through here -------------------------------------------------------------------
    def write(self, actor: str, tid: str, kind: str, payload: dict) -> tuple[int, dict]:
        """(status, body): written now (200) when the table needs no review, else a change request (202)."""
        if not self.reviewers(tid):
            return 200, self._apply(actor, tid, kind, payload)
        self._apply(actor, tid, kind, payload, dry_run=True)        # refused now if it could never be written
        cid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(input_changes.insert().values(id=cid, table_id=tid, kind=kind, payload=payload, author=actor,
                                                       state="pending", created_ms=_now()))
        t = self.inputs._get(tid)
        self._audit(actor, "input.change_requested", t["table_name"], {"change": cid, "kind": kind})
        self._tell("input.change_requested", f"A change to {t['table_name']} waits for review",
                   f"{actor.rsplit(':', 1)[-1]} asks to {kind} rows in {t['table_name']}.",
                   [r for r in self.reviewers(tid) if r != actor], {"table": tid, "change": cid})
        return 202, {"pending": cid, "state": "pending"}

    def _apply(self, actor: str, tid: str, kind: str, p: dict, dry_run: bool = False) -> dict:
        if kind == "add":
            return self.inputs.add(actor, tid, p.get("rows"), dry_run=dry_run)
        if kind == "change":
            return self.inputs.change(actor, tid, str(p.get("row_id") or ""), p.get("values"), p.get("expected_updated_at"),
                                      dry_run=dry_run)
        if kind == "remove":
            return self.inputs.remove(actor, tid, p.get("rows"), dry_run=dry_run)
        raise ApprovalError(f"kind: one of {', '.join(KINDS)}")

    # ---- review -----------------------------------------------------------------------------------
    def changes(self, actor: str, tid: str, state: Optional[str] = None) -> list[dict]:
        self.inputs._get(tid)
        reviewer = self.is_reviewer(actor, tid) or self.is_operator(actor)
        q = select(input_changes).where(input_changes.c.table_id == tid).order_by(input_changes.c.created_ms.desc())
        if state:
            if state not in STATES:
                raise ApprovalError(f"state: one of {', '.join(STATES)}")
            q = q.where(input_changes.c.state == state)
        if not reviewer:
            q = q.where(input_changes.c.author == actor)              # authors see their own
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q.limit(500))]

    def decide(self, actor: str, cid: str, approve: bool, note: str = "") -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(input_changes).where(input_changes.c.id == cid)).first()
        if r is None:
            raise ApprovalError("no such change", 404)
        if not self.is_reviewer(actor, r.table_id):
            raise ApprovalError("only the table's reviewers decide its changes", 403)
        if r.author == actor:
            raise ApprovalError("another reviewer decides your own change", 403)
        # Take it: only one reviewer decides a change, whatever happens at the same time.
        with self.engine.begin() as conn:
            n = conn.execute(update(input_changes).where(and_(input_changes.c.id == cid, input_changes.c.state == "pending"))
                             .values(state="deciding", reviewer=actor, decided_ms=_now())).rowcount
        if not n:
            raise ApprovalError(f"this change is {r.state} already", 409)
        result: Any = None
        if not approve:
            state = "rejected"
        else:
            from cairn.sqlworkspace.input_tables import InputTableError

            try:
                result = self._apply(r.author, r.table_id, r.kind, dict(r.payload))
                state = "applied"
            except InputTableError as e:
                state, result = "failed", {"error": str(e), "status": e.status}
            except Exception as e:  # noqa: BLE001 - Trino refused it (the author's permissions, say): say so
                state, result = "failed", {"error": str(e)[:500]}
        with self.engine.begin() as conn:
            conn.execute(update(input_changes).where(input_changes.c.id == cid).values(
                state=state, note=(note or "")[:2000] or None, result=result))
        t = self.inputs._get(r.table_id)
        self._audit(actor, f"input.change_{state}", t["table_name"], {"change": cid, "author": r.author})
        self._tell("input.change_decided", f"Your change to {t['table_name']} was {state}",
                   (note or "") if state == "rejected" else (result or {}).get("error", "") if state == "failed" else "",
                   [r.author], {"table": r.table_id, "change": cid, "state": state})
        with self.engine.connect() as conn:
            return dict(conn.execute(select(input_changes).where(input_changes.c.id == cid)).first()._mapping)

    def _tell(self, kind: str, title: str, body: str, who: list[str], payload: dict) -> None:
        people = [w for w in who if w.startswith("urn:li:corpuser:")]
        if self.notify is not None and people:
            try:
                self.notify(kind, title, body, people, payload)
            except Exception:  # noqa: BLE001 - a notification never undoes a change
                pass

    def _audit(self, actor: str, action: str, table: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("sql", action, actor, table, details)
