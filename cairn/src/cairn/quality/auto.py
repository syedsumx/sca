"""Auto-monitors: rules that keep monitors on every dataset matching a condition.

A rule selects datasets with the same condition grammar as policies, evaluated on the dataset's
attributes (``resource.domains``, ``resource.tags``, ``resource.classification``,
``resource.certification``, ``resource.platform``, ``resource.data_products``, ``resource.urn`` ...)
and lists monitor templates. The sweep makes the monitors match the rules:

* a matching dataset without the rule's monitors gets them;
* a changed template updates the monitors it owns;
* a dataset that stops matching (or is removed) loses them;
* monitors people created, and auto monitors someone edited ("detached"), are never touched.

Rules live in YAML files (``CAIRN_AUTO_MONITORS``: a file or a directory, default
``/etc/cairn/config/quality``), reviewed like policies::

    rules:
      - id: certified
        description: Freshness and volume on every certified dataset
        select: {attr: resource.certification, op: eq, value: CERTIFIED}
        monitors:
          - {type: freshness, severity: HIGH}
          - {type: volume}
      - id: pii-nulls
        select: {attr: resource.classification, op: contains, value: PII}
        monitors:
          - {type: field_metric, fields: {labelled: [PII]}, config: {metric: null_pct}}
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.governance.abac import evaluate_condition, validate_condition
from cairn.governance.attributes import resource_from_aspects
from cairn.quality.models import MonitorSpec, Schedule

logger = logging.getLogger(__name__)

AUTO_PREFIX = "auto-"
CREATED_BY = "auto-monitors"
DETACHED = "auto_detached"        # config flag set when someone edits an auto monitor
_SCROLL_ASPECTS = ["domains", "globalTags", "glossaryTerms", "structuredProperties", "editableSchemaMetadata",
                   "schemaMetadata", "ownership", "container", "dataPlatformInstance", "status"]


class FieldSelector(BaseModel):
    """Columns a field template applies to: by name pattern and/or by label (tag, term, classification)."""

    names: list[str] = Field(default_factory=list)
    labelled: list[str] = Field(default_factory=list)


class MonitorTemplate(BaseModel):
    type: Literal["freshness", "volume", "field_metric", "schema"]
    schedule: Schedule = Field(default_factory=lambda: Schedule(interval_seconds=3600))
    sensitivity: Literal["low", "medium", "high"] = "medium"
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    config: dict[str, Any] = Field(default_factory=dict)
    fields: Optional[FieldSelector] = None
    notify: list[str] = Field(default_factory=list)
    incident_after_failures: int = Field(default=1, ge=1, le=100)


class ProfileTemplate(BaseModel):
    """Profile matching datasets over a SQL connection when the catalog has no recent profile."""

    connection: str
    every_hours: float = Field(default=24, gt=0)
    sample_values: int = Field(default=0, ge=0, le=20, description="distinct sample values per column (0 = none)")
    drop_database: bool = True


class AutoRule(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
    description: str = ""
    enabled: bool = True
    select: dict[str, Any]
    monitors: list[MonitorTemplate] = Field(default_factory=list)
    profile: Optional[ProfileTemplate] = None
    max_datasets: int = Field(default=1000, ge=1, le=100_000)

    @field_validator("select")
    @classmethod
    def _valid_select(cls, v: dict) -> dict:
        validate_condition(v)
        return v

    @model_validator(mode="after")
    def _does_something(self) -> "AutoRule":
        if not self.monitors and not self.profile:
            raise ValueError("a rule needs monitors, a profile, or both")
        return self


class AutoRules(BaseModel):
    rules: list[AutoRule] = Field(default_factory=list)

    @field_validator("rules")
    @classmethod
    def _unique(cls, v: list[AutoRule]) -> list[AutoRule]:
        ids = [r.id for r in v]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return v


def load_rules(path: Optional[str] = None) -> AutoRules:
    """Rules from a YAML file, or from every ``*.yaml``/``*.yml`` in a directory (merged, sorted by
    name). Default ``CAIRN_AUTO_MONITORS`` or ``/etc/cairn/config/quality``. Read on every sweep."""
    import yaml

    path = path or os.environ.get("CAIRN_AUTO_MONITORS", "/etc/cairn/config/quality")
    if not path or not os.path.exists(path):
        return AutoRules()
    files = [path] if os.path.isfile(path) else sorted(
        os.path.join(path, f) for f in os.listdir(path) if f.endswith((".yaml", ".yml")))
    rules: list = []
    for f in files:
        with open(f) as fh:
            rules += (yaml.safe_load(fh) or {}).get("rules") or []
    return AutoRules.model_validate({"rules": rules})


def monitor_id(rule_id: str, template_index: int, dataset_urn: str, field: Optional[str] = None) -> str:
    digest = hashlib.sha1(f"{dataset_urn}|{field or ''}".encode()).hexdigest()[:16]
    return f"{AUTO_PREFIX}{rule_id}-{template_index}-{digest}"[:64]


def _dataset_name(urn: str) -> str:
    return urn.split(",")[1] if urn.count(",") >= 2 else urn


def _fields(resource: dict, aspects: dict, selector: FieldSelector) -> list[str]:
    paths = {f.get("fieldPath") for f in (aspects.get("schemaMetadata") or {}).get("fields", []) if f.get("fieldPath")}
    labels = resource.get("field_labels") or {}
    paths |= set(labels)
    out = []
    for path in sorted(p for p in paths if p):
        by_name = any(fnmatch.fnmatchcase(path, pat) for pat in selector.names)
        by_label = bool(set(selector.labelled) & set(labels.get(path, [])))
        if by_name or by_label:
            out.append(path)
    return out


class Planner:
    """Desired auto monitors for the catalog's current state."""

    def __init__(self, graph):
        self.graph = graph

    def datasets(self) -> list[tuple[str, dict, dict]]:
        products: dict[str, list[str]] = {}
        try:
            for row in self.graph.scroll_entities("dataProduct", ["dataProductProperties"]):
                for asset in (row.get("dataProductProperties") or {}).get("assets", []) or []:
                    if asset.get("destinationUrn"):
                        products.setdefault(asset["destinationUrn"], []).append(row["urn"])
        except Exception as e:  # noqa: BLE001 - products are an optional selector
            logger.warning("auto-monitors: data product scan failed: %s", e)
        out = []
        for row in self.graph.scroll_entities("dataset", _SCROLL_ASPECTS):
            if (row.get("status") or {}).get("removed"):
                continue
            resource = resource_from_aspects(row["urn"], row)
            resource["data_products"] = sorted(set(resource.get("data_products") or []) | set(products.get(row["urn"], [])))
            out.append((row["urn"], resource, row))
        return out

    def matches(self, rules: AutoRules, datasets: Optional[list] = None) -> dict[str, list[tuple[str, dict, dict]]]:
        """Datasets each enabled rule selects (capped at the rule's max_datasets)."""
        datasets = self.datasets() if datasets is None else datasets
        out = {}
        for rule in rules.rules:
            if not rule.enabled:
                continue
            matched = [(u, r, a) for u, r, a in datasets if evaluate_condition(rule.select, {"resource": r})]
            if len(matched) > rule.max_datasets:
                logger.warning("auto-monitors: rule %s matches %d datasets (max %d); using the first %d",
                               rule.id, len(matched), rule.max_datasets, rule.max_datasets)
                matched = sorted(matched, key=lambda m: m[0])[:rule.max_datasets]
            out[rule.id] = matched
        return out

    def plan(self, rules: AutoRules, matches: Optional[dict] = None) -> dict[str, MonitorSpec]:
        desired: dict[str, MonitorSpec] = {}
        matches = self.matches(rules) if matches is None else matches
        for rule in rules.rules:
            for urn, resource, aspects in matches.get(rule.id, []):
                for i, t in enumerate(rule.monitors):
                    targets = _fields(resource, aspects, t.fields) if t.type == "field_metric" and t.fields \
                        else [t.config.get("field")] if t.type == "field_metric" else [None]
                    for field in targets:
                        if t.type == "field_metric" and not field:
                            continue
                        config = {**t.config, **({"field": field} if field else {}), "auto_rule": rule.id}
                        label = f"{_dataset_name(urn)}{'.' + field if field else ''}"
                        desired[monitor_id(rule.id, i, urn, field)] = MonitorSpec(
                            name=f"{label} {t.type.replace('_', ' ')} (auto: {rule.id})", dataset_urn=urn,
                            type=t.type, schedule=t.schedule, config=config, sensitivity=t.sensitivity,
                            severity=t.severity, notify=t.notify, incident_after_failures=t.incident_after_failures)
        return desired


def _comparable(spec: MonitorSpec) -> dict:
    return spec.model_dump(mode="json")


def sweep(store, engine, graph, rules: AutoRules, dry_run: bool = False, connections=None,
          now: Optional[int] = None) -> dict:
    """Make auto monitors match the rules, and profile what is due. Returns what changed (or would
    change, with ``dry_run``). ``connections`` (a ConnectionRegistry) enables profiling."""
    planner = Planner(graph)
    matches = planner.matches(rules)
    desired = planner.plan(rules, matches)
    existing = {m.id: m for m in store.list_monitors() if m.id.startswith(AUTO_PREFIX)}
    report: dict[str, list[str]] = {"created": [], "updated": [], "removed": [], "detached": []}
    for mid, spec in desired.items():
        current = existing.get(mid)
        if current is None:
            report["created"].append(mid)
            if not dry_run:
                engine.sync_monitor(store.create_monitor(spec, CREATED_BY, monitor_id=mid))
        elif current.config.get(DETACHED):
            report["detached"].append(mid)
        elif _comparable(MonitorSpec(**current.model_dump(include=set(MonitorSpec.model_fields)))) != _comparable(spec):
            report["updated"].append(mid)
            if not dry_run:
                engine.sync_monitor(store.update_monitor(mid, spec))
    for mid, current in existing.items():
        if mid in desired:
            continue
        if current.config.get(DETACHED):
            report["detached"].append(mid)
            continue
        report["removed"].append(mid)
        if not dry_run:
            store.delete_monitor(mid)
            engine.remove_monitor(mid)
    report["profiled"], report["profile_errors"] = [], []
    if connections is not None:
        _profile_due(rules, matches, graph, connections, report, dry_run, now)
    for k in report:
        report[k].sort()
    if any(report[k] for k in ("created", "updated", "removed")):
        logger.info("auto-monitors%s: %d created, %d updated, %d removed", " (dry run)" if dry_run else "",
                    len(report["created"]), len(report["updated"]), len(report["removed"]))
    return report


def _profile_due(rules: AutoRules, matches: dict, graph, connections, report: dict, dry_run: bool,
                 now: Optional[int]) -> None:
    from cairn.common.proposals import now_ms
    from cairn.quality.profiler import profile_mcp, profile_table

    now = now if now is not None else now_ms()
    done: set[str] = set()
    for rule in rules.rules:
        p = rule.profile
        if p is None:
            continue
        for urn, _resource, _aspects in matches.get(rule.id, []):
            if urn in done:
                continue
            latest = (graph.get_timeseries(urn, "datasetProfile", 1) or [{}])[0].get("timestampMillis") or 0
            if now - latest < p.every_hours * 3600_000:
                continue
            done.add(urn)
            if dry_run:
                report["profiled"].append(urn)
                continue
            try:
                profile = profile_table(connections.engine(p.connection), urn, p.drop_database, p.sample_values)
                graph.emit([profile_mcp(urn, profile)])
                report["profiled"].append(urn)
            except Exception as e:  # noqa: BLE001 - one table must not stop the others
                logger.warning("auto-monitors: profiling %s failed: %s", urn, e)
                report["profile_errors"].append(f"{urn}: {e}")
