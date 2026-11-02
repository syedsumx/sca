//! WebDAV (RFC 4918, class 1) at `/-/dav/<bucket>/<path>`, for rclone, davfs2, cadaver, WinSCP and
//! other WebDAV clients.
//!
//! Methods: OPTIONS, PROPFIND (Depth 0 and 1; infinity is refused, as RFC 4918 allows), GET/HEAD (a
//! collection is shown as the file API's HTML listing), PUT, DELETE, MKCOL, COPY, MOVE, PROPPATCH (dead
//! properties are kept in the object's user metadata; live ones are protected), LOCK and UNLOCK
//! (class 2: exclusive and shared write locks, depth 0 or infinity, kept in the store's metadata so every
//! node sees them; only the key that took a lock can use, refresh or release it; WebDAV writes to a
//! locked resource without its token get 423 Locked).
//!
//! Collections are key prefixes; MKCOL writes an empty `name/` marker object, as S3 consoles do.
//! Every item is authorized as its S3 operation (ListObjectsV2, GetObject, PutObject, CopyObject,
//! DeleteObject, CreateBucket) with the same policies, quotas, encryption, events and audit trail as
//! the S3 API. A collection DELETE, COPY or MOVE first authorizes every key it would touch and changes
//! nothing if any is refused; it is limited to `MAX_TREE` keys (larger trees: use the S3 API). A MOVE
//! within an unversioned bucket is one transaction (every key moves, data untouched, or none does);
//! across buckets or in a versioned bucket it is a copy, then a delete of the source. Sign-in is the file API's (SigV4,
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
const DAV_METHODS: &str = "OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, DELETE, MKCOL, COPY, MOVE, LOCK, UNLOCK";
/// Lock timeouts: the default and the longest a client may ask for (it refreshes to keep a lock).
const LOCK_DEFAULT_S: u64 = 600;
const LOCK_MAX_S: u64 = 3600;

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
    dead: std::collections::BTreeMap<String, String>,
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
        dead: dead_props(Some(o)),
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
        dead: Default::default(),
    }
}

/// Known live properties; None (allprop) returns all of them.
const PROPS: [&str; 9] = [
    "displayname",
    "resourcetype",
    "getcontentlength",
    "getlastmodified",
    "getetag",
    "getcontenttype",
    "creationdate",
    "supportedlock",
    "lockdiscovery",
];

const SUPPORTED_LOCK: &str = "<D:supportedlock><D:lockentry><D:lockscope><D:exclusive/></D:lockscope>\
     <D:locktype><D:write/></D:locktype></D:lockentry><D:lockentry><D:lockscope><D:shared/></D:lockscope>\
     <D:locktype><D:write/></D:locktype></D:lockentry></D:supportedlock>";

/// A lock as `<D:activelock>` (lock discovery and LOCK answers).
fn active_lock(l: &engine::DavLock) -> String {
    let left = l.until.saturating_sub(engine::now_ms()) / 1000;
    format!(
        "<D:activelock><D:locktype><D:write/></D:locktype><D:lockscope>{}</D:lockscope>\
         <D:depth>{}</D:depth>{}<D:timeout>Second-{left}</D:timeout><D:locktoken><D:href>{}</D:href></D:locktoken>\
         <D:lockroot><D:href>{}</D:href></D:lockroot></D:activelock>",
        if l.shared { "<D:shared/>" } else { "<D:exclusive/>" },
        if l.deep { "infinity" } else { "0" },
        l.owner,
        xml::escape(&l.token),
        xml::escape(&href(&l.bucket, &l.path)),
    )
}

/// One condition of an `If` header list: a state token or an entity tag, possibly negated.
#[derive(Debug, PartialEq)]
enum Cond {
    Token(bool, String),
    Etag(bool, String),
}

/// An `If` header (RFC 4918 10.4): lists of conditions, each for the request's resource or for a
/// tagged one (its path). None: malformed.
#[allow(clippy::type_complexity, clippy::question_mark)]
fn parse_if(h: &str) -> Option<Vec<(Option<String>, Vec<Cond>)>> {
    let mut out = vec![];
    let mut tag: Option<String> = None;
    let mut rest = h.trim();
    while !rest.is_empty() {
        if let Some(r) = rest.strip_prefix('<') {
            let end = r.find('>')?;
            tag = Some(r[..end].to_string());
            rest = r[end + 1..].trim_start();
        } else if let Some(r) = rest.strip_prefix('(') {
            let end = r.find(')')?;
            let mut inner = r[..end].trim();
            let mut conds = vec![];
            while !inner.is_empty() {
                let not = inner.len() >= 3 && inner[..3].eq_ignore_ascii_case("not");
                if not {
                    inner = inner[3..].trim_start();
                }
                if let Some(x) = inner.strip_prefix('<') {
                    let e = x.find('>')?;
                    conds.push(Cond::Token(not, x[..e].to_string()));
                    inner = x[e + 1..].trim_start();
                } else if let Some(x) = inner.strip_prefix('[') {
                    let e = x.find(']')?;
                    conds.push(Cond::Etag(not, x[..e].trim_start_matches("W/").trim_matches('"').to_string()));
                    inner = x[e + 1..].trim_start();
                } else {
                    return None;
                }
            }
            if conds.is_empty() {
                return None;
            }
            out.push((tag.clone(), conds));
            rest = r[end + 1..].trim_start();
        } else {
            return None;
        }
    }
    (!out.is_empty()).then_some(out)
}

/// The lock tokens an `If` header presents (outside `Not`).
fn submitted_tokens(if_header: &str) -> Vec<String> {
    parse_if(if_header)
        .unwrap_or_default()
        .into_iter()
        .flat_map(|(_, conds)| conds)
        .filter_map(|c| match c {
            Cond::Token(false, t) => Some(t),
            _ => None,
        })
        .collect()
}

/// Dead properties, kept in the object's user metadata as `x-amz-meta-dav-props`: percent-encoded JSON
/// of `{"{namespace}name": "text"}`.
const DEAD_PROPS: &str = "dav-props";

fn dead_props(o: Option<&Object>) -> std::collections::BTreeMap<String, String> {
    o.and_then(|o| o.user_meta.get(DEAD_PROPS))
        .and_then(|v| percent_encoding::percent_decode_str(v).decode_utf8().ok().map(|s| s.into_owned()))
        .and_then(|v| serde_json::from_str(&v).ok())
        .unwrap_or_default()
}

/// `{ns}name` as an element with its own namespace declaration.
fn prop_xml(full: &str, value: Option<&str>) -> String {
    let (ns, name) = full.strip_prefix('{').and_then(|r| r.split_once('}')).unwrap_or(("", full));
    let name: String = name.chars().filter(|c| c.is_alphanumeric() || matches!(c, '-' | '_' | '.')).collect();
    // A property in no namespace takes the empty default namespace; a prefix cannot be bound to "".
    let (open, close) = if ns.is_empty() {
        (format!("{name} xmlns=\"\""), name.clone())
    } else {
        (format!("X:{name} xmlns:X=\"{}\"", xml::escape(ns)), format!("X:{name}"))
    };
    match value {
        Some(v) => format!("<{open}>{}</{close}>", xml::escape(v)),
        None => format!("<{open}/>"),
    }
}

/// The requested lock timeout (`Timeout: Second-N, Infinite`), capped.
fn lock_timeout(h: &str) -> u64 {
    for t in h.split(',').map(str::trim) {
        if let Some(n) = t.strip_prefix("Second-").and_then(|n| n.parse::<u64>().ok()) {
            return n.clamp(1, LOCK_MAX_S);
        }
        if t.eq_ignore_ascii_case("Infinite") {
            return LOCK_MAX_S;
        }
    }
    LOCK_DEFAULT_S
}

fn multistatus(items: &[Res], wanted: &Option<BTreeSet<String>>, dead_wanted: &[String], locks: &[engine::DavLock]) -> String {
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
        if want("supportedlock") {
            out.push_str(SUPPORTED_LOCK);
        }
        if want("lockdiscovery") {
            let path = r.href.strip_prefix(PREFIX).and_then(|p| p.strip_prefix('/')).unwrap_or("");
            let path = String::from_utf8(auth::percent_decode(path)).unwrap_or_default();
            let (b, k) = path.split_once('/').unwrap_or((path.as_str(), ""));
            let mine: Vec<&engine::DavLock> = locks.iter().filter(|l| l.bucket == b && !k.is_empty() && l.covers(k)).collect();
            if mine.is_empty() {
                out.push_str("<D:lockdiscovery/>");
            } else {
                out.push_str("<D:lockdiscovery>");
                for l in mine {
                    out.push_str(&active_lock(l));
                }
                out.push_str("</D:lockdiscovery>");
            }
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
        let mut missing = String::new();
        if wanted.is_none() {
            for (k, v) in &r.dead {
                out.push_str(&prop_xml(k, Some(v)));
            }
        } else {
            for k in dead_wanted {
                match r.dead.get(k) {
                    Some(v) => out.push_str(&prop_xml(k, Some(v))),
                    None => missing.push_str(&prop_xml(k, None)),
                }
            }
        }
        out.push_str("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>");
        if !missing.is_empty() {
            out.push_str(&format!("<D:propstat><D:prop>{missing}</D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>"));
        }
        out.push_str("</D:response>");
    }
    out.push_str("</D:multistatus>\n");
    out
}

fn xml_status(status: StatusCode, doc: String) -> Resp {
    let mut r = full(status, doc);
    r.headers_mut().insert("content-type", HeaderValue::from_static("application/xml; charset=utf-8"));
    r
}

fn uuid_v4() -> String {
    use rand::RngCore as _;
    let mut b = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h = hex::encode(b);
    format!("{}-{}-{}-{}-{}", &h[0..8], &h[8..12], &h[12..16], &h[16..20], &h[20..32])
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
    pub(crate) async fn may(&self, c: &Ctx, op: &'static str, bucket: &str, key: &str, list_prefix: Option<&str>) -> Result<(), ApiError> {
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
    pub(crate) fn tree(&self, bucket: &str, prefix: &str) -> Result<Vec<Object>, ApiError> {
        let mut out = vec![];
        let mut after = String::new();
        loop {
            let page = blocking(|| {
                self.eng.list(
                    bucket,
                    &ListOptions {
                        prefix: prefix.into(),
                        delimiter: String::new(),
                        after: after.clone(),
                        max_keys: 1000,
                        include_expired: false,
                    },
                )
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

    /// A PUT that creates a resource needs its parent collection (RFC 4918 9.7.1): 409 otherwise. Checked
    /// only when the caller may list that parent, so the answer reveals nothing they could not see.
    async fn put_parent(&self, c: &Ctx) -> Result<(), ApiError> {
        if c.method != Method::PUT {
            return Ok(());
        }
        let Some((p, _)) = c.key.trim_start_matches('/').rsplit_once('/') else { return Ok(()) };
        let parent = format!("{p}/");
        // Callers who may not write get the PUT's own 403; write-only callers (a drop box) are not told
        // what exists, and their PUT decides.
        if self.may(c, "PutObject", &c.bucket, &c.key, None).await.is_err()
            || self.may(c, "ListObjectsV2", &c.bucket, "", Some(&parent)).await.is_err()
        {
            return Ok(());
        }
        if self.collection_exists(&c.bucket, &parent)? {
            Ok(())
        } else {
            Err(e::DAV_CONFLICT.clone())
        }
    }

    /// Does a collection exist at `prefix` (ending in '/')? A bucket root always does.
    pub(crate) fn collection_exists(&self, bucket: &str, prefix: &str) -> Result<bool, ApiError> {
        if prefix.is_empty() {
            blocking(|| self.eng.head_bucket(bucket))?;
            return Ok(true);
        }
        let page = blocking(|| self.eng.list(bucket, &ListOptions { prefix: prefix.into(), max_keys: 1, ..Default::default() }))?;
        Ok(!page.objects.is_empty() || !page.common_prefixes.is_empty())
    }

    pub(crate) fn object(&self, bucket: &str, key: &str) -> Result<Option<Object>, ApiError> {
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
            h.insert("dav", HeaderValue::from_static("1, 2"));
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
        if let Err(err) = self.check_locks(&c, &host) {
            self.audit_denial(&c, &err);
            return fail("DavLocked", err, info);
        }
        let res: Result<(&'static str, Resp), ApiError> = match c.method.as_str() {
            "LOCK" => self.lock(&c, body).await.map(|r| ("DavLock", r)),
            "UNLOCK" => self.unlock(&c).await.map(|r| ("DavUnlock", r)),
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
                if let Err(err) = self.put_parent(&c).await {
                    return {
                        self.audit_denial(&c, &err);
                        fail("DavPut", err, info)
                    };
                }
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
            _ => Err(e::METHOD_NOT_ALLOWED.clone()),     // POST, ...
        };
        match res {
            Ok((op, r)) => (op, Ok(r), cors, info),
            Err(err) => {
                self.audit_denial(&c, &err);
                fail(if c.op == "unknown" { "Dav" } else { c.op }, err, info)
            }
        }
    }

    /// Evaluates a request's `If` header (RFC 4918 10.4): true when any list holds, for the request's
    /// resource or the one it is tagged with. A state token holds when it is a lock held on that
    /// resource; an entity tag when it is the resource's.
    fn if_holds(&self, c: &Ctx, conds: &[(Option<String>, Vec<Cond>)]) -> Result<bool, ApiError> {
        for (tag, list) in conds {
            let (bucket, key) = match tag {
                None => (c.bucket.clone(), c.key.clone()),
                Some(url) => {
                    let path = url.split_once("://").map(|(_, r)| r.find('/').map(|i| &r[i..]).unwrap_or("/")).unwrap_or(url);
                    let decoded = String::from_utf8(auth::percent_decode(path.split('?').next().unwrap_or(""))).unwrap_or_default();
                    split(&decoded).unwrap_or_default()
                }
            };
            let mut all = true;
            for cond in list {
                let (not, holds) = match cond {
                    Cond::Token(not, t) => {
                        let l = blocking(|| self.eng.dav_lock_get(t))?;
                        (*not, l.is_some_and(|l| l.bucket == bucket && !key.is_empty() && l.covers(&key)))
                    }
                    Cond::Etag(not, etag) => {
                        let o = if key.is_empty() || key.ends_with('/') { None } else { self.object(&bucket, &key)? };
                        (*not, o.is_some_and(|o| &o.etag == etag))
                    }
                };
                if holds == not {
                    all = false;
                    break;
                }
            }
            if all {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// Refuses a request whose `If` header does not hold (412), and a WebDAV write to a locked resource
    /// unless the request presents a lock token of the key that holds the lock (423): every exclusive
    /// lock that binds it, and one of its shared locks.
    fn check_locks(&self, c: &Ctx, host: &str) -> Result<(), ApiError> {
        let method = c.method.as_str();
        let if_header = c.header("if");
        if !if_header.is_empty() && !c.bucket.is_empty() {
            let conds = parse_if(if_header).ok_or_else(|| e::INVALID_ARGUMENT.clone())?;
            if !self.if_holds(c, &conds)? {
                return Err(e::DAV_LOCK_UNKNOWN.clone());
            }
        }
        if !matches!(method, "PUT" | "DELETE" | "MKCOL" | "PROPPATCH" | "COPY" | "MOVE") || c.bucket.is_empty() {
            return Ok(());
        }
        let me = c.auth.cred.access_key.as_str();
        let mut held = vec![];
        for t in submitted_tokens(if_header) {
            if let Some(l) = blocking(|| self.eng.dav_lock_get(&t))? {
                if l.holder == me {
                    held.push(l.token);
                }
            }
        }
        let satisfied = |locks: &[engine::DavLock]| {
            let exclusive_ok = locks.iter().filter(|l| !l.shared).all(|l| held.contains(&l.token));
            let shared: Vec<&engine::DavLock> = locks.iter().filter(|l| l.shared).collect();
            exclusive_ok && (shared.is_empty() || shared.iter().any(|l| held.contains(&l.token)))
        };
        // (bucket, path, member change, whole subtree)
        let mut targets: Vec<(String, String, bool, bool)> = vec![];
        match method {
            "PUT" | "MKCOL" => targets.push((c.bucket.clone(), c.key.clone(), true, false)),
            "PROPPATCH" => targets.push((c.bucket.clone(), c.key.clone(), false, false)),
            "DELETE" => targets.push((c.bucket.clone(), c.key.clone(), true, true)),
            _ => {
                if method == "MOVE" {
                    targets.push((c.bucket.clone(), c.key.clone(), true, true));
                }
                let dest = c.header("destination");
                let path = dest.split_once("://").map(|(_, r)| r.split_once('/').unwrap_or((r, ""))).map_or_else(
                    || dest.trim_start_matches('/').to_string(),
                    |(h, p)| if h.eq_ignore_ascii_case(host) { p.to_string() } else { String::new() },
                );
                let decoded =
                    String::from_utf8(auth::percent_decode(&format!("/{}", path.split('?').next().unwrap_or("")))).unwrap_or_default();
                if let Some((db, dk)) = split(&decoded) {
                    if !db.is_empty() && !dk.is_empty() {
                        targets.push((db, dk, true, true));
                    }
                }
            }
        }
        for (bucket, path, member, subtree) in targets {
            if path.is_empty() {
                continue;
            }
            let mut locks = blocking(|| self.eng.dav_locks_on(&bucket, &path, member, false))?;
            if subtree {
                let dir = if path.ends_with('/') { path.clone() } else { format!("{path}/") };
                locks.extend(blocking(|| self.eng.dav_locks_on(&bucket, &dir, false, true))?);
            }
            if !satisfied(&locks) {
                return Err(e::DAV_LOCKED.clone());
            }
        }
        Ok(())
    }

    /// LOCK: takes an exclusive or shared write lock (creating an empty file at an unmapped URL), or refreshes one
    /// (no body, its token in `If`).
    async fn lock<B>(&self, c: &Ctx, body: &mut B) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        if c.bucket.is_empty() || c.key.is_empty() {
            return Err(e::DAV_BUCKET_ROOT.clone());
        }
        self.may(c, "PutObject", &c.bucket, &c.key, None).await?;
        let timeout = lock_timeout(c.header("timeout"));
        let doc = self.read_small(c, &mut *body, 64 << 10).await?;
        let text = String::from_utf8_lossy(&doc).into_owned();
        let me = c.auth.cred.access_key.clone();
        let answer = |l: &engine::DavLock, status: StatusCode| {
            let doc = format!(
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<D:prop xmlns:D=\"DAV:\"><D:lockdiscovery>{}</D:lockdiscovery></D:prop>\n",
                active_lock(l)
            );
            let mut r = xml_status(status, doc);
            if let Ok(v) = HeaderValue::from_str(&format!("<{}>", l.token)) {
                r.headers_mut().insert("lock-token", v);
            }
            r
        };
        if text.trim().is_empty() {
            // A refresh: the token in If, held by this key, on this resource.
            let token = submitted_tokens(c.header("if")).into_iter().next().ok_or_else(|| e::DAV_NOT_LOCKED.clone())?;
            let l = blocking(|| self.eng.dav_lock_get(&token))?.ok_or_else(|| e::DAV_LOCK_UNKNOWN.clone())?;
            if l.holder != me || l.bucket != c.bucket || !l.covers(&c.key) {
                return Err(e::DAV_NOT_LOCKED.clone());
            }
            let until = engine::now_ms() + timeout * 1000;
            if !blocking(|| self.eng.dav_lock_update(&token, &me, Some(until)))? {
                return Err(e::DAV_NOT_LOCKED.clone());
            }
            return Ok(answer(&engine::DavLock { until, timeout_s: timeout, ..l }, StatusCode::OK));
        }
        let root = xml::parse(&text).filter(|r| r.name == "lockinfo").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let shared = root.child("lockscope").is_some_and(|s| s.child("shared").is_some());
        if root.child("locktype").is_some_and(|t| t.child("write").is_none()) {
            return Err(e::DAV_LOCK_TYPE.clone());
        }
        // The client's owner element, kept as escaped text (clients send a name or an href).
        let owner = root
            .child("owner")
            .map(|o| {
                let inner = o.child("href").map(|h| h.text.clone()).unwrap_or_else(|| o.text.clone());
                format!("<D:owner>{}</D:owner>", xml::escape(inner.trim()))
            })
            .unwrap_or_default();
        let existing = self.object(&c.bucket, c.key.trim_end_matches('/'))?;
        let dir = if c.key.ends_with('/') { c.key.clone() } else { format!("{}/", c.key) };
        let collection = existing.is_none() && self.collection_exists(&c.bucket, &dir)?;
        let deep = collection && !c.header("depth").trim().eq("0");
        let lock = engine::DavLock {
            token: format!("opaquelocktoken:{}", uuid_v4()),
            bucket: c.bucket.clone(),
            path: if collection { dir } else { c.key.trim_end_matches('/').to_string() },
            deep,
            shared,
            holder: me,
            owner,
            timeout_s: timeout,
            until: engine::now_ms() + timeout * 1000,
        };
        if blocking(|| self.eng.dav_lock(&lock))?.is_some() {
            return Err(e::DAV_LOCKED.clone());
        }
        let mut status = StatusCode::OK;
        if existing.is_none() && !collection {
            // An unmapped URL: LOCK creates an empty file (RFC 4918 7.3), owned by this lock.
            let mut x = c.clone();
            x.method = Method::PUT;
            x.key = lock.path.clone();
            x.query.retain(|k, _| super::files::presign_param(k));
            for h in ["content-type", "if", "timeout", "depth"] {
                x.headers.remove(h);
            }
            x.headers.insert("content-length", HeaderValue::from_static("0"));
            x.content_length = Some(0);
            if let Err(err) = self.route(&mut x, Empty::<Bytes>::new()).await {
                let _ = blocking(|| self.eng.dav_lock_update(&lock.token, &lock.holder, None));
                return Err(err);
            }
            status = StatusCode::CREATED;
        }
        Ok(answer(&lock, status))
    }

    /// UNLOCK: releases a lock this key holds (`Lock-Token: <token>`).
    async fn unlock(&self, c: &Ctx) -> Result<Resp, ApiError> {
        if c.bucket.is_empty() || c.key.is_empty() {
            return Err(e::DAV_BUCKET_ROOT.clone());
        }
        let token = c.header("lock-token").trim().trim_start_matches('<').trim_end_matches('>').to_string();
        let l = blocking(|| self.eng.dav_lock_get(&token))?.ok_or_else(|| e::DAV_NOT_LOCKED.clone())?;
        if l.bucket != c.bucket || !l.covers(&c.key) {
            return Err(e::DAV_NOT_LOCKED.clone());
        }
        if !blocking(|| self.eng.dav_lock_update(&token, &c.auth.cred.access_key, None))? {
            return Err(e::DAV_NOT_LOCKED.clone());
        }
        Ok(status_only(StatusCode::NO_CONTENT))
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
        // Requested properties that are not live ones: dead properties, by namespace and name.
        let dead_wanted: Vec<String> = if text.trim().is_empty() {
            vec![]
        } else {
            xml::parse(&text)
                .and_then(|r| {
                    r.child("prop").map(|p| {
                        p.children
                            .iter()
                            .filter(|x| !(x.ns == "DAV:" && PROPS.contains(&x.name.as_str())))
                            .map(|x| format!("{{{}}}{}", x.ns, x.name))
                            .collect()
                    })
                })
                .unwrap_or_default()
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
                                &ListOptions {
                                    prefix: p.clone(),
                                    delimiter: "/".into(),
                                    after: after.clone(),
                                    max_keys: 1000,
                                    include_expired: false,
                                },
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
        let locks = if c.bucket.is_empty() { vec![] } else { blocking(|| self.eng.dav_locks_on(&c.bucket, "", false, true))? };
        // A collection's dead properties are on its marker.
        for r in items.iter_mut().filter(|r| r.collection) {
            let path = String::from_utf8(auth::percent_decode(r.href.strip_prefix(PREFIX).unwrap_or("").trim_start_matches('/')))
                .unwrap_or_default();
            if let Some((b, k)) = path.split_once('/').filter(|(_, k)| !k.is_empty()) {
                r.dead = dead_props(self.object(b, k)?.as_ref());
            }
        }
        Ok(xml_status(StatusCode::MULTI_STATUS, multistatus(&items, &wanted, &dead_wanted, &locks)))
    }

    /// PROPPATCH: sets and removes dead properties, all or none (live DAV: properties are protected:
    /// 403, and every other change in the request then fails with 424).
    async fn proppatch<B>(&self, c: &Ctx, body: &mut B) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        if c.bucket.is_empty() || c.key.is_empty() {
            return Err(e::DAV_BUCKET_ROOT.clone());
        }
        self.may(c, "PutObject", &c.bucket, &c.key, None).await?;
        let doc = self.read_small(c, &mut *body, 64 << 10).await?;
        let root =
            xml::parse(&String::from_utf8_lossy(&doc)).filter(|r| r.name == "propertyupdate").ok_or_else(|| e::MALFORMED_XML.clone())?;
        // The object that holds them: the file, or a collection's marker (written if the collection is implied).
        let key = match self.object(&c.bucket, c.key.trim_end_matches('/'))? {
            Some(o) if !c.key.ends_with('/') => o.key,
            _ => {
                let marker = if c.key.ends_with('/') { c.key.clone() } else { format!("{}/", c.key) };
                if !self.collection_exists(&c.bucket, &marker)? {
                    return Err(e::NO_SUCH_KEY.clone());
                }
                marker
            }
        };
        let current = self.object(&c.bucket, &key)?;
        let mut props = dead_props(current.as_ref());
        let (mut changed, mut protected) = (vec![], vec![]);
        for action in root.children.iter().filter(|a| a.name == "set" || a.name == "remove") {
            for p in action.child("prop").map(|p| p.children.as_slice()).unwrap_or_default() {
                let full = format!("{{{}}}{}", p.ns, p.name);
                if p.ns == "DAV:" {
                    protected.push(full);
                    continue;
                }
                if action.name == "set" {
                    props.insert(full.clone(), p.text.clone());
                } else {
                    props.remove(&full);
                }
                changed.push(full);
            }
        }
        let answer = |ok: &[String], ok_status: &str, bad: &[String], bad_status: &str| {
            let mut doc = format!(
                "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<D:multistatus xmlns:D=\"DAV:\"><D:response><D:href>{}</D:href>",
                xml::escape(&href(&c.bucket, &c.key))
            );
            for (names, status) in [(ok, ok_status), (bad, bad_status)] {
                if !names.is_empty() {
                    let inner: String = names.iter().map(|n| prop_xml(n, None)).collect();
                    doc.push_str(&format!("<D:propstat><D:prop>{inner}</D:prop><D:status>HTTP/1.1 {status}</D:status></D:propstat>"));
                }
            }
            doc.push_str("</D:response></D:multistatus>\n");
            xml_status(StatusCode::MULTI_STATUS, doc)
        };
        if !protected.is_empty() {
            return Ok(answer(&changed, "424 Failed Dependency", &protected, "403 Forbidden"));
        }
        if changed.is_empty() {
            return Ok(answer(&[], "", &[], ""));
        }
        let json = serde_json::to_string(&props).map_err(|_| e::INTERNAL.clone())?;
        let encoded = percent_encoding::utf8_percent_encode(&json, percent_encoding::NON_ALPHANUMERIC).to_string();
        // Rewrite the object's metadata in place (a copy onto itself), over the version read.
        let mut x = c.clone();
        x.method = Method::PUT;
        x.key = key.clone();
        x.query.retain(|k, _| super::files::presign_param(k));
        x.headers = hyper::HeaderMap::new();
        x.headers.insert("authorization", c.headers.get("authorization").cloned().unwrap_or(HeaderValue::from_static("")));
        let src = format!("/{}/{}", aws_encode(c.bucket.as_bytes(), false), aws_encode(key.as_bytes(), true));
        let set = |x: &mut Ctx, k: &str, v: &str| -> Result<(), ApiError> {
            let name = hyper::header::HeaderName::from_bytes(k.as_bytes()).map_err(|_| e::INVALID_ARGUMENT.clone())?;
            x.headers.insert(name, HeaderValue::from_str(v).map_err(|_| e::INVALID_ARGUMENT.clone())?);
            Ok(())
        };
        set(&mut x, "x-amz-copy-source", &src)?;
        set(&mut x, "x-amz-metadata-directive", "REPLACE")?;
        set(&mut x, "content-length", "0")?;
        x.content_length = Some(0);
        if let Some(o) = &current {
            set(&mut x, "x-amz-copy-source-if-match", &format!("\"{}\"", o.etag))?;
            if !o.content_type.is_empty() {
                set(&mut x, "content-type", &o.content_type)?;
            }
            for (k, v) in &o.headers {
                set(&mut x, k, v)?;
            }
            for (k, v) in o.user_meta.iter().filter(|(k, _)| k.as_str() != DEAD_PROPS) {
                set(&mut x, &format!("x-amz-meta-{k}"), v)?;
            }
        }
        if !props.is_empty() {
            set(&mut x, &format!("x-amz-meta-{DEAD_PROPS}"), &encoded)?;
        }
        if current.is_none() {
            // An implied collection: write its marker with the properties.
            x.headers.remove("x-amz-copy-source");
            x.headers.remove("x-amz-metadata-directive");
            x.headers.remove("x-amz-copy-source-if-match");
        }
        match self.route(&mut x, Empty::<Bytes>::new()).await {
            Ok(_) => Ok(answer(&changed, "200 OK", &[], "")),
            Err(err) if err.code == e::METADATA_TOO_LARGE.code => Ok(answer(&[], "", &changed, "507 Insufficient Storage")),
            Err(err) => Err(err),
        }
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

    /// Renames keys of one bucket atomically (callers have authorized every pair), audited.
    pub(crate) fn rename_keys(&self, c: &Ctx, bucket: &str, pairs: &[(String, String)], overwrite: bool) -> Result<(), ApiError> {
        let mut ev = self.ev(c, "Copy");
        ev.audit = Some(crate::audit::Audit {
            category: "store",
            action: "object.rename".into(),
            actor: c.auth.cred.access_key.clone(),
            subject: format!("{bucket}/{}", pairs.first().map(|p| p.0.as_str()).unwrap_or("")),
            details: serde_json::json!({"to": pairs.first().map(|p| p.1.as_str()).unwrap_or("")}),
        });
        let rewrap = |o: &Object, to: &str| -> engine::Result<Option<engine::Sse>> {
            self.rewrap_sse(bucket, &o.key, to, &o.sse).map_err(|e| engine::Error::Index(format!("re-wrapping a data key: {}", e.message)))
        };
        tokio::task::block_in_place(|| self.eng.rename(bucket, pairs, overwrite, &ev, &rewrap))?;
        Ok(())
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
        // Depth (RFC 4918 9.8.3, 9.9.2): a COPY of a collection takes 0 (the collection alone) or
        // infinity; a MOVE always moves everything.
        let shallow = match c.header("depth").trim() {
            "" => false,
            d if d.eq_ignore_ascii_case("infinity") => false,
            "0" if !mv => true,
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        // The destination's parent collection must exist (RFC 4918 9.8.5: 409 Conflict otherwise).
        let parent = dk.trim_end_matches('/').rsplit_once('/').map(|(p, _)| format!("{p}/")).unwrap_or_default();
        if !self.collection_exists(&db, &parent)? {
            return Err(e::DAV_CONFLICT.clone());
        }
        let (objs, prefix) = self.resolve(c).await?;
        // A shallow COPY of a collection makes an empty collection: its marker, without members.
        let mut new_marker: Option<String> = None;
        // (source key, destination key)
        let pairs: Vec<(String, String)> = match &prefix {
            None => vec![(objs[0].key.clone(), dk.trim_end_matches('/').to_string())],
            Some(p) => {
                let dp = if dk.ends_with('/') { dk.clone() } else { format!("{dk}/") };
                if db == c.bucket && dp.starts_with(p.as_str()) {
                    return Err(e::DAV_CONFLICT.clone()); // into itself
                }
                if shallow {
                    if objs.iter().any(|o| &o.key == p) {
                        vec![(p.clone(), dp)] // the marker, with its properties
                    } else {
                        new_marker = Some(dp); // a collection only by its members: a new marker
                        vec![]
                    }
                } else {
                    objs.iter().map(|o| (o.key.clone(), format!("{dp}{}", &o.key[p.len()..]))).collect()
                }
            }
        };
        if db == c.bucket && pairs.iter().any(|(s, d)| s == d) {
            return Err(e::DAV_SAME.clone());
        }
        let mut existed = false;
        for d in pairs.iter().map(|(_, d)| d).chain(new_marker.iter()) {
            if self.object(&db, d)?.is_some() {
                existed = true;
            }
        }
        if existed && !overwrite {
            return Err(e::PRECONDITION.clone());
        }
        if let Some(m) = &new_marker {
            let mut x = c.clone();
            x.method = Method::PUT;
            x.bucket = db.clone();
            x.key = m.clone();
            x.query.retain(|k, _| super::files::presign_param(k));
            for h in ["content-type", "destination", "overwrite", "depth"] {
                x.headers.remove(h);
            }
            x.headers.insert("content-length", HeaderValue::from_static("0")); // the marker is empty
            self.route(&mut x, Empty::<Bytes>::new()).await?;
        }
        for (s, d) in &pairs {
            self.may(c, "GetObject", &c.bucket, s, None).await?;
            self.may(c, "PutObject", &db, d, None).await?;
            if mv {
                self.may(c, "DeleteObject", &c.bucket, s, None).await?;
            }
        }
        // Within one unversioned bucket a MOVE is one transaction: every key moves, or none does.
        if mv && db == c.bucket {
            match self.rename_keys(c, &db, &pairs, overwrite) {
                Ok(()) => return Ok(status_only(if existed { StatusCode::NO_CONTENT } else { StatusCode::CREATED })),
                Err(err) if err == e::RENAME_VERSIONED => {} // versioned: copy, then delete
                Err(err) => return Err(err),
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
