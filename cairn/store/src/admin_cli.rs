//! `cairn-store admin ...`: the admin API from the command line (signed with SigV4).
//!
//! ```text
//! cairn-store admin stats
//! cairn-store admin buckets
//! cairn-store admin bucket <name>
//! cairn-store admin quota <name> [--max-bytes N] [--max-objects N]
//! cairn-store admin quota-remove <name>
//! cairn-store admin audit [--after SEQ] [--jsonl]
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
            (aws_encode(k.as_bytes(), false), aws_encode(v.as_bytes(), false))
        })
        .collect();
    pairs.sort();
    let cq = pairs.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join("&");
    let payload = hex::encode(Sha256::digest(body));
    let canonical = format!(
        "{method}\n{}\n{cq}\nhost:{host}\nx-amz-content-sha256:{payload}\nx-amz-date:{amz_date}\n\nhost;x-amz-content-sha256;x-amz-date\n{payload}",
        aws_encode(path.as_bytes(), true)
    );
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
        (
            "authorization".into(),
            format!("AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={sig}"),
        ),
    ]
}

fn usage() -> ! {
    eprintln!("usage: cairn-store admin stats | buckets | bucket <name> | quota <name> [--max-bytes N] [--max-objects N] | quota-remove <name> | audit [--after SEQ] [--jsonl]");
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
