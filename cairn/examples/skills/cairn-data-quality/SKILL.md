---
name: cairn-data-quality
description: "Investigate and fix data quality in Cairn: why a dataset fails its checks, what it breaks, and new checks as code. Use when a dataset is stale, wrong, failing or untrusted."
---

# Data quality in Cairn

1. **What is failing.** `get_quality_status` on the dataset: which checks, since when, by how much.
2. **Where it comes from.** `get_lineage` upstream; `get_quality_status` on each upstream dataset; the runs of the
   pipelines that feed it (`api_search "pipeline runs"`).
3. **What it breaks.** `impact_analysis`; owners of certified downstream assets (`get_asset`).
4. **Report** the likely causes with evidence, most likely first, and whom to tell.
5. **Prevent it next time**: propose new or tighter checks as code (`iac_schema` for quality checks, `iac_plan`,
   `iac_propose`). Never edit data to make a check pass.
