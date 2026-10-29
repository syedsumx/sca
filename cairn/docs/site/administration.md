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
  - `JOBS` bytes billed, at `usd_per_tib`, split across the referenced tables.

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
