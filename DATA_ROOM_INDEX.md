# Cairn: Data Room Master Index

| Field | Value |
|---|---|
| Company | Cairn (product: self-hosted governed data platform) |
| Index version | 0.1 (draft for review) |
| Last reviewed | 2026-11-14 |
| Index owner | [TO CONFIRM: index owner] |
| Access | Virtual Data Room, reviewed before any external sharing |

**Status key:** *Draft* = written, not reviewed by a named owner. *Review* = reviewed by its owner, awaiting sign-off. *Final* = approved. *Gap* = no document yet.

**Important:** the legal documents are compliance frameworks for counsel and the board. They are not legal or tax advice. No agreement, filing or grant is stated as executed unless its own document says so.

---

## 1. Corporate and Legal

| Document | Link | Owner | Last reviewed | Status |
|---|---|---|---|---|
| IP governance: assignments, PIIAs, licence audit framework | [docs/legal/ip-governance.md](docs/legal/ip-governance.md) | [TO CONFIRM: owner] | 2026-11-14 | Draft |
| Open-source licence inventory (1,538 dependency rows) | [docs/legal/oss-license-inventory.md](docs/legal/oss-license-inventory.md) | [TO CONFIRM: owner] | 2026-11-14 | Draft; 23 flagged for legal review, 689 licences unknown |
| Cap table and equity governance | [docs/legal/cap-table-governance.md](docs/legal/cap-table-governance.md) | [TO CONFIRM: owner] | 2026-11-14 | Draft; proposed terms only |
| Third-party notices (repository) | [cairn/THIRD_PARTY_NOTICES.md](cairn/THIRD_PARTY_NOTICES.md) | Engineering | [TO CONFIRM: date] | Draft; known omissions listed in the licence inventory |
| Certificate of incorporation | Gap | [TO CONFIRM: owner] | — | Gap: not in the repository |
| Bylaws, board consents, stockholder agreements | Gap | [TO CONFIRM: owner] | — | Gap |
| Signed PIIAs and contractor agreements | Gap | [TO CONFIRM: owner] | — | Gap: framework exists; no signed records are in the repository |
| Stock ledger and option grant register | Gap | [TO CONFIRM: owner] | — | Gap |

## 2. Product Vision and Requirements

| Document | Link | Owner | Last reviewed | Status |
|---|---|---|---|---|
| Business requirements document (VDR version, v0.3) | [docs/product/BRD.md](docs/product/BRD.md) | [TO CONFIRM: product owner] | 2026-11-14 | Draft |
| Product and engineering roadmap (three phases) | [docs/product/ROADMAP.md](docs/product/ROADMAP.md) | [TO CONFIRM: product owner] | 2026-11-14 | Draft |
| Business requirements document, repository copy (v0.2) | [cairn/doc/BRD.md](cairn/doc/BRD.md) | Product | 2026-11-14 | Superseded by the VDR version; kept for history |
| Roadmap appendix: item status, generated | [cairn/doc/BRD-appendix-roadmap.md](cairn/doc/BRD-appendix-roadmap.md) | Product | 2026-11-14 | Generated |
| Engineering roadmap, source of truth (L1 to L185) | [cairn/ROADMAP.md](cairn/ROADMAP.md) | Engineering | 2026-11-14 | Draft; some status markers under review |
| Market sizing with a sourced estimate | Gap | [TO CONFIRM: owner] | — | Gap |
| Design-partner evidence and customer references | Gap | [TO CONFIRM: owner] | — | Gap |
| Competitor analysis, dated | Gap | [TO CONFIRM: owner] | — | Gap |
| Pricing model and price list | Gap | [TO CONFIRM: owner] | — | Gap: the model is under evaluation |

## 3. Technical Architecture and System Design

| Document | Link | Owner | Last reviewed | Status |
|---|---|---|---|---|
| Data engine and storage layer | [docs/architecture/data-engine.md](docs/architecture/data-engine.md) | Engineering | 2026-11-14 | Draft |
| AI platform and LLMOps | [docs/architecture/ai-platform.md](docs/architecture/ai-platform.md) | Engineering | 2026-11-14 | Draft |
| System design and API specifications | [docs/architecture/system-design.md](docs/architecture/system-design.md) | Engineering | 2026-11-14 | Draft |
| OpenAPI document (generated from the application) | [docs/architecture/openapi.json](docs/architecture/openapi.json) | Engineering | 2026-11-14 | Generated; regenerate with each API change |
| Architecture, repository copy | [cairn/ARCHITECTURE.md](cairn/ARCHITECTURE.md) | Engineering | 2026-11-14 | Draft; inconsistencies listed in the architecture documents |
| Platform overview and quickstart | [cairn/README.md](cairn/README.md) | Engineering | 2026-11-14 | Draft |
| User experience and active metadata specification | [cairn/UX_ACTIVE_METADATA_SPEC.md](cairn/UX_ACTIVE_METADATA_SPEC.md) | Product and design | [TO CONFIRM: date] | Draft |
| Cairn Store and Catalog component notes | [cairn/store/README.md](cairn/store/README.md), [cairn/catalog/README.md](cairn/catalog/README.md) | Engineering | 2026-11-14 | Draft |
| Cairn Core upstream record and patches | [cairn/core/UPSTREAM.md](cairn/core/UPSTREAM.md) | Engineering | 2026-11-14 | Draft |
| Deployment: Hetzner cluster | [cairn/deploy/hetzner/README.md](cairn/deploy/hetzner/README.md) | Engineering | 2026-11-14 | Draft |
| Helm chart | [cairn/helm/cairn](cairn/helm/cairn) | Engineering | 2026-11-14 | Draft; not yet run on a live cluster |

## 4. Security, Governance and Compliance

| Document | Link | Owner | Last reviewed | Status |
|---|---|---|---|---|
| Data governance: lineage, quality, audit | [docs/compliance/governance.md](docs/compliance/governance.md) | [TO CONFIRM: owner] | 2026-11-14 | Draft |
| Security posture: tenancy, encryption, RBAC | [docs/security/security-posture.md](docs/security/security-posture.md) | [TO CONFIRM: owner] | 2026-11-14 | Draft |
| Disaster recovery: RPO and RTO targets | [docs/security/disaster-recovery.md](docs/security/disaster-recovery.md) | [TO CONFIRM: owner] | 2026-11-14 | Draft; targets not yet verified by a recovery test |
| SOC 2 readiness | [cairn/docs/operations/soc2-readiness.md](cairn/docs/operations/soc2-readiness.md) | Security | 2026-10-29 | Draft; no audit has been scoped or run |
| Penetration test scope | [cairn/docs/operations/pentest-scope.md](cairn/docs/operations/pentest-scope.md) | Security | 2026-11-06 | Draft; no external test has run |
| Hosting runbook | [cairn/docs/operations/hosting-runbook.md](cairn/docs/operations/hosting-runbook.md) | Operations | 2026-11-14 | Draft |
| Agent security (product documentation) | [cairn/docs/site/agent-security.md](cairn/docs/site/agent-security.md) | Security | 2026-11-14 | Draft |
| Threat model | Gap as a standalone document | Security | — | Gap: referenced in the architecture and the README; no standalone file |
| SOC 2 Type I report | Gap | [TO CONFIRM: owner] | — | Gap: not started |
| Independent penetration test report | Gap | [TO CONFIRM: owner] | — | Gap: not run |
| Recovery drill records | Gap | [TO CONFIRM: owner] | — | Gap: drill script exists; no recorded drill results |

---

## Summary of gaps

Items marked *Gap* above are not in the repository. The most material for due diligence are:

1. Incorporation and governance records: certificate of incorporation, bylaws, board consents.
2. Signed IP assignments and PIIAs for every founder and contractor.
3. A stock ledger and option grant register, supporting the cap table.
4. A sourced market estimate and design-partner evidence.
5. An independent penetration test report and a SOC 2 report, neither of which has been started.
6. A recorded disaster recovery drill, supporting the RPO and RTO targets.

Every `[TO CONFIRM]` placeholder in the linked documents is a question for the owner named in its document. Placeholders are not facts.
