# Cairn on an existing GKE cluster with a Cloud Storage bucket (modules/gcp). Validated in CI, not applied.

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
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

variable "project" {
  type = string
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

variable "cairn_secrets" {
  type      = map(string)
  sensitive = true
}

provider "google" {
  project = var.project
  region  = var.region
}

data "google_client_config" "this" {}

data "google_container_cluster" "this" {
  name     = var.cluster_name
  location = var.region
}

provider "kubernetes" {
  host                   = "https://${data.google_container_cluster.this.endpoint}"
  token                  = data.google_client_config.this.access_token
  cluster_ca_certificate = base64decode(data.google_container_cluster.this.master_auth[0].cluster_ca_certificate)
}

provider "helm" {
  kubernetes = {
    host                   = "https://${data.google_container_cluster.this.endpoint}"
    token                  = data.google_client_config.this.access_token
    cluster_ca_certificate = base64decode(data.google_container_cluster.this.master_auth[0].cluster_ca_certificate)
  }
}

module "cairn" {
  source      = "../../modules/gcp"
  project     = var.project
  location    = var.region
  bucket_name = var.bucket_name
  profile     = "ha"
  secrets     = { "cairn-secrets" = var.cairn_secrets }
}
