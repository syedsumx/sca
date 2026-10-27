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

## MCP for AI tools

Cairn exposes the catalog to AI coding tools over the Model Context Protocol at `/cairn/mcp/mcp`.
Each request carries the user's own access token, so tools see exactly what the user can see.

```bash
claude mcp add --transport http cairn https://<cairn-host>/cairn/mcp/mcp \
  --header "Authorization: Bearer <your access token>"
```
