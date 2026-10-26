"""``cairn-executor`` plugin: propagate PII classification along column lineage.

Pipeline config (``config/actions/pii_propagation.yml``)::

    action:
      type: cairn_pii_propagation
      config:
        max_hops: 5
        blocking_transforms: [HASH, AGGREGATE, ENCRYPT]

Triggers on MCLs for ``editableSchemaMetadata``, ``schemaMetadata`` (new PII origins)
and ``upstreamLineage`` (new downstreams of existing origins). Writes produced by this
action carry ``systemMetadata.properties.cairn_propagated=true`` and are ignored, which
prevents feedback loops.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from pydantic import BaseModel

from cairn.common.core import CoreClient, MetadataGraph
from cairn.common.proposals import parse_schema_field_urn
from cairn.governance.pii import (
    PROPAGATION_SOURCE,
    GraphLineage,
    apply_terms_mcps,
    pii_origins_from_aspect,
    plan_propagation,
    revert_propagation_mcps,
)

logger = logging.getLogger(__name__)

TRIGGER_ASPECTS = {"editableSchemaMetadata", "schemaMetadata", "upstreamLineage"}

try:  # cairn-executor is an optional dependency
    from datahub_actions.action.action import Action as _ActionBase
except ImportError:  # pragma: no cover - exercised only without the [actions] extra
    _ActionBase = object  # type: ignore[assignment,misc]


class PropagationConfig(BaseModel):
    max_hops: int = 5
    blocking_transforms: list[str] = ["HASH", "AGGREGATE", "COUNT", "ENCRYPT", "TOKENIZE", "MASK"]
    dry_run: bool = False
    # Labels written right after new lineage can reach us before the lineage index has the edge:
    # origins that found no downstream edges are re-checked once after this delay (0 disables).
    recheck_seconds: float = 30.0
    core_url: Optional[str] = None
    core_token: Optional[str] = None


def lineage_hints(mcl: dict) -> tuple[set[str], dict[str, set[str]]]:
    """Upstream datasets of an ``upstreamLineage`` change, plus upstream → {this dataset} hints."""
    downstream = mcl.get("entityUrn", "")
    lineage = _aspect_from_mcl(mcl) or {}
    upstreams = {u.get("dataset") for u in lineage.get("upstreams", []) if u.get("dataset")}
    for fgl in lineage.get("fineGrainedLineages", []):
        upstreams |= {parse_schema_field_urn(u)[0] for u in fgl.get("upstreams") or []
                      if u.startswith("urn:li:schemaField:")}
    return upstreams, {u: {downstream} for u in upstreams}


def schedule_recheck(seconds: float, fn, *args) -> None:
    if seconds <= 0:
        return
    t = threading.Timer(seconds, fn, args)
    t.daemon = True
    t.start()


class PiiPropagator:
    """Transport-independent core so it can be unit-tested without Kafka."""

    def __init__(self, graph: MetadataGraph, config: PropagationConfig):
        self.graph = graph
        self.config = config

    def handle(self, mcl: dict) -> list[dict]:
        aspect_name = mcl.get("aspectName")
        if aspect_name not in TRIGGER_ASPECTS:
            return []
        props = ((mcl.get("systemMetadata") or {}).get("properties") or {})
        if props.get("cairn_propagated") == "true":
            return []
        dataset_urn = mcl.get("entityUrn", "")
        if not dataset_urn.startswith("urn:li:dataset:"):
            return []

        if aspect_name == "upstreamLineage":
            # A new lineage edge: re-propagate from PII origins on each upstream dataset.
            upstreams, hints = lineage_hints(mcl)
            origins = [o for ds in sorted(upstreams) for o in self._origins(ds)]
            return self.propagate(origins, hints) if origins else []
        origins = self._origins(dataset_urn)
        removed = self._removed_origins(mcl, aspect_name, dataset_urn)
        if removed:
            self._revert(removed)
        if not origins:
            return []
        planned, mcps = self._propagate(origins)
        if not planned:
            schedule_recheck(self.config.recheck_seconds, self.propagate, origins)
        return mcps

    def propagate(self, origins: list[tuple[str, str]], hints: Optional[dict[str, set[str]]] = None) -> list[dict]:
        return self._propagate(origins, hints)[1]

    def _propagate(self, origins, hints=None) -> tuple[int, list[dict]]:
        lineage = GraphLineage(self.graph, hints)
        plan = plan_propagation(origins, lineage, self.config.max_hops, self.config.blocking_transforms)
        if not plan:
            return 0, []
        assignments = [
            (step.target_field, step.term, PROPAGATION_SOURCE,
             {"origin": step.origin_field, "hops": str(step.hops), "propagated": "true"})
            for step in plan
        ]
        mcps = apply_terms_mcps(self.graph, assignments)
        if mcps and not self.config.dry_run:
            self.graph.emit(mcps)
        logger.info("PII propagation: %d targets across %d datasets", len(plan), len(mcps))
        return len(plan), mcps

    def _origins(self, dataset_urn: str) -> list[tuple[str, str]]:
        origins = []
        esm = self.graph.get_aspect(dataset_urn, "editableSchemaMetadata")
        if esm:
            origins += pii_origins_from_aspect(dataset_urn, esm, "editableSchemaFieldInfo")
        sm = self.graph.get_aspect(dataset_urn, "schemaMetadata")
        if sm:
            origins += pii_origins_from_aspect(dataset_urn, sm, "fields")
        return origins

    def _removed_origins(self, mcl: dict, aspect_name: str, dataset_urn: str) -> list[tuple[str, str]]:
        list_key = "editableSchemaFieldInfo" if aspect_name == "editableSchemaMetadata" else "fields"
        prev = _aspect_from_mcl(mcl, previous=True)
        cur = _aspect_from_mcl(mcl)
        if not prev:
            return []
        before = set(pii_origins_from_aspect(dataset_urn, prev, list_key))
        after = set(pii_origins_from_aspect(dataset_urn, cur or {}, list_key))
        return sorted(before - after)

    def _revert(self, removed: list[tuple[str, str]]) -> None:
        lineage = GraphLineage(self.graph)
        for origin, term in removed:
            plan = plan_propagation([(origin, term)], lineage, self.config.max_hops, [])
            datasets = {parse_schema_field_urn(s.target_field)[0] for s in plan}
            mcps = revert_propagation_mcps(self.graph, datasets, origin, term)
            if mcps and not self.config.dry_run:
                self.graph.emit(mcps)


def _aspect_from_mcl(mcl: dict, previous: bool = False) -> Optional[dict]:
    key = "previousAspectValue" if previous else "aspect"
    raw: Any = mcl.get(key)
    if not raw:
        return None
    value = raw.get("value") if isinstance(raw, dict) else raw
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    return json.loads(value) if isinstance(value, str) else value


class PiiPropagationAction(_ActionBase):  # type: ignore[misc,valid-type]
    @classmethod
    def create(cls, config_dict: dict, ctx: Any) -> "PiiPropagationAction":
        config = PropagationConfig.model_validate(config_dict or {})
        graph: MetadataGraph
        from cairn.common.config import get_settings

        s = get_settings()
        # The token file (rotated by cairn-token-keeper) wins over a token passed in the config.
        graph = CoreClient(config.core_url or s.core.url, config.core_token or s.core.token,
                           token_file=s.core.token_file)
        return cls(PiiPropagator(graph, config))

    def __init__(self, propagator: PiiPropagator):
        self.propagator = propagator

    def act(self, event: Any) -> None:
        from datahub_actions.event.event_registry import METADATA_CHANGE_LOG_EVENT_V1_TYPE

        if event.event_type != METADATA_CHANGE_LOG_EVENT_V1_TYPE:
            return
        mcl = event.event.to_obj() if hasattr(event.event, "to_obj") else dict(event.event)
        try:
            self.propagator.handle(mcl)
        except Exception:
            logger.exception("PII propagation failed for %s", mcl.get("entityUrn"))
            raise  # let the pipeline's retry/failure semantics apply

    def close(self) -> None:
        pass
