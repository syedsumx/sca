"""Builders for Cairn Core MetadataChangeProposals (MCPs) as plain JSON.

Cairn deliberately does not depend on ``acryl-datahub`` in its control-plane services:
the generated ``schema_classes`` are large and pin a specific Cairn Core version. MCPs are
built as dicts that match the Rest.li ``ingestProposal`` wire format
(``aspect.value`` is a JSON *string*), which Cairn Core validates against the PDL anyway.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Iterable, Optional
from urllib.parse import quote

JSON_CONTENT_TYPE = "application/json"
CAIRN_ACTOR = "urn:li:corpuser:cairn-system"
CAIRN_AI_ACTOR = "urn:li:corpuser:cairn-ai"


def now_ms() -> int:
    return int(time.time() * 1000)


def audit_stamp(actor: str = CAIRN_ACTOR, time_ms: Optional[int] = None) -> dict:
    return {"time": time_ms if time_ms is not None else now_ms(), "actor": actor}


def entity_type_of(urn: str) -> str:
    # urn:li:<entityType>:<key>
    parts = urn.split(":", 3)
    if len(parts) < 4 or parts[0] != "urn" or parts[1] != "li":
        raise ValueError(f"Not a Cairn Core urn: {urn!r}")
    return parts[2]


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def assertion_urn(monitor_id: str) -> str:
    return f"urn:li:assertion:cairn-{stable_id('monitor', monitor_id)}"


def incident_urn(monitor_id: str, opened_at_ms: int) -> str:
    return f"urn:li:incident:cairn-{stable_id('incident', monitor_id, str(opened_at_ms))}"


def schema_field_urn(dataset_urn: str, field_path: str) -> str:
    return f"urn:li:schemaField:({dataset_urn},{field_path})"


def parse_schema_field_urn(urn: str) -> tuple[str, str]:
    """Split ``urn:li:schemaField:(<datasetUrn>,<fieldPath>)``.

    Dataset urns themselves contain commas, so split on the *last* comma that sits at
    paren depth 1.
    """
    prefix = "urn:li:schemaField:("
    if not urn.startswith(prefix) or not urn.endswith(")"):
        raise ValueError(f"Not a schemaField urn: {urn!r}")
    body = urn[len(prefix) : -1]
    depth = 0
    split_at = -1
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            split_at = i
    if split_at < 0:
        raise ValueError(f"Malformed schemaField urn: {urn!r}")
    return body[:split_at], body[split_at + 1 :]


def encode_urn(urn: str) -> str:
    return quote(urn, safe="")


def make_mcp(
    entity_urn: str,
    aspect_name: str,
    aspect: dict,
    change_type: str = "UPSERT",
    system_properties: Optional[dict[str, str]] = None,
) -> dict:
    mcp: dict[str, Any] = {
        "entityType": entity_type_of(entity_urn),
        "entityUrn": entity_urn,
        "changeType": change_type,
        "aspectName": aspect_name,
        "aspect": {
            "contentType": JSON_CONTENT_TYPE,
            "value": json.dumps(aspect, sort_keys=True, separators=(",", ":")),
        },
    }
    if system_properties:
        mcp["systemMetadata"] = {
            "lastObserved": now_ms(),
            "properties": dict(system_properties),
        }
    return mcp


JSON_PATCH_CONTENT_TYPE = "application/json-patch+json"


def patch_mcp(entity_urn: str, aspect_name: str, ops: list[dict],
              system_properties: Optional[dict[str, str]] = None) -> dict:
    """A ``PATCH`` proposal: Cairn Core applies ``ops`` to the stored aspect atomically, so concurrent writers
    (e.g. two propagation actions labelling the same column) cannot overwrite each other."""
    mcp = make_mcp(entity_urn, aspect_name, {}, "PATCH", system_properties)
    mcp["aspect"] = {"contentType": JSON_PATCH_CONTENT_TYPE, "value": json.dumps(ops, separators=(",", ":"))}
    return mcp


def pointer(*segments: str) -> str:
    """RFC 6901 JSON pointer from raw segments (``/`` in a field path becomes ``~1``)."""
    return "".join("/" + seg.replace("~", "~0").replace("/", "~1") for seg in segments)


def field_label_op(op: str, field_path: str, label: str, association: Optional[dict] = None) -> dict:
    """Add/remove one tag or glossary term on an ``editableSchemaMetadata`` field (Cairn Core keyed-array patch)."""
    container, key = (("globalTags", "tags") if label.startswith("urn:li:tag:") else ("glossaryTerms", "terms"))
    out: dict[str, Any] = {"op": op, "path": pointer("editableSchemaFieldInfo", field_path, container, key, label)}
    if op == "add":
        out["value"] = association
    return out


def aspect_value(mcp: dict) -> dict:
    """Inverse of :func:`make_mcp` for the aspect payload."""
    aspect = mcp.get("aspect") or {}
    if "json" in aspect:
        return aspect["json"]
    value = aspect.get("value")
    return json.loads(value) if isinstance(value, str) else (value or {})


def glossary_term_association(
    term_urn: str,
    actor: str = CAIRN_ACTOR,
    source: Optional[str] = None,
    source_detail: Optional[dict[str, str]] = None,
) -> dict:
    assoc: dict[str, Any] = {"urn": term_urn}
    if source is not None:
        assoc["attribution"] = {
            "time": now_ms(),
            "actor": actor,
            "source": source,
            "sourceDetail": dict(source_detail or {}),
        }
    return assoc


def cairn_managed_owner(entry: dict) -> bool:
    """An ``ownership`` entry of the kind Cairn writes: type ``DATAOWNER`` with no source, or a manual one. Owners of
    other types (technical, business, custom ``typeUrn``) or from other sources (ingestion, a service) are never
    changed by Cairn."""
    if entry.get("type") != "DATAOWNER":
        return False
    tu = entry.get("typeUrn")
    if tu and "data_owner" not in tu.lower() and "dataowner" not in tu.lower():
        return False
    source = entry.get("source") or {}
    return not source or source.get("type") == "MANUAL"


def merge_ownership(before: Optional[dict], add: Iterable[str], remove: Iterable[str], stamp: dict) -> dict:
    """``before`` (an ``ownership`` aspect) with the people in ``add`` made owners (as ``DATAOWNER`` when they are not
    owners already, in any way) and the Cairn-managed entries (:func:`cairn_managed_owner`) of the people in
    ``remove`` taken out. Every other entry, and every other field of the aspect, is kept as it was."""
    before = dict(before or {})
    drop = set(remove) - set(add)
    owners = [o for o in before.get("owners") or []
              if isinstance(o, dict) and not (o.get("owner") in drop and cairn_managed_owner(o))]
    have = {o.get("owner") for o in owners}
    for u in add:
        if u not in have:
            owners.append({"owner": u, "type": "DATAOWNER"})
            have.add(u)
    return {**before, "owners": owners, "lastModified": stamp}


def iter_batches(items: Iterable[dict], max_items: int = 500, max_bytes: int = 1 << 20):
    """Chunk MCPs so neither item count nor serialized size exceeds the limits."""
    batch: list[dict] = []
    size = 0
    for item in items:
        item_size = len(json.dumps(item))
        if batch and (len(batch) >= max_items or size + item_size > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch
