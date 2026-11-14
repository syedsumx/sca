---
name: cairn-incident-triage
description: "Triage a data incident in Cairn: a failed pipeline run, a stale or wrong table, a breached service level. Find where it started, what it affects and who must know. Use when something is broken, late or wrong."
---

# Triage a data incident in Cairn

Read first; propose fixes, never apply them unasked.

1. **What is wrong, since when.** `get_quality_status` on the dataset; for a flow, `list_flows` then `get_flow_run`
   for the failed run, its failed step and its log.
2. **Where it started.** Walk `get_lineage` upstream and check each source's `get_quality_status`: the first
   unhealthy one is where to look. `get_history` on it for a change made just before.
3. **What it affects.** `impact_analysis` from the first unhealthy asset; owners of certified downstream assets
   (`get_asset`).
4. **Operations view** (toolset `operations`, when this connection lists it): open incidents, alerts and the
   pipeline's recent runs; `api_search "incidents"` finds the operations otherwise.
5. **Report**: the likely cause with evidence, what is affected, who to tell, and the fix to propose. A rerun is
   `run_flow` and only with the person's say-so; a change to the pipeline goes through skill `cairn-iac-change`.
   Never edit data to make a check pass.
