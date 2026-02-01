"""Multi-cloud Terraform rules for Azure, GCP, and OCI."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codescope.iac.scanner import (
    IaCFinding,
    IaCPlatform,
    IaCSeverity,
    _parse_tf_resources,
    _tf_body_has_block,
    _tf_body_has_key_value,
    _tf_body_get_value,
)


class MultiCloudTerraformScanner:
    """Terraform rules for Azure, GCP, and OCI resources."""

    def scan(self, path: Path) -> list[IaCFinding]:
        """Scan .tf files for Azure, GCP, and OCI misconfigurations."""
        path = Path(path)
        findings: list[IaCFinding] = []
        tf_files = list(path.rglob("*.tf"))

        all_resources: list[dict[str, Any]] = []
        for tf_file in tf_files:
            try:
                content = tf_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            all_resources.extend(_parse_tf_resources(content, str(tf_file)))

        # Azure rules
        findings.extend(self._az0001_storage_no_https(all_resources))
        findings.extend(self._az0002_storage_no_encryption(all_resources))
        findings.extend(self._az0003_nsg_open_to_world(all_resources))
        findings.extend(self._az0004_sql_no_audit(all_resources))
        findings.extend(self._az0005_keyvault_no_purge_protection(all_resources))
        findings.extend(self._az0006_managed_disk_no_encryption(all_resources))
        findings.extend(self._az0007_app_service_http(all_resources))
        findings.extend(self._az0008_sql_public_access(all_resources))
        findings.extend(self._az0009_aks_rbac_disabled(all_resources))
        findings.extend(self._az0010_hardcoded_secrets(all_resources))

        # GCP rules
        findings.extend(self._gc0001_gcs_no_encryption(all_resources))
        findings.extend(self._gc0002_gcs_public(all_resources))
        findings.extend(self._gc0003_firewall_open(all_resources))
        findings.extend(self._gc0004_sql_no_ssl(all_resources))
        findings.extend(self._gc0005_sql_public(all_resources))
        findings.extend(self._gc0006_compute_default_sa(all_resources))
        findings.extend(self._gc0007_gke_legacy_auth(all_resources))
        findings.extend(self._gc0008_gke_dashboard_enabled(all_resources))
        findings.extend(self._gc0009_iam_admin_role(all_resources))
        findings.extend(self._gc0010_kms_rotation(all_resources))

        # OCI rules
        findings.extend(self._oc0001_bucket_public(all_resources))
        findings.extend(self._oc0002_seclist_open(all_resources))
        findings.extend(self._oc0003_db_no_encryption(all_resources))
        findings.extend(self._oc0004_nsg_open(all_resources))
        findings.extend(self._oc0005_boot_volume_no_encryption(all_resources))

        return findings

    # ── Azure rules (AZ####) ─────────────────────────────────────────

    def _az0001_storage_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if _tf_body_has_key_value(res["body"], "enable_https_traffic_only", "false"):
                    findings.append(self._finding(
                        "AZ0001", "Azure Storage without HTTPS",
                        f"Storage account '{res['name']}' does not enforce HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set enable_https_traffic_only = true (default in provider v3+).",
                    ))
        return findings

    def _az0002_storage_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                # Check if infrastructure encryption is explicitly disabled
                if _tf_body_has_key_value(res["body"], "infrastructure_encryption_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0002", "Azure Storage without infrastructure encryption",
                        f"Storage account '{res['name']}' has infrastructure encryption disabled.",
                        IaCSeverity.HIGH, res,
                        "Set infrastructure_encryption_enabled = true.",
                    ))
        return findings

    def _az0003_nsg_open_to_world(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_network_security_rule", "azurerm_network_security_group"):
                body = res["body"]
                if "0.0.0.0/0" in body or '"*"' in body:
                    direction = _tf_body_get_value(body, "direction")
                    access = _tf_body_get_value(body, "access")
                    if direction and "inbound" in direction.lower() and access and "allow" in access.lower():
                        port = _tf_body_get_value(body, "destination_port_range") or ""
                        port_clean = port.strip('"')
                        if port_clean not in ("80", "443"):
                            findings.append(self._finding(
                                "AZ0003", "NSG rule open to world",
                                f"NSG rule '{res['name']}' allows inbound from 0.0.0.0/0 or * on port {port_clean}.",
                                IaCSeverity.HIGH, res,
                                "Restrict source_address_prefix to specific IP ranges.",
                            ))
        return findings

    def _az0004_sql_no_audit(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        sql_servers = [r for r in resources if r["type"] == "azurerm_mssql_server"]
        has_audit = "azurerm_mssql_server_extended_auditing_policy" in resource_types
        if sql_servers and not has_audit:
            for res in sql_servers:
                findings.append(self._finding(
                    "AZ0004", "Azure SQL without auditing",
                    f"SQL Server '{res['name']}' has no auditing policy configured.",
                    IaCSeverity.MEDIUM, res,
                    "Add an azurerm_mssql_server_extended_auditing_policy resource.",
                ))
        return findings

    def _az0005_keyvault_no_purge_protection(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                if not _tf_body_has_key_value(res["body"], "purge_protection_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0005", "Key Vault without purge protection",
                        f"Key Vault '{res['name']}' does not have purge protection enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set purge_protection_enabled = true.",
                    ))
        return findings

    def _az0006_managed_disk_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_managed_disk":
                enc_type = _tf_body_get_value(res["body"], "encryption_type")
                if enc_type and "none" in enc_type.lower():
                    findings.append(self._finding(
                        "AZ0006", "Managed Disk without encryption",
                        f"Managed Disk '{res['name']}' has encryption disabled.",
                        IaCSeverity.HIGH, res,
                        "Set encryption_type to EncryptionAtRestWithPlatformKey or EncryptionAtRestWithCustomerKey.",
                    ))
        return findings

    def _az0007_app_service_http(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_app_service", "azurerm_linux_web_app", "azurerm_windows_web_app"):
                if _tf_body_has_key_value(res["body"], "https_only", "false"):
                    findings.append(self._finding(
                        "AZ0007", "App Service allows HTTP",
                        f"App Service '{res['name']}' does not enforce HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set https_only = true.",
                    ))
        return findings

    def _az0008_sql_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0008", "Azure SQL publicly accessible",
                        f"SQL Server '{res['name']}' allows public network access.",
                        IaCSeverity.CRITICAL, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0009_aks_rbac_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if _tf_body_has_key_value(res["body"], "role_based_access_control_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0009", "AKS RBAC disabled",
                        f"AKS cluster '{res['name']}' has RBAC disabled.",
                        IaCSeverity.HIGH, res,
                        "Set role_based_access_control_enabled = true.",
                    ))
        return findings

    def _az0010_hardcoded_secrets(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        secret_keys = ("admin_password", "administrator_login_password", "client_secret")
        for res in resources:
            if not res["type"].startswith("azurerm_"):
                continue
            for key in secret_keys:
                val = _tf_body_get_value(res["body"], key)
                if val and (val.startswith('"') or val.startswith("'")):
                    stripped = val.strip('"').strip("'")
                    if not stripped.startswith("var.") and not stripped.startswith("${"):
                        findings.append(self._finding(
                            "AZ0010", "Hardcoded secret in Azure resource",
                            f"Resource '{res['name']}' has a hardcoded value for '{key}'.",
                            IaCSeverity.CRITICAL, res,
                            "Use variables or Azure Key Vault references instead of hardcoded secrets.",
                        ))
        return findings

    # ── GCP rules (GC####) ───────────────────────────────────────────

    def _gc0001_gcs_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_storage_bucket":
                # GCS encrypts by default but check for CMEK
                pass  # No finding — GCS encrypts by default
        return findings

    def _gc0002_gcs_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_storage_bucket_iam_member":
                body = res["body"]
                member = _tf_body_get_value(body, "member")
                if member and ("allUsers" in member or "allAuthenticatedUsers" in member):
                    findings.append(self._finding(
                        "GC0002", "GCS bucket publicly accessible",
                        f"GCS IAM binding '{res['name']}' grants access to {member.strip(chr(34))}.",
                        IaCSeverity.CRITICAL, res,
                        "Remove allUsers/allAuthenticatedUsers from IAM bindings.",
                    ))
            if res["type"] == "google_storage_bucket_access_control":
                body = res["body"]
                entity = _tf_body_get_value(body, "entity")
                if entity and ("allUsers" in entity or "allAuthenticatedUsers" in entity):
                    findings.append(self._finding(
                        "GC0002", "GCS bucket publicly accessible",
                        f"GCS access control '{res['name']}' grants public access.",
                        IaCSeverity.CRITICAL, res,
                        "Remove public access entities.",
                    ))
        return findings

    def _gc0003_firewall_open(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_firewall":
                body = res["body"]
                if "0.0.0.0/0" in body:
                    direction = _tf_body_get_value(body, "direction") or "INGRESS"
                    if "ingress" in direction.lower() or direction == "INGRESS":
                        # Check for ports
                        ports_match = re.search(r'ports\s*=\s*\[([^\]]*)\]', body)
                        if ports_match:
                            ports_str = ports_match.group(1)
                            if '"22"' in ports_str or '"3389"' in ports_str or '"0-65535"' in ports_str:
                                findings.append(self._finding(
                                    "GC0003", "GCP firewall open to world",
                                    f"Firewall rule '{res['name']}' allows ingress from 0.0.0.0/0 on sensitive ports.",
                                    IaCSeverity.HIGH, res,
                                    "Restrict source_ranges to specific IP ranges.",
                                ))
                        else:
                            findings.append(self._finding(
                                "GC0003", "GCP firewall open to world",
                                f"Firewall rule '{res['name']}' allows ingress from 0.0.0.0/0.",
                                IaCSeverity.HIGH, res,
                                "Restrict source_ranges to specific IP ranges.",
                            ))
        return findings

    def _gc0004_sql_no_ssl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_sql_database_instance":
                if _tf_body_has_key_value(res["body"], "require_ssl", "false"):
                    findings.append(self._finding(
                        "GC0004", "Cloud SQL without SSL enforcement",
                        f"Cloud SQL instance '{res['name']}' does not require SSL.",
                        IaCSeverity.HIGH, res,
                        "Set require_ssl = true in settings.ip_configuration.",
                    ))
        return findings

    def _gc0005_sql_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_sql_database_instance":
                body = res["body"]
                if "0.0.0.0/0" in body:
                    findings.append(self._finding(
                        "GC0005", "Cloud SQL publicly accessible",
                        f"Cloud SQL instance '{res['name']}' allows connections from 0.0.0.0/0.",
                        IaCSeverity.CRITICAL, res,
                        "Restrict authorized_networks to specific IP ranges or use private IP.",
                    ))
        return findings

    def _gc0006_compute_default_sa(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_instance":
                body = res["body"]
                email = _tf_body_get_value(body, "email")
                scopes = _tf_body_get_value(body, "scopes")
                if scopes and "cloud-platform" in scopes and not email:
                    findings.append(self._finding(
                        "GC0006", "Compute instance using default service account",
                        f"Compute instance '{res['name']}' uses default service account with broad scopes.",
                        IaCSeverity.MEDIUM, res,
                        "Create a dedicated service account with minimal permissions.",
                    ))
        return findings

    def _gc0007_gke_legacy_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if _tf_body_has_key_value(res["body"], "enable_legacy_abac", "true"):
                    findings.append(self._finding(
                        "GC0007", "GKE legacy ABAC enabled",
                        f"GKE cluster '{res['name']}' has legacy ABAC enabled.",
                        IaCSeverity.HIGH, res,
                        "Set enable_legacy_abac = false and use RBAC.",
                    ))
        return findings

    def _gc0008_gke_dashboard_enabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if _tf_body_has_key_value(res["body"], "disabled", "false"):
                    if _tf_body_has_block(res["body"], "kubernetes_dashboard"):
                        findings.append(self._finding(
                            "GC0008", "GKE dashboard enabled",
                            f"GKE cluster '{res['name']}' has the Kubernetes dashboard enabled.",
                            IaCSeverity.MEDIUM, res,
                            "Disable the Kubernetes dashboard addon.",
                        ))
        return findings

    def _gc0009_iam_admin_role(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("google_project_iam_member", "google_project_iam_binding"):
                role = _tf_body_get_value(res["body"], "role")
                if role and ("roles/owner" in role or "roles/editor" in role):
                    findings.append(self._finding(
                        "GC0009", "Overly permissive IAM role",
                        f"IAM binding '{res['name']}' grants {role.strip(chr(34))} role.",
                        IaCSeverity.HIGH, res,
                        "Use more specific roles following the principle of least privilege.",
                    ))
        return findings

    def _gc0010_kms_rotation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_kms_crypto_key":
                if not _tf_body_get_value(res["body"], "rotation_period"):
                    findings.append(self._finding(
                        "GC0010", "KMS key without rotation",
                        f"KMS key '{res['name']}' does not have automatic rotation configured.",
                        IaCSeverity.MEDIUM, res,
                        "Set rotation_period (e.g. '7776000s' for 90 days).",
                    ))
        return findings

    # ── OCI rules (OC####) ───────────────────────────────────────────

    def _oc0001_bucket_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_objectstorage_bucket":
                access_type = _tf_body_get_value(res["body"], "access_type")
                if access_type and access_type.strip('"') in ("ObjectRead", "ObjectReadWithoutList"):
                    findings.append(self._finding(
                        "OC0001", "OCI Object Storage bucket publicly accessible",
                        f"Object Storage bucket '{res['name']}' has public access enabled.",
                        IaCSeverity.CRITICAL, res,
                        "Set access_type = 'NoPublicAccess'.",
                    ))
        return findings

    def _oc0002_seclist_open(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_security_list":
                body = res["body"]
                if "0.0.0.0/0" in body:
                    # Check if it's ingress
                    if re.search(r'ingress_security_rules\s*\{', body):
                        protocol = _tf_body_get_value(body, "protocol")
                        if protocol and protocol.strip('"') == "all":
                            findings.append(self._finding(
                                "OC0002", "OCI Security List open to world",
                                f"Security List '{res['name']}' allows all ingress from 0.0.0.0/0.",
                                IaCSeverity.HIGH, res,
                                "Restrict ingress source CIDR to specific IP ranges.",
                            ))
        return findings

    def _oc0003_db_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_database_db_system":
                # OCI DB systems use TDE by default, but check for data_storage_size without CMEK
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0003", "OCI DB System without customer-managed encryption key",
                        f"DB System '{res['name']}' does not use a customer-managed encryption key.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for customer-managed encryption.",
                    ))
        return findings

    def _oc0004_nsg_open(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_network_security_group_security_rule":
                body = res["body"]
                direction = _tf_body_get_value(body, "direction")
                source = _tf_body_get_value(body, "source")
                if direction and "ingress" in direction.lower():
                    if source and "0.0.0.0/0" in source:
                        findings.append(self._finding(
                            "OC0004", "OCI NSG rule open to world",
                            f"NSG rule '{res['name']}' allows ingress from 0.0.0.0/0.",
                            IaCSeverity.HIGH, res,
                            "Restrict source to specific CIDR blocks.",
                        ))
        return findings

    def _oc0005_boot_volume_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_boot_volume":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0005", "OCI Boot Volume without customer-managed encryption",
                        f"Boot Volume '{res['name']}' does not use a customer-managed key.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for customer-managed encryption.",
                    ))
        return findings

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _finding(
        rule_id: str, title: str, description: str,
        severity: IaCSeverity, res: dict, remediation: str,
    ) -> IaCFinding:
        return IaCFinding(
            rule_id=rule_id, title=title, description=description,
            severity=severity, platform=IaCPlatform.TERRAFORM,
            file_path=res["file"], line_number=res["line"],
            resource_type=res["type"], resource_name=res["name"],
            remediation=remediation,
        )
