"""Independent post-deletion check: does anything still reference the subject?

This deliberately queries the stores directly instead of trusting lineage, so it catches
anything the purge missed.
"""

from collections.abc import Sequence

from sqlalchemy import ColumnElement, false, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from tether.db.models import (
    ChunkRow,
    Customer,
    LineageNode,
    MemoryEntryRow,
    SupportTicket,
    VectorRow,
)

PURGEABLE_TYPES = ("source_record", "chunk", "vector", "memory")


def _mentions(col: InstrumentedAttribute[str], needles: Sequence[str]) -> ColumnElement[bool]:
    return or_(false(), *(col.ilike(f"%{n}%") for n in needles if n))


def residual_references(
    session: Session, subject_id: str, identifiers: Sequence[str] = ()
) -> dict[str, int]:
    """Count rows that still reference the subject (by id, or by text identifiers such as
    their email). A clean deletion returns all zeros."""

    def count(model: type, *conditions: ColumnElement[bool]) -> int:
        return int(
            session.scalar(select(func.count()).select_from(model).where(or_(*conditions))) or 0
        )

    return {
        "customers": count(Customer, Customer.id == subject_id),
        "support_tickets": count(
            SupportTicket,
            SupportTicket.customer_id == subject_id,
            _mentions(SupportTicket.body, identifiers),
        ),
        "chunks": count(
            ChunkRow,
            ChunkRow.subject_ids.contains([subject_id]),
            _mentions(ChunkRow.text, identifiers),
        ),
        "vectors": count(
            VectorRow,
            VectorRow.subject_ids.contains([subject_id]),
            _mentions(VectorRow.content, identifiers),
        ),
        "memory_entries": count(
            MemoryEntryRow,
            MemoryEntryRow.subject_ids.contains([subject_id]),
            _mentions(MemoryEntryRow.content, identifiers),
        ),
        "active_lineage_artifacts": int(
            session.scalar(
                select(func.count())
                .select_from(LineageNode)
                .where(
                    LineageNode.subject_ids.contains([subject_id]),
                    LineageNode.node_type.in_(PURGEABLE_TYPES),
                    LineageNode.status == "active",
                )
            )
            or 0
        ),
    }
