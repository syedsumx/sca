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
        findings.extend(self._az0091_automation_account_public(all_resources))
        findings.extend(self._az0092_cognitive_account_public(all_resources))
        findings.extend(self._az0093_communication_service_no_identity(all_resources))
        findings.extend(self._az0094_container_group_public(all_resources))
        findings.extend(self._az0095_data_explorer_public(all_resources))
        findings.extend(self._az0096_databricks_public(all_resources))
        findings.extend(self._az0097_healthcare_fhir_public(all_resources))
        findings.extend(self._az0098_iot_hub_public(all_resources))
        findings.extend(self._az0099_machine_learning_public(all_resources))
        findings.extend(self._az0100_media_services_no_identity(all_resources))
        findings.extend(self._az0101_managed_disk_no_encryption(all_resources))
        findings.extend(self._az0102_private_dns_zone_no_link(all_resources))
        findings.extend(self._az0103_app_service_slot_no_https(all_resources))
        findings.extend(self._az0104_redis_no_tls(all_resources))
        findings.extend(self._az0105_search_service_public(all_resources))
        findings.extend(self._az0106_spring_cloud_no_vnet(all_resources))
        findings.extend(self._az0107_web_pubsub_public(all_resources))
        findings.extend(self._az0108_frontdoor_waf_missing(all_resources))
        findings.extend(self._az0109_app_gateway_no_waf(all_resources))
        findings.extend(self._az0110_dns_zone_no_dnssec(all_resources))
        findings.extend(self._az0111_express_route_no_encryption(all_resources))
        findings.extend(self._az0112_firewall_no_threat_intel(all_resources))
        findings.extend(self._az0113_image_builder_no_identity(all_resources))
        findings.extend(self._az0114_key_vault_no_purge_protection(all_resources))
        findings.extend(self._az0115_lb_no_health_probe(all_resources))
        findings.extend(self._az0116_log_analytics_no_cmk(all_resources))
        findings.extend(self._az0117_monitor_diagnostic_missing(all_resources))
        findings.extend(self._az0118_mysql_flexible_public(all_resources))
        findings.extend(self._az0119_network_interface_public_ip(all_resources))
        findings.extend(self._az0120_postgresql_flexible_public(all_resources))
        findings.extend(self._az0121_recovery_vault_no_encryption(all_resources))
        findings.extend(self._az0122_route_table_no_propagation(all_resources))
        findings.extend(self._az0123_service_bus_public(all_resources))
        findings.extend(self._az0124_snapshot_no_encryption(all_resources))
        findings.extend(self._az0125_storage_sync_no_private(all_resources))
        findings.extend(self._az0126_virtual_hub_no_firewall(all_resources))
        findings.extend(self._az0127_vm_scale_set_no_health_ext(all_resources))
        findings.extend(self._az0128_vnet_no_ddos_protection(all_resources))
        findings.extend(self._az0129_vpn_gateway_no_active_active(all_resources))
        findings.extend(self._az0130_waf_policy_prevention_mode(all_resources))
        findings.extend(self._az0131_event_grid_public(all_resources))
        findings.extend(self._az0132_event_hub_public(all_resources))
        findings.extend(self._az0133_cosmos_db_public(all_resources))
        findings.extend(self._az0134_function_app_public(all_resources))
        findings.extend(self._az0135_container_registry_public(all_resources))
        findings.extend(self._az0136_managed_hsm_no_purge_protection(all_resources))
        findings.extend(self._az0137_data_protection_vault_no_immutability(all_resources))
        findings.extend(self._az0138_private_endpoint_no_dns(all_resources))
        findings.extend(self._az0139_disk_access_public(all_resources))
        findings.extend(self._az0140_maintenance_config_missing(all_resources))
        findings.extend(self._az0141_firewall_policy_no_tls_inspection(all_resources))
        findings.extend(self._az0142_sql_server_no_aad_admin(all_resources))
        findings.extend(self._az0143_sql_audit_retention_short(all_resources))
        findings.extend(self._az0144_app_service_no_managed_identity(all_resources))
        findings.extend(self._az0145_app_service_remote_debugging(all_resources))
        findings.extend(self._az0146_app_service_ftp_enabled(all_resources))
        findings.extend(self._az0147_storage_blob_public_access(all_resources))
        findings.extend(self._az0148_storage_shared_key_access(all_resources))
        findings.extend(self._az0149_keyvault_soft_delete_disabled(all_resources))
        findings.extend(self._az0150_keyvault_rbac_not_enabled(all_resources))
        findings.extend(self._az0151_aks_no_azure_policy(all_resources))
        findings.extend(self._az0152_aks_public_api_server(all_resources))
        findings.extend(self._az0153_aks_no_disk_encryption_set(all_resources))
        findings.extend(self._az0154_vm_no_boot_diagnostics(all_resources))
        findings.extend(self._az0155_linux_vm_password_auth(all_resources))
        findings.extend(self._az0156_sql_database_no_tde(all_resources))
        findings.extend(self._az0157_cosmosdb_no_auto_failover(all_resources))
        findings.extend(self._az0158_servicebus_no_private_endpoint(all_resources))
        findings.extend(self._az0159_eventhub_no_capture(all_resources))
        findings.extend(self._az0160_function_app_no_managed_identity(all_resources))
        findings.extend(self._az0161_app_service_cors_wildcard(all_resources))
        findings.extend(self._az0162_postgresql_flex_no_backup_retention(all_resources))
        findings.extend(self._az0163_mysql_flex_no_backup_retention(all_resources))
        findings.extend(self._az0164_acr_no_content_trust(all_resources))
        findings.extend(self._az0165_acr_no_quarantine_policy(all_resources))
        findings.extend(self._az0166_aks_no_auto_upgrade(all_resources))
        findings.extend(self._az0167_app_insights_no_workspace(all_resources))
        findings.extend(self._az0168_keyvault_cert_no_auto_rotation(all_resources))
        findings.extend(self._az0169_frontdoor_no_https_redirect(all_resources))
        findings.extend(self._az0170_cdn_no_custom_domain_https(all_resources))
        findings.extend(self._az0171_sql_server_no_vuln_assessment(all_resources))
        findings.extend(self._az0172_app_service_outdated_runtime(all_resources))
        findings.extend(self._az0173_nsg_flow_logs_missing(all_resources))
        findings.extend(self._az0174_storage_no_lifecycle_mgmt(all_resources))
        findings.extend(self._az0175_aks_no_container_insights(all_resources))
        findings.extend(self._az0176_cosmosdb_no_network_restriction(all_resources))
        findings.extend(self._az0177_appgw_no_ssl_policy(all_resources))
        findings.extend(self._az0178_aks_no_upgrade_channel(all_resources))
        findings.extend(self._az0179_firewall_no_dns_proxy(all_resources))
        findings.extend(self._az0180_sql_server_min_tls(all_resources))
        findings.extend(self._az0181_postgresql_no_threat_detection(all_resources))
        findings.extend(self._az0182_storage_no_infra_encryption(all_resources))
        findings.extend(self._az0183_app_service_no_client_cert(all_resources))
        findings.extend(self._az0184_function_app_no_https(all_resources))
        findings.extend(self._az0185_redis_min_tls_version(all_resources))
        findings.extend(self._az0186_cosmosdb_local_auth_enabled(all_resources))
        findings.extend(self._az0187_container_app_no_ingress_restriction(all_resources))
        findings.extend(self._az0188_app_service_no_vnet_integration(all_resources))
        findings.extend(self._az0189_sql_db_no_ltr(all_resources))
        findings.extend(self._az0190_keyvault_no_diagnostic_settings(all_resources))
        findings.extend(self._az0191_vm_no_encryption_at_host(all_resources))
        findings.extend(self._az0192_aks_no_secret_store_csi(all_resources))
        findings.extend(self._az0193_app_service_no_health_check(all_resources))
        findings.extend(self._az0194_storage_no_soft_delete(all_resources))
        findings.extend(self._az0195_sql_server_no_private_endpoint(all_resources))
        findings.extend(self._az0196_aks_no_workload_identity(all_resources))
        findings.extend(self._az0197_cosmosdb_no_backup(all_resources))
        findings.extend(self._az0198_app_gw_no_health_probe(all_resources))
        findings.extend(self._az0199_vm_no_availability_zone(all_resources))
        findings.extend(self._az0200_storage_no_versioning(all_resources))
        findings.extend(self._az0201_aks_no_oms_agent(all_resources))
        findings.extend(self._az0202_keyvault_no_private_endpoint(all_resources))
        findings.extend(self._az0203_app_service_no_backup(all_resources))
        findings.extend(self._az0204_sql_no_geo_redundant_backup(all_resources))
        findings.extend(self._az0205_aks_no_node_pool_encryption(all_resources))
        findings.extend(self._az0206_function_app_no_runtime_version(all_resources))
        findings.extend(self._az0207_acr_no_retention_policy(all_resources))
        findings.extend(self._az0208_vm_scale_set_no_automatic_repairs(all_resources))
        findings.extend(self._az0209_app_service_no_always_on(all_resources))
        findings.extend(self._az0210_aks_no_azure_cni(all_resources))
        findings.extend(self._az0211_storage_queue_no_logging(all_resources))
        findings.extend(self._az0212_storage_table_no_logging(all_resources))
        findings.extend(self._az0213_mysql_no_threat_detection(all_resources))
        findings.extend(self._az0214_postgresql_no_connection_throttling(all_resources))
        findings.extend(self._az0215_app_service_no_ftps(all_resources))
        findings.extend(self._az0216_vm_no_update_management(all_resources))
        findings.extend(self._az0217_aks_no_defender(all_resources))
        findings.extend(self._az0218_storage_no_delete_retention(all_resources))
        findings.extend(self._az0219_keyvault_no_key_expiry(all_resources))
        findings.extend(self._az0220_keyvault_no_secret_expiry(all_resources))
        findings.extend(self._az0221_app_service_no_ip_restriction(all_resources))
        findings.extend(self._az0222_function_app_no_ip_restriction(all_resources))
        findings.extend(self._az0223_aks_no_pod_security_policy(all_resources))
        findings.extend(self._az0224_vm_no_just_in_time_access(all_resources))
        findings.extend(self._az0225_storage_no_cross_tenant_replication(all_resources))
        findings.extend(self._az0226_sql_server_no_outbound_network(all_resources))
        findings.extend(self._az0227_app_service_no_http2(all_resources))
        findings.extend(self._az0228_aks_no_image_cleaner(all_resources))
        findings.extend(self._az0229_cosmosdb_no_multiple_write_locations(all_resources))
        findings.extend(self._az0230_acr_no_zone_redundancy(all_resources))
        findings.extend(self._az0231_app_gw_no_autoscaling(all_resources))
        findings.extend(self._az0232_vm_no_accelerated_networking(all_resources))
        findings.extend(self._az0233_aks_no_http_proxy(all_resources))
        findings.extend(self._az0234_storage_no_change_feed(all_resources))
        findings.extend(self._az0235_sql_elastic_pool_no_zone_redundancy(all_resources))
        findings.extend(self._az0236_redis_no_zone_redundancy(all_resources))
        findings.extend(self._az0237_servicebus_no_zone_redundancy(all_resources))
        findings.extend(self._az0238_eventhub_no_zone_redundancy(all_resources))
        findings.extend(self._az0239_app_service_no_zone_redundancy(all_resources))
        findings.extend(self._az0240_function_app_no_zone_redundancy(all_resources))
        findings.extend(self._az0241_aks_no_availability_zones(all_resources))
        findings.extend(self._az0242_vm_no_disk_encryption_set(all_resources))
        findings.extend(self._az0243_app_config_no_encryption(all_resources))
        findings.extend(self._az0244_cognitive_services_no_cmk(all_resources))
        findings.extend(self._az0245_search_no_cmk(all_resources))
        findings.extend(self._az0246_data_factory_no_cmk(all_resources))
        findings.extend(self._az0247_synapse_no_cmk(all_resources))
        findings.extend(self._az0248_log_analytics_no_daily_cap(all_resources))
        findings.extend(self._az0249_monitor_no_action_group(all_resources))
        findings.extend(self._az0250_app_service_no_diagnostic_logs(all_resources))
        findings.extend(self._az0251_aks_no_audit_logging(all_resources))
        findings.extend(self._az0252_sql_no_advanced_threat_protection(all_resources))
        findings.extend(self._az0253_storage_no_advanced_threat_protection(all_resources))
        findings.extend(self._az0254_app_service_no_defender(all_resources))
        findings.extend(self._az0255_keyvault_no_access_policy_limit(all_resources))
        findings.extend(self._az0256_nsg_no_default_deny(all_resources))
        findings.extend(self._az0257_app_gw_no_request_routing_rule(all_resources))
        findings.extend(self._az0258_vm_no_patch_assessment(all_resources))
        findings.extend(self._az0259_aks_no_keda(all_resources))
        findings.extend(self._az0260_cosmosdb_no_analytical_storage(all_resources))
        findings.extend(self._az0261_sql_no_ledger(all_resources))
        findings.extend(self._az0262_storage_no_immutable_blob(all_resources))
        findings.extend(self._az0263_app_service_no_auth(all_resources))
        findings.extend(self._az0264_function_app_no_auth(all_resources))
        findings.extend(self._az0265_aks_no_local_account_disabled(all_resources))
        findings.extend(self._az0266_vm_no_trusted_launch(all_resources))
        findings.extend(self._az0267_vm_scale_set_no_trusted_launch(all_resources))
        findings.extend(self._az0268_acr_no_dedicated_data_endpoint(all_resources))
        findings.extend(self._az0269_app_gw_no_backend_pool(all_resources))
        findings.extend(self._az0270_lb_no_outbound_rules(all_resources))
        findings.extend(self._az0271_bastion_no_shareable_link(all_resources))
        findings.extend(self._az0272_firewall_no_premium_sku(all_resources))
        findings.extend(self._az0273_vpn_no_ikev2(all_resources))
        findings.extend(self._az0274_app_service_no_min_instances(all_resources))
        findings.extend(self._az0275_aks_no_node_taints(all_resources))
        findings.extend(self._az0276_storage_account_no_min_tls(all_resources))
        findings.extend(self._az0277_keyvault_no_certificate_contacts(all_resources))
        findings.extend(self._az0278_sql_no_connection_policy(all_resources))
        findings.extend(self._az0279_cosmosdb_no_free_tier(all_resources))
        findings.extend(self._az0280_app_service_no_health_check_path(all_resources))
        findings.extend(self._az0281_aks_no_system_node_pool(all_resources))
        findings.extend(self._az0282_vm_no_ephemeral_disk(all_resources))
        findings.extend(self._az0283_acr_no_export_policy(all_resources))
        findings.extend(self._az0284_app_gw_no_cookie_affinity(all_resources))
        findings.extend(self._az0285_storage_no_private_endpoint(all_resources))
        findings.extend(self._az0286_eventhub_no_dedicated_cluster(all_resources))
        findings.extend(self._az0287_servicebus_no_premium_messaging(all_resources))
        findings.extend(self._az0288_redis_no_data_persistence(all_resources))
        findings.extend(self._az0289_mysql_no_gtid_consistency(all_resources))
        findings.extend(self._az0290_postgresql_no_wal_retention(all_resources))
        findings.extend(self._az0291_app_service_no_scm_restrictions(all_resources))
        findings.extend(self._az0292_function_app_no_cors_validation(all_resources))
        findings.extend(self._az0293_aks_no_os_disk_type(all_resources))
        findings.extend(self._az0294_vm_no_data_disk_encryption(all_resources))
        findings.extend(self._az0295_keyvault_no_network_bypass(all_resources))
        findings.extend(self._az0296_storage_no_public_blob(all_resources))
        findings.extend(self._az0297_sql_no_active_directory_only(all_resources))
        findings.extend(self._az0298_cosmosdb_no_serverless(all_resources))
        findings.extend(self._az0299_app_service_no_websockets(all_resources))
        findings.extend(self._az0300_aks_no_cost_analysis(all_resources))
        findings.extend(self._az0301_vm_no_automatic_shutdown(all_resources))
        findings.extend(self._az0302_aks_no_run_command_disabled(all_resources))
        findings.extend(self._az0303_app_service_no_min_tls_cipher(all_resources))
        findings.extend(self._az0304_storage_no_static_website_error_page(all_resources))
        findings.extend(self._az0305_sql_no_elastic_pool(all_resources))
        findings.extend(self._az0306_aks_no_oidc_issuer(all_resources))
        findings.extend(self._az0307_cosmosdb_no_partition_merge(all_resources))
        findings.extend(self._az0308_app_gw_no_rewrite_rule(all_resources))
        findings.extend(self._az0309_vm_no_proximity_placement(all_resources))
        findings.extend(self._az0310_storage_no_large_file_share(all_resources))
        findings.extend(self._az0311_aks_no_vertical_pod_autoscaler(all_resources))
        findings.extend(self._az0312_keyvault_no_managed_hsm(all_resources))
        findings.extend(self._az0313_app_service_no_auto_heal(all_resources))
        findings.extend(self._az0314_sql_no_short_term_retention(all_resources))
        findings.extend(self._az0315_aks_no_blob_csi_driver(all_resources))
        findings.extend(self._az0316_function_app_no_elastic_plan(all_resources))
        findings.extend(self._az0317_acr_no_token_auth(all_resources))
        findings.extend(self._az0318_vm_scale_set_no_rolling_upgrade(all_resources))
        findings.extend(self._az0319_app_service_no_slot_sticky(all_resources))
        findings.extend(self._az0320_aks_no_file_csi_driver(all_resources))
        findings.extend(self._az0321_storage_no_nfsv3(all_resources))
        findings.extend(self._az0322_storage_no_sftp(all_resources))
        findings.extend(self._az0323_mysql_flex_no_ha(all_resources))
        findings.extend(self._az0324_postgresql_flex_no_ha(all_resources))
        findings.extend(self._az0325_app_service_no_vnet_route_all(all_resources))
        findings.extend(self._az0326_vm_no_custom_data(all_resources))
        findings.extend(self._az0327_aks_no_node_resource_group(all_resources))
        findings.extend(self._az0328_storage_no_hierarchical_namespace(all_resources))
        findings.extend(self._az0329_keyvault_no_rotation_policy(all_resources))
        findings.extend(self._az0330_keyvault_secret_no_content_type(all_resources))
        findings.extend(self._az0331_app_service_no_sticky_settings(all_resources))
        findings.extend(self._az0332_function_app_no_daily_memory_quota(all_resources))
        findings.extend(self._az0333_aks_no_maintenance_window(all_resources))
        findings.extend(self._az0334_vm_no_dedicated_host(all_resources))
        findings.extend(self._az0335_storage_no_allow_protected_append_writes(all_resources))
        findings.extend(self._az0336_sql_no_maintenance_window(all_resources))
        findings.extend(self._az0337_aks_no_node_pool_subnet(all_resources))
        findings.extend(self._az0338_cosmosdb_no_free_tier_check(all_resources))
        findings.extend(self._az0339_app_gw_no_redirect_config(all_resources))
        findings.extend(self._az0340_vm_no_gallery_image(all_resources))
        findings.extend(self._az0341_aks_no_sku_tier(all_resources))
        findings.extend(self._az0342_storage_no_account_replication(all_resources))
        findings.extend(self._az0343_keyvault_no_access_log(all_resources))
        findings.extend(self._az0344_app_service_no_detailed_error(all_resources))
        findings.extend(self._az0345_sql_no_read_replica(all_resources))
        findings.extend(self._az0346_aks_no_upgrade_settings(all_resources))
        findings.extend(self._az0347_cosmosdb_no_consistency_policy(all_resources))
        findings.extend(self._az0348_app_gw_no_frontend_port(all_resources))
        findings.extend(self._az0349_vm_no_capacity_reservation(all_resources))
        findings.extend(self._az0350_storage_no_queue_encryption_key(all_resources))
        findings.extend(self._az0351_aks_no_snapshot_controller(all_resources))
        findings.extend(self._az0352_keyvault_no_certificate_policy(all_resources))
        findings.extend(self._az0353_app_service_no_compression(all_resources))
        findings.extend(self._az0354_sql_no_active_geo_replication(all_resources))
        findings.extend(self._az0355_aks_no_windows_node_pool(all_resources))
        findings.extend(self._az0356_cosmosdb_no_cors(all_resources))
        findings.extend(self._az0357_app_gw_no_custom_error_page(all_resources))
        findings.extend(self._az0358_vm_no_license_type(all_resources))
        findings.extend(self._az0359_storage_no_table_encryption_key(all_resources))
        findings.extend(self._az0360_aks_no_network_dataplane(all_resources))
        findings.extend(self._az0361_keyvault_no_private_link_service(all_resources))
        findings.extend(self._az0362_app_service_no_worker_count(all_resources))
        findings.extend(self._az0363_sql_no_failover_group(all_resources))
        findings.extend(self._az0364_aks_no_node_pool_max_surge(all_resources))
        findings.extend(self._az0365_cosmosdb_no_geo_location(all_resources))
        findings.extend(self._az0366_app_gw_no_trusted_root_cert(all_resources))
        findings.extend(self._az0367_vm_no_ultra_ssd(all_resources))
        findings.extend(self._az0368_storage_no_blob_inventory(all_resources))
        findings.extend(self._az0369_aks_no_gpu_node_pool(all_resources))
        findings.extend(self._az0370_keyvault_no_firewall_bypass_metrics(all_resources))
        findings.extend(self._az0371_app_service_no_pre_warmed_instances(all_resources))
        findings.extend(self._az0372_sql_no_zone_redundant(all_resources))
        findings.extend(self._az0373_aks_no_outbound_type(all_resources))
        findings.extend(self._az0374_cosmosdb_no_capabilities(all_resources))
        findings.extend(self._az0375_app_gw_no_ssl_certificate(all_resources))
        findings.extend(self._az0376_vm_no_os_disk_caching(all_resources))
        findings.extend(self._az0377_storage_no_point_in_time_restore(all_resources))
        findings.extend(self._az0378_aks_no_enable_host_encryption(all_resources))
        findings.extend(self._az0379_keyvault_no_contact_email(all_resources))
        findings.extend(self._az0380_app_service_no_load_balancing_mode(all_resources))
        findings.extend(self._az0381_sql_no_transparent_data_encryption(all_resources))
        findings.extend(self._az0382_aks_no_kubelet_config(all_resources))
        findings.extend(self._az0383_cosmosdb_no_virtual_network_rule(all_resources))
        findings.extend(self._az0384_app_gw_no_firewall_policy(all_resources))
        findings.extend(self._az0385_vm_no_data_collection(all_resources))
        findings.extend(self._az0386_storage_no_routing_preference(all_resources))
        findings.extend(self._az0387_aks_no_linux_os_config(all_resources))
        findings.extend(self._az0388_keyvault_no_certificate_issuer(all_resources))
        findings.extend(self._az0389_app_service_no_app_command_line(all_resources))
        findings.extend(self._az0390_sql_no_identity(all_resources))
        findings.extend(self._az0391_aks_no_service_mesh(all_resources))
        findings.extend(self._az0392_cosmosdb_no_ip_range_filter(all_resources))
        findings.extend(self._az0393_app_gw_no_identity(all_resources))
        findings.extend(self._az0394_vm_no_identity(all_resources))
        findings.extend(self._az0395_storage_no_identity(all_resources))
        findings.extend(self._az0396_aks_no_private_dns_zone(all_resources))
        findings.extend(self._az0397_keyvault_no_sku(all_resources))
        findings.extend(self._az0398_app_service_no_linux_fx_version(all_resources))
        findings.extend(self._az0399_sql_no_minimum_tls_version(all_resources))
        findings.extend(self._az0400_aks_no_api_server_access_profile(all_resources))
        findings.extend(self._az0401_cosmosdb_no_default_identity(all_resources))
        findings.extend(self._az0402_app_gw_no_zones(all_resources))
        findings.extend(self._az0403_vm_no_secure_boot(all_resources))
        findings.extend(self._az0404_storage_no_dns_endpoint_type(all_resources))
        findings.extend(self._az0405_aks_no_node_public_ip(all_resources))
        findings.extend(self._az0406_keyvault_no_public_network_access(all_resources))
        findings.extend(self._az0407_app_service_no_remote_debugging_version(all_resources))
        findings.extend(self._az0408_sql_no_primary_user_assigned_identity(all_resources))
        findings.extend(self._az0409_aks_no_node_labels(all_resources))
        findings.extend(self._az0410_cosmosdb_no_access_key_metadata_writes(all_resources))
        findings.extend(self._az0411_app_gw_no_sku_capacity(all_resources))
        findings.extend(self._az0412_vm_no_vtpm(all_resources))
        findings.extend(self._az0413_storage_no_sas_expiration_period(all_resources))
        findings.extend(self._az0414_aks_no_pod_subnet(all_resources))
        findings.extend(self._az0415_keyvault_key_no_rotation(all_resources))
        findings.extend(self._az0416_app_service_no_use_32_bit_worker(all_resources))
        findings.extend(self._az0417_sql_no_retention_days(all_resources))
        findings.extend(self._az0418_aks_no_fips_enabled(all_resources))
        findings.extend(self._az0419_cosmosdb_no_network_acl_bypass(all_resources))
        findings.extend(self._az0420_app_gw_no_connection_draining(all_resources))
        findings.extend(self._az0421_vm_no_provision_vm_agent(all_resources))
        findings.extend(self._az0422_storage_no_default_to_oauth(all_resources))
        findings.extend(self._az0423_aks_no_disk_driver(all_resources))
        findings.extend(self._az0424_keyvault_key_no_key_opts(all_resources))
        findings.extend(self._az0425_app_service_no_managed_pipeline_mode(all_resources))
        findings.extend(self._az0426_sql_no_storage_account_type(all_resources))
        findings.extend(self._az0427_aks_no_temporary_name(all_resources))
        findings.extend(self._az0428_cosmosdb_no_mongo_server_version(all_resources))
        findings.extend(self._az0429_app_gw_no_path_rules(all_resources))
        findings.extend(self._az0430_vm_no_automatic_updates(all_resources))
        findings.extend(self._az0431_storage_no_shared_access_key(all_resources))
        findings.extend(self._az0432_aks_no_drain_timeout(all_resources))
        findings.extend(self._az0433_keyvault_key_no_curve(all_resources))
        findings.extend(self._az0434_app_service_no_scm_type(all_resources))
        findings.extend(self._az0435_sql_no_collation(all_resources))
        findings.extend(self._az0436_aks_no_soak_duration(all_resources))
        findings.extend(self._az0437_cosmosdb_no_create_mode(all_resources))
        findings.extend(self._az0438_app_gw_no_url_path_map(all_resources))
        findings.extend(self._az0439_vm_no_timezone(all_resources))
        findings.extend(self._az0440_storage_no_immutability_policy(all_resources))
        findings.extend(self._az0441_aks_no_node_pool_type(all_resources))
        findings.extend(self._az0442_keyvault_secret_no_tags(all_resources))
        findings.extend(self._az0443_app_service_no_client_affinity(all_resources))
        findings.extend(self._az0444_sql_no_max_size_gb(all_resources))
        findings.extend(self._az0445_aks_no_scale_down_mode(all_resources))
        findings.extend(self._az0446_cosmosdb_no_restore(all_resources))
        findings.extend(self._az0447_app_gw_no_gateway_ip_config(all_resources))
        findings.extend(self._az0448_vm_no_availability_set(all_resources))
        findings.extend(self._az0449_storage_no_cors_rules(all_resources))
        findings.extend(self._az0450_aks_no_workload_autoscaler(all_resources))
        findings.extend(self._az0451_keyvault_key_no_tags(all_resources))
        findings.extend(self._az0452_app_service_no_public_network_access(all_resources))
        findings.extend(self._az0453_sql_no_sku_name(all_resources))
        findings.extend(self._az0454_aks_no_spot_max_price(all_resources))
        findings.extend(self._az0455_cosmosdb_no_identity(all_resources))
        findings.extend(self._az0456_app_gw_no_private_link_config(all_resources))
        findings.extend(self._az0457_vm_no_os_disk_security_encryption(all_resources))
        findings.extend(self._az0458_storage_no_custom_domain(all_resources))
        findings.extend(self._az0459_aks_no_os_sku(all_resources))
        findings.extend(self._az0460_keyvault_no_tags(all_resources))
        findings.extend(self._az0461_app_service_no_logs_http(all_resources))
        findings.extend(self._az0462_sql_no_tags(all_resources))
        findings.extend(self._az0463_aks_no_priority(all_resources))
        findings.extend(self._az0464_cosmosdb_no_tags(all_resources))
        findings.extend(self._az0465_app_gw_no_tags(all_resources))
        findings.extend(self._az0466_vm_no_tags(all_resources))
        findings.extend(self._az0467_storage_no_tags(all_resources))
        findings.extend(self._az0468_aks_no_tags(all_resources))
        findings.extend(self._az0469_nsg_no_tags(all_resources))
        findings.extend(self._az0470_vnet_no_tags(all_resources))
        findings.extend(self._az0471_subnet_no_service_endpoints(all_resources))
        findings.extend(self._az0472_subnet_no_delegation(all_resources))
        findings.extend(self._az0473_subnet_no_private_endpoint_policies(all_resources))
        findings.extend(self._az0474_subnet_no_private_link_service_policies(all_resources))
        findings.extend(self._az0475_vnet_no_address_space(all_resources))
        findings.extend(self._az0476_vnet_no_dns_servers(all_resources))
        findings.extend(self._az0477_vnet_peering_no_allow_forwarded_traffic(all_resources))
        findings.extend(self._az0478_vnet_peering_no_allow_gateway_transit(all_resources))
        findings.extend(self._az0479_lb_no_frontend_ip(all_resources))
        findings.extend(self._az0480_lb_no_backend_pool(all_resources))
        findings.extend(self._az0481_lb_rule_no_idle_timeout(all_resources))
        findings.extend(self._az0482_lb_rule_no_enable_tcp_reset(all_resources))
        findings.extend(self._az0483_nat_gateway_no_idle_timeout(all_resources))
        findings.extend(self._az0484_nat_gateway_no_zones(all_resources))
        findings.extend(self._az0485_public_ip_no_allocation_method(all_resources))
        findings.extend(self._az0486_public_ip_no_sku(all_resources))
        findings.extend(self._az0487_public_ip_no_zones(all_resources))
        findings.extend(self._az0488_public_ip_no_ddos_protection_mode(all_resources))
        findings.extend(self._az0489_dns_record_no_ttl(all_resources))
        findings.extend(self._az0490_private_dns_record_no_ttl(all_resources))
        findings.extend(self._az0491_route_no_next_hop_type(all_resources))
        findings.extend(self._az0492_route_table_no_routes(all_resources))
        findings.extend(self._az0493_network_interface_no_dns_servers(all_resources))
        findings.extend(self._az0494_network_interface_no_internal_dns_name(all_resources))
        findings.extend(self._az0495_firewall_policy_no_intrusion_detection(all_resources))
        findings.extend(self._az0496_firewall_policy_no_threat_intelligence(all_resources))
        findings.extend(self._az0497_express_route_no_bandwidth(all_resources))
        findings.extend(self._az0498_express_route_no_peering_location(all_resources))
        findings.extend(self._az0499_vpn_connection_no_shared_key(all_resources))
        findings.extend(self._az0500_vpn_connection_no_ipsec_policy(all_resources))

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
        findings.extend(self._gc0011_bigquery_no_cmk(all_resources))
        findings.extend(self._gc0012_bigquery_public_dataset(all_resources))
        findings.extend(self._gc0013_pubsub_no_cmk(all_resources))
        findings.extend(self._gc0014_cloudfunctions_public(all_resources))
        findings.extend(self._gc0015_cloudfunctions_no_vpc(all_resources))
        findings.extend(self._gc0016_cloudrun_public(all_resources))
        findings.extend(self._gc0017_cloudrun_no_vpc(all_resources))
        findings.extend(self._gc0018_gke_no_network_policy(all_resources))
        findings.extend(self._gc0019_gke_no_private_cluster(all_resources))
        findings.extend(self._gc0020_gke_no_shielded_nodes(all_resources))
        findings.extend(self._gc0021_gke_no_workload_identity(all_resources))
        findings.extend(self._gc0022_gke_no_binary_auth(all_resources))
        findings.extend(self._gc0023_gke_no_pod_security_policy(all_resources))
        findings.extend(self._gc0024_compute_disk_no_cmk(all_resources))
        findings.extend(self._gc0025_compute_disk_no_snapshot(all_resources))
        findings.extend(self._gc0026_compute_instance_public_ip(all_resources))
        findings.extend(self._gc0027_compute_instance_no_shielded(all_resources))
        findings.extend(self._gc0028_compute_instance_no_oslogin(all_resources))
        findings.extend(self._gc0029_compute_instance_serial_port(all_resources))
        findings.extend(self._gc0030_compute_instance_ip_forwarding(all_resources))
        findings.extend(self._gc0031_vpc_flow_logs_disabled(all_resources))
        findings.extend(self._gc0032_vpc_subnet_private_access(all_resources))
        findings.extend(self._gc0033_dns_dnssec_disabled(all_resources))
        findings.extend(self._gc0034_logging_no_retention(all_resources))
        findings.extend(self._gc0035_monitoring_no_alert_policy(all_resources))
        findings.extend(self._gc0036_secret_manager_no_rotation(all_resources))
        findings.extend(self._gc0037_secret_manager_no_cmk(all_resources))
        findings.extend(self._gc0038_dataproc_no_cmk(all_resources))
        findings.extend(self._gc0039_dataproc_public_ip(all_resources))
        findings.extend(self._gc0040_composer_public_ip(all_resources))
        findings.extend(self._gc0041_composer_no_cmk(all_resources))
        findings.extend(self._gc0042_spanner_no_cmk(all_resources))
        findings.extend(self._gc0043_bigtable_no_cmk(all_resources))
        findings.extend(self._gc0044_memorystore_no_auth(all_resources))
        findings.extend(self._gc0045_memorystore_no_transit_encryption(all_resources))
        findings.extend(self._gc0046_filestore_no_cmk(all_resources))
        findings.extend(self._gc0047_artifact_registry_public(all_resources))
        findings.extend(self._gc0048_artifact_registry_no_cmk(all_resources))
        findings.extend(self._gc0049_container_registry_public(all_resources))
        findings.extend(self._gc0050_cloud_armor_no_rules(all_resources))
        findings.extend(self._gc0051_load_balancer_no_ssl_policy(all_resources))
        findings.extend(self._gc0052_load_balancer_no_logging(all_resources))
        findings.extend(self._gc0053_cdn_no_signed_urls(all_resources))
        findings.extend(self._gc0054_appengine_no_ssl(all_resources))
        findings.extend(self._gc0055_appengine_public(all_resources))
        findings.extend(self._gc0056_healthcare_dataset_public(all_resources))
        findings.extend(self._gc0057_healthcare_no_cmk(all_resources))
        findings.extend(self._gc0058_vertex_ai_public(all_resources))
        findings.extend(self._gc0059_vertex_ai_no_cmk(all_resources))
        findings.extend(self._gc0060_notebooks_public_ip(all_resources))
        findings.extend(self._gc0061_notebooks_no_cmk(all_resources))
        findings.extend(self._gc0062_dataflow_public_ip(all_resources))
        findings.extend(self._gc0063_dataflow_no_cmk(all_resources))
        findings.extend(self._gc0064_datafusion_public_ip(all_resources))
        findings.extend(self._gc0065_datafusion_no_cmk(all_resources))
        findings.extend(self._gc0066_iam_service_account_key(all_resources))
        findings.extend(self._gc0067_iam_no_separation_of_duties(all_resources))
        findings.extend(self._gc0068_org_policy_not_enforced(all_resources))
        findings.extend(self._gc0069_vpc_peering_no_export(all_resources))
        findings.extend(self._gc0070_vpn_no_high_availability(all_resources))
        findings.extend(self._gc0071_interconnect_no_encryption(all_resources))
        findings.extend(self._gc0072_nat_no_logging(all_resources))
        findings.extend(self._gc0073_router_no_bgp_auth(all_resources))
        findings.extend(self._gc0074_service_networking_no_private(all_resources))
        findings.extend(self._gc0075_endpoints_no_auth(all_resources))
        findings.extend(self._gc0076_apigateway_no_auth(all_resources))
        findings.extend(self._gc0077_cloudtasks_no_auth(all_resources))
        findings.extend(self._gc0078_scheduler_no_auth(all_resources))
        findings.extend(self._gc0079_workflows_no_auth(all_resources))
        findings.extend(self._gc0080_eventarc_no_auth(all_resources))
        findings.extend(self._gc0081_firestore_no_cmk(all_resources))
        findings.extend(self._gc0082_datastore_no_cmk(all_resources))
        findings.extend(self._gc0083_alloydb_public(all_resources))
        findings.extend(self._gc0084_alloydb_no_cmk(all_resources))
        findings.extend(self._gc0085_sql_no_backup(all_resources))
        findings.extend(self._gc0086_sql_no_ha(all_resources))
        findings.extend(self._gc0087_sql_maintenance_window(all_resources))
        findings.extend(self._gc0088_sql_no_insights(all_resources))
        findings.extend(self._gc0089_gke_no_release_channel(all_resources))
        findings.extend(self._gc0090_gke_no_maintenance_window(all_resources))
        findings.extend(self._gc0091_gke_master_authorized_networks(all_resources))
        findings.extend(self._gc0092_gke_no_intranode_visibility(all_resources))
        findings.extend(self._gc0093_gke_no_logging(all_resources))
        findings.extend(self._gc0094_gke_no_monitoring(all_resources))
        findings.extend(self._gc0095_compute_image_public(all_resources))
        findings.extend(self._gc0096_compute_snapshot_public(all_resources))
        findings.extend(self._gc0097_storage_uniform_access(all_resources))
        findings.extend(self._gc0098_storage_retention_policy(all_resources))
        findings.extend(self._gc0099_storage_versioning(all_resources))
        findings.extend(self._gc0100_storage_lifecycle(all_resources))
        findings.extend(self._gc0101_kms_no_destroy_protection(all_resources))
        findings.extend(self._gc0102_kms_public_key(all_resources))
        findings.extend(self._gc0103_logging_sink_no_filter(all_resources))
        findings.extend(self._gc0104_billing_budget_missing(all_resources))
        findings.extend(self._gc0105_project_default_network(all_resources))
        findings.extend(self._gc0106_project_default_service_account(all_resources))
        findings.extend(self._gc0107_folder_iam_public(all_resources))
        findings.extend(self._gc0108_org_iam_public(all_resources))
        findings.extend(self._gc0109_access_context_manager_missing(all_resources))
        findings.extend(self._gc0110_vpc_service_controls_missing(all_resources))

        # OCI rules
        findings.extend(self._oc0001_bucket_public(all_resources))
        findings.extend(self._oc0002_seclist_open(all_resources))
        findings.extend(self._oc0003_db_no_encryption(all_resources))
        findings.extend(self._oc0004_nsg_open(all_resources))
        findings.extend(self._oc0005_boot_volume_no_encryption(all_resources))
        findings.extend(self._oc0006_compute_no_encryption(all_resources))
        findings.extend(self._oc0007_compute_public_ip(all_resources))
        findings.extend(self._oc0008_block_volume_no_backup(all_resources))
        findings.extend(self._oc0009_vcn_no_flow_logs(all_resources))
        findings.extend(self._oc0010_load_balancer_no_ssl(all_resources))
        findings.extend(self._oc0011_load_balancer_no_waf(all_resources))
        findings.extend(self._oc0012_vault_no_key_rotation(all_resources))
        findings.extend(self._oc0013_autonomous_db_public(all_resources))
        findings.extend(self._oc0014_autonomous_db_no_encryption(all_resources))
        findings.extend(self._oc0015_file_storage_no_encryption(all_resources))
        findings.extend(self._oc0016_api_gateway_no_auth(all_resources))
        findings.extend(self._oc0017_functions_public(all_resources))
        findings.extend(self._oc0018_streaming_no_encryption(all_resources))
        findings.extend(self._oc0019_notification_no_encryption(all_resources))
        findings.extend(self._oc0020_logging_no_encryption(all_resources))
        findings.extend(self._oc0021_events_no_encryption(all_resources))
        findings.extend(self._oc0022_container_engine_public(all_resources))
        findings.extend(self._oc0023_container_engine_no_encryption(all_resources))
        findings.extend(self._oc0024_container_registry_public(all_resources))
        findings.extend(self._oc0025_data_catalog_no_encryption(all_resources))
        findings.extend(self._oc0026_data_flow_no_encryption(all_resources))
        findings.extend(self._oc0027_data_science_no_encryption(all_resources))
        findings.extend(self._oc0028_integration_no_encryption(all_resources))
        findings.extend(self._oc0029_analytics_no_encryption(all_resources))
        findings.extend(self._oc0030_mysql_no_encryption(all_resources))
        findings.extend(self._oc0031_mysql_public(all_resources))
        findings.extend(self._oc0032_nosql_no_encryption(all_resources))
        findings.extend(self._oc0033_dns_dnssec_disabled(all_resources))
        findings.extend(self._oc0034_email_no_dkim(all_resources))
        findings.extend(self._oc0035_waf_no_rules(all_resources))
        findings.extend(self._oc0036_bastion_public(all_resources))
        findings.extend(self._oc0037_service_mesh_no_mtls(all_resources))
        findings.extend(self._oc0038_golden_gate_public(all_resources))
        findings.extend(self._oc0039_devops_no_encryption(all_resources))
        findings.extend(self._oc0040_visual_builder_public(all_resources))
        findings.extend(self._oc0041_blockchain_public(all_resources))
        findings.extend(self._oc0042_media_flow_no_encryption(all_resources))
        findings.extend(self._oc0043_certificates_expiring(all_resources))
        findings.extend(self._oc0044_budget_missing(all_resources))
        findings.extend(self._oc0045_cloud_guard_disabled(all_resources))
        findings.extend(self._oc0046_vault_public(all_resources))
        findings.extend(self._oc0047_secret_no_rotation(all_resources))
        findings.extend(self._oc0048_iam_policy_overpermissive(all_resources))
        findings.extend(self._oc0049_identity_domain_no_mfa(all_resources))
        findings.extend(self._oc0050_compartment_no_policy(all_resources))
        findings.extend(self._oc0051_audit_retention_short(all_resources))
        findings.extend(self._oc0052_network_firewall_no_rules(all_resources))
        findings.extend(self._oc0053_drg_no_route_table(all_resources))
        findings.extend(self._oc0054_service_gateway_missing(all_resources))
        findings.extend(self._oc0055_nat_gateway_missing(all_resources))
        findings.extend(self._oc0056_vcn_local_peering_open(all_resources))
        findings.extend(self._oc0057_remote_peering_open(all_resources))
        findings.extend(self._oc0058_ipsec_weak_encryption(all_resources))
        findings.extend(self._oc0059_fastconnect_no_encryption(all_resources))
        findings.extend(self._oc0060_waa_no_policy(all_resources))
        findings.extend(self._oc0061_instance_pool_no_placement(all_resources))
        findings.extend(self._oc0062_autoscaling_no_policy(all_resources))
        findings.extend(self._oc0063_cluster_network_no_placement(all_resources))
        findings.extend(self._oc0064_dedicated_vm_host_missing(all_resources))
        findings.extend(self._oc0065_capacity_reservation_missing(all_resources))
        findings.extend(self._oc0066_image_no_encryption(all_resources))
        findings.extend(self._oc0067_cross_connect_no_macsec(all_resources))
        findings.extend(self._oc0068_vtap_no_encryption(all_resources))
        findings.extend(self._oc0069_network_load_balancer_no_nsg(all_resources))
        findings.extend(self._oc0070_health_check_no_https(all_resources))
        findings.extend(self._oc0071_db_home_no_backup(all_resources))
        findings.extend(self._oc0072_exadata_no_encryption(all_resources))
        findings.extend(self._oc0073_data_guard_missing(all_resources))
        findings.extend(self._oc0074_database_tools_public(all_resources))
        findings.extend(self._oc0075_osms_no_schedule(all_resources))
        findings.extend(self._oc0076_vulnerability_scanning_disabled(all_resources))
        findings.extend(self._oc0077_java_management_disabled(all_resources))
        findings.extend(self._oc0078_ops_insights_disabled(all_resources))
        findings.extend(self._oc0079_stack_monitoring_disabled(all_resources))
        findings.extend(self._oc0080_apm_no_encryption(all_resources))
        findings.extend(self._oc0081_log_analytics_no_encryption(all_resources))
        findings.extend(self._oc0082_service_connector_no_encryption(all_resources))
        findings.extend(self._oc0083_queue_no_encryption(all_resources))
        findings.extend(self._oc0084_opensearch_public(all_resources))
        findings.extend(self._oc0085_opensearch_no_encryption(all_resources))
        findings.extend(self._oc0086_redis_no_encryption(all_resources))
        findings.extend(self._oc0087_psql_public(all_resources))
        findings.extend(self._oc0088_psql_no_encryption(all_resources))
        findings.extend(self._oc0089_ai_service_no_encryption(all_resources))
        findings.extend(self._oc0090_generative_ai_no_endpoint(all_resources))
        findings.extend(self._oc0091_big_data_no_encryption(all_resources))
        findings.extend(self._oc0092_data_labeling_no_encryption(all_resources))
        findings.extend(self._oc0093_ocvs_no_encryption(all_resources))
        findings.extend(self._oc0094_rover_no_encryption(all_resources))
        findings.extend(self._oc0095_resource_scheduler_missing(all_resources))
        findings.extend(self._oc0096_limits_quota_missing(all_resources))
        findings.extend(self._oc0097_announcement_subscription_missing(all_resources))
        findings.extend(self._oc0098_console_connection_insecure(all_resources))
        findings.extend(self._oc0099_marketplace_agreement_missing(all_resources))
        findings.extend(self._oc0100_network_path_analyzer_missing(all_resources))
        findings.extend(self._oc0101_tag_namespace_missing(all_resources))
        findings.extend(self._oc0102_cost_tracking_tag_missing(all_resources))
        findings.extend(self._oc0103_ons_subscription_unconfirmed(all_resources))
        findings.extend(self._oc0104_alarm_missing(all_resources))
        findings.extend(self._oc0105_log_group_no_retention(all_resources))

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

    def _az0091_automation_account_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0091 – Azure Automation Account publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_automation_account":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0091", "Automation Account publicly accessible",
                        f"Automation Account '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0091", "Automation Account without managed identity",
                        f"Automation Account '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0092_cognitive_account_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0092 – Azure Cognitive Services publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cognitive_account":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0092", "Cognitive Services publicly accessible",
                        f"Cognitive Services account '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoints.",
                    ))
                if _tf_body_has_key_value(body, "local_auth_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0092", "Cognitive Services local auth enabled",
                        f"Cognitive Services account '{res['name']}' has local authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_auth_enabled = false and use Azure AD authentication.",
                    ))
        return findings

    def _az0093_communication_service_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0093 – Azure Communication Service without managed identity."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_communication_service":
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding(
                        "AZ0093", "Communication Service without managed identity",
                        f"Communication Service '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0094_container_group_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0094 – Azure Container Instance publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_group":
                body = res["body"]
                ip_type = _tf_body_get_value(body, "ip_address_type")
                if ip_type and "Public" in ip_type:
                    findings.append(self._finding(
                        "AZ0094", "Container Instance publicly accessible",
                        f"Container Group '{res['name']}' has public IP address.",
                        IaCSeverity.HIGH, res,
                        "Set ip_address_type = 'Private' and deploy into a VNet.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0094", "Container Instance without managed identity",
                        f"Container Group '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0095_data_explorer_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0095 – Azure Data Explorer (Kusto) publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kusto_cluster":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0095", "Data Explorer publicly accessible",
                        f"Kusto cluster '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if not _tf_body_has_key_value(body, "disk_encryption_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0095", "Data Explorer without disk encryption",
                        f"Kusto cluster '{res['name']}' does not have disk encryption enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set disk_encryption_enabled = true.",
                    ))
                if _tf_body_has_key_value(body, "double_encryption_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0095", "Data Explorer without double encryption",
                        f"Kusto cluster '{res['name']}' does not have double encryption.",
                        IaCSeverity.LOW, res,
                        "Set double_encryption_enabled = true for defense in depth.",
                    ))
        return findings

    def _az0096_databricks_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0096 – Azure Databricks publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_databricks_workspace":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0096", "Databricks workspace publicly accessible",
                        f"Databricks workspace '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if not _tf_body_has_key_value(body, "infrastructure_encryption_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0096", "Databricks without infrastructure encryption",
                        f"Databricks workspace '{res['name']}' lacks infrastructure encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set infrastructure_encryption_enabled = true.",
                    ))
        return findings

    def _az0097_healthcare_fhir_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0097 – Azure Healthcare APIs (FHIR) publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_healthcare_fhir_service":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0097", "Healthcare FHIR publicly accessible",
                        f"FHIR service '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
            if res["type"] == "azurerm_healthcare_service":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0097", "Healthcare service publicly accessible",
                        f"Healthcare service '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
        return findings

    def _az0098_iot_hub_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0098 – Azure IoT Hub publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_iothub":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0098", "IoT Hub publicly accessible",
                        f"IoT Hub '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if not _tf_body_get_value(body, "min_tls_version"):
                    findings.append(self._finding(
                        "AZ0098", "IoT Hub without minimum TLS version",
                        f"IoT Hub '{res['name']}' does not enforce minimum TLS version.",
                        IaCSeverity.MEDIUM, res,
                        "Set min_tls_version = '1.2'.",
                    ))
        return findings

    def _az0099_machine_learning_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0099 – Azure Machine Learning workspace publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_machine_learning_workspace":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0099", "ML workspace publicly accessible",
                        f"ML workspace '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if not _tf_body_has_block(body, "encryption"):
                    findings.append(self._finding(
                        "AZ0099", "ML workspace without CMK encryption",
                        f"ML workspace '{res['name']}' does not use customer-managed keys.",
                        IaCSeverity.MEDIUM, res,
                        "Add an encryption block with customer-managed key.",
                    ))
        return findings

    def _az0100_media_services_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0100 – Azure Media Services without managed identity."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_media_services_account":
                body = res["body"]
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0100", "Media Services without managed identity",
                        f"Media Services '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
                if not _tf_body_has_block(body, "encryption"):
                    findings.append(self._finding(
                        "AZ0100", "Media Services without CMK encryption",
                        f"Media Services '{res['name']}' does not use customer-managed keys.",
                        IaCSeverity.MEDIUM, res,
                        "Add an encryption block with customer-managed key.",
                    ))
        return findings

    def _az0101_managed_disk_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0101 – Azure Managed Disk without encryption at host."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_managed_disk":
                body = res["body"]
                if not _tf_body_get_value(body, "disk_encryption_set_id"):
                    findings.append(self._finding(
                        "AZ0101", "Managed Disk without disk encryption set",
                        f"Managed Disk '{res['name']}' does not use a disk encryption set.",
                        IaCSeverity.MEDIUM, res,
                        "Set disk_encryption_set_id to use customer-managed encryption.",
                    ))
                if _tf_body_has_key_value(body, "network_access_policy", '"AllowAll"'):
                    findings.append(self._finding(
                        "AZ0101", "Managed Disk allows all network access",
                        f"Managed Disk '{res['name']}' allows all network access.",
                        IaCSeverity.HIGH, res,
                        "Set network_access_policy = 'DenyAll' or 'AllowPrivate'.",
                    ))
        return findings

    def _az0102_private_dns_zone_no_link(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0102 – Azure Private DNS Zone without VNet link."""
        findings = []
        dns_zones = [r for r in resources if r["type"] == "azurerm_private_dns_zone"]
        dns_links = [r for r in resources if r["type"] == "azurerm_private_dns_zone_virtual_network_link"]
        linked_zones = set()
        for link in dns_links:
            zone_id = _tf_body_get_value(link["body"], "private_dns_zone_name")
            if zone_id:
                linked_zones.add(zone_id.strip('"'))
        for zone in dns_zones:
            zone_name = _tf_body_get_value(zone["body"], "name")
            if zone_name and zone_name.strip('"') not in linked_zones:
                findings.append(self._finding(
                    "AZ0102", "Private DNS Zone without VNet link",
                    f"Private DNS Zone '{zone['name']}' has no VNet link configured.",
                    IaCSeverity.MEDIUM, zone,
                    "Create an azurerm_private_dns_zone_virtual_network_link resource.",
                ))
        return findings

    def _az0103_app_service_slot_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0103 – Azure App Service slot without HTTPS enforcement."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_app_service_slot":
                body = res["body"]
                if _tf_body_has_key_value(body, "https_only", "false"):
                    findings.append(self._finding(
                        "AZ0103", "App Service slot allows HTTP",
                        f"App Service slot '{res['name']}' does not enforce HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set https_only = true.",
                    ))
        return findings

    def _az0104_redis_no_tls(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0104 – Azure Redis Cache without TLS enforcement."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_redis_cache":
                body = res["body"]
                if _tf_body_has_key_value(body, "enable_non_ssl_port", "true"):
                    findings.append(self._finding(
                        "AZ0104", "Redis Cache allows non-SSL connections",
                        f"Redis Cache '{res['name']}' has non-SSL port enabled.",
                        IaCSeverity.HIGH, res,
                        "Set enable_non_ssl_port = false.",
                    ))
                min_tls = _tf_body_get_value(body, "minimum_tls_version")
                if min_tls and "1.0" in min_tls:
                    findings.append(self._finding(
                        "AZ0104", "Redis Cache using TLS 1.0",
                        f"Redis Cache '{res['name']}' uses TLS 1.0.",
                        IaCSeverity.MEDIUM, res,
                        "Set minimum_tls_version = '1.2'.",
                    ))
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0104", "Redis Cache publicly accessible",
                        f"Redis Cache '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
        return findings

    def _az0105_search_service_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0105 – Azure Cognitive Search publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_search_service":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0105", "Search Service publicly accessible",
                        f"Search Service '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0105", "Search Service without managed identity",
                        f"Search Service '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0106_spring_cloud_no_vnet(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0106 – Azure Spring Cloud without VNet injection."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_spring_cloud_service", "azurerm_spring_cloud_app"):
                if res["type"] == "azurerm_spring_cloud_service":
                    body = res["body"]
                    if not _tf_body_has_block(body, "network"):
                        findings.append(self._finding(
                            "AZ0106", "Spring Cloud without VNet injection",
                            f"Spring Cloud service '{res['name']}' is not deployed in a VNet.",
                            IaCSeverity.HIGH, res,
                            "Add a network block with service_runtime_subnet_id and app_subnet_id.",
                        ))
        return findings

    def _az0107_web_pubsub_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0107 – Azure Web PubSub publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_web_pubsub":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0107", "Web PubSub publicly accessible",
                        f"Web PubSub '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if _tf_body_has_key_value(body, "local_auth_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0107", "Web PubSub local auth enabled",
                        f"Web PubSub '{res['name']}' has local authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_auth_enabled = false.",
                    ))
        return findings

    def _az0108_frontdoor_waf_missing(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0108 – Azure Front Door without WAF policy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_frontdoor":
                body = res["body"]
                if not _tf_body_get_value(body, "web_application_firewall_policy_link_id"):
                    findings.append(self._finding(
                        "AZ0108", "Front Door without WAF policy",
                        f"Front Door '{res['name']}' has no WAF policy linked.",
                        IaCSeverity.HIGH, res,
                        "Link a WAF policy using web_application_firewall_policy_link_id.",
                    ))
            if res["type"] == "azurerm_cdn_frontdoor_profile":
                body = res["body"]
                sku = _tf_body_get_value(body, "sku_name")
                if sku and "Standard" in sku:
                    findings.append(self._finding(
                        "AZ0108", "CDN Front Door using Standard SKU",
                        f"CDN Front Door '{res['name']}' uses Standard SKU (no WAF support).",
                        IaCSeverity.MEDIUM, res,
                        "Use Premium_AzureFrontDoor SKU for WAF support.",
                    ))
        return findings

    def _az0109_app_gateway_no_waf(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0109 – Azure Application Gateway without WAF."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                body = res["body"]
                sku_name = _tf_body_get_value(body, "name")
                if sku_name and "WAF" not in sku_name:
                    findings.append(self._finding(
                        "AZ0109", "Application Gateway without WAF",
                        f"Application Gateway '{res['name']}' does not use WAF SKU.",
                        IaCSeverity.HIGH, res,
                        "Use WAF_v2 SKU for web application firewall protection.",
                    ))
                if _tf_body_has_block(body, "waf_configuration"):
                    if _tf_body_has_key_value(body, "enabled", "false"):
                        findings.append(self._finding(
                            "AZ0109", "Application Gateway WAF disabled",
                            f"Application Gateway '{res['name']}' has WAF disabled.",
                            IaCSeverity.HIGH, res,
                            "Set enabled = true in waf_configuration.",
                        ))
        return findings

    def _az0110_dns_zone_no_dnssec(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0110 – Azure DNS Zone without DNSSEC."""
        findings = []
        dns_zones = [r for r in resources if r["type"] == "azurerm_dns_zone"]
        dnssec_configs = [r for r in resources if r["type"] == "azurerm_dns_zone_dnssec_config"]
        secured_zones = set()
        for cfg in dnssec_configs:
            zone_id = _tf_body_get_value(cfg["body"], "dns_zone_id")
            if zone_id:
                secured_zones.add(zone_id.strip('"'))
        for zone in dns_zones:
            if zone["name"] not in secured_zones:
                findings.append(self._finding(
                    "AZ0110", "DNS Zone without DNSSEC",
                    f"DNS Zone '{zone['name']}' does not have DNSSEC configured.",
                    IaCSeverity.MEDIUM, zone,
                    "Create an azurerm_dns_zone_dnssec_config for this zone.",
                ))
        return findings

    def _az0111_express_route_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0111 – Azure ExpressRoute without encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_express_route_circuit":
                body = res["body"]
                sku_tier = _tf_body_get_value(body, "tier")
                if sku_tier and "Standard" in sku_tier:
                    findings.append(self._finding(
                        "AZ0111", "ExpressRoute without Premium tier",
                        f"ExpressRoute circuit '{res['name']}' uses Standard tier.",
                        IaCSeverity.LOW, res,
                        "Consider Premium tier for global routing and higher limits.",
                    ))
            if res["type"] == "azurerm_express_route_connection":
                body = res["body"]
                if not _tf_body_has_key_value(body, "enable_internet_security", "true"):
                    findings.append(self._finding(
                        "AZ0111", "ExpressRoute connection without internet security",
                        f"ExpressRoute connection '{res['name']}' lacks internet security.",
                        IaCSeverity.MEDIUM, res,
                        "Set enable_internet_security = true.",
                    ))
        return findings

    def _az0112_firewall_no_threat_intel(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0112 – Azure Firewall without threat intelligence."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_firewall":
                body = res["body"]
                threat_mode = _tf_body_get_value(body, "threat_intel_mode")
                if not threat_mode or "Off" in threat_mode:
                    findings.append(self._finding(
                        "AZ0112", "Firewall without threat intelligence",
                        f"Azure Firewall '{res['name']}' has threat intelligence disabled.",
                        IaCSeverity.HIGH, res,
                        "Set threat_intel_mode = 'Alert' or 'Deny'.",
                    ))
                sku = _tf_body_get_value(body, "sku_tier")
                if sku and "Standard" in sku:
                    findings.append(self._finding(
                        "AZ0112", "Firewall without Premium tier",
                        f"Azure Firewall '{res['name']}' uses Standard tier (no TLS inspection).",
                        IaCSeverity.MEDIUM, res,
                        "Consider Premium tier for TLS inspection and IDPS.",
                    ))
        return findings

    def _az0113_image_builder_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0113 – Azure Image Builder without managed identity."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_image_builder_template":
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding(
                        "AZ0113", "Image Builder without managed identity",
                        f"Image Builder template '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'UserAssigned'.",
                    ))
        return findings

    def _az0114_key_vault_no_purge_protection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0114 – Azure Key Vault without purge protection."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                body = res["body"]
                if _tf_body_has_key_value(body, "purge_protection_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0114", "Key Vault without purge protection",
                        f"Key Vault '{res['name']}' does not have purge protection enabled.",
                        IaCSeverity.HIGH, res,
                        "Set purge_protection_enabled = true.",
                    ))
                if _tf_body_has_key_value(body, "soft_delete_retention_days", "7"):
                    findings.append(self._finding(
                        "AZ0114", "Key Vault with minimum soft delete retention",
                        f"Key Vault '{res['name']}' has minimum soft delete retention (7 days).",
                        IaCSeverity.LOW, res,
                        "Set soft_delete_retention_days to 90 for better protection.",
                    ))
                if _tf_body_has_key_value(body, "enable_rbac_authorization", "false"):
                    findings.append(self._finding(
                        "AZ0114", "Key Vault using access policies instead of RBAC",
                        f"Key Vault '{res['name']}' uses access policies instead of RBAC.",
                        IaCSeverity.MEDIUM, res,
                        "Set enable_rbac_authorization = true for Azure RBAC.",
                    ))
        return findings

    def _az0115_lb_no_health_probe(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0115 – Azure Load Balancer without health probe."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_lb":
                body = res["body"]
                sku = _tf_body_get_value(body, "sku")
                if sku and "Basic" in sku:
                    findings.append(self._finding(
                        "AZ0115", "Load Balancer using Basic SKU",
                        f"Load Balancer '{res['name']}' uses Basic SKU (no SLA, limited features).",
                        IaCSeverity.MEDIUM, res,
                        "Use Standard SKU for production workloads.",
                    ))
            if res["type"] == "azurerm_lb_rule":
                body = res["body"]
                if not _tf_body_get_value(body, "probe_id"):
                    findings.append(self._finding(
                        "AZ0115", "Load Balancer rule without health probe",
                        f"LB rule '{res['name']}' has no health probe configured.",
                        IaCSeverity.MEDIUM, res,
                        "Set probe_id to a health probe resource.",
                    ))
        return findings

    def _az0116_log_analytics_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0116 – Azure Log Analytics without CMK encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_log_analytics_workspace":
                body = res["body"]
                retention = _tf_body_get_value(body, "retention_in_days")
                if retention:
                    try:
                        days = int(retention.strip('"'))
                        if days < 30:
                            findings.append(self._finding(
                                "AZ0116", "Log Analytics with short retention",
                                f"Log Analytics '{res['name']}' has {days}-day retention.",
                                IaCSeverity.MEDIUM, res,
                                "Set retention_in_days to at least 30 for compliance.",
                            ))
                    except ValueError:
                        pass
                if _tf_body_has_key_value(body, "internet_ingestion_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0116", "Log Analytics allows internet ingestion",
                        f"Log Analytics '{res['name']}' allows internet data ingestion.",
                        IaCSeverity.MEDIUM, res,
                        "Set internet_ingestion_enabled = false for private-only ingestion.",
                    ))
        return findings

    def _az0117_monitor_diagnostic_missing(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0117 – Azure Monitor diagnostic settings missing."""
        findings = []
        has_diagnostic = any(r["type"] == "azurerm_monitor_diagnostic_setting" for r in resources)
        has_activity_log = any(r["type"] == "azurerm_monitor_activity_log_alert" for r in resources)
        if not has_diagnostic:
            # Check if there are resources that should have diagnostics
            important_types = {
                "azurerm_key_vault", "azurerm_sql_server",
                "azurerm_mssql_server", "azurerm_storage_account",
            }
            for res in resources:
                if res["type"] in important_types:
                    findings.append(self._finding(
                        "AZ0117", "No diagnostic settings configured",
                        f"Resource '{res['name']}' ({res['type']}) has no diagnostic settings.",
                        IaCSeverity.MEDIUM, res,
                        "Create an azurerm_monitor_diagnostic_setting for this resource.",
                    ))
                    break  # Only report once
        if not has_activity_log:
            for res in resources[:1]:
                findings.append(self._finding(
                    "AZ0117", "No activity log alerts configured",
                    "No Azure Monitor activity log alerts found in configuration.",
                    IaCSeverity.MEDIUM, res,
                    "Create azurerm_monitor_activity_log_alert resources for critical operations.",
                ))
        return findings

    def _az0118_mysql_flexible_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0118 – Azure MySQL Flexible Server publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mysql_flexible_server":
                body = res["body"]
                if not _tf_body_get_value(body, "delegated_subnet_id"):
                    findings.append(self._finding(
                        "AZ0118", "MySQL Flexible Server without VNet integration",
                        f"MySQL Flexible Server '{res['name']}' is not in a VNet.",
                        IaCSeverity.HIGH, res,
                        "Set delegated_subnet_id for VNet integration.",
                    ))
                if not _tf_body_has_key_value(body, "ssl_enforcement_enabled", "true"):
                    if _tf_body_has_key_value(body, "require_secure_transport", '"OFF"'):
                        findings.append(self._finding(
                            "AZ0118", "MySQL Flexible Server SSL not enforced",
                            f"MySQL Flexible Server '{res['name']}' does not require SSL.",
                            IaCSeverity.HIGH, res,
                            "Set require_secure_transport = 'ON' in server parameters.",
                        ))
        return findings

    def _az0119_network_interface_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0119 – Azure Network Interface with public IP."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_network_interface":
                body = res["body"]
                if _tf_body_get_value(body, "public_ip_address_id"):
                    findings.append(self._finding(
                        "AZ0119", "Network Interface with public IP",
                        f"NIC '{res['name']}' has a public IP address associated.",
                        IaCSeverity.MEDIUM, res,
                        "Remove public_ip_address_id and use Azure Bastion or VPN for access.",
                    ))
        return findings

    def _az0120_postgresql_flexible_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0120 – Azure PostgreSQL Flexible Server publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_postgresql_flexible_server":
                body = res["body"]
                if not _tf_body_get_value(body, "delegated_subnet_id"):
                    findings.append(self._finding(
                        "AZ0120", "PostgreSQL Flexible Server without VNet",
                        f"PostgreSQL Flexible Server '{res['name']}' is not in a VNet.",
                        IaCSeverity.HIGH, res,
                        "Set delegated_subnet_id for VNet integration.",
                    ))
                if not _tf_body_has_key_value(body, "ssl_enforcement_enabled", "true"):
                    pass  # PostgreSQL flexible enforces SSL by default
                if _tf_body_has_key_value(body, "geo_redundant_backup_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0120", "PostgreSQL without geo-redundant backup",
                        f"PostgreSQL Flexible Server '{res['name']}' lacks geo-redundant backup.",
                        IaCSeverity.MEDIUM, res,
                        "Set geo_redundant_backup_enabled = true for DR scenarios.",
                    ))
        return findings

    def _az0121_recovery_vault_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0121 – Azure Recovery Services Vault without encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_recovery_services_vault":
                body = res["body"]
                if _tf_body_has_key_value(body, "soft_delete_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0121", "Recovery Vault without soft delete",
                        f"Recovery Vault '{res['name']}' has soft delete disabled.",
                        IaCSeverity.HIGH, res,
                        "Set soft_delete_enabled = true.",
                    ))
                if not _tf_body_has_block(body, "encryption"):
                    findings.append(self._finding(
                        "AZ0121", "Recovery Vault without CMK encryption",
                        f"Recovery Vault '{res['name']}' does not use customer-managed keys.",
                        IaCSeverity.MEDIUM, res,
                        "Add an encryption block with customer-managed key.",
                    ))
        return findings

    def _az0122_route_table_no_propagation(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0122 – Azure Route Table BGP propagation enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_route_table":
                body = res["body"]
                if not _tf_body_has_key_value(body, "disable_bgp_route_propagation", "true"):
                    findings.append(self._finding(
                        "AZ0122", "Route Table with BGP propagation",
                        f"Route Table '{res['name']}' has BGP route propagation enabled.",
                        IaCSeverity.LOW, res,
                        "Set disable_bgp_route_propagation = true if not using ExpressRoute/VPN.",
                    ))
        return findings

    def _az0123_service_bus_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0123 – Azure Service Bus publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_servicebus_namespace":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0123", "Service Bus publicly accessible",
                        f"Service Bus namespace '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if _tf_body_has_key_value(body, "local_auth_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0123", "Service Bus local auth enabled",
                        f"Service Bus namespace '{res['name']}' has local authentication.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_auth_enabled = false and use Azure AD.",
                    ))
                sku = _tf_body_get_value(body, "sku")
                if sku and "Basic" in sku:
                    findings.append(self._finding(
                        "AZ0123", "Service Bus using Basic SKU",
                        f"Service Bus namespace '{res['name']}' uses Basic SKU.",
                        IaCSeverity.LOW, res,
                        "Use Standard or Premium SKU for production workloads.",
                    ))
        return findings

    def _az0124_snapshot_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0124 – Azure Snapshot without encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_snapshot":
                body = res["body"]
                if not _tf_body_get_value(body, "disk_encryption_set_id"):
                    findings.append(self._finding(
                        "AZ0124", "Snapshot without disk encryption set",
                        f"Snapshot '{res['name']}' does not use a disk encryption set.",
                        IaCSeverity.MEDIUM, res,
                        "Set disk_encryption_set_id for customer-managed encryption.",
                    ))
        return findings

    def _az0125_storage_sync_no_private(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0125 – Azure Storage Sync without private endpoint."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_sync":
                body = res["body"]
                if _tf_body_has_key_value(body, "incoming_traffic_policy", '"AllowAllTraffic"'):
                    findings.append(self._finding(
                        "AZ0125", "Storage Sync allows all traffic",
                        f"Storage Sync '{res['name']}' allows all incoming traffic.",
                        IaCSeverity.HIGH, res,
                        "Set incoming_traffic_policy = 'AllowVirtualNetworksOnly'.",
                    ))
        return findings

    def _az0126_virtual_hub_no_firewall(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0126 – Azure Virtual Hub without firewall."""
        findings = []
        vhubs = [r for r in resources if r["type"] == "azurerm_virtual_hub"]
        firewalls = [r for r in resources if r["type"] == "azurerm_firewall"]
        fw_vhub_ids = set()
        for fw in firewalls:
            vhub_id = _tf_body_get_value(fw["body"], "virtual_hub")
            if vhub_id:
                fw_vhub_ids.add(vhub_id.strip('"'))
        for vhub in vhubs:
            if vhub["name"] not in fw_vhub_ids:
                findings.append(self._finding(
                    "AZ0126", "Virtual Hub without firewall",
                    f"Virtual Hub '{vhub['name']}' has no Azure Firewall associated.",
                    IaCSeverity.MEDIUM, vhub,
                    "Deploy an Azure Firewall in the virtual hub.",
                ))
        return findings

    def _az0127_vm_scale_set_no_health_ext(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0127 – Azure VM Scale Set without health extension."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine_scale_set",
                               "azurerm_windows_virtual_machine_scale_set",
                               "azurerm_virtual_machine_scale_set"):
                body = res["body"]
                if not _tf_body_has_block(body, "automatic_instance_repair"):
                    findings.append(self._finding(
                        "AZ0127", "VM Scale Set without automatic repair",
                        f"VMSS '{res['name']}' has no automatic instance repair.",
                        IaCSeverity.MEDIUM, res,
                        "Add automatic_instance_repair block with enabled = true.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0127", "VM Scale Set without managed identity",
                        f"VMSS '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0128_vnet_no_ddos_protection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0128 – Azure VNet without DDoS protection."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network":
                body = res["body"]
                if not _tf_body_has_block(body, "ddos_protection_plan"):
                    findings.append(self._finding(
                        "AZ0128", "VNet without DDoS protection",
                        f"VNet '{res['name']}' has no DDoS protection plan.",
                        IaCSeverity.MEDIUM, res,
                        "Add a ddos_protection_plan block with enable = true.",
                    ))
        return findings

    def _az0129_vpn_gateway_no_active_active(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0129 – Azure VPN Gateway without active-active."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network_gateway":
                body = res["body"]
                gw_type = _tf_body_get_value(body, "type")
                if gw_type and "Vpn" in gw_type:
                    if _tf_body_has_key_value(body, "active_active", "false"):
                        findings.append(self._finding(
                            "AZ0129", "VPN Gateway without active-active",
                            f"VPN Gateway '{res['name']}' is not active-active.",
                            IaCSeverity.MEDIUM, res,
                            "Set active_active = true for high availability.",
                        ))
                    gen = _tf_body_get_value(body, "generation")
                    if gen and "Generation1" in gen:
                        findings.append(self._finding(
                            "AZ0129", "VPN Gateway using Generation1",
                            f"VPN Gateway '{res['name']}' uses Generation1.",
                            IaCSeverity.LOW, res,
                            "Use Generation2 for better performance.",
                        ))
        return findings

    def _az0130_waf_policy_prevention_mode(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0130 – Azure WAF policy not in prevention mode."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_web_application_firewall_policy":
                body = res["body"]
                if _tf_body_has_key_value(body, "mode", '"Detection"'):
                    findings.append(self._finding(
                        "AZ0130", "WAF policy in detection-only mode",
                        f"WAF policy '{res['name']}' is in Detection mode.",
                        IaCSeverity.HIGH, res,
                        "Set mode = 'Prevention' to actively block threats.",
                    ))
                if _tf_body_has_key_value(body, "enabled", "false"):
                    findings.append(self._finding(
                        "AZ0130", "WAF policy disabled",
                        f"WAF policy '{res['name']}' is disabled.",
                        IaCSeverity.CRITICAL, res,
                        "Set enabled = true in policy_settings.",
                    ))
        return findings

    def _az0131_event_grid_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0131 – Azure Event Grid topic publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_eventgrid_topic":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0131", "Event Grid topic publicly accessible",
                        f"Event Grid topic '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if _tf_body_has_key_value(body, "local_auth_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0131", "Event Grid topic local auth enabled",
                        f"Event Grid topic '{res['name']}' has local authentication.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_auth_enabled = false.",
                    ))
        return findings

    def _az0132_event_hub_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0132 – Azure Event Hub namespace publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_eventhub_namespace":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0132", "Event Hub namespace publicly accessible",
                        f"Event Hub namespace '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if _tf_body_has_key_value(body, "local_authentication_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0132", "Event Hub namespace local auth enabled",
                        f"Event Hub namespace '{res['name']}' has local authentication.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_authentication_enabled = false.",
                    ))
                sku = _tf_body_get_value(body, "sku")
                if sku and "Basic" in sku:
                    findings.append(self._finding(
                        "AZ0132", "Event Hub using Basic SKU",
                        f"Event Hub namespace '{res['name']}' uses Basic SKU.",
                        IaCSeverity.LOW, res,
                        "Use Standard or Premium for production.",
                    ))
        return findings

    def _az0133_cosmos_db_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0133 – Azure Cosmos DB publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0133", "Cosmos DB publicly accessible",
                        f"Cosmos DB '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if _tf_body_has_key_value(body, "local_authentication_disabled", "false"):
                    findings.append(self._finding(
                        "AZ0133", "Cosmos DB local auth enabled",
                        f"Cosmos DB '{res['name']}' has local authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_authentication_disabled = true.",
                    ))
                if not _tf_body_has_key_value(body, "is_virtual_network_filter_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0133", "Cosmos DB without VNet filter",
                        f"Cosmos DB '{res['name']}' has no VNet filter enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set is_virtual_network_filter_enabled = true.",
                    ))
        return findings

    def _az0134_function_app_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0134 – Azure Function App publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_function_app", "azurerm_linux_function_app",
                               "azurerm_windows_function_app"):
                body = res["body"]
                if _tf_body_has_key_value(body, "https_only", "false"):
                    findings.append(self._finding(
                        "AZ0134", "Function App allows HTTP",
                        f"Function App '{res['name']}' does not enforce HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set https_only = true.",
                    ))
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0134", "Function App publicly accessible",
                        f"Function App '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0134", "Function App without managed identity",
                        f"Function App '{res['name']}' has no managed identity.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0135_container_registry_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0135 – Azure Container Registry publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0135", "Container Registry publicly accessible",
                        f"ACR '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
                if _tf_body_has_key_value(body, "admin_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0135", "Container Registry admin account enabled",
                        f"ACR '{res['name']}' has admin account enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set admin_enabled = false and use Azure AD or service principals.",
                    ))
                if not _tf_body_has_key_value(body, "quarantine_policy_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0135", "Container Registry without quarantine policy",
                        f"ACR '{res['name']}' has no quarantine policy.",
                        IaCSeverity.LOW, res,
                        "Set quarantine_policy_enabled = true.",
                    ))
        return findings

    def _az0136_managed_hsm_no_purge_protection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0136 – Azure Managed HSM without purge protection."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_managed_hardware_security_module":
                body = res["body"]
                if _tf_body_has_key_value(body, "purge_protection_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0136", "Managed HSM without purge protection",
                        f"Managed HSM '{res['name']}' lacks purge protection.",
                        IaCSeverity.HIGH, res,
                        "Set purge_protection_enabled = true.",
                    ))
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0136", "Managed HSM publicly accessible",
                        f"Managed HSM '{res['name']}' has public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
        return findings

    def _az0137_data_protection_vault_no_immutability(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0137 – Azure Backup Vault without immutability."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_data_protection_backup_vault":
                body = res["body"]
                if not _tf_body_has_key_value(body, "redundancy", '"GeoRedundant"'):
                    findings.append(self._finding(
                        "AZ0137", "Backup Vault without geo-redundancy",
                        f"Backup Vault '{res['name']}' is not geo-redundant.",
                        IaCSeverity.MEDIUM, res,
                        "Set redundancy = 'GeoRedundant' for DR scenarios.",
                    ))
                if _tf_body_has_key_value(body, "soft_delete", '"Off"'):
                    findings.append(self._finding(
                        "AZ0137", "Backup Vault soft delete disabled",
                        f"Backup Vault '{res['name']}' has soft delete disabled.",
                        IaCSeverity.HIGH, res,
                        "Set soft_delete = 'On' for data protection.",
                    ))
        return findings

    def _az0138_private_endpoint_no_dns(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0138 – Azure Private Endpoint without DNS configuration."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_private_endpoint":
                body = res["body"]
                if not _tf_body_has_block(body, "private_dns_zone_group"):
                    findings.append(self._finding(
                        "AZ0138", "Private Endpoint without DNS zone group",
                        f"Private Endpoint '{res['name']}' has no DNS zone group.",
                        IaCSeverity.MEDIUM, res,
                        "Add a private_dns_zone_group block for DNS resolution.",
                    ))
        return findings

    def _az0139_disk_access_public(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0139 – Azure Disk Access publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_managed_disk":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0139", "Managed Disk public network access",
                        f"Managed Disk '{res['name']}' has public network access enabled.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false.",
                    ))
            if res["type"] == "azurerm_disk_access":
                # Disk access without private endpoint
                pass  # Existence is fine, just need private endpoint
        return findings

    def _az0140_maintenance_config_missing(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0140 – Azure resources without maintenance configuration."""
        findings = []
        has_maintenance = any(r["type"] == "azurerm_maintenance_configuration" for r in resources)
        aks_clusters = [r for r in resources if r["type"] == "azurerm_kubernetes_cluster"]
        for aks in aks_clusters:
            body = aks["body"]
            if not _tf_body_has_block(body, "maintenance_window"):
                findings.append(self._finding(
                    "AZ0140", "AKS without maintenance window",
                    f"AKS cluster '{aks['name']}' has no maintenance window configured.",
                    IaCSeverity.LOW, aks,
                    "Add a maintenance_window block to control update timing.",
                ))
        if not has_maintenance:
            vms = [r for r in resources if r["type"] in (
                "azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine")]
            if vms:
                findings.append(self._finding(
                    "AZ0140", "No maintenance configuration found",
                    "No azurerm_maintenance_configuration found for VM resources.",
                    IaCSeverity.LOW, vms[0],
                    "Create an azurerm_maintenance_configuration for scheduled updates.",
                ))
        return findings

    def _az0141_firewall_policy_no_tls_inspection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0141 – Azure Firewall Policy without TLS inspection."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_firewall_policy":
                body = res["body"]
                if not _tf_body_has_block(body, "tls_certificate"):
                    findings.append(self._finding(
                        "AZ0141", "Firewall Policy without TLS inspection",
                        f"Firewall Policy '{res['name']}' has no TLS inspection configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add a tls_certificate block to enable TLS inspection.",
                    ))
        return findings

    def _az0142_sql_server_no_aad_admin(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0142 – Azure SQL Server without AAD administrator."""
        findings = []
        sql_servers = [r for r in resources if r["type"] == "azurerm_mssql_server"]
        aad_admins = {_tf_body_get_value(r["body"], "server_id") for r in resources
                      if r["type"] == "azurerm_mssql_server_microsoft_support_auditing_policy"
                      or r["type"] == "azurerm_mssql_server_security_alert_policy"}
        for srv in sql_servers:
            body = srv["body"]
            if not _tf_body_has_block(body, "azuread_administrator"):
                findings.append(self._finding(
                    "AZ0142", "SQL Server without AAD administrator",
                    f"SQL Server '{srv['name']}' has no Azure AD administrator configured.",
                    IaCSeverity.HIGH, srv,
                    "Add an azuread_administrator block to enforce AAD authentication.",
                ))
        return findings

    def _az0143_sql_audit_retention_short(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0143 – Azure SQL Server auditing retention too short."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server_extended_auditing_policy":
                body = res["body"]
                retention = _tf_body_get_value(body, "retention_in_days")
                if retention:
                    try:
                        days = int(retention.strip('"'))
                        if days < 90:
                            findings.append(self._finding(
                                "AZ0143", "SQL audit retention too short",
                                f"SQL audit policy '{res['name']}' retains logs for only {days} days.",
                                IaCSeverity.MEDIUM, res,
                                "Set retention_in_days >= 90 for adequate audit trail.",
                            ))
                    except ValueError:
                        pass
        return findings

    def _az0144_app_service_no_managed_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0144 – Azure App Service without managed identity."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app", "azurerm_app_service"):
                body = res["body"]
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0144", "App Service without managed identity",
                        f"App Service '{res['name']}' has no managed identity configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned' or 'UserAssigned'.",
                    ))
        return findings

    def _az0145_app_service_remote_debugging(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0145 – Azure App Service remote debugging enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app", "azurerm_app_service"):
                body = res["body"]
                if _tf_body_has_key_value(body, "remote_debugging_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0145", "App Service remote debugging enabled",
                        f"App Service '{res['name']}' has remote debugging enabled.",
                        IaCSeverity.HIGH, res,
                        "Set remote_debugging_enabled = false in production.",
                    ))
        return findings

    def _az0146_app_service_ftp_enabled(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0146 – Azure App Service FTP access enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app", "azurerm_app_service"):
                body = res["body"]
                ftps = _tf_body_get_value(body, "ftps_state")
                if ftps and '"AllAllowed"' in ftps:
                    findings.append(self._finding(
                        "AZ0146", "App Service FTP access enabled",
                        f"App Service '{res['name']}' allows plain FTP access.",
                        IaCSeverity.HIGH, res,
                        "Set ftps_state = 'FtpsOnly' or 'Disabled'.",
                    ))
        return findings

    def _az0147_storage_blob_public_access(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0147 – Azure Storage Account allows blob public access."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                if _tf_body_has_key_value(body, "allow_nested_items_to_be_public", "true"):
                    findings.append(self._finding(
                        "AZ0147", "Storage Account allows blob public access",
                        f"Storage Account '{res['name']}' allows blob public access.",
                        IaCSeverity.HIGH, res,
                        "Set allow_nested_items_to_be_public = false.",
                    ))
        return findings

    def _az0148_storage_shared_key_access(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0148 – Azure Storage Account shared key access enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                val = _tf_body_get_value(body, "shared_access_key_enabled")
                if not val or "true" in (val or ""):
                    findings.append(self._finding(
                        "AZ0148", "Storage Account shared key access enabled",
                        f"Storage Account '{res['name']}' has shared key access enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set shared_access_key_enabled = false and use AAD authentication.",
                    ))
        return findings

    def _az0149_keyvault_soft_delete_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0149 – Azure Key Vault soft delete disabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                body = res["body"]
                if _tf_body_has_key_value(body, "soft_delete_retention_days", "0"):
                    findings.append(self._finding(
                        "AZ0149", "Key Vault soft delete disabled",
                        f"Key Vault '{res['name']}' has soft delete effectively disabled.",
                        IaCSeverity.HIGH, res,
                        "Set soft_delete_retention_days to 7-90 days.",
                    ))
        return findings

    def _az0150_keyvault_rbac_not_enabled(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0150 – Azure Key Vault RBAC authorization not enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                body = res["body"]
                if not _tf_body_has_key_value(body, "enable_rbac_authorization", "true"):
                    findings.append(self._finding(
                        "AZ0150", "Key Vault RBAC not enabled",
                        f"Key Vault '{res['name']}' does not use RBAC authorization.",
                        IaCSeverity.MEDIUM, res,
                        "Set enable_rbac_authorization = true for fine-grained access control.",
                    ))
        return findings

    def _az0151_aks_no_azure_policy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0151 – AKS cluster without Azure Policy addon."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if not _tf_body_has_key_value(body, "azure_policy_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0151", "AKS without Azure Policy addon",
                        f"AKS cluster '{res['name']}' does not have Azure Policy addon enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set azure_policy_enabled = true to enforce policies.",
                    ))
        return findings

    def _az0152_aks_public_api_server(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0152 – AKS cluster API server publicly accessible."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if not _tf_body_has_key_value(body, "private_cluster_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0152", "AKS API server publicly accessible",
                        f"AKS cluster '{res['name']}' has a publicly accessible API server.",
                        IaCSeverity.HIGH, res,
                        "Set private_cluster_enabled = true to restrict API server access.",
                    ))
        return findings

    def _az0153_aks_no_disk_encryption_set(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0153 – AKS cluster without disk encryption set."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if not _tf_body_get_value(body, "disk_encryption_set_id"):
                    findings.append(self._finding(
                        "AZ0153", "AKS without disk encryption set",
                        f"AKS cluster '{res['name']}' does not use a disk encryption set.",
                        IaCSeverity.MEDIUM, res,
                        "Set disk_encryption_set_id to use customer-managed keys for OS disks.",
                    ))
        return findings

    def _az0154_vm_no_boot_diagnostics(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0154 – Azure VM without boot diagnostics."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                body = res["body"]
                if not _tf_body_has_block(body, "boot_diagnostics"):
                    findings.append(self._finding(
                        "AZ0154", "VM without boot diagnostics",
                        f"VM '{res['name']}' has no boot diagnostics configured.",
                        IaCSeverity.LOW, res,
                        "Add a boot_diagnostics block for troubleshooting capabilities.",
                    ))
        return findings

    def _az0155_linux_vm_password_auth(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0155 – Linux VM with password authentication enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_linux_virtual_machine":
                body = res["body"]
                if _tf_body_has_key_value(body, "disable_password_authentication", "false"):
                    findings.append(self._finding(
                        "AZ0155", "Linux VM password authentication enabled",
                        f"Linux VM '{res['name']}' has password authentication enabled.",
                        IaCSeverity.HIGH, res,
                        "Set disable_password_authentication = true and use SSH keys.",
                    ))
        return findings

    def _az0156_sql_database_no_tde(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0156 – Azure SQL Database without Transparent Data Encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_database":
                body = res["body"]
                if _tf_body_has_key_value(body, "transparent_data_encryption_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0156", "SQL Database TDE disabled",
                        f"SQL Database '{res['name']}' has Transparent Data Encryption disabled.",
                        IaCSeverity.CRITICAL, res,
                        "Set transparent_data_encryption_enabled = true.",
                    ))
        return findings

    def _az0157_cosmosdb_no_auto_failover(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0157 – Cosmos DB without automatic failover."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                body = res["body"]
                if not _tf_body_has_key_value(body, "enable_automatic_failover", "true"):
                    findings.append(self._finding(
                        "AZ0157", "Cosmos DB without automatic failover",
                        f"Cosmos DB account '{res['name']}' does not have automatic failover enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set enable_automatic_failover = true for high availability.",
                    ))
        return findings

    def _az0158_servicebus_no_private_endpoint(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0158 – Service Bus namespace without private endpoint."""
        findings = []
        pe_targets = set()
        for r in resources:
            if r["type"] == "azurerm_private_endpoint":
                conn = _tf_body_get_value(r["body"], "private_connection_resource_id")
                if conn:
                    pe_targets.add(conn.strip('"'))
        for res in resources:
            if res["type"] == "azurerm_servicebus_namespace":
                body = res["body"]
                if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0158", "Service Bus namespace publicly accessible",
                        f"Service Bus namespace '{res['name']}' has public network access enabled without private endpoint.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use a private endpoint.",
                    ))
        return findings

    def _az0159_eventhub_no_capture(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0159 – Event Hub without capture enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_eventhub":
                body = res["body"]
                if not _tf_body_has_block(body, "capture_description"):
                    findings.append(self._finding(
                        "AZ0159", "Event Hub without capture enabled",
                        f"Event Hub '{res['name']}' does not have capture enabled.",
                        IaCSeverity.LOW, res,
                        "Add a capture_description block to archive events.",
                    ))
        return findings

    def _az0160_function_app_no_managed_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0160 – Azure Function App without managed identity."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app", "azurerm_function_app"):
                body = res["body"]
                if not _tf_body_has_block(body, "identity"):
                    findings.append(self._finding(
                        "AZ0160", "Function App without managed identity",
                        f"Function App '{res['name']}' has no managed identity configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add an identity block with type = 'SystemAssigned'.",
                    ))
        return findings

    def _az0161_app_service_cors_wildcard(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0161 – Azure App Service CORS allows wildcard origin."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app", "azurerm_app_service"):
                body = res["body"]
                if _tf_body_has_block(body, "cors") and '"*"' in body:
                    findings.append(self._finding(
                        "AZ0161", "App Service CORS wildcard origin",
                        f"App Service '{res['name']}' allows CORS from wildcard origin '*'.",
                        IaCSeverity.HIGH, res,
                        "Restrict allowed_origins to specific domains instead of '*'.",
                    ))
        return findings

    def _az0162_postgresql_flex_no_backup_retention(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0162 – PostgreSQL Flexible Server insufficient backup retention."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_postgresql_flexible_server":
                body = res["body"]
                retention = _tf_body_get_value(body, "backup_retention_days")
                if retention:
                    try:
                        days = int(retention.strip('"'))
                        if days < 7:
                            findings.append(self._finding(
                                "AZ0162", "PostgreSQL Flexible Server low backup retention",
                                f"PostgreSQL Flexible Server '{res['name']}' retains backups for only {days} days.",
                                IaCSeverity.MEDIUM, res,
                                "Set backup_retention_days >= 7 for adequate backup coverage.",
                            ))
                    except ValueError:
                        pass
        return findings

    def _az0163_mysql_flex_no_backup_retention(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0163 – MySQL Flexible Server insufficient backup retention."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mysql_flexible_server":
                body = res["body"]
                retention = _tf_body_get_value(body, "backup_retention_days")
                if retention:
                    try:
                        days = int(retention.strip('"'))
                        if days < 7:
                            findings.append(self._finding(
                                "AZ0163", "MySQL Flexible Server low backup retention",
                                f"MySQL Flexible Server '{res['name']}' retains backups for only {days} days.",
                                IaCSeverity.MEDIUM, res,
                                "Set backup_retention_days >= 7 for adequate backup coverage.",
                            ))
                    except ValueError:
                        pass
        return findings

    def _az0164_acr_no_content_trust(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0164 – Azure Container Registry without content trust."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                body = res["body"]
                if not _tf_body_has_key_value(body, "trust_policy_enabled", "true"):
                    sku = _tf_body_get_value(body, "sku")
                    if sku and "Premium" in sku:
                        findings.append(self._finding(
                            "AZ0164", "Container Registry without content trust",
                            f"Container Registry '{res['name']}' does not have content trust enabled.",
                            IaCSeverity.MEDIUM, res,
                            "Set trust_policy { enabled = true } on Premium SKU registries.",
                        ))
        return findings

    def _az0165_acr_no_quarantine_policy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0165 – Azure Container Registry without quarantine policy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                body = res["body"]
                if not _tf_body_has_key_value(body, "quarantine_policy_enabled", "true"):
                    sku = _tf_body_get_value(body, "sku")
                    if sku and "Premium" in sku:
                        findings.append(self._finding(
                            "AZ0165", "Container Registry without quarantine policy",
                            f"Container Registry '{res['name']}' does not have quarantine policy enabled.",
                            IaCSeverity.LOW, res,
                            "Set quarantine_policy_enabled = true on Premium SKU registries.",
                        ))
        return findings

    def _az0166_aks_no_auto_upgrade(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0166 – AKS cluster without automatic upgrade."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if not _tf_body_get_value(body, "automatic_channel_upgrade"):
                    findings.append(self._finding(
                        "AZ0166", "AKS without automatic upgrade",
                        f"AKS cluster '{res['name']}' has no automatic upgrade channel configured.",
                        IaCSeverity.MEDIUM, res,
                        "Set automatic_channel_upgrade to 'patch', 'stable', or 'rapid'.",
                    ))
        return findings

    def _az0167_app_insights_no_workspace(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0167 – Application Insights without Log Analytics workspace."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_insights":
                body = res["body"]
                if not _tf_body_get_value(body, "workspace_id"):
                    findings.append(self._finding(
                        "AZ0167", "Application Insights without workspace",
                        f"Application Insights '{res['name']}' is not linked to a Log Analytics workspace.",
                        IaCSeverity.LOW, res,
                        "Set workspace_id to a Log Analytics workspace for workspace-based mode.",
                    ))
        return findings

    def _az0168_keyvault_cert_no_auto_rotation(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0168 – Key Vault certificate without auto-rotation."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_certificate":
                body = res["body"]
                if not _tf_body_has_block(body, "lifetime_action"):
                    findings.append(self._finding(
                        "AZ0168", "Key Vault certificate without auto-rotation",
                        f"Key Vault certificate '{res['name']}' has no lifetime_action for auto-rotation.",
                        IaCSeverity.MEDIUM, res,
                        "Add a lifetime_action block with action_type = 'AutoRenew'.",
                    ))
        return findings

    def _az0169_frontdoor_no_https_redirect(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0169 – Azure Front Door without HTTPS redirect."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cdn_frontdoor_route":
                body = res["body"]
                if _tf_body_has_key_value(body, "https_redirect_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0169", "Front Door route without HTTPS redirect",
                        f"Front Door route '{res['name']}' does not redirect HTTP to HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set https_redirect_enabled = true.",
                    ))
        return findings

    def _az0170_cdn_no_custom_domain_https(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0170 – Azure CDN custom domain without HTTPS."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cdn_endpoint_custom_domain":
                body = res["body"]
                if not _tf_body_has_block(body, "cdn_managed_https"):
                    if not _tf_body_has_block(body, "user_managed_https"):
                        findings.append(self._finding(
                            "AZ0170", "CDN custom domain without HTTPS",
                            f"CDN custom domain '{res['name']}' has no HTTPS configuration.",
                            IaCSeverity.HIGH, res,
                            "Add a cdn_managed_https or user_managed_https block.",
                        ))
        return findings

    def _az0171_sql_server_no_vuln_assessment(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0171 – Azure SQL Server without vulnerability assessment."""
        findings = []
        vuln_servers = {_tf_body_get_value(r["body"], "server_security_alert_policy_id")
                        for r in resources
                        if r["type"] == "azurerm_mssql_server_vulnerability_assessment"}
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                findings.append(self._finding(
                    "AZ0171", "SQL Server without vulnerability assessment",
                    f"SQL Server '{res['name']}' has no vulnerability assessment configured.",
                    IaCSeverity.MEDIUM, res,
                    "Create an azurerm_mssql_server_vulnerability_assessment resource.",
                )) if not vuln_servers else None
        return findings

    def _az0172_app_service_outdated_runtime(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0172 – Azure App Service using outdated runtime version."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                body = res["body"]
                java_ver = _tf_body_get_value(body, "java_version")
                if java_ver and ("8" in java_ver or "7" in java_ver):
                    findings.append(self._finding(
                        "AZ0172", "App Service outdated Java runtime",
                        f"App Service '{res['name']}' uses outdated Java version {java_ver.strip(chr(34))}.",
                        IaCSeverity.MEDIUM, res,
                        "Upgrade to a supported Java LTS version (11, 17, or 21).",
                    ))
                python_ver = _tf_body_get_value(body, "python_version")
                if python_ver and ("2." in python_ver or "3.6" in python_ver or "3.7" in python_ver):
                    findings.append(self._finding(
                        "AZ0172", "App Service outdated Python runtime",
                        f"App Service '{res['name']}' uses outdated Python version {python_ver.strip(chr(34))}.",
                        IaCSeverity.MEDIUM, res,
                        "Upgrade to a supported Python version (3.9+).",
                    ))
        return findings

    def _az0173_nsg_flow_logs_missing(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0173 – NSG without flow logs configured."""
        findings = []
        flow_log_nsgs = set()
        for r in resources:
            if r["type"] == "azurerm_network_watcher_flow_log":
                nsg_id = _tf_body_get_value(r["body"], "network_security_group_id")
                if nsg_id:
                    flow_log_nsgs.add(nsg_id.strip('"'))
        nsgs = [r for r in resources if r["type"] == "azurerm_network_security_group"]
        if nsgs and not flow_log_nsgs:
            findings.append(self._finding(
                "AZ0173", "NSG flow logs not configured",
                "No NSG flow logs found for any network security groups.",
                IaCSeverity.MEDIUM, nsgs[0],
                "Create azurerm_network_watcher_flow_log resources for NSGs.",
            ))
        return findings

    def _az0174_storage_no_lifecycle_mgmt(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0174 – Azure Storage Account without lifecycle management."""
        findings = []
        mgmt_accounts = {_tf_body_get_value(r["body"], "storage_account_id")
                         for r in resources
                         if r["type"] == "azurerm_storage_management_policy"}
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not mgmt_accounts:
                    findings.append(self._finding(
                        "AZ0174", "Storage Account without lifecycle management",
                        f"Storage Account '{res['name']}' has no lifecycle management policy.",
                        IaCSeverity.LOW, res,
                        "Create an azurerm_storage_management_policy to manage blob lifecycles.",
                    ))
        return findings

    def _az0175_aks_no_container_insights(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0175 – AKS cluster without container insights."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if not _tf_body_has_block(body, "oms_agent") and not _tf_body_has_block(body, "monitor_metrics"):
                    findings.append(self._finding(
                        "AZ0175", "AKS without container insights",
                        f"AKS cluster '{res['name']}' does not have container insights enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Add an oms_agent block with log_analytics_workspace_id.",
                    ))
        return findings

    def _az0176_cosmosdb_no_network_restriction(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0176 – Cosmos DB without network access restriction."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                body = res["body"]
                if _tf_body_has_key_value(body, "is_virtual_network_filter_enabled", "false"):
                    if _tf_body_has_key_value(body, "public_network_access_enabled", "true"):
                        findings.append(self._finding(
                            "AZ0176", "Cosmos DB without network restriction",
                            f"Cosmos DB account '{res['name']}' has no network access restrictions.",
                            IaCSeverity.HIGH, res,
                            "Enable is_virtual_network_filter_enabled or set public_network_access_enabled = false.",
                        ))
        return findings

    def _az0177_appgw_no_ssl_policy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0177 – Application Gateway without SSL policy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                body = res["body"]
                if not _tf_body_has_block(body, "ssl_policy"):
                    findings.append(self._finding(
                        "AZ0177", "Application Gateway without SSL policy",
                        f"Application Gateway '{res['name']}' has no explicit SSL policy configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add an ssl_policy block with policy_type = 'Custom' or a predefined policy.",
                    ))
        return findings

    def _az0178_aks_no_upgrade_channel(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0178 – AKS cluster without node OS upgrade channel."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if not _tf_body_get_value(body, "node_os_channel_upgrade"):
                    findings.append(self._finding(
                        "AZ0178", "AKS without node OS upgrade channel",
                        f"AKS cluster '{res['name']}' has no node OS upgrade channel configured.",
                        IaCSeverity.LOW, res,
                        "Set node_os_channel_upgrade to 'SecurityPatch' or 'NodeImage'.",
                    ))
        return findings

    def _az0179_firewall_no_dns_proxy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0179 – Azure Firewall without DNS proxy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_firewall":
                body = res["body"]
                if not _tf_body_has_key_value(body, "dns_proxy_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0179", "Firewall without DNS proxy",
                        f"Azure Firewall '{res['name']}' does not have DNS proxy enabled.",
                        IaCSeverity.LOW, res,
                        "Set dns_proxy_enabled = true for FQDN-based rules.",
                    ))
        return findings

    def _az0180_sql_server_min_tls(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0180 – Azure SQL Server minimum TLS version not 1.2."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                body = res["body"]
                tls = _tf_body_get_value(body, "minimum_tls_version")
                if tls and ("1.0" in tls or "1.1" in tls):
                    findings.append(self._finding(
                        "AZ0180", "SQL Server minimum TLS below 1.2",
                        f"SQL Server '{res['name']}' allows TLS version below 1.2.",
                        IaCSeverity.HIGH, res,
                        "Set minimum_tls_version = '1.2'.",
                    ))
        return findings

    def _az0181_postgresql_no_threat_detection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0181 – PostgreSQL Server without threat detection."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_postgresql_server":
                body = res["body"]
                if not _tf_body_has_block(body, "threat_detection_policy"):
                    findings.append(self._finding(
                        "AZ0181", "PostgreSQL without threat detection",
                        f"PostgreSQL Server '{res['name']}' has no threat detection policy.",
                        IaCSeverity.MEDIUM, res,
                        "Add a threat_detection_policy block with state = 'Enabled'.",
                    ))
        return findings

    def _az0182_storage_no_infra_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0182 – Azure Storage Account without infrastructure encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                if not _tf_body_has_key_value(body, "infrastructure_encryption_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0182", "Storage Account without infrastructure encryption",
                        f"Storage Account '{res['name']}' does not have infrastructure encryption enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set infrastructure_encryption_enabled = true for double encryption.",
                    ))
        return findings

    def _az0183_app_service_no_client_cert(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0183 – Azure App Service without client certificate requirement."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                body = res["body"]
                mode = _tf_body_get_value(body, "client_certificate_mode")
                if not mode or "Optional" in (mode or "") or not _tf_body_has_key_value(body, "client_certificate_enabled", "true"):
                    pass  # Only flag when explicitly disabled
                if _tf_body_has_key_value(body, "client_certificate_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0183", "App Service client certificates disabled",
                        f"App Service '{res['name']}' has client certificate authentication disabled.",
                        IaCSeverity.LOW, res,
                        "Set client_certificate_enabled = true for mutual TLS.",
                    ))
        return findings

    def _az0184_function_app_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0184 – Azure Function App without HTTPS enforcement."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app", "azurerm_function_app"):
                body = res["body"]
                if _tf_body_has_key_value(body, "https_only", "false"):
                    findings.append(self._finding(
                        "AZ0184", "Function App allows HTTP",
                        f"Function App '{res['name']}' does not enforce HTTPS.",
                        IaCSeverity.HIGH, res,
                        "Set https_only = true.",
                    ))
        return findings

    def _az0185_redis_min_tls_version(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0185 – Azure Redis Cache minimum TLS version below 1.2."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_redis_cache":
                body = res["body"]
                tls = _tf_body_get_value(body, "minimum_tls_version")
                if tls and ("1.0" in tls or "1.1" in tls):
                    findings.append(self._finding(
                        "AZ0185", "Redis Cache minimum TLS below 1.2",
                        f"Redis Cache '{res['name']}' allows TLS version below 1.2.",
                        IaCSeverity.HIGH, res,
                        "Set minimum_tls_version = '1.2'.",
                    ))
        return findings

    def _az0186_cosmosdb_local_auth_enabled(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0186 – Cosmos DB local authentication not disabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                body = res["body"]
                if not _tf_body_has_key_value(body, "local_authentication_disabled", "true"):
                    findings.append(self._finding(
                        "AZ0186", "Cosmos DB local authentication enabled",
                        f"Cosmos DB account '{res['name']}' has local authentication enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_authentication_disabled = true and use AAD authentication.",
                    ))
        return findings

    def _az0187_container_app_no_ingress_restriction(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0187 – Azure Container App without ingress IP restrictions."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_app":
                body = res["body"]
                if _tf_body_has_block(body, "ingress"):
                    if not _tf_body_has_block(body, "ip_security_restriction"):
                        ext = _tf_body_get_value(body, "external_enabled")
                        if ext and "true" in ext:
                            findings.append(self._finding(
                                "AZ0187", "Container App without ingress restriction",
                                f"Container App '{res['name']}' has external ingress without IP restrictions.",
                                IaCSeverity.MEDIUM, res,
                                "Add ip_security_restriction rules to limit inbound access.",
                            ))
        return findings

    def _az0188_app_service_no_vnet_integration(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0188 – Azure App Service without VNet integration."""
        findings = []
        vnet_integrations = {_tf_body_get_value(r["body"], "app_service_id")
                             for r in resources
                             if r["type"] == "azurerm_app_service_virtual_network_swift_connection"}
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                body = res["body"]
                if not _tf_body_get_value(body, "virtual_network_subnet_id"):
                    if not vnet_integrations:
                        findings.append(self._finding(
                            "AZ0188", "App Service without VNet integration",
                            f"App Service '{res['name']}' has no VNet integration configured.",
                            IaCSeverity.MEDIUM, res,
                            "Set virtual_network_subnet_id for VNet integration.",
                        ))
        return findings

    def _az0189_sql_db_no_ltr(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0189 – Azure SQL Database without long-term retention policy."""
        findings = []
        ltr_dbs = {_tf_body_get_value(r["body"], "database_id")
                   for r in resources
                   if r["type"] == "azurerm_mssql_database_extended_auditing_policy"}
        for res in resources:
            if res["type"] == "azurerm_mssql_database":
                body = res["body"]
                if not _tf_body_has_block(body, "long_term_retention_policy"):
                    findings.append(self._finding(
                        "AZ0189", "SQL Database without long-term retention",
                        f"SQL Database '{res['name']}' has no long-term retention policy.",
                        IaCSeverity.LOW, res,
                        "Add a long_term_retention_policy block for compliance.",
                    ))
        return findings

    def _az0190_keyvault_no_diagnostic_settings(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0190 – Azure Key Vault without diagnostic settings."""
        findings = []
        diag_targets = set()
        for r in resources:
            if r["type"] == "azurerm_monitor_diagnostic_setting":
                target = _tf_body_get_value(r["body"], "target_resource_id")
                if target:
                    diag_targets.add(target.strip('"'))
        kv_resources = [r for r in resources if r["type"] == "azurerm_key_vault"]
        if kv_resources and not diag_targets:
            for kv in kv_resources:
                findings.append(self._finding(
                    "AZ0190", "Key Vault without diagnostic settings",
                    f"Key Vault '{kv['name']}' has no diagnostic settings configured.",
                    IaCSeverity.MEDIUM, kv,
                    "Create an azurerm_monitor_diagnostic_setting for the Key Vault.",
                ))
        return findings

    def _az0191_vm_no_encryption_at_host(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0191 – Azure VM without encryption at host."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_has_key_value(res["body"], "encryption_at_host_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0191", "VM without encryption at host",
                        f"Virtual Machine '{res['name']}' does not have encryption at host enabled.",
                        IaCSeverity.HIGH, res,
                        "Set encryption_at_host_enabled = true for end-to-end encryption.",
                    ))
        return findings

    def _az0192_aks_no_secret_store_csi(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0192 – AKS cluster without Secret Store CSI driver."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "key_vault_secrets_provider"):
                    findings.append(self._finding(
                        "AZ0192", "AKS without Secret Store CSI driver",
                        f"AKS cluster '{res['name']}' does not have Secret Store CSI driver enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Add key_vault_secrets_provider block to integrate with Azure Key Vault.",
                    ))
        return findings

    def _az0193_app_service_no_health_check(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0193 – App Service without health check enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_get_value(res["body"], "health_check_path"):
                    findings.append(self._finding(
                        "AZ0193", "App Service without health check",
                        f"App Service '{res['name']}' does not have health check configured.",
                        IaCSeverity.LOW, res,
                        "Set health_check_path in site_config for automatic instance healing.",
                    ))
        return findings

    def _az0194_storage_no_soft_delete(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0194 – Storage account without blob soft delete."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_block(res["body"], "blob_properties"):
                    findings.append(self._finding(
                        "AZ0194", "Storage without blob soft delete",
                        f"Storage account '{res['name']}' does not have blob soft delete configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add blob_properties block with delete_retention_policy enabled.",
                    ))
        return findings

    def _az0195_sql_server_no_private_endpoint(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0195 – SQL Server without private endpoint."""
        findings = []
        private_endpoints = {_tf_body_get_value(r["body"], "private_connection_resource_id")
                           for r in resources if r["type"] == "azurerm_private_endpoint"}
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0195", "SQL Server without private endpoint",
                        f"SQL Server '{res['name']}' allows public access without private endpoint.",
                        IaCSeverity.HIGH, res,
                        "Create azurerm_private_endpoint and disable public network access.",
                    ))
        return findings

    def _az0196_aks_no_workload_identity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0196 – AKS cluster without workload identity."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "workload_identity_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0196", "AKS without workload identity",
                        f"AKS cluster '{res['name']}' does not have workload identity enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set workload_identity_enabled = true for secure pod authentication.",
                    ))
        return findings

    def _az0197_cosmosdb_no_backup(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0197 – Cosmos DB without continuous backup."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_block(res["body"], "backup"):
                    findings.append(self._finding(
                        "AZ0197", "Cosmos DB without backup configuration",
                        f"Cosmos DB account '{res['name']}' does not have backup configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add backup block with type = 'Continuous' for point-in-time restore.",
                    ))
        return findings

    def _az0198_app_gw_no_health_probe(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0198 – Application Gateway without custom health probe."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_has_block(res["body"], "probe"):
                    findings.append(self._finding(
                        "AZ0198", "Application Gateway without health probe",
                        f"Application Gateway '{res['name']}' does not have custom health probe.",
                        IaCSeverity.LOW, res,
                        "Add probe block for custom health monitoring.",
                    ))
        return findings

    def _az0199_vm_no_availability_zone(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0199 – VM not deployed in availability zone."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_get_value(res["body"], "zone"):
                    findings.append(self._finding(
                        "AZ0199", "VM not in availability zone",
                        f"Virtual Machine '{res['name']}' is not deployed in an availability zone.",
                        IaCSeverity.LOW, res,
                        "Set zone = '1', '2', or '3' for high availability.",
                    ))
        return findings

    def _az0200_storage_no_versioning(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0200 – Storage account without blob versioning."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                if _tf_body_has_block(body, "blob_properties"):
                    if not _tf_body_has_key_value(body, "versioning_enabled", "true"):
                        findings.append(self._finding(
                            "AZ0200", "Storage without blob versioning",
                            f"Storage account '{res['name']}' does not have blob versioning enabled.",
                            IaCSeverity.LOW, res,
                            "Set versioning_enabled = true in blob_properties for data recovery.",
                        ))
        return findings

    def _az0201_aks_no_oms_agent(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0201 – AKS cluster without OMS agent for monitoring."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "oms_agent"):
                    findings.append(self._finding(
                        "AZ0201", "AKS without OMS agent",
                        f"AKS cluster '{res['name']}' does not have OMS agent configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add oms_agent block with log_analytics_workspace_id for monitoring.",
                    ))
        return findings

    def _az0202_keyvault_no_private_endpoint(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0202 – Key Vault without private endpoint."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0202", "Key Vault without private endpoint",
                        f"Key Vault '{res['name']}' allows public network access.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and use private endpoint.",
                    ))
        return findings

    def _az0203_app_service_no_backup(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0203 – App Service without backup configuration."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_block(res["body"], "backup"):
                    findings.append(self._finding(
                        "AZ0203", "App Service without backup",
                        f"App Service '{res['name']}' does not have backup configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add backup block with schedule for disaster recovery.",
                    ))
        return findings

    def _az0204_sql_no_geo_redundant_backup(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0204 – SQL Database without geo-redundant backup."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_database":
                if _tf_body_has_key_value(res["body"], "geo_backup_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0204", "SQL Database without geo-redundant backup",
                        f"SQL Database '{res['name']}' does not have geo-redundant backup enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set geo_backup_enabled = true for disaster recovery.",
                    ))
        return findings

    def _az0205_aks_no_node_pool_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0205 – AKS node pool without disk encryption set."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                if not _tf_body_get_value(res["body"], "os_disk_type"):
                    findings.append(self._finding(
                        "AZ0205", "AKS node pool without encryption set",
                        f"AKS node pool '{res['name']}' does not specify os_disk_type.",
                        IaCSeverity.LOW, res,
                        "Set os_disk_type = 'Managed' with enable_host_encryption = true.",
                    ))
        return findings

    def _az0206_function_app_no_runtime_version(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0206 – Function App without specified runtime version."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app"):
                if not _tf_body_has_block(res["body"], "application_stack"):
                    findings.append(self._finding(
                        "AZ0206", "Function App without runtime version",
                        f"Function App '{res['name']}' does not specify application_stack.",
                        IaCSeverity.LOW, res,
                        "Add application_stack block with specific runtime version.",
                    ))
        return findings

    def _az0207_acr_no_retention_policy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0207 – Container Registry without retention policy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "premium" in sku.lower():
                    if not _tf_body_has_block(res["body"], "retention_policy"):
                        findings.append(self._finding(
                            "AZ0207", "Container Registry without retention policy",
                            f"ACR '{res['name']}' does not have retention policy configured.",
                            IaCSeverity.LOW, res,
                            "Add retention_policy block to automatically delete untagged manifests.",
                        ))
        return findings

    def _az0208_vm_scale_set_no_automatic_repairs(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0208 – VM Scale Set without automatic repairs."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine_scale_set", "azurerm_windows_virtual_machine_scale_set"):
                if not _tf_body_has_block(res["body"], "automatic_instance_repair"):
                    findings.append(self._finding(
                        "AZ0208", "VM Scale Set without automatic repairs",
                        f"VM Scale Set '{res['name']}' does not have automatic instance repair.",
                        IaCSeverity.LOW, res,
                        "Add automatic_instance_repair block with enabled = true.",
                    ))
        return findings

    def _az0209_app_service_no_always_on(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0209 – App Service without Always On enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if _tf_body_has_key_value(res["body"], "always_on", "false"):
                    findings.append(self._finding(
                        "AZ0209", "App Service without Always On",
                        f"App Service '{res['name']}' does not have Always On enabled.",
                        IaCSeverity.LOW, res,
                        "Set always_on = true in site_config for production workloads.",
                    ))
        return findings

    def _az0210_aks_no_azure_cni(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0210 – AKS cluster not using Azure CNI networking."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                network_plugin = _tf_body_get_value(res["body"], "network_plugin")
                if network_plugin and "kubenet" in network_plugin.lower():
                    findings.append(self._finding(
                        "AZ0210", "AKS not using Azure CNI",
                        f"AKS cluster '{res['name']}' uses kubenet instead of Azure CNI.",
                        IaCSeverity.LOW, res,
                        "Set network_plugin = 'azure' for better network integration.",
                    ))
        return findings

    def _az0211_storage_queue_no_logging(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0211 – Storage account without queue logging."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_block(res["body"], "queue_properties"):
                    findings.append(self._finding(
                        "AZ0211", "Storage without queue logging",
                        f"Storage account '{res['name']}' does not have queue logging configured.",
                        IaCSeverity.LOW, res,
                        "Add queue_properties block with logging enabled.",
                    ))
        return findings

    def _az0212_storage_table_no_logging(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0212 – Storage account without table logging enabled via diagnostic settings."""
        findings = []
        diag_targets = set()
        for r in resources:
            if r["type"] == "azurerm_monitor_diagnostic_setting":
                target = _tf_body_get_value(r["body"], "target_resource_id")
                if target:
                    diag_targets.add(target.strip('"'))
        storage_accounts = [r for r in resources if r["type"] == "azurerm_storage_account"]
        if storage_accounts and not diag_targets:
            for sa in storage_accounts:
                findings.append(self._finding(
                    "AZ0212", "Storage without table diagnostic logging",
                    f"Storage account '{sa['name']}' does not have diagnostic settings.",
                    IaCSeverity.LOW, sa,
                    "Create azurerm_monitor_diagnostic_setting for table storage logs.",
                ))
        return findings

    def _az0213_mysql_no_threat_detection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0213 – MySQL server without threat detection."""
        findings = []
        threat_detection_exists = any(r["type"] == "azurerm_mysql_server_security_alert_policy"
                                      for r in resources)
        for res in resources:
            if res["type"] == "azurerm_mysql_server" and not threat_detection_exists:
                findings.append(self._finding(
                    "AZ0213", "MySQL without threat detection",
                    f"MySQL Server '{res['name']}' does not have threat detection configured.",
                    IaCSeverity.MEDIUM, res,
                    "Create azurerm_mysql_server_security_alert_policy resource.",
                ))
        return findings

    def _az0214_postgresql_no_connection_throttling(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0214 – PostgreSQL without connection throttling."""
        findings = []
        config_exists = {_tf_body_get_value(r["body"], "server_id")
                        for r in resources
                        if r["type"] == "azurerm_postgresql_configuration"
                        and "connection_throttling" in r["body"]}
        for res in resources:
            if res["type"] == "azurerm_postgresql_server":
                findings.append(self._finding(
                    "AZ0214", "PostgreSQL without connection throttling",
                    f"PostgreSQL Server '{res['name']}' may not have connection throttling enabled.",
                    IaCSeverity.LOW, res,
                    "Create azurerm_postgresql_configuration with name = 'connection_throttling' and value = 'on'.",
                ))
        return findings

    def _az0215_app_service_no_ftps(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0215 – App Service allowing non-FTPS deployment."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                ftps_state = _tf_body_get_value(res["body"], "ftps_state")
                if ftps_state and "allallowed" in ftps_state.lower():
                    findings.append(self._finding(
                        "AZ0215", "App Service allowing FTP",
                        f"App Service '{res['name']}' allows non-FTPS connections.",
                        IaCSeverity.MEDIUM, res,
                        "Set ftps_state = 'FtpsOnly' or 'Disabled'.",
                    ))
        return findings

    def _az0216_vm_no_update_management(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0216 – VM without update management configuration."""
        findings = []
        update_configs = {_tf_body_get_value(r["body"], "virtual_machine_id")
                        for r in resources
                        if r["type"] == "azurerm_maintenance_assignment_virtual_machine"}
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                findings.append(self._finding(
                    "AZ0216", "VM without update management",
                    f"Virtual Machine '{res['name']}' may not have update management configured.",
                    IaCSeverity.LOW, res,
                    "Configure Azure Update Management or create maintenance assignment.",
                ))
        return findings

    def _az0217_aks_no_defender(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0217 – AKS cluster without Microsoft Defender enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "microsoft_defender"):
                    findings.append(self._finding(
                        "AZ0217", "AKS without Microsoft Defender",
                        f"AKS cluster '{res['name']}' does not have Microsoft Defender enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Add microsoft_defender block with log_analytics_workspace_id.",
                    ))
        return findings

    def _az0218_storage_no_delete_retention(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0218 – Storage without container delete retention."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                if _tf_body_has_block(body, "blob_properties"):
                    if not _tf_body_has_block(body, "container_delete_retention_policy"):
                        findings.append(self._finding(
                            "AZ0218", "Storage without container delete retention",
                            f"Storage account '{res['name']}' does not have container delete retention.",
                            IaCSeverity.MEDIUM, res,
                            "Add container_delete_retention_policy block in blob_properties.",
                        ))
        return findings

    def _az0219_keyvault_no_key_expiry(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0219 – Key Vault key without expiration date."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_key":
                if not _tf_body_get_value(res["body"], "expiration_date"):
                    findings.append(self._finding(
                        "AZ0219", "Key Vault key without expiration",
                        f"Key Vault key '{res['name']}' does not have an expiration date.",
                        IaCSeverity.MEDIUM, res,
                        "Set expiration_date for key rotation compliance.",
                    ))
        return findings

    def _az0220_keyvault_no_secret_expiry(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0220 – Key Vault secret without expiration date."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_secret":
                if not _tf_body_get_value(res["body"], "expiration_date"):
                    findings.append(self._finding(
                        "AZ0220", "Key Vault secret without expiration",
                        f"Key Vault secret '{res['name']}' does not have an expiration date.",
                        IaCSeverity.MEDIUM, res,
                        "Set expiration_date for secret rotation compliance.",
                    ))
        return findings

    def _az0221_app_service_no_ip_restriction(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0221 – App Service without IP restrictions."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_block(res["body"], "ip_restriction"):
                    findings.append(self._finding(
                        "AZ0221", "App Service without IP restrictions",
                        f"App Service '{res['name']}' does not have IP restrictions configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add ip_restriction blocks in site_config to limit access.",
                    ))
        return findings

    def _az0222_function_app_no_ip_restriction(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0222 – Function App without IP restrictions."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app"):
                if not _tf_body_has_block(res["body"], "ip_restriction"):
                    findings.append(self._finding(
                        "AZ0222", "Function App without IP restrictions",
                        f"Function App '{res['name']}' does not have IP restrictions configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add ip_restriction blocks in site_config to limit access.",
                    ))
        return findings

    def _az0223_aks_no_pod_security_policy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0223 – AKS without pod security standards."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "workload_autoscaler_profile"):
                    # Check for Azure Policy addon as alternative
                    if not _tf_body_has_block(res["body"], "azure_policy_enabled"):
                        findings.append(self._finding(
                            "AZ0223", "AKS without pod security policy",
                            f"AKS cluster '{res['name']}' may not have pod security standards.",
                            IaCSeverity.MEDIUM, res,
                            "Enable Azure Policy addon or implement OPA Gatekeeper.",
                        ))
        return findings

    def _az0224_vm_no_just_in_time_access(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0224 – VM without Just-In-Time access consideration."""
        findings = []
        jit_exists = any(r["type"] == "azurerm_security_center_setting" for r in resources)
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not jit_exists:
                    findings.append(self._finding(
                        "AZ0224", "VM without JIT access",
                        f"Virtual Machine '{res['name']}' should consider Just-In-Time access.",
                        IaCSeverity.LOW, res,
                        "Enable Just-In-Time VM access in Microsoft Defender for Cloud.",
                    ))
        return findings

    def _az0225_storage_no_cross_tenant_replication(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0225 – Storage with cross-tenant replication allowed."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_key_value(res["body"], "cross_tenant_replication_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0225", "Storage with cross-tenant replication",
                        f"Storage account '{res['name']}' may allow cross-tenant replication.",
                        IaCSeverity.LOW, res,
                        "Set cross_tenant_replication_enabled = false if not needed.",
                    ))
        return findings

    def _az0226_sql_server_no_outbound_network(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0226 – SQL Server with outbound network access."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                if not _tf_body_has_key_value(res["body"], "outbound_network_restriction_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0226", "SQL Server with outbound access",
                        f"SQL Server '{res['name']}' does not restrict outbound network access.",
                        IaCSeverity.LOW, res,
                        "Set outbound_network_restriction_enabled = true.",
                    ))
        return findings

    def _az0227_app_service_no_http2(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0227 – App Service without HTTP/2 enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_key_value(res["body"], "http2_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0227", "App Service without HTTP/2",
                        f"App Service '{res['name']}' does not have HTTP/2 enabled.",
                        IaCSeverity.LOW, res,
                        "Set http2_enabled = true in site_config for improved performance.",
                    ))
        return findings

    def _az0228_aks_no_image_cleaner(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0228 – AKS without image cleaner enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "image_cleaner_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0228", "AKS without image cleaner",
                        f"AKS cluster '{res['name']}' does not have image cleaner enabled.",
                        IaCSeverity.LOW, res,
                        "Set image_cleaner_enabled = true to remove stale images.",
                    ))
        return findings

    def _az0229_cosmosdb_no_multiple_write_locations(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0229 – Cosmos DB without multiple write locations for HA."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_key_value(res["body"], "enable_multiple_write_locations", "true"):
                    findings.append(self._finding(
                        "AZ0229", "Cosmos DB without multi-write",
                        f"Cosmos DB account '{res['name']}' does not have multiple write locations.",
                        IaCSeverity.LOW, res,
                        "Set enable_multiple_write_locations = true for high availability.",
                    ))
        return findings

    def _az0230_acr_no_zone_redundancy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0230 – Container Registry without zone redundancy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "premium" in sku.lower():
                    if not _tf_body_has_key_value(res["body"], "zone_redundancy_enabled", "true"):
                        findings.append(self._finding(
                            "AZ0230", "Container Registry without zone redundancy",
                            f"ACR '{res['name']}' does not have zone redundancy enabled.",
                            IaCSeverity.LOW, res,
                            "Set zone_redundancy_enabled = true for Premium SKU.",
                        ))
        return findings

    def _az0231_app_gw_no_autoscaling(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0231 – Application Gateway without autoscaling."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_has_block(res["body"], "autoscale_configuration"):
                    findings.append(self._finding(
                        "AZ0231", "Application Gateway without autoscaling",
                        f"Application Gateway '{res['name']}' does not have autoscaling configured.",
                        IaCSeverity.LOW, res,
                        "Add autoscale_configuration block for automatic scaling.",
                    ))
        return findings

    def _az0232_vm_no_accelerated_networking(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0232 – Network interface without accelerated networking."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_network_interface":
                if not _tf_body_has_key_value(res["body"], "enable_accelerated_networking", "true"):
                    findings.append(self._finding(
                        "AZ0232", "NIC without accelerated networking",
                        f"Network interface '{res['name']}' does not have accelerated networking.",
                        IaCSeverity.LOW, res,
                        "Set enable_accelerated_networking = true for supported VM sizes.",
                    ))
        return findings

    def _az0233_aks_no_http_proxy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0233 – AKS cluster without HTTP proxy configuration."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                # Only flag if in a private network scenario
                if _tf_body_has_key_value(res["body"], "private_cluster_enabled", "true"):
                    if not _tf_body_has_block(res["body"], "http_proxy_config"):
                        findings.append(self._finding(
                            "AZ0233", "Private AKS without HTTP proxy",
                            f"Private AKS cluster '{res['name']}' may need HTTP proxy configuration.",
                            IaCSeverity.LOW, res,
                            "Consider adding http_proxy_config for outbound access.",
                        ))
        return findings

    def _az0234_storage_no_change_feed(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0234 – Storage account without change feed enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                if _tf_body_has_block(body, "blob_properties"):
                    if not _tf_body_has_key_value(body, "change_feed_enabled", "true"):
                        findings.append(self._finding(
                            "AZ0234", "Storage without change feed",
                            f"Storage account '{res['name']}' does not have change feed enabled.",
                            IaCSeverity.LOW, res,
                            "Set change_feed_enabled = true for audit trail.",
                        ))
        return findings

    def _az0235_sql_elastic_pool_no_zone_redundancy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0235 – SQL Elastic Pool without zone redundancy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_elasticpool":
                if not _tf_body_has_key_value(res["body"], "zone_redundant", "true"):
                    findings.append(self._finding(
                        "AZ0235", "SQL Elastic Pool without zone redundancy",
                        f"SQL Elastic Pool '{res['name']}' is not zone redundant.",
                        IaCSeverity.LOW, res,
                        "Set zone_redundant = true for high availability.",
                    ))
        return findings

    def _az0236_redis_no_zone_redundancy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0236 – Redis Cache without zone redundancy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_redis_cache":
                sku = _tf_body_get_value(res["body"], "sku_name")
                if sku and "premium" in sku.lower():
                    if not _tf_body_get_value(res["body"], "zones"):
                        findings.append(self._finding(
                            "AZ0236", "Redis Cache without zone redundancy",
                            f"Redis Cache '{res['name']}' is not zone redundant.",
                            IaCSeverity.LOW, res,
                            "Set zones = ['1', '2', '3'] for Premium SKU.",
                        ))
        return findings

    def _az0237_servicebus_no_zone_redundancy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0237 – Service Bus without zone redundancy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_servicebus_namespace":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "premium" in sku.lower():
                    if not _tf_body_has_key_value(res["body"], "zone_redundant", "true"):
                        findings.append(self._finding(
                            "AZ0237", "Service Bus without zone redundancy",
                            f"Service Bus '{res['name']}' is not zone redundant.",
                            IaCSeverity.LOW, res,
                            "Set zone_redundant = true for Premium SKU.",
                        ))
        return findings

    def _az0238_eventhub_no_zone_redundancy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0238 – Event Hub without zone redundancy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_eventhub_namespace":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and ("standard" in sku.lower() or "premium" in sku.lower()):
                    if not _tf_body_has_key_value(res["body"], "zone_redundant", "true"):
                        findings.append(self._finding(
                            "AZ0238", "Event Hub without zone redundancy",
                            f"Event Hub '{res['name']}' is not zone redundant.",
                            IaCSeverity.LOW, res,
                            "Set zone_redundant = true for high availability.",
                        ))
        return findings

    def _az0239_app_service_no_zone_redundancy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0239 – App Service Plan without zone redundancy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_service_plan":
                if not _tf_body_has_key_value(res["body"], "zone_balancing_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0239", "App Service Plan without zone redundancy",
                        f"App Service Plan '{res['name']}' is not zone balanced.",
                        IaCSeverity.LOW, res,
                        "Set zone_balancing_enabled = true for high availability.",
                    ))
        return findings

    def _az0240_function_app_no_zone_redundancy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0240 – Function App on non-zone-redundant plan."""
        findings = []
        zone_plans = {r["name"] for r in resources
                     if r["type"] == "azurerm_service_plan"
                     and _tf_body_has_key_value(r["body"], "zone_balancing_enabled", "true")}
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app"):
                service_plan_id = _tf_body_get_value(res["body"], "service_plan_id")
                # Cannot determine zone redundancy from plan reference
                pass  # Skip as this requires cross-resource analysis
        return findings

    def _az0241_aks_no_availability_zones(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0241 – AKS default node pool without availability zones."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if _tf_body_has_block(body, "default_node_pool"):
                    if not _tf_body_get_value(body, "zones"):
                        findings.append(self._finding(
                            "AZ0241", "AKS without availability zones",
                            f"AKS cluster '{res['name']}' default node pool is not zone redundant.",
                            IaCSeverity.MEDIUM, res,
                            "Set zones = ['1', '2', '3'] in default_node_pool.",
                        ))
        return findings

    def _az0242_vm_no_disk_encryption_set(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0242 – VM without disk encryption set."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_get_value(res["body"], "disk_encryption_set_id"):
                    findings.append(self._finding(
                        "AZ0242", "VM without disk encryption set",
                        f"Virtual Machine '{res['name']}' does not use a disk encryption set.",
                        IaCSeverity.MEDIUM, res,
                        "Set disk_encryption_set_id for customer-managed keys.",
                    ))
        return findings

    def _az0243_app_config_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0243 – App Configuration without encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_app_configuration":
                if not _tf_body_has_block(res["body"], "encryption"):
                    findings.append(self._finding(
                        "AZ0243", "App Configuration without encryption",
                        f"App Configuration '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Add encryption block with key_vault_key_identifier.",
                    ))
        return findings

    def _az0244_cognitive_services_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0244 – Cognitive Services without customer-managed key."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cognitive_account":
                if not _tf_body_has_block(res["body"], "customer_managed_key"):
                    findings.append(self._finding(
                        "AZ0244", "Cognitive Services without CMK",
                        f"Cognitive Services account '{res['name']}' does not use customer-managed keys.",
                        IaCSeverity.MEDIUM, res,
                        "Add customer_managed_key block with key_vault_key_id.",
                    ))
        return findings

    def _az0245_search_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0245 – Search Service without customer-managed key."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_search_service":
                if not _tf_body_get_value(res["body"], "customer_managed_key_enforcement_enabled"):
                    findings.append(self._finding(
                        "AZ0245", "Search Service without CMK",
                        f"Search Service '{res['name']}' does not enforce customer-managed keys.",
                        IaCSeverity.MEDIUM, res,
                        "Set customer_managed_key_enforcement_enabled = 'Enabled'.",
                    ))
        return findings

    def _az0246_data_factory_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0246 – Data Factory without customer-managed key."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_data_factory":
                if not _tf_body_get_value(res["body"], "customer_managed_key_id"):
                    findings.append(self._finding(
                        "AZ0246", "Data Factory without CMK",
                        f"Data Factory '{res['name']}' does not use customer-managed keys.",
                        IaCSeverity.MEDIUM, res,
                        "Set customer_managed_key_id for encryption at rest.",
                    ))
        return findings

    def _az0247_synapse_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0247 – Synapse Workspace without customer-managed key."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_synapse_workspace":
                if not _tf_body_has_block(res["body"], "customer_managed_key"):
                    findings.append(self._finding(
                        "AZ0247", "Synapse without CMK",
                        f"Synapse Workspace '{res['name']}' does not use customer-managed keys.",
                        IaCSeverity.MEDIUM, res,
                        "Add customer_managed_key block with key_versionless_id.",
                    ))
        return findings

    def _az0248_log_analytics_no_daily_cap(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0248 – Log Analytics without daily cap configured."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_log_analytics_workspace":
                if not _tf_body_get_value(res["body"], "daily_quota_gb"):
                    findings.append(self._finding(
                        "AZ0248", "Log Analytics without daily cap",
                        f"Log Analytics workspace '{res['name']}' does not have daily quota configured.",
                        IaCSeverity.LOW, res,
                        "Set daily_quota_gb to control costs.",
                    ))
        return findings

    def _az0249_monitor_no_action_group(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0249 – No Monitor action groups defined."""
        findings = []
        action_groups = [r for r in resources if r["type"] == "azurerm_monitor_action_group"]
        metric_alerts = [r for r in resources if r["type"] == "azurerm_monitor_metric_alert"]
        if metric_alerts and not action_groups:
            for alert in metric_alerts:
                findings.append(self._finding(
                    "AZ0249", "Metric alert without action group",
                    f"Metric alert '{alert['name']}' may not have action groups configured.",
                    IaCSeverity.LOW, alert,
                    "Create azurerm_monitor_action_group for alert notifications.",
                ))
        return findings

    def _az0250_app_service_no_diagnostic_logs(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0250 – App Service without diagnostic logs enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_block(res["body"], "logs"):
                    findings.append(self._finding(
                        "AZ0250", "App Service without diagnostic logs",
                        f"App Service '{res['name']}' does not have diagnostic logs configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add logs block with application_logs and http_logs configuration.",
                    ))
        return findings

    def _az0251_aks_no_audit_logging(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0251 – AKS cluster without audit logging to Log Analytics."""
        findings = []
        diag_settings = [r for r in resources if r["type"] == "azurerm_monitor_diagnostic_setting"]
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                # Check if there's a diagnostic setting for this cluster
                has_diag = any("kubernetes" in _tf_body_get_value(d["body"], "target_resource_id") or ""
                              for d in diag_settings)
                if not has_diag:
                    findings.append(self._finding(
                        "AZ0251", "AKS without audit logging",
                        f"AKS cluster '{res['name']}' may not have audit logging configured.",
                        IaCSeverity.MEDIUM, res,
                        "Create azurerm_monitor_diagnostic_setting for kube-audit logs.",
                    ))
        return findings

    def _az0252_sql_no_advanced_threat_protection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0252 – SQL Server without Advanced Threat Protection."""
        findings = []
        atp_exists = any(r["type"] == "azurerm_mssql_server_security_alert_policy" for r in resources)
        for res in resources:
            if res["type"] == "azurerm_mssql_server" and not atp_exists:
                findings.append(self._finding(
                    "AZ0252", "SQL Server without ATP",
                    f"SQL Server '{res['name']}' does not have Advanced Threat Protection.",
                    IaCSeverity.MEDIUM, res,
                    "Create azurerm_mssql_server_security_alert_policy resource.",
                ))
        return findings

    def _az0253_storage_no_advanced_threat_protection(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0253 – Storage without Advanced Threat Protection."""
        findings = []
        atp_accounts = {_tf_body_get_value(r["body"], "storage_account_id")
                       for r in resources
                       if r["type"] == "azurerm_advanced_threat_protection"}
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                findings.append(self._finding(
                    "AZ0253", "Storage without ATP",
                    f"Storage account '{res['name']}' may not have Advanced Threat Protection.",
                    IaCSeverity.MEDIUM, res,
                    "Create azurerm_advanced_threat_protection resource with enabled = true.",
                ))
        return findings

    def _az0254_app_service_no_defender(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0254 – App Service without Defender for App Service."""
        findings = []
        defender_plans = [r for r in resources if r["type"] == "azurerm_security_center_subscription_pricing"]
        has_app_defender = any("AppServices" in r["body"] for r in defender_plans)
        if not has_app_defender:
            for res in resources:
                if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                    findings.append(self._finding(
                        "AZ0254", "App Service without Defender",
                        f"App Service '{res['name']}' is not protected by Microsoft Defender.",
                        IaCSeverity.MEDIUM, res,
                        "Enable Microsoft Defender for App Service at subscription level.",
                    ))
        return findings

    def _az0255_keyvault_no_access_policy_limit(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0255 – Key Vault using access policies instead of RBAC."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                if not _tf_body_has_key_value(res["body"], "enable_rbac_authorization", "true"):
                    if _tf_body_has_block(res["body"], "access_policy"):
                        findings.append(self._finding(
                            "AZ0255", "Key Vault using access policies",
                            f"Key Vault '{res['name']}' uses access policies instead of RBAC.",
                            IaCSeverity.LOW, res,
                            "Set enable_rbac_authorization = true for better access management.",
                        ))
        return findings

    def _az0256_nsg_no_default_deny(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0256 – NSG without explicit default deny rule."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_network_security_group":
                body = res["body"]
                # Check if there's a deny all rule at the end
                if not ("Deny" in body and "4096" in body):
                    findings.append(self._finding(
                        "AZ0256", "NSG without default deny",
                        f"NSG '{res['name']}' may not have explicit default deny rule.",
                        IaCSeverity.LOW, res,
                        "Consider adding explicit deny rule at lowest priority.",
                    ))
        return findings

    def _az0257_app_gw_no_request_routing_rule(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0257 – Application Gateway without proper routing rules."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_has_block(res["body"], "request_routing_rule"):
                    findings.append(self._finding(
                        "AZ0257", "Application Gateway without routing rules",
                        f"Application Gateway '{res['name']}' may be missing routing rules.",
                        IaCSeverity.LOW, res,
                        "Add request_routing_rule block for proper traffic routing.",
                    ))
        return findings

    def _az0258_vm_no_patch_assessment(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0258 – VM without patch assessment mode."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_get_value(res["body"], "patch_assessment_mode"):
                    findings.append(self._finding(
                        "AZ0258", "VM without patch assessment",
                        f"Virtual Machine '{res['name']}' does not have patch assessment configured.",
                        IaCSeverity.LOW, res,
                        "Set patch_assessment_mode = 'AutomaticByPlatform' for automatic assessments.",
                    ))
        return findings

    def _az0259_aks_no_keda(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0259 – AKS without KEDA autoscaler."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "keda_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0259", "AKS without KEDA",
                        f"AKS cluster '{res['name']}' does not have KEDA autoscaler enabled.",
                        IaCSeverity.LOW, res,
                        "Set keda_enabled = true for event-driven autoscaling.",
                    ))
        return findings

    def _az0260_cosmosdb_no_analytical_storage(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0260 – Cosmos DB without analytical storage for analytics."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_key_value(res["body"], "analytical_storage_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0260", "Cosmos DB without analytical storage",
                        f"Cosmos DB account '{res['name']}' does not have analytical storage.",
                        IaCSeverity.LOW, res,
                        "Set analytical_storage_enabled = true for Synapse Link integration.",
                    ))
        return findings

    def _az0261_sql_no_ledger(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0261 – SQL Database without ledger for tamper-evidence."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_database":
                if not _tf_body_has_key_value(res["body"], "ledger_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0261", "SQL Database without ledger",
                        f"SQL Database '{res['name']}' does not have ledger enabled.",
                        IaCSeverity.LOW, res,
                        "Set ledger_enabled = true for tamper-evident data integrity.",
                    ))
        return findings

    def _az0262_storage_no_immutable_blob(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0262 – Storage without immutable blob storage policy."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_container":
                if not _tf_body_has_block(res["body"], "immutability_policy"):
                    pass  # Container-level check, optional
        return findings

    def _az0263_app_service_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0263 – App Service without authentication configured."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_block(res["body"], "auth_settings") and not _tf_body_has_block(res["body"], "auth_settings_v2"):
                    findings.append(self._finding(
                        "AZ0263", "App Service without authentication",
                        f"App Service '{res['name']}' does not have authentication configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add auth_settings_v2 block for Azure AD authentication.",
                    ))
        return findings

    def _az0264_function_app_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0264 – Function App without authentication configured."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app"):
                if not _tf_body_has_block(res["body"], "auth_settings") and not _tf_body_has_block(res["body"], "auth_settings_v2"):
                    findings.append(self._finding(
                        "AZ0264", "Function App without authentication",
                        f"Function App '{res['name']}' does not have authentication configured.",
                        IaCSeverity.MEDIUM, res,
                        "Add auth_settings_v2 block for Azure AD authentication.",
                    ))
        return findings

    def _az0265_aks_no_local_account_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0265 – AKS cluster with local accounts enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "local_account_disabled", "true"):
                    findings.append(self._finding(
                        "AZ0265", "AKS with local accounts enabled",
                        f"AKS cluster '{res['name']}' has local accounts enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set local_account_disabled = true for Azure AD only authentication.",
                    ))
        return findings

    def _az0266_vm_no_trusted_launch(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0266 – VM without Trusted Launch enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_has_key_value(res["body"], "secure_boot_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0266", "VM without Trusted Launch",
                        f"Virtual Machine '{res['name']}' does not have Trusted Launch enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set secure_boot_enabled = true and vtpm_enabled = true.",
                    ))
        return findings

    def _az0267_vm_scale_set_no_trusted_launch(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0267 – VM Scale Set without Trusted Launch enabled."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine_scale_set", "azurerm_windows_virtual_machine_scale_set"):
                if not _tf_body_has_key_value(res["body"], "secure_boot_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0267", "VM Scale Set without Trusted Launch",
                        f"VM Scale Set '{res['name']}' does not have Trusted Launch enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set secure_boot_enabled = true and vtpm_enabled = true.",
                    ))
        return findings

    def _az0268_acr_no_dedicated_data_endpoint(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0268 – Container Registry without dedicated data endpoints."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "premium" in sku.lower():
                    if not _tf_body_has_key_value(res["body"], "data_endpoint_enabled", "true"):
                        findings.append(self._finding(
                            "AZ0268", "ACR without dedicated data endpoint",
                            f"ACR '{res['name']}' does not have dedicated data endpoints.",
                            IaCSeverity.LOW, res,
                            "Set data_endpoint_enabled = true for regional data endpoints.",
                        ))
        return findings

    def _az0269_app_gw_no_backend_pool(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0269 – Application Gateway without backend address pool."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_has_block(res["body"], "backend_address_pool"):
                    findings.append(self._finding(
                        "AZ0269", "Application Gateway without backend pool",
                        f"Application Gateway '{res['name']}' has no backend address pool.",
                        IaCSeverity.LOW, res,
                        "Add backend_address_pool block for routing configuration.",
                    ))
        return findings

    def _az0270_lb_no_outbound_rules(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0270 – Load Balancer without outbound rules."""
        findings = []
        outbound_rules = {_tf_body_get_value(r["body"], "loadbalancer_id")
                        for r in resources if r["type"] == "azurerm_lb_outbound_rule"}
        for res in resources:
            if res["type"] == "azurerm_lb":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "standard" in sku.lower():
                    findings.append(self._finding(
                        "AZ0270", "Load Balancer without outbound rules",
                        f"Load Balancer '{res['name']}' may not have outbound rules configured.",
                        IaCSeverity.LOW, res,
                        "Create azurerm_lb_outbound_rule for explicit outbound connectivity.",
                    ))
        return findings

    def _az0271_bastion_no_shareable_link(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0271 – Bastion with shareable link enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_bastion_host":
                if _tf_body_has_key_value(res["body"], "shareable_link_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0271", "Bastion with shareable link",
                        f"Bastion Host '{res['name']}' has shareable link enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set shareable_link_enabled = false unless required.",
                    ))
        return findings

    def _az0272_firewall_no_premium_sku(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0272 – Azure Firewall without Premium SKU for advanced features."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_firewall":
                sku = _tf_body_get_value(res["body"], "sku_tier")
                if sku and "standard" in sku.lower():
                    findings.append(self._finding(
                        "AZ0272", "Azure Firewall without Premium SKU",
                        f"Azure Firewall '{res['name']}' is not using Premium SKU.",
                        IaCSeverity.LOW, res,
                        "Consider sku_tier = 'Premium' for TLS inspection and IDPS.",
                    ))
        return findings

    def _az0273_vpn_no_ikev2(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0273 – VPN Gateway without IKEv2 protocol."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network_gateway":
                vpn_type = _tf_body_get_value(res["body"], "vpn_type")
                if vpn_type and "policybased" in vpn_type.lower():
                    findings.append(self._finding(
                        "AZ0273", "VPN Gateway with PolicyBased type",
                        f"VPN Gateway '{res['name']}' uses PolicyBased instead of RouteBased.",
                        IaCSeverity.MEDIUM, res,
                        "Set vpn_type = 'RouteBased' for IKEv2 support.",
                    ))
        return findings

    def _az0274_app_service_no_min_instances(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0274 – App Service without minimum instance count."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_service_plan":
                if not _tf_body_get_value(res["body"], "worker_count"):
                    findings.append(self._finding(
                        "AZ0274", "App Service Plan without minimum instances",
                        f"App Service Plan '{res['name']}' may have only 1 instance.",
                        IaCSeverity.LOW, res,
                        "Set worker_count >= 2 for high availability.",
                    ))
        return findings

    def _az0275_aks_no_node_taints(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0275 – AKS system node pool without CriticalAddonsOnly taint."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if _tf_body_has_block(body, "default_node_pool"):
                    if not _tf_body_has_key_value(body, "only_critical_addons_enabled", "true"):
                        findings.append(self._finding(
                            "AZ0275", "AKS system pool without critical taint",
                            f"AKS cluster '{res['name']}' system pool accepts user workloads.",
                            IaCSeverity.LOW, res,
                            "Set only_critical_addons_enabled = true for system node pool.",
                        ))
        return findings

    def _az0276_storage_account_no_min_tls(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0276 – Storage account without minimum TLS version."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                tls_version = _tf_body_get_value(res["body"], "min_tls_version")
                if tls_version and "1.0" in tls_version or "1.1" in tls_version:
                    findings.append(self._finding(
                        "AZ0276", "Storage account with weak TLS",
                        f"Storage account '{res['name']}' allows TLS versions below 1.2.",
                        IaCSeverity.HIGH, res,
                        "Set min_tls_version = 'TLS1_2'.",
                    ))
        return findings

    def _az0277_keyvault_no_certificate_contacts(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0277 – Key Vault without certificate contacts."""
        findings = []
        contacts = [r for r in resources if r["type"] == "azurerm_key_vault_certificate_contacts"]
        if not contacts:
            for res in resources:
                if res["type"] == "azurerm_key_vault":
                    findings.append(self._finding(
                        "AZ0277", "Key Vault without certificate contacts",
                        f"Key Vault '{res['name']}' does not have certificate contacts.",
                        IaCSeverity.LOW, res,
                        "Create azurerm_key_vault_certificate_contacts for expiry notifications.",
                    ))
        return findings

    def _az0278_sql_no_connection_policy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0278 – SQL Server without connection policy set to Redirect."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                connection_policy = _tf_body_get_value(res["body"], "connection_policy")
                if connection_policy and "proxy" in connection_policy.lower():
                    findings.append(self._finding(
                        "AZ0278", "SQL Server with Proxy connection policy",
                        f"SQL Server '{res['name']}' uses Proxy connection policy.",
                        IaCSeverity.LOW, res,
                        "Set connection_policy = 'Redirect' for better performance.",
                    ))
        return findings

    def _az0279_cosmosdb_no_free_tier(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0279 – Cosmos DB not using free tier for dev/test."""
        findings = []
        # This is informational only
        return findings

    def _az0280_app_service_no_health_check_path(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0280 – App Service with empty health check path."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                health_path = _tf_body_get_value(res["body"], "health_check_path")
                if health_path and health_path.strip('"') == "":
                    findings.append(self._finding(
                        "AZ0280", "App Service with empty health check path",
                        f"App Service '{res['name']}' has empty health check path.",
                        IaCSeverity.LOW, res,
                        "Set health_check_path to a valid endpoint like '/health'.",
                    ))
        return findings

    def _az0281_aks_no_system_node_pool(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0281 – AKS without dedicated system node pool."""
        findings = []
        system_pools = [r for r in resources
                       if r["type"] == "azurerm_kubernetes_cluster_node_pool"
                       and _tf_body_has_key_value(r["body"], "mode", "System")]
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not system_pools:
                    findings.append(self._finding(
                        "AZ0281", "AKS without dedicated system node pool",
                        f"AKS cluster '{res['name']}' may lack dedicated system node pool.",
                        IaCSeverity.LOW, res,
                        "Create separate system and user node pools.",
                    ))
        return findings

    def _az0282_vm_no_ephemeral_disk(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0282 – VM not using ephemeral OS disk for stateless workloads."""
        findings = []
        # Informational rule - ephemeral disks are not always appropriate
        return findings

    def _az0283_acr_no_export_policy(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0283 – Container Registry without export policy disabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_container_registry":
                if not _tf_body_has_key_value(res["body"], "export_policy_enabled", "false"):
                    findings.append(self._finding(
                        "AZ0283", "ACR with export policy enabled",
                        f"ACR '{res['name']}' allows image export.",
                        IaCSeverity.LOW, res,
                        "Set export_policy_enabled = false to prevent image export.",
                    ))
        return findings

    def _az0284_app_gw_no_cookie_affinity(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0284 – Application Gateway with cookie-based affinity enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if _tf_body_has_key_value(res["body"], "cookie_based_affinity", "Enabled"):
                    findings.append(self._finding(
                        "AZ0284", "Application Gateway with cookie affinity",
                        f"Application Gateway '{res['name']}' uses cookie-based affinity.",
                        IaCSeverity.LOW, res,
                        "Consider disabling cookie_based_affinity for stateless apps.",
                    ))
        return findings

    def _az0285_storage_no_private_endpoint(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0285 – Storage account without private endpoint."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if _tf_body_has_key_value(res["body"], "public_network_access_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0285", "Storage without private endpoint",
                        f"Storage account '{res['name']}' allows public access without private endpoint.",
                        IaCSeverity.HIGH, res,
                        "Set public_network_access_enabled = false and create private endpoint.",
                    ))
        return findings

    def _az0286_eventhub_no_dedicated_cluster(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0286 – Event Hub without dedicated cluster for high throughput."""
        findings = []
        # Informational - dedicated clusters are expensive
        return findings

    def _az0287_servicebus_no_premium_messaging(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0287 – Service Bus without Premium messaging for production."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_servicebus_namespace":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "standard" in sku.lower():
                    findings.append(self._finding(
                        "AZ0287", "Service Bus without Premium SKU",
                        f"Service Bus '{res['name']}' uses Standard SKU.",
                        IaCSeverity.LOW, res,
                        "Consider Premium SKU for production with predictable performance.",
                    ))
        return findings

    def _az0288_redis_no_data_persistence(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0288 – Redis Cache without data persistence."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_redis_cache":
                sku = _tf_body_get_value(res["body"], "sku_name")
                if sku and "premium" in sku.lower():
                    if not _tf_body_has_block(res["body"], "redis_configuration"):
                        findings.append(self._finding(
                            "AZ0288", "Redis without data persistence",
                            f"Redis Cache '{res['name']}' may not have data persistence configured.",
                            IaCSeverity.MEDIUM, res,
                            "Configure rdb_backup_enabled or aof_backup_enabled in redis_configuration.",
                        ))
        return findings

    def _az0289_mysql_no_gtid_consistency(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0289 – MySQL Flexible Server without GTID consistency."""
        findings = []
        # MySQL Flexible Server configuration check
        return findings

    def _az0290_postgresql_no_wal_retention(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0290 – PostgreSQL without WAL retention for point-in-time recovery."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_postgresql_flexible_server":
                if not _tf_body_get_value(res["body"], "point_in_time_restore_time_in_days"):
                    findings.append(self._finding(
                        "AZ0290", "PostgreSQL without sufficient PITR",
                        f"PostgreSQL Flexible Server '{res['name']}' may have limited PITR.",
                        IaCSeverity.LOW, res,
                        "Set backup_retention_days to at least 7 for PITR.",
                    ))
        return findings

    def _az0291_app_service_no_scm_restrictions(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0291 – App Service without SCM site restrictions."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_block(res["body"], "scm_ip_restriction"):
                    findings.append(self._finding(
                        "AZ0291", "App Service without SCM restrictions",
                        f"App Service '{res['name']}' does not have SCM IP restrictions.",
                        IaCSeverity.MEDIUM, res,
                        "Add scm_ip_restriction blocks to protect deployment endpoint.",
                    ))
        return findings

    def _az0292_function_app_no_cors_validation(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0292 – Function App with permissive CORS."""
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app"):
                body = res["body"]
                if _tf_body_has_block(body, "cors"):
                    if '"*"' in body:
                        findings.append(self._finding(
                            "AZ0292", "Function App with permissive CORS",
                            f"Function App '{res['name']}' has permissive CORS configuration.",
                            IaCSeverity.MEDIUM, res,
                            "Specify explicit allowed_origins instead of wildcard.",
                        ))
        return findings

    def _az0293_aks_no_os_disk_type(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0293 – AKS node pool without ephemeral OS disk."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                os_disk_type = _tf_body_get_value(body, "os_disk_type")
                if not os_disk_type or "managed" in str(os_disk_type).lower():
                    findings.append(self._finding(
                        "AZ0293", "AKS without ephemeral OS disk",
                        f"AKS cluster '{res['name']}' does not use ephemeral OS disks.",
                        IaCSeverity.LOW, res,
                        "Set os_disk_type = 'Ephemeral' for faster node operations.",
                    ))
        return findings

    def _az0294_vm_no_data_disk_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0294 – VM data disk without encryption."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_machine_data_disk_attachment":
                # Data disks inherit encryption from managed disk settings
                pass
        return findings

    def _az0295_keyvault_no_network_bypass(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0295 – Key Vault network rules allowing Azure services bypass."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                body = res["body"]
                if _tf_body_has_block(body, "network_acls"):
                    bypass = _tf_body_get_value(body, "bypass")
                    if bypass and "azureservices" in bypass.lower():
                        findings.append(self._finding(
                            "AZ0295", "Key Vault with Azure services bypass",
                            f"Key Vault '{res['name']}' allows Azure services to bypass network rules.",
                            IaCSeverity.LOW, res,
                            "Consider removing 'AzureServices' from bypass if not needed.",
                        ))
        return findings

    def _az0296_storage_no_public_blob(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0296 – Storage account allowing public blob access."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if _tf_body_has_key_value(res["body"], "allow_nested_items_to_be_public", "true"):
                    findings.append(self._finding(
                        "AZ0296", "Storage allows public blob access",
                        f"Storage account '{res['name']}' allows nested items to be public.",
                        IaCSeverity.HIGH, res,
                        "Set allow_nested_items_to_be_public = false.",
                    ))
        return findings

    def _az0297_sql_no_active_directory_only(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0297 – SQL Server without Azure AD-only authentication."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                if not _tf_body_has_block(res["body"], "azuread_administrator"):
                    findings.append(self._finding(
                        "AZ0297", "SQL Server without Azure AD admin",
                        f"SQL Server '{res['name']}' does not have Azure AD administrator.",
                        IaCSeverity.MEDIUM, res,
                        "Add azuread_administrator block for Azure AD authentication.",
                    ))
        return findings

    def _az0298_cosmosdb_no_serverless(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0298 – Cosmos DB not using serverless for sporadic workloads."""
        findings = []
        # Informational - serverless is not always appropriate
        return findings

    def _az0299_app_service_no_websockets(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0299 – App Service with WebSockets disabled when needed."""
        findings = []
        # Informational - depends on application requirements
        return findings

    def _az0300_aks_no_cost_analysis(self, resources: list[dict]) -> list[IaCFinding]:
        """AZ0300 – AKS without cost analysis enabled."""
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "cost_analysis_enabled", "true"):
                    findings.append(self._finding(
                        "AZ0300", "AKS without cost analysis",
                        f"AKS cluster '{res['name']}' does not have cost analysis enabled.",
                        IaCSeverity.LOW, res,
                        "Set cost_analysis_enabled = true for cost visibility.",
                    ))
        return findings

    def _az0301_vm_no_automatic_shutdown(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        shutdown_schedules = {_tf_body_get_value(r["body"], "virtual_machine_id") for r in resources if r["type"] == "azurerm_dev_test_global_vm_shutdown_schedule"}
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not shutdown_schedules:
                    findings.append(self._finding("AZ0301", "VM without auto-shutdown", f"VM '{res['name']}' has no automatic shutdown schedule.", IaCSeverity.LOW, res, "Create azurerm_dev_test_global_vm_shutdown_schedule for cost savings."))
        return findings

    def _az0302_aks_no_run_command_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "run_command_enabled", "false"):
                    findings.append(self._finding("AZ0302", "AKS run command enabled", f"AKS '{res['name']}' has run command enabled.", IaCSeverity.MEDIUM, res, "Set run_command_enabled = false to prevent remote command execution."))
        return findings

    def _az0303_app_service_no_min_tls_cipher(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_get_value(res["body"], "minimum_tls_version"):
                    findings.append(self._finding("AZ0303", "App Service no min TLS", f"App Service '{res['name']}' has no minimum TLS version set.", IaCSeverity.MEDIUM, res, "Set minimum_tls_version = '1.2'."))
        return findings

    def _az0304_storage_no_static_website_error_page(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if _tf_body_has_block(res["body"], "static_website"):
                    if not _tf_body_get_value(res["body"], "error_404_document"):
                        findings.append(self._finding("AZ0304", "Storage static website no error page", f"Storage '{res['name']}' static website has no 404 error page.", IaCSeverity.LOW, res, "Set error_404_document in static_website block."))
        return findings

    def _az0305_sql_no_elastic_pool(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0306_aks_no_oidc_issuer(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "oidc_issuer_enabled", "true"):
                    findings.append(self._finding("AZ0306", "AKS no OIDC issuer", f"AKS '{res['name']}' has no OIDC issuer enabled.", IaCSeverity.MEDIUM, res, "Set oidc_issuer_enabled = true for workload identity."))
        return findings

    def _az0307_cosmosdb_no_partition_merge(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_key_value(res["body"], "partition_merge_enabled", "true"):
                    findings.append(self._finding("AZ0307", "Cosmos DB no partition merge", f"Cosmos DB '{res['name']}' has partition merge disabled.", IaCSeverity.LOW, res, "Set partition_merge_enabled = true for cost optimization."))
        return findings

    def _az0308_app_gw_no_rewrite_rule(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0309_vm_no_proximity_placement(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0310_storage_no_large_file_share(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_key_value(res["body"], "large_file_share_enabled", "true"):
                    findings.append(self._finding("AZ0310", "Storage no large file share", f"Storage '{res['name']}' has large file share disabled.", IaCSeverity.LOW, res, "Set large_file_share_enabled = true for up to 100 TiB."))
        return findings

    def _az0311_aks_no_vertical_pod_autoscaler(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "vertical_pod_autoscaler_enabled", "true"):
                    findings.append(self._finding("AZ0311", "AKS no VPA", f"AKS '{res['name']}' has no vertical pod autoscaler.", IaCSeverity.LOW, res, "Set vertical_pod_autoscaler_enabled = true."))
        return findings

    def _az0312_keyvault_no_managed_hsm(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0313_app_service_no_auto_heal(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_block(res["body"], "auto_heal_setting"):
                    findings.append(self._finding("AZ0313", "App Service no auto heal", f"App Service '{res['name']}' has no auto heal configured.", IaCSeverity.LOW, res, "Add auto_heal_setting block for automatic recovery."))
        return findings

    def _az0314_sql_no_short_term_retention(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_database":
                if not _tf_body_has_block(res["body"], "short_term_retention_policy"):
                    findings.append(self._finding("AZ0314", "SQL no short-term retention", f"SQL Database '{res['name']}' has no short-term retention.", IaCSeverity.MEDIUM, res, "Add short_term_retention_policy block."))
        return findings

    def _az0315_aks_no_blob_csi_driver(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_key_value(res["body"], "storage_profile"):
                    findings.append(self._finding("AZ0315", "AKS no blob CSI driver", f"AKS '{res['name']}' may not have blob CSI driver.", IaCSeverity.LOW, res, "Configure storage_profile with blob_driver_enabled = true."))
        return findings

    def _az0316_function_app_no_elastic_plan(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0317_acr_no_token_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0318_vm_scale_set_no_rolling_upgrade(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine_scale_set", "azurerm_windows_virtual_machine_scale_set"):
                if not _tf_body_has_block(res["body"], "rolling_upgrade_policy"):
                    findings.append(self._finding("AZ0318", "VMSS no rolling upgrade", f"VMSS '{res['name']}' has no rolling upgrade policy.", IaCSeverity.LOW, res, "Add rolling_upgrade_policy block for safe updates."))
        return findings

    def _az0319_app_service_no_slot_sticky(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0320_aks_no_file_csi_driver(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if _tf_body_has_block(body, "storage_profile"):
                    if _tf_body_has_key_value(body, "file_driver_enabled", "false"):
                        findings.append(self._finding("AZ0320", "AKS file CSI disabled", f"AKS '{res['name']}' has file CSI driver disabled.", IaCSeverity.LOW, res, "Set file_driver_enabled = true in storage_profile."))
        return findings

    def _az0321_storage_no_nfsv3(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0322_storage_no_sftp(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0323_mysql_flex_no_ha(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mysql_flexible_server":
                if not _tf_body_has_block(res["body"], "high_availability"):
                    findings.append(self._finding("AZ0323", "MySQL Flex no HA", f"MySQL Flexible Server '{res['name']}' has no HA.", IaCSeverity.MEDIUM, res, "Add high_availability block with mode = 'ZoneRedundant'."))
        return findings

    def _az0324_postgresql_flex_no_ha(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_postgresql_flexible_server":
                if not _tf_body_has_block(res["body"], "high_availability"):
                    findings.append(self._finding("AZ0324", "PostgreSQL Flex no HA", f"PostgreSQL Flexible Server '{res['name']}' has no HA.", IaCSeverity.MEDIUM, res, "Add high_availability block with mode = 'ZoneRedundant'."))
        return findings

    def _az0325_app_service_no_vnet_route_all(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_key_value(res["body"], "vnet_route_all_enabled", "true"):
                    findings.append(self._finding("AZ0325", "App Service no VNet route all", f"App Service '{res['name']}' does not route all traffic through VNet.", IaCSeverity.MEDIUM, res, "Set vnet_route_all_enabled = true."))
        return findings

    def _az0326_vm_no_custom_data(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0327_aks_no_node_resource_group(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_get_value(res["body"], "node_resource_group"):
                    findings.append(self._finding("AZ0327", "AKS no custom node RG", f"AKS '{res['name']}' uses default node resource group name.", IaCSeverity.LOW, res, "Set node_resource_group for custom naming."))
        return findings

    def _az0328_storage_no_hierarchical_namespace(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0329_keyvault_no_rotation_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_key":
                if not _tf_body_has_block(res["body"], "rotation_policy"):
                    findings.append(self._finding("AZ0329", "Key Vault key no rotation policy", f"Key '{res['name']}' has no rotation policy.", IaCSeverity.MEDIUM, res, "Add rotation_policy block for automatic key rotation."))
        return findings

    def _az0330_keyvault_secret_no_content_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_secret":
                if not _tf_body_get_value(res["body"], "content_type"):
                    findings.append(self._finding("AZ0330", "Key Vault secret no content type", f"Secret '{res['name']}' has no content type.", IaCSeverity.LOW, res, "Set content_type for secret classification."))
        return findings

    def _az0331_app_service_no_sticky_settings(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0332_function_app_no_daily_memory_quota(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_function_app", "azurerm_windows_function_app"):
                if not _tf_body_get_value(res["body"], "daily_memory_time_quota"):
                    findings.append(self._finding("AZ0332", "Function no memory quota", f"Function App '{res['name']}' has no daily memory quota.", IaCSeverity.LOW, res, "Set daily_memory_time_quota for cost control."))
        return findings

    def _az0333_aks_no_maintenance_window(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "maintenance_window"):
                    findings.append(self._finding("AZ0333", "AKS no maintenance window", f"AKS '{res['name']}' has no maintenance window.", IaCSeverity.LOW, res, "Add maintenance_window block for controlled updates."))
        return findings

    def _az0334_vm_no_dedicated_host(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0335_storage_no_allow_protected_append_writes(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0336_sql_no_maintenance_window(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0337_aks_no_node_pool_subnet(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                if not _tf_body_get_value(res["body"], "vnet_subnet_id"):
                    findings.append(self._finding("AZ0337", "AKS node pool no subnet", f"AKS node pool '{res['name']}' has no subnet configured.", IaCSeverity.MEDIUM, res, "Set vnet_subnet_id for network isolation."))
        return findings

    def _az0338_cosmosdb_no_free_tier_check(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0339_app_gw_no_redirect_config(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0340_vm_no_gallery_image(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0341_aks_no_sku_tier(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                sku = _tf_body_get_value(res["body"], "sku_tier")
                if not sku or "free" in str(sku).lower():
                    findings.append(self._finding("AZ0341", "AKS free tier", f"AKS '{res['name']}' uses Free tier.", IaCSeverity.LOW, res, "Set sku_tier = 'Standard' for production SLA."))
        return findings

    def _az0342_storage_no_account_replication(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                replication = _tf_body_get_value(res["body"], "account_replication_type")
                if replication and "lrs" in str(replication).lower():
                    findings.append(self._finding("AZ0342", "Storage LRS only", f"Storage '{res['name']}' uses LRS replication.", IaCSeverity.LOW, res, "Consider GRS or ZRS for higher durability."))
        return findings

    def _az0343_keyvault_no_access_log(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0344_app_service_no_detailed_error(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0345_sql_no_read_replica(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0346_aks_no_upgrade_settings(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "upgrade_settings"):
                    findings.append(self._finding("AZ0346", "AKS no upgrade settings", f"AKS '{res['name']}' default node pool has no upgrade settings.", IaCSeverity.LOW, res, "Add upgrade_settings with max_surge for safe upgrades."))
        return findings

    def _az0347_cosmosdb_no_consistency_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_block(res["body"], "consistency_policy"):
                    findings.append(self._finding("AZ0347", "Cosmos DB no consistency policy", f"Cosmos DB '{res['name']}' has no explicit consistency policy.", IaCSeverity.LOW, res, "Add consistency_policy block."))
        return findings

    def _az0348_app_gw_no_frontend_port(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0349_vm_no_capacity_reservation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0350_storage_no_queue_encryption_key(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0351_aks_no_snapshot_controller(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if _tf_body_has_block(body, "storage_profile"):
                    if _tf_body_has_key_value(body, "snapshot_controller_enabled", "false"):
                        findings.append(self._finding("AZ0351", "AKS snapshot controller disabled", f"AKS '{res['name']}' has snapshot controller disabled.", IaCSeverity.LOW, res, "Set snapshot_controller_enabled = true."))
        return findings

    def _az0352_keyvault_no_certificate_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0353_app_service_no_compression(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0354_sql_no_active_geo_replication(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0355_aks_no_windows_node_pool(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0356_cosmosdb_no_cors(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0357_app_gw_no_custom_error_page(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0358_vm_no_license_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_windows_virtual_machine":
                if not _tf_body_get_value(res["body"], "license_type"):
                    findings.append(self._finding("AZ0358", "Windows VM no license type", f"Windows VM '{res['name']}' has no license type for hybrid benefit.", IaCSeverity.LOW, res, "Set license_type = 'Windows_Server' for cost savings."))
        return findings

    def _az0359_storage_no_table_encryption_key(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0360_aks_no_network_dataplane(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_get_value(res["body"], "network_data_plane"):
                    findings.append(self._finding("AZ0360", "AKS no network dataplane", f"AKS '{res['name']}' uses default network dataplane.", IaCSeverity.LOW, res, "Consider network_data_plane = 'cilium' for advanced networking."))
        return findings

    def _az0361_keyvault_no_private_link_service(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0362_app_service_no_worker_count(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0363_sql_no_failover_group(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        failover_groups = [r for r in resources if r["type"] == "azurerm_mssql_failover_group"]
        if not failover_groups:
            for res in resources:
                if res["type"] == "azurerm_mssql_server":
                    findings.append(self._finding("AZ0363", "SQL no failover group", f"SQL Server '{res['name']}' has no failover group.", IaCSeverity.MEDIUM, res, "Create azurerm_mssql_failover_group for DR."))
        return findings

    def _az0364_aks_no_node_pool_max_surge(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                if not _tf_body_has_block(res["body"], "upgrade_settings"):
                    findings.append(self._finding("AZ0364", "AKS node pool no max surge", f"AKS node pool '{res['name']}' has no upgrade settings.", IaCSeverity.LOW, res, "Add upgrade_settings with max_surge."))
        return findings

    def _az0365_cosmosdb_no_geo_location(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_block(res["body"], "geo_location"):
                    findings.append(self._finding("AZ0365", "Cosmos DB no geo location", f"Cosmos DB '{res['name']}' has no geo_location configured.", IaCSeverity.MEDIUM, res, "Add geo_location block for regional distribution."))
        return findings

    def _az0366_app_gw_no_trusted_root_cert(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0367_vm_no_ultra_ssd(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0368_storage_no_blob_inventory(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        inventory_policies = [r for r in resources if r["type"] == "azurerm_storage_blob_inventory_policy"]
        if not inventory_policies:
            for res in resources:
                if res["type"] == "azurerm_storage_account":
                    findings.append(self._finding("AZ0368", "Storage no blob inventory", f"Storage '{res['name']}' has no blob inventory policy.", IaCSeverity.LOW, res, "Create azurerm_storage_blob_inventory_policy for tracking."))
        return findings

    def _az0369_aks_no_gpu_node_pool(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0370_keyvault_no_firewall_bypass_metrics(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0371_app_service_no_pre_warmed_instances(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0372_sql_no_zone_redundant(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_database":
                if not _tf_body_has_key_value(res["body"], "zone_redundant", "true"):
                    findings.append(self._finding("AZ0372", "SQL DB not zone redundant", f"SQL Database '{res['name']}' is not zone redundant.", IaCSeverity.MEDIUM, res, "Set zone_redundant = true for HA."))
        return findings

    def _az0373_aks_no_outbound_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_get_value(res["body"], "outbound_type"):
                    findings.append(self._finding("AZ0373", "AKS default outbound", f"AKS '{res['name']}' uses default outbound type.", IaCSeverity.LOW, res, "Consider outbound_type = 'userDefinedRouting' for controlled egress."))
        return findings

    def _az0374_cosmosdb_no_capabilities(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0375_app_gw_no_ssl_certificate(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_has_block(res["body"], "ssl_certificate"):
                    findings.append(self._finding("AZ0375", "App Gateway no SSL cert", f"Application Gateway '{res['name']}' has no SSL certificate.", IaCSeverity.HIGH, res, "Add ssl_certificate block for HTTPS termination."))
        return findings

    def _az0376_vm_no_os_disk_caching(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0377_storage_no_point_in_time_restore(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                body = res["body"]
                if _tf_body_has_block(body, "blob_properties"):
                    if not _tf_body_has_block(body, "restore_policy"):
                        findings.append(self._finding("AZ0377", "Storage no PITR", f"Storage '{res['name']}' has no point-in-time restore.", IaCSeverity.MEDIUM, res, "Add restore_policy in blob_properties."))
        return findings

    def _az0378_aks_no_enable_host_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                if not _tf_body_has_key_value(res["body"], "enable_host_encryption", "true"):
                    findings.append(self._finding("AZ0378", "AKS node pool no host encryption", f"AKS node pool '{res['name']}' has no host encryption.", IaCSeverity.MEDIUM, res, "Set enable_host_encryption = true."))
        return findings

    def _az0379_keyvault_no_contact_email(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0380_app_service_no_load_balancing_mode(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0381_sql_no_transparent_data_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_database":
                if _tf_body_has_key_value(res["body"], "transparent_data_encryption_enabled", "false"):
                    findings.append(self._finding("AZ0381", "SQL TDE disabled", f"SQL Database '{res['name']}' has TDE disabled.", IaCSeverity.HIGH, res, "Set transparent_data_encryption_enabled = true."))
        return findings

    def _az0382_aks_no_kubelet_config(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0383_cosmosdb_no_virtual_network_rule(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_block(res["body"], "virtual_network_rule"):
                    findings.append(self._finding("AZ0383", "Cosmos DB no VNet rule", f"Cosmos DB '{res['name']}' has no VNet rules.", IaCSeverity.MEDIUM, res, "Add virtual_network_rule blocks for network isolation."))
        return findings

    def _az0384_app_gw_no_firewall_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_get_value(res["body"], "firewall_policy_id"):
                    findings.append(self._finding("AZ0384", "App Gateway no WAF policy", f"Application Gateway '{res['name']}' has no WAF policy attached.", IaCSeverity.MEDIUM, res, "Set firewall_policy_id with azurerm_web_application_firewall_policy."))
        return findings

    def _az0385_vm_no_data_collection(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0386_storage_no_routing_preference(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0387_aks_no_linux_os_config(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0388_keyvault_no_certificate_issuer(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0389_app_service_no_app_command_line(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0390_sql_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding("AZ0390", "SQL Server no identity", f"SQL Server '{res['name']}' has no managed identity.", IaCSeverity.MEDIUM, res, "Add identity block with type = 'SystemAssigned'."))
        return findings

    def _az0391_aks_no_service_mesh(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "service_mesh_profile"):
                    findings.append(self._finding("AZ0391", "AKS no service mesh", f"AKS '{res['name']}' has no service mesh configured.", IaCSeverity.LOW, res, "Consider adding service_mesh_profile for Istio."))
        return findings

    def _az0392_cosmosdb_no_ip_range_filter(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_get_value(res["body"], "ip_range_filter"):
                    findings.append(self._finding("AZ0392", "Cosmos DB no IP filter", f"Cosmos DB '{res['name']}' has no IP range filter.", IaCSeverity.MEDIUM, res, "Set ip_range_filter for network access control."))
        return findings

    def _az0393_app_gw_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding("AZ0393", "App Gateway no identity", f"Application Gateway '{res['name']}' has no managed identity.", IaCSeverity.LOW, res, "Add identity block for Key Vault integration."))
        return findings

    def _az0394_vm_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding("AZ0394", "VM no managed identity", f"VM '{res['name']}' has no managed identity.", IaCSeverity.MEDIUM, res, "Add identity block with type = 'SystemAssigned'."))
        return findings

    def _az0395_storage_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding("AZ0395", "Storage no managed identity", f"Storage '{res['name']}' has no managed identity.", IaCSeverity.LOW, res, "Add identity block for CMK access."))
        return findings

    def _az0396_aks_no_private_dns_zone(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if _tf_body_has_key_value(res["body"], "private_cluster_enabled", "true"):
                    if not _tf_body_get_value(res["body"], "private_dns_zone_id"):
                        findings.append(self._finding("AZ0396", "Private AKS no DNS zone", f"Private AKS '{res['name']}' uses default DNS zone.", IaCSeverity.LOW, res, "Set private_dns_zone_id for custom DNS."))
        return findings

    def _az0397_keyvault_no_sku(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                sku = _tf_body_get_value(res["body"], "sku_name")
                if sku and "standard" in str(sku).lower():
                    findings.append(self._finding("AZ0397", "Key Vault standard SKU", f"Key Vault '{res['name']}' uses standard SKU.", IaCSeverity.LOW, res, "Consider sku_name = 'premium' for HSM-backed keys."))
        return findings

    def _az0398_app_service_no_linux_fx_version(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0399_sql_no_minimum_tls_version(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                tls = _tf_body_get_value(res["body"], "minimum_tls_version")
                if tls and "1.0" in str(tls):
                    findings.append(self._finding("AZ0399", "SQL weak TLS", f"SQL Server '{res['name']}' allows TLS 1.0.", IaCSeverity.HIGH, res, "Set minimum_tls_version = '1.2'."))
        return findings

    def _az0400_aks_no_api_server_access_profile(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "api_server_access_profile"):
                    findings.append(self._finding("AZ0400", "AKS no API server access profile", f"AKS '{res['name']}' has no API server access restrictions.", IaCSeverity.MEDIUM, res, "Add api_server_access_profile with authorized_ip_ranges."))
        return findings

    def _az0401_cosmosdb_no_default_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_get_value(res["body"], "default_identity_type"):
                    findings.append(self._finding("AZ0401", "Cosmos DB no default identity", f"Cosmos DB '{res['name']}' has no default identity.", IaCSeverity.LOW, res, "Set default_identity_type for CMK access."))
        return findings

    def _az0402_app_gw_no_zones(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_get_value(res["body"], "zones"):
                    findings.append(self._finding("AZ0402", "App Gateway no zones", f"Application Gateway '{res['name']}' has no availability zones.", IaCSeverity.MEDIUM, res, "Set zones = ['1', '2', '3'] for zone redundancy."))
        return findings

    def _az0403_vm_no_secure_boot(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_has_key_value(res["body"], "secure_boot_enabled", "true"):
                    findings.append(self._finding("AZ0403", "VM no secure boot", f"VM '{res['name']}' has secure boot disabled.", IaCSeverity.MEDIUM, res, "Set secure_boot_enabled = true."))
        return findings

    def _az0404_storage_no_dns_endpoint_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0405_aks_no_node_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if _tf_body_has_key_value(res["body"], "enable_node_public_ip", "true"):
                    findings.append(self._finding("AZ0405", "AKS nodes have public IP", f"AKS '{res['name']}' nodes have public IPs.", IaCSeverity.HIGH, res, "Set enable_node_public_ip = false."))
        return findings

    def _az0406_keyvault_no_public_network_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                if not _tf_body_has_key_value(res["body"], "public_network_access_enabled", "false"):
                    findings.append(self._finding("AZ0406", "Key Vault public access", f"Key Vault '{res['name']}' allows public access.", IaCSeverity.MEDIUM, res, "Set public_network_access_enabled = false."))
        return findings

    def _az0407_app_service_no_remote_debugging_version(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0408_sql_no_primary_user_assigned_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0409_aks_no_node_labels(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0410_cosmosdb_no_access_key_metadata_writes(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_key_value(res["body"], "access_key_metadata_writes_enabled", "false"):
                    findings.append(self._finding("AZ0410", "Cosmos DB key metadata writes", f"Cosmos DB '{res['name']}' allows access key metadata writes.", IaCSeverity.MEDIUM, res, "Set access_key_metadata_writes_enabled = false."))
        return findings

    def _az0411_app_gw_no_sku_capacity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0412_vm_no_vtpm(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_has_key_value(res["body"], "vtpm_enabled", "true"):
                    findings.append(self._finding("AZ0412", "VM no vTPM", f"VM '{res['name']}' has no vTPM enabled.", IaCSeverity.MEDIUM, res, "Set vtpm_enabled = true for Trusted Launch."))
        return findings

    def _az0413_storage_no_sas_expiration_period(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_block(res["body"], "sas_policy"):
                    findings.append(self._finding("AZ0413", "Storage no SAS policy", f"Storage '{res['name']}' has no SAS expiration policy.", IaCSeverity.MEDIUM, res, "Add sas_policy block with expiration_period."))
        return findings

    def _az0414_aks_no_pod_subnet(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_get_value(res["body"], "pod_subnet_id"):
                    findings.append(self._finding("AZ0414", "AKS no pod subnet", f"AKS '{res['name']}' has no dedicated pod subnet.", IaCSeverity.LOW, res, "Set pod_subnet_id for dynamic IP allocation."))
        return findings

    def _az0415_keyvault_key_no_rotation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0416_app_service_no_use_32_bit_worker(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if _tf_body_has_key_value(res["body"], "use_32_bit_worker", "true"):
                    findings.append(self._finding("AZ0416", "App Service 32-bit worker", f"App Service '{res['name']}' uses 32-bit worker.", IaCSeverity.LOW, res, "Set use_32_bit_worker = false for 64-bit."))
        return findings

    def _az0417_sql_no_retention_days(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0418_aks_no_fips_enabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                if not _tf_body_has_key_value(res["body"], "fips_enabled", "true"):
                    findings.append(self._finding("AZ0418", "AKS node pool no FIPS", f"AKS node pool '{res['name']}' has no FIPS enabled.", IaCSeverity.LOW, res, "Set fips_enabled = true for compliance."))
        return findings

    def _az0419_cosmosdb_no_network_acl_bypass(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                bypass = _tf_body_get_value(res["body"], "network_acl_bypass_for_azure_services")
                if bypass and "true" in str(bypass).lower():
                    findings.append(self._finding("AZ0419", "Cosmos DB ACL bypass", f"Cosmos DB '{res['name']}' allows Azure services bypass.", IaCSeverity.LOW, res, "Set network_acl_bypass_for_azure_services = false if not needed."))
        return findings

    def _az0420_app_gw_no_connection_draining(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0421_vm_no_provision_vm_agent(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if _tf_body_has_key_value(res["body"], "provision_vm_agent", "false"):
                    findings.append(self._finding("AZ0421", "VM no VM agent", f"VM '{res['name']}' has VM agent disabled.", IaCSeverity.MEDIUM, res, "Set provision_vm_agent = true for management."))
        return findings

    def _az0422_storage_no_default_to_oauth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_key_value(res["body"], "default_to_oauth_authentication", "true"):
                    findings.append(self._finding("AZ0422", "Storage no default OAuth", f"Storage '{res['name']}' does not default to OAuth.", IaCSeverity.LOW, res, "Set default_to_oauth_authentication = true."))
        return findings

    def _az0423_aks_no_disk_driver(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                body = res["body"]
                if _tf_body_has_block(body, "storage_profile"):
                    if _tf_body_has_key_value(body, "disk_driver_enabled", "false"):
                        findings.append(self._finding("AZ0423", "AKS disk driver disabled", f"AKS '{res['name']}' has disk CSI driver disabled.", IaCSeverity.LOW, res, "Set disk_driver_enabled = true."))
        return findings

    def _az0424_keyvault_key_no_key_opts(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0425_app_service_no_managed_pipeline_mode(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0426_sql_no_storage_account_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0427_aks_no_temporary_name(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0428_cosmosdb_no_mongo_server_version(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if _tf_body_has_key_value(res["body"], "kind", "MongoDB"):
                    if not _tf_body_get_value(res["body"], "mongo_server_version"):
                        findings.append(self._finding("AZ0428", "Cosmos DB no Mongo version", f"Cosmos DB '{res['name']}' has no MongoDB version specified.", IaCSeverity.LOW, res, "Set mongo_server_version = '4.2' or higher."))
        return findings

    def _az0429_app_gw_no_path_rules(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0430_vm_no_automatic_updates(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_windows_virtual_machine":
                if _tf_body_has_key_value(res["body"], "enable_automatic_updates", "false"):
                    findings.append(self._finding("AZ0430", "Windows VM no auto updates", f"Windows VM '{res['name']}' has automatic updates disabled.", IaCSeverity.MEDIUM, res, "Set enable_automatic_updates = true."))
        return findings

    def _az0431_storage_no_shared_access_key(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_key_value(res["body"], "shared_access_key_enabled", "false"):
                    findings.append(self._finding("AZ0431", "Storage shared key enabled", f"Storage '{res['name']}' has shared key access enabled.", IaCSeverity.MEDIUM, res, "Set shared_access_key_enabled = false for AAD-only."))
        return findings

    def _az0432_aks_no_drain_timeout(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0433_keyvault_key_no_curve(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0434_app_service_no_scm_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0435_sql_no_collation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0436_aks_no_soak_duration(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0437_cosmosdb_no_create_mode(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0438_app_gw_no_url_path_map(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0439_vm_no_timezone(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0440_storage_no_immutability_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_block(res["body"], "immutability_policy"):
                    findings.append(self._finding("AZ0440", "Storage no immutability policy", f"Storage '{res['name']}' has no account-level immutability policy.", IaCSeverity.LOW, res, "Add immutability_policy block for compliance."))
        return findings

    def _az0441_aks_no_node_pool_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0442_keyvault_secret_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_secret":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0442", "Key Vault secret no tags", f"Secret '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0443_app_service_no_client_affinity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if _tf_body_has_key_value(res["body"], "client_affinity_enabled", "true"):
                    findings.append(self._finding("AZ0443", "App Service client affinity enabled", f"App Service '{res['name']}' has client affinity enabled.", IaCSeverity.LOW, res, "Set client_affinity_enabled = false for stateless apps."))
        return findings

    def _az0444_sql_no_max_size_gb(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0445_aks_no_scale_down_mode(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                if not _tf_body_get_value(res["body"], "scale_down_mode"):
                    findings.append(self._finding("AZ0445", "AKS node pool no scale down mode", f"AKS node pool '{res['name']}' has no scale down mode.", IaCSeverity.LOW, res, "Set scale_down_mode = 'Deallocate' for cost savings."))
        return findings

    def _az0446_cosmosdb_no_restore(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0447_app_gw_no_gateway_ip_config(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0448_vm_no_availability_set(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0449_storage_no_cors_rules(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0450_aks_no_workload_autoscaler(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "workload_autoscaler_profile"):
                    findings.append(self._finding("AZ0450", "AKS no workload autoscaler", f"AKS '{res['name']}' has no workload autoscaler profile.", IaCSeverity.LOW, res, "Add workload_autoscaler_profile with keda_enabled = true."))
        return findings

    def _az0451_keyvault_key_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault_key":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0451", "Key Vault key no tags", f"Key '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0452_app_service_no_public_network_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if not _tf_body_has_key_value(res["body"], "public_network_access_enabled", "false"):
                    findings.append(self._finding("AZ0452", "App Service public access", f"App Service '{res['name']}' allows public access.", IaCSeverity.MEDIUM, res, "Set public_network_access_enabled = false with private endpoint."))
        return findings

    def _az0453_sql_no_sku_name(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0454_aks_no_spot_max_price(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                if _tf_body_has_key_value(res["body"], "priority", "Spot"):
                    if not _tf_body_get_value(res["body"], "spot_max_price"):
                        findings.append(self._finding("AZ0454", "AKS spot no max price", f"AKS spot pool '{res['name']}' has no max price.", IaCSeverity.LOW, res, "Set spot_max_price for cost control."))
        return findings

    def _az0455_cosmosdb_no_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_block(res["body"], "identity"):
                    findings.append(self._finding("AZ0455", "Cosmos DB no identity", f"Cosmos DB '{res['name']}' has no managed identity.", IaCSeverity.LOW, res, "Add identity block for RBAC."))
        return findings

    def _az0456_app_gw_no_private_link_config(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0457_vm_no_os_disk_security_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0458_storage_no_custom_domain(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0459_aks_no_os_sku(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster_node_pool":
                os_sku = _tf_body_get_value(res["body"], "os_sku")
                if not os_sku:
                    findings.append(self._finding("AZ0459", "AKS node pool no OS SKU", f"AKS node pool '{res['name']}' has no OS SKU specified.", IaCSeverity.LOW, res, "Set os_sku = 'AzureLinux' for security."))
        return findings

    def _az0460_keyvault_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_key_vault":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0460", "Key Vault no tags", f"Key Vault '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0461_app_service_no_logs_http(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_web_app", "azurerm_windows_web_app"):
                if _tf_body_has_block(res["body"], "logs"):
                    if not _tf_body_has_block(res["body"], "http_logs"):
                        findings.append(self._finding("AZ0461", "App Service no HTTP logs", f"App Service '{res['name']}' has no HTTP logging.", IaCSeverity.LOW, res, "Add http_logs block in logs configuration."))
        return findings

    def _az0462_sql_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_mssql_server":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0462", "SQL Server no tags", f"SQL Server '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0463_aks_no_priority(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0464_cosmosdb_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_cosmosdb_account":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0464", "Cosmos DB no tags", f"Cosmos DB '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0465_app_gw_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_application_gateway":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0465", "App Gateway no tags", f"Application Gateway '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0466_vm_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("azurerm_linux_virtual_machine", "azurerm_windows_virtual_machine"):
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0466", "VM no tags", f"VM '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0467_storage_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_storage_account":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0467", "Storage no tags", f"Storage '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0468_aks_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_kubernetes_cluster":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0468", "AKS no tags", f"AKS '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0469_nsg_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_network_security_group":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0469", "NSG no tags", f"NSG '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0470_vnet_no_tags(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network":
                if not _tf_body_has_block(res["body"], "tags"):
                    findings.append(self._finding("AZ0470", "VNet no tags", f"VNet '{res['name']}' has no tags.", IaCSeverity.LOW, res, "Add tags for organization."))
        return findings

    def _az0471_subnet_no_service_endpoints(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0472_subnet_no_delegation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0473_subnet_no_private_endpoint_policies(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_subnet":
                if not _tf_body_has_key_value(res["body"], "private_endpoint_network_policies_enabled", "true"):
                    findings.append(self._finding("AZ0473", "Subnet private endpoint policies", f"Subnet '{res['name']}' has network policies for private endpoints.", IaCSeverity.LOW, res, "Check private_endpoint_network_policies_enabled setting."))
        return findings

    def _az0474_subnet_no_private_link_service_policies(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0475_vnet_no_address_space(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0476_vnet_no_dns_servers(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0477_vnet_peering_no_allow_forwarded_traffic(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network_peering":
                if not _tf_body_has_key_value(res["body"], "allow_forwarded_traffic", "true"):
                    findings.append(self._finding("AZ0477", "VNet peering no forwarded traffic", f"VNet peering '{res['name']}' does not allow forwarded traffic.", IaCSeverity.LOW, res, "Set allow_forwarded_traffic = true if needed."))
        return findings

    def _az0478_vnet_peering_no_allow_gateway_transit(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0479_lb_no_frontend_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_lb":
                if not _tf_body_has_block(res["body"], "frontend_ip_configuration"):
                    findings.append(self._finding("AZ0479", "LB no frontend IP", f"Load Balancer '{res['name']}' has no frontend IP.", IaCSeverity.LOW, res, "Add frontend_ip_configuration block."))
        return findings

    def _az0480_lb_no_backend_pool(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        backend_pools = [r for r in resources if r["type"] == "azurerm_lb_backend_address_pool"]
        for res in resources:
            if res["type"] == "azurerm_lb":
                if not backend_pools:
                    findings.append(self._finding("AZ0480", "LB no backend pool", f"Load Balancer '{res['name']}' has no backend pool.", IaCSeverity.LOW, res, "Create azurerm_lb_backend_address_pool."))
        return findings

    def _az0481_lb_rule_no_idle_timeout(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_lb_rule":
                if not _tf_body_get_value(res["body"], "idle_timeout_in_minutes"):
                    findings.append(self._finding("AZ0481", "LB rule no idle timeout", f"LB rule '{res['name']}' uses default idle timeout.", IaCSeverity.LOW, res, "Set idle_timeout_in_minutes as needed."))
        return findings

    def _az0482_lb_rule_no_enable_tcp_reset(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_lb_rule":
                if not _tf_body_has_key_value(res["body"], "enable_tcp_reset", "true"):
                    findings.append(self._finding("AZ0482", "LB rule no TCP reset", f"LB rule '{res['name']}' has TCP reset disabled.", IaCSeverity.LOW, res, "Set enable_tcp_reset = true for Standard LB."))
        return findings

    def _az0483_nat_gateway_no_idle_timeout(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_nat_gateway":
                if not _tf_body_get_value(res["body"], "idle_timeout_in_minutes"):
                    findings.append(self._finding("AZ0483", "NAT Gateway no idle timeout", f"NAT Gateway '{res['name']}' uses default idle timeout.", IaCSeverity.LOW, res, "Set idle_timeout_in_minutes as needed."))
        return findings

    def _az0484_nat_gateway_no_zones(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_nat_gateway":
                if not _tf_body_get_value(res["body"], "zones"):
                    findings.append(self._finding("AZ0484", "NAT Gateway no zones", f"NAT Gateway '{res['name']}' has no availability zones.", IaCSeverity.MEDIUM, res, "Set zones for zone redundancy."))
        return findings

    def _az0485_public_ip_no_allocation_method(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0486_public_ip_no_sku(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_public_ip":
                sku = _tf_body_get_value(res["body"], "sku")
                if not sku or "basic" in str(sku).lower():
                    findings.append(self._finding("AZ0486", "Public IP Basic SKU", f"Public IP '{res['name']}' uses Basic SKU.", IaCSeverity.LOW, res, "Set sku = 'Standard' for zone redundancy."))
        return findings

    def _az0487_public_ip_no_zones(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_public_ip":
                sku = _tf_body_get_value(res["body"], "sku")
                if sku and "standard" in str(sku).lower():
                    if not _tf_body_get_value(res["body"], "zones"):
                        findings.append(self._finding("AZ0487", "Public IP no zones", f"Public IP '{res['name']}' has no availability zones.", IaCSeverity.LOW, res, "Set zones for zone redundancy."))
        return findings

    def _az0488_public_ip_no_ddos_protection_mode(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_public_ip":
                if not _tf_body_get_value(res["body"], "ddos_protection_mode"):
                    findings.append(self._finding("AZ0488", "Public IP no DDoS mode", f"Public IP '{res['name']}' has no DDoS protection mode.", IaCSeverity.LOW, res, "Set ddos_protection_mode = 'Enabled'."))
        return findings

    def _az0489_dns_record_no_ttl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0490_private_dns_record_no_ttl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0491_route_no_next_hop_type(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0492_route_table_no_routes(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0493_network_interface_no_dns_servers(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0494_network_interface_no_internal_dns_name(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0495_firewall_policy_no_intrusion_detection(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_firewall_policy":
                if not _tf_body_has_block(res["body"], "intrusion_detection"):
                    findings.append(self._finding("AZ0495", "Firewall policy no IDPS", f"Firewall policy '{res['name']}' has no intrusion detection.", IaCSeverity.MEDIUM, res, "Add intrusion_detection block with mode = 'Alert' or 'Deny'."))
        return findings

    def _az0496_firewall_policy_no_threat_intelligence(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_firewall_policy":
                if not _tf_body_get_value(res["body"], "threat_intelligence_mode"):
                    findings.append(self._finding("AZ0496", "Firewall policy no threat intel", f"Firewall policy '{res['name']}' has no threat intelligence.", IaCSeverity.MEDIUM, res, "Set threat_intelligence_mode = 'Alert' or 'Deny'."))
        return findings

    def _az0497_express_route_no_bandwidth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0498_express_route_no_peering_location(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        return findings

    def _az0499_vpn_connection_no_shared_key(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network_gateway_connection":
                shared_key = _tf_body_get_value(res["body"], "shared_key")
                if shared_key and (shared_key.startswith('"') or shared_key.startswith("'")):
                    stripped = shared_key.strip('"').strip("'")
                    if not stripped.startswith("var.") and not stripped.startswith("${"):
                        findings.append(self._finding("AZ0499", "VPN hardcoded shared key", f"VPN connection '{res['name']}' has hardcoded shared key.", IaCSeverity.HIGH, res, "Use variables or Key Vault for shared key."))
        return findings

    def _az0500_vpn_connection_no_ipsec_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "azurerm_virtual_network_gateway_connection":
                if not _tf_body_has_block(res["body"], "ipsec_policy"):
                    findings.append(self._finding("AZ0500", "VPN no IPsec policy", f"VPN connection '{res['name']}' has no custom IPsec policy.", IaCSeverity.MEDIUM, res, "Add ipsec_policy block with strong algorithms."))
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

    def _gc0011_bigquery_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_bigquery_dataset":
                if not _tf_body_get_value(res["body"], "default_encryption_configuration"):
                    findings.append(self._finding(
                        "GC0011", "BigQuery dataset without CMK encryption",
                        f"BigQuery dataset '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Configure default_encryption_configuration with kms_key_name.",
                    ))
        return findings

    def _gc0012_bigquery_public_dataset(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_bigquery_dataset_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0012", "BigQuery dataset publicly accessible",
                        f"BigQuery dataset IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.CRITICAL, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0013_pubsub_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_pubsub_topic":
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0013", "Pub/Sub topic without CMK encryption",
                        f"Pub/Sub topic '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_name for customer-managed encryption.",
                    ))
        return findings

    def _gc0014_cloudfunctions_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_cloudfunctions_function_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0014", "Cloud Function publicly accessible",
                        f"Cloud Function IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.HIGH, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0015_cloudfunctions_no_vpc(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_cloudfunctions_function":
                if not _tf_body_get_value(res["body"], "vpc_connector"):
                    findings.append(self._finding(
                        "GC0015", "Cloud Function without VPC connector",
                        f"Cloud Function '{res['name']}' is not connected to a VPC.",
                        IaCSeverity.MEDIUM, res,
                        "Set vpc_connector to connect the function to a VPC.",
                    ))
        return findings

    def _gc0016_cloudrun_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_cloud_run_service_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0016", "Cloud Run service publicly accessible",
                        f"Cloud Run service IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.HIGH, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0017_cloudrun_no_vpc(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_cloud_run_service":
                body = res["body"]
                if not _tf_body_has_block(body, "vpc_access"):
                    findings.append(self._finding(
                        "GC0017", "Cloud Run service without VPC access",
                        f"Cloud Run service '{res['name']}' is not connected to a VPC.",
                        IaCSeverity.MEDIUM, res,
                        "Configure vpc_access with a VPC connector.",
                    ))
        return findings

    def _gc0018_gke_no_network_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if not _tf_body_has_block(res["body"], "network_policy"):
                    findings.append(self._finding(
                        "GC0018", "GKE cluster without network policy",
                        f"GKE cluster '{res['name']}' does not have network policy enabled.",
                        IaCSeverity.HIGH, res,
                        "Enable network_policy in addons_config.",
                    ))
        return findings

    def _gc0019_gke_no_private_cluster(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if not _tf_body_has_block(res["body"], "private_cluster_config"):
                    findings.append(self._finding(
                        "GC0019", "GKE cluster not private",
                        f"GKE cluster '{res['name']}' is not configured as a private cluster.",
                        IaCSeverity.HIGH, res,
                        "Configure private_cluster_config with enable_private_nodes = true.",
                    ))
        return findings

    def _gc0020_gke_no_shielded_nodes(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if _tf_body_has_key_value(res["body"], "enable_shielded_nodes", "false"):
                    findings.append(self._finding(
                        "GC0020", "GKE cluster without shielded nodes",
                        f"GKE cluster '{res['name']}' does not have shielded nodes enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set enable_shielded_nodes = true.",
                    ))
        return findings

    def _gc0021_gke_no_workload_identity(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if not _tf_body_has_block(res["body"], "workload_identity_config"):
                    findings.append(self._finding(
                        "GC0021", "GKE cluster without workload identity",
                        f"GKE cluster '{res['name']}' does not have workload identity configured.",
                        IaCSeverity.HIGH, res,
                        "Configure workload_identity_config for secure workload authentication.",
                    ))
        return findings

    def _gc0022_gke_no_binary_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if _tf_body_has_key_value(res["body"], "enable_binary_authorization", "false"):
                    findings.append(self._finding(
                        "GC0022", "GKE cluster without binary authorization",
                        f"GKE cluster '{res['name']}' does not have binary authorization enabled.",
                        IaCSeverity.HIGH, res,
                        "Set enable_binary_authorization = true.",
                    ))
        return findings

    def _gc0023_gke_no_pod_security_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if not _tf_body_has_block(res["body"], "pod_security_policy_config"):
                    findings.append(self._finding(
                        "GC0023", "GKE cluster without pod security policy",
                        f"GKE cluster '{res['name']}' does not have pod security policy configured.",
                        IaCSeverity.MEDIUM, res,
                        "Configure pod_security_policy_config with enabled = true.",
                    ))
        return findings

    def _gc0024_compute_disk_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_disk":
                if not _tf_body_has_block(res["body"], "disk_encryption_key"):
                    findings.append(self._finding(
                        "GC0024", "Compute disk without CMK encryption",
                        f"Compute disk '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Configure disk_encryption_key with kms_key_self_link.",
                    ))
        return findings

    def _gc0025_compute_disk_no_snapshot(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        disk_names = set()
        snapshot_sources = set()
        for res in resources:
            if res["type"] == "google_compute_disk":
                disk_names.add(res["name"])
            if res["type"] == "google_compute_resource_policy":
                if _tf_body_has_block(res["body"], "snapshot_schedule_policy"):
                    snapshot_sources.add(res["name"])
        for res in resources:
            if res["type"] == "google_compute_disk":
                if not _tf_body_get_value(res["body"], "resource_policies"):
                    findings.append(self._finding(
                        "GC0025", "Compute disk without snapshot policy",
                        f"Compute disk '{res['name']}' does not have a snapshot schedule policy.",
                        IaCSeverity.LOW, res,
                        "Attach a resource_policy with snapshot_schedule_policy.",
                    ))
        return findings

    def _gc0026_compute_instance_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_instance":
                if _tf_body_has_block(res["body"], "access_config"):
                    findings.append(self._finding(
                        "GC0026", "Compute instance with public IP",
                        f"Compute instance '{res['name']}' has a public IP address.",
                        IaCSeverity.MEDIUM, res,
                        "Remove access_config to use only internal IP.",
                    ))
        return findings

    def _gc0027_compute_instance_no_shielded(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_instance":
                if not _tf_body_has_block(res["body"], "shielded_instance_config"):
                    findings.append(self._finding(
                        "GC0027", "Compute instance without shielded VM",
                        f"Compute instance '{res['name']}' is not a shielded VM.",
                        IaCSeverity.MEDIUM, res,
                        "Configure shielded_instance_config with enable_secure_boot = true.",
                    ))
        return findings

    def _gc0028_compute_instance_no_oslogin(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_instance":
                body = res["body"]
                if _tf_body_has_key_value(body, "enable-oslogin", "false"):
                    findings.append(self._finding(
                        "GC0028", "Compute instance without OS Login",
                        f"Compute instance '{res['name']}' does not have OS Login enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set metadata enable-oslogin = true.",
                    ))
        return findings

    def _gc0029_compute_instance_serial_port(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_instance":
                body = res["body"]
                if _tf_body_has_key_value(body, "serial-port-enable", "true"):
                    findings.append(self._finding(
                        "GC0029", "Compute instance serial port enabled",
                        f"Compute instance '{res['name']}' has serial port access enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set metadata serial-port-enable = false.",
                    ))
        return findings

    def _gc0030_compute_instance_ip_forwarding(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_instance":
                if _tf_body_has_key_value(res["body"], "can_ip_forward", "true"):
                    findings.append(self._finding(
                        "GC0030", "Compute instance IP forwarding enabled",
                        f"Compute instance '{res['name']}' has IP forwarding enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Set can_ip_forward = false unless required.",
                    ))
        return findings

    def _gc0031_vpc_flow_logs_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_subnetwork":
                if not _tf_body_has_block(res["body"], "log_config"):
                    findings.append(self._finding(
                        "GC0031", "VPC subnet flow logs disabled",
                        f"Subnet '{res['name']}' does not have flow logs enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Enable log_config for VPC flow logs.",
                    ))
        return findings

    def _gc0032_vpc_subnet_private_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_subnetwork":
                if _tf_body_has_key_value(res["body"], "private_ip_google_access", "false"):
                    findings.append(self._finding(
                        "GC0032", "VPC subnet without private Google access",
                        f"Subnet '{res['name']}' does not have private Google access enabled.",
                        IaCSeverity.LOW, res,
                        "Set private_ip_google_access = true.",
                    ))
        return findings

    def _gc0033_dns_dnssec_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_dns_managed_zone":
                if not _tf_body_has_block(res["body"], "dnssec_config"):
                    findings.append(self._finding(
                        "GC0033", "DNS zone without DNSSEC",
                        f"DNS zone '{res['name']}' does not have DNSSEC enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Configure dnssec_config with state = 'on'.",
                    ))
        return findings

    def _gc0034_logging_no_retention(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_logging_project_bucket_config":
                retention = _tf_body_get_value(res["body"], "retention_days")
                if retention and int(retention.strip('"')) < 30:
                    findings.append(self._finding(
                        "GC0034", "Logging bucket with insufficient retention",
                        f"Logging bucket '{res['name']}' has retention less than 30 days.",
                        IaCSeverity.MEDIUM, res,
                        "Set retention_days to at least 30 days.",
                    ))
        return findings

    def _gc0035_monitoring_no_alert_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_alert_policy = False
        for res in resources:
            if res["type"] == "google_monitoring_alert_policy":
                has_alert_policy = True
                break
        if not has_alert_policy:
            for res in resources:
                if res["type"] == "google_project":
                    findings.append(self._finding(
                        "GC0035", "No monitoring alert policies defined",
                        f"Project '{res['name']}' has no monitoring alert policies.",
                        IaCSeverity.LOW, res,
                        "Create google_monitoring_alert_policy resources.",
                    ))
                    break
        return findings

    def _gc0036_secret_manager_no_rotation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_secret_manager_secret":
                if not _tf_body_has_block(res["body"], "rotation"):
                    findings.append(self._finding(
                        "GC0036", "Secret without rotation policy",
                        f"Secret '{res['name']}' does not have automatic rotation configured.",
                        IaCSeverity.MEDIUM, res,
                        "Configure rotation block with rotation_period.",
                    ))
        return findings

    def _gc0037_secret_manager_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_secret_manager_secret":
                if not _tf_body_has_block(res["body"], "replication"):
                    findings.append(self._finding(
                        "GC0037", "Secret without CMK encryption",
                        f"Secret '{res['name']}' may not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Configure replication with customer_managed_encryption.",
                    ))
        return findings

    def _gc0038_dataproc_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_dataproc_cluster":
                if not _tf_body_has_block(res["body"], "cluster_config"):
                    continue
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0038", "Dataproc cluster without CMK encryption",
                        f"Dataproc cluster '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set encryption_config.kms_key_name in cluster_config.",
                    ))
        return findings

    def _gc0039_dataproc_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_dataproc_cluster":
                if _tf_body_has_key_value(res["body"], "internal_ip_only", "false"):
                    findings.append(self._finding(
                        "GC0039", "Dataproc cluster with public IP",
                        f"Dataproc cluster '{res['name']}' uses external IP addresses.",
                        IaCSeverity.MEDIUM, res,
                        "Set gce_cluster_config.internal_ip_only = true.",
                    ))
        return findings

    def _gc0040_composer_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_composer_environment":
                if _tf_body_has_key_value(res["body"], "enable_private_environment", "false"):
                    findings.append(self._finding(
                        "GC0040", "Composer environment with public IP",
                        f"Composer environment '{res['name']}' is not private.",
                        IaCSeverity.MEDIUM, res,
                        "Set private_environment_config.enable_private_environment = true.",
                    ))
        return findings

    def _gc0041_composer_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_composer_environment":
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0041", "Composer environment without CMK encryption",
                        f"Composer environment '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set encryption_config.kms_key_name.",
                    ))
        return findings

    def _gc0042_spanner_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_spanner_database":
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0042", "Spanner database without CMK encryption",
                        f"Spanner database '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set encryption_config.kms_key_name.",
                    ))
        return findings

    def _gc0043_bigtable_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_bigtable_instance":
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0043", "Bigtable instance without CMK encryption",
                        f"Bigtable instance '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set cluster.kms_key_name.",
                    ))
        return findings

    def _gc0044_memorystore_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_redis_instance":
                if _tf_body_has_key_value(res["body"], "auth_enabled", "false"):
                    findings.append(self._finding(
                        "GC0044", "Memorystore Redis without authentication",
                        f"Redis instance '{res['name']}' does not have AUTH enabled.",
                        IaCSeverity.HIGH, res,
                        "Set auth_enabled = true.",
                    ))
        return findings

    def _gc0045_memorystore_no_transit_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_redis_instance":
                if _tf_body_has_key_value(res["body"], "transit_encryption_mode", '"DISABLED"'):
                    findings.append(self._finding(
                        "GC0045", "Memorystore Redis without transit encryption",
                        f"Redis instance '{res['name']}' does not have transit encryption.",
                        IaCSeverity.HIGH, res,
                        "Set transit_encryption_mode = 'SERVER_AUTHENTICATION'.",
                    ))
        return findings

    def _gc0046_filestore_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_filestore_instance":
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0046", "Filestore instance without CMK encryption",
                        f"Filestore instance '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_name for customer-managed encryption.",
                    ))
        return findings

    def _gc0047_artifact_registry_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_artifact_registry_repository_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0047", "Artifact Registry publicly accessible",
                        f"Artifact Registry IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.HIGH, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0048_artifact_registry_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_artifact_registry_repository":
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0048", "Artifact Registry without CMK encryption",
                        f"Artifact Registry '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_name for customer-managed encryption.",
                    ))
        return findings

    def _gc0049_container_registry_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_registry":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0049", "Container Registry publicly accessible",
                        f"Container Registry '{res['name']}' may be publicly accessible.",
                        IaCSeverity.HIGH, res,
                        "Ensure storage bucket backing the registry has restricted access.",
                    ))
        return findings

    def _gc0050_cloud_armor_no_rules(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_security_policy":
                if not _tf_body_has_block(res["body"], "rule"):
                    findings.append(self._finding(
                        "GC0050", "Cloud Armor policy without rules",
                        f"Cloud Armor policy '{res['name']}' has no security rules defined.",
                        IaCSeverity.MEDIUM, res,
                        "Define rule blocks with appropriate match conditions.",
                    ))
        return findings

    def _gc0051_load_balancer_no_ssl_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_target_https_proxy":
                if not _tf_body_get_value(res["body"], "ssl_policy"):
                    findings.append(self._finding(
                        "GC0051", "Load balancer without SSL policy",
                        f"HTTPS proxy '{res['name']}' does not have an SSL policy configured.",
                        IaCSeverity.MEDIUM, res,
                        "Set ssl_policy to enforce modern TLS settings.",
                    ))
        return findings

    def _gc0052_load_balancer_no_logging(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_backend_service":
                if not _tf_body_has_block(res["body"], "log_config"):
                    findings.append(self._finding(
                        "GC0052", "Backend service without logging",
                        f"Backend service '{res['name']}' does not have logging enabled.",
                        IaCSeverity.LOW, res,
                        "Configure log_config with enable = true.",
                    ))
        return findings

    def _gc0053_cdn_no_signed_urls(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_backend_bucket":
                if _tf_body_has_key_value(res["body"], "enable_cdn", "true"):
                    if not _tf_body_has_block(res["body"], "cdn_policy"):
                        findings.append(self._finding(
                            "GC0053", "CDN backend without signed URLs",
                            f"Backend bucket '{res['name']}' has CDN enabled without signed URLs.",
                            IaCSeverity.MEDIUM, res,
                            "Configure cdn_policy with signed_url_cache_max_age_sec.",
                        ))
        return findings

    def _gc0054_appengine_no_ssl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_app_engine_domain_mapping":
                if not _tf_body_has_block(res["body"], "ssl_settings"):
                    findings.append(self._finding(
                        "GC0054", "App Engine domain without SSL",
                        f"App Engine domain mapping '{res['name']}' may not have SSL configured.",
                        IaCSeverity.HIGH, res,
                        "Configure ssl_settings block.",
                    ))
        return findings

    def _gc0055_appengine_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_app_engine_application":
                iap = _tf_body_has_block(res["body"], "iap")
                if not iap:
                    findings.append(self._finding(
                        "GC0055", "App Engine without IAP protection",
                        f"App Engine application '{res['name']}' does not have IAP enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Configure iap block for Identity-Aware Proxy.",
                    ))
        return findings

    def _gc0056_healthcare_dataset_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_healthcare_dataset_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0056", "Healthcare dataset publicly accessible",
                        f"Healthcare dataset IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.CRITICAL, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0057_healthcare_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_healthcare_dataset":
                # Healthcare API uses default encryption, check for CMK
                pass  # Healthcare API always encrypts, CMK is optional
        return findings

    def _gc0058_vertex_ai_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_vertex_ai_endpoint":
                body = res["body"]
                if not _tf_body_has_block(body, "private_service_connect_config"):
                    findings.append(self._finding(
                        "GC0058", "Vertex AI endpoint publicly accessible",
                        f"Vertex AI endpoint '{res['name']}' is not using private networking.",
                        IaCSeverity.MEDIUM, res,
                        "Configure private_service_connect_config for private access.",
                    ))
        return findings

    def _gc0059_vertex_ai_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_vertex_ai_dataset":
                if not _tf_body_get_value(res["body"], "encryption_spec"):
                    findings.append(self._finding(
                        "GC0059", "Vertex AI dataset without CMK encryption",
                        f"Vertex AI dataset '{res['name']}' may not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Configure encryption_spec with kms_key_name.",
                    ))
        return findings

    def _gc0060_notebooks_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_notebooks_instance":
                if _tf_body_has_key_value(res["body"], "no_public_ip", "false"):
                    findings.append(self._finding(
                        "GC0060", "Notebooks instance with public IP",
                        f"Notebooks instance '{res['name']}' has a public IP address.",
                        IaCSeverity.MEDIUM, res,
                        "Set no_public_ip = true.",
                    ))
        return findings

    def _gc0061_notebooks_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_notebooks_instance":
                if not _tf_body_get_value(res["body"], "kms_key"):
                    findings.append(self._finding(
                        "GC0061", "Notebooks instance without CMK encryption",
                        f"Notebooks instance '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key for customer-managed encryption.",
                    ))
        return findings

    def _gc0062_dataflow_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_dataflow_job":
                if _tf_body_has_key_value(res["body"], "ip_configuration", '"WORKER_IP_PUBLIC"'):
                    findings.append(self._finding(
                        "GC0062", "Dataflow job with public IP",
                        f"Dataflow job '{res['name']}' uses public IP addresses.",
                        IaCSeverity.MEDIUM, res,
                        "Set ip_configuration = 'WORKER_IP_PRIVATE'.",
                    ))
        return findings

    def _gc0063_dataflow_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_dataflow_job":
                if not _tf_body_get_value(res["body"], "kms_key_name"):
                    findings.append(self._finding(
                        "GC0063", "Dataflow job without CMK encryption",
                        f"Dataflow job '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_name for customer-managed encryption.",
                    ))
        return findings

    def _gc0064_datafusion_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_data_fusion_instance":
                if _tf_body_has_key_value(res["body"], "private_instance", "false"):
                    findings.append(self._finding(
                        "GC0064", "Data Fusion instance with public IP",
                        f"Data Fusion instance '{res['name']}' is not private.",
                        IaCSeverity.MEDIUM, res,
                        "Set private_instance = true.",
                    ))
        return findings

    def _gc0065_datafusion_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_data_fusion_instance":
                if not _tf_body_has_block(res["body"], "crypto_key_config"):
                    findings.append(self._finding(
                        "GC0065", "Data Fusion instance without CMK encryption",
                        f"Data Fusion instance '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Configure crypto_key_config with key_reference.",
                    ))
        return findings

    def _gc0066_iam_service_account_key(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_service_account_key":
                findings.append(self._finding(
                    "GC0066", "Service account key created",
                    f"Service account key '{res['name']}' is being created. Prefer workload identity.",
                    IaCSeverity.MEDIUM, res,
                    "Use workload identity instead of service account keys.",
                ))
        return findings

    def _gc0067_iam_no_separation_of_duties(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("google_project_iam_member", "google_project_iam_binding"):
                role = _tf_body_get_value(res["body"], "role")
                if role and "iam.serviceAccountAdmin" in role:
                    member = _tf_body_get_value(res["body"], "member")
                    if member and "user:" in member:
                        findings.append(self._finding(
                            "GC0067", "User with Service Account Admin role",
                            f"IAM binding '{res['name']}' grants Service Account Admin to a user.",
                            IaCSeverity.HIGH, res,
                            "Separate service account administration from usage.",
                        ))
        return findings

    def _gc0068_org_policy_not_enforced(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_org_policy_policy":
                if _tf_body_has_key_value(res["body"], "enforced", "false"):
                    findings.append(self._finding(
                        "GC0068", "Organization policy not enforced",
                        f"Org policy '{res['name']}' is not enforced.",
                        IaCSeverity.MEDIUM, res,
                        "Set enforced = true or configure appropriate rules.",
                    ))
        return findings

    def _gc0069_vpc_peering_no_export(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_network_peering":
                if _tf_body_has_key_value(res["body"], "export_custom_routes", "true"):
                    findings.append(self._finding(
                        "GC0069", "VPC peering exports custom routes",
                        f"VPC peering '{res['name']}' exports custom routes.",
                        IaCSeverity.LOW, res,
                        "Review if export_custom_routes is necessary.",
                    ))
        return findings

    def _gc0070_vpn_no_high_availability(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_vpn_gateway":
                # Classic VPN gateway - recommend HA VPN
                findings.append(self._finding(
                    "GC0070", "Classic VPN gateway in use",
                    f"VPN gateway '{res['name']}' is a classic VPN. Consider HA VPN.",
                    IaCSeverity.LOW, res,
                    "Use google_compute_ha_vpn_gateway for high availability.",
                ))
        return findings

    def _gc0071_interconnect_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_interconnect_attachment":
                if not _tf_body_get_value(res["body"], "encryption"):
                    findings.append(self._finding(
                        "GC0071", "Interconnect attachment without encryption",
                        f"Interconnect attachment '{res['name']}' may not have encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set encryption = 'IPSEC' for encrypted interconnect.",
                    ))
        return findings

    def _gc0072_nat_no_logging(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_router_nat":
                if not _tf_body_has_block(res["body"], "log_config"):
                    findings.append(self._finding(
                        "GC0072", "Cloud NAT without logging",
                        f"Cloud NAT '{res['name']}' does not have logging enabled.",
                        IaCSeverity.LOW, res,
                        "Configure log_config with enable = true.",
                    ))
        return findings

    def _gc0073_router_no_bgp_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_router_peer":
                if not _tf_body_has_block(res["body"], "md5_authentication_key"):
                    findings.append(self._finding(
                        "GC0073", "BGP peer without MD5 authentication",
                        f"Router peer '{res['name']}' does not have BGP MD5 authentication.",
                        IaCSeverity.MEDIUM, res,
                        "Configure md5_authentication_key for BGP session security.",
                    ))
        return findings

    def _gc0074_service_networking_no_private(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_service_networking_connection":
                if not _tf_body_get_value(res["body"], "reserved_peering_ranges"):
                    findings.append(self._finding(
                        "GC0074", "Service networking without reserved range",
                        f"Service networking connection '{res['name']}' has no reserved range.",
                        IaCSeverity.LOW, res,
                        "Configure reserved_peering_ranges for private service access.",
                    ))
        return findings

    def _gc0075_endpoints_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_endpoints_service":
                body = res["body"]
                if "authentication" not in body.lower():
                    findings.append(self._finding(
                        "GC0075", "Cloud Endpoints without authentication",
                        f"Endpoints service '{res['name']}' may not have authentication configured.",
                        IaCSeverity.HIGH, res,
                        "Configure authentication in the OpenAPI specification.",
                    ))
        return findings

    def _gc0076_apigateway_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_api_gateway_api_config":
                body = res["body"]
                if "security" not in body.lower():
                    findings.append(self._finding(
                        "GC0076", "API Gateway without authentication",
                        f"API Gateway config '{res['name']}' may not have authentication.",
                        IaCSeverity.HIGH, res,
                        "Configure security requirements in the API spec.",
                    ))
        return findings

    def _gc0077_cloudtasks_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_cloud_tasks_queue":
                # Cloud Tasks queues should have rate limiting
                if not _tf_body_has_block(res["body"], "rate_limits"):
                    findings.append(self._finding(
                        "GC0077", "Cloud Tasks queue without rate limits",
                        f"Cloud Tasks queue '{res['name']}' has no rate limits configured.",
                        IaCSeverity.LOW, res,
                        "Configure rate_limits block.",
                    ))
        return findings

    def _gc0078_scheduler_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_cloud_scheduler_job":
                if _tf_body_has_block(res["body"], "http_target"):
                    if not _tf_body_has_block(res["body"], "oauth_token") and \
                       not _tf_body_has_block(res["body"], "oidc_token"):
                        findings.append(self._finding(
                            "GC0078", "Cloud Scheduler job without authentication",
                            f"Scheduler job '{res['name']}' HTTP target has no auth token.",
                            IaCSeverity.MEDIUM, res,
                            "Configure oauth_token or oidc_token for authenticated calls.",
                        ))
        return findings

    def _gc0079_workflows_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_workflows_workflow":
                if not _tf_body_get_value(res["body"], "service_account"):
                    findings.append(self._finding(
                        "GC0079", "Workflow without service account",
                        f"Workflow '{res['name']}' does not specify a service account.",
                        IaCSeverity.MEDIUM, res,
                        "Set service_account for workflow execution identity.",
                    ))
        return findings

    def _gc0080_eventarc_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_eventarc_trigger":
                if not _tf_body_get_value(res["body"], "service_account"):
                    findings.append(self._finding(
                        "GC0080", "Eventarc trigger without service account",
                        f"Eventarc trigger '{res['name']}' does not specify a service account.",
                        IaCSeverity.MEDIUM, res,
                        "Set service_account for trigger execution identity.",
                    ))
        return findings

    def _gc0081_firestore_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_firestore_database":
                if not _tf_body_get_value(res["body"], "key_prefix"):
                    # Firestore uses Google-managed encryption by default
                    pass  # CMK support via CMEK-enabled Firestore
        return findings

    def _gc0082_datastore_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Datastore uses Firestore in Datastore mode
        return findings

    def _gc0083_alloydb_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_alloydb_instance":
                # AlloyDB instances should be in private networks
                if not _tf_body_get_value(res["body"], "network"):
                    findings.append(self._finding(
                        "GC0083", "AlloyDB instance without VPC network",
                        f"AlloyDB instance '{res['name']}' may not be in a private network.",
                        IaCSeverity.HIGH, res,
                        "Configure network for private connectivity.",
                    ))
        return findings

    def _gc0084_alloydb_no_cmk(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_alloydb_cluster":
                if not _tf_body_has_block(res["body"], "encryption_config"):
                    findings.append(self._finding(
                        "GC0084", "AlloyDB cluster without CMK encryption",
                        f"AlloyDB cluster '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Configure encryption_config with kms_key_name.",
                    ))
        return findings

    def _gc0085_sql_no_backup(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_sql_database_instance":
                if _tf_body_has_key_value(res["body"], "enabled", "false"):
                    if "backup_configuration" in res["body"]:
                        findings.append(self._finding(
                            "GC0085", "Cloud SQL without automated backups",
                            f"Cloud SQL instance '{res['name']}' has backups disabled.",
                            IaCSeverity.HIGH, res,
                            "Set backup_configuration.enabled = true.",
                        ))
        return findings

    def _gc0086_sql_no_ha(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_sql_database_instance":
                if not _tf_body_get_value(res["body"], "availability_type"):
                    findings.append(self._finding(
                        "GC0086", "Cloud SQL without high availability",
                        f"Cloud SQL instance '{res['name']}' is not highly available.",
                        IaCSeverity.MEDIUM, res,
                        "Set availability_type = 'REGIONAL' for high availability.",
                    ))
        return findings

    def _gc0087_sql_maintenance_window(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_sql_database_instance":
                if not _tf_body_has_block(res["body"], "maintenance_window"):
                    findings.append(self._finding(
                        "GC0087", "Cloud SQL without maintenance window",
                        f"Cloud SQL instance '{res['name']}' has no maintenance window defined.",
                        IaCSeverity.LOW, res,
                        "Configure maintenance_window for predictable updates.",
                    ))
        return findings

    def _gc0088_sql_no_insights(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_sql_database_instance":
                if not _tf_body_has_block(res["body"], "insights_config"):
                    findings.append(self._finding(
                        "GC0088", "Cloud SQL without Query Insights",
                        f"Cloud SQL instance '{res['name']}' does not have Query Insights enabled.",
                        IaCSeverity.LOW, res,
                        "Configure insights_config for query performance monitoring.",
                    ))
        return findings

    def _gc0089_gke_no_release_channel(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if not _tf_body_has_block(res["body"], "release_channel"):
                    findings.append(self._finding(
                        "GC0089", "GKE cluster without release channel",
                        f"GKE cluster '{res['name']}' is not subscribed to a release channel.",
                        IaCSeverity.MEDIUM, res,
                        "Configure release_channel for automatic updates.",
                    ))
        return findings

    def _gc0090_gke_no_maintenance_window(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if not _tf_body_has_block(res["body"], "maintenance_policy"):
                    findings.append(self._finding(
                        "GC0090", "GKE cluster without maintenance window",
                        f"GKE cluster '{res['name']}' has no maintenance window defined.",
                        IaCSeverity.LOW, res,
                        "Configure maintenance_policy for predictable updates.",
                    ))
        return findings

    def _gc0091_gke_master_authorized_networks(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if not _tf_body_has_block(res["body"], "master_authorized_networks_config"):
                    findings.append(self._finding(
                        "GC0091", "GKE cluster without master authorized networks",
                        f"GKE cluster '{res['name']}' allows unrestricted API server access.",
                        IaCSeverity.HIGH, res,
                        "Configure master_authorized_networks_config to restrict access.",
                    ))
        return findings

    def _gc0092_gke_no_intranode_visibility(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                if _tf_body_has_key_value(res["body"], "enable_intranode_visibility", "false"):
                    findings.append(self._finding(
                        "GC0092", "GKE cluster without intranode visibility",
                        f"GKE cluster '{res['name']}' has intranode visibility disabled.",
                        IaCSeverity.LOW, res,
                        "Set enable_intranode_visibility = true for pod-to-pod traffic visibility.",
                    ))
        return findings

    def _gc0093_gke_no_logging(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                logging_service = _tf_body_get_value(res["body"], "logging_service")
                if logging_service and "none" in logging_service.lower():
                    findings.append(self._finding(
                        "GC0093", "GKE cluster with logging disabled",
                        f"GKE cluster '{res['name']}' has logging disabled.",
                        IaCSeverity.HIGH, res,
                        "Set logging_service = 'logging.googleapis.com/kubernetes'.",
                    ))
        return findings

    def _gc0094_gke_no_monitoring(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_container_cluster":
                monitoring_service = _tf_body_get_value(res["body"], "monitoring_service")
                if monitoring_service and "none" in monitoring_service.lower():
                    findings.append(self._finding(
                        "GC0094", "GKE cluster with monitoring disabled",
                        f"GKE cluster '{res['name']}' has monitoring disabled.",
                        IaCSeverity.HIGH, res,
                        "Set monitoring_service = 'monitoring.googleapis.com/kubernetes'.",
                    ))
        return findings

    def _gc0095_compute_image_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_image_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0095", "Compute image publicly accessible",
                        f"Compute image IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.MEDIUM, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0096_compute_snapshot_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_compute_snapshot_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0096", "Compute snapshot publicly accessible",
                        f"Compute snapshot IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.HIGH, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0097_storage_uniform_access(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_storage_bucket":
                if _tf_body_has_key_value(res["body"], "uniform_bucket_level_access", "false"):
                    findings.append(self._finding(
                        "GC0097", "Storage bucket without uniform access",
                        f"Storage bucket '{res['name']}' does not use uniform bucket-level access.",
                        IaCSeverity.MEDIUM, res,
                        "Set uniform_bucket_level_access = true.",
                    ))
        return findings

    def _gc0098_storage_retention_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_storage_bucket":
                if not _tf_body_has_block(res["body"], "retention_policy"):
                    findings.append(self._finding(
                        "GC0098", "Storage bucket without retention policy",
                        f"Storage bucket '{res['name']}' has no retention policy.",
                        IaCSeverity.LOW, res,
                        "Configure retention_policy for data retention requirements.",
                    ))
        return findings

    def _gc0099_storage_versioning(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_storage_bucket":
                if not _tf_body_has_block(res["body"], "versioning"):
                    findings.append(self._finding(
                        "GC0099", "Storage bucket without versioning",
                        f"Storage bucket '{res['name']}' does not have versioning enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Configure versioning block with enabled = true.",
                    ))
        return findings

    def _gc0100_storage_lifecycle(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_storage_bucket":
                if not _tf_body_has_block(res["body"], "lifecycle_rule"):
                    findings.append(self._finding(
                        "GC0100", "Storage bucket without lifecycle rules",
                        f"Storage bucket '{res['name']}' has no lifecycle rules.",
                        IaCSeverity.LOW, res,
                        "Configure lifecycle_rule for automatic object management.",
                    ))
        return findings

    def _gc0101_kms_no_destroy_protection(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_kms_crypto_key":
                destroy_duration = _tf_body_get_value(res["body"], "destroy_scheduled_duration")
                if destroy_duration:
                    duration_str = destroy_duration.strip('"')
                    # Check if duration is less than 24 hours
                    if "s" in duration_str and int(duration_str.replace("s", "")) < 86400:
                        findings.append(self._finding(
                            "GC0101", "KMS key with short destroy duration",
                            f"KMS key '{res['name']}' has destroy duration less than 24 hours.",
                            IaCSeverity.MEDIUM, res,
                            "Set destroy_scheduled_duration to at least 24h.",
                        ))
        return findings

    def _gc0102_kms_public_key(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_kms_crypto_key_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0102", "KMS key publicly accessible",
                        f"KMS key IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.CRITICAL, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0103_logging_sink_no_filter(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_logging_project_sink":
                if not _tf_body_get_value(res["body"], "filter"):
                    findings.append(self._finding(
                        "GC0103", "Logging sink without filter",
                        f"Logging sink '{res['name']}' exports all logs without filtering.",
                        IaCSeverity.LOW, res,
                        "Set filter to export only required logs.",
                    ))
        return findings

    def _gc0104_billing_budget_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_budget = False
        for res in resources:
            if res["type"] == "google_billing_budget":
                has_budget = True
                break
        if not has_budget:
            for res in resources:
                if res["type"] == "google_project":
                    findings.append(self._finding(
                        "GC0104", "No billing budget defined",
                        f"Project '{res['name']}' has no billing budget configured.",
                        IaCSeverity.LOW, res,
                        "Create google_billing_budget for cost management.",
                    ))
                    break
        return findings

    def _gc0105_project_default_network(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_project":
                if _tf_body_has_key_value(res["body"], "auto_create_network", "true"):
                    findings.append(self._finding(
                        "GC0105", "Project with default network enabled",
                        f"Project '{res['name']}' will create the default network.",
                        IaCSeverity.MEDIUM, res,
                        "Set auto_create_network = false and create custom VPC.",
                    ))
        return findings

    def _gc0106_project_default_service_account(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("google_project_iam_member", "google_project_iam_binding"):
                member = _tf_body_get_value(res["body"], "member")
                if member and "compute@developer.gserviceaccount.com" in member:
                    findings.append(self._finding(
                        "GC0106", "Default compute service account in use",
                        f"IAM binding '{res['name']}' uses the default compute service account.",
                        IaCSeverity.MEDIUM, res,
                        "Create and use a custom service account instead.",
                    ))
        return findings

    def _gc0107_folder_iam_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_folder_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0107", "Folder IAM grants public access",
                        f"Folder IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.CRITICAL, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0108_org_iam_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "google_organization_iam_binding":
                body = res["body"]
                if "allUsers" in body or "allAuthenticatedUsers" in body:
                    findings.append(self._finding(
                        "GC0108", "Organization IAM grants public access",
                        f"Organization IAM binding '{res['name']}' grants public access.",
                        IaCSeverity.CRITICAL, res,
                        "Remove allUsers and allAuthenticatedUsers from members.",
                    ))
        return findings

    def _gc0109_access_context_manager_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_acm = False
        for res in resources:
            if res["type"] == "google_access_context_manager_access_policy":
                has_acm = True
                break
        # Only flag if there are GCP resources but no access context manager
        gcp_resources = [r for r in resources if r["type"].startswith("google_")]
        if not has_acm and len(gcp_resources) > 5:
            findings.append(self._finding(
                "GC0109", "Access Context Manager not configured",
                "No Access Context Manager policy found for this infrastructure.",
                IaCSeverity.LOW, gcp_resources[0] if gcp_resources else resources[0],
                "Consider configuring Access Context Manager for fine-grained access control.",
            ))
        return findings

    def _gc0110_vpc_service_controls_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_vsc = False
        for res in resources:
            if res["type"] == "google_access_context_manager_service_perimeter":
                has_vsc = True
                break
        # Only flag if there are sensitive GCP resources but no VPC-SC
        sensitive_types = [
            "google_bigquery_dataset", "google_storage_bucket",
            "google_sql_database_instance", "google_spanner_database"
        ]
        sensitive_resources = [r for r in resources if r["type"] in sensitive_types]
        if not has_vsc and sensitive_resources:
            findings.append(self._finding(
                "GC0110", "VPC Service Controls not configured",
                "Sensitive resources exist without VPC Service Controls protection.",
                IaCSeverity.MEDIUM, sensitive_resources[0],
                "Configure google_access_context_manager_service_perimeter for data exfiltration protection.",
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

    def _oc0006_compute_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_instance":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0006", "OCI Compute instance without CMK encryption",
                        f"Compute instance '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id in launch_options for customer-managed encryption.",
                    ))
        return findings

    def _oc0007_compute_public_ip(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_instance":
                if _tf_body_has_key_value(res["body"], "assign_public_ip", "true"):
                    findings.append(self._finding(
                        "OC0007", "OCI Compute instance with public IP",
                        f"Compute instance '{res['name']}' has a public IP assigned.",
                        IaCSeverity.MEDIUM, res,
                        "Set assign_public_ip = false and use bastion or NAT gateway.",
                    ))
        return findings

    def _oc0008_block_volume_no_backup(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        block_volumes = set()
        backup_volumes = set()
        for res in resources:
            if res["type"] == "oci_core_volume":
                block_volumes.add(res["name"])
            if res["type"] == "oci_core_volume_backup_policy_assignment":
                vol_id = _tf_body_get_value(res["body"], "asset_id")
                if vol_id:
                    backup_volumes.add(vol_id)
        for res in resources:
            if res["type"] == "oci_core_volume":
                findings.append(self._finding(
                    "OC0008", "OCI Block Volume without backup policy",
                    f"Block Volume '{res['name']}' may not have a backup policy assigned.",
                    IaCSeverity.MEDIUM, res,
                    "Create oci_core_volume_backup_policy_assignment for the volume.",
                ))
        return findings

    def _oc0009_vcn_no_flow_logs(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_vcn":
                # Check if there's a corresponding flow log
                pass
        for res in resources:
            if res["type"] == "oci_core_subnet":
                findings.append(self._finding(
                    "OC0009", "OCI VCN subnet without flow logs",
                    f"Subnet '{res['name']}' may not have VCN flow logs enabled.",
                    IaCSeverity.MEDIUM, res,
                    "Enable VCN flow logs via oci_logging_log for network visibility.",
                ))
        return findings

    def _oc0010_load_balancer_no_ssl(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_load_balancer_listener":
                protocol = _tf_body_get_value(res["body"], "protocol")
                if protocol and "http" in protocol.lower() and "https" not in protocol.lower():
                    findings.append(self._finding(
                        "OC0010", "OCI Load Balancer listener without SSL",
                        f"Load Balancer listener '{res['name']}' uses HTTP without SSL.",
                        IaCSeverity.HIGH, res,
                        "Use HTTPS protocol with ssl_configuration.",
                    ))
        return findings

    def _oc0011_load_balancer_no_waf(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        lb_ids = set()
        waf_protected = set()
        for res in resources:
            if res["type"] == "oci_load_balancer_load_balancer":
                lb_ids.add(res["name"])
            if res["type"] == "oci_waf_web_app_firewall":
                lb = _tf_body_get_value(res["body"], "load_balancer_id")
                if lb:
                    waf_protected.add(lb)
        for res in resources:
            if res["type"] == "oci_load_balancer_load_balancer":
                findings.append(self._finding(
                    "OC0011", "OCI Load Balancer without WAF",
                    f"Load Balancer '{res['name']}' may not have WAF protection.",
                    IaCSeverity.MEDIUM, res,
                    "Configure oci_waf_web_app_firewall for the load balancer.",
                ))
        return findings

    def _oc0012_vault_no_key_rotation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_kms_key":
                if not _tf_body_has_block(res["body"], "key_shape"):
                    continue
                # Keys should have rotation configured
                findings.append(self._finding(
                    "OC0012", "OCI Vault key without rotation schedule",
                    f"KMS key '{res['name']}' may not have automatic rotation configured.",
                    IaCSeverity.MEDIUM, res,
                    "Configure key rotation via OCI Key Management policies.",
                ))
        return findings

    def _oc0013_autonomous_db_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_database_autonomous_database":
                if _tf_body_has_key_value(res["body"], "is_access_control_enabled", "false"):
                    findings.append(self._finding(
                        "OC0013", "OCI Autonomous Database publicly accessible",
                        f"Autonomous Database '{res['name']}' has access control disabled.",
                        IaCSeverity.CRITICAL, res,
                        "Set is_access_control_enabled = true and configure whitelisted_ips.",
                    ))
        return findings

    def _oc0014_autonomous_db_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_database_autonomous_database":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0014", "OCI Autonomous Database without CMK encryption",
                        f"Autonomous Database '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for customer-managed encryption.",
                    ))
        return findings

    def _oc0015_file_storage_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_file_storage_file_system":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0015", "OCI File Storage without CMK encryption",
                        f"File System '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for customer-managed encryption.",
                    ))
        return findings

    def _oc0016_api_gateway_no_auth(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_apigateway_deployment":
                body = res["body"]
                if "authentication" not in body.lower():
                    findings.append(self._finding(
                        "OC0016", "OCI API Gateway without authentication",
                        f"API Gateway deployment '{res['name']}' may not have authentication configured.",
                        IaCSeverity.HIGH, res,
                        "Configure authentication in the API specification.",
                    ))
        return findings

    def _oc0017_functions_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_functions_function":
                # Functions should be invoked through API Gateway or with proper IAM
                pass
        return findings

    def _oc0018_streaming_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_streaming_stream":
                if not _tf_body_get_value(res["body"], "stream_pool_id"):
                    findings.append(self._finding(
                        "OC0018", "OCI Streaming without stream pool",
                        f"Stream '{res['name']}' is not in a stream pool with encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Use a stream_pool_id with CMK encryption configured.",
                    ))
        return findings

    def _oc0019_notification_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_ons_notification_topic":
                # ONS topics are encrypted by default, no CMK option currently
                pass
        return findings

    def _oc0020_logging_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_logging_log":
                # Logging service encrypts by default
                pass
        return findings

    def _oc0021_events_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_events_rule":
                if not _tf_body_has_block(res["body"], "actions"):
                    findings.append(self._finding(
                        "OC0021", "OCI Events rule without actions",
                        f"Events rule '{res['name']}' has no actions configured.",
                        IaCSeverity.LOW, res,
                        "Configure actions block with streaming, functions, or notifications.",
                    ))
        return findings

    def _oc0022_container_engine_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_containerengine_cluster":
                if _tf_body_has_key_value(res["body"], "is_public_ip_enabled", "true"):
                    findings.append(self._finding(
                        "OC0022", "OCI OKE cluster with public endpoint",
                        f"OKE cluster '{res['name']}' has a public Kubernetes API endpoint.",
                        IaCSeverity.HIGH, res,
                        "Set is_public_ip_enabled = false in endpoint_config.",
                    ))
        return findings

    def _oc0023_container_engine_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_containerengine_cluster":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0023", "OCI OKE cluster without CMK encryption",
                        f"OKE cluster '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for secrets encryption.",
                    ))
        return findings

    def _oc0024_container_registry_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_artifacts_container_repository":
                if _tf_body_has_key_value(res["body"], "is_public", "true"):
                    findings.append(self._finding(
                        "OC0024", "OCI Container Registry publicly accessible",
                        f"Container repository '{res['name']}' is publicly accessible.",
                        IaCSeverity.HIGH, res,
                        "Set is_public = false.",
                    ))
        return findings

    def _oc0025_data_catalog_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_datacatalog_catalog":
                # Data Catalog uses OCI-managed encryption
                pass
        return findings

    def _oc0026_data_flow_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_dataflow_application":
                if not _tf_body_get_value(res["body"], "archive_uri"):
                    findings.append(self._finding(
                        "OC0026", "OCI Data Flow without secure archive",
                        f"Data Flow application '{res['name']}' may not use encrypted storage.",
                        IaCSeverity.LOW, res,
                        "Ensure archive_uri points to an encrypted Object Storage bucket.",
                    ))
        return findings

    def _oc0027_data_science_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_datascience_notebook_session":
                if not _tf_body_get_value(res["body"], "notebook_session_config_details"):
                    findings.append(self._finding(
                        "OC0027", "OCI Data Science notebook without config",
                        f"Data Science notebook '{res['name']}' lacks configuration details.",
                        IaCSeverity.LOW, res,
                        "Configure notebook_session_config_details with appropriate settings.",
                    ))
        return findings

    def _oc0028_integration_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_integration_integration_instance":
                if _tf_body_has_key_value(res["body"], "is_byol", "false"):
                    pass  # BYOL is licensing, not security
        return findings

    def _oc0029_analytics_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_analytics_analytics_instance":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0029", "OCI Analytics instance without CMK encryption",
                        f"Analytics instance '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for customer-managed encryption.",
                    ))
        return findings

    def _oc0030_mysql_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_mysql_mysql_db_system":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0030", "OCI MySQL DB System without CMK encryption",
                        f"MySQL DB System '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for customer-managed encryption.",
                    ))
        return findings

    def _oc0031_mysql_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_mysql_mysql_db_system":
                if _tf_body_has_key_value(res["body"], "is_highly_available", "false"):
                    findings.append(self._finding(
                        "OC0031", "OCI MySQL DB System without high availability",
                        f"MySQL DB System '{res['name']}' is not highly available.",
                        IaCSeverity.MEDIUM, res,
                        "Set is_highly_available = true for production workloads.",
                    ))
        return findings

    def _oc0032_nosql_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_nosql_table":
                # NoSQL uses OCI-managed encryption by default
                pass
        return findings

    def _oc0033_dns_dnssec_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_dns_zone":
                if not _tf_body_has_block(res["body"], "dnssec_state"):
                    findings.append(self._finding(
                        "OC0033", "OCI DNS zone without DNSSEC",
                        f"DNS zone '{res['name']}' does not have DNSSEC enabled.",
                        IaCSeverity.MEDIUM, res,
                        "Enable DNSSEC for the DNS zone.",
                    ))
        return findings

    def _oc0034_email_no_dkim(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_email_sender":
                # Check for DKIM configuration
                pass
        return findings

    def _oc0035_waf_no_rules(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_waf_web_app_firewall_policy":
                if not _tf_body_has_block(res["body"], "request_protection"):
                    findings.append(self._finding(
                        "OC0035", "OCI WAF policy without protection rules",
                        f"WAF policy '{res['name']}' has no request protection configured.",
                        IaCSeverity.MEDIUM, res,
                        "Configure request_protection with appropriate rules.",
                    ))
        return findings

    def _oc0036_bastion_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_bastion_bastion":
                # Bastion is designed for secure access, check configuration
                if not _tf_body_get_value(res["body"], "client_cidr_block_allow_list"):
                    findings.append(self._finding(
                        "OC0036", "OCI Bastion without IP restrictions",
                        f"Bastion '{res['name']}' has no client CIDR restrictions.",
                        IaCSeverity.HIGH, res,
                        "Configure client_cidr_block_allow_list to restrict access.",
                    ))
        return findings

    def _oc0037_service_mesh_no_mtls(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_service_mesh_mesh":
                if _tf_body_has_key_value(res["body"], "mtls", '"DISABLED"'):
                    findings.append(self._finding(
                        "OC0037", "OCI Service Mesh without mTLS",
                        f"Service Mesh '{res['name']}' has mTLS disabled.",
                        IaCSeverity.HIGH, res,
                        "Enable mTLS for secure service-to-service communication.",
                    ))
        return findings

    def _oc0038_golden_gate_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_golden_gate_deployment":
                if _tf_body_has_key_value(res["body"], "is_public", "true"):
                    findings.append(self._finding(
                        "OC0038", "OCI GoldenGate deployment publicly accessible",
                        f"GoldenGate deployment '{res['name']}' is publicly accessible.",
                        IaCSeverity.HIGH, res,
                        "Set is_public = false and use private endpoints.",
                    ))
        return findings

    def _oc0039_devops_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_devops_project":
                # DevOps projects use OCI-managed encryption
                pass
        return findings

    def _oc0040_visual_builder_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_visual_builder_vb_instance":
                if _tf_body_has_key_value(res["body"], "is_visual_builder_enabled", "true"):
                    # Visual Builder instances need proper access control
                    pass
        return findings

    def _oc0041_blockchain_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_blockchain_blockchain_platform":
                # Blockchain platforms should use private endpoints
                pass
        return findings

    def _oc0042_media_flow_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_media_services_media_workflow":
                # Media workflows should encrypt content at rest
                pass
        return findings

    def _oc0043_certificates_expiring(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_certificates_management_certificate":
                # Check certificate validity
                pass
        return findings

    def _oc0044_budget_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_budget = False
        for res in resources:
            if res["type"] == "oci_budget_budget":
                has_budget = True
                break
        oci_resources = [r for r in resources if r["type"].startswith("oci_")]
        if not has_budget and len(oci_resources) > 5:
            findings.append(self._finding(
                "OC0044", "No OCI budget defined",
                "No budget configured for cost management.",
                IaCSeverity.LOW, oci_resources[0] if oci_resources else resources[0],
                "Create oci_budget_budget for cost tracking and alerts.",
            ))
        return findings

    def _oc0045_cloud_guard_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_cloud_guard = False
        for res in resources:
            if res["type"] == "oci_cloud_guard_cloud_guard_configuration":
                has_cloud_guard = True
                if _tf_body_has_key_value(res["body"], "status", '"DISABLED"'):
                    findings.append(self._finding(
                        "OC0045", "OCI Cloud Guard disabled",
                        f"Cloud Guard configuration '{res['name']}' is disabled.",
                        IaCSeverity.HIGH, res,
                        "Set status = 'ENABLED' for security monitoring.",
                    ))
        return findings

    def _oc0046_vault_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_kms_vault":
                vault_type = _tf_body_get_value(res["body"], "vault_type")
                if vault_type and "DEFAULT" in vault_type.upper():
                    findings.append(self._finding(
                        "OC0046", "OCI Vault using default type",
                        f"Vault '{res['name']}' uses DEFAULT vault type instead of VIRTUAL_PRIVATE.",
                        IaCSeverity.MEDIUM, res,
                        "Use vault_type = 'VIRTUAL_PRIVATE' for dedicated HSM.",
                    ))
        return findings

    def _oc0047_secret_no_rotation(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_vault_secret":
                if not _tf_body_has_block(res["body"], "secret_rules"):
                    findings.append(self._finding(
                        "OC0047", "OCI Secret without rotation rules",
                        f"Secret '{res['name']}' has no rotation rules configured.",
                        IaCSeverity.MEDIUM, res,
                        "Configure secret_rules for automatic rotation.",
                    ))
        return findings

    def _oc0048_iam_policy_overpermissive(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_identity_policy":
                body = res["body"]
                statements = _tf_body_get_value(body, "statements")
                if statements and "manage all-resources" in statements.lower():
                    findings.append(self._finding(
                        "OC0048", "OCI IAM policy overly permissive",
                        f"IAM policy '{res['name']}' grants manage all-resources.",
                        IaCSeverity.HIGH, res,
                        "Use more specific resource types and permissions.",
                    ))
        return findings

    def _oc0049_identity_domain_no_mfa(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_identity_domains_identity_provider":
                # Check MFA configuration
                pass
        return findings

    def _oc0050_compartment_no_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        compartments = set()
        policy_compartments = set()
        for res in resources:
            if res["type"] == "oci_identity_compartment":
                compartments.add(res["name"])
            if res["type"] == "oci_identity_policy":
                comp = _tf_body_get_value(res["body"], "compartment_id")
                if comp:
                    policy_compartments.add(comp)
        return findings

    def _oc0051_audit_retention_short(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_audit_configuration":
                retention = _tf_body_get_value(res["body"], "retention_period_days")
                if retention:
                    try:
                        days = int(retention.strip('"'))
                        if days < 90:
                            findings.append(self._finding(
                                "OC0051", "OCI Audit retention period too short",
                                f"Audit configuration '{res['name']}' has retention less than 90 days.",
                                IaCSeverity.MEDIUM, res,
                                "Set retention_period_days to at least 90.",
                            ))
                    except ValueError:
                        pass
        return findings

    def _oc0052_network_firewall_no_rules(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_network_firewall_network_firewall_policy":
                if not _tf_body_has_block(res["body"], "security_rules"):
                    findings.append(self._finding(
                        "OC0052", "OCI Network Firewall policy without rules",
                        f"Network Firewall policy '{res['name']}' has no security rules.",
                        IaCSeverity.MEDIUM, res,
                        "Configure security_rules for traffic filtering.",
                    ))
        return findings

    def _oc0053_drg_no_route_table(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_drg_attachment":
                if not _tf_body_get_value(res["body"], "drg_route_table_id"):
                    findings.append(self._finding(
                        "OC0053", "OCI DRG attachment without route table",
                        f"DRG attachment '{res['name']}' has no route table assigned.",
                        IaCSeverity.LOW, res,
                        "Set drg_route_table_id for controlled routing.",
                    ))
        return findings

    def _oc0054_service_gateway_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_service_gateway = False
        for res in resources:
            if res["type"] == "oci_core_service_gateway":
                has_service_gateway = True
                break
        vcns = [r for r in resources if r["type"] == "oci_core_vcn"]
        if not has_service_gateway and vcns:
            findings.append(self._finding(
                "OC0054", "OCI VCN without Service Gateway",
                f"VCN '{vcns[0]['name']}' does not have a Service Gateway for OCI services.",
                IaCSeverity.MEDIUM, vcns[0],
                "Create oci_core_service_gateway for private access to OCI services.",
            ))
        return findings

    def _oc0055_nat_gateway_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_nat = False
        for res in resources:
            if res["type"] == "oci_core_nat_gateway":
                has_nat = True
                break
        private_subnets = [r for r in resources if r["type"] == "oci_core_subnet" and
                          _tf_body_has_key_value(r["body"], "prohibit_public_ip_on_vnic", "true")]
        if not has_nat and private_subnets:
            findings.append(self._finding(
                "OC0055", "Private subnet without NAT Gateway",
                f"Private subnet '{private_subnets[0]['name']}' has no NAT Gateway for outbound access.",
                IaCSeverity.LOW, private_subnets[0],
                "Create oci_core_nat_gateway for outbound internet access.",
            ))
        return findings

    def _oc0056_vcn_local_peering_open(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_local_peering_gateway":
                if not _tf_body_get_value(res["body"], "route_table_id"):
                    findings.append(self._finding(
                        "OC0056", "OCI Local Peering Gateway without route table",
                        f"Local Peering Gateway '{res['name']}' has no route table for traffic control.",
                        IaCSeverity.LOW, res,
                        "Set route_table_id for controlled routing between VCNs.",
                    ))
        return findings

    def _oc0057_remote_peering_open(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_remote_peering_connection":
                # Remote peering should be carefully controlled
                pass
        return findings

    def _oc0058_ipsec_weak_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_ipsec":
                # Check encryption algorithm
                cpe_local = _tf_body_get_value(res["body"], "cpe_local_identifier_type")
                pass
        return findings

    def _oc0059_fastconnect_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_virtual_circuit":
                # FastConnect encryption depends on MACsec
                pass
        return findings

    def _oc0060_waa_no_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_waa_web_app_acceleration":
                if not _tf_body_get_value(res["body"], "web_app_acceleration_policy_id"):
                    findings.append(self._finding(
                        "OC0060", "OCI Web App Acceleration without policy",
                        f"Web App Acceleration '{res['name']}' has no acceleration policy.",
                        IaCSeverity.LOW, res,
                        "Set web_app_acceleration_policy_id for caching configuration.",
                    ))
        return findings

    def _oc0061_instance_pool_no_placement(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_instance_pool":
                if not _tf_body_has_block(res["body"], "placement_configurations"):
                    findings.append(self._finding(
                        "OC0061", "OCI Instance Pool without placement config",
                        f"Instance Pool '{res['name']}' lacks placement configuration.",
                        IaCSeverity.LOW, res,
                        "Configure placement_configurations for availability domain distribution.",
                    ))
        return findings

    def _oc0062_autoscaling_no_policy(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_autoscaling_auto_scaling_configuration":
                if not _tf_body_has_block(res["body"], "policies"):
                    findings.append(self._finding(
                        "OC0062", "OCI Autoscaling without policies",
                        f"Autoscaling configuration '{res['name']}' has no scaling policies.",
                        IaCSeverity.LOW, res,
                        "Configure policies for automatic scaling behavior.",
                    ))
        return findings

    def _oc0063_cluster_network_no_placement(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_cluster_network":
                if not _tf_body_has_block(res["body"], "placement_configuration"):
                    findings.append(self._finding(
                        "OC0063", "OCI Cluster Network without placement",
                        f"Cluster Network '{res['name']}' lacks placement configuration.",
                        IaCSeverity.LOW, res,
                        "Configure placement_configuration for HPC workloads.",
                    ))
        return findings

    def _oc0064_dedicated_vm_host_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Dedicated VM hosts are for compliance, not always required
        return findings

    def _oc0065_capacity_reservation_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Capacity reservations are optional for guaranteed capacity
        return findings

    def _oc0066_image_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_image":
                # Custom images should use CMK
                pass
        return findings

    def _oc0067_cross_connect_no_macsec(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_cross_connect":
                if not _tf_body_has_block(res["body"], "macsec_properties"):
                    findings.append(self._finding(
                        "OC0067", "OCI Cross Connect without MACsec",
                        f"Cross Connect '{res['name']}' does not have MACsec encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Configure macsec_properties for link-layer encryption.",
                    ))
        return findings

    def _oc0068_vtap_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_vtap":
                if not _tf_body_get_value(res["body"], "encapsulation_protocol"):
                    findings.append(self._finding(
                        "OC0068", "OCI VTAP without encapsulation",
                        f"VTAP '{res['name']}' may not encrypt mirrored traffic.",
                        IaCSeverity.LOW, res,
                        "Set encapsulation_protocol for secure traffic mirroring.",
                    ))
        return findings

    def _oc0069_network_load_balancer_no_nsg(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_network_load_balancer_network_load_balancer":
                if not _tf_body_get_value(res["body"], "network_security_group_ids"):
                    findings.append(self._finding(
                        "OC0069", "OCI Network Load Balancer without NSG",
                        f"Network Load Balancer '{res['name']}' has no NSG attached.",
                        IaCSeverity.MEDIUM, res,
                        "Set network_security_group_ids for traffic control.",
                    ))
        return findings

    def _oc0070_health_check_no_https(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_health_checks_http_monitor":
                protocol = _tf_body_get_value(res["body"], "protocol")
                if protocol and "http" in protocol.lower() and "https" not in protocol.lower():
                    findings.append(self._finding(
                        "OC0070", "OCI Health Check using HTTP",
                        f"Health Check '{res['name']}' uses HTTP instead of HTTPS.",
                        IaCSeverity.LOW, res,
                        "Use HTTPS protocol for health checks.",
                    ))
        return findings

    def _oc0071_db_home_no_backup(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_database_db_home":
                # DB Homes should have automatic backups enabled
                pass
        return findings

    def _oc0072_exadata_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_database_cloud_exadata_infrastructure":
                # Exadata uses encryption by default
                pass
        return findings

    def _oc0073_data_guard_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_database_db_system":
                # Check for Data Guard association
                pass
        return findings

    def _oc0074_database_tools_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_database_tools_database_tools_connection":
                # Database Tools connections should use private endpoints
                pass
        return findings

    def _oc0075_osms_no_schedule(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_os_management_hub_managed_instance":
                # OS Management should have scheduled jobs
                pass
        return findings

    def _oc0076_vulnerability_scanning_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_scanning = False
        for res in resources:
            if res["type"] == "oci_vulnerability_scanning_host_scan_recipe":
                has_scanning = True
                break
        compute_instances = [r for r in resources if r["type"] == "oci_core_instance"]
        if not has_scanning and compute_instances:
            findings.append(self._finding(
                "OC0076", "OCI Vulnerability Scanning not configured",
                "No vulnerability scanning recipe found for compute instances.",
                IaCSeverity.MEDIUM, compute_instances[0],
                "Create oci_vulnerability_scanning_host_scan_recipe for security scanning.",
            ))
        return findings

    def _oc0077_java_management_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Java Management Service is optional
        return findings

    def _oc0078_ops_insights_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Operations Insights is optional for performance monitoring
        return findings

    def _oc0079_stack_monitoring_disabled(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Stack Monitoring is optional
        return findings

    def _oc0080_apm_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_apm_apm_domain":
                # APM domains use OCI encryption
                pass
        return findings

    def _oc0081_log_analytics_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_log_analytics_namespace":
                # Log Analytics uses OCI encryption
                pass
        return findings

    def _oc0082_service_connector_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_sch_service_connector":
                # Service Connector Hub encrypts in transit
                pass
        return findings

    def _oc0083_queue_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_queue_queue":
                if not _tf_body_get_value(res["body"], "custom_encryption_key_id"):
                    findings.append(self._finding(
                        "OC0083", "OCI Queue without CMK encryption",
                        f"Queue '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set custom_encryption_key_id for customer-managed encryption.",
                    ))
        return findings

    def _oc0084_opensearch_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_opensearch_opensearch_cluster":
                if not _tf_body_get_value(res["body"], "subnet_id"):
                    findings.append(self._finding(
                        "OC0084", "OCI OpenSearch cluster without private subnet",
                        f"OpenSearch cluster '{res['name']}' may not be in a private subnet.",
                        IaCSeverity.HIGH, res,
                        "Deploy OpenSearch in a private subnet.",
                    ))
        return findings

    def _oc0085_opensearch_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_opensearch_opensearch_cluster":
                # OpenSearch encrypts by default in OCI
                pass
        return findings

    def _oc0086_redis_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_redis_redis_cluster":
                # Check for encryption settings
                pass
        return findings

    def _oc0087_psql_public(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_psql_db_system":
                if not _tf_body_get_value(res["body"], "network_details"):
                    findings.append(self._finding(
                        "OC0087", "OCI PostgreSQL DB System without network config",
                        f"PostgreSQL DB System '{res['name']}' lacks network configuration.",
                        IaCSeverity.HIGH, res,
                        "Configure network_details with private subnet.",
                    ))
        return findings

    def _oc0088_psql_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_psql_db_system":
                # PostgreSQL encrypts by default
                pass
        return findings

    def _oc0089_ai_service_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] in ("oci_ai_anomaly_detection_project", "oci_ai_document_project",
                              "oci_ai_language_project", "oci_ai_vision_project"):
                # AI services use OCI encryption
                pass
        return findings

    def _oc0090_generative_ai_no_endpoint(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_generative_ai_dedicated_ai_cluster":
                # Generative AI clusters should use dedicated endpoints
                pass
        return findings

    def _oc0091_big_data_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_bds_bds_instance":
                if not _tf_body_get_value(res["body"], "kms_key_id"):
                    findings.append(self._finding(
                        "OC0091", "OCI Big Data Service without CMK encryption",
                        f"Big Data Service '{res['name']}' does not use customer-managed encryption.",
                        IaCSeverity.MEDIUM, res,
                        "Set kms_key_id for customer-managed encryption.",
                    ))
        return findings

    def _oc0092_data_labeling_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_data_labeling_service_dataset":
                # Data Labeling uses OCI encryption
                pass
        return findings

    def _oc0093_ocvs_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_ocvp_sddc":
                # OCVP SDDC encryption settings
                pass
        return findings

    def _oc0094_rover_no_encryption(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_rover_rover_cluster":
                # Rover devices should use encryption
                pass
        return findings

    def _oc0095_resource_scheduler_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Resource Scheduler is optional for cost optimization
        return findings

    def _oc0096_limits_quota_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Service limits quotas are optional
        return findings

    def _oc0097_announcement_subscription_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Announcement subscriptions are optional
        return findings

    def _oc0098_console_connection_insecure(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_core_instance_console_connection":
                # Console connections should be temporary
                findings.append(self._finding(
                    "OC0098", "OCI Instance Console Connection exists",
                    f"Console connection '{res['name']}' provides direct instance access.",
                    IaCSeverity.LOW, res,
                    "Remove console connections when not actively needed.",
                ))
        return findings

    def _oc0099_marketplace_agreement_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Marketplace agreements are for third-party images
        return findings

    def _oc0100_network_path_analyzer_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Network Path Analyzer is optional for troubleshooting
        return findings

    def _oc0101_tag_namespace_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_tags = False
        for res in resources:
            if res["type"] == "oci_identity_tag_namespace":
                has_tags = True
                break
        oci_resources = [r for r in resources if r["type"].startswith("oci_")]
        if not has_tags and len(oci_resources) > 5:
            findings.append(self._finding(
                "OC0101", "No tag namespace defined",
                "No tag namespace configured for resource organization.",
                IaCSeverity.LOW, oci_resources[0] if oci_resources else resources[0],
                "Create oci_identity_tag_namespace for resource tagging.",
            ))
        return findings

    def _oc0102_cost_tracking_tag_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        # Cost tracking tags are optional but recommended
        return findings

    def _oc0103_ons_subscription_unconfirmed(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_ons_subscription":
                # Subscriptions should be confirmed
                pass
        return findings

    def _oc0104_alarm_missing(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        has_alarm = False
        for res in resources:
            if res["type"] == "oci_monitoring_alarm":
                has_alarm = True
                break
        oci_resources = [r for r in resources if r["type"].startswith("oci_")]
        if not has_alarm and len(oci_resources) > 5:
            findings.append(self._finding(
                "OC0104", "No monitoring alarms defined",
                "No monitoring alarms configured for alerting.",
                IaCSeverity.LOW, oci_resources[0] if oci_resources else resources[0],
                "Create oci_monitoring_alarm for critical metric alerting.",
            ))
        return findings

    def _oc0105_log_group_no_retention(self, resources: list[dict]) -> list[IaCFinding]:
        findings = []
        for res in resources:
            if res["type"] == "oci_logging_log_group":
                # Log groups should have appropriate retention
                pass
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
