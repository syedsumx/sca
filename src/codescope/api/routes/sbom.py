"""SBOM generation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from codescope.sbom.generator import SBOMGenerator

router = APIRouter()


class SBOMRequest(BaseModel):
    path: str = "."
    project_name: str = "my-project"
    project_version: str = "0.0.0"
    format: str = "cyclonedx"  # cyclonedx or spdx


@router.post("/sbom/generate")
async def generate_sbom(req: SBOMRequest):
    """Generate an SBOM from manifest files in the given path."""
    from pathlib import Path

    base = Path(req.path)
    if not base.exists():
        raise HTTPException(404, f"Path not found: {req.path}")

    gen = SBOMGenerator(project_name=req.project_name, project_version=req.project_version)

    # Auto-discover manifest files
    manifests = [
        "package.json", "requirements.txt", "requirements-dev.txt",
        "go.sum", "Gemfile.lock", "Cargo.lock",
    ]
    loaded = 0
    for name in manifests:
        manifest = base / name
        if manifest.exists():
            loaded += gen.load_from_manifest(manifest)

    # Also check subdirectories one level deep
    if base.is_dir():
        for child in base.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                for name in manifests:
                    manifest = child / name
                    if manifest.exists():
                        loaded += gen.load_from_manifest(manifest)

    if req.format == "spdx":
        doc = gen.to_spdx()
    else:
        doc = gen.to_cyclonedx()

    return {
        "format": req.format,
        "components_count": len(gen.components),
        "sbom": doc,
    }


@router.post("/sbom/download")
async def download_sbom(req: SBOMRequest):
    """Generate and return SBOM as downloadable JSON."""
    from pathlib import Path

    base = Path(req.path)
    if not base.exists():
        raise HTTPException(404, f"Path not found: {req.path}")

    gen = SBOMGenerator(project_name=req.project_name, project_version=req.project_version)
    manifests = ["package.json", "requirements.txt", "requirements-dev.txt", "go.sum"]
    for name in manifests:
        manifest = base / name
        if manifest.exists():
            gen.load_from_manifest(manifest)

    content = gen.to_json(format=req.format)
    filename = f"{req.project_name}-sbom.{req.format}.json"

    return JSONResponse(
        content={"filename": filename, "content": content},
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
