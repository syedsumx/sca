//! Data residency (`CAIRN_STORE_RESIDENCY_FILE`: the rules file Cairn reads as `CAIRN_RESIDENCY_FILE` and Cairn
//! Catalog as `CAIRN_CATALOG_RESIDENCY_FILE`).
//!
//! A pinned namespace's tables live under `<its region's location>/<namespace levels>` (Cairn Catalog keeps them
//! there). The store sends what it holds elsewhere in four ways: bucket replication (and two-way sync), backups to
//! a sink (on request, on a schedule, and continuous), write-back to a mounted remote bucket, and lifecycle
//! transitions to a storage tier. Each destination is in a region (`residency_region` on the replication target,
//! sink, remote or tier). An object under a region's location goes only to a destination whose region the pin of
//! its namespace allows; a destination with no region gets none. That is checked when the transfer is configured
//! (a replication configuration, a lifecycle rule, a write-back mount, a backup run, continuous backup at start)
//! and again for every object sent, so rules that change later stop what they no longer allow.
//!
//! An object under a region's location whose namespace no pin names stays in that region. Anything that cannot be
//! decided (a namespace two pins put in different regions or allowances) refuses: the store fails closed. A file
//! that is set but does not load stops the store at start.

use std::collections::{BTreeMap, BTreeSet};

use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RegionDoc {
    location: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PinDoc {
    namespaces: Vec<String>,
    region: String,
    #[serde(default)]
    allow: Vec<String>,
}

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct Doc {
    #[serde(default)]
    regions: BTreeMap<String, RegionDoc>,
    #[serde(default)]
    pins: Vec<PinDoc>,
}

#[derive(Debug, Clone)]
struct Pin {
    patterns: Vec<String>,
    region: String,
    allow: BTreeSet<String>,
}

/// A region's location: its bucket and the path under it (no slashes at either end; may be empty).
#[derive(Debug, Clone)]
struct Location {
    region: String,
    bucket: String,
    path: String,
}

#[derive(Debug, Default, Clone)]
pub struct Residency {
    locations: Vec<Location>,
    pins: Vec<Pin>,
}

pub fn valid_region(r: &str) -> bool {
    let b = r.as_bytes();
    !b.is_empty()
        && b.len() <= 32
        && b[0].is_ascii_lowercase()
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

/// `*` (any run of characters) and `?` (one), as the pins' namespace patterns are written.
fn glob(p: &[u8], s: &[u8]) -> bool {
    let (mut pi, mut si, mut star, mut mark) = (0, 0, usize::MAX, 0);
    while si < s.len() {
        if pi < p.len() && (p[pi] == b'?' || p[pi] == s[si]) {
            pi += 1;
            si += 1;
        } else if pi < p.len() && p[pi] == b'*' {
            star = pi;
            mark = si;
            pi += 1;
        } else if star != usize::MAX {
            pi = star + 1;
            mark += 1;
            si = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == b'*' {
        pi += 1;
    }
    pi == p.len()
}

fn where_(dest: &str) -> String {
    if dest.is_empty() {
        "a destination with no residency_region".into()
    } else {
        format!("region {dest}")
    }
}

impl Residency {
    pub fn parse(text: &str) -> Result<Residency, String> {
        // Known keys only: a misspelt key would otherwise leave namespaces unpinned.
        let value: serde_json::Value = serde_json::from_str(text).map_err(|e| format!("not JSON: {e}"))?;
        if !value.is_object() {
            return Err("the residency file is a JSON object".into());
        }
        let doc: Doc = serde_json::from_value(value).map_err(|e| format!("not a residency file: {e}"))?;
        let mut locations = vec![];
        for (name, r) in &doc.regions {
            let rest = r.location.strip_prefix("s3://").unwrap_or("");
            let (bucket, path) = rest.split_once('/').unwrap_or((rest, ""));
            if !valid_region(name) || bucket.len() < 3 {
                return Err(format!("regions.{name}: a region name and an s3:// location"));
            }
            locations.push(Location { region: name.clone(), bucket: bucket.into(), path: path.trim_matches('/').into() });
        }
        let mut pins = vec![];
        for (i, p) in doc.pins.into_iter().enumerate() {
            if !doc.regions.contains_key(&p.region) {
                return Err(format!("pins[{i}].region: {} is not one of the regions", p.region));
            }
            if p.namespaces.is_empty() {
                return Err(format!("pins[{i}].namespaces: at least one"));
            }
            let allow: BTreeSet<String> = if p.allow.is_empty() { [p.region.clone()].into() } else { p.allow.into_iter().collect() };
            if !allow.contains(&p.region) || !allow.iter().all(|a| doc.regions.contains_key(a)) {
                return Err(format!("pins[{i}].allow: regions, its own ({}) among them", p.region));
            }
            pins.push(Pin { patterns: p.namespaces.into_iter().map(|n| n.to_lowercase()).collect(), region: p.region, allow });
        }
        Ok(Residency { locations, pins })
    }

    pub fn from_env() -> Result<Residency, String> {
        match std::env::var("CAIRN_STORE_RESIDENCY_FILE") {
            Ok(path) if !path.trim().is_empty() => {
                let text = std::fs::read_to_string(path.trim()).map_err(|e| format!("{path}: {e}"))?;
                Residency::parse(&text)
            }
            _ => Ok(Residency::default()),
        }
    }

    pub fn active(&self) -> bool {
        !self.pins.is_empty()
    }

    /// The pin of a namespace (`a` or `a.b`): its region and allowance. Two pins that disagree: an error.
    fn pin(&self, ns: &str) -> Result<Option<(&str, &BTreeSet<String>)>, String> {
        let ns = ns.to_lowercase();
        let mut found: Option<&Pin> = None;
        for p in &self.pins {
            if p.patterns.iter().any(|pat| glob(pat.as_bytes(), ns.as_bytes())) {
                match found {
                    Some(f) if (f.region.as_str(), &f.allow) != (p.region.as_str(), &p.allow) => {
                        return Err(format!("{ns} is pinned to more than one region"))
                    }
                    _ => found = Some(p),
                }
            }
        }
        Ok(found.map(|p| (p.region.as_str(), &p.allow)))
    }

    /// Every region's allowance intersected for the namespaces named by `levels` (the path segments after a
    /// region's location): the pins of `a`, `a.b`, ... that put them in `region`. None when no pin names them.
    fn allow_of_levels(&self, region: &str, levels: &[&str]) -> Result<Option<BTreeSet<String>>, String> {
        let mut out: Option<BTreeSet<String>> = None;
        for i in 1..=levels.len() {
            if let Some((r, allow)) = self.pin(&levels[..i].join("."))? {
                if r == region {
                    out = Some(match out {
                        None => allow.clone(),
                        Some(o) => o.intersection(allow).cloned().collect(),
                    });
                }
            }
        }
        Ok(out)
    }

    /// What every pin in `region` allows (the region's own location as a whole).
    fn allow_of_region(&self, region: &str) -> BTreeSet<String> {
        let mut out: BTreeSet<String> = [region.to_string()].into();
        let mut all: Option<BTreeSet<String>> = None;
        for p in self.pins.iter().filter(|p| p.region == region) {
            all = Some(match all {
                None => p.allow.clone(),
                Some(a) => a.intersection(&p.allow).cloned().collect(),
            });
        }
        if let Some(a) = all {
            out = a; // always holds the region itself
        }
        out
    }

    /// The location of `bucket` that `key` lies under, and the path segments after it.
    fn locate<'a>(&'a self, bucket: &str, key: &'a str) -> Option<(&'a Location, Vec<&'a str>)> {
        self.locations.iter().find_map(|l| {
            if l.bucket != bucket {
                return None;
            }
            let rest = if l.path.is_empty() {
                key
            } else if key == l.path {
                ""
            } else {
                key.strip_prefix(&l.path)?.strip_prefix('/')?
            };
            Some((l, rest.split('/').filter(|s| !s.is_empty()).collect()))
        })
    }

    /// May the object `bucket/key` go to a destination in `dest` ("" for none)? The reason when not.
    pub fn check_object(&self, bucket: &str, key: &str, dest: &str) -> Result<(), String> {
        if !self.active() {
            return Ok(());
        }
        let Some((loc, segs)) = self.locate(bucket, key) else { return Ok(()) };
        // The namespace levels: every segment but the object's own name.
        let levels = &segs[..segs.len().saturating_sub(1)];
        let allow = self.allow_of_levels(&loc.region, levels)?.unwrap_or_else(|| [loc.region.clone()].into());
        if allow.contains(dest) {
            return Ok(());
        }
        Err(format!(
            "{bucket}/{key} is in region {}'s location and may go only to {}, not to {}",
            loc.region,
            allow.iter().cloned().collect::<Vec<_>>().join(", "),
            where_(dest)
        ))
    }

    /// May the objects of `bucket` under `prefix` ("" for all) go to `dest`? Checked when a transfer is
    /// configured: a prefix that does not name one namespace counts every pin of the region.
    pub fn check_prefix(&self, bucket: &str, prefix: &str, dest: &str) -> Result<(), String> {
        if !self.active() {
            return Ok(());
        }
        for l in self.locations.iter().filter(|l| l.bucket == bucket) {
            let base = if l.path.is_empty() { String::new() } else { format!("{}/", l.path) };
            let allow = if let Some(rest) = prefix.strip_prefix(&base) {
                // Inside the location: the complete segments name namespace levels.
                let mut segs: Vec<&str> = rest.split('/').collect();
                segs.pop(); // a partial last segment (or "" after a trailing slash) names nothing yet
                match self.allow_of_levels(&l.region, &segs)? {
                    Some(a) => a,
                    None => self.allow_of_region(&l.region),
                }
            } else if base.starts_with(prefix) || prefix == l.path {
                self.allow_of_region(&l.region) // the prefix takes in the whole location
            } else {
                continue;
            };
            if !allow.contains(dest) {
                return Err(format!(
                    "{bucket}/{prefix}* takes in region {}'s pinned data, which may go only to {}, not to {}",
                    l.region,
                    allow.iter().cloned().collect::<Vec<_>>().join(", "),
                    where_(dest)
                ));
            }
        }
        Ok(())
    }

    /// May everything in the buckets go to `dest` (a whole-store backup)?
    pub fn check_buckets(&self, buckets: &[String], dest: &str) -> Result<(), String> {
        for b in buckets {
            self.check_prefix(b, "", dest)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RULES: &str = r#"{"regions": {"eu": {"location": "s3://lake-eu/warehouse"}, "us": {"location": "s3://lake-us/warehouse"}},
        "pins": [{"namespaces": ["crm_eu"], "region": "eu"},
                 {"namespaces": ["stats_*"], "region": "us", "allow": ["us", "eu"]},
                 {"namespaces": ["vault_us"], "region": "us"}]}"#;

    #[test]
    fn objects_go_only_where_their_pin_allows() {
        let r = Residency::parse(RULES).unwrap();
        assert!(r.check_object("lake-eu", "warehouse/crm_eu/people-1/data/a.parquet", "eu").is_ok());
        assert!(r.check_object("lake-eu", "warehouse/crm_eu/people-1/data/a.parquet", "us").is_err());
        assert!(r.check_object("lake-eu", "warehouse/crm_eu/people-1/data/a.parquet", "").is_err());
        assert!(r.check_object("lake-us", "warehouse/stats_daily/t/data/a.parquet", "eu").is_ok());
        assert!(r.check_object("lake-us", "warehouse/vault_us/t/data/a.parquet", "eu").is_err());
        assert!(r.check_object("lake-eu", "warehouse/stray.txt", "us").is_err()); // stays in its region
        assert!(r.check_object("lake-eu", "elsewhere/x", "").is_ok()); // not under the location
        assert!(r.check_object("lakehouse", "warehouse/raw/t/x", "").is_ok());
    }

    #[test]
    fn configurations_count_every_pin_they_take_in() {
        let r = Residency::parse(RULES).unwrap();
        assert!(r.check_prefix("lake-eu", "", "us").is_err());
        assert!(r.check_prefix("lake-eu", "", "eu").is_ok());
        assert!(r.check_prefix("lake-us", "", "eu").is_err()); // vault_us is us only
        assert!(r.check_prefix("lake-us", "warehouse/stats_daily/", "eu").is_ok());
        assert!(r.check_prefix("lake-us", "warehouse/stats_", "eu").is_err()); // names no namespace yet
        assert!(r.check_prefix("lake-us", "ware", "eu").is_err());
        assert!(r.check_prefix("lake-us", "other/", "eu").is_ok());
        assert!(r.check_prefix("lakehouse", "", "").is_ok());
        assert!(r.check_buckets(&["lakehouse".into(), "lake-eu".into()], "us").is_err());
    }

    #[test]
    fn rules_that_do_not_hold_together_are_refused() {
        for bad in [
            r#"[]"#,
            r#"{"region": {}}"#,
            r#"{"regions": {"eu": {"location": "/data"}}}"#,
            r#"{"regions": {"eu": {"location": "s3://lake"}}, "pins": [{"namespaces": ["x"], "region": "mars"}]}"#,
            r#"{"regions": {"eu": {"location": "s3://lake"}, "us": {"location": "s3://lus"}}, "pins": [{"namespaces": ["x"], "region": "eu", "allow": ["us"]}]}"#,
        ] {
            assert!(Residency::parse(bad).is_err(), "{bad}");
        }
        let clash = Residency::parse(
            r#"{"regions": {"eu": {"location": "s3://lake-eu/w"}, "us": {"location": "s3://lake-us/w"}},
                "pins": [{"namespaces": ["crm_eu"], "region": "eu"}, {"namespaces": ["crm_*"], "region": "us"}]}"#,
        )
        .unwrap();
        assert!(clash.check_object("lake-eu", "w/crm_eu/t/x", "eu").is_err());
        assert!(glob(b"stats_*", b"stats_daily") && !glob(b"stats_?", b"stats_xy") && glob(b"a?c", b"abc"));
    }
}
