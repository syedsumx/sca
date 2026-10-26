---
title: APIs
order: 9
---
# APIs

All APIs are served on the same host as the UI and authenticate with an access token
(`Authorization: Bearer <token>`); create one under **Settings → Access Tokens**. What a token can
do is exactly what its owner can do.

| API | Path | Use |
|---|---|---|
| Cairn API (REST) | `/cairn/api/…` | quality, governance, marketplace, collaboration, context, agents, AI, embeds |
| Cairn API reference | [`/cairn/api/docs`](/cairn/api/docs) | interactive OpenAPI reference |
| Catalog GraphQL | `/api/v2/graphql` | search, entities, lineage, glossary, ownership |
| MCP | `/cairn/mcp/mcp` | AI coding tools (streamable HTTP) |

## Examples

```bash
# Your identity and roles
curl -H "Authorization: Bearer $TOKEN" https://<cairn-host>/cairn/api/me

# Search the catalog
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  https://<cairn-host>/api/v2/graphql \
  -d '{"query":"{ searchAcrossEntities(input:{query:\"orders\", start:0, count:5}) { total searchResults { entity { urn } } } }"}'

# Data quality health of a dataset
curl -H "Authorization: Bearer $TOKEN" \
  "https://<cairn-host>/cairn/api/quality/datasets/<url-encoded dataset urn>/health"
```
