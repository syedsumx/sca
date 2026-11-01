//! Storage tiers: lifecycle transitions move object data to a bucket elsewhere (S3, another Cairn Store,
//! Azure Blob or Google Cloud Storage), and reads fetch it from there.
//!
//! Tiers are defined by the operator (`CAIRN_STORE_TIERS_FILE`: a list of remote buckets as in
//! `remote::RemoteSpec`, each with a `storage_class` instead of an `id`); a lifecycle rule names one by its
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
use http_body_util::{BodyExt, StreamBody};
use hyper::body::Frame;

use super::errors::{self as e, ApiError};
use super::remote::{check_spec, Remote, RemoteSpec};
use super::{Ctx, Server};
use crate::engine::{Object, Segment, TierStore};

type Body = BoxBody<Bytes, std::io::Error>;

/// The storage classes a tier may have: those S3 reads without a restore.
pub const TIER_CLASSES: [&str; 3] = ["STANDARD_IA", "ONEZONE_IA", "GLACIER_IR"];

/// A tier: a remote bucket (S3, Azure Blob or GCS; see `remote::RemoteSpec`) named by its storage class.
pub type Tier = RemoteSpec;

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
        if !t.id.is_empty() {
            return Err(format!("tier {}: tiers are named by storage_class, not id", t.storage_class));
        }
        if !t.prefix.chars().all(|c| c.is_ascii_alphanumeric() || "/_.-".contains(c)) {
            return Err(format!("tier {}: prefix may only hold letters, digits and / _ . -", t.storage_class));
        }
        t.id = t.storage_class.clone();
        // The prefix is used as given (a tier's objects are <prefix><blob>).
        check_spec(t, "tier", false)?;
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
    tiers: BTreeMap<String, Remote>,
    pub metrics: TierMetrics,
}

impl Tiers {
    pub fn new(tiers: Vec<Tier>) -> Result<Tiers, String> {
        let mut m = BTreeMap::new();
        for t in tiers {
            m.insert(t.storage_class.clone(), Remote::new(t)?);
        }
        Ok(Tiers { tiers: m, metrics: Default::default() })
    }

    pub fn has(&self, class: &str) -> bool {
        self.tiers.contains_key(class)
    }

    fn tier(&self, class: &str) -> std::io::Result<&Remote> {
        self.tiers.get(class).ok_or_else(|| std::io::Error::other(format!("storage tier {class} is not configured")))
    }

    /// Copies a blob file to a tier.
    pub fn upload(&self, class: &str, blob: &str, file: Box<dyn Read + Send>, len: u64) -> std::io::Result<u64> {
        self.tier(class)?.put(&tier_name(blob), file, len, &[])?;
        Ok(len)
    }
}

impl TierStore for Tiers {
    fn open(&self, class: &str, blob: &str, offset: u64) -> std::io::Result<Box<dyn Read + Send>> {
        let t = self.tier(class)?;
        self.metrics.reads.fetch_add(1, Ordering::Relaxed);
        t.get(&tier_name(blob), offset)
    }

    fn delete(&self, class: &str, blob: &str) -> std::io::Result<()> {
        self.tier(class)?.delete(&tier_name(blob))?;
        self.metrics.deleted.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
}

/// A blob's object name in a tier (cluster ids name nodes and local ids: hashed to a plain name).
fn tier_name(blob: &str) -> std::borrow::Cow<'_, str> {
    if crate::engine::cid::is_cluster(blob) {
        use sha2::Digest;
        return format!("c{}", hex::encode(&sha2::Sha256::digest(blob.as_bytes())[..20])).into();
    }
    blob.into()
}

/// A file's bytes as a request body (read on a blocking thread).
pub(crate) fn file_body(mut file: impl Read + Send + 'static) -> Body {
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
pub(crate) struct BodyReader {
    rt: tokio::runtime::Handle,
    body: hyper::body::Incoming,
    buf: Bytes,
}

impl BodyReader {
    pub(crate) fn new(rt: tokio::runtime::Handle, body: hyper::body::Incoming) -> BodyReader {
        BodyReader { rt, body, buf: Bytes::new() }
    }
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
                let (reader, len) = self.eng.blob_reader(&s.blob).map_err(|e| std::io::Error::other(e.to_string()))?;
                bytes += tiers.upload(class, &s.blob, reader, len)?;
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
