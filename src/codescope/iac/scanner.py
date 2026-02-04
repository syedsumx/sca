"""Infrastructure-as-Code scanning engine for CodeScope.

Scans Terraform, CloudFormation, Kubernetes YAML, and Helm charts
for security misconfigurations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class IaCPlatform(Enum):
    """Supported IaC platforms."""

    TERRAFORM = "terraform"
    CLOUDFORMATION = "cloudformation"
    KUBERNETES = "kubernetes"
    HELM = "helm"


class IaCSeverity(Enum):
    """Severity levels for IaC findings."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class IaCFinding:
    """A single IaC security finding."""

    rule_id: str
    title: str
    description: str
    severity: IaCSeverity
    platform: IaCPlatform
    file_path: str
    line_number: int
    resource_type: str
    resource_name: str
    remediation: str

    def to_dict(self) -> dict[str, Any]:
        """Convert finding to a dictionary."""
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "severity": self.severity.value,
            "platform": self.platform.value,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "remediation": self.remediation,
        }


@dataclass
class IaCScanResult:
    """Result of an IaC scan."""

    findings: list[IaCFinding] = field(default_factory=list)
    files_scanned: int = 0
    platforms_detected: list[IaCPlatform] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        """Number of critical findings."""
        return sum(1 for f in self.findings if f.severity == IaCSeverity.CRITICAL)

    @property
    def high_count(self) -> int:
        """Number of high findings."""
        return sum(1 for f in self.findings if f.severity == IaCSeverity.HIGH)

    @property
    def medium_count(self) -> int:
        """Number of medium findings."""
        return sum(1 for f in self.findings if f.severity == IaCSeverity.MEDIUM)

    @property
    def low_count(self) -> int:
        """Number of low findings."""
        return sum(1 for f in self.findings if f.severity == IaCSeverity.LOW)

    @property
    def info_count(self) -> int:
        """Number of info findings."""
        return sum(1 for f in self.findings if f.severity == IaCSeverity.INFO)

    @property
    def is_clean(self) -> bool:
        """Whether no findings were detected."""
        return len(self.findings) == 0

    def to_dict(self) -> dict[str, Any]:
        """Convert scan result to a dictionary."""
        return {
            "findings": [f.to_dict() for f in self.findings],
            "files_scanned": self.files_scanned,
            "platforms_detected": [p.value for p in self.platforms_detected],
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "medium_count": self.medium_count,
            "low_count": self.low_count,
            "info_count": self.info_count,
            "is_clean": self.is_clean,
        }


# ---------------------------------------------------------------------------
# Terraform simple HCL-like parser helpers
# ---------------------------------------------------------------------------

def _parse_tf_resources(content: str, file_path: str) -> list[dict[str, Any]]:
    """Parse Terraform .tf file content into a list of resource dicts.

    Each dict contains:
        - type: resource type (e.g. "aws_s3_bucket")
        - name: resource name
        - body: raw text of the block body
        - line: starting line number
        - file: file path
    """
    resources: list[dict[str, Any]] = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(
            r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', line
        )
        if match:
            rtype = match.group(1)
            rname = match.group(2)
            start_line = i + 1  # 1-indexed
            depth = 1
            body_lines = []
            j = i + 1
            while j < len(lines) and depth > 0:
                body_lines.append(lines[j])
                depth += lines[j].count("{") - lines[j].count("}")
                j += 1
            resources.append(
                {
                    "type": rtype,
                    "name": rname,
                    "body": "\n".join(body_lines),
                    "line": start_line,
                    "file": file_path,
                }
            )
            i = j
        else:
            i += 1
    return resources


def _tf_body_has_block(body: str, block_name: str) -> bool:
    """Check if the resource body contains a named block."""
    return bool(re.search(rf'^\s*{re.escape(block_name)}\s*\{{', body, re.MULTILINE))


def _tf_body_has_key_value(body: str, key: str, value: str) -> bool:
    """Check if the resource body contains key = value."""
    pattern = rf'^\s*{re.escape(key)}\s*=\s*{re.escape(value)}'
    return bool(re.search(pattern, body, re.MULTILINE))


def _tf_body_get_value(body: str, key: str) -> str | None:
    """Get the value for a key in the body (simple single-line match)."""
    match = re.search(rf'^\s*{re.escape(key)}\s*=\s*(.+)', body, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class IaCScanner:
    """Infrastructure-as-Code security scanner."""

    def scan(self, path: Path) -> IaCScanResult:
        """Scan a directory for all IaC files and return aggregated results."""
        path = Path(path)
        all_findings: list[IaCFinding] = []
        files_scanned = 0
        platforms: list[IaCPlatform] = []

        # Detect Terraform (AWS + multi-cloud: Azure, GCP, OCI)
        tf_files = list(path.rglob("*.tf"))
        if tf_files:
            platforms.append(IaCPlatform.TERRAFORM)
            files_scanned += len(tf_files)
            all_findings.extend(self.scan_terraform(path))
            # Multi-cloud Terraform rules (Azure, GCP, OCI)
            from codescope.iac.multicloud import MultiCloudTerraformScanner
            all_findings.extend(MultiCloudTerraformScanner().scan(path))

        # Detect ARM templates and Bicep
        arm_files = list(path.rglob("*.bicep"))
        arm_json = [f for f in path.rglob("*.json") if self._is_arm_template(f)]
        if arm_files or arm_json:
            files_scanned += len(arm_files) + len(arm_json)
            from codescope.iac.arm_bicep import ARMBicepScanner
            all_findings.extend(ARMBicepScanner().scan(path))

        # Detect CloudFormation
        cfn_files = self._find_cloudformation_files(path)
        if cfn_files:
            platforms.append(IaCPlatform.CLOUDFORMATION)
            files_scanned += len(cfn_files)
            all_findings.extend(self.scan_cloudformation(path))

        # Detect Helm (before generic K8s so we can exclude helm-managed files)
        helm_dirs = self._find_helm_charts(path)
        if helm_dirs:
            platforms.append(IaCPlatform.HELM)
            for hdir in helm_dirs:
                values_file = hdir / "values.yaml"
                if values_file.exists():
                    files_scanned += 1
            all_findings.extend(self.scan_helm(path))

        # Detect Kubernetes YAML
        k8s_files = self._find_kubernetes_files(path)
        if k8s_files:
            platforms.append(IaCPlatform.KUBERNETES)
            files_scanned += len(k8s_files)
            all_findings.extend(self.scan_kubernetes(path))

        return IaCScanResult(
            findings=all_findings,
            files_scanned=files_scanned,
            platforms_detected=platforms,
        )

    @staticmethod
    def _is_arm_template(f: Path) -> bool:
        """Check if a JSON file is an ARM template."""
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                schema = data.get("$schema", "")
                return "deploymentTemplate" in schema
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Terraform scanning
    # ------------------------------------------------------------------

    def scan_terraform(self, path: Path) -> list[IaCFinding]:
        """Scan Terraform .tf files for security misconfigurations."""
        path = Path(path)
        findings: list[IaCFinding] = []
        tf_files = list(path.rglob("*.tf"))

        all_resources: list[dict[str, Any]] = []
        all_contents: dict[str, str] = {}

        for tf_file in tf_files:
            try:
                content = tf_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            all_contents[str(tf_file)] = content
            resources = _parse_tf_resources(content, str(tf_file))
            all_resources.extend(resources)

        findings.extend(self._tf0001_s3_no_encryption(all_resources))
        findings.extend(self._tf0002_s3_public_access(all_resources))
        findings.extend(self._tf0003_sg_open_to_world(all_resources))
        findings.extend(self._tf0004_rds_no_encryption(all_resources))
        findings.extend(self._tf0005_rds_publicly_accessible(all_resources))
        findings.extend(self._tf0006_iam_wildcard(all_resources))
        findings.extend(self._tf0007_missing_logging(all_resources))
        findings.extend(self._tf0008_unencrypted_ebs(all_resources))
        findings.extend(self._tf0009_default_vpc(all_resources))
        findings.extend(self._tf0010_hardcoded_secrets(all_resources))

        return findings

    def _tf0001_s3_no_encryption(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] == "aws_s3_bucket":
                if not _tf_body_has_block(res["body"], "server_side_encryption_configuration"):
                    findings.append(
                        IaCFinding(
                            rule_id="TF0001",
                            title="S3 bucket without encryption",
                            description=(
                                f"S3 bucket '{res['name']}' does not have "
                                "server-side encryption configured."
                            ),
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.TERRAFORM,
                            file_path=res["file"],
                            line_number=res["line"],
                            resource_type=res["type"],
                            resource_name=res["name"],
                            remediation=(
                                "Add a server_side_encryption_configuration block "
                                "with AES256 or aws:kms algorithm."
                            ),
                        )
                    )
        return findings

    def _tf0002_s3_public_access(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] == "aws_s3_bucket":
                acl_val = _tf_body_get_value(res["body"], "acl")
                if acl_val and acl_val.strip('"') in ("public-read", "public-read-write"):
                    findings.append(
                        IaCFinding(
                            rule_id="TF0002",
                            title="S3 bucket with public access",
                            description=(
                                f"S3 bucket '{res['name']}' has a public ACL: {acl_val}."
                            ),
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.TERRAFORM,
                            file_path=res["file"],
                            line_number=res["line"],
                            resource_type=res["type"],
                            resource_name=res["name"],
                            remediation=(
                                'Set acl = "private" and use bucket policies for '
                                "controlled access."
                            ),
                        )
                    )
        return findings

    def _tf0003_sg_open_to_world(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] == "aws_security_group":
                ingress_blocks = re.finditer(
                    r'ingress\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
                    res["body"],
                    re.DOTALL,
                )
                for ing_match in ingress_blocks:
                    ing_body = ing_match.group(1)
                    if '0.0.0.0/0' in ing_body:
                        # Check port
                        port_match = re.search(
                            r'(?:from_port|to_port)\s*=\s*(\d+)', ing_body
                        )
                        if port_match:
                            port = int(port_match.group(1))
                            if port not in (80, 443):
                                findings.append(
                                    IaCFinding(
                                        rule_id="TF0003",
                                        title="Security group open to world",
                                        description=(
                                            f"Security group '{res['name']}' allows "
                                            f"ingress from 0.0.0.0/0 on port {port}."
                                        ),
                                        severity=IaCSeverity.HIGH,
                                        platform=IaCPlatform.TERRAFORM,
                                        file_path=res["file"],
                                        line_number=res["line"],
                                        resource_type=res["type"],
                                        resource_name=res["name"],
                                        remediation=(
                                            "Restrict ingress CIDR blocks to specific "
                                            "IP ranges instead of 0.0.0.0/0."
                                        ),
                                    )
                                )
                        else:
                            # No port specified but open to world
                            findings.append(
                                IaCFinding(
                                    rule_id="TF0003",
                                    title="Security group open to world",
                                    description=(
                                        f"Security group '{res['name']}' allows "
                                        "ingress from 0.0.0.0/0."
                                    ),
                                    severity=IaCSeverity.HIGH,
                                    platform=IaCPlatform.TERRAFORM,
                                    file_path=res["file"],
                                    line_number=res["line"],
                                    resource_type=res["type"],
                                    resource_name=res["name"],
                                    remediation=(
                                        "Restrict ingress CIDR blocks to specific "
                                        "IP ranges instead of 0.0.0.0/0."
                                    ),
                                )
                            )
        return findings

    def _tf0004_rds_no_encryption(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] == "aws_db_instance":
                if not _tf_body_has_key_value(res["body"], "storage_encrypted", "true"):
                    findings.append(
                        IaCFinding(
                            rule_id="TF0004",
                            title="RDS instance without encryption",
                            description=(
                                f"RDS instance '{res['name']}' does not have "
                                "storage encryption enabled."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.TERRAFORM,
                            file_path=res["file"],
                            line_number=res["line"],
                            resource_type=res["type"],
                            resource_name=res["name"],
                            remediation="Add storage_encrypted = true to the resource.",
                        )
                    )
        return findings

    def _tf0005_rds_publicly_accessible(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] == "aws_db_instance":
                if _tf_body_has_key_value(res["body"], "publicly_accessible", "true"):
                    findings.append(
                        IaCFinding(
                            rule_id="TF0005",
                            title="RDS instance publicly accessible",
                            description=(
                                f"RDS instance '{res['name']}' is publicly accessible."
                            ),
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.TERRAFORM,
                            file_path=res["file"],
                            line_number=res["line"],
                            resource_type=res["type"],
                            resource_name=res["name"],
                            remediation="Set publicly_accessible = false.",
                        )
                    )
        return findings

    def _tf0006_iam_wildcard(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] in ("aws_iam_policy", "aws_iam_role_policy"):
                body = res["body"]
                if re.search(r'actions\s*=\s*\[\s*"\*"\s*\]', body) or re.search(
                    r'resources\s*=\s*\[\s*"\*"\s*\]', body
                ):
                    findings.append(
                        IaCFinding(
                            rule_id="TF0006",
                            title="IAM policy with wildcard",
                            description=(
                                f"IAM policy '{res['name']}' uses wildcard (*) in "
                                "actions or resources."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.TERRAFORM,
                            file_path=res["file"],
                            line_number=res["line"],
                            resource_type=res["type"],
                            resource_name=res["name"],
                            remediation=(
                                "Follow the principle of least privilege: specify "
                                "exact actions and resource ARNs."
                            ),
                        )
                    )
        return findings

    def _tf0007_missing_logging(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        resource_types = {r["type"] for r in resources}
        has_cloudtrail = "aws_cloudtrail" in resource_types
        has_flow_log = "aws_flow_log" in resource_types
        if resources and not has_cloudtrail and not has_flow_log:
            # Use the first resource file/line as reference
            ref = resources[0]
            findings.append(
                IaCFinding(
                    rule_id="TF0007",
                    title="Missing logging and monitoring",
                    description=(
                        "No aws_cloudtrail or aws_flow_log resources found. "
                        "Logging and monitoring should be configured."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.TERRAFORM,
                    file_path=ref["file"],
                    line_number=0,
                    resource_type="",
                    resource_name="",
                    remediation=(
                        "Add aws_cloudtrail and/or aws_flow_log resources "
                        "for audit logging."
                    ),
                )
            )
        return findings

    def _tf0008_unencrypted_ebs(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] == "aws_ebs_volume":
                if not _tf_body_has_key_value(res["body"], "encrypted", "true"):
                    findings.append(
                        IaCFinding(
                            rule_id="TF0008",
                            title="Unencrypted EBS volume",
                            description=(
                                f"EBS volume '{res['name']}' does not have "
                                "encryption enabled."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.TERRAFORM,
                            file_path=res["file"],
                            line_number=res["line"],
                            resource_type=res["type"],
                            resource_name=res["name"],
                            remediation="Add encrypted = true to the EBS volume resource.",
                        )
                    )
        return findings

    def _tf0009_default_vpc(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        for res in resources:
            if res["type"] == "aws_default_vpc":
                findings.append(
                    IaCFinding(
                        rule_id="TF0009",
                        title="Default VPC used",
                        description=(
                            f"Default VPC resource '{res['name']}' is present. "
                            "Using the default VPC is discouraged."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.TERRAFORM,
                        file_path=res["file"],
                        line_number=res["line"],
                        resource_type=res["type"],
                        resource_name=res["name"],
                        remediation=(
                            "Create a custom VPC with proper network segmentation."
                        ),
                    )
                )
        return findings

    def _tf0010_hardcoded_secrets(
        self, resources: list[dict[str, Any]]
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        secret_keys = ("password", "secret_key", "access_key")
        for res in resources:
            for key in secret_keys:
                val = _tf_body_get_value(res["body"], key)
                if val is not None:
                    # Skip variable references
                    stripped = val.strip('"').strip("'")
                    if stripped.startswith("var.") or stripped.startswith("${"):
                        continue
                    # It's a literal string value
                    if val.startswith('"') or val.startswith("'"):
                        findings.append(
                            IaCFinding(
                                rule_id="TF0010",
                                title="Hardcoded secret in Terraform",
                                description=(
                                    f"Resource '{res['name']}' has a hardcoded "
                                    f"value for '{key}'."
                                ),
                                severity=IaCSeverity.CRITICAL,
                                platform=IaCPlatform.TERRAFORM,
                                file_path=res["file"],
                                line_number=res["line"],
                                resource_type=res["type"],
                                resource_name=res["name"],
                                remediation=(
                                    "Use variables, secrets manager, or environment "
                                    "variables instead of hardcoded secrets."
                                ),
                            )
                        )
        return findings

    # ------------------------------------------------------------------
    # CloudFormation scanning
    # ------------------------------------------------------------------

    def scan_cloudformation(self, path: Path) -> list[IaCFinding]:
        """Scan CloudFormation templates for security misconfigurations."""
        path = Path(path)
        findings: list[IaCFinding] = []
        cfn_files = self._find_cloudformation_files(path)

        for cfn_file in cfn_files:
            try:
                content = cfn_file.read_text(encoding="utf-8")
                if cfn_file.suffix in (".yaml", ".yml"):
                    template = yaml.safe_load(content)
                else:
                    template = json.loads(content)
            except Exception:
                continue

            if not isinstance(template, dict):
                continue

            file_str = str(cfn_file)
            findings.extend(self._cfn_scan_template(template, file_str))

        return findings

    def _find_cloudformation_files(self, path: Path) -> list[Path]:
        """Find CloudFormation template files."""
        cfn_files: list[Path] = []
        for ext in ("*.yaml", "*.yml", "*.json"):
            for f in path.rglob(ext):
                try:
                    content = f.read_text(encoding="utf-8")
                    if f.suffix in (".yaml", ".yml"):
                        data = yaml.safe_load(content)
                    else:
                        data = json.loads(content)
                    if isinstance(data, dict) and (
                        "AWSTemplateFormatVersion" in data or "Resources" in data
                    ):
                        cfn_files.append(f)
                except Exception:
                    continue
        return cfn_files

    def _cfn_scan_template(
        self, template: dict[str, Any], file_path: str
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        resources = template.get("Resources", {})
        if not isinstance(resources, dict):
            return findings

        for res_name, res_def in resources.items():
            if not isinstance(res_def, dict):
                continue
            res_type = res_def.get("Type", "")
            properties = res_def.get("Properties", {})
            if not isinstance(properties, dict):
                properties = {}

            findings.extend(
                self._cf0001_s3_no_encryption(
                    res_name, res_type, properties, file_path
                )
            )
            findings.extend(
                self._cf0002_sg_open_ingress(
                    res_name, res_type, properties, file_path
                )
            )
            findings.extend(
                self._cf0003_iam_wildcard(
                    res_name, res_type, properties, file_path
                )
            )
            findings.extend(
                self._cf0004_rds_no_encryption(
                    res_name, res_type, properties, file_path
                )
            )
            findings.extend(
                self._cf0006_lambda_no_vpc(
                    res_name, res_type, properties, file_path
                )
            )
            findings.extend(
                self._cf0007_cloudfront_no_https(
                    res_name, res_type, properties, file_path
                )
            )
            findings.extend(
                self._cf0008_elb_no_access_logs(
                    res_name, res_type, properties, file_path
                )
            )

        # CF0005 — secrets in parameters
        findings.extend(self._cf0005_secrets_in_params(template, file_path))

        return findings

    def _cf0001_s3_no_encryption(
        self,
        res_name: str,
        res_type: str,
        properties: dict[str, Any],
        file_path: str,
    ) -> list[IaCFinding]:
        if res_type != "AWS::S3::Bucket":
            return []
        if "BucketEncryption" not in properties:
            return [
                IaCFinding(
                    rule_id="CF0001",
                    title="S3 bucket without encryption",
                    description=(
                        f"S3 bucket '{res_name}' does not have BucketEncryption "
                        "configured."
                    ),
                    severity=IaCSeverity.CRITICAL,
                    platform=IaCPlatform.CLOUDFORMATION,
                    file_path=file_path,
                    line_number=0,
                    resource_type=res_type,
                    resource_name=res_name,
                    remediation=(
                        "Add BucketEncryption with ServerSideEncryptionConfiguration."
                    ),
                )
            ]
        return []

    def _cf0002_sg_open_ingress(
        self,
        res_name: str,
        res_type: str,
        properties: dict[str, Any],
        file_path: str,
    ) -> list[IaCFinding]:
        if res_type != "AWS::EC2::SecurityGroup":
            return []
        findings: list[IaCFinding] = []
        ingress_rules = properties.get("SecurityGroupIngress", [])
        if not isinstance(ingress_rules, list):
            return []
        for rule in ingress_rules:
            if not isinstance(rule, dict):
                continue
            cidr = rule.get("CidrIp", "")
            from_port = rule.get("FromPort", 0)
            to_port = rule.get("ToPort", 0)
            if cidr == "0.0.0.0/0":
                try:
                    port_val = int(from_port)
                except (ValueError, TypeError):
                    port_val = 0
                if port_val not in (80, 443):
                    findings.append(
                        IaCFinding(
                            rule_id="CF0002",
                            title="Security group ingress from 0.0.0.0/0",
                            description=(
                                f"Security group '{res_name}' allows ingress "
                                f"from 0.0.0.0/0 on port {from_port}."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.CLOUDFORMATION,
                            file_path=file_path,
                            line_number=0,
                            resource_type=res_type,
                            resource_name=res_name,
                            remediation=(
                                "Restrict CidrIp to specific IP ranges."
                            ),
                        )
                    )
        return findings

    def _cf0003_iam_wildcard(
        self,
        res_name: str,
        res_type: str,
        properties: dict[str, Any],
        file_path: str,
    ) -> list[IaCFinding]:
        if res_type not in (
            "AWS::IAM::Policy",
            "AWS::IAM::Role",
            "AWS::IAM::ManagedPolicy",
        ):
            return []
        findings: list[IaCFinding] = []
        policy_doc = properties.get("PolicyDocument", {})
        if not isinstance(policy_doc, dict):
            return []
        statements = policy_doc.get("Statement", [])
        if not isinstance(statements, list):
            return []
        for stmt in statements:
            if not isinstance(stmt, dict):
                continue
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            resources = stmt.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]
            if "*" in actions or "*" in resources:
                findings.append(
                    IaCFinding(
                        rule_id="CF0003",
                        title="IAM policy with wildcard actions",
                        description=(
                            f"IAM resource '{res_name}' uses wildcard (*) in "
                            "actions or resources."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.CLOUDFORMATION,
                        file_path=file_path,
                        line_number=0,
                        resource_type=res_type,
                        resource_name=res_name,
                        remediation=(
                            "Follow the principle of least privilege."
                        ),
                    )
                )
        return findings

    def _cf0004_rds_no_encryption(
        self,
        res_name: str,
        res_type: str,
        properties: dict[str, Any],
        file_path: str,
    ) -> list[IaCFinding]:
        if res_type != "AWS::RDS::DBInstance":
            return []
        if properties.get("StorageEncrypted") is not True:
            return [
                IaCFinding(
                    rule_id="CF0004",
                    title="RDS without encryption",
                    description=(
                        f"RDS instance '{res_name}' does not have "
                        "StorageEncrypted set to true."
                    ),
                    severity=IaCSeverity.HIGH,
                    platform=IaCPlatform.CLOUDFORMATION,
                    file_path=file_path,
                    line_number=0,
                    resource_type=res_type,
                    resource_name=res_name,
                    remediation="Set StorageEncrypted: true.",
                )
            ]
        return []

    def _cf0005_secrets_in_params(
        self, template: dict[str, Any], file_path: str
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        params = template.get("Parameters", {})
        if not isinstance(params, dict):
            return []
        secret_patterns = re.compile(r"(password|secret|key)", re.IGNORECASE)
        for param_name, param_def in params.items():
            if not isinstance(param_def, dict):
                continue
            if secret_patterns.search(param_name) and "Default" in param_def:
                findings.append(
                    IaCFinding(
                        rule_id="CF0005",
                        title="Secret in CloudFormation parameter default",
                        description=(
                            f"Parameter '{param_name}' has a default value "
                            "and its name suggests it contains a secret."
                        ),
                        severity=IaCSeverity.CRITICAL,
                        platform=IaCPlatform.CLOUDFORMATION,
                        file_path=file_path,
                        line_number=0,
                        resource_type="Parameter",
                        resource_name=param_name,
                        remediation=(
                            "Remove default values from secret parameters "
                            "and use NoEcho: true."
                        ),
                    )
                )
        return findings

    def _cf0006_lambda_no_vpc(
        self,
        res_name: str,
        res_type: str,
        properties: dict[str, Any],
        file_path: str,
    ) -> list[IaCFinding]:
        if res_type != "AWS::Lambda::Function":
            return []
        if "VpcConfig" not in properties:
            return [
                IaCFinding(
                    rule_id="CF0006",
                    title="Lambda function without VPC config",
                    description=(
                        f"Lambda function '{res_name}' is not configured "
                        "with a VPC."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.CLOUDFORMATION,
                    file_path=file_path,
                    line_number=0,
                    resource_type=res_type,
                    resource_name=res_name,
                    remediation="Add VpcConfig to run Lambda inside a VPC.",
                )
            ]
        return []

    def _cf0007_cloudfront_no_https(
        self,
        res_name: str,
        res_type: str,
        properties: dict[str, Any],
        file_path: str,
    ) -> list[IaCFinding]:
        if res_type != "AWS::CloudFront::Distribution":
            return []
        dist_config = properties.get("DistributionConfig", {})
        if not isinstance(dist_config, dict):
            return []
        default_cache = dist_config.get("DefaultCacheBehavior", {})
        if not isinstance(default_cache, dict):
            return []
        policy = default_cache.get("ViewerProtocolPolicy", "")
        if policy != "https-only":
            return [
                IaCFinding(
                    rule_id="CF0007",
                    title="CloudFront without HTTPS-only",
                    description=(
                        f"CloudFront distribution '{res_name}' does not enforce "
                        "HTTPS-only viewer protocol policy."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.CLOUDFORMATION,
                    file_path=file_path,
                    line_number=0,
                    resource_type=res_type,
                    resource_name=res_name,
                    remediation=(
                        "Set ViewerProtocolPolicy to https-only."
                    ),
                )
            ]
        return []

    def _cf0008_elb_no_access_logs(
        self,
        res_name: str,
        res_type: str,
        properties: dict[str, Any],
        file_path: str,
    ) -> list[IaCFinding]:
        if res_type not in (
            "AWS::ElasticLoadBalancing::LoadBalancer",
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
        ):
            return []
        has_logs = False
        access_logs = properties.get("AccessLoggingPolicy") or properties.get(
            "LoadBalancerAttributes", []
        )
        if isinstance(access_logs, dict) and access_logs:
            has_logs = True
        elif isinstance(access_logs, list):
            for attr in access_logs:
                if isinstance(attr, dict) and attr.get("Key") == "access_logs.s3.enabled":
                    if attr.get("Value") == "true":
                        has_logs = True
        if not has_logs:
            return [
                IaCFinding(
                    rule_id="CF0008",
                    title="ELB without access logs",
                    description=(
                        f"Load balancer '{res_name}' does not have access "
                        "logging enabled."
                    ),
                    severity=IaCSeverity.LOW,
                    platform=IaCPlatform.CLOUDFORMATION,
                    file_path=file_path,
                    line_number=0,
                    resource_type=res_type,
                    resource_name=res_name,
                    remediation="Enable access logging for the load balancer.",
                )
            ]
        return []

    # ------------------------------------------------------------------
    # Kubernetes scanning
    # ------------------------------------------------------------------

    def scan_kubernetes(self, path: Path) -> list[IaCFinding]:
        """Scan Kubernetes YAML files for security misconfigurations."""
        path = Path(path)
        findings: list[IaCFinding] = []
        k8s_files = self._find_kubernetes_files(path)

        for k8s_file in k8s_files:
            try:
                content = k8s_file.read_text(encoding="utf-8")
                docs = list(yaml.safe_load_all(content))
            except Exception:
                continue

            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                if "apiVersion" not in doc or "kind" not in doc:
                    continue
                findings.extend(self._k8s_scan_manifest(doc, str(k8s_file)))

        return findings

    def _find_kubernetes_files(self, path: Path) -> list[Path]:
        """Find Kubernetes manifest files (excluding Helm charts)."""
        k8s_files: list[Path] = []
        helm_dirs = {str(d) for d in self._find_helm_charts(path)}

        for ext in ("*.yaml", "*.yml"):
            for f in path.rglob(ext):
                # Skip files inside Helm chart directories
                if any(str(f).startswith(hd) for hd in helm_dirs):
                    continue
                try:
                    content = f.read_text(encoding="utf-8")
                    docs = list(yaml.safe_load_all(content))
                    for doc in docs:
                        if (
                            isinstance(doc, dict)
                            and "apiVersion" in doc
                            and "kind" in doc
                        ):
                            k8s_files.append(f)
                            break
                except Exception:
                    continue
        return k8s_files

    def _k8s_scan_manifest(
        self, doc: dict[str, Any], file_path: str
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []
        kind = doc.get("kind", "")
        metadata = doc.get("metadata", {}) or {}
        resource_name = metadata.get("name", "unknown")

        # Get pod spec and containers depending on kind
        containers: list[dict[str, Any]] = []
        pod_spec: dict[str, Any] = {}

        if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"):
            spec = doc.get("spec", {}) or {}
            template = spec.get("template", {}) or {}
            pod_spec = template.get("spec", {}) or {}
            containers = pod_spec.get("containers", []) or []
        elif kind == "Pod":
            pod_spec = doc.get("spec", {}) or {}
            containers = pod_spec.get("containers", []) or []
        elif kind == "CronJob":
            spec = doc.get("spec", {}) or {}
            job_template = spec.get("jobTemplate", {}) or {}
            job_spec = job_template.get("spec", {}) or {}
            template = job_spec.get("template", {}) or {}
            pod_spec = template.get("spec", {}) or {}
            containers = pod_spec.get("containers", []) or []
        else:
            # Handle non-workload resources
            findings.extend(
                self._k8s_scan_other_resources(doc, kind, metadata, resource_name, file_path)
            )
            return findings

        for container in containers:
            if not isinstance(container, dict):
                continue
            container_name = container.get("name", "unknown")
            sec_ctx = container.get("securityContext", {}) or {}

            # K80001 — Running as root
            if sec_ctx.get("runAsNonRoot") is not True:
                findings.append(
                    IaCFinding(
                        rule_id="K80001",
                        title="Container may run as root",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' does not set "
                            "runAsNonRoot: true."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Set securityContext.runAsNonRoot: true."
                        ),
                    )
                )

            # K80002 — Privileged container
            if sec_ctx.get("privileged") is True:
                findings.append(
                    IaCFinding(
                        rule_id="K80002",
                        title="Privileged container",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' runs in privileged mode."
                        ),
                        severity=IaCSeverity.CRITICAL,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Remove privileged: true from securityContext."
                        ),
                    )
                )

            # K80003 — No resource limits
            resources = container.get("resources", {}) or {}
            if "limits" not in resources:
                findings.append(
                    IaCFinding(
                        rule_id="K80003",
                        title="No resource limits",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' has no resource limits."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set resources.limits for CPU and memory.",
                    )
                )

            # K80004 — Using latest tag
            image = container.get("image", "")
            if image:
                if ":" not in image or image.endswith(":latest"):
                    findings.append(
                        IaCFinding(
                            rule_id="K80004",
                            title="Using latest image tag",
                            description=(
                                f"Container '{container_name}' in {kind} "
                                f"'{resource_name}' uses 'latest' or "
                                "untagged image."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation=(
                                "Use a specific image tag or digest."
                            ),
                        )
                    )

            # K80007 — Writable root filesystem
            if sec_ctx.get("readOnlyRootFilesystem") is not True:
                findings.append(
                    IaCFinding(
                        rule_id="K80007",
                        title="Writable root filesystem",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' does not set "
                            "readOnlyRootFilesystem: true."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Set securityContext.readOnlyRootFilesystem: true."
                        ),
                    )
                )

            # K80008 — All capabilities
            caps = sec_ctx.get("capabilities", {}) or {}
            add_caps = caps.get("add", []) or []
            if "ALL" in add_caps:
                findings.append(
                    IaCFinding(
                        rule_id="K80008",
                        title="All capabilities added",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' adds ALL capabilities."
                        ),
                        severity=IaCSeverity.CRITICAL,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Drop ALL capabilities and add only required ones."
                        ),
                    )
                )

            # K80009 — No probes
            if "livenessProbe" not in container and "readinessProbe" not in container:
                findings.append(
                    IaCFinding(
                        rule_id="K80009",
                        title="No liveness/readiness probes",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' has no liveness or "
                            "readiness probes."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Add livenessProbe and readinessProbe."
                        ),
                    )
                )

            # K80011 — Secrets in env
            env_vars = container.get("env", []) or []
            for env_var in env_vars:
                if not isinstance(env_var, dict):
                    continue
                env_name = env_var.get("name", "").lower()
                env_value = env_var.get("value")
                if env_value and isinstance(env_value, str):
                    if any(
                        kw in env_name
                        for kw in ("password", "secret", "token", "key", "api_key")
                    ):
                        findings.append(
                            IaCFinding(
                                rule_id="K80011",
                                title="Secret in environment variable",
                                description=(
                                    f"Container '{container_name}' in {kind} "
                                    f"'{resource_name}' has a plain-text secret "
                                    f"in env var '{env_var.get('name', '')}'."
                                ),
                                severity=IaCSeverity.CRITICAL,
                                platform=IaCPlatform.KUBERNETES,
                                file_path=file_path,
                                line_number=0,
                                resource_type=kind,
                                resource_name=resource_name,
                                remediation=(
                                    "Use Kubernetes Secrets or external secret "
                                    "management instead of plain-text env values."
                                ),
                            )
                        )

            # K80012 — Allow privilege escalation
            if sec_ctx.get("allowPrivilegeEscalation") is not False:
                findings.append(
                    IaCFinding(
                        rule_id="K80012",
                        title="Allow privilege escalation",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' does not set "
                            "allowPrivilegeEscalation: false."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Set securityContext.allowPrivilegeEscalation: false."
                        ),
                    )
                )

        # K80005 — Host network
        if pod_spec.get("hostNetwork") is True:
            findings.append(
                IaCFinding(
                    rule_id="K80005",
                    title="Host network enabled",
                    description=(
                        f"{kind} '{resource_name}' uses the host network."
                    ),
                    severity=IaCSeverity.HIGH,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Remove hostNetwork: true.",
                )
            )

        # K80006 — Host PID
        if pod_spec.get("hostPID") is True:
            findings.append(
                IaCFinding(
                    rule_id="K80006",
                    title="Host PID enabled",
                    description=(
                        f"{kind} '{resource_name}' uses the host PID namespace."
                    ),
                    severity=IaCSeverity.HIGH,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Remove hostPID: true.",
                )
            )

        # K80010 — Default namespace
        namespace = metadata.get("namespace", "")
        if namespace == "default" or namespace == "":
            findings.append(
                IaCFinding(
                    rule_id="K80010",
                    title="Default namespace used",
                    description=(
                        f"{kind} '{resource_name}' is deployed to the "
                        "default namespace."
                    ),
                    severity=IaCSeverity.INFO,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation=(
                        "Use a dedicated namespace for your workloads."
                    ),
                )
            )

        # K80013 — Host IPC namespace
        if pod_spec.get("hostIPC") is True:
            findings.append(
                IaCFinding(
                    rule_id="K80013",
                    title="Host IPC namespace enabled",
                    description=(
                        f"{kind} '{resource_name}' uses the host IPC namespace."
                    ),
                    severity=IaCSeverity.HIGH,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Remove hostIPC: true.",
                )
            )

        # K80014 — Service account token auto-mounted
        if pod_spec.get("automountServiceAccountToken") is not False:
            findings.append(
                IaCFinding(
                    rule_id="K80014",
                    title="Service account token auto-mounted",
                    description=(
                        f"{kind} '{resource_name}' auto-mounts the service "
                        "account token."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation=(
                        "Set automountServiceAccountToken: false unless needed."
                    ),
                )
            )

        # K80015 — Default service account used
        sa = pod_spec.get("serviceAccountName", "default")
        if sa == "default" or sa == "":
            findings.append(
                IaCFinding(
                    rule_id="K80015",
                    title="Default service account used",
                    description=(
                        f"{kind} '{resource_name}' uses the default service account."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Create and use a dedicated service account.",
                )
            )

        # K80016 — Sharing process namespace
        if pod_spec.get("shareProcessNamespace") is True:
            findings.append(
                IaCFinding(
                    rule_id="K80016",
                    title="Process namespace sharing enabled",
                    description=(
                        f"{kind} '{resource_name}' shares process namespace "
                        "between containers."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Remove shareProcessNamespace: true unless necessary.",
                )
            )

        # K80017 — Host path volume mounted
        volumes = pod_spec.get("volumes", []) or []
        for vol in volumes:
            if not isinstance(vol, dict):
                continue
            vol_name = vol.get("name", "unknown")
            if "hostPath" in vol:
                host_path = vol.get("hostPath", {})
                path = host_path.get("path", "") if isinstance(host_path, dict) else ""
                findings.append(
                    IaCFinding(
                        rule_id="K80017",
                        title="Host path volume mounted",
                        description=(
                            f"{kind} '{resource_name}' mounts host path "
                            f"'{path}' via volume '{vol_name}'."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use PersistentVolumes instead of hostPath.",
                    )
                )
            # K80018 — Docker socket mounted
            if "hostPath" in vol:
                host_path = vol.get("hostPath", {})
                path = host_path.get("path", "") if isinstance(host_path, dict) else ""
                if path in ("/var/run/docker.sock", "/run/docker.sock"):
                    findings.append(
                        IaCFinding(
                            rule_id="K80018",
                            title="Docker socket mounted",
                            description=(
                                f"{kind} '{resource_name}' mounts the Docker socket."
                            ),
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation=(
                                "Do not mount the Docker socket; use kaniko or "
                                "buildah for container builds."
                            ),
                        )
                    )
            # K80019 — emptyDir with memory medium
            if "emptyDir" in vol:
                empty_dir = vol.get("emptyDir", {})
                medium = empty_dir.get("medium", "") if isinstance(empty_dir, dict) else ""
                size_limit = empty_dir.get("sizeLimit") if isinstance(empty_dir, dict) else None
                if medium == "Memory" and not size_limit:
                    findings.append(
                        IaCFinding(
                            rule_id="K80019",
                            title="Memory-backed emptyDir without size limit",
                            description=(
                                f"{kind} '{resource_name}' uses memory-backed emptyDir "
                                f"'{vol_name}' without a sizeLimit."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Set a sizeLimit on memory-backed emptyDir volumes.",
                        )
                    )

        # Additional container-level checks
        for container in containers:
            if not isinstance(container, dict):
                continue
            container_name = container.get("name", "unknown")
            sec_ctx = container.get("securityContext", {}) or {}
            resources = container.get("resources", {}) or {}

            # K80020 — Missing memory requests
            requests = resources.get("requests", {}) or {}
            if "memory" not in requests:
                findings.append(
                    IaCFinding(
                        rule_id="K80020",
                        title="Missing memory requests",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' has no memory requests."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set resources.requests.memory.",
                    )
                )

            # K80021 — Missing CPU requests
            if "cpu" not in requests:
                findings.append(
                    IaCFinding(
                        rule_id="K80021",
                        title="Missing CPU requests",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' has no CPU requests."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set resources.requests.cpu.",
                    )
                )

            # K80022 — Container exposes host port
            ports = container.get("ports", []) or []
            for port in ports:
                if isinstance(port, dict) and port.get("hostPort"):
                    findings.append(
                        IaCFinding(
                            rule_id="K80022",
                            title="Container exposes host port",
                            description=(
                                f"Container '{container_name}' in {kind} "
                                f"'{resource_name}' exposes host port "
                                f"{port.get('hostPort')}."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Use Services instead of hostPort.",
                        )
                    )

            # K80023 — Dangerous capabilities added
            caps = sec_ctx.get("capabilities", {}) or {}
            add_caps = caps.get("add", []) or []
            dangerous_caps = {
                "SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "SYS_RAWIO",
                "SYS_MODULE", "DAC_OVERRIDE", "SETUID", "SETGID",
            }
            added_dangerous = set(add_caps) & dangerous_caps
            if added_dangerous:
                findings.append(
                    IaCFinding(
                        rule_id="K80023",
                        title="Dangerous capabilities added",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' adds dangerous capabilities: "
                            f"{', '.join(added_dangerous)}."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Remove dangerous capabilities.",
                    )
                )

            # K80024 — Capabilities not dropped
            drop_caps = caps.get("drop", []) or []
            if "ALL" not in drop_caps and not drop_caps:
                findings.append(
                    IaCFinding(
                        rule_id="K80024",
                        title="Capabilities not dropped",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' does not drop any capabilities."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Drop ALL capabilities and add only required ones."
                        ),
                    )
                )

            # K80025 — NET_RAW capability (default)
            if "ALL" not in drop_caps and "NET_RAW" not in drop_caps:
                findings.append(
                    IaCFinding(
                        rule_id="K80025",
                        title="NET_RAW capability not dropped",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' retains NET_RAW capability."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Drop NET_RAW capability.",
                    )
                )

            # K80026 — No seccomp profile
            seccomp = sec_ctx.get("seccompProfile", {})
            if not seccomp:
                findings.append(
                    IaCFinding(
                        rule_id="K80026",
                        title="No seccomp profile",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' has no seccomp profile set."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation=(
                            "Set seccompProfile.type to RuntimeDefault or Localhost."
                        ),
                    )
                )

            # K80027 — Image pull policy not Always
            image_pull_policy = container.get("imagePullPolicy", "")
            image = container.get("image", "")
            if image_pull_policy != "Always" and (":latest" in image or ":" not in image):
                findings.append(
                    IaCFinding(
                        rule_id="K80027",
                        title="Image pull policy not Always for latest tag",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' uses latest tag without "
                            "imagePullPolicy: Always."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set imagePullPolicy: Always for latest tags.",
                    )
                )

            # K80028 — Proc mount type Unmasked
            if sec_ctx.get("procMount") == "Unmasked":
                findings.append(
                    IaCFinding(
                        rule_id="K80028",
                        title="Unmasked proc mount",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' has procMount set to Unmasked."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Remove procMount: Unmasked.",
                    )
                )

            # K80029 — No startup probe
            if kind in ("Deployment", "StatefulSet", "DaemonSet"):
                if "startupProbe" not in container:
                    findings.append(
                        IaCFinding(
                            rule_id="K80029",
                            title="No startup probe",
                            description=(
                                f"Container '{container_name}' in {kind} "
                                f"'{resource_name}' has no startup probe."
                            ),
                            severity=IaCSeverity.INFO,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Add startupProbe for slow-starting containers.",
                        )
                    )

            # K80030 — runAsUser set to 0 (root)
            run_as_user = sec_ctx.get("runAsUser")
            if run_as_user == 0:
                findings.append(
                    IaCFinding(
                        rule_id="K80030",
                        title="Container runs as root user",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' explicitly runs as root (UID 0)."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set runAsUser to a non-root UID (e.g., 1000).",
                    )
                )

            # K80031 — runAsGroup not set
            if "runAsGroup" not in sec_ctx:
                findings.append(
                    IaCFinding(
                        rule_id="K80031",
                        title="runAsGroup not set",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' does not set runAsGroup."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set runAsGroup to a non-root GID.",
                    )
                )

            # K80032 — Mounting sensitive paths
            volume_mounts = container.get("volumeMounts", []) or []
            sensitive_paths = {
                "/etc/shadow", "/etc/passwd", "/etc/kubernetes",
                "/root", "/var/log", "/proc", "/sys",
            }
            for vm in volume_mounts:
                if isinstance(vm, dict):
                    mount_path = vm.get("mountPath", "")
                    for sens in sensitive_paths:
                        if mount_path.startswith(sens):
                            findings.append(
                                IaCFinding(
                                    rule_id="K80032",
                                    title="Sensitive path mounted",
                                    description=(
                                        f"Container '{container_name}' in {kind} "
                                        f"'{resource_name}' mounts sensitive path "
                                        f"'{mount_path}'."
                                    ),
                                    severity=IaCSeverity.HIGH,
                                    platform=IaCPlatform.KUBERNETES,
                                    file_path=file_path,
                                    line_number=0,
                                    resource_type=kind,
                                    resource_name=resource_name,
                                    remediation="Avoid mounting sensitive host paths.",
                                )
                            )
                            break

            # K80033 — Environment variable from secret without key
            env_from = container.get("envFrom", []) or []
            for ef in env_from:
                if isinstance(ef, dict) and "secretRef" in ef:
                    findings.append(
                        IaCFinding(
                            rule_id="K80033",
                            title="All secret keys exposed as env vars",
                            description=(
                                f"Container '{container_name}' in {kind} "
                                f"'{resource_name}' exposes all keys from a secret."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Use specific secret keys instead of secretRef.",
                        )
                    )

            # K80034 — TTY or stdin enabled
            if container.get("tty") is True or container.get("stdin") is True:
                findings.append(
                    IaCFinding(
                        rule_id="K80034",
                        title="TTY or stdin enabled",
                        description=(
                            f"Container '{container_name}' in {kind} "
                            f"'{resource_name}' has tty or stdin enabled."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Disable tty and stdin unless required for debugging.",
                    )
                )

        # K80035 — Deployment with replica count of 1
        if kind == "Deployment":
            spec = doc.get("spec", {}) or {}
            replicas = spec.get("replicas", 1)
            if replicas == 1:
                findings.append(
                    IaCFinding(
                        rule_id="K80035",
                        title="Single replica deployment",
                        description=(
                            f"Deployment '{resource_name}' has only 1 replica."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use at least 2 replicas for high availability.",
                    )
                )

        # K80036 — No pod anti-affinity for Deployment/StatefulSet
        if kind in ("Deployment", "StatefulSet"):
            spec = doc.get("spec", {}) or {}
            template = spec.get("template", {}) or {}
            template_spec = template.get("spec", {}) or {}
            affinity = template_spec.get("affinity", {}) or {}
            if "podAntiAffinity" not in affinity:
                findings.append(
                    IaCFinding(
                        rule_id="K80036",
                        title="No pod anti-affinity",
                        description=(
                            f"{kind} '{resource_name}' has no pod anti-affinity rules."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add podAntiAffinity to spread pods across nodes.",
                    )
                )

        # K80037 — No topology spread constraints
        if kind in ("Deployment", "StatefulSet", "DaemonSet"):
            if not pod_spec.get("topologySpreadConstraints"):
                findings.append(
                    IaCFinding(
                        rule_id="K80037",
                        title="No topology spread constraints",
                        description=(
                            f"{kind} '{resource_name}' has no topology spread constraints."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add topologySpreadConstraints for even distribution.",
                    )
                )

        # K80038 — No priority class
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "Pod"):
            if not pod_spec.get("priorityClassName"):
                findings.append(
                    IaCFinding(
                        rule_id="K80038",
                        title="No priority class set",
                        description=(
                            f"{kind} '{resource_name}' has no priorityClassName."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set priorityClassName for scheduling priority.",
                    )
                )

        # K80039 — DNS policy not ClusterFirst
        dns_policy = pod_spec.get("dnsPolicy", "ClusterFirst")
        if dns_policy == "Default":
            findings.append(
                IaCFinding(
                    rule_id="K80039",
                    title="DNS policy set to Default",
                    description=(
                        f"{kind} '{resource_name}' uses host DNS policy."
                    ),
                    severity=IaCSeverity.LOW,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Use ClusterFirst or ClusterFirstWithHostNet.",
                )
            )

        # K80040 — Termination grace period too long
        grace_period = pod_spec.get("terminationGracePeriodSeconds", 30)
        if isinstance(grace_period, int) and grace_period > 300:
            findings.append(
                IaCFinding(
                    rule_id="K80040",
                    title="Long termination grace period",
                    description=(
                        f"{kind} '{resource_name}' has terminationGracePeriodSeconds "
                        f"set to {grace_period}s (> 300s)."
                    ),
                    severity=IaCSeverity.LOW,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Consider reducing terminationGracePeriodSeconds.",
                )
            )

        return findings

    def _k8s_scan_other_resources(
        self,
        doc: dict[str, Any],
        kind: str,
        metadata: dict[str, Any],
        resource_name: str,
        file_path: str,
    ) -> list[IaCFinding]:
        """Scan non-workload Kubernetes resources."""
        findings: list[IaCFinding] = []
        spec = doc.get("spec", {}) or {}

        # ----------------------------------------------------------------
        # Ingress rules (K80041-K80046)
        # ----------------------------------------------------------------
        if kind == "Ingress":
            # K80041 — Ingress without TLS
            tls = spec.get("tls", [])
            if not tls:
                findings.append(
                    IaCFinding(
                        rule_id="K80041",
                        title="Ingress without TLS",
                        description=(
                            f"Ingress '{resource_name}' does not have TLS configured."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add TLS configuration with a valid certificate.",
                    )
                )

            # K80042 — Ingress allows HTTP
            annotations = metadata.get("annotations", {}) or {}
            ssl_redirect = annotations.get("nginx.ingress.kubernetes.io/ssl-redirect", "")
            force_ssl = annotations.get("nginx.ingress.kubernetes.io/force-ssl-redirect", "")
            if ssl_redirect == "false" or (tls and not force_ssl):
                findings.append(
                    IaCFinding(
                        rule_id="K80042",
                        title="Ingress allows HTTP traffic",
                        description=(
                            f"Ingress '{resource_name}' may allow unencrypted HTTP traffic."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set nginx.ingress.kubernetes.io/ssl-redirect: 'true'.",
                    )
                )

            # K80043 — Ingress with wildcard host
            rules = spec.get("rules", []) or []
            for rule in rules:
                if isinstance(rule, dict):
                    host = rule.get("host", "")
                    if host == "*" or host.startswith("*."):
                        findings.append(
                            IaCFinding(
                                rule_id="K80043",
                                title="Ingress with wildcard host",
                                description=(
                                    f"Ingress '{resource_name}' uses wildcard host '{host}'."
                                ),
                                severity=IaCSeverity.MEDIUM,
                                platform=IaCPlatform.KUBERNETES,
                                file_path=file_path,
                                line_number=0,
                                resource_type=kind,
                                resource_name=resource_name,
                                remediation="Use specific host names instead of wildcards.",
                            )
                        )

            # K80044 — Ingress without rate limiting
            rate_limit = annotations.get("nginx.ingress.kubernetes.io/limit-rps", "")
            if not rate_limit:
                findings.append(
                    IaCFinding(
                        rule_id="K80044",
                        title="Ingress without rate limiting",
                        description=(
                            f"Ingress '{resource_name}' has no rate limiting configured."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add rate limiting annotations for DDoS protection.",
                    )
                )

            # K80045 — Ingress without WAF
            modsecurity = annotations.get("nginx.ingress.kubernetes.io/enable-modsecurity", "")
            waf = annotations.get("nginx.ingress.kubernetes.io/enable-owasp-core-rules", "")
            if not modsecurity and not waf:
                findings.append(
                    IaCFinding(
                        rule_id="K80045",
                        title="Ingress without WAF",
                        description=(
                            f"Ingress '{resource_name}' has no WAF/ModSecurity enabled."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Enable ModSecurity or OWASP core rules.",
                    )
                )

            # K80046 — Ingress without snippet annotations protection
            server_snippet = annotations.get("nginx.ingress.kubernetes.io/server-snippet", "")
            config_snippet = annotations.get("nginx.ingress.kubernetes.io/configuration-snippet", "")
            if server_snippet or config_snippet:
                findings.append(
                    IaCFinding(
                        rule_id="K80046",
                        title="Ingress uses configuration snippets",
                        description=(
                            f"Ingress '{resource_name}' uses configuration snippets "
                            "which can be a security risk."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Avoid using snippets; use dedicated annotations instead.",
                    )
                )

        # ----------------------------------------------------------------
        # Service rules (K80047-K80052)
        # ----------------------------------------------------------------
        elif kind == "Service":
            service_type = spec.get("type", "ClusterIP")

            # K80047 — Service type LoadBalancer
            if service_type == "LoadBalancer":
                findings.append(
                    IaCFinding(
                        rule_id="K80047",
                        title="Service type LoadBalancer",
                        description=(
                            f"Service '{resource_name}' is type LoadBalancer, "
                            "exposing it externally."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use Ingress with ClusterIP for external access.",
                    )
                )

            # K80048 — Service type NodePort
            if service_type == "NodePort":
                findings.append(
                    IaCFinding(
                        rule_id="K80048",
                        title="Service type NodePort",
                        description=(
                            f"Service '{resource_name}' is type NodePort, "
                            "exposing a port on all nodes."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use Ingress with ClusterIP for external access.",
                    )
                )

            # K80049 — Service with externalIPs
            external_ips = spec.get("externalIPs", [])
            if external_ips:
                findings.append(
                    IaCFinding(
                        rule_id="K80049",
                        title="Service with externalIPs",
                        description=(
                            f"Service '{resource_name}' uses externalIPs which can "
                            "be a security risk."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use LoadBalancer or Ingress instead of externalIPs.",
                    )
                )

            # K80050 — Service without selector
            selector = spec.get("selector", {})
            if not selector and service_type != "ExternalName":
                findings.append(
                    IaCFinding(
                        rule_id="K80050",
                        title="Service without selector",
                        description=(
                            f"Service '{resource_name}' has no pod selector."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add a selector or ensure this is intentional.",
                    )
                )

            # K80051 — Service exposes sensitive port
            sensitive_ports = {22, 23, 3389, 5432, 3306, 27017, 6379, 9200, 11211}
            ports = spec.get("ports", []) or []
            for port_def in ports:
                if isinstance(port_def, dict):
                    port = port_def.get("port")
                    if port in sensitive_ports:
                        findings.append(
                            IaCFinding(
                                rule_id="K80051",
                                title="Service exposes sensitive port",
                                description=(
                                    f"Service '{resource_name}' exposes port {port} "
                                    "which is commonly used by sensitive services."
                                ),
                                severity=IaCSeverity.MEDIUM,
                                platform=IaCPlatform.KUBERNETES,
                                file_path=file_path,
                                line_number=0,
                                resource_type=kind,
                                resource_name=resource_name,
                                remediation="Ensure this service is not publicly accessible.",
                            )
                        )

            # K80052 — Service uses session affinity None
            session_affinity = spec.get("sessionAffinity", "None")
            if session_affinity == "None" and service_type in ("LoadBalancer", "NodePort"):
                findings.append(
                    IaCFinding(
                        rule_id="K80052",
                        title="External service without session affinity",
                        description=(
                            f"Service '{resource_name}' is externally accessible "
                            "without session affinity."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Consider enabling sessionAffinity: ClientIP.",
                    )
                )

        # ----------------------------------------------------------------
        # NetworkPolicy rules (K80053-K80057)
        # ----------------------------------------------------------------
        elif kind == "NetworkPolicy":
            policy_types = spec.get("policyTypes", []) or []
            ingress_rules = spec.get("ingress", []) or []
            egress_rules = spec.get("egress", []) or []

            # K80053 — NetworkPolicy without egress rules
            if "Egress" not in policy_types:
                findings.append(
                    IaCFinding(
                        rule_id="K80053",
                        title="NetworkPolicy without egress restrictions",
                        description=(
                            f"NetworkPolicy '{resource_name}' does not restrict egress traffic."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add Egress to policyTypes and define egress rules.",
                    )
                )

            # K80054 — NetworkPolicy allows all ingress
            for rule in ingress_rules:
                if isinstance(rule, dict) and not rule:
                    findings.append(
                        IaCFinding(
                            rule_id="K80054",
                            title="NetworkPolicy allows all ingress",
                            description=(
                                f"NetworkPolicy '{resource_name}' has an empty ingress "
                                "rule allowing all traffic."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Define specific ingress rules.",
                        )
                    )

            # K80055 — NetworkPolicy allows all egress
            for rule in egress_rules:
                if isinstance(rule, dict) and not rule:
                    findings.append(
                        IaCFinding(
                            rule_id="K80055",
                            title="NetworkPolicy allows all egress",
                            description=(
                                f"NetworkPolicy '{resource_name}' has an empty egress "
                                "rule allowing all traffic."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Define specific egress rules.",
                        )
                    )

            # K80056 — NetworkPolicy with wide IP block
            for rule in ingress_rules + egress_rules:
                if isinstance(rule, dict):
                    from_or_to = rule.get("from", []) or rule.get("to", []) or []
                    for selector in from_or_to:
                        if isinstance(selector, dict):
                            ip_block = selector.get("ipBlock", {})
                            if isinstance(ip_block, dict):
                                cidr = ip_block.get("cidr", "")
                                if cidr in ("0.0.0.0/0", "::/0"):
                                    findings.append(
                                        IaCFinding(
                                            rule_id="K80056",
                                            title="NetworkPolicy allows all IPs",
                                            description=(
                                                f"NetworkPolicy '{resource_name}' allows "
                                                f"traffic from/to {cidr}."
                                            ),
                                            severity=IaCSeverity.HIGH,
                                            platform=IaCPlatform.KUBERNETES,
                                            file_path=file_path,
                                            line_number=0,
                                            resource_type=kind,
                                            resource_name=resource_name,
                                            remediation="Restrict CIDR to specific IP ranges.",
                                        )
                                    )

            # K80057 — NetworkPolicy without pod selector
            pod_selector = spec.get("podSelector", {})
            if isinstance(pod_selector, dict) and not pod_selector:
                findings.append(
                    IaCFinding(
                        rule_id="K80057",
                        title="NetworkPolicy applies to all pods",
                        description=(
                            f"NetworkPolicy '{resource_name}' has empty podSelector, "
                            "applying to all pods in namespace."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Ensure this is intentional for default deny policies.",
                    )
                )

        # ----------------------------------------------------------------
        # RBAC rules (K80058-K80070)
        # ----------------------------------------------------------------
        elif kind in ("ClusterRole", "Role"):
            rules = spec.get("rules", []) or []

            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                verbs = rule.get("verbs", []) or []
                api_groups = rule.get("apiGroups", []) or []
                resources = rule.get("resources", []) or []

                # K80058 — Wildcard verbs
                if "*" in verbs:
                    findings.append(
                        IaCFinding(
                            rule_id="K80058",
                            title="RBAC role with wildcard verbs",
                            description=(
                                f"{kind} '{resource_name}' grants all verbs (*)."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Specify explicit verbs instead of wildcard.",
                        )
                    )

                # K80059 — Wildcard resources
                if "*" in resources:
                    findings.append(
                        IaCFinding(
                            rule_id="K80059",
                            title="RBAC role with wildcard resources",
                            description=(
                                f"{kind} '{resource_name}' grants access to all resources (*)."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Specify explicit resources instead of wildcard.",
                        )
                    )

                # K80060 — Wildcard API groups
                if "*" in api_groups:
                    findings.append(
                        IaCFinding(
                            rule_id="K80060",
                            title="RBAC role with wildcard API groups",
                            description=(
                                f"{kind} '{resource_name}' grants access to all API groups (*)."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Specify explicit API groups instead of wildcard.",
                        )
                    )

                # K80061 — Access to secrets
                if "secrets" in resources and any(
                    v in verbs for v in ("*", "get", "list", "watch")
                ):
                    findings.append(
                        IaCFinding(
                            rule_id="K80061",
                            title="RBAC role can read secrets",
                            description=(
                                f"{kind} '{resource_name}' can read secrets."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Limit secret access to specific secrets if possible.",
                        )
                    )

                # K80062 — Exec into pods
                if "pods/exec" in resources or (
                    "pods" in resources and "create" in verbs
                ):
                    findings.append(
                        IaCFinding(
                            rule_id="K80062",
                            title="RBAC role can exec into pods",
                            description=(
                                f"{kind} '{resource_name}' allows executing commands in pods."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Restrict pod/exec access to administrators only.",
                        )
                    )

                # K80063 — Can create/modify roles
                if any(r in resources for r in ("roles", "clusterroles")) and any(
                    v in verbs for v in ("*", "create", "update", "patch")
                ):
                    findings.append(
                        IaCFinding(
                            rule_id="K80063",
                            title="RBAC role can escalate privileges",
                            description=(
                                f"{kind} '{resource_name}' can create or modify roles."
                            ),
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Restrict role modification to cluster administrators.",
                        )
                    )

                # K80064 — Can create service accounts
                if "serviceaccounts" in resources and any(
                    v in verbs for v in ("*", "create", "update", "patch")
                ):
                    findings.append(
                        IaCFinding(
                            rule_id="K80064",
                            title="RBAC role can create service accounts",
                            description=(
                                f"{kind} '{resource_name}' can create or modify service accounts."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Limit service account management permissions.",
                        )
                    )

                # K80065 — Can delete resources
                if "delete" in verbs or "deletecollection" in verbs:
                    findings.append(
                        IaCFinding(
                            rule_id="K80065",
                            title="RBAC role has delete permissions",
                            description=(
                                f"{kind} '{resource_name}' can delete resources."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Review if delete permissions are necessary.",
                        )
                    )

                # K80066 — Can impersonate users
                if "impersonate" in verbs:
                    findings.append(
                        IaCFinding(
                            rule_id="K80066",
                            title="RBAC role can impersonate",
                            description=(
                                f"{kind} '{resource_name}' can impersonate users or groups."
                            ),
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Remove impersonate permission unless absolutely required.",
                        )
                    )

                # K80067 — Access to persistent volumes
                if "persistentvolumes" in resources and any(
                    v in verbs for v in ("*", "create", "update", "patch", "delete")
                ):
                    findings.append(
                        IaCFinding(
                            rule_id="K80067",
                            title="RBAC role can modify persistent volumes",
                            description=(
                                f"{kind} '{resource_name}' can modify persistent volumes."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Limit PV management to storage administrators.",
                        )
                    )

        # K80068-K80070 — ClusterRoleBinding / RoleBinding rules
        elif kind in ("ClusterRoleBinding", "RoleBinding"):
            role_ref = doc.get("roleRef", {}) or {}
            role_name = role_ref.get("name", "")
            subjects = doc.get("subjects", []) or []

            # K80068 — Binding to cluster-admin
            if role_name == "cluster-admin":
                findings.append(
                    IaCFinding(
                        rule_id="K80068",
                        title="Binding to cluster-admin role",
                        description=(
                            f"{kind} '{resource_name}' binds to the cluster-admin role."
                        ),
                        severity=IaCSeverity.CRITICAL,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Create a custom role with minimal required permissions.",
                    )
                )

            # K80069 — Binding to system namespace service accounts
            for subject in subjects:
                if isinstance(subject, dict):
                    subj_kind = subject.get("kind", "")
                    subj_ns = subject.get("namespace", "")
                    if subj_kind == "ServiceAccount" and subj_ns in (
                        "kube-system", "kube-public", "default"
                    ):
                        findings.append(
                            IaCFinding(
                                rule_id="K80069",
                                title="Binding to system namespace service account",
                                description=(
                                    f"{kind} '{resource_name}' binds to service account "
                                    f"in '{subj_ns}' namespace."
                                ),
                                severity=IaCSeverity.MEDIUM,
                                platform=IaCPlatform.KUBERNETES,
                                file_path=file_path,
                                line_number=0,
                                resource_type=kind,
                                resource_name=resource_name,
                                remediation="Use dedicated namespaces for application service accounts.",
                            )
                        )

            # K80070 — Binding to group system:authenticated
            for subject in subjects:
                if isinstance(subject, dict):
                    subj_kind = subject.get("kind", "")
                    subj_name = subject.get("name", "")
                    if subj_kind == "Group" and subj_name in (
                        "system:authenticated", "system:unauthenticated"
                    ):
                        findings.append(
                            IaCFinding(
                                rule_id="K80070",
                                title="Binding to broad system group",
                                description=(
                                    f"{kind} '{resource_name}' binds to '{subj_name}', "
                                    "granting permissions to all authenticated/unauthenticated users."
                                ),
                                severity=IaCSeverity.CRITICAL,
                                platform=IaCPlatform.KUBERNETES,
                                file_path=file_path,
                                line_number=0,
                                resource_type=kind,
                                resource_name=resource_name,
                                remediation="Bind to specific users or groups instead.",
                            )
                        )

        # ----------------------------------------------------------------
        # ServiceAccount rules (K80071-K80073)
        # ----------------------------------------------------------------
        elif kind == "ServiceAccount":
            automount = doc.get("automountServiceAccountToken")
            secrets = doc.get("secrets", []) or []
            image_pull_secrets = doc.get("imagePullSecrets", []) or []

            # K80071 — ServiceAccount automounts token
            if automount is not False:
                findings.append(
                    IaCFinding(
                        rule_id="K80071",
                        title="ServiceAccount automounts token",
                        description=(
                            f"ServiceAccount '{resource_name}' automounts its token."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set automountServiceAccountToken: false if not needed.",
                    )
                )

            # K80072 — ServiceAccount in default namespace
            namespace = metadata.get("namespace", "default")
            if namespace == "default":
                findings.append(
                    IaCFinding(
                        rule_id="K80072",
                        title="ServiceAccount in default namespace",
                        description=(
                            f"ServiceAccount '{resource_name}' is in the default namespace."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Create ServiceAccounts in dedicated namespaces.",
                    )
                )

            # K80073 — ServiceAccount without imagePullSecrets
            if not image_pull_secrets:
                findings.append(
                    IaCFinding(
                        rule_id="K80073",
                        title="ServiceAccount without imagePullSecrets",
                        description=(
                            f"ServiceAccount '{resource_name}' has no imagePullSecrets."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add imagePullSecrets for private registries.",
                    )
                )

        # ----------------------------------------------------------------
        # Secret rules (K80074-K80077)
        # ----------------------------------------------------------------
        elif kind == "Secret":
            secret_type = doc.get("type", "Opaque")
            data = doc.get("data", {}) or {}
            string_data = doc.get("stringData", {}) or {}
            annotations = metadata.get("annotations", {}) or {}

            # K80074 — Secret in default namespace
            namespace = metadata.get("namespace", "default")
            if namespace == "default":
                findings.append(
                    IaCFinding(
                        rule_id="K80074",
                        title="Secret in default namespace",
                        description=(
                            f"Secret '{resource_name}' is in the default namespace."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Create Secrets in dedicated namespaces.",
                    )
                )

            # K80075 — Secret contains sensitive key names
            sensitive_keys = {"password", "pwd", "secret", "token", "key", "credential"}
            all_keys = set(data.keys()) | set(string_data.keys())
            for key in all_keys:
                if any(s in key.lower() for s in sensitive_keys):
                    findings.append(
                        IaCFinding(
                            rule_id="K80075",
                            title="Secret with sensitive data",
                            description=(
                                f"Secret '{resource_name}' contains key '{key}' "
                                "which may contain sensitive data."
                            ),
                            severity=IaCSeverity.INFO,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Ensure encryption at rest is enabled.",
                        )
                    )
                    break

            # K80076 — Secret uses stringData (plain text)
            if string_data:
                findings.append(
                    IaCFinding(
                        rule_id="K80076",
                        title="Secret uses plaintext stringData",
                        description=(
                            f"Secret '{resource_name}' uses stringData which stores "
                            "plaintext values in the manifest."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use external secret management (e.g., Vault, SOPS).",
                    )
                )

            # K80077 — Secret not sealed or encrypted
            is_sealed = annotations.get("sealedsecrets.bitnami.com/sealed", "")
            is_sops = annotations.get("sops.k8s.io/encrypted", "")
            if not is_sealed and not is_sops and (data or string_data):
                findings.append(
                    IaCFinding(
                        rule_id="K80077",
                        title="Secret not encrypted",
                        description=(
                            f"Secret '{resource_name}' is not encrypted with "
                            "SealedSecrets or SOPS."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use SealedSecrets, SOPS, or external secret management.",
                    )
                )

        # ----------------------------------------------------------------
        # ConfigMap rules (K80078-K80080)
        # ----------------------------------------------------------------
        elif kind == "ConfigMap":
            data = doc.get("data", {}) or {}

            # K80078 — ConfigMap may contain secrets
            secret_patterns = re.compile(
                r"(password|secret|token|api[_-]?key|private[_-]?key|credential)",
                re.IGNORECASE,
            )
            for key, value in data.items():
                if secret_patterns.search(key):
                    findings.append(
                        IaCFinding(
                            rule_id="K80078",
                            title="ConfigMap may contain sensitive data",
                            description=(
                                f"ConfigMap '{resource_name}' has key '{key}' "
                                "which may contain sensitive data."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Store sensitive data in Secrets, not ConfigMaps.",
                        )
                    )
                    break
                if isinstance(value, str) and secret_patterns.search(value):
                    findings.append(
                        IaCFinding(
                            rule_id="K80078",
                            title="ConfigMap may contain sensitive data",
                            description=(
                                f"ConfigMap '{resource_name}' value for '{key}' "
                                "may contain sensitive data."
                            ),
                            severity=IaCSeverity.HIGH,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Store sensitive data in Secrets, not ConfigMaps.",
                        )
                    )
                    break

            # K80079 — ConfigMap in default namespace
            namespace = metadata.get("namespace", "default")
            if namespace == "default":
                findings.append(
                    IaCFinding(
                        rule_id="K80079",
                        title="ConfigMap in default namespace",
                        description=(
                            f"ConfigMap '{resource_name}' is in the default namespace."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Create ConfigMaps in dedicated namespaces.",
                    )
                )

            # K80080 — Large ConfigMap
            total_size = sum(len(str(v)) for v in data.values())
            if total_size > 1048576:  # 1MB
                findings.append(
                    IaCFinding(
                        rule_id="K80080",
                        title="Large ConfigMap",
                        description=(
                            f"ConfigMap '{resource_name}' is larger than 1MB "
                            f"({total_size} bytes)."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Consider using a PersistentVolume for large data.",
                    )
                )

        # ----------------------------------------------------------------
        # PodDisruptionBudget rules (K80081-K80082)
        # ----------------------------------------------------------------
        elif kind == "PodDisruptionBudget":
            min_available = spec.get("minAvailable")
            max_unavailable = spec.get("maxUnavailable")

            # K80081 — PDB with zero disruption allowed
            if min_available == "100%" or max_unavailable == 0 or max_unavailable == "0%":
                findings.append(
                    IaCFinding(
                        rule_id="K80081",
                        title="PDB blocks all disruptions",
                        description=(
                            f"PodDisruptionBudget '{resource_name}' allows zero disruptions, "
                            "which can block node drains."
                        ),
                        severity=IaCSeverity.HIGH,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Allow at least 1 pod to be unavailable.",
                    )
                )

            # K80082 — PDB selector missing
            selector = spec.get("selector", {})
            if not selector:
                findings.append(
                    IaCFinding(
                        rule_id="K80082",
                        title="PDB without selector",
                        description=(
                            f"PodDisruptionBudget '{resource_name}' has no selector."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add a selector to match target pods.",
                    )
                )

        # ----------------------------------------------------------------
        # ResourceQuota rules (K80083-K80084)
        # ----------------------------------------------------------------
        elif kind == "ResourceQuota":
            hard = spec.get("hard", {}) or {}

            # K80083 — ResourceQuota without CPU limits
            if "limits.cpu" not in hard and "requests.cpu" not in hard:
                findings.append(
                    IaCFinding(
                        rule_id="K80083",
                        title="ResourceQuota without CPU limits",
                        description=(
                            f"ResourceQuota '{resource_name}' does not limit CPU."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add limits.cpu or requests.cpu to the quota.",
                    )
                )

            # K80084 — ResourceQuota without memory limits
            if "limits.memory" not in hard and "requests.memory" not in hard:
                findings.append(
                    IaCFinding(
                        rule_id="K80084",
                        title="ResourceQuota without memory limits",
                        description=(
                            f"ResourceQuota '{resource_name}' does not limit memory."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add limits.memory or requests.memory to the quota.",
                    )
                )

        # ----------------------------------------------------------------
        # LimitRange rules (K80085-K80086)
        # ----------------------------------------------------------------
        elif kind == "LimitRange":
            limits = spec.get("limits", []) or []

            # K80085 — LimitRange without default limits
            has_defaults = False
            for limit in limits:
                if isinstance(limit, dict) and "default" in limit:
                    has_defaults = True
                    break
            if not has_defaults:
                findings.append(
                    IaCFinding(
                        rule_id="K80085",
                        title="LimitRange without defaults",
                        description=(
                            f"LimitRange '{resource_name}' has no default limits."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add default limits for containers.",
                    )
                )

            # K80086 — LimitRange without max limits
            has_max = False
            for limit in limits:
                if isinstance(limit, dict) and "max" in limit:
                    has_max = True
                    break
            if not has_max:
                findings.append(
                    IaCFinding(
                        rule_id="K80086",
                        title="LimitRange without max limits",
                        description=(
                            f"LimitRange '{resource_name}' has no max limits."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add max limits to prevent resource abuse.",
                    )
                )

        # ----------------------------------------------------------------
        # Namespace rules (K80087-K80088)
        # ----------------------------------------------------------------
        elif kind == "Namespace":
            labels = metadata.get("labels", {}) or {}
            annotations = metadata.get("annotations", {}) or {}

            # K80087 — Namespace without resource quota
            # This is more of an advisory since we can't check for associated quotas
            findings.append(
                IaCFinding(
                    rule_id="K80087",
                    title="Ensure namespace has ResourceQuota",
                    description=(
                        f"Namespace '{resource_name}' should have an associated ResourceQuota."
                    ),
                    severity=IaCSeverity.INFO,
                    platform=IaCPlatform.KUBERNETES,
                    file_path=file_path,
                    line_number=0,
                    resource_type=kind,
                    resource_name=resource_name,
                    remediation="Create a ResourceQuota for this namespace.",
                )
            )

            # K80088 — Namespace without network policy label
            if "kubernetes.io/metadata.name" not in labels:
                findings.append(
                    IaCFinding(
                        rule_id="K80088",
                        title="Namespace without metadata label",
                        description=(
                            f"Namespace '{resource_name}' lacks the "
                            "kubernetes.io/metadata.name label."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Add kubernetes.io/metadata.name label for NetworkPolicy selectors.",
                    )
                )

        # ----------------------------------------------------------------
        # PersistentVolume / PersistentVolumeClaim rules (K80089-K80092)
        # ----------------------------------------------------------------
        elif kind == "PersistentVolume":
            pv_spec = spec
            access_modes = pv_spec.get("accessModes", []) or []
            reclaim_policy = pv_spec.get("persistentVolumeReclaimPolicy", "")
            storage_class = pv_spec.get("storageClassName", "")

            # K80089 — PV with ReadWriteMany access
            if "ReadWriteMany" in access_modes:
                findings.append(
                    IaCFinding(
                        rule_id="K80089",
                        title="PersistentVolume with ReadWriteMany",
                        description=(
                            f"PersistentVolume '{resource_name}' allows ReadWriteMany access."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Use ReadWriteOnce unless shared access is required.",
                    )
                )

            # K80090 — PV with Retain policy
            if reclaim_policy == "Retain":
                findings.append(
                    IaCFinding(
                        rule_id="K80090",
                        title="PersistentVolume with Retain policy",
                        description=(
                            f"PersistentVolume '{resource_name}' uses Retain policy, "
                            "which may leave data after PVC deletion."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Ensure manual cleanup process exists for retained volumes.",
                    )
                )

            # K80091 — PV without storage class
            if not storage_class:
                findings.append(
                    IaCFinding(
                        rule_id="K80091",
                        title="PersistentVolume without StorageClass",
                        description=(
                            f"PersistentVolume '{resource_name}' has no StorageClass."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Assign a StorageClass for consistent provisioning.",
                    )
                )

        elif kind == "PersistentVolumeClaim":
            access_modes = spec.get("accessModes", []) or []
            storage_class = spec.get("storageClassName", "")

            # K80092 — PVC without storage class
            if not storage_class:
                findings.append(
                    IaCFinding(
                        rule_id="K80092",
                        title="PersistentVolumeClaim without StorageClass",
                        description=(
                            f"PersistentVolumeClaim '{resource_name}' has no StorageClass."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Specify a StorageClass for consistent provisioning.",
                    )
                )

        # ----------------------------------------------------------------
        # HorizontalPodAutoscaler rules (K80093-K80095)
        # ----------------------------------------------------------------
        elif kind == "HorizontalPodAutoscaler":
            min_replicas = spec.get("minReplicas", 1)
            max_replicas = spec.get("maxReplicas", 0)
            metrics = spec.get("metrics", []) or []

            # K80093 — HPA with minReplicas of 1
            if min_replicas == 1:
                findings.append(
                    IaCFinding(
                        rule_id="K80093",
                        title="HPA with single minimum replica",
                        description=(
                            f"HorizontalPodAutoscaler '{resource_name}' has minReplicas=1, "
                            "which may not provide high availability."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set minReplicas to at least 2 for high availability.",
                    )
                )

            # K80094 — HPA with very high maxReplicas
            if max_replicas > 100:
                findings.append(
                    IaCFinding(
                        rule_id="K80094",
                        title="HPA with very high maxReplicas",
                        description=(
                            f"HorizontalPodAutoscaler '{resource_name}' has maxReplicas={max_replicas}."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Ensure cluster can handle this many replicas.",
                    )
                )

            # K80095 — HPA without custom metrics
            has_custom = any(
                m.get("type") in ("Object", "External", "Pods")
                for m in metrics if isinstance(m, dict)
            )
            if not has_custom and not metrics:
                findings.append(
                    IaCFinding(
                        rule_id="K80095",
                        title="HPA using only default metrics",
                        description=(
                            f"HorizontalPodAutoscaler '{resource_name}' relies on "
                            "default CPU/memory metrics only."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Consider adding custom metrics for better scaling.",
                    )
                )

        # ----------------------------------------------------------------
        # ValidatingWebhookConfiguration rules (K80096-K80098)
        # ----------------------------------------------------------------
        elif kind in ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"):
            webhooks = doc.get("webhooks", []) or []

            for webhook in webhooks:
                if not isinstance(webhook, dict):
                    continue
                webhook_name = webhook.get("name", "unknown")
                failure_policy = webhook.get("failurePolicy", "Fail")
                timeout_seconds = webhook.get("timeoutSeconds", 10)
                rules = webhook.get("rules", []) or []

                # K80096 — Webhook with Ignore failure policy
                if failure_policy == "Ignore":
                    findings.append(
                        IaCFinding(
                            rule_id="K80096",
                            title="Webhook with Ignore failure policy",
                            description=(
                                f"{kind} '{resource_name}' webhook '{webhook_name}' "
                                "uses Ignore failure policy."
                            ),
                            severity=IaCSeverity.MEDIUM,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Use Fail policy for security-critical webhooks.",
                        )
                    )

                # K80097 — Webhook with long timeout
                if timeout_seconds > 15:
                    findings.append(
                        IaCFinding(
                            rule_id="K80097",
                            title="Webhook with long timeout",
                            description=(
                                f"{kind} '{resource_name}' webhook '{webhook_name}' "
                                f"has timeout of {timeout_seconds}s."
                            ),
                            severity=IaCSeverity.LOW,
                            platform=IaCPlatform.KUBERNETES,
                            file_path=file_path,
                            line_number=0,
                            resource_type=kind,
                            resource_name=resource_name,
                            remediation="Reduce timeout to prevent API latency.",
                        )
                    )

                # K80098 — Webhook matching all resources
                for rule in rules:
                    if isinstance(rule, dict):
                        resources = rule.get("resources", []) or []
                        if "*" in resources:
                            findings.append(
                                IaCFinding(
                                    rule_id="K80098",
                                    title="Webhook matches all resources",
                                    description=(
                                        f"{kind} '{resource_name}' webhook '{webhook_name}' "
                                        "matches all resources (*)."
                                    ),
                                    severity=IaCSeverity.MEDIUM,
                                    platform=IaCPlatform.KUBERNETES,
                                    file_path=file_path,
                                    line_number=0,
                                    resource_type=kind,
                                    resource_name=resource_name,
                                    remediation="Limit webhook scope to specific resources.",
                                )
                            )

        # ----------------------------------------------------------------
        # CronJob specific rules (K80099-K80100)
        # ----------------------------------------------------------------
        elif kind == "CronJob":
            schedule = spec.get("schedule", "")
            concurrency_policy = spec.get("concurrencyPolicy", "Allow")
            starting_deadline = spec.get("startingDeadlineSeconds")
            successful_jobs_history = spec.get("successfulJobsHistoryLimit", 3)
            failed_jobs_history = spec.get("failedJobsHistoryLimit", 1)

            # K80099 — CronJob without concurrency policy
            if concurrency_policy == "Allow":
                findings.append(
                    IaCFinding(
                        rule_id="K80099",
                        title="CronJob allows concurrent executions",
                        description=(
                            f"CronJob '{resource_name}' allows concurrent job executions."
                        ),
                        severity=IaCSeverity.INFO,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set concurrencyPolicy to Forbid or Replace if jobs shouldn't overlap.",
                    )
                )

            # K80100 — CronJob without starting deadline
            if starting_deadline is None:
                findings.append(
                    IaCFinding(
                        rule_id="K80100",
                        title="CronJob without starting deadline",
                        description=(
                            f"CronJob '{resource_name}' has no startingDeadlineSeconds."
                        ),
                        severity=IaCSeverity.LOW,
                        platform=IaCPlatform.KUBERNETES,
                        file_path=file_path,
                        line_number=0,
                        resource_type=kind,
                        resource_name=resource_name,
                        remediation="Set startingDeadlineSeconds to handle missed schedules.",
                    )
                )

        return findings

    # ------------------------------------------------------------------
    # Helm scanning
    # ------------------------------------------------------------------

    def scan_helm(self, path: Path) -> list[IaCFinding]:
        """Scan Helm charts for security misconfigurations."""
        path = Path(path)
        findings: list[IaCFinding] = []
        helm_dirs = self._find_helm_charts(path)

        for chart_dir in helm_dirs:
            values_file = chart_dir / "values.yaml"
            if not values_file.exists():
                continue
            try:
                content = values_file.read_text(encoding="utf-8")
                values = yaml.safe_load(content)
            except Exception:
                continue

            if not isinstance(values, dict):
                continue

            file_str = str(values_file)
            findings.extend(self._helm_scan_values(values, file_str))

        return findings

    def _find_helm_charts(self, path: Path) -> list[Path]:
        """Find Helm chart directories by looking for Chart.yaml."""
        helm_dirs: list[Path] = []
        for chart_file in path.rglob("Chart.yaml"):
            helm_dirs.append(chart_file.parent)
        return helm_dirs

    def _helm_scan_values(
        self, values: dict[str, Any], file_path: str
    ) -> list[IaCFinding]:
        findings: list[IaCFinding] = []

        # HM0001 — Hardcoded secrets
        findings.extend(
            self._hm0001_hardcoded_secrets(values, file_path, prefix="")
        )

        # HM0002 — No resource limits
        if not self._helm_has_resource_limits(values):
            findings.append(
                IaCFinding(
                    rule_id="HM0002",
                    title="No resource limits in values.yaml",
                    description=(
                        "Helm values.yaml does not define resource limits."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.HELM,
                    file_path=file_path,
                    line_number=0,
                    resource_type="HelmValues",
                    resource_name="values.yaml",
                    remediation=(
                        "Add resources.limits with CPU and memory values."
                    ),
                )
            )

        # HM0003 — Image tag set to latest
        image = values.get("image", {})
        if isinstance(image, dict):
            tag = image.get("tag", "")
            if tag == "latest" or tag == "":
                findings.append(
                    IaCFinding(
                        rule_id="HM0003",
                        title="Image tag set to latest",
                        description=(
                            "Helm values.yaml uses 'latest' or empty image tag."
                        ),
                        severity=IaCSeverity.MEDIUM,
                        platform=IaCPlatform.HELM,
                        file_path=file_path,
                        line_number=0,
                        resource_type="HelmValues",
                        resource_name="values.yaml",
                        remediation="Set a specific image tag or digest.",
                    )
                )

        # HM0004 — securityContext not defined
        if "securityContext" not in values and "podSecurityContext" not in values:
            findings.append(
                IaCFinding(
                    rule_id="HM0004",
                    title="securityContext not defined",
                    description=(
                        "Helm values.yaml does not define a securityContext."
                    ),
                    severity=IaCSeverity.MEDIUM,
                    platform=IaCPlatform.HELM,
                    file_path=file_path,
                    line_number=0,
                    resource_type="HelmValues",
                    resource_name="values.yaml",
                    remediation=(
                        "Add securityContext with runAsNonRoot, "
                        "readOnlyRootFilesystem, etc."
                    ),
                )
            )

        return findings

    def _hm0001_hardcoded_secrets(
        self,
        values: dict[str, Any],
        file_path: str,
        prefix: str,
    ) -> list[IaCFinding]:
        """Recursively check for hardcoded secrets in Helm values."""
        findings: list[IaCFinding] = []
        secret_patterns = re.compile(
            r"(password|secret|token|api_key|apikey|access_key|private_key)",
            re.IGNORECASE,
        )
        for key, val in values.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(val, dict):
                findings.extend(
                    self._hm0001_hardcoded_secrets(val, file_path, full_key)
                )
            elif isinstance(val, str) and val:
                if secret_patterns.search(key):
                    findings.append(
                        IaCFinding(
                            rule_id="HM0001",
                            title="Hardcoded secret in values.yaml",
                            description=(
                                f"Key '{full_key}' in values.yaml appears to "
                                "contain a hardcoded secret."
                            ),
                            severity=IaCSeverity.CRITICAL,
                            platform=IaCPlatform.HELM,
                            file_path=file_path,
                            line_number=0,
                            resource_type="HelmValues",
                            resource_name="values.yaml",
                            remediation=(
                                "Use Helm secrets plugin or external secret "
                                "management."
                            ),
                        )
                    )
        return findings

    def _helm_has_resource_limits(self, values: dict[str, Any]) -> bool:
        """Check if values.yaml defines resource limits anywhere."""
        resources = values.get("resources", {})
        if isinstance(resources, dict) and "limits" in resources:
            return True
        # Check nested containers
        for key, val in values.items():
            if isinstance(val, dict):
                res = val.get("resources", {})
                if isinstance(res, dict) and "limits" in res:
                    return True
        return False
