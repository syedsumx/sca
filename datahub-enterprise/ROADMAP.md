# DHE Implementation Roadmap

Milestones are ordered by dependency. Each milestone has a definition of done (DoD) that can be
checked against a running stack. **Status** reflects this repository as of v0.1:
✅ implemented and unit-tested · 🟡 implemented, needs live-stack validation · ⬜ not started.

## M0 — Foundations (week 1–2)

| # | Deliverable | Status |
|---|---|---|
| 0.1 | Reverse-engineering analysis of DataHub Core and the extension points (`ARCHITECTURE_ENTERPRISE.md` Part 1) | ✅ |
| 0.2 | Python package skeleton: settings, GMS client (Rest.li ingestProposal, OpenAPI v3, GraphQL), MCP builders | ✅ |
| 0.3 | DHE database layer (SQLAlchemy Core; Postgres prod / SQLite tests), hash-chained audit log + verifier | ✅ |
| 0.4 | Caller identity: DataHub PAT validation via GMS `me`, UI session cookies via frontend, service token | ✅ |
| 0.5 | Compose stack (DataHub v1.8.0 core + DHE + pgvector + nginx edge) and Helm umbrella chart | 🟡 config/lint/kubeconform validated; images not built here |

**DoD:** `docker compose -f docker-compose.enterprise.yml up` reaches healthy. `dhe bootstrap` creates the
certification structured properties, the `Certified` tag and the `Classification.PII.*` terms.

## M1 — Native Data Quality & Anomaly Detection (week 2–5)

| # | Deliverable | Status |
|---|---|---|
| 1.1 | Detectors: seasonal robust-z (MAD), EWMA, learned freshness cadence, PSI + KS drift, schema diff, static bounds | ✅ |
| 1.2 | Collectors: DataHub signals (`operation`, `datasetProfile`, `schemaMetadata`) and SQL (dialect-quoted, read-only guard) | ✅ |
| 1.3 | Engine: idempotent run ids, outage buffering + flush, feedback loop, incident raise/auto-resolve that never reopens human-closed incidents | ✅ |
| 1.4 | Projection to native `assertionInfo` / `assertionRunEvent` / `incidentInfo` | 🟡 shapes follow the PDL; confirm in the UI Quality tab |
| 1.5 | Leader-elected scheduler (pg advisory lock) with hash partitioning | ✅ (lock path needs Postgres) |
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
| 2.3 | Native `dataHubPolicy` import (actors, owners/owner-types, resource filters) with retry until GMS is up | ✅ |
| 2.4 | Java `EnterpriseAuthorizer` GMS plugin: fail-closed, circuit breaker, TTL cache, root/system bypass | ✅ 16 JUnit tests against auth-api 1.5.0; 🟡 1.7.x path untested (needs JDK 25) |
| 2.5 | Additive vs authoritative mode (`AUTH_POLICIES_ENABLED=false` + PDP as sole authority) | 🟡 needs live GMS validation |
| 2.6 | PII classifier (names + validated value patterns) and lineage propagation action with attribution, loop safety, revert | ✅ |
| 2.7 | Certification/approval workflows: quorum, SoD, veto, entry checks, expiry, DQ auto-revoke, projection (structured property + tag) | ✅ |
| 2.8 | UI: access simulator, policy editor, certification inbox | ✅ |
| 2.9 | Next: MCL consumer that calls `AttributeResolver.invalidate` (today: TTL 60 s) | ⬜ |
| 2.10 | Next: enforce `mask_columns` obligations in GMS GraphQL responses (field-level redaction resolver) and in the DHE SQL proxy | ⬜ |
| 2.11 | Next: policy-as-code CI (`dhe policies validate` + simulation test fixtures in Git) and SCIM/IdP attribute sync | ⬜ |

**DoD:** with authoritative mode on, a contractor gets DENY for `VIEW_DATASET_PROFILE` on a PII
dataset in the native UI while an owner can still edit it. Tagging an upstream column as PII appears
on downstream columns within one MCL round-trip.

## M3 — In-VPC Remote Ingestion Agents (week 6–10)

| # | Deliverable | Status |
|---|---|---|
| 3.1 | Crypto: Ed25519 request signing, X25519/HKDF/AES-256-GCM sealed envelopes with AAD binding, bounded decompression | ✅ |
| 3.2 | Gateway: one-time enrolment tokens, nonce/skew replay protection, leasing with expiry + retries, sequence enforcement, pool policy MCP filtering, protected entity types | ✅ |
| 3.3 | Dispatcher: claims native `dataHubExecutionRequest`s by `executorId == pool`, updates `dataHubExecutionRequestResult` | ✅ (poll mode); ⬜ MCL mode wiring |
| 3.4 | Agent: enrolment, heartbeat, poll loop with backoff, local secret resolution (env/dir/Vault/AWS SM), encrypted `dhe-encrypted` sink | ✅ protocol tested with fake pipeline; 🟡 real `datahub ingest` run |
| 3.5 | Helm chart `dhe-remote-agent` (restricted PSS, read-only rootfs, egress-only NetworkPolicy) | ✅ lint/kubeconform |
| 3.6 | UI: fleet, pools, enrolment-token flow, runs | ✅ |
| 3.7 | Next: cancellation (`dataHubExecutionRequestSignal` → heartbeat `cancel_task_ids` → pipeline interrupt) | ⬜ gateway side done |
| 3.8 | Next: encrypted live log streaming from the pipeline logger, key rotation via signed re-enrolment, mTLS option | ⬜ |
| 3.9 | Next: on-disk spool for batches during gateway outages | ⬜ |

**DoD:** a Postgres source reachable *only* from the `vpc-finance` network is ingested through the
agent. The recipe in DataHub contains only `${PG_PASSWORD}`, and the gateway rejects a forged
platform URN.

## M4 — AI Context & Agentic Discovery (week 8–12)

| # | Deliverable | Status |
|---|---|---|
| 4.1 | LLM layer: Claude (`claude-opus-5-5`, structured outputs, adaptive thinking, effort, server-side refusal fallback) + Ollama for air-gapped | ✅ request shape unit-tested; 🟡 live API call |
| 4.2 | NL → typed `SearchPlan` → GMS search/lineage → grounded answer; hallucinated citations dropped; heuristic fallback | ✅ |
| 4.3 | Semantic index: entity documents, content-hash dedupe, pgvector HNSW / in-memory, MCL indexer action | ✅; ⬜ `embedding_provider=datahub` (delegate to GMS `SemanticSearchService`) |
| 4.4 | Documentation generation → `DOCUMENTATION` workflow → attributed write on approval | ✅ |
| 4.5 | Guardrails: PDP-filtered retrieval, PII redaction, untrusted-data wrapping, LLM call audit | ✅ |
| 4.6 | MCP server (stdio + streamable HTTP with PAT auth, 11 tools) | ✅ incl. HTTP round-trip test |
| 4.7 | UI: Ask DataHub panel, doc draft review | ✅ |
| 4.8 | Next: eval set for NLQ plans and doc drafts (golden questions per domain), cost tracking per user | ⬜ |
| 4.9 | Next: query-log grounding (popular SQL from `queryProperties`) and column-level lineage in doc prompts | ⬜ |

**DoD:** in Claude Code, `ask_metadata("which certified tables feed the revenue dashboard?")` returns
only assets the caller can see, each with a citation. `generate_documentation` creates a review item and
writes nothing until it is approved.

## M5 — Hardening & GA (week 12–16)

| # | Deliverable | Status |
|---|---|---|
| 5.1 | Build and scan images (`docker/Dockerfile`, `agent.Dockerfile`, plugin image); SBOM; cosign signatures | ⬜ |
| 5.2 | End-to-end smoke tests against the compose stack (Playwright UI + API), chaos tests (GMS/PDP/gateway outages) | ⬜ |
| 5.3 | Load test PDP (target p99 < 5 ms cached, < 25 ms uncached at 2k rps) and gateway (100 agents, 1 GB metadata/run) | ⬜ |
| 5.4 | Alembic migrations for the `dhe` schema (v0.1 uses `create_all`) | ⬜ |
| 5.5 | Threat model review + external pentest of the gateway; key management via KMS/HSM for the gateway identity | ⬜ |
| 5.6 | Upgrade playbook: pin-and-test matrix against DataHub minor releases (aspect shapes, GraphQL fields) | ⬜ |

## Code-generation plan (how each milestone was / will be produced)

1. **Contract first:** derive the shape of each aspect from the upstream PDL (`metadata-models/.../*.pdl`)
   and each API from `datahub-graphql-core/src/main/resources/*.graphql`. Encode it in builders
   (`quality/emitter.py`, `governance/certification.py`) with a unit test per shape.
2. **Pure core, thin adapters:** detectors, ABAC evaluation, propagation planning and the crypto envelope
   are pure functions tested in isolation. The engine, PDP, gateway and workflows compose them. HTTP,
   Kafka and MCP layers are thin wrappers around them.
3. **Fakes over mocks:** `InMemoryGraph` implements the same `MetadataGraph` protocol as `GmsClient`,
   so every module test runs the real code path end to end without a live DataHub.
4. **Live validation gate:** before a milestone's 🟡 items flip to ✅, run its DoD scenario on the
   compose stack and add it to the smoke suite (M5.2).
