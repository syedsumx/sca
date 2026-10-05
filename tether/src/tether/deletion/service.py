"""Deletion workflow orchestration.

``received -> retrieval_blocked -> discovered -> purging -> purged -> models_flagged -> certified``
(or ``failed``, recording the step that failed so a retry resumes there).

* :func:`submit` runs inside the API request. It creates the request and blocks
  retrieval **synchronously**, so content is hidden before the HTTP response returns.
  The remaining steps run on the job queue.
* :func:`process` advances one request step by step. Each step commits on its own, which
  makes progress visible live and lets a retry resume. Each step is idempotent.
* Submitting again for the same subject is a no-op that returns the existing request
  (and its certificate, once it is certified). A failed request is re-queued.
"""

import logging
import time
import traceback
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tether import __version__, audit
from tether.config import get_settings
from tether.db.models import (
    DeletionCertificateRow,
    DeletionRequestRow,
    LineageEdge,
    LineageNode,
    ModelVersionRow,
)
from tether.db.session import get_session_factory, session_scope
from tether.deletion import queue
from tether.deletion.certificate import CANONICALIZATION, certificate_hash
from tether.deletion.purgers import PURGERS, purge_nodes, sweep_untracked
from tether.deletion.states import DeletionStatus
from tether.lineage.ids import EdgeType, NodeType, node_id
from tether.lineage.query import models_trained_on_subject, subject_artifact_ids
from tether.policy.enforcement import RestrictionReason, restrict, unrestrict

log = logging.getLogger("tether.deletion")

JOB_KIND = "deletion.process"


class DeletionNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class SubmitResult:
    request: DeletionRequestRow
    created: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _transition(
    session: Session, req: DeletionRequestRow, status: DeletionStatus, **detail: Any
) -> None:
    """Change status, append to history, and write a log line and an audit entry."""
    previous = req.status
    req.status = status.value
    at = _now()
    req.history = [*req.history, {"status": status.value, "at": at.isoformat(), **detail}]
    if status == DeletionStatus.CERTIFIED:
        req.completed_at = at
    session.flush()
    log.info(
        "deletion request %s (subject=%s): %s -> %s",
        req.id,
        req.subject_id,
        previous,
        status.value,
    )
    audit.append(
        session,
        f"deletion.{status.value}",
        {"request_id": req.id, "from": previous, "to": status.value, **detail},
        subject_id=req.subject_id,
        entity_id=req.id,
    )


# --------------------------------------------------------------------------- submit


def submit(session: Session, subject_id: str, reason: str | None = None) -> SubmitResult:
    """Create (or return) the deletion request for ``subject_id`` and block retrieval now."""
    request_id = str(uuid.uuid4())
    inserted = session.execute(
        insert(DeletionRequestRow)
        .values(
            id=request_id,
            subject_id=subject_id,
            status=DeletionStatus.RECEIVED.value,
            reason=reason,
            inventory={},
            purged={},
            flagged={},
            history=[],
        )
        .on_conflict_do_nothing(index_elements=[DeletionRequestRow.subject_id])
        .returning(DeletionRequestRow.id)
    ).scalar_one_or_none()

    req = session.scalars(
        select(DeletionRequestRow)
        .where(DeletionRequestRow.subject_id == subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()

    if inserted is None:
        # Idempotent re-submission.
        if req.status == DeletionStatus.FAILED.value:
            log.info("re-queueing failed deletion request %s", req.id)
            audit.append(
                session, "deletion.retry_requested", {"request_id": req.id},
                subject_id=subject_id, entity_id=req.id,
            )  # fmt: skip
            queue.enqueue(session, JOB_KIND, {"request_id": req.id})
        return SubmitResult(req, created=False)

    _transition(session, req, DeletionStatus.RECEIVED, reason=reason)
    _block(session, req)
    queue.enqueue(session, JOB_KIND, {"request_id": req.id})
    return SubmitResult(req, created=True)


def retry(session: Session, request_id: str) -> DeletionRequestRow:
    req = get_request(session, request_id)
    if req.status == DeletionStatus.FAILED.value:
        audit.append(
            session, "deletion.retry_requested", {"request_id": req.id},
            subject_id=req.subject_id, entity_id=req.id,
        )  # fmt: skip
    if req.status != DeletionStatus.CERTIFIED.value:
        queue.enqueue(session, JOB_KIND, {"request_id": req.id})
    return req


def get_request(session: Session, request_id: str) -> DeletionRequestRow:
    req = session.get(DeletionRequestRow, request_id)
    if req is None:
        raise DeletionNotFoundError(request_id)
    return req


def get_certificate(session: Session, request_id: str) -> DeletionCertificateRow | None:
    return session.scalars(
        select(DeletionCertificateRow).where(DeletionCertificateRow.request_id == request_id)
    ).first()


# --------------------------------------------------------------------------- steps


def _block(session: Session, req: DeletionRequestRow) -> None:
    restrict(session, req.subject_id, RestrictionReason.DELETION_PENDING, f"request {req.id}")
    _transition(session, req, DeletionStatus.RETRIEVAL_BLOCKED)


def _discover(session: Session, req: DeletionRequestRow) -> None:
    ids = subject_artifact_ids(session, req.subject_id)
    nodes = list(session.scalars(select(LineageNode).where(LineageNode.id.in_(ids))))
    by_type: dict[str, list[str]] = {}
    for n in sorted(nodes, key=lambda n: n.id):
        by_type.setdefault(n.node_type, []).append(n.id)
    models = models_trained_on_subject(session, req.subject_id)
    previous = req.inventory.get("artifacts", {})
    # On a retry, keep everything found earlier: a purged node stays on the list.
    merged = {
        t: sorted(set(previous.get(t, [])) | set(by_type.get(t, [])))
        for t in set(previous) | set(by_type)
    }
    req.inventory = {
        "artifacts": merged,
        "counts": {t: len(v) for t, v in sorted(merged.items())},
        "models": sorted(m.id for m in models),
        "discovered_at": _now().isoformat(),
    }
    _transition(session, req, DeletionStatus.DISCOVERED, counts=req.inventory["counts"])


def _purge(session: Session, req: DeletionRequestRow) -> None:
    if req.status != DeletionStatus.PURGING.value:
        _transition(session, req, DeletionStatus.PURGING)
    artifacts: dict[str, list[str]] = req.inventory.get("artifacts", {})
    targets = [nid for t, ids in artifacts.items() if t in PURGERS for nid in ids]
    outcomes = purge_nodes(session, targets, req.subject_id)
    untracked = sweep_untracked(session, req.subject_id)
    # Keep the first recorded outcome per node, so a retry never overwrites the evidence
    # that rows were deleted with a later "0 rows" outcome.
    previous = {i["node_id"]: i for i in req.purged.get("items", [])}
    items = {o.node_id: o.to_json() for o in outcomes} | previous
    prev_untracked: dict[str, int] = req.purged.get("untracked", {})
    req.purged = {
        "items": sorted(items.values(), key=lambda i: (i["type"], i["node_id"])),
        "untracked": dict(Counter(prev_untracked) + Counter(untracked)),
        "purged_at": _now().isoformat(),
    }
    subject_node = session.get(LineageNode, node_id(NodeType.SUBJECT, req.subject_id))
    if subject_node is not None:
        subject_node.status = "deleted"
    counts = Counter(i["type"] for i in req.purged["items"])
    _transition(session, req, DeletionStatus.PURGED, counts=dict(sorted(counts.items())))


def _flag_models(session: Session, req: DeletionRequestRow) -> None:
    flagged = []
    for model_node in models_trained_on_subject(session, req.subject_id):
        snapshots = sorted(
            session.scalars(
                select(LineageEdge.dst_id)
                .join(LineageNode, LineageNode.id == LineageEdge.dst_id)
                .where(
                    LineageEdge.src_id == model_node.id,
                    LineageEdge.edge_type == EdgeType.TRAINED_ON.value,
                    LineageNode.subject_ids.contains([req.subject_id]),
                )
            )
        )
        reason = {
            "request_id": req.id,
            "subject_id": req.subject_id,
            "snapshots": snapshots,
            "reason": (
                f"trained on snapshot(s) {', '.join(s.split(':', 1)[1] for s in snapshots)} "
                f"containing deleted subject {req.subject_id}"
            ),
            "flagged_at": _now().isoformat(),
        }
        mv = session.get(ModelVersionRow, model_node.ref)
        if mv is not None:
            if not any(r.get("request_id") == req.id for r in mv.retraining_reasons):
                mv.retraining_reasons = [*mv.retraining_reasons, reason]
            mv.status = "needs_retraining"
        model_node.status = "needs_retraining"
        flagged.append({"model": model_node.ref, **reason})
    req.flagged = {"models": flagged, "flagged_at": _now().isoformat()}
    _transition(session, req, DeletionStatus.MODELS_FLAGGED, models=[f["model"] for f in flagged])


def _certify(session: Session, req: DeletionRequestRow) -> DeletionCertificateRow:
    existing = get_certificate(session, req.id)
    if existing is not None:
        if req.status != DeletionStatus.CERTIFIED.value:
            _transition(session, req, DeletionStatus.CERTIFIED, sha256=existing.sha256)
        return existing
    issued_at = _now()
    cert_id = str(uuid.uuid4())
    items: list[dict[str, Any]] = req.purged.get("items", [])
    content: dict[str, Any] = {
        "certificate_id": cert_id,
        "request_id": req.id,
        "subject_id": req.subject_id,
        "issued_at": issued_at.isoformat(),
        "issuer": f"tether/{__version__}",
        "timeline": req.history,
        "purged": {
            "items": items,
            "counts": dict(sorted(Counter(i["type"] for i in items).items())),
            "rows_deleted": sum(int(i["rows_deleted"]) for i in items),
            "untracked_rows_deleted": req.purged.get("untracked", {}),
        },
        "memory_needing_regeneration": [
            i["node_id"] for i in items if i["action"] == "deleted_needs_regeneration"
        ],
        "models_flagged_for_retraining": req.flagged.get("models", []),
        "retained": {
            "dataset_snapshots": req.inventory.get("artifacts", {}).get(
                NodeType.SNAPSHOT.value, []
            ),
            "note": (
                "Snapshot manifests are immutable training history and are retained; "
                "models trained on them are flagged for retraining. Lineage tombstones keep "
                "only the pseudonymous subject id, no content."
            ),
        },
        "verification": {"algorithm": "sha256", "canonicalization": CANONICALIZATION},
    }
    sha = certificate_hash(content)
    cert = DeletionCertificateRow(
        id=cert_id,
        request_id=req.id,
        subject_id=req.subject_id,
        issued_at=issued_at,
        content=content,
        sha256=sha,
    )
    session.add(cert)
    restrict(session, req.subject_id, RestrictionReason.DELETED, f"certificate {cert_id}")
    unrestrict(session, req.subject_id, RestrictionReason.DELETION_PENDING)
    _transition(session, req, DeletionStatus.CERTIFIED, certificate_id=cert_id, sha256=sha)
    return cert


STEPS: dict[DeletionStatus, Callable[[Session, DeletionRequestRow], Any]] = {
    DeletionStatus.RECEIVED: _block,
    DeletionStatus.RETRIEVAL_BLOCKED: _discover,
    DeletionStatus.DISCOVERED: _purge,
    DeletionStatus.PURGING: _purge,
    DeletionStatus.PURGED: _flag_models,
    DeletionStatus.MODELS_FLAGGED: _certify,
}


def _resume_status(req: DeletionRequestRow) -> DeletionStatus:
    if req.status != DeletionStatus.FAILED.value:
        return DeletionStatus(req.status)
    return DeletionStatus((req.error or {}).get("failed_in_status", DeletionStatus.RECEIVED.value))


def process(
    request_id: str,
    factory: sessionmaker[Session] | None = None,
    *,
    step_delay: float | None = None,
) -> DeletionStatus:
    """Advance a request to ``certified``. Safe to call repeatedly or concurrently."""
    factory = factory or get_session_factory()
    delay = get_settings().deletion_step_delay if step_delay is None else step_delay
    while True:
        failed_in: DeletionStatus | None = None
        try:
            with session_scope(factory) as session:
                req = session.scalars(
                    select(DeletionRequestRow)
                    .where(DeletionRequestRow.id == request_id)
                    .with_for_update()
                ).first()
                if req is None:
                    raise DeletionNotFoundError(request_id)
                current = _resume_status(req)
                if current == DeletionStatus.CERTIFIED:
                    return current
                failed_in = current
                if req.status == DeletionStatus.FAILED.value:
                    req.status = current.value
                    req.error = None
                req.attempts += 1
                STEPS[current](session, req)
                new_status = DeletionStatus(req.status)
        except DeletionNotFoundError:
            raise
        except Exception as exc:
            _mark_failed(factory, request_id, failed_in, exc)
            raise
        if new_status == DeletionStatus.CERTIFIED:
            return new_status
        if delay:
            time.sleep(delay)


def _mark_failed(
    factory: sessionmaker[Session],
    request_id: str,
    failed_in: DeletionStatus | None,
    exc: BaseException,
) -> None:
    with session_scope(factory) as session:
        req = session.get(DeletionRequestRow, request_id, with_for_update=True)
        if req is None:
            return
        req.error = {
            "failed_in_status": (failed_in or DeletionStatus.RECEIVED).value,
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=5),
            "at": _now().isoformat(),
        }
        log.exception("deletion request %s failed in %s", request_id, failed_in)
        _transition(
            session,
            req,
            DeletionStatus.FAILED,
            failed_in_status=req.error["failed_in_status"],
            error=f"{type(exc).__name__}: {exc}",
        )
