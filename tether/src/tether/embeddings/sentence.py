"""Optional sentence-transformers embedder.

Install with ``pip install "tether-lineage[sentence-transformers]"``.
"""

from collections.abc import Sequence
from typing import Any


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, dim: int) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "sentence-transformers is not installed; "
                "pip install 'tether-lineage[sentence-transformers]'"
            ) from exc
        self._model: Any = SentenceTransformer(model_name)
        model_dim = int(self._model.get_sentence_embedding_dimension())
        if model_dim != dim:
            raise ValueError(
                f"{model_name} produces {model_dim}-d vectors but TETHER_EMBEDDING_DIM={dim}"
            )
        self.dim = dim
        self.name = f"sentence-transformers:{model_name}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in v] for v in vectors]
