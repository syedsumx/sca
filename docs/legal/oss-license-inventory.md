# Open-source licence audit: inventory

**Status:** inventory for counsel and the board. This is not a legal opinion, and it is not a clearance of any licence.
**Date:** 2026-11-14. **Basis:** the repository at commit `783865d7` (branch `claude/funny-meitner-qtz0ux`).
**Related:** [`ip-governance.md`](ip-governance.md) (the framework this inventory feeds).

## 1. Purpose and limits

This inventory lists every third-party dependency the repository declares: from the package manifests, lock files, Dockerfile base images and pinned install lines, and Terraform provider declarations. For each it records the name, version, the manifest it comes from, its scope, and its licence as the repository records it. It then flags the licences counsel needs to review, and the ones the repository does not record.

It does not establish that any licence is acceptable, does not check that the bundled code matches the declared versions, and does not inventory Helm chart images, Compose images or container images pulled at run time, except where `cairn/THIRD_PARTY_NOTICES.md` names them.

## 2. Method

**Sources searched.**

- Python: `pyproject.toml` (root), `cli/`, `airflow-provider/`, `lakehouse-client/`, `spark/`, and `examples/connectors/csv-folder/`. Declared dependencies and optional groups.
- npm: `ui/package.json` with `ui/package-lock.json` (555 installed-package entries); `desktop/package.json` with `desktop/package-lock.json` (289 installed-package entries); `addins/`, `extension/` and `sdks/js/` manifests (no third-party dependencies declared).
- Rust: `store/`, `catalog/` and `ontology/` `Cargo.toml` and `Cargo.lock`. Registry and git packages; workspace crates excluded.
- Go: `terraform-provider-cairn/go.mod` (direct and indirect requirements).
- JVM: `plugins/java/cairn-authorizer/build.gradle`, `store/hadoop-check/build.gradle`, and the Maven artefacts that `docker/spark.Dockerfile` downloads.
- Dockerfiles: `FROM` images in `core/Dockerfile` and `docker/*.Dockerfile` (build arguments resolved from their defaults), and `pip install` lines with exact pins.
- Terraform: `required_providers` in `deploy/terraform/` and `deploy/hetzner/terraform/`.
- `sdks/java/`: source only, no build file. Its imports are Java platform packages only.

**Licence sources, in order.**

1. The licence field in the npm lock files. Every installed-package entry in both npm lock files carries one.
2. `cairn/THIRD_PARTY_NOTICES.md`, where it names the component or a family that includes it.
3. Nothing in the repository: the licence is `[TO CONFIRM: licence]`.

No licence was taken from installed packages, package caches, or the internet.

**Status rules.**

- *Permissive (review set)*: every licence term is in this review's working set: MIT, MIT-0, ISC, BSD-2-Clause, BSD-3-Clause, BSD, Apache-2.0, 0BSD, Unlicense, CC0-1.0, Python-2.0, PSF-2.0, Zlib, BlueOak-1.0.0. The set is for triage. Membership is not a determination that a licence is acceptable.
- *Legal review*: a copyleft licence (GPL, LGPL, AGPL, MPL, EPL, SSPL, BUSL, CC-BY-SA and similar); a licence outside the set; or a dual licence whose elected term must be recorded.
- *Unknown*: no licence recorded in the repository. This does not mean copyleft. It means the repository does not say.

**Counting.** npm and Rust rows are unique name-and-version pairs: a package installed at the same version in several places is listed once. The Rust count covers the three lock files together.

**Scope column.** For npm: `runtime`, `dev only`, `optional` or `peer` (from the lock file), and `direct` or `transitive`. Read `dev only` with care: see section 5.

## 3. Summary

The counts below are generated from the tables in section 6. A package that appears in several manifests has one row per manifest. `npm (addins)`, `npm (extension)` and `npm (sdks/js)` declare no third-party packages, so they contribute no rows.

| Ecosystem | Rows | Distinct names | Permissive (review set) | Legal review | Unknown licence |
|---|---:|---:|---:|---:|---:|
| Python (declared in manifests) | 77 | 63 | 4 | 3 | 70 |
| npm (ui) | 523 | 481 | 521 | 2 | 0 |
| npm (desktop) | 273 | 245 | 270 | 3 | 0 |
| npm (addins) | 0 | 0 | 0 | 0 | 0 |
| npm (extension) | 0 | 0 | 0 | 0 | 0 |
| npm (sdks/js) | 0 | 0 | 0 | 0 | 0 |
| Rust crates (store, catalog, ontology) | 561 | 499 | 0 | 1 | 560 |
| Go modules (terraform-provider-cairn) | 23 | 23 | 2 | 9 | 12 |
| Gradle / Maven (JVM) | 21 | 19 | 1 | 0 | 20 |
| Base images (Dockerfiles) | 33 | 15 | 23 | 5 | 5 |
| Python pinned in Dockerfiles | 20 | 17 | 5 | 0 | 15 |
| Terraform providers (deploy/) | 7 | 6 | 0 | 0 | 7 |
| **Total** | **1538** | | **826** | **23** | **689** |

## 4. Items flagged for legal review

### 4.1 Flagged from the manifests and lock files

Each row needs a decision from counsel: accept, replace, or record the elected licence.

| Ecosystem | Name | Version | Scope | Licence as recorded | Reason for legal review |
|---|---|---|---|---|---|
| Python (declared in manifests) | psycopg[binary] | >=3.1 | extra:postgres, extra:dr | LGPL-3.0 / LGPL-2.1 (per notices: psycopg and pymssql) | LGPL-3.0 / LGPL-2.1 (recorded in notices). Used unmodified as a library per the notices; LGPL conditions apply to any redistribution of the library and to relinking. |
| Python (declared in manifests) | pymssql | >=2.3 | extra:streams | LGPL-3.0 / LGPL-2.1 (per notices: psycopg and pymssql) | LGPL-3.0 / LGPL-2.1 (recorded in notices). Used unmodified per the notices; LGPL conditions apply to redistribution. |
| Python (declared in manifests) | ibm_db | >=3.2 | extra:streams (x86-64 Linux, macOS, Windows) | Apache-2.0 (package metadata); the wheel bundles IBM's Data Server Driver (clidriver), under IBM's own licence | The Python package is Apache-2.0, but its wheel carries IBM's proprietary CLI driver. Confirm the driver's terms permit shipping it in Cairn's images, or move it to an operator-installed extra as with hdbcli. |
| Python (declared in manifests) | hdbcli | >=2.20 | extra:hana only (not in streams, not in Cairn's images) | SAP Developer License Agreement (package metadata) | Proprietary SAP licence, not open source. Kept out of the default extras and images: an operator installs it (`pip install 'cairn-platform[hana]'`) under the customer's own SAP licence. Confirm this arrangement. |
| Python (declared in manifests) | pypdfium2 | >=4.30 | extra:documents | BSD-3-Clause and Apache-2.0 (per notices; bundled libraries listed in the wheel, not in the repository) | BSD-3-Clause and Apache-2.0 (recorded in notices), but the wheel bundles FreeType, HarfBuzz, ICU, libjpeg-turbo, libpng, libtiff, OpenJPEG, Little CMS and others. Their licences are listed only in the wheel's BUILD_LICENSES file, which is not in the repository. |
| npm (ui) | axe-core | 4.10.3 | dev only, direct; declared 4.10.3 (devDependencies) | MPL-2.0 | MPL-2.0 (file-level copyleft). Development dependency used by accessibility tests. Confirm it is not in any shipped UI bundle. |
| npm (ui) | caniuse-lite | 1.0.30001819 | dev only, transitive | CC-BY-4.0 | CC-BY-4.0: a data licence with attribution terms, outside the review set. Development dependency. |
| npm (desktop) | sanitize-filename | 1.6.5 | dev only, transitive | WTFPL OR ISC | Dual licence WTFPL OR ISC. The elected licence must be recorded (ISC is in the review set). Development dependency. |
| npm (desktop) | truncate-utf8-bytes | 1.0.2 | dev only, transitive | WTFPL | WTFPL: a permissive-in-intent licence outside the review set. Development dependency. |
| npm (desktop) | utf8-byte-length | 1.0.5 | dev only, transitive | (WTFPL OR MIT) | Dual licence (WTFPL OR MIT). The elected licence must be recorded (MIT is in the review set). Development dependency. |
| Rust crates (store, catalog, ontology) | webpki-roots | 1.0.11 | direct; crates.io registry | CDLA-Permissive-2.0 (per notices) | CDLA-Permissive-2.0 (recorded in notices): a data licence for the Mozilla root store, compiled into Cairn Store, Catalog and Ontology. Outside the review set; attribution duties to confirm. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/terraform-plugin-framework | v1.15.1 | direct | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/go-hclog | v1.5.0 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/go-plugin | v1.6.3 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/go-uuid | v1.0.3 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/terraform-plugin-go | v0.27.0 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/terraform-plugin-log | v0.9.0 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/terraform-registry-address | v0.2.5 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/terraform-svchost | v0.1.1 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Go modules (terraform-provider-cairn) | github.com/hashicorp/yamux | v0.1.1 | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | MPL-2.0 (recorded in notices: HashiCorp Go modules). File-level copyleft: applies to modified MPL files. Linked unmodified into the Terraform provider, per the notices. |
| Base images (Dockerfiles) | busybox:1.37 | (unnamed stage) | last stage in file | GPL-2.0 for BusyBox (per notices, under Alpine only; the busybox image itself is not named) | GPL-2.0 for BusyBox (recorded in notices under the Alpine row only). The busybox image is not named in the notices. Used as the final image of the authorizer-plugin and trino-functions images. |
| Base images (Dockerfiles) | alpine:3.20 | (unnamed stage) | last stage in file | MIT; GPL-2.0 for BusyBox (per notices: Alpine Linux) | Base image of Cairn Store, Catalog and Ontology. The notices record GPL-2.0 for BusyBox, which the image contains. |

### 4.2 Flagged from the notices only

Components named in `cairn/THIRD_PARTY_NOTICES.md` that are not in the manifests searched above. They are flagged because the notices record a copyleft licence or a licence outside the review set.

| Component | How it ships | Licence as recorded | Reason for legal review |
|---|---|---|---|
| MySQL Community Server | Database container: the notices name it as the Cairn Core database. `deploy/ops/backup.sh` dumps a MySQL catalog database in Compose | GPL-2.0 with the Universal FOSS Exception | Copyleft, shipped as a separate program in a container. Counsel to confirm the exception applies to Cairn's use and to the image as distributed. |
| Grafana Loki and Grafana Tempo | Optional observability stack (`cairn.observability.enabled`, off by default); unmodified upstream images | AGPL-3.0 | Network-use copyleft. Unmodified images per the notices; any modified build would need separate review. |
| PyInstaller | Builds the single-file `cairn` command-line binaries (`cli/cairn.spec`) | GPL-2.0-or-later with an exception for programs built with it | Copyleft tool; the exception is for the built program. The bundled Python runtime is under the PSF licence. Counsel to confirm the exception's conditions. |
| Tesseract language data | Used with Tesseract OCR for document parsing (the notices name the OCR engine as part of the control-plane image) | [TO CONFIRM: whether language data is shipped, and its licence] | The notices do not say whether language data is shipped or what licence applies to it. |
| JDK in the `gradle:8.14.3-jdk21` and Spark base images | Build image (authorizer plugin); Spark runtime | [TO CONFIRM: licence] | A Java runtime is in these images, but no JDK licence is recorded in the repository. |
| Node.js in `node:20.18-alpine` | Build stage of the consumer UI image (not in the shipped image) | [TO CONFIRM: licence] | Recorded nowhere in the repository. |

## 5. Unknown licences and repository gaps

**Most rows are unknown because the repository does not record their licences.** The unknown rows cover most Python packages, every Rust crate except `webpki-roots`, every JVM artefact except the Iceberg Spark runtime, the Go modules the notices do not name, every Terraform provider, and most Dockerfile pins. Their licences need to be taken from each package's own metadata at the pinned version, recorded in this file, and checked by counsel. The repository should record them (section 5, item 1). `[TO CONFIRM: process owner and date for recording the unknown licences]`

**Gaps that affect the audit:**

1. **Python has no lock file.** `pyproject.toml` declares version ranges only. No `uv.lock`, `poetry.lock` or requirements file exists. The set of packages in any built environment is therefore not fixed by the repository, apart from the exact pins in the Dockerfiles listed in section 6.
2. **Rust lock files record no licences.** `Cargo.lock` gives versions and sources only.
3. **JVM artefacts are not locked.** No Gradle lock or verification metadata exists. The Spark image downloads its jars by URL during the build. Only the Trino JDBC jar is checked against a SHA-1 (`docker/spark.Dockerfile`, line 48).
4. **Two AWS SDK versions.** `store/hadoop-check/build.gradle` uses AWS SDK 2.24.6 and `docker/spark.Dockerfile` downloads `s3-transfer-manager` 2.33.0. Whether the difference is intended is `[TO CONFIRM: owner]`.
5. **Terraform providers use `~>` constraints** and no `.terraform.lock.hcl` was found in the repository. The exact provider versions used in a deployment are not recorded.
6. **Helm charts declare no subchart dependencies.** Image versions are in the values files.
7. **Web UI scope.** The npm lock marks packages declared only in `devDependencies` as `dev only`. The web UI is built into a bundle that ships in the consumer UI image, and that bundle includes code from packages declared as development dependencies. React and Ant Design are declared that way (`ui/package.json`, `devDependencies` and `peerDependencies`). So `dev only` does not show what ships. `[TO CONFIRM: the packages in the built UI bundle]`
8. **Notices omit components the repository uses.** `cairn/THIRD_PARTY_NOTICES.md` does not name: OpenLineage, `hadoop-aws`, the AWS SDK transfer manager, the Trino JDBC driver; the base images `busybox:1.37`, `gradle:8.14.3-jdk21`, `rust:1-alpine` and `node:20.18-alpine`; and most Python packages pinned in the Dockerfiles (for example `fastapi`, `anthropic`, `openai`, `duckdb`, `pandas`, `pyarrow`, `nbconvert`). Its Python row is generic (`per package`), and no Python licence is recorded per package in the repository.
9. **No licence files for the Cairn product itself** were found, other than `ontology/LICENSE` (see `ip-governance.md`).
10. **Go module licences** are not in `go.mod`. `go.sum` is present (91 lines).

**Not third-party code.** `addins/`, `extension/` and `sdks/js/` import only Node.js built-in modules. `sdks/java/` imports only Java platform packages. Their manifests declare no third-party dependencies, so they have no rows beyond a placeholder.


## 6. Inventory tables

Each table lists one manifest-derived dependency per row, in the order the manifests give. Version is the exact version where a lock file or an exact pin gives one; otherwise the declared range.

### Python (declared in manifests)

77 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | pydantic | >=2.6 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 2 | httpx | >=0.27 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 3 | sqlalchemy | >=2.0 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 4 | pyyaml | >=6.0 | pyproject.toml | core, extra:dr | [TO CONFIRM: licence] | **unknown** |
| 5 | croniter | >=2.0 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 6 | cryptography | >=42 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 7 | sqlglot | >=25 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 8 | alembic | >=1.13 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 9 | tomli | >=2.0 | pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 10 | fastapi | >=0.110 | pyproject.toml | extra:server, extra:test | [TO CONFIRM: licence] | **unknown** |
| 11 | uvicorn[standard] | >=0.29 | pyproject.toml | extra:server | [TO CONFIRM: licence] | **unknown** |
| 12 | prometheus-client | >=0.20 | pyproject.toml | extra:server | [TO CONFIRM: licence] | **unknown** |
| 13 | python-multipart | >=0.0.9 | pyproject.toml | extra:server | [TO CONFIRM: licence] | **unknown** |
| 14 | webauthn | >=2.2 | pyproject.toml | extra:server | [TO CONFIRM: licence] | **unknown** |
| 15 | bcrypt | >=4.1 | pyproject.toml | extra:server | Apache-2.0 (per notices) | permissive (review set) |
| 16 | psycopg[binary] | >=3.1 | pyproject.toml | extra:postgres, extra:dr | LGPL-3.0 / LGPL-2.1 (per notices: psycopg and pymssql) | **legal review** |
| 17 | pgvector | >=0.2 | pyproject.toml | extra:postgres | [TO CONFIRM: licence] | **unknown** |
| 18 | anthropic | >=2.0 | pyproject.toml | extra:ai | [TO CONFIRM: licence] | **unknown** |
| 19 | mcp | >=2.0 | pyproject.toml | extra:ai | [TO CONFIRM: licence] | **unknown** |
| 20 | acryl-datahub-actions | >=1.0 | pyproject.toml | extra:actions | [TO CONFIRM: licence] | **unknown** |
| 21 | opentelemetry-sdk | >=1.39 | pyproject.toml | extra:otel | [TO CONFIRM: licence] | **unknown** |
| 22 | opentelemetry-exporter-otlp-proto-http | >=1.39 | pyproject.toml | extra:otel | [TO CONFIRM: licence] | **unknown** |
| 23 | opentelemetry-instrumentation-fastapi | >=0.60b0 | pyproject.toml | extra:otel | [TO CONFIRM: licence] | **unknown** |
| 24 | opentelemetry-instrumentation-httpx | >=0.60b0 | pyproject.toml | extra:otel | [TO CONFIRM: licence] | **unknown** |
| 25 | opentelemetry-instrumentation-sqlalchemy | >=0.60b0 | pyproject.toml | extra:otel | [TO CONFIRM: licence] | **unknown** |
| 26 | acryl-datahub | >=1.0 | pyproject.toml | extra:agent | [TO CONFIRM: licence] | **unknown** |
| 27 | snowflake-sqlalchemy | >=1.5 | pyproject.toml | extra:warehouse | [TO CONFIRM: licence] | **unknown** |
| 28 | sqlalchemy-bigquery | >=1.9 | pyproject.toml | extra:warehouse | [TO CONFIRM: licence] | **unknown** |
| 29 | databricks-sql-connector | >=3.0 | pyproject.toml | extra:warehouse | [TO CONFIRM: licence] | **unknown** |
| 30 | boto3 | >=1.34 | pyproject.toml | extra:warehouse, extra:test, extra:streams | [TO CONFIRM: licence] | **unknown** |
| 31 | pytest | >=8 | pyproject.toml | extra:test | [TO CONFIRM: licence] | **unknown** |
| 32 | pyjwt | >=2.8 | pyproject.toml | extra:test | [TO CONFIRM: licence] | **unknown** |
| 33 | cbor2 | >=5.6 | pyproject.toml | extra:test | [TO CONFIRM: licence] | **unknown** |
| 34 | openai | >=1.40 | pyproject.toml | extra:test | [TO CONFIRM: licence] | **unknown** |
| 35 | keyring | >=24 | pyproject.toml | extra:test, extra:cli | [TO CONFIRM: licence] | **unknown** |
| 36 | keyrings.alt | >=5 | pyproject.toml | extra:test | [TO CONFIRM: licence] | **unknown** |
| 37 | pyarrow | >=15 | pyproject.toml | extra:export, extra:streams | [TO CONFIRM: licence] | **unknown** |
| 38 | pyiceberg | >=0.8 | pyproject.toml | extra:export, extra:streams | [TO CONFIRM: licence] | **unknown** |
| 39 | trino | >=0.330 | pyproject.toml | extra:trino | [TO CONFIRM: licence] (the notices name the trino package under the Airflow provider only; confirm it covers this pin) | **unknown** |
| 40 | kafka-python | >=2.2,<3 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 41 | fastavro | >=1.9 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 42 | numpy | >=1.26 | pyproject.toml | extra:streams, extra:media | [TO CONFIRM: licence] | **unknown** |
| 43 | openpyxl | >=3.1 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 44 | mysql-replication | >=1.0 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 45 | pymysql | >=1.1 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 46 | pymssql | >=2.3 | pyproject.toml | extra:streams | LGPL-3.0 / LGPL-2.1 (per notices: psycopg and pymssql) | **legal review** |
| 47 | pymongo | >=4.6 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 48 | oracledb | >=2.5 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 49 | duckdb | >=1.1 | pyproject.toml | extra:streams | [TO CONFIRM: licence] | **unknown** |
| 50 | pypdf | >=5 | pyproject.toml | extra:documents | BSD-3-Clause (per notices) | permissive (review set) |
| 51 | pillow | >=10 | pyproject.toml | extra:documents | MIT-CMU (per notices) | permissive (review set) |
| 52 | pypdfium2 | >=4.30 | pyproject.toml | extra:documents | BSD-3-Clause and Apache-2.0 (per notices; bundled libraries listed in the wheel, not in the repository) | **legal review** |
| 53 | faster-whisper | >=1.1 | pyproject.toml | extra:media | [TO CONFIRM: licence] | **unknown** |
| 54 | torch | >=2.4 | pyproject.toml | extra:finetune | [TO CONFIRM: licence] | **unknown** |
| 55 | transformers | >=4.45 | pyproject.toml | extra:finetune | [TO CONFIRM: licence] | **unknown** |
| 56 | peft | >=0.13 | pyproject.toml | extra:finetune | [TO CONFIRM: licence] | **unknown** |
| 57 | huggingface-hub | >=0.24 | pyproject.toml | extra:finetune | [TO CONFIRM: licence] | **unknown** |
| 58 | signxml | >=4,<5 | pyproject.toml | extra:saml | [TO CONFIRM: licence] | **unknown** |
| 59 | lxml | >=5 | pyproject.toml | extra:saml | [TO CONFIRM: licence] | **unknown** |
| 60 | dnspython | >=2.6 | pyproject.toml | extra:dr | [TO CONFIRM: licence] | **unknown** |
| 61 | httpx | >=0.27 | cli/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 62 | pyyaml | >=6.0 | cli/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 63 | keyring | >=24 | cli/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 64 | pydantic | >=2.6 | cli/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 65 | croniter | >=2.0 | cli/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 66 | tomli | >=2.0 | cli/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 67 | pyarrow | >=15 | cli/pyproject.toml | extra:parquet | [TO CONFIRM: licence] | **unknown** |
| 68 | apache-airflow | >=3.0 | airflow-provider/pyproject.toml | core | Apache-2.0 (per notices) | permissive (review set) |
| 69 | trino | >=0.330 | airflow-provider/pyproject.toml | core | [TO CONFIRM: licence] (the notices name the trino package under the Airflow provider only; confirm it covers this pin) | **unknown** |
| 70 | httpx | >=0.27 | airflow-provider/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 71 | pyjwt[crypto] | >=2.8 | airflow-provider/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 72 | httpx | >=0.27 | lakehouse-client/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 73 | pyiceberg[pyarrow] | >=0.9 | lakehouse-client/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 74 | duckdb | >=1.1 | lakehouse-client/pyproject.toml | extra:duckdb | [TO CONFIRM: licence] | **unknown** |
| 75 | httpx | >=0.27 | spark/pyproject.toml | core | [TO CONFIRM: licence] | **unknown** |
| 76 | mlflow-skinny | ==3.5.0 | spark/pyproject.toml | extra:mlflow | [TO CONFIRM: licence] | **unknown** |
| 77 | pandas | >=2.0 | spark/pyproject.toml | extra:mlflow | [TO CONFIRM: licence] | **unknown** |
| 78 | ibm_db | >=3.2 | pyproject.toml | extra:streams | Apache-2.0 (bundles IBM clidriver) | **legal review** |
| 79 | hdbcli | >=2.20 | pyproject.toml | extra:hana | SAP Developer License Agreement | **legal review** |

### npm (ui)

523 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | @ant-design/colors | 6.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 2 | @ant-design/icons | 4.8.3 | ui/package.json + package-lock.json | dev only, direct; declared 4.8.3 (devDependencies) | MIT | permissive (review set) |
| 3 | @ant-design/icons-svg | 4.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 4 | @ant-design/react-slick | 0.29.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 5 | @asamuzakjp/css-color | 3.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 6 | @babel/code-frame | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 7 | @babel/compat-data | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 8 | @babel/core | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 9 | @babel/generator | 7.29.8 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 10 | @babel/helper-annotate-as-pure | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 11 | @babel/helper-compilation-targets | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 12 | lru-cache | 5.1.1 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 13 | @babel/helper-globals | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 14 | @babel/helper-module-imports | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 15 | @babel/helper-module-transforms | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 16 | @babel/helper-plugin-utils | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 17 | @babel/helper-string-parser | 7.29.11 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 18 | @babel/helper-validator-identifier | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 19 | @babel/helper-validator-option | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 20 | @babel/helpers | 7.29.10 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 21 | @babel/parser | 7.29.11 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 22 | @babel/plugin-syntax-jsx | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 23 | @babel/runtime | 7.29.10 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 24 | @babel/template | 7.29.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 25 | @babel/traverse | 7.29.10 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 26 | @babel/types | 7.29.11 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 27 | @codemirror/autocomplete | 6.20.4 | ui/package.json + package-lock.json | runtime, direct; declared 6.20.4 (dependencies) | MIT | permissive (review set) |
| 28 | @codemirror/commands | 6.11.1 | ui/package.json + package-lock.json | runtime, direct; declared 6.11.1 (dependencies) | MIT | permissive (review set) |
| 29 | @codemirror/lang-sql | 6.10.0 | ui/package.json + package-lock.json | runtime, direct; declared 6.10.0 (dependencies) | MIT | permissive (review set) |
| 30 | @codemirror/language | 6.13.1 | ui/package.json + package-lock.json | runtime, direct; declared 6.13.1 (dependencies) | MIT | permissive (review set) |
| 31 | @codemirror/search | 6.7.2 | ui/package.json + package-lock.json | runtime, direct; declared 6.7.2 (dependencies) | MIT | permissive (review set) |
| 32 | @codemirror/state | 6.7.6 | ui/package.json + package-lock.json | runtime, direct; declared 6.7.6 (dependencies) | MIT | permissive (review set) |
| 33 | @codemirror/streamparser | 6.0.0 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 34 | @codemirror/view | 6.43.15 | ui/package.json + package-lock.json | runtime, direct; declared 6.43.15 (dependencies) | MIT | permissive (review set) |
| 35 | @csstools/color-helpers | 5.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT-0 | permissive (review set) |
| 36 | @csstools/css-calc | 2.1.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 37 | @csstools/css-color-parser | 3.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 38 | @csstools/css-parser-algorithms | 3.0.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 39 | @csstools/css-tokenizer | 3.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 40 | @ctrl/tinycolor | 3.6.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 41 | @discoveryjs/json-ext | 0.5.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 42 | @emotion/is-prop-valid | 1.4.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 43 | @emotion/memoize | 0.9.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 44 | @emotion/stylis | 0.8.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 45 | @emotion/unitless | 0.7.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 46 | @esbuild/aix-ppc64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 47 | @esbuild/android-arm | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 48 | @esbuild/android-arm64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 49 | @esbuild/android-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 50 | @esbuild/darwin-arm64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 51 | @esbuild/darwin-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 52 | @esbuild/freebsd-arm64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 53 | @esbuild/freebsd-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 54 | @esbuild/linux-arm | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 55 | @esbuild/linux-arm64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 56 | @esbuild/linux-ia32 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 57 | @esbuild/linux-loong64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 58 | @esbuild/linux-mips64el | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 59 | @esbuild/linux-ppc64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 60 | @esbuild/linux-riscv64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 61 | @esbuild/linux-s390x | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 62 | @esbuild/linux-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 63 | @esbuild/netbsd-arm64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 64 | @esbuild/netbsd-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 65 | @esbuild/openbsd-arm64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 66 | @esbuild/openbsd-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 67 | @esbuild/sunos-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 68 | @esbuild/win32-arm64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 69 | @esbuild/win32-ia32 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 70 | @esbuild/win32-x64 | 0.24.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 71 | @jest/schemas | 29.6.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 72 | @jridgewell/gen-mapping | 0.3.13 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 73 | @jridgewell/remapping | 2.3.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 74 | @jridgewell/resolve-uri | 3.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 75 | @jridgewell/source-map | 0.3.11 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 76 | @jridgewell/sourcemap-codec | 1.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 77 | @jridgewell/trace-mapping | 0.3.31 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 78 | @lezer/common | 1.5.5 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 79 | @lezer/highlight | 1.2.5 | ui/package.json + package-lock.json | runtime, direct; declared 1.2.5 (dependencies) | MIT | permissive (review set) |
| 80 | @lezer/lr | 1.4.12 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 81 | @marijn/find-cluster-break | 1.0.4 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 82 | @napi-rs/lzma-linux-x64-gnu | 1.5.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 83 | @rc-component/portal | 1.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 84 | @rollup/rollup-android-arm-eabi | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 85 | @rollup/rollup-android-arm64 | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 86 | @rollup/rollup-darwin-arm64 | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 87 | @rollup/rollup-darwin-x64 | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 88 | @rollup/rollup-freebsd-arm64 | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 89 | @rollup/rollup-freebsd-x64 | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 90 | @rollup/rollup-linux-arm-gnueabihf | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 91 | @rollup/rollup-linux-arm-musleabihf | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 92 | @rollup/rollup-linux-arm64-gnu | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 93 | @rollup/rollup-linux-arm64-musl | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 94 | @rollup/rollup-linux-loong64-gnu | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 95 | @rollup/rollup-linux-loong64-musl | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 96 | @rollup/rollup-linux-ppc64-gnu | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 97 | @rollup/rollup-linux-ppc64-musl | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 98 | @rollup/rollup-linux-riscv64-gnu | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 99 | @rollup/rollup-linux-riscv64-musl | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 100 | @rollup/rollup-linux-s390x-gnu | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 101 | @rollup/rollup-linux-x64-gnu | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 102 | @rollup/rollup-linux-x64-musl | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 103 | @rollup/rollup-openbsd-x64 | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 104 | @rollup/rollup-openharmony-arm64 | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 105 | @rollup/rollup-win32-arm64-msvc | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 106 | @rollup/rollup-win32-ia32-msvc | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 107 | @rollup/rollup-win32-x64-gnu | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 108 | @rollup/rollup-win32-x64-msvc | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 109 | @sinclair/typebox | 0.27.12 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 110 | @testing-library/dom | 8.20.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 111 | @testing-library/react | 12.1.5 | ui/package.json + package-lock.json | dev only, direct; declared 12.1.5 (devDependencies) | MIT | permissive (review set) |
| 112 | @types/aria-query | 5.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 113 | @types/estree | 1.0.9 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 114 | @types/history | 4.7.11 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 115 | @types/hoist-non-react-statics | 3.3.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 116 | @types/json-schema | 7.0.15 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 117 | @types/node | 18.19.39 | ui/package.json + package-lock.json | dev only, direct; declared ^18.19.39 (devDependencies) | MIT | permissive (review set) |
| 118 | @types/prop-types | 15.7.15 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 119 | @types/react | 17.0.83 | ui/package.json + package-lock.json | dev only, direct; declared 17.0.83 (devDependencies) | MIT | permissive (review set) |
| 120 | @types/react-dom | 17.0.25 | ui/package.json + package-lock.json | dev only, direct; declared 17.0.25 (devDependencies) | MIT | permissive (review set) |
| 121 | @types/react-router | 5.1.20 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 122 | @types/react-router-dom | 5.3.3 | ui/package.json + package-lock.json | dev only, direct; declared 5.3.3 (devDependencies) | MIT | permissive (review set) |
| 123 | @types/scheduler | 0.16.8 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 124 | @types/styled-components | 5.1.34 | ui/package.json + package-lock.json | dev only, direct; declared 5.1.34 (devDependencies) | MIT | permissive (review set) |
| 125 | @vitest/expect | 1.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 126 | @vitest/runner | 1.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 127 | @vitest/snapshot | 1.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 128 | ansi-styles | 5.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 129 | pretty-format | 29.7.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 130 | react-is | 18.3.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 131 | @vitest/spy | 1.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 132 | @vitest/utils | 1.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 133 | @webassemblyjs/ast | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 134 | @webassemblyjs/floating-point-hex-parser | 1.13.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 135 | @webassemblyjs/helper-api-error | 1.13.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 136 | @webassemblyjs/helper-buffer | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 137 | @webassemblyjs/helper-numbers | 1.13.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 138 | @webassemblyjs/helper-wasm-bytecode | 1.13.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 139 | @webassemblyjs/helper-wasm-section | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 140 | @webassemblyjs/ieee754 | 1.13.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 141 | @webassemblyjs/leb128 | 1.13.2 | ui/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 142 | @webassemblyjs/utf8 | 1.13.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 143 | @webassemblyjs/wasm-edit | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 144 | @webassemblyjs/wasm-gen | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 145 | @webassemblyjs/wasm-opt | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 146 | @webassemblyjs/wasm-parser | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 147 | @webassemblyjs/wast-printer | 1.14.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 148 | @webpack-cli/configtest | 2.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 149 | @webpack-cli/info | 2.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 150 | @webpack-cli/serve | 2.0.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 151 | @xtuc/ieee754 | 1.2.0 | ui/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 152 | @xtuc/long | 4.2.2 | ui/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 153 | acorn | 8.19.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 154 | acorn-walk | 8.3.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 155 | agent-base | 7.1.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 156 | ajv | 8.20.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 157 | ajv-formats | 3.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 158 | ajv-keywords | 5.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 159 | ansi-regex | 5.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 160 | ansi-styles | 4.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 161 | antd | 4.24.7 | ui/package.json + package-lock.json | dev only, direct; declared 4.24.7 (peerDependencies) | MIT | permissive (review set) |
| 162 | argparse | 2.0.1 | ui/package.json + package-lock.json | runtime, transitive | Python-2.0 | permissive (review set) |
| 163 | aria-query | 5.1.3 | ui/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 164 | array-buffer-byte-length | 1.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 165 | array-tree-filter | 2.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 166 | assertion-error | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 167 | async-validator | 4.2.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 168 | asynckit | 0.4.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 169 | available-typed-arrays | 1.0.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 170 | axe-core | 4.10.3 | ui/package.json + package-lock.json | dev only, direct; declared 4.10.3 (devDependencies) | MPL-2.0 | **legal review** |
| 171 | baseline-browser-mapping | 2.11.33 | ui/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 172 | big.js | 5.2.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 173 | browserslist | 4.29.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 174 | buffer-from | 1.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 175 | cac | 6.7.14 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 176 | call-bind | 1.0.9 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 177 | call-bind-apply-helpers | 1.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 178 | call-bound | 1.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 179 | camelize | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 180 | caniuse-lite | 1.0.30001819 | ui/package.json + package-lock.json | dev only, transitive | CC-BY-4.0 | **legal review** |
| 181 | chai | 4.5.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 182 | chalk | 4.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 183 | check-error | 1.0.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 184 | chrome-trace-event | 1.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 185 | classnames | 2.5.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 186 | clone-deep | 4.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 187 | color-convert | 2.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 188 | color-name | 1.1.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 189 | colorette | 2.0.20 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 190 | combined-stream | 1.0.8 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 191 | commander | 2.20.3 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 192 | compute-scroll-into-view | 1.0.20 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 193 | confbox | 0.1.8 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 194 | convert-source-map | 2.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 195 | copy-to-clipboard | 3.3.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 196 | crelt | 1.0.7 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 197 | cross-spawn | 7.0.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 198 | css-color-keywords | 1.0.0 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 199 | css-to-react-native | 3.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 200 | cssstyle | 4.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 201 | rrweb-cssom | 0.8.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 202 | csstype | 3.2.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 203 | data-urls | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 204 | date-fns | 2.30.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 205 | dayjs | 1.11.23 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 206 | debug | 4.4.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 207 | decimal.js | 10.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 208 | deep-eql | 4.1.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 209 | deep-equal | 2.2.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 210 | define-data-property | 1.1.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 211 | define-properties | 1.2.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 212 | delayed-stream | 1.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 213 | diff-sequences | 29.6.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 214 | discontinuous-range | 1.0.0 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 215 | dom-accessibility-api | 0.5.16 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 216 | dom-align | 1.12.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 217 | dunder-proto | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 218 | electron-to-chromium | 1.5.464 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 219 | emojis-list | 3.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 220 | enhanced-resolve | 5.26.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 221 | entities | 6.0.1 | ui/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 222 | envinfo | 7.21.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 223 | es-define-property | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 224 | es-errors | 1.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 225 | es-get-iterator | 1.1.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 226 | es-module-lexer | 2.3.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 227 | es-object-atoms | 1.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 228 | es-set-tostringtag | 2.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 229 | esbuild | 0.24.2 | ui/package.json + package-lock.json | dev only, direct; declared ^0.24.2 (devDependencies) | MIT | permissive (review set) |
| 230 | esbuild-loader | 4.2.2 | ui/package.json + package-lock.json | dev only, direct; declared ^4.2.2 (devDependencies) | MIT | permissive (review set) |
| 231 | @esbuild/aix-ppc64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 232 | @esbuild/android-arm | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 233 | @esbuild/android-arm64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 234 | @esbuild/android-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 235 | @esbuild/darwin-arm64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 236 | @esbuild/darwin-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 237 | @esbuild/freebsd-arm64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 238 | @esbuild/freebsd-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 239 | @esbuild/linux-arm | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 240 | @esbuild/linux-arm64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 241 | @esbuild/linux-ia32 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 242 | @esbuild/linux-loong64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 243 | @esbuild/linux-mips64el | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 244 | @esbuild/linux-ppc64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 245 | @esbuild/linux-riscv64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 246 | @esbuild/linux-s390x | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 247 | @esbuild/linux-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 248 | @esbuild/netbsd-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 249 | @esbuild/openbsd-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 250 | @esbuild/sunos-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 251 | @esbuild/win32-arm64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 252 | @esbuild/win32-ia32 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 253 | @esbuild/win32-x64 | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 254 | esbuild | 0.21.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 255 | escalade | 3.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 256 | estree-walker | 3.0.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 257 | events | 3.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 258 | execa | 8.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 259 | fast-deep-equal | 3.1.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 260 | fast-uri | 3.1.9 | ui/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 261 | fastest-levenshtein | 1.0.16 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 262 | find-up | 4.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 263 | flat | 5.0.2 | ui/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 264 | for-each | 0.3.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 265 | form-data | 4.0.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 266 | fsevents | 2.3.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 267 | function-bind | 1.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 268 | functions-have-names | 1.2.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 269 | gensync | 1.0.0-beta.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 270 | get-func-name | 2.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 271 | get-intrinsic | 1.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 272 | get-proto | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 273 | get-stream | 8.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 274 | get-tsconfig | 4.14.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 275 | gopd | 1.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 276 | graceful-fs | 4.2.11 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 277 | has-bigints | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 278 | has-flag | 4.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 279 | has-property-descriptors | 1.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 280 | has-symbols | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 281 | has-tostringtag | 1.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 282 | hasown | 2.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 283 | history | 4.10.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 284 | hoist-non-react-statics | 3.3.2 | ui/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 285 | react-is | 16.13.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 286 | html-encoding-sniffer | 4.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 287 | http-proxy-agent | 7.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 288 | https-proxy-agent | 7.0.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 289 | human-signals | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 290 | iconv-lite | 0.6.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 291 | import-local | 3.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 292 | internal-slot | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 293 | interpret | 3.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 294 | is-arguments | 1.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 295 | is-array-buffer | 3.0.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 296 | is-bigint | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 297 | is-boolean-object | 1.2.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 298 | is-callable | 1.2.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 299 | is-core-module | 2.17.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 300 | is-date-object | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 301 | is-map | 2.0.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 302 | is-number-object | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 303 | is-plain-object | 2.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 304 | is-potential-custom-element-name | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 305 | is-regex | 1.2.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 306 | is-set | 2.0.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 307 | is-shared-array-buffer | 1.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 308 | is-stream | 3.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 309 | is-string | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 310 | is-symbol | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 311 | is-weakmap | 2.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 312 | is-weakset | 2.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 313 | isarray | 2.0.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 314 | isexe | 2.0.0 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 315 | isobject | 3.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 316 | jest-worker | 27.5.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 317 | supports-color | 8.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 318 | js-tokens | 4.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 319 | jsdom | 24.1.3 | ui/package.json + package-lock.json | dev only, direct; declared 24.1.3 (devDependencies) | MIT | permissive (review set) |
| 320 | jsesc | 3.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 321 | json-schema-traverse | 1.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 322 | json2mq | 0.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 323 | json5 | 2.2.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 324 | kind-of | 6.0.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 325 | loader-utils | 2.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 326 | local-pkg | 0.5.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 327 | locate-path | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 328 | lodash | 4.18.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 329 | loose-envify | 1.4.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 330 | loupe | 2.3.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 331 | lru-cache | 10.4.3 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 332 | lz-string | 1.5.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 333 | magic-string | 0.30.21 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 334 | marked | 14.1.4 | ui/package.json + package-lock.json | dev only, direct; declared ^14.1.4 (devDependencies) | MIT | permissive (review set) |
| 335 | math-intrinsics | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 336 | merge-stream | 2.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 337 | mime-db | 1.52.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 338 | mime-types | 2.1.35 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 339 | mimic-fn | 4.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 340 | minimizer-webpack-plugin | 5.13.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 341 | mlly | 1.8.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 342 | pathe | 2.0.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 343 | moment | 2.31.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 344 | moo | 0.5.3 | ui/package.json + package-lock.json | runtime, transitive | BSD-3-Clause | permissive (review set) |
| 345 | ms | 2.1.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 346 | nanoid | 3.3.20 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 347 | nearley | 2.20.1 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 348 | node-releases | 2.0.60 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 349 | npm-run-path | 5.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 350 | path-key | 4.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 351 | nwsapi | 2.2.28 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 352 | object-assign | 4.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 353 | object-inspect | 1.13.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 354 | object-is | 1.1.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 355 | object-keys | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 356 | object.assign | 4.1.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 357 | onetime | 6.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 358 | p-limit | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 359 | p-locate | 4.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 360 | p-limit | 2.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 361 | p-try | 2.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 362 | parse5 | 7.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 363 | path-exists | 4.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 364 | path-key | 3.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 365 | path-parse | 1.0.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 366 | path-to-regexp | 1.9.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 367 | isarray | 0.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 368 | pathe | 1.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 369 | pathval | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 370 | picocolors | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 371 | picomatch | 4.0.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 372 | pkg-dir | 4.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 373 | pkg-types | 1.3.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 374 | possible-typed-array-names | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 375 | postcss | 8.5.31 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 376 | postcss-value-parser | 4.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 377 | pretty-format | 27.5.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 378 | react-is | 17.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 379 | prop-types | 15.8.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 380 | psl | 1.15.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 381 | punycode | 2.3.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 382 | querystringify | 2.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 383 | railroad-diagrams | 1.0.0 | ui/package.json + package-lock.json | runtime, transitive | CC0-1.0 | permissive (review set) |
| 384 | randexp | 0.4.6 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 385 | rc-align | 4.0.15 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 386 | rc-cascader | 3.7.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 387 | rc-checkbox | 2.3.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 388 | rc-collapse | 3.4.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 389 | rc-dialog | 9.0.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 390 | rc-drawer | 6.1.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 391 | rc-dropdown | 4.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 392 | rc-field-form | 1.27.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 393 | rc-image | 5.13.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 394 | rc-input | 0.1.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 395 | rc-input-number | 7.3.11 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 396 | rc-mentions | 1.13.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 397 | rc-menu | 9.8.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 398 | rc-motion | 2.9.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 399 | rc-notification | 4.6.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 400 | rc-overflow | 1.5.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 401 | rc-pagination | 3.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 402 | rc-picker | 2.7.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 403 | rc-progress | 3.4.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 404 | rc-rate | 2.9.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 405 | rc-resize-observer | 1.4.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 406 | rc-segmented | 2.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 407 | rc-select | 14.1.18 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 408 | rc-slider | 10.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 409 | rc-steps | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 410 | rc-switch | 3.2.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 411 | rc-table | 7.26.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 412 | rc-tabs | 12.5.10 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 413 | rc-textarea | 0.4.7 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 414 | rc-tooltip | 5.2.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 415 | rc-tree | 5.7.12 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 416 | rc-tree-select | 5.5.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 417 | rc-trigger | 5.3.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 418 | rc-upload | 4.3.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 419 | rc-util | 5.44.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 420 | rc-virtual-list | 3.19.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 421 | react | 17.0.2 | ui/package.json + package-lock.json | dev only, direct; declared ^17.0.0 (peerDependencies) | MIT | permissive (review set) |
| 422 | react-dom | 17.0.2 | ui/package.json + package-lock.json | dev only, direct; declared ^17.0.0 (peerDependencies) | MIT | permissive (review set) |
| 423 | react-is | 19.3.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 424 | react-router | 5.3.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 425 | react-router-dom | 5.3.4 | ui/package.json + package-lock.json | dev only, direct; declared ^5.3.0 (peerDependencies) | MIT | permissive (review set) |
| 426 | rechoir | 0.8.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 427 | regexp.prototype.flags | 1.5.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 428 | require-from-string | 2.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 429 | requires-port | 1.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 430 | resize-observer-polyfill | 1.5.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 431 | resolve | 1.22.13 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 432 | resolve-cwd | 3.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 433 | resolve-from | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 434 | resolve-pathname | 3.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 435 | resolve-pkg-maps | 1.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 436 | ret | 0.1.15 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 437 | rollup | 4.65.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 438 | rrweb-cssom | 0.7.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 439 | safe-regex-test | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 440 | safer-buffer | 2.1.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 441 | saxes | 6.0.0 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 442 | scheduler | 0.20.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 443 | schema-utils | 4.8.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 444 | scroll-into-view-if-needed | 2.2.31 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 445 | semver | 6.3.1 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 446 | set-function-length | 1.2.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 447 | set-function-name | 2.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 448 | shallow-clone | 3.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 449 | shallowequal | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 450 | shebang-command | 2.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 451 | shebang-regex | 3.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 452 | side-channel | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 453 | side-channel-list | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 454 | side-channel-map | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 455 | side-channel-weakmap | 1.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 456 | siginfo | 2.0.0 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 457 | signal-exit | 4.1.0 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 458 | source-list-map | 2.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 459 | source-map | 0.6.1 | ui/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 460 | source-map-js | 1.2.2 | ui/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 461 | source-map-support | 0.5.21 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 462 | sql-formatter | 15.9.0 | ui/package.json + package-lock.json | runtime, direct; declared 15.9.0 (dependencies) | MIT | permissive (review set) |
| 463 | stackback | 0.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 464 | std-env | 3.10.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 465 | stop-iteration-iterator | 1.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 466 | string-convert | 0.2.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 467 | strip-final-newline | 3.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 468 | strip-literal | 2.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 469 | js-tokens | 9.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 470 | style-mod | 4.1.4 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 471 | styled-components | 5.3.11 | ui/package.json + package-lock.json | dev only, direct; declared ^5.2.1 (peerDependencies) | MIT | permissive (review set) |
| 472 | babel-plugin-styled-components | 2.3.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 473 | has-flag | 3.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 474 | supports-color | 5.5.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 475 | supports-color | 7.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 476 | supports-preserve-symlinks-flag | 1.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 477 | symbol-tree | 3.2.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 478 | tapable | 2.3.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 479 | terser | 5.51.2 | ui/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 480 | tiny-invariant | 1.3.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 481 | tiny-warning | 1.0.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 482 | tinybench | 2.9.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 483 | tinypool | 0.8.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 484 | tinyspy | 2.2.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 485 | toggle-selection | 1.0.6 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 486 | tough-cookie | 4.1.4 | ui/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 487 | tr46 | 5.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 488 | type-detect | 4.1.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 489 | typescript | 4.9.5 | ui/package.json + package-lock.json | dev only, direct; declared 4.9.5 (devDependencies) | Apache-2.0 | permissive (review set) |
| 490 | ufo | 1.6.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 491 | undici-types | 5.26.5 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 492 | universalify | 0.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 493 | update-browserslist-db | 1.3.4 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 494 | url-parse | 1.5.10 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 495 | value-equal | 1.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 496 | vite | 5.4.21 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 497 | vite-node | 1.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 498 | vitest | 1.6.0 | ui/package.json + package-lock.json | dev only, direct; declared 1.6.0 (devDependencies) | MIT | permissive (review set) |
| 499 | w3c-keyname | 2.2.8 | ui/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 500 | w3c-xmlserializer | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 501 | watchpack | 2.6.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 502 | webidl-conversions | 7.0.0 | ui/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 503 | webpack | 5.112.2 | ui/package.json + package-lock.json | dev only, direct; declared ^5.97.1 (devDependencies) | MIT | permissive (review set) |
| 504 | webpack-cli | 5.1.4 | ui/package.json + package-lock.json | dev only, direct; declared ^5.1.4 (devDependencies) | MIT | permissive (review set) |
| 505 | commander | 10.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 506 | webpack-merge | 5.10.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 507 | webpack-sources | 1.4.3 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 508 | mime-db | 1.54.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 509 | webpack-sources | 3.6.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 510 | whatwg-encoding | 3.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 511 | whatwg-mimetype | 4.0.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 512 | whatwg-url | 14.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 513 | which | 2.0.2 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 514 | which-boxed-primitive | 1.1.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 515 | which-collection | 1.0.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 516 | which-typed-array | 1.1.24 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 517 | why-is-node-running | 2.3.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 518 | wildcard | 2.0.1 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 519 | ws | 8.22.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 520 | xml-name-validator | 5.0.0 | ui/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 521 | xmlchars | 2.2.0 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 522 | yallist | 3.1.1 | ui/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 523 | yocto-queue | 1.2.2 | ui/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |

### npm (desktop)

273 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | @electron-internal/extract-zip | 1.0.8 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 2 | @electron/asar | 3.4.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 3 | balanced-match | 1.0.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 4 | brace-expansion | 1.1.21 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 5 | minimatch | 3.1.5 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 6 | @electron/fuses | 1.8.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 7 | fs-extra | 9.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 8 | @electron/get | 5.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 9 | @electron/notarize | 2.5.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 10 | @electron/osx-sign | 1.3.3 | desktop/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 11 | isbinaryfile | 4.0.10 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 12 | @electron/rebuild | 4.2.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 13 | @electron/universal | 2.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 14 | brace-expansion | 2.1.7 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 15 | fs-extra | 11.4.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 16 | minimatch | 9.0.9 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 17 | @electron/windows-sign | 1.2.2 | desktop/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 18 | @isaacs/fs-minipass | 4.0.1 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 19 | @malept/cross-spawn-promise | 2.0.0 | desktop/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 20 | @malept/flatpak-bundler | 0.4.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 21 | @noble/hashes | 1.8.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 22 | @peculiar/asn1-schema | 2.11.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 23 | @peculiar/json-schema | 1.1.12 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 24 | @peculiar/utils | 2.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 25 | @peculiar/webcrypto | 1.7.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 26 | @sindresorhus/is | 4.6.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 27 | @szmarczak/http-timer | 4.0.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 28 | @types/cacheable-request | 6.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 29 | @types/debug | 4.1.13 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 30 | @types/fs-extra | 9.0.13 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 31 | @types/http-cache-semantics | 4.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 32 | @types/keyv | 3.1.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 33 | @types/ms | 2.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 34 | @types/node | 24.19.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 35 | @types/responselike | 1.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 36 | @xmldom/xmldom | 0.8.15 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 37 | abbrev | 4.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 38 | agent-base | 7.1.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 39 | ajv | 8.20.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 40 | ansi-regex | 5.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 41 | ansi-styles | 4.3.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 42 | app-builder-lib | 26.18.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 43 | @electron/get | 3.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 44 | fs-extra | 8.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 45 | semver | 6.3.2 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 46 | ci-info | 4.3.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 47 | env-paths | 2.2.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 48 | jsonfile | 4.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 49 | semver | 7.7.4 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 50 | universalify | 0.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 51 | argparse | 2.0.1 | desktop/package.json + package-lock.json | runtime, transitive | Python-2.0 | permissive (review set) |
| 52 | asn1js | 3.1.0 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 53 | async | 3.2.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 54 | async-exit-hook | 2.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 55 | asynckit | 0.4.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 56 | at-least-node | 1.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 57 | aws4 | 1.13.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 58 | balanced-match | 4.0.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 59 | base64-js | 1.5.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 60 | bluebird | 3.7.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 61 | boolean | 3.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 62 | brace-expansion | 5.0.12 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 63 | buffer-from | 1.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 64 | builder-util | 26.16.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 65 | builder-util-runtime | 9.7.0 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 66 | bytestreamjs | 2.0.1 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 67 | cacheable-lookup | 5.0.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 68 | cacheable-request | 7.0.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 69 | call-bind-apply-helpers | 1.0.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 70 | chalk | 4.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 71 | chownr | 3.0.0 | desktop/package.json + package-lock.json | dev only, transitive | BlueOak-1.0.0 | permissive (review set) |
| 72 | chromium-pickle-js | 0.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 73 | ci-info | 4.4.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 74 | cliui | 8.0.1 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 75 | clone-response | 1.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 76 | color-convert | 2.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 77 | color-name | 1.1.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 78 | combined-stream | 1.0.8 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 79 | commander | 5.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 80 | compare-version | 0.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 81 | concat-map | 0.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 82 | core-util-is | 1.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 83 | cross-dirname | 0.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 84 | cross-spawn | 7.0.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 85 | isexe | 2.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 86 | which | 2.0.2 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 87 | debug | 4.4.3 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 88 | decompress-response | 6.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 89 | mimic-response | 3.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 90 | defer-to-connect | 2.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 91 | define-data-property | 1.1.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 92 | define-properties | 1.2.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 93 | delayed-stream | 1.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 94 | detect-node | 2.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 95 | dir-compare | 4.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 96 | dmg-builder | 26.18.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 97 | dotenv | 16.6.1 | desktop/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 98 | dotenv-expand | 11.0.7 | desktop/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 99 | dunder-proto | 1.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 100 | duplexer2 | 0.1.4 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 101 | ejs | 3.1.10 | desktop/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 102 | electron | 45.2.3 | desktop/package.json + package-lock.json | dev only, direct; declared 45.2.3 (devDependencies) | MIT | permissive (review set) |
| 103 | electron-builder | 26.18.0 | desktop/package.json + package-lock.json | dev only, direct; declared 26.18.0 (devDependencies) | MIT | permissive (review set) |
| 104 | electron-builder-squirrel-windows | 26.18.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 105 | electron-publish | 26.18.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 106 | electron-updater | 6.8.11 | desktop/package.json + package-lock.json | runtime, direct; declared 6.8.11 (dependencies) | MIT | permissive (review set) |
| 107 | electron-winstaller | 5.4.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 108 | fs-extra | 7.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 109 | emoji-regex | 8.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 110 | end-of-stream | 1.4.5 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 111 | env-paths | 3.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 112 | err-code | 2.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 113 | es-define-property | 1.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 114 | es-errors | 1.3.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 115 | es-object-atoms | 1.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 116 | es-set-tostringtag | 2.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 117 | es6-error | 4.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 118 | escalade | 3.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 119 | escape-string-regexp | 4.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 120 | exponential-backoff | 3.1.3 | desktop/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 121 | fast-deep-equal | 3.1.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 122 | fast-uri | 3.1.9 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 123 | fdir | 6.5.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 124 | filelist | 1.0.6 | desktop/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 125 | minimatch | 5.1.9 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 126 | form-data | 4.0.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 127 | fs-extra | 10.1.0 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 128 | fs.realpath | 1.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 129 | function-bind | 1.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 130 | get-caller-file | 2.0.5 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 131 | get-intrinsic | 1.3.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 132 | get-proto | 1.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 133 | get-stream | 5.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 134 | glob | 7.2.3 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 135 | global-agent | 3.0.0 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 136 | globalthis | 1.0.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 137 | gopd | 1.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 138 | got | 11.8.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 139 | graceful-fs | 4.2.11 | desktop/package.json + package-lock.json | runtime, transitive | ISC | permissive (review set) |
| 140 | has-flag | 4.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 141 | has-property-descriptors | 1.0.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 142 | has-symbols | 1.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 143 | has-tostringtag | 1.0.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 144 | hasown | 2.0.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 145 | hosted-git-info | 4.1.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 146 | http-cache-semantics | 4.3.0 | desktop/package.json + package-lock.json | dev only, transitive | BSD-2-Clause | permissive (review set) |
| 147 | http-proxy-agent | 7.0.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 148 | http2-wrapper | 1.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 149 | https-proxy-agent | 7.0.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 150 | inflight | 1.0.6 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 151 | inherits | 2.0.4 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 152 | is-fullwidth-code-point | 3.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 153 | isarray | 1.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 154 | isbinaryfile | 5.0.7 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 155 | isexe | 3.1.5 | desktop/package.json + package-lock.json | dev only, transitive | BlueOak-1.0.0 | permissive (review set) |
| 156 | jake | 10.9.4 | desktop/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 157 | jiti | 2.7.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 158 | js-yaml | 4.3.2 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 159 | json-buffer | 3.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 160 | json-schema-traverse | 1.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 161 | json-stringify-safe | 5.0.1 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 162 | json5 | 2.2.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 163 | jsonfile | 6.2.1 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 164 | keyv | 4.5.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 165 | lazy-val | 1.0.5 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 166 | lodash | 4.18.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 167 | lodash.escaperegexp | 4.1.2 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 168 | lodash.isequal | 4.5.0 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 169 | lowercase-keys | 2.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 170 | lru-cache | 6.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 171 | matcher | 3.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 172 | math-intrinsics | 1.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 173 | mime | 2.6.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 174 | mime-db | 1.52.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 175 | mime-types | 2.1.35 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 176 | mimic-response | 1.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 177 | minimatch | 10.2.6 | desktop/package.json + package-lock.json | dev only, transitive | BlueOak-1.0.0 | permissive (review set) |
| 178 | minimist | 1.2.8 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 179 | minipass | 7.1.3 | desktop/package.json + package-lock.json | dev only, transitive | BlueOak-1.0.0 | permissive (review set) |
| 180 | minizlib | 3.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 181 | mkdirp | 0.5.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 182 | ms | 2.1.3 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 183 | node-abi | 4.40.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 184 | node-api-version | 0.2.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 185 | node-gyp | 12.4.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 186 | isexe | 4.0.0 | desktop/package.json + package-lock.json | dev only, transitive | BlueOak-1.0.0 | permissive (review set) |
| 187 | undici | 6.29.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 188 | which | 6.0.1 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 189 | node-int64 | 0.4.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 190 | nopt | 9.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 191 | normalize-url | 6.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 192 | object-keys | 1.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 193 | once | 1.4.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 194 | p-cancelable | 2.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 195 | p-limit | 3.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 196 | path-is-absolute | 1.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 197 | path-key | 3.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 198 | pe-library | 0.4.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 199 | picocolors | 1.1.1 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 200 | picomatch | 4.0.7 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 201 | pkijs | 3.4.3 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 202 | playwright-core | 1.64.1 | desktop/package.json + package-lock.json | dev only, direct; declared 1.64.1 (devDependencies) | Apache-2.0 | permissive (review set) |
| 203 | plist | 3.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 204 | postject | 1.0.0-alpha.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 205 | commander | 9.5.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 206 | proc-log | 6.1.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 207 | process-nextick-args | 2.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 208 | progress | 2.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 209 | promise-retry | 2.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 210 | proper-lockfile | 4.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 211 | pump | 3.0.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 212 | pvtsutils | 1.3.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 213 | pvutils | 1.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 214 | quick-lru | 5.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 215 | read-binary-file-arch | 1.0.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 216 | readable-stream | 2.3.8 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 217 | require-directory | 2.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 218 | require-from-string | 2.0.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 219 | resedit | 1.7.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 220 | resolve-alpn | 1.2.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 221 | responselike | 2.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 222 | retry | 0.12.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 223 | rimraf | 2.6.3 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 224 | roarr | 2.15.4 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 225 | safe-buffer | 5.1.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 226 | sanitize-filename | 1.6.5 | desktop/package.json + package-lock.json | dev only, transitive | WTFPL OR ISC | **legal review** |
| 227 | sax | 1.6.1 | desktop/package.json + package-lock.json | runtime, transitive | BlueOak-1.0.0 | permissive (review set) |
| 228 | semver | 7.8.6 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 229 | semver-compare | 1.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 230 | serialize-error | 7.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 231 | shebang-command | 2.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 232 | shebang-regex | 3.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 233 | signal-exit | 3.0.7 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 234 | simple-update-notifier | 2.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 235 | source-map | 0.6.1 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 236 | source-map-support | 0.5.21 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 237 | sprintf-js | 1.1.3 | desktop/package.json + package-lock.json | dev only, transitive | BSD-3-Clause | permissive (review set) |
| 238 | stat-mode | 1.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 239 | string_decoder | 1.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 240 | string-width | 4.2.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 241 | strip-ansi | 6.0.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 242 | sumchecker | 3.0.1 | desktop/package.json + package-lock.json | dev only, transitive | Apache-2.0 | permissive (review set) |
| 243 | supports-color | 7.2.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 244 | tar | 7.5.22 | desktop/package.json + package-lock.json | dev only, transitive | BlueOak-1.0.0 | permissive (review set) |
| 245 | yallist | 5.0.0 | desktop/package.json + package-lock.json | dev only, transitive | BlueOak-1.0.0 | permissive (review set) |
| 246 | temp | 0.9.4 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 247 | temp-file | 3.4.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 248 | tiny-async-pool | 1.3.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 249 | semver | 5.7.3 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 250 | tiny-typed-emitter | 2.1.0 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 251 | tinyglobby | 0.2.17 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 252 | tmp | 0.2.7 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 253 | tmp-promise | 3.0.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 254 | truncate-utf8-bytes | 1.0.2 | desktop/package.json + package-lock.json | dev only, transitive | WTFPL | **legal review** |
| 255 | tslib | 2.9.1 | desktop/package.json + package-lock.json | dev only, transitive | 0BSD | permissive (review set) |
| 256 | type-fest | 0.13.1 | desktop/package.json + package-lock.json | dev only, transitive | (MIT OR CC0-1.0) | permissive (review set) |
| 257 | undici | 7.30.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 258 | undici-types | 7.24.6 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 259 | universalify | 2.0.1 | desktop/package.json + package-lock.json | runtime, transitive | MIT | permissive (review set) |
| 260 | unzipper | 0.12.5 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 261 | fs-extra | 11.3.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 262 | utf8-byte-length | 1.0.5 | desktop/package.json + package-lock.json | dev only, transitive | (WTFPL OR MIT) | **legal review** |
| 263 | util-deprecate | 1.0.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 264 | webcrypto-core | 1.9.2 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 265 | which | 5.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 266 | wrap-ansi | 7.0.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 267 | wrappy | 1.0.2 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 268 | xmlbuilder | 15.1.1 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 269 | y18n | 5.0.8 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 270 | yallist | 4.0.0 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 271 | yargs | 17.7.3 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |
| 272 | yargs-parser | 21.1.1 | desktop/package.json + package-lock.json | dev only, transitive | ISC | permissive (review set) |
| 273 | yocto-queue | 0.1.0 | desktop/package.json + package-lock.json | dev only, transitive | MIT | permissive (review set) |

### npm (addins)

1 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | (none declared) |  | addins/package.json | none; no third-party packages declared; no lock file in the repository | n/a | permissive (review set) |

### npm (extension)

1 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | (none declared) |  | extension/package.json | none; no third-party packages declared; no lock file in the repository | n/a | permissive (review set) |

### npm (sdks/js)

1 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | (none declared) |  | sdks/js/package.json | none; no third-party packages declared; no lock file in the repository | Apache-2.0 | permissive (review set) |

### Rust crates (store, catalog, ontology)

561 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | adler2 | 2.0.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 2 | aead | 0.5.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 3 | aead | 0.6.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 4 | aes | 0.8.4 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 5 | aes | 0.9.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 6 | aes-gcm | 0.10.3 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 7 | aes-gcm | 0.11.1 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 8 | ahash | 0.7.8 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 9 | ahash | 0.8.12 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 10 | aho-corasick | 1.1.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 11 | alloc-no-stdlib | 2.0.4 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 12 | alloc-stdlib | 0.2.4 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 13 | android_system_properties | 0.1.6 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 14 | anyhow | 1.0.104 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 15 | apache-avro | 0.21.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 16 | ar_archive_writer | 0.5.3 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 17 | argon2 | 0.6.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 18 | array-init | 2.1.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 19 | arrow-arith | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 20 | arrow-array | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 21 | arrow-buffer | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 22 | arrow-cast | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 23 | arrow-data | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 24 | arrow-ipc | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 25 | arrow-ord | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 26 | arrow-schema | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 27 | arrow-select | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 28 | arrow-string | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 29 | as-any | 0.3.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 30 | async-lock | 3.4.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 31 | async-trait | 0.1.92 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 32 | atoi | 2.0.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 33 | atomic-waker | 1.1.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 34 | autocfg | 1.5.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 35 | axum | 0.8.9 | ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 36 | axum-core | 0.5.6 | ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 37 | backon | 1.6.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 38 | base16ct | 1.0.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 39 | base64 | 0.22.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 40 | base64 | 0.23.1 | catalog/Cargo.lock, ontology/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 41 | base64ct | 1.8.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 42 | bcrypt-pbkdf | 0.11.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 43 | bigdecimal | 0.4.11 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 44 | bimap | 0.6.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 45 | bitflags | 1.3.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 46 | bitflags | 2.13.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 47 | blake2 | 0.11.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 48 | block-buffer | 0.10.4 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 49 | block-buffer | 0.12.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 50 | block-padding | 0.4.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 51 | blowfish | 0.10.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 52 | bnum | 0.12.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 53 | bon | 3.10.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 54 | bon-macros | 3.10.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 55 | brotli | 8.0.4 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 56 | brotli-decompressor | 5.0.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 57 | bs58 | 0.5.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 58 | bumpalo | 3.20.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 59 | bytemuck | 1.25.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 60 | byteorder | 1.5.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 61 | byteorder-lite | 0.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 62 | bytes | 1.12.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 63 | cbc | 0.2.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 64 | cc | 1.10.0 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 65 | cc | 1.11.1 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 66 | cfg-if | 1.0.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 67 | cfg_aliases | 0.2.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 68 | chacha20 | 0.10.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 69 | chrono | 0.4.45 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 70 | cipher | 0.4.4 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 71 | cipher | 0.5.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 72 | cmov | 0.5.4 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 73 | color_quant | 1.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 74 | const-oid | 0.10.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 75 | const-oid | 0.9.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 76 | const-random | 0.1.18 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 77 | const-random-macro | 0.1.16 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 78 | core-foundation-sys | 0.8.7 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 79 | cpubits | 0.1.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 80 | cpufeatures | 0.2.17 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 81 | cpufeatures | 0.3.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 82 | crc32c | 0.6.8 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 83 | crc32fast | 1.5.3 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 84 | crossbeam-channel | 0.5.17 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 85 | crossbeam-epoch | 0.9.21 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 86 | crossbeam-utils | 0.8.23 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 87 | crunchy | 0.2.4 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 88 | crypto-bigint | 0.7.10 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 89 | crypto-common | 0.1.7 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 90 | crypto-common | 0.2.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 91 | ctr | 0.10.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 92 | ctr | 0.9.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 93 | ctutils | 0.4.7 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 94 | ctutils | 0.4.8 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 95 | curve25519-dalek | 5.0.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 96 | curve25519-dalek-derive | 0.1.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 97 | darling | 0.20.11 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 98 | darling | 0.24.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 99 | darling_core | 0.20.11 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 100 | darling_core | 0.24.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 101 | darling_macro | 0.20.11 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 102 | darling_macro | 0.24.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 103 | dashmap | 6.2.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 104 | data-encoding | 2.11.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 105 | defmt | 1.2.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 106 | defmt-macros | 1.2.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 107 | defmt-parser | 1.1.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 108 | delegate | 0.13.5 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 109 | der | 0.7.10 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 110 | der | 0.8.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 111 | der_derive | 0.7.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 112 | deranged | 0.5.9 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 113 | derive_builder | 0.20.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 114 | derive_builder_core | 0.20.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 115 | derive_builder_macro | 0.20.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 116 | des | 0.9.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 117 | digest | 0.10.7 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 118 | digest | 0.11.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 119 | displaydoc | 0.2.7 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 120 | dissimilar | 1.0.11 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 121 | dyn-clone | 1.0.20 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 122 | ecdsa | 0.17.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 123 | ed25519 | 3.0.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 124 | ed25519-dalek | 3.0.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 125 | either | 1.19.0 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 126 | elliptic-curve | 0.14.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 127 | enum_dispatch | 0.3.13 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 128 | equivalent | 1.0.3 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 129 | erased-serde | 0.4.11 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 130 | errno | 0.3.14 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 131 | event-listener | 5.4.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 132 | event-listener-strategy | 0.5.4 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 133 | expect-test | 1.5.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 134 | fallible-iterator | 0.2.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 135 | fallible-iterator | 0.3.0 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 136 | fallible-streaming-iterator | 0.1.9 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 137 | fastnum | 0.7.5 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 138 | fastrand | 2.5.0 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 139 | fdeflate | 0.3.7 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 140 | ff | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 141 | fiat-crypto | 0.3.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 142 | find-msvc-tools | 0.1.15 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 143 | flagset | 0.4.7 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 144 | flatbuffers | 25.12.19 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 145 | flate2 | 1.1.10 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 146 | fnv | 1.0.7 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 147 | foldhash | 0.1.5 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 148 | form_urlencoded | 1.2.2 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 149 | fuser | 0.18.0 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 150 | futures | 0.3.34 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 151 | futures-channel | 0.3.34 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 152 | futures-core | 0.3.34 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 153 | futures-executor | 0.3.34 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 154 | futures-io | 0.3.34 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 155 | futures-macro | 0.3.34 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 156 | futures-sink | 0.3.34 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 157 | futures-task | 0.3.34 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 158 | futures-util | 0.3.34 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 159 | generic-array | 0.14.7 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 160 | generic-array | 1.4.5 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 161 | getrandom | 0.2.17 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 162 | getrandom | 0.3.4 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 163 | getrandom | 0.4.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 164 | ghash | 0.5.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 165 | ghash | 0.6.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 166 | gif | 0.14.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 167 | glob-match | 0.2.1 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 168 | gloo-timers | 0.3.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 169 | gloo-timers | 0.4.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 170 | group | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 171 | h2 | 0.4.20 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 172 | half | 2.7.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 173 | hashbrown | 0.12.3 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 174 | hashbrown | 0.14.5 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 175 | hashbrown | 0.15.5 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 176 | hashbrown | 0.17.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 177 | hashlink | 0.10.0 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 178 | heck | 0.5.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 179 | hex | 0.4.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 180 | hex-literal | 1.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 181 | hkdf | 0.13.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 182 | hmac | 0.12.1 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 183 | hmac | 0.13.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 184 | http | 1.5.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 185 | http-body | 1.1.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 186 | http-body-util | 0.1.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 187 | httparse | 1.10.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 188 | httpdate | 1.0.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 189 | hybrid-array | 0.4.15 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 190 | hyper | 1.12.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 191 | hyper-rustls | 0.27.10 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 192 | hyper-timeout | 0.5.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 193 | hyper-util | 0.1.21 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 194 | iana-time-zone | 0.1.65 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 195 | iana-time-zone-haiku | 0.1.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 196 | iceberg | 0.11.0 | catalog/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 197 | iceberg-property-macro | 0.11.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 198 | icu_collections | 2.4.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 199 | icu_locale_core | 2.4.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 200 | icu_normalizer | 2.4.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 201 | icu_normalizer_data | 2.4.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 202 | icu_properties | 2.4.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 203 | icu_properties_data | 2.4.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 204 | icu_provider | 2.4.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 205 | ident_case | 1.0.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 206 | idna | 1.1.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 207 | idna_adapter | 1.2.2 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 208 | image | 0.25.10 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 209 | image-webp | 0.2.4 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 210 | indexmap | 1.9.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 211 | indexmap | 2.14.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 212 | inout | 0.1.4 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 213 | inout | 0.2.5 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 214 | instant | 0.1.13 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 215 | integer-encoding | 3.0.4 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 216 | integer-encoding | 4.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 217 | inventory | 0.3.25 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 218 | ipnet | 2.12.2 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 219 | itertools | 0.13.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 220 | itertools | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 221 | itoa | 1.0.18 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 222 | jiff | 0.2.38 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 223 | jiff-core | 0.1.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 224 | jiff-static | 0.2.38 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 225 | jiff-tzdb | 0.1.9 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 226 | jiff-tzdb-platform | 0.1.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 227 | jobserver | 0.1.35 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 228 | js-sys | 0.3.107 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 229 | keccak | 0.2.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 230 | kem | 0.3.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 231 | lazy_static | 1.5.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 232 | lexical-core | 1.0.6 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 233 | lexical-parse-float | 1.0.6 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 234 | lexical-parse-integer | 1.0.6 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 235 | lexical-util | 1.0.7 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 236 | lexical-write-float | 1.0.6 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 237 | lexical-write-integer | 1.0.6 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 238 | libc | 0.2.191 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 239 | libm | 0.2.16 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 240 | libredox | 0.1.25 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 241 | libsqlite3-sys | 0.35.0 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 242 | linux-raw-sys | 0.12.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 243 | litemap | 0.8.3 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 244 | lock_api | 0.4.14 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 245 | log | 0.4.35 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 246 | lru | 0.7.8 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 247 | lru-slab | 0.1.3 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 248 | lz4_flex | 0.13.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 249 | matchers | 0.2.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 250 | matchit | 0.8.4 | ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 251 | md-5 | 0.10.6 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 252 | md-5 | 0.11.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 253 | md5 | 0.8.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 254 | memchr | 2.8.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 255 | memoffset | 0.9.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 256 | mime | 0.3.17 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 257 | mime | 0.3.18 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 258 | miniz_oxide | 0.8.9 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 259 | miniz_oxide | 0.9.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 260 | mio | 1.2.4 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 261 | ml-kem | 0.3.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 262 | module-lattice | 0.2.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 263 | moka | 0.12.16 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 264 | moxcms | 0.8.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 265 | murmur3 | 0.5.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 266 | nix | 0.31.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 267 | nu-ansi-term | 0.50.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 268 | num-bigint | 0.4.8 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 269 | num-bigint | 0.5.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 270 | num-complex | 0.4.6 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 271 | num-conv | 0.2.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 272 | num-integer | 0.1.47 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 273 | num-traits | 0.2.19 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 274 | num_enum | 0.7.6 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 275 | num_enum_derive | 0.7.6 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 276 | objc2-core-foundation | 0.3.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 277 | objc2-system-configuration | 0.3.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 278 | object | 0.39.1 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 279 | once_cell | 1.21.4 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 280 | opaque-debug | 0.3.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 281 | ordered-float | 3.9.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 282 | ordered-float | 4.6.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 283 | p256 | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 284 | p384 | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 285 | p521 | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 286 | page_size | 0.6.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 287 | pageant | 0.2.5 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 288 | parking | 2.2.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 289 | parking_lot | 0.11.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 290 | parking_lot | 0.12.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 291 | parking_lot_core | 0.8.6 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 292 | parking_lot_core | 0.9.12 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 293 | parquet | 58.4.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 294 | password-hash | 0.6.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 295 | paste | 1.0.15 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 296 | pbkdf2 | 0.12.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 297 | pbkdf2 | 0.13.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 298 | pem-rfc7468 | 1.0.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 299 | percent-encoding | 2.3.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 300 | phc | 0.6.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 301 | phf | 0.13.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 302 | phf_shared | 0.13.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 303 | pin-project | 1.1.13 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 304 | pin-project-internal | 1.1.13 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 305 | pin-project-lite | 0.2.17 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 306 | pkcs5 | 0.8.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 307 | pkcs8 | 0.11.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 308 | pkg-config | 0.3.34 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 309 | png | 0.18.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 310 | poly1305 | 0.9.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 311 | polyval | 0.6.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 312 | polyval | 0.7.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 313 | portable-atomic | 1.15.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 314 | portable-atomic-util | 0.2.8 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 315 | postgres | 0.19.14 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 316 | postgres-protocol | 0.6.12 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 317 | postgres-types | 0.2.14 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 318 | potential_utf | 0.1.6 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 319 | powerfmt | 0.2.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 320 | ppv-lite86 | 0.2.21 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 321 | prettyplease | 0.3.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 322 | primefield | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 323 | primeorder | 0.14.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 324 | proc-macro-crate | 3.5.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 325 | proc-macro2 | 1.0.107 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 326 | prost | 0.14.4 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 327 | prost-derive | 0.14.4 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 328 | prost-types | 0.14.4 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 329 | psm | 0.1.32 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 330 | pxfm | 0.1.30 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 331 | quad-rand | 0.2.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 332 | quick-error | 2.0.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 333 | quinn | 0.11.12 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 334 | quinn-proto | 0.11.19 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 335 | quinn-udp | 0.5.16 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 336 | quote | 1.0.47 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 337 | r-efi | 5.3.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 338 | r-efi | 6.0.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 339 | rand | 0.10.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 340 | rand | 0.8.8 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 341 | rand | 0.9.5 | catalog/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 342 | rand_chacha | 0.3.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 343 | rand_chacha | 0.9.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 344 | rand_core | 0.10.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 345 | rand_core | 0.6.4 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 346 | rand_core | 0.9.5 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 347 | rand_pcg | 0.10.2 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 348 | recursive | 0.1.1 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 349 | recursive-proc-macro-impl | 0.1.1 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 350 | redb | 2.6.4 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 351 | redox_syscall | 0.2.16 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 352 | redox_syscall | 0.5.18 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 353 | reed-solomon-erasure | 6.0.0 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 354 | ref-cast | 1.0.27 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 355 | ref-cast-impl | 1.0.27 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 356 | regex | 1.13.1 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 357 | regex-automata | 0.4.18 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 358 | regex-lite | 0.1.9 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 359 | regex-syntax | 0.8.11 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 360 | reqwest | 0.12.28 | catalog/Cargo.lock, ontology/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 361 | rfc6979 | 0.6.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 362 | ring | 0.17.14 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 363 | roaring | 0.11.5 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 364 | rsasl | 2.3.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 365 | rskafka | 0.6.0 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 366 | rusqlite | 0.37.0 | ontology/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 367 | russh | 0.65.0 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 368 | russh-cryptovec | 0.62.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 369 | russh-sftp | 3.0.1 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 370 | russh-util | 0.52.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 371 | rustc-hash | 2.1.3 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 372 | rustc_version | 0.4.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 373 | rustix | 1.1.6 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 374 | rustls | 0.23.46 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 375 | rustls-pki-types | 1.15.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 376 | rustls-webpki | 0.103.16 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 377 | rustversion | 1.0.23 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 378 | ryu | 1.0.23 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 379 | salsa20 | 0.11.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 380 | schemars | 0.9.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 381 | schemars | 1.2.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 382 | scopeguard | 1.2.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 383 | scrypt | 0.12.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 384 | sec1 | 0.8.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 385 | semver | 1.0.28 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 386 | seq-macro | 0.3.6 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 387 | serde | 1.0.229 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 388 | serde-big-array | 0.5.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 389 | serde_bytes | 0.11.19 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 390 | serde_core | 1.0.229 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 391 | serde_derive | 1.0.229 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 392 | serde_json | 1.0.154 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 393 | serde_path_to_error | 0.1.20 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 394 | serde_repr | 0.1.21 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 395 | serde_urlencoded | 0.7.1 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 396 | serde_with | 3.24.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 397 | serde_with_macros | 3.24.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 398 | serdect | 0.4.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 399 | sha1 | 0.10.7 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 400 | sha1 | 0.11.0 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 401 | sha2 | 0.10.9 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 402 | sha2 | 0.11.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 403 | sha3 | 0.12.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 404 | shake | 0.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 405 | sharded-slab | 0.1.7 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 406 | shlex | 2.0.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 407 | signal-hook-registry | 1.4.8 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 408 | signature | 3.0.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 409 | simd-adler32 | 0.3.10 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 410 | simdutf8 | 0.1.5 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 411 | siphasher | 1.0.4 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 412 | slab | 0.4.12 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 413 | smallvec | 1.16.4 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 414 | smallvec | 1.16.5 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 415 | snap | 1.1.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 416 | socket2 | 0.6.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 417 | spin | 0.9.9 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 418 | spki | 0.7.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 419 | spki | 0.8.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 420 | sponge-cursor | 0.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 421 | sqlparser | 0.59.0 | ontology/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 422 | sqlparser_derive | 0.3.0 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 423 | ssh-cipher | 0.3.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 424 | ssh-encoding | 0.3.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 425 | ssh-key | 0.7.0-rc.11 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 426 | stable_deref_trait | 1.2.1 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 427 | stacker | 0.1.25 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 428 | stringprep | 0.1.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 429 | strsim | 0.11.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 430 | strum | 0.27.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 431 | strum_macros | 0.27.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 432 | subtle | 2.6.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 433 | syn | 2.0.119 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 434 | syn | 3.0.9 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 435 | sync_wrapper | 1.0.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 436 | synstructure | 0.14.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 437 | tagptr | 0.2.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 438 | tempfile | 3.27.0 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 439 | thiserror | 1.0.69 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 440 | thiserror | 2.0.21 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 441 | thiserror-impl | 1.0.69 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 442 | thiserror-impl | 2.0.21 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 443 | thread_local | 1.1.10 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 444 | thrift | 0.23.0 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 445 | time | 0.3.55 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 446 | time-core | 0.1.9 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 447 | time-macros | 0.2.32 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 448 | tiny-keccak | 2.0.2 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 449 | tinystr | 0.8.5 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 450 | tinyvec | 1.13.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 451 | tls_codec | 0.4.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 452 | tls_codec_derive | 0.4.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 453 | tokio | 1.54.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 454 | tokio-macros | 2.7.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 455 | tokio-postgres | 0.7.18 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 456 | tokio-postgres-rustls | 0.13.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 457 | tokio-rustls | 0.26.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 458 | tokio-stream | 0.1.19 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 459 | tokio-util | 0.7.20 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 460 | toml_datetime | 1.1.2+spec-1.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 461 | toml_edit | 0.25.17+spec-1.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 462 | toml_parser | 1.1.5+spec-1.1.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 463 | tonic | 0.14.9 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 464 | tonic-prost | 0.14.9 | store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 465 | tower | 0.5.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 466 | tower-http | 0.6.11 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 467 | tower-layer | 0.3.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 468 | tower-service | 0.3.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 469 | tracing | 0.1.44 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 470 | tracing-attributes | 0.1.31 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 471 | tracing-core | 0.1.36 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 472 | tracing-log | 0.2.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 473 | tracing-serde | 0.2.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 474 | tracing-subscriber | 0.3.23 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 475 | try-lock | 0.2.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 476 | twox-hash | 2.1.5 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 477 | typed-builder | 0.20.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 478 | typed-builder-macro | 0.20.1 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 479 | typeid | 1.0.3 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 480 | typenum | 1.20.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 481 | typetag | 0.2.23 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 482 | typetag-impl | 0.2.23 | catalog/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 483 | unicode-bidi | 0.3.18 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 484 | unicode-ident | 1.0.27 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 485 | unicode-normalization | 0.1.26 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 486 | unicode-properties | 0.1.4 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 487 | unicode-properties | 0.1.5 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 488 | universal-hash | 0.5.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 489 | universal-hash | 0.6.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 490 | untrusted | 0.9.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 491 | url | 2.5.8 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 492 | utf8_iter | 1.1.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 493 | utf_types | 1.0.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 494 | uuid | 1.29.0 | catalog/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 495 | uuid | 1.29.1 | ontology/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 496 | valuable | 0.1.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 497 | vcpkg | 0.2.15 | ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 498 | version_check | 0.9.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 499 | want | 0.3.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 500 | wasi | 0.11.1+wasi-snapshot-preview1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 501 | wasi | 0.14.7+wasi-0.2.4 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 502 | wasip2 | 1.0.4+wasi-0.2.12 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 503 | wasite | 1.0.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 504 | wasm-bindgen | 0.2.130 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 505 | wasm-bindgen-futures | 0.4.80 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 506 | wasm-bindgen-macro | 0.2.130 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 507 | wasm-bindgen-macro-support | 0.2.130 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 508 | wasm-bindgen-shared | 0.2.130 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 509 | web-sys | 0.3.107 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 510 | web-time | 1.1.0 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 511 | webpki-roots | 1.0.11 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | direct; crates.io registry | CDLA-Permissive-2.0 (per notices) | **legal review** |
| 512 | weezl | 0.1.12 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 513 | whoami | 2.1.3 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 514 | winapi | 0.3.9 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 515 | winapi-i686-pc-windows-gnu | 0.4.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 516 | winapi-x86_64-pc-windows-gnu | 0.4.0 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 517 | windows | 0.62.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 518 | windows-collections | 0.3.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 519 | windows-core | 0.62.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 520 | windows-future | 0.3.2 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 521 | windows-implement | 0.60.2 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 522 | windows-interface | 0.59.3 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 523 | windows-link | 0.2.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 524 | windows-numerics | 0.3.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 525 | windows-result | 0.4.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 526 | windows-strings | 0.5.1 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 527 | windows-sys | 0.52.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 528 | windows-sys | 0.61.2 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 529 | windows-targets | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 530 | windows-threading | 0.2.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 531 | windows_aarch64_gnullvm | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 532 | windows_aarch64_msvc | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 533 | windows_i686_gnu | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 534 | windows_i686_gnullvm | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 535 | windows_i686_msvc | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 536 | windows_x86_64_gnu | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 537 | windows_x86_64_gnullvm | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 538 | windows_x86_64_msvc | 0.52.6 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 539 | winnow | 1.0.4 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 540 | wit-bindgen | 0.57.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 541 | wnaf | 0.14.1 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 542 | writeable | 0.6.4 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 543 | x509-cert | 0.2.5 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 544 | yoke | 0.8.4 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 545 | yoke-derive | 0.8.4 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 546 | zerocopy | 0.8.63 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 547 | zerocopy-derive | 0.8.63 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 548 | zerofrom | 0.1.8 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 549 | zerofrom-derive | 0.1.8 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 550 | zeroize | 1.9.1 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 551 | zeroize_derive | 1.5.0 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 552 | zerotrie | 0.2.5 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 553 | zerovec | 0.11.9 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 554 | zerovec-derive | 0.11.7 | catalog/Cargo.lock, ontology/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 555 | zlib-rs | 0.6.8 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 556 | zmij | 1.0.23 | catalog/Cargo.lock, ontology/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 557 | zstd | 0.13.3 | catalog/Cargo.lock, store/Cargo.lock | direct; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 558 | zstd-safe | 7.3.0 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 559 | zstd-sys | 2.1.2+zstd.1.5.7 | catalog/Cargo.lock, store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 560 | zune-core | 0.5.3 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |
| 561 | zune-jpeg | 0.5.15 | store/Cargo.lock | transitive; crates.io registry | [TO CONFIRM: licence] | **unknown** |

### Go modules (terraform-provider-cairn)

23 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | github.com/hashicorp/terraform-plugin-framework | v1.15.1 | terraform-provider-cairn/go.mod | direct | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 2 | github.com/fatih/color | v1.13.0 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 3 | github.com/golang/protobuf | v1.5.4 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 4 | github.com/hashicorp/go-hclog | v1.5.0 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 5 | github.com/hashicorp/go-plugin | v1.6.3 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 6 | github.com/hashicorp/go-uuid | v1.0.3 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 7 | github.com/hashicorp/terraform-plugin-go | v0.27.0 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 8 | github.com/hashicorp/terraform-plugin-log | v0.9.0 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 9 | github.com/hashicorp/terraform-registry-address | v0.2.5 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 10 | github.com/hashicorp/terraform-svchost | v0.1.1 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 11 | github.com/hashicorp/yamux | v0.1.1 | terraform-provider-cairn/go.mod | indirect | MPL-2.0 (per notices: HashiCorp Go modules) | **legal review** |
| 12 | github.com/mattn/go-colorable | v0.1.12 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 13 | github.com/mattn/go-isatty | v0.0.17 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 14 | github.com/mitchellh/go-testing-interface | v1.14.1 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 15 | github.com/oklog/run | v1.0.0 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 16 | github.com/vmihailenco/msgpack/v5 | v5.4.1 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 17 | github.com/vmihailenco/tagparser/v2 | v2.0.0 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 18 | golang.org/x/net | v0.39.0 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 19 | golang.org/x/sys | v0.32.0 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 20 | golang.org/x/text | v0.24.0 | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 21 | google.golang.org/genproto/googleapis/rpc | v0.0.0-20250218202821-56aae31c358a | terraform-provider-cairn/go.mod | indirect | [TO CONFIRM: licence] | **unknown** |
| 22 | google.golang.org/grpc | v1.72.1 | terraform-provider-cairn/go.mod | indirect | Apache-2.0 (per notices: gRPC) | permissive (review set) |
| 23 | google.golang.org/protobuf | v1.36.6 | terraform-provider-cairn/go.mod | indirect | BSD-3-Clause (per notices: protobuf) | permissive (review set) |

### Gradle / Maven (JVM)

21 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | io.acryl:datahub-auth-api | 1.5.0 | plugins/java/cairn-authorizer/build.gradle | compileOnly (provided by Cairn Core at run time) | [TO CONFIRM: licence] (DataHub family is Apache-2.0 per notices; this artefact and version are not named there) | **unknown** |
| 2 | org.junit:junit-bom | 5.11.4 | plugins/java/cairn-authorizer/build.gradle | testImplementation | [TO CONFIRM: licence] | **unknown** |
| 3 | org.junit.jupiter:junit-jupiter | 5.11.4 (from BOM) | plugins/java/cairn-authorizer/build.gradle | testImplementation | [TO CONFIRM: licence] | **unknown** |
| 4 | org.junit.platform:junit-platform-launcher | 5.11.4 (from BOM) | plugins/java/cairn-authorizer/build.gradle | testRuntimeOnly | [TO CONFIRM: licence] | **unknown** |
| 5 | org.apache.hadoop:hadoop-client-api | 3.4.1 | store/hadoop-check/build.gradle | implementation | [TO CONFIRM: licence] | **unknown** |
| 6 | org.apache.hadoop:hadoop-client-runtime | 3.4.1 | store/hadoop-check/build.gradle | runtimeOnly | [TO CONFIRM: licence] | **unknown** |
| 7 | org.apache.hadoop:hadoop-aws | 3.4.1 | store/hadoop-check/build.gradle | implementation (transitive = false) | [TO CONFIRM: licence] | **unknown** |
| 8 | software.amazon.awssdk:bom | 2.24.6 | store/hadoop-check/build.gradle | platform | [TO CONFIRM: licence] | **unknown** |
| 9 | software.amazon.awssdk:s3 | 2.24.6 (BOM) | store/hadoop-check/build.gradle | implementation | [TO CONFIRM: licence] | **unknown** |
| 10 | software.amazon.awssdk:s3-transfer-manager | 2.24.6 (BOM) | store/hadoop-check/build.gradle | implementation | [TO CONFIRM: licence] | **unknown** |
| 11 | software.amazon.awssdk:sts | 2.24.6 (BOM) | store/hadoop-check/build.gradle | implementation | [TO CONFIRM: licence] | **unknown** |
| 12 | software.amazon.awssdk:apache-client | 2.24.6 (BOM) | store/hadoop-check/build.gradle | implementation | [TO CONFIRM: licence] | **unknown** |
| 13 | org.apache.commons:commons-lang3 | 3.12.0 | store/hadoop-check/build.gradle | runtimeOnly | [TO CONFIRM: licence] | **unknown** |
| 14 | org.apache.hadoop.thirdparty:hadoop-shaded-guava | 1.3.0 | store/hadoop-check/build.gradle | runtimeOnly | [TO CONFIRM: licence] | **unknown** |
| 15 | org.slf4j:slf4j-nop | 1.7.36 | store/hadoop-check/build.gradle | runtimeOnly | [TO CONFIRM: licence] | **unknown** |
| 16 | org.apache.iceberg:iceberg-spark-runtime-4.0_2.13 | 1.10.0 | docker/spark.Dockerfile (jar downloaded at build) | runtime, Spark image | Apache-2.0 (per notices: Apache Iceberg Spark runtime 1.10.0) | permissive (review set) |
| 17 | org.apache.iceberg:iceberg-aws-bundle | 1.10.0 | docker/spark.Dockerfile (jar downloaded at build) | runtime, Spark image | [TO CONFIRM: licence] (Apache Iceberg project; this artefact is not named in the notices) | **unknown** |
| 18 | io.openlineage:openlineage-spark_2.13 | 1.55.0 | docker/spark.Dockerfile (jar downloaded at build) | runtime, Spark image | [TO CONFIRM: licence] (not named in THIRD_PARTY_NOTICES.md) | **unknown** |
| 19 | org.apache.hadoop:hadoop-aws | 3.4.1 | docker/spark.Dockerfile (jar downloaded at build) | runtime, Spark image | [TO CONFIRM: licence] (not named in THIRD_PARTY_NOTICES.md) | **unknown** |
| 20 | software.amazon.awssdk:s3-transfer-manager | 2.33.0 | docker/spark.Dockerfile (jar downloaded at build) | runtime, Spark image | [TO CONFIRM: licence] (not named in THIRD_PARTY_NOTICES.md) | **unknown** |
| 21 | io.trino:trino-jdbc | 484 | docker/spark.Dockerfile (jar downloaded at build) | runtime, Spark image | [TO CONFIRM: licence] (not named in THIRD_PARTY_NOTICES.md) | **unknown** |

### Base images (Dockerfiles)

33 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | acryldata/datahub-gms:v1.8.0 | stage upstream-core | core/Dockerfile | earlier stage in file | Apache-2.0 (per notices: Cairn Core is a modified DataHub 1.8.0) | permissive (review set) |
| 2 | acryldata/datahub-frontend-react:v1.8.0 | stage upstream-web | core/Dockerfile | earlier stage in file | Apache-2.0 (per notices: Cairn Core is a modified DataHub 1.8.0) | permissive (review set) |
| 3 | acryldata/datahub-upgrade:v1.8.0 | stage upstream-setup | core/Dockerfile | earlier stage in file | Apache-2.0 (per notices: Cairn Core is a modified DataHub 1.8.0) | permissive (review set) |
| 4 | acryldata/datahub-actions:v1.8.0-slim | stage upstream-executor | core/Dockerfile | earlier stage in file | Apache-2.0 (per notices: Cairn Core is a modified DataHub 1.8.0) | permissive (review set) |
| 5 | python:3.12-slim | stage relabel-core | core/Dockerfile | earlier stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 6 | python:3.12-slim | stage relabel-setup | core/Dockerfile | earlier stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 7 | python:3.12-slim | stage relabel-web | core/Dockerfile | earlier stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 8 | python:3.12-slim | stage build | docker/Dockerfile | earlier stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 9 | python:3.12-slim | stage runtime | docker/Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 10 | python:3.12-slim | (unnamed stage) | docker/agent-runtime.Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 11 | python:3.12-slim | stage build | docker/agent.Dockerfile | earlier stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 12 | python:3.12-slim | stage runtime | docker/agent.Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 13 | apache/airflow:3.3.2-python3.12 | (unnamed stage) | docker/airflow.Dockerfile | last stage in file | Apache-2.0 (per notices) | permissive (review set) |
| 14 | gradle:8.14.3-jdk21 | stage gradle | docker/authorizer-plugin.Dockerfile | earlier stage in file | [TO CONFIRM: licence] (Gradle is Apache-2.0 per notices' Gradle wrapper line; the image includes a JDK whose licence is not recorded) | **unknown** |
| 15 | busybox:1.37 | (unnamed stage) | docker/authorizer-plugin.Dockerfile | last stage in file | GPL-2.0 for BusyBox (per notices, under Alpine only; the busybox image itself is not named) | **legal review** |
| 16 | rust:1-alpine | stage build | docker/catalog.Dockerfile | earlier stage in file | [TO CONFIRM: licence] (build stage only; the Rust toolchain image is not named in the notices) | **unknown** |
| 17 | alpine:3.20 | (unnamed stage) | docker/catalog.Dockerfile | last stage in file | MIT; GPL-2.0 for BusyBox (per notices: Alpine Linux) | **legal review** |
| 18 | node:20.18-alpine | stage build | docker/consumer-ui.Dockerfile | earlier stage in file | [TO CONFIRM: licence] (build stage only; Node.js licence not recorded) | **unknown** |
| 19 | nginxinc/nginx-unprivileged:1.27-alpine | (unnamed stage) | docker/consumer-ui.Dockerfile | last stage in file | BSD-2-Clause (per notices: nginx-unprivileged) | permissive (review set) |
| 20 | python:3.12-slim | (unnamed stage) | docker/dataplane.Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 21 | python:3.12-slim | stage build | docker/dbt.Dockerfile | earlier stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 22 | python:3.12-slim | stage runtime | docker/dbt.Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 23 | python:3.12-slim | (unnamed stage) | docker/egress-proxy.Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 24 | python:3.12-slim | (unnamed stage) | docker/jobs.Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 25 | rust:1-alpine | stage build | docker/ontology.Dockerfile | earlier stage in file | [TO CONFIRM: licence] (build stage only; the Rust toolchain image is not named in the notices) | **unknown** |
| 26 | alpine:3.20 | (unnamed stage) | docker/ontology.Dockerfile | last stage in file | MIT; GPL-2.0 for BusyBox (per notices: Alpine Linux) | **legal review** |
| 27 | python:3.12-slim | (unnamed stage) | docker/sandbox.Dockerfile | last stage in file | Python Software Foundation License; base packages per Debian (per notices: Python images) | permissive (review set) |
| 28 | apache/spark:4.0.1-scala2.13-java17-python3-ubuntu | (unnamed stage) | docker/spark.Dockerfile | last stage in file | Apache-2.0 (per notices) | permissive (review set) |
| 29 | rust:1-alpine | stage build | docker/store.Dockerfile | earlier stage in file | [TO CONFIRM: licence] (build stage only; the Rust toolchain image is not named in the notices) | **unknown** |
| 30 | alpine:3.20 | (unnamed stage) | docker/store.Dockerfile | last stage in file | MIT; GPL-2.0 for BusyBox (per notices: Alpine Linux) | **legal review** |
| 31 | trinodb/trino:484 | stage build | docker/trino-functions.Dockerfile | earlier stage in file | Apache-2.0 (per notices) | permissive (review set) |
| 32 | busybox:1.37 | (unnamed stage) | docker/trino-functions.Dockerfile | last stage in file | GPL-2.0 for BusyBox (per notices, under Alpine only; the busybox image itself is not named) | **legal review** |
| 33 | python:3.12-alpine | (unnamed stage) | tests/connectors/standin-source/Dockerfile | last stage in file | Python Software Foundation License; Alpine packages (per notices: Python images) | permissive (review set) |

### Python pinned in Dockerfiles

20 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | fastapi | ==0.162.0 | docker/agent-runtime.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 2 | uvicorn | ==0.54.0 | docker/agent-runtime.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 3 | httpx | ==0.28.1 | docker/agent-runtime.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 4 | anthropic | ==2.3.0 | docker/agent-runtime.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 5 | openai | ==3.46.0 | docker/agent-runtime.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 6 | apache-airflow | ==3.3.2 | docker/airflow.Dockerfile | runtime, image | Apache-2.0 (per notices) | permissive (review set) |
| 7 | dbt-core | ==1.12.6 | docker/airflow.Dockerfile | runtime, image | Apache-2.0 (per notices) | permissive (review set) |
| 8 | dbt-trino | ==1.11.0 | docker/airflow.Dockerfile | runtime, image | Apache-2.0 (per notices) | permissive (review set) |
| 9 | httpx | ==0.28.1 | docker/dataplane.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 10 | cryptography | ==46.0.3 | docker/dataplane.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 11 | dbt-core | ==1.12.6 | docker/dbt.Dockerfile | runtime, image | Apache-2.0 (per notices) | permissive (review set) |
| 12 | dbt-trino | ==1.11.0 | docker/dbt.Dockerfile | runtime, image | Apache-2.0 (per notices) | permissive (review set) |
| 13 | nbconvert | ==7.16.6 | docker/jobs.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 14 | nbclient | ==0.10.2 | docker/jobs.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 15 | ipykernel | ==6.29.5 | docker/jobs.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 16 | trino | ==0.333.0 | docker/jobs.Dockerfile | runtime, image | [TO CONFIRM: licence] (the notices name the trino package under the Airflow provider only; confirm it covers this pin) | **unknown** |
| 17 | duckdb | ==1.1.3 | docker/sandbox.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 18 | pandas | ==2.2.3 | docker/sandbox.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 19 | pyarrow | ==18.1.0 | docker/sandbox.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |
| 20 | numpy | ==2.1.3 | docker/sandbox.Dockerfile | runtime, image | [TO CONFIRM: licence] | **unknown** |

### Terraform providers (deploy/)

7 entries.

| # | Name | Version | Manifest | Scope | Licence (as recorded) | Status |
|---:|---|---|---|---|---|---|
| 1 | hashicorp/helm | ~> 3.0 | deploy/terraform/modules/cairn/versions.tf | required_providers | [TO CONFIRM: licence] | **unknown** |
| 2 | hashicorp/kubernetes | ~> 2.38 | deploy/terraform/modules/cairn/versions.tf | required_providers | [TO CONFIRM: licence] | **unknown** |
| 3 | hashicorp/aws | ~> 6.0 | deploy/terraform/modules/aws/main.tf | required_providers | [TO CONFIRM: licence] | **unknown** |
| 4 | hashicorp/google | ~> 6.0 | deploy/terraform/modules/gcp/main.tf | required_providers | [TO CONFIRM: licence] | **unknown** |
| 5 | hashicorp/azurerm | ~> 4.0 | deploy/terraform/modules/azure/main.tf | required_providers | [TO CONFIRM: licence] | **unknown** |
| 6 | hetznercloud/hcloud | ~> 1.52 | deploy/terraform/modules/hetzner/main.tf | required_providers | [TO CONFIRM: licence] | **unknown** |
| 7 | hetznercloud/hcloud | ~> 1.52 | deploy/hetzner/terraform/versions.tf | required_providers | [TO CONFIRM: licence] | **unknown** |

