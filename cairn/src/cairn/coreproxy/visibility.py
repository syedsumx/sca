"""View enforcement for GraphQL responses on the UI path (authoritative mode).

Cairn Core enforces view denies on its REST API but returns entities through GraphQL regardless,
so the masking proxy applies them: every governed asset the caller is denied ``VIEW_ENTITY_PAGE``
on is reduced to its identity (``urn``, ``type``, ``__typename``); its name, description, schema,
ownership and every nested object are dropped. The asset still counts in search totals and appears
as an unnamed entry, the same trade-off the catalog makes for its own restricted entities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Asset types whose visibility policies govern. Vocabulary (tags, terms, platforms, users, groups)
# is never hidden: it is needed to read the rest of the catalog.
GOVERNED_TYPES = frozenset({
    "dataset", "chart", "dashboard", "dataJob", "dataFlow", "container", "dataProduct", "mlModel",
    "mlModelGroup", "mlFeatureTable", "mlFeature", "mlPrimaryKey", "notebook", "document", "query",
})
KEEP = ("urn", "type", "__typename")


@dataclass
class RestrictResult:
    restricted: set[str] = field(default_factory=set)


def entity_type(urn: str) -> str:
    return urn.split(":", 3)[2] if urn.startswith("urn:li:") and urn.count(":") >= 3 else ""


def restrict_document(doc: Any, denied: Callable[[str], bool]) -> RestrictResult:
    """Reduce, in place, every governed entity object in ``doc`` that ``denied(urn)`` is true for."""
    result = RestrictResult()
    verdicts: dict[str, bool] = {}

    def is_denied(urn: str) -> bool:
        if urn not in verdicts:
            verdicts[urn] = entity_type(urn) in GOVERNED_TYPES and denied(urn)
        return verdicts[urn]

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            urn = node.get("urn")
            if isinstance(urn, str) and is_denied(urn):
                for key in list(node):
                    if key not in KEEP:
                        node[key] = None
                result.restricted.add(urn)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return result
