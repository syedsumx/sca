"""Retrieval-time policy enforcement for RAG and agent memory."""

from datetime import date, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from tether.db.models import Customer, VectorRow
from tether.embeddings.fake import FakeEmbedder
from tether.pipelines import rag
from tether.pipelines.run import PipelineResult
from tether.policy.consent import PolicyUpdate, update_policy
from tether.policy.enforcement import RestrictionReason, restrict
from tether.policy.tags import ConsentStatus
from tether.stores.memory import read_memory

SUBJECT = "cust-007"


def _query_for(session: Session, subject_id: str) -> str:
    c = session.get(Customer, subject_id)
    assert c is not None
    return f"{c.name} {c.email} {c.phone}"


def _hits_subject(session: Session, embedder: FakeEmbedder, subject_id: str, k: int = 10) -> int:
    hits = rag.query(session, embedder, _query_for(session, subject_id), top_k=k)
    return sum(subject_id in h.subject_ids for h in hits)


def test_query_finds_subject_before_any_restriction(
    session: Session, demo_world: PipelineResult, embedder: FakeEmbedder
) -> None:
    hits = rag.query(session, embedder, _query_for(session, SUBJECT), top_k=5)
    assert hits[0].subject_ids == [SUBJECT]
    assert _hits_subject(session, embedder, SUBJECT) > 0


def test_consent_revocation_blocks_retrieval_immediately(
    session: Session, demo_world: PipelineResult, embedder: FakeEmbedder
) -> None:
    assert _hits_subject(session, embedder, SUBJECT) > 0
    update_policy(session, SUBJECT, PolicyUpdate(consent_status=ConsentStatus.REVOKED))
    # No purge, no re-index: the next query is already filtered.
    hits = rag.query(session, embedder, _query_for(session, SUBJECT), top_k=10)
    assert len(hits) == 10, "the filter runs before top-k, so results stay full"
    assert all(SUBJECT not in h.subject_ids for h in hits)
    # Granting consent again restores retrieval.
    update_policy(session, SUBJECT, PolicyUpdate(consent_status=ConsentStatus.GRANTED))
    assert _hits_subject(session, embedder, SUBJECT) > 0


def test_pending_deletion_blocks_retrieval_and_memory(
    session: Session, demo_world: PipelineResult, embedder: FakeEmbedder
) -> None:
    assert read_memory(session, SUBJECT)
    restrict(session, SUBJECT, RestrictionReason.DELETION_PENDING)
    assert _hits_subject(session, embedder, SUBJECT, k=50) == 0
    assert read_memory(session, SUBJECT) == []
    # Multi-subject digests that include the subject are hidden too.
    assert all(SUBJECT not in m.subject_ids for m in read_memory(session, limit=1000))
    # Unfiltered reads (admin / purge paths) still see the data.
    assert read_memory(session, SUBJECT, enforce_policy=False)


def test_deletion_restriction_survives_consent_regrant(
    session: Session, demo_world: PipelineResult, embedder: FakeEmbedder
) -> None:
    restrict(session, SUBJECT, RestrictionReason.DELETION_PENDING)
    update_policy(session, SUBJECT, PolicyUpdate(consent_status=ConsentStatus.GRANTED))
    assert _hits_subject(session, embedder, SUBJECT, k=50) == 0


def test_expired_retention_on_vector_policy_is_filtered(
    session: Session, demo_world: PipelineResult, embedder: FakeEmbedder
) -> None:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    session.execute(
        update(VectorRow)
        .where(VectorRow.subject_ids.contains([SUBJECT]))
        .values(policy=VectorRow.policy.op("||")({"retention_until": yesterday}))
    )
    assert _hits_subject(session, embedder, SUBJECT, k=50) == 0


def test_revoked_consent_in_vector_policy_is_filtered(
    session: Session, demo_world: PipelineResult, embedder: FakeEmbedder
) -> None:
    session.execute(
        update(VectorRow)
        .where(VectorRow.subject_ids.contains([SUBJECT]))
        .values(policy=VectorRow.policy.op("||")({"consent_status": "revoked"}))
    )
    assert _hits_subject(session, embedder, SUBJECT, k=50) == 0
