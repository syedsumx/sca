# SOC 2 readiness

> Internal. This is a map from the SOC 2 Trust Services Criteria (2017, revised points of focus 2022)
> to what Cairn already does and what the company must still put in place. A SOC 2 Type II report
> is an auditor's opinion on controls operating over a period (usually 3 to 12 months). Most of the
> work is policies, people and evidence, not code. Get an audit firm to scope it before you start
> the observation window.

## Scope

**In scope:**

- **For a hosted offering:** the hosting platform (one Kubernetes release per tenant, see `hosting-runbook.md`) and the people who run it.
- **For self-hosted customers:** the development and release process, which they ask about too.

**Criteria:** Security (the common criteria, CC1 to CC9) is required. Availability (A1) and Confidentiality (C1) are the usual additions for a data catalog. Processing Integrity and Privacy are seldom asked for here.

## Evidence the product produces

Run `deploy/compliance/collect-evidence.sh` on a schedule, monthly and for each audit request, against each deployment. It writes one dated folder with checksums and never collects secrets.

| File | What it shows |
|---|---|
| `audit-verify.json`, `audit-log.jsonl` | the hash-chained audit log is intact; every access decision, policy, grant, provisioning and admin change in the period |
| `access-policies.json`, `access-personas.json`, `access-purposes.json` | who may do what: the input to quarterly access reviews |
| `access-extensions.json` | every installed extension and exactly what its token may do |
| `licence.json`, `ai-systems-inventory.json` | licence state; the AI system inventory and its approvals |
| `schema-revision.json`, `images.json`, `source-revision.txt` | which code and schema are running (change management) |
| `deployment-config.yaml` | the running configuration, secrets redacted |
| `backups.jsonl`, `alert-rules.yaml`, `services.json` | backups taken, monitoring in place, services healthy |

## Control map

Each criterion below is summarised. **Cairn** lists what the product or its tooling already provides, and **Company** lists what the organisation has to own.

### CC1 Control environment: integrity, oversight, structure, competence, accountability

- **Cairn:** none; this is about the company.
- **Company:**
  - a code of conduct, signed on hire and every year;
  - an org chart with security responsibilities, and a named security owner;
  - background checks where the law allows;
  - security awareness training on hire and every year, with completion records;
  - performance reviews that include security duties.

### CC2 Communication and information

- **Cairn:**
  - the product documentation (`/docs`);
  - this map;
  - `security.txt` and a vulnerability disclosure page, to add for the hosted service.
- **Company:**
  - a published security page and customer notifications for changes and incidents;
  - an internal channel for reporting security concerns;
  - system descriptions kept current: architecture, data flows, subprocessors.

### CC3 Risk assessment

- **Cairn:** the gap analysis and the threat notes in `docs/site/agent-security.md`.
- **Company:**
  - a yearly risk assessment, with a risk register (likelihood, impact, owner, treatment);
  - fraud risk considered;
  - a reassessment on major changes, such as hosting or a new subprocessor.

### CC4 Monitoring activities

- **Cairn:**
  - Prometheus metrics, 14 alert rules and a Grafana dashboard (`deploy/observability`);
  - `cairn audit verify`;
  - the evidence script, run on a schedule.
- **Company:**
  - a quarterly control self-check, with deficiencies tracked to closure;
  - a yearly penetration test of the hosted service.

### CC5 Control activities and policies

- **Cairn:** controls are enforced in code: policy decisions, purposes and masking, least-privilege service accounts.
- **Company:** written policies, approved and reviewed every year, covering:
  - information security,
  - access control,
  - change management,
  - incident response,
  - business continuity,
  - vendor management,
  - data retention and disposal,
  - acceptable use,
  - encryption,
  - secure development.

### CC6 Logical and physical access

| Point of focus | Cairn | Company |
|---|---|---|
| Authentication | SSO (OIDC/SAML) for people; signed, expiring API tokens; extension tokens scoped to a manifest | MFA enforced at the identity provider for staff and admin accounts |
| Provisioning and deprovisioning | SCIM keeps users and groups in step with the identity provider; deprovisioning works even when Cairn is read-only | joiner/mover/leaver process with tickets; access removed within one business day of leaving |
| Least privilege | the control-plane account holds only the privileges it needs; personas, purposes and ABAC policies; masking of classified columns | **quarterly access reviews** using `access-*.json`, signed off and retained |
| Credentials | the service token rotates on its own (token keeper); extension tokens and signing secrets rotate with `cairn extension rotate`; secrets can be mounted as files | a secrets manager; no shared accounts; rotation on staff departure |
| Network | TLS at the edge with HSTS; network policies that admit only a tenant's own pods; webhook egress refuses loopback and cloud metadata addresses | cloud network segmentation, a WAF for the hosted service |
| Data at rest | — | encrypted volumes and managed database encryption (cloud provider); encrypted backups |
| Physical | — | inherited from the cloud provider's SOC 2 report (carve-out); keep their report on file |

### CC7 System operations: detection, response, recovery

- **Cairn:**
  - alerts for availability, errors, slow policy decisions and spikes in denied access;
  - the audit log;
  - the licence and AI-budget alerts;
  - `hosting-runbook.md` for alert response;
  - backup and restore scripts, rehearsed.
- **Company:**
  - an incident response plan with severity levels, roles, customer communication and post-incident reviews;
  - an on-call rota;
  - vulnerability scanning of images (CI already runs Trivy for criticals) with patch deadlines by severity (for example critical within 7 days);
  - log retention of at least one year.

### CC8 Change management

- **Cairn:**
  - every change goes through CI: unit tests, UI tests, Java tests, Helm lint, the leak scan, image scans and the full live e2e suite;
  - migrations are forward-only and checked (`cairn db check`);
  - the upgrade playbook and `deploy/ops/rehearse-upgrade.sh`;
  - the engine-release test matrix (`core/UPSTREAM.md`).
- **Company:**
  - protected main branch with required review by someone other than the author (the auditor samples merged pull requests for this);
  - a separate production deploy approval;
  - an emergency change procedure;
  - a release record linking each deployment to its source revision (`source-revision.txt`, `images.json`).

### CC9 Risk mitigation: business disruption and vendors

- **Cairn:** backups, restore and the high-availability profile (see A1).
- **Company:**
  - a vendor inventory with risk tiers, and a yearly review of critical vendors (cloud provider, AI model provider, email, monitoring), keeping their SOC 2 reports;
  - data processing agreements;
  - cyber insurance, if required.

### A1 Availability

- **Cairn:**
  - `values-ha.yaml`: replicas spread over zones, PodDisruptionBudgets, and leader-locked workers;
  - published load limits (`tests/load/limits.yaml`);
  - `deploy/ops/backup.sh` and `restore.sh`, with a verified round trip;
  - recovery steps in the hosting runbook.
- **Company:**
  - capacity monitoring;
  - RPO and RTO targets per tenant tier, stated in contracts;
  - a **restore test at least once a year, with a record of the result**;
  - a business continuity plan and a disaster recovery test.

### C1 Confidentiality

- **Cairn:**
  - classification-driven masking;
  - purposes that restrict sensitive data;
  - the AI-system rule (a high-risk system may not use restricted data without an approved purpose);
  - tenant isolation: separate databases, roles, index prefixes, Kafka and namespaces;
  - secrets redacted from evidence.
- **Company:**
  - a data classification policy;
  - retention schedules;
  - **secure deletion at offboarding, with a certificate to the customer** (see the hosting runbook).

## Getting to the report

| Step | When | Output |
|---|---|---|
| 1. Pick an audit firm and a compliance platform (optional) | month 0 | engagement letter, scope |
| 2. Write and approve the policies; train staff | months 0–2 | policy set, training records |
| 3. Gap assessment or SOC 2 Type I (design at a point in time) | month 2–3 | Type I report, or a list of gaps |
| 4. Close gaps; schedule the evidence script; start access reviews | months 2–3 | first evidence folders, review sign-offs |
| 5. Observation window (operating effectiveness) | 3–6 months, then yearly | evidence for every control, every period |
| 6. SOC 2 Type II fieldwork and report | after the window | Type II report to share under NDA |

The most common reasons a first Type II report has exceptions:

- access reviews that weren't done on time;
- changes merged without review;
- leavers whose access was removed late;
- no restore test on record;
- vendor reviews missing.
