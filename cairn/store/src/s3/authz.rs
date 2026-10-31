//! Authorization: every request is checked against the key's scope or policy, the bucket policy, the
//! public access block and (when configured) Cairn's policy decision point.
//!
//! Cairn's PDP is an extra, deny-only layer: the store asks it about requests the store's own rules
//! allow, for the key's Cairn identity (`actor` in the credentials file). DENY refuses the request;
//! ALLOW and NOT_APPLICABLE leave the store's decision standing. If the PDP cannot be reached the
//! request is refused (fail closed). Decisions are cached for 30 s.

use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::StatusCode;
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use serde::{Deserialize, Serialize};

use super::errors::{self as e, ApiError};
use super::policy::{Effect, Policy, Request as PolicyRequest};
use super::xml::{self, Writer};
use super::{empty, iso_time, xml_resp, Ctx, Resp, Server};

type R = Result<Resp, ApiError>;

const PDP_TTL: Duration = Duration::from_secs(30);
const PDP_CACHE_MAX: usize = 10_000;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

/// The S3 action (IAM name) for an operation.
pub fn action_of(op: &str) -> Option<&'static str> {
    Some(match op {
        "ListBuckets" => "s3:ListAllMyBuckets",
        "CreateBucket" => "s3:CreateBucket",
        "DeleteBucket" => "s3:DeleteBucket",
        "HeadBucket" | "ListObjects" | "ListObjectsV2" => "s3:ListBucket",
        "ListMultipartUploads" => "s3:ListBucketMultipartUploads",
        "GetBucketLocation" => "s3:GetBucketLocation",
        "GetBucketVersioning" => "s3:GetBucketVersioning",
        "PutBucketVersioning" => "s3:PutBucketVersioning",
        "PutObjectLockConfiguration" => "s3:PutBucketObjectLockConfiguration",
        "GetObjectLockConfiguration" => "s3:GetBucketObjectLockConfiguration",
        "PutObjectRetention" => "s3:PutObjectRetention",
        "GetObjectRetention" => "s3:GetObjectRetention",
        "PutObjectLegalHold" => "s3:PutObjectLegalHold",
        "GetObjectLegalHold" => "s3:GetObjectLegalHold",
        "ListObjectVersions" => "s3:ListBucketVersions",
        "GetBucketAcl" => "s3:GetBucketAcl",
        "PutBucketAcl" => "s3:PutBucketAcl",
        "GetBucketTagging" => "s3:GetBucketTagging",
        "PutBucketTagging" | "DeleteBucketTagging" => "s3:PutBucketTagging",
        "GetBucketCors" => "s3:GetBucketCORS",
        "PutBucketCors" | "DeleteBucketCors" => "s3:PutBucketCORS",
        "GetBucketOwnershipControls" => "s3:GetBucketOwnershipControls",
        "PutBucketOwnershipControls" => "s3:PutBucketOwnershipControls",
        "GetBucketPolicy" => "s3:GetBucketPolicy",
        "PutBucketPolicy" => "s3:PutBucketPolicy",
        "DeleteBucketPolicy" => "s3:DeleteBucketPolicy",
        "GetBucketPolicyStatus" => "s3:GetBucketPolicyStatus",
        "GetPublicAccessBlock" => "s3:GetBucketPublicAccessBlock",
        "PutPublicAccessBlock" | "DeletePublicAccessBlock" => "s3:PutBucketPublicAccessBlock",
        "GetBucketLifecycle" => "s3:GetLifecycleConfiguration",
        "PutBucketLifecycle" | "DeleteBucketLifecycle" => "s3:PutLifecycleConfiguration",
        "DeleteBucketEncryption" => "s3:PutEncryptionConfiguration",
        "DeleteBucketWebsite" => "s3:DeleteBucketWebsite",
        "DeleteBucketReplication" => "s3:PutReplicationConfiguration",
        "GetObject" | "HeadObject" => "s3:GetObject",
        "PutObject" | "CopyObject" | "CreateMultipartUpload" | "UploadPart" | "UploadPartCopy" | "CompleteMultipartUpload" => {
            "s3:PutObject"
        }
        "ListParts" => "s3:ListMultipartUploadParts",
        "AbortMultipartUpload" => "s3:AbortMultipartUpload",
        "DeleteObject" => "s3:DeleteObject",
        "GetObjectTagging" => "s3:GetObjectTagging",
        "PutObjectTagging" => "s3:PutObjectTagging",
        "DeleteObjectTagging" => "s3:DeleteObjectTagging",
        "GetObjectAcl" => "s3:GetObjectAcl",
        "PutObjectAcl" => "s3:PutObjectAcl",
        _ => return None,
    })
}

/// The action for a request that names a version (`?versionId=`), e.g. s3:GetObjectVersion.
pub fn versioned_action(action: &'static str, versioned: bool) -> &'static str {
    if !versioned {
        return action;
    }
    match action {
        "s3:GetObject" => "s3:GetObjectVersion",
        "s3:DeleteObject" => "s3:DeleteObjectVersion",
        "s3:GetObjectTagging" => "s3:GetObjectVersionTagging",
        "s3:PutObjectTagging" => "s3:PutObjectVersionTagging",
        "s3:DeleteObjectTagging" => "s3:DeleteObjectVersionTagging",
        "s3:GetObjectAcl" => "s3:GetObjectVersionAcl",
        "s3:PutObjectAcl" => "s3:PutObjectVersionAcl",
        a => a,
    }
}

fn is_read(action: &str) -> bool {
    let a = action.to_ascii_lowercase();
    a.starts_with("s3:get") || a.starts_with("s3:list")
}

/// Bucket administration: reserved for admin keys unless a key policy grants it.
fn is_bucket_admin(action: &str) -> bool {
    action == "s3:CreateBucket"
        || action.starts_with("s3:DeleteBucket")
        || action.starts_with("s3:PutBucket")
        || (action.starts_with("s3:Put") && action.ends_with("Configuration"))
}

/// The bucket's public access block (all four settings; S3's names).
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct PublicAccessBlock {
    pub block_public_acls: bool,
    pub ignore_public_acls: bool,
    pub block_public_policy: bool,
    pub restrict_public_buckets: bool,
}

/// Client for Cairn's policy decision point.
pub struct Pdp {
    url: String,
    token: String,
    http: Client<HttpConnector, Full<Bytes>>,
    cache: Mutex<HashMap<String, (Instant, bool)>>,
}

#[derive(Deserialize)]
struct PdpDecision {
    decision: String,
}

impl Pdp {
    pub fn new(url: &str, token: &str) -> Result<Pdp, String> {
        if !url.starts_with("http://") {
            return Err("CAIRN_STORE_PDP_URL must be an http:// URL inside the deployment".into());
        }
        if token.is_empty() {
            return Err("CAIRN_STORE_PDP_TOKEN (a Cairn service token) is required with CAIRN_STORE_PDP_URL".into());
        }
        let mut conn = HttpConnector::new();
        conn.set_connect_timeout(Some(Duration::from_secs(2)));
        Ok(Pdp {
            url: format!("{}/governance/v1/authorize", url.trim_end_matches('/')),
            token: token.into(),
            http: Client::builder(TokioExecutor::new()).build(conn),
            cache: Mutex::new(HashMap::new()),
        })
    }

    /// true: denied. Errors count as denied.
    async fn denies(&self, actor: &str, action: &str, resource: &str, attrs: serde_json::Value) -> bool {
        let key = format!("{actor}\u{0}{action}\u{0}{resource}");
        if let Some((at, denied)) = self.cache.lock().unwrap().get(&key) {
            if at.elapsed() < PDP_TTL {
                return *denied;
            }
        }
        let body = serde_json::json!({
            "actor": actor,
            "privilege": action,
            "resource": {"type": if resource.contains('/') { "storeObject" } else { "storeBucket" }, "urn": resource},
            "resource_attributes": attrs,
        });
        let req = hyper::Request::post(&self.url)
            .header("content-type", "application/json")
            .header("authorization", format!("Bearer {}", self.token))
            .body(Full::new(Bytes::from(body.to_string())));
        let Ok(req) = req else { return true };
        let res = tokio::time::timeout(Duration::from_secs(3), async {
            let resp = self.http.request(req).await.map_err(|e| e.to_string())?;
            if resp.status() != StatusCode::OK {
                return Err(format!("HTTP {}", resp.status()));
            }
            let bytes = resp.into_body().collect().await.map_err(|e| e.to_string())?.to_bytes();
            serde_json::from_slice::<PdpDecision>(&bytes).map_err(|e| e.to_string())
        })
        .await;
        let denied = match res {
            Ok(Ok(d)) => d.decision == "DENY",
            Ok(Err(err)) => {
                tracing::warn!(error = %err, "policy decision point: refusing the request (fail closed)");
                return true;
            }
            Err(_) => {
                tracing::warn!("policy decision point timed out: refusing the request (fail closed)");
                return true;
            }
        };
        let mut cache = self.cache.lock().unwrap();
        if cache.len() >= PDP_CACHE_MAX {
            cache.retain(|_, (at, _)| at.elapsed() < PDP_TTL);
            if cache.len() >= PDP_CACHE_MAX {
                cache.clear();
            }
        }
        cache.insert(key, (Instant::now(), denied));
        denied
    }
}

impl Server {
    fn bucket_policy(&self, bucket: &str) -> Result<Option<Policy>, ApiError> {
        if bucket.is_empty() {
            return Ok(None);
        }
        match blocking(|| self.eng.get_config(bucket, "policy")) {
            Ok(Some(d)) => Ok(Some(Policy::parse(&d, Some(bucket))?)),
            Ok(None) | Err(crate::engine::Error::NoSuchBucket) => Ok(None),
            Err(err) => Err(err.into()),
        }
    }

    fn public_access_block(&self, bucket: &str) -> Result<PublicAccessBlock, ApiError> {
        match blocking(|| self.eng.get_config(bucket, "publicAccessBlock")) {
            Ok(Some(d)) => serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone()),
            Ok(None) | Err(crate::engine::Error::NoSuchBucket) => Ok(PublicAccessBlock::default()),
            Err(err) => Err(err.into()),
        }
    }

    /// Is public access blocked for this bucket (store-wide or by its public access block)?
    fn public_blocked(&self, bucket: &str, for_policy: bool) -> Result<bool, ApiError> {
        if self.block_public {
            return Ok(true);
        }
        let pab = self.public_access_block(bucket)?;
        Ok(if for_policy { pab.block_public_policy } else { pab.restrict_public_buckets })
    }

    /// Condition keys for a request (`key` empty for bucket-level actions).
    fn context(&self, c: &Ctx, bucket: &str, key: &str, want_tags: bool) -> BTreeMap<String, Vec<String>> {
        let mut m: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut put = |k: &str, v: String| {
            m.insert(k.to_string(), vec![v]);
        };
        if let Some(ip) = c.peer {
            put("aws:sourceip", ip.to_string());
        }
        put("aws:securetransport", self.tls.to_string());
        let now = (self.clock)();
        put("aws:currenttime", iso_time((now.unix_timestamp_nanos() / 1_000_000) as u64));
        put("aws:epochtime", now.unix_timestamp().to_string());
        let anonymous = c.auth.cred.access_key.is_empty();
        put("aws:principaltype", if anonymous { "Anonymous" } else { "User" }.into());
        if !anonymous {
            put("aws:username", c.auth.cred.access_key.clone());
            put("aws:userid", c.auth.cred.access_key.clone());
            put("s3:signatureversion", if c.auth.scope.is_empty() { "AWS" } else { "AWS4-HMAC-SHA256" }.into());
            put("s3:authtype", if c.headers.contains_key("authorization") { "REST-HEADER" } else { "REST-QUERY-STRING" }.into());
        }
        for (h, k) in [
            ("referer", "aws:referer"),
            ("user-agent", "aws:useragent"),
            ("x-amz-acl", "s3:x-amz-acl"),
            ("x-amz-content-sha256", "s3:x-amz-content-sha256"),
            ("x-amz-server-side-encryption", "s3:x-amz-server-side-encryption"),
            ("x-amz-object-lock-mode", "s3:object-lock-mode"),
            ("x-amz-object-lock-retain-until-date", "s3:object-lock-retain-until-date"),
            ("x-amz-object-lock-legal-hold", "s3:object-lock-legal-hold"),
        ] {
            let v = c.header(h);
            if !v.is_empty() {
                put(k, v.to_string());
            }
        }
        if key.is_empty() && bucket == c.bucket {
            for (q, k) in [("prefix", "s3:prefix"), ("delimiter", "s3:delimiter"), ("max-keys", "s3:max-keys")] {
                if c.has(q) {
                    put(k, c.q(q).to_string());
                }
            }
            if c.op.starts_with("ListObjects") && !c.has("prefix") {
                put("s3:prefix", String::new());
            }
        }
        if let Ok(tags) = super::config::parse_tag_header(c.header("x-amz-tagging")) {
            for (k, v) in tags {
                put(&format!("s3:requestobjecttag/{}", k.to_ascii_lowercase()), v);
            }
        }
        if want_tags && !key.is_empty() {
            if let Ok(o) = blocking(|| self.eng.get_object(bucket, key)) {
                for (k, v) in o.tags {
                    put(&format!("s3:existingobjecttag/{}", k.to_ascii_lowercase()), v);
                }
            }
        }
        m
    }

    /// Authorizes `action` on `bucket`/`key` for the request's key (or for anonymous access).
    pub(crate) async fn authorize(&self, c: &Ctx, action: &str, bucket: &str, key: &str) -> Result<(), ApiError> {
        let cred = &c.auth.cred;
        let anonymous = cred.access_key.is_empty();
        let resource = match (bucket.is_empty(), key.is_empty()) {
            (true, _) => "*".to_string(),
            (false, true) => format!("arn:aws:s3:::{bucket}"),
            (false, false) => format!("arn:aws:s3:::{bucket}/{key}"),
        };
        if cred.read_only && !is_read(action) {
            return Err(e::ACCESS_DENIED.clone());
        }
        let bucket_policy = self.bucket_policy(bucket)?;
        let key_policy = self.key_policies.get(&cred.access_key);
        let ctx = if bucket_policy.is_some() || key_policy.is_some() {
            self.context(c, bucket, key, !matches!(action, "s3:PutObject" | "s3:AbortMultipartUpload"))
        } else {
            BTreeMap::new()
        };
        let req = PolicyRequest { action, resource: &resource, principal: &cred.access_key, context: &ctx };
        let from_bucket = bucket_policy.as_ref().and_then(|p| p.evaluate(&req));
        let from_key = key_policy.and_then(|p| p.evaluate(&req));
        // Admin keys can always manage a bucket's policy, so a policy cannot lock them out.
        let lockout_exempt = cred.admin && matches!(action, "s3:GetBucketPolicy" | "s3:PutBucketPolicy" | "s3:DeleteBucketPolicy");
        if !lockout_exempt && (from_bucket == Some(Effect::Deny) || from_key == Some(Effect::Deny)) {
            return Err(e::ACCESS_DENIED.clone());
        }
        let allowed = if anonymous {
            from_bucket == Some(Effect::Allow) && !self.public_blocked(bucket, false)?
        } else if cred.admin || lockout_exempt {
            true
        } else {
            let identity = match key_policy {
                Some(_) => from_key == Some(Effect::Allow),
                None => (action == "s3:ListAllMyBuckets" || cred.allows(bucket)) && !is_bucket_admin(action),
            };
            identity || from_bucket == Some(Effect::Allow)
        };
        if !allowed {
            return Err(e::ACCESS_DENIED.clone());
        }
        if let (Some(pdp), false) = (&self.pdp, anonymous) {
            let actor = if cred.actor.is_empty() { format!("urn:li:corpuser:{}", cred.access_key) } else { cred.actor.clone() };
            let attrs = serde_json::json!({"bucket": bucket, "key": key, "store_key": cred.access_key});
            if pdp.denies(&actor, action, &resource, attrs).await {
                return Err(e::ACCESS_DENIED.clone());
            }
        }
        Ok(())
    }

    /// Authorizes the operation `classify` named. DeleteObjects is checked per key by its handler.
    pub(crate) async fn authorize_request(&self, c: &Ctx) -> Result<(), ApiError> {
        match c.op {
            "DeleteObjects" => Ok(()),
            op => match action_of(op) {
                Some(action) => self.authorize(c, versioned_action(action, c.has("versionId")), &c.bucket, &c.key).await,
                // Unsupported or not allowed: still only for signed requests.
                None if c.auth.cred.access_key.is_empty() => Err(e::ACCESS_DENIED.clone()),
                None => Ok(()),
            },
        }
    }

    // ------------------------------------------------------------------------------- bucket policy

    pub(crate) async fn put_bucket_policy<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 20 << 10).await?;
        let p = Policy::parse(&doc, Some(&c.bucket))?;
        if p.is_public() && self.public_blocked(&c.bucket, true)? {
            return Err(e::PUBLIC_POLICY_BLOCKED.clone());
        }
        blocking(|| self.eng.put_config(&c.bucket, "policy", Some(&doc)))?;
        Ok(empty(StatusCode::NO_CONTENT))
    }

    pub(crate) fn get_bucket_policy(&self, c: &Ctx) -> R {
        let doc = blocking(|| self.eng.get_config(&c.bucket, "policy"))?.ok_or_else(|| e::NO_SUCH_BUCKET_POLICY.clone())?;
        let mut r = super::full(StatusCode::OK, doc);
        super::set_header(&mut r, "content-type", "application/json");
        Ok(r)
    }

    pub(crate) fn get_policy_status(&self, c: &Ctx) -> R {
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let public = self.bucket_policy(&c.bucket)?.is_some_and(|p| p.is_public()) && !self.public_blocked(&c.bucket, false)?;
        let mut w = Writer::new("PolicyStatus");
        w.el("IsPublic", public.to_string());
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    // ------------------------------------------------------------------------------- public access block

    pub(crate) async fn put_public_access_block<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "PublicAccessBlockConfiguration")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        let flag = |n: &str| -> Result<bool, ApiError> {
            match root.text_of(n).as_str() {
                "" | "false" | "FALSE" => Ok(false),
                "true" | "TRUE" => Ok(true),
                _ => Err(e::MALFORMED_XML.clone()),
            }
        };
        let pab = PublicAccessBlock {
            block_public_acls: flag("BlockPublicAcls")?,
            ignore_public_acls: flag("IgnorePublicAcls")?,
            block_public_policy: flag("BlockPublicPolicy")?,
            restrict_public_buckets: flag("RestrictPublicBuckets")?,
        };
        let v = serde_json::to_vec(&pab).map_err(|_| e::INTERNAL.clone())?;
        blocking(|| self.eng.put_config(&c.bucket, "publicAccessBlock", Some(&v)))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_public_access_block(&self, c: &Ctx) -> R {
        let doc =
            blocking(|| self.eng.get_config(&c.bucket, "publicAccessBlock"))?.ok_or_else(|| e::NO_SUCH_PUBLIC_ACCESS_BLOCK.clone())?;
        let pab: PublicAccessBlock = serde_json::from_slice(&doc).map_err(|_| e::INTERNAL.clone())?;
        let mut w = Writer::new("PublicAccessBlockConfiguration");
        w.el("BlockPublicAcls", pab.block_public_acls.to_string())
            .el("IgnorePublicAcls", pab.ignore_public_acls.to_string())
            .el("BlockPublicPolicy", pab.block_public_policy.to_string())
            .el("RestrictPublicBuckets", pab.restrict_public_buckets.to_string());
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }
}
