"""``cairn-executor`` plugin keeping the semantic index fresh from MCL events."""

from __future__ import annotations

import json
import logging
from typing import Any

from cairn.ai.catalog import Catalog
from cairn.ai.embeddings import build_embedder, build_vector_store
from cairn.ai.semantic import SemanticIndex
from cairn.common.config import get_settings
from cairn.common.db import get_engine
from cairn.common.core import CoreClient

logger = logging.getLogger(__name__)

try:
    from datahub_actions.action.action import Action as _ActionBase
except ImportError:  # pragma: no cover
    _ActionBase = object  # type: ignore[assignment,misc]


class VectorIndexerAction(_ActionBase):  # type: ignore[misc,valid-type]
    @classmethod
    def create(cls, config_dict: dict, ctx: Any) -> "VectorIndexerAction":
        """Config keys (all optional, default to CAIRN_* settings): database_url, embedding_provider,
        core_url, core_token."""
        cfg = config_dict or {}
        s = get_settings()
        if cfg.get("embedding_provider"):
            s = s.model_copy(update={"ai": s.ai.model_copy(update={"embedding_provider": cfg["embedding_provider"]})})
        graph = CoreClient(cfg.get("core_url") or s.core.url, cfg.get("core_token") or s.core.token)
        embedder = build_embedder(s)
        store = build_vector_store(get_engine(cfg.get("database_url") or s.database_url), embedder.dim)
        return cls(SemanticIndex(Catalog(graph), embedder, store))

    def __init__(self, index: SemanticIndex):
        self.index = index

    def act(self, event: Any) -> None:
        from datahub_actions.event.event_registry import METADATA_CHANGE_LOG_EVENT_V1_TYPE

        if event.event_type != METADATA_CHANGE_LOG_EVENT_V1_TYPE:
            return
        mcl = event.event.to_obj() if hasattr(event.event, "to_obj") else dict(event.event)
        if isinstance(mcl.get("aspect"), dict) and isinstance(mcl["aspect"].get("value"), bytes):
            mcl["aspect"]["value"] = json.loads(mcl["aspect"]["value"])
        result = self.index.handle_mcl(mcl)
        if result:
            logger.debug("semantic index %s: %s", mcl.get("entityUrn"), result)

    def close(self) -> None:
        pass
