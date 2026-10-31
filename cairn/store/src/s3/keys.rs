//! Access keys managed while the store runs, through the admin API (`/-/admin/v1/keys`) and
//! `cairn-store admin key-*`.
//!
//! Keys from the environment and `CAIRN_STORE_CREDENTIALS_FILE` stay as they are (they are listed, but
//! cannot be changed here). Keys created through the API live in the index: the store generates every
//! secret (a 256-bit random value, returned once when the key is created or rotated), and stores it
//! wrapped (AES-256-GCM) by a keyring key when the store has one (`CAIRN_STORE_MASTER_KEY` or
//! `CAIRN_STORE_KEYRING_FILE`), in the clear otherwise. A key whose secret was wrapped by a keyring key
//! that is no longer configured stops the store from starting rather than being dropped. Each change is
//! written in one transaction with its audit entry and takes effect on the next request (S3, file API,
//! WebDAV, SFTP, admin API); a deleted key's open SFTP sessions end when they next authorize.

use std::collections::BTreeMap;
use std::sync::Arc;

use base64::Engine as _;
use hyper::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::auth::Credential;
use super::errors::{self as e, ApiError};
use super::{policy, Server};

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::URL_SAFE_NO_PAD;

/// Every key the store accepts, with their parsed key policies.
#[derive(Default)]
pub struct KeySet {
    pub creds: BTreeMap<String, Credential>,
    pub policies: BTreeMap<String, policy::Policy>,
    /// Keys from the environment or the credentials file (read-only here).
    pub fixed: std::collections::BTreeSet<String>,
}

impl KeySet {
    pub fn insert(&mut self, c: Credential) -> Result<(), String> {
        if c.access_key.is_empty() || c.secret_key.len() < 8 {
            return Err(format!("credential {:?}: an access key and a secret of at least 8 characters are required", c.access_key));
        }
        if let Some(p) = &c.policy {
            let parsed = policy::Policy::parse(p.to_string().as_bytes(), None)
                .map_err(|err| format!("credential {:?}: policy: {}", c.access_key, err.message))?;
            self.policies.insert(c.access_key.clone(), parsed);
        }
        self.creds.insert(c.access_key.clone(), c);
        Ok(())
    }
}

/// A key as stored in the index.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct StoredKey {
    pub access_key: String,
    /// The secret in the clear (no keyring), or...
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub secret: String,
    /// ... wrapped by the keyring key `kek`.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub secret_wrapped: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub kek: String,
    #[serde(default)]
    pub buckets: Vec<String>,
    #[serde(default)]
    pub read_only: bool,
    #[serde(default)]
    pub admin: bool,
    #[serde(default)]
    pub policy: Option<Value>,
    #[serde(default)]
    pub actor: String,
    #[serde(default)]
    pub ssh_keys: Vec<String>,
    #[serde(default)]
    pub description: String,
    pub created: String,
    pub created_by: String,
    #[serde(default)]
    pub rotated: String,
}

/// The settable parts of a key (create and update).
#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct KeySpec {
    #[serde(default)]
    access_key: Option<String>,
    #[serde(default)]
    buckets: Option<Vec<String>>,
    #[serde(default)]
    read_only: Option<bool>,
    #[serde(default)]
    admin: Option<bool>,
    #[serde(default)]
    policy: Option<Value>,
    #[serde(default)]
    actor: Option<String>,
    #[serde(default)]
    ssh_keys: Option<Vec<String>>,
    #[serde(default)]
    description: Option<String>,
}

fn aad(access_key: &str) -> Vec<u8> {
    format!("cairn-store/access-key/{access_key}").into_bytes()
}

fn valid_access_key(ak: &str) -> bool {
    (3..=64).contains(&ak.len()) && ak.chars().all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
}

fn now() -> String {
    super::iso_time(crate::engine::now_ms())
}

impl Server {
    /// The current key set (cheap: shared until the next change).
    pub(crate) fn keyset(&self) -> Arc<KeySet> {
        self.keys.read().unwrap().clone()
    }

    /// A stored key's credential, unwrapping its secret.
    fn credential_of(&self, k: &StoredKey) -> Result<Credential, String> {
        let secret = if k.secret_wrapped.is_empty() {
            k.secret.clone()
        } else {
            let kek = self.keyring.keys.get(&k.kek).ok_or_else(|| {
                format!("access key {}: its secret is wrapped by keyring key {:?}, which is not configured", k.access_key, k.kek)
            })?;
            let wrapped = B64.decode(&k.secret_wrapped).map_err(|_| format!("access key {}: bad wrapped secret", k.access_key))?;
            let raw = crate::crypt::unwrap(kek, &wrapped, &aad(&k.access_key))
                .ok_or_else(|| format!("access key {}: the secret does not unwrap with keyring key {:?}", k.access_key, k.kek))?;
            B64.encode(raw)
        };
        Ok(Credential {
            access_key: k.access_key.clone(),
            secret_key: secret,
            buckets: k.buckets.clone(),
            read_only: k.read_only,
            admin: k.admin,
            policy: k.policy.clone(),
            actor: k.actor.clone(),
            ssh_keys: k.ssh_keys.clone(),
        })
    }

    /// Rebuilds the key set from the fixed keys and the stored ones.
    pub(crate) fn reload_keys(&self) -> Result<(), String> {
        let fixed = self.keyset();
        let mut next = KeySet { fixed: fixed.fixed.clone(), ..Default::default() };
        for ak in &fixed.fixed {
            if let Some(c) = fixed.creds.get(ak) {
                next.insert(c.clone())?;
            }
        }
        for (ak, rec) in self.eng.keys().map_err(|e| e.to_string())? {
            if next.fixed.contains(&ak) {
                return Err(format!("access key {ak} is defined both in the configuration and in the index"));
            }
            let k: StoredKey = serde_json::from_slice(&rec).map_err(|e| format!("access key {ak}: {e}"))?;
            next.insert(self.credential_of(&k)?)?;
        }
        *self.keys.write().unwrap() = Arc::new(next);
        Ok(())
    }

    fn stored(&self, ak: &str) -> Result<Option<StoredKey>, ApiError> {
        for (k, rec) in self.eng.keys()? {
            if k == ak {
                return serde_json::from_slice(&rec).map(Some).map_err(|_| e::INTERNAL.clone());
            }
        }
        Ok(None)
    }

    /// A new secret, set (wrapped when possible) on `k`; returns the secret.
    fn new_secret(&self, k: &mut StoredKey) -> String {
        let raw: [u8; 32] = rand::random();
        let kek_id = if !self.keyring.sse_s3_key.is_empty() {
            Some(self.keyring.sse_s3_key.clone())
        } else {
            self.keyring.keys.keys().next().cloned()
        };
        match kek_id.and_then(|id| self.keyring.keys.get(&id).map(|kek| (id, kek))) {
            Some((id, kek)) => {
                k.secret_wrapped = B64.encode(crate::crypt::wrap(kek, &raw, &aad(&k.access_key)));
                k.kek = id;
                k.secret.clear();
            }
            None => {
                k.secret = B64.encode(raw);
                k.secret_wrapped.clear();
                k.kek.clear();
            }
        }
        B64.encode(raw)
    }

    fn key_json(&self, ks: &KeySet, ak: &str) -> Value {
        let c = &ks.creds[ak];
        let stored = if ks.fixed.contains(ak) { None } else { self.stored(ak).ok().flatten() };
        json!({
            "access_key": ak,
            "source": if ks.fixed.contains(ak) { "configuration" } else { "api" },
            "admin": c.admin,
            "read_only": c.read_only,
            "buckets": c.buckets,
            "policy": c.policy,
            "actor": c.actor,
            "ssh_keys": c.ssh_keys.len(),
            "description": stored.as_ref().map(|s| s.description.clone()).unwrap_or_default(),
            "created": stored.as_ref().map(|s| s.created.clone()),
            "created_by": stored.as_ref().map(|s| s.created_by.clone()),
            "rotated": stored.as_ref().map(|s| s.rotated.clone()).filter(|r| !r.is_empty()),
            "secret_protected": stored.as_ref().map(|s| !s.secret_wrapped.is_empty()),
        })
    }

    /// Validates a key's settings the way start-up validates the credentials file.
    fn check_spec(k: &StoredKey) -> Result<(), ApiError> {
        if let Some(p) = &k.policy {
            policy::Policy::parse(p.to_string().as_bytes(), None).map_err(|_| e::KEY_POLICY.clone())?;
        }
        for s in &k.ssh_keys {
            russh::keys::PublicKey::from_openssh(s.trim()).map_err(|_| e::KEY_SSH.clone())?;
        }
        if k.description.len() > 256 || k.actor.len() > 256 {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        Ok(())
    }

    fn apply(spec: &KeySpec, k: &mut StoredKey) {
        if let Some(v) = &spec.buckets {
            k.buckets = v.clone();
        }
        if let Some(v) = spec.read_only {
            k.read_only = v;
        }
        if let Some(v) = spec.admin {
            k.admin = v;
        }
        if spec.policy.is_some() {
            k.policy = spec.policy.clone().filter(|p| !p.is_null());
        }
        if let Some(v) = &spec.actor {
            k.actor = v.clone();
        }
        if let Some(v) = &spec.ssh_keys {
            k.ssh_keys = v.clone();
        }
        if let Some(v) = &spec.description {
            k.description = v.clone();
        }
    }

    fn save(&self, k: &StoredKey, action: &str, actor: &str, details: Value) -> Result<(), ApiError> {
        let rec = serde_json::to_vec(k).map_err(|_| e::INTERNAL.clone())?;
        let audit =
            crate::audit::Audit { category: "store", action: action.into(), actor: actor.into(), subject: k.access_key.clone(), details };
        self.eng.put_key(&k.access_key, Some(&rec), Some(&audit))?;
        self.reload_keys().map_err(|err| {
            tracing::error!(error = %err, "key reload");
            e::INTERNAL.clone()
        })
    }

    /// `/-/admin/v1/keys...` (the caller is an admin key: `actor`).
    pub(crate) fn keys_api(
        &self,
        method: &hyper::Method,
        segs: &[&str],
        body: &[u8],
        actor: &str,
    ) -> Result<(StatusCode, Value), ApiError> {
        use hyper::Method;
        let ks = self.keyset();
        match (method, segs) {
            (&Method::GET, []) => {
                Ok((StatusCode::OK, json!({"keys": ks.creds.keys().map(|ak| self.key_json(&ks, ak)).collect::<Vec<_>>()})))
            }
            (&Method::GET, [ak]) => {
                if !ks.creds.contains_key(*ak) {
                    return Err(e::KEY_NOT_FOUND.clone());
                }
                Ok((StatusCode::OK, self.key_json(&ks, ak)))
            }
            (&Method::POST, []) => {
                let spec: KeySpec =
                    serde_json::from_slice(if body.is_empty() { b"{}" } else { body }).map_err(|_| e::MALFORMED_JSON.clone())?;
                let ak = match &spec.access_key {
                    Some(a) => a.clone(),
                    None => format!("CK{}", hex::encode_upper(rand::random::<[u8; 9]>())),
                };
                if !valid_access_key(&ak) {
                    return Err(e::KEY_NAME.clone());
                }
                if ks.creds.contains_key(&ak) {
                    return Err(e::KEY_EXISTS.clone());
                }
                let mut k = StoredKey { access_key: ak.clone(), created: now(), created_by: actor.into(), ..Default::default() };
                Self::apply(&spec, &mut k);
                Self::check_spec(&k)?;
                let secret = self.new_secret(&mut k);
                self.save(
                    &k,
                    "key.create",
                    actor,
                    json!({"admin": k.admin, "read_only": k.read_only, "buckets": k.buckets, "policy": k.policy.is_some()}),
                )?;
                tracing::info!(access_key = %ak, by = actor, admin = k.admin, "access key created");
                let mut v = self.key_json(&self.keyset(), &ak);
                v["secret_key"] = json!(secret);
                Ok((StatusCode::CREATED, v))
            }
            (&Method::PATCH, [ak]) | (&Method::PUT, [ak]) => {
                if ks.fixed.contains(*ak) {
                    return Err(e::KEY_FIXED.clone());
                }
                let mut k = self.stored(ak)?.ok_or_else(|| e::KEY_NOT_FOUND.clone())?;
                let spec: KeySpec = serde_json::from_slice(body).map_err(|_| e::MALFORMED_JSON.clone())?;
                if spec.access_key.as_deref().is_some_and(|a| a != *ak) {
                    return Err(e::KEY_NAME.clone());
                }
                Self::apply(&spec, &mut k);
                Self::check_spec(&k)?;
                self.save(
                    &k,
                    "key.update",
                    actor,
                    json!({"admin": k.admin, "read_only": k.read_only, "buckets": k.buckets, "policy": k.policy.is_some()}),
                )?;
                Ok((StatusCode::OK, self.key_json(&self.keyset(), ak)))
            }
            (&Method::POST, [ak, "rotate"]) => {
                if ks.fixed.contains(*ak) {
                    return Err(e::KEY_FIXED.clone());
                }
                let mut k = self.stored(ak)?.ok_or_else(|| e::KEY_NOT_FOUND.clone())?;
                let secret = self.new_secret(&mut k);
                k.rotated = now();
                self.save(&k, "key.rotate", actor, json!({}))?;
                tracing::info!(access_key = %ak, by = actor, "access key rotated");
                let mut v = self.key_json(&self.keyset(), ak);
                v["secret_key"] = json!(secret);
                Ok((StatusCode::OK, v))
            }
            (&Method::DELETE, [ak]) => {
                if ks.fixed.contains(*ak) {
                    return Err(e::KEY_FIXED.clone());
                }
                if *ak == actor {
                    return Err(e::KEY_SELF.clone());
                }
                let audit = crate::audit::Audit {
                    category: "store",
                    action: "key.delete".into(),
                    actor: actor.into(),
                    subject: ak.to_string(),
                    details: json!({}),
                };
                self.eng.put_key(ak, None, Some(&audit)).map_err(|err| match err {
                    crate::engine::Error::NoSuchKey => e::KEY_NOT_FOUND.clone(),
                    other => other.into(),
                })?;
                self.reload_keys().map_err(|_| e::INTERNAL.clone())?;
                tracing::info!(access_key = %ak, by = actor, "access key deleted");
                Ok((StatusCode::NO_CONTENT, Value::Null))
            }
            _ => Err(e::ADMIN_NOT_FOUND.clone()),
        }
    }
}
