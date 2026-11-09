# Private networking: S3 through a VPC gateway endpoint (the bucket can be limited to it), the Cairn gateway and API
# on an internal load balancer only, and that load balancer offered to other VPCs (customers' data planes, private
# clients) as a PrivateLink endpoint service.

variable "private_networking" {
  description = <<-EOT
    null: public endpoints, as before. Otherwise:
      vpc_id                       the cluster's VPC
      route_table_ids              route tables that get the S3 gateway endpoint
      restrict_bucket_to_endpoint  deny S3 requests to the bucket that do not come through the endpoint (default false)
      internal_load_balancer       the gateway Service and the Ingress on internal load balancers (default true)
      api_nlb_arn                  the internal NLB in front of the gateway/API (after the first apply), to publish
                                   with PrivateLink; null: no endpoint service
      allowed_principals           AWS principals (ARNs) allowed to connect to the endpoint service
  EOT
  type = object({
    vpc_id                      = string
    route_table_ids             = list(string)
    restrict_bucket_to_endpoint = optional(bool, false)
    internal_load_balancer      = optional(bool, true)
    api_nlb_arn                 = optional(string)
    allowed_principals          = optional(list(string), [])
  })
  default = null
}

data "aws_region" "current" {}

locals {
  private  = var.private_networking != null
  internal = local.private && try(var.private_networking.internal_load_balancer, true)
  internal_lb_yaml = local.internal ? [yamlencode({
    cairn = {
      gateway = {
        service = {
          type = "LoadBalancer"
          annotations = {
            "service.beta.kubernetes.io/aws-load-balancer-type"            = "external"
            "service.beta.kubernetes.io/aws-load-balancer-scheme"          = "internal"
            "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type" = "ip"
          }
        }
      }
    }
    core = { ingress = { annotations = { "alb.ingress.kubernetes.io/scheme" = "internal" } } }
  })] : []
}

resource "aws_vpc_endpoint" "s3" {
  count             = local.private ? 1 : 0
  vpc_id            = var.private_networking.vpc_id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.private_networking.route_table_ids
  tags              = { Name = "${var.cluster_name}-cairn-s3" }
}

data "aws_iam_policy_document" "bucket_via_endpoint" {
  count = local.private && var.create_bucket && try(var.private_networking.restrict_bucket_to_endpoint, false) ? 1 : 0
  statement {
    sid       = "OnlyThroughTheVpcEndpoint"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::${local.bucket}", "arn:aws:s3:::${local.bucket}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:SourceVpce"
      values   = [aws_vpc_endpoint.s3[0].id]
    }
  }
}

resource "aws_s3_bucket_policy" "via_endpoint" {
  count  = length(data.aws_iam_policy_document.bucket_via_endpoint)
  bucket = aws_s3_bucket.cairn[0].id
  policy = data.aws_iam_policy_document.bucket_via_endpoint[0].json
}

resource "aws_vpc_endpoint_service" "api" {
  count                      = local.private && try(var.private_networking.api_nlb_arn, null) != null ? 1 : 0
  acceptance_required        = true
  network_load_balancer_arns = [var.private_networking.api_nlb_arn]
  allowed_principals         = var.private_networking.allowed_principals
  tags                       = { Name = "${var.cluster_name}-cairn-api" }
}

output "s3_vpc_endpoint_id" {
  description = "The S3 gateway endpoint (private networking)."
  value       = try(aws_vpc_endpoint.s3[0].id, null)
}

output "api_endpoint_service_name" {
  description = "The PrivateLink service name other VPCs create interface endpoints to (private networking)."
  value       = try(aws_vpc_endpoint_service.api[0].service_name, null)
}
