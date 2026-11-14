---
name: cairn-dev
description: How to build, change and test Cairn, the governed active-metadata platform under cairn/. Use for any work in this repository's cairn/ folder - new capabilities, connectors, entity types, policies, UI pages, IaC kinds, migrations, CI failures, releases - so the change is anchored on Cairn Core (the metadata graph) and meets the project's testing and documentation rules.
---

# Working on Cairn

Read `AGENTS.md` at the repository root first: it has the layout, the build and test commands and the
conventions. This skill adds the product anchor and step-by-step recipes. Where they differ, AGENTS.md wins.

## The anchor: everything revolves around Cairn Core

Cairn Core is the metadata graph (the white-labelled DataHub engine under `cairn/core/`): data assets, the
catalog, search, lineage, glossary, domains, data products, data contracts, ownership and documentation.
Every capability sits in one of four rings around it (ROADMAP.md, *Cairn Core at the centre*):

| Ring | Uses the graph by | Examples |
|---|---|---|
| Core | being it | catalog, assets, lineage, glossary, domains, data products, contracts, marketplace |
| Govern | deciding on it | policy decision point, personas and purposes, masking, people and roles, reviews |
| Activate | keeping it alive | connectors and agents, quality, automations, observability, AI context, Slack and Teams |
| Extend | running work that registers back into it | lakehouse, Trino, Spark, dbt, BI, flows, managed Airflow and Postgres, ML, agents |

Before building anything, write down in the ROADMAP row:

1. **Ring.** Which ring it is in.
2. **Reads.** What it reads from the graph (assets, owners, terms, lineage, policies).
3. **Writes back.** What it writes back: its objects as Core entities with owners and domain; lineage
   (OpenLineage or Core edges, column level where possible); quality, usage and run results as aspects.
4. **Governance.** Which policy decisions govern it (privileges on which resource URNs), and that it fails closed.
5. **Visibility.** Where it shows in the Catalog: asset page, data product, domain, lineage.

A feature that keeps its objects outside the graph is not done.

## Recipes

### A new capability (any ring)
1. Package under `cairn/src/cairn/<area>/`. Wire it in `services.py` after its dependencies, and add its router in `app.py`.
2. New tables: add a migration `NNNN_*.py` (next number after the latest one in `src/cairn/migrations/versions/`),
   then register the table in `src/cairn/common/db.py`.
3. Configurable over the API (create and delete): add a `Kind` in `src/cairn/iac/kinds.py`, then run
   `python -m cairn.iac.terraform` and `python -m cairn.iac.forms` to regenerate.
4. Privileges: add a new privilege to the policy engine. Deny by default, and refuse when the decision cannot be made.
5. Graph: register the capability's objects and runs in Cairn Core (entity, owners, domain, lineage) and link
   them from the asset page.
6. UI: the page goes in the section of its ring in `ui/src/consumer/components/Layout.tsx` (NAV_GROUPS). Keep
   `hostNav.ts` and `deploy/mfe/mfe.config.yaml` in step with it. Use the theme tokens (`components/tokens.ts`) and
   `StatusBadge`, put strings in `i18n/`, and keep controls at least 44 px tall.
7. Tests against real components (below), then docs: `docs/site/administration.md` for operators, the ROADMAP row
   (✅ only when every part of its definition of done is tested, otherwise 🟡 with exactly what remains), then
   `node scripts/capabilities.mjs` and `node scripts/build-help.mjs` in `ui/`.

### A connector (Activate)
- Put it under `src/cairn/sources/` or as an ingestion recipe for Cairn Core. Read secrets only from secret
  scopes; a remote agent resolves them inside the customer's network.
- Emit datasets, schemas, owners and lineage to Core, and test against the real system or its official emulator.
  If there is no account, say so in the row instead of pretending.

### An entity type or aspect in Cairn Core (Core)
- Prefer Core's existing entity types: dataset, dataJob, dataFlow, dashboard, chart, mlModel, dataProduct,
  glossaryTerm, domain, and so on. Add a custom aspect only when nothing fits, and document it in
  `ARCHITECTURE.md`.
- Write through the existing emitters and clients. Never write to Core's storage directly.

### An engine or runtime (Extend)
- It must authorise through the policy decision point: Trino through the OPA protocol, the catalog through
  vended credentials, others through the plugin or proxy pattern. Masks and row filters apply wherever data
  is read; otherwise the engine is refused.
- Its runs emit OpenLineage to Cairn, so the graph shows what each run read and wrote.
- Use short-lived, audience-bound tokens and workload identity. Never store a password or key in the engine.

### A CI failure
1. Read the failing job's log: list the jobs, then fetch the failed job's log tail. The `stack` job needs only
   `python`, `ui` and `java`, so it is skipped when one of those three fails (other jobs' failures do not skip it).
2. Reproduce locally with the same test and real components, find the root cause and fix it. Never skip a
   test. Flaky is not a root cause: find the race.
3. `.github/workflows/cairn-ci.yml` cancels runs in progress. Pushing while a long run is still useful loses
   its result, so hold the push unless that run is already red.

## Rules that bite
- Fail closed. Secrets never go on command lines or in logs. Every change commits with its audit entry
  (`audit.record(..., conn=c)` inside the same transaction).
- Test against real binaries and images, never mocks: `CAIRN_STORE_BIN`, `CAIRN_CATALOG_BIN`, and Docker for Trino,
  Kafka, PostgreSQL, Airflow and so on.
- No model names or identifiers in code, docs or commits. Commit messages say what changed and why.
- Work on `claude/datahub-enterprise-extension`. Push with `git push -u origin <branch>`, and don't open pull
  requests unless asked.
