# The licence as code: `terraform test` in modules/cairn plans the module with mocked providers (no cluster) and checks
# that a licence becomes the Secret the chart reads, named in the release's values (CI: job terraform-modules).

mock_provider "helm" {}
mock_provider "kubernetes" {}

run "no_licence_no_secret" {
  command = plan
  assert {
    condition     = length(kubernetes_secret_v1.licence) == 0
    error_message = "without a licence there is no licence Secret"
  }
  assert {
    condition     = length(helm_release.cairn.values) == 1
    error_message = "without a licence the release has only its own values"
  }
}

run "licence_becomes_the_chart_s_secret" {
  command = plan
  variables {
    licence           = "cairn-licence-text"
    licence_required  = true
    licence_tenant_id = "acme"
  }
  assert {
    condition     = kubernetes_secret_v1.licence[0].metadata[0].name == "cairn-licence"
    error_message = "the licence Secret is <release>-licence"
  }
  assert {
    condition     = nonsensitive(kubernetes_secret_v1.licence[0].data["licence"]) == "cairn-licence-text"
    error_message = "the Secret holds the licence under the key the chart reads"
  }
  assert {
    condition = yamldecode(helm_release.cairn.values[1]).cairn.licence == {
      existingSecret = "cairn-licence", secretKey = "licence", required = true, tenantId = "acme"
    }
    error_message = "the release reads the licence from that Secret"
  }
}

run "licence_from_a_file" {
  command = plan
  variables {
    licence_file = "tests/licence.txt"
  }
  assert {
    condition     = nonsensitive(kubernetes_secret_v1.licence[0].data["licence"]) == "licence-from-a-file\n"
    error_message = "licence_file is read into the Secret"
  }
}

run "not_both" {
  command = plan
  variables {
    licence      = "x"
    licence_file = "tests/licence.txt"
  }
  expect_failures = [kubernetes_secret_v1.licence]
}

run "a_tenant_s_licence" {
  command = plan
  variables {
    tenants         = { acme = {} }
    tenant_licences = { acme = "acme-licence" }
  }
  assert {
    condition     = kubernetes_secret_v1.tenant_licence["acme"].metadata[0].name == "cairn-licence"
    error_message = "the tenant's licence is the Secret cairn-licence in its namespace"
  }
  assert {
    condition = yamldecode(helm_release.tenant["acme"].values[2]).cairn.licence == {
      existingSecret = "cairn-licence", secretKey = "licence", required = true, tenantId = "acme"
    }
    error_message = "the tenant's release requires its licence, bound to its id"
  }
}
