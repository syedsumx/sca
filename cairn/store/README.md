# Cairn Store

Cairn's S3-compatible object store, written in Rust: one node, or a cluster of nodes that share PostgreSQL
metadata (`CAIRN_STORE_META`). Lakehouse tables, volumes, backups and exports live in it; any S3 client works
(boto3, pyarrow, pyiceberg, Trino, Spark, DuckDB).

```
src/engine.rs     storage engine: immutable blob files + one transactional index (redb); design notes at the top
src/engine/       blob packs, disk layout, the key-value index, chunk compression, content ids
src/s3/           the S3 API on hyper (SigV4/SigV2, aws-chunked bodies, checksums, multipart, versioning, object
                  lock, lifecycle and tiering, replication, residency, events, STS, IAM), plus WebDAV (dav.rs),
                  SFTP (sftp.rs), the file API (files.rs), the Kafka protocol on message queues (kafka.rs),
                  encryption (sse.rs, kms.rs, kmip.rs) and website hosting (website.rs)
src/csi/          Kubernetes CSI driver: buckets as persistent volumes
src/mount.rs      `cairn-store mount`: a bucket as a local directory (FUSE)
src/admin_cli.rs  `cairn-store admin`: the admin API from the command line
src/main.rs       serve | healthcheck | fsck | version | admin | mount | bench | csi
conformance/      suites against the real binary: S3 (boto3, pyarrow, pyiceberg), WebDAV, SFTP, Kafka, key management
                  (Vault, AWS, cloud KMS, KMIP) and crash tests
```

Configuration is the environment: the `CAIRN_STORE_*` variables documented at the top of `src/main.rs`.

Shipped features, in short (details in the operator guide): encryption at rest with a keyring or customer-managed
keys (HashiCorp Vault or OpenBao Transit, AWS KMS, Azure Key Vault, Google Cloud KMS, KMIP); versioning, object lock,
lifecycle and tiering, bucket quotas, replication, event notifications (webhook, Kafka, SQS, Pub/Sub, NATS); backups
to S3, Azure Blob and GCS sinks, optionally continuous for point-in-time restores; TLS and mutual TLS, including
between cluster nodes.

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings
cargo test                                             # unit tests
cargo build --release
CAIRN_STORE_BIN=target/release/cairn-store pytest -q conformance   # the Python packages CI installs for it
                                                                   # (see .github/workflows/cairn-ci.yml)
```

Run it:

```bash
CAIRN_STORE_ACCESS_KEY=admin CAIRN_STORE_SECRET_KEY=$(openssl rand -hex 24) \
CAIRN_STORE_DATA=./data CAIRN_STORE_BUCKETS=lakehouse cargo run --release
```

Design notes are at the top of `src/engine.rs`. Operator documentation is in `docs/site/administration.md`
(the "Cairn Store" section).
