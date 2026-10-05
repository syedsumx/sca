"""The demo pipelines record correct lineage through the SDK."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tether.db.models import (
    ChunkRow,
    DatasetSnapshotRow,
    LineageNode,
    MemoryEntryRow,
    ModelVersionRow,
    VectorRow,
)
from tether.pipelines.rag import chunk_text
from tether.pipelines.run import PipelineResult


def test_chunker_packs_sentences() -> None:
    text = "One short sentence. " * 20
    chunks = chunk_text(text, max_chars=60)
    assert all(len(c) <= 60 for c in chunks)
    assert " ".join(chunks) == text.strip()


def test_rag_vectors_carry_subjects_and_link_to_chunks(
    session: Session, demo_world: PipelineResult
) -> None:
    assert demo_world.rag.vectors == demo_world.rag.chunks > demo_world.rag.records
    vec = session.scalars(select(VectorRow).limit(1)).one()
    chunk = session.get(ChunkRow, vec.chunk_id)
    assert chunk is not None
    assert vec.subject_ids == chunk.subject_ids
    assert len(vec.subject_ids) == 1
    node = session.get(LineageNode, f"vector:{vec.collection}/{vec.id}")
    assert node is not None
    assert node.subject_ids == vec.subject_ids


def test_revoked_subject_is_never_ingested(session: Session, demo_world: PipelineResult) -> None:
    # cust-013 has consent revoked in the seed data.
    assert demo_world.rag.skipped_restricted > 0
    count = session.scalar(
        select(func.count())
        .select_from(VectorRow)
        .where(VectorRow.subject_ids.contains(["cust-013"]))
    )
    assert count == 0
    full = session.get(DatasetSnapshotRow, "support-tickets-full-v1")
    assert full is not None
    assert "cust-013" not in full.subject_ids
    assert full.manifest["excluded"]


def test_training_models_and_snapshots(session: Session, demo_world: PipelineResult) -> None:
    assert sorted(demo_world.training.models) == [
        "model:support-assistant@1.0",
        "model:support-assistant@1.1",
        "model:ticket-router@0.3",
    ]
    cohort_b = session.get(DatasetSnapshotRow, "support-tickets-cohort-b-v1")
    assert cohort_b is not None
    assert all(int(s.split("-")[1]) >= 26 for s in cohort_b.subject_ids)
    assert session.scalar(select(func.count()).select_from(ModelVersionRow)) == 3


def test_memory_has_single_and_multi_subject_entries(
    session: Session, demo_world: PipelineResult
) -> None:
    entries = session.scalars(select(MemoryEntryRow)).all()
    summaries = [e for e in entries if e.kind == "customer_summary"]
    digests = [e for e in entries if e.kind == "digest"]
    assert summaries and all(len(e.subject_ids) == 1 for e in summaries)
    assert any(len(e.subject_ids) > 1 for e in digests)
    for e in digests:
        assert e.derived_from and all(d.startswith("source_record:") for d in e.derived_from)
