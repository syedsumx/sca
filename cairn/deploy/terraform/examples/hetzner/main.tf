# Cairn's hosting cluster on Hetzner, end to end: the load balancer and the cluster (modules/hetzner), then Cairn's
# tenants on it (modules/cairn). HCLOUD_TOKEN for the load balancer; cluster.env for the servers (outside git).
#
#   export HCLOUD_TOKEN=...            # or a credentials file read by your shell, never typed on a command line
#   terraform apply -var cluster_env=$HOME/cairn/cluster.env -var 'node_public_ips=["203.0.113.11", ...]'

terraform {
  required_version = ">= 1.6"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.52"
    }
  }
}

variable "cluster_env" {
  type = string
}

variable "node_public_ips" {
  type = list(string)
}

variable "tenant_licences" {
  description = "Each tenant's licence (a file's contents), by tenant id. Sensitive: from a secrets manager or TF_VAR_."
  type        = map(string)
  default     = {}
  sensitive   = true
}

module "cluster" {
  source          = "../../modules/hetzner"
  cluster_env     = var.cluster_env
  node_public_ips = var.node_public_ips
}

provider "kubernetes" {
  config_path = module.cluster.kubeconfig
}

provider "helm" {
  kubernetes = {
    config_path = module.cluster.kubeconfig
  }
}

module "cairn" {
  source           = "../../modules/cairn"
  create_namespace = false
  namespace        = "cairn-platform"
  profile          = "ha-tenant"
  # Each tenant's database credentials come from deploy/hosting/tenant.sh create (cairn-secrets in its namespace);
  # here, its release and its licence (the module makes the Secret and requires it, bound to the tenant).
  tenants         = { for id in nonsensitive(keys(var.tenant_licences)) : id => {} }
  tenant_licences = var.tenant_licences
}

output "lb_ipv4" {
  value = module.cluster.lb_ipv4
}
