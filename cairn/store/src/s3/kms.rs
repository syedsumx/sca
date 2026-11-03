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
}

/// Which key manager, and how to sign in to it.
enum Backend {
    Vault { mount: String, namespace: String, token: String, token_file: Option<PathBuf> },
    Aws { region: String, access_key: String, secret_key: String, secret_key_file: Option<PathBuf>, session_token_file: Option<PathBuf> },
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
    rt: tokio::runtime::Runtime,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Full<Bytes>>,
}

fn safe(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/'))
        && !s.contains("..")
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
            other => return Err(format!("type {other:?}: the external key managers are vault-transit and aws-kms")),
        };
        if !(addr.starts_with("https://") || addr.starts_with("http://")) {
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
            rt,
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
        }
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
        std::thread::scope(|s| s.spawn(|| self.rt.block_on(fut)).join()).map_err(|_| "key manager call panicked".to_string())?
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
}
