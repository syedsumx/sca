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

### SAML identity providers

For an identity provider set up for SAML 2.0 (ADFS, Okta, Entra ID, PingFederate, Keycloak and others), Cairn
signs people in with SAML and hands the result on to the sign-in above as OpenID Connect: Cairn is the SAML
service provider towards the identity provider, and an OpenID provider towards `cairn-web` (`cairn.identity.saml`).

1. Set `CAIRN_SSO_ISSUER_URL` to where browsers reach Cairn's API, followed by `/v1/sso` (for example
   `https://cairn.example.com/cairn/api/v1/sso`), `CAIRN_SAML_IDP_METADATA_URL` (or `CAIRN_SAML_IDP_METADATA_FILE`)
   to the identity provider's SAML metadata, `CAIRN_SSO_SIGNING_PASSPHRASE` (encrypts Cairn's token-signing key;
   `_FILE` works) and `CAIRN_SSO_CLIENTS_FILE`, a JSON file of the OpenID clients:
   `[{"client_id": "cairn-web", "client_secret": "…", "redirect_uris": ["https://cairn.example.com/callback/oidc"]}]`.
2. In the identity provider, add a SAML application from Cairn's metadata, `<CAIRN_SSO_ISSUER_URL>/saml/metadata`
   (entity ID `<CAIRN_SSO_ISSUER_URL>/saml/metadata` unless `CAIRN_SAML_SP_ENTITY_ID` is set; assertion consumer
   service `<CAIRN_SSO_ISSUER_URL>/saml/acs`, HTTP-POST). Sign the assertion or the whole response (do not encrypt
   it), and send the groups in an attribute named `groups` (`CAIRN_SAML_GROUPS_ATTRIBUTE`), the email in `email`
   (`CAIRN_SAML_EMAIL_ATTRIBUTE`). The user is the NameID unless `CAIRN_SAML_USERNAME_ATTRIBUTE` names an attribute.
3. Point the OIDC sign-in at Cairn: `CAIRN_SSO_DISCOVERY_URL=<CAIRN_SSO_ISSUER_URL>/.well-known/openid-configuration`,
   with the client ID and secret from the clients file, then `cairn sso check` and `CAIRN_SSO_ENABLED=true` as above.

Cairn accepts a SAML response only if all of these hold:
- It answers a sign-in Cairn started (`InResponseTo`, used once, within 10 minutes).
- It holds exactly one assertion. That assertion, or the response containing it, carries a valid signature with the
  identity provider's certificate, and only the signed content is read.
- The issuer is the identity provider, the destination and recipient are Cairn's ACS, and the audience is Cairn.
- It is inside its validity window (`CAIRN_SAML_CLOCK_SKEW_SECONDS`, default 120).
- Its assertion ID has not been seen before.

Encrypted assertions and documents with a DTD are refused. The OpenID side issues authorization codes that work
once for 2 minutes, checks PKCE (S256) and the client secret, and signs ID tokens with a key of its own, so they
are never accepted by Trino. Sign-ins and refusals (with the reason) are audited: `sso.saml_signed_in` and
`sso.saml_refused`, category `identity`. Install the `saml` extra (`pip install "cairn-platform[saml]"`). Tables
`cairn_saml_requests`, `cairn_saml_assertions` and `cairn_sso_codes` (migration 0079). Tested against a real
Keycloak (`tests/test_saml_e2e.py`), with both a signed assertion and a signed response. The refusals tested are a
tampered NameID, a second assertion smuggled beside the signed one, a replay, a response for another sign-in, a
DTD, a wrong PKCE verifier and a wrong client secret.

## API deprecations

Routes being retired are listed in `cairn.common.deprecation.DEPRECATIONS` and at `GET /cairn/api/deprecations`.
From the deprecation date they answer with `Deprecation`, `Sunset` and `Link: rel="successor-version"` headers;
from the sunset date (at least 180 days later) they answer `410 Gone`. Before an upgrade past a sunset, see who
still calls a retired route: `cairn_api_deprecated_requests_total{route}` counts calls to deprecated routes, and
the clients in `sdks/` warn their callers once per route. The policy for callers is in the API docs
(*Deprecation policy*).

## API quotas and the IP allowlist

Each tenant (one Cairn deployment) can cap API use, and accept requests only from known networks.

| Setting | Environment default | What it does |
|---|---|---|
| `tenant_per_minute` | `CAIRN_API_TENANT_PER_MINUTE` | requests a minute for the whole tenant |
| `user_per_minute` | `CAIRN_API_USER_PER_MINUTE` | requests a minute for each person |
| `users` | — | `{urn: n}`: a person's (or a service account's) own limit |
| `ip_allowlist` | `CAIRN_IP_ALLOWLIST` | CIDRs; requests from anywhere else get 403, before sign-in |
| `trusted_proxies` | `CAIRN_TRUSTED_PROXIES` | CIDRs of the proxies in front, whose `X-Forwarded-For` is believed |

Environment values are comma-separated. An operator (`MANAGE_SYSTEM_OPERATIONS`) changes them at run time with
`GET` and `PUT /v1/admin/limits`; every replica uses the change within 5 seconds, and the change is audited
(`limits.changed`).

* **Over a quota**, the API answers 429 with `Retry-After` (the seconds left in the minute), `X-RateLimit-Limit`,
  `X-RateLimit-Remaining` and `X-RateLimit-Scope` (`user` or `tenant`). Counts are kept in Cairn's database
  per minute, so all replicas share one count. Cairn's service token is not held to a person's quota, but it
  counts towards the tenant's.
* **The client's address** is the connection's. When the connection comes from a trusted proxy, it is the
  last address in `X-Forwarded-For` that is not a trusted proxy. An address put in front by the client is
  therefore not believed.
* **Locking out:** a change whose allowlist would refuse the operator making it is refused (409). Include the
  addresses of Cairn's own callers: Trino's access-control and event callbacks, Spark, the web UI's proxy,
  and in-cluster jobs (usually the pod CIDR).
* If the settings cannot be read, the last ones read are used; before any were read, requests are refused (fail closed). `/health` is never refused (probes).
* Agents keep their own per-agent quotas as well (`/v1/agents`).

Tables `cairn_api_limits` and `cairn_api_usage` (migration 0083). Tested over HTTP against two replicas sharing one
database (`tests/test_api_limits.py`). The tests cover:
* a person's quota counted across both replicas, with `Retry-After`;
* a named override;
* the tenant quota;
* the allowlist, a spoofed address refused, and a trusted proxy;
* the lock-out guard.

## Secret scopes

API keys, passwords and tokens that notebooks, jobs and pipelines need live in **secret scopes**
(`/v1/secret-scopes`), not in code or environment variables. Each scope has an access list of people and groups:

| Permission | May |
|---|---|
| `READ` | read values |
| `WRITE` | also add, change and remove secrets |
| `MANAGE` | also change the access list and delete the scope |

Whoever creates a scope manages it, and a scope always keeps at least one manager. Someone not on the list gets
404 for the scope, as if it did not exist. An operator (`MANAGE_SYSTEM_OPERATIONS`) may manage every scope, but
reads a value only after adding themselves with `READ`; that change is audited like any other. Extensions and AI
agents acting for someone are refused (403).

From a notebook or job, as its person: `cairn_lakehouse.secret("stripe", "api_key")`. It uses `CAIRN_URL` and the
person's own token (`CAIRN_TOKEN[_FILE]` or `CAIRN_WORKLOAD_TOKEN_FILE`). Over the API:
`PUT /v1/secret-scopes/<scope>/secrets/<key>` with `{"value": "..."}`, and `GET` the same path to read.

**Encryption.** Values are encrypted with AES-256-GCM, each bound to its scope and key, so a value copied to
another row does not decrypt. The key is `CAIRN_SECRETS_KEY`: 32 random bytes, base64
(`openssl rand -base64 32`), from a Kubernetes Secret; `CAIRN_SECRETS_KEY_FILE` works too. Without it the feature
is off (503 "not configured"); values are never kept unencrypted.

**Rotation.**
1. Put the new key in `CAIRN_SECRETS_KEY` and the old one in `CAIRN_SECRETS_KEY_PREVIOUS`, then restart.
2. As an operator, `POST /v1/secret-scopes/rotate`; it re-encrypts every value with the new key and returns how many.
3. Remove `CAIRN_SECRETS_KEY_PREVIOUS`.

**Values are never listed, logged or audited.** Listing a scope gives its keys, who last changed each one and
when. Reading a value returns `Cache-Control: no-store` and is audited as `secret.read` (who, scope and key). The
other entries are `secret.scope_created`, `secret.acl_changed`, `secret.written`, `secret.removed`,
`secret.scope_deleted` and `secret.key_rotated`, in category `identity`.

Limits: 1,000 secrets per scope, and 128 KB per value. Tables `cairn_secret_scopes`, `cairn_secret_acls` and
`cairn_secrets` (migration 0080). Tested over HTTP in `tests/test_secret_scopes.py`. The tests cover: access by
person and by group; the operator rule; a moved or altered ciphertext being refused; a notebook reading through
the client; and rotation with the previous key.

## Records: retention, legal holds and erasure

The privacy officer's side is in the governance guide (Privacy). Operators set:

* `CAIRN_PRIVACY_ACTOR`: the identity that deletes, for retention and erasure, on Trino. Give it `WRITE_DATASET`
  on the covered tables, and `MANAGE_DATASET` for expiring snapshots after an erasure. Without it,
  enforcement and erasure answer 503; checks still run.
* `CAIRN_ERASURE_SNAPSHOT_RETENTION` (default `0s`): how far back an erasure expires a table's snapshots. Trino's
  `iceberg.expire-snapshots.min-retention` must be at most this, or the proof says the history remains.
* Proofs are signed with Cairn's key store (`CAIRN_TRINO_ACCESS_KEY`), under a key of their own (`privacy-proof`).
  They cannot pass for Trino tokens.

Legal holds are enforced where the data is changed: in Trino's access control (Cairn's OPA endpoint). They are
read from the database on every check, so a hold applies at once on every replica, and a failed check refuses.
Data changed outside Trino (Spark writing to the catalog directly, for example) is not held. Use the store's
object lock for that.

Tables `cairn_legal_holds`, `cairn_retention_runs` and `cairn_erasure_proofs` (migration 0082). Tested on real Trino
(`test_legal_hold_retention_that_deletes_and_erasure_with_signed_proof`). The test covers:
* a held table refusing `DELETE`, `UPDATE`, `TRUNCATE`, `DROP` and `expire_snapshots` to an administrator;
* enforced retention deleting the two old rows of the other table;
* an erasure leaving one snapshot and 0 rows, and listing the held table as kept;
* the proof checked with the published key, and a forged copy refused;
* release.

The fail-closed hold check is tested in `tests/test_trino_access.py`.

## Access reviews

Owners recertify the access people hold, and access nobody vouches for does not survive the due date
(`/cairn/api/governance/access-reviews`, `cairn.governance.access_reviews`). Auditors (SOC 2 CC6.2/CC6.3, ISO 27001
A.5.18) ask for this evidence.

- **A campaign** (`POST`, `MANAGE_POLICIES`): `name`, `personas` (whose grants are reviewed; empty for all), `reviewers`, `due_days` (14) and `on_no_response` (`revoke`, the default, or `keep`).
  - It takes every active, time-boxed persona grant in scope, which is the access approved marketplace requests create. Lapsed grants are left out.
- **Who reviews.**
  - A grant from an access request goes to the data product's owners; any other grant to the campaign's `reviewers`.
  - Nobody reviews their own access.
  - A campaign in which some grant would have no reviewer is refused (422): name more reviewers.
- **Deciding.** Each reviewer sees only their own items (`GET .../{id}`) and keeps or revokes each one (`POST .../{id}/items/{item}`), with a comment.
  - A revoke takes the access away at once.
  - An item is decided once.
- **The due date.** The scheduler's leader closes due campaigns and settles undecided items by `on_no_response`. It also reminds reviewers with open items once a day; they were told when the campaign started.
  - `POST .../{id}/close` closes a campaign early, the same way.
- **The record.** Every start, decision and close is audited, with who decided and why. `GET .../{id}` gives an administrator the progress (items, decided, kept, revoked) and every item's outcome.
- **Tested** in `tests/test_access_reviews.py`, on SQLite and PostgreSQL, with the real policy decision point and persona grants.
  - A request-based grant was reviewed by its product's owner and kept.
  - Another was revoked, and access was refused at once.
  - A reviewer's own grant went to the other reviewer, and a campaign that would have left a grant unreviewed was refused.
  - At the due date, the grant nobody answered for was revoked. A `keep` campaign kept it.
  - The audit chain stayed whole.

## Passkeys and step-up

People sign in through their identity provider (OIDC or SAML), where multi-factor sign-in is set up. On top of that, Cairn asks for a passkey before the actions that matter most. A passkey (WebAuthn: a laptop's, phone's or security key's own key) is phishing-resistant and can't be replayed.

- **Turning it on.** Set `CAIRN_PASSKEY_RP_ID` (the site, such as `cairn.example.com`) and `CAIRN_PASSKEY_ORIGIN` (`https://cairn.example.com`). Without them, passkeys and step-up are off.
- **Actions that need a step-up.** `CAIRN_STEP_UP_ACTIONS`, by default `detokenize`, `tokens.forget` and `share.create`.
  - Add `secret.read` where secrets are only read interactively: notebooks and jobs read them as the person, with no one there to touch a key.
  - Service principals are never asked.
- **Registering.** `POST /v1/passkeys/register/options` gives the browser the options for `navigator.credentials.create`, and `POST /v1/passkeys/register` keeps the result: the public key only. `GET /v1/passkeys` lists one's own.
  - Someone who already has a passkey needs a step-up to add or remove one, so a stolen session cannot swap in the thief's own key.
  - The person is told of every new passkey (`security.passkey_added`).
- **Stepping up.** `POST /v1/passkeys/step-up/options`, then `POST /v1/passkeys/step-up` with the browser's assertion (`navigator.credentials.get`).
  - The answer is a step-up token for that person only, good for `CAIRN_STEP_UP_SECONDS` (300).
  - The client sends it as `X-Cairn-Step-Up`. Without a valid one, a listed action is refused with 401 and `WWW-Authenticate: step-up`.
- **People without a passkey** are refused the listed actions until they register one. `CAIRN_STEP_UP_ALLOW_UNENROLLED=true` lets them through during a roll-out.
- **Security.**
  - Challenges last five minutes and are used once.
  - User verification (PIN or biometric) is required.
  - The origin must match, so a phishing site's assertion fails.
  - A signature counter that goes backwards (a cloned authenticator) is refused.
  - Only hashes of step-up tokens are kept.
  - Registrations, removals, step-ups, refusals and each use are audited.
- **Tested** in `tests/test_passkeys.py`, over HTTP, with a software authenticator (a real P-256 key and WebAuthn data) checked by the `webauthn` library:
  - detokenising refused without a step-up and allowed with one;
  - someone else's, an expired or a made-up step-up refused;
  - a stolen session unable to add or remove a passkey;
  - a replayed challenge, a phishing origin and a cloned counter refused.

## Security posture

Cairn checks its own configuration and state for the mistakes that expose data, and says how to fix each one. It is Cairn's counterpart to Snowflake's Trust Center scanners.

- **Scans.** `POST /governance/posture/scan` (`MANAGE_SYSTEM_OPERATIONS`) runs every check now; the scheduler runs it once a day after the last scan. `GET /governance/posture` returns the latest result, and `GET /governance/posture/history` the scores over time.
- **Findings.** Each finding has a `check`, a `severity` (critical, high, medium or low), the `evidence` and the `fix`.
- **The score.** It starts at 100 and loses 25 per critical, 10 per high, 4 per medium and 1 per low finding.
- **Notifications.** A high or critical finding that is new since the last scan is sent to `CAIRN_SECURITY_CONTACTS` (people or groups, comma-separated) as `security.posture_finding`. Every scan is audited (`posture.scanned`).
- **The checks:**

  | Check | Severity | What it finds |
  |---|---|---|
  | `trusted_header` | critical | `CAIRN_DEV_TRUST_ACTOR_HEADER` is on: anyone who reaches the API can say who they are |
  | `open_policy` | critical / high | An allow policy for every action, or for reading or writing data on every dataset, with no condition and no subjects |
  | `audit_chain` | critical | The audit log's hash chain does not verify: an entry was changed or removed |
  | `pii_unmasked` | high | Datasets have columns classified sensitive and no policy masks anything |
  | `residency_region` | high | Namespaces are pinned to regions and `CAIRN_REGION` is unset |
  | `authz_mode` | medium | `CAIRN_AUTHZ_MODE` is not `authoritative` |
  | `long_shares` / `idle_shares` | medium / low | Active shares to outside recipients that last over 180 more days, or have not been used for 90 |
  | `no_access_review` | medium | Active grants and no access review closed in 180 days |
  | `secrets_key` | high / medium / low | The secret scopes' key is unusable, secrets are still under the previous key, or the previous key lingers after the rotation finished |
  | `ai_budget` | low | A model is configured and there is no daily AI budget |

- **Fail closed.** A check that cannot run (say, the catalog is down) is reported as `error: …` in `checks`, never as passed.
- **Tested** in `tests/test_posture.py`, with the real database, audit chain, policy decision point, shares and grants:
  - every check finding exactly what it is for, and a policy with a condition not flagged;
  - the score, and the findings going once fixed;
  - new high findings told once;
  - a tampered audit entry caught as critical;
  - a failing check reported as an error.

## Data residency

Data residency pins lakehouse namespaces to a region. Their tables are then stored only in that region's storage, are read and written only by a deployment in a region the pin allows, and are sent out only to such regions.

- **The rules are deployment configuration.** They live in one JSON file, which Cairn reads as `CAIRN_RESIDENCY_FILE`, Cairn Catalog as `CAIRN_CATALOG_RESIDENCY_FILE` and Cairn Store as `CAIRN_STORE_RESIDENCY_FILE`. In Kubernetes, set `cairn.residency.rules` in the chart: it becomes a ConfigMap mounted into every Cairn component, the catalog and the store.
  - A pin changes only through a reviewed deployment change. No administrator moves pinned data by changing a setting, and there is no API that does.
  - A file that is set but invalid stops Cairn and the catalog at start. Unknown keys are refused too, so a misspelt key cannot leave namespaces unpinned.

  ```json
  {"regions": {"eu": {"location": "s3://lake-eu/warehouse"}, "us": {"location": "s3://lake-us/warehouse"}},
   "pins": [{"namespaces": ["crm_eu", "hr_*"], "region": "eu"},
            {"namespaces": ["sales_us"], "region": "us", "allow": ["us", "eu"]}]}
  ```

  - `regions` gives each region's storage location. Each region's bucket must exist in the store.
  - `pins` names namespaces (or `*` patterns) and their region. `allow` lists the regions their data may be processed in and sent to. Without it, that is the pin's own region alone.
- **The deployment's region.** `CAIRN_REGION` (chart: `cairn.residency.region`) is where this deployment runs: Trino, Spark and the catalog's engines. A pinned table is read and written only when this region is in its pin's `allow`. Elsewhere (a standby in another region, a restored copy, a test deployment), the table is listed but no rows are read or written. With no region set, pinned tables are processed nowhere.
- **Where the data is stored.** Cairn Catalog keeps a pinned namespace's tables under `<region location>/<namespace>`, whoever asks, administrators included. It refuses:
  - any other location: at create, at register, in a commit, or in a `write.data.path`, `write.metadata.path` or `write.object-storage.path` property;
  - a change of the namespace's own `location`;
  - a clone or rename between namespaces that are not pinned alike, because a clone shares its source's files and a rename keeps them.

  Region locations are reserved for pinned namespaces: an unpinned table may not point into one, so pinned files are never reachable through a table that is not pinned. The store role that per-table keys come from is given the region buckets as well.
- **What leaves.**
  - **Shares.** A share takes the recipient's `region` (`POST /v1/shares` with `"region": "eu"`). A pinned table is shared only with a recipient whose region its pin allows, and that is checked again on every request the recipient makes. A recipient with no region gets no pinned table.
  - **Reverse syncs.** A reverse sync takes the `region` the app keeps its data in, checked at save and at every run.
  - **Copies made with SQL.** A Trino statement that reads a pinned table and writes a table (`CREATE TABLE … AS SELECT`, `INSERT … SELECT`, `UPDATE`, `MERGE`, materialized views) is refused before it runs, administrators included, unless the table it writes is in a namespace pinned to a region the source's pin allows. An unpinned namespace, or a table in another catalog (PostgreSQL through Trino, say), has no region and is refused. Copies within the pin, into another allowed region's pinned namespace, and from unpinned tables into pinned ones are allowed.
    - How: Trino asks Cairn about each table of a statement separately (the OPA protocol), and each question carries the statement's query id. Cairn records the pinned namespaces each statement reads and the namespaces it writes (`cairn_residency_flows`, in Cairn's database so every replica sees them, kept a day) and refuses the question that completes a forbidden pair, whichever comes first. A pinned read or a write that carries no query id cannot be followed and is refused.
    - Namespaces do not inherit a pin when they are created. Pins come only from the rules file, so to copy pinned data, pin the destination namespace (a reviewed change) and copy into it.
    - Engines that read files directly (pyiceberg, Spark with lakehouse tokens) can only read pinned tables in a region the pin allows; what such a client then writes elsewhere is outside what Cairn sees.
  - **Cairn Store's own transfers.** Bucket replication (and two-way sync), backups to a sink (on request, on a schedule and continuous), write-back to a mounted remote bucket and lifecycle transitions to a storage tier send objects to another store. Each replication target, backup sink, remote and tier names the region it keeps data in: `"residency_region": "eu"` in its entry in `CAIRN_STORE_REPLICATION_TARGETS_FILE`, `CAIRN_STORE_BACKUP_SINKS_FILE`, `CAIRN_STORE_REMOTES_FILE` or `CAIRN_STORE_TIERS_FILE`. An object under a region's location goes only to a destination whose region the pin of its namespace allows; a destination with no `residency_region` gets none. Objects under a region's location that no pin names stay in that region.
    - When configured: a replication configuration (each enabled rule's prefix; a prefix that names no single namespace counts every pin of the region), a lifecycle rule with a transition, and a write-back mount are refused with `403 AccessDenied`; a whole-store backup to a sink is refused when any bucket holding a region's location may not go there; continuous backup (`CAIRN_STORE_BACKUP_CONTINUOUS`) stops the store at start in the same case.
    - When it runs: every object replicated, journalled, written back or tiered is checked again, so rules that change later stop what they no longer allow (a replicated object is marked `FAILED`; a journal entry or write-back is dropped; a transition is skipped). The reason is in the store's log.
- **Seeing the rules.** `GET /governance/residency` (`MANAGE_POLICIES`) shows the deployment's region, the regions and the pins.
- **Fail closed.** If a namespace cannot be decided, it is refused: for example, when two pins put it in different regions, or when the check itself fails.
- **Pinning a namespace that already has tables.** Tables stored outside the region's location refuse commits until they are moved, for example with `CREATE TABLE … AS SELECT` into the pinned namespace.
- **Tested** in `tests/test_residency_e2e.py`, with the real store and catalog and the Cairn API:
  - locations, refusals for an administrator, renames and `write.data.path`;
  - reads from an allowed and a refused region, through the catalog and through Trino's access decisions;
  - shares by region, rechecked when the rules change, and reverse syncs;
  - copies on real Trino: `CREATE TABLE … AS SELECT`, `INSERT … SELECT` (with a join), `UPDATE` with a subquery and a copy into PostgreSQL refused, for an administrator too, leaving nothing written; copies within the pin, into an allowed region and from unpinned tables allowed;
  - a second Cairn Store standing in for the other regions: replication configurations refused or allowed by region and prefix, objects replicated within what pins allow, whole-store backups refused, continuous backup journalling us and unpinned data but no eu data, a write-back mount and a tier transition refused, and replication configured before a pin stopping (`FAILED`) once the store restarts with the pin.

## Tokenisation

Reversible tokenisation replaces sensitive values with tokens where data lands. The lakehouse then holds only tokens, so every engine, share, export and copy carries only tokens. People allowed to can get the values back from the vault, for a stated purpose, audited.

- **Turning it on.** Set `CAIRN_TOKENS_KEY` to 32 random bytes in base64 (`openssl rand -base64 32`). In Kubernetes, pass it from a Secret through `cairn.extraEnv` (`valueFrom.secretKeyRef`); `CAIRN_TOKENS_KEY_FILE` also works. Without a key, tokenisation is off (503), and values are never kept unencrypted.
- **Token domains.** A domain is a kind of value, such as `email`, `iban` or `patient_id`, with its own random keys. Create one with `POST /governance/tokens/domains` and `{"name": "email", "description": "…"}` (`MANAGE_POLICIES`). The domain's keys are kept wrapped by the master key.
- **Tokens.** A token is `tok_` followed by 26 characters, a keyed HMAC of the value.
  - Within a domain, the same value is always the same token, so joins, counts and group-bys still work.
  - Without the key, nobody can find a value's token by trying candidates, as they could with a plain hash of an e-mail address.
  - Tokenised columns are text.
- **Tokenising as data lands.** Give a pipe or a stream `"tokenize": {"email": "email"}` (column to domain). Rows are tokenised before they are written. The upsert key may be tokenised too: the same value finds the same row.
  - Whoever sets the pipe or stream up needs `TOKENIZE` on each domain (`urn:li:tokenDomain:<name>`).
  - Changing `tokenize` on a pipe that has already loaded files leaves those rows as they were.
- **Tokenising through the API.** `POST /governance/tokens/domains/{d}/tokenize` with `{"values": [...]}` (`TOKENIZE`), for applications.
- **Getting values back.** `POST /governance/tokens/domains/{d}/detokenize` with `{"tokens": [...], "purpose": "fraud case 4471"}`.
  - It needs `DETOKENIZE` on the domain, granted by a Cairn policy (`actions: [DETOKENIZE]`, `resource_urns: [urn:li:tokenDomain:email]`).
  - The purpose is required (8 to 500 characters).
  - At most `CAIRN_TOKENS_MAX_BATCH` tokens (1,000) per call. A token the vault does not hold gives `null`.
  - Every call is audited (`token.detokenized`) with who, the purpose and the count. The audit never holds values or tokens, and the answer is not cached.
- **Forgetting (erasure).** `POST /governance/tokens/domains/{d}/forget` with `{"values": [...]}` or `{"tokens": [...]}` (`MANAGE_POLICIES`) deletes their entries from the vault. The tokens stay in the tables, but can never be turned back into the values. Use it for subject erasure requests on tokenised data.
- **Rotating the master key.** Put the new key in `CAIRN_TOKENS_KEY` and the old one in `CAIRN_TOKENS_KEY_PREVIOUS`, then call `POST /governance/tokens/rotate` (`MANAGE_POLICIES`). It re-wraps every domain's keys; tokens and the vault do not change. Afterwards, remove the old key.
- **The vault.** Each value is kept encrypted with AES-256-GCM under its domain's own key, bound to the domain and the token, in Cairn's database (`cairn_token_vault`). Back the database up as usual; without the master key, the backup reveals nothing.
- **Tested** in `tests/test_tokens.py`, on SQLite and PostgreSQL with the real policy decision point:
  - tokens per domain, encrypted storage, privileges and purposes, audit without values, rotation and forgetting.

  Also tested in `tests/test_pipes_e2e.py` and `tests/test_streams_e2e.py`, with the real store and catalog: tokenised files and a tokenised upsert key.

## Aggregation and projection policies

Some tables should only ever be read in aggregate, such as pay, health or survey answers. Some columns can be
used but should never be shown, such as an email that analysts may join or filter on. Both are policy
obligations on reading the table (`VIEW_DATASET_PROFILE`), so they follow the same subjects, conditions and
exemptions as any other policy:

```yaml
- id: pay-in-aggregate
  effect: obligation
  actions: [VIEW_DATASET_PROFILE]
  target:
    resource_urns: ["urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.pay,PROD)"]
    subjects: ["urn:li:corpGroup:analysts"]
  obligations:
    - {type: aggregation, params: {min_group: 5, entity_key: person_id}}
    - {type: projection, params: {columns: [email]}}
```

**Aggregation.** Every query block that reads the table must aggregate: a `GROUP BY`, or aggregates only. It
may not use `*` or window functions, and every column it returns is aggregated or grouped. The aggregates allowed
are the ones that reduce a group to a figure: `count`, `count_if`, `sum`, `avg`, `approx_distinct`, `stddev` and
`variance`. `min`, `max`, `arbitrary`, `array_agg`, `listagg`, percentiles, `min_by`/`max_by` and functions Cairn
does not know are refused anywhere in such a block, `HAVING` and `ORDER BY` included, because they return a row's
own value. Cairn adds
`HAVING count(*) >= min_group` to that block, so no group smaller than `min_group` rows is returned. This holds
in subqueries, CTEs and each branch of a `UNION` too.

With `entity_key`, the floor counts distinct entities instead (`count(DISTINCT <alias>.<key>)`), and the block
may join other tables. Without an entity key, joining in that block is refused: a join can repeat one person's
rows until a group passes the floor. Aggregate the table in a subquery first, or set `entity_key`.

**Projection.** The listed columns may be used in `WHERE`, `JOIN`, `GROUP BY`, `HAVING` and `ORDER BY`, and
counted (`count(email)`). They are never returned: not as a column, not inside an expression such as
`upper(email)` or `max(email)`, and not through a subquery. A `*` over the table is refused.

**Where queries run.** Cairn's query endpoint (`/v1/data/query`) checks the SQL against these policies. It
serves the SQL editor, dashboards and ask-your-data. A query that breaks a policy is refused with the reason
and audited (`data.query_denied`). A query that passes is rewritten with the floors and run:

- On a Trino connection, it runs as a one-query identity, `checked-<id>`. That identity is registered for ten
  minutes with the person and the tables the query was checked for, in `cairn_checked_queries`. Trino applies
  the person's own row filters and masks.
- On other connections, Cairn runs the rewritten SQL itself.

Direct Trino access (JDBC, BI tools, notebooks) refuses any read of an aggregation-protected table. It also
refuses any read that names a projection-protected column. Trino's access control sees which tables and
columns a query reads, not the shape of the query, so those queries go through Cairn.

Row previews of such tables are refused. A misconfigured policy refuses rather than allows, for example a
`min_group` below 2 or an aggregation policy without columns.

`checked-` (like `cleanroom-`) is a reserved user name: nobody signs in as one, and a checked identity Cairn
did not register, or one that has expired, reads nothing.

The floor bounds how small a group can be. It does not stop someone from comparing two large groups that
differ by one person: that is what differential privacy in clean rooms is for.

## Consent

Purposes that need consent, and each person's consent, are kept by Cairn (`cairn_consent_purposes`,
`cairn_consents`; the privacy officer's guide is *Governance → Consent*). What operators need to know:

- **Purposes** are made with `POST /cairn/api/governance/consent/purposes` and changed with `PUT .../purposes/{id}`. A new purpose whose name gives an existing id is refused (409), so it cannot take over another purpose's members and records.
- **Identity for the consent platform.** Give it a service identity with `MANAGE_CONSENT` only. It posts records
  to `/cairn/api/governance/consent/purposes/{id}/records`, up to 10,000 subjects per call.
- **Where filters run.** Trino applies them as row filters through Cairn's access control. On direct warehouse
  connections Cairn rewrites the query. SQLite gets a hash function from Cairn on each connection. The engines with
  a hash Cairn knows are Trino, PostgreSQL, DuckDB, Snowflake, Redshift, BigQuery, Databricks, MySQL and SQLite; on
  any other engine, members of a purpose get no rows from a dataset holding that kind of identifier.
- **Size.** A filter carries the purpose's records inline, up to 10,000 (withdrawals for `opt_out`, grants for
  `opt_in`). Past that, members get no rows from the affected datasets until it is reduced. The purpose shows
  `filter_state: over_limit` and the API logs a warning.
- **Freshness.** A new record applies to queries within 5 seconds on each API node.

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

## Workload identity

Pipelines (dbt, Airflow, Spark) can sign in with the identity their platform already gives them, instead of a stored personal access token. That identity is a Kubernetes service account's projected token, or a CI job's OIDC token. Cairn exchanges it for a short-lived Cairn token of the pipeline user an administrator bound it to, so there is no token to store, copy or rotate.

- **Trusted issuers** (`CAIRN_WORKLOAD_ISSUERS`, a JSON list; Helm `cairn.workloadIdentity.issuers`). Each issuer has:
  - `name`;
  - `issuer`: the tokens' `iss`, exactly;
  - `audience`: one of its own, such as `cairn`. The cluster's default audience is refused, so ordinary service-account tokens never qualify;
  - its keys: `jwks_url` (HTTPS, with `ca_file` if needed) or `jwks_file`. Managed Kubernetes services publish their OIDC issuer and keys, and so does GitHub Actions. On your own cluster, bind `system:service-account-issuer-discovery` so the keys can be read.

  Two limits apply. `max_token_lifetime` (24 hours) refuses long-lived tokens, such as Kubernetes' legacy secret tokens, which never expire. `token_ttl` (an hour) is the Cairn token's lifetime.
- **Bindings** (`/v1/workload-identity/bindings`, `MANAGE_SYSTEM_OPERATIONS`). A binding maps an issuer and a token subject (`system:serviceaccount:pipelines:svc-dbt`, a CI `sub`) to one Cairn user. Optional claims must match too, for example `{"kubernetes.io.namespace": "pipelines"}` or `{"ref": "refs/heads/main"}`. Creating and deleting a binding is audited in the same transaction (`workload.binding_created`, `workload.binding_deleted`).
- **The exchange** (`POST /v1/auth/workload-token`, OAuth 2.0 token exchange). Cairn checks the token's signature, issuer, audience, expiry and lifetime. Exactly one user must be bound, and that user must not be suspended. The Cairn token then works wherever a personal access token does, as that user: Trino tokens, lakehouse sessions, dbt run records. Each exchange is audited (`workload.token_issued`). Anything else is refused. If the issuer's keys cannot be fetched, the exchange answers 503.
- **In the pipelines.** Set `CAIRN_WORKLOAD_TOKEN_FILE` to the token's path (`cairn dbt`, `cairn-spark`), or put `workload_token_file` in the Airflow `cairn` connection's extra. The file is read again at each exchange, since the kubelet rotates it. `cairn dbt` exchanges again before recording a run, so a long build never records with an expired token. Helm's `cairn.dbt.jobs[].workloadIdentity: {serviceAccount: svc-dbt}` runs the job as that service account with a projected token for audience `cairn`. `deploy/airflow/values-cairn.yaml` mounts one in every Airflow pod.
- **Tested** (`tests/test_workload_identity_e2e.py`, against a real Kubernetes API server):
  - **The exchange.** A token the cluster issued for `pipelines/svc-dbt` (audience `cairn`) was exchanged for `svc-dbt`'s Cairn token. With it, `svc-dbt` got a Trino token and a lakehouse session, and `cairn dbt`'s and `cairn-spark`'s exchanges worked.
  - **Refusals.** These were refused: the cluster's default audience, an unbound service account, the same name in another namespace, a forged token, a 48-hour token, a suspended user, and a deleted binding.
  - **Admin only.** Non-administrators could not bind.

## AI agents

AI agents act on behalf of a person, never with permissions of their own. What an agent may do is
that person's access, narrowed by the agent's scopes:

| Scope | Allows |
|---|---|
| `read` | Reading the catalog: every read, and requests that change nothing |
| `query_data` | Previewing tables, running read-only SQL, asking questions of data (masked as for the person) |
| `propose` | Creating things that wait for review: documentation drafts, workflows, access requests, discussions |
| `write` | Changing metadata directly |

Administration (`/admin`, licences, policies, other agents) is never reachable with an agent
credential. Every audit entry made through an agent records it as `details.agent`.

**AI tools people connect** (Claude Desktop, Claude Code, IDEs) sign in with OAuth 2.1, as the MCP
specification defines:

1. The tool registers itself.
2. The person signs in as usual and sees a consent page listing the scopes; they can untick any.
3. The tool receives a one-hour access token and a refresh token, which rotates on each use.

A refresh token or sign-in code used twice ends the whole grant. People see and disconnect their
agents in **Settings → AI agents**.

`CAIRN_OAUTH_REGISTRATION` controls registration:

- `open` (default): new tools can connect straight away.
- `admin`: new tools wait until an administrator enables them.
- `off`: only agents an administrator created.

**Automations** get an agent key instead. In **Settings → AI agents → New automation key**, choose
a name, the person it acts as, and its scopes. The key (`ca_…`) is shown once; **New key** rotates
it. To make an agent depend on the AI system register, link it to an AI system
(`PATCH /admin/agents/{id}` with `ai_system`): it stops working whenever that system is not approved.

**Controlling agents** (Settings → AI agents, for administrators):

- **Pause all agents** is the kill switch. Every agent credential is refused, with the reason, until
  you resume them. People are unaffected.
- **Quotas:** each agent has a limit per minute (default 120) and per day (default 20,000). Over a
  limit it gets `429` with `Retry-After`. Requests refused this way do not count against the daily
  quota. The list shows each agent's requests today.
- **Autonomy:**
  - *Suggestions wait for review* (the default).
  - *Apply confident suggestions*: an agent's suggestion at or above the confidence you set applies
    at once, only if the person it acts for may make that change; otherwise it waits like any other.
    Auto-applied suggestions show as *auto accepted* in the AI steward history and can be undone
    there.

**Intents (intent-based security):** scopes say what an agent may ever do; an intent narrows one run or one
credential to what the task in hand needs. An intent is
`{"purpose": "...", "actions": [...], "tables": [...], "domains": [...]}`:

| Category | Covers |
|---|---|
| `read` | catalog reads (every GET, and requests that change nothing) |
| `query` | previews, SQL, ask-your-data, metric queries; SQL whose statements are `SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `EXPLAIN` |
| `propose` | things that wait for a person's review (documentation drafts, workflows, access requests, discussions) |
| `write` | direct changes; SQL `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`, `ALTER`, `COPY` … |
| `share` | shares, clean rooms, grants, publishing, the outbox, embedding, artifact sharing, reverse syncs |
| `export` | exports, downloads, detokenizing; SQL `UNLOAD`, `INTO OUTFILE`, writes to a storage location |
| `model` | the AI gateway |
| `admin` | administration; SQL `GRANT`, `REVOKE`, and any statement Cairn does not recognise |

- `tables` are dataset urns or name patterns (`finance.public.*`, matched against the dataset's name); `domains` are
  domain urns. With either set, a request naming a dataset in neither (a path or query parameter, the tables a SQL
  statement reads, a tool's arguments) is refused.
- **Every agent request is classified**: HTTP by method and route, SQL (`POST /data/query`) by its statement kinds
  (the most powerful wins), MCP and assistant tools by the tool. A request outside the intent gets **403** with the
  reason and is audited as `agent.intent_denied` (category, reason, route or tool, dataset, run, the intent). The
  scope check comes first and is unchanged.
- **Where intents come from:** an agent app invocation may pass `intent` (`POST /v1/agent-apps/<name>/invocations`
  with `{"input", "intent"}`); it rides on the run's short-lived token, so the app's own model calls, SQL and API
  requests are held to it (an app that calls a model needs `model`). A custom agent declares `intent` in its
  definition (changing it takes a deployed agent back to draft). An administrator may give a key agent one with
  `PATCH /admin/agents/{id}` `{"intent": {...}}` (`{"intent": {}}` removes it). OAuth tokens carry none.
- **Credentials without an intent** work as before, unless you require intents:
  `PUT /cairn/api/v1/agent-intents/settings {"required": true}` (`MANAGE_GLOBAL_SETTINGS`, audited as
  `agents.intents_required` / `agents.intents_optional`). Then every agent request without one is refused, agent app
  invocations without one are refused up front, and custom agents without one do not run (fail closed).
- **Denials:** Agents → *Intent denials*, or `GET /cairn/api/v1/agent-intents/denials?days=7`: administrators and
  operators see all; people see those made for them or by the agents they own. `POST /v1/agent-intents/classify`
  with `{"sql": ...}` or `{"method", "path"}` shows how a request would be classified.

**Seeing what agents do:**

- Each agent's **Activity** lists its recent tool calls: tool, arguments (personal data redacted),
  outcome, time taken, and whether catalog text in the result was flagged.
- **Datasets read**, next to an agent's activity, lists the datasets the agent read (data and metadata) over the last 30 days (`GET /cairn/api/admin/agents/{id}/datasets`). People see the same for the agents connected to them.
- Administrators see every agent's activity. People see what agents did for them, in Settings →
  AI agents.
- `/admin/agents/stats` sums calls, errors, refusals and flagged text per agent and per tool over a
  day. Prometheus gets `cairn_agent_tool_calls_total`, `cairn_agent_tool_seconds` and
  `cairn_ai_injection_flagged_total`; the alerts `CairnAgentErrorsHigh` and `CairnInjectionFlagged`
  watch them.
- **Feedback:** people rate answers from the assistant and from *Ask your data* with a thumbs up or
  down. `GET /ai/feedback` summarises the ratings, and `GET /ai/feedback/eval-cases` exports the
  down-voted questions as `cairn ai eval` cases. Fill in what a right answer must contain and add
  them to the eval suite, so mistakes people saw stay fixed.

**Agent trace intelligence (what the traces say):** Agents → *Agent insights* and *Agent findings* analyse each
agent's recorded tool calls and model use over a window.

- `GET /cairn/api/v1/agent-traces/insights?days=7` (1 to 30 days) gives, per agent: runs, tool calls, errors,
  refusals, error rate, average and p95 latency, slow calls, loops, model calls, tokens and estimated cost, and cost
  per run, with the agent's own baseline. `GET …/insights/{agent id}?days=7` adds a per-tool breakdown, its runs
  (loops first), each loop's (redacted) arguments and a daily series.
- **Runs.** A custom agent's run, an agent app run's short-lived token and an OAuth access token's session each count
  as a run; every traced tool call records the run it belongs to. Calls made with an agent key have no run of their
  own and are grouped into sessions per person, split after 30 idle minutes.
- **Loops** are the same tool with the same arguments repeated within one run at least `loop_threshold` times
  (default 4). **Slow calls** take `slow_call_ms` (default 10 000) or more.
- **Model cost** comes from AI usage metering: tasks `gateway:<agent id>` (agent keys and agent apps through the AI
  gateway) and `agent.<name>` (custom agents).
- **Outliers** compare the window with the agent's own history only: the period before it (up to three windows,
  within the 30 days traces are kept). Calls per day and model cost per day are flagged when they are at least
  `outlier_factor` (default 2) times the agent's daily mean and three standard deviations above it; p95 latency,
  calls per run and the error rate when they are at least `outlier_factor` times the baseline. An agent with too
  little history gets an `info` finding saying so.
- **Findings** have a kind (`loop`, `error_rate`, `refusals`, `slow_calls`, `injection_flagged`,
  `volume_outlier`, `cost_outlier`, `latency_outlier`, `calls_per_run_outlier`, `error_outlier`, `no_baseline`) and a
  severity: `critical` (a loop at twice the threshold, an error rate at or over `error_rate_critical`, an outlier at
  twice the factor), `warning` or `info`. They are listed most severe first.
- **Who sees it.** Administrators (`MANAGE_GLOBAL_SETTINGS`) and operators (`MANAGE_SYSTEM_OPERATIONS`) see every
  agent; anyone else sees only the agents they own, and others' agents answer 404. Agent credentials are refused (403).
- **Thresholds.** `GET|PUT /cairn/api/v1/agent-traces/settings` with any of `loop_threshold`, `slow_call_ms`,
  `outlier_factor`, `min_calls` (calls needed before rates and latency are judged, default 10),
  `error_rate_warning` (0.2) and `error_rate_critical` (0.5). Changing them needs `MANAGE_GLOBAL_SETTINGS` and is
  audited as `agent_traces.settings_changed`. Reading insights changes nothing.

**Agentic abuse detection (prompt injection and exfiltration):** rule-based detectors run wherever text crosses into
or out of a model on an agent's behalf, record findings, block the serious ones and pause an agent that keeps
tripping them (`cairn.ai.abuse`, table `cairn_agent_abuse_findings`, migration 0129).

- **What is checked.**
  - *Into models:* every message of an AI gateway call (agent apps' model calls included) and every tool result a
    custom agent's tools return, for **injection**: text telling an AI to ignore its instructions or policies, to
    send data somewhere, to call tools, or hidden in invisible characters. In a tool result (data someone may have
    planted) it is `critical`; in a person's own message `warning`.
  - *Out of models and into tools:* gateway answers (plain and streamed) and custom agents' answers and tool calls,
    for **secrets** (cloud keys, API keys, tokens, private keys, passwords: `critical`), **PII dumps**
    (`pii_dump_threshold` values or more of the kinds masking protects, found with the column classifier's validated
    value detectors: `critical`; a third of that: `warning`), **destinations** (an http(s) URL anywhere in a tool's
    arguments, or an email address given as a recipient, outside `allowed_domains`: `critical`) and, at the gateway,
    **tools out of scope** (the model asking for a tool the request did not offer: `critical`).
  - *MCP:* tool arguments are checked for destinations (a blocked call answers `{"error": "blocked"}`). Instructions
    in what an MCP tool returns are already flagged in place before they leave Cairn, so they are recorded as a
    `warning` finding for the agent rather than blocked.
- **Blocking.** A finding at or above `block_at` (`critical` by default; `warning` blocks more) stops it: the gateway
  answers 403 in the provider's error format without calling the provider (inputs) or without returning its answer
  (outputs; an agent's streamed answer is held until it has been checked, a person's is passed through), a custom
  agent's run stops with status `blocked` before the model sees the tool result or before the tool runs.
- **Auto-pause.** When an agent reaches `auto_pause_after` (3; 0 never) open critical findings within
  `window_hours` (24), it is disabled in the agent hub: its key and tokens stop working at once and its runs are
  refused, until an administrator enables it again. Audited `agent_abuse.auto_paused`.
- **Model judge (optional).** `CAIRN_ABUSE_JUDGE_URL`: Cairn posts `{"direction", "text"}` and reads
  `{"severity", "reason"}`; the judge can add a finding the rules missed, never remove one, and when it cannot be
  reached the rules' verdict stands.
- **Findings:** `GET /cairn/api/v1/agent-abuse/findings?agent=&severity=&status=&limit=` (agent, person, run,
  source, detector, severity, blocked, a redacted summary: never a secret or the data), `POST …/findings/{id}/dismiss`
  (dismissed findings no longer count towards auto-pause). Administrators and operators see every agent's findings,
  owners their agents'; agent credentials are refused. The agent insights carry each agent's counters (`abuse`:
  critical, warning, blocked) and an `abuse` finding.
- **Settings:** `GET|PUT /cairn/api/v1/agent-abuse/settings` with `enabled`, `block_at`, `auto_pause_after`,
  `window_hours`, `pii_dump_threshold` (10) and `allowed_domains` (`["corp.example.com"]`; subdomains included).
  Changing them needs `MANAGE_GLOBAL_SETTINGS`. `POST …/scan` with `{"text", "direction": "input" | "output"}` is a
  dry run for administrators and operators.
- **Audited** (category `agents`): `agent_abuse.finding` (detector, severity, blocked, run; no content),
  `.auto_paused`, `.dismissed`, `.settings_changed`.

Tested end to end (`tests/test_agent_abuse.py`): through the AI gateway with a provider stand-in and an agent key,
planted instructions in a tool result blocked before the provider, a tool call to a domain outside the allowlist
blocked (an allowlisted one passing), a streamed PII dump blocked and the third critical finding pausing the agent
(its key then refused), a person's answer carrying a secret blocked; through a custom agent, a quality monitor named
to instruct agents stopping the run before the model saw it, a suggestion linking out stopped before it ran, and the
agent paused; the findings API, dismissal, settings, the insights' counters and the audit.

**Custom agents (built in Cairn):** a team builds an agent from Cairn's own tools, deploys it, and people run it.

- `POST /cairn/api/v1/custom-agents` with `{"name", "description", "instructions", "tools": [...], "scopes": [...],
  "team": "urn:li:corpGroup:…", "ai_system": "...", "max_steps": 8}`. The tools are the assistant's (search, lineage,
  quality, ask-your-data, suggestions and the rest); the scopes are `read`, `query_data` and `propose` (built agents
  never get `write`), and every tool's scope must be among them.
- Building one needs `BUILD_AGENTS`. Its owner changes, deploys (`POST …/{name}/deploy`), undeploys and deletes it.
  It is run (`POST …/{name}/run` with `{"input": "..."}`) by its owner, members of its `team`, and people granted
  `USE_AGENT` on `urn:li:cairnAgent:<name>`; for anyone else it does not exist (404).
- Only a deployed agent runs. Changing a deployed agent's instructions, tools, scopes, steps or AI system takes it
  back to draft until its owner deploys it again; `version` and `deployed_version` show which is live.
- A run is made **as the person who starts it**, through an agent identity registered in the agent hub (kind
  `built`, no key or sign-in of its own): their permissions, narrowed to the agent's scopes and tools. A tool it was
  not given, or one outside its scopes, is refused and the model is told so; the run is marked `refused`.
- The administrators' pause switch, the agent's request quotas and, when `ai_system` is set, that system's approval
  in the AI register all apply: an agent linked to an unapproved system cannot be deployed or run. The AI budget and
  rate limits apply too (429 when spent).
- Each tool call appears in the agent's **Activity** like any other agent's; `GET …/{name}/runs` lists runs (input,
  answer, tools called, status). The owner sees every run; others see their own. Building, changing, deploying,
  running and deleting are audited.
- Runs need a model that runs tools (`CAIRN_LLM_PROVIDER=anthropic`); without one, `run` answers 503.

## The assistant's memory, artifacts and deep research

The assistant keeps three things for each person. Agents and service accounts have none of them.

**Memory.** These are short notes the assistant reads at the start of each conversation, such as "reports revenue
in EUR, fiscal year starts in April". It saves a note only when the person asks it to, or states a lasting
preference, and every saved note is listed in the reply.

- People see and delete their notes at `GET /ai/memory` and `DELETE /ai/memory/{id}` (or `DELETE /ai/memory` for
  all of them). They add notes themselves with `POST /ai/memory`.
- `PUT /ai/memory/settings {"enabled": false}` switches memory off. The assistant then neither reads nor saves
  notes.
- Each note is at most 500 characters, and each person has at most 50. PII is redacted before a note is stored.
- A note that reads like instructions to an AI is refused and audited (`assistant.memory.refused`). This is what a
  poisoned tool result would try to plant.
- Notes go to the model inside a `<user_memory>` wrapper, as background data. They never override the
  assistant's rules.

**Artifacts.** These are outputs worth keeping: markdown, a table, a chart spec (JSON), a SQL query, or a research
report. The assistant saves one when asked (`save_artifact`); people can also create one with
`POST /ai/artifacts`.

- Each edit (`PATCH /ai/artifacts/{id}`, with `expected_version`) makes a new version. The last 20 versions are
  kept and readable with `?version=`.
- An artifact remembers the datasets it was made from: those the conversation read data from, and any dataset
  urn cited in its content.
- `POST /ai/artifacts/{id}/share {"add": [...], "remove": [...]}` shares it with users or groups.
- Anyone other than the owner can open it only if, at that moment, they hold `VIEW_DATASET_PROFILE` on every
  source. If not, they get 403. If an access decision cannot be made, the answer is also 403.
- People it is not shared with get 404. Only the owner edits, shares or deletes.

**Deep research.** `POST /ai/research {"question": ..., "max_steps": 1-6}` returns 202 and runs in the
background, in three stages:

1. The model plans the sub-questions.
2. It works through each one with the read-only tools, as the person who asked: search, lineage, quality,
   knowledge, metrics and `ask_data`. It cannot suggest changes, request access or save anything.
3. It writes a report, which is saved as a `report` artifact with citations.

`GET /ai/research/{id}` shows the plan and each step's finding and tool calls as they happen. A run:

- can be stopped between steps with `POST /ai/research/{id}/cancel`;
- stops with `failed` when the AI budget is spent;
- counts towards the person's AI usage (`assistant.research`).

Each person may have two runs at a time. The scheduler marks a run that a restart left `running` for over two
hours as failed.

Deep research needs the Anthropic provider. Without it the endpoint answers 503.

Everything above is audited: `assistant.memory.*`, `assistant.artifact.*` and `assistant.research.*`.

## AI gateway: model calls from code people and agents write

Agent code written outside Cairn (LangGraph, the OpenAI Agents SDK, the Claude Agent SDK, plain scripts) calls models
through Cairn instead of holding a provider key. It uses the providers' own SDKs, pointed at Cairn, with a Cairn
token:

```python
anthropic.Anthropic(base_url=f"{cairn}/v1/ai/gateway/anthropic", api_key=cairn_token)
openai.OpenAI(base_url=f"{cairn}/v1/ai/gateway/openai/v1", api_key=cairn_token)
```

- **Endpoints:** `POST /v1/ai/gateway/anthropic/v1/messages` (Anthropic), `POST /v1/ai/gateway/openai/v1/chat/completions`
  (OpenAI or any OpenAI-compatible server: vLLM, Ollama, a model Cairn serves), `GET /v1/ai/gateway/models` (the
  models the caller may use). Streaming is passed through as it arrives.
- **Upstreams** (the API's environment): `CAIRN_AI_GATEWAY_ANTHROPIC_KEY` (or `…_FILE`) and optionally
  `CAIRN_AI_GATEWAY_ANTHROPIC_URL`; `CAIRN_AI_GATEWAY_OPENAI_URL` and `CAIRN_AI_GATEWAY_OPENAI_KEY` (or `…_FILE`).
  The provider only ever sees Cairn's key; callers never hold one. With neither configured, the gateway is off.
- **Who may call:** a person's Cairn token, or an agent key (`ca_…`) acting for its owner, which needs the agent
  scope `model` (the agent hub's pause switch and quotas apply). Each call needs `USE_AI_GATEWAY` on the model
  (`urn:li:aiModel:<model>`), and the model must be listed in `CAIRN_AI_GATEWAY_MODELS` (nothing listed: every model
  is refused).
- **Limits:** the deployment's daily AI budget (`CAIRN_AI_DAILY_BUDGET_USD`) and each person's daily token allowance
  (`CAIRN_AI_USER_DAILY_TOKENS`), shared with Cairn's own AI features, and `CAIRN_AI_GATEWAY_RPM` calls a minute per
  person. A refused call never reaches the provider, and the SDK sees the provider's own error type.
- **Metering:** every call's tokens and estimated cost, streamed ones included, are recorded under the person, as
  feature `gateway` or `gateway:<agent id>`, in the AI usage report and the audit log. Prompts and answers are not
  stored.

Tested through the real services, decision point, database and HTTP API with the official Anthropic and OpenAI SDKs
against a local stand-in for the providers (`tests/test_ai_gateway.py`): plain and streamed calls on both protocols,
the provider seeing only Cairn's keys, metering per person and agent, refusals (a model not served, no
`USE_AI_GATEWAY`, no sign-in, the rate limit, the token allowance) before the provider, and agent keys without the
`model` scope refused.

### Routes: virtual models with rules and fallback

A **route** is a model name callers use (`"model": "cairn-auto"`) that stands for an ordered list of real models,
the candidates (`cairn.ai.gateway_routes`, table `cairn_ai_gateway_routes`, migration 0129).

- **More upstreams.** Besides the Anthropic and OpenAI upstreams, `CAIRN_AI_GATEWAY_UPSTREAMS` names more:
  `backup=anthropic@https://…,local=openai@http://…`, each one's key in `CAIRN_AI_GATEWAY_UPSTREAM_<NAME>_KEY` (or
  `…_FILE`; for `backup`, `CAIRN_AI_GATEWAY_UPSTREAM_BACKUP_KEY`). `CAIRN_AI_GATEWAY_MODEL_UPSTREAMS`
  (`model=backup,…`) says which upstream serves a model; other models go to the upstream of the protocol they are
  called on. A model served by an upstream of the other protocol is not reachable on this one.
- **Defining routes** (`MANAGE_SYSTEM_OPERATIONS`; agent credentials refused; audited `gateway.route_saved`,
  `gateway.route_deleted`): `PUT /v1/ai/gateway/routes/{name}` with
  `{"description", "candidates": [{"model", "capabilities": ["tools", "vision"], "max_input_tokens", "timeout_s"} | "model", …],
  "strategy": "ordered" | "cheapest", "max_cost_usd", "latency_budget_ms"}`; `GET /v1/ai/gateway/routes` (anyone
  who may call the gateway); `DELETE /v1/ai/gateway/routes/{name}`. Candidates must be served models (up to 10); a
  route cannot take a served model's name.
- **Rules, per call.** A candidate is skipped when the caller may not use it (`USE_AI_GATEWAY` on its
  `urn:li:aiModel:<model>`), when the request uses tools or images it does not support, when the request's input
  (estimated at four characters a token) is over its `max_input_tokens`, or when the request's estimated cost (input
  plus `max_tokens` of output, at `CAIRN_AI_PRICES`) is over the route's `max_cost_usd`. `cheapest` orders the rest
  by that estimate. With none left the call is refused before any provider: 403 when the caller may use none of
  them, otherwise 422 naming why each was skipped.
- **Fallback.** The candidates are tried in turn: a provider error (5xx), a timeout (the candidate's `timeout_s`, and
  what is left of the route's `latency_budget_ms`, which covers every attempt), an unreachable provider or a rate
  limit (429) moves on to the next; any other answer (a 400, say) is returned. Streams fall back before their first
  byte. When every candidate fails, the error lists each attempt; a spent latency budget answers 504.
- **Metering and the answer.** The call is metered on the model that answered (the AI usage report, the budget and
  the person's allowance as for any call), and the response names it: `X-Cairn-Model` and `X-Cairn-Route` headers
  and the provider's own `model` field. Each routed call is audited `gateway.routed` with the route, the model and
  every attempt (model, status, outcome, milliseconds, or why it was skipped), never the content.
- **`cairn-auto`** is built in: every served model in `CAIRN_AI_GATEWAY_MODELS` order, falling back on errors,
  timeouts and rate limits. Defining a route called `cairn-auto` replaces it. `GET /v1/ai/gateway/models` lists the
  routes a person may use (`cairn_route: true`, with the candidates open to them).

Tested through the HTTP gateway with the Anthropic SDK and two provider stand-ins, the primary failing as told
(`tests/test_ai_gateway_routes.py`): fallback on 429, 500, a timeout and before a stream, not on a 400; metering
and the response naming the model that answered; candidates the caller may not use skipped without reaching the
provider and none left refused; the cost ceiling, cheapest first, tools, input size and the latency budget; the
built-in `cairn-auto`; the operator API.

## Code-first agents: `cairn agents init`, `dev`, `deploy`

From an empty directory to a deployed agent, with the runtime, model access, sessions and governance already in place:

```bash
cairn agents init triage --template basic        # or openai-agents, langgraph
cd triage && pytest tests                         # offline: the model is a local stand-in
export CAIRN_URL=https://cairn.example.com CAIRN_TOKEN=…   # your Cairn token, from the environment only
cairn agents dev                                  # http://127.0.0.1:8765/ : a chat page
cairn agents deploy                               # Cairn runs it as an agent app
cairn agents invoke triage "Which tickets are open?"
```

- **The project.** `agent.py` defines `respond(ctx, messages)` and returns the answer. `ctx.anthropic()` and
  `ctx.openai()` are the providers' SDK clients already pointed at Cairn's AI gateway with the right token, and
  `ctx.gateway` gives the base URLs and key for frameworks that take them (LangGraph, the OpenAI Agents SDK).
  `ctx.sql(...)` runs SQL through Cairn as the person; `ctx.user` and `ctx.session_id` say who is asking.
  `agent.toml` names the agent, its model and its scopes (`model`, `query_data`, `read`, `propose`). There are
  three templates: `basic` (Claude with a SQL tool), `openai-agents` and `langgraph`. Each comes with a test that
  runs offline against a stand-in model (`cairn.agentkit.testing.FakeContext`) and with a chat page.
- **dev** runs the project in `cairn.agentkit.runtime` on your machine. Every model and data call goes through Cairn
  as you. Sessions are kept in the project's `.cairn/`.
- **deploy** (`POST /v1/agent-apps`, `BUILD_AGENTS`) sends the project, without `.cairn/`, caches or virtualenvs, up
  to `CAIRN_AGENT_APPS_MAX_BUNDLE` (5 MB). Cairn registers the app in the agent hub (kind `app`, its scopes, no key)
  and starts it. A redeploy by its owner makes a new version.
- **Runtime:** `CAIRN_AGENT_APPS_RUNTIME=process` runs each app as a local process on the API's host, on 127.0.0.1,
  with a secret Cairn gives it in a file, so only Cairn reaches it. It runs in Cairn's Python environment: a deploy
  whose `requirements.txt` names a package that is not installed is refused, naming it. `CAIRN_AGENT_APPS_CAIRN_URL`
  is the address the runtime calls Cairn back on, and `CAIRN_AGENT_APPS_DIR` is where projects are unpacked.
- **On Kubernetes** (`CAIRN_AGENT_APPS_RUNTIME=kubernetes`; chart: `cairn.agentApps.runtime: kubernetes`), each app is
  a Deployment of one pod in `cairn.agentApps.kubernetes.namespace` (`cairn-agents`), with a Secret, a Service and a
  NetworkPolicy, labelled `cairn.io/agent-app: <name>`. The chart gives Cairn's API a Role there for those objects
  (and to list pods) and nothing else.
  - The pod runs `cairn.agentApps.kubernetes.image`, built from `docker/agent-runtime.Dockerfile`: Cairn's agent
    runtime with the Anthropic and OpenAI SDKs. For agents that need more (LangGraph, the OpenAI Agents SDK, your own
    libraries), build FROM it, or pass `--build-arg EXTRA_REQUIREMENTS=...`. A deploy whose `requirements.txt` names
    a package the image lacks is refused, naming it, and leaves nothing behind.
  - The project travels in the app's Secret (so a bundle is at most 700,000 bytes on Kubernetes), with the secret
    Cairn sends on every request; neither is in the Deployment. The pod runs as user 10001 without privileges, on a
    read-only root file system, without a Kubernetes token, and holds no credential of its own: each run brings the
    asking person's 15-minute token.
  - Only Cairn reaches the pods: by default Cairn's API pods in this release's namespace
    (`cairn.agentApps.kubernetes.ingressFrom` to change). The pods call Cairn back at `cairn.agentApps.cairnUrl`
    (default: the API Service); with `cairn.networkPolicy.enabled`, the API's policy admits them.
  - `access: node-port` (with `nodeAddress`) is for a Cairn outside the cluster. A redeploy replaces the pod
    (`Recreate`): two versions never answer side by side. A deploy that does not become ready within
    `CAIRN_AGENT_APPS_K8S_START_TIMEOUT` seconds (180) is refused with the reason (the pod's termination message,
    an image pull error, or why it cannot be scheduled), and the app stays down until a deploy works.
  - Any Cairn replica serves any app: the address and secret are read from the cluster.
- **Invoking** (`POST /v1/agent-apps/<name>/invocations` with `input` and `session_id`) needs to be the app's owner or
  `USE_AGENT` on `urn:li:cairnAgent:<name>`. Each run gets a 15-minute token for the person asking, narrowed to the
  app's scopes and revoked when the run ends. The agent's model calls go through the AI gateway, and its SQL through
  Cairn's query path, as that person: their policies, masks, row filters and budget apply, and usage is metered to
  them as `gateway:<agent id>`. The pause switch and the agent's quotas apply.
- **Sessions:** Cairn keeps each conversation per app, person and session (the last `CAIRN_AGENT_APPS_HISTORY`, 40
  messages) and sends it with each run. People read only their own (`GET …/sessions/<id>`).
- `DELETE /v1/agent-apps/<name>` (its owner or an operator) stops the runtime and removes the app, its identity and
  its sessions. Audited: `agent_app.deployed`, `agent_app.invoked` (no content), `agent_app.deleted`.
- **MLflow:** with `CAIRN_MLFLOW_URL` set, deploying makes an experiment `agents/<name>` in Cairn's MLflow (its id is
  the app's `mlflow_experiment`), and every invocation is a run in it: `mlflow.user` the person who asked, the app
  version and session as tags, `latency_ms`, `input_chars`, `output_chars` and `turns` as metrics, status FINISHED or
  FAILED. No message content is logged. Tracking is best effort: an unreachable MLflow is logged, never fails a run.

Tested through the real services, decision point, database and HTTP API, with the agent's real code in its own process
and the model behind Cairn's AI gateway, a local stand-in for the provider (`tests/test_agent_apps.py`):

- the scaffold's tests offline; dev mode's chat page, sessions and metering as the developer;
- deploy refused without `BUILD_AGENTS` and for a missing package;
- invocations as the caller, metered to them, each run's token revoked, sessions kept apart and their history sent;
  a person without `USE_AGENT` refused;
- a redeploy; the pause switch; delete stopping the runtime and the identity.
- on a real cluster (`tests/test_agent_apps_k8s.py`, kind in CI): the app deployed with only the chart's Role, the
  pod calling Cairn's gateway back as the asker, a pod elsewhere in the cluster refused by the network policy and a
  request without the secret refused, another Cairn replica finding the app, a redeploy replacing the pod, a missing
  package refused by name, Cairn's account refused outside its namespace, and delete removing every object.
- against a real MLflow server: the app's experiment, and each run under the caller's name with its metrics and no
  content (`tests/test_agent_apps_mlflow.py`).

All three templates' own tests passed with the real frameworks (anthropic, openai-agents, langgraph).

## Agent memory

Agents keep chat sessions per conversation; **agent memory** is what an agent keeps across them about the people it
works for: preferences, facts about their work, decisions made (`cairn.ai.agent_memory`, table
`cairn_agent_memories`, migration 0129).

- **Scopes of a memory.** `person` (about one person: written by the agent during a run for them, or by the person),
  `team` (shared with a group's members: written only by a member, by hand) and `shared` (everyone who may use the
  agent: written only by the agent's owner or an operator). An agent's run acts for one person, so an agent writes
  only `person` memories; a run recalls that person's memories, their teams' and the shared ones, and never another
  person's.
- **From agent code:** the agent scope `memory` (add it to `agent.toml`'s `scopes`, or to a key agent's) and
  `ctx.memory.remember(text)`, `ctx.memory.recall(query, k=5)`, `ctx.memory.forget(id)` in `cairn.agentkit`. Over
  HTTP with the run token or key: `POST /v1/agent-memory` `{"content"}`, `POST /v1/agent-memory/recall`
  `{"query", "k"}`, `DELETE /v1/agent-memory/{id}`. Under a declared intent (L109), recalling counts as `read`,
  remembering and forgetting as `write`.
- **Recall** is by meaning and recency: the query and the memories are embedded with the embedder Cairn uses for
  search by meaning (`CAIRN_EMBEDDING_PROVIDER`; the built-in hashing embedder by default), and the score is
  `0.8 × similarity + 0.2 × recency` (recency halves every 30 days since the memory was written or last recalled).
  Memories with no similarity to the query are not returned. Each memory records the run that wrote it
  (`source_run`).
- **Refused content (422, audited `agent_memory.refused` without the content):** values of the kinds masking
  policies protect (emails, SSNs, card numbers, IBANs, phone numbers, IP addresses, found with the column
  classifier's validated value detectors), credentials (cloud keys, API keys, tokens, private keys, passwords) and
  text that reads like instructions to an AI. Keep the fact ("prefers email contact"), not the value.
- **Limits:** `CAIRN_AGENT_MEMORY_MAX_CHARS` (2,000 characters a memory), `CAIRN_AGENT_MEMORY_MAX_PER_SUBJECT`
  (200 per agent and person, team or shared pool: when an agent writes one more, its oldest, least recently recalled
  memory there is evicted, audited `agent_memory.evicted`; a person's own write is refused instead) and
  `CAIRN_AGENT_MEMORY_RETENTION_DAYS` (180: a memory not recalled for that long is no longer recalled and is purged).
- **People decide:** `GET /v1/me/agent-memories[?agent=<id or name>]` lists what agents keep about the caller (and
  the team and shared memories they can see, with `deletable`); `DELETE /v1/me/agent-memories/{id}` and
  `DELETE /v1/me/agent-memories?agent=<id or name>` delete them; `POST /v1/me/agent-memories`
  `{"agent", "content", "scope", "team"}` adds one by hand. Only people reach these routes, under their own
  identity; agent credentials are refused. Deleting an agent app or custom agent deletes its memories.
- **Audited** (category `agents`): `agent_memory.saved`, `.recalled`, `.deleted`, `.refused`, `.evicted`, `.purged`
  with who, agent, scope and run, never the content.

Tested through the real services, decision point, database and HTTP API with a real agent app process remembering
and recalling through its run token (`tests/test_agent_memory.py`): remembered in one session and recalled by meaning
in another for the same person, never for another person; PII, credentials and instructions refused; the person
listing and deleting their memories and nobody else; team and shared memories written only by members and the owner;
the `memory` scope required; eviction at the cap, retention, and deletion with the agent.

## Code sandboxes for agents

Agents (custom agents, agent apps, MCP clients) and people run Python, or SQL over DuckDB, in a sandbox: a container
of its own per run, never Cairn's process.

```bash
curl -X POST "$CAIRN/v1/sandboxes/runs" -H "Authorization: Bearer $TOKEN" -d '{
  "language": "python", "code": "import csv; rows = list(csv.DictReader(open(\"sales.csv\"))); result = len(rows)",
  "files": [{"name": "sales.csv", "content": "<base64>"}],
  "network": {"cairn": true}, "limits": {"timeout_s": 60, "memory_mb": 512, "cpus": 1}}'
```

- **What comes back** when the run ends: `status` (`succeeded`, `failed`, `timeout`, `memory`), `result` (the value
  the code left in a variable named `result`; for SQL the columns and rows), `stdout`, `stderr` (256 KB each), and
  `files`: what the code wrote under `out/`, base64, up to `CAIRN_SANDBOX_MAX_OUTPUT_MB` (5) together. Input `files`
  (up to `CAIRN_SANDBOX_MAX_INPUT_MB`, 20) are copied into the working directory first.
- **For agents:** the `run_python` tool (custom agents can be given it; the assistant and the MCP server offer it; it
  needs the `query_data` scope) and `ctx.run_python(code, files=…, cairn=True)` in agent apps.
- **Runtime.** `CAIRN_SANDBOX_RUNTIME=docker` starts a container per run from `CAIRN_SANDBOX_IMAGE` (build
  `docker/sandbox.Dockerfile`: Python with DuckDB, pandas, pyarrow and numpy; any Python image works for Python runs).
  The container runs as uid 1000 with no capabilities and no new privileges, a read-only root file system, a process
  limit, the run's CPU and memory (no swap) limits, and no network interface (`--network none`). The runner, the code
  and the inputs are mounted read-only; `/work` (the working directory) and `/tmp` are in memory and vanish with the
  run (`CAIRN_SANDBOX_WORK_MB`, 256). `CAIRN_SANDBOX_DOCKER_RUNTIME=runsc` runs it under gVisor where installed.
  `CAIRN_SANDBOX_RUNTIME=kubernetes` makes a pod per run in `CAIRN_SANDBOX_K8S_NAMESPACE` (`cairn-sandboxes`), with
  `CAIRN_SANDBOX_K8S_RUNTIME_CLASS` if set: the program and inputs in a Secret (700 KB of inputs at most), the token in
  another, no service account token, the same security context, and a NetworkPolicy, made before the pod, that denies
  all traffic. Cairn's account needs to create and delete pods, secrets and network policies there and read pods and
  their logs. Without a runtime, runs are refused (503).
- **Network: none by default.** A run that asks for network (`network.cairn` or `network.allow`) gets one way out: an
  egress proxy Cairn runs for that run (`HTTP_PROXY`/`HTTPS_PROXY` in the sandbox). With docker it is a Unix socket
  mounted into the container, the container still having no network interface; on Kubernetes it is
  `CAIRN_SANDBOX_K8S_PROXY_LISTEN` on every API replica (reached at `CAIRN_SANDBOX_K8S_PROXY_URL`, the sandboxes'
  NetworkPolicy allowing only `CAIRN_SANDBOX_K8S_PROXY_PEERS`), each run with credentials signed with
  `CAIRN_SANDBOX_PROXY_KEY`. The proxy lets through only the hosts in `network.allow`, each of which must be in
  `CAIRN_SANDBOX_ALLOWED_HOSTS` (an operator's list; a request naming another host is refused before anything
  runs), and with `network.cairn`, Cairn itself as `http://cairn` (`CAIRN_SANDBOX_CAIRN_URL` says where Cairn really
  is; `CAIRN_SANDBOX_CAIRN_CA_FILE` for a private certificate). Names are resolved by the proxy, and a host resolving
  to a private, loopback or link-local address is refused unless `CAIRN_SANDBOX_ALLOW_PRIVATE_EGRESS=true`. A run's
  traffic is capped at `CAIRN_SANDBOX_MAX_EGRESS_MB` (100).
- **Data as the person, governed.** With `network.cairn`, the run gets `CAIRN_URL` (`http://cairn`) and
  `CAIRN_TOKEN`: a token for the person who asked, through the sandbox's own agent identity (`agt_cairn_sandbox`,
  made on first use), narrowed to `read` and `query_data` and, when an agent asked, to that agent's scopes and
  declared intent. It lasts the run and is revoked when it ends. The code queries Cairn (`POST $CAIRN_URL/data/query`)
  and gets exactly what the person would: masked columns masked, row filters applied. The pause switch for agents
  stops it. The token reaches the container through an env file (docker) or a Secret (Kubernetes), never a command
  line.
- **Who and how much.** Running code needs `RUN_CODE`. Each person may have `CAIRN_SANDBOX_MAX_PER_PERSON` (2) runs
  at once and Cairn `CAIRN_SANDBOX_MAX_RUNNING` (8); more are refused with 429. A run's limits are capped by
  `CAIRN_SANDBOX_MAX_TIMEOUT_S` (600), `CAIRN_SANDBOX_MAX_MEMORY_MB` (2048) and `CAIRN_SANDBOX_MAX_CPUS` (2); the
  defaults are 60 s, 512 MB and one CPU. A run over its time is stopped (`timeout`), one over its memory killed
  (`memory`). `GET /v1/sandboxes/limits` shows the settings.
- **Recorded, metered, audited.** `GET /v1/sandboxes/runs` and `/runs/<id>` (your own; operators see all) give each
  run's person, agent, language, limits, network, status, duration and sizes, never the code or its output, kept
  `CAIRN_SANDBOX_KEEP_DAYS` (30). `GET /v1/sandboxes/usage?days=30` sums runs, CPU seconds, memory (MB × seconds),
  egress and outcomes per person and day. Each run is audited as `sandbox.run_<status>`.

Tested end to end with real containers started by the real API (`tests/test_sandboxes_e2e.py`, in CI's trino job with
the `cairn/sandbox` image): a computation over an input file returning its result and a generated file, as uid 1000
on a read-only root; no network (no interface for a direct connection; the proxy refusing any host but Cairn, and a
host an operator has not allowed refused before the run); a time limit and a memory limit enforced; a query from
inside the sandbox through Cairn seeing a masked column masked for the person and unmasked for an administrator, with
each run's token revoked; `RUN_CODE`, the per-person cap, records, usage and audit; the `run_python` tool within an
agent's scopes; SQL over DuckDB. The Kubernetes pod and the proxy's credentials are checked without a cluster
(`tests/test_sandboxes.py`).

## Agents and skills in the marketplace

Agents and skills are listed in the marketplace beside data products, and requested the same way.

- **Listing** (`POST /marketplace/listings`): `{"kind": "agent" | "skill", "target": "<name>", "title",
  "description", "tags", "approvers", "purposes", "max_duration_days", "requires_justification"}`.
  - An agent is a deployed agent app or a custom agent (`urn:li:cairnAgent:<name>`); a skill is an installed
    extension (`urn:li:cairnSkill:<name>`).
  - Only the agent's owner (the extension's installer) or an operator lists it, once per target. Approvers default
    to the owner.
  - `PATCH /marketplace/listings/<id> {"published": false}` hides it; `DELETE` removes it and revokes every grant
    made through it.
- **Finding** (`GET /marketplace/search?q=&kind=all|data_product|agent|skill`): data products, agents and skills in
  one list, each with `kind`; `GET /marketplace/listings?q=&kind=` lists agents and skills alone. Each card shows
  the caller's access (`NONE`, `PENDING`, `GRANTED`, `EXPIRED`, `REJECTED`) and what approval grants.
- **Requesting** (`POST /marketplace/listings/<id>/access-requests {"purpose", "justification", "duration_days"}`).
  The request joins the marketplace's queue: the approvers' inbox and notifications, approve, reject, cancel,
  revoke (`/marketplace/access-requests/...`), with separation of duties.
- **What approval grants.** A time-boxed membership in the listing's persona, which allows `USE_AGENT` on the
  agent's URN (or `USE_SKILL` on the skill's) and nothing else. Agent apps and custom agents already check
  `USE_AGENT` before a run. At the expiry the decision point stops allowing it; the marketplace's sweep then marks
  the request `EXPIRED` and revokes the membership.
- **Audit:** `marketplace.listing_published`, `marketplace.listing_changed`, `marketplace.listing_removed`, and the
  requests' usual entries.
- **Tested** (`tests/test_marketplace_agents.py`, through the HTTP API with a real agent app process and the model
  behind the AI gateway): another person could not list Ana's agent; Cal found it by its description beside data
  products, could not use it, asked (the listing's duration and justification rules held, a second request and
  approving his own were refused), Ana approved from her inbox and Cal's invocation answered; nothing else was
  granted; at the expiry Cal was refused at once and the sweep expired the request and revoked the grant. A skill
  was listed, granted as `USE_SKILL` on its URN only, and delisting took the grant away.

## CoWork: scheduled tasks and an approved outbox

The assistant can work for one person on a schedule, and send its results to Slack or email only once they approve.

- **Scheduled tasks** (`/ai/scheduled-tasks`: `POST` with `name`, `prompt`, `cron` (five fields, UTC, at most hourly),
  optional `max_steps` (1 to 8) and `send`; `GET`, `PUT` (including `enabled`), `DELETE`, `POST …/<id>/run`). Each run
  is a deep-research run as the owner: their policies, masks and row filters apply, their AI budget pays, only
  read-only tools are offered, and the report is saved as their artifact with the datasets it read. The owner is
  told in Cairn and on their own channels. A person keeps up to `CAIRN_COWORK_MAX_TASKS` (20) tasks. A task whose
  owner's account is suspended fails rather than runs, and three failures in a row pause it. The scheduler
  (`cairn scheduler`) runs due tasks; each is claimed by moving its next run, so it runs once.
- **Where results may go:** `send` is `{"slack": "<channel>"}` with a channel listed in `CAIRN_COWORK_SLACK_CHANNELS`,
  or `{"email": [addresses]}` in domains listed in `CAIRN_COWORK_EMAIL_DOMAINS` (comma-separated). With neither set,
  nothing is sent anywhere. Slack uses the bot token (`CAIRN_SLACK_BOT_TOKEN`), email the SMTP relay (`CAIRN_SMTP_URL`).
- **The outbox** (`/ai/outbox`): a run never sends by itself. It puts the report in the owner's outbox and notifies
  them with **Send** / **Don't send** buttons (Slack) or signed links (email); `POST /ai/outbox` queues any artifact
  they may read. `POST /ai/outbox/<id>/approve` (the owner only; anyone else gets 404) checks again that the target
  is allowed, that the owner may still read the report and may export every dataset it was made from
  (`EXPORT_DATASET`, fail closed: otherwise `refused`), redacts PII from the text, and sends it once. `…/reject`
  closes an item; unanswered items expire after `CAIRN_COWORK_OUTBOX_HOURS` (72).
- Audited (category `ai`): `cowork.task.saved`, `cowork.task.deleted`, `cowork.task.ran`, `cowork.outbound.sent`,
  `cowork.outbound.rejected`, `cowork.outbound.refused`, with targets and ids, never the text.
- **From the command line** (`cairn tasks`): the same API for the person whose token is used. `CAIRN_URL` and
  `CAIRN_TOKEN` (or `CAIRN_TOKEN_FILE`, a file holding it) are read from the environment only; there is no token
  argument, so it stays out of shell history and process lists. Agent credentials are refused (403): tasks are a
  person's own.

  | Command | Does |
  |---|---|
  | `cairn tasks list` | your tasks: cron, enabled, last status, last and next run, where reports go |
  | `cairn tasks create --name … --prompt … --cron "0 7 * * 1" [--max-steps 4] [--slack "#ch" \| --email a@x,b@x]` | a new task |
  | `cairn tasks show <id>` | a task and its last 20 runs (status, report, outbox item, error) |
  | `cairn tasks output <id> [--run <run id>]` | the report of the latest finished run (or of that run) |
  | `cairn tasks run <id>` | runs it now and waits for the run |
  | `cairn tasks pause <id>`, `resume <id>`, `delete <id>` | stop or restart its schedule; delete it |
  | `cairn tasks outbox [--status pending]` | items waiting for approval and the ones decided |
  | `cairn tasks approve <id>`, `reject <id>` | send an item, or close it unsent |

  `--json` (before or after the subcommand) prints the API's answer. Exit status 0 is done, 1 the API refused (its
  reason on stderr), 2 a usage or configuration error. Tested by running the real CLI against a real Cairn server
  (`tests/test_tasks_cli.py`).

Tested through the real services, policy decision point, database and HTTP API, with Mailpit as the SMTP server, a
local stand-in for Slack's Web API and a scripted model (`tests/test_cowork.py`): schedules more often than hourly
and unlisted targets were refused; a due task ran once as its owner and nothing was sent until she approved from
the notification's button; someone else could not decide it; the email arrived through SMTP; PII was redacted; a
person who may read but not export the dataset was refused; rejected and expired items were not sent; a suspended
owner's task was paused after three failures.

## Profiles, the installable app and Cairn Desktop

**Profiles.** Each person has a profile (**Platform → Profile**, `GET`/`PUT /v1/me/profile`): role, team, a default
persona (only one they belong to), language (also the UI's), notification channels, pinned assets, dashboards,
metrics, saved queries and workbooks, and the assistant's standing instructions (up to 2,000 characters). The
assistant's chat and CoWork's scheduled tasks (plan, each step and the report) get the profile as context, wrapped
as data after Cairn's rules: it shapes answers (language, focus, format) but grants nothing and cannot lift a rule.
Notification channels are the same settings as Settings → Notifications (one source). A pinned asset the person can
no longer see drops out when the profile is read. Profiles are people's own: agent credentials never reach `/v1/me`,
so an agent cannot plant standing instructions. Changes are audited (`profile.updated`).

**The installable app.** The consumer UI also serves the workspace as a progressive web app at
`/cairn-ui/app.html` (same origin as `/cairn/api`): a web app manifest (`manifest.webmanifest`, standalone, 192 and
512 px icons and a maskable one), and a service worker (`sw.js`, scope `/cairn-ui/`) that caches the app shell only:
the page, its scripts and styles, icons and the offline page. Cairn's API and anything outside `/cairn-ui/` go
straight to the network and are never stored, so no governed data stays on a device. Opened without a network, the
app shows an offline page. On a phone, open the address and use **Add to Home Screen** (Safari) or **Install app**
(Chrome, Edge); the sidebar becomes a drawer under 960 px. The consumer UI's nginx serves the shell files and
`sw.js` with `Cache-Control: no-cache`, so a deploy reaches devices at their next start.

**Web push** (optional) announces inbox notifications on people's devices. Set:

- `CAIRN_WEBPUSH_VAPID_KEY_FILE`: a file with an EC P-256 private key in PEM (`openssl ecparam -name prime256v1
  -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out vapid.pem`), mounted from a Kubernetes Secret; never in code or
  a command line. Changing it invalidates every subscription.
- `CAIRN_WEBPUSH_SUBJECT`: `mailto:` an operations address, or an https URL (push services contact it).
- `CAIRN_WEBPUSH_ALLOWED_HOSTS` (optional): the push services Cairn sends to (exact hosts or `*.` patterns); by
  default Google (`fcm.googleapis.com`), Mozilla (`updates.push.services.mozilla.com`), Apple (`web.push.apple.com`,
  `*.push.apple.com`) and Microsoft (`*.notify.windows.com`). Subscriptions to other hosts are refused, so Cairn
  never posts to an address a browser made up. The API needs egress to these hosts.

People then turn push on in their profile, on each device ("Turn on for this device"). Each push is empty and signed
with the VAPID key (RFC 8292): the browser vendor's push service learns only that there is something; the service
worker fetches the newest unread notification from Cairn with the person's own session and shows it. Subscriptions
the push service reports gone (404, 410) are deleted; ten devices per person are kept. Without a key, push is off
and the channel cannot be turned on.

**Cairn Desktop** (`cairn/desktop`, Electron; the [Cairn Desktop](desktop.html) page is for the people who use it):
Cairn's web workspace in an app, with several workspaces (the command line's profiles, the same `config.toml`), each
in its own browser session; sign-in is Cairn's usual one in the app's window; "Sign in the command line too" runs the
bundled `cairn login` and approves its device code with the person's session (the device flow below; the CLI keeps
its tokens in the OS keychain, the app never holds them); OS notifications from the person's inbox and the pages;
`cairn://<workspace>/<page>` links; a tray icon with the unread count; and the dashboards and reports a person opened,
kept encrypted with the OS keychain for reading while Cairn cannot be reached. Installers (dmg and zip, NSIS,
AppImage and deb, with the CLI inside) come from the `cairn-desktop-release` workflow on a `desktop-v*` tag; updates
come from the rolling `desktop-latest` release (electron-updater, checked against the SHA-512 in `latest*.yml`).

**Workspace settings for Cairn Desktop** (`GET /v1/desktop/policy` for any signed-in person, which the app reads with
their session; `GET` and `PUT /admin/desktop/policy` with `MANAGE_GLOBAL_SETTINGS`; each change audited as
`desktop.policy_changed`; stored in `cairn_agent_settings` under the key `desktop`, no migration). A `PUT` states the
whole policy (what it leaves out goes back to its default); unknown or invalid settings are refused (422):

| Setting | Default | What it does |
|---|---|---|
| `offline_cache` | `true` | `false`: the app keeps nothing of this Cairn for offline reading, and deletes what it kept when it next connects |
| `offline_cache_pages` | `20` | at most this many pages kept per person (1-100) |
| `offline_cache_max_age_days` | `14` | kept pages are deleted after this many days (1-90) |
| `updates` | `"auto"` | `"notify"`: the app only says an update is out; `"off"`: it never checks (app-wide: the strictest workspace wins) |
| `pinned_version` | none | the app does not update past this version (`"1.4.2"`; the lowest pin among workspaces wins) |
| `update_feed_url` | none | an https folder with a release's files (electron-updater's generic feed), instead of GitHub Releases |
| `sign_in_origins` | `[]` | https origins of the identity provider the app's window may show during sign-in, besides the redirects it follows |

```
curl -X PUT https://cairn.example.com/cairn/api/admin/desktop/policy -H "Authorization: Bearer $(cat token)" \
     -H 'Content-Type: application/json' -d '{"offline_cache": false, "updates": "notify"}'
```

The app fails closed: until it has read a workspace's settings it keeps nothing offline. A computer's policy file
(`/etc/cairn/desktop-policy.json`, `/Library/Application Support/Cairn/desktop-policy.json`,
`%ProgramData%\Cairn\desktop-policy.json`) sets the same things for IT, plus workspaces to add and the Cairns
allowed; the strictest of the file, the workspaces and the person wins.

Tested end to end: the profile API through a real Cairn API server over HTTP, with the assistant's chat and a CoWork
task whose research saw the profile (the model is a scripted stand-in), and web push against a stand-in push service
that verified each VAPID signature (`tests/test_profiles.py`); the built UI in Chromium against a real Cairn
(`tests/test_pwa_e2e.py`): the manifest and its icons, the service worker registered and controlling the page with
only the shell cached, the offline page when Cairn is unreachable, the Profile page saved through the API and read
back, the main pages at phone width without sideways scrolling, the drawer, and axe on the Profile page at phone
width. Cairn Desktop: the real app under a virtual display, with GNOME Keyring as the OS keychain, against two real
Cairns and the CLI's PyInstaller binary (`tests/test_desktop_e2e.py`): adding a workspace (seen by `cairn profiles
list`), signing in with a session, two workspaces with separate cookie jars, deep links, a page's and an inbox
notification, links elsewhere opening in the browser, "Sign in the command line too" ending with `cairn whoami` as the
person from the keychain, a dashboard kept encrypted and read offline after Cairn stopped, cleared on sign-out, and an
administrator's `offline_cache: false` honoured; the settings API itself in `tests/test_desktop_policy.py`; unit tests
in `desktop/test`; a Linux build (unpacked and AppImage) smoke-tested, and an AppImage updating itself from a local
feed (and not past a pinned version); the .deb installed with apt, run from /opt with safeStorage on GNOME Keyring,
and removed. Not tested end to end: a real browser vendor's push service (a browser's subscription needs it).

**The desktop release builds** (`cairn-desktop-release` workflow, on every push that changes the app or the CLI; a
`desktop-v*` tag publishes) smoke-test each packaged app against a stand-in Cairn: a workspace opened, a deep link
followed, a page's notification reaching the main process; on macOS and Windows also the tray icon and safeStorage on
the OS keychain (a round trip; the "Cairn Safe Storage" Keychain item on macOS, the DPAPI-wrapped key in Local State
on Windows); on Linux the .deb installed, run and removed. A packaged app's fuses keep debuggers out, so the app
reports this itself when started with `CAIRN_DESKTOP_SELF_TEST=/absolute/path/report.json`: twice a second for two
minutes it writes (0600) whether the tray exists, the notifications that reached it (workspace, title, whether handed
to the OS), and safeStorage's state (available, the Linux backend, a round trip of a throwaway string); never a token,
cookie or page content. Whether the OS then shows a notification is up to the OS (macOS asks the person first) and
can't be seen from a hosted runner.

## The command line: `cairn login` and people's commands

People use `cairn` (the [Command line](command-line.html) page: `pipx install ./cli`, release binaries, Homebrew, an
install script) to query, search, show and ask as themselves. What the operator decides:

**Sign-in (device flow, RFC 8628).** `cairn login` asks `POST /cairn/api/oauth/device_authorization` (client
`cairn-cli`, public: what it gets is useless until approved) for a code, the person approves it on the web UI's
`/cairn/device` page, and the CLI polls `POST /oauth/token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code`
(answers `authorization_pending`, `slow_down`, `expired_token`, `access_denied`, as the RFC says). The authorization
server metadata (`/.well-known/oauth-authorization-server`) lists the device endpoint and grant. Approving needs the
person's browser session (never a token, so a signed-in CLI cannot approve another device), takes JSON only (no
cross-site form posts), shows the device's name and address first, and asks for a passkey when `device.approve` is in
`CAIRN_STEP_UP_ACTIONS` (see Passkeys and step-up). A suspended person cannot approve. Each person gets
`CAIRN_DEVICE_LOOKUPS_PER_10_MIN` (20) wrong codes in ten minutes; codes are eight letters without vowels (about 34
bits) and live `CAIRN_DEVICE_CODE_SECONDS` (600).

**Tokens.** The CLI acts as the person, with their rights (`via` "cli" in `/me` and the audit), never more:

| Setting | Default | What |
|---|---|---|
| `CAIRN_DEVICE_LOGIN` | `on` | `off` turns command-line sign-in off (the device endpoints refuse, existing tokens stop) |
| `CAIRN_CLI_ACCESS_TOKEN_SECONDS` | 3600 | access tokens (`cla_…`), 300 to 43,200 |
| `CAIRN_CLI_REFRESH_TOKEN_DAYS` | 30 | refresh tokens (`clr_…`) unused this long expire, 1 to 365 |
| `CAIRN_CLI_SESSION_MAX_DAYS` | 90 | a sign-in ends after this whatever its refreshes |
| `CAIRN_DEVICE_CODE_SECONDS` | 600 | how long a code waits for approval |
| `CAIRN_DEVICE_POLL_SECONDS` | 5 | the polling interval given to the CLI (faster polls get `slow_down`, +5 s) |
| `CAIRN_DEVICE_STARTS_PER_10_MIN` | 30 | sign-ins one address may start in ten minutes (the start is public) |
| `CAIRN_DEVICE_LOOKUPS_PER_10_MIN` | 20 | wrong codes one person may enter in ten minutes |
| `CAIRN_DEVICE_VERIFICATION_URL` | `<public url>/cairn/device` | the page in the codes' instructions (the web UI's `/device` route) |

Refresh tokens rotate: each works once, and one that comes back after use (it was copied) signs the whole device out
(`device.refresh_reused`). A refresh is refused, and the device signed out, once the person's account is not active
(`corpUserStatus`); SCIM deactivation signs out all of a person's devices at once. Tokens are kept in Cairn only as
SHA-256 hashes, in the agents' token table (`cairn_agent_tokens`, kinds `cli_*`): no migration.

**Signed-in devices.** People see theirs on the profile page (`GET /v1/me/devices`) and sign one out there
(`DELETE /v1/me/devices/{id}`) or with `cairn logout` (`POST /oauth/revoke`, which ends the session whichever of its
tokens is given). Administrators (`MANAGE_GLOBAL_SETTINGS`): `GET /admin/devices[?actor=]`, `DELETE
/admin/devices/{id}`, and `POST /admin/devices/sign-out` `{"actor": "urn:li:corpuser:…"}` for every device of a
person (offboarding). Audited: `device.approved`, `device.denied`, `device.signed_in`, `device.signed_out`,
`device.all_signed_out`, `device.refresh_reused` (category `identity`).

**Queries from the command line.** `cairn query` uses `POST /v1/statements` (then `/next` and `/cancel`): the SQL
workspace's checks (`DataAccessService.prepare`: read-only, catalogued tables, `VIEW_DATASET_PROFILE`, aggregation and
projection policies), then the statement runs on Trino signed in as the person with a five-minute Cairn token, so
Trino's access control applies their masks and row filters. Pages stream for any result size. The next-page cursor
is sealed with AES-GCM for the person who started the query (no state on the API; any replica serves any page;
nobody else can read or cancel it); cancelling deletes the query in Trino. People only (agents keep `/data/query`).
Settings: `CAIRN_QUERY_CONNECTION` (the `trino://` connection with `run_as_user` to use; default the only one, or
`lakehouse`), `CAIRN_QUERY_MAX_RUN_TIME` (`1h`, Trino's `query_max_run_time`). `--warehouse` checks `USE_WAREHOUSE`
and runs on that warehouse's port of the warehouse proxy (`CAIRN_WAREHOUSES_URL`, `CAIRN_WAREHOUSE_PORTS`). Every
statement is audited (`data.queried` with `streamed: true` and Trino's query id; `data.query_cancelled`).

`cairn search` and `cairn show` use `GET /v1/assets/search` and `GET /v1/assets/describe` (only what the caller may
see; `VIEW_ENTITY_PAGE`); `cairn ask` uses `/data/ask`; `cairn whoami` uses `/me?detail=true`, which adds the
person's groups.

Tested end to end (`tests/test_cli_e2e.py`): the CLI as separate processes (the Python package, and the PyInstaller
binary in CI) against a real Cairn API, a real Trino asking Cairn before every action, Cairn Catalog and Store: device
sign-in approved through the API and on the built `/device` page in headless Chromium, tokens in a keyring backend
and in the 0600 file, profiles and environment precedence, every query format streamed over many pages with masks
and row filters as the person, Ctrl-C cancelling the query in Trino, a refused table (exit 4, privilege named),
warehouses, search, show, ask (a stand-in model provider), flows, iac and ontology through the signed-in profile,
refresh rotation and reuse, sign-out from the profile page and with `cairn logout`. Also: the device flow and
sessions on a real database (`tests/test_device_login.py`), the client pieces (`tests/test_cli_client.py`), the
`cairn-cli` wheel installed alone without server dependencies (`tests/test_cli_client_only.py`), and the install
script against an HTTPS mirror (`tests/test_cli_install_script.py`). `cairn ontology status` and `cairn ontology
compile --wait` against Cairn with the real compiler (`tests/test_ontology_e2e.py`, the package and the binary).

**The release builds** (`cairn-cli-release` workflow, on every push that changes the CLI; a `cli-v*` tag publishes):
each of the five binaries (Linux x86_64 and arm64, macOS arm64 and x86_64, Windows x86_64) is smoke-tested, then signs
in through the platform's real keychain with `cli/ci/keychain-smoke.sh` (macOS Keychain, Windows Credential Manager,
the Secret Service of an unlocked GNOME Keyring in a D-Bus session): `cairn login --token-stdin` against a stand-in
Cairn, `cairn whoami --output json` (its `credential_store` is `keyring` and `credential_backend` names the keyring
backend, e.g. `keyring.backends.macOS.Keyring`; `file` when the tokens went to `credentials.json`), the item found
with the OS's own tool (`security`, `cmdkey`, `secret-tool`) and gone after `cairn logout`. The macOS builds also
install the Homebrew formula from a local tap (`cli/ci/homebrew-smoke.sh`: `brew tap-new`, the formula pointing at
the build with its sha256, `brew install`, `brew test`, the completions, `brew uninstall`). Run either script by hand
on a computer to check it the same way (they leave nothing behind). Signing stays off until its secrets exist.

## The guided experience: onboarding, capabilities and help

New people are asked their role the first time they open the home page (Analyst, Data engineer, Data steward, Admin,
or Just exploring; the profile's own role suggests one), and get a **Get started** checklist for it, a sidebar for
that role, the command palette (⌘K / Ctrl+K) and a help panel on every page. The person's side is described in
[Getting started](getting-started.html) (“A guided start”); this is the operator's side.

**Where it is kept.** A person's onboarding progress (`role`, the steps `done`, `dismissed`, `asked`) and sidebar
choices (`pinned`, `hidden`, `show_all`) are the `ui` field of their profile (`GET`/`PUT /v1/me/profile`, column
`cairn_profiles.ui`, migration 0134), so they follow the person to every browser, Cairn Desktop and their phone. A
`PUT` with `{"ui": {"onboarding": {...}}}` changes only the parts it names. Step ids are lowercase words (at most 60),
sidebar entries app paths (at most 80); anything else is refused with 422. Like the rest of the profile it is a
person's own: agents and services cannot read or write it.

**Steps tick themselves** by what the API shows the person, asked as them: a saved query or dashboard they own, a
query in their SQL history, a flow they own (with a schedule), a quality monitor, an accepted or dismissed ontology
proposal, an access request they decided, an AI gateway route, a budget. Visiting a dataset's page and getting an
answer from the assistant tick their steps in the browser. Steps with nothing to detect (single sign-on, groups,
policies, a glossary term, an owner) are ticked by hand. A check the person may not make counts as not done.

**Turning it off.** `PUT /v1/ui/settings` with `{"onboarding": false}` (or **Settings → Guided start**, for
admins) stops the role question and hides **Get started** for everyone; `{"onboarding": true}` turns them back on.
Changing it needs `MANAGE_GLOBAL_SETTINGS` (decided by the policy engine; when it can't decide, the change is
refused). `GET /v1/ui/settings` answers anyone signed in, and `GET /ui/config` carries the setting as
`features.onboarding`. Each change is audited (`ui.settings.updated`, category `identity`). The settings are kept in
`cairn_ui_settings`; people's progress is kept, so turning it on again picks up where they were.

**Capabilities.** `GET /v1/me/capabilities` tells the signed-in person which platform privileges they hold, decided
by the policy engine for them at that moment: `MANAGE_POLICIES` (also `is_admin`), `MANAGE_GLOBAL_SETTINGS`,
`MANAGE_SYSTEM_OPERATIONS`, `MANAGE_INGESTION`, `MANAGE_GLOSSARIES`, `MANAGE_DATABASE`, `MANAGE_ML_MODEL`,
`MANAGE_FINETUNE`, `MANAGE_DATA_APP`, `MANAGE_VOLUMES`, `USE_AI_GATEWAY`, `USE_AGENT` and `RUN_JOBS`, with
`features.onboarding`. It fails closed: a privilege the engine can't decide (an error, a timeout) is reported as not
held. People only (agents and service tokens get 403). The UI uses it to leave out what a person can't use at all:
the Governance studio without `MANAGE_POLICIES`; hub tabs whose list the API refuses without a privilege (data
residency, tokenisation, SIEM export and storage without `MANAGE_POLICIES`; sample datasets, security posture,
limits, all AI usage, chargeback, the AI gateway's models and table maintenance without `MANAGE_SYSTEM_OPERATIONS`;
notification routes without `MANAGE_GLOBAL_SETTINGS`); a hub with no usable tab leaves the sidebar. While the answer
is not known, those entries are left out. **Hiding is never the control**: every endpoint still decides each request,
and a person who types an address they may not use is refused by the API. Everyone keeps **Cost & usage → Your
usage** (their own AI calls, tokens and cost, `GET /ai/usage?mine=true`).

**Help** is the section of these docs about the page or hub tab on screen (each platform area's `docs` anchor, and a
topic per purpose-built page in `ui/src/consumer/guide/help-topics.json`). The sections are bundled into the app when
it is built (`node scripts/build-help.mjs`, checked by `npm test`), so help opens with no server, in the offline demo
too. **Open full docs** opens `/docs/…`; **Ask the assistant about this page** starts a question naming the page.

Tested: the profile's `ui` (round trip, partial updates, validation, people only), capabilities (an admin, a person,
a policy engine that fails, agents and services refused) and the workspace switch (403 without the privilege, 422
for bad values, `/ui/config` and capabilities follow, audited) through a real Cairn over HTTP
(`tests/test_profiles.py`); in Chromium against a real Cairn (`tests/test_tasks_ui_e2e.py`): the role question, the
checklist ticking steps from the API, the palette, help, shortcuts, the sidebar customised and undone, Bob's
sidebar without the operators' areas, and the fifteen common tasks; axe on the role question, **Get started**, the
open palette, the help panel and the shortcuts (`tests/test_accessibility_e2e.py`).

## Excel and Google Sheets

Cairn for Excel (an Office add-in) and Cairn for Google Sheets (an Apps Script add-on) put governed answers in
cells, as the person signed in to them. Both offer the same three functions, whose results spill from the cell,
headers first:

| Excel | Google Sheets | Answers with |
|---|---|---|
| `=CAIRN.QUERY(sql, [connection])` | `=CAIRN_QUERY(sql, [connection])` | a read-only query's rows |
| `=CAIRN.METRIC(name, [dimensions], [filters])` | `=CAIRN_METRIC(name, [dimensions], [filters])` | a governed metric (the metrics layer's, else the semantic layer's approved one), per dimension |
| `=CAIRN.ASK(question)` | `=CAIRN_ASK(question)` | the rows answering a question (ask-your-data; needs an LLM provider) |

Dimensions are `"region, country"` or a range of names; filters `"region = EU; amount >= 10"`, `"region in EU, US"`
or a range (operators `= != <> > >= < <= in` and `not in`). A task pane (Excel) and a sidebar (Sheets) sign people in
and out, list the governed metrics they may see, and insert one as a live formula or as values.

**How they sign in.** Each add-in is an OAuth 2.0 public client of Cairn (the authorization server the AI agents
use): authorization code with PKCE (S256), the person's normal sign-in and a consent page, then an access token and
a refresh token that rotates on each use. The tokens are the person's, narrowed to the scopes `read`, `query_data`
and `model`. Masks, row filters, table permissions and aggregation policies apply as they do for that person in
Cairn; administration is never reachable. Each add-in shows in **Settings → AI agents** (the hub's pause, quotas and
audit apply), and people disconnect it there. The access token lives one hour for Excel and two hours for Sheets:
Sheets' custom functions may read the stored tokens but not write them, so they never refresh themselves, and an
hourly time-driven trigger (installed at sign-in) refreshes instead. A function whose token has run out asks the
person to sign in again; it never spends the refresh token.

**Configuration** (the API's environment):

- `CAIRN_EXCEL_ADDIN_ORIGINS`: the https origin(s) the Excel add-in's files are served from, comma separated (e.g.
  `https://addins.example.com`). This creates the client `agt_excel_addin` with the redirect URI
  `<origin>/callback.html`, and allows CORS from those origins, for bearer tokens only (no cookies), on the routes the
  add-in calls from the browser: `/oauth/token`, `/oauth/revoke`, `/me` and `/data/sheets/*`. No other origin or
  route gets CORS headers.
- `CAIRN_SHEETS_ADDON_SCRIPT_IDS`: the Apps Script project id(s) of the Sheets add-on. This creates the client
  `agt_sheets_addon` with the redirect URI `https://script.google.com/macros/d/<id>/usercallback`. Apps Script calls
  Cairn from Google's servers, so Cairn must be reachable from the internet for Sheets; no CORS is involved.
- `CAIRN_SHEETS_DEFAULT_CONNECTION`: the connection `QUERY` uses when none is named (when only one connection
  serves catalog datasets, it is the default).

A value that is not an https origin or a script id stops the API at start. Removing one turns its client off and
revokes its tokens; setting it again turns it back on (unless an administrator turned it off in the hub).

**Building and deploying** (no dependencies, Node 18 or later): `node build.mjs --cairn-url https://cairn.example.com
--addin-url https://addins.example.com` in `cairn/addins` writes `dist/excel` and `dist/sheets` for that Cairn
(`--api-base` when the API is not at `<cairn-url>/cairn/api`; `--id` for your own manifest GUID).

- *Excel.* Serve `dist/excel` over https at the add-in URL (any static host; `callback.html` must be at its root).
  For a pilot, sideload `dist/excel/manifest.xml`: in Excel on the web, **Home → Add-ins → More add-ins → Upload
  My Add-in**; on Windows, a network share added as a trusted add-in catalog. For everyone, upload the manifest in
  the Microsoft 365 admin center (**Settings → Integrated apps → Upload custom apps**) and assign it to users or
  groups. The add-in needs Excel with the SharedRuntime 1.1 requirement set (Microsoft 365 on Windows, Mac and the
  web).
- *Google Sheets.* Create an Apps Script project (or `clasp create --type standalone`), push `dist/sheets`
  (`clasp push`), and set the script property `CAIRN_API_URL` to the API URL (`https://cairn.example.com/cairn/api`;
  optionally `CAIRN_CLIENT_ID`). Put the project id in `CAIRN_SHEETS_ADDON_SCRIPT_IDS`. To test, use **Deploy → Test
  deployments** on a spreadsheet. For your domain, link a Google Cloud project, configure the OAuth consent screen
  (internal), and publish the add-on privately on the Google Workspace Marketplace (the Marketplace SDK's app
  configuration names the script's deployment), then install it for the domain from the Google Admin console.
  `appsscript.json` limits outbound calls to Cairn's origin (`urlFetchWhitelist`).

Tested end to end against a real Cairn API server with a SQLite warehouse and the metrics layer
(`tests/test_spreadsheet_addins.py`): the built add-ins' own JavaScript runs in Node (the Excel client and custom
functions with a stand-in for Office's `CustomFunctions` and `OfficeRuntime.storage`; `Code.gs` with stand-ins for
the Apps Script services, custom functions run with read-only properties as in Sheets). Each signs in through the
real authorization page with PKCE; a PII column comes back masked, the person's row filter holds in metrics and
queries, a denied table and a write are refused, the token cannot reach administration, a replayed code and a spent
refresh token end the grant, concurrent Excel functions refresh once, and sign-out revokes the tokens. The model
behind `ASK` is a scripted stand-in. The manifest is checked structurally (the schema's elements and order, every
resource id defined, every URL on the add-in's origin), not against Microsoft's XSD, which is not available offline;
running in Excel and Sheets themselves is not part of CI.

## Sources

**Settings → Sources** manages where the catalog's metadata comes from. It needs `MANAGE_INGESTION`.

**Add a source** from the gallery, which covers:

- warehouses: Snowflake, BigQuery, Redshift;
- lakes: Databricks Unity Catalog, S3, Glue;
- databases: PostgreSQL, MySQL, SQL Server, Trino, MongoDB;
- dbt Core and dbt Cloud;
- BI tools: Looker, Tableau, Power BI, Superset, Preset, Metabase, Sigma, Qlik Sense;
- machine learning: MLflow, Amazon SageMaker, Vertex AI;
- Kafka.

For anything else, **Another connector** takes a recipe.

Each form asks for the connection details:

- **Secrets by name only.** A password field takes the *name* of a secret, never its value. The recipe holds a `${NAME}` reference, resolved by whatever runs the source: Cairn's secret store, or the secrets backend of an in-VPC agent pool. A recipe with an inline credential is refused.
- **Runs on.** Choose Cairn's executor, or an agent pool for sources only reachable inside your network. See [Remote agents](remote-agents.html).
- **Schedule.** A cron expression and time zone, or manual runs only.
- **Tell about failures.** The people to notify; by default, whoever set up the source.
- **Advanced options.** JSON merged into the connector's config, for options the form doesn't show.

**Test connection** checks the details on the executor that will run the source, so a pool source is tested by that pool's agents. Nothing is ingested. The result says whether Cairn could connect, and which capabilities (lineage, usage, profiling) the credentials allow.

**Runs and logs.** The list shows each source's last run. **Runs** lists the last 20 runs with their duration, the metadata changes they sent and any failures. **Log** shows a run's output; for pool runs, these are the lines the agent shipped, with secrets removed.

**Health.** Every source is one of:

| State | Meaning |
|---|---|
| healthy | the last run succeeded |
| running | a run is in progress |
| failing | the last run failed, or none has succeeded |
| stale | scheduled, but no successful run for more than twice its interval (at least a day for daily sources) |
| never run | no run yet |

The notifier checks health every five minutes. When a source becomes failing or stale, its owners get a `source.failing` or `source.stale` notification; when it recovers, they get `source.recovered`. Route these to a channel under notification routes. Prometheus gets `cairn_ingestion_sources{state}`, and the alert `CairnSourceFailing` fires after 30 minutes.

The API is `/cairn/api/sources`:

- `GET /connectors` and `GET /executors`;
- `GET` or `POST` on the collection;
- `GET`, `PUT` or `DELETE` on `/{urn}/detail`;
- `POST /{urn}/run`;
- `POST /test`, then poll `GET /test/{id}`;
- `GET /runs/{run urn}`.

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

### Additive or authoritative

`CAIRN_AUTHZ_MODE` and `CAIRN_NATIVE_POLICIES_ENABLED` go together: `additive` with `true` (the default), or
`authoritative` with `false`. Set them on Cairn Core and on Cairn's services alike (Compose and the chart's
`authoritative` profile do), then restart both.

- **Additive.** Core's authorizer chain is first-allow-wins: a Cairn allow grants, and where Cairn has no rule
  (`NOT_APPLICABLE`) or denies, Core's native policies still decide. A Cairn deny on something a native policy
  allows does not hold.
- **Authoritative.** The Cairn authorizer is Core's only authorizer and the policy decision point alone decides:
  where no rule grants, the answer is a deny (not "not applicable"); Core's native policies count only as imported
  into the PDP (every 2 minutes, and at once on a policy change), where a Cairn deny overrides them; and when the PDP
  cannot be reached, Core refuses (fail closed). Only the break-glass users in the authorizer's `rootUsers`
  (`urn:li:corpuser:admin`) are let in without the PDP, so keep that account's password in your break-glass process.
- **Switching** from additive to authoritative changes nothing anyone may do that a native policy grants (it is
  imported), but every Cairn deny starts to hold. Check the posture report's `authz_mode` finding, and try the
  policies in the access simulator first.
- **Tested** against a live Cairn Core (CI job `stack`): after the additive suite the stack is switched to
  authoritative and `tests/e2e/test_authoritative_live.py` checks, as a person with their own token through Core's
  GraphQL, a default deny, a Cairn allow granting alone, a native Core policy overruled by a Cairn deny and counting
  once imported, and Core refusing the person (but not the break-glass admin) while the PDP is stopped; the
  additive suite's `test_14` checks the same deny on viewing a dataset in both modes.

## Network

Users only need the edge (port 9000, or your ingress). The internal APIs are not exposed; developer
consoles are disabled at the edge.

**HTTPS.** Put the certificate chain and key (PEM) in `deploy/tls/` as `tls.crt` and `tls.key`, or
point `CAIRN_TLS_DIR` at another directory, and restart the edge. It then also serves HTTPS on port
9443 (`CAIRN_MAPPED_EDGE_TLS_PORT`) with TLS 1.2+ and HSTS. Set `CAIRN_TLS_REDIRECT=true` to send
plain HTTP to HTTPS (`CAIRN_TLS_PUBLIC_PORT` is the port users reach, default 443), and set
`CAIRN_PUBLIC_URL` to the `https://` address. On Kubernetes, TLS is the ingress's job.

**Gateway API.** On a cluster that routes with the Gateway API instead of an Ingress controller, set `core.ingress.enabled=false` and `core.gateway.enabled=true` (the chart refuses both at once). The chart then renders an HTTPRoute serving the same paths as the Ingress: `/cairn/api` and `/cairn/mcp` with their prefix stripped (and `X-Forwarded-Prefix: /cairn/api`), OAuth discovery (`/.well-known/oauth-*`) unchanged, `/cairn-ui/` and `/docs` to the consumer UI, the rest to the web UI, and the engine's developer consoles to nothing. Attach it to a shared Gateway with `core.gateway.parentRefs` (`[{name, namespace, sectionName}]`; the Gateway must allow routes from the release's namespace), or let the chart create one with `core.gateway.create.enabled`, `create.className` (the GatewayClass) and `create.tlsSecret` (an HTTPS listener on 443; use `create.annotations` for cert-manager). `hostnames` defaults to `core.ingress.host`; `timeout` (300s) and `apiTimeout` (3600s) bound requests. Hosted tenants use a shared Gateway with `CAIRN_GATEWAY=<namespace>/<name>[/<listener>]` for `tenant.sh` (TLS is then the Gateway's, a wildcard certificate for `*.<domain>`). Tested through Envoy Gateway on kind in CI (`tests/test_gateway_api_kind.py`), and validated against the Gateway API schemas.

**Idle connections.** Cairn's API keeps an idle HTTP connection open for 75 seconds
(`CAIRN_HTTP_KEEP_ALIVE_SECONDS`), longer than Trino's HTTP client pools one for its access checks. With a shorter
value, a check sent just as Cairn closed the connection failed with "Failed to query OPA backend". A load balancer
between Trino and Cairn should keep idle connections at least as long.

**Rate limits.** Per client address: password sign-in 10 attempts a minute (bursts of 20), the Cairn
API 50 requests a second (bursts of 400). Over the limit, the edge answers `429 Too Many Requests`.

## Secrets as files

Any `CAIRN_*` secret can be given as a file instead: `CAIRN_DB_PASSWORD_FILE=/run/secrets/db` sets
`CAIRN_DB_PASSWORD` to the file's content (Docker secrets, Kubernetes secret volumes, Vault agent).
Setting both forms is an error, so a stale value cannot hide the file. `ANTHROPIC_API_KEY_FILE` works
the same way.

## Other databases through Trino (federation)

Trino queries other databases in place (PostgreSQL, MySQL, SQL Server, Oracle, Hive, and its other connectors), and Cairn governs them like the lakehouse.

- **How it is governed.** Trino asks Cairn about every table in every catalog. Cairn names a federated catalog's tables as datasets of their platform: `CAIRN_TRINO_CATALOGS`, e.g. `{"crm": {"platform": "postgres", "database": "crm"}}` names `crm.sales.customers` as the `postgres` dataset `crm.sales.customers`. Policies, masks, row filters and refusals then apply in place, and across joins with lakehouse tables.
- **Helm:** `cairn.trino.federation`. Each entry has:
  - `name`: the Trino catalog;
  - `connector`;
  - `properties`, such as `connection-url`;
  - `credentialsSecret`: a Secret with `user` and `password` keys, passed to Trino as environment variables. The chart refuses passwords, secrets and tokens in `properties`;
  - `platform` and `database`: how Cairn names its tables.

  The chart writes the catalog files on the coordinator and workers, and sets `CAIRN_TRINO_CATALOGS` on the API.
- **Catalog the source too.** Add the database as a source in Cairn, so its tables have owners, labels (for masks) and lineage under the same names.
- **Tested** (`tests/test_trino_e2e.py`, Trino 476 with PostgreSQL 16 as catalog `crm`, its password from the environment):
  - the admin saw the rows as they are;
  - `ana` got the labelled email column masked;
  - `bob` got his row filter;
  - an HR table was refused;
  - a join of a lakehouse table with the PostgreSQL table was governed on both sides.

## External Iceberg catalogs

Another organisation's Iceberg REST catalog, or a cloud provider's, queried through Trino as a catalog of its
own and governed like the lakehouse.

- **Register** (`MANAGE_SYSTEM_OPERATIONS`): `POST /v1/external-catalogs` with
  - `name`: the Trino catalog (lowercase, not one of Cairn's own);
  - `uri`, and `warehouse` if the catalog wants one;
  - `auth`: `oauth2` (client credentials: `client_id`, `token_url`, default `<uri>/v1/oauth/tokens`, `scope`),
    `token` (a bearer token) or `none`;
  - `credential_ref`: where the client secret or token is, never the value: `scope:<scope>/<key>` (a secret
    scope the registering operator can read; read as them, so revoking their access stops the catalog) or
    `env:CAIRN_EXTCAT_<NAME>` (only that prefix);
  - `store_endpoint` and `store_region`: where the data files are, if the catalog's vended settings do not say;
  - `writable` (default `false`).

  Cairn signs in once to check the reference (a refusal is `502`, with the status only), adds the catalog to
  Trino and catalogs it. `GET /v1/external-catalogs[/<name>]` (`MANAGE_INGESTION`) shows each with its Trino
  state and last sync; `POST …/<name>/sync` catalogs it again, `POST …/<name>/attach` adds it to Trino again,
  `PATCH …/<name> {"writable": true|false}`, `DELETE …/<name>` drops it from Trino and marks its datasets
  removed. Everything is audited (`external_catalogs`), with references only.
- **In Trino, with no credential.** Cairn runs `CREATE CATALOG <name> USING iceberg` as `cairn-catalogs`, the
  only identity Trino's access control lets create or drop catalogs (Cairn never issues it a token). Trino signs
  in to the external catalog with OAuth2 against Cairn's broker, `POST /v1/external-catalogs/oauth/tokens`: the
  client id is the catalog's name and the client secret is `${ENV:CAIRN_TRINO_ACCESS_KEY}`, resolved inside
  Trino. Cairn checks the key, exchanges the credential and returns the external access token (cached until a
  minute before it ends). Data files are read with the credentials the external catalog vends per table.
  Settings on the API: `CAIRN_EXTERNAL_CATALOGS_BROKER_URL` (Cairn as Trino reaches it) and
  `CAIRN_EXTERNAL_CATALOGS_TRINO_CONNECTION` (the `trino://` connection Cairn runs `CREATE CATALOG` on). Trino
  needs `catalog.management=dynamic` and a writable catalog directory. Helm: `cairn.trino.externalCatalogs`
  (`enabled`, `connection`).
- **Catalogued.** Namespaces, tables and schemas are read from the REST API (metadata only) as `iceberg`
  datasets `<name>.<namespace>.<table>`, the names access control uses, on registration, on demand and every
  `CAIRN_EXTERNAL_CATALOG_SYNC_SECONDS` (3600). Labels and descriptions added in Cairn survive; dropped tables
  are marked removed.
- **Governed.** Grants, masks, row filters and refusals apply as on the lakehouse. The catalog is read-only
  unless `writable`: writes, table and schema changes are refused for everyone.
- **Restarts.** Every `CAIRN_EXTERNAL_CATALOGS_RECONCILE_SECONDS` (60) the scheduler adds registered catalogs
  that Trino has lost again.
- **Tested** (`tests/test_external_catalogs_e2e.py`): a second Cairn Catalog and Cairn Store (scoped keys) as
  the partner's catalog, its OAuth client secret behind `env:` and an identity provider stand-in, on Trino 484:
  - registration refused for non-operators, for credentials in the request, for references outside
    `CAIRN_EXTCAT_*`, for Cairn's own catalog names and for a wrong secret;
  - Trino's catalog file holds no secret, and the broker refuses anything but Trino's key;
  - both tables catalogued with their schemas, and a label added in Cairn kept by the next sync;
  - the admin read the rows as they are, `ana` got the labelled email masked, `bob` his row filter, and `ana`
    was refused the HR table;
  - inserts, deletes, `CREATE TABLE` and `CREATE SCHEMA` were refused, and so was `DROP CATALOG`;
  - the catalog was added again after it was dropped from Trino, and removing it marked its datasets removed.

## Operational databases

Applications need a database they can write to row by row, with low latency, next to the lakehouse. Cairn makes
PostgreSQL databases on a server it administers, and hands out the only way in: a login per person that works
for at most a day.

Settings, which Helm sets from `cairn.databases`, in both the API and the scheduler:

- `CAIRN_DATABASES_ADMIN_URL` (or `_FILE`): a role with `CREATEDB` and `CREATEROLE`.
- `CAIRN_DATABASES_HOST`, `CAIRN_DATABASES_PORT`: where clients reach the server.
- `CAIRN_DATABASES_SSLMODE` (default `require`).

The endpoints:

- `POST /v1/databases {"name"}` (`CREATE_DATABASE`) makes the database. It has an owner role and a reader role,
  `cdb_<name>_owner` and `cdb_<name>_read`. Neither can log in, and only they may connect: `PUBLIC` loses
  `CONNECT` and the `public` schema.
- `POST /v1/databases/<name>/credentials {"access": "write" | "read", "ttl_minutes": 5-1440}` answers with
  host, port, database, user, password and expiry:
  - `write` needs `USE_DATABASE` on `urn:li:pgDatabase:<name>`. The login acts as the owner role, so what it
    creates belongs to the database and outlives the login.
  - `read` needs `VIEW_DATABASE`. Its sessions are read-only, and it may read every table, including future
    ones.

  The password is shown once. Cairn keeps neither the password nor a connection URL with it in. The login
  (`cdb_u_<id>`) is recorded with who asked, and audited (`database.credentials`, with no password).
- `GET .../logins` lists the open logins: your own, or all of them for a database's managers.
  `DELETE .../logins/<user>` ends one now, and its sessions with it.
- The scheduler drops logins once they expire, ending their sessions. One it cannot drop (say, something outside
  Cairn made it own an object) is set to `NOLOGIN`, its sessions are ended, a warning is logged, and it is tried
  again on the next run; the others expire as usual.
- `POST /v1/databases/<name>/branches {"name"}` (`MANAGE_DATABASE` on the source, and `CREATE_DATABASE`) makes
  a copy with roles of its own, for a test or a migration rehearsal. Its objects are handed to the copy's
  owner one by one, and the source's roles lose every grant and default privilege in the copy: a branch never
  touches the source, and either can be dropped without the other. PostgreSQL copies only a database nobody is connected
  to, so a busy source answers 409.
- `DELETE /v1/databases/<name>` (`MANAGE_DATABASE`) ends the sessions and drops the database, its roles and its
  logins.

Nothing here runs SQL a caller wrote. Names are lowercase letters, digits and `_`, are checked, and are always
quoted.

Also revoke `CONNECT` on the server's `postgres` database from `PUBLIC`, so a login reaches only its own
database.

Tested on a real PostgreSQL 16 (`tests/test_databases.py`):

- a write login created and filled a table owned by the database;
- a read login read it and could not write;
- someone without the privilege got 403;
- a login could not reach another database;
- revoked and expired logins were dropped, and their open sessions ended;
- a branch, with a `serial` table, was its own copy, and a busy source answered 409;
- the source of a branch could be dropped, and the branch kept working;
- an expired login that could not be dropped was locked out and retried, and the others still expired;
- dropping removed everything;
- no password reached the audit log.

## Serving tables: lookups in milliseconds on governed tables

A serving table keeps a lakehouse table (or a derived table) as a row store in PostgreSQL, so applications and
dashboards get rows by key in milliseconds instead of a Trino query in seconds, under exactly the source table's
policies. Cairn keeps it in step with the Iceberg table; nobody connects to the row store but Cairn.

**Turning it on.** Serving tables need Cairn's own catalog client (`CAIRN_SERVING_CATALOG`, the same JSON
properties as `CAIRN_PIPES_CATALOG`, which it falls back to) and a PostgreSQL database:

| Setting | What it does |
|---|---|
| `CAIRN_SERVING_STORE_URL` (or `..._FILE`) | The serving database's connection string (a role that may create a schema in it). |
| `CAIRN_DATABASES_ADMIN_URL` (or `..._FILE`) | Without the setting above: Cairn makes the database `cairn_serving` (`CAIRN_SERVING_DATABASE`) on the server it administers for operational databases, with no access for anyone else. It is not listed under `/v1/databases`, and no login is handed out for it. |
| `CAIRN_SERVING_STATEMENT_TIMEOUT_MS` | The statement timeout of a read (default 5000). Reads run on read-only sessions. |

The rows live in the schema `cairn_serving`, one table per serving table (`st_<name>_<generation>`) with its
primary key and an index per declared secondary index; pending writes in `cairn_serving._cairn_pending`. Cairn's
own database keeps the declarations (`cairn_serving_tables`, migration 0127). The scheduler syncs every serving
table whose source has a new snapshot (`cairn scheduler`); replicas take a PostgreSQL advisory lock per table, so
one syncs it at a time. The source table's URN is the one Trino's access control uses
(`CAIRN_LAKEHOUSE_TRINO_CATALOG` names the lakehouse's Trino catalog), so the same policies apply.

**Declaring one.** The table's owners (`MANAGE_DATASET`) or an operator (`MANAGE_SYSTEM_OPERATIONS`):
`POST /v1/serving` `{"name": "accounts", "table": "sales.accounts", "primary_key": ["id"], "indexes": [["region"]],
"writable": false}`. The table must be in the catalog; key and index columns are simple columns. Cairn fills it at
once (nothing is kept if that fails). `GET /v1/serving` lists those whose table you may see; `GET /v1/serving/<name>`
shows the columns, the row count, the snapshot served and the source's current one, `behind`, `lag_ms` (how long
the oldest commit not yet served has waited; 0 when up to date), `pending_writes`, the last sync (`mode`, rows
written and removed, duration) and the last error. `POST .../sync` syncs now (`{"full": true}` rebuilds);
`DELETE /v1/serving/<name>` drops it (an operator cannot drop a table with writes not yet in the lakehouse table).

**How it syncs.** Comparing the served snapshot's data and delete files with the current one's: if files were only
added, only those files are read and their rows upserted by key (**append**); if files were removed or delete files
added (an `UPDATE`, `DELETE`, `MERGE`, compaction or rollback, or the served snapshot was expired), the table is read
and compared by a hash per row, and only the rows that differ are written and the keys gone deleted (**changed**);
if the table's columns changed, a new row store is filled and swapped in, the old one dropped (**full**). Rows with a
null key are skipped; if the source repeats a key, the last row read wins. Tables with equality deletes are not
read (compact them). Trino's position-delete files are read (Cairn reads them plainly where pyiceberg cannot).

**Reading.** `GET /v1/serving/<name>/rows` with `key` (repeatable; a one-column key), `from`/`to` (a range of the
first key column, `from` included), `filter=<column>:<op>:<value>` (`eq ne lt le gt ge in prefix null notnull`;
`in` takes `|`-separated values), `columns`, `limit` (up to 1000) and `order` (`asc`/`desc` by key).
`POST /v1/serving/<name>/lookup` `{"keys": [{"id": 1}], "columns": [...]}` looks up to 1000 keys of any shape and
answers each `found` or `missing`. Every answer is decided for the caller as the data preview decides a read of the
source table: it must be in the catalog and the caller need read access (`VIEW_DATASET_PROFILE`); a table under an
aggregation or projection policy has no rows to serve; the caller's row filters (policies and consent) are run in
PostgreSQL, so a filtered-out key reads as `missing`; column masks are applied to every value as Trino masks them;
a key, range or filter on a column masked for the caller is refused. A decision that cannot be made answers 503;
row filters that cannot be built pass nothing; a row filter PostgreSQL cannot run refuses (read through Trino).
Service tokens are refused. Reads are not audited one by one; declaring, syncing, writing and dropping are
(`serving.*`). Measured in the end-to-end test through the API, with a mask and a row filter applied: p50 about
10 ms, p95 under 50 ms per key lookup on a busy 4-CPU machine.

**Writing (hybrid tables).** A table declared `"writable": true` (simple columns only) takes
`POST /v1/serving/<name>/rows` `{"rows": [{"id": 9001, "balance": "10.50"}]}` from its owners (`MANAGE_DATASET`;
not operators): each row names its key; the given columns are merged into the stored row, readable at once
(governed like any other row), and kept as a pending write. The next sync first writes the pending rows into the
Iceberg table (the rows with those keys replaced, in one commit), then syncs; a key with a pending write is never
overwritten by a sync before it lands. If the write-back fails, the sync stops and says why, and the writes stay
pending.

**Not done.** The serving tables are not a Trino catalog: Trino reads the Iceberg table itself.

## Mirrors: PostgreSQL tables in the lakehouse

A mirror keeps a PostgreSQL table in a lakehouse table, change by change, with no Kafka or Debezium in between.

`POST /v1/mirrors {"name", "connection", "table": "schema.table", "target": "namespace.table"}`
(`MANAGE_INGESTION`) starts one. `connection` names one of Cairn's PostgreSQL connections
(`CAIRN_QUALITY_CONNECTIONS`). The source needs:

- `wal_level = logical`;
- a role that may replicate (`REPLICATION`) and create a publication for the table;
- a primary key on the table.

Mirrors run where streams run (`CAIRN_STREAMS_ENABLED=true` and the streams' catalog settings).

**How it works.**

1. Cairn creates a publication for the table and a logical replication slot with PostgreSQL's own `pgoutput`
   decoder, so there is nothing to install on the server.
2. It copies the table's rows.
3. Each scheduler tick reads whole transactions from the slot and writes them as one Iceberg commit through
   the streams' writer. Inserts and updates are upserts by key, a key change removes the old row, and deletes
   delete. A `TRUNCATE` empties the lakehouse table, and a column added at the source is added to the table.

The commit records the last change's log position (LSN), and only then is the slot advanced. A crash between
the two replays changes the table already has, and they are skipped, so nothing is written twice or lost.

Values keep their PostgreSQL types: integers, floats, `numeric` (as a float), booleans, dates and timestamps
(`timestamptz` in UTC). JSON, UUID and other types arrive as text. A large value an update did not touch (an
unchanged TOASTed column) is read back from the source by key.

**Status and stopping.**

- `GET /v1/mirrors/<name>` shows the status, rows written, last LSN, and `retained_bytes`: the WAL the slot holds
  back on the server.
- `POST .../sync` runs one now.
- `POST .../pause` keeps the slot and stops reading, while WAL piles up on the server. `POST .../resume`
  carries on.
- `DELETE` drops the slot and the publication. Delete mirrors you no longer need: a slot nobody reads keeps the
  server's WAL forever.

Tested with PostgreSQL 16 and the real store and catalog (`tests/test_mirror_e2e.py`):

- the copy, then inserts, updates, a key change, deletes, a new column, NULLs, dates, timestamps and numerics;
- an unchanged 9 kB TOASTed value kept;
- a `TRUNCATE`;
- a crash after the commit and before the slot advanced, which wrote nothing twice;
- pause, with WAL retained, and resume;
- delete, which dropped the slot and the publication;
- refusals of a table without a primary key and of a server without logical WAL.

## Document sources: SharePoint and Google Drive

A document source keeps a SharePoint (or OneDrive) folder, or a Google Drive folder, in a Cairn Store prefix. A
files pipe on that prefix then indexes the files (text, pages, AI description and fields), so they join SQL, search
and AI under Cairn's policies. Operators (`MANAGE_SYSTEM_OPERATIONS`) manage them at `/v1/document-sources`:

```json
{"name": "contracts", "kind": "sharepoint", "location": {"drive_id": "b!x…", "folder": "Contracts/2026"},
 "bucket": "landing", "prefix": "sharepoint/contracts/", "secret_scope": "sharepoint-keys", "cron": "0 * * * *"}
```

- **SharePoint** (Microsoft Graph): keys `tenant_id`, `client_id` and `client_secret` (an app registration with
  `Sites.Selected` granted on the site, or `Files.Read.All`); optionally `graph_url` and `login_url`.
- **Google Drive**: `"location": {"folder_id": "…"}` and the key `service_account_json` (share the folder with the
  service account as a viewer); optionally `base_url`. Google Docs, Sheets and Slides are exported as Word, CSV and
  PowerPoint files; forms and other Google-only items are skipped.

Each run (on its `cron`, or `POST …/<name>/run`) lists the folder and its subfolders and writes new and changed
files (by ETag, checksum or modification time) to `<bucket>/<prefix><path>`. It removes the objects of files that
were deleted or moved away, and writes a moved file at its new path. Files over `CAIRN_DOC_SOURCES_MAX_BYTES`
(100 MB) are skipped and listed. The store is written with `CAIRN_DOC_SOURCES_STORE`
(`{"endpoint", "access_key", "secret_key", "region"}`, a key that may write the sources' prefixes). The keys are
read from the secret scope as the source's owner at every run, and never stored on the source or logged. A failed
run is recorded on the source and audited. `GET …/<name>` lists the files kept.

Who may read the files is decided in Cairn, not in SharePoint or Drive: everyone the files pipe's table is shared
with sees every file synced. Point a source at a folder whose files that audience may see. Audited:
`document_source.saved`, `document_source.deleted`, `document_source.synced` and `document_source.failed`.

Tested with the real store, catalog and files pipe, against a local stand-in for Microsoft Graph and the Google
Drive API (`tests/test_doc_sources_e2e.py`):

- an app's sign-in, files in subfolders and on a second page, indexed with their text;
- a changed file rewritten, a moved one written at its new path, a deleted one removed, a second run writing nothing;
- a service account's signed token, a Google Doc exported as Word, a form skipped;
- a file over the limit skipped;
- a wrong secret and an unreadable scope failing the run.

## Mirrors from MySQL

A connection whose URL is `mysql://user:password@host:3306/db` is mirrored through MySQL's row-based binary log.
The API is the same (`POST /v1/mirrors` with `"table": "db.table"`).

The server needs:

- `log_bin` on, `binlog_format = ROW` and `binlog_row_image = FULL`, which are MySQL 8's defaults;
- `binlog_row_metadata = FULL`, which is not a default: it makes each change carry its column names.

The user needs `REPLICATION SLAVE`, `REPLICATION CLIENT` and `SELECT` on the table, and the table needs a primary
key. A server without these settings is refused when the mirror is made.

**How it works.**

1. Cairn records the log's current position.
2. It copies the table.
3. It reads the log from that position, so changes made during the copy are applied after it and converge on
   it.
4. Each run applies whole transactions, up to the transaction's XID event, so a rolled-back transaction leaves
   no trace. A `TRUNCATE` empties the lakehouse table.

Log positions are the offsets. Each lakehouse commit records the next one, and the mirror records its position
after the commit, so a crash in between replays changes that are then skipped.

MySQL keeps no replication slot, so a mirror's position is only its own record. The server must keep its logs
(`binlog_expire_logs_seconds`) longer than a mirror may be paused. If the position it had read to has been
purged, the mirror fails and says to recreate it, which copies the table again.

Tested with MySQL 8.4 and the real store and catalog (`tests/test_mirror_mysql_e2e.py`):

- the copy, then inserts, updates, a key change, deletes, `DECIMAL`, `DATE` and `DATETIME`;
- a rolled-back transaction, which left nothing;
- `TRUNCATE`;
- a replay from an older position, which wrote only the new change;
- refusals of a table without a primary key and of a statement-based log.

## Mirrors from SQL Server

A connection whose URL is `mssql://user:password@host:1433/database` (or `mssql+pymssql://`, `sqlserver://`) is
mirrored through SQL Server's change data capture (CDC), with the `pymssql` driver (the `streams` extra). Cairn
changes nothing on the server. A database administrator turns CDC on and keeps SQL Server Agent running (it runs
the capture and cleanup jobs):

```sql
EXEC sys.sp_cdc_enable_db;
EXEC sys.sp_cdc_enable_table @source_schema = N'dbo', @source_name = N'orders', @role_name = NULL;
```

The connection's user needs `SELECT` on the table and its change table (`db_owner`, or the gating role if one was
named). A table without CDC, or without a primary key, is refused with what to do.

The copy is taken after reading the highest LSN the capture job has recorded (or, right after CDC was turned on
for the table, from the start of its capture instance). Changes after it are then applied in commit order, whole
transactions at a time; updates come with their old values, so a key change deletes the old row. A change
replayed over the copy writes the same row again, so the lakehouse table converges on the source. Commit LSNs are
the offsets, and the mirror records the last one it applied, so a crash between the lakehouse commit and that
record replays changes that leave the table the same.

CDC's cleanup job keeps changes for three days by default. A mirror paused for longer finds them gone, fails and
says to recreate it, which copies the table again.

Tested with SQL Server 2022 (SQL Server Agent running the capture job) and the real store and catalog
(`tests/test_mirror_mssql_e2e.py`):

- the copy, then inserts, updates, a key change, deletes, `DECIMAL`, `BIT`, `DATE` and `DATETIME2`;
- a rolled-back transaction, which left nothing;
- a replay from an older LSN, which left the table right;
- refusals of a table without CDC and of one without a primary key.

## Mirrors from MongoDB

A connection whose URL is `mongodb://` (or `mongodb+srv://`) mirrors a `database.collection` through change
streams, with `pymongo` (the `streams` extra). Change streams need a replica set or a sharded cluster (a single
node started with `--replSet` will do). The connection's user needs the `read` role on the database. Cairn
changes nothing on the server.

Each document becomes a row keyed by `_id` (an ObjectId as its hex string). Top-level fields are columns, and a
field that appears later is added as a column; embedded documents and arrays are kept as JSON text; decimals are
floats and dates are UTC timestamps. Give a field the same type in every document: a value that does not fit its
column fails the mirror rather than being dropped.

The copy starts from the cluster's operation time read before it, so the changes after it converge on the copy.
Inserts, replacements and updates write the document as it is now (an update reads the current document), and
deletes delete. A multi-document transaction arrives whole; an aborted one leaves nothing. The mirror records its
resume token after each lakehouse commit; cluster times are the offsets, so a crash in between replays changes that
leave the table the same. A dropped or renamed collection ends the stream: the mirror fails and says to recreate
it. The oplog must outlast a paused mirror; if its token is gone, the mirror fails and says so.

Tested with MongoDB 7 (a single-node replica set) and the real store and catalog (`tests/test_mirror_mongo_e2e.py`):

- the copy, then inserts (one with a new field), updates, a replacement and deletes; ObjectIds, decimals, dates
  and embedded documents;
- a transaction of five inserts arriving whole, and an aborted one leaving nothing;
- a replay from an older position, which left the table right;
- a dropped collection failing the mirror; refusals of a missing collection and of a server that is not a replica
  set.

## Cairn Ontology: the ontology compiler

Cairn Ontology (`cairn-ontology`, image `cairn/ontology`, source `ontology/`) compiles an ontology from Cairn's
catalog and query history: business terms with the columns they map to, relationships (how tables join, with
evidence), metrics and named filters taken from the queries people run, and join patterns through bridge tables.
It is a Rust port of the compiler at the heart of NVIDIA's open-source Auto Ontology (Apache-2.0; `ontology/NOTICE`
lists what was ported and what changed). Cairn is its only client: Cairn decides who may see and do what.

**What a compilation does.** For each table, in parallel (`CAIRN_ONTOLOGY_WORKERS`, 3): profile its columns from
the sample Cairn sends (uniqueness, most common values, the full value set of categorical columns, the stored
notation of dates kept as strings, e.g. `DD/MM/YYYY`, and a warning when one column mixes date shapes); ask the
model which columns are foreign keys and whether the table is a junction; describe the other columns; propose its
business term(s) with a display name for each column and the synonyms its description states. Results are
committed in a fixed table order, so when two tables propose the same term name the first in that order keeps it
and the other's is qualified (`Purchase Order (sales.orders)`), whichever model answer came first. Then:
relationships (declared keys; suspected foreign keys resolved to a unique key of another table by embedding
similarity, name and type, the model choosing or abstaining, with a value check when it abstains; dropped when the
column shares no value with its target; joins seen in the query log added with their counts; one default join per
role group); metrics from the query log (expressions ranked by how often their queries ran, judged by the model,
made into complete, validated SELECTs); and bridge tables (every column a key) with a join pattern through them.
Terms and columns are embedded for search by meaning.

**Runs.** Every compilation is a versioned run (`full`, or `incremental`: a table whose snapshot did not change
since the published run is reused, not sent to the model again). A run can be cancelled: tables in flight finish,
the rest are skipped, and the run resumes later from the tables it had not committed (also after the process
stopped: such runs are marked `interrupted`). The last completed run is the published ontology.

**Model calls.** Through Cairn's AI gateway on the OpenAI protocol (`/v1/ai/gateway/openai/v1`, chat completions
with structured JSON answers and `…/embeddings`), as the compiler's own agent identity (a key agent with scope
`model`, acting for an owner who has `USE_AI_GATEWAY` on both models): every call is admitted, budgeted and
metered under that identity (feature `gateway:<agent>`). Create it once with `POST /v1/ontology/agent`
(`MANAGE_GLOBAL_SETTINGS`, body `{"owner": "urn:li:corpuser:…"}`); its key is shown once: put it in a Kubernetes
Secret (`cairn.ontology.model.keySecret`). Answers are validated against their expected shape and retried
(`CAIRN_ONTOLOGY_MODEL_RETRIES`, 2; timeout `CAIRN_ONTOLOGY_MODEL_TIMEOUT_SECONDS`, 120); at most
`CAIRN_ONTOLOGY_MODEL_MAX_INFLIGHT` (6) calls are in flight; a 429's `Retry-After` (at most a minute) is waited out
before the retry, without holding a slot. Without a chat model configured, each step uses its fallback (a term
named after its table, no inferred keys). With one configured, a run whose model calls still fail after the retries
(the provider down, the gateway's rate limit or budget) **fails closed**: its state is `failed` with
`stats.model_failures` and a message, nothing is published (the published ontology stays as it was), and its table
results are dropped so that resuming it, once the model answers, compiles those tables with the model. Without an embeddings model, search by meaning and embedding
candidates are off; name heuristics still find keys. The agent's per-minute quota (120 requests by default) also
counts these calls: a full compile makes up to four model calls a table plus those for relationships and metrics, so
raise the compiler agent's `per_minute` (`PATCH /admin/agents/<id>` with `{"per_minute": …}`) to what a compile of your
catalog needs; over the quota the gateway answers 429, and calls that still fail make the run fail as above.

**Syncing.** The context-miner worker syncs and compiles every `cairn.ontology.intervalSeconds` (3600; 0: on
demand only), one replica at a time; `cairn ontology sync|compile|import|status` does it from the command line.
A sync sends every dataset of the catalog with its columns, types, nullability, primary and foreign keys,
descriptions (edited ones first), deprecation and profile, and a sample of up to `CAIRN_ONTOLOGY_SAMPLE_ROWS`
(200) rows read as `cairn.ontology.sampleAs` through the governed data path (that person's masks apply; leave it
empty to send profiles only); then the finished Trino queries since the last sync (`/v1/queries` data). Tables
can be kept out with `tableAllowRegex` / `tableDenyRegex` (deny wins, matched against the unqualified name).

**Proposals.** A finished run comes back into the context inbox (**Semantic models → Ontology proposals**, and
the Context inbox): a term as a glossary-term proposal linked to its columns, a relationship as a join, a metric
as a metric definition. Deciding is a person's act (agents are refused) and needs `MANAGE_GLOSSARIES`; accepting
also needs `EDIT_ENTITY_GLOSSARY_TERMS` on each dataset it is about: a term is written under the glossary node
**Ontology** and linked to its columns (its name, definition and synonyms can be edited as it is accepted); a join
or metric becomes an approved, versioned definition that Ask-your-data and text to SQL use. A rejected proposal is
not brought back by later runs. Every decision is audited (`ontology.proposal_accepted` / `_rejected`) with the
reviewer's comment, which is also kept with the proposal.

**The Ontology explorer** (**Ontology** in the navigation, `/ontology`; linked from Semantic models). Everything on
it is the signed-in person's view: elements over datasets they may not see are not there, and every action is
theirs, under their privileges.

- *Graph*: business terms as nodes coloured by domain (the dataset's domain, or its schema when it has none),
  accepted terms filled and proposed ones with a dashed ring, bridge tables as diamonds, tables without a term as
  squares; relationships as arrows labelled with their columns and cardinality (`customer_id → id · N:1`), solid
  for declared keys, dashed when only seen in queries, dotted when inferred. Pan by dragging or with the arrow keys,
  zoom with the wheel or `+`/`-`, `0` or **Fit** to see everything, and a minimap to move around. Filter by
  domain, schema, status (accepted, proposed, compiled, rejected) and confidence; **Find a term** zooms to it and
  lights up its neighbours. Over 150 terms the graph starts with one node per domain (sized by its terms, edges
  summed); open a domain, filter or search to see its terms, or **Show every term**.
- *List*: the same terms, relationships and bridge tables as tables: the screen-reader alternative to the graph.
- *Term* (the side panel, or `/ontology/terms/<id>`): definition, synonyms, domain, owners (the dataset's), the
  mapped columns (each linking to the asset), date formats detected, relationships with their join and evidence
  (declared key, joins in the query log with query and run counts, the model's confidence), the metrics that use
  it, bridge tables through it, provenance (the run, its mode, when and who asked, and its inputs: tables, columns,
  queries), and the proposals still open on it, with accept, reject, and edit-and-accept. Sampled values are never
  shown (they were read as the sampling identity, not as the viewer).
- *Review*: the open proposals, filtered by kind and confidence; select some (or work on the highlighted one) and
  accept or reject them with one comment. Keyboard: `j`/`k` move, `x` selects, `a` accepts, `r` rejects, `o`
  opens the term. **Accept all above** a confidence lists what it will accept before it does. Each proposal is
  decided as if alone: those you may not decide are listed with the reason and the others go through.
- *Compile* (`MANAGE_GLOSSARIES`): choose every table, some schemas or some tables (pickers over the compiler's
  catalog, limited to tables you may see), incremental or full, and see what it will cost before it starts: tables
  to compile, tables reused, and an upper bound of model and embedding calls. A run limited to some tables compiles
  only those; every other table keeps what the published run made of it, so the ontology stays whole. The run's
  progress shows live (tables done, then relationships, metrics, bridge tables, publishing) with **Cancel** and
  **Resume**; when it completes its proposals are imported into the review queue and the diff against the run
  before is shown (terms, relationships and metrics added, removed and changed).
- *Download* the graph as JSON-LD, RDF Turtle or CSV (one row per term, column attribute, relationship and metric).

Elsewhere: an asset page's summary has **Business terms** (the table's terms and each column's, linked to the term
pages, and its joins); search shows matching terms above the assets with a **Term** badge (a synonym finds its
term); Ask-your-data answers show **Grounded in** chips for the terms of the tables used and the compiled
relationships given to the model (`grounded` in the answer).

**Ask-your-data.** The relationships among the tables a question uses are given to the model (the default join
of each role first), and the SQL it writes is checked with the compiler's SQL validation; the answer carries the
findings (`ontology`). When the compiler cannot answer, the answer carries a warning instead: the check never
blocks the governed run.

**API** (`/v1/ontology`). Reads show only elements over datasets the caller may see (`VIEW_ENTITY_PAGE` on every
one): `GET /terms?q=`, `GET /relationships?datasets=…&between=true`, `GET /metrics`, `GET /bridges`,
`GET /search?q=&mode=name|semantic|hybrid`, `GET /graph?format=json|jsonld|turtle` (OWL classes, properties and
relationships), `GET /provenance/<kind>/<id>`, `POST /validate-sql` (`{"sql"}`: unknown tables and columns,
deprecated ones, joins with no known path, joins contradicting the known keys, fan-out, metrics computed unlike
their definition; a query over a table the caller may not see is answered with `not_visible` only),
`GET /explorer` (terms with domain, schema, status and confidence; relationships; bridge tables; tables),
`GET /terms/<id>` (a term with its evidence and provenance), `GET /asset?urn=` (a dataset's terms per column; 404
for a dataset the caller may not see), `GET /search-terms?q=` (by name or synonym), `GET /catalog` (the tables
the caller may see, for scoping a run), `GET /graph?format=csv`, `GET /proposals?kind=`,
`POST /proposals/<id>/accept|reject` (`{"comment", "edits": {"name", "definition", "synonyms"}}`, edits for terms
only) and `POST /proposals/batch` (`{"ids": [...], "action": "accept"|"reject", "comment"}`, at most 500; the
outcome of each). Compiling needs `MANAGE_GLOSSARIES`: `POST /sync`, `POST /estimate` (`{"mode", "tables",
"schemas"}`), `POST /runs` (`{"mode": "incremental"|"full", "sync": true, "wait": false, "tables": [...],
"schemas": [...]}`: a scope names tables the caller may see, else 422), `GET /runs/<id>` (with `progress` while
it runs), `POST /runs/<id>/cancel|resume|import`, `GET /runs/<id>/diff?against=` (a completed run against the
completed run before it, over what the caller may see) and `GET /runs` (a run's statistics count every table);
`GET /status` shows whether the compiler answers. MCP tools: `ontology_search_terms`, `ontology_relationships`,
`ontology_validate_sql`.

**Deployment.** Helm: `cairn.ontology.enabled`, its token in `cairn.ontology.secret` (Cairn calls with it; the
compiler refuses every `/v1` request without it), its PostgreSQL URL in `cairn.ontology.database.existingSecret`
(required), the models in `cairn.ontology.model.chat` / `.embeddings` (served by the gateway:
`CAIRN_AI_GATEWAY_MODELS`). One replica (strategy Recreate); a network policy lets only this release's components
call it. Compose: `--profile ontology` (state in a SQLite file in a volume; set `CAIRN_ONTOLOGY_URL` and
`CAIRN_ONTOLOGY_MODEL_KEY_FILE`). The compiler's own settings are listed in `cairn-ontology --help`'s source
(`ontology/src/main.rs`); `/-/ready`, `/-/healthy` and Prometheus `/metrics` (model calls, failures, retries,
tokens, runs) are open for probes.

**Down.** When the compiler is unavailable, the ontology pages say so, compiles fail with 503, Ask-your-data
answers with a warning, and nothing else in Cairn changes.

## Query history, profiles and tuning hints

Every finished Trino query, on the shared coordinator and on team warehouses, is kept for `CAIRN_QUERY_HISTORY_DAYS` (30). These are the same events Trino's HTTP event listener sends for usage statistics. Each query records:

- who ran it, its SQL, and its state and error;
- when it ran, and its wall, CPU and queued time;
- the bytes and rows it read and the rows it returned;
- its peak memory and the tables it read;
- its plan, when Trino includes one.

- **`GET /v1/queries`.** People see their own queries; operators (`MANAGE_SYSTEM_OPERATIONS`) see everyone's. Filter by `user`, `state`, `since`, `table` (a dataset URN) or `text`, or sort with `slowest`. The SQL can hold literals people typed, so no one else sees it.
- **`GET /v1/queries/<id>`.** The profile, with tuning hints drawn from its statistics and SQL:
  - it waited long in the queue (the warehouse was busy);
  - it read gigabytes to return a few rows (filter on partition columns, or keep a derived table);
  - it used `SELECT *` over wide reads;
  - it has a cross join, or a condition-less `FROM a, b` (`UNNEST` and `LATERAL` excepted);
  - it sorted a large result without `LIMIT`;
  - its memory was near the per-query limit (`CAIRN_TRINO_QUERY_MAX_MEMORY`);
  - it failed, with the failure explained: memory, access refused by Cairn's policies, or capacity.
- **In the UI.** **Query insight** (`#/queries` in the workspace) lists the queries with their time, bytes read and rows, filtered by SQL text, user (operators), state, or the slowest first. A query's profile shows its hints, statistics, tables and SQL.
- **Tested** with a real Trino (`tests/test_trino_e2e.py`). A person saw only their own queries, finished and refused, with times, rows and tables. An operator saw everyone's. A cross join and a refused read got their hints, and another person could not open the profile.
- **The pages, tested in a browser** (`tests/test_sql_ops_ui_e2e.py`: the built UI in Chromium against a real Cairn API, a real database and a real git repository):
  - a workflow made in the editor ran with a failing check; the browser showed the check's value and the skipped report, and the owner was notified;
  - a git folder was linked and pulled, then a change made in Cairn was pushed to a new branch, never to `main`;
  - query insight showed only the person's own queries, slowest first, with the queue, `SELECT *` and unbounded-sort hints.

## Warehouses: a Trino per team, stopped when idle

A warehouse is a Trino of a team's own: its own size, its own queue, its own bill. The same catalogs and Cairn's policies apply. The warehouse proxy (`cairn warehouses`) sits in front, with one port per warehouse.

- **Scale to zero.** After `idleMinutes` without client traffic, the warehouse is stopped. A running query counts as traffic, because its client keeps polling. The next query starts the warehouse, waits until Trino takes queries (`startTimeoutSeconds`, then a short warm-up), and goes through. Queries arriving together share one start. `idleMinutes: 0` keeps a warehouse up.
- **Several nodes, scaled with the load.** Give a warehouse `workers: {min, max, queriesPerWorker, scaleDownMinutes}` and the chart deploys a workers Deployment for it, with the coordinator running no queries itself. The proxy counts the queries running and queued through it, from the statement URLs their clients poll. It keeps `ceil(queries / queriesPerWorker)` workers, between `min` and `max`:
  - more at once when the load rises;
  - fewer after `scaleDownMinutes` of less load;
  - none while the warehouse is stopped.

  A query that arrives with no worker up waits in Trino for one (up to `startTimeoutSeconds`). With Docker, workers are a pool of containers (`"workers": {"docker": {"containers": […]}}`).
- **Start now.** Pipelines → Warehouses → **Start now** (`POST /v1/warehouses/<name>/start`, with `USE_WAREHOUSE`) starts a stopped warehouse before the first query, so that query does not wait. Starting counts as use, so the warehouse runs for at least its idle time. The proxy takes the start only with the control key the chart gives it and the API (the Trino access key). The start is audited.
- **What it costs.** Every `CAIRN_WAREHOUSES_REPORT_SECONDS` (300), the proxy reports node-seconds to Cairn: the coordinator and each ready worker, for the time they ran. Each report is one row of the cost report (Reports → Costs → jobs: job `urn:li:warehouse:<name>`, engine `warehouse`), priced at `cairn.trino.warehouses.usdPerNodeHour` (`CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR`). A report that fails is sent again with the same id, and Cairn records each id once, so nothing is counted twice.
- **Who may use one.** Each warehouse asks Cairn as itself. Running a query there needs `USE_WAREHOUSE` on `urn:li:warehouse:<name>`, granted to the team like any policy; operators may use any. What a query may read is decided as everywhere else, masks and row filters included.
- **Helm.** List the warehouses in `cairn.trino.warehouses.list` (`name`, `port`, optional `idleMinutes`, `queryMaxMemory`, `resources`). The chart deploys:
  - each warehouse's Trino, whose replica count Helm leaves to the proxy;
  - the proxy, with TLS from `cairn.trino.tls.secretName`, and a Service with a port per warehouse;
  - a Role that lets the proxy read and scale only those Deployments;
  - NetworkPolicies, so a warehouse is reached only through the proxy.

  Clients connect to the proxy's Service on the warehouse's port, with their usual Cairn token.
- **Compose.** Use the Docker scaler (`"docker": {"container": "…"}` in `CAIRN_WAREHOUSES`). The proxy then needs the Docker socket; prefer Kubernetes in production.
- **What it reports.** `GET /cairn/warehouse` on a warehouse's port shows its state: whether it is ready and running, its queries in flight, how long it has been idle, and how often it was started and stopped.
- **In the workspace.** Pipelines → Warehouses shows each warehouse's state, its queries running and queued, its workers, time idle and when it will stop (`GET /v1/warehouses`). People see the warehouses they may use; operators see all. A warehouse the API cannot reach shows as unreachable. The chart points the API at the proxy (`CAIRN_WAREHOUSES_URL`, `CAIRN_WAREHOUSE_PORTS`).
- **Tested.**
  - **Docker** (`tests/test_warehouses_e2e.py`, with real Trino). Three queries to a stopped warehouse started it once, and all were answered. Trino's follow-up URLs pointed back at the proxy. A long query kept it awake. When idle it stopped, and the next query started it again.
  - **Kubernetes** (`tests/test_warehouses_k8s.py`, with a real API server and the chart's RBAC). The proxy scaled its warehouse to 1 and back to 0, and was refused (403) on any other Deployment.
  - **Several nodes** (`tests/test_warehouses_scaling_e2e.py`, with a real Trino coordinator and two worker containers):
    - the first query started the coordinator and then one worker;
    - two queries at once brought a second worker;
    - after the load dropped, the workers went before the coordinator's idle time, then the coordinator stopped;
    - **Start now** worked only with the control key;
    - node-seconds reached the cost report once per report.

    With Kubernetes (`tests/test_warehouses_k8s.py`), the proxy's account scaled the workers Deployment 0 → 3 → 1 → 0 with the load.
  - **Admission** (`tests/test_trino_access.py`). Only the team's members and operators ran queries on a warehouse.

### Adaptive warehouses

An adaptive warehouse has no size of its own. It is one port in front of several of the warehouses above,
listed smallest first, and for each query it picks which one runs it. People connect to one place and nobody
picks a size:

```yaml
cairn:
  trino:
    warehouses:
      list:
        - { name: sales, port: 8443 }
        - { name: sales-xl, port: 8444, workers: { min: 0, max: 8 } }
      adaptive:
        - name: auto
          port: 8450
          tiers: [sales, sales-xl]
          largeWhen: { cpu_ms: 600000, input_bytes: 100000000000, peak_memory_bytes: 8000000000 }
          overflowAt: 8
```

For each new query, the proxy asks Cairn how earlier runs of the same query went (`POST /v1/warehouses/hint`,
with the Trino access key). "The same query" means the same fingerprint: comments, literals and the length of
`IN` lists are left out, and case and spacing are folded. So a report run with another date or customer id is
the same query. Cairn answers with the median CPU time, bytes read, peak memory and wall time of the last 20
finished runs, from query history. The answer holds no SQL and no user.

The query then goes:

- to the largest tier, when an earlier run reached any `largeWhen` figure (figures and thresholds are compared
  as given, fractions included);
- to the largest tier, when the smallest already has `overflowAt` queries running or queued and the largest
  has fewer ("overflow");
- otherwise to the smallest tier.

A query stays where it started: its follow-up requests are routed by query id. If the proxy no longer knows a
query (it restarted, or another replica took the query), it asks the tiers that are up in turn; only the one
running the query knows it, the others answer 404 and are neither woken nor charged for it.

Each tier is still a warehouse of its own. It starts on demand, scales its workers, stops when idle, is billed
as itself, and admits only its members, so give the tiers the same groups.

If Cairn cannot be asked (the API is away, or `CAIRN_WAREHOUSES_HINT_URL` is unset), queries are routed by
load alone. `GET /cairn/warehouse` on the adaptive port shows each tier's load and how many queries went where
and why (`small:history`, `large:overflow`, …).

Tested with two real Trino warehouses and the hint endpoint served over HTTP from a real query history
(`tests/test_warehouses_adaptive_e2e.py`):

- a light query started only the small warehouse;
- a heavy report with new literals started the large one and came back whole;
- a query never seen went to the small one;
- while a slow query held the small one, the next query overflowed to the large one;
- with no hint to be had, queries went to the small one;
- after the proxy forgot a running query, its polls still reached the large warehouse and it came back whole;
- the hint endpoint answered only with the key.

## Notebooks: JupyterHub with Cairn sign-in

JupyterHub runs from the official JupyterHub chart with `helm/jupyterhub/values-cairn.yaml`. Its hub and notebook images include `cairn-lakehouse`.

- **Sign-in.** People sign in to the hub through Cairn: JupyterHub is a client of Cairn's OAuth2 server, like Trino and Airflow. Its tokens' audience is `jupyterhub`, so they are accepted nowhere else. On Cairn's side, set `cairn.notebooks.jupyterhub`:
  - `enabled: true`;
  - `redirectUris`: `https://<hub>/hub/oauth_callback`;
  - the client secret, in a Secret shared with the hub.

  In `jupyterhub_config.py`, `cairn_lakehouse.jupyter.configure_hub(c, cairn_url=…, hub_url=…)` sets the rest: `GenericOAuthenticator`, Cairn's endpoints, and the spawn hook. `JUPYTERHUB_CRYPT_KEY` must be set.
- **Kernels act as the person.** When a person's server starts, the hub exchanges their sign-in token for a Cairn token of their own (`POST /v1/notebooks/token`, valid for `tokenTtlSeconds`, 8 hours) and gives it to the server as `CAIRN_TOKEN`:
  - `%%sql` runs on Trino as them, with their masks and row filters, and so do `%cairn spark` and MLflow;
  - the token works wherever a personal access token does;
  - it stops working as soon as the person is suspended, and no new one is given;
  - each exchange is audited.
- **Notebooks are catalog assets.** Every save sends the notebook to Cairn (`cairn_lakehouse.jupyter.post_save_hook`, set by `configure_hub`). It becomes a notebook asset owned by the person, with its cells, and a job whose inputs are the tables its `%%sql` cells read. Two-part names use `defaultCatalog`, and CTEs are not counted as tables. A failed publish never stops the save.
- **Git folders in Jupyter.** The server extension `cairn_lakehouse.jupyter` (enabled by `configure_hub`) writes the notebooks of each of the person's [git folders](#git-folders) under `git/<folder>/` when their server starts, and again on `POST <server>/cairn/git/sync`.
  - **Saves go back to git.** Saving a notebook under `git/<folder>/` (the save hook) also keeps it with that folder, and the folder's next push commits it.
  - **Outputs stay out of git.** Pushed notebooks have their outputs and execution counts cleared, so query results never reach the repository and diffs show only what changed.
  - **Local work is never overwritten.** The sync writes a file only when it is missing, or still as the last sync left it. A file edited in a way Cairn has not seen (its save never reached Cairn) is kept and reported.
- **Tested** (`tests/test_jupyterhub_e2e.py`, with a real JupyterHub, configurable-http-proxy and Jupyter server):
  - a person signed in through Cairn's OAuth flow;
  - their server started with their own Cairn token;
  - saving a notebook in it published the notebook to the catalog, owned by them, with lineage from the two tables its query read;
  - tokens for other audiences were refused, the notebook token worked as the person, and it stopped when they were suspended;
  - a git folder's notebooks appeared in the person's server. An edited one (run, with an output) and a new one were pushed to a branch of their own, outputs cleared, while `main` stayed as it was;
  - a notebook changed in git and in Jupyter was a conflict; taking git's version brought the file in Jupyter in line;
  - a file edited outside Jupyter was kept by the sync.

## Machine learning: MLflow behind Cairn

MLflow keeps experiments, runs and the model registry. People and pipelines reach it only through Cairn, at `<cairn>/v1/ml`, with their usual Cairn token:

```bash
export MLFLOW_TRACKING_URI=https://cairn.example.com/cairn/api/v1/ml
export MLFLOW_TRACKING_TOKEN=<your Cairn token>   # a person's, or a pipeline user's
```

- **Runs are the caller's.** Cairn sets `mlflow.user` on a new run to the person or pipeline user signed in, whatever the client says, and refuses changes to it. Experiments, runs and artifacts are open to signed-in people, like a shared workspace.
- **The registry is governed.** A registered model belongs to whoever created it, and a name someone else owns is refused. The model's catalog asset is `urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,<name>,PROD)`:
  - reading a model needs `VIEW_ENTITY_PAGE` on it, or being its owner;
  - changing it (versions, aliases, stages, tags, rename, delete) needs `MANAGE_ML_MODEL` on it, or being its owner;
  - searches list only the models the caller may see.

  Creating a model and every change are audited (category `ml`).
- **Models are catalog assets.** Each new version appears in the catalog as an ML model in its model group, owned by its creator. The run that trained it is recorded as a job. Its inputs are the run's datasets (`mlflow.log_input`): a dataset whose source or name is a lakehouse table (`lakehouse.<ns>.<table>` or `iceberg://<ns>.<table>`) becomes lineage from that table, so the model shows up downstream of the data it was trained on.
- **Serving.** Run `mlflow models serve` for a model (for example `models:/churn@champion`). List it in `cairn.mlflow.serving` (`CAIRN_ML_SERVING`: `{"churn": "http://churn-serving:5000"}`), and callers send predictions to `POST <cairn>/v1/ml/serve/churn/invocations`. Calling a model needs `USE_ML_MODEL` on it, or being its owner. Each call is audited.
- **Feature tables.** A table's owners (`MANAGE_DATASET`) declare its columns as features: `POST /v1/ml/feature-tables` with `name`, `table` (`namespace.table`), `keys` (the entity's key columns) and optionally `features` (default: every other column). Cairn publishes the feature table, one feature per column and the primary keys, each with the table as its source, so the catalog shows table, features and the models trained on that table together. Reading feature values is reading the table: as yourself, under its policies, masks and row filters. `GET /v1/ml/feature-tables` lists those whose table the caller may see (`tests/test_feature_tables.py`).
- **Online feature store.** A feature table's owners (`MANAGE_DATASET` on its table) serve it by entity key for models at prediction time: `PUT /v1/ml/feature-tables/<name>/online` with optionally `timestamp_column` (the latest row per entity wins; without it the table holds one row per entity, and a repeated entity is logged) and `ttl_seconds` (older values are served as `expired`). Cairn fills it at once (and keeps nothing if that fails), then each scheduler tick looks at the table's current snapshot and, when it moved, writes only the entities whose values changed and removes those gone. `POST .../online/sync` syncs now; `GET .../online` shows the entities, the snapshot served and the last error; `DELETE .../online` stops serving and drops the values.
  - **Lookups.** `POST .../online/lookup` with `entities` (each a map of the key columns; values are read as the key's type, so `"7"` finds 7) and optionally `features`, at most 1,000 entities a call. Each result has `status` (`found`, `missing`, `expired`), the values and `event_ms`.
  - **Governed as a read of the table.** Values are kept raw and each lookup is decided for the caller on the source table: without read access (`VIEW_DATASET_PROFILE`) nothing is returned; a column mask masks the feature as Trino masks it (the same strategy per type); a row filter refuses the lookup (it is SQL an entity lookup cannot apply: read the table through Trino). The service principal cannot look features up. Lookups are not audited per call; turning serving on and off is.
  - **Where values live.** Cairn's database (tables `cairn_online_feature_tables` and `cairn_online_feature_values`, migration 0065), or a database of their own with `CAIRN_ONLINE_STORE_URL` (created there on start). Cairn reads the lakehouse with its own catalog client: `CAIRN_ONLINE_FEATURES_CATALOG` (pyiceberg REST settings, JSON or a file), else `CAIRN_PIPES_CATALOG`. Tested with the real store and catalog in `tests/test_online_features_e2e.py`.
- **Model monitoring.** Every call to a model through Cairn (`POST /v1/ml/serve/<model>/invocations`) is logged (time, caller, status, latency, rows) and answered with `X-Cairn-Inference-Id`. A model's managers (its owner, `MANAGE_ML_MODEL`) turn monitoring on with `PUT /v1/ml/models/<model>/monitor`:
  - `baseline`: `{"table": "<namespace>.<table>"}` (the training table, read once by Cairn's own catalog client: `CAIRN_ML_MONITOR_CATALOG`, else `CAIRN_PIPES_CATALOG`; a missing table or column is refused when set up) or `{"since_ms", "until_ms"}` (a reference period of the model's own logged calls; checks wait until it ends). `features` limits the comparison (default: every input column).
  - Each check (every `every_minutes`, default 60, on the scheduler; or `POST .../monitor/run`) covers the last `window_minutes` (default 1440): per feature the population stability index (ten quantile bins of the baseline for numbers, its 20 most common values otherwise) and the mean against the baseline's; the predictions' PSI against a reference period; error rate; latency p50 and p95; and accuracy (classes) or mean absolute error (numbers) over the calls with labels. Above `drift_threshold` (default 0.2) a feature drifts; under `min_rows` (default 100) the check reports `insufficient`. Turning to drift notifies the owner and `notify` (`ml.drift_detected`), turning back `ml.drift_resolved`.
  - `POST /v1/ml/models/<model>/feedback` with `inference_id` and `labels` (one per row), by the model's managers or whoever made the call. `GET .../monitor` and `.../monitor/checks` are for whoever may see the model.
  - **Payloads** (inputs and predictions) are kept only for monitored models while `log_payloads` is on (default), readable only by the model's managers (`GET .../monitor/inferences`), deleted after `retention_days` (default 30) and when monitoring is turned off; the call log and checks stay. Tables `cairn_ml_monitors`, `cairn_ml_inferences`, `cairn_ml_monitor_runs` (migration 0066). Tested with MLflow's real model server in `tests/test_ml_e2e.py`, and a training table in the real catalog in `tests/test_model_monitoring_e2e.py`.
- **Helm.** `cairn.mlflow.enabled: true` deploys MLflow (`ghcr.io/mlflow/mlflow`). It needs:
  - a PostgreSQL URL in a Secret (`cairn.mlflow.database.secretName`);
  - artifacts in Cairn Store (`cairn.mlflow.artifacts`), with a store key for that prefix only.

  A NetworkPolicy lets only Cairn's API reach it. The chart points the API at it (`CAIRN_MLFLOW_URL`).
- **Tested** (`tests/test_ml_e2e.py`, with a real MLflow 3.5 server, the MLflow client and `mlflow models serve`):
  - a person trained a scikit-learn model on a lakehouse table and registered it through Cairn;
  - the run was theirs although the client claimed another user;
  - the model was theirs, and appeared in the catalog with lineage from the training table;
  - another person could neither see, change nor take the model until a policy let them see it, and seeing still did not let them change it;
  - predictions went through Cairn only for the owner, or with `USE_ML_MODEL`.

### Managed model serving

A registered model version is deployed from Cairn, scaled with its load, and invoked as the caller (`/v1/ml/deployments`, `cairn.ml.serving`).

- **Deploying.** `POST /v1/ml/deployments` takes:
  - `name`, `model`, and `version` (a registered version number);
  - `min_replicas` (0 to scale to zero) and `max_replicas`;
  - `target_concurrency` (requests in flight per replica, default 2) and `idle_minutes` (10).

  It needs `MANAGE_ML_MODEL` on the model, or owning it. `PUT …/{name}` changes a deployment; a new version replaces its servers. `DELETE …/{name}` removes it. Unknown versions are refused (404).
- **Invoking.** `POST /v1/ml/deployments/{name}/invocations` takes MLflow's scoring format. It needs `USE_ML_MODEL` on the model, checked on every call as the caller, and the call is monitored and audited like any served model (`X-Cairn-Inference-Id`). A deployment is listed and described only to those who may use the model. Its servers listen on localhost, reached only through Cairn.
- **Scaling.**
  - Cairn counts the requests it has in flight to each deployment. Every `CAIRN_ML_SCALE_SECONDS` (10) it records the average concurrency.
  - It then scales the deployment to `ceil(concurrency / target_concurrency)` replicas, within the minimum and maximum: up at once, down one replica per tick.
  - After `idle_minutes` without a call it goes to `min_replicas`. At zero, the next call starts a replica and waits for it, up to `CAIRN_ML_COLD_START_SECONDS` (180), then answers 503.
  - Scaling is audited (`ml.model_deployed`, `ml.model_scaled`, `ml.model_undeployed`).
- **Runtimes.** Without a runtime, deployments are refused (503).
  - `CAIRN_ML_SERVING_RUNTIME=process` runs MLflow's own model server (`mlflow models serve` from
    `CAIRN_ML_SERVING_MLFLOW`, against `CAIRN_MLFLOW_URL`) as processes on the API's host, with
    `CAIRN_ML_SERVING_WORKERS` workers each. It therefore needs a single API replica.
  - `CAIRN_ML_SERVING_RUNTIME=kubernetes` (in Helm, `cairn.mlflow.servingKubernetes.enabled`) runs a Deployment,
    a Service and a NetworkPolicy per model deployment in `CAIRN_ML_SERVING_K8S_NAMESPACE` (`cairn-models`). Any
    number of API replicas may run.
    - **Replicas.** Cairn's scaling sets them.
    - **Pods.** They run MLflow's model server from `CAIRN_ML_SERVING_K8S_IMAGE`: MLflow's image, or one of your
      own with the libraries your models need. They fetch the version from Cairn's MLflow, which serves the
      artifacts, so they hold no store keys, no registry credentials and no Kubernetes token. They run as a
      non-root user without privileges.
    - **Who reaches them.** Only Cairn's API pods (`CAIRN_ML_SERVING_K8S_INGRESS_FROM`, by default the API pods
      in Cairn's namespace), so every prediction goes through Cairn's checks. MLflow's network policy admits the
      model servers so they can fetch their models.
    - **Rights.** Cairn's account gets a Role for those three kinds in that namespace only.
    - **Reaching the cluster from outside.** When Cairn runs outside the cluster, set
      `CAIRN_ML_SERVING_K8S_API`, `_TOKEN_FILE` and `_CA_FILE`, plus `_ACCESS=node-port`. Cairn then sends each request to a node that runs a ready model server (its Role may list the namespace's pods): the Service keeps the caller's address, so the network policy still admits only Cairn, and only such a node answers.
- **Tested** with a real MLflow server and MLflow's model server (`tests/test_ml_e2e.py`):
  - Someone without `MANAGE_ML_MODEL` could not deploy, and an unknown version was refused.
  - The deployment started at zero, and the first call started a replica and returned the model's predictions.
  - Someone without `USE_ML_MODEL` neither saw nor called it.
  - Eight clients in parallel scaled it to its maximum of two replicas, both serving.
  - Once idle, it went back to zero, and only the model's manager could delete it.
- **Tested on Kubernetes** (kind, `tests/test_ml_serving_k8s.py`):
  - A deployment applied with only the chart's Role started from zero on the first call, and its pod (no token, a
    user without privileges) answered with the model's predictions.
  - Another pod in the cluster could not reach it.
  - It served from two replicas, and went back to zero once idle.
  - The same account was refused in another namespace, and deleting the deployment removed its objects.

### Training on Spark

A Spark job's training runs are tracked in MLflow through Cairn, as the job's own Cairn identity
(`cairn_spark.mlflow`, in the `cairn/spark` image; elsewhere `pip install "cairn-spark[mlflow]"`):

```python
from cairn_spark import lakehouse_session
from cairn_spark.mlflow import use_mlflow, log_table_input

spark = lakehouse_session("churn-training")
mlflow = use_mlflow(experiment="churn")
with mlflow.start_run():
    log_table_input("sales.customers")   # lineage from the table to the model
    ...                                  # train, then mlflow.spark.log_model / mlflow.sklearn.log_model
```

- **Which identity.** `use_mlflow()` sets MLflow's tracking and registry to `<CAIRN_URL>/v1/ml`, with the same token
  `lakehouse_session()` uses: `CAIRN_TOKEN[_FILE]`, or the workload's platform token exchanged at Cairn
  (`CAIRN_WORKLOAD_TOKEN_FILE`). Runs, models and registered versions are that user's, under Cairn's rights on
  models, and audited.
- **No secrets exposed.** The token reaches MLflow only through its environment, never a command line or a URL.
- **Lineage.** `log_table_input(table)` records a lakehouse table as the run's input, so the model's lineage in
  the catalog starts there.
- **Tested** against a real MLflow through Cairn (`tests/test_ml_e2e.py`). A job given only `CAIRN_URL` and a token
  file was tracked as its user: the run, the registered model's owner, and lineage from the table. Without a token
  it was refused before reaching MLflow. Training with `pyspark.ml` inside the Spark image isn't run in CI yet.

## Fine-tuning on governed data

Cairn tunes a language model on examples read from your data and serves it, all inside Cairn.

**Setting up.**

1. Install the workers' libraries: CPU PyTorch from its own index,
   `pip install torch --index-url https://download.pytorch.org/whl/cpu`, then `pip install "cairn-platform[finetune]"`.
2. Put base models in place once: `cairn finetune-model download <hugging face id>`, for example
   `distilbert/distilgpt2`, into `CAIRN_FINETUNE_MODEL_DIR`. It takes the model's own top-level files only.

Jobs never download. Fine-tuning is on when `CAIRN_FINETUNE_MODEL_DIR` is set.

**A job.** `POST /v1/ml/finetunes` (`CREATE_FINETUNE`):

```json
{"name": "ticket-triage", "base_model": "distilbert/distilgpt2",
 "source": {"connection": "lakehouse", "sql": "SELECT subject, team FROM support.tickets"},
 "prompt_column": "subject", "completion_column": "team", "prompt_template": "Ticket: {prompt}\nTeam:",
 "epochs": 3, "learning_rate": 0.0005, "lora": true}
```

**The data.** The SQL runs through Cairn's query path as the person who asks, so their access, masks, row
filters and aggregation or projection policies shape the training set, as for any query of theirs. A job takes
at most 1000 rows, the query limit, and needs at least 10 examples.

Before training, emails, phone numbers, and card and account numbers left in the text are redacted. Set
`"redact_pii": false` only for data meant to hold them.

The job records the datasets the examples came from. Its owner can read the examples
(`GET .../examples`); others need `MANAGE_FINETUNE`.

**The training.** Each job trains in a process of its own:

- it runs `python -I`, with no network (Hugging Face offline) and none of Cairn's environment;
- it trains a LoRA adapter by default, or the whole model with `"lora": false`;
- it records each epoch's loss.

The status goes `queued` → `running` → `done` or `failed`, with the worker's error.

**Using it.** `POST /v1/ml/finetunes/<name>/generate {"prompts": [...], "max_new_tokens"}` needs `USE_FINETUNE` on
`urn:li:finetune:<name>`; its owner has it. Anyone else also needs read access (`VIEW_DATASET_PROFILE`) to every dataset the model was
trained on, decided at each call, since the model and its examples hold what its owner could read; the same holds
for reading the examples. Each call is audited with the number of prompts, never their text.
`DELETE` removes the model and its examples.

Tested with real training: LoRA on PyTorch CPU on `distilbert/distilgpt2`, with examples read through Cairn's
governed query path from a real database (`tests/test_finetune.py`, in the ml job). The test checked:

- PII was redacted, and the sources were recorded;
- the epoch loss fell to under 30% of where it started;
- the tuned model answered the taught teams;
- the worker ran offline with no Cairn variables;
- use was refused without `USE_FINETUNE`, and audited without the prompts;
- bad requests were refused.

## Delta Lake tables

Trino's `delta` catalog keeps Delta Lake tables in Cairn Store, with its metastore files beside them (`s3://<bucket>/<prefix>/`, default `s3://lakehouse/delta/`).

- **Governed like the lakehouse.** A Delta table `delta.<schema>.<table>` is the `delta-lake` dataset `<schema>.<table>`. Cairn's access, masks, row filters and write rights apply to it, as to any Trino table.
- **Delta's own features.** History (`"orders$history"`), time travel (`FOR VERSION AS OF`), `CREATE TABLE AS`, inserts, updates, deletes and `MERGE`.
- **Tables other engines wrote.** Spark and Databricks Delta tables, for example. Register one by its location: `CALL delta.system.register_table('schema', 'table', 's3://…/table')`. `unregister_table` forgets one and leaves its files.
- **Helm.** Set `cairn.trino.delta.enabled`, and `credentialsSecret` (`access-key`, `secret-key`) with a store key that can reach only the Delta prefix. The chart sets the catalog's mapping on the API.
- **Several writers.** Each commit writes the next log entry only if it does not exist yet (`If-None-Match: *`, `s3.exclusive-create`), which Cairn Store checks atomically. Several coordinators, clusters or other engines that commit the same way can write one table at once: a writer that loses the race retries at the next version, and nothing is lost or overwritten. Engines that cannot write conditionally (older Spark Delta releases on S3) must not write the same tables.
- **Converting to Iceberg, in place.** `POST /v1/delta/convert` (`MANAGE_SYSTEM_OPERATIONS`) with `location` (the Delta table's `s3://…` location) and `target` (`namespace.table`) makes an Iceberg table in Cairn Catalog **at the same location**, with the Delta table's live Parquet files at its latest version. No data is copied, and the catalog's per-table credentials cover the files.
  - `GET /v1/delta/inspect?location=…` first says what would be converted, or why it cannot be.
  - Cairn reads the Delta log from the store: the latest checkpoint, then the commits after it. The table records `cairn.converted-from` and `cairn.delta-version`.
  - **One way.** The files now belong to the Iceberg table, and its maintenance may delete them as it changes. Retire the Delta table first: `CALL delta.system.unregister_table('schema', 'table')`. If the Delta log moves during conversion, the new table is dropped and the request refused.
  - **Partitioned tables.** Delta keeps partition values in its log, not in the files. The Iceberg table is partitioned by the same columns (identity), and each file gets its partition values from the log, nulls included. Readers take those columns from the partition, and filters on them prune files. Supported partition column types: string, integer, long, short, byte, double, float, boolean, date, timestamp and decimal.
  - **Deletion vectors.** Cairn reads each file's deletion vector from the log (inline, or in its `deletion_vector_<uuid>.bin`). It checks that the count of deleted rows matches the log's, then writes that file again without its deleted rows, under `<location>/cairn-converted/`. Files without deletion vectors are still adopted as they are, without copying. `inspect` reports `partitioned_by` and `files_with_deletes`; `convert` reports `rewritten_for_deletes`.
  - **Refused:** column mapping (files whose column names are not the table's), and partition columns of other types. Copy those with a derived table (`SELECT * FROM delta.<schema>.<table>`).
  - **Settings.** `CAIRN_DELTA_STORE` (a key that reads the Delta tables) and `CAIRN_DELTA_CATALOG`, defaulting to the pipes' store and catalog.
- **Tested** with Trino and Cairn Store (`tests/test_trino_e2e.py`). A Delta table was created from the lakehouse and written. Masks, row filters and a refused write applied. History and time travel worked. After the table was unregistered, it was registered again by its location and still governed.
  - **Two clusters at once.** Two Trino clusters inserted into one Delta table concurrently, twelve inserts in all. Every row arrived, and the log had one version per insert after the table's creation.
  - **Conversion.** A Delta table with a checkpoint, an insert, an update and a delete was unregistered and converted. Trino read the same rows from the Iceberg table, which then took an insert. Converting again to the same name and a location with no Delta log were refused.
  - **Partitions and deletes.** A Delta table partitioned by a text and a date column (one row with no region), with rows deleted by deletion vectors Trino wrote, was converted. The decoded vectors held exactly the deleted rows. The Iceberg table returned the same rows, was partitioned the same way (a filter on the partition read only its files), and only the files with deletes were written again. It then took an insert. The Roaring layouts a vector may use (array, bitmap and run containers, positions past 2³²) are checked too.

## Functions in SQL: AI and Python

Trino runs two kinds of functions beyond SQL's own, both under Cairn's policies.

- **AI functions.** Trino's `ai` connector, as the catalog `llm`: `llm.ai.ai_gen`, `ai_classify`, `ai_analyze_sentiment`, `ai_extract`, `ai_mask`, `ai_translate` and `ai_fix_grammar`. For example: `SELECT llm.ai.ai_classify(comment, ARRAY['bug', 'praise']) FROM support.tickets`.
  - **Who may call them.** They send rows to a model outside the lakehouse, so calling one needs Cairn's `USE_AI_FUNCTIONS`, granted like any privilege. Others are refused before anything is sent. The catalog crawler never calls them. `CAIRN_TRINO_AI_CATALOGS` names the AI catalogs (default `llm`).
  - **What the model sees.** What the caller sees: masks and row filters apply before any function runs, so a masked column reaches the model masked.
  - **Through Cairn** (default, `cairn.trino.ai.viaCairn`). Trino calls the model through Cairn (`/v1/trino/<key>/ai/...`), and only Cairn has the provider's key (`CAIRN_AI_SQL_API_KEY`). Cairn:
    - allows only `model` and `allowedModels`;
    - counts every call against the deployment's daily AI budget (`CAIRN_AI_DAILY_BUDGET_USD`, shared with Cairn's own AI features);
    - caps calls a minute (`callsPerMinute`, per API replica);
    - records tokens and cost in AI usage as `trino-sql`.

    Trino's connector does not say who a call is for, so tokens are counted for the deployment as a whole. Per person, Cairn admits at most `userDailyQueries` AI-function queries a day, and the person's own token allowance (`CAIRN_AI_USER_DAILY_TOKENS`) applies. Each query is recorded under the person, so the usage report shows who used AI functions and how often. When a limit is reached, the query fails and nothing reaches the model. Trino reports "request to AI provider failed"; Cairn's log says why. The connector also keeps answers to prompts it has already seen, so a repeated prompt is not sent again.
  - **Helm.** Set `cairn.trino.ai`: `enabled`, `provider` (`anthropic`, or `openai` for any OpenAI-compatible endpoint such as Ollama or vLLM), `model`, an optional `endpoint`, and `apiKeySecret`.
- **Python functions.** Inline, per query, sandboxed in WebAssembly by Trino:

  ```sql
  WITH FUNCTION domain(e varchar) RETURNS varchar LANGUAGE PYTHON WITH (handler = 'domain')
  AS $$
  def domain(e):
      return e.split('@')[-1]
  $$
  SELECT domain(email) FROM sales.orders
  ```

  They run on the caller's rows, after masks and filters, and cannot reach files or the network.
- **Stored functions.** SQL and Python functions created once and called anywhere: `CREATE FUNCTION routines.<schema>.<name>(...) RETURNS ... RETURN ...` (or `LANGUAGE PYTHON`). They live in the `routines` catalog (Trino's Hive connector, with a file metastore in the store; Helm: `cairn.trino.routines.enabled` and `credentialsSecret`).
  - **Who may call one.** Calling one needs `USE_FUNCTION` on `urn:li:function:routines.<schema>.<name>` (resource type `function`), granted per function like any privilege. Creating or dropping one needs `MANAGE_DATASET`.
  - **What it sees.** Masks and row filters apply before any function runs. A SQL function cannot query tables, so it never reaches more than its caller passes it.
  - **Settings.** `CAIRN_TRINO_ROUTINE_CATALOGS` names the catalogs that hold stored functions (default `routines`).
  - **In the catalog.** Each stored function is an asset people can find, document and own. It is a job with subtype `Function` in the flow `(trino,routines,PROD)`, with:
    - its signatures (every overload), return types, language, body and comment;
    - `function_urn`, the `urn:li:function:…` that `USE_FUNCTION` policies name.

    Trino's query events keep it current. A finished `CREATE [OR REPLACE] FUNCTION` publishes it, owned by its creator; replacing it keeps the owner. `DROP FUNCTION` drops that signature, and the asset is removed with its last one.
  - **Sync.** `POST /v1/trino/functions/sync` (`{"connection": …}`, `MANAGE_INGESTION`) lists the routines catalogs' functions as Cairn's crawler (`SHOW FUNCTIONS`). It adds those Cairn was not told of and removes those that are gone. A function Cairn already knows keeps the body it learned.
  - **Tested** with Trino (`tests/test_trino_e2e.py`): a created function appeared with its comment, body and creator; an overload joined it, and dropping it left the other; a function made while Cairn was not listening came in with the sync (refused to someone without `MANAGE_INGESTION`); dropped functions were removed. The statements as Trino receives them, in `tests/test_trino_functions.py`.
- **Procedures** (`/v1/procedures`). A named list of SQL statements with typed parameters (`integer`, `double`, `varchar`, `boolean`, `date`; `:name` in a statement), on a Trino connection.
  - **Calling one.** `POST /v1/procedures/<name>/call` with `{"args": {...}}`. Each argument is checked against its type and bound as a SQL literal, never spliced in as text. The statements run in order **as the caller**, so Cairn's policies apply to every statement. A failing statement stops the call; what ran before it stays. The last statement's rows come back, up to 1,000.
  - **Who may call one.** Its owner, and those allowed `EXECUTE_PROCEDURE` on `urn:li:procedure:<name>`. A procedure never runs with its owner's rights: it does only what the caller could do anyway. Every call is audited with its arguments.
  - **Checked when saved.** Each entry is one statement, and only declared parameters are used.
- **Tested** with Trino (`tests/test_trino_e2e.py`, against a stand-in for the model's API; the decisions in `tests/test_trino_access.py`):
  - **AI functions.** A user without `USE_AI_FUNCTIONS` was refused and nothing was sent. With it, the model was sent her masked email, never the raw one.
  - **Through Cairn.** The provider received Cairn's key, never Trino's. The call was in Cairn's AI usage, with her query under her name. Her daily cap, a model not allowed, and the spent budget each stopped the query, and nothing reached the model. The rate limit, Anthropic usage and an unreachable provider are covered in `tests/test_sql_ai_gateway.py`.
  - **Python functions.** One ran on masked values for her, and on real values for the admin.
  - **Stored functions.** A stored SQL function and a stored Python function ran for the admin. Ana was refused both until she was given `USE_FUNCTION` on one. That one then ran on her masked emails, and she was still refused the other and creating her own.
  - **Procedures.** A three-statement procedure replaced a row and returned totals. A hostile string argument was stored as a value, not run as SQL. A bad date and an undeclared parameter were refused. Ana could not call it without `EXECUTE_PROCEDURE`; with it, her first statement was refused because she may not write the table, and nothing was written.

## SQL workspace

**SQL** (`/sql`) is where people write and run read-only SQL against any connection that serves catalog datasets, as themselves.

- **The same checks as a dataset's page.** Queries go through Cairn's data access service. Every table is checked for the person, masked columns are masked, and on a Trino connection with `run_as_user`, Trino's row filters apply too. A query that writes, or reads a table the person may not query, is refused.
- **Schema browser.** Lists the connection's datasets the person may see, with their columns. Double-click a table to insert its name.
- **Results.** A table, a chart and a CSV download. Ctrl+Enter runs the query. Charts: bars (side by side or stacked), lines, areas, points and pies of one or more numeric y columns against an x column; one big number; a **heatmap**, one value (y) coloured by two columns, x across and `series` down; and a **map**, rows placed at their `lat` and `lon` columns (degrees), sized by y and named by x. A click on a bar, cell or point filters the dashboard's other tiles.
- **Map tiles.** Without a tile server, a map draws its points on a plain grid, and browsers fetch nothing from elsewhere. Set `CAIRN_MAP_TILES_URL` to your tile server's `https://…/{z}/{x}/{y}.png` and `CAIRN_MAP_ATTRIBUTION` to the credit its licence asks for, and maps draw on those tiles; anything but an `https://` URL with all three placeholders stops the API at start. Viewers' browsers then load the tiles from that server, so the web UI's content security policy must allow it (`img-src`).
- **On a phone.** Narrower than 768 pixels, a dashboard's tiles stack in one column, charts are drawn for the narrow screen, wide tables scroll sideways inside their tile (the page itself never does), and a map with up to 20 points names them on the map, since there is no hover. Embedded dashboards scroll wide tables the same way. Tested in Chromium as an iPhone against Cairn (`tests/test_sql_ops_ui_e2e.py`).
- **Autocomplete.** As you type a name or a dot (or with Ctrl+Space), the editor suggests what may come next: tables and schemas after `FROM` and `JOIN` (and after a comma in `FROM`), a schema's tables after `schema.`, a table's or alias's columns after `name.`, and elsewhere the columns of the tables the query reads (qualified with their table or alias when two share a name), then functions and keywords. Suggestions come only from the tables the person may query (`GET /sql/schema`), never inside strings or comments; names that are not plain identifiers are quoted. Arrows move, Enter or Tab takes one, Escape closes (`ui/src/consumer/sql/complete.ts`; in Chromium against Cairn in `tests/test_sql_ops_ui_e2e.py`).
- **Workbooks** (**SQL → Workbooks**, `/sql/workbooks`): explore a table like a spreadsheet, without writing SQL. Pick a table the person may query, then add:
  - **calculated columns** with spreadsheet formulas: columns in square brackets, text in quotes, `+ - * / %`, `&` to join text, comparisons, `And`/`Or`/`Not`, and functions: `If`, `Coalesce`, `IsNull`, `Between`, `In`; `Abs`, `Round`, `Floor`, `Ceiling`, `Power`, `Sqrt`, `Ln`, `Number`; `Text`, `Upper`, `Lower`, `Trim`, `Len`, `Left`, `Right`, `Mid`, `Contains`, `StartsWith`, `Replace`, `Concat`; `Today`, `Now`, `Date`, `DateTrunc`, `DatePart`, `DateDiff`, `DateAdd`, `Year`, `Month`, `Day`. Calculated columns may use the ones before them; division never truncates and never fails on zero (it gives empty);
  - **filters** (`=`, `≠`, `>`, `≥`, `<`, `≤`, is one of, is none of, contains, starts with, between, is empty, is not empty), on rows or, ticked "on groups", on grouped values;
  - **grouping** and **aggregates** (`Sum`, `Avg`, `Min`, `Max`, `Count`, `CountDistinct`, `CountIf`, `SumIf`, `Median`, `Percentile`, `StdDev`, and expressions of them); a column used outside an aggregate must be grouped;
  - a **pivot**: one column per value of a column (the values found in the data as the person sees it, up to 24), each the chosen aggregate over that value's rows;
  - sort, and up to 10,000 rows; a CSV download.
  The element is compiled to one SQL query and run through the SQL workspace as the person looking: their policies, masks and row filters apply, and a column computed from a masked one is masked too. The page shows the SQL it ran. Formulas never reach SQL as written: they are parsed, columns must exist, text becomes escaped literals and only the listed functions produce SQL. Workbooks are saved with a name and shared or not (`GET/POST /sql/workbooks`, `PUT/DELETE /sql/workbooks/<id>`, owners only change theirs); a shared workbook shows each viewer only what they may see. `POST /sql/workbooks/compile` returns the SQL for an element and `POST /sql/workbooks/run` runs it. Table `cairn_workbooks` (migration 0073). Tested: the compiler (`tests/test_workbooks.py`), on real Trino as three people (`tests/test_trino_e2e.py`), and in Chromium (`tests/test_sql_ops_ui_e2e.py`).
- **Metrics** (**SQL → Metrics**, `/sql/metrics`): a business measure defined once, so "Revenue" is the same number in every dashboard and query. A metric names a measure over one table: `name`, `label`, `connection`, `table`, `measure` (a workbook aggregate formula, such as `Sum([amount])`), its own `filters` (the measure counts only those rows, such as `status = 'paid'`), an optional `time_column`, and `dimensions` (the columns it may be broken down by; none listed means any). Saving compiles the definition over the table as its owner sees it, so a measure that is not an aggregate, or a column that does not exist, is refused.
  - **Querying:** `POST /sql/metrics/query` with `{"metrics": [...], "dimensions": [...], "grain": "day|week|month|quarter|year", "filters": [...]}`. The metrics in one query read the same table and connection; each keeps its own filters, so several metrics are computed side by side in one query. It compiles to one workbook element and runs as the person asking: their policies, masks and row filters apply. Dashboards show metric tiles (`{"metric": {...the query...}}`; controls and clicks on the metric's table's columns filter them), and a change to a definition changes every tile that uses it.
  - **Synonyms:** a metric may list up to 10 `synonyms` (other words people use for it); Ask-your-data and the AI builder are told them.
  - **Semantic model autopilot** (`POST /sql/semantic-models` with `connection` and up to 8 `tables`; `use_model: false` to skip the model): Cairn drafts a semantic model instead of anyone writing metrics by hand. Every table must be one the person may see (otherwise 404). Each is profiled by one governed query run as the person (rows, non-null and distinct counts, smallest and largest values), so masks and row filters apply and a column that comes back masked is neither a dimension nor a measure. Query history of the last `CAIRN_AUTOPILOT_HISTORY_DAYS` days (90) is parsed for its shape only: the aggregates over the tables' columns with the equality filters they ran with, group-bys and join keys, each counted. A filter value from history is kept only if the person's own read of that column holds it. Candidates: a row count per table, each aggregate history ran (ranked by runs), a sum of each numeric non-key column; dimensions from group-bys and columns with few values; a time column; relationships from joins. With an LLM configured, the model names the candidates it keeps (name, label, description, up to five synonyms, dimension descriptions); it refers to candidates by id only and never writes a formula, and it is shown no SQL text and no user. Every proposal is then run as the person (its total, shown as `total`), and refused ones are listed with why. A draft is visible only to its author (`GET /sql/semantic-models`, `GET`/`DELETE …/<id>`). `POST …/<id>/accept` with `{"metrics": [names], "edits": {name: {...}}}` saves the chosen proposals as the person's metrics, with the table's time column and dimensions, each checked again as it is saved; an existing name is skipped with 409. Audited: `semantic_model.drafted` (tables and counts, no values) and `semantic_model.accepted`. Tested on real Trino with query history from Trino's event listener and a scripted model (`tests/test_trino_e2e.py`).
  - **Ownership and trust:** anyone defines a metric. Only its owner or an operator (`MANAGE_SYSTEM_OPERATIONS`) changes or deletes it. Only an operator certifies it (`POST /sql/metrics/<name>/certify`), and changing a certified metric withdraws the certification until it is certified again. Each change bumps `version`. Audited (category `sql`): `metric.saved`, `metric.certified`, `metric.deleted`.
  - **Over SQL, for Tableau, Power BI and other tools** (`POST /sql/metrics/<name>/publish`, `DELETE …/publish`; the
    metric's owner or an operator): the metric becomes a Trino view, `metrics.<name>` (`CAIRN_METRIC_VIEW_SCHEMA`), of
    its measure per listed dimension and per day (`period`), created `SECURITY INVOKER` on the metric's connection
    as the person publishing (Cairn's policies decide whether they may create views in that schema; create the
    schema once). A BI tool signed in to Trino as a person (OAuth2 through Cairn, or a Cairn token) sums it up and
    gets that person's number, the same as a dashboard: the view runs as them, so their row filters and masks
    apply, and Cairn decides reads of the view on the terms of the metric's table (whoever may read the table may
    read its metric's view; nobody else). Only measures that stay right when summed again are published (`Sum`,
    `Count`, `SumIf`, `CountIf`); an average or a distinct count is refused, as a BI tool cannot add up per-day
    values of those. The view follows each change of the definition (or is dropped if it can no longer be one) and
    is dropped with the metric. Audited (`metric.published`, `metric.unpublished`). Migration 0091. Tested on real
    Trino: the admin's and Bob's sums over the view equal Cairn's metric query for each (Bob's row filter applying
    inside it), a changed definition changed it, and deleting the metric dropped it (`tests/test_trino_e2e.py`).
  - **Metric alerts** (`/sql/metric-alerts`): a metric watched per `grain` (day, week, month, quarter), optionally
    filtered (`filters`), with a `rule`: `change` (the last complete period against the one before: `drop_pct`,
    `rise_pct`), `range` (against the usual range of the `periods` before it, median ± `band` robust standard
    deviations, quiet until 4 exist) or `threshold` (`op`, `value`). The period still filling is never judged. Each
    check (every `every_minutes`, on the scheduler; or `POST …/<id>/check`) runs the metric as the alert's owner,
    under their policies and row filters. Only changes of state are told, to the owner and `recipients`
    (`metric.alert_triggered` once when it goes wrong, `metric.alert_resolved` once when it recovers), so a metric
    that stays out of range is reported once. Only the owner sees, changes or checks an alert. Audited
    (`metric_alert.saved`, `.triggered`, `.ok`, `.deleted`). Table `cairn_metric_alerts` (migration 0090). Tested on
    real Trino: US revenue down 25% week on week told the admin once over three checks and once on recovery, while
    Bob's same alert, under his EU-only row filter, stayed quiet (`tests/test_trino_e2e.py`).
  - Workbook aggregates take the same per-aggregate filters (`{"name", "formula", "where": [filters]}`). Table `cairn_metrics` (migration 0076). Tested: the compiler (`tests/test_workbooks.py`), on real Trino as three people, with a dashboard tile following a change of definition (`tests/test_trino_e2e.py`), and the page (`metrics.test.tsx`).
- **Build from a question** (**Dashboards → Build from a question**, `POST /sql/builder` with `question`, optional `datasets`, `save` and `name`): the AI builder turns a question into a dashboard. It needs an LLM provider (as Ask-your-data). The model is given the metrics whose tables the person may see and the tables Ask-your-data would choose (only those they may see), and proposes tiles (metric queries or workbook elements) and filter controls as structured output; it never writes SQL. Every tile is compiled as the person asking: a metric not listed, a dimension the metric does not allow, a column the table lacks or a table they may not see is refused, the model is told why and gets one more try, and tiles still refused are listed (`refused`), not shown. The preview runs every tile as the person, and `save` keeps it as their own private dashboard. Audited as `bi.built` (the question redacted). **Ask-your-data** is also given the metrics layer's definitions for the tables it uses, as approved metrics (`definitions` in its answer). Tested on real Trino with a scripted model (`tests/test_trino_e2e.py`) and in the page (`dashboards.test.tsx`).
- **Dashboard actions** (buttons on a dashboard, `/sql/dashboards/<id>/actions`): forms and buttons that change things, run as the person who clicks. The dashboard's owner adds them; everyone who may open the dashboard sees and runs them (`POST …/actions/<action>/run` with `inputs` and, for a row action, the `row` picked in a table tile).
  - `input_row`: a form that adds a row to an input table (`fields`: the columns it asks for, `constants`: fixed values). `input_update`: a button that changes the picked row (`set`: fixed values such as `{"status": "approved"}`, optional `fields`), with the row's `_updated_at` as read, so a change made since is refused (409). Both are written on Trino as the person clicking, so Cairn's policies decide: someone without `WRITE_DATASET` is refused.
  - `workflow`: runs a SQL workflow. Workflows run as their owner, so only the workflow's owner (or an operator) adds one, and `who` (`owner`, the default, or `viewers`) says who may click it. The person clicking sees each step's status, not its rows.
  - `webhook`: posts the form and who sent it to an API. Only an operator adds one, and only to a URL starting with an origin in `CAIRN_ACTION_WEBHOOK_ORIGINS` (comma separated, such as `https://hooks.example.com`); redirects are not followed, and the URL is not shown to viewers. With `CAIRN_ACTION_WEBHOOK_SECRET` (or `CAIRN_ACTION_WEBHOOK_SECRET_FILE`) each body is signed: `X-Cairn-Signature: sha256=<hex HMAC-SHA256 of the body>`.
  - Inputs are only the fields an action declares, checked against their types. `confirm` sets a question asked before it runs. Each run is kept for the dashboard's owner (`GET …/actions/<action>/runs`: who, `ok`, `refused` or `error`, the time; not the values) and audited (`bi.action_added`, `bi.action_run`, `bi.action_removed`). Tables `cairn_bi_actions` and `cairn_bi_action_runs` (migration 0077). Tested on real Trino as the owner and a reader (`tests/test_trino_e2e.py`) and in the page (`dashboards.test.tsx`).
- **Result cache** (dashboards, workbooks, metrics; never the SQL editor): a result is kept under its query and a policy fingerprint: for each table read, whether the person may read it, the row filters and masks Trino applies to them and the masks Cairn applies to results. Two people share a cached result only when those decisions are the same for every table (a row filter naming the person, `{user}`, makes it theirs alone); when a decision cannot be made nothing is cached. The fingerprint is worked out on every lookup, so a policy change takes effect at once. A result is dropped when a table it read changes (pipelines, dbt, derived tables and input tables record their writes) and after `CAIRN_RESULT_CACHE_SECONDS` (default 600; `0` turns the cache off); results over `CAIRN_RESULT_CACHE_MAX_BYTES` (default 2 MB) are not kept. A hit is audited as `data.queried` with `cached`. `POST …/render` with `"fresh": true` runs every tile again. Table `cairn_result_cache`.
- **Scheduled exports** (**Dashboards → Schedule export**, `/sql/reports`): `{"name", "target": {"dashboard": id} | {"workbook": id}, "format": "csv" | "pdf", "every_minutes": 60 to 43,200, "recipients": [people]}` (the owner always gets a copy). Each recipient's copy is rendered as that recipient, so their masks and row filters apply; a recipient who may not open the dashboard or workbook gets nothing and the run lists them as skipped. Copies are kept 7 days and downloaded only by their recipient (`GET /sql/reports/files/<id>`); the recipient is told (event `report.ready`), by each channel they have turned on: email carries the file as an attachment (`?starttls=false` on an `smtp://` URL for a relay on the same host); **Slack** uploads it into the recipient's own direct messages (the bot needs the `files:write` scope as well as `chat:write`); **Teams** (a person's own Workflows webhook) gets a card whose link opens their copy, since Teams webhooks cannot carry files. Each copy goes only to its own recipient: a notification never carries another person's file, and a shared Slack or Teams channel (a route) is told that copies are ready but never given one, because one file cannot be each member's own view. Links are absolute (`<public URL>/cairn/api/sql/reports/files/<id>`) and open only for the signed-in recipient. Runs are claimed so one replica sends each; the notifier's scheduler runs them, and `POST /sql/reports/<id>/run` sends one now. Audited: `report.saved`, `report.sent`, `report.deleted`. Tables `cairn_report_schedules` and `cairn_report_files` (migration 0078). Tested on real Trino as four people, with Mailpit receiving the email (`tests/test_trino_e2e.py`), the files (`tests/test_reports.py`), Slack delivery against a stand-in for Slack's Web API (`tests/test_report_delivery.py`: each copy in its own recipient's DM byte for byte, none to someone who may not open the dashboard, none to the shared channel) and the page (`dashboards.test.tsx`).
- **Input tables** (**SQL → Input tables**, `/sql/input-tables`): lakehouse tables people type into: targets, forecasts, mappings, approvals. `POST /sql/input-tables` with a Trino connection that runs as the user (`run_as_user`), `table` (`schema.table`) and `columns` (`text`, `number`, `integer`, `boolean`, `date`, `timestamp`) creates an Iceberg table with those columns plus `_row_id`, `_updated_by` and `_updated_at`, and puts it in the catalog owned by its creator (property `cairn_input_table`), so workbooks, the SQL editor and dashboards read it at once. In the page each cell is edited in its column's type; **Add row**, **Save**, **Delete selected**.
  - **Governed as the person.** Every statement (`CREATE TABLE`, `INSERT`, `UPDATE`, `DELETE`) runs on Trino as the person, so Trino asks Cairn: creating needs `MANAGE_DATASET` on the new table, writing needs `WRITE_DATASET`. Reads (`GET …/<id>/rows`) go through governed data access: masks and row filters apply, masked columns are shown read-only. Values are checked against their column's type and written as typed literals, never as SQL; at most 500 rows per request.
  - **No lost edits.** A change (`PATCH …/<id>/rows/<row_id>` with `values` and `expected_updated_at`, the row's `_updated_at` as read) writes only if nobody changed the row since; otherwise nothing is written and the answer is 409 (**Read again** in the page).
  - **Audited** (category `sql`): `input.created`, `input.rows_added`, `input.row_changed`, `input.rows_deleted` and `input.forgotten`, with the row ids and columns changed, not the values. Each commit is a new snapshot of the table, so the change feed, derived tables and materialized views follow.
  - `DELETE /sql/input-tables/<id>` (its creator or an operator with `MANAGE_SYSTEM_OPERATIONS`) stops listing it as an input table; the data stays in the lakehouse. Table `cairn_input_tables` (migration 0074). Tested on real Trino as three people (`tests/test_trino_e2e.py`), the typed values (`tests/test_input_tables.py`) and the page (`ui/src/consumer/__tests__/inputTables.test.tsx`).
  - **Review before changes land.** The table's creator or an operator sets reviewers
    (`PUT /sql/input-tables/<id>/approval`, `{"reviewers": ["urn:li:corpuser:…", "urn:li:corpGroup:…"]}`; `[]`
    turns review off).
    - **Waiting.** Every added, changed or deleted row is then a change request (202 with its id). It is
      checked at once (columns, types) and written only when a reviewer approves it.
    - **Four eyes.** Nobody approves their own change, so a reviewer's own change needs another reviewer.
    - **Written as the author.** An approved change is written on Trino as its author, so their policies
      decide and `_updated_by` is theirs. A row edited meanwhile makes the change `failed`, with why, instead
      of overwriting it. Rejected changes are never written.
    - **API.** `GET …/<id>/changes` lists changes (`?state=pending`); reviewers see all of them, authors their
      own. `POST …/changes/<cid>/approve` and `POST …/changes/<cid>/reject` (with a `note`) decide.
    - **Notifications.** Reviewers named as people are told of new changes (`input.change_requested`). Groups
      are not expanded for notifications. Authors are told the decision (`input.change_decided`).
    - Each step is audited. A change being decided while Cairn stops stays `deciding`; an operator can see it
      in the list.
    - Tables `cairn_input_approval` and `cairn_input_changes` (migration 0087). Tested on real Trino
      (`test_input_table_changes_wait_for_a_reviewer_and_land_as_their_author`).
- **History.** Each person's last 500 runs, including refused ones and why (`GET /sql/history`).
- **Saved queries.** Private, or shared with everyone. A shared query runs as whoever opens it, never as its owner, so each person sees only what they may (`/sql/saved`, `POST /sql/saved/{id}/run`). Only its owner changes or deletes it. A saved query keeps its chart.
- **Who.** People only: service, agent and extension tokens are refused.
- **Dashboards** (**Dashboards**, `/sql/dashboards`). A named, optionally shared set of saved queries, shown as their charts or tables. Opening one runs every tile as the viewer. A tile the viewer may not see shows the refusal, not the data. Each tile must be a query its owner may open. Only the owner changes or deletes a dashboard.
  - **Tiles** are saved queries (`{"query": id}`) or workbook elements (`{"element": {...}, "connection": c}`, saved workbooks in the page, one tile per element). Each tile has a chart: `table`, `bar`, `stacked_bar`, `line`, `area`, `scatter`, `pie` or `number` (one big value).
  - **Controls** (up to 12, `controls` on the dashboard): `{"id", "label", "column", "kind"}` with `kind` `values` (pick from a list: the column's values in `source` `{connection, table}`, as the viewer sees them, `GET /sql/dashboards/<id>/controls/<control>/values`), `range`, `date_range` or `text` (contains). A control filters every tile that has its column, or only the tiles in `targets` (positions from 0); `default` is its starting value. Tiles say which filters applied (`applied`).
  - **Cross-filter:** a click on a value in a chart filters the other tiles with that column (the value shows as a tag; close it to clear). **Drill:** an element tile with `drill` (columns from the top down, such as `["region", "country", "city"]`, the element grouped by the first) offers **Drill into <value>** after a click and **Up**.
  - `POST /sql/dashboards/<id>/render` with `{"controls": {id: value}, "selection": [{column, value, from}], "drill": {"<tile>": [values]}}` renders so filtered; `GET …/render` renders with the defaults. Filters reach SQL only as quoted columns and literals: a saved query runs wrapped (`SELECT * FROM (<query>) AS tile WHERE …`), an element gets them as its own filters. Everything runs as the viewer, so their row filters and masks apply under every control and click. Column `controls` on `cairn_dashboards` (migration 0075). Tested: on SQLite (`tests/test_sql_workspace.py`), on real Trino as two people with drill and a values control (`tests/test_trino_e2e.py`), the page (`dashboards.test.tsx`) and in Chromium (`tests/test_sql_ops_ui_e2e.py`).
- **Alerts** (`/sql/alerts`). A saved query checked every `every_minutes` (5 minutes to a week), as its owner. The condition is `rows` (the number of rows) or a column of the first row, compared with a number (`>`, `>=`, `<`, `<=`, `==`, `!=`).
- **Workflows** (`/v1/sql-workflows`). Scheduled SQL in the product: named steps that depend on each other (`after`; cycles are refused), run on a `cron` (UTC, at most every five minutes) or with **Run now**, as the workflow's owner. Steps can be:
  - `derived`: refresh a derived table (`full` for a rebuild). Only its owner, or an operator, may schedule it; this is how SQL that writes runs on a schedule.
  - `check`: a saved query's `rows`, or a column of its first row, compared with a value. It fails the step when the condition does not hold (a quality gate between steps).
  - `query`: a saved query, or `sql` on a `connection`, run as the owner. Its row count and first 20 rows are kept with the run and sent to the recipients (a scheduled report).

  A step whose dependency failed is skipped, and `retries` (0-3) retries a failing step. Every run is kept, with each step's status, rows, error, attempts and time. The owner and `recipients` are told when a run fails (`sql.workflow_failed`), or after every run with `notify: always` (`sql.workflow_finished`), on their notification channels. Queries go through the SQL workspace, so the owner's policies, masks and row filters apply. Several scheduler replicas claim each due run once.
  - **In the UI.** **SQL workflows** (`#/sql/workflows` in the workspace) lists a person's workflows with their schedule, next run and last outcome. Its editor builds the steps (a table refresh, a check, a report), what each comes after and its retries; **Run now** runs one, and each run shows every step's outcome.
  - **Tested** in `tests/test_sql_workspace.py` (order, masks, a failing check skipping its dependents, notifications, the cron, checked definitions) and `tests/test_trino_e2e.py` (a derived-table refresh then a check, on real Trino).
  - **Who is told.** The owner and the alert's recipients are told when the condition starts to hold (`sql.alert_triggered`), when it stops (`sql.alert_resolved`), and once when the query fails. They hear about changes, not every check, on their notification channels.
  - **Where checks run.** The scheduler's leader runs them (`cairn scheduler`), so there is one check per alert however many replicas run. **Check now** runs one at once.
- **Tested** (`tests/test_sql_workspace.py`, and against Trino in `tests/test_trino_e2e.py`):
  - masking, refusals and history per person;
  - saving, sharing, running a shared query as another person (who got their own row filter), and owner-only changes;
  - a schema browser that left out what the person may not see;
  - a shared dashboard rendered for its owner and for someone refused one of its tiles, who saw that tile's refusal and the other tile's data;
  - an alert that stayed quiet on its first fine check, was told once (to its owner and recipient) when the condition began, again when it stopped, and once when its query failed. It was not checked before it was due.

### BI assets in the catalog, and the impact of a change

In the web UI: a dataset's page has a **BI impact** tab, with what breaks if the table or a chosen column goes.


Every save publishes the asset to the catalog. A saved query or a workbook becomes a **chart**
(`urn:li:chart:(cairn,query.<id>)`, `…workbook.<id>`), with `chartInfo.inputs` naming the tables it reads and
`inputFields` naming the columns. A dashboard becomes a **dashboard** (`urn:li:dashboard:(cairn,<id>)`), listing its
charts and tables. The catalog's lineage therefore runs from a table to the dashboards over it. Deleting an asset
marks its entity removed. Ownership and the asset's link come along.

**Impact before a change.** `GET /sql/impact?dataset=<urn>&column=<name>` lists the saved queries, workbooks and
dashboards that break if that column goes; without `column`, the ones that read the table at all.

* A dashboard is listed when one of its tiles uses an affected query (`through_queries`) or reads the column
  itself.
* A workbook element is compiled again against the table's columns each time it runs. It counts only when it
  names the column, in a formula, grouping, filter or sort.
* SQL is parsed: `SELECT *` reads every column. A column that cannot be tied to one table of a join counts for
  each table, so impact over-reports rather than misses.
* The caller sees the names of the assets they may open (operators see all). Others are counted per owner, so
  the person planning the change knows whom to ask.

`POST /sql/catalog/sync` (operators) publishes every asset again, for example after the catalog was away. A
failed publish never fails the save.

Table `cairn_bi_lineage` (migration 0084). Tested on real Trino
(`test_bi_assets_are_catalog_entities_and_a_dropped_column_shows_what_breaks`). The impact asked before
`DROP COLUMN` named exactly the query and dashboard that then failed; the other query and the workbook kept
working.

### Embedded dashboards

A portal can show a Cairn dashboard to its own users, each seeing their own data.

**1. Register the portal as an embed client.** An operator does this in `CAIRN_EMBED_CLIENTS_FILE` (JSON):

```json
{"portal": {"secret_file": "/run/secrets/portal-embed", "origins": ["https://portal.example.com"],
            "dashboards": ["<dashboard id>"], "users": ["urn:li:corpuser:portal-*"]}}
```

The secret needs at least 32 bytes; keep it in a Kubernetes Secret. `dashboards` may be `["*"]`. `users` lists
the patterns of Cairn users the portal may sign for, for example people provisioned by SCIM.

**2. Sign a token on the portal's server.** It is never signed in the browser. The token is HMAC-SHA256 over
base64url JSON:

```python
claims = {"client": "portal", "sub": "urn:li:corpuser:portal-ana", "dashboard": "<id>",
          "origin": "https://portal.example.com", "exp": int(time.time()) + 300, "jti": uuid.uuid4().hex}
body = b64url(json.dumps(claims)); token = body + "." + b64url(hmac.new(secret, body.encode(), "sha256").digest())
```

`cairn.embed.dashboards.sign(secret, claims)` does the same.

**3. Frame it.** The portal's page uses `<iframe src="https://cairn.example.com/embed/v1/dashboards/<id>?token=…">`.

**What Cairn checks.** Each of these is refused with 403 and audited as `embed.refused`:
* the signature;
* that the client exists;
* that the user and the dashboard are ones the client may use;
* that the origin is one of the client's;
* the expiry, which can be at most 10 minutes ahead;
* that the `jti` was never used before.

If all hold, Cairn serves the page with an embed session of its own (`CAIRN_EMBED_SESSION_TTL`, default 3600
seconds), audited as `embed.session_started`.

**What the page can do.** It may be framed only by the token's origin (`frame-ancestors`). It runs only its own
inline script (a nonce). It talks only to Cairn. Every tile runs **as the token's user**, so their policies, masks
and row filters apply. A session follows what the client allows now: take a dashboard or a user off the client
and its sessions stop working.

Tables `cairn_embed_nonces` and `cairn_embed_sessions` (migration 0086). Tested on real Trino, in Chromium
(`test_an_embedded_dashboard_shows_each_portal_user_their_own_rows`):
* Bob's portal page shows only his rows;
* a page on another origin cannot frame the dashboard;
* reused, expired, too-long-lived, tampered tokens, and tokens for another user, origin or dashboard, are refused.

The older embeds of catalog cards (`/embed/v1/tokens`, `CAIRN_EMBED_ALLOWED_ORIGINS`) are separate and unchanged.

### Version history

In the web UI: **History** on a dashboard, a workbook or a saved query lists its versions, shows what changed and restores one.


Every save of a saved query, workbook or dashboard is kept as a numbered version. A save that changes nothing
is not a new version.

| Call | What it does |
|---|---|
| `GET /sql/versions/<kind>/<id>` | the versions, newest first, with who saved each and when |
| `GET …/<n>` | one version as it was |
| `GET …/diff?a=<n>&b=<m>` | the fields that changed, and a line-by-line diff |
| `POST …/<n>/restore` | saves that version again, as a new version |

* **Restore** goes through the asset's own save, so the same rules hold. Owners and editors restore. A
  dashboard's tiles are checked again. An editor's restore leaves who may see the asset as it is.
* **Reading history:** anyone who may open the asset may read it. Others get 404.
* **What is kept:** the newest `CAIRN_BI_VERSIONS` (default 200) versions of each asset. Versions go with
  the asset when it is deleted.
* A version that could not be recorded never fails the save; the next save records the asset as it is then.

Table `cairn_bi_versions` (migration 0085). Tested on real Trino (`test_bi_version_history_rolls_a_broken_edit_back`):
* a query broken by an edit, compared and restored, runs again;
* a dashboard's earlier name and tiles are restored;
* a viewer may read the history but not restore it.

### Folders, comments and usage

* **Folders** (`/sql/folders`).
  - A person makes folders, nested up to 8 deep, and files dashboards, workbooks and saved queries they may
    open in them (`PUT /sql/folders/<id>/items/<kind>/<id>`).
  - A folder that is `shared` is listed for everyone.
  - A folder shows each viewer only the assets they may open, so a folder never reveals what is not shared
    with them.
  - Only its owner changes or deletes a folder; a folder with folders in it is not deleted.
* **Comments** (`/sql/comments/<kind>/<id>`).
  - Anyone who may open an asset reads and writes its comments, optionally about one tile (`tile`) and as a
    reply (`parent`).
  - An `@name` mention tells that person (`bi.mentioned`), but only if they may open the asset. The owner is
    told of new comments (`bi.commented`).
  - Authors edit and delete their own comments, and the asset's owner may delete any. A deleted comment
    keeps its place in the thread, without its text.
  - Comments and deletions are audited.
* **Usage** (`/sql/usage`).
  - A dashboard rendered (embedded ones too), a saved query run or a workbook opened counts one view, per
    person and day.
  - `GET /sql/usage/<kind>/<id>` shows the asset's owner and editors its views and distinct viewers per day.
  - `GET /sql/usage/popular` lists the most viewed assets the caller may open, and `GET /sql/usage/stale` the
    caller's own assets nobody viewed in the last `days` (default 30) days.
  - Views are kept `CAIRN_BI_USAGE_DAYS` (default 180) days.

* **When an asset is deleted**, it leaves the folders it was in, and its comments and views are deleted with it.
* **Notifications** about BI assets (shared, mentioned, commented) link to where the asset opens in the web UI:
  a dashboard itself (`/cairn/dashboards?open=<id>`), a workbook or saved query its page.

In the web UI:
* **Comments** on a dashboard, a workbook or a saved query opens its threads. Reply, edit your own, or delete
  (yours, or any as the owner); on a dashboard, a comment can be about one tile.
* **Usage** (owners and editors) shows its views and people per day over the last 30 days.
* The Dashboards page has a **Folders** tab, to make, rename, share and delete folders, file assets into them
  and open them from there, and a **Usage** tab with the most viewed assets and your own that nobody opened in
  30 days.

Tables `cairn_bi_folders`, `cairn_bi_folder_items`, `cairn_bi_comments` and `cairn_bi_views` (migration 0088). Tested
over HTTP against a Cairn server and a SQLite warehouse (`tests/test_bi_collab.py`), and in Chromium against Cairn
(`tests/test_sql_ops_ui_e2e.py`: a dashboard filed into a new folder, opened from it, commented on with a
mention, and its views shown).

### Sharing with people and groups

In the web UI: **Share** on a dashboard, a workbook or a saved query (owners and editors) lists who has it; its owner adds a person or `group:name` as viewer or editor, or removes them.


Saved queries, workbooks and dashboards are shared with people or groups (`PUT /sql/shares/<kind>/<id>`, kind
`query`, `workbook` or `dashboard`, body `{"principal": "urn:li:corpGroup:finance", "role": "VIEWER"}`; `"role":
null` stops sharing). A shared asset shows its `role` for the caller: `OWNER`, `EDITOR` or `VIEWER`.

| Role | May |
|---|---|
| `VIEWER` | open and run it |
| `EDITOR` | also change it (name, SQL, elements, tiles) |
| owner | also share it, make it visible to everyone (`shared`), and delete it |

* Someone it is not shared with gets 404, as if it did not exist. Membership of a group counts, so a dashboard
  shared with Finance follows the group's members.
* An editor's save keeps the owner and leaves `shared` as it was. Tiles already on a dashboard stay when an editor
  saves it; any new tile must be a query the editor may open.
* Sharing never shares data. Every query, element and tile runs as the person looking, under their own
  policies, masks and row filters. A dashboard shared with someone lets them render the saved queries its tiles
  are built on, but not open those queries on their own.
* Who has it: `GET /sql/shares/<kind>/<id>` (the owner and editors). A person shared with is told (event
  `bi.shared`).
* Each change is audited (`bi.shared` and `bi.unshared`, category `sql`). Deleting an asset drops its shares.
* Up to 500 people and groups per asset.
* Metrics are not shared this way: they are platform-wide definitions (see the metrics layer).

Table `cairn_bi_shares` (migration 0081). Tested on real Trino
(`test_bi_sharing_with_people_and_groups_keeps_each_viewers_data`). The test covers:
* a viewer who sees only their own rows through the owner's dashboard;
* a group of editors;
* refused reshares, deletes and new tiles;
* removing access.

## Data apps

Data apps are Streamlit apps (or any web app) that Cairn serves to people, each viewer signed in and governed as themselves.

- **Define** one with `POST /v1/apps` (`MANAGE_SYSTEM_OPERATIONS`): `name`, `title`, and where it runs.
  - `url`: the app runs elsewhere, for example a Deployment beside Cairn, and Cairn only fronts it.
  - `command`: Cairn starts it on the first visit and stops it after `idle_minutes` (30) without traffic, for single-node installs. `{port}` and `{base}` in the command are filled in, so for Streamlit: `["streamlit", "run", "app.py", "--server.port", "{port}", "--server.baseUrlPath", "{base}", "--server.headless", "true"]`, with `workdir` for the app's folder.
  - Either way, the app serves under `/apps/<name>/`, its base path (`CAIRN_APP_BASE`).
- **Open** it at `/apps/<name>/`. Every request goes through Cairn, page loads and WebSockets alike:
  - **Signed in.** Strangers get 401.
  - **Allowed.** Opening an app needs `USE_APP` on `urn:li:dataApp:<name>`. Others get 403, and the app never sees their request; a `command` app is not even started for them. `GET /v1/apps` lists only the apps the caller may open.
  - **Told who is viewing.** Cairn removes every `X-Cairn-*` header the browser sent, then adds:
    - `X-Cairn-User`: the viewer's urn.
    - `X-Cairn-App-Key`: the app's own key. An app reached by `url` should refuse requests without it; `command` apps get it as `CAIRN_APP_KEY`.
    - `X-Cairn-Trino-Token` (where Trino is set up): a ten-minute token for Trino as the viewer. Queries the app runs with it are the viewer's own, under their policies, masks and row filters, never the app's.
  - **No secrets passed on.** A `command` app's environment drops Cairn's own secrets.
- **Keys.** Each app's key is `HMAC(CAIRN_APPS_SECRET, name)`, defaulting to the service token: nothing is stored, every replica agrees, and rotating the secret rotates every key.
- **From git.** Add `git`, either `{"repo": …}` (a repository configured for Cairn's git integration, whose token Cairn uses) or `{"url": …}` (one whose prefix the operator allows in `CAIRN_APP_GIT_URLS`; chart: `cairn.apps.gitUrls`), plus `branch` (`main`) and `path` (the app's folder, where `command` runs).
  - **Deploys.** Creating the app deploys the branch's current commit, and the app records it (`commit`). A new commit on the branch changes nothing until `POST /v1/apps/<name>/deploy`. Each deploy is audited (`app.deployed`).
  - **Fails closed.** An app whose first deploy fails is not kept.
  - **Signing in.** The repository's token reaches git through the environment, never a command line or the clone's config.
- **On Kubernetes** (`"runtime": "kubernetes"`, with `image` and `port`). Each app is a Deployment of its own in `cairn.apps.kubernetes.namespace`, at zero replicas until someone opens it. The first visit scales it to one and waits until it is ready. Every visit stamps it, and after `idle_minutes` without one it is scaled back to zero, whichever Cairn replica served it.
  - **Code.** An app from git gets its code from an init container (`gitImage`) that checks out exactly the deployed commit. Deploying again rolls the pods to the new commit.
  - **Locked down.** The pod has no Kubernetes credentials and runs as a non-root user (10001) without privileges or capabilities. It gets `PORT`, `CAIRN_APP_BASE` and `CAIRN_APP_KEY`; the key and any git token come from a Secret of its own.
  - **Only Cairn reaches it.** A NetworkPolicy lets only Cairn's API pods in (or `cairn.apps.kubernetes.ingressFrom`). Cairn reaches the app at its Service; when Cairn runs outside the cluster, set `access: node-port` and `nodeAddress`. Node ports keep the caller's address, so the policy can name Cairn's.
  - **Permissions.** The chart gives Cairn's API a Role in that namespace for the apps' Deployments, Services, Secrets and NetworkPolicies, and nothing else.
- **Tested** in `tests/test_data_apps_git.py` (a real repository; the app a real process). The app ran the deployed commit in its `path`, kept it until deployed again, and then ran the new one. A URL outside the allowed list was refused, and a failed first deploy left no app.
- **Tested on Kubernetes** in `tests/test_data_apps_k8s.py` (a kind cluster; the chart's Role; code from a git server):
  - the app's objects were created stopped, and the key appeared only in its Secret;
  - the first visit through Cairn started it, running the deployed commit as user 10001;
  - another pod in the cluster was refused by the network policy;
  - a deploy rolled it to the new commit, and idleness scaled it back to zero;
  - Cairn's account could not create apps in another namespace, and deleting the app removed its objects.
- **Tested** in `tests/test_data_apps_e2e.py`, with a real Streamlit app opened in Chromium through Cairn.
  - Strangers got 401, someone without `USE_APP` got 403 on the page and on the WebSocket, and the app was not started for them.
  - Ana's first visit started the app. Its page, which Streamlit draws over its WebSocket, showed her as the viewer, the app key verified, and a Trino token present.
  - The headers her browser forged (`X-Cairn-User: root`) never reached the app, and Cairn's secrets were not in its environment.
  - The idle app was stopped, and the next visit started it again.

**Talk to your app.** People using an app can ask questions about what it shows (`POST /v1/apps/<name>/chat`, and
**Ask about this app** on the Data apps tab), answered by the model from the app's declared context and the asker's
own data access.

- **Declaring the context.** With the app (`POST` or `PUT /v1/apps`), an operator sets `chat`:
  `{"description": "Customers by region", "tables": ["urn:li:dataset:(…)"], "queries": [{"name": "customers",
  "sql": "SELECT region, count(*) AS n FROM main.users GROUP BY region", "connection": "wh"}],
  "context_endpoint": "/cairn-context"}` (up to 50 tables and 10 queries). The listing shows whether an app has a
  chat and its description, never the queries.
- **Asking.** `{"question": "...", "view": {...}, "history": [...]}` from someone with `USE_APP` on the app (others:
  403, before anything runs). Agents and services cannot ask.
  - The declared queries run through Cairn's query path **as the asker**: their policies, masks and row filters
    apply, and a query they may not run is left out and named in `left_out`. The app's own credentials are never
    used. The declared tables are described (name, description, columns) only when the asker may see them.
  - With `context_endpoint`, Cairn asks the running app `GET /apps/<name><endpoint>?view=<the view, JSON>` the way
    its pages are reached: with the asker's identity headers (`X-Cairn-User`, the app key, the viewer's Trino token
    where Trino is set up), without anything of Cairn's. Its JSON answer (up to 20 KB) says what the app is showing;
    an app that does not answer is left out.
  - The model is called through the AI gateway as the asker: `USE_AI_GATEWAY` on the model, their budget and rate
    limit, metered as `app-chat:<name>`. The model is the request's `model`, else `CAIRN_APPS_CHAT_MODEL`, else the
    first served model the asker may use. Everything from the app and the queries is passed as data, never as
    instructions, and values masked for the asker stay masked.
- **The answer** has `answer`, `citations` (every table whose data or description the model was given),
  `masked_columns`, `left_out` and `app_view` (whether the app's view was included). Audited as `app.chat`: the app,
  the model and the tables, never the question or the answer.
- **Tested** in `tests/test_app_chat_e2e.py`, with a real app process started by Cairn, a real SQLite warehouse and the
  model behind the gateway (a stand-in repeating what it was given). Ana's answer held her masked emails and none of
  the real ones reached the model; the administrator's held them unmasked. The app's context endpoint was asked as
  Ana with the page's view, got her identity and the app key and none of Cairn's secrets, and its page title reached
  the answer. The answer cited the table read; a query on a table Ana may not read was left out and named. Bob
  (no `USE_APP`) was refused before the model was called; the call was metered to Ana and audited without content.

## App packages: apps others install on their own data

An app package is a data app one team publishes and other teams install on their own datasets. Each install
reads only the tables its installer binds to it.

**Publishing.**

1. Register the package from git: `POST /v1/app-packages {"name", "git": {url | repo, branch, path}}`
   (`CREATE_APP_PACKAGE`, from the same repositories and URLs as data apps).
2. Release versions: `POST /v1/app-packages/<name>/versions {"version": "1.2.0"}` (`MANAGE_APP_PACKAGE` on
   `urn:li:appPackage:<name>`).

A version is the branch's commit at that moment plus the manifest found in the package's folder,
`cairn-app.json`:

```json
{"title": "Churn explorer",
 "command": ["streamlit", "run", "app.py", "--server.port", "{port}", "--server.baseUrlPath", "{base}"],
 "references": [{"name": "customers", "privilege": "VIEW_DATASET_PROFILE",
                 "description": "one row per customer, with its plan and signup date"}]}
```

A reference may only ask to read. A version never changes: a fix is a new version.

**Installing.** `POST /v1/app-installs {"name", "package", "version", "references": {"customers": "<dataset urn>"}}`
needs `USE_APP_PACKAGE` on the package. Every declared reference must be bound to one of the installer's
datasets, and binding is consent: the installer must hold the reference's privilege on that dataset
themselves. The install becomes a data app pinned to the version's commit, opened at `/apps/<name>/` with
`USE_APP` as usual.

An installed app is another party's code, so it runs with none of Cairn's environment: only `PATH`, `PORT`,
`CAIRN_APP_BASE` and its own `CAIRN_APP_KEY`.

**What it may read.** Each request to the app carries two Trino tokens:

- `X-Cairn-Trino-Token`, for the viewer, as for any data app;
- `X-Cairn-App-Trino-Token`, for the install's own identity, `cairnapp-<name>`.

Trino lets the app's identity:

- read the bound datasets and nothing else, not even other tables the installer can read;
- do no writes, use no AI or stored functions, and read no metadata tables.

Its reads are decided as the installer's, with the installer's masks, row filters and query policies.
`cairnapp-` is a reserved user name: nobody signs in as one, and an identity whose install is gone reads
nothing.

**Upgrading.** `POST /v1/app-installs/<name>/upgrade {"version", "references"}` moves an install to another
version and redeploys it. It can go to a later version or back to an earlier one:

- references the new version adds are bound then;
- bindings the target version no longer asks for are dropped.

`DELETE /v1/app-installs/<name>` unbinds and removes the install (the installer, or `MANAGE_DATA_APP`).

Releases, installs, upgrades and uninstalls are audited.

Tested with a real git repository and the app as a real process reached through Cairn
(`tests/test_app_packages.py`). The test covered:

- consent refusals;
- the app's own token, and no leaked environment;
- the identity's reads, refusals, row filter and mask (Trino's protocol against the policy engine);
- immutable versions, an upgrade binding a new reference, and a rollback that unbinds it;
- uninstalling.

On a real Trino, the app's identity read the bound table with the installer's filter and was refused another
table and a write (`test_an_installed_apps_identity_reads_its_bound_tables_only`).

## Git folders

A Git folder keeps a person's saved queries and notebooks in a git repository, reviewed like code. Cairn works on a clone of its own with the `git` command, so any host works.

- **Create** one with `POST /v1/git-folders`: `name`, and `repo` (a repository configured for Cairn's git integration, whose token Cairn uses) or `url` (one the operator allows in `CAIRN_GIT_FOLDER_URLS`, a comma-separated list of URL prefixes). Optional: `branch` (`main`), `path` (a folder in the repository), and `every_minutes` for a scheduled pull. Folders are their owner's: nobody else sees or uses them.
- **Pull** (`POST /v1/git-folders/<id>/pull`).
  - Each `.sql` file becomes one of the owner's saved queries. Header comments give its settings (`-- connection:`, `-- description:`, `-- shared:`, `-- chart:` as JSON); the rest is the SQL.
  - Each `.ipynb` notebook is published to the catalog, with the tables its `%%sql` cells read.
  - A file changed in git updates its query, and a deleted file deletes it.
  - **Conflicts.** When the query was also changed in Cairn since the last pull, the file is reported as a conflict and left alone. Pull with `{"force": true}` to take git's version.
- **Push** (`POST /v1/git-folders/<id>/push`, optional `message` and `add`, a list of other saved query ids to add).
  - The folder's queries, as they are in Cairn, are committed by their owner to a new branch, `cairn/<user>/<time>`, never to the folder's branch. For a configured GitHub, GitLab or Bitbucket repository, a pull request is opened from it.
  - A query already proposed, or one git already holds as it is, is not pushed again.
  - Until the pull request is merged, pulls neither conflict with it nor delete the queries it adds. Once it is merged, the next pull has nothing to report.
- **Signing in to the host.** For a configured repository, the token is sent as a header on each git command and never written into the clone. Only `https` and `ssh` remotes are used, plus `file` when an allowed prefix is `file://`.
- **Notebooks in Jupyter** (`GET /v1/git-folders/<id>/notebooks`). Each notebook as it was last saved in the owner's JupyterHub server, or else as git has it. JupyterHub's extension syncs them into `git/<folder>/`; see [Notebooks](#notebooks-jupyterhub-with-cairn-sign-in).
  - Saves there are kept for the next push. Pushed notebooks have their outputs cleared.
  - A pull that brings a new version of a notebook also changed in Jupyter since reports a conflict, unless `force`.
  - Folder names must give different directories: a second folder named `Reports` beside `reports` is refused.
- **In the UI.** **Git folders** (`#/sql/git` in the workspace) links a folder, pulls it (showing what came in, conflicts, and a button to take git's version) and pushes with a commit message, linking to the pull request when the host opens one.
- **Settings.** `CAIRN_GIT_FOLDERS_DIR` holds the clones (default `$CAIRN_DATA_DIR/git-folders`). Deleting a folder removes its clone and keeps its queries as ordinary saved queries.
- **Tested** in `tests/test_git_folders.py`, with a real git repository and a colleague's clone.
  - A pull made two queries from their headers and published a notebook; pulling again changed nothing.
  - A colleague's edit and deletion were pulled. A push put a changed query and a new one on a new branch committed by their owner, leaving `main` alone; pushing again made no branch.
  - Before the merge, a pull left the new query alone; after it, a pull had nothing to report.
  - A file changed on both sides was a conflict until forced.
  - Another person could not see or pull the folder, and a URL outside the allowed prefixes was refused.

## Reports

**Reports** in the workspace answers the two questions that fund a catalog: is it used, and is the estate getting governed? It has four sections:

- **Coverage:** the share of datasets that are described, owned, classified, certified and monitored. It is shown overall, by domain and by platform. Click any figure to list the datasets that lack it.
- **Adoption:** weekly and monthly active users (people who opened an asset), asset views over 7 days, access requests over 30 days (submitted, approved and pending), and the median time to a decision.
- **Quality:** open incidents, the monitored share, and the mean time to resolve. The resolve time is measured from a monitor's first failure to its next pass, over 30 days.
- **Governance:** personas, purposes, active grants, and grants that expire within 7 days.

A snapshot is computed once a day by the notifier and kept per day, so the history shows the trend. **Recompute now** refreshes it on demand, and **Download CSV** exports coverage by domain and by platform. Reports are for administrators (`MANAGE_GLOBAL_SETTINGS`). Set `CAIRN_REPORTS_OPEN=true` to show them to everyone who is signed in; the drill-down lists still show each person only the datasets they may see.

The API, under `/cairn/api/reports`:

- `GET` returns the latest snapshot,
- `POST /refresh` recomputes it,
- `GET /history?days=90` returns the headline figures per day,
- `GET /coverage.csv` exports coverage,
- `GET /assets?missing=owner&domain=urn:li:domain:finance` lists the datasets behind a figure (`domain=none` means no domain; `platform=` filters by platform).

### Cost per asset

**Reports → Cost** lists two kinds of table:

- **Idle tables:** no reads in 30 days and nothing downstream in lineage, ranked by what they cost to store.
- **Expensive per read:** tables whose storage and query cost is high for how rarely they are read.

**Propose deprecation** notifies the table's owners and followers with the figures behind the proposal.

Daily figures per table come from warehouse collectors, which run once a day for the previous day. Configure them in `CAIRN_COST_SOURCES`, a YAML file whose `sources` entries use the named connections:

- **Snowflake** (`platform: snowflake`) reads `ACCOUNT_USAGE`:
  - storage from `TABLE_STORAGE_METRICS`, priced at `storage_usd_per_tb_month` (default 23),
  - compute from `QUERY_ATTRIBUTION_HISTORY` credits, priced at `credit_usd` (default 3), split evenly across the tables each query read (`ACCESS_HISTORY`).

  The connection's role needs access to the `SNOWFLAKE` database.
- **BigQuery** (`platform: bigquery`, `region: region-us`) reads `INFORMATION_SCHEMA`:
  - `TABLE_STORAGE` logical bytes, at `storage_usd_per_gib_month`,
  - `JOBS` bytes billed, at `usd_per_tib`, split across the referenced tables. With editions pricing (`pricing: editions`), it uses slot-hours (`total_slot_ms`) at `usd_per_slot_hour` (default 0.06) instead.
- **Databricks** (`platform: databricks`) reads the system tables:
  - list-price dollars per SQL warehouse and per job for the day (`system.billing.usage` with `system.billing.list_prices`);
  - a warehouse's cost is split across its statements by duration (`system.query.history`), and a job's across its runs;
  - each statement or job then splits its share across the tables it read (`system.access.table_lineage`).

  The system tables have no per-table storage figure, so Databricks rows carry compute only. The connection needs `SELECT` on those system schemas.
- **Redshift** (`platform: redshift`) splits the day's compute across queries by elapsed time (`SYS_QUERY_HISTORY`), then across the tables each query scanned (`SYS_QUERY_DETAIL`):
  - the day's compute is the cluster's cost per day (`daily_compute_usd`);
  - for Serverless (`serverless: true`), it's RPU-seconds from `SYS_SERVERLESS_USAGE`, at `usd_per_rpu_hour` (default 0.375).
- **Spark** has no bill to read, so Cairn prices each application from its OpenLineage events when `CAIRN_COST_SPARK_USD_PER_CORE_HOUR` is set:
  - **The price.** The cores it reserved, times its wall-clock time from start to finish, at that rate.
  - **The cores.** `spark.driver.cores` plus `spark.executor.instances` × `spark.executor.cores`. With dynamic allocation, `maxExecutors` (an upper bound); N for `local[N]`. Otherwise `CAIRN_COST_SPARK_DEFAULT_CORES` (default 4). `cairn_spark` sends these settings with the events.
  - **Who pays.** Producing data is what the job is for, so the cost is split evenly across the tables its queries wrote, on the day it finished.
  - **Charged once.** Each application is charged once in `cairn_run_costs`, however often its events are delivered.
  - **Per job.** **Reports → Cost → Spark jobs** (`GET /reports/costs/jobs?days=30`) lists what each application cost over 30 days: its runs, running time and dollars.

  It is an estimate of what was reserved, not a bill. For the exact figure from your cloud, post daily rows to `POST /reports/costs`.
- **Trino** (Cairn's own warehouse) prices each query from what Trino reports for it when `CAIRN_COST_TRINO_USD_PER_CPU_HOUR` or `CAIRN_COST_TRINO_USD_PER_TB_SCANNED` is set:
  - **The price.** Its CPU time at the first rate plus the bytes it scanned at the second, from the query history Trino's event listener sends (kept `CAIRN_QUERY_HISTORY_DAYS`, 30). Failed queries count: their CPU was spent. Each query's profile (`GET /v1/queries/<id>`) shows its `cost_usd`.
  - **Per person, team and table.** **Reports → Cost → Trino** (`GET /reports/costs/trino?by=user|team|table&days=30`) lists queries, CPU hours, data scanned and dollars.
    - A **team** is one of the person's groups, or only those named in `CAIRN_COST_TEAMS` (comma-separated group URNs). A person's queries are split evenly among their teams; people in none are under "(no team)".
    - A **table**'s share is an even split of each query across the tables it read.
    - The rows add up to the total whichever way the report is cut.
  - **Who sees what.** Operators (`MANAGE_SYSTEM_OPERATIONS`, or everyone with `CAIRN_REPORTS_OPEN`) see every query. Anyone else sees what their own queries cost, and by table only the tables they may see.
  - **In the table report.** The idle and expensive lists add Trino's compute to each table it read, and count each query as a read.
  - Without either rate, the report still shows CPU and data scanned, and no dollars.
- **The lakehouse's own storage** (Iceberg tables in Cairn Catalog and Cairn Store) is sized every day when `CAIRN_COST_LAKEHOUSE_USD_PER_TB_MONTH` is set:
  - **The size.** Each table's current snapshot's files (`total-files-size`), priced for one day of the month. Snapshots kept for time travel are not counted; maintenance expires them.
  - **The catalog.** The one Cairn's pipes write to (`CAIRN_PIPES_CATALOG`), or `CAIRN_COST_LAKEHOUSE_CATALOG` (the same JSON of catalog properties, or a path to one). Its token must see every table.
  - **When.** With the warehouses, in the daily collection the scheduler runs after midnight UTC. A table that cannot be loaded is skipped and logged; the others are still sized.
- **Per team and day.** `GET /reports/costs/teams?days=30` gives each team's costs for each day, with nothing posted by hand:
  - **SQL:** the team's Trino queries (CPU hours, data scanned, queued seconds and dollars), split evenly among the teams of the person who ran each one;
  - **Storage** and **table compute:** each table's daily storage and engine compute (the lakehouse, warehouses, Spark runs that wrote it), to its owners' teams: an owning group, or an owning person's teams;
  - **Runs:** Spark runs that wrote no table, to the teams of their job's owners;
  - a **total** per team and day. Teams are a person's groups, or only those in `CAIRN_COST_TEAMS`; what has no team is under "(no team)".
  - **Who sees what.** Operators (or everyone, with `CAIRN_REPORTS_OPEN`) see every team; anyone else sees the teams they are in.

  Storage is the `SVV_TABLE_INFO` size, at `storage_usd_per_gb_month` (default 0.024). The connection's user needs to see every user's queries (`SYSLOG ACCESS UNRESTRICTED`).

For other sources, post daily rows to `POST /cairn/api/reports/costs` (`{"rows": [{urn, day, storage_bytes, storage_usd, compute_usd, reads}]}`). Posting a dataset and day again replaces that row.

The rest of the API is under `/cairn/api/reports/costs`:

- `GET /idle?days=30` and `GET /expensive?days=30` return the two lists,
- `GET /asset?urn=…` returns one table's daily series,
- `POST /propose-deprecation` with `{"urn", "reason"}` sends the proposal.

### Cost budgets

A budget is what one part of the platform may spend each period, with alerts before and when it does. Operators (`MANAGE_SYSTEM_OPERATIONS`) set them at `POST /cairn/api/v1/budgets`:

- `name`, `amount_usd`, `period` (`month`, the default; `week`, from Monday; `day`; calendar periods in UTC), `thresholds` (percents of the amount; default 80 and 100), `forecast` (default on) and `notify` (people or groups).
- One `scope`: `{"all": true}`; `{"domain": "urn:li:domain:…"}`; `{"owner": "urn:li:corpGroup:…"}` (the tables a person or group owns); `{"platform": "snowflake"}`; `{"tables": [urns]}`; `{"warehouse": "<name>"}` (one of `CAIRN_WAREHOUSES`); `{"ai": true}` or `{"ai": "urn:li:corpuser:…"}`.
- **Spend** comes from the cost report, never counted twice: the tables' daily storage and compute (collectors, posted rows, Spark runs split across what they wrote), runs charged to no table (warehouse node-hours; Spark runs that wrote nothing) and AI calls.
- **Alerts.** The scheduler checks each budget hourly (`POST …/budgets/<id>/check` checks one now). Each threshold, and the forecast (the spend so far at its daily rate over the whole period, once a fifth of it has passed, when that would exceed the amount), is sent at most once a period to the budget's creator and `notify`: `budget.threshold_crossed` (one message for the highest threshold just crossed) and `budget.forecast_exceeded`, through the notification hub (in-app, and any routes for those types).
- `GET …/budgets/<id>` shows the period's spend by day, the ten largest contributors, the forecast and the alerts sent. Budgets are visible to operators, their creators and the people they notify. `PATCH` changes one and `DELETE` removes it; changes are audited (`budget.created`, `budget.changed`, `budget.deleted`). Tables `cairn_budgets` and `cairn_budget_alerts` (migration 0068). Tested through the API in `tests/test_budgets.py`.

### Chargeback statements

A chargeback statement is the month's bill per cost centre: every cost Cairn knows, charged to whoever caused it. It adds up to the month's total, and is frozen when issued.

- **Cost centres.** `PUT /cairn/api/v1/chargeback/cost-centres/{code}` with `{"name": "Finance", "teams": ["urn:li:corpGroup:finance"]}` (`MANAGE_SYSTEM_OPERATIONS`). The code is the one finance knows, such as `CC-4100`. A team pays from one cost centre.
- **What is charged, and to whom.** These are the same figures as the cost report and budgets, and nothing is counted twice:
  - **SQL:** each Trino query's CPU and scanned bytes, at `CAIRN_COST_TRINO_USD_PER_CPU_HOUR` and `CAIRN_COST_TRINO_USD_PER_TB_SCANNED`, to the cost centres of the person who ran it. It is split evenly when their teams pay from several.
  - **AI:** each model call's cost, to the cost centres of the person it was for.
  - **Storage and table compute:** from the warehouse collectors and Spark runs, to the cost centres of the table's owners in the catalog: a group's own, or a person's teams.
  - **Runs charged to no table** (warehouse node-hours, Spark runs that wrote nothing): to the cost centres of the run's owner.
  - **Unallocated:** what cannot be placed (someone in no cost centre's team, a table with no owner) is its own line, `unallocated`, with the reasons. So the statement always adds up to the month's total.
- **Preview.** `GET /cairn/api/v1/chargeback/preview/2026-10` shows a month as it stands, current or past. Nothing is kept.
- **Issue.** `POST /cairn/api/v1/chargeback/statements` with `{"month": "2026-10"}`, once the month (UTC) is over.
  - The statement is frozen: its lines and totals do not change when late figures arrive.
  - It is audited (`chargeback.issued`, with each cost centre's total).
  - Issuing the month again makes a new version, and the old one is kept.
- **Read.** `GET /cairn/api/v1/chargeback/statements/{id}`, or `?format=csv` for finance, with one row per cost centre and source, and CPU hours and TB scanned for SQL.
  - Operators see the whole statement.
  - Members of a cost centre's teams see their own lines only.
- **Tested** in `tests/test_chargeback.py`, on SQLite and PostgreSQL:
  - every source charged to the right cost centre, split for people in two;
  - the unallocated line with its reasons, and the total reconciling;
  - refused before the month ends;
  - frozen against a late figure, with a second version;
  - a member's own view, and the CSV.

## One database

By default the catalog stores its metadata in MySQL, and Cairn's own data is in PostgreSQL. You can put the catalog in PostgreSQL too. Then you have one database server to run, back up and patch, and no MySQL licence to review.

- **Compose:** add `docker-compose.postgres.yml` to every command:

  ```bash
  docker compose -f docker-compose.cairn.yml -f docker-compose.postgres.yml up -d
  ```

  The catalog goes into a `cairn_catalog` database on the `cairn-postgres` server, which a one-off job creates. MySQL is not started.
- **Helm:** set `core.database.type: postgres` and point the host, port (5432), name and user at the server. Create the database before installing; the setup job then creates its tables. `core.database.sslMode` sets the JDBC `sslmode` (default `prefer`).
- **Backups:** `deploy/ops/backup.sh` and `restore.sh` find the catalog wherever it is. Pass the same compose files in `COMPOSE`.

Choose this when you install. Switching an existing deployment does not move its catalog: the new catalog database starts empty. Re-ingest your sources into it, or restore the backup of a deployment that already ran on PostgreSQL.

## Licence

Cairn checks its licence offline. A licence key is signed text naming:

- the customer, the edition and any extra features,
- the number of people who may use Cairn,
- the expiry date.

Install it in **Settings → Licence**, with `cairn licence install <file>`, or from a Secret (`cairn.licence.existingSecret` on Helm). The key is checked when you install it: a key that was altered, that is for another tenant, or that has an unknown signature is refused.

| State | What happens |
|---|---|
| No licence | Evaluation mode: everything works. A deployment with `CAIRN_LICENCE_REQUIRED=true` (hosted tenants) is read-only instead. |
| Valid | Everything the edition includes works. AI, automations, warehouse tag sync, ticketing, data preview and SQL, and cost per asset are in the enterprise edition, or listed as extra features; anything else answers 402. |
| Expired | For the licence's grace period (14 days by default) everything keeps working, with a banner. After that Cairn is read-only. |
| More people than seats | A seat is anyone who used Cairn in the last 30 days, in order of first use. People beyond the limit can read but not change anything. |

**Read-only** means changes are refused with HTTP 402. Reading, searching, the assistant's answers, signing in, user provisioning and installing a new licence all keep working. Service accounts are never limited.

Everyone sees a banner when the licence needs attention. `cairn licence show` prints the state. The metrics `cairn_licence_days_left`, `cairn_licence_seats` and `cairn_licence_read_only` drive three alerts: expiring, read-only, and seats full.

## Hosting tenants

For a hosted offering, each tenant gets its own Cairn release in its own Kubernetes namespace. `deploy/hosting/tenant.sh` creates, upgrades and removes tenants:

```bash
export CAIRN_HOST_DOMAIN=cairn.example.cloud CAIRN_PG_HOST=pg.internal CAIRN_PG_ADMIN_URL=postgresql://admin@pg.internal/postgres \
       CAIRN_SEARCH_HOST=search.internal CAIRN_KAFKA_TEMPLATE='kafka.cairn-{id}:9092'
deploy/hosting/tenant.sh create acme --licence acme.licence --values platform.yaml
deploy/hosting/tenant.sh upgrade acme [--licence renewed.licence]
deploy/hosting/tenant.sh delete acme [--drop-data]
deploy/hosting/tenant.sh list
```

**What a tenant gets:**

- **Hostname:** `https://<id>.<domain>`.
- **Databases:** the Cairn database and the catalog database on the shared PostgreSQL server, owned by the tenant's own role. The catalog runs in PostgreSQL ([One database](#one-database)).
- **Search:** indices in the shared OpenSearch cluster under the prefix `<id>_`.
- **Kafka:** its own.
- **Kubernetes:** a ResourceQuota, network policies that admit only the tenant's own pods and the ingress, and credentials generated for it (never printed).

The tenant's licence must name its id. The release runs with `cairn.licence.required: true`, so a tenant without a valid licence can't change anything. Add `--dry-run` to `create` to render the manifests without touching a cluster.

**Optional settings for `tenant.sh`:**

| Setting | Effect |
|---|---|
| `CAIRN_TENANT_SERVICES=1` | Each tenant gets its own OpenSearch and Kafka in its namespace (`deploy/hosting/tenant-services.yaml`). Only its own release reaches them. `CAIRN_SEARCH_HOST` and `CAIRN_KAFKA_TEMPLATE` are then not needed. |
| `CAIRN_TENANT_SERVICES_HA=1` | With `CAIRN_TENANT_SERVICES`: those services replicated, three nodes each (`deploy/hosting/tenant-services-ha.yaml`), so the tenant keeps working when it loses a node or pod. See *Replicated tenant services* below. |
| `CAIRN_TENANT_SERVICES_SPREAD` | `DoNotSchedule` (the default) puts one pod of each per node and needs three nodes. `ScheduleAnyway` lets fewer nodes run them, for tests. |
| `CAIRN_TENANT_SEARCH_HEAP`, `_SEARCH_MEMORY`, `_KAFKA_HEAP`, `_KAFKA_MEMORY` | Per-node sizes of the replicated services. The defaults are a 1g heap in 2Gi for OpenSearch and 512m in 1Gi for Kafka. |
| `CAIRN_INGRESS_NAMESPACE` | The ingress controller's namespace. Turns on namespace isolation (`deploy/hosting/isolation.yaml`): nothing in another tenant's namespace reaches this one. Add `CAIRN_MONITORING_NAMESPACE` to let Prometheus scrape it. |
| `CAIRN_PG_KUBECTL=<namespace>/<cluster>` | Administer a CloudNativePG cluster through its primary's local socket (`kubectl exec`), instead of `CAIRN_PG_ADMIN_URL`. |
| `CAIRN_TLS_ISSUER` | A cert-manager ClusterIssuer. Each tenant's host gets its certificate from it. |
| `CAIRN_POD_SECURITY` | The Pod Security level enforced in tenant namespaces (`baseline` or `restricted`). |

Every tenant's databases are closed to other roles: `CONNECT` is revoked from `PUBLIC`. `tenant.sh render-services <id>` prints the isolation policies and services that `create` would apply.

### Signup and billing

The hosting control plane can take signups and payments itself: set `CAIRN_SIGNUP` (JSON or a file) on its Cairn API and notifier. Required: `domain` (tenants are `<tenant>.<domain>`), `public_url` (where the control plane is reached), `smtp` (`smtps://user@host:465`, or `smtp://host:587`, which uses STARTTLS) with `smtp_password_file` and `from`, `stripe_key_file`, `stripe_webhook_secret_file`, `licence_key_file` and `licence_kid` (the vendor signing key, as `deploy/ops/licence.py keygen` makes it), and `plans` (`{"team": {"price": "price_…", "edition": "team", "max_users": 25}}`). Optional: `provision` (the command, default `["deploy/hosting/tenant.sh"]`), `grace_days` (14). Every secret is read from a file; a wrong setting stops the control plane at start-up.

1. **Sign up** (`POST /v1/signup`, public): `email`, `organization`, `tenant` (3 to 32 lowercase letters, digits and dashes; reserved names such as `www`, `api` and `admin` are refused; `GET /v1/signup/available/<tenant>` checks one) and `plan`. Cairn emails a confirmation link, good once for 24 hours. Attempts are limited per address and per client.
2. **Confirm** (`GET /v1/signup/verify?token=`): Cairn creates the Stripe customer and a Checkout session for the plan's price and sends the browser to Stripe's payment page.
3. **Stripe's webhook** (`POST /v1/billing/stripe/webhook`; point a Stripe webhook endpoint at it with these events): the `Stripe-Signature` header is checked against the endpoint's secret with five minutes' tolerance, and each event is handled once, even when Stripe sends it again or twice at once.
   - `checkout.session.completed`: the tenant is created (`provision create <tenant> --licence <file>`) with a licence for the plan's edition and users, valid until the paid period ends, and the owner is emailed its address. If provisioning fails, the signup shows `failed` with the error and the notifier retries it every tick until it works.
   - `invoice.paid` renews the licence to the new period's end; `customer.subscription.updated` with another plan's price reissues it for that edition (`provision upgrade`).
   - `customer.subscription.deleted`: nothing renews the licence. It runs out, its grace passes and the tenant becomes read-only with its data kept; the owner is told. `invoice.payment_failed` emails the owner while Stripe retries.
4. **Billing portal**: `POST /v1/billing/portal-link {"email"}` emails the owner a link (good once, for an hour) that opens Stripe's billing portal (`GET /v1/billing/portal?token=`) to change the plan or card, or cancel. The answer is the same whether or not the address owns a workspace.

Licence files are written 0600 to a private temporary directory and deleted after use. Signups are in `cairn_signups` and Stripe's events in `cairn_stripe_events` (migration 0070); confirmations and licences issued or renewed are audited (category `hosting`). Tested with stripe-mock and Mailpit in `tests/test_signup_e2e.py`.

### Usage metering

Hosted tenants report their usage to the control plane, which bills it through Stripe's meters.

- **Keys.** Add `metering_key_file` (a random secret) and `meters` (which metrics are billed, and as which Stripe meter: `{"query_cpu_seconds": "cairn_cpu_seconds", "warehouse_node_seconds": "cairn_node_seconds", "ai_usd_cents": "cairn_ai_cents"}`) to `CAIRN_SIGNUP`. Each new tenant gets its own key, derived from that secret and its name, so the control plane stores none: provisioning passes it to `tenant.sh create … --metering-key FILE`, which keeps it in the tenant's Secret `cairn-metering` and turns on `cairn.metering` (the notifier gets `CAIRN_METERING_URL` and the key file). For an existing tenant, run `tenant.sh upgrade <id> --metering-key FILE` with `CAIRN_METERING_URL` set.
- **What a tenant reports**, once a day is over (UTC) by an hour, and again until the control plane accepts it: `seats` (people active in the last 30 days, as the licence counts them), `query_cpu_seconds` (CPU time of the day's finished Trino queries), `warehouse_node_seconds` (team warehouses' node time) and `ai_usd_cents` (AI calls at the providers' prices). The report is signed (`X-Cairn-Signature: sha256=<HMAC>`) with the tenant's key; `POST /v1/metering/report` refuses a wrong signature (401), a day not yet over (400) and an unknown tenant (404).
- **Billing.** The control plane keeps one value per tenant, day and metric (a newer report replaces it until it is billed). Once a day has settled (`settle_hours` after it ended, default 6), the notifier sends each metric that has a meter as a Stripe meter event for the tenant's customer, identified `<tenant>-<day>-<metric>`, so Stripe counts it once however often it is retried; a failure is kept on the row and retried. Operators see a tenant's usage at `GET /v1/metering/<tenant>`. Tables `cairn_metering_sent` (in tenants) and `cairn_tenant_usage` (on the control plane), migration 0071. Tested in `tests/test_metering_e2e.py` against stripe-mock.

### Status page

`CAIRN_STATUS` (JSON or a file) turns on a public status page: `title`, `public_url` (for the feed's links) and `components`, each a `name` and a health `url` (for example the API's `/health`, Cairn Store's `/-/healthy`, Cairn Catalog's `/-/ready`), with an optional `slow_ms`.

- **Probes.** The notifier checks each component every `every_seconds` (60): a GET that answers 2xx within `timeout_ms` (5,000) succeeds. A component is *operational*; *degraded* when it answers more slowly than `slow_ms` or one of its last three checks failed; in *outage* when its last two failed. Checks are kept 90 days, and each day's uptime is the share that succeeded.
- **Incidents and maintenance** (`MANAGE_SYSTEM_OPERATIONS`, audited): `POST /v1/status/incidents` with `title`, `components`, `impact` (`minor`, `major`, `critical`, or `maintenance` with `starts_ms` and `ends_ms`) and a first `message`; `POST /v1/status/incidents/<id>/updates` with `phase` (`investigating`, `identified`, `monitoring`, `resolved`; for maintenance `scheduled`, `in_progress`, `completed`) and `message`. An open incident raises its components' state to its impact (minor: degraded, major: partial outage, critical: outage); maintenance does while it runs and is listed ahead of time.
- **Public, without signing in:** `GET /status` (a page without scripts: the overall state, open incidents and maintenance, each component's state and 90 days of daily uptime, past incidents), `GET /status.json` and `GET /status/feed.atom` (incidents and their updates). What operators write is escaped. Tables `cairn_status_checks` and `cairn_status_incidents`, migration 0072. Tested against a real Cairn Store that is stopped and restarted in `tests/test_status_page_e2e.py`.

### Hosting on Hetzner

`deploy/hetzner/` builds a hosting cluster on Hetzner dedicated servers. Its README is the full runbook.

**What it sets up:**

- **Servers:** three nodes on a private vSwitch.
- **Firewall:** SSH and the Kubernetes API only from your addresses; the ingress only from the load balancer.
- **Kubernetes:** k3s with an embedded etcd, a WireGuard-encrypted pod network, secrets encrypted at rest and an API audit log.
- **Load balancer:** a Hetzner one, with the PROXY protocol (Terraform).
- **Shared services:** ingress-nginx, cert-manager (Let's Encrypt), CloudNativePG (a primary and two replicas with WAL archiving and nightly backups to Hetzner Object Storage), and kube-prometheus-stack.
- **Tenants:** created with `tenant.sh` and every option above. Each gets its own OpenSearch and Kafka.

```bash
deploy/hetzner/cluster.sh ~/cairn-hetzner/cluster.env prepare     # then: buckets, k3s, platform
deploy/hosting/tenant.sh create acme --licence acme.licence --values deploy/hetzner/platform-values.yaml
```

Three AX42 servers (about €200–260 a month with the load balancer and backups) take 5–8 small tenants. CI (job `hetzner`) runs the whole setup on a real machine and checks tenant isolation: the firewall, k3s, the platform services, and a tenant created by `tenant.sh`.

**Known limits:**

- **ingress-nginx:** Kubernetes retired the project in March 2026. It stays pinned because Cairn's chart uses its annotations.
- **Tenant OpenSearch and Kafka:** by default each runs on one node and doesn't fail over to another. If that node
  is down, the tenant waits for it; the search index can be rebuilt from the database. Use replicated services for
  tenants that must not wait.

### Replicated tenant services

With `CAIRN_TENANT_SERVICES_HA=1`, a tenant gets a three-node OpenSearch cluster and three Kafka brokers. The brokers
also form Kafka's controller quorum.

- **What a lost node does.** OpenSearch keeps one replica of every shard (Cairn Core's default), so all documents
  stay readable and writable with one node down. The cluster turns yellow, then green once the replicas are rebuilt.
  Kafka topics get three copies and need two in sync (`acks=all`), so a lost broker loses no write and stops none.
- **Two nodes lost.** Kafka refuses writes it could lose; they are not acknowledged. OpenSearch can't elect a
  cluster manager and stops answering until a node is back. The tenant waits; nothing it was told was saved is lost.
- **Spreading.** One pod of each per node (topology spread, `DoNotSchedule`), and a PodDisruptionBudget that lets
  at most one of each go at a time. Node drains and upgrades therefore never take the quorum away. This needs three
  nodes.
- **Setting.** The OpenSearch nodes run with `node.store.allow_mmap: false`, so the hosts need no
  `vm.max_map_count` raised (that would take a privileged init container, which tenant namespaces don't allow). This
  costs some search speed on large indices.
- **Chosen at creation.** `tenant.sh` records the layout and refuses to switch an existing tenant in place. To move a
  tenant from one layout to the other:
  1. Stop its release.
  2. Delete the `opensearch` and `kafka` StatefulSets and their volume claims, and the `cairn-tenant-services`
     ConfigMap.
  3. Run `tenant.sh upgrade` with the new setting.
  4. Rebuild the search index from the database (Cairn Core's reindex job). Events still in Kafka are lost, so
     do this when nothing is pending.
- **Tested.** CI runs the rendered manifest's containers, three of each in Docker, and stops nodes. It also runs the
  manifest on k3s and deletes pods there.

## Outages: what fails closed and what is kept

Tested by stopping real components under load in CI (chaos tests):

| Goes down | While it is down | When it is back |
|---|---|---|
| Cairn's API (the policy decision point Trino asks) | Trino refuses every query and write: nothing is allowed without a decision | Queries and writes go on; nothing refused was written |
| Cairn Catalog (process killed) | Writes to Iceberg tables fail; none is acknowledged | Writes go on; every acknowledged write is in its table exactly once |
| The agent gateway (process stopped) | Agents spool sealed batches on disk (`spool_max_bytes`) | Agents send them in order; a run that cannot finish within its lease is handed back |
| Cairn Core, behind the gateway | The gateway refuses batches it cannot store; agents keep and resend them | Every change lands once, in order |

Tested in `tests/test_trino_e2e.py::test_outages_under_load_fail_closed_and_lose_no_writes` (Trino, the store
and the catalog: three writers inserting without pause while Cairn is stopped and started and the catalog
process killed and restarted) and `tests/test_chaos_gateway.py` (a real gateway restarted and Core refusing
writes while an agent sends a run). Cairn Core's own outages are tested at its edge: quality results, Trino
lineage and usage wait in Cairn's database and are sent again (`tests/test_quality_engine.py`,
`tests/test_trino_access.py`).

The catalog and the store with `CAIRN_*_FSYNC=false` (tests only) now still write every commit: it survives
the process dying, though not the machine. Before, such commits stayed in memory until a later durable commit,
so a killed process lost them; the chaos test found it.

## Accessibility

Cairn's pages are checked with axe (the WCAG 2.1 A and AA rules) on every change: discover, search, an asset,
marketplace, requests, the assistant, context, governance, reports, pipelines, the SQL workspace and its
workflows, workbooks, input tables, metrics and git folders, query insight, dashboards, settings and extensions.
Each page is loaded in Chromium against a running Cairn with data in it, and none may show a serious or critical
violation (`tests/test_accessibility_e2e.py`). The check covers each page as it first renders. Dialogs, drawers and
other states opened from a page are not checked automatically.

antd 4's Select, which several pages use, gives screen readers a broken combobox while it is closed. The UI
bundle corrects it when it is built (`ui/scripts/rc-select-a11y-loader.js`), and the build stops if an antd
upgrade changes the code the fix applies to.

## Monitoring

Every Cairn service exposes Prometheus metrics at `/metrics`. Workers without an HTTP API (notifier, scheduler, context miner) serve them on `CAIRN_METRICS_PORT`; Helm sets it to 9108.

**Metrics:**

| Metric | What it measures |
|---|---|
| `cairn_http_requests_total`, `cairn_http_request_seconds` | requests and latency per service and route |
| `cairn_pdp_decisions_total{decision,mode}`, `cairn_pdp_decision_seconds` | authorization decisions and their latency |
| `cairn_quality_evaluations_total{type,status}`, `cairn_quality_incidents_total{event}` | monitor runs and incidents |
| `cairn_notifications_total{channel,outcome}`, `cairn_notifications_pending` | deliveries and backlog |
| `cairn_ai_tokens_total`, `cairn_ai_cost_usd_total`, `cairn_ai_budget_refusals_total` | AI use per task |
| `cairn_audit_events_total{category}` | audit log entries |
| `cairn_worker_last_success_timestamp_seconds{worker}`, `cairn_worker_errors_total` | background worker health |
| `cairn_proxy_masked_total{policy}` | values the core proxy masked |

**Dashboards and alerts** live in `deploy/observability/`:

- `cairn-overview.json`, a Grafana dashboard,
- `cairn-store.json`, the Cairn Store dashboard: requests and errors by operation, latency, capacity and disks, events, replication, queues, lifecycle and tiers, the cluster, and SFTP, for one node or all,
- `cairn-store-alerts.yaml`, the store's rules: a node down, server errors, disks filling up and full, an event backlog or dropped events, replication failing, a cluster node down or copies that cannot be placed, failing tier transitions, and many failed SFTP sign-ins,
- `cairn-alerts.yaml`, Prometheus rules (checked with `promtool` in CI) covering: a service down, server errors above 5%, slow requests, slow policy decisions, a spike in denials, a stalled or failing worker, a notification backlog or failing channel, a burst of quality incidents, the AI budget reached, and the licence expiring, read-only or out of seats.

**To run them:**

- **Compose:** `docker compose -f docker-compose.cairn.yml -f docker-compose.observability.yml up -d` adds Prometheus (`localhost:9090`) and Grafana (`localhost:3300`, with `GRAFANA_ADMIN_PASSWORD` in `.env`), pre-loaded with both.
- **Helm:** `cairn.serviceMonitor.enabled` scrapes the services and the workers (a ServiceMonitor and a PodMonitor), and the store at `/-/metrics`. `cairn.prometheusRule.enabled` installs the rules, and `cairn.grafanaDashboard.enabled` installs the dashboard as a ConfigMap for the Grafana sidecar.

**Tracing** is optional OpenTelemetry. Install the `otel` extra and set `CAIRN_OTEL_ENDPOINT` to an OTLP/HTTP collector. Requests, outgoing calls and database queries are then traced, sampled at `CAIRN_OTEL_SAMPLE_RATIO` (default 10%). Health and metrics endpoints are not traced.

## Limits

These are measured on the reference single-node Compose stack: 4 vCPU, 16 GB, one replica of each service and a catalog of about 110 datasets and 30 data products. The load is 10 concurrent clients. They are the numbers `tests/load/limits.yaml` holds Cairn to, with headroom over the measurement.

| Path | Throughput | p95 latency |
|---|---|---|
| Policy decision (`/governance/v1/authorize`) | at least 200 per second | under 100 ms |
| Badges for 20 search results (`/collab/enrich`) | at least 25 per second | under 600 ms |
| Dataset health | at least 90 per second | under 250 ms |
| Marketplace page | at least 14 per second | under 1.5 s |
| Inbox | at least 90 per second | under 250 ms |
| Contracts | at least 130 per second | under 200 ms |

On this node, throughput stops growing at about 10 concurrent clients; past that, only latency rises. Add replicas of the API and the policy service to serve more (see [High availability](#high-availability)).

**To measure your own deployment**, run the load test against it with a token that can see the catalog:

```bash
python tests/load/loadtest.py --api https://cairn.example.com/cairn/api --token "$TOKEN" --users 10 --duration 30
```

The command exits non-zero if a path breaks its limit. Pass `--limits your-limits.yaml` to check your own targets, and `--json out.json` to keep the measurements.

**Decisions at scale**, measured in CI on the real code (`tests/test_scale_decisions.py`), with 61 policies (team grants by schema, masks, denies):

| What | Measured | Held to |
|---|---|---|
| A cached policy decision | p50 0.011 ms, p99 0.04 ms, about 75,000 a second on one thread | p99 under 5 ms at 2,000 a second |
| Cairn Catalog's decision for a job reading 1,000 tables (one call) | 650 ms the first time, 120 ms warm | under 2 s, and 0.5 s warm |

The policy engine indexes policies by the privilege they grant, and only matching policies are evaluated, so a large policy set costs little for a privilege it does not mention. Explain mode (`/governance/v1/authorize?explain=true`) still evaluates every policy and says why each did or did not apply.

**Chaos** (`catalog/conformance/test_chaos.py`, three catalog replicas on PostgreSQL, run in CI):

- **A replica killed mid-commit.** One of three replicas is killed (SIGKILL) during 48 concurrent commits from six writers. The writers fail over to the other replicas. Every acknowledged commit is in the table exactly once, with one snapshot each. A client that was not told a commit's outcome checks the table before retrying.
- **The database restarts** under the replicas. Each one serves again without being restarted.
- **The store is killed** during writes and stays down for three seconds. Writes fail while it is down; afterwards the table reads in full, nothing acknowledged is missing, and writes resume. This holds with the store's production durability (`CAIRN_STORE_FSYNC` on). With it off, which only tests use, the same crash loses acknowledged writes, and this test caught exactly that.

**Security checks in CI** (`tests/test_route_security.py`). Every API route, called without credentials and with an untrusted actor header, is refused (401), except a short list of routes that are public by design: OAuth token endpoints, webhooks that check their sender's signature, and embeds with their own tokens. Each is listed with why. A new route that answers strangers fails the build until it is protected or added to the list on purpose. No route answers a stranger with a server error. API answers carry `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and `Referrer-Policy: no-referrer`, whatever proxy is in front. These checks are not a penetration test; an external one is still to do.

## High availability

`helm/cairn/values-ha.yaml` is a profile for production clusters:

```bash
helm install cairn ./helm/cairn -f my-values.yaml -f helm/cairn/values-ha.yaml
```

- **Stateless services** (API, policy service, core proxy) run 3 replicas, and the MCP server and workspace UI run 2. Each component's replicas are spread over nodes and zones (`cairn.ha.spreadReplicas`), and a PodDisruptionBudget keeps enough of them up during node drains.
- **Workers** (scheduler, notifier, context miner) run 2 replicas. A Postgres advisory lock makes one of them the leader for each cycle, so work is never done twice. If the leader's node goes away, the standby takes over on its next cycle.
- **The gateway** runs more than one replica only when its keys come from a shared Secret (`gateway.keys.existingSecret`). Without one, the keys live on a volume, the gateway runs a single replica and it gets no PodDisruptionBudget.
- **Databases:** point `global.*` at managed, replicated Postgres, MySQL and OpenSearch. The databases the chart can run are single instances.
- **Cairn Store** runs as one node by default. For high availability, run it as a cluster (`cairn.store.cluster.enabled`, with its metadata in replicated PostgreSQL): every object is kept on several nodes, and any node serves every request. See [Clusters](#cairn-store).

## Cairn Store

Cairn Store is Cairn's own S3-compatible object store, for lakehouse tables (Iceberg data and metadata) and metadata exports. Any S3 client works with it: Trino, Spark, pyiceberg, pyarrow, DuckDB, the AWS CLI and SDKs.

- **Docker Compose:** service `cairn-store`, S3 endpoint `http://cairn-store:8333` inside the stack and `http://localhost:8333` from the host. Set `CAIRN_STORE_ACCESS_KEY` and `CAIRN_STORE_SECRET_KEY` in `.env`; the bucket `lakehouse` is created at start.
- **Kubernetes:** set `cairn.store.enabled=true` and create the Secret named in `cairn.store.secret`. The chart runs one replica on a persistent volume. Engines in other namespaces need an entry in `cairn.networkPolicy.storeIngressFrom`.

**Keys.** The access key from the environment is an admin key. `CAIRN_STORE_CREDENTIALS_FILE` adds more keys, as a JSON list. Each key may be limited to certain buckets (`"buckets": ["lake-*"]`), to reading (`"read_only": true`), or by its own IAM-style policy (`"policy": {...}`, which replaces the bucket list). Only admin keys (`"admin": true`) create and delete buckets and change bucket settings, unless a key policy grants it. Requests are signed with AWS Signature Version 4, or Version 2 for older tools.

**Access policies.** Each bucket can have a bucket policy in the S3 policy language (`PutBucketPolicy`). It can grant other keys access to parts of the bucket, or deny access under conditions. Supported conditions: source IP, time, object tags (`s3:ExistingObjectTag/<key>`, `s3:RequestObjectTag/<key>`), list prefix, signature type, user agent and referer. An explicit `Deny` always wins, even for admin keys; admin keys can still read, change and delete the bucket policy, so a policy cannot lock them out. Policies are checked when they are stored: an unknown operator or condition key is refused with `MalformedPolicy`, never stored and ignored.

**Public access.** Anonymous requests are refused, and public bucket policies are rejected, unless `CAIRN_STORE_BLOCK_PUBLIC_ACCESS=false`. With it off, a bucket policy for `"Principal": "*"` can allow anonymous reads (for example, to serve static files), and each bucket's public access block (`PutPublicAccessBlock`) can still restrict that. `GetBucketPolicyStatus` reports whether a bucket is public.

**Cairn policies.** With `CAIRN_STORE_PDP_URL` (for example `http://cairn-pdp:8810`) and `CAIRN_STORE_PDP_TOKEN` (a Cairn service token), the store also asks Cairn's policy decision point about every request its own rules allow. It asks as the key's Cairn identity (`"actor"` in the credentials file), with the privilege named after the S3 action (`s3:GetObject`) and the resource `arn:aws:s3:::bucket/key`. A Cairn `deny` policy refuses the request. While the decision point cannot be reached, requests are refused (fail closed). Decisions are cached for 30 seconds. In Helm, set `cairn.store.pdp.enabled=true`.

**What it supports.** Buckets; objects with metadata; ranged reads; conditional reads and writes (`If-Match`, `If-None-Match: *`); listing (versions 1 and 2); batch deletes; copies; multipart uploads, including copying parts; checksums (CRC32, CRC32C, CRC64NVME, SHA-1, SHA-256, composite and full-object); presigned URLs; and object and bucket tags (up to 10 per object, set with the object, by copy or later, and 50 per bucket).

**Ownership and ACLs.** The bucket owner owns every object (S3's `BucketOwnerEnforced`). Access comes from keys, never from ACLs: ACL requests that only grant the owner full control succeed, and any other grant is refused with `AccessControlListNotSupported`.

**CORS.** Each bucket can have CORS rules (`PutBucketCors`), so browser apps such as the Cairn UI can upload and download with presigned URLs. Browser preflight requests are answered from the bucket's rules without a signature. Responses to allowed origins carry the CORS headers, error responses included. A bucket without rules refuses every cross-origin request.

**Versioning.** Turn it on per bucket (`PutBucketVersioning`, `Enabled` or `Suspended`). Overwrites and deletes then keep the previous version. A delete adds a delete marker, and deleting the marker brings the object back. Any version can be read, copied, tagged or permanently deleted by its version ID; `ListObjectVersions` lists them all. Old versions count towards stored bytes until they are permanently deleted (or expired by a lifecycle rule, once those are supported). Reading or deleting a specific version needs the version permissions (`s3:GetObjectVersion`, `s3:DeleteObjectVersion`). MFA delete is not supported.

**Lifecycle rules.** Each bucket can have lifecycle rules (`PutBucketLifecycleConfiguration`). Rules can expire current objects a number of days after they were written, or on a date. They can also remove delete markers that are the last thing left of an object, expire noncurrent versions (optionally keeping the newest few), and abort multipart uploads left unfinished. A rule can select objects by prefix, tags and size. The store applies the rules in the background, every `CAIRN_STORE_LIFECYCLE_INTERVAL` seconds (default 3600). It only expires the version it checked, so an object rewritten in the meantime is kept. Responses for objects that will expire carry `x-amz-expiration`. Rules can also move objects, or noncurrent versions, to a storage tier (below). Metrics: `cairn_store_lifecycle_*_total`.

**Times to live.** Besides rules, a single object can be given a time to live.
- **On a write.** Send `x-cairn-ttl` on `PutObject`, `CopyObject` or `CreateMultipartUpload`. The value is a number of seconds, or a number followed by `s`, `m`, `h`, `d` or `w` (at most 100 years).
  - A copy and a completed multipart upload are new objects, so their time counts from when the copy is made or the upload completes.
  - `GET` and `HEAD` answer with `x-cairn-expires`.
- **A bucket default.** `PUT /-/admin/v1/buckets/<bucket>/ttl` with `{"ttl": "7d"}` (or `{"seconds": n}`) gives every new object in the bucket that time to live. `DELETE` removes the default. A write with `x-cairn-ttl: 0` opts out. Setting and removing the default is recorded in the audit trail, and the bucket's admin view shows `ttl_seconds`.
- **When the time runs out.** The object is gone at once for reads and listings on every protocol (S3, file API, WebDAV, SFTP, mounts), and its key can be written again. The lifecycle worker then deletes it, counted in `cairn_store_lifecycle_ttl_expired_total`. In versioned buckets it deletes the expired version itself rather than adding a delete marker, so its bytes go too; earlier versions stay.
- **Refusals.** A time to live is refused in buckets with Object Lock, since a locked object must not vanish. It is also refused when `CAIRN_STORE_LIFECYCLE_INTERVAL` is 0, because nothing would delete expired objects.

**Object lock.** Buckets created with object lock (`x-amz-bucket-object-lock-enabled: true`), or versioned buckets that get an object lock configuration, keep versions write-once:

- **Retention.** A version can carry retention until a date, in `GOVERNANCE` or `COMPLIANCE` mode, set per object or from the bucket's default retention.
- **Compliance mode.** The version cannot be deleted, and its retention cannot be shortened, by anyone, including admin keys, until the date passes.
- **Governance mode.** The same, unless a request with `x-amz-bypass-governance-retention: true` comes from a key allowed `s3:BypassGovernanceRetention`.
- **Legal hold.** A legal hold blocks deletion until it is removed.
- **Where it is enforced.** The engine enforces protection, so client deletes, batch deletes and lifecycle rules all respect it. Overwrites and plain deletes keep the locked version, as a noncurrent version under a delete marker.
- **Versioning.** Versioning cannot be suspended on such a bucket.

**Browser uploads.** HTML forms can upload straight to a bucket with a signed POST policy, as produced by any AWS SDK's `generate_presigned_post` (SigV4 or SigV2). The store checks the signature and the expiration. It also checks every condition (exact values, `starts-with`, `content-length-range`), and that every form field is covered by one, so a browser cannot add fields the policy did not allow. The upload is authorized as a PUT for the key that signed the policy. Supported form fields: object metadata, content type, tags, encryption, object lock, `success_action_status` and `success_action_redirect`. The file is streamed to disk and its size limits are enforced as it arrives. Allow the web app's origin with CORS rules.

**TLS.** Set `CAIRN_STORE_TLS_CERT` and `CAIRN_STORE_TLS_KEY` (PEM files) to serve HTTPS. The files are re-read when they change, so renewed certificates take effect without a restart, and a broken renewal is refused while the current certificate stays in use.

- **Mutual TLS.** `CAIRN_STORE_TLS_CLIENT_CA` requires client certificates signed by that CA. Requests are still signed with the client's key; mutual TLS adds proof of which machine is connecting.
- **What TLS turns on.** With TLS, `aws:SecureTransport` is true in policies, and SSE-C is accepted.
- **Outbound HTTPS.** Webhooks and Cairn's policy decision point can be `https://`. They trust the public roots plus `CAIRN_STORE_CA_FILE`.
- **Helm.** `cairn.store.tlsSecret`, a `kubernetes.io/tls` Secret (cert-manager works), and optionally `cairn.store.clientCaSecret`.
- **Kafka.** Event delivery to Kafka is plaintext for now; keep the broker inside the deployment's network.

**Audit trail.** The store keeps an append-only, hash-chained audit trail in Cairn's audit format: each entry's hash is `sha256(prev_hash ‖ canonical JSON)`, so any change to a past entry, or a missing entry, is detected. An entry is written in the same transaction as the change it records. Recorded:

- bucket creation and deletion;
- every bucket configuration change (policy, CORS, lifecycle, encryption, website, notifications, logging, tags, public access block, object lock), with the document's SHA-256;
- versioning changes and quotas;
- object-lock retention and legal-hold changes, and deletes that bypassed governance retention;
- requests from signed keys that were refused. Anonymous refusals are only logged.

Export the trail with `GET /-/admin/v1/audit?after=<seq>&format=jsonl` (SigV4, admin key) and check it with `cairn audit verify-file` (use `--after-head` to prove that consecutive exports join up). On a stopped store, `cairn-store audit-verify -data /data` checks the chain too.

**Access logs.** `PutBucketLogging` writes a bucket's requests, in S3's server access log format, to objects in a target bucket every `CAIRN_STORE_ACCESS_LOG_FLUSH_SECONDS` (default 60). The requester must be allowed to write to the target. Like S3's, these logs are best effort; the audit trail is the record of changes. `CAIRN_STORE_LOG_REQUESTS=true` also writes one structured JSON log line per request.

**Event notifications.** Buckets can send events when objects are created (`Put`, `Post`, `Copy`, `CompleteMultipartUpload`), removed (`Delete`, `DeleteMarkerCreated`), tagged or expired by lifecycle rules. Rules are set with `PutBucketNotificationConfiguration` and can filter by key prefix and suffix. Events use S3's event JSON format.

- **Targets.** Events go to targets the operator defines in `CAIRN_STORE_EVENT_TARGETS_FILE`. A rule names its target as the last part of the destination ARN (`arn:cairn:store:::lake`).
  - **Webhooks:** `{"type": "webhook", "id": "cairn", "url": "http://...", "secret": "..."}`.
  - **Kafka topics:** `{"type": "kafka", "id": "lake", "brokers": ["kafka:9092"], "topic": "..."}`.
  - **Amazon SQS queues:** `{"type": "sqs", "id": "q", "queue_url": "https://sqs.eu-west-1.amazonaws.com/<account>/<queue>", "access_key": "...", "secret_key_file": "..."}`.
    - `region` is read from the queue URL, or given for other SQS-compatible services.
    - The event id is sent as the `x-cairn-event-id` message attribute.
    - FIFO queues (`.fifo`) get the bucket as message group and the event id as deduplication id.
  - **Google Cloud Pub/Sub topics:** `{"type": "pubsub", "id": "ps", "topic": "projects/<p>/topics/<t>", "auth": "service_account", "credentials_file": "..."}`.
    - `auth` works as for GCS remotes: `service_account`, `metadata` or `none`.
    - `endpoint` defaults to Google's.
    - Attributes `x-cairn-event-id` and `key` (`<bucket>/<key>`) are set on each message.
    - `"ordered": true` publishes with the bucket as ordering key.
  - **NATS subjects:** `{"type": "nats", "id": "n", "url": "nats://nats:4222", "subject": "cairn.lake"}`.
    - Use `tls://` for TLS, verified against the system roots and `CAIRN_STORE_CA_FILE`.
    - Optional `token` (or `token_file`), or `user` and `password` (or `password_file`).
    - An event counts as delivered once the server has processed it.
    - The event id goes as the `Nats-Msg-Id` header, which JetStream uses to drop duplicates. Bind a JetStream stream to the subject to keep messages for consumers that are away.

  The store will not start with a target that does not fit its type, such as a bad URL scheme, a malformed Pub/Sub topic, a NATS subject with spaces or wildcards, or missing credentials. Deliveries are at least once, so consumers should drop repeats by the event id.
- **Webhook signatures.** Webhook bodies are signed: `X-Cairn-Signature: sha256=<HMAC-SHA256 of the body with the target's secret>`.
- **No lost events.** An event is recorded in the same transaction as the change it describes. It is delivered at least once, in order for each target, with retries and backoff, and survives a crash or restart of the store.
- **Delivery that keeps failing.** Events still undeliverable after `CAIRN_STORE_EVENT_MAX_AGE_HOURS` (default 168) are dropped and counted.
- **Metrics.** `cairn_store_events_{delivered,failed,dropped}_total` and `cairn_store_events_pending`.
- **Helm.** Put the targets file in a Secret and set `cairn.store.eventTargetsSecret`.

**File API and directory browser.** `/-/files/<bucket>/<path>` serves buckets as plain files and directories, for scripts and browsers.

- `GET` or `HEAD` a file reads it (ranges and conditional requests work); `PUT` writes it; `DELETE` removes it. `?versionId=` picks a version.
- `GET` a directory (a path ending in `/`, a bucket, or `/-/files/` for the list of buckets) lists it, as JSON (`{"path", "entries": [{"name", "type", "size", "modified", "etag", "storage_class"}], "next"}`) or, for browsers (`Accept: text/html`), as an HTML page. Page through with `?limit=` (up to 1000) and `?after=<next>`.
- **Same rules as S3.** Each request is the S3 operation it stands for (GetObject, PutObject, DeleteObject, ListObjectsV2 with a `/` delimiter, ListBuckets), so bucket and key policies, Cairn policies, quotas, encryption, events, the audit trail and access logs all apply. Only `versionId`, `limit` and `after` are accepted; S3 subresources such as `?acl` stay on the S3 API.
- **Image thumbnails.** `GET` an image with `?width=`, `?height=` and optionally `?fit=contain|cover|fill` (contain by default: fit inside the box, keeping proportions) returns a resized copy in the image's own format, for JPEG, PNG, GIF (first frame) and WebP. The read is the GetObject it stands for: authorized, audited and metered the same way. Images are never enlarged. Sources are limited to 64 MiB and 50 megapixels, targets to 4096 x 4096, and a resize cannot be combined with a `Range`. The copy carries its own ETag and is not cached by the store, so put a CDN or cache in front for busy thumbnails.
- **Signing in.** Sign requests with SigV4 (`curl --aws-sigv4 "aws:amz:us-east-1:s3" --user KEY:SECRET https://store/-/files/lake/report.csv`) or use a presigned URL. Anonymous requests work only where a public bucket policy allows them. For browsers, `CAIRN_STORE_FILES_BASIC_AUTH=true` also accepts HTTP Basic (the access key and secret key). This needs TLS, because Basic sends the secret with every request; set `CAIRN_STORE_FILES_BASIC_ALLOW_HTTP=true` only when TLS ends in front of the store. In Helm: `cairn.store.filesBasicAuth`.
- **Browser safety.** Files are served with `Content-Security-Policy: sandbox` and `nosniff`, so an HTML file in a bucket cannot run script with the viewer's credentials. Listing pages allow no script. Writes take no form posts, so another site cannot write through the viewer's browser unless the bucket's CORS rules allow that site.
- Errors are JSON: `{"error": "NoSuchKey", "message": "..."}`.

**WebDAV.** `/-/dav/<bucket>/<path>` serves buckets over WebDAV (classes 1 and 2), for rclone, davfs2, cadaver, WinSCP and other WebDAV clients. For example: `rclone config create cairn webdav url=https://store.example.com/-/dav/ vendor=other user=KEY pass=SECRET`.

- **Methods.** PROPFIND (depth 0, 1 and infinity; infinity only for trees of up to 10,000 keys), GET, HEAD, PUT, DELETE, MKCOL, COPY, MOVE, PROPPATCH, LOCK and UNLOCK.
- **Properties.** PROPPATCH sets and removes dead properties: a request's changes all apply or none do. They are kept as text in the object's user metadata (`x-amz-meta-dav-props`), so they share S3's 2 KB per object; past that the change fails with `507`. A collection keeps its properties on its marker object. Live properties (`DAV:getetag` and the like) are protected: changing one answers `403`, and the rest of that request `424`. A change rewrites the object's metadata (a copy onto itself), conditional on the version read.
- **Locks.** LOCK takes an exclusive or shared write lock, to depth 0 or infinity, for `Timeout` seconds (600 by default, 3600 at most; clients refresh to keep it). LOCK on an unmapped URL creates an empty file.
  - **Who can use a lock.** A WebDAV write to a locked resource must present the lock's token in an `If` header (`423 Locked` otherwise), and only the key that took the lock can use its token, refresh it or UNLOCK it.
  - **What a lock covers.** A lock to depth infinity covers everything below its collection, members added later too. Writes that add or remove a member also need the token of a lock on its collection.
  - **Shared and exclusive.** Shared locks coexist; an exclusive lock never shares.
  - **`If` headers** are evaluated in full (state tokens, entity tags, `Not`, tagged lists): one that does not hold fails the request with `412`.
  - **Shared by every node.** Locks are kept in the store's metadata (embedded or PostgreSQL), so every node of a cluster honours them. Expired locks are cleared.
  - **WebDAV only.** Locks bind WebDAV clients. S3, SFTP and the mount do not take part, because S3 has no locking. Use object lock (retention) when every protocol must be stopped.
- **Tested** with litmus, the WebDAV protocol test suite: `basic`, `copymove`, `props` and `locks` all pass. Further tests cover tokens bound to their key, deep locks over new members and moves, conflicts both ways, shared locks, expiry, and lock discovery.
- **Collections** are key prefixes. MKCOL writes an empty `name/` marker object, as S3 consoles do. As WebDAV requires, PUT and MKCOL into a collection that does not exist answer 409 and create nothing. This check is made only for callers who may write the resource and list its parent, so write-only keys (drop boxes) learn nothing about what exists. MKCOL on `/-/dav/<bucket>` creates the bucket, for keys allowed to create buckets. Whole buckets cannot be deleted, copied or moved over WebDAV.
- **Same rules as S3.** Each item is the S3 operation it stands for (ListObjectsV2, GetObject, PutObject, CopyObject, DeleteObject), with the same policies, quotas, encryption, events and audit trail. A collection DELETE, COPY or MOVE first checks every key it would touch and changes nothing if any is refused. These operations are limited to 10,000 keys; use the S3 API for larger trees. A MOVE is a copy followed by a delete, so it is not atomic.
- **Renames are atomic.** A MOVE within an unversioned bucket, of one file or a whole directory, is one index transaction: every key moves with its data, metadata and modification time, or none does, and no bytes are copied. Every key is authorized first, encrypted objects get their data keys re-wrapped for their new names, events and replication see a delete and a copy per key, and the move is one `object.rename` entry in the audit trail. Moves across buckets, or in a versioned bucket (where history stays with the old keys), are a copy and then a delete. SFTP renames and renames through `cairn-store mount` use the same path, for directories too.
- **Signing in** works as for the file API. WebDAV clients use HTTP Basic, so turn on `CAIRN_STORE_FILES_BASIC_AUTH` and serve TLS.

**SFTP.** Set `CAIRN_STORE_SFTP_ADDR` (for example `:2022`) to serve SFTP. It works with `sftp`, WinSCP, FileZilla, rclone and other file-transfer clients. In Helm: `cairn.store.sftp.enabled`, with a separate `-store-sftp` Service and network-policy peers in `cairn.store.sftp.ingressFrom`.

- **Paths** are `/<bucket>/<key>`. `/` lists the buckets the key may see. `mkdir` on a bucket name creates the bucket, for keys allowed to create buckets.
- **Signing in.** The SSH user name is an access key. It signs in with its secret key as the password (turn this off with `CAIRN_STORE_SFTP_PASSWORD_AUTH=false`), or with an SSH public key listed under `"ssh_keys"` for that key in the credentials file. A key that does not parse stops the store at start-up. Only the `sftp` subsystem is served: no shell, commands or forwarding. Refused sign-ins are logged and counted (`cairn_store_sftp_auth_failures_total`).
- **Host key.** `CAIRN_STORE_SFTP_HOST_KEY` (an OpenSSH private key). Without it, an Ed25519 key is created as `sftp_host_ed25519_key` in the data directory on first start and kept, so clients see the same host key after restarts. Its fingerprint is logged at start-up. In Helm, `cairn.store.sftp.hostKeySecret` mounts one from a Secret.
- **Same rules as S3.** Each operation is the S3 operation it stands for: a ranged GetObject per read, PutObject when an upload is closed, DeleteObject, CopyObject, and ListObjectsV2 with a `/` delimiter. Policies, Cairn policies, quotas, encryption, events and the audit trail all apply.
- **Uploads** are staged in the data directory's temporary area and stored in one step when the file is closed, so readers never see half a file. A dropped connection leaves nothing behind.
- **Refused rather than faked:** appends, in-place updates and out-of-order writes (files are written from the start, in order); changing permissions or timestamps; links; and renaming directories (use WebDAV MOVE or the S3 API). A rename never overwrites an existing file.

**Mounting a bucket (FUSE).** `cairn-store mount <bucket>[/prefix] <directory>` shows a bucket, or a prefix of it, as a local directory on Linux, for tools that only work with files. The `cairn-store` binary is static, so it can be copied to any Linux host with FUSE. Unmount with `fusermount3 -u <directory>` (or `umount`).

```sh
CAIRN_STORE_ENDPOINT=https://store.example.com CAIRN_STORE_ACCESS_KEY=... CAIRN_STORE_SECRET_KEY=... \
  cairn-store mount lake/raw /mnt/raw            # add --read-only, or --allow-other to share the mount
```

- **An ordinary client.** The mount talks to the store over the S3 API, signed with the key it is given, so that key's policies, Cairn policies, quotas, encryption, events and the audit trail apply as for any client. The mount fails at once if the key cannot list the bucket.
- **Reads** are ranged GETs. **Writes** are staged in a private, sparse temporary file (`CAIRN_STORE_MOUNT_STAGING`, default the system temporary directory) and stored when the file is closed or flushed, so other readers never see half a file. Overwriting a file (`>`, `cp`) replaces it in one step.
- **Appends and edits in place** work, as do writes at any offset, holes and `truncate` to any size. The file is handled in 8 MiB chunks: a chunk is fetched only when a write covers part of it, and a file over 16 MiB is stored as a multipart upload whose unchanged chunks are copied inside the store, so changing a few bytes of a large file sends one chunk. Each store is conditional on the version the edit started from: if the file changed elsewhere meanwhile, the close fails with `ESTALE` and the other version stays.
- **Directories** are key prefixes. `mkdir` writes an empty `name/` marker object, as S3 consoles do. `rmdir` works only on empty directories.
- **Modes, owners, times and extended attributes** are kept in each object's user metadata, under the same headers s3fs-fuse uses: `x-amz-meta-mode`, `-uid`, `-gid`, `-mtime` and `-xattr`. So `chmod`, `chown`, `touch`, `cp -p`, `tar` and `rsync -a` work, and S3 clients see the values.
  - New files and directories keep the mode they were created with.
  - A change rewrites the object's metadata in place: a copy onto itself, conditional on the version it read, so a concurrent writer is never overwritten.
  - A directory that exists only because keys sit below it gets a marker object to hold its metadata.
  - Objects without these headers show as `0644` files and `0755` directories, owned by the mount's user (`--uid`, `--gid`).
  - Extended attributes must be in the `user.` namespace. They share S3's 2 KB of user metadata per object; going over it fails with `E2BIG`.
  - The access time is not kept.
- **Permissions.** The store's policies always decide what the mount's key may do. With `--enforce-modes` the kernel also checks modes and owners before every access, as on a local disk (use it with `--allow-other` when several local users share a mount).
- **Symbolic links** are objects whose content is the target, with a mode marking them as links. **Hard links** fail with `EPERM`: a bucket cannot give one object two names.
- **Renames** of files and directories are one transaction in an unversioned bucket, and keep metadata. In a versioned bucket a file rename is a copy and a delete, and a directory rename returns `EXDEV`, so `mv` copies and deletes instead.
- **Tested** (`store/conformance/test_mount.py`):
  - modes, owners and times, read back after a rewrite, a rename and a new mount;
  - a directory implied by its keys gets a marker;
  - creation modes;
  - symbolic links, including relative and dangling ones;
  - extended attributes, including create-only, a foreign namespace and the size limit;
  - hard links refused;
  - with `--enforce-modes`, another local user can read a `0644` file but not a `0600` one.

**Kubernetes volumes (CSI).** `cairn-store csi` is a CSI driver (`csi.cairn.store`): PersistentVolumeClaims of its StorageClass become Cairn Store buckets, mounted into pods with the FUSE client above. Enable it in Helm with `cairn.store.csi.enabled`. That runs it on every node, with the standard registrar and provisioner sidecars, and creates the `cairn-store` StorageClass.

- **Volumes.** Each claim gets a bucket of its own, named after the volume, with the requested size as a hard quota. Or, with the StorageClass parameter `bucket` (`csi.storageClass.bucket`), each claim gets a prefix of one shared bucket. Each volume carries a marker object, so a bucket that already exists and is not a volume is never taken over. Deleting a claim (reclaim policy `Delete`) deletes its objects and bucket. Versioned and object-locked buckets are refused, so their history is not half deleted.
- **Mounts.** Any access mode works (many pods on many nodes can mount a bucket); block volumes are refused. Read-only claims and publishes mount read-only. The mount options `uid=N,gid=N` set who owns the files, for pods that run as non-root; other options are refused rather than ignored. A volume uses the key in its node-publish secret (`access_key`, `secret_key`) when given, otherwise the driver's own key, which must be allowed to create buckets.
- **Limits.** No snapshots, cloning or expansion. The driver container runs privileged, as FUSE drivers must, and needs `/dev/fuse` on each node.

**Hadoop, Spark and Hive.** Hadoop's S3A file system (`hadoop-aws`, with Hadoop 3.4) works with Cairn Store as it is. The conformance suite runs Hadoop's own client against the store: directories, small and multipart writes, positioned reads, listings, file and directory renames and recursive deletes. The settings:

```properties
fs.s3a.endpoint=https://store.example.com
fs.s3a.endpoint.region=us-east-1
fs.s3a.path.style.access=true
fs.s3a.access.key=...           # an access key scoped to the buckets the job uses
fs.s3a.secret.key=...
```

Use `s3a://<bucket>/<path>` paths. In Spark, prefix each setting with `spark.hadoop.`. S3A renames are copies followed by deletes, as on any S3 store, so write job output with an S3A committer (`fs.s3a.committer.name=magic` or `directory`). Hadoop sees the key's permissions: a key limited to some buckets cannot open others.

**Remote buckets: S3, Azure Blob Storage and Google Cloud Storage.** Storage tiers, mounted remote buckets and backup sinks all name a bucket elsewhere with the same fields. `kind` says how the store talks to it: `s3` (the default), `azure` or `gcs`. Azure and GCS are spoken natively, through their own APIs and sign-in, not through an S3 gateway.

- **`s3`**: `endpoint`, `bucket`, `prefix`, `access_key`, `secret_key` (or `secret_key_file`), `region` (default `us-east-1`). This covers AWS S3, MinIO, another Cairn Store and other S3-compatible services.
- **`azure`**: `bucket` is the container. `access_key` is the storage account. Sign in with one of two:
  - the account key in `secret_key` (or `secret_key_file`), which uses Shared Key signing;
  - a SAS token in `sas` (or `sas_file`).

  `endpoint` defaults to `https://<account>.blob.core.windows.net`; for Azurite, give `http://host:10000/<account>`. Blobs above 64 MiB are uploaded in 16 MiB blocks. User metadata whose name Azure does not allow (S3 allows `-`, Azure does not) is stored under an encoded name and read back under the original one.
- **`gcs`**: `bucket`, `prefix`, and `auth`, which is one of:
  - `service_account`: `credentials_file` is a service account JSON key. The store signs its token request and caches the token.
  - `metadata`: the service account of the VM or pod, from the metadata server. On GKE, bind the store's Kubernetes service account to a Google service account with workload identity, and set `cairn.store.serviceAccountName`.
  - `none`: emulators only.

  `endpoint` defaults to `https://storage.googleapis.com`.

Example: `[{"id": "lake", "kind": "gcs", "bucket": "acme-lake", "prefix": "raw/", "auth": "service_account", "credentials_file": "/etc/cairn-store-remotes/sa.json"}, {"id": "archive", "kind": "azure", "bucket": "archive", "access_key": "acmestore", "secret_key_file": "/etc/cairn-store-remotes/azure-key"}]`.

The store will not start with a field that does not apply to its kind (a `region` on Azure, keys on GCS), an unknown `kind`, or missing credentials. In Helm, the other keys of the Secret that holds `remotes.json`, `tiers.json` or `sinks.json` are mounted next to it, so key files can live in the same Secret. HDFS is not a remote kind: reach Hadoop data through S3A in the other direction (above).

**Storage tiers.** Lifecycle transitions can move object data to a bucket elsewhere (S3, another Cairn Store, Azure Blob Storage or Google Cloud Storage; see above), to keep cold data off the store's disk.

- **Tiers.** The operator defines them in `CAIRN_STORE_TIERS_FILE`: `[{"storage_class": "STANDARD_IA", "endpoint": "https://s3.eu-west-1.amazonaws.com", "bucket": "lake-cold", "prefix": "cairn/", "access_key": "...", "secret_key_file": "/etc/...", "region": "eu-west-1"}]`, with the fields of any remote bucket (above) and a `storage_class` instead of an `id`. A tier's storage class is one of `STANDARD_IA`, `ONEZONE_IA` or `GLACIER_IR`: classes that are read without a restore. In Helm, put `tiers.json` in a Secret and set `cairn.store.tiersSecret`.
- **Rules.** `Transition` (after `Days` or on a `Date`) and `NoncurrentVersionTransition` (after `NoncurrentDays`) name a tier by its storage class. A rule naming any other class is refused with `InvalidStorageClass`. Expiration wins over a transition due at the same time.
- **How data moves.** Each blob is copied to the tier exactly as it is stored on disk, so with encryption at rest the tier only ever holds ciphertext. The version's index entry then points at the tier, in one transaction and only if the version was not rewritten meanwhile. The local copy is then deleted. A failed copy is retried on the next pass.
- **Reads** are transparent: `GetObject`, ranges, copies and replication fetch the bytes from the tier. `HeadObject` and listings report the storage class. Copying an object onto itself with `StorageClass=STANDARD` brings it back to the store's disk.
- **Deletes.** Deleting a tiered version deletes its blobs in the tier, from the lifecycle worker; a tier that is down is retried.
- **Writes** are always `STANDARD` (`REDUCED_REDUNDANCY` is accepted and stored as `STANDARD`). Any other `x-amz-storage-class` is refused: objects reach a tier only through a transition.
- **Backups** do not copy tiered data. Protect the tier bucket with its own versioning or replication.
- **Metrics.** `cairn_store_tier_transitions_total`, `_transitioned_bytes_total`, `_transition_failures_total`, `_reads_total`, `_deletes_total`.

**Mounting remote buckets.** A local bucket can mirror a bucket elsewhere (AWS S3, MinIO, another Cairn Store, an Azure container or a GCS bucket; see above), so clients and engines use one store while the data stays where it is. Define remotes in `CAIRN_STORE_REMOTES_FILE`: `[{"id": "erp-exports", "endpoint": "https://s3.eu-west-1.amazonaws.com", "bucket": "acme-erp", "prefix": "exports/", "access_key": "...", "secret_key_file": "/etc/...", "region": "eu-west-1"}]`. Then mount one: `cairn-store admin remote-mount <bucket> --remote erp-exports [--write-back]` (or `PUT /-/admin/v1/buckets/<bucket>/remote`).

- **Sync.** Every `CAIRN_STORE_REMOTE_SYNC_INTERVAL` seconds (60), or on `remote-sync <bucket>`, the store lists the remote and makes each of its current objects a local object whose bytes stay on the remote: keys, sizes, ETags, content types and user metadata are local, so listings and `HeadObject` are fast. Objects changed or deleted on the remote follow at the next pass. A change made here and the remote's are settled as in two-way sync: the newer one wins.
- **Reads** fetch the bytes from the remote, ranges included, so nothing is copied until it is read. `remote-cache <bucket> [--prefix P]` copies objects' bytes to this store (encrypted at rest like any new object), so reads stay local and work while the remote is down. `remote-uncache` drops the local copy of objects the remote still has unchanged.
- **Write-back.** With `--write-back`, objects written or deleted here are written to or deleted on the remote, through the outbox: at least once, in order, retried while the remote is down. Without it, the mount is read-only towards the remote: local writes stay local, and local deletes do not touch the remote.
- **Rules.** A remote is mounted in one bucket at a time; `remote <bucket>` shows the mount, and how many objects are remote-only, cached and local-only. `remote-unmount` stops syncing and writing back; objects already mirrored stay readable while the remote is configured. Only the remote's current objects are mirrored, not its old versions. Mounts and unmounts are recorded in the audit trail. In Helm, put `remotes.json` in a Secret and set `cairn.store.remotesSecret`.

**Replication.** A bucket can copy its new objects to a bucket on another S3 endpoint (another Cairn Store, or any S3), set with `PutBucketReplication`.

- **Destinations.** The operator defines them in `CAIRN_STORE_REPLICATION_TARGETS_FILE`: `[{"id": "dr", "endpoint": "https://store.dr.example.com", "access_key": "...", "secret_key_file": "/etc/...", "region": "us-east-1"}]`. A rule names one as its Role (`arn:cairn:store:::dr`) and gives the destination bucket (`arn:aws:s3:::lake-copy`), so bucket owners can only send data where the operator allowed. In Helm, put `targets.json` in a Secret and set `cairn.store.replicationTargetsSecret`.
- **What is copied.** Versioning must be enabled. New versions matching a rule (by prefix and tags; rules have priorities) are copied with their metadata, tags, legal hold and the retention still in force (the destination bucket then needs object lock); delete markers too when the rule says `DeleteMarkerReplication: Enabled`. Permanent deletes of a version are not replicated, as in S3. Objects larger than `CAIRN_STORE_REPLICATION_MULTIPART_BYTES` (default 256 MiB) are sent as multipart uploads of `CAIRN_STORE_REPLICATION_PART_BYTES` (default 64 MiB).
- **Delivery.** A copy is recorded in the same transaction as the write, then delivered at least once and in order per destination, retried with backoff while the destination is down. `HeadObject` reports `x-amz-replication-status`: `PENDING`, then `COMPLETED`, or `FAILED` when the destination refuses the copy for good (for example, a missing bucket or a refused key) or it is still undeliverable after `CAIRN_STORE_EVENT_MAX_AGE_HOURS`.
- **Encryption.** Encrypted objects are decrypted and sent over the connection, so use `https` endpoints; the destination encrypts with its own keys. Objects encrypted with customer keys (SSE-C) cannot be read by the store and are marked `FAILED`.
- **Existing objects.** `cairn-store admin replication-resync <bucket>` (or `POST /-/admin/v1/buckets/<bucket>/replication/resync`) queues every current object that is not itself a replica, for example after adding a rule to a bucket that already has data. It is recorded in the audit trail.
- **Two-way sync.** Two stores can replicate the same bucket to each other (active-active): give each one the other as a target and a rule. A copy arrives marked as a replica (`x-amz-replication-status: REPLICA`) with its source's modification time, and is never replicated again, so nothing bounces back. When both sites change the same key, the newer version wins on both sides: a replica older than the version already there is acknowledged and dropped (ties go to the larger ETag), and a replicated delete gives way to a newer write. Writing replicas needs `s3:ReplicateObject` (or `s3:ReplicateDelete`) on the destination, and a replica timestamp more than five minutes in the future is refused. Keep the sites' clocks in sync (NTP); changes to tags and metadata alone are not replicated.
- **Not supported** (refused with `NotImplemented`, never accepted and ignored): the `ExistingObjectReplication` setting (use a resync), replication time control, replication metrics, destination encryption settings and source selection criteria.
- **Metrics.** `cairn_store_replication_delivered_total`, `_failed_total`, `_dropped_total`; `cairn_store_events_pending` counts the outbox, copies included.

**Static websites.** With `CAIRN_STORE_WEBSITE_DOMAIN` set (for example `sites.example.com`), a bucket with a website configuration (`PutBucketWebsite`) is served at `<bucket>.sites.example.com`. Configurations support index and error documents, redirect-all, routing rules that redirect by key prefix or error code, and per-object redirects (`x-amz-website-redirect-location`). Website requests are anonymous, so a site is served only if its bucket policy grants public read and public access is allowed: hosting never exposes a bucket by itself.

**Other S3 calls.** `GetObjectAttributes` (ETag, checksums, parts with their checksums, size). Request payment always answers `BucketOwner`, and setting `Requester` is refused. Transfer acceleration can only be `Suspended`. Intelligent tiering is refused with `NotImplemented` until that feature exists.

**Encryption at rest.** Give the store a key and it encrypts every new object (SSE-S3): set `CAIRN_STORE_MASTER_KEY` (a base64 32-byte key, `openssl rand -base64 32`), or `CAIRN_STORE_KEYRING_FILE` for several keys. Each object gets its own data key; its bytes are encrypted with AES-256-GCM in 64 KiB chunks, so ranged reads stay fast and any tampering is detected. The data key is stored only wrapped by a keyring key.

- **SSE-KMS.** Clients and bucket defaults can name a keyring key (`aws:kms` with a key id), for example one key per team.
- **SSE-C.** Customer-provided keys are supported; the store never stores them. They need TLS, or `CAIRN_STORE_SSE_C_ALLOW_HTTP=true` when TLS ends at a proxy in front of the store.
- **Bucket defaults.** `PutBucketEncryption` sets a bucket's default encryption.
- **Key rotation.** Add a key to the keyring and point `sse_s3_key` at it. Existing objects keep the key they were written with, so keep old keys until those objects are rewritten or deleted. An object whose key has been removed can no longer be read.
- **Back up the keyring.** Without it, encrypted objects cannot be recovered.
- **Customer-managed keys (HashiCorp Vault or OpenBao Transit).** A key the store never holds goes under `"external"` in the keyring file:

  ```json
  {"external": {"customer-a": {"type": "vault-transit", "addr": "https://vault:8200", "key": "cairn-a",
     "mount": "transit", "token_file": "/etc/cairn-store-keyring/vault-token", "cache_seconds": 60}}}
  ```

  It is used like any keyring key, by id: `aws:kms` with `customer-a`, a bucket default, or `sse_s3_key`. Each object's data key is encrypted by Vault when the object is written and decrypted by Vault when it is read. The object's name is bound in, so a wrapped key moved to another object does not open.
  - **The customer stays in control.** Revoke the store's token or policy and the objects can no longer be read or written. Restore it and they can. Delete the key in Vault and the data is gone for good (crypto-shredding). Changes take effect after `cache_seconds`: how long a decrypted data key stays in memory.
  - **Rotation.** Rotate the key in Vault; nothing is needed from the store. Old objects still open, and new ones use the latest version.
  - **The token.** Give the store a token whose policy allows only `update` on `transit/encrypt/<key>` and `transit/decrypt/<key>`. The token file is read on each call, so it can be renewed in place. In Helm, put `vault-token` in the same Secret as `keyring.json` (`cairn.store.secret.keyringSecret`). `namespace` sets Vault Enterprise's namespace.
  - **Not covered.** Access-key secrets stay wrapped by a local keyring key, since they are needed when the store starts.
  - **Fails closed.** A keyring whose external key does not load stops the store at start-up. When Vault refuses or cannot be reached, the request fails with `KMS.DisabledException`; nothing is written in the clear.
  - **Tested** with a real Vault (`store/conformance/test_kms_vault.py`): encryption, copies, rotation, revocation and restore, crypto-shredding, a token scoped to another key, and bad keyrings.
- **Customer-managed keys in AWS KMS** (or any service that speaks the KMS API). Use `"type": "aws-kms"`:

  ```json
  {"external": {"customer-b": {"type": "aws-kms", "region": "eu-west-1",
     "key": "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-...", "access_key": "AKIA...",
     "secret_key_file": "/etc/cairn-store-keyring/kms-secret", "cache_seconds": 60}}}
  ```

  - **How it works.** Each data key is encrypted with KMS `Encrypt` and decrypted with `Decrypt`, signed with SigV4. The object's name is bound in twice: in the plaintext, as with Vault, and as the encryption context (`cairn-object`). KMS checks the context on every decrypt, and CloudTrail records it. `key` is the key's id, ARN or alias.
  - **Credentials.** `access_key` with `secret_key_file` (and optionally `session_token_file`), each file read on every call. Without `access_key`, the store's environment is used (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`). Give them only `kms:Encrypt` and `kms:Decrypt` on the one key.
  - **Endpoint.** `addr` defaults to `https://kms.<region>.amazonaws.com`. Set it for a VPC endpoint or another KMS-compatible service.
  - **The customer stays in control.** Disabling the key, or removing the store's grant or permission, stops reads and writes after `cache_seconds`. Scheduling the key for deletion shreds the data.
  - **Tested** against a real KMS-compatible server (`nsmithuk/local-kms`, in `store/conformance/test_kms_aws.py`): encryption, copies, a key named by alias, a disabled and re-enabled key, a key pending deletion, a key that does not exist, and bad configurations. That server does not check signatures, so SigV4 is covered by the signer the store's other AWS clients share.
- **Customer-managed keys in Azure Key Vault** (or Managed HSM). Use `"type": "azure-keyvault"` with an RSA key that allows `wrapKey` and `unwrapKey`:

  ```json
  {"external": {"customer-c": {"type": "azure-keyvault", "addr": "https://acme.vault.azure.net", "key": "cairn-c",
     "auth": "workload-identity", "tenant_id": "<directory id>", "client_id": "<app id>",
     "federated_token_file": "/var/run/secrets/azure/tokens/azure-identity-token", "cache_seconds": 60}}}
  ```

  - **How it works.** Each data key, with the object's name bound in, is wrapped with RSA-OAEP-256 (`wrapkey`) and unwrapped with `unwrapkey`. The key version that wrapped it is kept with it, so old objects still open after a rotation. Without `version`, the store uses the key's current version, looked up again every `cache_seconds`; set `version` to pin one.
  - **Sign-in (`auth`).** `client-secret`: an app registration's secret in `client_secret_file`. `workload-identity`: the federated token AKS projects into the pod, in `federated_token_file`. `managed-identity`: the VM's or node's identity (`client_id` picks a user-assigned one). `token-file`: a ready access token, read on every call. Tokens are kept until a minute before they expire. `auth_addr` overrides the sign-in address (`https://login.microsoftonline.com`; the managed-identity endpoint for `managed-identity`), e.g. for a sovereign cloud.
  - **The customer stays in control.** Disabling the key version, or removing the store's role (`Key Vault Crypto Service Encryption User`), stops reads of the objects it wrapped after `cache_seconds`. Purging the key shreds the data.
- **Customer-managed keys in Google Cloud KMS.** Use `"type": "gcp-kms"` with a symmetric `ENCRYPT_DECRYPT` key:

  ```json
  {"external": {"customer-d": {"type": "gcp-kms", "key": "projects/acme/locations/europe-west1/keyRings/cairn/cryptoKeys/customer-d",
     "auth": "service-account", "credentials_file": "/etc/cairn-store-keyring/gcp-sa.json", "cache_seconds": 60}}}
  ```

  - **How it works.** Each data key is encrypted with Cloud KMS `encrypt` and decrypted with `decrypt`. The object's name is bound in the plaintext and as additional authenticated data (its SHA-256), which KMS checks on every decrypt. KMS picks the key version itself.
  - **Sign-in (`auth`).** `service-account`: a service account key file; the store signs its token request (RS256). `metadata`: the identity of the GKE workload or VM. `token-file`: a ready access token. Give the account only `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the one key. `addr` overrides `https://cloudkms.googleapis.com` (e.g. a private endpoint).
  - **The customer stays in control.** Disabling or destroying the key version, or removing the role, stops reads after `cache_seconds`.
- **Customer-managed keys in an HSM over KMIP.** Use `"type": "kmip"` for HSMs and key managers that speak KMIP 1.4 (Thales CipherTrust, Entrust KeyControl, Fortanix, IBM GKLM, PyKMIP and others), with an AES key that allows encrypt and decrypt:

  ```json
  {"external": {"customer-e": {"type": "kmip", "addr": "hsm.acme.internal:5696", "key": "<the key's unique id>",
     "ca_file": "/etc/cairn-store-keyring/kmip-ca.pem", "client_cert_file": "/etc/cairn-store-keyring/kmip.crt",
     "client_key_file": "/etc/cairn-store-keyring/kmip.key", "cache_seconds": 60}}}
  ```

  - **How it works.** The store connects with TLS and its client certificate (mutual TLS: the HSM decides by certificate what the store may use) and asks the HSM to encrypt each data key with AES-GCM, the object's name as the additional data. The key never leaves the HSM.
  - **The customer stays in control.** Revoking, deactivating or destroying the key, or removing the certificate's access, stops reads after `cache_seconds`.
- **Common to every customer-managed key.** The same fail-closed behaviour as Vault: a keyring that does not load stops the store; a refusal fails the request with `KMS.DisabledException` and logs the key manager's answer. Keep credential files in the keyring Secret. **Tested** against real servers (`store/conformance/test_kms_cloud.py`): Azure Key Vault on Lowkey Vault with each sign-in (client secret, workload identity, managed identity), rotation and a disabled version; Cloud KMS on floci-gcp with a signed service-account sign-in and a key the object was not written with; KMIP on PyKMIP's server, with a certificate the HSM refuses and a revoked key.

Objects written before a key was set stay unencrypted. Requests to set them are refused with `NotImplemented`; Cairn Store never accepts a setting it would ignore.

**Metadata in PostgreSQL.** By default the store keeps its index (buckets, objects, versions, uploads, settings, keys, events waiting to be sent and the audit trail) in an embedded database in the data directory. Set `CAIRN_STORE_META` to a PostgreSQL URL (`postgres://user:pass@host/db?sslmode=require`), or `CAIRN_STORE_META_FILE` to a file that holds one, to keep it in PostgreSQL instead. This suits teams that already run, back up and monitor PostgreSQL. In Helm, set `cairn.store.metadata.secretName`.

- **Same behaviour.** Every change is still one transaction, together with its events and audit entry, and writers are serialized as before. The full conformance suite runs on both backends.
- **Blobs stay on the data volume.** The data directory and its metadata database are bound together by a store id. A store refuses to start on another store's database, or on a database whose data directory has no store id. A data directory whose metadata is in PostgreSQL never opens with an empty embedded index, so `fsck` and `audit-verify` need `CAIRN_STORE_META` too.
- **Moving an existing store.** Stop it, then run `cairn-store migrate-meta` with `CAIRN_STORE_META` set to an empty database. Everything is copied in one transaction, and the embedded index is kept aside as `index.redb.migrated-<time>`.
- **Backups and TLS.** `cairn-store admin backup` still writes a data directory with an embedded index, which a store can start on, or which can be moved back with `migrate-meta`. Back up the PostgreSQL database with your usual tools as well. TLS to PostgreSQL uses the public roots plus `CAIRN_STORE_CA_FILE`.

**Small objects.** Objects up to `CAIRN_STORE_PACK_BELOW` bytes (64 KiB by default; `0` turns this off) are appended to shared pack files under `packs/` instead of getting a file each. Millions of small objects then become a few large files, and reading one is a seek. Pack files are closed at `CAIRN_STORE_PACK_FILE_BYTES` (1 GiB).

- **Same guarantees.** Packed bytes are written and synced before the index entry that refers to them, are encrypted like any other object, and work with ranges, versions, copies, replication, tiering and backups.
- **Deleting a small object** leaves dead space in its pack. Compaction runs every `CAIRN_STORE_COMPACT_INTERVAL` seconds (3600; `0` turns it off). It works on packs whose dead share is at least `CAIRN_STORE_COMPACT_DEAD_RATIO` (0.5): it copies their live objects into the current pack, switches each reference in one index transaction (only where the object is unchanged), and deletes the old pack once nothing refers to it. Packs that hold only deleted objects are removed. Compaction never runs during a backup.
- **On demand.** `cairn-store admin compact [--min-dead 0.3]` (or `POST /-/admin/v1/compact`) compacts now; it is recorded in the audit trail. `cairn-store admin stats` reports packs, their bytes and the live bytes in them.
- **Backups** copy pack files whole, and hard-link closed packs that are unchanged since the previous backup.

**Several disks.** A store can spread blob files over several disks, each with a type: `CAIRN_STORE_DISKS=/mnt/hdd2:hdd,/mnt/nvme1:ssd` adds disks to the data directory, whose own type is `CAIRN_STORE_DATA_DISK_TYPE` (`hdd`).

- **Placement.** New objects go to the disk with the most free space among those of the bucket's type. Buckets without one use the data directory's type. `cairn-store admin bucket-disk <bucket> --type ssd` pins a bucket (refused if the store has no disk of that type); without `--type` it goes back to the default. A pinned bucket whose disks are all gone gets 503 rather than writes on another type. Existing objects stay where they are.
- **Safe changes.** Each disk carries a marker naming its store and number, so the list can be reordered, and another store's disk is refused. The data directory remembers every disk it has used: if one is left out of the list, the store refuses to start rather than lose the blobs on it. The index, small-object packs and the audit trail stay in the data directory.
- **Clusters.** Copies and shards placed on other nodes go to a disk of the same type there; a node without one is skipped. Repair and rebalancing keep the type.
- **Seeing it.** `cairn-store admin disks` lists each disk with its free and total bytes, `/-/metrics` has `cairn_store_disk_free_bytes`, and backups and `fsck` cover every disk. In Helm, list them under `cairn.store.extraDisks` (`{name, type, size, storageClassName}`); each becomes a volume of its own per pod.

**Compression.** Set `CAIRN_STORE_COMPRESSION=zstd` to compress new object data at rest. CSV, JSON, logs and similar text often take a third or less of the space.

- **Ranged reads stay cheap.** Data is compressed in independent 1 MiB chunks, and each object records where its chunks start, so a read at any offset decompresses only the chunks it touches.
- **Only what pays.** An object whose first chunk does not compress by at least an eighth is stored as written (media, archives, Parquet), and already-compressed content types (images, video, audio, zip, gzip, Parquet, PDF) are not even tried.
- **Transparent.** Sizes, ETags, checksums, ranges, multipart, copies, versions, replication, tiering, clusters and backups all see the object, not the compressed bytes. With encryption at rest, data is compressed first and then encrypted.
- **Turning it off** affects only new writes: compressed objects stay readable. `cairn-store admin stats` reports the bytes written and stored since start. In Helm, set `cairn.store.compression: zstd`.

**Message queues.** Besides objects, the store keeps durable message topics under `/-/queues/v1/`, for small pipelines that need a queue without running a broker. Requests are signed with SigV4 like the admin API.

- **Topics.** Admin keys create them with `PUT /-/queues/v1/topics/<name>` (`{"retention_seconds": ..., "max_messages": ...}`), list them, and delete them with their messages. Creating, changing and deleting a topic is recorded in the audit trail.
- **Publishing.** `POST .../topics/<name>/messages` appends one message (the request body, up to 1 MiB, with its `content-type` and an optional `x-cairn-key`); `?batch` takes `{"messages": [{"value" or "value_base64", "key"}]}`, up to 1,000 at a time. A batch is written in one transaction, so an acknowledged publish survives a crash, and messages get gapless, increasing sequence numbers.
- **Consuming.** `GET .../messages?group=<g>&max=100` returns the messages after the group's committed offset, in order; `?wait=10` long-polls for up to 20 seconds. `POST .../groups/<g>` with `{"seq": n}` commits, so delivery is at least once: until a group commits, it gets the same messages again. Offsets never move back. `?after=<seq>` reads from a position without a group. `GET .../topics/<name>` shows each group's offset and lag.
- **Retention.** Messages older than `retention_seconds`, or beyond `max_messages`, are removed from the oldest, every `CAIRN_STORE_QUEUE_RETENTION_INTERVAL` seconds (60). A group behind the oldest kept message continues from there.
- **Access.** Other keys publish and consume on topics their bucket patterns allow (`lake-*` covers a `lake-feed` topic); read-only keys consume and commit only. Topics live in the store's metadata, so they work on PostgreSQL metadata and in clusters, and are part of backups. Metrics: `cairn_store_queue_published_total`, `_delivered_total`, `_trimmed_total`.
- **Partitions.** A topic has `partitions` (1 by default, up to 64; `PUT` with a higher number adds some, and they are never removed). Order, sequence numbers and group offsets are per partition. A message goes to:
  - `?partition=p`, when given;
  - otherwise, the partition its key hashes to (Kafka's murmur2, so the HTTP API and Kafka producers agree). A batch's keys must all hash to one partition;
  - otherwise, the next partition in turn.

  Reads and commits name a `partition` (default 0), and `GET .../topics/<name>` shows every partition's bounds and group lags.
- **The Kafka protocol.** With `CAIRN_STORE_KAFKA_ADDR` (e.g. `:9092`), Kafka clients use these topics: producers, consumers, consumer groups and the admin client. In Helm, set `cairn.store.kafka.enabled`.
  - Kafka offsets are sequence numbers minus one. A group's committed offsets are the queue group's offsets, so the HTTP API and Kafka consumers see the same progress.
  - **Sign-in.** SASL/PLAIN, with an access key as the user name and its secret as the password. Keys reach topics as above, and a wrong secret is refused. Nothing but the version and SASL requests is answered before sign-in.
  - **TLS.** The listener uses TLS (SASL_SSL) with the store's certificate. Without one, it starts only with `CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT=true`, since PLAIN sends the secret as it is.
  - **Advertised address.** `CAIRN_STORE_KAFKA_ADVERTISED` is the `host:port` clients reach. It defaults to the listen address, which must then name a host. Helm defaults it to the release's kafka Service.
  - **Requests.** ApiVersions, SASL, Metadata, Produce (record batches, uncompressed), Fetch (long polling), ListOffsets (earliest, latest, by time), CreateTopics (admin keys; replication factor 1, since the store keeps its own copies), FindCoordinator, OffsetCommit/Fetch, and JoinGroup, SyncGroup, Heartbeat and LeaveGroup. Partitions are assigned by the clients' own assignors.
  - **Not offered.** Compressed batches (refused with `UNSUPPORTED_COMPRESSION_TYPE`, so set `compression.type=none`), transactions and idempotent producers (set `enable.idempotence=false` on Java clients).
  - **Clusters.** The node a group's members connect to keeps the group's membership, so point all members of a group at one node or address.
- **When to use a Kafka cluster instead.** For very high throughput, compression, transactions or exactly-once processing, use Cairn's Kafka, which the store's event notifications can feed. These queues favour simplicity and transactional durability.

**Durability.** A write is acknowledged only once its bytes and its index entry are on disk. It is readable immediately afterwards. Conditional writes are atomic. After a crash the store keeps every acknowledged write and drops any half-finished ones when it starts. `cairn-store fsck -data /data` checks a stopped store and reports objects whose data is missing.

**Backups.** With `CAIRN_STORE_BACKUP_DIR` set, `cairn-store admin backup <name>` (or `POST /-/admin/v1/backups`) writes an online backup while the store keeps serving.

- **Consistent.** A backup holds the index as of one moment and every blob it refers to. Writers are not paused; deleted blobs are kept until the backup has copied them.
- **Incremental.** Blobs already in the newest earlier backup are hard-linked, so repeated backups take little extra space. The backup directory can be on its own volume; copy backups elsewhere with a tool that preserves hard links (`rsync -H`).
- **Restoring.** A backup is a complete data directory: check it with `cairn-store fsck -data <backup>`, and restore by starting the store on a copy of it. Event deliveries that were pending are not part of a backup, so they are not sent again. Backups are recorded in the audit trail.
- **Helm.** Set `cairn.store.backups.enabled` for a second volume.

**Backups to S3-compatible storage, Azure and GCS.** Backups can also go to a bucket elsewhere: another Cairn Store, AWS S3, MinIO, an Azure container or a GCS bucket (see the remote bucket fields above). Define sinks in `CAIRN_STORE_BACKUP_SINKS_FILE`: `[{"id": "offsite", "endpoint": "https://s3.eu-west-1.amazonaws.com", "bucket": "acme-store-backups", "prefix": "prod", "access_key": "...", "secret_key_file": "/etc/...", "region": "eu-west-1"}]`.

- **Writing one.** `cairn-store admin backup <name> --sink offsite` (or `POST /-/admin/v1/backups` with `"sink"`) takes the same consistent snapshot as a local backup while the store keeps serving, then uploads the index and every blob it refers to.
- **Incremental.** Blobs are immutable, so each one is uploaded once and shared by every backup in the sink; a later backup uploads only what is new. Packs of small objects are shared once they are closed.
- **Encrypted at rest stays encrypted.** Blobs are uploaded exactly as stored, so the sink holds ciphertext for encrypted objects. Keep the keyring separately: a restored store needs it.
- **Listing and deleting.** `cairn-store admin backups --sink offsite` lists complete backups (a backup counts only once its manifest is written, last). `backup-delete <name> --sink offsite` removes it, then the blobs no remaining backup refers to. Use one sink prefix per store.
- **On a schedule.** `CAIRN_STORE_BACKUP_SINK_SCHEDULE=offsite` writes a backup every `CAIRN_STORE_BACKUP_SINK_INTERVAL` seconds (86400), named `auto-<time>`, and keeps the newest `CAIRN_STORE_BACKUP_SINK_KEEP` (7). A failed run is logged and retried at the next interval.
- **Continuously, with point-in-time restore.** With `CAIRN_STORE_BACKUP_CONTINUOUS=offsite`, every object write and delete (from any interface, lifecycle and replication included) is recorded in the same transaction as the change, then shipped to the sink's journal (`journal/<sequence>.json`, after the blobs it names), at least once and in order, retried while the sink is down. Each backup records how far in the journal it goes. `cairn-store restore-backup offsite latest /new/data --until 2026-11-01T09:30:00Z` restores the newest backup made before that time and replays the journal up to it; without `--until`, up to the last change shipped. The journal covers objects (their data, metadata, tags and encryption); bucket settings come from the backup. Replayed changes become the current versions, so the history of old versions between backups is not rebuilt. Deleting a backup also prunes the journal from before the oldest one left. In Helm, set `cairn.store.backupSinks.continuous`.
- **Restoring.** `cairn-store restore-backup offsite <name> /new/data` downloads a backup into a new, empty data directory, checking every blob's length. It has an embedded index: start a store on it, or move its metadata into PostgreSQL with `migrate-meta`.
- **Audit and Helm.** Backups and deletions are recorded in the audit trail. In Helm, put `sinks.json` in a Secret and set `cairn.store.backupSinks.secretName`, and optionally `schedule`, `intervalSeconds` and `keep`. Sinks are not available in a cluster.

**Clusters.** Several store nodes can share one PostgreSQL metadata database and keep every object's bytes on more than one node's disk. Each node serves the whole S3 API (and the file API, WebDAV and SFTP), so a load balancer can send any request to any node, and a write on one node is readable on every node at once.

- **Turning it on.** On each node, set `CAIRN_STORE_CLUSTER_NODE` (its name: lowercase letters, digits and `-`), `CAIRN_STORE_CLUSTER_URL` (its address for the other nodes, such as `http://store-0.store:8333`), the same `CAIRN_STORE_META`, and the same `CAIRN_STORE_CLUSTER_SECRET` (16 characters or more, or `_FILE`). Give every node the same keyring, credentials and tier, replication and event settings. A data directory remembers its node name, and a node refuses to start under another name, without PostgreSQL metadata, or with a setting a cluster cannot honour (`CAIRN_STORE_PACK_BELOW`, `CAIRN_STORE_BACKUP_DIR`).
- **Copies.** Each blob is written to `CAIRN_STORE_CLUSTER_COPIES` (2) distinct nodes, the receiving node's own disk first, before the index entry that refers to it is committed. If fewer nodes are up than the write needs, it fails with `503 ServiceUnavailable` and nothing becomes visible: the store never keeps fewer copies than configured.
- **Racks and zones.** Give each node `CAIRN_STORE_CLUSTER_ZONE` (a data center or availability zone) and `CAIRN_STORE_CLUSTER_RACK` (a rack or host). Placement always spreads copies across zones first, then racks, then nodes. Set `CAIRN_STORE_CLUSTER_SPREAD=rack` or `zone` to make that a rule: copies then never share a rack (or zone), erasure-coded blobs keep at most `m` shards in one, so losing a whole rack or zone loses no data, and a write that cannot meet the rule fails with 503. A node that does not say where it is refuses to start under such a rule. When the rule is added to a running cluster, repair moves the copies that share a domain once there is somewhere better, and rebalancing never breaks it. `cairn-store admin cluster` shows each node's zone and rack. In Helm, each node's rack is its Kubernetes node and `cluster.zone` names the release's zone; set `cluster.spread: rack` once the store runs on at least as many Kubernetes nodes as copies.
- **Erasure coding.** With `CAIRN_STORE_CLUSTER_EC=k+m` (for example `4+2`), blobs of at least `CAIRN_STORE_CLUSTER_EC_ABOVE` bytes (1 MiB) are split into k data shards and m Reed-Solomon parity shards on k+m distinct nodes. Any k shards rebuild the data, so `4+2` survives two lost nodes for 1.5 times the space, where three copies would take three times. Smaller blobs are copied.
- **Cold data to erasure coding.** Hot data can stay as copies (fast to read and repair) while older data takes less space: with `CAIRN_STORE_CLUSTER_EC_AFTER=<seconds>`, the node running the shared work re-encodes replicated blobs of at least `CAIRN_STORE_CLUSTER_EC_CONVERT_ABOVE` bytes (1 MiB) whose object is that old into k+m shards, on its repair cycle. Set `CAIRN_STORE_CLUSTER_EC_ABOVE` high to write everything as copies first. `cairn-store admin cluster-encode [--older-than S] [--limit N]` runs a conversion now (audited). Shards follow the spread rule and the blob's disk type; each blob's id is switched in one transaction, only if the object is unchanged, and the old copies are deleted after. With 2 copies and 4+2 shards, a cold blob goes from twice its size to 1.5 times.
- **Reads.** A read takes this node's copy when it has one, otherwise a live node's, and moves to the next copy if one fails, even partway through. Erasure-coded reads take the data shards and rebuild from parity when some are missing. Ranged reads fetch only the stripes they need.
- **Failure and repair.** Nodes record a heartbeat every `CAIRN_STORE_CLUSTER_HEARTBEAT` seconds (5). A node silent for `CAIRN_STORE_CLUSTER_DEAD_AFTER` seconds (60) is dead. Every `CAIRN_STORE_CLUSTER_REPAIR_INTERVAL` seconds (300), repair re-creates the copies and shards that were on dead, draining or removed nodes, switches each blob's placement in one index transaction (only if the object is unchanged), and deletes the copies it replaced. A node that comes back rejoins by itself.
- **One node runs the shared work.** Event and replication delivery, lifecycle, deleting collected blobs and repair run on one node at a time, under a lease in the database. If that node dies, another takes over within the dead-after time. Access keys created on one node reach the others within a heartbeat.
- **Draining, removing, rebalancing.** `cairn-store admin cluster` lists the nodes (state, liveness, copies and bytes held) and the health of every blob (healthy, degraded, lost). `cluster-drain <node>` stops new data going to a node, and the next repair moves its copies off; `cluster-repair` runs a repair now. `cluster-remove <node>` takes a node out for good: its copies count as lost and are re-created, it stops if still running, and it refuses to start again until `cluster-activate <node>`. `cluster-rebalance [--limit N]` moves copies from the fullest nodes to the emptiest, for example after adding nodes. The same calls are under `/-/admin/v1/cluster`, and each one is recorded in the audit trail.
- **Joining an existing store.** Start a store that already keeps its metadata in PostgreSQL as a cluster node. Its blobs, small packed ones included, are placed on the cluster in the background, and are readable from every node once placed.
- **Between nodes.** Nodes talk over `/-/cluster/v1/`, and each call carries an HMAC of the request under the cluster secret. S3 keys do not open it.
  - **TLS by default.** The MAC covers the request, not the bytes, so node URLs must be `https://` (the node serves TLS with `CAIRN_STORE_TLS_CERT`, and `CAIRN_STORE_CA_FILE` trusts a private CA). A node with an `http://` URL refuses to start unless `CAIRN_STORE_CLUSTER_ALLOW_HTTP=true`, for networks you trust.
  - **Mutual TLS.** `CAIRN_STORE_CLUSTER_CA_FILE` names the CA that signs node certificates. Each node then presents its certificate on every call (`CAIRN_STORE_CLUSTER_TLS_CERT` and `_KEY`, by default its server certificate, which then needs the `clientAuth` usage too) and trusts only that CA for the other nodes.
  - **What mutual TLS refuses.** The node-to-node API refuses a connection without such a certificate, even with a valid signature. A certificate from another CA fails the handshake. S3 clients on the same port need no certificate.
  - **Renewals.** A renewed node certificate is picked up without a restart.
  - **In Helm,** a cluster needs `cairn.store.tlsSecret`, or `cluster.allowInsecureHttp=true`. `cluster.mutualTls` uses the `ca.crt` of that Secret, as cert-manager writes it. The certificate must name every node: `*.<release>-cairn-store-nodes.<namespace>.svc`.
  - **Tested:**
    - two nodes over mutual TLS copied a blob, and the second served it once the first was killed;
    - a signed call without a node certificate got `403`, and one with a certificate from another CA was refused in the handshake;
    - nodes with `http://` URLs, or with a cluster CA but no TLS, refused to start.
- **Backups and limits.** Online backups, local or to a sink, cover one disk, so they are off in a cluster: back up the PostgreSQL database, and rely on copies across nodes (and replication to another site) for the bytes. Usage counters are kept in PostgreSQL with every write and are not recounted at start-up. In Helm, set `cairn.store.cluster.enabled` with `replicas`, `copies` and optionally `erasureCoding`; the chart runs a StatefulSet with a volume per node.
- **Monitoring.** `/-/metrics` adds `cairn_store_cluster_nodes` and counters for copies placed, remote reads, failovers, reconstructions, repairs, moves and deletes.

**Limits.** A single store runs on one node: the volume, its backups and any replicated buckets are the only copies. Keep backups on other storage, or take volume snapshots, or run a cluster. Objects can be up to 5 GiB in one request, and up to 10,000 parts of 5 GiB each with multipart uploads.

**Quotas and the admin API.** Cairn's admin API for the store lives under `/-/admin/v1/`. Requests are signed with SigV4 by an admin key, so any AWS SDK signer works; bodies and responses are JSON.

- **Statistics:** `GET stats`.
- **Buckets:** `GET buckets` and `GET buckets/<name>` report each bucket's usage (stored bytes, including old versions and unfinished uploads, and the number of object versions) and its settings.
- **Quotas:** `PUT buckets/<name>/quota` with `{"max_bytes": ..., "max_objects": ...}` sets a quota, and `DELETE` removes it.

A write that would take a bucket over its quota is refused with `QuotaExceeded`, inside the same transaction, so concurrent uploads cannot slip past the limit. Deletes and overwrites that shrink a bucket always work. Usage is updated with every write, and recounted from what is stored each time a single store starts.

**Access keys while the store runs.** Admin keys can create, change, rotate and delete access keys without a restart: in **Settings → Storage → Access keys**, through `cairn-store admin keys | key <name> | key-create | key-update | key-rotate | key-delete`, or through `/-/admin/v1/keys`.

- **Secrets.** The store generates every secret, a 256-bit random value, and shows it once, when the key is created or rotated. It is never listed again. With encryption at rest configured (`CAIRN_STORE_MASTER_KEY` or a keyring), stored secrets are wrapped with AES-256-GCM. If the keyring key that wrapped them is later removed, the store refuses to start rather than silently dropping those keys.
- **Scope.** A key can be limited to buckets (`lake-*`), made read-only or admin, or given its own IAM-style policy. It can also carry a Cairn identity (`actor`) and SSH public keys for SFTP. Policies and SSH keys are checked when they are saved.
- **Effect.** Every change applies to the next request, on S3, the file API, WebDAV, SFTP and the admin API. A rotated secret stops working at once. A deleted key is refused at once.
- **Audit.** Each change is written to the audit trail in the same transaction (`key.create`, `key.update`, `key.rotate`, `key.delete`), never with the secret. Changes made from Cairn are also recorded in Cairn's own audit log.
- **Rules.** Keys from the environment and `CAIRN_STORE_CREDENTIALS_FILE` are listed, but are changed only in that configuration. A key cannot delete itself. Stored keys are included in backups.

**IAM-compatible API.** The store answers the AWS IAM Query API, so `aws iam`, the AWS SDKs and Terraform's aws provider (pointed at the store's endpoint) can manage users, access keys and policies. Requests are `POST /` signed with SigV4 for the `iam` service; `aws sts get-caller-identity` (service `sts`) is also answered. The account id in ARNs is `000000000000`.

- **Users and access keys.** Supported actions:
  - `CreateUser`, `GetUser`, `ListUsers`, `DeleteUser`;
  - `CreateAccessKey`, `ListAccessKeys`, `UpdateAccessKey` (`Active` or `Inactive`), `DeleteAccessKey`.

  A user has at most two keys. An inactive key is refused like an unknown one. As in IAM, a user is deleted only once its keys, policies and group memberships are gone. IAM users' keys are listed in `/-/admin/v1/keys` with their `user` and `status`. They can be rotated or deleted there, but their permissions are changed only through IAM (`KeyManagedByIam`).
- **Policies.**
  - Inline: `PutUserPolicy` and `PutGroupPolicy`, with their `Get`, `List` and `Delete` counterparts.
  - Managed: `CreatePolicy`, `GetPolicy`, `ListPolicies`, `DeletePolicy`. Versions: `CreatePolicyVersion`, `GetPolicyVersion`, `ListPolicyVersions`, `SetDefaultPolicyVersion`, `DeletePolicyVersion` (five at most).
  - Attachments: `Attach` and `Detach` for users and groups, and `ListAttachedUserPolicies` / `ListAttachedGroupPolicies`.
  - Groups: `CreateGroup`, `GetGroup`, `ListGroups`, `ListGroupsForUser`, `AddUserToGroup`, `RemoveUserFromGroup`, `DeleteGroup`.

  Policy documents are checked like key policies: `s3:`, `iam:` and `sts:` actions, and `arn:aws:s3:::` or `arn:aws:iam::000000000000:` resources. A malformed document is refused with `MalformedPolicyDocument`. Other IAM actions (tags, roles, login profiles) answer `InvalidAction`.
- **Evaluation.** A user's keys carry its effective policy: its inline and attached policies, plus those of its groups. As in IAM, an explicit Deny wins, and otherwise something must Allow. A user with no policy may do nothing. Bucket policies can name a user as a principal (`"AWS": "arn:aws:iam::000000000000:user/alice"`). `aws:username` is the user's name.
- **Who may call it.** Admin keys may make any call. Other keys may call an action only where their policy allows `iam:<Action>` on the entity's ARN (`arn:aws:iam::000000000000:user/<path><name>`, `group/`, `policy/`). So a team lead can, for example, manage only the users under `/contractors/`.
- **Consistency.** Users, groups and policies are kept in the index. Each change is one transaction, together with the keys it creates or deletes and its audit entry (`iam.<Action>`). That holds on PostgreSQL metadata across a cluster too, and backups include IAM.

**Temporary credentials (STS).** Set `CAIRN_STORE_STS_SECRET` (32+ characters, the same on every cluster node; or `_FILE`; in Helm, `cairn.store.secret.stsSecretKey`) and the store answers AWS STS. Tools get short-lived, narrowly scoped keys instead of long-lived ones; any AWS SDK uses them as it uses AWS's.
- **Roles.** IAM roles (`aws iam create-role`, `put-role-policy`, `attach-role-policy`, `get-role`, `list-roles`, `update-assume-role-policy`, `delete-role`) have a trust policy, which says who may assume the role, and permission policies, which say what its sessions may do. A trust policy names only `sts:` actions. `MaxSessionDuration` runs from 3600 to 43200 seconds.
- **`AssumeRole`.** A session of a role whose trust policy names the caller, an IAM user by its ARN. If it trusts the whole account (`arn:aws:iam::000000000000:root`) or everyone, the caller's own policy must also allow `sts:AssumeRole` on the role. A `Policy` (2048 characters at most) narrows the session further: the session may only do what both the role and that policy allow. Cairn Catalog uses this to hand engines credentials for one table.
- **`GetSessionToken`.** A temporary key of the caller's own key, with that key's limits; a read-only key stays read-only. A session of an admin key is not an admin key.
- **`AssumeRoleWithWebIdentity`.** A signed-in person's OIDC token (RS256 JWT) buys a role's session; the request is unsigned, and the token is the credential. `CAIRN_STORE_STS_PROVIDERS` lists the issuers the store trusts: `[{"issuer": "cairn", "audiences": ["cairn-store"], "jwks_url": "…/v1/trino/jwks.json"}]`, or `jwks_file`. The trust policy names the issuer as `Federated` and can require `"<issuer>:sub"` and `"<issuer>:aud"` with `StringEquals` or `StringLike`. The session carries the person's Cairn identity (`urn:li:corpuser:<sub>`), so Cairn's policy decision point sees the person, not the role.
- **How a temporary key works.**
  - Its id starts with `ASIA`, and its secret is derived from the id with the STS secret, so neither is stored.
  - Its session token is the session's claims (role, expiry, session policy, identity), signed with the STS secret. Every node can check it, and an edited or swapped token is refused (`InvalidToken`).
  - Deleting a role ends its sessions at once. A session's policies are the role's current ones, so detaching one takes effect immediately.
  - Temporary keys cannot manage IAM, assume further roles or sign browser POST forms. Presigned URLs (SigV4 and SigV2) carry the token.
  - Every issue is audited (`sts.AssumeRole`, `sts.GetSessionToken`, `sts.AssumeRoleWithWebIdentity`) with the temporary key's id.
- **Off by default.** Without `CAIRN_STORE_STS_SECRET`, STS actions are refused. In Compose a development secret is set; change it.
- **Tested** with boto3's STS client: role and session-policy limits; edited, swapped and missing tokens; deleted roles; trust by name and by account; read-only and admin key sessions; web identity with good, foreign-signed, expired, wrong-audience and untrusted tokens; presigned URLs.

**Storage in the Cairn UI.** Cairn admins see **Settings → Storage**, which lists:

- every bucket, with its usage, its quota, and whether it has versioning, object lock, default encryption, lifecycle rules or a bucket policy;
- quotas, which can be set and removed from the page;
- the store's audit trail, with a button that verifies the hash chain from the beginning.

The control plane calls the store's admin API with the store's admin key (`CAIRN_STORE_URL`, `CAIRN_STORE_ADMIN_ACCESS_KEY`, `CAIRN_STORE_ADMIN_SECRET_KEY`; set automatically in Compose and Helm), and records quota changes in Cairn's own audit log too. Users never see the key.

**On the command line.** `cairn-store admin stats | buckets | bucket <name> | quota <name> --max-bytes N --max-objects N | quota-remove <name> | audit [--after SEQ] [--jsonl]` calls the same API. It is signed with `CAIRN_STORE_ACCESS_KEY` and `CAIRN_STORE_SECRET_KEY` (an admin key), against `CAIRN_STORE_ENDPOINT` or the local listener.

**Benchmarking.** `cairn-store bench` measures an S3 endpoint: this store, or any S3-compatible one, with the endpoint and key as for `cairn-store admin`.
- **Options:** `--objects 1000 --size 64KiB --concurrency 16 --ops put,get,list,delete`, with `--bucket` (default `cairn-bench`) and `--keep`.
- **What it does.** It writes the objects under a fresh prefix and reads them back, checking every byte count and ETag. It then lists and deletes them. It never touches other keys in the bucket.
- **The report** is JSON: for each phase, operations, errors, seconds, operations and MiB per second, and latency (mean, p50, p90, p99, max). Watch the Cairn Store dashboard while it runs.
- **Clean-up and exit codes.** A bucket the run created is removed again (unless `--keep`). The command exits 1 if any request failed.

**Monitoring.** `/-/healthy`, `/-/ready` and Prometheus metrics at `/-/metrics`: requests by operation and status, bytes in and out, buckets, objects, stored bytes, open uploads and noncurrent versions, per-bucket bytes and objects (`cairn_store_bucket_bytes`, `cairn_store_bucket_objects`), and lifecycle actions.

## Cairn Catalog

Cairn Catalog is Cairn's Iceberg REST catalog. Engines that speak the Iceberg REST protocol connect to it: Trino, Spark, Flink, pyiceberg and DuckDB. Table data and metadata files live in Cairn Store (or any S3 store); the catalog keeps track of each table's current metadata.

- **Docker Compose:** service `cairn-catalog`, at `http://cairn-catalog:8181` inside the stack and `http://localhost:8181` from the host. Set `CAIRN_CATALOG_TOKEN` in `.env`. The catalog writes to the store with the store's key, and new tables go under `s3://lakehouse/warehouse`.
- **Kubernetes:** set `cairn.catalog.enabled=true` (with `cairn.store.enabled=true`, or point `cairn.catalog.s3Endpoint` at another store). Create the Secret named in `cairn.catalog.secret`. Engines in other namespaces need an entry in `cairn.networkPolicy.catalogIngressFrom`.

**Connecting an engine.** Use the catalog URL and the token as a bearer token. For example, in pyiceberg: `RestCatalog("cairn", uri="http://cairn-catalog:8181", token="...")`. With each table the catalog also gives the engine the store's endpoint and keys, so engines need no storage settings of their own. Set `CAIRN_CATALOG_CLIENT_S3_ENDPOINT` when engines reach the store at another address.

**Which keys engines get** (`CAIRN_CATALOG_VEND_CREDENTIALS`):
- **`scoped`** (the default when `CAIRN_CATALOG_VEND_ROLE` is set; Compose turns it on). Each table comes with temporary keys from the store's STS that reach that table's location only. With a read-only catalog token they only read; otherwise they read, write and delete there.
  - **What engines hold.** No store key, so an engine cannot touch another table's files or anything else in the store. Trino's lakehouse catalog then has no S3 keys at all (`iceberg.rest-catalog.vended-credentials-enabled=true`), and pyiceberg needs only the catalog token.
  - **The role.** `CAIRN_CATALOG_VEND_ROLE` names a store role. When it is missing, the catalog creates it at start-up, trusted only by the catalog's key and allowed the warehouse bucket; each session's policy narrows it to one table. An ARN is used as given.
  - **Lifetime.** Keys last `CAIRN_CATALOG_VEND_SECONDS` (3600; 900 to 43200) and are reused while at least a quarter of that is left.
  - **Fails closed.** The store must have STS on (`CAIRN_STORE_STS_SECRET`). The catalog checks at start-up and refuses to start otherwise, rather than falling back to its own key.
  - **In Helm,** set `cairn.catalog.vendRole`; the chart refuses it when this release's store has no `cairn.store.secret.stsSecretKey`.
- **`static`** (the default otherwise): the catalog's own store key, which reaches the whole store.
- **`off`**: engines bring their own storage keys.

**Tested:**
- **pyiceberg** wrote and read with vended keys only.
- **A table's keys** listed and read their own table, were refused another table's files and the rest of the bucket, and were reused across loads.
- **A read-only token's keys** could not write or delete.
- **Start-up** was refused without STS.
- **Trino 476** with no store key ran `CREATE TABLE AS`, `INSERT`, `UPDATE`, `DELETE` and `optimize`.
- **Compose:** the catalog created its role, and Trino wrote and read the lakehouse with no key in its environment.

**Tokens.** `CAIRN_CATALOG_TOKEN` is an admin token. `CAIRN_CATALOG_TOKENS_FILE` adds more tokens as a JSON list. Each token can be limited to namespaces (`"namespaces": ["sales", "sales.*"]`) or to reading (`"read_only": true`).

**What it supports.**

- Namespaces, nested, with properties.
- Tables: create (including staged creates for create-table-as-select), load, commit, rename, drop, and register an existing metadata file.
- Multi-table transactions, and metrics reports (accepted and dropped).

Server-side scan planning and remote request signing are not supported yet.

**Commits.** Table metadata is checked and updated by iceberg-rust, the Apache Iceberg project's Rust library, so the catalog applies the table spec the same way the reference implementations do.

1. A commit writes a new metadata file.
2. The catalog then switches the table to that file, but only if the table has not changed since the commit was prepared.
3. A writer that lost the race gets `409 CommitFailedException`. Engines refresh and retry by themselves.

A multi-table transaction changes every table or none.

**Views.** The catalog serves Iceberg views (the REST spec's views API: list, create, load, replace, rename, drop), so `CREATE VIEW`, `CREATE OR REPLACE VIEW`, `ALTER VIEW … RENAME`, `COMMENT ON VIEW` and `DROP VIEW` work in Trino, and pyiceberg lists them.
- **How they are kept.** A view's metadata file is written under its location, and every replace adds a version, checked with `assert-view-uuid`. A view and a table cannot share a name.
- **Who may change them.** Read-only tokens read views but change none; namespace-scoped tokens see only their namespaces.
- **Materialized views.** Trino has none on REST catalogs (`createMaterializedView is not supported`), so Cairn keeps them; see [Materialized views](#materialized-views).
- **Tested:** the REST API (create, conflict, load, replace, stale replace, list, scope, drop), and Trino 476 (create, query, replace, comment, rename, name clashes both ways, read-only token, drop).

**Where its state lives.** By default it is embedded, on one volume (`/data`), and the catalog runs as a single replica. Back that volume up together with Cairn Store.

**Several replicas (high availability).** Set `CAIRN_CATALOG_DATABASE_URL` (or `_FILE`) to a PostgreSQL URL. The catalog then keeps namespaces, tables, views and staged creates there, and any number of replicas serve the same lakehouse.
- **Commits stay safe.** Every change is one transaction. A writer lock in PostgreSQL makes replicas take turns, so a commit's compare-and-set cannot interleave with another replica's, and a multi-table transaction still changes every table or none.
- **TLS.** Connections use TLS unless the URL says `sslmode=disable`. `CAIRN_CATALOG_DATABASE_CA_FILE` adds a CA.
- **One database per lakehouse.** The first catalog to connect records its warehouse in the database. A catalog with a different warehouse is refused at start-up.
- **Scoped credentials.** Replicas starting at the same time all try to create the same store role. The first one wins; the others use the role it created.
- **In Helm,** create a Secret with the URL and set `cairn.catalog.database.existingSecret` (key `urlKey`, default `url`) and `cairn.catalog.replicas`. The catalog then runs as a Deployment with no volume. It rolls out one pod at a time and has a PodDisruptionBudget; `cairn.ha.spreadReplicas` spreads pods over nodes and zones. The chart refuses `replicas` above 1 without a database.
- **Back up** the PostgreSQL database together with Cairn Store.
- **Tested.** CI runs the whole conformance suite twice: once on the embedded state, once on PostgreSQL. The PostgreSQL run adds three replicas sharing one database:
  - a table written through one replica was read, appended and renamed through another;
  - a commit prepared on stale metadata was refused by the other replica, and pyiceberg retried it on top;
  - twelve racing creates of one table, spread over the replicas, left exactly one winner;
  - nine concurrent appends lost none;
  - the lakehouse kept working after a replica was killed.

  The replicas start with scoped credentials on, so they also race to create the store role.

**Sharing tables outside the organisation.** A share gives a recipient, such as a partner or an auditor, read access to named tables until it expires or is revoked. The recipient reads with any Iceberg REST client (pyiceberg, Spark, Trino, Snowflake, DuckDB) from Cairn Catalog. Shares are listed under **Pipelines → Shares**.

- **Making a share.** `POST /v1/shares` with `{"name": "partner-a", "recipient": "Partner A", "tables": ["analytics.products"], "expires_days": 30}`. Add `"region"` (where the recipient keeps what it reads) to share tables pinned to a region (see "Data residency"). You need MANAGE on every table you share. The answer holds the recipient's credential and connection settings, and is the only time the credential is shown. Only a hash of it is kept.
- **What the recipient sees.** Only the shared tables and their namespaces, read-only. Their storage keys are the catalog's scoped keys for one table at a time, read-only. A share never gets the store's own key, and the catalog refuses shares unless `CAIRN_CATALOG_VEND_CREDENTIALS=scoped`.
- **Masks and row filters.** A table to which a column mask or row filter applies is never shared, because its files hold what they hide. This is checked when the share is made and again on every request.
- **Revoking.** `POST /v1/shares/{id}/revoke`, or **Revoke** in the UI, stops the share at once. The catalog asks Cairn about the recipient on every request, without caching, so the next request and the next token renewal are refused. Storage keys already handed out last at most the catalog's credential lifetime (`CAIRN_CATALOG_VEND_SECONDS`). Expiry works the same way.
- **Tokens.** The recipient's client gets its token with the credential (OAuth 2.0 client credentials) at `POST /v1/shares/oauth/tokens`. Each token lasts `CAIRN_SHARES_TOKEN_TTL` seconds (default 900).
- **Settings.** `CAIRN_SHARES_CATALOG_URL` and `CAIRN_SHARES_TOKEN_URL` are the catalog and token endpoint as recipients reach them; both must be public. In Helm, set them under `cairn.catalog.identity.shares`. `CAIRN_SHARES_MAX_DAYS` (default 365) is the longest a share may last.
- **Who sees shares.** People see the shares they made. Operators (MANAGE_SYSTEM_OPERATIONS) see and revoke them all.
- **Delta Sharing.** Every share is also served through the open Delta Sharing protocol, for recipients whose tools speak it (the `delta-sharing` client for pandas and Spark, Power BI, Tableau, Databricks).
  - **The profile.** The answer to making a share holds `delta_sharing`: a profile file (`shareCredentialsVersion`, `endpoint`, `bearerToken`, `expirationTime`) to save as the recipient's `.share` file. The bearer token is the share's credential (`<share id>.<secret>`), so it is shown once and ends with the share.
  - **The endpoint.** `/v1/delta-sharing` (`GET /shares`, `/shares/<share>/schemas`, `…/tables`, `…/all-tables`, `…/tables/<table>/version`, `…/metadata`, `POST …/query`). The share's name is the share; a table's namespace is its schema.
  - **The same rules.** Only the shared tables are listed and read, and only while they may still be shared: a table a column mask or row filter applies to, or one pinned to a region the recipient's is not allowed, is not listed and its query is refused (404). A revoked or expired share is refused at once (403); a wrong token too (401).
  - **The files.** A query loads the table from Cairn Catalog as the share itself (the catalog asks Cairn, as for the Iceberg REST recipient) and answers with the current snapshot's Parquet files as pre-signed Cairn Store URLs, signed with the read-only keys the catalog vended for that table. They last `CAIRN_SHARES_DELTA_URL_SECONDS` (default 900), never past the share's end or the keys' own expiry: after a revocation, URLs already handed out stop within that time. The answer uses Delta Sharing's Parquet format: reader version 1, the schema as Delta's `schemaString`, each file's row count in `stats`, the snapshot's sequence number as the table version.
  - **Refused tables.** A table with row-level deletes (its data files still hold the deleted rows), one whose columns were renamed after files were written (the files carry the old names), and columns Delta has no type for (`time`, `uuid`, `fixed`) are refused (400). Iceberg timestamps with and without a zone are both announced as Delta `timestamp`; the Parquet files keep their own type. Time travel and the change data feed are not offered (400).
  - **Settings.** `CAIRN_SHARES_DELTA_SHARING_URL` is the endpoint as recipients reach it (default: the request's own address); `CAIRN_SHARES_CATALOG_INTERNAL_URL` is where Cairn reaches Cairn Catalog (default `CAIRN_SHARES_CATALOG_URL`); `CAIRN_SHARES_STORE_URL` is the store endpoint recipients fetch files from, signed into the URLs (default: the endpoint the catalog vends), so it must be public.
  - **Audit and usage.** Each query is audited (`share.delta_query`, with the table, version and number of files) and counted in the share's usage, refusals and wrong tokens too.
  - **Tested** with the real `delta-sharing` client (1.4.3) in its own environment, reading a shared table from Cairn Store and Cairn Catalog (`tests/test_delta_sharing_e2e.py`): only the shared tables listed, the rows as written, scoped short-lived URLs of the table's Parquet files, a masked table and a renamed column refused, wrong tokens refused, and the client refused at once after revocation.
  - **Snowflake.** Publishing to Snowflake as a listing is not built: it needs a Snowflake account to test against.
- **Audit.** Creating and revoking a share, and every token a recipient gets, are audited (category `shares`).
- **Usage.** Cairn counts what each share's recipient does, per day and table, from the catalog's questions about
  it and the token endpoint: requests, table loads (a question about one table: loading its metadata, snapshot
  and files), refusals (a table in a shared namespace that was not shared, or a revoked or expired share), failures
  (a wrong secret, a decision that could not be made) and tokens, with the last access. Tables outside the share's
  namespaces are refused by the catalog without asking, so they count as requests only. Counts are written every
  few seconds and kept `CAIRN_SHARES_USAGE_RETENTION_DAYS` (default 90).
  - `GET /v1/shares/<name or id>/usage?days=30`: totals, the tables read, each table's loads, refusals and last
    access, and each day's counts. For the share's maker and operators.
  - `GET /v1/shares/usage/overview?unused_days=30&expiring_days=14`: every share the caller sees, with `unused`
    (active, no access in `unused_days`), `expiring_soon`, and `quality_failing`: recipients reading tables
    whose quality status (the quality module's latest evaluations, as on trust badges) is failing.
  - **Notices.** The scheduler tells the share's maker once when a share is about to expire (`share.expiring`) and
    once per unused spell (`share.unused`), through their notification channels. `CAIRN_SHARES_EXPIRING_DAYS`
    (14), `CAIRN_SHARES_UNUSED_DAYS` (30), checked every `CAIRN_SHARES_NOTICE_SECONDS` (3600).
  - **Tested** (`tests/test_share_usage_e2e.py`): pyiceberg as the recipient against the real store and catalog
    listed, loaded and read a table, was refused one beside it and sent a wrong secret; the counts, tables read and
    last access came back by name and by id (not for another person); a share unused for 40 days and ending in 2
    days was found and its maker notified once of each; a table read through a share with a failing quality
    evaluation was reported; older days were dropped.

**Data clean rooms.** Two or more parties join their tables in aggregate, and none of them sees the others' rows (`/v1/clean-rooms`).

- **A room** names its `parties` (Cairn users or groups) and `min_group` (k, default 10: the smallest group a result may describe). Each party brings tables it may read itself (`POST /v1/clean-rooms/<room>/tables`).
- **Templates.** Any party proposes a query (`PUT .../templates/<name>`, SQL over the room's tables with `:params`), and it runs only once every party has approved its current text. Changing the text clears the approvals. Cairn checks each one when it is proposed: it reads only the room's tables, it is an aggregate (no rows, no `SELECT *`), and its parameters are declared.
- **Small groups never leave.** At run time (`POST .../templates/<name>/run`), arguments are bound as typed literals, and Cairn adds `HAVING count(*) >= k` itself, whatever the template says.
- **The room's own identity.** Queries run on Trino as `cleanroom-<room>`. Cairn's access control lets it read the contributed tables, without masks or row filters (the parties agreed to bring them), and nothing else. Nobody can sign in under that name, whatever their identity provider calls them. Proposals, approvals and runs are audited with who asked.
- **Outside parties.** A party can be an organisation outside Cairn, written `partner:<name>` (lowercase letters, digits and `-`). A room needs at least one Cairn party, and is made by one.
  - **Its secret.** When the room is made, the answer holds each partner's secret (`partner_secrets`) this once. Pass it on to that partner. Cairn keeps only its hash.
  - **Signing in.** The partner uses HTTP Basic, with its name without `partner:` as the username and the secret as the password, at `/v1/clean-rooms/<room>/partner`. A wrong or missing secret, or another room, gets 401, the same answer for each.
  - **What it can do.** Read the room (`GET`), propose templates (`PUT .../partner/templates/<name>`), approve them and run them, under the same rules as every party.
  - **Its data.** It has no lakehouse tables, so it uploads a file: `PUT .../partner/data/<table>`, with `Content-Type: text/csv` or `application/vnd.apache.parquet`, up to 100 MB.
    - Cairn writes it to `<catalog>.cleanrooms.<room>__<partner>__<table>`, through the catalog pipes write to (`CAIRN_PIPES_CATALOG`; without one, uploads are refused). `<catalog>` is `CAIRN_CLEANROOM_CATALOG`, Trino's name for the lakehouse.
    - The table joins the room, and only the room's identity reads it. The other parties can't read it directly.
    - An upload replaces that table's earlier contents, columns included. Column names are lowercase letters, digits and `_`.
    - A table name that another party or another room already holds is refused (409). Names joined with `__` can coincide, and no upload writes over another room's data.
  - **Uploads are for partners.** Cairn parties bring their tables with `POST .../tables`. Every partner action is audited with `partner:<name>` as the actor.
- **Differential privacy.** A room created with `"privacy": {"epsilon": 1, "budget": 10}` releases only noisy answers.
  - **What a template may return.** Group keys, `count(...)` and `sum(column)`, and each summed column needs `bounds` in the template (`"bounds": {"b.spend": [0, 100]}`). Other aggregates, such as `max` and `avg`, are refused when the template is proposed. Compute an average from a noisy sum and a noisy count. So are `HAVING`, `ORDER BY`, `LIMIT` and aggregates in subqueries, CTEs or `WHERE`: they act on the true answers (which groups appear, in what order), so sort and filter the noisy result instead.
  - **Each run** clips the summed values to their bounds and takes `epsilon` from the budget. The epsilon is split between the result's aggregates, and each gets Laplace noise scaled to its sensitivity: 1 for a count, the larger bound for a sum.
    - Counts come back as whole numbers, never below 0, and sums rounded to cents.
    - The answer states the noise scales, the epsilon spent and the epsilon left.
  - **The budget.** It is taken before the query runs, so a failed run costs too. Two runs can't both take the last of it. Once it is spent, the room answers nothing more (409), and neither the budget nor epsilon can be changed after the room is made.
  - **Limits.** The sensitivities assume each person is at most one row of the joined result, so join on a key unique per person. Which groups appear is not itself noised, so use public group keys (regions, campaigns), not values that identify someone. The k floor still applies, to the true counts.
  - **Choosing epsilon.** Smaller is more private and noisier. Between 0.1 and 1 per run is usual, with a total budget that bounds what all the room's answers together can reveal.
- **Tested** on Trino (`tests/test_trino_e2e.py`). A retailer and an advertiser joined buyers to clicks. The template ran only after both approved, and the one-person APAC group was never released. Row templates, `SELECT *`, another table and a non-party were refused, and so was a changed template until it was approved again. Neither party could read the other's table, and the room's identity was refused a table outside the room. A differentially private room (epsilon 1 a run, budget 3) answered three times with fresh noise near the true clipped values, then refused the fourth run. A `max`, an `avg` and an unbounded `sum` were refused when proposed, and the noise generator was checked to be Laplace at the stated scale. An outside partner signed in with its secret, uploaded a CSV that the room joined to a Cairn party's table (written to the real store and catalog, unreadable by the Cairn party), proposed a template and ran it once the Cairn party approved. A missing or wrong secret, another partner name and another room were refused, and a second upload replaced the first.


The recipient connects like this:

```python
from pyiceberg.catalog.rest import RestCatalog
cat = RestCatalog("partner", uri="https://lake.example.com", credential="<the credential>", scope="catalog",
                  **{"oauth2-server-uri": "https://cairn.example.com/v1/shares/oauth/tokens"})
cat.load_table("analytics.products").scan().to_arrow()
```

Tested in `tests/test_shares_e2e.py`, with the real store and catalog and pyiceberg as the recipient.

**AI data packages: a share with its meaning.** A recipient's agents need more than tables: what the tables and
columns mean, the governed metrics over them and the questions they answer. A share's data package carries that,
signed by Cairn.

- **Publishing.** `PUT /v1/shares/<share>/package` with `{"metrics": ["avg_price"], "sample_questions": ["What is our
  average list price?"], "notes": "Prices are refreshed nightly."}` (the share's maker or an operator) publishes a new
  version. Cairn writes the manifest from the catalog: each shared table's description and columns (type,
  description, glossary terms), the glossary terms' names and definitions, the metrics named (their measure,
  filters, dimensions, SQL expression and certification), the sample questions, the notes, the agents attached and
  the catalog's address. Only what is shared is described: a metric over a table the share does not include is
  refused (409). The manifest is signed as a compact JWS (RS256, `typ` `cairn-data-package+jws`) with the key Cairn
  publishes at `/v1/trino/jwks.json`. Every version is kept (`GET /v1/shares/<share>/package` lists them);
  `GET /v1/share-packages` lists the caller's shares with their latest version and agents (**Sharing → Data
  packages**). Audited: `share.package_published`.
- **Fetching.** The recipient calls `GET /v1/shares/recipient/package` (`?version=` for an older one) with the
  share's OAuth token (`Authorization: Bearer`, from `/v1/shares/oauth/tokens`) or the credential itself (HTTP Basic,
  the id and secret around the `:`). The answer holds the `manifest`, its `signature` and `digest`, and `jwks_uri`.
  The recipient checks the signature against the JWKS (any JOSE library, or RS256 by hand) before trusting it.
  Nothing else signs in there: a person's token, or a lakehouse token that is not the share's, is refused (401).
  Audited: `share.package_fetched`.

**Agents on shared data.** The provider can let the recipient ask one of its deployed agent apps
(`/v1/agent-apps`) about the shared tables, without handing over the tables' meaning to someone else's model or the
data to anything but the share.

- **Attaching.** `POST /v1/shares/<share>/agents` with `{"app": "partner-helper"}`, by the share's maker or an
  operator, who must be allowed to use the app; it needs the `model` scope. `DELETE /v1/shares/<share>/agents/<app>`
  detaches it. Audited: `share.agent_attached`, `share.agent_detached`.
- **Asking.** The recipient lists the agents at `GET /v1/shares/recipient/agents` and asks one with
  `POST /v1/shares/recipient/agents/<app>/invocations` (`{"input", "session_id"}`), with the share's credential.
  - The run's actor is the recipient, `urn:li:corpuser:share:<share id>`, with the identity type `share-recipient`.
    Its Cairn run token is narrowed to the `model` scope: it reaches the AI gateway and nothing else in Cairn.
  - Its model calls are allowed (or refused) as for the provider who attached the agent, who pays for them, and are
    metered to the recipient (`gateway:<agent id>`); the provider's budgets and the gateway's limits apply.
  - Its data comes only through a 15-minute lakehouse token for the share's own identity, passed to the agent's
    runtime with the run. In the agent, `ctx.catalog()` is a pyiceberg catalog with it: Cairn Catalog asks Cairn about
    each request, so the agent reads the shared tables, read-only, and is refused every other table.
  - Each invocation is recorded per recipient (`GET /v1/shares/<share>/agents?days=30` gives each agent's runs,
    failures, sessions, time and last use) and audited as `share.agent_invoked` (no content). Sessions are kept per
    recipient as for people.
- **Revoking** the share, or its expiry, stops both at once: the package and the agents are refused (403), and the
  lakehouse token an agent holds is refused by the catalog on its next request.

Tested end to end with the real store and catalog, the agent's real code in its own process and the model behind
the AI gateway (`tests/test_share_packages_e2e.py`): the recipient fetched the package with the share's token and
with its credential and verified Cairn's signature against the JWKS (a tampered manifest failed); the manifest held
the table, its columns, the glossary term, the metric and the agent, and a metric over an unshared table was
refused; the agent read the shared table (three rows, summed up by the model) and was refused `sales.orders` and
`hr.salaries`; the model calls were metered to the recipient, every run token was the recipient's, `model` only, and
revoked; such a token was refused `/data/query`; revoking the share refused the package, the agent and the
lakehouse token.

**Zero-copy clones.** `POST /v1/namespaces/{ns}/tables/{table}/clone` with `{"name": "dev_orders"}` (and optionally `"namespace": ["dev"]`) makes a new table on the same data files. Nothing is copied. The clone has its own id and location, so its new writes go to its own location, and the two tables change independently. The caller needs read access to the source and manage access to the target namespace.

- Neither table may expire a snapshot they share, because expiring it would delete data files the other still reads. Such a commit is refused with 409 ("shared with a clone"). Once the clone is dropped, the source may expire those snapshots again.
- The catalog records shared snapshots in `cairn.*` table properties. Clients may not set or remove `cairn.*` properties, on tables or on namespaces (403).

**Undrop.** A table dropped without `purge` can be brought back for `CAIRN_CATALOG_UNDROP_DAYS` days (default 7):

- `GET /v1/namespaces/{ns}/dropped` lists the tables that can be brought back.
- `POST /v1/namespaces/{ns}/tables/{table}/undrop` brings one back with its history.

A purged table cannot be brought back. Undrop fails with 409 if a new table now has the name. A dropped clone stops holding its source's snapshots, so the source may expire them. The clone can then no longer be brought back, because its data files may be gone (409).

**Branches and tags.** The catalog keeps Iceberg branches and tags, so you can work in write-audit-publish order: write to a branch, check it, then publish it to `main`. Engines create and use them:

- pyiceberg: `manage_snapshots().create_branch(…)`, `append(…, branch="audit")` and `set_current_snapshot(…)`.
- Spark: `ALTER TABLE … CREATE BRANCH` and `spark.wap.branch`.
- Trino reads a tag or branch with `FOR VERSION AS OF 'release-1'`.

Clones, undrop and write-audit-publish with pyiceberg are tested in `catalog/conformance/test_clones.py`.

**Limits.** Dropping a table with `purge` removes its current metadata file. Data files are left in the store.

**Trino.** Trino is the SQL engine for the lakehouse, through its Iceberg connector. It runs on Cairn Catalog's tables, and their data stays in Cairn Store.
- **Running it.** In Compose, `docker compose -f docker-compose.cairn.yml --profile trino up -d` starts `cairn-trino` with HTTPS on `localhost:8443` (`CAIRN_MAPPED_TRINO_PORT`). In Helm, set `cairn.trino.enabled` (it needs `cairn.catalog.enabled` and `cairn.api.enabled`).
  - **Sizing:** `queryMaxMemory` and `resources`.
  - **Workers:** `workers: N` adds N worker pods. The coordinator then plans queries and leaves the work to them. Access control and lineage stay on the coordinator, and the nodes share a secret (the access key) for their internal traffic.
  - **Resource groups:** `cairn.trino.resourceGroups` (Trino's resource-groups JSON; `{}` turns it off). Compose uses the same groups, from `deploy/trino/resource-groups.json`. The defaults:
    - `global.cairn` for Cairn's own queries: the SQL editor, Ask-your-data and the catalog crawler (source `cairn`). 10 at a time, 30% of memory.
    - `global.pipelines` for sources named airflow, dbt, etl, dagster or prefect. 20 at a time.
    - `global.adhoc.<user>` for everyone else, each person in their own group. 5 at a time each, 20% of memory.

    A group's excess queries wait in its queue rather than starving the others.
  - **A group per team.** Give each team its own group under a common parent, with a selector naming its members, so one team's flood waits in its own queue (or is refused once that queue is full) while other teams' queries start at once:

    ```yaml
    resourceGroups:
      rootGroups:
        - name: global
          softMemoryLimit: 90%
          hardConcurrencyLimit: 100
          maxQueued: 1000
          subGroups:
            - name: teams
              softMemoryLimit: 80%
              hardConcurrencyLimit: 20
              maxQueued: 200
              schedulingPolicy: fair
              subGroups:
                - {name: team-a, softMemoryLimit: 40%, hardConcurrencyLimit: 2, maxQueued: 4}
                - {name: team-b, softMemoryLimit: 40%, hardConcurrencyLimit: 2, maxQueued: 4}
      selectors:
        - {user: "(ann|al)", group: global.teams.team-a}
        - {user: "(bea|bo)", group: global.teams.team-b}
        - {group: "global.adhoc.${USER}"}
    ```

    Select by `user` (Trino's user is the person Cairn signed in), not by `source` or client tags, which a client sets as it likes. With a Trino group provider, `userGroup` selects a team as a whole. Each query's queued time is in its profile and in the team cost report below.
  - **Tested under load.** `tests/test_workload_e2e.py` runs real Trino with such groups, Cairn deciding every action and receiving every finished query. Team A sends ten heavy queries at once: Trino runs two, queues four and refuses the rest (`QUERY_QUEUE_FULL`); meanwhile team B's queries start at once (under 2 seconds queued) and finish. Then each team's compute and storage appear in the per-day team cost report with nothing posted by hand.
  - **Autoscaling:** `cairn.trino.autoscaling` (`enabled`, `minWorkers`, `maxWorkers`, `targetCPUUtilizationPercentage`) adds a HorizontalPodAutoscaler on the workers; set `workers` to at least `minWorkers`. It scales in one worker at a time.
  - **Draining.** A removed worker drains first. Its preStop hook puts it in Trino's `SHUTTING_DOWN` state: it takes no new work, finishes what it runs, then exits. `gracefulShutdownSeconds` (120) sets the wait, and the pod's termination grace period allows for it. Workers check no queries themselves, since the coordinator asks Cairn, so their only rule lets the hook's user, `cairn-drain`, drain them. That user is just a name in a header, but the network policy limits who can reach a worker, and the most it can do is drain one.
  - **Disruption budget:** a PodDisruptionBudget lets drains and upgrades take one worker at a time.
  - **Fault-tolerant execution:** `cairn.trino.faultTolerance.enabled` sets Trino's `retry-policy` (`TASK`, the default, or `QUERY`). If a worker dies mid-query, its tasks run again on another worker and the query still succeeds. Trino stages intermediate data in a Cairn Store bucket for this:
    - Create the bucket (`exchangeBucket`, default `trino-exchange`).
    - Give Trino a key that reaches only that bucket, in the Secret named by `exchangeKeySecret`. Default name `cairn-trino-exchange`, keys `access-key` and `secret-key`.
    - The chart refuses fault tolerance without workers.
    - Retries cost staging I/O, so use it for long batch work (pipelines), not quick interactive queries.
  - **Spill:** `cairn.trino.spill.enabled` lets joins, aggregations and sorts that outgrow memory spill to an emptyDir on each node (`sizeLimit`, 25Gi), up to `maxUsedSpacePerNode` (20GB).
  - **File cache:** `cairn.trino.cache.enabled` keeps lakehouse data and metadata files on an emptyDir on each node (`maxSize` 10GB, `sizeLimit` 12Gi), so repeated reads skip the store. The cache is per pod and starts empty after a restart. Since Trino 484 the cache is a server-level cache manager: the chart writes `cache-manager.properties` (`cache-manager.name=alluxio`, the directory and size), points `config.properties` at it, and the catalog keeps only `fs.cache.enabled=true`.
  - **The coordinator.** Trino runs one coordinator per cluster and has no coordinator failover: a query running when it dies fails and must be resubmitted. Everything else lives outside it: tables in Cairn Catalog and Cairn Store, sign-in and policies in Cairn. So a replacement coordinator serves the same lakehouse as soon as it starts, and workers rejoin it on their own. For no single coordinator at all, run several Trino clusters behind Trino Gateway; Cairn does not ship that.
  - **Tested as rendered.** `tests/test_trino_helm.py` renders the chart with TLS and a worker, then runs the coordinator, the worker and the init containers in Docker exactly as the pods would. It uses the rendered files, environment and commands, a `kubernetes.io/tls`-style key and certificate, and a real Cairn API, catalog and store. It checks sign-in over TLS, masks, row filters, a query spread over the worker, lineage and usage. It also checks the resource groups each query lands in. It runs the worker's preStop hook during a long query: the query finished, then the worker left. Finally it replaces the coordinator: a new worker joined it, and the tables, masks and sign-in all still worked. A second render turns on fault tolerance, spill and the cache with two workers. While a query is running, it kills a worker that holds some of the query's tasks: the query still returns the right answer, Trino reports the failed tasks it retried, and intermediate data went through the exchange bucket. Repeated reads fill the cache directory. CI runs it, the Trino end-to-end tests and the chart renders with Trino on. CI fails if any Trino test is skipped.
  - **Network policy:** with `cairn.networkPolicy.enabled`, only this release's pods reach Trino, plus the clients listed in `cairn.networkPolicy.trinoIngressFrom` (BI tools, notebooks).
- **Sign-in.** Trino believes only tokens Cairn signs. The Trino user is whoever Cairn says it is, not a name a client claims.
  - **Getting a token.** A signed-in user calls `POST /v1/trino/token` (optional `{"ttl_seconds": 600}`, 60 seconds to 12 hours; default `CAIRN_TRINO_TOKEN_TTL`, 3600). The answer has the `token` and the Trino `user`. Use it from the Trino CLI (`trino --server https://… --access-token <token> --user <user>`), JDBC (`accessToken`) or Python (`Authorization: Bearer`). Every token issued is audited (`trino.token_issued`). Service and agent credentials get no token.
  - **How Trino checks it.** Trino's JWT authenticator reads Cairn's public key from `/v1/trino/jwks.json`, which needs no sign-in, and requires the issuer (`CAIRN_TRINO_TOKEN_ISSUER`, default `cairn`) and the audience `trino`. A missing, edited, expired or foreign-signed token gets 401. Ana's token with `--user admin` is an impersonation: Trino asks Cairn, and Cairn refuses it without `MANAGE_SYSTEM_OPERATIONS`.
  - **The signing key.** It is made on first use and kept in Cairn's database (`cairn_trino_signing_keys`), so every API replica signs with the same key. It is encrypted with `CAIRN_TRINO_ACCESS_KEY`: the database alone cannot sign a token. Changing the access key therefore means deleting that row, which makes a new key and invalidates outstanding tokens. Trino reads the key when it starts, so Compose starts Trino once the API is healthy, and in Helm an init container waits for it.
  - **Single sign-on (web UI and BI tools).** Cairn is the OAuth2 server Trino signs people in with. Trino's web UI sends a person to Cairn's `/cairn/api/v1/trino/oauth/authorize`. A person already signed in to Cairn goes straight back with a one-time code; anyone else signs in to Cairn first. Trino exchanges the code (with its client secret) at `/v1/trino/oauth/token` for a Cairn-signed token.
    - **Clients.** The same flow serves clients' external authentication: the CLI's `--external-authentication`, JDBC `externalAuthentication=true`, and Python's `OAuth2Authentication`. The client opens the sign-in page in the browser, then picks up its token. BI tools need no copied token, and people sign in again when it expires.
    - **Safeguards.** There is no consent page, because Trino is Cairn's own engine. Codes are single-use, last 2 minutes, are kept hashed, and are bound to the redirect address and to a PKCE challenge when the client sends one. Cairn only sends people back to `CAIRN_TRINO_OAUTH_REDIRECT_URIS` (Trino's `https://<host>/oauth2/callback`, HTTPS only) and refuses any other address outright. The crawler identity cannot sign in, and every sign-in is audited (`trino.sign_in`).
    - **Settings.** In Compose this is on (`CAIRN_TRINO_OAUTH_SECRET`; Trino at `https://localhost:8443`). In Helm, set `cairn.trino.sso.enabled` and `sso.publicUrl` (Trino's HTTPS address); the client secret is `sso.clientSecretKey` in the access-key Secret, and TLS is required.
    - **Tested** with Trino 476 over TLS: the web UI signed in through Cairn; a person without a Cairn session was sent to Cairn's sign-in; a client without a token was told where to sign in, picked up a Cairn token after the browser sign-in, and was `ana` to Trino and Cairn's policies. Also tested: one-use codes, the client secret, the redirect address and PKCE.
  - **TLS.** Tokens travel only over HTTPS; plain HTTP stays for Trino's own nodes, which share a secret. In Compose, `cairn-trino-tls` makes a self-signed certificate once, into the `trinotls` volume: `keystore.p12` for Trino, `ca.pem` for clients. Mount your own with `CAIRN_TRINO_TLS_DIR`. In Helm, set `cairn.trino.tls.secretName` to a `kubernetes.io/tls` Secret; Trino serves it on `cairn.trino.tls.httpsPort` (8443). The chart refuses to render without it unless `cairn.trino.allowInsecureHttp` is true, which is for tests only because tokens then cross the network unencrypted.
  - **Tested** with Trino 476 over TLS: no token, a token signed with another key, an edited token and a claimed impersonation all refused; plain HTTP refused; the CLI and the SQL editor signed in with Cairn tokens. In Compose: 401 without a token, and a query with a token, the certificate verified against `ca.pem`, both from the host and from the API container.
- **The `lakehouse` catalog.** Both setups configure it from `deploy/trino/catalog/lakehouse.properties`:
  - the catalog's REST URI with its token as an OAuth2 bearer token (`iceberg.rest-catalog.security=OAUTH2`);
  - the store through Trino's native S3 support, with path-style access.

  Secrets come from the environment (`${ENV:...}`), never from the file itself. To give another Trino a scoped view, configure a second catalog with a namespace-scoped or read-only token. The catalog then enforces the scope for every statement.
- **What works.** The conformance suite runs Trino 476 against the store and catalog binaries:
  - creating schemas and partitioned tables, and `INSERT`, `UPDATE`, `DELETE` and `MERGE`;
  - time travel (`FOR VERSION AS OF`), the `$snapshots` and `$partitions` tables, `ALTER TABLE ... ADD COLUMN` and `EXECUTE optimize`;
  - `CREATE TABLE AS`;
  - tables shared both ways with pyiceberg;
  - a read-only token refused every change.
- **Access control through Cairn.** Trino asks Cairn's policy engine before every action, through Trino's OPA access-control plugin (`deploy/trino/access-control.properties`). Compose and Helm turn this on whenever Trino runs.
  - **Users.** The Trino user `alice` is `urn:li:corpuser:alice`; Trino groups are `urn:li:corpGroup:<group>`.
  - **Datasets.** A table `catalog.schema.table` is `urn:li:dataset:(urn:li:dataPlatform:trino,<catalog>.<schema>.<table>,PROD)`, the URN Cairn's Trino source gives it. `CAIRN_TRINO_CATALOGS` maps a catalog elsewhere, for example `{"lakehouse": {"platform": "iceberg", "database": ""}}`.
  - **Privileges.**
    - Seeing a table (listings, `SHOW COLUMNS`) needs `VIEW_ENTITY_PAGE`.
    - Reading rows needs `VIEW_DATASET_PROFILE`, as in the SQL editor.
    - Changing rows (`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`) needs `WRITE_DATASET`.
    - Creating, altering and dropping tables, views and schemas, and table procedures such as `optimize`, need `MANAGE_DATASET`.
    - Other users' queries and system operations need `MANAGE_SYSTEM_OPERATIONS`.
    - Operations Cairn does not know are refused.
    - **Iceberg metadata tables** (`<table>$<name>`) are decided on their table. `$snapshots`, `$history` and `$refs` hold no row values: whoever may read the table may read them. The others (`$files`, `$manifests`, `$partitions` and the rest) carry column bounds that can show masked values, so they need `MANAGE_DATASET` on the table. None takes masks or row filters.
    - **Connector table functions** (`<catalog>.system.<name>`, such as Iceberg's and Delta's `table_changes` or a JDBC catalog's `query`) return rows past masks and row filters, so only operators (`MANAGE_SYSTEM_OPERATIONS`) may call them. Value-free ones (`bucket`) stay open. A table's changes are read through [the change feed](#change-data-feed) instead.
    - **Who has them.** `WRITE_DATASET`, `MANAGE_DATASET` and `MANAGE_SYSTEM_OPERATIONS` are Cairn's own privileges, and Cairn Core policies never grant them. Trino has no Core fallback, so a request no Cairn policy allows is refused. The baseline bundle grants all three to the group `urn:li:corpGroup:lakehouse-admins` (policy `lakehouse-admins-manage-tables`). Add your lakehouse administrators to it (SCIM, or Core's group page), or write your own policy. Tested with a real Cairn Core: a user put in the group through SCIM created schemas and tables through Trino, while the Core admin, who is not in the group, was refused.
  - **Masks and filters.** Columns the caller's masking plan covers come back masked in SQL, so filters and joins see the masked values too. Every mask keeps the column's type:
    - **Text:** `partial` (`j•••@•••.com`), `hash` (a stable digest) or `redact` (`••••`).
    - **Numbers:** `partial` keeps one significant figure, cut toward zero, so 98,765.43 becomes 90,000.00 and never overflows the type. `hash` gives a stable number of the same type, so joins still match. `redact` gives 0.
    - **Dates and timestamps:** `partial` keeps the month, `hash` gives a stable date and `redact` gives 1970-01-01. Times keep the hour.
    - **Locations (`geo`):** for people who may know roughly where, not exactly where. Coordinates (double, real, decimal) are rounded to `CAIRN_GEO_MASK_DECIMALS` places: 2, about 1 km, by default; 1 is about 11 km. A shape, whether WKT text, `Geometry` or `SphericalGeography`, becomes its centre point, rounded the same way. Values that are not locations get the `partial` mask, so a `geo` policy can cover a whole PII label. Trino's spatial functions (`ST_*`, `great_circle_distance`, Bing tiles) run on the coarse values: maps, distances and areas still work. `geo` is the weakest mask, since it shows more than `partial` (51.5074 is `51.51` under `geo` and `5•••••4` under `partial`): masks are chosen per column, and on a column that another policy also masks, the stronger mask wins (a broad `PII` partial policy overrides `geo` on location columns, while other columns of the table keep their own mask). The PII classifier labels location columns `Classification.PII.Location`, by name (`lat`, `lng`, `geom`, `wkt`, `coordinates` …) and by value (WKT shapes, `lat, lon` pairs in range). Tested on Trino in `tests/test_trino_e2e.py` (coarse coordinates and centres, spatial SQL on them, the admin unmasked, every type kept), and in `tests/test_geo_mask.py`.
    - **Anything else:** booleans, binary, JSON, arrays, maps and rows become NULL.
    - **Tested** on Trino 476 for every strategy on 18 column types, including `char(n)`, `varchar(n)`, `decimal(p,s)`, `real`, `timestamp(p) with time zone` and `time`: the type is kept and NULL stays NULL.
   - **Row filters.** `row_filter` obligations (`{"expression": "region = 'EU'"}`, with `{user}` for the Trino user) become Trino row filters.
  - **The key.** Trino presents `CAIRN_TRINO_ACCESS_KEY` (at least 24 characters) in the request path. Requests without it get a 404. In Helm, put it in the Secret named by `cairn.trino.accessKeySecret`; the API and Trino both read it.
  - **Tested** with Trino 476 against a real Cairn API: hidden tables refused and left out of `SHOW TABLES`, labelled emails masked (`j•••@•••.com`, so `WHERE email = ...` finds nothing), row filters applied, and writes, drops and new schemas refused without the privilege.
- **Lineage from Trino.** Trino's OpenLineage event listener (`deploy/trino/event-listener.properties`, on in Compose and Helm) sends every query that writes to Cairn: `INSERT`, `CREATE TABLE AS`, `MERGE`, `UPDATE` and `DELETE`.
  - Each query becomes a run of a job named after the tables it writes (`trino:<catalog>.<schema>.<table>`), so repeated loads into a table share one job and its run history.
  - The job records the tables the query read and wrote, column-level lineage (which source columns feed which output columns), and the SQL.
  - Datasets carry the same URNs as access control, so lineage, policies and masking refer to the same datasets.
  - Tested with Trino 476: a `CREATE TABLE AS` and an `INSERT` recorded as two runs of one job, with `by_region.region` traced to `orders.region`.
- **Lineage on the asset page.** The Lineage tab draws the lineage around an asset: sources on the left, what uses it on the right, one to three hops (Trino's lineage included). Each asset shows its quality status and opens on selection.
  - **Paths.** Edges follow Cairn Core's lineage paths, so a two-hop source joins the asset between rather than the asset itself.
  - **Hidden assets.** Assets the viewer may not see are left out and counted. A path through one is joined around it with a dashed line, so the hidden asset is never named.
  - **The API.** `GET /embed/v1/graph?urn=…&depth=1..3` serves the same graph for the signed-in user (the lineage embed serves it for embed tokens).
  - **Tested** against a real Cairn Core: a Trino table's graph ran from the source table through the Trino job to the table.
- **Who reads what (usage).** Trino's HTTP event listener (`deploy/trino/usage-listener.properties`, on in Compose and Helm next to the lineage listener) sends every finished query to Cairn: who ran it, and the tables and columns it read. OpenLineage events do not name the user, so lineage alone cannot say who read a table.
  - **What Cairn records.** Each dataset's usage statistics (`datasetUsageStatistics`, one bucket per day): the number of queries, the distinct users, each user's count and how often each column was read. The usage tab, popularity and the AI steward's evidence read them.
  - **What counts as a read.** Only finished queries count. A failed or refused query, `EXPLAIN` and `DESCRIBE` do not, and neither do reads of Trino's own metadata (`system`, `information_schema`). Each query counts once, even when Trino resends it. Reads by `INSERT ... SELECT` count for the tables read; the write is lineage.
  - **Where counts live.** Cairn's database keeps them (`cairn_trino_usage`, 35 days), so a day's bucket adds up every API replica. A bucket Core cannot take now waits and is written by the retry loop, so a Core outage loses no usage.
  - **Tested** with Trino 476: ana's and bob's reads counted with their columns, and ana's refused read of `hr.salaries` not counted. Also tested against a real Cairn Core, including a query run while Core was stopped (counted once Core was back).
- **Trino's tables in the catalog.** Cairn reads each Trino connection with `run_as_user` into its catalog, so the SQL editor and Ask-your-data can query those tables. (They serve only catalogued tables.)
  - **How it reads.** Cairn reads `information_schema` and the table comments as its own crawler, `cairn-catalog`, signed in with a Cairn token. Cairn's access control lets the crawler see every table and column and read no rows. `/v1/trino/token` never issues a token for it.
  - **What it writes.** Every table becomes a dataset: description, properties (`trino_catalog`, `trino_schema`, `trino_table`), schema with Trino's types and column comments, and subtype (`Table` or `View`). Datasets use the URNs that access control, lineage and usage use (`CAIRN_TRINO_CATALOGS`). Give the connection's `catalog` the same platform and database.
  - **Dropped tables.** Tables gone since the last sync are marked removed, and the SQL editor then refuses them as not in the catalog.
  - **When it runs.** Every `CAIRN_TRINO_CATALOG_SYNC_SECONDS` (3600; 0 turns it off), with each run claimed so one API replica does it. Run it now with `POST /v1/trino/catalog-sync {"connection": "lakehouse"}`, which needs `MANAGE_INGESTION`. Each run is audited (`trino.catalog_synced`).
  - **The Trino connector in Ingestion.** It still works for a Trino Cairn does not sign in to; it needs the executor to install the `trino` plugin.
  - **Tested** with Trino 476: typed columns, comments and views synced; the crawler refused `SELECT` on a hidden table; the SQL editor queried a synced table; a dropped table marked removed. Also tested against a real Cairn Core in Compose.
- **The SQL editor and Ask-your-data on Trino.** Add a Trino connection with `run_as_user: true` (see `config/connections/connections.example.yml`):

  ```yaml
  lakehouse:
    url: trino://cairn@cairn-trino:8443/lakehouse?protocol=https&verify=/etc/cairn/trino-tls/ca.pem
    run_as_user: true
    catalog: {platform: trino, database: lakehouse}
  ```

  - **How a query runs.** Cairn checks the query as it does for every connection: catalogued tables only, the caller must be allowed to read each one, and results are masked. Trino then runs it as the signed-in user, so Trino's own checks (Cairn's policies, row filters included) apply as well. Row filters thus now reach the SQL editor and Ask-your-data.
  - **Authentication.** Each query signs in with a five-minute Cairn token for the caller, so Trino knows the user from the token. `protocol=https` selects TLS, and `verify` is the CA file to trust (`true` means the system's CAs; `false` turns checking off). The connection's CA file wins over `REQUESTS_CA_BUNDLE`. For a Trino that does not trust Cairn's tokens, put a password in the URL (`trino://cairn:<password>@host:443/lakehouse`): `cairn` then impersonates the caller, which needs `MANAGE_SYSTEM_OPERATIONS`. If Trino answers 401, the editor says that Trino did not accept Cairn's sign-in.
  - **Requirements.** It needs the `trino` extra (`cairn-platform[trino]`, part of `all`).
  - **Cairn's own reads** (quality monitors with `source: sql`, profiling) on such a connection sign in as its `service_user` (default `urn:li:corpuser:cairn-quality`) with a fresh 15-minute Cairn token, so Trino asks Cairn's policies about that user like anyone else: grant it read on the tables you monitor (row filters on it would change the counts it sees). Without Cairn's Trino sign-in (`CAIRN_TRINO_ACCESS_KEY`) these reads are refused, never sent unsigned. A connection with a password in its URL signs in with that instead.
  - **Row cap.** The editor's row cap now goes onto the query's own `LIMIT` (a smaller limit the query already has is kept), so `ORDER BY` holds on engines that drop the order of a subquery, as Trino does.
- **When Cairn Core is down.** Trino sends each lineage event once and never resends it. An event Core cannot take now therefore waits in Cairn's database (`cairn_trino_lineage_outbox`), Trino gets a 202, and the query itself is unaffected.
  - **Retries.** Every API replica retries due events every `CAIRN_TRINO_LINEAGE_RETRY_SECONDS` (15), backing off from 15 seconds to 15 minutes. A replica claims an event before writing it, so each is written once.
  - **Dropped events.** An event Core rejects as invalid, or one still unwritten after `CAIRN_TRINO_LINEAGE_MAX_AGE_SECONDS` (7 days), is dropped and audited (`trino.lineage_dropped`).
  - **Watching it.** The gauge `cairn_trino_lineage_held` counts waiting batches. Only when the database cannot keep an event either does Trino get a 503.
  - **Tested** with a real Cairn Core in Compose (MySQL, OpenSearch, Kafka). Lineage from `CREATE TABLE AS` and `INSERT` reached Core with column lineage. With Core stopped, a `CREATE TABLE AS` still ran and its two events were held; 10 seconds after Core came back they were written and the job was in Core.
- **Table locations.** Namespaces report a `location` (their place under the warehouse, unless they set one), and new tables go under it with a unique suffix. Trino builds its table locations from it.

**dbt.** dbt projects run on the lakehouse through Trino, as a Cairn identity, and each run is recorded in Cairn.

- **Who a run is.** Each project runs as its own Cairn user, for example `svc-dbt`: create the user in Cairn and a personal access token for it. Cairn's policies then decide what the project may read and build, like for anyone:
  - `VIEW_DATASET_PROFILE` on its sources;
  - `WRITE_DATASET` and `MANAGE_DATASET` on its target schema;
  - `EDIT_LINEAGE` and `EDIT_ENTITY_ASSERTIONS` to record its runs.

  Masks and row filters apply to it. Lineage, usage and the audit trail name it.
- **No database password.** `cairn dbt <dbt arguments>` exchanges the Cairn token for a short-lived Trino token (`CAIRN_DBT_TOKEN_TTL`, 4 hours by default, at most 12; it must outlast the run). It writes a private profile (`method: jwt`, TLS) and runs dbt. The token never reaches the project or dbt's artifacts. With `CAIRN_CA_FILE`, Trino's certificate is checked against that CA only, whatever `REQUESTS_CA_BUNDLE` says. Queries run in Trino's `pipelines` resource group.
- **What a run records** (`POST /pipelines/dbt/runs`, sent by `cairn dbt` after `run`, `build`, `test`, `seed` or `snapshot`), on the datasets Trino names, for the models, seeds and snapshots the run built:
  - **Docs.** Descriptions and column descriptions fill only empty ones: a Trino comment or a description written in Cairn is never overwritten. `dbt.*` properties hold the unique id, materialization, package, path and last status. Catalog syncs keep them. To keep docs in the tables themselves, where every engine sees them, set `+persist_docs: {relation: true, columns: true}` in the project, as the example does.
  - **Tags and owners.** dbt `tags` become tags, and `meta.owner` (a Cairn user name) a technical owner. Both are added, never removed.
  - **Lineage.** Each model's upstream models and sources. dbt's lineage is replaced on every run, so a removed `ref` disappears, and lineage from elsewhere is kept. Trino's own query lineage still arrives as well.
  - **Tests.** Each dbt test becomes a check on the model it tests (pass, warn, fail or error), on its Quality tab, and a newly failing test tells the model's owners.
  - The run is audited (`dbt.run_recorded`).
- **Governance and the whole project** (from the manifest, on every run):
  - **Model properties.** A model's contract (enforced or not), access (public, protected, private), group (and the group's owner) and version become `dbt.*` properties.
  - **Deprecation.** A `deprecation_date` deprecates the dataset in Cairn, with its decommission date. Lifting it in dbt lifts only dbt's deprecation.
  - **Exposures** become dashboards (or the exposure's other type) downstream of the models they use, owned by their owner.
  - **Metrics.** MetricFlow semantic models and metrics arrive in the Context inbox as metric definitions, mapped to their datasets, for a steward to approve.
  - **Unit tests** are checks on their model, named `unit test: <name>`.
  - **Source freshness.** `cairn dbt source freshness` records a `source freshness` check on each source, PASS, WARN or FAIL, with its age and limits.
- **Run history.** Each run is kept: command, environment (`CAIRN_DBT_ENVIRONMENT`, `prod` by default), status, timing, and each node's result. They are served at `GET /pipelines/dbt/runs` and on the **Pipelines** page. Operators (`MANAGE_INGESTION`) see every run, and a pipeline user sees its own.
- **Slim CI.** A successful `run` or `build` saves its manifest as its environment's state (`GET /pipelines/dbt/state`). With `CAIRN_DBT_DEFER_TO=prod`, `cairn dbt` fetches prod's state and adds `--state … --defer`, so `cairn dbt build --select state:modified+` builds only what changed and defers the rest to prod. CI runs record their own environment (`CAIRN_DBT_ENVIRONMENT=ci`) and never become prod's state.
- **Docs site.** `cairn dbt docs generate` publishes dbt's docs site to Cairn, at `/cairn/api/pipelines/dbt/docs/<project>/`. Each viewer's manifest and catalog hold only the models, sources and tests of datasets they may see, with exposures and metrics limited to those. Publishing is audited (`dbt.docs_published`).
- **Packages from a private mirror.** `CAIRN_DBT_PACKAGE_HUB_URL` (Helm `cairn.dbt.packageHubUrl`) points `dbt deps` at a mirror of the dbt package hub, so builds need no internet access. Installing packages when the project's image is built works too.
- **Exit codes.** `cairn dbt` exits with dbt's code. If dbt succeeded but the run could not be recorded, it exits 3, so a scheduler notices; set `CAIRN_DBT_RECORD=optional` to only warn.
- **The image.** `cairn/dbt` (`docker/dbt.Dockerfile`) has dbt-core 1.12 and dbt-trino 1.11 and runs as a non-root user. It works with a read-only root filesystem and a read-only project: dbt writes only under `/tmp/cairn`, artifacts in `/tmp/cairn/target`. A project's own image adds its code: `FROM cairn/dbt` and `COPY . /project`.
- **Compose:** `docker compose -f docker-compose.cairn.yml --profile trino --profile dbt run --rm cairn-dbt build` runs `CAIRN_DBT_PROJECT` (default `examples/dbt/lakehouse`) with the token in `CAIRN_DBT_TOKEN`.
- **Helm:** `cairn.dbt.jobs` makes a CronJob per project. Each job sets:
  - `name`, `image`, `schedule` and `args` (default `build`);
  - `schema`, the default schema;
  - `tokenSecret`, the pipeline user's token;
  - `caSecret`, Trino's CA;
  - `timeoutSeconds` and `threads`.

  Jobs never overlap and are not retried, since a failed build is for a person to look at. The chart refuses jobs without `cairn.trino` and its TLS. Trino's certificate must name `<release>-cairn-trino`. Airflow can run the same image instead of a CronJob.
- **Tested** (`tests/test_dbt_e2e.py`, in CI against the `cairn/dbt` image as built) with dbt-core 1.12.6 and dbt-trino 1.11.0, on Trino 476, Cairn Catalog and Cairn Store:
  - **The build.** `svc-dbt`, signed in with its personal access token, built a seed, a table from a source and a view. The table's docs, owner, tag, lineage and properties landed on the datasets Trino names, and the view's lineage named both of its upstreams.
  - **Tests.** A passing and a failing test became checks on their models. dbt's exit code (1, for the failing test) came back after the run was recorded.
  - **Secrets.** The Trino token appeared in none of the project's or dbt's files.
  - **Crawls.** A catalog sync kept dbt's description and properties, and the next run's column docs.
  - **The example project.** It built, and with `persist_docs` its docs were in the Iceberg table and its columns, as Trino shows them.
  - **Policies.** `svc-dbt` was refused tables outside its grants and saw masked PII.
  - **Recording.** It was refused without the rights to record.
  - **The governed project** (`tests/test_dbt_more_e2e.py`): contract, access, group owner and deprecation landed on the datasets. The exposure became a dashboard over its model, the unit test a check and the metric an inbox finding. Source freshness passed while fresh and failed once stale. Runs were listed, and a CI build of one changed model deferred to prod's state without replacing it. The docs site showed `svc-dbt` its models and hid them from `bob`, and a package came from a stub private hub.

**Airflow.** Airflow 3 (3.3) orchestrates pipelines on the lakehouse: its tasks run as a Cairn identity, its runs and lineage go to Cairn, and people sign in to its UI through Cairn.

- **The image.** `cairn/airflow` (`docker/airflow.Dockerfile`) is the official Apache Airflow image with:
  - the FAB, OpenLineage and Kubernetes providers, pinned by Airflow's own constraints;
  - `cairn-airflow-provider` (`airflow-provider/`);
  - `cairn dbt` with dbt-core and dbt-trino in a virtualenv of their own, `/opt/cairn-dbt`, so dbt's dependencies never meet Airflow's.
- **Who a task is.** A `cairn` connection (`cairn_default`) holds Cairn's API URL and a pipeline user's Cairn token, for example `svc-airflow`. Put the token in a secrets backend in production, or in `AIRFLOW_CONN_CAIRN_DEFAULT`. Its `extra` names Trino (`trino_host`, `trino_port`, `trino_catalog`) and the CA (`ca_file`). Grant the user what its DAGs read and build, plus `EDIT_LINEAGE` and `EDIT_ENTITY_ASSERTIONS`.
- **Operators.**
  - `CairnTrinoOperator(sql=..., schema=...)` runs SQL on Trino with a fresh short-lived Trino token per task. Cairn's policies, masks and row filters apply, and the last statement's rows (at most 1000) are the task's result. Queries run in the `pipelines` resource group.
  - `CairnDbtOperator(project_dir=..., command="build", select=...)` runs `cairn dbt` (see **dbt**), so the run is recorded in Cairn. Artifacts, logs and packages go to a scratch directory per run, so projects can be read-only. The task fails when dbt fails, and when Cairn could not record a successful run.
  - The connection's CA file wins over `REQUESTS_CA_BUNDLE`.
- **Runs and lineage in Cairn.** Point OpenLineage's HTTP transport at Cairn (`AIRFLOW__OPENLINEAGE__TRANSPORT`: url `<cairn>`, endpoint `openlineage/api/v1/lineage`, `api_key` auth with the pipeline user's token). Each DAG becomes a pipeline in Cairn and each task a job with its runs. Trino's own lineage adds the tables each query read and wrote, named like the catalog's datasets.
- **Sign-in through Cairn.** Airflow's UI signs people in with Cairn as the OAuth2 server (client `airflow`, with its own secret, redirect addresses and token audience).
  - **Roles.** `webserver_config.py` calls `cairn_airflow.auth.configure(globals())`. The token's signature is checked against Cairn's published key, and the person's Cairn groups map to Airflow roles through `CAIRN_AIRFLOW_ROLES` (`cairn-admins=Admin;data-engineers=User`, or JSON).
  - **Fails closed.** Anyone in no mapped group signs in with the `Public` role, which sees nothing. Roles are synced at every sign-in, so removing someone from a group removes their access at their next sign-in.
  - **Kept apart from Trino.** Airflow's tokens are refused by Trino (another audience), and a code issued to one client cannot be redeemed by the other. Sign-ins are audited (`airflow.sign_in`).
  - **On Cairn's side,** set `CAIRN_AIRFLOW_OAUTH_SECRET` and `CAIRN_AIRFLOW_OAUTH_REDIRECT_URIS` (`https://<airflow>/auth/oauth-authorized/cairn`; plain HTTP only for `localhost`). This needs Trino's sign-in to be on (`CAIRN_TRINO_ACCESS_KEY`), whose signing key it uses.
- **Runs when data changes.** `CairnDatasetChangedTrigger(["lakehouse.sales.orders"])`, in an Airflow 3 `AssetWatcher`, runs a DAG whenever one of its tables changes, whoever wrote it.
  - **Where changes come from.** Cairn records each dataset a pipeline finished writing: completed OpenLineage runs (Spark, Airflow, Trino's own lineage) and the models of dbt runs. They are served at `GET /v1/datasets/changes`, in order by a cursor, only for datasets the caller may see, and kept 30 days.
  - **Naming tables.** URNs, or tables as `catalog.schema.table`, which `GET /v1/datasets/resolve` turns into URNs.
  - **No replay.** The trigger watches from when it starts.
- **Quality gates.** Both read Cairn's status: monitors, plus other tools' checks such as dbt tests, Great Expectations and Soda.
  - `CairnQualityGateOperator(datasets=[...])` fails the task, without retries, when a dataset is not good enough, and names what failed.
  - `CairnQualitySensor` waits until the datasets are good, in reschedule mode.
  - By default, PASS and WARN pass, unchecked datasets do not, and open incidents block.
- **Failed pipelines.** When a run fails, every table its job writes gets an incident ("[Pipeline] … failed"), and the owners of the job and the tables are notified (`pipeline.run_failed`). The job's next successful run resolves it. One incident per job and table, however often it fails. `CairnTrinoOperator` reports the tables its SQL reads and writes, so its runs, failed ones too, are tied to them.
- **Team access.** With `CAIRN_AIRFLOW_TEAM_ROLES=true`, each Cairn group a person is in also gives them the Airflow role `team:<group>`, plus `CairnMember`, which is Viewer without access to every DAG. A DAG grants its team access with `access_control={"team:finance": {"DAGs": {"can_read", "can_edit"}}}`, and people see only their teams' DAGs. A team's role is created when a DAG first names it.
- **dbt, one task per model.** `CairnDbtTaskGroup(group_id=..., project_dir=...)` builds one task per model, seed and snapshot from the project's `manifest.json` (from `dbt parse`, ahead of time), wired as dbt's DAG. Each task runs `cairn dbt build --select fqn:<node>` and records its run, so a failed model is retried alone and its tests gate what depends on it.
- **Cairn flows on Airflow.** `cairn_airflow.flows.CairnFlowStepOperator` and `CairnFlowBranchOperator` run one step of a Cairn flow in Cairn, as the flow's owner, and `cairn_airflow.quality.CairnCheckOperator` is a flow's check on Trino: what a flow exported as a DAG, or a flow with `runner: airflow`, uses. Cairn also shows Airflow's DAGs to the people they belong to and converts them to flows. See [Airflow, both ways](#airflow-both-ways).
- **Airflow's own operators.** With `AIRFLOW__SECRETS__BACKEND=cairn_airflow.secrets.CairnSecretsBackend`, the connection `trino_lakehouse` gets a fresh Trino token for the pipeline user each time it is used. `SQLExecuteQueryOperator` and `TrinoOperator` then run under Cairn's policies with no stored password. The backend also serves `cairn_default`.
- **Lineage without a stored key.** Set OpenLineage's transport `auth` to `{"type": "cairn_airflow.lineage.CairnTokenProvider", "workload_token_file": "/var/run/secrets/cairn/token"}`. Lineage is then sent with the pipeline user's Cairn token, exchanged from the pod's token and renewed before it expires.
- **Tested** (`tests/test_airflow_more_e2e.py`, with the `cairn/airflow` image):
  - **Quality gate.** It failed while a check failed, naming it, and passed once the check passed.
  - **Failure incident.** A task refused by Cairn's policies raised an incident on the table it writes; the next good run resolved it and recorded a change.
  - **Trigger.** It fired when the table was written.
  - **Secrets backend.** Airflow's `SQLExecuteQueryOperator` read a table the pipeline user may see, and was refused one it may not.
  - **dbt.** The example project ran as one task per model, each recorded.
  - **Team access.** A person in `finance` got `team:finance` and `CairnMember` and could read the finance DAG but not HR's.
  - **Lineage.** It reached Cairn through the token provider, with the task's tables.
- **Compose:** `docker compose -f docker-compose.cairn.yml --profile trino --profile airflow up -d` starts Airflow's API server (`http://localhost:8090`), scheduler, DAG processor and triggerer. It uses LocalExecutor and an `airflow` database in Cairn's PostgreSQL, created by `airflow-init`. Tasks run with the token in `CAIRN_AIRFLOW_TOKEN`, on the DAGs in `CAIRN_AIRFLOW_DAGS` (default `examples/airflow/dags`) and the dbt projects in `examples/dbt`.
- **Kubernetes:** use the official Apache Airflow chart (1.20+) with `deploy/airflow/values-cairn.yaml`. It sets the `cairn/airflow` image, KubernetesExecutor (each task in its own pod), external PostgreSQL, secrets for the Cairn connection, OpenLineage and the OAuth secret, sign-in through Cairn with no local admin account, and DAGs from git. The file lists the Secrets to create. CI renders the chart with it.
- **Tested** (`tests/test_airflow_e2e.py`, in CI against the `cairn/airflow` image as built) with Airflow 3.3.2, Trino 476, Cairn Catalog and Cairn Store:
  - **A DAG.** It ran SQL on Trino and a dbt build as `svc-airflow`. Both tables were built, the dbt run was recorded, the DAG and both tasks reached Cairn through OpenLineage, and Trino's lineage named the source table.
  - **Policies.** A DAG reading a table `svc-airflow` may not see failed with Access Denied, and its dbt task never ran.
  - **Sign-in.** Airflow's API server (over TLS) sent the browser to Cairn. Signed in to Cairn, the browser came back and was signed in to Airflow. `ana` got the User role from her group, and `bob`, in no mapped group, got only Public. A token not signed by Cairn was refused.
  - **Compose.** In a Compose slice the API server became healthy, the scheduler, DAG processor and triggerer ran, the example DAG parsed, and `/auth/login/cairn` redirected to Cairn's sign-in, with PKCE.

**Spark and other engines that read files (pyiceberg, DuckDB).** Spark 4 runs on the lakehouse as a Cairn identity. Cairn decides every table it touches, the catalog vends storage credentials one table at a time, and its lineage reaches Cairn.

- **Why this is different from Trino.** Trino applies Cairn's masks and row filters inside each query. Spark reads a table's data files itself, and those files hold what a mask or filter would hide. So for these engines:
  - Cairn Catalog asks Cairn about each table, as the user (`POST /v1/lakehouse/decide`, with Cairn's service token; decisions are cached for 15 seconds).
  - A table with a mask or row filter for that user is **refused**: "Masks or row filters apply to this table for you. Query it through Trino." The user queries it through Trino, or through a view a data owner builds in Trino.
  - **Reading it anyway, governed.** `cairn_spark.governed_read(spark, "sales.orders")` reads such a table through Trino, as the same user, with Spark's JDBC reader. Trino applies their masks and row filters, so Spark gets what they may see and never the files. Spark pushes column choices and filters down to Trino.
    - **Parallel reads.** Give `partition_column` with `lower_bound`, `upper_bound` and `num_partitions`, or `predicates`: one WHERE clause per partition.
    - **Settings.** Set `CAIRN_TRINO_URL` (https) and `CAIRN_TRINO_CATALOG` (default `lakehouse`). Set `CAIRN_CA_FILE` at the same path on the driver and the executors. The image includes Trino's JDBC driver.
    - **The token.** The read signs in with a short Trino token from Cairn (`POST /v1/trino/token`), passed in the read's properties so Spark redacts it. The read must start within the token's life. Pipeline users get one the same way: with their personal access token, or a workload token exchanged at Cairn (`CAIRN_WORKLOAD_TOKEN_FILE`). Only the shared service token and agents are refused.
    - **Tested** with Spark, Trino and Cairn Catalog (`tests/test_spark_e2e.py`). Through Trino, `ana` saw her masked emails, also in a two-partition read with a pushed-down filter; `bob` saw only his rows; a table hidden from `ana` was refused. The catalog still refused Spark the files.
  - Tables the user may not see are not listed. Their namespaces show only what is visible.
  - Reading needs read. Appending, overwriting and deleting rows needs write. Creating, dropping, renaming and changing schema or properties needs manage. Namespaces need Cairn's system privilege.
  - Vended credentials are read-only unless the user may write the table. They cover only that table's location.
- **Fails closed.** The catalog refuses every Cairn-issued token when Cairn cannot answer (503), and any error in a decision decides nothing. It will not start with Cairn-issued tokens unless it vends scoped credentials (`CAIRN_CATALOG_VEND_ROLE`), since a store key would bypass the per-table decisions. Tokens are RS256, checked against Cairn's published key (`/v1/trino/jwks.json`) for issuer, audience `lakehouse`, expiry and not-before. Trino's and Airflow's tokens are refused (other audiences). The catalog's static tokens (`CAIRN_CATALOG_TOKEN`, for Trino and admin tools) work as before.
- **Who a job is.** A person, or a pipeline user such as `svc-spark` with a personal access token. `POST /v1/lakehouse/token` exchanges it for a **lakehouse session**: a first catalog token and a session credential. Opening a session is audited (`lakehouse.token_issued`). Service and agent tokens get no session.
- **Long jobs.** A job runs as long as its session lasts, without a long-lived token.
  - **Tokens.** Each catalog token lasts an hour (`CAIRN_LAKEHOUSE_TOKEN_TTL`). Iceberg gets and renews tokens at `POST /v1/lakehouse/oauth/tokens`, on the driver and on every executor. It uses the session credential (OAuth client credentials) or the current token (token exchange, RFC 8693).
  - **Checks on every renewal.** The session must not have been ended, and the user must not be suspended. A session ends after `CAIRN_LAKEHOUSE_MAX_SESSION` (7 days) in any case. If Cairn cannot check, the renewal is refused. Renewals are audited (`lakehouse.token_refreshed`).
  - **The credential.** Only its hash is kept. `cairn-spark` adds it to Spark's redaction, so it never shows in the Spark UI or event logs.
  - **Storage keys** are renewed through the catalog (`GET …/tables/{table}/credentials`), with the table decided again each time. `CAIRN_CATALOG_VEND_REVALIDATE_SECONDS` (360 seconds up to the keys' lifetime, `CAIRN_CATALOG_VEND_SECONDS`) sets how often clients must come back. A permission removed then reaches running jobs within that time.
- **Ending sessions.** `POST /v1/lakehouse/sessions/revoke` with `{"user": "..."}` (`MANAGE_SYSTEM_OPERATIONS`) ends all of a user's open sessions in one audited transaction (`lakehouse.sessions_revoked`). None is renewed again, so the job stops once its last token expires: within `CAIRN_LAKEHOUSE_TOKEN_TTL`, plus the catalog's minute of allowed clock skew. Suspending the user (SCIM) has the same effect at the next renewal. New sessions can still be opened.
- **Iceberg's client.** Use the session credential, not a bare token. Iceberg 1.10's REST client renews a token it was given only once, so a job given a token alone stops after two token lifetimes. `cairn_spark` sets this up. For your own settings, see `lakehouse_conf(...)`.
- **In a job.** `from cairn_spark import lakehouse_session; spark = lakehouse_session("my-job")` (package `cairn-spark`, `spark/`). It reads:
  - `CAIRN_URL`, Cairn's API;
  - `CAIRN_TOKEN` or `CAIRN_TOKEN_FILE`, the caller's Cairn token;
  - `CAIRN_CATALOG_URI`, Cairn Catalog;
  - `CAIRN_CA_FILE`, if needed.

  The `lakehouse` catalog is the default, so `spark.sql("SELECT … FROM analytics.products")` works. For `spark-submit --conf` settings instead, see `cairn_spark.lakehouse_conf(...)`.
- **Lineage.** The OpenLineage Spark listener sends each job's runs, with table and column lineage, to Cairn as the same user. It authenticates with the lakehouse session credential, which Cairn accepts at the OpenLineage endpoint only. So a job's lineage lasts as long as its session, not one token's hour, and stops when the session is ended or the user is suspended. Spark names tables by their storage paths, so Cairn renames the tables of Cairn Catalog (`CAIRN_LAKEHOUSE_CATALOG_URIS`: the catalog's addresses as engines reach it) to the datasets Trino and the catalog crawler name. Lineage from Trino, dbt and Spark then meets on one dataset.
- **The image.** `cairn/spark` (`docker/spark.Dockerfile`) is the official Apache Spark 4.0 image with:
  - Apache Iceberg 1.10 (Spark runtime and AWS bundle, for S3FileIO);
  - OpenLineage for Spark 1.55;
  - `cairn-spark`.
- **Turning it on.**
  - **Compose:** on by default (`CAIRN_CATALOG_JWKS_URL` and `CAIRN_CATALOG_CAIRN_URL` on `cairn-catalog`). `docker compose -f docker-compose.cairn.yml --profile spark run --rm cairn-spark /jobs/lakehouse_products.py` runs a job from `CAIRN_SPARK_JOBS` (default `examples/spark`) with the token in `CAIRN_SPARK_TOKEN`.
  - **Helm:** set `cairn.catalog.identity.enabled: true`. This needs `cairn.catalog.vendRole` and `cairn.trino.enabled`, or the chart refuses to render. Set `identity.publicUrl` to the catalog's address outside the cluster, if engines use one.
  - **Network policy:** Spark's pods need to reach the API (`cairn.networkPolicy.apiIngressFrom`), the catalog (`catalogIngressFrom`) and the store.
  - **Running jobs:** run `cairn/spark` (or `FROM cairn/spark` with your code) with `cairn-spark submit`, the Spark Operator, or Airflow (below).
- **JVM programs and SQL.** `cairn-spark {submit|sql|shell|pyspark} [Spark arguments]` runs any Spark program as a Cairn identity: Scala and Java jobs (`--class`), the `spark-sql` shell, Python files.
  - It opens the lakehouse session (the same variables as `lakehouse_session`) and passes the settings to Spark in a private properties file: mode 0600, removed afterwards. The session credential never appears on a command line.
  - The caller's `--properties-file` and `--conf` still apply.
- **Spark on Kubernetes, with no secret in the cluster.** In cluster mode, spark-submit stores the application's settings in a ConfigMap that the driver mounts. A credential given at submit time would be readable by anyone who may read ConfigMaps in the namespace. So a cluster-mode application gets no credential at all:
  - `cairn-spark submit --master k8s://… --deploy-mode cluster job.py` submits only the driver's Cairn settings (`CAIRN_URL`, `CAIRN_CATALOG_URI`, `CAIRN_WORKLOAD_TOKEN_FILE`), plus a driver pod template.
  - The template mounts a projected service-account token for audience `cairn` at `/var/run/secrets/cairn/token`. The kubelet rotates it.
  - A job's own driver pod template (`spark.kubernetes.driver.podTemplateFile`, JSON or YAML) is kept: its volumes, sidecars and tolerations stay, and the token is added to it. A template that already uses the token's volume name or mount path is refused, not overwritten (`tests/test_spark_templates.py`; the kind job mounts its program from a config map this way).
  - The driver, a Python program calling `lakehouse_session()`, exchanges that token at Cairn (workload identity) and signs in as the pipeline user its service account is bound to.
  - Executors get no token. Their settings come from the driver over Spark's RPC, and their storage keys come with the tables.
  - The service account is `--conf spark.kubernetes.authenticate.driver.serviceAccountName=…` or `CAIRN_SPARK_SERVICE_ACCOUNT`.
  - **JVM programs** (`--class`) run in cluster mode too. Their catalog settings must exist before the program starts, so their driver pod's command is `cairn-spark-driver`. It signs in with the pod's projected token, opens the lakehouse session, merges its settings into a private copy of the driver's properties file (mode 0600, inside the pod) and then starts Spark's own driver. Nothing secret reaches the cluster's API. Client mode works as before.
- **The Spark Operator.** `cairn_spark.kubernetes.spark_application(name, image=…, main="/jobs/orders.py", service_account="svc-spark", cairn_url=…, catalog_uri=…)` builds the same application as a `SparkApplication` (`sparkoperator.k8s.io/v1beta2`, for `kubectl apply`):
  - a Python driver with the projected token and the driver environment;
  - executors with no identity.

  With `main_class`, it builds a JVM application whose driver template runs `cairn-spark-driver`. It refuses any `spark_conf` key that looks like a secret.
- **From Airflow.** `cairn_airflow.spark.CairnSparkApplicationOperator(task_id=…, name=…, namespace=…, service_account=…, image=…, main=…, cairn_url=…, catalog_uri=…)`:
  - creates one `SparkApplication` per task try, labelled with its DAG and task;
  - follows it to `COMPLETED`;
  - fails the task with the application's error message on `FAILED` or `SUBMISSION_FAILED`;
  - deletes the application when the task is killed.

  The worker's service account needs `create`, `get`, `watch` and `delete` on `sparkapplications`, and nothing on Secrets. The `cairn/airflow` image ships `cairn-spark` for it.
- **Helm.** `cairn.spark.enabled: true` (needs `cairn.workloadIdentity.issuers`) creates:
  - the drivers' service account (`driverServiceAccount`, default `svc-spark`; bind it to a pipeline user in Cairn);
  - a Role letting it start and stop its executors (pods, services, ConfigMaps, volume claims; nothing on Secrets);
  - with `submitters: [{name: airflow-worker, namespace: airflow}]`, a Role letting those accounts run `SparkApplication`s.
- **Teams.** `cairn.spark.teams` gives each team its own namespace (`spark-<team>` by default):
  - its drivers' service account (`svc-spark-<team>`; bind it to the team's pipeline user in Cairn), with a Role to manage its executors there and nowhere else, and nothing on Secrets;
  - a ResourceQuota on what all its Spark pods use together (`quota: {cpu, memory, pods}`);
  - a LimitRange: default requests for pods that state none (so the quota counts them), and a cap per container (`maxCpu`, `maxMemory`);
  - with `submitters`, a Role letting those accounts (e.g. Airflow's workers) run `SparkApplication`s there.

  Run a team's jobs with `--conf spark.kubernetes.namespace=spark-<team>` and its service account, or `namespace` and `service_account` on `CairnSparkApplicationOperator`.
- **History server.** Set `CAIRN_SPARK_EVENT_LOG_DIR`, and `lakehouse_session`, `cairn-spark` and the Kubernetes helpers turn on Spark's event logs there. The session credential, Cairn's token and store keys are redacted in them (`*********(redacted)`), as in the Spark UI. Each application records its Cairn user (`spark.cairn.user`).
  - **Signed in through Cairn.** Spark's history server has no sign-in of its own, so it is never exposed. Cairn serves it at `/v1/spark/history/` (`CAIRN_SPARK_HISTORY_URL`: where Cairn reaches it). Each person sees the applications they ran, and nothing about others' (404). People with `MANAGE_SYSTEM_OPERATIONS` see every application. Only reads are passed on. Run the history server with `-Dspark.ui.proxyBase=<Cairn's public API path>/v1/spark/history`, so its pages link back through Cairn.
  - **Event logs in Cairn Store.** Set `CAIRN_SPARK_EVENT_LOG_DIR=s3a://spark-events/logs`, `CAIRN_STORE_URL` (the store's S3 endpoint) and `CAIRN_SPARK_EVENT_LOG_ROLE`. The job trades its lakehouse token for the role's temporary credentials at the store's STS (`AssumeRoleWithWebIdentity`), for `CAIRN_SPARK_EVENT_LOG_SECONDS` (default 12 hours, or the role's maximum). So no store key is in any job. Set up the role once, with the store's IAM API:
    - its trust: `{"Effect": "Allow", "Principal": {"Federated": "<Cairn's token issuer>"}, "Action": "sts:AssumeRoleWithWebIdentity"}`. The store must trust that issuer, for audience `lakehouse` (`CAIRN_STORE_STS_PROVIDERS`).
    - its policy: `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on `arn:aws:s3:::spark-events/logs` and `…/logs/*` (S3A looks at the directory's own key too), and `s3:ListBucket` on the bucket. `MaxSessionDuration`: 43200.
    - The history server reads the prefix with a read-only key (`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in its environment; `cairn_spark.eventlog.history_server_opts` gives its settings).
    - **Who can read what.** Spark users can write, and read back, the shared event-log prefix: event logs hold query plans, never secrets. To keep teams' logs apart, give each team its own bucket, role and history server. Store policies also take `${cairn:user}` and `${aws:username}` in resources, for prefixes per person where a tool needs no shared directory.
  - **Compose:** `cairn-spark-history` (profile `spark`), on the `spark-events` volume, through Cairn at `/cairn/api/v1/spark/history/`. Its port is on the host's loopback only (`127.0.0.1:18080`), for operators.
  - **Helm:** `cairn.spark.historyServer.enabled: true`: a ClusterIP service, served through Cairn. The logs are on a ReadWriteMany claim (`cairn.spark.eventLog`, mounted by drivers at `/opt/spark-events`), or in Cairn Store (`cairn.spark.eventLog.store: {enabled, dir, readerSecret}`).
  - Logs older than `maxAge` (default 30 days; `CAIRN_SPARK_HISTORY_MAX_AGE` in Compose) are removed.
- **Streaming.** Structured Streaming into Iceberg works the same way (`writeStream.format("iceberg").toTable("lakehouse.…")`). A stream runs past its token lifetimes because its session renews them, and it stops when the session is ended.
- **pyiceberg and DuckDB.** The package `cairn-lakehouse` (`lakehouse-client/`) gives Python engines the same governed access:
  - `cairn_lakehouse.catalog()` opens a lakehouse session (the same variables as Spark) and returns a pyiceberg catalog on Cairn Catalog, with per-table storage keys and renewal through the session credential.
  - `duckdb_connection(["analytics.products"])` registers each table as a DuckDB view, read through pyiceberg, so DuckDB needs no extension and no store key. Install it with `pip install "cairn-lakehouse[duckdb]"`.
  - Tables with masks or row filters are refused, as for Spark.
- **What Spark costs.** Set `CAIRN_COST_SPARK_USD_PER_CORE_HOUR` and Cairn prices each Spark application from its OpenLineage events (see **Cost per asset**).
- **Not supported, on purpose.**
  - **Shared multi-tenant Spark Connect servers.** A Spark Connect server holds one catalog session for everyone it serves. Cairn's per-table decisions and storage keys would be those of the server's identity, not each user's. Run one Spark application per identity (the pattern above), or query shared data through Trino, which applies each user's policies itself.
  - **dbt Python models.** On dbt-trino they don't exist: Trino runs SQL only. Running them on Spark through dbt would put a session credential in dbt's profile on every developer's machine and CI runner. Use SQL models on Trino, and Spark jobs (above) for Python transformations, scheduled next to dbt in Airflow.
- **Tested** (`tests/test_spark_e2e.py`, in CI against the `cairn/spark` image as built) with Spark 4.0.1, Iceberg 1.10.0 and OpenLineage 1.55.0, on Cairn Catalog with the store's STS:
  - **Reads.** `ana` listed only what she may see. A table with her masked column was refused, and so was one she may not read.
  - **Writes.** She read, built a table (CTAS) and inserted into it in a schema she manages, and was refused an insert into one she may only read.
  - **Lineage.** It reached Cairn on the lakehouse dataset names, column by column.
  - **Row filters.** `bob`'s row filter kept the files away from him too.
  - **The example job.** It ran as `ana` and was refused as `bob`.
  - **Tokens.** A forged signature, a malformed token and Trino's token were refused (401). A service token got no lakehouse token, and only a service token could ask for decisions.
  - **Long jobs.** A job running seven minutes on one-minute tokens kept reading and committing. Its storage keys were reported to expire after six minutes, so they had to be renewed. Every token renewal was in its one session, and its keys were renewed through the catalog, decided again: a reader's renewal stayed read-only, and someone the table is not for was refused.
  - **Ending a session.** After `ana`'s sessions were ended mid-job, her commits failed once her last token expired. She could not end sessions herself, and could open a new one.
  - **Renewals.** Token exchange and the session credential (form or HTTP Basic) worked; another service's token, a token with no session, a forged token and a wrong secret were refused. So were a suspended user and a session past its maximum length, and when Cairn could not check the user it answered 503. With Cairn unreachable the catalog answered 503. It would not start with Cairn-issued tokens but no scoped credentials.
  - **JVM and SQL.** `cairn-spark sql` (a JVM program, no Python session) counted a table as `ana`, was refused a masked table, and refused `bob` the table. The session credential was never printed.
  - **Streaming.** A rate-source stream into Iceberg kept committing micro-batches for 150 seconds on one-minute tokens.
  - **History server.** A job wrote its event log to Cairn Store with the event-log role's temporary credentials. The role could not list another bucket. The log, and the history server reading it from the store, showed the run with its session credential, lineage key and store keys redacted, and no Cairn token. Through Cairn, `ana` saw her run, `bob` saw nothing of it (404 on its pages), and an operator saw everything.
  - **pyiceberg and DuckDB** (`tests/test_lakehouse_client_e2e.py`). Both were governed like Spark: listing, a refused masked table, writes only where allowed.
- **Tested on Kubernetes** (`tests/test_spark_k8s_e2e.py`, against a real Kubernetes 1.31 API server and the `cairn/spark` image; no kubelet, so what is checked is what Spark and the operator write to the cluster):
  - **Cluster mode.** `cairn-spark submit --deploy-mode cluster`, run by a submitter with a Cairn token in its environment, created a driver pod. The pod had the `svc-spark` service account, the projected `cairn` token and the driver environment.
  - **No secret stored.** Nothing secret was in the namespace's pods, ConfigMaps, Secrets or services, including the driver's `spark.properties`. A JVM program (SparkPi) submitted in cluster mode got a driver pod running `cairn-spark-driver`, again with no secret stored.
  - **A JVM driver signs itself in.** `cairn-spark-driver`, run with that pod's environment, a cluster-issued token and Spark's own driver arguments, ran `spark-sql` (a JVM program) as `ana` and counted the governed table.
  - **The driver signs itself in.** Run with that pod's environment and a token the cluster issued for `svc-spark`, it signed in as `ana` through workload identity. It read and wrote governed tables, and was refused one `ana` may not read.
  - **The run was priced.** It was charged at 2 cores for its wall-clock time, all to the table it wrote.
  - **The Spark Operator.** Its own `sparkapplications` CRD (v2.2.1) accepted the Python and JVM `SparkApplication`s that `cairn_spark` builds (server-side, strict validation). A jar without its main class, and a secret-looking setting, were refused.
  - **Teams** (`tests/test_spark_teams_k8s.py`, with the API server's quota controller). The chart's team objects were applied. A pod stating nothing got the team's default requests. A pod over the per-container cap was refused, and so were pods past the team's CPU and pod quotas. The drivers' account could manage pods and ConfigMaps in its namespace, not in another, and could not read Secrets.
  - **Airflow.** `CairnSparkApplicationOperator`, in the `cairn/airflow` image, created an application and followed it to `COMPLETED`, and failed on `FAILED` with the driver's error message. Executors had no identity.

## Iceberg v3 tables

Cairn Catalog keeps Iceberg format v3 tables:

- A table is created as v3 with the property `format-version=3`, or upgraded in place with
  `ALTER TABLE … SET TBLPROPERTIES ('format-version'='3')`. An upgrade cannot be undone, which is Iceberg's rule.
- The catalog understands the v3 types (`variant`, `timestamp_ns`, `timestamptz_ns`) and keeps the v3
  metadata: row lineage (`next-row-id`, each snapshot's first row id) and deletion vectors.

Spark 4 with Iceberg 1.10 (the `cairn/spark` image) writes v3 tables:

- deletes and merges with deletion vectors (`'write.delete.mode'='merge-on-read'`);
- row lineage (`_row_id`, `_last_updated_sequence_number`);
- `VARIANT` columns (`parse_json`, `variant_get`).

Spark's metadata tables (`t.snapshots`, `t.delete_files`) work too: loading a table in a namespace that does not
exist answers "no such table". Spark tries `<namespace>.<table>.<metadata table>` as a table before it reads the
metadata table, so it needs that answer.

**Readers.** What each engine can read depends on its version:

**Engines.** Every engine Cairn ships reads and writes v3 tables:

| Engine | Reads v3 | Deletion vectors | `VARIANT` | Writes v3 | Row lineage |
|---|---|---|---|---|---|
| Spark 4 / Iceberg 1.10 | ✅ | ✅ read and written | ✅ | ✅ | ✅ kept on rewrite |
| Trino 484 (the pinned Trino, governed SQL) | ✅ | ✅ read and written | ✅ read | ✅ | ✅ |
| Cairn's own writers (pyiceberg 0.12 with Cairn's v3 writers) | ✅ | ✅ applied on read | — | ✅ | ✅ new rows; rows a rewrite keeps take new ids |

So v3 tables, deletion vectors and `VARIANT` columns included, are queried through Cairn's governed SQL with its
masks and row filters. Trino 476 and older (Iceberg 1.9) fail a query on deletion vectors or `VARIANT` with
Trino's own error and never return deleted rows; the pin moved to 484 for v3.

Cairn's own writers (files and stream pipes, mirrors, SaaS syncs, synthetic tables, samples) write through
pyiceberg, which reads v3 but has no v3 manifest writers (0.12). Cairn adds them (`cairn.pipelines.iceberg_v3`,
applied only while pyiceberg lacks its own): their appends, deletes, upserts and overwrites work on v3 tables, and
row lineage follows the v3 spec. Each new data manifest takes the next row ids, manifests carried into a later
snapshot keep theirs, and each snapshot records its first row id and added rows so the catalog moves
`next-row-id` on. A rewrite by these writers (a delete, an upsert) writes the rows it keeps into new files, and
those rows take new ids; Spark and Trino keep ids when they rewrite. pyiceberg cannot upgrade a table to v3;
upgrade with Spark (or Trino). Tested on the real store and catalog (`tests/test_iceberg_v3_writes.py`).


## Table maintenance

Cairn keeps lakehouse tables healthy with Iceberg's own procedures, run through Trino on a schedule:
- **Compaction** (`optimize`): rewrites files smaller than `optimize_threshold` (default `128MB`) into bigger ones.
- **Snapshot expiry** (`expire_snapshots`): snapshots older than `expire_days`, so the data files only they referenced can go.
- **Orphan cleanup** (`remove_orphan_files`): files no snapshot references, older than `orphan_days`.

- **Policies** (`PUT /maintenance/policies/{scope}`, `MANAGE_SYSTEM_OPERATIONS`). `scope` is `*`, a schema or `schema.table`. The most specific one applies. Each has `optimize`, `optimize_threshold`, `expire_days`, `orphan_days` (null: skip), `every_hours` and `enabled`. Changes are audited (`maintenance.policy_set`).
- **Who runs it.** It runs on a run-as-user Trino connection (`CAIRN_MAINTENANCE_CONNECTION`, a named connection), as the Cairn user `CAIRN_MAINTENANCE_USER` (default `svc-maintenance`). Cairn's policy decides for it as for anyone, so give that user manage on the tables it maintains.
- **When.** The scheduler's leader (`cairn scheduler`) maintains each table whose policy is due. `POST /maintenance/run {"schema", "table"}` runs one now.
- **What happened.** Each operation is recorded with its outcome (`GET /maintenance/runs?table=`). One failing (say, refused) does not stop the next.
- **Automatic clustering.** A policy's `cluster` is `"auto"`, a list of up to four columns, or null (off); `cluster_depth` (default 4) is how much overlap is tolerated.
  - With `"auto"`, Cairn reads the table's finished queries of the last `CAIRN_CLUSTER_LOOKBACK_DAYS` (default 14) from Trino's query history (it needs Trino access on, `CAIRN_TRINO_ACCESS_KEY`), parses each, and counts the table's columns used in `WHERE`, `JOIN … ON` and `HAVING`, weighted by the bytes the query read. A column counts when at least 10% of the queries (and two) use it; the first two by weight are chosen. Booleans, binary, arrays, maps, rows and JSON are never chosen.
  - Cairn sets the Iceberg sort order (`sorted_by`), so every later write through Trino sorts its files. It then measures the **clustering depth**: on average, how many files a file's range of the first column overlaps (1 is perfectly clustered), from the files' column bounds. When the sort order changed or the depth is above `cluster_depth`, it rewrites every file in order (`optimize`).
  - Each check is a `cluster` operation in the runs, with what it did (for example `sorted by day, region (was unsorted); rewritten, depth 6.0 -> 1.0`). `GET /maintenance/clustering` shows each table's columns, how they were chosen, the evidence (queries, filter counts, bytes), its depth and when it was last rewritten. Table `cairn_table_clustering` and the policies' `cluster` columns are migration 0067.
- **Point lookups (`lookup`).** This is Cairn's counterpart to Snowflake's search optimization. A policy's `lookup` is `"auto"`, a list of up to five columns, or null (off).
  - The columns get Parquet Bloom filters (Trino's `parquet_bloom_filter_columns`). A query that looks a row up by value (`WHERE id = …`, `id IN (…)`) then skips every file that cannot hold the value, which min/max statistics cannot do for random keys such as UUIDs or order numbers. A Bloom filter is probabilistic: for a small share of absent values it answers "maybe" and the file is read anyway (Trino's writer sets its size; in Cairn's tests, 0 to 2 lookups in 10 for a missing id), so lookups get much cheaper on average, not every single one.
  - **`"auto"`** takes the columns that at least 10% of the table's recent queries (and at least two) look up by value with a literal, from the same query history as clustering. Up to three columns; join keys and column-to-column comparisons do not count.
  - Floating-point, boolean, binary and nested columns are refused: Bloom filters are for exact values.
  - When the columns change, every file is rewritten once (`optimize`), so the data already there gets the filters; later writes through Trino include them. Each check is a `lookup` operation in the runs, saying what it did. Migration 0108.
  - **Tested** on real Trino in `tests/test_trino_e2e.py`: `id` chosen from the query history; the table rewritten with nothing lost; a lookup of a missing id inside every file's min/max range scans 0 rows, against all rows for the same lookup on an unfiltered column; a `double` column refused.
- **Trino's floors.** Trino refuses retention under `iceberg.expire-snapshots.min-retention` and `iceberg.remove-orphan-files.min-retention` (7 days by default). Keep them unless you mean to allow less.
- **Tested** (`tests/test_table_maintenance.py`; against Trino and Cairn Catalog in `tests/test_trino_e2e.py`):
  - **Real tables.** Five small inserts became one file. Snapshots were expired to one. A stray file in the table's location was deleted. The rows were unchanged, every step was recorded, and the table was not maintained again before it was due.
  - **Logic.** The table-level policy beat the schema's, and the schema's beat `*`. A refused step was recorded and the others ran. Bad scopes, names and values were refused, and only operators could set policies.

## Table branches, tags and data diff

Iceberg tables in the lakehouse can have branches (refs that move) and tags (refs that stay). Cairn manages them under its policies, gates publishing on checks (write-audit-publish), and compares any two refs or snapshots.

- **Turning it on.** It needs the lakehouse catalog Cairn writes through (`CAIRN_PIPES_CATALOG`) and a Trino connection with `run_as_user` (`CAIRN_BRANCHES_CONNECTION`, by default the derived tables' or maintenance connection). The Trino catalog name is `CAIRN_DERIVED_CATALOG` (`lakehouse`).
- **Refs.** Tables are named `namespace.table` in these paths.
  - `GET /v1/tables/{table}/refs` lists branches and tags (`VIEW_ENTITY_PAGE`).
  - `POST /v1/tables/{table}/refs` with `{"name": "etl", "type": "branch" | "tag", "from": "main" | <tag> | <snapshot id>, "max_ref_age_days": 30}` makes one.
  - `DELETE /v1/tables/{table}/refs/{name}` removes one. `main` is never removed.
  - Making and removing refs needs `MANAGE_DATASET` on the table. Each is audited (`table.branch_created`, `table.tag_removed`, …).
- **Writing to a branch.** Engines write as usual: Spark `INSERT INTO t.branch_etl`, or pyiceberg `append(df, branch="etl")`, `overwrite(…, branch=…)` and `delete(…, branch=…)`. Read a ref with `FOR VERSION AS OF 'etl'`. It is the same table to Cairn's policies, masks and row filters.
- **Write-audit-publish.** `POST /v1/tables/{table}/refs/{branch}/publish` with `{"checks": ["SELECT id FROM {table} WHERE amount < 0"]}` (`WRITE_DATASET`).
  - Each check runs on Trino as the caller, against the branch (`{table}`). A check passes when it returns no rows.
  - When every check passes, `main` is fast-forwarded to the branch's head.
  - A failed check refuses the publish (409, with each check's outcome), and so does a `main` that moved on since the branch was made. In both cases `main` stays where it was.
  - Audited as `table.published` or `table.publish_refused`, with the checks.
- **Data diff.** `GET /v1/tables/{table}/diff?from=v1&to=etl&key=id&limit=20` (`VIEW_DATASET_PROFILE`).
  - **The schema:** columns added, removed and retyped.
  - **With `key`:** rows added, removed and changed by key. A changed row shows each column's `before_…` and `after_…`.
  - **Without a key:** rows only in one side (`EXCEPT ALL`).
  - Counts and up to 100 sample rows, as the caller on Trino: their masks and row filters apply to both sides.
- **Tested** in `tests/test_trino_e2e.py`, with real Trino and Cairn Catalog:
  - branch writes that leave `main` alone, and refs refused without `MANAGE_DATASET`;
  - keyed and plain diffs;
  - a failed check holding `main`, then a publish;
  - a stale branch refused;
  - a diff masked for a reader.

## Synthetic test data

`POST /v1/synthetic` makes a lakehouse table shaped like a real one, with none of its people in it, for development, tests and demos.

- **The request.** `{"source": "sales.orders", "target": "dev.orders_synthetic", "rows": 10000, "seed": 7, "replace": false}`. Reading the source needs `VIEW_DATASET_PROFILE` on it, and making the target needs `MANAGE_DATASET` on it. A target that exists is refused (409) unless `replace` is true. It uses the same settings as table branches (`CAIRN_PIPES_CATALOG`, the Trino connection).
- **Where the statistics come from.** They are computed on Trino as the caller, so the caller's masks and row filters apply: what they may not see does not shape the data either.
- **Sensitive columns.** No real value of a sensitive column is copied. A column is sensitive when it is classified sensitive (a PII glossary term and the like) or masked for the caller. Its values are made from its type and shape alone: an e-mail column gets `user123@example.com`. Its numbers stay between the 5th and 95th percentiles, never the extremes.
- **Categories.** A column with few distinct values (50 at most) keeps its values and their shares, but only values that occur at least `CAIRN_SYNTHETIC_MIN_COUNT` times (10). Rare values, which can identify people, never appear.
- **What is kept, column by column:**
  - the share of nulls;
  - numbers drawn from the source's percentiles;
  - date and time ranges;
  - text lengths;
  - unique columns stay unique.
  
  Relations between columns are not kept: each column is drawn on its own.
- **The answer and the record.** The answer says what each column was made from (`unique`, `categories`, `distribution`, `shape` or `null`). The same `seed` gives the same table. The table carries `cairn.synthetic.source`, and the run is audited (`synthetic.generated`).
- **Tested** in `tests/test_trino_e2e.py`, on real Trino and Cairn Catalog: no PII value copied, rare categories absent, sensitive extremes never reached, nulls, ranges, uniqueness and shares kept, and the same seed giving the same table.

## Pipes (automatic file loading)

A pipe loads the files that land in a Cairn Store prefix into a lakehouse table, once each. It names a `bucket`, a `prefix`, a `format` (`csv`, `json` lines, `parquet` or `xlsx`) and the Iceberg `target` (`namespace.table`). With `create_table`, the table is created from the first file.

- **Create** with `POST /v1/pipes` (`MANAGE_SYSTEM_OPERATIONS`). List, delete, and see each file's outcome (`GET /v1/pipes/{id}/files`). Changes are audited.
- **As files arrive.** Point a store event target (a webhook) at `POST /v1/pipes/events`, with the secret in `CAIRN_PIPES_WEBHOOK_SECRET`. Then add the bucket's `s3:ObjectCreated:*` rule for the prefix. Cairn takes only bodies the store signed with that secret (`X-Cairn-Signature`); anything else gets 401.
- **Sweep.** The scheduler's leader also lists each prefix, and `POST /v1/pipes/{id}/sweep` does it now. This loads anything no event announced.
- **Files as data (`format: files`).** The table is a directory of the prefix, kept as files come and go: one row per object. Query it in SQL, or point search and AI at it.
  - **Columns.** `key`, `path` (`s3://…`), `size`, `modified_ms`, `content_type`, `sha256`, plus the document's `text`, `pages` and `extract_error`.
  - **Text** is extracted from:
    - plain text, Markdown, CSV, JSON, XML, YAML and logs;
    - HTML (the visible text, without scripts or styles);
    - PDF (each page's text layer, with the `documents` extra); a scanned page, one without a text layer, is read by OCR;
    - images (PNG, JPEG, TIFF, BMP, GIF, WebP), by OCR, a page per frame;
    - Word `.docx` and PowerPoint `.pptx`.

    Other files get a row without text. Text is capped at `CAIRN_PIPES_MAX_TEXT` characters, and files over `CAIRN_PIPES_MAX_DOCUMENT_BYTES` (50 MB) are listed without it.
  - **OCR of scanned documents.** Tesseract reads the pages a scanner made: the images a PDF page draws when the page has no text layer (fewer than 16 characters), and image files. Cairn's image includes Tesseract with English (build argument `OCR_LANGUAGES`, for example `"eng deu fra"`, adds others).
    - **Settings.** `CAIRN_OCR` is `auto` (the default: on when Tesseract is installed), `on` (a document that needs OCR without Tesseract gets an `extract_error`) or `off`. `CAIRN_OCR_LANGUAGES` sets the languages (`eng`, or `eng+deu` for several; each needs its language data). `CAIRN_OCR_COMMAND` names the binary.
    - **Limits.** Each page has `CAIRN_OCR_TIMEOUT` seconds (60) and each document `CAIRN_OCR_DOCUMENT_TIMEOUT` (600) and `CAIRN_OCR_MAX_PAGES` pages (100). An image is read only up to `CAIRN_OCR_MAX_PIXELS` (60 million; a 600 dpi A3 scan is about 35 million), checked from its declared size before it is decoded.
    - **What a limit or a bad page does.** The page is skipped and named in the row's `extract_error`; the rest of the document is still read, and the pipe goes on.
    - Tesseract runs as its own process per page, reading the image from its standard input. It gets no file paths and no environment beyond `PATH`.
  - **Speech to text for audio and video.** WAV, MP3, M4A, AAC, OGG, Opus, FLAC, WebM, MP4, MOV, MKV and AVI files get what is said in `text`, and their length in whole seconds in `pages`.
    - Transcription runs on Cairn's own hosts (Whisper, through `faster-whisper`, in the `media` extra and the platform image), so recordings never leave the deployment.
    - **The model.** Cairn never downloads a model while loading data. Put one in `CAIRN_TRANSCRIBE_MODEL_DIR` first with `cairn transcribe-model download` (in Kubernetes, a volume through `extraVolumes` and `extraVolumeMounts`). That way hosts without internet access work the same.
    - **Settings.** `CAIRN_TRANSCRIBE_MODEL` names the model: `base` by default; `tiny.en` is fastest; `large-v3` is the most accurate. `CAIRN_TRANSCRIBE_LANGUAGE` sets the language (empty: detected). `CAIRN_TRANSCRIBE` is `auto` (on when `faster-whisper` is installed), `on` (a recording without it gets an `extract_error`) or `off`.
    - **Limits.** Each file gets at most `CAIRN_TRANSCRIBE_TIMEOUT` seconds (1800), and recordings longer than `CAIRN_TRANSCRIBE_MAX_SECONDS` (3600) are noted, not read. The length is read from the file's header before anything is decoded. `CAIRN_TRANSCRIBE_THREADS` (2) sets the CPU threads per file.
    - **Isolation.** Each file is transcribed in its own process, with no network (`HF_HUB_OFFLINE`), nothing but `PATH`, and a temporary home. A file that is not a recording, or that fails, gets the reason on its row; the pipe goes on.
    - **Tested** in `tests/test_pipes_e2e.py`, with the real store and catalog: spoken WAV, MP3 and MP4 files transcribed; a broken file noted; a recording over the limit refused.
  - **AI enrichment (`ai`).** A files pipe can ask Cairn's model about each file as it lands. `"ai": {"describe": true, "extract": {"invoice_number": "the invoice's number", "total": "the total amount due, as a number"}}` adds four columns:
    - `description` and `labels` (a JSON list of up to 10) for images: PNG, JPEG, GIF, WebP, TIFF and BMP.
    - `extracted`: the asked-for fields as a JSON object, for any document with text and any image. A field the file does not have is null. In SQL: `json_extract_scalar(extracted, '$.total')`.
    - `ai_error`: why a file was not enriched (no model, AI budget spent, model refused or failed). The pipe goes on.
    - **What is sent.** Images are scaled to at most `CAIRN_PIPES_AI_IMAGE_PIXELS` (1568) on the long side and sent as JPEG. The same pixel limit as OCR applies, checked before decoding. Text is cut to `CAIRN_PIPES_AI_MAX_TEXT` characters (100,000). Files with neither are not sent.
    - **The model and budget.** It is Cairn's model (`CAIRN_AI_*`: Anthropic, or a local Ollama model such as a vision-capable Llama, for data that must not leave the deployment). Calls count against the AI budget as the pipe's owner, feature `pipe_ai`, and show in the AI usage report.
    - Fields are lowercase names (up to 30), each with a description of up to 500 characters. `ai` is only for `files` pipes. Adding it to a pipe whose table exists adds the columns.
    - **Tested** in `tests/test_pipes_e2e.py`, with the real store and catalog and a recording stand-in for the model: an image described, labelled and read; a text file's fields extracted; a binary file left alone; a failing model noted on the row; usage recorded as the owner. Also tested in `tests/test_llm_images.py`, which checks each provider's image format.
  - **Changes.** A changed object replaces its row in one commit. A deleted object's row goes when the bucket's `s3:ObjectRemoved:*` event arrives, or at the next sweep.
  - **Tested** in `tests/test_pipes_e2e.py`, with real stores and events: text, HTML, PDF, Word and binary files, a change and a deletion by event, and a deletion found by the sweep. With Tesseract: a PDF with a text page and a scanned page, and a scanned image, read word for word; a page over the size limit named on the row while the rest is kept; and `CAIRN_OCR=on` without Tesseract reported.
- **Once each.** A file (key, size, modification time) is loaded once. It is recorded with its outcome. A file that fails, such as one with columns the table lacks, is recorded as an error; the next file still loads. A failed file is retried when it changes. Markers such as `_SUCCESS` are skipped.
- **Bad rows (`bad_rows`).** With `fail` (the default), a file with a value the table cannot take, such as `n/a` in a number column, is refused whole and recorded as an error.
  - With `quarantine`, those rows are set aside and the rest of the file loads. Each set-aside row is kept with its file, row number (from 0), the reason and the row as it was (the first 4 KiB, as JSON): `GET /v1/pipes/{id}/rejects`. The file's record shows `rejected`, the count.
  - Columns the table lacks still refuse the whole file: that is the file's shape, not a bad row. `quarantine` is not for `files` pipes, which have no rows to refuse.
  - Deleting the pipe deletes its set-aside rows. Fix the rows and land them as a new file to load them.
  - Tested in `tests/test_pipes_e2e.py`, with the real store and catalog: the same file refused whole by default, and loaded less its two bad rows with `quarantine`.
- **Excel (`format: xlsx`).** Each workbook loads its first sheet.
  - The first non-empty row is the header. Headers become column names ("Amount (EUR)" becomes `amount_eur`; a repeated header gets `_2`).
  - Each column is typed by its values: whole numbers, decimals, true/false, dates and times; a column whose values mix types is text.
  - Formulas load as the values Excel last saved. Rows that are entirely empty are skipped.
  - A file that is not a workbook is recorded as an error, and the next one still loads.
  - Tested in `tests/test_pipes_e2e.py` with a workbook written by openpyxl: three typed rows from the first sheet, an empty row skipped, a number in a text column kept as text, and a broken file recorded.
- **Tables and charts (`"ai": {"parse": true}`).** A files pipe adds `parsed` and `parse_error`: for each PDF and
  image, the JSON of its tables and charts (see [Document parsing](#document-parsing-tables-and-charts), without
  the text, which is in `text`); other files get null. Charts are read by Cairn's model as the pipe's owner; without
  a model, the text layer's tables still come.
- **Downstream.** Loaded tables are recorded as changed, so data-aware schedules (Airflow, dbt) see the new data.
- **Settings.** `CAIRN_PIPES_STORE` holds `{"endpoint", "access_key", "secret_key", "region"}`; give it a key that can only read the pipes' prefixes. `CAIRN_PIPES_CATALOG` holds the pyiceberg REST catalog properties: `uri`, `token`, and the warehouse's `s3.*` settings. Each is JSON, or a path to a JSON file.
- **Tested** with real Cairn Stores and Cairn Catalog (`tests/test_pipes_e2e.py`):
  - **Events.** Files loaded as the store's signed events arrived.
  - **Failures.** A file that did not fit was recorded as failed, and the next file still loaded.
  - **Once only.** A sweep afterwards loaded nothing again.
  - **Sweep.** Files under a prefix with no event rule were loaded by the sweep, and the markers were skipped.
  - **Signature.** A bad signature got 401.

## Document parsing: tables and charts

Cairn reads the tables and charts in PDFs and images as data (JSON), beside their text.

- **Tables from the text layer, with no model.** On a PDF page with a text layer, Cairn reads where each piece of
  text is drawn and finds runs of lines whose cells fall in the same columns. Each run is a table:
  `{"source": "text_layer", "header": [...], "rows": [[...]]}`, cells as text, a missing cell `null`. The same
  document always gives the same tables. Simple layouts work best; a two-column page of prose can look like a table.
- **Charts by the deployment's multimodal model.** A page that draws something (an image, or vector graphics such
  as bars and axes) is rendered to PNG by Poppler's `pdftoppm` (Cairn's image has it; without it, the images the
  page embeds are sent) and shown to Cairn's model (`CAIRN_LLM_PROVIDER`, `CAIRN_AI_MODEL`). It returns each chart
  as `{"type": "bar" | "line" | "pie" | "scatter" | "area" | "other", "title", "x_label", "y_label", "labels",
  "series": [{"name", "values"}]}` (values are numbers, `null` where unreadable), and tables drawn as pictures
  (`"source": "model"`). It is told which tables the text layer already gave, and copies of them are dropped. Image
  files (PNG, JPEG, TIFF, GIF, WebP, BMP) go to the model a frame at a time. Text in a picture is treated as data.
- **The result.** `{"pages": [{"page", "text", "tables", "charts"}], "page_count", "tables", "charts", "model",
  "model_pages", "errors"}`. Pages that draw graphics but were not read (no model, `charts: false`, the page budget
  or a model error) are listed in `errors`; the rest of the document is still parsed.
- **API.** `POST /v1/documents/parse` with `{"name": "report.pdf", "content": "<base64>", "charts": true, "save":
  true}`. With `charts` (the default) pages go to the model, so it needs `USE_AI_FUNCTIONS`, and calls are metered
  under the caller (task `document_parse`) and held to `CAIRN_AI_DAILY_BUDGET_USD` and `CAIRN_AI_USER_DAILY_TOKENS`.
  `charts: false` uses the text layer only and needs no privilege. Saved results (tables and charts, not the text)
  are kept for the person who asked: `GET /v1/documents/parsed`, `GET /v1/documents/parsed/<id>`,
  `DELETE /v1/documents/parsed/<id>`; operators (`MANAGE_SYSTEM_OPERATIONS`) see everyone's. Each parse is audited
  as `document.parsed` with counts only, never what the document says (migration 0128, `cairn_parsed_documents`).
- **In files pipes.** `"ai": {"parse": true}` (see [Pipes](#pipes-automatic-file-loading)).
- **Settings.** `CAIRN_PARSE_MAX_MODEL_PAGES` (20) pages shown to the model per document; `CAIRN_PARSE_DPI` (110)
  and `CAIRN_PARSE_IMAGE_PIXELS` (1,568 on the long side) for what it is shown, within OCR's
  `CAIRN_OCR_MAX_PIXELS`; `CAIRN_PARSE_DOCUMENT_TIMEOUT` (600 s); `CAIRN_PARSE_MAX_BYTES` (50 MB) for the API;
  `CAIRN_PARSE_RENDER_COMMAND` (`pdftoppm`).
- **Tested** in `tests/test_document_parse_e2e.py`: a generated two-page PDF with a table in its text layer and a
  drawn bar chart embedded as an image. With no model, the table came back exactly and the chart page was listed as
  not read. With Anthropic's SDK talking to a scripted stand-in for the model's API, through Cairn's metered
  provider: only the chart page was rendered by `pdftoppm` and shown (the bars in the picture), the chart's labels
  and values came back, the model's copy of the known table was dropped and a table only in the picture was kept,
  and the call was metered under the person. A PNG chart and a three-frame TIFF (held to a one-page budget) were
  read. Through the API: a person without `USE_AI_FUNCTIONS` was refused charts but got the text layer's tables,
  results were listed and read back by their owner only, and audits held no content. A files pipe on the real
  store and catalog filled `parsed` for the PDF and the image and left it null for a text file.

## Zero-copy Parquet: files registered as tables in place

Parquet files already in Cairn Store (written by another system, an export, a Spark job) become an Iceberg table in
Cairn Catalog without being copied. The table then reads the original objects and is governed and queried like any
other lakehouse table.

- **Register** with `POST /v1/lakehouse/parquet-tables` (`MANAGE_INGESTION`): `{"name": "events", "bucket":
  "landing", "prefix": "exports/events/", "target": "files.events", "every_minutes": 60}`. Every `.parquet`
  object under the prefix is taken; names starting with `_` or `.` (`_SUCCESS`, `.crc`) are skipped.
- **Where files may come from.** Only buckets in `CAIRN_PARQUET_BUCKETS` (comma-separated; unset, nothing is
  registered). The person must be able to read the files:
  - a governed volume's bucket (`cairn-vol-<name>`): a read or write grant on the volume;
  - any other bucket: `READ_FILES` on `urn:li:storagePath:<bucket>/<prefix>`, granted by policy like any
    privilege (for example a policy on `urn:li:storagePath:landing/exports/*`). No policy, no read.
- **Schema.** Read from each file's footer (no data is read). All files must have the same columns and types, or
  the registration is refused naming the file that differs. Files written with Iceberg field ids (another table's
  files) are refused.
- **Partitions** come from Hive-style directories, `…/region=EU/day=2026-11-01/part-0.parquet`: each `key=value`
  level is an identity-partitioned column, a whole number when every value is one and text otherwise
  (`__HIVE_DEFAULT_PARTITION__` is null). Every file needs the same keys in the same order, and a key may not also
  be a column in the files. Readers get the value from the table's metadata, so queries filter on it and skip files.
- **No copy.** The table is created with that schema and partitioning, a name mapping (the files have no field ids)
  and `gc.enabled=false`, and the files are added by reference with statistics from their footers, in one commit.
  Only metadata is written under the table's location. Because the table does not own the files, snapshot expiry
  and orphan removal never delete them; delete or replace files only through the registration (below).
- **Lineage.** The store prefix (`urn:li:dataset:(urn:li:dataPlatform:s3,<bucket>/<prefix>,PROD)`) is recorded as
  the table's upstream in the catalog, and the table is recorded as changed, so data-aware schedules see it.
- **New files.** `POST /v1/lakehouse/parquet-tables/<name>/refresh`, and the scheduler every `every_minutes`, add
  the files that appeared since, as the person who registered them (their read access is checked again each
  time). New files must match the registered columns and partition keys. A file that disappeared, or changed size
  in place, is reported in `problems` and left in the table (Iceberg files are immutable: write new data under a
  new name). A failed run keeps its reason in `error`.
- **Manage.** `GET /v1/lakehouse/parquet-tables[/<name>]`, `PUT …/<name>` (`every_minutes`, `enabled`),
  `DELETE …/<name>` (stops adding files; the table and the files stay). Audited: `parquet_table.registered`,
  `.refreshed`, `.updated`, `.deleted`. Kept in `cairn_parquet_tables` (migration 0128).
- **Settings.** `CAIRN_PARQUET_STORE` (default `CAIRN_PIPES_STORE`: a key that can read the registered buckets),
  `CAIRN_PARQUET_CATALOG` (default `CAIRN_PIPES_CATALOG`), `CAIRN_PARQUET_BUCKETS`, `CAIRN_PARQUET_MAX_FILES`
  (10,000 per registration). Trino reads the files with its own store key, so that key must reach those buckets.
  Engines that take narrowed credentials from Cairn Catalog for a table's location (Spark) cannot read files outside
  it: query these tables through Trino.
- **Tested** in `tests/test_parquet_tables_e2e.py` on the real store, catalog and Trino 484: two files under
  `region=EU/` and `region=US/` were registered (a `_SUCCESS` marker skipped); the table's manifests referenced the
  same object keys and only metadata was written; Trino grouped by the region taken from the paths, and the email
  column came back masked for ana and raw for the admin. Refused: someone without `READ_FILES`, a bucket not
  allowed, and files that disagreed on their schema. Two new files (one a new partition) were added on refresh, a
  third by the schedule, a file with another schema was refused, and a deleted file was reported.

## Sample datasets

A new tenant can start with a small documented lakehouse to try search, SQL, dashboards and Ask-your-data on before its own data arrives.

- **The retail set.** Three tables in the `samples` namespace: `customers` (2,000), `products` (120) and `orders` (20,000 over the last year).
  - The rows come from a fixed seed, so every tenant gets the same data, and no person is behind them (addresses are at `example.com`).
  - Each table and column is described in the catalog, and the tables carry the `sample-data` tag.
  - Three starter queries are saved, shared, on the lakehouse connection: revenue by region and month, best-selling products, and return rate by category.
- **Loading and removing.** `POST /v1/samples/retail`, `DELETE /v1/samples/retail` and `GET /v1/samples` (the sets and their state), all with `MANAGE_SYSTEM_OPERATIONS`; from a shell, `cairn samples load|remove|list`.
  - Loading again replaces the sample rows and the starter queries; nothing doubles.
  - Removing drops the tables, with their files, and the starter queries.
  - Each load and removal is audited.
- **For new tenants.** `CAIRN_TENANT_SAMPLES=retail deploy/hosting/tenant.sh create ...` sets the chart's `cairn.samples.onStart`, which sets `CAIRN_SAMPLES_ON_START`.
  - The scheduler's leader then loads the set once, when the tenant first starts, and retries until it has.
  - Once a set has been removed it is not loaded again.
- **Where it goes.** The tables are written through the catalog pipes write to (`CAIRN_PIPES_CATALOG`); without one, loading is refused (503). `CAIRN_SAMPLES_TRINO_CATALOG` is Trino's name for that catalog (`lakehouse`), and `CAIRN_SAMPLES_CONNECTION` is the connection the starter queries use (`lakehouse`).
- **Tested** with the real store, catalog and Trino (`tests/test_trino_e2e.py`).
  - Trino read 20,000 orders, and the starter queries' revenue matched the generated rows.
  - The tables and columns were documented, and reloading kept 20,000 rows and three queries.
  - Someone without the right was refused (403).
  - Removing dropped everything, and the on-start load brought nothing back. On a tenant that never had the set, it loaded it exactly once.

## Vector search on lakehouse data

A vector index lets people search a lakehouse table's text by meaning: support articles, tickets, product descriptions, or the text of a files pipe.

- **Define** an index with `POST /v1/vectors` (`MANAGE_INGESTION`): `name`, `source` (`namespace.table`), `key` and `text` (the key and text columns), optional `chunk_chars` (1,500) and `every_minutes`.
- **Embedding.** Cairn splits each row's text into overlapping chunks and embeds them with the deployment's embedding model, the same one search by meaning uses (`CAIRN_EMBEDDINGS`; Ollama in your VPC, or the built-in hashing embedder). The vectors are kept in a lakehouse table of their own, `<source>__<index>_vectors` (key, chunk, text, hash, embedding, model).
- **Kept in step.** Run `POST /v1/vectors/<name>/refresh`, or let the scheduler run it every `every_minutes`. Changed rows are embedded again, new ones added and removed ones dropped. Unchanged rows are not re-embedded; a model change re-embeds everything. Each refresh is one commit.
- **Search.** `GET /v1/vectors/<name>/search?q=...&k=10` returns the closest chunks with their row key, text and cosine score.
  - The caller needs read access to the source table.
  - It is refused when a mask or row filter applies to them there, because the index holds the raw text.
  - Each API replica keeps one index in memory per table version.
- **Approximate search (IVF).** `ann` on an index (in `POST` or `PUT`):
  - `{"lists": 256, "probes": 8}` sets the index's shape.
  - `"auto"`, the default, uses IVF once the index has `CAIRN_VECTORS_ANN_MIN` chunks (20,000), with about √n
    lists and a sixteenth of them probed (at least 4).
  - `false` keeps search exact.

  Each refresh trains cluster centres on a sample of the vectors (spherical k-means), assigns every chunk to its
  nearest centre, and stores that index beside the vectors (`<vectors table>/cairn-ann/<snapshot>.npz`, numeric
  and text arrays only) for the snapshot it was built from. A search scores the query against the centres, then
  compares it only with the chunks in the `probes` nearest lists. More probes find more true neighbours at more
  cost. If the stored index isn't for the table's current snapshot (another writer, a failed build), the search
  is exact and still right. Each result says which way it was searched (`method`, and `searched` for IVF).
- **Change or remove** an index with `PUT /v1/vectors/<name>` (key, text, chunking, schedule, enabled; a changed key, text or chunking is embedded again on the next refresh) and `DELETE /v1/vectors/<name>`, which also drops its vectors table, since it holds the raw text. An index keeps its source: to index another table, create another index.
- **Settings.** `CAIRN_VECTORS_CATALOG` (default: the pipes' catalog, `CAIRN_PIPES_CATALOG`).
- **Tested** in `tests/test_vectors_e2e.py`, with the real store and catalog. Questions found the right articles. A refresh re-embedded only the changed and new rows and dropped the removed one. Someone without read access, and someone with a row filter, were refused.

- **Approximate search, tested.** 6,000 clustered vectors with 64 lists and 6 probes:
  - Recall@10 against exact search was 1.0 over 30 queries, while each query compared about a tenth of the chunks.
  - With a stored index for another snapshot, search was exact.
  - A refresh after the table changed built the index again for the new snapshot, and turning `ann` off made
    search exact on the next refresh.

### Answers with sources

`POST /v1/vectors/<name>/answer` `{"question": "...", "k": 8}` answers a question only from an index's passages and
says which ones it used.

- **Retrieved as the caller.** The `k` closest chunks (1-20, default 8) are found with the same checks as search:
  read access to the source table, and no mask or row filter there (the index holds the raw text). Optional
  `min_score` drops chunks below that cosine score. If no chunk is left, Cairn answers that it cannot, without
  calling the model.
- **The model.** The chunks are numbered and sent, as data, to the deployment's model (`CAIRN_LLM_PROVIDER`,
  `CAIRN_AI_MODEL`) through Cairn's server-side model path. The model must return each sentence with the numbers of
  the passages that state it, and is told not to follow instructions found inside passages. With no model
  configured, the endpoint answers 503.
- **What comes back.** `answer` (the sentences, each followed by its citation numbers), `sentences`
  (`{text, sources, supported}`), `citations` (`[{n, key, chunk, text, score}]`: only the passages actually cited),
  and `unsupported` (the positions of sentences with no valid citation, so a reader can tell grounded text from
  the rest). A citation to a passage that was not given is dropped.
- **Refusals.** When the model says the passages do not answer the question, or it cites none of them, Cairn
  returns `answered: false`, no answer, and a `refusal` that says so.
- **Metered and budgeted** like Cairn's other model calls: under the caller, as feature `vectors.answer`, held to
  `CAIRN_AI_DAILY_BUDGET_USD` and `CAIRN_AI_USER_DAILY_TOKENS` (429 when used up, before the model is called).
- **Audited without content.** Each answer records `vector_index.answered` with the index, `k`, how many passages
  were retrieved and cited, how many sentences were unsupported, the model and its token counts. The question,
  the answer and the passages are not recorded.
- `CAIRN_VECTORS_ANSWER_CONTEXT_CHARS` (60,000) caps the passage text sent per question; the closest come first.
- **Tested** in `tests/test_vectors_answer_e2e.py`, with the real store and catalog, a real lakehouse table, and
  Anthropic's SDK talking to a scripted stand-in for the model's API. A refund question was answered from the
  refund passage alone; a sentence citing a passage that was never given, and one citing nothing, were flagged. A
  question the passages do not answer, and an answer that cited only made-up passages, were refused. People without read access or under a row filter were
  refused before the model was called. Calls were metered under the caller, refused at the token allowance, and
  audited without the question, answer or passages.

### Search from SQL: `ai_search`

With Cairn's Trino functions plugin installed, a query searches an index itself:

```sql
SELECT r.key, r.text, r.score
FROM UNNEST(ai_search('help', 'how do refunds work', 5)) AS r(key, chunk, text, score)

-- joined back to the source table, governed like any other read
SELECT a.*, r.score
FROM UNNEST(ai_search('help', 'forgot password')) AS r(key, chunk, text, score)
JOIN lakehouse.support.articles a ON a.id = r.key
```

- **Shape.** `ai_search(index, query)` or `ai_search(index, query, k)` (1-100, default 10) returns
  `array(row(key varchar, chunk integer, text varchar, score double))`, closest first. It is a scalar function
  returning an array (Trino's plugin functions cannot be table functions), so read it with `UNNEST`. It is not
  deterministic: Trino calls it each time it is evaluated, so put it in `FROM UNNEST(...)` rather than in a
  per-row expression.
- **As the person.** Trino calls Cairn (`POST /v1/trino/functions/ai-search`, with Trino's access key in a header)
  naming the query's user, and Cairn searches as them with the same refusals as `GET /v1/vectors/<name>/search`:
  read access to the source table, and no mask or row filter there. Cairn's own identities (the catalog crawler,
  clean rooms, installed apps, share recipients, checked queries) are refused. A refusal fails the query with
  Cairn's reason (`Cairn: searching help needs read access to …`).
- **Metered.** Each search is held to the daily AI budget and the person's token allowance
  (`CAIRN_AI_DAILY_BUDGET_USD`, `CAIRN_AI_USER_DAILY_TOKENS`) and recorded in AI usage under the person (task
  `sql_ai_search`, the query's estimated tokens). `CAIRN_AI_SEARCH_USER_DAILY` caps searches a person a day.
- **Install (Helm).** `cairn.trino.functions.enabled: true`. An init container from `cairn/trino-functions`
  (`docker/trino-functions.Dockerfile`; `deploy/build-images.sh` builds it as `trino-functions`) copies the plugin
  into `/usr/lib/trino/plugin/cairn-functions` on the coordinator and workers, and Trino gets `CAIRN_API_URL`.
  The plugin is compiled inside the Trino image against its own SPI (`plugins/java/cairn-trino-functions/build.sh`),
  so rebuild the image with each Trino version (`TRINO_IMAGE`). Without the URL or key on Trino, every call fails.
- **Tested** in `tests/test_ai_search_e2e.py`: a real Trino 484 with the plugin built from source, the index's
  vectors in the real store and catalog. Ana's query returned the same chunks as the API, joined back to the source
  table, and was metered under her; someone without read access, someone with a row filter, an unknown index, a k
  out of range and Cairn's crawler identity were refused with the reason; the daily cap stopped her seventh search;
  the endpoint answered only with Trino's key.

## Streams (Kafka and change data capture)

A stream writes a Kafka topic into a lakehouse table continuously. Define it with `POST /v1/streams` (`MANAGE_SYSTEM_OPERATIONS`): `name`, `topic`, `format`, `target` (`namespace.table`), and optionally `mode`, `key`, `create_table`, `evolve`, `start` (`earliest` or `latest`), `batch_rows` and `batch_seconds`.

- **Formats.**
  - `json`: one JSON object per message, one row. In `append` mode, rows are added. In `upsert` mode, a row replaces the row with the same `key` columns; a tombstone whose Kafka key is a JSON object of the key columns deletes that row.
  - `debezium`: Debezium change events, with or without the schema envelope.
  - **Avro** (`encoding: avro`): messages in the Confluent wire format (a magic byte, the schema's id, the Avro body), as Debezium's Avro converter and Confluent serializers write them. Schemas come from a Confluent-compatible Schema Registry (`CAIRN_STREAMS_SCHEMA_REGISTRY`: `{"url", "username", "password", "ca_file"}`; in Helm, `schema-registry.json` in the streams Secret) and are kept once read. Avro decimals and timestamps arrive as such. A message in another format, or naming a schema the registry does not know, is set aside; a registry that cannot be reached stops the batch, which is retried. Protobuf and JSON Schema messages are not read yet. Creates, updates and snapshot reads write the `after` image; deletes remove the row. The table follows the source database. Always `upsert`.
  - `http`: no Kafka. Producers post rows to the table over HTTPS (`POST /v1/ingest/<name>/rows` with `{"channel", "offset", "rows": [...]}`), up to 10,000 rows a request; `append` or `upsert`, as for `json`.
    - Each request is one commit, and its snapshot records the channel's next offset. Name one channel per producer and send the channel offset of the first row: a retried request then writes nothing twice, and an overlap writes only the new rows. A gap is refused (409).
    - `GET /v1/ingest/<name>/channels/<channel>` says where a producer should resume.
    - Without `offset`, rows are added at the channel's offset (at least once).
    - Posting needs `INGEST_STREAM` on `urn:li:stream:<name>` (or `MANAGE_INGESTION`), and the stream must be enabled. One writer per channel at a time.
    - Rows that do not fit are set aside like messages, with the channel in the reason. HTTP-only deployments set `CAIRN_STREAMS_ENABLED=true` without `CAIRN_STREAMS_KAFKA`.
    - Tested in `tests/test_ingest_http_e2e.py`, with the real store and catalog: retries, overlaps, gaps, separate channels, rejects, upserts and who may post.
- **Exactly once.** Each batch is one Iceberg commit, and its snapshot records the next Kafka offset of each partition. A worker taking over a partition starts from there. So if a worker dies between the table commit and the Kafka offset commit, nothing is written twice. Kafka's own committed offsets are the fallback once those snapshots have been expired.
- **Bad messages.** A message that cannot be a row is set aside with its offset and the reason (`GET /v1/streams/{id}/rejects`), and the stream goes on. Examples: not JSON, an unknown operation, no key, or a column the table lacks. With `evolve`, new columns are added to the table instead.
- **Failures.** A batch the table refuses, such as a value that does not convert, stops the stream. The error shows on its status and the batch is retried with back-off. Nothing is skipped silently.
- **Bad rows (`bad_rows`).** With `quarantine` instead of `fail` (the default), a row the table refuses is set aside with the bad messages, with its offset and the reason, and the rest of the batch commits. In an upsert stream the refused change leaves its key alone: the row already in the table stays. For HTTP streams, `rejected` in the response counts them. Tested in `tests/test_streams_e2e.py`, with the real store and catalog: append and upsert, and a retried request that files nothing twice.
- **Transforms (`transform`).** One SQL `SELECT` over `batch` (the batch's incoming rows; nested objects are
  structs, so `geo.country` works) reshapes each micro-batch before it lands, in the same commit, so transformed data
  is in the table within seconds instead of waiting for a derived table's schedule. For `json` and `http` streams.
  ```sql
  SELECT id, lower(email) AS email, CAST(amount AS DECIMAL(12, 2)) AS amount, CAST(ts AS TIMESTAMP) AS ts
  FROM batch WHERE status <> 'test'
  ```
  - **What it may do.** Projection, renames, casts and row-level expressions and functions, and `WHERE`. Each output
    row comes from one input row, so joins, other tables, subqueries, `WITH`, `UNION`, aggregates, `GROUP BY`,
    `DISTINCT`, window functions, `UNNEST`, `ORDER BY` and `LIMIT` are refused. Other tables are refused because
    their access could not be checked here; join them in a derived table. Name every computed column with `AS`. In
    `upsert` mode the key columns must be in the output. Deletes (tombstones) are applied by key, untransformed.
  - **Checked when defined** (`POST` and `PUT /v1/streams`): the statement is parsed (sqlglot), checked against these
    rules, and bound in the engine against a row of NULLs, so a function or type that does not exist is refused then
    rather than on the first batch.
  - **Engine: DuckDB, in-process.** It runs the statement on the batch in memory, column-wise, with standard SQL
    casts, string, date and JSON functions, and nothing to deploy. Expression-only evaluation (sqlglot) would need
    Cairn to implement each function. Sending each batch to Trino would add a round trip and a query per batch.
    The engine is sandboxed: an in-memory database with file system and network access off, no extension
    loading, one thread, a memory limit (`CAIRN_STREAMS_TRANSFORM_MEMORY`, 512MB), and its configuration locked
    before the statement runs.
  - **Rows.** A row the `WHERE` drops is skipped. A row the statement fails on, such as a cast that does not
    convert, is set aside with the rejects (`GET /v1/streams/{id}/rejects`), with the engine's reason and the row
    as it arrived; the batch is split in halves until those rows are found, and the rest commit. A row the table
    then refuses follows `bad_rows`. A table the stream creates takes the transform's output types (a
    `DECIMAL(12, 2)` stays one).
  - **Offsets.** When nothing is left to write, an empty commit still records the batch's offsets, so an HTTP
    channel moves on. HTTP responses say how many rows were `rejected` and `filtered`.
  - **Tested** in `tests/test_stream_transform_e2e.py`, through an HTTP ingest stream into a real lakehouse table
    (real store and catalog) read back through a real Trino. Rows landed lowercased, cast and filtered, with the
    transform's column types, and were readable through Trino as soon as the request returned. A row whose amount
    did not convert was set aside with the reason while the rest committed. A retry wrote nothing twice. A request
    whose rows were all filtered still moved the channel on. Upserts kept their key. Joins, aggregates, other
    tables, file reads, unnamed columns, unknown functions and a transform dropping the key were refused at
    definition. File access, writes and configuration changes failed inside the engine.
- **Lineage.** Give `source` (the dataset URN the topic carries, such as the Postgres table) and Cairn adds it to the target's upstream lineage, merged with what is there. If that write fails, the stream is not created.
- **Status.** `GET /v1/streams` shows each stream's status, rows written, lag and last commit. `POST /v1/streams/{id}/enabled` pauses or resumes a stream. Changes are audited. Written tables are recorded as changed, for data-aware schedules.
- **Workers.** `cairn streams` runs every enabled stream. Several replicas share each stream's partitions, through one Kafka consumer group per stream.
  - **Settings.** `CAIRN_STREAMS_KAFKA` holds the kafka-python consumer settings: `bootstrap_servers`, `security_protocol`, `sasl_*`, `ssl_cafile`. `CAIRN_STREAMS_CATALOG` holds the Iceberg REST catalog properties (default: `CAIRN_PIPES_CATALOG`). Each is JSON, or a path to a JSON file.
  - **Helm.** Set `cairn.streams.enabled`, with `existingSecret` holding `kafka.json` and `catalog.json`.
  - **Compose.** Use the `streams` profile, with `CAIRN_STREAMS_ENABLED=true` on the API.
- **Tested** with a real Kafka broker, Cairn Store and Cairn Catalog (`tests/test_streams_e2e.py`):
  - **CDC.** Debezium snapshot reads, creates, updates and deletes kept the table equal to the source, through later changes too. A message that was not JSON was set aside, and tombstones were ignored.
  - **Exactly once.** A worker whose Kafka offsets were deleted resumed from the table's snapshots: 8 rows, not 13.
  - **Avro** (Redpanda's Kafka and Schema Registry). Debezium-shaped Avro events, decimals included, kept the table equal to the source. A JSON message and one naming an unknown schema were set aside. An Avro stream was refused when no registry was configured.
  - **Columns.** A column the table lacked was set aside, and with `evolve` it was added.
  - **Lineage.** The source was added to the target's lineage, merged with what was there. When Cairn Core failed, nothing was created.
  - **Checks.** Bad definitions were refused, and the API was for operators only.

## Change data feed

A lakehouse table's row changes between two of its snapshots, for consumers that sync, audit or react to what changed.

- **`GET /v1/lakehouse/tables/<schema>/<table>/changes?since=<snapshot id or ISO time>`**, optionally with `until` (default: the current snapshot), `key` (columns, comma-separated) and `limit` (1,000; at most 10,000).
  - Each row comes back with `_change_type`: `insert` or `delete`.
  - With `key`, a deletion and an insertion with the same key are an update: `update_preimage` and `update_postimage`.
  - `until_snapshot` is the snapshot to pass as the next `since`.
  - The changes are net between the two snapshots: a row added and removed in between does not appear.
- **`GET …/snapshots`** lists the table's snapshots: id (also as a string, `id`, since a 64-bit id loses digits as a JavaScript number), parent, commit time, operation, rows (`records`; unknown when the table has equality deletes), rows added and deleted, and which one is `current`.

### Snapshot browser and restore

A table's **Snapshots** tab (Iceberg tables) lists its snapshots, shows the table as it was at any of them, and restores one.

- **`GET …/snapshots/<id or ISO time>/rows`** (`limit`, 100; at most 1,000) is the table at that snapshot, read as the caller, so their masks and row filters apply. A time means the last snapshot committed at or before it.
- **`POST …/restore`** with `snapshot` (an id or an ISO time) and `reason` (required) makes that snapshot the table's current one again (Iceberg's `rollback_to_snapshot`). It runs on Trino as the caller, so it needs `MANAGE_DATASET` on the table, like any table procedure; others are refused by Trino and the table is unchanged. Restoring the current snapshot is refused (409).
- **Undoing.** Later snapshots stay in the table's history, so a restore is undone by restoring the snapshot that was current before it, until snapshot expiry removes them.
- **Audited** as `table.restored`, with the snapshot before and after, and the reason.
- **Tested** on real Trino and Iceberg (`test_a_person_browses_a_tables_snapshots_and_restores_yesterdays`). A table had a mistaken delete. The admin saw yesterday's snapshot with 3 rows and today's with 1, viewed yesterday's rows, and restored it with a reason, which was audited. Then they undid the restore. Bob saw only his EU rows of the old snapshot, and his restore was refused with the table unchanged. The UI test covers the list, the rows view, the reason and the exact 64-bit id sent.
- **Governed as a read.** Both snapshots are read as the caller, with ordinary time-travel queries on Trino, so their policies, masks and row filters apply. Someone masked sees masked values in the feed, and someone row-filtered sees only the changes to rows they may see at each snapshot. Trino's own `table_changes` is not offered, because its rows escape masks and row filters.
- **Cost.** The feed compares the two snapshots (`EXCEPT ALL` both ways): exact, but it reads both. Keep feeds frequent on large tables.
- **Settings.** `CAIRN_CHANGE_FEED_CONNECTION` is a run-as-user Trino connection (default: `CAIRN_DERIVED_CONNECTION`, then `CAIRN_MAINTENANCE_CONNECTION`). The catalog's name comes from `CAIRN_DERIVED_CATALOG`.
- **Tested** on real Trino (`tests/test_trino_e2e.py`). Each case, and what happened:

  | Case | Result |
  |---|---|
  | Inserts, a delete and an update since a snapshot | Came back as inserts, a delete and an update (pre- and post-image) by key, and as deletes and inserts without one |
  | The feed from its own last snapshot | Empty |
  | A masked reader | Saw only masked emails |
  | A row-filtered reader | Saw his EU rows leave and arrive |
  | A table the reader may not read | Refused |
  | `$files` | Refused to a reader |
  | `$snapshots` | Open to a reader |
  | `table_changes` on an append-only table | Ran for the admin, refused to a masked reader |

  The access decisions are also in `tests/test_trino_access.py`.

## Materialized views

A materialized view is a query's result kept as a lakehouse table, refreshed whenever a table it reads changes. Write it in the SQL editor (or `POST /sql/run`); Cairn runs these statements itself:

- **`CREATE MATERIALIZED VIEW [IF NOT EXISTS] [lakehouse.]<schema>.<name> [WITH (refresh_every_minutes = N)] AS <select>`.**
  - It is filled at once. If that first fill fails (for example, the query reads a table its author may not read), nothing is kept.
  - It refreshes when a table it reads changes, and also every `N` minutes when given.
- **`REFRESH MATERIALIZED VIEW <name>`** refreshes it now. **`DROP MATERIALIZED VIEW [IF EXISTS] <name>`** removes it and its table. Only its owner (or an operator) may do either.
- **What counts as a change.** Any commit to a table it reads, whoever writes it:
  - Cairn Catalog reports every commit to Cairn (`POST /v1/lakehouse/changes`, the catalog's service token only), so pyiceberg, Spark and Trino writes all count.
  - So do pipes, streams, dbt and lineage.

  The scheduler's leader refreshes due views in dependency order.
- **Fresh or stale.** `GET /v1/derived?kind=materialized_view` lists them, each with `fresh` (nothing it reads changed since its last good refresh) and `changed`, the tables that did.
- **Whose rows.** A view is a derived table of kind `materialized_view`: it refreshes as its owner, under their masks and row filters. Reading it is reading its table, under that table's policies, so give the table the access its contents call for.
- **Catalog settings.** Commits are reported when the catalog decides access with Cairn (`CAIRN_CATALOG_CAIRN_URL`, `CAIRN_CATALOG_CAIRN_TOKEN`). A report is retried for about a minute in the background; a commit never waits for it.
- **Tested:**
  - `tests/test_trino_e2e.py`, on real Trino: a view was created and filled. A change to its source made it stale and was refreshed on the next tick, and the next tick had nothing to do. `REFRESH` and `DROP` worked for its owner and were refused to another person. A view whose author may not read its source was not kept.
  - `tests/test_lakehouse_client_e2e.py`, with the real catalog: a pyiceberg commit reached Cairn's change log from Cairn Catalog, and only the catalog may report changes.

## Derived tables (incremental refresh)

A derived table is a SELECT kept as a lakehouse table and refreshed on Trino. Define it with `POST /v1/derived`: `name`, `sql` (one SELECT), `target` (`schema.table` in the lakehouse catalog), `mode`, and when it refreshes (`every_minutes`, `on_change`, or both).

- **Modes.**
  - `full`: `CREATE OR REPLACE TABLE … AS <sql>`, atomically.
  - `append`: adds the rows whose `watermark` column is above the table's current maximum.
  - `merge`: rows at or above the maximum `watermark` (all rows, without one) replace the rows with the same `key` columns; the others are inserted.

  The first run, and `POST /v1/derived/{id}/refresh {"full": true}`, builds the table from the whole query. Use that after changing the query's columns.
- **When.** The scheduler's leader refreshes each table every `every_minutes`. With `on_change`, it also refreshes when a table the query reads is recorded as changed: by pipes, streams, dbt, Spark, Trino writes or another derived table. Upstream derived tables refresh first, so chains finish in one tick. One refresh runs at a time per table (a lease in the database); another gets 409.
- **As its owner.** A refresh runs on Trino as the table's owner, so their access, masks and row filters apply, and Trino's lineage records it. Anyone may define one that runs as themselves. Only operators (`MANAGE_SYSTEM_OPERATIONS`) may define one that runs as someone else, and see everyone's. Others see only their own.
- **What happened.** Each run is recorded with its kind, trigger and outcome (`GET /v1/derived/{id}/runs`). Definitions are audited. Deleting a definition leaves the table itself.
- **Settings.** `CAIRN_DERIVED_CONNECTION` is a run-as-user Trino connection (default: `CAIRN_MAINTENANCE_CONNECTION`). `CAIRN_DERIVED_CATALOG` is the lakehouse catalog's name in Trino (default `lakehouse`).
- **Tested** on real Trino and Cairn Catalog (`tests/test_trino_e2e.py`; the logic in `tests/test_derived_tables.py`):
  - **Chain.** An append table and a full rebuild chained on it both refreshed after their source changed, in order and in one tick. The append added only the new rows, none twice.
  - **Merge.** A merge applied an update and inserted the new rows.
  - **Owner.** A table owned by a user without access to its source failed its run, recorded, and nothing was created.
  - **Checks.** Statements that are not one SELECT, a query reading its own target, and bad columns were refused. Owners saw and ran only their own. A running refresh blocked a second one.

### Slowly changing dimensions (SCD type 2)

`mode: "scd2"` keeps every version of each row. Give `key` (the columns that identify a row) and, optionally,
`tracked` (the columns whose changes make a new version; default: every non-key column).

```json
{"name": "customers_history", "target": "crm.customers_history", "mode": "scd2", "key": ["customer_id"],
 "tracked": ["segment", "tier"], "every_minutes": 60,
 "sql": "SELECT customer_id, name, segment, tier, email FROM lakehouse.crm.customers"}
```

- **The table** has the query's columns plus `valid_from`, `valid_to` (`timestamp(6) with time zone`; NULL while
  current) and `is_current`. The first run creates it with every row current.
- **Each refresh is one `MERGE`** on Trino, as the owner, so it commits or fails as a whole:
  - a key whose tracked columns changed has its current row closed (`valid_to` set, `is_current = false`) and a new
    current version inserted; the two meet exactly (the old `valid_to` is the new `valid_from`);
  - a new key is inserted; a key gone from the query has its current row closed; a key that comes back starts a
    new version;
  - changes to columns that are not tracked are written onto the current row in place (type 1 for those columns).
- **Idempotent.** A refresh with nothing changed in the query changes nothing, so a retried or repeated refresh is
  the same as one.
- **The history is the table.** A full refresh is refused once the table has its history columns (the run records
  why); drop the table to start again. Changing the query, `key` or `tracked` keeps the history: the next refresh
  compares against it. A column added to the query is not written until it is added to the table
  (`ALTER TABLE … ADD COLUMN`). The key must be unique in the query: duplicates fail the run.
- `scd2` takes no `watermark` (every row of the query is compared) and is for tables, not materialized views.

### Medallion layers

Tables that pipes, streams and derived tables write can be marked **bronze**, **silver** or **gold**.

- **Setting.** Add `"layer"` to a definition (`POST`/`PUT` on `/v1/pipes`, `/v1/streams`, `/v1/derived`), or use
  `PUT /v1/medallion/<namespace.table>` `{"layer": "gold"}` (`MANAGE_INGESTION`); `DELETE` removes it.
  `GET /v1/medallion` (optionally `?layer=gold`) lists layered tables; the definitions' own listings show `layer`.
- **In the catalog.** The table's dataset gets the tag `urn:li:tag:medallion.<layer>` (one medallion tag at a time;
  its other tags stay).
- **Gold is earned.** Marking a table gold is refused (409, with what is missing) unless it has an owner in the
  catalog, a description, and a data contract that is met or quality monitors whose latest evaluations all pass
  (at least one). A definition that asks for gold is checked before anything is written.
- **Staying gold.** `GET /v1/medallion` runs the checks again for every table (`gold_ready`, `problems`), so a gold
  table whose contract broke shows it. The layer itself is not removed automatically.
- Every change is audited (`medallion.set`, `medallion.removed`) with the previous layer. Without Cairn Core,
  bronze and silver work and gold is refused.
- **Tested** in `tests/test_scd2_medallion_e2e.py` on a real Trino with Cairn Store, Cairn Catalog and the Cairn API.
  An scd2 table built from a customer table took a tracked change (closed and a new version that met it exactly),
  an untracked change (in place), a removed key (closed), a new key and a returning key. Two more refreshes changed
  nothing, a full refresh was refused with the history intact, and changing the tracked columns kept it. The
  table's silver layer, set with its definition, showed as a catalog tag. Gold was refused while it lacked an
  owner, a description and a contract (also through the definition, and for a user without `MANAGE_INGESTION`), and
  granted once they existed and the contract was met. When the contract broke, the listing showed the table no
  longer qualified. A stream's bronze layer was set with its definition; an unknown layer, and gold without its
  conditions, were refused before the stream was created.

## Pipeline agent

The pipeline agent writes, fixes and ships derived tables from a goal in plain words, as the person asking. It needs
an LLM provider and derived tables (`CAIRN_DERIVED_CONNECTION`).

- **Write:** `POST /v1/pipeline-agent/tasks` with `goal`, `target` (`schema.table`), optional `datasets` (urns) and
  `checks` (`{"name", "sql"}`, each a SELECT over `candidate` that counts wrong rows; 0 passes). The model is given
  the tables the person may see (as Ask-your-data chooses them, or the datasets named) with their column names, and
  proposes a derived table (one SELECT, `full`, `append` by watermark or `merge` by key, `every_minutes` and/or
  `on_change`) and checks of its own, as structured output. It never sees rows.
- **Try, writing nothing:** each proposal is checked as a derived table would be, every table it reads must be one
  the person may see, it is planned (`EXPLAIN`) and previewed on Trino as the person (masks and row filters apply),
  a merge key must be unique (a check Cairn adds) and an append watermark a column, then every check runs over the
  result. What fails (Trino's error, redacted, or a check that found rows) goes back to the model, up to
  `CAIRN_PIPELINE_AGENT_ATTEMPTS` (3) tries; each try is kept as a step. The task ends `ready` or `failed`.
- **Fix:** `{"fix": "<derived table id>"}` (its owner only, otherwise 404) gives the model the current definition and
  its latest failed runs' errors.
- **Ship:** `POST …/tasks/<id>/approve` (the task's owner, a `ready` task) creates the derived table as theirs, or
  updates the broken one and rebuilds it in full, refreshes it once on Trino as them, and runs the checks on the
  table it made: `shipped`, or `ship_failed` with the run's error or the failing checks (ask for a fix).
  The outcome carries the pipeline as a `cairn bundle` resource (`as_code`), to commit to git.
  `…/reject` closes a task. Tasks are visible only to their owner (`GET /v1/pipeline-agent/tasks`).
- Model calls are metered to the person (AI budget). Audited (category `lakehouse`): `pipeline_agent.drafted`,
  `pipeline_agent.shipped`, `pipeline_agent.rejected`.

Tested on real Trino and Cairn Catalog with a scripted model (`tests/test_trino_e2e.py`): a query naming a missing
column and one failing the person's check went back to the model and the third try was ready; trying wrote
nothing; the approved pipeline was built as its owner and its checks passed on the table; a plan reading a table
the person may not see was refused on every try; a broken pipeline was fixed from its failed run.

## Notebooks

Notebooks (Jupyter, JupyterHub, VS Code) work on the lakehouse as the person signed in, with `cairn-lakehouse` (`lakehouse-client/`).

- **`%load_ext cairn_lakehouse.notebook`** adds:
  - `%%sql`: the cell runs on Trino as the notebook's Cairn user, with their masks, row filters and refusals. The result shows as a table, a pandas DataFrame when pandas is installed. `--to df` keeps it in a variable. `--limit N` caps the rows (default 10000), and says when the cap was hit.
  - `%cairn whoami`, and `%cairn spark`: a SparkSession on the lakehouse as the same user.

  Without magics: `from cairn_lakehouse.notebook import sql`.
- **Sign-in.** The notebook's Cairn token is `CAIRN_TOKEN`, `CAIRN_TOKEN_FILE` or `CAIRN_WORKLOAD_TOKEN_FILE`. For Trino it gets a short Trino token from Cairn, renewed before expiry, so nothing long-lived reaches Trino. In JupyterHub, pass each person's Cairn token to their server as `CAIRN_TOKEN` (for example from the authenticator's auth state).
- **Settings.** `CAIRN_URL`, `CAIRN_TRINO_URL` (https), `CAIRN_TRINO_CATALOG` (default `lakehouse`) and `CAIRN_CA_FILE`, which is used as given, whatever `REQUESTS_CA_BUNDLE` says.
- **Lineage and usage** come from Trino, as for any client.
- **Not yet.**
  - A JupyterHub deployment in the chart with Cairn sign-in.
  - Notebooks as catalog assets.
  - MLflow behind Cairn sign-in, with models in the catalog (roadmap L4 and L7).
- **Tested** with Trino (`tests/test_trino_e2e.py`, an IPython shell). `ana`'s cells saw her masked emails and were refused a hidden table. `bob`'s saw only his rows. The cap was reported, and a token Cairn does not know got no Trino token.

## Scheduled notebook and Python jobs

A notebook or Python script can run on a schedule (or on demand) as its owner, in a container of its own, with its output kept and its failures told (`/v1/jobs`, `cairn.pipelines.jobs`).

- **Defining one.** `POST /v1/jobs` with `name`, `kind` (`notebook` with the notebook's JSON in `notebook`, or `python` with the script in `code`), and optionally `params`, `cron` (UTC), `timeout_minutes` (1 to 690, default 60), `recipients` (people) and `notify` (`failure`, the default, or `always`). `PUT /v1/jobs/{name}` changes it; `POST /v1/jobs/{name}/run` starts a run and answers at once (202, with the run's id; `?wait=true` answers when it has finished); `GET /v1/jobs/{name}/runs` and `/runs/{id}` show the runs, with the executed notebook (or the script's output) and the log.
- **Who.** Defining or running a job needs `RUN_JOBS`, granted like any privilege. Only the owner or an operator (`MANAGE_SYSTEM_OPERATIONS`) sees, changes, runs or deletes a job. The owner's right is checked again at every run, so a scheduled job stops when its owner loses `RUN_JOBS` (the run is recorded as failed and they are told).
- **As the owner.** Each run gets a Trino token minted for its owner, valid for the run's time limit plus five minutes, so what the job reads it reads under their policies, masks and row filters. The container sees `CAIRN_USER`, `CAIRN_JOB`, `CAIRN_JOB_RUN`, `CAIRN_JOB_PARAMS`, `CAIRN_TRINO_TOKEN`, `CAIRN_TRINO_URL` (`CAIRN_JOBS_TRINO_URL`) and, when Trino's certificate is private, `CAIRN_TRINO_CA_FILE` (from `CAIRN_JOBS_TRINO_CA_FILE`). A notebook also gets a first cell that sets `PARAMS`. No Trino configured, no token.
- **Isolated.** A job never runs in Cairn's process. `CAIRN_JOBS_RUNTIME` picks where; unset, jobs are refused (503).
  - `docker`: a new container per run from `CAIRN_JOBS_IMAGE` (default `cairn/jobs:latest`, built from `docker/jobs.Dockerfile`). It runs as uid 1000 with a read-only root, no capabilities, no new privileges, `CAIRN_JOBS_MEMORY` (2g), `CAIRN_JOBS_CPUS` (2) and 512 processes, the code mounted read-only, and `CAIRN_JOBS_DOCKER_NETWORK` if set. The token reaches it through an env file (mode 0600, outside the mount), never a command line.
  - `kubernetes`: a Job per run in `CAIRN_JOBS_K8S_NAMESPACE`, with the code in a ConfigMap and the token in a Secret, both deleted after the run (the Job's TTL cleans up anything left). The pod runs non-root with a read-only root, no capabilities, the runtime's seccomp profile, no service-account token and `CAIRN_JOBS_K8S_RESOURCES`. In the chart: `cairn.jobs.runtime: kubernetes`, `namespace`, `image`, `trinoUrl`, `trinoCa` (a ConfigMap key) and `resources`. The chart gives Cairn's account a Role in that namespace for Jobs, ConfigMaps, Secrets, pod lists and pod logs only. A ConfigMap holds at most 1 MiB, so on Kubernetes a notebook or script must be under that (elsewhere 5 MB); a larger one fails its run with Kubernetes' reason.
- **Time limit.** A run over `timeout_minutes` is stopped (the container removed, or the Job's deadline) and recorded as failed.
- **Kept and told.** Each run keeps its status, times, output and log, up to `CAIRN_JOBS_MAX_OUTPUT_BYTES` (2 MB; the log keeps its end). Runs older than `CAIRN_JOBS_KEEP_DAYS` (30) are removed. A failed run tells the owner and recipients (`job.failed`, with its last log line); `notify: always` also tells of successes (`job.finished`). Saves, deletes and runs are audited (`job.saved`, `job.deleted`, `job.run_succeeded`, `job.run_failed`).
- **Scheduling.** The scheduler (`cairn scheduler`) starts due jobs; each is claimed first, so one scheduler starts each run. Runs go to a pool of `CAIRN_JOBS_CONCURRENCY` (4) beside the scheduler's tick, so a long job never holds up monitors and alerts. A run still marked running 15 minutes past its time limit was lost in a restart: it is recorded as failed, and its owner told, once.
- **Tested.**
  - With Docker and the real image (`tests/test_jobs.py`): a notebook's output kept with its parameters, a failing script told with its last line, the container locked down (read-only root and code, no capabilities, no new privileges), the token absent from every command line, a run over its limit stopped, rights checked at save and at each run, a scheduled run claimed once, and a run lost in a restart failed and told.
  - On Trino (`test_a_scheduled_job_reads_the_lake_as_its_owner`): the admin's job counted every row; Bob's same job counted only his EU rows, over TLS against Trino's CA, and no token was kept.
  - On kind (`tests/test_jobs_k8s.py`): runs as Kubernetes Jobs with only the chart's Role, the pod locked down, a failure and a time limit recorded, and nothing (the token's Secret included) left behind.

## Flows (orchestration)

A flow is one DAG that mixes any kind of step, run as its owner (`/v1/flows`, `cairn.flows`). People build flows on the canvas (**Flows** in the navigation), agents through MCP or the API, and teams keep them in git as bundles. SQL workflows (`/v1/sql-workflows`) keep working: their steps now run through the same executors, and **Open as flow** (`POST /v1/flows/from-sql-workflow/{id}`) turns one into a flow draft.

- **The definition.** One document, `kind: flow`, in YAML or JSON: `name` (lowercase letters, digits, `-` and `_`), `description`, `runner` (`cairn`, the default, or `airflow`: see [Airflow, both ways](#airflow-both-ways)), `params` (each with a `type`: `string`, `int`, `float`, `bool`, `date` or `list`, a `default` and `required`), `triggers`, `concurrency`, `sla_minutes`, `notify`, `steps` and an optional `layout` (each step's place on the canvas, so the canvas and the code view round-trip). `POST /v1/flows/validate` checks a definition and answers every problem with its path (`steps[2].after: no step 'x' to come after`, cycles named step by step, unknown step types, templates that read a step that does not come before, a branch side that does not come after the branch); a YAML mistake gives its line. `cairn flows validate FILE` does the same without Cairn.
- **Steps.** Every step has a `name`, a `type`, `after` (the steps it comes after), `when` (`all_ok`, the default; `none_failed`, for joins after a branch; `all_done`; `any_failed`, for clean-up and alerts), `retries` (0-10) with `retry_delay_s` and `retry_backoff`, `timeout_s`, and `reads`/`writes` (tables, for lineage).
  - Data: `sql` (a saved query, or `sql` on a `connection`, through the SQL workspace), `derived` (refresh a derived table the owner owns), `check` (a measure, `rows` or a column, compared with `op` and `value`; a failure stops what depends on it), `pipe` (load the files a pipe has not loaded; needs `MANAGE_SYSTEM_OPERATIONS`), `stream` (wait until a stream has written what its topic held when the step started; parked meanwhile, looking again every `poll_s`; Kafka unreachable or the topic gone fails the step, and so does not catching up within `timeout_s`; a stream that does not exist is refused at publish).
  - Compute: `python` and `notebook` (inline `code`/`notebook`, or a defined `job`; the job runner, so `CAIRN_JOBS_RUNTIME` and `RUN_JOBS` apply), `spark` (`application`, `class_name`, `args`, `conf`; `spark-submit` in `CAIRN_FLOWS_SPARK_IMAGE` (a team's image built on `cairn/spark`, its application inside) with a lakehouse session issued to the owner; needs `CAIRN_FLOWS_CAIRN_URL` and `CAIRN_FLOWS_CATALOG_URI`; the container runs as the image's own non-root user, `CAIRN_FLOWS_SPARK_UID`, 185, since Spark's Hadoop needs a named user; no session for the owner fails the step before Spark starts), `dbt` (`command` run/build/test/seed/snapshot, `select`, `project_dir` in `CAIRN_FLOWS_DBT_IMAGE`, a project's image built on `cairn/dbt`; on Trino with the owner's token, in the catalog `CAIRN_FLOWS_TRINO_CATALOG`, `lakehouse` by default; Trino's certificate is always verified, with Cairn's CA or the system's; dbt writes only under `/tmp`), `airflow` (trigger `dag_id` with `conf` and, with `wait`, wait for its result).
  - AI: `agent` (ask an agent app with `input`; its answer is `steps.<name>.output`, and `steps.<name>.result` when it is a JSON object).
  - Control: `approval` (wait for one of `approvers` or a member of `groups` to approve in Cairn; it fails after `timeout_s`, three days by default), `wait` (`seconds`, or `until` a time of day or a date and time in the flow's time zone), `branch` (`condition` on earlier outputs, `then` and `else` naming the steps on each side; the other side is skipped), `for_each` (`items`, a list or an expression giving one, at most 1000; `step`, the step to repeat with `{{ item }}` and `{{ index }}`; `concurrency`, 1-32).
  - Notify: `notify` (`to`, `title`, `message`; the owner when `to` is empty).
- **Templates and expressions.** `{{ ... }}` in a step's text reads `params.<name>`, `steps.<name>.<field>` (an earlier step's output, its `status` and `error`), `run.id`, `run.logical_date`, `run.trigger`, `flow.name`, and in a for-each `item` and `index`. Expressions are a small, safe subset (literals, `and or not`, comparisons, arithmetic, field and index access, `len int float str bool round abs min max lower upper`), parsed and walked by Cairn, never evaluated as code. A step name with `-` is read whole (`steps.full-report.rows`); write subtraction with spaces.
- **Triggers.** `{type: schedule, cron, timezone}` (five fields, IANA time zone, at most every `CAIRN_FLOWS_MIN_INTERVAL_MINUTES` minutes, 5 by default), `{type: dataset, tables: [...]}` (a run when one of the tables changes: Trino's commits through its lineage, pipes, streams, dbt, derived tables and other flows all record changes; a flow cannot watch a table it writes), `{type: flow, flow, on: [succeeded | failed | finished]}`, and manual and API runs, always available. A flow that is turned off (`POST /v1/flows/{name}/enabled`) starts no scheduled or triggered runs.
- **Run as the owner.** Every step runs as the flow's owner, whoever started the run: SQL on Trino as them (their policies, masks and row filters), derived tables only theirs, jobs with their Trino token, agent apps as them. A run of a suspended owner (or one Cairn cannot check) is recorded as failed and starts nothing; a step whose token or decision cannot be had fails. Airflow runs under Airflow's own identity: Cairn passes `cairn_user`, `cairn_flow` and `cairn_run` in the DAG run's `conf`, and the step needs `RUN_JOBS`.
- **Who.** The owner, `editors` (edit the draft, run, cancel, rerun, backfill), `viewers` (see the flow and its runs) and admins (`MANAGE_POLICIES`); people or groups (`urn:li:corpGroup:...`), set with `PUT /v1/flows/{name}/sharing`. Only the owner or an admin publishes, shares, turns off or deletes, since a published version is what runs as the owner. Anyone else gets 404. Agents may list, read and (with their `write` scope) run flows, never define or publish them.
- **Drafts and versions.** A draft is saved as it is, valid or not (`PUT /v1/flows/{name}`), and shows its problems. `POST /v1/flows/{name}/publish` checks it completely, including what it refers to (the derived table is the owner's, the job, agent app, saved query, pipe and other flow exist and are theirs to use, `RUN_JOBS` for compute steps), and makes it the next version. Runs pin the version they started on; `GET /v1/flows/{name}/diff` shows what publishing would change (steps added, removed and changed, and settings). The owner may run the draft (`version: draft`) to try it.
- **Runs.** `POST /v1/flows/{name}/runs` with `params` starts one; `GET /v1/flows/runs/{id}` shows each step's status, attempt, times, output (at most `CAIRN_FLOWS_MAX_OUTPUT_BYTES`, 64 KB) and, at `/steps/{key}/log`, its log (the last `CAIRN_FLOWS_MAX_LOG_BYTES`, 64 KB). `/cancel` cancels, `/rerun` with `from_step` runs that step and everything after it again (without it, what did not succeed) while the rest keeps its outputs, and `POST /v1/flows/{name}/backfill` with `start`, `end` and `param` queues one run per day with its logical date. `concurrency.policy`: `queue` (the default: a run waits for the one going), `skip` (recorded as skipped) or `allow` (up to `max_active`). A run still going `sla_minutes` after it started tells the owner once (`flow.sla_missed`). Runs are kept `CAIRN_FLOWS_KEEP_DAYS` (90) days.
- **Approvals.** An approval step tells its approvers (`flow.approval_requested`) and waits without holding a worker; **Flows** lists what waits for you, and the run view has **Approve** and **Reject** (`POST /v1/flows/runs/{id}/steps/{step}/approve` or `/reject`, with a comment). Only a named approver or a member of a named group decides; each decision is audited.
- **Notifications.** `notify: {on: [failure, success, sla], to: [...]}` (by default failures and SLAs, to the owner): `flow.failed` lists each step's result, `flow.succeeded`, `flow.sla_missed`; notify steps send `flow.notice`. They go to the notification hub, so people's channels (Slack, Teams, email, push) apply.
- **Lineage and audit.** Steps that declare `reads`/`writes`, and derived-table refreshes, are recorded as OpenLineage runs of the job `cairn-flows://<flow>`/`<step>`: lineage in the catalog, and a change for each table written, which can start other flows. Creating, saving, sharing, publishing, running, cancelling, approving and deleting are audited (`flow.*`, category `flows`).
- **Several replicas.** A run is driven by one process at a time under a lease in the database (`CAIRN_FLOWS_LEASE_SECONDS`, 60, renewed while it runs); a process that stops loses its lease and the scheduler's next tick takes the run over (a step that was running is retried, or failed as interrupted). Waiting steps (approvals, waits, Airflow, streams) park the run until it needs looking at again. Scheduled and dataset starts and admission into `max_active` are claimed with conditional updates, so each run starts once whichever replica ticks. Runs started from the API run in the API process (a pool of `CAIRN_FLOWS_WORKERS`, 8, for the steps); the scheduler (`cairn scheduler`) starts scheduled and triggered runs and resumes parked and orphaned ones.
- **Compute steps' logs.** When a Python, notebook, dbt or Spark step fails, the end of what it printed (dbt's own report, Spark's error) is in the step's log, so the reason (Trino's `Access Denied`, the catalog's refusal) is a click away. The owner's tokens are never in it.
- **Airflow steps.** `CAIRN_AIRFLOW_URL` (Airflow's base URL), `CAIRN_AIRFLOW_API` (`v2` for Airflow 3, the default, or `v1` for Airflow 2), and a bearer token in `CAIRN_AIRFLOW_TOKEN_FILE`, or `CAIRN_AIRFLOW_USERNAME` with `CAIRN_AIRFLOW_PASSWORD_FILE` (Basic for v1; for v2 Cairn gets a token at `/auth/token`); `CAIRN_AIRFLOW_CA_FILE` for a private CA. Secrets are read from files, never from the command line. Without `CAIRN_AIRFLOW_URL` an Airflow step fails with that message, so the flow stops rather than skip work silently. The step polls every `poll_s` (30 s) up to its `timeout_s`.
- **Limits.** 200 steps (`CAIRN_FLOWS_MAX_STEPS`, up to 1000), 512 KB per definition, 50 parameters, 20 triggers, 1000 for-each items, retries 0-10, a step's time limit up to 7 days, a backfill up to 366 days.
- **As code.** Flows are a registry kind: in a bundle, `flows: [{name, spec: <the definition without its name>, enabled}]`, and in Terraform `cairn_flow` (`name`, `spec = jsonencode({...})`, `enabled`). `cairn bundle plan` compares the spec with the published version (drift shows as an update), `apply` saves and publishes them, and `--prune` deletes those the bundle made and no longer declares. `cairn flows deploy FILE` creates or updates and publishes one flow; `cairn flows run NAME --param k=v --wait` runs it and exits with its result; `cairn flows logs RUN [--step KEY]` shows a run or a step's log; `cairn flows list`. They read `CAIRN_URL` and `CAIRN_TOKEN` (or `CAIRN_TOKEN_FILE`).
- **For agents.** MCP tools `list_flows`, `run_flow` (scope `write`) and `get_flow_run`, as the caller: an agent sees and runs only what the person it acts for may.
- **The canvas.** Pan, zoom, a minimap, snap to a grid and auto-layout; steps from a palette grouped as Data, Compute, AI, Control and Notify (drag, or press Enter to add after the selected step); drag from a step's right handle onto another to make it come after it (a cycle is refused); a form per step type with problems shown as you type; multi-select (Shift-click or Shift-drag), Delete, Ctrl+Z / Ctrl+Shift+Z, Ctrl+C / Ctrl+V, arrow keys, F to fit. The **Code** tab is the YAML, editable both ways. **Run** colours each step by its status (edges into running steps move), and a step's output, log, **Rerun from here** and approval buttons are a click away. **Publish** shows the diff first. Templates start a draft; the theme is light, dark or the system's.
- **Tested.**
  - The engine (`tests/test_flows.py`) on a real SQLite warehouse through the SQL workspace: order, branches, for-each, templates, retries, a failed check stopping its dependents, rerun from a step, cancel, time limits, approvals by people and groups, concurrency, a suspended owner refused, two engines on one database starting a scheduled run and a dataset run once, a run taken over after its driver died, versions, drafts and diffs, every validation error, YAML round trips, backfill, flow and SLA triggers, lineage, the API, bundles (plan, apply, drift, prune) and the MCP tools.
  - On Trino, Cairn Catalog, Cairn Store and Docker (`tests/test_flows_e2e.py`): a derived table refreshed on Trino as the owner, a check stopping the report on bad data and the owner and recipient told, a branch on a row count, an INSERT through Trino starting a flow, a Python step in a job container reading Trino with the owner's token, an approval over HTTP, and an owner without the grant refused by Trino.
  - In Chromium against a real Cairn (`tests/test_flows_ui_e2e.py`): a flow built from the gallery, steps added and linked with the mouse, saved, published after the diff and run with each step's status on the canvas; axe passes on the Flows page and its editor (`tests/test_accessibility_e2e.py`).
  - The engine steps against the real engines (pipelines job): a `stream` step on a real Kafka broker waiting, parked, until the stream's worker has written the topic's offsets into its Iceberg table and then letting the flow go on (the next step reads the rows on Trino), failing when it does not catch up in time and when Kafka cannot be reached (`tests/test_flows_engines_e2e.py`); a `dbt` step in a project's image on `cairn/dbt` building the seed and the model on Trino as the owner, and the same project as an owner without the grant refused by Trino with dbt's report in the step's log (same file); a `spark` step in an image on `cairn/spark` building an Iceberg table through Cairn Catalog as the owner with a lakehouse session issued to them, the same application as someone who may not see the schema refused, and no session or no image failing before Spark starts (`tests/test_flows_spark_e2e.py`); an `airflow` step starting a DAG in a real Airflow 3.3, waiting for it and going on, and failing with a failed DAG run or an unknown DAG (`tests/test_flows_airflow_e2e.py`).

### Airflow, both ways

Teams that run Airflow keep it: a flow can become an Airflow DAG, Airflow can be a flow's runner, and Airflow's own DAGs show in Cairn.

- **A flow as a DAG file.** `GET /v1/flows/{name}/airflow` (its published version; `?version=<n>` or `draft`), the **Airflow DAG** button on a flow, or `cairn flows export-airflow NAME --out dags/name.py` (`--file flow.yaml` works without Cairn). The file uses `cairn-airflow-provider` and holds no secret: tasks sign in with Airflow's `cairn` connection (`?conn_id=` to name another).
  - **What maps.** Each step is a task with the same id. `after` becomes `>>`. `when` becomes the trigger rule: `all_ok` is `all_success`, `none_failed` is `none_failed`, `all_done` is `all_done`, `any_failed` is `one_failed` (which fires as soon as one fails). Retries, `retry_delay_s`, back-off (Airflow doubles the delay) and `timeout_s` become `retries`, `retry_delay`, `retry_exponential_backoff` and `execution_timeout`. Parameters become typed `Param`s; a default that is a Cairn template (`{{ run.logical_date }}`) is left empty for Cairn to fill. A schedule becomes a `CronTriggerTimetable` in its time zone; table triggers become assets watched by `CairnDatasetChangedTrigger`; both together, `AssetOrTimeSchedule`. `concurrency.max_active` is `max_active_runs`.
  - **Native tasks** (`?operators=native`, the default): SQL with `sql` and a connection (`CairnTrinoOperator`), a check (`CairnCheckOperator`, new: the flow's measure, `op` and `value` on Trino), dbt (`CairnDbtOperator`), Spark with a Python application (`CairnSparkApplicationOperator`; its image, service account and URLs come from the worker's environment, `CAIRN_SPARK_IMAGE` and so on) and an Airflow DAG (`TriggerDagRunOperator`). They run as the connection's Cairn user, under that user's grants. A step stays native only when its templates are plain `{{ params.x }}` and no other step reads its output.
  - **Delegated tasks.** Everything else runs in Cairn, as the flow's owner, through `CairnFlowStepOperator`, with a comment in the file saying why: approvals, for-each, waits, derived tables, pipes, streams, notebooks, Python, agents, notifications, saved queries and steps whose outputs others read. A branch is `CairnFlowBranchOperator` (a `BaseBranchOperator`): Cairn evaluates the condition and Airflow skips the side not chosen, as Cairn does. `?operators=cairn` delegates every step.
  - **Said in the file.** What does not map: a trigger on another flow, a second schedule, `concurrency: skip` (Airflow queues instead), the SLA and notifications.
  - **A delegated task** asks Cairn to run its step (`POST /v1/flows/runs/{run}/steps/{step}/execute`) as the connection's user, who must be the flow's owner, an editor or an admin. Cairn runs it as the owner with the outputs of the steps before it, records it in the run history, and answers with its status; the task asks again until the step has finished, and each Airflow try runs it again. A DAG run Airflow started on its own is recorded as a run of the flow by its first delegated task (`POST /v1/flows/{name}/airflow-runs`, once per DAG run). Its native tasks show in that run when Cairn can read Airflow (below).
- **Airflow as a flow's runner** (`runner: airflow` in the definition).
  - **Deploying.** Publishing writes the flow's DAG (`cairn__<flow>.py`, every step delegated, importing `DAG` and `Param` from `airflow.sdk` on Airflow 3 and from Airflow 2's modules otherwise, tagged `cairn-flow:<flow>` and `cairn-flow-version:<n>`) into Airflow's DAG folder before the version is made: `CAIRN_FLOWS_AIRFLOW_DAGS=path:/opt/airflow/dags/cairn` (a volume Airflow mounts; written atomically) or `git:<git folder id>` (committed onto that folder's branch, for git-sync). Deleting the flow removes the file. Publishing refuses a runner-airflow flow when either the folder or `CAIRN_AIRFLOW_URL` is not set. A bad `CAIRN_FLOWS_AIRFLOW_DAGS` stops Cairn at start.
  - **Runs.** Cairn keeps the triggers (schedule, tables, other flows, people, agents), so the DAG has no schedule of its own. A run waits until Airflow has loaded that version (up to `CAIRN_FLOWS_AIRFLOW_DEPLOY_TIMEOUT_SECONDS`, 300), unpauses the DAG, starts a DAG run through the REST API (`dag_run_id` `cairn-<run>`, with `cairn_run`, `cairn_flow`, `cairn_user` and the parameters in its conf) and follows it every `CAIRN_FLOWS_AIRFLOW_POLL_SECONDS` (10): each step's status comes from its task instance (`success` ok, `failed` failed, `skipped` and `upstream_failed` skipped, `up_for_retry` retrying, `queued` and `running` running), and the run ends with the DAG run. Each task runs its step in Cairn as the owner, so the run-as-owner rule holds whoever the Airflow connection's user is. Cancelling marks the DAG run failed. Airflow unreachable for `CAIRN_FLOWS_AIRFLOW_UNREACHABLE_MINUTES` (30) fails the run.
  - **Airflow 2** (`CAIRN_AIRFLOW_API=v1`): Cairn signs in with Basic authentication (`CAIRN_AIRFLOW_USERNAME`, `CAIRN_AIRFLOW_PASSWORD_FILE`), so Airflow's `[api] auth_backends` must include `airflow.api.auth.backend.basic_auth`; DAG runs are made without a logical date (Airflow uses now), runs are listed newest first by `execution_date`, a DAG's source is read by its file token (Cairn asks for JSON), its schedule comes from `schedule_interval`, and its owners and tags from the DAG itself (Airflow 2's DAG details leave them empty). Airflow 2's `SequentialExecutor` runs one task at a time, so a runner-airflow flow there should not wait in an `airflow` step for another DAG; use the `LocalExecutor` or another one. Wrong credentials fail the call (401), never an empty answer.
  - **git-sync.** With `git:<git folder id>`, run git-sync beside Airflow's DAG processor (as the official chart does), its `--link` the directory Airflow reads; if git-sync's root is inside the DAG folder, put `.worktrees/` and `.git/` in the folder's `.airflowignore` so Airflow reads only the published link. A commit reaches Airflow at git-sync's next `--period`, and the run waits for that version (`CAIRN_FLOWS_AIRFLOW_DEPLOY_TIMEOUT_SECONDS`).
  - **Steps running in Cairn** hold a lease (`CAIRN_FLOWS_LEASE_SECONDS`), renewed while they run, so a step whose Cairn process stopped is started again when its task next asks. Approvals, waits and other waiting steps answer "waiting" and are looked at again when their time comes or a decision arrives.
- **Airflow's DAGs in Cairn** (`GET /v1/airflow/dags`, `/v1/airflow/dags/{id}?run=`, the **Airflow** tab on the Flows page).
  - **Who sees what.** Airflow cannot be asked as the person (its sign-in through Cairn is a browser flow), so Cairn reads it with its service account (`CAIRN_AIRFLOW_*` above) and shows each person only the DAGs that are theirs: a DAG whose `owners` include their user name or one of their groups, a DAG tagged `team:<group>` or `group:<group>` with one of their groups, or a DAG tagged `cairn-flow:<flow>` for a flow they may see. Admins (`MANAGE_POLICIES`) see every DAG. When their groups cannot be read, only their user name counts; another person's DAG answers 404. Airflow not configured answers 503, unreachable 502, never an empty list. Agents are refused.
  - **What is shown.** The DAG's tasks on the flow canvas, read-only, with their order (`downstream_task_ids`), operator, retries and trigger rule; its last ten runs; and the chosen run's task states, refreshed while it runs.
  - **Convert to a Cairn flow** (`POST /v1/airflow/dags/{id}/convert`, with an optional `name`). Cairn reads the DAG's source (`GET /dagSources`) for each task's arguments. Every task must be `CairnTrinoOperator` (one statement, no `schema`), `CairnCheckOperator`, `CairnDbtOperator` (run, build, test, seed or snapshot, with `select`), `CairnSparkApplicationOperator`, `TriggerDagRunOperator` or `EmptyOperator` (dropped, its edges kept), with literal arguments whose templates are only `{{ params.x }}`, and a trigger rule that maps. The schedule (cron or a preset) and `params` carry over; SQL steps use `CAIRN_FLOWS_AIRFLOW_SQL_CONNECTION` (`lakehouse`). The result is a draft of the person's. Otherwise the answer is 409 and names each task that blocks and why (a `PythonOperator`, a mapped task, a computed argument, a DAG Cairn generated from a flow).
- **Tested** (`tests/test_flows_airflow.py`; `tests/test_flows_airflow_e2e.py` in the pipelines job, with the `cairn/airflow` image: its API server, scheduler, DAG processor and executor).
  - **The generator**, against golden files (native, delegated and runner), with every edge, trigger rule, retry, time limit, parameter, schedule and table trigger checked; deploying to a directory and to a git folder's branch in a real repository.
  - **In a real Airflow 3.3**: a flow with a check, dbt, Spark, a DAG trigger, a branch, an approval and a for-each exported and loaded by `DagBag` and the DAG processor with no import errors. A runner-airflow flow published (its DAG written into Airflow's folder), run from Cairn: Airflow ran each task, each task ran its step in Cairn as the owner (the connection's user an editor), the branch's other side was skipped in both, and Cairn's run history had every step's status. DAGs listed for their owner, a team member and an admin and hidden from others, with a run's live states. A DAG of provider operators converted to a draft (its `EmptyOperator` dropped, the schedule and retries kept), and one with a `PythonOperator` refused naming the task.
  - **The UI**: the Airflow tab's read-only canvas with live states and conversion, with vitest.
  - **Native tasks executed by Airflow 3.3**: a flow's exported DAG run by Airflow alone: its SQL (`CairnTrinoOperator`) and check (`CairnCheckOperator`) ran on Trino as the connection's user, its dbt task (`CairnDbtOperator`, in the `cairn/airflow` image) built the project, its `TriggerDagRunOperator` started another DAG and waited for it, its delegated step ran in Cairn as the owner; the table was in Trino, Trino's history named the connection's user, and Cairn's run history had one run with every task's status; a check that did not hold failed its task and Airflow skipped what came after. Not run to completion: an exported Spark task (`CairnSparkApplicationOperator`), which needs the Spark Operator on a cluster; its SparkApplication is checked against a real API server and the operator's CRD (`tests/test_spark_k8s_e2e.py`).
  - **git-sync**: the real `registry.k8s.io/git-sync/git-sync:v4.8.0` syncing a git folder's branch (a real repository, served over HTTP) into Airflow's DAG folder; Airflow loaded the DAG Cairn committed on publish, a run started in Cairn was carried out by Airflow with its history in Cairn, and deleting the flow took the DAG away again.
  - **Airflow 2.10** (`tests/test_flows_airflow2_e2e.py`, `apache/airflow:slim-2.10.5-python3.12`, the provider from this tree): the same runner flow over `/api/v1` with Basic authentication (branch skipped, an `airflow` step waiting for another DAG, each step run in Cairn as the owner), the DAGs listed and shown with their last run, a provider-only DAG converted, and wrong credentials refused.

## SaaS connectors

Salesforce and HubSpot objects load into governed lakehouse tables on a schedule, incrementally, and Google Sheets load whole (`/v1/saas-syncs`, `cairn.pipelines.saas`).

- **Defining one.** `POST /v1/saas-syncs` with `name`, `kind` (`salesforce`, `hubspot` or `google_sheets`), `object` (an API name: `Account`, `contacts`; for a sheet `<spreadsheet id>/<sheet name>`), `fields` (API names; for a sheet, column names), `target` (`namespace.table`), `secret_scope`, and optionally `cron` (UTC). `PUT …/{name}` changes it; `POST …/{name}/run` runs it now (202; `?wait=true` waits); `DELETE …/{name}` removes it. Only the owner sees and changes a sync.
- **Keys.** They live in a [secret scope](#secret-scopes) the owner may read, and are read as the owner at each run. They are never stored on the sync, sent to the browser, logged or audited. A required key the owner cannot read stops the run before any request.
  - Salesforce: `instance_url` and a connected app's `client_id` and `client_secret` (OAuth client credentials flow), or an `access_token`. API version `CAIRN_SAAS_SALESFORCE_API` (`v61.0`).
  - HubSpot: a private app's `access_token`, and optionally `base_url` (default `https://api.hubapi.com`).
  - Google Sheets: either a service account's JSON key as `service_account_json`, or an `api_key` for a sheet shared with anyone who has the link. Optionally `base_url` (default `https://sheets.googleapis.com`).
    - With a service account, share the spreadsheet with its address; viewer is enough.
    - Cairn signs a JWT with the key, for the read-only Sheets scope, and exchanges it at the key's `token_uri`.
  - All must be `https://`.
- **Incremental.** Each run asks only for records modified since the last one it loaded (Salesforce `SystemModstamp`, HubSpot `hs_lastmodifieddate`), oldest first, and upserts them by `id`: the batch's ids are deleted and the batch is appended, in one commit. The cursor advances after each committed batch (`CAIRN_SAAS_BATCH_ROWS`, 5,000), so a failed run resumes where it stopped. Records modified within the cursor's own second are asked for again and upserted again, so none is skipped. HubSpot's search stops at 10,000 records, so a longer run asks again from the last one it got. Changing a sync's object or table starts it from the beginning.
- **Google Sheets load whole.** A sheet's rows have no ids or modification times, so each run replaces the table with the sheet as it is now, in one commit. A deleted row goes, and an emptied sheet gives an empty table.
  - The sheet's first row is its header, turned into column names as for Excel ("Target (EUR)" becomes `target_eur`), and `fields` names the columns to load.
  - A field the sheet does not have fails the run with the columns it does have.
  - `id` is the row number in the sheet. Values are read unformatted (numbers as numbers) and stored as text.
- **The table.** It is created on the first run, with `id`, the fields (lowercased, as text, as the APIs return them), `_modified` and `_synced_ms`. A field added to the sync adds a column. Deleted records are not removed, because neither API's incremental query returns them.
- **Governed.** The table is an ordinary lakehouse table: Cairn's policies, masks and row filters apply to everyone who reads it. The owner needs `WRITE_DATASET` on it, checked when the sync is saved and at every run. Loads are recorded as dataset changes, so data-aware schedules follow.
- **Runs** go to a pool of `CAIRN_SAAS_CONCURRENCY` (2) beside the scheduler's tick, and each sync is claimed, so one scheduler runs it. A failed run is recorded on the sync (`last_status`, `last_error`), audited (`saas.sync_failed`) and told to the owner. Successful runs are audited as `saas.sync_run`, with the rows and the cursor.
- **Settings.** On when there is a catalog to load into: `CAIRN_SAAS_CATALOG`, or else `CAIRN_PIPES_CATALOG` (pyiceberg REST catalog properties), and secret scopes are on (`CAIRN_SECRETS_KEY`). Outbound HTTPS to the SaaS hosts must be allowed by your network policy.
- **Tested** with the real store, catalog and Trino, a real secret scope, and a local server answering the endpoints the connectors call (`tests/test_saas_e2e.py`). It is not a substitute for a run against a real Salesforce org or HubSpot account.
  - Salesforce: five accounts loaded over three pages. A change and an addition were then asked from the cursor and upserted, with no duplicates. Bob's row filter applied to the loaded table.
  - HubSpot: contacts loaded, a field added a column, and a failing API was recorded and told, with the cursor kept.
  - Google Sheets: a sheet read with a service account, whose signed JWT the test's token endpoint verified (signature, audience, read-only scope), and with an API key. An edited sheet, a deleted row and an emptied sheet were each followed exactly. A missing column and a key the sheet refused were reported as failed runs.
  - Refused: a sync the owner may not write, injection-shaped object and field names, keys the owner cannot read (no request was made), and another person's sync.

## Reverse ETL

Governed lakehouse tables sync out to SaaS apps (HubSpot, Salesforce) as their owner (`/v1/reverse-syncs`, `cairn.pipelines.reverse_etl`).

- **Defining one.** `POST /v1/reverse-syncs` with:
  - `name`, `source` (`schema.table` in the lakehouse), `kind` (`hubspot` or `salesforce`) and `object` (`contacts`, `Contact`);
  - `key`: `{column, field}`. The source column matches records in the app by `field`: a HubSpot unique property such as `email`, or a Salesforce external id field such as `Cairn_Id__c`;
  - `mapping`: `{source column: app field}`;
  - `secret_scope`, and optionally `cron`.

  `PUT`, `DELETE` and `POST …/{name}/run` (`?wait=true`) work as for the SaaS connectors. Only the owner sees a sync.
- **As the owner.** Each run reads the source on Trino as the sync's owner, so their row filters decide which rows leave. Sending data out of the platform needs `EXPORT_DATASET` on the source, granted like any privilege and checked at save and at every run.
- **Masked columns are never sent.** Before reading, the owner's masking plan for the source is checked. If the key or any mapped column is masked for them, the sync is refused and nothing is read or sent. Masked values would overwrite the app's real ones. If the plan cannot be decided, every column counts as masked.
- **Only changes.** Cairn keeps a hash of each row's key and mapped values, never the values, and a run sends only new and changed rows:
  - HubSpot: `batch/upsert` by the key property, 100 per call;
  - Salesforce: composite upsert by the external id field, 200 per call.

  Rows that disappear from the source are not deleted in the app. A source over `CAIRN_REVERSE_ETL_MAX_ROWS` (100,000) is refused.
- **Failures.**
  - Records the app rejects are counted and the first five reasons kept. The run is `partial`, and those records are sent again next run, since only accepted ones are remembered.
  - A failed or partial run is audited (`reverse.sync_failed`, `reverse.sync_run`, with counts and never values) and told to the owner.
- **Keys** come from a secret scope read as the owner, with the same keys and fail-closed rules as [SaaS connectors](#saas-connectors).
- **Settings.** On when there is a run-as-user Trino connection: `CAIRN_REVERSE_ETL_CONNECTION`, or else the change feed's or derived tables' connection. The lakehouse catalog's name comes from `CAIRN_DERIVED_CATALOG`. Secret scopes must be on.
- **Tested** on real Trino, against a local server answering the documented upsert endpoints (`tests/test_reverse_etl_e2e.py`). It is not a substitute for a run against a real account.
  - A segment synced to HubSpot. A second run with nothing changed sent nothing. A changed score and a new row went alone, and a record the app rejected was sent again, alone, on the next run.
  - Bob's syncs that sent, or were keyed on, his masked email were refused. Without email, only his EU rows left.
  - Losing `EXPORT_DATASET` stopped the next run before any request.
  - Salesforce upserted by external id.

## Governed volumes

A volume is a folder of files (landing files, models, documents, exports) that Cairn's grants govern, not only the store's bucket policies (`/v1/volumes`, `cairn.dataaccess.volumes`).

- **What one is.** A bucket of its own in Cairn Store, `cairn-vol-<name>`. Who may read or write it is decided by Cairn: its owner, and the people and groups it is granted to (`read` or `write`), with group membership from Cairn's identity.
- **Managing.** `POST /v1/volumes` (`name`, `description`) needs `MANAGE_VOLUMES`. The owner, or an operator (`MANAGE_SYSTEM_OPERATIONS`), grants (`PUT /v1/volumes/{name}/grants` with `principal`, a `urn:li:corpuser:…` or `urn:li:corpGroup:…`, and `access`), revokes (`DELETE …/grants?principal=`) and deletes it (`DELETE /v1/volumes/{name}`, only when empty). Only they see a volume's grants. To anyone without access, a volume does not exist (404).
- **Files through Cairn.** `GET /v1/volumes/{name}/files?prefix=` lists, `GET …/files/{path}` downloads, `PUT …/files/{path}` uploads (up to `CAIRN_VOLUMES_MAX_UPLOAD_BYTES`, 100 MB; larger files go through the credentials below), `DELETE …/files/{path}` deletes. A path is segments of letters, digits, `.`, `_` and `-`. The grant is checked on every call, and reads and writes are audited (`volume.file_read`, `volume.file_written`, `volume.file_deleted`).
- **From Spark and other engines.** `POST /v1/volumes/{name}/credentials` (`access`: `read` or `write`) checks the grant, then returns temporary store credentials of the `cairn-volumes` role, narrowed by a session policy to that one bucket, and to reading unless the person may write. Cairn creates the role in the store the first time. The credentials last `CAIRN_VOLUMES_CREDENTIAL_TTL` (900 seconds; 900 to 3600), so a revoked grant stops Cairn's API at once and the engine's credentials within that time. In Spark: `cairn_spark.use_volume(spark, "landing")` returns `s3a://cairn-vol-landing/` with the credentials set for that bucket (call it again to renew them in a long job), or pass `cairn_spark.volume_conf("landing")` as a session's `extra_conf`. Spark redacts them in its UI and event logs. Issued credentials are audited (`volume.credentials`).
- **Settings.** Cairn reaches the store at `CAIRN_STORE_URL` with `CAIRN_VOLUMES_ACCESS_KEY` and `CAIRN_VOLUMES_SECRET_KEY` (or `_FILE`), an admin key of the store, falling back to the store admin key the chart already gives the API. Without a key, volumes are refused (503). Engines get `CAIRN_VOLUMES_ENDPOINT` (default `CAIRN_STORE_URL`) as the store's address. Credentials need the store's STS (`cairn.store.secret.stsSecretKey` in the chart).
- **Tested.**
  - On a real Cairn Store (`tests/test_volumes.py`). A volume shared with a group was listed and read by a member and refused (404) to someone else. A read grant stayed read-only. The member's credentials read that bucket and were refused (AccessDenied) for writing to it and for another volume's bucket. Write credentials did a multipart upload. A revoked grant ended API access and new credentials. Only empty volumes were deleted, and bad names, paths and principals were refused.
  - In Spark (`test_a_volume_shared_with_a_group_is_read_by_them_alone_in_spark`). The member's job read the volume's files and could not write them until granted write. Then it wrote, and the file was read back through Cairn. The other person's job was refused before getting any credentials.

## Infrastructure as code

Everything a team configures in Cairn can live in git, reviewed like code, and so can the platform it runs on. An agent can do all of it through the same API and tools: read the registry (`GET /v1/iac/schema`), export what is there, change the files and plan; it proposes changes for a person to approve (see [Agents and infrastructure as code](#agents-and-infrastructure-as-code)).

- **The resource registry** (`cairn.iac.registry`, `cairn.iac.kinds`). Every configurable resource type is declared once: its API (the read path, and the create, update and delete calls as path templates), its id, the fields a bundle knows it by, and its fields (type, required, forces a new resource, computed, sensitive, write-only, default). Everything else is derived from it:
  - the bundle sections (`cairn bundle`);
  - the Terraform provider's resources and data sources: `python -m cairn.iac.terraform` writes `terraform-provider-cairn/internal/provider/zz_generated_kinds.go` and the provider README's resource list, and CI fails when either is out of date. The provider's resources are fixed when it is built, because Terraform asks for a provider's schema before it configures the provider (so before it could ask any Cairn), and `terraform validate` works offline;
  - `cairn export` and `GET /v1/iac/export`;
  - `GET /v1/iac/schema` (any signed-in person): the registry as JSON, for agents and the UI: Settings, **Infrastructure as code** lists every type with how many of each this workspace holds (`GET /v1/iac/inventory`, counted as the caller: a type that is off here, or not theirs to read, shows none) and exports the workspace; a resource's details in the platform hubs offer **Copy as Terraform** and **Copy as YAML**.
  - **Adding a resource type** is one `Kind(...)` entry (in `cairn.iac.kinds`, or in a module's `IAC_KINDS` listed in `registry.MODULES`), then `python -m cairn.iac.terraform`. A field that names another resource (`ref="secret_scopes.name"`) is exported as a Terraform reference, so Terraform creates them in order.
- **The resource types** (54 of them; each is a bundle section, a Terraform resource `cairn_<type>` and a data source):
  - **Identity and access**: `policies` (`cairn_policy`), `workspaces` (`cairn_workspace`), `personas` (`cairn_persona`), `purposes` (`cairn_purpose`), `consent_purposes` (`cairn_consent_purpose`), `agent_identities` (`cairn_agent_identity`, a service principal: its key is in the state as created and never read back), `workload_bindings` (`cairn_workload_binding`), `secret_scopes` (`cairn_secret_scope`), `secret_scope_acls` (`cairn_secret_scope_acl`), `secrets` (`cairn_secret`).
  - **Data and pipelines**: `connections` (`cairn_connection`), `maintenance` (`cairn_maintenance_policy`), `pipes` (`cairn_pipe`), `streams` (`cairn_stream`), `medallion_layers` (`cairn_medallion_layer`), `parquet_tables` (`cairn_parquet_table`), `mirrors` (`cairn_mirror`), `serving_tables` (`cairn_serving_table`), `document_sources` (`cairn_document_source`), `derived` (`cairn_derived_table`), `saas_syncs` (`cairn_saas_sync`), `reverse_syncs` (`cairn_reverse_sync`), `volumes` (`cairn_volume`), `volume_grants` (`cairn_volume_grant`), `external_catalogs` (`cairn_external_catalog`).
  - **SQL and BI**: `input_tables` (`cairn_input_table`), `queries` (`cairn_saved_query`), `sql_folders` (`cairn_sql_folder`), `metrics` (`cairn_metric`), `procedures` (`cairn_procedure`), `workbooks` (`cairn_workbook`), `dashboards` (`cairn_dashboard`), `alerts` (`cairn_sql_alert`), `metric_alerts` (`cairn_metric_alert`), `report_schedules` (`cairn_report_schedule`), `workflows` (`cairn_sql_workflow`).
  - **Quality and governance**: `quality_monitors` (`cairn_quality_monitor`), `slos` (`cairn_slo`), `retention_policies` (`cairn_retention_policy`), `automations` (`cairn_automation`), `forms` (`cairn_form`), `budgets` (`cairn_budget`), `cost_centres` (`cairn_cost_centre`), `marketplace_listings` (`cairn_marketplace_listing`), `notification_routes` (`cairn_notification_route`).
  - **Compute and jobs**: `jobs` (`cairn_job`: a notebook or a script, read back with `GET /v1/jobs?source=true`).
  - **AI and agents**: `vectors` (`cairn_vector_index`), `custom_agents` (`cairn_custom_agent`), `ai_gateway_routes` (`cairn_ai_gateway_route`), `model_deployments` (`cairn_model_deployment`), `agent_abuse_settings` (`cairn_agent_abuse_settings`), `agent_intent_settings` (`cairn_agent_intent_settings`).
  - **Workspace**: `git_folders` (`cairn_git_folder`), `data_apps` (`cairn_data_app`, read back with `GET /v1/apps?config=true`, operators only).
  - **Children and settings.** A secret, a secret scope's access entry and a volume's grant live under their parent: their Terraform id is `<parent>/<id>` (`finance-db/password`), and a bundle knows them by both. The agent abuse and intent settings always exist: there is one of each, destroying it only forgets it.
- **Not managed as code, and why.** The coverage gate (`tests/test_iac_coverage.py`) reads every route from the source and fails when an API collection that can be created (or set) and deleted is neither a resource type nor on its list with a reason:
  - *runtime state, not config*: assistants' artifacts, notes and scheduled tasks; bookmarks and follows; embedding tokens; comments; where a person files an item in a folder; semantic-model drafts; agents' memories; fine-tuning runs; app installs (a published package's version); on-demand databases; a table's branches and tags; a volume's files;
  - *owned elsewhere*: SCIM users and groups (the identity provider; declare access to groups with policies, personas and workspaces), the licence (a Kubernetes Secret), Cairn Store's own keys and bucket quotas (the store's administration), online feature stores and model monitors (set up with their feature table or model);
  - *a release or an act, not a declaration*: agent apps (each deploy uploads code and makes a version: `cairn agent deploy` from CI; their identity and access are an agent identity and policies), dashboard actions (part of a dashboard's design), agents on a share (part of a share);
  - *no update or delete API*: shares and their packages (a credential handed to an outside party once, under a step-up, revoked with `POST /v1/shares/{id}/revoke`), clean rooms (an agreement between two parties), tokenisation domains (created once: dropping one would make its tokens unrecoverable), manual persona grants (time-boxed: declare membership in the persona's `members`);
  - *no API*: warehouses (Helm values `cairn.trino.warehouses`; who may use one is a policy granting `USE_WAREHOUSE`), the ontology's compile settings (Helm values `cairn.ontology.*`; runs are on demand), residency rules (configuration, read-only in the API), glossary terms, domains, tags and classification rules (written in the catalog, Cairn Core's API), AI gateway model endpoints and keys (Helm values; routes over them are `cairn_ai_gateway_route`), Spark job definitions (submitted, not stored).
- **Export** (`cairn export`, `GET /v1/iac/export`). A live Cairn as code, so an existing workspace comes under git. `cairn export --format terraform --out cairn.tf` (or `--format yaml` for a bundle; `--kind` to narrow) reads every type as the token's user (`CAIRN_URL`, `CAIRN_TOKEN` or `CAIRN_TOKEN_FILE`) and writes a `resource` and an `import` block for each (Terraform 1.5+; the provider's write-only secrets need 1.11+). `terraform plan` then shows only imports, and `cairn bundle plan` on the YAML shows every resource unchanged (applying it adopts them). The token user's own types (saved queries, dashboards, alerts, folders, ...) are exported for that user only. `GET /v1/iac/export?kind=<type>&id=<Terraform id>&format=terraform|yaml` exports one resource for the person asking: their permissions apply, through the same API. Values Cairn adds (ids, timestamps, states) and fields at their defaults are left out.
- **Secrets.**
  - A secret's value is *write-only*: sent to Cairn, never read back, never in Terraform's state (`value` is a write-only attribute) or a bundle's state. Give it from a sensitive variable or a file. Raise `value_version` (Terraform and bundles alike) to send it again; a bundle keeps the versions in its state, never the values or their hashes.
  - Sensitive fields Cairn does return (a notification route's webhook URL, a quality monitor's webhook URLs) are marked sensitive and exported as variables: `var.<name>` with a `sensitive` variable block in Terraform, `${NAME}` in a bundle. Connections name a Cairn secret (`password: FINANCE_PG_PASSWORD`), external catalogs a credential reference, never a value.
  - Terraform's state still holds sensitive values that are not write-only (an agent identity's key): keep the state encrypted and access-controlled.
- **Drift.** A resource drifts only when something declared differs: what Cairn adds is not drift (also after an import). A resource deleted outside is created again by the next apply. Something a type can only be created (a secret scope, a mirror, a serving table) is replaced by Terraform when it changes; a bundle replaces it too, except an input table (its rows are people's work), which it refuses. A field that forces a new resource on a type that can be changed (a vector index's source) is refused by a bundle: delete it and declare a new one.
- **The platform as Terraform** (`deploy/terraform`):
  - `modules/cairn` installs the chart (`helm/cairn`) with the helm and kubernetes providers: a values `profile` (`default`, `ha`, `tenant`, `ha-tenant`, `active-metadata`, `authoritative`), `values` as HCL, `secrets` (Kubernetes Secrets from sensitive variables, made before the release) and `tenants` (each a namespace `cairn-<id>`, a quota, its `tenant_secrets` and a release with the tenant profile, as `deploy/hosting/tenant.sh` makes them; their databases are still made by `tenant.sh create` or your database module). The caller configures the providers.
  - `modules/hetzner` makes the load balancer (`deploy/hetzner/terraform`, which is now also a plain module) and runs `cluster.sh`'s bootstrap from Terraform: prepare, k3s and the kubeconfig, the backup buckets, the platform services, each again when its trigger changes (the nodes, the k3s version, `revision`). `cluster.env` is read by the scripts on your machine, never by Terraform, so nothing of it is in the state; give `name` the same value as its `CLUSTER_NAME`.
  - `modules/aws`, `modules/gcp` and `modules/azure` take an existing EKS, GKE or AKS cluster and make object storage (an S3 bucket, a Cloud Storage bucket, a storage account container) for backups and metadata exports, reached without keys: IAM roles for service accounts, Workload Identity, or a federated user-assigned identity, each for Cairn's service account only. On AKS the pods also need the label `azure.workload.identity/use: "true"`, which the chart does not set yet: add it with a policy or mutate it in until it does.
  - `examples/` holds a root module for each (`kind`, `hetzner`, `aws`, `gcp`, `azure`). Secrets come from `TF_VAR_...` or a secrets manager's data source, never a `.tfvars` file in git (`deploy/terraform/.gitignore` keeps them out).
- **Bundles** (`cairn bundle`). Resources are declared in YAML (`examples/bundles/cairn.yaml`) under a `bundle:` name, one section per type.
  - **Commands.** `cairn bundle validate FILE` checks the file alone. `plan` shows what would be created, updated and, with `--prune`, deleted; `--detailed-exitcode` exits 2 when there are changes. `apply` does it.
  - **Variables.** `${NAME}` comes from `--var NAME=value` or the environment, so one file serves several environments. A missing one is an error. `$${NAME}` stays a literal `${NAME}` (for a custom recipe's secret reference).
  - **Prune.** Cairn keeps each bundle's state: what it manages, versioned (`GET` and `PUT /v1/bundles/{name}`, `MANAGE_SYSTEM_OPERATIONS`). `--prune` deletes only what the bundle created, never resources made by hand or by other bundles.
  - **Adoption and concurrency.** An existing resource with a declared name is adopted. Two applies from one plan cannot both win: the second is told to plan again.
  - **Partial failures.** Changes go through Cairn's API as the token's user, so its permissions and audit apply. State is recorded after each change, so a failure halfway leaves an accurate record and the next apply finishes the job.
- **Saved queries, dashboards, alerts and workflows** are the token user's own (`queries`, `dashboards`, `alerts`, `workflows`). Dashboard tiles, alerts and workflow steps name their saved query (`query: revenue-by-region`); Cairn's ids are looked up at apply time, and a name that is not one of the user's saved queries is refused at plan. Others' shared queries and dashboards of the same name are never touched. Prune deletes dashboards, alerts and workflows before the queries they name.
- **BI** (`workbooks`, `metrics`, `input_tables`, and `dashboards` with any tile).
  - **Dashboards** take three kinds of tile: a saved query (`query: <name>`), a workbook element
    (`element: {...}` with its `connection`) and a metric (`metric: {metrics: [...], dimensions: [...]}`). They may
    also carry `controls`.
  - **Workbooks** are `name`, `connection` and `spec`.
  - **Metrics** are known by `name`. They need `label`, `connection`, `table` and `measure`, and may have
    `filters`, `time_column` and `dimensions`. A metric is checked by compiling it, so its table must exist.
  - **Input tables** are known by `table` and need `connection` and `columns`. A bundle creates an input table
    but never rewrites one, because its rows are people's work: a plan whose declared table differs from Cairn's
    is refused. Prune unregisters it; the table and its rows stay.
  - **Order.** Input tables, queries and metrics are created before the workbooks and dashboards that use them,
    and deleted after.
  - **Staging and production.** One file serves both through `${env}`. The test deploys a dashboard to staging
    and production on real Trino, edits one, and prunes
    (`test_bi_as_code_deploys_a_dashboard_to_staging_and_production`).
  - **Plan** asks only for the kinds the bundle declares or owns, so a service that is off (maintenance, say)
    does not fail a BI-only bundle.
- **Policies** (`policies`). Each is one of Cairn's own access policies, set and deleted on its own through `PUT` and `DELETE /governance/policies/{id}` (`MANAGE_POLICIES`); the rest of the bundle in force is left as it is. Each change is a new audited version of the policy bundle, and concurrent changes are serialized (an advisory lock on PostgreSQL), so two editors or replicas never lose each other's work. A policy is checked when the file is read, and compared exactly: removing a declared condition is a change. Policies owned elsewhere (personas, purposes, workspaces, native policies) are refused. Policies are created first and deleted last, so access goes last.
- **Connections** (`connections`). Ingestion sources: `name`, `connector`, its form `values`, `schedule`, `executor` (or `recipe` for a custom connector). Secret fields name a Cairn secret (`password: FINANCE_PG_PASSWORD`), so no secret is ever in git. Owners are kept when the bundle does not declare them (`MANAGE_INGESTION`).
- **Warehouses** are part of the deployment, not resources: declare them in Helm values (`cairn.trino.warehouses`), or with Terraform's `helm_release`. Who may use one is a policy granting `USE_WAREHOUSE` on `urn:li:warehouse:<name>`.
- **Drift.** A resource drifts only when something the bundle declares differs. What Cairn adds (ids, timestamps, alert states, a tile's default title) does not count.
- **A vector index keeps its source.** To index another table, delete the index and declare a new one; plan says so.
- **In place.** Changes keep each resource's id (`PUT /v1/pipes/{id}`, `/v1/streams/{id}`, `/v1/derived/{id}`), so a stream keeps its offsets. A derived table whose query changes is rebuilt from the whole query on its next refresh.
- **In CI.** On a pull request, run `cairn bundle plan cairn.yaml --var ENV=prod` with a read token. On merge, run `cairn bundle apply cairn.yaml --var ENV=prod --prune` with a pipeline user's token (`CAIRN_URL`, `CAIRN_TOKEN`).
- **Terraform and OpenTofu** (`terraform-provider-cairn`). Every resource type above is a resource and a data source, working through the same API. A metric's id is its name; an input table is created once (any change replaces it in the plan, destroy unregisters it). The Terraform test covers workbooks, metrics and metric tiles; `cairn_input_table` follows the bundle's path, which is tested on Trino, and has not been run by Terraform yet. A policy's id is `policy_id`; its `target`, `condition` and `obligations` are JSON. A connection's `values` and `schedule` are JSON. Changes are made in place. Something deleted outside Terraform is created again, and import uses the id (a maintenance policy's is its scope). See the provider's README.
  - **References.** Dashboards, alerts and workflow steps take saved query ids (`cairn_saved_query.x.id`). Dashboard `tiles`, workflow `steps` and a query's `chart` are JSON (`jsonencode(...)`), compared like a bundle's: what Cairn adds is not drift. A saved query's connection is `connection_name`, since Terraform keeps `connection` for itself.
  - **Data sources.** Each resource type is also a data source, read by its name (a maintenance policy by its scope), for what is managed by hand, by a bundle or by another workspace: `data "cairn_saved_query" "q" { name = "by-hand" }`. When others share one of the same name, the caller's own wins.
  - **Publishing.** The provider folder holds a GoReleaser config, the registry manifest and a release workflow for its own repository (`terraform-provider-cairn`, split from this one with `git subtree split`): a `vX.Y.Z` tag builds every platform and signs the checksums with GPG, which is what the Terraform and OpenTofu registries read. CI checks the config and runs an unsigned snapshot release on every push. See the provider's README, "Publishing".
- **Tested.**
  - **Every type, end to end** (`tests/test_iac_roundtrip.py`, the real API in-process; and `tests/test_terraform_provider.py`, Terraform 1.13 with the provider built from source against a real Cairn over HTTP). Each type whose service runs without outside systems and has a registry example (39 of the 54, in all seven families; connections, dashboards, alerts, workflows and input tables are covered by the tests below) is made from its example; exported as a bundle and planned with no changes; changed in git and applied in place; a secret rotated by its version; pruned. With Terraform: created, no drift, every example's change made in place (`0 added, N changed, 0 destroyed`), the secret never in the state, one of each family removed from the state and imported by id with no change (an imported secret with a version declared is sent once more, as it should), destroyed. An exported workspace planned as `N to import, 0 to add, 0 to change, 0 to destroy`, applied, then planned with no changes, and its YAML planned by a bundle with no changes. `GET /v1/iac/export` and `/v1/iac/inventory` answered as the caller (another person saw none of the operators' types).
  - **The types that need outside systems** (`tests/test_iac_outside_e2e.py`, CI job `terraform`): `workload_bindings`, `parquet_tables`, `mirrors`, `serving_tables`, `volumes`, `volume_grants`, `external_catalogs`, `model_deployments`, `slos` and `report_schedules`, each declared in a bundle (created, planned with no changes, exported and planned with no changes, changed in place, pruned) and by Terraform (created, no drift, changed in place, removed from the state and imported by id with no change, destroyed), against the real systems: a kube-apiserver and etcd (a service account token is exchanged at `/v1/auth/workload-token` only while its binding exists), Cairn Store, Cairn Catalog and Trino (Parquet files registered in place and counted; another organisation's catalog attached to Trino and queried), PostgreSQL 16 with `wal_level = logical` (a mirror's publication and replication slot are made and dropped; a serving table is read by key), MLflow (a model trained and registered through Cairn, then served through the deployment), and Mailpit (a report run and mailed with its file).
  - **Declaring them.** A workload binding's `user` is the full `urn:li:corpuser:…`: Cairn keeps it that way, so a bare name would show as a change on every plan. Removing a Parquet table's declaration stops registering new files; the Iceberg table and its files stay (zero copy), so drop the table yourself if you no longer want it, before declaring the same target again. An external catalog's credential is a reference (`env:` or `scope:`), never a value.
  - **The coverage gate and the generated provider** (`tests/test_iac_coverage.py`, in the python job).
  - **The platform modules**: `terraform fmt`, `tflint` and `terraform validate` for every module and example (job `terraform-modules`; run locally too); `examples/kind` is applied on a kind cluster in that job and the API's `/health` asked. The cloud and Hetzner modules are validated only, never applied by CI.
  - **Bundles** (`tests/test_bundles.py`, against the real API). Plan, apply and re-plan with no changes. An update kept the ids. Prune left a hand-made pipe alone. An existing pipe was adopted. A stale plan was refused. A bad resource stopped the apply with Cairn's reason, after recording what was done.
  - **Terraform** (`tests/test_terraform_provider.py`, Terraform 1.13 and the provider built from source). Create, then no drift. An in-place change kept the ids. A resource deleted outside was recreated. Cairn's refusal surfaced as a Terraform error. Import, then destroy, which left a hand-made pipe.
  - **Policies and connections.** A bundle created a policy and a connection; the rest of the policy bundle, including a persona's policy, was untouched, and the connection's secret stayed a reference. A second plan had nothing to do. A change to the priority, a removed condition and a new database were updated in place, keeping the connection's owners. A persona's policy id and an invalid policy were refused. Prune deleted the connection, then the policy (`tests/test_bundles.py`). One-policy changes from 4 processes at once on PostgreSQL lost nothing, and the same test fails without the lock (`tests/test_policy_items.py`). Terraform created both, read the admin policy and the connection through data sources, showed no drift (also after an import), changed both in place, put back a policy deleted outside, and destroyed both (`tests/test_terraform_provider.py`).
  - **SQL resources and vector indexes.** A bundle (`tests/test_bundles.py`) created saved queries, a dashboard, an alert, a workflow and a vector index, with query names turned into ids. A second plan had nothing to do, even after someone else shared a query of the same name. Changes kept the ids. A new source for an index and an unknown query name were refused at plan. Prune deleted the dashboard, alert and workflow before their query, and left another person's query alone. Terraform did the same with ids, read a hand-made query through a data source, showed no drift on the JSON attributes, changed two resources in place and destroyed only what it made.

## Forms in the web UI

The platform hubs' create, edit and run actions are forms, not JSON (Getting started, "Creating and changing things: forms", says how people use them).

- **Where the forms come from.** `python -m cairn.iac.forms` writes `ui/src/consumer/resources/forms.generated.json`, one form per action that sends a body in the UI's area catalog (`ui/src/consumer/resources/catalog.ts`). A form's fields come from the resource registry when the action is a resource type's create or update call (types, required fields, defaults, descriptions, sensitive and write-only fields, references to other types, and which fields go in the path), else from the route's request model in the API's OpenAPI document, else from the action's example body. Field names choose the widget: SQL editors, cron with its meaning in words, durations with units, password fields for secrets, and pickers that search the API. The file is bundled with the UI, so forms need no request to draw and the offline demo has them too.
- **Keeping them current.** After changing the registry, a route's request model or the catalog, run `python -m cairn.iac.forms`; CI runs `python -m cairn.iac.forms --check` and fails when the file is stale. A UI test fails when an action that sends a body has no form and is not on the reasoned exclusion list (`FORM_EXCLUSIONS` in `ui/src/consumer/resources/forms.ts`; empty today).
- **Errors.** Every page, the hubs and the purpose-built pages alike, shows API errors through one presenter: a sentence and what to do (403: the permission named in the refusal and whom to ask; 409; 422 with each problem on its field; 429; 503 "not configured": a link to the area's section of this manual), with the API's answer under **Details** (a toast or a line under a field carries the sentence only). A UI test fails when a page puts an error's raw text on screen. A form's fields are checked before sending (required, patterns, ranges, cron, JSON), and FastAPI's 422 `loc` places the API's own problems on the fields.
- **Pickers.** A field that names something in Cairn lists what the person may use: tables and datasets (Cairn Core's catalog search, else the connections' tables), people and groups, connections, Cairn's warehouses (`CAIRN_WAREHOUSES`, with their state from the warehouse proxy), secret scopes and secret references (`scope:<scope>/<key>`, from the scopes the person may read; never a value), the person's git folders (sent by id), flows, agents, and resources of another type. A picker never blocks: a value it does not list can be typed. The same pickers are used outside the hubs: a flow's **After another flow succeeds** trigger and an infrastructure-as-code change's **Commit to a branch for review**. An external catalog's **Warehouse** is the other catalog's own warehouse name, typed, not one of Cairn's.
- **A budget's scope** is one choice among everything, a domain, an owner, a platform, tables, a Cairn warehouse or AI spend; the form refuses two at once before sending.
- **Docs links.** Each area in the catalog names its section of these docs (`docs`); a UI test fails when the section does not exist.
- **What it doesn't change.** A form sends the same request the API always took, as the person signed in: it adds no permission and checks nothing the API does not check again. Raw JSON is always there for keys a form does not show.

## Agents and infrastructure as code

Agents change Cairn the way a careful team does: they write the change as code, plan it, and propose it; a person who could make the change approves it and applies it, as themselves (`/v1/iac/changes`, `cairn.iac.changes`). People can propose the same way, and **Ask an agent** drafts a change from plain words.

- **A change** is a bundle (the sections above: `queries`, `flows`, `policies`, ...) or Terraform files (`{"main.tf": "..."}`: `resource "cairn_<type>"` blocks with literal values, `jsonencode(...)`, `var.<name>` and references to other resources in the files; other blocks, interpolation and other functions are refused with their line). It may add `delete: [{kind, key}]` for what it removes, which needs `allow_delete: true`, as does anything a plan would replace.
- **Plan** (`POST /v1/iac/changes/plan`, MCP `iac_plan`): the bundle engine plans it through Cairn's own API, in-process, with no side effects, and answers each resource created, updated, replaced or deleted with each field before and after (secrets and sensitive fields masked) and a digest. A person plans as themselves; an agent's plan reads Cairn as the person it acts for, read-only (some APIs, such as the SQL workspace, are for people only, and a plan only reads).
- **Propose** (`POST /v1/iac/changes`, MCP `iac_propose`): stores the change (as bundle YAML, and the same as Terraform for the record) with its plan, its title and intent, who wrote it (the agent and the person it acts for, the *sponsor*), and status `proposed`. Agents need the scope `iac:propose` (asked for at sign-in, or given to an agent key); without it they may plan but not propose. Refused, and audited (`iac.change_refused`): a change that changes nothing, one that deletes or replaces without `allow_delete`, and one that holds a secret: secrets and sensitive fields must be references (`${NAME}`), and nothing anywhere in the change may look like a credential (keys, tokens, private keys, passwords). An agent's tool call that carries one is also stopped by abuse detection first. The people in `CAIRN_IAC_CHANGE_APPROVERS` (user or group urns, comma-separated) and the sponsor are told (`iac.change_proposed`).
- **Approve or reject** (`POST /v1/iac/changes/{id}/approve` or `/reject`, with a `comment`; the **Changes** tab under Settings, **Infrastructure as code**). The approver:
  - is a person (agents never approve, decide or apply: those routes refuse every agent credential);
  - holds `APPROVE_IAC_CHANGE` on every resource type the change touches (`urn:li:iacKind:<type>`, e.g. a policy allowing it on `urn:li:iacKind:*` for platform owners, or on `urn:li:iacKind:queries` for a BI lead); when the policy cannot be asked, the answer is no;
  - could make the change themselves: Cairn plans it again as the approver, and it must come out the same (an update to someone else's saved query would, as the approver, be a new one: refused);
  - is not the sponsor or the author (separation of duties), unless `CAIRN_IAC_SELF_APPROVE=true`.
  Rejecting needs the same rights; the sponsor or author withdrawing their own change is `withdrawn` (`DELETE /v1/iac/changes/{id}` too). Every refusal is audited (`iac.change_approval_refused`).
- **Apply** (`POST /v1/iac/changes/{id}/apply`, MCP `iac_apply` for a person): only the approver applies, as themselves, so their permissions and audit apply to each resource (a saved query or flow made this way is theirs). `variables` gives the values of the change's references (a secret's `${NAME}`): used for this apply only, never stored, logged or audited, and never taken from Cairn's environment; a missing one is refused. Cairn plans again first: when the plan differs from the one approved (someone changed a resource meanwhile), the apply is refused (409, with the new plan), the change goes back to `proposed` and the approvers are told (`iac.change_drift`). Then each resource is created, changed or deleted through Cairn's API, and the change is `applied`, or `failed` with what was done before the failure.
- **In git** (`POST /v1/iac/changes/{id}/commit` with `git_folder`, MCP `iac_open_pr`): the change (`change.yaml`, `main.tf` and `PLAN.md`) is committed to a new branch, `cairn/iac-change-<id>`, of one of the person's [git folders](#git-folders), never to the folder's branch, with a pull request where the repository is configured for it. Agents need `iac:propose`.
- **MCP tools**: `iac_schema` (the resource types; with `kind`, its fields), `iac_inventory`, `iac_export` (`kind`, `id`, `format` yaml or terraform), `iac_plan`, `iac_propose`, `iac_changes` (the requests the caller may see), `iac_apply` (people only) and `iac_open_pr`. They act through Cairn's API with the MCP caller's own credentials, so they see and plan only what the person may.
- **Ask an agent** (`POST /v1/iac/changes/draft` with `prompt`; `GET /v1/iac/changes/assistant` says whether it is ready). Cairn's assistant asks the model `CAIRN_IAC_AGENT_MODEL` through [Cairn's AI gateway](#ai-gateway-model-calls-from-code-people-and-agents-write), as the person (their `USE_AI_GATEWAY` on the model, their budget, metering and abuse checks), with the IaC tools above; every tool runs as the person. What it proposes is a change request by the agent `cairn-iac-assistant` for the person, so someone else approves it. Asked for a flow ("build a nightly flow that refreshes X, checks it, then emails the report"), it proposes a `flows` resource, which opens read-only on the canvas from the change (**Open in canvas**). Without a model or the gateway, it says so and drafts nothing.
- **Who sees a change**: its sponsor and author, the approver and the person who applied it, admins, and people who may approve changes to its types. Others get 404.
- **Audit** (category `iac`): `iac.change_proposed`, `iac.change_refused`, `iac.change_approved`, `iac.change_approval_refused`, `iac.change_rejected`, `iac.change_withdrawn`, `iac.change_drift`, `iac.change_apply_started`, `iac.change_applied`, `iac.change_failed`, `iac.change_committed`, each with the change's id.
- **Tested** (`tests/test_iac_agent_e2e.py`: a real Cairn API and its MCP server over the streamable HTTP transport with agent keys, a real git repository, and the AI gateway to a local stand-in for the model provider that answers with tool calls). An agent planned (nothing made) and proposed a saved query and a flow; the person without `APPROVE_IAC_CHANGE` and the sponsor were refused; an approver approved, and applying made both as the approver; a resource changed between approval and apply was refused as drift and sent back for approval; a delete without `allow_delete`, a secret's value (in a bundle and in Terraform) and a credential in a query were refused; an agent without `iac:propose` planned but could not propose; agents could neither approve nor apply; a Terraform change with a reference between resources planned like a bundle; a change was committed to a branch of a git folder; Ask an agent proposed a three-step nightly flow through the gateway (the provider saw Cairn's key, never the person's credentials), was refused for a person without the model, and said so with no model; every step was in the audit log. The UI's Changes view and Ask an agent with vitest.
- **Not tested**: a pull request opened on a hosted git provider (the branch and commit are tested with a local repository), and Ask an agent with a real model (the loop and the gateway are tested with a stand-in that answers with tool calls).

## Backup and restore

Docker Compose deployments are backed up with one script:

```bash
deploy/ops/backup.sh                    # writes backups/cairn-<timestamp>/
deploy/ops/restore.sh backups/cairn-<timestamp>
```

**A backup holds:**

- the Cairn database (`pg_dump`),
- the catalog database (`mysqldump --single-transaction`),
- the gateway's identity keys, so remote agents keep trusting it,
- `config/`,
- a `manifest.json` with each file's checksum, the Cairn version and the schema revision.

Both dumps are consistent and taken while Cairn runs. The search index is not backed up: it is rebuilt from the catalog database.

**A restore** first checks the checksums and asks you to confirm. It then:

1. stops Cairn, but not its databases,
2. replaces both databases and the gateway keys,
3. rebuilds the search index,
4. starts Cairn again.

Anything written after the backup was taken is lost. `config/` is unpacked only with `RESTORE_CONFIG=1`. Backups contain secrets and personal data, so keep them encrypted.

On Kubernetes, use your database service's backups (point-in-time recovery) and back up the gateway key Secret. After restoring the catalog database, rebuild the index by running the setup image with `-u RestoreIndices -a clean`.

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
* **Push it to a SIEM** as it is written: list sinks in `CAIRN_SIEM_SINKS` (JSON or a file; a wrong sink stops Cairn at start-up) and the notifier (`cairn notifier`) sends each one what it has not accepted yet.
  - `splunk_hec`: Splunk's HTTP Event Collector (`url`, `token_file`, optional `index`, `sourcetype`, default `cairn:audit`).
  - `syslog`: RFC 5424 over TCP (`host`, `port`), with TLS when `tls: true` (`ca_file` for a private CA); facility *log audit*, severity *warning* for denials and failures; `format: json` (default) or `cef` (QRadar, ArcSight, Sentinel's CEF connector).
  - `http`: the entries as a JSON array, with `Authorization: Bearer` from `token_file` (Elastic, Datadog, Sumo Logic, a log pipeline).
  - Tokens are read from files (mounted secrets), never from the configuration. `categories` limits a sink; `batch` (500) sets the batch size.
  - **Delivery** is at least once, in chain order: a sink's cursor moves only when it accepted a batch (HEC's and HTTP's answers; for syslog, the receiver reading the whole stream and closing). Each entry carries `id`, `seq` and its chain `hash`, so the SIEM can drop repeats and verify the chain from what it holds. A failing sink backs off (up to five minutes) and keeps its place; one notifier ships a sink at a time.
  - `GET /cairn/api/v1/audit/siem` (`MANAGE_POLICIES`) shows each sink's position, lag behind the log, entries sent and last error; `POST …/siem/<sink>/rewind {"seq": n}` sends again after entry `n` (audited). Metric: `cairn_siem_entries_sent_total{sink}`. Migration 0069. Tested against Vector as a Splunk HEC, syslog (TCP and TLS) and HTTP receiver in `tests/test_siem_e2e.py`.

## Disaster recovery across regions

A standby region keeps a copy of the lakehouse, so losing the primary region costs minutes and the last few commits, not the lakehouse.

**What runs in the standby.**

- **Cairn Store**, receiving the primary's bucket by replication (`PutBucketReplication` to a target on the standby; see Replication above). Versioning is on at both ends.
- **PostgreSQL**, a streaming standby of the primary's database that holds the catalog's state (and Cairn's own). Use your PostgreSQL operator, or `pg_basebackup -R`.
- **Nothing else needs to run** until failover. Cairn Catalog, the Cairn API and Trino hold no state of their own beyond the database and the store.

**Why reconcile.** The two copies are made independently, so either can be ahead. Where the database is ahead, a table points at a metadata file, manifest or data file that never reached the standby store, and cannot be read there.

**Failover runbook.**

1. **Declare the primary lost**, and stop anything still writing to it.
2. **Promote the standby database**: `pg_ctl promote`, or your operator's switchover.
3. **Start Cairn Catalog** in the standby: `CAIRN_CATALOG_DATABASE_URL` pointing at the promoted database, `CAIRN_CATALOG_S3_ENDPOINT` at the standby store, the same warehouse (`s3://<bucket>/...`) and tokens.
4. **Reconcile**: `cairn dr reconcile --catalog <standby catalog> --token <catalog token> --store <standby store> --access-key ... --secret-key-file ...`.
   - It reports every table that cannot be read and what it would roll back to: its newest commit whose files all arrived.
   - Run it again with `--apply` to make the repairs. Each repair drops the table without purging its files, then registers it at that metadata file, so the files of the lost commits stay in the store for inspection.
   - Tables whose files are all there are not touched. A table with no whole commit left is reported as unrecoverable; restore it from a backup.
5. **Point clients at the standby**: Cairn's API and Trino's catalogs, through DNS or configuration.
6. **Afterwards**, rebuild the old primary as the new standby, the other way round.

**Automatic failover (`cairn dr watch`).** Steps 2 and 5 can run by themselves. Run the watcher in the standby region, one replica, never in the primary (it must not share the primary's fate): `cairn dr watch --config failover.yaml`, from Cairn's image (the `dr` extra).

- **When it acts.** Every `interval_s` (10) it probes the primary's checks. It fails over only when every primary check has failed `failures` rounds in a row (3; at least 2). One component failing is not a lost region, and a primary that answers again resets the count.
- **Only onto a healthy standby.** Every standby check must pass and every standby database must answer. Otherwise it logs why and keeps watching: a region that stays down is better than two that both take writes.
- **What it does, in order.**
  1. Promotes each standby PostgreSQL (`pg_promote`) and waits until it takes writes. That covers the catalog's database and Cairn's own, whether they are on one cluster or two.
  2. Points DNS at the standby. Each record is replaced by an RFC 2136 update signed with a TSIG key, then read back from the server. This works with BIND, PowerDNS, Knot and Windows DNS. For a hosted DNS service, use its own health-checked failover records instead.
  3. Writes its state file. While the file exists it does nothing more, and it never fails back on its own. Rebuild the old primary as the new standby (step 6), then remove the file.
  - **A failover stopped half-way** (say, the DNS server did not answer after the databases were promoted) is finished in the next round, whatever the primary does by then. The state file records it before anything changes. Sending clients back to a returned primary would leave two regions taking writes.
- **Then** run step 4 (reconcile) and start Cairn Catalog and Cairn in the standby, or keep them running there against the standby database: they reconnect once it is promoted.
- **Settings** (YAML):
  - `primary_checks` and `standby_checks`: `http(s)://` URLs (any status below 500 counts) or `tcp://host:port`.
  - Prefer HTTP for the standby. A hung process still completes TCP handshakes, so only an HTTP check tells that it works.
  - `standby_databases`: each `url_file`, a file holding the PostgreSQL URL.
  - `dns`: `server`, `port`, `zone`, `key_name`, `key_algorithm` (`hmac-sha256`/`384`/`512`), `key_secret_file`, and `records` (`name`, `type` A, AAAA or CNAME, `value`, `ttl` of at most 3600; keep it low).
  - `state_file`, `failures`, `interval_s` and `timeout_s`.
  - Secrets come only from files. Settings that could make it misfire are refused at start: fewer than 2 failures, an unknown check scheme or record type, a long TTL, or an unreadable secret.
- **The DNS server's side** (BIND): `key "cairn-failover." { algorithm hmac-sha256; secret "..."; };` and, in the zone, `update-policy { grant cairn-failover. name api.example.com. A; ... };`. Grant only the records the watcher changes.

**Measured** in the drill (`tests/test_dr_drill.py`, run in CI):

- **Setup.** Two regions, each with a real store and PostgreSQL. The primary's store replicated to the standby's, and its database streamed to the standby's.
- **The loss.** Ten commits were made. The standby's store was frozen for the last four, as over a slow link. Then the primary region was killed: store, catalog and database.
- **Recovery.** The standby database was promoted and a catalog started on it. The report found one table to roll back by four commits, and left the other alone. `--apply` repaired it.
- **Results.** RPO: 4 of 10 commits (4.3 s of writes). RTO: about 2 s from the loss to every table readable. The standby then took writes.
- **Automatic failover**, with a real BIND server taking the updates. Cairn's own database had been migrated on the primary's cluster and had audited work.
  - The primary region was lost while the standby store was hung, and the watcher refused to fail over.
  - Once the standby answered, the next round promoted the standby database and switched `api` (A) and `lake` (CNAME), 1.4 s after the loss.
  - Cairn's database came across with its audit chain whole (5 entries) and took the next write.
  - A wrong TSIG key was refused by the DNS server, a partial outage of the primary never triggered, and nothing happened after the failover.
- **Your figures** depend on replication lag (watch `x-amz-replication-status` and PostgreSQL's replication lag) and on how fast your DNS and clients follow.

## Upgrades

Upgrades run forward only: there are no down-migrations, so the way back is the backup you take first. Releases are rehearsed by installing the previous release, filling it with data, and upgrading it with exactly these steps. The current release was rehearsed from schema revision 0009 to 0014; every table kept its data and the audit chain stayed valid.

**What happens during an upgrade:**

- **Cairn Core:** the `cairn-setup` job migrates the catalog's schema, search indices and topics before Core starts. On Helm it is a `pre-upgrade` hook.
- **Cairn services:** each migrates the Cairn database when it starts, one at a time, under a database lock. The others wait.
- **Workers:** a replica that is still on the old version keeps working until it is replaced. The leader locks keep two versions from doing the same work.

**Before you start:**

1. Read the release notes for setting changes and new required values.
2. Check the current schema: `docker compose exec cairn-api cairn db current`.
3. Take a backup: `deploy/ops/backup.sh` on Compose, or a database snapshot on Kubernetes.

**Docker Compose:**

```bash
deploy/ops/backup.sh                                   # 1. back up
git pull                                               # 2. the new release's compose file and config
export CAIRN_VERSION=<new version>                     #    (or set it in .env)
docker compose -f docker-compose.cairn.yml pull        # 3. fetch the new images
docker compose -f docker-compose.cairn.yml up -d --wait   # 4. migrate and restart
```

On the reference stack, step 4 took about two minutes. During that time the UI and API restart, so expect a short outage on a single node.

**Kubernetes (Helm):**

```bash
helm upgrade cairn ./helm/cairn -f my-values.yaml [-f helm/cairn/values-ha.yaml] --wait --timeout 20m
```

The setup hook runs first. The Deployments then roll one pod at a time, and the PodDisruptionBudgets keep each service up during the roll.

**After the upgrade, check:**

```bash
docker compose exec cairn-api cairn db check        # exit 0: the schema is at the release's revision
docker compose exec cairn-api cairn audit verify    # the audit chain is intact
docker compose ps                                   # every service healthy
```

Then sign in, open a dataset, and run a search.

**If something is wrong:**

1. Go back to the previous version's images.
2. Restore the backup: `deploy/ops/restore.sh backups/cairn-<timestamp>`.

A newer schema is not readable by older services, so restoring is required: rolling back the images alone is not enough.

```bash
cairn db current     # schema revision of the Cairn database, and the latest one
cairn db check       # non-zero exit if the schema is not at the latest revision or differs from it
```

**The upgrade matrix** (the engines Cairn works with).

- **The pins.** `deploy/versions/pins.json` records the version of each engine Cairn is tested with: Trino, Spark, its Iceberg runtime, dbt-core, dbt-trino, Airflow and pyiceberg. A test (`tests/test_version_pins.py`) checks that every file that pins one says the same version.
- **Every week** (`.github/workflows/cairn-upgrade-matrix.yml`, Mondays, or on demand), `deploy/versions/latest.py` looks up each one's newest release on Docker Hub, PyPI and Maven Central, skipping pre-releases. For each that is newer than its pin, the matching end-to-end tests run against it: Trino's against the new Trino image, the catalog and lakehouse suites against the new pyiceberg, and the dbt, Airflow and Spark suites against images built with the new version.
  - A green row means the bump is safe. A red row is the work to do before bumping. A skipped test counts as a failure.
- **Bumping.** Change the pin in `pins.json` and in the files it lists, in one pull request. It merges only when the main CI passes on it.
- **Tried here.** The resolver found Trino 484, Spark 4.2.1 and Iceberg 1.12.0 newer than their pins. Cairn's Trino suite (21 tests) was then run against Trino 484:
  - First, Trino did not start: 484 refuses `s3.exclusive-create` (now `delta.s3.transaction-log-conditional-writes.enabled`) and renames `fs.native-s3.enabled` to `fs.s3.enabled`.
  - With those two settings renamed, every test passed. Policies, masks, row filters, SSO, lineage, AI functions, stored functions, procedures, Delta writes from two clusters and Delta conversion all worked unchanged.
  - The tests name each setting the way the image in use expects. Trino is now pinned to 484 (for Iceberg v3): the chart, Compose and the conformance suite use `fs.s3.enabled` and `delta.s3.transaction-log-conditional-writes.enabled`; set `CAIRN_TRINO_IMAGE` to an older image to run the tests against it.
