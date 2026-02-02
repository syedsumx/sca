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
        findings.extend(self._az0011_cosmosdb_no_cmk(all_resources))
        findings.extend(self._az0012_cosmosdb_public_access(all_resources))
        findings.extend(self._az0013_function_http(all_resources))
        findings.extend(self._az0014_acr_admin_enabled(all_resources))
        findings.extend(self._az0015_acr_no_encryption(all_resources))
        findings.extend(self._az0016_servicebus_no_encryption(all_resources))
        findings.extend(self._az0017_eventhub_no_encryption(all_resources))
        findings.extend(self._az0018_log_analytics_retention(all_resources))
        findings.extend(self._az0019_redis_no_ssl(all_resources))
        findings.extend(self._az0020_redis_no_encryption(all_resources))
        findings.extend(self._az0021_appgw_no_waf(all_resources))
        findings.extend(self._az0022_postgresql_no_ssl(all_resources))
        findings.extend(self._az0023_postgresql_public_access(all_resources))
        findings.extend(self._az0024_mysql_no_ssl(all_resources))
        findings.extend(self._az0025_mysql_public_access(all_resources))
        findings.extend(self._az0026_monitor_no_diagnostic(all_resources))
        findings.extend(self._az0027_frontdoor_no_waf(all_resources))
        findings.extend(self._az0028_container_group_public(all_resources))
        findings.extend(self._az0029_logic_app_no_https(all_resources))
        findings.extend(self._az0030_synapse_public_access(all_resources))
        findings.extend(self._az0031_data_factory_public_access(all_resources))
        findings.extend(self._az0032_search_service_public(all_resources))
        findings.extend(self._az0033_cognitive_account_public(all_resources))
        findings.extend(self._az0034_mariadb_no_ssl(all_resources))
        findings.extend(self._az0035_mariadb_public_access(all_resources))
        findings.extend(self._az0036_batch_account_public(all_resources))
        findings.extend(self._az0037_api_management_no_https(all_resources))
        findings.extend(self._az0038_storage_no_network_rules(all_resources))
        findings.extend(self._az0039_keyvault_no_network_acls(all_resources))
        findings.extend(self._az0040_signalr_public_access(all_resources))
        findings.extend(self._az0041_iothub_public_access(all_resources))
        findings.extend(self._az0042_webapp_min_tls(all_resources))
        findings.extend(self._az0043_aks_network_policy(all_resources))
        findings.extend(self._az0044_dns_zone_public(all_resources))
        findings.extend(self._az0045_databricks_public_access(all_resources))
        findings.extend(self._az0046_purview_public_access(all_resources))
        findings.extend(self._az0047_container_app_insecure(all_resources))
        findings.extend(self._az0048_spring_cloud_public(all_resources))
        findings.extend(self._az0049_automation_account_public(all_resources))
        findings.extend(self._az0050_virtual_network_no_ddos(all_resources))
        findings.extend(self._az0051_firewall_no_threat_intel(all_resources))
        findings.extend(self._az0052_express_route_no_encryption(all_resources))
        findings.extend(self._az0053_machine_learning_public(all_resources))
        findings.extend(self._az0054_event_grid_public(all_resources))
        findings.extend(self._az0055_stream_analytics_public(all_resources))
        findings.extend(self._az0056_hdinsight_public(all_resources))
        findings.extend(self._az0057_notification_hub_no_auth(all_resources))
        findings.extend(self._az0058_managed_identity_missing(all_resources))
        findings.extend(self._az0059_private_endpoint_missing(all_resources))
        findings.extend(self._az0060_defender_for_cloud(all_resources))
        findings.extend(self._az0061_vpn_gateway_no_aad(all_resources))
        findings.extend(self._az0062_nat_gateway_missing(all_resources))
        findings.extend(self._az0063_load_balancer_no_backend(all_resources))
        findings.extend(self._az0064_traffic_manager_no_https(all_resources))
        findings.extend(self._az0065_media_services_public(all_resources))
        findings.extend(self._az0066_data_lake_store_no_encryption(all_resources))
        findings.extend(self._az0067_bastion_host_missing(all_resources))
        findings.extend(self._az0068_policy_assignment_missing(all_resources))
        findings.extend(self._az0069_backup_vault_missing(all_resources))
        findings.extend(self._az0070_monitor_action_group_missing(all_resources))
        findings.extend(self._az0071_vm_disk_encryption(all_resources))
        findings.extend(self._az0072_network_watcher_missing(all_resources))
        findings.extend(self._az0073_app_config_public(all_resources))
        findings.extend(self._az0074_maps_account_cors(all_resources))
        findings.extend(self._az0075_cdn_endpoint_no_https(all_resources))
        findings.extend(self._az0076_container_app_env_internal(all_resources))
        findings.extend(self._az0077_service_fabric_no_aad(all_resources))
        findings.extend(self._az0078_digital_twins_public(all_resources))
        findings.extend(self._az0079_openai_public_access(all_resources))
        findings.extend(self._az0080_managed_grafana_public(all_resources))
        findings.extend(self._az0081_data_factory_public(all_resources))
        findings.extend(self._az0082_logic_app_no_managed_identity(all_resources))
        findings.extend(self._az0083_api_management_no_https(all_resources))
        findings.extend(self._az0084_synapse_public_access(all_resources))
        findings.extend(self._az0085_stream_analytics_no_identity(all_resources))
        findings.extend(self._az0086_purview_public_access(all_resources))
        findings.extend(self._az0087_batch_account_no_private(all_resources))
        findings.extend(self._az0088_notification_hub_no_auth(all_resources))
        findings.extend(self._az0089_signalr_public_access(all_resources))
        findings.extend(self._az0090_static_web_app_no_auth(all_resources))

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

    def _az0011_cosmosdb_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_get_value(res["body"], "key_vault_key_id"):
                    findings.append(self._finding(
                        "AZ0011", "Cosmos DB without customer-managed key",
                        f"Cosmos DB account '{res['name']}' does not use a customer-managed encryption key.",
                        IaCSeverity.MEDIUM, res,
                        "Set key_vault_key_id to use a customer-managed key for encryption.",
                    ))
        return findings

    def _az0012_cosmosdb_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0012", "Cosmos DB publicly accessible",
                        f"Cosmos DB account '{res['name']}' allows public network access.",
                        IaCSeverity.CRITICAL, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if _tf_body_has_key_value(res["body"], "is_virtual_network_filter_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0012", "Cosmos DB publicly accessible",
                        f"Cosmos DB account '{res['name']}' has virtual network filtering disabled.",
                        IaCSeverity.CRITICAL, res,
                        "Set is_virtual_network_filter_enabled = true and configure virtual_network_rule blocks.",
                    ))
        return findings

    def _az0013_function_http(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_function_app", "azurerm_linux_function_app", "azurerm_windows_function_app"):
                if _tf_body_has_key_value(res["body"], "https_only", "false"):
                    findings.append(self._finding(
                        "AZ0013", "Azure Function allows HTTP",
                        f"Function App '{res['name']}' does not enforce HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set https_only = true.",
                    ))
        return findings

    def _az0014_acr_admin_enabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                if _tf_body_has_key_value(res["body"], "admin_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0014", "Container Registry admin account enabled",
                        f"ACR '{res['name']}' has the admin account enabled.",
                        IaCSeverity.HIGH, res,
                        "Set admin_enabled = false and use Azure AD authentication or managed identities.",
                    ))
        return findings

    def _az0015_acr_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "premium" in sku.lower():
                    if not _tf_body_has_block(res["body"], "encryption"):
                        findings.append(self._finding(
                            "AZ0015", "Container Registry without CMK encryption",
                            f"ACR '{res['name']}' (Premium) does not configure customer-managed key encryption.",
                            IaCSeverity.MEDIUM, res,
                            "Add an encryption block with key_vault_key_id for customer-managed keys.",
                        ))
        return findings

    def _az0016_servicebus_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_servicebus_namespace":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "premium" in sku.lower():
                    if not _tf_body_has_block(res["body"], "customer_managed_key"):
                        findings.append(self._finding(
                            "AZ0016", "Service Bus without CMK encryption",
                            f"Service Bus namespace '{res['name']}' (Premium) does not use customer-managed key encryption.",
                            IaCSeverity.MEDIUM, res,
                            "Add a customer_managed_key block with key_vault_key_id.",
                        ))
        return findings

    def _az0017_eventhub_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_eventhub_namespace":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and ("premium" in sku.lower() or "dedicated" in sku.lower()):
                    if not _tf_body_has_block(res["body"], "customer_managed_key"):
                        findings.append(self._finding(
                            "AZ0017", "Event Hub without CMK encryption",
                            f"Event Hub namespace '{res['name']}' does not use customer-managed key encryption.",
                            IaCSeverity.MEDIUM, res,
                            "Add a customer_managed_key block for encryption at rest.",
                        ))
        return findings

    def _az0018_log_analytics_retention(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_log_analytics_workspace":
                retention = _tf_body_get_value(res["body"], "retention_in_days")
                if retention:
                    try:
                        days = int(retention.strip('"'))
                        if days < 90:
                            findings.append(self._finding(
                                "AZ0018", "Log Analytics insufficient retention",
                                f"Log Analytics workspace '{res['name']}' retention is {days} days (< 90).",
                                IaCSeverity.MEDIUM, res,
                                "Set retention_in_days to at least 90 for compliance.",
                            ))
                    except ValueError:
                        pass
        return findings

    def _az0019_redis_no_ssl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_redis_cache":
                if _tf_body_has_key_value(res["body"], "enable_non_ssl_port", "true"):
                    findings.append(self._finding(
                        "AZ0019", "Redis Cache non-SSL port enabled",
                        f"Redis Cache '{res['name']}' has the non-SSL port (6379) enabled.",
                        IaCSeverity.HIGH, res,
                        "Set enable_non_ssl_port = false to enforce TLS connections.",
                    ))
        return findings

    def _az0020_redis_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_redis_cache":
                if _tf_body_has_key_value(res["body"], "minimum_tls_version", '"1.0"') or \
                   _tf_body_has_key_value(res["body"], "minimum_tls_version", '"1.1"'):
                    findings.append(self._finding(
                        "AZ0020", "Redis Cache outdated TLS version",
                        f"Redis Cache '{res['name']}' uses a TLS version lower than 1.2.",
                        IaCSeverity.HIGH, res,
                        "Set minimum_tls_version = '1.2'.",
                    ))
        return findings

    def _az0021_appgw_no_waf(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                sku_tier = _tf_body_get_value(res["body"], "tier")
                if sku_tier and "waf" not in sku_tier.lower():
                    findings.append(self._finding(
                        "AZ0021", "Application Gateway without WAF",
                        f"Application Gateway '{res['name']}' does not use a WAF-enabled SKU tier.",
                        IaCSeverity.HIGH, res,
                        "Use SKU tier WAF_v2 and configure a WAF policy.",
                    ))
        return findings

    def _az0022_postgresql_no_ssl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_postgresql_server", "azurerm_postgresql_flexible_server"):
                if _tf_body_has_key_value(res["body"], "ssl_enforcement_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0022", "PostgreSQL without SSL enforcement",
                        f"PostgreSQL server '{res['name']}' does not enforce SSL connections.",
                        IaCSeverity.HIGH, res,
                        "Set ssl_enforcement_enabled = true.",
                    ))
        return findings

    def _az0023_postgresql_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_postgresql_server", "azurerm_postgresql_flexible_server"):
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0023", "PostgreSQL publicly accessible",
                        f"PostgreSQL server '{res['name']}' allows public network access.",
                        IaCSeverity.CRITICAL, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0024_mysql_no_ssl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_mysql_server", "azurerm_mysql_flexible_server"):
                if _tf_body_has_key_value(res["body"], "ssl_enforcement_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0024", "MySQL without SSL enforcement",
                        f"MySQL server '{res['name']}' does not enforce SSL connections.",
                        IaCSeverity.HIGH, res,
                        "Set ssl_enforcement_enabled = true.",
                    ))
        return findings

    def _az0025_mysql_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_mysql_server", "azurerm_mysql_flexible_server"):
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0025", "MySQL publicly accessible",
                        f"MySQL server '{res['name']}' allows public network access.",
                        IaCSeverity.CRITICAL, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0026_monitor_no_diagnostic(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_diagnostic = "azurerm_monitor_diagnostic_setting" in resource_types
        key_resources = [
            r for r in resources
            if r["type"] in (
                "azurerm_key_vault", "azurerm_mssql_server",
                "azurerm_cosmosdb_account", "azurerm_kubernetes_cluster",
            )
        ]
        if key_resources and not has_diagnostic:
            for res in key_resources:
                findings.append(self._finding(
                    "AZ0026", "Azure resource without diagnostic settings",
                    f"Resource '{res['name']}' ({res['type']}) has no azurerm_monitor_diagnostic_setting configured.",
                    IaCSeverity.MEDIUM, res,
                    "Add an azurerm_monitor_diagnostic_setting resource to capture logs and metrics.",
                ))
        return findings

    def _az0027_frontdoor_no_waf(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_frontdoor", "azurerm_cdn_frontdoor_profile"):
                if not _tf_body_get_value(res["body"], "web_application_firewall_policy_link_id") and \
                   not _tf_body_get_value(res["body"], "firewall_policy_id"):
                    findings.append(self._finding(
                        "AZ0027", "Front Door without WAF policy",
                        f"Front Door '{res['name']}' does not have a WAF policy linked.",
                        IaCSeverity.HIGH, res,
                        "Link a WAF policy via web_application_firewall_policy_link_id or firewall_policy_id.",
                    ))
        return findings

    def _az0028_container_group_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_group":
                ip_type = _tf_body_get_value(res["body"], "ip_address_type")
                if ip_type and "public" in ip_type.lower():
                    findings.append(self._finding(
                        "AZ0028", "Container Instance publicly accessible",
                        f"Container Group '{res['name']}' has a public IP address.",
                        IaCSeverity.HIGH, res,
                        "Set ip_address_type = 'Private' and deploy into a virtual network.",
                    ))
        return findings

    def _az0029_logic_app_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_logic_app_standard":
                if _tf_body_has_key_value(res["body"], "https_only", "false"):
                    findings.append(self._finding(
                        "AZ0029", "Logic App allows HTTP",
                        f"Logic App '{res['name']}' does not enforce HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set https_only = true.",
                    ))
        return findings

    def _az0030_synapse_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_synapse_workspace":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0030", "Synapse Workspace publicly accessible",
                        f"Synapse Workspace '{res['name']}' allows public network access.",
                        IaCSeverity.CRITICAL, res,
                        "Set public_network_access_enabled = false and use managed private endpoints.",
                    ))
        return findings

    def _az0031_data_factory_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_data_factory":
                if _tf_body_has_key_value(res["body"], "public_network_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0031", "Data Factory publicly accessible",
                        f"Data Factory '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_enabled = false and use managed virtual network.",
                    ))
        return findings

    def _az0032_search_service_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_search_service":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0032", "Azure Cognitive Search publicly accessible",
                        f"Search Service '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0033_cognitive_account_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cognitive_account":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0033", "Cognitive Services publicly accessible",
                        f"Cognitive Account '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if not _tf_body_get_value(res["body"], "custom_subdomain_name"):
                    findings.append(self._finding(
                        "AZ0033", "Cognitive Services without custom subdomain",
                        f"Cognitive Account '{res['name']}' does not set a custom subdomain (required for private endpoints).",
                        IaCSeverity.MEDIUM, res,
                        "Set custom_subdomain_name to enable private endpoint and managed identity support.",
                    ))
        return findings

    def _az0034_mariadb_no_ssl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mariadb_server":
                if _tf_body_has_key_value(res["body"], "ssl_enforcement_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0034", "MariaDB without SSL enforcement",
                        f"MariaDB server '{res['name']}' does not enforce SSL connections.",
                        IaCSeverity.HIGH, res,
                        "Set ssl_enforcement_enabled = true.",
                    ))
        return findings

    def _az0035_mariadb_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mariadb_server":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0035", "MariaDB publicly accessible",
                        f"MariaDB server '{res['name']}' allows public network access.",
                        IaCSeverity.CRITICAL, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0036_batch_account_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_batch_account":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0036", "Batch Account publicly accessible",
                        f"Batch Account '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0037_api_management_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_api_management":
                body = res["body"]
                if _tf_body_has_block(body, "protocol_settings"):
                    if _tf_body_has_key_value(body, "enable_http2", "false"):
                        pass  # HTTP/2 is optional, not a security issue
                # Check for management API exposed over HTTP
                if _tf_body_has_key_value(body, "virtual_network_type", '"None"') or \
                   not _tf_body_get_value(body, "virtual_network_type"):
                    findings.append(self._finding(
                        "AZ0037", "API Management without virtual network integration",
                        f"API Management '{res['name']}' is not integrated with a virtual network.",
                        IaCSeverity.MEDIUM, res,
                        "Set virtual_network_type to 'Internal' or 'External' and configure virtual_network_configuration.",
                    ))
        return findings

    def _az0038_storage_no_network_rules(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                if not _tf_body_has_block(body, "network_rules"):
                    findings.append(self._finding(
                        "AZ0038", "Storage Account without network rules",
                        f"Storage Account '{res['name']}' has no network_rules block — defaults to allowing all networks.",
                        IaCSeverity.HIGH, res,
                        "Add a network_rules block with default_action = 'Deny' and explicit allow rules.",
                    ))
                elif _tf_body_has_key_value(body, "default_action", '"Allow"'):
                    findings.append(self._finding(
                        "AZ0038", "Storage Account network rules allow all",
                        f"Storage Account '{res['name']}' network_rules default_action is Allow.",
                        IaCSeverity.HIGH, res,
                        "Set default_action = 'Deny' in network_rules.",
                    ))
        return findings

    def _az0039_keyvault_no_network_acls(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                body = res["body"]
                if not _tf_body_has_block(body, "network_acls"):
                    findings.append(self._finding(
                        "AZ0039", "Key Vault without network ACLs",
                        f"Key Vault '{res['name']}' has no network_acls — defaults to allowing all networks.",
                        IaCSeverity.HIGH, res,
                        "Add a network_acls block with default_action = 'Deny'.",
                    ))
                elif _tf_body_has_key_value(body, "default_action", '"Allow"'):
                    findings.append(self._finding(
                        "AZ0039", "Key Vault network ACLs allow all",
                        f"Key Vault '{res['name']}' network_acls default_action is Allow.",
                        IaCSeverity.HIGH, res,
                        "Set default_action = 'Deny' in network_acls.",
                    ))
        return findings

    def _az0040_signalr_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_signalr_service", "azurerm_web_pubsub"):
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0040", "SignalR/Web PubSub publicly accessible",
                        f"Service '{res['name']}' ({res['type']}) allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0041_iothub_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_iothub":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0041", "IoT Hub publicly accessible",
                        f"IoT Hub '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if not _tf_body_get_value(res["body"], "min_tls_version"):
                    findings.append(self._finding(
                        "AZ0041", "IoT Hub without minimum TLS version",
                        f"IoT Hub '{res['name']}' does not specify a minimum TLS version.",
                        IaCSeverity.MEDIUM, res,
                        "Set min_tls_version = '1.2'.",
                    ))
        return findings

    def _az0042_webapp_min_tls(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_app_service", "azurerm_linux_web_app", "azurerm_windows_web_app"):
                tls_ver = _tf_body_get_value(res["body"], "min_tls_version")
                if tls_ver and tls_ver.strip('"') in ("1.0", "1.1"):
                    findings.append(self._finding(
                        "AZ0042", "Web App using outdated TLS version",
                        f"Web App '{res['name']}' minimum TLS version is {tls_ver.strip(chr(34))}.",
                        IaCSeverity.HIGH, res,
                        "Set min_tls_version = '1.2' in site_config.",
                    ))
        return findings

    def _az0043_aks_network_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_get_value(res["body"], "network_policy"):
                    if _tf_body_has_block(res["body"], "network_profile"):
                        findings.append(self._finding(
                            "AZ0043", "AKS without network policy",
                            f"AKS cluster '{res['name']}' does not have a network policy configured.",
                            IaCSeverity.MEDIUM, res,
                            "Set network_policy = 'azure' or 'calico' in network_profile.",
                        ))
        return findings

    def _az0044_dns_zone_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_private_dns = "azurerm_private_dns_zone" in resource_types
        public_dns_zones = [r for r in resources if r["type"] == "azurerm_dns_zone"]
        if public_dns_zones and not has_private_dns:
            for res in public_dns_zones:
                findings.append(self._finding(
                    "AZ0044", "Public DNS zone without private DNS complement",
                    f"DNS Zone '{res['name']}' is public with no azurerm_private_dns_zone detected.",
                    IaCSeverity.INFO, res,
                    "Consider using azurerm_private_dns_zone for internal service resolution.",
                ))
        return findings

    def _az0045_databricks_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_databricks_workspace":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0045", "Databricks Workspace publicly accessible",
                        f"Databricks Workspace '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and configure VNet injection.",
                    ))
                if not _tf_body_has_block(res["body"], "custom_parameters"):
                    findings.append(self._finding(
                        "AZ0045", "Databricks Workspace without VNet injection",
                        f"Databricks Workspace '{res['name']}' has no custom_parameters for VNet injection.",
                        IaCSeverity.MEDIUM, res,
                        "Add custom_parameters with virtual_network_id and private/public subnet details.",
                    ))
        return findings

    def _az0046_purview_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_purview_account":
                if _tf_body_has_key_value(res["body"], "public_network_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0046", "Purview Account publicly accessible",
                        f"Purview Account '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_enabled = false and use managed private endpoints.",
                    ))
        return findings

    def _az0047_container_app_insecure(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_app":
                if _tf_body_has_key_value(res["body"], "transport", '"http"'):
                    findings.append(self._finding(
                        "AZ0047", "Container App using HTTP transport",
                        f"Container App '{res['name']}' ingress uses HTTP transport.",
                        IaCSeverity.HIGH, res,
                        "Set transport = 'auto' or 'http2' in the ingress block for TLS.",
                    ))
                if _tf_body_has_key_value(res["body"], "allow_insecure_connections", "true"):
                    findings.append(self._finding(
                        "AZ0047", "Container App allows insecure connections",
                        f"Container App '{res['name']}' allows insecure (HTTP) connections.",
                        IaCSeverity.HIGH, res,
                        "Set allow_insecure_connections = false in the ingress block.",
                    ))
        return findings

    def _az0048_spring_cloud_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_spring_cloud_service", "azurerm_spring_cloud_app"):
                if res["type"] == "azurerm_spring_cloud_app":
                    if _tf_body_has_key_value(res["body"], "is_public", "true"):
                        findings.append(self._finding(
                            "AZ0048", "Spring Cloud App publicly accessible",
                            f"Spring Cloud App '{res['name']}' is publicly accessible.",
                            IaCSeverity.HIGH, res,
                            "Set is_public = false and use private endpoints or API gateway.",
                        ))
                if res["type"] == "azurerm_spring_cloud_service":
                    if not _tf_body_has_block(res["body"], "network"):
                        findings.append(self._finding(
                            "AZ0048", "Spring Cloud without VNet integration",
                            f"Spring Cloud Service '{res['name']}' has no network block for VNet injection.",
                            IaCSeverity.MEDIUM, res,
                            "Add a network block with service_runtime_subnet_id and app_subnet_id.",
                        ))
        return findings

    def _az0049_automation_account_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_automation_account":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0049", "Automation Account publicly accessible",
                        f"Automation Account '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0050_virtual_network_no_ddos(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network":
                if not _tf_body_has_block(res["body"], "ddos_protection_plan"):
                    findings.append(self._finding(
                        "AZ0050", "Virtual Network without DDoS protection",
                        f"Virtual Network '{res['name']}' does not have a DDoS protection plan.",
                        IaCSeverity.MEDIUM, res,
                        "Add a ddos_protection_plan block with enable = true.",
                    ))
        return findings

    def _az0051_firewall_no_threat_intel(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_firewall":
                mode = _tf_body_get_value(res["body"], "threat_intel_mode")
                if not mode or (mode and mode.strip('"').lower() == "off"):
                    findings.append(self._finding(
                        "AZ0051", "Azure Firewall threat intelligence disabled",
                        f"Firewall '{res['name']}' has threat intelligence mode off or unset.",
                        IaCSeverity.HIGH, res,
                        "Set threat_intel_mode = 'Alert' or 'Deny'.",
                    ))
        return findings

    def _az0052_express_route_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_express_route_circuit":
                sku_tier = _tf_body_get_value(res["body"], "tier")
                if sku_tier and "premium" not in sku_tier.lower():
                    findings.append(self._finding(
                        "AZ0052", "ExpressRoute without Premium tier",
                        f"ExpressRoute circuit '{res['name']}' is not using Premium tier (needed for Global Reach and MACsec).",
                        IaCSeverity.MEDIUM, res,
                        "Use Premium tier SKU for MACsec encryption and Global Reach support.",
                    ))
        return findings

    def _az0053_machine_learning_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_machine_learning_workspace":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0053", "Machine Learning Workspace publicly accessible",
                        f"ML Workspace '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if not _tf_body_get_value(res["body"], "high_business_impact"):
                    findings.append(self._finding(
                        "AZ0053", "Machine Learning Workspace without high business impact flag",
                        f"ML Workspace '{res['name']}' does not set high_business_impact for data encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set high_business_impact = true to enable additional encryption controls.",
                    ))
        return findings

    def _az0054_event_grid_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_eventgrid_topic", "azurerm_eventgrid_domain"):
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0054", "Event Grid publicly accessible",
                        f"Event Grid '{res['name']}' ({res['type']}) allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if not _tf_body_has_key_value(res["body"], "local_auth_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0054", "Event Grid local auth not disabled",
                        f"Event Grid '{res['name']}' has local (key-based) authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_auth_enabled = false and use Azure AD authentication.",
                    ))
        return findings

    def _az0055_stream_analytics_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_stream_analytics_job":
                if not _tf_body_get_value(res["body"], "content_storage_policy"):
                    findings.append(self._finding(
                        "AZ0055", "Stream Analytics without content storage policy",
                        f"Stream Analytics Job '{res['name']}' does not configure content_storage_policy.",
                        IaCSeverity.MEDIUM, res,
                        "Set content_storage_policy = 'JobStorageAccount' for data isolation.",
                    ))
        return findings

    def _az0056_hdinsight_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        hdi_types = (
            "azurerm_hdinsight_hadoop_cluster",
            "azurerm_hdinsight_spark_cluster",
            "azurerm_hdinsight_hbase_cluster",
            "azurerm_hdinsight_kafka_cluster",
            "azurerm_hdinsight_interactive_query_cluster",
        )
        for res in resources:
            if res["type"] in hdi_types:
                if not _tf_body_has_block(res["body"], "virtual_network"):
                    findings.append(self._finding(
                        "AZ0056", "HDInsight cluster without VNet",
                        f"HDInsight cluster '{res['name']}' is not deployed in a virtual network.",
                        IaCSeverity.HIGH, res,
                        "Add a virtual_network block with subnet_id for network isolation.",
                    ))
                if _tf_body_has_key_value(res["body"], "is_default", "true"):
                    # default storage using blob, not ADLS Gen2
                    pass
                tls = _tf_body_get_value(res["body"], "tls_min_version")
                if tls and tls.strip('"') in ("1.0", "1.1"):
                    findings.append(self._finding(
                        "AZ0056", "HDInsight cluster outdated TLS",
                        f"HDInsight cluster '{res['name']}' uses TLS version {tls.strip(chr(34))}.",
                        IaCSeverity.HIGH, res,
                        "Set tls_min_version = '1.2'.",
                    ))
        return findings

    def _az0057_notification_hub_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_notification_hub_namespace":
                sku = _tf_body_get_value(res["body"], "sku_name")
                if sku and "free" in sku.lower():
                    findings.append(self._finding(
                        "AZ0057", "Notification Hub on Free tier",
                        f"Notification Hub namespace '{res['name']}' uses the Free tier (no SLA or SAS policies).",
                        IaCSeverity.MEDIUM, res,
                        "Use Basic or Standard tier for production workloads with SAS authentication.",
                    ))
        return findings

    def _az0058_managed_identity_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        identity_resources = (
            "azurerm_linux_web_app", "azurerm_windows_web_app", "azurerm_app_service",
            "azurerm_linux_function_app", "azurerm_windows_function_app", "azurerm_function_app",
            "azurerm_container_app", "azurerm_logic_app_standard",
        )
        for res in resources:
            if res["type"] in identity_resources:
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding(
                        "AZ0058", "Azure resource without managed identity",
                        f"Resource '{res['name']}' ({res['type']}) does not configure a managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned' or 'UserAssigned'.",
                    ))
        return findings

    def _az0059_private_endpoint_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_private_endpoints = "azurerm_private_endpoint" in resource_types
        pe_candidates = [
            r for r in resources
            if r["type"] in (
                "azurerm_storage_account", "azurerm_mssql_server",
                "azurerm_cosmosdb_account", "azurerm_key_vault",
                "azurerm_container_registry",
            )
        ]
        if pe_candidates and not has_private_endpoints:
            for res in pe_candidates:
                findings.append(self._finding(
                    "AZ0059", "Azure resource without private endpoint",
                    f"Resource '{res['name']}' ({res['type']}) has no azurerm_private_endpoint configured.",
                    IaCSeverity.MEDIUM, res,
                    "Add an azurerm_private_endpoint resource for private connectivity.",
                ))
        return findings

    def _az0060_defender_for_cloud(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_defender = "azurerm_security_center_subscription_pricing" in resource_types
        critical_resources = [
            r for r in resources
            if r["type"] in (
                "azurerm_kubernetes_cluster", "azurerm_mssql_server",
                "azurerm_storage_account", "azurerm_key_vault",
            )
        ]
        if critical_resources and not has_defender:
            # Report once for the first resource found
            res = critical_resources[0]
            findings.append(self._finding(
                "AZ0060", "Microsoft Defender for Cloud not enabled",
                "No azurerm_security_center_subscription_pricing resource found to enable Defender plans.",
                IaCSeverity.MEDIUM, res,
                "Add azurerm_security_center_subscription_pricing resources for relevant resource types.",
            ))
        return findings

    def _az0061_vpn_gateway_no_aad(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_vpn_gateway":
                if not _tf_body_has_block(res["body"], "bgp_settings"):
                    findings.append(self._finding(
                        "AZ0061", "VPN Gateway without BGP settings",
                        f"VPN Gateway '{res['name']}' does not configure BGP settings.",
                        IaCSeverity.MEDIUM, res,
                        "Add a bgp_settings block for proper routing configuration.",
                    ))
            if res["type"] == "azurerm_point_to_site_vpn_gateway":
                if not _tf_body_has_block(res["body"], "connection_configuration"):
                    findings.append(self._finding(
                        "AZ0061", "Point-to-Site VPN Gateway misconfigured",
                        f"P2S VPN Gateway '{res['name']}' has no connection_configuration block.",
                        IaCSeverity.MEDIUM, res,
                        "Add a connection_configuration block with VPN client address pool.",
                    ))
        return findings

    def _az0062_nat_gateway_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_nat_gw = "azurerm_nat_gateway" in resource_types
        subnets = [r for r in resources if r["type"] == "azurerm_subnet"]
        public_vms = [
            r for r in resources
            if r["type"] == "azurerm_public_ip" and
            _tf_body_get_value(r["body"], "allocation_method")
        ]
        if subnets and public_vms and not has_nat_gw:
            findings.append(self._finding(
                "AZ0062", "No NAT Gateway for outbound traffic",
                "Subnets with public IPs detected but no azurerm_nat_gateway configured for centralized outbound.",
                IaCSeverity.MEDIUM, subnets[0],
                "Add an azurerm_nat_gateway and associate it with subnets via azurerm_subnet_nat_gateway_association.",
            ))
        return findings

    def _az0063_load_balancer_no_backend(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_lb":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "basic" in sku.lower():
                    findings.append(self._finding(
                        "AZ0063", "Load Balancer using Basic SKU",
                        f"Load Balancer '{res['name']}' uses Basic SKU (no SLA, no availability zones, no NSG support).",
                        IaCSeverity.MEDIUM, res,
                        "Upgrade to Standard SKU for zone redundancy, NSG support, and SLA.",
                    ))
        return findings

    def _az0064_traffic_manager_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_traffic_manager_profile":
                protocol = _tf_body_get_value(res["body"], "protocol")
                if protocol and "http" == protocol.strip('"').lower():
                    findings.append(self._finding(
                        "AZ0064", "Traffic Manager using HTTP health checks",
                        f"Traffic Manager '{res['name']}' monitor uses HTTP instead of HTTPS.",
                        IaCSeverity.MEDIUM, res,
                        "Set protocol = 'HTTPS' in the monitor_config block.",
                    ))
        return findings

    def _az0065_media_services_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_media_services_account":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0065", "Media Services publicly accessible",
                        f"Media Services Account '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
        return findings

    def _az0066_data_lake_store_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_data_lake_store":
                enc = _tf_body_get_value(res["body"], "encryption_state")
                if enc and "disabled" in enc.lower():
                    findings.append(self._finding(
                        "AZ0066", "Data Lake Store encryption disabled",
                        f"Data Lake Store '{res['name']}' has encryption disabled.",
                        IaCSeverity.CRITICAL, res,
                        "Set encryption_state = 'Enabled' (enabled by default).",
                    ))
                enc_type = _tf_body_get_value(res["body"], "encryption_type")
                if enc_type and "servicemanaged" in enc_type.lower():
                    findings.append(self._finding(
                        "AZ0066", "Data Lake Store using service-managed keys",
                        f"Data Lake Store '{res['name']}' uses service-managed keys instead of CMK.",
                        IaCSeverity.MEDIUM, res,
                        "Set encryption_type = 'UserManaged' and provide key vault key ID.",
                    ))
        return findings

    def _az0067_bastion_host_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_bastion = "azurerm_bastion_host" in resource_types
        vnets = [r for r in resources if r["type"] == "azurerm_virtual_network"]
        vms = [r for r in resources if r["type"] in (
            "azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine",
            "azurerm_virtual_machine",
        )]
        if vnets and vms and not has_bastion:
            findings.append(self._finding(
                "AZ0067", "No Azure Bastion Host for VM access",
                "Virtual machines detected but no azurerm_bastion_host for secure RDP/SSH access.",
                IaCSeverity.MEDIUM, vnets[0],
                "Deploy an azurerm_bastion_host in AzureBastionSubnet for secure VM access without public IPs.",
            ))
        return findings

    def _az0068_policy_assignment_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_policy = (
            "azurerm_policy_assignment" in resource_types
            or "azurerm_subscription_policy_assignment" in resource_types
            or "azurerm_resource_group_policy_assignment" in resource_types
            or "azurerm_management_group_policy_assignment" in resource_types
        )
        rg_count = sum(1 for r in resources if r["type"] == "azurerm_resource_group")
        if rg_count >= 1 and not has_policy:
            rg = next(r for r in resources if r["type"] == "azurerm_resource_group")
            findings.append(self._finding(
                "AZ0068", "No Azure Policy assignments detected",
                "Resource groups defined but no Azure Policy assignments found for governance.",
                IaCSeverity.INFO, rg,
                "Add azurerm_policy_assignment resources to enforce organizational standards.",
            ))
        return findings

    def _az0069_backup_vault_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_backup = (
            "azurerm_recovery_services_vault" in resource_types
            or "azurerm_backup_policy_vm" in resource_types
            or "azurerm_data_protection_backup_vault" in resource_types
        )
        vms = [r for r in resources if r["type"] in (
            "azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine",
            "azurerm_virtual_machine",
        )]
        if vms and not has_backup:
            findings.append(self._finding(
                "AZ0069", "No backup vault for virtual machines",
                "Virtual machines detected but no Recovery Services or Backup vault configured.",
                IaCSeverity.MEDIUM, vms[0],
                "Add azurerm_recovery_services_vault and azurerm_backup_protected_vm for VM backups.",
            ))
        return findings

    def _az0070_monitor_action_group_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_action_group = "azurerm_monitor_action_group" in resource_types
        has_alerts = (
            "azurerm_monitor_metric_alert" in resource_types
            or "azurerm_monitor_activity_log_alert" in resource_types
        )
        critical = [r for r in resources if r["type"] in (
            "azurerm_kubernetes_cluster", "azurerm_mssql_server",
            "azurerm_storage_account", "azurerm_key_vault",
        )]
        if critical and not has_action_group and not has_alerts:
            findings.append(self._finding(
                "AZ0070", "No Azure Monitor action groups or alerts",
                "Critical resources found but no azurerm_monitor_action_group or alert rules configured.",
                IaCSeverity.MEDIUM, critical[0],
                "Add azurerm_monitor_action_group and azurerm_monitor_metric_alert resources for monitoring.",
            ))
        return findings

    def _az0071_vm_disk_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                os_disk_enc = _tf_body_get_value(res["body"], "disk_encryption_set_id")
                if not os_disk_enc and not _tf_body_has_block(res["body"], "os_disk"):
                    findings.append(self._finding(
                        "AZ0071", "VM without disk encryption set",
                        f"Virtual Machine '{res['name']}' does not reference a disk_encryption_set_id.",
                        IaCSeverity.MEDIUM, res,
                        "Set disk_encryption_set_id on os_disk and data disks for CMK encryption.",
                    ))
                elif _tf_body_has_block(res["body"], "os_disk"):
                    if not _tf_body_get_value(res["body"], "disk_encryption_set_id"):
                        findings.append(self._finding(
                            "AZ0071", "VM OS disk without encryption set",
                            f"Virtual Machine '{res['name']}' os_disk does not use a disk_encryption_set_id.",
                            IaCSeverity.MEDIUM, res,
                            "Set disk_encryption_set_id in the os_disk block for customer-managed key encryption.",
                        ))
        return findings

    def _az0072_network_watcher_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        resource_types = {r["type"] for r in resources}
        has_watcher = "azurerm_network_watcher" in resource_types
        has_flow_log = "azurerm_network_watcher_flow_log" in resource_types
        nsgs = [r for r in resources if r["type"] in (
            "azurerm_network_security_group", "azurerm_network_security_rule",
        )]
        if nsgs and not has_watcher and not has_flow_log:
            findings.append(self._finding(
                "AZ0072", "No Network Watcher or NSG flow logs",
                "NSGs detected but no azurerm_network_watcher or azurerm_network_watcher_flow_log configured.",
                IaCSeverity.MEDIUM, nsgs[0],
                "Add azurerm_network_watcher and azurerm_network_watcher_flow_log for network monitoring.",
            ))
        return findings

    def _az0073_app_config_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_app_configuration":
                if _tf_body_has_key_value(res["body"], "public_network_access", '"Enabled"'):
                    findings.append(self._finding(
                        "AZ0073", "App Configuration publicly accessible",
                        f"App Configuration '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access = 'Disabled' and use private endpoints.",
                    ))
                if _tf_body_has_key_value(res["body"], "local_auth_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0073", "App Configuration local auth enabled",
                        f"App Configuration '{res['name']}' has local (access key) authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_auth_enabled = false and use Azure AD authentication.",
                    ))
        return findings

    def _az0074_maps_account_cors(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_maps_account":
                if not _tf_body_has_key_value(res["body"], "local_authentication_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0074", "Azure Maps local authentication enabled",
                        f"Maps Account '{res['name']}' has local (shared key) authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_authentication_enabled = false and use Azure AD authentication.",
                    ))
        return findings

    def _az0075_cdn_endpoint_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cdn_endpoint":
                if _tf_body_has_key_value(res["body"], "is_http_allowed", "true"):
                    findings.append(self._finding(
                        "AZ0075", "CDN endpoint allows HTTP",
                        f"CDN Endpoint '{res['name']}' allows HTTP traffic.",
                        IaCSeverity.HIGH, res,
                        "Set is_http_allowed = false to enforce HTTPS-only traffic.",
                    ))
        return findings

    def _az0076_container_app_env_internal(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_app_environment":
                if not _tf_body_has_key_value(res["body"], "internal_load_balancer_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0076", "Container App Environment not internal",
                        f"Container App Environment '{res['name']}' does not use an internal load balancer.",
                        IaCSeverity.MEDIUM, res,
                        "Set internal_load_balancer_enabled = true for private-only ingress.",
                    ))
        return findings

    def _az0077_service_fabric_no_aad(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_service_fabric_cluster":
                if not _tf_body_has_block(res["body"], "azure_active_directory"):
                    findings.append(self._finding(
                        "AZ0077", "Service Fabric without Azure AD authentication",
                        f"Service Fabric cluster '{res['name']}' does not configure Azure AD authentication.",
                        IaCSeverity.HIGH, res,
                        "Add an azure_active_directory block for cluster authentication.",
                    ))
                security = _tf_body_get_value(res["body"], "reliability_level")
                if security and security.strip('"').lower() == "none":
                    findings.append(self._finding(
                        "AZ0077", "Service Fabric with no reliability level",
                        f"Service Fabric cluster '{res['name']}' has reliability_level set to None.",
                        IaCSeverity.HIGH, res,
                        "Set reliability_level to Bronze, Silver, Gold, or Platinum.",
                    ))
        return findings

    def _az0078_digital_twins_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_digital_twins_instance":
                if not _tf_body_has_block(res["body"], "private_endpoint"):
                    findings.append(self._finding(
                        "AZ0078", "Digital Twins without private endpoint",
                        f"Digital Twins instance '{res['name']}' does not configure a private endpoint.",
                        IaCSeverity.HIGH, res,
                        "Add a private endpoint for secure access to the Digital Twins instance.",
                    ))
        return findings

    def _az0079_openai_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cognitive_account":
                kind = _tf_body_get_value(res["body"], "kind")
                if kind and "openai" in kind.lower():
                    if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                        findings.append(self._finding(
                            "AZ0079", "Azure OpenAI publicly accessible",
                            f"Azure OpenAI account '{res['name']}' allows public network access.",
                            IaCSeverity.CRITICAL, res,
                            "Set public_network_access_enabled = false and use private endpoints.",
                        ))
                    if not _tf_body_has_block(res["body"], "network_acls"):
                        findings.append(self._finding(
                            "AZ0079", "Azure OpenAI without network ACLs",
                            f"Azure OpenAI account '{res['name']}' has no network_acls block.",
                            IaCSeverity.HIGH, res,
                            "Add a network_acls block with default_action = 'Deny'.",
                        ))
        return findings

    def _az0080_managed_grafana_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_dashboard_grafana":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0080", "Managed Grafana publicly accessible",
                        f"Managed Grafana '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if _tf_body_has_key_value(res["body"], "api_key_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0080", "Managed Grafana API key authentication enabled",
                        f"Managed Grafana '{res['name']}' has API key authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set api_key_enabled = false and use Azure AD authentication.",
                    ))
        return findings

    def _az0081_data_factory_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0081 – Azure Data Factory publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_data_factory":
                if _tf_body_has_key_value(res["body"], "public_network_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0081", "Data Factory publicly accessible",
                        f"Data Factory '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_enabled = false and use private endpoints.",
                    ))
                if not _tf_body_get_value(res["body"], "managed_virtual_network_enabled"):
                    findings.append(self._finding(
                        "AZ0081", "Data Factory without managed virtual network",
                        f"Data Factory '{res['name']}' does not use managed virtual network.",
                        IaCSeverity.MEDIUM, res,
                        "Set managed_virtual_network_enabled = true.",
                    ))
        return findings

    def _az0082_logic_app_no_managed_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0082 – Azure Logic App without managed identity."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_logic_app_standard", "azurerm_logic_app_workflow"):
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding(
                        "AZ0082", "Logic App without managed identity",
                        f"Logic App '{res['name']}' has no managed identity configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0083_api_management_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0083 – Azure API Management without HTTPS enforcement."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_api_management":
                body = res["body"]
                if _tf_body_has_key_value(body, "sku_name", '"Consumption"'):
                    pass  # Consumption tier is HTTPS-only by default
                else:
                    # Check for protocols block
                    if _tf_body_has_key_value(body, "enable_http2", "false"):
                        findings.append(self._finding(
                            "AZ0083", "API Management HTTP/2 disabled",
                            f"API Management '{res['name']}' has HTTP/2 disabled.",
                            IaCSeverity.LOW, res,
                            "Set enable_http2 = true for improved performance.",
                        ))
            if res["type"] == "azurerm_api_management_api":
                body = res["body"]
                protocols = _tf_body_get_value(body, "protocols")
                if protocols and "http" in protocols.lower() and "https" not in protocols.lower():
                    findings.append(self._finding(
                        "AZ0083", "API Management API allows HTTP",
                        f"API Management API '{res['name']}' allows insecure HTTP protocol.",
                        IaCSeverity.HIGH, res,
                        "Set protocols = ['https'] to enforce HTTPS only.",
                    ))
        return findings

    def _az0084_synapse_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0084 – Azure Synapse Analytics publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_synapse_workspace":
                body = res["body"]
                if _tf_body_has_key_value(body, "managed_virtual_network_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0084", "Synapse without managed virtual network",
                        f"Synapse workspace '{res['name']}' does not use managed virtual network.",
                        IaCSeverity.HIGH, res,
                        "Set managed_virtual_network_enabled = true.",
                    ))
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0084", "Synapse publicly accessible",
                        f"Synapse workspace '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
            if res["type"] == "azurerm_synapse_sql_pool":
                if not _tf_body_get_value(res["body"], "data_encrypted"):
                    findings.append(self._finding(
                        "AZ0084", "Synapse SQL pool without TDE",
                        f"Synapse SQL pool '{res['name']}' may not have TDE enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set data_encrypted = true for transparent data encryption.",
                    ))
        return findings

    def _az0085_stream_analytics_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0085 – Azure Stream Analytics without managed identity."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_stream_analytics_job":
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding(
                        "AZ0085", "Stream Analytics without managed identity",
                        f"Stream Analytics job '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0086_purview_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0086 – Azure Purview (Microsoft Purview) publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_purview_account":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0086", "Purview account publicly accessible",
                        f"Purview account '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_enabled = false and use private endpoints.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0086", "Purview account without managed identity",
                        f"Purview account '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0087_batch_account_no_private(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0087 – Azure Batch account without private endpoint."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_batch_account":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0087", "Batch account publicly accessible",
                        f"Batch account '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                pool_alloc = _tf_body_get_value(body, "pool_allocation_mode")
                if pool_alloc and "UserSubscription" not in pool_alloc:
                    if not _tf_body_has_block(body, "encryption"):
                        findings.append(self._finding(
                            "AZ0087", "Batch account without encryption configuration",
                            f"Batch account '{res['name']}' has no encryption block.",
                            IaCSeverity.MEDIUM, res,
                            "Add an encryption block with customer-managed key.",
                        ))
        return findings

    def _az0088_notification_hub_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0088 – Azure Notification Hub namespace misconfiguration."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_notification_hub_namespace":
                body = res["body"]
                if not _tf_body_has_key_value(body, "enabled", "true"):
                    findings.append(self._finding(
                        "AZ0088", "Notification Hub namespace disabled",
                        f"Notification Hub namespace '{res['name']}' is not enabled.",
                        IaCSeverity.LOW, res,
                        "Set enabled = true.",
                    ))
                sku = _tf_body_get_value(body, "sku_name")
                if sku and "Free" in sku:
                    findings.append(self._finding(
                        "AZ0088", "Notification Hub namespace on Free tier",
                        f"Notification Hub namespace '{res['name']}' uses Free SKU (no SLA).",
                        IaCSeverity.LOW, res,
                        "Consider Standard or Basic SKU for production workloads.",
                    ))
        return findings

    def _az0089_signalr_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0089 – Azure SignalR Service publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_signalr_service":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0089", "SignalR Service publicly accessible",
                        f"SignalR Service '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0089", "SignalR Service without managed identity",
                        f"SignalR Service '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0090_static_web_app_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0090 – Azure Static Web App without authentication."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_static_web_app":
                body = res["body"]
                sku = _tf_body_get_value(body, "sku_tier")
                if sku and "Free" in sku:
                    findings.append(self._finding(
                        "AZ0090", "Static Web App on Free tier",
                        f"Static Web App '{res['name']}' uses Free tier (limited features).",
                        IaCSeverity.LOW, res,
                        "Consider Standard tier for production workloads with custom auth.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0090", "Static Web App without managed identity",
                        f"Static Web App '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
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
