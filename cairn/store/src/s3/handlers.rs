//! S3 operations.

use std::collections::BTreeMap;
use std::io::{self, Read};
use std::net::IpAddr;

use base64::Engine as _;
use bytes::Bytes;
use http_body_util::{BodyExt, StreamBody};
use hyper::body::Frame;
use hyper::{Method, Response, StatusCode};

use super::body::BodyCheck;
use super::checksum::{self, Hasher};
use super::config::{check_acl_headers, parse_tag_header};
use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, http_time, iso_time, set_header, xml_resp, Ctx, Resp, Server};
use crate::engine::{self, CompletePart, Condition, FreshError, ListOptions, Object, Part, Segment, Upload};

type R = Result<Resp, ApiError>;
/// A copied range: the source object, the new blob, its MD5 and checksums.
type Copied = (Object, Segment, Vec<u8>, BTreeMap<String, String>);

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::STANDARD;
const B64URL: base64::engine::GeneralPurpose = base64::engine::general_purpose::URL_SAFE_NO_PAD;

/// Engine calls block on disk and fsync: run them off the async scheduler's fast path.
fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

/// Bucket configuration Cairn Store does not support yet: setting it is refused, never silently ignored.
const UNSUPPORTED_BUCKET_CONFIG: [&str; 14] = [
    "versioning",
    "cors",
    "policy",
    "lifecycle",
    "publicAccessBlock",
    "object-lock",
    "encryption",
    "notification",
    "replication",
    "logging",
    "website",
    "requestPayment",
    "accelerate",
    "intelligent-tiering",
];

/// Headers stored with an object and returned on GET/HEAD.
const STORED_HEADERS: [&str; 5] = ["content-encoding", "content-disposition", "content-language", "cache-control", "expires"];

fn object_headers(c: &Ctx) -> (String, BTreeMap<String, String>, BTreeMap<String, String>) {
    let ct = match c.header("content-type") {
        "" => "binary/octet-stream".to_string(),
        v => v.to_string(),
    };
    let mut hs = BTreeMap::new();
    for h in STORED_HEADERS {
        let mut v = c.header(h).to_string();
        if h == "content-encoding" {
            v = v
                .split(',')
                .map(str::trim)
                .filter(|t| !t.is_empty() && !t.eq_ignore_ascii_case("aws-chunked"))
                .collect::<Vec<_>>()
                .join(",");
        }
        if !v.is_empty() {
            hs.insert(h.to_string(), v);
        }
    }
    let mut um = BTreeMap::new();
    for (k, v) in c.headers.iter() {
        if let Some(name) = k.as_str().strip_prefix("x-amz-meta-") {
            let v = String::from_utf8_lossy(v.as_bytes()).into_owned();
            um.entry(name.to_string()).and_modify(|e: &mut String| e.push_str(&format!(",{v}"))).or_insert(v);
        }
    }
    (ct, hs, um)
}

fn meta_too_large(um: &BTreeMap<String, String>) -> bool {
    um.iter().map(|(k, v)| k.len() + v.len()).sum::<usize>() > 2048
}

fn valid_bucket_name(n: &str) -> bool {
    let b = n.as_bytes();
    (3..=63).contains(&b.len())
        && b.iter().all(|&c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'.' || c == b'-')
        && b[0].is_ascii_alphanumeric()
        && b[b.len() - 1].is_ascii_alphanumeric()
        && !n.contains("..")
        && !n.contains(".-")
        && !n.contains("-.")
        && n.parse::<IpAddr>().is_err()
}

fn cond_of(c: &Ctx) -> Condition {
    let opt = |s: &str| (!s.is_empty()).then(|| s.to_string());
    Condition { if_match: opt(c.header("if-match")), if_none_match: opt(c.header("if-none-match")) }
}

fn etag_header_matches(header: &str, etag: &str) -> bool {
    engine::etag_matches(header, etag.trim_matches('"'))
}

impl Server {
    pub(crate) async fn route<B>(&self, c: &mut Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        if c.bucket.is_empty() {
            c.op = "ListBuckets";
            if c.method != Method::GET {
                return Err(e::METHOD_NOT_ALLOWED.clone());
            }
            return self.list_buckets(c);
        }
        if c.key.is_empty() {
            return self.bucket_op(c, body).await;
        }
        self.object_op(c, body).await
    }

    async fn bucket_op<B>(&self, c: &mut Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        match c.method {
            Method::PUT if c.has("tagging") => {
                c.op = "PutBucketTagging";
                self.put_bucket_tagging(c, body).await
            }
            Method::PUT if c.has("acl") => {
                c.op = "PutBucketAcl";
                self.put_acl(c, body, false).await
            }
            Method::PUT if c.has("ownershipControls") => {
                c.op = "PutBucketOwnershipControls";
                self.put_ownership(c, body).await
            }
            Method::PUT if UNSUPPORTED_BUCKET_CONFIG.iter().any(|k| c.has(k)) => {
                c.op = "Unsupported";
                Err(e::NOT_IMPLEMENTED.clone())
            }
            Method::PUT => {
                c.op = "CreateBucket";
                self.create_bucket(c, body).await
            }
            Method::DELETE if c.has("tagging") => {
                c.op = "DeleteBucketTagging";
                self.delete_config(c, "tagging")
            }
            Method::DELETE if c.has("ownershipControls") => {
                c.op = "Unsupported";
                Err(e::NOT_IMPLEMENTED.clone())
            }
            Method::DELETE
                if ["policy", "cors", "lifecycle", "publicAccessBlock", "encryption", "website", "replication"]
                    .iter()
                    .any(|k| c.has(k)) =>
            {
                // None of these can be set yet, so there is nothing to delete.
                c.op = "DeleteBucketConfig";
                blocking(|| self.eng.head_bucket(&c.bucket))?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            Method::DELETE => {
                c.op = "DeleteBucket";
                if !c.auth.cred.admin {
                    return Err(e::ACCESS_DENIED.clone());
                }
                blocking(|| self.eng.delete_bucket(&c.bucket))?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            Method::HEAD => {
                c.op = "HeadBucket";
                blocking(|| self.eng.head_bucket(&c.bucket))?;
                let mut r = empty(StatusCode::OK);
                set_header(&mut r, "x-amz-bucket-region", &self.region);
                Ok(r)
            }
            Method::POST if c.has("delete") => {
                c.op = "DeleteObjects";
                self.delete_objects(c, body).await
            }
            Method::GET => {
                blocking(|| self.eng.head_bucket(&c.bucket))?;
                if c.has("location") {
                    c.op = "GetBucketLocation";
                    let region = if self.region == "us-east-1" { "" } else { self.region.as_str() };
                    return Ok(xml_resp(
                        StatusCode::OK,
                        format!(
                            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<LocationConstraint xmlns=\"{}\">{}</LocationConstraint>",
                            xml::XMLNS,
                            region
                        ),
                    ));
                }
                if c.has("versioning") {
                    c.op = "GetBucketVersioning";
                    return Ok(xml_resp(StatusCode::OK, Writer::new("VersioningConfiguration").finish()));
                }
                if c.has("acl") {
                    c.op = "GetBucketAcl";
                    return Ok(self.acl(c));
                }
                if c.has("uploads") {
                    c.op = "ListMultipartUploads";
                    return self.list_uploads(c);
                }
                if c.has("tagging") {
                    c.op = "GetBucketTagging";
                    return self.get_bucket_tagging(c);
                }
                if c.has("ownershipControls") {
                    c.op = "GetBucketOwnershipControls";
                    return self.get_ownership(c);
                }
                for (k, err) in [
                    ("policy", &e::NO_SUCH_BUCKET_POLICY),
                    ("lifecycle", &e::NO_SUCH_LIFECYCLE),
                    ("cors", &e::NO_SUCH_CORS),
                    ("publicAccessBlock", &e::NO_SUCH_PUBLIC_ACCESS_BLOCK),
                ] {
                    if c.has(k) {
                        return Err(err.clone());
                    }
                }
                if ["versions", "object-lock", "replication", "encryption", "website", "logging", "notification", "requestPayment"]
                    .iter()
                    .any(|k| c.has(k))
                {
                    c.op = "Unsupported";
                    return Err(e::NOT_IMPLEMENTED.clone());
                }
                if c.q("list-type") == "2" {
                    c.op = "ListObjectsV2";
                    return self.list(c, true);
                }
                c.op = "ListObjects";
                self.list(c, false)
            }
            Method::POST => Err(e::NOT_IMPLEMENTED.clone()),
            _ => Err(e::METHOD_NOT_ALLOWED.clone()),
        }
    }

    async fn object_op<B>(&self, c: &mut Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let copy = !c.header("x-amz-copy-source").is_empty();
        match c.method {
            Method::PUT if c.has("uploadId") && c.has("partNumber") => {
                if copy {
                    c.op = "UploadPartCopy";
                    self.upload_part_copy(c)
                } else {
                    c.op = "UploadPart";
                    self.upload_part(c, body).await
                }
            }
            Method::PUT if c.has("tagging") => {
                c.op = "PutObjectTagging";
                self.put_object_tagging(c, body).await
            }
            Method::PUT if c.has("acl") => {
                c.op = "PutObjectAcl";
                self.put_acl(c, body, true).await
            }
            Method::PUT if ["retention", "legal-hold"].iter().any(|k| c.has(k)) => {
                c.op = "Unsupported";
                Err(e::NOT_IMPLEMENTED.clone())
            }
            Method::PUT if copy => {
                c.op = "CopyObject";
                self.copy_object(c)
            }
            Method::PUT => {
                c.op = "PutObject";
                self.put_object(c, body).await
            }
            Method::GET if c.has("uploadId") => {
                c.op = "ListParts";
                self.list_parts(c)
            }
            Method::GET if c.has("acl") => {
                c.op = "GetObjectAcl";
                blocking(|| self.eng.get_object(&c.bucket, &c.key))?;
                Ok(self.acl(c))
            }
            Method::GET if c.has("tagging") => {
                c.op = "GetObjectTagging";
                self.get_object_tagging(c)
            }
            Method::GET if ["attributes", "torrent", "retention", "legal-hold"].iter().any(|k| c.has(k)) => Err(e::NOT_IMPLEMENTED.clone()),
            Method::GET => {
                c.op = "GetObject";
                self.get_object(c, false)
            }
            Method::HEAD => {
                c.op = "HeadObject";
                self.get_object(c, true)
            }
            Method::DELETE if c.has("uploadId") => {
                c.op = "AbortMultipartUpload";
                blocking(|| self.eng.abort_upload(&c.bucket, &c.key, c.q("uploadId")))?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            Method::DELETE if c.has("tagging") => {
                c.op = "DeleteObjectTagging";
                self.delete_object_tagging(c)
            }
            Method::DELETE => {
                c.op = "DeleteObject";
                let if_match = c.header("if-match");
                blocking(|| self.eng.delete_object(&c.bucket, &c.key, (!if_match.is_empty()).then_some(if_match)))?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            Method::POST if c.has("uploads") => {
                c.op = "CreateMultipartUpload";
                self.create_upload(c)
            }
            Method::POST if c.has("uploadId") => {
                c.op = "CompleteMultipartUpload";
                self.complete_upload(c, body).await
            }
            Method::POST => Err(e::NOT_IMPLEMENTED.clone()),
            _ => Err(e::METHOD_NOT_ALLOWED.clone()),
        }
    }

    fn owner_xml(&self, c: &Ctx, w: &mut Writer, tag: &'static str) {
        let id = &c.auth.cred.access_key;
        w.open(tag).el("ID", id).el("DisplayName", id).close();
    }

    fn acl(&self, c: &Ctx) -> Resp {
        let id = &c.auth.cred.access_key;
        let mut w = Writer::new("AccessControlPolicy");
        self.owner_xml(c, &mut w, "Owner");
        w.open("AccessControlList").open("Grant").raw(&format!(
            "<Grantee xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" xsi:type=\"CanonicalUser\"><ID>{0}</ID><DisplayName>{0}</DisplayName></Grantee>",
            xml::escape(id)));
        w.el("Permission", "FULL_CONTROL").close().close();
        xml_resp(StatusCode::OK, w.finish())
    }

    // --------------------------------------------------------------------------------- request bodies

    /// Reads a small request body (XML documents), verifying it.
    pub(crate) async fn read_small<B>(&self, c: &Ctx, body: B, limit: usize) -> Result<Vec<u8>, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let mut check = BodyCheck::new(&c.headers, c.content_length, &c.auth, &[])?;
        let mut out = Vec::new();
        let mut body = body;
        while let Some(frame) = body.frame().await {
            let frame = frame.map_err(|_| e::INCOMPLETE_BODY.clone())?;
            if let Ok(data) = frame.into_data() {
                check.push(&data, &mut out)?;
                if out.len() > limit {
                    return Err(e::ENTITY_TOO_LARGE.clone());
                }
            }
        }
        check.finish()?;
        let md5 = c.header("content-md5");
        if !md5.is_empty() {
            use md5::Digest;
            if B64.encode(md5::Md5::digest(&out)) != md5 {
                return Err(e::BAD_DIGEST.clone());
            }
        }
        Ok(out)
    }

    /// Streams a request body into a sealed blob, verifying signatures, SHA-256, Content-MD5 and
    /// checksums; `compute` adds checksum algorithms to compute even if the client sent none.
    async fn receive<B>(
        &self,
        c: &Ctx,
        body: B,
        limit: u64,
        compute: &[&str],
    ) -> Result<(Segment, Vec<u8>, BTreeMap<String, String>), ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let chunked = !c.header("x-amz-decoded-content-length").is_empty();
        if c.content_length.is_none() && !chunked {
            return Err(e::MISSING_LENGTH.clone());
        }
        let mut check = BodyCheck::new(&c.headers, c.content_length, &c.auth, compute)?;
        if check.expected_len().is_some_and(|n| n > limit) {
            return Err(e::ENTITY_TOO_LARGE.clone());
        }
        let mut blob = self.eng.new_blob()?;
        let mut out = Vec::with_capacity(256 << 10);
        let mut body = body;
        while let Some(frame) = body.frame().await {
            let frame = frame.map_err(|err| {
                tracing::debug!(error = %err, "request body");
                e::INCOMPLETE_BODY.clone()
            })?;
            let Ok(data) = frame.into_data() else { continue };
            check.push(&data, &mut out)?;
            if blob.size() + out.len() as u64 > limit {
                return Err(e::ENTITY_TOO_LARGE.clone());
            }
            if out.len() >= 128 << 10 {
                blocking(|| blob.write(&out)).map_err(engine::Error::Io)?;
                out.clear();
            }
        }
        check.finish()?;
        blocking(|| blob.write(&out)).map_err(engine::Error::Io)?;
        let md5 = blob.md5();
        let want = c.header("content-md5");
        if !want.is_empty() && B64.encode(&md5) != want {
            return Err(e::BAD_DIGEST.clone());
        }
        let seg = blocking(|| blob.seal())?;
        Ok((seg, md5, check.checksums()))
    }

    // --------------------------------------------------------------------------------- buckets

    fn list_buckets(&self, c: &Ctx) -> R {
        let buckets = blocking(|| self.eng.list_buckets())?;
        let mut w = Writer::new("ListAllMyBucketsResult");
        self.owner_xml(c, &mut w, "Owner");
        w.open("Buckets");
        for b in buckets.iter().filter(|b| c.auth.cred.allows(&b.name)) {
            w.open("Bucket").el("Name", &b.name).el("CreationDate", iso_time(b.created)).close();
        }
        w.close();
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    async fn create_bucket<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        if !c.auth.cred.admin {
            return Err(e::ACCESS_DENIED.clone());
        }
        if !valid_bucket_name(&c.bucket) {
            return Err(e::INVALID_BUCKET_NAME.clone());
        }
        check_acl_headers(c)?;
        if c.header("x-amz-bucket-object-lock-enabled").eq_ignore_ascii_case("true") {
            return Err(e::NOT_IMPLEMENTED.clone());
        }
        let doc = self.read_small(c, body, 64 << 10).await?;
        let doc = String::from_utf8_lossy(&doc);
        if !doc.trim().is_empty() && xml::parse(&doc).is_none() {
            return Err(e::MALFORMED_XML.clone());
        }
        blocking(|| self.eng.create_bucket(&c.bucket))?;
        let mut r = empty(StatusCode::OK);
        set_header(&mut r, "location", &format!("/{}", c.bucket));
        Ok(r)
    }

    async fn delete_objects<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 2 << 20).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc)).filter(|r| r.name == "Delete").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let keys: Vec<String> = root.all("Object").map(|o| o.child("Key").map(|k| k.text.clone()).unwrap_or_default()).collect();
        if keys.is_empty() || keys.len() > 1000 {
            return Err(e::MALFORMED_XML.clone());
        }
        let quiet = root.text_of("Quiet").eq_ignore_ascii_case("true");
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let mut w = Writer::new("DeleteResult");
        for k in keys {
            match blocking(|| self.eng.delete_object(&c.bucket, &k, None)) {
                Ok(()) if !quiet => {
                    w.open("Deleted").el("Key", &k).close();
                }
                Ok(()) => {}
                Err(err) => {
                    let ae = ApiError::from(err);
                    w.open("Error").el("Key", &k).el("Code", ae.code).el("Message", ae.message).close();
                }
            }
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    fn max_keys(&self, c: &Ctx, name: &str) -> Result<usize, ApiError> {
        match c.q(name) {
            "" => Ok(1000),
            v => v.parse::<usize>().map(|n| n.min(1000)).map_err(|_| e::INVALID_ARGUMENT.clone()),
        }
    }

    fn list(&self, c: &Ctx, v2: bool) -> R {
        let max_keys = self.max_keys(c, "max-keys")?;
        let url = match c.q("encoding-type") {
            "" => false,
            "url" => true,
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        let enc = |s: &str| if url { super::auth::aws_encode(s.as_bytes(), true) } else { s.to_string() };
        let token = c.q("continuation-token");
        let after = if v2 {
            if token.is_empty() {
                c.q("start-after").to_string()
            } else {
                String::from_utf8(B64URL.decode(token).map_err(|_| e::INVALID_TOKEN.clone())?).map_err(|_| e::INVALID_TOKEN.clone())?
            }
        } else {
            c.q("marker").to_string()
        };
        let opt = ListOptions { prefix: c.q("prefix").into(), delimiter: c.q("delimiter").into(), after, max_keys };
        let res = blocking(|| self.eng.list(&c.bucket, &opt))?;
        let mut w = Writer::new("ListBucketResult");
        w.el("Name", &c.bucket).el("Prefix", enc(c.q("prefix")));
        if v2 {
            w.opt("StartAfter", enc(c.q("start-after"))).opt("ContinuationToken", token);
            if res.truncated {
                w.el("NextContinuationToken", B64URL.encode(res.next_after.as_bytes()));
            }
            w.el("KeyCount", (res.objects.len() + res.common_prefixes.len()).to_string());
        } else {
            w.el("Marker", enc(c.q("marker")));
            if res.truncated && !opt.delimiter.is_empty() {
                w.el("NextMarker", enc(&res.next_after));
            }
        }
        w.opt("Delimiter", enc(&opt.delimiter)).el("MaxKeys", max_keys.to_string());
        if url {
            w.el("EncodingType", "url");
        }
        w.el("IsTruncated", res.truncated.to_string());
        let with_owner = !v2 || c.q("fetch-owner") == "true";
        for o in &res.objects {
            w.open("Contents")
                .el("Key", enc(&o.key))
                .el("LastModified", iso_time(o.modified))
                .el("ETag", format!("\"{}\"", o.etag))
                .el("Size", o.size.to_string())
                .el("StorageClass", "STANDARD");
            if with_owner {
                self.owner_xml(c, &mut w, "Owner");
            }
            for a in o.checksums.keys() {
                w.el("ChecksumAlgorithm", a.to_ascii_uppercase());
            }
            if !o.checksums.is_empty() {
                w.opt("ChecksumType", &o.checksum_type);
            }
            w.close();
        }
        for p in &res.common_prefixes {
            w.open("CommonPrefixes").el("Prefix", enc(p)).close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    fn list_uploads(&self, c: &Ctx) -> R {
        let max = self.max_keys(c, "max-uploads")?;
        let (prefix, delim) = (c.q("prefix"), c.q("delimiter"));
        let ups = blocking(|| self.eng.list_uploads(&c.bucket, prefix))?;
        let (key_marker, id_marker) = (c.q("key-marker"), c.q("upload-id-marker"));
        let mut w = Writer::new("ListMultipartUploadsResult");
        w.el("Bucket", &c.bucket)
            .el("KeyMarker", key_marker)
            .el("UploadIdMarker", id_marker)
            .el("Prefix", prefix)
            .opt("Delimiter", delim)
            .el("MaxUploads", max.to_string());
        let (mut listed, mut truncated, mut next) = (vec![], false, (String::new(), String::new()));
        let mut prefixes: Vec<String> = vec![];
        for u in ups {
            if !key_marker.is_empty()
                && (u.key.as_str() < key_marker || (u.key == key_marker && (id_marker.is_empty() || u.id.as_str() <= id_marker)))
            {
                continue;
            }
            if !delim.is_empty() {
                if let Some(i) = u.key[prefix.len()..].find(delim) {
                    let cp = format!("{prefix}{}", &u.key[prefix.len()..prefix.len() + i + delim.len()]);
                    if !prefixes.contains(&cp) {
                        prefixes.push(cp);
                    }
                    continue;
                }
            }
            if listed.len() >= max {
                truncated = true;
                break;
            }
            next = (u.key.clone(), u.id.clone());
            listed.push(u);
        }
        w.el("NextKeyMarker", &next.0).el("NextUploadIdMarker", &next.1).el("IsTruncated", truncated.to_string());
        for u in &listed {
            w.open("Upload").el("Key", &u.key).el("UploadId", &u.id);
            self.owner_xml(c, &mut w, "Initiator");
            self.owner_xml(c, &mut w, "Owner");
            w.el("StorageClass", "STANDARD").el("Initiated", iso_time(u.initiated)).close();
        }
        for p in &prefixes {
            w.open("CommonPrefixes").el("Prefix", p).close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    // --------------------------------------------------------------------------------- objects

    async fn put_object<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let (ct, hs, um) = object_headers(c);
        if meta_too_large(&um) {
            return Err(e::METADATA_TOO_LARGE.clone());
        }
        check_acl_headers(c)?;
        let tags = parse_tag_header(c.header("x-amz-tagging"))?;
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let algo = c.header("x-amz-checksum-algorithm").to_ascii_lowercase();
        let compute: Vec<&str> = [algo.as_str()].into_iter().filter(|a| !a.is_empty()).collect();
        let (seg, md5, checksums) = self.receive(c, body, self.max_obj, &compute).await?;
        let obj = Object {
            key: c.key.clone(),
            size: seg.size,
            etag: hex::encode(&md5),
            modified: engine::now_ms(),
            content_type: ct,
            headers: hs,
            user_meta: um,
            checksums: checksums.clone(),
            checksum_type: "FULL_OBJECT".into(),
            segments: vec![seg],
            part_count: 0,
            tags,
        };
        let etag = obj.etag.clone();
        blocking(|| self.eng.put_object(&c.bucket, obj, &cond_of(c)))?;
        let mut r = empty(StatusCode::OK);
        set_header(&mut r, "etag", &format!("\"{etag}\""));
        write_checksums(&mut r, &checksums, "FULL_OBJECT");
        Ok(r)
    }

    fn get_object(&self, c: &Ctx, head: bool) -> R {
        let part_number: u32 = match c.q("partNumber") {
            "" => 0,
            v => v.parse().ok().filter(|n| (1..=engine::MAX_PARTS).contains(n)).ok_or_else(|| e::INVALID_ARGUMENT.clone())?,
        };
        let range_header = c.header("range").to_string();
        let mut picked = (0u64, 0u64, false);
        let res = blocking(|| {
            self.eng.get_fresh(&c.bucket, &c.key, |o| -> Result<(u64, u64), ReadErr> {
                check_read_conditions(c, o)?;
                let (off, n, ranged) = if part_number > 0 {
                    if part_number as usize > o.segments.len() {
                        return Err(ReadErr::Api(e::INVALID_PART_NUMBER.clone()));
                    }
                    let off: u64 = o.segments[..part_number as usize - 1].iter().map(|s| s.size).sum();
                    (off, o.segments[part_number as usize - 1].size, o.part_count > 0)
                } else if !range_header.is_empty() {
                    match parse_range(&range_header, o.size).map_err(ReadErr::Api)? {
                        Some((off, n)) => (off, n, true),
                        None => (0, o.size, false),
                    }
                } else {
                    (0, o.size, false)
                };
                picked = (off, n, ranged);
                Ok((off, if head { 0 } else { n }))
            })
        });
        let (obj, reader) = match res {
            Ok(v) => v,
            Err(FreshError::Engine(err)) => return Err(err.into()),
            Err(FreshError::Range(o, ReadErr::NotModified)) => {
                let mut r = empty(StatusCode::NOT_MODIFIED);
                set_header(&mut r, "etag", &format!("\"{}\"", o.etag));
                set_header(&mut r, "last-modified", &http_time(o.modified));
                return Ok(r);
            }
            Err(FreshError::Range(o, ReadErr::Api(err))) => {
                if err == e::INVALID_RANGE && !head {
                    let mut r = super::error_response(&err, &c.path, "", false);
                    set_header(&mut r, "content-range", &format!("bytes */{}", o.size));
                    return Ok(r);
                }
                return Err(err);
            }
        };
        let (off, n, ranged) = picked;
        let status = if ranged { StatusCode::PARTIAL_CONTENT } else { StatusCode::OK };
        let mut r = if head { empty(status) } else { stream_response(status, reader) };
        set_header(&mut r, "content-type", &obj.content_type);
        for (k, v) in &obj.headers {
            set_header(&mut r, k, v);
        }
        for (k, v) in &obj.user_meta {
            set_header(&mut r, &format!("x-amz-meta-{k}"), v);
        }
        for h in ["content-type", "content-language", "expires", "cache-control", "content-disposition", "content-encoding"] {
            let v = c.q(&format!("response-{h}"));
            if !v.is_empty() {
                set_header(&mut r, h, v);
            }
        }
        set_header(&mut r, "etag", &format!("\"{}\"", obj.etag));
        set_header(&mut r, "last-modified", &http_time(obj.modified));
        set_header(&mut r, "accept-ranges", "bytes");
        if !obj.tags.is_empty() {
            set_header(&mut r, "x-amz-tagging-count", &obj.tags.len().to_string());
        }
        if part_number > 0 && obj.part_count > 0 {
            set_header(&mut r, "x-amz-mp-parts-count", &obj.part_count.to_string());
        }
        if !ranged && c.header("x-amz-checksum-mode").eq_ignore_ascii_case("enabled") {
            write_checksums(&mut r, &obj.checksums, &obj.checksum_type);
        }
        set_header(&mut r, "content-length", &n.to_string());
        if ranged {
            set_header(&mut r, "content-range", &format!("bytes {}-{}/{}", off, off + n - 1, obj.size));
        }
        Ok(r)
    }

    /// Copies `[range]` of a source object into a new sealed blob, computing `algos` checksums.
    fn copy_bytes(&self, c: &Ctx, src_bucket: &str, src_key: &str, range: &str, algos: &[String]) -> Result<Copied, ApiError> {
        let res = self.eng.get_fresh(src_bucket, src_key, |o| -> Result<(u64, u64), ApiError> {
            check_copy_conditions(c, o)?;
            if range.is_empty() {
                return Ok((0, o.size));
            }
            let spec = range.strip_prefix("bytes=").unwrap_or("");
            let (a, b) = spec.split_once('-').ok_or_else(|| e::INVALID_RANGE.clone())?;
            match (a.parse::<u64>(), b.parse::<u64>()) {
                (Ok(start), Ok(end)) if start <= end && end < o.size => Ok((start, end - start + 1)),
                _ => Err(e::INVALID_RANGE.clone()),
            }
        });
        let (src, mut reader) = match res {
            Ok(v) => v,
            Err(FreshError::Engine(err)) => return Err(err.into()),
            Err(FreshError::Range(_, err)) => return Err(err),
        };
        let mut hashers: Vec<(String, Hasher)> = algos.iter().filter_map(|a| Hasher::new(a).map(|h| (a.clone(), h))).collect();
        let mut blob = self.eng.new_blob()?;
        let mut buf = vec![0u8; 256 << 10];
        loop {
            let n = reader.read(&mut buf).map_err(engine::Error::Io)?;
            if n == 0 {
                break;
            }
            for (_, h) in hashers.iter_mut() {
                h.update(&buf[..n]);
            }
            blob.write(&buf[..n]).map_err(engine::Error::Io)?;
        }
        let md5 = blob.md5();
        let seg = blob.seal()?;
        Ok((src, seg, md5, hashers.iter().map(|(a, h)| (a.clone(), h.base64())).collect()))
    }

    fn copy_object(&self, c: &Ctx) -> R {
        let (src_bucket, src_key) = copy_source(c.header("x-amz-copy-source"))?;
        if !c.auth.cred.allows(&src_bucket) {
            return Err(e::ACCESS_DENIED.clone());
        }
        let directive = match c.header("x-amz-metadata-directive").to_ascii_uppercase().as_str() {
            "" | "COPY" => "COPY",
            "REPLACE" => "REPLACE",
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        let tag_directive = match c.header("x-amz-tagging-directive").to_ascii_uppercase().as_str() {
            "" | "COPY" => "COPY",
            "REPLACE" => "REPLACE",
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        check_acl_headers(c)?;
        let new_tags = parse_tag_header(c.header("x-amz-tagging"))?;
        let new_algo = c.header("x-amz-checksum-algorithm").to_ascii_lowercase();
        if src_bucket == c.bucket && src_key == c.key && directive == "COPY" && tag_directive == "COPY" && new_algo.is_empty() {
            return Err(e::COPY_TO_ITSELF.clone());
        }
        let (src, seg, md5, checksums) = blocking(|| -> Result<_, ApiError> {
            self.eng.head_bucket(&c.bucket)?;
            let meta = self.eng.get_object(&src_bucket, &src_key)?;
            let algos: Vec<String> = if new_algo.is_empty() {
                meta.checksums.iter().filter(|(_, v)| !v.contains('-')).map(|(a, _)| a.clone()).collect()
            } else {
                vec![new_algo.clone()]
            };
            self.copy_bytes(c, &src_bucket, &src_key, "", &algos)
        })?;
        let tags = if tag_directive == "REPLACE" { new_tags } else { src.tags.clone() };
        let (ct, hs, um) = if directive == "REPLACE" { object_headers(c) } else { (src.content_type, src.headers, src.user_meta) };
        let obj = Object {
            key: c.key.clone(),
            size: seg.size,
            etag: hex::encode(&md5),
            modified: engine::now_ms(),
            content_type: ct,
            headers: hs,
            user_meta: um,
            checksums: checksums.clone(),
            checksum_type: "FULL_OBJECT".into(),
            segments: vec![seg],
            part_count: 0,
            tags,
        };
        let (etag, modified) = (obj.etag.clone(), obj.modified);
        blocking(|| self.eng.put_object(&c.bucket, obj, &cond_of(c)))?;
        let mut w = Writer::new("CopyObjectResult");
        w.el("LastModified", iso_time(modified)).el("ETag", format!("\"{etag}\""));
        let mut r = xml_resp(StatusCode::OK, w.finish());
        write_checksums(&mut r, &checksums, "FULL_OBJECT");
        Ok(r)
    }

    // --------------------------------------------------------------------------------- multipart

    fn create_upload(&self, c: &Ctx) -> R {
        let (ct, hs, um) = object_headers(c);
        if meta_too_large(&um) {
            return Err(e::METADATA_TOO_LARGE.clone());
        }
        check_acl_headers(c)?;
        let tags = parse_tag_header(c.header("x-amz-tagging"))?;
        let algo = c.header("x-amz-checksum-algorithm").to_ascii_lowercase();
        if !algo.is_empty() && !checksum::valid(&algo) {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        let mut ctype = c.header("x-amz-checksum-type").to_ascii_uppercase();
        if !algo.is_empty() && ctype.is_empty() {
            ctype = if algo == "crc64nvme" { "FULL_OBJECT" } else { "COMPOSITE" }.into();
        }
        if !ctype.is_empty() && ctype != "COMPOSITE" && ctype != "FULL_OBJECT" {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        if ctype == "FULL_OBJECT" && (algo == "sha1" || algo == "sha256") {
            return Err(e::FULL_OBJECT_SHA.clone());
        }
        let u = blocking(|| {
            self.eng.create_upload(Upload {
                bucket: c.bucket.clone(),
                key: c.key.clone(),
                content_type: ct,
                headers: hs,
                user_meta: um,
                checksum_algo: algo.clone(),
                checksum_type: ctype.clone(),
                tags,
                ..Default::default()
            })
        })?;
        let mut w = Writer::new("InitiateMultipartUploadResult");
        w.el("Bucket", &c.bucket).el("Key", &c.key).el("UploadId", &u.id);
        let mut r = xml_resp(StatusCode::OK, w.finish());
        if !algo.is_empty() {
            set_header(&mut r, "x-amz-checksum-algorithm", &algo.to_ascii_uppercase());
            set_header(&mut r, "x-amz-checksum-type", &ctype);
        }
        Ok(r)
    }

    fn part_number(&self, c: &Ctx) -> Result<u32, ApiError> {
        c.q("partNumber").parse::<u32>().ok().filter(|n| (1..=engine::MAX_PARTS).contains(n)).ok_or_else(|| e::INVALID_PART_ARG.clone())
    }

    async fn upload_part<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let n = self.part_number(c)?;
        let u = blocking(|| self.eng.get_upload(&c.bucket, &c.key, c.q("uploadId")))?;
        let compute: Vec<&str> = [u.checksum_algo.as_str()].into_iter().filter(|a| !a.is_empty()).collect();
        let (seg, md5, checksums) = self.receive(c, body, 5 << 30, &compute).await?;
        let p = Part {
            number: n,
            blob: seg.blob,
            size: seg.size,
            etag: hex::encode(&md5),
            md5,
            checksums: checksums.clone(),
            modified: engine::now_ms(),
        };
        let etag = p.etag.clone();
        blocking(|| self.eng.put_part(&c.bucket, &c.key, &u.id, p))?;
        let mut r = empty(StatusCode::OK);
        set_header(&mut r, "etag", &format!("\"{etag}\""));
        if u.checksum_algo.is_empty() {
            write_checksums(&mut r, &checksums, "");
        } else if let Some(v) = checksums.get(&u.checksum_algo) {
            set_header(&mut r, &format!("x-amz-checksum-{}", u.checksum_algo), v);
        }
        Ok(r)
    }

    fn upload_part_copy(&self, c: &Ctx) -> R {
        let n = self.part_number(c)?;
        let (src_bucket, src_key) = copy_source(c.header("x-amz-copy-source"))?;
        if !c.auth.cred.allows(&src_bucket) {
            return Err(e::ACCESS_DENIED.clone());
        }
        let (etag, modified) = blocking(|| -> Result<_, ApiError> {
            let u = self.eng.get_upload(&c.bucket, &c.key, c.q("uploadId"))?;
            let algos: Vec<String> = [u.checksum_algo.clone()].into_iter().filter(|a| !a.is_empty()).collect();
            let (_, seg, md5, checksums) = self.copy_bytes(c, &src_bucket, &src_key, c.header("x-amz-copy-source-range"), &algos)?;
            let p = Part { number: n, blob: seg.blob, size: seg.size, etag: hex::encode(&md5), md5, checksums, modified: engine::now_ms() };
            let out = (p.etag.clone(), p.modified);
            self.eng.put_part(&c.bucket, &c.key, &u.id, p)?;
            Ok(out)
        })?;
        let mut w = Writer::new("CopyPartResult");
        w.el("LastModified", iso_time(modified)).el("ETag", format!("\"{etag}\""));
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    fn list_parts(&self, c: &Ctx) -> R {
        let after: u32 = c.q("part-number-marker").parse().unwrap_or(0);
        let max = match c.q("max-parts") {
            "" => 1000,
            v => v.parse::<usize>().map_err(|_| e::INVALID_ARGUMENT.clone())?.min(1000),
        };
        let u = blocking(|| self.eng.get_upload(&c.bucket, &c.key, c.q("uploadId")))?;
        let (parts, truncated) = blocking(|| self.eng.list_parts(&c.bucket, &c.key, &u.id, after, max))?;
        let mut w = Writer::new("ListPartsResult");
        w.el("Bucket", &c.bucket).el("Key", &c.key).el("UploadId", &u.id);
        self.owner_xml(c, &mut w, "Initiator");
        self.owner_xml(c, &mut w, "Owner");
        w.el("StorageClass", "STANDARD")
            .el("PartNumberMarker", after.to_string())
            .el("NextPartNumberMarker", parts.last().map(|p| p.number).unwrap_or(0).to_string())
            .el("MaxParts", max.to_string())
            .el("IsTruncated", truncated.to_string())
            .opt("ChecksumAlgorithm", u.checksum_algo.to_ascii_uppercase());
        for p in &parts {
            w.open("Part")
                .el("PartNumber", p.number.to_string())
                .el("LastModified", iso_time(p.modified))
                .el("ETag", format!("\"{}\"", p.etag))
                .el("Size", p.size.to_string());
            checksum_elements(&mut w, &p.checksums);
            w.close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    async fn complete_upload<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let id = c.q("uploadId").to_string();
        let doc = self.read_small(c, body, 8 << 20).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "CompleteMultipartUpload")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        let requested: Vec<(u32, String, BTreeMap<String, String>)> = root
            .all("Part")
            .map(|p| {
                let given = [
                    ("crc32", "ChecksumCRC32"),
                    ("crc32c", "ChecksumCRC32C"),
                    ("crc64nvme", "ChecksumCRC64NVME"),
                    ("sha1", "ChecksumSHA1"),
                    ("sha256", "ChecksumSHA256"),
                ]
                .iter()
                .filter_map(|(a, tag)| Some((a.to_string(), p.text_of(tag))).filter(|(_, v)| !v.is_empty()))
                .collect();
                (p.text_of("PartNumber").parse().unwrap_or(0), p.text_of("ETag"), given)
            })
            .collect();
        if requested.is_empty() {
            return Err(e::MALFORMED_XML.clone());
        }
        let obj = blocking(|| -> Result<Object, ApiError> {
            let u = self.eng.get_upload(&c.bucket, &c.key, &id)?;
            let (all, _) = self.eng.list_parts(&c.bucket, &c.key, &id, 0, engine::MAX_PARTS as usize)?;
            let have: BTreeMap<u32, &Part> = all.iter().map(|p| (p.number, p)).collect();
            let mut chosen: Vec<Part> = vec![];
            for (n, _, given) in &requested {
                let Some(p) = have.get(n) else { return Err(e::INVALID_PART.clone()) };
                if given.iter().any(|(a, v)| p.checksums.get(a).is_some_and(|have| have != v)) {
                    return Err(e::INVALID_PART.clone());
                }
                chosen.push((*p).clone());
            }
            // The object's checksum: of the part checksums (composite) or over the whole object.
            let mut checksums = BTreeMap::new();
            if !u.checksum_algo.is_empty() {
                let a = u.checksum_algo.as_str();
                let mut h = Hasher::new(a).ok_or_else(|| e::INVALID_ARGUMENT.clone())?;
                if u.checksum_type == "COMPOSITE" {
                    for p in &chosen {
                        let raw = p
                            .checksums
                            .get(a)
                            .and_then(|v| B64.decode(v).ok())
                            .filter(|r| !r.is_empty())
                            .ok_or_else(|| e::INVALID_PART.clone())?;
                        h.update(&raw);
                    }
                    checksums.insert(a.to_string(), format!("{}-{}", h.base64(), chosen.len()));
                } else {
                    let segs: Vec<Segment> = chosen.iter().map(|p| Segment { blob: p.blob.clone(), size: p.size }).collect();
                    let size = segs.iter().map(|s| s.size).sum();
                    let mut reader = self.eng.reader(&Object { segments: segs, ..Default::default() }, 0, size)?;
                    let mut buf = vec![0u8; 256 << 10];
                    loop {
                        let n = reader.read(&mut buf).map_err(engine::Error::Io)?;
                        if n == 0 {
                            break;
                        }
                        h.update(&buf[..n]);
                    }
                    checksums.insert(a.to_string(), h.base64());
                    let want = c.header(&format!("x-amz-checksum-{a}"));
                    if !want.is_empty() && want != h.base64() {
                        return Err(e::CHECKSUM_MISMATCH.clone());
                    }
                }
            }
            let want: Vec<CompletePart> = requested.iter().map(|(n, etag, _)| CompletePart { number: *n, etag: etag.clone() }).collect();
            let ctype = u.checksum_type.clone();
            Ok(self.eng.complete_upload(&c.bucket, &c.key, &id, &want, &cond_of(c), |_, parts| {
                if parts.len() != chosen.len() || parts.iter().zip(&chosen).any(|(a, b)| a.blob != b.blob) {
                    return Err(engine::Error::Conflict); // a part was re-uploaded while completing
                }
                Ok((checksums, ctype))
            })?)
        })?;
        let mut w = Writer::new("CompleteMultipartUploadResult");
        w.el("Location", format!("/{}/{}", c.bucket, c.key))
            .el("Bucket", &c.bucket)
            .el("Key", &c.key)
            .el("ETag", format!("\"{}\"", obj.etag));
        checksum_elements(&mut w, &obj.checksums);
        if !obj.checksums.is_empty() {
            w.el("ChecksumType", &obj.checksum_type);
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }
}

fn checksum_elements(w: &mut Writer, cs: &BTreeMap<String, String>) {
    for (a, v) in cs {
        let tag = match a.as_str() {
            "crc32" => "ChecksumCRC32",
            "crc32c" => "ChecksumCRC32C",
            "crc64nvme" => "ChecksumCRC64NVME",
            "sha1" => "ChecksumSHA1",
            "sha256" => "ChecksumSHA256",
            _ => continue,
        };
        w.el(tag, v);
    }
}

fn write_checksums(r: &mut Resp, cs: &BTreeMap<String, String>, ctype: &str) {
    for (a, v) in cs {
        set_header(r, &format!("x-amz-checksum-{a}"), v);
    }
    if !cs.is_empty() && !ctype.is_empty() {
        set_header(r, "x-amz-checksum-type", ctype);
    }
}

/// Parses x-amz-copy-source ("/bucket/key", URL-encoded, optional ?versionId).
fn copy_source(v: &str) -> Result<(String, String), ApiError> {
    let v = v.split('?').next().unwrap_or("");
    let dec = String::from_utf8(super::auth::percent_decode(v)).map_err(|_| e::INVALID_COPY_SOURCE.clone())?;
    let dec = dec.trim_start_matches('/');
    match dec.split_once('/') {
        Some((b, k)) if !b.is_empty() && !k.is_empty() => Ok((b.to_string(), k.to_string())),
        _ => Err(e::INVALID_COPY_SOURCE.clone()),
    }
}

enum ReadErr {
    NotModified,
    Api(ApiError),
}

fn parse_http_ms(v: &str) -> Option<u64> {
    httpdate::parse_http_date(v).ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_millis() as u64)
}

fn check_read_conditions(c: &Ctx, o: &Object) -> Result<(), ReadErr> {
    let modified = o.modified / 1000 * 1000;
    let if_match = c.header("if-match");
    if !if_match.is_empty() {
        if !etag_header_matches(if_match, &o.etag) {
            return Err(ReadErr::Api(e::PRECONDITION.clone()));
        }
    } else if let Some(t) = parse_http_ms(c.header("if-unmodified-since")) {
        if modified > t {
            return Err(ReadErr::Api(e::PRECONDITION.clone()));
        }
    }
    let if_none = c.header("if-none-match");
    if !if_none.is_empty() {
        if etag_header_matches(if_none, &o.etag) {
            return Err(ReadErr::NotModified);
        }
    } else if let Some(t) = parse_http_ms(c.header("if-modified-since")) {
        if modified <= t {
            return Err(ReadErr::NotModified);
        }
    }
    Ok(())
}

fn check_copy_conditions(c: &Ctx, o: &Object) -> Result<(), ApiError> {
    let modified = o.modified / 1000 * 1000;
    let h = |n: &str| c.header(n).to_string();
    let m = h("x-amz-copy-source-if-match");
    if !m.is_empty() && !etag_header_matches(&m, &o.etag) {
        return Err(e::PRECONDITION.clone());
    }
    let nm = h("x-amz-copy-source-if-none-match");
    if !nm.is_empty() && etag_header_matches(&nm, &o.etag) {
        return Err(e::PRECONDITION.clone());
    }
    if parse_http_ms(&h("x-amz-copy-source-if-unmodified-since")).is_some_and(|t| modified > t) {
        return Err(e::PRECONDITION.clone());
    }
    if parse_http_ms(&h("x-amz-copy-source-if-modified-since")).is_some_and(|t| modified <= t) {
        return Err(e::PRECONDITION.clone());
    }
    Ok(())
}

/// Parses a single "bytes=" range. Ok(None): the header is ignored and the whole object is served.
pub(crate) fn parse_range(h: &str, size: u64) -> Result<Option<(u64, u64)>, ApiError> {
    let Some(spec) = h.strip_prefix("bytes=") else { return Ok(None) };
    if spec.contains(',') {
        return Ok(None);
    }
    let Some((a, b)) = spec.trim().split_once('-') else { return Ok(None) };
    if a.is_empty() {
        let Ok(k) = b.parse::<u64>() else { return Ok(None) };
        if k == 0 || size == 0 {
            return Err(e::INVALID_RANGE.clone());
        }
        let k = k.min(size);
        return Ok(Some((size - k, k)));
    }
    let Ok(start) = a.parse::<u64>() else { return Ok(None) };
    let mut end = size.saturating_sub(1);
    if !b.is_empty() {
        match b.parse::<u64>() {
            Ok(e) if e >= start => end = end.min(e),
            _ => return Ok(None),
        }
    }
    if start >= size {
        return Err(e::INVALID_RANGE.clone());
    }
    Ok(Some((start, end - start + 1)))
}

/// Streams an object's bytes from a blocking reader thread.
fn stream_response(status: StatusCode, mut reader: crate::engine::ObjectReader) -> Resp {
    let (tx, rx) = tokio::sync::mpsc::channel::<io::Result<Frame<Bytes>>>(4);
    tokio::task::spawn_blocking(move || {
        let mut buf = vec![0u8; 256 << 10];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => return,
                Ok(n) => {
                    if tx.blocking_send(Ok(Frame::data(Bytes::copy_from_slice(&buf[..n])))).is_err() {
                        return; // the client went away
                    }
                }
                Err(err) => {
                    let _ = tx.blocking_send(Err(err));
                    return;
                }
            }
        }
    });
    let stream = futures_util::stream::unfold(rx, |mut rx| async move { rx.recv().await.map(|item| (item, rx)) });
    let mut r = Response::new(BodyExt::boxed(StreamBody::new(stream)));
    *r.status_mut() = status;
    r
}
