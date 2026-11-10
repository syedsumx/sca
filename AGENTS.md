# AGENTS.md: working on Cairn

Guidance for coding agents (and people) working in this repository. The active product is **Cairn**, a
governed data platform, under `cairn/`. Other top-level folders (`aegis/`, `datahub-enterprise/`, `src/`,
`dashboard/`, `vscode-extension/`, `ide/`) are earlier projects; don't change them unless asked.

The full platform guide (architecture, capabilities, hosting, CI, roadmap queue, change log) is the
"Cairn Platform Guide" doc. The in-repo references are `cairn/README.md`, `cairn/ARCHITECTURE.md`,
`cairn/ROADMAP.md` and `cairn/docs/site/` (the in-product docs; `administration.md` is the operator manual).

## Layout of `cairn/`

| Path | What it is | Language |
|---|---|---|
| `src/cairn/` | Control plane: FastAPI app (`app.py`), service wiring (`services.py`), CLI (`cli.py`) | Python 3.12 |
| `src/cairn/governance/` | Policy decision point (PDP), ABAC, personas, purposes, masking | Python |
| `src/cairn/dataaccess/` | Trino access (OPA protocol), shares, clean rooms, data apps, warehouses | Python |
| `src/cairn/sqlworkspace/` | Saved queries, dashboards, alerts, SQL workflows, procedures, git folders | Python |
| `src/cairn/pipelines/` | Pipes, streams, derived tables, maintenance, Delta, vectors, DR | Python |
| `src/cairn/iac/` | Everything as code: the resource registry (`registry.py`, `kinds.py`), `cairn bundle`, `cairn export`, `/v1/iac`, the provider's generator (`terraform.py`) | Python |
| `src/cairn/ml/`, `ai/`, `agent/`, `context/` | MLflow integration, feature tables, AI functions, assistants, context agents | Python |
| `src/cairn/quality/`, `marketplace/`, `identity/`, `notify/`, `export/` | Quality, access requests, SSO/SCIM, notifications, metadata export | Python |
| `src/cairn/migrations/versions/` | Alembic migrations, numbered `00NN_*.py` (latest: 0142) | Python |
| `store/` | Cairn Store: S3-compatible object store | Rust |
| `catalog/` | Cairn Catalog: Iceberg REST catalog | Rust |
| `ontology/` | Cairn Ontology: the ontology compiler (a port of NVIDIA Auto Ontology's; see `ontology/NOTICE`) | Rust |
| `spark/` | `cairn-spark`: governed Spark submit, Kubernetes pod templates | Python |
| `airflow-provider/` | Airflow provider for Cairn | Python |
| `terraform-provider-cairn/` | Terraform provider; its resource table `zz_generated_kinds.go` is generated from the registry | Go |
| `sdks/js`, `sdks/java` | Clients for the main APIs (no dependencies; `tests/test_sdk_clients.py`) | JavaScript, Java |
| `ui/` | Web UI and micro-frontend | TypeScript, React |
| `core/`, `plugins/` | Cairn Core (white-labelled DataHub engine) patches and Java plugins | Java |
| `helm/cairn`, `helm/jupyterhub`, `helm/cairn-remote-agent` | Helm charts | YAML |
| `deploy/hosting/tenant.sh` | Hosted tenants: one release per namespace | Bash |
| `deploy/hetzner/` | Hetzner cluster: node prep, k3s, platform services, Terraform LB, CI smoke test | Bash, HCL |
| `deploy/versions/` | Pinned versions for the upgrade matrix | JSON |
| `deploy/terraform/` | The platform as Terraform: `modules/cairn` (the chart), `modules/hetzner`, `modules/aws`, `gcp`, `azure`; `examples/` | HCL |
| `tests/` | Python tests, unit and end-to-end | Python |
| `docs/site/` | In-product documentation (also `de/ es/ fr/ ja/`) | Markdown |

## Skills: how to build and test

Run everything from `cairn/`.

- **Python setup:** `pip install -e ".[server,ai,test,postgres,trino,streams,export,documents]" ruff`.
- **Lint:** `ruff check .`.
- **Unit tests:** `pytest -q -p no:cacheprovider -o addopts="" tests/<file>.py`.
- **End-to-end tests against the real store and catalog:** build them once with
  `cargo build --release` (in `store/`, `catalog/` and `ontology/`), then set
  `CAIRN_STORE_BIN=.../release/cairn-store CAIRN_CATALOG_BIN=.../release/cairn-catalog`
  (and `CAIRN_ONTOLOGY_BIN=.../release/cairn-ontology` for `tests/test_ontology_e2e.py`). Trino, Kafka,
  PostgreSQL and the other services run as Docker containers that the tests start themselves.
- **Rust:** `cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked` in `store/`, `catalog/` and `ontology/`.
- **UI:** `npm ci && npm run typecheck && npm test && npm run build` in `ui/`; accessibility: `npm run build:mfe`, then
  `pytest -q -p no:cacheprovider -o addopts="" tests/test_accessibility_e2e.py` (axe on the main pages).
- **Helm:** `helm lint helm/cairn` and `helm template` with each values profile; CI runs kubeconform.
- **Terraform provider:** `gofmt -l . && go vet ./...` in `terraform-provider-cairn/`; the end-to-end test is
  `tests/test_terraform_provider.py`.
- **Everything as code:** a new configurable API (create and delete) needs a `Kind` in `src/cairn/iac/kinds.py` (or
  a reasoned entry in `tests/test_iac_coverage.py`'s `EXCLUDED`), then `python -m cairn.iac.terraform` to regenerate
  the provider (CI fails if it is stale). Tests: `tests/test_iac_coverage.py`, `tests/test_iac_roundtrip.py`, and
  the Terraform ones in `tests/test_terraform_provider.py`.
- **Terraform modules:** in `deploy/terraform`, `terraform fmt -check -recursive`, `tflint --recursive --config
  "$PWD/.tflint.hcl"`, and `terraform init -backend=false && terraform validate` in each module and example; CI
  job `terraform-modules` also applies `examples/kind` on kind.
- **Shell:** `shellcheck -S warning` on scripts in `deploy/`.
- **Hetzner setup:** `deploy/hetzner/ci/k3s-smoke.sh` runs the whole setup on one Linux machine (sudo,
  internet); CI runs it as job `hetzner`.

CI is `.github/workflows/cairn-ci.yml` (17 jobs; `live-model` needs the `ANTHROPIC_API_KEY` secret); `.github/workflows/cairn-upgrade-matrix.yml` runs weekly.

## Conventions

- **Test against real components.** Use real binaries and images (store, catalog, Trino, Kafka, PostgreSQL,
  Spark, MLflow), not mocks. Reproduce a CI failure before fixing it, and show the same check passing.
- **Fail closed.** When a policy decision, token or key can't be obtained, refuse rather than allow.
- **New database tables:** add a migration in `src/cairn/migrations/versions/` and register the table's
  module in `src/cairn/common/db.py` (`tests/test_migrations.py` enforces it).
- **FastAPI:** import `Request` and `WebSocket` at module level; with `from __future__ import annotations`
  a function-level import turns them into query parameters.
- **New service wiring:** in `services.py`; build a service after the services it depends on.
- **Docs with every change:** operator-facing behaviour goes in `docs/site/administration.md`; status goes in
  `ROADMAP.md` (✅ shipped and tested, 🟡 partly validated, ⬜ planned); update the platform guide's
  roadmap queue and change log after each completed item.
- **Commits:** one item per commit (or a few), with a message that says what changed and why. Don't put
  model names or identifiers in commits, code or docs.
- **Branch:** work on `claude/datahub-enterprise-extension`; push with `git push -u origin <branch>`. Don't
  open pull requests unless asked.
- **Secrets:** never on command lines or in logs; pass them through files or file descriptors, and keep
  generated credentials in Kubernetes Secrets.

## Working through the roadmap queue

The queue (65 items: the original 28, the BI track, the gaps found after it, and the capability audit's L47-L67) is in the platform guide. For each item, in order:

1. Build it in the right package, with its migration, wiring, API and docs.
2. Test it end to end against real components locally; add the test to the CI job that covers its area.
3. Commit and push; check that CI is green (fix and re-push if not).
4. Update `ROADMAP.md`, `docs/site/administration.md`, and the guide's queue status and change log.
