//! Storage tiers: lifecycle transitions move object data to a bucket on another S3 endpoint (a cloud
//! bucket, or another Cairn Store), and reads fetch it from there.
//!
//! Tiers are defined by the operator (`CAIRN_STORE_TIERS_FILE`: `[{"storage_class", "endpoint", "bucket",
//! "prefix", "access_key", "secret_key" | "secret_key_file", "region"}]`); a lifecycle rule names one by its
//! storage class. A transition copies each blob exactly as it is stored on disk (so with encryption at
//! rest the tier only ever holds ciphertext), then swaps the version's segments in one index transaction,
//! only if the version is unchanged; the local blobs are collected afterwards. Reads are ranged GETs on
//! the tier, so tiered objects stay readable at once (S3's instant-retrieval classes); classes that need a
//! restore first (GLACIER, DEEP_ARCHIVE) are refused. Deleting a tiered version deletes its blobs in the
//! tier, from a background task.

use std::collections::BTreeMap;
use std::io::Read;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::Frame;
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use serde::Deserialize;

use super::errors::{self as e, ApiError};
use super::{Ctx, Server};
use crate::engine::{Object, Segment, TierStore};

type Body = BoxBody<Bytes, std::io::Error>;

/// The storage classes a tier may have: those S3 reads without a restore.
pub const TIER_CLASSES: [&str; 3] = ["STANDARD_IA", "ONEZONE_IA", "GLACIER_IR"];

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tier {
    pub storage_class: String,
    pub endpoint: String,
    pub bucket: String,
    #[serde(default)]
    pub prefix: String,
    pub access_key: String,
    #[serde(default)]
    pub secret_key: String,
    #[serde(default)]
    pub secret_key_file: String,
    #[serde(default = "us_east_1")]
    pub region: String,
}

fn us_east_1() -> String {
    "us-east-1".into()
}

pub fn load_tiers(text: &str) -> Result<Vec<Tier>, String> {
    let mut ts: Vec<Tier> = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let mut seen = std::collections::HashSet::new();
    for t in ts.iter_mut() {
        if !TIER_CLASSES.contains(&t.storage_class.as_str()) {
            return Err(format!(
                "tier {:?}: storage_class must be one of {} (classes read without a restore)",
                t.storage_class,
                TIER_CLASSES.join(", ")
            ));
        }
        if !seen.insert(t.storage_class.clone()) {
            return Err(format!("tier {}: defined twice", t.storage_class));
        }
        if !t.secret_key_file.is_empty() {
            t.secret_key =
                std::fs::read_to_string(&t.secret_key_file).map_err(|e| format!("{}: {e}", t.secret_key_file))?.trim().to_string();
        }
        if t.bucket.is_empty() || t.access_key.is_empty() || t.secret_key.is_empty() {
            return Err(format!("tier {}: bucket, access_key and secret_key are required", t.storage_class));
        }
        if !t.endpoint.starts_with("http://") && !t.endpoint.starts_with("https://") {
            return Err(format!("tier {}: endpoint must be http:// or https://", t.storage_class));
        }
        if !t.prefix.chars().all(|c| c.is_ascii_alphanumeric() || "/_.-".contains(c)) {
            return Err(format!("tier {}: prefix may only hold letters, digits and / _ . -", t.storage_class));
        }
        t.endpoint = t.endpoint.trim_end_matches('/').to_string();
    }
    Ok(ts)
}

#[derive(Default)]
pub struct TierMetrics {
    pub transitioned: AtomicU64,
    pub transitioned_bytes: AtomicU64,
    pub failed: AtomicU64,
    pub reads: AtomicU64,
    pub deleted: AtomicU64,
}

/// The tier client. Its calls block (they run on blocking threads: lifecycle passes and object readers).
pub struct Tiers {
    tiers: BTreeMap<String, Tier>,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>,
    rt: tokio::runtime::Handle,
    pub metrics: TierMetrics,
}

impl Tiers {
    pub fn new(tiers: Vec<Tier>) -> Result<Tiers, String> {
        Ok(Tiers {
            tiers: tiers.into_iter().map(|t| (t.storage_class.clone(), t)).collect(),
            http: Client::builder(TokioExecutor::new()).build(crate::tls::https_connector()?),
            rt: tokio::runtime::Handle::current(),
            metrics: Default::default(),
        })
    }

    pub fn has(&self, class: &str) -> bool {
        self.tiers.contains_key(class)
    }

    fn tier(&self, class: &str) -> std::io::Result<&Tier> {
        self.tiers.get(class).ok_or_else(|| std::io::Error::other(format!("storage tier {class} is not configured")))
    }

    async fn send(
        &self,
        t: &Tier,
        method: &str,
        blob: &str,
        headers: &[(&str, String)],
        body: Body,
    ) -> std::io::Result<hyper::Response<hyper::body::Incoming>> {
        let url = format!("{}/{}/{}{blob}", t.endpoint, t.bucket, t.prefix);
        let mut req = hyper::Request::builder().method(method).uri(&url);
        for (k, v) in crate::admin_cli::sign_with_payload(
            method,
            &url,
            "UNSIGNED-PAYLOAD",
            &t.access_key,
            &t.secret_key,
            &t.region,
            time::OffsetDateTime::now_utc(),
        ) {
            req = req.header(k, v);
        }
        for (k, v) in headers {
            req = req.header(*k, v.as_str());
        }
        let req = req.body(body).map_err(std::io::Error::other)?;
        tokio::time::timeout(Duration::from_secs(60), self.http.request(req))
            .await
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, format!("{method} {url}: timed out")))?
            .map_err(|e| std::io::Error::other(format!("{method} {url}: {e}")))
    }

    async fn check(resp: hyper::Response<hyper::body::Incoming>, what: &str) -> std::io::Result<hyper::Response<hyper::body::Incoming>> {
        if resp.status().is_success() {
            return Ok(resp);
        }
        let status = resp.status();
        let body = resp.into_body().collect().await.map(|b| b.to_bytes()).unwrap_or_default();
        let msg = format!("{what}: HTTP {status}: {}", String::from_utf8_lossy(&body).chars().take(300).collect::<String>());
        Err(std::io::Error::new(
            if status == hyper::StatusCode::NOT_FOUND { std::io::ErrorKind::NotFound } else { std::io::ErrorKind::Other },
            msg,
        ))
    }

    /// Copies a blob file to a tier.
    pub fn upload(&self, class: &str, blob: &str, file: std::fs::File) -> std::io::Result<u64> {
        let t = self.tier(class)?;
        let len = file.metadata()?.len();
        self.rt.block_on(async {
            let resp = self.send(t, "PUT", blob, &[("content-length", len.to_string())], file_body(file)).await?;
            Self::check(resp, &format!("PUT {blob} to tier {class}")).await?;
            Ok(len)
        })
    }
}

impl TierStore for Tiers {
    fn open(&self, class: &str, blob: &str, offset: u64) -> std::io::Result<Box<dyn Read + Send>> {
        let t = self.tier(class)?;
        self.metrics.reads.fetch_add(1, Ordering::Relaxed);
        let resp = self.rt.block_on(async {
            let resp = self.send(t, "GET", blob, &[("range", format!("bytes={offset}-"))], empty()).await?;
            Self::check(resp, &format!("GET {blob} from tier {class}")).await
        })?;
        if offset > 0 && resp.status() != hyper::StatusCode::PARTIAL_CONTENT {
            return Err(std::io::Error::other(format!("tier {class} ignored a range request")));
        }
        Ok(Box::new(BodyReader { rt: self.rt.clone(), body: resp.into_body(), buf: Bytes::new() }))
    }

    fn delete(&self, class: &str, blob: &str) -> std::io::Result<()> {
        let t = self.tier(class)?;
        self.rt.block_on(async {
            let resp = self.send(t, "DELETE", blob, &[], empty()).await?;
            match Self::check(resp, &format!("DELETE {blob} from tier {class}")).await {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
                _ => Ok(()),
            }
        })?;
        self.metrics.deleted.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
}

fn empty() -> Body {
    Full::new(Bytes::new()).map_err(|never| match never {}).boxed()
}

/// A file's bytes as a request body (read on a blocking thread).
fn file_body(mut file: std::fs::File) -> Body {
    let (tx, rx) = tokio::sync::mpsc::channel::<std::io::Result<Frame<Bytes>>>(4);
    tokio::task::spawn_blocking(move || {
        let mut buf = vec![0u8; 256 << 10];
        loop {
            match file.read(&mut buf) {
                Ok(0) => return,
                Ok(k) => {
                    if tx.blocking_send(Ok(Frame::data(Bytes::copy_from_slice(&buf[..k])))).is_err() {
                        return;
                    }
                }
                Err(e) => {
                    let _ = tx.blocking_send(Err(e));
                    return;
                }
            }
        }
    });
    let s = futures_util::stream::unfold(rx, |mut rx| async move { rx.recv().await.map(|item| (item, rx)) });
    BodyExt::boxed(StreamBody::new(s))
}

/// Reads a response body from a blocking thread.
struct BodyReader {
    rt: tokio::runtime::Handle,
    body: hyper::body::Incoming,
    buf: Bytes,
}

impl Read for BodyReader {
    fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
        while self.buf.is_empty() {
            let frame = self.rt.block_on(async { tokio::time::timeout(Duration::from_secs(60), self.body.frame()).await });
            match frame {
                Err(_) => return Err(std::io::Error::new(std::io::ErrorKind::TimedOut, "tier read timed out")),
                Ok(None) => return Ok(0),
                Ok(Some(Err(e))) => return Err(std::io::Error::other(e)),
                Ok(Some(Ok(f))) => {
                    if let Ok(d) = f.into_data() {
                        self.buf = d;
                    }
                }
            }
        }
        let n = out.len().min(self.buf.len());
        out[..n].copy_from_slice(&self.buf[..n]);
        let _ = self.buf.split_to(n);
        Ok(n)
    }
}

impl Server {
    /// Refuses storage classes this store cannot honour on a write: objects are written as STANDARD and
    /// reach a tier only through a lifecycle transition. (REDUCED_REDUNDANCY asks for less durability than
    /// STANDARD gives, so it is stored as STANDARD.)
    pub(crate) fn check_storage_class(&self, c: &Ctx) -> Result<(), ApiError> {
        match c.header("x-amz-storage-class") {
            "" | "STANDARD" | "REDUCED_REDUNDANCY" => Ok(()),
            _ => Err(e::INVALID_STORAGE_CLASS.clone()),
        }
    }

    /// Moves a version's blobs to the tier `class` (blocking). Returns whether it moved.
    pub(crate) fn move_to_tier(&self, bucket: &str, o: &Object, class: &str) -> bool {
        let Some(tiers) = self.tiers.as_ref() else { return false };
        let mut new: Vec<Segment> = o.segments.clone();
        let mut uploaded: Vec<String> = vec![];
        let mut bytes = 0u64;
        let res = (|| -> std::io::Result<bool> {
            for s in new.iter_mut().filter(|s| s.tier.is_empty()) {
                bytes += tiers.upload(class, &s.blob, self.eng.blob_file(&s.blob)?)?;
                uploaded.push(s.blob.clone());
                s.tier = class.to_string();
            }
            self.eng.transition(bucket, &o.key, &o.version_id, &o.segments, new).map_err(|e| std::io::Error::other(e.to_string()))
        })();
        match res {
            Ok(true) => {
                tiers.metrics.transitioned.fetch_add(1, Ordering::Relaxed);
                tiers.metrics.transitioned_bytes.fetch_add(bytes, Ordering::Relaxed);
                true
            }
            other => {
                if let Err(err) = &other {
                    tiers.metrics.failed.fetch_add(1, Ordering::Relaxed);
                    tracing::warn!(bucket, key = %o.key, tier = class, error = %err, "transition failed; will retry");
                }
                // Changed meanwhile, or failed: the copies in the tier are not referenced.
                for b in uploaded {
                    let _ = tiers.delete(class, &b);
                }
                false
            }
        }
    }
}
