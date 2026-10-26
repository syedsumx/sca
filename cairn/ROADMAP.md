# Cairn Implementation Roadmap

Milestones are ordered by dependency. Each milestone has a definition of done (DoD) that can be
checked against a running stack. **Status** reflects this repository as of v0.1:
✅ implemented and unit-tested · 🟡 implemented, needs live-stack validation · ⬜ not started.

## M0 — Foundations (week 1–2)

| # | Deliverable | Status |
|---|---|---|
| 0.1 | Cairn Core contracts and extension points mapped (maintainer notes in `core/`) | ✅ |
| 0.2 | Python package skeleton: settings, Cairn Core client (Rest.li ingestProposal, OpenAPI v3, GraphQL), MCP builders | ✅ |
| 0.3 | Cairn database layer (SQLAlchemy Core; Postgres prod / SQLite tests), hash-chained audit log + verifier | ✅ |
| 0.4 | Caller identity: Cairn Core PAT validation via Cairn Core `me`, UI session cookies via frontend, service token | ✅ |
| 0.5 | Compose stack (Cairn Core + Cairn + pgvector + nginx edge) and Helm umbrella chart | ✅ compose stack runs healthy; 🟡 Helm only lint/kubeconform |

**DoD:** `docker compose -f docker-compose.cairn.yml up` reaches healthy. `cairn bootstrap` creates the
certification structured properties, the `Certified` tag and the `Classification.PII.*` terms.

## M1 — Native Data Quality & Anomaly Detection (week 2–5)

| # | Deliverable | Status |
|---|---|---|
| 1.1 | Detectors: seasonal robust-z (MAD), EWMA, learned freshness cadence, PSI + KS drift, schema diff, static bounds | ✅ |
| 1.2 | Collectors: Cairn Core signals (`operation`, `datasetProfile`, `schemaMetadata`) and SQL (dialect-quoted, read-only guard) | ✅ |
| 1.3 | Engine: idempotent run ids, outage buffering + flush, feedback loop, incident raise/auto-resolve that never reopens human-closed incidents | ✅ |
| 1.4 | Projection to native `assertionInfo` / `assertionRunEvent` / `incidentInfo` | ✅ verified live via Cairn Core GraphQL (e2e) |
| 1.5 | Leader-elected scheduler (pg advisory lock) with hash partitioning | ✅ leadership acquired on Postgres in the live stack |
| 1.6 | REST API + React dashboard, monitor editor, evaluation chart, dataset health badge | ✅ |
| 1.7 | Next: smart-monitor auto-provisioning (create volume/freshness monitors for every certified or top-usage dataset) | ⬜ |
| 1.8 | Next: per-column profiling collector (null/distinct for all columns in one scan) and partition-aware volume | ⬜ |
| 1.9 | Next: optional Kafka-sink of evaluations for real-time alerting fan-out (PagerDuty/Opsgenie native) | ⬜ |

**DoD:** a volume monitor on a demo table pages within one interval when rows drop 90%. It auto-resolves
on recovery, and both events are visible on the dataset's Quality and Incidents tabs.

## M2 — Enterprise Governance (week 4–8)

| # | Deliverable | Status |
|---|---|---|
| 2.1 | ABAC engine: condition DSL (17 operators, `ref` to subject attrs, dotted structured-property keys), safe regex, targets | ✅ |
| 2.2 | PDP: deny-overrides, RBAC roles + inheritance + bindings, obligations, sub-resource checks, explain traces, decision cache with MCL-driven invalidation | ✅ |
| 2.3 | Native `dataHubPolicy` import (actors, owners/owner-types, resource filters) with retry until Cairn Core is up | ✅ |
| 2.4 | Java `CairnAuthorizer` Cairn Core plugin: fail-closed, circuit breaker, TTL cache, root/system bypass | ✅ 16 JUnit tests; loads and initialises in Cairn Core (Java 25) |
| 2.5 | Additive vs authoritative mode (`CAIRN_NATIVE_POLICIES_ENABLED=false` + PDP as sole authority) | 🟡 needs live Cairn Core validation |
| 2.6 | PII classifier (names + validated value patterns) and lineage propagation action with attribution, loop safety, revert | ✅ propagation verified live (Kafka → actions → Cairn Core) |
| 2.7 | Certification/approval workflows: quorum, SoD, veto, entry checks, expiry, DQ auto-revoke, projection (structured property + tag) | ✅ |
| 2.8 | UI: access simulator, policy editor, certification inbox | ✅ |
| 2.9 | Next: MCL consumer that calls `AttributeResolver.invalidate` (today: TTL 60 s) | ⬜ |
| 2.10 | Next: enforce `mask_columns` obligations in Cairn Core GraphQL responses (field-level redaction resolver) and in the Cairn SQL proxy | ⬜ |
| 2.11 | Next: policy-as-code CI (`cairn policies validate` + simulation test fixtures in Git) and SCIM/IdP attribute sync | ⬜ |

**DoD:** with authoritative mode on, a contractor gets DENY for `VIEW_DATASET_PROFILE` on a PII
dataset in the native UI while an owner can still edit it. Tagging an upstream column as PII appears
on downstream columns within one MCL round-trip.

## M3 — In-VPC Remote Ingestion Agents (week 6–10)

| # | Deliverable | Status |
|---|---|---|
| 3.1 | Crypto: Ed25519 request signing, X25519/HKDF/AES-256-GCM sealed envelopes with AAD binding, bounded decompression | ✅ |
| 3.2 | Gateway: one-time enrolment tokens, nonce/skew replay protection, leasing with expiry + retries, sequence enforcement, pool policy MCP filtering, protected entity types | ✅ |
| 3.3 | Dispatcher: claims native `dataHubExecutionRequest`s by `executorId == pool`, updates `dataHubExecutionRequestResult` | ✅ verified live (poll mode); ⬜ MCL mode wiring |
| 3.4 | Agent: enrolment, heartbeat, poll loop with backoff, local secret resolution (env/dir/Vault/AWS SM), encrypted `cairn-encrypted` sink | ✅ real Postgres ingestion through an isolated network verified live |
| 3.5 | Helm chart `cairn-remote-agent` (restricted PSS, read-only rootfs, egress-only NetworkPolicy) | ✅ lint/kubeconform |
| 3.6 | UI: fleet, pools, enrolment-token flow, runs | ✅ |
| 3.7 | Next: cancellation (`dataHubExecutionRequestSignal` → heartbeat `cancel_task_ids` → pipeline interrupt) | ⬜ gateway side done |
| 3.8 | Next: encrypted live log streaming from the pipeline logger, key rotation via signed re-enrolment, mTLS option | ⬜ |
| 3.9 | Next: on-disk spool for batches during gateway outages | ⬜ |

**DoD:** a Postgres source reachable *only* from the `vpc-finance` network is ingested through the
agent. The recipe in Cairn Core contains only `${PG_PASSWORD}`, and the gateway rejects a forged
platform URN.

## M4 — AI Context & Agentic Discovery (week 8–12)

| # | Deliverable | Status |
|---|---|---|
| 4.1 | LLM layer: Claude (`claude-opus-5-5`, structured outputs, adaptive thinking, effort, server-side refusal fallback) + Ollama for air-gapped | ✅ request shape unit-tested; 🟡 live API call |
| 4.2 | NL → typed `SearchPlan` → Cairn Core search/lineage → grounded answer; hallucinated citations dropped; heuristic fallback | ✅ |
| 4.3 | Semantic index: entity documents, content-hash dedupe, pgvector HNSW / in-memory, MCL indexer action | ✅; ⬜ `embedding_provider=catalog` (delegate to Cairn Core `SemanticSearchService`) |
| 4.4 | Documentation generation → `DOCUMENTATION` workflow → attributed write on approval | ✅ |
| 4.5 | Guardrails: PDP-filtered retrieval, PII redaction, untrusted-data wrapping, LLM call audit | ✅ |
| 4.6 | MCP server (stdio + streamable HTTP with PAT auth, 11 tools) | ✅ verified live against Cairn Core |
| 4.7 | UI: Ask Cairn Core panel, doc draft review | ✅ |
| 4.8 | Next: eval set for NLQ plans and doc drafts (golden questions per domain), cost tracking per user | ⬜ |
| 4.9 | Next: query-log grounding (popular SQL from `queryProperties`) and column-level lineage in doc prompts | ⬜ |

**DoD:** in Claude Code, `ask_metadata("which certified tables feed the revenue dashboard?")` returns
only assets the caller can see, each with a citation. `generate_documentation` creates a review item and
writes nothing until it is approved.

## M5 — Hardening & GA (week 12–16)

| # | Deliverable | Status |
|---|---|---|
| 5.1 | Build and scan images (`docker/Dockerfile`, `agent.Dockerfile`, plugin image); SBOM; cosign signatures | ✅ build (`deploy/build-images.sh`, apt-free images); ⬜ scan, SBOM, signing, registry push in CI |
| 5.2 | End-to-end smoke tests against the compose stack (Playwright UI + API), chaos tests (Cairn Core/PDP/gateway outages) | ✅ API smoke suite `tests/e2e` (8 scenarios); ⬜ Playwright UI, chaos tests, CI job |
| 5.3 | Load test PDP (target p99 < 5 ms cached, < 25 ms uncached at 2k rps) and gateway (100 agents, 1 GB metadata/run) | ⬜ |
| 5.4 | Alembic migrations for the `cairn` schema (v0.1 uses `create_all`) | ⬜ |
| 5.5 | Threat model review + external pentest of the gateway; key management via KMS/HSM for the gateway identity | ⬜ |
| 5.6 | Upgrade playbook: pin-and-test matrix against Cairn Core minor releases (aspect shapes, GraphQL fields) | ⬜ |

## M6 — Active Metadata & AI Context Layer (UX_ACTIVE_METADATA_SPEC.md)

| # | Deliverable | Status |
|---|---|---|
| U1 | Consumer MFE shell (`/mfe/cairn`), `/ui/config`, collaboration (bookmarks, follows, views, trending, threads with @mentions), Discover + faceted search | ✅ code + tests; ⬜ mounted in a live cairn-web, search p95 measured |
| U2 | Marketplace: product cards/detail, request policies, access requests with auto-approval, SoD, time-boxed persona grants with expiry and warnings, Slack/Teams/email approvals | ✅ code + tests; ⬜ live Slack round-trip |
| U3 | Personas & Purposes (studio, compiler into PDP layers, purpose denies at priority 1000, masking obligations), `cairn-core-proxy` masking with fail-closed behaviour, rule-based tag/term propagation (`cairn_label_propagation`) | ✅ code + tests; ⬜ compliance sign-off on the live stack |
| U4 | Context agents: query/dbt/BI/run miners, METRIC_DEFINITION / DEFINITION_DRIFT / TERM_* / JOIN_RELATIONSHIP / RUN_ANOMALY findings, accept → native glossary terms / ER relationships / discussions, ontology | ✅ code + tests; ⬜ pilot-domain acceptance rate |
| U5 | Assistant (Claude tool loop over caller-scoped tools, offline fallback), lineage-anomaly explainer, markdown docs | ✅ code + tests; ⬜ golden-set evaluation with a live model |
| U6 | Embeds: signed origin-bound tokens, web components + iframe pages, Tableau `.trex`, Looker/Power BI/portal guides; Teams adaptive cards | ✅ code + tests; ⬜ validated inside each BI tool |
| U7 | Deployment: compose services (`cairn-core-proxy`, `cairn-context-miner`, `cairn-notifier`, `cairn-consumer-ui`), Helm templates + `values-active-metadata.yaml`, Slack app manifest | ✅ rendered and linted; ⬜ live cluster |

## Issues found by the first live run (fixed)

Running against real Cairn Core surfaced five defects that unit tests with fakes could not catch:

1. Cairn Core loads `<plugin>/<plugin-name>.jar`, but the build produced a versioned jar name, so Cairn Core failed to start.
2. Cairn Core rejects `NO_EXPIRY` access tokens. Bootstrap now mints a token with a finite lifetime
   (`CAIRN_CORE_TOKEN_DURATION`, default `THREE_MONTHS`) and re-mints it when it stops validating.
3. The admin API eagerly created gateway keys, which crashed on a read-only root filesystem. Key loading is
   now lazy and happens only in `cairn gateway`.
4. Compose `tmpfs` mounts default to `root 0755`, which the non-root agent cannot write. They are now
   mounted with `mode=1777`.
5. OpenSearch blocks index creation at its 95% disk flood stage. The threshold is configurable in compose
   via `CAIRN_SEARCH_DISK_THRESHOLD_ENABLED`.

## Code-generation plan (how each milestone was / will be produced)

1. **Contract first:** derive the shape of each aspect and API from the Cairn Core schemas (aspect
   models and the GraphQL schema). Encode it in builders
   (`quality/emitter.py`, `governance/certification.py`) with a unit test per shape.
2. **Pure core, thin adapters:** detectors, ABAC evaluation, propagation planning and the crypto envelope
   are pure functions tested in isolation. The engine, PDP, gateway and workflows compose them. HTTP,
   Kafka and MCP layers are thin wrappers around them.
3. **Fakes over mocks:** `InMemoryGraph` implements the same `MetadataGraph` protocol as `CoreClient`,
   so every module test runs the real code path end to end without a live Cairn Core.
4. **Live validation gate:** before a milestone's 🟡 items flip to ✅, run its DoD scenario on the
   compose stack and add it to the smoke suite (M5.2).
