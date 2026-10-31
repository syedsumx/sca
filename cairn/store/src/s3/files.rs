//! The plain HTTP file API and directory browser: `/-/files/<bucket>/<path>`.
//!
//! - `GET`/`HEAD` a file reads it (ranges and conditional requests as for GetObject); `PUT` writes one;
//!   `DELETE` removes one. `versionId` selects a version.
//! - `GET` a directory (a path ending in `/`, or a bucket, or `/-/files/` for the bucket list) lists it:
//!   JSON by default, an HTML page for browsers (`Accept: text/html`). `limit` and `after` page through it.
//!
//! Every request is the S3 operation it stands for (GetObject, PutObject, DeleteObject, ListObjectsV2
//! with a `/` delimiter, ListBuckets) and is authorized, audited, encrypted, metered and logged exactly
//! like that operation: bucket and key policies, Cairn's policy decision point and quotas all apply.
//! Requests are signed with SigV4 (`curl --aws-sigv4 "aws:amz:<region>:s3"`), presigned, or anonymous
//! (allowed only by a public bucket policy). HTTP Basic authentication with an access key and secret key,
//! for browsers, is off unless `CAIRN_STORE_FILES_BASIC_AUTH=true`, and needs TLS (or
//! `CAIRN_STORE_FILES_BASIC_ALLOW_HTTP=true` when TLS ends in front of the store).
//!
//! Files are served with `Content-Security-Policy: sandbox` and `nosniff`, so an HTML file in a bucket
//! cannot run script with the viewer's credentials; listing pages allow no script at all. Writes take no
//! form POSTs, so a cross-site page cannot write without passing the bucket's CORS rules.

use std::collections::BTreeMap;

use base64::Engine as _;
use bytes::Bytes;
use hyper::header::HeaderValue;
use hyper::{Method, StatusCode};
use sha2::{Digest, Sha256};

use super::auth::{self, aws_encode, Auth};
use super::errors::{self as e, ApiError};
use super::{accesslog, full, iso_time, Ctx, Dispatched, ErrorCode, Resp, Server};
use crate::engine::ListOptions;

const PREFIX: &str = "/-/files";

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

/// Query parameters of a presigned URL (SigV4 and SigV2); they stay for the signature check.
pub(crate) fn presign_param(k: &str) -> bool {
    k.to_ascii_lowercase().starts_with("x-amz-") || matches!(k, "Signature" | "AWSAccessKeyId" | "Expires")
}

pub(crate) fn html_escape(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => o.push_str("&amp;"),
            '<' => o.push_str("&lt;"),
            '>' => o.push_str("&gt;"),
            '"' => o.push_str("&quot;"),
            '\'' => o.push_str("&#39;"),
            c => o.push(c),
        }
    }
    o
}

fn human(n: u64) -> String {
    let units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut v = n as f64;
    let mut i = 0;
    while v >= 1024.0 && i < units.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", units[i])
    }
}

/// One directory entry.
struct Entry {
    name: String,
    dir: bool,
    size: u64,
    modified: u64,
    etag: String,
    storage_class: String,
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    let (x, y) = (Sha256::digest(a), Sha256::digest(b));
    x.iter().zip(y.iter()).fold(0u8, |acc, (p, q)| acc | (p ^ q)) == 0
}

impl Server {
    /// Is this request for the file API? (Path-style only: virtual-host requests are plain S3.)
    pub(crate) fn is_files_path(decoded: &str) -> bool {
        decoded == PREFIX || decoded.starts_with("/-/files/")
    }

    /// HTTP Basic credentials (`access_key:secret_key`), when allowed.
    fn basic_auth(&self, header: &str) -> Result<Auth, ApiError> {
        if !self.files_basic {
            return Err(e::FILES_BASIC_OFF.clone());
        }
        if !self.tls && !self.files_basic_http {
            return Err(e::FILES_BASIC_NEEDS_TLS.clone());
        }
        let raw = base64::engine::general_purpose::STANDARD.decode(header.trim()).map_err(|_| e::INVALID_ARGUMENT.clone())?;
        let s = String::from_utf8(raw).map_err(|_| e::INVALID_ARGUMENT.clone())?;
        let (ak, sk) = s.split_once(':').ok_or_else(|| e::INVALID_ARGUMENT.clone())?;
        let cred = self.creds.get(ak).ok_or_else(|| e::INVALID_ACCESS_KEY.clone())?;
        if !ct_eq(cred.secret_key.as_bytes(), sk.as_bytes()) {
            return Err(e::SIGNATURE_MISMATCH.clone());
        }
        let mut a = Auth::anonymous();
        a.cred = cred.clone();
        Ok(a)
    }

    /// Authenticates a file API or WebDAV request: HTTP Basic (when allowed), SigV4/SigV2 (header or
    /// presigned), or anonymous.
    pub(crate) fn plain_auth(
        &self,
        parts: &hyper::http::request::Parts,
        raw_path: &str,
        raw_query: &str,
        host: &str,
    ) -> Result<Auth, ApiError> {
        let authz = parts.headers.get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("");
        if let Some(b) = authz.strip_prefix("Basic ") {
            return self.basic_auth(b);
        }
        if auth::is_anonymous(&parts.headers, raw_query) {
            return Ok(Auth::anonymous());
        }
        let request = auth::Request {
            method: parts.method.as_str(),
            raw_path,
            raw_query,
            host,
            headers: &parts.headers,
            now: (self.clock)(),
            vhost_bucket: None,
        };
        auth::verify(&self.creds, &request)
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn dispatch_files<B>(
        &self,
        parts: hyper::http::request::Parts,
        body: &mut B,
        peer: Option<std::net::IpAddr>,
        request_id: &str,
        decoded: &str,
        raw_path: String,
        raw_query: String,
        host: String,
    ) -> Dispatched
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let rest = decoded.strip_prefix(PREFIX).unwrap_or("");
        let p = rest.strip_prefix('/').unwrap_or(rest);
        let (bucket, key) = match p.split_once('/') {
            Some((b, k)) => (b.to_string(), k.to_string()),
            None => (p.to_string(), String::new()),
        };
        let hdr = |n: &str| parts.headers.get(n).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let mut info = accesslog::LogInfo {
            bucket: bucket.clone(),
            key: key.clone(),
            method: parts.method.to_string(),
            uri: if raw_query.is_empty() { raw_path.clone() } else { format!("{raw_path}?{raw_query}") },
            referer: hdr("referer"),
            user_agent: hdr("user-agent"),
            host: hdr("host"),
            ..Default::default()
        };
        if parts.method == Method::OPTIONS {
            return ("PreflightCORS", self.preflight(&bucket, &parts.headers), None, info);
        }
        let cors = parts.headers.contains_key("origin").then(|| (bucket.clone(), parts.method.to_string(), parts.headers.clone()));
        let browser = hdr("accept").contains("text/html");
        let head = parts.method == Method::HEAD;
        let fail = |op: &'static str, err: ApiError, anonymous: bool, info| -> Dispatched {
            (op, Ok(self.files_error(&err, head, anonymous)), cors.clone(), info)
        };
        if !matches!(parts.method, Method::GET | Method::HEAD | Method::PUT | Method::DELETE) {
            return fail("MethodNotAllowed", e::METHOD_NOT_ALLOWED.clone(), false, info);
        }
        let anonymous = auth::is_anonymous(&parts.headers, &raw_query);
        let auth = match self.plain_auth(&parts, &raw_path, &raw_query, &host) {
            Ok(a) => a,
            Err(err) => return fail("auth", err, false, info),
        };
        info.principal = auth.cred.access_key.clone();
        let mut query: BTreeMap<String, String> = BTreeMap::new();
        let (mut after, mut limit) = (String::new(), 1000usize);
        for pair in raw_query.split('&').filter(|p| !p.is_empty()) {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            let (k, v) = (auth::query_decode(k), auth::query_decode(v));
            match k.as_str() {
                "after" => after = v,
                "limit" => match v.parse::<usize>() {
                    Ok(n) if n > 0 => limit = n.min(1000),
                    _ => return fail("route", e::INVALID_ARGUMENT.clone(), anonymous, info),
                },
                "versionId" => {
                    query.insert(k, v);
                }
                k if presign_param(k) => {
                    query.insert(k.to_string(), v);
                }
                // S3 subresources (?acl, ?tagging, ...) are not part of the file API.
                _ => return fail("route", e::FILES_PARAMETER.clone(), anonymous, info),
            }
        }
        let is_dir = (parts.method == Method::GET || head) && (key.is_empty() || key.ends_with('/'));
        if is_dir && query.contains_key("versionId") {
            return fail("route", e::FILES_PARAMETER.clone(), anonymous, info);
        }
        let content_length = parts.headers.get("content-length").and_then(|v| v.to_str().ok()?.parse().ok());
        let mut ctx = Ctx {
            method: parts.method,
            bucket: bucket.clone(),
            key: key.clone(),
            path: raw_path,
            query,
            headers: parts.headers,
            auth,
            content_length,
            op: "unknown",
            peer,
            request_id: request_id.to_string(),
        };
        let res = if ctx.key.len() > 1024 {
            Err(e::KEY_TOO_LONG.clone())
        } else if is_dir {
            self.files_dir(&mut ctx, &key, &after, limit, browser).await
        } else {
            self.route(&mut ctx, body).await.map(|mut r| {
                if ctx.method == Method::GET || head {
                    let h = r.headers_mut();
                    h.insert("content-security-policy", HeaderValue::from_static("sandbox"));
                    h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
                }
                r
            })
        };
        match res {
            // Handlers that answer errors themselves (delete markers, missing keys): JSON here too.
            Ok(r) if r.status().as_u16() >= 400 && r.extensions().get::<ErrorCode>().is_some() => {
                let ErrorCode(code) = *r.extensions().get::<ErrorCode>().unwrap();
                let err = ApiError { code, message: r.status().canonical_reason().unwrap_or("Error"), status: r.status().as_u16() };
                let mut j = self.files_error(&err, head, anonymous);
                for h in ["x-amz-delete-marker", "x-amz-version-id"] {
                    if let Some(v) = r.headers().get(h) {
                        j.headers_mut().insert(h, v.clone());
                    }
                }
                (ctx.op, Ok(j), cors, info)
            }
            Ok(r) => (ctx.op, Ok(r), cors, info),
            Err(err) => {
                self.audit_denial(&ctx, &err);
                let op = ctx.op;
                fail(op, err, anonymous, info)
            }
        }
    }

    /// Errors as JSON (an anonymous refusal asks a browser to sign in when Basic authentication is on).
    pub(crate) fn files_error(&self, err: &ApiError, head: bool, anonymous: bool) -> Resp {
        let ask = anonymous && self.files_basic && err.status == 403;
        let status =
            if ask { StatusCode::UNAUTHORIZED } else { StatusCode::from_u16(err.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR) };
        let body = if head { String::new() } else { serde_json::json!({"error": err.code, "message": err.message}).to_string() };
        let mut r = full(status, body);
        r.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
        if ask {
            r.headers_mut().insert("www-authenticate", HeaderValue::from_static("Basic realm=\"Cairn Store\", charset=\"UTF-8\""));
        }
        r.extensions_mut().insert(ErrorCode(err.code));
        r
    }

    /// Lists a directory (or the buckets), authorized as ListObjectsV2 with a `/` delimiter (ListBuckets).
    pub(crate) async fn files_dir(&self, c: &mut Ctx, prefix: &str, after: &str, limit: usize, browser: bool) -> Result<Resp, ApiError> {
        let mut entries = vec![];
        let mut next = None;
        c.key = String::new();
        if c.bucket.is_empty() {
            c.op = "ListBuckets";
            self.authorize_request(c).await?;
            let visible = |name: &str| c.auth.cred.admin || c.auth.cred.policy.is_some() || c.auth.cred.allows(name);
            for b in blocking(|| self.eng.list_buckets())?.into_iter().filter(|b| visible(&b.name)) {
                entries.push(Entry {
                    name: format!("{}/", b.name),
                    dir: true,
                    size: 0,
                    modified: b.created,
                    etag: String::new(),
                    storage_class: String::new(),
                });
            }
        } else {
            c.query.insert("list-type".into(), "2".into());
            c.query.insert("prefix".into(), prefix.to_string());
            c.query.insert("delimiter".into(), "/".into());
            c.op = super::handlers::classify(c);
            self.authorize_request(c).await?;
            let opt = ListOptions { prefix: prefix.to_string(), delimiter: "/".into(), after: after.to_string(), max_keys: limit };
            let bucket = c.bucket.clone();
            let res = blocking(|| self.eng.list(&bucket, &opt))?;
            for p in &res.common_prefixes {
                entries.push(Entry {
                    name: p[prefix.len()..].to_string(),
                    dir: true,
                    size: 0,
                    modified: 0,
                    etag: String::new(),
                    storage_class: String::new(),
                });
            }
            for o in res.objects.iter().filter(|o| o.key != prefix) {
                entries.push(Entry {
                    name: o.key[prefix.len()..].to_string(),
                    dir: false,
                    size: o.size,
                    modified: o.modified,
                    etag: o.etag.clone(),
                    storage_class: o.storage_class().to_string(),
                });
            }
            entries.sort_by(|a, b| (!a.dir, &a.name).cmp(&(!b.dir, &b.name)));
            if res.truncated {
                next = Some(res.next_after);
            }
        }
        let path = if c.bucket.is_empty() { "/".to_string() } else { format!("/{}/{prefix}", c.bucket) };
        let mut r = if browser { self.files_html(&path, &entries, next.as_deref()) } else { files_json(&path, &entries, next.as_deref()) };
        r.headers_mut().insert("cache-control", HeaderValue::from_static("no-store"));
        Ok(r)
    }

    fn files_html(&self, path: &str, entries: &[Entry], next: Option<&str>) -> Resp {
        let mut rows = String::new();
        if path != "/" {
            rows.push_str("<tr><td><a href=\"../\">../</a></td><td></td><td></td><td></td></tr>\n");
        }
        for en in entries {
            let base = en.name.trim_end_matches('/');
            let href = format!("./{}{}", aws_encode(base.as_bytes(), false), if en.dir { "/" } else { "" });
            let modified = if en.modified > 0 { iso_time(en.modified) } else { String::new() };
            let (size, class) = if en.dir { (String::new(), String::new()) } else { (human(en.size), en.storage_class.clone()) };
            rows.push_str(&format!(
                "<tr><td><a href=\"{}\">{}</a></td><td class=\"n\">{}</td><td>{}</td><td>{}</td></tr>\n",
                html_escape(&href),
                html_escape(&en.name),
                size,
                modified,
                html_escape(&class)
            ));
        }
        let more = next
            .map(|n| format!("<p><a href=\"?after={}\">Next page</a></p>", html_escape(&aws_encode(n.as_bytes(), false))))
            .unwrap_or_default();
        let title = html_escape(path);
        let doc = format!(
            "<!doctype html>\n<html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">\
             <title>{title} · Cairn Store</title><style>body{{font:14px system-ui,sans-serif;margin:24px;color:#1b1b1b}}\
             table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #e5e5e5}}\
             .n{{text-align:right}}a{{color:#0b57d0;text-decoration:none}}</style></head><body>\
             <h1>{title}</h1><table><thead><tr><th>Name</th><th class=\"n\">Size</th><th>Modified</th><th>Storage class</th></tr></thead>\
             <tbody>\n{rows}</tbody></table>{more}</body></html>\n"
        );
        let mut r = full(StatusCode::OK, doc);
        let h = r.headers_mut();
        h.insert("content-type", HeaderValue::from_static("text/html; charset=utf-8"));
        h.insert("content-security-policy", HeaderValue::from_static("default-src 'none'; style-src 'unsafe-inline'; form-action 'none'"));
        h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
        h.insert("x-frame-options", HeaderValue::from_static("DENY"));
        r
    }
}

fn files_json(path: &str, entries: &[Entry], next: Option<&str>) -> Resp {
    let list: Vec<serde_json::Value> = entries
        .iter()
        .map(|en| {
            if en.dir {
                serde_json::json!({"name": en.name, "type": "directory"})
            } else {
                serde_json::json!({"name": en.name, "type": "file", "size": en.size, "modified": iso_time(en.modified),
                    "etag": en.etag, "storage_class": en.storage_class})
            }
        })
        .collect();
    let doc = serde_json::json!({"path": path, "entries": list, "next": next});
    let mut r = full(StatusCode::OK, doc.to_string());
    r.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
    r
}
