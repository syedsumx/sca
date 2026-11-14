"""AI enrichment of files pipes: images described and labelled, and named fields extracted from documents.

A files pipe with ``"ai": {...}`` adds columns to its directory table, filled as each file lands:

* ``"describe": true``: images (PNG, JPEG, GIF, WebP, TIFF, BMP) get ``description`` (a sentence or two) and
  ``labels`` (up to 10 short labels), from Cairn's model (``CAIRN_AI_*``; Anthropic or a local Ollama model);
* ``"extract": {"invoice_number": "the invoice's number", "total": "the total amount due, as a number"}``: each
  document (any file with text: PDF, Word, scans read by OCR, transcribed recordings) and each image gets the
  fields in ``extracted``, a JSON object (``json_extract_scalar(extracted, '$.total')`` in SQL). A field the
  file does not have is null.

Calls go through Cairn's metered model: they count against the AI budget (``CAIRN_AI_DAILY_BUDGET_USD``) as the
pipe's owner, feature ``pipe_ai``. A file whose call fails (no model, budget spent, refused, too large) gets the
reason in ``ai_error``; the pipe goes on. Images are scaled to at most ``CAIRN_PIPES_AI_IMAGE_PIXELS`` (1568 on
the long side) before they are sent, and text is cut to ``CAIRN_PIPES_AI_MAX_TEXT`` characters (100,000).
"""

from __future__ import annotations

import io
import json
import os
import re
from typing import Any, Optional

FIELD = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MAX_FIELDS = 30
SENDABLE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp", ".tif": "image/png", ".tiff": "image/png", ".bmp": "image/png"}
SYSTEM = ("You describe and read files for a data catalog. Answer only from what the file shows or says; when a "
          "field is not there, give null. Never guess personal data that is not shown.")


class EnrichError(Exception):
    pass


def check(spec: Any) -> dict:
    """A pipe's ``ai``: {"describe": bool, "extract": {field: what it is}}; {} for none."""
    if spec in (None, {}):
        return {}
    if not isinstance(spec, dict) or set(spec) - {"describe", "extract", "parse"}:
        raise EnrichError('ai: {"describe": true, "extract": {"field": "what it is"}, "parse": true}')
    out: dict[str, Any] = {}
    if spec.get("describe"):
        out["describe"] = True
    if spec.get("parse"):                  # tables and charts as JSON (cairn.pipelines.parse), column ``parsed``
        out["parse"] = True
    fields = spec.get("extract") or {}
    if not isinstance(fields, dict) or len(fields) > MAX_FIELDS or not all(
            isinstance(k, str) and FIELD.match(k) and isinstance(v, str) and 0 < len(v) <= 500 for k, v in fields.items()):
        raise EnrichError(f"ai.extract: up to {MAX_FIELDS} fields, lowercase names, each with what it is (500 characters)")
    if fields:
        out["extract"] = dict(sorted(fields.items()))
    return out


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def image_for_model(name: str, data: bytes) -> Optional[tuple[str, bytes]]:
    """The image as the model takes it: its first frame, scaled to fit, as PNG or JPEG; None for other files."""
    ext = os.path.splitext(name.lower())[1]
    if ext not in SENDABLE:
        return None
    from PIL import Image

    from cairn.pipelines import ocr

    edge = _int("CAIRN_PIPES_AI_IMAGE_PIXELS", 1568)
    with Image.open(io.BytesIO(data)) as img:                 # reads the header only
        ocr._pixels_ok(*img.size)                             # the same size limit as OCR, before decoding
        img.seek(0)
        frame = img.convert("RGB")
    frame.thumbnail((edge, edge))
    out = io.BytesIO()
    frame.save(out, format="JPEG", quality=85)
    return "image/jpeg", out.getvalue()


def enrich(llm, spec: dict, name: str, data: bytes, text: Optional[str]) -> tuple[dict, Optional[str]]:
    """``({"description", "labels", "extracted"}, error)`` for one file."""
    from cairn.ai.usage import llm_task

    out: dict[str, Any] = {"description": None, "labels": None, "extracted": None}
    if not spec:
        return out, None
    if llm is None:
        return out, "AI enrichment needs Cairn's model (CAIRN_AI_*): none is set up"
    try:
        image = image_for_model(name, data)
    except Exception as e:  # noqa: BLE001 - recorded on the row
        return out, f"the image cannot be sent: {type(e).__name__}: {str(e)[:200]}"
    describe = bool(spec.get("describe")) and image is not None
    fields = spec.get("extract") or {}
    body = (text or "")[:_int("CAIRN_PIPES_AI_MAX_TEXT", 100_000)]
    if not describe and not (fields and (image is not None or body.strip())):
        return out, None
    props: dict[str, Any] = {}
    if describe:
        props["description"] = {"type": "string"}
        props["labels"] = {"type": "array", "items": {"type": "string"}}
    if fields:
        props["extracted"] = {"type": "object", "additionalProperties": False, "required": sorted(fields),
                              "properties": {k: {"type": ["string", "null"]} for k in fields}}
    schema = {"type": "object", "additionalProperties": False, "required": sorted(props), "properties": props}
    asks = []
    if describe:
        asks.append("describe the image in one or two sentences (description) and give up to 10 short labels (labels)")
    if fields:
        asks.append("extract these fields (extracted), null when absent: "
                    + "; ".join(f"{k}: {v}" for k, v in sorted(fields.items())))
    user = f"File: {os.path.basename(name)}\nTask: " + "; and ".join(asks) + "."
    if body.strip():
        user += f"\n\nThe file's text:\n<<<\n{body}\n>>>"
    from cairn.ai.llm import LLMError

    try:
        with llm_task("pipe_ai"):
            result, _ = llm.generate_json(SYSTEM, user, schema, max_tokens=4000, images=[image] if image else None)
    except LLMError as e:
        return out, f"AI: {str(e)[:300]}"
    except Exception as e:  # noqa: BLE001 - recorded on the row; the pipe goes on
        return out, f"AI: {type(e).__name__}: {str(e)[:300]}"
    if describe:
        out["description"] = str(result.get("description") or "")[:4000] or None
        labels = [str(x)[:100] for x in (result.get("labels") or []) if x][:10]
        out["labels"] = json.dumps(labels) if labels else None
    if fields:
        got = result.get("extracted") or {}
        out["extracted"] = json.dumps({k: (None if got.get(k) is None else str(got.get(k))[:2000]) for k in sorted(fields)})
    return out, None
