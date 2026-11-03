//! Customer-managed keys: key-encryption keys that stay in the customer's key manager.
//!
//! An external key in the keyring file (`"external": {"<id>": {...}}`) is a HashiCorp Vault (or OpenBao)
//! Transit key: the store never holds it. Each object's data key is encrypted by Vault
//! (`POST /v1/<mount>/encrypt/<key>`) when the object is written, and decrypted by Vault when it is read.
//! The object's name is bound in: the plaintext Vault encrypts is the data key followed by a hash of
//! the bucket and key, checked on the way back, so a wrapped key copied to another object does not open.
//!
//! The customer stays in control: revoke the store's Vault token or policy, or delete the key, and
//! the objects can no longer be read (after `cache_seconds`, how long the store keeps a decrypted data
//! key in memory; default 60). Rotating the key in Vault needs nothing from the store: old versions
//! still decrypt, new writes use the latest.
//!
//! ```json
//! {"external": {"customer-a": {"type": "vault-transit", "addr": "https://vault:8200", "key": "cairn-a",
//!   "mount": "transit", "token_file": "/run/secrets/vault-token", "namespace": "", "cache_seconds": 60}}}
//! ```
//!
//! Or an AWS KMS key (`"type": "aws-kms"`; any service that speaks the KMS API): the data key is encrypted
//! with `Encrypt` and decrypted with `Decrypt`, signed with SigV4, and the object's name is also bound in as
//! the encryption context (`cairn-object`), which KMS checks and CloudTrail records. Revoke the store's
//! grant or IAM permission, or disable the key, and the objects can no longer be read.
//!
//! ```json
//! {"external": {"customer-b": {"type": "aws-kms", "region": "eu-west-1",
//!   "key": "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-...", "access_key": "AKIA...",
//!   "secret_key_file": "/run/secrets/kms-secret", "session_token_file": "", "addr": ""}}}
//! ```
//!
//! Without `access_key`, the store's environment gives the credentials (`AWS_ACCESS_KEY_ID`,
//! `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), read on every call so rotated credentials are picked up.
//! `addr` defaults to `https://kms.<region>.amazonaws.com`.
//!
//! Or an Azure Key Vault (or Managed HSM) RSA key (`"type": "azure-keyvault"`): the data key is wrapped
//! with `wrapkey` (RSA-OAEP-256) and unwrapped with `unwrapkey` by the key version that wrapped it, so a
//! rotated key still opens old objects. Sign-in (`auth`): `client-secret` (`tenant_id`, `client_id`,
//! `client_secret_file`), `workload-identity` (AKS: `tenant_id`, `client_id`, `federated_token_file`),
//! `managed-identity` (optionally `client_id`) or `token-file`. Disable the key or remove the store's
//! access, and the objects can no longer be read.
//!
//! ```json
//! {"external": {"customer-c": {"type": "azure-keyvault", "addr": "https://acme.vault.azure.net", "key": "cairn-c",
//!   "auth": "workload-identity", "tenant_id": "...", "client_id": "...",
//!   "federated_token_file": "/var/run/secrets/azure/tokens/azure-identity-token"}}}
//! ```
//!
//! Or a Google Cloud KMS key (`"type": "gcp-kms"`, `key` = its resource name): `encrypt` and `decrypt`, with
//! the object's name bound in as the additional authenticated data, which Cloud KMS checks. Sign-in
//! (`auth`): `service-account` (`credentials_file`, a service account key), `metadata` (GKE and GCE) or
//! `token-file`. `addr` defaults to `https://cloudkms.googleapis.com`.
//!
//! ```json
//! {"external": {"customer-d": {"type": "gcp-kms", "auth": "metadata",
//!   "key": "projects/acme/locations/europe-west1/keyRings/cairn/cryptoKeys/customer-d"}}}
//! ```
//!
//! Or a key in a KMIP server (`"type": "kmip"`: an HSM or key manager speaking KMIP 1.4, e.g. Thales
//! CipherTrust, Fortanix, Entrust KeyControl): `key` is the AES key's unique identifier, `addr` the
//! server's `host:port`, and the store signs in with a client certificate (`client_cert_file`,
//! `client_key_file`; `ca_file` checks the server). Encrypt and Decrypt in AES-GCM, the object's name
//! authenticated. Revoke or destroy the key, and the objects can no longer be read.
//!
//! ```json
//! {"external": {"customer-e": {"type": "kmip", "addr": "hsm.acme.internal:5696", "key": "4711",
//!   "ca_file": "/run/secrets/kmip/ca.pem", "client_cert_file": "/run/secrets/kmip/tls.crt",
//!   "client_key_file": "/run/secrets/kmip/tls.key"}}}
//! ```

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use base64::Engine as _;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

use crate::crypt::Key;

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::STANDARD;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExternalConfig {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub addr: String,
    pub key: String,
    #[serde(default = "default_mount")]
    pub mount: String,
    #[serde(default)]
    pub token_file: String,
    #[serde(default)]
    pub token: String,
    #[serde(default)]
    pub namespace: String,
    #[serde(default = "default_cache")]
    pub cache_seconds: u64,
    // aws-kms
    #[serde(default)]
    pub region: String,
    #[serde(default)]
    pub access_key: String,
    #[serde(default)]
    pub secret_key: String,
    #[serde(default)]
    pub secret_key_file: String,
    #[serde(default)]
    pub session_token_file: String,
    // azure-keyvault, gcp-kms: how the store signs in
    #[serde(default)]
    pub auth: String,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub tenant_id: String,
    #[serde(default)]
    pub client_id: String,
    #[serde(default)]
    pub client_secret_file: String,
    #[serde(default)]
    pub federated_token_file: String,
    #[serde(default)]
    pub credentials_file: String,
    /// The sign-in endpoint (Entra ID's login, the managed identity or metadata endpoint), for sovereign
    /// clouds and tests.
    #[serde(default)]
    pub auth_addr: String,
    // kmip
    #[serde(default)]
    pub ca_file: String,
    #[serde(default)]
    pub client_cert_file: String,
    #[serde(default)]
    pub client_key_file: String,
}

/// Which key manager, and how to sign in to it.
enum Backend {
    Vault { mount: String, namespace: String, token: String, token_file: Option<PathBuf> },
    Aws { region: String, access_key: String, secret_key: String, secret_key_file: Option<PathBuf>, session_token_file: Option<PathBuf> },
    Azure { version: String, auth: CloudAuth },
    Gcp { auth: CloudAuth },
    Kmip(super::kmip::Kmip),
}

/// How the store gets an OAuth access token for Azure Key Vault or Google Cloud KMS.
enum CloudAuth {
    TokenFile(PathBuf),
    AzureSecret { login: String, tenant: String, client: String, secret_file: PathBuf },
    AzureFederated { login: String, tenant: String, client: String, token_file: PathBuf },
    AzureManaged { endpoint: String, client: String },
    GcpServiceAccount { email: String, key_id: String, key: std::sync::Arc<ring::signature::RsaKeyPair>, token_uri: String },
    GcpMetadata { endpoint: String },
}

const AZURE_SCOPE: &str = "https://vault.azure.net/.default";
const GCP_SCOPE: &str = "https://www.googleapis.com/auth/cloudkms";
const B64URL: base64::engine::GeneralPurpose = base64::engine::general_purpose::URL_SAFE_NO_PAD;

fn guid(s: &str) -> bool {
    s.len() == 36 && s.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
}

fn plain_name(s: &str, max: usize) -> bool {
    !s.is_empty() && s.len() <= max && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
}

/// The private key of a PEM (PKCS#8) block.
fn pem_pkcs8(pem: &str) -> Result<Vec<u8>, String> {
    let body: String = pem.lines().filter(|l| !l.starts_with("-----")).collect();
    B64.decode(body.trim()).map_err(|e| format!("the private key: {e}"))
}

fn cloud_auth(c: &ExternalConfig, azure: bool) -> Result<CloudAuth, String> {
    let file = |f: &str, what: &str| -> Result<PathBuf, String> {
        if f.is_empty() {
            Err(format!("{} needs {what}", c.auth))
        } else {
            Ok(PathBuf::from(f))
        }
    };
    let need_ids = || -> Result<(), String> {
        if !guid(&c.tenant_id) || !guid(&c.client_id) {
            return Err("tenant_id and client_id: the app's directory (tenant) and client ids".into());
        }
        Ok(())
    };
    let login = || {
        if c.auth_addr.is_empty() {
            "https://login.microsoftonline.com".to_string()
        } else {
            c.auth_addr.trim_end_matches('/').to_string()
        }
    };
    match (azure, c.auth.as_str()) {
        (_, "token-file") => Ok(CloudAuth::TokenFile(file(&c.token_file, "token_file")?)),
        (true, "client-secret") => {
            need_ids()?;
            Ok(CloudAuth::AzureSecret {
                login: login(),
                tenant: c.tenant_id.clone(),
                client: c.client_id.clone(),
                secret_file: file(&c.client_secret_file, "client_secret_file")?,
            })
        }
        (true, "workload-identity") => {
            need_ids()?;
            Ok(CloudAuth::AzureFederated {
                login: login(),
                tenant: c.tenant_id.clone(),
                client: c.client_id.clone(),
                token_file: file(&c.federated_token_file, "federated_token_file")?,
            })
        }
        (true, "managed-identity") => {
            if !c.client_id.is_empty() && !guid(&c.client_id) {
                return Err("client_id: the user-assigned identity's client id".into());
            }
            let endpoint = if c.auth_addr.is_empty() { "http://169.254.169.254".into() } else { c.auth_addr.trim_end_matches('/').into() };
            Ok(CloudAuth::AzureManaged { endpoint, client: c.client_id.clone() })
        }
        (false, "service-account") => {
            let path = file(&c.credentials_file, "credentials_file")?;
            let raw = std::fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
            let j: serde_json::Value = serde_json::from_str(&raw).map_err(|e| format!("{}: {e}", path.display()))?;
            let text = |k: &str| j[k].as_str().unwrap_or_default().to_string();
            if text("type") != "service_account" || text("client_email").is_empty() || text("private_key").is_empty() {
                return Err(format!("{}: not a service account key", path.display()));
            }
            let der = pem_pkcs8(&text("private_key"))?;
            let key = ring::signature::RsaKeyPair::from_pkcs8(&der).map_err(|e| format!("{}: the private key: {e}", path.display()))?;
            let token_uri = if !c.auth_addr.is_empty() {
                c.auth_addr.clone()
            } else if text("token_uri").is_empty() {
                "https://oauth2.googleapis.com/token".into()
            } else {
                text("token_uri")
            };
            Ok(CloudAuth::GcpServiceAccount {
                email: text("client_email"),
                key_id: text("private_key_id"),
                key: std::sync::Arc::new(key),
                token_uri,
            })
        }
        (false, "metadata") => {
            let endpoint =
                if c.auth_addr.is_empty() { "http://metadata.google.internal".into() } else { c.auth_addr.trim_end_matches('/').into() };
            Ok(CloudAuth::GcpMetadata { endpoint })
        }
        (true, other) => Err(format!("auth {other:?}: client-secret, workload-identity, managed-identity or token-file")),
        (false, other) => Err(format!("auth {other:?}: service-account, metadata or token-file")),
    }
}

fn default_mount() -> String {
    "transit".into()
}

fn default_cache() -> u64 {
    60
}

/// Decrypted data keys, by (ciphertext, object binding), with when they were decrypted.
type Cache = Mutex<HashMap<(String, Vec<u8>), (Key, Instant)>>;

/// A key in a customer's key manager: a Vault Transit key or an AWS KMS key.
pub struct Transit {
    id: String,
    addr: String,
    key: String,
    backend: Backend,
    ttl: Duration,
    cache: Cache,
    token: Mutex<Option<(String, Instant)>>,
    /// Azure: the key's current version and when it was looked up.
    latest: Mutex<Option<(String, Instant)>>,
    /// Always Some until dropped (see Drop).
    rt: Option<tokio::runtime::Runtime>,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Full<Bytes>>,
}

fn safe(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/'))
        && !s.contains("..")
}

impl Drop for Transit {
    /// The store drops keyrings on its own runtime, where a runtime may not be dropped in place.
    fn drop(&mut self) {
        if let Some(rt) = self.rt.take() {
            rt.shutdown_background();
        }
    }
}

impl Transit {
    pub fn new(id: &str, c: ExternalConfig) -> Result<Transit, String> {
        let file = |f: &str| (!f.is_empty()).then(|| PathBuf::from(f));
        let (addr, backend) = match c.kind.as_str() {
            "vault-transit" => {
                if !safe(&c.key) || !safe(&c.mount) || (!c.namespace.is_empty() && !safe(&c.namespace)) {
                    return Err("key, mount and namespace are names".into());
                }
                if c.token.is_empty() == c.token_file.is_empty() {
                    return Err("give token_file (or token)".into());
                }
                if !c.region.is_empty() || !c.access_key.is_empty() || !c.secret_key.is_empty() || !c.secret_key_file.is_empty() {
                    return Err("region and AWS credentials are for aws-kms keys".into());
                }
                (c.addr, Backend::Vault { mount: c.mount, namespace: c.namespace, token: c.token, token_file: file(&c.token_file) })
            }
            "aws-kms" => {
                let region_ok =
                    !c.region.is_empty() && c.region.len() <= 32 && c.region.chars().all(|x| x.is_ascii_alphanumeric() || x == '-');
                if !region_ok {
                    return Err("region: the key's AWS region".into());
                }
                let key_ok = !c.key.is_empty()
                    && c.key.len() <= 2048
                    && c.key.chars().all(|x| x.is_ascii_alphanumeric() || matches!(x, '-' | '_' | '/' | ':'));
                if !key_ok {
                    return Err("key: the KMS key's id, ARN or alias".into());
                }
                if !c.token.is_empty() || !c.token_file.is_empty() || !c.namespace.is_empty() {
                    return Err("token and namespace are for vault-transit keys".into());
                }
                if !c.access_key.is_empty() && c.secret_key.is_empty() == c.secret_key_file.is_empty() {
                    return Err("with access_key, give secret_key_file (or secret_key)".into());
                }
                if c.access_key.is_empty() && (!c.secret_key.is_empty() || !c.secret_key_file.is_empty()) {
                    return Err("a secret key needs its access_key".into());
                }
                let addr = if c.addr.is_empty() { format!("https://kms.{}.amazonaws.com", c.region) } else { c.addr };
                (
                    addr,
                    Backend::Aws {
                        region: c.region,
                        access_key: c.access_key,
                        secret_key: c.secret_key,
                        secret_key_file: file(&c.secret_key_file),
                        session_token_file: file(&c.session_token_file),
                    },
                )
            }
            "azure-keyvault" | "gcp-kms" | "kmip" => {
                let theirs: &[(&str, &str)] = &[
                    ("region", &c.region),
                    ("access_key", &c.access_key),
                    ("secret_key", &c.secret_key),
                    ("secret_key_file", &c.secret_key_file),
                    ("namespace", &c.namespace),
                    ("token", &c.token),
                ];
                if let Some((name, _)) = theirs.iter().find(|(_, v)| !v.is_empty()) {
                    return Err(format!("{name} is not a {} setting", c.kind));
                }
                match c.kind.as_str() {
                    "azure-keyvault" => {
                        if !plain_name(&c.key, 127) || (!c.version.is_empty() && !plain_name(&c.version, 64)) {
                            return Err("key (and version): the Key Vault key's name and version".into());
                        }
                        if c.addr.is_empty() {
                            return Err("addr: the vault's address, https://<name>.vault.azure.net".into());
                        }
                        (c.addr.clone(), Backend::Azure { version: c.version.clone(), auth: cloud_auth(&c, true)? })
                    }
                    "gcp-kms" => {
                        let parts: Vec<&str> = c.key.split('/').collect();
                        let shape = parts.len() == 8
                            && parts.iter().step_by(2).copied().eq(["projects", "locations", "keyRings", "cryptoKeys"])
                            && parts.iter().skip(1).step_by(2).all(|p| plain_name(p, 128));
                        if !shape {
                            return Err("key: projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>".into());
                        }
                        let addr = if c.addr.is_empty() { "https://cloudkms.googleapis.com".to_string() } else { c.addr.clone() };
                        (addr, Backend::Gcp { auth: cloud_auth(&c, false)? })
                    }
                    _ => {
                        if c.key.is_empty() || c.key.len() > 256 || c.key.chars().any(|x| x.is_control()) {
                            return Err("key: the KMIP unique identifier of the AES key".into());
                        }
                        let (host, port) = c.addr.rsplit_once(':').ok_or("addr: the KMIP server's host:port")?;
                        let port: u16 = port.parse().map_err(|_| "addr: the KMIP server's host:port")?;
                        if host.is_empty() || host.contains('/') {
                            return Err("addr: the KMIP server's host:port".into());
                        }
                        for (name, v) in
                            [("ca_file", &c.ca_file), ("client_cert_file", &c.client_cert_file), ("client_key_file", &c.client_key_file)]
                        {
                            if v.is_empty() {
                                return Err(format!("kmip needs {name}"));
                            }
                        }
                        let tls = super::kmip::tls_config(
                            std::path::Path::new(&c.ca_file),
                            std::path::Path::new(&c.client_cert_file),
                            std::path::Path::new(&c.client_key_file),
                        )?;
                        (format!("kmip://{}", c.addr), Backend::Kmip(super::kmip::Kmip { host: host.to_string(), port, tls }))
                    }
                }
            }
            other => {
                return Err(format!(
                    "type {other:?}: the external key managers are vault-transit, aws-kms, azure-keyvault, gcp-kms and kmip"
                ))
            }
        };
        if !(addr.starts_with("https://") || addr.starts_with("http://") || addr.starts_with("kmip://")) {
            return Err("addr is the key manager's http(s):// address".into());
        }
        // Its own runtime: calls are made from synchronous code that may run on the store's runtime.
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .thread_name("cairn-kms")
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        let http = {
            let _g = rt.enter();
            Client::builder(TokioExecutor::new()).build(crate::tls::https_connector()?)
        };
        let t = Transit {
            id: id.into(),
            addr: addr.trim_end_matches('/').into(),
            key: c.key,
            backend,
            ttl: Duration::from_secs(c.cache_seconds),
            cache: Mutex::new(HashMap::new()),
            token: Mutex::new(None),
            latest: Mutex::new(None),
            rt: Some(rt),
            http,
        };
        t.credentials()?; // credentials that cannot be read stop the store at start-up
        Ok(t)
    }

    /// Read on every call, so rotated tokens and keys are picked up: Vault's token, or AWS's
    /// (access key, secret key, session token).
    fn credentials(&self) -> Result<(String, String, String), String> {
        let read = |f: &PathBuf| std::fs::read_to_string(f).map(|s| s.trim().to_string()).map_err(|e| format!("{}: {e}", f.display()));
        match &self.backend {
            Backend::Vault { token, token_file, .. } => {
                Ok((token_file.as_ref().map(read).transpose()?.unwrap_or_else(|| token.clone()), String::new(), String::new()))
            }
            Backend::Aws { access_key, secret_key, secret_key_file, session_token_file, .. } => {
                if access_key.is_empty() {
                    let env = |k: &str| std::env::var(k).unwrap_or_default();
                    let (ak, sk) = (env("AWS_ACCESS_KEY_ID"), env("AWS_SECRET_ACCESS_KEY"));
                    if ak.is_empty() || sk.is_empty() {
                        return Err("no AWS credentials (access_key, or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY)".into());
                    }
                    return Ok((ak, sk, env("AWS_SESSION_TOKEN")));
                }
                let sk = secret_key_file.as_ref().map(read).transpose()?.unwrap_or_else(|| secret_key.clone());
                let st = session_token_file.as_ref().map(read).transpose()?.unwrap_or_default();
                Ok((access_key.clone(), sk, st))
            }
            Backend::Azure { auth, .. } | Backend::Gcp { auth } => {
                // Files that must be readable at start-up; tokens themselves are fetched on use.
                match auth {
                    CloudAuth::TokenFile(f)
                    | CloudAuth::AzureSecret { secret_file: f, .. }
                    | CloudAuth::AzureFederated { token_file: f, .. } => {
                        read(f)?;
                    }
                    _ => {}
                }
                Ok((String::new(), String::new(), String::new()))
            }
            Backend::Kmip(_) => Ok((String::new(), String::new(), String::new())),
        }
    }

    /// An OAuth access token for Azure Key Vault or Cloud KMS: from the file on every call, or fetched and
    /// kept until a minute before it expires.
    fn bearer(&self) -> Result<String, String> {
        let auth = match &self.backend {
            Backend::Azure { auth, .. } | Backend::Gcp { auth } => auth,
            _ => unreachable!(),
        };
        let read = |f: &PathBuf| std::fs::read_to_string(f).map(|s| s.trim().to_string()).map_err(|e| format!("{}: {e}", f.display()));
        if let CloudAuth::TokenFile(f) = auth {
            return read(f);
        }
        if let Some((t, until)) = self.token.lock().unwrap().as_ref() {
            if Instant::now() < *until {
                return Ok(t.clone());
            }
        }
        let r = match auth {
            CloudAuth::TokenFile(_) => unreachable!(),
            CloudAuth::AzureSecret { login, tenant, client, secret_file } => self.form(
                &format!("{login}/{tenant}/oauth2/v2.0/token"),
                &[
                    ("grant_type", "client_credentials"),
                    ("client_id", client),
                    ("client_secret", &read(secret_file)?),
                    ("scope", AZURE_SCOPE),
                ],
            )?,
            CloudAuth::AzureFederated { login, tenant, client, token_file } => self.form(
                &format!("{login}/{tenant}/oauth2/v2.0/token"),
                &[
                    ("grant_type", "client_credentials"),
                    ("client_id", client),
                    ("client_assertion_type", "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"),
                    ("client_assertion", &read(token_file)?),
                    ("scope", AZURE_SCOPE),
                ],
            )?,
            CloudAuth::AzureManaged { endpoint, client } => {
                let mut url =
                    format!("{endpoint}/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net");
                if !client.is_empty() {
                    url.push_str(&format!("&client_id={client}"));
                }
                self.get(&url, &[("metadata", "true")])?
            }
            CloudAuth::GcpMetadata { endpoint } => self
                .get(&format!("{endpoint}/computeMetadata/v1/instance/service-accounts/default/token"), &[("metadata-flavor", "Google")])?,
            CloudAuth::GcpServiceAccount { email, key_id, key, token_uri } => {
                let now = time::OffsetDateTime::now_utc().unix_timestamp();
                let header = serde_json::json!({"alg": "RS256", "typ": "JWT", "kid": key_id});
                let claims = serde_json::json!({"iss": email, "scope": GCP_SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600});
                let signing = format!("{}.{}", B64URL.encode(header.to_string()), B64URL.encode(claims.to_string()));
                let mut sig = vec![0u8; key.public().modulus_len()];
                key.sign(&ring::signature::RSA_PKCS1_SHA256, &ring::rand::SystemRandom::new(), signing.as_bytes(), &mut sig)
                    .map_err(|_| "signing the service account's token request failed".to_string())?;
                let jwt = format!("{signing}.{}", B64URL.encode(sig));
                self.form(token_uri, &[("grant_type", "urn:ietf:params:oauth:grant-type:jwt-bearer"), ("assertion", &jwt)])?
            }
        };
        let token = r["access_token"].as_str().ok_or("the sign-in returned no access token")?.to_string();
        let secs = r["expires_in"].as_u64().or_else(|| r["expires_in"].as_str().and_then(|s| s.parse().ok())).unwrap_or(300);
        let until = Instant::now() + Duration::from_secs(secs.saturating_sub(60).max(10));
        *self.token.lock().unwrap() = Some((token.clone(), until));
        Ok(token)
    }

    /// The key's current version (wrapping names one): looked up and kept for `cache_seconds`, so a new
    /// version is used soon after the customer rotates the key.
    fn azure_latest(&self) -> Result<String, String> {
        if let Some((v, at)) = self.latest.lock().unwrap().as_ref() {
            if at.elapsed() < self.ttl {
                return Ok(v.clone());
            }
        }
        let bearer = format!("Bearer {}", self.bearer()?);
        let r = self.get(&format!("{}/keys/{}?api-version=7.4", self.addr, self.key), &[("authorization", &bearer)])?;
        let kid = r["key"]["kid"].as_str().unwrap_or_default();
        let v = kid.rsplit('/').next().filter(|v| plain_name(v, 64)).ok_or("the key manager returned no key version")?.to_string();
        *self.latest.lock().unwrap() = Some((v.clone(), Instant::now()));
        Ok(v)
    }

    fn form(&self, url: &str, fields: &[(&str, &str)]) -> Result<serde_json::Value, String> {
        let enc = |v: &str| percent_encoding::utf8_percent_encode(v, percent_encoding::NON_ALPHANUMERIC).to_string();
        let body = fields.iter().map(|(k, v)| format!("{k}={}", enc(v))).collect::<Vec<_>>().join("&");
        let req = hyper::Request::post(url).header("content-type", "application/x-www-form-urlencoded");
        self.send(req.body(Full::new(Bytes::from(body))).map_err(|e| e.to_string())?)
    }

    fn get(&self, url: &str, headers: &[(&str, &str)]) -> Result<serde_json::Value, String> {
        let mut req = hyper::Request::get(url);
        for (k, v) in headers {
            req = req.header(*k, *v);
        }
        self.send(req.body(Full::new(Bytes::new())).map_err(|e| e.to_string())?)
    }

    /// A JSON call to Azure Key Vault or Cloud KMS with the bearer token.
    fn cloud(&self, url: &str, body: serde_json::Value) -> Result<serde_json::Value, String> {
        let req = hyper::Request::post(url)
            .header("content-type", "application/json")
            .header("authorization", format!("Bearer {}", self.bearer()?));
        self.send(req.body(Full::new(Bytes::from(body.to_string()))).map_err(|e| e.to_string())?)
    }

    fn send(&self, req: hyper::Request<Full<Bytes>>) -> Result<serde_json::Value, String> {
        let fut = async {
            let resp = tokio::time::timeout(Duration::from_secs(10), self.http.request(req))
                .await
                .map_err(|_| "timed out".to_string())?
                .map_err(|e| e.to_string())?;
            let status = resp.status();
            let bytes = resp.into_body().collect().await.map(|b| b.to_bytes()).map_err(|e| e.to_string())?;
            if !status.is_success() {
                return Err(format!("HTTP {status}: {}", String::from_utf8_lossy(&bytes).chars().take(300).collect::<String>()));
            }
            serde_json::from_slice::<serde_json::Value>(&bytes).map_err(|e| e.to_string())
        };
        // On a plain thread, so this never blocks inside an async runtime's context.
        std::thread::scope(|s| s.spawn(|| self.rt.as_ref().expect("runtime").block_on(fut)).join())
            .map_err(|_| "key manager call panicked".to_string())?
    }

    fn vault(&self, op: &str, body: serde_json::Value) -> Result<serde_json::Value, String> {
        let Backend::Vault { mount, namespace, .. } = &self.backend else { unreachable!() };
        let url = format!("{}/v1/{}/{op}/{}", self.addr, mount, self.key);
        let mut req = hyper::Request::post(url.as_str())
            .header("content-type", "application/json")
            .header("x-vault-token", self.credentials()?.0)
            .header("x-vault-request", "true");
        if !namespace.is_empty() {
            req = req.header("x-vault-namespace", namespace);
        }
        self.send(req.body(Full::new(Bytes::from(body.to_string()))).map_err(|e| e.to_string())?)
    }

    /// A KMS API call (`TrentService.<op>`), signed with SigV4.
    fn aws(&self, op: &str, body: serde_json::Value) -> Result<serde_json::Value, String> {
        let Backend::Aws { region, .. } = &self.backend else { unreachable!() };
        let (ak, sk, st) = self.credentials()?;
        let payload = body.to_string();
        let url = format!("{}/", self.addr);
        let target = format!("TrentService.{op}");
        let mut extra: Vec<(&str, &str)> = vec![("content-type", "application/x-amz-json-1.1"), ("x-amz-target", target.as_str())];
        if !st.is_empty() {
            extra.push(("x-amz-security-token", st.as_str()));
        }
        let hash = hex::encode(Sha256::digest(payload.as_bytes()));
        let signed = crate::admin_cli::sign_service("POST", &url, "kms", &hash, &extra, &ak, &sk, region, time::OffsetDateTime::now_utc());
        let mut req = hyper::Request::post(url.as_str());
        for (k, v) in extra.iter() {
            req = req.header(*k, *v);
        }
        for (k, v) in signed {
            req = req.header(k, v);
        }
        self.send(req.body(Full::new(Bytes::from(payload))).map_err(|e| e.to_string())?)
    }

    fn context(aad: &[u8]) -> serde_json::Value {
        serde_json::json!({"cairn-object": hex::encode(Sha256::digest(aad))})
    }

    fn bound(dek: &Key, aad: &[u8]) -> Vec<u8> {
        let mut v = dek.to_vec();
        v.extend_from_slice(&Sha256::digest(aad));
        v
    }

    /// The data key encrypted by the key manager, bound to `aad`.
    pub fn wrap(&self, dek: &Key, aad: &[u8]) -> Result<String, String> {
        let plain = B64.encode(Self::bound(dek, aad));
        let ct = match self.backend {
            Backend::Vault { .. } => {
                let r = self.vault("encrypt", serde_json::json!({"plaintext": plain}))?;
                r["data"]["ciphertext"].as_str().ok_or("the key manager returned no ciphertext")?.to_string()
            }
            Backend::Aws { .. } => {
                let r = self
                    .aws("Encrypt", serde_json::json!({"KeyId": self.key, "Plaintext": plain, "EncryptionContext": Self::context(aad)}))?;
                r["CiphertextBlob"].as_str().ok_or("the key manager returned no ciphertext")?.to_string()
            }
            Backend::Azure { ref version, .. } => {
                let version = if version.is_empty() { self.azure_latest()? } else { version.clone() };
                let path = format!("{}/{version}", self.key);
                let r = self.cloud(
                    &format!("{}/keys/{path}/wrapkey?api-version=7.4", self.addr),
                    serde_json::json!({"alg": "RSA-OAEP-256", "value": B64URL.encode(Self::bound(dek, aad))}),
                )?;
                let value = r["value"].as_str().ok_or("the key manager returned no wrapped key")?;
                // Kept with the version that wrapped it: unwrapping names it, so a rotated key still opens old objects.
                let kid = r["kid"].as_str().unwrap_or_default();
                let used = kid.rsplit('/').next().filter(|v| plain_name(v, 64)).ok_or("the key manager returned no key version")?;
                format!("{used}.{value}")
            }
            Backend::Gcp { .. } => {
                let r = self.cloud(
                    &format!("{}/v1/{}:encrypt", self.addr, self.key),
                    serde_json::json!({"plaintext": plain, "additionalAuthenticatedData": B64.encode(Sha256::digest(aad))}),
                )?;
                r["ciphertext"].as_str().ok_or("the key manager returned no ciphertext")?.to_string()
            }
            Backend::Kmip(ref k) => B64.encode(k.encrypt(&self.key, &Self::bound(dek, aad), &Sha256::digest(aad))?),
        };
        self.cache.lock().unwrap().insert((ct.clone(), aad.to_vec()), (*dek, Instant::now()));
        Ok(ct)
    }

    /// The data key back from the key manager (or from the cache, for `cache_seconds`).
    pub fn unwrap(&self, ct: &str, aad: &[u8]) -> Result<Key, String> {
        let k = (ct.to_string(), aad.to_vec());
        {
            let mut cache = self.cache.lock().unwrap();
            if let Some((dek, at)) = cache.get(&k) {
                if at.elapsed() < self.ttl {
                    return Ok(*dek);
                }
                cache.remove(&k);
            }
            if cache.len() > 100_000 {
                let ttl = self.ttl;
                cache.retain(|_, (_, at)| at.elapsed() < ttl);
            }
        }
        let plain = match self.backend {
            Backend::Vault { .. } => {
                let r = self.vault("decrypt", serde_json::json!({"ciphertext": ct}))?;
                B64.decode(r["data"]["plaintext"].as_str().ok_or("the key manager returned no plaintext")?).map_err(|e| e.to_string())?
            }
            Backend::Aws { .. } => {
                // The key is named too, so a ciphertext of another key in the account is not decrypted.
                let r = self.aws(
                    "Decrypt",
                    serde_json::json!({"CiphertextBlob": ct, "KeyId": self.key, "EncryptionContext": Self::context(aad)}),
                )?;
                B64.decode(r["Plaintext"].as_str().ok_or("the key manager returned no plaintext")?).map_err(|e| e.to_string())?
            }
            Backend::Azure { .. } => {
                let (version, value) = ct.split_once('.').ok_or("a damaged wrapped key")?;
                if !plain_name(version, 64) {
                    return Err("a damaged wrapped key".into());
                }
                let r = self.cloud(
                    &format!("{}/keys/{}/{version}/unwrapkey?api-version=7.4", self.addr, self.key),
                    serde_json::json!({"alg": "RSA-OAEP-256", "value": value}),
                )?;
                B64URL.decode(r["value"].as_str().ok_or("the key manager returned no plaintext")?).map_err(|e| e.to_string())?
            }
            Backend::Gcp { .. } => {
                let r = self.cloud(
                    &format!("{}/v1/{}:decrypt", self.addr, self.key),
                    serde_json::json!({"ciphertext": ct, "additionalAuthenticatedData": B64.encode(Sha256::digest(aad))}),
                )?;
                B64.decode(r["plaintext"].as_str().ok_or("the key manager returned no plaintext")?).map_err(|e| e.to_string())?
            }
            Backend::Kmip(ref k) => k.decrypt(&self.key, &B64.decode(ct).map_err(|e| e.to_string())?, &Sha256::digest(aad))?,
        };
        if plain.len() != 64 {
            return Err("the key manager returned something that is not a data key".into());
        }
        let dek: Key = plain[..32].try_into().unwrap();
        if !bool::from(plain[32..].ct_eq(&Sha256::digest(aad)[..])) {
            return Err("the data key belongs to another object".into());
        }
        if !self.ttl.is_zero() {
            self.cache.lock().unwrap().insert(k, (dek, Instant::now()));
        }
        Ok(dek)
    }

    pub fn id(&self) -> &str {
        &self.id
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(v: serde_json::Value) -> Result<ExternalConfig, serde_json::Error> {
        serde_json::from_value(v)
    }

    #[test]
    fn configs_are_checked() {
        let ok = serde_json::json!({"type": "vault-transit", "addr": "http://127.0.0.1:1", "key": "k", "token": "t"});
        assert!(Transit::new("a", cfg(ok.clone()).unwrap()).is_ok());
        for (k, v) in [("type", "gcp-kms"), ("addr", "vault:8200"), ("key", "../x"), ("mount", "a b"), ("region", "eu-west-1")] {
            let mut bad = ok.clone();
            bad[k] = serde_json::json!(v);
            assert!(Transit::new("a", cfg(bad).unwrap()).is_err(), "{k}");
        }
        let mut both = ok.clone();
        both["token_file"] = serde_json::json!("/x");
        assert!(Transit::new("a", cfg(both).unwrap()).is_err());
        let mut missing = ok.clone();
        missing["token"] = serde_json::json!("");
        missing["token_file"] = serde_json::json!("/does/not/exist");
        assert!(Transit::new("a", cfg(missing).unwrap()).is_err());
        let mut extra = ok;
        extra["secret"] = serde_json::json!("x");
        assert!(cfg(extra).is_err());
    }

    #[test]
    fn aws_configs_are_checked() {
        let ok = serde_json::json!({"type": "aws-kms", "region": "eu-west-1", "key": "arn:aws:kms:eu-west-1:111122223333:key/ab-12",
                                    "access_key": "AKIDEXAMPLE", "secret_key": "s"});
        let t = Transit::new("b", cfg(ok.clone()).unwrap()).unwrap();
        assert_eq!(t.addr, "https://kms.eu-west-1.amazonaws.com");
        for (k, v) in [("region", ""), ("region", "eu west"), ("key", "a b"), ("token", "t"), ("addr", "kms.local")] {
            let mut bad = ok.clone();
            bad[k] = serde_json::json!(v);
            assert!(Transit::new("b", cfg(bad).unwrap()).is_err(), "{k}={v}");
        }
        let mut both = ok.clone();
        both["secret_key_file"] = serde_json::json!("/x");
        assert!(Transit::new("b", cfg(both).unwrap()).is_err());
        let mut orphan = ok;
        orphan["access_key"] = serde_json::json!("");
        assert!(Transit::new("b", cfg(orphan).unwrap()).is_err());
    }

    #[test]
    fn azure_gcp_and_kmip_configs_are_checked() {
        let dir = std::env::temp_dir().join(format!("cairn-kms-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let tok = dir.join("token");
        std::fs::write(&tok, "t").unwrap();
        let tok = tok.to_str().unwrap();
        let id = "11111111-2222-3333-4444-555555555555";
        let az = serde_json::json!({"type": "azure-keyvault", "addr": "https://acme.vault.azure.net", "key": "k",
                                    "auth": "client-secret", "tenant_id": id, "client_id": id, "client_secret_file": tok});
        assert!(Transit::new("c", cfg(az.clone()).unwrap()).is_ok());
        for (k, v) in [
            ("auth", "service-account"),
            ("tenant_id", "acme"),
            ("key", "a/b"),
            ("addr", ""),
            ("region", "x"),
            ("client_secret_file", "/does/not/exist"),
        ] {
            let mut bad = az.clone();
            bad[k] = serde_json::json!(v);
            assert!(Transit::new("c", cfg(bad).unwrap()).is_err(), "azure {k}={v}");
        }
        let mi =
            serde_json::json!({"type": "azure-keyvault", "addr": "https://acme.vault.azure.net", "key": "k", "auth": "managed-identity"});
        assert!(Transit::new("c", cfg(mi).unwrap()).is_ok());
        let gcp = serde_json::json!({"type": "gcp-kms", "key": "projects/p/locations/global/keyRings/r/cryptoKeys/k", "auth": "metadata"});
        let t = Transit::new("d", cfg(gcp.clone()).unwrap()).unwrap();
        assert_eq!(t.addr, "https://cloudkms.googleapis.com");
        for (k, v) in [("key", "projects/p/keyRings/r/cryptoKeys/k"), ("auth", "client-secret"), ("auth", "service-account")] {
            let mut bad = gcp.clone();
            bad[k] = serde_json::json!(v);
            assert!(Transit::new("d", cfg(bad).unwrap()).is_err(), "gcp {k}={v}");
        }
        let kmip = serde_json::json!({"type": "kmip", "addr": "hsm:5696", "key": "1", "ca_file": "/x", "client_cert_file": "/x",
                                      "client_key_file": "/x"});
        assert!(Transit::new("e", cfg(kmip.clone()).unwrap()).is_err()); // unreadable certificates stop the store
        for (k, v) in [("addr", "hsm"), ("addr", "https://hsm:5696"), ("ca_file", "")] {
            let mut bad = kmip.clone();
            bad[k] = serde_json::json!(v);
            assert!(Transit::new("e", cfg(bad).unwrap()).is_err(), "kmip {k}={v}");
        }
        std::fs::remove_dir_all(&dir).ok();
    }
}
