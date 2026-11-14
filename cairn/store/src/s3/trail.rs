//! Audit records for store changes (see `crate::audit`): configuration, buckets, versioning, quotas,
//! object lock changes, governance bypasses, and refused requests from signed keys.

use serde_json::{json, Value};

use super::errors::ApiError;
use super::{Ctx, Server};
use crate::audit::Audit;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

impl Server {
    /// An audit record for something this request did.
    pub(crate) fn audit_rec(&self, c: &Ctx, action: impl Into<String>, mut details: Value) -> Audit {
        let subject = if c.key.is_empty() { c.bucket.clone() } else { format!("{}/{}", c.bucket, c.key) };
        if !details.is_object() {
            details = json!({});
        }
        details["request_id"] = json!(c.request_id);
        details["source_ip"] = json!(c.peer.map(|p| p.to_string()).unwrap_or_default());
        Audit {
            category: "store",
            action: action.into(),
            actor: if c.auth.cred.access_key.is_empty() { "anonymous".into() } else { c.auth.cred.access_key.clone() },
            subject,
            details,
        }
    }

    /// Sets (Some) or removes (None) a bucket configuration document, with its audit entry, atomically.
    pub(crate) fn set_config(&self, c: &Ctx, kind: &str, doc: Option<&[u8]>) -> Result<(), ApiError> {
        let (verb, details) = match doc {
            Some(d) => ("put", json!({"sha256": super::auth::sha256_hex(d), "bytes": d.len()})),
            None => ("delete", json!({})),
        };
        let a = self.audit_rec(c, format!("config.{kind}.{verb}"), details);
        blocking(|| self.eng.put_config_audited(&c.bucket, kind, doc, Some(&a)))?;
        Ok(())
    }

    /// Records a refused request from a signed key (anonymous refusals are only logged, so scanners
    /// cannot flood the trail).
    pub(crate) fn audit_denial(&self, c: &Ctx, err: &ApiError) {
        if err.code != "AccessDenied" {
            return;
        }
        if c.auth.cred.access_key.is_empty() {
            tracing::info!(op = c.op, bucket = %c.bucket, key = %c.key, ip = ?c.peer, "anonymous request refused");
            return;
        }
        let a = self.audit_rec(c, "request.denied", json!({"operation": c.op, "reason": err.message}));
        if let Err(e) = blocking(|| self.eng.append_audit(&a)) {
            tracing::error!(error = %e, "audit trail");
        }
    }
}
