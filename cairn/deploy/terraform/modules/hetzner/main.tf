# Cairn's hosting cluster on Hetzner dedicated servers, from Terraform: the cloud load balancer in front of the nodes
# (deploy/hetzner/terraform) and the bootstrap deploy/hetzner/cluster.sh does (each node prepared, k3s with an
# embedded etcd, the admin kubeconfig, the backup buckets, the shared platform services), each step run once and
# again when its trigger changes. Then modules/cairn installs Cairn (or tenants) on the cluster it made.
#
# Secrets stay out of Terraform: cluster.env (the servers, the object storage keys, ...) is read by the scripts on
# your machine, never by Terraform, so nothing of it is in the state. HCLOUD_TOKEN is the hcloud provider's.

terraform {
  required_version = ">= 1.6"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.52"
    }
  }
}

variable "cluster_env" {
  description = "The path of cluster.env (deploy/hetzner/cluster.env.example; outside git, chmod 600)."
  type        = string
}

variable "name" {
  description = "Prefix for the cloud resources (the load balancer)."
  type        = string
  default     = "cairn"
}

variable "location" {
  description = "fsn1, nbg1 or hel1: the dedicated servers' region."
  type        = string
  default     = "fsn1"
}

variable "load_balancer_type" {
  type    = string
  default = "lb11"
}

variable "node_public_ips" {
  description = "Public IPv4 of every node (as in cluster.env)."
  type        = list(string)
}

variable "k3s_version" {
  description = "The k3s version (cluster.env's K3S_VERSION should match): changing it runs the k3s step again."
  type        = string
  default     = "v1.33.3+k3s1"
}

variable "buckets" {
  description = "Make the backup buckets in Hetzner Object Storage (needs the AWS CLI and the keys in cluster.env)."
  type        = bool
  default     = true
}

variable "platform" {
  description = "Install the shared platform services (ingress, cert-manager, CloudNativePG, monitoring)."
  type        = bool
  default     = true
}

variable "revision" {
  description = "Raise it to run every bootstrap step again (they are idempotent)."
  type        = number
  default     = 1
}

locals {
  scripts    = abspath("${path.module}/../../../hetzner")
  env        = abspath(var.cluster_env)
  nodes      = join(",", var.node_public_ips)
  state_dir  = "${dirname(local.env)}/.cluster-${var.name}"
  kubeconfig = "${local.state_dir}/kubeconfig"
}

module "edge" {
  source             = "../../../hetzner/terraform"
  name               = var.name
  location           = var.location
  load_balancer_type = var.load_balancer_type
  node_public_ips    = var.node_public_ips
}

resource "terraform_data" "prepare" {
  triggers_replace = [local.nodes, var.revision]
  provisioner "local-exec" {
    command = "${local.scripts}/cluster.sh ${local.env} prepare"
  }
}

resource "terraform_data" "k3s" {
  triggers_replace = [local.nodes, var.k3s_version, var.revision]
  provisioner "local-exec" {
    command = "${local.scripts}/cluster.sh ${local.env} k3s && ${local.scripts}/cluster.sh ${local.env} kubeconfig"
  }
  depends_on = [terraform_data.prepare]
}

resource "terraform_data" "buckets" {
  count            = var.buckets ? 1 : 0
  triggers_replace = [var.revision]
  provisioner "local-exec" {
    command = "${local.scripts}/cluster.sh ${local.env} buckets"
  }
  depends_on = [terraform_data.k3s]
}

resource "terraform_data" "platform" {
  count            = var.platform ? 1 : 0
  triggers_replace = [var.revision, module.edge.lb_ipv4]
  provisioner "local-exec" {
    command = "${local.scripts}/cluster.sh ${local.env} platform"
  }
  depends_on = [terraform_data.k3s, terraform_data.buckets]
}

output "lb_ipv4" {
  description = "Point *.<your domain> here, and set LB_SOURCE_CIDRS=\"<this>/32\" in cluster.env."
  value       = module.edge.lb_ipv4
}

output "kubeconfig" {
  description = "The admin kubeconfig cluster.sh wrote (configure the helm and kubernetes providers with it)."
  value       = local.kubeconfig
  depends_on  = [terraform_data.k3s]
}
