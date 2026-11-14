//! `cairn-store bench`: a load generator for an S3 endpoint (this store, or any S3-compatible one).
//!
//! ```text
//! cairn-store bench [--bucket B] [--objects N] [--size 64KiB] [--concurrency C] [--ops put,get,list,delete] [--keep]
//! ```
//!
//! Writes N objects of the given size under a fresh prefix (`cairn-bench/<run>/`) with C requests in
//! flight, reads them back (checking every byte count and ETag), lists them and deletes them, and prints
//! one JSON report: per phase, operations, errors, seconds, operations and MiB per second, and latency
//! percentiles. The bucket (default `cairn-bench`) is created when missing and removed afterwards if
//! this run created it and left it empty (`--keep` keeps it). Endpoint and key as for `cairn-store admin`
//! (CAIRN_STORE_ENDPOINT, CAIRN_STORE_ACCESS_KEY, CAIRN_STORE_SECRET_KEY). Exits 1 when any request failed.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use serde_json::{json, Value};
use sha2::Digest;

use crate::s3::auth::aws_encode;

type Http = Client<hyper_rustls::HttpsConnector<HttpConnector>, Full<Bytes>>;

struct Opts {
    bucket: String,
    objects: usize,
    size: usize,
    concurrency: usize,
    ops: Vec<String>,
    keep: bool,
}

/// `4096`, `64KiB`, `64k`, `4MiB`, `1GiB` (binary units).
pub(crate) fn parse_size(s: &str) -> Option<usize> {
    let s = s.trim();
    let digits = s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    let (n, unit) = s.split_at(digits);
    let n: usize = n.parse().ok()?;
    let mult = match unit.to_ascii_lowercase().as_str() {
        "" | "b" => 1,
        "k" | "kib" | "kb" => 1 << 10,
        "m" | "mib" | "mb" => 1 << 20,
        "g" | "gib" | "gb" => 1 << 30,
        _ => return None,
    };
    n.checked_mul(mult).filter(|v| *v <= 5 << 30)
}

fn parse(args: &[String]) -> Result<Opts, String> {
    let mut o = Opts { bucket: "cairn-bench".into(), objects: 1000, size: 64 << 10, concurrency: 16, ops: vec![], keep: false };
    let mut it = args.iter();
    let ops_all = ["put", "get", "list", "delete"];
    o.ops = ops_all.iter().map(|s| s.to_string()).collect();
    while let Some(a) = it.next() {
        let mut val = || it.next().cloned().ok_or(format!("{a} needs a value"));
        match a.as_str() {
            "--bucket" => o.bucket = val()?,
            "--objects" => o.objects = val()?.parse().ok().filter(|n| *n > 0).ok_or("--objects is a positive number")?,
            "--size" => o.size = parse_size(&val()?).ok_or("--size is like 4096, 64KiB, 4MiB or 1GiB (at most 5 GiB)")?,
            "--concurrency" => {
                o.concurrency = val()?.parse().ok().filter(|n| (1..=1024).contains(n)).ok_or("--concurrency is 1 to 1024")?
            }
            "--ops" => {
                o.ops = val()?.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
                if o.ops.is_empty() || o.ops.iter().any(|p| !ops_all.contains(&p.as_str())) {
                    return Err("--ops is a list of put, get, list and delete".into());
                }
            }
            "--keep" => o.keep = true,
            other => return Err(format!("unknown argument {other}")),
        }
    }
    if o.ops.iter().any(|p| p != "put") && !o.ops.iter().any(|p| p == "put") {
        return Err("get, list and delete need put in the same run (they work on the objects it writes)".into());
    }
    Ok(o)
}

struct Ctx {
    http: Http,
    endpoint: String,
    ak: String,
    sk: String,
    region: String,
}

impl Ctx {
    async fn send(&self, method: &str, url: &str, body: Bytes, hash: &str) -> Result<(u16, hyper::HeaderMap, Bytes), String> {
        let mut req = hyper::Request::builder().method(method).uri(url);
        for (k, v) in
            crate::admin_cli::sign_with_payload(method, url, hash, &self.ak, &self.sk, &self.region, time::OffsetDateTime::now_utc())
        {
            req = req.header(k, v);
        }
        if method == "PUT" {
            req = req.header("content-length", body.len().to_string());
        }
        let req = req.body(Full::new(body)).map_err(|e| e.to_string())?;
        let resp = tokio::time::timeout(Duration::from_secs(300), self.http.request(req))
            .await
            .map_err(|_| "timed out".to_string())?
            .map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let headers = resp.headers().clone();
        let b = resp.into_body().collect().await.map_err(|e| e.to_string())?.to_bytes();
        Ok((status, headers, b))
    }

    fn url(&self, bucket: &str, key: &str, query: &str) -> String {
        let mut u = format!("{}/{}", self.endpoint, aws_encode(bucket.as_bytes(), false));
        if !key.is_empty() {
            u.push('/');
            u.push_str(&aws_encode(key.as_bytes(), true));
        }
        if !query.is_empty() {
            u.push('?');
            u.push_str(query);
        }
        u
    }
}

/// One phase's measurements.
#[derive(Default)]
struct Phase {
    lat_us: Vec<u64>,
    errors: usize,
    bytes: u64,
    first_error: String,
}

fn report(op: &str, mut p: Phase, elapsed: Duration) -> Value {
    p.lat_us.sort_unstable();
    let pct = |q: f64| -> f64 {
        if p.lat_us.is_empty() {
            return 0.0;
        }
        let i = ((p.lat_us.len() as f64 - 1.0) * q).round() as usize;
        p.lat_us[i] as f64 / 1000.0
    };
    let secs = elapsed.as_secs_f64().max(1e-9);
    let n = p.lat_us.len();
    let mean = if n == 0 { 0.0 } else { p.lat_us.iter().sum::<u64>() as f64 / n as f64 / 1000.0 };
    let r = |v: f64| (v * 1000.0).round() / 1000.0;
    let mut v = json!({
        "op": op, "ops": n, "errors": p.errors, "seconds": r(secs), "ops_per_second": r(n as f64 / secs),
        "mib_per_second": r(p.bytes as f64 / secs / (1 << 20) as f64),
        "latency_ms": {"mean": r(mean), "p50": r(pct(0.5)), "p90": r(pct(0.9)), "p99": r(pct(0.99)), "max": r(pct(1.0))},
    });
    if p.errors > 0 {
        v["first_error"] = json!(p.first_error);
    }
    v
}

/// Runs `n` operations with `c` in flight; `f(i)` performs operation i and returns the bytes it moved.
async fn run_phase<F, Fut>(n: usize, c: usize, f: F) -> (Phase, Duration)
where
    F: Fn(usize) -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = Result<u64, String>> + Send,
{
    let f = Arc::new(f);
    let next = Arc::new(AtomicUsize::new(0));
    let start = Instant::now();
    let mut workers = tokio::task::JoinSet::new();
    for _ in 0..c.min(n) {
        let (f, next) = (f.clone(), next.clone());
        workers.spawn(async move {
            let mut p = Phase::default();
            loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                if i >= n {
                    return p;
                }
                let t = Instant::now();
                match f(i).await {
                    Ok(b) => {
                        p.lat_us.push(t.elapsed().as_micros() as u64);
                        p.bytes += b;
                    }
                    Err(e) => {
                        p.errors += 1;
                        if p.first_error.is_empty() {
                            p.first_error = e;
                        }
                    }
                }
            }
        });
    }
    let mut all = Phase::default();
    while let Some(Ok(p)) = workers.join_next().await {
        all.lat_us.extend(p.lat_us);
        all.errors += p.errors;
        all.bytes += p.bytes;
        if all.first_error.is_empty() {
            all.first_error = p.first_error;
        }
    }
    (all, start.elapsed())
}

pub fn run(args: &[String], endpoint: &str, ak: &str, sk: &str, region: &str) -> i32 {
    let o = match parse(args) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("bench: {e}\nusage: cairn-store bench [--bucket B] [--objects N] [--size 64KiB] [--concurrency C] [--ops put,get,list,delete] [--keep]");
            return 2;
        }
    };
    let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().expect("runtime");
    rt.block_on(async move {
        let http = match crate::tls::https_connector() {
            Ok(c) => Client::builder(TokioExecutor::new()).pool_max_idle_per_host(o.concurrency).build(c),
            Err(e) => {
                eprintln!("bench: {e}");
                return 2;
            }
        };
        let cx = Arc::new(Ctx {
            http,
            endpoint: endpoint.trim_end_matches('/').to_string(),
            ak: ak.into(),
            sk: sk.into(),
            region: region.into(),
        });
        let empty_hash = hex::encode(sha2::Sha256::digest(b""));
        // The bucket: used if it is there, created (and later removed if left empty) if not.
        let created = match cx.send("PUT", &cx.url(&o.bucket, "", ""), Bytes::new(), &empty_hash).await {
            Ok((200, _, _)) => true,
            Ok((409, _, b)) if String::from_utf8_lossy(&b).contains("BucketAlreadyOwnedByYou") => false,
            Ok((s, _, b)) => {
                eprintln!(
                    "bench: cannot use bucket {}: HTTP {s}: {}",
                    o.bucket,
                    String::from_utf8_lossy(&b).chars().take(300).collect::<String>()
                );
                return 2;
            }
            Err(e) => {
                eprintln!("bench: {}: {e}", cx.endpoint);
                return 2;
            }
        };
        let run_id = format!("{}-{}", crate::engine::now_ms(), hex::encode(rand::random::<[u8; 3]>()));
        let prefix = format!("cairn-bench/{run_id}/");
        let key = {
            let prefix = prefix.clone();
            move |i: usize| format!("{prefix}{i:08}")
        };
        let body: Bytes = (0..o.size).map(|_| rand::random::<u8>()).collect::<Vec<u8>>().into();
        let hash = hex::encode(sha2::Sha256::digest(&body));
        let etag = hex::encode(md5::Md5::digest(&body));
        let mut phases = vec![];
        let mut failed = false;
        for op in &o.ops {
            let (cx2, key2, bucket) = (cx.clone(), key.clone(), o.bucket.clone());
            let (p, el) = match op.as_str() {
                "put" => {
                    let (body, hash, etag) = (body.clone(), hash.clone(), etag.clone());
                    run_phase(o.objects, o.concurrency, move |i| {
                        let (cx, url, body, hash, etag) =
                            (cx2.clone(), cx2.url(&bucket, &key2(i), ""), body.clone(), hash.clone(), etag.clone());
                        async move {
                            let n = body.len() as u64;
                            match cx.send("PUT", &url, body, &hash).await? {
                                (200, h, _) if h.get("etag").and_then(|v| v.to_str().ok()).is_some_and(|v| v.trim_matches('"') == etag) => {
                                    Ok(n)
                                }
                                (200, _, _) => Err("PUT answered with another ETag".into()),
                                (s, _, b) => {
                                    Err(format!("PUT: HTTP {s}: {}", String::from_utf8_lossy(&b).chars().take(200).collect::<String>()))
                                }
                            }
                        }
                    })
                    .await
                }
                "get" => {
                    let (eh, size, etag) = (empty_hash.clone(), o.size, etag.clone());
                    run_phase(o.objects, o.concurrency, move |i| {
                        let (cx, url, eh, etag) = (cx2.clone(), cx2.url(&bucket, &key2(i), ""), eh.clone(), etag.clone());
                        async move {
                            match cx.send("GET", &url, Bytes::new(), &eh).await? {
                                (200, h, b)
                                    if b.len() == size
                                        && h.get("etag").and_then(|v| v.to_str().ok()).is_some_and(|v| v.trim_matches('"') == etag) =>
                                {
                                    Ok(b.len() as u64)
                                }
                                (200, _, b) => Err(format!("GET returned {} bytes or another ETag (expected {size})", b.len())),
                                (s, _, _) => Err(format!("GET: HTTP {s}")),
                            }
                        }
                    })
                    .await
                }
                "list" => {
                    // Pages of 1000 keys under the run's prefix, one after another (a listing is sequential).
                    let start = Instant::now();
                    let mut p = Phase::default();
                    let mut token = String::new();
                    let mut seen = 0usize;
                    loop {
                        let mut q = vec![("list-type", "2".to_string()), ("max-keys", "1000".into()), ("prefix", prefix.clone())];
                        if !token.is_empty() {
                            q.push(("continuation-token", token.clone()));
                        }
                        q.sort();
                        let qs = q.iter().map(|(k, v)| format!("{k}={}", aws_encode(v.as_bytes(), false))).collect::<Vec<_>>().join("&");
                        let t = Instant::now();
                        match cx.send("GET", &cx.url(&o.bucket, "", &qs), Bytes::new(), &empty_hash).await {
                            Ok((200, _, b)) => {
                                p.lat_us.push(t.elapsed().as_micros() as u64);
                                let doc = String::from_utf8_lossy(&b).into_owned();
                                let root = crate::s3::xml::parse(&doc);
                                seen += root.as_ref().map_or(0, |r| r.all("Contents").count());
                                token = root.as_ref().map(|r| r.text_of("NextContinuationToken")).unwrap_or_default();
                                if root.is_none_or(|r| r.text_of("IsTruncated") != "true") {
                                    break;
                                }
                            }
                            other => {
                                p.errors += 1;
                                p.first_error = format!("{:?}", other.map(|(s, _, _)| s));
                                break;
                            }
                        }
                    }
                    let want = if o.ops.iter().any(|x| x == "put") { o.objects } else { seen };
                    if seen != want {
                        p.errors += 1;
                        p.first_error = format!("listed {seen} keys, expected {want}");
                    }
                    (p, start.elapsed())
                }
                _ => {
                    let eh = empty_hash.clone();
                    run_phase(o.objects, o.concurrency, move |i| {
                        let (cx, url, eh) = (cx2.clone(), cx2.url(&bucket, &key2(i), ""), eh.clone());
                        async move {
                            match cx.send("DELETE", &url, Bytes::new(), &eh).await? {
                                (204 | 200, _, _) => Ok(0),
                                (s, _, _) => Err(format!("DELETE: HTTP {s}")),
                            }
                        }
                    })
                    .await
                }
            };
            failed |= p.errors > 0;
            phases.push(report(op, p, el));
        }
        let deleted = o.ops.iter().any(|x| x == "delete");
        let mut removed_bucket = false;
        if created && deleted && !o.keep {
            removed_bucket = matches!(cx.send("DELETE", &cx.url(&o.bucket, "", ""), Bytes::new(), &empty_hash).await, Ok((204, _, _)));
        }
        let out = json!({
            "endpoint": cx.endpoint, "bucket": o.bucket, "prefix": prefix, "objects": o.objects, "size_bytes": o.size,
            "concurrency": o.concurrency, "phases": phases, "objects_left": !deleted, "bucket_removed": removed_bucket,
        });
        println!("{}", serde_json::to_string_pretty(&out).unwrap());
        if failed {
            1
        } else {
            0
        }
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn sizes() {
        assert_eq!(super::parse_size("4096"), Some(4096));
        assert_eq!(super::parse_size("64KiB"), Some(65536));
        assert_eq!(super::parse_size("64k"), Some(65536));
        assert_eq!(super::parse_size("4MiB"), Some(4 << 20));
        assert_eq!(super::parse_size("1g"), Some(1 << 30));
        for bad in ["", "MiB", "4TiB", "1.5MiB", "-1", "6GiB"] {
            assert_eq!(super::parse_size(bad), None, "{bad}");
        }
    }
}
