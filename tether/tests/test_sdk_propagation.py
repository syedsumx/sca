"""SDK propagation rules: subjects and policies flow down every transformation."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import tether
from tether.db.models import (
    DatasetSnapshotRow,
    LineageEdge,
    LineageEventRow,
    LineageNode,
    ModelVersionRow,
    PolicyTagRow,
    SubjectArtifact,
)
from tether.lineage.ids import NodeType
from tether.policy.enforcement import RestrictionReason, restrict
from tether.policy.tags import ConsentStatus, PolicyTags
from tether.sdk import LineageInputError, UnknownArtifactError

ALICE = {"consent_status": "granted", "license": "internal", "retention_until": "2030-01-01"}
BOB = {"consent_status": "pending", "license": "confidential", "retention_until": "2028-01-01"}


def test_record_source_creates_subjects_and_edges(session: Session) -> None:
    ref = tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    assert ref.id == "source_record:tickets/1"
    assert ref.node_type == NodeType.SOURCE_RECORD
    assert ref.subject_ids == ("alice",)
    assert ref.policy.license == "internal"
    edge = session.scalars(select(LineageEdge)).one()
    assert (edge.src_id, edge.dst_id, edge.edge_type) == (
        "source_record:tickets/1",
        "subject:alice",
        "derived_from",
    )


def test_record_source_defaults_to_policy_table_and_fails_closed(session: Session) -> None:
    session.add(PolicyTagRow(subject_id="alice", consent_status="granted", license="public"))
    session.flush()
    known = tether.record_source("t/1", ["alice"], session=session)
    assert known.policy == PolicyTags(consent_status=ConsentStatus.GRANTED, license="public")
    unknown = tether.record_source("t/2", ["nobody"], session=session)
    assert unknown.policy.consent_status == ConsentStatus.PENDING


def test_chunk_inherits_from_source(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    chunks = tether.track_chunks("tickets/1", ["hello", "world"], session=session)
    assert [c.id for c in chunks] == ["chunk:tickets/1#0", "chunk:tickets/1#1"]
    for c in chunks:
        assert c.subject_ids == ("alice",)
        assert c.policy == PolicyTags.coerce(ALICE)


def test_embedding_inherits_from_chunk_and_is_stored_in_collection(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    chunks = tether.track_chunks("tickets/1", ["hello"], session=session)
    [vec] = tether.track_embeddings(chunks, "support", ["v-1"], session=session)
    assert vec.id == "vector:support/v-1"
    assert vec.subject_ids == ("alice",)
    assert vec.policy == PolicyTags.coerce(ALICE)
    edges = {(e.src_id, e.dst_id, e.edge_type) for e in session.scalars(select(LineageEdge))}
    assert ("vector:support/v-1", "chunk:tickets/1#0", "derived_from") in edges
    assert ("vector:support/v-1", "collection:support", "stored_in") in edges


def test_multi_source_chunk_gets_union_and_most_restrictive(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    tether.record_source("tickets/2", ["bob"], BOB, session=session)
    [chunk] = tether.track_chunks(["tickets/1", "tickets/2"], ["merged"], session=session)
    assert chunk.subject_ids == ("alice", "bob")
    assert chunk.policy == PolicyTags(
        consent_status="pending", license="confidential", retention_until=date(2028, 1, 1)
    )


def test_memory_from_multiple_sources_gets_union_and_most_restrictive(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    tether.record_source("tickets/2", ["bob"], BOB, session=session)
    [chunk] = tether.track_chunks("tickets/1", ["c"], session=session)
    mem = tether.track_memory_write("digest-1", [chunk, "tickets/2"], session=session)
    assert mem.id == "memory:digest-1"
    assert mem.subject_ids == ("alice", "bob")
    assert mem.policy.consent_status == ConsentStatus.PENDING
    assert mem.policy.license == "confidential"
    # Memory derived from memory keeps propagating.
    mem2 = tether.track_memory_write("digest-2", ["memory:digest-1"], session=session)
    assert mem2.subject_ids == ("alice", "bob")


def test_snapshot_manifest_and_model_granularity(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    tether.record_source("tickets/2", ["bob"], BOB, session=session)
    snap = tether.snapshot_dataset(
        "train", ["tickets/1", "tickets/2"], snapshot_id="s1", session=session
    )
    assert snap == "s1"
    row = session.get(DatasetSnapshotRow, "s1")
    assert row is not None
    assert row.subject_ids == ["alice", "bob"]
    assert {r["record_id"] for r in row.manifest["records"]} == {
        "source_record:tickets/1",
        "source_record:tickets/2",
    }
    model = tether.register_model("bot", "1", [snap], session=session)
    assert model.id == "model:bot@1"
    assert model.subject_ids == ("alice", "bob")
    # The model links to the snapshot only, never to records.
    model_edges = session.scalars(
        select(LineageEdge).where(LineageEdge.src_id == "model:bot@1")
    ).all()
    assert [(e.dst_id, e.edge_type) for e in model_edges] == [("snapshot:s1", "trained_on")]
    mv = session.get(ModelVersionRow, "bot@1")
    assert mv is not None
    assert mv.trained_on == ["s1"]
    assert mv.status == "ok"


def test_snapshot_excludes_restricted_subjects(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    tether.record_source("tickets/2", ["bob"], BOB, session=session)
    restrict(session, "bob", RestrictionReason.CONSENT_REVOKED)
    snap = tether.snapshot_dataset("train", ["tickets/1", "tickets/2"], session=session)
    row = session.get(DatasetSnapshotRow, snap)
    assert row is not None
    assert row.subject_ids == ["alice"]
    assert row.manifest["excluded"] == [
        {"record_id": "source_record:tickets/2", "reason": "restricted"}
    ]


def test_subject_index_lists_every_artifact(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    chunks = tether.track_chunks("tickets/1", ["a", "b"], session=session)
    tether.track_embeddings(chunks, "support", ["v1", "v2"], session=session)
    tether.track_memory_write("m1", ["tickets/1"], session=session)
    indexed = set(
        session.scalars(
            select(SubjectArtifact.node_id).where(SubjectArtifact.subject_id == "alice")
        )
    )
    assert indexed == {
        "subject:alice",
        "source_record:tickets/1",
        "chunk:tickets/1#0",
        "chunk:tickets/1#1",
        "vector:support/v1",
        "vector:support/v2",
        "memory:m1",
    }


def test_reingest_is_idempotent(session: Session) -> None:
    for _ in range(2):
        tether.record_source("tickets/1", ["alice"], ALICE, session=session)
        tether.track_chunks("tickets/1", ["a"], session=session)
    assert session.query(LineageNode).count() == 3  # subject, record, chunk
    assert session.query(LineageEdge).count() == 2


def test_every_call_emits_an_openlineage_event(session: Session) -> None:
    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    tether.track_chunks("tickets/1", ["a"], session=session)
    events = session.scalars(select(LineageEventRow).order_by(LineageEventRow.event_time)).all()
    assert [e.job_name for e in events] == ["tether.record_source", "tether.track_chunks"]
    payload = events[1].payload
    assert payload["eventType"] == "COMPLETE"
    assert payload["inputs"][0]["name"] == "tickets/1"
    facet = payload["outputs"][0]["facets"]["tether"]
    assert facet["subjectIds"] == ["alice"]
    assert facet["policy"]["license"] == "internal"


def test_unknown_parent_is_rejected(session: Session) -> None:
    with pytest.raises(UnknownArtifactError):
        tether.track_chunks("tickets/missing", ["x"], session=session)
    with pytest.raises(LineageInputError):
        tether.track_embeddings(["chunk:x"], "c", [], session=session)
    with pytest.raises(LineageInputError):
        tether.record_source("tickets/1", [], session=session)


def test_client_commits_its_own_transaction_when_no_session_given() -> None:
    ref = tether.record_source("tickets/9", ["zoe"], {"consent_status": "granted"})
    with tether.get_client()._session(None) as s:
        assert s.get(LineageNode, ref.id) is not None


def test_discovery_walks_graph_and_finds_models(session: Session) -> None:
    from tether.lineage.query import (
        artifacts_by_type,
        models_trained_on_subject,
        subject_graph,
    )

    tether.record_source("tickets/1", ["alice"], ALICE, session=session)
    tether.record_source("tickets/2", ["bob"], BOB, session=session)
    chunks = tether.track_chunks("tickets/1", ["a"], session=session)
    tether.track_embeddings(chunks, "support", ["v1"], session=session)
    s_all = tether.snapshot_dataset("all", ["tickets/1", "tickets/2"], session=session)
    s_bob = tether.snapshot_dataset("bob-only", ["tickets/2"], session=session)
    tether.register_model("bot", "1", [s_all], session=session)
    tether.register_model("bot", "2", [s_bob], session=session)

    grouped = artifacts_by_type(session, "alice")
    assert set(grouped) == {"subject", "source_record", "chunk", "vector", "snapshot", "model"}
    assert [n.id for n in grouped["model"]] == ["model:bot@1"]
    assert [m.id for m in models_trained_on_subject(session, "alice")] == ["model:bot@1"]
    assert {m.id for m in models_trained_on_subject(session, "bob")} == {
        "model:bot@1",
        "model:bot@2",
    }
    graph = subject_graph(session, "alice")
    ids = {n.id for n in graph.nodes}
    assert "collection:support" in ids
    assert "source_record:tickets/2" not in ids
    assert all(e.src_id in ids and e.dst_id in ids for e in graph.edges)
