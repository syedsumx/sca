---
title: Administration
order: 8
---
# Administration

## Install

* **Docker Compose** for evaluation and small installs: copy `.env.example` to `.env`, set every
  `CHANGE_ME`, then `docker compose -f docker-compose.cairn.yml up -d`. Open port 9000.
* **Kubernetes**: the `cairn` Helm chart. Bring your own MySQL 8, OpenSearch 2 and Kafka 3; put the
  secrets in a Kubernetes Secret named `cairn-core-secrets` (see the chart's values file).

## Accounts

* `admin` is the built-in root account; its password is `CAIRN_ADMIN_PASSWORD`. Use it to set up
  single sign-on and roles, then keep it for break-glass use.
* The control plane runs as `cairn-service`, a least-privilege service account created at install.
  It cannot manage policies, users, credentials, secrets or other people's tokens. Its token is
  rotated every `CAIRN_TOKEN_ROTATE_DAYS` days (default 7).
* User provisioning (SCIM) writes as `cairn-provisioner`, which may manage people and groups and
  nothing else. Keeping it apart means the control plane can never change group membership, and so
  never its own access. Its token is rotated the same way and only the API service can read it.
* People create their own access tokens under **Settings → Access Tokens**.

## Single sign-on

Cairn signs people in with any OpenID Connect identity provider (Okta, Microsoft Entra ID, Google,
Keycloak, Auth0 and others). Password sign-in stays available for `admin`.

1. In the identity provider, create a web application (authorization code flow) and register the
   redirect URI `<CAIRN_PUBLIC_URL>/callback/oidc`, for example
   `https://cairn.example.com/callback/oidc`.
2. Set `CAIRN_SSO_CLIENT_ID`, `CAIRN_SSO_CLIENT_SECRET` and `CAIRN_SSO_DISCOVERY_URL` (the
   provider's `.../.well-known/openid-configuration` address).
3. Check the settings before switching sign-in on:

   ```
   docker compose -f docker-compose.cairn.yml exec cairn-api cairn sso check
   ```

   It prints the redirect URI to register and checks the discovery document, signing keys, PKCE
   support and the client credentials (without signing anyone in). Fix anything marked `FAIL`.
4. Set `CAIRN_SSO_ENABLED=true` and restart `cairn-web`. The login page shows **Sign in with SSO**.

People are created on first sign-in, and the groups in their token's `groups` claim
(`CAIRN_SSO_GROUPS_CLAIM`) become Cairn groups, so personas and policies can refer to them. Other
settings: `CAIRN_SSO_SCOPE` (default `openid email profile`), `CAIRN_SSO_USER_CLAIM` (default
`preferred_username`) and `CAIRN_SSO_REQUIRED_GROUPS` (only members of these groups may sign in).

## User provisioning (SCIM)

With SCIM 2.0 the identity provider creates, updates and deactivates people and groups ahead of
their first sign-in, and removes access when someone leaves.

* Base URL: `<CAIRN_PUBLIC_URL>/cairn/api/scim/v2`
* Authentication: bearer token, the value of `CAIRN_SCIM_TOKEN` (`openssl rand -hex 32`)
* Supported: Users and Groups (create, read, replace, patch, delete), filters on `userName`,
  `externalId`, `displayName` and `id`, pagination.

A SCIM user's `userName` is the Cairn user name, so it must match what single sign-on provides
(`CAIRN_SSO_USER_CLAIM`). Deleting a user deactivates them; their ownership and history stay. Group
membership changes take effect on policy decisions within seconds. When groups come from SCIM, set
`CAIRN_SSO_EXTRACT_GROUPS=false` so sign-in does not also rewrite them.

## AI agents

AI agents act on behalf of a person, never with permissions of their own. What an agent may do is
that person's access, narrowed by the agent's scopes:

| Scope | Allows |
|---|---|
| `read` | Reading the catalog: every read, and requests that change nothing |
| `query_data` | Previewing tables, running read-only SQL, asking questions of data (masked as for the person) |
| `propose` | Creating things that wait for review: documentation drafts, workflows, access requests, discussions |
| `write` | Changing metadata directly |

Administration (`/admin`, licences, policies, other agents) is never reachable with an agent
credential. Every audit entry made through an agent records it as `details.agent`.

**AI tools people connect** (Claude Desktop, Claude Code, IDEs) sign in with OAuth 2.1, as the MCP
specification defines:

1. The tool registers itself.
2. The person signs in as usual and sees a consent page listing the scopes; they can untick any.
3. The tool receives a one-hour access token and a refresh token, which rotates on each use.

A refresh token or sign-in code used twice ends the whole grant. People see and disconnect their
agents in **Settings → AI agents**.

`CAIRN_OAUTH_REGISTRATION` controls registration:

- `open` (default): new tools can connect straight away.
- `admin`: new tools wait until an administrator enables them.
- `off`: only agents an administrator created.

**Automations** get an agent key instead. In **Settings → AI agents → New automation key**, choose
a name, the person it acts as, and its scopes. The key (`ca_…`) is shown once; **New key** rotates
it. To make an agent depend on the AI system register, link it to an AI system
(`PATCH /admin/agents/{id}` with `ai_system`): it stops working whenever that system is not approved.

**Controlling agents** (Settings → AI agents, for administrators):

- **Pause all agents** is the kill switch. Every agent credential is refused, with the reason, until
  you resume them. People are unaffected.
- **Quotas:** each agent has a limit per minute (default 120) and per day (default 20,000). Over a
  limit it gets `429` with `Retry-After`. Requests refused this way do not count against the daily
  quota. The list shows each agent's requests today.
- **Autonomy:**
  - *Suggestions wait for review* (the default).
  - *Apply confident suggestions*: an agent's suggestion at or above the confidence you set applies
    at once, only if the person it acts for may make that change; otherwise it waits like any other.
    Auto-applied suggestions show as *auto accepted* in the AI steward history and can be undone
    there.

**Seeing what agents do:**

- Each agent's **Activity** lists its recent tool calls: tool, arguments (personal data redacted),
  outcome, time taken, and whether catalog text in the result was flagged.
- **Datasets read**, next to an agent's activity, lists the datasets the agent read (data and metadata) over the last 30 days (`GET /cairn/api/admin/agents/{id}/datasets`). People see the same for the agents connected to them.
- Administrators see every agent's activity. People see what agents did for them, in Settings →
  AI agents.
- `/admin/agents/stats` sums calls, errors, refusals and flagged text per agent and per tool over a
  day. Prometheus gets `cairn_agent_tool_calls_total`, `cairn_agent_tool_seconds` and
  `cairn_ai_injection_flagged_total`; the alerts `CairnAgentErrorsHigh` and `CairnInjectionFlagged`
  watch them.
- **Feedback:** people rate answers from the assistant and from *Ask your data* with a thumbs up or
  down. `GET /ai/feedback` summarises the ratings, and `GET /ai/feedback/eval-cases` exports the
  down-voted questions as `cairn ai eval` cases. Fill in what a right answer must contain and add
  them to the eval suite, so mistakes people saw stay fixed.

## Sources

**Settings → Sources** manages where the catalog's metadata comes from. It needs `MANAGE_INGESTION`.

**Add a source** from the gallery, which covers:

- warehouses: Snowflake, BigQuery, Redshift;
- lakes: Databricks Unity Catalog, S3, Glue;
- databases: PostgreSQL, MySQL, SQL Server, Trino, MongoDB;
- dbt Core and dbt Cloud;
- BI tools: Looker, Tableau, Power BI, Superset, Preset, Metabase, Sigma, Qlik Sense;
- machine learning: MLflow, Amazon SageMaker, Vertex AI;
- Kafka.

For anything else, **Another connector** takes a recipe.

Each form asks for the connection details:

- **Secrets by name only.** A password field takes the *name* of a secret, never its value. The recipe holds a `${NAME}` reference, resolved by whatever runs the source: Cairn's secret store, or the secrets backend of an in-VPC agent pool. A recipe with an inline credential is refused.
- **Runs on.** Choose Cairn's executor, or an agent pool for sources only reachable inside your network. See [Remote agents](remote-agents.html).
- **Schedule.** A cron expression and time zone, or manual runs only.
- **Tell about failures.** The people to notify; by default, whoever set up the source.
- **Advanced options.** JSON merged into the connector's config, for options the form doesn't show.

**Test connection** checks the details on the executor that will run the source, so a pool source is tested by that pool's agents. Nothing is ingested. The result says whether Cairn could connect, and which capabilities (lineage, usage, profiling) the credentials allow.

**Runs and logs.** The list shows each source's last run. **Runs** lists the last 20 runs with their duration, the metadata changes they sent and any failures. **Log** shows a run's output; for pool runs, these are the lines the agent shipped, with secrets removed.

**Health.** Every source is one of:

| State | Meaning |
|---|---|
| healthy | the last run succeeded |
| running | a run is in progress |
| failing | the last run failed, or none has succeeded |
| stale | scheduled, but no successful run for more than twice its interval (at least a day for daily sources) |
| never run | no run yet |

The notifier checks health every five minutes. When a source becomes failing or stale, its owners get a `source.failing` or `source.stale` notification; when it recovers, they get `source.recovered`. Route these to a channel under notification routes. Prometheus gets `cairn_ingestion_sources{state}`, and the alert `CairnSourceFailing` fires after 30 minutes.

The API is `/cairn/api/sources`:

- `GET /connectors` and `GET /executors`;
- `GET` or `POST` on the collection;
- `GET`, `PUT` or `DELETE` on `/{urn}/detail`;
- `POST /{urn}/run`;
- `POST /test`, then poll `GET /test/{id}`;
- `GET /runs/{run urn}`.

## Main settings

| Setting | Purpose |
|---|---|
| `CAIRN_ADMIN_PASSWORD` | root account password |
| `CAIRN_PUBLIC_URL` | URL users reach Cairn on (used in notification links) |
| `CAIRN_AUTHZ_MODE`, `CAIRN_NATIVE_POLICIES_ENABLED` | additive or authoritative governance |
| `CAIRN_LLM_PROVIDER`, `CAIRN_AI_MODEL`, `ANTHROPIC_API_KEY`, `CAIRN_OLLAMA_URL` | assistant model |
| `CAIRN_SLACK_*`, `CAIRN_TEAMS_WEBHOOK_URL`, `CAIRN_SMTP_URL` | notification channels |
| `CAIRN_EMBED_ALLOWED_ORIGINS` | sites allowed to embed Cairn components |
| `CAIRN_TOKEN_ROTATE_DAYS`, `CAIRN_TOKEN_GRACE_MINUTES` | service token rotation |
| `CAIRN_SSO_*` | single sign-on (see above) |
| `CAIRN_SCIM_TOKEN` | SCIM provisioning token |
| `CAIRN_MCL_INVALIDATION` | apply metadata changes to access decisions at once (default `true`) |

Secrets (`CAIRN_*_SECRET`, `*_KEY`, `*_PASSWORD`) must be long random values
(`openssl rand -hex 32`).

## Network

Users only need the edge (port 9000, or your ingress). The internal APIs are not exposed; developer
consoles are disabled at the edge.

**HTTPS.** Put the certificate chain and key (PEM) in `deploy/tls/` as `tls.crt` and `tls.key`, or
point `CAIRN_TLS_DIR` at another directory, and restart the edge. It then also serves HTTPS on port
9443 (`CAIRN_MAPPED_EDGE_TLS_PORT`) with TLS 1.2+ and HSTS. Set `CAIRN_TLS_REDIRECT=true` to send
plain HTTP to HTTPS (`CAIRN_TLS_PUBLIC_PORT` is the port users reach, default 443), and set
`CAIRN_PUBLIC_URL` to the `https://` address. On Kubernetes, TLS is the ingress's job.

**Rate limits.** Per client address: password sign-in 10 attempts a minute (bursts of 20), the Cairn
API 50 requests a second (bursts of 400). Over the limit, the edge answers `429 Too Many Requests`.

## Secrets as files

Any `CAIRN_*` secret can be given as a file instead: `CAIRN_DB_PASSWORD_FILE=/run/secrets/db` sets
`CAIRN_DB_PASSWORD` to the file's content (Docker secrets, Kubernetes secret volumes, Vault agent).
Setting both forms is an error, so a stale value cannot hide the file. `ANTHROPIC_API_KEY_FILE` works
the same way.

## Reports

**Reports** in the workspace answers the two questions that fund a catalog: is it used, and is the estate getting governed? It has four sections:

- **Coverage:** the share of datasets that are described, owned, classified, certified and monitored. It is shown overall, by domain and by platform. Click any figure to list the datasets that lack it.
- **Adoption:** weekly and monthly active users (people who opened an asset), asset views over 7 days, access requests over 30 days (submitted, approved and pending), and the median time to a decision.
- **Quality:** open incidents, the monitored share, and the mean time to resolve. The resolve time is measured from a monitor's first failure to its next pass, over 30 days.
- **Governance:** personas, purposes, active grants, and grants that expire within 7 days.

A snapshot is computed once a day by the notifier and kept per day, so the history shows the trend. **Recompute now** refreshes it on demand, and **Download CSV** exports coverage by domain and by platform. Reports are for administrators (`MANAGE_GLOBAL_SETTINGS`). Set `CAIRN_REPORTS_OPEN=true` to show them to everyone who is signed in; the drill-down lists still show each person only the datasets they may see.

The API, under `/cairn/api/reports`:

- `GET` returns the latest snapshot,
- `POST /refresh` recomputes it,
- `GET /history?days=90` returns the headline figures per day,
- `GET /coverage.csv` exports coverage,
- `GET /assets?missing=owner&domain=urn:li:domain:finance` lists the datasets behind a figure (`domain=none` means no domain; `platform=` filters by platform).

### Cost per asset

**Reports → Cost** lists two kinds of table:

- **Idle tables:** no reads in 30 days and nothing downstream in lineage, ranked by what they cost to store.
- **Expensive per read:** tables whose storage and query cost is high for how rarely they are read.

**Propose deprecation** notifies the table's owners and followers with the figures behind the proposal.

Daily figures per table come from warehouse collectors, which run once a day for the previous day. Configure them in `CAIRN_COST_SOURCES`, a YAML file whose `sources` entries use the named connections:

- **Snowflake** (`platform: snowflake`) reads `ACCOUNT_USAGE`:
  - storage from `TABLE_STORAGE_METRICS`, priced at `storage_usd_per_tb_month` (default 23),
  - compute from `QUERY_ATTRIBUTION_HISTORY` credits, priced at `credit_usd` (default 3), split evenly across the tables each query read (`ACCESS_HISTORY`).

  The connection's role needs access to the `SNOWFLAKE` database.
- **BigQuery** (`platform: bigquery`, `region: region-us`) reads `INFORMATION_SCHEMA`:
  - `TABLE_STORAGE` logical bytes, at `storage_usd_per_gib_month`,
  - `JOBS` bytes billed, at `usd_per_tib`, split across the referenced tables. With editions pricing (`pricing: editions`), it uses slot-hours (`total_slot_ms`) at `usd_per_slot_hour` (default 0.06) instead.
- **Databricks** (`platform: databricks`) reads the system tables:
  - list-price dollars per SQL warehouse and per job for the day (`system.billing.usage` with `system.billing.list_prices`);
  - a warehouse's cost is split across its statements by duration (`system.query.history`), and a job's across its runs;
  - each statement or job then splits its share across the tables it read (`system.access.table_lineage`).

  The system tables have no per-table storage figure, so Databricks rows carry compute only. The connection needs `SELECT` on those system schemas.
- **Redshift** (`platform: redshift`) splits the day's compute across queries by elapsed time (`SYS_QUERY_HISTORY`), then across the tables each query scanned (`SYS_QUERY_DETAIL`):
  - the day's compute is the cluster's cost per day (`daily_compute_usd`);
  - for Serverless (`serverless: true`), it's RPU-seconds from `SYS_SERVERLESS_USAGE`, at `usd_per_rpu_hour` (default 0.375).

  Storage is the `SVV_TABLE_INFO` size, at `storage_usd_per_gb_month` (default 0.024). The connection's user needs to see every user's queries (`SYSLOG ACCESS UNRESTRICTED`).

For other sources, post daily rows to `POST /cairn/api/reports/costs` (`{"rows": [{urn, day, storage_bytes, storage_usd, compute_usd, reads}]}`). Posting a dataset and day again replaces that row.

The rest of the API is under `/cairn/api/reports/costs`:

- `GET /idle?days=30` and `GET /expensive?days=30` return the two lists,
- `GET /asset?urn=…` returns one table's daily series,
- `POST /propose-deprecation` with `{"urn", "reason"}` sends the proposal.

## One database

By default the catalog stores its metadata in MySQL, and Cairn's own data is in PostgreSQL. You can put the catalog in PostgreSQL too. Then you have one database server to run, back up and patch, and no MySQL licence to review.

- **Compose:** add `docker-compose.postgres.yml` to every command:

  ```bash
  docker compose -f docker-compose.cairn.yml -f docker-compose.postgres.yml up -d
  ```

  The catalog goes into a `cairn_catalog` database on the `cairn-postgres` server, which a one-off job creates. MySQL is not started.
- **Helm:** set `core.database.type: postgres` and point the host, port (5432), name and user at the server. Create the database before installing; the setup job then creates its tables. `core.database.sslMode` sets the JDBC `sslmode` (default `prefer`).
- **Backups:** `deploy/ops/backup.sh` and `restore.sh` find the catalog wherever it is. Pass the same compose files in `COMPOSE`.

Choose this when you install. Switching an existing deployment does not move its catalog: the new catalog database starts empty. Re-ingest your sources into it, or restore the backup of a deployment that already ran on PostgreSQL.

## Licence

Cairn checks its licence offline. A licence key is signed text naming:

- the customer, the edition and any extra features,
- the number of people who may use Cairn,
- the expiry date.

Install it in **Settings → Licence**, with `cairn licence install <file>`, or from a Secret (`cairn.licence.existingSecret` on Helm). The key is checked when you install it: a key that was altered, that is for another tenant, or that has an unknown signature is refused.

| State | What happens |
|---|---|
| No licence | Evaluation mode: everything works. A deployment with `CAIRN_LICENCE_REQUIRED=true` (hosted tenants) is read-only instead. |
| Valid | Everything the edition includes works. AI, automations, warehouse tag sync, ticketing, data preview and SQL, and cost per asset are in the enterprise edition, or listed as extra features; anything else answers 402. |
| Expired | For the licence's grace period (14 days by default) everything keeps working, with a banner. After that Cairn is read-only. |
| More people than seats | A seat is anyone who used Cairn in the last 30 days, in order of first use. People beyond the limit can read but not change anything. |

**Read-only** means changes are refused with HTTP 402. Reading, searching, the assistant's answers, signing in, user provisioning and installing a new licence all keep working. Service accounts are never limited.

Everyone sees a banner when the licence needs attention. `cairn licence show` prints the state. The metrics `cairn_licence_days_left`, `cairn_licence_seats` and `cairn_licence_read_only` drive three alerts: expiring, read-only, and seats full.

## Hosting tenants

For a hosted offering, each tenant gets its own Cairn release in its own Kubernetes namespace. `deploy/hosting/tenant.sh` creates, upgrades and removes tenants:

```bash
export CAIRN_HOST_DOMAIN=cairn.example.cloud CAIRN_PG_HOST=pg.internal CAIRN_PG_ADMIN_URL=postgresql://admin@pg.internal/postgres \
       CAIRN_SEARCH_HOST=search.internal CAIRN_KAFKA_TEMPLATE='kafka.cairn-{id}:9092'
deploy/hosting/tenant.sh create acme --licence acme.licence --values platform.yaml
deploy/hosting/tenant.sh upgrade acme [--licence renewed.licence]
deploy/hosting/tenant.sh delete acme [--drop-data]
deploy/hosting/tenant.sh list
```

**What a tenant gets:**

- **Hostname:** `https://<id>.<domain>`.
- **Databases:** the Cairn database and the catalog database on the shared PostgreSQL server, owned by the tenant's own role. The catalog runs in PostgreSQL ([One database](#one-database)).
- **Search:** indices in the shared OpenSearch cluster under the prefix `<id>_`.
- **Kafka:** its own.
- **Kubernetes:** a ResourceQuota, network policies that admit only the tenant's own pods and the ingress, and credentials generated for it (never printed).

The tenant's licence must name its id. The release runs with `cairn.licence.required: true`, so a tenant without a valid licence can't change anything. Add `--dry-run` to `create` to render the manifests without touching a cluster.

## Monitoring

Every Cairn service exposes Prometheus metrics at `/metrics`. Workers without an HTTP API (notifier, scheduler, context miner) serve them on `CAIRN_METRICS_PORT`; Helm sets it to 9108.

**Metrics:**

| Metric | What it measures |
|---|---|
| `cairn_http_requests_total`, `cairn_http_request_seconds` | requests and latency per service and route |
| `cairn_pdp_decisions_total{decision,mode}`, `cairn_pdp_decision_seconds` | authorization decisions and their latency |
| `cairn_quality_evaluations_total{type,status}`, `cairn_quality_incidents_total{event}` | monitor runs and incidents |
| `cairn_notifications_total{channel,outcome}`, `cairn_notifications_pending` | deliveries and backlog |
| `cairn_ai_tokens_total`, `cairn_ai_cost_usd_total`, `cairn_ai_budget_refusals_total` | AI use per task |
| `cairn_audit_events_total{category}` | audit log entries |
| `cairn_worker_last_success_timestamp_seconds{worker}`, `cairn_worker_errors_total` | background worker health |
| `cairn_proxy_masked_total{policy}` | values the core proxy masked |

**Dashboards and alerts** live in `deploy/observability/`:

- `cairn-overview.json`, a Grafana dashboard,
- `cairn-alerts.yaml`, Prometheus rules (checked with `promtool` in CI) covering: a service down, server errors above 5%, slow requests, slow policy decisions, a spike in denials, a stalled or failing worker, a notification backlog or failing channel, a burst of quality incidents, the AI budget reached, and the licence expiring, read-only or out of seats.

**To run them:**

- **Compose:** `docker compose -f docker-compose.cairn.yml -f docker-compose.observability.yml up -d` adds Prometheus (`localhost:9090`) and Grafana (`localhost:3300`, with `GRAFANA_ADMIN_PASSWORD` in `.env`), pre-loaded with both.
- **Helm:** `cairn.serviceMonitor.enabled` scrapes the services and the workers (a ServiceMonitor and a PodMonitor). `cairn.prometheusRule.enabled` installs the rules, and `cairn.grafanaDashboard.enabled` installs the dashboard as a ConfigMap for the Grafana sidecar.

**Tracing** is optional OpenTelemetry. Install the `otel` extra and set `CAIRN_OTEL_ENDPOINT` to an OTLP/HTTP collector. Requests, outgoing calls and database queries are then traced, sampled at `CAIRN_OTEL_SAMPLE_RATIO` (default 10%). Health and metrics endpoints are not traced.

## Limits

These are measured on the reference single-node Compose stack: 4 vCPU, 16 GB, one replica of each service and a catalog of about 110 datasets and 30 data products. The load is 10 concurrent clients. They are the numbers `tests/load/limits.yaml` holds Cairn to, with headroom over the measurement.

| Path | Throughput | p95 latency |
|---|---|---|
| Policy decision (`/governance/v1/authorize`) | at least 200 per second | under 100 ms |
| Badges for 20 search results (`/collab/enrich`) | at least 25 per second | under 600 ms |
| Dataset health | at least 90 per second | under 250 ms |
| Marketplace page | at least 14 per second | under 1.5 s |
| Inbox | at least 90 per second | under 250 ms |
| Contracts | at least 130 per second | under 200 ms |

On this node, throughput stops growing at about 10 concurrent clients; past that, only latency rises. Add replicas of the API and the policy service to serve more (see [High availability](#high-availability)).

**To measure your own deployment**, run the load test against it with a token that can see the catalog:

```bash
python tests/load/loadtest.py --api https://cairn.example.com/cairn/api --token "$TOKEN" --users 10 --duration 30
```

The command exits non-zero if a path breaks its limit. Pass `--limits your-limits.yaml` to check your own targets, and `--json out.json` to keep the measurements.

## High availability

`helm/cairn/values-ha.yaml` is a profile for production clusters:

```bash
helm install cairn ./helm/cairn -f my-values.yaml -f helm/cairn/values-ha.yaml
```

- **Stateless services** (API, policy service, core proxy) run 3 replicas, and the MCP server and workspace UI run 2. Each component's replicas are spread over nodes and zones (`cairn.ha.spreadReplicas`), and a PodDisruptionBudget keeps enough of them up during node drains.
- **Workers** (scheduler, notifier, context miner) run 2 replicas. A Postgres advisory lock makes one of them the leader for each cycle, so work is never done twice. If the leader's node goes away, the standby takes over on its next cycle.
- **The gateway** runs more than one replica only when its keys come from a shared Secret (`gateway.keys.existingSecret`). Without one, the keys live on a volume, the gateway runs a single replica and it gets no PodDisruptionBudget.
- **Databases:** point `global.*` at managed, replicated Postgres, MySQL and OpenSearch. The databases the chart can run are single instances.

## Cairn Store

Cairn Store is Cairn's own S3-compatible object store, for lakehouse tables (Iceberg data and metadata) and metadata exports. Any S3 client works with it: Trino, Spark, pyiceberg, pyarrow, DuckDB, the AWS CLI and SDKs.

- **Docker Compose:** service `cairn-store`, S3 endpoint `http://cairn-store:8333` inside the stack and `http://localhost:8333` from the host. Set `CAIRN_STORE_ACCESS_KEY` and `CAIRN_STORE_SECRET_KEY` in `.env`; the bucket `lakehouse` is created at start.
- **Kubernetes:** set `cairn.store.enabled=true` and create the Secret named in `cairn.store.secret`. The chart runs one replica on a persistent volume. Engines in other namespaces need an entry in `cairn.networkPolicy.storeIngressFrom`.

**Keys.** The access key from the environment is an admin key. `CAIRN_STORE_CREDENTIALS_FILE` adds more keys, as a JSON list. Each key may be limited to certain buckets (`"buckets": ["lake-*"]`), to reading (`"read_only": true`), or by its own IAM-style policy (`"policy": {...}`, which replaces the bucket list). Only admin keys (`"admin": true`) create and delete buckets and change bucket settings, unless a key policy grants it. Requests are signed with AWS Signature Version 4, or Version 2 for older tools.

**Access policies.** Each bucket can have a bucket policy in the S3 policy language (`PutBucketPolicy`). It can grant other keys access to parts of the bucket, or deny access under conditions. Supported conditions: source IP, time, object tags (`s3:ExistingObjectTag/<key>`, `s3:RequestObjectTag/<key>`), list prefix, signature type, user agent and referer. An explicit `Deny` always wins, even for admin keys; admin keys can still read, change and delete the bucket policy, so a policy cannot lock them out. Policies are checked when they are stored: an unknown operator or condition key is refused with `MalformedPolicy`, never stored and ignored.

**Public access.** Anonymous requests are refused, and public bucket policies are rejected, unless `CAIRN_STORE_BLOCK_PUBLIC_ACCESS=false`. With it off, a bucket policy for `"Principal": "*"` can allow anonymous reads (for example, to serve static files), and each bucket's public access block (`PutPublicAccessBlock`) can still restrict that. `GetBucketPolicyStatus` reports whether a bucket is public.

**Cairn policies.** With `CAIRN_STORE_PDP_URL` (for example `http://cairn-pdp:8810`) and `CAIRN_STORE_PDP_TOKEN` (a Cairn service token), the store also asks Cairn's policy decision point about every request its own rules allow. It asks as the key's Cairn identity (`"actor"` in the credentials file), with the privilege named after the S3 action (`s3:GetObject`) and the resource `arn:aws:s3:::bucket/key`. A Cairn `deny` policy refuses the request. While the decision point cannot be reached, requests are refused (fail closed). Decisions are cached for 30 seconds. In Helm, set `cairn.store.pdp.enabled=true`.

**What it supports.** Buckets; objects with metadata; ranged reads; conditional reads and writes (`If-Match`, `If-None-Match: *`); listing (versions 1 and 2); batch deletes; copies; multipart uploads, including copying parts; checksums (CRC32, CRC32C, CRC64NVME, SHA-1, SHA-256, composite and full-object); presigned URLs; and object and bucket tags (up to 10 per object, set with the object, by copy or later, and 50 per bucket).

**Ownership and ACLs.** The bucket owner owns every object (S3's `BucketOwnerEnforced`). Access comes from keys, never from ACLs: ACL requests that only grant the owner full control succeed, and any other grant is refused with `AccessControlListNotSupported`.

**CORS.** Each bucket can have CORS rules (`PutBucketCors`), so browser apps such as the Cairn UI can upload and download with presigned URLs. Browser preflight requests are answered from the bucket's rules without a signature. Responses to allowed origins carry the CORS headers, error responses included. A bucket without rules refuses every cross-origin request.

**Versioning.** Turn it on per bucket (`PutBucketVersioning`, `Enabled` or `Suspended`). Overwrites and deletes then keep the previous version. A delete adds a delete marker, and deleting the marker brings the object back. Any version can be read, copied, tagged or permanently deleted by its version ID; `ListObjectVersions` lists them all. Old versions count towards stored bytes until they are permanently deleted (or expired by a lifecycle rule, once those are supported). Reading or deleting a specific version needs the version permissions (`s3:GetObjectVersion`, `s3:DeleteObjectVersion`). MFA delete is not supported.

**Lifecycle rules.** Each bucket can have lifecycle rules (`PutBucketLifecycleConfiguration`). Rules can expire current objects a number of days after they were written, or on a date. They can also remove delete markers that are the last thing left of an object, expire noncurrent versions (optionally keeping the newest few), and abort multipart uploads left unfinished. A rule can select objects by prefix, tags and size. The store applies the rules in the background, every `CAIRN_STORE_LIFECYCLE_INTERVAL` seconds (default 3600). It only expires the version it checked, so an object rewritten in the meantime is kept. Responses for objects that will expire carry `x-amz-expiration`. Metrics: `cairn_store_lifecycle_*_total`. Transitions to other storage classes are refused for now.

**Object lock.** Buckets created with object lock (`x-amz-bucket-object-lock-enabled: true`), or versioned buckets that get an object lock configuration, keep versions write-once:

- **Retention.** A version can carry retention until a date, in `GOVERNANCE` or `COMPLIANCE` mode, set per object or from the bucket's default retention.
- **Compliance mode.** The version cannot be deleted, and its retention cannot be shortened, by anyone, including admin keys, until the date passes.
- **Governance mode.** The same, unless a request with `x-amz-bypass-governance-retention: true` comes from a key allowed `s3:BypassGovernanceRetention`.
- **Legal hold.** A legal hold blocks deletion until it is removed.
- **Where it is enforced.** The engine enforces protection, so client deletes, batch deletes and lifecycle rules all respect it. Overwrites and plain deletes keep the locked version, as a noncurrent version under a delete marker.
- **Versioning.** Versioning cannot be suspended on such a bucket.

**Browser uploads.** HTML forms can upload straight to a bucket with a signed POST policy, as produced by any AWS SDK's `generate_presigned_post` (SigV4 or SigV2). The store checks the signature and the expiration. It also checks every condition (exact values, `starts-with`, `content-length-range`), and that every form field is covered by one, so a browser cannot add fields the policy did not allow. The upload is authorized as a PUT for the key that signed the policy. Supported form fields: object metadata, content type, tags, encryption, object lock, `success_action_status` and `success_action_redirect`. The file is streamed to disk and its size limits are enforced as it arrives. Allow the web app's origin with CORS rules.

**TLS.** Set `CAIRN_STORE_TLS_CERT` and `CAIRN_STORE_TLS_KEY` (PEM files) to serve HTTPS. The files are re-read when they change, so renewed certificates take effect without a restart, and a broken renewal is refused while the current certificate stays in use.

- **Mutual TLS.** `CAIRN_STORE_TLS_CLIENT_CA` requires client certificates signed by that CA. Requests are still signed with the client's key; mutual TLS adds proof of which machine is connecting.
- **What TLS turns on.** With TLS, `aws:SecureTransport` is true in policies, and SSE-C is accepted.
- **Outbound HTTPS.** Webhooks and Cairn's policy decision point can be `https://`. They trust the public roots plus `CAIRN_STORE_CA_FILE`.
- **Helm.** `cairn.store.tlsSecret`, a `kubernetes.io/tls` Secret (cert-manager works), and optionally `cairn.store.clientCaSecret`.
- **Kafka.** Event delivery to Kafka is plaintext for now; keep the broker inside the deployment's network.

**Audit trail.** The store keeps an append-only, hash-chained audit trail in Cairn's audit format: each entry's hash is `sha256(prev_hash ‖ canonical JSON)`, so any change to a past entry, or a missing entry, is detected. An entry is written in the same transaction as the change it records. Recorded:

- bucket creation and deletion;
- every bucket configuration change (policy, CORS, lifecycle, encryption, website, notifications, logging, tags, public access block, object lock), with the document's SHA-256;
- versioning changes and quotas;
- object-lock retention and legal-hold changes, and deletes that bypassed governance retention;
- requests from signed keys that were refused. Anonymous refusals are only logged.

Export the trail with `GET /-/admin/v1/audit?after=<seq>&format=jsonl` (SigV4, admin key) and check it with `cairn audit verify-file` (use `--after-head` to prove that consecutive exports join up). On a stopped store, `cairn-store audit-verify -data /data` checks the chain too.

**Access logs.** `PutBucketLogging` writes a bucket's requests, in S3's server access log format, to objects in a target bucket every `CAIRN_STORE_ACCESS_LOG_FLUSH_SECONDS` (default 60). The requester must be allowed to write to the target. Like S3's, these logs are best effort; the audit trail is the record of changes. `CAIRN_STORE_LOG_REQUESTS=true` also writes one structured JSON log line per request.

**Event notifications.** Buckets can send events when objects are created (`Put`, `Post`, `Copy`, `CompleteMultipartUpload`), removed (`Delete`, `DeleteMarkerCreated`), tagged or expired by lifecycle rules. Rules are set with `PutBucketNotificationConfiguration` and can filter by key prefix and suffix. Events use S3's event JSON format.

- **Targets.** Events go to targets the operator defines in `CAIRN_STORE_EVENT_TARGETS_FILE`: webhooks (`{"type": "webhook", "id": "cairn", "url": "http://...", "secret": "..."}`) and Kafka topics (`{"type": "kafka", "id": "lake", "brokers": ["kafka:9092"], "topic": "..."}`). A rule names its target as the last part of the destination ARN (`arn:cairn:store:::lake`).
- **Webhook signatures.** Webhook bodies are signed: `X-Cairn-Signature: sha256=<HMAC-SHA256 of the body with the target's secret>`.
- **No lost events.** An event is recorded in the same transaction as the change it describes. It is delivered at least once, in order for each target, with retries and backoff, and survives a crash or restart of the store.
- **Delivery that keeps failing.** Events still undeliverable after `CAIRN_STORE_EVENT_MAX_AGE_HOURS` (default 168) are dropped and counted.
- **Metrics.** `cairn_store_events_{delivered,failed,dropped}_total` and `cairn_store_events_pending`.
- **Helm.** Put the targets file in a Secret and set `cairn.store.eventTargetsSecret`.

**Static websites.** With `CAIRN_STORE_WEBSITE_DOMAIN` set (for example `sites.example.com`), a bucket with a website configuration (`PutBucketWebsite`) is served at `<bucket>.sites.example.com`. Configurations support index and error documents, redirect-all, routing rules that redirect by key prefix or error code, and per-object redirects (`x-amz-website-redirect-location`). Website requests are anonymous, so a site is served only if its bucket policy grants public read and public access is allowed: hosting never exposes a bucket by itself.

**Other S3 calls.** `GetObjectAttributes` (ETag, checksums, parts with their checksums, size). Request payment always answers `BucketOwner`, and setting `Requester` is refused. Transfer acceleration can only be `Suspended`. Replication and intelligent tiering are refused with `NotImplemented` until their features exist.

**Encryption at rest.** Give the store a key and it encrypts every new object (SSE-S3): set `CAIRN_STORE_MASTER_KEY` (a base64 32-byte key, `openssl rand -base64 32`), or `CAIRN_STORE_KEYRING_FILE` for several keys. Each object gets its own data key; its bytes are encrypted with AES-256-GCM in 64 KiB chunks, so ranged reads stay fast and any tampering is detected. The data key is stored only wrapped by a keyring key.

- **SSE-KMS.** Clients and bucket defaults can name a keyring key (`aws:kms` with a key id), for example one key per team.
- **SSE-C.** Customer-provided keys are supported; the store never stores them. They need TLS, or `CAIRN_STORE_SSE_C_ALLOW_HTTP=true` when TLS ends at a proxy in front of the store.
- **Bucket defaults.** `PutBucketEncryption` sets a bucket's default encryption.
- **Key rotation.** Add a key to the keyring and point `sse_s3_key` at it. Existing objects keep the key they were written with, so keep old keys until those objects are rewritten or deleted. An object whose key has been removed can no longer be read.
- **Back up the keyring.** Without it, encrypted objects cannot be recovered.

Objects written before a key was set stay unencrypted. Requests to set them are refused with `NotImplemented`; Cairn Store never accepts a setting it would ignore.

**Durability.** A write is acknowledged only once its bytes and its index entry are on disk. It is readable immediately afterwards. Conditional writes are atomic. After a crash the store keeps every acknowledged write and drops any half-finished ones when it starts. `cairn-store fsck -data /data` checks a stopped store and reports objects whose data is missing.

**Limits.** This release runs on one node: there is no replication, so the volume is the only copy. Put it on reliable storage and back it up: stop the store and copy `/data`, or take a volume snapshot. Objects can be up to 5 GiB in one request, and up to 10,000 parts of 5 GiB each with multipart uploads.

**Quotas and the admin API.** Cairn's admin API for the store lives under `/-/admin/v1/`. Requests are signed with SigV4 by an admin key, so any AWS SDK signer works; bodies and responses are JSON.

- **Statistics:** `GET stats`.
- **Buckets:** `GET buckets` and `GET buckets/<name>` report each bucket's usage (stored bytes, including old versions and unfinished uploads, and the number of object versions) and its settings.
- **Quotas:** `PUT buckets/<name>/quota` with `{"max_bytes": ..., "max_objects": ...}` sets a quota, and `DELETE` removes it.

A write that would take a bucket over its quota is refused with `QuotaExceeded`, inside the same transaction, so concurrent uploads cannot slip past the limit. Deletes and overwrites that shrink a bucket always work. Usage is updated with every write, and recounted from what is stored each time the store starts.

**Storage in the Cairn UI.** Cairn admins see **Settings → Storage**, which lists:

- every bucket, with its usage, its quota, and whether it has versioning, object lock, default encryption, lifecycle rules or a bucket policy;
- quotas, which can be set and removed from the page;
- the store's audit trail, with a button that verifies the hash chain from the beginning.

The control plane calls the store's admin API with the store's admin key (`CAIRN_STORE_URL`, `CAIRN_STORE_ADMIN_ACCESS_KEY`, `CAIRN_STORE_ADMIN_SECRET_KEY`; set automatically in Compose and Helm), and records quota changes in Cairn's own audit log too. Users never see the key.

**On the command line.** `cairn-store admin stats | buckets | bucket <name> | quota <name> --max-bytes N --max-objects N | quota-remove <name> | audit [--after SEQ] [--jsonl]` calls the same API. It is signed with `CAIRN_STORE_ACCESS_KEY` and `CAIRN_STORE_SECRET_KEY` (an admin key), against `CAIRN_STORE_ENDPOINT` or the local listener.

**Monitoring.** `/-/healthy`, `/-/ready` and Prometheus metrics at `/-/metrics`: requests by operation and status, bytes in and out, buckets, objects, stored bytes, open uploads and noncurrent versions, per-bucket bytes and objects (`cairn_store_bucket_bytes`, `cairn_store_bucket_objects`), and lifecycle actions.

## Cairn Catalog

Cairn Catalog is Cairn's Iceberg REST catalog. Engines that speak the Iceberg REST protocol connect to it: Trino, Spark, Flink, pyiceberg and DuckDB. Table data and metadata files live in Cairn Store (or any S3 store); the catalog keeps track of each table's current metadata.

- **Docker Compose:** service `cairn-catalog`, at `http://cairn-catalog:8181` inside the stack and `http://localhost:8181` from the host. Set `CAIRN_CATALOG_TOKEN` in `.env`. The catalog writes to the store with the store's key, and new tables go under `s3://lakehouse/warehouse`.
- **Kubernetes:** set `cairn.catalog.enabled=true` (with `cairn.store.enabled=true`, or point `cairn.catalog.s3Endpoint` at another store). Create the Secret named in `cairn.catalog.secret`. Engines in other namespaces need an entry in `cairn.networkPolicy.catalogIngressFrom`.

**Connecting an engine.** Use the catalog URL and the token as a bearer token. For example, in pyiceberg: `RestCatalog("cairn", uri="http://cairn-catalog:8181", token="...")`. With each table the catalog also gives the engine the store's endpoint and key, so engines need no storage settings of their own. Set `CAIRN_CATALOG_VEND_CREDENTIALS=false` to turn that off. Set `CAIRN_CATALOG_CLIENT_S3_ENDPOINT` when engines reach the store at another address.

**Tokens.** `CAIRN_CATALOG_TOKEN` is an admin token. `CAIRN_CATALOG_TOKENS_FILE` adds more tokens as a JSON list. Each token can be limited to namespaces (`"namespaces": ["sales", "sales.*"]`) or to reading (`"read_only": true`).

**What it supports.**

- Namespaces, nested, with properties.
- Tables: create (including staged creates for create-table-as-select), load, commit, rename, drop, and register an existing metadata file.
- Multi-table transactions, and metrics reports (accepted and dropped).

Views, server-side scan planning and remote request signing are not supported yet.

**Commits.** Table metadata is checked and updated by iceberg-rust, the Apache Iceberg project's Rust library, so the catalog applies the table spec the same way the reference implementations do.

1. A commit writes a new metadata file.
2. The catalog then switches the table to that file, but only if the table has not changed since the commit was prepared.
3. A writer that lost the race gets `409 CommitFailedException`. Engines refresh and retry by themselves.

A multi-table transaction changes every table or none.

**Limits.** This release runs on one node. Its state lives on one volume (`/data`): back it up together with Cairn Store. Dropping a table with `purge` removes its current metadata file. Data files are left in the store.

## Backup and restore

Docker Compose deployments are backed up with one script:

```bash
deploy/ops/backup.sh                    # writes backups/cairn-<timestamp>/
deploy/ops/restore.sh backups/cairn-<timestamp>
```

**A backup holds:**

- the Cairn database (`pg_dump`),
- the catalog database (`mysqldump --single-transaction`),
- the gateway's identity keys, so remote agents keep trusting it,
- `config/`,
- a `manifest.json` with each file's checksum, the Cairn version and the schema revision.

Both dumps are consistent and taken while Cairn runs. The search index is not backed up: it is rebuilt from the catalog database.

**A restore** first checks the checksums and asks you to confirm. It then:

1. stops Cairn, but not its databases,
2. replaces both databases and the gateway keys,
3. rebuilds the search index,
4. starts Cairn again.

Anything written after the backup was taken is lost. `config/` is unpacked only with `RESTORE_CONFIG=1`. Backups contain secrets and personal data, so keep them encrypted.

On Kubernetes, use your database service's backups (point-in-time recovery) and back up the gateway key Secret. After restoring the catalog database, rebuild the index by running the setup image with `-u RestoreIndices -a clean`.

## Audit log

Denied access decisions, policy, persona and purpose changes, grants, access requests,
certification approvals, user provisioning and remote-agent activity are written to a hash-chained
audit log: altering or deleting an entry breaks the chain.

* **Check it**: `cairn audit verify`, or `GET /cairn/api/governance/audit/verify`.
* **Export it** to a SIEM or an archive, in chain order:
  `GET /cairn/api/governance/audit/export?after_seq=<last seq>&format=jsonl` (or `csv`; filters
  `since`, `until`, `category`), or `cairn audit export --out audit.jsonl`. `--follow` keeps
  writing new entries, for a log shipper.
* **Verify an export** without database access: `cairn audit verify-file audit.jsonl`. Pass the
  previous export's `head` with `--after-head` to prove nothing is missing in between.

## Upgrades

Upgrades run forward only: there are no down-migrations, so the way back is the backup you take first. Releases are rehearsed by installing the previous release, filling it with data, and upgrading it with exactly these steps. The current release was rehearsed from schema revision 0009 to 0014; every table kept its data and the audit chain stayed valid.

**What happens during an upgrade:**

- **Cairn Core:** the `cairn-setup` job migrates the catalog's schema, search indices and topics before Core starts. On Helm it is a `pre-upgrade` hook.
- **Cairn services:** each migrates the Cairn database when it starts, one at a time, under a database lock. The others wait.
- **Workers:** a replica that is still on the old version keeps working until it is replaced. The leader locks keep two versions from doing the same work.

**Before you start:**

1. Read the release notes for setting changes and new required values.
2. Check the current schema: `docker compose exec cairn-api cairn db current`.
3. Take a backup: `deploy/ops/backup.sh` on Compose, or a database snapshot on Kubernetes.

**Docker Compose:**

```bash
deploy/ops/backup.sh                                   # 1. back up
git pull                                               # 2. the new release's compose file and config
export CAIRN_VERSION=<new version>                     #    (or set it in .env)
docker compose -f docker-compose.cairn.yml pull        # 3. fetch the new images
docker compose -f docker-compose.cairn.yml up -d --wait   # 4. migrate and restart
```

On the reference stack, step 4 took about two minutes. During that time the UI and API restart, so expect a short outage on a single node.

**Kubernetes (Helm):**

```bash
helm upgrade cairn ./helm/cairn -f my-values.yaml [-f helm/cairn/values-ha.yaml] --wait --timeout 20m
```

The setup hook runs first. The Deployments then roll one pod at a time, and the PodDisruptionBudgets keep each service up during the roll.

**After the upgrade, check:**

```bash
docker compose exec cairn-api cairn db check        # exit 0: the schema is at the release's revision
docker compose exec cairn-api cairn audit verify    # the audit chain is intact
docker compose ps                                   # every service healthy
```

Then sign in, open a dataset, and run a search.

**If something is wrong:**

1. Go back to the previous version's images.
2. Restore the backup: `deploy/ops/restore.sh backups/cairn-<timestamp>`.

A newer schema is not readable by older services, so restoring is required: rolling back the images alone is not enough.

```bash
cairn db current     # schema revision of the Cairn database, and the latest one
cairn db check       # non-zero exit if the schema is not at the latest revision or differs from it
```
