"""Deterministic, dependency-free embedder for tests and offline demos.

It hashes word tokens into a fixed-size signed bag of words and normalizes the result.
Texts that share words land close together, which is enough for a believable RAG demo.
"""

import hashlib
import math
import re
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9@.+\-]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "the", "to", "of", "for", "my", "i", "is", "in", "on",
        "it", "me", "at", "or", "from", "with", "be", "has", "not", "was", "this",
    }
)  # fmt: skip


class FakeEmbedder:
    name = "fake"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            token = token.strip(".-+")
            if not token or token in _STOPWORDS:
                continue
            digest = hashlib.sha256(token.encode()).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            vec[idx] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]
