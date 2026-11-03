//! People and pipeline users on the catalog (CAIRN_CATALOG_JWKS_URL): engines that read table files
//! themselves (Spark, pyiceberg, DuckDB) sign in with a token Cairn issued for audience `lakehouse`, and
//! the catalog asks Cairn, for that user, what they may do with each table (`/v1/lakehouse/decide`).
//!
//! * Tokens are RS256 JWTs checked against Cairn's published keys (fetched, cached, refetched for an
//!   unknown key id), for issuer, audience, expiry and not-before.
//! * A decision says view, read, write, manage and whether reading is restricted (a mask or row filter
//!   applies): restricted tables are refused, since their files hold what the mask or filter would hide.
//! * Decisions are cached for a few seconds; any error refuses (fail closed).

use std::collections::HashMap;
use std::sync::{Mutex, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use serde::Deserialize;
use serde_json::{json, Value};

const DECISION_TTL: Duration = Duration::from_secs(15);
const JWKS_TTL: Duration = Duration::from_secs(600);
const SKEW_SECS: i64 = 60;

#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct Decision {
    pub view: bool,
    pub read: bool,
    pub write: bool,
    pub manage: bool,
    pub restricted: bool,
}

impl Decision {
    pub fn all() -> Decision {
        Decision { view: true, read: true, write: true, manage: true, restricted: false }
    }
}

pub struct IdentityConfig {
    pub jwks_url: String,
    pub issuer: String,
    pub audience: String,
    /// Cairn's API (the decisions), and the Cairn service token the catalog asks with.
    pub cairn_url: String,
    pub cairn_token: String,
}

type DecisionKey = (String, Vec<String>, String);
/// RSA public keys by key id: (modulus, exponent), and when they were fetched.
type Keys = (Option<Instant>, HashMap<String, (Vec<u8>, Vec<u8>)>);

pub struct Identity {
    cfg: IdentityConfig,
    http: reqwest::Client,
    keys: RwLock<Keys>,
    decisions: Mutex<HashMap<DecisionKey, (Instant, Decision)>>,
    schemas: Mutex<HashMap<String, (Instant, bool)>>,
}

/// External share recipients sign in as `share:<id>` (Cairn issues their tokens from the share's credential).
pub fn is_share(user: &str) -> bool {
    user.starts_with("share:")
}

fn b64(s: &str) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(s.trim_end_matches('=')).map_err(|e| e.to_string())
}

fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

impl Identity {
    pub fn new(cfg: IdentityConfig) -> Result<Identity, String> {
        if cfg.cairn_token.len() < 16 {
            return Err("CAIRN_CATALOG_CAIRN_TOKEN: a Cairn service token (it asks Cairn about every table)".into());
        }
        let http = reqwest::Client::builder().timeout(Duration::from_secs(10)).build().map_err(|e| e.to_string())?;
        Ok(Identity {
            cfg,
            http,
            keys: RwLock::new((None, HashMap::new())),
            decisions: Mutex::new(HashMap::new()),
            schemas: Mutex::new(HashMap::new()),
        })
    }

    async fn fetch_keys(&self) -> Result<(), String> {
        let body = self.http.get(&self.cfg.jwks_url).send().await.map_err(|e| e.to_string())?.bytes().await.map_err(|e| e.to_string())?;
        let doc: Value = serde_json::from_slice(&body).map_err(|e| format!("JWKS: {e}"))?;
        let mut keys = HashMap::new();
        for k in doc["keys"].as_array().cloned().unwrap_or_default() {
            if k["kty"] == "RSA" {
                if let (Some(kid), Some(n), Some(e)) = (k["kid"].as_str(), k["n"].as_str(), k["e"].as_str()) {
                    keys.insert(kid.to_string(), (b64(n)?, b64(e)?));
                }
            }
        }
        *self.keys.write().unwrap() = (Some(Instant::now()), keys);
        Ok(())
    }

    async fn key(&self, kid: &str) -> Result<(Vec<u8>, Vec<u8>), String> {
        {
            let k = self.keys.read().unwrap();
            if let (Some(at), Some(v)) = (k.0, k.1.get(kid)) {
                if at.elapsed() < JWKS_TTL {
                    return Ok(v.clone());
                }
            }
        }
        self.fetch_keys().await?;
        self.keys.read().unwrap().1.get(kid).cloned().ok_or_else(|| "the token's key is not one Cairn publishes".into())
    }

    /// The Cairn user a token signs in, or why it does not.
    pub async fn verify(&self, token: &str) -> Result<String, String> {
        let parts: Vec<&str> = token.split('.').collect();
        let [h, c, s] = parts.as_slice() else { return Err("not a JWT".into()) };
        let header: Value = serde_json::from_slice(&b64(h)?).map_err(|e| e.to_string())?;
        if header["alg"] != "RS256" {
            return Err("only RS256 tokens are accepted".into());
        }
        let (n, e) = self.key(header["kid"].as_str().unwrap_or("")).await?;
        let msg = format!("{h}.{c}");
        ring::signature::RsaPublicKeyComponents { n: &n, e: &e }
            .verify(&ring::signature::RSA_PKCS1_2048_8192_SHA256, msg.as_bytes(), &b64(s)?)
            .map_err(|_| "bad signature".to_string())?;
        let claims: Value = serde_json::from_slice(&b64(c)?).map_err(|e| e.to_string())?;
        if claims["iss"] != self.cfg.issuer.as_str() {
            return Err("wrong issuer".into());
        }
        let aud_ok = match &claims["aud"] {
            Value::String(a) => a == &self.cfg.audience,
            Value::Array(a) => a.iter().any(|x| x == self.cfg.audience.as_str()),
            _ => false,
        };
        if !aud_ok {
            return Err(format!("the token is not for {} (another service's token)", self.cfg.audience));
        }
        let t = now();
        if claims["exp"].as_i64().is_none_or(|exp| exp + SKEW_SECS < t) {
            return Err("the token has expired".into());
        }
        if claims["nbf"].as_i64().is_some_and(|nbf| nbf - SKEW_SECS > t) {
            return Err("the token is not valid yet".into());
        }
        claims["sub"].as_str().filter(|s| !s.is_empty()).map(str::to_string).ok_or_else(|| "the token names no user".into())
    }

    async fn ask(&self, user: &str, tables: &[(Vec<String>, String)]) -> Result<(bool, Vec<Decision>, Vec<String>), String> {
        let body = json!({"user": user, "tables": tables.iter().map(|(ns, n)| json!({"namespace": ns, "name": n})).collect::<Vec<_>>()});
        let r = self
            .http
            .post(format!("{}/v1/lakehouse/decide", self.cfg.cairn_url.trim_end_matches('/')))
            .bearer_auth(&self.cfg.cairn_token)
            .header("content-type", "application/json")
            .body(body.to_string())
            .send()
            .await
            .map_err(|e| format!("Cairn: {e}"))?;
        let status = r.status();
        let bytes = r.bytes().await.map_err(|e| e.to_string())?;
        if !status.is_success() {
            return Err(format!("Cairn answered {status}: {}", String::from_utf8_lossy(&bytes)));
        }
        #[derive(Deserialize)]
        struct Answer {
            schemas: bool,
            tables: Vec<Decision>,
            /// For an external share: the only namespaces its recipient may see.
            #[serde(default)]
            namespaces: Vec<String>,
        }
        let a: Answer = serde_json::from_slice(&bytes).map_err(|e| format!("Cairn's answer: {e}"))?;
        if a.tables.len() != tables.len() {
            return Err("Cairn's answer does not match the question".into());
        }
        Ok((a.schemas, a.tables, a.namespaces))
    }

    /// An external share's recipient: the namespaces it may see now (none once the share has ended).
    /// Never cached, so revoking a share stops the next request.
    pub async fn share_scope(&self, user: &str) -> Result<Vec<String>, String> {
        Ok(self.ask(user, &[]).await?.2)
    }

    /// What `user` may do with each table (cached briefly).
    pub async fn decide(&self, user: &str, tables: &[(Vec<String>, String)]) -> Result<Vec<Decision>, String> {
        if is_share(user) {
            return Ok(self.ask(user, tables).await?.1);
        }
        let mut out: Vec<Option<Decision>> = vec![None; tables.len()];
        let mut missing = vec![];
        {
            let cache = self.decisions.lock().unwrap();
            for (i, (ns, n)) in tables.iter().enumerate() {
                match cache.get(&(user.to_string(), ns.clone(), n.clone())) {
                    Some((at, d)) if at.elapsed() < DECISION_TTL => out[i] = Some(d.clone()),
                    _ => missing.push(i),
                }
            }
        }
        if !missing.is_empty() {
            let ask: Vec<(Vec<String>, String)> = missing.iter().map(|i| tables[*i].clone()).collect();
            let (schemas, got, _) = self.ask(user, &ask).await?;
            self.schemas.lock().unwrap().insert(user.to_string(), (Instant::now(), schemas));
            let mut cache = self.decisions.lock().unwrap();
            if cache.len() > 50_000 {
                cache.clear();
            }
            for (i, d) in missing.into_iter().zip(got) {
                cache.insert((user.to_string(), tables[i].0.clone(), tables[i].1.clone()), (Instant::now(), d.clone()));
                out[i] = Some(d);
            }
        }
        Ok(out.into_iter().map(|d| d.unwrap_or_default()).collect())
    }

    /// Tells Cairn that these tables were committed to, so what depends on them can refresh. In the
    /// background, a few tries over about a minute; a commit never waits for it or fails because of it.
    pub fn changed(self: &std::sync::Arc<Self>, tables: Vec<(Vec<String>, String)>) {
        if tables.is_empty() {
            return;
        }
        let me = self.clone();
        tokio::spawn(async move {
            let body = json!({"tables": tables.iter().map(|(ns, n)| json!({"namespace": ns, "name": n})).collect::<Vec<_>>()});
            let url = format!("{}/v1/lakehouse/changes", me.cfg.cairn_url.trim_end_matches('/'));
            for attempt in 0..5u32 {
                let r = me
                    .http
                    .post(&url)
                    .bearer_auth(&me.cfg.cairn_token)
                    .header("content-type", "application/json")
                    .body(body.to_string())
                    .send()
                    .await;
                match r {
                    Ok(r) if r.status().is_success() => return,
                    Ok(r) if r.status().is_client_error() => {
                        tracing::warn!(status = %r.status(), "Cairn refused the table changes; not retried");
                        return;
                    }
                    Ok(r) => tracing::warn!(status = %r.status(), attempt, "telling Cairn of table changes"),
                    Err(e) => tracing::warn!(error = %e, attempt, "telling Cairn of table changes"),
                }
                tokio::time::sleep(Duration::from_secs(2u64.pow(attempt + 1))).await;
            }
        });
    }

    /// May `user` create, change and drop namespaces?
    pub async fn schemas(&self, user: &str) -> Result<bool, String> {
        if let Some((at, ok)) = self.schemas.lock().unwrap().get(user) {
            if at.elapsed() < DECISION_TTL {
                return Ok(*ok);
            }
        }
        let (schemas, _, _) = self.ask(user, &[]).await?;
        self.schemas.lock().unwrap().insert(user.to_string(), (Instant::now(), schemas));
        Ok(schemas)
    }
}
