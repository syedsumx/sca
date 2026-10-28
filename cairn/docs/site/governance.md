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

Metadata changes take effect within seconds: when an owner, tag, term, domain, column label or group
membership changes, cached attributes and decisions for that asset or person are dropped at once, and
a changed catalog policy is re-imported immediately (`CAIRN_MCL_INVALIDATION`, on by default).

Two modes:

* **Additive** (default): Cairn's rules can grant access on top of the catalog's native policies.
  Good for a gradual rollout.
* **Authoritative**: Cairn is the only authority and its deny rules are enforced everywhere. Set
  `CAIRN_AUTHZ_MODE=authoritative` and `CAIRN_NATIVE_POLICIES_ENABLED=false`; the catalog's native
  policies are then imported into Cairn and evaluated alongside its own rules.

Masking applies to every read path: the UI, GraphQL and the REST API (`/openapi/v3` entities,
aspects and lists, and `/openapi/v2` timeseries).

## Personas and purposes

* A **persona** (for example *Finance Analysts*) bundles permissions for a group of people within a
  scope (domains, products, tags).
* A **purpose** (for example *GDPR personal data*) states why data may be used. Purpose denies take
  priority over persona grants.

Manage both in **Workspace → Governance studio**. Grants can be time-boxed; holders are warned before they
expire.

## PII and masking

Columns are classified (email, phone, national IDs and more) and classifications **propagate along
column lineage**, stopping at transformations that remove the sensitivity (hashing, aggregation,
counting, encryption, tokenisation, masking). Sample values and profiles of classified columns are
masked for people without a matching purpose; the asset page says when something is masked for you.

## Rule-based propagation

Tags and glossary terms can follow lineage by rule (for example: *gdpr-subject* flows to every
downstream table). Rules live in Workspace → Governance studio.

## Certification

Owners request certification; a steward who is not the requester approves it (separation of
duties). Certified assets show a badge with the approver and validity date. Approvals can be given
inline from Slack, Teams or email.

## Compliance forms

A compliance form asks the people responsible for a set of datasets for the metadata an audit
needs, and tracks who answered. Create one in **Governance studio → Compliance forms**:

* **Which datasets:** a classification, domain, platform or tag (all must hold). Through the API,
  any policy condition (`POST /cairn/api/governance/forms`).
* **Who answers:** the dataset's owners, optionally only some ownership types (data owners, say),
  with named people as a fallback when a dataset has no such owner.
* **Questions:** description, owners, glossary terms, tags, domain or a structured property, which
  are written to the catalog as the person answering; or free text and yes/no answers, which are
  kept in Cairn. Terms, tags and property values can be limited to a list of choices.
* **Sign-off:** a *Completion + sign-off* form also asks the assignee to confirm the answers.

**Assign…** previews how many datasets match and how many have nobody to answer, then creates one
task per dataset. Matching is re-checked every hour, so new datasets get tasks and datasets that no
longer match lose tasks nobody started. Assignees are notified (`form.assigned`) and find their
tasks under **Requests → Forms**, with current catalog values already filled in. Overdue tasks
trigger a daily reminder (`form.due`). People who can edit an asset may complete its task for the
assignee.

A dataset shows **Compliant** once every form assigned to it is complete (and signed off where the
form asks for it), and **Forms open** until then, on search results and asset pages.
**Progress** breaks each form down by domain and by assignee.
