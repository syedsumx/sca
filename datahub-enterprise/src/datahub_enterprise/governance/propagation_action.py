"""``datahub-actions`` plugin: propagate PII classification along column lineage.

Pipeline config (``config/actions/pii_propagation.yml``)::

    action:
      type: dhe_pii_propagation
      config:
        max_hops: 5
        blocking_transforms: [HASH, AGGREGATE, ENCRYPT]

Triggers on MCLs for ``editableSchemaMetadata``, ``schemaMetadata`` (new PII origins)
and ``upstreamLineage`` (new downstreams of existing origins). Writes produced by this
action carry ``systemMetadata.properties.dhe_propagated=true`` and are ignored, which
prevents feedback loops.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel

from datahub_enterprise.common.gms import GmsClient, MetadataGraph
from datahub_enterprise.common.proposals import parse_schema_field_urn
from datahub_enterprise.governance.pii import (
    PROPAGATION_SOURCE,
    GraphLineage,
    apply_terms_mcps,
    pii_origins_from_aspect,
    plan_propagation,
    revert_propagation_mcps,
)

logger = logging.getLogger(__name__)

TRIGGER_ASPECTS = {"editableSchemaMetadata", "schemaMetadata", "upstreamLineage"}

try:  # datahub-actions is an optional dependency
    from datahub_actions.action.action import Action as _ActionBase
except ImportError:  # pragma: no cover - exercised only without the [actions] extra
    _ActionBase = object  # type: ignore[assignment,misc]


class PropagationConfig(BaseModel):
    max_hops: int = 5
    blocking_transforms: list[str] = ["HASH", "AGGREGATE", "COUNT", "ENCRYPT", "TOKENIZE", "MASK"]
    dry_run: bool = False
    gms_url: Optional[str] = None
    gms_token: Optional[str] = None


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
        if props.get("dhe_propagated") == "true":
            return []
        dataset_urn = mcl.get("entityUrn", "")
        if not dataset_urn.startswith("urn:li:dataset:"):
            return []

        if aspect_name == "upstreamLineage":
            # A new lineage edge: re-propagate from PII origins on each upstream dataset.
            lineage = _aspect_from_mcl(mcl) or {}
            upstreams = {u.get("dataset") for u in lineage.get("upstreams", []) if u.get("dataset")}
            for fgl in lineage.get("fineGrainedLineages", []):
                for up in fgl.get("upstreams") or []:
                    if up.startswith("urn:li:schemaField:"):
                        upstreams.add(parse_schema_field_urn(up)[0])
            origins = []
            for ds in upstreams:
                origins.extend(self._origins(ds))
        else:
            origins = self._origins(dataset_urn)
            removed = self._removed_origins(mcl, aspect_name, dataset_urn)
            if removed:
                self._revert(removed)
        if not origins:
            return []
        return self.propagate(origins)

    def propagate(self, origins: list[tuple[str, str]]) -> list[dict]:
        lineage = GraphLineage(self.graph)
        plan = plan_propagation(origins, lineage, self.config.max_hops, self.config.blocking_transforms)
        if not plan:
            return []
        assignments = [
            (step.target_field, step.term, PROPAGATION_SOURCE,
             {"origin": step.origin_field, "hops": str(step.hops), "propagated": "true"})
            for step in plan
        ]
        mcps = apply_terms_mcps(self.graph, assignments)
        if mcps and not self.config.dry_run:
            self.graph.emit(mcps)
        logger.info("PII propagation: %d targets across %d datasets", len(plan), len(mcps))
        return mcps

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
        if config.gms_url:
            graph = GmsClient(config.gms_url, config.gms_token)
        else:
            from datahub_enterprise.common.config import get_settings

            s = get_settings()
            graph = GmsClient(s.gms.url, s.gms.token)
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
