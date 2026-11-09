# Cairn on an existing AKS cluster with a storage account (modules/azure). Validated in CI, not applied.

terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
  }
}

variable "subscription_id" {
  type = string
}

variable "resource_group_name" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "storage_account_name" {
  type = string
}

variable "private_networking" {
  description = "The module's private_networking (private endpoints, an internal load balancer); null: public."
  type        = any
  default     = null
}

variable "cairn_secrets" {
  type      = map(string)
  sensitive = true
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

data "azurerm_kubernetes_cluster" "this" {
  name                = var.cluster_name
  resource_group_name = var.resource_group_name
}

provider "kubernetes" {
  host                   = data.azurerm_kubernetes_cluster.this.kube_config[0].host
  client_certificate     = base64decode(data.azurerm_kubernetes_cluster.this.kube_config[0].client_certificate)
  client_key             = base64decode(data.azurerm_kubernetes_cluster.this.kube_config[0].client_key)
  cluster_ca_certificate = base64decode(data.azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate)
}

provider "helm" {
  kubernetes = {
    host                   = data.azurerm_kubernetes_cluster.this.kube_config[0].host
    client_certificate     = base64decode(data.azurerm_kubernetes_cluster.this.kube_config[0].client_certificate)
    client_key             = base64decode(data.azurerm_kubernetes_cluster.this.kube_config[0].client_key)
    cluster_ca_certificate = base64decode(data.azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate)
  }
}

module "cairn" {
  source               = "../../modules/azure"
  resource_group_name  = var.resource_group_name
  cluster_name         = var.cluster_name
  storage_account_name = var.storage_account_name
  profile              = "ha"
  secrets              = { "cairn-secrets" = var.cairn_secrets }
  # Private endpoints and an internal load balancer (see the module's private_networking): null keeps them public.
  private_networking = var.private_networking
}
