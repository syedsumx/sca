# Cairn — Business Requirements Document (BRD)

| Field | Value |
|---|---|
| Product | Cairn: self-hosted (and optionally managed) data platform for governed lakehouse, analytics, AI and automation, built on Cairn Core |
| Document | Business Requirements Document, **v0.2** (draft for review) |
| Date | 2026-11-14 |
| Owner | Product owner, Cairn |
| Approvers | Head of Engineering, Head of Security, Head of Data Platform (to be named) |
| Status | Draft. Status of each capability is the roadmap's own marker (see §4.3 and the appendix) |
| Companion | `BRD-appendix-roadmap.md`: all 185 roadmap items with their status, generated from `ROADMAP.md` |
| Source of truth | `cairn/ROADMAP.md`, `cairn/ARCHITECTURE.md`, `cairn/UX_ACTIVE_METADATA_SPEC.md`, `cairn/README.md`, `AGENTS.md` |

**Change log**

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-11-14 | First draft, built from the README and the early roadmap. Covered about 60 requirements in 4 areas |
| v0.2 | 2026-11-14 | Rebuilt from the full roadmap (185 items, 13 areas). Corrected: Cairn Core is built on an open-source engine with Cairn's patches, not an unmodified image; managed multi-tenant hosting is in scope as an option, not excluded |

---

## 1. Purpose

This document defines what Cairn must do for the organisations that buy and run it, why it matters, and how we will know it has worked. It is the reference for product, engineering, security and go-to-market decisions. Architecture lives in `ARCHITECTURE.md`. Sequencing and status live in `ROADMAP.md`. Each business requirement here points to the roadmap items that define it.

## 2. Background and problem

Data teams run separate products to store, query, transform, orchestrate, catalogue, govern, visualise and monitor their data, and increasingly to build AI agents on top of it. Each product has its own identity, its own permissions and its own audit trail. The result:

1. **Access is inconsistent.** A person can be allowed in the warehouse and refused in the catalogue, or the reverse. Pipelines hold database passwords with broad rights.
2. **Governance is after the fact.** Masks and row filters are applied in one tool and missed in the others that read the same data.
3. **Lineage and ownership are partial.** No one can answer "what breaks if I change this table?" across the whole estate.
4. **Cost and lock-in are high.** Teams pay for a catalogue, a warehouse, an orchestrator, a BI tool, a notebook service, an ETL tool and a governance suite, and their data and metadata sit inside each vendor.
5. **Sensitive networks block SaaS.** Regulated customers need the data plane, and sometimes the control plane, inside their own network, including air-gapped sites.

Cairn gives one identity, one policy and one metadata graph across open-source engines, deployed in the customer's network or as a managed service.

## 3. Vision and goals

**Vision.** One governed data platform in which every person, pipeline, notebook, dashboard and AI agent signs in once, is decided by one policy, and leaves one audit trail.

| Goal | Business outcome | Measure |
|---|---|---|
| G1. One identity and one policy | Access is decided the same way in every engine, app and AI tool | No engine, app or agent holds a database password or store key in its configuration (verified by test) |
| G2. Governance at the point of read | Masks, row filters and purposes apply wherever data is read | Restricted tables are refused to any engine that cannot enforce policy (fail closed) |
| G3. One metadata graph | Lineage, ownership, quality and usage are visible across engines and tools | Column-level lineage from Trino, Spark, dbt, Airflow, notebooks and CDC into Cairn Core |
| G4. Run where the data is | Self-hosted, air-gapped or managed, with the control plane and data plane split where needed | Compose and Helm deployments; outbound-only remote agents; signed release bundles for air-gapped sites |
| G5. Replace the stack over time | Teams retire separate catalogue, warehouse, ETL, BI and orchestration vendors | Migration tooling and connectors in §6.4; migration items are still planned (see §4.3) |
| G6. Self-service with guardrails | Analysts answer questions and build dashboards without tickets, inside the policy | Access requests and dashboards work through approvals, not tickets |
| G7. Trust for AI | AI answers, drafts and agent actions run as the caller and are reviewed | Every AI call carries the caller's permissions; drafts need a human approval |
| G8. Operate it as a product | Sell, bill, update and support many installations | Self-service signup and billing, usage metering, a status page, a fleet console and signed releases exist (see §6.10) |

## 4. Scope

### 4.1 Product areas

The roadmap groups capabilities into four rings around Cairn Core (`ROADMAP.md`, "Cairn Core at the centre"). Every capability states which ring it is in and what it reads from and writes back to the graph. The thirteen areas below are an editorial grouping of the 185 numbered roadmap items for this BRD. The appendix is the authoritative item-level mapping.

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

- Replacing the source systems' transactional applications. Cairn reads them through mirrors, connectors and agents.
- Building a general-purpose consumer BI product. Cairn's BI is for governed analytics inside the platform, and its trust components embed in other BI tools (§6.6).
- A public marketplace open to any organisation. A cross-organisation exchange is planned (L184) and is not committed here.
- Hosting one customer's data inside another customer's tenant.

### 4.3 Status definitions

The roadmap's markers are the status used throughout this document:

- **✅ Shipped:** built and tested end to end in CI, against the real binaries and images.
- **🟡 Partly validated:** built and shipped, but some checks have not run live (for example, on a real cluster, with a live model or against a real Slack or Teams endpoint). The roadmap usually says which checks are outstanding.
- **⬜ Planned:** not built. Several of these come from the November 2026 capability audit (L168–L185) and are not yet committed to a release.

Only ✅ items count as shipped in §10 and in the success measures in §12.

## 5. Stakeholders and users

| Persona | Who | What they need from Cairn | Areas |
|---|---|---|---|
| **Platform admin** | Runs the platform team | Install, upgrade, manage tenants and pools, monitor health, manage agent and Airflow deployments | K, L, M |
| **Data engineer** | Builds pipelines | Run SQL, dbt, Spark, Airflow and Flows as a pipeline identity; mirror databases; see lineage and failures | A, B, C, D |
| **Data steward** | Owns domains and quality | Glossary, certification, access reviews, retention, reviewing AI drafts and ontology proposals | H, I, J |
| **Analyst / business user** | Finds and uses data | Discover assets, ask questions, build dashboards and workbooks, request access without a ticket | F, G, H |
| **Data scientist / ML engineer** | Builds models | Notebooks, MLflow, training on governed data, serving, fine-tuning | E, B |
| **Security and compliance officer** | Accountable for risk | Prove who accessed what; enforce masks, purposes and consent; export audit to SIEM; check the security posture | J, K |
| **Application developer** | Builds data apps and agents | Data apps, agent code with sandboxes, the AI gateway, MCP tools, the CLI, SDKs | B, G, M |
| **Vendor operations (for managed and fleet installs)** | Runs many customer installs | Signed releases, update channels, fleet visibility, status and usage reporting | L |
| **Automation and AI agents** | Non-human callers | Act through MCP and APIs under a named identity and the same policy; propose infrastructure changes | G, K, M |
| **Business owner / executive** | Pays for and judges the platform | Cost and chargeback, risk reduction, the path off legacy vendors | L, A, B |

## 6. Business requirements

Priority uses MoSCoW: **M** = must have for the target release, **S** = should, **C** = could. Each requirement cites the roadmap items that define it, with their status in brackets. The full item list is in `BRD-appendix-roadmap.md`.

### 6.1 Identity, access and security

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-ID-01 | People sign in once, through OIDC, SAML or SSO, with users and groups provisioned by SCIM | M | §1.1 Architecture; SAML L47 [✅] |
| BR-ID-02 | Pipelines, notebooks and agents act as named pipeline users with personal access tokens, exchanged for short-lived, audience-bound engine tokens (default 4 hours, maximum 12) | M | Principles; L44 [✅] |
| BR-ID-03 | No engine, project file, notebook or agent stores a database password or store key in plain text; secrets are scoped to owners or groups | M | L48 [✅]; secret confinement tests |
| BR-ID-04 | Strong sign-in for sensitive actions: passkeys, and a fresh passkey check (step-up) before the most sensitive changes | M | L83 [✅] |
| BR-ID-05 | Customer-managed keys in AWS KMS, Azure Key Vault or Google Cloud KMS, as well as Vault | M | L22 [✅] |
| BR-ID-06 | Per-tenant and per-user API quotas, and tenant IP allowlists | S | L51 [✅] |
| BR-ID-07 | The platform checks its own configuration for risky settings and reports them | S | L82 [✅] |
| BR-ID-08 | Agent runs declare a purpose and the kinds of data they need, and are checked against it; prompt injection and exfiltration are detected | M | L109 [✅], L114 [✅] |

### 6.2 Policy, governance and privacy

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-GOV-01 | One policy decision point decides access for every engine, app, metadata API and AI tool | M | Principles; ARCH §1.3 |
| BR-GOV-02 | Deny overrides allow; a decision that cannot be made is a refusal | M | README (fail closed) |
| BR-GOV-03 | Masks, row filters and aggregation-only or minimum-group policies apply on every read path; an engine that cannot enforce them is refused restricted tables | M | L2 [✅], L85 [✅] |
| BR-GOV-04 | PII is classified and the classification propagates over column lineage | M | Module B |
| BR-GOV-05 | Personas and purposes grant access by role and purpose; a purpose deny overrides a persona grant; consent is required where a purpose needs it | M | Module E (U3); L66 [✅] |
| BR-GOV-06 | Access is approved with separation of duties; grants can be time-boxed and revoked; periodic access reviews recertify who holds what | M | Module B; Module E (U2); L71 [✅] |
| BR-GOV-07 | Retention is enforced, with legal hold and automated erasure | M | L50 [✅] |
| BR-GOV-08 | Sensitive values can be tokenised, with token domains holding their own keys | S | L74 [✅] |
| BR-GOV-09 | Geospatial data is governed; location fields can be masked to coarse areas | S | L79 [✅] |
| BR-GOV-10 | Data residency: lakehouse namespaces pinned to a region | M | L73 [✅] |
| BR-GOV-11 | Policy changes are validated in CI before they reach a deployment | S | ROADMAP 2.11 [⬜] |

### 6.3 Lakehouse storage and tables (Area A)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-LH-01 | Object storage with replication, erasure coding, versioning, object lock, lifecycle and tiering | M | Platform table, "Object storage" [✅] |
| BR-LH-02 | Iceberg tables with per-table credential vending and governed access; Delta Lake tables readable and writable without conflict | M | L19 [✅], L86 [✅] (Iceberg v3: deletion vectors, row lineage, VARIANT) |
| BR-LH-03 | Table maintenance: snapshot expiry, orphan removal, compaction, on a schedule | M | L1 [✅] |
| BR-LH-04 | Zero-copy clones, undrop, and table branches and tags, with write-audit | M | L12 [✅], L75 [✅] |
| BR-LH-05 | Registering Parquet files already in the store as tables without copying them | S | L115 [✅] |
| BR-LH-06 | Tables placed on the customer's own cloud storage, and external Iceberg catalogues governed alongside Cairn's | S | L132 [🟡], L105 [✅] |
| BR-LH-07 | Storage credentials and external locations are governed objects | M | L133 [✅] |
| BR-LH-08 | Snowflake and Databricks tables can be read in place through governed catalogues | S | L151 [🟡] |
| BR-LH-09 | Change data feeds for consumers that sync; materialized views refreshed when their inputs change | S | L25 [✅], L24 [✅] |
| BR-LH-10 | Serving tables for interactive analytics and hybrid transactional use | S | L103 [✅] |
| BR-LH-11 | Governed volumes (folders of files) under Cairn's policies | S | L58 [✅]; L59 snapshot browser [✅] |
| BR-LH-12 | Vector indexes over lakehouse text, with search by meaning that respects policy | S | L16 [✅] |
| BR-LH-13 | Bad rows are quarantined rather than failing a whole load | M | L72 [✅] |
| BR-LH-14 | Synthetic test data shaped like real tables, with no real people's data | S | L76 [✅] |
| BR-LH-15 | Point-lookup acceleration with bloom filters on the columns queries look rows up by | C | L81 [✅] |
| BR-LH-16 | Stateful streaming SQL with a declared freshness target | C | L176 [⬜] |

### 6.4 Ingestion, mirrors and connectivity (Areas C and D)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-ING-01 | Streams from Kafka and Debezium CDC into Iceberg, exactly once | M | L5 [✅]; Platform table |
| BR-ING-02 | HTTP streaming ingestion: producers post rows without Kafka | S | L13 [✅] |
| BR-ING-03 | Files as data: directory tables and text extraction; speech-to-text and AI description and field extraction for files | S | L15 [✅], L77 [✅], L78 [✅] |
| BR-ING-04 | PostgreSQL tables mirrored into the lakehouse through logical replication, with whole-schema selection | M | L89 [✅], L166 [🟡] |
| BR-ING-05 | Mirrors from MySQL, SQL Server and MongoDB change streams | S | L92 [✅], L96 [✅], L97 [✅] |
| BR-ING-06 | Log-based mirrors from Oracle, then Db2 and SAP HANA | S | L167 [🟡] |
| BR-ING-07 | SaaS sources (Salesforce, HubSpot and similar) loaded into Iceberg, with reverse ETL to SaaS apps | S | L61 [🟡], L62 [🟡] |
| BR-ING-08 | A declarative connector builder and runner for REST sources | S | L165 [🟡] |
| BR-ING-09 | Document sources (SharePoint, Google Drive) kept in governed storage | C | L98 [🟡] |
| BR-ING-10 | Apache NiFi 2 deployments, managed per team and environment, with governed connectors | C | L152 [🟡] |
| BR-ING-11 | Remote agents inside the customer's network pull data from private sources, with outbound-only connections and no inbound ports | M | L44 [✅]; Module C |
| BR-ING-12 | Connector coverage: each catalogue connector has a live ingestion test; ETL and SaaS sources validated on real accounts | M | L168 [⬜], L182 [⬜] |
| BR-ING-13 | Reaching data in other networks through SSH tunnels and private endpoints | C | L178 [⬜] |

### 6.5 Query engines, SQL and workloads (Area B)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-SQL-01 | SQL on the lakehouse with Trino, with per-team warehouses that stop when idle and start on the first query | M | Platform table, "SQL warehouse" [✅]; L87 [✅] |
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
| BR-SQL-12 | Data apps (Streamlit and other web apps) served by Cairn, each viewer signed in as themselves; app packages installed by other teams on their own data | S | L23 [✅], L90 [✅], L120 [✅] |
| BR-SQL-13 | System tables exposing Cairn's operational data as SQL | S | L134 [🟡] |
| BR-SQL-14 | Transactions across several lakehouse tables, richer table and aggregate functions, and governed HTTP calls from SQL | C | L180 [⬜] |

### 6.6 Transformation, orchestration and operational databases (Area C)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-TR-01 | dbt runs as a pipeline user; models, tests, owners and lineage are recorded in Cairn | M | Platform table, "Transformation" [✅] |
| BR-TR-02 | Derived tables refresh on a schedule, by watermark or by merge, and when what they read changes | M | Platform table [✅] |
| BR-TR-03 | Slowly changing dimensions (type 2) and medallion layers | S | L110 [✅] |
| BR-TR-04 | Cairn Flows: one DAG mixing SQL, tables, checks, notebooks, Python, Spark, dbt, agents, Airflow DAGs, approvals and branches, run as its owner | M | L122 [✅] |
| BR-TR-05 | A visual flow canvas, with YAML in git and from agents | M | L123 [✅] |
| BR-TR-06 | Airflow both ways: a flow exports as an Airflow DAG, and Airflow can run a flow | S | L124 [🟡] |
| BR-TR-07 | Managed Airflow 3.3 deployments per team and environment, with promotion, rollback and hibernation | S | L144 [🟡] |
| BR-TR-08 | Pipeline observability for Airflow, and OpenTelemetry export across the platform | S | L145 [🟡], L141 [🟡] |
| BR-TR-09 | Flows that compose: sub-flows, task groups, mapping over sub-graphs, sensors, pools and priorities | S | L173 [⬜] |
| BR-TR-10 | Orchestration steps on other platforms, from Flows and from Airflow | S | L138 [🟡] |
| BR-TR-11 | dbt projects edited and built in the product, with environments and job pages | S | L172 [⬜] |
| BR-TR-12 | Operational PostgreSQL databases created, branched and managed per team (like a managed cloud database) | S | L88 [✅], L140 [🟡] |

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
| BR-BI-05 | An AI builder that turns a question into a workbook or dashboard | S | L35 [✅] |
| BR-BI-06 | Forms and buttons on dashboards that write to input tables or call flows | S | L36 [✅] |
| BR-BI-07 | Scheduled exports as PDF or CSV, delivered by email, Slack and Teams; a result cache shared only between permitted viewers | S | L37 [✅], L55 [✅] |
| BR-BI-08 | Metric alerts with thresholds and anomaly detection | M | L53 [✅] |
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
| BR-AI-01 | The assistant acts as the caller and sees only what the caller may see; the same governed tools serve the UI, the CLI and MCP | M | Module D; L154 [🟡] |
| BR-AI-02 | AI-written documentation, definitions and ontology proposals are drafts until a human approves them | M | Module D; L93 [✅] |
| BR-AI-03 | Model provider is configurable (Anthropic, OpenAI-compatible, cloud model services, or local models); the platform works when no model is reachable | M | Module E (U5); L99 [✅], L113 [✅] |
| BR-AI-04 | An AI gateway with virtual model names and routing rules, and an MCP tool registry for external servers | S | L113 [✅], L155 [🟡] |
| BR-AI-05 | The whole platform API is exposed as MCP tools | S | L154 [🟡] |
| BR-AI-06 | Agents built in code (`cairn agents init`, `dev`, `deploy`) and from the assistant's tools | S | L100 [✅], L64 [🟡] |
| BR-AI-07 | Agent sandboxes for Python and SQL, so agent code runs isolated | M | L116 [✅] |
| BR-AI-08 | Governed memory for the assistant and for agents, saved only when asked | S | L84 [✅], L111 [✅] |
| BR-AI-09 | Scheduled assistant tasks with an approved outbox; automations from the CLI | S | L95 [✅], L108 [✅] |
| BR-AI-10 | A pipeline agent that writes, tries, fixes and ships pipelines from a goal | S | L94 [✅] |
| BR-AI-11 | Trace intelligence: what each agent's tool calls and model use say, and where they fail | S | L101 [✅] |
| BR-AI-12 | Grounded answers with sources from vector indexes | S | L102 [✅] |
| BR-AI-13 | AI search and document parsing callable from SQL and pipelines | S | L112 [✅] |
| BR-AI-14 | Data packages shared with their semantic context, so outside agents work on shared data safely | C | L117 [✅] |
| BR-AI-15 | The Cairn agent in GitHub Copilot and other AI coding tools, and the agent kit for project work | S | L156 [🟡], L164 [✅] |
| BR-AI-16 | Agent marketplace: agent apps, custom agents and extensions listed in the marketplace | C | L107 [✅] |
| BR-AI-17 | Usage measured, with budgets; AI setup as code | S | Platform table, "AI" [✅]; L158 [🟡] |
| BR-AI-18 | Golden-question evaluation of answers before each release | S | ROADMAP 4.8 [⬜] |

### 6.10 Discovery, metadata and ontology (Area H)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-MD-01 | A typed metadata graph of assets, domains, glossary terms, data products and contracts (Cairn Core) | M | Cairn Core; Platform table [✅] |
| BR-MD-02 | Search by keyword and by meaning, with facets the person is allowed to see | M | Cairn Core; Module E (U1) [✅] |
| BR-MD-03 | Column-level lineage from every engine, dbt, Airflow, Spark, notebooks and CDC | M | Platform table [✅] |
| BR-MD-04 | Glossary and domain editing | M | L148 [✅] |
| BR-MD-05 | Ontology compiled from the catalogue and query log, with an explorer and a review queue | S | L121 [✅], L129 [✅] |
| BR-MD-06 | Context agents propose glossary terms and relationships that a steward accepts | M | Module E (U4) [✅] |
| BR-MD-07 | Bookmarks, follows, @mentions, threads, reactions and trending | S | Module E (U1) [✅] |
| BR-MD-08 | Catalogue daily-use features in Cairn's own UI: custom metadata, badges, announcements, popularity | S | L174 [⬜] |
| BR-MD-09 | Curated question spaces with instructions and verified question-and-query pairs | S | L177 [⬜] |
| BR-MD-10 | Exchange of listings between organisations, with pricing and provider analytics | C | L184 [⬜] |

### 6.11 Data quality and reliability (Area I)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-DQ-01 | Monitors for freshness, volume, schema, field metrics and drift, with learned baselines and partition-aware volume checks | M | Module A [✅]; L43 [✅] |
| BR-DQ-02 | Failed checks raise incidents that resolve automatically when the data recovers | M | Module A [✅] |
| BR-DQ-03 | Data contracts are defined and checked | M | Platform table, "Data quality" [✅] |
| BR-DQ-04 | Quality and cost checks at the source (Snowflake and Databricks) | C | L181 [⬜] |

### 6.12 Governance workflows and audit

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-AUD-01 | Every change is one transaction with its audit entry | M | Principles (fail closed) |
| BR-AUD-02 | The audit log is hash-chained and tamper-evident | M | Platform table, "Governance" [✅] |
| BR-AUD-03 | Audit is pushed to a SIEM: Splunk HEC, syslog (RFC 5424 and CEF), at-least-once and ordered | M | L26 [✅] |
| BR-AUD-04 | Compliance forms and evidence can be exported for review | S | Platform table [✅] |
| BR-AUD-05 | Independent external penetration test and audited controls (SOC 2 or ISO 27001) before general availability | M | L185 [⬜] |

### 6.13 Platform operations, commercial and fleet (Area L)

| ID | Requirement | Priority | Roadmap |
|---|---|---|---|
| BR-OPS-01 | Self-service signup, subscription through Stripe Checkout, and tenant licences | M | L27 [✅] |
| BR-OPS-02 | Tenants report daily usage; the control plane bills it | M | L28 [✅] |
| BR-OPS-03 | A public status page with components, 90-day uptime and incidents | S | L29 [✅] |
| BR-OPS-04 | High availability: replicated tenant search and messaging; failover tested | M | L68 [✅]; Helm HA (platform table) [🟡] |
| BR-OPS-05 | Disaster recovery across regions: store replication, catalogue and database recovery | M | L9 [✅] |
| BR-OPS-06 | Load and resilience tests: policy-decision latency, and outage tests for Cairn Core, the PDP, the gateway and the catalogue | M | L10 [🟡], L46 [✅] |
| BR-OPS-07 | Upgrades are pinned and tested against each new engine and Cairn Core release | M | L11 [✅]; Upgrade matrix (weekly) |
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
| BR-DX-07 | Clients for Python, JavaScript and Java, with a deprecation policy for the API | S | L65 [✅]; L175 [⬜] (Python client for the whole API) |
| BR-DX-08 | A design system shared by the web UI and the workspace | S | L146 [🟡] |
| BR-DX-09 | One home per concept in the product (information architecture), and a calmer, consistent workspace | S | L147 [🟡], L149 [🟡], L150 [🟡] |
| BR-DX-10 | One administrator section for people, roles and access | M | L139 [✅] |
| BR-DX-11 | Accessibility: the main pages are checked with automated accessibility tests in CI | M | L67 [✅] |
| BR-DX-12 | Profiles per person that the assistant and schedules use | C | L119 [🟡] |
| BR-DX-13 | Packages of data apps published by one team and installed by others | S | L90 [✅] |

## 7. Non-functional requirements

| ID | Category | Requirement | Target | Status |
|---|---|---|---|---|
| NFR-01 | Security | Access decisions fail closed | Verified by tests | ✅ |
| NFR-02 | Security | Secrets never leave the customer network in plain text; secret-confinement tests | Verified | ✅ |
| NFR-03 | Security | Remote agents hold no Cairn Core credentials and accept no inline secrets | Verified | ✅ |
| NFR-04 | Security | Threat model with a negative test for each attack (`docs/site/agent-security.md`) | Maintained | ✅ |
| NFR-05 | Security | External penetration test of the gateway and policy engine | Before general availability | ⬜ (BR-AUD-05) |
| NFR-06 | Availability | Control plane highly available under Helm (replicas, pod disruption budgets, network policies) | Availability target to be agreed (proposed 99.9% monthly) | 🟡 |
| NFR-07 | Performance | Policy decisions at low latency under load | Cached decisions measured in CI; the roadmap's target is p99 under 5 ms at 2,000 requests per second | 🟡 (L10) |
| NFR-08 | Performance | Interactive search and asset pages respond quickly on a realistically sized catalogue | Target to be set from a load test | ⬜ |
| NFR-09 | Scalability | Engines scale independently; Trino workers autoscale; warehouses start on demand | Verified in platform configuration | ✅ |
| NFR-10 | Operability | Monitoring dashboards, health checks, structured logs, traces | Present; see BR-OPS-09 | 🟡 |
| NFR-11 | Portability | Runs in Docker Compose for evaluation, in Kubernetes with Helm, and on Terraform modules for AWS, GCP, Azure and Hetzner | Compose verified; Helm on live clusters pending | 🟡 |
| NFR-12 | Upgrades | Pin-and-test against each release; upgrade playbook | Playbook in progress | ✅ (L11); playbook ⬜ (roadmap 5.6) |
| NFR-13 | Licensing | Third-party licences are listed and ship with every distribution | `THIRD_PARTY_NOTICES.md` | ✅ |
| NFR-14 | Accessibility | Web UI meets WCAG 2.1 AA (automated checks in CI; full audit pending) | Target | 🟡 (L67) |
| NFR-15 | Internationalisation | Interfaces translatable; documentation in English, German, Spanish, French and Japanese | Present | ✅ |
| NFR-16 | Supply chain | Images signed with SBOMs; non-root; minimal OS packages | Verified | ✅ |
| NFR-17 | Air-gap | Install and update without outbound access | Bundles (BR-OPS-12) | 🟡 |

## 8. Assumptions, constraints and dependencies

**Assumptions**

- Customers run their own network, and can host agents inside it.
- Customers accept open-source engines (Trino, Spark, Airflow, dbt, Iceberg, PostgreSQL, Kafka, JupyterHub, MLflow, NiFi) as the foundation.
- Cairn Core is built on an open-source engine. Cairn maintains patches to it and adds Java plugins and a control plane around it (`AGENTS.md`).

**Constraints**

- Cairn Core runs on Java 25; the authorizer plugin is built for Java 17.
- Engine versions are pinned and tested as a set (Trino 484, Spark 4.0 with Iceberg 1.10, Airflow 3.3, dbt-core 1.12, Iceberg v3 features on the catalogue's iceberg-rust 0.11).
- Docker Hub rate limits and TLS-intercepting proxies affect image builds in some networks; build scripts support mirrors and extra CA files.
- Database schema changes go through numbered migrations (latest 0157 at this version).

**Dependencies**

- Cairn Core and its search and messaging stack (OpenSearch or Elasticsearch, Kafka, MySQL or PostgreSQL)
- Customer identity provider for SSO and SCIM
- Optional: a model provider (Anthropic, OpenAI-compatible, a hyperscaler model service, or a local model); Slack or Teams; Vault, AWS Secrets Manager, KMS or Key Vault; Stripe for billing

## 9. Risks and issues

| ID | Risk or issue | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R-01 | 🟡 items (43 at this version) are not yet validated live: Helm on a cluster, real Slack and Teams, live model calls, BI-embedded components, fleet and air-gap flows | High | High | Live validation list before each release (§10, stage 1); treat 🟡 as not generally available | Engineering |
| R-02 | Cairn Core or engine upgrades break shapes or fields | Medium | High | Pin-and-test matrix (weekly); upgrade playbook (roadmap 5.6) | Engineering |
| R-03 | Security findings in the gateway, policy engine or agent sandboxes before the external test | Medium | Critical | External test before general availability (BR-AUD-05); threat model maintained; sandbox negative tests | Security |
| R-04 | Policy misconfiguration grants access by mistake | Medium | High | Policy validation (BR-GOV-11, planned); deny-overrides; purposes at higher priority; access reviews | Security / Product |
| R-05 | Scope breadth: 185 roadmap items, 18 of them planned, and a small team | High | High | Prioritise by this BRD; cut "could" items first; area owners | Product |
| R-06 | Competition from catalogue, warehouse and BI vendors with larger teams | High | Medium | Self-hosted, one-policy story; migration tooling (planned); open engines | Product |
| R-07 | Air-gapped or restricted networks cannot run the design as built | Medium | High | Outbound-only agents; bundles (BR-OPS-12); documented network requirements | Engineering |
| R-08 | Model output is wrong, or a provider changes terms or prices | Medium | Medium | Evaluation (BR-AI-18); AI output is drafts; offline fallback; provider configurable | Product |
| R-09 | Two backlogs disagree: `AGENTS.md` describes a 65-item queue, while the roadmap numbers 185 items | High | Medium | Decision Q-06 | Product / Engineering |
| R-10 | Commercial operations (billing, signup, status, fleet) run live for the first time at scale | Medium | High | Stage 3 gate in §10; usage reconciliation test | Operations |

## 10. Release approach

Releases are gated on the roadmap's markers, not on dates. A capability counts as released when it is ✅ shipped.

| Stage | Entry | Exit |
|---|---|---|
| **Stage 1: Live validation** | Current code base | Each 🟡 item that a release depends on has its live checks run, with recorded results (cluster, Slack and Teams endpoints, a live model, BI embeds, fleet and air-gap flows) |
| **Stage 2: Assurance** | Stage 1 exit | External penetration test passed (BR-AUD-05); SOC 2 or ISO 27001 plan agreed; key management reviewed (BR-ID-05) |
| **Stage 3: General availability** | Stage 2 exit | Upgrade playbook tested against one engine and one Cairn Core minor release; billing and status reconciled for one full cycle; runbooks and support in place |
| **Stage 4: Growth** | GA | Planned items (⬜) prioritised by customer demand and by §6 |

## 11. Acceptance criteria

A business requirement is accepted when all of the following hold:

1. Its behaviour has an automated test in CI, and an end-to-end test against real components where it crosses services (the roadmap's standard).
2. It is assigned to one of the four rings in `ROADMAP.md`, and states what it reads from and writes back to Cairn Core.
3. Access decisions fail closed, and audit entries are written in the same transaction as the change.
4. Its operator and user documentation is updated in `docs/site/`, and its roadmap status is updated.
5. The product owner has checked the behaviour against §5.

## 12. Success measures (first 12 months after general availability)

- 3 paying design partners converted to paid plans
- 40 paying organisations and 2,000 active developer seats (planning figures, to be confirmed with the business owner)
- Net revenue retention above 110% for the pilot cohort
- Weekly scanning or querying on at least 60% of connected projects
- Every 🟡 item used in a release has a recorded live validation

## 13. Open questions

| # | Question | Why it matters | Decision needed from |
|---|---|---|---|
| Q-01 | Which offer comes first: self-hosted only, managed (signup and billing are built), or both? | Changes support, pricing, GA checklist and the hosting commitments | Leadership |
| Q-02 | Which compliance standard comes first, SOC 2 or ISO 27001? | Sets the audit timeline and budget (BR-AUD-05) | Security |
| Q-03 | What are the performance targets for search, asset pages and the largest supported repository? | NFR-08 cannot be tested without them | Engineering |
| Q-04 | Which model provider is the default for paying customers, and is local-only operation required? | Affects BR-AI-03 and data-residency promises | Product / Security |
| Q-05 | Which migration source comes first, Snowflake or Databricks? | Sets the order of L170 and the connector work in BR-ING-12 | Product |
| Q-06 | Which list is the working backlog: the roadmap (185 items) or the 65-item queue in `AGENTS.md`? | Two lists risk two plans (R-09) | Product / Engineering |
| Q-07 | Who owns Stage 1 live validation and its sign-off? | Nothing in §10 passes without it | Engineering |

## 14. Glossary

| Term | Meaning |
|---|---|
| **Cairn Core** | The metadata system of record: typed graph, search, lineage, catalogue UI and APIs. Built on an open-source engine, with Cairn's patches |
| **Control plane** | The Cairn services on top of Core: quality, governance, agents, AI, MCP, actions, billing |
| **Data plane** | Where data is read and processed; can run apart from the control plane |
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
