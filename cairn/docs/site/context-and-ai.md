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

## Assistant

Ask questions in plain English from the search bar or the assistant panel. The assistant only uses
metadata you are allowed to see, redacts personal data before anything reaches a model, cites the
assets it used, and can explain lineage anomalies. It runs on Claude or on a local model for
air-gapped installs; without a model it falls back to search-based answers.

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
claude mcp add --transport http cairn https://<cairn-host>/cairn/mcp/mcp \
  --header "Authorization: Bearer <your access token>"
```

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
