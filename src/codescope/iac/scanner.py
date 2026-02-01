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
