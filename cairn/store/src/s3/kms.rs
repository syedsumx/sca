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
}

fn default_mount() -> String {
    "transit".into()
}

fn default_cache() -> u64 {
    60
}

/// Decrypted data keys, by (ciphertext, object binding), with when they were decrypted.
type Cache = Mutex<HashMap<(String, Vec<u8>), (Key, Instant)>>;

/// A Vault Transit key.
pub struct Transit {
    id: String,
    addr: String,
    mount: String,
    key: String,
    namespace: String,
    token: String,
    token_file: Option<PathBuf>,
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
        if c.kind != "vault-transit" {
            return Err(format!("type {:?}: the external key managers are vault-transit", c.kind));
        }
        if !(c.addr.starts_with("https://") || c.addr.starts_with("http://")) {
            return Err("addr is the key manager's http(s):// address".into());
        }
        if !safe(&c.key) || !safe(&c.mount) || (!c.namespace.is_empty() && !safe(&c.namespace)) {
            return Err("key, mount and namespace are names".into());
        }
        if c.token.is_empty() == c.token_file.is_empty() {
            return Err("give token_file (or token)".into());
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
            addr: c.addr.trim_end_matches('/').into(),
            mount: c.mount,
            key: c.key,
            namespace: c.namespace,
            token: c.token,
            token_file: (!c.token_file.is_empty()).then(|| PathBuf::from(c.token_file)),
            ttl: Duration::from_secs(c.cache_seconds),
            cache: Mutex::new(HashMap::new()),
            rt,
            http,
        };
        t.token()?; // a token that cannot be read stops the store at start-up
        Ok(t)
    }

    /// Read on every call, so a rotated token file is picked up.
    fn token(&self) -> Result<String, String> {
        match &self.token_file {
            Some(f) => std::fs::read_to_string(f).map(|s| s.trim().to_string()).map_err(|e| format!("{}: {e}", f.display())),
            None => Ok(self.token.clone()),
        }
    }

    fn call(&self, op: &str, body: serde_json::Value) -> Result<serde_json::Value, String> {
        let url = format!("{}/v1/{}/{op}/{}", self.addr, self.mount, self.key);
        let mut req = hyper::Request::post(url.as_str())
            .header("content-type", "application/json")
            .header("x-vault-token", self.token()?)
            .header("x-vault-request", "true");
        if !self.namespace.is_empty() {
            req = req.header("x-vault-namespace", &self.namespace);
        }
        let req = req.body(Full::new(Bytes::from(body.to_string()))).map_err(|e| e.to_string())?;
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

    fn bound(dek: &Key, aad: &[u8]) -> Vec<u8> {
        let mut v = dek.to_vec();
        v.extend_from_slice(&Sha256::digest(aad));
        v
    }

    /// The data key encrypted by the key manager, bound to `aad`.
    pub fn wrap(&self, dek: &Key, aad: &[u8]) -> Result<String, String> {
        let r = self.call("encrypt", serde_json::json!({"plaintext": B64.encode(Self::bound(dek, aad))}))?;
        let ct = r["data"]["ciphertext"].as_str().ok_or("the key manager returned no ciphertext")?.to_string();
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
        let r = self.call("decrypt", serde_json::json!({"ciphertext": ct}))?;
        let plain =
            B64.decode(r["data"]["plaintext"].as_str().ok_or("the key manager returned no plaintext")?).map_err(|e| e.to_string())?;
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
        for (k, v) in [("type", "aws-kms"), ("addr", "vault:8200"), ("key", "../x"), ("mount", "a b")] {
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
}
