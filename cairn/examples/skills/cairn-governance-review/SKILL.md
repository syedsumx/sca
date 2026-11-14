---
name: cairn-governance-review
description: "Review governance in Cairn: unlabelled personal data, missing owners and descriptions, who can access what, and access requests. Use for PII sweeps, audits and access questions."
---

# Governance review in Cairn

- **Personal data.** Find columns that hold personal data without a label (`search_assets`, `get_asset`); propose
  tags, descriptions and owners (`suggest_tags`, `suggest_description`, `suggest_owner`). Proposals wait for review;
  never copy personal values into them.
- **Access.** `check_access` before anything else; to get access, `request_access` with a clear purpose. Never
  suggest a way around a refusal, a mask or a row filter.
- **Policies** are read, not edited, here: changes to policies go through `iac_propose` and their approvers.
- Summarise what you proposed and where the reviews wait.
