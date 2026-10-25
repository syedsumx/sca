"""Response masking for metadata that carries *data values*.

Only values derived from the underlying data are masked: field-profile samples and
statistics, and literals inside SQL text. Names, schemas, descriptions and lineage stay
visible: hiding them breaks discovery without protecting any data.

The transformer is pure: ``mask_document(doc, planner)`` walks a decoded JSON document (a
GraphQL or OpenAPI response), tracks the nearest enclosing dataset URN, and asks the
``planner`` which columns of that dataset must be masked for the current caller.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

PROFILE_VALUE_KEYS = ("sampleValues", "min", "max", "mean", "median", "stdev", "quantiles",
                      "distinctValueFrequencies", "distinctValueList", "histogram")
SQL_KEYS = ("topSqlQueries",)
# Bytes that must appear in a response before it is worth decoding and walking.
TRIGGERS = (b'"fieldProfiles"', b'"sampleValues"', b'"statement"', b'"topSqlQueries"', b'"datasetProfile"')

_SQL_STRING = re.compile(r"'(?:[^']|'')*'")
_SQL_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_DATASET = "urn:li:dataset:"


def needs_inspection(body: bytes) -> bool:
    return any(t in body for t in TRIGGERS)


# --------------------------------------------------------------------------- strategies
def mask_scalar(value: Any, strategy: str) -> Any:
    if value is None:
        return None
    if strategy == "null":
        return None
    text = str(value)
    if strategy == "hash":
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:12]
    if strategy == "partial":
        if "@" in text:
            local, _, domain = text.partition("@")
            tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
            return f"{local[:1]}•••@•••{'.' + tld if tld else ''}"
        if len(text) <= 4:
            return "•" * len(text)
        return f"{text[:1]}{'•' * min(len(text) - 2, 8)}{text[-1:]}"
    return "••••"


def mask_sql(sql: str) -> str:
    """Replace string and numeric literals with ``?``, keeping the query's shape readable."""
    return _SQL_NUMBER.sub("?", _SQL_STRING.sub("?", sql))


# --------------------------------------------------------------------------- plans
@dataclass
class DatasetPlan:
    """What to mask for one dataset, for one caller."""

    fields: set[str] = field(default_factory=set)
    all_fields: bool = False
    strategy: str = "partial"
    mask_queries: bool = False
    policies: set[str] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return self.all_fields or bool(self.fields) or self.mask_queries

    def masks_field(self, field_path: Optional[str]) -> bool:
        if self.all_fields:
            return True
        if not field_path:
            return False
        if field_path in self.fields:
            return True
        # v2 field paths ("[version=2.0].[type=struct].customer.email") end with the simple path.
        simple = field_path.split("].")[-1] if "]." in field_path else field_path
        return simple in self.fields or any(f.endswith("." + simple) or simple.endswith("." + f) for f in self.fields)


NO_MASK = DatasetPlan()
_STRATEGY_STRENGTH = {"partial": 0, "hash": 1, "redact": 2, "null": 3}


def label_matches(pattern: str, labels: Iterable[str]) -> bool:
    """Purpose label semantics: urns match exactly or by '*' prefix; bare labels match classification
    paths and their descendants ("PII" matches "PII.Email"), case-insensitively."""
    p = pattern.lower()
    prefix = p.endswith("*")
    base = p.rstrip("*").rstrip(".") if prefix else p
    for label in labels:
        lab = label.lower()
        if base.startswith("urn:"):
            if lab == base or (prefix and lab.startswith(base)):
                return True
        else:
            leaf = lab.split("classification.", 1)[-1] if lab.startswith("urn:li:glossaryterm:") else lab
            if leaf == base or leaf.startswith(base + ".") or lab == base:
                return True
    return False


def build_plan(obligations: list[dict], resource: dict) -> DatasetPlan:
    """Turn ``mask_columns`` obligations + resource attributes into a :class:`DatasetPlan`."""
    plan = DatasetPlan()
    chosen: Optional[str] = None
    field_labels: dict[str, list[str]] = resource.get("field_labels") or {}
    sensitive: dict[str, list[str]] = resource.get("sensitive_fields") or {}
    dataset_labels = set(resource.get("tags") or []) | set(resource.get("glossary_terms") or [])
    for ob in obligations:
        if ob.get("type") != "mask_columns":
            continue
        params = ob.get("params") or {}
        patterns = params.get("labels") or params.get("classification") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        strategy = params.get("strategy", "partial")
        hit = False
        if any(label_matches(p, dataset_labels) for p in patterns):
            plan.all_fields = True
            hit = True
        for path in set(field_labels) | set(sensitive):
            labels = set(field_labels.get(path, [])) | set(sensitive.get(path, []))
            if any(label_matches(p, labels) for p in patterns):
                plan.fields.add(path)
                hit = True
        if hit:
            plan.mask_queries = True
            if params.get("purpose"):
                plan.policies.add(f"purpose:{params['purpose']}")
            # Several purposes may hit the same dataset: the strongest strategy wins.
            if chosen is None or _STRATEGY_STRENGTH.get(strategy, 0) > _STRATEGY_STRENGTH.get(chosen, 0):
                chosen = strategy
    if chosen:
        plan.strategy = chosen
    return plan


def fail_closed_plan(resource: Optional[dict]) -> DatasetPlan:
    """When the PDP is unreachable: mask every labelled column (or everything if unknown)."""
    if not resource:
        return DatasetPlan(all_fields=True, strategy="redact", mask_queries=True, policies={"fail-closed"})
    fields = set((resource.get("field_labels") or {}).keys()) | set((resource.get("sensitive_fields") or {}).keys())
    return DatasetPlan(fields=fields, all_fields=bool(resource.get("classification")) and not fields,
                       strategy="redact", mask_queries=bool(fields), policies={"fail-closed"})


# --------------------------------------------------------------------------- walker
@dataclass
class MaskResult:
    masked_values: int = 0
    policies: set[str] = field(default_factory=set)


Planner = Callable[[str], DatasetPlan]


def _find_dataset_urns(obj: Any, out: set[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, dict):
        u = obj.get("urn")
        if isinstance(u, str) and u.startswith(_DATASET):
            out.add(u)
        for v in obj.values():
            _find_dataset_urns(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _find_dataset_urns(v, out, depth + 1)


def _mask_profile(profile: dict, plan: DatasetPlan, result: MaskResult) -> None:
    if not plan.masks_field(profile.get("fieldPath")):
        return
    for key in PROFILE_VALUE_KEYS:
        if key not in profile or profile[key] is None:
            continue
        value = profile[key]
        if key == "quantiles" and isinstance(value, list):
            profile[key] = [{**q, "value": mask_scalar(q.get("value"), plan.strategy)} if isinstance(q, dict) else q
                            for q in value]
        elif key in ("distinctValueFrequencies", "histogram") and isinstance(value, list):
            profile[key] = [{**f, "value": mask_scalar(f.get("value"), plan.strategy)} if isinstance(f, dict) else f
                            for f in value]
        elif isinstance(value, list):
            profile[key] = [mask_scalar(v, plan.strategy) for v in value]
        else:
            profile[key] = mask_scalar(value, plan.strategy)
        result.masked_values += 1
    result.policies |= plan.policies


def mask_document(doc: Any, planner: Planner, context_urn: Optional[str] = None) -> MaskResult:
    """Mask ``doc`` in place. ``context_urn`` seeds the dataset context (e.g. from an OpenAPI URL)."""
    result = MaskResult()
    plans: dict[str, DatasetPlan] = {}

    def plan_for(urn: Optional[str]) -> DatasetPlan:
        if not urn:
            return NO_MASK
        if urn not in plans:
            plans[urn] = planner(urn)
        return plans[urn]

    def query_plan(obj: dict) -> DatasetPlan:
        urns: set[str] = set()
        _find_dataset_urns(obj, urns)
        merged = DatasetPlan()
        for u in urns:
            p = plan_for(u)
            if p.mask_queries:
                merged.mask_queries = True
                merged.policies |= p.policies
        return merged

    def walk(node: Any, ds_urn: Optional[str]) -> None:
        if isinstance(node, dict):
            u = node.get("urn")
            if isinstance(u, str):
                if u.startswith(_DATASET):
                    ds_urn = u
                elif u.startswith("urn:li:query:"):
                    qp = query_plan(node)
                    if qp.mask_queries:
                        _mask_statements(node, result, qp)
            for key, value in node.items():
                if key == "fieldProfiles" and isinstance(value, list):
                    plan = plan_for(ds_urn)
                    if plan.active:
                        for prof in value:
                            if isinstance(prof, dict):
                                _mask_profile(prof, plan, result)
                    continue
                if key in SQL_KEYS and isinstance(value, list):
                    plan = plan_for(ds_urn)
                    if plan.mask_queries:
                        node[key] = [mask_sql(q) if isinstance(q, str) else q for q in value]
                        result.masked_values += len(value)
                        result.policies |= plan.policies
                    continue
                if key == "statement" and isinstance(value, dict) and isinstance(value.get("value"), str):
                    plan = plan_for(ds_urn)
                    if plan.mask_queries:
                        value["value"] = mask_sql(value["value"])
                        result.masked_values += 1
                        result.policies |= plan.policies
                    continue
                walk(value, ds_urn)
        elif isinstance(node, list):
            for item in node:
                walk(item, ds_urn)

    walk(doc, context_urn)
    return result


def _mask_statements(node: Any, result: MaskResult, plan: DatasetPlan) -> None:
    if isinstance(node, dict):
        st = node.get("statement")
        if isinstance(st, dict) and isinstance(st.get("value"), str):
            st["value"] = mask_sql(st["value"])
            result.masked_values += 1
            result.policies |= plan.policies
        for k, v in node.items():
            if k != "statement":
                _mask_statements(v, result, plan)
    elif isinstance(node, list):
        for v in node:
            _mask_statements(v, result, plan)
