# Cairn on an existing GKE cluster (Workload Identity on), with a Cloud Storage bucket for its backups and metadata
# exports. Cairn's service account acts as a Google service account that may use only that bucket (no keys).
# The google, helm and kubernetes providers are configured by the caller (../../examples/gcp).

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project" {
  description = "The Google Cloud project of the cluster and the bucket."
  type        = string
}

variable "location" {
  description = "The bucket's location, e.g. EU or europe-west3."
  type        = string
}

variable "bucket_name" {
  type = string
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

locals {
  service_account = "cairn"
}

resource "google_storage_bucket" "cairn" {
  project                     = var.project
  name                        = var.bucket_name
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
}

resource "google_service_account" "cairn" {
  project      = var.project
  account_id   = "cairn-${substr(md5(var.namespace), 0, 6)}"
  display_name = "Cairn (${var.namespace})"
}

resource "google_storage_bucket_iam_member" "cairn" {
  bucket = google_storage_bucket.cairn.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cairn.email}"
}

resource "google_service_account_iam_member" "workload" {
  service_account_id = google_service_account.cairn.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[${var.namespace}/${local.service_account}]"
}

module "cairn" {
  source     = "../cairn"
  namespace  = var.namespace
  profile    = var.profile
  chart_path = var.chart_path
  secrets    = var.secrets
  values_yaml = concat([
    yamlencode({
      cairn = {
        serviceAccount = { create = true, name = local.service_account,
        annotations = { "iam.gke.io/gcp-service-account" = google_service_account.cairn.email } }
        metadataExport = { enabled = true, destination = "gs://${google_storage_bucket.cairn.name}/cairn" }
      }
    }),
  ], local.internal_lb_yaml, [yamlencode(var.values)])
  depends_on = [google_service_account_iam_member.workload]
}

output "bucket" {
  value = google_storage_bucket.cairn.name
}

output "service_account" {
  value = google_service_account.cairn.email
}

output "namespace" {
  value = module.cairn.namespace
}
