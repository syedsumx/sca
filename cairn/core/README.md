# Cairn Core images

Internal. How the Cairn Core service images are produced from the pinned engine release
(`UPSTREAM_VERSION`). Licence obligations: see `../THIRD_PARTY_NOTICES.md`, which every image ships at
`/usr/share/doc/cairn/`. Engine internals, the patch list and upgrade notes: `UPSTREAM.md`.

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
| UI branding | `web/rebrand.mjs` | Rewrites user-visible string literals, JSX text and the translation catalogues (values only, never keys or GraphQL documents); points the upstream documentation links at `/docs`; replaces wordmark, marks, favicon, manifest and page title; disables three vendor-media panels: the product-announcements panel, the welcome modal and the source-setup education modal. Each patch must still match, so an upstream rename fails the build |
| UI packaging | `web/package.py` | Renames bundle assets that carry the engine's name (images get the Cairn mark) and rewrites their references; adds `assets/brand/` |
| Server relabel | `tools/relabel.py` | In compiled classes, rewrites only prose string constants (text with a space) and the root-account URN; in seed data (bootstrap MCPs, root policies) renames the root account to `urn:li:corpuser:admin`, relabels display names and default source names |
| Settings | `rootfs/usr/local/bin/cairn-env`, `rootfs/etc/cairn/env.d/*.map` | Entrypoint shim: maps the documented `CAIRN_*` settings onto the engine's variable names; an engine variable that is already set wins |
| Init hooks | `rootfs/etc/cairn/init.d/` | JDBC URL and driver from `CAIRN_DB_TYPE`/`CAIRN_DB_HOST`/`CAIRN_DB_NAME`; schema-registry URL from the core API address; the `admin` login from `CAIRN_ADMIN_PASSWORD` (the engine's sample accounts are removed) |

Engine defaults that used to live in compose (consumers, search implementation, theme, token auth, …)
are `ENV` in `Dockerfile`, so deployments only set `CAIRN_*`.

## Settings

A `CAIRN_*` setting can also come from a file: `CAIRN_X_FILE=/path` sets `CAIRN_X` to the file's content (for
Kubernetes or Docker secrets). Setting both `CAIRN_X` and `CAIRN_X_FILE` is an error.

| Cairn setting | Images | Notes |
|---|---|---|
| `CAIRN_ADMIN_PASSWORD` | web | required; root account `admin` |
| `CAIRN_LOCAL_USERS_FILE`, `CAIRN_WEB_USERS_FILE` | web | paths, not secrets: extra `user:password` accounts (one per line) and the file the login reads |
| `CAIRN_SYSTEM_CLIENT_SECRET` | all | internal service-to-service secret |
| `CAIRN_SESSION_SIGNING_KEY`, `CAIRN_TOKEN_SALT` | all | access/session token signing and salt; `cairn-core-proxy` reads the key too (Helm) |
| `CAIRN_TOKEN_AUTH_ENABLED` | all | token authentication (`METADATA_SERVICE_AUTH_ENABLED`) |
| `CAIRN_WEB_SECRET` | web | cookie signing |
| `CAIRN_SSO_*` | web | OpenID Connect sign-in: client, discovery URL, base URL, scope, claims, groups, provisioning. Register `<CAIRN_SSO_BASE_URL>/callback/oidc` with the identity provider; `cairn sso check` validates the settings (`core/rootfs/etc/cairn/env.d/10-web.map` lists them all) |
| `CAIRN_DB_TYPE` (`mysql` or `postgres`), `CAIRN_DB_HOST`, `CAIRN_DB_NAME`, `CAIRN_DB_USER`, `CAIRN_DB_PASSWORD` (or `CAIRN_DB_URL`), `CAIRN_DB_SSLMODE` (postgres) | core, setup | the catalog's database: MySQL, or PostgreSQL |
| `CAIRN_SEARCH_HOST`, `CAIRN_SEARCH_PORT`, `CAIRN_SEARCH_USE_SSL`, `CAIRN_SEARCH_USERNAME`, `CAIRN_SEARCH_PASSWORD` | core, setup | OpenSearch |
| `CAIRN_KAFKA_BOOTSTRAP`, `CAIRN_SCHEMA_REGISTRY_URL` | all | Kafka |
| `CAIRN_CORE_API_HOST`, `CAIRN_CORE_API_PORT`, `CAIRN_CORE_API_PROTOCOL` | web, setup, executor | web points at `cairn-core-proxy` |
| `CAIRN_NATIVE_POLICIES_ENABLED`, `CAIRN_VIEW_AUTHORIZATION_ENABLED` | core | authorizer mode of the engine: `CAIRN_NATIVE_POLICIES_ENABLED` maps to `AUTH_POLICIES_ENABLED` (the image default is `true`, which is additive mode) |
| `CAIRN_AUTHZ_MODE` | control plane | read by the Python control plane (`src/cairn/common/config.py`), not by the Core image: `additive` (default) or `authoritative`. Authoritative also needs `CAIRN_NATIVE_POLICIES_ENABLED=false`; the Helm overlay is `helm/cairn/values-authoritative.yaml` |
| `CAIRN_PDP_URL`, `CAIRN_SERVICE_TOKEN` | core | read by the authorizer plugin (`plugins/java/cairn-authorizer/config.yml`) |
| `CAIRN_APP_TITLE`, `CAIRN_LOGO_URL`, `CAIRN_FAVICON_URL` | core | per-tenant branding overrides |
| `CAIRN_JAVA_OPTS` | core, web | JVM options |
| `CAIRN_WEB_PORT` | web | HTTP port |

## Protocol identifiers that keep the engine's spelling

Changing these would break stored data, clients or the ingestion framework, and none are shown to
end users: URN types and aspect names (`urn:li:dataHubPolicy:…`, `dataHubExecutionRequest`), the
system principal `__datahub_system`, Kafka topic names, HTTP header names, Java/Python package and
module names, and in-container paths such as `/etc/datahub/plugins`.

## Edge rules

The edge returns 404 for the engine's developer consoles (`/openapi/swagger-ui`, `/openapi/v3/api-docs`,
`/api/graphiql`). The rule is in `docker/nginx/locations.conf` (Compose), in the Helm ingress
(`helm/cairn/templates/ingress.yaml`) and in the Gateway API routes (`helm/cairn/templates/gateway-api.yaml`).
`cairn-core` and `cairn-web` publish ports on loopback only in compose; users reach Cairn through the edge.

## Upgrading the engine

1. Bump `core/UPSTREAM_VERSION`; read the release notes for renamed variables (update `env.d/*.map`).
   `deploy/build-images.sh` passes the version to the build. A direct `docker build` uses the Dockerfile's
   `UPSTREAM_VERSION` and `UPSTREAM_JAR_VERSION` arguments, which must then be changed too.
2. `core/web/build.sh` and check that `rebrand.mjs` still finds `ProductUpdates.tsx` (it fails loudly).
3. Rebuild, start a fresh stack, run `pytest tests/e2e` and `node core/tools/leak_scan.mjs`.
