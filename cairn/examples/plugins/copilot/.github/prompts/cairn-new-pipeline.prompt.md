---
agent: agent
description: "Build a governed pipeline from a source into the lakehouse, as code"
tools: ['cairn/*']
---

Build a pipeline in Cairn from ${input:source:the source (a database table, topic or API)} into the lakehouse table ${input:target:namespace.table}, refreshed ${input:schedule:how often}. Find the source and target, design the ingestion and any transformation as a flow, add a freshness and a not-null quality check on the key columns, plan it as a bundle and propose it for approval. Show the plan.
