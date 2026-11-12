# Cairn on an existing EKS cluster, with an S3 bucket for its backups and metadata exports. Cairn's pods reach the
# bucket through IAM roles for service accounts (no keys): the role trusts the cluster's OIDC provider for Cairn's
# service account only. The aws, helm and kubernetes providers are configured by the caller (../../examples/aws).

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "cluster_name" {
  description = "The EKS cluster (it has an IAM OIDC provider: eksctl utils associate-iam-oidc-provider)."
  type        = string
}

variable "bucket_name" {
  description = "The S3 bucket for backups and metadata exports."
  type        = string
}

variable "create_bucket" {
  description = "Create the bucket (false: it exists)."
  type        = bool
  default     = true
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
  description = "Chart values over this module's (see modules/cairn)."
  type        = any
  default     = {}
}

variable "secrets" {
  description = "Kubernetes Secrets for the release (see modules/cairn). Sensitive."
  type        = map(map(string))
  default     = {}
  sensitive   = true
}

variable "chart_path" {
  type    = string
  default = null
}

variable "model_upstreams" {
  description = "Models Cairn's AI gateway reaches on Amazon Bedrock (L153): the names callers use => Bedrock model or inference profile ids. Cairn's role is allowed to invoke them; empty: none."
  type        = map(string)
  default     = {}
}

variable "bedrock_region" {
  description = "The region Cairn calls Bedrock in (empty: the AWS provider's region)."
  type        = string
  default     = ""
}

data "aws_caller_identity" "current" {}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

data "aws_iam_openid_connect_provider" "this" {
  url = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
}

locals {
  bucket          = var.create_bucket ? aws_s3_bucket.cairn[0].bucket : var.bucket_name
  service_account = "cairn"
  issuer          = replace(data.aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
}

resource "aws_s3_bucket" "cairn" {
  count  = var.create_bucket ? 1 : 0
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "cairn" {
  count                   = var.create_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.cairn[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "cairn" {
  count  = var.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.cairn[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cairn" {
  count  = var.create_bucket ? 1 : 0
  bucket = aws_s3_bucket.cairn[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.this.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:sub"
      values   = ["system:serviceaccount:${var.namespace}:${local.service_account}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cairn" {
  name               = "${var.cluster_name}-cairn"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

data "aws_iam_policy_document" "bucket" {
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.bucket}"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["arn:aws:s3:::${local.bucket}/*"]
  }
}

resource "aws_iam_role_policy" "bucket" {
  name   = "cairn-bucket"
  role   = aws_iam_role.cairn.id
  policy = data.aws_iam_policy_document.bucket.json
}

# Bedrock (L153): Cairn's role (IAM roles for service accounts) invokes foundation models and this account's inference
# profiles; the gateway signs each call with it, so no key exists anywhere.
data "aws_iam_policy_document" "bedrock" {
  statement {
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["arn:aws:bedrock:*::foundation-model/*",
    "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  count  = length(var.model_upstreams) > 0 ? 1 : 0
  name   = "cairn-bedrock"
  role   = aws_iam_role.cairn.id
  policy = data.aws_iam_policy_document.bedrock.json
}

locals {
  cloud_upstreams = length(var.model_upstreams) > 0 ? [{
    name   = "bedrock"
    kind   = "bedrock"
    region = var.bedrock_region != "" ? var.bedrock_region : data.aws_region.current.region
    models = var.model_upstreams
  }] : []
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
        annotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.cairn.arn } }
        metadataExport = { enabled = true, destination = "s3://${local.bucket}/cairn" }
        aiGateway      = { cloudUpstreams = local.cloud_upstreams }
      }
    }),
  ], local.internal_lb_yaml, [yamlencode(var.values)])
}

output "bucket" {
  value = local.bucket
}

output "role_arn" {
  description = "The IAM role Cairn's service account takes."
  value       = aws_iam_role.cairn.arn
}

output "namespace" {
  value = module.cairn.namespace
}
