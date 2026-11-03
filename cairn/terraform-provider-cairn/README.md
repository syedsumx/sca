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

Each is also a data source, read by its name (a maintenance policy by its scope):
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
