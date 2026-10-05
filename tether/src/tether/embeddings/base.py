"""Embedder interface and factory."""

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def get_embedder(kind: str | None = None, dim: int | None = None) -> Embedder:
    """Build the configured embedder (``fake`` or ``sentence-transformers``)."""
    from tether.config import get_settings

    settings = get_settings()
    kind = (kind or settings.embedder).lower()
    dim = dim or settings.embedding_dim
    if kind == "fake":
        from tether.embeddings.fake import FakeEmbedder

        return FakeEmbedder(dim)
    if kind in {"sentence-transformers", "sentence_transformers", "st"}:
        from tether.embeddings.sentence import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder(settings.sentence_transformers_model, dim)
    raise ValueError(f"unknown embedder: {kind!r}")
