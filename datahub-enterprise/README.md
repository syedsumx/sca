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
docker compose -f docker-compose.enterprise.yml up -d
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
dhe policies validate config/policies

cd plugins/java/enterprise-authorizer && ./gradlew test pluginBundle
cd ui && npm install && npx tsc --noEmit && npx vitest run
```

## Verification status (v0.1)

| Check | Result |
|---|---|
| Python unit/integration tests (`pytest`) | 74 passed |
| MCP streamable-HTTP round trip (auth middleware → tool runs as caller) | passed |
| Java plugin (`./gradlew test`) against `datahub-auth-api:1.5.0` | 16 passed |
| UI (`tsc --noEmit`, `vitest`) | clean, 35 passed |
| `docker compose config`, `helm lint`, kubeconform (k8s 1.30) | passed |
| Images built / stack started / live GMS round trip | **not yet:** see ROADMAP M5.1–5.2 |

## Security notes

- The PDP fails closed. Authoritative mode (`AUTH_POLICIES_ENABLED=false` on GMS) makes DHE the only
  authorizer, so deny rules are enforced. Additive mode can only *grant*. This follows from GMS's
  first-ALLOW-wins `AuthorizerChain`.
- Remote agents hold no GMS credentials. The gateway rejects recipes with inline secrets, and filters
  every MCP against the pool's platform and aspect allowlist. Identity and policy entities are always
  blocked.
- AI features only retrieve what the caller may `VIEW_ENTITY_PAGE`. Free text is PII-redacted before it
  enters a prompt. Generated documentation is never written without an approving reviewer.
