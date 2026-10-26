"""Semantic index: entity documents → embeddings → vector store, kept fresh from MCL."""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from cairn.ai.catalog import Catalog, entity_document, summarize
from cairn.ai.embeddings import Embedder, VectorStore, content_hash
from cairn.ai.guardrails import filter_viewable

logger = logging.getLogger(__name__)

# Aspects whose change alters the embedded document.
INDEXED_ASPECTS = {
    "datasetProperties", "editableDatasetProperties", "schemaMetadata", "editableSchemaMetadata",
    "globalTags", "glossaryTerms", "domains", "ownership", "dashboardInfo", "chartInfo", "dataJobInfo",
    "status",
}


class SemanticIndex:
    def __init__(self, catalog: Catalog, embedder: Embedder, store: VectorStore):
        self.catalog = catalog
        self.embedder = embedder
        self.store = store

    def index_urn(self, urn: str) -> str:
        details = self.catalog.details(urn)
        if not details:
            self.store.delete(urn)
            return "deleted"
        doc = entity_document(details)
        chash = content_hash(doc, self.embedder)
        if self.store.get_hash(urn) == chash:
            return "unchanged"
        vector = self.embedder.embed([doc])[0]
        s = summarize(details)
        self.store.upsert(urn, (details.get("type") or "").lower(), s["name"], doc, vector, chash)
        return "indexed"

    def index_many(self, urns: Iterable[str]) -> dict[str, int]:
        stats = {"indexed": 0, "unchanged": 0, "deleted": 0, "failed": 0}
        for urn in urns:
            try:
                stats[self.index_urn(urn)] += 1
            except Exception as e:
                logger.warning("indexing %s failed: %s", urn, e)
                stats["failed"] += 1
        return stats

    def rebuild(self, entity_types: Optional[list[str]] = None, page: int = 200, max_entities: int = 100_000) -> dict:
        urns: list[str] = []
        for t in entity_types or ["dataset", "dashboard", "chart", "dataJob", "glossaryTerm", "dataProduct"]:
            start = 0
            while len(urns) < max_entities:
                data = self.catalog.graph.graphql(
                    "query($i: SearchAcrossEntitiesInput!){ searchAcrossEntities(input:$i){ total "
                    "searchResults { entity { urn } } } }",
                    {"i": {"types": [t.upper() if t != "dataJob" else "DATA_JOB"], "query": "*",
                           "start": start, "count": page}})
                res = (data.get("searchAcrossEntities") or {})
                batch = [r["entity"]["urn"] for r in res.get("searchResults") or []]
                urns.extend(batch)
                start += page
                if len(batch) < page or start >= int(res.get("total") or 0):
                    break
        return self.index_many(urns)

    def handle_mcl(self, mcl: dict) -> Optional[str]:
        if mcl.get("aspectName") not in INDEXED_ASPECTS:
            return None
        return self.index_urn(mcl["entityUrn"])

    def search(self, query: str, k: int = 10, entity_types: Optional[list[str]] = None,
               can_view: Optional[Callable[[str], bool]] = None) -> list[dict]:
        vector = self.embedder.embed([query])[0]
        # Over-fetch so authorization filtering still leaves k results.
        hits = self.store.search(vector, k * 3, [t.lower() for t in entity_types] if entity_types else None)
        items = [{"urn": h.urn, "type": h.entity_type, "name": h.name, "score": round(h.score, 4),
                  "description": h.snippet} for h in hits]
        return filter_viewable(items, can_view)[:k]
