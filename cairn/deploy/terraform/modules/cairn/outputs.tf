output "namespace" {
  description = "The namespace Cairn runs in."
  value       = helm_release.cairn.namespace
}

output "release" {
  description = "The Helm release's name, chart version and status."
  value = {
    name    = helm_release.cairn.name
    version = helm_release.cairn.metadata.version
    status  = helm_release.cairn.status
  }
}

output "api_service" {
  description = "The API's Service in the cluster (its /health answers when Cairn is up)."
  value       = "${local.fullname}-api.${var.namespace}.svc"
}

output "tenant_namespaces" {
  description = "Each hosted tenant's namespace."
  value       = { for id, ns in kubernetes_namespace_v1.tenant : id => ns.metadata[0].name }
}
