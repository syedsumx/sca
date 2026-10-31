# Cairn Store

Cairn's S3-compatible object store (single node), written in Rust.

```
src/engine.rs     storage engine: immutable blob files + one transactional index (redb)
src/s3/           S3 API on hyper: SigV4/SigV2 auth, aws-chunked bodies, checksums, objects,
                  listing, multipart, XML
src/main.rs       serve | fsck | healthcheck | version
conformance/      boto3, pyarrow and pyiceberg suites against the real binary, plus crash tests
```

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo test                                             # unit tests
cargo build --release
CAIRN_STORE_BIN=target/release/cairn-store pytest -q conformance   # needs boto3, botocore[crt], pyarrow, pyiceberg[sql-sqlite]
```

Run it:

```bash
CAIRN_STORE_ACCESS_KEY=admin CAIRN_STORE_SECRET_KEY=$(openssl rand -hex 24) \
CAIRN_STORE_DATA=./data CAIRN_STORE_BUCKETS=lakehouse cargo run --release
```

Design notes are at the top of `src/engine.rs`. Operator documentation is in
`docs/site/administration.md` (Cairn Store).
