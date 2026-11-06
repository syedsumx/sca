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

## Clients

Clients for the main Cairn APIs (identity, dataset health, semantic search and questions, governed SQL, saved
queries, data products and access requests, custom agents) are in the repository under `sdks/`. Neither has any
dependencies. Both send the token as a bearer token, refuse plain `http://` except to localhost, raise an error
carrying Cairn's status and explanation, retry a `429` or `503` only when it carries `Retry-After` (twice by
default, waiting up to 30 s), and warn once per route when a route is deprecated. Every call can also be made
directly with `request(method, path, …)`.

**JavaScript and TypeScript** (`sdks/js`, Node 18+, browsers, Deno, Bun; types included):

```js
import { CairnClient } from "@cairn/client";

const cairn = new CairnClient({ baseUrl: "https://<cairn-host>/cairn/api", token: process.env.CAIRN_TOKEN });
const { columns, rows } = await cairn.runSql("lakehouse", "SELECT region, count(*) FROM lake.sales.orders GROUP BY 1");
```

**Java** (`sdks/java`, Java 17+, `io.cairn.client`):

```java
CairnClient cairn = CairnClient.builder("https://<cairn-host>/cairn/api").token(System.getenv("CAIRN_TOKEN")).build();
Map<String, Object> result = cairn.runSql("lakehouse", "SELECT region, count(*) FROM lake.sales.orders GROUP BY 1", 200);
```

Answers are JSON (`Map<String, Object>` and `List<Object>` in Java). A token sees and does only what its owner may:
masked columns stay masked and denied tables answer `403`, whichever client asks.

## Deprecation policy

Routes are retired with notice, never silently:

- A deprecated route keeps working for at least **180 days**. Until then every answer from it carries
  `Deprecation: @<unix time>` (RFC 9745), `Sunset: <date>` (RFC 8594) and, when there is one,
  `Link: <replacement>; rel="successor-version"`. The API reference marks it deprecated, with its sunset date.
- After its sunset date the route answers `410 Gone`, with the same headers and the replacement in the message.
- `GET /cairn/api/deprecations` (any signed-in caller) lists the routes being retired, their dates and replacements. The JavaScript and
  Java clients report each deprecated route they call once (a warning by default; pass `onDeprecation` to handle
  it yourself).
- Within `/v1/`, fields are added but not removed or given a new meaning; a change that would do either goes
  through this same notice.
