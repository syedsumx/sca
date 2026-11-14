//! Times to live: an object written with `x-cairn-ttl: <duration>` (or into a bucket with a default time
//! to live) expires that long after it is written. From then on it is gone for reads and listings on
//! every protocol, and the lifecycle worker deletes it (the version itself, in versioned buckets, so its
//! bytes go too). A copy and a completed multipart upload are new objects and count from then.
//!
//! Durations: seconds, or a number followed by s, m, h, d or w (at most 100 years); `0` on a write in a
//! bucket with a default means "no time to live". Refused with Object Lock (a locked object must not
//! vanish) and when lifecycle processing is off (nothing would delete expired objects).

use serde_json::{json, Value};

use super::errors::{self as e, ApiError};
use super::{Ctx, Server};
use crate::engine;

const MAX_SECS: u64 = 100 * 365 * 86_400;

/// Parses a duration (`3600`, `90s`, `15m`, `12h`, `7d`, `2w`) into seconds.
pub fn parse(v: &str) -> Option<u64> {
    let v = v.trim();
    let (num, unit) = match v.char_indices().last()? {
        (i, c) if c.is_ascii_alphabetic() => (&v[..i], c),
        _ => (v, 's'),
    };
    if num.is_empty() || !num.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let mult = match unit {
        's' => 1,
        'm' => 60,
        'h' => 3600,
        'd' => 86_400,
        'w' => 7 * 86_400,
        _ => return None,
    };
    num.parse::<u64>().ok()?.checked_mul(mult).filter(|s| *s <= MAX_SECS)
}

/// When an object written now with `ttl` seconds to live expires (0: never).
pub fn expires_at(ttl: u64) -> u64 {
    if ttl == 0 {
        0
    } else {
        engine::now_ms() + ttl * 1000
    }
}

impl Server {
    /// A bucket's default time to live in seconds (0: none).
    pub(crate) fn bucket_ttl(&self, bucket: &str) -> Result<u64, ApiError> {
        let doc = tokio::task::block_in_place(|| self.eng.get_config(bucket, "ttl"))?;
        Ok(doc.and_then(|d| serde_json::from_slice::<Value>(&d).ok()).and_then(|v| v["seconds"].as_u64()).unwrap_or(0))
    }

    /// The time to live a write asks for (its header, else the bucket's default), checked.
    pub(crate) fn ttl_for_write(&self, c: &Ctx, bucket: &str) -> Result<u64, ApiError> {
        let ttl = match c.header("x-cairn-ttl") {
            "" => self.bucket_ttl(bucket)?,
            v => parse(v).ok_or_else(|| e::INVALID_TTL.clone())?,
        };
        if ttl > 0 {
            self.ttl_allowed(bucket)?;
            self.mark_ttl_bucket(bucket)?;
        }
        Ok(ttl)
    }

    fn ttl_allowed(&self, bucket: &str) -> Result<(), ApiError> {
        if self.lifecycle_secs == 0 {
            return Err(e::TTL_NO_LIFECYCLE.clone());
        }
        if self.lock_config(bucket)?.is_some() {
            return Err(e::TTL_LOCKED.clone());
        }
        Ok(())
    }

    /// Remembers (in the index) that a bucket holds objects with a time to live, so the sweep visits it.
    fn mark_ttl_bucket(&self, bucket: &str) -> Result<(), ApiError> {
        if self.ttl_buckets.lock().unwrap().contains(bucket) {
            return Ok(());
        }
        tokio::task::block_in_place(|| -> Result<(), engine::Error> {
            if self.eng.get_config(bucket, "ttl-used")?.is_none() {
                self.eng.put_config_audited(bucket, "ttl-used", Some(b"1"), None)?;
            }
            Ok(())
        })?;
        self.ttl_buckets.lock().unwrap().insert(bucket.to_string());
        Ok(())
    }

    /// Sets (Some: a `{"seconds": n}` or `{"ttl": "7d"}` document) or removes a bucket's default.
    pub(crate) fn put_bucket_ttl(&self, bucket: &str, doc: Option<&[u8]>, actor: &str) -> Result<(), ApiError> {
        let secs = match doc {
            None => None,
            Some(d) => {
                let v: Value = serde_json::from_slice(d).map_err(|_| e::MALFORMED_JSON.clone())?;
                let secs = match (v.get("seconds"), v.get("ttl")) {
                    (Some(s), None) => s.as_u64().filter(|s| *s > 0 && *s <= MAX_SECS),
                    (None, Some(t)) => t.as_str().and_then(parse).filter(|s| *s > 0),
                    _ => None,
                }
                .ok_or_else(|| e::INVALID_TTL.clone())?;
                self.ttl_allowed(bucket)?;
                self.mark_ttl_bucket(bucket)?;
                Some(secs)
            }
        };
        let audit = crate::audit::Audit {
            category: "store",
            action: if secs.is_some() { "bucket.ttl.put" } else { "bucket.ttl.delete" }.into(),
            actor: actor.into(),
            subject: bucket.to_string(),
            details: json!({"seconds": secs}),
        };
        let body = secs.map(|s| json!({"seconds": s}).to_string());
        tokio::task::block_in_place(|| self.eng.put_config_audited(bucket, "ttl", body.as_deref().map(str::as_bytes), Some(&audit)))?;
        Ok(())
    }

    /// Deletes a bucket's expired objects (its versions too). Returns how many went.
    pub(crate) fn ttl_sweep(&self, bucket: &str, unversioned: bool) -> Result<u64, engine::Error> {
        if self.eng.get_config(bucket, "ttl-used")?.is_none() {
            return Ok(0);
        }
        let ev = engine::Ev { kind: "", lifecycle: true, ..Default::default() };
        let mut n = 0;
        let mut after = String::new();
        loop {
            let page = self
                .eng
                .list(bucket, &engine::ListOptions { after: after.clone(), max_keys: 1000, include_expired: true, ..Default::default() })?;
            for o in page.objects.iter().filter(|o| o.expired()) {
                let res = if unversioned {
                    self.eng.delete_object_ev(bucket, &o.key, Some(&o.etag), &ev)
                } else {
                    self.eng.delete_version_ev(bucket, &o.key, o.version(), false, &ev)
                };
                match res {
                    Ok(_) => n += 1,
                    Err(engine::Error::PreconditionFailed) | Err(engine::Error::NoSuchKey) | Err(engine::Error::NoSuchVersion) => {}
                    Err(err) => tracing::warn!(bucket, key = %o.key, error = %err, "time-to-live expiration"),
                }
            }
            if !page.truncated {
                break;
            }
            after = page.next_after;
        }
        // Noncurrent versions whose time ran out.
        if !unversioned {
            let (mut km, mut vm) = (String::new(), String::new());
            loop {
                let page = self.eng.list_versions(bucket, "", "", &km, &vm, 1000)?;
                for v in page.entries.iter().filter(|v| !v.latest && v.obj.expired()) {
                    if self.eng.delete_version_ev(bucket, &v.obj.key, v.obj.version(), false, &ev).is_ok() {
                        n += 1;
                    }
                }
                match (page.truncated, page.next_key.clone(), page.next_version.clone()) {
                    (true, k, v) => {
                        km = k;
                        vm = v;
                    }
                    _ => break,
                }
            }
        }
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn durations() {
        assert_eq!(super::parse("3600"), Some(3600));
        assert_eq!(super::parse("90s"), Some(90));
        assert_eq!(super::parse("15m"), Some(900));
        assert_eq!(super::parse("12h"), Some(43_200));
        assert_eq!(super::parse("7d"), Some(604_800));
        assert_eq!(super::parse("2w"), Some(1_209_600));
        assert_eq!(super::parse("0"), Some(0));
        for bad in ["", "h", "-1", "1.5h", "3y", "99999999999999999999", "101000d", "1 h"] {
            assert_eq!(super::parse(bad), None, "{bad}");
        }
    }
}
