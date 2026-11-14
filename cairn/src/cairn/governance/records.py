"""Records management: legal holds, retention that deletes, and erasure that proves it.

**Legal holds** (``/governance/privacy/holds``, a privacy officer). A hold on a table, with its case and reason,
keeps every row: while it is on, Trino refuses any statement that removes or rewrites the table's data or its
history for everyone (``DELETE``, ``UPDATE``, ``MERGE``, ``TRUNCATE``, ``DROP``, dropping or changing columns,
renaming it, and table procedures such as ``expire_snapshots``); retention skips it and an erasure leaves it, saying
so. Holds are released, never deleted; both are audited.

**Retention that deletes.** A retention policy with ``"enforce": true`` deletes the rows older than its limit
(by the time column) from each table it covers, on Trino, as ``CAIRN_PRIVACY_ACTOR`` (who needs write on those
tables). It runs with the daily privacy sweep, or now (``POST /governance/privacy/retention/enforce``). Every run
is kept (``GET …/retention/runs``): table, cutoff, rows deleted, or why not (held, no time column, refused).

**Erasure that proves it.** For a deletion request, the officer sends the person's identifier again (Cairn keeps
only its hash, and checks it matches) to ``POST /governance/privacy/requests/<id>/erase``. For each table with a
part still to do, on a Trino connection, Cairn counts the person's rows, deletes them, counts again (it must be 0)
and expires the table's older snapshots, so the rows are gone from its history too (Trino's
``iceberg.expire-snapshots.min-retention`` must allow it; if not, the proof says the snapshots remain). Tables on
hold are left and listed. When every part is done, Cairn signs a report (``GET …/requests/<id>/proof``): which
tables, how many rows before, deleted and after, whether history was expired, when, and by whom, with the
identifier only as its hash. It is a JWS signed with Cairn's key (``GET …/privacy/proof-keys``), so anyone can
check it without Cairn; ``POST …/privacy/proof/verify`` checks one too.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, and_, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

PROOF_TYPE = "cairn-erasure-proof"
HOLD_ID = re.compile(r"^hold_[0-9a-f]{12}$")

legal_holds = Table(
    "cairn_legal_holds", metadata,
    Column("id", String(24), primary_key=True),
    Column("dataset", String(700), nullable=False, index=True),
    Column("case_ref", String(200), nullable=False),
    Column("reason", Text, nullable=False),
    Column("placed_by", String(512), nullable=False),
    Column("placed_ms", BigInteger, nullable=False),
    Column("released_by", String(512), nullable=True),
    Column("released_ms", BigInteger, nullable=True),
    Column("release_note", Text, nullable=True),
)
retention_runs = Table(
    "cairn_retention_runs", metadata,
    Column("id", String(40), primary_key=True),
    Column("dataset", String(700), nullable=False, index=True),
    Column("policy", String(40), nullable=False),
    Column("cutoff_ms", BigInteger, nullable=False),
    Column("state", String(12), nullable=False),          # deleted | held | skipped | failed
    Column("deleted", Integer, nullable=True),
    Column("detail", String(500), nullable=False),
    Column("ran_ms", BigInteger, nullable=False, index=True),
)
erasure_proofs = Table(
    "cairn_erasure_proofs", metadata,
    Column("request", String(40), primary_key=True),
    Column("jws", Text, nullable=False),
    Column("doc", JSONText, nullable=False),
    Column("signed_ms", BigInteger, nullable=False),
)


class RecordsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}Z"


class Records:
    def __init__(self, engine, privacy, data_access, signer=None, audit=None, notify=None,
                 actor: Optional[str] = None):
        """``privacy``: the PrivacyService (requests, policies, where data is); ``data_access``: runs statements on
        Trino; ``signer``: a TrinoTokens with a key of its own, for proofs; ``actor``: who deletes
        (``CAIRN_PRIVACY_ACTOR``)."""
        self.engine, self.privacy, self.data, self.signer = engine, privacy, data_access, signer
        self.audit, self.notify = audit, notify
        self.actor = actor if actor is not None else os.environ.get("CAIRN_PRIVACY_ACTOR") or None

    # ---- legal holds ------------------------------------------------------------------------------
    def holds(self, dataset: Optional[str] = None, active_only: bool = True) -> list[dict]:
        q = select(legal_holds).order_by(legal_holds.c.placed_ms.desc())
        if dataset:
            q = q.where(legal_holds.c.dataset == dataset)
        if active_only:
            q = q.where(legal_holds.c.released_ms.is_(None))
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    def held(self, dataset: str) -> bool:
        """True while any hold on the dataset is on (read from the database each time: never stale)."""
        with self.engine.connect() as conn:
            return conn.execute(select(legal_holds.c.id).where(and_(
                legal_holds.c.dataset == dataset, legal_holds.c.released_ms.is_(None))).limit(1)).first() is not None

    def place(self, actor: str, body: dict) -> dict:
        dataset = str((body or {}).get("dataset") or "")
        case, reason = str(body.get("case") or "").strip(), str(body.get("reason") or "").strip()
        if not dataset.startswith("urn:li:dataset:("):
            raise RecordsError("dataset: a dataset urn")
        if not case or not reason:
            raise RecordsError("a hold names its case and its reason")
        hid, now = f"hold_{uuid.uuid4().hex[:12]}", _now()
        with self.engine.begin() as conn:
            conn.execute(legal_holds.insert().values(id=hid, dataset=dataset, case_ref=case[:200], reason=reason[:2000],
                                                     placed_by=actor, placed_ms=now))
        self._audit("privacy.hold_placed", actor, dataset, {"hold": hid, "case": case[:200]})
        return self._hold(hid)

    def release(self, actor: str, hid: str, note: str) -> dict:
        if not HOLD_ID.match(hid or ""):
            raise RecordsError("no such hold", 404)
        if not (note or "").strip():
            raise RecordsError("say why the hold is released")
        with self.engine.begin() as conn:
            n = conn.execute(update(legal_holds).where(and_(legal_holds.c.id == hid, legal_holds.c.released_ms.is_(None)))
                             .values(released_by=actor, released_ms=_now(), release_note=note.strip()[:2000])).rowcount
        if not n:
            raise RecordsError("no such hold in force", 404)
        h = self._hold(hid)
        self._audit("privacy.hold_released", actor, h["dataset"], {"hold": hid, "note": note.strip()[:200]})
        return h

    def _hold(self, hid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(legal_holds).where(legal_holds.c.id == hid)).first()
        if r is None:
            raise RecordsError("no such hold", 404)
        return dict(r._mapping)

    # ---- running statements on the table's Trino connection -----------------------------------------
    def _target(self, urn: str) -> tuple[dict, str, str]:
        """``(connection definition, table name as Trino knows it, dialect)`` for a dataset on a Trino connection."""
        from sqlglot import exp

        from cairn.dataaccess.service import DataAccessError

        if self.data is None:
            raise RecordsError("warehouse connections are not configured", 503)
        try:
            source, parts = self.data.source_for(urn)
        except DataAccessError as e:
            raise RecordsError(str(e), 404)
        definition = (self.data.registry.definitions() or {}).get(source.connection) if self.data.registry else None
        if not definition or not str(definition.get("url", "")).startswith("trino://") or not definition.get("run_as_user"):
            raise RecordsError(f"{source.connection!r} is not a Trino connection that runs as the user", 409)
        tp = parts[-2:] if source.database or len(parts) < 3 else parts
        table = exp.table_(tp[-1], db=tp[-2] if len(tp) > 1 else None, catalog=tp[-3] if len(tp) > 2 else None)
        return definition, table.sql(dialect="trino"), "trino"

    def _run(self, definition: dict, sql: str) -> list[list[Any]]:
        return self.data.run_on_trino(definition, self.actor, sql, 1000)[1]

    def _need_actor(self) -> str:
        if not self.actor:
            raise RecordsError("deleting is not configured: set CAIRN_PRIVACY_ACTOR, the identity that deletes "
                               "(it needs write on the tables)", 503)
        return self.actor

    # ---- retention that deletes ---------------------------------------------------------------------
    def enforce_retention(self, now_ms: Optional[int] = None, by: Optional[str] = None) -> dict:
        from sqlglot import exp

        actor = self._need_actor()
        now = now_ms or _now()
        stats = {"deleted": 0, "tables": 0, "held": 0, "skipped": 0, "failed": 0}
        policies = [p for p in self.privacy.policies() if getattr(p, "enforce", False)]
        if not policies:
            return stats
        for urn, res, aspects in self.privacy.datasets():
            for p in policies:
                if not self.privacy._covered(p, res):
                    continue
                cutoff = now - p.days * 86_400_000
                col = self.privacy._time_column(p, aspects)
                state, deleted, detail = "skipped", None, ""
                if self.held(urn):
                    state, detail = "held", "a legal hold is on: nothing deleted"
                elif col is None:
                    detail = "no time column found; set time_column on the policy"
                else:
                    try:
                        definition, table, _ = self._target(urn)
                        cond = exp.LT(this=exp.column(col), expression=exp.func(
                            "from_iso8601_timestamp", exp.Literal.string(_iso(cutoff))))
                        rows = self._run(definition, f"DELETE FROM {table} WHERE {cond.sql(dialect='trino')}")
                        deleted = int((rows or [[0]])[0][0] or 0)
                        state, detail = "deleted", f"rows with {col} before {_iso(cutoff)}"
                    except RecordsError as e:
                        detail = str(e)[:500]
                    except Exception as e:  # noqa: BLE001 - one table refusing does not stop the others
                        state, detail = "failed", str(e)[:500]
                stats["tables" if state == "deleted" else state] += 1
                stats["deleted"] += deleted or 0
                with self.engine.begin() as conn:
                    conn.execute(retention_runs.insert().values(
                        id=uuid.uuid4().hex, dataset=urn, policy=p.id, cutoff_ms=cutoff, state=state, deleted=deleted,
                        detail=detail[:500], ran_ms=now))
                if state in ("deleted", "failed"):
                    self._audit(f"privacy.retention_{state}", by or actor, urn,
                                {"policy": p.id, "cutoff": _iso(cutoff), "deleted": deleted, "detail": detail[:200]})
        return stats

    def runs(self, limit: int = 200) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(retention_runs).order_by(retention_runs.c.ran_ms.desc()).limit(min(limit, 1000)))]

    # ---- erasure ------------------------------------------------------------------------------------
    def erase(self, rid: str, identifier: str, officer: str) -> dict:
        from sqlglot import exp

        from cairn.governance.privacy import privacy_requests

        self._need_actor()
        r = self.privacy.get(rid)
        if r["kind"] != "deletion":
            raise RecordsError("only deletion requests erase", 409)
        if r["state"] != "OPEN":
            raise RecordsError(f"this request is {r['state'].lower()}", 409)
        import hmac

        with self.engine.connect() as conn:
            stored = conn.execute(select(privacy_requests.c.identifier_hash).where(privacy_requests.c.id == rid)).scalar()
        if not hmac.compare_digest(stored, self.privacy._hash(r["identifier_kind"], identifier)):
            raise RecordsError("that is not this request's identifier", 403)
        value = identifier.strip()
        columns = {f["dataset"]: f["columns"] for f in r["findings"]}
        results = []
        for part in r.get("parts") or []:
            if part["state"] != "PENDING":
                continue
            urn = part["dataset"]
            if self.held(urn):
                holds = [h["id"] for h in self.holds(urn)]
                self._set_part(rid, urn, "HELD", officer, {"holds": holds},
                               f"kept: legal hold {', '.join(holds)}")
                results.append({"dataset": urn, "state": "HELD", "holds": holds})
                continue
            try:
                definition, table, _ = self._target(urn)
            except RecordsError as e:
                results.append({"dataset": urn, "state": "PENDING", "reason": str(e)})   # left for its owners
                continue
            conds = []
            for c in columns.get(urn) or []:
                col = exp.column(c.split(".")[-1])
                conds.append(exp.EQ(this=exp.func("LOWER", col), expression=exp.Literal.string(value.lower()))
                             if r["identifier_kind"] == "email" else exp.EQ(this=col, expression=exp.Literal.string(value)))
            if not conds:
                results.append({"dataset": urn, "state": "PENDING", "reason": "no identifier column is known"})
                continue
            where = exp.or_(*conds).sql(dialect="trino")
            try:
                before = int(self._run(definition, f"SELECT count(*) FROM {table} WHERE {where}")[0][0])
                deleted = int((self._run(definition, f"DELETE FROM {table} WHERE {where}") or [[0]])[0][0] or 0) if before else 0
                after = int(self._run(definition, f"SELECT count(*) FROM {table} WHERE {where}")[0][0])
            except Exception as e:  # noqa: BLE001 - refused or failed: the part stays to do, with the reason
                results.append({"dataset": urn, "state": "PENDING", "reason": str(e)[:300]})
                continue
            if after:
                results.append({"dataset": urn, "state": "PENDING", "reason": f"{after} rows remain after the delete"})
                continue
            expired, history = False, ""
            retention = os.environ.get("CAIRN_ERASURE_SNAPSHOT_RETENTION", "0s")
            try:
                self._run(definition, f"ALTER TABLE {table} EXECUTE expire_snapshots(retention_threshold => '{retention}')")
                expired, history = True, f"snapshots older than {retention} expired"
            except Exception as e:  # noqa: BLE001 - the rows are gone now; older snapshots stay until maintenance
                history = f"older snapshots remain until table maintenance expires them: {str(e)[:200]}"
            evidence = {"table": table, "rows_before": before, "rows_deleted": deleted, "rows_after": after,
                        "history_expired": expired, "history": history, "at": _iso(_now()), "as": self.actor}
            self._set_part(rid, urn, "DONE", officer, {"erasure": evidence}, "erased by Cairn")
            results.append({"dataset": urn, "state": "DONE", **evidence})
        self._audit("privacy.erasure_run", officer, rid, {"parts": [{k: v for k, v in x.items() if k in (
            "dataset", "state", "rows_deleted")} for x in results]})
        out = self.privacy.get(rid)
        if out["state"] == "COMPLETED":
            self._sign(rid, officer)
        return {"request": self.privacy.get(rid), "results": results}

    def _set_part(self, rid: str, dataset: str, state: str, actor: str, extra: dict, note: str) -> None:
        from cairn.governance.privacy import privacy_requests

        with self.engine.begin() as conn:
            row = conn.execute(select(privacy_requests).where(privacy_requests.c.id == rid).with_for_update()).first()
            doc = dict(row.doc)
            parts = [dict(p) for p in doc.get("parts") or []]
            for p in parts:
                if p["dataset"] == dataset and p["state"] == "PENDING":
                    p.update(state=state, note=note, done_by=actor, done_ms=_now(), **extra)
            doc["parts"] = parts
            complete = all(p["state"] != "PENDING" for p in parts)
            conn.execute(update(privacy_requests).where(privacy_requests.c.id == rid).values(
                doc=doc, state="COMPLETED" if complete else row.state))

    def _sign(self, rid: str, officer: str) -> None:
        from cairn.governance.privacy import privacy_requests

        if self.signer is None:
            return
        r = self.privacy.get(rid)
        with self.engine.connect() as conn:
            h = conn.execute(select(privacy_requests.c.identifier_hash).where(privacy_requests.c.id == rid)).scalar()
            if conn.execute(select(erasure_proofs.c.request).where(erasure_proofs.c.request == rid)).first():
                return
        doc = {"request": rid, "reference": r.get("reference") or "", "identifier_kind": r["identifier_kind"],
               "identifier_sha256": h, "opened": _iso(r["created_ms"]), "completed": _iso(_now()), "officer": officer,
               "issuer": self.signer.issuer,
               "tables": [{"dataset": p["dataset"], "state": p["state"],
                           **({k: p["erasure"][k] for k in ("table", "rows_before", "rows_deleted", "rows_after",
                                                            "history_expired", "at")} if p.get("erasure") else {}),
                           **({"holds": p["holds"]} if p.get("holds") else {}),
                           **({"marked_by": p.get("done_by"), "note": p.get("note")} if not p.get("erasure") else {})}
                          for p in r.get("parts") or []],
               "searched": [{"dataset": f["dataset"], "rows": f["rows"], "searched": f["searched"]} for f in r["findings"]]}
        jws = self.signer.sign_document(doc, PROOF_TYPE)
        with self.engine.begin() as conn:
            conn.execute(erasure_proofs.insert().values(request=rid, jws=jws, doc=doc, signed_ms=_now()))
        self._audit("privacy.erasure_proved", officer, rid, {"tables": len(doc["tables"])})
        if self.notify is not None:
            try:
                self.notify(r["created_by"], rid)
            except Exception:  # noqa: BLE001
                logger.exception("erasure notification failed")

    def proof(self, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(erasure_proofs).where(erasure_proofs.c.request == rid)).first()
        if r is None:
            raise RecordsError("no proof yet: the request is not complete", 404)
        return {"request": rid, "report": r.doc, "jws": r.jws, "keys": "/governance/privacy/proof-keys"}

    def verify(self, jws: str) -> dict:
        if self.signer is None:
            raise RecordsError("proofs are not configured: they need Trino sign-in (CAIRN_TRINO_ACCESS_KEY)", 503)
        try:
            return {"valid": True, "report": self.signer.verify_document(jws, PROOF_TYPE)}
        except ValueError as e:
            return {"valid": False, "reason": str(e)}

    def keys(self) -> dict:
        if self.signer is None:
            raise RecordsError("proofs are not configured: they need Trino sign-in (CAIRN_TRINO_ACCESS_KEY)", 503)
        return self.signer.jwks()

    def _audit(self, action: str, actor: str, subject: Optional[str], details: dict) -> None:
        if self.audit is not None:
            self.audit.record("privacy", action, actor, subject, details)


def build_records_router(rec: Records, current_principal, authorize: Callable) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/governance/privacy", tags=["privacy"])

    def officer(p) -> str:
        authorize(p, "MANAGE_POLICIES", None)
        return p.urn

    def call(fn):
        try:
            return fn()
        except RecordsError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/holds")
    def holds(dataset: Optional[str] = None, all: bool = False, p=Depends(current_principal)):
        officer(p)
        return rec.holds(dataset, active_only=not all)

    @router.post("/holds", status_code=201)
    def place(body: dict = Body(...), p=Depends(current_principal)):
        return call(lambda: rec.place(officer(p), body))

    @router.post("/holds/{hid}/release")
    def release(hid: str, body: dict = Body(...), p=Depends(current_principal)):
        return call(lambda: rec.release(officer(p), hid, str(body.get("note") or "")))

    @router.post("/retention/enforce")
    def enforce(p=Depends(current_principal)):
        who = officer(p)
        return call(lambda: rec.enforce_retention(by=who))

    @router.get("/retention/runs")
    def runs(limit: int = 200, p=Depends(current_principal)):
        officer(p)
        return rec.runs(limit)

    @router.post("/requests/{rid}/erase")
    def erase(rid: str, body: dict = Body(...), p=Depends(current_principal)):
        from cairn.governance.privacy import PrivacyError

        who = officer(p)
        try:
            return call(lambda: rec.erase(rid, str(body.get("identifier") or ""), who))
        except PrivacyError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/requests/{rid}/proof")
    def proof(rid: str, p=Depends(current_principal)):
        officer(p)
        return call(lambda: rec.proof(rid))

    @router.get("/proof-keys")
    def keys():
        return call(rec.keys)

    @router.post("/proof/verify")
    def verify(body: dict = Body(...)):
        return call(lambda: rec.verify(str(body.get("jws") or "")))

    return router
