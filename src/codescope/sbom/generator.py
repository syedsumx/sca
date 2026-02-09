"""Generate SBOM documents in CycloneDX and SPDX formats."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class SBOMComponent:
    """A single dependency / component."""

    name: str
    version: str
    type: str = "library"  # library, framework, application, file
    purl: str = ""  # package URL
    license: str = ""
    description: str = ""
    scope: str = "required"  # required, optional, dev
    hashes: dict[str, str] = field(default_factory=dict)


class SBOMGenerator:
    """Generate SBOM from dependency scan results."""

    def __init__(self, project_name: str = "", project_version: str = "0.0.0") -> None:
        self.project_name = project_name
        self.project_version = project_version
        self.components: list[SBOMComponent] = []

    def add_component(self, component: SBOMComponent) -> None:
        self.components.append(component)

    def load_from_dependency_scan(self, scan_results: list[dict]) -> int:
        """Load components from CodeScope dependency scanner output."""
        count = 0
        for dep in scan_results:
            purl = ""
            ecosystem = dep.get("ecosystem", "").lower()
            name = dep.get("name", "")
            version = dep.get("version", "")
            if ecosystem == "npm":
                purl = f"pkg:npm/{name}@{version}"
            elif ecosystem in ("pypi", "pip"):
                purl = f"pkg:pypi/{name}@{version}"
            elif ecosystem == "go":
                purl = f"pkg:golang/{name}@{version}"
            elif ecosystem == "maven":
                purl = f"pkg:maven/{name}@{version}"
            elif ecosystem == "rubygems":
                purl = f"pkg:gem/{name}@{version}"
            elif ecosystem == "cargo":
                purl = f"pkg:cargo/{name}@{version}"

            self.components.append(SBOMComponent(
                name=name,
                version=version,
                purl=purl,
                license=dep.get("license", ""),
                scope="dev" if dep.get("dev", False) else "required",
            ))
            count += 1
        return count

    def load_from_manifest(self, manifest_path: str | Path) -> int:
        """Parse a manifest file directly (package.json, requirements.txt, etc.)."""
        path = Path(manifest_path)
        if not path.exists():
            return 0

        count = 0
        content = path.read_text(errors="replace")

        if path.name == "package.json":
            data = json.loads(content)
            for section, scope in [("dependencies", "required"), ("devDependencies", "dev")]:
                for name, ver in data.get(section, {}).items():
                    ver_clean = ver.lstrip("^~>=<")
                    self.components.append(SBOMComponent(
                        name=name, version=ver_clean,
                        purl=f"pkg:npm/{name}@{ver_clean}", scope=scope,
                    ))
                    count += 1

        elif path.name in ("requirements.txt", "requirements-dev.txt"):
            for line in content.split("\n"):
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                parts = line.split("==")
                name = parts[0].split(">=")[0].split("<=")[0].split("~=")[0].strip()
                version = parts[1].strip() if len(parts) > 1 else "0.0.0"
                scope = "dev" if "dev" in path.name else "required"
                self.components.append(SBOMComponent(
                    name=name, version=version,
                    purl=f"pkg:pypi/{name}@{version}", scope=scope,
                ))
                count += 1

        elif path.name == "go.sum":
            seen = set()
            for line in content.split("\n"):
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0]
                    version = parts[1].split("/")[0].lstrip("v")
                    key = f"{name}@{version}"
                    if key not in seen:
                        seen.add(key)
                        self.components.append(SBOMComponent(
                            name=name, version=version,
                            purl=f"pkg:golang/{name}@{version}",
                        ))
                        count += 1
        return count

    def to_cyclonedx(self) -> dict:
        """Generate CycloneDX 1.5 JSON SBOM."""
        components = []
        for comp in self.components:
            c: dict = {
                "type": comp.type,
                "name": comp.name,
                "version": comp.version,
                "scope": comp.scope,
                "bom-ref": comp.purl or f"{comp.name}@{comp.version}",
            }
            if comp.purl:
                c["purl"] = comp.purl
            if comp.license:
                c["licenses"] = [{"license": {"id": comp.license}}]
            if comp.hashes:
                c["hashes"] = [{"alg": k, "content": v} for k, v in comp.hashes.items()]
            components.append(c)

        return {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "serialNumber": f"urn:uuid:{uuid.uuid4()}",
            "version": 1,
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tools": [{"vendor": "CodeScope", "name": "codescope", "version": "0.1.0"}],
                "component": {
                    "type": "application",
                    "name": self.project_name,
                    "version": self.project_version,
                },
            },
            "components": components,
        }

    def to_spdx(self) -> dict:
        """Generate SPDX 2.3 JSON SBOM."""
        doc_ns = f"https://codescope.dev/spdx/{self.project_name}/{uuid.uuid4()}"
        packages = []

        # Root package
        packages.append({
            "SPDXID": "SPDXRef-RootPackage",
            "name": self.project_name or "unknown-project",
            "versionInfo": self.project_version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
        })

        relationships = []
        for i, comp in enumerate(self.components):
            spdx_id = f"SPDXRef-Package-{i}"
            pkg: dict = {
                "SPDXID": spdx_id,
                "name": comp.name,
                "versionInfo": comp.version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
            }
            if comp.purl:
                pkg["externalRefs"] = [{
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": comp.purl,
                }]
            if comp.license:
                pkg["licenseConcluded"] = comp.license
                pkg["licenseDeclared"] = comp.license
            else:
                pkg["licenseConcluded"] = "NOASSERTION"
                pkg["licenseDeclared"] = "NOASSERTION"
            packages.append(pkg)
            relationships.append({
                "spdxElementId": "SPDXRef-RootPackage",
                "relatedSpdxElement": spdx_id,
                "relationshipType": "DEPENDS_ON",
            })

        return {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"{self.project_name}-sbom",
            "documentNamespace": doc_ns,
            "creationInfo": {
                "created": datetime.now(timezone.utc).isoformat(),
                "creators": ["Tool: CodeScope-0.1.0"],
            },
            "packages": packages,
            "relationships": relationships,
        }

    def to_json(self, format: str = "cyclonedx") -> str:
        """Serialize to JSON string."""
        if format == "spdx":
            return json.dumps(self.to_spdx(), indent=2)
        return json.dumps(self.to_cyclonedx(), indent=2)
