"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-05 22:30:24.019072
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION tether_audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TRIGGER = """
CREATE TRIGGER audit_log_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION tether_audit_log_append_only();
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=256), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_event_type"), "audit_log", ["event_type"], unique=False)
    op.create_index(op.f("ix_audit_log_subject_id"), "audit_log", ["subject_id"], unique=False)
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(length=512), nullable=False),
        sa.Column("source_record_id", sa.String(length=512), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("subject_ids", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chunks_source_record_id"), "chunks", ["source_record_id"], unique=False
    )
    op.create_index(
        "ix_chunks_subject_ids", "chunks", ["subject_ids"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=False),
        sa.Column("address", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "dataset_snapshots",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject_ids", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dataset_snapshots_subject_ids",
        "dataset_snapshots",
        ["subject_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("purged", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("flagged", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id"),
    )
    op.create_index(
        op.f("ix_deletion_requests_status"), "deletion_requests", ["status"], unique=False
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"], unique=False)
    op.create_table(
        "lineage_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("job_name", sa.String(length=200), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exported", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_lineage_events_event_time"), "lineage_events", ["event_time"], unique=False
    )
    op.create_index(
        op.f("ix_lineage_events_job_name"), "lineage_events", ["job_name"], unique=False
    )
    op.create_table(
        "lineage_nodes",
        sa.Column("id", sa.String(length=512), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("ref", sa.String(length=512), nullable=False),
        sa.Column("subject_ids", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attrs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_lineage_nodes_node_type"), "lineage_nodes", ["node_type"], unique=False
    )
    op.create_index(op.f("ix_lineage_nodes_status"), "lineage_nodes", ["status"], unique=False)
    op.create_index(
        "ix_lineage_nodes_subject_ids",
        "lineage_nodes",
        ["subject_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "memory_entries",
        sa.Column("id", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("subject_ids", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("derived_from", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memory_entries_subject_ids",
        "memory_entries",
        ["subject_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("trained_on", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("retraining_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_model_name_version"),
    )
    op.create_table(
        "policy_tags",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("consent_status", sa.String(length=20), nullable=False),
        sa.Column("license", sa.String(length=50), nullable=True),
        sa.Column("retention_until", sa.Date(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("subject_id"),
    )
    op.create_table(
        "subject_restrictions",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("subject_id", "reason"),
    )
    op.create_table(
        "vectors",
        sa.Column("collection", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=256), nullable=False),
        sa.Column("chunk_id", sa.String(length=512), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
        sa.Column("subject_ids", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("collection", "id"),
    )
    op.create_index(
        "ix_vectors_subject_ids", "vectors", ["subject_ids"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "deletion_certificates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["deletion_requests.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index(
        op.f("ix_deletion_certificates_subject_id"),
        "deletion_certificates",
        ["subject_id"],
        unique=False,
    )
    op.create_table(
        "lineage_edges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("src_id", sa.String(length=512), nullable=False),
        sa.Column("dst_id", sa.String(length=512), nullable=False),
        sa.Column("edge_type", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dst_id"], ["lineage_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["src_id"], ["lineage_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("src_id", "dst_id", "edge_type", name="uq_lineage_edge"),
    )
    op.create_index(op.f("ix_lineage_edges_dst_id"), "lineage_edges", ["dst_id"], unique=False)
    op.create_index(op.f("ix_lineage_edges_src_id"), "lineage_edges", ["src_id"], unique=False)
    op.create_table(
        "subject_artifacts",
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=512), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["lineage_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subject_id", "node_id"),
    )
    op.create_index("ix_subject_artifacts_node_id", "subject_artifacts", ["node_id"], unique=False)
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_support_tickets_customer_id"), "support_tickets", ["customer_id"], unique=False
    )
    op.execute(APPEND_ONLY_FN)
    op.execute(APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS tether_audit_log_append_only()")
    op.drop_index(op.f("ix_support_tickets_customer_id"), table_name="support_tickets")
    op.drop_table("support_tickets")
    op.drop_index("ix_subject_artifacts_node_id", table_name="subject_artifacts")
    op.drop_table("subject_artifacts")
    op.drop_index(op.f("ix_lineage_edges_src_id"), table_name="lineage_edges")
    op.drop_index(op.f("ix_lineage_edges_dst_id"), table_name="lineage_edges")
    op.drop_table("lineage_edges")
    op.drop_index(op.f("ix_deletion_certificates_subject_id"), table_name="deletion_certificates")
    op.drop_table("deletion_certificates")
    op.drop_index("ix_vectors_subject_ids", table_name="vectors", postgresql_using="gin")
    op.drop_table("vectors")
    op.drop_table("subject_restrictions")
    op.drop_table("policy_tags")
    op.drop_table("model_versions")
    op.drop_index(
        "ix_memory_entries_subject_ids", table_name="memory_entries", postgresql_using="gin"
    )
    op.drop_table("memory_entries")
    op.drop_index(
        "ix_lineage_nodes_subject_ids", table_name="lineage_nodes", postgresql_using="gin"
    )
    op.drop_index(op.f("ix_lineage_nodes_status"), table_name="lineage_nodes")
    op.drop_index(op.f("ix_lineage_nodes_node_type"), table_name="lineage_nodes")
    op.drop_table("lineage_nodes")
    op.drop_index(op.f("ix_lineage_events_job_name"), table_name="lineage_events")
    op.drop_index(op.f("ix_lineage_events_event_time"), table_name="lineage_events")
    op.drop_table("lineage_events")
    op.drop_index("ix_jobs_status_run_after", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_deletion_requests_status"), table_name="deletion_requests")
    op.drop_table("deletion_requests")
    op.drop_index(
        "ix_dataset_snapshots_subject_ids", table_name="dataset_snapshots", postgresql_using="gin"
    )
    op.drop_table("dataset_snapshots")
    op.drop_table("customers")
    op.drop_index("ix_chunks_subject_ids", table_name="chunks", postgresql_using="gin")
    op.drop_index(op.f("ix_chunks_source_record_id"), table_name="chunks")
    op.drop_table("chunks")
    op.drop_index(op.f("ix_audit_log_subject_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_event_type"), table_name="audit_log")
    op.drop_table("audit_log")
