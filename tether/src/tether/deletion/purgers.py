"""Per-artifact-type purgers.

A purger deletes the stored data behind a lineage node and turns the node into a
content-free tombstone. Every purger is idempotent: purging something that is already
gone deletes zero rows and still succeeds. To support a new backend (for example Qdrant),
register a purger for its node type.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tether.db.models import ChunkRow, LineageNode, MemoryEntryRow, VectorRow
from tether.lineage.ids import NodeType
from tether.stores.chunks import delete_chunks
from tether.stores.memory import delete_memory
from tether.stores.source import delete_source_record, purge_order, sweep_subject_rows
from tether.stores.vector import PgVectorStore

PURGED = "purged"
NEEDS_REGENERATION = "needs_regeneration"
# Attributes derived from content (e.g. text hashes) are dropped from tombstones.
_CONTENT_ATTRS = {"sha256"}


@dataclass(frozen=True)
class PurgeOutcome:
    node_id: str
    node_type: str
    rows_deleted: int
    action: str  # "deleted" | "deleted_needs_regeneration"
    other_subjects: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "type": self.node_type,
            "rows_deleted": self.rows_deleted,
            "action": self.action,
            "other_subjects": self.other_subjects,
        }


Purger = Callable[[Session, LineageNode, str], PurgeOutcome]


def _tombstone(node: LineageNode, status: str, extra: dict[str, Any] | None = None) -> None:
    node.status = status
    node.attrs = {
        **{k: v for k, v in node.attrs.items() if k not in _CONTENT_ATTRS},
        **(extra or {}),
    }


def _others(node: LineageNode, subject_id: str) -> list[str]:
    return [s for s in node.subject_ids if s != subject_id]


def purge_memory(session: Session, node: LineageNode, subject_id: str) -> PurgeOutcome:
    rows = delete_memory(session, [node.ref])
    others = _others(node, subject_id)
    if others:
        # A multi-subject entry cannot be partially deleted. Drop it and mark it for
        # regeneration from the subjects that remain.
        _tombstone(
            node,
            NEEDS_REGENERATION,
            {"remaining_subject_ids": others, "regeneration_reason": "subject_deleted"},
        )
        return PurgeOutcome(
            node.id, node.node_type, rows, "deleted_needs_regeneration", len(others)
        )
    _tombstone(node, PURGED)
    return PurgeOutcome(node.id, node.node_type, rows, "deleted")


def purge_vector(session: Session, node: LineageNode, subject_id: str) -> PurgeOutcome:
    collection = str(node.attrs.get("collection", ""))
    vector_id = str(node.attrs.get("vector_id", ""))
    rows = PgVectorStore(session).delete(collection, [vector_id])
    _tombstone(node, PURGED)
    return PurgeOutcome(node.id, node.node_type, rows, "deleted", len(_others(node, subject_id)))


def purge_chunk(session: Session, node: LineageNode, subject_id: str) -> PurgeOutcome:
    rows = delete_chunks(session, [node.id])
    _tombstone(node, PURGED)
    return PurgeOutcome(node.id, node.node_type, rows, "deleted", len(_others(node, subject_id)))


def purge_source_record(session: Session, node: LineageNode, subject_id: str) -> PurgeOutcome:
    rows = delete_source_record(session, node.ref)
    _tombstone(node, PURGED)
    return PurgeOutcome(node.id, node.node_type, rows, "deleted", len(_others(node, subject_id)))


PURGERS: dict[str, Purger] = {
    NodeType.MEMORY.value: purge_memory,
    NodeType.VECTOR.value: purge_vector,
    NodeType.CHUNK.value: purge_chunk,
    NodeType.SOURCE_RECORD.value: purge_source_record,
}

# Derived artifacts go before their sources, so an interrupted purge never leaves a
# derived copy whose source is already gone.
PURGE_TYPE_ORDER = [
    NodeType.MEMORY.value,
    NodeType.VECTOR.value,
    NodeType.CHUNK.value,
    NodeType.SOURCE_RECORD.value,
]


def purge_nodes(session: Session, node_ids: list[str], subject_id: str) -> list[PurgeOutcome]:
    nodes = list(session.scalars(select(LineageNode).where(LineageNode.id.in_(node_ids))))

    def order(n: LineageNode) -> tuple[int, int, str]:
        type_rank = PURGE_TYPE_ORDER.index(n.node_type) if n.node_type in PURGERS else 99
        within = purge_order(n.ref) if n.node_type == NodeType.SOURCE_RECORD.value else 0
        return type_rank, within, n.id

    outcomes = []
    for node in sorted(nodes, key=order):
        purger = PURGERS.get(node.node_type)
        if purger is not None:
            outcomes.append(purger(session, node, subject_id))
    session.flush()
    return outcomes


def sweep_untracked(session: Session, subject_id: str) -> dict[str, int]:
    """Safety net: delete stored rows tagged with the subject that lineage never saw."""
    swept: dict[str, int] = {}
    for label, model in (
        ("memory_entries", MemoryEntryRow),
        ("vectors", VectorRow),
        ("chunks", ChunkRow),
    ):
        result = session.execute(delete(model).where(model.subject_ids.contains([subject_id])))
        n = int(getattr(result, "rowcount", 0) or 0)
        if n:
            swept[label] = n
    swept.update(sweep_subject_rows(session, subject_id))
    return swept
