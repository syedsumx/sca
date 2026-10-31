//! Server-side encryption: SSE-S3 (keyring key), SSE-KMS (a named keyring key), SSE-C (customer key),
//! bucket default encryption, and the store default (encrypt every new object when a keyring is set).
//!
//! Keyring: `CAIRN_STORE_KEYRING_FILE` (`{"keys": {"<id>": "<base64 32 bytes>"}, "sse_s3_key": "<id>"}`) or
//! `CAIRN_STORE_MASTER_KEY` (one base64 key, id "default"). Rotation: add a key, point `sse_s3_key` at it;
//! objects keep the key they were written with, so old keys stay in the keyring until those are gone.
//! Customer keys arrive in request headers, so SSE-C is refused over plain HTTP unless the deployment
//! terminates TLS in front of the store and says so (`CAIRN_STORE_SSE_C_ALLOW_HTTP=true`).

use std::collections::BTreeMap;

use base64::Engine as _;
use bytes::Bytes;
use hyper::StatusCode;
use md5::Digest;
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, set_header, xml_resp, Ctx, Resp, Server};
use crate::crypt::{self, Key};
use crate::engine::Sse;

type R = Result<Resp, ApiError>;

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::STANDARD;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

/// Key-encryption keys, by id.
#[derive(Clone, Default)]
pub struct Keyring {
    pub keys: BTreeMap<String, Key>,
    /// The key SSE-S3 (and the store default) uses; empty: SSE-S3 is not available.
    pub sse_s3_key: String,
}

#[derive(Deserialize)]
struct KeyringFile {
    keys: BTreeMap<String, String>,
    #[serde(default)]
    sse_s3_key: String,
}

impl Keyring {
    pub fn from_json(text: &str) -> Result<Keyring, String> {
        let f: KeyringFile = serde_json::from_str(text).map_err(|e| e.to_string())?;
        let mut keys = BTreeMap::new();
        for (id, v) in f.keys {
            keys.insert(id.clone(), decode_key(&v).map_err(|e| format!("key {id:?}: {e}"))?);
        }
        if !f.sse_s3_key.is_empty() && !keys.contains_key(&f.sse_s3_key) {
            return Err(format!("sse_s3_key {:?} is not in keys", f.sse_s3_key));
        }
        Ok(Keyring { keys, sse_s3_key: f.sse_s3_key })
    }

    pub fn single(b64: &str) -> Result<Keyring, String> {
        let k = decode_key(b64)?;
        Ok(Keyring { keys: [("default".to_string(), k)].into(), sse_s3_key: "default".into() })
    }
}

fn decode_key(b64: &str) -> Result<Key, String> {
    B64.decode(b64.trim()).ok().and_then(|b| b.try_into().ok()).ok_or_else(|| "must be 32 bytes, base64".to_string())
}

/// Binds a wrapped data key to the object it belongs to.
fn aad(bucket: &str, key: &str) -> Vec<u8> {
    format!("cairn-store/{bucket}/{key}").into_bytes()
}

/// A bucket's default encryption.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct BucketEncryption {
    pub alg: String,
    #[serde(default)]
    pub key_id: String,
    #[serde(default)]
    pub bucket_key: bool,
}

fn hmac_key(salt: &[u8], key: &Key) -> String {
    B64.encode(super::auth::hmac_sha256(salt, key))
}

impl Server {
    /// A customer key from SSE-C headers (`copy`: the x-amz-copy-source-... ones). None: no SSE-C headers.
    fn customer_key(&self, c: &Ctx, copy: bool) -> Result<Option<(Key, String)>, ApiError> {
        let p = if copy { "x-amz-copy-source-server-side-encryption-customer-" } else { "x-amz-server-side-encryption-customer-" };
        let (alg, key, md5) = (c.header(&format!("{p}algorithm")), c.header(&format!("{p}key")), c.header(&format!("{p}key-md5")));
        if alg.is_empty() && key.is_empty() && md5.is_empty() {
            return Ok(None);
        }
        if !self.tls && !self.sse_c_over_http {
            return Err(e::SSE_C_NEEDS_TLS.clone());
        }
        if alg != "AES256" || key.is_empty() || md5.is_empty() {
            return Err(e::SSE_C_HEADERS.clone());
        }
        let k: Key = B64.decode(key).ok().and_then(|b| b.try_into().ok()).ok_or_else(|| e::SSE_C_HEADERS.clone())?;
        if B64.encode(md5::Md5::digest(k)) != md5 {
            return Err(e::SSE_C_KEY_MD5.clone());
        }
        Ok(Some((k, md5.to_string())))
    }

    fn bucket_encryption(&self, bucket: &str) -> Result<Option<BucketEncryption>, ApiError> {
        match blocking(|| self.eng.get_config(bucket, "encryption"))? {
            None => Ok(None),
            Some(d) => Ok(Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?)),
        }
    }

    fn keyring_sse(&self, alg: &str, key_id: &str, bucket: &str, key: &str) -> Result<(Sse, Key), ApiError> {
        let id = if alg == "AES256" { self.keyring.sse_s3_key.clone() } else { key_id.to_string() };
        if alg == "AES256" && id.is_empty() {
            return Err(e::SSE_NOT_CONFIGURED.clone());
        }
        if id.is_empty() {
            return Err(e::KMS_KEY_REQUIRED.clone());
        }
        let kek = self.keyring.keys.get(&id).ok_or_else(|| e::KMS_NOT_FOUND.clone())?;
        let dek = crypt::random_key();
        let wrapped = B64.encode(crypt::wrap(kek, &dek, &aad(bucket, key)));
        Ok((Sse { alg: alg.into(), key_id: id, wrapped, ..Default::default() }, dek))
    }

    /// How a new version of `bucket/key` is encrypted, and its data key: from the request's SSE headers,
    /// else the bucket's default encryption, else the store default. None: stored in the clear.
    pub(crate) fn sse_for_write(&self, c: &Ctx, bucket: &str, key: &str) -> Result<Option<(Sse, Key)>, ApiError> {
        let alg = c.header("x-amz-server-side-encryption");
        let kms_id = c.header("x-amz-server-side-encryption-aws-kms-key-id");
        let customer = self.customer_key(c, false)?;
        if let Some((k, md5)) = customer {
            if !alg.is_empty() || !kms_id.is_empty() {
                return Err(e::SSE_CONFLICT.clone());
            }
            let salt: [u8; 16] = rand::random();
            let sse =
                Sse { alg: "SSE-C".into(), key_md5: md5, key_hmac: hmac_key(&salt, &k), salt: B64.encode(salt), ..Default::default() };
            return Ok(Some((sse, k)));
        }
        match alg {
            "AES256" if kms_id.is_empty() => return self.keyring_sse("AES256", "", bucket, key).map(Some),
            "aws:kms" => {
                let id = if kms_id.is_empty() {
                    self.bucket_encryption(bucket)?.filter(|b| b.alg == "aws:kms").map(|b| b.key_id).unwrap_or_default()
                } else {
                    kms_id.to_string()
                };
                return self.keyring_sse("aws:kms", &id, bucket, key).map(Some);
            }
            "aws:kms:dsse" => return Err(e::SSE_DSSE_UNSUPPORTED.clone()),
            "" if kms_id.is_empty() => {}
            _ => return Err(e::SSE_INVALID.clone()),
        }
        if let Some(b) = self.bucket_encryption(bucket)? {
            return self.keyring_sse(&b.alg, &b.key_id, bucket, key).map(Some);
        }
        if self.encrypt_by_default && !self.keyring.sse_s3_key.is_empty() {
            return self.keyring_sse("AES256", "", bucket, key).map(Some);
        }
        Ok(None)
    }

    /// The encryption a write without SSE headers gets: the bucket default, else the store default.
    pub(crate) fn default_sse(&self, bucket: &str, key: &str) -> Result<Option<(Sse, Key)>, ApiError> {
        if let Some(b) = self.bucket_encryption(bucket)? {
            return self.keyring_sse(&b.alg, &b.key_id, bucket, key).map(Some);
        }
        if self.encrypt_by_default && !self.keyring.sse_s3_key.is_empty() {
            return self.keyring_sse("AES256", "", bucket, key).map(Some);
        }
        Ok(None)
    }

    /// The data key to read a version encrypted as `sse` (`copy`: the request's copy source).
    pub(crate) fn data_key(&self, c: &Ctx, bucket: &str, key: &str, sse: &Option<Sse>, copy: bool) -> Result<Option<Key>, ApiError> {
        let Some(sse) = sse else { return Ok(None) };
        if sse.alg == "SSE-C" {
            let (k, _) = self.customer_key(c, copy)?.ok_or_else(|| e::SSE_C_REQUIRED.clone())?;
            let salt = B64.decode(&sse.salt).map_err(|_| e::INTERNAL.clone())?;
            if !bool::from(hmac_key(&salt, &k).as_bytes().ct_eq(sse.key_hmac.as_bytes())) {
                return Err(e::SSE_C_WRONG_KEY.clone());
            }
            return Ok(Some(k));
        }
        let kek = self.keyring.keys.get(&sse.key_id).ok_or_else(|| {
            tracing::error!(key_id = %sse.key_id, bucket, key, "the key that encrypted this object is not in the keyring");
            e::KMS_KEY_UNAVAILABLE.clone()
        })?;
        let wrapped = B64.decode(&sse.wrapped).map_err(|_| e::INTERNAL.clone())?;
        crypt::unwrap(kek, &wrapped, &aad(bucket, key)).map(Some).ok_or_else(|| {
            tracing::error!(key_id = %sse.key_id, bucket, key, "data key failed to unwrap");
            e::KMS_KEY_UNAVAILABLE.clone()
        })
    }

    pub(crate) fn sse_headers(r: &mut Resp, sse: &Option<Sse>) {
        match sse.as_ref().map(|s| s.alg.as_str()) {
            Some("SSE-C") => {
                let s = sse.as_ref().unwrap();
                set_header(r, "x-amz-server-side-encryption-customer-algorithm", "AES256");
                set_header(r, "x-amz-server-side-encryption-customer-key-md5", &s.key_md5);
            }
            Some("aws:kms") => {
                set_header(r, "x-amz-server-side-encryption", "aws:kms");
                set_header(r, "x-amz-server-side-encryption-aws-kms-key-id", &sse.as_ref().unwrap().key_id);
            }
            Some(a) => set_header(r, "x-amz-server-side-encryption", a),
            None => {}
        }
    }

    // ------------------------------------------------------------------------------- bucket default

    pub(crate) async fn put_bucket_encryption<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "ServerSideEncryptionConfiguration")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        let rule = root.child("Rule").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let d = rule.child("ApplyServerSideEncryptionByDefault").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let cfg = BucketEncryption {
            alg: d.text_of("SSEAlgorithm"),
            key_id: d.text_of("KMSMasterKeyID"),
            bucket_key: rule.text_of("BucketKeyEnabled") == "true",
        };
        match cfg.alg.as_str() {
            "AES256" if cfg.key_id.is_empty() => {
                if self.keyring.sse_s3_key.is_empty() {
                    return Err(e::SSE_NOT_CONFIGURED.clone());
                }
            }
            "aws:kms" => {
                if !self.keyring.keys.contains_key(&cfg.key_id) {
                    return Err(if cfg.key_id.is_empty() { e::KMS_KEY_REQUIRED.clone() } else { e::KMS_NOT_FOUND.clone() });
                }
            }
            "aws:kms:dsse" => return Err(e::SSE_DSSE_UNSUPPORTED.clone()),
            _ => return Err(e::MALFORMED_XML.clone()),
        }
        let v = serde_json::to_vec(&cfg).map_err(|_| e::INTERNAL.clone())?;
        self.set_config(c, "encryption", Some(&v))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_bucket_encryption(&self, c: &Ctx) -> R {
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let cfg = match self.bucket_encryption(&c.bucket)? {
            Some(cfg) => cfg,
            None if self.encrypt_by_default && !self.keyring.sse_s3_key.is_empty() => {
                BucketEncryption { alg: "AES256".into(), ..Default::default() }
            }
            None => return Err(e::NO_SUCH_ENCRYPTION.clone()),
        };
        let mut w = Writer::new("ServerSideEncryptionConfiguration");
        w.open("Rule").open("ApplyServerSideEncryptionByDefault").el("SSEAlgorithm", &cfg.alg).opt("KMSMasterKeyID", &cfg.key_id).close();
        w.el("BucketKeyEnabled", cfg.bucket_key.to_string()).close();
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keyrings() {
        let k = B64.encode([7u8; 32]);
        let r = Keyring::from_json(&format!(r#"{{"keys": {{"old": "{k}", "new": "{k}"}}, "sse_s3_key": "new"}}"#)).unwrap();
        assert_eq!(r.keys.len(), 2);
        assert!(Keyring::from_json(&format!(r#"{{"keys": {{"a": "{k}"}}, "sse_s3_key": "b"}}"#)).is_err());
        assert!(Keyring::single("c2hvcnQ=").is_err());
        assert_eq!(Keyring::single(&k).unwrap().sse_s3_key, "default");
    }
}
