# Cairn Core images

Internal. How the Cairn Core service images are produced from the pinned engine release
(`UPSTREAM_VERSION`). Licence obligations: see `../THIRD_PARTY_NOTICES.md`, which every image ships at
`/usr/share/doc/cairn/`. Engine internals and upgrade notes: `UPSTREAM.md`.

| Image | Target | Role |
|---|---|---|
| `cairn/core` | `core` | metadata service: APIs, search, graph, schema registry, authorizer plugin baked in |
| `cairn/web` | `web` | web application, login, Cairn-branded UI bundle, workspace MFE config |
| `cairn/setup` | `setup` | one-shot schema / index / topic / seed-data job; run before every start or upgrade |
| `cairn/executor` | `executor` | runs ingestion recipes scheduled from the UI |

```bash
core/web/build.sh                                  # UI bundle -> core/web/build/web-ui-assets.jar
ONLY="authorizer core web setup executor" deploy/build-images.sh
```

## What each build step does

| Piece | File | Effect |
|---|---|---|
| UI white-label | `web/rebrand.mjs` | Rewrites user-visible string literals, JSX text and the 14 translation catalogues (values only, never keys or GraphQL documents); points every upstream documentation link at `/docs`; replaces wordmark, marks, favicon, manifest and page title; disables the product-announcements panel |
| UI packaging | `web/package.py` | Renames bundle assets that carry the engine's name (images get the Cairn mark) and rewrites their references; adds `assets/brand/` |
| Server relabel | `tools/relabel.py` | In compiled classes, rewrites only prose string constants (text with a space) and the root-account URN; in seed data (bootstrap MCPs, root policies) renames the root account to `urn:li:corpuser:admin`, relabels display names and default source names |
| Settings | `rootfs/usr/local/bin/cairn-env`, `rootfs/etc/cairn/env.d/*.map` | Entrypoint shim: maps the documented `CAIRN_*` settings onto the engine's variable names; an engine variable that is already set wins |
| Init hooks | `rootfs/etc/cairn/init.d/` | JDBC URL and driver from `CAIRN_DB_TYPE`/`CAIRN_DB_HOST`/`CAIRN_DB_NAME`; schema-registry URL from the core API address; the `admin` login from `CAIRN_ADMIN_PASSWORD` (the engine's sample accounts are removed) |

Engine defaults that used to live in compose (consumers, search implementation, theme, token auth, …)
are `ENV` in `Dockerfile`, so deployments only set `CAIRN_*`.

## Settings

| Cairn setting | Images | Notes |
|---|---|---|
| `CAIRN_ADMIN_PASSWORD` | web | required; root account `admin` |
| `CAIRN_SYSTEM_CLIENT_SECRET` | all | internal service-to-service secret |
| `CAIRN_SESSION_SIGNING_KEY`, `CAIRN_TOKEN_SALT` | core, setup | access/session token signing; `cairn-core-proxy` reads the key too |
| `CAIRN_WEB_SECRET` | web | cookie signing |
| `CAIRN_DB_TYPE` (`mysql` or `postgres`), `CAIRN_DB_HOST`, `CAIRN_DB_NAME`, `CAIRN_DB_USER`, `CAIRN_DB_PASSWORD` (or `CAIRN_DB_URL`), `CAIRN_DB_SSLMODE` (postgres) | core, setup | the catalog's database: MySQL, or PostgreSQL |
| `CAIRN_SEARCH_HOST`, `CAIRN_SEARCH_PORT`, `CAIRN_SEARCH_USE_SSL`, `CAIRN_SEARCH_USERNAME`, `CAIRN_SEARCH_PASSWORD` | core, setup | OpenSearch |
| `CAIRN_KAFKA_BOOTSTRAP`, `CAIRN_SCHEMA_REGISTRY_URL` | all | Kafka |
| `CAIRN_CORE_API_HOST`, `CAIRN_CORE_API_PORT`, `CAIRN_CORE_API_PROTOCOL` | web, setup, executor | web points at `cairn-core-proxy` |
| `CAIRN_NATIVE_POLICIES_ENABLED`, `CAIRN_VIEW_AUTHORIZATION_ENABLED` | core | authorizer mode (see `plugins/java/cairn-authorizer`) |
| `CAIRN_PDP_URL`, `CAIRN_SERVICE_TOKEN`, `CAIRN_AUTHZ_MODE` | core | read by the authorizer plugin |
| `CAIRN_APP_TITLE`, `CAIRN_LOGO_URL`, `CAIRN_FAVICON_URL` | core | per-tenant branding overrides |
| `CAIRN_JAVA_OPTS`, `CAIRN_WEB_PORT` | core, web | JVM / port |

## Protocol identifiers that keep the engine's spelling

Changing these would break stored data, clients or the ingestion framework, and none are shown to
end users: URN types and aspect names (`urn:li:dataHubPolicy:…`, `dataHubExecutionRequest`), the
system principal `__datahub_system`, Kafka topic names, HTTP header names, Java/Python package and
module names, and in-container paths such as `/etc/datahub/plugins`.

## Edge rules

The edge (`docker/nginx/nginx.conf`, Helm ingress) returns 404 for the engine's developer consoles
(`/openapi/swagger-ui`, `/openapi/v3/api-docs`, `/api/graphiql`). `cairn-core` and `cairn-web`
publish ports on loopback only in compose; users reach Cairn through the edge.

## Upgrading the engine

1. Bump `UPSTREAM_VERSION`; read the release notes for renamed variables (update `env.d/*.map`).
2. `core/web/build.sh` and check that `rebrand.mjs` still finds `ProductUpdates.tsx` (it fails loudly).
3. Rebuild, start a fresh stack, run `pytest tests/e2e` and `node core/tools/leak_scan.mjs`.
