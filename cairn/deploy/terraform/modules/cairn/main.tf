# Cairn on an existing Kubernetes cluster: the Helm chart (helm/cairn) with a values profile and your values, the
# Secrets it reads, and hosted tenants (one release each, as deploy/hosting/tenant.sh makes them).
# The helm and kubernetes providers are configured by the caller (see ../../examples).

locals {
  chart = coalesce(var.chart_path, "${path.module}/../../../../helm/cairn")
  # The chart's names: the release's name when it holds "cairn", else <release>-cairn (cairn.fullname).
  fullname = strcontains(var.release_name, "cairn") ? var.release_name : "${var.release_name}-cairn"
  local    = var.chart_repository == null
  profiles = {
    default         = []
    ha              = ["values-ha.yaml"]
    tenant          = ["values-tenant.yaml"]
    ha-tenant       = ["values-ha.yaml", "values-tenant.yaml"]
    active-metadata = ["values-active-metadata.yaml"]
    authoritative   = ["values-authoritative.yaml"]
  }
  # A chart from a repository carries its profiles inside; a local chart's are read from its folder.
  profile_values = local.local ? [for f in local.profiles[var.profile] : file("${local.chart}/${f}")] : []
  tenant_values  = local.local ? file("${local.chart}/values-tenant.yaml") : ""
  secret_names   = toset(nonsensitive(keys(var.secrets)))
  # The licence: a Secret of its own, named in the chart's values (the chart installs it at start and on change).
  has_licence    = nonsensitive(var.licence != null) || var.licence_file != null
  licence_secret = "${local.fullname}-licence"
  licence_values = local.has_licence ? [yamlencode({ cairn = { licence = {
    existingSecret = local.licence_secret, secretKey = "licence", required = var.licence_required,
    tenantId       = var.licence_tenant_id
  } } })] : []
  tenant_licence_ids = toset(nonsensitive(keys(var.tenant_licences)))
  tenant_secret_pairs = merge([
    for id, secrets in nonsensitive(var.tenant_secrets) : { for name, _ in secrets : "${id}/${name}" => { id = id, name = name } }
  ]...)
}

resource "kubernetes_namespace_v1" "cairn" {
  count = var.create_namespace ? 1 : 0
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "cairn"
    }
  }
}

resource "kubernetes_secret_v1" "cairn" {
  for_each = local.secret_names
  metadata {
    name      = each.key
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "cairn"
    }
  }
  data       = var.secrets[each.key]
  type       = "Opaque"
  depends_on = [kubernetes_namespace_v1.cairn]
}

resource "kubernetes_secret_v1" "licence" {
  count = local.has_licence ? 1 : 0
  metadata {
    name      = local.licence_secret
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "cairn"
    }
  }
  data = { licence = var.licence != null ? var.licence : file(var.licence_file) }
  type = "Opaque"
  lifecycle {
    precondition {
      condition     = !(nonsensitive(var.licence != null) && var.licence_file != null)
      error_message = "Give licence or licence_file, not both."
    }
  }
  depends_on = [kubernetes_namespace_v1.cairn]
}

resource "helm_release" "cairn" {
  name       = var.release_name
  namespace  = var.namespace
  chart      = local.chart
  repository = var.chart_repository
  version    = var.chart_version
  # The licence's values come after var.values: the licence variables decide where the chart reads it from.
  values     = concat(local.profile_values, [yamlencode(var.values)], local.licence_values, var.values_yaml)
  wait       = var.wait
  timeout    = var.timeout
  atomic     = var.atomic
  depends_on = [kubernetes_namespace_v1.cairn, kubernetes_secret_v1.cairn, kubernetes_secret_v1.licence]
}

# ---- hosted tenants -----------------------------------------------------------------------------------------------
resource "kubernetes_namespace_v1" "tenant" {
  for_each = var.tenants
  metadata {
    name = "cairn-${each.key}"
    labels = {
      "app.kubernetes.io/part-of" = "cairn"
      "cairn.io/tenant"           = each.key
    }
  }
}

resource "kubernetes_resource_quota_v1" "tenant" {
  for_each = var.tenants
  metadata {
    name      = "cairn-tenant"
    namespace = kubernetes_namespace_v1.tenant[each.key].metadata[0].name
  }
  spec {
    hard = {
      "requests.cpu"    = each.value.quota.cpu
      "requests.memory" = each.value.quota.memory
      pods              = each.value.quota.pods
    }
  }
}

resource "kubernetes_secret_v1" "tenant" {
  for_each = local.tenant_secret_pairs
  metadata {
    name      = each.value.name
    namespace = kubernetes_namespace_v1.tenant[each.value.id].metadata[0].name
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "cairn.io/tenant"              = each.value.id
    }
  }
  data = var.tenant_secrets[each.value.id][each.value.name]
  type = "Opaque"
}

resource "kubernetes_secret_v1" "tenant_licence" {
  for_each = local.tenant_licence_ids
  metadata {
    name      = "cairn-licence"
    namespace = kubernetes_namespace_v1.tenant[each.key].metadata[0].name
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "cairn.io/tenant"              = each.key
    }
  }
  data = { licence = var.tenant_licences[each.key] }
  type = "Opaque"
  lifecycle {
    precondition {
      condition     = contains(keys(var.tenants), each.key)
      error_message = "tenant_licences: ${each.key} is not one of tenants."
    }
  }
}

resource "helm_release" "tenant" {
  for_each   = var.tenants
  name       = "cairn"
  namespace  = kubernetes_namespace_v1.tenant[each.key].metadata[0].name
  chart      = local.chart
  repository = var.chart_repository
  version    = var.chart_version
  # A tenant with a licence in tenant_licences requires it, bound to its id (after its own values).
  values = compact([local.tenant_values, yamlencode(each.value.values),
    contains(local.tenant_licence_ids, each.key) ? yamlencode({ cairn = { licence = {
      required = true, tenantId = each.key, existingSecret = "cairn-licence", secretKey = "licence"
  } } }) : ""])
  wait       = var.wait
  timeout    = var.timeout
  atomic     = var.atomic
  depends_on = [kubernetes_secret_v1.tenant, kubernetes_secret_v1.tenant_licence, kubernetes_resource_quota_v1.tenant]
}
