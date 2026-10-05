"""Pluggable embedders."""

from tether.embeddings.base import Embedder, get_embedder
from tether.embeddings.fake import FakeEmbedder

__all__ = ["Embedder", "FakeEmbedder", "get_embedder"]
