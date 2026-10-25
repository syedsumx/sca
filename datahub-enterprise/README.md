# DataHub Enterprise (DHE)

DHE is a self-hosted extension layer for open-source [DataHub](https://github.com/datahub-project/datahub). It adds
capabilities otherwise found in commercial DataHub Cloud, and it **requires no fork of DataHub Core**:

| Module | What you get |
|---|---|
| **A. Native Data Quality** | Freshness, volume, field-metric, distribution-drift, schema and custom-SQL monitors with learned baselines. Results appear as native assertions and incidents. |
| **B. Enterprise Governance** | An ABAC + RBAC Policy Decision Point with deny-overrides, a GMS authorizer plugin, PII classification with column-lineage propagation, and certification and approval workflows. |
| **C. In-VPC Remote Agents** | Outbound-only ingestion agents. They resolve secrets locally and stream metadata signed and end-to-end encrypted. |
| **D. AI Context & Discovery** | An MCP server, natural-language catalog Q&A, semantic vector search, and AI documentation that goes through human review. |

Read **[ARCHITECTURE_ENTERPRISE.md](ARCHITECTURE_ENTERPRISE.md)** for the analysis of DataHub internals and
the design, and **[ROADMAP.md](ROADMAP.md)** for milestones and status.

## Layout

```
src/datahub_enterprise/      Python services (one package, many entry points: `dhe …`)
  common/  quality/  governance/  agent/  ai/  services.py  app.py  cli.py
plugins/java/enterprise-authorizer/   GMS Authorizer plugin → PDP
ui/                          @dhe/web-react-modules (drop-in for datahub-web-react)
config/                      policy bundle, workflow templates, monitor + connection examples
deploy/  docker/             bootstrap scripts, actions pipelines, Dockerfiles, nginx edge
docker-compose.enterprise.yml   full local stack (DataHub core + DHE + demo private VPC)
helm/datahub-enterprise/     umbrella chart (depends on the official `datahub` chart)
helm/dhe-remote-agent/       chart installed inside each private network
tests/                       pytest suite (runs without a live DataHub)
```

## Quickstart (local)

```bash
cp .env.example .env                  # replace every CHANGE_ME (openssl rand -hex 32)
deploy/build-images.sh                # dhe/datahub-enterprise, dhe/remote-agent, dhe/enterprise-authorizer
#   Docker Hub rate limits: BASE_IMAGE=mirror.gcr.io/library/python:3.12-slim \
#                           BUSYBOX_IMAGE=mirror.gcr.io/library/busybox:1.37 deploy/build-images.sh
#   TLS-intercepting proxy: EXTRA_CA_FILE=/path/ca.crt HTTPS_PROXY=... deploy/build-images.sh
docker compose -f docker-compose.enterprise.yml up -d --no-build
# UI (DataHub + DHE behind one origin): http://localhost:9000   ·  DHE API: http://localhost:8800/docs

# Create the DHE structured properties / Certified tag / PII glossary terms
docker compose -f docker-compose.enterprise.yml exec dhe-api dhe bootstrap

# Remote agent demo: create an enrolment token for the "finance" pool, put it in .env, restart the agent
curl -XPOST -H "Authorization: Bearer $DATAHUB_PAT" localhost:8800/agents/pools/finance/enrollment-tokens
docker compose -f docker-compose.enterprise.yml up -d dhe-remote-agent
# Then create an ingestion source in the DataHub UI with executor id "finance".
```

**MCP (Claude Code / Claude Desktop):**

```bash
# stdio
DHE_MCP_TOKEN=<datahub PAT> DHE_GMS_URL=http://localhost:8080 dhe mcp
# or streamable HTTP at http://localhost:8830/mcp with header "Authorization: Bearer <datahub PAT>"
claude mcp add --transport http datahub-enterprise http://localhost:8830/mcp --header "Authorization: Bearer $DATAHUB_PAT"
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[server,ai,test]"
pytest                                   # 74 tests: detectors, engine, PDP, PII, workflows, agent protocol, AI, APIs
# live smoke suite against the running compose stack (8 scenarios)
DHE_E2E=1 DHE_E2E_TOKEN=$(docker compose -f docker-compose.enterprise.yml exec -T dhe-api cat /run/dhe/gms_token) \
  pytest tests/e2e -v
dhe policies validate config/policies

cd plugins/java/enterprise-authorizer && ./gradlew test pluginBundle
cd ui && npm install && npx tsc --noEmit && npx vitest run
```

## Verification status (v0.1)

| Check | Result |
|---|---|
| Python unit/integration tests (`pytest`) | 74 passed |
| Java plugin (`./gradlew test`) against `datahub-auth-api:1.5.0` | 16 passed |
| UI (`tsc --noEmit`, `vitest`) | clean, 35 passed |
| `docker compose config`, `helm lint`, kubeconform (k8s 1.30) | passed |
| Images built (`deploy/build-images.sh`): control plane / agent / plugin | 145 MB / 144 MB / 2.3 MB compressed, non-root, no OS packages |
| Full compose stack (DataHub **v1.8.0** core + DHE), 15 services | all healthy |
| GMS loads `EnterpriseAuthorizer` (GMS runs Java 25; plugin built for 17) | passed |
| **Live e2e** (`tests/e2e`, 8 scenarios): PAT identity · monitor → native `VOLUME` assertion + run events → incident raised and auto-resolved · native policy import · PII propagated over column lineage via Kafka → datahub-actions → GMS · SoD on certification · MCP streamable HTTP · **in-VPC agent ingesting a DB unreachable from the control plane, triggered by a native DataHub execution request** | 8 passed |
| Secret confinement: agent DB password absent from gateway/API logs, DHE DB and GMS (only `${FINANCE_PG_PASSWORD}`) | verified |
| Not yet run live | Helm on a real cluster, authoritative mode, Claude/Ollama calls, UI modules inside datahub-web-react |

## Security notes

- The PDP fails closed. Authoritative mode (`AUTH_POLICIES_ENABLED=false` on GMS) makes DHE the only
  authorizer, so deny rules are enforced. Additive mode can only *grant*. This follows from GMS's
  first-ALLOW-wins `AuthorizerChain`.
- Remote agents hold no GMS credentials. The gateway rejects recipes with inline secrets, and filters
  every MCP against the pool's platform and aspect allowlist. Identity and policy entities are always
  blocked.
- AI features only retrieve what the caller may `VIEW_ENTITY_PAGE`. Free text is PII-redacted before it
  enters a prompt. Generated documentation is never written without an approving reviewer.
