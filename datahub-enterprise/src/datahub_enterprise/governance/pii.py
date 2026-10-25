"""PII classification and lineage-based propagation.

Classification combines column-name heuristics with validated value patterns (Luhn
for card numbers, ISO-7064 mod-97 for IBANs). Propagation walks column-level lineage
(``upstreamLineage.fineGrainedLineages``) downstream and attaches the same
classification term with an attribution pointing at the origin column, so every
propagated label is explainable and reversible.
"""

from __future__ import annotations

import ipaddress
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from datahub_enterprise.common.gms import MetadataGraph
from datahub_enterprise.common.proposals import (
    DHE_ACTOR,
    audit_stamp,
    glossary_term_association,
    make_mcp,
    parse_schema_field_urn,
    schema_field_urn,
)

PROPAGATION_SOURCE = "urn:li:dataHubAction:dhe-pii-propagation"
CLASSIFIER_SOURCE = "urn:li:dataHubAction:dhe-pii-classifier"
TERM_PREFIX = "urn:li:glossaryTerm:Classification.PII."


def pii_term(kind: str) -> str:
    return f"{TERM_PREFIX}{kind}"


# --------------------------------------------------------------------------- classification
def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    s = value.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in rearranged)
    return int(num) % 97 == 1


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


_VALUE_DETECTORS: dict[str, Callable[[str], bool]] = {
    "Email": lambda v: bool(re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", v)),
    "Phone": lambda v: bool(re.fullmatch(r"\+?[\d\s().\-]{7,20}", v)) and 7 <= sum(c.isdigit() for c in v) <= 15,
    "SSN": lambda v: bool(re.fullmatch(r"(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}", v)),
    "CreditCard": lambda v: (lambda d: 13 <= len(d) <= 19 and _luhn_ok(d))(re.sub(r"[ \-]", "", v))
    if re.fullmatch(r"[\d \-]{13,23}", v) else False,
    "IBAN": _iban_ok,
    "IPAddress": _is_ip,
}

_NAME_PATTERNS: list[tuple[str, str, float]] = [
    (r"(^|_)(e_?mail|email_address)($|_)", "Email", 0.8),
    (r"(^|_)(phone|mobile|msisdn|tel|telephone)(_|$)", "Phone", 0.75),
    (r"(^|_)(ssn|social_security|national_id|nino|aadhaar)(_|$)", "SSN", 0.9),
    (r"(^|_)(cc|card|credit_card|pan)(_?num(ber)?)?($|_)", "CreditCard", 0.7),
    (r"(^|_)iban($|_)", "IBAN", 0.85),
    (r"(^|_)(ip|ip_addr(ess)?|client_ip)($|_)", "IPAddress", 0.6),
    (r"(^|_)(dob|date_of_birth|birth_?date)($|_)", "DateOfBirth", 0.85),
    (r"(^|_)(first_?name|last_?name|full_?name|surname|given_name)($|_)", "Name", 0.75),
    (r"(^|_)(street|address(_line)?\d?|postal_?code|zip(_?code)?)($|_)", "Address", 0.7),
    (r"(^|_)(passport(_no|_number)?)($|_)", "Passport", 0.9),
]


@dataclass
class FieldClassification:
    field_path: str
    kind: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    action: str = "none"  # apply | suggest | none

    def as_dict(self) -> dict:
        return {"field_path": self.field_path, "kind": self.kind, "confidence": round(self.confidence, 3),
                "evidence": self.evidence, "action": self.action, "term": pii_term(self.kind)}


def _normalize_name(path: str) -> str:
    leaf = path.split(".")[-1]
    leaf = re.sub(r"\[.*?\]", "", leaf)  # strip v2 field-path annotations like [type=string]
    leaf = re.sub(r"([a-z])([A-Z])", r"\1_\2", leaf)
    return leaf.lower()


def classify_field(
    field_path: str,
    samples: Sequence[str] = (),
    auto_apply_threshold: float = 0.9,
    suggest_threshold: float = 0.6,
) -> list[FieldClassification]:
    name = _normalize_name(field_path)
    scores: dict[str, tuple[float, list[str]]] = {}
    for pattern, kind, weight in _NAME_PATTERNS:
        if re.search(pattern, name):
            scores[kind] = (weight, [f"column name matches {kind}"])
    values = [str(s).strip() for s in samples if s is not None and str(s).strip()]
    if values:
        for kind, check in _VALUE_DETECTORS.items():
            hits = sum(1 for v in values if check(v))
            frac = hits / len(values)
            if frac >= 0.3:
                value_score = 0.3 + 0.7 * frac
                prev, ev = scores.get(kind, (0.0, []))
                combined = 1 - (1 - prev) * (1 - value_score)
                scores[kind] = (combined, ev + [f"{hits}/{len(values)} sampled values match {kind}"])
    out = []
    for kind, (score, evidence) in sorted(scores.items(), key=lambda kv: -kv[1][0]):
        action = "apply" if score >= auto_apply_threshold else "suggest" if score >= suggest_threshold else "none"
        out.append(FieldClassification(field_path, kind, score, evidence, action))
    return out


def classify_schema(fields: Iterable[dict], samples: Optional[dict[str, Sequence[str]]] = None,
                    auto_apply_threshold: float = 0.9, suggest_threshold: float = 0.6) -> list[FieldClassification]:
    samples = samples or {}
    results: list[FieldClassification] = []
    for f in fields:
        path = f["fieldPath"]
        top = classify_field(path, samples.get(path, ()), auto_apply_threshold, suggest_threshold)
        if top and top[0].action != "none":
            results.append(top[0])
    return results


# --------------------------------------------------------------------------- lineage + propagation
@dataclass(frozen=True)
class FieldEdge:
    downstream_field: str
    transform: Optional[str]


LineageFn = Callable[[str], list[FieldEdge]]


@dataclass
class PropagationStep:
    target_field: str
    term: str
    origin_field: str
    hops: int
    path: list[str]

    def as_dict(self) -> dict:
        return {"target_field": self.target_field, "term": self.term, "origin_field": self.origin_field,
                "hops": self.hops, "path": self.path}


def plan_propagation(
    origins: Sequence[tuple[str, str]],
    downstream_fields: LineageFn,
    max_hops: int = 5,
    blocking_transforms: Sequence[str] = ("HASH", "AGGREGATE", "COUNT", "ENCRYPT", "TOKENIZE", "MASK"),
    max_targets: int = 10_000,
) -> list[PropagationStep]:
    """BFS from ``(field_urn, term_urn)`` origins; stops at blocking transforms and cycles."""
    blocking = {b.upper() for b in blocking_transforms}
    plan: list[PropagationStep] = []
    seen: set[tuple[str, str]] = set(origins)
    for origin_field, term in origins:
        queue = deque([(origin_field, 0, [origin_field])])
        while queue:
            current, hops, path = queue.popleft()
            if hops >= max_hops:
                continue
            for edge in downstream_fields(current):
                transform = (edge.transform or "").upper()
                if any(b in transform for b in blocking):
                    continue
                key = (edge.downstream_field, term)
                if key in seen:
                    continue
                seen.add(key)
                new_path = path + [edge.downstream_field]
                plan.append(PropagationStep(edge.downstream_field, term, origin_field, hops + 1, new_path))
                if len(plan) >= max_targets:
                    return plan
                queue.append((edge.downstream_field, hops + 1, new_path))
    return plan


_DOWNSTREAMS_QUERY = """
query dheDownstreams($urn: String!) {
  entity(urn: $urn) {
    ... on Dataset {
      relationships(input: {types: ["DownstreamOf"], direction: INCOMING, start: 0, count: 1000}) {
        relationships { entity { urn } }
      }
    }
  }
}
"""


class GraphLineage:
    """Column-level lineage from GMS: downstream datasets via graph, edges via upstreamLineage."""

    def __init__(self, graph: MetadataGraph):
        self.graph = graph
        self._downstream_cache: dict[str, list[str]] = {}

    def downstream_datasets(self, dataset_urn: str) -> list[str]:
        if dataset_urn not in self._downstream_cache:
            data = self.graph.graphql(_DOWNSTREAMS_QUERY, {"urn": dataset_urn})
            rels = (((data.get("entity") or {}).get("relationships") or {}).get("relationships") or [])
            self._downstream_cache[dataset_urn] = [r["entity"]["urn"] for r in rels if r.get("entity")]
        return self._downstream_cache[dataset_urn]

    def __call__(self, field_urn: str) -> list[FieldEdge]:
        dataset_urn, _ = parse_schema_field_urn(field_urn)
        edges: list[FieldEdge] = []
        for ds in self.downstream_datasets(dataset_urn):
            lineage = self.graph.get_aspect(ds, "upstreamLineage") or {}
            for fgl in lineage.get("fineGrainedLineages", []):
                if field_urn in (fgl.get("upstreams") or []):
                    for down in fgl.get("downstreams") or []:
                        edges.append(FieldEdge(down, fgl.get("transformOperation")))
        return edges


def _field_info(esm: dict, field_path: str) -> dict:
    infos = esm.setdefault("editableSchemaFieldInfo", [])
    for info in infos:
        if info.get("fieldPath") == field_path:
            return info
    info = {"fieldPath": field_path}
    infos.append(info)
    return info


def apply_terms_mcps(
    graph: MetadataGraph,
    assignments: Sequence[tuple[str, str, str, dict[str, str]]],
    actor: str = DHE_ACTOR,
) -> list[dict]:
    """Build ``editableSchemaMetadata`` MCPs for ``(field_urn, term, source, sourceDetail)``.

    Read-modify-write per dataset; existing terms (manual or propagated) are preserved.
    """
    by_dataset: dict[str, list[tuple[str, str, str, dict]]] = {}
    for field_urn, term, source, detail in assignments:
        ds, path = parse_schema_field_urn(field_urn)
        by_dataset.setdefault(ds, []).append((path, term, source, detail))
    mcps = []
    for ds, items in by_dataset.items():
        esm = graph.get_aspect(ds, "editableSchemaMetadata") or {}
        changed = False
        for path, term, source, detail in items:
            info = _field_info(esm, path)
            terms = info.setdefault("glossaryTerms", {"terms": [], "auditStamp": audit_stamp(actor)})
            if any(t.get("urn") == term for t in terms.get("terms", [])):
                continue
            terms.setdefault("terms", []).append(glossary_term_association(term, actor, source, detail))
            terms["auditStamp"] = audit_stamp(actor)
            changed = True
        if changed:
            esm.setdefault("created", audit_stamp(actor))
            esm["lastModified"] = audit_stamp(actor)
            mcps.append(make_mcp(ds, "editableSchemaMetadata", esm, system_properties={"dhe_propagated": "true"}))
    return mcps


def revert_propagation_mcps(graph: MetadataGraph, dataset_urns: Iterable[str], origin_field: str, term: str) -> list[dict]:
    """Remove every propagated copy of ``term`` whose attribution points at ``origin_field``."""
    mcps = []
    for ds in dataset_urns:
        esm = graph.get_aspect(ds, "editableSchemaMetadata")
        if not esm:
            continue
        changed = False
        for info in esm.get("editableSchemaFieldInfo", []):
            terms = (info.get("glossaryTerms") or {}).get("terms")
            if not terms:
                continue
            keep = [t for t in terms if not (
                t.get("urn") == term
                and (t.get("attribution") or {}).get("source") == PROPAGATION_SOURCE
                and (t.get("attribution") or {}).get("sourceDetail", {}).get("origin") == origin_field)]
            if len(keep) != len(terms):
                info["glossaryTerms"]["terms"] = keep
                changed = True
        if changed:
            esm["lastModified"] = audit_stamp(DHE_ACTOR)
            mcps.append(make_mcp(ds, "editableSchemaMetadata", esm, system_properties={"dhe_propagated": "true"}))
    return mcps


def pii_origins_from_aspect(dataset_urn: str, aspect: dict, list_key: str) -> list[tuple[str, str]]:
    """Extract (field_urn, PII term) pairs that are *origins* (not themselves propagated)."""
    origins = []
    for f in aspect.get(list_key, []):
        for t in (f.get("glossaryTerms") or {}).get("terms", []):
            urn = t.get("urn", "")
            if urn.startswith(TERM_PREFIX) and (t.get("attribution") or {}).get("source") != PROPAGATION_SOURCE:
                origins.append((schema_field_urn(dataset_urn, f["fieldPath"]), urn))
    return origins
