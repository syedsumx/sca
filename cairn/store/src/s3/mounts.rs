//! Remote buckets mounted into local buckets: a local bucket mirrors a bucket on another S3 endpoint
//! (`CAIRN_STORE_REMOTES_FILE`: `[{"id", "endpoint", "bucket", "prefix", "access_key", "secret_key", "region"}]`).
//!
//! * A sync pass lists the remote and makes each remote object a local object whose data stays remote
//!   (a segment in the tier `@<remote>`): listings, HEAD and metadata are local; reads fetch the bytes
//!   from the remote, ranges included. Objects changed or deleted on the remote follow at the next pass
//!   (the newer write wins over a local change, as in two-way sync).
//! * `cache` copies mirrored objects' bytes to this store (encrypted like any new object), so reads stay
//!   local; `uncache` drops the local copy of objects the remote still has unchanged.
//! * With write-back, objects written or deleted here are written to or deleted on the remote, through the
//!   outbox (at least once, in order, retried while the remote is down).
//!
//! Only the remote's current objects are mirrored (not its old versions).

use std::collections::{BTreeMap, HashMap};
use std::io::Read;
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::errors::{self as e, ApiError};
use super::remote::Remote;
use super::Server;
use crate::engine::{self, Object, OutboxEntry, Segment, TierStore};

/// A bucket's mount (bucket configuration kind "remote").
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Mount {
    pub remote: String,
    #[serde(default)]
    pub write_back: bool,
}

#[derive(Debug, Default, Serialize)]
pub struct SyncReport {
    pub remote_objects: u64,
    pub added: u64,
    pub updated: u64,
    pub removed: u64,
    /// Remote changes that gave way to newer local ones.
    pub skipped: u64,
    pub failed: u64,
}

/// Reads tiered segments: `@<remote>` from mounted remotes, other tiers from the storage tiers.
pub struct TierRouter {
    pub tiers: Option<Arc<super::tiering::Tiers>>,
    pub remotes: Arc<BTreeMap<String, Remote>>,
}

impl TierStore for TierRouter {
    fn open(&self, tier: &str, blob: &str, offset: u64) -> std::io::Result<Box<dyn Read + Send>> {
        if let Some(id) = tier.strip_prefix('@') {
            let r = self.remotes.get(id).ok_or_else(|| std::io::Error::other(format!("remote {id} is no longer configured")))?;
            return r.get(blob, offset);
        }
        self.tiers.as_ref().ok_or_else(|| std::io::Error::other(format!("storage tier {tier} is not configured")))?.open(tier, blob, offset)
    }

    fn delete(&self, tier: &str, blob: &str) -> std::io::Result<()> {
        if tier.starts_with('@') {
            return Ok(()); // the remote's own object: deleted there only through write-back
        }
        self.tiers.as_ref().map_or(Ok(()), |t| t.delete(tier, blob))
    }
}

fn mirrored(o: &Object, id: &str) -> bool {
    let tier = format!("@{id}");
    !o.segments.is_empty() && o.segments.iter().all(|s| s.tier == tier)
}

fn sink_err(e: std::io::Error) -> ApiError {
    tracing::warn!(error = %e, "remote bucket");
    e::REMOTE_FAILED.clone()
}

impl Server {
    pub(crate) fn mount_of(&self, bucket: &str) -> Result<Option<Mount>, ApiError> {
        match tokio::task::block_in_place(|| self.eng.get_config(bucket, "remote"))? {
            None => Ok(None),
            Some(d) => Ok(Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?)),
        }
    }

    fn remote(&self, id: &str) -> Result<&Remote, ApiError> {
        self.remotes.get(id).ok_or_else(|| e::NO_SUCH_REMOTE.clone())
    }

    /// Mounts the remote `m.remote` into `bucket` (blocking).
    pub(crate) fn mount(&self, bucket: &str, m: &Mount, actor: &str) -> Result<Value, ApiError> {
        self.remote(&m.remote)?;
        self.eng.head_bucket(bucket)?;
        for b in self.eng.list_buckets()? {
            if b.name != bucket && self.mount_of(&b.name)?.is_some_and(|o| o.remote == m.remote) {
                return Err(e::REMOTE_IN_USE.clone());
            }
        }
        let audit = crate::audit::Audit {
            category: "store",
            action: "remote.mount".into(),
            actor: actor.into(),
            subject: bucket.into(),
            details: json!({"remote": m.remote, "write_back": m.write_back}),
        };
        let doc = serde_json::to_vec(m).unwrap();
        self.eng.put_config_audited(bucket, "remote", Some(&doc), Some(&audit))?;
        tracing::info!(bucket, remote = %m.remote, write_back = m.write_back, "remote bucket mounted");
        Ok(json!({"bucket": bucket, "remote": m.remote, "write_back": m.write_back}))
    }

    pub(crate) fn unmount(&self, bucket: &str, actor: &str) -> Result<(), ApiError> {
        let m = self.mount_of(bucket)?.ok_or_else(|| e::NOT_MOUNTED.clone())?;
        let audit = crate::audit::Audit {
            category: "store",
            action: "remote.unmount".into(),
            actor: actor.into(),
            subject: bucket.into(),
            details: json!({"remote": m.remote}),
        };
        self.eng.put_config_audited(bucket, "remote", None, Some(&audit))?;
        Ok(())
    }

    /// A mount's state: its settings and how many objects are mirrored or cached.
    pub(crate) fn mount_status(&self, bucket: &str) -> Result<Value, ApiError> {
        let m = self.mount_of(bucket)?.ok_or_else(|| e::NOT_MOUNTED.clone())?;
        let objs = self.eng.current_objects(bucket)?;
        let remote_only = objs.iter().filter(|o| mirrored(o, &m.remote)).count();
        let from_remote = objs.iter().filter(|o| !o.remote_etag.is_empty()).count();
        Ok(json!({
            "bucket": bucket, "remote": m.remote, "write_back": m.write_back, "objects": objs.len(),
            "remote_only": remote_only, "cached": from_remote.saturating_sub(remote_only),
            "local_only": objs.len() - from_remote,
        }))
    }

    /// One sync pass: the remote's current objects become (or update, or remove) local mirrors.
    pub(crate) fn remote_sync(&self, bucket: &str) -> Result<SyncReport, ApiError> {
        let m = self.mount_of(bucket)?.ok_or_else(|| e::NOT_MOUNTED.clone())?;
        let r = self.remote(&m.remote)?;
        let (listed, _) = r.list("", "").map_err(sink_err)?;
        let local: HashMap<String, Object> = self.eng.current_objects(bucket)?.into_iter().map(|o| (o.key.clone(), o)).collect();
        let mut rep = SyncReport { remote_objects: listed.len() as u64, ..Default::default() };
        let tier = format!("@{}", m.remote);
        let mut seen = std::collections::HashSet::new();
        for l in &listed {
            seen.insert(l.key.clone());
            let update = match local.get(&l.key) {
                None => false,
                Some(o) if o.remote_etag == l.etag => continue,
                Some(o) if !o.remote_etag.is_empty() => true, // changed on the remote
                Some(_) => continue,                          // written here (and written back, or shadowing)
            };
            if l.key.is_empty() || l.key.len() > 1024 {
                continue;
            }
            let head = match r.head_info(&l.key) {
                Ok(Some(h)) => h,
                Ok(None) => continue, // deleted meanwhile
                Err(err) => {
                    tracing::warn!(bucket, key = %l.key, error = %err, "remote object");
                    rep.failed += 1;
                    continue;
                }
            };
            let obj = Object {
                key: l.key.clone(),
                size: head.len,
                etag: head.etag.clone(),
                modified: l.modified.max(1),
                content_type: head.content_type,
                user_meta: head.meta,
                segments: vec![Segment { blob: l.key.clone(), size: head.len, nonce: String::new(), tier: tier.clone() }],
                remote_etag: head.etag,
                ..Default::default()
            };
            let ev =
                engine::Ev { kind: "Put", principal: format!("remote:{}", m.remote), replica: Some(obj.modified), ..Default::default() };
            match self.eng.put_object_ev(bucket, obj, &engine::Condition::default(), &ev) {
                Ok(_) if update => rep.updated += 1,
                Ok(_) => rep.added += 1,
                Err(engine::Error::Stale) => rep.skipped += 1,
                Err(err) => {
                    tracing::warn!(bucket, key = %l.key, error = %err, "mirroring a remote object");
                    rep.failed += 1;
                }
            }
        }
        // Mirrors whose remote object is gone (objects written here and not yet written back are kept).
        for (k, o) in &local {
            if o.remote_etag.is_empty() || seen.contains(k) {
                continue;
            }
            let ev = engine::Ev { principal: format!("remote:{}", m.remote), replica: Some(engine::now_ms()), ..Default::default() };
            match self.eng.delete_object_ev(bucket, k, Some(&o.etag), &ev) {
                Ok(_) => rep.removed += 1,
                Err(engine::Error::Stale) | Err(engine::Error::PreconditionFailed) => rep.skipped += 1,
                Err(err) => {
                    tracing::warn!(bucket, key = %k, error = %err, "removing a mirror");
                    rep.failed += 1;
                }
            }
        }
        if rep.added + rep.updated + rep.removed > 0 {
            tracing::info!(bucket, remote = %m.remote, added = rep.added, updated = rep.updated, removed = rep.removed, "remote sync");
        }
        Ok(rep)
    }

    /// Copies mirrored objects' bytes (under `prefix`) to this store.
    pub(crate) fn remote_cache(&self, bucket: &str, prefix: &str) -> Result<Value, ApiError> {
        let m = self.mount_of(bucket)?.ok_or_else(|| e::NOT_MOUNTED.clone())?;
        let r = self.remote(&m.remote)?;
        let (mut cached, mut bytes, mut failed) = (0u64, 0u64, 0u64);
        for o in self.eng.current_objects(bucket)? {
            if !o.key.starts_with(prefix) || !mirrored(&o, &m.remote) {
                continue;
            }
            let res = (|| -> Result<bool, ApiError> {
                let (sse, dek) = self.default_sse(bucket, &o.key)?.unzip();
                let mut w = self.eng.new_blob_with(dek.as_ref())?;
                let mut src = r.get(&o.key, 0).map_err(sink_err)?;
                let mut buf = vec![0u8; 256 << 10];
                loop {
                    let n = src.read(&mut buf).map_err(sink_err)?;
                    if n == 0 {
                        break;
                    }
                    w.write(&buf[..n]).map_err(engine::Error::Io)?;
                }
                if w.size() != o.size {
                    return Err(e::REMOTE_FAILED.clone()); // changed on the remote meanwhile: the next sync updates it
                }
                let seg = w.seal()?;
                let ok = self.eng.transition_with(bucket, &o.key, &o.version_id, &o.segments, vec![seg.clone()], Some(sse))?;
                if !ok {
                    self.eng.discard(&[seg]);
                }
                Ok(ok)
            })();
            match res {
                Ok(true) => {
                    cached += 1;
                    bytes += o.size;
                }
                Ok(false) => {}
                Err(_) => failed += 1,
            }
        }
        Ok(json!({"bucket": bucket, "cached": cached, "bytes": bytes, "failed": failed}))
    }

    /// Drops the local copy of cached objects (under `prefix`) that the remote still has unchanged.
    pub(crate) fn remote_uncache(&self, bucket: &str, prefix: &str) -> Result<Value, ApiError> {
        let m = self.mount_of(bucket)?.ok_or_else(|| e::NOT_MOUNTED.clone())?;
        let r = self.remote(&m.remote)?;
        let tier = format!("@{}", m.remote);
        let mut dropped = 0u64;
        for o in self.eng.current_objects(bucket)? {
            if !o.key.starts_with(prefix) || o.remote_etag.is_empty() || mirrored(&o, &m.remote) {
                continue;
            }
            if r.head_info(&o.key).map_err(sink_err)?.is_none_or(|h| h.etag != o.remote_etag || h.len != o.size) {
                continue; // the remote no longer has these bytes: keep ours
            }
            let seg = Segment { blob: o.key.clone(), size: o.size, nonce: String::new(), tier: tier.clone() };
            if self.eng.transition_with(bucket, &o.key, &o.version_id, &o.segments, vec![seg], Some(None))? {
                dropped += 1;
            }
        }
        Ok(json!({"bucket": bucket, "uncached": dropped}))
    }

    /// Writes one outbox entry of a write-back mount to its remote.
    pub(crate) async fn write_back(&self, id: &str, ent: &OutboxEntry) -> Result<(), super::replication::Failure> {
        use super::replication::Failure;
        let r = self.remotes.get(id).ok_or_else(|| Failure::Permanent(format!("remote {id} is no longer configured")))?;
        if ent.event.contains("ObjectRemoved") {
            return tokio::task::block_in_place(|| r.delete(&ent.key)).map_err(|e| Failure::Retry(e.to_string()));
        }
        let version = if ent.version.is_empty() { "null" } else { ent.version.as_str() };
        let obj = match tokio::task::block_in_place(|| self.eng.get_version(&ent.bucket, &ent.key, Some(version))) {
            Ok(o) if !o.delete_marker => o,
            _ => return Ok(()), // gone or replaced: a later entry carries what to do
        };
        if mirrored(&obj, id) || (!obj.remote_etag.is_empty() && obj.etag == obj.remote_etag) {
            return Ok(()); // the remote has it
        }
        if obj.sse.as_ref().is_some_and(|s| s.alg == "SSE-C") {
            return Err(Failure::Permanent("objects encrypted with customer keys (SSE-C) cannot be written back".into()));
        }
        let key = self.unwrap_key(&ent.bucket, &obj).map_err(|e| Failure::Retry(e.message.to_string()))?;
        let mut headers: Vec<(&str, String)> = vec![("content-type", obj.content_type.clone())];
        let meta: Vec<(String, String)> = obj.user_meta.iter().map(|(k, v)| (format!("x-amz-meta-{k}"), v.clone())).collect();
        headers.extend(meta.iter().map(|(k, v)| (k.as_str(), v.clone())));
        let etag = tokio::task::block_in_place(|| -> Result<String, Failure> {
            let reader = self.eng.reader(&obj, 0, obj.size, key.as_ref()).map_err(|e| Failure::Retry(e.to_string()))?;
            r.put(&obj.key, Box::new(reader), obj.size, &headers).map_err(|e| Failure::Retry(e.to_string()))
        })?;
        let local_etag = obj.etag.clone();
        let _ = tokio::task::block_in_place(|| {
            self.eng.update_object_ev(
                &ent.bucket,
                &ent.key,
                Some(version),
                |o| {
                    if o.etag == local_etag {
                        o.remote_etag = etag.clone();
                    }
                    Ok(())
                },
                &engine::Ev::default(),
            )
        });
        Ok(())
    }

    /// Syncs every mounted bucket every `every` (one node at a time in a cluster).
    pub async fn run_remote_sync(self: Arc<Self>, every: Duration) {
        let mut tick = tokio::time::interval(every);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            if self.remotes.is_empty() || !self.may_run("remote-sync") {
                continue;
            }
            let me = self.clone();
            let _ = tokio::task::spawn_blocking(move || {
                for b in me.eng.list_buckets().unwrap_or_default() {
                    if let Ok(Some(_)) = me.eng.get_config(&b.name, "remote") {
                        if let Err(err) = me.remote_sync(&b.name) {
                            tracing::warn!(bucket = %b.name, error = %err.message, "remote sync failed; will retry");
                        }
                    }
                }
            })
            .await;
        }
    }

    /// The admin API's remote-mount calls under buckets/{bucket}/remote (blocking).
    pub(crate) fn remote_admin(
        &self,
        m: &hyper::Method,
        bucket: &str,
        rest: &[&str],
        body: &[u8],
        actor: &str,
    ) -> Result<(hyper::StatusCode, Value), ApiError> {
        use hyper::{Method, StatusCode};
        let prefix = || -> String {
            serde_json::from_slice::<Value>(body)
                .ok()
                .and_then(|v| v.get("prefix").and_then(Value::as_str).map(str::to_string))
                .unwrap_or_default()
        };
        match (m, rest) {
            (&Method::GET, []) => Ok((StatusCode::OK, self.mount_status(bucket)?)),
            (&Method::PUT, []) => {
                let mount: Mount = serde_json::from_slice(body).map_err(|_| e::MALFORMED_JSON.clone())?;
                Ok((StatusCode::OK, self.mount(bucket, &mount, actor)?))
            }
            (&Method::DELETE, []) => {
                self.unmount(bucket, actor)?;
                Ok((StatusCode::OK, json!({"bucket": bucket, "unmounted": true})))
            }
            (&Method::POST, ["sync"]) => Ok((StatusCode::OK, serde_json::to_value(self.remote_sync(bucket)?).unwrap())),
            (&Method::POST, ["cache"]) => Ok((StatusCode::OK, self.remote_cache(bucket, &prefix())?)),
            (&Method::POST, ["uncache"]) => Ok((StatusCode::OK, self.remote_uncache(bucket, &prefix())?)),
            _ => Err(e::ADMIN_NOT_FOUND.clone()),
        }
    }
}
