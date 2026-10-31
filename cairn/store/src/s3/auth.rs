//! Request authentication: AWS Signature Version 4 (header and presigned query) and Version 2 (still
//! used by some tools for presigned URLs).

use std::collections::BTreeMap;

use base64::Engine as _;
use hmac::{Hmac, Mac};
use hyper::HeaderMap;
use serde::Deserialize;
use sha1::Sha1;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use time::{macros::format_description, OffsetDateTime, PrimitiveDateTime};

use super::errors::{self as e, ApiError};

pub const ALGORITHM: &str = "AWS4-HMAC-SHA256";
pub const UNSIGNED_PAYLOAD: &str = "UNSIGNED-PAYLOAD";
pub const STREAMING_SIGNED: &str = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD";
pub const STREAMING_SIGNED_TRAILER: &str = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER";
pub const STREAMING_UNSIGNED_TRAILER: &str = "STREAMING-UNSIGNED-PAYLOAD-TRAILER";
pub const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const MAX_SKEW_SECS: i64 = 15 * 60;

/// An access key the store accepts.
#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Credential {
    pub access_key: String,
    pub secret_key: String,
    /// Glob patterns of buckets the key may use; empty = all buckets.
    #[serde(default)]
    pub buckets: Vec<String>,
    /// Only GET and HEAD.
    #[serde(default)]
    pub read_only: bool,
    /// May create and delete buckets.
    #[serde(default)]
    pub admin: bool,
    /// An IAM-style policy for the key (no Principal). When set, it replaces the key's bucket scope.
    #[serde(default)]
    pub policy: Option<serde_json::Value>,
    /// The key's Cairn identity (a corpuser URN), for Cairn's policy decision point.
    #[serde(default)]
    pub actor: String,
}

impl Credential {
    pub fn allows(&self, bucket: &str) -> bool {
        bucket.is_empty() || self.buckets.is_empty() || self.buckets.iter().any(|p| glob_match::glob_match(p, bucket))
    }
}

/// A verified request signature (or an anonymous request: empty access key).
#[derive(Clone)]
pub struct Auth {
    pub cred: Credential,
    /// Hex SHA-256 of the payload, or one of the UNSIGNED / STREAMING markers.
    pub payload_hash: String,
    pub seed_sig: String,
    pub signing_key: Vec<u8>,
    pub amz_date: String,
    pub scope: String,
}

impl Auth {
    pub fn anonymous() -> Auth {
        Auth {
            cred: Credential::default(),
            payload_hash: UNSIGNED_PAYLOAD.into(),
            seed_sig: String::new(),
            signing_key: vec![],
            amz_date: String::new(),
            scope: String::new(),
        }
    }
}

/// Does the request carry no credentials at all (no Authorization header, no presigned parameters)?
pub fn is_anonymous(headers: &HeaderMap, raw_query: &str) -> bool {
    !headers.contains_key("authorization")
        && !raw_query.split('&').any(|p| {
            let k = p.split('=').next().unwrap_or("");
            k == "X-Amz-Algorithm" || k == "X-Amz-Credential" || k == "AWSAccessKeyId" || k == "X-Amz-Signature"
        })
}

pub fn hmac_sha256(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut m = <Hmac<Sha256>>::new_from_slice(key).expect("hmac accepts any key");
    m.update(data);
    m.finalize().into_bytes().to_vec()
}

pub fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

fn signing_key(secret: &str, date: &str, region: &str, service: &str) -> Vec<u8> {
    let k = hmac_sha256(format!("AWS4{secret}").as_bytes(), date.as_bytes());
    let k = hmac_sha256(&k, region.as_bytes());
    let k = hmac_sha256(&k, service.as_bytes());
    hmac_sha256(&k, b"aws4_request")
}

/// SigV4 URI encoding: unreserved characters stay, everything else is %XX (upper case).
pub fn aws_encode(data: &[u8], keep_slash: bool) -> String {
    let mut out = String::with_capacity(data.len());
    for &c in data {
        if c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_' | b'.' | b'~') || (keep_slash && c == b'/') {
            out.push(c as char);
        } else {
            out.push_str(&format!("%{c:02X}"));
        }
    }
    out
}

pub fn percent_decode(s: &str) -> Vec<u8> {
    percent_encoding::percent_decode_str(s).collect()
}

/// Query-string decoding: like percent decoding, with '+' as a space.
pub fn query_decode(s: &str) -> String {
    String::from_utf8_lossy(&percent_decode(&s.replace('+', " "))).into_owned()
}

fn canonical_query(raw: &str, skip: &str) -> String {
    let mut pairs: Vec<(String, String)> = raw
        .split('&')
        .filter(|p| !p.is_empty())
        .filter_map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            let k = query_decode(k);
            (k != skip).then(|| (aws_encode(k.as_bytes(), false), aws_encode(query_decode(v).as_bytes(), false)))
        })
        .collect();
    pairs.sort();
    pairs.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join("&")
}

fn header_str<'a>(h: &'a HeaderMap, name: &str) -> &'a str {
    h.get(name).and_then(|v| v.to_str().ok()).unwrap_or("")
}

fn canonical_headers(h: &HeaderMap, host: &str, signed: &[&str]) -> String {
    let mut out = String::new();
    for name in signed {
        let v = if *name == "host" {
            host.to_string()
        } else {
            h.get_all(*name)
                .iter()
                .map(|v| String::from_utf8_lossy(v.as_bytes()).split_whitespace().collect::<Vec<_>>().join(" "))
                .collect::<Vec<_>>()
                .join(",")
        };
        out.push_str(&format!("{name}:{}\n", v.trim()));
    }
    out
}

fn parse_amz_date(s: &str) -> Option<OffsetDateTime> {
    PrimitiveDateTime::parse(s, format_description!("[year][month][day]T[hour][minute][second]Z")).ok().map(|d| d.assume_utc())
}

/// What a request needs to be verified.
pub struct Request<'a> {
    pub method: &'a str,
    pub raw_path: &'a str,
    pub raw_query: &'a str,
    pub host: &'a str,
    pub headers: &'a HeaderMap,
    pub now: OffsetDateTime,
    /// Virtual-hosted bucket (SigV2 puts it in the canonical resource).
    pub vhost_bucket: Option<&'a str>,
}

pub fn verify(creds: &BTreeMap<String, Credential>, r: &Request) -> Result<Auth, ApiError> {
    let q: BTreeMap<String, String> = r
        .raw_query
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            (query_decode(k), query_decode(v))
        })
        .collect();
    let auth = header_str(r.headers, "authorization");
    if !auth.is_empty() {
        if let Some(rest) = auth.strip_prefix("AWS4-HMAC-SHA256 ") {
            let fields: BTreeMap<&str, &str> = rest.split(',').filter_map(|p| p.trim().split_once('=')).collect();
            let mut amz_date = header_str(r.headers, "x-amz-date").to_string();
            if amz_date.is_empty() {
                if let Ok(t) = httpdate::parse_http_date(header_str(r.headers, "date")) {
                    amz_date = OffsetDateTime::from(t)
                        .format(format_description!("[year][month][day]T[hour][minute][second]Z"))
                        .unwrap_or_default();
                }
            }
            let payload = header_str(r.headers, "x-amz-content-sha256");
            if payload.is_empty() {
                return Err(e::MISSING_CONTENT_SHA.clone());
            }
            return check_v4(
                creds,
                r,
                fields.get("Credential").copied().unwrap_or(""),
                fields.get("SignedHeaders").copied().unwrap_or(""),
                fields.get("Signature").copied().unwrap_or(""),
                &amz_date,
                payload,
                "",
                None,
            );
        }
        if let Some(rest) = auth.strip_prefix("AWS ") {
            let (ak, sig) = rest.split_once(':').ok_or_else(|| e::AUTH_HEADER_MALFORMED.clone())?;
            let when =
                [header_str(r.headers, "x-amz-date"), header_str(r.headers, "date")].into_iter().find(|s| !s.is_empty()).unwrap_or("");
            let t = httpdate::parse_http_date(when).map(OffsetDateTime::from).map_err(|_| e::TIME_SKEWED.clone())?;
            if (r.now - t).whole_seconds().abs() > MAX_SKEW_SECS {
                return Err(e::TIME_SKEWED.clone());
            }
            let date = if header_str(r.headers, "x-amz-date").is_empty() { header_str(r.headers, "date") } else { "" };
            return check_v2(creds, r, ak, sig, date);
        }
        return Err(e::AUTH_HEADER_MALFORMED.clone());
    }
    if let Some(alg) = q.get("X-Amz-Algorithm") {
        if alg != ALGORITHM {
            return Err(e::AUTH_QUERY_MALFORMED.clone());
        }
        let expires: i64 = q
            .get("X-Amz-Expires")
            .and_then(|v| v.parse().ok())
            .filter(|n| (0..=7 * 24 * 3600).contains(n))
            .ok_or_else(|| e::AUTH_QUERY_MALFORMED.clone())?;
        let payload = q.get("X-Amz-Content-Sha256").map(String::as_str).unwrap_or(UNSIGNED_PAYLOAD);
        let g = |k: &str| q.get(k).map(String::as_str).unwrap_or("");
        return check_v4(
            creds,
            r,
            g("X-Amz-Credential"),
            g("X-Amz-SignedHeaders"),
            g("X-Amz-Signature"),
            g("X-Amz-Date"),
            payload,
            "X-Amz-Signature",
            Some(expires),
        );
    }
    if let Some(ak) = q.get("AWSAccessKeyId") {
        let exp: i64 = q.get("Expires").and_then(|v| v.parse().ok()).ok_or_else(|| e::AUTH_QUERY_MALFORMED.clone())?;
        if r.now.unix_timestamp() > exp {
            return Err(e::EXPIRED.clone());
        }
        return check_v2(creds, r, ak, q.get("Signature").map(String::as_str).unwrap_or(""), &exp.to_string());
    }
    Err(e::ACCESS_DENIED.clone())
}

/// Verifies a browser POST policy signed with SigV4: the signature is HMAC(signing key, policy base64).
pub fn verify_post_v4(
    creds: &BTreeMap<String, Credential>,
    credential: &str,
    amz_date: &str,
    policy_b64: &str,
    signature: &str,
) -> Result<Credential, ApiError> {
    let parts: Vec<&str> = credential.split('/').collect();
    if parts.len() != 5 || parts[3] != "s3" || parts[4] != "aws4_request" || !amz_date.starts_with(parts[1]) || signature.is_empty() {
        return Err(e::AUTH_HEADER_MALFORMED.clone());
    }
    let cred = creds.get(parts[0]).ok_or_else(|| e::INVALID_ACCESS_KEY.clone())?;
    let key = signing_key(&cred.secret_key, parts[1], parts[2], parts[3]);
    let want = hex::encode(hmac_sha256(&key, policy_b64.as_bytes()));
    if !bool::from(want.as_bytes().ct_eq(signature.as_bytes())) {
        return Err(e::SIGNATURE_MISMATCH.clone());
    }
    Ok(cred.clone())
}

/// Verifies a browser POST policy signed with SigV2: base64(HMAC-SHA1(secret, policy base64)).
pub fn verify_post_v2(
    creds: &BTreeMap<String, Credential>,
    access_key: &str,
    policy_b64: &str,
    signature: &str,
) -> Result<Credential, ApiError> {
    let cred = creds.get(access_key).ok_or_else(|| e::INVALID_ACCESS_KEY.clone())?;
    let mut m = <Hmac<Sha1>>::new_from_slice(cred.secret_key.as_bytes()).expect("hmac accepts any key");
    m.update(policy_b64.as_bytes());
    let want = base64::engine::general_purpose::STANDARD.encode(m.finalize().into_bytes());
    if !bool::from(want.as_bytes().ct_eq(signature.as_bytes())) {
        return Err(e::SIGNATURE_MISMATCH.clone());
    }
    Ok(cred.clone())
}

#[allow(clippy::too_many_arguments)]
fn check_v4(
    creds: &BTreeMap<String, Credential>,
    r: &Request,
    credential: &str,
    signed_headers: &str,
    signature: &str,
    amz_date: &str,
    payload: &str,
    skip: &str,
    expires: Option<i64>,
) -> Result<Auth, ApiError> {
    let parts: Vec<&str> = credential.split('/').collect();
    if parts.len() != 5 || parts[4] != "aws4_request" || signed_headers.is_empty() || signature.is_empty() {
        return Err(e::AUTH_HEADER_MALFORMED.clone());
    }
    let cred = creds.get(parts[0]).ok_or_else(|| e::INVALID_ACCESS_KEY.clone())?;
    let t = parse_amz_date(amz_date).ok_or_else(|| e::AUTH_HEADER_MALFORMED.clone())?;
    if !amz_date.starts_with(parts[1]) {
        return Err(e::AUTH_HEADER_MALFORMED.clone());
    }
    let age = (r.now - t).whole_seconds();
    match expires {
        Some(exp) => {
            if age > exp {
                return Err(e::EXPIRED.clone());
            }
            if -age > MAX_SKEW_SECS {
                return Err(e::TIME_SKEWED.clone());
            }
        }
        None if age.abs() > MAX_SKEW_SECS => return Err(e::TIME_SKEWED.clone()),
        None => {}
    }
    let signed: Vec<&str> = signed_headers.split(';').collect();
    let mut path = aws_encode(&percent_decode(r.raw_path), true);
    if path.is_empty() {
        path = "/".into();
    }
    let canonical = [
        r.method.to_string(),
        path,
        canonical_query(r.raw_query, skip),
        canonical_headers(r.headers, r.host, &signed),
        signed_headers.to_string(),
        payload.to_string(),
    ]
    .join("\n");
    let scope = parts[1..].join("/");
    let to_sign = format!("{ALGORITHM}\n{amz_date}\n{scope}\n{}", sha256_hex(canonical.as_bytes()));
    let key = signing_key(&cred.secret_key, parts[1], parts[2], parts[3]);
    let want = hex::encode(hmac_sha256(&key, to_sign.as_bytes()));
    if !bool::from(want.as_bytes().ct_eq(signature.as_bytes())) {
        return Err(e::SIGNATURE_MISMATCH.clone());
    }
    Ok(Auth {
        cred: cred.clone(),
        payload_hash: payload.to_string(),
        seed_sig: signature.to_string(),
        signing_key: key,
        amz_date: amz_date.to_string(),
        scope,
    })
}

/// Sub-resources that are part of a SigV2 canonical resource.
const V2_SUBRESOURCES: [&str; 25] = [
    "acl",
    "cors",
    "delete",
    "lifecycle",
    "location",
    "logging",
    "notification",
    "partNumber",
    "policy",
    "requestPayment",
    "restore",
    "tagging",
    "torrent",
    "uploadId",
    "uploads",
    "versionId",
    "versioning",
    "versions",
    "website",
    "response-cache-control",
    "response-content-disposition",
    "response-content-encoding",
    "response-content-language",
    "response-content-type",
    "response-expires",
];

fn check_v2(creds: &BTreeMap<String, Credential>, r: &Request, ak: &str, sig: &str, date: &str) -> Result<Auth, ApiError> {
    let cred = creds.get(ak).ok_or_else(|| e::INVALID_ACCESS_KEY.clone())?;
    let mut amz: Vec<String> = r
        .headers
        .keys()
        .map(|k| k.as_str())
        .filter(|k| k.starts_with("x-amz-"))
        .map(|k| {
            let vals: Vec<String> = r.headers.get_all(k).iter().map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned()).collect();
            format!("{k}:{}", vals.join(","))
        })
        .collect();
    amz.sort();
    amz.dedup();
    let mut resource = match r.vhost_bucket {
        Some(b) => format!("/{b}{}", r.raw_path),
        None => r.raw_path.to_string(),
    };
    let mut subs: Vec<String> = r
        .raw_query
        .split('&')
        .filter(|p| !p.is_empty())
        .filter_map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            let k = query_decode(k);
            V2_SUBRESOURCES.contains(&k.as_str()).then(|| if v.is_empty() { k.clone() } else { format!("{k}={}", query_decode(v)) })
        })
        .collect();
    subs.sort();
    if !subs.is_empty() {
        resource = format!("{resource}?{}", subs.join("&"));
    }
    let mut sts = format!("{}\n{}\n{}\n{date}\n", r.method, header_str(r.headers, "content-md5"), header_str(r.headers, "content-type"));
    for h in &amz {
        sts.push_str(h);
        sts.push('\n');
    }
    sts.push_str(&resource);
    let mut m = <Hmac<Sha1>>::new_from_slice(cred.secret_key.as_bytes()).expect("hmac accepts any key");
    m.update(sts.as_bytes());
    let want = base64::engine::general_purpose::STANDARD.encode(m.finalize().into_bytes());
    if !bool::from(want.as_bytes().ct_eq(sig.as_bytes())) {
        return Err(e::SIGNATURE_MISMATCH.clone());
    }
    Ok(Auth {
        cred: cred.clone(),
        payload_hash: UNSIGNED_PAYLOAD.into(),
        seed_sig: String::new(),
        signing_key: vec![],
        amz_date: String::new(),
        scope: String::new(),
    })
}
