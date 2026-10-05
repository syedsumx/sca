"""The Tether SDK client.

Pipeline code calls these methods next to its own writes, so lineage is recorded at the
moment data moves. Propagation is automatic:

* a chunk inherits subject ids and policy from its source record(s)
* a vector inherits them from its chunk
* a snapshot takes the union of its records' subjects
* a model takes the union of its snapshots
* a memory entry takes the union of everything it derives from

When an artifact derives from several parents, it carries the **union** of their subject
ids and the **most restrictive** of their policies.

Every method accepts an optional SQLAlchemy ``session``. Pass one to record lineage in
the same transaction as your data writes, which is the recommended pattern. Without
one, the client opens and commits its own transaction.
"""

import hashlib
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from tether.config import get_settings
from tether.db.models import DatasetSnapshotRow, ModelVersionRow, PolicyTagRow
from tether.db.session import get_session_factory, session_scope
from tether.lineage import openlineage
from tether.lineage.ids import (
    EdgeType,
    NodeType,
    ensure_typed,
    model_ref,
    node_id,
    split,
    vector_ref,
)
from tether.lineage.store import LineageStore, NodeSpec
from tether.policy.enforcement import restricted_subject_ids
from tether.policy.tags import ConsentStatus, PolicyTags, most_restrictive, union_subjects

TagsLike = PolicyTags | Mapping[str, Any] | None


class LineageInputError(ValueError):
    """Raised for malformed SDK input (e.g. mismatched list lengths)."""


@dataclass(frozen=True)
class ArtifactRef:
    """A recorded artifact: its typed lineage id plus inherited subjects and policy."""

    id: str
    node_type: NodeType
    subject_ids: tuple[str, ...]
    policy: PolicyTags
    attrs: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def ref(self) -> str:
        """The id without its type prefix (e.g. ``support_tickets/tkt-0001``)."""
        return split(self.id)[1]

    def __str__(self) -> str:
        return self.id


def _ref_from_spec(spec: NodeSpec) -> ArtifactRef:
    return ArtifactRef(
        id=spec.id,
        node_type=spec.node_type,
        subject_ids=tuple(sorted(set(spec.subject_ids))),
        policy=spec.policy,
        attrs=spec.attrs,
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "dataset"


class TetherClient:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        exporter: openlineage.LineageExporter | None = None,
        namespace: str | None = None,
    ) -> None:
        settings = get_settings()
        self._session_factory = session_factory
        self.exporter = exporter or openlineage.exporter_from_url(settings.openlineage_url)
        self.namespace = namespace or settings.openlineage_namespace

    @contextmanager
    def _session(self, session: Session | None) -> Iterator[Session]:
        if session is not None:
            yield session
            return
        with session_scope(self._session_factory or get_session_factory()) as own:
            yield own

    def _store(self, session: Session) -> LineageStore:
        return LineageStore(session, self.exporter)

    # ------------------------------------------------------------------ sources

    def record_source(
        self,
        record_id: str,
        subject_ids: Sequence[str],
        tags: TagsLike = None,
        *,
        attrs: Mapping[str, Any] | None = None,
        session: Session | None = None,
    ) -> ArtifactRef:
        """Register a source record (e.g. ``support_tickets/tkt-0001``) and its subjects.

        When ``tags`` is omitted, the subjects' rows in ``policy_tags`` are merged. A
        subject with no row counts as ``pending`` consent (fail closed).
        """
        if not subject_ids:
            raise LineageInputError("record_source needs at least one subject id")
        _, ref = split(ensure_typed(record_id, NodeType.SOURCE_RECORD))
        with self._session(session) as s:
            store = self._store(s)
            subject_policies = self._subject_policies(s, subject_ids)
            policy = (
                PolicyTags.coerce(tags)
                if tags is not None
                else most_restrictive(subject_policies.values())
            )
            subject_specs = [
                NodeSpec(NodeType.SUBJECT, sid, [sid], subject_policies[sid]) for sid in subject_ids
            ]
            spec = NodeSpec(
                NodeType.SOURCE_RECORD, ref, list(subject_ids), policy, dict(attrs or {})
            )
            store.upsert_nodes([*subject_specs, spec])
            store.add_edges((spec.id, sub.id, EdgeType.DERIVED_FROM) for sub in subject_specs)
            store.emit("tether.record_source", self.namespace, subject_specs, [spec])
            return _ref_from_spec(spec)

    def _subject_policies(self, s: Session, subject_ids: Sequence[str]) -> dict[str, PolicyTags]:
        rows = s.scalars(select(PolicyTagRow).where(PolicyTagRow.subject_id.in_(subject_ids)))
        found = {
            r.subject_id: PolicyTags(
                consent_status=ConsentStatus(r.consent_status),
                license=r.license,
                retention_until=r.retention_until,
            )
            for r in rows
        }
        pending = PolicyTags(consent_status=ConsentStatus.PENDING)
        return {sid: found.get(sid, pending) for sid in subject_ids}

    # ------------------------------------------------------------------ chunks / vectors

    def track_chunks(
        self,
        source_record_id: str | Sequence[str],
        chunks: Sequence[str],
        *,
        session: Session | None = None,
    ) -> list[ArtifactRef]:
        """Record chunks cut from one source record (or several, for merged chunks).

        Chunk ids are deterministic (``chunk:<source ref>#<seq>``), so re-ingesting a record
        updates its chunks in place instead of duplicating them.
        """
        sources = [source_record_id] if isinstance(source_record_id, str) else source_record_id
        parent_ids = [ensure_typed(x, NodeType.SOURCE_RECORD) for x in sources]
        with self._session(session) as s:
            store = self._store(s)
            parents = store.require(parent_ids)
            subjects = union_subjects(p.subject_ids for p in parents)
            policy = most_restrictive(p.policy for p in parents)
            base = "+".join(split(p)[1] for p in parent_ids)
            specs = [
                NodeSpec(
                    NodeType.CHUNK,
                    f"{base}#{seq}",
                    subjects,
                    policy,
                    {
                        "seq": seq,
                        "chars": len(text),
                        "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    },
                )
                for seq, text in enumerate(chunks)
            ]
            store.upsert_nodes(specs)
            store.add_edges((c.id, p, EdgeType.DERIVED_FROM) for c in specs for p in parent_ids)
            store.emit("tether.track_chunks", self.namespace, parents, specs)
            return [_ref_from_spec(c) for c in specs]

    def track_embeddings(
        self,
        chunk_ids: Sequence[str | ArtifactRef],
        collection: str,
        vector_ids: Sequence[str],
        *,
        session: Session | None = None,
    ) -> list[ArtifactRef]:
        """Record vectors (one per chunk) written into ``collection``."""
        if len(chunk_ids) != len(vector_ids):
            raise LineageInputError("chunk_ids and vector_ids must have the same length")
        parent_ids = [ensure_typed(str(c), NodeType.CHUNK) for c in chunk_ids]
        with self._session(session) as s:
            store = self._store(s)
            parents = store.require(parent_ids)
            coll = NodeSpec(NodeType.COLLECTION, collection, [], PolicyTags(), {"kind": "pgvector"})
            store.upsert_nodes([coll])
            specs = [
                NodeSpec(
                    NodeType.VECTOR,
                    vector_ref(collection, vid),
                    list(parent.subject_ids),
                    PolicyTags.coerce(parent.policy),
                    {"collection": collection, "vector_id": vid, "chunk_id": parent.id},
                )
                for parent, vid in zip(parents, vector_ids, strict=True)
            ]
            store.upsert_nodes(specs)
            store.add_edges(
                edge
                for v in specs
                for edge in (
                    (v.id, v.attrs["chunk_id"], EdgeType.DERIVED_FROM),
                    (v.id, coll.id, EdgeType.STORED_IN),
                )
            )
            store.emit("tether.track_embeddings", self.namespace, parents, specs)
            return [_ref_from_spec(v) for v in specs]

    # ------------------------------------------------------------------ training

    def snapshot_dataset(
        self,
        name: str,
        record_ids: Sequence[str],
        *,
        snapshot_id: str | None = None,
        exclude_restricted: bool = True,
        session: Session | None = None,
    ) -> str:
        """Freeze a training dataset and store its manifest (record ids and subject ids).

        Records belonging to restricted subjects (revoked consent, pending deletion,
        deleted) are left out by default and listed under ``excluded`` in the manifest.
        """
        parent_ids = [ensure_typed(r, NodeType.SOURCE_RECORD) for r in record_ids]
        snap_ref = snapshot_id or (
            f"{_slug(name)}-{datetime.now(UTC):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
        )
        with self._session(session) as s:
            store = self._store(s)
            records = store.require(parent_ids)
            blocked = restricted_subject_ids(s) if exclude_restricted else set()
            members = [r for r in records if not (set(r.subject_ids) & blocked)]
            excluded = [r for r in records if set(r.subject_ids) & blocked]
            subjects = union_subjects(r.subject_ids for r in members)
            manifest = {
                "name": name,
                "records": [{"record_id": r.id, "subject_ids": r.subject_ids} for r in members],
                "subject_ids": subjects,
                "excluded": [{"record_id": r.id, "reason": "restricted"} for r in excluded],
            }
            spec = NodeSpec(
                NodeType.SNAPSHOT,
                snap_ref,
                subjects,
                most_restrictive(r.policy for r in members),
                {"name": name, "record_count": len(members), "excluded_count": len(excluded)},
            )
            store.upsert_nodes([spec])
            store.add_edges((r.id, spec.id, EdgeType.CONTAINED_IN) for r in members)
            s.execute(
                insert(DatasetSnapshotRow)
                .values(
                    id=snap_ref,
                    name=name,
                    manifest=manifest,
                    subject_ids=subjects,
                    record_count=len(members),
                )
                .on_conflict_do_nothing()
            )
            store.emit("tether.snapshot_dataset", self.namespace, members, [spec])
            return snap_ref

    def register_model(
        self,
        name: str,
        version: str,
        trained_on_snapshot_ids: Sequence[str],
        *,
        session: Session | None = None,
    ) -> ArtifactRef:
        """Register a model version trained on one or more dataset snapshots.

        Models link to snapshots only, never to individual records. The snapshot manifest
        holds the record-level detail.
        """
        parent_ids = [ensure_typed(x, NodeType.SNAPSHOT) for x in trained_on_snapshot_ids]
        with self._session(session) as s:
            store = self._store(s)
            snaps = store.require(parent_ids)
            ref = model_ref(name, version)
            spec = NodeSpec(
                NodeType.MODEL,
                ref,
                union_subjects(x.subject_ids for x in snaps),
                most_restrictive(x.policy for x in snaps),
                {"name": name, "version": version},
            )
            store.upsert_nodes([spec])
            store.add_edges((spec.id, p, EdgeType.TRAINED_ON) for p in parent_ids)
            snapshot_refs = [split(p)[1] for p in parent_ids]
            stmt = insert(ModelVersionRow).values(
                id=ref, name=name, version=version, trained_on=snapshot_refs, status="ok"
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[ModelVersionRow.id], set_={"trained_on": snapshot_refs}
                )
            )
            store.emit("tether.register_model", self.namespace, snaps, [spec])
            return _ref_from_spec(spec)

    # ------------------------------------------------------------------ agent memory

    def track_memory_write(
        self,
        memory_id: str,
        derived_from_ids: Sequence[str | ArtifactRef],
        *,
        attrs: Mapping[str, Any] | None = None,
        session: Session | None = None,
    ) -> ArtifactRef:
        """Record an agent-memory entry derived from records, chunks or other memories.

        Untyped ids default to source records.
        """
        if not derived_from_ids:
            raise LineageInputError("a memory entry must derive from at least one artifact")
        parent_ids = [ensure_typed(str(x), NodeType.SOURCE_RECORD) for x in derived_from_ids]
        _, ref = split(ensure_typed(memory_id, NodeType.MEMORY))
        with self._session(session) as s:
            store = self._store(s)
            parents = store.require(parent_ids)
            spec = NodeSpec(
                NodeType.MEMORY,
                ref,
                union_subjects(p.subject_ids for p in parents),
                most_restrictive(p.policy for p in parents),
                {**dict(attrs or {}), "derived_from": parent_ids},
            )
            store.upsert_nodes([spec])
            store.add_edges((spec.id, p, EdgeType.DERIVED_FROM) for p in parent_ids)
            store.emit("tether.track_memory_write", self.namespace, parents, [spec])
            return _ref_from_spec(spec)


def subject_node_id(subject_id: str) -> str:
    return node_id(NodeType.SUBJECT, subject_id)
