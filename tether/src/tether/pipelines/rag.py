"""RAG pipeline: support tickets -> chunks -> embeddings -> pgvector collection.

This shows the intended SDK pattern: every data write sits next to the matching
``tether.*`` call, inside the same transaction.
"""

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

import tether
from tether.db.models import Customer, SupportTicket
from tether.embeddings.base import Embedder
from tether.policy.enforcement import restricted_subject_ids
from tether.stores.chunks import upsert_chunks
from tether.stores.source import record_id
from tether.stores.vector import PgVectorStore, VectorHit, VectorItem

log = logging.getLogger(__name__)

COLLECTION = "support-kb"
MAX_CHUNK_CHARS = 160
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Greedy sentence packing: chunks of whole sentences, about ``max_chars`` long."""
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(text.strip()):
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class IngestResult:
    records: int
    chunks: int
    vectors: int
    skipped_restricted: int


def record_customers(session: Session) -> int:
    """Register each customer profile row as a source record of its own subject."""
    customers = session.scalars(select(Customer).order_by(Customer.id)).all()
    for c in customers:
        tether.record_source(record_id("customers", c.id), [c.id], session=session)
    return len(customers)


def ingest_tickets(
    session: Session, embedder: Embedder, collection: str = COLLECTION
) -> IngestResult:
    """Chunk and embed every ticket whose subject is not restricted."""
    blocked = restricted_subject_ids(session)
    store = PgVectorStore(session)
    tickets = session.scalars(select(SupportTicket).order_by(SupportTicket.id)).all()
    n_records = n_chunks = n_vectors = skipped = 0
    for t in tickets:
        if t.customer_id in blocked:
            skipped += 1
            continue
        rid = record_id("support_tickets", t.id)
        # 1. tag the source with its subject. Policy comes from policy_tags.
        tether.record_source(rid, [t.customer_id], attrs={"category": t.category}, session=session)
        # 2. chunk, and record the chunks. Each one inherits subject and policy.
        texts = chunk_text(f"{t.title}. {t.body}")
        chunk_refs = tether.track_chunks(rid, texts, session=session)
        upsert_chunks(session, f"source_record:{rid}", chunk_refs, texts)
        # 3. embed, write the vectors, and record them. They inherit from their chunks.
        vector_ids = [c.ref.split("/", 1)[1] for c in chunk_refs]  # e.g. "tkt-0001#0"
        embeddings = embedder.embed(texts)
        vector_refs = tether.track_embeddings(chunk_refs, collection, vector_ids, session=session)
        store.upsert(
            collection,
            [
                VectorItem(
                    id=vid,
                    embedding=emb,
                    content=text,
                    subject_ids=list(vref.subject_ids),
                    policy=vref.policy.to_json(),
                    chunk_id=cref.id,
                )
                for vid, emb, text, vref, cref in zip(
                    vector_ids, embeddings, texts, vector_refs, chunk_refs, strict=True
                )
            ],
        )
        n_records += 1
        n_chunks += len(texts)
        n_vectors += len(vector_refs)
    log.info(
        "RAG ingest: %d records, %d chunks, %d vectors (%d restricted records skipped)",
        n_records,
        n_chunks,
        n_vectors,
        skipped,
    )
    return IngestResult(n_records, n_chunks, n_vectors, skipped)


def query(
    session: Session,
    embedder: Embedder,
    text: str,
    top_k: int = 5,
    collection: str = COLLECTION,
) -> list[VectorHit]:
    """Policy-enforced retrieval. Blocked subjects never leave the database."""
    [embedding] = embedder.embed([text])
    return PgVectorStore(session).search(collection, embedding, top_k, enforce_policy=True)
