//! Bucket replication: new object versions (and, when a rule says so, delete markers) are copied to a
//! bucket on another S3 endpoint (another Cairn Store, or any S3).
//!
//! Destinations are defined by the operator (`CAIRN_STORE_REPLICATION_TARGETS_FILE`: `[{"id", "endpoint",
//! "access_key", "secret_key" | "secret_key_file", "region"}]`); a bucket's replication configuration names
//! one as its Role (`arn:cairn:store:::<id>`), so bucket owners can only send data where the operator
//! allowed. Copies go through the engine's outbox: they are recorded with the write, delivered at least
//! once and in order, and retried until the destination takes them. Encrypted objects are decrypted and
//! sent over the connection (use https endpoints); the destination applies its own encryption. Objects
//! encrypted with customer keys (SSE-C) cannot be read by the store and are marked FAILED.

use std::collections::BTreeMap;
use std::io::Read;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::Frame;
use hyper::StatusCode;
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use serde::Deserialize;

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, xml_resp, Ctx, Resp, Server};
use crate::engine::{self, Object, OutboxEntry, ReplConfig, ReplRule};

type R = Result<Resp, ApiError>;
type Body = BoxBody<Bytes, std::io::Error>;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplTarget {
    pub id: String,
    pub endpoint: String,
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

pub fn load_targets(text: &str) -> Result<Vec<ReplTarget>, String> {
    let mut ts: Vec<ReplTarget> = serde_json::from_str(text).map_err(|e| e.to_string())?;
    for t in ts.iter_mut() {
        if !t.secret_key_file.is_empty() {
            t.secret_key =
                std::fs::read_to_string(&t.secret_key_file).map_err(|e| format!("{}: {e}", t.secret_key_file))?.trim().to_string();
        }
        if t.id.is_empty() || t.access_key.is_empty() || t.secret_key.is_empty() {
            return Err(format!("replication target {:?}: id, access_key and secret_key are required", t.id));
        }
        if !t.endpoint.starts_with("http://") && !t.endpoint.starts_with("https://") {
            return Err(format!("replication target {}: endpoint must be http:// or https://", t.id));
        }
        t.endpoint = t.endpoint.trim_end_matches('/').to_string();
    }
    Ok(ts)
}

/// Replication client: destinations, an HTTP(S) client, multipart sizes.
pub struct Replicator {
    pub targets: BTreeMap<String, ReplTarget>,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>,
    multipart_above: u64,
    part_size: u64,
}

/// A failed copy: retry it, or give up (the destination refused it for good).
pub enum Failure {
    Retry(String),
    Permanent(String),
}

impl Replicator {
    pub fn new(targets: Vec<ReplTarget>) -> Result<Replicator, String> {
        let num = |k: &str, d: u64| std::env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d);
        Ok(Replicator {
            targets: targets.into_iter().map(|t| (t.id.clone(), t)).collect(),
            http: Client::builder(TokioExecutor::new()).build(crate::tls::https_connector()?),
            multipart_above: num("CAIRN_STORE_REPLICATION_MULTIPART_BYTES", 256 << 20),
            part_size: num("CAIRN_STORE_REPLICATION_PART_BYTES", 64 << 20).max(5 << 20),
        })
    }

    async fn send(
        &self,
        t: &ReplTarget,
        method: &str,
        path_q: &str,
        headers: &[(String, String)],
        body: Body,
        signed_payload: Option<&[u8]>,
    ) -> Result<(hyper::HeaderMap, Bytes), Failure> {
        let url = format!("{}{path_q}", t.endpoint);
        let payload =
            signed_payload.map(|b| hex::encode(<sha2::Sha256 as sha2::Digest>::digest(b))).unwrap_or_else(|| "UNSIGNED-PAYLOAD".into());
        let mut req = hyper::Request::builder().method(method).uri(&url);
        for (k, v) in crate::admin_cli::sign_with_payload(
            method,
            &url,
            &payload,
            &t.access_key,
            &t.secret_key,
            &t.region,
            time::OffsetDateTime::now_utc(),
        ) {
            req = req.header(k, v);
        }
        for (k, v) in headers {
            req = req.header(k.as_str(), v.as_str());
        }
        let req = req.body(body).map_err(|e| Failure::Permanent(e.to_string()))?;
        let resp = tokio::time::timeout(Duration::from_secs(600), self.http.request(req))
            .await
            .map_err(|_| Failure::Retry("timed out".into()))?
            .map_err(|e| Failure::Retry(e.to_string()))?;
        let status = resp.status();
        let resp_headers = resp.headers().clone();
        let bytes = resp.into_body().collect().await.map(|b| b.to_bytes()).unwrap_or_default();
        if status.is_success() {
            return Ok((resp_headers, bytes));
        }
        let msg = format!("{method} {url}: HTTP {status}: {}", String::from_utf8_lossy(&bytes).chars().take(300).collect::<String>());
        if status.is_client_error() && status != StatusCode::REQUEST_TIMEOUT && status != StatusCode::TOO_MANY_REQUESTS {
            Err(Failure::Permanent(msg))
        } else {
            Err(Failure::Retry(msg))
        }
    }
}

fn small(b: impl Into<Bytes>) -> Body {
    Full::new(b.into()).map_err(|never| match never {}).boxed()
}

/// Streams `[off, off+n)` of an object from a blocking reader.
fn stream(eng: std::sync::Arc<crate::engine::Engine>, obj: Object, key: Option<crate::crypt::Key>, off: u64, n: u64) -> Body {
    let (tx, rx) = tokio::sync::mpsc::channel::<std::io::Result<Frame<Bytes>>>(4);
    tokio::task::spawn_blocking(move || {
        let mut reader = match eng.reader(&obj, off, n, key.as_ref()) {
            Ok(r) => r,
            Err(e) => {
                let _ = tx.blocking_send(Err(std::io::Error::other(e.to_string())));
                return;
            }
        };
        let mut buf = vec![0u8; 256 << 10];
        loop {
            match reader.read(&mut buf) {
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

fn object_path(bucket: &str, key: &str) -> String {
    format!("/{}/{}", super::auth::aws_encode(bucket.as_bytes(), false), super::auth::aws_encode(key.as_bytes(), true))
}

impl Server {
    pub(crate) fn replication_config(&self, bucket: &str) -> Result<Option<ReplConfig>, ApiError> {
        match blocking(|| self.eng.get_config(bucket, "replication"))? {
            None => Ok(None),
            Some(d) => Ok(Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?)),
        }
    }

    pub(crate) async fn put_replication<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 256 << 10).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "ReplicationConfiguration")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        let role = root.text_of("Role");
        let target = role.rsplit(':').next().unwrap_or("").to_string();
        if !self.replicator.as_ref().is_some_and(|r| r.targets.contains_key(&target)) {
            return Err(e::REPL_TARGET_UNKNOWN.clone());
        }
        let info = blocking(|| self.eng.head_bucket(&c.bucket))?;
        if info.versioning != "Enabled" {
            return Err(e::REPL_NEEDS_VERSIONING.clone());
        }
        let mut rules = vec![];
        for r in root.all("Rule") {
            for ch in &r.children {
                match ch.name.as_str() {
                    "ID" | "Priority" | "Status" | "Filter" | "Prefix" | "Destination" | "DeleteMarkerReplication" => {}
                    "ExistingObjectReplication" if ch.text_of("Status") != "Enabled" => {}
                    "SourceSelectionCriteria" | "ExistingObjectReplication" => return Err(e::REPL_FEATURE.clone()),
                    _ => return Err(e::MALFORMED_XML.clone()),
                }
            }
            let dest = r.child("Destination").ok_or_else(|| e::MALFORMED_XML.clone())?;
            for ch in &dest.children {
                match ch.name.as_str() {
                    "Bucket" | "StorageClass" | "Account" => {}
                    _ => return Err(e::REPL_FEATURE.clone()), // ReplicationTime, Metrics, EncryptionConfiguration, ...
                }
            }
            let dest_bucket = dest.text_of("Bucket").trim_start_matches("arn:aws:s3:::").to_string();
            if dest_bucket.is_empty() {
                return Err(e::MALFORMED_XML.clone());
            }
            let mut rule = ReplRule {
                id: match r.text_of("ID") {
                    v if v.is_empty() => engine::new_id()[..12].to_string(),
                    v => v,
                },
                priority: r.text_of("Priority").parse().unwrap_or(0),
                enabled: match r.text_of("Status").as_str() {
                    "Enabled" => true,
                    "Disabled" => false,
                    _ => return Err(e::MALFORMED_XML.clone()),
                },
                dest_bucket,
                delete_markers: r.child("DeleteMarkerReplication").is_some_and(|d| d.text_of("Status") == "Enabled"),
                ..Default::default()
            };
            match (r.child("Filter"), r.child("Prefix")) {
                (Some(f), _) => {
                    let el = f.child("And").unwrap_or(f);
                    rule.prefix = el.text_of_raw("Prefix");
                    for t in el.all("Tag") {
                        rule.tags.insert(t.text_of_raw("Key"), t.text_of_raw("Value"));
                    }
                }
                (None, Some(p)) => {
                    rule.prefix = p.text.clone();
                    rule.legacy = true;
                }
                (None, None) => {}
            }
            rules.push(rule);
        }
        if rules.is_empty() || rules.len() > 1000 {
            return Err(e::MALFORMED_XML.clone());
        }
        let cfg = ReplConfig { target, role, rules };
        let v = serde_json::to_vec(&cfg).map_err(|_| e::INTERNAL.clone())?;
        self.set_config(c, "replication", Some(&v))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_replication(&self, c: &Ctx) -> R {
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let cfg = self.replication_config(&c.bucket)?.ok_or_else(|| e::NO_SUCH_REPLICATION.clone())?;
        let mut w = Writer::new("ReplicationConfiguration");
        w.el("Role", &cfg.role);
        for r in &cfg.rules {
            w.open("Rule")
                .el("ID", &r.id)
                .el("Priority", r.priority.to_string())
                .el("Status", if r.enabled { "Enabled" } else { "Disabled" });
            if r.legacy {
                w.el("Prefix", &r.prefix);
            } else {
                w.open("Filter");
                if r.tags.is_empty() {
                    w.el("Prefix", &r.prefix);
                } else {
                    w.open("And").el("Prefix", &r.prefix);
                    for (k, v) in &r.tags {
                        w.open("Tag").el("Key", k).el("Value", v).close();
                    }
                    w.close();
                }
                w.close();
                w.open("DeleteMarkerReplication").el("Status", if r.delete_markers { "Enabled" } else { "Disabled" }).close();
            }
            w.open("Destination").el("Bucket", format!("arn:aws:s3:::{}", r.dest_bucket)).close().close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    /// Copies one outbox entry's version (or delete marker) to its destination.
    pub(crate) async fn replicate(&self, target: &str, dest_bucket: &str, ent: &OutboxEntry) -> Result<(), Failure> {
        let rep = self.replicator.as_ref().ok_or_else(|| Failure::Permanent("replication is not configured".into()))?;
        let t =
            rep.targets.get(target).ok_or_else(|| Failure::Permanent(format!("replication target {target} is no longer configured")))?;
        let path = object_path(dest_bucket, &ent.key);
        if ent.event.ends_with("DeleteMarkerCreated") {
            rep.send(t, "DELETE", &path, &[], small(Bytes::new()), Some(b"")).await?;
            return Ok(());
        }
        let version = if ent.version.is_empty() { "null" } else { ent.version.as_str() };
        let obj = match blocking(|| self.eng.get_version(&ent.bucket, &ent.key, Some(version))) {
            Ok(o) if !o.delete_marker => o,
            _ => return Ok(()), // the version is gone: nothing to copy
        };
        let res = self.copy_out(rep, t, &ent.bucket, &path, &obj).await;
        let status = match &res {
            Ok(()) => "COMPLETED",
            Err(Failure::Permanent(_)) => "FAILED",
            Err(Failure::Retry(_)) => return res,
        };
        self.mark_replication(ent, status);
        res
    }

    /// Records a version's replication status (FAILED also when its copy is dropped past its maximum age).
    pub(crate) fn mark_replication(&self, ent: &OutboxEntry, status: &str) {
        if ent.event.ends_with("DeleteMarkerCreated") {
            return;
        }
        let version = if ent.version.is_empty() { "null" } else { ent.version.as_str() };
        let _ = blocking(|| {
            self.eng.update_object_ev(
                &ent.bucket,
                &ent.key,
                Some(version),
                |o| {
                    o.replication_status = status.into();
                    Ok(())
                },
                &engine::Ev::default(),
            )
        });
    }

    async fn copy_out(&self, rep: &Replicator, t: &ReplTarget, src_bucket: &str, path: &str, obj: &Object) -> Result<(), Failure> {
        if obj.sse.as_ref().is_some_and(|s| s.alg == "SSE-C") {
            return Err(Failure::Permanent("objects encrypted with customer keys (SSE-C) cannot be replicated".into()));
        }
        let key = self.unwrap_key(src_bucket, obj).map_err(|e| Failure::Retry(e.message.to_string()))?;
        let mut headers: Vec<(String, String)> = vec![("content-type".into(), obj.content_type.clone())];
        headers.extend(obj.headers.iter().map(|(k, v)| (k.clone(), v.clone())));
        headers.extend(obj.user_meta.iter().map(|(k, v)| (format!("x-amz-meta-{k}"), v.clone())));
        if !obj.tags.is_empty() {
            let q: Vec<String> = obj
                .tags
                .iter()
                .map(|(k, v)| format!("{}={}", super::auth::aws_encode(k.as_bytes(), false), super::auth::aws_encode(v.as_bytes(), false)))
                .collect();
            headers.push(("x-amz-tagging".into(), q.join("&")));
        }
        // Object lock: the copy keeps the retention still in force, and a legal hold.
        let retained = !obj.lock.mode.is_empty() && obj.lock.until > engine::now_ms();
        if retained {
            headers.push(("x-amz-object-lock-mode".into(), obj.lock.mode.clone()));
            headers.push(("x-amz-object-lock-retain-until-date".into(), super::iso_time(obj.lock.until)));
        }
        if obj.lock.legal_hold {
            headers.push(("x-amz-object-lock-legal-hold".into(), "ON".into()));
        }
        // A locked single PUT must carry its MD5 (S3's rule); objects without a plain MD5 ETag go multipart.
        let md5 = hex::decode(&obj.etag).ok().filter(|b| b.len() == 16);
        let locked = retained || obj.lock.legal_hold;
        if obj.size <= rep.multipart_above && (!locked || md5.is_some()) {
            let mut h = headers.clone();
            h.push(("content-length".into(), obj.size.to_string()));
            if let (true, Some(m)) = (locked, md5) {
                use base64::Engine as _;
                h.push(("content-md5".into(), base64::engine::general_purpose::STANDARD.encode(m)));
            }
            rep.send(t, "PUT", path, &h, stream(self.eng.clone(), obj.clone(), key, 0, obj.size), None).await?;
            return Ok(());
        }
        // Large objects: a multipart upload, part by part.
        let (_, body) = rep.send(t, "POST", &format!("{path}?uploads"), &headers, small(Bytes::new()), Some(b"")).await?;
        let doc = String::from_utf8_lossy(&body).into_owned();
        let upload = xml::parse(&doc)
            .map(|r| r.text_of("UploadId"))
            .filter(|u| !u.is_empty())
            .ok_or_else(|| Failure::Retry("no UploadId in the reply".into()))?;
        let mut etags: Vec<(u32, String)> = vec![];
        let mut off = 0u64;
        let mut n = 1u32;
        while off < obj.size {
            let len = rep.part_size.min(obj.size - off);
            let q = format!("{path}?partNumber={n}&uploadId={}", super::auth::aws_encode(upload.as_bytes(), false));
            let res = rep
                .send(
                    t,
                    "PUT",
                    &q,
                    &[("content-length".into(), len.to_string())],
                    stream(self.eng.clone(), obj.clone(), key, off, len),
                    None,
                )
                .await;
            match res {
                Ok((h, _)) => etags.push((n, h.get("etag").and_then(|v| v.to_str().ok()).unwrap_or("").to_string())),
                Err(err) => {
                    let _ = rep
                        .send(
                            t,
                            "DELETE",
                            &format!("{path}?uploadId={}", super::auth::aws_encode(upload.as_bytes(), false)),
                            &[],
                            small(Bytes::new()),
                            Some(b""),
                        )
                        .await;
                    return Err(err);
                }
            }
            off += len;
            n += 1;
        }
        let mut w = Writer::new("CompleteMultipartUpload");
        for (p, etag) in &etags {
            w.open("Part").el("PartNumber", p.to_string()).el("ETag", etag).close();
        }
        let doc = w.finish().into_bytes();
        rep.send(
            t,
            "POST",
            &format!("{path}?uploadId={}", super::auth::aws_encode(upload.as_bytes(), false)),
            &[("content-type".into(), "application/xml".into())],
            small(doc.clone()),
            Some(&doc),
        )
        .await?;
        Ok(())
    }
}
