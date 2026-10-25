"""``datahub-actions`` plugin keeping the semantic index fresh from MCL events."""

from __future__ import annotations

import json
import logging
from typing import Any

from datahub_enterprise.ai.catalog import Catalog
from datahub_enterprise.ai.embeddings import build_embedder, build_vector_store
from datahub_enterprise.ai.semantic import SemanticIndex
from datahub_enterprise.common.config import get_settings
from datahub_enterprise.common.db import get_engine
from datahub_enterprise.common.gms import GmsClient

logger = logging.getLogger(__name__)

try:
    from datahub_actions.action.action import Action as _ActionBase
except ImportError:  # pragma: no cover
    _ActionBase = object  # type: ignore[assignment,misc]


class VectorIndexerAction(_ActionBase):  # type: ignore[misc,valid-type]
    @classmethod
    def create(cls, config_dict: dict, ctx: Any) -> "VectorIndexerAction":
        """Config keys (all optional, default to DHE_* settings): database_url, embedding_provider,
        gms_url, gms_token."""
        cfg = config_dict or {}
        s = get_settings()
        if cfg.get("embedding_provider"):
            s = s.model_copy(update={"ai": s.ai.model_copy(update={"embedding_provider": cfg["embedding_provider"]})})
        graph = GmsClient(cfg.get("gms_url") or s.gms.url, cfg.get("gms_token") or s.gms.token)
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
