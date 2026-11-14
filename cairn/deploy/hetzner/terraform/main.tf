# The public entry point: a Hetzner Cloud load balancer in front of the dedicated servers. It passes
# TCP through (TLS ends at ingress-nginx, with each tenant's certificate) and adds the PROXY protocol,
# so Cairn sees its users' addresses. Its health check takes a node out when its ingress stops answering.
resource "hcloud_load_balancer" "edge" {
  name               = "${var.name}-edge"
  load_balancer_type = var.load_balancer_type
  location           = var.location
  algorithm {
    type = "least_connections"
  }
  labels = {
    app = "cairn"
  }
}

# Dedicated servers are targets by IP (Hetzner allows IP targets for its own dedicated servers).
resource "hcloud_load_balancer_target" "node" {
  for_each         = toset(var.node_public_ips)
  type             = "ip"
  load_balancer_id = hcloud_load_balancer.edge.id
  ip               = each.value
}

locals {
  ports = {
    http  = { listen = 80, node = 30080 }
    https = { listen = 443, node = 30443 }
  }
}

resource "hcloud_load_balancer_service" "ingress" {
  for_each         = local.ports
  load_balancer_id = hcloud_load_balancer.edge.id
  protocol         = "tcp"
  listen_port      = each.value.listen
  destination_port = each.value.node
  proxyprotocol    = true
  health_check {
    protocol = "tcp"
    port     = each.value.node
    interval = 10
    timeout  = 5
    retries  = 3
  }
}
