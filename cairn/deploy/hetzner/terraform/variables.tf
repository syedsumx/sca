variable "name" {
  description = "Prefix for the cloud resources."
  type        = string
  default     = "cairn"
}

variable "location" {
  description = "Load balancer location; use the dedicated servers' data centre region (fsn1, nbg1 or hel1)."
  type        = string
  default     = "fsn1"
  validation {
    condition     = contains(["fsn1", "nbg1", "hel1"], var.location)
    error_message = "Hetzner dedicated servers are in fsn1, nbg1 or hel1."
  }
}

variable "load_balancer_type" {
  description = "lb11 (25 targets, 10k connections) is enough to start; lb21 or lb31 for more."
  type        = string
  default     = "lb11"
}

variable "node_public_ips" {
  description = "Public IPv4 of every dedicated server (the nodes in cluster.env)."
  type        = list(string)
  validation {
    condition     = length(var.node_public_ips) > 0 && alltrue([for ip in var.node_public_ips : can(cidrhost("${ip}/32", 0))])
    error_message = "Give at least one IPv4 address."
  }
}
