---
title: Context and AI
order: 5
---
# Context and AI

## Context agents

Context agents read query history, dbt models, BI definitions and pipeline runs and turn them into
**findings** for stewards to review in **Workspace → Context inbox**:

| Finding | Meaning |
|---|---|
| `METRIC_DEFINITION` | a metric computed the same way in many places, with its definition |
| `DEFINITION_DRIFT` | the same metric name computed differently in two places |
| `TERM_SUGGESTION`, `TERM_LINK` | a missing glossary term, or a term that belongs on an asset |
| `JOIN_RELATIONSHIP` | tables that are routinely joined, and on which keys |
| `DOC_STALENESS` | documentation that no longer matches the data |
| `RUN_ANOMALY` | a pipeline run that behaved unusually |

Accepting a finding writes it to the catalog (glossary terms, relationships, discussions); dismissed
findings do not come back.

**Column lineage from SQL.** On each cycle the agents also read the SQL that writes data: `INSERT … SELECT` and
`CREATE TABLE … AS SELECT` in the query log, and view definitions. Each written column is traced through CTEs,
subqueries, joins, `SELECT *` and expressions to the physical columns it reads, using the catalogued schemas.
The result goes straight into the target's lineage, column by column. A column copied as is is a `COPY`; anything
computed is `SQL`. The statement that produced an edge is kept on it. Cairn only adds what is missing: edges
from crawlers, OpenLineage or people are never removed. SQL that can't be parsed, or tables that aren't in the
catalog, are skipped.

## Assistant

Ask questions in plain English from the search bar or the assistant panel. The assistant only uses
metadata you are allowed to see, redacts personal data before anything reaches a model, cites the
assets it used, and can explain lineage anomalies. It runs on Claude or on a local model for
air-gapped installs; without a model it falls back to search-based answers.

## Ask your data

On a table's **Data** tab, type a question such as *net revenue by day this month* and press
**Ask**. Cairn writes one read-only SQL query and runs it for you. It shows the query, a one-line
explanation, the approved definitions it used, and the rows. **Edit in the SQL editor** copies the
query so you can adjust it.

- **Grounded:** the model sees the table's columns, the approved metric and join definitions for it,
  and the queries that answered similar questions before. It uses those definitions exactly.
- **Governed:** the query runs as you. Your table permissions, purposes and column masking apply, as
  for SQL you write yourself. Every question is in the audit log (`data.asked`).
- **Self-correcting:** if the warehouse rejects the query, the model gets the error and one more try.
  A permission refusal is final.

The assistant and MCP clients have the same ability through the `ask_data` tool. Without an AI
provider the box says so, and the SQL editor still works. Over the API: `POST /data/ask` with
`{"question": …, "urns": [...]}`. Leave `urns` empty and Cairn picks the best-matching datasets
you may see; send `"run": false` to get the SQL without running it.

## Search by meaning

Search results add a **Related by meaning** section: assets whose descriptions, columns, tags and
terms are close to what you typed even when the words differ ("money each region made per day"
finds `revenue_daily`). Each result shows the words it matched. Asset pages list **Similar assets**,
which helps find the certified copy of a table or the other half of a join. Both only ever show
assets you can see.

The index fills itself from the catalog when Cairn starts, follows changes as they happen, and is
reconciled with the catalog every 6 hours (`CAIRN_SEMANTIC_RECONCILE_SECONDS`), which also drops
deleted assets. Embeddings are computed inside your deployment: by the built-in embedder (no model,
no network), or by a local model through Ollama (`CAIRN_EMBEDDING_PROVIDER=ollama`,
`CAIRN_EMBEDDING_MODEL`, `CAIRN_EMBEDDING_DIM`). Changing the embedder re-indexes everything in the
background. Operators can check the index with `GET /cairn/api/ai/index/status` and force a pass
with `POST /cairn/api/ai/index/rebuild`.

## Documentation drafts

The assistant can draft descriptions for assets and columns. Drafts are never published without a
reviewer's approval.

## Usage, cost and budgets

Every model call is recorded with who asked, which feature (`nlq.plan`, `nlq.answer`,
`docgen.draft`, `lineage.explain`, `assistant.chat`), the model, tokens and an estimated cost, and
is written to the audit log under category `ai`.

```
GET /cairn/api/ai/usage?group_by=task|actor|model|provider|day&days=30
GET /cairn/api/ai/usage?mine=true          # anyone: their own usage
```

Seeing everyone's usage needs `MANAGE_SYSTEM_OPERATIONS`. Costs use list prices, including
prompt-cache reads and writes; set `CAIRN_AI_PRICES` (`{"model-prefix": [input, output]}` in USD per
million tokens) for negotiated rates. Self-hosted models cost nothing.

| Setting | Effect when reached |
|---|---|
| `CAIRN_AI_DAILY_BUDGET_USD` | All AI features fall back to search-based answers until the next UTC day |
| `CAIRN_AI_USER_DAILY_TOKENS` | The same, for that one user |

Documentation drafts return `429` instead of falling back, since there is no non-AI draft.

## Evaluating changes

`cairn ai eval` runs golden cases (`config/ai-evals/*.yaml`) through the search, documentation and
lineage-explanation features. Offline, a scripted model checks the guardrails deterministically:
personal data is redacted before the prompt, catalog text stays inside its untrusted-data wrapper,
assets the asker cannot see never reach the prompt or the answer, invented citations and columns are
dropped. `--live` runs the same cases against the configured model and reports tokens, cost and
latency per case, so a model or prompt change can be compared before rollout:

```
docker compose exec cairn-api cairn ai eval                  # offline, in CI
docker compose exec cairn-api cairn ai eval --live --json /tmp/eval.json
```

Add a case for every incident the assistant gets wrong; the command exits non-zero on any failure.

## MCP for AI tools

Cairn exposes the catalog to AI coding tools over the Model Context Protocol at `/cairn/mcp/mcp`.
Each request carries the user's own access token, so tools see exactly what the user can see.

```bash
claude mcp add --transport http cairn https://<cairn-host>/cairn/mcp/mcp
```

The first time, the tool opens Cairn in your browser. You sign in and choose what it may do (see
**AI agents** in Administration), and it connects with a short-lived token that acts as you. Tools
without OAuth support can still send a personal access token:
`--header "Authorization: Bearer <token>"`.

What a client gets:

- **Tools**, each with a title and structured output. They search, read assets, lineage and impact,
  check quality and access, find definitions and past queries, answer questions with data
  (`ask_data`), and propose documentation for review.
- **Resources** a client can attach as context:
  - `cairn://asset/{urn}`, `cairn://lineage/{urn}` and `cairn://quality/{urn}`, with the urn
    percent-encoded;
  - `cairn://definitions`, the approved metric and join definitions.
- **Prompts** for common jobs, each chaining the right tools: *Document a dataset*, *Review the
  impact of a change*, *Investigate a data quality issue*, *Answer a question with data*.
- **Completion**: typing a few letters of an asset name in a prompt or resource argument suggests
  matching assets you may see.

**Agents propose, people decide.**

- **Suggestion tools** (`suggest_description`, `suggest_tags`, `suggest_glossary_terms` and
  `suggest_owner`) put the agent's suggestion in the review queue (**Governance → AI steward**).
  Each suggestion is labelled with the agent and the person it acted for. Nothing changes until
  someone with the right permission accepts it. Accepted suggestions are written as one batch you
  can undo.
- **`request_access`** files an access request that the product's approvers decide.
- **`start_discussion`** opens a thread marked as posted by an agent.

These tools need the agent's `propose` scope.

**Screening.** Catalog text is written by people, so it may try to instruct an AI:

- Before any catalog text reaches a model or an MCP client, Cairn removes invisible characters
  (hidden Unicode tags, zero-width and direction controls).
- Text that reads like instructions to an AI is marked *flagged by Cairn* in place, and the event is
  recorded in the audit log (`ai.injection_flagged`).
- Suggestions and messages that carry such text are refused (`agent.suggestion_refused`), so an
  agent cannot be steered into planting it in the catalog.
- Each agent may make up to 200 suggestions an hour.

## AI steward

Coverage is a catalog's weak point: most tables have no description and no owner, and nobody has time to write ten thousand of them. The AI steward proposes them for people to review.

A run, started from **Governance studio → AI steward → Find gaps now** or `POST /cairn/api/ai/steward/run`, walks the datasets that lack a description, an owner or a classification, most viewed first. Each proposal carries its evidence and a confidence:

- **Owners.** The people who ran most of the recent queries, who last edited the dataset's metadata, and who own its domain and its upstream tables. Service accounts and Cairn's own automation identities never count, and their queries don't dilute anyone's share. With no clear evidence, nobody is proposed.
- **Classifications.** PII terms for columns, from column names and sampled values. These are the same detectors that label PII during ingestion.
- **Descriptions.** A grounded draft from the schema, lineage and neighbouring documentation, when a model is configured. Drafts count against the AI budget (`max_drafts` per run); when the budget runs out, the run keeps proposing everything else.

**Review.** In the review screen, select many proposals and **Accept** or **Reject** them together. You can edit a description or owner before accepting.

- Each proposal needs the right to make that edit on its dataset (`EDIT_ENTITY_OWNERS`, `EDIT_DATASET_COL_GLOSSARY_TERMS` or `EDIT_ENTITY_DOCS`).
- Accepted owners and terms are written as one bulk batch attributed to the reviewer, so they can be undone like any batch.
- Rejected proposals are not made again, and pending ones aren't repeated.
- Set `CAIRN_STEWARD_AUTO_ACCEPT=0.9` to apply classifications at or above that confidence without review. They are recorded as `AUTO_ACCEPTED`.

`cairn ai eval` includes deterministic cases for owner inference and classification (`config/ai-evals/steward.yaml`). API: `GET /cairn/api/ai/steward/proposals?state=&kind=&urn=` and `POST /cairn/api/ai/steward/decide` with `{"ids", "action", "edits"}`.

## Context layer for analytics agents

Text-to-SQL agents fail mostly on business meaning, not syntax: which revenue, which customer status. Cairn gives them vetted meaning from what your teams already do.

**How was this answered before.** Every mined query is indexed by the question it answers: its name and description, the metrics it computes, what it groups by, and the tables it reads. **Context inbox → Answered before** searches them, and so do agents, through the `how_was_this_answered` MCP tool or `GET /cairn/api/context/answers?question=…`. Results show the SQL people actually ran, with the approved definitions it uses. Only queries over tables the caller may see are returned.

**Approved definitions.** In the Context inbox, **Edit & approve** on a metric or join finding lets the owner set the name, expression and description before accepting. The result is an approved definition. Each change makes a new version, and earlier versions are kept (`GET /cairn/api/context/definitions/{id}/versions`). Agents read them with the `get_definitions` MCP tool.

**Exports to the warehouse's semantic layer.** **Context inbox → Definitions** exports the approved metrics and joins:

- **Snowflake:** a `CREATE SEMANTIC VIEW` with tables, relationships and metrics, for Cortex Analyst and Snowflake Intelligence.
- **Databricks:** a Unity Catalog metric view (`WITH METRICS LANGUAGE YAML`) over the fact table you choose, for Genie.

Review the SQL before running it. Primary keys come from approved join targets. The API is `GET /cairn/api/context/definitions/export?format=snowflake|databricks&name=&source=&datasets=`.

**Measuring it.** `cairn ai eval` includes `text2sql` cases. Each one asks the question twice, first with the schema only and then with Cairn's context (approved definitions and similar past queries). It runs both answers against a small warehouse, compares them with the expected result, and reports accuracy both ways. Offline, the model output is scripted. With `--live` it asks your configured model, so you can see what the context layer is worth on your own questions: add cases to `config/ai-evals/`. The generator refuses anything but a single read-only `SELECT`.

## Governed metrics (semantic layer)

Where teams already define metrics in a semantic layer, Cairn imports them so people and agents query a metric by name instead of writing SQL for it.

**Import.** An administrator posts the file to `POST /cairn/api/context/semantic/import` with `{"format": "metricflow" | "cube" | "lookml", "content": "<file text>", "ref": "<path>"}`. Supported:

- **dbt Semantic Layer (MetricFlow):** simple, ratio and derived metrics over semantic models' measures, with their dimensions and default time dimension.
- **Cube:** each cube's measures (`count`, `sum`, `avg`, `min`, `max`, `count_distinct`, `number`) over its `sql_table`.
- **LookML:** each view's measures over its `sql_table_name`. Measures that reference other fields (`${total}`) are skipped.

Each metric becomes a metric finding in the **Context inbox**, mapped to the catalog dataset for its table. Approving it makes it a governed, versioned definition that keeps its dimensions and time dimension. Nothing is queryable before it is approved.

**Query.** `GET /cairn/api/context/metrics` lists the approved metrics over datasets you may see. `POST /cairn/api/context/metrics/{name}/query` takes `{"dimensions": [...], "filters": [{"dimension", "op", "value"}], "time_grain": "day|week|month|quarter|year", "limit": 200}`. The SQL is compiled from the definition:

- dimensions and filters must be the metric's own;
- filter values are always literals, and the operators are `=`, `!=`, `<`, `<=`, `>`, `>=` and `in`;
- an expression with a subquery is refused.

The query runs through data access, so your permissions, purpose and masking apply. The response includes the SQL that ran.

Agents use the `list_metrics` and `query_metric` MCP tools, and the assistant uses them too. `query_metric` needs the agent's `query_data` scope.

## Knowledge sources

Teams explain their data in wikis and documents. Cairn reads those pages, links them to the assets they talk about, and uses them to ground the assistant and agents.

**Sources** are reviewed YAML files in `config/knowledge/`, or set `CAIRN_KNOWLEDGE`. Start from `sources.yaml.example`:

| Type | Reads | Needs |
|---|---|---|
| `confluence` | the pages of one space | a bot account's email and API token |
| `notion` | the pages of one database | an internal integration token, with the database shared to it |
| `gdocs` | the Google Docs in one Drive folder | the service's Google identity (or `token`), with read access to the folder |
| `sharepoint` | the site pages of one site | an Entra ID app with `Sites.Read.All` |

Sources sync every day; `POST /cairn/api/context/knowledge/sync` runs them now. `GET /cairn/api/context/knowledge` shows each source's pages and last sync.

**Linking.** A page is linked to an asset when it names it:

- a link to the asset in Cairn;
- its full name (`schema.table` or longer);
- for distinctive names (with an underscore, at least eight characters, mentioned twice), the table name alone.

Linked pages show on the asset under **From your team's docs**, and as links in the catalog view. Links people added there are kept.

**Grounding.** The `search_knowledge` tool (assistant and MCP) finds pages that answer a question by meaning, with an excerpt, the link and the datasets each page is about. Page text is screened like other catalog text before an AI sees it.

**Who sees what.** Cairn can't read each page's own permissions. Pages from a source are shown to everyone in Cairn, so index only spaces, databases, folders and sites the whole organisation may read. Set `admins_only: true` on a source to keep its pages to administrators.
