//! WebDAV (RFC 4918, class 1) at `/-/dav/<bucket>/<path>`, for rclone, davfs2, cadaver, WinSCP and
//! other WebDAV clients.
//!
//! Methods: OPTIONS, PROPFIND (Depth 0 and 1; infinity is refused, as RFC 4918 allows), GET/HEAD (a
//! collection is shown as the file API's HTML listing), PUT, DELETE, MKCOL, COPY, MOVE, PROPPATCH (dead
//! properties are not stored: every change is answered 403 Forbidden in the multistatus). Locking
//! (class 2) is not offered: LOCK and UNLOCK are refused, so clients that insist on locks (macOS Finder)
//! mount read-only.
//!
//! Collections are key prefixes; MKCOL writes an empty `name/` marker object, as S3 consoles do.
//! Every item is authorized as its S3 operation (ListObjectsV2, GetObject, PutObject, CopyObject,
//! DeleteObject, CreateBucket) with the same policies, quotas, encryption, events and audit trail as
//! the S3 API. A collection DELETE, COPY or MOVE first authorizes every key it would touch and changes
//! nothing if any is refused; it is limited to `MAX_TREE` keys (larger trees: use the S3 API). A MOVE
//! is a copy, then a delete of the source, so it is not atomic. Sign-in is the file API's (SigV4,
//! presigned URLs, or HTTP Basic when `CAIRN_STORE_FILES_BASIC_AUTH` allows it).

use std::collections::BTreeSet;

use bytes::Bytes;
use http_body_util::Empty;
use hyper::header::HeaderValue;
use hyper::{Method, StatusCode};

use super::auth::{self, aws_encode};
use super::errors::{self as e, ApiError};
use super::xml;
use super::{accesslog, full, http_time, iso_time, Ctx, Dispatched, Resp, Server};
use crate::engine::{self, ListOptions, Object};

const PREFIX: &str = "/-/dav";
const MAX_TREE: usize = 10_000;
const DAV_METHODS: &str = "OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, DELETE, MKCOL, COPY, MOVE";

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

/// A resource in a PROPFIND answer.
struct Res {
    href: String,
    name: String,
    collection: bool,
    size: u64,
    modified: u64,
    etag: String,
    content_type: String,
}

fn href(bucket: &str, key: &str) -> String {
    let mut h = format!("{PREFIX}/");
    if !bucket.is_empty() {
        h.push_str(&aws_encode(bucket.as_bytes(), false));
        h.push('/');
        h.push_str(&aws_encode(key.as_bytes(), true));
    }
    h
}

fn display_name(bucket: &str, key: &str) -> String {
    let k = key.trim_end_matches('/');
    if k.is_empty() {
        bucket.to_string()
    } else {
        k.rsplit('/').next().unwrap_or(k).to_string()
    }
}

fn file_res(bucket: &str, o: &Object) -> Res {
    Res {
        href: href(bucket, &o.key),
        name: display_name(bucket, &o.key),
        collection: false,
        size: o.size,
        modified: o.modified,
        etag: o.etag.clone(),
        content_type: if o.content_type.is_empty() { "application/octet-stream".into() } else { o.content_type.clone() },
    }
}

fn dir_res(bucket: &str, prefix: &str, modified: u64) -> Res {
    Res {
        href: href(bucket, prefix),
        name: display_name(bucket, prefix),
        collection: true,
        size: 0,
        modified,
        etag: String::new(),
        content_type: String::new(),
    }
}

/// Known live properties; None (allprop) returns all of them.
const PROPS: [&str; 7] =
    ["displayname", "resourcetype", "getcontentlength", "getlastmodified", "getetag", "getcontenttype", "creationdate"];

fn multistatus(items: &[Res], wanted: &Option<BTreeSet<String>>) -> String {
    let mut out = String::from("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<D:multistatus xmlns:D=\"DAV:\">");
    for r in items {
        out.push_str(&format!("<D:response><D:href>{}</D:href><D:propstat><D:prop>", xml::escape(&r.href)));
        let want = |p: &str| wanted.as_ref().is_none_or(|w| w.contains(p));
        if want("displayname") {
            out.push_str(&format!("<D:displayname>{}</D:displayname>", xml::escape(&r.name)));
        }
        if want("resourcetype") {
            out.push_str(if r.collection { "<D:resourcetype><D:collection/></D:resourcetype>" } else { "<D:resourcetype/>" });
        }
        if r.modified > 0 {
            if want("getlastmodified") {
                out.push_str(&format!("<D:getlastmodified>{}</D:getlastmodified>", http_time(r.modified)));
            }
            if want("creationdate") {
                out.push_str(&format!("<D:creationdate>{}</D:creationdate>", iso_time(r.modified)));
            }
        }
        if !r.collection {
            if want("getcontentlength") {
                out.push_str(&format!("<D:getcontentlength>{}</D:getcontentlength>", r.size));
            }
            if want("getetag") {
                out.push_str(&format!("<D:getetag>\"{}\"</D:getetag>", xml::escape(&r.etag)));
            }
            if want("getcontenttype") {
                out.push_str(&format!("<D:getcontenttype>{}</D:getcontenttype>", xml::escape(&r.content_type)));
            }
        }
        out.push_str("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>");
    }
    out.push_str("</D:multistatus>\n");
    out
}

fn xml_status(status: StatusCode, doc: String) -> Resp {
    let mut r = full(status, doc);
    r.headers_mut().insert("content-type", HeaderValue::from_static("application/xml; charset=utf-8"));
    r
}

fn status_only(status: StatusCode) -> Resp {
    full(status, Bytes::new())
}

/// Splits a decoded `/-/dav/...` path into (bucket, key).
fn split(decoded: &str) -> Option<(String, String)> {
    let rest = decoded.strip_prefix(PREFIX)?;
    if !(rest.is_empty() || rest.starts_with('/')) {
        return None;
    }
    let p = rest.strip_prefix('/').unwrap_or(rest);
    Some(match p.split_once('/') {
        Some((b, k)) => (b.to_string(), k.to_string()),
        None => (p.to_string(), String::new()),
    })
}

impl Server {
    pub(crate) fn is_dav_path(decoded: &str) -> bool {
        decoded == PREFIX || decoded.starts_with("/-/dav/")
    }

    /// Authorizes `op` on bucket/key with the request's credentials (a copy of its context).
    async fn may(&self, c: &Ctx, op: &'static str, bucket: &str, key: &str, list_prefix: Option<&str>) -> Result<(), ApiError> {
        let mut x = c.clone();
        x.op = op;
        x.bucket = bucket.to_string();
        x.key = key.to_string();
        x.query.retain(|k, _| super::files::presign_param(k));
        if let Some(p) = list_prefix {
            x.query.insert("list-type".into(), "2".into());
            x.query.insert("prefix".into(), p.to_string());
            x.query.insert("delimiter".into(), "/".into());
        }
        self.authorize_request(&x).await
    }

    /// Every key under a prefix (at most MAX_TREE).
    fn tree(&self, bucket: &str, prefix: &str) -> Result<Vec<Object>, ApiError> {
        let mut out = vec![];
        let mut after = String::new();
        loop {
            let page = blocking(|| {
                self.eng
                    .list(bucket, &ListOptions { prefix: prefix.into(), delimiter: String::new(), after: after.clone(), max_keys: 1000 })
            })?;
            out.extend(page.objects);
            if out.len() > MAX_TREE {
                return Err(e::DAV_TREE_TOO_LARGE.clone());
            }
            if !page.truncated {
                return Ok(out);
            }
            after = page.next_after;
        }
    }

    /// Does a collection exist at `prefix` (ending in '/')? A bucket root always does.
    fn collection_exists(&self, bucket: &str, prefix: &str) -> Result<bool, ApiError> {
        if prefix.is_empty() {
            blocking(|| self.eng.head_bucket(bucket))?;
            return Ok(true);
        }
        let page = blocking(|| self.eng.list(bucket, &ListOptions { prefix: prefix.into(), max_keys: 1, ..Default::default() }))?;
        Ok(!page.objects.is_empty() || !page.common_prefixes.is_empty())
    }

    fn object(&self, bucket: &str, key: &str) -> Result<Option<Object>, ApiError> {
        match blocking(|| self.eng.get_object(bucket, key)) {
            Ok(o) if !o.delete_marker => Ok(Some(o)),
            Ok(_) | Err(engine::Error::NoSuchKey) => Ok(None),
            Err(err) => Err(err.into()),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn dispatch_dav<B>(
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
        let (bucket, key) = split(decoded).unwrap_or_default();
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
        let head = parts.method == Method::HEAD;
        if parts.method == Method::OPTIONS {
            if parts.headers.contains_key("access-control-request-method") {
                return ("PreflightCORS", self.preflight(&bucket, &parts.headers), None, info);
            }
            let mut r = status_only(StatusCode::OK);
            let h = r.headers_mut();
            h.insert("dav", HeaderValue::from_static("1"));
            h.insert("allow", HeaderValue::from_static(DAV_METHODS));
            h.insert("ms-author-via", HeaderValue::from_static("DAV"));
            return ("DavOptions", Ok(r), None, info);
        }
        let cors = parts.headers.contains_key("origin").then(|| (bucket.clone(), parts.method.to_string(), parts.headers.clone()));
        let anonymous = auth::is_anonymous(&parts.headers, &raw_query);
        let fail =
            |op: &'static str, err: ApiError, info| -> Dispatched { (op, Ok(self.files_error(&err, head, anonymous)), cors.clone(), info) };
        let auth = match self.plain_auth(&parts, &raw_path, &raw_query, &host) {
            Ok(a) => a,
            Err(err) => return fail("auth", err, info),
        };
        info.principal = auth.cred.access_key.clone();
        let mut query = std::collections::BTreeMap::new();
        for pair in raw_query.split('&').filter(|p| !p.is_empty()) {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            let k = auth::query_decode(k);
            if !super::files::presign_param(&k) {
                return fail("route", e::FILES_PARAMETER.clone(), info);
            }
            query.insert(k, auth::query_decode(v));
        }
        let content_length = parts.headers.get("content-length").and_then(|v| v.to_str().ok()?.parse().ok());
        let mut c = Ctx {
            method: parts.method.clone(),
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
        if c.key.len() > 1024 {
            return fail("route", e::KEY_TOO_LONG.clone(), info);
        }
        let res: Result<(&'static str, Resp), ApiError> = match c.method.as_str() {
            "PROPFIND" => self.propfind(&c, body).await.map(|r| ("DavPropfind", r)),
            "PROPPATCH" => self.proppatch(&c, body).await.map(|r| ("DavProppatch", r)),
            "MKCOL" => self.mkcol(&mut c, body).await.map(|r| ("DavMkcol", r)),
            "COPY" | "MOVE" => {
                self.copy_move(&c, &host).await.map(|r| (if c.method.as_str() == "COPY" { "DavCopy" } else { "DavMove" }, r))
            }
            "DELETE" => self.dav_delete(&c).await.map(|r| ("DavDelete", r)),
            "GET" | "HEAD" if bucket.is_empty() || key.is_empty() || key.ends_with('/') => {
                let k = key.clone();
                self.files_dir(&mut c, &k, "", 1000, true).await.map(|r| ("DavGetCollection", r))
            }
            "GET" | "HEAD" | "PUT" if !key.ends_with('/') && !bucket.is_empty() && !key.is_empty() => {
                let mut x = c.clone();
                x.query.retain(|k, _| super::files::presign_param(k));
                let r = self.route(&mut x, body).await.map(|mut r| {
                    if !matches!(x.method, Method::PUT) {
                        let h = r.headers_mut();
                        h.insert("content-security-policy", HeaderValue::from_static("sandbox"));
                        h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
                    } else if r.status() == StatusCode::OK {
                        *r.status_mut() = StatusCode::CREATED;
                    }
                    r
                });
                c.op = x.op;
                r.map(|r| (x.op, r))
            }
            "PUT" => Err(e::METHOD_NOT_ALLOWED.clone()), // onto a collection
            _ => Err(e::METHOD_NOT_ALLOWED.clone()),     // LOCK, UNLOCK (no class 2), POST, ...
        };
        match res {
            Ok((op, r)) => (op, Ok(r), cors, info),
            Err(err) => {
                self.audit_denial(&c, &err);
                fail(if c.op == "unknown" { "Dav" } else { c.op }, err, info)
            }
        }
    }

    async fn propfind<B>(&self, c: &Ctx, body: &mut B) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        // Depth infinity (also a missing header) is served for trees of up to MAX_TREE keys.
        let depth = match c.header("depth") {
            "0" => 0,
            "1" => 1,
            "" | "infinity" | "Infinity" => 2,
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        let finite_only = || {
            let doc = "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<D:error xmlns:D=\"DAV:\"><D:propfind-finite-depth/></D:error>\n";
            xml_status(StatusCode::FORBIDDEN, doc.into())
        };
        let doc = self.read_small(c, &mut *body, 64 << 10).await?;
        let text = String::from_utf8_lossy(&doc);
        let wanted: Option<BTreeSet<String>> = if text.trim().is_empty() {
            None
        } else {
            let root = xml::parse(&text).filter(|r| r.name == "propfind").ok_or_else(|| e::MALFORMED_XML.clone())?;
            root.child("prop").map(|p| p.children.iter().map(|x| x.name.clone()).filter(|n| PROPS.contains(&n.as_str())).collect())
        };
        let mut items = vec![];
        if c.bucket.is_empty() {
            self.may(c, "ListBuckets", "", "", None).await?;
            items.push(dir_res("", "", 0));
            if depth >= 1 {
                let visible = |name: &str| c.auth.cred.admin || c.auth.cred.policy.is_some() || c.auth.cred.allows(name);
                for b in blocking(|| self.eng.list_buckets())?.into_iter().filter(|b| visible(&b.name)) {
                    items.push(dir_res(&b.name, "", b.created));
                }
            }
        } else {
            let (bucket, key) = (c.bucket.as_str(), c.key.as_str());
            let prefix = if key.is_empty() || key.ends_with('/') {
                Some(key.to_string())
            } else {
                // A file, or a collection named without its trailing slash.
                self.may(c, "HeadObject", bucket, key, None).await?;
                match self.object(bucket, key)? {
                    Some(o) => {
                        items.push(file_res(bucket, &o));
                        None
                    }
                    None => Some(format!("{key}/")),
                }
            };
            if let Some(p) = prefix {
                self.may(c, "ListObjectsV2", bucket, "", Some(&p)).await?;
                if !self.collection_exists(bucket, &p)? {
                    return Err(e::NO_SUCH_KEY.clone());
                }
                items.push(dir_res(bucket, &p, 0));
                if depth == 2 {
                    let keys = match self.tree(bucket, &p) {
                        Ok(k) => k,
                        Err(err) if err.code == e::DAV_TREE_TOO_LARGE.code => return Ok(finite_only()),
                        Err(err) => return Err(err),
                    };
                    let mut dirs = BTreeSet::new();
                    for o in &keys {
                        let rel = &o.key[p.len()..];
                        let mut at = 0;
                        while let Some(i) = rel[at..].find('/') {
                            at += i + 1;
                            dirs.insert(format!("{p}{}", &rel[..at]));
                        }
                    }
                    items.extend(dirs.iter().map(|d| dir_res(bucket, d, 0)));
                    items.extend(keys.iter().filter(|o| !o.key.ends_with('/')).map(|o| file_res(bucket, o)));
                } else if depth == 1 {
                    let mut after = String::new();
                    loop {
                        let page = blocking(|| {
                            self.eng.list(
                                bucket,
                                &ListOptions { prefix: p.clone(), delimiter: "/".into(), after: after.clone(), max_keys: 1000 },
                            )
                        })?;
                        for cp in &page.common_prefixes {
                            items.push(dir_res(bucket, cp, 0));
                        }
                        for o in page.objects.iter().filter(|o| o.key != p) {
                            items.push(file_res(bucket, o));
                        }
                        if items.len() > MAX_TREE * 10 {
                            return Err(e::DAV_TREE_TOO_LARGE.clone());
                        }
                        if !page.truncated {
                            break;
                        }
                        after = page.next_after;
                    }
                }
            }
        }
        Ok(xml_status(StatusCode::MULTI_STATUS, multistatus(&items, &wanted)))
    }

    /// Dead properties are not stored: each requested change is answered 403.
    async fn proppatch<B>(&self, c: &Ctx, body: &mut B) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        self.may(c, "PutObjectTagging", &c.bucket, &c.key, None).await?;
        let doc = self.read_small(c, &mut *body, 64 << 10).await?;
        let root =
            xml::parse(&String::from_utf8_lossy(&doc)).filter(|r| r.name == "propertyupdate").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let mut props = String::new();
        for action in root.children.iter().filter(|a| a.name == "set" || a.name == "remove") {
            for p in action.child("prop").map(|p| p.children.as_slice()).unwrap_or_default() {
                props.push_str(&format!(
                    "<D:{}/>",
                    p.name.chars().filter(|ch| ch.is_alphanumeric() || *ch == '-' || *ch == '_').collect::<String>()
                ));
            }
        }
        let doc = format!(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<D:multistatus xmlns:D=\"DAV:\"><D:response><D:href>{}</D:href>\
             <D:propstat><D:prop>{props}</D:prop><D:status>HTTP/1.1 403 Forbidden</D:status></D:propstat></D:response></D:multistatus>\n",
            xml::escape(&href(&c.bucket, &c.key))
        );
        Ok(xml_status(StatusCode::MULTI_STATUS, doc))
    }

    async fn mkcol<B>(&self, c: &mut Ctx, body: &mut B) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        if c.content_length.is_some_and(|n| n > 0) {
            return Err(e::DAV_MKCOL_BODY.clone());
        }
        if c.bucket.is_empty() {
            return Err(e::METHOD_NOT_ALLOWED.clone());
        }
        let mut x = c.clone();
        x.method = Method::PUT;
        x.query.retain(|k, _| super::files::presign_param(k));
        if c.key.is_empty() || c.key == "/" {
            x.key = String::new(); // a new bucket (CreateBucket: admin keys, or a key policy granting it)
            self.route(&mut x, &mut *body).await?;
            c.op = x.op;
            return Ok(status_only(StatusCode::CREATED));
        }
        let marker = if c.key.ends_with('/') { c.key.clone() } else { format!("{}/", c.key) };
        self.may(c, "ListObjectsV2", &c.bucket, "", Some(&marker)).await?;
        if self.collection_exists(&c.bucket, &marker)? || self.object(&c.bucket, marker.trim_end_matches('/'))?.is_some() {
            return Err(e::METHOD_NOT_ALLOWED.clone());
        }
        let parent = marker.trim_end_matches('/').rsplit_once('/').map(|(p, _)| format!("{p}/")).unwrap_or_default();
        if !self.collection_exists(&c.bucket, &parent)? {
            return Err(e::DAV_CONFLICT.clone());
        }
        x.key = marker;
        x.headers.remove("content-type");
        x.headers.insert("content-length", HeaderValue::from_static("0")); // the marker is empty
        x.content_length = Some(0);
        self.route(&mut x, &mut *body).await?;
        c.op = x.op;
        Ok(status_only(StatusCode::CREATED))
    }

    /// The source of a DELETE, COPY or MOVE: one object, or every key of a collection.
    async fn resolve(&self, c: &Ctx) -> Result<(Vec<Object>, Option<String>), ApiError> {
        if c.bucket.is_empty() || c.key.is_empty() {
            return Err(e::DAV_BUCKET_ROOT.clone()); // whole buckets: the S3 API
        }
        if !c.key.ends_with('/') {
            self.may(c, "HeadObject", &c.bucket, &c.key, None).await?;
            if let Some(o) = self.object(&c.bucket, &c.key)? {
                return Ok((vec![o], None));
            }
        }
        let prefix = if c.key.ends_with('/') { c.key.clone() } else { format!("{}/", c.key) };
        self.may(c, "ListObjectsV2", &c.bucket, "", Some(&prefix)).await?;
        let keys = self.tree(&c.bucket, &prefix)?;
        if keys.is_empty() {
            return Err(e::NO_SUCH_KEY.clone());
        }
        Ok((keys, Some(prefix)))
    }

    async fn delete_key(&self, c: &Ctx, bucket: &str, key: &str) -> Result<(), ApiError> {
        let mut x = c.clone();
        x.method = Method::DELETE;
        x.bucket = bucket.to_string();
        x.key = key.to_string();
        x.query.retain(|k, _| super::files::presign_param(k));
        self.route(&mut x, Empty::<Bytes>::new()).await.map(|_| ())
    }

    async fn dav_delete(&self, c: &Ctx) -> Result<Resp, ApiError> {
        let (objs, _) = self.resolve(c).await?;
        for o in &objs {
            self.may(c, "DeleteObject", &c.bucket, &o.key, None).await?; // all or nothing
        }
        for o in &objs {
            self.delete_key(c, &c.bucket, &o.key).await?;
        }
        Ok(status_only(StatusCode::NO_CONTENT))
    }

    async fn copy_move(&self, c: &Ctx, host: &str) -> Result<Resp, ApiError> {
        let mv = c.method.as_str() == "MOVE";
        let dest = c.header("destination");
        let path = match dest.split_once("://") {
            Some((_, rest)) => {
                let (h, p) = rest.split_once('/').map(|(h, p)| (h, format!("/{p}"))).unwrap_or((rest, "/".into()));
                if !h.eq_ignore_ascii_case(host) {
                    return Err(e::DAV_BAD_GATEWAY.clone()); // another server
                }
                p
            }
            None => dest.to_string(),
        };
        let path = path.split('?').next().unwrap_or("").to_string();
        let decoded = String::from_utf8(auth::percent_decode(&path)).map_err(|_| e::INVALID_ARGUMENT.clone())?;
        let (db, dk) = split(&decoded).ok_or_else(|| e::DAV_BAD_GATEWAY.clone())?;
        if db.is_empty() || dk.is_empty() {
            return Err(e::DAV_BUCKET_ROOT.clone());
        }
        let overwrite = !c.header("overwrite").eq_ignore_ascii_case("F");
        let (objs, prefix) = self.resolve(c).await?;
        // (source key, destination key)
        let pairs: Vec<(String, String)> = match &prefix {
            None => vec![(objs[0].key.clone(), dk.trim_end_matches('/').to_string())],
            Some(p) => {
                let dp = if dk.ends_with('/') { dk.clone() } else { format!("{dk}/") };
                if db == c.bucket && dp.starts_with(p.as_str()) {
                    return Err(e::DAV_CONFLICT.clone()); // into itself
                }
                objs.iter().map(|o| (o.key.clone(), format!("{dp}{}", &o.key[p.len()..]))).collect()
            }
        };
        if db == c.bucket && pairs.iter().any(|(s, d)| s == d) {
            return Err(e::DAV_SAME.clone());
        }
        let mut existed = false;
        for (_, d) in &pairs {
            if self.object(&db, d)?.is_some() {
                existed = true;
            }
        }
        if existed && !overwrite {
            return Err(e::PRECONDITION.clone());
        }
        for (s, d) in &pairs {
            self.may(c, "GetObject", &c.bucket, s, None).await?;
            self.may(c, "PutObject", &db, d, None).await?;
            if mv {
                self.may(c, "DeleteObject", &c.bucket, s, None).await?;
            }
        }
        for (s, d) in &pairs {
            let mut x = c.clone();
            x.method = Method::PUT;
            x.bucket = db.clone();
            x.key = d.clone();
            x.query.retain(|k, _| super::files::presign_param(k));
            let src = format!("/{}/{}", aws_encode(c.bucket.as_bytes(), false), aws_encode(s.as_bytes(), true));
            x.headers.insert("x-amz-copy-source", HeaderValue::from_str(&src).map_err(|_| e::INVALID_ARGUMENT.clone())?);
            for h in ["content-length", "content-type", "destination", "overwrite", "depth"] {
                x.headers.remove(h);
            }
            self.route(&mut x, Empty::<Bytes>::new()).await?;
        }
        if mv {
            for (s, _) in &pairs {
                self.delete_key(c, &c.bucket, s).await?;
            }
        }
        Ok(status_only(if existed { StatusCode::NO_CONTENT } else { StatusCode::CREATED }))
    }
}
