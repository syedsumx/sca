//! Data residency (CAIRN_CATALOG_RESIDENCY_FILE, the same file Cairn reads as CAIRN_RESIDENCY_FILE).
//!
//! A namespace pinned to a region keeps its tables under `<region location>/<namespace levels>`, and nowhere
//! else: the catalog refuses any other table location for it (create, register, commit, `write.*.path`
//! properties), a change of the namespace's own `location`, and a clone or rename between namespaces that are
//! not pinned alike (a clone shares its source's files; a rename keeps them). The region locations are reserved
//! for pinned namespaces: an unpinned table may not point into one, so pinned files are never reachable through
//! a table that is not pinned. The rules are deployment configuration, read once at start; a file that does not
//! load stops the catalog.

use std::collections::BTreeMap;

use serde::Deserialize;

/// Table properties that move where an engine writes a table's files.
pub const PATH_PROPERTIES: [&str; 4] = ["write.data.path", "write.metadata.path", "write.object-storage.path", "write.folder-storage.path"];

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
    /// Where the data may be processed and sent: Cairn's concern; the catalog only keeps it in its region.
    #[serde(default)]
    #[allow(dead_code)]
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

#[derive(Default, Debug)]
pub struct Residency {
    regions: BTreeMap<String, String>,
    pins: Vec<(Vec<String>, String)>,
}

/// Where a namespace's tables must live: its region and the base location.
#[derive(Debug, PartialEq)]
pub struct Pin {
    pub region: String,
    pub base: String,
}

fn valid_region(r: &str) -> bool {
    let b = r.as_bytes();
    !b.is_empty()
        && b.len() <= 32
        && b[0].is_ascii_lowercase()
        && b.iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

/// `prefix` itself or anything under it (`s3://b/eu` covers `s3://b/eu/x`, not `s3://b/europe`).
pub fn under(location: &str, prefix: &str) -> bool {
    let l = location.trim_end_matches('/');
    l == prefix || l.starts_with(&format!("{prefix}/"))
}

impl Residency {
    pub fn parse(text: &str) -> Result<Residency, String> {
        // An object with known keys only: a misspelt key would otherwise leave namespaces unpinned.
        let value: serde_json::Value = serde_json::from_str(text).map_err(|e| format!("not JSON: {e}"))?;
        if !value.is_object() {
            return Err("the residency file is a JSON object".into());
        }
        let doc: Doc = serde_json::from_value(value).map_err(|e| format!("not a residency file: {e}"))?;
        let mut regions = BTreeMap::new();
        for (name, r) in doc.regions {
            let loc = r.location.trim_end_matches('/').to_string();
            if !valid_region(&name) || !loc.starts_with("s3://") || loc.len() <= 5 {
                return Err(format!("regions.{name}: a region name and an s3:// location"));
            }
            regions.insert(name, loc);
        }
        let mut pins = Vec::new();
        for (i, p) in doc.pins.into_iter().enumerate() {
            if !regions.contains_key(&p.region) {
                return Err(format!("pins[{i}].region: {} is not one of the regions", p.region));
            }
            if p.namespaces.is_empty() {
                return Err(format!("pins[{i}].namespaces: at least one"));
            }
            pins.push((p.namespaces.into_iter().map(|n| n.to_lowercase()).collect(), p.region));
        }
        Ok(Residency { regions, pins })
    }

    pub fn from_env() -> Result<Residency, String> {
        match std::env::var("CAIRN_CATALOG_RESIDENCY_FILE") {
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

    /// The buckets of the region locations (the store role engines' keys come from must reach them).
    pub fn buckets(&self) -> Vec<String> {
        let mut out: Vec<String> =
            self.regions.values().filter_map(|l| l.strip_prefix("s3://").map(|r| r.split('/').next().unwrap_or("").to_string())).collect();
        out.sort();
        out.dedup();
        out
    }

    /// The pin of a namespace, or None. A namespace two pins put in different regions: an error (refused).
    pub fn pin(&self, levels: &[String]) -> Result<Option<Pin>, String> {
        let name = levels.join(".").to_lowercase();
        let mut found: Option<&String> = None;
        for (patterns, region) in &self.pins {
            if patterns.iter().any(|p| glob_match::glob_match(p, &name)) {
                match found {
                    Some(r) if r != region => return Err(format!("{name} is pinned to more than one region")),
                    _ => found = Some(region),
                }
            }
        }
        Ok(found.map(|r| Pin { region: r.clone(), base: format!("{}/{}", self.regions[r], levels.join("/").to_lowercase()) }))
    }

    /// May a table of the namespace live at `location`? The reason when not.
    pub fn check_location(&self, levels: &[String], location: &str) -> Result<(), String> {
        if !self.active() {
            return Ok(());
        }
        match self.pin(levels)? {
            Some(p) if under(location, &p.base) => Ok(()),
            Some(p) => Err(format!("{} is pinned to region {}: its tables stay under {}", levels.join("."), p.region, p.base)),
            None => match self.regions.iter().find(|(_, l)| under(location, l)) {
                Some((r, _)) => Err(format!("{location} is in region {r}'s location, which only pinned namespaces use")),
                None => Ok(()),
            },
        }
    }

    /// May data move between the two namespaces with its files (a clone, a rename)?
    pub fn check_move(&self, from: &[String], to: &[String]) -> Result<(), String> {
        if !self.active() {
            return Ok(());
        }
        let (a, b) = (self.pin(from)?, self.pin(to)?);
        let region = |p: &Option<Pin>| p.as_ref().map(|p| p.region.clone());
        if region(&a) != region(&b) {
            let name = |p: &Option<Pin>| p.as_ref().map(|p| format!("region {}", p.region)).unwrap_or_else(|| "no region".into());
            return Err(format!(
                "{} ({}) and {} ({}) are not pinned alike: the files would leave their region",
                from.join("."),
                name(&a),
                to.join("."),
                name(&b)
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r() -> Residency {
        Residency::parse(
            r#"{"regions": {"eu": {"location": "s3://lake-eu/warehouse/"}, "us": {"location": "s3://lake-us/w"}},
                "pins": [{"namespaces": ["crm_eu", "hr_*"], "region": "eu"}, {"namespaces": ["sales"], "region": "us"},
                         {"namespaces": ["both"], "region": "eu"}, {"namespaces": ["bo*"], "region": "us"}]}"#,
        )
        .unwrap()
    }

    fn ns(s: &str) -> Vec<String> {
        s.split('.').map(str::to_string).collect()
    }

    #[test]
    fn pinned_namespaces_keep_their_tables_in_their_region() {
        let r = r();
        assert_eq!(r.pin(&ns("hr_payroll")).unwrap().unwrap().base, "s3://lake-eu/warehouse/hr_payroll");
        assert!(r.check_location(&ns("crm_eu"), "s3://lake-eu/warehouse/crm_eu/t-1").is_ok());
        assert!(r.check_location(&ns("crm_eu"), "s3://lake-eu/warehouse/crm_euro/t").is_err());
        assert!(r.check_location(&ns("crm_eu"), "s3://lakehouse/warehouse/crm_eu/t").is_err());
        assert!(r.check_location(&ns("crm_eu"), "s3://lake-eu/warehouse/sales/t").is_err());
        // Unpinned tables may not point into a region's location.
        assert!(r.check_location(&ns("raw"), "s3://lake-eu/warehouse/crm_eu/t-1").is_err());
        assert!(r.check_location(&ns("raw"), "s3://lakehouse/warehouse/raw/t").is_ok());
        assert!(r.pin(&ns("both")).is_err() && r.check_location(&ns("both"), "s3://lake-eu/warehouse/both/t").is_err());
        assert_eq!(r.buckets(), vec!["lake-eu".to_string(), "lake-us".to_string()]);
    }

    #[test]
    fn data_moves_only_between_namespaces_pinned_alike() {
        let r = r();
        assert!(r.check_move(&ns("crm_eu"), &ns("hr_x")).is_ok());
        assert!(r.check_move(&ns("raw"), &ns("other")).is_ok());
        assert!(r.check_move(&ns("crm_eu"), &ns("raw")).is_err());
        assert!(r.check_move(&ns("raw"), &ns("crm_eu")).is_err());
        assert!(r.check_move(&ns("crm_eu"), &ns("sales")).is_err());
        assert!(Residency::default().check_move(&ns("a"), &ns("b")).is_ok());
    }

    #[test]
    fn a_file_that_does_not_hold_together_is_refused() {
        assert!(
            Residency::parse(r#"{"regions": {"eu": {"location": "s3://a"}}, "pins": [{"namespaces": ["x"], "region": "us"}]}"#).is_err()
        );
        assert!(Residency::parse(r#"{"regions": {"EU": {"location": "s3://a"}}}"#).is_err());
        assert!(Residency::parse(r#"{"regions": {"eu": {"location": "/data"}}}"#).is_err());
        assert!(Residency::parse("[]").is_err());
        assert!(Residency::parse(r#"{"regions": {}, "pin": []}"#).is_err());
        assert!(Residency::parse(
            r#"{"regions": {"eu": {"location": "s3://a"}}, "pins": [{"namespaces": ["x"], "region": "eu", "allow": ["eu"]}]}"#
        )
        .is_ok());
    }
}
