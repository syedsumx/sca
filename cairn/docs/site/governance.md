---
title: Governance
order: 3
---
# Governance

## Policy decisions

Every access decision in Cairn goes to the **policy decision point**, which combines role-based
rules (who you are) with attribute-based rules (what the data is: domain, tags, classification,
certification) using **deny-overrides**: a single matching deny wins. Decisions are cached briefly
and every decision is written to a tamper-evident audit log (`GET /cairn/api/governance/audit`).

Two modes:

* **Additive** (default): Cairn's rules can grant access on top of the catalog's native policies.
  Good for a gradual rollout.
* **Authoritative**: Cairn is the only authority and its deny rules are enforced everywhere. Set
  `CAIRN_NATIVE_POLICIES_ENABLED=false`.

## Personas and purposes

* A **persona** (for example *Finance Analysts*) bundles permissions for a group of people within a
  scope (domains, products, tags).
* A **purpose** (for example *GDPR personal data*) states why data may be used. Purpose denies take
  priority over persona grants.

Manage both in **Workspace → Governance**. Grants can be time-boxed; holders are warned before they
expire.

## PII and masking

Columns are classified (email, phone, national IDs and more) and classifications **propagate along
column lineage**, stopping at transformations that remove the sensitivity (hashing, aggregation,
counting, encryption, tokenisation, masking). Sample values and profiles of classified columns are
masked for people without a matching purpose; the asset page says when something is masked for you.

## Rule-based propagation

Tags and glossary terms can follow lineage by rule (for example: *gdpr-subject* flows to every
downstream table). Rules live in Workspace → Governance.

## Certification

Owners request certification; a steward who is not the requester approves it (separation of
duties). Certified assets show a badge with the approver and validity date. Approvals can be given
inline from Slack, Teams or email.
