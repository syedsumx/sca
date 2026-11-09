# terraform-provider-cairn

Cairn resources in Terraform (or OpenTofu), managed through Cairn's API as the token's user, so Cairn's
permissions and audit apply:

<!-- BEGIN GENERATED RESOURCES -->

54 resources, each also a data source:

**Identity and access**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_policy` | `policies` | One of Cairn's own access policies (source cairn), set and deleted on its own: the rest of the policy bundle stays as it is |
| `cairn_workspace` | `workspaces` | A workspace: domains run by their own admins, with members and the permissions each role has there |
| `cairn_persona` | `personas` | A persona: who is in it (people, groups, attribute rules), what it reaches (domains, platforms, tags...) and with which permission bundles; Cairn compiles it into policies |
| `cairn_purpose` | `purposes` | A purpose: data with these tags or classifications is used only by who is allowed, and masked for others |
| `cairn_consent_purpose` | `consent_purposes` | A consent purpose: rows of people who did not consent (opt-in) or who withdrew (opt-out) are filtered out for its members |
| `cairn_agent_identity` | `agent_identities` | A service principal: an agent identity with a key, acting as its owner within its scopes |
| `cairn_workload_binding` | `workload_bindings` | A workload identity binding: tokens from an issuer with this subject (and claims) act as a Cairn user |
| `cairn_secret_scope` | `secret_scopes` | A secret scope: secrets that notebooks, jobs and connectors read as the person running them |
| `cairn_secret_scope_acl` | `secret_scope_acls` | Who may use a secret scope: READ, WRITE or MANAGE for a person or a group |
| `cairn_secret` | `secrets` | One secret in a scope |

**Data and pipelines**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_connection` | `connections` | An ingestion source: a connector, its form values (secret fields name a Cairn secret, never its value), where it runs and when |
| `cairn_maintenance_policy` | `maintenance` | Table maintenance (compaction, snapshot expiry, orphan cleanup) for *, a schema or schema.table |
| `cairn_pipe` | `pipes` | Files landing in a Cairn Store prefix, loaded into a lakehouse table once each |
| `cairn_stream` | `streams` | A Kafka topic (Debezium change events included) written into a lakehouse table, exactly once |
| `cairn_medallion_layer` | `medallion_layers` | A table's medallion layer (bronze, silver or gold), shown in the catalog and used by quality and lineage |
| `cairn_parquet_table` | `parquet_tables` | Parquet files in a store prefix registered as a lakehouse table in place (no copy), refreshed as files land |
| `cairn_mirror` | `mirrors` | A database table mirrored into the lakehouse from its change log (PostgreSQL, MySQL, SQL Server, MongoDB) |
| `cairn_serving_table` | `serving_tables` | A lakehouse table kept in PostgreSQL for key lookups in milliseconds, under the table's policies |
| `cairn_document_source` | `document_sources` | A SharePoint or Google Drive folder copied into a store prefix on a schedule (its keys in a secret scope) |
| `cairn_derived_table` | `derived` | A SELECT kept as a lakehouse table, refreshed on Trino as the token's user |
| `cairn_saas_sync` | `saas_syncs` | A SaaS object (Salesforce, HubSpot, Google Sheets) loaded into a lakehouse table on a schedule |
| `cairn_reverse_sync` | `reverse_syncs` | Reverse ETL: a governed table written out to a SaaS app's objects, matched by a key, on a schedule |
| `cairn_volume` | `volumes` | A governed volume: files under Cairn's grants, in a Cairn Store bucket of its own |
| `cairn_volume_grant` | `volume_grants` | Read or write access to a volume for a person or a group |
| `cairn_external_catalog` | `external_catalogs` | Another organisation's Iceberg REST catalog, read (and optionally written) under Cairn's policies |

**SQL and BI**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_input_table` | `input_tables` | A lakehouse table people type into, written on Trino as the person writing |
| `cairn_saved_query` | `queries` | A saved SQL query (read-only), with its chart; the token user's own |
| `cairn_sql_folder` | `sql_folders` | A folder of saved queries, dashboards and workbooks; the token user's own |
| `cairn_metric` | `metrics` | A business measure defined once (a workbook aggregate over one table), the same number in every query, workbook and dashboard; run as the person asking |
| `cairn_procedure` | `procedures` | A stored procedure: SQL statements with typed parameters, called by people allowed to (CALL_PROCEDURE) |
| `cairn_workbook` | `workbooks` | A spreadsheet workbook: elements compiled to SQL and run as the person looking |
| `cairn_dashboard` | `dashboards` | Saved queries shown together, each run as its viewer |
| `cairn_sql_alert` | `alerts` | A saved query checked on a schedule as its owner; its owner and recipients are told when the condition starts and stops holding |
| `cairn_metric_alert` | `metric_alerts` | An alert on a metric: a change, a range or a threshold, checked on a schedule as its owner |
| `cairn_report_schedule` | `report_schedules` | A dashboard or workbook sent as CSV or PDF on a schedule to its owner and recipients |
| `cairn_sql_workflow` | `workflows` | Steps that depend on each other (derived-table refreshes, checks, report queries), on a schedule, run as their owner |

**Quality and governance**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_quality_monitor` | `quality_monitors` | A data quality monitor on a dataset (freshness, volume, field metrics, schema or SQL), on a schedule |
| `cairn_slo` | `slos` | A data product's service level objective: the share of its monitors' evaluations that must pass |
| `cairn_retention_policy` | `retention_policies` | How long data with a label (tag, term or classification) is kept; with enforce, older rows are deleted |
| `cairn_automation` | `automations` | A governance automation: assets that match a condition get actions (tags, owners, domains, ...), on a schedule or on events |
| `cairn_form` | `forms` | A governance form: questions asked of the owners of the assets that match a condition |
| `cairn_budget` | `budgets` | A cost budget for a scope (all, a domain, an owner, a platform, tables, a warehouse or AI), with alerts at thresholds and on the forecast |
| `cairn_cost_centre` | `cost_centres` | A chargeback cost centre: the teams (groups) whose use it pays for |
| `cairn_notification_route` | `notification_routes` | A notification route: events of these types (in these domains) to Slack, Teams, a webhook, a ticketing system, Kafka, EventBridge or Pub/Sub |
| `cairn_marketplace_listing` | `marketplace_listings` | An agent or skill listed in the marketplace, with who approves access requests and for which purposes |

**Compute and jobs**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_job` | `jobs` | A scheduled job: a notebook or a Python script, run as its owner on a cron schedule (or on demand) |

**AI and agents**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_vector_index` | `vectors` | Vector search over a lakehouse table's text, kept in step with the table and searched under Cairn's policies |
| `cairn_custom_agent` | `custom_agents` | An agent built in Cairn: instructions, tools and scopes, run as the person asking (or deployed) |
| `cairn_ai_gateway_route` | `ai_gateway_routes` | A virtual model on the AI gateway: candidates in order (or by cost or latency), with fallbacks |
| `cairn_model_deployment` | `model_deployments` | A registered model's version served behind Cairn, scaled between min and max replicas |
| `cairn_agent_abuse_settings` | `agent_abuse_settings` | Agentic abuse detection: whether it blocks, at which severity, and when it pauses an agent |
| `cairn_agent_intent_settings` | `agent_intent_settings` | Intent-based security for agents: whether every agent request must declare an intent |

**Workspace**

| Resource | Bundle section | What it is |
|---|---|---|
| `cairn_git_folder` | `git_folders` | A git folder: a repository's branch and path in the token user's workspace (its notebooks and queries) |
| `cairn_data_app` | `data_apps` | A data app: one Cairn starts (a command, from git or on Kubernetes) or one running elsewhere (a URL), reached only through Cairn |

<!-- END GENERATED RESOURCES -->

The list and the provider's resource table (`internal/provider/zz_generated_kinds.go`) are generated from Cairn's
resource registry (`src/cairn/iac/registry.py` and `kinds.py` in the Cairn repository) by
`python -m cairn.iac.terraform`; Cairn's CI fails when they are stale. One engine (`resource.go`) reads and writes
them all. A few rules hold for every type:

- **Children** live under a parent: a secret's, a secret scope access entry's and a volume grant's id is
  `<parent>/<id>` (`terraform import cairn_secret.db finance-db/password`).
- **Write-only values.** A secret's `value` is a write-only attribute (Terraform 1.11 or later): sent to Cairn,
  never stored in the state, never read back. Raise `value_version` to send it again.
- **Settings that always exist** (`cairn_agent_abuse_settings`, `cairn_agent_intent_settings`): destroying one
  only removes it from the state.
- **Created once.** A type Cairn cannot change in place (a secret scope, a mirror, a serving table, a workload
  binding) is replaced when any of its attributes changes.
- **Import** takes what Cairn holds, so a plan right after an import shows no change. `cairn export --format
  terraform` writes the resource and `import` blocks of a whole workspace.

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
