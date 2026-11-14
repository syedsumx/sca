# Cairn Catalog

Cairn's Iceberg REST catalog, written in Rust on iceberg-rust (the Apache Iceberg project's Rust
library), which validates requirements and applies metadata updates exactly as the table spec says.
Tables can be created as Iceberg format version 1, 2 or 3.

```
src/api.rs        the REST API (namespaces, tables, commits, transactions, rename, register), tokens
src/db.rs         catalog state: one compare-and-set transaction per commit, in redb (one catalog)
src/pg.rs         the same state in PostgreSQL (CAIRN_CATALOG_DATABASE_URL), so several replicas serve one lakehouse
src/identity.rs   engines (Spark, pyiceberg, DuckDB) sign in with Cairn-issued tokens; Cairn decides per table
src/s3.rs         SigV4 client that writes metadata files to Cairn Store (or any S3)
src/external.rs   tables on the customer's storage: external locations, Azure SAS, S3 STS, GCS downscoped tokens
src/residency.rs  data residency: a namespace pinned to a region keeps its tables in that region
src/main.rs       serve | healthcheck | version
conformance/      pytest suites against the real binaries: the REST API (pyiceberg), Iceberg v3, credential
                  vending, clones, HA, chaos, and Trino (needs Docker)
```

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo test
cargo build --release && (cd ../store && cargo build --release)
CAIRN_CATALOG_BIN=target/release/cairn-catalog CAIRN_STORE_BIN=../store/target/release/cairn-store \
  pytest -q conformance          # needs pyiceberg[pyarrow], boto3, httpx, requests, pytest
```

Operator documentation: `docs/site/administration.md` (Cairn Catalog).
