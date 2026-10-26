# Aegis

Aegis is a self-hosted extension layer for open-source [DataHub](https://github.com/datahub-project/datahub). It adds
capabilities otherwise found in commercial DataHub Cloud, and it **requires no fork of DataHub Core**:

| Module | What you get |
|---|---|
| **A. Native Data Quality** | Freshness, volume, field-metric, distribution-drift, schema and custom-SQL monitors with learned baselines. Results appear as native assertions and incidents. |
| **B. Enterprise Governance** | An ABAC + RBAC Policy Decision Point with deny-overrides, a GMS authorizer plugin, PII classification with column-lineage propagation, and certification and approval workflows. |
| **C. In-VPC Remote Agents** | Outbound-only ingestion agents. They resolve secrets locally and stream metadata signed and end-to-end encrypted. |
| **D. AI Context & Discovery** | An MCP server, natural-language catalog Q&A, semantic vector search, and AI documentation that goes through human review. |
| **E. Active Metadata & AI Context Layer** | A consumer workspace micro-frontend: Discover, faceted search, trending assets, discussions, bookmarks, and a data marketplace where access is requested without tickets. It adds Personas & Purposes, with purpose denies overriding persona grants, and PII masking in the `aegis-gms-proxy`. Rule-based tag and term propagation runs over column lineage. Context agents mine queries, dbt, BI and job runs for definitions, drift and ontologies. The layer also includes an assistant, plain-English lineage-anomaly explanations, and Slack/Teams actions. Trust components can be embedded in Tableau, Looker, Power BI and portals. |

Read **[ARCHITECTURE.md](ARCHITECTURE.md)** for the analysis of DataHub internals and
the design, **[UX_ACTIVE_METADATA_SPEC.md](UX_ACTIVE_METADATA_SPEC.md)** for the consumer UX and
active-metadata blueprint (module E), and **[ROADMAP.md](ROADMAP.md)** for milestones and status.
Deployment of module E is covered in [deploy/README-active-metadata.md](deploy/README-active-metadata.md).

## Layout

```
src/aegis/      Python services (one package, many entry points: `aegis …`)
  common/  quality/  governance/  agent/  ai/  services.py  app.py  cli.py
  collab/  marketplace/  context/  notify/  embed/  gmsproxy/      (module E)
plugins/java/aegis-authorizer/   GMS Authorizer plugin → PDP
ui/                          @aegis/web-react-modules + consumer workspace MFE (src/consumer) + embed SDK (src/embed)
embeds/                      Tableau / Looker / Power BI / portal packaging for the embed components
config/                      policy bundle, workflow templates, monitor + connection examples
deploy/  docker/             bootstrap scripts, actions pipelines, Dockerfiles, nginx edge
docker-compose.aegis.yml   full local stack (DataHub core + Aegis + demo private VPC)
helm/aegis/     umbrella chart (depends on the official `datahub` chart)
helm/aegis-remote-agent/       chart installed inside each private network
tests/                       pytest suite (runs without a live DataHub)
```

## Quickstart (local)

```bash
cp .env.example .env                  # replace every CHANGE_ME (openssl rand -hex 32)
deploy/build-images.sh                # aegis/platform, aegis/remote-agent, aegis/authorizer
#   Docker Hub rate limits: BASE_IMAGE=mirror.gcr.io/library/python:3.12-slim \
#                           BUSYBOX_IMAGE=mirror.gcr.io/library/busybox:1.37 deploy/build-images.sh
#   TLS-intercepting proxy: EXTRA_CA_FILE=/path/ca.crt HTTPS_PROXY=... deploy/build-images.sh
docker compose -f docker-compose.aegis.yml up -d --no-build
# UI (DataHub + Aegis behind one origin): http://localhost:9000   ·  Aegis API: http://localhost:8800/docs

# Create the Aegis structured properties / Certified tag / PII glossary terms
docker compose -f docker-compose.aegis.yml exec aegis-api aegis bootstrap

# Remote agent demo: create the "finance" pool, mint an enrolment token, put it in .env
# (AEGIS_AGENT_ENROLLMENT_TOKEN), restart the agent
curl -XPOST -H "Authorization: Bearer $DATAHUB_PAT" -H 'Content-Type: application/json' \
  localhost:8800/agents/pools -d '{"name": "finance", "allowed_platforms": ["postgres"]}'
curl -XPOST -H "Authorization: Bearer $DATAHUB_PAT" localhost:8800/agents/pools/finance/enrollment-tokens
docker compose -f docker-compose.aegis.yml up -d aegis-remote-agent
# Then create an ingestion source in the DataHub UI with executor id "finance".
```

**MCP (Claude Code / Claude Desktop):**

```bash
# stdio
AEGIS_MCP_TOKEN=<datahub PAT> AEGIS_GMS_URL=http://localhost:8080 aegis mcp
# or streamable HTTP at http://localhost:8830/mcp with header "Authorization: Bearer <datahub PAT>"
claude mcp add --transport http aegis http://localhost:8830/mcp --header "Authorization: Bearer $DATAHUB_PAT"
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[server,ai,test]"
pytest                                   # 74 tests: detectors, engine, PDP, PII, workflows, agent protocol, AI, APIs
# live smoke suite against the running compose stack (8 scenarios)
AEGIS_E2E=1 AEGIS_E2E_TOKEN=$(docker compose -f docker-compose.aegis.yml exec -T aegis-api cat /run/aegis/gms_token) \
  pytest tests/e2e -v
aegis policies validate config/policies

cd plugins/java/aegis-authorizer && ./gradlew test pluginBundle
cd ui && npm install && npx tsc --noEmit && npx vitest run
```

## Verification status (v0.1)

| Check | Result |
|---|---|
| Python unit/integration tests (`pytest`) | 99 passed (v0.2, incl. module E) |
| Java plugin (`./gradlew test`) against `datahub-auth-api:1.5.0` | 16 passed |
| UI (`tsc --noEmit`, `vitest`) | clean, 64 passed; MFE `remoteEntry.js` + ~162 kB gz first load; embed SDK 4 kB gz |
| Module E API contract: UI client calls vs. backend routes and response shapes | all routes and shapes match |
| `aegis/consumer-ui` image; MFE mounted in headless Chromium via `remoteEntry.js` → `mount()` | passed |
| `docker compose config`, `helm lint`, kubeconform (k8s 1.30) | passed |
| Images built (`deploy/build-images.sh`): control plane / agent / plugin | 145 MB / 144 MB / 2.3 MB compressed, non-root, no OS packages |
| Full compose stack (DataHub **v1.8.0** core + Aegis), 15 services | all healthy |
| GMS loads `AegisAuthorizer` (GMS runs Java 25; plugin built for 17) | passed |
| **Live e2e** (`tests/e2e`, 8 scenarios): PAT identity · monitor → native `VOLUME` assertion + run events → incident raised and auto-resolved · native policy import · PII propagated over column lineage via Kafka → datahub-actions → GMS · SoD on certification · MCP streamable HTTP · **in-VPC agent ingesting a DB unreachable from the control plane, triggered by a native DataHub execution request** | 8 passed |
| Secret confinement: agent DB password absent from gateway/API logs, Aegis DB and GMS (only `${FINANCE_PG_PASSWORD}`) | verified |
| **Live e2e, module E** (`tests/e2e/test_active_metadata_live.py`, 13 scenarios, real users with their own PATs): personas grant within scope, and purpose denies override them · PII masked per caller on the frontend → `aegis-gms-proxy` → GMS path (partial mask for the analyst, clear for the purpose-holding steward, no masking on direct service access) · PII terms and rule-based tags both propagate over column lineage via Kafka · bookmarks, follows, @mentions, replies, reactions and trending · marketplace request blocked by SoD, then approved by a **signed Slack button**, with a time-boxed grant and revoke · quality incident → follower inbox + webhook delivered by `aegis-notifier` · certification approved inline · context miner finds metric drift and joins, accepted join written as a native `erModelRelationship` · assistant (offline mode), lineage explainer and markdown docs · embed tokens scoped, origin-bound and revocable through the edge · notifier sweep · **workspace MFE rendered inside the DataHub UI** after a real login (Discover, Marketplace, asset page) | 13 passed, alongside the 8 platform scenarios: **21/21** on DataHub v1.8.0, repeatable on a stack that already holds earlier runs' data |
| Not yet run live | Helm on a real cluster, authoritative mode, Claude/Ollama calls (the assistant ran in offline mode), real Slack/Teams/SMTP endpoints (the Slack protocol was simulated with valid signatures), embeds inside Tableau/Looker/Power BI |

## Name, trademarks and third-party software

Aegis is an independent product built on the open-source DataHub project. It is not affiliated with
or endorsed by the DataHub project or its maintainers. "DataHub" is a trademark of its owners and is
used here only to describe what Aegis runs on.

DataHub and the other platform components (OpenSearch, Kafka, MySQL or PostgreSQL) run as their
upstream, unmodified images under their own licences; Aegis does not redistribute their source. If
you ship DataHub's images or source with Aegis, include DataHub's `LICENSE` and `NOTICE` (Apache 2.0).

Branding: the DataHub shell shows the Aegis logo, favicon and title through GMS's
`REACT_APP_LOGO_URL` / `REACT_APP_FAVICON_URL` / `REACT_APP_TITLE` (set in compose and Helm). Text
and images that DataHub hard-codes (the "DataHub Core" wordmark in the nav header, "Welcome to
DataHub" on the login page) need a fork of `datahub-web-react` to change.

## Security notes

- The PDP fails closed. Authoritative mode (`AUTH_POLICIES_ENABLED=false` on GMS) makes Aegis the only
  authorizer, so deny rules are enforced. Additive mode can only *grant*. This follows from GMS's
  first-ALLOW-wins `AuthorizerChain`.
- Remote agents hold no GMS credentials. The gateway rejects recipes with inline secrets, and filters
  every MCP against the pool's platform and aspect allowlist. Identity and policy entities are always
  blocked.
- AI features only retrieve what the caller may `VIEW_ENTITY_PAGE`. Free text is PII-redacted before it
  enters a prompt. Generated documentation is never written without an approving reviewer.
