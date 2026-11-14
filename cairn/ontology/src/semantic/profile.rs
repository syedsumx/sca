// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/semantic/visit_enter.py (`calculate_columns_profiling` and its
// helpers) and auto_ontology/utils/sample_values.py.
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust; the sample comes with the catalog snapshot (Cairn reads it under its own
// governance, masking applied) instead of a `SELECT * … LIMIT 1000` the compiler would run itself.
//! Per-column profiling of a table's sampled values: uniqueness, the most common values, the full value set of
//! a categorical column, the stored date notation, a warning for text columns that mix date shapes, and the key
//! paths of JSON columns.

use std::collections::{BTreeMap, HashMap};

use serde_json::Value;

use super::date_format::{infer_date_format, is_date_type};
use crate::model::{ColumnProfile, Table};

const TOP_N: usize = 5;
pub const MAX_SAMPLE_VALUE_LEN: usize = 30;
pub const MAX_EMBEDDED_JSON_SAMPLE_LEN: usize = 60;
const EXCLUDED_SAMPLE_TYPES: &[&str] = &["date", "time", "timestamp", "datetime", "uuid"];
const LOW_CARDINALITY_MAX: usize = 25;
const TEXT_SAMPLE_TYPES: &[&str] = &["char", "text", "string", "clob", "enum"];
const UNIT_MARKER_KEYS: &[&str] = &["unit", "units", "uom", "currency", "measure"];
const DATE_KEY_HINTS: &[&str] = &["date", "_dt", "day", "time", "moment", "schedule", "_on", "_at"];

fn shapes() -> &'static Vec<(&'static str, regex::Regex)> {
    static S: std::sync::OnceLock<Vec<(&'static str, regex::Regex)>> = std::sync::OnceLock::new();
    S.get_or_init(|| {
        [
            ("YYYY-MM-DD", r"^\d{4}-\d{2}-\d{2}"),
            ("YYYY/MM/DD", r"^\d{4}/\d{1,2}/\d{1,2}$"),
            ("DD/MM/YYYY or MM/DD/YYYY (ambiguous)", r"^\d{1,2}/\d{1,2}/\d{4}$"),
            ("DD/MM/YY or MM/DD/YY (ambiguous)", r"^\d{2}/\d{2}/\d{2}$"),
            ("Mon DD, YYYY", r"^[A-Za-z]{3,9} \d{1,2},? \d{4}$"),
        ]
        .into_iter()
        .map(|(l, p)| (l, regex::Regex::new(p).expect("shape")))
        .collect()
    })
}

pub fn is_excluded_sample_type(t: Option<&str>) -> bool {
    t.is_some_and(|t| {
        let l = t.to_lowercase();
        EXCLUDED_SAMPLE_TYPES.iter().any(|x| l.contains(x))
    })
}

pub fn is_text_sample_type(t: Option<&str>) -> bool {
    t.is_some_and(|t| {
        let l = t.to_lowercase();
        TEXT_SAMPLE_TYPES.iter().any(|x| l.contains(x))
    })
}

/// Distinct recognised date shapes among string values, in first-seen order (non-strings skipped).
pub fn date_shapes_seen(values: &[Value]) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    for v in values {
        let Value::String(s) = v else { continue };
        for (label, re) in shapes() {
            if re.is_match(s) {
                if !out.iter().any(|x| x == label) {
                    out.push(label.to_string());
                }
                break;
            }
        }
    }
    out
}

/// A stable grouping key that keeps `1`, `"1"` and `true` apart and works for arrays and objects.
pub fn sample_key(v: &Value) -> String {
    serde_json::to_string(v).unwrap_or_default()
}

/// Whether a value is short enough to keep as a sample (numbers and booleans always are).
pub fn keep_persisted_sample(v: &Value) -> bool {
    match v {
        Value::Bool(_) | Value::Number(_) => true,
        Value::String(s) => s.chars().count() <= MAX_SAMPLE_VALUE_LEN,
        other => sample_key(other).chars().count() <= MAX_SAMPLE_VALUE_LEN,
    }
}

/// Sample values as display text, values longer than `max_len` dropped (`stringify_sample_values`).
pub fn stringify_samples(values: &[Value], max_len: Option<usize>) -> Vec<String> {
    values
        .iter()
        .filter(|v| !v.is_null())
        .map(|v| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        })
        .filter(|s| max_len.is_none_or(|m| s.chars().count() <= m))
        .collect()
}

/// The `most_common(n)` of a sample, first-seen order breaking ties (as `Counter` does).
fn most_common(values: &[Value], n: usize) -> Vec<Value> {
    let mut counts: Vec<(String, usize, Value)> = vec![];
    let mut index: HashMap<String, usize> = HashMap::new();
    for v in values {
        let k = sample_key(v);
        match index.get(&k) {
            Some(i) => counts[*i].1 += 1,
            None => {
                index.insert(k.clone(), counts.len());
                counts.push((k, 1, v.clone()));
            }
        }
    }
    let mut order: Vec<usize> = (0..counts.len()).collect();
    order.sort_by(|a, b| counts[*b].1.cmp(&counts[*a].1).then(a.cmp(b)));
    order.into_iter().take(n).map(|i| counts[i].2.clone()).collect()
}

fn safe_annotate(base: &str, annotations: &[String]) -> String {
    let mut entry = base.to_string();
    for a in annotations {
        let candidate = format!("{entry} {a}");
        if candidate.chars().count() <= MAX_EMBEDDED_JSON_SAMPLE_LEN {
            entry = candidate;
        } else {
            break;
        }
    }
    entry
}

fn repr(s: &str) -> String {
    format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
}

fn str_of(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// The key paths of a JSON column, annotated with units, date notation and example values where they fit.
fn json_keys(values: &[Value]) -> Vec<String> {
    let mut top_level: Vec<String> = vec![];
    let mut containers: Vec<(String, Vec<String>)> = vec![];
    let mut nested_examples: BTreeMap<(String, String), Vec<String>> = BTreeMap::new();
    let mut top_examples: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for raw in values {
        let Value::Object(map) = raw else { continue };
        for (k, v) in map {
            if let Value::Object(inner) = v {
                let pos = match containers.iter().position(|(c, _)| c == k) {
                    Some(p) => p,
                    None => {
                        containers.push((k.clone(), vec![]));
                        containers.len() - 1
                    }
                };
                for (nk, nv) in inner {
                    if !containers[pos].1.contains(nk) {
                        containers[pos].1.push(nk.clone());
                    }
                    let hint = DATE_KEY_HINTS.iter().any(|h| nk.to_lowercase().contains(h));
                    if nv.is_null() || !hint {
                        continue;
                    }
                    let ex = nested_examples.entry((k.clone(), nk.clone())).or_default();
                    let s = str_of(nv);
                    if ex.len() < 3 && !ex.contains(&s) && s.chars().count() <= MAX_SAMPLE_VALUE_LEN {
                        ex.push(s);
                    }
                }
            } else {
                if !top_level.contains(k) {
                    top_level.push(k.clone());
                }
                if v.is_null() {
                    continue;
                }
                let ex = top_examples.entry(k.clone()).or_default();
                let s = str_of(v);
                if ex.len() < TOP_N && !ex.contains(&s) && s.chars().count() <= MAX_SAMPLE_VALUE_LEN {
                    ex.push(s);
                }
            }
        }
    }
    let mut out = vec![];
    for (k, nested) in &containers {
        let unit = nested.iter().find(|n| UNIT_MARKER_KEYS.contains(&n.to_lowercase().as_str()));
        for nk in nested {
            let base = format!("{k}.{nk}");
            let mut anns = vec![];
            if let Some(u) = unit {
                if u != nk {
                    anns.push(format!("[unit: {k}.{u}]"));
                }
            }
            if let Some(ex) = nested_examples.get(&(k.clone(), nk.clone())) {
                if !ex.is_empty() {
                    let vals: Vec<Value> = ex.iter().map(|s| Value::String(s.clone())).collect();
                    let shapes = date_shapes_seen(&vals);
                    if shapes.len() > 1 {
                        let full = format!("[WARNING mixed date formats observed: {}]", shapes.join(", "));
                        if format!("{base} {full}").chars().count() <= MAX_EMBEDDED_JSON_SAMPLE_LEN {
                            anns.push(full);
                        } else {
                            anns.push("[WARNING mixed date formats observed]".into());
                        }
                    } else if let Some(s) = shapes.first() {
                        anns.push(format!("[date format: {s}]"));
                    } else {
                        anns.push(format!("[e.g. {}]", repr(&ex[0])));
                    }
                }
            }
            out.push(safe_annotate(&base, &anns));
        }
    }
    for k in &top_level {
        let ex = top_examples.get(k).cloned().unwrap_or_default();
        let mut best = k.clone();
        for n in 1..=ex.len() {
            let candidate = format!("{k} [e.g. {}]", ex[..n].iter().map(|e| repr(e)).collect::<Vec<_>>().join(", "));
            if candidate.chars().count() <= MAX_EMBEDDED_JSON_SAMPLE_LEN {
                best = candidate;
            } else {
                break;
            }
        }
        out.push(best);
    }
    out.truncate(20);
    out
}

/// Profiles every column of `table` from its sampled values (`calculate_columns_profiling`).
/// `probe_distinct_values` gates using the column's complete distinct set for categorical text columns.
pub fn profile_table(table: &Table, probe_distinct_values: bool) -> BTreeMap<String, ColumnProfile> {
    let mut out = BTreeMap::new();
    for col in &table.columns {
        let values: Vec<Value> = col.samples.iter().filter(|v| !v.is_null()).cloned().collect();
        if values.is_empty() && col.distinct_values.is_none() && col.is_unique.is_none() {
            continue;
        }
        let keys: Vec<String> = values.iter().map(sample_key).collect();
        let inferred_unique = {
            let set: std::collections::HashSet<&String> = keys.iter().collect();
            !keys.is_empty() && set.len() == keys.len()
        };
        let is_unique = col.is_unique.unwrap_or(inferred_unique);
        let top = most_common(&values, TOP_N);
        let declared = Some(col.data_type.as_str()).filter(|t| !t.is_empty());
        let mut col_values = top.clone();
        if probe_distinct_values && !is_unique && top.len() < TOP_N && is_text_sample_type(declared) {
            if let Some(d) = col.distinct_values.as_ref().filter(|d| d.len() <= LOW_CARDINALITY_MAX) {
                let mut seen: std::collections::HashSet<String> = col_values.iter().map(sample_key).collect();
                for v in d {
                    if seen.insert(sample_key(v)) {
                        col_values.push(v.clone());
                    }
                }
            }
        }
        let format = if is_date_type(declared) || is_text_sample_type(declared) {
            let source: Vec<Value> = if values.is_empty() { col.distinct_values.clone().unwrap_or_default() } else { values.clone() };
            infer_date_format(source.iter())
        } else {
            None
        };
        let mut sample_values = vec![];
        if !is_excluded_sample_type(declared) {
            if declared.is_some_and(|t| t.to_lowercase().contains("json")) {
                sample_values = json_keys(&values).into_iter().map(Value::String).collect();
            } else {
                sample_values = col_values.iter().filter(|v| keep_persisted_sample(v)).cloned().collect();
                if is_text_sample_type(declared) {
                    let s = date_shapes_seen(&values);
                    if s.len() > 1 {
                        sample_values.push(Value::String(format!("[WARNING mixed date formats observed: {}]", s.join(", "))));
                    }
                }
            }
        }
        out.insert(col.name.clone(), ColumnProfile { is_unique: Some(is_unique), format, sample_values, top_values: col_values });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Column;
    use serde_json::json;

    fn col(name: &str, t: &str, samples: Vec<Value>) -> Column {
        Column { name: name.into(), data_type: t.into(), samples, ..Default::default() }
    }

    #[test]
    fn uniqueness_top_values_and_dates() {
        let mut status = col("status", "varchar", vec![json!("paid"); 8]);
        status.samples.push(json!("refunded"));
        status.distinct_values = Some(vec![json!("paid"), json!("refunded"), json!("banned")]);
        let t = Table {
            id: "t".into(),
            name: "orders".into(),
            columns: vec![
                col("id", "bigint", (1..=9).map(|i| json!(i)).collect()),
                status,
                col("order_day", "varchar", vec![json!("2026-01-03"), json!("2026-02-13")]),
                col("mixed", "varchar", vec![json!("2026-01-03"), json!("2026/02/13")]),
                col("created", "timestamp", vec![json!("2026-01-03 10:00:00")]),
                col("note", "varchar", vec![json!("x".repeat(40)), json!("short")]),
            ],
            ..Default::default()
        };
        let p = profile_table(&t, true);
        assert_eq!(p["id"].is_unique, Some(true));
        assert_eq!(p["status"].is_unique, Some(false));
        assert_eq!(p["status"].sample_values, vec![json!("paid"), json!("refunded"), json!("banned")]);
        assert_eq!(p["order_day"].format.as_deref(), Some("YYYY-MM-DD"));
        assert!(p["mixed"].sample_values.last().unwrap().as_str().unwrap().starts_with("[WARNING mixed date formats"));
        assert_eq!(p["created"].format.as_deref(), Some("YYYY-MM-DD HH:MM:SS"));
        assert!(p["created"].sample_values.is_empty(), "dates are not kept as samples");
        assert_eq!(p["note"].sample_values, vec![json!("short")]);
        // Without the distinct probe a column keeps the row sample's values.
        assert_eq!(profile_table(&t, false)["status"].sample_values, vec![json!("paid"), json!("refunded")]);
    }

    #[test]
    fn json_columns_show_their_key_paths() {
        let t = Table {
            id: "t".into(),
            name: "p".into(),
            columns: vec![col(
                "attrs",
                "json",
                vec![json!({"salary": {"value": 4, "unit": "annual"}, "Tx_Adh": "High", "visit": {"visit_date": "2026-01-02"}})],
            )],
            ..Default::default()
        };
        let keys = &profile_table(&t, true)["attrs"].sample_values;
        let k: Vec<&str> = keys.iter().filter_map(|v| v.as_str()).collect();
        assert!(k.contains(&"salary.value [unit: salary.unit]"), "{k:?}");
        assert!(k.contains(&"visit.visit_date [date format: YYYY-MM-DD]"), "{k:?}");
        assert!(k.contains(&"Tx_Adh [e.g. 'High']"), "{k:?}");
    }
}
