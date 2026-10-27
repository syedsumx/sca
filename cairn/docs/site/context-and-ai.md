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
