//! Object lock (WORM): a bucket's lock configuration and default retention, per-version retention and
//! legal hold.
//!
//! The rules are S3's. COMPLIANCE retention can be extended but never shortened or removed, by anyone.
//! GOVERNANCE retention can be shortened or removed only with `x-amz-bypass-governance-retention: true`
//! and the s3:BypassGovernanceRetention permission. A legal hold blocks deletion until it is removed.
//! The engine enforces protection when a version is permanently deleted (by a client or by lifecycle);
//! overwrites and plain deletes in these always-versioned buckets keep the locked version.

use bytes::Bytes;
use hyper::StatusCode;
use serde::{Deserialize, Serialize};

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, iso_time, set_header, xml_resp, Ctx, Resp, Server};
use crate::engine::{self, Lock, Object};

type R = Result<Resp, ApiError>;

const DAY_MS: u64 = 86_400_000;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

/// A bucket's object lock configuration.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct LockConfig {
    #[serde(default)]
    pub mode: String,
    #[serde(default)]
    pub days: Option<u64>,
    #[serde(default)]
    pub years: Option<u64>,
}

impl LockConfig {
    /// The default retention for a version written at `now`.
    fn default_lock(&self, now: u64) -> Lock {
        let ms = self.days.map(|d| d * DAY_MS).or(self.years.map(|y| y * 365 * DAY_MS));
        match ms {
            Some(ms) if !self.mode.is_empty() => Lock { mode: self.mode.clone(), until: now + ms, legal_hold: false },
            _ => Lock::default(),
        }
    }
}

fn parse_time(v: &str) -> Result<u64, ApiError> {
    let t = time::OffsetDateTime::parse(v, &time::format_description::well_known::Rfc3339).map_err(|_| e::LOCK_INVALID_DATE.clone())?;
    Ok((t.unix_timestamp_nanos() / 1_000_000).max(0) as u64)
}

fn valid_mode(m: &str) -> bool {
    m == "GOVERNANCE" || m == "COMPLIANCE"
}

/// May a version's retention change from `old` to `new` (mode, until)? `bypass`: governance bypass allowed.
fn retention_change_allowed(old: &Lock, new_mode: &str, new_until: u64, now: u64, bypass: bool) -> Result<(), ApiError> {
    if old.until <= now || old.mode.is_empty() {
        return Ok(()); // no retention in force
    }
    let extends = new_mode == old.mode && new_until >= old.until;
    match old.mode.as_str() {
        "COMPLIANCE" if !extends => Err(e::LOCK_COMPLIANCE.clone()),
        "GOVERNANCE" if !extends && !bypass => Err(e::LOCK_GOVERNANCE.clone()),
        _ => Ok(()),
    }
}

impl Server {
    pub(crate) fn lock_config(&self, bucket: &str) -> Result<Option<LockConfig>, ApiError> {
        match blocking(|| self.eng.get_config(bucket, "object-lock"))? {
            None => Ok(None),
            Some(d) => Ok(Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?)),
        }
    }

    /// The lock a new version gets: from the request's x-amz-object-lock-* headers, else the bucket's
    /// default retention. Lock headers on a bucket without object lock are refused.
    pub(crate) fn lock_for_write(&self, c: &Ctx, bucket: &str) -> Result<Lock, ApiError> {
        let (mode, until, hold) =
            (c.header("x-amz-object-lock-mode"), c.header("x-amz-object-lock-retain-until-date"), c.header("x-amz-object-lock-legal-hold"));
        let cfg = self.lock_config(bucket)?;
        if cfg.is_none() {
            if !mode.is_empty() || !until.is_empty() || !hold.is_empty() {
                return Err(e::LOCK_NOT_CONFIGURED.clone());
            }
            return Ok(Lock::default());
        }
        let cfg = cfg.unwrap_or_default();
        let now = engine::now_ms();
        let mut lock = if mode.is_empty() && until.is_empty() {
            cfg.default_lock(now)
        } else {
            if mode.is_empty() || until.is_empty() || !valid_mode(mode) {
                return Err(e::LOCK_HEADERS.clone());
            }
            let until = parse_time(until)?;
            if until <= now {
                return Err(e::LOCK_PAST_DATE.clone());
            }
            Lock { mode: mode.to_string(), until, legal_hold: false }
        };
        lock.legal_hold = match hold {
            "" | "OFF" => false,
            "ON" => true,
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        if (!lock.mode.is_empty() || lock.legal_hold)
            && c.header("content-md5").is_empty()
            && !c
                .headers
                .keys()
                .any(|k| k.as_str().starts_with("x-amz-checksum-") && k.as_str() != "x-amz-checksum-type" || k.as_str() == "x-amz-trailer")
            && c.op != "CopyObject"
            && c.op != "CreateMultipartUpload"
        {
            // S3 requires an integrity check on writes that lock objects.
            return Err(e::LOCK_NEEDS_CHECKSUM.clone());
        }
        Ok(lock)
    }

    /// Lock headers on GET/HEAD.
    pub(crate) fn lock_headers(r: &mut Resp, o: &Object) {
        if !o.lock.mode.is_empty() {
            set_header(r, "x-amz-object-lock-mode", &o.lock.mode);
            set_header(r, "x-amz-object-lock-retain-until-date", &iso_time(o.lock.until));
        }
        if o.lock.legal_hold {
            set_header(r, "x-amz-object-lock-legal-hold", "ON");
        }
    }

    /// Is a governance bypass requested, and is the caller allowed to make it?
    pub(crate) async fn bypass_governance(&self, c: &Ctx, key: &str) -> Result<bool, ApiError> {
        if !c.header("x-amz-bypass-governance-retention").eq_ignore_ascii_case("true") {
            return Ok(false);
        }
        self.authorize(c, "s3:BypassGovernanceRetention", &c.bucket, key).await?;
        Ok(true)
    }

    // ------------------------------------------------------------------------------- bucket config

    pub(crate) async fn put_lock_config<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "ObjectLockConfiguration")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        if root.text_of("ObjectLockEnabled") != "Enabled" {
            return Err(e::MALFORMED_XML.clone());
        }
        let mut cfg = LockConfig::default();
        if let Some(d) = root.child("Rule").and_then(|r| r.child("DefaultRetention")) {
            cfg.mode = d.text_of("Mode");
            let num = |n: &str| -> Result<Option<u64>, ApiError> {
                match d.text_of(n).as_str() {
                    "" => Ok(None),
                    v => v.parse::<u64>().ok().filter(|n| *n > 0).map(Some).ok_or_else(|| e::LOCK_INVALID_PERIOD.clone()),
                }
            };
            cfg.days = num("Days")?;
            cfg.years = num("Years")?;
            if !valid_mode(&cfg.mode) || cfg.days.is_some() == cfg.years.is_some() {
                return Err(e::MALFORMED_XML.clone());
            }
        }
        let info = blocking(|| self.eng.head_bucket(&c.bucket))?;
        if info.versioning != "Enabled" {
            return Err(e::LOCK_NEEDS_VERSIONING_ENABLED.clone());
        }
        let v = serde_json::to_vec(&cfg).map_err(|_| e::INTERNAL.clone())?;
        blocking(|| self.eng.put_config(&c.bucket, "object-lock", Some(&v)))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_lock_config(&self, c: &Ctx) -> R {
        let cfg = self.lock_config(&c.bucket)?.ok_or_else(|| e::NO_SUCH_LOCK_CONFIG.clone())?;
        let mut w = Writer::new("ObjectLockConfiguration");
        w.el("ObjectLockEnabled", "Enabled");
        if !cfg.mode.is_empty() {
            w.open("Rule").open("DefaultRetention").el("Mode", &cfg.mode);
            if let Some(d) = cfg.days {
                w.el("Days", d.to_string());
            }
            if let Some(y) = cfg.years {
                w.el("Years", y.to_string());
            }
            w.close().close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    // ------------------------------------------------------------------------------- retention, hold

    pub(crate) async fn put_retention<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        if self.lock_config(&c.bucket)?.is_none() {
            return Err(e::LOCK_NOT_CONFIGURED.clone());
        }
        let root = xml::parse(&String::from_utf8_lossy(&doc)).filter(|r| r.name == "Retention").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let (mode, until) = (root.text_of("Mode"), root.text_of("RetainUntilDate"));
        let until = match (mode.is_empty(), until.is_empty()) {
            (true, true) => 0, // remove retention
            (false, false) if valid_mode(&mode) => parse_time(&until)?,
            _ => return Err(e::MALFORMED_XML.clone()),
        };
        let now = engine::now_ms();
        if until != 0 && until <= now {
            return Err(e::LOCK_PAST_DATE.clone());
        }
        let bypass = self.bypass_governance(c, &c.key).await?;
        let mut refused = None;
        blocking(|| {
            self.eng.update_object(&c.bucket, &c.key, Some(c.q("versionId")), |o| {
                if let Err(err) = retention_change_allowed(&o.lock, &mode, until, now, bypass) {
                    refused = Some(err);
                    return Err(engine::Error::ObjectLocked);
                }
                o.lock.mode = mode.clone();
                o.lock.until = until;
                Ok(())
            })
        })
        .map_err(|err| refused.take().unwrap_or_else(|| ApiError::from(err)))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_retention(&self, c: &Ctx) -> R {
        if self.lock_config(&c.bucket)?.is_none() {
            return Err(e::LOCK_NOT_CONFIGURED.clone());
        }
        let o = blocking(|| self.eng.get_version(&c.bucket, &c.key, Some(c.q("versionId"))))?;
        if o.lock.mode.is_empty() {
            return Err(e::NO_SUCH_OBJECT_LOCK.clone());
        }
        let mut w = Writer::new("Retention");
        w.el("Mode", &o.lock.mode).el("RetainUntilDate", iso_time(o.lock.until));
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    pub(crate) async fn put_legal_hold<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        if self.lock_config(&c.bucket)?.is_none() {
            return Err(e::LOCK_NOT_CONFIGURED.clone());
        }
        let root = xml::parse(&String::from_utf8_lossy(&doc)).filter(|r| r.name == "LegalHold").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let on = match root.text_of("Status").as_str() {
            "ON" => true,
            "OFF" => false,
            _ => return Err(e::MALFORMED_XML.clone()),
        };
        blocking(|| {
            self.eng.update_object(&c.bucket, &c.key, Some(c.q("versionId")), |o| {
                o.lock.legal_hold = on;
                Ok(())
            })
        })?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_legal_hold(&self, c: &Ctx) -> R {
        if self.lock_config(&c.bucket)?.is_none() {
            return Err(e::LOCK_NOT_CONFIGURED.clone());
        }
        let o = blocking(|| self.eng.get_version(&c.bucket, &c.key, Some(c.q("versionId"))))?;
        let mut w = Writer::new("LegalHold");
        w.el("Status", if o.lock.legal_hold { "ON" } else { "OFF" });
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retention_changes() {
        let now = 1_000_000;
        let gov = Lock { mode: "GOVERNANCE".into(), until: now + 1000, legal_hold: false };
        let comp = Lock { mode: "COMPLIANCE".into(), until: now + 1000, legal_hold: false };
        assert!(retention_change_allowed(&comp, "COMPLIANCE", now + 2000, now, false).is_ok()); // extend
        assert_eq!(retention_change_allowed(&comp, "COMPLIANCE", now + 500, now, true).unwrap_err(), e::LOCK_COMPLIANCE);
        assert_eq!(retention_change_allowed(&comp, "GOVERNANCE", now + 5000, now, true).unwrap_err(), e::LOCK_COMPLIANCE);
        assert_eq!(retention_change_allowed(&comp, "", 0, now, true).unwrap_err(), e::LOCK_COMPLIANCE);
        assert_eq!(retention_change_allowed(&gov, "", 0, now, false).unwrap_err(), e::LOCK_GOVERNANCE);
        assert!(retention_change_allowed(&gov, "", 0, now, true).is_ok());
        assert!(retention_change_allowed(&gov, "COMPLIANCE", now + 1000, now, true).is_ok());
        assert!(retention_change_allowed(&gov, "GOVERNANCE", now + 1500, now, false).is_ok());
        let expired = Lock { mode: "COMPLIANCE".into(), until: now - 1, legal_hold: false };
        assert!(retention_change_allowed(&expired, "", 0, now, false).is_ok());
        let cfg = LockConfig { mode: "GOVERNANCE".into(), days: Some(2), years: None };
        assert_eq!(cfg.default_lock(now).until, now + 2 * DAY_MS);
    }
}
