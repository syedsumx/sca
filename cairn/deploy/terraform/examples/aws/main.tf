# Cairn on an existing EKS cluster with an S3 bucket (modules/aws). Validated in CI (terraform validate), not applied.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
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

variable "region" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "bucket_name" {
  type = string
}

variable "private_networking" {
  description = "The module's private_networking (private endpoints, an internal load balancer); null: public."
  type        = any
  default     = null
}

variable "cairn_secrets" {
  description = "cairn-secrets' keys (service-token, database-url, ...): TF_VAR_cairn_secrets or a secrets manager."
  type        = map(string)
  sensitive   = true
}

provider "aws" {
  region = var.region
}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

data "aws_eks_cluster_auth" "this" {
  name = var.cluster_name
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.this.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.this.token
}

provider "helm" {
  kubernetes = {
    host                   = data.aws_eks_cluster.this.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.this.token
  }
}

module "cairn" {
  source       = "../../modules/aws"
  cluster_name = var.cluster_name
  bucket_name  = var.bucket_name
  profile      = "ha"
  secrets      = { "cairn-secrets" = var.cairn_secrets }
  # Private endpoints and an internal load balancer (see the module's private_networking): null keeps them public.
  private_networking = var.private_networking
}
