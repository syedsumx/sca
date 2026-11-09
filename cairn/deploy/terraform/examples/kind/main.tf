# Cairn's API on a local kind cluster, from modules/cairn: what CI applies (job terraform-kind) before checking the
# API's /health. A minimal profile: no Cairn Core (core.enabled=false), only the API, one replica, SQLite in the pod.
#
#   kind create cluster --name cairn
#   docker build -f docker/Dockerfile -t cairn/platform:ci . && kind load docker-image cairn/platform:ci --name cairn
#   TF_VAR_service_token=$(openssl rand -hex 32) terraform apply

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
  }
}

variable "kubeconfig" {
  description = "The cluster's kubeconfig."
  type        = string
  default     = "~/.kube/config"
}

variable "context" {
  type    = string
  default = "kind-cairn"
}

variable "image_tag" {
  type    = string
  default = "ci"
}

variable "service_token" {
  description = "Cairn's service token (TF_VAR_service_token; never in a file)."
  type        = string
  sensitive   = true
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig)
  config_context = var.context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig)
    config_context = var.context
  }
}

module "cairn" {
  source    = "../../modules/cairn"
  namespace = "cairn"
  # CI waits for the API itself (kubectl rollout status), then asks its /health.
  wait   = false
  atomic = false
  secrets = {
    "cairn-secrets" = {
      "service-token"        = var.service_token
      "database-url"         = "sqlite:////tmp/cairn.db"
      "action-link-secret"   = sha256("${var.service_token}|actions")
      "embed-signing-secret" = sha256("${var.service_token}|embeds")
    }
  }
  values = {
    global = { cairn = { registry = "cairn", image = { tag = var.image_tag, pullPolicy = "Never" } } }
    core   = { enabled = false }
    cairn = {
      core          = { url = "http://cairn-core.invalid:8080", frontendUrl = "http://cairn-core.invalid:9002" }
      api           = { replicas = 1, autoscaling = { enabled = false } }
      pdp           = { replicas = 1, autoscaling = { enabled = false }, pdb = { enabled = false } }
      gateway       = { replicas = 1, autoscaling = { enabled = false }, pdb = { enabled = false } }
      mcp           = { enabled = false }
      scheduler     = { enabled = false }
      coreProxy     = { enabled = false }
      contextMiner  = { enabled = false }
      notifier      = { enabled = false }
      consumerUi    = { enabled = false }
      networkPolicy = { enabled = false }
      actions = {
        piiPropagation = { enabled = false }
        vectorIndexer  = { enabled = false }
      }
    }
  }
}

output "api_service" {
  value = module.cairn.api_service
}
