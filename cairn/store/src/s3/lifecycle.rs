//! Lifecycle rules: expire current objects, delete markers and noncurrent versions, and abort incomplete
//! multipart uploads, per bucket. A background worker applies them (`CAIRN_STORE_LIFECYCLE_INTERVAL`).
//!
//! Safety: the worker deletes a current object only if it is still the version it looked at (If-Match on
//! its ETag), and removes noncurrent versions by version ID, so a concurrent write is never lost. Object
//! lock (when configured) is enforced by the engine, so a locked version is never expired.
//! Transitions to other storage classes are refused until storage tiers exist.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use hyper::StatusCode;
use serde::{Deserialize, Serialize};

use super::errors::{self as e, ApiError};
use super::xml::{self, Element, Writer};
use super::{empty, iso_time, xml_resp, Ctx, Resp, Server};
use crate::engine::{self, ListOptions, Object};

type R = Result<Resp, ApiError>;

const MAX_RULES: usize = 1000;

/// The event context of lifecycle actions.
static LIFECYCLE: engine::Ev =
    engine::Ev { kind: "", principal: String::new(), source_ip: String::new(), request_id: String::new(), lifecycle: true, audit: None };

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Filter {
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub tags: BTreeMap<String, String>,
    #[serde(default)]
    pub size_greater_than: Option<u64>,
    #[serde(default)]
    pub size_less_than: Option<u64>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Rule {
    pub id: String,
    pub enabled: bool,
    pub filter: Filter,
    /// Expire current versions this many days after they were written...
    pub expiration_days: Option<u64>,
    /// ... or on this date (ms).
    pub expiration_date: Option<u64>,
    pub expired_delete_marker: bool,
    pub noncurrent_days: Option<u64>,
    pub newer_noncurrent: Option<u64>,
    pub abort_days: Option<u64>,
    /// Whether the rule used <Filter> (vs the legacy top-level <Prefix>), to echo it back the same way.
    #[serde(default)]
    pub legacy_prefix: bool,
}

impl Filter {
    fn matches(&self, o: &Object) -> bool {
        o.key.starts_with(&self.prefix)
            && self.tags.iter().all(|(k, v)| o.tags.get(k) == Some(v))
            && self.size_greater_than.is_none_or(|n| o.size > n)
            && self.size_less_than.is_none_or(|n| o.size < n)
    }
}

fn days(el: &Element, name: &str) -> Result<Option<u64>, ApiError> {
    match el.text_of(name).as_str() {
        "" => Ok(None),
        v => match v.parse::<u64>() {
            Ok(n) if n > 0 => Ok(Some(n)),
            _ => Err(e::LIFECYCLE_INVALID_DAYS.clone()),
        },
    }
}

fn parse_filter(el: &Element) -> Result<Filter, ApiError> {
    let mut f = Filter::default();
    let tag = |t: &Element| -> Result<(String, String), ApiError> {
        let k = t.child("Key").ok_or_else(|| e::MALFORMED_XML.clone())?;
        Ok((k.text.clone(), t.child("Value").map(|v| v.text.clone()).unwrap_or_default()))
    };
    let size = |el: &Element, n: &str| -> Result<Option<u64>, ApiError> {
        match el.text_of(n).as_str() {
            "" => Ok(None),
            v => v.parse().map(Some).map_err(|_| e::MALFORMED_XML.clone()),
        }
    };
    let parts = el
        .children
        .iter()
        .filter(|c| ["Prefix", "Tag", "And", "ObjectSizeGreaterThan", "ObjectSizeLessThan"].contains(&c.name.as_str()))
        .count();
    if parts > 1 {
        return Err(e::MALFORMED_XML.clone()); // more than one condition needs <And>
    }
    if let Some(and) = el.child("And") {
        f.prefix = and.text_of_raw("Prefix");
        for t in and.all("Tag") {
            let (k, v) = tag(t)?;
            f.tags.insert(k, v);
        }
        f.size_greater_than = size(and, "ObjectSizeGreaterThan")?;
        f.size_less_than = size(and, "ObjectSizeLessThan")?;
    } else {
        f.prefix = el.text_of_raw("Prefix");
        if let Some(t) = el.child("Tag") {
            let (k, v) = tag(t)?;
            f.tags.insert(k, v);
        }
        f.size_greater_than = size(el, "ObjectSizeGreaterThan")?;
        f.size_less_than = size(el, "ObjectSizeLessThan")?;
    }
    let pairs: Vec<(String, String)> = f.tags.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
    super::config::validate_tags(&pairs, super::config::MAX_BUCKET_TAGS)?;
    Ok(f)
}

/// Parses and validates a LifecycleConfiguration document.
pub fn parse(doc: &[u8]) -> Result<Vec<Rule>, ApiError> {
    let root =
        xml::parse(&String::from_utf8_lossy(doc)).filter(|r| r.name == "LifecycleConfiguration").ok_or_else(|| e::MALFORMED_XML.clone())?;
    let mut rules: Vec<Rule> = vec![];
    for r in root.all("Rule") {
        for c in &r.children {
            match c.name.as_str() {
                "ID" | "Status" | "Filter" | "Prefix" | "Expiration" | "NoncurrentVersionExpiration" | "AbortIncompleteMultipartUpload" => {
                }
                "Transition" | "NoncurrentVersionTransition" => return Err(e::LIFECYCLE_TRANSITIONS_UNSUPPORTED.clone()),
                _ => return Err(e::MALFORMED_XML.clone()),
            }
        }
        let mut rule = Rule { id: r.text_of("ID"), ..Default::default() };
        if rule.id.is_empty() {
            rule.id = engine::new_id()[..12].to_string();
        }
        if rule.id.chars().count() > 255 || rules.iter().any(|x| x.id == rule.id) {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        rule.enabled = match r.text_of("Status").as_str() {
            "Enabled" => true,
            "Disabled" => false,
            _ => return Err(e::MALFORMED_XML.clone()),
        };
        match (r.child("Filter"), r.child("Prefix")) {
            (Some(f), None) => rule.filter = parse_filter(f)?,
            (None, Some(p)) => {
                rule.filter.prefix = p.text.clone();
                rule.legacy_prefix = true;
            }
            (None, None) => {}
            (Some(_), Some(_)) => return Err(e::MALFORMED_XML.clone()),
        }
        if let Some(x) = r.child("Expiration") {
            rule.expiration_days = days(x, "Days")?;
            let date = x.text_of("Date");
            if !date.is_empty() {
                let t = time::OffsetDateTime::parse(&date, &time::format_description::well_known::Rfc3339)
                    .map_err(|_| e::LIFECYCLE_INVALID_DATE.clone())?;
                if t.time() != time::Time::MIDNIGHT {
                    return Err(e::LIFECYCLE_INVALID_DATE.clone());
                }
                rule.expiration_date = Some((t.unix_timestamp_nanos() / 1_000_000) as u64);
            }
            rule.expired_delete_marker = x.text_of("ExpiredObjectDeleteMarker") == "true";
            let set =
                [rule.expiration_days.is_some(), rule.expiration_date.is_some(), rule.expired_delete_marker].iter().filter(|b| **b).count();
            if set != 1 {
                return Err(e::MALFORMED_XML.clone());
            }
            if rule.expired_delete_marker && !rule.filter.tags.is_empty() {
                return Err(e::INVALID_ARGUMENT.clone());
            }
        }
        if let Some(x) = r.child("NoncurrentVersionExpiration") {
            rule.noncurrent_days = days(x, "NoncurrentDays")?;
            rule.newer_noncurrent = match x.text_of("NewerNoncurrentVersions").as_str() {
                "" => None,
                v => Some(v.parse::<u64>().ok().filter(|n| (1..=100).contains(n)).ok_or_else(|| e::INVALID_ARGUMENT.clone())?),
            };
            if rule.noncurrent_days.is_none() {
                return Err(e::MALFORMED_XML.clone());
            }
        }
        if let Some(x) = r.child("AbortIncompleteMultipartUpload") {
            rule.abort_days = days(x, "DaysAfterInitiation")?;
            if rule.abort_days.is_none() || !rule.filter.tags.is_empty() {
                return Err(e::INVALID_ARGUMENT.clone());
            }
        }
        if rule.expiration_days.is_none()
            && rule.expiration_date.is_none()
            && !rule.expired_delete_marker
            && rule.noncurrent_days.is_none()
            && rule.abort_days.is_none()
        {
            return Err(e::LIFECYCLE_NO_ACTION.clone());
        }
        rules.push(rule);
    }
    if rules.is_empty() || rules.len() > MAX_RULES {
        return Err(e::MALFORMED_XML.clone());
    }
    Ok(rules)
}

pub fn to_xml(rules: &[Rule]) -> String {
    let mut w = Writer::new("LifecycleConfiguration");
    for r in rules {
        w.open("Rule").el("ID", &r.id);
        if r.legacy_prefix {
            w.el("Prefix", &r.filter.prefix);
        } else {
            w.open("Filter");
            let f = &r.filter;
            let n = (!f.prefix.is_empty()) as usize
                + f.tags.len()
                + f.size_greater_than.is_some() as usize
                + f.size_less_than.is_some() as usize;
            if n > 1 {
                w.open("And");
            }
            if !f.prefix.is_empty() || n == 0 {
                w.el("Prefix", &f.prefix);
            }
            for (k, v) in &f.tags {
                w.open("Tag").el("Key", k).el("Value", v).close();
            }
            if let Some(s) = f.size_greater_than {
                w.el("ObjectSizeGreaterThan", s.to_string());
            }
            if let Some(s) = f.size_less_than {
                w.el("ObjectSizeLessThan", s.to_string());
            }
            if n > 1 {
                w.close();
            }
            w.close();
        }
        w.el("Status", if r.enabled { "Enabled" } else { "Disabled" });
        if r.expiration_days.is_some() || r.expiration_date.is_some() || r.expired_delete_marker {
            w.open("Expiration");
            if let Some(d) = r.expiration_days {
                w.el("Days", d.to_string());
            }
            if let Some(d) = r.expiration_date {
                w.el("Date", iso_time(d));
            }
            if r.expired_delete_marker {
                w.el("ExpiredObjectDeleteMarker", "true");
            }
            w.close();
        }
        if let Some(d) = r.noncurrent_days {
            w.open("NoncurrentVersionExpiration").el("NoncurrentDays", d.to_string());
            if let Some(n) = r.newer_noncurrent {
                w.el("NewerNoncurrentVersions", n.to_string());
            }
            w.close();
        }
        if let Some(d) = r.abort_days {
            w.open("AbortIncompleteMultipartUpload").el("DaysAfterInitiation", d.to_string()).close();
        }
        w.close();
    }
    w.finish()
}

/// What one lifecycle pass did.
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct Pass {
    pub expired: u64,
    pub delete_markers_removed: u64,
    pub noncurrent_expired: u64,
    pub uploads_aborted: u64,
}

#[derive(Default)]
pub struct LifecycleMetrics {
    pub expired: AtomicU64,
    pub delete_markers_removed: AtomicU64,
    pub noncurrent_expired: AtomicU64,
    pub uploads_aborted: AtomicU64,
    pub runs: AtomicU64,
}

impl Server {
    pub(crate) fn lifecycle_rules(&self, bucket: &str) -> Result<Option<Vec<Rule>>, ApiError> {
        match blocking(|| self.eng.get_config(bucket, "lifecycle"))? {
            None => Ok(None),
            Some(d) => Ok(Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?)),
        }
    }

    pub(crate) async fn put_lifecycle<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 1 << 20).await?;
        let rules = parse(&doc)?;
        let v = serde_json::to_vec(&rules).map_err(|_| e::INTERNAL.clone())?;
        self.set_config(c, "lifecycle", Some(&v))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_lifecycle(&self, c: &Ctx) -> R {
        let rules = self.lifecycle_rules(&c.bucket)?.ok_or_else(|| e::NO_SUCH_LIFECYCLE.clone())?;
        Ok(xml_resp(StatusCode::OK, to_xml(&rules)))
    }

    /// The `x-amz-expiration` header for a current object (the earliest applicable expiration rule).
    pub(crate) fn expiration_header(&self, bucket: &str, o: &Object) -> Option<String> {
        let rules = self.lifecycle_rules(bucket).ok().flatten()?;
        let day = self.lifecycle_day_ms;
        rules
            .iter()
            .filter(|r| r.enabled && r.filter.matches(o))
            .filter_map(|r| {
                let at = r.expiration_days.map(|d| o.modified + d * day).or(r.expiration_date)?;
                Some((at, r.id.clone()))
            })
            .min()
            .map(|(at, id)| format!("expiry-date=\"{}\", rule-id=\"{}\"", super::http_time(at), id))
    }

    /// Applies every bucket's lifecycle rules once.
    pub fn lifecycle_pass(&self, now: u64) -> Pass {
        let mut total = Pass::default();
        let buckets = self.eng.list_buckets().unwrap_or_default();
        for b in buckets {
            let rules = match self.eng.get_config(&b.name, "lifecycle") {
                Ok(Some(d)) => serde_json::from_slice::<Vec<Rule>>(&d).unwrap_or_default(),
                _ => continue,
            };
            let rules: Vec<Rule> = rules.into_iter().filter(|r| r.enabled).collect();
            if rules.is_empty() {
                continue;
            }
            match self.apply_rules(&b.name, &rules, now) {
                Ok(p) => {
                    total.expired += p.expired;
                    total.delete_markers_removed += p.delete_markers_removed;
                    total.noncurrent_expired += p.noncurrent_expired;
                    total.uploads_aborted += p.uploads_aborted;
                }
                Err(err) => tracing::warn!(bucket = %b.name, error = %err, "lifecycle pass"),
            }
        }
        let m = &self.lifecycle_metrics;
        m.expired.fetch_add(total.expired, Ordering::Relaxed);
        m.delete_markers_removed.fetch_add(total.delete_markers_removed, Ordering::Relaxed);
        m.noncurrent_expired.fetch_add(total.noncurrent_expired, Ordering::Relaxed);
        m.uploads_aborted.fetch_add(total.uploads_aborted, Ordering::Relaxed);
        m.runs.fetch_add(1, Ordering::Relaxed);
        if total != Pass::default() {
            tracing::info!(
                expired = total.expired,
                delete_markers_removed = total.delete_markers_removed,
                noncurrent_expired = total.noncurrent_expired,
                uploads_aborted = total.uploads_aborted,
                "lifecycle pass"
            );
        }
        total
    }

    fn apply_rules(&self, bucket: &str, rules: &[Rule], now: u64) -> Result<Pass, engine::Error> {
        let day = self.lifecycle_day_ms;
        let mut pass = Pass::default();
        // Current versions.
        let current_rules: Vec<&Rule> = rules.iter().filter(|r| r.expiration_days.is_some() || r.expiration_date.is_some()).collect();
        if !current_rules.is_empty() {
            let mut after = String::new();
            loop {
                let page = self.eng.list(bucket, &ListOptions { after: after.clone(), max_keys: 1000, ..Default::default() })?;
                for o in &page.objects {
                    let due = current_rules.iter().any(|r| {
                        r.filter.matches(o)
                            && (r.expiration_days.is_some_and(|d| now >= o.modified + d * day)
                                || r.expiration_date.is_some_and(|t| now >= t))
                    });
                    if due {
                        match self.eng.delete_object_ev(bucket, &o.key, Some(&o.etag), &LIFECYCLE) {
                            Ok(_) => pass.expired += 1,
                            Err(engine::Error::PreconditionFailed) => {} // rewritten meanwhile
                            Err(err) => tracing::warn!(bucket, key = %o.key, error = %err, "lifecycle expiration"),
                        }
                    }
                }
                if !page.truncated {
                    break;
                }
                after = page.next_after;
            }
        }
        // Noncurrent versions and expired delete markers.
        let version_rules: Vec<&Rule> = rules.iter().filter(|r| r.noncurrent_days.is_some() || r.expired_delete_marker).collect();
        if !version_rules.is_empty() {
            let (mut key_marker, mut vid_marker) = (String::new(), String::new());
            let mut per_key: BTreeMap<String, Vec<engine::VersionEntry>> = BTreeMap::new();
            loop {
                let page = self.eng.list_versions(bucket, "", "", &key_marker, &vid_marker, 1000)?;
                for v in page.entries {
                    per_key.entry(v.obj.key.clone()).or_default().push(v);
                }
                if !page.truncated {
                    break;
                }
                key_marker = page.next_key;
                vid_marker = page.next_version;
            }
            for (key, entries) in per_key {
                let noncurrent: Vec<&engine::VersionEntry> = entries.iter().filter(|v| !v.latest).collect();
                for r in &version_rules {
                    let Some(d) = r.noncurrent_days else { continue };
                    let keep = r.newer_noncurrent.unwrap_or(0);
                    let mut newer = 0u64; // noncurrent versions (not markers) newer than this one
                    for v in &noncurrent {
                        let o = &v.obj;
                        // Markers have no size or tags: only the prefix applies to them.
                        let applies = if o.delete_marker { key.starts_with(&r.filter.prefix) } else { r.filter.matches(o) };
                        let kept = !o.delete_marker && newer < keep;
                        if !o.delete_marker {
                            newer += 1;
                        }
                        let since = if o.noncurrent_since > 0 { o.noncurrent_since } else { o.modified };
                        if applies
                            && !kept
                            && now >= since + d * day
                            && self.eng.delete_version_ev(bucket, &key, o.version(), false, &LIFECYCLE).is_ok()
                        {
                            pass.noncurrent_expired += 1;
                        }
                    }
                }
                // A delete marker that is the only version left is an expired delete marker.
                if version_rules.iter().any(|r| r.expired_delete_marker && key.starts_with(&r.filter.prefix)) {
                    if let Ok(list) = self.eng.list_versions(bucket, &key, "", "", "", 2) {
                        let mine: Vec<_> = list.entries.iter().filter(|v| v.obj.key == key).collect();
                        if mine.len() == 1
                            && mine[0].obj.delete_marker
                            && self.eng.delete_version_ev(bucket, &key, mine[0].obj.version(), false, &LIFECYCLE).is_ok()
                        {
                            pass.delete_markers_removed += 1;
                        }
                    }
                }
            }
        }
        // Incomplete multipart uploads.
        for r in rules.iter().filter(|r| r.abort_days.is_some()) {
            let d = r.abort_days.unwrap_or(0);
            for u in self.eng.list_uploads(bucket, &r.filter.prefix)? {
                if now >= u.initiated + d * day && self.eng.abort_upload(bucket, &u.key, &u.id).is_ok() {
                    pass.uploads_aborted += 1;
                }
            }
        }
        Ok(pass)
    }

    /// Runs lifecycle passes forever, every `interval`.
    pub async fn run_lifecycle(self: Arc<Self>, interval: Duration) {
        let mut tick = tokio::time::interval(interval);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        tick.tick().await;
        loop {
            tick.tick().await;
            let me = self.clone();
            let _ = tokio::task::spawn_blocking(move || me.lifecycle_pass(engine::now_ms())).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn documents() {
        let doc = br#"<LifecycleConfiguration>
            <Rule><ID>raw</ID><Filter><And><Prefix>raw/</Prefix><Tag><Key>tier</Key><Value>tmp</Value></Tag></And></Filter><Status>Enabled</Status>
              <Expiration><Days>7</Days></Expiration><NoncurrentVersionExpiration><NoncurrentDays>30</NoncurrentDays><NewerNoncurrentVersions>3</NewerNoncurrentVersions></NoncurrentVersionExpiration></Rule>
            <Rule><ID>mpu</ID><Filter><Prefix></Prefix></Filter><Status>Disabled</Status><AbortIncompleteMultipartUpload><DaysAfterInitiation>2</DaysAfterInitiation></AbortIncompleteMultipartUpload></Rule>
            <Rule><ID>old</ID><Prefix>logs/</Prefix><Status>Enabled</Status><Expiration><Date>2030-01-01T00:00:00.000Z</Date></Expiration></Rule>
        </LifecycleConfiguration>"#;
        let rules = parse(doc).unwrap();
        assert_eq!(rules.len(), 3);
        assert_eq!(rules[0].filter.tags.get("tier").unwrap(), "tmp");
        assert_eq!((rules[0].expiration_days, rules[0].noncurrent_days, rules[0].newer_noncurrent), (Some(7), Some(30), Some(3)));
        assert!(!rules[1].enabled && rules[1].abort_days == Some(2));
        assert!(rules[2].legacy_prefix && rules[2].expiration_date.is_some());
        assert_eq!(parse(to_xml(&rules).as_bytes()).unwrap(), rules);
    }

    #[test]
    fn validation() {
        let bad = |rule: &str| {
            parse(format!("<LifecycleConfiguration><Rule><ID>x</ID>{rule}</Rule></LifecycleConfiguration>").as_bytes()).unwrap_err()
        };
        assert_eq!(
            bad("<Filter><Prefix/></Filter><Status>Enabled</Status><Transition><Days>30</Days><StorageClass>GLACIER</StorageClass></Transition>"),
            e::LIFECYCLE_TRANSITIONS_UNSUPPORTED
        );
        assert_eq!(
            bad("<Filter><Prefix/></Filter><Status>Enabled</Status><Expiration><Days>0</Days></Expiration>"),
            e::LIFECYCLE_INVALID_DAYS
        );
        assert_eq!(
            bad("<Filter><Prefix/></Filter><Status>Enabled</Status><Expiration><Date>2030-01-01T10:00:00Z</Date></Expiration>"),
            e::LIFECYCLE_INVALID_DATE
        );
        assert_eq!(bad("<Filter><Prefix/></Filter><Status>Enabled</Status>"), e::LIFECYCLE_NO_ACTION);
        assert_eq!(bad("<Filter><Prefix/></Filter><Status>Maybe</Status><Expiration><Days>1</Days></Expiration>"), e::MALFORMED_XML);
        assert_eq!(
            bad("<Filter><Prefix>a</Prefix><Tag><Key>k</Key><Value>v</Value></Tag></Filter><Status>Enabled</Status><Expiration><Days>1</Days></Expiration>"),
            e::MALFORMED_XML
        );
    }

    #[test]
    fn filters() {
        let f = Filter {
            prefix: "raw/".into(),
            tags: [("tier".into(), "tmp".into())].into(),
            size_greater_than: Some(10),
            size_less_than: None,
        };
        let mut o = Object { key: "raw/a".into(), size: 11, tags: [("tier".into(), "tmp".into())].into(), ..Default::default() };
        assert!(f.matches(&o));
        o.size = 10;
        assert!(!f.matches(&o));
        o.size = 11;
        o.tags.clear();
        assert!(!f.matches(&o));
    }
}
