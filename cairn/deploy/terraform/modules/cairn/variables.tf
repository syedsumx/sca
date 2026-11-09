variable "namespace" {
  description = "The namespace Cairn is installed in."
  type        = string
  default     = "cairn"
}

variable "create_namespace" {
  description = "Create the namespace (false: it exists, e.g. made by the platform team)."
  type        = bool
  default     = true
}

variable "release_name" {
  description = "The Helm release's name."
  type        = string
  default     = "cairn"
}

variable "chart_path" {
  description = "The chart: a local path (default: helm/cairn of this repository) or a chart name with repository."
  type        = string
  default     = null
}

variable "chart_repository" {
  description = "The chart repository, when chart_path is a chart name (an OCI or HTTP repository)."
  type        = string
  default     = null
}

variable "chart_version" {
  description = "The chart version, from a repository (null: the local chart as it is)."
  type        = string
  default     = null
}

variable "profile" {
  description = "A values profile of the chart: default, ha, tenant, ha-tenant, active-metadata or authoritative."
  type        = string
  default     = "default"
  validation {
    condition     = contains(["default", "ha", "tenant", "ha-tenant", "active-metadata", "authoritative"], var.profile)
    error_message = "profile: default, ha, tenant, ha-tenant, active-metadata or authoritative."
  }
}

variable "values" {
  description = "Chart values (as HCL), applied over the profile; e.g. { cairn = { trino = { enabled = true } } }."
  type        = any
  default     = {}
}

variable "values_yaml" {
  description = "More values documents (YAML), applied in order after var.values (modules that wrap this one add theirs)."
  type        = list(string)
  default     = []
}

variable "secrets" {
  description = <<-EOT
    Kubernetes Secrets made before the release, by name: { "cairn-secrets" = { "service-token" = "...",
    "database-url" = "..." } }. Sensitive: give them from sensitive variables or a secrets manager's data source,
    never in a .tf file. They land in Terraform's state: keep the state encrypted and access-controlled.
  EOT
  type        = map(map(string))
  default     = {}
  sensitive   = true
}

variable "tenants" {
  description = <<-EOT
    Hosted tenants, by id: each a release of the chart in namespace cairn-<id> with the tenant profile
    (values-tenant.yaml) and its own values, as deploy/hosting/tenant.sh makes them. Their databases are made by
    tenant.sh create (or your database module): give their URLs in tenant_secrets.
  EOT
  type = map(object({
    values = optional(any, {})
    quota = optional(object({
      cpu    = optional(string, "8")
      memory = optional(string, "32Gi")
      pods   = optional(string, "100")
    }), {})
  }))
  default = {}
  validation {
    condition     = alltrue([for id in keys(var.tenants) : can(regex("^[a-z0-9][a-z0-9-]{1,30}$", id))])
    error_message = "A tenant id is lowercase letters, digits and -, 2 to 31 characters."
  }
}

variable "tenant_secrets" {
  description = "Each tenant's Secrets, by tenant id then Secret name (the licence, cairn-secrets, ...). Sensitive."
  type        = map(map(map(string)))
  default     = {}
  sensitive   = true
}

variable "wait" {
  description = "Wait until every Deployment is ready."
  type        = bool
  default     = true
}

variable "timeout" {
  description = "Seconds Helm waits for the release."
  type        = number
  default     = 900
}

variable "atomic" {
  description = "Roll a failed install or upgrade back."
  type        = bool
  default     = true
}
