---
title: Governance
order: 3
---
# Governance

## Policy decisions

Every access decision in Cairn goes to the **policy decision point**, which combines role-based
rules (who you are) with attribute-based rules (what the data is: domain, tags, classification,
certification) using **deny-overrides**: a single matching deny wins. Decisions are cached briefly
and every decision is written to a tamper-evident audit log (`GET /cairn/api/governance/audit`).

Metadata changes take effect within seconds: when an owner, tag, term, domain, column label or group
membership changes, cached attributes and decisions for that asset or person are dropped at once, and
a changed catalog policy is re-imported immediately (`CAIRN_MCL_INVALIDATION`, on by default).

Two modes:

* **Additive** (default): Cairn's rules can grant access on top of the catalog's native policies.
  Good for a gradual rollout.
* **Authoritative**: Cairn is the only authority and its deny rules are enforced everywhere. Set
  `CAIRN_AUTHZ_MODE=authoritative` and `CAIRN_NATIVE_POLICIES_ENABLED=false`; the catalog's native
  policies are then imported into Cairn and evaluated alongside its own rules.

Masking applies to every read path: the UI, GraphQL and the REST API (`/openapi/v3` entities,
aspects and lists, and `/openapi/v2` timeseries).

## Personas and purposes

* A **persona** (for example *Finance Analysts*) bundles permissions for a group of people within a
  scope (domains, products, tags).
* A **purpose** (for example *GDPR personal data*) states why data may be used. Purpose denies take
  priority over persona grants.

Manage both in **Workspace → Governance studio**. Grants can be time-boxed; holders are warned before they
expire.

## PII and masking

Columns are classified (email, phone, national IDs and more) and classifications **propagate along
column lineage**, stopping at transformations that remove the sensitivity (hashing, aggregation,
counting, encryption, tokenisation, masking). Sample values and profiles of classified columns are
masked for people without a matching purpose; the asset page says when something is masked for you.

## Rule-based propagation

Tags and glossary terms can follow lineage by rule (for example: *gdpr-subject* flows to every
downstream table). Rules live in Workspace → Governance studio.

## Certification

Owners request certification; a steward who is not the requester approves it (separation of
duties). Certified assets show a badge with the approver and validity date. Approvals can be given
inline from Slack, Teams or email.

## Compliance forms

A compliance form asks the people responsible for a set of datasets for the metadata an audit
needs, and tracks who answered. Create one in **Governance studio → Compliance forms**:

* **Which datasets:** a classification, domain, platform or tag (all must hold). Through the API,
  any policy condition (`POST /cairn/api/governance/forms`).
* **Who answers:** the dataset's owners, optionally only some ownership types (data owners, say),
  with named people as a fallback when a dataset has no such owner.
* **Questions:** description, owners, glossary terms, tags, domain or a structured property, which
  are written to the catalog as the person answering; or free text and yes/no answers, which are
  kept in Cairn. Terms, tags and property values can be limited to a list of choices.
* **Sign-off:** a *Completion + sign-off* form also asks the assignee to confirm the answers.

**Assign…** previews how many datasets match and how many have nobody to answer, then creates one
task per dataset. Matching is re-checked every hour, so new datasets get tasks and datasets that no
longer match lose tasks nobody started. Assignees are notified (`form.assigned`) and find their
tasks under **Requests → Forms**, with current catalog values already filled in. Overdue tasks
trigger a daily reminder (`form.due`). People who can edit an asset may complete its task for the
assignee.

A dataset shows **Compliant** once every form assigned to it is complete (and signed off where the
form asks for it), and **Forms open** until then, on search results and asset pages.
**Progress** breaks each form down by domain and by assignee.

## Bulk editing

Search results have **Export CSV** and **Bulk edit…** buttons. The export has one row per dataset and one per column, with these columns: `urn`, `field`, `name`, `platform`, `description`, `owners`, `tags`, `glossary_terms`, `domain` and `classification`.

To make changes, edit the cells in any spreadsheet and load the file back:

- Separate several values with `;`. Usernames, tag names and term names are enough; you don't need full urns.
- Leave a cell empty to keep its current value, or put `-` to clear it.
- `name`, `platform` and `classification` are read-only. Owners and domain apply only to dataset rows.
- Values you type replace the current ones. For example, `gold;pii` in `tags` adds whichever of the two is missing and removes any other tag.

Before anything is written, the preview shows:

- every change, as additions and removals,
- the changes you aren't allowed to make (per asset and per column, using the same privileges as editing in the catalog), which are skipped,
- row problems: unknown urns, unknown columns, and assets you can't see.

**Apply** writes the whole batch at once:

- Your rights are checked again at that point.
- Changes someone has already made since the preview are skipped.
- Each write is tagged with the batch id.

**Undo** restores the previous values. It is refused if someone has edited the same aspects since the batch was applied. Only the person who imported a batch can apply or undo it.

API: `GET /cairn/api/bulk/export?query=…` (or `urns=…`), `POST /cairn/api/bulk/import` with the CSV as the body, `POST /cairn/api/bulk/{id}/apply`, `POST /cairn/api/bulk/{id}/undo`, and `GET /cairn/api/bulk` for your recent batches.

## Warehouse tags

Classifications protect data only when queries go through Cairn, unless the warehouse knows about them too. Tag sync copies catalog labels to warehouse tags, so Snowflake and Databricks tag-based masking and row policies cover direct queries as well.

A target is a reviewed YAML file in `config/tag-sync/` (or `CAIRN_TAG_SYNC`); see `snowflake.yaml.example`. It sets:

- **dialect:** `snowflake` or `databricks` (Unity Catalog).
- **connection:** a named warehouse connection. Use a role that may only apply tags.
- **platform:** the platform whose datasets are synced.
- **mappings:** from a tag or glossary term urn (a trailing `*` matches a prefix) to a warehouse tag and value. For each warehouse tag on a table or column, the first matching mapping wins. Two exact mappings that ask for different values are reported as a conflict.

Dataset-level labels become table tags, and column labels (ingested or edited in the catalog) become column tags. Cairn records every tag it sets, so:

- it unsets a tag only when Cairn applied it and the label has since gone from the catalog,
- tags people set in the warehouse are never touched,
- a statement that fails is retried on the next sweep.

Targets sync every 15 minutes (`CAIRN_TAG_SYNC_EVERY_SECONDS`). A target without a connection, or with `dry_run: true`, only plans.

In **Governance studio → Warehouse tags**, **Preview** shows each `SET` or `UNSET` with its exact statement, and **Sync now** runs them. The API is `GET /cairn/api/governance/tag-sync`, plus `POST /cairn/api/governance/tag-sync/{id}/sync` with `{"dry_run": false}` (the default is a dry run). Both need `MANAGE_GLOBAL_SETTINGS`.

The warehouse does the enforcing. For example, in Snowflake:

```sql
CREATE TAG IF NOT EXISTS GOVERNANCE.TAGS.PII ALLOWED_VALUES 'email', 'phone', 'other';
CREATE MASKING POLICY GOVERNANCE.TAGS.MASK_PII AS (v STRING) RETURNS STRING ->
  CASE WHEN IS_ROLE_IN_SESSION('PII_READER') THEN v ELSE '***' END;
ALTER TAG GOVERNANCE.TAGS.PII SET MASKING POLICY GOVERNANCE.TAGS.MASK_PII;
GRANT APPLY TAG ON ACCOUNT TO ROLE CAIRN_TAGGER;   -- the role behind the connection
```

Tags you set directly in the warehouse come back into the catalog through ingestion.

## Automations

An automation keeps a rule true across every matching dataset, now and as new datasets appear. For example: "every table in the finance schema gets the Finance domain and an owner".

Build one in **Governance studio → Automations**:

- **Which datasets:** name contains, platform, domain, classification or tag. **Advanced** takes any policy condition.
- **When:** on a schedule (every N minutes, at least 10), and/or when something happens to a dataset:
  - it appears, or its schema, tags, glossary terms, owners or description change;
  - personal data is detected on it;
  - a quality incident is raised, its contract breaks, or it is deprecated;
  - access to it is requested, or its certification is decided.
- **Then:** set domain, add owner, add tag, add glossary term, notify people, or **queue AI
  suggestions** (owners, classifications, a description). Suggestions wait in Governance → AI
  steward for review.

An event runs the automation on the one dataset the event is about, if that dataset matches. The
API's `when` field adds a condition over the event: `event.type`, `event.added`, `event.removed`,
`event.actor`, plus the event's own details (for example a quality incident's `severity`). Events
wait in a durable queue and are handled within seconds. Changes made by automations never trigger
automations, so rules cannot loop.

**Preview matches** lists the matching datasets and what a run would write. Nothing is written until you create the automation.

Each run writes only what is missing. It adds tags, terms and owners without removing the ones people set, and sets the domain only where it differs. Writes are attributed to the automation's own identity, `urn:li:corpuser:cairn-automation-<id>`, so history and the audit log show which rule made each change.

**Runs** shows what each run changed. **Undo** restores a run's changes, unless someone has edited the same assets since. Notify actions tell the listed people only about the assets a run actually changed. Pausing or deleting an automation keeps what it already changed.

API: `/cairn/api/governance/automations`, which needs `MANAGE_GLOBAL_SETTINGS`:

- `GET` and `POST` on the collection,
- `POST /preview`,
- `POST /{id}/run`, `POST /{id}/enabled`, `GET /{id}/runs`, `DELETE /{id}`,
- `POST /runs/{run_id}/undo`.

## Data preview and SQL

An asset's **Data** tab shows the table's first 100 rows, and a small SQL editor runs read-only queries over the same connection. Both use a named warehouse connection whose `catalog` block says which datasets it serves:

```yaml
# config/connections/warehouse.yml (re-read when it changes; *.example.yml files are skipped)
connections:
  warehouse:
    url: postgresql+psycopg://${WH_READER}:${WH_READER_PASSWORD}@warehouse:5432/analytics
    catalog: {platform: postgres, database: analytics}   # serves analytics.<schema>.<table>
```

Use a role that can only `SELECT`. Cairn also opens each Postgres session read-only and with a statement timeout.

**Rules:**

- **Authorization.** Every table a query reads must be in the catalog, and the caller needs `VIEW_DATASET_PROFILE` on it. That is the same privilege that guards sample values, so a policy that hides profiles from contractors hides rows too.
- **Masking.** Results are masked with the caller's masking plan, as profiles are. For a query, an output column is masked when it is computed from a masked column, so `upper(email) AS who` is masked as well. `SELECT *` is masked by column name. Aggregates that reveal no value, such as `count(*)`, are not.
- **What can run.** Only one `SELECT` (or `WITH … SELECT`) statement at a time, with schema-qualified table names. A query returns at most 1,000 rows and runs for at most 15 seconds.
- **Audit.** Every preview and query is recorded in the audit log under `data`, with the SQL (literals masked), the tables and the masked columns. Refused queries are recorded too.

API:

- `GET /cairn/api/data/sources` lists the connections that serve datasets,
- `GET /cairn/api/data/preview?urn=…` returns a table's first rows,
- `POST /cairn/api/data/query` with `{"sql", "urn" or "connection", "limit"}` runs a query.

## AI systems

**Governance → AI systems** is the inventory of the AI your organisation runs or buys. Each entry records:

- its purpose and owner,
- the models it runs and the datasets it is trained on or reads,
- the vendor, for bought-in systems,
- its risk tier and approval.

**The risk tier** comes from a risk assessment: the worst tier any answer points to. The default assessment (`config/ai-governance/risk-assessment.yaml`) follows the EU AI Act's categories:

| Tier | When |
|---|---|
| Unacceptable | a prohibited practice (Article 5), such as social scoring or untargeted face scraping. It can't be approved. |
| High | a use listed in Annex III (employment, credit scoring, education, essential services…), or a safety component of a regulated product. Needs a written account of human oversight. |
| Limited | transparency duties (Article 50): talks to people, or generates or alters content. |
| Minimal | none of the above. |

The default is a starting point, not legal advice. Edit its questions, options and tiers to match your framework, or replace the file.

**Approval** moves a system from draft to in review to approved, rejected or retired:

- Approving needs `MANAGE_POLICIES`, and the owner can't approve their own system.
- Changing a system's models, datasets, answers or purpose sends an approved system back to draft.
- Owners are notified of decisions, and every step is in the audit log.

**Restricted data needs an approved purpose.** A high-risk system that uses a dataset classified as restricted (PII, Confidential or Restricted by default; set `restricted_labels`) can only be approved if its purpose covers those classifications. Link a purpose from [Personas and purposes](#personas-and-purposes). The check runs again whenever the inventory is read: a dataset classified as restricted after approval shows up as a violation straight away.

**The inventory report** (`GET /cairn/api/governance/ai-systems/report`, or **Download inventory** for CSV) counts systems by tier and state. It also lists high-risk systems that aren't approved and every use of restricted data without a purpose.
