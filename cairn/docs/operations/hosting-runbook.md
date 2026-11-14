# Hosting runbook

> Internal. How to run Cairn as a hosted service: one isolated release per tenant (Administration →
> Hosting tenants in the product docs). Commands assume `kubectl` and `helm` against the hosting
> cluster, and the platform environment variables that `deploy/hosting/tenant.sh` reads. The Hetzner
> cluster that this runbook was built against is described in `deploy/hetzner/README.md`.

## The platform

Each tenant is a Helm release `cairn-<id>` in namespace `cairn-<id>`, labelled `cairn.io/tenant`. It gets:

- the HA profile and `values-tenant.yaml`,
- its databases `cairn_<id>` and `cairn_catalog_<id>`, owned by role `cairn_<id>`, on the shared PostgreSQL server,
- its search and Kafka, in one of two layouts chosen when the tenant is created:
  - **own services** (`CAIRN_TENANT_SERVICES=1`, the Hetzner setting): OpenSearch and Kafka in the tenant's
    namespace, one node each, or three replicated nodes each with `CAIRN_TENANT_SERVICES_HA=1` (that variable
    only takes effect together with `CAIRN_TENANT_SERVICES=1`). A tenant's layout is not switched in place;
  - **shared services**: an index prefix `<id>_` on the shared OpenSearch cluster (`CAIRN_SEARCH_HOST`) and a
    Kafka bootstrap from `CAIRN_KAFKA_TEMPLATE`;
- a ResourceQuota and network policies.

**Shared by every tenant:**

| Component | Notes |
|---|---|
| PostgreSQL | the shared server, with point-in-time recovery on (WAL archived continuously) and pgvector installed (`tenant.sh create` adds the extension). On the Hetzner cluster it is a CloudNativePG cluster with replicas on other nodes; nightly base backups kept 30 days. One server per region; split when CPU stays above 60% or storage above 70% (sizing policy) |
| Search and Kafka | own per tenant (above), or shared. Shared OpenSearch has no snapshot schedule in the repository; the index can always be rebuilt from PostgreSQL (see Recovery) |
| Ingress | TLS for `*.<domain>`; HSTS is on in the product. The Hetzner cluster uses ingress-nginx. The chart can also route through a shared Gateway API Gateway (`CAIRN_GATEWAY`) |
| Monitoring | Prometheus Operator. The chart's PodMonitor, PrometheusRule and Grafana dashboard are off by default and switched on in a tenant's values (`cairn.serviceMonitor`, `cairn.prometheusRule`, `cairn.grafanaDashboard`). The rules are in `deploy/observability/` |

**Per tenant, in the shared secrets manager:**

- the licence file,
- the PostgreSQL role password (also in the namespace's Secrets),
- the gateway key Secret, if the tenant uses remote agents across replicas.

## Tenant lifecycle

**Provision:**

1. Issue the licence, bound to the tenant id: `deploy/ops/licence.py issue … --tenant <id>`.
2. Run `deploy/hosting/tenant.sh create <id> --licence <file> --values platform.yaml`. It takes about 15 minutes.
   Add `--metering-key FILE` when the tenant reports its usage to this control plane (this needs
   `CAIRN_METERING_URL`). `--dry-run` renders the release without touching the cluster; `tenant.sh list` lists
   the tenants.
3. Check it:
   - `https://<id>.<domain>` loads;
   - **Settings → Licence** shows `valid`;
   - `kubectl -n cairn-<id> get pods` is all ready.
4. Hand over the admin password from Secret `cairn-core-secrets` (key `admin-password`), through the secure
   channel. Ask the customer to set up SSO and SCIM, then disable the local admin.

**Renew a licence:** `tenant.sh upgrade <id> --licence renewed.licence` replaces the licence Secret. The licence is
read when a process starts, and the chart does not restart pods when that Secret changes, so restart the release's
Deployments afterwards: `kubectl -n cairn-<id> rollout restart deploy`. The `CairnLicenceExpiring` alert fires
21 days before expiry.

**Offboard:**

1. Export the tenant's metadata for them if the contract says so: `kubectl -n cairn-<id> exec deploy/cairn-<id>-api -- cairn export metadata --to <their bucket>`.
2. Run `tenant.sh delete <id> --drop-data`. It uninstalls the release and deletes the namespace (which takes any
   of the tenant's own OpenSearch and Kafka with it). With `--drop-data` it also drops both databases and the
   role. Without that flag the databases are kept.
3. For a tenant on shared services, delete its indices (`DELETE /<id>_*` on OpenSearch) and its Kafka topics.
4. Point-in-time backups age out on their own; record the date they will be gone.
5. Send the customer a deletion confirmation listing what was removed and when the backups expire.

## Upgrades

Never upgrade every tenant at once:

1. **Rehearse** the release: `deploy/ops/rehearse-upgrade.sh <previous tag>` runs on a local Docker Compose stack. It
   installs the previous tag, fills it with data, upgrades it with the documented steps and checks that no data was
   lost. It destroys that stack's volumes. This is the release gate for the images. For a hosted tenant, rehearse
   on a staging tenant made the same way (no script does this). It has to pass.
2. **Canary:** upgrade the internal tenant first, with `tenant.sh upgrade <id>`. The chart's Core setup job runs as a
   pre-upgrade hook, and each Cairn process migrates the schema when it starts. Watch the dashboard for an hour:
   error rate, latency, workers.
3. **Waves:** upgrade 10% of tenants, then 50%, then the rest, with at least two hours between waves. Stop at the
   first error-rate alert and roll that tenant back.
4. **Roll back:** migrations are forward-only, so a rollback is the previous chart and images plus a restore (below).
   Prefer fixing forward, and only roll back on data-affecting faults.

Record each wave: release, tenants, start, end, problems. That record is the change-management evidence for SOC 2.

**Releases and the updater:**

- A release is a signed manifest that names every image and the chart by digest. A channel (`stable`, `early`,
  `lts`, ...) is a signed index with a serial and an expiry, so a feed cannot roll an install back. The tooling is
  `deploy/ops/release.py`.
- The chart's `cairn.updates` values set the feed, the channel and how many release keys must sign. An empty feed
  means no update checks.
- The updater (`cairn.updates.agent.enabled`, off by default) applies an approved update in its maintenance window,
  as its own Job. It runs `backupCommand` before the update and `restoreCommand` after a failed one. It is a
  separate path from `tenant.sh upgrade`, which is the path this runbook uses.
- Air-gapped installs (`cairn.updates.offline.enabled`, feed left empty) import one release bundle at a time with
  `cairn updates import <bundle>` in an API pod.
- Fleet check-ins (`cairn.fleet`) are off until a URL is set. They send a fixed list (version, offered update, the
  last update's outcome, licence state and seats, component health). `GET /v1/system/fleet` shows the exact body.

## Backups and recovery

**Targets** (set them per contract tier; these are the defaults to offer):

- RPO 15 minutes,
- RTO 4 hours for a single tenant,
- RTO 24 hours for a region.

| What | How it is protected | Restore |
|---|---|---|
| A tenant's databases | point-in-time recovery on the shared PostgreSQL server | no script in the repository. Restore the two databases to a new server or name, then point the release at them (`tenant.sh upgrade` with `CAIRN_PG_HOST` or `--values`) |
| Search index | rebuilt from the catalog database | `deploy/ops/restore.sh` runs `cairn-setup -u RestoreIndices -a clean` on Compose. For a hosted tenant, run the same setup image command in its namespace (`helm get values` gives the env); no script does this |
| Gateway keys | the Secret, copied to the secrets manager | re-create the Secret; remote agents keep trusting the gateway |
| Licence and credentials | the secrets manager | re-create the Secrets |

**Single tenant: data deleted or corrupted by mistake.**

1. Choose the restore point from the tenant's audit log: the last good entry.
2. Scale the release to 0: `kubectl -n cairn-<id> scale deploy --all --replicas 0`.
3. Restore both databases to that point.
4. Rebuild the index.
5. Scale back up.
6. Check that `cairn db check` and `cairn audit verify` pass.

**Region lost.** Restore PostgreSQL in the standby region from cross-region backups, create OpenSearch and Kafka, then run `tenant.sh upgrade` for each tenant against the new endpoints and rebuild each index. No script in the repository does this. Practise it once a year and keep the record (SOC 2 A1).

**Restore drill:** each quarter, restore one random tenant to a scratch namespace, run the smoke tests against it, and delete it.

## Alert response

Each alert has a severity: **critical** pages the on-call engineer; **warning** opens a ticket that needs looking at on the same business day; **info** needs no action. The rules are in `deploy/observability/cairn-alerts.yaml` (17 alerts for Cairn) and `cairn-store-alerts.yaml` (11 for the Cairn Store).

| Alert | First look | Usual fixes |
|---|---|---|
| CairnTargetDown (critical) | `kubectl -n cairn-<id> get pods`; events; `kubectl logs --previous` | OOM: raise the limit or find the leak; crash loop after an upgrade: roll that tenant back |
| CairnHighErrorRate (critical) | the dashboard by route; API logs; the core's logs | database or search unavailable; a bad deploy; the core overloaded (scale it) |
| CairnSlowRequests | the latency panel by route; database CPU | scale the API; slow queries on the shared database |
| CairnPolicyDecisionsSlow | the policy-decision latency panel; the policy count | a very large or regex-heavy bundle; attribute lookups to the core timing out |
| CairnDenySpike | the audit log for `access.denied` around the start | a policy change (find it in the audit log and talk to the customer); probing (block at the WAF) |
| CairnWorkerStalled (critical) | the worker's logs; Postgres locks (`pg_locks` on advisory keys) | a stuck leader: restart the pod and the standby takes over; database unavailable |
| CairnWorkerFailing | the worker's logs | a broken connection, credentials, or a bad config file |
| CairnNotificationBacklog | `cairn_notifications_pending`; notifier logs | Slack, Teams or mail down; scale is fine, as the backlog drains once the channel recovers |
| CairnNotificationsFailing | the failing channel's errors | expired webhook URL or token: tell the tenant's admin |
| CairnQualityIncidentSpike | which datasets; their sources | an upstream outage at the customer: let them know, nothing to fix here |
| CairnSourceFailing (warning) | Settings → Sources: the ingestion source that is failing or stale for 30 minutes | the source's credentials or schedule; tell its owner |
| CairnAIBudgetReached (info) | usage per task | expected behaviour; raise the budget if the contract allows |
| CairnLicenceExpiring | `licence.json` for the tenant | issue and install the renewal (account management) |
| CairnLicenceReadOnly (critical) | Settings → Licence: missing, invalid or expired | install the licence; a key error after a rotation means the image's keys lack the signing key |
| CairnLicenceSeatsFull | seats used vs limit | tell account management; the newest users can still read |
| CairnAgentErrorsHigh | Settings → AI agents → stats by agent and tool (`/admin/agents/stats`) | a broken tool after an upgrade; an agent calling with bad arguments (tell its owner, lower its quota or disable it) |
| CairnInjectionFlagged (info) | `ai.injection_flagged` in the audit log: which tool, which text | someone wrote instructions to AIs into catalog text: fix the description, talk to its author |
| CairnStoreDown (critical) | the Store's `up` is 0 for 5 minutes: its pods and logs | restart it; check its node and volume |
| CairnStoreServerErrors (warning) | more than 5% of requests answer 5xx for 10 minutes | the Store's logs for the failing route |
| CairnStoreDiskFilling (warning), CairnStoreDiskFull (critical) | a disk has under 10%, or under 3%, free (the alert names the disk) | free space or add capacity |
| CairnStoreEventBacklog (warning), CairnStoreEventsDropped (warning) | more than 1,000 event notifications waiting for 15 minutes; any dropped in an hour | the consumer of the Store's events |
| CairnStoreReplicationFailing (warning) | replication from a node keeps failing for 30 minutes | the peer's reachability; the Store's logs |
| CairnStoreClusterNodeDown (critical) | a known cluster node is not alive for 5 minutes | that node's pod or host |
| CairnStorePlacementFailing (critical) | writes could not place their copies | cluster health and capacity |
| CairnStoreTierFailures (warning) | transitions to a storage tier keep failing for 30 minutes | the tier's endpoint and credentials |
| CairnStoreSftpSignInFailures (info) | more than one failed SFTP sign-in a second for 10 minutes | the sources of the attempts in the Store's logs |

## Incidents

**Severity levels:**

- **SEV1:** data exposure, or a tenant entirely down.
- **SEV2:** major degradation for one or more tenants.
- **SEV3:** minor.

**Roles:**

- incident lead,
- communications,
- scribe.

A SEV1 or SEV2 opens a status page entry within 30 minutes. A suspected data exposure brings in the security owner at once and starts the contractual notification clock (often 72 hours).

**Customer facts come from the evidence:**

- the tenant's audit log (`cairn audit export --since …`) says who accessed what;
- `cairn audit verify` proves the log was not altered.

A blameless review within five business days produces actions with owners. Link the review in the incident record.

## Access to tenant data

Operators have no standing access to tenant data.

**Break-glass procedure:**

1. Open a ticket with the customer's consent.
2. The incident lead approves it.
3. Mint a short-lived admin token for that tenant. No command in the repository does this; confirm the method with the platform owner before the first use.
4. Do the work.
5. Revoke the token.

Everything done through Cairn lands in the tenant's audit log, so attach the export to the ticket. Direct database access uses a temporary role with the session logged; no command in the repository creates that role either, so confirm the method the same way.

## Rotation

| Secret | How | When |
|---|---|---|
| Control-plane service token | rotated by the token keeper (`CAIRN_TOKEN_ROTATE_DAYS`) | automatic, every 7 days by default |
| PostgreSQL role password | set a new one; update Secret `cairn-core-secrets` (key `db-password`) and `cairn-secrets` (key `database-url`); then restart the release | yearly, and when staff leave |
| Extension tokens and signing secrets | `cairn extension rotate <name>` in the tenant | when the extension's operator asks, or on suspicion |
| Licence signing key | new key pair (`deploy/ops/licence.py keygen`), add the public key to the images, re-issue licences, then remove the old key in a later release | on compromise; otherwise every two years |
| TLS certificates | cert-manager | automatic |
