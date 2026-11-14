# Data governance controls: evidence and open items

**Status:** draft for counsel and the board. Not an audit opinion, and not legal advice.
**Date:** 2026-11-14. **Basis:** the repository at commit `783865d7` (branch `claude/funny-meitner-qtz0ux`).

**Paths.** Module paths such as `governance/pdp.py` are relative to `cairn/src/cairn/`. Other paths are relative to the `cairn/` folder of the repository unless they start with the repository's own top-level folder.

## 1. Purpose and how to read this document

This document says which data governance controls the Cairn platform implements, where the evidence is, and what is still open. It is written for counsel and investors reviewing the product, so each statement carries one of these labels:

- **Verified in code**: the module that implements the behaviour was read, from its description and, where cited, its implementing code. Where a test file exists it is named; test files were located but their cases were not all reviewed, and no test suite was executed for this document.
- **Documented**: stated in the product manual (`cairn/docs/site/`) or the architecture document, but not re-read in code for this document.
- **Target**: an intended outcome that no test in the repository verifies.
- **Open item**: a gap, an unresolved design question, or a placeholder `[TO CONFIRM: ...]` that needs an owner.

Where the platform's own documents disagree with the code, the disagreement is listed in section 13 and is not resolved here.

## 2. Policy decision point (PDP)

All governed access is decided by one policy decision point, `cairn/src/cairn/governance/pdp.py`, which combines role-based grants (`rbac.py`) with attribute-based rules (`abac.py`).

- **Verified in code.** Explicit DENY overrides ALLOW. With no applicable rule, the decision is DENY in authoritative mode (`pdp.py`, "default deny" branch). In additive mode the request falls through to Cairn Core's own authorizer, which can only grant.
- **Verified in code.** Decisions are cached for 30 seconds by default (`cache_ttl_seconds`). An attribute change invalidates the cache through a metadata change event consumer (`governance/invalidation.py`; `tests/test_mcl_invalidation.py`).
- **Verified in code.** `POST /governance/v1/authorize?explain=true` returns every rule evaluated with its match trace, which the access simulator in the web UI uses.
- **Fail closed.** A privilege the policy engine cannot decide is reported as not held (`cairn/src/cairn/identity/capabilities.py`). The Java authorizer plugin for Cairn Core is fail-closed with a circuit breaker (ROADMAP item 2.4).
- **Authoritative mode** (`CAIRN_NATIVE_POLICIES_ENABLED=false`) makes Cairn the only authorizer, so deny rules are enforced. The security posture scan flags additive mode as a finding (section 12).

**Built-in roles** (`rbac.py`, `BUILTIN_ROLES`): `Admin` grants `*`; `Editor` grants view, search, edit and glossary privileges; `Reader` grants entity page and dataset view privileges. Platform privileges that are not about one asset are listed in `identity/capabilities.py` (`PLATFORM_PRIVILEGES`, 15 entries).

**Open item (mask enforcement).** `mask_columns` obligations are enforced by the Cairn SQL proxy (`cairn/src/cairn/coreproxy/masking.py`). Field-level redaction in Cairn Core's GraphQL responses is not built: ROADMAP item 2.10 is marked ⬜ planned. A caller who reaches Cairn Core's GraphQL API directly is therefore not masked by this mechanism. `[TO CONFIRM: which access paths to customer data bypass the SQL proxy in a standard deployment]`

**Open item (policy as code).** Policy validation and simulation fixtures in Git (ROADMAP item 2.11) are marked ⬜ planned.

## 3. Classification and PII

- **Documented.** The PII classifier combines column-name heuristics with value patterns (email, phone, SSN, payment card with Luhn check, IBAN, IP address) and produces a confidence score; values above `auto_apply_threshold` are applied automatically (`ARCHITECTURE.md` §1.3.2; module `governance/pii.py`).
- **Verified in code.** Classification propagates along column-level lineage. The propagation action (`governance/propagation_action.py`) triggers on schema and lineage change events, walks downstream up to `max_hops` (the example configuration uses 5), and stops at blocking transforms (`HASH`, `AGGREGATE`, `ENCRYPT`). Propagated writes are attributed and can be reverted.
- **Documented, live-tested.** PII terms and rule-based tags propagated over column lineage through Kafka in the live end-to-end suite (`tests/e2e/test_active_metadata_live.py`; requires a running stack).

## 4. Lineage

Lineage comes from three sources.

- **SQL-derived column lineage.** `context/column_lineage.py` traces each output column of `INSERT ... SELECT`, `CREATE TABLE ... AS SELECT` and view definitions back to physical columns, using the catalogued schemas. It merges the result into the target's upstream lineage without removing edges a crawler or a person wrote, and keeps the statement that produced each edge. **Limit:** only statements Cairn observes (its query log and view definitions) are traced. Statements Cairn never sees produce no lineage.
- **OpenLineage events** are recorded as change events with source `openlineage` (`pipelines/changes.py`; `tests/test_openlineage.py`).
- **Engine-side lineage in Trino system tables.** Table and column lineage, including Trino's, is queryable as `cairn_system.lineage.*` (ROADMAP L134, status 🟡: the Helm chart does not yet deploy the system tables; `tests/test_system_tables_e2e.py`).

## 5. Data quality

- **Verified in code.** Monitors run as a pipeline: collect, detect, persist, emit, then the incident lifecycle (`quality/engine.py`). External quality results are accepted (`quality/external.py`). Contracts are in `quality/contracts.py`.
- **Documented.** A failing critical monitor can automatically revoke a certification, if configured (`ARCHITECTURE.md` §1.3.3).
- **Tests.** `tests/test_quality_engine.py`, `tests/test_external_quality.py`, `tests/test_quality_latest_statuses.py`.

## 6. Certification and approval workflows

- **Verified in code** (`governance/certification.py`). The states are DRAFT, SUBMITTED, IN_REVIEW, APPROVED, CERTIFIED, EXPIRED, REVOKED, plus REJECTED and CHANGES_REQUESTED. Approval needs a quorum of distinct eligible reviewers. The requester cannot approve (separation of duties). Any eligible reviewer can veto. Entry checks gate APPROVED to CERTIFIED. Every transition is written to the workflow history and to the audit chain.
- **Documented.** On CERTIFIED the platform writes the structured property `io.cairn.certification.status` and the tag `Certified`; the certification expires at `valid_until`.
- **Tests.** Certification cases appear in `tests/test_governance.py` and `tests/test_active_metadata.py`.

## 7. Access reviews (ROADMAP L71, ✅)

- **Verified in code and tested** (`governance/access_reviews.py`; `tests/test_access_reviews.py`, run on SQLite and PostgreSQL with the real PDP). A campaign takes every active, time-boxed grant in its scope: persona grants and approved marketplace requests. Reviewers keep or revoke each item with a comment. Nobody reviews their own access. A campaign in which some grant has no eligible reviewer is refused. A revoke takes effect at once.
- **Default on no response: revoke.** Undecided items are revoked at the due date unless the campaign says `keep`. Reminders are sent daily by the scheduler. Every decision is audited.
- **Scope limit.** Per the module's own description, campaigns cover time-boxed grants. Standing role bindings are not described as in scope. `[TO CONFIRM: whether standing RBAC role bindings must be reviewed on a schedule, and by whom]`
- **Posture check.** The security posture scan flags grants that exist when no access review has closed in the last 180 days (section 12).

## 8. Privacy operations: subject requests, retention, legal holds, erasure

**Subject requests** (`governance/privacy.py`). A data protection officer (`MANAGE_POLICIES`) opens a request for one person, identified by email, phone or customer identifier. Cairn finds the columns classified for that identifier kind and, where a warehouse connection serves the dataset, counts that person's rows through data access. Access requests end in a report. Deletion requests give each dataset to its owner with the statement to run, are due in 30 days (`due_days`), and send reminders. The identifier is stored as a salted hash and a masked hint.

**Retention** (`governance/records.py`). A retention policy with `"enforce": true` deletes rows older than its limit, on Trino, as `CAIRN_PRIVACY_ACTOR`. It runs with the daily privacy sweep or on demand. Every run is recorded, including runs that were skipped (held table, no time column, refused). Without `CAIRN_PRIVACY_ACTOR`, enforcement and erasure answer 503 (`administration.md`, "Records").

**Legal holds** (`records.py`). A hold on a table, with its case and reason, makes Trino refuse every statement that removes or rewrites the table's rows or history, for everyone including administrators: `DELETE`, `UPDATE`, `MERGE`, `TRUNCATE`, `DROP`, renames, column drops and table procedures such as expiring snapshots. Retention skips held tables. Holds are released, never deleted; both actions are audited.

**Erasure with proof** (`records.py`). For a deletion request, the officer re-sends the identifier (Cairn checks its hash). For each table on a Trino connection, Cairn counts the person's rows, deletes them, counts again (the count must be 0), and expires the table's older snapshots. The snapshot step depends on Trino's `iceberg.expire-snapshots.min-retention`; if that does not allow it, the proof says the snapshots remain. The default for `CAIRN_ERASURE_SNAPSHOT_RETENTION` is `0s`. The signed proof is a JWS that anyone can verify with the published key (`GET .../proof-keys`).

- **Tests.** Holds, retention and erasure are exercised by `tests/test_trino_e2e.py` and `tests/test_trino_access.py` (trino CI job). `tests/test_privacy.py` exists for the request workflow; its cases were not reviewed for this document.
- **Limit.** Erasure reaches only tables on Trino connections. Tables on other connections, and tables on hold, are listed for their owners.
- **Open item (wording).** Two statements about deletion disagree. The `privacy.py` module description says "Cairn never deletes data itself", while the erasure endpoint and the product manual (`docs/site/governance.md`) say Cairn deletes on Trino when the officer asks. See section 13.
- **Open item (default).** `CAIRN_ERASURE_SNAPSHOT_RETENTION=0s` means that, where Trino allows it, erasure removes the table's older snapshots at once. `[TO CONFIRM: the retention setting counsel and the DPO want as the default, and the Trino minimum-retention value in production]`

**Target (not verified at scale).** Retention and erasure are described as correct for every covered table. The repository tests run them against a small test lakehouse only.

## 9. Consent

- **Verified in code** (`governance/consent.py`; `tests/test_consent.py`). A purpose that needs consent (opt-in or opt-out) names its members (users, groups, personas) and the identifier kind that links a row to a person. Withdrawn consent is recorded from a consent platform or from the person. Rows of a withdrawn person are filtered for members of the purpose, through a Trino row filter or by rewriting queries sent to a direct warehouse connection.
- **Design point for counsel.** Consent records store the SHA-256 of the normalised identifier, unsalted, on purpose: each warehouse computes the same hash of its own column to match it. Low-entropy identifiers, such as email addresses, can be matched against a candidate list by anyone who has both the hash and that list. `[TO CONFIRM: DPO assessment of unsalted identifier hashes as personal data]`

## 10. Data residency

- **Verified in code** (`governance/residency.py`; `tests/test_residency_e2e.py`, trino CI job). Namespaces can be pinned to a region. Cairn Catalog keeps a pinned namespace's tables under that region's storage location and refuses any other location for them, including on rename, move or clone. `allow` lists the regions the data may be processed in and sent to.
- **Limit.** The rules are a deployment file (`CAIRN_RESIDENCY_FILE`), not an API. Enforcement depends on the deployment's Trino, Spark and catalog engines honouring the pin, as the residency test covers them.

## 11. Audit log and SIEM

- **Verified in code** (`common/audit.py`). Each audit entry stores `sha256(prev_hash || canonical_json(entry))`. `verify_chain` detects an altered or removed entry. The API route is `GET /governance/audit/verify` and the command is `cairn audit verify`.
- **Limit: no external anchor.** The chain is stored in the same database as the entries it protects. The module description itself says stronger guarantees need the head hash anchored outside the database (for example in object-lock storage). Nothing in the repository does that. A person who controls the database could rewrite the chain from the altered entry onward, so the chain shows tampering only against a copy held elsewhere. `[TO CONFIRM: whether an external anchor will be operated, and where]`
- **Verified in code and tested.** The audit log is exported to SIEM sinks (Splunk HEC, syslog RFC 5424 and CEF over TLS, HTTPS) with ordered at-least-once delivery and a cursor per sink (ROADMAP L26, ✅; `tests/test_siem_e2e.py`, `tests/test_audit_export.py`).
- **Coverage.** Policy denies, approvals, agent enrolment and LLM-generated writes are audited (`ARCHITECTURE.md` §1.1, cross-cutting design; §1.5 for the AI writes). Prompts are kept only if `CAIRN_AI_AUDIT_PROMPTS=true` (default false).

## 12. Continuous posture checks

`governance/posture.py` runs a set of configuration checks daily and on demand, scores them, and alerts `CAIRN_SECURITY_CONTACTS` on new high or critical findings. The checks are:

- `trusted_header`: the development trust-actor header is on, so any caller can claim an identity.
- `authz_mode`: the PDP only adds to Cairn Core's grants.
- `open_policy`: an allow policy for every action, or for reading data, with no condition and no subjects.
- `pii_unmasked`: sensitive columns exist and no policy masks anything.
- `audit_chain`: the audit hash chain does not verify.
- `long_shares` and `idle_shares`: outside shares older than 180 days, or unused for 90 days.
- `no_access_review`: grants exist and no access review has closed in 180 days.
- `residency_region`: pins exist and `CAIRN_REGION` is unset.
- `ai_budget`: a model is configured and no daily AI budget is set.
- `secrets_key`: secret scopes hold values with no rotation partner key, or a previous key lingers after a rotation.

**Gap.** The checks do not include whether the object store has an encryption key (section 7 of the security posture document, `../security/security-posture.md`).

## 13. Inconsistencies and gaps found while writing this document

These are reported, not corrected. Each is also listed in the security posture or disaster recovery documents where it applies.

1. **Deletion wording.** `governance/privacy.py` says "Cairn never deletes data itself"; `governance/records.py` erasure and `docs/site/governance.md` say Cairn deletes rows on Trino when asked.
2. **Roadmap evidence.** ROADMAP L50 (retention, legal hold, erasure) is marked ✅ with no test reference in its row. Tests exist (section 8), but the row does not cite them. The roadmap's own L185 row records that a capability audit found shipped rows it could not match to a test.
3. **Access review scope.** ROADMAP L71 describes reviews "over the access people hold"; the module covers time-boxed grants only (section 7).
4. **Mask enforcement.** ROADMAP 2.10 (Core GraphQL redaction) is open; `ARCHITECTURE.md` §1.3.1 describes `mask_columns` obligations without stating the open path.

## 14. Open items

| # | Item | Owner |
|---|---|---|
| G1 | Confirm which data access paths bypass the SQL proxy in a standard deployment (section 2) | [TO CONFIRM: owner] |
| G2 | Decide on an external audit-chain anchor (section 11) | [TO CONFIRM: owner] |
| G3 | Confirm the erasure default and the Trino minimum retention in production (section 8) | [TO CONFIRM: DPO] |
| G4 | Resolve the deletion wording (section 13, item 1) | [TO CONFIRM: owner] |
| G5 | Decide whether standing role bindings enter access reviews (section 7) | [TO CONFIRM: owner] |
| G6 | DPO assessment of unsalted identifier hashes (section 9) | [TO CONFIRM: DPO] |
| G7 | Named owner and schedule for governance evidence, including quarterly access review campaigns | [TO CONFIRM: owner] |

## 15. Related assessments (linked, not restated)

- SOC 2 readiness mapping: [`cairn/docs/operations/soc2-readiness.md`](../../cairn/docs/operations/soc2-readiness.md)
- External penetration test scope: [`cairn/docs/operations/pentest-scope.md`](../../cairn/docs/operations/pentest-scope.md)

Those documents set out the audit mapping and the external test scope. This document does not restate their conclusions.
