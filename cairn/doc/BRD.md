# Cairn — Business Requirements Document (BRD)

| Field | Value |
|---|---|
| Product | Cairn: self-hosted active-metadata and data-governance platform on Cairn Core |
| Document | Business Requirements Document, v0.1 (draft for review) |
| Date | 2026-11-14 |
| Owner | Product owner, Cairn |
| Approvers | Head of Engineering, Head of Security, Head of Data Platform (to be named) |
| Status | Draft. Requirements are proposed unless marked **Shipped** (see §4.3) |
| Source of truth | `cairn/ROADMAP.md`, `cairn/ARCHITECTURE.md`, `cairn/UX_ACTIVE_METADATA_SPEC.md`, `cairn/README.md` |

---

## 1. Purpose

This document defines what Cairn must do for the organisations that buy and run it, why it matters, and how we will know it has worked. It is the reference for product, engineering, security and go-to-market decisions. Technical design lives in `ARCHITECTURE.md`. Sequencing lives in `ROADMAP.md`. This BRD does not restate either; it links each business requirement to them.

## 2. Background and problem

Data teams today run several separate products to store, query, transform, orchestrate, catalogue, govern and monitor their data. Each has its own identity, its own permissions model and its own audit trail. Results are that:

1. **Access is inconsistent.** A person can be allowed in the warehouse and refused in the catalogue, or the reverse. Pipelines often hold database passwords with broad rights.
2. **Governance is after the fact.** PII is classified late, and masks and row filters are applied in one tool but not in the others that read the same data.
3. **Lineage and ownership are partial.** Each engine records what it sees, and nobody can answer "what breaks if I change this table?" across the whole estate.
4. **Cost and lock-in are high.** Teams pay for a catalogue vendor, a warehouse, an orchestrator and a governance suite, and their data and metadata sit inside each vendor.
5. **Sensitive networks block SaaS.** Regulated customers need the data plane, and often the control plane, inside their own network.

Cairn addresses these by giving one identity, one policy and one metadata graph across the open-source engines a team already runs or can adopt, deployed in the customer's own network.

## 3. Vision and goals

**Vision.** One governed data platform, self-hosted, in which every person, pipeline and AI agent signs in once, is decided by one policy, and leaves one audit trail.

| Goal | Business outcome | Primary measure (target) |
|---|---|---|
| G1. One identity and one policy | Access decisions are the same in every engine | 0 engines hold a database password or store key in production configs (verified by test) |
| G2. Governance at the point of read | Masks and row filters apply wherever data is read | 100% of restricted tables refused to engines that cannot enforce policy (fail closed) |
| G3. One metadata graph | Lineage, ownership, quality and usage are visible across engines | Column-level lineage from Trino, Spark, dbt, Airflow and CDC into Cairn Core |
| G4. Self-hosted by default | Data and control plane stay in the customer's network when required | Deployable with compose and Helm; in-VPC agents need no inbound connection |
| G5. Replace a stack, not a feature | A team can retire a catalogue, an orchestrator and a warehouse vendor over time | Migration tooling and parity list maintained in `ROADMAP.md` (items L170, L181 and related) |
| G6. Trust for AI | AI answers and AI-written documentation are governed and reviewed | Every AI output carries the caller's permissions; drafts need human approval |

## 4. Scope

### 4.1 In scope

The product is organised in four rings around Cairn Core (`ROADMAP.md`, "Cairn Core at the centre"). Every capability must say which ring it is in and what it reads from or writes back to the graph.

| Area | What the business gets | Modules / components |
|---|---|---|
| Data platform engines | Store, tables, SQL, Python and Spark, transformation, orchestration | Cairn Store, Cairn Catalog, Trino, dbt, Airflow, Spark, Cairn Flows |
| Governance | Policy, masking, row filters, personas, PII propagation, certification, audit | Module B (PDP, Cairn Core authorizer plugin), module E (personas and purposes) |
| Data quality | Monitors, incidents, contracts, profiling | Module A |
| Ingestion and connectivity | Connectors, in-VPC agents, streams and pipes | Module C, Cairn Store streams |
| Discovery and consumption | Catalog, search, lineage, marketplace, embeds, collaboration | Cairn Core UI, module E consumer workspace, embed SDK |
| AI | Assistant, documentation drafts, context agents, MCP server, governed metrics | Module D, module E assistant |
| People and automation | Terminal, infrastructure as code, notifications | `cairn` CLI, bundles and Terraform provider, notifier |
| Operations | Install, upgrade, monitor, scale | Compose, Helm, bootstrap scripts, observability stack |

### 4.2 Out of scope (for this BRD version)

- Replacing the source systems' transactional databases. Cairn reads them through connectors and agents.
- Building a general-purpose BI dashboard product. Cairn embeds trust components into Tableau, Looker, Power BI and portals (module E) and does not compete as a BI tool.
- Hosting customer data for others as a multi-tenant SaaS data warehouse. A hosting path exists (`deploy/hetzner`) but is not a product commitment in v1.
- A public marketplace across organisations (roadmap item L184 is planned, not committed here).

### 4.3 Status of the scope

Status uses the roadmap's own markers: **Shipped** (built and tested end to end in CI), **Partly validated** (built, some checks not yet run live), **Planned** (not built).

| Capability | Status | Evidence / gap |
|---|---|---|
| Object storage, tables, Trino SQL, dbt, Airflow, Spark, Flows | Shipped | `ROADMAP.md` "The platform today" |
| Governance (PDP, masking, row filters, personas, purposes, PII propagation, certification, audit) | Shipped | README verification table |
| Quality monitors, contracts, incidents | Shipped | Module A; `ARCHITECTURE.md` §1.2 |
| In-VPC remote agents | Shipped | Live e2e: agent ingests a database unreachable from the control plane |
| AI context, MCP server, assistant | Partly validated | Assistant ran in offline mode in tests; live model calls not yet run |
| Marketplace approvals (Slack, Teams, email) | Partly validated | Slack protocol simulated with valid signatures; real endpoints not yet run |
| Embeds in Tableau, Looker, Power BI | Partly validated | Packaged and tested; not yet validated inside each BI tool |
| Helm on a real cluster | Partly validated | Linted and kubeconform-checked; not yet run on a live cluster |
| External penetration test, SOC 2, ISO 27001 | Planned | Roadmap item L185 |
| Upgrade playbook against Cairn Core minor releases | Planned | Roadmap item 5.6 |

## 5. Stakeholders and users

| Persona | Who | What they need from Cairn | Primary modules |
|---|---|---|---|
| **Platform admin** | Runs the platform team | Install, upgrade, configure tenants, monitor health, manage pools and agents | Compose/Helm, IaC, observability |
| **Data engineer** | Builds pipelines | Run SQL, dbt, Spark and Airflow as a pipeline identity without secrets; see lineage and failures | Trino, dbt, Airflow, Spark, Flows, quality |
| **Data steward** | Owns domains and quality | Define glossary terms, certify assets, approve access, review AI drafts | Core, governance, certification, context agents |
| **Data analyst / consumer** | Finds and uses data | Discover assets, request access without tickets, ask questions in plain language, see trust signals | Consumer workspace, marketplace, assistant |
| **Security and compliance officer** | Accountable for risk | Prove who accessed what, enforce masks and purposes, export audit evidence | PDP, audit log, compliance forms |
| **Business owner / executive** | Pays for and judges the platform | Cost visibility, risk reduction, migration path off legacy vendors | Reporting, migration tooling |
| **Automation and AI agents** | Non-human callers | Act through MCP and APIs under a named identity and the same policy | MCP server, agent gateway, IaC proposals |

## 6. Business requirements

Priority uses MoSCoW: **M** = must have for the target release, **S** = should, **C** = could, **W** = won't in this version. Each requirement links to the roadmap or architecture section that defines it.

### 6.1 Identity and access

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-ID-01 | People sign in once through SSO; users and groups are provisioned through SCIM | M | ROADMAP principles; `ARCHITECTURE.md` §1.1 |
| BR-ID-02 | Pipelines and agents act as named pipeline users with personal access tokens, exchanged for short-lived, audience-bound engine tokens (default 4 hours, maximum 12) | M | ROADMAP "Design decisions" |
| BR-ID-03 | No engine, project file or remote agent stores a database password or store key in plain text | M | README security notes |
| BR-ID-04 | Every token issuance is audited | M | ROADMAP "Design decisions" |
| BR-ID-05 | The control plane's service account cannot manage policies, users, credentials or delete entities | M | README security notes |
| BR-ID-06 | Service credentials rotate automatically with a grace period | M | README (`cairn-token-keeper`) |

### 6.2 Policy and governance

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-GOV-01 | One policy decision point decides access for every engine, the metadata APIs and AI tools | M | ROADMAP principle "One policy" |
| BR-GOV-02 | Deny overrides allow; a decision that cannot be made is a refusal | M | ARCHITECTURE §1.3; README (fail closed) |
| BR-GOV-03 | Masks and row filters apply on every read path. An engine that cannot enforce them is refused restricted tables | M | ROADMAP "Design decisions" |
| BR-GOV-04 | PII is classified and the classification propagates over column lineage | M | Module B |
| BR-GOV-05 | Personas and purposes grant access by role and purpose; a purpose deny overrides a persona grant | M | Module E (U3) |
| BR-GOV-06 | Certification and access approvals enforce separation of duties | M | Module B; Module E (U2) |
| BR-GOV-07 | Access can be granted for a time-boxed period, with expiry warnings and revocation | M | Module E (U2) |
| BR-GOV-08 | Policy changes are validated in CI before they reach a deployment (`cairn policies validate`) | S | ROADMAP 2.11 (planned) |
| BR-GOV-09 | Masking obligations are enforced in the GraphQL responses of Cairn Core | S | ROADMAP 2.10 (planned) |

### 6.3 Data quality and reliability

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-DQ-01 | Monitors for freshness, volume, schema, field metrics and drift, with learned baselines | M | Module A |
| BR-DQ-02 | Failed checks raise incidents that auto-resolve when the data recovers | M | Module A; README live e2e |
| BR-DQ-03 | Data contracts are defined and checked | M | Platform table, "Data quality" |
| BR-DQ-04 | Quality results appear as native assertions in the catalogue | M | README e2e |
| BR-DQ-05 | Optional real-time alert fan-out to paging tools | C | ROADMAP 1.9 (planned) |

### 6.4 Data platform and engines

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-ENG-01 | SQL on the lakehouse with Trino, per-team warehouses that stop when idle | M | Platform table |
| BR-ENG-02 | Iceberg tables with per-table credential vending and Cairn-governed access | M | Cairn Catalog |
| BR-ENG-03 | dbt runs as a pipeline user; models, tests, owners and lineage are recorded in Cairn | M | Platform table |
| BR-ENG-04 | Orchestration as DAGs (Cairn Flows and Airflow 3.3) run as their owner; Airflow DAGs are readable on the canvas | M | Platform table |
| BR-ENG-05 | Spark reads each table under Cairn's decision; restricted tables are read through Trino | M | ROADMAP "Design decisions" |
| BR-ENG-06 | Streams from Kafka and Debezium CDC into Iceberg, exactly once | S | Platform table, "Ingestion" |
| BR-ENG-07 | Stateful streaming SQL with a declared freshness target | C | ROADMAP L176 (planned) |
| BR-ENG-08 | Migration assessment and conversion from Snowflake and Databricks, with a report of what could not be converted | S | ROADMAP L170 (planned) |

### 6.5 Discovery, metadata and lineage

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-MD-01 | A typed metadata graph of assets, domains, glossary terms, data products and contracts | M | Cairn Core |
| BR-MD-02 | Search by keyword and by meaning, with faceted filters the person is allowed to see | M | Cairn Core; Module E (U1) |
| BR-MD-03 | Column-level lineage from every engine, dbt, Airflow, Spark and CDC | M | Platform table |
| BR-MD-04 | Glossary terms and relationships are proposed by context agents and accepted by a steward | M | Module E (U4) |
| BR-MD-05 | Ontology: business terms, metrics and relationships compiled from the catalog and query history, reviewed as proposals | S | Cairn Ontology |
| BR-MD-06 | Bookmarks, follows, @mentions, threads, reactions and trending | S | Module E (U1) |

### 6.6 Consumption and self-service

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-CON-01 | A consumer workspace (Discover, asset pages, marketplace) mounted inside the Cairn UI | M | Module E (U1, U2) |
| BR-CON-02 | Request access to a data product without a ticket; approval routed to the owner | M | Module E (U2) |
| BR-CON-03 | Ask questions in plain language; answers use only data the caller may see | M | Module D; Platform table, "SQL in the product" |
| BR-CON-04 | Trust components (certification, quality, ownership) embed in BI tools and portals with origin-bound, revocable tokens | S | Module E (U6) |
| BR-CON-05 | A command-line tool signs in through the browser and runs queries, search and asks as the person | S | `cairn` CLI, README |
| BR-CON-06 | Notifications to Slack and Teams with signed interactive approvals | S | Module E (U2, U7) |

### 6.7 AI

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-AI-01 | AI tools run as the caller and see only what the caller may see | M | Module D; Module E (U5) |
| BR-AI-02 | AI-written documentation and definitions are drafts until a human approves them | M | Module D |
| BR-AI-03 | The MCP server offers the same governed tools to Claude and other MCP clients | M | Module D; README |
| BR-AI-04 | The model provider is configurable (Claude or a local model); the product works when no model is reachable | M | Module E (U5), offline fallback |
| BR-AI-05 | AI usage is measured, with budgets | S | Platform table, "AI" |
| BR-AI-06 | AI answers are evaluated against golden questions before release | S | ROADMAP 4.8 (planned) |

### 6.8 Automation and infrastructure as code

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-IAC-01 | Everything configurable can be expressed as code (bundles from git, a Terraform provider) | M | README; ROADMAP platform table |
| BR-IAC-02 | Agents propose infrastructure changes; a person with the right privileges approves and applies | M | README (IaC changes) |
| BR-IAC-03 | Policy bundles and workflow templates are versioned in git | M | `config/` |

### 6.9 Audit and compliance

| ID | Requirement | Priority | Source |
|---|---|---|---|
| BR-AUD-01 | Every change is one transaction with its audit entry | M | ROADMAP principle "Fail closed" |
| BR-AUD-02 | The audit log is hash-chained and tamper-evident | M | Platform table, "Governance" |
| BR-AUD-03 | Compliance forms and evidence can be exported for review | S | Platform table, "Governance" |
| BR-AUD-04 | Independent external penetration test and audited controls (SOC 2 or ISO 27001) before general availability | M | ROADMAP L185 (planned) |

## 7. Non-functional requirements

| ID | Category | Requirement | Target | Status |
|---|---|---|---|---|
| NFR-01 | Security | Fail closed on every access decision | Verified by tests | Shipped |
| NFR-02 | Security | Secrets never leave the customer network in plain text | Verified by the secret-confinement test (agent password absent from logs and Cairn Core) | Shipped |
| NFR-03 | Security | Remote agents hold no Cairn Core credentials and accept no inline secrets in recipes | Verified | Shipped |
| NFR-04 | Security | Threat model and a negative test for each attack | Maintained in `docs/site/agent-security.md` | Shipped |
| NFR-05 | Security | Key management through KMS or HSM for the gateway identity | To be defined | Planned (ROADMAP 5.5) |
| NFR-06 | Availability | Core services are highly available under Helm (replicas, pod disruption budgets, network policies) | 99.9% monthly target for the control plane, to be confirmed on a live cluster | Partly validated |
| NFR-07 | Resilience | Outages of Cairn Core, the PDP or the gateway degrade safely and are tested | Chaos tests | Planned (ROADMAP 5.2) |
| NFR-08 | Performance | Interactive search and asset pages respond quickly on a catalogue of realistic size | Target to be set from a load test | Planned |
| NFR-09 | Scalability | Engines scale independently; Trino workers autoscale; warehouses start on demand | Verified in platform configuration | Shipped |
| NFR-10 | Operability | Monitoring dashboards, health checks, structured logs | Present | Shipped |
| NFR-11 | Portability | Runs in Docker Compose for evaluation and in Kubernetes with Helm for production | Compose verified; Helm on a live cluster pending | Partly validated |
| NFR-12 | Upgrades | Pin-and-test against each Cairn Core minor release | Playbook to be written | Planned (ROADMAP 5.6) |
| NFR-13 | Licensing | Third-party licences are listed and ship with every distribution | `THIRD_PARTY_NOTICES.md` | Shipped |
| NFR-14 | Accessibility | Dashboard and workspace meet WCAG 2.1 AA | Target proposed; not yet audited | Planned |
| NFR-15 | Internationalisation | Interfaces are translatable | Present (i18n in the platform) | Shipped |
| NFR-16 | Supply chain | Images are signed with SBOMs; non-root; minimal OS packages | Verified | Shipped |

## 8. Assumptions, constraints and dependencies

**Assumptions**

- Customers run their own network and can host the agents inside it.
- Customers accept open-source engines (Trino, Spark, Airflow, dbt, Iceberg, PostgreSQL, Kafka) as the foundation.
- Cairn Core is used as an unmodified upstream image; Cairn adds a Java authorizer plugin and a control plane around it.

**Constraints**

- Cairn Core runs on Java 25; the authorizer plugin is built for Java 17.
- Engine versions are pinned and tested as a set (Trino 484, Spark 4.0 with Iceberg 1.10, Airflow 3.3, dbt-core 1.12).
- Docker Hub rate limits and TLS-intercepting proxies affect image builds in some networks; the build scripts support mirrors and extra CA files.

**Dependencies**

- Cairn Core and its search and messaging stack (OpenSearch or Elasticsearch, Kafka, MySQL or PostgreSQL)
- Customer identity provider for SSO and SCIM
- Optional: Claude or a local model for AI features; Slack or Teams for notifications; Vault or AWS Secrets Manager for secrets

## 9. Risks and issues

| ID | Risk or issue | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R-01 | Live behaviour differs from tests for Helm, real Slack/Teams, live model calls and BI-embedded trust components (not yet run live) | Medium | High | Run the live validation list before each release; treat "partly validated" as not shipped to customers | Engineering |
| R-02 | Cairn Core upstream changes break aspect shapes or GraphQL fields | Medium | High | Upgrade playbook and pin-and-test matrix (ROADMAP 5.6); contract tests | Engineering |
| R-03 | Security findings in the gateway or policy engine before an external test | Medium | Critical | External penetration test before GA (BR-AUD-04); threat model maintained | Security |
| R-04 | Policy misconfiguration grants access by mistake | Medium | High | Policy validation in CI (BR-GOV-08); deny-overrides; purposes at higher priority | Security / Product |
| R-05 | Competitive pressure from catalogue and warehouse vendors with larger teams | High | Medium | Focus on self-hosted, one-policy story; migration tooling; open engines | Product |
| R-06 | Customers in restricted networks cannot run the agent or control plane as designed | Low | High | Outbound-only agents; documented network requirements; Helm values for air-gapped mirrors | Engineering |
| R-07 | Scope breadth: many planned items (ROADMAP "Next", L168–L185 and others) compete for a small team | High | High | Prioritise by BRD priority; track planned items in the roadmap; cut "could" items first | Product |
| R-08 | AI answers give wrong results, or a model provider changes terms | Medium | Medium | Golden-question evaluation (ROADMAP 4.8); AI outputs are drafts; offline fallback | Product |

## 10. Release approach

Releases are gated on the roadmap's status markers, not on dates. A capability ships when it is **Shipped**: built, tested end to end in CI, and checked against the real images.

| Stage | Entry | Exit |
|---|---|---|
| **Stage 1: Live validation** | Current code base | Each "partly validated" item in §4.3 is run live (Helm on a cluster, real Slack and Teams endpoints, live model, BI embeds) with recorded results |
| **Stage 2: Assurance** | Stage 1 exit | External penetration test passed; SOC 2 or ISO 27001 plan agreed; KMS or HSM key management (NFR-05) in place |
| **Stage 3: General availability** | Stage 2 exit | Upgrade playbook (NFR-12) tested against one Cairn Core minor release; monitoring and support runbooks in place; pricing and packaging approved |
| **Stage 4: Growth** | GA | Planned items (ROADMAP "Next", L168–L185) prioritised by customer demand and by the requirements in §6 |

## 11. Acceptance criteria

A business requirement is accepted when all of the following hold:

1. Its behaviour is covered by an automated test that runs in CI, and by an end-to-end test against the real images where the requirement crosses services.
2. It is assigned to one of the four rings in `ROADMAP.md` and states what it reads from and writes back to Cairn Core.
3. Its access decisions fail closed, and its audit entries are written in the same transaction as the change.
4. Its documentation is updated in `docs/site/`.
5. The product owner has checked the behaviour against the user story in §5.

## 12. Open questions

| # | Question | Why it matters | Decision needed from |
|---|---|---|---|
| Q-01 | Which deployment is the first commercial offer: self-hosted only, or also a managed Cairn on Hetzner? | Changes support, pricing and the GA checklist | Leadership |
| Q-02 | Which compliance standard comes first, SOC 2 or ISO 27001? | Sets the audit timeline and budget | Security |
| Q-03 | What are the performance targets for search and asset pages, and for largest supported repository size? | NFR-08 cannot be tested without them | Engineering |
| Q-04 | Which AI provider is the default for paying customers, and is local-only operation required? | Affects BR-AI-04 and data-residency commitments | Product / Security |
| Q-05 | Which migration source comes first, Snowflake or Databricks? | Determines the first migration tooling in BR-ENG-08 | Product |
| Q-06 | Who owns the Stage 1 live-validation list and its sign-off? | Nothing in §10 can pass without it | Engineering |

## 13. Glossary

| Term | Meaning |
|---|---|
| **Cairn Core** | The metadata system of record: typed graph, search, lineage, catalogue UI and APIs. Upstream image, unmodified |
| **Control plane** | The Cairn services added on top of Core: quality, governance, agents, AI, MCP, actions |
| **PDP** | Policy decision point. Decides every access request with deny-overrides |
| **Persona** | A role bundle of grants, scoped and time-boxed |
| **Purpose** | A stated reason for access. A purpose deny overrides a persona grant |
| **Pipeline user** | A non-human identity that runs jobs and holds personal access tokens |
| **Remote agent** | An outbound-only ingestion process inside a customer network |
| **Shipped / Partly validated / Planned** | Roadmap markers for built and tested; built with some live checks outstanding; not yet built |
| **Fail closed** | If a decision cannot be made, access is refused |
| **Ring** | One of four areas around Cairn Core that a capability belongs to |
