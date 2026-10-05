"""Agent memory store (``memory_entries``). Reads are policy-filtered by default."""

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tether.db.models import MemoryEntryRow
from tether.policy.enforcement import allowed_predicate
from tether.sdk.client import ArtifactRef


def write_memory(session: Session, ref: ArtifactRef, kind: str, content: str) -> None:
    values = {
        "id": ref.ref,
        "kind": kind,
        "content": content,
        "subject_ids": list(ref.subject_ids),
        "derived_from": list(ref.attrs.get("derived_from", [])),
        "policy": ref.policy.to_json(),
    }
    stmt = insert(MemoryEntryRow).values(values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[MemoryEntryRow.id],
            set_={k: stmt.excluded[k] for k in values if k != "id"},
        )
    )


def read_memory(
    session: Session,
    subject_id: str | None = None,
    *,
    enforce_policy: bool = True,
    limit: int = 100,
) -> list[MemoryEntryRow]:
    stmt = select(MemoryEntryRow)
    if subject_id is not None:
        stmt = stmt.where(MemoryEntryRow.subject_ids.contains([subject_id]))
    if enforce_policy:
        stmt = stmt.where(allowed_predicate(MemoryEntryRow.subject_ids, MemoryEntryRow.policy))
    return list(session.scalars(stmt.order_by(MemoryEntryRow.id).limit(limit)))


def delete_memory(session: Session, ids: Sequence[str]) -> int:
    if not ids:
        return 0
    result = session.execute(delete(MemoryEntryRow).where(MemoryEntryRow.id.in_(ids)))
    return int(getattr(result, "rowcount", 0) or 0)
