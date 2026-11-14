# Security posture: controls, evidence and gaps

**Status:** draft for counsel and investors. Not an audit opinion or a penetration test result.
**Date:** 2026-11-14. **Basis:** the repository at commit `783865d7` (branch `claude/funny-meitner-qtz0ux`).

## 1. Scope and how to read this document

This document describes the security controls of the Cairn platform as they are built in this repository, the evidence for each, and the gaps. Labels used:

- **Verified in code**: read in the implementing module; test files named where they exist. The test suites were not executed for this document.
- **Documented**: stated in the product manual (`cairn/docs/site/`), the README or the architecture document, and not re-read in code here.
- **Target**: intended, not verified by a test in the repository.
- **Default**: the value a fresh deployment gets, from the Helm chart or code defaults. Defaults decide what most deployments are, so they are stated separately.
- **Open item**: a gap or a `[TO CONFIRM: ...]` placeholder.

Cairn is self-hosted or run as hosted tenants. In self-hosted deployments the customer makes most of the configuration choices below, and their defaults matter most. In hosted tenants the operator makes them.

## 2. System and trust boundaries

| Component | Language | Role | Trust |
|---|---|---|---|
| Cairn API and scheduler (`cairn/src/cairn`) | Python | Control plane: policy decisions, governance, workflows, audit | Holds the service token and PostgreSQL |
| Cairn Core (modified DataHub 1.8.0) | Java | Metadata graph, native policies, PAT validation | Source of truth for personal access tokens |
| Cairn Store (`cairn/store`) | Rust | S3-compatible object store | Holds data at rest; optional encryption |
| Cairn Catalog (`cairn/catalog`) | Rust | Iceberg REST catalog | A deployment where the catalog vends store keys alongside Cairn-issued tokens is refused (ROADMAP principles) |
| Trino | Java (upstream) | Query engine; access checked against the PDP | No database credentials |
| Remote agents (`cairn/src/cairn/agent`) | Python | Ingest from customer networks; outbound HTTPS only | Hold source credentials; hold no Cairn Core token |
| Hosted tenants (`cairn/deploy/hosting`) | Kubernetes | One release per tenant namespace | Shared PostgreSQL server; see section 9 |

## 3. Identity and authentication

- **Verified in code** (`cairn/src/cairn/common/security.py`). Three credential types are accepted: a service token (compared in constant time); a Cairn Core personal access token, validated by asking Cairn Core with that token, so Cairn Core decides validity and revocation; and UI session cookies. Agent keys (`ca_`), OAuth access tokens (`cat_`) and command-line tokens (`cla_`) resolve to a person. Successful validations are cached for 60 seconds, keyed by the SHA-256 of the credential.
- **Verified in code and tested.** SAML 2.0 single sign-on (ROADMAP L47, ✅; `tests/test_saml_e2e.py`). SCIM user provisioning (`tests/test_scim.py`). Passkeys (WebAuthn) and step-up tokens that last five minutes for the most sensitive actions (ROADMAP L83, ✅; `tests/test_passkeys.py`).
- **Open item.** `[TO CONFIRM: whether password sign-in is available in each deployment mode, and whether multi-factor authentication is enforced for it]`
- **Bootstrap.** The administrator password is used once, at bootstrap, to mint the service accounts' credentials (`deploy/bootstrap/mint_core_token.py`).

## 4. Authorization and privileges

- **Policy decisions.** The PDP, RBAC and ABAC are described in the governance document (`../compliance/governance.md`, section 2). Explicit DENY overrides ALLOW; no applicable rule means DENY in authoritative mode. Decisions the engine cannot make are refused.
- **Service accounts.** The control plane runs as `cairn-service`. Its privileges are fixed in `deploy/bootstrap/mint_core_token.py`: 10 platform privileges and 16 metadata privileges, held through two non-editable policies. **Verified in code.** Policy, user, credential, secret and token administration, entity deletion and system operations are not in its list. The list does include `EDIT_ENTITY` and `EDIT_LINEAGE` on metadata, and `VIEW_ALL_QUERIES`, which the context miner needs. Whether those grants are limited by resource in the policy is `[TO CONFIRM: resource scope of cairn-service's metadata policies]`.
- **Provisioning account.** `cairn-provisioner` manages people and groups for SCIM only, and cannot change its own group membership.
- **Built-in roles** (`cairn/src/cairn/governance/rbac.py`): `Admin` (all), `Editor`, `Reader`.
- **Token rotation.** The control plane's service token is rotated every 7 days by default (`CAIRN_TOKEN_ROTATE_DAYS`), and the previous token is revoked after 15 minutes (`CAIRN_TOKEN_GRACE_MINUTES`), by `deploy/bootstrap/mint_core_token.py`. **Verified in code.** Tests: `tests/test_token_keeper.py`, `tests/test_tokens.py`.

## 5. Secrets and key management

- **Remote agents.** Recipes reference secrets as `${NAME}` and are resolved only on the agent (environment, file, Vault or AWS Secrets Manager). The gateway refuses any task that carries an inline secret (`ARCHITECTURE.md` §1.5).
- **Secret scopes for people's notebooks and jobs** (`cairn/src/cairn/identity/secret_scopes.py`; ROADMAP L48, ✅; `tests/test_secret_scopes.py`). Values are never shown again after entry.
- **Store encryption keys.** The object store can wrap its data keys with Vault Transit, AWS KMS, Azure Key Vault, Google Cloud KMS or KMIP-compatible HSMs (ROADMAP L22, ✅; `store/conformance/test_kms_aws.py`, `store/conformance/test_kms_cloud.py`).
- **Secrets in Kubernetes.** Credentials are Kubernetes Secrets. The chart expects them to be created by the operator or by an external secret manager (`helm/cairn/values.yaml`).
- **Open item.** `[TO CONFIRM: rotation procedure and owner for the store keyring and CAIRN_SECRETS_KEY]`

## 6. Encryption in transit

| Path | Default in the Helm chart | Source |
|---|---|---|
| Trino to clients | Refused without TLS unless `allowInsecureHttp` is set | `helm/cairn/values.yaml` (trino tls) |
| Edge (Compose deployments) | HTTP is served; HTTPS (TLS 1.2+, HSTS) is added when a certificate is in `deploy/tls/`; redirect to HTTPS only with `CAIRN_TLS_REDIRECT=true` | `docs/site/administration.md` (HTTPS) |
| Kubernetes ingress or Gateway API | TLS is the ingress's or the Gateway's job; cert-manager supported | `docs/site/administration.md`, `CAIRN_TLS_ISSUER` |
| Cairn Store HTTP and S3 API | **Off** (`cairn.store.tlsSecret` is empty) | `helm/cairn/values.yaml` |
| Cairn Store node-to-node | TLS when configured; **mutual TLS off** (`mutualTls: false`) | `helm/cairn/values.yaml` |
| Remote agent gateway | **Off** (`tls.secretName` is empty); mutual TLS only when a client CA is set | `helm/cairn/values.yaml` |
| Remote agent payloads | End to end, independent of TLS: X25519 key agreement, HKDF-SHA256, AES-256-GCM | `ARCHITECTURE.md` §1.5; `docs/site/agent-security.md` |

**Open item (defaults).** The store and the gateway serve plain HTTP in a default chart install. A deployment that does not set the TLS settings sends store and gateway traffic unencrypted inside the cluster. `[TO CONFIRM: whether the hosted and reference deployments always set these, and whether the default should change]`

## 7. Encryption at rest

- **Verified in code** (`store/src/crypt.rs`). Each object has its own random 256-bit data key. Object bytes are encrypted with AES-256-GCM in chunks. Each chunk is authenticated, so reordered, altered or truncated data fails to decrypt. Data keys are stored only wrapped by a key from the store's keyring.
- **Default: off.** Encryption applies to new objects only when a key is configured (`CAIRN_STORE_MASTER_KEY` or `CAIRN_STORE_KEYRING_FILE`, or an external key manager). Without one, objects are stored in the clear (`docs/site/administration.md`, "Encryption at rest").
- **Not checked by the posture scan.** The continuous posture scan does not test whether the store has a key (governance document, section 12).
- **Backups.** A restored store needs its keyring. `docs/site/administration.md` says to keep the keyring separately from the data.
- **PostgreSQL.** Encryption at rest for the Cairn database and the catalog database depends on the storage the operator provides. `[TO CONFIRM: storage encryption for the PostgreSQL volumes in the hosted cluster]`
- **Lost keyring.** If the keyring is lost, encrypted objects cannot be read; `docs/site/administration.md` says a restored store needs it.

## 8. Audit, monitoring and posture

- **Audit log.** Append-only and hash-chained (`sha256(prev_hash || canonical_json(entry))`). The chain is verified by `cairn audit verify` and `GET /governance/audit/verify`. **Limit:** the chain is not anchored outside its database, so it detects tampering only against a copy held elsewhere. See the governance document, section 11.
- **SIEM export.** Splunk HEC, syslog (RFC 5424 and CEF over TLS) and HTTPS sinks, ordered and at-least-once (ROADMAP L26, ✅; `tests/test_siem_e2e.py`).
- **Posture scan.** Daily, and on demand, with ten configuration checks, a score, and alerts for new high or critical findings (`cairn/src/cairn/governance/posture.py`; `tests/test_posture.py`). The governance document lists the checks.
- **Metrics and dashboards.** Operational dashboards and alert rules are in the Helm chart (`docs/site/administration.md`). The hosting runbook defines the alerts an operator should watch (`docs/operations/hosting-runbook.md`).

## 9. Hosted tenants: isolation

Each hosted tenant is one Helm release in its own namespace, `cairn-<id>`, with its own databases (`cairn_<id>`, `cairn_catalog_<id>`) owned by its own role (`docs/operations/hosting-runbook.md`; `deploy/hosting/tenant.sh`).

- **Network isolation is conditional.** The NetworkPolicy in `deploy/hosting/isolation.yaml` is rendered only when `CAIRN_INGRESS_NAMESPACE` is set (`deploy/hosting/tenant.sh`, `render_services`). When it is rendered, nothing from another tenant's namespace reaches the tenant, and only the release, the ingress controller and Prometheus reach its pods. The isolation step of `deploy/hetzner/ci/k3s-smoke.sh` runs cross-tenant probes on k3s in CI (the `hetzner` job). Docker Compose deployments do not use this policy.
- **Pod security is off by default.** Pod Security levels are applied to tenant namespaces only when `CAIRN_POD_SECURITY` is set (`tenant.sh`, `CAIRN_POD_SECURITY`).
- **Shared PostgreSQL.** One PostgreSQL server holds every tenant's databases in a region (`hosting-runbook.md`, "Shared by every tenant"). Separation is by database and role. A server-level compromise or a superuser error reaches all tenants on that server. `[TO CONFIRM: whether tenants are placed on separate servers above a size or sensitivity threshold]`
- **Shared OpenSearch.** When `CAIRN_TENANT_SERVICES` is not set, tenants share an OpenSearch cluster and are separated by an index prefix (`<id>_`). That is logical separation only.
- **Own OpenSearch and Kafka.** Set `CAIRN_TENANT_SERVICES=1` to give each tenant its own, and `CAIRN_TENANT_SERVICES_HA=1` for replicated three-node services (`tests/test_tenant_services_ha.py`). This must be chosen when the tenant is created; an existing tenant is not switched in place (`tenant.sh`).
- **Penetration test.** The external penetration test scope lists tenant isolation as its first objective (section 16).

## 10. Remote agents

Remote agents run inside the customer's network and connect outward to the gateway only. They open no listening ports.

- **Verified in code and documented.** Each request carries an Ed25519 signature over method, path, timestamp, nonce and body hash. Timestamps more than 300 seconds apart are refused. The nonce cache keeps entries for twice the allowed skew. Batch sequence numbers must increase (`ARCHITECTURE.md` §1.5). Agent keys can be revoked and rotated (`POST /agents/{id}/revoke`, `POST /agents/{id}/keys/rotate`).
- **Threat model.** `docs/site/agent-security.md` sets out the parts, trust and threats, and states that each attack has a test against the real gateway. This document did not check each of those tests.
- **Pool policy.** Agents may write only the platforms, URN prefixes and aspects their pool allows. The gateway checks every metadata change against the pool before forwarding it.

## 11. AI features

- **Documented.** The assistant and AI functions retrieve only what the caller may view. Free text is redacted for personal data before it enters a prompt. Generated documentation is written only after an approving reviewer accepts it. The model never receives credentials. Every model call is audited (model, token counts, entities in context); prompts are kept only if `CAIRN_AI_AUDIT_PROMPTS=true`, which defaults to false (`ARCHITECTURE.md` §1.6; `cairn/src/cairn/common/config.py`).
- **Open item.** `[TO CONFIRM: which model providers are configured in each reference deployment, and their data terms]`

## 12. Software supply chain and vulnerability management

- **Verified in CI** (`.github/workflows/cairn-ci.yml`, job `stack`). Cairn-owned images are scanned and the build fails on fixable critical vulnerabilities. Other images are scanned and reported only. Each image gets an SPDX software bill of materials. On pushes to the main branch, images are published and signed, with the SBOM attached as a signed attestation.
- **Verified in the repository.** Release manifests are signed and an install checks the signature (`docs/site/administration.md`, "Updates: the release feed"). The release threshold is one key by default and two during key rotation.
- **Gap.** The repository has no automated dependency update configuration (no Dependabot or Renovate file). Python dependencies are declared as version ranges with no lock file, so a rebuilt Python environment may differ from the one tested. See the open-source inventory (`docs/legal/oss-license-inventory.md`).
- **Inventory.** The open-source inventory lists the third-party dependencies from every manifest and lock file, with licences as recorded in the repository.

## 13. Known gaps

| # | Gap | Evidence | Status |
|---|---|---|---|
| S1 | Field-level masking is not enforced in Cairn Core GraphQL responses | ROADMAP 2.10 ⬜ | Planned |
| S2 | Independent assurance (SOC 2, ISO 27001) and live validation of private endpoints and hosting on AWS, Azure and GCP | ROADMAP L185 ⬜ | Open; see section 16 |
| S3 | Private networking for user code and the cloud modules | ROADMAP L135, L136 | 🟡 or open (ROADMAP L185 text) |
| S4 | Store and gateway serve plain HTTP by default in the chart | section 6 | Default; configuration |
| S5 | Encryption at rest is off by default | section 7 | Default; configuration |
| S6 | Pod Security is off by default for tenants | section 9 | Default; configuration |
| S7 | Shared PostgreSQL for all tenants in a region | section 9 | Architecture |
| S8 | No external anchor for the audit chain | governance document, section 11 | Not built |
| S9 | Store key is not in the posture scan | section 7 | Not built |
| S10 | No automated dependency updates; Python ranges without a lock file | section 12 | Open |

## 14. Inconsistencies found while writing this document

These are reported and not corrected in the product.

1. **Tenant isolation wording.** The hosting runbook describes each tenant as "one isolated release". The network-level isolation is rendered only when `CAIRN_INGRESS_NAMESPACE` is set, and the runbook does not mention that condition (`hosting-runbook.md`, first paragraph; `tenant.sh`).
2. **Erasure wording.** The `privacy.py` module description and the erasure endpoint disagree (governance document, section 13).
3. **Region recovery.** The hosting runbook says that no script in the repository restores a lost region and that cross-region backups are needed. The roadmap (L9) and the administration manual describe `cairn dr reconcile` and `cairn dr watch` as shipped. See the disaster recovery document, section 9.
4. **Store encryption in the posture scan.** The scan's checks do not include store encryption, although the store's encryption is one of the platform's main data-at-rest controls.

## 15. Open items

| # | Item | Owner |
|---|---|---|
| Z1 | Decide the default for store and gateway TLS (section 6) | [TO CONFIRM: owner] |
| Z2 | Confirm tenant placement on PostgreSQL servers (section 9) | [TO CONFIRM: owner] |
| Z3 | Confirm resource scope of the service account's metadata grants (section 4) | [TO CONFIRM: owner] |
| Z4 | Confirm MFA for password sign-in where it exists (section 3) | [TO CONFIRM: owner] |
| Z5 | Keyring and secrets key rotation procedure (section 5) | [TO CONFIRM: owner] |
| Z6 | Storage encryption for PostgreSQL volumes (section 7) | [TO CONFIRM: owner] |
| Z7 | Model providers and data terms for AI features (section 11) | [TO CONFIRM: owner] |
| Z8 | Automated dependency updates and a lock file for Python (section 12) | [TO CONFIRM: owner] |

## 16. Related assessments (linked, not restated)

- SOC 2 readiness mapping: [`cairn/docs/operations/soc2-readiness.md`](../../cairn/docs/operations/soc2-readiness.md)
- External penetration test scope: [`cairn/docs/operations/pentest-scope.md`](../../cairn/docs/operations/pentest-scope.md)

Those documents set out the audit mapping and the external test scope. This document does not restate their conclusions.
