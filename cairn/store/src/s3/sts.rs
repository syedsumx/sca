//! Temporary credentials (AWS STS): `AssumeRole`, `GetSessionToken` and `AssumeRoleWithWebIdentity`.
//!
//! A temporary key is stateless, so every node of a cluster accepts it without sharing anything but the
//! STS secret (`CAIRN_STORE_STS_SECRET`):
//!
//! * the access key id is `ASIA` + 16 random characters;
//! * its secret is derived from the id with the STS secret (HMAC), so it is never stored;
//! * the session token is the session's claims (role, expiry, session policy, Cairn identity) signed
//!   with the STS secret. A client sends it as `X-Amz-Security-Token`; the store checks the signature,
//!   the expiry and that the role still exists (deleting a role ends its sessions).
//!
//! A session may do what its role's policies allow and, when it was given a session policy, only what
//! that policy allows as well. `GetSessionToken` gives a session of the caller's own key (its key policy,
//! or its IAM user's policies). `AssumeRoleWithWebIdentity` exchanges a signed-in user's token (an OIDC
//! JWT, RS256, from a configured issuer such as Cairn) for a role's session; the session carries the
//! user's Cairn identity, so Cairn's policy decision point sees the person, not the role.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use subtle::ConstantTimeEq;

use super::auth::{hmac_sha256, Credential};
use super::errors::{self as e, ApiError};
use super::policy;

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::URL_SAFE_NO_PAD;
pub const MIN_SECS: i64 = 900;
pub const MAX_SECS: i64 = 43_200;
pub const TEMP_PREFIX: &str = "ASIA";

/// An OIDC issuer whose tokens `AssumeRoleWithWebIdentity` accepts.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Provider {
    /// The token's `iss`.
    pub issuer: String,
    /// The token's `aud` must be one of these.
    pub audiences: Vec<String>,
    /// Where its signing keys are: a JWKS URL, or a JWKS file.
    #[serde(default)]
    pub jwks_url: String,
    #[serde(default)]
    pub jwks_file: String,
    /// How the token's subject becomes a Cairn identity (`{sub}` is replaced).
    #[serde(default = "default_actor")]
    pub actor: String,
}

fn default_actor() -> String {
    "urn:li:corpuser:{sub}".into()
}

pub struct StsConfig {
    secret: Vec<u8>,
    pub providers: Vec<Provider>,
    jwks: Mutex<BTreeMap<String, (Instant, Vec<Jwk>)>>,
}

impl std::fmt::Debug for StsConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StsConfig").field("providers", &self.providers).finish_non_exhaustive()
    }
}

impl StsConfig {
    /// `secret`: at least 32 characters, the same on every node. `providers`: JSON list (may be empty).
    pub fn new(secret: &str, providers: &str) -> Result<StsConfig, String> {
        if secret.len() < 32 {
            return Err("CAIRN_STORE_STS_SECRET: at least 32 characters, the same on every node".into());
        }
        let providers: Vec<Provider> = match providers.trim() {
            "" => vec![],
            p => serde_json::from_str(p).map_err(|e| format!("CAIRN_STORE_STS_PROVIDERS: {e}"))?,
        };
        for p in &providers {
            if p.issuer.is_empty() || p.audiences.is_empty() || p.jwks_url.is_empty() == p.jwks_file.is_empty() {
                return Err(format!(
                    "CAIRN_STORE_STS_PROVIDERS: issuer {:?} needs an issuer, audiences and one of jwks_url or jwks_file",
                    p.issuer
                ));
            }
        }
        Ok(StsConfig { secret: secret.as_bytes().to_vec(), providers, jwks: Mutex::new(BTreeMap::new()) })
    }

    /// The secret of a temporary access key.
    pub fn secret_for(&self, ak: &str) -> String {
        B64.encode(hmac_sha256(&self.secret, format!("cairn-sts-secret:{ak}").as_bytes()))[..40].to_string()
    }

    fn sign(&self, payload: &str) -> String {
        B64.encode(hmac_sha256(&self.secret, format!("cairn-sts-token:{payload}").as_bytes()))
    }
}

/// What a session token says.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Claims {
    pub v: u8,
    pub ak: String,
    /// The role (AssumeRole, web identity), or "" for GetSessionToken.
    pub role: String,
    /// GetSessionToken: the key the session belongs to.
    #[serde(default)]
    pub key: String,
    /// Expiry, unix seconds.
    pub exp: i64,
    /// The session policy, if any (JSON text).
    #[serde(default)]
    pub pol: Option<String>,
    /// The Cairn identity the session acts for.
    #[serde(default)]
    pub act: String,
    /// Who asked for it (an access key, or the web identity's issuer and subject).
    #[serde(default)]
    pub src: String,
    #[serde(default)]
    pub name: String,
}

/// A session, attached to the credential a temporary key resolves to.
#[derive(Debug)]
pub struct Session {
    pub role: String,
    /// A key's session (GetSessionToken): the key it belongs to.
    pub parent: String,
    pub name: String,
    /// The session policy (it narrows what the role allows).
    pub policy: Option<policy::Policy>,
}

/// A new temporary key.
pub struct Issued {
    pub access_key: String,
    pub secret_key: String,
    pub token: String,
    pub expires: i64,
}

pub fn issue(cfg: &StsConfig, mut claims: Claims) -> Issued {
    const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let r: [u8; 16] = rand::random();
    let ak = format!("{TEMP_PREFIX}{}", r.iter().map(|b| A[(*b as usize) % 32] as char).collect::<String>());
    claims.ak = ak.clone();
    let payload = B64.encode(serde_json::to_vec(&claims).unwrap_or_default());
    let token = format!("{payload}.{}", cfg.sign(&payload));
    Issued { secret_key: cfg.secret_for(&ak), access_key: ak, token, expires: claims.exp }
}

pub fn is_temporary(ak: &str) -> bool {
    ak.starts_with(TEMP_PREFIX) && ak.len() == 20
}

/// Checks a session token for a temporary key: signature, key, expiry.
pub fn verify(cfg: &StsConfig, ak: &str, token: &str, now: i64) -> Result<Claims, ApiError> {
    let (payload, sig) = token.split_once('.').ok_or_else(|| e::INVALID_SESSION_TOKEN.clone())?;
    if !bool::from(cfg.sign(payload).as_bytes().ct_eq(sig.as_bytes())) {
        return Err(e::INVALID_SESSION_TOKEN.clone());
    }
    let claims: Claims =
        B64.decode(payload).ok().and_then(|b| serde_json::from_slice(&b).ok()).ok_or_else(|| e::INVALID_SESSION_TOKEN.clone())?;
    if claims.v != 1 || claims.ak != ak {
        return Err(e::INVALID_SESSION_TOKEN.clone());
    }
    if now >= claims.exp {
        return Err(e::EXPIRED_TOKEN.clone());
    }
    Ok(claims)
}

/// The credential a verified session acts as. `role_known`: the role still exists.
pub fn credential(cfg: &StsConfig, c: &Claims, parent: Option<&Credential>) -> Result<Credential, ApiError> {
    let policy = match &c.pol {
        Some(p) => Some(policy::Policy::parse(p.as_bytes(), None).map_err(|_| e::INVALID_SESSION_TOKEN.clone())?),
        None => None,
    };
    let base = parent.cloned().unwrap_or_default();
    Ok(Credential {
        access_key: c.ak.clone(),
        secret_key: cfg.secret_for(&c.ak),
        // A session of a key keeps the key's scope; a role's session has only its policies.
        buckets: if c.role.is_empty() { base.buckets } else { vec![] },
        read_only: base.read_only,
        admin: false,
        policy: None,
        actor: if c.act.is_empty() { base.actor } else { c.act.clone() },
        ssh_keys: vec![],
        user: if c.role.is_empty() { base.user } else { String::new() },
        session: Some(Arc::new(Session { role: c.role.clone(), parent: c.key.clone(), name: c.name.clone(), policy })),
    })
}

/// Validates a session policy document: valid JSON policy, at most 2048 characters (as AWS).
pub fn session_policy(raw: &str) -> Result<Option<String>, String> {
    if raw.is_empty() {
        return Ok(None);
    }
    if raw.chars().filter(|c| !c.is_whitespace()).count() > 2048 {
        return Err("PackedPolicyTooLarge: the session policy is over 2048 characters".into());
    }
    policy::Policy::parse(raw.as_bytes(), None).map_err(|e| format!("MalformedPolicyDocument: {}", e.message))?;
    let v: Value = serde_json::from_str(raw).map_err(|_| "MalformedPolicyDocument: not JSON".to_string())?;
    Ok(Some(v.to_string()))
}

pub fn duration(raw: &str, max: i64) -> Result<i64, String> {
    match raw {
        "" => Ok(3600.min(max)),
        n => n
            .parse::<i64>()
            .ok()
            .filter(|n| (MIN_SECS..=max).contains(n))
            .ok_or_else(|| format!("DurationSeconds must be between {MIN_SECS} and {max}")),
    }
}

// ------------------------------------------------------------------------------- web identity

#[derive(Clone, Debug, Deserialize)]
struct Jwk {
    #[serde(default)]
    kid: String,
    #[serde(default)]
    kty: String,
    #[serde(default)]
    n: String,
    #[serde(default)]
    e: String,
}

/// A verified web identity token: (issuer, subject, the provider's Cairn identity for it).
pub struct WebIdentity {
    pub issuer: String,
    pub subject: String,
    pub audience: String,
    pub actor: String,
}

impl StsConfig {
    async fn keys_of(&self, p: &Provider, refresh: bool) -> Result<Vec<Jwk>, String> {
        if !refresh {
            if let Some((at, keys)) = self.jwks.lock().unwrap().get(&p.issuer) {
                if at.elapsed() < Duration::from_secs(300) {
                    return Ok(keys.clone());
                }
            }
        }
        let text = if !p.jwks_file.is_empty() {
            std::fs::read_to_string(&p.jwks_file).map_err(|e| format!("{}: {e}", p.jwks_file))?
        } else {
            fetch(&p.jwks_url).await?
        };
        #[derive(Deserialize)]
        struct Set {
            keys: Vec<Jwk>,
        }
        let set: Set = serde_json::from_str(&text).map_err(|e| format!("JWKS of {}: {e}", p.issuer))?;
        self.jwks.lock().unwrap().insert(p.issuer.clone(), (Instant::now(), set.keys.clone()));
        Ok(set.keys)
    }

    /// Checks a web identity token (RS256 JWT) against the configured issuers.
    pub async fn web_identity(&self, token: &str, now: i64) -> Result<WebIdentity, String> {
        let parts: Vec<&str> = token.split('.').collect();
        if parts.len() != 3 {
            return Err("the token is not a JWT".into());
        }
        let header: Value = B64.decode(parts[0]).ok().and_then(|b| serde_json::from_slice(&b).ok()).ok_or("bad JWT header")?;
        let claims: Value = B64.decode(parts[1]).ok().and_then(|b| serde_json::from_slice(&b).ok()).ok_or("bad JWT claims")?;
        if header["alg"] != "RS256" {
            return Err("only RS256 tokens are accepted".into());
        }
        let iss = claims["iss"].as_str().unwrap_or("");
        let p = self.providers.iter().find(|p| p.issuer == iss).ok_or_else(|| format!("issuer {iss:?} is not trusted"))?;
        let sig = B64.decode(parts[2]).map_err(|_| "bad JWT signature")?;
        let kid = header["kid"].as_str().unwrap_or("");
        let signed = format!("{}.{}", parts[0], parts[1]);
        let mut ok = false;
        for refresh in [false, true] {
            let keys = self.keys_of(p, refresh).await?;
            ok = keys.iter().filter(|k| k.kty == "RSA" && (kid.is_empty() || k.kid == kid)).any(|k| {
                let (Ok(n), Ok(e)) = (B64.decode(&k.n), B64.decode(&k.e)) else { return false };
                ring::signature::RsaPublicKeyComponents { n, e }
                    .verify(&ring::signature::RSA_PKCS1_2048_8192_SHA256, signed.as_bytes(), &sig)
                    .is_ok()
            });
            if ok {
                break;
            }
        }
        if !ok {
            return Err("the token's signature does not verify".into());
        }
        let exp = claims["exp"].as_i64().ok_or("the token has no expiry")?;
        if now >= exp {
            return Err("the token has expired".into());
        }
        if claims["nbf"].as_i64().is_some_and(|nbf| now + 60 < nbf) {
            return Err("the token is not valid yet".into());
        }
        let auds: Vec<String> = match &claims["aud"] {
            Value::String(s) => vec![s.clone()],
            Value::Array(a) => a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
            _ => vec![],
        };
        let audience = auds.iter().find(|a| p.audiences.contains(a)).cloned().ok_or("the token is not for this store (aud)")?;
        let sub = claims["sub"].as_str().filter(|s| !s.is_empty()).ok_or("the token has no subject")?;
        Ok(WebIdentity { issuer: iss.to_string(), subject: sub.to_string(), audience, actor: p.actor.replace("{sub}", sub) })
    }
}

async fn fetch(url: &str) -> Result<String, String> {
    use http_body_util::BodyExt;
    let uri: hyper::Uri = url.parse().map_err(|e| format!("{url}: {e}"))?;
    let https = crate::tls::https_connector()?;
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new())
        .build::<_, http_body_util::Empty<bytes::Bytes>>(https);
    let resp = tokio::time::timeout(Duration::from_secs(10), client.get(uri))
        .await
        .map_err(|_| format!("{url}: timed out"))?
        .map_err(|e| format!("{url}: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("{url}: HTTP {}", resp.status()));
    }
    let body = resp.into_body().collect().await.map_err(|e| format!("{url}: {e}"))?.to_bytes();
    String::from_utf8(body.to_vec()).map_err(|_| format!("{url}: not text"))
}

// ------------------------------------------------------------------------------- trust policies

/// Does a role's trust policy let this principal assume it? `principal`: ("AWS", arn) or
/// ("Federated", issuer). `context`: condition keys (`<issuer>:sub`, `<issuer>:aud`, `sts:RoleSessionName`).
pub fn trusts(trust: &Value, action: &str, kind: &str, principal: &str, context: &BTreeMap<String, String>) -> bool {
    let statements: Vec<&Value> = match &trust["Statement"] {
        Value::Array(a) => a.iter().collect(),
        o @ Value::Object(_) => vec![o],
        _ => vec![],
    };
    let mut allowed = false;
    for s in statements {
        let names = |v: &Value| -> Vec<String> {
            match v {
                Value::String(s) => vec![s.clone()],
                Value::Array(a) => a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect(),
                _ => vec![],
            }
        };
        let acts = names(&s["Action"]);
        if !acts.iter().any(|a| a == action || a == "sts:*" || a == "*") {
            continue;
        }
        let who = match &s["Principal"] {
            Value::String(p) if p == "*" => true,
            Value::Object(m) => m.get(kind).map(names).unwrap_or_default().iter().any(|p| {
                p == "*"
                    || p == principal
                    || (kind == "AWS" && (p == super::iam::ACCOUNT || p == &format!("arn:aws:iam::{}:root", super::iam::ACCOUNT)))
            }),
            _ => false,
        };
        if !who || !conditions_hold(&s["Condition"], context) {
            continue;
        }
        match s["Effect"].as_str() {
            Some("Deny") => return false,
            Some("Allow") => allowed = true,
            _ => {}
        }
    }
    allowed
}

/// Trust-policy conditions: StringEquals, StringLike and their Not forms, on the context's keys.
fn conditions_hold(cond: &Value, ctx: &BTreeMap<String, String>) -> bool {
    let Some(ops) = cond.as_object() else { return cond.is_null() };
    ops.iter().all(|(op, kv)| {
        let Some(kv) = kv.as_object() else { return false };
        kv.iter().all(|(k, want)| {
            let wants: Vec<String> = match want {
                Value::String(s) => vec![s.clone()],
                Value::Array(a) => a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect(),
                _ => return false,
            };
            let have = ctx.iter().find(|(c, _)| c.eq_ignore_ascii_case(k)).map(|(_, v)| v.as_str());
            let hit = |like: bool| have.is_some_and(|h| wants.iter().any(|w| if like { glob_match::glob_match(w, h) } else { w == h }));
            match op.as_str() {
                "StringEquals" => hit(false),
                "StringLike" => hit(true),
                "StringNotEquals" => !hit(false),
                "StringNotLike" => !hit(true),
                _ => false, // unknown condition operators never match (fail closed)
            }
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> StsConfig {
        StsConfig::new(&"s".repeat(32), "").unwrap()
    }

    fn claims(exp: i64) -> Claims {
        Claims {
            v: 1,
            ak: String::new(),
            role: "reader".into(),
            key: String::new(),
            exp,
            pol: None,
            act: String::new(),
            src: "k".into(),
            name: "s".into(),
        }
    }

    #[test]
    fn tokens_verify_and_expire() {
        let c = cfg();
        let i = issue(&c, claims(2000));
        assert!(is_temporary(&i.access_key));
        assert_eq!(verify(&c, &i.access_key, &i.token, 1000).unwrap().role, "reader");
        assert_eq!(verify(&c, &i.access_key, &i.token, 2000).unwrap_err().code, "ExpiredToken");
        // Another key's token, an edited token, another store's secret: refused.
        let j = issue(&c, claims(2000));
        assert_eq!(verify(&c, &i.access_key, &j.token, 1000).unwrap_err().code, "InvalidToken");
        let (p, s) = i.token.split_once('.').unwrap();
        let mut edited: Claims = serde_json::from_slice(&B64.decode(p).unwrap()).unwrap();
        edited.role = "admin".into();
        let forged = format!("{}.{s}", B64.encode(serde_json::to_vec(&edited).unwrap()));
        assert_eq!(verify(&c, &i.access_key, &forged, 1000).unwrap_err().code, "InvalidToken");
        let other = StsConfig::new(&"t".repeat(32), "").unwrap();
        assert!(verify(&other, &i.access_key, &i.token, 1000).is_err());
        assert_eq!(c.secret_for(&i.access_key), i.secret_key);
        assert!(StsConfig::new("short", "").is_err());
    }

    #[test]
    fn trust_policies() {
        let t: Value = serde_json::json!({"Statement": [
            {"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::000000000000:user/catalog"}, "Action": "sts:AssumeRole"},
            {"Effect": "Allow", "Principal": {"Federated": "cairn"}, "Action": "sts:AssumeRoleWithWebIdentity",
             "Condition": {"StringEquals": {"cairn:aud": "cairn-store"}, "StringLike": {"cairn:sub": "data-*"}}}]});
        let none = BTreeMap::new();
        assert!(trusts(&t, "sts:AssumeRole", "AWS", "arn:aws:iam::000000000000:user/catalog", &none));
        assert!(!trusts(&t, "sts:AssumeRole", "AWS", "arn:aws:iam::000000000000:user/eve", &none));
        let ctx =
            |sub: &str| BTreeMap::from([("cairn:aud".to_string(), "cairn-store".to_string()), ("cairn:sub".to_string(), sub.to_string())]);
        assert!(trusts(&t, "sts:AssumeRoleWithWebIdentity", "Federated", "cairn", &ctx("data-ana")));
        assert!(!trusts(&t, "sts:AssumeRoleWithWebIdentity", "Federated", "cairn", &ctx("bob")));
        assert!(!trusts(&t, "sts:AssumeRoleWithWebIdentity", "Federated", "other", &ctx("data-ana")));
        assert!(!conditions_hold(&serde_json::json!({"NumericLessThan": {"x": "1"}}), &none));
    }

    #[test]
    fn session_policies_and_durations() {
        assert!(session_policy("").unwrap().is_none());
        assert!(session_policy(r#"{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"*"}]}"#)
            .unwrap()
            .is_some());
        assert!(session_policy("{nope").is_err());
        assert_eq!(duration("", 43200).unwrap(), 3600);
        assert!(duration("60", 43200).is_err());
        assert!(duration("50000", 43200).is_err());
    }
}
