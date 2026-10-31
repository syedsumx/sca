# Cairn Catalog

Cairn's Iceberg REST catalog, written in Rust on iceberg-rust (the Apache Iceberg project's Rust
library), which validates requirements and applies metadata updates exactly as the table spec says.

```
src/api.rs        the REST API (namespaces, tables, commits, transactions, rename, register), tokens
src/db.rs         catalog state in redb: one compare-and-set transaction per commit
src/s3.rs         SigV4 client that writes metadata files to Cairn Store (or any S3)
src/main.rs       serve | healthcheck | version
conformance/      pyiceberg's RestCatalog against the real catalog and Cairn Store
```

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo test
cargo build --release && (cd ../store && cargo build --release)
CAIRN_CATALOG_BIN=target/release/cairn-catalog CAIRN_STORE_BIN=../store/target/release/cairn-store \
  pytest -q conformance          # needs pyiceberg[pyarrow], requests
```

Operator documentation: `docs/site/administration.md` (Cairn Catalog).
