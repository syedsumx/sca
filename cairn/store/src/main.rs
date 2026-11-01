//! cairn-store: Cairn's S3-compatible object store (one node, or a cluster of nodes).
//!
//! ```text
//! cairn-store serve        run the server (default)
//! cairn-store fsck         check the data directory and remove debris left by a crash, then exit
//! cairn-store healthcheck  exit 0 if the local server is ready (container health checks)
//! cairn-store admin ...    the admin API (see admin_cli)
//! cairn-store mount <bucket>[/prefix] <dir>  a bucket as a FUSE directory (see mount)
//! cairn-store csi         a Kubernetes CSI driver (csi.cairn.store) for buckets as volumes (see csi)
//! cairn-store restore-backup <sink> <name> <dir>  download a backup from a sink into a new data directory
//! cairn-store version      print the version
//! ```
//!
//! Configuration (flags `-addr`, `-data` override the first two):
//!
//! ```text
//! CAIRN_STORE_ADDR              listen address (:8333)
//! CAIRN_STORE_DATA              data directory (/data)
//! CAIRN_STORE_ACCESS_KEY        admin access key ...
//! CAIRN_STORE_SECRET_KEY        ... and its secret (or CAIRN_STORE_SECRET_KEY_FILE)
//! CAIRN_STORE_CREDENTIALS_FILE  JSON list of further keys: access_key, secret_key, buckets, read_only, admin, policy, actor
//! CAIRN_STORE_REGION            region reported to clients (us-east-1)
//! CAIRN_STORE_DOMAIN            enables virtual-hosted-style requests on <bucket>.<domain>
//! CAIRN_STORE_BUCKETS           comma-separated buckets to create at start-up
//! CAIRN_STORE_BLOCK_PUBLIC_ACCESS  false allows public bucket policies and anonymous reads they grant (default true)
//! CAIRN_STORE_PDP_URL           Cairn's policy decision point, e.g. http://cairn-pdp:8810 (optional; deny-only layer)
//! CAIRN_STORE_PDP_TOKEN         a Cairn service token for it (or CAIRN_STORE_PDP_TOKEN_FILE)
//! CAIRN_STORE_LIFECYCLE_INTERVAL  seconds between lifecycle passes (3600; 0 turns lifecycle processing off)
//! CAIRN_STORE_LIFECYCLE_DAY_SECONDS  length of a lifecycle "day" (86400; shorter only for tests)
//! CAIRN_STORE_KEYRING_FILE      keys for encryption at rest: {"keys": {"<id>": "<base64 32 bytes>"}, "sse_s3_key": "<id>"}
//! CAIRN_STORE_MASTER_KEY        ... or one base64 key (or CAIRN_STORE_MASTER_KEY_FILE)
//! CAIRN_STORE_ENCRYPT_BY_DEFAULT  with a keyring, encrypt new objects with SSE-S3 by default (true)
//! CAIRN_STORE_SSE_C_ALLOW_HTTP  accept customer keys over plain HTTP, when TLS ends in front of the store (false)
//! CAIRN_STORE_WEBSITE_DOMAIN    serve bucket websites at <bucket>.<domain> (public buckets only; off when empty)
//! CAIRN_STORE_EVENT_TARGETS_FILE  event notification targets: [{"type": "webhook"|"kafka"|"sqs"|"pubsub"|"nats", "id": ..., ...}]
//! CAIRN_STORE_EVENT_MAX_AGE_HOURS  drop events still undeliverable after this long (168)
//! CAIRN_STORE_LOG_REQUESTS      one JSON log line per request (false)
//! CAIRN_STORE_ACCESS_LOG_FLUSH_SECONDS  how often server access logs are written to their target buckets (60)
//! CAIRN_STORE_TLS_CERT          serve HTTPS with this PEM certificate chain ...
//! CAIRN_STORE_TLS_KEY           ... and PEM private key (both re-read when they change)
//! CAIRN_STORE_TLS_CLIENT_CA     require client certificates signed by this CA (mutual TLS)
//! CAIRN_STORE_TLS_PROBE_CERT/KEY  client certificate for the healthcheck command under mutual TLS
//! CAIRN_STORE_CA_FILE           extra CA certificates trusted for outbound HTTPS (webhooks, PDP)
//! CAIRN_STORE_BACKUP_DIR        where online backups (admin API: POST /-/admin/v1/backups) are written (off when empty)
//! CAIRN_STORE_BACKUP_SINKS_FILE  backup sinks (S3, Azure Blob, GCS): [{"id", "kind", "endpoint", "bucket", "prefix", ...}] (see s3::remote::RemoteSpec)
//! CAIRN_STORE_BACKUP_CONTINUOUS  journal every object change to this sink, for point-in-time restores (off when empty)
//! CAIRN_STORE_BACKUP_SINK_SCHEDULE  back up to this sink on a schedule (off when empty) ...
//! CAIRN_STORE_BACKUP_SINK_INTERVAL / _KEEP  ... every N seconds (86400), keeping the newest N scheduled ones (7)
//! CAIRN_STORE_REMOTES_FILE      remote buckets (S3, Azure Blob, GCS) that can be mounted into local ones: [{"id", "kind", "endpoint", "bucket", "prefix", ...}]
//! CAIRN_STORE_REMOTE_SYNC_INTERVAL  seconds between syncs of mounted remote buckets (60)
//! CAIRN_STORE_FILES_BASIC_AUTH  the file API (/-/files/) also accepts HTTP Basic access_key:secret_key (false; TLS only)
//! CAIRN_STORE_FILES_BASIC_ALLOW_HTTP  ... also over plain HTTP, when TLS ends in front of the store (false)
//! CAIRN_STORE_KAFKA_ADDR  serve the Kafka protocol on the message queues on this address (off when empty), e.g. :9092
//! CAIRN_STORE_KAFKA_ADVERTISED  host:port Kafka clients connect to (default: CAIRN_STORE_KAFKA_ADDR, which must then name a host)
//! CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT  true: serve Kafka without TLS (SASL/PLAIN then sends secrets in the clear)
//! CAIRN_STORE_SFTP_ADDR  serve SFTP on this address (off when empty), e.g. :2022
//! CAIRN_STORE_SFTP_HOST_KEY  the SSH host key (OpenSSH format; default <data>/sftp_host_ed25519_key, created when missing)
//! CAIRN_STORE_SFTP_PASSWORD_AUTH  keys sign in with their secret key as the password (true)
//! CAIRN_STORE_META            keep metadata in PostgreSQL (postgres://user:pass@host/db?sslmode=require) instead of the
//!                              embedded index; CAIRN_STORE_META_FILE reads the URL from a file (a mounted Secret)
//! CAIRN_STORE_PACK_BELOW       objects up to this many bytes are packed into shared pack files (65536; 0: off)
//! CAIRN_STORE_PACK_FILE_BYTES  a pack file is closed at this size (1 GiB)
//! CAIRN_STORE_COMPACT_INTERVAL seconds between compaction passes over packs (3600; 0: off)
//! CAIRN_STORE_COMPACT_DEAD_RATIO  a pack is compacted when this share of it is dead (0.5)
//! CAIRN_STORE_TIERS_FILE  storage tiers for lifecycle transitions: remote buckets with a "storage_class" instead of an "id"
//! CAIRN_STORE_REPLICATION_TARGETS_FILE  bucket replication destinations: [{"id", "endpoint", "access_key", "secret_key", "region"}]
//! CAIRN_STORE_CLUSTER_NODE   run as this node of a cluster (off when empty; needs CAIRN_STORE_META, shared by the nodes)
//! CAIRN_STORE_CLUSTER_URL    this node's address for the other nodes, e.g. http://store-0.store:8333
//! CAIRN_STORE_STS_SECRET      turns on temporary credentials (STS): at least 32 characters, the same on every node (or _FILE)
//! CAIRN_STORE_STS_PROVIDERS   OIDC issuers AssumeRoleWithWebIdentity trusts: JSON [{issuer, audiences, jwks_url|jwks_file, actor}] (or _FILE)
//! CAIRN_STORE_CLUSTER_SECRET the secret that authenticates node-to-node calls, the same on every node (or _FILE)
//! CAIRN_STORE_CLUSTER_COPIES copies of each blob, on distinct nodes (2)
//! CAIRN_STORE_CLUSTER_EC     erasure-code large blobs as k data + m parity shards, e.g. 4+2 (off)
//! CAIRN_STORE_CLUSTER_EC_ABOVE  ... blobs of at least this many bytes (1048576)
//! CAIRN_STORE_CLUSTER_EC_AFTER  seconds after which replicated blobs are re-encoded as k+m shards (off)
//! CAIRN_STORE_CLUSTER_EC_CONVERT_ABOVE  ... blobs of at least this many bytes (1048576)
//! CAIRN_STORE_CLUSTER_ZONE / _RACK  where this node is (labels; copies are spread across zones, then racks)
//! CAIRN_STORE_CLUSTER_SPREAD  the failure domain copies must not share: node (default), rack or zone
//! CAIRN_STORE_CLUSTER_HEARTBEAT / _DEAD_AFTER / _REPAIR_INTERVAL  seconds (5 / 60 / 300)
//! CAIRN_STORE_COMPRESSION    compress new object data: off or zstd (off; in 1 MiB chunks, ranges stay cheap)
//! CAIRN_STORE_QUEUE_RETENTION_INTERVAL  seconds between retention passes over message queue topics (60)
//! CAIRN_STORE_DISKS          more disks for blob files: /mnt/a:hdd,/mnt/b:ssd (off when empty)
//! CAIRN_STORE_DATA_DISK_TYPE the data directory's disk type, and that of buckets without one (hdd)
//! CAIRN_STORE_FSYNC             false disables fsync (tests only: a crash can lose acknowledged writes)
//! CAIRN_STORE_LOG_LEVEL         debug, info, warn, error
//! ```

// Errors are cold paths here; boxing them would only add allocations to the code that matters.
#![allow(clippy::result_large_err)]

mod admin_cli;
mod audit;
mod bench;
mod crypt;
mod csi;
mod engine;
mod mount;
mod s3;
mod tls;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

use engine::Engine;
use s3::auth::Credential;

const VERSION: &str = env!("CARGO_PKG_VERSION");

fn env(k: &str, default: &str) -> String {
    std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

fn fail(msg: &str, err: impl std::fmt::Display) -> ! {
    tracing::error!(error = %err, "{msg}");
    eprintln!("cairn-store: {msg}: {err}");
    std::process::exit(1)
}

pub(crate) fn listen_addr(addr: &str) -> String {
    if addr.starts_with(':') {
        format!("0.0.0.0{addr}")
    } else {
        addr.to_string()
    }
}

/// The Kafka listener's settings (None: off). The advertised address must be one clients can reach.
fn kafka_config() -> Result<Option<s3::kafka::KafkaConfig>, String> {
    let addr = env("CAIRN_STORE_KAFKA_ADDR", "");
    if addr.is_empty() {
        return Ok(None);
    }
    let adv = match env("CAIRN_STORE_KAFKA_ADVERTISED", "") {
        a if a.is_empty() => addr.clone(),
        a => a,
    };
    let (host, port) = adv.rsplit_once(':').ok_or("the advertised address is host:port")?;
    let port: i32 = port.parse().map_err(|_| "the advertised port is not a number")?;
    if host.is_empty() || host == "0.0.0.0" || host == "[::]" {
        return Err("set CAIRN_STORE_KAFKA_ADVERTISED to the host:port Kafka clients connect to".into());
    }
    Ok(Some(s3::kafka::KafkaConfig { addr, advertised_host: host.trim_matches(['[', ']']).to_string(), advertised_port: port }))
}

/// A secret from `<K>_FILE` (a mounted secret) or `<K>`.
fn secret(k: &str) -> Result<String, String> {
    match std::env::var(format!("{k}_FILE")) {
        Ok(f) if !f.is_empty() => Ok(std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?.trim().to_string()),
        _ => Ok(env(k, "")),
    }
}

/// Temporary credentials: on when CAIRN_STORE_STS_SECRET is set (identity providers are optional).
fn sts_config() -> Result<Option<s3::sts::StsConfig>, String> {
    let key = secret("CAIRN_STORE_STS_SECRET")?;
    if key.is_empty() {
        return Ok(None);
    }
    let providers = match env("CAIRN_STORE_STS_PROVIDERS_FILE", "") {
        f if f.is_empty() => env("CAIRN_STORE_STS_PROVIDERS", ""),
        f => std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?,
    };
    s3::sts::StsConfig::new(&key, &providers).map(Some)
}

/// Keys for encryption at rest: a keyring file, or one master key. None configured: no SSE-S3/SSE-KMS.
fn keyring() -> Result<s3::sse::Keyring, String> {
    let file = env("CAIRN_STORE_KEYRING_FILE", "");
    if !file.is_empty() {
        let text = std::fs::read_to_string(&file).map_err(|e| format!("{file}: {e}"))?;
        return s3::sse::Keyring::from_json(&text).map_err(|e| format!("{file}: {e}"));
    }
    match secret("CAIRN_STORE_MASTER_KEY")? {
        k if k.is_empty() => Ok(s3::sse::Keyring::default()),
        k => s3::sse::Keyring::single(&k).map_err(|e| format!("CAIRN_STORE_MASTER_KEY: {e}")),
    }
}

fn credentials() -> Result<Vec<Credential>, String> {
    let mut out = vec![];
    let mut secret = env("CAIRN_STORE_SECRET_KEY", "");
    if let Ok(f) = std::env::var("CAIRN_STORE_SECRET_KEY_FILE") {
        secret = std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?.trim().to_string();
    }
    let ak = env("CAIRN_STORE_ACCESS_KEY", "");
    if !ak.is_empty() {
        out.push(Credential { access_key: ak, secret_key: secret, admin: true, ..Default::default() });
    }
    if let Ok(f) = std::env::var("CAIRN_STORE_CREDENTIALS_FILE") {
        let text = std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?;
        let more: Vec<Credential> = serde_json::from_str(&text).map_err(|e| format!("{f}: {e}"))?;
        out.extend(more);
    }
    if out.is_empty() {
        return Err("set CAIRN_STORE_ACCESS_KEY and CAIRN_STORE_SECRET_KEY, or CAIRN_STORE_CREDENTIALS_FILE".into());
    }
    Ok(out)
}

/// The PostgreSQL metadata database, when the store keeps its metadata there (`CAIRN_STORE_META`).
fn meta() -> Option<String> {
    match std::env::var("CAIRN_STORE_META_FILE") {
        Ok(f) if !f.is_empty() => Some(std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e)).trim().to_string()),
        _ => std::env::var("CAIRN_STORE_META").ok().filter(|v| !v.is_empty()),
    }
}

/// Disks for blob files (CAIRN_STORE_DISKS) and the data directory's disk type.
fn disk_config() -> engine::DiskConfig {
    engine::DiskConfig {
        main_kind: env("CAIRN_STORE_DATA_DISK_TYPE", "hdd"),
        extra: engine::disks::parse_specs(&env("CAIRN_STORE_DISKS", "")).unwrap_or_else(|e| fail("CAIRN_STORE_DISKS", e)),
    }
}

/// This node's name in a cluster (None: a single store).
fn node_name() -> Option<String> {
    Some(env("CAIRN_STORE_CLUSTER_NODE", "")).filter(|n| !n.is_empty())
}

/// Cluster settings (None: a single store). Everything a cluster needs is checked before serving.
fn cluster_config() -> Option<s3::cluster::ClusterConfig> {
    let node = node_name()?;
    let secs = |k: &str, d: &str| -> Duration {
        let v: f64 = env(k, d).parse().unwrap_or_else(|e| fail(k, e));
        if v.is_nan() || v <= 0.0 {
            fail(k, "must be more than 0");
        }
        Duration::from_secs_f64(v)
    };
    let cfg = s3::cluster::ClusterConfig {
        node,
        url: env("CAIRN_STORE_CLUSTER_URL", ""),
        secret: secret("CAIRN_STORE_CLUSTER_SECRET").unwrap_or_else(|e| fail("CAIRN_STORE_CLUSTER_SECRET", e)),
        copies: env("CAIRN_STORE_CLUSTER_COPIES", "2").parse().unwrap_or_else(|e| fail("CAIRN_STORE_CLUSTER_COPIES", e)),
        ec: s3::cluster::parse_ec(&env("CAIRN_STORE_CLUSTER_EC", "")).unwrap_or_else(|e| fail("CAIRN_STORE_CLUSTER_EC", e)),
        ec_above: env("CAIRN_STORE_CLUSTER_EC_ABOVE", "1048576").parse().unwrap_or_else(|e| fail("CAIRN_STORE_CLUSTER_EC_ABOVE", e)),
        ec_after: match env("CAIRN_STORE_CLUSTER_EC_AFTER", "") {
            v if v.is_empty() || v == "off" => None,
            v => Some(Duration::from_secs(v.parse().unwrap_or_else(|e| fail("CAIRN_STORE_CLUSTER_EC_AFTER", e)))),
        },
        ec_convert_above: env("CAIRN_STORE_CLUSTER_EC_CONVERT_ABOVE", "1048576")
            .parse()
            .unwrap_or_else(|e| fail("CAIRN_STORE_CLUSTER_EC_CONVERT_ABOVE", e)),
        heartbeat: secs("CAIRN_STORE_CLUSTER_HEARTBEAT", "5"),
        dead_after: secs("CAIRN_STORE_CLUSTER_DEAD_AFTER", "60"),
        repair_every: secs("CAIRN_STORE_CLUSTER_REPAIR_INTERVAL", "300"),
        zone: env("CAIRN_STORE_CLUSTER_ZONE", ""),
        rack: env("CAIRN_STORE_CLUSTER_RACK", ""),
        spread: env("CAIRN_STORE_CLUSTER_SPREAD", "node"),
    };
    cfg.validate().unwrap_or_else(|e| fail("cluster", e));
    if meta().is_none() {
        fail("cluster", "a cluster keeps its metadata in PostgreSQL: set CAIRN_STORE_META");
    }
    for k in ["CAIRN_STORE_PACK_BELOW", "CAIRN_STORE_BACKUP_DIR", "CAIRN_STORE_BACKUP_SINKS_FILE"] {
        if !env(k, "").is_empty() && env(k, "") != "0" {
            fail("cluster", format!("{k} is not available in a cluster (packs and backups cover one disk); unset it"));
        }
    }
    Some(cfg)
}

/// Backup sinks from CAIRN_STORE_BACKUP_SINKS_FILE (none when unset).
fn backup_sinks() -> Result<Vec<s3::remote::RemoteSpec>, String> {
    match env("CAIRN_STORE_BACKUP_SINKS_FILE", "") {
        f if f.is_empty() => Ok(vec![]),
        f => s3::remote::load_specs(&std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?, "backup sink"),
    }
}

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let cmd = if args.first().is_some_and(|a| !a.starts_with('-')) { args.remove(0) } else { "serve".to_string() };
    let mut addr = env("CAIRN_STORE_ADDR", ":8333");
    let mut data = env("CAIRN_STORE_DATA", "/data");
    // `admin` takes its own arguments.
    let rest: Vec<String> = if cmd == "admin" || cmd == "mount" || cmd == "restore-backup" || cmd == "csi" || cmd == "bench" {
        std::mem::take(&mut args)
    } else {
        vec![]
    };
    let mut it = args.into_iter();
    while let Some(a) = it.next() {
        match a.trim_start_matches('-') {
            "addr" => addr = it.next().unwrap_or_default(),
            "data" => data = it.next().unwrap_or_default(),
            other => {
                eprintln!("unknown flag -{other} (flags: -addr, -data)");
                std::process::exit(2);
            }
        }
    }
    let level = env("CAIRN_STORE_LOG_LEVEL", "info");
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::new(format!("cairn_store={level},warn")))
        .with_writer(std::io::stdout)
        .init();

    match cmd.as_str() {
        "version" => println!("cairn-store {VERSION}"),
        "fsck" => {
            let eng = Engine::open_full(&data, true, meta().as_deref(), node_name().as_deref(), &disk_config())
                .unwrap_or_else(|e| fail("open store", e));
            let rep = eng.recover().unwrap_or_else(|e| fail("fsck", e));
            println!("{}", serde_json::to_string_pretty(&rep).unwrap());
            if rep.objects_with_missing_blobs > 0 {
                std::process::exit(2);
            }
        }
        "admin" => {
            let tls = !env("CAIRN_STORE_TLS_CERT", "").is_empty();
            let default = format!("{}://{}", if tls { "https" } else { "http" }, listen_addr(&addr).replace("0.0.0.0", "127.0.0.1"));
            let endpoint = env("CAIRN_STORE_ENDPOINT", &default);
            let sk = secret("CAIRN_STORE_SECRET_KEY").unwrap_or_else(|e| fail("CAIRN_STORE_SECRET_KEY", e));
            let code = admin_cli::run(&rest, &endpoint, &env("CAIRN_STORE_ACCESS_KEY", ""), &sk, &env("CAIRN_STORE_REGION", "us-east-1"));
            std::process::exit(code);
        }
        "mount" => {
            let tls = !env("CAIRN_STORE_TLS_CERT", "").is_empty();
            let default = format!("{}://{}", if tls { "https" } else { "http" }, listen_addr(&addr).replace("0.0.0.0", "127.0.0.1"));
            let endpoint = env("CAIRN_STORE_ENDPOINT", &default);
            let sk = secret("CAIRN_STORE_SECRET_KEY").unwrap_or_else(|e| fail("CAIRN_STORE_SECRET_KEY", e));
            let code = mount::run(&rest, &endpoint, &env("CAIRN_STORE_ACCESS_KEY", ""), &sk, &env("CAIRN_STORE_REGION", "us-east-1"));
            std::process::exit(code);
        }
        "bench" => {
            let tls = !env("CAIRN_STORE_TLS_CERT", "").is_empty();
            let default = format!("{}://{}", if tls { "https" } else { "http" }, listen_addr(&addr).replace("0.0.0.0", "127.0.0.1"));
            let endpoint = env("CAIRN_STORE_ENDPOINT", &default);
            let sk = secret("CAIRN_STORE_SECRET_KEY").unwrap_or_else(|e| fail("CAIRN_STORE_SECRET_KEY", e));
            std::process::exit(bench::run(
                &rest,
                &endpoint,
                &env("CAIRN_STORE_ACCESS_KEY", ""),
                &sk,
                &env("CAIRN_STORE_REGION", "us-east-1"),
            ));
        }
        "migrate-meta" => {
            // Copies a stopped store's embedded index into PostgreSQL (CAIRN_STORE_META), then sets the
            // embedded index aside so the store opens on PostgreSQL from now on.
            let url = meta().unwrap_or_else(|| fail("migrate-meta", "set CAIRN_STORE_META to the postgres:// URL to copy to"));
            let eng = Engine::open_full(&data, true, None, None, &disk_config()).unwrap_or_else(|e| fail("open store (is it stopped?)", e));
            let rows = eng.migrate_meta_to(&url).unwrap_or_else(|e| fail("migrate-meta", e));
            drop(eng);
            let index = std::path::Path::new(&data).join("index.redb");
            let kept = index.with_extension(format!("redb.migrated-{}", engine::now_ms()));
            std::fs::rename(&index, &kept).unwrap_or_else(|e| fail("set the embedded index aside", e));
            println!("{}", serde_json::json!({"rows": rows, "embedded_index_kept_as": kept.display().to_string()}));
            std::process::exit(0);
        }
        "csi" => {
            let tls = !env("CAIRN_STORE_TLS_CERT", "").is_empty();
            let default = format!("{}://{}", if tls { "https" } else { "http" }, listen_addr(&addr).replace("0.0.0.0", "127.0.0.1"));
            let endpoint = env("CAIRN_STORE_ENDPOINT", &default);
            let sk = secret("CAIRN_STORE_SECRET_KEY").unwrap_or_else(|e| fail("CAIRN_STORE_SECRET_KEY", e));
            std::process::exit(csi::run(&endpoint, &env("CAIRN_STORE_ACCESS_KEY", ""), &sk, &env("CAIRN_STORE_REGION", "us-east-1")));
        }
        "restore-backup" => {
            // Downloads a backup from a sink into a new data directory: restore-backup <sink> <name> <dir>.
            let (Some(sink), Some(name), Some(dir)) = (rest.first(), rest.get(1), rest.get(2)) else {
                eprintln!("usage: cairn-store restore-backup <sink> <name|latest> <dir> [--until <RFC 3339 time>]   (sinks: CAIRN_STORE_BACKUP_SINKS_FILE)");
                std::process::exit(2);
            };
            let specs = backup_sinks().unwrap_or_else(|e| fail("CAIRN_STORE_BACKUP_SINKS_FILE", e));
            let spec =
                specs.into_iter().find(|s| &s.id == sink).unwrap_or_else(|| fail("restore-backup", format!("no backup sink {sink}")));
            let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().unwrap_or_else(|e| fail("runtime", e));
            let _enter = rt.enter();
            let remote = s3::remote::Remote::new(spec).unwrap_or_else(|e| fail("restore-backup", e));
            // restore-backup <sink> <name|latest> <dir> [--until <RFC 3339 time>]
            let until = rest.iter().position(|a| a == "--until").and_then(|i| rest.get(i + 1)).map(|t| {
                time::OffsetDateTime::parse(t, &time::format_description::well_known::Rfc3339)
                    .map(|t| (t.unix_timestamp_nanos() / 1_000_000) as u64)
                    .unwrap_or_else(|e| fail("--until (an RFC 3339 time, e.g. 2026-11-01T12:00:00Z)", e))
            });
            let continuous = until.is_some() || name == "latest";
            let out = if continuous {
                s3::sinks::restore_until(&remote, (name != "latest").then_some(name.as_str()), until, std::path::Path::new(dir))
            } else {
                s3::sinks::restore(&remote, name, std::path::Path::new(dir))
            }
            .unwrap_or_else(|e| fail("restore-backup", e));
            println!("{out}");
            std::process::exit(0);
        }
        "audit-verify" => {
            // Checks the audit trail's hash chain (offline; safe while the store runs, as redb allows one
            // writer and concurrent readers only within a process, so stop the store first).
            let eng = Engine::open_full(&data, true, meta().as_deref(), node_name().as_deref(), &disk_config())
                .unwrap_or_else(|e| fail("open store", e));
            let entries = eng.audit_entries(0, usize::MAX).unwrap_or_else(|e| fail("read audit trail", e));
            let res = audit::verify(&entries, true);
            println!("{}", serde_json::to_string_pretty(&res).unwrap());
            if res["valid"] != true {
                std::process::exit(1);
            }
        }
        "healthcheck" => {
            let tls = !env("CAIRN_STORE_TLS_CERT", "").is_empty();
            let ok = std::net::TcpStream::connect_timeout(
                &listen_addr(&addr).replace("0.0.0.0", "127.0.0.1").parse::<SocketAddr>().unwrap_or_else(|e| fail("address", e)),
                Duration::from_secs(3),
            )
            .and_then(|mut s| {
                use std::io::{Read, Write};
                s.set_read_timeout(Some(Duration::from_secs(3)))?;
                let req = b"GET /-/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n";
                let mut buf = [0u8; 16];
                let n = if tls {
                    let cfg = tls::probe_config().map_err(std::io::Error::other)?;
                    let mut conn = rustls::ClientConnection::new(cfg, "localhost".try_into().map_err(std::io::Error::other)?)
                        .map_err(std::io::Error::other)?;
                    let mut t = rustls::Stream::new(&mut conn, &mut s);
                    t.write_all(req)?;
                    t.read(&mut buf)?
                } else {
                    s.write_all(req)?;
                    s.read(&mut buf)?
                };
                Ok(buf[..n].starts_with(b"HTTP/1.1 200"))
            })
            .unwrap_or(false);
            std::process::exit(if ok { 0 } else { 1 });
        }
        "serve" => {
            let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().unwrap_or_else(|e| fail("runtime", e));
            rt.block_on(serve(addr, data));
        }
        other => {
            eprintln!(
                "unknown command {other:?} (serve, fsck, audit-verify, admin, mount, csi, migrate-meta, restore-backup, healthcheck, version)"
            );
            std::process::exit(2);
        }
    }
}

async fn serve(addr: String, data: String) {
    let creds = credentials().unwrap_or_else(|e| fail("credentials", e));
    let fsync = env("CAIRN_STORE_FSYNC", "true") != "false";
    let cluster = cluster_config();
    let eng = Arc::new(
        Engine::open_full(&data, fsync, meta().as_deref(), cluster.as_ref().map(|c| c.node.as_str()), &disk_config())
            .unwrap_or_else(|e| fail("open store", e)),
    );
    let metadata = if eng.meta_is_postgres() { "postgresql" } else { "embedded" };
    let num = |k: &str, d: &str| -> u64 { env(k, d).parse().unwrap_or_else(|e| fail(k, e)) };
    let pack_default = if cluster.is_some() { "0" } else { "65536" };
    eng.set_packing(num("CAIRN_STORE_PACK_BELOW", pack_default), num("CAIRN_STORE_PACK_FILE_BYTES", "1073741824"));
    match env("CAIRN_STORE_COMPRESSION", "off").as_str() {
        "off" => {}
        "zstd" => eng.set_compression(true),
        other => fail("CAIRN_STORE_COMPRESSION", format!("{other:?}: use off or zstd")),
    }
    if !fsync {
        tracing::warn!("fsync is off: a crash or power loss can lose writes that were acknowledged");
    }
    for b in env("CAIRN_STORE_BUCKETS", "").split(',').map(str::trim).filter(|b| !b.is_empty()) {
        match eng.create_bucket(b) {
            Ok(()) | Err(engine::Error::BucketExists) => {}
            Err(e) => fail(&format!("create bucket {b}"), e),
        }
    }
    let keys = creds.len();
    let server = Arc::new(
        s3::Server::new(
            eng.clone(),
            s3::Config {
                region: env("CAIRN_STORE_REGION", "us-east-1"),
                domain: env("CAIRN_STORE_DOMAIN", ""),
                credentials: creds,
                max_object: 0,
                block_public: env("CAIRN_STORE_BLOCK_PUBLIC_ACCESS", "true") != "false",
                keyring: keyring().unwrap_or_else(|e| fail("keyring", e)),
                encrypt_by_default: env("CAIRN_STORE_ENCRYPT_BY_DEFAULT", "true") != "false",
                sse_c_over_http: env("CAIRN_STORE_SSE_C_ALLOW_HTTP", "false") == "true",
                website_domain: env("CAIRN_STORE_WEBSITE_DOMAIN", ""),
                event_targets: match env("CAIRN_STORE_EVENT_TARGETS_FILE", "") {
                    f if f.is_empty() => vec![],
                    f => s3::events::load_targets(&std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e)))
                        .unwrap_or_else(|e| fail(&f, e)),
                },
                log_requests: env("CAIRN_STORE_LOG_REQUESTS", "false") == "true",
                tls: !env("CAIRN_STORE_TLS_CERT", "").is_empty(),
                replication_targets: match env("CAIRN_STORE_REPLICATION_TARGETS_FILE", "") {
                    f if f.is_empty() => vec![],
                    f => s3::replication::load_targets(&std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e)))
                        .unwrap_or_else(|e| fail(&f, e)),
                },
                files_basic: env("CAIRN_STORE_FILES_BASIC_AUTH", "false") == "true",
                files_basic_http: env("CAIRN_STORE_FILES_BASIC_ALLOW_HTTP", "false") == "true",
                tiers: match env("CAIRN_STORE_TIERS_FILE", "") {
                    f if f.is_empty() => vec![],
                    f => s3::tiering::load_tiers(&std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e)))
                        .unwrap_or_else(|e| fail(&f, e)),
                },
                backup_root: match env("CAIRN_STORE_BACKUP_DIR", "") {
                    d if d.is_empty() => None,
                    d => {
                        std::fs::create_dir_all(&d).unwrap_or_else(|e| fail(&d, e));
                        Some(d.into())
                    }
                },
                event_max_age_hours: env("CAIRN_STORE_EVENT_MAX_AGE_HOURS", "168")
                    .parse()
                    .unwrap_or_else(|e| fail("CAIRN_STORE_EVENT_MAX_AGE_HOURS", e)),
                lifecycle_day_secs: env("CAIRN_STORE_LIFECYCLE_DAY_SECONDS", "86400")
                    .parse()
                    .unwrap_or_else(|e| fail("CAIRN_STORE_LIFECYCLE_DAY_SECONDS", e)),
                pdp: match env("CAIRN_STORE_PDP_URL", "") {
                    u if u.is_empty() => None,
                    u => Some((u, secret("CAIRN_STORE_PDP_TOKEN").unwrap_or_else(|e| fail("CAIRN_STORE_PDP_TOKEN", e)))),
                },
                cluster: cluster.clone(),
                backup_sinks: backup_sinks().unwrap_or_else(|e| fail("CAIRN_STORE_BACKUP_SINKS_FILE", e)),
                kafka: kafka_config().unwrap_or_else(|e| fail("CAIRN_STORE_KAFKA_ADDR", e)),
                lifecycle_secs: env("CAIRN_STORE_LIFECYCLE_INTERVAL", "3600").parse().unwrap_or(3600),
                sts: sts_config().unwrap_or_else(|e| fail("STS", e)),
                remotes: match env("CAIRN_STORE_REMOTES_FILE", "") {
                    f if f.is_empty() => vec![],
                    f => s3::remote::load_specs(&std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e)), "remote")
                        .unwrap_or_else(|e| fail(&f, e)),
                },
            },
        )
        .unwrap_or_else(|e| fail("configure", e)),
    );
    let lifecycle_every: u64 =
        env("CAIRN_STORE_LIFECYCLE_INTERVAL", "3600").parse().unwrap_or_else(|e| fail("CAIRN_STORE_LIFECYCLE_INTERVAL", e));
    tokio::spawn(server.clone().run_events());
    tokio::spawn(server.clone().run_cluster());
    tokio::spawn(server.clone().run_queue_retention(Duration::from_secs(num("CAIRN_STORE_QUEUE_RETENTION_INTERVAL", "60").max(1))));
    tokio::spawn(server.clone().run_remote_sync(Duration::from_secs(num("CAIRN_STORE_REMOTE_SYNC_INTERVAL", "60").max(1))));
    // Continuous backup: every object change goes to the sink's journal (point-in-time restore).
    let continuous = env("CAIRN_STORE_BACKUP_CONTINUOUS", "");
    if !continuous.is_empty() && !backup_sinks().unwrap_or_default().iter().any(|s| s.id == continuous) {
        fail("CAIRN_STORE_BACKUP_CONTINUOUS", format!("no backup sink {continuous} in CAIRN_STORE_BACKUP_SINKS_FILE"));
    }
    eng.set_journal((!continuous.is_empty()).then_some(continuous.as_str())).unwrap_or_else(|e| fail("CAIRN_STORE_BACKUP_CONTINUOUS", e));
    let sink_auto = env("CAIRN_STORE_BACKUP_SINK_SCHEDULE", "");
    if !sink_auto.is_empty() {
        if !backup_sinks().unwrap_or_default().iter().any(|s| s.id == sink_auto) {
            fail("CAIRN_STORE_BACKUP_SINK_SCHEDULE", format!("no backup sink {sink_auto} in CAIRN_STORE_BACKUP_SINKS_FILE"));
        }
        let every = num("CAIRN_STORE_BACKUP_SINK_INTERVAL", "86400").max(1);
        let keep = num("CAIRN_STORE_BACKUP_SINK_KEEP", "7").max(1) as usize;
        tokio::spawn(server.clone().run_sink_backups(sink_auto, Duration::from_secs(every), keep));
    }
    let flush: u64 =
        env("CAIRN_STORE_ACCESS_LOG_FLUSH_SECONDS", "60").parse().unwrap_or_else(|e| fail("CAIRN_STORE_ACCESS_LOG_FLUSH_SECONDS", e));
    tokio::spawn(server.clone().run_access_logs(Duration::from_secs(flush.max(1))));
    let compact_every = num("CAIRN_STORE_COMPACT_INTERVAL", "3600");
    let dead: f64 = env("CAIRN_STORE_COMPACT_DEAD_RATIO", "0.5").parse().unwrap_or_else(|e| fail("CAIRN_STORE_COMPACT_DEAD_RATIO", e));
    if compact_every > 0 && cluster.is_none() {
        let eng = eng.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(compact_every)).await;
                let e = eng.clone();
                if let Ok(Err(err)) = tokio::task::spawn_blocking(move || e.compact(dead, false)).await {
                    tracing::warn!(error = %err, "pack compaction");
                }
            }
        });
    }
    if lifecycle_every > 0 {
        tokio::spawn(server.clone().run_lifecycle(Duration::from_secs(lifecycle_every)));
    }
    let tls = match (env("CAIRN_STORE_TLS_CERT", ""), env("CAIRN_STORE_TLS_KEY", "")) {
        (c, _) if c.is_empty() => None,
        (_, k) if k.is_empty() => fail("TLS", "CAIRN_STORE_TLS_KEY is required with CAIRN_STORE_TLS_CERT"),
        (c, k) => {
            let files = tls::TlsFiles {
                cert: c.into(),
                key: k.into(),
                client_ca: Some(env("CAIRN_STORE_TLS_CLIENT_CA", "")).filter(|v| !v.is_empty()).map(Into::into),
            };
            let r = tls::Reloading::new(files).unwrap_or_else(|e| fail("TLS", e));
            tokio::spawn(r.clone().watch());
            Some(r)
        }
    };
    if let Some(addr) = server.kafka.as_ref().map(|k| k.addr.clone()) {
        if tls.is_none() && env("CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT", "false") != "true" {
            fail(
                "CAIRN_STORE_KAFKA_ADDR",
                "Kafka sign-in (SASL/PLAIN) needs TLS (CAIRN_STORE_TLS_CERT); set CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT=true to serve it in the clear",
            );
        }
        let l = TcpListener::bind(listen_addr(&addr)).await.unwrap_or_else(|e| fail("CAIRN_STORE_KAFKA_ADDR", e));
        tracing::info!(addr = %addr, tls = tls.is_some(), "kafka listening");
        tokio::spawn(server.clone().run_kafka(l, tls.clone()));
    }
    let sftp_addr = env("CAIRN_STORE_SFTP_ADDR", "");
    if !sftp_addr.is_empty() {
        let cfg = s3::sftp::SftpConfig {
            addr: sftp_addr,
            host_key: match env("CAIRN_STORE_SFTP_HOST_KEY", "") {
                k if k.is_empty() => std::path::Path::new(&data).join("sftp_host_ed25519_key"),
                k => k.into(),
            },
            password_auth: env("CAIRN_STORE_SFTP_PASSWORD_AUTH", "true") == "true",
        };
        let srv = server.clone();
        // A bad SFTP setting (host key, address, ssh_keys) stops the store rather than being ignored.
        tokio::spawn(async move {
            if let Err(e) = s3::sftp::serve(srv, cfg).await {
                fail("SFTP", e)
            }
        });
    }
    let listener = TcpListener::bind(listen_addr(&addr)).await.unwrap_or_else(|e| fail("listen", e));
    tracing::info!(addr = %addr, data = %data, version = VERSION, keys, fsync, tls = tls.is_some(), metadata, node = cluster.as_ref().map(|c| c.node.as_str()).unwrap_or(""), "cairn-store listening");
    let mut shutdown = Box::pin(async {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    });
    let graceful = hyper_util::server::graceful::GracefulShutdown::new();
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, peer)) = accepted else { continue };
                let server = server.clone();
                let svc = service_fn(move |req| server.clone().handle_from(req, Some(peer.ip())));
                let mut builder = http1::Builder::new();
                builder.timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_secs(30)).keep_alive(true);
                match &tls {
                    None => {
                        let conn = graceful.watch(builder.serve_connection(TokioIo::new(stream), svc));
                        tokio::spawn(async move {
                            if let Err(e) = conn.await {
                                tracing::debug!(error = %e, "connection");
                            }
                        });
                    }
                    Some(t) => {
                        let (acceptor, watcher) = (t.acceptor(), graceful.watcher());
                        tokio::spawn(async move {
                            match tokio::time::timeout(Duration::from_secs(10), acceptor.accept(stream)).await {
                                Ok(Ok(s)) => {
                                    if let Err(e) = watcher.watch(builder.serve_connection(TokioIo::new(s), svc)).await {
                                        tracing::debug!(error = %e, "connection");
                                    }
                                }
                                Ok(Err(e)) => tracing::debug!(error = %e, peer = %peer, "TLS handshake"),
                                Err(_) => tracing::debug!(peer = %peer, "TLS handshake timed out"),
                            }
                        });
                    }
                }
            }
            _ = &mut shutdown => break,
        }
    }
    // Stop taking requests; idle connections close at once, requests in flight get 30 s to finish.
    tracing::info!("shutting down");
    if tokio::time::timeout(Duration::from_secs(30), graceful.shutdown()).await.is_err() {
        tracing::warn!("requests still running after 30 s; exiting anyway");
    }
}
