# Private networking: Cloud Storage through Private Service Connect (an internal address for Google's APIs and a
# private DNS zone pointing storage.googleapis.com at it), the Cairn gateway and API on an internal load balancer,
# and that load balancer published to other VPCs (customers' data planes) as a Private Service Connect service.

variable "private_networking" {
  description = <<-EOT
    null: public endpoints, as before. Otherwise:
      network                 the VPC's self link (the cluster's)
      region                  the region of the published service and its NAT subnet
      psc_address             an unused internal address for Google's APIs (e.g. 10.250.0.2), outside every subnet
      internal_load_balancer  the gateway Service and the Ingress on internal load balancers (default true)
      api_forwarding_rule     the internal load balancer's forwarding rule (after the first apply) to publish; null: none
      nat_cidr                a range for the published service's NAT subnet (e.g. 10.251.0.0/24)
      consumer_projects       projects allowed to connect to it (accepted automatically)
  EOT
  type = object({
    network                = string
    region                 = string
    psc_address            = string
    internal_load_balancer = optional(bool, true)
    api_forwarding_rule    = optional(string)
    nat_cidr               = optional(string, "10.251.0.0/24")
    consumer_projects      = optional(list(string), [])
  })
  default = null
}

locals {
  private  = var.private_networking != null
  internal = local.private && try(var.private_networking.internal_load_balancer, true)
  publish  = local.private && try(var.private_networking.api_forwarding_rule, null) != null
  internal_lb_yaml = local.internal ? [yamlencode({
    cairn = {
      gateway = {
        service = {
          type        = "LoadBalancer"
          annotations = { "networking.gke.io/load-balancer-type" = "Internal" }
        }
      }
    }
    core = { ingress = { className = "gce-internal" } }
  })] : []
}

resource "google_compute_global_address" "apis" {
  count        = local.private ? 1 : 0
  project      = var.project
  name         = "cairn-${substr(md5(var.namespace), 0, 6)}-apis"
  purpose      = "PRIVATE_SERVICE_CONNECT"
  address_type = "INTERNAL"
  address      = var.private_networking.psc_address
  network      = var.private_networking.network
}

resource "google_compute_global_forwarding_rule" "apis" {
  count                 = local.private ? 1 : 0
  project               = var.project
  name                  = "cairnapis${substr(md5(var.namespace), 0, 6)}"
  target                = "all-apis"
  network               = var.private_networking.network
  ip_address            = google_compute_global_address.apis[0].id
  load_balancing_scheme = ""
}

resource "google_dns_managed_zone" "storage" {
  count       = local.private ? 1 : 0
  project     = var.project
  name        = "cairn-${substr(md5(var.namespace), 0, 6)}-storage"
  dns_name    = "storage.googleapis.com."
  description = "Cloud Storage through Private Service Connect (Cairn)"
  visibility  = "private"
  private_visibility_config {
    networks {
      network_url = var.private_networking.network
    }
  }
}

resource "google_dns_record_set" "storage" {
  count        = local.private ? 1 : 0
  project      = var.project
  managed_zone = google_dns_managed_zone.storage[0].name
  name         = "storage.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.apis[0].address]
}

resource "google_compute_subnetwork" "psc_nat" {
  count         = local.publish ? 1 : 0
  project       = var.project
  name          = "cairn-${substr(md5(var.namespace), 0, 6)}-psc-nat"
  region        = var.private_networking.region
  network       = var.private_networking.network
  ip_cidr_range = var.private_networking.nat_cidr
  purpose       = "PRIVATE_SERVICE_CONNECT"
}

resource "google_compute_service_attachment" "api" {
  count                 = local.publish ? 1 : 0
  project               = var.project
  name                  = "cairn-${substr(md5(var.namespace), 0, 6)}-api"
  region                = var.private_networking.region
  enable_proxy_protocol = false
  connection_preference = "ACCEPT_MANUAL"
  nat_subnets           = [google_compute_subnetwork.psc_nat[0].id]
  target_service        = var.private_networking.api_forwarding_rule
  dynamic "consumer_accept_lists" {
    for_each = var.private_networking.consumer_projects
    content {
      project_id_or_num = consumer_accept_lists.value
      connection_limit  = 10
    }
  }
}

output "storage_private_address" {
  description = "The internal address Cloud Storage is reached at (private networking)."
  value       = try(google_compute_global_address.apis[0].address, null)
}

output "api_service_attachment" {
  description = "The Private Service Connect service other VPCs connect to (private networking)."
  value       = try(google_compute_service_attachment.api[0].self_link, null)
}
