"""ORM models.

One Postgres database holds everything in the MVP:

* demo source data: ``customers``, ``support_tickets``, ``policy_tags``
* AI artifact stores: ``chunks``, ``vectors`` (pgvector), ``memory_entries``,
  ``dataset_snapshots``, ``model_versions``
* the lineage graph: ``lineage_nodes``, ``lineage_edges``, ``subject_artifacts`` (index), and
  ``lineage_events`` (raw OpenLineage events)
* governance: ``subject_restrictions``, ``deletion_requests``, ``deletion_certificates``,
  ``audit_log``, ``jobs``
"""

from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tether.config import get_settings

EMBEDDING_DIM = get_settings().embedding_dim

StrArray = ARRAY(String)


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- source data


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(50))
    address: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = _now()


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), ForeignKey("customers.id"), index=True)
    category: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class PolicyTagRow(Base):
    __tablename__ = "policy_tags"

    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    consent_status: Mapped[str] = mapped_column(String(20), default="granted")
    license: Mapped[str | None] = mapped_column(String(50), nullable=True)
    retention_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SubjectRestriction(Base):
    """Live block list consulted by every retrieval path.

    reason: ``consent_revoked`` | ``deletion_pending`` | ``deleted``
    """

    __tablename__ = "subject_restrictions"

    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32), primary_key=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- lineage graph


class LineageNode(Base):
    __tablename__ = "lineage_nodes"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    ref: Mapped[str] = mapped_column(String(512))
    subject_ids: Mapped[list[str]] = mapped_column(StrArray, default=list)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # active | purged | needs_regeneration | needs_retraining
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_lineage_nodes_subject_ids", "subject_ids", postgresql_using="gin"),)


class LineageEdge(Base):
    """Directed edge ``src -[edge_type]-> dst``.

    * ``derived_from``: child -> parent (chunk -> source record, vector -> chunk, ...)
    * ``stored_in``: vector -> collection
    * ``contained_in``: source record -> dataset snapshot
    * ``trained_on``: model version -> dataset snapshot
    """

    __tablename__ = "lineage_edges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    src_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("lineage_nodes.id", ondelete="CASCADE"), index=True
    )
    dst_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("lineage_nodes.id", ondelete="CASCADE"), index=True
    )
    edge_type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _now()

    __table_args__ = (UniqueConstraint("src_id", "dst_id", "edge_type", name="uq_lineage_edge"),)


class SubjectArtifact(Base):
    """Precomputed index: subject -> every artifact carrying the subject id."""

    __tablename__ = "subject_artifacts"

    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("lineage_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    node_type: Mapped[str] = mapped_column(String(32))

    __table_args__ = (Index("ix_subject_artifacts_node_id", "node_id"),)


class LineageEventRow(Base):
    __tablename__ = "lineage_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    job_name: Mapped[str] = mapped_column(String(200), index=True)
    run_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    exported: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- AI artifacts


class ChunkRow(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    source_record_id: Mapped[str] = mapped_column(String(512), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    subject_ids: Mapped[list[str]] = mapped_column(StrArray)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_chunks_subject_ids", "subject_ids", postgresql_using="gin"),)


class VectorRow(Base):
    __tablename__ = "vectors"

    collection: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    chunk_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    subject_ids: Mapped[list[str]] = mapped_column(StrArray)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_vectors_subject_ids", "subject_ids", postgresql_using="gin"),)


class MemoryEntryRow(Base):
    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    content: Mapped[str] = mapped_column(Text)
    subject_ids: Mapped[list[str]] = mapped_column(StrArray)
    derived_from: Mapped[list[str]] = mapped_column(StrArray)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        Index("ix_memory_entries_subject_ids", "subject_ids", postgresql_using="gin"),
    )


class DatasetSnapshotRow(Base):
    __tablename__ = "dataset_snapshots"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject_ids: Mapped[list[str]] = mapped_column(StrArray)
    record_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        Index("ix_dataset_snapshots_subject_ids", "subject_ids", postgresql_using="gin"),
    )


class ModelVersionRow(Base):
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(50))
    trained_on: Mapped[list[str]] = mapped_column(StrArray)
    # ok | needs_retraining
    status: Mapped[str] = mapped_column(String(32), default="ok")
    retraining_reasons: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("name", "version", name="uq_model_name_version"),)


# --------------------------------------------------------------------------- governance


class DeletionRequestRow(Base):
    __tablename__ = "deletion_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # One request per subject: re-submitting is idempotent and returns this row.
    subject_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    inventory: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    purged: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    flagged: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    history: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeletionCertificateRow(Base):
    __tablename__ = "deletion_certificates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("deletion_requests.id"), unique=True
    )
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))


class AuditLogRow(Base):
    """Append-only, hash-chained audit log (UPDATE/DELETE blocked by a trigger)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class JobRow(Base):
    """Minimal Postgres-backed job queue (claimed with FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime] = _now()
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)
