"""Embedding providers and the vector store used for semantic search.

* ``HashingEmbedder``: deterministic feature hashing over word unigrams/bigrams and
  character trigrams. No model download and no network, which makes it useful for
  air-gapped tests and as a lexical-semantic baseline.
* ``OllamaEmbedder``: in-VPC neural embeddings (e.g. ``nomic-embed-text``).
* ``PgVectorStore`` / ``InMemoryVectorStore``: cosine kNN with content-hash dedupe so
  unchanged entity documents are never re-embedded.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def _tokens(text_: str) -> list[str]:
    # split camelCase and snake_case so "customerEmail" ~ "customer email"
    text_ = re.sub(r"([a-z])([A-Z])", r"\1 \2", text_).replace("_", " ").lower()
    return _WORD.findall(text_)


class HashingEmbedder:
    name = "hashing"

    def __init__(self, dim: int = 384):
        self.dim = dim

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % self.dim
        sign = 1.0 if h[4] & 1 else -1.0
        return idx, sign

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            words = _tokens(t)
            feats: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
            feats += [(f"b:{a}_{b}", 0.7) for a, b in zip(words, words[1:])]
            for w in words:
                padded = f"#{w}#"
                feats += [(f"c:{padded[i:i + 3]}", 0.3) for i in range(len(padded) - 2)]
            for f, weight in feats:
                idx, sign = self._bucket(f)
                vec[idx] += sign * weight
            out.append(_normalize(vec))
        return out


class OllamaEmbedder:
    name = "ollama"

    def __init__(self, url: str, model: str = "nomic-embed-text", dim: int = 768, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.model = model
        self.dim = dim
        self.http = httpx.Client(timeout=timeout)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        resp = self.http.post(f"{self.url}/api/embed", json={"model": self.model, "input": list(texts)})
        resp.raise_for_status()
        return [_normalize(v) for v in resp.json()["embeddings"]]


def build_embedder(settings) -> Embedder:
    ai = settings.ai
    if ai.embedding_provider == "ollama":
        return OllamaEmbedder(ai.ollama_url, ai.embedding_model, ai.embedding_dim)
    if ai.embedding_provider == "datahub":
        # Delegating to GMS SemanticSearchService is roadmap item M4.3; until then use the local index.
        logging.getLogger(__name__).warning("embedding_provider=datahub not implemented yet; using hashing")
    return HashingEmbedder(ai.embedding_dim)


def content_hash(text_: str, embedder: Embedder) -> str:
    return hashlib.sha256(f"{embedder.name}:{embedder.dim}:{text_}".encode()).hexdigest()


@dataclass
class VectorHit:
    urn: str
    score: float
    entity_type: str
    name: str
    snippet: str


class VectorStore(Protocol):
    def upsert(self, urn: str, entity_type: str, name: str, text_: str, vector: list[float], chash: str) -> None: ...

    def get_hash(self, urn: str) -> Optional[str]: ...

    def delete(self, urn: str) -> None: ...

    def search(self, vector: list[float], k: int = 10, entity_types: Optional[list[str]] = None) -> list[VectorHit]: ...


class InMemoryVectorStore:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str, str, list[float], str]] = {}
        self._lock = threading.Lock()

    def upsert(self, urn, entity_type, name, text_, vector, chash) -> None:
        with self._lock:
            self.rows[urn] = (entity_type, name, text_, vector, chash)

    def get_hash(self, urn):
        row = self.rows.get(urn)
        return row[4] if row else None

    def delete(self, urn) -> None:
        with self._lock:
            self.rows.pop(urn, None)

    def search(self, vector, k=10, entity_types=None):
        hits = []
        for urn, (etype, name, text_, vec, _) in self.rows.items():
            if entity_types and etype not in entity_types:
                continue
            score = sum(a * b for a, b in zip(vector, vec))
            hits.append(VectorHit(urn, score, etype, name, text_[:300]))
        hits.sort(key=lambda h: -h.score)
        return hits[:k]


class PgVectorStore:
    """pgvector-backed store (``CREATE EXTENSION vector`` is done by the compose/helm init)."""

    def __init__(self, engine: Engine, dim: int, table: str = "dhe_embeddings"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
            raise ValueError("invalid table name")
        self.engine = engine
        self.dim = dim
        self.table = table
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {table} (urn TEXT PRIMARY KEY, entity_type TEXT NOT NULL, "
                f"name TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT NOT NULL, "
                f"embedding vector({int(dim)}) NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {table}_hnsw ON {table} "
                              f"USING hnsw (embedding vector_cosine_ops)"))

    @staticmethod
    def _lit(vector: list[float]) -> str:
        return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"

    def upsert(self, urn, entity_type, name, text_, vector, chash) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                f"INSERT INTO {self.table} (urn, entity_type, name, body, content_hash, embedding) "
                f"VALUES (:urn, :t, :n, :b, :h, CAST(:v AS vector)) ON CONFLICT (urn) DO UPDATE SET "
                f"entity_type = EXCLUDED.entity_type, name = EXCLUDED.name, body = EXCLUDED.body, "
                f"content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding, updated_at = now()"),
                {"urn": urn, "t": entity_type, "n": name, "b": text_, "h": chash, "v": self._lit(vector)})

    def get_hash(self, urn):
        with self.engine.connect() as conn:
            return conn.execute(text(f"SELECT content_hash FROM {self.table} WHERE urn = :u"), {"u": urn}).scalar()

    def delete(self, urn) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {self.table} WHERE urn = :u"), {"u": urn})

    def search(self, vector, k=10, entity_types=None):
        sql = (f"SELECT urn, entity_type, name, body, 1 - (embedding <=> CAST(:v AS vector)) AS score "
               f"FROM {self.table} ")
        params: dict = {"v": self._lit(vector), "k": int(k)}
        if entity_types:
            sql += "WHERE entity_type = ANY(:types) "
            params["types"] = list(entity_types)
        sql += "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :k"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [VectorHit(r.urn, float(r.score), r.entity_type, r.name, r.body[:300]) for r in rows]


def build_vector_store(engine: Engine, dim: int) -> VectorStore:
    if engine.dialect.name == "postgresql":
        return PgVectorStore(engine, dim)
    return InMemoryVectorStore()
