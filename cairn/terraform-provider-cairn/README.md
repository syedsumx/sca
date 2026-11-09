# terraform-provider-cairn

Cairn resources in Terraform (or OpenTofu), managed through Cairn's API as the token's user, so Cairn's
permissions and audit apply:

<!-- BEGIN GENERATED RESOURCES -->

14 resources, each also a data source:

**Identity and access**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_policy` | `policies` | One of Cairn's own access policies (source cairn), set and deleted on its own: the rest of the policy bundle stays as it is |

**Data and pipelines**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_connection` | `connections` | An ingestion source: a connector, its form values (secret fields name a Cairn secret, never its value), where it runs and when |
| `cairn_maintenance_policy` | `maintenance` | Table maintenance (compaction, snapshot expiry, orphan cleanup) for *, a schema or schema.table |
| `cairn_pipe` | `pipes` | Files landing in a Cairn Store prefix, loaded into a lakehouse table once each |
| `cairn_stream` | `streams` | A Kafka topic (Debezium change events included) written into a lakehouse table, exactly once |
| `cairn_derived_table` | `derived` | A SELECT kept as a lakehouse table, refreshed on Trino as the token's user |

**SQL and BI**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_input_table` | `input_tables` | A lakehouse table people type into, written on Trino as the person writing |
| `cairn_saved_query` | `queries` | A saved SQL query (read-only), with its chart; the token user's own |
| `cairn_metric` | `metrics` | A business measure defined once (a workbook aggregate over one table), the same number in every query, workbook and dashboard; run as the person asking |
| `cairn_workbook` | `workbooks` | A spreadsheet workbook: elements compiled to SQL and run as the person looking |
| `cairn_dashboard` | `dashboards` | Saved queries shown together, each run as its viewer |
| `cairn_sql_alert` | `alerts` | A saved query checked on a schedule as its owner; its owner and recipients are told when the condition starts and stops holding |
| `cairn_sql_workflow` | `workflows` | Steps that depend on each other (derived-table refreshes, checks, report queries), on a schedule, run as their owner |

**AI and agents**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_vector_index` | `vectors` | Vector search over a lakehouse table's text, kept in step with the table and searched under Cairn's policies |

<!-- END GENERATED RESOURCES -->

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
