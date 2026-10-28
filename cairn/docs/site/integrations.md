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

## Actions in chat

Approvals (access requests, certifications) carry signed one-click buttons. In Slack, `/cairn`
searches the catalog as the linked user. People are matched to their Cairn account by email;
unmatched users are refused. In Teams and email the buttons are links signed for the recipient:
they work once, only for the person they were sent to, and ask for confirmation in the browser.
If someone already decided the request elsewhere, the button says so instead of acting twice.

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

The **Cairn for BI** Chrome extension (Manifest V3, in `extension/`) shows the catalog card in a side panel for the Looker, Tableau or Power BI dashboard you have open. The card shows the description, owners, certification, data quality and checks, and an **Open in Cairn** link.

- **Pages it recognises:**
  - Looker dashboards, Looks and Explores on `*.looker.com`,
  - Tableau views and workbooks on Tableau Cloud and `*.tableau.com`, including routes in `#/site/…`,
  - Power BI reports and dashboards on `app.powerbi.com`.

  It follows navigation inside these single-page apps.
- **How it finds the asset:** it looks up the page through `GET /cairn/api/embed/v1/context?url=…`, using the person's own Cairn session. They see only what they may see, and a sign-in link when they aren't signed in. Query strings and in-page anchors are ignored, so filters and tiles don't stop a match.
- **What it reads:** only the page address, never the page content.
- **Custom domains:** a self-hosted Looker or Tableau Server, for example, are added in the extension's options. The browser asks for access to each host you add, and to nothing else.

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

`gs://` and `abfs://` URIs work the same way. Credentials come from the usual environment variables (`AWS_*`, `GOOGLE_APPLICATION_CREDENTIALS`, `AZURE_STORAGE_*`) or the machine's identity. For S3-compatible storage, add `?endpoint_override=host:port&scheme=http` to the URI.

**Iceberg** in your catalog (AWS Glue, Unity, Polaris, Snowflake Open Catalog or any REST catalog). Each run is one snapshot, so time travel replaces partitions:

```bash
export PYICEBERG_CATALOG__LAKE__URI=https://catalog.example.com/api/catalog PYICEBERG_CATALOG__LAKE__TOKEN=…
cairn export metadata --format iceberg --catalog lake --namespace cairn
```

**Scheduling:** on Helm, set `cairn.metadataExport.enabled`, with `destination` (or `format: iceberg`), `schedule` (nightly by default) and `credentialsSecret`. On Compose, run the command from cron: `docker compose exec -T cairn-api cairn export metadata --to …`.
