//! A small S3 client for table metadata files: create-only PUT, GET and DELETE, signed with SigV4.
//! Works with Cairn Store and any other S3 endpoint (path-style addressing).

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};
use time::{macros::format_description, OffsetDateTime};

#[derive(Clone, Debug)]
pub struct S3Config {
    /// e.g. http://cairn-store:8333
    pub endpoint: String,
    pub region: String,
    pub access_key: String,
    pub secret_key: String,
}

#[derive(Debug)]
pub enum Error {
    /// Create-only PUT: the object already exists.
    Exists,
    NotFound,
    BadLocation(String),
    Http(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Exists => write!(f, "object already exists"),
            Error::NotFound => write!(f, "object not found"),
            Error::BadLocation(l) => write!(f, "not an s3:// location: {l}"),
            Error::Http(e) => write!(f, "object store: {e}"),
        }
    }
}

#[derive(Clone)]
pub struct Client {
    cfg: S3Config,
    http: reqwest::Client,
}

/// Splits `s3://bucket/key` (or `s3a://`, `s3n://`) into bucket and key.
pub fn parse_location(loc: &str) -> Result<(String, String), Error> {
    let rest = ["s3://", "s3a://", "s3n://"].iter().find_map(|p| loc.strip_prefix(p)).ok_or_else(|| Error::BadLocation(loc.into()))?;
    match rest.split_once('/') {
        Some((b, k)) if !b.is_empty() && !k.is_empty() => Ok((b.to_string(), k.to_string())),
        _ => Err(Error::BadLocation(loc.into())),
    }
}

fn hmac(key: &[u8], data: &str) -> Vec<u8> {
    let mut m = <Hmac<Sha256>>::new_from_slice(key).expect("hmac accepts any key");
    m.update(data.as_bytes());
    m.finalize().into_bytes().to_vec()
}

fn uri_encode(s: &str, keep_slash: bool) -> String {
    let mut out = String::new();
    for &c in s.as_bytes() {
        if c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_' | b'.' | b'~') || (keep_slash && c == b'/') {
            out.push(c as char);
        } else {
            out.push_str(&format!("%{c:02X}"));
        }
    }
    out
}

impl Client {
    pub fn new(cfg: S3Config) -> Client {
        let http = reqwest::Client::builder().timeout(std::time::Duration::from_secs(60)).build().expect("http client");
        Client { cfg, http }
    }

    fn request(&self, method: &str, bucket: &str, key: &str, body: &[u8], extra: &[(&str, &str)]) -> reqwest::RequestBuilder {
        let endpoint = self.cfg.endpoint.trim_end_matches('/');
        let host = endpoint.split("://").nth(1).unwrap_or(endpoint).split('/').next().unwrap_or("").to_string();
        let path = format!("/{}/{}", uri_encode(bucket, false), uri_encode(key, true));
        let now = OffsetDateTime::now_utc();
        let amz_date = now.format(format_description!("[year][month][day]T[hour][minute][second]Z")).unwrap();
        let date = &amz_date[..8];
        let payload = hex::encode(Sha256::digest(body));
        let mut headers: Vec<(String, String)> =
            vec![("host".into(), host), ("x-amz-content-sha256".into(), payload.clone()), ("x-amz-date".into(), amz_date.clone())];
        headers.extend(extra.iter().map(|(k, v)| (k.to_ascii_lowercase(), v.to_string())));
        headers.sort();
        let signed: Vec<&str> = headers.iter().map(|(k, _)| k.as_str()).collect();
        let canonical_headers: String = headers.iter().map(|(k, v)| format!("{k}:{}\n", v.trim())).collect();
        let canonical = format!("{method}\n{path}\n\n{canonical_headers}\n{}\n{payload}", signed.join(";"));
        let scope = format!("{date}/{}/s3/aws4_request", self.cfg.region);
        let to_sign = format!("AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}", hex::encode(Sha256::digest(canonical.as_bytes())));
        let key =
            hmac(&hmac(&hmac(&hmac(format!("AWS4{}", self.cfg.secret_key).as_bytes(), date), &self.cfg.region), "s3"), "aws4_request");
        let sig = hex::encode(hmac(&key, &to_sign));
        let auth =
            format!("AWS4-HMAC-SHA256 Credential={}/{scope}, SignedHeaders={}, Signature={sig}", self.cfg.access_key, signed.join(";"));
        let mut rb = self.http.request(method.parse().unwrap(), format!("{endpoint}{path}")).header("authorization", auth);
        for (k, v) in &headers {
            if k != "host" {
                rb = rb.header(k.as_str(), v.as_str());
            }
        }
        rb
    }

    /// Writes an object that must not exist yet (If-None-Match: *).
    pub async fn put_new(&self, location: &str, body: Vec<u8>, content_type: &str) -> Result<(), Error> {
        let (bucket, key) = parse_location(location)?;
        let resp = self
            .request("PUT", &bucket, &key, &body, &[("content-type", content_type), ("if-none-match", "*")])
            .body(body)
            .send()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;
        match resp.status().as_u16() {
            200 | 201 | 204 => Ok(()),
            412 => Err(Error::Exists),
            s => Err(Error::Http(format!("PUT {location}: HTTP {s}: {}", resp.text().await.unwrap_or_default()))),
        }
    }

    pub async fn get(&self, location: &str) -> Result<Vec<u8>, Error> {
        let (bucket, key) = parse_location(location)?;
        let resp = self.request("GET", &bucket, &key, b"", &[]).send().await.map_err(|e| Error::Http(e.to_string()))?;
        match resp.status().as_u16() {
            200 => Ok(resp.bytes().await.map_err(|e| Error::Http(e.to_string()))?.to_vec()),
            404 => Err(Error::NotFound),
            s => Err(Error::Http(format!("GET {location}: HTTP {s}"))),
        }
    }

    pub async fn delete(&self, location: &str) -> Result<(), Error> {
        let (bucket, key) = parse_location(location)?;
        let resp = self.request("DELETE", &bucket, &key, b"", &[]).send().await.map_err(|e| Error::Http(e.to_string()))?;
        match resp.status().as_u16() {
            200 | 204 | 404 => Ok(()),
            s => Err(Error::Http(format!("DELETE {location}: HTTP {s}"))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn locations() {
        assert_eq!(
            parse_location("s3://lake/wh/t/metadata/00000-x.metadata.json").unwrap(),
            ("lake".into(), "wh/t/metadata/00000-x.metadata.json".into())
        );
        assert_eq!(parse_location("s3a://lake/k").unwrap().0, "lake");
        assert!(parse_location("file:///tmp/x").is_err());
        assert!(parse_location("s3://lake").is_err());
    }
}
