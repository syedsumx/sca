//! `cairn-store admin ...`: the admin API from the command line (signed with SigV4).
//!
//! ```text
//! cairn-store admin stats
//! cairn-store admin buckets
//! cairn-store admin bucket <name>
//! cairn-store admin quota <name> [--max-bytes N] [--max-objects N]
//! cairn-store admin quota-remove <name>
//! cairn-store admin audit [--after SEQ] [--jsonl]
//! cairn-store admin backup <name>         (an online backup under CAIRN_STORE_BACKUP_DIR)
//! cairn-store admin backups [--sink ID]
//! cairn-store admin backup <name> --sink ID    (to an S3-compatible sink from CAIRN_STORE_BACKUP_SINKS_FILE)
//! cairn-store admin backup-delete <name> --sink ID
//! cairn-store admin compact [--min-dead 0.3]   (compact packs of small objects now)
//! cairn-store admin keys | key <ak>
//! cairn-store admin key-create [--name AK] [--admin] [--read-only] [--buckets a,b*] [--policy-file F]
//!                              [--actor URN] [--ssh-key-file F] [--description TEXT]
//! cairn-store admin key-update <ak> [the same options]
//! cairn-store admin key-rotate <ak>
//! cairn-store admin key-delete <ak>
//! cairn-store admin disks | bucket-disk <bucket> [--type ssd]   (no --type: back to the default type)
//! cairn-store admin remote <bucket> | remote-mount <bucket> --remote ID [--write-back] | remote-unmount <bucket>
//! cairn-store admin remote-sync <bucket> | remote-cache <bucket> [--prefix P] | remote-uncache <bucket> [--prefix P]
//! cairn-store admin replication-resync <bucket>  (send objects written before replication was configured)
//! cairn-store admin cluster                (nodes and blob health)
//! cairn-store admin cluster-repair | cluster-rebalance [--limit N]
//! cairn-store admin cluster-drain <node> | cluster-activate <node> | cluster-remove <node>
//! ```
//!
//! Endpoint: `CAIRN_STORE_ENDPOINT` (default: this store's listen address, http or https as configured);
//! key: `CAIRN_STORE_ACCESS_KEY` / `CAIRN_STORE_SECRET_KEY` (an admin key).

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use sha2::{Digest, Sha256};

use crate::s3::auth::{aws_encode, hmac_sha256};

/// SigV4 headers (service "s3", signed payload).
pub fn sign(method: &str, url: &str, body: &[u8], ak: &str, sk: &str, region: &str, now: time::OffsetDateTime) -> Vec<(String, String)> {
    sign_with_payload(method, url, &hex::encode(Sha256::digest(body)), ak, sk, region, now)
}

/// SigV4 headers for a payload hash (a hex SHA-256, or "UNSIGNED-PAYLOAD").
pub fn sign_with_payload(
    method: &str,
    url: &str,
    payload: &str,
    ak: &str,
    sk: &str,
    region: &str,
    now: time::OffsetDateTime,
) -> Vec<(String, String)> {
    sign_full(method, url, payload, &[], ak, sk, region, now)
}

/// SigV4 headers, also signing `extra` headers (lower-case names, e.g. x-amz-copy-source, range).
#[allow(clippy::too_many_arguments)]
pub fn sign_full(
    method: &str,
    url: &str,
    payload: &str,
    extra: &[(&str, &str)],
    ak: &str,
    sk: &str,
    region: &str,
    now: time::OffsetDateTime,
) -> Vec<(String, String)> {
    let amz_date = now.format(time::macros::format_description!("[year][month][day]T[hour][minute][second]Z")).unwrap_or_default();
    let date = &amz_date[..8];
    let rest = url.split_once("://").map(|(_, r)| r).unwrap_or(url);
    let (host, path_q) = rest.split_once('/').map(|(h, p)| (h, format!("/{p}"))).unwrap_or((rest, "/".into()));
    let (path, query) = path_q.split_once('?').unwrap_or((&path_q, ""));
    let mut pairs: Vec<(String, String)> = query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            // Canonical form: decode what the URL carries, then encode as SigV4 does.
            (aws_encode(&crate::s3::auth::percent_decode(k), false), aws_encode(&crate::s3::auth::percent_decode(v), false))
        })
        .collect();
    pairs.sort();
    let cq = pairs.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join("&");
    let payload = payload.to_string();
    let mut signed: Vec<(String, String)> =
        vec![("host".into(), host.to_string()), ("x-amz-content-sha256".into(), payload.clone()), ("x-amz-date".into(), amz_date.clone())];
    signed.extend(extra.iter().map(|(k, v)| (k.to_ascii_lowercase(), v.trim().to_string())));
    signed.sort();
    let names = signed.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>().join(";");
    let lines: String = signed.iter().map(|(k, v)| format!("{k}:{v}\n")).collect();
    let canonical = format!("{method}\n{}\n{cq}\n{lines}\n{names}\n{payload}", aws_encode(path.as_bytes(), true));
    let scope = format!("{date}/{region}/s3/aws4_request");
    let to_sign = format!("AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}", hex::encode(Sha256::digest(canonical.as_bytes())));
    let k = hmac_sha256(
        &hmac_sha256(&hmac_sha256(&hmac_sha256(format!("AWS4{sk}").as_bytes(), date.as_bytes()), region.as_bytes()), b"s3"),
        b"aws4_request",
    );
    let sig = hex::encode(hmac_sha256(&k, to_sign.as_bytes()));
    vec![
        ("x-amz-content-sha256".into(), payload),
        ("x-amz-date".into(), amz_date),
        ("authorization".into(), format!("AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={names}, Signature={sig}")),
    ]
}

fn usage() -> ! {
    eprintln!("usage: cairn-store admin stats | buckets | bucket <name> | quota <name> [--max-bytes N] [--max-objects N] | quota-remove <name> | audit [--after SEQ] [--jsonl] | backup <name> | backups | compact [--min-dead R] | keys | key <ak> | key-create [options] | key-update <ak> [options] | key-rotate <ak> | key-delete <ak> | cluster | cluster-repair | cluster-rebalance [--limit N] | cluster-drain|cluster-activate|cluster-remove <node>");
    std::process::exit(2)
}

/// Runs an admin command; returns the process exit code.
pub fn run(args: &[String], endpoint: &str, ak: &str, sk: &str, region: &str) -> i32 {
    let mut it = args.iter();
    let cmd = it.next().map(String::as_str).unwrap_or_else(|| usage());
    let rest: Vec<&String> = it.collect();
    let flag = |name: &str| rest.iter().position(|a| a.as_str() == name).and_then(|i| rest.get(i + 1)).map(|s| s.to_string());
    let name = || rest.first().filter(|a| !a.starts_with("--")).map(|s| aws_encode(s.as_bytes(), false)).unwrap_or_else(|| usage());
    let (method, path, body) = match cmd {
        "stats" => ("GET", "stats".to_string(), None),
        "buckets" => ("GET", "buckets".to_string(), None),
        "bucket" => ("GET", format!("buckets/{}", name()), None),
        "quota" => {
            let num = |f: &str| flag(f).map(|v| v.parse::<u64>().unwrap_or_else(|_| usage()));
            let body = serde_json::json!({"max_bytes": num("--max-bytes"), "max_objects": num("--max-objects")});
            ("PUT", format!("buckets/{}/quota", name()), Some(body))
        }
        "quota-remove" => ("DELETE", format!("buckets/{}/quota", name()), None),
        "backup" => {
            let mut b = serde_json::json!({"name": rest.first().map(|s| s.as_str()).unwrap_or_else(|| usage())});
            if let Some(s) = flag("--sink") {
                b["sink"] = s.into();
            }
            ("POST", "backups".to_string(), Some(b))
        }
        "backups" => match flag("--sink") {
            Some(s) => ("GET", format!("backups?sink={}", aws_encode(s.as_bytes(), false)), None),
            None => ("GET", "backups".to_string(), None),
        },
        "backup-delete" => {
            let s = flag("--sink").unwrap_or_else(|| usage());
            ("DELETE", format!("backups/{}?sink={}", name(), aws_encode(s.as_bytes(), false)), None)
        }
        "keys" => ("GET", "keys".to_string(), None),
        "compact" => {
            let min = flag("--min-dead").map(|v| v.parse::<f64>().unwrap_or_else(|_| usage())).unwrap_or(0.0);
            ("POST", "compact".to_string(), Some(serde_json::json!({"min_dead": min})))
        }
        "disks" => ("GET", "disks".to_string(), None),
        "bucket-disk" => match flag("--type") {
            Some(t) => ("PUT", format!("buckets/{}/disk", name()), Some(serde_json::json!({"type": t}))),
            None => ("DELETE", format!("buckets/{}/disk", name()), None),
        },
        "remote" => ("GET", format!("buckets/{}/remote", name()), None),
        "remote-mount" => {
            let remote = flag("--remote").unwrap_or_else(|| usage());
            let wb = rest.iter().any(|a| a.as_str() == "--write-back");
            ("PUT", format!("buckets/{}/remote", name()), Some(serde_json::json!({"remote": remote, "write_back": wb})))
        }
        "remote-unmount" => ("DELETE", format!("buckets/{}/remote", name()), None),
        "remote-sync" => ("POST", format!("buckets/{}/remote/sync", name()), None),
        "remote-cache" | "remote-uncache" => (
            "POST",
            format!("buckets/{}/remote/{}", name(), cmd.trim_start_matches("remote-")),
            Some(serde_json::json!({"prefix": flag("--prefix").unwrap_or_default()})),
        ),
        "replication-resync" => ("POST", format!("buckets/{}/replication/resync", name()), None),
        "cluster" => ("GET", "cluster".to_string(), None),
        "cluster-repair" => ("POST", "cluster/repair".to_string(), None),
        "cluster-rebalance" => {
            let limit = flag("--limit").map(|v| v.parse::<u64>().unwrap_or_else(|_| usage())).unwrap_or(10_000);
            ("POST", "cluster/rebalance".to_string(), Some(serde_json::json!({"limit": limit})))
        }
        "cluster-drain" => ("POST", format!("cluster/nodes/{}/drain", name()), None),
        "cluster-activate" => ("POST", format!("cluster/nodes/{}/activate", name()), None),
        "cluster-remove" => ("POST", format!("cluster/nodes/{}/remove", name()), None),
        "key" => ("GET", format!("keys/{}", name()), None),
        "key-rotate" => ("POST", format!("keys/{}/rotate", name()), None),
        "key-delete" => ("DELETE", format!("keys/{}", name()), None),
        "key-create" | "key-update" => {
            let has = |f: &str| rest.iter().any(|a| a.as_str() == f);
            let read = |f: &str| {
                flag(f).map(|p| {
                    std::fs::read_to_string(&p).unwrap_or_else(|e| {
                        eprintln!("{p}: {e}");
                        std::process::exit(2)
                    })
                })
            };
            let mut b = serde_json::Map::new();
            if cmd == "key-create" {
                if let Some(n) = flag("--name") {
                    b.insert("access_key".into(), n.into());
                }
            }
            if has("--admin") {
                b.insert("admin".into(), true.into());
            }
            if has("--not-admin") {
                b.insert("admin".into(), false.into());
            }
            if has("--read-only") {
                b.insert("read_only".into(), true.into());
            }
            if has("--read-write") {
                b.insert("read_only".into(), false.into());
            }
            if let Some(v) = flag("--buckets") {
                b.insert("buckets".into(), v.split(',').filter(|x| !x.is_empty()).collect::<Vec<_>>().into());
            }
            if let Some(v) = read("--policy-file") {
                b.insert(
                    "policy".into(),
                    serde_json::from_str(&v).unwrap_or_else(|e| {
                        eprintln!("--policy-file: {e}");
                        std::process::exit(2)
                    }),
                );
            }
            if let Some(v) = flag("--actor") {
                b.insert("actor".into(), v.into());
            }
            if let Some(v) = read("--ssh-key-file") {
                b.insert("ssh_keys".into(), v.lines().filter(|l| !l.trim().is_empty()).collect::<Vec<_>>().into());
            }
            if let Some(v) = flag("--description") {
                b.insert("description".into(), v.into());
            }
            if cmd == "key-create" {
                ("POST", "keys".to_string(), Some(serde_json::Value::Object(b)))
            } else {
                ("PATCH", format!("keys/{}", name()), Some(serde_json::Value::Object(b)))
            }
        }
        "audit" => {
            let after = flag("--after").unwrap_or_else(|| "0".into());
            let fmt = if rest.iter().any(|a| a.as_str() == "--jsonl") { "&format=jsonl" } else { "" };
            ("GET", format!("audit?after={after}&limit=10000{fmt}"), None)
        }
        _ => usage(),
    };
    let body: Vec<u8> = body
        .map(|b| {
            let mut b = b;
            if let Some(m) = b.as_object_mut() {
                m.retain(|_, v| !v.is_null());
            }
            b.to_string().into_bytes()
        })
        .unwrap_or_default();
    let url = format!("{}/-/admin/v1/{path}", endpoint.trim_end_matches('/'));
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().expect("runtime");
    rt.block_on(async {
        let conn = match crate::tls::https_connector() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("cairn-store admin: {e}");
                return 1;
            }
        };
        let client: Client<_, Full<Bytes>> = Client::builder(TokioExecutor::new()).build(conn);
        let mut req = hyper::Request::builder().method(method).uri(&url);
        for (k, v) in sign(method, &url, &body, ak, sk, region, time::OffsetDateTime::now_utc()) {
            req = req.header(k, v);
        }
        if !body.is_empty() {
            req = req.header("content-type", "application/json");
        }
        let Ok(req) = req.body(Full::new(Bytes::from(body))) else { return 2 };
        match client.request(req).await {
            Ok(resp) => {
                let ok = resp.status().is_success();
                let bytes = resp.into_body().collect().await.map(|b| b.to_bytes()).unwrap_or_default();
                let text = String::from_utf8_lossy(&bytes);
                match serde_json::from_str::<serde_json::Value>(&text) {
                    Ok(v) => println!("{}", serde_json::to_string_pretty(&v).unwrap_or_default()),
                    Err(_) => print!("{text}"),
                }
                if ok {
                    0
                } else {
                    1
                }
            }
            Err(e) => {
                eprintln!("cairn-store admin: {url}: {e}");
                1
            }
        }
    })
}
