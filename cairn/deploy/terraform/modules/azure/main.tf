# Cairn on an existing AKS cluster (OIDC issuer and workload identity on), with a storage account container for its
# backups and metadata exports. Cairn's service account is federated to a user-assigned identity that may use only
# that account's blobs (no keys). The azurerm, helm and kubernetes providers are configured by the caller
# (../../examples/azure).

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

variable "resource_group_name" {
  description = "The resource group of the cluster; the storage account and the identity are made in it."
  type        = string
}

variable "cluster_name" {
  type = string
}

variable "storage_account_name" {
  description = "3 to 24 lowercase letters and digits, unique in Azure."
  type        = string
}

variable "namespace" {
  type    = string
  default = "cairn"
}

variable "profile" {
  type    = string
  default = "default"
}

variable "values" {
  type    = any
  default = {}
}

variable "secrets" {
  type      = map(map(string))
  default   = {}
  sensitive = true
}

variable "chart_path" {
  type    = string
  default = null
}

data "azurerm_resource_group" "this" {
  name = var.resource_group_name
}

data "azurerm_kubernetes_cluster" "this" {
  name                = var.cluster_name
  resource_group_name = var.resource_group_name
}

locals {
  service_account = "cairn"
}

resource "azurerm_storage_account" "cairn" {
  name                            = var.storage_account_name
  resource_group_name             = var.resource_group_name
  location                        = data.azurerm_resource_group.this.location
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  account_kind                    = "StorageV2"
  is_hns_enabled                  = true
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
}

resource "azurerm_storage_container" "cairn" {
  name                  = "cairn"
  storage_account_id    = azurerm_storage_account.cairn.id
  container_access_type = "private"
}

resource "azurerm_user_assigned_identity" "cairn" {
  name                = "${var.cluster_name}-cairn"
  resource_group_name = var.resource_group_name
  location            = data.azurerm_resource_group.this.location
}

resource "azurerm_role_assignment" "blobs" {
  scope                = azurerm_storage_account.cairn.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.cairn.principal_id
}

resource "azurerm_federated_identity_credential" "cairn" {
  name                = "cairn-${var.namespace}"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.cairn.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = data.azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject             = "system:serviceaccount:${var.namespace}:${local.service_account}"
}

module "cairn" {
  source     = "../cairn"
  namespace  = var.namespace
  profile    = var.profile
  chart_path = var.chart_path
  secrets    = var.secrets
  values_yaml = [
    yamlencode({
      cairn = {
        serviceAccount = { create = true, name = local.service_account,
        annotations = { "azure.workload.identity/client-id" = azurerm_user_assigned_identity.cairn.client_id } }
        metadataExport = {
          enabled     = true
          destination = "abfs://cairn@${azurerm_storage_account.cairn.name}.dfs.core.windows.net/cairn"
        }
      }
    }),
    yamlencode(var.values),
  ]
  depends_on = [azurerm_role_assignment.blobs, azurerm_federated_identity_credential.cairn]
}

output "storage_account" {
  value = azurerm_storage_account.cairn.name
}

output "identity_client_id" {
  value = azurerm_user_assigned_identity.cairn.client_id
}

output "namespace" {
  value = module.cairn.namespace
}
