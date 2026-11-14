// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/catalog/{normalize.py, table_filter.py, extract.py, diff.py}.
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust for Cairn Ontology; the snapshot arrives as JSON from Cairn instead of DataFrames
// read from a connector.
//! Catalog ingestion: nullability normalisation, table allow/deny filtering, coverage validation and the
//! incremental diff against what is stored.

use std::collections::{BTreeMap, BTreeSet};

use regex::Regex;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

use crate::model::{FilterSpec, Table};

/// Coerce one `is_nullable` value to a boolean (`coerce_nullable`): booleans as they are, the
/// information_schema spellings `YES`/`NO` (and `TRUE`/`T`/`Y`/`1`, `FALSE`/`F`/`N`/`0`); empty is unknown.
/// Anything else is refused rather than guessed: both spellings are truthy as strings, which is how a whole
/// catalog once read as nullable.
pub fn coerce_nullable(v: &Value) -> Result<Option<bool>, String> {
    match v {
        Value::Null => Ok(None),
        Value::Bool(b) => Ok(Some(*b)),
        Value::Number(n) => match n.as_i64() {
            Some(1) => Ok(Some(true)),
            Some(0) => Ok(Some(false)),
            _ => Err(format!("nullable must be a boolean (or 'YES'/'NO'); got {n}")),
        },
        Value::String(s) => {
            let t = s.trim().to_ascii_uppercase();
            match t.as_str() {
                "YES" | "TRUE" | "T" | "Y" | "1" => Ok(Some(true)),
                "NO" | "FALSE" | "F" | "N" | "0" => Ok(Some(false)),
                "" => Ok(None),
                _ => Err(format!("nullable must be a boolean (or 'YES'/'NO'); got {s:?}")),
            }
        }
        other => Err(format!("nullable must be a boolean (or 'YES'/'NO'); got {other}")),
    }
}

pub fn de_nullable<'de, D: Deserializer<'de>>(d: D) -> Result<Option<bool>, D::Error> {
    let v = Value::deserialize(d)?;
    coerce_nullable(&v).map_err(serde::de::Error::custom)
}

/// Regex allow/deny filtering of relations (`table_filter.py`): deny wins, matching is a search against the
/// unqualified name, case-sensitive. A pattern that does not compile is an error, never ignored: ignoring it
/// would ingest everything the operator meant to exclude.
#[derive(Clone, Debug, Default)]
pub struct TableFilter {
    pub allow: Option<Regex>,
    pub deny: Option<Regex>,
}

impl TableFilter {
    pub fn from_spec(spec: Option<&FilterSpec>) -> Result<TableFilter, String> {
        let Some(spec) = spec else { return Ok(TableFilter::default()) };
        Ok(TableFilter {
            allow: compile_pattern(spec.table_allow_regex.as_deref(), "Table allowlist")?,
            deny: compile_pattern(spec.table_deny_regex.as_deref(), "Table denylist")?,
        })
    }

    pub fn active(&self) -> bool {
        self.allow.is_some() || self.deny.is_some()
    }

    pub fn keep(&self, table_name: &str) -> bool {
        if table_name.is_empty() {
            return true;
        }
        if self.deny.as_ref().is_some_and(|d| d.is_match(table_name)) {
            return false;
        }
        if self.allow.as_ref().is_some_and(|a| !a.is_match(table_name)) {
            return false;
        }
        true
    }

    pub fn describe(&self) -> String {
        let mut parts = vec![];
        if let Some(a) = &self.allow {
            parts.push(format!("allow={:?}", a.as_str()));
        }
        if let Some(d) = &self.deny {
            parts.push(format!("deny={:?}", d.as_str()));
        }
        if parts.is_empty() {
            "inactive".into()
        } else {
            parts.join(", ")
        }
    }
}

pub fn compile_pattern(raw: Option<&str>, field: &str) -> Result<Option<Regex>, String> {
    let Some(p) = raw.map(str::trim).filter(|p| !p.is_empty()) else { return Ok(None) };
    Regex::new(p).map(Some).map_err(|e| format!("{field} is not a valid regex: {e}"))
}

/// Fail closed when a relation is listed without a column inventory (`_validate_relation_column_coverage`):
/// a table with zero columns is what a partial extraction looks like, and it would be offered to queries
/// that then fail.
pub fn validate_coverage(tables: &[Table]) -> Result<(), String> {
    let missing: Vec<String> = tables.iter().filter(|t| t.columns.is_empty()).map(|t| t.qualified()).collect();
    if missing.is_empty() {
        return Ok(());
    }
    let preview = missing.iter().take(10).cloned().collect::<Vec<_>>().join(", ");
    let more = if missing.len() > 10 { format!(" (+{} more)", missing.len() - 10) } else { String::new() };
    Err(format!("the snapshot lists {} table(s), but {} had no described columns: {preview}{more}", tables.len(), missing.len()))
}

/// Checks every table has an id and a name, ids are unique, and column names are unique within a table.
pub fn validate_shape(tables: &[Table]) -> Result<(), String> {
    let mut seen = BTreeSet::new();
    for t in tables {
        if t.id.trim().is_empty() || t.name.trim().is_empty() {
            return Err("every table needs an id and a name".into());
        }
        if !seen.insert(t.id.clone()) {
            return Err(format!("table id {:?} appears twice", t.id));
        }
        let mut cols = BTreeSet::new();
        for c in &t.columns {
            if c.name.trim().is_empty() || !cols.insert(c.name.to_lowercase()) {
                return Err(format!("table {}: column names must be present and unique", t.qualified()));
            }
        }
    }
    Ok(())
}

/// What a table's shape is, for the diff: types, nullability, keys and table type (`diff.py` compares
/// `data_type`, `is_nullable`, `table_type` and `pk`). Descriptions are not part of it.
pub fn shape_fingerprint(t: &Table) -> String {
    let cols: Vec<_> = t.columns.iter().map(|c| (c.name.to_lowercase(), c.data_type.to_lowercase(), c.nullable)).collect();
    let fks: Vec<_> = t.foreign_keys.iter().map(|f| (f.columns.clone(), f.target_table.clone(), f.target_columns.clone())).collect();
    let v = serde_json::json!([t.table_type, t.primary_key, cols, fks]);
    sha(&v.to_string())
}

/// Everything a compilation reads from a table: when it is unchanged, an incremental run reuses the
/// table's earlier results instead of asking the model again.
pub fn content_fingerprint(t: &Table) -> String {
    sha(&serde_json::to_string(t).unwrap_or_default())
}

fn sha(s: &str) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(s.as_bytes()))[..32].to_string()
}

/// The difference between the stored catalog and a new snapshot (`update_diff_from_existing_schema`).
#[derive(Clone, Debug, Default, Serialize, PartialEq)]
pub struct Diff {
    pub tables_added: Vec<String>,
    pub tables_removed: Vec<String>,
    /// Tables whose type or keys changed.
    pub tables_updated: Vec<String>,
    /// Tables whose description, samples or profile changed (shape unchanged).
    pub tables_refreshed: Vec<String>,
    pub columns_added: Vec<String>,
    pub columns_removed: Vec<String>,
    /// Columns whose `data_type` or nullability changed.
    pub columns_updated: Vec<String>,
    pub filtered_out: usize,
}

impl Diff {
    pub fn changed(&self) -> bool {
        !(self.tables_added.is_empty()
            && self.tables_removed.is_empty()
            && self.tables_updated.is_empty()
            && self.tables_refreshed.is_empty()
            && self.columns_added.is_empty()
            && self.columns_removed.is_empty()
            && self.columns_updated.is_empty())
    }
}

/// Compare `existing` (by id) with `incoming`. With `replace`, tables missing from `incoming` are removed.
pub fn diff(existing: &BTreeMap<String, Table>, incoming: &[Table], replace: bool) -> Diff {
    let mut d = Diff::default();
    let new_ids: BTreeSet<&str> = incoming.iter().map(|t| t.id.as_str()).collect();
    for t in incoming {
        let Some(old) = existing.get(&t.id) else {
            d.tables_added.push(t.id.clone());
            continue;
        };
        let old_cols: BTreeMap<String, &crate::model::Column> = old.columns.iter().map(|c| (c.name.to_lowercase(), c)).collect();
        let new_cols: BTreeMap<String, &crate::model::Column> = t.columns.iter().map(|c| (c.name.to_lowercase(), c)).collect();
        for (k, c) in &new_cols {
            match old_cols.get(k) {
                None => d.columns_added.push(format!("{}#{}", t.id, c.name)),
                Some(o) => {
                    if o.data_type.to_lowercase() != c.data_type.to_lowercase() || o.nullable != c.nullable {
                        d.columns_updated.push(format!("{}#{}", t.id, c.name));
                    }
                }
            }
        }
        for (k, c) in &old_cols {
            if !new_cols.contains_key(k) {
                d.columns_removed.push(format!("{}#{}", t.id, c.name));
            }
        }
        if old.table_type != t.table_type || old.primary_key != t.primary_key || old.foreign_keys != t.foreign_keys {
            d.tables_updated.push(t.id.clone());
        } else if content_fingerprint(old) != content_fingerprint(t) {
            d.tables_refreshed.push(t.id.clone());
        }
    }
    if replace {
        for id in existing.keys() {
            if !new_ids.contains(id.as_str()) {
                d.tables_removed.push(id.clone());
            }
        }
    }
    d
}

/// Resolves table references (`orders`, `retail.orders`, `lake.retail.orders`, or a table id) to table ids.
#[derive(Clone, Debug, Default)]
pub struct TableIndex {
    by_id: BTreeMap<String, usize>,
    by_name: BTreeMap<String, Vec<usize>>,
    pub tables: Vec<Table>,
}

impl TableIndex {
    pub fn new(tables: Vec<Table>) -> TableIndex {
        let mut idx = TableIndex { tables, ..Default::default() };
        for (i, t) in idx.tables.iter().enumerate() {
            idx.by_id.insert(t.id.clone(), i);
            for key in [t.name.to_lowercase(), t.qualified().to_lowercase(), t.full_name().to_lowercase()] {
                let e = idx.by_name.entry(key).or_default();
                if !e.contains(&i) {
                    e.push(i);
                }
            }
        }
        idx
    }

    pub fn get(&self, id: &str) -> Option<&Table> {
        self.by_id.get(id).map(|i| &self.tables[*i])
    }

    /// The table a SQL reference names, when it names exactly one.
    pub fn resolve(&self, reference: &str) -> Option<&Table> {
        if let Some(t) = self.get(reference) {
            return Some(t);
        }
        let key = reference.to_lowercase();
        if let Some(v) = self.by_name.get(&key) {
            if v.len() == 1 {
                return Some(&self.tables[v[0]]);
            }
        }
        // A longer reference than the catalog knows (catalog.schema.table against schema.table): try its tail.
        let parts: Vec<&str> = key.split('.').collect();
        for start in 1..parts.len() {
            let tail = parts[start..].join(".");
            if let Some(v) = self.by_name.get(&tail) {
                if v.len() == 1 {
                    return Some(&self.tables[v[0]]);
                }
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Column;
    use serde_json::json;

    fn table(id: &str, name: &str, cols: &[(&str, &str)]) -> Table {
        Table {
            id: id.into(),
            schema: "retail".into(),
            name: name.into(),
            columns: cols.iter().map(|(n, t)| Column { name: n.to_string(), data_type: t.to_string(), ..Default::default() }).collect(),
            ..Default::default()
        }
    }

    #[test]
    fn nullable_spellings_are_coerced_and_unknown_ones_refused() {
        assert_eq!(coerce_nullable(&json!("YES")), Ok(Some(true)));
        assert_eq!(coerce_nullable(&json!("no")), Ok(Some(false)));
        assert_eq!(coerce_nullable(&json!(true)), Ok(Some(true)));
        assert_eq!(coerce_nullable(&json!(0)), Ok(Some(false)));
        assert_eq!(coerce_nullable(&json!("")), Ok(None));
        assert_eq!(coerce_nullable(&json!(null)), Ok(None));
        assert!(coerce_nullable(&json!("maybe")).is_err());
        let c: Column = serde_json::from_value(json!({"name": "a", "nullable": "NO"})).unwrap();
        assert_eq!(c.nullable, Some(false));
        assert!(serde_json::from_value::<Column>(json!({"name": "a", "nullable": "perhaps"})).is_err());
    }

    #[test]
    fn deny_wins_and_allow_narrows() {
        let f = TableFilter::from_spec(Some(&FilterSpec {
            table_allow_regex: Some("^sales_".into()),
            table_deny_regex: Some("_stage".into()),
        }))
        .unwrap();
        assert!(f.keep("sales_orders"));
        assert!(!f.keep("sales_orders_stage"));
        assert!(!f.keep("hr_pay"));
        assert!(f.keep(""));
        assert!(TableFilter::from_spec(Some(&FilterSpec { table_allow_regex: Some("(".into()), table_deny_regex: None }))
            .unwrap_err()
            .contains("Table allowlist is not a valid regex"));
        let blank = TableFilter::from_spec(Some(&FilterSpec { table_allow_regex: Some("  ".into()), table_deny_regex: None }));
        assert!(!blank.unwrap().active());
        // Case-sensitive, as the engines treat identifiers.
        let d = TableFilter::from_spec(Some(&FilterSpec { table_allow_regex: None, table_deny_regex: Some("Tmp".into()) })).unwrap();
        assert!(d.keep("tmp_x") && !d.keep("Tmp_x"));
    }

    #[test]
    fn a_table_without_columns_fails_closed() {
        let t = vec![table("a", "orders", &[("id", "int")]), table("b", "ghost", &[])];
        assert!(validate_coverage(&t).unwrap_err().contains("retail.ghost"));
        assert!(validate_coverage(&t[..1]).is_ok());
    }

    #[test]
    fn the_diff_sees_added_removed_and_changed_tables_and_columns() {
        let mut old = BTreeMap::new();
        old.insert("a".into(), table("a", "orders", &[("id", "int"), ("amount", "int"), ("gone", "text")]));
        old.insert("b".into(), table("b", "stale", &[("x", "int")]));
        old.insert("c".into(), table("c", "same", &[("x", "int")]));
        let mut c2 = table("c", "same", &[("x", "int")]);
        c2.description = Some("now documented".into());
        let mut a2 = table("a", "orders", &[("id", "int"), ("amount", "decimal(10,2)"), ("new_col", "text")]);
        a2.primary_key = vec!["id".into()];
        let incoming = vec![a2, c2, table("d", "fresh", &[("y", "int")])];
        let d = diff(&old, &incoming, true);
        assert_eq!(d.tables_added, vec!["d"]);
        assert_eq!(d.tables_removed, vec!["b"]);
        assert_eq!(d.tables_updated, vec!["a"]);
        assert_eq!(d.tables_refreshed, vec!["c"]);
        assert_eq!(d.columns_added, vec!["a#new_col"]);
        assert_eq!(d.columns_removed, vec!["a#gone"]);
        assert_eq!(d.columns_updated, vec!["a#amount"]);
        // Merge mode never removes.
        assert!(diff(&old, &incoming, false).tables_removed.is_empty());
        // The same snapshot again is no change at all.
        let same: Vec<Table> = old.values().cloned().collect();
        assert!(!diff(&old, &same, true).changed());
    }

    #[test]
    fn references_resolve_by_any_qualification() {
        let mut t = table("urn:orders", "orders", &[("id", "int")]);
        t.catalog = "lake".into();
        let idx = TableIndex::new(vec![t, table("urn:c", "customers", &[("id", "int")])]);
        assert_eq!(idx.resolve("orders").unwrap().id, "urn:orders");
        assert_eq!(idx.resolve("RETAIL.ORDERS").unwrap().id, "urn:orders");
        assert_eq!(idx.resolve("lake.retail.orders").unwrap().id, "urn:orders");
        assert_eq!(idx.resolve("other.retail.customers").unwrap().id, "urn:c");
        assert!(idx.resolve("nope").is_none());
    }
}
