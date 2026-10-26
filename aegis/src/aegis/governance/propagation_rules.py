"""Rule-based tag + glossary-term propagation over column lineage (generalises PII propagation).

Pipeline config::

    action:
      type: aegis_label_propagation
      config:
        rules:
          - id: pii
            labels: ["urn:li:glossaryTerm:Classification.PII.*"]
          - id: gdpr-tags
            labels: ["urn:li:tag:gdpr*", "urn:li:tag:Confidential"]
            max_hops: 3
            target_platforms: [snowflake, looker]    # only label columns on these platforms
          - id: finance-terms
            labels: ["urn:li:glossaryTerm:Finance.*"]
            blocking_transforms: [AGGREGATE]          # aggregates do not inherit the term

Each rule names the labels it carries (exact urns or ``*`` prefixes; tags and terms both live on
``editableSchemaMetadata`` field info). Copies are attributed to the rule's source urn with the
origin field, so they are reverted precisely when the origin label is removed, and the writes are
marked ``aegis_propagated`` so they never re-trigger propagation.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from aegis.common.gms import GmsClient, MetadataGraph
from aegis.common.proposals import (
    AEGIS_ACTOR,
    field_label_op,
    glossary_term_association,
    parse_schema_field_urn,
    patch_mcp,
    schema_field_urn,
)
from aegis.governance.pii import GraphLineage, plan_propagation
from aegis.governance.propagation_action import (
    TRIGGER_ASPECTS,
    _aspect_from_mcl,
    lineage_hints,
    schedule_recheck,
)

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "urn:li:dataHubAction:aegis-propagation:"
DEFAULT_BLOCKING = ["HASH", "AGGREGATE", "COUNT", "ENCRYPT", "TOKENIZE", "MASK"]


class PropagationRule(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{0,62}$")
    labels: list[str] = Field(min_length=1)
    max_hops: int = Field(default=5, ge=1, le=20)
    blocking_transforms: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKING))
    target_platforms: list[str] = Field(default_factory=list, description="empty = any platform")
    enabled: bool = True

    @field_validator("labels")
    @classmethod
    def _urns(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if not x.startswith(("urn:li:tag:", "urn:li:glossaryTerm:"))]
        if bad:
            raise ValueError(f"labels must be tag or glossary term urns (globs allowed): {bad}")
        return v

    @property
    def source(self) -> str:
        return SOURCE_PREFIX + self.id

    def matches(self, label: str) -> bool:
        return any(fnmatch.fnmatchcase(label, p) for p in self.labels)

    def targets_platform(self, dataset_urn: str) -> bool:
        if not self.target_platforms:
            return True
        platform = dataset_urn.split("urn:li:dataPlatform:", 1)[-1].split(",", 1)[0]
        return platform in self.target_platforms


class RulesConfig(BaseModel):
    rules: list[PropagationRule] = Field(default_factory=list)
    dry_run: bool = False
    recheck_seconds: float = 30.0   # see PropagationConfig.recheck_seconds
    gms_url: Optional[str] = None
    gms_token: Optional[str] = None


# --------------------------------------------------------------------------- aspect helpers
def _is_tag(label: str) -> bool:
    return label.startswith("urn:li:tag:")


def _labels_of(info: dict) -> list[tuple[str, dict]]:
    """(label urn, association) for every term and tag on a field."""
    out = [(t.get("urn", ""), t) for t in (info.get("glossaryTerms") or {}).get("terms", [])]
    out += [(t.get("tag", ""), t) for t in (info.get("globalTags") or {}).get("tags", [])]
    return out


def rule_origins(rule: PropagationRule, dataset_urn: str, aspect: dict, list_key: str) -> list[tuple[str, str]]:
    """(field urn, label) pairs matching ``rule`` that were not themselves propagated by a Aegis rule."""
    out = []
    for f in aspect.get(list_key, []):
        for label, assoc in _labels_of(f):
            source = (assoc.get("attribution") or {}).get("source") or ""
            if label and rule.matches(label) and not source.startswith(SOURCE_PREFIX):
                out.append((schema_field_urn(dataset_urn, f["fieldPath"]), label))
    return out


def apply_labels_mcps(graph: MetadataGraph, assignments: list[tuple[str, str, str, dict]],
                      actor: str = AEGIS_ACTOR) -> list[dict]:
    """``editableSchemaMetadata`` PATCH proposals adding ``(field_urn, label, source, detail)``.

    PATCH (not read-modify-write) because several propagation pipelines label the same columns
    concurrently; labels already present on the field are skipped.
    """
    by_dataset: dict[str, list[tuple[str, str, str, dict]]] = {}
    for field_urn, label, source, detail in assignments:
        ds, path = parse_schema_field_urn(field_urn)
        by_dataset.setdefault(ds, []).append((path, label, source, detail))
    mcps = []
    for ds, items in by_dataset.items():
        esm = graph.get_aspect(ds, "editableSchemaMetadata")
        ops = []
        for path, label, source, detail in items:
            if any(existing == label for existing, _ in _labels_of(_field(esm, path))):
                continue
            assoc = glossary_term_association(label, actor, source, detail)
            if _is_tag(label):
                assoc = {"tag": label, "attribution": assoc["attribution"]}
            ops.append(field_label_op("add", path, label, assoc))
        if ops:
            mcps.append(patch_mcp(ds, "editableSchemaMetadata", ops, system_properties={"aegis_propagated": "true"}))
    return mcps


def revert_labels_mcps(graph: MetadataGraph, dataset_urns, source: str, origin_field: str, label: str) -> list[dict]:
    mcps = []
    for ds in dataset_urns:
        esm = graph.get_aspect(ds, "editableSchemaMetadata")
        ops = []
        for info in (esm or {}).get("editableSchemaFieldInfo", []):
            for urn, assoc in _labels_of(info):
                attr = assoc.get("attribution") or {}
                if (urn == label and attr.get("source") == source
                        and (attr.get("sourceDetail") or {}).get("origin") == origin_field):
                    ops.append(field_label_op("remove", info["fieldPath"], label))
        if ops:
            mcps.append(patch_mcp(ds, "editableSchemaMetadata", ops, system_properties={"aegis_propagated": "true"}))
    return mcps


def _field(esm: Optional[dict], path: str) -> dict:
    for info in (esm or {}).get("editableSchemaFieldInfo", []):
        if info.get("fieldPath") == path:
            return info
    return {}


# --------------------------------------------------------------------------- propagator
class RulePropagator:
    def __init__(self, graph: MetadataGraph, config: RulesConfig):
        self.graph = graph
        self.config = config

    @property
    def rules(self) -> list[PropagationRule]:
        return [r for r in self.config.rules if r.enabled]

    def handle(self, mcl: dict) -> list[dict]:
        aspect_name = mcl.get("aspectName")
        if aspect_name not in TRIGGER_ASPECTS:
            return []
        if ((mcl.get("systemMetadata") or {}).get("properties") or {}).get("aegis_propagated") == "true":
            return []
        dataset_urn = mcl.get("entityUrn", "")
        if not dataset_urn.startswith("urn:li:dataset:"):
            return []
        hints: Optional[dict[str, set[str]]] = None
        if aspect_name == "upstreamLineage":
            datasets, hints = lineage_hints(mcl)
        else:
            datasets = {dataset_urn}
            self._revert_removed(mcl, aspect_name, dataset_urn)
        mcps: list[dict] = []
        for rule in self.rules:
            origins = [o for ds in sorted(datasets) for o in self._origins(rule, ds)]
            if not origins:
                continue
            planned, out = self._propagate(rule, origins, hints)
            mcps += out
            if not planned and hints is None:
                schedule_recheck(self.config.recheck_seconds, self.propagate, rule, origins)
        return mcps

    def propagate(self, rule: PropagationRule, origins: list[tuple[str, str]],
                  hints: Optional[dict[str, set[str]]] = None) -> list[dict]:
        return self._propagate(rule, origins, hints)[1]

    def _propagate(self, rule, origins, hints=None) -> tuple[int, list[dict]]:
        plan = plan_propagation(origins, GraphLineage(self.graph, hints), rule.max_hops, rule.blocking_transforms)
        plan = [s for s in plan if rule.targets_platform(parse_schema_field_urn(s.target_field)[0])]
        if not plan:
            return 0, []
        mcps = apply_labels_mcps(self.graph, [
            (s.target_field, s.term, rule.source, {"origin": s.origin_field, "hops": str(s.hops),
                                                   "rule": rule.id, "propagated": "true"}) for s in plan])
        if mcps and not self.config.dry_run:
            self.graph.emit(mcps)
        logger.info("rule %s: propagated to %d fields across %d datasets", rule.id, len(plan), len(mcps))
        return len(plan), mcps

    def _origins(self, rule: PropagationRule, dataset_urn: str) -> list[tuple[str, str]]:
        out = []
        for aspect_name, key in (("editableSchemaMetadata", "editableSchemaFieldInfo"), ("schemaMetadata", "fields")):
            aspect = self.graph.get_aspect(dataset_urn, aspect_name)
            if aspect:
                out += rule_origins(rule, dataset_urn, aspect, key)
        return out

    def _revert_removed(self, mcl: dict, aspect_name: str, dataset_urn: str) -> None:
        prev = _aspect_from_mcl(mcl, previous=True)
        if not prev:
            return
        key = "editableSchemaFieldInfo" if aspect_name == "editableSchemaMetadata" else "fields"
        cur = _aspect_from_mcl(mcl) or {}
        lineage = GraphLineage(self.graph)
        for rule in self.rules:
            removed = set(rule_origins(rule, dataset_urn, prev, key)) - set(rule_origins(rule, dataset_urn, cur, key))
            for origin, label in sorted(removed):
                plan = plan_propagation([(origin, label)], lineage, rule.max_hops, [])
                targets = {parse_schema_field_urn(s.target_field)[0] for s in plan}
                mcps = revert_labels_mcps(self.graph, targets, rule.source, origin, label)
                if mcps and not self.config.dry_run:
                    self.graph.emit(mcps)


try:  # datahub-actions is an optional dependency
    from datahub_actions.action.action import Action as _ActionBase
except ImportError:  # pragma: no cover
    _ActionBase = object  # type: ignore[assignment,misc]


class LabelPropagationAction(_ActionBase):  # type: ignore[misc,valid-type]
    @classmethod
    def create(cls, config_dict: dict, ctx: Any) -> "LabelPropagationAction":
        config = RulesConfig.model_validate(config_dict or {})
        if config.gms_url:
            graph: MetadataGraph = GmsClient(config.gms_url, config.gms_token)
        else:
            from aegis.common.config import get_settings

            s = get_settings()
            graph = GmsClient(s.gms.url, s.gms.token, token_file=s.gms.token_file)
        return cls(RulePropagator(graph, config))

    def __init__(self, propagator: RulePropagator):
        self.propagator = propagator

    def act(self, event: Any) -> None:
        from datahub_actions.event.event_registry import METADATA_CHANGE_LOG_EVENT_V1_TYPE

        if event.event_type != METADATA_CHANGE_LOG_EVENT_V1_TYPE:
            return
        mcl = event.event.to_obj() if hasattr(event.event, "to_obj") else dict(event.event)
        try:
            self.propagator.handle(mcl)
        except Exception:
            logger.exception("label propagation failed for %s", mcl.get("entityUrn"))
            raise

    def close(self) -> None:
        pass
