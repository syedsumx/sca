---
title: Administration
order: 8
---
# Administration

## Install

* **Docker Compose** for evaluation and small installs: copy `.env.example` to `.env`, set every
  `CHANGE_ME`, then `docker compose -f docker-compose.cairn.yml up -d`. Open port 9000.
* **Kubernetes**: the `cairn` Helm chart. Bring your own MySQL 8, OpenSearch 2 and Kafka 3; put the
  secrets in a Kubernetes Secret named `cairn-core-secrets` (see the chart's values file).

## Accounts

* `admin` is the built-in root account; its password is `CAIRN_ADMIN_PASSWORD`. Use it to set up
  single sign-on and roles, then keep it for break-glass use.
* The control plane runs as `cairn-service`, a least-privilege service account created at install.
  It cannot manage policies, users, credentials, secrets or other people's tokens. Its token is
  rotated every `CAIRN_TOKEN_ROTATE_DAYS` days (default 7).
* User provisioning (SCIM) writes as `cairn-provisioner`, which may manage people and groups and
  nothing else. Keeping it apart means the control plane can never change group membership, and so
  never its own access. Its token is rotated the same way and only the API service can read it.
* People create their own access tokens under **Settings → Access Tokens**.

## Single sign-on

Cairn signs people in with any OpenID Connect identity provider (Okta, Microsoft Entra ID, Google,
Keycloak, Auth0 and others). Password sign-in stays available for `admin`.

1. In the identity provider, create a web application (authorization code flow) and register the
   redirect URI `<CAIRN_PUBLIC_URL>/callback/oidc`, for example
   `https://cairn.example.com/callback/oidc`.
2. Set `CAIRN_SSO_CLIENT_ID`, `CAIRN_SSO_CLIENT_SECRET` and `CAIRN_SSO_DISCOVERY_URL` (the
   provider's `.../.well-known/openid-configuration` address).
3. Check the settings before switching sign-in on:

   ```
   docker compose -f docker-compose.cairn.yml exec cairn-api cairn sso check
   ```

   It prints the redirect URI to register and checks the discovery document, signing keys, PKCE
   support and the client credentials (without signing anyone in). Fix anything marked `FAIL`.
4. Set `CAIRN_SSO_ENABLED=true` and restart `cairn-web`. The login page shows **Sign in with SSO**.

People are created on first sign-in, and the groups in their token's `groups` claim
(`CAIRN_SSO_GROUPS_CLAIM`) become Cairn groups, so personas and policies can refer to them. Other
settings: `CAIRN_SSO_SCOPE` (default `openid email profile`), `CAIRN_SSO_USER_CLAIM` (default
`preferred_username`) and `CAIRN_SSO_REQUIRED_GROUPS` (only members of these groups may sign in).

## User provisioning (SCIM)

With SCIM 2.0 the identity provider creates, updates and deactivates people and groups ahead of
their first sign-in, and removes access when someone leaves.

* Base URL: `<CAIRN_PUBLIC_URL>/cairn/api/scim/v2`
* Authentication: bearer token, the value of `CAIRN_SCIM_TOKEN` (`openssl rand -hex 32`)
* Supported: Users and Groups (create, read, replace, patch, delete), filters on `userName`,
  `externalId`, `displayName` and `id`, pagination.

A SCIM user's `userName` is the Cairn user name, so it must match what single sign-on provides
(`CAIRN_SSO_USER_CLAIM`). Deleting a user deactivates them; their ownership and history stay. Group
membership changes take effect on policy decisions within seconds. When groups come from SCIM, set
`CAIRN_SSO_EXTRACT_GROUPS=false` so sign-in does not also rewrite them.

## Main settings

| Setting | Purpose |
|---|---|
| `CAIRN_ADMIN_PASSWORD` | root account password |
| `CAIRN_PUBLIC_URL` | URL users reach Cairn on (used in notification links) |
| `CAIRN_AUTHZ_MODE`, `CAIRN_NATIVE_POLICIES_ENABLED` | additive or authoritative governance |
| `CAIRN_LLM_PROVIDER`, `CAIRN_AI_MODEL`, `ANTHROPIC_API_KEY`, `CAIRN_OLLAMA_URL` | assistant model |
| `CAIRN_SLACK_*`, `CAIRN_TEAMS_WEBHOOK_URL`, `CAIRN_SMTP_URL` | notification channels |
| `CAIRN_EMBED_ALLOWED_ORIGINS` | sites allowed to embed Cairn components |
| `CAIRN_TOKEN_ROTATE_DAYS`, `CAIRN_TOKEN_GRACE_MINUTES` | service token rotation |
| `CAIRN_SSO_*` | single sign-on (see above) |
| `CAIRN_SCIM_TOKEN` | SCIM provisioning token |
| `CAIRN_MCL_INVALIDATION` | apply metadata changes to access decisions at once (default `true`) |

Secrets (`CAIRN_*_SECRET`, `*_KEY`, `*_PASSWORD`) must be long random values
(`openssl rand -hex 32`).

## Network

Users only need the edge (port 9000, or your ingress). The internal APIs are not exposed; developer
consoles are disabled at the edge.

**HTTPS.** Put the certificate chain and key (PEM) in `deploy/tls/` as `tls.crt` and `tls.key`, or
point `CAIRN_TLS_DIR` at another directory, and restart the edge. It then also serves HTTPS on port
9443 (`CAIRN_MAPPED_EDGE_TLS_PORT`) with TLS 1.2+ and HSTS. Set `CAIRN_TLS_REDIRECT=true` to send
plain HTTP to HTTPS (`CAIRN_TLS_PUBLIC_PORT` is the port users reach, default 443), and set
`CAIRN_PUBLIC_URL` to the `https://` address. On Kubernetes, TLS is the ingress's job.

**Rate limits.** Per client address: password sign-in 10 attempts a minute (bursts of 20), the Cairn
API 50 requests a second (bursts of 400). Over the limit, the edge answers `429 Too Many Requests`.

## Secrets as files

Any `CAIRN_*` secret can be given as a file instead: `CAIRN_DB_PASSWORD_FILE=/run/secrets/db` sets
`CAIRN_DB_PASSWORD` to the file's content (Docker secrets, Kubernetes secret volumes, Vault agent).
Setting both forms is an error, so a stale value cannot hide the file. `ANTHROPIC_API_KEY_FILE` works
the same way.

## Audit log

Denied access decisions, policy, persona and purpose changes, grants, access requests,
certification approvals, user provisioning and remote-agent activity are written to a hash-chained
audit log: altering or deleting an entry breaks the chain.

* **Check it**: `cairn audit verify`, or `GET /cairn/api/governance/audit/verify`.
* **Export it** to a SIEM or an archive, in chain order:
  `GET /cairn/api/governance/audit/export?after_seq=<last seq>&format=jsonl` (or `csv`; filters
  `since`, `until`, `category`), or `cairn audit export --out audit.jsonl`. `--follow` keeps
  writing new entries, for a log shipper.
* **Verify an export** without database access: `cairn audit verify-file audit.jsonl`. Pass the
  previous export's `head` with `--after-head` to prove nothing is missing in between.

## Upgrades

Run the new version's images. The `cairn-setup` job migrates the catalog's schema, indices and topics
before the services start, and the control-plane services migrate their own database on start (one
at a time, under a database lock). Read the release notes for setting changes first.

```bash
cairn db current     # schema revision of the control-plane database, and the latest one
cairn db check       # non-zero exit if the schema is not at the latest revision or differs from it
```

Back up the control-plane database before upgrading; migrations move forward only.
