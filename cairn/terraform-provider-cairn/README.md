# terraform-provider-cairn

Cairn resources in Terraform (or OpenTofu), managed through Cairn's API as the token's user, so Cairn's
permissions and audit apply:

| Resource | What it is |
|---|---|
| `cairn_pipe` | Files landing in a Cairn Store prefix, loaded into a lakehouse table once each |
| `cairn_stream` | A Kafka topic (Debezium change events included) written into a lakehouse table, exactly once |
| `cairn_derived_table` | A SELECT kept as a lakehouse table, refreshed on Trino as the token's user |
| `cairn_maintenance_policy` | Compaction, snapshot expiry and orphan cleanup for `*`, a schema or a table |
| `cairn_vector_index` | Vector search over a table's text, kept in step with it, searched under Cairn's policies |
| `cairn_saved_query` | A saved read-only query and its chart (`connection_name`, `chart = jsonencode({...})`) |
| `cairn_dashboard` | Saved queries shown together (`tiles = jsonencode([{query = cairn_saved_query.x.id}])`) |
| `cairn_sql_alert` | A saved query checked on a schedule, telling its owner when a condition starts and stops |
| `cairn_sql_workflow` | Steps that depend on each other (refreshes, checks, reports) on a cron schedule |
| `cairn_policy` | One of Cairn's own access policies, set on its own (`policy_id`, `effect`, `actions`, `target`, `condition`) |
| `cairn_connection` | An ingestion source: `connector`, `values` (secret fields name a Cairn secret), `schedule`, `executor` |

Warehouses are part of the deployment (Helm values `cairn.trino.warehouses`, or Terraform's `helm_release`);
who may use one is a `cairn_policy` granting `USE_WAREHOUSE` on `urn:li:warehouse:<name>`.

Each is also a data source, read by its name (a maintenance policy by its scope, a policy by its `policy_id`):
`data "cairn_saved_query" "q" { name = "weekly-revenue" }`. Saved queries, dashboards, alerts and
workflows are the token user's own; when others share one of the same name, the caller's own wins.
JSON attributes are compared by what you declare, so what Cairn adds (ids, default titles) is not drift.

```hcl
terraform {
  required_providers { cairn = { source = "cairn/cairn" } }
}
provider "cairn" {}   # CAIRN_URL and CAIRN_TOKEN (or url, token, ca_file)

resource "cairn_stream" "customers" {
  name   = "customers-cdc"
  topic  = "pg.public.customers"
  format = "debezium"
  key    = ["id"]
  target = "cdc.customers"
  create_table = true
}
```

Changes are made in place, so a stream keeps its offsets. Something deleted outside Terraform is created
again on the next apply. Import with the resource's id (for maintenance policies, the scope).

Build: `go build -o terraform-provider-cairn .`, then point Terraform at the directory with a
`dev_overrides` block for `cairn/cairn` (or publish it to your registry). Tested by
`tests/test_terraform_provider.py` with Terraform 1.13.

## Publishing

The Terraform Registry (and OpenTofu's) publishes providers from a public GitHub repository named
`terraform-provider-<name>` whose releases carry signed checksums. This folder holds everything for that
repository: `.goreleaser.yml`, `terraform-registry-manifest.json` and `.github/workflows/release.yml`.

1. Make the repository from this folder, with its history:
   `git subtree split --prefix=cairn/terraform-provider-cairn -b provider-release` in the Cairn repository,
   then push that branch as `main` of `<owner>/terraform-provider-cairn`.
2. Create a GPG key for signing; add its secret key and passphrase to that repository's secrets as
   `GPG_PRIVATE_KEY` and `PASSPHRASE`, and its public key to your registry account.
3. Sign in to registry.terraform.io with GitHub and publish the repository as a provider.
4. Tag a release there: `git tag v0.1.0 && git push --tags`. The workflow builds every platform, signs the
   checksums and makes the GitHub release the registry reads.

Users then write `source = "<owner>/cairn"`. The Cairn repository's CI checks `.goreleaser.yml` and runs
an unsigned snapshot release on every push, so a release tag does not find a broken build.
