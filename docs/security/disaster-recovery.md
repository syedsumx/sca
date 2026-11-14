# Disaster recovery: targets, mechanisms, evidence and gaps

**Status:** draft for counsel and investors. The recovery targets below are targets, not measured service levels.
**Date:** 2026-11-14. **Basis:** the repository at commit `783865d7` (branch `claude/funny-meitner-qtz0ux`).

**Paths.** `pipelines/dr_failover.py` is relative to `cairn/src/cairn/`. `platform/...` manifests are relative to `cairn/deploy/hetzner/`. Other paths are relative to `cairn/`.

## 1. Definitions

- **Recovery point objective (RPO):** the most recent acknowledged writes that may be lost when a failure happens.
- **Recovery time objective (RTO):** the time from the failure until the service serves reads and writes again.

Every target below is labelled one of:

- **Target, not verified by a recovery test.** The figure is stated in a document. No test in the repository restores to that figure.
- **Lab-verified, not at target scale.** A test in the repository measures or asserts the figure, but on a small lab stack.
- **Open item.** No figure is stated, or the figure needs an owner.

## 2. Targets

| # | Scope | Target | Stated in | Label | Evidence and limits |
|---|---|---|---|---|---|
| T1 | One hosted tenant: RPO | 15 minutes | `docs/operations/hosting-runbook.md` ("Targets", "these are the defaults to offer") | Target, not verified by a recovery test | The mechanism is point-in-time recovery from archived WAL (section 3). The archive interval is not set in the repository (section 6, G3). |
| T2 | One hosted tenant: RTO | 4 hours | same | Target, not verified by a recovery test | The tenant restore is manual (runbook, "Single tenant"). No script exists for it, and no test runs it. |
| T3 | Whole region: RTO | 24 hours | same | Lab-verified, not at target scale for the 24-hour figure | `tests/test_dr_drill.py` asserts that recovery takes under 120 seconds on a two-region lab (store, catalog and database). It does not test the 24-hour target at production size. |
| T4 | Whole region: RPO | none stated | not stated | Open item | The drill records the commits lost (section 4). No target is set. `[TO CONFIRM: region RPO target]` |
| T5 | Automatic failover: time from loss to DNS switch | none stated | `docs/site/administration.md` (lab figure 1.4 s) | Lab figure, not asserted | The drill records the switch time in the audit log (`rto_s`). It asserts that the switch happened, not the value. |
| T6 | Object store (lakehouse data) RPO | none stated | not stated | Open item | Store replication lag is the RPO. It is monitored through `x-amz-replication-status` (`administration.md`) but has no target. |
| T7 | Search index and event streams | none stated | not stated | Open item | The search index is rebuilt from the catalog database; the rebuild time is not measured. Event streams are not in the drill (section 4). |

**Proposal.** Set each target per contract tier, state the measurement method, and run the test that measures it. Section 8 lists the tests.

## 3. Mechanisms that exist

**Hosted cluster (Hetzner, `cairn/deploy/hetzner`).**

- **Cairn's own PostgreSQL.** A CloudNativePG cluster (`platform/postgres.yaml`) with replicas on other nodes. WAL is archived continuously to object storage. A base backup runs nightly (cron `0 30 2 * * *`, `platform/postgres-scheduled-backup.yaml`), and backups are kept 30 days (`platform/postgres-backup.yaml`, `retentionPolicy: "30d"`). The object-storage endpoint and region come from `cluster.env` (`OBJECT_STORAGE_ENDPOINT`, `OBJECT_STORAGE_REGION`), a single location.
- **Kubernetes state.** etcd snapshots every six hours to object storage (`deploy/hetzner/README.md`). Restoring etcd is a manual `k3s server --cluster-reset` (README).
- **Tenant lakehouse.** Continuous backup of each tenant's Cairn Store to `<prefix>-tenants` is a manual setup step (`deploy/hetzner/README.md`, "Backups"; the Cairn Store's "Backups and restore" section of `docs/site/administration.md`).
- **Tenant OpenSearch and Kafka.** Replicated across three nodes when the tenant is created with `CAIRN_TENANT_SERVICES_HA=1`. This covers node loss, not region loss (ROADMAP L68; `tests/test_tenant_services_ha.py`; `deploy/hetzner/ci/k3s-smoke.sh`). OpenSearch has no snapshot schedule in the repository; the index is rebuilt from PostgreSQL.

**Compose deployment (`cairn/deploy/ops`).**

- `backup.sh` takes consistent snapshots of the Cairn database (`pg_dump`) and the catalog database (`mysqldump`, or `pg_dump` with the PostgreSQL compose file), and copies the gateway keys and configuration. It does not back up the search index. `docs/site/administration.md`, "Backup and restore", describes it.
- `restore.sh` replaces both databases and the gateway keys, rebuilds the search index, and restarts the services. Anything written after the backup is lost.
- Neither script is run by a CI job or referenced by a test in the repository (section 6, G5).

**Database instances (customer databases, `docs/site/administration.md`, "Backups and restore").** Schedules with retention, restore verification, point-in-time restore into a new instance, and branches. Tested by `tests/test_postgres_instances.py` (trino CI job): a backup is restored to a throwaway instance, and a point-in-time restore has the rows written before the target time and not after. **This covers databases Cairn provisions for applications. It does not cover Cairn's own control-plane database.**

**Lakehouse failover (`cairn dr`, `cairn/src/cairn/pipelines/dr_failover.py`).**

- `cairn dr reconcile`: after the standby takes over, moves any table whose newest files never reached the standby back to its newest whole commit. It reports first; `--apply` repairs. A table with no whole commit left is reported as unrecoverable.
- `cairn dr watch`: runs in the standby region. It probes the primary every 10 seconds and fails over only when every primary check has failed three rounds in a row (minimum two). It fails over only onto a healthy standby. It promotes the standby PostgreSQL databases, repoints DNS through RFC 2136 updates signed with TSIG, and writes a state file. It never fails back on its own.
- Settings are refused at start-up if they could make the watcher act wrongly (fewer than two failures, an unknown check scheme, a DNS TTL above 3600 seconds, an unreadable secret).

## 4. Evidence: the recovery drill

`tests/test_dr_drill.py` runs in the `trino` CI job (`.github/workflows/cairn-ci.yml` at the repository root). It needs Docker with `postgres:16-alpine`, and the store and catalog binaries.

**What the drill does.** Two regions, each with a real store, PostgreSQL and catalog. The primary's store replicates to the standby's; the primary's database streams to the standby. Ten commits are written. The standby store is frozen for the last four, as on a slow link. The primary region is then killed (store, catalog and database). The standby database is promoted, a catalog starts on it, and `cairn dr reconcile` runs.

**What it asserts.**

- The report finds one table to roll back, and the rollback matches the commits actually missing on the standby (the reported loss is consistent with the data).
- The commits present on the standby are a prefix of those acknowledged, each readable in full, and at least six of them are present (everything replicated before the link slowed).
- Recovery takes under 120 seconds (`assert rto < 120`).
- After the failover the standby accepts a write and reads it back.

**What it prints but does not assert.** The RPO and RTO values reported elsewhere in the repository: "RPO 4 of 10 commits (4.3 s)" and "RTO about 2 s" (ROADMAP L9; `docs/site/administration.md`). Those are the values printed in a run. A future run that loses more commits would still pass unless the consistency check fails.

**Automatic failover test** (same file). A real BIND server takes the DNS updates and Cairn's own database is on the primary cluster with audited work. The test asserts that the watcher refuses to act while the standby is unhealthy, that once the standby answers the watcher promotes it and switches `api` and `lake` to the standby, that the audit chain verifies with five entries and then six after the failover, and that a second run reports the failover already done. It records the switch time (`rto_s`) without asserting a bound. `tests/test_dr_failover_config.py` checks the settings rules without the databases.

**Limits of the evidence.** The drill uses a small dataset on one machine per region. It covers the lakehouse (store, catalog, database). It does not cover Kafka, OpenSearch, Trino, the Helm release, DNS propagation to clients, or Cairn's own control-plane restore from the nightly base backup.

## 5. Procedures a responder would follow

These come from the runbook and README, and are manual. No repository test runs any of them end to end.

- **Single tenant, data deleted or corrupted:** choose the restore point from the tenant's audit log, scale the release to zero, restore both databases to that point, rebuild the index, scale up, then run `cairn db check` and `cairn audit verify` (`hosting-runbook.md`, "Single tenant").
- **Restore PostgreSQL on the hosted cluster:** recreate the `cairn-pg` cluster with `bootstrap.recovery` from the same object-storage backup (`deploy/hetzner/README.md`, "Restoring PostgreSQL").
- **Restore etcd:** `k3s server --cluster-reset --cluster-reset-restore-path=<snapshot>` on one node, then rejoin the others (`deploy/hetzner/README.md`).
- **Region lost:** restore PostgreSQL in the standby region from cross-region backups, create OpenSearch and Kafka, run `tenant.sh upgrade` for each tenant against the new endpoints, and rebuild each index. The runbook states that no script in the repository does this (section 6, G1).
- **Failover of the lakehouse:** run `cairn dr reconcile`, then `--apply` if needed, point clients at the standby, and later rebuild the old primary as the new standby (`docs/site/administration.md`, "Disaster recovery across regions", failover runbook).

## 6. Gaps and inconsistencies

**G1. The runbook and the roadmap disagree about region recovery.** `hosting-runbook.md` says: "No script in the repository does this" for a lost region, and the region-loss procedure is manual. ROADMAP L9 (✅) and `docs/site/administration.md` describe `cairn dr reconcile` and `cairn dr watch`, which are in `pipelines/dr_failover.py` and `cli.py`. The runbook covers the Cairn-level failover poorly or not at all. `[TO CONFIRM: owner to update hosting-runbook.md to describe cairn dr]`

**G2. No cross-region copy of Cairn's own database is configured in the repository.** The runbook's region-loss step assumes PostgreSQL backups exist in the standby region. The hosted cluster writes to one object-storage endpoint and region (`cluster.env.example`). `[TO CONFIRM: where the cross-region copy of the nightly base backups and the WAL is kept, and by what mechanism]`

**G3. The archive interval is not set.** The 15-minute RPO target needs WAL to reach object storage at least every 15 minutes in quiet periods. `archive_timeout` does not appear in the repository's PostgreSQL manifests. `[TO CONFIRM: archive_timeout in effect on the cairn-pg cluster]`

**G4. Restore procedures are not tested.** Neither the tenant restore nor the hosted PostgreSQL restore has a test in the repository. The runbook sets a quarterly restore drill of one random tenant and an annual region drill. No record of either is in the repository. `[TO CONFIRM: records of restore drills, and the dates of the last ones]`

**G5. Compose backup and restore scripts are not in CI.** `deploy/ops/backup.sh`, `restore.sh` and `rehearse-upgrade.sh` are not referenced by a job in `cairn-ci.yml` or a test in `tests/`.

**G6. Store keyring backup.** The administration manual says a restored store needs its keyring, and to keep the keyring separately. The repository has no procedure for backing up the keyring or the Vault or KMS credentials it depends on. Losing them makes encrypted objects unreadable. `[TO CONFIRM: keyring backup procedure and owner]`

**G7. Retention windows.** PostgreSQL backups are kept 30 days (`postgres-backup.yaml`). A point-in-time restore is possible only within the retained window, so a corruption found after 30 days cannot be restored from these backups. `[TO CONFIRM: whether the retention is the contracted window]`

**G8. Kafka and OpenSearch recovery.** The drill does not include Kafka. Events in flight during a region loss are not covered by any test. OpenSearch has no snapshot schedule in the repository; its rebuild time is not measured.

**G9. The drill's figures are not asserted.** The RPO and RTO figures in ROADMAP L9 are printed by the drill but not asserted. A regression in replication lag would not fail the test unless the RTO bound is exceeded.

**G10. etcd retention.** Snapshots are taken every six hours. The number retained is not stated. `[TO CONFIRM: etcd snapshot retention]`

## 7. Responsibilities

`[TO CONFIRM: named owner for each recovery procedure, the on-call rota that runs it, and who declares a disaster]`

## 8. Proposed verification plan (not executed)

Proposals for owners to accept or reject. None of these is in the repository.

- **P1. Timed tenant restore.** In CI, write a marker table on a tenant's database every minute, restore to a point in time, and measure how much of the marker data is missing (RPO) and how long the restore takes (RTO). Assert against T1 and T2.
- **P2. Cairn database restore.** Restore Cairn's own control-plane database from the nightly base backup in a scratch namespace, run `cairn db check` and `cairn audit verify`, and record the time. This closes G4 for the control plane.
- **P3. Compose backup and restore in CI.** Add `deploy/ops/backup.sh` and `restore.sh` to a CI job, and assert that the audit chain verifies after restore (closes G5).
- **P4. Cross-region drill.** Run `cairn dr` between two hosted locations, measuring T3, T5 and T6, and record the result in this document.
- **P5. Asserted drill bounds.** Change `tests/test_dr_drill.py` to assert the RPO and RTO values the roadmap states, with a tolerance, so that a regression fails CI (closes G9).

## 9. Related assessments (linked, not restated)

- SOC 2 readiness mapping, including the availability criteria: [`cairn/docs/operations/soc2-readiness.md`](../../cairn/docs/operations/soc2-readiness.md)
- External penetration test scope: [`cairn/docs/operations/pentest-scope.md`](../../cairn/docs/operations/pentest-scope.md)

Those documents set out the audit mapping and the external test scope. This document does not restate their conclusions.
