"""End-to-end deletion workflow: submit -> block -> discover -> purge -> flag -> certify."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from tether import audit
from tether.db.models import (
    AuditLogRow,
    Customer,
    DatasetSnapshotRow,
    DeletionRequestRow,
    JobRow,
    LineageNode,
    MemoryEntryRow,
    ModelVersionRow,
)
from tether.deletion import purgers, service, worker
from tether.deletion.certificate import verify_certificate
from tether.deletion.states import DeletionStatus
from tether.deletion.verify import residual_references
from tether.embeddings.fake import FakeEmbedder
from tether.pipelines import rag
from tether.pipelines.memory import regenerate_flagged_memory
from tether.pipelines.run import PipelineResult

SUBJECT = "cust-007"


def _identifiers(session: Session, subject_id: str) -> list[str]:
    c = session.get(Customer, subject_id)
    assert c is not None
    return [c.email, c.phone]


def _expected_models(session: Session, subject_id: str) -> set[str]:
    snaps = {
        s.id for s in session.scalars(select(DatasetSnapshotRow)) if subject_id in s.subject_ids
    }
    return {m.id for m in session.scalars(select(ModelVersionRow)) if set(m.trained_on) & snaps}


def _multi_subject_memory(session: Session, subject_id: str) -> set[str]:
    return {
        m.id
        for m in session.scalars(select(MemoryEntryRow))
        if subject_id in m.subject_ids and len(m.subject_ids) > 1
    }


def test_full_deletion_workflow(
    session: Session,
    session_factory: sessionmaker[Session],
    demo_world: PipelineResult,
    embedder: FakeEmbedder,
) -> None:
    ids = _identifiers(session, SUBJECT)
    query = " ".join(ids)
    expected_models = _expected_models(session, SUBJECT)
    multi_memory = _multi_subject_memory(session, SUBJECT)
    assert expected_models and multi_memory, "fixture must exercise models and shared memory"
    assert any(SUBJECT in h.subject_ids for h in rag.query(session, embedder, query, top_k=10))
    session.commit()

    # --- submit: retrieval is blocked before the call returns
    result = service.submit(session, SUBJECT, reason="GDPR art. 17")
    session.commit()
    assert result.created
    req_id = result.request.id
    assert result.request.status == DeletionStatus.RETRIEVAL_BLOCKED

    # --- the window between the request and the purge: data exists but cannot be retrieved
    assert residual_references(session, SUBJECT)["vectors"] > 0
    hits = rag.query(session, embedder, query, top_k=20)
    assert hits and all(SUBJECT not in h.subject_ids for h in hits)

    # --- run the job
    assert worker.drain(session_factory) == 1
    session.expire_all()
    req = session.get(DeletionRequestRow, req_id)
    assert req is not None
    assert req.status == DeletionStatus.CERTIFIED
    assert [h["status"] for h in req.history] == [s.value for s in service.DeletionStatus][:7]

    # --- nothing references the subject any more
    residual = residual_references(session, SUBJECT, ids)
    assert residual == dict.fromkeys(residual, 0), residual
    assert all(SUBJECT not in h.subject_ids for h in rag.query(session, embedder, query, top_k=50))

    # --- exactly the right models are flagged
    flagged = {
        m.id for m in session.scalars(select(ModelVersionRow)) if m.status == "needs_retraining"
    }
    assert flagged == expected_models
    for m in session.scalars(select(ModelVersionRow).where(ModelVersionRow.id.in_(flagged))):
        assert m.retraining_reasons[0]["subject_id"] == SUBJECT
        assert SUBJECT in m.retraining_reasons[0]["reason"]

    # --- multi-subject memory removed and marked for regeneration
    for mem_id in multi_memory:
        assert session.get(MemoryEntryRow, mem_id) is None
        node = session.get(LineageNode, f"memory:{mem_id}")
        assert node is not None
        assert node.status == "needs_regeneration"
        assert SUBJECT not in node.attrs["remaining_subject_ids"]

    # --- certificate verifies and matches what happened
    cert = service.get_certificate(session, req_id)
    assert cert is not None
    assert verify_certificate(cert.content, cert.sha256)
    assert {f["model"] for f in cert.content["models_flagged_for_retraining"]} == {
        m.split(":", 1)[-1] for m in expected_models
    }
    assert set(cert.content["memory_needing_regeneration"]) == {f"memory:{m}" for m in multi_memory}
    assert cert.content["purged"]["counts"]["vector"] > 0
    tampered = {**cert.content, "subject_id": "someone-else"}
    assert not verify_certificate(tampered, cert.sha256)

    # --- audit log is intact and recorded every state change
    assert audit.verify_chain(session).ok
    events = session.scalars(
        select(AuditLogRow.event_type)
        .where(AuditLogRow.entity_id == req_id)
        .order_by(AuditLogRow.id)
    ).all()
    assert events == [f"deletion.{s}" for s in [h["status"] for h in req.history]]

    # --- re-running is a no-op returning the same request and certificate
    jobs_before = session.scalar(select(func.count()).select_from(JobRow))
    again = service.submit(session, SUBJECT)
    session.commit()
    assert not again.created
    assert again.request.id == req_id
    assert session.scalar(select(func.count()).select_from(JobRow)) == jobs_before
    assert service.process(req_id, session_factory) == DeletionStatus.CERTIFIED
    cert_again = service.get_certificate(session, req_id)
    assert cert_again is not None
    assert cert_again.sha256 == cert.sha256

    # --- flagged digests can be regenerated without the deleted subject
    rebuilt = regenerate_flagged_memory(session)
    assert rebuilt
    for node_id in rebuilt:
        node = session.get(LineageNode, node_id)
        assert node is not None
        assert node.status == "active"
        assert SUBJECT not in node.subject_ids


def test_failed_step_records_error_and_retry_resumes(
    session: Session,
    session_factory: sessionmaker[Session],
    demo_world: PipelineResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    real = purgers.PURGERS["vector"]

    def flaky(s: Session, node: LineageNode, subject_id: str) -> purgers.PurgeOutcome:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("vector store unavailable")
        return real(s, node, subject_id)

    monkeypatch.setitem(purgers.PURGERS, "vector", flaky)
    req_id = service.submit(session, SUBJECT).request.id
    session.commit()

    with pytest.raises(RuntimeError):
        service.process(req_id, session_factory)
    session.expire_all()
    req = session.get(DeletionRequestRow, req_id)
    assert req is not None
    assert req.status == DeletionStatus.FAILED
    assert req.error is not None
    assert req.error["failed_in_status"] in {"discovered", "purging"}
    assert "vector store unavailable" in req.error["message"]
    # Retrieval stays blocked while the request is failed.
    assert residual_references(session, SUBJECT)["vectors"] > 0
    assert all(
        SUBJECT not in h.subject_ids
        for h in rag.query(session, FakeEmbedder(), " ".join(_identifiers(session, SUBJECT)), 30)
    )

    # Re-submitting a failed request re-queues it, and the worker finishes the job.
    again = service.submit(session, SUBJECT)
    session.commit()
    assert not again.created
    worker.drain(session_factory)
    session.expire_all()
    req = session.get(DeletionRequestRow, req_id)
    assert req is not None
    assert req.status == DeletionStatus.CERTIFIED
    assert req.error is None
    assert residual_references(session, SUBJECT) == dict.fromkeys(
        residual_references(session, SUBJECT), 0
    )
    statuses = [h["status"] for h in req.history]
    assert "failed" in statuses and statuses[-1] == "certified"


def test_worker_retries_failed_jobs_with_backoff(
    session: Session,
    session_factory: sessionmaker[Session],
    demo_world: PipelineResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(s: Session, req: DeletionRequestRow) -> None:
        raise RuntimeError("boom")

    monkeypatch.setitem(service.STEPS, DeletionStatus.PURGED, broken)
    req_id = service.submit(session, SUBJECT).request.id
    session.commit()
    worker.run_once(session_factory)
    job = session.scalars(select(JobRow)).one()
    session.refresh(job)
    assert job.status == "queued" and job.attempts == 1 and "boom" in (job.last_error or "")
    req = session.get(DeletionRequestRow, req_id)
    assert req is not None
    session.refresh(req)
    assert req.status == "failed"
    assert req.error is not None and req.error["failed_in_status"] == "purged"


def test_audit_log_is_append_only(session: Session) -> None:
    audit.append(session, "test.event", {"x": 1})
    session.commit()
    with pytest.raises(DBAPIError):
        session.execute(text("UPDATE audit_log SET payload = '{}'::jsonb"))
    session.rollback()
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM audit_log"))
    session.rollback()
    assert audit.verify_chain(session).ok


def test_unknown_subject_still_certifies_cleanly(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    req_id = service.submit(session, "nobody").request.id
    session.commit()
    worker.drain(session_factory)
    cert = service.get_certificate(session, req_id)
    assert cert is not None
    assert cert.content["purged"]["rows_deleted"] == 0
    assert verify_certificate(cert.content, cert.sha256)
