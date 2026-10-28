# Hosting runbook

> Internal. How to run Cairn as a hosted service: one isolated release per tenant (Administration →
> Hosting tenants in the product docs). Commands assume `kubectl` and `helm` against the hosting
> cluster, and the platform environment variables that `deploy/hosting/tenant.sh` reads.

## The platform

Each tenant is a Helm release `cairn-<id>` in namespace `cairn-<id>`, labelled `cairn.io/tenant`. It gets:

- the HA profile and `values-tenant.yaml`,
- its own Kafka,
- its own databases (`cairn_<id>`, `cairn_catalog_<id>`) and role on the shared PostgreSQL server,
- an index prefix `<id>_` on the shared OpenSearch cluster,
- a ResourceQuota and network policies.

**Shared by every tenant:**

| Component | Notes |
|---|---|
| PostgreSQL | managed, multi-zone, point-in-time recovery on, pgvector installed. One server per region; split when CPU stays above 60% or storage above 70% |
| OpenSearch | managed, three or more nodes across zones; snapshots daily (the index can always be rebuilt from PostgreSQL, see Recovery) |
| Ingress | TLS for `*.<domain>`; HSTS is on in the product |
| Monitoring | Prometheus Operator: the chart's PodMonitor, PrometheusRule and dashboard (`cairn.serviceMonitor`, `cairn.prometheusRule`, `cairn.grafanaDashboard`) |

**Per tenant, in the shared secrets manager:**

- the licence file,
- the PostgreSQL role password (also in the namespace's Secrets),
- the gateway key Secret, if the tenant uses remote agents across replicas.

## Tenant lifecycle

**Provision:**

1. Issue the licence, bound to the tenant id: `deploy/ops/licence.py issue … --tenant <id>`.
2. Run `deploy/hosting/tenant.sh create <id> --licence <file> --values platform.yaml`. It takes about 15 minutes.
3. Check it:
   - `https://<id>.<domain>` loads;
   - **Settings → Licence** shows `valid`;
   - `kubectl -n cairn-<id> get pods` is all ready.
4. Hand over the admin password from Secret `cairn-core-secrets`, through the secure channel. Ask the customer to set up SSO and SCIM, then disable the local admin.

**Renew a licence:** `tenant.sh upgrade <id> --licence renewed.licence`. The release picks up the new Secret at start. The `CairnLicenceExpiring` alert fires 21 days before expiry.

**Offboard:**

1. Export the tenant's metadata for them if the contract says so: `kubectl -n cairn-<id> exec deploy/cairn-<id>-api -- cairn export metadata --to <their bucket>`.
2. Run `tenant.sh delete <id> --drop-data`. It removes the release, the namespace, both databases and the role.
3. Delete the tenant's indices (`DELETE /<id>_*` on OpenSearch) and its Kafka.
4. Point-in-time backups age out on their own; record the date they will be gone.
5. Send the customer a deletion confirmation listing what was removed and when the backups expire.

## Upgrades

Never upgrade every tenant at once:

1. **Rehearse** the release on a copy: `deploy/ops/rehearse-upgrade.sh <previous tag>`. It has to pass.
2. **Canary:** upgrade the internal tenant first, with `tenant.sh upgrade <id>`. The Helm setup hook migrates; the services migrate their own database at start, one at a time. Watch the dashboard for an hour: error rate, latency, workers.
3. **Waves:** upgrade 10% of tenants, then 50%, then the rest, with at least two hours between waves. Stop at the first error-rate alert and roll that tenant back.
4. **Roll back:** migrations are forward-only, so a rollback is the previous chart and images plus a restore (below). Prefer fixing forward, and only roll back on data-affecting faults.

Record each wave: release, tenants, start, end, problems. That record is the change-management evidence for SOC 2.

## Backups and recovery

**Targets** (set them per contract tier; these are the defaults to offer):

- RPO 15 minutes,
- RTO 4 hours for a single tenant,
- RTO 24 hours for a region.

| What | How it is protected | Restore |
|---|---|---|
| A tenant's databases | point-in-time recovery on the shared PostgreSQL server | restore the two databases to a new server or name, then point the release at them (`tenant.sh upgrade` with `CAIRN_PG_HOST` or `--values`) |
| Search index | rebuilt from the catalog database | run the setup image with `-u RestoreIndices -a clean` in the tenant's namespace (`helm get values` gives the env) |
| Gateway keys | the Secret, copied to the secrets manager | re-create the Secret; remote agents keep trusting the gateway |
| Licence and credentials | the secrets manager | re-create the Secrets |

**Single tenant: data deleted or corrupted by mistake.**

1. Choose the restore point from the tenant's audit log: the last good entry.
2. Scale the release to 0: `kubectl -n cairn-<id> scale deploy --all --replicas 0`.
3. Restore both databases to that point.
4. Rebuild the index.
5. Scale back up.
6. Check that `cairn db check` and `cairn audit verify` pass.

**Region lost.** Restore PostgreSQL in the standby region from cross-region backups, create OpenSearch and Kafka, then run `tenant.sh upgrade` for each tenant against the new endpoints and rebuild each index. Practise this once a year and keep the record (SOC 2 A1).

**Restore drill:** each quarter, restore one random tenant to a scratch namespace, run the smoke tests against it, and delete it.

## Alert response

Each alert has a severity: **critical** pages the on-call engineer; **warning** opens a ticket that needs looking at on the same business day; **info** needs no action.

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
| CairnAIBudgetReached (info) | usage per task | expected behaviour; raise the budget if the contract allows |
| CairnLicenceExpiring | `licence.json` for the tenant | issue and install the renewal (account management) |
| CairnLicenceReadOnly (critical) | Settings → Licence: missing, invalid or expired | install the licence; a key error after a rotation means the image's keys lack the signing key |
| CairnLicenceSeatsFull | seats used vs limit | tell account management; the newest users can still read |

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
3. Mint a short-lived admin token for that tenant.
4. Do the work.
5. Revoke the token.

Everything done through Cairn lands in the tenant's audit log, so attach the export to the ticket. Direct database access uses a temporary role with the session logged.

## Rotation

| Secret | How | When |
|---|---|---|
| Control-plane service token | rotated by the token keeper (`CAIRN_TOKEN_ROTATE_DAYS`) | automatic, every 7 days by default |
| PostgreSQL role password | set a new one, update both Secrets, then restart the release | yearly, and when staff leave |
| Extension tokens and signing secrets | `cairn extension rotate <name>` in the tenant | when the extension's operator asks, or on suspicion |
| Licence signing key | new key pair (`deploy/ops/licence.py keygen`), add the public key to the images, re-issue licences, then remove the old key in a later release | on compromise; otherwise every two years |
| TLS certificates | cert-manager | automatic |
