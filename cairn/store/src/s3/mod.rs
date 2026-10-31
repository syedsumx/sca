//! The S3 API over a Cairn Store engine.

pub mod auth;
pub mod body;
pub mod checksum;
mod config;
pub mod errors;
mod handlers;
pub mod xml;

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Full};
use hyper::header::{HeaderName, HeaderValue};
use hyper::{HeaderMap, Method, Request, Response, StatusCode};
use time::OffsetDateTime;

use crate::engine::{Engine, Stats};
use auth::{Auth, Credential};
use errors::{self as e, ApiError};

pub type Body = BoxBody<Bytes, io::Error>;
pub type Resp = Response<Body>;

pub struct Config {
    pub region: String,
    /// Enables virtual-hosted-style requests: `<bucket>.<domain>`.
    pub domain: String,
    pub credentials: Vec<Credential>,
    /// Largest single PUT (default 5 GiB).
    pub max_object: u64,
}

pub struct Server {
    eng: Arc<Engine>,
    creds: BTreeMap<String, Credential>,
    region: String,
    domain: String,
    max_obj: u64,
    metrics: Metrics,
    clock: Box<dyn Fn() -> OffsetDateTime + Send + Sync>,
}

/// One request, after routing and authentication.
pub(crate) struct Ctx {
    pub method: Method,
    pub bucket: String,
    pub key: String,
    pub path: String,
    pub query: BTreeMap<String, String>,
    pub headers: HeaderMap,
    pub auth: Auth,
    pub content_length: Option<u64>,
    pub op: &'static str,
}

impl Ctx {
    pub fn has(&self, k: &str) -> bool {
        self.query.contains_key(k)
    }

    pub fn q(&self, k: &str) -> &str {
        self.query.get(k).map(String::as_str).unwrap_or("")
    }

    pub fn header(&self, name: &str) -> &str {
        self.headers.get(name).and_then(|v| v.to_str().ok()).unwrap_or("")
    }
}

impl Server {
    pub fn new(eng: Arc<Engine>, cfg: Config) -> Result<Server, String> {
        if cfg.credentials.is_empty() {
            return Err("at least one credential is required".into());
        }
        let mut creds = BTreeMap::new();
        for c in cfg.credentials {
            if c.access_key.is_empty() || c.secret_key.len() < 8 {
                return Err(format!("credential {:?}: an access key and a secret of at least 8 characters are required", c.access_key));
            }
            creds.insert(c.access_key.clone(), c);
        }
        Ok(Server {
            eng,
            creds,
            region: if cfg.region.is_empty() { "us-east-1".into() } else { cfg.region },
            domain: cfg.domain.to_ascii_lowercase(),
            max_obj: if cfg.max_object == 0 { 5 << 30 } else { cfg.max_object },
            metrics: Metrics::default(),
            clock: Box::new(OffsetDateTime::now_utc),
        })
    }

    /// Replaces the clock (tests).
    #[cfg(test)]
    pub fn set_clock(&mut self, f: impl Fn() -> OffsetDateTime + Send + Sync + 'static) {
        self.clock = Box::new(f);
    }

    pub async fn handle<B>(self: Arc<Self>, req: Request<B>) -> Result<Resp, Infallible>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let started = Instant::now();
        let request_id = hex::encode_upper(rand::random::<[u8; 8]>());
        let len_in = req.headers().get("content-length").and_then(|v| v.to_str().ok()?.parse::<u64>().ok()).unwrap_or(0);
        let head = req.method() == Method::HEAD;
        let path = req.uri().path().to_string();
        let (op, result) = if path.starts_with("/-/") { ("admin", Ok(self.admin(&path))) } else { self.dispatch(req).await };
        let mut resp = match result {
            Ok(r) => r,
            Err(err) => error_response(&err, &path, &request_id, head),
        };
        let h = resp.headers_mut();
        h.insert("x-amz-request-id", HeaderValue::from_str(&request_id).unwrap());
        h.insert("x-amz-id-2", HeaderValue::from_str(&request_id).unwrap());
        h.insert("server", HeaderValue::from_static("CairnStore"));
        let status = resp.status().as_u16();
        self.metrics.observe(op, status, started.elapsed(), len_in);
        tracing::debug!(id = %request_id, op, status, path = %path, ms = started.elapsed().as_millis() as u64, "request");
        Ok(resp)
    }

    async fn dispatch<B>(&self, req: Request<B>) -> (&'static str, Result<Resp, ApiError>)
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let (parts, body) = req.into_parts();
        let raw_path = parts.uri.path().to_string();
        let raw_query = parts.uri.query().unwrap_or("").to_string();
        let host = parts.headers.get("host").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let host_only = host
            .rsplit_once(':')
            .filter(|(_, p)| p.bytes().all(|c| c.is_ascii_digit()))
            .map(|(h, _)| h)
            .unwrap_or(&host)
            .to_ascii_lowercase();
        let vhost = (!self.domain.is_empty()).then(|| host_only.strip_suffix(&format!(".{}", self.domain)).map(str::to_string)).flatten();
        let decoded = match String::from_utf8(auth::percent_decode(&raw_path)) {
            Ok(p) => p,
            Err(_) => return ("route", Err(e::INVALID_ARGUMENT.clone())),
        };
        let (bucket, key) = match &vhost {
            Some(b) => (b.clone(), decoded.trim_start_matches('/').to_string()),
            None => {
                let p = decoded.strip_prefix('/').unwrap_or(&decoded);
                match p.split_once('/') {
                    Some((b, k)) => (b.to_string(), k.to_string()),
                    None => (p.to_string(), String::new()),
                }
            }
        };
        let request = auth::Request {
            method: parts.method.as_str(),
            raw_path: &raw_path,
            raw_query: &raw_query,
            host: &host,
            headers: &parts.headers,
            now: (self.clock)(),
            vhost_bucket: vhost.as_deref(),
        };
        let auth = match auth::verify(&self.creds, &request) {
            Ok(a) => a,
            Err(err) => return ("auth", Err(err)),
        };
        if !auth.cred.allows(&bucket) {
            return ("auth", Err(e::ACCESS_DENIED.clone()));
        }
        if auth.cred.read_only && parts.method != Method::GET && parts.method != Method::HEAD {
            return ("auth", Err(e::ACCESS_DENIED.clone()));
        }
        let query = raw_query
            .split('&')
            .filter(|p| !p.is_empty())
            .map(|p| {
                let (k, v) = p.split_once('=').unwrap_or((p, ""));
                (auth::query_decode(k), auth::query_decode(v))
            })
            .collect();
        let content_length = parts.headers.get("content-length").and_then(|v| v.to_str().ok()?.parse().ok());
        let mut ctx =
            Ctx { method: parts.method, bucket, key, path: raw_path, query, headers: parts.headers, auth, content_length, op: "unknown" };
        let res = if ctx.key.len() > 1024 { Err(e::KEY_TOO_LONG.clone()) } else { self.route(&mut ctx, body).await };
        (ctx.op, res)
    }

    fn admin(&self, path: &str) -> Resp {
        match path {
            "/-/healthy" => text(StatusCode::OK, "ok\n"),
            "/-/ready" => match tokio::task::block_in_place(|| self.eng.list_buckets()) {
                Ok(_) => text(StatusCode::OK, "ready\n"),
                Err(_) => text(StatusCode::SERVICE_UNAVAILABLE, "not ready\n"),
            },
            "/-/metrics" => {
                let mut r = text(StatusCode::OK, self.metrics.render(&self.eng));
                r.headers_mut().insert("content-type", HeaderValue::from_static("text/plain; version=0.0.4"));
                r
            }
            _ => text(StatusCode::NOT_FOUND, ""),
        }
    }
}

// ------------------------------------------------------------------------------------------- responses

pub(crate) fn full(status: StatusCode, body: impl Into<Bytes>) -> Resp {
    let mut r = Response::new(Full::new(body.into()).map_err(|never| match never {}).boxed());
    *r.status_mut() = status;
    r
}

pub(crate) fn text(status: StatusCode, body: impl Into<Bytes>) -> Resp {
    full(status, body)
}

pub(crate) fn empty(status: StatusCode) -> Resp {
    full(status, Bytes::new())
}

pub(crate) fn xml_resp(status: StatusCode, doc: String) -> Resp {
    let mut r = full(status, doc);
    r.headers_mut().insert("content-type", HeaderValue::from_static("application/xml"));
    r
}

pub(crate) fn set_header(r: &mut Resp, name: &str, value: &str) {
    if let (Ok(n), Ok(v)) = (HeaderName::from_bytes(name.as_bytes()), HeaderValue::from_str(value)) {
        r.headers_mut().insert(n, v);
    }
}

fn error_response(err: &ApiError, resource: &str, request_id: &str, head: bool) -> Resp {
    let status = StatusCode::from_u16(err.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    if head {
        return empty(status);
    }
    let doc = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<Error><Code>{}</Code><Message>{}</Message><Resource>{}</Resource><RequestId>{}</RequestId></Error>",
        err.code,
        xml::escape(err.message),
        xml::escape(resource),
        request_id
    );
    xml_resp(status, doc)
}

pub(crate) fn iso_time(ms: u64) -> String {
    let t = OffsetDateTime::from_unix_timestamp_nanos(ms as i128 * 1_000_000).unwrap_or(OffsetDateTime::UNIX_EPOCH);
    t.format(time::macros::format_description!("[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:3]Z")).unwrap_or_default()
}

pub(crate) fn http_time(ms: u64) -> String {
    httpdate::fmt_http_date(std::time::UNIX_EPOCH + Duration::from_millis(ms))
}

// ------------------------------------------------------------------------------------------- metrics

#[derive(Default)]
struct Metrics {
    requests: Mutex<BTreeMap<(&'static str, u16), u64>>,
    seconds: Mutex<BTreeMap<&'static str, f64>>,
    bytes_in: AtomicU64,
    stats: Mutex<Option<(Instant, Stats)>>,
}

impl Metrics {
    fn observe(&self, op: &'static str, status: u16, took: Duration, bytes_in: u64) {
        *self.requests.lock().unwrap().entry((op, status)).or_default() += 1;
        *self.seconds.lock().unwrap().entry(op).or_default() += took.as_secs_f64();
        self.bytes_in.fetch_add(bytes_in, Ordering::Relaxed);
    }

    fn render(&self, eng: &Engine) -> String {
        let mut out = String::from("# TYPE cairn_store_requests_total counter\n");
        for ((op, status), n) in self.requests.lock().unwrap().iter() {
            out.push_str(&format!("cairn_store_requests_total{{op=\"{op}\",status=\"{status}\"}} {n}\n"));
        }
        out.push_str("# TYPE cairn_store_request_seconds_total counter\n");
        for (op, s) in self.seconds.lock().unwrap().iter() {
            out.push_str(&format!("cairn_store_request_seconds_total{{op=\"{op}\"}} {s}\n"));
        }
        out.push_str(&format!(
            "# TYPE cairn_store_received_bytes_total counter\ncairn_store_received_bytes_total {}\n",
            self.bytes_in.load(Ordering::Relaxed)
        ));
        let mut cached = self.stats.lock().unwrap();
        if cached.as_ref().is_none_or(|(at, _)| at.elapsed() > Duration::from_secs(30)) {
            if let Ok(st) = tokio::task::block_in_place(|| eng.stats()) {
                *cached = Some((Instant::now(), st));
            }
        }
        if let Some((_, st)) = cached.as_ref() {
            for (name, v) in
                [("buckets", st.buckets), ("objects", st.objects), ("stored_bytes", st.bytes), ("multipart_uploads", st.uploads)]
            {
                out.push_str(&format!("# TYPE cairn_store_{name} gauge\ncairn_store_{name} {v}\n"));
            }
        }
        out
    }
}

#[cfg(test)]
mod tests;
