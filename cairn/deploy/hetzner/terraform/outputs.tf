output "lb_ipv4" {
  description = "Point *.<CAIRN_HOST_DOMAIN> here (DNS A record), and set LB_SOURCE_CIDRS=\"<this>/32\" in cluster.env."
  value       = hcloud_load_balancer.edge.ipv4
}

output "lb_ipv6" {
  description = "Optional AAAA record for *.<CAIRN_HOST_DOMAIN>."
  value       = hcloud_load_balancer.edge.ipv6
}
