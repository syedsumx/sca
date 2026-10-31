//! Cairn's admin API for the store, under `/-/admin/v1/`: bucket usage and quotas, store statistics.
//! Requests are signed with SigV4 by an admin key (any AWS SDK signer works, service "s3"); bodies and
//! responses are JSON.
//!
//! ```text
//! GET    /-/admin/v1/stats                     buckets, objects, versions, bytes, uploads
//! GET    /-/admin/v1/buckets                   every bucket with its usage, quota and settings
//! GET    /-/admin/v1/buckets/{bucket}          one bucket
//! PUT    /-/admin/v1/buckets/{bucket}/quota    {"max_bytes": n, "max_objects": n} (either or both)
//! DELETE /-/admin/v1/buckets/{bucket}/quota
//! ```

use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::{Method, Request, StatusCode};
use serde_json::{json, Value};

use super::auth;
use super::errors::{self as e, ApiError};
use super::{full, set_header, Resp, Server};
use crate::engine::Quota;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

fn json_resp(status: StatusCode, v: Value) -> Resp {
    let mut r = full(status, v.to_string());
    set_header(&mut r, "content-type", "application/json");
    r
}

fn json_error(err: &ApiError) -> Resp {
    json_resp(
        StatusCode::from_u16(err.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        json!({"error": err.code, "message": err.message}),
    )
}

impl Server {
    fn bucket_json(&self, name: &str) -> Result<Value, ApiError> {
        let info = self.eng.head_bucket(name)?;
        let (bytes, objects) = self.eng.usage(name)?;
        let quota: Option<Quota> = match self.eng.get_config(name, "quota")? {
            Some(d) => Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?),
            None => None,
        };
        let has = |kind: &str| self.eng.get_config(name, kind).map(|d| d.is_some());
        Ok(json!({
            "name": info.name,
            "created": super::iso_time(info.created),
            "versioning": if info.versioning.is_empty() { "Off" } else { info.versioning.as_str() },
            "usage": {"bytes": bytes, "objects": objects},
            "quota": quota,
            "object_lock": has("object-lock")?,
            "encryption_default": has("encryption")?,
            "lifecycle": has("lifecycle")?,
            "policy": has("policy")?,
        }))
    }

    /// Handles a request to the admin API.
    pub(crate) async fn admin_api<B>(&self, req: Request<B>) -> Resp
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        match self.admin_inner(req).await {
            Ok(r) => r,
            Err(err) => json_error(&err),
        }
    }

    async fn admin_inner<B>(&self, req: Request<B>) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let (parts, body) = req.into_parts();
        let raw_path = parts.uri.path().to_string();
        let raw_query = parts.uri.query().unwrap_or("").to_string();
        let host = parts.headers.get("host").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let request = auth::Request {
            method: parts.method.as_str(),
            raw_path: &raw_path,
            raw_query: &raw_query,
            host: &host,
            headers: &parts.headers,
            now: (self.clock)(),
            vhost_bucket: None,
        };
        if auth::is_anonymous(&parts.headers, &raw_query) {
            return Err(e::ACCESS_DENIED.clone());
        }
        let a = auth::verify(&self.creds, &request)?;
        if !a.cred.admin {
            return Err(e::ACCESS_DENIED.clone());
        }
        let body = body.collect().await.map_err(|_| e::INCOMPLETE_BODY.clone())?.to_bytes();
        if body.len() > 64 << 10 {
            return Err(e::ENTITY_TOO_LARGE.clone());
        }
        if a.payload_hash != auth::UNSIGNED_PAYLOAD && a.payload_hash != auth::sha256_hex(&body) {
            return Err(e::CONTENT_SHA_MISMATCH.clone());
        }
        let path = raw_path.trim_start_matches("/-/admin/v1").trim_matches('/');
        let segs: Vec<String> =
            path.split('/').filter(|s| !s.is_empty()).map(|s| String::from_utf8_lossy(&auth::percent_decode(s)).into_owned()).collect();
        let segs: Vec<&str> = segs.iter().map(String::as_str).collect();
        let m = &parts.method;
        blocking(|| match (m, segs.as_slice()) {
            (&Method::GET, ["stats"]) => {
                let st = self.eng.stats()?;
                Ok(json_resp(StatusCode::OK, serde_json::to_value(st).map_err(|_| e::INTERNAL.clone())?))
            }
            (&Method::GET, ["audit"]) => {
                let q: std::collections::BTreeMap<String, String> = raw_query
                    .split('&')
                    .filter_map(|p| p.split_once('='))
                    .map(|(k, v)| (auth::query_decode(k), auth::query_decode(v)))
                    .collect();
                let after = q.get("after").and_then(|v| v.parse().ok()).unwrap_or(0);
                let limit = q.get("limit").and_then(|v| v.parse().ok()).unwrap_or(1000usize).min(10_000);
                let entries = self.eng.audit_entries(after, limit)?;
                if q.get("format").map(String::as_str) == Some("jsonl") {
                    let mut body = String::new();
                    for en in &entries {
                        body.push_str(&serde_json::to_string(en).map_err(|_| e::INTERNAL.clone())?);
                        body.push('\n');
                    }
                    let mut r = full(StatusCode::OK, body);
                    set_header(&mut r, "content-type", "application/x-ndjson");
                    return Ok(r);
                }
                Ok(json_resp(StatusCode::OK, json!({"entries": entries, "head": entries.last().map(|en| en.hash.clone())})))
            }
            (&Method::GET, ["buckets"]) => {
                let mut out = vec![];
                for b in self.eng.list_buckets()? {
                    out.push(self.bucket_json(&b.name)?);
                }
                Ok(json_resp(StatusCode::OK, json!({"buckets": out})))
            }
            (&Method::GET, ["buckets", b]) => Ok(json_resp(StatusCode::OK, self.bucket_json(b)?)),
            (&Method::PUT, ["buckets", b, "quota"]) => {
                let q: Quota = serde_json::from_slice(&body).map_err(|_| e::MALFORMED_JSON.clone())?;
                if q.max_bytes.is_none() && q.max_objects.is_none() {
                    return Err(e::MALFORMED_JSON.clone());
                }
                let v = serde_json::to_vec(&q).map_err(|_| e::INTERNAL.clone())?;
                let audit = crate::audit::Audit {
                    category: "store",
                    action: "quota.put".into(),
                    actor: a.cred.access_key.clone(),
                    subject: b.to_string(),
                    details: json!({"max_bytes": q.max_bytes, "max_objects": q.max_objects}),
                };
                self.eng.put_config_audited(b, "quota", Some(&v), Some(&audit))?;
                tracing::info!(bucket = %b, admin = %a.cred.access_key, max_bytes = ?q.max_bytes, max_objects = ?q.max_objects, "bucket quota set");
                Ok(json_resp(StatusCode::OK, self.bucket_json(b)?))
            }
            (&Method::DELETE, ["buckets", b, "quota"]) => {
                let audit = crate::audit::Audit {
                    category: "store",
                    action: "quota.delete".into(),
                    actor: a.cred.access_key.clone(),
                    subject: b.to_string(),
                    details: json!({}),
                };
                self.eng.put_config_audited(b, "quota", None, Some(&audit))?;
                tracing::info!(bucket = %b, admin = %a.cred.access_key, "bucket quota removed");
                Ok(json_resp(StatusCode::OK, self.bucket_json(b)?))
            }
            _ => Err(e::ADMIN_NOT_FOUND.clone()),
        })
    }
}
