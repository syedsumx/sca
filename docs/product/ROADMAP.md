# Cairn: Three-Phase Product Roadmap

| Field | Value |
|---|---|
| Product | Cairn: a governed data platform built on Cairn Core |
| Document | Product roadmap, **v0.1** (draft for review) |
| Date | 2026-11-14 |
| Prepared for | Virtual data room (VDR) review |
| Owner | [TO CONFIRM: named product owner] |
| Approvers | [TO CONFIRM: phase sign-off by Head of Engineering, Head of Security and Head of Data Platform, by name] |
| Companion | [`BRD.md`](BRD.md) in this folder: requirements, targets, risks and open questions |
| Source of truth | [`cairn/ROADMAP.md`](../../cairn/ROADMAP.md): every item, its status marker and its evidence. Markers were read on 2026-11-14 |

**Status markers** (the roadmap's own): **✅** shipped and tested end to end in CI; **🟡** shipped, partly validated (some checks not yet run live); **⬜** planned.

**Change log**

| Version | Date | Change |
|---|---|---|
| v0.1 | 2026-11-14 | First version. Maps the 185 numbered items to three phases and sets an exit criterion for each |

---

## 1. Purpose and rules

This document groups the roadmap's existing items into three phases, with an exit criterion for each. It does not add capabilities. The rules are:

1. **Only existing L-numbers are assigned.** Every one of the 185 items (L1 to L185) appears in exactly one phase (Appendix A). A check run while preparing this version confirmed this: no item is missing and none is listed twice.
2. **Phase names follow the review brief.** Phase 1 is MVP / Alpha, Phase 2 is Beta / enterprise readiness, and Phase 3 is Scale / GA. Each phase's theme is the one given in the brief.
3. **Some themes have no numbered item.** Object storage (Cairn Store), the SQL warehouse (Trino), tables and catalogue (Cairn Catalog) and discovery (Cairn Core) are rows of the roadmap's platform table, marked ✅, with no L-number. They are shown as un-numbered baseline rows in §3. Where the brief asks for "schema discovery APIs", no numbered item exists. [TO CONFIRM: a numbered item for schema discovery, or acceptance of the platform-table row as the evidence]
4. **Placement is a proposal.** Items that a theme does not name are placed in the phase where their dependencies and users sit. Each placement is a proposal [TO CONFIRM: product owner sign-off].
5. **Markers come from the roadmap.** A phase's status is the count of its items by marker. This document does not change any marker. Where the roadmap's text contradicts its marker, the item is flagged (§7 and BRD Appendix A).

**Reading the phases against the product today.** Most of what Phase 1 describes is shipped. Phase 2 holds most of the product, and Phase 3 is mostly open. The phases therefore record where the product stands and what gates each step. They are not a plan to build Phase 1 from nothing.

---

## 2. Summary

| Phase | Theme | Items | ✅ | 🟡 | ⬜ | Exit criterion (summary) |
|---|---|---|---|---|---|---|

| **Phase 1** | MVP / Alpha: core engine integration, object storage connectivity, basic query execution, schema discovery | 34 | 32 | 2 | 0 | All items ✅; L132 and L147 (🟡) closed by the live checks named in their rows |
| **Phase 2** | Beta / enterprise readiness: AI platform integration, LLMOps pipelines, lineage tracking, automated governance | 117 | 87 | 24 | 6 | Every item ✅ or formally deferred; every 🟡 live check run and recorded |
| **Phase 3** | Scale / GA: multi-cloud expansion, advanced agentic orchestration, enterprise compliance certification | 34 | 5 | 17 | 12 | Compliance evidence on file (L185); multi-cloud and fleet items validated live |
| **Total** | | **185** | **124** | **43** | **18** | Matches the roadmap: 124, 43, 18 |

**Finding.** Phase 1 is nearly complete (32 of 34 ✅). Phase 2 holds 87 ✅ and 30 open items. Phase 3 has 5 ✅, 17 🟡 and 12 ⬜, so the GA gate depends mostly on items that are not yet built or not yet validated.

### 2.1 The current release

The roadmap marks L139 to L152 as the current release (`cairn/ROADMAP.md`, "Next"). Of its 14 items, **2 are ✅ and 12 are 🟡**, and none is ⬜. By phase: 3 in Phase 1, 7 in Phase 2 and 4 in Phase 3. A release built on these items cannot be generally available until the 🟡 checks in §3 to §5 are run (BRD §11, stage 1).

| Item | Capability | Phase | Status |
|---|---|---|---|
| L139 | People & roles | P1 | ✅ |
| L140 | Managed Postgres | P3 | 🟡 |
| L141 | End-to-end observability for the whole platform | P2 | 🟡 |
| L142 | An SRE agent for operations and managed Postgres | P3 | 🟡 |
| L143 | Custom ops dashboards | P2 | 🟡 |
| L144 | Managed Airflow | P3 | 🟡 |
| L145 | Pipeline observability for Airflow, and OpenTelemetry export everywhere | P2 | 🟡 |
| L146 | Design system and screens from the platform design | P2 | 🟡 |
| L147 | Catalog-first: Cairn Core at the centre | P1 | 🟡 |
| L148 | Glossary and domain editing | P1 | ✅ |
| L149 | One home per concept (information architecture) | P2 | 🟡 |
| L150 | Calmer, consistent workspace | P2 | 🟡 |
| L151 | Snowflake and Databricks tables in place | P3 | 🟡 |
| L152 | Apache NiFi: governed data connectors | P2 | 🟡 |

---

## 3. Phase 1: MVP / Alpha

**Theme.** Core engine integration, object storage connectivity, basic query execution and schema discovery.

**What the phase delivers.** A lakehouse on Cairn Store and Cairn Catalog that Trino and Spark read and write under Cairn's policy; table maintenance, clones and branches; ingestion of files and streams into tables; and the catalogue foundations that every later phase uses.

**Evidence the roadmap records (examples, by item):**

- L2: Spark reads masked and row-filtered tables through Trino (`tests/test_spark_e2e.py`). The catalogue still refuses the raw files.
- L1: table maintenance on a schedule, with the definition of done that a table of 1,000 small files and 100 snapshots is compacted and expired (`tests/test_trino_e2e.py`).
- L12: zero-copy clones, undrop and branches in Cairn Catalog (`catalog/conformance/test_clones.py`).
- L19: concurrent Delta writers on Cairn Store; in-place conversion to Iceberg (`tests/test_trino_e2e.py`).
- L5: Kafka and Debezium into Iceberg, exactly once (`tests/test_streams_e2e.py`). L13: HTTP ingestion with per-channel offsets (`tests/test_ingest_http_e2e.py`).
- L22: customer-managed keys for Cairn Store in AWS KMS, Azure Key Vault, Google Cloud KMS and KMIP (`store/conformance/test_kms_cloud.py`).

**Dependencies on 🟡 or ⬜ items.** Phase 1 depends on two 🟡 items and no ⬜ item:

- **L132** (tables on the customer's own cloud storage). Azure, S3 and GCS locations are accepted only inside registered external locations, and the catalogue tests run against Azurite. The roadmap lists these as still open: ADLS Gen2 is checked by signature only (Azurite has no hierarchical namespace); GCS has no emulator and is not run; Spark on these tables is not run; and Entra, AWS STS, workload identities and Vault are not run against the real services. Phase 1's object-storage claim depends on it.
- **L147** (catalogue-first workspace, Cairn Core at the centre). The sections, data products and glossary are built and tested. It is 🟡 because some objects are not yet registered in Cairn Core: managed databases, agent apps and custom agents, observability data products, and file pipes and scheduled jobs (which emit no lineage). Phase 1's schema-discovery claim depends on it.

**Exit criterion.** All of the following are observable:

1. `cairn/ROADMAP.md` marks 32 of the 34 Phase 1 items ✅, and L132 and L147 are the only exceptions.
2. L132 (today 🟡) and L147 (today 🟡) move to ✅ once the open items in their rows are closed and recorded: for L132, the runs against the real Entra, AWS STS, workload-identity and Vault services, a GCS run, and Spark on the tables; for L147, the registration in Cairn Core of the objects listed in its row.
3. On the release candidate, the CI jobs `store`, `catalog`, `trino`, `python` and `stack` are green.
4. The L1 definition of done passes in the `trino` job.

**Un-numbered baseline (no L-number; the platform table of `cairn/ROADMAP.md`, all ✅):**

| Row | Capability | Status |
|---|---|---|
| Object storage | Cairn Store (Rust, S3 API): replication, erasure coding, versioning, object lock, lifecycle, tiering | ✅ |
| Tables and catalogue | Cairn Catalog (Rust, Iceberg REST): per-table credential vending, HA on PostgreSQL | ✅ |
| SQL warehouse | Trino 484 with per-team warehouses and Cairn sign-in | ✅ |
| Discovery and metadata | Cairn Core: search, column-level lineage, glossary, domains, data products | ✅ |

**Items in this phase:**

**1.1 Core engine integration (lakehouse, engines, catalogue)** (19 items; 19 ✅, 0 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L1 | Table maintenance service | ✅ |
| L2 | Governed Spark on restricted tables | ✅ |
| L3 | Spark on Kubernetes | ✅ |
| L4 | Notebooks | ✅ |
| L6 | Workload management and chargeback | ✅ |
| L12 | Zero-copy clones, undrop, branches and tags | ✅ |
| L14 | Query insight | ✅ |
| L17 | Scheduled SQL and in-product workflows | ✅ |
| L18 | Infrastructure as code for SQL and search | ✅ |
| L19 | Delta Lake beyond one writer, and into Iceberg | ✅ |
| L24 | Materialized views | ✅ |
| L25 | Change data feed | ✅ |
| L45 | Python functions | ✅ |
| L59 | Snapshot browser and restore | ✅ |
| L75 | Table branches, tags and data diff | ✅ |
| L86 | Iceberg v3 tables | ✅ |
| L87 | Adaptive warehouses | ✅ |
| L137 | Everything as code, with no configuration gaps | ✅ |
| L163 | Workspaces: a SQL editor like the best cloud warehouses' | ✅ |

**1.2 Object storage and file connectivity** (10 items; 9 ✅, 1 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L5 | Streaming and CDC ingestion | ✅ |
| L13 | HTTP streaming ingestion | ✅ |
| L15 | Files as data | ✅ |
| L22 | Customer keys beyond Vault | ✅ |
| L58 | Governed volumes | ✅ |
| L72 | Bad-row quarantine in pipes and streams | ✅ |
| L105 | External Iceberg catalogs, governed | ✅ |
| L115 | Zero-copy Parquet | ✅ |
| L132 | Tables on your own cloud storage | 🟡 |
| L133 | Storage credentials and external locations as governed objects | ✅ |

**1.3 Schema discovery and catalogue foundations** (5 items; 4 ✅, 1 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L38 | BI assets in the catalog | ✅ |
| L93 | Semantic model autopilot | ✅ |
| L139 | People & roles | ✅ |
| L147 | Catalog-first: Cairn Core at the centre | 🟡 |
| L148 | Glossary and domain editing | ✅ |

---

## 4. Phase 2: Beta / enterprise readiness

**Theme.** AI platform integration, LLMOps pipelines, lineage tracking and automated governance. This phase also holds the enterprise-readiness work that the BRD needs before general availability: self-service analytics, ingestion breadth, operations and commercial operations, and the developer experience.

**What the phase delivers.** A governed AI gateway that routes model calls and runs agents as the caller, with traces, abuse detection and sandboxes. Lineage and transformation across Flows, Airflow and dbt. Automated governance: masks, purposes, consent, reviews, retention and tokenisation. Self-service BI with metrics and dashboards. Operational assurance: HA, DR, load tests and observability.

**Evidence the roadmap records (examples):**

- L99 and L113: the AI gateway for agent code, with routing by capability, input size and timeout.
- L100 and L116: code-first agents, and sandboxes for agent code.
- L109 and L114: intents that declare purpose and actions, and abuse detection on inputs reaching models.
- L7 and L91: MLflow behind Cairn sign-in, with model monitoring, and fine-tuning on governed data (`tests/test_ml_e2e.py`, `tests/test_model_monitoring_e2e.py`).
- L9: disaster-recovery drill in CI, with a failover runbook (`tests/test_dr_drill.py`). L68: failover for tenant OpenSearch and Kafka.
- L67: twenty main pages checked with axe-core for WCAG 2.1 A and AA in CI.

**Dependencies on 🟡 or ⬜ items.** Phase 2 has 30 open items: 24 🟡 and 6 ⬜. The phase's exit depends on each of them.

- **Planned (⬜) items:** L168 Catalog connectors proven and widened; L171 Evaluation for agents and models; L172 dbt developed and run in the product; L174 The catalog's daily-use features in Cairn's own UI; L177 Curated question spaces; L182 SaaS and ERP sources on real accounts, and wider reverse ETL.
- **Partly validated (🟡) items:** L10, L61, L62, L98, L118, L119, L124, L125, L130, L134, L141, L143, L145, L146, L149, L150, L152, L154, L155, L156, L157, L165, L166, L167.

The three items most relevant to AI platform readiness are L154 (the whole API as MCP tools), L155 (the gateway as an MCP tool registry) and L171 (evaluation for agents and models). L171 is ⬜, and the BRD's release gate for answer quality (BR-AI-18) depends on it. Until it is built, no release can carry a measured answer-quality claim.

**Exit criterion.** All of the following are observable:

1. Every Phase 2 item is ✅ in `cairn/ROADMAP.md`, or is removed from this phase by a recorded decision in `BRD.md` §14. No item is left open without a decision.
2. Each 🟡 item in this phase has its named live checks run, with results recorded (BRD §11, stage 1).
3. On the release candidate, the CI jobs `python`, `pipelines`, `ml`, `spark-kind`, `ui`, `helm` and `stack` are green, and the `live-model` job has run with its secret set. The roadmap records that `live-model` has not yet run.
4. The four ✅ items whose live checks are recorded as not yet run (L19, L35, L94, L95) are either re-marked in `cairn/ROADMAP.md` or have their checks run and recorded.

**Items in this phase:**

**2.1 AI platform integration** (18 items; 14 ✅, 4 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L16 | Vector search on lakehouse data | ✅ |
| L77 | Speech to text for audio and video files | ✅ |
| L78 | AI enrichment of files: image description and field extraction | ✅ |
| L84 | The assistant's memory, artifacts and deep research | ✅ |
| L99 | AI gateway for agent code | ✅ |
| L100 | Code-first agents: `cairn agents init`, `dev`, `deploy` | ✅ |
| L102 | Grounded answers with sources | ✅ |
| L107 | Agents and skills in the marketplace | ✅ |
| L111 | Agent memory | ✅ |
| L112 | AI search and document parsing in SQL and pipelines | ✅ |
| L116 | Sandboxes for agent code | ✅ |
| L117 | AI data packages and agents on shared data | ✅ |
| L120 | Talk to your app | ✅ |
| L154 | The whole API over MCP | 🟡 |
| L155 | The AI gateway as an MCP tool registry | 🟡 |
| L156 | The Cairn agent in GitHub Copilot and other AI coding tools | 🟡 |
| L157 | Example agents, skills and plugins | 🟡 |
| L164 | Cairn agent kit (in-house) | ✅ |

**2.2 LLMOps and model lifecycle** (7 items; 6 ✅, 0 🟡, 1 ⬜)

| ID | Capability | Status |
|---|---|---|
| L7 | Machine learning | ✅ |
| L63 | Managed model serving | ✅ |
| L69 | Spark training and model serving in the chart | ✅ |
| L91 | Fine-tuning on governed data | ✅ |
| L101 | Agent trace intelligence | ✅ |
| L113 | Dynamic model routing in the AI gateway | ✅ |
| L171 | Evaluation for agents and models | ⬜ |

**2.3 Lineage, transformation and orchestration** (10 items; 8 ✅, 1 🟡, 1 ⬜)

| ID | Capability | Status |
|---|---|---|
| L20 | Git-backed SQL and notebooks | ✅ |
| L21 | Stored functions and procedures | ✅ |
| L23 | Data apps | ✅ |
| L57 | Scheduled notebook and Python jobs in Cairn | ✅ |
| L90 | App packages | ✅ |
| L110 | SCD type 2 and medallion layers | ✅ |
| L122 | Cairn Flows | ✅ |
| L123 | Flow canvas | ✅ |
| L124 | Airflow both ways | 🟡 |
| L172 | dbt developed and run in the product | ⬜ |

**2.4 Ingestion, mirrors and connector coverage** (14 items; 5 ✅, 7 🟡, 2 ⬜)

| ID | Capability | Status |
|---|---|---|
| L61 | SaaS data connectors | 🟡 |
| L62 | Reverse ETL | 🟡 |
| L89 | Mirrors | ✅ |
| L92 | Connectors: MySQL change data capture | ✅ |
| L96 | Connectors: SQL Server change data capture | ✅ |
| L97 | Connectors: MongoDB change streams | ✅ |
| L98 | Document sources: SharePoint and Google Drive | 🟡 |
| L104 | Streaming transformations | ✅ |
| L152 | Apache NiFi: governed data connectors | 🟡 |
| L165 | A connector builder and runner for data sources | 🟡 |
| L166 | Mirrors that replicate a whole database faithfully | 🟡 |
| L167 | Oracle, Db2 and SAP HANA as mirror sources | 🟡 |
| L168 | Catalog connectors proven and widened | ⬜ |
| L182 | SaaS and ERP sources on real accounts, and wider reverse ETL | ⬜ |

**2.5 Discovery and ontology** (4 items; 2 ✅, 0 🟡, 2 ⬜)

| ID | Capability | Status |
|---|---|---|
| L121 | Cairn Ontology | ✅ |
| L129 | Ontology explorer | ✅ |
| L174 | The catalog's daily-use features in Cairn's own UI | ⬜ |
| L177 | Curated question spaces | ⬜ |

**2.6 Automated governance and security** (17 items; 17 ✅, 0 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L26 | SIEM export | ✅ |
| L44 | Remote agent hardening | ✅ |
| L47 | SAML single sign-on | ✅ |
| L48 | Secrets for people's notebooks and jobs | ✅ |
| L50 | Retention enforced, legal hold and automated erasure | ✅ |
| L51 | Per-tenant and per-user API quotas, and tenant IP allowlists | ✅ |
| L66 | Consent management | ✅ |
| L71 | Access reviews | ✅ |
| L73 | Data residency | ✅ |
| L74 | Reversible tokenisation | ✅ |
| L76 | Synthetic test data | ✅ |
| L79 | Governed geospatial | ✅ |
| L82 | Security posture scan | ✅ |
| L83 | Passkeys and step-up | ✅ |
| L85 | Aggregation and projection policies | ✅ |
| L109 | Intent-based security for agents | ✅ |
| L114 | Agentic abuse detection | ✅ |

**2.7 Business intelligence and self-service analytics** (19 items; 18 ✅, 1 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L31 | BI: spreadsheet workbooks | ✅ |
| L32 | BI: input tables (writeback) | ✅ |
| L33 | BI: interactive dashboards | ✅ |
| L34 | BI: metrics layer | ✅ |
| L35 | BI: AI builder | ✅ |
| L36 | BI: app actions | ✅ |
| L37 | BI: scheduled exports and a result cache | ✅ |
| L39 | BI version history | ✅ |
| L40 | Embedded dashboards | ✅ |
| L41 | BI as code | ✅ |
| L42 | Input-table approvals | ✅ |
| L49 | Sharing BI assets with people and groups | ✅ |
| L52 | BI folders, comments and usage analytics | ✅ |
| L53 | Metric alerts | ✅ |
| L54 | Map and heatmap charts; dashboards that work on a phone | ✅ |
| L55 | Export files delivered to Slack and Teams | ✅ |
| L56 | Metrics over SQL for outside BI tools | ✅ |
| L103 | Serving tables (interactive analytics and hybrid tables) | ✅ |
| L118 | Cairn in Excel and Google Sheets | 🟡 |

**2.8 Data quality** (1 items; 1 ✅, 0 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L43 | Partition-aware volume monitors | ✅ |

**2.9 Reliability, observability and operations** (14 items; 9 ✅, 5 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L9 | Disaster recovery across regions | ✅ |
| L10 | Scale and assurance | 🟡 |
| L11 | Upgrade matrix | ✅ |
| L29 | Status page | ✅ |
| L30 | Gateway API | ✅ |
| L46 | Chaos tests | ✅ |
| L67 | Accessibility tested | ✅ |
| L68 | Failover for tenant OpenSearch and Kafka | ✅ |
| L70 | Sample datasets for new tenants | ✅ |
| L81 | Point-lookup acceleration | ✅ |
| L134 | System tables | 🟡 |
| L141 | End-to-end observability for the whole platform | 🟡 |
| L143 | Custom ops dashboards | 🟡 |
| L145 | Pipeline observability for Airflow, and OpenTelemetry export everywhere | 🟡 |

**2.10 Commercial operations and cost** (4 items; 4 ✅, 0 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L27 | Signup and billing | ✅ |
| L28 | Usage metering | ✅ |
| L60 | Cost per query, user and table on Trino | ✅ |
| L80 | Chargeback statements | ✅ |

**2.11 Developer and administrator experience** (9 items; 3 ✅, 6 🟡, 0 ⬜)

| ID | Capability | Status |
|---|---|---|
| L65 | SDKs beyond Python, and an API deprecation policy | ✅ |
| L119 | Profiles, mobile app and desktop | 🟡 |
| L125 | Everything as code | 🟡 |
| L127 | Friendly forms everywhere | ✅ |
| L128 | Guided experience | ✅ |
| L130 | CLI 2.0 | 🟡 |
| L146 | Design system and screens from the platform design | 🟡 |
| L149 | One home per concept (information architecture) | 🟡 |
| L150 | Calmer, consistent workspace | 🟡 |

---

## 5. Phase 3: Scale / GA

**Theme.** Multi-cloud expansion, advanced agentic orchestration, enterprise compliance certification. This phase also holds scale, sharing across organisations, developer reach and migration.

**What the phase delivers.** Lakehouse tables and external catalogues on the customer's own clouds; split control and data planes; signed releases, a fleet console and air-gapped bundles; agents that plan and propose infrastructure changes and run orchestration across platforms; and the external evidence that enterprise buyers and auditors require.

**Evidence the roadmap records (examples):**

- L22: customer-managed keys on AWS, Azure and Google Cloud. L133: storage credentials for Azure, S3 and GCS, held as governed objects.
- L105: external Iceberg catalogues, governed (ROADMAP L105).
- L94, L95 and L108: the pipeline agent, scheduled assistant tasks and CLI automations. The live-model checks for L94 and L95 are not yet run.
- L142 (SRE agent) and L126 (agent-proposed infrastructure changes) are 🟡 (partly validated).
- L185 is ⬜. The roadmap says the external penetration test and the audited controls need a third party, so they are not code.

**Dependencies on 🟡 or ⬜ items.** Phase 3 has 29 open items: 17 🟡 and 12 ⬜. Three dependencies deserve attention:

- **L132 (Phase 1, 🟡)** gates the multi-cloud table work. Phase 3's Azure, S3 and GCS claims depend on it.
- **L135 (split control and data plane, 🟡)** and **L136 (network controls, 🟡)** gate the customer-network deployment model. The air-gap bundles in L162 (🟡) are a related item in the same deployment area.
- **L185 (⬜)** gates compliance certification. L10 (🟡) is the penetration test within load and assurance, and its findings must be closed before the external test is counted.

**Exit criterion.** All of the following are observable. Where an item needs an external party, the party and the artefact are named:

1. Every Phase 3 item is ✅ in `cairn/ROADMAP.md`, or is deferred by a recorded decision.
2. L185 (today ⬜) reaches ✅ with its evidence on file: an external penetration test report for the gateway and the policy engine, with no open finding at the severity agreed in the BRD [TO CONFIRM: severity threshold], and an audit attestation for SOC 2 or ISO 27001 (BRD Q-02). [TO CONFIRM: the auditor and the report owner]
3. **L159 to L162 are validated live.** A signed release is installed and updated on a live cluster through an update channel; the fleet console shows at least one install; an air-gapped bundle installs with no outbound access. [TO CONFIRM: the live environments, cloud accounts and cluster names]
4. The upgrade matrix (L11) is green on the release candidate, and the upgrade playbook (legacy M5 row 5.6) is ✅ and tested against one engine and one Cairn Core minor release (BRD §11, stage 3).
5. The BRD's stage 2 and stage 3 exit criteria (BRD §11) are met.

**Items in this phase:**

**3.1 Multi-cloud, hybrid and managed services** (14 items; 1 ✅, 12 🟡, 1 ⬜)

| ID | Capability | Status |
|---|---|---|
| L8 | Open data sharing | 🟡 |
| L88 | Operational databases | ✅ |
| L135 | Split control plane and data plane | 🟡 |
| L136 | Network controls for user code, and private networking | 🟡 |
| L140 | Managed Postgres | 🟡 |
| L144 | Managed Airflow | 🟡 |
| L151 | Snowflake and Databricks tables in place | 🟡 |
| L153 | Models from the hyperscalers' model services | 🟡 |
| L158 | The AI setup as code | 🟡 |
| L159 | Signed releases and channels | 🟡 |
| L160 | Scheduled updates and the updater | 🟡 |
| L161 | The fleet console | 🟡 |
| L162 | Air-gapped release bundles | 🟡 |
| L178 | Reaching data that is elsewhere | ⬜ |

**3.2 Advanced agentic orchestration** (8 items; 3 ✅, 4 🟡, 1 ⬜)

| ID | Capability | Status |
|---|---|---|
| L64 | Build and deploy custom agents | 🟡 |
| L94 | Pipeline agent | ✅ |
| L95 | CoWork: scheduled tasks and an approved outbox | ✅ |
| L108 | Agent automations from the CLI: `cairn tasks` | ✅ |
| L126 | Agents do infrastructure as code | 🟡 |
| L138 | Orchestrate across platforms, from Cairn Flows and from Airflow | 🟡 |
| L142 | An SRE agent for operations and managed Postgres | 🟡 |
| L173 | Flows that compose | ⬜ |

**3.3 Scale, sharing, developer reach and migration** (11 items; 1 ✅, 1 🟡, 9 ⬜)

| ID | Capability | Status |
|---|---|---|
| L106 | Share observability | ✅ |
| L131 | Cairn Desktop 1.0 | 🟡 |
| L169 | Sharing beyond plain tables, and clean rooms across organisations | ⬜ |
| L170 | Migration tooling from Snowflake and Databricks | ⬜ |
| L175 | A Python client for the whole API | ⬜ |
| L176 | Stateful streaming SQL with a lag target | ⬜ |
| L179 | Machine learning at the vendors' depth | ⬜ |
| L180 | SQL engine gaps | ⬜ |
| L181 | Quality and cost at the source | ⬜ |
| L183 | Developer reach | ⬜ |
| L184 | An exchange between organisations | ⬜ |

**3.4 Enterprise compliance certification** (1 items; 0 ✅, 0 🟡, 1 ⬜)

| ID | Capability | Status |
|---|---|---|
| L185 | Not code: assurance and live validation | ⬜ |

---

## 6. Items not yet built

The 18 ⬜ items below come from the November 2026 capability audit (L168 to L185) and are not committed to a release. The phase column shows where each one gates an exit.

| ID | Capability | Phase | Gates |
|---|---|---|---|
| L168 | Catalog connectors proven and widened | P2 | Phase 2 exit |
| L169 | Sharing beyond plain tables, and clean rooms across organisations | P3 | Phase 3 exit |
| L170 | Migration tooling from Snowflake and Databricks | P3 | Phase 3 exit |
| L171 | Evaluation for agents and models | P2 | Phase 2 exit |
| L172 | dbt developed and run in the product | P2 | Phase 2 exit |
| L173 | Flows that compose | P3 | Phase 3 exit |
| L174 | The catalog's daily-use features in Cairn's own UI | P2 | Phase 2 exit |
| L175 | A Python client for the whole API | P3 | Phase 3 exit |
| L176 | Stateful streaming SQL with a lag target | P3 | Phase 3 exit |
| L177 | Curated question spaces | P2 | Phase 2 exit |
| L178 | Reaching data that is elsewhere | P3 | Phase 3 exit |
| L179 | Machine learning at the vendors' depth | P3 | Phase 3 exit |
| L180 | SQL engine gaps | P3 | Phase 3 exit |
| L181 | Quality and cost at the source | P3 | Phase 3 exit |
| L182 | SaaS and ERP sources on real accounts, and wider reverse ETL | P2 | Phase 2 exit |
| L183 | Developer reach | P3 | Phase 3 exit |
| L184 | An exchange between organisations | P3 | Phase 3 exit |
| L185 | Not code: assurance and live validation | P3 | Phase 3 exit |

---

## 7. Risks to the phase plan

| Risk | Effect on the phases | Mitigation |
|---|---|---|
| Phase 2 holds 117 items, most of the product | The exit is hard to meet in one step, and the test load is large | Split Phase 2 into two releases if the exit cannot be met. [TO CONFIRM: the split, if needed] |
| Live validation capacity (BRD R-01) | 43 🟡 items wait for live checks. The current release has 12 | Run live checks in the order the phases need them |
| Markers that rest on checks not yet run (BRD R-11) | Phase 2 and Phase 1 counts may overstate what is shipped | Re-mark or run the checks (BRD Appendix A, A-9 to A-12) |
| Two numbering systems (BRD R-12) | Readers may take legacy M-rows for L-items | Cite L-numbers; cite legacy rows with their milestone |
| Two backlogs (BRD R-09, Q-06) | `AGENTS.md` describes a 65-item queue. This roadmap has 185 items | Decide which list is the working backlog before the phases are scheduled |

---

## 8. Open items

1. Owner and approvers for each phase [TO CONFIRM: names].
2. Live environments for the 🟡 checks, including cluster, Slack and Teams endpoints, a live model, BI embeds, the fleet and air-gap flows [TO CONFIRM: environments and owners] (BRD Q-07).
3. Whether schema discovery needs a numbered item (§1, rule 3).
4. The phase placements in Appendix A, which are proposals (§1, rule 4).
5. The markers flagged in BRD Appendix A (A-9 to A-12): L19, L35, L94, L95 and legacy row 5.5.

---

## Appendix A. Item-level mapping

Every numbered item, its phase and sub-area, and its marker. Titles are from `cairn/doc/BRD-appendix-roadmap.md`. The marker is the roadmap's.

| ID | Capability | Phase | Sub-area | Status |
|---|---|---|---|---|
| L1 | Table maintenance service | P1 | 1.1 | ✅ |
| L2 | Governed Spark on restricted tables | P1 | 1.1 | ✅ |
| L3 | Spark on Kubernetes | P1 | 1.1 | ✅ |
| L4 | Notebooks | P1 | 1.1 | ✅ |
| L5 | Streaming and CDC ingestion | P1 | 1.2 | ✅ |
| L6 | Workload management and chargeback | P1 | 1.1 | ✅ |
| L7 | Machine learning | P2 | 2.2 | ✅ |
| L8 | Open data sharing | P3 | 3.1 | 🟡 |
| L9 | Disaster recovery across regions | P2 | 2.9 | ✅ |
| L10 | Scale and assurance | P2 | 2.9 | 🟡 |
| L11 | Upgrade matrix | P2 | 2.9 | ✅ |
| L12 | Zero-copy clones, undrop, branches and tags | P1 | 1.1 | ✅ |
| L13 | HTTP streaming ingestion | P1 | 1.2 | ✅ |
| L14 | Query insight | P1 | 1.1 | ✅ |
| L15 | Files as data | P1 | 1.2 | ✅ |
| L16 | Vector search on lakehouse data | P2 | 2.1 | ✅ |
| L17 | Scheduled SQL and in-product workflows | P1 | 1.1 | ✅ |
| L18 | Infrastructure as code for SQL and search | P1 | 1.1 | ✅ |
| L19 | Delta Lake beyond one writer, and into Iceberg | P1 | 1.1 | ✅ |
| L20 | Git-backed SQL and notebooks | P2 | 2.3 | ✅ |
| L21 | Stored functions and procedures | P2 | 2.3 | ✅ |
| L22 | Customer keys beyond Vault | P1 | 1.2 | ✅ |
| L23 | Data apps | P2 | 2.3 | ✅ |
| L24 | Materialized views | P1 | 1.1 | ✅ |
| L25 | Change data feed | P1 | 1.1 | ✅ |
| L26 | SIEM export | P2 | 2.6 | ✅ |
| L27 | Signup and billing | P2 | 2.10 | ✅ |
| L28 | Usage metering | P2 | 2.10 | ✅ |
| L29 | Status page | P2 | 2.9 | ✅ |
| L30 | Gateway API | P2 | 2.9 | ✅ |
| L31 | BI: spreadsheet workbooks | P2 | 2.7 | ✅ |
| L32 | BI: input tables (writeback) | P2 | 2.7 | ✅ |
| L33 | BI: interactive dashboards | P2 | 2.7 | ✅ |
| L34 | BI: metrics layer | P2 | 2.7 | ✅ |
| L35 | BI: AI builder | P2 | 2.7 | ✅ |
| L36 | BI: app actions | P2 | 2.7 | ✅ |
| L37 | BI: scheduled exports and a result cache | P2 | 2.7 | ✅ |
| L38 | BI assets in the catalog | P1 | 1.3 | ✅ |
| L39 | BI version history | P2 | 2.7 | ✅ |
| L40 | Embedded dashboards | P2 | 2.7 | ✅ |
| L41 | BI as code | P2 | 2.7 | ✅ |
| L42 | Input-table approvals | P2 | 2.7 | ✅ |
| L43 | Partition-aware volume monitors | P2 | 2.8 | ✅ |
| L44 | Remote agent hardening | P2 | 2.6 | ✅ |
| L45 | Python functions | P1 | 1.1 | ✅ |
| L46 | Chaos tests | P2 | 2.9 | ✅ |
| L47 | SAML single sign-on | P2 | 2.6 | ✅ |
| L48 | Secrets for people's notebooks and jobs | P2 | 2.6 | ✅ |
| L49 | Sharing BI assets with people and groups | P2 | 2.7 | ✅ |
| L50 | Retention enforced, legal hold and automated erasure | P2 | 2.6 | ✅ |
| L51 | Per-tenant and per-user API quotas, and tenant IP allowlists | P2 | 2.6 | ✅ |
| L52 | BI folders, comments and usage analytics | P2 | 2.7 | ✅ |
| L53 | Metric alerts | P2 | 2.7 | ✅ |
| L54 | Map and heatmap charts; dashboards that work on a phone | P2 | 2.7 | ✅ |
| L55 | Export files delivered to Slack and Teams | P2 | 2.7 | ✅ |
| L56 | Metrics over SQL for outside BI tools | P2 | 2.7 | ✅ |
| L57 | Scheduled notebook and Python jobs in Cairn | P2 | 2.3 | ✅ |
| L58 | Governed volumes | P1 | 1.2 | ✅ |
| L59 | Snapshot browser and restore | P1 | 1.1 | ✅ |
| L60 | Cost per query, user and table on Trino | P2 | 2.10 | ✅ |
| L61 | SaaS data connectors | P2 | 2.4 | 🟡 |
| L62 | Reverse ETL | P2 | 2.4 | 🟡 |
| L63 | Managed model serving | P2 | 2.2 | ✅ |
| L64 | Build and deploy custom agents | P3 | 3.2 | 🟡 |
| L65 | SDKs beyond Python, and an API deprecation policy | P2 | 2.11 | ✅ |
| L66 | Consent management | P2 | 2.6 | ✅ |
| L67 | Accessibility tested | P2 | 2.9 | ✅ |
| L68 | Failover for tenant OpenSearch and Kafka | P2 | 2.9 | ✅ |
| L69 | Spark training and model serving in the chart | P2 | 2.2 | ✅ |
| L70 | Sample datasets for new tenants | P2 | 2.9 | ✅ |
| L71 | Access reviews | P2 | 2.6 | ✅ |
| L72 | Bad-row quarantine in pipes and streams | P1 | 1.2 | ✅ |
| L73 | Data residency | P2 | 2.6 | ✅ |
| L74 | Reversible tokenisation | P2 | 2.6 | ✅ |
| L75 | Table branches, tags and data diff | P1 | 1.1 | ✅ |
| L76 | Synthetic test data | P2 | 2.6 | ✅ |
| L77 | Speech to text for audio and video files | P2 | 2.1 | ✅ |
| L78 | AI enrichment of files: image description and field extraction | P2 | 2.1 | ✅ |
| L79 | Governed geospatial | P2 | 2.6 | ✅ |
| L80 | Chargeback statements | P2 | 2.10 | ✅ |
| L81 | Point-lookup acceleration | P2 | 2.9 | ✅ |
| L82 | Security posture scan | P2 | 2.6 | ✅ |
| L83 | Passkeys and step-up | P2 | 2.6 | ✅ |
| L84 | The assistant's memory, artifacts and deep research | P2 | 2.1 | ✅ |
| L85 | Aggregation and projection policies | P2 | 2.6 | ✅ |
| L86 | Iceberg v3 tables | P1 | 1.1 | ✅ |
| L87 | Adaptive warehouses | P1 | 1.1 | ✅ |
| L88 | Operational databases | P3 | 3.1 | ✅ |
| L89 | Mirrors | P2 | 2.4 | ✅ |
| L90 | App packages | P2 | 2.3 | ✅ |
| L91 | Fine-tuning on governed data | P2 | 2.2 | ✅ |
| L92 | Connectors: MySQL change data capture | P2 | 2.4 | ✅ |
| L93 | Semantic model autopilot | P1 | 1.3 | ✅ |
| L94 | Pipeline agent | P3 | 3.2 | ✅ |
| L95 | CoWork: scheduled tasks and an approved outbox | P3 | 3.2 | ✅ |
| L96 | Connectors: SQL Server change data capture | P2 | 2.4 | ✅ |
| L97 | Connectors: MongoDB change streams | P2 | 2.4 | ✅ |
| L98 | Document sources: SharePoint and Google Drive | P2 | 2.4 | 🟡 |
| L99 | AI gateway for agent code | P2 | 2.1 | ✅ |
| L100 | Code-first agents: `cairn agents init`, `dev`, `deploy` | P2 | 2.1 | ✅ |
| L101 | Agent trace intelligence | P2 | 2.2 | ✅ |
| L102 | Grounded answers with sources | P2 | 2.1 | ✅ |
| L103 | Serving tables (interactive analytics and hybrid tables) | P2 | 2.7 | ✅ |
| L104 | Streaming transformations | P2 | 2.4 | ✅ |
| L105 | External Iceberg catalogs, governed | P1 | 1.2 | ✅ |
| L106 | Share observability | P3 | 3.3 | ✅ |
| L107 | Agents and skills in the marketplace | P2 | 2.1 | ✅ |
| L108 | Agent automations from the CLI: `cairn tasks` | P3 | 3.2 | ✅ |
| L109 | Intent-based security for agents | P2 | 2.6 | ✅ |
| L110 | SCD type 2 and medallion layers | P2 | 2.3 | ✅ |
| L111 | Agent memory | P2 | 2.1 | ✅ |
| L112 | AI search and document parsing in SQL and pipelines | P2 | 2.1 | ✅ |
| L113 | Dynamic model routing in the AI gateway | P2 | 2.2 | ✅ |
| L114 | Agentic abuse detection | P2 | 2.6 | ✅ |
| L115 | Zero-copy Parquet | P1 | 1.2 | ✅ |
| L116 | Sandboxes for agent code | P2 | 2.1 | ✅ |
| L117 | AI data packages and agents on shared data | P2 | 2.1 | ✅ |
| L118 | Cairn in Excel and Google Sheets | P2 | 2.7 | 🟡 |
| L119 | Profiles, mobile app and desktop | P2 | 2.11 | 🟡 |
| L120 | Talk to your app | P2 | 2.1 | ✅ |
| L121 | Cairn Ontology | P2 | 2.5 | ✅ |
| L122 | Cairn Flows | P2 | 2.3 | ✅ |
| L123 | Flow canvas | P2 | 2.3 | ✅ |
| L124 | Airflow both ways | P2 | 2.3 | 🟡 |
| L125 | Everything as code | P2 | 2.11 | 🟡 |
| L126 | Agents do infrastructure as code | P3 | 3.2 | 🟡 |
| L127 | Friendly forms everywhere | P2 | 2.11 | ✅ |
| L128 | Guided experience | P2 | 2.11 | ✅ |
| L129 | Ontology explorer | P2 | 2.5 | ✅ |
| L130 | CLI 2.0 | P2 | 2.11 | 🟡 |
| L131 | Cairn Desktop 1.0 | P3 | 3.3 | 🟡 |
| L132 | Tables on your own cloud storage | P1 | 1.2 | 🟡 |
| L133 | Storage credentials and external locations as governed objects | P1 | 1.2 | ✅ |
| L134 | System tables | P2 | 2.9 | 🟡 |
| L135 | Split control plane and data plane | P3 | 3.1 | 🟡 |
| L136 | Network controls for user code, and private networking | P3 | 3.1 | 🟡 |
| L137 | Everything as code, with no configuration gaps | P1 | 1.1 | ✅ |
| L138 | Orchestrate across platforms, from Cairn Flows and from Airflow | P3 | 3.2 | 🟡 |
| L139 | People & roles | P1 | 1.3 | ✅ |
| L140 | Managed Postgres | P3 | 3.1 | 🟡 |
| L141 | End-to-end observability for the whole platform | P2 | 2.9 | 🟡 |
| L142 | An SRE agent for operations and managed Postgres | P3 | 3.2 | 🟡 |
| L143 | Custom ops dashboards | P2 | 2.9 | 🟡 |
| L144 | Managed Airflow | P3 | 3.1 | 🟡 |
| L145 | Pipeline observability for Airflow, and OpenTelemetry export everywhere | P2 | 2.9 | 🟡 |
| L146 | Design system and screens from the platform design | P2 | 2.11 | 🟡 |
| L147 | Catalog-first: Cairn Core at the centre | P1 | 1.3 | 🟡 |
| L148 | Glossary and domain editing | P1 | 1.3 | ✅ |
| L149 | One home per concept (information architecture) | P2 | 2.11 | 🟡 |
| L150 | Calmer, consistent workspace | P2 | 2.11 | 🟡 |
| L151 | Snowflake and Databricks tables in place | P3 | 3.1 | 🟡 |
| L152 | Apache NiFi: governed data connectors | P2 | 2.4 | 🟡 |
| L153 | Models from the hyperscalers' model services | P3 | 3.1 | 🟡 |
| L154 | The whole API over MCP | P2 | 2.1 | 🟡 |
| L155 | The AI gateway as an MCP tool registry | P2 | 2.1 | 🟡 |
| L156 | The Cairn agent in GitHub Copilot and other AI coding tools | P2 | 2.1 | 🟡 |
| L157 | Example agents, skills and plugins | P2 | 2.1 | 🟡 |
| L158 | The AI setup as code | P3 | 3.1 | 🟡 |
| L159 | Signed releases and channels | P3 | 3.1 | 🟡 |
| L160 | Scheduled updates and the updater | P3 | 3.1 | 🟡 |
| L161 | The fleet console | P3 | 3.1 | 🟡 |
| L162 | Air-gapped release bundles | P3 | 3.1 | 🟡 |
| L163 | Workspaces: a SQL editor like the best cloud warehouses' | P1 | 1.1 | ✅ |
| L164 | Cairn agent kit (in-house) | P2 | 2.1 | ✅ |
| L165 | A connector builder and runner for data sources | P2 | 2.4 | 🟡 |
| L166 | Mirrors that replicate a whole database faithfully | P2 | 2.4 | 🟡 |
| L167 | Oracle, Db2 and SAP HANA as mirror sources | P2 | 2.4 | 🟡 |
| L168 | Catalog connectors proven and widened | P2 | 2.4 | ⬜ |
| L169 | Sharing beyond plain tables, and clean rooms across organisations | P3 | 3.3 | ⬜ |
| L170 | Migration tooling from Snowflake and Databricks | P3 | 3.3 | ⬜ |
| L171 | Evaluation for agents and models | P2 | 2.2 | ⬜ |
| L172 | dbt developed and run in the product | P2 | 2.3 | ⬜ |
| L173 | Flows that compose | P3 | 3.2 | ⬜ |
| L174 | The catalog's daily-use features in Cairn's own UI | P2 | 2.5 | ⬜ |
| L175 | A Python client for the whole API | P3 | 3.3 | ⬜ |
| L176 | Stateful streaming SQL with a lag target | P3 | 3.3 | ⬜ |
| L177 | Curated question spaces | P2 | 2.5 | ⬜ |
| L178 | Reaching data that is elsewhere | P3 | 3.1 | ⬜ |
| L179 | Machine learning at the vendors' depth | P3 | 3.3 | ⬜ |
| L180 | SQL engine gaps | P3 | 3.3 | ⬜ |
| L181 | Quality and cost at the source | P3 | 3.3 | ⬜ |
| L182 | SaaS and ERP sources on real accounts, and wider reverse ETL | P2 | 2.4 | ⬜ |
| L183 | Developer reach | P3 | 3.3 | ⬜ |
| L184 | An exchange between organisations | P3 | 3.3 | ⬜ |
| L185 | Not code: assurance and live validation | P3 | 3.4 | ⬜ |

## Appendix B. Sources

| Source | Used for |
|---|---|
| `cairn/ROADMAP.md` | Items, markers, evidence, the platform table, the current release |
| `cairn/doc/BRD-appendix-roadmap.md` | Titles and marker totals (checked against `ROADMAP.md`) |
| `cairn/UX_ACTIVE_METADATA_SPEC.md` | UX milestones U1 to U7 (legacy M6 rows) |
| `docs/product/BRD.md` | Requirements, targets, risks and open questions |
