---
name: cairn-impact-review
description: "Review what a change to a dataset, pipeline or definition breaks downstream in Cairn, and whom to tell, before the change is made. Use before renaming, dropping, retyping or redefining anything others depend on, and when reviewing a pull request that does."
---

# Impact review in Cairn

Change nothing here: this skill reads and reports.

1. **Name the change precisely:** which asset, which columns, what happens to them (renamed, dropped, retyped,
   redefined, rescheduled).
2. **Downstream.** `impact_analysis` on the asset; `get_lineage` for the column-level paths. Group what is affected:
   tables, dashboards, metrics, flows, data products, shares.
3. **Who relies on it.** `get_asset` for the owners of each certified or widely used downstream asset;
   `get_quality_status` for contracts and checks that would start failing.
4. **History.** `get_history` on the asset: recent changes that already moved it, and who made them.
5. **Report**, most serious first: what breaks, what merely changes, what is unaffected, the owners to tell, and a
   safer order of steps (add the new column, move readers, then remove the old one). Offer `start_discussion` on the
   asset so the owners see it.
