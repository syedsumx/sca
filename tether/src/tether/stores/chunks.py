"""Chunk store (Postgres table ``chunks``)."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tether.db.models import ChunkRow
from tether.sdk.client import ArtifactRef


def upsert_chunks(
    session: Session, source_record_id: str, refs: Sequence[ArtifactRef], texts: Sequence[str]
) -> None:
    rows: list[dict[str, Any]] = [
        {
            "id": ref.id,
            "source_record_id": source_record_id,
            "seq": seq,
            "text": text,
            "subject_ids": list(ref.subject_ids),
            "policy": ref.policy.to_json(),
        }
        for seq, (ref, text) in enumerate(zip(refs, texts, strict=True))
    ]
    if not rows:
        return
    stmt = insert(ChunkRow).values(rows)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ChunkRow.id],
            set_={
                "text": stmt.excluded.text,
                "subject_ids": stmt.excluded.subject_ids,
                "policy": stmt.excluded.policy,
            },
        )
    )


def delete_chunks(session: Session, ids: Sequence[str]) -> int:
    if not ids:
        return 0
    result = session.execute(delete(ChunkRow).where(ChunkRow.id.in_(ids)))
    return int(getattr(result, "rowcount", 0) or 0)
