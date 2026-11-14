---
name: cairn-iac-change
description: "Change Cairn's configuration safely as code: export what exists, edit, plan, propose for approval. Use for any change to flows, checks, policies, connections, dashboards, MCP servers or agents."
---

# Change Cairn as code

1. `iac_schema` for the kinds involved and `iac_export` for what exists now (YAML).
2. Edit the bundle: keep names stable (a rename is a replace), secrets as `${NAME}` references.
3. `iac_plan` and read every change back to the person: created, updated (field by field), replaced, deleted.
4. `iac_propose` with a title and the intent; for teams that review in git, `iac_open_pr` to a git folder.
5. Stop. A person approves and applies it.
