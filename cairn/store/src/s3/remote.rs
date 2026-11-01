//! A small client for buckets on other endpoints (backup sinks, mounted remote buckets, storage tiers): S3
//! (SigV4), Azure Blob (Shared Key or SAS) and Google Cloud Storage (OAuth: a service account key, the
//! metadata server, or none for emulators). Put, get from an offset, head, delete and list. Its calls block
//! (run them on blocking threads).

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

/// A bucket (and key prefix) on another endpoint: S3 (`kind` "s3", the default), an Azure Blob container
/// ("azure") or a Google Cloud Storage bucket ("gcs").
///
/// - s3: `endpoint`, `access_key`, `secret_key` (or `secret_key_file`), `region` (default us-east-1).
/// - azure: `access_key` is the storage account; `secret_key` (or `secret_key_file`) its account key, or
///   `sas` (or `sas_file`) a SAS token instead. `endpoint` defaults to https://<account>.blob.core.windows.net
///   (for Azurite: http://host:10000/<account>).
/// - gcs: `auth` is "service_account" (`credentials_file`: a service account JSON key), "metadata" (the
///   instance's service account from the metadata server, as on GKE with workload identity) or "none"
///   (emulators). `endpoint` defaults to https://storage.googleapis.com.
#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteSpec {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub endpoint: String,
    pub bucket: String,
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub access_key: String,
    #[serde(default)]
    pub secret_key: String,
    #[serde(default)]
    pub secret_key_file: String,
    #[serde(default)]
    pub region: String,
    #[serde(default)]
    pub sas: String,
    #[serde(default)]
    pub sas_file: String,
    #[serde(default)]
    pub auth: String,
    #[serde(default)]
    pub credentials_file: String,
    /// Storage tiers only: the storage class the tier serves.
    #[serde(default)]
    pub storage_class: String,
    #[serde(skip)]
    service_account: Option<ServiceAccount>,
}

/// A GCS service account key.
#[derive(Clone, Debug)]
pub struct ServiceAccount {
    email: String,
    token_uri: String,
    key_der: Vec<u8>,
}

fn read_secret(path: &str) -> Result<String, String> {
    Ok(std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?.trim().to_string())
}

fn service_account(path: &str) -> Result<ServiceAccount, String> {
    #[derive(Deserialize)]
    struct Key {
        #[serde(rename = "type")]
        kind: String,
        client_email: String,
        private_key: String,
        #[serde(default)]
        token_uri: String,
    }
    let k: Key = serde_json::from_str(&std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?)
        .map_err(|e| format!("{path}: not a service account key: {e}"))?;
    if k.kind != "service_account" {
        return Err(format!("{path}: type is {:?}, not service_account", k.kind));
    }
    let b64: String = k.private_key.lines().filter(|l| !l.starts_with("-----")).collect();
    use base64::Engine;
    let der = base64::engine::general_purpose::STANDARD.decode(b64.trim()).map_err(|e| format!("{path}: private_key: {e}"))?;
    ring::signature::RsaKeyPair::from_pkcs8(&der).map_err(|e| format!("{path}: private_key is not an RSA PKCS#8 key: {e}"))?;
    let token_uri = if k.token_uri.is_empty() { "https://oauth2.googleapis.com/token".to_string() } else { k.token_uri };
    Ok(ServiceAccount { email: k.client_email, token_uri, key_der: der })
}

/// Checks a Google sign-in (`auth`: service_account, metadata or none; defaults to service_account when a
/// credentials file is given) and reads the service account key.
pub fn check_google_auth(auth: &mut String, credentials_file: &str, what: &str) -> Result<Option<ServiceAccount>, String> {
    if auth.is_empty() && !credentials_file.is_empty() {
        *auth = "service_account".into();
    }
    match auth.as_str() {
        "service_account" if credentials_file.is_empty() => Err(format!("{what}: auth service_account needs credentials_file")),
        "service_account" => Ok(Some(service_account(credentials_file)?)),
        "metadata" | "none" if !credentials_file.is_empty() => Err(format!("{what}: credentials_file is only for auth service_account")),
        "metadata" | "none" => Ok(None),
        "" => Err(format!("{what}: auth is required (service_account, metadata or none)")),
        a => Err(format!("{what}: auth {a:?} is not service_account, metadata or none")),
    }
}

/// Google OAuth access tokens: from a service account key (a signed JWT grant), the metadata server, or
/// none. Tokens are cached until a minute before they expire.
pub struct GoogleAuth {
    mode: String,
    sa: Option<ServiceAccount>,
    what: String,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>,
    token: std::sync::Mutex<Option<(String, std::time::Instant)>>,
}

impl GoogleAuth {
    pub fn new(mode: &str, sa: Option<ServiceAccount>, what: &str) -> Result<GoogleAuth, String> {
        Ok(GoogleAuth {
            mode: mode.to_string(),
            sa,
            what: what.to_string(),
            http: Client::builder(TokioExecutor::new()).build(crate::tls::https_connector()?),
            token: Default::default(),
        })
    }

    pub async fn token(&self) -> std::io::Result<Option<String>> {
        if self.mode == "none" {
            return Ok(None);
        }
        if let Some((t, until)) = self.token.lock().unwrap().clone() {
            if std::time::Instant::now() < until {
                return Ok(Some(t));
            }
        }
        let req = match self.mode.as_str() {
            "metadata" => {
                let host = std::env::var("GCE_METADATA_HOST").unwrap_or_else(|_| "metadata.google.internal".into());
                hyper::Request::builder()
                    .method("GET")
                    .uri(format!("http://{host}/computeMetadata/v1/instance/service-accounts/default/token"))
                    .header("metadata-flavor", "Google")
                    .body(small(Bytes::new()))
                    .map_err(err)?
            }
            _ => {
                let sa = self.sa.as_ref().ok_or_else(|| err("no service account key"))?;
                let jwt = gcs_jwt(sa)?;
                let form = format!("grant_type={}&assertion={jwt}", aws_encode(b"urn:ietf:params:oauth:grant-type:jwt-bearer", false));
                hyper::Request::builder()
                    .method("POST")
                    .uri(&sa.token_uri)
                    .header("content-type", "application/x-www-form-urlencoded")
                    .header("content-length", form.len().to_string())
                    .body(small(form))
                    .map_err(err)?
            }
        };
        let what = format!("access token for {}", self.what);
        let resp = tokio::time::timeout(Duration::from_secs(30), self.http.request(req))
            .await
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, format!("{what}: timed out")))?
            .map_err(|e| err(format!("{what}: {e}")))?;
        let resp = Remote::check(resp, &what).await?;
        let body = resp.into_body().collect().await.map_err(err)?.to_bytes();
        #[derive(Deserialize)]
        struct Tok {
            access_token: String,
            #[serde(default)]
            expires_in: u64,
        }
        let t: Tok = serde_json::from_slice(&body).map_err(|e| err(format!("{what}: {e}")))?;
        let life = Duration::from_secs(t.expires_in.max(120) - 60);
        *self.token.lock().unwrap() = Some((t.access_token.clone(), std::time::Instant::now() + life));
        Ok(Some(t.access_token))
    }
}

/// Checks one spec and fills in its defaults. `normalize_prefix` ends a non-empty prefix with `/`.
pub fn check_spec(s: &mut RemoteSpec, what: &str, normalize_prefix: bool) -> Result<(), String> {
    let name = s.id.clone();
    if s.kind.is_empty() {
        s.kind = "s3".into();
    }
    let kind = s.kind.clone();
    let refuse = |field: &str, set: bool| if set { Err(format!("{what} {name}: {field} does not apply to kind {kind}")) } else { Ok(()) };
    if !s.secret_key_file.is_empty() {
        if !s.secret_key.is_empty() {
            return Err(format!("{what} {name}: secret_key and secret_key_file are exclusive"));
        }
        s.secret_key = read_secret(&s.secret_key_file)?;
    }
    if !s.sas_file.is_empty() {
        if !s.sas.is_empty() {
            return Err(format!("{what} {name}: sas and sas_file are exclusive"));
        }
        s.sas = read_secret(&s.sas_file)?;
    }
    s.sas = s.sas.trim_start_matches('?').to_string();
    if s.bucket.is_empty() {
        return Err(format!("{what} {name}: bucket is required"));
    }
    match s.kind.as_str() {
        "s3" => {
            refuse("sas", !s.sas.is_empty())?;
            refuse("auth", !s.auth.is_empty())?;
            refuse("credentials_file", !s.credentials_file.is_empty())?;
            if s.endpoint.is_empty() || s.access_key.is_empty() || s.secret_key.is_empty() {
                return Err(format!("{what} {name}: endpoint, access_key and secret_key are required"));
            }
            if s.region.is_empty() {
                s.region = "us-east-1".into();
            }
        }
        "azure" => {
            refuse("region", !s.region.is_empty())?;
            refuse("auth", !s.auth.is_empty())?;
            refuse("credentials_file", !s.credentials_file.is_empty())?;
            if s.access_key.is_empty() {
                return Err(format!("{what} {name}: access_key (the storage account) is required"));
            }
            if s.secret_key.is_empty() == s.sas.is_empty() {
                return Err(format!("{what} {name}: give either secret_key (the account key) or sas"));
            }
            if !s.secret_key.is_empty() {
                use base64::Engine;
                base64::engine::general_purpose::STANDARD
                    .decode(&s.secret_key)
                    .map_err(|_| format!("{what} {name}: secret_key is not a base64 account key"))?;
            }
            if s.endpoint.is_empty() {
                s.endpoint = format!("https://{}.blob.core.windows.net", s.access_key);
            }
            if s.bucket.len() < 3 || !s.bucket.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') {
                return Err(format!("{what} {name}: an Azure container name is 3-63 lowercase letters, digits and -"));
            }
        }
        "gcs" => {
            refuse("region", !s.region.is_empty())?;
            refuse("sas", !s.sas.is_empty())?;
            refuse("access_key", !s.access_key.is_empty())?;
            refuse("secret_key", !s.secret_key.is_empty())?;
            s.service_account = check_google_auth(&mut s.auth, &s.credentials_file, &format!("{what} {name}"))?;
            if s.endpoint.is_empty() {
                s.endpoint = "https://storage.googleapis.com".into();
            }
        }
        k => return Err(format!("{what} {name}: kind {k:?} is not s3, azure or gcs")),
    }
    if !s.endpoint.starts_with("http://") && !s.endpoint.starts_with("https://") {
        return Err(format!("{what} {name}: endpoint must be http:// or https://"));
    }
    if normalize_prefix && !s.prefix.is_empty() && !s.prefix.ends_with('/') {
        s.prefix.push('/');
    }
    s.endpoint = s.endpoint.trim_end_matches('/').to_string();
    Ok(())
}

/// Reads and checks a JSON list of remote buckets (`what` names them in errors).
pub fn load_specs(text: &str, what: &str) -> Result<Vec<RemoteSpec>, String> {
    let mut v: Vec<RemoteSpec> = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let mut seen = std::collections::HashSet::new();
    for s in v.iter_mut() {
        if s.id.is_empty() || !s.id.chars().all(|c| c.is_ascii_alphanumeric() || "-_".contains(c)) {
            return Err(format!("{what} {:?}: id is letters, digits, - and _", s.id));
        }
        if !seen.insert(s.id.clone()) {
            return Err(format!("{what} {}: defined twice", s.id));
        }
        if !s.storage_class.is_empty() {
            return Err(format!("{what} {}: storage_class is for storage tiers", s.id));
        }
        check_spec(s, what, true)?;
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
    /// GCS: its sign-in.
    google: Option<GoogleAuth>,
}

fn small(b: impl Into<Bytes>) -> Body {
    Full::new(b.into()).map_err(|never| match never {}).boxed()
}

fn err(e: impl std::fmt::Display) -> std::io::Error {
    std::io::Error::other(e.to_string())
}

/// Azure blobs above this size are written as blocks.
const AZURE_BLOCKS_ABOVE: u64 = 64 << 20;
const AZURE_BLOCK: u64 = 16 << 20;
const AZURE_VERSION: &str = "2021-12-02";
/// Azure metadata names are C# identifiers; other names (S3 allows `-`) are stored hex-encoded behind this.
const AZURE_META_HEX: &str = "cairnx_";

fn azure_meta_name(k: &str) -> String {
    let ident = k.chars().next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        && !k.starts_with(AZURE_META_HEX);
    if ident {
        k.to_string()
    } else {
        format!("{AZURE_META_HEX}{}", hex::encode(k))
    }
}

fn azure_meta_unname(k: &str) -> String {
    k.strip_prefix(AZURE_META_HEX).and_then(|h| String::from_utf8(hex::decode(h).ok()?).ok()).unwrap_or_else(|| k.to_string())
}

/// Splits a URL into its path (as sent) and query parameters (decoded).
fn url_parts(url: &str) -> (String, Vec<(String, String)>) {
    let after = url.split_once("://").map_or(url, |(_, r)| r);
    let rest = after.find('/').map_or("/", |i| &after[i..]);
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    let params = query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            let dec = |s: &str| String::from_utf8_lossy(&super::auth::percent_decode(s)).into_owned();
            (dec(k), dec(v))
        })
        .collect();
    (path.to_string(), params)
}

/// Shared Key authorization for an Azure Blob request.
fn azure_shared_key(account: &str, key_b64: &str, method: &str, url: &str, headers: &[(String, String)]) -> String {
    use base64::Engine;
    use hmac::Mac;
    let get =
        |name: &str| headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.trim().to_string()).unwrap_or_default();
    let len = match get("content-length").as_str() {
        "0" => String::new(),
        l => l.to_string(),
    };
    let mut ms: Vec<(String, String)> = headers
        .iter()
        .filter(|(k, _)| k.to_ascii_lowercase().starts_with("x-ms-"))
        .map(|(k, v)| (k.to_ascii_lowercase(), v.trim().to_string()))
        .collect();
    ms.sort();
    let (path, params) = url_parts(url);
    let mut resource = format!("/{account}{path}");
    let mut q: std::collections::BTreeMap<String, Vec<String>> = Default::default();
    for (k, v) in params {
        q.entry(k.to_ascii_lowercase()).or_default().push(v);
    }
    for (k, mut vs) in q {
        vs.sort();
        resource.push_str(&format!("\n{k}:{}", vs.join(",")));
    }
    let mut sts = format!(
        "{method}\n{}\n{}\n{len}\n{}\n{}\n\n{}\n{}\n{}\n{}\n{}\n",
        get("content-encoding"),
        get("content-language"),
        get("content-md5"),
        get("content-type"),
        get("if-modified-since"),
        get("if-match"),
        get("if-none-match"),
        get("if-unmodified-since"),
        get("range"),
    );
    for (k, v) in ms {
        sts.push_str(&format!("{k}:{v}\n"));
    }
    sts.push_str(&resource);
    let key = base64::engine::general_purpose::STANDARD.decode(key_b64).unwrap_or_default();
    let mut mac = hmac::Hmac::<sha2::Sha256>::new_from_slice(&key).expect("any key length");
    mac.update(sts.as_bytes());
    format!("SharedKey {account}:{}", base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes()))
}

fn rfc3339_ms(s: &str) -> u64 {
    time::OffsetDateTime::parse(s, &time::format_description::well_known::Rfc3339)
        .map(|t| (t.unix_timestamp_nanos() / 1_000_000) as u64)
        .unwrap_or(0)
}

/// A reader that hands out at most `left` bytes of a shared reader (consecutive Azure blocks).
struct Share {
    inner: std::sync::Arc<std::sync::Mutex<Box<dyn Read + Send>>>,
    left: u64,
}

impl Read for Share {
    fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
        if self.left == 0 {
            return Ok(0);
        }
        let n = (out.len() as u64).min(self.left) as usize;
        let k = self.inner.lock().unwrap().read(&mut out[..n])?;
        if k == 0 {
            return Err(std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "source ended early"));
        }
        self.left -= k as u64;
        Ok(k)
    }
}

impl Remote {
    pub fn new(spec: RemoteSpec) -> Result<Remote, String> {
        let (spec_kind, spec_auth, spec_sa, spec_id) =
            (spec.kind.clone(), spec.auth.clone(), spec.service_account.clone(), spec.id.clone());
        Ok(Remote {
            spec,
            http: Client::builder(TokioExecutor::new()).build(crate::tls::https_connector()?),
            rt: tokio::runtime::Handle::current(),
            google: (spec_kind == "gcs").then(|| GoogleAuth::new(&spec_auth, spec_sa, &spec_id)).transpose()?,
        })
    }

    fn kind(&self) -> &str {
        &self.spec.kind
    }

    /// The object's URL (S3 and Azure: path style; GCS: its JSON API resource).
    fn url(&self, key: &str, query: &str) -> String {
        let name = format!("{}{key}", self.spec.prefix);
        let path = match self.kind() {
            "gcs" => format!("/storage/v1/b/{}/o/{}", aws_encode(self.spec.bucket.as_bytes(), false), aws_encode(name.as_bytes(), false)),
            _ => format!("/{}/{}", aws_encode(self.spec.bucket.as_bytes(), false), aws_encode(name.as_bytes(), true)),
        };
        format!("{}{path}{}", self.spec.endpoint, if query.is_empty() { String::new() } else { format!("?{query}") })
    }

    /// A GCS access token (cached until shortly before it expires).
    async fn gcs_token(&self) -> std::io::Result<Option<String>> {
        match &self.google {
            Some(g) => g.token().await,
            None => Ok(None),
        }
    }

    async fn send(
        &self,
        method: &str,
        url: &str,
        headers: &[(&str, String)],
        body: Body,
    ) -> std::io::Result<hyper::Response<hyper::body::Incoming>> {
        let mut url = url.to_string();
        let mut all: Vec<(String, String)> = headers.iter().map(|(k, v)| (k.to_string(), v.clone())).collect();
        match self.kind() {
            "azure" => {
                all.push(("x-ms-date".into(), httpdate::fmt_http_date(std::time::SystemTime::now())));
                all.push(("x-ms-version".into(), AZURE_VERSION.into()));
                if !self.spec.sas.is_empty() {
                    url.push(if url.contains('?') { '&' } else { '?' });
                    url.push_str(&self.spec.sas);
                } else {
                    let auth = azure_shared_key(&self.spec.access_key, &self.spec.secret_key, method, &url, &all);
                    all.push(("authorization".into(), auth));
                }
            }
            "gcs" => {
                if let Some(t) = self.gcs_token().await? {
                    all.push(("authorization".into(), format!("Bearer {t}")));
                }
            }
            _ => {
                for (k, v) in crate::admin_cli::sign_with_payload(
                    method,
                    &url,
                    "UNSIGNED-PAYLOAD",
                    &self.spec.access_key,
                    &self.spec.secret_key,
                    &self.spec.region,
                    time::OffsetDateTime::now_utc(),
                ) {
                    all.push((k.to_string(), v.to_string()));
                }
            }
        }
        let mut req = hyper::Request::builder().method(method).uri(&url);
        for (k, v) in &all {
            req = req.header(k.as_str(), v.as_str());
        }
        let req = req.body(body).map_err(err)?;
        let shown = url.split('?').next().unwrap_or("").to_string(); // never log a SAS
        tokio::time::timeout(Duration::from_secs(300), self.http.request(req))
            .await
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, format!("{method} {shown}: timed out")))?
            .map_err(|e| err(format!("{method} {shown}: {e}")))
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

    async fn body_of(resp: hyper::Response<hyper::body::Incoming>) -> std::io::Result<Bytes> {
        resp.into_body().collect().await.map(|b| b.to_bytes()).map_err(err)
    }

    fn etag_of(resp: &hyper::Response<hyper::body::Incoming>) -> String {
        resp.headers().get("etag").and_then(|v| v.to_str().ok()).unwrap_or("").trim_matches('"').to_string()
    }

    /// Writes an object (streamed); returns its ETag. `headers` are S3's: content-type and x-amz-meta-*.
    pub fn put(&self, key: &str, body: Box<dyn Read + Send>, len: u64, headers: &[(&str, String)]) -> std::io::Result<String> {
        let what = format!("PUT {key} to {}", self.spec.id);
        match self.kind() {
            "azure" => self.azure_put(key, body, len, headers, &what),
            "gcs" => self.gcs_put(key, body, len, headers, &what),
            _ => {
                let url = self.url(key, "");
                self.rt.block_on(async {
                    let mut h: Vec<(&str, String)> = vec![("content-length", len.to_string())];
                    h.extend(headers.iter().cloned());
                    let resp = self.send("PUT", &url, &h, super::tiering::file_body(body.take(len))).await?;
                    let resp = Self::check(resp, &what).await?;
                    Ok(Self::etag_of(&resp))
                })
            }
        }
    }

    fn azure_headers(headers: &[(&str, String)]) -> Vec<(String, String)> {
        headers
            .iter()
            .filter_map(|(k, v)| {
                let k = k.to_ascii_lowercase();
                if k == "content-type" {
                    Some(("x-ms-blob-content-type".to_string(), v.clone()))
                } else {
                    k.strip_prefix("x-amz-meta-").map(|m| (format!("x-ms-meta-{}", azure_meta_name(m)), v.clone()))
                }
            })
            .collect()
    }

    fn azure_put(
        &self,
        key: &str,
        body: Box<dyn Read + Send>,
        len: u64,
        headers: &[(&str, String)],
        what: &str,
    ) -> std::io::Result<String> {
        let props = Self::azure_headers(headers);
        let url = self.url(key, "");
        self.rt.block_on(async {
            if len <= AZURE_BLOCKS_ABOVE {
                let mut h: Vec<(&str, String)> = vec![("content-length", len.to_string()), ("x-ms-blob-type", "BlockBlob".into())];
                h.extend(props.iter().map(|(k, v)| (k.as_str(), v.clone())));
                let resp = self.send("PUT", &url, &h, super::tiering::file_body(body.take(len))).await?;
                return Ok(Self::etag_of(&Self::check(resp, what).await?));
            }
            use base64::Engine;
            let shared = std::sync::Arc::new(std::sync::Mutex::new(body));
            let mut ids = vec![];
            let mut done = 0u64;
            while done < len {
                let n = AZURE_BLOCK.min(len - done);
                let id = base64::engine::general_purpose::STANDARD.encode(format!("block-{:08}", ids.len()));
                let u = self.url(key, &format!("comp=block&blockid={}", aws_encode(id.as_bytes(), false)));
                let part = Share { inner: shared.clone(), left: n };
                let resp = self.send("PUT", &u, &[("content-length", n.to_string())], super::tiering::file_body(part)).await?;
                Self::check(resp, what).await?;
                ids.push(id);
                done += n;
            }
            let mut doc = String::from("<?xml version=\"1.0\" encoding=\"utf-8\"?><BlockList>");
            for id in &ids {
                doc.push_str(&format!("<Latest>{id}</Latest>"));
            }
            doc.push_str("</BlockList>");
            let mut h: Vec<(&str, String)> = vec![("content-length", doc.len().to_string()), ("content-type", "application/xml".into())];
            h.extend(props.iter().map(|(k, v)| (k.as_str(), v.clone())));
            let resp = self.send("PUT", &self.url(key, "comp=blocklist"), &h, small(doc)).await?;
            Ok(Self::etag_of(&Self::check(resp, what).await?))
        })
    }

    fn gcs_put(&self, key: &str, body: Box<dyn Read + Send>, len: u64, headers: &[(&str, String)], what: &str) -> std::io::Result<String> {
        let mut meta = serde_json::Map::new();
        let mut doc = serde_json::json!({"name": format!("{}{key}", self.spec.prefix)});
        for (k, v) in headers {
            let k = k.to_ascii_lowercase();
            if k == "content-type" {
                doc["contentType"] = v.clone().into();
            } else if let Some(m) = k.strip_prefix("x-amz-meta-") {
                meta.insert(m.to_string(), v.clone().into());
            }
        }
        if !meta.is_empty() {
            doc["metadata"] = meta.into();
        }
        let ctype = doc["contentType"].as_str().map(str::to_string);
        let doc = doc.to_string();
        let url =
            format!("{}/upload/storage/v1/b/{}/o?uploadType=resumable", self.spec.endpoint, aws_encode(self.spec.bucket.as_bytes(), false));
        self.rt.block_on(async {
            let mut h = vec![
                ("content-type", "application/json; charset=UTF-8".to_string()),
                ("content-length", doc.len().to_string()),
                ("x-upload-content-length", len.to_string()),
            ];
            let mut data_h = vec![("content-length", len.to_string())];
            if let Some(t) = &ctype {
                h.push(("x-upload-content-type", t.clone()));
                data_h.push(("content-type", t.clone()));
            }
            let resp = Self::check(self.send("POST", &url, &h, small(doc)).await?, what).await?;
            let session = resp.headers().get("location").and_then(|v| v.to_str().ok()).map(str::to_string);
            let session = session.ok_or_else(|| err(format!("{what}: no upload session")))?;
            let resp = self.send("PUT", &session, &data_h, super::tiering::file_body(body.take(len))).await?;
            let body = Self::body_of(Self::check(resp, what).await?).await?;
            let v: serde_json::Value = serde_json::from_slice(&body).map_err(|e| err(format!("{what}: {e}")))?;
            Ok(v["etag"].as_str().unwrap_or("").to_string())
        })
    }

    /// Writes a small object from memory.
    pub fn put_bytes(&self, key: &str, data: Vec<u8>, content_type: &str) -> std::io::Result<String> {
        let len = data.len() as u64;
        self.put(key, Box::new(std::io::Cursor::new(data)), len, &[("content-type", content_type.to_string())])
    }

    /// Reads an object from byte `offset` on.
    pub fn get(&self, key: &str, offset: u64) -> std::io::Result<Box<dyn Read + Send>> {
        let url = match self.kind() {
            "gcs" => self.url(key, "alt=media"),
            _ => self.url(key, ""),
        };
        let range = match self.kind() {
            "azure" => "x-ms-range",
            _ => "range",
        };
        let resp = self.rt.block_on(async {
            let h: Vec<(&str, String)> = if offset > 0 { vec![(range, format!("bytes={offset}-"))] } else { vec![] };
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
        Ok(self.head_info(key)?.map(|h| (h.len, h.etag)))
    }

    /// An object's size, ETag, content type and user metadata (None: no such object).
    pub fn head_info(&self, key: &str) -> std::io::Result<Option<HeadInfo>> {
        let what = format!("HEAD {key} on {}", self.spec.id);
        if self.kind() == "gcs" {
            let url = self.url(key, "");
            return self.rt.block_on(async {
                let resp = self.send("GET", &url, &[], small(Bytes::new())).await?;
                if resp.status() == hyper::StatusCode::NOT_FOUND {
                    return Ok(None);
                }
                let body = Self::body_of(Self::check(resp, &what).await?).await?;
                let v: serde_json::Value = serde_json::from_slice(&body).map_err(|e| err(format!("{what}: {e}")))?;
                let s = |k: &str| v[k].as_str().unwrap_or("").to_string();
                let meta = v["metadata"]
                    .as_object()
                    .map(|m| m.iter().filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string()))).collect())
                    .unwrap_or_default();
                Ok(Some(HeadInfo { len: s("size").parse().unwrap_or(0), etag: s("etag"), content_type: s("contentType"), meta }))
            });
        }
        let (prefix, unname): (&str, fn(&str) -> String) = match self.kind() {
            "azure" => ("x-ms-meta-", azure_meta_unname),
            _ => ("x-amz-meta-", str::to_string),
        };
        let url = self.url(key, "");
        self.rt.block_on(async {
            let resp = self.send("HEAD", &url, &[], small(Bytes::new())).await?;
            if resp.status() == hyper::StatusCode::NOT_FOUND {
                return Ok(None);
            }
            let resp = Self::check(resp, &what).await?;
            let h = resp.headers();
            let get = |k: &str| h.get(k).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
            let meta =
                h.iter().filter_map(|(k, v)| Some((unname(k.as_str().strip_prefix(prefix)?), v.to_str().ok()?.to_string()))).collect();
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

    async fn get_doc(&self, url: &str) -> std::io::Result<Bytes> {
        let resp = self.send("GET", url, &[], small(Bytes::new())).await?;
        Self::body_of(Self::check(resp, &format!("list {}", self.spec.id)).await?).await
    }

    fn query(mut q: Vec<(&str, String)>) -> String {
        q.sort();
        q.iter().map(|(k, v)| format!("{k}={}", aws_encode(v.as_bytes(), false))).collect::<Vec<_>>().join("&")
    }

    /// Lists every object under `prefix` (below the spec's prefix), or with a delimiter, the common
    /// prefixes too. Pages through the whole listing.
    pub fn list(&self, prefix: &str, delimiter: &str) -> std::io::Result<(Vec<Listed>, Vec<String>)> {
        let mut out = vec![];
        let mut prefixes = vec![];
        let mut token = String::new();
        let full = format!("{}{prefix}", self.spec.prefix);
        let bucket = aws_encode(self.spec.bucket.as_bytes(), false);
        let rel = |k: &str| k.strip_prefix(&self.spec.prefix).map(str::to_string);
        loop {
            let mut q = vec![("prefix", full.clone())];
            if !delimiter.is_empty() {
                q.push(("delimiter", delimiter.to_string()));
            }
            match self.kind() {
                "gcs" => {
                    if !token.is_empty() {
                        q.push(("pageToken", token.clone()));
                    }
                    let url = format!("{}/storage/v1/b/{bucket}/o?{}", self.spec.endpoint, Self::query(q));
                    let body = self.rt.block_on(self.get_doc(&url))?;
                    let v: serde_json::Value =
                        serde_json::from_slice(&body).map_err(|e| err(format!("{}: unreadable listing: {e}", self.spec.id)))?;
                    for it in v["items"].as_array().into_iter().flatten() {
                        let Some(key) = it["name"].as_str().and_then(rel) else { continue };
                        out.push(Listed {
                            key,
                            size: it["size"].as_str().and_then(|s| s.parse().ok()).unwrap_or(0),
                            etag: it["etag"].as_str().unwrap_or("").to_string(),
                            modified: rfc3339_ms(it["updated"].as_str().unwrap_or("")),
                        });
                    }
                    for p in v["prefixes"].as_array().into_iter().flatten() {
                        prefixes.extend(p.as_str().and_then(rel));
                    }
                    token = v["nextPageToken"].as_str().unwrap_or("").to_string();
                }
                "azure" => {
                    q.extend([("restype", "container".to_string()), ("comp", "list".to_string()), ("maxresults", "5000".to_string())]);
                    if !token.is_empty() {
                        q.push(("marker", token.clone()));
                    }
                    let url = format!("{}/{bucket}?{}", self.spec.endpoint, Self::query(q));
                    let body = self.rt.block_on(self.get_doc(&url))?;
                    let doc = String::from_utf8_lossy(&body).into_owned();
                    let root = super::xml::parse(doc.trim_start_matches('\u{feff}'))
                        .ok_or_else(|| err(format!("{}: unreadable listing", self.spec.id)))?;
                    if let Some(blobs) = root.child("Blobs") {
                        for b in blobs.all("Blob") {
                            let Some(key) = rel(&b.text_of_raw("Name")) else { continue };
                            let p = b.child("Properties");
                            let prop = |k: &str| p.map(|p| p.text_of(k)).unwrap_or_default();
                            let modified = httpdate::parse_http_date(&prop("Last-Modified"))
                                .ok()
                                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                                .map_or(0, |d| d.as_millis() as u64);
                            out.push(Listed {
                                key,
                                size: prop("Content-Length").parse().unwrap_or(0),
                                etag: prop("Etag").trim_matches('"').to_string(),
                                modified,
                            });
                        }
                        for p in blobs.all("BlobPrefix") {
                            prefixes.extend(rel(&p.text_of_raw("Name")));
                        }
                    }
                    token = root.text_of("NextMarker");
                }
                _ => {
                    q.push(("list-type", "2".to_string()));
                    if !token.is_empty() {
                        q.push(("continuation-token", token.clone()));
                    }
                    let url = format!("{}/{bucket}?{}", self.spec.endpoint, Self::query(q));
                    let body = self.rt.block_on(self.get_doc(&url))?;
                    let doc = String::from_utf8_lossy(&body).into_owned();
                    let root = super::xml::parse(&doc).ok_or_else(|| err(format!("{}: unreadable listing", self.spec.id)))?;
                    for c in root.all("Contents") {
                        let Some(key) = rel(&c.text_of_raw("Key")) else { continue };
                        out.push(Listed {
                            key,
                            size: c.text_of("Size").parse().unwrap_or(0),
                            etag: c.text_of("ETag").trim_matches('"').to_string(),
                            modified: rfc3339_ms(&c.text_of("LastModified")),
                        });
                    }
                    for p in root.all("CommonPrefixes") {
                        prefixes.extend(rel(&p.text_of_raw("Prefix")));
                    }
                    token = if root.text_of("IsTruncated") == "true" { root.text_of("NextContinuationToken") } else { String::new() };
                }
            }
            if token.is_empty() {
                break;
            }
        }
        Ok((out, prefixes))
    }
}

/// A signed JWT asking for a storage token with a service account key.
fn gcs_jwt(sa: &ServiceAccount) -> std::io::Result<String> {
    use base64::Engine;
    let b64 = |b: &[u8]| base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b);
    let now = time::OffsetDateTime::now_utc().unix_timestamp();
    let claims = serde_json::json!({
        "iss": sa.email, "scope": "https://www.googleapis.com/auth/devstorage.read_write",
        "aud": sa.token_uri, "iat": now, "exp": now + 3600,
    });
    let msg = format!("{}.{}", b64(br#"{"alg":"RS256","typ":"JWT"}"#), b64(claims.to_string().as_bytes()));
    let key = ring::signature::RsaKeyPair::from_pkcs8(&sa.key_der).map_err(err)?;
    let mut sig = vec![0u8; key.public().modulus_len()];
    key.sign(&ring::signature::RSA_PKCS1_SHA256, &ring::rand::SystemRandom::new(), msg.as_bytes(), &mut sig).map_err(err)?;
    Ok(format!("{msg}.{}", b64(&sig)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn azure_meta_names_round_trip() {
        for k in ["plain", "with-dash", "9lives", "cairnx_sneaky", "_ok"] {
            let n = azure_meta_name(k);
            assert!(n.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'), "{n}");
            assert_eq!(azure_meta_unname(&n), k);
        }
        assert_eq!(azure_meta_name("plain"), "plain");
    }

    #[test]
    fn azure_shared_key_matches_the_documented_canonical_form() {
        // Canonicalized resource: the account, the path as sent, then the decoded query parameters, sorted.
        let h = vec![
            ("x-ms-version".to_string(), "2021-12-02".to_string()),
            ("x-ms-date".to_string(), "Sun, 01 Nov 2026 00:00:00 GMT".to_string()),
        ];
        let a =
            azure_shared_key("acct", "a2V5", "GET", "https://acct.blob.core.windows.net/c?restype=container&comp=list&prefix=a%2Fb", &h);
        use base64::Engine;
        use hmac::Mac;
        let sts = "GET\n\n\n\n\n\n\n\n\n\n\n\nx-ms-date:Sun, 01 Nov 2026 00:00:00 GMT\nx-ms-version:2021-12-02\n/acct/c\ncomp:list\nprefix:a/b\nrestype:container";
        let mut mac = hmac::Hmac::<sha2::Sha256>::new_from_slice(b"key").unwrap();
        mac.update(sts.as_bytes());
        assert_eq!(a, format!("SharedKey acct:{}", base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes())));
    }

    #[test]
    fn specs_fail_closed() {
        let bad = [
            r#"[{"id":"a","kind":"ftp","bucket":"b"}]"#,
            r#"[{"id":"a","kind":"azure","bucket":"box","access_key":"acct"}]"#,
            r#"[{"id":"a","kind":"azure","bucket":"box","access_key":"acct","secret_key":"a2V5","sas":"sv=1"}]"#,
            r#"[{"id":"a","kind":"azure","bucket":"Box","access_key":"acct","sas":"sv=1"}]"#,
            r#"[{"id":"a","kind":"azure","bucket":"box","access_key":"acct","sas":"sv=1","region":"eu"}]"#,
            r#"[{"id":"a","kind":"gcs","bucket":"b"}]"#,
            r#"[{"id":"a","kind":"gcs","bucket":"b","auth":"none","access_key":"x"}]"#,
            r#"[{"id":"a","kind":"gcs","bucket":"b","auth":"magic"}]"#,
            r#"[{"id":"a","kind":"s3","bucket":"b","endpoint":"http://x","access_key":"k","secret_key":"s","sas":"x"}]"#,
            r#"[{"id":"a","bucket":"b","endpoint":"http://x","access_key":"k","secret_key":"s","storage_class":"STANDARD_IA"}]"#,
        ];
        for b in bad {
            assert!(load_specs(b, "remote").is_err(), "{b}");
        }
        let ok = load_specs(r#"[{"id":"a","kind":"azure","bucket":"box","access_key":"acct","sas":"?sv=1"}]"#, "remote").unwrap();
        assert_eq!(ok[0].endpoint, "https://acct.blob.core.windows.net");
        assert_eq!(ok[0].sas, "sv=1");
        let g = load_specs(r#"[{"id":"g","kind":"gcs","bucket":"b","auth":"none","prefix":"p"}]"#, "remote").unwrap();
        assert_eq!((g[0].endpoint.as_str(), g[0].prefix.as_str()), ("https://storage.googleapis.com", "p/"));
        let s = load_specs(r#"[{"id":"s","bucket":"b","endpoint":"http://x/","access_key":"k","secret_key":"s"}]"#, "remote").unwrap();
        assert_eq!((s[0].kind.as_str(), s[0].region.as_str(), s[0].endpoint.as_str()), ("s3", "us-east-1", "http://x"));
    }
}
