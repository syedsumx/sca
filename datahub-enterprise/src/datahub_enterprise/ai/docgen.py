"""Grounded documentation generation, routed through the approval workflow.

Drafts are never written directly to DataHub: :meth:`DocGenerator.propose` creates a
``DOCUMENTATION`` workflow whose payload holds the draft; :func:`apply_documentation`
writes it with attribution only after a reviewer approves.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from datahub_enterprise.ai.catalog import Catalog, entity_document
from datahub_enterprise.ai.guardrails import UNTRUSTED_NOTICE, redact, untrusted
from datahub_enterprise.ai.llm import LLMError, LLMProvider, validate_against_schema
from datahub_enterprise.common.gms import MetadataGraph
from datahub_enterprise.common.proposals import DHE_AI_ACTOR, audit_stamp, make_mcp

DRAFT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "description", "columns", "suggested_terms", "confidence", "unknowns"],
    "properties": {
        "summary": {"type": "string", "description": "one sentence"},
        "description": {"type": "string", "description": "markdown, 2-6 short paragraphs"},
        "columns": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["field_path", "description", "suggested_terms"],
            "properties": {
                "field_path": {"type": "string"},
                "description": {"type": "string"},
                "suggested_terms": {"type": "array", "items": {"type": "string"}},
            }}},
        "suggested_terms": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "description": "0..1 overall confidence"},
        "unknowns": {"type": "array", "items": {"type": "string"},
                     "description": "facts that could not be determined from the context"},
    },
}

DOC_SYSTEM = f"""You are a data steward writing catalog documentation for a single data asset.
Use only the supplied context (schema, lineage neighbours, existing descriptions, glossary terms).
- Describe what the asset contains, its grain, and how it is produced/used when the context shows it.
- Column descriptions: one precise sentence each; only for columns in the schema; keep field_path exact.
- Do not guess business rules. Put anything you cannot determine into `unknowns` and lower `confidence`.
- suggested_terms must be glossary term urns that appear in the context, or empty.
{UNTRUSTED_NOTICE}"""


class DocGenerator:
    def __init__(self, catalog: Catalog, llm: Optional[LLMProvider], model: Optional[str] = None,
                 on_llm_call: Optional[Callable[[str, Any], None]] = None):
        self.catalog = catalog
        self.llm = llm
        self.model = model
        self.on_llm_call = on_llm_call

    def build_context(self, urn: str) -> tuple[dict, str]:
        details = self.catalog.details(urn)
        if not details:
            raise LookupError(f"entity {urn} not found")
        parts = ["## Asset", entity_document(details)]
        try:
            ups = self.catalog.lineage(urn, "UPSTREAM", 1, 10)
            downs = self.catalog.lineage(urn, "DOWNSTREAM", 1, 10)
        except Exception:
            ups, downs = [], []
        if ups:
            parts += ["## Direct upstreams"] + [f"- {u['name']} ({u['urn']}): {u.get('description', '')[:300]}"
                                                for u in ups]
        if downs:
            parts += ["## Direct downstreams"] + [f"- {d['name']} ({d['urn']}): {d.get('description', '')[:200]}"
                                                  for d in downs]
        return details, redact("\n".join(parts))

    def draft(self, urn: str) -> dict:
        if self.llm is None:
            raise LLMError("no LLM provider configured (set DHE_LLM_PROVIDER=anthropic or ollama)")
        details, context = self.build_context(urn)
        draft, usage = self.llm.generate_json(DOC_SYSTEM, untrusted(context), DRAFT_SCHEMA, model=self.model)
        validate_against_schema(draft, DRAFT_SCHEMA)
        if self.on_llm_call:
            self.on_llm_call("docgen.draft", usage)
        known = {f["fieldPath"] for f in ((details.get("schemaMetadata") or {}).get("fields") or [])}
        draft["columns"] = [c for c in draft["columns"] if not known or c["field_path"] in known]
        draft["confidence"] = max(0.0, min(1.0, float(draft["confidence"])))
        draft["model"] = usage.model
        return draft


def apply_documentation(graph: MetadataGraph, entity_urn: str, draft: dict, approved_by: str) -> list[dict]:
    """Write an approved draft with attribution; existing human-written column docs win."""
    stamp = audit_stamp(DHE_AI_ACTOR)
    edp = graph.get_aspect(entity_urn, "editableDatasetProperties") or {}
    edp["description"] = draft["description"]
    edp["lastModified"] = stamp
    edp.setdefault("created", stamp)
    esm = graph.get_aspect(entity_urn, "editableSchemaMetadata") or {}
    infos = {i["fieldPath"]: i for i in esm.get("editableSchemaFieldInfo", [])}
    for col in draft.get("columns", []):
        info = infos.setdefault(col["field_path"], {"fieldPath": col["field_path"]})
        if not info.get("description"):
            info["description"] = col["description"]
    esm["editableSchemaFieldInfo"] = list(infos.values())
    esm["lastModified"] = stamp
    esm.setdefault("created", stamp)
    props = {"dhe_source": "ai-docgen", "dhe_approved_by": approved_by, "dhe_model": str(draft.get("model", ""))}
    mcps = [make_mcp(entity_urn, "editableDatasetProperties", edp, system_properties=props),
            make_mcp(entity_urn, "editableSchemaMetadata", esm, system_properties=props)]
    graph.emit(mcps)
    return mcps
