# Cairn Store

Cairn's S3-compatible object store (single node), written in Go.

```
internal/engine   storage engine: immutable blob files + one transactional index (bbolt)
internal/s3       S3 API: SigV4/SigV2 auth, aws-chunked bodies, checksums, objects, listing, multipart
cmd/cairn-store   serve | fsck | healthcheck | version
conformance/      boto3, pyarrow and pyiceberg suites against the real binary, plus crash tests
```

```bash
go test -race ./...                                   # unit tests
go build -o /tmp/cairn-store ./cmd/cairn-store
CAIRN_STORE_BIN=/tmp/cairn-store pytest -q conformance  # needs boto3, botocore[crt], pyarrow, pyiceberg[sql-sqlite]
```

Run it:

```bash
CAIRN_STORE_ACCESS_KEY=admin CAIRN_STORE_SECRET_KEY=$(openssl rand -hex 24) \
CAIRN_STORE_DATA=./data CAIRN_STORE_BUCKETS=lakehouse go run ./cmd/cairn-store
```

Design notes are at the top of `internal/engine/engine.go`. Operator documentation is in
`docs/site/administration.md` (Cairn Store).
