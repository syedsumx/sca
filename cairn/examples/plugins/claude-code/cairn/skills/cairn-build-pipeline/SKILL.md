---
name: cairn-build-pipeline
description: "Build a governed data pipeline in Cairn end to end, as code: ingestion, transformation, quality checks, schedule, proposed for approval. Use when asked to load, move, transform or refresh data in Cairn."
---

# Build a pipeline in Cairn

Work through Cairn's MCP server (`cairn`), as the person you work for. Everything is proposed as code and a person
approves it; you never apply a change yourself.

1. **Find the source and the target.** `semantic_search` or `search_assets`; `get_asset` for schemas and owners;
   `get_lineage` for what already feeds the target. If the target table does not exist, say so and include it.
2. **Choose the pieces** (`api_search` finds each one's operation; `iac_schema` describes each kind):
   - ingestion: a mirror (databases, change data capture), a pipe (files, streams), a SaaS sync, or a NiFi flow;
   - transformation: a derived table (SQL) or a flow step;
   - orchestration: a flow (`kind: flow`) with its schedule or dataset trigger;
   - checks: at least freshness and not-null on the key columns, as quality checks the flow runs.
3. **Write one bundle** with all of it. Secrets are `${NAME}` references to secret scopes, never values.
4. **Plan** with `iac_plan` and read the plan back: every resource created, its fields.
5. **Propose** with `iac_propose` (a title, the intent in one sentence) and show the person the plan and the
   change request's link. Stop there.
6. After a person approves and applies it: run the flow (`run_flow`), then report the run, the rows in the target
   and the checks' results (`get_quality_status`).

Never weaken a policy or a mask to make something work: say what is refused and who can grant it.
