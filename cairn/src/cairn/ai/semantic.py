"""Semantic index: entity documents → embeddings → vector store, kept fresh from MCL."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional

from cairn.ai.catalog import ENTITY_TYPES, Catalog, entity_document, summarize
from cairn.ai.embeddings import Embedder, VectorStore, content_hash, matched_terms
from cairn.ai.guardrails import filter_viewable

logger = logging.getLogger(__name__)

# Aspects whose change alters the embedded document.
INDEXED_ASPECTS = {
    "datasetProperties", "editableDatasetProperties", "schemaMetadata", "editableSchemaMetadata",
    "globalTags", "glossaryTerms", "domains", "ownership", "dashboardInfo", "chartInfo", "dataJobInfo",
    "status",
}


DEFAULT_TYPES = ["dataset", "dashboard", "chart", "dataJob", "glossaryTerm", "dataProduct"]


class SemanticIndex:
    def __init__(self, catalog: Catalog, embedder: Embedder, store: VectorStore):
        self.catalog = catalog
        self.embedder = embedder
        self.store = store
        self.last_rebuild: Optional[dict] = None
        self._rebuild_lock = threading.Lock()

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
        """Index every entity of these types (unchanged documents are skipped). A full rebuild also
        drops entries for entities that no longer exist."""
        if not self._rebuild_lock.acquire(blocking=False):
            return {"status": "already running"}
        try:
            started = time.time()
            urns, complete = self._all_urns(entity_types, page, max_entities)
            stats = self.index_many(urns)
            # Prune only after a complete listing: a type that failed to list must not lose its entries.
            if entity_types is None and complete and len(urns) < max_entities:
                gone = self.store.urns() - set(urns)
                for urn in gone:
                    self.store.delete(urn)
                stats["pruned"] = len(gone)
            stats["seconds"] = round(time.time() - started, 2)
            self.last_rebuild = {**stats, "finished_at": int(time.time() * 1000)}
            return stats
        finally:
            self._rebuild_lock.release()

    def _all_urns(self, entity_types: Optional[list[str]], page: int,
                  max_entities: int) -> tuple[list[str], bool]:
        urns: list[str] = []
        complete = True
        for t in entity_types or DEFAULT_TYPES:
            start = 0
            try:
                while len(urns) < max_entities:
                    data = self.catalog.graph.graphql(
                        "query($i: SearchAcrossEntitiesInput!){ searchAcrossEntities(input:$i){ total "
                        "searchResults { entity { urn } } } }",
                        {"i": {"types": [ENTITY_TYPES.get(t, t.upper())], "query": "*", "start": start,
                               "count": page}})
                    res = (data.get("searchAcrossEntities") or {})
                    batch = [r["entity"]["urn"] for r in res.get("searchResults") or []]
                    urns.extend(batch)
                    start += page
                    if len(batch) < page or start >= int(res.get("total") or 0):
                        break
            except Exception as e:  # noqa: BLE001 - one entity type must not stop the others
                logger.warning("listing %s for the semantic index failed: %s", t, e)
                complete = False
        return list(dict.fromkeys(urns)), complete

    def status(self) -> dict:
        counts = self.store.counts()
        return {"embedder": self.embedder.name, "dimensions": self.embedder.dim, "entries": sum(counts.values()),
                "by_type": counts, "last_rebuild": self.last_rebuild}

    def ensure_built(self) -> dict:
        """Bring the index in line with the catalog at start: fills an empty index (first start,
        or the embedder's dimension changed) and re-embeds documents whose embedder changed;
        everything else is skipped by content hash."""
        if not self.store.counts():
            logger.info("semantic index is empty: indexing the catalog")
        return self.rebuild()

    def handle_mcl(self, mcl: dict) -> Optional[str]:
        if mcl.get("aspectName") not in INDEXED_ASPECTS:
            return None
        return self.index_urn(mcl["entityUrn"])

    def search(self, query: str, k: int = 10, entity_types: Optional[list[str]] = None,
               can_view: Optional[Callable[[str], bool]] = None, exclude: Iterable[str] = (),
               min_score: float = 0.0) -> list[dict]:
        vector = self.embedder.embed([query])[0]
        return self._nearest(vector, k, entity_types, can_view, set(exclude), min_score, query)

    def similar(self, urn: str, k: int = 5, entity_types: Optional[list[str]] = None,
                can_view: Optional[Callable[[str], bool]] = None) -> list[dict]:
        """Assets whose documentation, schema and tags read most like ``urn``'s."""
        vector = self.store.get_vector(urn)
        if vector is None:
            self.index_urn(urn)
            vector = self.store.get_vector(urn)
            if vector is None:
                return []
        return self._nearest(vector, k, entity_types, can_view, {urn}, 0.05, None)

    def _nearest(self, vector, k, entity_types, can_view, exclude: set, min_score: float,
                 query: Optional[str]) -> list[dict]:
        # Over-fetch so authorization filtering and exclusions still leave k results.
        hits = self.store.search(vector, k * 3 + len(exclude),
                                 [t.lower() for t in entity_types] if entity_types else None)
        items = []
        for h in hits:
            if h.urn in exclude or h.score < min_score:
                continue
            fields = _document_fields(h.snippet)
            item = {"urn": h.urn, "type": h.entity_type, "name": h.name, "score": round(h.score, 4),
                    "platform": fields.get("platform"),
                    "description": (fields.get("description") or h.snippet)[:300]}
            if query is not None:
                item["matched"] = matched_terms(query, f"{h.name} {h.snippet}")
            items.append(item)
        return filter_viewable(items, can_view)[:k]


def _document_fields(doc: str) -> dict[str, str]:
    """``key: value`` lines of an indexed entity document (see :func:`entity_document`)."""
    out: dict[str, str] = {}
    for line in doc.splitlines()[1:]:
        key, sep, value = line.partition(": ")
        if sep and key in ("platform", "description", "domain") and key not in out:
            out[key] = value
    return out

