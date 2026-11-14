# Private networking: the storage account through a Private Endpoint (blob and dfs) with its private DNS zones linked
# to the cluster's VNet, public access to the account turned off, the Cairn gateway and API on an internal load
# balancer, and that load balancer offered to other VNets (customers' data planes) as a Private Link service.

variable "private_networking" {
  description = <<-EOT
    null: public endpoints, as before. Otherwise:
      vnet_id                       the cluster's VNet
      endpoint_subnet_id            the subnet the private endpoints get their addresses in
      internal_load_balancer        the gateway Service on an internal load balancer (default true)
      api_frontend_ip_config_id     the internal load balancer's frontend IP configuration (after the first apply), to
                                    publish with Private Link; null: no Private Link service
      private_link_nat_subnet_id    the subnet for the Private Link service's NAT addresses
      allowed_subscriptions         subscriptions that may see and connect to it (auto-approved)
  EOT
  type = object({
    vnet_id                    = string
    endpoint_subnet_id         = string
    internal_load_balancer     = optional(bool, true)
    api_frontend_ip_config_id  = optional(string)
    private_link_nat_subnet_id = optional(string)
    allowed_subscriptions      = optional(list(string), [])
  })
  default = null
}

locals {
  private  = var.private_networking != null
  internal = local.private && try(var.private_networking.internal_load_balancer, true)
  publish  = local.private && try(var.private_networking.api_frontend_ip_config_id, null) != null
  storage_zones = local.private ? {
    blob = "privatelink.blob.core.windows.net"
    dfs  = "privatelink.dfs.core.windows.net"
  } : {}
  internal_lb_yaml = local.internal ? [yamlencode({
    cairn = {
      gateway = {
        service = {
          type        = "LoadBalancer"
          annotations = { "service.beta.kubernetes.io/azure-load-balancer-internal" = "true" }
        }
      }
    }
    core = { ingress = { annotations = { "appgw.ingress.kubernetes.io/use-private-ip" = "true" } } }
  })] : []
}

resource "azurerm_private_dns_zone" "storage" {
  for_each            = local.storage_zones
  name                = each.value
  resource_group_name = var.resource_group_name
}

resource "azurerm_private_dns_zone_virtual_network_link" "storage" {
  for_each              = local.storage_zones
  name                  = "cairn-${each.key}"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.storage[each.key].name
  virtual_network_id    = var.private_networking.vnet_id
  registration_enabled  = false
}

resource "azurerm_private_endpoint" "storage" {
  for_each            = local.storage_zones
  name                = "${var.storage_account_name}-${each.key}"
  resource_group_name = var.resource_group_name
  location            = data.azurerm_resource_group.this.location
  subnet_id           = var.private_networking.endpoint_subnet_id
  private_service_connection {
    name                           = "${var.storage_account_name}-${each.key}"
    private_connection_resource_id = azurerm_storage_account.cairn.id
    subresource_names              = [each.key]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "cairn-${each.key}"
    private_dns_zone_ids = [azurerm_private_dns_zone.storage[each.key].id]
  }
}

resource "azurerm_private_link_service" "api" {
  count                                       = local.publish ? 1 : 0
  name                                        = "${var.cluster_name}-cairn-api"
  resource_group_name                         = var.resource_group_name
  location                                    = data.azurerm_resource_group.this.location
  load_balancer_frontend_ip_configuration_ids = [var.private_networking.api_frontend_ip_config_id]
  visibility_subscription_ids                 = var.private_networking.allowed_subscriptions
  auto_approval_subscription_ids              = var.private_networking.allowed_subscriptions
  nat_ip_configuration {
    name      = "primary"
    subnet_id = var.private_networking.private_link_nat_subnet_id
    primary   = true
  }
}

output "storage_private_endpoints" {
  description = "The storage account's private endpoints (private networking)."
  value       = { for k, v in azurerm_private_endpoint.storage : k => v.private_service_connection[0].private_ip_address }
}

output "api_private_link_service_alias" {
  description = "The Private Link service other VNets create private endpoints to (private networking)."
  value       = try(azurerm_private_link_service.api[0].alias, null)
}
