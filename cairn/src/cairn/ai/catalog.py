"""Read-side catalog access for the AI layer (GraphQL) and entity → text rendering."""

from __future__ import annotations

from typing import Any, Optional

from cairn.common.core import MetadataGraph

ENTITY_TYPES = {
    "dataset": "DATASET", "dashboard": "DASHBOARD", "chart": "CHART", "dataJob": "DATA_JOB",
    "dataFlow": "DATA_FLOW", "glossaryTerm": "GLOSSARY_TERM", "domain": "DOMAIN", "dataProduct": "DATA_PRODUCT",
    "mlModel": "MLMODEL", "container": "CONTAINER",
}
FILTER_FIELDS = {"platform", "domains", "tags", "glossaryTerms", "owners", "origin", "typeNames", "container"}

_SUMMARY_FRAGMENT = """
  urn
  type
  ... on Dataset { name platform { name } properties { name description } editableProperties { description } }
  ... on Dashboard { platform { name } properties { name description } }
  ... on Chart { platform { name } properties { name description } }
  ... on DataJob { properties { name description } }
  ... on DataFlow { properties { name description } }
  ... on GlossaryTerm { properties { name description } }
  ... on Domain { properties { name description } }
  ... on DataProduct { properties { name description } }
"""

SEARCH_QUERY = f"""
query cairnSearch($input: SearchAcrossEntitiesInput!) {{
  searchAcrossEntities(input: $input) {{
    total
    searchResults {{ entity {{ {_SUMMARY_FRAGMENT} }} }}
  }}
}}
"""

LINEAGE_QUERY = f"""
query cairnLineage($input: SearchAcrossLineageInput!) {{
  searchAcrossLineage(input: $input) {{
    total
    searchResults {{ degree entity {{ {_SUMMARY_FRAGMENT} }} }}
  }}
}}
"""

DETAILS_QUERY = """
query cairnEntity($urn: String!) {
  entity(urn: $urn) {
    urn
    type
    ... on Dataset {
      name
      platform { name }
      properties { name description customProperties { key value } }
      editableProperties { description }
      schemaMetadata(version: 0) { fields { fieldPath nativeDataType description } }
      editableSchemaMetadata { editableSchemaFieldInfo { fieldPath description } }
      ownership { owners { owner { ... on CorpUser { urn username } ... on CorpGroup { urn name } } } }
      domain { domain { urn properties { name } } }
      tags { tags { tag { urn name } } }
      glossaryTerms { terms { term { urn name } } }
    }
  }
}
"""

ENTITIES_QUERY = f"""
query cairnEntities($urns: [String!]!) {{
  entities(urns: $urns) {{ {_SUMMARY_FRAGMENT}
    ... on Dataset {{ domain {{ domain {{ urn properties {{ name }} }} }} }}
    ... on DataProduct {{ domain {{ domain {{ urn properties {{ name }} }} }} }}
  }}
}}
"""

FACETS_QUERY = """
query cairnFacets($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    facets { field aggregations { value count } }
  }
}
"""


def _props_name(e: dict) -> str:
    return (e.get("properties") or {}).get("name") or e.get("name") or e.get("urn", "")


def summarize(e: dict) -> dict:
    desc = ((e.get("editableProperties") or {}).get("description")
            or (e.get("properties") or {}).get("description") or "")
    return {"urn": e.get("urn"), "type": e.get("type"), "name": _props_name(e),
            "platform": (e.get("platform") or {}).get("name"), "description": desc[:500]}


class Catalog:
    def __init__(self, graph: MetadataGraph):
        self.graph = graph

    def search(self, query: str, entity_types: Optional[list[str]] = None,
               filters: Optional[list[dict]] = None, count: int = 10) -> list[dict]:
        inp: dict[str, Any] = {"query": query or "*", "start": 0, "count": count}
        types = [ENTITY_TYPES.get(t, t.upper()) for t in (entity_types or [])]
        if types:
            inp["types"] = types
        clauses = [{"field": f["field"], "values": f["values"], "condition": f.get("condition", "EQUAL"),
                    "negated": bool(f.get("negated", False))}
                   for f in (filters or []) if f.get("field") in FILTER_FIELDS and f.get("values")]
        if clauses:
            inp["orFilters"] = [{"and": clauses}]
        data = self.graph.graphql(SEARCH_QUERY, {"input": inp})
        results = ((data.get("searchAcrossEntities") or {}).get("searchResults")) or []
        return [summarize(r["entity"]) for r in results if r.get("entity")]

    def summaries(self, urns: list[str]) -> dict[str, dict]:
        """Batched name/type/platform/description lookup (one GraphQL round-trip per 100 urns)."""
        out: dict[str, dict] = {}
        for i in range(0, len(urns), 100):
            chunk = urns[i:i + 100]
            data = self.graph.graphql(ENTITIES_QUERY, {"urns": chunk})
            for e in data.get("entities") or []:
                if e and e.get("urn"):
                    s = summarize(e)
                    dom = ((e.get("domain") or {}).get("domain") or {})
                    s["domain"] = {"urn": dom["urn"], "name": (dom.get("properties") or {}).get("name")} if dom else None
                    out[e["urn"]] = s
        return out

    def lineage(self, urn: str, direction: str = "DOWNSTREAM", max_hops: int = 3, count: int = 50) -> list[dict]:
        degrees = ["1", "2", "3+"][: max(1, min(max_hops, 3))]
        data = self.graph.graphql(LINEAGE_QUERY, {"input": {
            "urn": urn, "direction": direction.upper(), "query": "*", "start": 0, "count": count,
            "orFilters": [{"and": [{"field": "degree", "values": degrees}]}]}})
        out = []
        for r in ((data.get("searchAcrossLineage") or {}).get("searchResults")) or []:
            item = summarize(r["entity"])
            item["degree"] = r.get("degree")
            out.append(item)
        return out

    def details(self, urn: str) -> Optional[dict]:
        return (self.graph.graphql(DETAILS_QUERY, {"urn": urn}) or {}).get("entity")

    def facet_vocabulary(self, max_values: int = 50) -> dict[str, list[str]]:
        try:
            data = self.graph.graphql(FACETS_QUERY, {"input": {"query": "*", "start": 0, "count": 0}})
        except Exception:
            return {}
        vocab: dict[str, list[str]] = {}
        for facet in ((data.get("searchAcrossEntities") or {}).get("facets")) or []:
            if facet.get("field") in FILTER_FIELDS:
                aggs = sorted(facet.get("aggregations") or [], key=lambda a: -a.get("count", 0))
                vocab[facet["field"]] = [a["value"] for a in aggs[:max_values]]
        return vocab


def entity_document(details: dict) -> str:
    """Render an entity into the text that gets embedded and fed to LLM prompts."""
    lines = [f"{details.get('type', '').lower()} {_props_name(details)}"]
    if details.get("platform"):
        lines.append(f"platform: {details['platform'].get('name')}")
    desc = ((details.get("editableProperties") or {}).get("description")
            or (details.get("properties") or {}).get("description"))
    if desc:
        lines.append(f"description: {desc}")
    dom = ((details.get("domain") or {}).get("domain") or {}).get("properties") or {}
    if dom.get("name"):
        lines.append(f"domain: {dom['name']}")
    tags = [t["tag"].get("name") for t in ((details.get("tags") or {}).get("tags") or []) if t.get("tag")]
    if tags:
        lines.append("tags: " + ", ".join(filter(None, tags)))
    terms = [t["term"].get("name") for t in ((details.get("glossaryTerms") or {}).get("terms") or []) if t.get("term")]
    if terms:
        lines.append("glossary terms: " + ", ".join(filter(None, terms)))
    owners = []
    for o in ((details.get("ownership") or {}).get("owners") or []):
        owner = o.get("owner") or {}
        owners.append(owner.get("username") or owner.get("name") or owner.get("urn"))
    if owners:
        lines.append("owners: " + ", ".join(filter(None, owners)))
    edit_desc = {f["fieldPath"]: f.get("description") for f in
                 ((details.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or [])}
    fields = ((details.get("schemaMetadata") or {}).get("fields")) or []
    if fields:
        lines.append("columns:")
        for f in fields[:300]:
            d = edit_desc.get(f["fieldPath"]) or f.get("description") or ""
            lines.append(f"- {f['fieldPath']} ({f.get('nativeDataType', '')}){': ' + d if d else ''}")
    return "\n".join(lines)
