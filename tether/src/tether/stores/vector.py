"""Vector store interface and the pgvector implementation.

Any implementation **must** apply the policy filter *inside* the similarity query, before
the top-k cut, so filtered results never fall short and blocked content never leaves the
store. Qdrant, for example, would use a ``must_not`` filter on a ``subject_ids`` payload
holding the live block list.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tether.db.models import VectorRow
from tether.policy.enforcement import allowed_predicate


@dataclass(frozen=True)
class VectorItem:
    id: str
    embedding: list[float]
    content: str
    subject_ids: list[str]
    policy: dict[str, Any] = field(default_factory=dict)
    chunk_id: str | None = None


@dataclass(frozen=True)
class VectorHit:
    collection: str
    id: str
    content: str
    score: float
    subject_ids: list[str]
    chunk_id: str | None
    policy: dict[str, Any]


class VectorStore(Protocol):
    def upsert(self, collection: str, items: Sequence[VectorItem]) -> None: ...

    def search(
        self, collection: str, embedding: list[float], top_k: int, *, enforce_policy: bool = True
    ) -> list[VectorHit]: ...

    def delete(self, collection: str, ids: Sequence[str]) -> int: ...


class PgVectorStore:
    """Vectors in the ``vectors`` table, searched by cosine distance.

    The search is exact (no ANN index) so the policy filter can never interact badly with
    approximate candidate lists. That is fine at MVP scale. With pgvector 0.8 or later,
    HNSW plus ``hnsw.iterative_scan`` keeps filtered queries correct at larger scale.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, collection: str, items: Sequence[VectorItem]) -> None:
        if not items:
            return
        rows = [
            {
                "collection": collection,
                "id": it.id,
                "embedding": it.embedding,
                "content": it.content,
                "subject_ids": it.subject_ids,
                "policy": it.policy,
                "chunk_id": it.chunk_id,
            }
            for it in items
        ]
        stmt = insert(VectorRow).values(rows)
        self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[VectorRow.collection, VectorRow.id],
                set_={
                    "embedding": stmt.excluded.embedding,
                    "content": stmt.excluded.content,
                    "subject_ids": stmt.excluded.subject_ids,
                    "policy": stmt.excluded.policy,
                    "chunk_id": stmt.excluded.chunk_id,
                },
            )
        )

    def search(
        self, collection: str, embedding: list[float], top_k: int, *, enforce_policy: bool = True
    ) -> list[VectorHit]:
        distance = VectorRow.embedding.cosine_distance(embedding)
        stmt = select(VectorRow, distance.label("distance")).where(
            VectorRow.collection == collection
        )
        if enforce_policy:
            stmt = stmt.where(allowed_predicate(VectorRow.subject_ids, VectorRow.policy))
        stmt = stmt.order_by(distance, VectorRow.id).limit(top_k)
        return [
            VectorHit(
                collection=row.collection,
                id=row.id,
                content=row.content,
                score=1.0 - float(dist),
                subject_ids=list(row.subject_ids),
                chunk_id=row.chunk_id,
                policy=dict(row.policy),
            )
            for row, dist in self.session.execute(stmt).all()
        ]

    def delete(self, collection: str, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        result = self.session.execute(
            delete(VectorRow).where(VectorRow.collection == collection, VectorRow.id.in_(ids))
        )
        return int(getattr(result, "rowcount", 0) or 0)
