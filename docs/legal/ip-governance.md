# Intellectual property governance: framework and open items

**Status:** a compliance framework for counsel and the board. It is **not legal or tax advice**. Counsel must review every statement and placeholder before anyone relies on it, including the investors who receive this document.
**Date:** 2026-11-14. **Basis:** the repository at commit `783865d7` (branch `claude/funny-meitner-qtz0ux`).
**Related:** [`oss-license-inventory.md`](oss-license-inventory.md) (the third-party inventory this document relies on).

## 1. Purpose and scope

This document sets out how the company should establish, record and maintain its rights in the Cairn code, and its obligations to upstream projects and third parties. It separates three things:

- **Facts** the repository shows, with the command or file that shows them.
- **Framework**: the controls the company should operate, stated as proposals.
- **Open items**: decisions and documents only the company or counsel can supply, marked `[TO CONFIRM: ...]`.

Nothing here states that any agreement has been signed, any assignment made, or any licence cleared.

## 2. Facts from the repository

| Fact | Value | How it was established |
|---|---|---|
| Commits on the branch | 788, from 2026-01-24 to 2026-11-14 | `git log` |
| Distinct contributors recorded in git history | 2 (names and addresses are not reproduced in this document) | `git log --format='%an'`, counted |
| Commits with a co-author trailer naming an AI system | 477 | `git log --grep="Co-Authored-By"` |
| Commits with a Developer Certificate of Origin sign-off (`Signed-off-by`) | 0 | `git log --format='%B'` |
| Licence file for Cairn itself | none at the repository root or in `cairn/` | file search |
| Licence files in the repository | `cairn/ontology/LICENSE` (Apache-2.0, with an NVIDIA copyright line) and `cairn/ontology/NOTICE` | file search |
| Product licence as declared | Apache-2.0, in `cairn/pyproject.toml`, the `cli`, `airflow-provider`, `lakehouse-client` and `spark` packages, `cairn/ui/package.json`, `cairn/desktop/package.json` and `cairn/sdks/js/package.json` | manifests |
| Third-party notices | `cairn/THIRD_PARTY_NOTICES.md`, which states that it must accompany every distribution of Cairn: source, images and charts | file read |
| Contributor agreement, CLA, `CONTRIBUTING`, `SECURITY` files | none found | file search |
| Copyright or SPDX headers in the Python and Rust sources searched | none found | `grep` of `cairn/src`, `cairn/store/src`, `cairn/catalog/src` |

**Reading the facts.** A repository with no sign-offs, no contributor agreement and no licence file for the product leaves the company's inbound and outbound rights to be established outside the repository. Section 4 covers the decisions.

## 3. Upstream and third-party code

### 3.1 Cairn Core: modified DataHub 1.8.0

Cairn Core is a modified version of DataHub 1.8.0, licensed under Apache-2.0 (`THIRD_PARTY_NOTICES.md`, "Cairn Core"). The notices reproduce the upstream NOTICE file verbatim and list the changes: relabelled user-facing text, translations and logos; a renamed built-in root account; default configuration; the Cairn authorizer plugin; micro-frontend configuration; and disabled product announcements. The build steps that make the changes are in `cairn/core/`.

- Apache-2.0 requires modified files to carry prominent notices that they were changed (section 4(b) of the licence text). The repository's build records the changes in `cairn/core/` rather than in each file. `[TO CONFIRM: counsel to confirm that the build's record satisfies section 4(b) for each distributed image]`
- Apache-2.0 section 6 says the licence does not grant permission to use the trade names, trademarks or product names of the licensor. The notices and the documentation refer to DataHub by name. `[TO CONFIRM: counsel review of nominative use of "DataHub" in the product and its documentation]`

### 3.2 Cairn Ontology: derived from NVIDIA Auto Ontology

`cairn/ontology/` is a Rust port of the ontology compiler of NVIDIA Auto Ontology, Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES, licensed under Apache-2.0. `cairn/ontology/NOTICE` lists the ported files, the changes made, and NVIDIA's own notice. Fourteen source files under `cairn/ontology/src` name the upstream origin. `[TO CONFIRM: counsel to confirm that the NOTICE content satisfies the licence for each file in the distributed image]`

### 3.3 Copyleft and other licences that need legal review

The full list, with the reason for each, is in section 4 of [`oss-license-inventory.md`](oss-license-inventory.md). The main points for counsel:

| Component | Licence as recorded | Where it is used | Question for counsel |
|---|---|---|---|
| MySQL Community Server | GPL-2.0 with the Universal FOSS Exception | A database container | Confirm the exception covers the way Cairn uses and ships the image |
| Grafana Loki and Tempo | AGPL-3.0 | Optional observability images, unmodified, off by default | Confirm the position for unmodified and for any modified build |
| PyInstaller | GPL-2.0-or-later with an exception | Builds the `cairn` command-line binaries | Confirm the exception's conditions for the binaries |
| psycopg, pymssql | LGPL | Python database drivers, used unmodified | Confirm the obligations for the distributed binaries and images |
| HashiCorp Go modules | MPL-2.0 | Linked unmodified into the Terraform provider | Confirm no MPL file is modified; confirm the notice obligations |
| certifi, axe-core | MPL-2.0 | `certifi` in Python dependencies; `axe-core` in development tests | Confirm `axe-core` is not in a shipped bundle |
| BusyBox (in Alpine and the `busybox` images) | GPL-2.0 | Runtime images of Cairn Store, Catalog, Ontology, the authorizer plugin and the Trino functions | Confirm the notices cover the `busybox` image, which they do not name |
| webpki-roots | CDLA-Permissive-2.0 | Compiled into Cairn Store, Catalog and Ontology | Confirm the attribution duties of this data licence |
| JDK in the Gradle and Spark images | [TO CONFIRM: licence] | Build image and Spark runtime | Record the JDK licence and distribution position |

### 3.4 Components with no recorded licence

Most Python, Rust, JVM and Go dependencies have no licence recorded in the repository. The inventory marks them `[TO CONFIRM: licence]`. The inventory does not infer their licences. Counsel should not rely on any licence for those components until the company has recorded it from the package's own metadata at the pinned version.

## 4. Inbound rights: contributions and authorship

**Facts.** Two distinct contributors are recorded in git history. Of 788 commits, 477 carry a co-author trailer naming an AI system. No commit carries a Developer Certificate of Origin sign-off. No contributor agreement is in the repository.

**Framework (proposal).** Choose one of these before the next external contribution or the next investor diligence round:

- **Option A, a contributor licence agreement (CLA)** that grants the company rights to the contributions, with an assignment or a licence. Counsel drafts it; every contributor signs before merge.
- **Option B, the Developer Certificate of Origin** with `Signed-off-by` on each commit, and an inbound licence of Apache-2.0 for contributions.
- **Option C, no external contributions.** The repository is closed to contributions from outside the company, and that is recorded.

Whichever is chosen, the record of each person's agreement should be held outside the repository by the company's counsel or secretary.

**Employees and contractors.**

- `[TO CONFIRM: invention assignment agreement (PIIA) signed by each person who has committed to the repository, with its date]`
- `[TO CONFIRM: whether any contributor worked under a previous employer's assignment term that could reach Cairn code]`
- `[TO CONFIRM: contractor agreements with assignment of work product, for every contractor who committed code]`

**AI-assisted authorship.** 477 commits carry a co-author trailer naming an AI system. `[TO CONFIRM: counsel position on the ownership and originality of AI-assisted code in the company's jurisdiction]` `[TO CONFIRM: the terms of each AI service used to produce code, including output ownership, confidentiality and any training-use commitments]`

**Records.** The company should keep a register of agreements (contributor, form, version, date, signatory) outside the repository. `[TO CONFIRM: owner and location of the register]`

## 5. Outbound rights: the licence the product is distributed under

**Facts.** The product manifests declare Apache-2.0. The repository has no `LICENSE` file for Cairn itself, so a recipient of the source has the declaration but no licence text at the root.

**Framework (proposal).**

1. Choose the outbound licence for the product, and whether any part is distributed under a different one (for example, the SDKs).
2. Add a `LICENSE` file at the root of the repository and in each separately distributed package, with the chosen text.
3. Add a `NOTICE` file for the company's own attribution, separate from `THIRD_PARTY_NOTICES.md`.
4. Keep `THIRD_PARTY_NOTICES.md` in every distribution, as its own text requires, and regenerate it from the inventory each release.

`[TO CONFIRM: outbound licence decision, and the owner who will add the LICENSE and NOTICE files]`

## 6. Patents

Apache-2.0 includes an express patent licence from each contributor for the contribution, subject to the terms of section 3 of the licence text reproduced in the notices. Cairn's own patents or patent applications, if any, are not recorded anywhere in the repository. `[TO CONFIRM: patent filings, applications and freedom-to-operate review, if any]`

## 7. Name and marks

The product is named Cairn. `[TO CONFIRM: trademark search and clearance for "Cairn" in the company's markets, and whether the name is registered]` Section 3.1 covers the upstream names.

## 8. Export controls

The product includes cryptographic functionality: AES-256-GCM for data at rest, TLS, Ed25519 and X25519 for remote agents, and AES-256-GCM for agent payloads (`cairn/store/src/crypt.rs`; `ARCHITECTURE.md` §1.4). `[TO CONFIRM: export classification of the product and of each distributed image, and any filing or licence obligation in the company's jurisdiction and the jurisdictions of its customers]`

## 9. Process for keeping rights and licences current

Proposed, to be owned by a named person:

1. **Each release.** Regenerate the third-party inventory from the manifests and lock files, using the same rules as [`oss-license-inventory.md`](oss-license-inventory.md). The continuous integration already produces an SPDX software bill of materials for each Cairn image (`.github/workflows/cairn-ci.yml` at the repository root, job `stack`). Use those files as the source of the release inventory.
2. **Each new dependency.** Before a dependency is added, record its licence in the inventory. A dependency whose licence is copyleft, non-permissive or unrecorded is reviewed by counsel before it merges.
3. **Each upstream change.** When DataHub or NVIDIA Auto Ontology releases a new version that Cairn ports, check the NOTICE and attribution headers against the changes.
4. **Each distribution.** Ship `THIRD_PARTY_NOTICES.md` and the licence files with every image, chart and package.
5. **Annually.** Counsel reviews this document and the inventory.

`[TO CONFIRM: owner of steps 1 to 5]`

## 10. Open items

| # | Item | Owner |
|---|---|---|
| I1 | Choose the inbound mechanism (section 4) and record the agreements | [TO CONFIRM: counsel] |
| I2 | Collect PIIAs and contractor assignments for each committer | [TO CONFIRM: company] |
| I3 | Position on AI-assisted code and the AI services' terms | [TO CONFIRM: counsel] |
| I4 | Outbound licence decision and LICENSE and NOTICE files (section 5) | [TO CONFIRM: company and counsel] |
| I5 | Review of Apache-2.0 notices for Cairn Core images (section 3.1) | [TO CONFIRM: counsel] |
| I6 | Nominative use of upstream names; trademark clearance for Cairn (sections 3.1, 7) | [TO CONFIRM: counsel] |
| I7 | Review of the copyleft and non-permissive items in section 3.3 | [TO CONFIRM: counsel] |
| I8 | Record licences for every `[TO CONFIRM: licence]` row in the inventory | [TO CONFIRM: owner] |
| I9 | Export classification (section 8) | [TO CONFIRM: counsel] |
| I10 | Patent position (section 6) | [TO CONFIRM: counsel] |
| I11 | Owner of the annual review and the release process (section 9) | [TO CONFIRM: company] |

## 11. Limits of this document

This is a framework and an evidence summary prepared from the repository. It is not legal advice, does not interpret any licence for the company, and does not clear any component. Each `[TO CONFIRM]` item needs a decision or a document from the company or from counsel.
