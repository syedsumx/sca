//! A small S3 client for buckets on other endpoints (backup sinks, mounted remote buckets): put, get from
//! an offset, head, delete and list, signed with SigV4. Its calls block (run them on blocking threads).

use std::io::Read;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use serde::Deserialize;

use super::auth::aws_encode;

type Body = BoxBody<Bytes, std::io::Error>;

/// An S3 bucket (and key prefix) on another endpoint.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteSpec {
    pub id: String,
    pub endpoint: String,
    pub bucket: String,
    #[serde(default)]
    pub prefix: String,
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

/// Reads and checks a JSON list of remote buckets (`what` names them in errors).
pub fn load_specs(text: &str, what: &str) -> Result<Vec<RemoteSpec>, String> {
    let mut v: Vec<RemoteSpec> = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let mut seen = std::collections::HashSet::new();
    for s in v.iter_mut() {
        if !s.secret_key_file.is_empty() {
            s.secret_key =
                std::fs::read_to_string(&s.secret_key_file).map_err(|e| format!("{}: {e}", s.secret_key_file))?.trim().to_string();
        }
        if s.id.is_empty() || !s.id.chars().all(|c| c.is_ascii_alphanumeric() || "-_".contains(c)) {
            return Err(format!("{what} {:?}: id is letters, digits, - and _", s.id));
        }
        if !seen.insert(s.id.clone()) {
            return Err(format!("{what} {}: defined twice", s.id));
        }
        if s.bucket.is_empty() || s.access_key.is_empty() || s.secret_key.is_empty() {
            return Err(format!("{what} {}: bucket, access_key and secret_key are required", s.id));
        }
        if !s.endpoint.starts_with("http://") && !s.endpoint.starts_with("https://") {
            return Err(format!("{what} {}: endpoint must be http:// or https://", s.id));
        }
        if !s.prefix.is_empty() && !s.prefix.ends_with('/') {
            s.prefix.push('/');
        }
        s.endpoint = s.endpoint.trim_end_matches('/').to_string();
    }
    Ok(v)
}

/// One listed object.
#[derive(Clone, Debug, Default)]
pub struct Listed {
    /// The key below the spec's prefix.
    pub key: String,
    pub size: u64,
    pub etag: String,
    /// Milliseconds since the epoch.
    pub modified: u64,
}

/// What a HEAD tells about an object.
#[derive(Clone, Debug, Default)]
pub struct HeadInfo {
    pub len: u64,
    pub etag: String,
    pub content_type: String,
    pub meta: std::collections::BTreeMap<String, String>,
}

pub struct Remote {
    pub spec: RemoteSpec,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>,
    rt: tokio::runtime::Handle,
}

fn small(b: impl Into<Bytes>) -> Body {
    Full::new(b.into()).map_err(|never| match never {}).boxed()
}

fn err(e: impl std::fmt::Display) -> std::io::Error {
    std::io::Error::other(e.to_string())
}

impl Remote {
    pub fn new(spec: RemoteSpec) -> Result<Remote, String> {
        Ok(Remote {
            spec,
            http: Client::builder(TokioExecutor::new()).build(crate::tls::https_connector()?),
            rt: tokio::runtime::Handle::current(),
        })
    }

    fn url(&self, key: &str, query: &str) -> String {
        let path = format!(
            "/{}/{}",
            aws_encode(self.spec.bucket.as_bytes(), false),
            aws_encode(format!("{}{key}", self.spec.prefix).as_bytes(), true)
        );
        format!("{}{path}{}", self.spec.endpoint, if query.is_empty() { String::new() } else { format!("?{query}") })
    }

    async fn send(
        &self,
        method: &str,
        url: &str,
        headers: &[(&str, String)],
        body: Body,
    ) -> std::io::Result<hyper::Response<hyper::body::Incoming>> {
        let mut req = hyper::Request::builder().method(method).uri(url);
        for (k, v) in crate::admin_cli::sign_with_payload(
            method,
            url,
            "UNSIGNED-PAYLOAD",
            &self.spec.access_key,
            &self.spec.secret_key,
            &self.spec.region,
            time::OffsetDateTime::now_utc(),
        ) {
            req = req.header(k, v);
        }
        for (k, v) in headers {
            req = req.header(*k, v.as_str());
        }
        let req = req.body(body).map_err(err)?;
        tokio::time::timeout(Duration::from_secs(300), self.http.request(req))
            .await
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, format!("{method} {url}: timed out")))?
            .map_err(|e| err(format!("{method} {url}: {e}")))
    }

    async fn check(resp: hyper::Response<hyper::body::Incoming>, what: &str) -> std::io::Result<hyper::Response<hyper::body::Incoming>> {
        if resp.status().is_success() {
            return Ok(resp);
        }
        let status = resp.status();
        let body = resp.into_body().collect().await.map(|b| b.to_bytes()).unwrap_or_default();
        Err(std::io::Error::new(
            if status == hyper::StatusCode::NOT_FOUND { std::io::ErrorKind::NotFound } else { std::io::ErrorKind::Other },
            format!("{what}: HTTP {status}: {}", String::from_utf8_lossy(&body).chars().take(300).collect::<String>()),
        ))
    }

    /// Writes an object (streamed); returns its ETag.
    pub fn put(&self, key: &str, body: Box<dyn Read + Send>, len: u64, headers: &[(&str, String)]) -> std::io::Result<String> {
        let url = self.url(key, "");
        self.rt.block_on(async {
            let mut h: Vec<(&str, String)> = vec![("content-length", len.to_string())];
            h.extend(headers.iter().cloned());
            let resp = self.send("PUT", &url, &h, super::tiering::file_body(body.take(len))).await?;
            let resp = Self::check(resp, &format!("PUT {key} to {}", self.spec.id)).await?;
            Ok(resp.headers().get("etag").and_then(|v| v.to_str().ok()).unwrap_or("").trim_matches('"').to_string())
        })
    }

    /// Writes a small object from memory.
    pub fn put_bytes(&self, key: &str, data: Vec<u8>, content_type: &str) -> std::io::Result<String> {
        let len = data.len() as u64;
        self.put(key, Box::new(std::io::Cursor::new(data)), len, &[("content-type", content_type.to_string())])
    }

    /// Reads an object from byte `offset` on.
    pub fn get(&self, key: &str, offset: u64) -> std::io::Result<Box<dyn Read + Send>> {
        let url = self.url(key, "");
        let resp = self.rt.block_on(async {
            let h: Vec<(&str, String)> = if offset > 0 { vec![("range", format!("bytes={offset}-"))] } else { vec![] };
            let resp = self.send("GET", &url, &h, small(Bytes::new())).await?;
            Self::check(resp, &format!("GET {key} from {}", self.spec.id)).await
        })?;
        if offset > 0 && resp.status() != hyper::StatusCode::PARTIAL_CONTENT {
            return Err(err(format!("{} ignored a range request", self.spec.id)));
        }
        Ok(Box::new(super::tiering::BodyReader::new(self.rt.clone(), resp.into_body())))
    }

    pub fn get_bytes(&self, key: &str) -> std::io::Result<Vec<u8>> {
        let mut out = vec![];
        self.get(key, 0)?.read_to_end(&mut out)?;
        Ok(out)
    }

    /// An object's size and ETag (None: no such object).
    pub fn head(&self, key: &str) -> std::io::Result<Option<(u64, String)>> {
        let url = self.url(key, "");
        self.rt.block_on(async {
            let resp = self.send("HEAD", &url, &[], small(Bytes::new())).await?;
            if resp.status() == hyper::StatusCode::NOT_FOUND {
                return Ok(None);
            }
            let resp = Self::check(resp, &format!("HEAD {key} on {}", self.spec.id)).await?;
            let len = resp.headers().get("content-length").and_then(|v| v.to_str().ok()?.parse().ok()).unwrap_or(0);
            let etag = resp.headers().get("etag").and_then(|v| v.to_str().ok()).unwrap_or("").trim_matches('"').to_string();
            Ok(Some((len, etag)))
        })
    }

    /// An object's size, ETag, content type and user metadata (None: no such object).
    pub fn head_info(&self, key: &str) -> std::io::Result<Option<HeadInfo>> {
        let url = self.url(key, "");
        self.rt.block_on(async {
            let resp = self.send("HEAD", &url, &[], small(Bytes::new())).await?;
            if resp.status() == hyper::StatusCode::NOT_FOUND {
                return Ok(None);
            }
            let resp = Self::check(resp, &format!("HEAD {key} on {}", self.spec.id)).await?;
            let h = resp.headers();
            let get = |k: &str| h.get(k).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
            let meta = h
                .iter()
                .filter_map(|(k, v)| Some((k.as_str().strip_prefix("x-amz-meta-")?.to_string(), v.to_str().ok()?.to_string())))
                .collect();
            Ok(Some(HeadInfo {
                len: get("content-length").parse().unwrap_or(0),
                etag: get("etag").trim_matches('"').to_string(),
                content_type: get("content-type"),
                meta,
            }))
        })
    }

    /// Deletes an object (a missing one counts as deleted).
    pub fn delete(&self, key: &str) -> std::io::Result<()> {
        let url = self.url(key, "");
        self.rt.block_on(async {
            let resp = self.send("DELETE", &url, &[], small(Bytes::new())).await?;
            match Self::check(resp, &format!("DELETE {key} on {}", self.spec.id)).await {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
                _ => Ok(()),
            }
        })
    }

    /// Lists every object under `prefix` (below the spec's prefix), or with a delimiter, the common
    /// prefixes too. Pages through the whole listing.
    pub fn list(&self, prefix: &str, delimiter: &str) -> std::io::Result<(Vec<Listed>, Vec<String>)> {
        let mut out = vec![];
        let mut prefixes = vec![];
        let mut token = String::new();
        let full = format!("{}{prefix}", self.spec.prefix);
        loop {
            let mut q = vec![("list-type", "2".to_string()), ("prefix", full.clone())];
            if !delimiter.is_empty() {
                q.push(("delimiter", delimiter.to_string()));
            }
            if !token.is_empty() {
                q.push(("continuation-token", token.clone()));
            }
            q.sort();
            let query: Vec<String> = q.iter().map(|(k, v)| format!("{k}={}", aws_encode(v.as_bytes(), false))).collect();
            let url = format!("{}/{}?{}", self.spec.endpoint, aws_encode(self.spec.bucket.as_bytes(), false), query.join("&"));
            let body = self.rt.block_on(async {
                let resp = self.send("GET", &url, &[], small(Bytes::new())).await?;
                let resp = Self::check(resp, &format!("list {}", self.spec.id)).await?;
                resp.into_body().collect().await.map(|b| b.to_bytes()).map_err(err)
            })?;
            let doc = String::from_utf8_lossy(&body).into_owned();
            let root = super::xml::parse(&doc).ok_or_else(|| err(format!("{}: unreadable listing", self.spec.id)))?;
            for c in root.all("Contents") {
                let key = c.text_of_raw("Key");
                let Some(rel) = key.strip_prefix(&self.spec.prefix) else { continue };
                let modified = time::OffsetDateTime::parse(&c.text_of("LastModified"), &time::format_description::well_known::Rfc3339)
                    .map(|t| (t.unix_timestamp_nanos() / 1_000_000) as u64)
                    .unwrap_or(0);
                out.push(Listed {
                    key: rel.to_string(),
                    size: c.text_of("Size").parse().unwrap_or(0),
                    etag: c.text_of("ETag").trim_matches('"').to_string(),
                    modified,
                });
            }
            for p in root.all("CommonPrefixes") {
                if let Some(rel) = p.text_of_raw("Prefix").strip_prefix(&self.spec.prefix) {
                    prefixes.push(rel.to_string());
                }
            }
            if root.text_of("IsTruncated") != "true" {
                break;
            }
            token = root.text_of("NextContinuationToken");
            if token.is_empty() {
                break;
            }
        }
        Ok((out, prefixes))
    }
}
