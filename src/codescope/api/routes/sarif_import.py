"""SARIF import endpoint — aggregate results from external tools."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

router = APIRouter()

# In-memory imported results store
_imported_results: dict[str, dict] = {}

_SARIF_LEVEL_TO_SEVERITY = {
    "error": "BLOCKER",
    "warning": "MAJOR",
    "note": "MINOR",
    "none": "INFO",
}

_SARIF_KIND_TO_TYPE = {
    "fail": "BUG",
    "open": "VULNERABILITY",
    "informational": "CODE_SMELL",
    "notApplicable": "CODE_SMELL",
    "pass": "CODE_SMELL",
    "review": "CODE_SMELL",
}


def _parse_sarif(sarif: dict) -> list[dict]:
    """Convert SARIF JSON into a flat list of CodeScope-style issues."""
    issues: list[dict] = []
    for run in sarif.get("runs", []):
        tool_name = run.get("tool", {}).get("driver", {}).get("name", "unknown")
        rules_map: dict[str, dict] = {}
        for rule in run.get("tool", {}).get("driver", {}).get("rules", []):
            rules_map[rule["id"]] = rule

        for result in run.get("results", []):
            rule_id = result.get("ruleId", "unknown")
            rule_info = rules_map.get(rule_id, {})
            level = result.get("level", "warning")
            kind = result.get("kind", "fail")

            location = {}
            for loc in result.get("locations", [])[:1]:
                phys = loc.get("physicalLocation", {})
                artifact = phys.get("artifactLocation", {})
                region = phys.get("region", {})
                location = {
                    "file": artifact.get("uri", ""),
                    "start_line": region.get("startLine", 0),
                    "end_line": region.get("endLine", region.get("startLine", 0)),
                    "start_column": region.get("startColumn", 0),
                    "end_column": region.get("endColumn", 0),
                }

            issues.append({
                "id": str(uuid.uuid4()),
                "rule_id": f"{tool_name}:{rule_id}",
                "rule_name": rule_info.get("shortDescription", {}).get("text", rule_id),
                "type": _SARIF_KIND_TO_TYPE.get(kind, "BUG"),
                "severity": _SARIF_LEVEL_TO_SEVERITY.get(level, "MAJOR"),
                "message": result.get("message", {}).get("text", ""),
                "location": location,
                "tool": tool_name,
                "tags": ["imported", f"tool:{tool_name}"],
            })
    return issues


@router.post("/sarif/import")
async def import_sarif(file: UploadFile = File(...)):
    """Import a SARIF file and convert to CodeScope issues."""
    if not file.filename or not file.filename.endswith((".sarif", ".json")):
        raise HTTPException(400, "File must be .sarif or .json")
    content = await file.read()
    try:
        sarif = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON in SARIF file")

    if sarif.get("$schema", "").find("sarif") == -1 and "runs" not in sarif:
        raise HTTPException(400, "File does not appear to be valid SARIF")

    issues = _parse_sarif(sarif)
    import_id = str(uuid.uuid4())[:8]

    # Group by tool
    tools: dict[str, int] = {}
    for iss in issues:
        t = iss.get("tool", "unknown")
        tools[t] = tools.get(t, 0) + 1

    _imported_results[import_id] = {
        "id": import_id,
        "filename": file.filename,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "issue_count": len(issues),
        "tools": tools,
        "issues": issues,
    }
    return {
        "import_id": import_id,
        "issue_count": len(issues),
        "tools": tools,
        "message": f"Imported {len(issues)} issues from {file.filename}",
    }


@router.get("/sarif/imports")
async def list_imports():
    """List all SARIF imports."""
    return [
        {k: v for k, v in imp.items() if k != "issues"}
        for imp in _imported_results.values()
    ]


@router.get("/sarif/imports/{import_id}")
async def get_import(import_id: str):
    """Get full issues from a SARIF import."""
    imp = _imported_results.get(import_id)
    if not imp:
        raise HTTPException(404, "Import not found")
    return imp


@router.delete("/sarif/imports/{import_id}")
async def delete_import(import_id: str):
    """Delete a SARIF import."""
    if import_id not in _imported_results:
        raise HTTPException(404, "Import not found")
    del _imported_results[import_id]
    return {"message": "Import deleted"}
