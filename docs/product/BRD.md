# Cairn: Business Requirements Document (BRD)

| Field | Value |
|---|---|
| Product | Cairn: a governed data platform built on Cairn Core, the metadata graph. Self-hosted, with an optional managed offer |
| Document | Business Requirements Document, **v0.3** (draft for review) |
| Date | 2026-11-14 |
| Prepared for | Virtual data room (VDR) review |
| Owner | [TO CONFIRM: named product owner] |
| Approvers | [TO CONFIRM: Head of Engineering, Head of Security, Head of Data Platform, by name] |
| Status | Draft. Each capability carries the roadmap's own status marker |
| Companion | [`ROADMAP.md`](ROADMAP.md) in this folder: the three-phase roadmap and the item-level mapping |
| Predecessor | [`cairn/doc/BRD.md`](../../cairn/doc/BRD.md) (v0.2). Superseded by this version |
| Evidence base | Repository state checked on 2026-11-14 (the checkout's working branch). Sources are listed in Appendix B |

**How to read this document.** Each statement is one of four kinds:

- **Fact (verified):** checked in code, tests, pinned versions or the roadmap, with the source named.
- **Roadmap claim:** stated by `cairn/ROADMAP.md` with its marker. Not re-run for this document.
- **Target:** an intended value that has not been measured or agreed.
- **Open item:** marked `[TO CONFIRM: what is needed]`.

Status markers are the roadmap's: **✅** shipped and tested end to end in CI; **🟡** shipped, partly validated (some checks have not run live); **⬜** planned. L-numbers refer to the roadmap's numbered queue in `cairn/ROADMAP.md` ("Next"). Where a citation uses the legacy milestone table (M0 to M6, in the roadmap's "History" section), it is written as "legacy M2 row 2.11", for example.

**Change log**

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-11-14 | First draft, built from the README and the early roadmap. About 60 requirements in four areas |
| v0.2 | 2026-11-14 | Rebuilt from the full roadmap: 185 items in 13 areas. Cairn Core described as built on an open-source engine with Cairn's patches; managed hosting in scope as an option |
| v0.3 | 2026-11-14 | Moved to `docs/product/` for the VDR. Added the executive summary, market thesis, personas and use cases, the four functional themes, performance targets, and a monetisation comparison. Corrected citations and statuses found on review; see Appendix A |

---

## 1. Executive summary

Open table formats have made the storage layer of a data platform portable. Apache Iceberg and Delta Lake tables can be read and written by several engines (Cairn: L19, L86). Governance has not followed. Each engine still decides access on its own terms, so a mask or row filter applied in one tool can be missed by another that reads the same files. Lineage and ownership stop at engine boundaries. AI agents are added as new identities that hold their own credentials. This gap is widest in organisations that must run the data plane in their own network.

Cairn addresses the gap with one identity model, one policy decision point (PDP) that every engine and AI tool consults, and a metadata graph (Cairn Core) that records assets, lineage, owners and quality across engines. Agents act as the calling person or pipeline user, within the same policy. Cairn's design rules are fail closed (an undecidable request is refused) and one transaction per change with its audit entry. These rules are stated in `cairn/ROADMAP.md` and are tested for the core paths, including policy caching and chaos cases (L10, L46).

**Where the product stands (2026-11-14).** The roadmap lists 185 items: **124 ✅ shipped, 43 🟡 partly validated, 18 ⬜ planned.** The current release, L139 to L152, has two shipped items and twelve partly validated items. Four matters must be resolved before general availability:

1. **Assurance is not done.** The external penetration test and SOC 2 or ISO 27001 audit are planned (L185 ⬜). The penetration test is also part of L10, which is 🟡.
2. **Live validation is open for 43 items.** These include Helm on live clusters, real Slack and Teams endpoints, live model calls, BI embeds, fleet and air-gap flows. Under §10 they are not generally available.
3. **Some ✅ markers overstate the evidence.** Three items (L35, L94, L95) record a live-model suite as "not yet run". §10 and Appendix A list these.
4. **The commercial model is open.** The price and the pricing model are not decided. Market size has no sourced estimate.

**Decisions requested** (§14): which offer ships first (self-hosted, managed or both, Q-01); which list is the working backlog (Q-06); the pricing model (Q-10); and the owner of live validation (Q-07).

---

## 2. Market thesis and problem

### 2.1 The gap

Three gaps recur in the roadmap's own reasoning:

1. **Storage is open; policy is not.** A table in an open format can be read by any engine that has its files. Cairn's answer is that restricted tables are not handed to engines that read files directly. Those users query through Trino, which applies the policy (roadmap "Design decisions", and L2 ✅ for Spark).
2. **Agents arrive as new identities.** AI assistants, code-first agents and pipeline agents need data. Cairn's answer is that every model call and tool call runs as the caller, with the same policy, through the AI gateway (L99 ✅), intents that declare purpose and permitted actions (L109 ✅), sandboxed code execution (L116 ✅), and abuse detection on inputs (L114 ✅).
3. **Lineage and ownership stop at tool boundaries.** Cairn's answer is that engines register their runs back into Cairn Core. This covers Trino, Spark, dbt, Airflow (OpenLineage), notebooks and CDC. The platform table in the roadmap records column-level lineage as ✅.

### 2.2 Thesis (hypothesis)

Organisations that run open engines in their own network will pay for one governed control plane across those engines, rather than one catalogue, one governance product and one orchestrator per engine. This is a hypothesis. Design-partner evidence that supports or refutes it is not in the repository. **[TO CONFIRM: customer interviews or design-partner evidence, with dates and sample size]**

### 2.3 Market size

**[TO CONFIRM: market sizing with a sourced estimate: total, serviceable and obtainable market, with the source, publication date and method. No figure is stated in this document.]**

### 2.4 Competitive context

The roadmap benchmarks individual capabilities against incumbents: Snowflake and Databricks (storage, catalogue, SQL, sharing), dbt and Airflow (transformation and orchestration), catalogue vendors (search, lineage, glossary), and MLflow-based platforms (ML). These are capability references, not market data. **[TO CONFIRM: a dated competitor analysis with sources, covering pricing and packaging as well as features.]**

---

## 3. Product definition

### 3.1 What Cairn is

Cairn is an active-metadata and governance platform built on **Cairn Core**. Cairn Core is the metadata system of record: assets, lineage, glossary, domains, data products and contracts. Everything else is arranged in four rings around it (`cairn/ROADMAP.md`, "Cairn Core at the centre"):

| Ring | What it does with the graph | Examples |
|---|---|---|
| **Core** | Is the graph | Assets and catalogue, search, lineage, glossary, domains, data products, contracts, marketplace and access requests |
| **Govern** | Decides on the graph | PDP (ABAC and RBAC, deny overrides), personas and purposes, masking, row filters, classification, certification, access reviews, residency, tokenisation, audit |
| **Activate** | Keeps the graph alive | Connectors and remote agents, data quality, observability, AI context (assistant, MCP, ontology, semantic search), Slack, Teams, embeds |
| **Extend** | Runs work that registers back into the graph | Cairn Store and Cairn Catalog (lakehouse), Trino, Spark, dbt, SQL and BI, flows, managed Airflow and PostgreSQL, ML, agents, CLI, desktop and SDKs |

### 3.2 Architecture facts (verified)

The following were checked in the repository on 2026-11-14.

| Component | Fact | Source |
|---|---|---|
| Cairn Core | Built from the open-source DataHub project, pinned at **v1.8.0**, with Cairn's patches and Java plugins | `cairn/core/UPSTREAM_VERSION`, `cairn/core/UPSTREAM.md` |
| SQL engine | Trino **484** | `cairn/deploy/versions/pins.json` |
| Spark | Spark **4.0.1** with Iceberg **1.10.0** | `pins.json`, Spark image |
| Orchestration | Apache Airflow **3.3.2** (chart 1.20.0); dbt-core **1.12.6** with dbt-trino **1.11.0** | `pins.json` |
| Catalogue | Cairn Catalog (Rust, Iceberg REST); iceberg-rust 0.11 for v3 features | L86 |
| Object store | Cairn Store (Rust, S3 API) | Roadmap "The platform today" |
| Other pinned engines | Apache Polaris 1.8.0 and Unity Catalog OSS 0.7.0 as test stand-ins; Apache NiFi 2.12.0 for managed deployments; deltalake 1.6.6 in tests | `pins.json` |
| Schema | **157** numbered migrations; latest `0157_managed_airflow_leases.py` | `cairn/src/cairn/migrations/versions/` |
| API surface | About 160 source modules create a FastAPI `APIRouter` | `cairn/src/cairn/**/*.py` |
| Helm | Chart `cairn` version 0.2.0. Profiles: default, `values-ha`, `values-tenant`, `values-authoritative`, `values-active-metadata` | `cairn/helm/cairn/` |
| Helm resilience | Pod disruption budgets in 3 templates; network policies in 4 | `cairn/helm/cairn/templates/` |
| Infrastructure as code | Terraform modules for `cairn`, `aws`, `azure`, `gcp`, `hetzner`; examples including `kind` | `cairn/deploy/terraform/` |
| Continuous integration | **17** jobs in `cairn-ci.yml`, including `python`, `store`, `catalog`, `trino`, `ml`, `pipelines`, `spark-kind`, `ui`, `java`, `hetzner`, `helm`, `stack` and `live-model` | `.github/workflows/cairn-ci.yml` |
| Tenants | Hosted tenants: one release per namespace | `cairn/deploy/hosting/tenant.sh` (per `AGENTS.md`) |

**Java runtime (open item).** v0.2 stated that Cairn Core runs on Java 25. The repository does not confirm this. Core is built from DataHub's images. The authorizer plugin's build emits Java 17 bytecode with a default toolchain of 21, and its build comment says that DataHub 1.6 and later need Java 25 class files (`plugins/java/cairn-authorizer/build.gradle`). **[TO CONFIRM: the Java version the deployed Core image runs, and the toolchain used in CI.]**

### 3.3 Deployment models

| Model | What runs where | Status |
|---|---|---|
| Self-hosted, Docker Compose | Evaluation and small installs | Compose stack ✅ (roadmap 0.5, legacy) |
| Self-hosted, Kubernetes with Helm | Control and data plane in the customer's cluster | Chart linted and validated with kubeconform in CI; live-cluster validation open (🟡 in several items) |
| Hosted tenants | One release per tenant namespace; signup and billing | L27 ✅, L28 ✅, L68 ✅ |
| Split control and data plane | Cairn hosted; Trino, Spark and jobs in the customer's cluster | L135 🟡, L136 🟡 |
| Air-gapped | Signed release bundles as one file | L162 🟡 |

---

## 4. Scope

### 4.1 Product areas

The roadmap groups capabilities into four rings. This BRD groups the 185 numbered items into thirteen areas for reading. The grouping is editorial, and the appendix of the v0.2 BRD (`cairn/doc/BRD-appendix-roadmap.md`) is the item-level reference. The column totals below were re-added on 2026-11-14 and agree with the roadmap's 185, 124, 43 and 18. Item-level membership of each area was not re-verified.

| Area | Scope in one line | Items | ✅ | 🟡 | ⬜ |
|---|---|---|---|---|---|
| A. Lakehouse storage and tables | Iceberg and Delta tables, clones, branches, streams in, retention, residency, vector search | 27 | 23 | 3 | 1 |
| B. Query engines, SQL and data apps | Trino SQL, workloads, workflows, functions, workspaces, data apps | 15 | 13 | 1 | 1 |
| C. Transformation, orchestration and pipelines | Flows, dbt, Airflow, Spark-based transforms, database mirrors, managed databases | 17 | 8 | 7 | 2 |
| D. Data ingestion and SaaS connectivity | SaaS sources and reverse ETL, documents, NiFi, connector builder, reach to remote data | 7 | 0 | 4 | 3 |
| E. Notebooks, machine learning and model serving | JupyterHub, MLflow, serving, fine-tuning, hyperscaler models | 10 | 6 | 1 | 3 |
| F. Business intelligence and self-service analytics | Workbooks, dashboards, metrics, input tables, alerts, exports, embeds | 20 | 19 | 1 | 0 |
| G. AI assistant and agents | Assistant, agent kit, AI gateway, MCP, memory, sandboxes, scheduled tasks | 27 | 21 | 6 | 0 |
| H. Discovery, metadata and ontology | Catalogue, ontology, glossary, question spaces, external catalogues | 8 | 3 | 0 | 5 |
| I. Data quality and reliability | Partition-aware monitors, quality at source | 2 | 1 | 0 | 1 |
| J. Governance, privacy and compliance | Access reviews, retention and legal hold, consent, tokenisation, aggregation policies | 7 | 7 | 0 | 0 |
| K. Identity, security and remote agents | SAML, passkeys, secrets, quotas, customer keys, agent hardening, split planes, egress | 10 | 8 | 2 | 0 |
| L. Platform operations and commercial | Signup and billing, metering, status page, HA, observability, releases, fleet, air-gap | 20 | 10 | 9 | 1 |
| M. Developer and admin experience | CLI, desktop app, IaC, SDKs, forms, onboarding, design system, workspace | 15 | 5 | 9 | 1 |
| **Total** | | **185** | **124** | **43** | **18** |

### 4.2 Out of scope (this version)

- Replacing source systems' transactional applications. Cairn reads them through mirrors, connectors and agents.
- A general-purpose consumer BI product. Cairn's BI is governed analytics inside the platform. Its trust components embed in other BI tools (L40 ✅).
- An open public marketplace. A cross-organisation exchange is planned (L184 ⬜) and not committed here.
- Hosting one customer's data inside another customer's tenant.

### 4.3 Status definitions

- **✅ Shipped:** built and tested end to end in CI, against the real binaries and images.
- **🟡 Partly validated:** built and shipped, with some checks not yet run live. The roadmap usually says which checks are outstanding.
- **⬜ Planned:** not built. Items L168 to L185 come from the November 2026 capability audit and are not committed to a release.

Only ✅ counts as shipped in §10, §12 and the success measures. Where the roadmap text records an open live check under a ✅ marker, this document keeps the marker and flags the item in Appendix A. Marker changes belong in `cairn/ROADMAP.md`, not here.

---

## 5. Stakeholders, personas and use cases

### 5.1 Personas

The four personas that the VDR review asked for are listed first. The remaining personas from v0.2 follow.

| Persona | Who | What they need from Cairn | Areas |
|---|---|---|---|
| **Enterprise data architect** | Designs the estate across engines and clouds | One policy and one graph across engines; open formats; a design where the data plane stays in the customer's network; infrastructure defined as code | A, K, M |
| **Data platform engineer** | Runs the platform | Install, upgrade, scale and recover the platform; monitor it; manage tenants, pools, warehouses and agent deployments | C, K, L, M |
| **AI platform operations** | Runs models, gateways and agents | A governed model gateway with routing and budgets; agent traces; abuse detection; sandboxes; evaluation before release | E, G, L |
| **Analyst and data steward** | Uses and curates governed data | Find assets; run governed queries; build dashboards without tickets; certify, review access and approve drafts | F, H, J |
| Platform admin | Runs the platform team | Install, upgrade, manage tenants and pools, health | K, L, M |
| Data engineer | Builds pipelines | SQL, dbt, Spark, Airflow and Flows as a pipeline identity; mirrors; lineage and failures | A, B, C, D |
| Data scientist and ML engineer | Builds models | Notebooks, MLflow, training on governed data, serving, fine-tuning | E, B |
| Security and compliance officer | Accountable for risk | Prove who accessed what; enforce masks, purposes and consent; export audit to a SIEM; check posture | J, K |
| Application developer | Builds data apps and agents | Data apps, agent code with sandboxes, the AI gateway, MCP tools, CLI, SDKs | B, G, M |
| Vendor operations (managed and fleet installs) | Runs many customer installs | Signed releases, update channels, fleet visibility, status and usage reporting | L |
| Automation and AI agents | Non-human callers | Act through MCP and APIs under a named identity and the same policy | G, K, M |
| Business owner or executive | Pays for and judges the platform | Cost and chargeback, risk reduction, the path off legacy vendors | L, A, B |

### 5.2 Use cases

| ID | Persona | Use case | Roadmap items | Status |
|---|---|---|---|---|
| UC-1 | Enterprise data architect | Define a table once; read it through Trino, Spark and pyiceberg under the same masks and row filters | L2 [✅], L3 [✅], L86 [✅] | ✅ |
| UC-2 | Enterprise data architect | Keep data and keys in the customer's network; share only with named recipients | L22 [✅], L135 [🟡], L8 [🟡] | 🟡 |
| UC-3 | Data platform engineer | Scale Trino workers and warehouses with load; warehouses stop when idle | Platform table [✅], L87 [✅], L6 [✅] | ✅ |
| UC-4 | Data platform engineer | Upgrade the stack against pinned, tested engine versions | L11 [✅], legacy M5 row 5.6 [⬜] | 🟡 |
| UC-5 | AI platform operations | Route model calls by capability, size and timeout; keep usage within budgets | L113 [✅], L99 [✅], L6 [✅] (cost budgets with alerts) | ✅ |
| UC-6 | AI platform operations | Find where agents' tool calls fail; check agent inputs for abuse | L101 [✅], L114 [✅] | ✅ |
| UC-7 | AI platform operations | Evaluate answers and agents before release | L171 [⬜] | ⬜ |
| UC-8 | Analyst | Ask a question and get a dashboard built only from governed tiles | L35 [✅], L34 [✅] | ✅ (live-model suite not yet run, see Appendix A) |
| UC-9 | Analyst and steward | Review who holds access and recertify it | L71 [✅], L66 [✅] | ✅ |
| UC-10 | Analyst | Find assets and their glossary terms; see column lineage | Platform table, Discovery [✅], L148 [✅] | ✅ |

---

## 6. Business requirements

Priority uses MoSCoW: **M** = must have for the target release, **S** = should, **C** = could. Each requirement cites the roadmap items that define it, with status in brackets.

### 6.0 Functional themes

The four themes below cover the requirements the VDR review asked for. Detailed requirements follow in §6.1 to §6.14.

| ID | Theme and requirement | Roadmap items (status) | Note |
|---|---|---|---|
| **FR-1** | **Multi-tenant query engine.** Governed SQL on shared infrastructure, with isolation between teams and tenants | Platform table, SQL warehouse [✅]; L6 [✅]; L87 [✅]; L60 [✅]; L68 [✅]; L51 [✅]; hosted tenants: `tenant.sh` (AGENTS.md) | Isolation is by resource group and warehouse within a deployment, and by namespace release across tenants. The roadmap does not describe a shared multi-tenant control plane. [TO CONFIRM: the managed-offer tenancy model] |
| **FR-2** | **Metadata cataloguing.** Assets, lineage, glossary, search, and discovery of schemas through APIs | Platform table, Discovery and metadata [✅]; L38 [✅]; L148 [✅]; L93 [✅]; L121 [✅]; L129 [✅]; L147 [🟡]; L174 [⬜] | Schema discovery has no dedicated numbered item. The un-numbered Discovery row and L93 are the evidence. [TO CONFIRM: a numbered item for schema discovery APIs, or acceptance of these rows] |
| **FR-3** | **Agentic control plane.** Model gateway, agents and tools that act as the caller under policy | L99 [✅]; L100 [✅]; L109 [✅]; L114 [✅]; L116 [✅]; L117 [✅]; L94 [✅]; L95 [✅]; L154 [🟡]; L155 [🟡]; L126 [🟡]; L142 [🟡]; L64 [🟡]; L171 [⬜]; L173 [⬜] | Agent autonomy is limited by declared scopes (read, query_data, propose) and intents. Infrastructure changes that agents propose are applied only after a person approves them (L126) |
| **FR-4** | **Unified governance.** One policy decision point for every engine, app and AI tool, with masks, purposes, audit and reviews | Legacy M2 (PDP, masking, certification) [✅]; L2 [✅]; L85 [✅]; L66 [✅]; L71 [✅]; L74 [✅]; L50 [✅]; L73 [✅]; L82 [✅]; L26 [✅]; L47 [✅]; L22 [✅]; L133 [✅]; M6 U3 personas and purposes (code and tests; live sign-off open) | Compliance sign-off on the live stack is open for U3. Policy-as-code CI is planned (legacy M2 row 2.11 [⬜]) |

### 6.1 Identity, access and security

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-ID-01 | People sign in once, through OIDC, SAML or SSO, with users and groups provisioned by SCIM | M | SAML L47 [✅]; OIDC and SCIM 2.0 (legacy M5 row 5.10 [✅]) |
| BR-ID-02 | Pipelines, notebooks and agents act as named pipeline users with personal access tokens, exchanged for short-lived, audience-bound engine tokens. Default 4 hours, maximum 12 | M | Principle "One identity" (un-numbered). The 12-hour ceiling is enforced in code (`identity/notebooks.py` and `identity/workload.py`, 43200 s). The 4-hour default is stated in the roadmap and is not confirmed in code |
| BR-ID-03 | No engine, project file, notebook or agent stores a database password or store key in plain text; secrets are scoped to owners or groups | M | L48 [✅] |
| BR-ID-04 | Strong sign-in for sensitive actions: passkeys, and a fresh passkey check (step-up) before the most sensitive changes | M | L83 [✅] |
| BR-ID-05 | Customer-managed keys in AWS KMS, Azure Key Vault or Google Cloud KMS, as well as Vault and KMIP HSMs for Cairn Store | M | L22 [✅] |
| BR-ID-06 | Per-tenant and per-user API quotas, and tenant IP allowlists | S | L51 [✅] |
| BR-ID-07 | The platform checks its own configuration for risky settings and reports them | S | L82 [✅] |
| BR-ID-08 | Agent runs declare a purpose and the kinds of data they need, and are checked against it; prompt injection and exfiltration are detected | M | L109 [✅], L114 [✅] |

### 6.2 Policy, governance and privacy

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-GOV-01 | One policy decision point decides access for every engine, app, metadata API and AI tool | M | Principle "One policy"; legacy M2 row 2.2 [✅] |
| BR-GOV-02 | Deny overrides allow; a decision that cannot be made is a refusal | M | Principle "Fail closed"; legacy M2 row 2.2 [✅] |
| BR-GOV-03 | Masks, row filters and aggregation-only or minimum-group policies apply on every read path; an engine that cannot enforce them is refused restricted tables | M | L2 [✅], L85 [✅], legacy M5 row 5.8 [✅] |
| BR-GOV-04 | PII is classified and the classification propagates over column lineage | M | Architecture Module B; legacy M2 row 2.6 [✅] |
| BR-GOV-05 | Personas and purposes grant access by role and purpose; a purpose deny overrides a persona grant; consent is required where a purpose needs it | M | M6 U3 (code and tests; live compliance sign-off open); L66 [✅] |
| BR-GOV-06 | Access is approved with separation of duties; grants can be time-boxed and revoked; periodic access reviews recertify who holds what | M | L71 [✅]; M6 U2 (code and tests; live Slack round-trip open) |
| BR-GOV-07 | Retention is enforced, with legal hold and automated erasure | M | L50 [✅] |
| BR-GOV-08 | Sensitive values can be tokenised, with token domains holding their own keys | S | L74 [✅] |
| BR-GOV-09 | Geospatial data is governed; location fields can be masked to coarse areas | S | L79 [✅] |
| BR-GOV-10 | Data residency: lakehouse namespaces pinned to a region | M | L73 [✅] |
| BR-GOV-11 | Policy changes are validated in CI before they reach a deployment | S | Legacy M2 row 2.11 [⬜] (policy-as-code CI). No numbered L-item. [TO CONFIRM: whether this needs an L-number] |

### 6.3 Lakehouse storage and tables (Area A)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-LH-01 | Object storage with replication, erasure coding, versioning, object lock, lifecycle and tiering | M | Platform table, Object storage [✅] |
| BR-LH-02 | Iceberg tables with per-table credential vending and governed access; Delta Lake tables readable and writable without conflict | M | L19 [✅], L86 [✅] (Iceberg v3: deletion vectors, row lineage, VARIANT) |
| BR-LH-03 | Table maintenance: snapshot expiry, orphan removal, compaction, on a schedule | M | L1 [✅] |
| BR-LH-04 | Zero-copy clones, undrop, and table branches and tags, with write-audit | M | L12 [✅], L75 [✅] |
| BR-LH-05 | Registering Parquet files already in the store as tables without copying them | S | L115 [✅] |
| BR-LH-06 | Tables placed on the customer's own cloud storage, and external Iceberg catalogues governed alongside Cairn's | S | L132 [🟡], L105 [✅] |
| BR-LH-07 | Storage credentials and external locations are governed objects | M | L133 [✅] |
| BR-LH-08 | Snowflake and Databricks tables can be read in place through governed catalogues | S | L151 [🟡] |
| BR-LH-09 | Change data feeds for consumers that sync; materialized views refreshed when their inputs change | S | L25 [✅], L24 [✅] |
| BR-LH-10 | Serving tables for interactive analytics and hybrid transactional use | S | L103 [✅] |
| BR-LH-11 | Governed volumes (folders of files) under Cairn's policies | S | L58 [✅] |
| BR-LH-12 | Vector indexes over lakehouse text, with search by meaning that respects policy | S | L16 [✅] (approximate search for very large tables is still listed as to do in L16) |
| BR-LH-13 | Bad rows are quarantined rather than failing a whole load | M | L72 [✅] |
| BR-LH-14 | Synthetic test data shaped like real tables, with no real people's data | S | L76 [✅] |
| BR-LH-15 | Point-lookup acceleration with bloom filters on the columns queries look rows up by | C | L81 [✅] |
| BR-LH-16 | Stateful streaming SQL with a declared freshness target | C | L176 [⬜] |

### 6.4 Ingestion, mirrors and connectivity (Areas C and D)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-ING-01 | Streams from Kafka and Debezium CDC into Iceberg, exactly once | M | L5 [✅] |
| BR-ING-02 | HTTP streaming ingestion: producers post rows without Kafka | S | L13 [✅] |
| BR-ING-03 | Files as data: directory tables and text extraction; speech-to-text and AI description and field extraction for files | S | L15 [✅], L77 [✅], L78 [✅] |
| BR-ING-04 | PostgreSQL tables mirrored into the lakehouse through logical replication, with whole-schema selection | M | L89 [✅], L166 [🟡] |
| BR-ING-05 | Mirrors from MySQL, SQL Server and MongoDB change streams | S | L92 [✅], L96 [✅], L97 [✅] |
| BR-ING-06 | Log-based mirrors from Oracle, then Db2 and SAP HANA | S | L167 [🟡] (Oracle built; Db2 and SAP HANA not listed as built) |
| BR-ING-07 | SaaS sources (Salesforce, HubSpot and similar) loaded into Iceberg, with reverse ETL to SaaS apps | S | L61 [🟡], L62 [🟡] |
| BR-ING-08 | A declarative connector builder and runner for REST sources | S | L165 [🟡] |
| BR-ING-09 | Document sources (SharePoint, Google Drive) kept in governed storage | C | L98 [🟡] |
| BR-ING-10 | Apache NiFi 2 deployments, managed per team and environment, with governed connectors | C | L152 [🟡] |
| BR-ING-11 | Remote agents inside the customer's network pull data from private sources, with outbound-only connections and no inbound ports | M | L44 [✅] (remote agent hardening); Architecture Module C |
| BR-ING-12 | Connector coverage: each catalogue connector has a live ingestion test; ETL and SaaS sources validated on real accounts | M | L168 [⬜], L182 [⬜] |
| BR-ING-13 | Reaching data in other networks through SSH tunnels and private endpoints | C | L178 [⬜] |

### 6.5 Query engines, SQL and workloads (Area B)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-SQL-01 | SQL on the lakehouse with Trino, with per-team warehouses that stop when idle and start on the first query | M | Platform table, SQL warehouse [✅]; L87 [✅] |
| BR-SQL-02 | Adaptive warehouse sizing: one endpoint picks the size for each query | S | L87 [✅] |
| BR-SQL-03 | Spark reads each table under Cairn's decision; restricted tables are read through Trino | M | L2 [✅], L3 [✅] |
| BR-SQL-04 | Spark on Kubernetes, in cluster mode and through the Spark Operator | S | L3 [✅] |
| BR-SQL-05 | Workloads are managed: resource groups per team, Spark quotas, chargeback | M | L6 [✅] |
| BR-SQL-06 | Cost is visible per query, user, table and cost centre; chargeback statements per month | M | L60 [✅], L80 [✅] |
| BR-SQL-07 | Query history, profiles and tuning hints | S | L14 [✅] |
| BR-SQL-08 | A SQL workspace comparable to leading cloud warehouses: editor, autocomplete, saved queries, history | M | L163 [✅] |
| BR-SQL-09 | Saved queries and notebooks kept in git, and stored functions and procedures governed as catalogue objects | S | L20 [✅], L21 [✅] |
| BR-SQL-10 | Python functions, sandboxed, callable from SQL and governed like SQL functions | S | L45 [✅] |
| BR-SQL-11 | Scheduled SQL workflows with steps that depend on each other | M | L17 [✅] |
| BR-SQL-12 | Data apps served by Cairn, each viewer signed in as themselves; app packages installed by other teams on their own data | S | L23 [✅], L90 [✅], L120 [✅] |
| BR-SQL-13 | System tables exposing Cairn's operational data as SQL | S | L134 [🟡] |
| BR-SQL-14 | Transactions across several lakehouse tables, richer table and aggregate functions, and governed HTTP calls from SQL | C | L180 [⬜] |

### 6.6 Transformation, orchestration and operational databases (Area C)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-TR-01 | dbt runs as a pipeline user; models, tests, owners and lineage are recorded in Cairn | M | Platform table, Transformation [✅] |
| BR-TR-02 | Derived tables refresh on a schedule, by watermark or by merge, and when what they read changes | M | Platform table, Transformation [✅] |
| BR-TR-03 | Slowly changing dimensions (type 2) and medallion layers | S | L110 [✅] |
| BR-TR-04 | Cairn Flows: one DAG mixing SQL, tables, checks, notebooks, Python, Spark, dbt, agents, Airflow DAGs, approvals and branches, run as its owner | M | L122 [✅] |
| BR-TR-05 | A visual flow canvas, with YAML in git and from agents | M | L123 [✅] |
| BR-TR-06 | Airflow both ways: a flow exports as an Airflow DAG, and Airflow can run a flow | S | L124 [🟡] |
| BR-TR-07 | Managed Airflow 3.3 deployments per team and environment, with promotion, rollback and hibernation | S | L144 [🟡] |
| BR-TR-08 | Pipeline observability for Airflow, and OpenTelemetry export across the platform | S | L145 [🟡], L141 [🟡] |
| BR-TR-09 | Flows that compose: sub-flows, task groups, mapping over sub-graphs, sensors, pools and priorities | S | L173 [⬜] |
| BR-TR-10 | Orchestration steps on other platforms, from Flows and from Airflow | S | L138 [🟡] |
| BR-TR-11 | dbt projects edited and built in the product, with environments and job pages | S | L172 [⬜] |
| BR-TR-12 | Operational PostgreSQL databases created, branched and managed per team | S | L88 [✅], L140 [🟡] |

### 6.7 Notebooks, machine learning and model serving (Area E)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-ML-01 | Notebooks with JupyterHub, signed in through Cairn, with kernels that read governed data | M | L4 [✅] |
| BR-ML-02 | Scheduled notebook and Python jobs in Cairn, each run as its owner | S | L57 [✅] |
| BR-ML-03 | MLflow tracking and model registry behind Cairn sign-in | M | L7 [✅] |
| BR-ML-04 | Managed model serving of registered versions, with autoscaling and traffic splits | S | L63 [✅], L69 [✅] |
| BR-ML-05 | Fine-tuning on governed data, where training examples are read with the caller's permissions | C | L91 [✅] |
| BR-ML-06 | Models from the hyperscalers' model services (Amazon Bedrock, Azure AI Foundry and similar) | S | L153 [🟡] |
| BR-ML-07 | Evaluation sets, scorers and judges for models and agents, with regression gates in CI | S | L171 [⬜] |
| BR-ML-08 | GPU pools, distributed training, and point-in-time training sets from feature tables | C | L179 [⬜] |
| BR-ML-09 | Co-edited notebooks, R, per-user governed Spark Connect and a VS Code extension | C | L183 [⬜] |

### 6.8 Business intelligence and self-service analytics (Area F)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-BI-01 | Workbooks: explore a table like a spreadsheet, with formulas, filters and charts | M | L31 [✅] |
| BR-BI-02 | Input tables: people type rows that are written to the lakehouse as themselves, with optional approval before changes land | M | L32 [✅], L42 [✅] |
| BR-BI-03 | Interactive dashboards with shared filters, cross-filtering and drill-down; they work on a phone | M | L33 [✅], L54 [✅] |
| BR-BI-04 | A metrics layer: named metrics defined once and used everywhere, including by outside BI tools | M | L34 [✅], L56 [✅] |
| BR-BI-05 | An AI builder that turns a question into a workbook or dashboard | S | L35 [✅] (the live-model suite is recorded as not yet run; see Appendix A) |
| BR-BI-06 | Forms and buttons on dashboards that write to input tables or call flows | S | L36 [✅] |
| BR-BI-07 | Scheduled exports as PDF or CSV, delivered by email, Slack and Teams; a result cache shared only between permitted viewers | S | L37 [✅], L55 [✅] |
| BR-BI-08 | Metric alerts with thresholds and anomaly detection | M | L53 [✅] (threshold and change rules; anomaly detection is not separately listed) |
| BR-BI-09 | Saved queries, workbooks and dashboards are catalogue entities, with version history and comparison | M | L38 [✅], L39 [✅] |
| BR-BI-10 | Dashboards embedded in other products with signed, origin-bound, revocable tokens | S | L40 [✅] |
| BR-BI-11 | Dashboards, workbooks and metrics defined as code | M | L41 [✅] |
| BR-BI-12 | Sharing with people and groups (viewer and editor roles); folders, comments and usage analytics | M | L49 [✅], L52 [✅] |
| BR-BI-13 | Map and heatmap charts | S | L54 [✅] |
| BR-BI-14 | Shared tables and share observability: who reads what, through each share | S | L106 [✅]; L8 open sharing [🟡] |
| BR-BI-15 | Spreadsheet add-ins for Excel and Google Sheets | C | L118 [🟡] |

### 6.9 AI assistant and agents (Area G)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-AI-01 | The assistant acts as the caller and sees only what the caller may see; the same governed tools serve the UI, the CLI and MCP | M | Architecture Module D; M6 U5 (code and tests; golden-set evaluation open); L154 [🟡] |
| BR-AI-02 | AI-written documentation, definitions and ontology proposals are drafts until a human approves them | M | Legacy M4 row 4.4 [✅] (documentation generation as drafts); M6 U4 (context agents: proposals accepted by a steward) |
| BR-AI-03 | Model provider is configurable (for example Anthropic, an OpenAI-compatible API, a cloud model service or local models). The platform works when no model is reachable | M | L99 [✅], L113 [✅]; M6 U5 (offline fallback) |
| BR-AI-04 | An AI gateway with virtual model names and routing rules, and an MCP tool registry for external servers | S | L113 [✅], L155 [🟡] |
| BR-AI-05 | The whole platform API is exposed as MCP tools | S | L154 [🟡] |
| BR-AI-06 | Agents built in code (`cairn agents init`, `dev`, `deploy`) and from the assistant's tools | S | L100 [✅], L64 [🟡] |
| BR-AI-07 | Agent sandboxes for Python and SQL, so agent code runs isolated | M | L116 [✅] |
| BR-AI-08 | Governed memory for the assistant and for agents, saved only when asked | S | L84 [✅], L111 [✅] |
| BR-AI-09 | Scheduled assistant tasks with an approved outbox; automations from the CLI | S | L95 [✅], L108 [✅] (L95's live-model checks are recorded as not yet run; see Appendix A) |
| BR-AI-10 | A pipeline agent that writes, tries, fixes and ships pipelines from a goal | S | L94 [✅] (live-model checks recorded as not yet run; see Appendix A) |
| BR-AI-11 | Trace intelligence: what each agent's tool calls and model use say, and where they fail | S | L101 [✅] |
| BR-AI-12 | Grounded answers with sources from vector indexes | S | L102 [✅] |
| BR-AI-13 | AI search and document parsing callable from SQL and pipelines | S | L112 [✅] |
| BR-AI-14 | Data packages shared with their semantic context, so outside agents work on shared data safely | C | L117 [✅] |
| BR-AI-15 | The Cairn agent in GitHub Copilot and other AI coding tools, and the agent kit for project work | S | L156 [🟡], L164 [✅] |
| BR-AI-16 | Agent marketplace: agent apps, custom agents and extensions listed in the marketplace | C | L107 [✅] |
| BR-AI-17 | Usage measured, with budgets; AI setup as code | S | Platform table, AI [✅]; L158 [🟡] |
| BR-AI-18 | Evaluation of answers and agents before each release, with a golden set | S | L171 [⬜]; legacy M4 row 4.8 [⬜] (eval set for NLQ plans and drafts). Corrects the v0.2 citation "ROADMAP 4.8" |

### 6.10 Discovery, metadata and ontology (Area H)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-MD-01 | A typed metadata graph of assets, domains, glossary terms, data products and contracts (Cairn Core) | M | Cairn Core; Platform table, Discovery [✅] |
| BR-MD-02 | Search by keyword and by meaning, with facets the person is allowed to see | M | Platform table, Discovery [✅]; M6 U1 (code and tests; search p95 measurement open) |
| BR-MD-03 | Column-level lineage from every engine, dbt, Airflow, Spark, notebooks and CDC | M | Platform table, Lineage [✅] |
| BR-MD-04 | Glossary and domain editing | M | L148 [✅] |
| BR-MD-05 | Ontology compiled from the catalogue and query log, with an explorer and a review queue | S | L121 [✅], L129 [✅] |
| BR-MD-06 | Context agents propose glossary terms and relationships that a steward accepts | M | M6 U4 (code and tests; pilot-domain acceptance rate open) |
| BR-MD-07 | Bookmarks, follows, @mentions, threads, reactions and trending | S | M6 U1 (code and tests) |
| BR-MD-08 | Catalogue daily-use features in Cairn's own UI: custom metadata, badges, announcements, popularity | S | L174 [⬜] |
| BR-MD-09 | Curated question spaces with instructions and verified question-and-query pairs | S | L177 [⬜] |
| BR-MD-10 | Exchange of listings between organisations, with pricing and provider analytics | C | L184 [⬜] |

### 6.11 Data quality and reliability (Area I)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-DQ-01 | Monitors for freshness, volume, schema, field metrics and drift, with learned baselines and partition-aware volume checks | M | Platform table, Data quality [✅]; L43 [✅] |
| BR-DQ-02 | Failed checks raise incidents that resolve automatically when the data recovers | M | Platform table, Data quality [✅] |
| BR-DQ-03 | Data contracts are defined and checked | M | Platform table, Data quality [✅] |
| BR-DQ-04 | Quality and cost checks at the source (Snowflake and Databricks) | C | L181 [⬜] |

### 6.12 Governance workflows and audit

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-AUD-01 | Every change is one transaction with its audit entry | M | Principle "Fail closed" (un-numbered) |
| BR-AUD-02 | The audit log is hash-chained and tamper-evident | M | Platform table, Governance [✅] |
| BR-AUD-03 | Audit is pushed to a SIEM: Splunk HEC, syslog (RFC 5424 and CEF), at-least-once and ordered | M | L26 [✅] |
| BR-AUD-04 | Compliance forms and evidence can be exported for review | S | Platform table, Governance [✅] |
| BR-AUD-05 | Independent external penetration test and audited controls (SOC 2 or ISO 27001) before general availability | M | L185 [⬜]; L10 [🟡] (the penetration test) |

### 6.13 Platform operations, commercial and fleet (Area L)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-OPS-01 | Self-service signup, subscription through Stripe Checkout, and tenant licences | M | L27 [✅] |
| BR-OPS-02 | Tenants report daily usage; the control plane bills it | M | L28 [✅] |
| BR-OPS-03 | A public status page with components, 90-day uptime and incidents | S | L29 [✅] |
| BR-OPS-04 | High availability: replicated tenant search and messaging; failover tested | M | L68 [✅]; Helm HA is 🟡 in this document (see Appendix A) |
| BR-OPS-05 | Disaster recovery across regions: store replication, catalogue and database recovery | M | L9 [✅] |
| BR-OPS-06 | Load and resilience tests: policy-decision latency, and outage tests for Cairn Core, the PDP, the gateway and the catalogue | M | L10 [🟡], L46 [✅] |
| BR-OPS-07 | Upgrades are pinned and tested against each new engine and Cairn Core release | M | L11 [✅] |
| BR-OPS-08 | Gateway API routing as an alternative to Ingress | S | L30 [✅] |
| BR-OPS-09 | Platform observability: OpenTelemetry traces, metrics and logs; operations dashboards | S | L141 [🟡], L143 [🟡] |
| BR-OPS-10 | An SRE agent with a built-in identity, for operations and managed Postgres | C | L142 [🟡] |
| BR-OPS-11 | Signed releases with channels; scheduled updates within maintenance windows; a fleet console showing every customer install | M | L159 [🟡], L160 [🟡], L161 [🟡] |
| BR-OPS-12 | Air-gapped release bundles delivered as one file | M | L162 [🟡] |
| BR-OPS-13 | Split control and data planes; network controls for user code, including egress allowlists and private networking | M | L135 [🟡], L136 [🟡] |
| BR-OPS-14 | Sample datasets for new tenants | C | L70 [✅] |
| BR-OPS-15 | Customer-facing reports on usage and chargeback | S | L80 [✅] |

### 6.14 Developer and administrator experience (Area M)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-DX-01 | Everything configurable is code: bundles from git, a Terraform provider, and a registry of resource types | M | L18 [✅], L125 [🟡], L137 [✅] |
| BR-DX-02 | Agents plan and propose infrastructure changes; a person with the right privileges approves and applies | M | L126 [🟡] |
| BR-DX-03 | Every create, edit and run in the platform hubs is a form | S | L127 [✅] |
| BR-DX-04 | First-run onboarding by role, with a "get started" checklist | S | L128 [✅] |
| BR-DX-05 | A command-line tool that signs in through the browser and runs queries, search and assistant requests | M | L130 [🟡] |
| BR-DX-06 | A desktop app with several workspaces | C | L131 [🟡] |
| BR-DX-07 | Clients for Python, JavaScript and Java, with a deprecation policy for the API | S | L65 [✅]; L175 [⬜] (a Python client for the whole API) |
| BR-DX-08 | A design system shared by the web UI and the workspace | S | L146 [🟡] |
| BR-DX-09 | One home per concept in the product (information architecture), and a calmer, consistent workspace | S | L147 [🟡], L149 [🟡], L150 [🟡] |
| BR-DX-10 | One administrator section for people, roles and access | M | L139 [✅] |
| BR-DX-11 | Accessibility: the main pages are checked with automated accessibility tests in CI | M | L67 [✅] |
| BR-DX-12 | Profiles per person that the assistant and schedules use | C | L119 [🟡] |
| BR-DX-13 | Packages of data apps published by one team and installed by others | S | L90 [✅] |

---

## 7. Non-functional requirements

### 7.1 Performance

The roadmap records load tests and their results in L10. This document states the targets and where they are measured. It does not restate measured values, which belong to the test artifacts and to `cairn/ROADMAP.md`.

| ID | Area | Target | Measured by | Status |
|---|---|---|---|---|
| NFR-P1 | Policy decisions, cached | p99 under 5 ms at 2,000 requests per second, on one thread for the decision path | `tests/test_scale_decisions.py` asserts the target in CI. Results recorded in L10 | 🟡 (L10) |
| NFR-P2 | Policy decisions, uncached and catalogue scale | Target p99 under 25 ms uncached at 2,000 requests per second (legacy M5 row 5.3). A decision on a 1,000-table job | L10 (catalogue-scale decision); legacy row 5.3 is 🟡 | 🟡 |
| NFR-P3 | Query performance on Trino | **[TO CONFIRM: latency and concurrency targets by workload class, for example interactive, dashboard and batch, agreed with Engineering]**. The roadmap records no query-latency target. L6 records a load test of workload isolation under Trino | L6 (isolation only) | Target open; no roadmap item |
| NFR-P4 | Search and asset pages | Search p95 under 300 ms (U1 exit criterion, `cairn/UX_ACTIVE_METADATA_SPEC.md`, Part 10). Measurement on a realistically sized catalogue | M6 U1: "search p95 measured" is open | 🟡 target set, measurement open |
| NFR-P5 | Object store throughput | Target to be set. L10 publishes store throughput at two object sizes as a CI artifact | L10 | 🟡 |

### 7.2 Elasticity and availability

| ID | Category | Requirement | Target | Status |
|---|---|---|---|---|
| NFR-09 | Scalability | Engines scale independently; Trino workers autoscale; warehouses start on demand and scale to zero | Verified in platform configuration and load test (L6); worker autoscaling per the platform table | ✅ |
| NFR-E2 | Elasticity | Model serving scales by in-flight concurrency | L63 [✅] | ✅ |
| NFR-06 | Availability | Control plane highly available under Helm: replicas, pod disruption budgets, network policies | **[TO CONFIRM: availability target. v0.2 proposed 99.9% monthly; this is a proposal and not agreed.]** | 🟡 |
| NFR-A2 | Recovery | Cross-region recovery with a tested failover runbook; recovery point and recovery time measured in CI (values recorded in L9) | **[TO CONFIRM: the RPO and RTO committed for general availability]** | ✅ (L9) |
| NFR-A3 | Tenant services | Replicated tenant search and messaging; failover tested | L68 [✅] | ✅ |

### 7.3 Security and assurance

| ID | Category | Requirement | Target | Status |
|---|---|---|---|---|
| NFR-01 | Security | Access decisions fail closed | Verified by tests | ✅ |
| NFR-02 | Security | Secrets never leave the customer network in plain text; secret-confinement tests | Verified | ✅ |
| NFR-03 | Security | Remote agents hold no Cairn Core credentials and accept no inline secrets | Verified | ✅ |
| NFR-04 | Security | Threat model with a negative test for each attack (`docs/site/agent-security.md`) | Maintained | ✅ (legacy M5 row 5.5, threat-model part) |
| NFR-05 | Security | External penetration test of the gateway and policy engine | Before general availability | ⬜ (L185; L10 [🟡]) |
| NFR-A4 | Security | Customer-managed keys for the store and the gateway identity; the gateway's keys wrapped by Vault transit or AWS KMS | Store keys (L22); gateway keys (L10) | Store shipped (L22). Gateway partly validated (L10) |
| NFR-A5 | Security | Security posture scan with ten checks, including trusted headers, open policies, audit chain and unmasked PII | L82 | ✅ |
| NFR-16 | Supply chain | Images signed with SBOMs; non-root; minimal OS packages | Verified (legacy M5 row 5.1) | ✅ |
| NFR-A6 | Assurance | SOC 2 or ISO 27001 audit | **[TO CONFIRM: audit standard and timeline (Q-02)]** | ⬜ (L185) |

### 7.4 Operability, portability and quality

| ID | Category | Requirement | Target | Status |
|---|---|---|---|---|
| NFR-10 | Operability | Monitoring dashboards, health checks, structured logs, traces | L141, L143 (partly validated) | 🟡 |
| NFR-11 | Portability | Runs in Docker Compose for evaluation, in Kubernetes with Helm, and on Terraform modules for AWS, GCP, Azure and Hetzner | Compose verified; Helm on live clusters pending | 🟡 |
| NFR-12 | Upgrades | Pin-and-test against each release; upgrade playbook | L11 ✅; the upgrade playbook is legacy M5 row 5.6 [⬜] | 🟡 |
| NFR-13 | Licensing | Third-party licences are listed and ship with every distribution | `THIRD_PARTY_NOTICES.md` | ✅ |
| NFR-14 | Accessibility | Main pages checked with axe-core (WCAG 2.1 A and AA) in CI | L67 [✅]. An independent audit is not in the roadmap **[TO CONFIRM: whether an independent audit is required]** | ✅ (automated checks) |
| NFR-15 | Internationalisation | Interfaces translatable; documentation in English, German, Spanish, French and Japanese | `docs/site/` has `de`, `es`, `fr` and `ja` folders | ✅ |
| NFR-17 | Air-gap | Install and update without outbound access | Bundles (BR-OPS-12) | 🟡 |

---

## 8. Monetisation strategy

### 8.1 Commercial mechanisms in the product

These mechanisms exist as roadmap items. Their prices and packaging are not set.

- **Signup, Checkout and licences (L27 ✅):** self-service signup with email confirmation, Stripe Checkout, and a tenant licence that follows its subscription. A cancelled subscription turns read-only after a grace period, and the data is kept.
- **Usage metering (L28 ✅):** tenants send signed daily usage reports. Settled days are sent as Stripe meter events, with idempotent identifiers. The roadmap names Trino CPU, warehouse time and AI spend as billable inputs.
- **Cost attribution (L60 ✅, L80 ✅, L6 ✅):** cost per query, per user, per team and per table, with chargeback statements per month, and cost budgets with threshold and forecast alerts.
- **Warehouse sizing and scale-to-zero (L87 ✅, L6 ✅):** warehouses that stop when idle and resource groups per team.
- **Status page (L29 ✅):** public uptime and incidents, which support service-level conversations with buyers.

### 8.2 Options compared

Two families are under evaluation: consumption-based pricing, where a buyer pays for metered use, and compute-tiering, where a buyer chooses a tier that sets limits and features. A hybrid is also possible.

| Criterion | Consumption-based | Compute-tiering | Hybrid (platform fee, tier, metered AI) |
|---|---|---|---|
| Buyer predictability | Lower: spend follows usage | Higher: fixed per tier | Medium |
| Alignment with value | Strong for query and AI volume | Weaker: value not tied to use | Strong for AI, moderate for compute |
| Dependence on metering | High: needs L28 at invoice accuracy (R-10) | Low: tiers need only limits | High for the metered part |
| Fit with self-hosted installs | Needs signed reports from customer-run installs. The mechanism exists for tenants (L28). Self-hosted licensing is open **[TO CONFIRM]** | Natural: a tier is a licence key | Mixed |
| Procurement friction | Higher: budgets are hard to forecast | Lower | Medium |
| Operational cost to Cairn | Metering, reconciliation and disputes | Limit enforcement and tier management | Both |
| Mechanisms in product | L28, L60, L80, L6, L87 | L27 licence tie to subscription; L6, L87 limits | All of the above |

### 8.3 Position

**The model under evaluation is [TO CONFIRM: the pricing model, the decision owner and the date].** This document does not recommend a model. The comparison is an input to that decision.

### 8.4 Prices

No price, unit price, discount, trial length or tier limit is stated in this document. **[TO CONFIRM: the approved price list, with its approver and effective date.]**

### 8.5 Conditions for the decision

The chosen model should satisfy these conditions before it is offered:

1. Usage reports reconcile to invoices for one full cycle (R-10 and Stage 3 in §11).
2. The model works for self-hosted installs, or self-hosted is out of the offer **[TO CONFIRM]**.
3. AI spend is capped by budgets (BR-AI-17 [✅ platform table; L158 🟡]).
4. The customer can see the cost attribution behind an invoice (L60, L80).

---

## 9. Assumptions, constraints and dependencies

**Assumptions**

- Customers run their own network and can host agents inside it.
- Customers accept open-source engines (Trino, Spark, Airflow, dbt, Iceberg, PostgreSQL, Kafka, JupyterHub, MLflow, NiFi) as the foundation.
- Cairn Core is built from DataHub v1.8.0 with Cairn's patches (`cairn/core/UPSTREAM.md`).

**Constraints**

- Engine versions are pinned and tested as a set (`deploy/versions/pins.json`): Trino 484, Spark 4.0.1 with Iceberg 1.10.0, Airflow 3.3.2, dbt-core 1.12.6, iceberg-rust 0.11 for v3 features.
- The authorizer plugin emits Java 17 bytecode. The Core runtime Java version is an open item (§3.2).
- Docker Hub rate limits and TLS-intercepting proxies affect image builds in some networks. Build scripts support mirrors and extra CA files.
- Database schema changes go through numbered migrations (latest 0157).

**Dependencies**

- Cairn Core and its search and messaging stack (OpenSearch or Elasticsearch, Kafka, MySQL or PostgreSQL).
- The customer's identity provider for SSO and SCIM.
- Optional: a model provider (Anthropic, an OpenAI-compatible API, a hyperscaler model service or a local model); Slack or Teams; Vault, AWS Secrets Manager, KMS or Key Vault; Stripe for billing.

---

## 10. Risks and issues

| ID | Risk or issue | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R-01 | 43 items marked 🟡 are not yet validated live: Helm on a cluster, real Slack and Teams, live model calls, BI-embedded components, fleet and air-gap flows. The current release (L139 to L152) has 12 of 14 items 🟡 | High | High | Live validation list before each release (§11, stage 1). Treat 🟡 as not generally available | Engineering |
| R-02 | Cairn Core or engine upgrades break shapes or fields | Medium | High | Pin-and-test matrix (weekly, L11, ✅). Upgrade playbook: legacy M5 row 5.6, ⬜ | Engineering |
| R-03 | Security findings in the gateway, policy engine or agent sandboxes before the external test | Medium | Critical | External test before general availability (BR-AUD-05); threat model maintained; sandbox negative tests | Security |
| R-04 | Policy misconfiguration grants access by mistake | Medium | High | Policy validation (BR-GOV-11, planned); deny-overrides; purposes at higher priority; access reviews (L71) | Security / Product |
| R-05 | Scope breadth: 185 roadmap items, 18 of them planned, and a small team | High | High | Prioritise by this BRD; cut "could" items first; area owners | Product |
| R-06 | Competition from catalogue, warehouse and BI vendors with larger teams | High | Medium | Self-hosted, one-policy story; migration tooling (L170 ⬜); open engines | Product |
| R-07 | Air-gapped or restricted networks cannot run the design as built | Medium | High | Outbound-only agents (L44); bundles (BR-OPS-12); documented network requirements | Engineering |
| R-08 | Model output is wrong, or a provider changes terms or prices | Medium | Medium | Evaluation (BR-AI-18, L171 ⬜); AI output is drafts; offline fallback; provider configurable | Product |
| R-09 | Two backlogs disagree: `AGENTS.md` describes a 65-item queue, while the roadmap numbers 185 items | High | Medium | Decision Q-06 | Product / Engineering |
| R-10 | Commercial operations (billing, signup, status, fleet) run live for the first time at scale | Medium | High | Stage 3 gate in §11; usage reconciliation test | Operations |
| R-11 | Shipped markers that rest on checks not yet run: L35, L94 and L95 record the live-model suite as "not yet run"; legacy row 5.5 is ✅ for the threat model while its external pentest is ⬜ | Medium | High | Re-mark in `cairn/ROADMAP.md`, or run the checks and record the result (Appendix A) | Engineering |
| R-12 | Two numbering systems (the L-queue and the legacy M0 to M6 rows) are cited in different documents | High | Low | Use L-numbers in all new text; cite legacy rows with their milestone (Appendix A) | Product |
| R-13 | UX milestones U1 to U7 are shipped as code and tests, with live checks open. The v0.2 BRD marked them ✅ | High | Medium | Mark as 🟡 in this BRD (done); align in `cairn/ROADMAP.md` | Product |

---

## 11. Release approach

Releases are gated on the roadmap's markers, not on dates. A capability counts as released when it is ✅ shipped.

| Stage | Entry | Exit |
|---|---|---|
| **Stage 1: Live validation** | Current code base | Each 🟡 item that a release depends on has its live checks run, with recorded results (cluster, Slack and Teams endpoints, a live model, BI embeds, fleet and air-gap flows) |
| **Stage 2: Assurance** | Stage 1 exit | External penetration test passed (BR-AUD-05); SOC 2 or ISO 27001 plan agreed; key management reviewed (BR-ID-05) |
| **Stage 3: General availability** | Stage 2 exit | Upgrade playbook tested against one engine and one Cairn Core minor release; billing and status reconciled for one full cycle; runbooks and support in place |
| **Stage 4: Growth** | GA | Planned items (⬜) prioritised by customer demand and by §6 |

The three phases in `docs/product/ROADMAP.md` are a grouping of the same items. Phase 3's exit corresponds to Stage 2 and Stage 3 exit. This mapping is a proposal **[TO CONFIRM: product owner sign-off]**.

---

## 12. Acceptance criteria

A business requirement is accepted when all of the following hold:

1. Its behaviour has an automated test in CI, and an end-to-end test against real components where it crosses services (the roadmap's standard).
2. It is assigned to one of the four rings in `cairn/ROADMAP.md`, and states what it reads from and writes back to Cairn Core.
3. Access decisions fail closed, and audit entries are written in the same transaction as the change.
4. Its operator and user documentation is updated in `cairn/docs/site/`, and its roadmap status is updated.
5. The product owner has checked the behaviour against §5.

---

## 13. Success measures

The first twelve months after general availability are measured as follows. The commercial figures are planning figures carried over from v0.2. They have not been verified and are not facts.

| Measure | Target | Status |
|---|---|---|
| Design partners converted to paid plans | 3 **[TO CONFIRM: planning figure from v0.2; source and owner]** | Planning |
| Paying organisations and active developer seats | 40 organisations and 2,000 seats **[TO CONFIRM: planning figures from v0.2]** | Planning |
| Net revenue retention, pilot cohort | Above 110% **[TO CONFIRM: planning figure from v0.2]** | Planning |
| Connected projects with weekly scanning or querying | At least 60% **[TO CONFIRM: definition of "connected project" and how it is measured]** | Planning |
| Live validation of each 🟡 item used in a release | Recorded result for each | Target |
| Every ✅ marker backed by a passing CI run | No ✅ item with an open live check in `cairn/ROADMAP.md` | Target (Appendix A) |

---

## 14. Open questions

| # | Question | Why it matters | Decision needed from |
|---|---|---|---|
| Q-01 | Which offer comes first: self-hosted only, managed (signup and billing are built), or both? | Changes support, pricing, GA checklist and the hosting commitments | Leadership |
| Q-02 | Which compliance standard comes first, SOC 2 or ISO 27001? | Sets the audit timeline and budget (BR-AUD-05) | Security |
| Q-03 | What are the performance targets for query latency (NFR-P3), and the RPO and RTO for general availability (NFR-A2)? | NFR-P3 cannot be tested without a target. Search has a target (NFR-P4) | Engineering |
| Q-04 | Which model provider is the default for paying customers, and is local-only operation required? | Affects BR-AI-03 and data-residency promises | Product / Security |
| Q-05 | Which migration source comes first, Snowflake or Databricks? | Sets the order of L170 and the connector work in BR-ING-12 | Product |
| Q-06 | Which list is the working backlog: the roadmap (185 items) or the 65-item queue in `AGENTS.md`? | Two lists risk two plans (R-09) | Product / Engineering |
| Q-07 | Who owns Stage 1 live validation and its sign-off? | Nothing in §11 passes without it | Engineering |
| Q-08 | Should schema discovery APIs have a numbered item? | FR-2 relies on an un-numbered row and L93 | Product |
| Q-09 | What is the sourced market-size estimate (§2.3), and who owns it? | No market figure is stated | Leadership |
| Q-10 | Which pricing model is under evaluation (§8), and when is it decided? | Blocks the commercial offer | Leadership |
| Q-11 | What Java runtime does the deployed Cairn Core image use (§3.2)? | v0.2 stated Java 25 without evidence | Engineering |
| Q-12 | Should the markers of L35, L94, L95 and legacy row 5.5 change until their checks run (R-11)? | Affects the ✅ count in §4.1 and §10 | Engineering |

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **Cairn Core** | The metadata system of record: typed graph, search, lineage, catalogue UI and APIs. Built from the open-source DataHub project, pinned at v1.8.0, with Cairn's patches |
| **Control plane** | The Cairn services on top of Core: quality, governance, agents, AI, MCP, actions, billing |
| **Data plane** | Where data is read and processed. Can run apart from the control plane (L135) |
| **PDP** | Policy decision point. Decides every access request with deny-overrides |
| **Persona / purpose** | A role bundle of grants, and a stated reason for access. A purpose deny overrides a persona grant |
| **Pipeline user** | A non-human identity that runs jobs and holds personal access tokens |
| **Remote agent** | An outbound-only process inside a customer network that reads private sources |
| **Input table** | A table people type into, written to the lakehouse as themselves |
| **Flow** | A DAG of steps (SQL, tables, checks, notebooks, Python, Spark, dbt, agents, Airflow) run as its owner |
| **Fleet** | The set of customer installations a vendor operates, seen in one console |
| **✅ / 🟡 / ⬜** | Shipped and tested; built and partly validated; planned |
| **Fail closed** | If a decision cannot be made, access is refused |
| **Ring** | One of four areas around Cairn Core that a capability belongs to |
| **Legacy row** | A row of the roadmap's M0 to M6 milestone tables, cited as "legacy M2 row 2.11" |

---

## Appendix A. Corrections and inconsistencies

This register lists what changed from v0.2 and what was found in the sources. Items in the last column are for the owners of the source files. This document does not edit anything in `cairn/`.

| # | Where | Finding | Action in v0.3 | Fix needed in |
|---|---|---|---|---|
| A-1 | v0.2 BR-AI-02 | Cited L93 for AI documentation drafts. L93 is semantic-model autopilot | Cites legacy row 4.4 [✅] | None (BRD only) |
| A-2 | v0.2 BR-AI-18 | Cited "ROADMAP 4.8". That is a legacy row, not an L-item | Cites L171 [⬜] and legacy row 4.8 [⬜] | None |
| A-3 | v0.2 BR-GOV-11, NFR-12 | Cited "ROADMAP 2.11" and "roadmap 5.6" without saying they are legacy rows | Cited as legacy rows with their status | None |
| A-4 | v0.2 BR-ID-02 | Cited L44 (remote agent hardening) for pipeline token exchange | Cites the principle; L44 is for BR-ING-11 | None |
| A-5 | v0.2 BR-LH-11 | Cited L59 (snapshot browser) for governed volumes | Removed | None |
| A-6 | v0.2 BR-GOV-05, BR-MD-02, BR-MD-06, BR-MD-07, BR-AI-01, BR-AI-03 | Cited "Module E" with milestones U1 to U5 as [✅]. `cairn/ARCHITECTURE.md` defines Modules A to D only. `cairn/README.md` mentions a "Module E API contract" without defining it. In `ROADMAP.md` M6, U1 to U5 read "✅ code and tests; ⬜ live checks" | Cites M6 rows and says what is open | `cairn/ARCHITECTURE.md` and `cairn/README.md` (define or rename "Module E"); `cairn/ROADMAP.md` (M6 markers) |
| A-7 | v0.2 NFR-14 | Marked accessibility as 🟡. L67 is ✅ (automated axe checks in CI) | Marked ✅ for automated checks, with the independent audit as an open item | None |
| A-8 | v0.2 §3, Helm | Helm HA marked 🟡 in BR-OPS-04 and NFR-06, but the platform table marks "Operations" ✅ and cites "HA" | Kept 🟡 | `cairn/ROADMAP.md`: align the platform table |
| A-9 | `cairn/ROADMAP.md` L35 | Marked ✅, but the text says the live-model suite is "not yet run" | Kept ✅; flagged in UC-8, BR-BI-05 and R-11 | `cairn/ROADMAP.md` |
| A-10 | `cairn/ROADMAP.md` L94, L95 | Marked ✅, with the live-model checks "not yet run" | Kept ✅; flagged in BR-AI-09, BR-AI-10 and R-11 | `cairn/ROADMAP.md` |
| A-11 | `cairn/ROADMAP.md` L19 | Marked ✅. The same row says converting partitioned tables and deletion vectors in place is "to do", then says they convert too | Kept ✅; the row contradicts itself | `cairn/ROADMAP.md` |
| A-12 | `cairn/ROADMAP.md` legacy row 5.5 | Marked ✅ for the threat model. The external pentest in the same cell is marked ⬜, and the roadmap's other items also treat the pentest as open | Row kept at ✅; the pentest is tracked in BR-AUD-05 and NFR-05 | `cairn/ROADMAP.md`: split the row |
| A-13 | `cairn/doc/BRD-appendix-roadmap.md` | Current-state text is truncated with an ellipsis in every row. Markers and totals agree with the roadmap (185: 124, 43, 18) | Used for markers only; this document cites `ROADMAP.md` for evidence | `cairn/doc/` (regenerate untruncated, or drop) |
| A-14 | `AGENTS.md` | Says the queue is 65 items, "the original 28, the BI track, the gaps found after it, and the capability audit's L47-L67". In `ROADMAP.md`, L47 to L67 are other items (SAML, retention, BI sharing, consent, accessibility), and the capability audit is L168 to L185 | Not resolved here; R-09 and Q-06 | `AGENTS.md` |
| A-15 | `AGENTS.md` | Names a working branch whose name contains "datahub-enterprise-extension". The checkout used for this review is on a different branch | Not resolved here | `AGENTS.md` or the working branch |
| A-16 | v0.2 §8, Java 25 | Stated as fact. The build shows Java 17 bytecode and a default Java 21 toolchain, with Java 25 needed for newer DataHub class files | Open item (§3.2, Q-11) | Confirm the deployed image; state it in the platform guide |
| A-17 | v0.2 BR-ID-02, default token lifetime | 4-hour default in the roadmap is not confirmed in code. The 12-hour ceiling is confirmed | Kept as roadmap claim | Confirm the default in code |

---

## Appendix B. Sources

All paths are relative to the repository root `/home/user/sca`.

| Source | Used for |
|---|---|
| `cairn/ROADMAP.md` | Items L1 to L185 and their markers; principles; the platform table; the legacy M0 to M6 rows; the current release |
| `cairn/doc/BRD-appendix-roadmap.md` | Item titles and marker totals (checked against `ROADMAP.md`: all 185 markers agree) |
| `cairn/doc/BRD.md` (v0.2) | Predecessor. Reused where correct; see Appendix A |
| `cairn/ARCHITECTURE.md` | The four modules A to D; the ring model; the Core contracts |
| `cairn/UX_ACTIVE_METADATA_SPEC.md` | Milestones U1 to U7 and the search latency target (Part 10) |
| `cairn/README.md` | Product statements |
| `AGENTS.md` | Repository conventions; CI job list; tenant model; the 65-item queue statement |
| `cairn/deploy/versions/pins.json` | Pinned engine versions |
| `cairn/core/UPSTREAM_VERSION`, `cairn/core/UPSTREAM.md` | DataHub v1.8.0 as the Core base |
| `cairn/plugins/java/cairn-authorizer/build.gradle` | Java toolchain |
| `cairn/src/cairn/migrations/versions/` | 157 migrations, latest 0157 |
| `cairn/helm/cairn/Chart.yaml`, `templates/`, `values*.yaml` | Chart version; profiles; PDB and NetworkPolicy templates |
| `cairn/deploy/terraform/modules/` | Terraform modules |
| `.github/workflows/cairn-ci.yml` | 17 CI jobs |
| `cairn/tests/test_scale_decisions.py` | Asserts the NFR-P1 target |
| `cairn/src/cairn/identity/notebooks.py`, `identity/workload.py` | 12-hour token ceiling |
