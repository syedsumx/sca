# Cairn BRD appendix: roadmap traceability

Generated from `cairn/ROADMAP.md` on `main` (`3c829dc8`). Re-generate when the roadmap changes. Status markers are the roadmap's own: ✅ shipped and tested end to end in CI, 🟡 shipped and partly validated, ⬜ planned.

**Totals:** 185 items · ✅ 124 shipped · 🟡 43 partly validated · ⬜ 18 planned.

| ID | Capability | Status | Current state (from the roadmap) |
|---|---|---|---|
| L1 | Table maintenance service | ✅ | policies per table, schema or default; compaction, snapshot expiry, orphan removal on a schedule; automatic cl… |
| L2 | Governed Spark on restricted tables | ✅ | a Cairn-planned scan through Trino: `cairn_spark.governed_read` reads as the user, with their masks and row fi… |
| L3 | Spark on Kubernetes | ✅ | submission, operator, Airflow task and history server (`tests/test_spark_k8s_e2e.py`); on a full cluster in CI… |
| L4 | Notebooks | ✅ | JupyterHub signs people in through Cairn (OAuth client `jupyterhub`); each person's server gets a Cairn token … |
| L5 | Streaming and CDC ingestion | ✅ | Kafka JSON and Debezium CDC into Iceberg (`cairn streams`): upserts and deletes, offsets in the table snapshot… |
| L6 | Workload management and chargeback | ✅ | a resource group per team, tested under load on real Trino with Cairn deciding every action: team A's flood ru… |
| L7 | Machine learning | ✅ | MLflow behind Cairn sign-in (`/v1/ml`): runs attributed to the caller, an owned and governed registry, version… |
| L8 | Open data sharing | 🟡 | shares through Iceberg REST: recipient credential, only the shared tables, read-only scoped keys, expiry, revo… |
| L9 | Disaster recovery across regions | ✅ | standby store by bucket replication, standby PostgreSQL by streaming, a failover runbook and `cairn dr reconci… |
| L10 | Scale and assurance | 🟡 | in CI: cached decisions p99 0.02 ms at about 107k/s on one thread and a 1,000-table catalog decision in 0.6 s … |
| L11 | Upgrade matrix | ✅ | `deploy/versions/pins.json` (checked against every file that pins), a weekly workflow that finds each engine's… |
| L12 | Zero-copy clones, undrop, branches and tags | ✅ | `POST …/tables/{t}/clone`, `…/undrop`, `GET …/dropped` in Cairn Catalog; branches and tags kept by the catalog… |
| L13 | HTTP streaming ingestion | ✅ | http streams: `POST /v1/ingest/<name>/rows` with per-channel offsets in the table's snapshots, `INGEST_STREAM`… |
| L14 | Query insight | ✅ | `/v1/queries` from Trino's events: per-query statistics and SQL for its author and operators, hints for queuei… |
| L15 | Files as data | ✅ | files pipes: one row per object with its metadata and the text of text, HTML, PDF, Word and PowerPoint files, … |
| L16 | Vector search on lakehouse data | ✅ | vector indexes: chunks embedded with the deployment's model into `<table>__<index>_vectors`, one per index, re… |
| L17 | Scheduled SQL and in-product workflows | ✅ | SQL workflows (`/v1/sql-workflows`): derived refreshes, checks and reports in a DAG on cron, with retries, run… |
| L18 | Infrastructure as code for SQL and search | ✅ | bundle kinds and Terraform resources for all five, references by name (bundles) or id (Terraform), subset drif… |
| L19 | Delta Lake beyond one writer, and into Iceberg | ✅ | conditional-write commits (`s3.exclusive-create`, checked atomically by Cairn Store), proven with two Trino cl… |
| L20 | Git-backed SQL and notebooks | ✅ | Git folders over the `git` command (any host): pull to saved queries and catalog notebooks, conflicts reported… |
| L21 | Stored functions and procedures | ✅ | stored functions in a `routines` catalog, `USE_FUNCTION` per function with masks applied first; procedures wit… |
| L22 | Customer keys beyond Vault | ✅ | `aws-kms` external keys: SigV4, the object's name as the encryption context, rotated credentials picked up (`s… |
| L23 | Data apps | ✅ | `/apps/<name>/` proxy (HTTP and WebSocket), `USE_APP` per app, the viewer's identity and a Trino token for the… |
| L24 | Materialized views | ✅ | `CREATE`, `REFRESH` and `DROP MATERIALIZED VIEW` in the SQL editor, refreshed on change as their owner, fresh/… |
| L25 | Change data feed | ✅ | `GET /v1/lakehouse/tables/<schema>/<table>/changes` (inserts, deletes, updates by key, the next cursor) and `.… |
| L26 | SIEM export | ✅ | at-least-once, ordered delivery with a cursor per sink, backoff, lease and rewind; tokens from files; tested a… |
| L27 | Signup and billing | ✅ | email confirmation, Checkout, signed and idempotent webhooks, provisioning through `tenant.sh` with retries, r… |
| L28 | Usage metering | ✅ | signed daily reports with per-tenant keys derived on the control plane, delivered by `tenant.sh --metering-key… |
| L29 | Status page | ✅ | probes with operational/degraded/outage, daily uptime, incidents with updates and scheduled maintenance, a scr… |
| L30 | Gateway API | ✅ | `core.gateway` in the chart, the same routing as the Ingress (prefix rewrites, X-Forwarded-Prefix, timeouts, c… |
| L31 | BI: spreadsheet workbooks | ✅ | the formula language compiled safely to Trino SQL, workbook elements with calculated columns, row and group fi… |
| L32 | BI: input tables (writeback) | ✅ | create, add, edit and delete rows as the person (Trino asks Cairn for each statement), typed literals, optimis… |
| L33 | BI: interactive dashboards | ✅ | values, range, date range and text controls (choices from the data as the viewer sees it), cross-filter on cli… |
| L34 | BI: metrics layer | ✅ | metrics with their own filters, time column and allowed dimensions, compiled to one workbook element and run a… |
| L35 | BI: AI builder | ✅ | a question becomes a dashboard of metric and workbook tiles and controls proposed as structured output (no mod… |
| L36 | BI: app actions | ✅ | forms and buttons that add or change input-table rows as the person clicking (Trino decides), run workflows on… |
| L37 | BI: scheduled exports and a result cache | ✅ | a result cache shared only between people whose decisions on every table read are the same, dropped on a table… |
| L38 | BI assets in the catalog | ✅ |  |
| L39 | BI version history | ✅ |  |
| L40 | Embedded dashboards | ✅ |  |
| L41 | BI as code | ✅ |  |
| L42 | Input-table approvals | ✅ |  |
| L43 | Partition-aware volume monitors | ✅ | `partition_column` on SQL volume monitors (missing and thin days or hours); tested on an Iceberg table partiti… |
| L44 | Remote agent hardening | ✅ | Signed rotation with proof of the new key, a grace period and crash recovery; mutual TLS on the gateway; teste… |
| L45 | Python functions | ✅ | already built: inline and stored `LANGUAGE PYTHON` functions on Trino under `USE_FUNCTION` (`tests/test_trino_… |
| L46 | Chaos tests | ✅ | PDP and catalog stopped under Trino load, gateway restarted and Core refusing writes mid-run; Core itself at i… |
| L47 | SAML single sign-on | ✅ | Cairn as SAML service provider (signed response or assertion, signature-wrapping and replay refused, audience,… |
| L48 | Secrets for people's notebooks and jobs | ✅ |  |
| L49 | Sharing BI assets with people and groups | ✅ |  |
| L50 | Retention enforced, legal hold and automated erasure | ✅ |  |
| L51 | Per-tenant and per-user API quotas, and tenant IP allowlists | ✅ |  |
| L52 | BI folders, comments and usage analytics | ✅ | API and screens (folders, threads, usage and stale assets), tested in Chromium |
| L53 | Metric alerts | ✅ | Change, range and threshold rules per period, run as the owner, told once per change of state; tested on Trino |
| L54 | Map and heatmap charts; dashboards that work on a phone | ✅ | Heatmap and point-map charts (operator's tiles optional); one-column dashboards on a phone, tested in Chromium… |
| L55 | Export files delivered to Slack and Teams | ✅ | Each recipient's own copy uploaded to their Slack DM; Teams gets a link to it (webhooks carry no files); share… |
| L56 | Metrics over SQL for outside BI tools | ✅ | Metrics published as SECURITY INVOKER views read on their table's terms; same numbers as Cairn's metric query … |
| L57 | Scheduled notebook and Python jobs in Cairn | ✅ | Notebooks and scripts on a cron, each run in a locked-down container (Docker) or a Kubernetes Job, with a Trin… |
| L58 | Governed volumes | ✅ | Volumes (a bucket each) under Cairn's grants to people and groups; files through the API, and narrowed, short-… |
| L59 | Snapshot browser and restore | ✅ | Snapshots tab with row counts, the table as it was (as the person sees it), and restore with a reason, as the … |
| L60 | Cost per query, user and table on Trino | ✅ | Each query priced from its CPU and scanned bytes; attributed to people, teams (even split) and tables (even sp… |
| L61 | SaaS data connectors | 🟡 | Salesforce and HubSpot into Iceberg on a cron, incremental by modification time, upserted by id, keys from sec… |
| L62 | Reverse ETL | 🟡 | HubSpot and Salesforce upserts read on Trino as the owner (row filters apply), refused when a sent column is m… |
| L63 | Managed model serving | ✅ | Deployments of registered versions, invoked as the caller (USE_ML_MODEL), scaled by in-flight concurrency with… |
| L64 | Build and deploy custom agents | 🟡 | Agents built from the assistant's tools with scopes (read, query_data, propose), deployed with draft-on-change… |
| L65 | SDKs beyond Python, and an API deprecation policy | ✅ | Dependency-free JavaScript/TypeScript and Java 17 clients for the main APIs (identity, health, search and ques… |
| L66 | Consent management | ✅ | Purposes that need consent (opt-out or opt-in, members by user, group or persona, an identifier kind); records… |
| L67 | Accessibility tested | ✅ | Twenty main pages checked with axe-core (WCAG 2.1 A/AA) in Chromium against a running Cairn, in CI; fixed what… |
| L68 | Failover for tenant OpenSearch and Kafka | ✅ | `CAIRN_TENANT_SERVICES_HA=1`. Tested on the rendered manifest in Docker: Kafka with a broker lost (no write lo… |
| L69 | Spark training and model serving in the chart | ✅ | Model serving on Kubernetes tested on kind in CI (run 287: a Deployment per model deployment with only the cha… |
| L70 | Sample datasets for new tenants | ✅ | `/v1/samples`, `cairn samples`, loaded once on a new tenant's first start (`CAIRN_TENANT_SAMPLES`, chart `cair… |
| L71 | Access reviews | ✅ | `/governance/access-reviews`: product owners review request-based grants, no self-review, revoke at once, unde… |
| L72 | Bad-row quarantine in pipes and streams | ✅ | `bad_rows: quarantine` on pipes and streams; set-aside rows with file and row (or offset), reason and value (`… |
| L73 | Data residency | ✅ | Rules file shared by Cairn and Cairn Catalog (`cairn.residency` in the chart). The catalog keeps pinned tables… |
| L74 | Reversible tokenisation | ✅ | Token domains with their own keys (wrapped by `CAIRN_TOKENS_KEY`, rotatable without changing tokens); determin… |
| L75 | Table branches, tags and data diff | ✅ | `/v1/tables/{t}/refs` (branches, tags, `MANAGE_DATASET`), publish with SQL checks on the branch as the caller … |
| L76 | Synthetic test data | ✅ | `/v1/synthetic`: statistics on Trino as the caller (their masks apply); sensitive and masked columns made from… |
| L77 | Speech to text for audio and video files | ✅ | Local Whisper (`faster-whisper`, the `media` extra): text and length in seconds on the files table; models put… |
| L78 | AI enrichment of files: image description and field extraction | ✅ | Files pipes' `ai`: `description`, `labels` and `extracted` (JSON) columns, `ai_error` on failure; images scale… |
| L79 | Governed geospatial | ✅ | `geo` mask strategy: coordinates rounded (`CAIRN_GEO_MASK_DECIMALS`), WKT/Geometry/SphericalGeography reduced … |
| L80 | Chargeback statements | ✅ | Cost centres (code, name, teams); SQL by who ran it, AI by who asked, tables by their owners, unattached runs … |
| L81 | Point-lookup acceleration | ✅ | Maintenance policies' `lookup`: `auto` from Trino's query history (literal `=`/`IN` lookups, ≥10% of queries) … |
| L82 | Security posture scan | ✅ | Ten checks (trusted header, open policies, audit chain, unmasked PII, residency region, authz mode, long and i… |
| L83 | Passkeys and step-up | ✅ | WebAuthn registration and assertions (`webauthn`), step-up tokens (5 minutes, the person only, hashed), `X-Cai… |
| L84 | The assistant's memory, artifacts and deep research | ✅ | Memory notes (saved only when asked; listed, deletable, switchable off; PII redacted; instruction-like notes r… |
| L85 | Aggregation and projection policies | ✅ | Policy obligations (`aggregation`: `min_group`, `entity_key`; `projection`: `columns`). Cairn checks each quer… |
| L86 | Iceberg v3 tables | ✅ | Catalog on iceberg-rust 0.11 (v3 types and metadata); Spark 4 / Iceberg 1.10 writes v3 with deletion vectors, … |
| L87 | Adaptive warehouses | ✅ | Query fingerprints in query history (migration 0113) and `POST /v1/warehouses/hint` (median CPU, bytes, memory… |
| L88 | Operational databases | ✅ | `/v1/databases`: create, branch (a copy with its own roles), drop; per-person logins (`write`/`read`, up to a … |
| L89 | Mirrors | ✅ | `/v1/mirrors`: a publication and a `pgoutput` slot per mirror, an initial copy, whole transactions per Iceberg… |
| L90 | App packages | ✅ | Packages from git with immutable versions (commit + `cairn-app.json` manifest declaring read references); inst… |
| L91 | Fine-tuning on governed data | ✅ | `/v1/ml/finetunes`: training sets from Cairn's governed query path (masks, filters, query policies), PII redac… |
| L92 | Connectors: MySQL change data capture | ✅ | Mirrors accept `mysql://` connections: settings checked (row-based log, full row image and metadata), a copy f… |
| L93 | Semantic model autopilot | ✅ | `/sql/semantic-models` (migration 0118): profiles as the person (masks and row filters apply), query history's… |
| L94 | Pipeline agent | ✅ | `/v1/pipeline-agent/tasks` (migration 0119): the model proposes a derived table and checks (never sees rows); … |
| L95 | CoWork: scheduled tasks and an approved outbox | ✅ | `/ai/scheduled-tasks` and `/ai/outbox` (migration 0120): cron at most hourly, research runs as the owner (read… |
| L96 | Connectors: SQL Server change data capture | ✅ | Mirrors accept `mssql://` connections: CDC and a primary key checked (refused with the commands to run), a cop… |
| L97 | Connectors: MongoDB change streams | ✅ | Mirrors accept `mongodb://` connections (migration 0121 widens positions for resume tokens): replica set and c… |
| L98 | Document sources: SharePoint and Google Drive | 🟡 | `/v1/document-sources` (migration 0122): Microsoft Graph (client credentials) and Drive (service account) list… |
| L99 | AI gateway for agent code | ✅ | `/v1/ai/gateway/anthropic/v1/messages`, `/v1/ai/gateway/openai/v1/chat/completions`, `/v1/ai/gateway/models`: … |
| L100 | Code-first agents: `cairn agents init`, `dev`, `deploy` | ✅ | `cairn.agentkit` (runtime, `ctx` with gateway-routed SDK clients and governed SQL, offline test kit, three tem… |
| L101 | Agent trace intelligence | ✅ | `cairn.ai.trace_insights`, `/v1/agent-traces/insights` (and per agent), `/v1/agent-traces/settings` (admin, au… |
| L102 | Grounded answers with sources | ✅ | `POST /v1/vectors/<name>/answer`: passages retrieved as the caller (refused under masks and row filters), numb… |
| L103 | Serving tables (interactive analytics and hybrid tables) | ✅ | `cairn.dataaccess.serving`, `/v1/serving` (migration 0127): owners or operators declare a serving table (prima… |
| L104 | Streaming transformations | ✅ | `transform` on `json` and `http` streams: one row-for-row SELECT over `batch`, checked when the stream is defi… |
| L105 | External Iceberg catalogs, governed | ✅ | `/v1/external-catalogs` (migration 0125): registration with a credential reference (secret scope or `CAIRN_EXT… |
| L106 | Share observability | ✅ | Per share, day and table counts from Cairn Catalog's per-request questions and the token endpoint, with retent… |
| L107 | Agents and skills in the marketplace | ✅ | `/marketplace/listings` and `/marketplace/search` (migration 0125): listings by the agent's or skill's owner, … |
| L108 | Agent automations from the CLI: `cairn tasks` | ✅ | `cairn.agentkit.tasks`: `list`, `create` (name, prompt, cron, max steps, Slack or email), `show` (last runs), … |
| L109 | Intent-based security for agents | ✅ | `cairn.identity.intents`: intents (purpose, actions `read`, `query`, `propose`, `write`, `share`, `export`, `m… |
| L110 | SCD type 2 and medallion layers | ✅ | Derived mode `scd2` (`key`, `tracked`): one Trino MERGE per refresh closes changed and removed keys (`valid_to… |
| L111 | Agent memory | ✅ | `cairn.ai.agent_memory` (migration 0129): memories per agent and person (written by the agent through its run … |
| L112 | AI search and document parsing in SQL and pipelines | ✅ | Cairn's Trino functions plugin (`plugins/java/cairn-trino-functions`, built inside the Trino image, installed … |
| L113 | Dynamic model routing in the AI gateway | ✅ | `cairn.ai.gateway_routes` (migration 0129): candidates with capabilities (tools, vision), input size and timeo… |
| L114 | Agentic abuse detection | ✅ | `cairn.ai.abuse` (migration 0129): rule-based detectors with severities on inputs reaching models (gateway mes… |
| L115 | Zero-copy Parquet | ✅ | `POST /v1/lakehouse/parquet-tables` (`MANAGE_INGESTION`): files only from `CAIRN_PARQUET_BUCKETS`, read access… |
| L116 | Sandboxes for agent code | ✅ | `cairn.ai.sandboxes`: `POST /v1/sandboxes/runs` (also the `run_python` tool for custom agents, the assistant a… |
| L117 | AI data packages and agents on shared data | ✅ | `cairn.dataaccess.share_packages`: `PUT /v1/shares/<share>/package` publishes a versioned manifest (tables, co… |
| L118 | Cairn in Excel and Google Sheets | 🟡 | `cairn/addins`: the Excel add-in (manifest, shared runtime, custom functions `CAIRN.QUERY`/`METRIC`/`ASK`, tas… |
| L119 | Profiles, mobile app and desktop | 🟡 | `cairn.identity.profiles` (migration 0131): `GET`/`PUT /v1/me/profile` (role, team, default persona among the … |
| L120 | Talk to your app | ✅ | `cairn.dataaccess.app_chat`: `chat` on a data app (description, tables, queries, a context endpoint; migration… |
| L121 | Cairn Ontology | ✅ | `cairn/ontology` (`cairn-ontology`, Rust; PostgreSQL or SQLite): catalog snapshots diffed incrementally with a… |
| L122 | Cairn Flows | ✅ | `cairn.flows` (migration 0132): one definition (`kind: flow`, YAML or JSON, with `layout` for the canvas) chec… |
| L123 | Flow canvas | ✅ | **Flows** in the navigation: a canvas with pan, zoom, minimap, snap to grid, layered auto-layout, a palette gr… |
| L124 | Airflow both ways | 🟡 | `cairn.flows.airflow_dag`: `GET /v1/flows/{name}/airflow`, `cairn flows export-airflow` (also from a file, wit… |
| L125 | Everything as code | 🟡 | `cairn.iac.registry` + `cairn.iac.kinds`: 54 resource types (identity and access, data and pipelines, SQL and … |
| L126 | Agents do infrastructure as code | 🟡 | `cairn.iac.changes` (migration 0133): `/v1/iac/changes` (plan, propose, list, get, approve, reject, apply, com… |
| L127 | Friendly forms everywhere | ✅ | `cairn.iac.forms` (`python -m cairn.iac.forms`, CI `--check`) writes `forms.generated.json`: 122 forms for eve… |
| L128 | Guided experience | ✅ | Onboarding: a role question on first visit to the home page (suggested from the profile's role; "Not now" is a… |
| L129 | Ontology explorer | ✅ | **Ontology** in the navigation (`/ontology`, linked from Semantic models; no new dependencies): an SVG force-d… |
| L130 | CLI 2.0 | 🟡 | `cairn login`: OAuth 2.0 device flow (RFC 8628) in `cairn.identity.device` (`POST /oauth/device_authorization`… |
| L131 | Cairn Desktop 1.0 | 🟡 | `cairn/desktop` (Electron 45.2.3, electron-builder 26.18.0, electron-updater 6.8.11): workspaces are the CLI's… |
| L132 | Tables on your own cloud storage | 🟡 | Cairn Catalog (`catalog/src/external.rs`): table and namespace locations on Azure, S3 and GCS accepted only in… |
| L133 | Storage credentials and external locations as governed objects | ✅ | `cairn.dataaccess.storage_locations` (migration 0137): storage credentials (Azure, S3, GCS; secrets only as `f… |
| L134 | System tables | 🟡 | `cairn.dataaccess.system_tables` (no migration): eight Iceberg tables materialised by the scheduler's leader e… |
| L135 | Split control plane and data plane | 🟡 | `cairn.dataplane` (migration 0136): data planes (`/v1/dataplanes`, `cairn_dataplane`), single-use enrolment to… |
| L136 | Network controls for user code, and private networking | 🟡 | `cairn.network` (migration 0136): `/v1/network/egress-rules` (hosts, `*.domain`, CIDRs, ports, per workspace o… |
| L137 | Everything as code, with no configuration gaps | ✅ | 78 resource types (17 new): `agent_pools` (`PUT`/`DELETE /agents/pools/{name}` added; an occupied pool is refu… |
| L138 | Orchestrate across platforms, from Cairn Flows and from Airflow | 🟡 | `cairn.flows.platforms` (no migration): step types `http` (method, URL, headers, a JSON body with templates, b… |
| L139 | People & roles | ✅ | `cairn.identity.people`, migration `0138` (`cairn_people`): `/v1/people/users` (search, paging, filters status… |
| L140 | Managed Postgres | 🟡 | `cairn.databases` (migration 0140): instances (`/v1/databases/instances`, `cairn_pg_instances`, `CREATE_DATABA… |
| L141 | End-to-end observability for the whole platform | 🟡 | `cairn.observability` (migration `0140`: `cairn_obs_config`, `cairn_obs_alerts`, `cairn_obs_incidents`, `cairn… |
| L142 | An SRE agent for operations and managed Postgres | 🟡 | `cairn.sre` (migration 0142: `cairn_sre_config`, `cairn_sre_runs`, `cairn_sre_actions`, `cairn_sre_postmortems… |
| L143 | Custom ops dashboards | 🟡 | `cairn.observability.dashboards` (ops tiles, limits, `/v1/observability/tiles/{preview,options,limits}`), `bui… |
| L144 | Managed Airflow | 🟡 | `cairn.airflow_deploy` (migration 0143: `cairn_airflow_deployments`, `cairn_airflow_deploys`, `cairn_airflow_p… |
| L145 | Pipeline observability for Airflow, and OpenTelemetry export everywhere | 🟡 | `cairn.observability.airflow_otel` (`airflow_otel_env(cfg, service_name, deployment)`: Airflow 3.3's `AIRFLOW_… |
| L146 | Design system and screens from the platform design | 🟡 | `components/tokens.ts` (tokens, CSS variables light and dark, the scoped antd look: 44 px controls, status tag… |
| L147 | Catalog-first: Cairn Core at the centre | 🟡 | `marketplace/products.py` + `products_api.py` (`/catalog/products`: Core aspects written, `MANAGE_DATA_PRODUCT… |
| L148 | Glossary and domain editing | ✅ | CI run 379 (the stack job, against the real Cairn Core): every live test passes, including term groups, terms … |
| L149 | One home per concept (information architecture) | 🟡 | Steps 1 and 2 built and tested: `components/Layout.tsx` (the seven groups; Inbox with its unread count and Pro… |
| L150 | Calmer, consistent workspace | 🟡 | `utils/errors.ts` (not connected: `SERVICES`, `isNotConnected`), `components/ErrorNotice.tsx` (`NotConnected`)… |
| L151 | Snowflake and Databricks tables in place | 🟡 | `dataaccess/external_catalogs.py` (`PRESETS`: `snowflake_open_catalog`, `snowflake_horizon`, `databricks_unity… |
| L152 | Apache NiFi: governed data connectors | 🟡 | `cairn.nifi` (migration 0145: `cairn_nifi_deployments`, `cairn_nifi_flows`; `/v1/nifi`): `spec` (deployments: … |
| L153 | Models from the hyperscalers' model services | 🟡 | `cairn.ai.cloud_providers` (bedrock: InvokeModel and the response stream's AWS event stream decoded into Anthr… |
| L154 | The whole API over MCP | 🟡 | `cairn.ai.mcp_api` (catalogue from OpenAPI, 12 toolsets each at most 128 tools, 786 tools and 43 exclusions wi… |
| L155 | The AI gateway as an MCP tool registry | 🟡 | `cairn.ai.mcp_registry` (migration 0147 `cairn_mcp_servers`; registrations, credentials from secret scopes, th… |
| L156 | The Cairn agent in GitHub Copilot and other AI coding tools | 🟡 | `cairn.ai.ide` and `cairn ide init` (VS Code `.vscode/mcp.json`, the coding agent's `.github/copilot-mcp.json`… |
| L157 | Example agents, skills and plugins | 🟡 | `examples/agents` (quality-triage, pii-steward, change-impact-reviewer, metric-analyst, onboarding-guide, acce… |
| L158 | The AI setup as code | 🟡 | `cairn.aiGateway.cloudUpstreams` (Helm); `model_upstreams`/`bedrock_region` (aws), `foundry` (azure), `vertex`… |
| L159 | Signed releases and channels | 🟡 | `cairn.release` (manifest, feed, publish), `deploy/ops/release.py`, migration 0148, Helm `cairn.updates`. Test… |
| L160 | Scheduled updates and the updater | 🟡 | `cairn.release.updates`, `cairn.release.runner`, `cairn updates apply`, migration 0149, Helm `cairn.updates.ag… |
| L161 | The fleet console | 🟡 | `cairn.fleet` (server, reporter, common), `cairn fleet serve` / `operator`, migration 0150, Helm `cairn.fleet`… |
| L162 | Air-gapped release bundles | 🟡 | `cairn.release.oci` (registry v2 client: by digest only, Basic and token sign-in from a Docker config file, ht… |
| L163 | Workspaces: a SQL editor like the best cloud warehouses' | ✅ | Parts 1-4. Part 4: `sqlworkspace/assist.py` (`/sql/assist/write`, `/explain`, `/fix`: told only the schema the… |
| L164 | Cairn agent kit (in-house) | ✅ | (1) `agentkit/scan.py` (`cairn agents scan`: MCP configs, tool settings and hooks, agents, skills and instruct… |
| L165 | A connector builder and runner for data sources | 🟡 | Part 1: `pipelines/rest_source.py` and SaaS syncs of kind `rest` (a `spec`: records path, id, modified and cur… |
| L166 | Mirrors that replicate a whole database faithfully | 🟡 | PostgreSQL: `pipelines/mirror_db.py`, database mirrors (`/v1/database-mirrors`, `MANAGE_INGESTION`, migration … |
| L167 | Oracle, Db2 and SAP HANA as mirror sources | 🟡 | Oracle built and tested: `pipelines/mirror_oracle.py`, `oracle://` connections on the existing mirrors API (`/… |
| L168 | Catalog connectors proven and widened | ⬜ | Capability audit, November 2026 (Atlan). 25 gallery forms in `sources/catalog.py`; live ingestion evidenced fo… |
| L169 | Sharing beyond plain tables, and clean rooms across organisations | ⬜ | Capability audit, November 2026 (Snowflake secure views, Delta Sharing). Shares carry tables only and refuse m… |
| L170 | Migration tooling from Snowflake and Databricks | ⬜ | Capability audit, November 2026 (Lakebridge, SnowConvert). sqlglot is used only for formatting and validation … |
| L171 | Evaluation for agents and models | ⬜ | Capability audit, November 2026 (MLflow 3 evaluation, Cortex AI Observability). `ai/evals.py` covers built-in … |
| L172 | dbt developed and run in the product | ⬜ | Capability audit, November 2026 (dbt Platform, Fusion). Today: runs, state, docs and metrics import (`pipeline… |
| L173 | Flows that compose | ⬜ | Capability audit, November 2026 (Airflow 3, Astronomer). `flows/model.py` `STEP_TYPES` is fixed and has no flo… |
| L174 | The catalog's daily-use features in Cairn's own UI | ⬜ | Capability audit, November 2026 (Atlan). Several exist only in Cairn Core's UI or are mined but not shown |
| L175 | A Python client for the whole API | ⬜ | Capability audit, November 2026 (pyatlan, Databricks and Snowflake SDKs). `sdks/` has JavaScript and Java |
| L176 | Stateful streaming SQL with a lag target | ⬜ | Capability audit, November 2026. `pipelines/stream_transform.py` refuses joins, aggregates and windows |
| L177 | Curated question spaces | ⬜ | Capability audit, November 2026. Ask-your-data is grounded by the ontology and metrics, with no verified queri… |
| L178 | Reaching data that is elsewhere | ⬜ | Capability audit, November 2026 (Fivetran hybrid deployment, Snowpipe, Auto Loader). Pipes read Cairn Store on… |
| L179 | Machine learning at the vendors' depth | ⬜ | Capability audit, November 2026. L91 says GPU nodes are not covered; `ml/serving.py` has autoscaling only; `pi… |
| L180 | SQL engine gaps | ⬜ | Capability audit, November 2026. Table-level clone and undrop only (L12); functions are SQL and scalar Python;… |
| L181 | Quality and cost at the source | ⬜ | Capability audit, November 2026 (Atlan Data Quality Studio). Monitors run SQL through connections; cost per ta… |
| L182 | SaaS and ERP sources on real accounts, and wider reverse ETL | ⬜ | Capability audit, November 2026. L61 and L62 are tested against emulated APIs |
| L183 | Developer reach | ⬜ | Capability audit, November 2026. Spark Connect is unsupported on purpose today; L134 has eight system tables |
| L184 | An exchange between organisations | ⬜ | Capability audit, November 2026. The marketplace is internal; `marketplace/publish.py` publishes out to Snowfl… |
| L185 | Not code: assurance and live validation | ⬜ | Capability audit, November 2026. Rows 5.5 and L10 (pentest), L135/L136 (private networking) and L125 (cloud mo… |
