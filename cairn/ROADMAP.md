# Cairn Roadmap

**Goal.** Cairn is the one data platform an enterprise runs, the way teams use Snowflake or Databricks. It covers storage, tables, SQL, transformation, orchestration, Python and Spark, governance, discovery, quality and AI. It is built from open-source engines and runs in the customer's own network. Every engine signs in as a Cairn identity and is governed by the same Cairn policy.

Principles every item is held to:

- **One identity.** People sign in once (SSO, SCIM). Pipelines run as pipeline users with personal access tokens, exchanged for short-lived, audience-bound tokens per engine: `trino`, `lakehouse`, `airflow`. No engine holds a database password or a store key.
- **One policy.** Cairn's policy decision point decides for every engine: Trino, Cairn Catalog (Spark, pyiceberg), the metadata APIs and the AI tools. Masks and row filters apply wherever data is read. Where an engine cannot apply them, it is refused, never trusted.
- **Fail closed.** If a decision cannot be made, access is refused, and so is a deployment configured unsafely (for example, a catalog vending store keys alongside Cairn-issued tokens). Every change is one transaction with its audit entry.
- **Tested for real.** Each capability has end-to-end tests against the real binaries and the images as shipped, run in CI.

Status: ✅ shipped and tested end to end in CI · 🟡 shipped, partly validated · ⬜ planned.

## The platform today

| Layer | Snowflake / Databricks | Cairn | Status |
|---|---|---|---|
| Object storage | Managed storage / cloud object stores | **Cairn Store** (Rust, S3 API). Clusters with replicas, erasure coding and racks/zones; versioning and object lock; SSE, with customer-managed keys in Vault Transit; STS temporary credentials; lifecycle and tiering; backups, continuous backup and two-way sync; WebDAV, SFTP, FUSE, CSI and Hadoop S3A; node TLS | ✅ |
| Tables and catalog | Snowflake Iceberg tables / Unity Catalog | **Cairn Catalog** (Rust, Iceberg REST). Tables and views; Delta Lake tables through Trino, registered by location, several writers at once, converted to Iceberg in place; commits via iceberg-rust; per-table credential vending; HA on PostgreSQL; Cairn identities governed per table | ✅ |
| SQL warehouse | Virtual warehouses / Databricks SQL | **Trino 476** on the lakehouse. Per-team warehouses that stop when idle and start on the first query (`USE_WAREHOUSE`); Cairn sign-in (JWT, OAuth2 SSO for the web UI and BI); access control through Cairn's policies (OPA protocol); query lineage and usage into Cairn; resource groups; fault-tolerant execution, spill and cache; workers with autoscaling | ✅ |
| SQL in the product | Snowsight / SQL editor | Data preview and SQL editor on Trino, with autocomplete from the tables the person may query; AI functions (`USE_AI_FUNCTIONS`) and inline Python functions in SQL; Ask-your-data (text-to-SQL) with the caller's permissions | ✅ |
| Transformation | dbt Cloud on Snowflake / Databricks | **dbt-core 1.12** with dbt-trino (`cairn dbt`, `cairn/dbt`). Runs as a pipeline user; docs, tags, owners, lineage and tests recorded in Cairn; Helm CronJobs. Derived tables: a SELECT kept as a table, refreshed fully, by watermark or by merge, on a schedule or when what it reads changes, as its owner | ✅ |
| Orchestration | Tasks / Databricks Workflows | **Apache Airflow 3.3** (`cairn/airflow`, `cairn-airflow-provider`). Cairn operators for Trino and dbt; OpenLineage to Cairn; UI sign-in through Cairn with group → role mapping; official chart values | ✅ |
| Python and Spark | Snowpark / Databricks Spark | **Spark 4.0** with Iceberg 1.10 (`cairn/spark`, `cairn-spark`). Signed in as a Cairn identity; each table decided by Cairn; per-table credentials, read-only unless writable; column lineage to Cairn | ✅ |
| Governance | Horizon / Unity Catalog | Policy decision point (RBAC with ABAC, deny-overrides, obligations), masking on every read path, row filters, personas and purposes, PII classification and propagation, certification workflows, compliance forms, hash-chained audit log | ✅ |
| Discovery and metadata | Horizon catalog / Unity Catalog explorer | Cairn Core: search, search by meaning, lineage (column level), glossary, domains, data products, change history, bulk editing, workspaces, i18n | ✅ |
| Data quality | Data metric functions / Lakehouse Monitoring | Monitors (volume, freshness, schema, drift, learned thresholds), data contracts, profiling, incidents, results from dbt and other tools | ✅ |
| AI | Cortex / Mosaic AI | Assistant, documentation drafts, context agents, governed metrics (semantic layer), MCP server, AI-system governance, usage and budgets; Claude or local models | ✅ |
| Sharing and marketplace | Listings / Delta Sharing | Internal marketplace with access requests; publishing to Snowflake listings and Delta Sharing shares | ✅ |
| Ingestion | Connectors / Lakeflow Connect | Source connectors and in-VPC remote agents (signed, sealed); the store's message queues and events; pipes (files landing in the store, loaded once each); streams (Kafka and Debezium CDC into Iceberg, exactly once) | ✅ |
| Operations | Managed service | Compose and Helm (HA, network policies, PDBs); infrastructure as code (`cairn bundle` from git, a Terraform provider); SSO and SCIM; Alembic migrations; signed images with SBOMs; monitoring dashboards; hosting tenants, with a ready Hetzner setup (`deploy/hetzner`: k3s on dedicated servers, shared PostgreSQL with backups, per-tenant search and Kafka, tenant isolation, tested in CI) | ✅ |

## Design decisions behind the lakehouse integration

- **Restricted tables are refused to engines that read files.** A table with a mask or row filter for a user is not handed to Spark, pyiceberg or DuckDB, because its data files hold what the mask or filter hides. Those users query it through Trino, which applies the policy, or through a view. Spark reads such tables through Trino too (`governed_read`, item L2 below).
- **Airflow 3.3, not 3.0.** 3.3 is the current stable 3.x release, with the fixes 3.0 lacked. Providers are pinned by Airflow's constraints. dbt runs in its own virtualenv in the image, so its dependencies never meet Airflow's.
- **Short-lived tokens, never secrets in projects.** dbt, Airflow and Spark exchange the pipeline user's token for an engine token at run time. Tokens default to 4 hours and are capped at 12. Issuing one is audited.
- **One dataset per table, across engines.** Lineage from Trino, dbt, Airflow and Spark lands on the dataset names the catalog crawler uses. Spark's storage-path names are renamed through the OpenLineage symlinks facet.
- **Opt-in images.** The dbt, Airflow and Spark images are built on request (`ONLY="dbt airflow spark"`). The core platform stays small.

## Next

Ordered by what an enterprise needs first to retire a Snowflake or Databricks contract. Each item has a definition of done that is tested end to end.

| # | Item | Definition of done | Status |
|---|---|---|---|
| L1 | **Table maintenance service.** Snapshot expiry, orphan-file removal, data-file compaction and manifest rewrites per table, on a schedule and on demand, with retention as a table property | A table with 1,000 small files and 100 snapshots is compacted and expired on schedule. Time travel within retention still works, orphaned files are gone, and every action is audited | ✅ policies per table, schema or default; compaction, snapshot expiry, orphan removal on a schedule; automatic clustering: sort columns chosen from what the table's queries filter on (Trino's query history), the sort order set and the files rewritten when they overlap (`tests/test_trino_e2e.py`) |
| L2 | **Governed Spark on restricted tables.** A policy-enforcing scan path for masks and row filters (Spark Connect on trusted compute, or a Cairn-planned scan through Trino), so Spark users are no longer refused | `ana` reads a masked table in Spark and sees masked values. `bob`'s row filter returns only his rows. Neither can reach the raw files | ✅ a Cairn-planned scan through Trino: `cairn_spark.governed_read` reads as the user, with their masks and row filters, partitioned; the catalog still refuses the files (`tests/test_spark_e2e.py`) |
| L3 | **Spark on Kubernetes.** Cluster-mode submission and Spark Operator `SparkApplication`s with no secret in the cluster (the driver signs in by workload identity), `CairnSparkApplicationOperator` for Airflow, driver RBAC and a history server in the chart; then per-team namespaces with quotas | An Airflow DAG submits a Spark job to the cluster as a pipeline user. Its lineage and run appear in Cairn, and the job is refused a table outside its grants | ✅ submission, operator, Airflow task and history server (`tests/test_spark_k8s_e2e.py`); on a full cluster in CI (kind): a cluster-mode job's driver pod signs in with its projected service-account token, starts an executor pod, reads a governed table and is refused another (`tests/test_spark_kind_e2e.py`); per-team quotas (`tests/test_spark_teams_k8s.py`) |
| L4 | **Notebooks.** JupyterHub with Cairn sign-in; kernels with `cairn-spark` and Trino as the signed-in person; notebooks as catalog assets | A person signs in through Cairn, queries Trino and Spark from a notebook under their own policies, and the notebook appears in Cairn with lineage | ✅ JupyterHub signs people in through Cairn (OAuth client `jupyterhub`); each person's server gets a Cairn token of their own, suspended with them; `%%sql` on Trino and `%cairn spark` as the person; saved notebooks become catalog assets owned by them with lineage from their `%%sql` tables (`cairn_lakehouse.jupyter`, `helm/jupyterhub/values-cairn.yaml`, `tests/test_jupyterhub_e2e.py` with a real JupyterHub) |
| L5 | **Streaming and CDC ingestion.** Kafka → Iceberg (Kafka Connect Iceberg sink) and Debezium CDC as governed pipelines, with exactly-once commits and schema evolution | A PostgreSQL table's changes arrive in an Iceberg table within a minute, with lineage from the source and quality monitors on freshness | ✅ Kafka JSON and Debezium CDC into Iceberg (`cairn streams`): upserts and deletes, offsets in the table snapshots for exactly once, rejects, schema evolution, source lineage (`tests/test_streams_e2e.py`) |
| L6 | **Workload management and chargeback.** Trino resource groups and Spark quotas per team, with costs from Trino and Spark query statistics fed into the cost report automatically | Two teams' queries are isolated under load, and each team's compute and storage costs appear per day without manual posting | 🟡 resource groups and cost report exist; Spark compute is priced per application from its runs (`CAIRN_COST_SPARK_USD_PER_CORE_HOUR`); team warehouses scale to zero and their workers follow the load, with node-hours priced in the cost report (`tests/test_warehouses_scaling_e2e.py`); cost budgets per domain, owner, platform, tables, warehouse or AI, with threshold and forecast alerts once a period (`tests/test_budgets.py`); per-query Trino costs on tables are planned |
| L7 | **Machine learning.** MLflow tracking and model registry behind Cairn sign-in; models and features as catalog assets, with lineage from training tables | A model trained in Spark is registered as a pipeline user. Cairn shows its lineage to the training tables, and access to the model follows Cairn's policy | 🟡 MLflow behind Cairn sign-in (`/v1/ml`): runs attributed to the caller, an owned and governed registry, versions in the catalog with lineage from training tables, serving through Cairn with `USE_ML_MODEL` (`tests/test_ml_e2e.py`, trained with scikit-learn); feature tables as catalog assets with their lineage, declared by the table's owners (`tests/test_feature_tables.py`); an online feature store: feature tables served by entity key, kept in step with the table's snapshots, each lookup governed as a read (masks as Trino applies them, row-filtered callers refused) (`tests/test_online_features_e2e.py`); model monitoring: every served call logged, drift (PSI) against the training table or a reference period, accuracy from labels, alerts on drift, payloads for the model's managers only (`tests/test_ml_e2e.py`, `tests/test_model_monitoring_e2e.py`); Spark training and serving deployments in the chart to do |
| L8 | **Open data sharing.** Iceberg REST sharing to external recipients, with recipient identities, expiry and revocation in Cairn | A recipient in another organisation reads a shared table with pyiceberg. Revoking the share stops access at once | 🟡 shares through Iceberg REST: recipient credential, only the shared tables, read-only scoped keys, expiry, revocation at once, never a masked or filtered table (`/v1/shares`, Pipelines → Shares, `tests/test_shares_e2e.py`); publishing to Snowflake and Delta Sharing; clean rooms: approved aggregate templates only, k-anonymous results, the room's own Trino identity reading only its tables (`/v1/clean-rooms`, `tests/test_trino_e2e.py`); differential privacy and rooms with outside parties to do |
| L9 | **Disaster recovery across regions.** Store replication, catalog and Cairn database replicas, and a tested failover runbook with measured recovery point and recovery time objectives | A region is lost in a test. The standby serves the lakehouse with the recorded RPO and RTO | ✅ standby store by bucket replication, standby PostgreSQL by streaming, a failover runbook and `cairn dr reconcile` (tables whose newest files never arrived move back to their newest whole commit); drill in CI with the primary region killed: RPO 4 of 10 commits (4.3 s), RTO about 2 s (`tests/test_dr_drill.py`); automated DNS failover and Cairn's own database in the drill to do |
| L10 | **Scale and assurance.** Load tests: PDP p99 under 5 ms cached at 2k rps, catalog decisions on 1k-table jobs, store throughput. Chaos tests. An external penetration test. KMS or HSM for gateway keys | Published results in CI, and pentest findings closed | 🟡 in CI: cached decisions p99 0.04 ms at about 75k/s and a 1,000-table catalog decision in 0.65 s (`tests/test_scale_decisions.py`, after indexing policies by privilege: 3x faster); chaos with a replica killed mid-commit, the database restarted and the store crashed, nothing acknowledged lost or doubled (`catalog/conformance/test_chaos.py`); every route refused to strangers but a reviewed public list, security headers (`tests/test_route_security.py`); KMS, Key Vault, Cloud KMS and KMIP HSMs for store keys (L22). An external penetration test, store throughput at scale and a KMS or HSM for gateway keys to do |
| L11 | **Upgrade matrix.** Pin-and-test against new Trino, Iceberg, Spark, dbt and Airflow releases | A version bump is merged only after CI passes with the new engine version | ✅ `deploy/versions/pins.json` (checked against every file that pins), a weekly workflow that finds each engine's newest release and runs the matching end-to-end suites on it (`cairn-upgrade-matrix.yml`); tried here: Trino 484 needed two renamed settings, then passed all 21 Trino tests |
| L12 | **Zero-copy clones, undrop, branches and tags.** Clone a table without copying its data; bring back a dropped table; write-audit-publish on Iceberg branches | A clone reads the source's files and writes its own; neither can expire what they share; a dropped table comes back within the retention, a purged one does not | ✅ `POST …/tables/{t}/clone`, `…/undrop`, `GET …/dropped` in Cairn Catalog; branches and tags kept by the catalog, tested with pyiceberg (`catalog/conformance/test_clones.py`) |
| L13 | **HTTP streaming ingestion.** Producers post rows into lakehouse tables without Kafka, exactly once | A producer that retries a request writes nothing twice and resumes where the table says | ✅ http streams: `POST /v1/ingest/<name>/rows` with per-channel offsets in the table's snapshots, `INGEST_STREAM` (`tests/test_ingest_http_e2e.py`) |
| L14 | **Query insight.** Query history, profiles and tuning hints | A person sees their own slow queries with why they were slow; operators see all | ✅ `/v1/queries` from Trino's events: per-query statistics and SQL for its author and operators, hints for queueing, wide or unfiltered reads, cross joins, unbounded sorts, memory and failures (`tests/test_trino_e2e.py`); a Query insight page with filters and profiles (`tests/test_sql_ops_ui_e2e.py`, in Chromium) |
| L15 | **Files as data.** Directory tables and document text extraction | A folder of documents is a table with each file's text, kept current as files change | ✅ files pipes: one row per object with its metadata and the text of text, HTML, PDF, Word and PowerPoint files, updated on change and removed on delete (`tests/test_pipes_e2e.py`); OCR of scanned documents to do |
| L16 | **Vector search on lakehouse data.** Search a table's text by meaning, governed | A question finds the right rows; an index follows its table; only readers search it | ✅ vector indexes: chunks embedded with the deployment's model into `<table>__<index>_vectors`, one per index, refreshed incrementally, searched with READ on the source and never past a mask or row filter (`tests/test_vectors_e2e.py`); approximate (ANN) indexes for very large tables to do |
| L17 | **Scheduled SQL and in-product workflows.** Steps that depend on each other, on a schedule, as their owner | A nightly refresh, a check and a report run in order; a failed check stops what follows and tells the owner | ✅ SQL workflows (`/v1/sql-workflows`): derived refreshes, checks and reports in a DAG on cron, with retries, run history and notifications (`tests/test_sql_workspace.py`, `tests/test_trino_e2e.py`); a SQL workflows page with a step editor, Run now and run history (`tests/test_sql_ops_ui_e2e.py`, in Chromium) |
| L18 | **Infrastructure as code for SQL and search.** Saved queries, dashboards, alerts, SQL workflows and vector indexes in bundles and Terraform; Terraform data sources | A team keeps its reports in git: a plan shows only real changes, and references between them stay right | ✅ bundle kinds and Terraform resources for all five, references by name (bundles) or id (Terraform), subset drift checks, one data source per type (`tests/test_bundles.py`, `tests/test_terraform_provider.py` with Terraform 1.13); policies (one at a time, serialized, audited) and connections as bundle kinds and Terraform resources, with data sources (`tests/test_bundles.py`, `tests/test_policy_items.py`, `tests/test_terraform_provider.py`); warehouses stay deployment config, their access a policy; a registry-ready release (GoReleaser, signed checksums, manifest) checked in CI. Publishing to the public registry needs the provider's own repository and a GPG key |
| L19 | **Delta Lake beyond one writer, and into Iceberg.** Concurrent Delta writers on Cairn Store; Delta tables converted to Iceberg without copying | Two clusters write one Delta table at once and lose nothing; a Delta table becomes an Iceberg table in place | ✅ conditional-write commits (`s3.exclusive-create`, checked atomically by Cairn Store), proven with two Trino clusters; in-place conversion from the Delta log (checkpoints and commits), refusing partitioned tables, deletion vectors and column mapping (`tests/test_trino_e2e.py`); converting partitioned tables and deletion vectors in place to do |
| L20 | **Git-backed SQL and notebooks.** Saved queries and notebooks kept in a git repository, pulled and pushed by their owner | A team reviews its queries in pull requests; a merged change reaches Cairn, and an edit made in Cairn is proposed back | ✅ Git folders over the `git` command (any host): pull to saved queries and catalog notebooks, conflicts reported rather than overwritten, push to a new branch with a pull request for configured hosts (`tests/test_git_folders.py`); a Git folders page to link, pull (conflicts shown) and push (`tests/test_sql_ops_ui_e2e.py`, in Chromium); notebooks round-trip through JupyterHub: a folder's notebooks are synced into each owner's server, saves there are pushed (outputs cleared), conflicts reported and local work never overwritten (`tests/test_jupyterhub_e2e.py`, real JupyterHub and git) |
| L21 | **Stored functions and procedures.** SQL and Python functions kept in a catalog; parameterized procedures | A team shares a function everyone may call under policy; an operator gives a few people a procedure for a controlled change | ✅ stored functions in a `routines` catalog, `USE_FUNCTION` per function with masks applied first; procedures with typed, bound parameters run as the caller, `EXECUTE_PROCEDURE` per procedure (`tests/test_trino_e2e.py`); owner's-rights procedures deliberately not offered; functions as catalog assets: published from Trino's events (creator as owner, signatures, body, comment, overloads and drops), a sync for what was missed, the policy's function urn on the asset (`tests/test_trino_e2e.py`, `tests/test_trino_functions.py`) |
| L22 | **Customer keys beyond Vault.** Customer-managed keys in AWS KMS, Azure Key Vault, Google Cloud KMS and KMIP HSMs for Cairn Store | A customer disables their key and their objects can no longer be read | ✅ `aws-kms` external keys: SigV4, the object's name as the encryption context, rotated credentials picked up (`store/conformance/test_kms_aws.py` against a real KMS-compatible server). ✅ `azure-keyvault` (RSA-OAEP-256 wrap, the wrapping version kept, client secret, workload identity, managed identity), `gcp-kms` (the object's name as AAD, signed service-account sign-in or the metadata server) and `kmip` (KMIP 1.4 over mutual TLS, AES-GCM in the HSM), tested against Lowkey Vault, floci-gcp and PyKMIP's server, with a disabled version, a key the object was not written with, a refused certificate and a revoked key (`store/conformance/test_kms_cloud.py`) |
| L23 | **Data apps.** Streamlit and other web apps served by Cairn, each viewer signed in and governed as themselves | A team ships an app; people open it with their Cairn sign-in and see only their own data | ✅ `/apps/<name>/` proxy (HTTP and WebSocket), `USE_APP` per app, the viewer's identity and a Trino token for them passed to the app, forged headers stripped, apps started on demand and stopped when idle (`tests/test_data_apps_e2e.py`, Streamlit in Chromium); apps from git at a deployed commit (`tests/test_data_apps_git.py`); a Kubernetes runtime: a Deployment per app at the deployed commit, started on a visit and stopped when idle, locked-down pods reached only by Cairn, the chart's namespace Role (`tests/test_data_apps_k8s.py` on kind) |
| L24 | **Materialized views.** A query's result kept as a table and refreshed when what it reads changes, written in SQL | An analyst writes `CREATE MATERIALIZED VIEW`; any write to its source, from any engine, refreshes it, and it says when it is stale | ✅ `CREATE`, `REFRESH` and `DROP MATERIALIZED VIEW` in the SQL editor, refreshed on change as their owner, fresh/stale with what changed; Cairn Catalog reports every commit, so pyiceberg, Spark and Trino writes all count (`tests/test_trino_e2e.py`, `tests/test_lakehouse_client_e2e.py`) |
| L25 | **Change data feed.** A table's row changes since a snapshot, for consumers that sync or react | A downstream system reads only what changed since it last looked, seeing what its reader may see | ✅ `GET /v1/lakehouse/tables/<schema>/<table>/changes` (inserts, deletes, updates by key, the next cursor) and `.../snapshots`, read as the caller so masks and row filters apply; Iceberg metadata tables decided on their table (value-bearing ones for its managers), and connector table functions that bypass masks (`table_changes`, JDBC `query`) for operators only (`tests/test_trino_e2e.py`, `tests/test_trino_access.py`) |
| L26 | **SIEM export.** The audit log pushed to Splunk HEC, syslog (RFC 5424 and CEF, over TLS) and HTTPS sinks as it is written | A security team's SIEM holds every audit entry in order and can verify the chain; a sink that is down loses nothing | ✅ at-least-once, ordered delivery with a cursor per sink, backoff, lease and rewind; tokens from files; tested against Vector as the receiver (`tests/test_siem_e2e.py`) |
| L27 | **Signup and billing.** Self-service signup, Stripe Checkout and a tenant whose licence follows its subscription | A team signs up, pays and gets its workspace; renewals extend its licence, a cancelled one turns read-only after its grace with its data kept | ✅ email confirmation, Checkout, signed and idempotent webhooks, provisioning through `tenant.sh` with retries, renewals and plan changes reissuing the licence, the billing portal by emailed link (`tests/test_signup_e2e.py`, against stripe-mock and Mailpit) |
| L28 | **Usage metering.** Tenants report their daily usage; the control plane bills it through Stripe's meters | A tenant's Trino CPU, warehouse time and AI spend appear on its invoice, each day billed once | ✅ signed daily reports with per-tenant keys derived on the control plane, delivered by `tenant.sh --metering-key` and the chart; settled days sent as Stripe meter events with idempotent identifiers (`tests/test_metering_e2e.py`, against stripe-mock) |
| L29 | **Status page.** Components probed, 90-day uptime, incidents and maintenance, public | Customers see when a part of the platform is down or degraded, and what operators say about it | ✅ probes with operational/degraded/outage, daily uptime, incidents with updates and scheduled maintenance, a script-free page, JSON and an Atom feed (`tests/test_status_page_e2e.py`, against a real Cairn Store stopped and restarted) |
| L30 | **Gateway API.** HTTPRoutes (and optionally the Gateway) instead of an Ingress, for tenants too | A cluster without ingress-nginx serves Cairn through its Gateway with the same paths | ✅ `core.gateway` in the chart, the same routing as the Ingress (prefix rewrites, X-Forwarded-Prefix, timeouts, consoles blocked), validated against the Gateway API schemas; `tenant.sh` on a shared Gateway (`CAIRN_GATEWAY`); the end-to-end check through Envoy Gateway on kind (`tests/test_gateway_api_kind.py`) runs in CI's kind job |
| L31 | **BI: spreadsheet workbooks.** Explore a table like a spreadsheet: formulas, filters, grouping, pivots, compiled to SQL and run as the viewer | An analyst builds revenue by region and quarter without SQL; a colleague with masked columns sees them masked | ✅ the formula language compiled safely to Trino SQL, workbook elements with calculated columns, row and group filters, aggregates and pivots, saved and shared workbooks, the Workbooks page (`tests/test_workbooks.py`, `tests/test_trino_e2e.py`, `tests/test_sql_ops_ui_e2e.py`) |
| L32 | **BI: input tables (writeback).** Tables people type into, written on Trino as the person and read back governed | Finance types quarterly targets next to actuals; a workbook compares them at once; a stale edit never overwrites a colleague's | ✅ create, add, edit and delete rows as the person (Trino asks Cairn for each statement), typed literals, optimistic concurrency on `_updated_at`, catalog registration, audit, the Input tables page (`tests/test_trino_e2e.py`, `tests/test_input_tables.py`, `inputTables.test.tsx`) |
| L33 | **BI: interactive dashboards.** Shared filter controls, cross-filtering, drill-down, workbook tiles and more chart types | A sales lead picks EU and a date range, clicks a region to filter the orders, drills from region to country; each viewer still sees only their rows | ✅ values, range, date range and text controls (choices from the data as the viewer sees it), cross-filter on click, drill paths on workbook tiles, stacked bar, area, scatter, pie and number charts, all run as the viewer (`tests/test_sql_workspace.py`, `tests/test_trino_e2e.py`, `dashboards.test.tsx`, `tests/test_sql_ops_ui_e2e.py`) |
| L34 | **BI: metrics layer.** Named metrics (measure, dimensions, filters, time grain) defined once and used in workbooks, dashboards and Ask-your-data | "Revenue" means the same in every tile and answer; changing its definition changes them all | ✅ metrics with their own filters, time column and allowed dimensions, compiled to one workbook element and run as the viewer; metric tiles on dashboards; owner-only changes, operator certification withdrawn on change, versions, audit; the Metrics page (`tests/test_workbooks.py`, `tests/test_trino_e2e.py`, `metrics.test.tsx`). Ask-your-data using metrics comes with the AI builder (L35) |
| L35 | **BI: AI builder.** The assistant builds workbooks and dashboards from a question, using metrics and governed columns | "Revenue by region this quarter, with a filter on country" becomes a saved dashboard the person may see | ✅ a question becomes a dashboard of metric and workbook tiles and controls proposed as structured output (no model-written SQL), each tile compiled as the person, refusals fed back once, a governed preview, saved as the person's own; Ask-your-data given the metrics layer's definitions (`tests/test_trino_e2e.py`, `dashboards.test.tsx`); 🟡 not yet evaluated with a live model |
| L36 | **BI: app actions.** Forms and buttons on dashboards that write to input tables, call an API or run a workflow, as the person | An approver clicks Approve on a row; the input table changes and a workflow runs, audited | ✅ forms and buttons that add or change input-table rows as the person clicking (Trino decides), run workflows only for those the owner allows, and post to operator-allowed APIs signed; every run kept and audited; buttons, forms and row picking on dashboards (`tests/test_trino_e2e.py`, `dashboards.test.tsx`) |
| L37 | **BI: scheduled exports and a result cache.** Dashboards and workbooks sent as PDF/CSV on a schedule; results cached per viewer's policy and invalidated when tables change | A Monday email with each recipient's own view; a second viewer with the same policy gets the cached result | ✅ a result cache shared only between people whose decisions on every table read are the same, dropped on a table change or after a time, audited; CSV and PDF exports on a schedule rendered as each recipient, kept for them alone, emailed as attachments; Schedule export on dashboards (`tests/test_trino_e2e.py`, `tests/test_reports.py`, `dashboards.test.tsx`) |
| L38 | **BI assets in the catalog.** Workbooks, dashboards and input tables as catalog entities with lineage from the tables they read | Dropping a column shows which dashboards break, before it is dropped | ⬜ |
| L39 | **BI version history.** Every save of a workbook or dashboard kept; compare and restore | A broken edit is rolled back to yesterday's version | ⬜ |
| L40 | **Embedded dashboards.** Dashboards embedded with signed, origin-bound tokens, running as the embedding user | A portal shows a dashboard with that user's row filters | ⬜ |
| L41 | **BI as code.** Workbooks, dashboards and input tables in `cairn bundle` and the Terraform provider | A dashboard deployed from git to staging and production | ⬜ |
| L42 | **Input-table approvals.** Optional review before changes to an input table land | A change to targets waits for a reviewer; approved, it is written as its author | ⬜ |
| L43 | **Partition-aware volume monitors.** Volume and freshness per partition | A missing day in a partitioned table alerts even when the table total looks normal | ⬜ |
| L44 | **Remote agent hardening.** Key rotation by signed re-enrolment and an mTLS option between agent and gateway | An agent rotates its key without downtime; a gateway with mTLS refuses an agent without a client certificate | ⬜ |
| L45 | **Python functions.** Sandboxed Python UDFs callable from SQL, governed like SQL functions | A Python function runs on Trino rows without network or filesystem access | ✅ already built: inline and stored `LANGUAGE PYTHON` functions on Trino under `USE_FUNCTION` (`tests/test_trino_e2e.py::test_ai_and_python_functions_run_in_sql_under_cairns_policies`); found by the capability audit |
| L46 | **Chaos tests.** Cairn Core, the PDP, the gateway and the catalog stopped under load in CI | Each outage fails closed and recovers without lost writes | ⬜ |
| L47 | **SAML single sign-on.** SAML 2.0 alongside OIDC: SP metadata, signed assertions checked, groups from attributes | An Okta/ADFS/Entra tenant set up for SAML signs people in to Cairn with their groups; a forged or replayed assertion is refused | ✅ Cairn as SAML service provider (signed response or assertion, signature-wrapping and replay refused, audience, recipient, validity, InResponseTo) and OpenID provider for Cairn Core (code + PKCE, own signing key, groups claim); audited (`tests/test_saml_e2e.py` against Keycloak) |
| L48 | **Secrets for people's notebooks and jobs.** Secret scopes owned by people or groups, read by notebooks, workflows and Spark as the person | A notebook reads an API key from a scope its user may read; nobody else can; the value is never logged or shown again | ✅ |
| L49 | **Sharing BI assets with people and groups.** Viewer and editor roles on dashboards, workbooks, saved queries and metrics | A dashboard shared with Finance as viewers opens for Finance only; an editor changes it; others get 404 | ⬜ |
| L50 | **Retention enforced, legal hold and automated erasure.** Retention policies delete expired rows; legal hold blocks deletes and expiry on a table; right-to-be-forgotten deletes rows and proves it | Rows older than the policy are gone after the run; a table on hold keeps them; an erasure request ends with a signed report of rows removed | ⬜ |
| L51 | **Per-tenant and per-user API quotas, and tenant IP allowlists** | A user over their quota gets 429 with Retry-After; a request from outside the tenant's allowlist is refused | ⬜ |
| L52 | **BI folders, comments and usage analytics** | Dashboards organised in folders, discussed in threads, and a report shows who uses which and which are stale | ⬜ |
| L53 | **Metric alerts.** Thresholds and anomaly detection on metrics | Revenue dropping 20% week over week, or out of its usual range, notifies the owner once | ⬜ |
| L54 | **Map and heatmap charts; dashboards that work on a phone** | A region map and a heatmap render from metric tiles; a dashboard is usable at phone width | ⬜ |
| L55 | **Export files delivered to Slack and Teams** | A scheduled export arrives in a Slack channel as a file, each recipient's own view | ⬜ |
| L56 | **Metrics over SQL for outside BI tools** | Tableau or Power BI queries a metric through Trino as the signed-in person and gets the same number as the dashboard | ⬜ |
| L57 | **Scheduled notebook and Python jobs in Cairn** | A notebook runs nightly as its owner, its output kept, failures notified | ⬜ |
| L58 | **Governed volumes.** Folders of files under Cairn's policies, not only S3 policies | A volume shared with a group is readable by them alone, through the API and Spark | ⬜ |
| L59 | **Snapshot browser and restore** | A person sees a table's snapshots and restores yesterday's, audited | ⬜ |
| L60 | **Cost per query, user and table on Trino** | The cost report attributes Trino CPU and scanned bytes to people, teams and tables | ⬜ |
| L61 | **SaaS data connectors.** Loading from Salesforce, HubSpot and similar into Iceberg | Salesforce accounts land in a governed table on a schedule, incrementally | ⬜ |
| L62 | **Reverse ETL.** Syncing governed tables out to SaaS apps | A segment table syncs to a CRM as its owner; masked columns are never sent | ⬜ |
| L63 | **Managed model serving.** Deployments with autoscaling from the registry | A registered model is deployed from Cairn, scales with load and is invoked as the caller | ⬜ |
| L64 | **Build and deploy custom agents** | A team defines an agent with tools and scopes, deploys it, and its actions are governed and traced | ⬜ |
| L65 | **SDKs beyond Python, and an API deprecation policy** | JS and Java clients for the main APIs; deprecated routes send Deprecation and Sunset headers | ⬜ |
| L66 | **Consent management** | A subject's withdrawn consent excludes their rows from purposes that need it | ⬜ |
| L67 | **Accessibility tested.** Automated axe checks of the main pages in CI | The main pages pass axe with no serious violations | ⬜ |

## History: the metadata platform (M0–M6)

The milestones below built Cairn's metadata, governance, quality and AI layers. Their statuses are as recorded at v0.1. Later work is reflected in the platform table above.

### M0 — Foundations (week 1–2)

| # | Deliverable | Status |
|---|---|---|
| 0.1 | Cairn Core contracts and extension points mapped (maintainer notes in `core/`) | ✅ |
| 0.2 | Python package skeleton: settings, Cairn Core client (Rest.li ingestProposal, OpenAPI v3, GraphQL), MCP builders | ✅ |
| 0.3 | Cairn database layer (SQLAlchemy Core; Postgres prod / SQLite tests), hash-chained audit log + verifier | ✅ |
| 0.4 | Caller identity: Cairn Core PAT validation via Cairn Core `me`, UI session cookies via frontend, service token | ✅ |
| 0.5 | Compose stack (Cairn Core + Cairn + pgvector + nginx edge) and Helm umbrella chart | ✅ compose stack runs healthy; 🟡 Helm only lint/kubeconform |

**DoD:** `docker compose -f docker-compose.cairn.yml up` reaches healthy. `cairn bootstrap` creates the
certification structured properties, the `Certified` tag and the `Classification.PII.*` terms.

### M1 — Native Data Quality & Anomaly Detection (week 2–5)

| # | Deliverable | Status |
|---|---|---|
| 1.1 | Detectors: seasonal robust-z (MAD), EWMA, learned freshness cadence, PSI + KS drift, schema diff, static bounds | ✅ |
| 1.2 | Collectors: Cairn Core signals (`operation`, `datasetProfile`, `schemaMetadata`) and SQL (dialect-quoted, read-only guard) | ✅ |
| 1.3 | Engine: idempotent run ids, outage buffering + flush, feedback loop, incident raise/auto-resolve that never reopens human-closed incidents | ✅ |
| 1.4 | Projection to native `assertionInfo` / `assertionRunEvent` / `incidentInfo` | ✅ verified live via Cairn Core GraphQL (e2e) |
| 1.5 | Leader-elected scheduler (pg advisory lock) with hash partitioning | ✅ leadership acquired on Postgres in the live stack |
| 1.6 | REST API + React dashboard, monitor editor, evaluation chart, dataset health badge | ✅ |
| 1.7 | Smart-monitor auto-provisioning (create volume/freshness monitors for every certified or top-usage dataset) | ✅ auto-monitor rules (`quality/scheduler.py`, `tests/test_auto_monitors.py`) |
| 1.8 | Per-column profiling collector (null/distinct for all columns in one scan) and partition-aware volume | 🟡 per-column profiling (`quality/profiler.py`); ⬜ partition-aware volume (L43) |
| 1.9 | Next: optional Kafka-sink of evaluations for real-time alerting fan-out (PagerDuty/Opsgenie native) | ⬜ |

**DoD:** a volume monitor on a demo table pages within one interval when rows drop 90%. It auto-resolves
on recovery, and both events are visible on the dataset's Quality and Incidents tabs.

### M2 — Enterprise Governance (week 4–8)

| # | Deliverable | Status |
|---|---|---|
| 2.1 | ABAC engine: condition DSL (17 operators, `ref` to subject attrs, dotted structured-property keys), safe regex, targets | ✅ |
| 2.2 | PDP: deny-overrides, RBAC roles + inheritance + bindings, obligations, sub-resource checks, explain traces, decision cache with MCL-driven invalidation | ✅ |
| 2.3 | Native `dataHubPolicy` import (actors, owners/owner-types, resource filters) with retry until Cairn Core is up | ✅ |
| 2.4 | Java `CairnAuthorizer` Cairn Core plugin: fail-closed, circuit breaker, TTL cache, root/system bypass | ✅ 16 JUnit tests; loads and initialises in Cairn Core (Java 25) |
| 2.5 | Additive vs authoritative mode (`CAIRN_NATIVE_POLICIES_ENABLED=false` + PDP as sole authority) | 🟡 needs live Cairn Core validation |
| 2.6 | PII classifier (names + validated value patterns) and lineage propagation action with attribution, loop safety, revert | ✅ propagation verified live (Kafka → actions → Cairn Core) |
| 2.7 | Certification/approval workflows: quorum, SoD, veto, entry checks, expiry, DQ auto-revoke, projection (structured property + tag) | ✅ |
| 2.8 | UI: access simulator, policy editor, certification inbox | ✅ |
| 2.9 | MCL consumer that calls `AttributeResolver.invalidate` (was: TTL 60 s) | ✅ `governance/invalidation.py` (`tests/test_mcl_invalidation.py`) |
| 2.10 | Next: enforce `mask_columns` obligations in Cairn Core GraphQL responses (field-level redaction resolver) and in the Cairn SQL proxy | ⬜ |
| 2.11 | Next: policy-as-code CI (`cairn policies validate` + simulation test fixtures in Git) and SCIM/IdP attribute sync | ⬜ |

**DoD:** with authoritative mode on, a contractor gets DENY for `VIEW_DATASET_PROFILE` on a PII
dataset in the native UI while an owner can still edit it. Tagging an upstream column as PII appears
on downstream columns within one MCL round-trip.

### M3 — In-VPC Remote Ingestion Agents (week 6–10)

| # | Deliverable | Status |
|---|---|---|
| 3.1 | Crypto: Ed25519 request signing, X25519/HKDF/AES-256-GCM sealed envelopes with AAD binding, bounded decompression | ✅ |
| 3.2 | Gateway: one-time enrolment tokens, nonce/skew replay protection, leasing with expiry + retries, sequence enforcement, pool policy MCP filtering, protected entity types | ✅ |
| 3.3 | Dispatcher: claims native `dataHubExecutionRequest`s by `executorId == pool`, updates `dataHubExecutionRequestResult` | ✅ verified live (poll mode); ⬜ MCL mode wiring |
| 3.4 | Agent: enrolment, heartbeat, poll loop with backoff, local secret resolution (env/dir/Vault/AWS SM), encrypted `cairn-encrypted` sink | ✅ real Postgres ingestion through an isolated network verified live |
| 3.5 | Helm chart `cairn-remote-agent` (restricted PSS, read-only rootfs, egress-only NetworkPolicy) | ✅ lint/kubeconform |
| 3.6 | UI: fleet, pools, enrolment-token flow, runs | ✅ |
| 3.7 | Cancellation (`dataHubExecutionRequestSignal` → heartbeat `cancel_task_ids` → pipeline interrupt) | ✅ queued and running tasks (`tests/test_agent_ops.py`) |
| 3.8 | Encrypted live log streaming from the pipeline logger, key rotation via signed re-enrolment, mTLS option | 🟡 run logs streamed, scrubbed and paginated (`tests/test_agent_ops.py`); ⬜ key rotation by re-enrolment and mTLS (L44) |
| 3.9 | On-disk spool for batches during gateway outages | ✅ in order, bounded, lease hand-back (`agent/sink.py`, `tests/test_agent_ops.py`) |

**DoD:** a Postgres source reachable *only* from the `vpc-finance` network is ingested through the
agent. The recipe in Cairn Core contains only `${PG_PASSWORD}`, and the gateway rejects a forged
platform URN.

### M4 — AI Context & Agentic Discovery (week 8–12)

| # | Deliverable | Status |
|---|---|---|
| 4.1 | LLM layer: Claude (`claude-opus-5-5`, structured outputs, adaptive thinking, effort, server-side refusal fallback) + Ollama for air-gapped | ✅ request shape unit-tested; 🟡 live API call |
| 4.2 | NL → typed `SearchPlan` → Cairn Core search/lineage → grounded answer; hallucinated citations dropped; heuristic fallback | ✅ |
| 4.3 | Semantic index: entity documents, content-hash dedupe, pgvector HNSW / in-memory, MCL indexer action | ✅; ⬜ `embedding_provider=catalog` (delegate to Cairn Core `SemanticSearchService`) |
| 4.4 | Documentation generation → `DOCUMENTATION` workflow → attributed write on approval | ✅ |
| 4.5 | Guardrails: PDP-filtered retrieval, PII redaction, untrusted-data wrapping, LLM call audit | ✅ |
| 4.6 | MCP server (stdio + streamable HTTP with PAT auth, 11 tools) | ✅ verified live against Cairn Core |
| 4.7 | UI: Ask Cairn Core panel, doc draft review | ✅ |
| 4.8 | Next: eval set for NLQ plans and doc drafts (golden questions per domain), cost tracking per user | ⬜ |
| 4.9 | Next: query-log grounding (popular SQL from `queryProperties`) and column-level lineage in doc prompts | ⬜ |

**DoD:** in Claude Code, `ask_metadata("which certified tables feed the revenue dashboard?")` returns
only assets the caller can see, each with a citation. `generate_documentation` creates a review item and
writes nothing until it is approved.

### M5 — Hardening & GA (week 12–16)

| # | Deliverable | Status |
|---|---|---|
| 5.1 | Build and scan images (`docker/Dockerfile`, `agent.Dockerfile`, plugin image); SBOM; cosign signatures | ✅ build (`deploy/build-images.sh`), Trivy gate, SBOMs, cosign signing and registry push from `main` in CI |
| 5.2 | End-to-end smoke tests against the compose stack (Playwright UI + API), chaos tests (Cairn Core/PDP/gateway outages) | ✅ e2e suite (API, Playwright UI, SSO, SCIM, edge) in CI on a fresh stack; ⬜ chaos tests |
| 5.3 | Load test PDP (target p99 < 5 ms cached, < 25 ms uncached at 2k rps) and gateway (100 agents, 1 GB metadata/run) | 🟡 `tests/load/loadtest.py` against published limits, cached decisions and 1k-table jobs in `tests/test_scale_decisions.py`; ⬜ 2k rps and gateway volume not run in CI |
| 5.4 | Alembic migrations for the `cairn` schema; v0.1 databases (`create_all`) are adopted at the baseline | ✅ SQLite + Postgres tests, live upgrade of a v0.1 database, `cairn db check` in CI |
| 5.5 | Threat model review + external pentest of the gateway; key management via KMS/HSM for the gateway identity | ✅ threat model (`docs/site/agent-security.md`) with a negative test per attack, gateway hardening (vocabulary, `editable*`, platform proof, body cap, trusted proxies); ⬜ external pentest, KMS/HSM |
| 5.6 | Upgrade playbook: pin-and-test matrix against Cairn Core minor releases (aspect shapes, GraphQL fields) | ⬜ |
| 5.7 | Authoritative mode proven live: Cairn denies hold on REST and GraphQL (proxy view enforcement); PDP attribute reads no longer re-enter the authorizer | ✅ e2e test_14 in both modes |
| 5.8 | Masking on every read path (GraphQL, OpenAPI v3 entity/aspect/list, v2 timeseries) | ✅ e2e test_03b |
| 5.9 | Metadata changes reach access decisions within seconds (MCL-driven invalidation) | ✅ unit tests, e2e test_15 |
| 5.10 | SSO (OIDC) with guided check (`cairn sso check`); SCIM 2.0 provisioning with a separate least-privilege account | ✅ mock IdP + Playwright sign-in, SCIM e2e incl. account separation |
| 5.11 | Edge TLS + HSTS, rate limits, audit export (`/governance/audit/export`, `cairn audit export/verify-file`), `*_FILE` secrets | ✅ e2e test_edge, unit tests |

### M6 — Active Metadata & AI Context Layer (UX_ACTIVE_METADATA_SPEC.md)

| # | Deliverable | Status |
|---|---|---|
| U1 | Consumer MFE shell (`/mfe/cairn`), `/ui/config`, collaboration (bookmarks, follows, views, trending, threads with @mentions), Discover + faceted search | ✅ code + tests; ⬜ mounted in a live cairn-web, search p95 measured |
| U2 | Marketplace: product cards/detail, request policies, access requests with auto-approval, SoD, time-boxed persona grants with expiry and warnings, Slack/Teams/email approvals | ✅ code + tests; ⬜ live Slack round-trip |
| U3 | Personas & Purposes (studio, compiler into PDP layers, purpose denies at priority 1000, masking obligations), `cairn-core-proxy` masking with fail-closed behaviour, rule-based tag/term propagation (`cairn_label_propagation`) | ✅ code + tests; ⬜ compliance sign-off on the live stack |
| U4 | Context agents: query/dbt/BI/run miners, METRIC_DEFINITION / DEFINITION_DRIFT / TERM_* / JOIN_RELATIONSHIP / RUN_ANOMALY findings, accept → native glossary terms / ER relationships / discussions, ontology | ✅ code + tests; ⬜ pilot-domain acceptance rate |
| U5 | Assistant (Claude tool loop over caller-scoped tools, offline fallback), lineage-anomaly explainer, markdown docs | ✅ code + tests; ⬜ golden-set evaluation with a live model |
| U6 | Embeds: signed origin-bound tokens, web components + iframe pages, Tableau `.trex`, Looker/Power BI/portal guides; Teams adaptive cards | ✅ code + tests; ⬜ validated inside each BI tool |
| U7 | Deployment: compose services (`cairn-core-proxy`, `cairn-context-miner`, `cairn-notifier`, `cairn-consumer-ui`), Helm templates + `values-active-metadata.yaml`, Slack app manifest | ✅ rendered and linted; ⬜ live cluster |

### Issues found by the first live run (fixed)

Running against real Cairn Core surfaced five defects that unit tests with fakes could not catch:

1. Cairn Core loads `<plugin>/<plugin-name>.jar`, but the build produced a versioned jar name, so Cairn Core failed to start.
2. Cairn Core rejects `NO_EXPIRY` access tokens. Bootstrap now mints a token with a finite lifetime
   (`CAIRN_CORE_TOKEN_DURATION`, default `THREE_MONTHS`) and re-mints it when it stops validating.
3. The admin API eagerly created gateway keys, which crashed on a read-only root filesystem. Key loading is
   now lazy and happens only in `cairn gateway`.
4. Compose `tmpfs` mounts default to `root 0755`, which the non-root agent cannot write. They are now
   mounted with `mode=1777`.
5. OpenSearch blocks index creation at its 95% disk flood stage. The threshold is configurable in compose
   via `CAIRN_SEARCH_DISK_THRESHOLD_ENABLED`.

### Code-generation plan (how each milestone was / will be produced)

1. **Contract first:** derive the shape of each aspect and API from the Cairn Core schemas (aspect
   models and the GraphQL schema). Encode it in builders
   (`quality/emitter.py`, `governance/certification.py`) with a unit test per shape.
2. **Pure core, thin adapters:** detectors, ABAC evaluation, propagation planning and the crypto envelope
   are pure functions tested in isolation. The engine, PDP, gateway and workflows compose them. HTTP,
   Kafka and MCP layers are thin wrappers around them.
3. **Fakes over mocks:** `InMemoryGraph` implements the same `MetadataGraph` protocol as `CoreClient`,
   so every module test runs the real code path end to end without a live Cairn Core.
4. **Live validation gate:** before a milestone's 🟡 items flip to ✅, run its DoD scenario on the
   compose stack and add it to the smoke suite (M5.2).
