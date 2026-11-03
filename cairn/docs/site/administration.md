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

## Workload identity

Pipelines (dbt, Airflow, Spark) can sign in with the identity their platform already gives them, instead of a stored personal access token. That identity is a Kubernetes service account's projected token, or a CI job's OIDC token. Cairn exchanges it for a short-lived Cairn token of the pipeline user an administrator bound it to, so there is no token to store, copy or rotate.

- **Trusted issuers** (`CAIRN_WORKLOAD_ISSUERS`, a JSON list; Helm `cairn.workloadIdentity.issuers`). Each issuer has:
  - `name`;
  - `issuer`: the tokens' `iss`, exactly;
  - `audience`: one of its own, such as `cairn`. The cluster's default audience is refused, so ordinary service-account tokens never qualify;
  - its keys: `jwks_url` (HTTPS, with `ca_file` if needed) or `jwks_file`. Managed Kubernetes services publish their OIDC issuer and keys, and so does GitHub Actions. On your own cluster, bind `system:service-account-issuer-discovery` so the keys can be read.

  Two limits apply. `max_token_lifetime` (24 hours) refuses long-lived tokens, such as Kubernetes' legacy secret tokens, which never expire. `token_ttl` (an hour) is the Cairn token's lifetime.
- **Bindings** (`/v1/workload-identity/bindings`, `MANAGE_SYSTEM_OPERATIONS`). A binding maps an issuer and a token subject (`system:serviceaccount:pipelines:svc-dbt`, a CI `sub`) to one Cairn user. Optional claims must match too, for example `{"kubernetes.io.namespace": "pipelines"}` or `{"ref": "refs/heads/main"}`. Creating and deleting a binding is audited in the same transaction (`workload.binding_created`, `workload.binding_deleted`).
- **The exchange** (`POST /v1/auth/workload-token`, OAuth 2.0 token exchange). Cairn checks the token's signature, issuer, audience, expiry and lifetime. Exactly one user must be bound, and that user must not be suspended. The Cairn token then works wherever a personal access token does, as that user: Trino tokens, lakehouse sessions, dbt run records. Each exchange is audited (`workload.token_issued`). Anything else is refused. If the issuer's keys cannot be fetched, the exchange answers 503.
- **In the pipelines.** Set `CAIRN_WORKLOAD_TOKEN_FILE` to the token's path (`cairn dbt`, `cairn-spark`), or put `workload_token_file` in the Airflow `cairn` connection's extra. The file is read again at each exchange, since the kubelet rotates it. `cairn dbt` exchanges again before recording a run, so a long build never records with an expired token. Helm's `cairn.dbt.jobs[].workloadIdentity: {serviceAccount: svc-dbt}` runs the job as that service account with a projected token for audience `cairn`. `deploy/airflow/values-cairn.yaml` mounts one in every Airflow pod.
- **Tested** (`tests/test_workload_identity_e2e.py`, against a real Kubernetes API server):
  - **The exchange.** A token the cluster issued for `pipelines/svc-dbt` (audience `cairn`) was exchanged for `svc-dbt`'s Cairn token. With it, `svc-dbt` got a Trino token and a lakehouse session, and `cairn dbt`'s and `cairn-spark`'s exchanges worked.
  - **Refusals.** These were refused: the cluster's default audience, an unbound service account, the same name in another namespace, a forged token, a 48-hour token, a suspended user, and a deleted binding.
  - **Admin only.** Non-administrators could not bind.

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

## Other databases through Trino (federation)

Trino queries other databases in place (PostgreSQL, MySQL, SQL Server, Oracle, Hive, and its other connectors), and Cairn governs them like the lakehouse.

- **How it is governed.** Trino asks Cairn about every table in every catalog. Cairn names a federated catalog's tables as datasets of their platform: `CAIRN_TRINO_CATALOGS`, e.g. `{"crm": {"platform": "postgres", "database": "crm"}}` names `crm.sales.customers` as the `postgres` dataset `crm.sales.customers`. Policies, masks, row filters and refusals then apply in place, and across joins with lakehouse tables.
- **Helm:** `cairn.trino.federation`. Each entry has:
  - `name`: the Trino catalog;
  - `connector`;
  - `properties`, such as `connection-url`;
  - `credentialsSecret`: a Secret with `user` and `password` keys, passed to Trino as environment variables. The chart refuses passwords, secrets and tokens in `properties`;
  - `platform` and `database`: how Cairn names its tables.

  The chart writes the catalog files on the coordinator and workers, and sets `CAIRN_TRINO_CATALOGS` on the API.
- **Catalog the source too.** Add the database as a source in Cairn, so its tables have owners, labels (for masks) and lineage under the same names.
- **Tested** (`tests/test_trino_e2e.py`, Trino 476 with PostgreSQL 16 as catalog `crm`, its password from the environment):
  - the admin saw the rows as they are;
  - `ana` got the labelled email column masked;
  - `bob` got his row filter;
  - an HR table was refused;
  - a join of a lakehouse table with the PostgreSQL table was governed on both sides.

## Query history, profiles and tuning hints

Every finished Trino query, on the shared coordinator and on team warehouses, is kept for `CAIRN_QUERY_HISTORY_DAYS` (30). These are the same events Trino's HTTP event listener sends for usage statistics. Each query records:

- who ran it, its SQL, and its state and error;
- when it ran, and its wall, CPU and queued time;
- the bytes and rows it read and the rows it returned;
- its peak memory and the tables it read;
- its plan, when Trino includes one.

- **`GET /v1/queries`.** People see their own queries; operators (`MANAGE_SYSTEM_OPERATIONS`) see everyone's. Filter by `user`, `state`, `since`, `table` (a dataset URN) or `text`, or sort with `slowest`. The SQL can hold literals people typed, so no one else sees it.
- **`GET /v1/queries/<id>`.** The profile, with tuning hints drawn from its statistics and SQL:
  - it waited long in the queue (the warehouse was busy);
  - it read gigabytes to return a few rows (filter on partition columns, or keep a derived table);
  - it used `SELECT *` over wide reads;
  - it has a cross join, or a condition-less `FROM a, b` (`UNNEST` and `LATERAL` excepted);
  - it sorted a large result without `LIMIT`;
  - its memory was near the per-query limit (`CAIRN_TRINO_QUERY_MAX_MEMORY`);
  - it failed, with the failure explained: memory, access refused by Cairn's policies, or capacity.
- **In the UI.** **Query insight** (`#/queries` in the workspace) lists the queries with their time, bytes read and rows, filtered by SQL text, user (operators), state, or the slowest first. A query's profile shows its hints, statistics, tables and SQL.
- **Tested** with a real Trino (`tests/test_trino_e2e.py`). A person saw only their own queries, finished and refused, with times, rows and tables. An operator saw everyone's. A cross join and a refused read got their hints, and another person could not open the profile.
- **The pages, tested in a browser** (`tests/test_sql_ops_ui_e2e.py`: the built UI in Chromium against a real Cairn API, a real database and a real git repository):
  - a workflow made in the editor ran with a failing check; the browser showed the check's value and the skipped report, and the owner was notified;
  - a git folder was linked and pulled, then a change made in Cairn was pushed to a new branch, never to `main`;
  - query insight showed only the person's own queries, slowest first, with the queue, `SELECT *` and unbounded-sort hints.

## Warehouses: a Trino per team, stopped when idle

A warehouse is a Trino of a team's own: its own size, its own queue, its own bill. The same catalogs and Cairn's policies apply. The warehouse proxy (`cairn warehouses`) sits in front, with one port per warehouse.

- **Scale to zero.** After `idleMinutes` without client traffic, the warehouse is stopped. A running query counts as traffic, because its client keeps polling. The next query starts the warehouse, waits until Trino takes queries (`startTimeoutSeconds`, then a short warm-up), and goes through. Queries arriving together share one start. `idleMinutes: 0` keeps a warehouse up.
- **Several nodes, scaled with the load.** Give a warehouse `workers: {min, max, queriesPerWorker, scaleDownMinutes}` and the chart deploys a workers Deployment for it, with the coordinator running no queries itself. The proxy counts the queries running and queued through it, from the statement URLs their clients poll. It keeps `ceil(queries / queriesPerWorker)` workers, between `min` and `max`:
  - more at once when the load rises;
  - fewer after `scaleDownMinutes` of less load;
  - none while the warehouse is stopped.

  A query that arrives with no worker up waits in Trino for one (up to `startTimeoutSeconds`). With Docker, workers are a pool of containers (`"workers": {"docker": {"containers": […]}}`).
- **Start now.** Pipelines → Warehouses → **Start now** (`POST /v1/warehouses/<name>/start`, with `USE_WAREHOUSE`) starts a stopped warehouse before the first query, so that query does not wait. Starting counts as use, so the warehouse runs for at least its idle time. The proxy takes the start only with the control key the chart gives it and the API (the Trino access key). The start is audited.
- **What it costs.** Every `CAIRN_WAREHOUSES_REPORT_SECONDS` (300), the proxy reports node-seconds to Cairn: the coordinator and each ready worker, for the time they ran. Each report is one row of the cost report (Reports → Costs → jobs: job `urn:li:warehouse:<name>`, engine `warehouse`), priced at `cairn.trino.warehouses.usdPerNodeHour` (`CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR`). A report that fails is sent again with the same id, and Cairn records each id once, so nothing is counted twice.
- **Who may use one.** Each warehouse asks Cairn as itself. Running a query there needs `USE_WAREHOUSE` on `urn:li:warehouse:<name>`, granted to the team like any policy; operators may use any. What a query may read is decided as everywhere else, masks and row filters included.
- **Helm.** List the warehouses in `cairn.trino.warehouses.list` (`name`, `port`, optional `idleMinutes`, `queryMaxMemory`, `resources`). The chart deploys:
  - each warehouse's Trino, whose replica count Helm leaves to the proxy;
  - the proxy, with TLS from `cairn.trino.tls.secretName`, and a Service with a port per warehouse;
  - a Role that lets the proxy read and scale only those Deployments;
  - NetworkPolicies, so a warehouse is reached only through the proxy.

  Clients connect to the proxy's Service on the warehouse's port, with their usual Cairn token.
- **Compose.** Use the Docker scaler (`"docker": {"container": "…"}` in `CAIRN_WAREHOUSES`). The proxy then needs the Docker socket; prefer Kubernetes in production.
- **What it reports.** `GET /cairn/warehouse` on a warehouse's port shows its state: whether it is ready and running, its queries in flight, how long it has been idle, and how often it was started and stopped.
- **In the workspace.** Pipelines → Warehouses shows each warehouse's state, its queries running and queued, its workers, time idle and when it will stop (`GET /v1/warehouses`). People see the warehouses they may use; operators see all. A warehouse the API cannot reach shows as unreachable. The chart points the API at the proxy (`CAIRN_WAREHOUSES_URL`, `CAIRN_WAREHOUSE_PORTS`).
- **Tested.**
  - **Docker** (`tests/test_warehouses_e2e.py`, with real Trino). Three queries to a stopped warehouse started it once, and all were answered. Trino's follow-up URLs pointed back at the proxy. A long query kept it awake. When idle it stopped, and the next query started it again.
  - **Kubernetes** (`tests/test_warehouses_k8s.py`, with a real API server and the chart's RBAC). The proxy scaled its warehouse to 1 and back to 0, and was refused (403) on any other Deployment.
  - **Several nodes** (`tests/test_warehouses_scaling_e2e.py`, with a real Trino coordinator and two worker containers):
    - the first query started the coordinator and then one worker;
    - two queries at once brought a second worker;
    - after the load dropped, the workers went before the coordinator's idle time, then the coordinator stopped;
    - **Start now** worked only with the control key;
    - node-seconds reached the cost report once per report.

    With Kubernetes (`tests/test_warehouses_k8s.py`), the proxy's account scaled the workers Deployment 0 → 3 → 1 → 0 with the load.
  - **Admission** (`tests/test_trino_access.py`). Only the team's members and operators ran queries on a warehouse.

## Notebooks: JupyterHub with Cairn sign-in

JupyterHub runs from the official JupyterHub chart with `helm/jupyterhub/values-cairn.yaml`. Its hub and notebook images include `cairn-lakehouse`.

- **Sign-in.** People sign in to the hub through Cairn: JupyterHub is a client of Cairn's OAuth2 server, like Trino and Airflow. Its tokens' audience is `jupyterhub`, so they are accepted nowhere else. On Cairn's side, set `cairn.notebooks.jupyterhub`:
  - `enabled: true`;
  - `redirectUris`: `https://<hub>/hub/oauth_callback`;
  - the client secret, in a Secret shared with the hub.

  In `jupyterhub_config.py`, `cairn_lakehouse.jupyter.configure_hub(c, cairn_url=…, hub_url=…)` sets the rest: `GenericOAuthenticator`, Cairn's endpoints, and the spawn hook. `JUPYTERHUB_CRYPT_KEY` must be set.
- **Kernels act as the person.** When a person's server starts, the hub exchanges their sign-in token for a Cairn token of their own (`POST /v1/notebooks/token`, valid for `tokenTtlSeconds`, 8 hours) and gives it to the server as `CAIRN_TOKEN`:
  - `%%sql` runs on Trino as them, with their masks and row filters, and so do `%cairn spark` and MLflow;
  - the token works wherever a personal access token does;
  - it stops working as soon as the person is suspended, and no new one is given;
  - each exchange is audited.
- **Notebooks are catalog assets.** Every save sends the notebook to Cairn (`cairn_lakehouse.jupyter.post_save_hook`, set by `configure_hub`). It becomes a notebook asset owned by the person, with its cells, and a job whose inputs are the tables its `%%sql` cells read. Two-part names use `defaultCatalog`, and CTEs are not counted as tables. A failed publish never stops the save.
- **Git folders in Jupyter.** The server extension `cairn_lakehouse.jupyter` (enabled by `configure_hub`) writes the notebooks of each of the person's [git folders](#git-folders) under `git/<folder>/` when their server starts, and again on `POST <server>/cairn/git/sync`.
  - **Saves go back to git.** Saving a notebook under `git/<folder>/` (the save hook) also keeps it with that folder, and the folder's next push commits it.
  - **Outputs stay out of git.** Pushed notebooks have their outputs and execution counts cleared, so query results never reach the repository and diffs show only what changed.
  - **Local work is never overwritten.** The sync writes a file only when it is missing, or still as the last sync left it. A file edited in a way Cairn has not seen (its save never reached Cairn) is kept and reported.
- **Tested** (`tests/test_jupyterhub_e2e.py`, with a real JupyterHub, configurable-http-proxy and Jupyter server):
  - a person signed in through Cairn's OAuth flow;
  - their server started with their own Cairn token;
  - saving a notebook in it published the notebook to the catalog, owned by them, with lineage from the two tables its query read;
  - tokens for other audiences were refused, the notebook token worked as the person, and it stopped when they were suspended;
  - a git folder's notebooks appeared in the person's server. An edited one (run, with an output) and a new one were pushed to a branch of their own, outputs cleared, while `main` stayed as it was;
  - a notebook changed in git and in Jupyter was a conflict; taking git's version brought the file in Jupyter in line;
  - a file edited outside Jupyter was kept by the sync.

## Machine learning: MLflow behind Cairn

MLflow keeps experiments, runs and the model registry. People and pipelines reach it only through Cairn, at `<cairn>/v1/ml`, with their usual Cairn token:

```bash
export MLFLOW_TRACKING_URI=https://cairn.example.com/cairn/api/v1/ml
export MLFLOW_TRACKING_TOKEN=<your Cairn token>   # a person's, or a pipeline user's
```

- **Runs are the caller's.** Cairn sets `mlflow.user` on a new run to the person or pipeline user signed in, whatever the client says, and refuses changes to it. Experiments, runs and artifacts are open to signed-in people, like a shared workspace.
- **The registry is governed.** A registered model belongs to whoever created it, and a name someone else owns is refused. The model's catalog asset is `urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,<name>,PROD)`:
  - reading a model needs `VIEW_ENTITY_PAGE` on it, or being its owner;
  - changing it (versions, aliases, stages, tags, rename, delete) needs `MANAGE_ML_MODEL` on it, or being its owner;
  - searches list only the models the caller may see.

  Creating a model and every change are audited (category `ml`).
- **Models are catalog assets.** Each new version appears in the catalog as an ML model in its model group, owned by its creator. The run that trained it is recorded as a job. Its inputs are the run's datasets (`mlflow.log_input`): a dataset whose source or name is a lakehouse table (`lakehouse.<ns>.<table>` or `iceberg://<ns>.<table>`) becomes lineage from that table, so the model shows up downstream of the data it was trained on.
- **Serving.** Run `mlflow models serve` for a model (for example `models:/churn@champion`). List it in `cairn.mlflow.serving` (`CAIRN_ML_SERVING`: `{"churn": "http://churn-serving:5000"}`), and callers send predictions to `POST <cairn>/v1/ml/serve/churn/invocations`. Calling a model needs `USE_ML_MODEL` on it, or being its owner. Each call is audited.
- **Feature tables.** A table's owners (`MANAGE_DATASET`) declare its columns as features: `POST /v1/ml/feature-tables` with `name`, `table` (`namespace.table`), `keys` (the entity's key columns) and optionally `features` (default: every other column). Cairn publishes the feature table, one feature per column and the primary keys, each with the table as its source, so the catalog shows table, features and the models trained on that table together. Reading feature values is reading the table: as yourself, under its policies, masks and row filters. `GET /v1/ml/feature-tables` lists those whose table the caller may see (`tests/test_feature_tables.py`).
- **Online feature store.** A feature table's owners (`MANAGE_DATASET` on its table) serve it by entity key for models at prediction time: `PUT /v1/ml/feature-tables/<name>/online` with optionally `timestamp_column` (the latest row per entity wins; without it the table holds one row per entity, and a repeated entity is logged) and `ttl_seconds` (older values are served as `expired`). Cairn fills it at once (and keeps nothing if that fails), then each scheduler tick looks at the table's current snapshot and, when it moved, writes only the entities whose values changed and removes those gone. `POST .../online/sync` syncs now; `GET .../online` shows the entities, the snapshot served and the last error; `DELETE .../online` stops serving and drops the values.
  - **Lookups.** `POST .../online/lookup` with `entities` (each a map of the key columns; values are read as the key's type, so `"7"` finds 7) and optionally `features`, at most 1,000 entities a call. Each result has `status` (`found`, `missing`, `expired`), the values and `event_ms`.
  - **Governed as a read of the table.** Values are kept raw and each lookup is decided for the caller on the source table: without read access (`VIEW_DATASET_PROFILE`) nothing is returned; a column mask masks the feature as Trino masks it (the same strategy per type); a row filter refuses the lookup (it is SQL an entity lookup cannot apply: read the table through Trino). The service principal cannot look features up. Lookups are not audited per call; turning serving on and off is.
  - **Where values live.** Cairn's database (tables `cairn_online_feature_tables` and `cairn_online_feature_values`, migration 0065), or a database of their own with `CAIRN_ONLINE_STORE_URL` (created there on start). Cairn reads the lakehouse with its own catalog client: `CAIRN_ONLINE_FEATURES_CATALOG` (pyiceberg REST settings, JSON or a file), else `CAIRN_PIPES_CATALOG`. Tested with the real store and catalog in `tests/test_online_features_e2e.py`.
- **Model monitoring.** Every call to a model through Cairn (`POST /v1/ml/serve/<model>/invocations`) is logged (time, caller, status, latency, rows) and answered with `X-Cairn-Inference-Id`. A model's managers (its owner, `MANAGE_ML_MODEL`) turn monitoring on with `PUT /v1/ml/models/<model>/monitor`:
  - `baseline`: `{"table": "<namespace>.<table>"}` (the training table, read once by Cairn's own catalog client: `CAIRN_ML_MONITOR_CATALOG`, else `CAIRN_PIPES_CATALOG`; a missing table or column is refused when set up) or `{"since_ms", "until_ms"}` (a reference period of the model's own logged calls; checks wait until it ends). `features` limits the comparison (default: every input column).
  - Each check (every `every_minutes`, default 60, on the scheduler; or `POST .../monitor/run`) covers the last `window_minutes` (default 1440): per feature the population stability index (ten quantile bins of the baseline for numbers, its 20 most common values otherwise) and the mean against the baseline's; the predictions' PSI against a reference period; error rate; latency p50 and p95; and accuracy (classes) or mean absolute error (numbers) over the calls with labels. Above `drift_threshold` (default 0.2) a feature drifts; under `min_rows` (default 100) the check reports `insufficient`. Turning to drift notifies the owner and `notify` (`ml.drift_detected`), turning back `ml.drift_resolved`.
  - `POST /v1/ml/models/<model>/feedback` with `inference_id` and `labels` (one per row), by the model's managers or whoever made the call. `GET .../monitor` and `.../monitor/checks` are for whoever may see the model.
  - **Payloads** (inputs and predictions) are kept only for monitored models while `log_payloads` is on (default), readable only by the model's managers (`GET .../monitor/inferences`), deleted after `retention_days` (default 30) and when monitoring is turned off; the call log and checks stay. Tables `cairn_ml_monitors`, `cairn_ml_inferences`, `cairn_ml_monitor_runs` (migration 0066). Tested with MLflow's real model server in `tests/test_ml_e2e.py`, and a training table in the real catalog in `tests/test_model_monitoring_e2e.py`.
- **Helm.** `cairn.mlflow.enabled: true` deploys MLflow (`ghcr.io/mlflow/mlflow`). It needs:
  - a PostgreSQL URL in a Secret (`cairn.mlflow.database.secretName`);
  - artifacts in Cairn Store (`cairn.mlflow.artifacts`), with a store key for that prefix only.

  A NetworkPolicy lets only Cairn's API reach it. The chart points the API at it (`CAIRN_MLFLOW_URL`).
- **Tested** (`tests/test_ml_e2e.py`, with a real MLflow 3.5 server, the MLflow client and `mlflow models serve`):
  - a person trained a scikit-learn model on a lakehouse table and registered it through Cairn;
  - the run was theirs although the client claimed another user;
  - the model was theirs, and appeared in the catalog with lineage from the training table;
  - another person could neither see, change nor take the model until a policy let them see it, and seeing still did not let them change it;
  - predictions went through Cairn only for the owner, or with `USE_ML_MODEL`.

## Delta Lake tables

Trino's `delta` catalog keeps Delta Lake tables in Cairn Store, with its metastore files beside them (`s3://<bucket>/<prefix>/`, default `s3://lakehouse/delta/`).

- **Governed like the lakehouse.** A Delta table `delta.<schema>.<table>` is the `delta-lake` dataset `<schema>.<table>`. Cairn's access, masks, row filters and write rights apply to it, as to any Trino table.
- **Delta's own features.** History (`"orders$history"`), time travel (`FOR VERSION AS OF`), `CREATE TABLE AS`, inserts, updates, deletes and `MERGE`.
- **Tables other engines wrote.** Spark and Databricks Delta tables, for example. Register one by its location: `CALL delta.system.register_table('schema', 'table', 's3://…/table')`. `unregister_table` forgets one and leaves its files.
- **Helm.** Set `cairn.trino.delta.enabled`, and `credentialsSecret` (`access-key`, `secret-key`) with a store key that can reach only the Delta prefix. The chart sets the catalog's mapping on the API.
- **Several writers.** Each commit writes the next log entry only if it does not exist yet (`If-None-Match: *`, `s3.exclusive-create`), which Cairn Store checks atomically. Several coordinators, clusters or other engines that commit the same way can write one table at once: a writer that loses the race retries at the next version, and nothing is lost or overwritten. Engines that cannot write conditionally (older Spark Delta releases on S3) must not write the same tables.
- **Converting to Iceberg, in place.** `POST /v1/delta/convert` (`MANAGE_SYSTEM_OPERATIONS`) with `location` (the Delta table's `s3://…` location) and `target` (`namespace.table`) makes an Iceberg table in Cairn Catalog **at the same location**, with the Delta table's live Parquet files at its latest version. No data is copied, and the catalog's per-table credentials cover the files.
  - `GET /v1/delta/inspect?location=…` first says what would be converted, or why it cannot be.
  - Cairn reads the Delta log from the store: the latest checkpoint, then the commits after it. The table records `cairn.converted-from` and `cairn.delta-version`.
  - **One way.** The files now belong to the Iceberg table, and its maintenance may delete them as it changes. Retire the Delta table first: `CALL delta.system.unregister_table('schema', 'table')`. If the Delta log moves during conversion, the new table is dropped and the request refused.
  - **Refused:** partitioned tables (Delta keeps partition values in paths, not in files), deletion vectors and column mapping. Copy those with a derived table (`SELECT * FROM delta.<schema>.<table>`).
  - **Settings.** `CAIRN_DELTA_STORE` (a key that reads the Delta tables) and `CAIRN_DELTA_CATALOG`, defaulting to the pipes' store and catalog.
- **Tested** with Trino and Cairn Store (`tests/test_trino_e2e.py`). A Delta table was created from the lakehouse and written. Masks, row filters and a refused write applied. History and time travel worked. After the table was unregistered, it was registered again by its location and still governed.
  - **Two clusters at once.** Two Trino clusters inserted into one Delta table concurrently, twelve inserts in all. Every row arrived, and the log had one version per insert after the table's creation.
  - **Conversion.** A Delta table with a checkpoint, an insert, an update and a delete was unregistered and converted. Trino read the same rows from the Iceberg table, which then took an insert. Converting again to the same name, a partitioned Delta table and a location with no Delta log were refused.

## Functions in SQL: AI and Python

Trino runs two kinds of functions beyond SQL's own, both under Cairn's policies.

- **AI functions.** Trino's `ai` connector, as the catalog `llm`: `llm.ai.ai_gen`, `ai_classify`, `ai_analyze_sentiment`, `ai_extract`, `ai_mask`, `ai_translate` and `ai_fix_grammar`. For example: `SELECT llm.ai.ai_classify(comment, ARRAY['bug', 'praise']) FROM support.tickets`.
  - **Who may call them.** They send rows to a model outside the lakehouse, so calling one needs Cairn's `USE_AI_FUNCTIONS`, granted like any privilege. Others are refused before anything is sent. The catalog crawler never calls them. `CAIRN_TRINO_AI_CATALOGS` names the AI catalogs (default `llm`).
  - **What the model sees.** What the caller sees: masks and row filters apply before any function runs, so a masked column reaches the model masked.
  - **Through Cairn** (default, `cairn.trino.ai.viaCairn`). Trino calls the model through Cairn (`/v1/trino/<key>/ai/...`), and only Cairn has the provider's key (`CAIRN_AI_SQL_API_KEY`). Cairn:
    - allows only `model` and `allowedModels`;
    - counts every call against the deployment's daily AI budget (`CAIRN_AI_DAILY_BUDGET_USD`, shared with Cairn's own AI features);
    - caps calls a minute (`callsPerMinute`, per API replica);
    - records tokens and cost in AI usage as `trino-sql`.

    Trino's connector does not say who a call is for, so tokens are counted for the deployment as a whole. Per person, Cairn admits at most `userDailyQueries` AI-function queries a day, and the person's own token allowance (`CAIRN_AI_USER_DAILY_TOKENS`) applies. Each query is recorded under the person, so the usage report shows who used AI functions and how often. When a limit is reached, the query fails and nothing reaches the model. Trino reports "request to AI provider failed"; Cairn's log says why. The connector also keeps answers to prompts it has already seen, so a repeated prompt is not sent again.
  - **Helm.** Set `cairn.trino.ai`: `enabled`, `provider` (`anthropic`, or `openai` for any OpenAI-compatible endpoint such as Ollama or vLLM), `model`, an optional `endpoint`, and `apiKeySecret`.
- **Python functions.** Inline, per query, sandboxed in WebAssembly by Trino:

  ```sql
  WITH FUNCTION domain(e varchar) RETURNS varchar LANGUAGE PYTHON WITH (handler = 'domain')
  AS $$
  def domain(e):
      return e.split('@')[-1]
  $$
  SELECT domain(email) FROM sales.orders
  ```

  They run on the caller's rows, after masks and filters, and cannot reach files or the network.
- **Stored functions.** SQL and Python functions created once and called anywhere: `CREATE FUNCTION routines.<schema>.<name>(...) RETURNS ... RETURN ...` (or `LANGUAGE PYTHON`). They live in the `routines` catalog (Trino's Hive connector, with a file metastore in the store; Helm: `cairn.trino.routines.enabled` and `credentialsSecret`).
  - **Who may call one.** Calling one needs `USE_FUNCTION` on `urn:li:function:routines.<schema>.<name>` (resource type `function`), granted per function like any privilege. Creating or dropping one needs `MANAGE_DATASET`.
  - **What it sees.** Masks and row filters apply before any function runs. A SQL function cannot query tables, so it never reaches more than its caller passes it.
  - **Settings.** `CAIRN_TRINO_ROUTINE_CATALOGS` names the catalogs that hold stored functions (default `routines`).
  - **In the catalog.** Each stored function is an asset people can find, document and own. It is a job with subtype `Function` in the flow `(trino,routines,PROD)`, with:
    - its signatures (every overload), return types, language, body and comment;
    - `function_urn`, the `urn:li:function:…` that `USE_FUNCTION` policies name.

    Trino's query events keep it current. A finished `CREATE [OR REPLACE] FUNCTION` publishes it, owned by its creator; replacing it keeps the owner. `DROP FUNCTION` drops that signature, and the asset is removed with its last one.
  - **Sync.** `POST /v1/trino/functions/sync` (`{"connection": …}`, `MANAGE_INGESTION`) lists the routines catalogs' functions as Cairn's crawler (`SHOW FUNCTIONS`). It adds those Cairn was not told of and removes those that are gone. A function Cairn already knows keeps the body it learned.
  - **Tested** with Trino (`tests/test_trino_e2e.py`): a created function appeared with its comment, body and creator; an overload joined it, and dropping it left the other; a function made while Cairn was not listening came in with the sync (refused to someone without `MANAGE_INGESTION`); dropped functions were removed. The statements as Trino receives them, in `tests/test_trino_functions.py`.
- **Procedures** (`/v1/procedures`). A named list of SQL statements with typed parameters (`integer`, `double`, `varchar`, `boolean`, `date`; `:name` in a statement), on a Trino connection.
  - **Calling one.** `POST /v1/procedures/<name>/call` with `{"args": {...}}`. Each argument is checked against its type and bound as a SQL literal, never spliced in as text. The statements run in order **as the caller**, so Cairn's policies apply to every statement. A failing statement stops the call; what ran before it stays. The last statement's rows come back, up to 1,000.
  - **Who may call one.** Its owner, and those allowed `EXECUTE_PROCEDURE` on `urn:li:procedure:<name>`. A procedure never runs with its owner's rights: it does only what the caller could do anyway. Every call is audited with its arguments.
  - **Checked when saved.** Each entry is one statement, and only declared parameters are used.
- **Tested** with Trino (`tests/test_trino_e2e.py`, against a stand-in for the model's API; the decisions in `tests/test_trino_access.py`):
  - **AI functions.** A user without `USE_AI_FUNCTIONS` was refused and nothing was sent. With it, the model was sent her masked email, never the raw one.
  - **Through Cairn.** The provider received Cairn's key, never Trino's. The call was in Cairn's AI usage, with her query under her name. Her daily cap, a model not allowed, and the spent budget each stopped the query, and nothing reached the model. The rate limit, Anthropic usage and an unreachable provider are covered in `tests/test_sql_ai_gateway.py`.
  - **Python functions.** One ran on masked values for her, and on real values for the admin.
  - **Stored functions.** A stored SQL function and a stored Python function ran for the admin. Ana was refused both until she was given `USE_FUNCTION` on one. That one then ran on her masked emails, and she was still refused the other and creating her own.
  - **Procedures.** A three-statement procedure replaced a row and returned totals. A hostile string argument was stored as a value, not run as SQL. A bad date and an undeclared parameter were refused. Ana could not call it without `EXECUTE_PROCEDURE`; with it, her first statement was refused because she may not write the table, and nothing was written.

## SQL workspace

**SQL** (`/sql`) is where people write and run read-only SQL against any connection that serves catalog datasets, as themselves.

- **The same checks as a dataset's page.** Queries go through Cairn's data access service. Every table is checked for the person, masked columns are masked, and on a Trino connection with `run_as_user`, Trino's row filters apply too. A query that writes, or reads a table the person may not query, is refused.
- **Schema browser.** Lists the connection's datasets the person may see, with their columns. Double-click a table to insert its name.
- **Results.** A table, a bar or line chart (x column, one or more numeric y columns) and a CSV download. Ctrl+Enter runs the query.
- **History.** Each person's last 500 runs, including refused ones and why (`GET /sql/history`).
- **Saved queries.** Private, or shared with everyone. A shared query runs as whoever opens it, never as its owner, so each person sees only what they may (`/sql/saved`, `POST /sql/saved/{id}/run`). Only its owner changes or deletes it. A saved query keeps its chart.
- **Who.** People only: service, agent and extension tokens are refused.
- **Dashboards** (**Dashboards**, `/sql/dashboards`). A named, optionally shared set of saved queries, shown as their charts or tables. Opening one runs every tile as the viewer. A tile the viewer may not see shows the refusal, not the data. Each tile must be a query its owner may open. Only the owner changes or deletes a dashboard.
- **Alerts** (`/sql/alerts`). A saved query checked every `every_minutes` (5 minutes to a week), as its owner. The condition is `rows` (the number of rows) or a column of the first row, compared with a number (`>`, `>=`, `<`, `<=`, `==`, `!=`).
- **Workflows** (`/v1/sql-workflows`). Scheduled SQL in the product: named steps that depend on each other (`after`; cycles are refused), run on a `cron` (UTC, at most every five minutes) or with **Run now**, as the workflow's owner. Steps can be:
  - `derived`: refresh a derived table (`full` for a rebuild). Only its owner, or an operator, may schedule it; this is how SQL that writes runs on a schedule.
  - `check`: a saved query's `rows`, or a column of its first row, compared with a value. It fails the step when the condition does not hold (a quality gate between steps).
  - `query`: a saved query, or `sql` on a `connection`, run as the owner. Its row count and first 20 rows are kept with the run and sent to the recipients (a scheduled report).

  A step whose dependency failed is skipped, and `retries` (0-3) retries a failing step. Every run is kept, with each step's status, rows, error, attempts and time. The owner and `recipients` are told when a run fails (`sql.workflow_failed`), or after every run with `notify: always` (`sql.workflow_finished`), on their notification channels. Queries go through the SQL workspace, so the owner's policies, masks and row filters apply. Several scheduler replicas claim each due run once.
  - **In the UI.** **SQL workflows** (`#/sql/workflows` in the workspace) lists a person's workflows with their schedule, next run and last outcome. Its editor builds the steps (a table refresh, a check, a report), what each comes after and its retries; **Run now** runs one, and each run shows every step's outcome.
  - **Tested** in `tests/test_sql_workspace.py` (order, masks, a failing check skipping its dependents, notifications, the cron, checked definitions) and `tests/test_trino_e2e.py` (a derived-table refresh then a check, on real Trino).
  - **Who is told.** The owner and the alert's recipients are told when the condition starts to hold (`sql.alert_triggered`), when it stops (`sql.alert_resolved`), and once when the query fails. They hear about changes, not every check, on their notification channels.
  - **Where checks run.** The scheduler's leader runs them (`cairn scheduler`), so there is one check per alert however many replicas run. **Check now** runs one at once.
- **Tested** (`tests/test_sql_workspace.py`, and against Trino in `tests/test_trino_e2e.py`):
  - masking, refusals and history per person;
  - saving, sharing, running a shared query as another person (who got their own row filter), and owner-only changes;
  - a schema browser that left out what the person may not see;
  - a shared dashboard rendered for its owner and for someone refused one of its tiles, who saw that tile's refusal and the other tile's data;
  - an alert that stayed quiet on its first fine check, was told once (to its owner and recipient) when the condition began, again when it stopped, and once when its query failed. It was not checked before it was due.

## Data apps

Data apps are Streamlit apps (or any web app) that Cairn serves to people, each viewer signed in and governed as themselves.

- **Define** one with `POST /v1/apps` (`MANAGE_SYSTEM_OPERATIONS`): `name`, `title`, and where it runs.
  - `url`: the app runs elsewhere, for example a Deployment beside Cairn, and Cairn only fronts it.
  - `command`: Cairn starts it on the first visit and stops it after `idle_minutes` (30) without traffic, for single-node installs. `{port}` and `{base}` in the command are filled in, so for Streamlit: `["streamlit", "run", "app.py", "--server.port", "{port}", "--server.baseUrlPath", "{base}", "--server.headless", "true"]`, with `workdir` for the app's folder.
  - Either way, the app serves under `/apps/<name>/`, its base path (`CAIRN_APP_BASE`).
- **Open** it at `/apps/<name>/`. Every request goes through Cairn, page loads and WebSockets alike:
  - **Signed in.** Strangers get 401.
  - **Allowed.** Opening an app needs `USE_APP` on `urn:li:dataApp:<name>`. Others get 403, and the app never sees their request; a `command` app is not even started for them. `GET /v1/apps` lists only the apps the caller may open.
  - **Told who is viewing.** Cairn removes every `X-Cairn-*` header the browser sent, then adds:
    - `X-Cairn-User`: the viewer's urn.
    - `X-Cairn-App-Key`: the app's own key. An app reached by `url` should refuse requests without it; `command` apps get it as `CAIRN_APP_KEY`.
    - `X-Cairn-Trino-Token` (where Trino is set up): a ten-minute token for Trino as the viewer. Queries the app runs with it are the viewer's own, under their policies, masks and row filters, never the app's.
  - **No secrets passed on.** A `command` app's environment drops Cairn's own secrets.
- **Keys.** Each app's key is `HMAC(CAIRN_APPS_SECRET, name)`, defaulting to the service token: nothing is stored, every replica agrees, and rotating the secret rotates every key.
- **From git.** Add `git`, either `{"repo": …}` (a repository configured for Cairn's git integration, whose token Cairn uses) or `{"url": …}` (one whose prefix the operator allows in `CAIRN_APP_GIT_URLS`; chart: `cairn.apps.gitUrls`), plus `branch` (`main`) and `path` (the app's folder, where `command` runs).
  - **Deploys.** Creating the app deploys the branch's current commit, and the app records it (`commit`). A new commit on the branch changes nothing until `POST /v1/apps/<name>/deploy`. Each deploy is audited (`app.deployed`).
  - **Fails closed.** An app whose first deploy fails is not kept.
  - **Signing in.** The repository's token reaches git through the environment, never a command line or the clone's config.
- **On Kubernetes** (`"runtime": "kubernetes"`, with `image` and `port`). Each app is a Deployment of its own in `cairn.apps.kubernetes.namespace`, at zero replicas until someone opens it. The first visit scales it to one and waits until it is ready. Every visit stamps it, and after `idle_minutes` without one it is scaled back to zero, whichever Cairn replica served it.
  - **Code.** An app from git gets its code from an init container (`gitImage`) that checks out exactly the deployed commit. Deploying again rolls the pods to the new commit.
  - **Locked down.** The pod has no Kubernetes credentials and runs as a non-root user (10001) without privileges or capabilities. It gets `PORT`, `CAIRN_APP_BASE` and `CAIRN_APP_KEY`; the key and any git token come from a Secret of its own.
  - **Only Cairn reaches it.** A NetworkPolicy lets only Cairn's API pods in (or `cairn.apps.kubernetes.ingressFrom`). Cairn reaches the app at its Service; when Cairn runs outside the cluster, set `access: node-port` and `nodeAddress`. Node ports keep the caller's address, so the policy can name Cairn's.
  - **Permissions.** The chart gives Cairn's API a Role in that namespace for the apps' Deployments, Services, Secrets and NetworkPolicies, and nothing else.
- **Tested** in `tests/test_data_apps_git.py` (a real repository; the app a real process). The app ran the deployed commit in its `path`, kept it until deployed again, and then ran the new one. A URL outside the allowed list was refused, and a failed first deploy left no app.
- **Tested on Kubernetes** in `tests/test_data_apps_k8s.py` (a kind cluster; the chart's Role; code from a git server):
  - the app's objects were created stopped, and the key appeared only in its Secret;
  - the first visit through Cairn started it, running the deployed commit as user 10001;
  - another pod in the cluster was refused by the network policy;
  - a deploy rolled it to the new commit, and idleness scaled it back to zero;
  - Cairn's account could not create apps in another namespace, and deleting the app removed its objects.
- **Tested** in `tests/test_data_apps_e2e.py`, with a real Streamlit app opened in Chromium through Cairn.
  - Strangers got 401, someone without `USE_APP` got 403 on the page and on the WebSocket, and the app was not started for them.
  - Ana's first visit started the app. Its page, which Streamlit draws over its WebSocket, showed her as the viewer, the app key verified, and a Trino token present.
  - The headers her browser forged (`X-Cairn-User: root`) never reached the app, and Cairn's secrets were not in its environment.
  - The idle app was stopped, and the next visit started it again.

## Git folders

A Git folder keeps a person's saved queries and notebooks in a git repository, reviewed like code. Cairn works on a clone of its own with the `git` command, so any host works.

- **Create** one with `POST /v1/git-folders`: `name`, and `repo` (a repository configured for Cairn's git integration, whose token Cairn uses) or `url` (one the operator allows in `CAIRN_GIT_FOLDER_URLS`, a comma-separated list of URL prefixes). Optional: `branch` (`main`), `path` (a folder in the repository), and `every_minutes` for a scheduled pull. Folders are their owner's: nobody else sees or uses them.
- **Pull** (`POST /v1/git-folders/<id>/pull`).
  - Each `.sql` file becomes one of the owner's saved queries. Header comments give its settings (`-- connection:`, `-- description:`, `-- shared:`, `-- chart:` as JSON); the rest is the SQL.
  - Each `.ipynb` notebook is published to the catalog, with the tables its `%%sql` cells read.
  - A file changed in git updates its query, and a deleted file deletes it.
  - **Conflicts.** When the query was also changed in Cairn since the last pull, the file is reported as a conflict and left alone. Pull with `{"force": true}` to take git's version.
- **Push** (`POST /v1/git-folders/<id>/push`, optional `message` and `add`, a list of other saved query ids to add).
  - The folder's queries, as they are in Cairn, are committed by their owner to a new branch, `cairn/<user>/<time>`, never to the folder's branch. For a configured GitHub, GitLab or Bitbucket repository, a pull request is opened from it.
  - A query already proposed, or one git already holds as it is, is not pushed again.
  - Until the pull request is merged, pulls neither conflict with it nor delete the queries it adds. Once it is merged, the next pull has nothing to report.
- **Signing in to the host.** For a configured repository, the token is sent as a header on each git command and never written into the clone. Only `https` and `ssh` remotes are used, plus `file` when an allowed prefix is `file://`.
- **Notebooks in Jupyter** (`GET /v1/git-folders/<id>/notebooks`). Each notebook as it was last saved in the owner's JupyterHub server, or else as git has it. JupyterHub's extension syncs them into `git/<folder>/`; see [Notebooks](#notebooks-jupyterhub-with-cairn-sign-in).
  - Saves there are kept for the next push. Pushed notebooks have their outputs cleared.
  - A pull that brings a new version of a notebook also changed in Jupyter since reports a conflict, unless `force`.
  - Folder names must give different directories: a second folder named `Reports` beside `reports` is refused.
- **In the UI.** **Git folders** (`#/sql/git` in the workspace) links a folder, pulls it (showing what came in, conflicts, and a button to take git's version) and pushes with a commit message, linking to the pull request when the host opens one.
- **Settings.** `CAIRN_GIT_FOLDERS_DIR` holds the clones (default `$CAIRN_DATA_DIR/git-folders`). Deleting a folder removes its clone and keeps its queries as ordinary saved queries.
- **Tested** in `tests/test_git_folders.py`, with a real git repository and a colleague's clone.
  - A pull made two queries from their headers and published a notebook; pulling again changed nothing.
  - A colleague's edit and deletion were pulled. A push put a changed query and a new one on a new branch committed by their owner, leaving `main` alone; pushing again made no branch.
  - Before the merge, a pull left the new query alone; after it, a pull had nothing to report.
  - A file changed on both sides was a conflict until forced.
  - Another person could not see or pull the folder, and a URL outside the allowed prefixes was refused.

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
- **Spark** has no bill to read, so Cairn prices each application from its OpenLineage events when `CAIRN_COST_SPARK_USD_PER_CORE_HOUR` is set:
  - **The price.** The cores it reserved, times its wall-clock time from start to finish, at that rate.
  - **The cores.** `spark.driver.cores` plus `spark.executor.instances` × `spark.executor.cores`. With dynamic allocation, `maxExecutors` (an upper bound); N for `local[N]`. Otherwise `CAIRN_COST_SPARK_DEFAULT_CORES` (default 4). `cairn_spark` sends these settings with the events.
  - **Who pays.** Producing data is what the job is for, so the cost is split evenly across the tables its queries wrote, on the day it finished.
  - **Charged once.** Each application is charged once in `cairn_run_costs`, however often its events are delivered.
  - **Per job.** **Reports → Cost → Spark jobs** (`GET /reports/costs/jobs?days=30`) lists what each application cost over 30 days: its runs, running time and dollars.

  It is an estimate of what was reserved, not a bill. For the exact figure from your cloud, post daily rows to `POST /reports/costs`.

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

**Optional settings for `tenant.sh`:**

| Setting | Effect |
|---|---|
| `CAIRN_TENANT_SERVICES=1` | Each tenant gets its own OpenSearch and Kafka in its namespace (`deploy/hosting/tenant-services.yaml`). Only its own release reaches them. `CAIRN_SEARCH_HOST` and `CAIRN_KAFKA_TEMPLATE` are then not needed. |
| `CAIRN_INGRESS_NAMESPACE` | The ingress controller's namespace. Turns on namespace isolation (`deploy/hosting/isolation.yaml`): nothing in another tenant's namespace reaches this one. Add `CAIRN_MONITORING_NAMESPACE` to let Prometheus scrape it. |
| `CAIRN_PG_KUBECTL=<namespace>/<cluster>` | Administer a CloudNativePG cluster through its primary's local socket (`kubectl exec`), instead of `CAIRN_PG_ADMIN_URL`. |
| `CAIRN_TLS_ISSUER` | A cert-manager ClusterIssuer. Each tenant's host gets its certificate from it. |
| `CAIRN_POD_SECURITY` | The Pod Security level enforced in tenant namespaces (`baseline` or `restricted`). |

Every tenant's databases are closed to other roles: `CONNECT` is revoked from `PUBLIC`. `tenant.sh render-services <id>` prints the isolation policies and services that `create` would apply.

### Hosting on Hetzner

`deploy/hetzner/` builds a hosting cluster on Hetzner dedicated servers. Its README is the full runbook.

**What it sets up:**

- **Servers:** three nodes on a private vSwitch.
- **Firewall:** SSH and the Kubernetes API only from your addresses; the ingress only from the load balancer.
- **Kubernetes:** k3s with an embedded etcd, a WireGuard-encrypted pod network, secrets encrypted at rest and an API audit log.
- **Load balancer:** a Hetzner one, with the PROXY protocol (Terraform).
- **Shared services:** ingress-nginx, cert-manager (Let's Encrypt), CloudNativePG (a primary and two replicas with WAL archiving and nightly backups to Hetzner Object Storage), and kube-prometheus-stack.
- **Tenants:** created with `tenant.sh` and every option above. Each gets its own OpenSearch and Kafka.

```bash
deploy/hetzner/cluster.sh ~/cairn-hetzner/cluster.env prepare     # then: buckets, k3s, platform
deploy/hosting/tenant.sh create acme --licence acme.licence --values deploy/hetzner/platform-values.yaml
```

Three AX42 servers (about €200–260 a month with the load balancer and backups) take 5–8 small tenants. CI (job `hetzner`) runs the whole setup on a real machine and checks tenant isolation: the firewall, k3s, the platform services, and a tenant created by `tenant.sh`.

**Known limits:**

- **ingress-nginx:** Kubernetes retired the project in March 2026. It stays pinned because Cairn's chart uses its annotations.
- **Tenant OpenSearch and Kafka:** each runs on one node and doesn't fail over to another.

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
- `cairn-store.json`, the Cairn Store dashboard: requests and errors by operation, latency, capacity and disks, events, replication, queues, lifecycle and tiers, the cluster, and SFTP, for one node or all,
- `cairn-store-alerts.yaml`, the store's rules: a node down, server errors, disks filling up and full, an event backlog or dropped events, replication failing, a cluster node down or copies that cannot be placed, failing tier transitions, and many failed SFTP sign-ins,
- `cairn-alerts.yaml`, Prometheus rules (checked with `promtool` in CI) covering: a service down, server errors above 5%, slow requests, slow policy decisions, a spike in denials, a stalled or failing worker, a notification backlog or failing channel, a burst of quality incidents, the AI budget reached, and the licence expiring, read-only or out of seats.

**To run them:**

- **Compose:** `docker compose -f docker-compose.cairn.yml -f docker-compose.observability.yml up -d` adds Prometheus (`localhost:9090`) and Grafana (`localhost:3300`, with `GRAFANA_ADMIN_PASSWORD` in `.env`), pre-loaded with both.
- **Helm:** `cairn.serviceMonitor.enabled` scrapes the services and the workers (a ServiceMonitor and a PodMonitor), and the store at `/-/metrics`. `cairn.prometheusRule.enabled` installs the rules, and `cairn.grafanaDashboard.enabled` installs the dashboard as a ConfigMap for the Grafana sidecar.

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

**Decisions at scale**, measured in CI on the real code (`tests/test_scale_decisions.py`), with 61 policies (team grants by schema, masks, denies):

| What | Measured | Held to |
|---|---|---|
| A cached policy decision | p50 0.011 ms, p99 0.04 ms, about 75,000 a second on one thread | p99 under 5 ms at 2,000 a second |
| Cairn Catalog's decision for a job reading 1,000 tables (one call) | 650 ms the first time, 120 ms warm | under 2 s, and 0.5 s warm |

The policy engine indexes policies by the privilege they grant, and only matching policies are evaluated, so a large policy set costs little for a privilege it does not mention. Explain mode (`/governance/v1/authorize?explain=true`) still evaluates every policy and says why each did or did not apply.

**Chaos** (`catalog/conformance/test_chaos.py`, three catalog replicas on PostgreSQL, run in CI):

- **A replica killed mid-commit.** One of three replicas is killed (SIGKILL) during 48 concurrent commits from six writers. The writers fail over to the other replicas. Every acknowledged commit is in the table exactly once, with one snapshot each. A client that was not told a commit's outcome checks the table before retrying.
- **The database restarts** under the replicas. Each one serves again without being restarted.
- **The store is killed** during writes and stays down for three seconds. Writes fail while it is down; afterwards the table reads in full, nothing acknowledged is missing, and writes resume. This holds with the store's production durability (`CAIRN_STORE_FSYNC` on). With it off, which only tests use, the same crash loses acknowledged writes, and this test caught exactly that.

**Security checks in CI** (`tests/test_route_security.py`). Every API route, called without credentials and with an untrusted actor header, is refused (401), except a short list of routes that are public by design: OAuth token endpoints, webhooks that check their sender's signature, and embeds with their own tokens. Each is listed with why. A new route that answers strangers fails the build until it is protected or added to the list on purpose. No route answers a stranger with a server error. API answers carry `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and `Referrer-Policy: no-referrer`, whatever proxy is in front. These checks are not a penetration test; an external one is still to do.

## High availability

`helm/cairn/values-ha.yaml` is a profile for production clusters:

```bash
helm install cairn ./helm/cairn -f my-values.yaml -f helm/cairn/values-ha.yaml
```

- **Stateless services** (API, policy service, core proxy) run 3 replicas, and the MCP server and workspace UI run 2. Each component's replicas are spread over nodes and zones (`cairn.ha.spreadReplicas`), and a PodDisruptionBudget keeps enough of them up during node drains.
- **Workers** (scheduler, notifier, context miner) run 2 replicas. A Postgres advisory lock makes one of them the leader for each cycle, so work is never done twice. If the leader's node goes away, the standby takes over on its next cycle.
- **The gateway** runs more than one replica only when its keys come from a shared Secret (`gateway.keys.existingSecret`). Without one, the keys live on a volume, the gateway runs a single replica and it gets no PodDisruptionBudget.
- **Databases:** point `global.*` at managed, replicated Postgres, MySQL and OpenSearch. The databases the chart can run are single instances.
- **Cairn Store** runs as one node by default. For high availability, run it as a cluster (`cairn.store.cluster.enabled`, with its metadata in replicated PostgreSQL): every object is kept on several nodes, and any node serves every request. See [Clusters](#cairn-store).

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

**Lifecycle rules.** Each bucket can have lifecycle rules (`PutBucketLifecycleConfiguration`). Rules can expire current objects a number of days after they were written, or on a date. They can also remove delete markers that are the last thing left of an object, expire noncurrent versions (optionally keeping the newest few), and abort multipart uploads left unfinished. A rule can select objects by prefix, tags and size. The store applies the rules in the background, every `CAIRN_STORE_LIFECYCLE_INTERVAL` seconds (default 3600). It only expires the version it checked, so an object rewritten in the meantime is kept. Responses for objects that will expire carry `x-amz-expiration`. Rules can also move objects, or noncurrent versions, to a storage tier (below). Metrics: `cairn_store_lifecycle_*_total`.

**Times to live.** Besides rules, a single object can be given a time to live.
- **On a write.** Send `x-cairn-ttl` on `PutObject`, `CopyObject` or `CreateMultipartUpload`. The value is a number of seconds, or a number followed by `s`, `m`, `h`, `d` or `w` (at most 100 years).
  - A copy and a completed multipart upload are new objects, so their time counts from when the copy is made or the upload completes.
  - `GET` and `HEAD` answer with `x-cairn-expires`.
- **A bucket default.** `PUT /-/admin/v1/buckets/<bucket>/ttl` with `{"ttl": "7d"}` (or `{"seconds": n}`) gives every new object in the bucket that time to live. `DELETE` removes the default. A write with `x-cairn-ttl: 0` opts out. Setting and removing the default is recorded in the audit trail, and the bucket's admin view shows `ttl_seconds`.
- **When the time runs out.** The object is gone at once for reads and listings on every protocol (S3, file API, WebDAV, SFTP, mounts), and its key can be written again. The lifecycle worker then deletes it, counted in `cairn_store_lifecycle_ttl_expired_total`. In versioned buckets it deletes the expired version itself rather than adding a delete marker, so its bytes go too; earlier versions stay.
- **Refusals.** A time to live is refused in buckets with Object Lock, since a locked object must not vanish. It is also refused when `CAIRN_STORE_LIFECYCLE_INTERVAL` is 0, because nothing would delete expired objects.

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

- **Targets.** Events go to targets the operator defines in `CAIRN_STORE_EVENT_TARGETS_FILE`. A rule names its target as the last part of the destination ARN (`arn:cairn:store:::lake`).
  - **Webhooks:** `{"type": "webhook", "id": "cairn", "url": "http://...", "secret": "..."}`.
  - **Kafka topics:** `{"type": "kafka", "id": "lake", "brokers": ["kafka:9092"], "topic": "..."}`.
  - **Amazon SQS queues:** `{"type": "sqs", "id": "q", "queue_url": "https://sqs.eu-west-1.amazonaws.com/<account>/<queue>", "access_key": "...", "secret_key_file": "..."}`.
    - `region` is read from the queue URL, or given for other SQS-compatible services.
    - The event id is sent as the `x-cairn-event-id` message attribute.
    - FIFO queues (`.fifo`) get the bucket as message group and the event id as deduplication id.
  - **Google Cloud Pub/Sub topics:** `{"type": "pubsub", "id": "ps", "topic": "projects/<p>/topics/<t>", "auth": "service_account", "credentials_file": "..."}`.
    - `auth` works as for GCS remotes: `service_account`, `metadata` or `none`.
    - `endpoint` defaults to Google's.
    - Attributes `x-cairn-event-id` and `key` (`<bucket>/<key>`) are set on each message.
    - `"ordered": true` publishes with the bucket as ordering key.
  - **NATS subjects:** `{"type": "nats", "id": "n", "url": "nats://nats:4222", "subject": "cairn.lake"}`.
    - Use `tls://` for TLS, verified against the system roots and `CAIRN_STORE_CA_FILE`.
    - Optional `token` (or `token_file`), or `user` and `password` (or `password_file`).
    - An event counts as delivered once the server has processed it.
    - The event id goes as the `Nats-Msg-Id` header, which JetStream uses to drop duplicates. Bind a JetStream stream to the subject to keep messages for consumers that are away.

  The store will not start with a target that does not fit its type, such as a bad URL scheme, a malformed Pub/Sub topic, a NATS subject with spaces or wildcards, or missing credentials. Deliveries are at least once, so consumers should drop repeats by the event id.
- **Webhook signatures.** Webhook bodies are signed: `X-Cairn-Signature: sha256=<HMAC-SHA256 of the body with the target's secret>`.
- **No lost events.** An event is recorded in the same transaction as the change it describes. It is delivered at least once, in order for each target, with retries and backoff, and survives a crash or restart of the store.
- **Delivery that keeps failing.** Events still undeliverable after `CAIRN_STORE_EVENT_MAX_AGE_HOURS` (default 168) are dropped and counted.
- **Metrics.** `cairn_store_events_{delivered,failed,dropped}_total` and `cairn_store_events_pending`.
- **Helm.** Put the targets file in a Secret and set `cairn.store.eventTargetsSecret`.

**File API and directory browser.** `/-/files/<bucket>/<path>` serves buckets as plain files and directories, for scripts and browsers.

- `GET` or `HEAD` a file reads it (ranges and conditional requests work); `PUT` writes it; `DELETE` removes it. `?versionId=` picks a version.
- `GET` a directory (a path ending in `/`, a bucket, or `/-/files/` for the list of buckets) lists it, as JSON (`{"path", "entries": [{"name", "type", "size", "modified", "etag", "storage_class"}], "next"}`) or, for browsers (`Accept: text/html`), as an HTML page. Page through with `?limit=` (up to 1000) and `?after=<next>`.
- **Same rules as S3.** Each request is the S3 operation it stands for (GetObject, PutObject, DeleteObject, ListObjectsV2 with a `/` delimiter, ListBuckets), so bucket and key policies, Cairn policies, quotas, encryption, events, the audit trail and access logs all apply. Only `versionId`, `limit` and `after` are accepted; S3 subresources such as `?acl` stay on the S3 API.
- **Image thumbnails.** `GET` an image with `?width=`, `?height=` and optionally `?fit=contain|cover|fill` (contain by default: fit inside the box, keeping proportions) returns a resized copy in the image's own format, for JPEG, PNG, GIF (first frame) and WebP. The read is the GetObject it stands for: authorized, audited and metered the same way. Images are never enlarged. Sources are limited to 64 MiB and 50 megapixels, targets to 4096 x 4096, and a resize cannot be combined with a `Range`. The copy carries its own ETag and is not cached by the store, so put a CDN or cache in front for busy thumbnails.
- **Signing in.** Sign requests with SigV4 (`curl --aws-sigv4 "aws:amz:us-east-1:s3" --user KEY:SECRET https://store/-/files/lake/report.csv`) or use a presigned URL. Anonymous requests work only where a public bucket policy allows them. For browsers, `CAIRN_STORE_FILES_BASIC_AUTH=true` also accepts HTTP Basic (the access key and secret key). This needs TLS, because Basic sends the secret with every request; set `CAIRN_STORE_FILES_BASIC_ALLOW_HTTP=true` only when TLS ends in front of the store. In Helm: `cairn.store.filesBasicAuth`.
- **Browser safety.** Files are served with `Content-Security-Policy: sandbox` and `nosniff`, so an HTML file in a bucket cannot run script with the viewer's credentials. Listing pages allow no script. Writes take no form posts, so another site cannot write through the viewer's browser unless the bucket's CORS rules allow that site.
- Errors are JSON: `{"error": "NoSuchKey", "message": "..."}`.

**WebDAV.** `/-/dav/<bucket>/<path>` serves buckets over WebDAV (classes 1 and 2), for rclone, davfs2, cadaver, WinSCP and other WebDAV clients. For example: `rclone config create cairn webdav url=https://store.example.com/-/dav/ vendor=other user=KEY pass=SECRET`.

- **Methods.** PROPFIND (depth 0, 1 and infinity; infinity only for trees of up to 10,000 keys), GET, HEAD, PUT, DELETE, MKCOL, COPY, MOVE, PROPPATCH, LOCK and UNLOCK.
- **Properties.** PROPPATCH sets and removes dead properties: a request's changes all apply or none do. They are kept as text in the object's user metadata (`x-amz-meta-dav-props`), so they share S3's 2 KB per object; past that the change fails with `507`. A collection keeps its properties on its marker object. Live properties (`DAV:getetag` and the like) are protected: changing one answers `403`, and the rest of that request `424`. A change rewrites the object's metadata (a copy onto itself), conditional on the version read.
- **Locks.** LOCK takes an exclusive or shared write lock, to depth 0 or infinity, for `Timeout` seconds (600 by default, 3600 at most; clients refresh to keep it). LOCK on an unmapped URL creates an empty file.
  - **Who can use a lock.** A WebDAV write to a locked resource must present the lock's token in an `If` header (`423 Locked` otherwise), and only the key that took the lock can use its token, refresh it or UNLOCK it.
  - **What a lock covers.** A lock to depth infinity covers everything below its collection, members added later too. Writes that add or remove a member also need the token of a lock on its collection.
  - **Shared and exclusive.** Shared locks coexist; an exclusive lock never shares.
  - **`If` headers** are evaluated in full (state tokens, entity tags, `Not`, tagged lists): one that does not hold fails the request with `412`.
  - **Shared by every node.** Locks are kept in the store's metadata (embedded or PostgreSQL), so every node of a cluster honours them. Expired locks are cleared.
  - **WebDAV only.** Locks bind WebDAV clients. S3, SFTP and the mount do not take part, because S3 has no locking. Use object lock (retention) when every protocol must be stopped.
- **Tested** with litmus, the WebDAV protocol test suite: `basic`, `copymove`, `props` and `locks` all pass. Further tests cover tokens bound to their key, deep locks over new members and moves, conflicts both ways, shared locks, expiry, and lock discovery.
- **Collections** are key prefixes. MKCOL writes an empty `name/` marker object, as S3 consoles do. As WebDAV requires, PUT and MKCOL into a collection that does not exist answer 409 and create nothing. This check is made only for callers who may write the resource and list its parent, so write-only keys (drop boxes) learn nothing about what exists. MKCOL on `/-/dav/<bucket>` creates the bucket, for keys allowed to create buckets. Whole buckets cannot be deleted, copied or moved over WebDAV.
- **Same rules as S3.** Each item is the S3 operation it stands for (ListObjectsV2, GetObject, PutObject, CopyObject, DeleteObject), with the same policies, quotas, encryption, events and audit trail. A collection DELETE, COPY or MOVE first checks every key it would touch and changes nothing if any is refused. These operations are limited to 10,000 keys; use the S3 API for larger trees. A MOVE is a copy followed by a delete, so it is not atomic.
- **Renames are atomic.** A MOVE within an unversioned bucket, of one file or a whole directory, is one index transaction: every key moves with its data, metadata and modification time, or none does, and no bytes are copied. Every key is authorized first, encrypted objects get their data keys re-wrapped for their new names, events and replication see a delete and a copy per key, and the move is one `object.rename` entry in the audit trail. Moves across buckets, or in a versioned bucket (where history stays with the old keys), are a copy and then a delete. SFTP renames and renames through `cairn-store mount` use the same path, for directories too.
- **Signing in** works as for the file API. WebDAV clients use HTTP Basic, so turn on `CAIRN_STORE_FILES_BASIC_AUTH` and serve TLS.

**SFTP.** Set `CAIRN_STORE_SFTP_ADDR` (for example `:2022`) to serve SFTP. It works with `sftp`, WinSCP, FileZilla, rclone and other file-transfer clients. In Helm: `cairn.store.sftp.enabled`, with a separate `-store-sftp` Service and network-policy peers in `cairn.store.sftp.ingressFrom`.

- **Paths** are `/<bucket>/<key>`. `/` lists the buckets the key may see. `mkdir` on a bucket name creates the bucket, for keys allowed to create buckets.
- **Signing in.** The SSH user name is an access key. It signs in with its secret key as the password (turn this off with `CAIRN_STORE_SFTP_PASSWORD_AUTH=false`), or with an SSH public key listed under `"ssh_keys"` for that key in the credentials file. A key that does not parse stops the store at start-up. Only the `sftp` subsystem is served: no shell, commands or forwarding. Refused sign-ins are logged and counted (`cairn_store_sftp_auth_failures_total`).
- **Host key.** `CAIRN_STORE_SFTP_HOST_KEY` (an OpenSSH private key). Without it, an Ed25519 key is created as `sftp_host_ed25519_key` in the data directory on first start and kept, so clients see the same host key after restarts. Its fingerprint is logged at start-up. In Helm, `cairn.store.sftp.hostKeySecret` mounts one from a Secret.
- **Same rules as S3.** Each operation is the S3 operation it stands for: a ranged GetObject per read, PutObject when an upload is closed, DeleteObject, CopyObject, and ListObjectsV2 with a `/` delimiter. Policies, Cairn policies, quotas, encryption, events and the audit trail all apply.
- **Uploads** are staged in the data directory's temporary area and stored in one step when the file is closed, so readers never see half a file. A dropped connection leaves nothing behind.
- **Refused rather than faked:** appends, in-place updates and out-of-order writes (files are written from the start, in order); changing permissions or timestamps; links; and renaming directories (use WebDAV MOVE or the S3 API). A rename never overwrites an existing file.

**Mounting a bucket (FUSE).** `cairn-store mount <bucket>[/prefix] <directory>` shows a bucket, or a prefix of it, as a local directory on Linux, for tools that only work with files. The `cairn-store` binary is static, so it can be copied to any Linux host with FUSE. Unmount with `fusermount3 -u <directory>` (or `umount`).

```sh
CAIRN_STORE_ENDPOINT=https://store.example.com CAIRN_STORE_ACCESS_KEY=... CAIRN_STORE_SECRET_KEY=... \
  cairn-store mount lake/raw /mnt/raw            # add --read-only, or --allow-other to share the mount
```

- **An ordinary client.** The mount talks to the store over the S3 API, signed with the key it is given, so that key's policies, Cairn policies, quotas, encryption, events and the audit trail apply as for any client. The mount fails at once if the key cannot list the bucket.
- **Reads** are ranged GETs. **Writes** are staged in a private, sparse temporary file (`CAIRN_STORE_MOUNT_STAGING`, default the system temporary directory) and stored when the file is closed or flushed, so other readers never see half a file. Overwriting a file (`>`, `cp`) replaces it in one step.
- **Appends and edits in place** work, as do writes at any offset, holes and `truncate` to any size. The file is handled in 8 MiB chunks: a chunk is fetched only when a write covers part of it, and a file over 16 MiB is stored as a multipart upload whose unchanged chunks are copied inside the store, so changing a few bytes of a large file sends one chunk. Each store is conditional on the version the edit started from: if the file changed elsewhere meanwhile, the close fails with `ESTALE` and the other version stays.
- **Directories** are key prefixes. `mkdir` writes an empty `name/` marker object, as S3 consoles do. `rmdir` works only on empty directories.
- **Modes, owners, times and extended attributes** are kept in each object's user metadata, under the same headers s3fs-fuse uses: `x-amz-meta-mode`, `-uid`, `-gid`, `-mtime` and `-xattr`. So `chmod`, `chown`, `touch`, `cp -p`, `tar` and `rsync -a` work, and S3 clients see the values.
  - New files and directories keep the mode they were created with.
  - A change rewrites the object's metadata in place: a copy onto itself, conditional on the version it read, so a concurrent writer is never overwritten.
  - A directory that exists only because keys sit below it gets a marker object to hold its metadata.
  - Objects without these headers show as `0644` files and `0755` directories, owned by the mount's user (`--uid`, `--gid`).
  - Extended attributes must be in the `user.` namespace. They share S3's 2 KB of user metadata per object; going over it fails with `E2BIG`.
  - The access time is not kept.
- **Permissions.** The store's policies always decide what the mount's key may do. With `--enforce-modes` the kernel also checks modes and owners before every access, as on a local disk (use it with `--allow-other` when several local users share a mount).
- **Symbolic links** are objects whose content is the target, with a mode marking them as links. **Hard links** fail with `EPERM`: a bucket cannot give one object two names.
- **Renames** of files and directories are one transaction in an unversioned bucket, and keep metadata. In a versioned bucket a file rename is a copy and a delete, and a directory rename returns `EXDEV`, so `mv` copies and deletes instead.
- **Tested** (`store/conformance/test_mount.py`):
  - modes, owners and times, read back after a rewrite, a rename and a new mount;
  - a directory implied by its keys gets a marker;
  - creation modes;
  - symbolic links, including relative and dangling ones;
  - extended attributes, including create-only, a foreign namespace and the size limit;
  - hard links refused;
  - with `--enforce-modes`, another local user can read a `0644` file but not a `0600` one.

**Kubernetes volumes (CSI).** `cairn-store csi` is a CSI driver (`csi.cairn.store`): PersistentVolumeClaims of its StorageClass become Cairn Store buckets, mounted into pods with the FUSE client above. Enable it in Helm with `cairn.store.csi.enabled`. That runs it on every node, with the standard registrar and provisioner sidecars, and creates the `cairn-store` StorageClass.

- **Volumes.** Each claim gets a bucket of its own, named after the volume, with the requested size as a hard quota. Or, with the StorageClass parameter `bucket` (`csi.storageClass.bucket`), each claim gets a prefix of one shared bucket. Each volume carries a marker object, so a bucket that already exists and is not a volume is never taken over. Deleting a claim (reclaim policy `Delete`) deletes its objects and bucket. Versioned and object-locked buckets are refused, so their history is not half deleted.
- **Mounts.** Any access mode works (many pods on many nodes can mount a bucket); block volumes are refused. Read-only claims and publishes mount read-only. The mount options `uid=N,gid=N` set who owns the files, for pods that run as non-root; other options are refused rather than ignored. A volume uses the key in its node-publish secret (`access_key`, `secret_key`) when given, otherwise the driver's own key, which must be allowed to create buckets.
- **Limits.** No snapshots, cloning or expansion. The driver container runs privileged, as FUSE drivers must, and needs `/dev/fuse` on each node.

**Hadoop, Spark and Hive.** Hadoop's S3A file system (`hadoop-aws`, with Hadoop 3.4) works with Cairn Store as it is. The conformance suite runs Hadoop's own client against the store: directories, small and multipart writes, positioned reads, listings, file and directory renames and recursive deletes. The settings:

```properties
fs.s3a.endpoint=https://store.example.com
fs.s3a.endpoint.region=us-east-1
fs.s3a.path.style.access=true
fs.s3a.access.key=...           # an access key scoped to the buckets the job uses
fs.s3a.secret.key=...
```

Use `s3a://<bucket>/<path>` paths. In Spark, prefix each setting with `spark.hadoop.`. S3A renames are copies followed by deletes, as on any S3 store, so write job output with an S3A committer (`fs.s3a.committer.name=magic` or `directory`). Hadoop sees the key's permissions: a key limited to some buckets cannot open others.

**Remote buckets: S3, Azure Blob Storage and Google Cloud Storage.** Storage tiers, mounted remote buckets and backup sinks all name a bucket elsewhere with the same fields. `kind` says how the store talks to it: `s3` (the default), `azure` or `gcs`. Azure and GCS are spoken natively, through their own APIs and sign-in, not through an S3 gateway.

- **`s3`**: `endpoint`, `bucket`, `prefix`, `access_key`, `secret_key` (or `secret_key_file`), `region` (default `us-east-1`). This covers AWS S3, MinIO, another Cairn Store and other S3-compatible services.
- **`azure`**: `bucket` is the container. `access_key` is the storage account. Sign in with one of two:
  - the account key in `secret_key` (or `secret_key_file`), which uses Shared Key signing;
  - a SAS token in `sas` (or `sas_file`).

  `endpoint` defaults to `https://<account>.blob.core.windows.net`; for Azurite, give `http://host:10000/<account>`. Blobs above 64 MiB are uploaded in 16 MiB blocks. User metadata whose name Azure does not allow (S3 allows `-`, Azure does not) is stored under an encoded name and read back under the original one.
- **`gcs`**: `bucket`, `prefix`, and `auth`, which is one of:
  - `service_account`: `credentials_file` is a service account JSON key. The store signs its token request and caches the token.
  - `metadata`: the service account of the VM or pod, from the metadata server. On GKE, bind the store's Kubernetes service account to a Google service account with workload identity, and set `cairn.store.serviceAccountName`.
  - `none`: emulators only.

  `endpoint` defaults to `https://storage.googleapis.com`.

Example: `[{"id": "lake", "kind": "gcs", "bucket": "acme-lake", "prefix": "raw/", "auth": "service_account", "credentials_file": "/etc/cairn-store-remotes/sa.json"}, {"id": "archive", "kind": "azure", "bucket": "archive", "access_key": "acmestore", "secret_key_file": "/etc/cairn-store-remotes/azure-key"}]`.

The store will not start with a field that does not apply to its kind (a `region` on Azure, keys on GCS), an unknown `kind`, or missing credentials. In Helm, the other keys of the Secret that holds `remotes.json`, `tiers.json` or `sinks.json` are mounted next to it, so key files can live in the same Secret. HDFS is not a remote kind: reach Hadoop data through S3A in the other direction (above).

**Storage tiers.** Lifecycle transitions can move object data to a bucket elsewhere (S3, another Cairn Store, Azure Blob Storage or Google Cloud Storage; see above), to keep cold data off the store's disk.

- **Tiers.** The operator defines them in `CAIRN_STORE_TIERS_FILE`: `[{"storage_class": "STANDARD_IA", "endpoint": "https://s3.eu-west-1.amazonaws.com", "bucket": "lake-cold", "prefix": "cairn/", "access_key": "...", "secret_key_file": "/etc/...", "region": "eu-west-1"}]`, with the fields of any remote bucket (above) and a `storage_class` instead of an `id`. A tier's storage class is one of `STANDARD_IA`, `ONEZONE_IA` or `GLACIER_IR`: classes that are read without a restore. In Helm, put `tiers.json` in a Secret and set `cairn.store.tiersSecret`.
- **Rules.** `Transition` (after `Days` or on a `Date`) and `NoncurrentVersionTransition` (after `NoncurrentDays`) name a tier by its storage class. A rule naming any other class is refused with `InvalidStorageClass`. Expiration wins over a transition due at the same time.
- **How data moves.** Each blob is copied to the tier exactly as it is stored on disk, so with encryption at rest the tier only ever holds ciphertext. The version's index entry then points at the tier, in one transaction and only if the version was not rewritten meanwhile. The local copy is then deleted. A failed copy is retried on the next pass.
- **Reads** are transparent: `GetObject`, ranges, copies and replication fetch the bytes from the tier. `HeadObject` and listings report the storage class. Copying an object onto itself with `StorageClass=STANDARD` brings it back to the store's disk.
- **Deletes.** Deleting a tiered version deletes its blobs in the tier, from the lifecycle worker; a tier that is down is retried.
- **Writes** are always `STANDARD` (`REDUCED_REDUNDANCY` is accepted and stored as `STANDARD`). Any other `x-amz-storage-class` is refused: objects reach a tier only through a transition.
- **Backups** do not copy tiered data. Protect the tier bucket with its own versioning or replication.
- **Metrics.** `cairn_store_tier_transitions_total`, `_transitioned_bytes_total`, `_transition_failures_total`, `_reads_total`, `_deletes_total`.

**Mounting remote buckets.** A local bucket can mirror a bucket elsewhere (AWS S3, MinIO, another Cairn Store, an Azure container or a GCS bucket; see above), so clients and engines use one store while the data stays where it is. Define remotes in `CAIRN_STORE_REMOTES_FILE`: `[{"id": "erp-exports", "endpoint": "https://s3.eu-west-1.amazonaws.com", "bucket": "acme-erp", "prefix": "exports/", "access_key": "...", "secret_key_file": "/etc/...", "region": "eu-west-1"}]`. Then mount one: `cairn-store admin remote-mount <bucket> --remote erp-exports [--write-back]` (or `PUT /-/admin/v1/buckets/<bucket>/remote`).

- **Sync.** Every `CAIRN_STORE_REMOTE_SYNC_INTERVAL` seconds (60), or on `remote-sync <bucket>`, the store lists the remote and makes each of its current objects a local object whose bytes stay on the remote: keys, sizes, ETags, content types and user metadata are local, so listings and `HeadObject` are fast. Objects changed or deleted on the remote follow at the next pass. A change made here and the remote's are settled as in two-way sync: the newer one wins.
- **Reads** fetch the bytes from the remote, ranges included, so nothing is copied until it is read. `remote-cache <bucket> [--prefix P]` copies objects' bytes to this store (encrypted at rest like any new object), so reads stay local and work while the remote is down. `remote-uncache` drops the local copy of objects the remote still has unchanged.
- **Write-back.** With `--write-back`, objects written or deleted here are written to or deleted on the remote, through the outbox: at least once, in order, retried while the remote is down. Without it, the mount is read-only towards the remote: local writes stay local, and local deletes do not touch the remote.
- **Rules.** A remote is mounted in one bucket at a time; `remote <bucket>` shows the mount, and how many objects are remote-only, cached and local-only. `remote-unmount` stops syncing and writing back; objects already mirrored stay readable while the remote is configured. Only the remote's current objects are mirrored, not its old versions. Mounts and unmounts are recorded in the audit trail. In Helm, put `remotes.json` in a Secret and set `cairn.store.remotesSecret`.

**Replication.** A bucket can copy its new objects to a bucket on another S3 endpoint (another Cairn Store, or any S3), set with `PutBucketReplication`.

- **Destinations.** The operator defines them in `CAIRN_STORE_REPLICATION_TARGETS_FILE`: `[{"id": "dr", "endpoint": "https://store.dr.example.com", "access_key": "...", "secret_key_file": "/etc/...", "region": "us-east-1"}]`. A rule names one as its Role (`arn:cairn:store:::dr`) and gives the destination bucket (`arn:aws:s3:::lake-copy`), so bucket owners can only send data where the operator allowed. In Helm, put `targets.json` in a Secret and set `cairn.store.replicationTargetsSecret`.
- **What is copied.** Versioning must be enabled. New versions matching a rule (by prefix and tags; rules have priorities) are copied with their metadata, tags, legal hold and the retention still in force (the destination bucket then needs object lock); delete markers too when the rule says `DeleteMarkerReplication: Enabled`. Permanent deletes of a version are not replicated, as in S3. Objects larger than `CAIRN_STORE_REPLICATION_MULTIPART_BYTES` (default 256 MiB) are sent as multipart uploads of `CAIRN_STORE_REPLICATION_PART_BYTES` (default 64 MiB).
- **Delivery.** A copy is recorded in the same transaction as the write, then delivered at least once and in order per destination, retried with backoff while the destination is down. `HeadObject` reports `x-amz-replication-status`: `PENDING`, then `COMPLETED`, or `FAILED` when the destination refuses the copy for good (for example, a missing bucket or a refused key) or it is still undeliverable after `CAIRN_STORE_EVENT_MAX_AGE_HOURS`.
- **Encryption.** Encrypted objects are decrypted and sent over the connection, so use `https` endpoints; the destination encrypts with its own keys. Objects encrypted with customer keys (SSE-C) cannot be read by the store and are marked `FAILED`.
- **Existing objects.** `cairn-store admin replication-resync <bucket>` (or `POST /-/admin/v1/buckets/<bucket>/replication/resync`) queues every current object that is not itself a replica, for example after adding a rule to a bucket that already has data. It is recorded in the audit trail.
- **Two-way sync.** Two stores can replicate the same bucket to each other (active-active): give each one the other as a target and a rule. A copy arrives marked as a replica (`x-amz-replication-status: REPLICA`) with its source's modification time, and is never replicated again, so nothing bounces back. When both sites change the same key, the newer version wins on both sides: a replica older than the version already there is acknowledged and dropped (ties go to the larger ETag), and a replicated delete gives way to a newer write. Writing replicas needs `s3:ReplicateObject` (or `s3:ReplicateDelete`) on the destination, and a replica timestamp more than five minutes in the future is refused. Keep the sites' clocks in sync (NTP); changes to tags and metadata alone are not replicated.
- **Not supported** (refused with `NotImplemented`, never accepted and ignored): the `ExistingObjectReplication` setting (use a resync), replication time control, replication metrics, destination encryption settings and source selection criteria.
- **Metrics.** `cairn_store_replication_delivered_total`, `_failed_total`, `_dropped_total`; `cairn_store_events_pending` counts the outbox, copies included.

**Static websites.** With `CAIRN_STORE_WEBSITE_DOMAIN` set (for example `sites.example.com`), a bucket with a website configuration (`PutBucketWebsite`) is served at `<bucket>.sites.example.com`. Configurations support index and error documents, redirect-all, routing rules that redirect by key prefix or error code, and per-object redirects (`x-amz-website-redirect-location`). Website requests are anonymous, so a site is served only if its bucket policy grants public read and public access is allowed: hosting never exposes a bucket by itself.

**Other S3 calls.** `GetObjectAttributes` (ETag, checksums, parts with their checksums, size). Request payment always answers `BucketOwner`, and setting `Requester` is refused. Transfer acceleration can only be `Suspended`. Intelligent tiering is refused with `NotImplemented` until that feature exists.

**Encryption at rest.** Give the store a key and it encrypts every new object (SSE-S3): set `CAIRN_STORE_MASTER_KEY` (a base64 32-byte key, `openssl rand -base64 32`), or `CAIRN_STORE_KEYRING_FILE` for several keys. Each object gets its own data key; its bytes are encrypted with AES-256-GCM in 64 KiB chunks, so ranged reads stay fast and any tampering is detected. The data key is stored only wrapped by a keyring key.

- **SSE-KMS.** Clients and bucket defaults can name a keyring key (`aws:kms` with a key id), for example one key per team.
- **SSE-C.** Customer-provided keys are supported; the store never stores them. They need TLS, or `CAIRN_STORE_SSE_C_ALLOW_HTTP=true` when TLS ends at a proxy in front of the store.
- **Bucket defaults.** `PutBucketEncryption` sets a bucket's default encryption.
- **Key rotation.** Add a key to the keyring and point `sse_s3_key` at it. Existing objects keep the key they were written with, so keep old keys until those objects are rewritten or deleted. An object whose key has been removed can no longer be read.
- **Back up the keyring.** Without it, encrypted objects cannot be recovered.
- **Customer-managed keys (HashiCorp Vault or OpenBao Transit).** A key the store never holds goes under `"external"` in the keyring file:

  ```json
  {"external": {"customer-a": {"type": "vault-transit", "addr": "https://vault:8200", "key": "cairn-a",
     "mount": "transit", "token_file": "/etc/cairn-store-keyring/vault-token", "cache_seconds": 60}}}
  ```

  It is used like any keyring key, by id: `aws:kms` with `customer-a`, a bucket default, or `sse_s3_key`. Each object's data key is encrypted by Vault when the object is written and decrypted by Vault when it is read. The object's name is bound in, so a wrapped key moved to another object does not open.
  - **The customer stays in control.** Revoke the store's token or policy and the objects can no longer be read or written. Restore it and they can. Delete the key in Vault and the data is gone for good (crypto-shredding). Changes take effect after `cache_seconds`: how long a decrypted data key stays in memory.
  - **Rotation.** Rotate the key in Vault; nothing is needed from the store. Old objects still open, and new ones use the latest version.
  - **The token.** Give the store a token whose policy allows only `update` on `transit/encrypt/<key>` and `transit/decrypt/<key>`. The token file is read on each call, so it can be renewed in place. In Helm, put `vault-token` in the same Secret as `keyring.json` (`cairn.store.secret.keyringSecret`). `namespace` sets Vault Enterprise's namespace.
  - **Not covered.** Access-key secrets stay wrapped by a local keyring key, since they are needed when the store starts.
  - **Fails closed.** A keyring whose external key does not load stops the store at start-up. When Vault refuses or cannot be reached, the request fails with `KMS.DisabledException`; nothing is written in the clear.
  - **Tested** with a real Vault (`store/conformance/test_kms_vault.py`): encryption, copies, rotation, revocation and restore, crypto-shredding, a token scoped to another key, and bad keyrings.
- **Customer-managed keys in AWS KMS** (or any service that speaks the KMS API). Use `"type": "aws-kms"`:

  ```json
  {"external": {"customer-b": {"type": "aws-kms", "region": "eu-west-1",
     "key": "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-...", "access_key": "AKIA...",
     "secret_key_file": "/etc/cairn-store-keyring/kms-secret", "cache_seconds": 60}}}
  ```

  - **How it works.** Each data key is encrypted with KMS `Encrypt` and decrypted with `Decrypt`, signed with SigV4. The object's name is bound in twice: in the plaintext, as with Vault, and as the encryption context (`cairn-object`). KMS checks the context on every decrypt, and CloudTrail records it. `key` is the key's id, ARN or alias.
  - **Credentials.** `access_key` with `secret_key_file` (and optionally `session_token_file`), each file read on every call. Without `access_key`, the store's environment is used (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`). Give them only `kms:Encrypt` and `kms:Decrypt` on the one key.
  - **Endpoint.** `addr` defaults to `https://kms.<region>.amazonaws.com`. Set it for a VPC endpoint or another KMS-compatible service.
  - **The customer stays in control.** Disabling the key, or removing the store's grant or permission, stops reads and writes after `cache_seconds`. Scheduling the key for deletion shreds the data.
  - **Tested** against a real KMS-compatible server (`nsmithuk/local-kms`, in `store/conformance/test_kms_aws.py`): encryption, copies, a key named by alias, a disabled and re-enabled key, a key pending deletion, a key that does not exist, and bad configurations. That server does not check signatures, so SigV4 is covered by the signer the store's other AWS clients share.
- **Customer-managed keys in Azure Key Vault** (or Managed HSM). Use `"type": "azure-keyvault"` with an RSA key that allows `wrapKey` and `unwrapKey`:

  ```json
  {"external": {"customer-c": {"type": "azure-keyvault", "addr": "https://acme.vault.azure.net", "key": "cairn-c",
     "auth": "workload-identity", "tenant_id": "<directory id>", "client_id": "<app id>",
     "federated_token_file": "/var/run/secrets/azure/tokens/azure-identity-token", "cache_seconds": 60}}}
  ```

  - **How it works.** Each data key, with the object's name bound in, is wrapped with RSA-OAEP-256 (`wrapkey`) and unwrapped with `unwrapkey`. The key version that wrapped it is kept with it, so old objects still open after a rotation. Without `version`, the store uses the key's current version, looked up again every `cache_seconds`; set `version` to pin one.
  - **Sign-in (`auth`).** `client-secret`: an app registration's secret in `client_secret_file`. `workload-identity`: the federated token AKS projects into the pod, in `federated_token_file`. `managed-identity`: the VM's or node's identity (`client_id` picks a user-assigned one). `token-file`: a ready access token, read on every call. Tokens are kept until a minute before they expire. `auth_addr` overrides the sign-in address (`https://login.microsoftonline.com`; the managed-identity endpoint for `managed-identity`), e.g. for a sovereign cloud.
  - **The customer stays in control.** Disabling the key version, or removing the store's role (`Key Vault Crypto Service Encryption User`), stops reads of the objects it wrapped after `cache_seconds`. Purging the key shreds the data.
- **Customer-managed keys in Google Cloud KMS.** Use `"type": "gcp-kms"` with a symmetric `ENCRYPT_DECRYPT` key:

  ```json
  {"external": {"customer-d": {"type": "gcp-kms", "key": "projects/acme/locations/europe-west1/keyRings/cairn/cryptoKeys/customer-d",
     "auth": "service-account", "credentials_file": "/etc/cairn-store-keyring/gcp-sa.json", "cache_seconds": 60}}}
  ```

  - **How it works.** Each data key is encrypted with Cloud KMS `encrypt` and decrypted with `decrypt`. The object's name is bound in the plaintext and as additional authenticated data (its SHA-256), which KMS checks on every decrypt. KMS picks the key version itself.
  - **Sign-in (`auth`).** `service-account`: a service account key file; the store signs its token request (RS256). `metadata`: the identity of the GKE workload or VM. `token-file`: a ready access token. Give the account only `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the one key. `addr` overrides `https://cloudkms.googleapis.com` (e.g. a private endpoint).
  - **The customer stays in control.** Disabling or destroying the key version, or removing the role, stops reads after `cache_seconds`.
- **Customer-managed keys in an HSM over KMIP.** Use `"type": "kmip"` for HSMs and key managers that speak KMIP 1.4 (Thales CipherTrust, Entrust KeyControl, Fortanix, IBM GKLM, PyKMIP and others), with an AES key that allows encrypt and decrypt:

  ```json
  {"external": {"customer-e": {"type": "kmip", "addr": "hsm.acme.internal:5696", "key": "<the key's unique id>",
     "ca_file": "/etc/cairn-store-keyring/kmip-ca.pem", "client_cert_file": "/etc/cairn-store-keyring/kmip.crt",
     "client_key_file": "/etc/cairn-store-keyring/kmip.key", "cache_seconds": 60}}}
  ```

  - **How it works.** The store connects with TLS and its client certificate (mutual TLS: the HSM decides by certificate what the store may use) and asks the HSM to encrypt each data key with AES-GCM, the object's name as the additional data. The key never leaves the HSM.
  - **The customer stays in control.** Revoking, deactivating or destroying the key, or removing the certificate's access, stops reads after `cache_seconds`.
- **Common to every customer-managed key.** The same fail-closed behaviour as Vault: a keyring that does not load stops the store; a refusal fails the request with `KMS.DisabledException` and logs the key manager's answer. Keep credential files in the keyring Secret. **Tested** against real servers (`store/conformance/test_kms_cloud.py`): Azure Key Vault on Lowkey Vault with each sign-in (client secret, workload identity, managed identity), rotation and a disabled version; Cloud KMS on floci-gcp with a signed service-account sign-in and a key the object was not written with; KMIP on PyKMIP's server, with a certificate the HSM refuses and a revoked key.

Objects written before a key was set stay unencrypted. Requests to set them are refused with `NotImplemented`; Cairn Store never accepts a setting it would ignore.

**Metadata in PostgreSQL.** By default the store keeps its index (buckets, objects, versions, uploads, settings, keys, events waiting to be sent and the audit trail) in an embedded database in the data directory. Set `CAIRN_STORE_META` to a PostgreSQL URL (`postgres://user:pass@host/db?sslmode=require`), or `CAIRN_STORE_META_FILE` to a file that holds one, to keep it in PostgreSQL instead. This suits teams that already run, back up and monitor PostgreSQL. In Helm, set `cairn.store.metadata.secretName`.

- **Same behaviour.** Every change is still one transaction, together with its events and audit entry, and writers are serialized as before. The full conformance suite runs on both backends.
- **Blobs stay on the data volume.** The data directory and its metadata database are bound together by a store id. A store refuses to start on another store's database, or on a database whose data directory has no store id. A data directory whose metadata is in PostgreSQL never opens with an empty embedded index, so `fsck` and `audit-verify` need `CAIRN_STORE_META` too.
- **Moving an existing store.** Stop it, then run `cairn-store migrate-meta` with `CAIRN_STORE_META` set to an empty database. Everything is copied in one transaction, and the embedded index is kept aside as `index.redb.migrated-<time>`.
- **Backups and TLS.** `cairn-store admin backup` still writes a data directory with an embedded index, which a store can start on, or which can be moved back with `migrate-meta`. Back up the PostgreSQL database with your usual tools as well. TLS to PostgreSQL uses the public roots plus `CAIRN_STORE_CA_FILE`.

**Small objects.** Objects up to `CAIRN_STORE_PACK_BELOW` bytes (64 KiB by default; `0` turns this off) are appended to shared pack files under `packs/` instead of getting a file each. Millions of small objects then become a few large files, and reading one is a seek. Pack files are closed at `CAIRN_STORE_PACK_FILE_BYTES` (1 GiB).

- **Same guarantees.** Packed bytes are written and synced before the index entry that refers to them, are encrypted like any other object, and work with ranges, versions, copies, replication, tiering and backups.
- **Deleting a small object** leaves dead space in its pack. Compaction runs every `CAIRN_STORE_COMPACT_INTERVAL` seconds (3600; `0` turns it off). It works on packs whose dead share is at least `CAIRN_STORE_COMPACT_DEAD_RATIO` (0.5): it copies their live objects into the current pack, switches each reference in one index transaction (only where the object is unchanged), and deletes the old pack once nothing refers to it. Packs that hold only deleted objects are removed. Compaction never runs during a backup.
- **On demand.** `cairn-store admin compact [--min-dead 0.3]` (or `POST /-/admin/v1/compact`) compacts now; it is recorded in the audit trail. `cairn-store admin stats` reports packs, their bytes and the live bytes in them.
- **Backups** copy pack files whole, and hard-link closed packs that are unchanged since the previous backup.

**Several disks.** A store can spread blob files over several disks, each with a type: `CAIRN_STORE_DISKS=/mnt/hdd2:hdd,/mnt/nvme1:ssd` adds disks to the data directory, whose own type is `CAIRN_STORE_DATA_DISK_TYPE` (`hdd`).

- **Placement.** New objects go to the disk with the most free space among those of the bucket's type. Buckets without one use the data directory's type. `cairn-store admin bucket-disk <bucket> --type ssd` pins a bucket (refused if the store has no disk of that type); without `--type` it goes back to the default. A pinned bucket whose disks are all gone gets 503 rather than writes on another type. Existing objects stay where they are.
- **Safe changes.** Each disk carries a marker naming its store and number, so the list can be reordered, and another store's disk is refused. The data directory remembers every disk it has used: if one is left out of the list, the store refuses to start rather than lose the blobs on it. The index, small-object packs and the audit trail stay in the data directory.
- **Clusters.** Copies and shards placed on other nodes go to a disk of the same type there; a node without one is skipped. Repair and rebalancing keep the type.
- **Seeing it.** `cairn-store admin disks` lists each disk with its free and total bytes, `/-/metrics` has `cairn_store_disk_free_bytes`, and backups and `fsck` cover every disk. In Helm, list them under `cairn.store.extraDisks` (`{name, type, size, storageClassName}`); each becomes a volume of its own per pod.

**Compression.** Set `CAIRN_STORE_COMPRESSION=zstd` to compress new object data at rest. CSV, JSON, logs and similar text often take a third or less of the space.

- **Ranged reads stay cheap.** Data is compressed in independent 1 MiB chunks, and each object records where its chunks start, so a read at any offset decompresses only the chunks it touches.
- **Only what pays.** An object whose first chunk does not compress by at least an eighth is stored as written (media, archives, Parquet), and already-compressed content types (images, video, audio, zip, gzip, Parquet, PDF) are not even tried.
- **Transparent.** Sizes, ETags, checksums, ranges, multipart, copies, versions, replication, tiering, clusters and backups all see the object, not the compressed bytes. With encryption at rest, data is compressed first and then encrypted.
- **Turning it off** affects only new writes: compressed objects stay readable. `cairn-store admin stats` reports the bytes written and stored since start. In Helm, set `cairn.store.compression: zstd`.

**Message queues.** Besides objects, the store keeps durable message topics under `/-/queues/v1/`, for small pipelines that need a queue without running a broker. Requests are signed with SigV4 like the admin API.

- **Topics.** Admin keys create them with `PUT /-/queues/v1/topics/<name>` (`{"retention_seconds": ..., "max_messages": ...}`), list them, and delete them with their messages. Creating, changing and deleting a topic is recorded in the audit trail.
- **Publishing.** `POST .../topics/<name>/messages` appends one message (the request body, up to 1 MiB, with its `content-type` and an optional `x-cairn-key`); `?batch` takes `{"messages": [{"value" or "value_base64", "key"}]}`, up to 1,000 at a time. A batch is written in one transaction, so an acknowledged publish survives a crash, and messages get gapless, increasing sequence numbers.
- **Consuming.** `GET .../messages?group=<g>&max=100` returns the messages after the group's committed offset, in order; `?wait=10` long-polls for up to 20 seconds. `POST .../groups/<g>` with `{"seq": n}` commits, so delivery is at least once: until a group commits, it gets the same messages again. Offsets never move back. `?after=<seq>` reads from a position without a group. `GET .../topics/<name>` shows each group's offset and lag.
- **Retention.** Messages older than `retention_seconds`, or beyond `max_messages`, are removed from the oldest, every `CAIRN_STORE_QUEUE_RETENTION_INTERVAL` seconds (60). A group behind the oldest kept message continues from there.
- **Access.** Other keys publish and consume on topics their bucket patterns allow (`lake-*` covers a `lake-feed` topic); read-only keys consume and commit only. Topics live in the store's metadata, so they work on PostgreSQL metadata and in clusters, and are part of backups. Metrics: `cairn_store_queue_published_total`, `_delivered_total`, `_trimmed_total`.
- **Partitions.** A topic has `partitions` (1 by default, up to 64; `PUT` with a higher number adds some, and they are never removed). Order, sequence numbers and group offsets are per partition. A message goes to:
  - `?partition=p`, when given;
  - otherwise, the partition its key hashes to (Kafka's murmur2, so the HTTP API and Kafka producers agree). A batch's keys must all hash to one partition;
  - otherwise, the next partition in turn.

  Reads and commits name a `partition` (default 0), and `GET .../topics/<name>` shows every partition's bounds and group lags.
- **The Kafka protocol.** With `CAIRN_STORE_KAFKA_ADDR` (e.g. `:9092`), Kafka clients use these topics: producers, consumers, consumer groups and the admin client. In Helm, set `cairn.store.kafka.enabled`.
  - Kafka offsets are sequence numbers minus one. A group's committed offsets are the queue group's offsets, so the HTTP API and Kafka consumers see the same progress.
  - **Sign-in.** SASL/PLAIN, with an access key as the user name and its secret as the password. Keys reach topics as above, and a wrong secret is refused. Nothing but the version and SASL requests is answered before sign-in.
  - **TLS.** The listener uses TLS (SASL_SSL) with the store's certificate. Without one, it starts only with `CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT=true`, since PLAIN sends the secret as it is.
  - **Advertised address.** `CAIRN_STORE_KAFKA_ADVERTISED` is the `host:port` clients reach. It defaults to the listen address, which must then name a host. Helm defaults it to the release's kafka Service.
  - **Requests.** ApiVersions, SASL, Metadata, Produce (record batches, uncompressed), Fetch (long polling), ListOffsets (earliest, latest, by time), CreateTopics (admin keys; replication factor 1, since the store keeps its own copies), FindCoordinator, OffsetCommit/Fetch, and JoinGroup, SyncGroup, Heartbeat and LeaveGroup. Partitions are assigned by the clients' own assignors.
  - **Not offered.** Compressed batches (refused with `UNSUPPORTED_COMPRESSION_TYPE`, so set `compression.type=none`), transactions and idempotent producers (set `enable.idempotence=false` on Java clients).
  - **Clusters.** The node a group's members connect to keeps the group's membership, so point all members of a group at one node or address.
- **When to use a Kafka cluster instead.** For very high throughput, compression, transactions or exactly-once processing, use Cairn's Kafka, which the store's event notifications can feed. These queues favour simplicity and transactional durability.

**Durability.** A write is acknowledged only once its bytes and its index entry are on disk. It is readable immediately afterwards. Conditional writes are atomic. After a crash the store keeps every acknowledged write and drops any half-finished ones when it starts. `cairn-store fsck -data /data` checks a stopped store and reports objects whose data is missing.

**Backups.** With `CAIRN_STORE_BACKUP_DIR` set, `cairn-store admin backup <name>` (or `POST /-/admin/v1/backups`) writes an online backup while the store keeps serving.

- **Consistent.** A backup holds the index as of one moment and every blob it refers to. Writers are not paused; deleted blobs are kept until the backup has copied them.
- **Incremental.** Blobs already in the newest earlier backup are hard-linked, so repeated backups take little extra space. The backup directory can be on its own volume; copy backups elsewhere with a tool that preserves hard links (`rsync -H`).
- **Restoring.** A backup is a complete data directory: check it with `cairn-store fsck -data <backup>`, and restore by starting the store on a copy of it. Event deliveries that were pending are not part of a backup, so they are not sent again. Backups are recorded in the audit trail.
- **Helm.** Set `cairn.store.backups.enabled` for a second volume.

**Backups to S3-compatible storage, Azure and GCS.** Backups can also go to a bucket elsewhere: another Cairn Store, AWS S3, MinIO, an Azure container or a GCS bucket (see the remote bucket fields above). Define sinks in `CAIRN_STORE_BACKUP_SINKS_FILE`: `[{"id": "offsite", "endpoint": "https://s3.eu-west-1.amazonaws.com", "bucket": "acme-store-backups", "prefix": "prod", "access_key": "...", "secret_key_file": "/etc/...", "region": "eu-west-1"}]`.

- **Writing one.** `cairn-store admin backup <name> --sink offsite` (or `POST /-/admin/v1/backups` with `"sink"`) takes the same consistent snapshot as a local backup while the store keeps serving, then uploads the index and every blob it refers to.
- **Incremental.** Blobs are immutable, so each one is uploaded once and shared by every backup in the sink; a later backup uploads only what is new. Packs of small objects are shared once they are closed.
- **Encrypted at rest stays encrypted.** Blobs are uploaded exactly as stored, so the sink holds ciphertext for encrypted objects. Keep the keyring separately: a restored store needs it.
- **Listing and deleting.** `cairn-store admin backups --sink offsite` lists complete backups (a backup counts only once its manifest is written, last). `backup-delete <name> --sink offsite` removes it, then the blobs no remaining backup refers to. Use one sink prefix per store.
- **On a schedule.** `CAIRN_STORE_BACKUP_SINK_SCHEDULE=offsite` writes a backup every `CAIRN_STORE_BACKUP_SINK_INTERVAL` seconds (86400), named `auto-<time>`, and keeps the newest `CAIRN_STORE_BACKUP_SINK_KEEP` (7). A failed run is logged and retried at the next interval.
- **Continuously, with point-in-time restore.** With `CAIRN_STORE_BACKUP_CONTINUOUS=offsite`, every object write and delete (from any interface, lifecycle and replication included) is recorded in the same transaction as the change, then shipped to the sink's journal (`journal/<sequence>.json`, after the blobs it names), at least once and in order, retried while the sink is down. Each backup records how far in the journal it goes. `cairn-store restore-backup offsite latest /new/data --until 2026-11-01T09:30:00Z` restores the newest backup made before that time and replays the journal up to it; without `--until`, up to the last change shipped. The journal covers objects (their data, metadata, tags and encryption); bucket settings come from the backup. Replayed changes become the current versions, so the history of old versions between backups is not rebuilt. Deleting a backup also prunes the journal from before the oldest one left. In Helm, set `cairn.store.backupSinks.continuous`.
- **Restoring.** `cairn-store restore-backup offsite <name> /new/data` downloads a backup into a new, empty data directory, checking every blob's length. It has an embedded index: start a store on it, or move its metadata into PostgreSQL with `migrate-meta`.
- **Audit and Helm.** Backups and deletions are recorded in the audit trail. In Helm, put `sinks.json` in a Secret and set `cairn.store.backupSinks.secretName`, and optionally `schedule`, `intervalSeconds` and `keep`. Sinks are not available in a cluster.

**Clusters.** Several store nodes can share one PostgreSQL metadata database and keep every object's bytes on more than one node's disk. Each node serves the whole S3 API (and the file API, WebDAV and SFTP), so a load balancer can send any request to any node, and a write on one node is readable on every node at once.

- **Turning it on.** On each node, set `CAIRN_STORE_CLUSTER_NODE` (its name: lowercase letters, digits and `-`), `CAIRN_STORE_CLUSTER_URL` (its address for the other nodes, such as `http://store-0.store:8333`), the same `CAIRN_STORE_META`, and the same `CAIRN_STORE_CLUSTER_SECRET` (16 characters or more, or `_FILE`). Give every node the same keyring, credentials and tier, replication and event settings. A data directory remembers its node name, and a node refuses to start under another name, without PostgreSQL metadata, or with a setting a cluster cannot honour (`CAIRN_STORE_PACK_BELOW`, `CAIRN_STORE_BACKUP_DIR`).
- **Copies.** Each blob is written to `CAIRN_STORE_CLUSTER_COPIES` (2) distinct nodes, the receiving node's own disk first, before the index entry that refers to it is committed. If fewer nodes are up than the write needs, it fails with `503 ServiceUnavailable` and nothing becomes visible: the store never keeps fewer copies than configured.
- **Racks and zones.** Give each node `CAIRN_STORE_CLUSTER_ZONE` (a data center or availability zone) and `CAIRN_STORE_CLUSTER_RACK` (a rack or host). Placement always spreads copies across zones first, then racks, then nodes. Set `CAIRN_STORE_CLUSTER_SPREAD=rack` or `zone` to make that a rule: copies then never share a rack (or zone), erasure-coded blobs keep at most `m` shards in one, so losing a whole rack or zone loses no data, and a write that cannot meet the rule fails with 503. A node that does not say where it is refuses to start under such a rule. When the rule is added to a running cluster, repair moves the copies that share a domain once there is somewhere better, and rebalancing never breaks it. `cairn-store admin cluster` shows each node's zone and rack. In Helm, each node's rack is its Kubernetes node and `cluster.zone` names the release's zone; set `cluster.spread: rack` once the store runs on at least as many Kubernetes nodes as copies.
- **Erasure coding.** With `CAIRN_STORE_CLUSTER_EC=k+m` (for example `4+2`), blobs of at least `CAIRN_STORE_CLUSTER_EC_ABOVE` bytes (1 MiB) are split into k data shards and m Reed-Solomon parity shards on k+m distinct nodes. Any k shards rebuild the data, so `4+2` survives two lost nodes for 1.5 times the space, where three copies would take three times. Smaller blobs are copied.
- **Cold data to erasure coding.** Hot data can stay as copies (fast to read and repair) while older data takes less space: with `CAIRN_STORE_CLUSTER_EC_AFTER=<seconds>`, the node running the shared work re-encodes replicated blobs of at least `CAIRN_STORE_CLUSTER_EC_CONVERT_ABOVE` bytes (1 MiB) whose object is that old into k+m shards, on its repair cycle. Set `CAIRN_STORE_CLUSTER_EC_ABOVE` high to write everything as copies first. `cairn-store admin cluster-encode [--older-than S] [--limit N]` runs a conversion now (audited). Shards follow the spread rule and the blob's disk type; each blob's id is switched in one transaction, only if the object is unchanged, and the old copies are deleted after. With 2 copies and 4+2 shards, a cold blob goes from twice its size to 1.5 times.
- **Reads.** A read takes this node's copy when it has one, otherwise a live node's, and moves to the next copy if one fails, even partway through. Erasure-coded reads take the data shards and rebuild from parity when some are missing. Ranged reads fetch only the stripes they need.
- **Failure and repair.** Nodes record a heartbeat every `CAIRN_STORE_CLUSTER_HEARTBEAT` seconds (5). A node silent for `CAIRN_STORE_CLUSTER_DEAD_AFTER` seconds (60) is dead. Every `CAIRN_STORE_CLUSTER_REPAIR_INTERVAL` seconds (300), repair re-creates the copies and shards that were on dead, draining or removed nodes, switches each blob's placement in one index transaction (only if the object is unchanged), and deletes the copies it replaced. A node that comes back rejoins by itself.
- **One node runs the shared work.** Event and replication delivery, lifecycle, deleting collected blobs and repair run on one node at a time, under a lease in the database. If that node dies, another takes over within the dead-after time. Access keys created on one node reach the others within a heartbeat.
- **Draining, removing, rebalancing.** `cairn-store admin cluster` lists the nodes (state, liveness, copies and bytes held) and the health of every blob (healthy, degraded, lost). `cluster-drain <node>` stops new data going to a node, and the next repair moves its copies off; `cluster-repair` runs a repair now. `cluster-remove <node>` takes a node out for good: its copies count as lost and are re-created, it stops if still running, and it refuses to start again until `cluster-activate <node>`. `cluster-rebalance [--limit N]` moves copies from the fullest nodes to the emptiest, for example after adding nodes. The same calls are under `/-/admin/v1/cluster`, and each one is recorded in the audit trail.
- **Joining an existing store.** Start a store that already keeps its metadata in PostgreSQL as a cluster node. Its blobs, small packed ones included, are placed on the cluster in the background, and are readable from every node once placed.
- **Between nodes.** Nodes talk over `/-/cluster/v1/`, and each call carries an HMAC of the request under the cluster secret. S3 keys do not open it.
  - **TLS by default.** The MAC covers the request, not the bytes, so node URLs must be `https://` (the node serves TLS with `CAIRN_STORE_TLS_CERT`, and `CAIRN_STORE_CA_FILE` trusts a private CA). A node with an `http://` URL refuses to start unless `CAIRN_STORE_CLUSTER_ALLOW_HTTP=true`, for networks you trust.
  - **Mutual TLS.** `CAIRN_STORE_CLUSTER_CA_FILE` names the CA that signs node certificates. Each node then presents its certificate on every call (`CAIRN_STORE_CLUSTER_TLS_CERT` and `_KEY`, by default its server certificate, which then needs the `clientAuth` usage too) and trusts only that CA for the other nodes.
  - **What mutual TLS refuses.** The node-to-node API refuses a connection without such a certificate, even with a valid signature. A certificate from another CA fails the handshake. S3 clients on the same port need no certificate.
  - **Renewals.** A renewed node certificate is picked up without a restart.
  - **In Helm,** a cluster needs `cairn.store.tlsSecret`, or `cluster.allowInsecureHttp=true`. `cluster.mutualTls` uses the `ca.crt` of that Secret, as cert-manager writes it. The certificate must name every node: `*.<release>-cairn-store-nodes.<namespace>.svc`.
  - **Tested:**
    - two nodes over mutual TLS copied a blob, and the second served it once the first was killed;
    - a signed call without a node certificate got `403`, and one with a certificate from another CA was refused in the handshake;
    - nodes with `http://` URLs, or with a cluster CA but no TLS, refused to start.
- **Backups and limits.** Online backups, local or to a sink, cover one disk, so they are off in a cluster: back up the PostgreSQL database, and rely on copies across nodes (and replication to another site) for the bytes. Usage counters are kept in PostgreSQL with every write and are not recounted at start-up. In Helm, set `cairn.store.cluster.enabled` with `replicas`, `copies` and optionally `erasureCoding`; the chart runs a StatefulSet with a volume per node.
- **Monitoring.** `/-/metrics` adds `cairn_store_cluster_nodes` and counters for copies placed, remote reads, failovers, reconstructions, repairs, moves and deletes.

**Limits.** A single store runs on one node: the volume, its backups and any replicated buckets are the only copies. Keep backups on other storage, or take volume snapshots, or run a cluster. Objects can be up to 5 GiB in one request, and up to 10,000 parts of 5 GiB each with multipart uploads.

**Quotas and the admin API.** Cairn's admin API for the store lives under `/-/admin/v1/`. Requests are signed with SigV4 by an admin key, so any AWS SDK signer works; bodies and responses are JSON.

- **Statistics:** `GET stats`.
- **Buckets:** `GET buckets` and `GET buckets/<name>` report each bucket's usage (stored bytes, including old versions and unfinished uploads, and the number of object versions) and its settings.
- **Quotas:** `PUT buckets/<name>/quota` with `{"max_bytes": ..., "max_objects": ...}` sets a quota, and `DELETE` removes it.

A write that would take a bucket over its quota is refused with `QuotaExceeded`, inside the same transaction, so concurrent uploads cannot slip past the limit. Deletes and overwrites that shrink a bucket always work. Usage is updated with every write, and recounted from what is stored each time a single store starts.

**Access keys while the store runs.** Admin keys can create, change, rotate and delete access keys without a restart: in **Settings → Storage → Access keys**, through `cairn-store admin keys | key <name> | key-create | key-update | key-rotate | key-delete`, or through `/-/admin/v1/keys`.

- **Secrets.** The store generates every secret, a 256-bit random value, and shows it once, when the key is created or rotated. It is never listed again. With encryption at rest configured (`CAIRN_STORE_MASTER_KEY` or a keyring), stored secrets are wrapped with AES-256-GCM. If the keyring key that wrapped them is later removed, the store refuses to start rather than silently dropping those keys.
- **Scope.** A key can be limited to buckets (`lake-*`), made read-only or admin, or given its own IAM-style policy. It can also carry a Cairn identity (`actor`) and SSH public keys for SFTP. Policies and SSH keys are checked when they are saved.
- **Effect.** Every change applies to the next request, on S3, the file API, WebDAV, SFTP and the admin API. A rotated secret stops working at once. A deleted key is refused at once.
- **Audit.** Each change is written to the audit trail in the same transaction (`key.create`, `key.update`, `key.rotate`, `key.delete`), never with the secret. Changes made from Cairn are also recorded in Cairn's own audit log.
- **Rules.** Keys from the environment and `CAIRN_STORE_CREDENTIALS_FILE` are listed, but are changed only in that configuration. A key cannot delete itself. Stored keys are included in backups.

**IAM-compatible API.** The store answers the AWS IAM Query API, so `aws iam`, the AWS SDKs and Terraform's aws provider (pointed at the store's endpoint) can manage users, access keys and policies. Requests are `POST /` signed with SigV4 for the `iam` service; `aws sts get-caller-identity` (service `sts`) is also answered. The account id in ARNs is `000000000000`.

- **Users and access keys.** Supported actions:
  - `CreateUser`, `GetUser`, `ListUsers`, `DeleteUser`;
  - `CreateAccessKey`, `ListAccessKeys`, `UpdateAccessKey` (`Active` or `Inactive`), `DeleteAccessKey`.

  A user has at most two keys. An inactive key is refused like an unknown one. As in IAM, a user is deleted only once its keys, policies and group memberships are gone. IAM users' keys are listed in `/-/admin/v1/keys` with their `user` and `status`. They can be rotated or deleted there, but their permissions are changed only through IAM (`KeyManagedByIam`).
- **Policies.**
  - Inline: `PutUserPolicy` and `PutGroupPolicy`, with their `Get`, `List` and `Delete` counterparts.
  - Managed: `CreatePolicy`, `GetPolicy`, `ListPolicies`, `DeletePolicy`. Versions: `CreatePolicyVersion`, `GetPolicyVersion`, `ListPolicyVersions`, `SetDefaultPolicyVersion`, `DeletePolicyVersion` (five at most).
  - Attachments: `Attach` and `Detach` for users and groups, and `ListAttachedUserPolicies` / `ListAttachedGroupPolicies`.
  - Groups: `CreateGroup`, `GetGroup`, `ListGroups`, `ListGroupsForUser`, `AddUserToGroup`, `RemoveUserFromGroup`, `DeleteGroup`.

  Policy documents are checked like key policies: `s3:`, `iam:` and `sts:` actions, and `arn:aws:s3:::` or `arn:aws:iam::000000000000:` resources. A malformed document is refused with `MalformedPolicyDocument`. Other IAM actions (tags, roles, login profiles) answer `InvalidAction`.
- **Evaluation.** A user's keys carry its effective policy: its inline and attached policies, plus those of its groups. As in IAM, an explicit Deny wins, and otherwise something must Allow. A user with no policy may do nothing. Bucket policies can name a user as a principal (`"AWS": "arn:aws:iam::000000000000:user/alice"`). `aws:username` is the user's name.
- **Who may call it.** Admin keys may make any call. Other keys may call an action only where their policy allows `iam:<Action>` on the entity's ARN (`arn:aws:iam::000000000000:user/<path><name>`, `group/`, `policy/`). So a team lead can, for example, manage only the users under `/contractors/`.
- **Consistency.** Users, groups and policies are kept in the index. Each change is one transaction, together with the keys it creates or deletes and its audit entry (`iam.<Action>`). That holds on PostgreSQL metadata across a cluster too, and backups include IAM.

**Temporary credentials (STS).** Set `CAIRN_STORE_STS_SECRET` (32+ characters, the same on every cluster node; or `_FILE`; in Helm, `cairn.store.secret.stsSecretKey`) and the store answers AWS STS. Tools get short-lived, narrowly scoped keys instead of long-lived ones; any AWS SDK uses them as it uses AWS's.
- **Roles.** IAM roles (`aws iam create-role`, `put-role-policy`, `attach-role-policy`, `get-role`, `list-roles`, `update-assume-role-policy`, `delete-role`) have a trust policy, which says who may assume the role, and permission policies, which say what its sessions may do. A trust policy names only `sts:` actions. `MaxSessionDuration` runs from 3600 to 43200 seconds.
- **`AssumeRole`.** A session of a role whose trust policy names the caller, an IAM user by its ARN. If it trusts the whole account (`arn:aws:iam::000000000000:root`) or everyone, the caller's own policy must also allow `sts:AssumeRole` on the role. A `Policy` (2048 characters at most) narrows the session further: the session may only do what both the role and that policy allow. Cairn Catalog uses this to hand engines credentials for one table.
- **`GetSessionToken`.** A temporary key of the caller's own key, with that key's limits; a read-only key stays read-only. A session of an admin key is not an admin key.
- **`AssumeRoleWithWebIdentity`.** A signed-in person's OIDC token (RS256 JWT) buys a role's session; the request is unsigned, and the token is the credential. `CAIRN_STORE_STS_PROVIDERS` lists the issuers the store trusts: `[{"issuer": "cairn", "audiences": ["cairn-store"], "jwks_url": "…/v1/trino/jwks.json"}]`, or `jwks_file`. The trust policy names the issuer as `Federated` and can require `"<issuer>:sub"` and `"<issuer>:aud"` with `StringEquals` or `StringLike`. The session carries the person's Cairn identity (`urn:li:corpuser:<sub>`), so Cairn's policy decision point sees the person, not the role.
- **How a temporary key works.**
  - Its id starts with `ASIA`, and its secret is derived from the id with the STS secret, so neither is stored.
  - Its session token is the session's claims (role, expiry, session policy, identity), signed with the STS secret. Every node can check it, and an edited or swapped token is refused (`InvalidToken`).
  - Deleting a role ends its sessions at once. A session's policies are the role's current ones, so detaching one takes effect immediately.
  - Temporary keys cannot manage IAM, assume further roles or sign browser POST forms. Presigned URLs (SigV4 and SigV2) carry the token.
  - Every issue is audited (`sts.AssumeRole`, `sts.GetSessionToken`, `sts.AssumeRoleWithWebIdentity`) with the temporary key's id.
- **Off by default.** Without `CAIRN_STORE_STS_SECRET`, STS actions are refused. In Compose a development secret is set; change it.
- **Tested** with boto3's STS client: role and session-policy limits; edited, swapped and missing tokens; deleted roles; trust by name and by account; read-only and admin key sessions; web identity with good, foreign-signed, expired, wrong-audience and untrusted tokens; presigned URLs.

**Storage in the Cairn UI.** Cairn admins see **Settings → Storage**, which lists:

- every bucket, with its usage, its quota, and whether it has versioning, object lock, default encryption, lifecycle rules or a bucket policy;
- quotas, which can be set and removed from the page;
- the store's audit trail, with a button that verifies the hash chain from the beginning.

The control plane calls the store's admin API with the store's admin key (`CAIRN_STORE_URL`, `CAIRN_STORE_ADMIN_ACCESS_KEY`, `CAIRN_STORE_ADMIN_SECRET_KEY`; set automatically in Compose and Helm), and records quota changes in Cairn's own audit log too. Users never see the key.

**On the command line.** `cairn-store admin stats | buckets | bucket <name> | quota <name> --max-bytes N --max-objects N | quota-remove <name> | audit [--after SEQ] [--jsonl]` calls the same API. It is signed with `CAIRN_STORE_ACCESS_KEY` and `CAIRN_STORE_SECRET_KEY` (an admin key), against `CAIRN_STORE_ENDPOINT` or the local listener.

**Benchmarking.** `cairn-store bench` measures an S3 endpoint: this store, or any S3-compatible one, with the endpoint and key as for `cairn-store admin`.
- **Options:** `--objects 1000 --size 64KiB --concurrency 16 --ops put,get,list,delete`, with `--bucket` (default `cairn-bench`) and `--keep`.
- **What it does.** It writes the objects under a fresh prefix and reads them back, checking every byte count and ETag. It then lists and deletes them. It never touches other keys in the bucket.
- **The report** is JSON: for each phase, operations, errors, seconds, operations and MiB per second, and latency (mean, p50, p90, p99, max). Watch the Cairn Store dashboard while it runs.
- **Clean-up and exit codes.** A bucket the run created is removed again (unless `--keep`). The command exits 1 if any request failed.

**Monitoring.** `/-/healthy`, `/-/ready` and Prometheus metrics at `/-/metrics`: requests by operation and status, bytes in and out, buckets, objects, stored bytes, open uploads and noncurrent versions, per-bucket bytes and objects (`cairn_store_bucket_bytes`, `cairn_store_bucket_objects`), and lifecycle actions.

## Cairn Catalog

Cairn Catalog is Cairn's Iceberg REST catalog. Engines that speak the Iceberg REST protocol connect to it: Trino, Spark, Flink, pyiceberg and DuckDB. Table data and metadata files live in Cairn Store (or any S3 store); the catalog keeps track of each table's current metadata.

- **Docker Compose:** service `cairn-catalog`, at `http://cairn-catalog:8181` inside the stack and `http://localhost:8181` from the host. Set `CAIRN_CATALOG_TOKEN` in `.env`. The catalog writes to the store with the store's key, and new tables go under `s3://lakehouse/warehouse`.
- **Kubernetes:** set `cairn.catalog.enabled=true` (with `cairn.store.enabled=true`, or point `cairn.catalog.s3Endpoint` at another store). Create the Secret named in `cairn.catalog.secret`. Engines in other namespaces need an entry in `cairn.networkPolicy.catalogIngressFrom`.

**Connecting an engine.** Use the catalog URL and the token as a bearer token. For example, in pyiceberg: `RestCatalog("cairn", uri="http://cairn-catalog:8181", token="...")`. With each table the catalog also gives the engine the store's endpoint and keys, so engines need no storage settings of their own. Set `CAIRN_CATALOG_CLIENT_S3_ENDPOINT` when engines reach the store at another address.

**Which keys engines get** (`CAIRN_CATALOG_VEND_CREDENTIALS`):
- **`scoped`** (the default when `CAIRN_CATALOG_VEND_ROLE` is set; Compose turns it on). Each table comes with temporary keys from the store's STS that reach that table's location only. With a read-only catalog token they only read; otherwise they read, write and delete there.
  - **What engines hold.** No store key, so an engine cannot touch another table's files or anything else in the store. Trino's lakehouse catalog then has no S3 keys at all (`iceberg.rest-catalog.vended-credentials-enabled=true`), and pyiceberg needs only the catalog token.
  - **The role.** `CAIRN_CATALOG_VEND_ROLE` names a store role. When it is missing, the catalog creates it at start-up, trusted only by the catalog's key and allowed the warehouse bucket; each session's policy narrows it to one table. An ARN is used as given.
  - **Lifetime.** Keys last `CAIRN_CATALOG_VEND_SECONDS` (3600; 900 to 43200) and are reused while at least a quarter of that is left.
  - **Fails closed.** The store must have STS on (`CAIRN_STORE_STS_SECRET`). The catalog checks at start-up and refuses to start otherwise, rather than falling back to its own key.
  - **In Helm,** set `cairn.catalog.vendRole`; the chart refuses it when this release's store has no `cairn.store.secret.stsSecretKey`.
- **`static`** (the default otherwise): the catalog's own store key, which reaches the whole store.
- **`off`**: engines bring their own storage keys.

**Tested:**
- **pyiceberg** wrote and read with vended keys only.
- **A table's keys** listed and read their own table, were refused another table's files and the rest of the bucket, and were reused across loads.
- **A read-only token's keys** could not write or delete.
- **Start-up** was refused without STS.
- **Trino 476** with no store key ran `CREATE TABLE AS`, `INSERT`, `UPDATE`, `DELETE` and `optimize`.
- **Compose:** the catalog created its role, and Trino wrote and read the lakehouse with no key in its environment.

**Tokens.** `CAIRN_CATALOG_TOKEN` is an admin token. `CAIRN_CATALOG_TOKENS_FILE` adds more tokens as a JSON list. Each token can be limited to namespaces (`"namespaces": ["sales", "sales.*"]`) or to reading (`"read_only": true`).

**What it supports.**

- Namespaces, nested, with properties.
- Tables: create (including staged creates for create-table-as-select), load, commit, rename, drop, and register an existing metadata file.
- Multi-table transactions, and metrics reports (accepted and dropped).

Server-side scan planning and remote request signing are not supported yet.

**Commits.** Table metadata is checked and updated by iceberg-rust, the Apache Iceberg project's Rust library, so the catalog applies the table spec the same way the reference implementations do.

1. A commit writes a new metadata file.
2. The catalog then switches the table to that file, but only if the table has not changed since the commit was prepared.
3. A writer that lost the race gets `409 CommitFailedException`. Engines refresh and retry by themselves.

A multi-table transaction changes every table or none.

**Views.** The catalog serves Iceberg views (the REST spec's views API: list, create, load, replace, rename, drop), so `CREATE VIEW`, `CREATE OR REPLACE VIEW`, `ALTER VIEW … RENAME`, `COMMENT ON VIEW` and `DROP VIEW` work in Trino, and pyiceberg lists them.
- **How they are kept.** A view's metadata file is written under its location, and every replace adds a version, checked with `assert-view-uuid`. A view and a table cannot share a name.
- **Who may change them.** Read-only tokens read views but change none; namespace-scoped tokens see only their namespaces.
- **Materialized views.** Trino has none on REST catalogs (`createMaterializedView is not supported`), so Cairn keeps them; see [Materialized views](#materialized-views).
- **Tested:** the REST API (create, conflict, load, replace, stale replace, list, scope, drop), and Trino 476 (create, query, replace, comment, rename, name clashes both ways, read-only token, drop).

**Where its state lives.** By default it is embedded, on one volume (`/data`), and the catalog runs as a single replica. Back that volume up together with Cairn Store.

**Several replicas (high availability).** Set `CAIRN_CATALOG_DATABASE_URL` (or `_FILE`) to a PostgreSQL URL. The catalog then keeps namespaces, tables, views and staged creates there, and any number of replicas serve the same lakehouse.
- **Commits stay safe.** Every change is one transaction. A writer lock in PostgreSQL makes replicas take turns, so a commit's compare-and-set cannot interleave with another replica's, and a multi-table transaction still changes every table or none.
- **TLS.** Connections use TLS unless the URL says `sslmode=disable`. `CAIRN_CATALOG_DATABASE_CA_FILE` adds a CA.
- **One database per lakehouse.** The first catalog to connect records its warehouse in the database. A catalog with a different warehouse is refused at start-up.
- **Scoped credentials.** Replicas starting at the same time all try to create the same store role. The first one wins; the others use the role it created.
- **In Helm,** create a Secret with the URL and set `cairn.catalog.database.existingSecret` (key `urlKey`, default `url`) and `cairn.catalog.replicas`. The catalog then runs as a Deployment with no volume. It rolls out one pod at a time and has a PodDisruptionBudget; `cairn.ha.spreadReplicas` spreads pods over nodes and zones. The chart refuses `replicas` above 1 without a database.
- **Back up** the PostgreSQL database together with Cairn Store.
- **Tested.** CI runs the whole conformance suite twice: once on the embedded state, once on PostgreSQL. The PostgreSQL run adds three replicas sharing one database:
  - a table written through one replica was read, appended and renamed through another;
  - a commit prepared on stale metadata was refused by the other replica, and pyiceberg retried it on top;
  - twelve racing creates of one table, spread over the replicas, left exactly one winner;
  - nine concurrent appends lost none;
  - the lakehouse kept working after a replica was killed.

  The replicas start with scoped credentials on, so they also race to create the store role.

**Sharing tables outside the organisation.** A share gives a recipient, such as a partner or an auditor, read access to named tables until it expires or is revoked. The recipient reads with any Iceberg REST client (pyiceberg, Spark, Trino, Snowflake, DuckDB) from Cairn Catalog. Shares are listed under **Pipelines → Shares**.

- **Making a share.** `POST /v1/shares` with `{"name": "partner-a", "recipient": "Partner A", "tables": ["analytics.products"], "expires_days": 30}`. You need MANAGE on every table you share. The answer holds the recipient's credential and connection settings, and is the only time the credential is shown. Only a hash of it is kept.
- **What the recipient sees.** Only the shared tables and their namespaces, read-only. Their storage keys are the catalog's scoped keys for one table at a time, read-only. A share never gets the store's own key, and the catalog refuses shares unless `CAIRN_CATALOG_VEND_CREDENTIALS=scoped`.
- **Masks and row filters.** A table to which a column mask or row filter applies is never shared, because its files hold what they hide. This is checked when the share is made and again on every request.
- **Revoking.** `POST /v1/shares/{id}/revoke`, or **Revoke** in the UI, stops the share at once. The catalog asks Cairn about the recipient on every request, without caching, so the next request and the next token renewal are refused. Storage keys already handed out last at most the catalog's credential lifetime (`CAIRN_CATALOG_VEND_SECONDS`). Expiry works the same way.
- **Tokens.** The recipient's client gets its token with the credential (OAuth 2.0 client credentials) at `POST /v1/shares/oauth/tokens`. Each token lasts `CAIRN_SHARES_TOKEN_TTL` seconds (default 900).
- **Settings.** `CAIRN_SHARES_CATALOG_URL` and `CAIRN_SHARES_TOKEN_URL` are the catalog and token endpoint as recipients reach them; both must be public. In Helm, set them under `cairn.catalog.identity.shares`. `CAIRN_SHARES_MAX_DAYS` (default 365) is the longest a share may last.
- **Who sees shares.** People see the shares they made. Operators (MANAGE_SYSTEM_OPERATIONS) see and revoke them all.
- **Audit.** Creating and revoking a share, and every token a recipient gets, are audited (category `shares`).

**Data clean rooms.** Two or more parties join their tables in aggregate, and none of them sees the others' rows (`/v1/clean-rooms`).

- **A room** names its `parties` (Cairn users or groups) and `min_group` (k, default 10: the smallest group a result may describe). Each party brings tables it may read itself (`POST /v1/clean-rooms/<room>/tables`).
- **Templates.** Any party proposes a query (`PUT .../templates/<name>`, SQL over the room's tables with `:params`), and it runs only once every party has approved its current text. Changing the text clears the approvals. Cairn checks each one when it is proposed: it reads only the room's tables, it is an aggregate (no rows, no `SELECT *`), and its parameters are declared.
- **Small groups never leave.** At run time (`POST .../templates/<name>/run`), arguments are bound as typed literals, and Cairn adds `HAVING count(*) >= k` itself, whatever the template says.
- **The room's own identity.** Queries run on Trino as `cleanroom-<room>`. Cairn's access control lets it read the contributed tables, without masks or row filters (the parties agreed to bring them), and nothing else. Nobody can sign in under that name, whatever their identity provider calls them. Proposals, approvals and runs are audited with who asked.
- **Tested** on Trino (`tests/test_trino_e2e.py`). A retailer and an advertiser joined buyers to clicks. The template ran only after both approved, and the one-person APAC group was never released. Row templates, `SELECT *`, another table and a non-party were refused, and so was a changed template until it was approved again. Neither party could read the other's table, and the room's identity was refused a table outside the room.


The recipient connects like this:

```python
from pyiceberg.catalog.rest import RestCatalog
cat = RestCatalog("partner", uri="https://lake.example.com", credential="<the credential>", scope="catalog",
                  **{"oauth2-server-uri": "https://cairn.example.com/v1/shares/oauth/tokens"})
cat.load_table("analytics.products").scan().to_arrow()
```

Tested in `tests/test_shares_e2e.py`, with the real store and catalog and pyiceberg as the recipient.

**Zero-copy clones.** `POST /v1/namespaces/{ns}/tables/{table}/clone` with `{"name": "dev_orders"}` (and optionally `"namespace": ["dev"]`) makes a new table on the same data files. Nothing is copied. The clone has its own id and location, so its new writes go to its own location, and the two tables change independently. The caller needs read access to the source and manage access to the target namespace.

- Neither table may expire a snapshot they share, because expiring it would delete data files the other still reads. Such a commit is refused with 409 ("shared with a clone"). Once the clone is dropped, the source may expire those snapshots again.
- The catalog records shared snapshots in `cairn.*` table properties. Clients may not set or remove `cairn.*` properties, on tables or on namespaces (403).

**Undrop.** A table dropped without `purge` can be brought back for `CAIRN_CATALOG_UNDROP_DAYS` days (default 7):

- `GET /v1/namespaces/{ns}/dropped` lists the tables that can be brought back.
- `POST /v1/namespaces/{ns}/tables/{table}/undrop` brings one back with its history.

A purged table cannot be brought back. Undrop fails with 409 if a new table now has the name. A dropped clone stops holding its source's snapshots, so the source may expire them. The clone can then no longer be brought back, because its data files may be gone (409).

**Branches and tags.** The catalog keeps Iceberg branches and tags, so you can work in write-audit-publish order: write to a branch, check it, then publish it to `main`. Engines create and use them:

- pyiceberg: `manage_snapshots().create_branch(…)`, `append(…, branch="audit")` and `set_current_snapshot(…)`.
- Spark: `ALTER TABLE … CREATE BRANCH` and `spark.wap.branch`.
- Trino reads a tag or branch with `FOR VERSION AS OF 'release-1'`.

Clones, undrop and write-audit-publish with pyiceberg are tested in `catalog/conformance/test_clones.py`.

**Limits.** Dropping a table with `purge` removes its current metadata file. Data files are left in the store.

**Trino.** Trino is the SQL engine for the lakehouse, through its Iceberg connector. It runs on Cairn Catalog's tables, and their data stays in Cairn Store.
- **Running it.** In Compose, `docker compose -f docker-compose.cairn.yml --profile trino up -d` starts `cairn-trino` with HTTPS on `localhost:8443` (`CAIRN_MAPPED_TRINO_PORT`). In Helm, set `cairn.trino.enabled` (it needs `cairn.catalog.enabled` and `cairn.api.enabled`).
  - **Sizing:** `queryMaxMemory` and `resources`.
  - **Workers:** `workers: N` adds N worker pods. The coordinator then plans queries and leaves the work to them. Access control and lineage stay on the coordinator, and the nodes share a secret (the access key) for their internal traffic.
  - **Resource groups:** `cairn.trino.resourceGroups` (Trino's resource-groups JSON; `{}` turns it off). Compose uses the same groups, from `deploy/trino/resource-groups.json`. The defaults:
    - `global.cairn` for Cairn's own queries: the SQL editor, Ask-your-data and the catalog crawler (source `cairn`). 10 at a time, 30% of memory.
    - `global.pipelines` for sources named airflow, dbt, etl, dagster or prefect. 20 at a time.
    - `global.adhoc.<user>` for everyone else, each person in their own group. 5 at a time each, 20% of memory.

    A group's excess queries wait in its queue rather than starving the others.
  - **Autoscaling:** `cairn.trino.autoscaling` (`enabled`, `minWorkers`, `maxWorkers`, `targetCPUUtilizationPercentage`) adds a HorizontalPodAutoscaler on the workers; set `workers` to at least `minWorkers`. It scales in one worker at a time.
  - **Draining.** A removed worker drains first. Its preStop hook puts it in Trino's `SHUTTING_DOWN` state: it takes no new work, finishes what it runs, then exits. `gracefulShutdownSeconds` (120) sets the wait, and the pod's termination grace period allows for it. Workers check no queries themselves, since the coordinator asks Cairn, so their only rule lets the hook's user, `cairn-drain`, drain them. That user is just a name in a header, but the network policy limits who can reach a worker, and the most it can do is drain one.
  - **Disruption budget:** a PodDisruptionBudget lets drains and upgrades take one worker at a time.
  - **Fault-tolerant execution:** `cairn.trino.faultTolerance.enabled` sets Trino's `retry-policy` (`TASK`, the default, or `QUERY`). If a worker dies mid-query, its tasks run again on another worker and the query still succeeds. Trino stages intermediate data in a Cairn Store bucket for this:
    - Create the bucket (`exchangeBucket`, default `trino-exchange`).
    - Give Trino a key that reaches only that bucket, in the Secret named by `exchangeKeySecret`. Default name `cairn-trino-exchange`, keys `access-key` and `secret-key`.
    - The chart refuses fault tolerance without workers.
    - Retries cost staging I/O, so use it for long batch work (pipelines), not quick interactive queries.
  - **Spill:** `cairn.trino.spill.enabled` lets joins, aggregations and sorts that outgrow memory spill to an emptyDir on each node (`sizeLimit`, 25Gi), up to `maxUsedSpacePerNode` (20GB).
  - **File cache:** `cairn.trino.cache.enabled` keeps lakehouse data and metadata files on an emptyDir on each node (`maxSize` 10GB, `sizeLimit` 12Gi), so repeated reads skip the store. The cache is per pod and starts empty after a restart.
  - **The coordinator.** Trino runs one coordinator per cluster and has no coordinator failover: a query running when it dies fails and must be resubmitted. Everything else lives outside it: tables in Cairn Catalog and Cairn Store, sign-in and policies in Cairn. So a replacement coordinator serves the same lakehouse as soon as it starts, and workers rejoin it on their own. For no single coordinator at all, run several Trino clusters behind Trino Gateway; Cairn does not ship that.
  - **Tested as rendered.** `tests/test_trino_helm.py` renders the chart with TLS and a worker, then runs the coordinator, the worker and the init containers in Docker exactly as the pods would. It uses the rendered files, environment and commands, a `kubernetes.io/tls`-style key and certificate, and a real Cairn API, catalog and store. It checks sign-in over TLS, masks, row filters, a query spread over the worker, lineage and usage. It also checks the resource groups each query lands in. It runs the worker's preStop hook during a long query: the query finished, then the worker left. Finally it replaces the coordinator: a new worker joined it, and the tables, masks and sign-in all still worked. A second render turns on fault tolerance, spill and the cache with two workers. While a query is running, it kills a worker that holds some of the query's tasks: the query still returns the right answer, Trino reports the failed tasks it retried, and intermediate data went through the exchange bucket. Repeated reads fill the cache directory. CI runs it, the Trino end-to-end tests and the chart renders with Trino on. CI fails if any Trino test is skipped.
  - **Network policy:** with `cairn.networkPolicy.enabled`, only this release's pods reach Trino, plus the clients listed in `cairn.networkPolicy.trinoIngressFrom` (BI tools, notebooks).
- **Sign-in.** Trino believes only tokens Cairn signs. The Trino user is whoever Cairn says it is, not a name a client claims.
  - **Getting a token.** A signed-in user calls `POST /v1/trino/token` (optional `{"ttl_seconds": 600}`, 60 seconds to 12 hours; default `CAIRN_TRINO_TOKEN_TTL`, 3600). The answer has the `token` and the Trino `user`. Use it from the Trino CLI (`trino --server https://… --access-token <token> --user <user>`), JDBC (`accessToken`) or Python (`Authorization: Bearer`). Every token issued is audited (`trino.token_issued`). Service and agent credentials get no token.
  - **How Trino checks it.** Trino's JWT authenticator reads Cairn's public key from `/v1/trino/jwks.json`, which needs no sign-in, and requires the issuer (`CAIRN_TRINO_TOKEN_ISSUER`, default `cairn`) and the audience `trino`. A missing, edited, expired or foreign-signed token gets 401. Ana's token with `--user admin` is an impersonation: Trino asks Cairn, and Cairn refuses it without `MANAGE_SYSTEM_OPERATIONS`.
  - **The signing key.** It is made on first use and kept in Cairn's database (`cairn_trino_signing_keys`), so every API replica signs with the same key. It is encrypted with `CAIRN_TRINO_ACCESS_KEY`: the database alone cannot sign a token. Changing the access key therefore means deleting that row, which makes a new key and invalidates outstanding tokens. Trino reads the key when it starts, so Compose starts Trino once the API is healthy, and in Helm an init container waits for it.
  - **Single sign-on (web UI and BI tools).** Cairn is the OAuth2 server Trino signs people in with. Trino's web UI sends a person to Cairn's `/cairn/api/v1/trino/oauth/authorize`. A person already signed in to Cairn goes straight back with a one-time code; anyone else signs in to Cairn first. Trino exchanges the code (with its client secret) at `/v1/trino/oauth/token` for a Cairn-signed token.
    - **Clients.** The same flow serves clients' external authentication: the CLI's `--external-authentication`, JDBC `externalAuthentication=true`, and Python's `OAuth2Authentication`. The client opens the sign-in page in the browser, then picks up its token. BI tools need no copied token, and people sign in again when it expires.
    - **Safeguards.** There is no consent page, because Trino is Cairn's own engine. Codes are single-use, last 2 minutes, are kept hashed, and are bound to the redirect address and to a PKCE challenge when the client sends one. Cairn only sends people back to `CAIRN_TRINO_OAUTH_REDIRECT_URIS` (Trino's `https://<host>/oauth2/callback`, HTTPS only) and refuses any other address outright. The crawler identity cannot sign in, and every sign-in is audited (`trino.sign_in`).
    - **Settings.** In Compose this is on (`CAIRN_TRINO_OAUTH_SECRET`; Trino at `https://localhost:8443`). In Helm, set `cairn.trino.sso.enabled` and `sso.publicUrl` (Trino's HTTPS address); the client secret is `sso.clientSecretKey` in the access-key Secret, and TLS is required.
    - **Tested** with Trino 476 over TLS: the web UI signed in through Cairn; a person without a Cairn session was sent to Cairn's sign-in; a client without a token was told where to sign in, picked up a Cairn token after the browser sign-in, and was `ana` to Trino and Cairn's policies. Also tested: one-use codes, the client secret, the redirect address and PKCE.
  - **TLS.** Tokens travel only over HTTPS; plain HTTP stays for Trino's own nodes, which share a secret. In Compose, `cairn-trino-tls` makes a self-signed certificate once, into the `trinotls` volume: `keystore.p12` for Trino, `ca.pem` for clients. Mount your own with `CAIRN_TRINO_TLS_DIR`. In Helm, set `cairn.trino.tls.secretName` to a `kubernetes.io/tls` Secret; Trino serves it on `cairn.trino.tls.httpsPort` (8443). The chart refuses to render without it unless `cairn.trino.allowInsecureHttp` is true, which is for tests only because tokens then cross the network unencrypted.
  - **Tested** with Trino 476 over TLS: no token, a token signed with another key, an edited token and a claimed impersonation all refused; plain HTTP refused; the CLI and the SQL editor signed in with Cairn tokens. In Compose: 401 without a token, and a query with a token, the certificate verified against `ca.pem`, both from the host and from the API container.
- **The `lakehouse` catalog.** Both setups configure it from `deploy/trino/catalog/lakehouse.properties`:
  - the catalog's REST URI with its token as an OAuth2 bearer token (`iceberg.rest-catalog.security=OAUTH2`);
  - the store through Trino's native S3 support, with path-style access.

  Secrets come from the environment (`${ENV:...}`), never from the file itself. To give another Trino a scoped view, configure a second catalog with a namespace-scoped or read-only token. The catalog then enforces the scope for every statement.
- **What works.** The conformance suite runs Trino 476 against the store and catalog binaries:
  - creating schemas and partitioned tables, and `INSERT`, `UPDATE`, `DELETE` and `MERGE`;
  - time travel (`FOR VERSION AS OF`), the `$snapshots` and `$partitions` tables, `ALTER TABLE ... ADD COLUMN` and `EXECUTE optimize`;
  - `CREATE TABLE AS`;
  - tables shared both ways with pyiceberg;
  - a read-only token refused every change.
- **Access control through Cairn.** Trino asks Cairn's policy engine before every action, through Trino's OPA access-control plugin (`deploy/trino/access-control.properties`). Compose and Helm turn this on whenever Trino runs.
  - **Users.** The Trino user `alice` is `urn:li:corpuser:alice`; Trino groups are `urn:li:corpGroup:<group>`.
  - **Datasets.** A table `catalog.schema.table` is `urn:li:dataset:(urn:li:dataPlatform:trino,<catalog>.<schema>.<table>,PROD)`, the URN Cairn's Trino source gives it. `CAIRN_TRINO_CATALOGS` maps a catalog elsewhere, for example `{"lakehouse": {"platform": "iceberg", "database": ""}}`.
  - **Privileges.**
    - Seeing a table (listings, `SHOW COLUMNS`) needs `VIEW_ENTITY_PAGE`.
    - Reading rows needs `VIEW_DATASET_PROFILE`, as in the SQL editor.
    - Changing rows (`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`) needs `WRITE_DATASET`.
    - Creating, altering and dropping tables, views and schemas, and table procedures such as `optimize`, need `MANAGE_DATASET`.
    - Other users' queries and system operations need `MANAGE_SYSTEM_OPERATIONS`.
    - Operations Cairn does not know are refused.
    - **Iceberg metadata tables** (`<table>$<name>`) are decided on their table. `$snapshots`, `$history` and `$refs` hold no row values: whoever may read the table may read them. The others (`$files`, `$manifests`, `$partitions` and the rest) carry column bounds that can show masked values, so they need `MANAGE_DATASET` on the table. None takes masks or row filters.
    - **Connector table functions** (`<catalog>.system.<name>`, such as Iceberg's and Delta's `table_changes` or a JDBC catalog's `query`) return rows past masks and row filters, so only operators (`MANAGE_SYSTEM_OPERATIONS`) may call them. Value-free ones (`bucket`) stay open. A table's changes are read through [the change feed](#change-data-feed) instead.
    - **Who has them.** `WRITE_DATASET`, `MANAGE_DATASET` and `MANAGE_SYSTEM_OPERATIONS` are Cairn's own privileges, and Cairn Core policies never grant them. Trino has no Core fallback, so a request no Cairn policy allows is refused. The baseline bundle grants all three to the group `urn:li:corpGroup:lakehouse-admins` (policy `lakehouse-admins-manage-tables`). Add your lakehouse administrators to it (SCIM, or Core's group page), or write your own policy. Tested with a real Cairn Core: a user put in the group through SCIM created schemas and tables through Trino, while the Core admin, who is not in the group, was refused.
  - **Masks and filters.** Columns the caller's masking plan covers come back masked in SQL, so filters and joins see the masked values too. Every mask keeps the column's type:
    - **Text:** `partial` (`j•••@•••.com`), `hash` (a stable digest) or `redact` (`••••`).
    - **Numbers:** `partial` keeps one significant figure, cut toward zero, so 98,765.43 becomes 90,000.00 and never overflows the type. `hash` gives a stable number of the same type, so joins still match. `redact` gives 0.
    - **Dates and timestamps:** `partial` keeps the month, `hash` gives a stable date and `redact` gives 1970-01-01. Times keep the hour.
    - **Anything else:** booleans, binary, JSON, arrays, maps and rows become NULL.
    - **Tested** on Trino 476 for every strategy on 18 column types, including `char(n)`, `varchar(n)`, `decimal(p,s)`, `real`, `timestamp(p) with time zone` and `time`: the type is kept and NULL stays NULL.
   - **Row filters.** `row_filter` obligations (`{"expression": "region = 'EU'"}`, with `{user}` for the Trino user) become Trino row filters.
  - **The key.** Trino presents `CAIRN_TRINO_ACCESS_KEY` (at least 24 characters) in the request path. Requests without it get a 404. In Helm, put it in the Secret named by `cairn.trino.accessKeySecret`; the API and Trino both read it.
  - **Tested** with Trino 476 against a real Cairn API: hidden tables refused and left out of `SHOW TABLES`, labelled emails masked (`j•••@•••.com`, so `WHERE email = ...` finds nothing), row filters applied, and writes, drops and new schemas refused without the privilege.
- **Lineage from Trino.** Trino's OpenLineage event listener (`deploy/trino/event-listener.properties`, on in Compose and Helm) sends every query that writes to Cairn: `INSERT`, `CREATE TABLE AS`, `MERGE`, `UPDATE` and `DELETE`.
  - Each query becomes a run of a job named after the tables it writes (`trino:<catalog>.<schema>.<table>`), so repeated loads into a table share one job and its run history.
  - The job records the tables the query read and wrote, column-level lineage (which source columns feed which output columns), and the SQL.
  - Datasets carry the same URNs as access control, so lineage, policies and masking refer to the same datasets.
  - Tested with Trino 476: a `CREATE TABLE AS` and an `INSERT` recorded as two runs of one job, with `by_region.region` traced to `orders.region`.
- **Lineage on the asset page.** The Lineage tab draws the lineage around an asset: sources on the left, what uses it on the right, one to three hops (Trino's lineage included). Each asset shows its quality status and opens on selection.
  - **Paths.** Edges follow Cairn Core's lineage paths, so a two-hop source joins the asset between rather than the asset itself.
  - **Hidden assets.** Assets the viewer may not see are left out and counted. A path through one is joined around it with a dashed line, so the hidden asset is never named.
  - **The API.** `GET /embed/v1/graph?urn=…&depth=1..3` serves the same graph for the signed-in user (the lineage embed serves it for embed tokens).
  - **Tested** against a real Cairn Core: a Trino table's graph ran from the source table through the Trino job to the table.
- **Who reads what (usage).** Trino's HTTP event listener (`deploy/trino/usage-listener.properties`, on in Compose and Helm next to the lineage listener) sends every finished query to Cairn: who ran it, and the tables and columns it read. OpenLineage events do not name the user, so lineage alone cannot say who read a table.
  - **What Cairn records.** Each dataset's usage statistics (`datasetUsageStatistics`, one bucket per day): the number of queries, the distinct users, each user's count and how often each column was read. The usage tab, popularity and the AI steward's evidence read them.
  - **What counts as a read.** Only finished queries count. A failed or refused query, `EXPLAIN` and `DESCRIBE` do not, and neither do reads of Trino's own metadata (`system`, `information_schema`). Each query counts once, even when Trino resends it. Reads by `INSERT ... SELECT` count for the tables read; the write is lineage.
  - **Where counts live.** Cairn's database keeps them (`cairn_trino_usage`, 35 days), so a day's bucket adds up every API replica. A bucket Core cannot take now waits and is written by the retry loop, so a Core outage loses no usage.
  - **Tested** with Trino 476: ana's and bob's reads counted with their columns, and ana's refused read of `hr.salaries` not counted. Also tested against a real Cairn Core, including a query run while Core was stopped (counted once Core was back).
- **Trino's tables in the catalog.** Cairn reads each Trino connection with `run_as_user` into its catalog, so the SQL editor and Ask-your-data can query those tables. (They serve only catalogued tables.)
  - **How it reads.** Cairn reads `information_schema` and the table comments as its own crawler, `cairn-catalog`, signed in with a Cairn token. Cairn's access control lets the crawler see every table and column and read no rows. `/v1/trino/token` never issues a token for it.
  - **What it writes.** Every table becomes a dataset: description, properties (`trino_catalog`, `trino_schema`, `trino_table`), schema with Trino's types and column comments, and subtype (`Table` or `View`). Datasets use the URNs that access control, lineage and usage use (`CAIRN_TRINO_CATALOGS`). Give the connection's `catalog` the same platform and database.
  - **Dropped tables.** Tables gone since the last sync are marked removed, and the SQL editor then refuses them as not in the catalog.
  - **When it runs.** Every `CAIRN_TRINO_CATALOG_SYNC_SECONDS` (3600; 0 turns it off), with each run claimed so one API replica does it. Run it now with `POST /v1/trino/catalog-sync {"connection": "lakehouse"}`, which needs `MANAGE_INGESTION`. Each run is audited (`trino.catalog_synced`).
  - **The Trino connector in Ingestion.** It still works for a Trino Cairn does not sign in to; it needs the executor to install the `trino` plugin.
  - **Tested** with Trino 476: typed columns, comments and views synced; the crawler refused `SELECT` on a hidden table; the SQL editor queried a synced table; a dropped table marked removed. Also tested against a real Cairn Core in Compose.
- **The SQL editor and Ask-your-data on Trino.** Add a Trino connection with `run_as_user: true` (see `config/connections/connections.example.yml`):

  ```yaml
  lakehouse:
    url: trino://cairn@cairn-trino:8443/lakehouse?protocol=https&verify=/etc/cairn/trino-tls/ca.pem
    run_as_user: true
    catalog: {platform: trino, database: lakehouse}
  ```

  - **How a query runs.** Cairn checks the query as it does for every connection: catalogued tables only, the caller must be allowed to read each one, and results are masked. Trino then runs it as the signed-in user, so Trino's own checks (Cairn's policies, row filters included) apply as well. Row filters thus now reach the SQL editor and Ask-your-data.
  - **Authentication.** Each query signs in with a five-minute Cairn token for the caller, so Trino knows the user from the token. `protocol=https` selects TLS, and `verify` is the CA file to trust (`true` means the system's CAs; `false` turns checking off). The connection's CA file wins over `REQUESTS_CA_BUNDLE`. For a Trino that does not trust Cairn's tokens, put a password in the URL (`trino://cairn:<password>@host:443/lakehouse`): `cairn` then impersonates the caller, which needs `MANAGE_SYSTEM_OPERATIONS`. If Trino answers 401, the editor says that Trino did not accept Cairn's sign-in.
  - **Requirements.** It needs the `trino` extra (`cairn-platform[trino]`, part of `all`).
  - **Row cap.** The editor's row cap now goes onto the query's own `LIMIT` (a smaller limit the query already has is kept), so `ORDER BY` holds on engines that drop the order of a subquery, as Trino does.
- **When Cairn Core is down.** Trino sends each lineage event once and never resends it. An event Core cannot take now therefore waits in Cairn's database (`cairn_trino_lineage_outbox`), Trino gets a 202, and the query itself is unaffected.
  - **Retries.** Every API replica retries due events every `CAIRN_TRINO_LINEAGE_RETRY_SECONDS` (15), backing off from 15 seconds to 15 minutes. A replica claims an event before writing it, so each is written once.
  - **Dropped events.** An event Core rejects as invalid, or one still unwritten after `CAIRN_TRINO_LINEAGE_MAX_AGE_SECONDS` (7 days), is dropped and audited (`trino.lineage_dropped`).
  - **Watching it.** The gauge `cairn_trino_lineage_held` counts waiting batches. Only when the database cannot keep an event either does Trino get a 503.
  - **Tested** with a real Cairn Core in Compose (MySQL, OpenSearch, Kafka). Lineage from `CREATE TABLE AS` and `INSERT` reached Core with column lineage. With Core stopped, a `CREATE TABLE AS` still ran and its two events were held; 10 seconds after Core came back they were written and the job was in Core.
- **Table locations.** Namespaces report a `location` (their place under the warehouse, unless they set one), and new tables go under it with a unique suffix. Trino builds its table locations from it.

**dbt.** dbt projects run on the lakehouse through Trino, as a Cairn identity, and each run is recorded in Cairn.

- **Who a run is.** Each project runs as its own Cairn user, for example `svc-dbt`: create the user in Cairn and a personal access token for it. Cairn's policies then decide what the project may read and build, like for anyone:
  - `VIEW_DATASET_PROFILE` on its sources;
  - `WRITE_DATASET` and `MANAGE_DATASET` on its target schema;
  - `EDIT_LINEAGE` and `EDIT_ENTITY_ASSERTIONS` to record its runs.

  Masks and row filters apply to it. Lineage, usage and the audit trail name it.
- **No database password.** `cairn dbt <dbt arguments>` exchanges the Cairn token for a short-lived Trino token (`CAIRN_DBT_TOKEN_TTL`, 4 hours by default, at most 12; it must outlast the run). It writes a private profile (`method: jwt`, TLS) and runs dbt. The token never reaches the project or dbt's artifacts. With `CAIRN_CA_FILE`, Trino's certificate is checked against that CA only, whatever `REQUESTS_CA_BUNDLE` says. Queries run in Trino's `pipelines` resource group.
- **What a run records** (`POST /pipelines/dbt/runs`, sent by `cairn dbt` after `run`, `build`, `test`, `seed` or `snapshot`), on the datasets Trino names, for the models, seeds and snapshots the run built:
  - **Docs.** Descriptions and column descriptions fill only empty ones: a Trino comment or a description written in Cairn is never overwritten. `dbt.*` properties hold the unique id, materialization, package, path and last status. Catalog syncs keep them. To keep docs in the tables themselves, where every engine sees them, set `+persist_docs: {relation: true, columns: true}` in the project, as the example does.
  - **Tags and owners.** dbt `tags` become tags, and `meta.owner` (a Cairn user name) a technical owner. Both are added, never removed.
  - **Lineage.** Each model's upstream models and sources. dbt's lineage is replaced on every run, so a removed `ref` disappears, and lineage from elsewhere is kept. Trino's own query lineage still arrives as well.
  - **Tests.** Each dbt test becomes a check on the model it tests (pass, warn, fail or error), on its Quality tab, and a newly failing test tells the model's owners.
  - The run is audited (`dbt.run_recorded`).
- **Governance and the whole project** (from the manifest, on every run):
  - **Model properties.** A model's contract (enforced or not), access (public, protected, private), group (and the group's owner) and version become `dbt.*` properties.
  - **Deprecation.** A `deprecation_date` deprecates the dataset in Cairn, with its decommission date. Lifting it in dbt lifts only dbt's deprecation.
  - **Exposures** become dashboards (or the exposure's other type) downstream of the models they use, owned by their owner.
  - **Metrics.** MetricFlow semantic models and metrics arrive in the Context inbox as metric definitions, mapped to their datasets, for a steward to approve.
  - **Unit tests** are checks on their model, named `unit test: <name>`.
  - **Source freshness.** `cairn dbt source freshness` records a `source freshness` check on each source, PASS, WARN or FAIL, with its age and limits.
- **Run history.** Each run is kept: command, environment (`CAIRN_DBT_ENVIRONMENT`, `prod` by default), status, timing, and each node's result. They are served at `GET /pipelines/dbt/runs` and on the **Pipelines** page. Operators (`MANAGE_INGESTION`) see every run, and a pipeline user sees its own.
- **Slim CI.** A successful `run` or `build` saves its manifest as its environment's state (`GET /pipelines/dbt/state`). With `CAIRN_DBT_DEFER_TO=prod`, `cairn dbt` fetches prod's state and adds `--state … --defer`, so `cairn dbt build --select state:modified+` builds only what changed and defers the rest to prod. CI runs record their own environment (`CAIRN_DBT_ENVIRONMENT=ci`) and never become prod's state.
- **Docs site.** `cairn dbt docs generate` publishes dbt's docs site to Cairn, at `/cairn/api/pipelines/dbt/docs/<project>/`. Each viewer's manifest and catalog hold only the models, sources and tests of datasets they may see, with exposures and metrics limited to those. Publishing is audited (`dbt.docs_published`).
- **Packages from a private mirror.** `CAIRN_DBT_PACKAGE_HUB_URL` (Helm `cairn.dbt.packageHubUrl`) points `dbt deps` at a mirror of the dbt package hub, so builds need no internet access. Installing packages when the project's image is built works too.
- **Exit codes.** `cairn dbt` exits with dbt's code. If dbt succeeded but the run could not be recorded, it exits 3, so a scheduler notices; set `CAIRN_DBT_RECORD=optional` to only warn.
- **The image.** `cairn/dbt` (`docker/dbt.Dockerfile`) has dbt-core 1.12 and dbt-trino 1.11 and runs as a non-root user. It works with a read-only root filesystem and a read-only project: dbt writes only under `/tmp/cairn`, artifacts in `/tmp/cairn/target`. A project's own image adds its code: `FROM cairn/dbt` and `COPY . /project`.
- **Compose:** `docker compose -f docker-compose.cairn.yml --profile trino --profile dbt run --rm cairn-dbt build` runs `CAIRN_DBT_PROJECT` (default `examples/dbt/lakehouse`) with the token in `CAIRN_DBT_TOKEN`.
- **Helm:** `cairn.dbt.jobs` makes a CronJob per project. Each job sets:
  - `name`, `image`, `schedule` and `args` (default `build`);
  - `schema`, the default schema;
  - `tokenSecret`, the pipeline user's token;
  - `caSecret`, Trino's CA;
  - `timeoutSeconds` and `threads`.

  Jobs never overlap and are not retried, since a failed build is for a person to look at. The chart refuses jobs without `cairn.trino` and its TLS. Trino's certificate must name `<release>-cairn-trino`. Airflow can run the same image instead of a CronJob.
- **Tested** (`tests/test_dbt_e2e.py`, in CI against the `cairn/dbt` image as built) with dbt-core 1.12.6 and dbt-trino 1.11.0, on Trino 476, Cairn Catalog and Cairn Store:
  - **The build.** `svc-dbt`, signed in with its personal access token, built a seed, a table from a source and a view. The table's docs, owner, tag, lineage and properties landed on the datasets Trino names, and the view's lineage named both of its upstreams.
  - **Tests.** A passing and a failing test became checks on their models. dbt's exit code (1, for the failing test) came back after the run was recorded.
  - **Secrets.** The Trino token appeared in none of the project's or dbt's files.
  - **Crawls.** A catalog sync kept dbt's description and properties, and the next run's column docs.
  - **The example project.** It built, and with `persist_docs` its docs were in the Iceberg table and its columns, as Trino shows them.
  - **Policies.** `svc-dbt` was refused tables outside its grants and saw masked PII.
  - **Recording.** It was refused without the rights to record.
  - **The governed project** (`tests/test_dbt_more_e2e.py`): contract, access, group owner and deprecation landed on the datasets. The exposure became a dashboard over its model, the unit test a check and the metric an inbox finding. Source freshness passed while fresh and failed once stale. Runs were listed, and a CI build of one changed model deferred to prod's state without replacing it. The docs site showed `svc-dbt` its models and hid them from `bob`, and a package came from a stub private hub.

**Airflow.** Airflow 3 (3.3) orchestrates pipelines on the lakehouse: its tasks run as a Cairn identity, its runs and lineage go to Cairn, and people sign in to its UI through Cairn.

- **The image.** `cairn/airflow` (`docker/airflow.Dockerfile`) is the official Apache Airflow image with:
  - the FAB, OpenLineage and Kubernetes providers, pinned by Airflow's own constraints;
  - `cairn-airflow-provider` (`airflow-provider/`);
  - `cairn dbt` with dbt-core and dbt-trino in a virtualenv of their own, `/opt/cairn-dbt`, so dbt's dependencies never meet Airflow's.
- **Who a task is.** A `cairn` connection (`cairn_default`) holds Cairn's API URL and a pipeline user's Cairn token, for example `svc-airflow`. Put the token in a secrets backend in production, or in `AIRFLOW_CONN_CAIRN_DEFAULT`. Its `extra` names Trino (`trino_host`, `trino_port`, `trino_catalog`) and the CA (`ca_file`). Grant the user what its DAGs read and build, plus `EDIT_LINEAGE` and `EDIT_ENTITY_ASSERTIONS`.
- **Operators.**
  - `CairnTrinoOperator(sql=..., schema=...)` runs SQL on Trino with a fresh short-lived Trino token per task. Cairn's policies, masks and row filters apply, and the last statement's rows (at most 1000) are the task's result. Queries run in the `pipelines` resource group.
  - `CairnDbtOperator(project_dir=..., command="build", select=...)` runs `cairn dbt` (see **dbt**), so the run is recorded in Cairn. Artifacts, logs and packages go to a scratch directory per run, so projects can be read-only. The task fails when dbt fails, and when Cairn could not record a successful run.
  - The connection's CA file wins over `REQUESTS_CA_BUNDLE`.
- **Runs and lineage in Cairn.** Point OpenLineage's HTTP transport at Cairn (`AIRFLOW__OPENLINEAGE__TRANSPORT`: url `<cairn>`, endpoint `openlineage/api/v1/lineage`, `api_key` auth with the pipeline user's token). Each DAG becomes a pipeline in Cairn and each task a job with its runs. Trino's own lineage adds the tables each query read and wrote, named like the catalog's datasets.
- **Sign-in through Cairn.** Airflow's UI signs people in with Cairn as the OAuth2 server (client `airflow`, with its own secret, redirect addresses and token audience).
  - **Roles.** `webserver_config.py` calls `cairn_airflow.auth.configure(globals())`. The token's signature is checked against Cairn's published key, and the person's Cairn groups map to Airflow roles through `CAIRN_AIRFLOW_ROLES` (`cairn-admins=Admin;data-engineers=User`, or JSON).
  - **Fails closed.** Anyone in no mapped group signs in with the `Public` role, which sees nothing. Roles are synced at every sign-in, so removing someone from a group removes their access at their next sign-in.
  - **Kept apart from Trino.** Airflow's tokens are refused by Trino (another audience), and a code issued to one client cannot be redeemed by the other. Sign-ins are audited (`airflow.sign_in`).
  - **On Cairn's side,** set `CAIRN_AIRFLOW_OAUTH_SECRET` and `CAIRN_AIRFLOW_OAUTH_REDIRECT_URIS` (`https://<airflow>/auth/oauth-authorized/cairn`; plain HTTP only for `localhost`). This needs Trino's sign-in to be on (`CAIRN_TRINO_ACCESS_KEY`), whose signing key it uses.
- **Runs when data changes.** `CairnDatasetChangedTrigger(["lakehouse.sales.orders"])`, in an Airflow 3 `AssetWatcher`, runs a DAG whenever one of its tables changes, whoever wrote it.
  - **Where changes come from.** Cairn records each dataset a pipeline finished writing: completed OpenLineage runs (Spark, Airflow, Trino's own lineage) and the models of dbt runs. They are served at `GET /v1/datasets/changes`, in order by a cursor, only for datasets the caller may see, and kept 30 days.
  - **Naming tables.** URNs, or tables as `catalog.schema.table`, which `GET /v1/datasets/resolve` turns into URNs.
  - **No replay.** The trigger watches from when it starts.
- **Quality gates.** Both read Cairn's status: monitors, plus other tools' checks such as dbt tests, Great Expectations and Soda.
  - `CairnQualityGateOperator(datasets=[...])` fails the task, without retries, when a dataset is not good enough, and names what failed.
  - `CairnQualitySensor` waits until the datasets are good, in reschedule mode.
  - By default, PASS and WARN pass, unchecked datasets do not, and open incidents block.
- **Failed pipelines.** When a run fails, every table its job writes gets an incident ("[Pipeline] … failed"), and the owners of the job and the tables are notified (`pipeline.run_failed`). The job's next successful run resolves it. One incident per job and table, however often it fails. `CairnTrinoOperator` reports the tables its SQL reads and writes, so its runs, failed ones too, are tied to them.
- **Team access.** With `CAIRN_AIRFLOW_TEAM_ROLES=true`, each Cairn group a person is in also gives them the Airflow role `team:<group>`, plus `CairnMember`, which is Viewer without access to every DAG. A DAG grants its team access with `access_control={"team:finance": {"DAGs": {"can_read", "can_edit"}}}`, and people see only their teams' DAGs. A team's role is created when a DAG first names it.
- **dbt, one task per model.** `CairnDbtTaskGroup(group_id=..., project_dir=...)` builds one task per model, seed and snapshot from the project's `manifest.json` (from `dbt parse`, ahead of time), wired as dbt's DAG. Each task runs `cairn dbt build --select fqn:<node>` and records its run, so a failed model is retried alone and its tests gate what depends on it.
- **Airflow's own operators.** With `AIRFLOW__SECRETS__BACKEND=cairn_airflow.secrets.CairnSecretsBackend`, the connection `trino_lakehouse` gets a fresh Trino token for the pipeline user each time it is used. `SQLExecuteQueryOperator` and `TrinoOperator` then run under Cairn's policies with no stored password. The backend also serves `cairn_default`.
- **Lineage without a stored key.** Set OpenLineage's transport `auth` to `{"type": "cairn_airflow.lineage.CairnTokenProvider", "workload_token_file": "/var/run/secrets/cairn/token"}`. Lineage is then sent with the pipeline user's Cairn token, exchanged from the pod's token and renewed before it expires.
- **Tested** (`tests/test_airflow_more_e2e.py`, with the `cairn/airflow` image):
  - **Quality gate.** It failed while a check failed, naming it, and passed once the check passed.
  - **Failure incident.** A task refused by Cairn's policies raised an incident on the table it writes; the next good run resolved it and recorded a change.
  - **Trigger.** It fired when the table was written.
  - **Secrets backend.** Airflow's `SQLExecuteQueryOperator` read a table the pipeline user may see, and was refused one it may not.
  - **dbt.** The example project ran as one task per model, each recorded.
  - **Team access.** A person in `finance` got `team:finance` and `CairnMember` and could read the finance DAG but not HR's.
  - **Lineage.** It reached Cairn through the token provider, with the task's tables.
- **Compose:** `docker compose -f docker-compose.cairn.yml --profile trino --profile airflow up -d` starts Airflow's API server (`http://localhost:8090`), scheduler, DAG processor and triggerer. It uses LocalExecutor and an `airflow` database in Cairn's PostgreSQL, created by `airflow-init`. Tasks run with the token in `CAIRN_AIRFLOW_TOKEN`, on the DAGs in `CAIRN_AIRFLOW_DAGS` (default `examples/airflow/dags`) and the dbt projects in `examples/dbt`.
- **Kubernetes:** use the official Apache Airflow chart (1.20+) with `deploy/airflow/values-cairn.yaml`. It sets the `cairn/airflow` image, KubernetesExecutor (each task in its own pod), external PostgreSQL, secrets for the Cairn connection, OpenLineage and the OAuth secret, sign-in through Cairn with no local admin account, and DAGs from git. The file lists the Secrets to create. CI renders the chart with it.
- **Tested** (`tests/test_airflow_e2e.py`, in CI against the `cairn/airflow` image as built) with Airflow 3.3.2, Trino 476, Cairn Catalog and Cairn Store:
  - **A DAG.** It ran SQL on Trino and a dbt build as `svc-airflow`. Both tables were built, the dbt run was recorded, the DAG and both tasks reached Cairn through OpenLineage, and Trino's lineage named the source table.
  - **Policies.** A DAG reading a table `svc-airflow` may not see failed with Access Denied, and its dbt task never ran.
  - **Sign-in.** Airflow's API server (over TLS) sent the browser to Cairn. Signed in to Cairn, the browser came back and was signed in to Airflow. `ana` got the User role from her group, and `bob`, in no mapped group, got only Public. A token not signed by Cairn was refused.
  - **Compose.** In a Compose slice the API server became healthy, the scheduler, DAG processor and triggerer ran, the example DAG parsed, and `/auth/login/cairn` redirected to Cairn's sign-in, with PKCE.

**Spark and other engines that read files (pyiceberg, DuckDB).** Spark 4 runs on the lakehouse as a Cairn identity. Cairn decides every table it touches, the catalog vends storage credentials one table at a time, and its lineage reaches Cairn.

- **Why this is different from Trino.** Trino applies Cairn's masks and row filters inside each query. Spark reads a table's data files itself, and those files hold what a mask or filter would hide. So for these engines:
  - Cairn Catalog asks Cairn about each table, as the user (`POST /v1/lakehouse/decide`, with Cairn's service token; decisions are cached for 15 seconds).
  - A table with a mask or row filter for that user is **refused**: "Masks or row filters apply to this table for you. Query it through Trino." The user queries it through Trino, or through a view a data owner builds in Trino.
  - **Reading it anyway, governed.** `cairn_spark.governed_read(spark, "sales.orders")` reads such a table through Trino, as the same user, with Spark's JDBC reader. Trino applies their masks and row filters, so Spark gets what they may see and never the files. Spark pushes column choices and filters down to Trino.
    - **Parallel reads.** Give `partition_column` with `lower_bound`, `upper_bound` and `num_partitions`, or `predicates`: one WHERE clause per partition.
    - **Settings.** Set `CAIRN_TRINO_URL` (https) and `CAIRN_TRINO_CATALOG` (default `lakehouse`). Set `CAIRN_CA_FILE` at the same path on the driver and the executors. The image includes Trino's JDBC driver.
    - **The token.** The read signs in with a short Trino token from Cairn (`POST /v1/trino/token`), passed in the read's properties so Spark redacts it. The read must start within the token's life. Pipeline users get one the same way: with their personal access token, or a workload token exchanged at Cairn (`CAIRN_WORKLOAD_TOKEN_FILE`). Only the shared service token and agents are refused.
    - **Tested** with Spark, Trino and Cairn Catalog (`tests/test_spark_e2e.py`). Through Trino, `ana` saw her masked emails, also in a two-partition read with a pushed-down filter; `bob` saw only his rows; a table hidden from `ana` was refused. The catalog still refused Spark the files.
  - Tables the user may not see are not listed. Their namespaces show only what is visible.
  - Reading needs read. Appending, overwriting and deleting rows needs write. Creating, dropping, renaming and changing schema or properties needs manage. Namespaces need Cairn's system privilege.
  - Vended credentials are read-only unless the user may write the table. They cover only that table's location.
- **Fails closed.** The catalog refuses every Cairn-issued token when Cairn cannot answer (503), and any error in a decision decides nothing. It will not start with Cairn-issued tokens unless it vends scoped credentials (`CAIRN_CATALOG_VEND_ROLE`), since a store key would bypass the per-table decisions. Tokens are RS256, checked against Cairn's published key (`/v1/trino/jwks.json`) for issuer, audience `lakehouse`, expiry and not-before. Trino's and Airflow's tokens are refused (other audiences). The catalog's static tokens (`CAIRN_CATALOG_TOKEN`, for Trino and admin tools) work as before.
- **Who a job is.** A person, or a pipeline user such as `svc-spark` with a personal access token. `POST /v1/lakehouse/token` exchanges it for a **lakehouse session**: a first catalog token and a session credential. Opening a session is audited (`lakehouse.token_issued`). Service and agent tokens get no session.
- **Long jobs.** A job runs as long as its session lasts, without a long-lived token.
  - **Tokens.** Each catalog token lasts an hour (`CAIRN_LAKEHOUSE_TOKEN_TTL`). Iceberg gets and renews tokens at `POST /v1/lakehouse/oauth/tokens`, on the driver and on every executor. It uses the session credential (OAuth client credentials) or the current token (token exchange, RFC 8693).
  - **Checks on every renewal.** The session must not have been ended, and the user must not be suspended. A session ends after `CAIRN_LAKEHOUSE_MAX_SESSION` (7 days) in any case. If Cairn cannot check, the renewal is refused. Renewals are audited (`lakehouse.token_refreshed`).
  - **The credential.** Only its hash is kept. `cairn-spark` adds it to Spark's redaction, so it never shows in the Spark UI or event logs.
  - **Storage keys** are renewed through the catalog (`GET …/tables/{table}/credentials`), with the table decided again each time. `CAIRN_CATALOG_VEND_REVALIDATE_SECONDS` (360 seconds up to the keys' lifetime, `CAIRN_CATALOG_VEND_SECONDS`) sets how often clients must come back. A permission removed then reaches running jobs within that time.
- **Ending sessions.** `POST /v1/lakehouse/sessions/revoke` with `{"user": "..."}` (`MANAGE_SYSTEM_OPERATIONS`) ends all of a user's open sessions in one audited transaction (`lakehouse.sessions_revoked`). None is renewed again, so the job stops once its last token expires: within `CAIRN_LAKEHOUSE_TOKEN_TTL`, plus the catalog's minute of allowed clock skew. Suspending the user (SCIM) has the same effect at the next renewal. New sessions can still be opened.
- **Iceberg's client.** Use the session credential, not a bare token. Iceberg 1.10's REST client renews a token it was given only once, so a job given a token alone stops after two token lifetimes. `cairn_spark` sets this up. For your own settings, see `lakehouse_conf(...)`.
- **In a job.** `from cairn_spark import lakehouse_session; spark = lakehouse_session("my-job")` (package `cairn-spark`, `spark/`). It reads:
  - `CAIRN_URL`, Cairn's API;
  - `CAIRN_TOKEN` or `CAIRN_TOKEN_FILE`, the caller's Cairn token;
  - `CAIRN_CATALOG_URI`, Cairn Catalog;
  - `CAIRN_CA_FILE`, if needed.

  The `lakehouse` catalog is the default, so `spark.sql("SELECT … FROM analytics.products")` works. For `spark-submit --conf` settings instead, see `cairn_spark.lakehouse_conf(...)`.
- **Lineage.** The OpenLineage Spark listener sends each job's runs, with table and column lineage, to Cairn as the same user. It authenticates with the lakehouse session credential, which Cairn accepts at the OpenLineage endpoint only. So a job's lineage lasts as long as its session, not one token's hour, and stops when the session is ended or the user is suspended. Spark names tables by their storage paths, so Cairn renames the tables of Cairn Catalog (`CAIRN_LAKEHOUSE_CATALOG_URIS`: the catalog's addresses as engines reach it) to the datasets Trino and the catalog crawler name. Lineage from Trino, dbt and Spark then meets on one dataset.
- **The image.** `cairn/spark` (`docker/spark.Dockerfile`) is the official Apache Spark 4.0 image with:
  - Apache Iceberg 1.10 (Spark runtime and AWS bundle, for S3FileIO);
  - OpenLineage for Spark 1.55;
  - `cairn-spark`.
- **Turning it on.**
  - **Compose:** on by default (`CAIRN_CATALOG_JWKS_URL` and `CAIRN_CATALOG_CAIRN_URL` on `cairn-catalog`). `docker compose -f docker-compose.cairn.yml --profile spark run --rm cairn-spark /jobs/lakehouse_products.py` runs a job from `CAIRN_SPARK_JOBS` (default `examples/spark`) with the token in `CAIRN_SPARK_TOKEN`.
  - **Helm:** set `cairn.catalog.identity.enabled: true`. This needs `cairn.catalog.vendRole` and `cairn.trino.enabled`, or the chart refuses to render. Set `identity.publicUrl` to the catalog's address outside the cluster, if engines use one.
  - **Network policy:** Spark's pods need to reach the API (`cairn.networkPolicy.apiIngressFrom`), the catalog (`catalogIngressFrom`) and the store.
  - **Running jobs:** run `cairn/spark` (or `FROM cairn/spark` with your code) with `cairn-spark submit`, the Spark Operator, or Airflow (below).
- **JVM programs and SQL.** `cairn-spark {submit|sql|shell|pyspark} [Spark arguments]` runs any Spark program as a Cairn identity: Scala and Java jobs (`--class`), the `spark-sql` shell, Python files.
  - It opens the lakehouse session (the same variables as `lakehouse_session`) and passes the settings to Spark in a private properties file: mode 0600, removed afterwards. The session credential never appears on a command line.
  - The caller's `--properties-file` and `--conf` still apply.
- **Spark on Kubernetes, with no secret in the cluster.** In cluster mode, spark-submit stores the application's settings in a ConfigMap that the driver mounts. A credential given at submit time would be readable by anyone who may read ConfigMaps in the namespace. So a cluster-mode application gets no credential at all:
  - `cairn-spark submit --master k8s://… --deploy-mode cluster job.py` submits only the driver's Cairn settings (`CAIRN_URL`, `CAIRN_CATALOG_URI`, `CAIRN_WORKLOAD_TOKEN_FILE`), plus a driver pod template.
  - The template mounts a projected service-account token for audience `cairn` at `/var/run/secrets/cairn/token`. The kubelet rotates it.
  - A job's own driver pod template (`spark.kubernetes.driver.podTemplateFile`, JSON or YAML) is kept: its volumes, sidecars and tolerations stay, and the token is added to it. A template that already uses the token's volume name or mount path is refused, not overwritten (`tests/test_spark_templates.py`; the kind job mounts its program from a config map this way).
  - The driver, a Python program calling `lakehouse_session()`, exchanges that token at Cairn (workload identity) and signs in as the pipeline user its service account is bound to.
  - Executors get no token. Their settings come from the driver over Spark's RPC, and their storage keys come with the tables.
  - The service account is `--conf spark.kubernetes.authenticate.driver.serviceAccountName=…` or `CAIRN_SPARK_SERVICE_ACCOUNT`.
  - **JVM programs** (`--class`) run in cluster mode too. Their catalog settings must exist before the program starts, so their driver pod's command is `cairn-spark-driver`. It signs in with the pod's projected token, opens the lakehouse session, merges its settings into a private copy of the driver's properties file (mode 0600, inside the pod) and then starts Spark's own driver. Nothing secret reaches the cluster's API. Client mode works as before.
- **The Spark Operator.** `cairn_spark.kubernetes.spark_application(name, image=…, main="/jobs/orders.py", service_account="svc-spark", cairn_url=…, catalog_uri=…)` builds the same application as a `SparkApplication` (`sparkoperator.k8s.io/v1beta2`, for `kubectl apply`):
  - a Python driver with the projected token and the driver environment;
  - executors with no identity.

  With `main_class`, it builds a JVM application whose driver template runs `cairn-spark-driver`. It refuses any `spark_conf` key that looks like a secret.
- **From Airflow.** `cairn_airflow.spark.CairnSparkApplicationOperator(task_id=…, name=…, namespace=…, service_account=…, image=…, main=…, cairn_url=…, catalog_uri=…)`:
  - creates one `SparkApplication` per task try, labelled with its DAG and task;
  - follows it to `COMPLETED`;
  - fails the task with the application's error message on `FAILED` or `SUBMISSION_FAILED`;
  - deletes the application when the task is killed.

  The worker's service account needs `create`, `get`, `watch` and `delete` on `sparkapplications`, and nothing on Secrets. The `cairn/airflow` image ships `cairn-spark` for it.
- **Helm.** `cairn.spark.enabled: true` (needs `cairn.workloadIdentity.issuers`) creates:
  - the drivers' service account (`driverServiceAccount`, default `svc-spark`; bind it to a pipeline user in Cairn);
  - a Role letting it start and stop its executors (pods, services, ConfigMaps, volume claims; nothing on Secrets);
  - with `submitters: [{name: airflow-worker, namespace: airflow}]`, a Role letting those accounts run `SparkApplication`s.
- **Teams.** `cairn.spark.teams` gives each team its own namespace (`spark-<team>` by default):
  - its drivers' service account (`svc-spark-<team>`; bind it to the team's pipeline user in Cairn), with a Role to manage its executors there and nowhere else, and nothing on Secrets;
  - a ResourceQuota on what all its Spark pods use together (`quota: {cpu, memory, pods}`);
  - a LimitRange: default requests for pods that state none (so the quota counts them), and a cap per container (`maxCpu`, `maxMemory`);
  - with `submitters`, a Role letting those accounts (e.g. Airflow's workers) run `SparkApplication`s there.

  Run a team's jobs with `--conf spark.kubernetes.namespace=spark-<team>` and its service account, or `namespace` and `service_account` on `CairnSparkApplicationOperator`.
- **History server.** Set `CAIRN_SPARK_EVENT_LOG_DIR`, and `lakehouse_session`, `cairn-spark` and the Kubernetes helpers turn on Spark's event logs there. The session credential, Cairn's token and store keys are redacted in them (`*********(redacted)`), as in the Spark UI. Each application records its Cairn user (`spark.cairn.user`).
  - **Signed in through Cairn.** Spark's history server has no sign-in of its own, so it is never exposed. Cairn serves it at `/v1/spark/history/` (`CAIRN_SPARK_HISTORY_URL`: where Cairn reaches it). Each person sees the applications they ran, and nothing about others' (404). People with `MANAGE_SYSTEM_OPERATIONS` see every application. Only reads are passed on. Run the history server with `-Dspark.ui.proxyBase=<Cairn's public API path>/v1/spark/history`, so its pages link back through Cairn.
  - **Event logs in Cairn Store.** Set `CAIRN_SPARK_EVENT_LOG_DIR=s3a://spark-events/logs`, `CAIRN_STORE_URL` (the store's S3 endpoint) and `CAIRN_SPARK_EVENT_LOG_ROLE`. The job trades its lakehouse token for the role's temporary credentials at the store's STS (`AssumeRoleWithWebIdentity`), for `CAIRN_SPARK_EVENT_LOG_SECONDS` (default 12 hours, or the role's maximum). So no store key is in any job. Set up the role once, with the store's IAM API:
    - its trust: `{"Effect": "Allow", "Principal": {"Federated": "<Cairn's token issuer>"}, "Action": "sts:AssumeRoleWithWebIdentity"}`. The store must trust that issuer, for audience `lakehouse` (`CAIRN_STORE_STS_PROVIDERS`).
    - its policy: `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on `arn:aws:s3:::spark-events/logs` and `…/logs/*` (S3A looks at the directory's own key too), and `s3:ListBucket` on the bucket. `MaxSessionDuration`: 43200.
    - The history server reads the prefix with a read-only key (`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in its environment; `cairn_spark.eventlog.history_server_opts` gives its settings).
    - **Who can read what.** Spark users can write, and read back, the shared event-log prefix: event logs hold query plans, never secrets. To keep teams' logs apart, give each team its own bucket, role and history server. Store policies also take `${cairn:user}` and `${aws:username}` in resources, for prefixes per person where a tool needs no shared directory.
  - **Compose:** `cairn-spark-history` (profile `spark`), on the `spark-events` volume, through Cairn at `/cairn/api/v1/spark/history/`. Its port is on the host's loopback only (`127.0.0.1:18080`), for operators.
  - **Helm:** `cairn.spark.historyServer.enabled: true`: a ClusterIP service, served through Cairn. The logs are on a ReadWriteMany claim (`cairn.spark.eventLog`, mounted by drivers at `/opt/spark-events`), or in Cairn Store (`cairn.spark.eventLog.store: {enabled, dir, readerSecret}`).
  - Logs older than `maxAge` (default 30 days; `CAIRN_SPARK_HISTORY_MAX_AGE` in Compose) are removed.
- **Streaming.** Structured Streaming into Iceberg works the same way (`writeStream.format("iceberg").toTable("lakehouse.…")`). A stream runs past its token lifetimes because its session renews them, and it stops when the session is ended.
- **pyiceberg and DuckDB.** The package `cairn-lakehouse` (`lakehouse-client/`) gives Python engines the same governed access:
  - `cairn_lakehouse.catalog()` opens a lakehouse session (the same variables as Spark) and returns a pyiceberg catalog on Cairn Catalog, with per-table storage keys and renewal through the session credential.
  - `duckdb_connection(["analytics.products"])` registers each table as a DuckDB view, read through pyiceberg, so DuckDB needs no extension and no store key. Install it with `pip install "cairn-lakehouse[duckdb]"`.
  - Tables with masks or row filters are refused, as for Spark.
- **What Spark costs.** Set `CAIRN_COST_SPARK_USD_PER_CORE_HOUR` and Cairn prices each Spark application from its OpenLineage events (see **Cost per asset**).
- **Not supported, on purpose.**
  - **Shared multi-tenant Spark Connect servers.** A Spark Connect server holds one catalog session for everyone it serves. Cairn's per-table decisions and storage keys would be those of the server's identity, not each user's. Run one Spark application per identity (the pattern above), or query shared data through Trino, which applies each user's policies itself.
  - **dbt Python models.** On dbt-trino they don't exist: Trino runs SQL only. Running them on Spark through dbt would put a session credential in dbt's profile on every developer's machine and CI runner. Use SQL models on Trino, and Spark jobs (above) for Python transformations, scheduled next to dbt in Airflow.
- **Tested** (`tests/test_spark_e2e.py`, in CI against the `cairn/spark` image as built) with Spark 4.0.1, Iceberg 1.10.0 and OpenLineage 1.55.0, on Cairn Catalog with the store's STS:
  - **Reads.** `ana` listed only what she may see. A table with her masked column was refused, and so was one she may not read.
  - **Writes.** She read, built a table (CTAS) and inserted into it in a schema she manages, and was refused an insert into one she may only read.
  - **Lineage.** It reached Cairn on the lakehouse dataset names, column by column.
  - **Row filters.** `bob`'s row filter kept the files away from him too.
  - **The example job.** It ran as `ana` and was refused as `bob`.
  - **Tokens.** A forged signature, a malformed token and Trino's token were refused (401). A service token got no lakehouse token, and only a service token could ask for decisions.
  - **Long jobs.** A job running seven minutes on one-minute tokens kept reading and committing. Its storage keys were reported to expire after six minutes, so they had to be renewed. Every token renewal was in its one session, and its keys were renewed through the catalog, decided again: a reader's renewal stayed read-only, and someone the table is not for was refused.
  - **Ending a session.** After `ana`'s sessions were ended mid-job, her commits failed once her last token expired. She could not end sessions herself, and could open a new one.
  - **Renewals.** Token exchange and the session credential (form or HTTP Basic) worked; another service's token, a token with no session, a forged token and a wrong secret were refused. So were a suspended user and a session past its maximum length, and when Cairn could not check the user it answered 503. With Cairn unreachable the catalog answered 503. It would not start with Cairn-issued tokens but no scoped credentials.
  - **JVM and SQL.** `cairn-spark sql` (a JVM program, no Python session) counted a table as `ana`, was refused a masked table, and refused `bob` the table. The session credential was never printed.
  - **Streaming.** A rate-source stream into Iceberg kept committing micro-batches for 150 seconds on one-minute tokens.
  - **History server.** A job wrote its event log to Cairn Store with the event-log role's temporary credentials. The role could not list another bucket. The log, and the history server reading it from the store, showed the run with its session credential, lineage key and store keys redacted, and no Cairn token. Through Cairn, `ana` saw her run, `bob` saw nothing of it (404 on its pages), and an operator saw everything.
  - **pyiceberg and DuckDB** (`tests/test_lakehouse_client_e2e.py`). Both were governed like Spark: listing, a refused masked table, writes only where allowed.
- **Tested on Kubernetes** (`tests/test_spark_k8s_e2e.py`, against a real Kubernetes 1.31 API server and the `cairn/spark` image; no kubelet, so what is checked is what Spark and the operator write to the cluster):
  - **Cluster mode.** `cairn-spark submit --deploy-mode cluster`, run by a submitter with a Cairn token in its environment, created a driver pod. The pod had the `svc-spark` service account, the projected `cairn` token and the driver environment.
  - **No secret stored.** Nothing secret was in the namespace's pods, ConfigMaps, Secrets or services, including the driver's `spark.properties`. A JVM program (SparkPi) submitted in cluster mode got a driver pod running `cairn-spark-driver`, again with no secret stored.
  - **A JVM driver signs itself in.** `cairn-spark-driver`, run with that pod's environment, a cluster-issued token and Spark's own driver arguments, ran `spark-sql` (a JVM program) as `ana` and counted the governed table.
  - **The driver signs itself in.** Run with that pod's environment and a token the cluster issued for `svc-spark`, it signed in as `ana` through workload identity. It read and wrote governed tables, and was refused one `ana` may not read.
  - **The run was priced.** It was charged at 2 cores for its wall-clock time, all to the table it wrote.
  - **The Spark Operator.** Its own `sparkapplications` CRD (v2.2.1) accepted the Python and JVM `SparkApplication`s that `cairn_spark` builds (server-side, strict validation). A jar without its main class, and a secret-looking setting, were refused.
  - **Teams** (`tests/test_spark_teams_k8s.py`, with the API server's quota controller). The chart's team objects were applied. A pod stating nothing got the team's default requests. A pod over the per-container cap was refused, and so were pods past the team's CPU and pod quotas. The drivers' account could manage pods and ConfigMaps in its namespace, not in another, and could not read Secrets.
  - **Airflow.** `CairnSparkApplicationOperator`, in the `cairn/airflow` image, created an application and followed it to `COMPLETED`, and failed on `FAILED` with the driver's error message. Executors had no identity.

## Table maintenance

Cairn keeps lakehouse tables healthy with Iceberg's own procedures, run through Trino on a schedule:
- **Compaction** (`optimize`): rewrites files smaller than `optimize_threshold` (default `128MB`) into bigger ones.
- **Snapshot expiry** (`expire_snapshots`): snapshots older than `expire_days`, so the data files only they referenced can go.
- **Orphan cleanup** (`remove_orphan_files`): files no snapshot references, older than `orphan_days`.

- **Policies** (`PUT /maintenance/policies/{scope}`, `MANAGE_SYSTEM_OPERATIONS`). `scope` is `*`, a schema or `schema.table`. The most specific one applies. Each has `optimize`, `optimize_threshold`, `expire_days`, `orphan_days` (null: skip), `every_hours` and `enabled`. Changes are audited (`maintenance.policy_set`).
- **Who runs it.** It runs on a run-as-user Trino connection (`CAIRN_MAINTENANCE_CONNECTION`, a named connection), as the Cairn user `CAIRN_MAINTENANCE_USER` (default `svc-maintenance`). Cairn's policy decides for it as for anyone, so give that user manage on the tables it maintains.
- **When.** The scheduler's leader (`cairn scheduler`) maintains each table whose policy is due. `POST /maintenance/run {"schema", "table"}` runs one now.
- **What happened.** Each operation is recorded with its outcome (`GET /maintenance/runs?table=`). One failing (say, refused) does not stop the next.
- **Trino's floors.** Trino refuses retention under `iceberg.expire-snapshots.min-retention` and `iceberg.remove-orphan-files.min-retention` (7 days by default). Keep them unless you mean to allow less.
- **Tested** (`tests/test_table_maintenance.py`; against Trino and Cairn Catalog in `tests/test_trino_e2e.py`):
  - **Real tables.** Five small inserts became one file. Snapshots were expired to one. A stray file in the table's location was deleted. The rows were unchanged, every step was recorded, and the table was not maintained again before it was due.
  - **Logic.** The table-level policy beat the schema's, and the schema's beat `*`. A refused step was recorded and the others ran. Bad scopes, names and values were refused, and only operators could set policies.

## Pipes (automatic file loading)

A pipe loads the files that land in a Cairn Store prefix into a lakehouse table, once each. It names a `bucket`, a `prefix`, a `format` (`csv`, `json` lines or `parquet`) and the Iceberg `target` (`namespace.table`). With `create_table`, the table is created from the first file.

- **Create** with `POST /v1/pipes` (`MANAGE_SYSTEM_OPERATIONS`). List, delete, and see each file's outcome (`GET /v1/pipes/{id}/files`). Changes are audited.
- **As files arrive.** Point a store event target (a webhook) at `POST /v1/pipes/events`, with the secret in `CAIRN_PIPES_WEBHOOK_SECRET`. Then add the bucket's `s3:ObjectCreated:*` rule for the prefix. Cairn takes only bodies the store signed with that secret (`X-Cairn-Signature`); anything else gets 401.
- **Sweep.** The scheduler's leader also lists each prefix, and `POST /v1/pipes/{id}/sweep` does it now. This loads anything no event announced.
- **Files as data (`format: files`).** The table is a directory of the prefix, kept as files come and go: one row per object. Query it in SQL, or point search and AI at it.
  - **Columns.** `key`, `path` (`s3://…`), `size`, `modified_ms`, `content_type`, `sha256`, plus the document's `text`, `pages` and `extract_error`.
  - **Text** is extracted from:
    - plain text, Markdown, CSV, JSON, XML, YAML and logs;
    - HTML (the visible text, without scripts or styles);
    - PDF (each page's text layer, with the `documents` extra; scanned pages without a text layer give nothing);
    - Word `.docx` and PowerPoint `.pptx`.

    Other files get a row without text. Text is capped at `CAIRN_PIPES_MAX_TEXT` characters, and files over `CAIRN_PIPES_MAX_DOCUMENT_BYTES` (50 MB) are listed without it.
  - **Changes.** A changed object replaces its row in one commit. A deleted object's row goes when the bucket's `s3:ObjectRemoved:*` event arrives, or at the next sweep.
  - **Tested** in `tests/test_pipes_e2e.py`, with real stores and events: text, HTML, PDF, Word and binary files, a change and a deletion by event, and a deletion found by the sweep.
- **Once each.** A file (key, size, modification time) is loaded once. It is recorded with its outcome. A file that fails, such as one with columns the table lacks, is recorded as an error; the next file still loads. A failed file is retried when it changes. Markers such as `_SUCCESS` are skipped.
- **Downstream.** Loaded tables are recorded as changed, so data-aware schedules (Airflow, dbt) see the new data.
- **Settings.** `CAIRN_PIPES_STORE` holds `{"endpoint", "access_key", "secret_key", "region"}`; give it a key that can only read the pipes' prefixes. `CAIRN_PIPES_CATALOG` holds the pyiceberg REST catalog properties: `uri`, `token`, and the warehouse's `s3.*` settings. Each is JSON, or a path to a JSON file.
- **Tested** with real Cairn Stores and Cairn Catalog (`tests/test_pipes_e2e.py`):
  - **Events.** Files loaded as the store's signed events arrived.
  - **Failures.** A file that did not fit was recorded as failed, and the next file still loaded.
  - **Once only.** A sweep afterwards loaded nothing again.
  - **Sweep.** Files under a prefix with no event rule were loaded by the sweep, and the markers were skipped.
  - **Signature.** A bad signature got 401.

## Vector search on lakehouse data

A vector index lets people search a lakehouse table's text by meaning: support articles, tickets, product descriptions, or the text of a files pipe.

- **Define** an index with `POST /v1/vectors` (`MANAGE_INGESTION`): `name`, `source` (`namespace.table`), `key` and `text` (the key and text columns), optional `chunk_chars` (1,500) and `every_minutes`.
- **Embedding.** Cairn splits each row's text into overlapping chunks and embeds them with the deployment's embedding model, the same one search by meaning uses (`CAIRN_EMBEDDINGS`; Ollama in your VPC, or the built-in hashing embedder). The vectors are kept in a lakehouse table of their own, `<source>__<index>_vectors` (key, chunk, text, hash, embedding, model).
- **Kept in step.** Run `POST /v1/vectors/<name>/refresh`, or let the scheduler run it every `every_minutes`. Changed rows are embedded again, new ones added and removed ones dropped. Unchanged rows are not re-embedded; a model change re-embeds everything. Each refresh is one commit.
- **Search.** `GET /v1/vectors/<name>/search?q=...&k=10` returns the closest chunks with their row key, text and cosine score.
  - The caller needs read access to the source table.
  - It is refused when a mask or row filter applies to them there, because the index holds the raw text.
  - Each API replica keeps one index in memory per table version.
- **Change or remove** an index with `PUT /v1/vectors/<name>` (key, text, chunking, schedule, enabled; a changed key, text or chunking is embedded again on the next refresh) and `DELETE /v1/vectors/<name>`, which also drops its vectors table, since it holds the raw text. An index keeps its source: to index another table, create another index.
- **Settings.** `CAIRN_VECTORS_CATALOG` (default: the pipes' catalog, `CAIRN_PIPES_CATALOG`).
- **Tested** in `tests/test_vectors_e2e.py`, with the real store and catalog. Questions found the right articles. A refresh re-embedded only the changed and new rows and dropped the removed one. Someone without read access, and someone with a row filter, were refused.

## Streams (Kafka and change data capture)

A stream writes a Kafka topic into a lakehouse table continuously. Define it with `POST /v1/streams` (`MANAGE_SYSTEM_OPERATIONS`): `name`, `topic`, `format`, `target` (`namespace.table`), and optionally `mode`, `key`, `create_table`, `evolve`, `start` (`earliest` or `latest`), `batch_rows` and `batch_seconds`.

- **Formats.**
  - `json`: one JSON object per message, one row. In `append` mode, rows are added. In `upsert` mode, a row replaces the row with the same `key` columns; a tombstone whose Kafka key is a JSON object of the key columns deletes that row.
  - `debezium`: Debezium change events, with or without the schema envelope.
  - **Avro** (`encoding: avro`): messages in the Confluent wire format (a magic byte, the schema's id, the Avro body), as Debezium's Avro converter and Confluent serializers write them. Schemas come from a Confluent-compatible Schema Registry (`CAIRN_STREAMS_SCHEMA_REGISTRY`: `{"url", "username", "password", "ca_file"}`; in Helm, `schema-registry.json` in the streams Secret) and are kept once read. Avro decimals and timestamps arrive as such. A message in another format, or naming a schema the registry does not know, is set aside; a registry that cannot be reached stops the batch, which is retried. Protobuf and JSON Schema messages are not read yet. Creates, updates and snapshot reads write the `after` image; deletes remove the row. The table follows the source database. Always `upsert`.
  - `http`: no Kafka. Producers post rows to the table over HTTPS (`POST /v1/ingest/<name>/rows` with `{"channel", "offset", "rows": [...]}`), up to 10,000 rows a request; `append` or `upsert`, as for `json`.
    - Each request is one commit, and its snapshot records the channel's next offset. Name one channel per producer and send the channel offset of the first row: a retried request then writes nothing twice, and an overlap writes only the new rows. A gap is refused (409).
    - `GET /v1/ingest/<name>/channels/<channel>` says where a producer should resume.
    - Without `offset`, rows are added at the channel's offset (at least once).
    - Posting needs `INGEST_STREAM` on `urn:li:stream:<name>` (or `MANAGE_INGESTION`), and the stream must be enabled. One writer per channel at a time.
    - Rows that do not fit are set aside like messages, with the channel in the reason. HTTP-only deployments set `CAIRN_STREAMS_ENABLED=true` without `CAIRN_STREAMS_KAFKA`.
    - Tested in `tests/test_ingest_http_e2e.py`, with the real store and catalog: retries, overlaps, gaps, separate channels, rejects, upserts and who may post.
- **Exactly once.** Each batch is one Iceberg commit, and its snapshot records the next Kafka offset of each partition. A worker taking over a partition starts from there. So if a worker dies between the table commit and the Kafka offset commit, nothing is written twice. Kafka's own committed offsets are the fallback once those snapshots have been expired.
- **Bad messages.** A message that cannot be a row is set aside with its offset and the reason (`GET /v1/streams/{id}/rejects`), and the stream goes on. Examples: not JSON, an unknown operation, no key, or a column the table lacks. With `evolve`, new columns are added to the table instead.
- **Failures.** A batch the table refuses, such as a value that does not convert, stops the stream. The error shows on its status and the batch is retried with back-off. Nothing is skipped silently.
- **Lineage.** Give `source` (the dataset URN the topic carries, such as the Postgres table) and Cairn adds it to the target's upstream lineage, merged with what is there. If that write fails, the stream is not created.
- **Status.** `GET /v1/streams` shows each stream's status, rows written, lag and last commit. `POST /v1/streams/{id}/enabled` pauses or resumes a stream. Changes are audited. Written tables are recorded as changed, for data-aware schedules.
- **Workers.** `cairn streams` runs every enabled stream. Several replicas share each stream's partitions, through one Kafka consumer group per stream.
  - **Settings.** `CAIRN_STREAMS_KAFKA` holds the kafka-python consumer settings: `bootstrap_servers`, `security_protocol`, `sasl_*`, `ssl_cafile`. `CAIRN_STREAMS_CATALOG` holds the Iceberg REST catalog properties (default: `CAIRN_PIPES_CATALOG`). Each is JSON, or a path to a JSON file.
  - **Helm.** Set `cairn.streams.enabled`, with `existingSecret` holding `kafka.json` and `catalog.json`.
  - **Compose.** Use the `streams` profile, with `CAIRN_STREAMS_ENABLED=true` on the API.
- **Tested** with a real Kafka broker, Cairn Store and Cairn Catalog (`tests/test_streams_e2e.py`):
  - **CDC.** Debezium snapshot reads, creates, updates and deletes kept the table equal to the source, through later changes too. A message that was not JSON was set aside, and tombstones were ignored.
  - **Exactly once.** A worker whose Kafka offsets were deleted resumed from the table's snapshots: 8 rows, not 13.
  - **Avro** (Redpanda's Kafka and Schema Registry). Debezium-shaped Avro events, decimals included, kept the table equal to the source. A JSON message and one naming an unknown schema were set aside. An Avro stream was refused when no registry was configured.
  - **Columns.** A column the table lacked was set aside, and with `evolve` it was added.
  - **Lineage.** The source was added to the target's lineage, merged with what was there. When Cairn Core failed, nothing was created.
  - **Checks.** Bad definitions were refused, and the API was for operators only.

## Change data feed

A lakehouse table's row changes between two of its snapshots, for consumers that sync, audit or react to what changed.

- **`GET /v1/lakehouse/tables/<schema>/<table>/changes?since=<snapshot id or ISO time>`**, optionally with `until` (default: the current snapshot), `key` (columns, comma-separated) and `limit` (1,000; at most 10,000).
  - Each row comes back with `_change_type`: `insert` or `delete`.
  - With `key`, a deletion and an insertion with the same key are an update: `update_preimage` and `update_postimage`.
  - `until_snapshot` is the snapshot to pass as the next `since`.
  - The changes are net between the two snapshots: a row added and removed in between does not appear.
- **`GET …/snapshots`** lists the table's snapshots: id, parent, commit time and operation.
- **Governed as a read.** Both snapshots are read as the caller, with ordinary time-travel queries on Trino, so their policies, masks and row filters apply. Someone masked sees masked values in the feed, and someone row-filtered sees only the changes to rows they may see at each snapshot. Trino's own `table_changes` is not offered, because its rows escape masks and row filters.
- **Cost.** The feed compares the two snapshots (`EXCEPT ALL` both ways): exact, but it reads both. Keep feeds frequent on large tables.
- **Settings.** `CAIRN_CHANGE_FEED_CONNECTION` is a run-as-user Trino connection (default: `CAIRN_DERIVED_CONNECTION`, then `CAIRN_MAINTENANCE_CONNECTION`). The catalog's name comes from `CAIRN_DERIVED_CATALOG`.
- **Tested** on real Trino (`tests/test_trino_e2e.py`). Each case, and what happened:

  | Case | Result |
  |---|---|
  | Inserts, a delete and an update since a snapshot | Came back as inserts, a delete and an update (pre- and post-image) by key, and as deletes and inserts without one |
  | The feed from its own last snapshot | Empty |
  | A masked reader | Saw only masked emails |
  | A row-filtered reader | Saw his EU rows leave and arrive |
  | A table the reader may not read | Refused |
  | `$files` | Refused to a reader |
  | `$snapshots` | Open to a reader |
  | `table_changes` on an append-only table | Ran for the admin, refused to a masked reader |

  The access decisions are also in `tests/test_trino_access.py`.

## Materialized views

A materialized view is a query's result kept as a lakehouse table, refreshed whenever a table it reads changes. Write it in the SQL editor (or `POST /sql/run`); Cairn runs these statements itself:

- **`CREATE MATERIALIZED VIEW [IF NOT EXISTS] [lakehouse.]<schema>.<name> [WITH (refresh_every_minutes = N)] AS <select>`.**
  - It is filled at once. If that first fill fails (for example, the query reads a table its author may not read), nothing is kept.
  - It refreshes when a table it reads changes, and also every `N` minutes when given.
- **`REFRESH MATERIALIZED VIEW <name>`** refreshes it now. **`DROP MATERIALIZED VIEW [IF EXISTS] <name>`** removes it and its table. Only its owner (or an operator) may do either.
- **What counts as a change.** Any commit to a table it reads, whoever writes it:
  - Cairn Catalog reports every commit to Cairn (`POST /v1/lakehouse/changes`, the catalog's service token only), so pyiceberg, Spark and Trino writes all count.
  - So do pipes, streams, dbt and lineage.

  The scheduler's leader refreshes due views in dependency order.
- **Fresh or stale.** `GET /v1/derived?kind=materialized_view` lists them, each with `fresh` (nothing it reads changed since its last good refresh) and `changed`, the tables that did.
- **Whose rows.** A view is a derived table of kind `materialized_view`: it refreshes as its owner, under their masks and row filters. Reading it is reading its table, under that table's policies, so give the table the access its contents call for.
- **Catalog settings.** Commits are reported when the catalog decides access with Cairn (`CAIRN_CATALOG_CAIRN_URL`, `CAIRN_CATALOG_CAIRN_TOKEN`). A report is retried for about a minute in the background; a commit never waits for it.
- **Tested:**
  - `tests/test_trino_e2e.py`, on real Trino: a view was created and filled. A change to its source made it stale and was refreshed on the next tick, and the next tick had nothing to do. `REFRESH` and `DROP` worked for its owner and were refused to another person. A view whose author may not read its source was not kept.
  - `tests/test_lakehouse_client_e2e.py`, with the real catalog: a pyiceberg commit reached Cairn's change log from Cairn Catalog, and only the catalog may report changes.

## Derived tables (incremental refresh)

A derived table is a SELECT kept as a lakehouse table and refreshed on Trino. Define it with `POST /v1/derived`: `name`, `sql` (one SELECT), `target` (`schema.table` in the lakehouse catalog), `mode`, and when it refreshes (`every_minutes`, `on_change`, or both).

- **Modes.**
  - `full`: `CREATE OR REPLACE TABLE … AS <sql>`, atomically.
  - `append`: adds the rows whose `watermark` column is above the table's current maximum.
  - `merge`: rows at or above the maximum `watermark` (all rows, without one) replace the rows with the same `key` columns; the others are inserted.

  The first run, and `POST /v1/derived/{id}/refresh {"full": true}`, builds the table from the whole query. Use that after changing the query's columns.
- **When.** The scheduler's leader refreshes each table every `every_minutes`. With `on_change`, it also refreshes when a table the query reads is recorded as changed: by pipes, streams, dbt, Spark, Trino writes or another derived table. Upstream derived tables refresh first, so chains finish in one tick. One refresh runs at a time per table (a lease in the database); another gets 409.
- **As its owner.** A refresh runs on Trino as the table's owner, so their access, masks and row filters apply, and Trino's lineage records it. Anyone may define one that runs as themselves. Only operators (`MANAGE_SYSTEM_OPERATIONS`) may define one that runs as someone else, and see everyone's. Others see only their own.
- **What happened.** Each run is recorded with its kind, trigger and outcome (`GET /v1/derived/{id}/runs`). Definitions are audited. Deleting a definition leaves the table itself.
- **Settings.** `CAIRN_DERIVED_CONNECTION` is a run-as-user Trino connection (default: `CAIRN_MAINTENANCE_CONNECTION`). `CAIRN_DERIVED_CATALOG` is the lakehouse catalog's name in Trino (default `lakehouse`).
- **Tested** on real Trino and Cairn Catalog (`tests/test_trino_e2e.py`; the logic in `tests/test_derived_tables.py`):
  - **Chain.** An append table and a full rebuild chained on it both refreshed after their source changed, in order and in one tick. The append added only the new rows, none twice.
  - **Merge.** A merge applied an update and inserted the new rows.
  - **Owner.** A table owned by a user without access to its source failed its run, recorded, and nothing was created.
  - **Checks.** Statements that are not one SELECT, a query reading its own target, and bad columns were refused. Owners saw and ran only their own. A running refresh blocked a second one.

## Notebooks

Notebooks (Jupyter, JupyterHub, VS Code) work on the lakehouse as the person signed in, with `cairn-lakehouse` (`lakehouse-client/`).

- **`%load_ext cairn_lakehouse.notebook`** adds:
  - `%%sql`: the cell runs on Trino as the notebook's Cairn user, with their masks, row filters and refusals. The result shows as a table, a pandas DataFrame when pandas is installed. `--to df` keeps it in a variable. `--limit N` caps the rows (default 10000), and says when the cap was hit.
  - `%cairn whoami`, and `%cairn spark`: a SparkSession on the lakehouse as the same user.

  Without magics: `from cairn_lakehouse.notebook import sql`.
- **Sign-in.** The notebook's Cairn token is `CAIRN_TOKEN`, `CAIRN_TOKEN_FILE` or `CAIRN_WORKLOAD_TOKEN_FILE`. For Trino it gets a short Trino token from Cairn, renewed before expiry, so nothing long-lived reaches Trino. In JupyterHub, pass each person's Cairn token to their server as `CAIRN_TOKEN` (for example from the authenticator's auth state).
- **Settings.** `CAIRN_URL`, `CAIRN_TRINO_URL` (https), `CAIRN_TRINO_CATALOG` (default `lakehouse`) and `CAIRN_CA_FILE`, which is used as given, whatever `REQUESTS_CA_BUNDLE` says.
- **Lineage and usage** come from Trino, as for any client.
- **Not yet.**
  - A JupyterHub deployment in the chart with Cairn sign-in.
  - Notebooks as catalog assets.
  - MLflow behind Cairn sign-in, with models in the catalog (roadmap L4 and L7).
- **Tested** with Trino (`tests/test_trino_e2e.py`, an IPython shell). `ana`'s cells saw her masked emails and were refused a hidden table. `bob`'s saw only his rows. The cap was reported, and a token Cairn does not know got no Trino token.

## Infrastructure as code

Access policies, connections (ingestion sources), pipes, streams, derived tables, maintenance policies, vector indexes, saved queries, dashboards, SQL alerts and SQL workflows can live in git, reviewed like code.

- **Bundles** (`cairn bundle`). Resources are declared in YAML (`examples/bundles/cairn.yaml`) under a `bundle:` name.
  - **Commands.** `cairn bundle validate FILE` checks the file alone. `plan` shows what would be created, updated and, with `--prune`, deleted; `--detailed-exitcode` exits 2 when there are changes. `apply` does it.
  - **Variables.** `${NAME}` comes from `--var NAME=value` or the environment, so one file serves several environments. A missing one is an error. `$${NAME}` stays a literal `${NAME}` (for a custom recipe's secret reference).
  - **Prune.** Cairn keeps each bundle's state: what it manages, versioned (`GET` and `PUT /v1/bundles/{name}`, `MANAGE_SYSTEM_OPERATIONS`). `--prune` deletes only what the bundle created, never resources made by hand or by other bundles.
  - **Adoption and concurrency.** An existing resource with a declared name is adopted. Two applies from one plan cannot both win: the second is told to plan again.
  - **Partial failures.** Changes go through Cairn's API as the token's user, so its permissions and audit apply. State is recorded after each change, so a failure halfway leaves an accurate record and the next apply finishes the job.
- **Saved queries, dashboards, alerts and workflows** are the token user's own (`queries`, `dashboards`, `alerts`, `workflows`). Dashboard tiles, alerts and workflow steps name their saved query (`query: revenue-by-region`); Cairn's ids are looked up at apply time, and a name that is not one of the user's saved queries is refused at plan. Others' shared queries and dashboards of the same name are never touched. Prune deletes dashboards, alerts and workflows before the queries they name.
- **Policies** (`policies`). Each is one of Cairn's own access policies, set and deleted on its own through `PUT` and `DELETE /governance/policies/{id}` (`MANAGE_POLICIES`); the rest of the bundle in force is left as it is. Each change is a new audited version of the policy bundle, and concurrent changes are serialized (an advisory lock on PostgreSQL), so two editors or replicas never lose each other's work. A policy is checked when the file is read, and compared exactly: removing a declared condition is a change. Policies owned elsewhere (personas, purposes, workspaces, native policies) are refused. Policies are created first and deleted last, so access goes last.
- **Connections** (`connections`). Ingestion sources: `name`, `connector`, its form `values`, `schedule`, `executor` (or `recipe` for a custom connector). Secret fields name a Cairn secret (`password: FINANCE_PG_PASSWORD`), so no secret is ever in git. Owners are kept when the bundle does not declare them (`MANAGE_INGESTION`).
- **Warehouses** are part of the deployment, not resources: declare them in Helm values (`cairn.trino.warehouses`), or with Terraform's `helm_release`. Who may use one is a policy granting `USE_WAREHOUSE` on `urn:li:warehouse:<name>`.
- **Drift.** A resource drifts only when something the bundle declares differs. What Cairn adds (ids, timestamps, alert states, a tile's default title) does not count.
- **A vector index keeps its source.** To index another table, delete the index and declare a new one; plan says so.
- **In place.** Changes keep each resource's id (`PUT /v1/pipes/{id}`, `/v1/streams/{id}`, `/v1/derived/{id}`), so a stream keeps its offsets. A derived table whose query changes is rebuilt from the whole query on its next refresh.
- **In CI.** On a pull request, run `cairn bundle plan cairn.yaml --var ENV=prod` with a read token. On merge, run `cairn bundle apply cairn.yaml --var ENV=prod --prune` with a pipeline user's token (`CAIRN_URL`, `CAIRN_TOKEN`).
- **Terraform and OpenTofu** (`terraform-provider-cairn`). The resources `cairn_policy`, `cairn_connection`, `cairn_pipe`, `cairn_stream`, `cairn_derived_table`, `cairn_maintenance_policy`, `cairn_vector_index`, `cairn_saved_query`, `cairn_dashboard`, `cairn_sql_alert` and `cairn_sql_workflow` work through the same API. A policy's id is `policy_id`; its `target`, `condition` and `obligations` are JSON. A connection's `values` and `schedule` are JSON. Changes are made in place. Something deleted outside Terraform is created again, and import uses the id (a maintenance policy's is its scope). See the provider's README.
  - **References.** Dashboards, alerts and workflow steps take saved query ids (`cairn_saved_query.x.id`). Dashboard `tiles`, workflow `steps` and a query's `chart` are JSON (`jsonencode(...)`), compared like a bundle's: what Cairn adds is not drift. A saved query's connection is `connection_name`, since Terraform keeps `connection` for itself.
  - **Data sources.** Each resource type is also a data source, read by its name (a maintenance policy by its scope), for what is managed by hand, by a bundle or by another workspace: `data "cairn_saved_query" "q" { name = "by-hand" }`. When others share one of the same name, the caller's own wins.
  - **Publishing.** The provider folder holds a GoReleaser config, the registry manifest and a release workflow for its own repository (`terraform-provider-cairn`, split from this one with `git subtree split`): a `vX.Y.Z` tag builds every platform and signs the checksums with GPG, which is what the Terraform and OpenTofu registries read. CI checks the config and runs an unsigned snapshot release on every push. See the provider's README, "Publishing".
- **Tested.**
  - **Bundles** (`tests/test_bundles.py`, against the real API). Plan, apply and re-plan with no changes. An update kept the ids. Prune left a hand-made pipe alone. An existing pipe was adopted. A stale plan was refused. A bad resource stopped the apply with Cairn's reason, after recording what was done.
  - **Terraform** (`tests/test_terraform_provider.py`, Terraform 1.13 and the provider built from source). Create, then no drift. An in-place change kept the ids. A resource deleted outside was recreated. Cairn's refusal surfaced as a Terraform error. Import, then destroy, which left a hand-made pipe.
  - **Policies and connections.** A bundle created a policy and a connection; the rest of the policy bundle, including a persona's policy, was untouched, and the connection's secret stayed a reference. A second plan had nothing to do. A change to the priority, a removed condition and a new database were updated in place, keeping the connection's owners. A persona's policy id and an invalid policy were refused. Prune deleted the connection, then the policy (`tests/test_bundles.py`). One-policy changes from 4 processes at once on PostgreSQL lost nothing, and the same test fails without the lock (`tests/test_policy_items.py`). Terraform created both, read the admin policy and the connection through data sources, showed no drift (also after an import), changed both in place, put back a policy deleted outside, and destroyed both (`tests/test_terraform_provider.py`).
  - **SQL resources and vector indexes.** A bundle (`tests/test_bundles.py`) created saved queries, a dashboard, an alert, a workflow and a vector index, with query names turned into ids. A second plan had nothing to do, even after someone else shared a query of the same name. Changes kept the ids. A new source for an index and an unknown query name were refused at plan. Prune deleted the dashboard, alert and workflow before their query, and left another person's query alone. Terraform did the same with ids, read a hand-made query through a data source, showed no drift on the JSON attributes, changed two resources in place and destroyed only what it made.

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

## Disaster recovery across regions

A standby region keeps a copy of the lakehouse, so losing the primary region costs minutes and the last few commits, not the lakehouse.

**What runs in the standby.**

- **Cairn Store**, receiving the primary's bucket by replication (`PutBucketReplication` to a target on the standby; see Replication above). Versioning is on at both ends.
- **PostgreSQL**, a streaming standby of the primary's database that holds the catalog's state (and Cairn's own). Use your PostgreSQL operator, or `pg_basebackup -R`.
- **Nothing else needs to run** until failover. Cairn Catalog, the Cairn API and Trino hold no state of their own beyond the database and the store.

**Why reconcile.** The two copies are made independently, so either can be ahead. Where the database is ahead, a table points at a metadata file, manifest or data file that never reached the standby store, and cannot be read there.

**Failover runbook.**

1. **Declare the primary lost**, and stop anything still writing to it.
2. **Promote the standby database**: `pg_ctl promote`, or your operator's switchover.
3. **Start Cairn Catalog** in the standby: `CAIRN_CATALOG_DATABASE_URL` pointing at the promoted database, `CAIRN_CATALOG_S3_ENDPOINT` at the standby store, the same warehouse (`s3://<bucket>/...`) and tokens.
4. **Reconcile**: `cairn dr reconcile --catalog <standby catalog> --token <catalog token> --store <standby store> --access-key ... --secret-key-file ...`.
   - It reports every table that cannot be read and what it would roll back to: its newest commit whose files all arrived.
   - Run it again with `--apply` to make the repairs. Each repair drops the table without purging its files, then registers it at that metadata file, so the files of the lost commits stay in the store for inspection.
   - Tables whose files are all there are not touched. A table with no whole commit left is reported as unrecoverable; restore it from a backup.
5. **Point clients at the standby**: Cairn's API and Trino's catalogs, through DNS or configuration.
6. **Afterwards**, rebuild the old primary as the new standby, the other way round.

**Measured** in the drill (`tests/test_dr_drill.py`, run in CI):

- **Setup.** Two regions, each with a real store and PostgreSQL. The primary's store replicated to the standby's, and its database streamed to the standby's.
- **The loss.** Ten commits were made. The standby's store was frozen for the last four, as over a slow link. Then the primary region was killed: store, catalog and database.
- **Recovery.** The standby database was promoted and a catalog started on it. The report found one table to roll back by four commits, and left the other alone. `--apply` repaired it.
- **Results.** RPO: 4 of 10 commits (4.3 s of writes). RTO: about 2 s from the loss to every table readable. The standby then took writes.
- **Your figures** depend on replication lag (watch `x-amz-replication-status` and PostgreSQL's replication lag) and on how fast your DNS and clients follow.

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

**The upgrade matrix** (the engines Cairn works with).

- **The pins.** `deploy/versions/pins.json` records the version of each engine Cairn is tested with: Trino, Spark, its Iceberg runtime, dbt-core, dbt-trino, Airflow and pyiceberg. A test (`tests/test_version_pins.py`) checks that every file that pins one says the same version.
- **Every week** (`.github/workflows/cairn-upgrade-matrix.yml`, Mondays, or on demand), `deploy/versions/latest.py` looks up each one's newest release on Docker Hub, PyPI and Maven Central, skipping pre-releases. For each that is newer than its pin, the matching end-to-end tests run against it: Trino's against the new Trino image, the catalog and lakehouse suites against the new pyiceberg, and the dbt, Airflow and Spark suites against images built with the new version.
  - A green row means the bump is safe. A red row is the work to do before bumping. A skipped test counts as a failure.
- **Bumping.** Change the pin in `pins.json` and in the files it lists, in one pull request. It merges only when the main CI passes on it.
- **Tried here.** The resolver found Trino 484, Spark 4.2.1 and Iceberg 1.12.0 newer than their pins. Cairn's Trino suite (21 tests) was then run against Trino 484:
  - First, Trino did not start: 484 refuses `s3.exclusive-create` (now `delta.s3.transaction-log-conditional-writes.enabled`) and renames `fs.native-s3.enabled` to `fs.s3.enabled`.
  - With those two settings renamed, every test passed. Policies, masks, row filters, SSO, lineage, AI functions, stored functions, procedures, Delta writes from two clusters and Delta conversion all worked unchanged.
  - The tests name each setting the way the image in use expects. The chart and Compose pin 476; bumping to 484 means renaming the two settings in `helm/cairn/templates/trino.yaml` in the same pull request.
