"""Policy-enforced retrieval."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from tether.api import schemas
from tether.api.deps import get_embedder_dep, get_session
from tether.embeddings.base import Embedder
from tether.pipelines import rag

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/query", response_model=schemas.RagQueryOut)
def query(
    body: schemas.RagQueryIn,
    session: Annotated[Session, Depends(get_session)],
    embedder: Annotated[Embedder, Depends(get_embedder_dep)],
) -> schemas.RagQueryOut:
    """Similarity search over the support knowledge base.

    Vectors tied to a restricted subject (revoked consent, pending or completed deletion)
    or carrying an expired or revoked policy are filtered inside the database query.
    """
    hits = rag.query(session, embedder, body.query, body.top_k)
    return schemas.RagQueryOut(
        query=body.query,
        results=[
            schemas.RagHitOut(
                vector_id=h.id,
                collection=h.collection,
                chunk_id=h.chunk_id,
                content=h.content,
                score=round(h.score, 6),
                subject_ids=h.subject_ids,
            )
            for h in hits
        ],
    )
