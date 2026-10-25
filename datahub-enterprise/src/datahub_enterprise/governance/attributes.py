"""Subject and resource attribute resolution for the PDP.

Attributes are read from GMS aspects and cached with a TTL. MCL-driven invalidation
(:meth:`AttributeResolver.invalidate`) drops an entity's entry as soon as one of its
aspects changes, so authorization reacts to re-classification within seconds rather
than waiting out the TTL.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

from datahub_enterprise.common.gms import MetadataGraph

PII_TERM_PREFIX = "urn:li:glossaryTerm:Classification.PII"
CLASSIFICATION_TERM_PREFIX = "urn:li:glossaryTerm:Classification."
CERTIFICATION_PROPERTY = "urn:li:structuredProperty:io.dhe.certification.status"
_PROP_PREFIX = "urn:li:structuredProperty:"


def _sp_values(aspect: Optional[dict]) -> dict[str, list]:
    out: dict[str, list] = {}
    for prop in (aspect or {}).get("properties", []):
        name = prop.get("propertyUrn", "")
        name = name[len(_PROP_PREFIX):] if name.startswith(_PROP_PREFIX) else name
        vals = []
        for v in prop.get("values", []):
            if isinstance(v, dict):
                vals.append(v.get("string", v.get("double")))
            else:
                vals.append(v)
        out[name] = vals
    return out


def _classification_from_terms(term_urns: list[str]) -> list[str]:
    """``urn:li:glossaryTerm:Classification.PII.Email`` → ``["PII", "PII.Email"]``."""
    labels: set[str] = set()
    for t in term_urns:
        if t.startswith(CLASSIFICATION_TERM_PREFIX):
            path = t[len(CLASSIFICATION_TERM_PREFIX):].split(".")
            for i in range(1, len(path) + 1):
                labels.add(".".join(path[:i]))
    return sorted(labels)


def parse_dataset_urn(urn: str) -> dict[str, str]:
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([A-Z_]+)\)$", urn)
    return {"platform": m.group(1), "name": m.group(2), "env": m.group(3)} if m else {}


class AttributeResolver:
    def __init__(self, graph: Optional[MetadataGraph], ttl_seconds: int = 60):
        self.graph = graph
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict]] = {}
        self._versions: dict[str, int] = {}
        self._lock = threading.Lock()

    def invalidate(self, urn: str) -> None:
        with self._lock:
            self._cache.pop(urn, None)
            self._versions[urn] = self._versions.get(urn, 0) + 1

    def version(self, urn: Optional[str]) -> int:
        return self._versions.get(urn or "", 0)

    def _cached(self, urn: str, loader) -> dict:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(urn)
            if hit and hit[0] > now:
                return hit[1]
        value = loader(urn)
        with self._lock:
            self._cache[urn] = (now + self.ttl, value)
        return value

    def _aspect(self, urn: str, name: str) -> Optional[dict]:
        if self.graph is None:
            return None
        try:
            return self.graph.get_aspect(urn, name)
        except Exception:
            return None

    # ---- subject ------------------------------------------------------------------
    def subject(self, urn: str) -> dict:
        return self._cached(urn, self._load_subject)

    def _load_subject(self, urn: str) -> dict:
        info = self._aspect(urn, "corpUserInfo") or {}
        groups = set((self._aspect(urn, "groupMembership") or {}).get("groups", []))
        groups |= set((self._aspect(urn, "nativeGroupMembership") or {}).get("nativeGroups", []))
        roles = (self._aspect(urn, "roleMembership") or {}).get("roles", [])
        props = _sp_values(self._aspect(urn, "structuredProperties"))
        attributes: dict[str, Any] = {
            "department": info.get("departmentName"),
            "title": info.get("title"),
            "country": info.get("countryCode"),
            "email": info.get("email"),
            "display_name": info.get("displayName") or info.get("fullName"),
        }
        # Structured properties on users (e.g. io.dhe.clearance, io.dhe.employment_type)
        # are exposed by their last path segment: subject.attributes.clearance.
        for name, values in props.items():
            key = name.split(".")[-1]
            attributes[key] = values[0] if len(values) == 1 else values
        return {"urn": urn, "groups": sorted(groups), "roles": sorted(roles),
                "attributes": {k: v for k, v in attributes.items() if v is not None}}

    # ---- resource -----------------------------------------------------------------
    def resource(self, urn: str) -> dict:
        return self._cached(urn, self._load_resource)

    def _load_resource(self, urn: str) -> dict:
        entity_type = urn.split(":", 3)[2] if urn.startswith("urn:li:") else "unknown"
        ownership = self._aspect(urn, "ownership") or {}
        owners = [o.get("owner") for o in ownership.get("owners", []) if o.get("owner")]
        owner_types = sorted({o.get("typeUrn") or o.get("type") for o in ownership.get("owners", [])} - {None})
        tags = [t.get("tag") for t in (self._aspect(urn, "globalTags") or {}).get("tags", [])]
        terms = [t.get("urn") for t in (self._aspect(urn, "glossaryTerms") or {}).get("terms", [])]
        field_terms: list[str] = []
        sensitive_fields: dict[str, list[str]] = {}
        # Every label attached to a column: tag urns, term urns and classification labels.
        field_labels: dict[str, set[str]] = {}
        for aspect_name, list_key in (("editableSchemaMetadata", "editableSchemaFieldInfo"), ("schemaMetadata", "fields")):
            for f in (self._aspect(urn, aspect_name) or {}).get(list_key, []):
                path = f.get("fieldPath")
                ft = [t.get("urn") for t in (f.get("glossaryTerms") or {}).get("terms", []) if t.get("urn")]
                ftags = [t.get("tag") for t in (f.get("globalTags") or {}).get("tags", []) if t.get("tag")]
                field_terms.extend(ft)
                cls = _classification_from_terms(ft)
                if path and (ft or ftags):
                    field_labels.setdefault(path, set()).update(ft, ftags, cls)
                if cls:
                    sensitive_fields.setdefault(path, [])
                    sensitive_fields[path] = sorted(set(sensitive_fields[path]) | set(cls))
        domains = (self._aspect(urn, "domains") or {}).get("domains", [])
        container = (self._aspect(urn, "container") or {}).get("container")
        dpi = self._aspect(urn, "dataPlatformInstance") or {}
        sp_aspect = self._aspect(urn, "structuredProperties")
        props = _sp_values(sp_aspect)
        parsed = parse_dataset_urn(urn) if entity_type == "dataset" else {}
        certification = (props.get("io.dhe.certification.status") or [None])[0]
        data_products = [d.get("destinationUrn") for d in
                         (self._aspect(urn, "dataProducts") or {}).get("dataProducts", []) if d.get("destinationUrn")]
        return {
            "urn": urn,
            "type": entity_type,
            "owners": owners,
            "owner_types": owner_types,
            "tags": [t for t in tags if t],
            "glossary_terms": [t for t in terms if t],
            "domains": domains,
            "containers": [container] if container else [],
            "platform": parsed.get("platform") or (dpi.get("platform") or "").split(":")[-1] or None,
            "platform_instances": [dpi["instance"]] if dpi.get("instance") else [],
            "env": parsed.get("env"),
            "classification": _classification_from_terms(terms + field_terms),
            "sensitive_fields": sensitive_fields,
            "field_labels": {k: sorted(v) for k, v in field_labels.items()},
            "all_labels": sorted({t for t in tags if t} | {t for t in terms if t}
                                 | {x for v in field_labels.values() for x in v}),
            "data_products": data_products,
            "certification": certification,
            "structured_properties": props,
            "structured_property_urns": [p.get("propertyUrn") for p in (sp_aspect or {}).get("properties", [])],
        }
