//! Browser uploads: POST Object with an HTML form (multipart/form-data) and a signed policy document.
//!
//! The policy (base64 JSON: expiration and conditions) is signed with SigV4 (x-amz-signature) or SigV2
//! (signature); the store checks the signature, that the policy has not expired, and S3's rule that
//! every form field (except policy, signature fields, file and x-ignore-*) is covered by a condition, so
//! a browser cannot add fields the policy author did not allow. The upload is then authorized like a PUT
//! for the signing key (or as anonymous, without a policy, which needs a public bucket policy). The file
//! is streamed to disk; content-length-range is enforced while it arrives.

use std::collections::BTreeMap;

use base64::Engine as _;
use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::header::{HeaderName, HeaderValue};
use hyper::{HeaderMap, StatusCode};
use serde_json::Value;

use super::auth::{self, Auth};
use super::config::{check_acl_headers, parse_tagging, MAX_OBJECT_TAGS};
use super::errors::{self as e, ApiError};
use super::xml::Writer;
use super::{empty, set_header, xml_resp, Ctx, Resp, Server};
use crate::engine::{self, Object};

type R = Result<Resp, ApiError>;

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::STANDARD;
const MAX_FIELD: usize = 64 << 10;
const MAX_FIELDS_TOTAL: usize = 1 << 20;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    hay.windows(needle.len()).position(|w| w == needle)
}

/// A streaming multipart/form-data reader.
struct Form<B> {
    body: B,
    delim: Vec<u8>,
    buf: Vec<u8>,
    eof: bool,
    total: usize,
}

struct PartHead {
    name: String,
    filename: Option<String>,
    content_type: String,
}

impl<B> Form<B>
where
    B: hyper::body::Body<Data = Bytes> + Send + Unpin,
    B::Error: std::fmt::Display,
{
    fn new(body: B, boundary: &str) -> Self {
        // The body starts with "--boundary"; every later boundary is preceded by CRLF.
        Form { body, delim: format!("\r\n--{boundary}").into_bytes(), buf: b"\r\n".to_vec(), eof: false, total: 0 }
    }

    async fn fill(&mut self) -> Result<bool, ApiError> {
        while !self.eof {
            match self.body.frame().await {
                None => self.eof = true,
                Some(Err(_)) => return Err(e::INCOMPLETE_BODY.clone()),
                Some(Ok(f)) => {
                    if let Ok(d) = f.into_data() {
                        self.total += d.len();
                        self.buf.extend_from_slice(&d);
                        return Ok(true);
                    }
                }
            }
        }
        Ok(false)
    }

    /// Advances past the next boundary; false when it was the closing one.
    async fn next_part(&mut self) -> Result<Option<PartHead>, ApiError> {
        loop {
            if let Some(i) = find(&self.buf, &self.delim) {
                let after = i + self.delim.len();
                while self.buf.len() < after + 2 {
                    if !self.fill().await? {
                        return Err(e::MALFORMED_POST.clone());
                    }
                }
                if &self.buf[after..after + 2] == b"--" {
                    return Ok(None);
                }
                self.buf.drain(..after + 2);
                break;
            }
            if self.buf.len() > MAX_FIELD + self.delim.len() || !self.fill().await? {
                return Err(e::MALFORMED_POST.clone());
            }
        }
        let end = loop {
            if let Some(i) = find(&self.buf, b"\r\n\r\n") {
                break i;
            }
            if self.buf.len() > 16 << 10 || !self.fill().await? {
                return Err(e::MALFORMED_POST.clone());
            }
        };
        let head = String::from_utf8_lossy(&self.buf[..end]).into_owned();
        self.buf.drain(..end + 4);
        let mut out = PartHead { name: String::new(), filename: None, content_type: String::new() };
        for line in head.split("\r\n") {
            let Some((k, v)) = line.split_once(':') else { continue };
            match k.trim().to_ascii_lowercase().as_str() {
                "content-disposition" => {
                    for p in v.split(';').map(str::trim) {
                        if let Some(n) = p.strip_prefix("name=") {
                            out.name = n.trim_matches('"').to_string();
                        } else if let Some(f) = p.strip_prefix("filename=") {
                            out.filename = Some(f.trim_matches('"').to_string());
                        }
                    }
                }
                "content-type" => out.content_type = v.trim().to_string(),
                _ => {}
            }
        }
        if out.name.is_empty() {
            return Err(e::MALFORMED_POST.clone());
        }
        Ok(Some(out))
    }

    /// Reads a small field's value (up to the next boundary, which stays in the buffer).
    async fn value(&mut self) -> Result<String, ApiError> {
        loop {
            if let Some(i) = find(&self.buf, &self.delim) {
                let v = String::from_utf8_lossy(&self.buf[..i]).into_owned();
                self.buf.drain(..i);
                return Ok(v);
            }
            if self.buf.len() > MAX_FIELD || self.total > MAX_FIELDS_TOTAL || !self.fill().await? {
                return Err(e::MALFORMED_POST.clone());
            }
        }
    }

    /// Streams the file part to `sink` until the next boundary.
    async fn stream(&mut self, mut sink: impl FnMut(&[u8]) -> Result<(), ApiError>) -> Result<(), ApiError> {
        loop {
            if let Some(i) = find(&self.buf, &self.delim) {
                sink(&self.buf[..i])?;
                self.buf.drain(..i);
                return Ok(());
            }
            let safe = self.buf.len().saturating_sub(self.delim.len());
            if safe > 0 {
                sink(&self.buf[..safe])?;
                self.buf.drain(..safe);
            }
            if !self.fill().await? {
                return Err(e::MALFORMED_POST.clone());
            }
        }
    }
}

/// Checks the policy's expiration and conditions against the form fields (lower-cased names).
fn check_policy(policy: &Value, fields: &BTreeMap<String, String>, bucket: &str, now: i64) -> Result<Option<(u64, u64)>, ApiError> {
    let exp = policy.get("expiration").and_then(Value::as_str).ok_or_else(|| e::POST_POLICY.clone())?;
    let t = time::OffsetDateTime::parse(exp, &time::format_description::well_known::Rfc3339).map_err(|_| e::POST_POLICY.clone())?;
    if t.unix_timestamp() < now {
        return Err(e::POST_EXPIRED.clone());
    }
    let conds = policy.get("conditions").and_then(Value::as_array).ok_or_else(|| e::POST_POLICY.clone())?;
    let mut covered: Vec<String> = vec![];
    let mut range = None;
    let field = |name: &str| -> Option<String> {
        let n = name.trim_start_matches('$').to_ascii_lowercase();
        if n == "bucket" {
            Some(bucket.to_string())
        } else {
            fields.get(&n).cloned()
        }
    };
    for c in conds {
        match c {
            Value::Object(m) => {
                for (k, v) in m {
                    let want = v.as_str().ok_or_else(|| e::POST_POLICY.clone())?;
                    if field(k).as_deref() != Some(want) {
                        return Err(e::POST_CONDITION.clone());
                    }
                    covered.push(k.to_ascii_lowercase());
                }
            }
            Value::Array(a) => {
                let op = a.first().and_then(Value::as_str).unwrap_or("").to_ascii_lowercase();
                match op.as_str() {
                    "eq" | "starts-with" => {
                        let (Some(name), Some(want)) = (a.get(1).and_then(Value::as_str), a.get(2).and_then(Value::as_str)) else {
                            return Err(e::POST_POLICY.clone());
                        };
                        let got = field(name).unwrap_or_default();
                        let ok = if op == "eq" {
                            got == want
                        } else if name.eq_ignore_ascii_case("$content-type") {
                            // starts-with on Content-Type applies to each comma-separated value.
                            got.split(',').all(|v| v.trim().starts_with(want))
                        } else {
                            got.starts_with(want)
                        };
                        if !ok {
                            return Err(e::POST_CONDITION.clone());
                        }
                        covered.push(name.trim_start_matches('$').to_ascii_lowercase());
                    }
                    "content-length-range" => {
                        let (Some(lo), Some(hi)) = (a.get(1).and_then(Value::as_u64), a.get(2).and_then(Value::as_u64)) else {
                            return Err(e::POST_POLICY.clone());
                        };
                        range = Some((lo, hi));
                    }
                    _ => return Err(e::POST_POLICY.clone()),
                }
            }
            _ => return Err(e::POST_POLICY.clone()),
        }
    }
    if !covered.iter().any(|c| c == "bucket") {
        return Err(e::POST_CONDITION.clone());
    }
    for name in fields.keys() {
        let exempt =
            ["policy", "x-amz-signature", "signature", "awsaccesskeyid", "file"].contains(&name.as_str()) || name.starts_with("x-ignore-");
        if !exempt && !covered.contains(name) {
            return Err(e::POST_CONDITION.clone());
        }
    }
    Ok(range)
}

/// Form fields that become request headers of the upload.
fn header_fields(fields: &BTreeMap<String, String>) -> HeaderMap {
    let mut h = HeaderMap::new();
    for (k, v) in fields {
        let name = match k.as_str() {
            "acl" => "x-amz-acl".to_string(),
            "content-type" | "cache-control" | "content-disposition" | "content-encoding" | "expires" | "content-md5" => k.clone(),
            _ if k.starts_with("x-amz-meta-")
                || k.starts_with("x-amz-server-side-encryption")
                || k.starts_with("x-amz-object-lock-")
                || k.starts_with("x-amz-checksum-") =>
            {
                k.clone()
            }
            _ => continue,
        };
        if let (Ok(n), Ok(v)) = (HeaderName::from_bytes(name.as_bytes()), HeaderValue::from_str(v)) {
            h.insert(n, v);
        }
    }
    h
}

impl Server {
    pub(crate) async fn post_object<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let ct = c.header("content-type");
        let boundary = ct.split(';').map(str::trim).find_map(|p| p.strip_prefix("boundary=")).map(|b| b.trim_matches('"').to_string());
        let Some(boundary) = boundary.filter(|b| !b.is_empty() && b.len() <= 200) else { return Err(e::MALFORMED_POST.clone()) };
        let mut form = Form::new(body, &boundary);
        let mut fields: BTreeMap<String, String> = BTreeMap::new();
        let mut file: Option<PartHead> = None;
        while let Some(part) = form.next_part().await? {
            if part.filename.is_some() || part.name.eq_ignore_ascii_case("file") {
                file = Some(part);
                break;
            }
            let v = form.value().await?;
            fields.insert(part.name.to_ascii_lowercase(), v);
        }
        let file = file.ok_or_else(|| e::POST_NO_FILE.clone())?;
        if !file.content_type.is_empty() && !fields.contains_key("content-type") {
            // The file part's own type is used only when the form gives none.
            fields.insert("content-type".into(), file.content_type.clone());
            fields.insert("x-ignore-file-content-type".into(), String::new());
        }

        if !fields.contains_key("key") {
            return Err(e::POST_NO_KEY.clone());
        }
        // Who is uploading: the policy's signer, or anonymous.
        let now = (self.clock)();
        let mut range = None;
        let auth = match fields.get("policy") {
            Some(p64) => {
                let doc = B64.decode(p64.trim()).map_err(|_| e::POST_POLICY.clone())?;
                let policy: Value = serde_json::from_slice(&doc).map_err(|_| e::POST_POLICY.clone())?;
                let cred = if fields.get("x-amz-algorithm").map(String::as_str) == Some(auth::ALGORITHM) {
                    let g = |k: &str| fields.get(k).map(String::as_str).unwrap_or("");
                    auth::verify_post_v4(&self.creds, g("x-amz-credential"), g("x-amz-date"), p64.trim(), g("x-amz-signature"))?
                } else if let Some(ak) = fields.get("awsaccesskeyid") {
                    auth::verify_post_v2(&self.creds, ak, p64.trim(), fields.get("signature").map(String::as_str).unwrap_or(""))?
                } else {
                    return Err(e::POST_POLICY.clone());
                };
                range = check_policy(&policy, &fields, &c.bucket, now.unix_timestamp())?;
                let mut a = Auth::anonymous();
                a.cred = cred;
                a.scope = "post".into();
                a
            }
            None if fields.keys().any(|k| ["x-amz-signature", "signature", "awsaccesskeyid", "x-amz-credential"].contains(&k.as_str())) => {
                return Err(e::POST_POLICY.clone());
            }
            None => Auth::anonymous(),
        };
        let filename = file.filename.clone().unwrap_or_default();
        let key = fields.get("key").ok_or_else(|| e::POST_NO_KEY.clone())?.replace("${filename}", &filename);
        if key.is_empty() || key.len() > 1024 {
            return Err(e::POST_NO_KEY.clone());
        }
        let mut fc = c.clone();
        fc.key = key.clone();
        fc.auth = auth;
        fc.headers = header_fields(&fields);
        fc.op = "PostObject";
        self.authorize(&fc, "s3:PutObject", &c.bucket, &key).await?;
        check_acl_headers(&fc)?;
        let tags = match fields.get("tagging") {
            Some(doc) => parse_tagging(doc.as_bytes(), MAX_OBJECT_TAGS)?,
            None => BTreeMap::new(),
        };
        let info = blocking(|| self.eng.head_bucket(&c.bucket))?;
        let lock = self.lock_for_write(&fc, &c.bucket)?;
        let (sse, dek) = self.sse_for_write(&fc, &c.bucket, &key)?.unzip();
        let (content_type, headers, user_meta) = super::handlers::object_headers(&fc);
        let max = range.map(|(_, hi)| hi).unwrap_or(u64::MAX).min(self.max_obj);
        let mut blob = self.eng.new_blob_with(dek.as_ref())?;
        let mut pending: Vec<u8> = Vec::with_capacity(256 << 10);
        let mut too_large = false;
        form.stream(|chunk| {
            if blob.size() + pending.len() as u64 + chunk.len() as u64 > max {
                too_large = true;
                return Err(e::ENTITY_TOO_LARGE.clone());
            }
            pending.extend_from_slice(chunk);
            if pending.len() >= 128 << 10 {
                blocking(|| blob.write(&pending)).map_err(engine::Error::Io)?;
                pending.clear();
            }
            Ok(())
        })
        .await
        .map_err(|err| if too_large { e::ENTITY_TOO_LARGE.clone() } else { err })?;
        blocking(|| blob.write(&pending)).map_err(engine::Error::Io)?;
        if let Some((lo, _)) = range {
            if blob.size() < lo {
                return Err(e::ENTITY_TOO_SMALL.clone());
            }
        }
        let md5 = blob.md5();
        if let Some(want) = fields.get("content-md5") {
            if B64.encode(&md5) != *want {
                return Err(e::BAD_DIGEST.clone());
            }
        }
        let seg = blocking(|| blob.seal())?;
        let obj = Object {
            key: key.clone(),
            size: seg.size,
            etag: hex::encode(&md5),
            modified: engine::now_ms(),
            content_type,
            headers,
            user_meta,
            checksum_type: "FULL_OBJECT".into(),
            segments: vec![seg],
            tags,
            lock,
            sse,
            ..Default::default()
        };
        let stored = blocking(|| self.eng.put_object(&c.bucket, obj, &Default::default()))?;
        let etag = format!("\"{}\"", stored.etag);
        let location = format!("/{}/{}", c.bucket, super::auth::aws_encode(key.as_bytes(), true));
        let redirect = fields.get("success_action_redirect").or(fields.get("redirect")).filter(|u| !u.is_empty());
        let mut r = if let Some(url) = redirect {
            let sep = if url.contains('?') { '&' } else { '?' };
            let q = |s: &str| super::auth::aws_encode(s.as_bytes(), false);
            let mut r = empty(StatusCode::SEE_OTHER);
            set_header(&mut r, "location", &format!("{url}{sep}bucket={}&key={}&etag={}", q(&c.bucket), q(&key), q(&etag)));
            r
        } else {
            match fields.get("success_action_status").map(String::as_str) {
                Some("200") => empty(StatusCode::OK),
                Some("201") => {
                    let mut w = Writer::new("PostResponse");
                    w.el("Location", &location).el("Bucket", &c.bucket).el("Key", &key).el("ETag", &etag);
                    xml_resp(StatusCode::CREATED, w.finish())
                }
                _ => empty(StatusCode::NO_CONTENT),
            }
        };
        set_header(&mut r, "etag", &etag);
        if redirect.is_none() {
            set_header(&mut r, "location", &location);
        }
        if !info.versioning.is_empty() {
            set_header(&mut r, "x-amz-version-id", stored.version());
        }
        Self::sse_headers(&mut r, &stored.sse);
        Ok(r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fields(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    #[test]
    fn policy_conditions() {
        let p: Value = serde_json::from_str(
            r#"{"expiration": "2099-01-01T00:00:00Z", "conditions": [
                {"bucket": "uploads"}, ["starts-with", "$key", "user/42/"], {"acl": "private"},
                ["starts-with", "$Content-Type", "image/"], ["content-length-range", 1, 1048576],
                ["eq", "$x-amz-meta-source", "web"]]}"#,
        )
        .unwrap();
        let ok = fields(&[
            ("key", "user/42/a.png"),
            ("acl", "private"),
            ("content-type", "image/png"),
            ("x-amz-meta-source", "web"),
            ("policy", "..."),
        ]);
        assert_eq!(check_policy(&p, &ok, "uploads", 0).unwrap(), Some((1, 1048576)));
        assert!(check_policy(&p, &ok, "other", 0).is_err());
        let mut bad = ok.clone();
        bad.insert("key".into(), "user/43/a.png".into());
        assert!(check_policy(&p, &bad, "uploads", 0).is_err());
        let mut extra = ok.clone();
        extra.insert("x-amz-meta-sneaky".into(), "1".into()); // a field no condition covers
        assert!(check_policy(&p, &extra, "uploads", 0).is_err());
        let mut ignored = ok.clone();
        ignored.insert("x-ignore-tracking".into(), "1".into());
        assert!(check_policy(&p, &ignored, "uploads", 0).is_ok());
        let expired: Value =
            serde_json::from_str(r#"{"expiration": "2001-01-01T00:00:00Z", "conditions": [{"bucket": "uploads"}]}"#).unwrap();
        assert_eq!(check_policy(&expired, &fields(&[]), "uploads", 1_700_000_000).unwrap_err(), e::POST_EXPIRED);
    }
}
