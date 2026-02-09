"""Container Image Scanning API routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.container import ContainerScanner

router = APIRouter()


class ContainerScanRequest(BaseModel):
    project_path: str


class DockerfileLintRequest(BaseModel):
    file_path: str


@router.post("/container/scan")
async def scan_containers(request: ContainerScanRequest):
    """Scan a project for Dockerfile and docker-compose misconfigurations."""
    scan_path = Path(request.project_path)
    if not scan_path.exists():
        raise HTTPException(status_code=404, detail="Project path not found")

    scanner = ContainerScanner()
    result = scanner.scan(scan_path)
    return result.to_dict()


@router.post("/container/scan-dockerfile")
async def scan_dockerfile(request: DockerfileLintRequest):
    """Lint a single Dockerfile."""
    file_path = Path(request.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    scanner = ContainerScanner()
    findings = scanner.scan_dockerfile(file_path)
    return {
        "file": str(file_path),
        "findings": [f.to_dict() for f in findings],
        "total": len(findings),
    }


@router.post("/container/scan-compose")
async def scan_compose(request: DockerfileLintRequest):
    """Lint a single docker-compose file."""
    file_path = Path(request.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    scanner = ContainerScanner()
    findings = scanner.scan_compose(file_path)
    return {
        "file": str(file_path),
        "findings": [f.to_dict() for f in findings],
        "total": len(findings),
    }


@router.get("/container/rules")
async def list_container_rules():
    """List all container scanning rules."""
    dockerfile_rules = [
        {"id": "DL0001", "severity": "HIGH", "title": "Running as root", "platform": "dockerfile"},
        {"id": "DL0002", "severity": "MEDIUM", "title": "Using latest tag", "platform": "dockerfile"},
        {"id": "DL0003", "severity": "CRITICAL", "title": "Secrets in ENV", "platform": "dockerfile"},
        {"id": "DL0004", "severity": "LOW", "title": "COPY without --chown", "platform": "dockerfile"},
        {"id": "DL0005", "severity": "LOW", "title": "ADD instead of COPY", "platform": "dockerfile"},
        {"id": "DL0006", "severity": "LOW", "title": "apt-get without --no-install-recommends", "platform": "dockerfile"},
        {"id": "DL0007", "severity": "MEDIUM", "title": "Unpinned package versions", "platform": "dockerfile"},
        {"id": "DL0008", "severity": "HIGH", "title": "Exposing SSH port", "platform": "dockerfile"},
        {"id": "DL0009", "severity": "MEDIUM", "title": "Using sudo", "platform": "dockerfile"},
        {"id": "DL0010", "severity": "INFO", "title": "Missing HEALTHCHECK", "platform": "dockerfile"},
        {"id": "DL0011", "severity": "INFO", "title": "Non-multistage FROM", "platform": "dockerfile"},
        {"id": "DL0012", "severity": "CRITICAL", "title": "Curl piped to shell", "platform": "dockerfile"},
        {"id": "DL0013", "severity": "MEDIUM", "title": "Too many exposed ports", "platform": "dockerfile"},
        {"id": "DL0014", "severity": "MEDIUM", "title": "Relative WORKDIR", "platform": "dockerfile"},
        {"id": "DL0015", "severity": "LOW", "title": "Apt cache not cleaned", "platform": "dockerfile"},
    ]
    compose_rules = [
        {"id": "DC0001", "severity": "CRITICAL", "title": "Privileged container", "platform": "compose"},
        {"id": "DC0002", "severity": "HIGH", "title": "Host network mode", "platform": "compose"},
        {"id": "DC0003", "severity": "HIGH", "title": "Host PID namespace", "platform": "compose"},
        {"id": "DC0004", "severity": "CRITICAL", "title": "Secrets in environment", "platform": "compose"},
        {"id": "DC0005", "severity": "MEDIUM", "title": "No resource limits", "platform": "compose"},
        {"id": "DC0006", "severity": "LOW", "title": "Writable root filesystem", "platform": "compose"},
        {"id": "DC0007", "severity": "CRITICAL", "title": "All capabilities added", "platform": "compose"},
        {"id": "DC0008", "severity": "LOW", "title": "Missing no-new-privileges", "platform": "compose"},
    ]
    return {"rules": dockerfile_rules + compose_rules, "total": 23}
