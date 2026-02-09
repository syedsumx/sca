"""Azure ARM Template and Bicep scanner for CodeScope."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from codescope.iac.scanner import IaCFinding, IaCPlatform, IaCSeverity


class ARMBicepScanner:
    """Scans ARM templates (.json) and Bicep files (.bicep) for misconfigurations."""

    ARM_PLATFORM = IaCPlatform.CLOUDFORMATION  # reuse enum; distinguished by rule_id prefix

    def scan(self, path: Path) -> list[IaCFinding]:
        """Scan a directory for ARM and Bicep files."""
        path = Path(path)
        findings: list[IaCFinding] = []
        findings.extend(self.scan_arm(path))
        findings.extend(self.scan_bicep(path))
        return findings

    # ── ARM template scanning ────────────────────────────────────────

    def scan_arm(self, path: Path) -> list[IaCFinding]:
        """Scan ARM template JSON files."""
        path = Path(path)
        findings: list[IaCFinding] = []

        for json_file in path.rglob("*.json"):
            try:
                content = json_file.read_text(encoding="utf-8")
                template = json.loads(content)
            except Exception:
                continue

            if not isinstance(template, dict):
                continue
            # Identify ARM templates by $schema or resources array
            schema = template.get("$schema", "")
            if "deploymentTemplate" not in schema and "resources" not in template:
                continue
            if not isinstance(template.get("resources"), list):
                continue

            findings.extend(self._scan_arm_template(template, str(json_file)))

        return findings

    def _scan_arm_template(self, template: dict, file_path: str) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        resources = template.get("resources", [])

        for res in resources:
            if not isinstance(res, dict):
                continue
            res_type = res.get("type", "")
            res_name = res.get("name", "unknown")
            props = res.get("properties", {}) or {}

            findings.extend(self._arm0001_storage_https(res_type, res_name, props, file_path))
            findings.extend(self._arm0002_nsg_open(res_type, res_name, props, file_path))
            findings.extend(self._arm0003_sql_no_audit(res_type, res_name, props, file_path))
            findings.extend(self._arm0004_sql_no_tde(res_type, res_name, props, file_path))
            findings.extend(self._arm0005_keyvault_no_purge(res_type, res_name, props, file_path))
            findings.extend(self._arm0006_app_service_http(res_type, res_name, props, file_path))
            findings.extend(self._arm0007_disk_no_encryption(res_type, res_name, props, file_path))
            findings.extend(self._arm0008_sql_public(res_type, res_name, props, file_path))

        # Check parameters for secrets with defaults
        findings.extend(self._arm0009_secret_params(template, file_path))

        return findings

    def _arm0001_storage_https(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type != "Microsoft.Storage/storageAccounts":
            return []
        if props.get("supportsHttpsTrafficOnly") is False:
            return [self._arm_finding("ARM0001", "Storage account allows HTTP",
                f"Storage account '{name}' does not enforce HTTPS.", IaCSeverity.HIGH, fp, name, res_type,
                "Set supportsHttpsTrafficOnly to true.")]
        return []

    def _arm0002_nsg_open(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type != "Microsoft.Network/networkSecurityGroups":
            return []
        findings = []
        rules = props.get("securityRules", [])
        if not isinstance(rules, list):
            return []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_props = rule.get("properties", {}) or {}
            direction = rule_props.get("direction", "")
            access = rule_props.get("access", "")
            src = rule_props.get("sourceAddressPrefix", "")
            dest_port = rule_props.get("destinationPortRange", "")
            if direction == "Inbound" and access == "Allow" and src in ("*", "0.0.0.0/0"):
                if dest_port not in ("80", "443"):
                    findings.append(self._arm_finding("ARM0002", "NSG allows inbound from any source",
                        f"NSG '{name}' allows inbound from {src} on port {dest_port}.",
                        IaCSeverity.HIGH, fp, name, res_type,
                        "Restrict sourceAddressPrefix to specific IP ranges."))
        return findings

    def _arm0003_sql_no_audit(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type != "Microsoft.Sql/servers":
            return []
        # Simple check — auditing is usually a sub-resource
        return [self._arm_finding("ARM0003", "SQL Server audit not inline",
            f"SQL Server '{name}' — verify auditing is configured via sub-resource.",
            IaCSeverity.INFO, fp, name, res_type,
            "Deploy Microsoft.Sql/servers/auditingSettings sub-resource.")]

    def _arm0004_sql_no_tde(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type != "Microsoft.Sql/servers/databases":
            return []
        # TDE is enabled by default on Azure SQL, but check for explicit disable
        tde = props.get("transparentDataEncryption", {})
        if isinstance(tde, dict) and tde.get("status") == "Disabled":
            return [self._arm_finding("ARM0004", "SQL Database TDE disabled",
                f"SQL Database '{name}' has Transparent Data Encryption disabled.",
                IaCSeverity.HIGH, fp, name, res_type,
                "Remove the transparentDataEncryption.status = Disabled setting.")]
        return []

    def _arm0005_keyvault_no_purge(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type != "Microsoft.KeyVault/vaults":
            return []
        if props.get("enablePurgeProtection") is not True:
            return [self._arm_finding("ARM0005", "Key Vault without purge protection",
                f"Key Vault '{name}' does not have purge protection.",
                IaCSeverity.MEDIUM, fp, name, res_type,
                "Set enablePurgeProtection to true.")]
        return []

    def _arm0006_app_service_http(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type not in ("Microsoft.Web/sites", "Microsoft.Web/sites/slots"):
            return []
        if props.get("httpsOnly") is not True:
            return [self._arm_finding("ARM0006", "App Service allows HTTP",
                f"App Service '{name}' does not enforce HTTPS.",
                IaCSeverity.HIGH, fp, name, res_type,
                "Set httpsOnly to true.")]
        return []

    def _arm0007_disk_no_encryption(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type != "Microsoft.Compute/disks":
            return []
        enc = props.get("encryption", {})
        if isinstance(enc, dict) and enc.get("type") == "EncryptionAtRestWithPlatformKey":
            return []  # Default encryption is fine
        if not enc:
            return [self._arm_finding("ARM0007", "Managed Disk without explicit encryption",
                f"Managed Disk '{name}' does not specify encryption settings.",
                IaCSeverity.MEDIUM, fp, name, res_type,
                "Set encryption.type explicitly.")]
        return []

    def _arm0008_sql_public(self, res_type: str, name: str, props: dict, fp: str) -> list[IaCFinding]:
        if res_type != "Microsoft.Sql/servers":
            return []
        if props.get("publicNetworkAccess") == "Enabled":
            return [self._arm_finding("ARM0008", "SQL Server publicly accessible",
                f"SQL Server '{name}' has publicNetworkAccess enabled.",
                IaCSeverity.CRITICAL, fp, name, res_type,
                "Set publicNetworkAccess to Disabled and use private endpoints.")]
        return []

    def _arm0009_secret_params(self, template: dict, fp: str) -> list[IaCFinding]:
        findings = []
        params = template.get("parameters", {})
        if not isinstance(params, dict):
            return []
        secret_pat = re.compile(r"(password|secret|key)", re.IGNORECASE)
        for pname, pdef in params.items():
            if not isinstance(pdef, dict):
                continue
            ptype = pdef.get("type", "")
            if secret_pat.search(pname) and ptype != "secureString" and "defaultValue" in pdef:
                findings.append(self._arm_finding("ARM0009", "Secret parameter with default value",
                    f"Parameter '{pname}' appears to be a secret but uses type '{ptype}' with a default value.",
                    IaCSeverity.CRITICAL, fp, pname, "Parameter",
                    "Use type 'secureString' and remove defaultValue."))
        return findings

    # ── Bicep scanning ───────────────────────────────────────────────

    def scan_bicep(self, path: Path) -> list[IaCFinding]:
        """Scan Bicep (.bicep) files for misconfigurations."""
        path = Path(path)
        findings: list[IaCFinding] = []

        for bicep_file in path.rglob("*.bicep"):
            try:
                content = bicep_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            findings.extend(self._scan_bicep_content(content, str(bicep_file)))

        return findings

    def _scan_bicep_content(self, content: str, file_path: str) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        lines = content.split("\n")

        # Parse resource blocks
        resources = self._parse_bicep_resources(content, file_path)

        for res in resources:
            findings.extend(self._bicep_rules(res))

        # BIC0009 — @secure() missing on secret params
        findings.extend(self._bic0009_insecure_params(lines, file_path))

        return findings

    def _parse_bicep_resources(self, content: str, file_path: str) -> list[dict]:
        """Parse Bicep resource declarations."""
        resources = []
        lines = content.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            # resource <name> '<type>@<version>' = { ... }
            match = re.match(r"^\s*resource\s+(\w+)\s+'([^']+)'", line)
            if match:
                res_name = match.group(1)
                res_type_full = match.group(2)
                # Extract just the resource type without API version
                res_type = res_type_full.split("@")[0] if "@" in res_type_full else res_type_full
                start_line = i + 1  # 1-indexed

                # Collect the block body
                depth = 0
                body_lines = []
                j = i
                while j < len(lines):
                    depth += lines[j].count("{") - lines[j].count("}")
                    body_lines.append(lines[j])
                    j += 1
                    if depth <= 0 and "{" in "".join(body_lines):
                        break

                resources.append({
                    "name": res_name,
                    "type": res_type,
                    "body": "\n".join(body_lines),
                    "line": start_line,
                    "file": file_path,
                })
                i = j
            else:
                i += 1
        return resources

    def _bicep_rules(self, res: dict) -> list[IaCFinding]:
        findings = []
        res_type = res["type"]
        body = res["body"]

        # BIC0001 — Storage HTTPS
        if res_type == "Microsoft.Storage/storageAccounts":
            if "supportsHttpsTrafficOnly: false" in body:
                findings.append(self._bicep_finding("BIC0001", "Storage allows HTTP",
                    f"Storage account '{res['name']}' does not enforce HTTPS.",
                    IaCSeverity.HIGH, res,
                    "Set supportsHttpsTrafficOnly: true"))

        # BIC0002 — NSG open to world
        if res_type == "Microsoft.Network/networkSecurityGroups":
            if "'*'" in body or "'0.0.0.0/0'" in body:
                if "Inbound" in body and "Allow" in body:
                    findings.append(self._bicep_finding("BIC0002", "NSG open to world",
                        f"NSG '{res['name']}' allows inbound from any source.",
                        IaCSeverity.HIGH, res,
                        "Restrict sourceAddressPrefix to specific CIDRs."))

        # BIC0003 — Key Vault purge protection
        if res_type == "Microsoft.KeyVault/vaults":
            if "enablePurgeProtection: true" not in body:
                findings.append(self._bicep_finding("BIC0003", "Key Vault without purge protection",
                    f"Key Vault '{res['name']}' does not enable purge protection.",
                    IaCSeverity.MEDIUM, res,
                    "Add enablePurgeProtection: true"))

        # BIC0004 — App Service HTTP
        if "Microsoft.Web/sites" in res_type:
            if "httpsOnly: false" in body or "httpsOnly:" not in body:
                findings.append(self._bicep_finding("BIC0004", "App Service allows HTTP",
                    f"App Service '{res['name']}' does not enforce HTTPS.",
                    IaCSeverity.HIGH, res,
                    "Set httpsOnly: true"))

        # BIC0005 — SQL public access
        if res_type == "Microsoft.Sql/servers":
            if "publicNetworkAccess: 'Enabled'" in body:
                findings.append(self._bicep_finding("BIC0005", "SQL Server publicly accessible",
                    f"SQL Server '{res['name']}' has public network access.",
                    IaCSeverity.CRITICAL, res,
                    "Set publicNetworkAccess: 'Disabled'"))

        # BIC0006 — AKS RBAC
        if res_type == "Microsoft.ContainerService/managedClusters":
            if "enableRBAC: false" in body:
                findings.append(self._bicep_finding("BIC0006", "AKS RBAC disabled",
                    f"AKS cluster '{res['name']}' has RBAC disabled.",
                    IaCSeverity.HIGH, res,
                    "Set enableRBAC: true"))

        # BIC0007 — Managed disk encryption
        if res_type == "Microsoft.Compute/disks":
            if "encryptionSettingsCollection" not in body and "encryption:" not in body:
                findings.append(self._bicep_finding("BIC0007", "Disk without explicit encryption",
                    f"Disk '{res['name']}' does not have explicit encryption.",
                    IaCSeverity.MEDIUM, res,
                    "Add encryption settings."))

        # BIC0008 — Hardcoded secrets
        secret_pat = re.compile(r"(password|secret|adminPassword|clientSecret)\s*:\s*'[^']+'")
        match = secret_pat.search(body)
        if match:
            findings.append(self._bicep_finding("BIC0008", "Hardcoded secret in Bicep",
                f"Resource '{res['name']}' contains hardcoded secret value.",
                IaCSeverity.CRITICAL, res,
                "Use @secure() parameters or Key Vault references."))

        return findings

    def _bic0009_insecure_params(self, lines: list[str], file_path: str) -> list[IaCFinding]:
        """Check for params that look like secrets but are not marked @secure()."""
        findings = []
        secret_pat = re.compile(r"(password|secret|key|token)", re.IGNORECASE)

        for i, line in enumerate(lines):
            match = re.match(r"^\s*param\s+(\w+)\s+string", line)
            if match:
                param_name = match.group(1)
                if secret_pat.search(param_name):
                    # Check if the previous line has @secure()
                    prev = lines[i - 1].strip() if i > 0 else ""
                    if "@secure()" not in prev:
                        findings.append(IaCFinding(
                            rule_id="BIC0009",
                            title="Secret parameter without @secure()",
                            description=f"Parameter '{param_name}' looks like a secret but is not marked @secure().",
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.TERRAFORM,  # reuse
                            file_path=file_path,
                            line_number=i + 1,
                            resource_type="Parameter",
                            resource_name=param_name,
                            remediation="Add @secure() decorator above the parameter declaration.",
                        ))
        return findings

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _arm_finding(rule_id: str, title: str, desc: str, sev: IaCSeverity,
                     fp: str, name: str, rtype: str, rem: str) -> IaCFinding:
        return IaCFinding(rule_id=rule_id, title=title, description=desc,
                          severity=sev, platform=IaCPlatform.CLOUDFORMATION,
                          file_path=fp, line_number=0,
                          resource_type=rtype, resource_name=name, remediation=rem)

    @staticmethod
    def _bicep_finding(rule_id: str, title: str, desc: str, sev: IaCSeverity,
                       res: dict, rem: str) -> IaCFinding:
        return IaCFinding(rule_id=rule_id, title=title, description=desc,
                          severity=sev, platform=IaCPlatform.TERRAFORM,
                          file_path=res["file"], line_number=res["line"],
                          resource_type=res["type"], resource_name=res["name"],
                          remediation=rem)
