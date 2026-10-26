# Cairn

Cairn is a self-hosted active-metadata and data-governance platform. **Cairn Core** is its metadata
system of record (typed metadata graph, search, lineage, catalog UI and APIs); the Cairn control plane
adds these modules on top:

| Module | What you get |
|---|---|
| **A. Native Data Quality** | Freshness, volume, field-metric, distribution-drift, schema and custom-SQL monitors with learned baselines. Results appear as native assertions and incidents. |
| **B. Enterprise Governance** | An ABAC + RBAC Policy Decision Point with deny-overrides, a Cairn Core authorizer plugin, PII classification with column-lineage propagation, and certification and approval workflows. |
| **C. In-VPC Remote Agents** | Outbound-only ingestion agents. They resolve secrets locally and stream metadata signed and end-to-end encrypted. |
| **D. AI Context & Discovery** | An MCP server, natural-language catalog Q&A, semantic vector search, and AI documentation that goes through human review. |
| **E. Active Metadata & AI Context Layer** | A consumer workspace micro-frontend: Discover, faceted search, trending assets, discussions, bookmarks, and a data marketplace where access is requested without tickets. It adds Personas & Purposes, with purpose denies overriding persona grants, and PII masking in the `cairn-core-proxy`. Rule-based tag and term propagation runs over column lineage. Context agents mine queries, dbt, BI and job runs for definitions, drift and ontologies. The layer also includes an assistant, plain-English lineage-anomaly explanations, and Slack/Teams actions. Trust components can be embedded in Tableau, Looker, Power BI and portals. |

Read **[ARCHITECTURE.md](ARCHITECTURE.md)** for the design, **[UX_ACTIVE_METADATA_SPEC.md](UX_ACTIVE_METADATA_SPEC.md)** for the consumer UX and
active-metadata blueprint (module E), and **[ROADMAP.md](ROADMAP.md)** for milestones and status.
Deployment of module E is covered in [deploy/README-active-metadata.md](deploy/README-active-metadata.md).

## Layout

```
src/cairn/                   Python services (one package, many entry points: `cairn …`)
  common/  quality/  governance/  agent/  ai/  services.py  app.py  cli.py
  collab/  marketplace/  context/  notify/  embed/  coreproxy/      (module E)
plugins/java/cairn-authorizer/   Cairn Core Authorizer plugin → PDP
ui/                          @cairn/web-react-modules + consumer workspace MFE (src/consumer) + embed SDK (src/embed)
embeds/                      Tableau / Looker / Power BI / portal packaging for the embed components
config/                      policy bundle, workflow templates, monitor + connection examples
deploy/  docker/             bootstrap scripts, actions pipelines, Dockerfiles, nginx edge
docker-compose.cairn.yml   full local stack (Cairn Core + Cairn control plane + demo private VPC)
core/                        Cairn Core images (core, web, setup, executor) and web UI build
helm/cairn/                  umbrella chart (Cairn Core + control plane)
helm/cairn-remote-agent/     chart installed inside each private network
tests/                       pytest suite (runs without a live Cairn Core)
```

## Quickstart (local)

```bash
cp .env.example .env                  # replace every CHANGE_ME (openssl rand -hex 32)
core/web/build.sh                     # web UI bundle (once per Cairn Core release; Node 22 + yarn)
deploy/build-images.sh                # cairn/{core,web,setup,executor,platform,consumer-ui,remote-agent,authorizer}
#   Docker Hub rate limits: BASE_IMAGE=mirror.gcr.io/library/python:3.12-slim \
#                           BUSYBOX_IMAGE=mirror.gcr.io/library/busybox:1.37 deploy/build-images.sh
#   TLS-intercepting proxy: EXTRA_CA_FILE=/path/ca.crt HTTPS_PROXY=... deploy/build-images.sh
docker compose -f docker-compose.cairn.yml up -d --no-build
# UI: http://localhost:9000, sign in as admin / CAIRN_ADMIN_PASSWORD  ·  Cairn API: http://localhost:8800/docs
# One-hour admin token for the curl examples below:
CAIRN_TOKEN=$(CAIRN_ADMIN_PASSWORD=... python3 deploy/bootstrap/mint_core_token.py \
  --frontend-url http://localhost:9002 --core-url http://localhost:8080 --print-admin-token)

# Create the Cairn structured properties / Certified tag / PII glossary terms
docker compose -f docker-compose.cairn.yml exec cairn-api cairn bootstrap

# Remote agent demo: create the "finance" pool, mint an enrolment token, put it in .env
# (CAIRN_AGENT_ENROLLMENT_TOKEN), restart the agent
curl -XPOST -H "Authorization: Bearer $CAIRN_TOKEN" -H 'Content-Type: application/json' \
  localhost:8800/agents/pools -d '{"name": "finance", "allowed_platforms": ["postgres"]}'
curl -XPOST -H "Authorization: Bearer $CAIRN_TOKEN" localhost:8800/agents/pools/finance/enrollment-tokens
docker compose -f docker-compose.cairn.yml up -d cairn-remote-agent
# Then create an ingestion source in the UI with executor id "finance".
```

**MCP (Claude Code / Claude Desktop):**

```bash
# stdio
CAIRN_MCP_TOKEN=<Cairn access token> CAIRN_CORE_URL=http://localhost:8080 cairn mcp
# or streamable HTTP at http://localhost:8830/mcp with header "Authorization: Bearer <Cairn access token>"
claude mcp add --transport http cairn http://localhost:8830/mcp --header "Authorization: Bearer $CAIRN_TOKEN"
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[server,ai,test]"
pytest                                   # 74 tests: detectors, engine, PDP, PII, workflows, agent protocol, AI, APIs
# live smoke suite against the running compose stack (8 scenarios)
tests/e2e/run.sh -v
cairn policies validate config/policies

cd plugins/java/cairn-authorizer && ./gradlew test pluginBundle
cd ui && npm install && npx tsc --noEmit && npx vitest run
```

## Verification status (v0.1)

| Check | Result |
|---|---|
| Python unit/integration tests (`pytest`) | 99 passed (v0.2, incl. module E) |
| Java plugin (`./gradlew test`) against the Cairn Core authorization API | 16 passed |
| UI (`tsc --noEmit`, `vitest`) | clean, 64 passed; MFE `remoteEntry.js` + ~162 kB gz first load; embed SDK 4 kB gz |
| Module E API contract: UI client calls vs. backend routes and response shapes | all routes and shapes match |
| `cairn/consumer-ui` image; MFE mounted in headless Chromium via `remoteEntry.js` → `mount()` | passed |
| `docker compose config`, `helm lint`, kubeconform (k8s 1.30) | passed |
| Images built (`deploy/build-images.sh`): control plane / agent / plugin | 145 MB / 144 MB / 2.3 MB compressed, non-root, no OS packages |
| Full compose stack (Cairn Core + Cairn control plane), 15 services | all healthy |
| Cairn Core loads `CairnAuthorizer` (Cairn Core runs Java 25; plugin built for 17) | passed |
| **Live e2e** (`tests/e2e`, 8 scenarios): PAT identity · monitor → native `VOLUME` assertion + run events → incident raised and auto-resolved · native policy import · PII propagated over column lineage via Kafka → cairn-executor → Cairn Core · SoD on certification · MCP streamable HTTP · **in-VPC agent ingesting a DB unreachable from the control plane, triggered by a native Cairn Core execution request** | 8 passed |
| Secret confinement: agent DB password absent from gateway/API logs, Cairn DB and Cairn Core (only `${FINANCE_PG_PASSWORD}`) | verified |
| **Live e2e, module E** (`tests/e2e/test_active_metadata_live.py`, 13 scenarios, real users with their own PATs): personas grant within scope, and purpose denies override them · PII masked per caller on the frontend → `cairn-core-proxy` → Cairn Core path (partial mask for the analyst, clear for the purpose-holding steward, no masking on direct service access) · PII terms and rule-based tags both propagate over column lineage via Kafka · bookmarks, follows, @mentions, replies, reactions and trending · marketplace request blocked by SoD, then approved by a **signed Slack button**, with a time-boxed grant and revoke · quality incident → follower inbox + webhook delivered by `cairn-notifier` · certification approved inline · context miner finds metric drift and joins, accepted join written as a native `erModelRelationship` · assistant (offline mode), lineage explainer and markdown docs · embed tokens scoped, origin-bound and revocable through the edge · notifier sweep · **workspace MFE rendered inside the Cairn Core UI** after a real login (Discover, Marketplace, asset page) | 13 passed, alongside the 8 platform scenarios: **21/21**, repeatable on a stack that already holds earlier runs' data |
| Not yet run live | Helm on a real cluster, authoritative mode, Claude/Ollama calls (the assistant ran in offline mode), real Slack/Teams/SMTP endpoints (the Slack protocol was simulated with valid signatures), embeds inside Tableau/Looker/Power BI |

## Third-party software

Cairn includes open-source components under their own licences (Apache 2.0, BSD, MIT and others).
Their copyright notices and licence texts are listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md),
which must ship with every distribution of Cairn.

## Security notes

- The control plane never acts as root. It runs as `urn:li:corpuser:cairn-service`, created at
  bootstrap with two non-editable policies (`deploy/bootstrap/mint_core_token.py` lists the
  privileges). It cannot manage policies, users, credentials, secrets or other users' tokens, delete
  entities or run system operations. `cairn-token-keeper` rotates its token every
  `CAIRN_TOKEN_ROTATE_DAYS` (default 7): the account mints its own successor and the previous
  token is revoked after `CAIRN_TOKEN_GRACE_MINUTES`. The admin password is used once, at bootstrap.
- The PDP fails closed. Authoritative mode (`CAIRN_NATIVE_POLICIES_ENABLED=false`) makes Cairn the only
  authorizer, so deny rules are enforced. Additive mode can only *grant*. This follows from Cairn Core's
  first-ALLOW-wins `AuthorizerChain`.
- Remote agents hold no Cairn Core credentials. The gateway rejects recipes with inline secrets, and filters
  every MCP against the pool's platform and aspect allowlist. Identity and policy entities are always
  blocked.
- AI features only retrieve what the caller may `VIEW_ENTITY_PAGE`. Free text is PII-redacted before it
  enters a prompt. Generated documentation is never written without an approving reviewer.
