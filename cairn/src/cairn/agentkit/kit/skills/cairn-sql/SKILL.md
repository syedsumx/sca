---
name: cairn-sql
description: "Write and run SQL on Cairn's governed data: the right tables, approved metric definitions, the connection's dialect, and results read as the person sees them. Use when asked a question the data answers, or to write, explain or fix a query."
---

# SQL on governed data in Cairn

Every statement runs as the person you work for: row filters and masks apply, and what comes back is what they may
see. Never try another route to data a statement was refused.

1. **Find the tables** (skill `cairn-find-data`): `semantic_search`, `get_asset` for the columns and their meaning.
2. **Use approved definitions.** `list_metrics` and `query_metric` when a metric exists for the number asked for;
   `get_definitions` for terms such as "active customer". Do not re-derive a definition that exists.
3. **Let Cairn write a first draft when it helps:** `ask_data` answers a question with SQL it shows;
   `how_was_this_answered` explains which tables and definitions an answer used.
4. **Check before running:** `ontology_validate_sql` for joins the ontology does not support; read-only statements
   only; a `LIMIT` on exploration; never `SELECT *` on wide tables with masked columns.
5. **Report** the statement, the result, and the caveats: masked columns, filters that applied, stale sources
   (`get_quality_status`).

Saved queries, dashboards, alerts and metrics are configuration: change them as code (skill `cairn-iac-change`).
