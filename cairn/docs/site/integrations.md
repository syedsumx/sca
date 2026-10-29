---
title: Integrations
order: 7
---
# Integrations

## Notifications

Cairn notifies people in the workspace inbox and, per their settings, in Slack, Microsoft Teams or
email. Routes per domain or event type are configured in **Workspace → Workspace settings → Notifications**;
generic webhooks receive the same events as JSON.

| Channel | Setup |
|---|---|
| Slack | install the Cairn Slack app; set `CAIRN_SLACK_BOT_TOKEN` and `CAIRN_SLACK_SIGNING_SECRET` |
| Microsoft Teams | add a Workflows webhook and set `CAIRN_TEAMS_WEBHOOK_URL` or a per-domain route |
| Email | set `CAIRN_SMTP_URL` |
| Webhook | add a route with a URL |
| Kafka | add a route with a topic; set `CAIRN_EVENTS_KAFKA_BOOTSTRAP` (and `CAIRN_EVENTS_KAFKA_SECURITY_PROTOCOL`, `…_SASL_MECHANISM`, `…_SASL_USERNAME`, `…_SASL_PASSWORD` if needed) |
| Amazon EventBridge | add a route with an event bus name or ARN; the notifier uses the AWS credential chain (`AWS_REGION`) |
| Google Cloud Pub/Sub | add a route with `projects/<project>/topics/<topic>`; the notifier uses its Google identity |

What the notifier will and will not call:

* **Slack** messages are checked against Slack's Block Kit limits before they are sent; long titles
  and bodies are shortened rather than rejected by Slack, and mentions in catalog text are escaped
  so a description cannot ping a channel. When Slack rate-limits Cairn for a few seconds the message
  waits; longer limits are retried on the next delivery round. For GovSlack or an egress proxy set
  `CAIRN_SLACK_API_URL`. Replies to button clicks go only to `hooks.slack.com`
  (`CAIRN_SLACK_RESPONSE_HOSTS` adds origins).
* **Teams** webhook URLs, including the personal ones people paste into their settings, must be
  https on a Microsoft webhook host (`.webhook.office.com`, `.logic.azure.com`,
  `.powerplatform.com`, `.powerautomate.com`). Anything else is refused when it is saved and again
  when it is called. `CAIRN_TEAMS_WEBHOOK_HOSTS` replaces the list (host suffixes, or exact origins
  such as a corporate proxy).
* **Webhooks** never reach loopback, link-local (including cloud metadata at 169.254.169.254) or
  unspecified addresses, and do not follow redirects. Private networks are allowed for on-premises
  receivers; `CAIRN_WEBHOOK_ALLOW_PRIVATE=false` limits webhooks to public addresses.

**Events out to your pipelines.** Kafka, EventBridge and Pub/Sub routes send every matching Cairn event (or only the `event_types` and `domains` the route names) as one JSON object:

```
{"id": "ntf_…", "type": "quality.incident.raised", "title": "…", "body": "…",
 "entity_urn": "urn:li:dataset:(…)", "url": "https://cairn/…", "actor": "urn:li:corpuser:…",
 "details": {…}, "time": "2026-10-30T02:00:00+00:00", "source": "cairn"}
```

- **Kafka** messages are keyed by the entity, so one asset's events stay in order, and carry a `cairn-event-type` header.
- **EventBridge** entries have `Source: cairn`, the event type as `DetailType`, and the entity as a resource. Match them with an event pattern such as `{"source": ["cairn"], "detail-type": ["quality.incident.raised"]}`.
- **Pub/Sub** messages carry `type` and `entity_urn` attributes, for subscription filters.

A delivery that fails is retried on the next rounds, up to five times. The API is `PUT /cairn/api/notify/routes` with routes like `{"id": "to-kafka", "channel": "kafka", "target": "cairn.events", "event_types": [], "domains": []}`.

## Actions in chat

Approvals (access requests, certifications) carry signed one-click buttons. In Slack, `/cairn`
searches the catalog as the linked user. People are matched to their Cairn account by email;
unmatched users are refused. In Teams and email the buttons are links signed for the recipient:
they work once, only for the person they were sent to, and ask for confirmation in the browser.
If someone already decided the request elsewhere, the button says so instead of acting twice.

## The assistant in Slack and Teams

**Slack:** turn on *Event Subscriptions* in the Slack app with the request URL
`https://<cairn-host>/cairn/api/integrations/slack/events`. Subscribe to the bot events
`app_mention` and `message.im`, and add the scopes `chat:write`, `im:history` and
`app_mentions:read`.

- A **direct message** to the app is answered in the DM with the person's own access. Follow-ups
  in the same thread continue the conversation.
- An **@mention in a channel** never posts a person's access to the channel:
  - With `CAIRN_SLACK_CHANNEL_ACTOR` set (a Cairn user whose access suits everyone in your
    channels), the answer is posted in the thread as that identity.
  - Otherwise only the person who asked sees the answer (ephemeral).

**Teams:** create an *outgoing webhook* in the team with the callback URL
`https://<cairn-host>/cairn/api/integrations/teams/messages`, and set `CAIRN_TEAMS_OUTGOING_SECRET`
to the security token Teams shows.

- Answers are visible to the whole channel, so they run as `CAIRN_TEAMS_CHANNEL_ACTOR`, which is
  required.
- Teams waits five seconds for a reply. A slower answer is posted to `CAIRN_TEAMS_REPLY_WEBHOOK`
  (an incoming webhook or Workflows URL for the channel) when set.
- With SCIM from Entra ID, the audit log records who asked.

Every question is audited (`assistant.chat_question`) with where it was asked and whose access
answered it.

## Embeds

Show trust information inside BI tools and portals:

* **Web components**: load `/cairn-ui/embed/sdk.js`, then use `<cairn-asset-card>`,
  `<cairn-quality-badge>` and `<cairn-lineage>`.
* **Embeddable pages** for tools that only take a URL:
  `/cairn/api/embed/v1/frame/{card,quality,lineage}?urn=…&token=…`.

Embed tokens are scoped to assets, bound to the origins in `CAIRN_EMBED_ALLOWED_ORIGINS` and can be
revoked. A page on any other origin gets neither: the browser refuses to frame the embeddable pages
(`Content-Security-Policy: frame-ancestors`) and refuses the web components' data requests (CORS). Packaged guides exist for Tableau, Looker and Power BI.

## Browser extension for BI tools

The **Cairn for BI** Chrome extension (Manifest V3, in `extension/`) shows the catalog card in a side panel for the dashboard you have open in Looker, Tableau, Power BI, Sigma, Superset or Preset, Metabase, or Qlik Cloud. The card shows the description, owners, certification, data quality and checks, and an **Open in Cairn** link.

- **Pages it recognises:**
  - Looker dashboards, Looks and Explores on `*.looker.com`,
  - Tableau views and workbooks on Tableau Cloud and `*.tableau.com`, including routes in `#/site/…`,
  - Power BI reports and dashboards on `app.powerbi.com`,
  - Sigma workbooks on `app.sigmacomputing.com`,
  - Superset and Preset dashboards and charts (Preset on `*.preset.io`, self-hosted Superset as a custom domain),
  - Metabase dashboards and questions on `*.metabaseapp.com` (the slug Metabase adds is ignored),
  - Qlik Cloud apps on `*.qlikcloud.com`.

  It follows navigation inside these single-page apps.
- **How it finds the asset:** it looks up the page through `GET /cairn/api/embed/v1/context?url=…`, using the person's own Cairn session. They see only what they may see, and a sign-in link when they aren't signed in. Query strings and in-page anchors are ignored, so filters and tiles don't stop a match.
- **What it reads:** only the page address, never the page content.
- **Custom domains:** a self-hosted Looker, Tableau Server, Superset or Metabase, for example, are added in the extension's options. The browser asks for access to each host you add, and to nothing else.

**Rolling it out.** `node extension/build.mjs --cairn-url https://cairn.example.com` builds `extension/dist/` with your Cairn address and its host permission built in, ready to load unpacked, publish privately, or force-install through Chrome enterprise policy. Without `--cairn-url`, people enter the address in the options. `npm test` in `extension/` runs its unit tests.

## Ticketing and incident tools

Where approvals and on-call already live in Jira, ServiceNow, PagerDuty or Opsgenie, Cairn opens tickets there and listens for the decision. Approvals don't need a second queue.

**Set up:**

1. Describe each system in a YAML file in `config/ticketing/`, or point `CAIRN_TICKETING` at one; see `ticketing.yaml.example`. Secrets are `${ENV}` references.
2. Add notification routes with `channel: ticket` and `target: <system id>`, filtered by event type and domain like any route. For example, `access.requested` and `certification.requested` go to `jira-it`, and `quality.incident.raised` and `quality.incident.resolved` go to `pagerduty`.

**What each system does:**

- **Jira and ServiceNow.**
  - A request opens one issue (or `sc_request`), labelled `cairn`, with a link back and a `kind:id` reference. Later events about the same request are added as comments or work notes.
  - When the issue reaches one of the `approve_statuses` or `reject_statuses`, the Jira webhook, or a ServiceNow business rule posting `{sys_id, approval, approver_email}`, calls `POST /cairn/api/notify/inbound/<system id>`.
  - Cairn then decides the request as the person who decided in the ticket, matched by their email. Cairn's own approval rules still apply, so someone who couldn't approve in Cairn can't approve from a ticket. People who don't map to a Cairn user are refused.
- **PagerDuty and Opsgenie.**
  - A quality incident at or above `min_severity` triggers an incident, using Events API v2 or an Opsgenie alert, with a stable dedup key per monitor.
  - Recovery resolves it.

**Security.** Inbound calls must carry the system's `inbound_secret`, either as `?secret=` or the `X-Cairn-Secret` header, or sign the body with it (`X-Hub-Signature: sha256=…`, as Jira does). Every decision made from a ticket is audited (`ticket.decided`). Administrators see the configured systems and their open tickets at `GET /cairn/api/notify/ticketing`.

## OpenLineage

Airflow, Spark, dbt, Dagster, Flink and other tools that emit OpenLineage can push lineage and runs to Cairn as they happen, without polling.

Point the tool's OpenLineage HTTP transport at `https://<cairn>/cairn/api/openlineage` with a Cairn API key. Create the key under **Settings → AI agents** with the `write` scope; its person needs `EDIT_LINEAGE`. For Airflow:

```
OPENLINEAGE_URL=https://cairn.example.com/cairn/api/openlineage
OPENLINEAGE_API_KEY=ca_…
```

Each event becomes catalog metadata:

- **The job** becomes a pipeline and a task, with the datasets it reads and writes. Airflow's `dag.task` names put tasks under their DAG. Column-level lineage comes from the `columnLineage` facet, and the SQL from the `sql` facet. Inputs and outputs build up over a job's events, and lineage that crawlers found on the datasets is left alone.
- **The run** becomes a run of that task, with its start and result. `COMPLETE` is a success, `FAIL` a failure, and `ABORT` skipped. Run history and failure streaks show next to the job, and the context agents use them.
- **Datasets** the catalog doesn't know yet are created, with their schema from the `schema` facet. Catalogued datasets keep their crawled schema.

Dataset names follow OpenLineage's naming: the namespace's scheme picks the platform. For example, `postgres://host:5432` with `db.schema.table` becomes a Postgres dataset, `snowflake://account` a Snowflake one (lower-cased), and `s3://bucket` with `/path` the S3 dataset `bucket/path`.

Datasets are created in the `PROD` environment; set `CAIRN_OPENLINEAGE_ENV` to change it. The endpoint takes one event or a list of up to 500, and each delivery is in the audit log (`openlineage.received`).

## Git and dbt

Connect the GitHub, GitLab or Bitbucket repositories that hold your dbt projects. Cairn then comments on pull requests with what a change affects, and sends descriptions written in Cairn back to dbt.

**Set up.** Describe each repository in `config/git/repos.yaml`, or set `CAIRN_GIT` to another file or folder. Start from `repos.yaml.example`. Each repository needs:

- the provider (`github`, `gitlab` or `bitbucket`) and the repository path;
- a token, as `${ENV}`. It needs read and write on contents and pull requests, and permission to set commit statuses;
- a webhook secret;
- where `dbt_project.yml` is in the repository;
- the warehouse, database and schema the models build into, so Cairn can find the models in the catalog.

Then add a webhook on the repository: `https://<cairn>/cairn/api/integrations/git/<id>/webhook`, with the same secret, for pull request (GitHub, Bitbucket) or merge request (GitLab) events. **Settings → Sources** lists the repositories, and flags a missing token or secret.

**Impact on pull requests.** When a pull request is opened or updated, Cairn reads which dbt models it changes. That covers SQL files, and properties files whose columns change. Cairn posts one comment, updated on every push, showing per model:

- the downstream assets, certified first, and how many dashboards;
- the owners to tell;
- the model's data contract, if it has one;
- the columns the change removes.

A commit status, `cairn/impact`, fails when a removed column (or a removed model) is one a contract promises. Make it a required check to stop contract-breaking merges. To refresh the comment by hand, use `POST /cairn/api/integrations/git/<id>/impact` with `{"pull_request": "7"}`.

**Docs back to dbt.** On a dataset that a configured repository builds, **Send docs to dbt** opens a pull request. It writes the descriptions people edited in Cairn, the table's and its columns', into the model's properties file. Only those lines change, so comments and formatting elsewhere stay as they were. This needs edit rights on the dataset's docs, and the pull request is in the audit log.

## Metadata export

Cairn can write its catalog facts into your own storage every night, as tables you query alongside the data they describe. Use them to report on ownership coverage, to find uncertified tables in critical pipelines, or to join a quality status to your own controls.

| Table | One row per |
|---|---|
| `assets` | dataset, data product, dashboard, chart or ML model: platform, name, description, domain, certification, classification, owner count |
| `columns` | dataset column: type, description, nullable, labels, classification |
| `owners` | asset and owner, with the ownership type |
| `labels` | tag or glossary term on an asset or a column |
| `lineage` | upstream to downstream dataset edge |
| `monitor_results` | quality monitor, with its latest status and value |
| `access_grants` | marketplace grant: product, grantee, active or expired |

Every row carries `exported_at`. The table layout is versioned (`schema_version` in `_manifest.json`): new columns can appear, but a column is never renamed or removed within a version.

**Parquet** in object storage or on a path. Each run writes one partition per table and day, so register each folder as an external table with Hive partitioning on `export_date`:

```bash
cairn export metadata --to s3://acme-lake/cairn --keep-days 30
#   s3://acme-lake/cairn/assets/export_date=2026-11-02/assets.parquet, …, _manifest.json
```

```sql
-- DuckDB
SELECT name, certification, owner_count
FROM read_parquet('s3://acme-lake/cairn/assets/*/*.parquet', hive_partitioning = true)
WHERE export_date = (SELECT max(export_date) FROM read_parquet('s3://acme-lake/cairn/assets/*/*.parquet', hive_partitioning = true));

-- Snowflake, over an external stage on the same bucket (other columns are VALUE:<name>)
CREATE EXTERNAL TABLE cairn_assets (
    export_date DATE AS TO_DATE(SPLIT_PART(SPLIT_PART(METADATA$FILENAME, 'export_date=', 2), '/', 1)))
  PARTITION BY (export_date)
  LOCATION = @cairn_stage/assets/
  AUTO_REFRESH = TRUE
  FILE_FORMAT = (TYPE = PARQUET);
```

`gs://` and `abfs://` URIs work the same way. Credentials come from the usual environment variables (`AWS_*`, `GOOGLE_APPLICATION_CREDENTIALS`, `AZURE_STORAGE_*`) or the machine's identity. For S3-compatible storage, add `?endpoint_override=host:port&scheme=http` to the URI. If your S3-compatible server predates AWS's default request checksums, also set `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` and `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required`.

**Iceberg** in your catalog (AWS Glue, Unity, Polaris, Snowflake Open Catalog or any REST catalog). Each run is one snapshot, so time travel replaces partitions:

```bash
export PYICEBERG_CATALOG__LAKE__URI=https://catalog.example.com/api/catalog PYICEBERG_CATALOG__LAKE__TOKEN=…
cairn export metadata --format iceberg --catalog lake --namespace cairn
```

**Scheduling:** on Helm, set `cairn.metadataExport.enabled`, with `destination` (or `format: iceberg`), `schedule` (nightly by default) and `credentialsSecret`. On Compose, run the command from cron: `docker compose exec -T cairn-api cairn export metadata --to …`.

## Extensions

An extension adds to Cairn without changing it. It runs as your own service, and Cairn gives it three things:

- **an API token** that can do only what its manifest asks for,
- **events**, delivered as signed webhooks,
- **sections in the workspace**.

Everything an extension may do is declared in one file, `cairn-extension.yaml`:

```yaml
name: incident-log
version: 1.0.0
publisher: Acme Data Platform
description: Recent quality incidents with each asset's trust badges.
permissions: [VIEW_ENTITY_PAGE]                  # the token can do this and nothing else
scope: {attr: resource.domains, op: contains, value: urn:li:domain:finance}   # optional: only these assets
events:
  - url: https://incident-log.acme.internal/events
    types: [quality.incident.raised, contract.broken]                         # empty = every event
ui:
  - label: Incident log
    url: https://incident-log.acme.internal/ui
    placement: workspace                         # or asset: a tab on every asset page, called with ?urn=
```

**Install it** under **Settings → Extensions**, or with `cairn extension install <folder>`. `cairn extension validate <folder>` checks a manifest without installing it, for the extension's own CI.

A first install shows the API token (`cx_…`) and the event signing secret once. Upgrading with a new version keeps both. Rotate them with `cairn extension rotate <name>`. Disabling an extension stops its token, events and sections at once; removing it deletes them. Every change is in the audit log.

**What it can do:**

- **The token** acts as `urn:li:corpuser:cairn-ext-<name>`. It is allowed exactly the listed `permissions`, only on assets that match `scope` (an ABAC condition, like a policy's). It can never hold administration privileges, manage extensions, or take a licence seat.
- **Events** are the notification types listed under [Notifications](#notifications). Each delivery carries `Cairn-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<body>">`. Check it and refuse deliveries older than five minutes. The example has a ten-line `signature_ok` to copy.
- **Sections** load in a sandboxed frame: scripts and forms run, but the page can't read Cairn's cookies, storage or page. It talks to its own backend, which holds the token.

The example in `examples/extensions/incident-log` uses all three, with the Python standard library only.
