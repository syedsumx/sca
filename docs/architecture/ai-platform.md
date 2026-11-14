# Cairn AI Platform: agents, retrieval, model execution and LLMOps

| | |
|---|---|
| Document | Technical architecture, AI platform (2 of 3) |
| Evidence date | 2026-11-14, repository commit `783865d7` (branch `claude/funny-meitner-qtz0ux`) |
| Scope | The agentic control plane, the AI gateway, the MCP server, retrieval over metadata and tables, secure model execution, LLMOps, telemetry for AI |
| Companion documents | [data-engine.md](data-engine.md), [system-design.md](system-design.md), [openapi.json](openapi.json) |
| Primary sources | `cairn/ARCHITECTURE.md` section 1.5, `cairn/ROADMAP.md`, `cairn/doc/BRD.md` (BR-AI-03, Q-04), the module docstrings under `cairn/src/cairn/ai/`, `identity/`, `agentkit/` |

**Status markers.** ✅ shipped and tested end to end in CI, 🟡 shipped but partly validated, ⬜ planned, as the roadmap uses them. The L-numbers are the roadmap's queue items. Evidence is a source file (relative to `cairn/`), a test file, or a roadmap row. Unverified statements are tagged **[TO CONFIRM: …]**.

**Naming.** Three components share the word "gateway". This document uses these names only:

| Name | What it is | Where |
|---|---|---|
| **AI gateway** | Model endpoints for agent code: Anthropic-protocol and OpenAI-protocol routes, with budgets, routes and metering (L99 ✅, L113 ✅) | `src/cairn/ai/gateway.py`, `gateway_routes.py`, `cloud_providers.py` |
| **Agent gateway** (remote agents) | The control-plane process that takes signed, encrypted batches from in-VPC ingestion agents (`cairn gateway`, port 8820). It is not an AI component. | `src/cairn/agent/gateway.py`; `cairn/ARCHITECTURE.md` section 1.4 |
| **Dataplane tunnel** | A separate route family on the gateway process (`/dataplanes/tunnel/…`), used for connections that the control plane reaches through an agent | `src/cairn/dataplane/` (`api.py`, `connector.py`, `service.py`); the gateway process route list (see `system-design.md`, section 5.2) |

---

## 1. Summary

- Agents are **principals with scopes**, not free-running processes. Every agent request runs as a person (the sponsor or the caller), is narrowed to a declared **intent** (purpose, action categories, tables, domains), is checked against scopes, and is metered and traced. Agent credentials are refused where a person's token is required (`src/cairn/agentkit/tasks.py`, `src/cairn/identity/agents.py`, `src/cairn/identity/intents.py`).
- Models are reached through two paths. Cairn's own features call the configured provider (`CAIRN_LLM_PROVIDER`). Agent code calls the AI gateway with a Cairn token, so no provider key is ever given to agent code.
- Retrieval is authorization-aware. Assets the caller cannot view are dropped before anything reaches a prompt, and text that is classified as PII is redacted first.
- Without a configured model, each AI feature takes its own fallback. Natural-language search falls back to a deterministic parser, and document generation fails with an error and writes nothing (`src/cairn/ai/llm.py:163-170`; `cairn/ARCHITECTURE.md` section 1.9). Whether every feature behaves this way when no model is reachable is not yet tested (section 8).
- LLMOps is partly in place. Offline golden-case evaluation exists for the built-in features. Evaluation of custom agents and prompt registries is planned (L171 ⬜). Fine-tuning and model serving are shipped with stated limits (L91, L63, L69).

---

## 2. The agentic control plane

### 2.1 Agents and tasks

| Element | What it is | Status | Evidence |
|---|---|---|---|
| Custom agents | Defined from the assistant's tools, with scopes `read`, `query_data`, `propose`; deployed with draft-on-change; run as the caller narrowed to their tools and scopes; under the pause switch, quotas, AI budget and AI-system approval | 🟡 (a live-model suite exists in CI job `live-model` and has not run) | L64; `src/cairn/ai/custom_agents.py`; `/v1/custom-agents` |
| Code-first agents | `cairn agents init`, `dev`, `deploy`. Each app has its own agent identity. Per-run tokens are narrowed to the app's scopes and revoked after the run. | ✅ | L100; `src/cairn/agentkit/`; `src/cairn/ai/agent_apps.py`; `/v1/agent-apps` |
| Agent identity and credentials | Agent credentials use the prefixes `ca_` and `cat_`. Each is validated by the agent hub, and an unknown, expired or revoked one is refused. | ✅ | `src/cairn/common/security.py` (`_resolve`); `src/cairn/identity/agents.py` |
| Agent scopes | Checked on every request. A route with no declared scope is refused for agents. | ✅ | `src/cairn/app.py` (`_principal_dependency`: "not allowed for agents") |
| Intents | Each run declares purpose and action categories. Anything outside is refused with HTTP 403 and an audit event `agent.intent_denied`. | ✅ | L109; `src/cairn/identity/intents.py` (categories `read`, `query`, `propose`, `write`, `share`, `export`, `model`, `admin`) |
| Pipeline agent | Proposes a derived table and checks; each try is planned, previewed and checked on Trino as the person, writing nothing; approval creates the table as the person. Three tries. The model never sees rows. | ✅ | L94; `/v1/pipeline-agent/tasks` |
| CoWork (scheduled tasks and outbox) | The assistant runs a person's scheduled research at most hourly, as that person, with their policies and budget. Output is held in an outbox until the person approves sending it to Slack or email. A task that fails three times in a row is paused. | ✅ | L95; `src/cairn/ai/cowork.py`; `/ai/scheduled-tasks`, `/ai/outbox` |
| `cairn tasks` | CoWork tasks from the command line, with the person's token from the environment | ✅ | L108; `src/cairn/agentkit/tasks.py` |
| SRE agent | A built-in agent identity with read-mostly scopes (`read`, `model`, `iac:propose`). Each run uses a 15-minute credential for the person it acts for. | 🟡 | L142; `src/cairn/sre/` |
| Assistant, deep research and steward | The assistant and its workspace runs, and the steward API under `/ai/steward` | ✅ (features as listed in the roadmap "AI" row) | `src/cairn/ai/assistant.py`, `workspace.py`, `steward.py`; the roadmap "AI" row |
| Agents in marketplace | Agent apps, custom agents and extensions are listed and requested. Approval grants a time-boxed persona with `USE_AGENT` (or `USE_SKILL`) on the agent only. | ✅ | L107 |
| Agent memory | Memory per agent and person (scope `memory`), team and shared memories written only by people, recall by meaning plus recency. PII, credentials and instructions are refused at write. | ✅ | L111; `src/cairn/ai/agent_memory.py` (docstring); migration `0129` |

**Two kinds of "task".** In the remote-ingestion protocol, a task is a leased unit of ingestion work (`/agents/{id}/tasks/poll`, served by the gateway process, with leases and `max_attempts`). In the AI platform, a task is a CoWork scheduled task or a pipeline-agent task. The two are not the same API.

### 2.2 The MCP server

- **Transports.** `stdio` (`cairn mcp`, identity from `CAIRN_MCP_TOKEN`) and `streamable-http` at `/mcp` on port 8830 (compose service `cairn-mcp`; edge path `/cairn/mcp/`). Over HTTP, every request carries `Authorization: Bearer <Cairn access token>` (`src/cairn/ai/mcp_server.py` module docstring).
- **Identity.** Every tool runs as the calling user. Results are filtered by the PDP. Write-like tools only create review proposals (`mcp_server.py` docstring).
- **Surface.** The server registers **46** tools with `@server.tool` (`src/cairn/ai/mcp_server.py`), four resources (three templates, `cairn://asset/{urn}`, `cairn://lineage/{urn}`, `cairn://quality/{urn}`, and the fixed `cairn://definitions`), and prompts. `cairn/ARCHITECTURE.md` section 1.5 lists 11 of the tools. See section 8, item 3.
- **Whole API over MCP (L154 🟡).** The roadmap reports a catalogue generated from the OpenAPI description in `cairn.ai.mcp_api`, with 786 tools in 12 toolsets and 43 exclusions with stated reasons in the default build. The openapi.json in this folder is the same kind of source. [TO CONFIRM: that the generated catalogue matches the spec in this folder at the same commit; the roadmap figures are not reproduced here.]
- **External MCP servers (L155 🟡).** `cairn.ai.mcp_registry` registers external servers (for example GitHub, Jira, ServiceNow or an internal server). Credentials come from secret scopes. Tools are exposed as `<server>__<tool>`. Calls are rate-limited and audited.
- **Coding agents (L156 🟡, L157 🟡).** `cairn ide init` writes MCP configuration for VS Code, GitHub Copilot, `.mcp.json`, Cursor and JetBrains (`src/cairn/ai/ide.py`). `cairn agents scan` (L164 ✅) checks MCP configurations, tool settings, hooks, agents and skills against 14 rules, and can refuse registration of unsafe custom agents and MCP servers (`src/cairn/agentkit/scan.py`).

### 2.3 Telemetry and intent controls for agents

| Control | What it does | Status | Evidence |
|---|---|---|---|
| Trace intelligence | Per agent: runs, tool calls, errors, refusals, error rate, latency (average and p95), slow calls, loops (the same tool with the same redacted arguments at least `loop_threshold` times in one run), and outliers against its own baseline | ✅ | L101; `src/cairn/ai/trace_insights.py` (docstring); `/v1/agent-traces/insights`; migration `0124` (calls) |
| Abuse detection | Rule-based detectors with severities on inputs reaching models (prompt injection, invisible characters), and on outputs and tool arguments (secrets, PII dumps, destinations outside an allowlist, tools out of scope) | ✅ | L114; `src/cairn/ai/abuse.py` (docstring); migration `0129` |
| Guardrail primitives | `redact`, `untrusted` (wraps catalog text as data), `filter_viewable`, `strip_invisible`, `injection_signals`, `screen`, `pii_values`, `secret_kinds` | ✅ (code) | `src/cairn/ai/guardrails.py` |
| Sandboxes for agent code | Python, or SQL over DuckDB, in a container or pod per run. No network by default (`docker --network none`). An allowlisted egress proxy is used when a run asks for network. Data comes only through the person's governed token. Limits on time, memory and CPU. | ✅ | L116; `src/cairn/ai/sandboxes.py` (docstring; line 642 `--network none`); `/v1/sandboxes/runs` |

---

## 3. AI gateway: model endpoints for agent code

| Surface | Path | Notes |
|---|---|---|
| Anthropic-protocol route | `POST /v1/ai/gateway/anthropic/v1/messages` | Agent code uses the provider's SDK, with `base_url` pointed at Cairn and a Cairn token instead of a provider key |
| OpenAI-protocol route | `POST /v1/ai/gateway/openai/v1/chat/completions`, `…/embeddings`, `GET …/models` | For OpenAI or any OpenAI-compatible server (for example vLLM or Ollama) |
| Models the caller may use | `GET /v1/ai/gateway/models` | |
| Routes (virtual model names) | `GET/PUT/DELETE /v1/ai/gateway/routes`, `…/routes/{name}` | Ordered candidates with rules, cost ceiling, strategy (ordered or cheapest), latency budget and fallback (L113 ✅) |

**Each call** (`src/cairn/ai/gateway.py` docstring):

- is made **as someone**: a person's token, or an agent key that acts for its owner and needs the agent scope `model`. The agent pause switch and quotas apply.
- needs `USE_AI_GATEWAY` on `urn:li:aiModel:<model>`, and the model must be listed in `CAIRN_AI_GATEWAY_MODELS`. Nothing listed means every model is refused.
- is held to the deployment's daily AI budget and the person's daily token allowance, and to `CAIRN_AI_GATEWAY_RPM` calls per minute per person.
- reaches the provider with **Cairn's** key (`CAIRN_AI_GATEWAY_ANTHROPIC_KEY`, `CAIRN_AI_GATEWAY_OPENAI_KEY`, or their `_FILE` forms). No provider key is given to agent code.
- is metered, streamed responses included, under the person, as feature `gateway` or `gateway:<agent>`, and audited without its content.
- streams are passed through as they arrive. Usage is read from the stream's own events.

**Upstreams.** Additional named upstreams can be configured (`CAIRN_AI_GATEWAY_UPSTREAMS`, `CAIRN_AI_GATEWAY_MODEL_UPSTREAMS`). Cloud model services are supported through `CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS`, with three kinds: **bedrock** (Amazon Bedrock, SigV4 through the credential chain), **foundry** (Azure AI Foundry, Entra workload identity or a key), and **vertex** (Google Vertex AI). L153 is 🟡. Its tests (`tests/test_ai_gateway_clouds.py`) use stand-ins built to each cloud's published API, and the Bedrock signatures are recomputed by botocore. [TO CONFIRM: live runs against each cloud service, which the L153 row does not claim.]

**Dynamic routes (L113 ✅).** A route's candidates are filtered by the caller's rights and by input size and capabilities (tools, vision). Fallback is triggered by 5xx, timeouts, unreachable providers and HTTP 429, and streams fall back only before the first byte. A candidate the caller may not use is skipped. If none is left, the call is refused.

---

## 4. Metadata and vector retrieval

### 4.1 Semantic index over metadata

- **Entity documents.** Assets are rendered to text (name, description, column names and descriptions, tags, terms, domain, owners) (`src/cairn/ai/catalog.py`, `entity_document`).
- **Index maintenance.** The semantic index is kept fresh from Cairn Core's metadata change log, and it skips re-embedding when the content hash is unchanged (`src/cairn/ai/semantic.py` docstring: "kept fresh from MCL"; `src/cairn/ai/embeddings.py`, `content_hash`).
- **Vector stores.** `PgVectorStore` (pgvector, cosine, an HNSW index named `<table>_hnsw`) and `InMemoryVectorStore` for tests (`src/cairn/ai/embeddings.py:200-225`). The extension is created by the compose or Helm init.
- **Embedding providers** (`CAIRN_EMBEDDING_PROVIDER`; `src/cairn/common/config.py:91`): `hashing` (a deterministic local model for tests and air-gapped checks), `ollama` (in-VPC), or `catalog` (Cairn Core's native semantic search).
- **Retrieval guardrails.** Retrieval is authorization-aware. Entities the caller cannot view are dropped before they reach a prompt (`filter_viewable`). Text is redacted for PII before prompt assembly (`redact`). Catalog text is wrapped as untrusted data, so a planted instruction in a description is read as data (`untrusted`, `guardrails.py:36`).

### 4.2 Vector indexes on lakehouse tables

- A vector index is `<table>__<index>_vectors`, one per index, refreshed incrementally, and searched with READ on the source table. Results never bypass a mask or row filter on the source (L16 ✅; `tests/test_vectors_e2e.py`).
- Approximate search (IVF lists trained at refresh, with exact search when the stored index does not match the current snapshot) is described in the L16 row with a recall figure. The same row also lists approximate indexes as "to do". [TO CONFIRM: the status of approximate search. See `data-engine.md`, section 9, item 5.]
- **Grounded answers (L102 ✅).** `POST /v1/vectors/<name>/answer` retrieves passages as the caller, numbers them, and has the deployment's model answer from them. Citations are checked: sentences without a valid citation are flagged, and the answer is refused when no passage supports it. The call is audited without content.
- **`ai_search` in Trino SQL (L112 ✅).** A Trino function searches a vector index as the query's user, with no mask or row filter on the source. Cairn's own identities are refused.

### 4.3 Natural-language search and documentation

- **NL to a structured search plan** (`src/cairn/ai/nlq.py`). The model returns a typed `SearchPlan` through structured output, validated against a JSON schema. The plan is executed against Cairn Core search, never as free SQL. A deterministic heuristic parser serves when no model is configured.
- **Documentation drafts** (`src/cairn/ai/docgen.py`). Drafts are proposals in the governance workflow and are never written directly. Approval writes the description with attribution to the approver. AI-created proposals are attributed to `urn:li:corpuser:cairn-ai` (`src/cairn/common/proposals.py:19`).
- **Semantic models** (L93 ✅) are drafted from profiles and query shapes as the person, with no SQL text kept from history.

---

## 5. Secure model execution

### 5.1 Provider selection and configuration

| Setting | Values | Evidence |
|---|---|---|
| `CAIRN_LLM_PROVIDER` | `anthropic` (the hosted provider, via its SDK), `ollama` (in-VPC, native `/api/chat` with a JSON-schema format), `none` (default) | `src/cairn/common/config.py:83, 159`; `src/cairn/ai/llm.py` (classes `AnthropicProvider`, `OllamaProvider`, `StaticProvider`) |
| Default model and per-task models | `CAIRN_AI_MODEL` (the default), `CAIRN_AI_MODEL_DOCGEN`, `CAIRN_AI_MODEL_NLQ` | `src/cairn/common/config.py:160-162` (the default value is not reproduced in this document) |
| Server-side fallback for refusals | `CAIRN_AI_SERVER_SIDE_FALLBACK`, default on | `src/cairn/common/config.py:164`; `src/cairn/ai/llm.py` module docstring |
| Ollama | `CAIRN_OLLAMA_URL`, `CAIRN_OLLAMA_MODEL` | `src/cairn/common/config.py:165-166` |
| Provider keys | Read from the environment or from files (`_SECRET_FILE_PREFIXES`) | `src/cairn/common/config.py:25` |

`build_provider` returns `None` when the provider is `none` (`src/cairn/ai/llm.py:163-170`). Every feature then takes its non-AI path.

### 5.2 Structured output only

Cairn asks models for structured results only. The one primitive is `generate_json`, which returns a dict validated against a JSON schema (`src/cairn/ai/llm.py` module docstring; `validate_against_schema`, line 172). Free-form SQL is never executed from a model's answer.

### 5.3 Identity of model calls

- **Cairn's own features** run as the caller. `MeteredProvider` wraps every provider. The caller and the feature are carried in context variables, so concurrent requests cannot attribute usage to each other (`src/cairn/ai/usage.py` module docstring).
- **Budgets.** A call is refused with `LLMBudgetExceeded` when the deployment's daily spend or the caller's daily token allowance is used up. That error is an `LLMError`, so each feature falls back to its non-AI path.
- **Metering.** Usage (who, feature, model, tokens, estimated cost) is recorded in `cairn_ai_usage` and written to the audit log. Prices are list prices in USD per million tokens, and `CAIRN_AI_PRICES` overrides them. Self-hosted models cost 0.
- **Agent code** calls the AI gateway, which acts as the person or as the agent for its owner (section 3). Agent code never holds a provider key.
- **Credentials.** The gateway's provider keys are held by Cairn. [TO CONFIRM: ARCHITECTURE.md's statement that "the LLM never receives credentials or raw query results" is not verified by a test in this review.]

### 5.4 Offline and no-model behaviour (BR-AI-03)

The business requirement BR-AI-03 in `cairn/doc/BRD.md` (line 257) reads: "Model provider is configurable (Anthropic, OpenAI-compatible, cloud model services, or local models); the platform works when no model is reachable". Its status is recorded as "Module E (U5); L99 [✅], L113 [✅]".

What the code does without a model:

| Feature | Behaviour when no model is configured or reachable | Evidence |
|---|---|---|
| Natural-language search | Deterministic heuristic parser | `cairn/ARCHITECTURE.md` section 1.9; `src/cairn/ai/nlq.py` |
| Documentation drafts | Fails with an error; nothing is written | `cairn/ARCHITECTURE.md` section 1.9; `src/cairn/ai/docgen.py` |
| Budget exhausted | Each feature takes its non-AI path | `src/cairn/ai/usage.py` |
| Assistant | Runs in offline mode (the roadmap's verification table) | `cairn/README.md` "Verification status" |

**[TO CONFIRM: an end-to-end test that runs every AI feature with `CAIRN_LLM_PROVIDER=none` and with the provider unreachable, and records the result. This is needed to close BR-AI-03 on evidence.]**

Open question Q-04 in the BRD (line 440) asks which provider is the default for paying customers and whether local-only operation is required. It is open.

### 5.5 Sandboxes (execution of agent code)

Section 2.3 summarises the controls. The security-relevant points, from `src/cairn/ai/sandboxes.py`:

- A run never executes inside Cairn's process. Each run gets a new container (`CAIRN_SANDBOX_RUNTIME=docker`) or a new pod in `CAIRN_SANDBOX_K8S_NAMESPACE`, optionally under a sandboxing runtime class such as gVisor.
- The container runs as a non-root user with no capabilities. Its working directory is in memory.
- Network is off unless a run asks for it, and then only through a per-run egress proxy with an allowlist.
- Data comes only through the person's governed token, so the run sees what that person sees.

[TO CONFIRM: the gVisor and Kubernetes pod settings in a real cluster. The roadmap's L116 row describes the docker and Kubernetes runtimes; it does not say which were run live.]

---

## 6. LLMOps

| Area | Status | What exists | Evidence |
|---|---|---|---|
| Evaluation of built-in AI features | ✅ (offline) | `cairn ai eval` runs golden cases from YAML (`config/ai-evals`). By default cases run against a scripted model and check guardrails deterministically: PII redacted in the prompt, catalog text wrapped as untrusted, hidden assets absent from the prompt and from the answer, no invented columns or citations. With `--live` they run against the configured provider and report tokens, estimated cost and latency. | `src/cairn/ai/evals.py` (docstring); `tests/test_ai_evals.py` |
| Evaluation of custom agents and models | ⬜ | Evaluation sets, scorers and model judges over custom agents and their traces, regression gates in CI and before deploy, and a prompt registry with versions. The roadmap says `ai/evals.py` covers built-in features only. | L171 ⬜ |
| Live-model evaluation | 🟡 **[TO CONFIRM]** | The live-model suite runs in CI job `live-model` when the `ANTHROPIC_API_KEY` secret is set. The roadmap records it as "not yet run" (L64), and the README lists calls to the hosted provider and to Ollama under "Not yet run live". | L64; `cairn/README.md` "Verification status" |
| Fine-tuning on governed data | ✅ for LoRA and full fine-tuning on PyTorch CPU; GPU nodes and provider fine-tuning APIs are not covered | Training sets come from the governed query path, with PII redacted and sources recorded. Training runs in an offline, isolated worker. Generation runs behind `USE_FINETUNE` and is audited without prompts. `cairn finetune-model download`. | L91 ✅; `/v1/ml/finetunes`; `tests/test_finetune.py` (ml job) |
| Model serving | ✅ (local processes, and Kubernetes) | Deployments of registered MLflow versions, invoked as the caller (`USE_ML_MODEL`), scaled by in-flight concurrency with scale-to-zero. In the chart, one Deployment per model, with no credentials in pods. | L63 ✅; L69 ✅ (kind run 287 in CI) |
| Experiment tracking and model registry | ✅ | MLflow behind Cairn sign-in at `/v1/ml`. Runs attributed to the caller; registry owned and governed; versions in the catalog with lineage from training tables. | L7 ✅; `tests/test_ml_e2e.py` |
| Spark training tracked in MLflow | ✅ | `cairn_spark.mlflow` runs the job as its user, with lineage | L69 ✅ |
| Usage, budgets and cost | ✅ | Per-person and deployment budgets; cost estimates; `cairn_ai_usage` | `src/cairn/ai/usage.py` |

---

## 7. Telemetry and audit for AI

- **Audit.** An append-only, hash-chained audit log. Each entry stores `sha256(prev_hash ‖ canonical_json(body))` (`src/cairn/common/audit.py` module docstring). Verification is `GET /governance/audit/verify`. Export is `GET /governance/audit/export`. Records go to SIEM sinks (Splunk HEC, syslog with RFC 5424 or CEF, HTTPS) with at-least-once delivery (L26 ✅; `tests/test_siem_e2e.py`).
- **AI calls in the audit.** The gateway and the usage meter audit each call without its content (`gateway.py`, `usage.py` docstrings). Prompt content is not stored by default. The setting `CAIRN_AI_AUDIT_PROMPTS` is parsed (`src/cairn/common/config.py:94, 170`), but no code path reads it. See section 8, item 2.
- **Agent calls.** Every traced tool call is recorded with its run, including custom agents' runs, agent-app run tokens and OAuth sessions (`cairn_agent_calls`, migration `0124`, per L101).
- **OpenTelemetry (L141 🟡).** `src/cairn/observability/telemetry.py` exports traces, metrics and logs over OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Only `http/protobuf` is shipped; gRPC falls back to it with a warning. FastAPI requests, outgoing HTTP calls (with W3C `traceparent` injected), SQLAlchemy statements and background cycles are traced. Trino queries carry the trace id as client tag `cairn_trace`. The roadmap says Grafana Cloud, Honeycomb, Datadog and New Relic are documented and untested, and that the Helm stack has been checked with `helm template` and kubeconform, not run on a cluster.
- **Observability schema.** Migration `0139` creates `cairn_obs_config`, `cairn_obs_alerts`, `cairn_obs_incidents`, `cairn_obs_incident_events` and `cairn_obs_deploys` (L141).

[TO CONFIRM: the ARCHITECTURE.md section 1.1 statement about observability ("an OpenTelemetry trace header passed through to Cairn Core") is an understatement of L141. The current design is in `telemetry.py`. This is a documentation point, not a gap.]

---

## 8. Open items and discrepancies

| # | Item | Status | Needed to close |
|---|---|---|---|
| 1 | Live tests of the model paths: the `live-model` CI job has not been run, and calls to the hosted provider and to Ollama are listed as "not yet run live" | **[TO CONFIRM]** | A recorded run of the live-model suite with the secret set |
| 2 | `CAIRN_AI_AUDIT_PROMPTS` is parsed but not used (`src/cairn/common/config.py:94, 170`). ARCHITECTURE.md section 1.5 says prompts are kept only when it is `true`. | **Discrepancy** | Either implement the flag or correct the architecture text |
| 3 | `cairn/ARCHITECTURE.md` section 1.5 lists 11 MCP tools; `src/cairn/ai/mcp_server.py` registers 46 | **Discrepancy** (the list is a subset) | Correct the list in ARCHITECTURE.md, or state that it is a subset |
| 4 | "The LLM never receives credentials or raw query results" (ARCHITECTURE.md section 1.5, guardrail 4) | **[TO CONFIRM]** | A test that inspects prompts for credentials and raw rows |
| 5 | BR-AI-03: no evidence in the repository for "works when no model is reachable" across all features | **[TO CONFIRM]** | The test in section 5.4 |
| 6 | Cloud model services (L153 🟡): tests use stand-ins, not live Bedrock, Foundry or Vertex accounts | **[TO CONFIRM]** | Live runs, or a stated list of what remains a stand-in |
| 7 | Evaluation of custom agents, scorers, judges, regression gates and prompt registry (L171 ⬜) | Planned | L171 |
| 8 | Default provider for paying customers and whether local-only operation is required (BRD Q-04) | Open | Product decision |
| 9 | Sandbox runtime settings (gVisor, pod security) on a real cluster | **[TO CONFIRM]** | A cluster run |
| 10 | The MCP catalogue generated from the OpenAPI description (L154: 786 tools, 43 exclusions) | Roadmap figure, not re-checked here | A regeneration at the same commit, compared with the count |
| 11 | ARCHITECTURE.md section 1.5, guardrail 3, says every call is audited with "entities in context". The usage record stores model, feature, actor and tokens. The entities field was not found. | **[TO CONFIRM]** | Confirm the audit fields in `usage.py` |

---

## 9. Traceability to the roadmap

| Capability | L-number | Status |
|---|---|---|
| Agent platform (code-first agents, agent apps) | L100 | ✅ |
| Custom agents | L64 | 🟡 |
| Intent-based security | L109 | ✅ |
| Agent memory | L111 | ✅ |
| AI gateway for agent code | L99 | ✅ |
| Dynamic model routing | L113 | ✅ |
| Agent trace intelligence | L101 | ✅ |
| Abuse detection | L114 | ✅ |
| Sandboxes | L116 | ✅ |
| Grounded answers with sources | L102 | ✅ |
| Vector search on lakehouse data | L16 | ✅ (see section 4.2) |
| AI search in SQL and pipelines | L112 | ✅ |
| AI enrichment of files | L78 | ✅ |
| Semantic model autopilot | L93 | ✅ |
| Pipeline agent | L94 | ✅ |
| CoWork | L95 | ✅ |
| SRE agent | L142 | 🟡 |
| Cloud model services | L153 | 🟡 |
| Whole API over MCP | L154 | 🟡 |
| External MCP servers | L155 | 🟡 |
| Coding agents (IDE configuration) | L156 | 🟡 |
| Example agents, skills and plugins | L157 | 🟡 |
| Agent kit and security scanner | L164 | ✅ |
| Fine-tuning | L91 | ✅ (CPU) |
| Model serving | L63, L69 | ✅ |
| MLflow integration | L7 | ✅ |
| Evaluation for agents and models | L171 | ⬜ |
| OpenTelemetry for the platform | L141 | 🟡 |
| Share packages with semantic context | L117 | ✅ |
| Talk to your app (data apps) | L120 | ✅ |
| Data-app AI in marketplace | L107 | ✅ |
