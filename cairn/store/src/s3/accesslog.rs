//! Server access logging (S3's PutBucketLogging): records of the requests to a bucket, in S3's access log
//! format, written as objects to a target bucket every `CAIRN_STORE_ACCESS_LOG_FLUSH_SECONDS` (default 60).
//! Like S3's, this is best effort: records buffered when the store stops abruptly are lost. (The audit
//! trail, not access logs, is the record of changes.) `CAIRN_STORE_LOG_REQUESTS=true` also writes one
//! structured JSON log line per request.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bytes::Bytes;
use hyper::StatusCode;
use serde::{Deserialize, Serialize};

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, xml_resp, Ctx, Resp, Server};
use crate::engine::{self, Object};

type R = Result<Resp, ApiError>;

const MAX_BUFFER: usize = 4 << 20;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct LogConfig {
    pub target_bucket: String,
    #[serde(default)]
    pub target_prefix: String,
}

/// What one request was, for its access log record.
#[derive(Clone, Debug, Default)]
pub struct LogInfo {
    pub bucket: String,
    pub key: String,
    pub method: String,
    pub uri: String,
    pub principal: String,
    pub auth_type: &'static str,
    pub sig_version: &'static str,
    pub referer: String,
    pub user_agent: String,
    pub host: String,
    pub version_id: String,
    pub object_size: Option<u64>,
}

#[derive(Default)]
pub struct AccessLogs {
    configs: Mutex<HashMap<String, Option<LogConfig>>>,
    buffers: Mutex<HashMap<String, (LogConfig, Vec<u8>)>>,
}

fn dash(s: &str) -> &str {
    if s.is_empty() {
        "-"
    } else {
        s
    }
}

/// S3's operation name: REST.<METHOD>.<resource>.
fn operation(info: &LogInfo, op: &str) -> String {
    let res = match op {
        "GetObject" | "HeadObject" | "PutObject" | "DeleteObject" | "CopyObject" | "PostObject" => "OBJECT",
        "UploadPart" | "UploadPartCopy" => "PART",
        "CreateMultipartUpload" | "CompleteMultipartUpload" | "AbortMultipartUpload" | "ListParts" => "UPLOAD",
        "ListObjects" | "ListObjectsV2" | "HeadBucket" | "CreateBucket" | "DeleteBucket" => "BUCKET",
        "ListObjectVersions" => "BUCKETVERSIONS",
        "DeleteObjects" => "MULTI_OBJECT_DELETE",
        o if o.contains("Tagging") => "TAGGING",
        o if o.contains("Acl") => "ACL",
        o if o.contains("Cors") => "CORS",
        o if o.contains("Policy") => "BUCKETPOLICY",
        o if o.contains("Lifecycle") => "LIFECYCLE",
        o if o.contains("Versioning") => "VERSIONING",
        o if o.contains("Encryption") => "ENCRYPTION",
        o if o.contains("Website") => "WEBSITE",
        o if o.contains("Notification") => "NOTIFICATION",
        o if o.contains("Logging") => "LOGGINGSTATUS",
        _ => "OBJECT",
    };
    format!("REST.{}.{res}", info.method)
}

impl Server {
    fn log_config(&self, bucket: &str) -> Option<LogConfig> {
        if let Some(c) = self.access_logs.configs.lock().unwrap().get(bucket) {
            return c.clone();
        }
        let cfg = blocking(|| self.eng.get_config(bucket, "logging")).ok().flatten().and_then(|d| serde_json::from_slice(&d).ok());
        self.access_logs.configs.lock().unwrap().insert(bucket.to_string(), cfg.clone());
        cfg
    }

    pub(crate) async fn put_logging<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "BucketLoggingStatus")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        let cfg = match root.child("LoggingEnabled") {
            None => None,
            Some(le) => {
                let cfg = LogConfig { target_bucket: le.text_of("TargetBucket"), target_prefix: le.text_of_raw("TargetPrefix") };
                if le.child("TargetGrants").is_some_and(|g| !g.children.is_empty()) {
                    return Err(e::ACL_NOT_SUPPORTED.clone());
                }
                if blocking(|| self.eng.head_bucket(&cfg.target_bucket)).is_err() {
                    return Err(e::LOGGING_TARGET.clone());
                }
                // Logs land in the target with the requester's rights: it must be allowed to write there.
                self.authorize(c, "s3:PutObject", &cfg.target_bucket, &format!("{}x", cfg.target_prefix)).await?;
                Some(cfg)
            }
        };
        let v = cfg.as_ref().map(|c| serde_json::to_vec(c).unwrap_or_default());
        self.set_config(c, "logging", v.as_deref())?;
        self.access_logs.configs.lock().unwrap().insert(c.bucket.clone(), cfg);
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_logging(&self, c: &Ctx) -> R {
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let mut w = Writer::new("BucketLoggingStatus");
        if let Some(cfg) = self.log_config(&c.bucket) {
            w.open("LoggingEnabled").el("TargetBucket", &cfg.target_bucket).el("TargetPrefix", &cfg.target_prefix).close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    /// Records a finished request (access log record and, when enabled, a JSON log line).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn log_request(
        &self,
        info: &LogInfo,
        op: &str,
        status: u16,
        error_code: &str,
        bytes_sent: u64,
        ms: u64,
        request_id: &str,
        peer: Option<std::net::IpAddr>,
    ) {
        let ip = peer.map(|p| p.to_string()).unwrap_or_default();
        if self.log_requests {
            tracing::info!(target: "cairn_store::access", op, status, bucket = %info.bucket, key = %info.key, principal = %info.principal,
                ip = %ip, bytes_sent, ms, request_id, error = error_code, "request");
        }
        if info.bucket.is_empty() || op == "Website" {
            return;
        }
        let Some(cfg) = self.log_config(&info.bucket) else { return };
        let now = (self.clock)();
        let time = now
            .format(time::macros::format_description!("[day]/[month repr:short]/[year]:[hour]:[minute]:[second] +0000"))
            .unwrap_or_default();
        let key = if info.key.is_empty() { "-".to_string() } else { super::auth::aws_encode(info.key.as_bytes(), true) };
        let line = format!(
            "{owner} {bucket} [{time}] {ip} {requester} {rid} {opn} {key} \"{uri}\" {status} {err} {sent} {size} {ms} - \"{referer}\" \"{ua}\" {vid} {rid} {sig} - {auth} {host} - - -\n",
            owner = "cairn-store",
            bucket = info.bucket,
            ip = dash(&ip),
            requester = dash(&info.principal),
            rid = request_id,
            opn = operation(info, op),
            uri = format!("{} {} HTTP/1.1", info.method, info.uri).replace('"', "%22"),
            err = dash(error_code),
            sent = if bytes_sent == 0 { "-".to_string() } else { bytes_sent.to_string() },
            size = info.object_size.map(|s| s.to_string()).unwrap_or_else(|| "-".into()),
            referer = dash(&info.referer).replace('"', "%22"),
            ua = dash(&info.user_agent).replace('"', "%22"),
            vid = dash(&info.version_id),
            sig = dash(info.sig_version),
            auth = dash(info.auth_type),
            host = dash(&info.host),
        );
        let mut bufs = self.access_logs.buffers.lock().unwrap();
        let entry = bufs.entry(info.bucket.clone()).or_insert_with(|| (cfg.clone(), Vec::new()));
        entry.0 = cfg;
        if entry.1.len() < MAX_BUFFER {
            entry.1.extend_from_slice(line.as_bytes());
        }
    }

    /// Writes buffered access log records to their target buckets.
    pub fn flush_access_logs(&self) {
        let pending: Vec<(String, (LogConfig, Vec<u8>))> =
            std::mem::take(&mut *self.access_logs.buffers.lock().unwrap()).into_iter().collect();
        for (source, (cfg, data)) in pending {
            if data.is_empty() {
                continue;
            }
            let now = (self.clock)();
            let stamp = now.format(time::macros::format_description!("[year]-[month]-[day]-[hour]-[minute]-[second]")).unwrap_or_default();
            let key = format!("{}{stamp}-{}", cfg.target_prefix, hex::encode_upper(rand::random::<[u8; 8]>()));
            let res = (|| -> Result<(), ApiError> {
                let sse = self.default_sse(&cfg.target_bucket, &key)?;
                let mut blob = self.eng.new_blob_with(sse.as_ref().map(|(_, k)| k))?;
                blob.write(&data).map_err(engine::Error::Io)?;
                let md5 = blob.md5();
                let seg = blob.seal()?;
                let obj = Object {
                    key: key.clone(),
                    size: seg.size,
                    etag: hex::encode(&md5),
                    modified: engine::now_ms(),
                    content_type: "text/plain".into(),
                    segments: vec![seg],
                    sse: sse.map(|(s, _)| s),
                    ..Default::default()
                };
                self.eng.put_object_ev(
                    &cfg.target_bucket,
                    obj,
                    &Default::default(),
                    &engine::Ev { kind: "Put", principal: "cairn-store".into(), ..Default::default() },
                )?;
                Ok(())
            })();
            if let Err(err) = res {
                tracing::warn!(source = %source, target = %cfg.target_bucket, error = %err, "access log delivery failed; records dropped");
            }
        }
    }

    pub async fn run_access_logs(self: Arc<Self>, every: Duration) {
        loop {
            tokio::time::sleep(every).await;
            let me = self.clone();
            let _ = tokio::task::spawn_blocking(move || me.flush_access_logs()).await;
        }
    }
}
