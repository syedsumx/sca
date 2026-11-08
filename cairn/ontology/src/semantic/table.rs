// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/semantic/deterministic.py, fk_suggester.py and
// term_extractor.py (and their tests).
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust; model calls go through `crate::llm`.
//! One table's semantic steps: which columns look like foreign keys (and whether the table is a junction),
//! the non-FK columns as ColumnAttribute candidates with business descriptions, and the business Term(s)
//! with display names and synonyms.

use std::collections::{BTreeMap, BTreeSet};

use regex::Regex;
use serde_json::Value;

use super::date_format::is_date_type;
use super::profile::stringify_samples;
use crate::llm::Llm;
use crate::model::{Column, ColumnProfile, Table};
use crate::prompts;

const DESCRIPTION_BATCH_SIZE: usize = 15;
const FORMAT_MARKER: &str = "format:";

/// A column candidate for Term assignment; `display_name` is set by term extraction.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Spec {
    pub source_column: String,
    pub name: String,
    pub display_name: String,
    pub datatype: String,
    pub description: Option<String>,
}

/// A declared FK as the per-table steps see it (`ctx["fks"]`).
#[derive(Clone, Debug, PartialEq)]
pub struct DeclaredFk {
    pub source_column: String,
    pub target_table: String,
    pub target_column: String,
}

/// A human-readable fallback Term name from a physical table name (`to_term_name`):
/// `purchase_orders` → `Purchase Orders`, `XMLHttpRequest` → `XML Http Request`.
pub fn to_term_name(table_name: &str) -> String {
    let chars: Vec<char> = table_name.trim().chars().collect();
    let mut s = String::new();
    for i in 0..chars.len() {
        let c = chars[i];
        if i > 0 {
            let p = chars[i - 1];
            let next_lower = chars.get(i + 1).is_some_and(|n| n.is_lowercase());
            if (p.is_uppercase() && c.is_uppercase() && next_lower) || ((p.is_lowercase() || p.is_ascii_digit()) && c.is_uppercase()) {
                s.push(' ');
            }
        }
        s.push(c);
    }
    s.split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|p| !p.is_empty())
        .map(|p| if is_upper(p) { p.to_string() } else { capitalize(p) })
        .collect::<Vec<_>>()
        .join(" ")
}

fn is_upper(s: &str) -> bool {
    s.chars().any(|c| c.is_alphabetic()) && !s.chars().any(|c| c.is_lowercase())
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + &c.as_str().to_lowercase(),
        None => String::new(),
    }
}

/// `order_date` → `orderDate` (`_column_to_attr_name`).
pub fn column_to_attr_name(column: &str) -> String {
    let parts: Vec<&str> = column.trim().split(|c: char| c == '_' || c.is_whitespace()).filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return column.to_string();
    }
    let mut out = parts[0].to_lowercase();
    for p in &parts[1..] {
        out.push_str(&capitalize(p));
    }
    out
}

pub fn fk_source_columns(fks: &[DeclaredFk]) -> BTreeSet<String> {
    fks.iter().filter(|f| !f.source_column.is_empty()).map(|f| f.source_column.clone()).collect()
}

fn column_samples(col: &Column, profiles: &BTreeMap<String, ColumnProfile>) -> Vec<String> {
    if is_date_type(Some(&col.data_type)) {
        return vec![];
    }
    profiles.get(&col.name).map(|p| stringify_samples(&p.sample_values, None)).unwrap_or_default()
}

fn add_samples_suffix(text: &str, samples: &[String]) -> String {
    if samples.is_empty() {
        text.to_string()
    } else {
        format!("{text} — samples: {}", samples.join(", "))
    }
}

fn enrich(col: &Column, profiles: &BTreeMap<String, ColumnProfile>, text: Option<&str>) -> Option<String> {
    let text = text.unwrap_or("");
    let clause = profiles.get(&col.name).and_then(|p| p.format.clone()).map(|f| format!("{FORMAT_MARKER} {f}"));
    let out = match clause {
        Some(c) if !text.contains(FORMAT_MARKER) => {
            if text.is_empty() {
                c
            } else {
                format!("{text} — {c}")
            }
        }
        _ => text.to_string(),
    };
    Some(out).filter(|s| !s.is_empty())
}

async fn describe_batch(llm: &Llm, columns: &[&Column], profiles: &BTreeMap<String, ColumnProfile>) -> BTreeMap<String, String> {
    if !llm.chat_configured() {
        return BTreeMap::new();
    }
    let lines: Vec<String> = columns
        .iter()
        .map(|c| {
            let dt = if c.data_type.is_empty() { "unknown" } else { &c.data_type };
            add_samples_suffix(&format!("  - {} ({dt})", c.name), &column_samples(c, profiles))
        })
        .collect();
    let prompt = format!("Columns:\n{}", lines.join("\n"));
    let Some(result) = llm
        .structured::<prompts::ColumnDescriptionResult>(
            "column_descriptions",
            prompts::DESCRIPTION_SYSTEM,
            &prompt,
            prompts::column_description_schema(),
            Some(0.0),
            None,
        )
        .await
    else {
        tracing::warn!("column description batch failed — proceeding without them");
        return BTreeMap::new();
    };
    // A model sometimes echoes the annotation ("Academic Year (TEXT)"): exact name first, then the longest
    // requested name the answer starts with.
    let requested: Vec<&str> = columns.iter().map(|c| c.name.as_str()).collect();
    let mut out = BTreeMap::new();
    for d in result.descriptions {
        let desc = d.description.trim().to_string();
        if desc.is_empty() {
            continue;
        }
        if requested.contains(&d.column_name.as_str()) {
            out.entry(d.column_name.clone()).or_insert(desc);
            continue;
        }
        if let Some(best) = requested.iter().filter(|n| !n.is_empty() && d.column_name.starts_with(**n)).max_by_key(|n| n.len()) {
            out.entry(best.to_string()).or_insert(desc);
        }
    }
    out
}

/// Non-FK columns mapped 1:1 to ColumnAttribute candidates (`column_attribute_specs`). Columns without a
/// description get one from the model (in batches of 15); existing descriptions get the sample values
/// appended; date columns get their stored notation.
pub async fn column_attribute_specs(
    llm: &Llm,
    columns: &[Column],
    fks: &[DeclaredFk],
    suggested: &BTreeSet<String>,
    profiles: &BTreeMap<String, ColumnProfile>,
) -> Vec<Spec> {
    let mut fk_cols = fk_source_columns(fks);
    fk_cols.extend(suggested.iter().cloned());
    let candidates: Vec<&Column> = columns.iter().filter(|c| !c.name.is_empty() && !fk_cols.contains(&c.name)).collect();
    let with: Vec<&Column> = candidates.iter().copied().filter(|c| c.description.as_deref().is_some_and(|d| !d.is_empty())).collect();
    let without: Vec<&Column> = candidates.iter().copied().filter(|c| c.description.as_deref().is_none_or(str::is_empty)).collect();
    let mut generated = BTreeMap::new();
    for batch in without.chunks(DESCRIPTION_BATCH_SIZE) {
        generated.extend(describe_batch(llm, batch, profiles).await);
    }
    let spec = |c: &Column, d: Option<String>| Spec {
        source_column: c.name.clone(),
        name: column_to_attr_name(&c.name),
        display_name: String::new(),
        datatype: c.data_type.clone(),
        description: d,
    };
    let mut out: Vec<Spec> = with
        .iter()
        .map(|c| {
            let text = add_samples_suffix(c.description.as_deref().unwrap_or(""), &column_samples(c, profiles));
            spec(c, enrich(c, profiles, Some(&text)))
        })
        .collect();
    out.extend(without.iter().map(|c| spec(c, enrich(c, profiles, generated.get(&c.name).map(String::as_str)))));
    out
}

// ------------------------------------------------------------------------------------- FK suggestion

#[derive(Clone, Debug, Default, PartialEq)]
pub struct FkSuggestions {
    pub suggestions: Vec<(String, String)>,
    pub is_junction_table: bool,
    pub junction_table_rationale: String,
}

fn format_column_line(col: &Column, samples: &[Value], is_unique: Option<bool>) -> String {
    let mut suffix = col.description.as_deref().filter(|d| !d.is_empty()).map(|d| format!(" — {d}")).unwrap_or_default();
    let s = stringify_samples(samples, Some(30));
    if !s.is_empty() {
        suffix.push_str(&format!(" [samples: {}]", s.join(", ")));
    }
    if let Some(u) = is_unique {
        suffix.push_str(&format!(" [is_unique: {u}]"));
    }
    format!("  - {} ({}){suffix}", col.name, col.data_type)
}

/// Which non-PK, non-declared-FK columns may be foreign keys, and whether the table is a junction
/// (`suggest_potential_foreign_keys`). `fk_targets` are this table's columns other tables' declared FKs
/// reference (they identify rows here, so they are not candidates).
pub async fn suggest_foreign_keys(
    llm: &Llm,
    table: &Table,
    fks: &[DeclaredFk],
    fk_targets: &BTreeSet<String>,
    profiles: &BTreeMap<String, ColumnProfile>,
) -> FkSuggestions {
    let pk: BTreeSet<String> = table.primary_key.iter().cloned().collect();
    let known = fk_source_columns(fks);
    let candidates: Vec<&Column> =
        table.columns.iter().filter(|c| !pk.contains(&c.name) && !known.contains(&c.name) && !fk_targets.contains(&c.name)).collect();
    if candidates.is_empty() && table.columns.len() <= 2 {
        return FkSuggestions::default();
    }
    let header = table.qualified();
    let join = |s: &BTreeSet<String>| if s.is_empty() { "(none)".to_string() } else { s.iter().cloned().collect::<Vec<_>>().join(", ") };
    let unique = |c: &Column| profiles.get(&c.name).and_then(|p| p.is_unique);
    let samples = |c: &Column| profiles.get(&c.name).map(|p| p.sample_values.clone()).unwrap_or_default();
    let all_lines: Vec<String> = table.columns.iter().map(|c| format_column_line(c, &samples(c), unique(c))).collect();
    let cand_lines: Vec<String> = candidates.iter().map(|c| format_column_line(c, &samples(c), unique(c))).collect();
    let fk_lines: Vec<String> = fks.iter().map(|f| format!("  - {} -> {}", f.source_column, f.target_table)).collect();
    let prompt = format!(
        "Table: {header}\nDescription: {}\nPrimary key columns (exclude from suggestions): {}\n\
Known foreign key columns (exclude from suggestions): {}\nKnown foreign key roles:\n{}\nAll table columns:\n{}\n\
Candidate columns:\n{}\n",
        table.description.as_deref().unwrap_or(""),
        join(&pk),
        join(&known),
        if fk_lines.is_empty() { "  (none)".to_string() } else { fk_lines.join("\n") },
        all_lines.join("\n"),
        if cand_lines.is_empty() { "  (none)".to_string() } else { cand_lines.join("\n") },
    );
    let Some(result) = llm
        .structured::<prompts::FkAndPkResult>("fk_suggestions", prompts::FK_SYSTEM, &prompt, prompts::fk_schema(), Some(0.0), None)
        .await
    else {
        return FkSuggestions::default();
    };
    let llm_pk: BTreeSet<String> = result
        .pk_column_names
        .iter()
        .map(|n| n.trim().to_string())
        .filter(|n| !n.is_empty() && profiles.get(n).and_then(|p| p.is_unique) != Some(false))
        .collect();
    let allowed: BTreeSet<&str> = candidates.iter().map(|c| c.name.as_str()).collect();
    let mut seen = BTreeSet::new();
    let mut out = vec![];
    for s in &result.fk_suggestions {
        let n = s.column_name.trim().to_string();
        if n.is_empty() || !allowed.contains(n.as_str()) || !seen.insert(n.clone()) {
            continue;
        }
        out.push((n, s.rationale.trim().to_string()));
    }
    // A non-unique uuid column that is not the table's key almost certainly references another entity.
    for c in &candidates {
        if seen.contains(&c.name) {
            continue;
        }
        if c.data_type.eq_ignore_ascii_case("uuid") && !llm_pk.contains(&c.name) && unique(c) == Some(false) {
            seen.insert(c.name.clone());
            out.push((
                c.name.clone(),
                "uuid type, not a declared or inferred primary key — almost certainly references another entity".into(),
            ));
        }
    }
    FkSuggestions {
        suggestions: out,
        is_junction_table: result.is_junction_table,
        junction_table_rationale: result.junction_table_rationale.trim().to_string(),
    }
}

// ----------------------------------------------------------------------------------- term extraction

#[derive(Clone, Debug, PartialEq, Default)]
pub struct TermProposal {
    pub name: String,
    pub description: String,
    pub synonyms: Vec<String>,
    /// (source column, display name)
    pub attributes: Vec<(String, String)>,
}

const INVALID: &[&str] = &["unnamed", "unknown", "none", ""];

fn label_re() -> &'static Regex {
    static R: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    R.get_or_init(|| Regex::new(r"^[A-Z][A-Za-z0-9]*(?: [A-Za-z][A-Za-z0-9]*)*$").expect("label"))
}

fn normalize_label(name: &str, fallback: &str) -> String {
    let c = name.trim();
    if INVALID.contains(&c.to_lowercase().as_str()) || !label_re().is_match(c) {
        return fallback.to_string();
    }
    to_term_name(c)
}

/// Writes the chosen display labels back onto the specs (`apply_display_names_to_specs`).
pub fn apply_display_names(terms: &[TermProposal], specs: &mut [Spec]) {
    for t in terms {
        for (col, label) in &t.attributes {
            if let Some(s) = specs.iter_mut().find(|s| &s.source_column == col) {
                s.display_name = label.clone();
            }
        }
    }
    for s in specs.iter_mut() {
        if s.display_name.is_empty() {
            s.display_name = s.name.clone();
        }
    }
}

fn fallback_terms(table: &Table, specs: &[Spec]) -> Vec<TermProposal> {
    vec![TermProposal {
        name: to_term_name(&table.name),
        description: format!("Business entity represented by table {}", table.name),
        synonyms: vec![],
        attributes: specs.iter().map(|s| (s.source_column.clone(), s.name.clone())).collect(),
    }]
}

/// Keeps only proposals that name real candidate columns, each column once; unassigned columns go to the
/// first Term (`_sanitize_result`).
pub fn sanitize(raw: prompts::RawTableTermsResult, table: &Table, specs: &[Spec]) -> Vec<TermProposal> {
    let by_col: BTreeMap<&str, &Spec> = specs.iter().map(|s| (s.source_column.as_str(), s)).collect();
    let default_term = to_term_name(&table.name);
    let mut out: Vec<TermProposal> = vec![];
    let mut seen_names = BTreeSet::new();
    let mut assigned = BTreeSet::new();
    for t in raw.terms {
        let name = normalize_label(&t.name, &default_term);
        if !seen_names.insert(name.clone()) {
            continue;
        }
        let mut attrs = vec![];
        for a in t.attributes {
            let col = a.source_column.trim().to_string();
            let Some(spec) = by_col.get(col.as_str()) else { continue };
            if col.is_empty() || assigned.contains(&col) {
                continue;
            }
            attrs.push((col.clone(), normalize_label(&a.display_name, &spec.name)));
            assigned.insert(col);
        }
        out.push(TermProposal { name, description: t.description.trim().to_string(), synonyms: vec![], attributes: attrs });
    }
    if out.is_empty() {
        return fallback_terms(table, specs);
    }
    for s in specs {
        if !assigned.contains(&s.source_column) {
            out[0].attributes.push((s.source_column.clone(), s.name.clone()));
            assigned.insert(s.source_column.clone());
        }
    }
    out
}

/// Alternate names written in a table description (`_extract_synonyms`).
async fn extract_synonyms(llm: &Llm, description: &str) -> Vec<String> {
    if description.is_empty() {
        return vec![];
    }
    llm.structured::<prompts::SynonymExtraction>(
        "synonyms",
        prompts::SYNONYM_SYSTEM,
        &format!("Description: {description}"),
        prompts::synonyms_schema(),
        Some(0.0),
        Some(1024),
    )
    .await
    .map(|r| r.synonyms.into_iter().map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect())
    .unwrap_or_default()
}

/// The domain context given to Term extraction (`DomainSummary`).
#[derive(Clone, Debug, Default, serde::Deserialize, serde::Serialize)]
pub struct DomainSummary {
    #[serde(default)]
    pub domains: Vec<String>,
    #[serde(default)]
    pub core_entities: Vec<String>,
    #[serde(default)]
    pub business_rules: Vec<String>,
}

/// One or more Terms for the table, each column assigned with a display name (`extract_term`).
pub async fn extract_terms(llm: &Llm, table: &Table, specs: &[Spec], domain: Option<&DomainSummary>) -> Vec<TermProposal> {
    if specs.is_empty() {
        return fallback_terms(table, specs);
    }
    let description = table.description.clone().unwrap_or_default();
    let lines: Vec<String> = specs
        .iter()
        .take(40)
        .map(|s| {
            let d = s.description.as_deref().map(|d| format!(" — {d}")).unwrap_or_default();
            format!("  - source_column={} ({}){d}", s.source_column, s.datatype)
        })
        .collect();
    let domain_block = domain
        .map(|d| {
            format!(
                "\nDomains: {}\nCore entities: {}\n",
                d.domains.iter().take(8).cloned().collect::<Vec<_>>().join(", "),
                d.core_entities.iter().take(12).cloned().collect::<Vec<_>>().join(", ")
            )
        })
        .unwrap_or_default();
    let prompt = format!(
        "Table: {}\nDescription: {description}\nCandidate columns (assign each to exactly one Term with a display_name):\n{}\n{domain_block}",
        table.name,
        lines.join("\n")
    );
    let Some(raw) = llm
        .structured::<prompts::RawTableTermsResult>("terms", prompts::TERM_SYSTEM, &prompt, prompts::terms_schema(), Some(0.0), Some(4096))
        .await
    else {
        return fallback_terms(table, specs);
    };
    let mut terms = sanitize(raw, table, specs);
    let synonyms = extract_synonyms(llm, &description).await;
    if !synonyms.is_empty() {
        for t in &mut terms {
            t.synonyms = synonyms.clone();
        }
    }
    terms
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prompts::{RawTableTermsResult, RawTermProposal, TermColumnRef};

    #[test]
    fn term_names() {
        assert_eq!(to_term_name("purchase_orders"), "Purchase Orders");
        assert_eq!(to_term_name("delta_lite_event"), "Delta Lite Event");
        assert_eq!(to_term_name("gtl_ui_event"), "Gtl Ui Event");
        assert_eq!(to_term_name("GtlImagingEvent"), "Gtl Imaging Event");
        assert_eq!(to_term_name("XMLHttpRequest"), "XML Http Request");
        assert_eq!(column_to_attr_name("order_date"), "orderDate");
        assert_eq!(column_to_attr_name("Total amount"), "totalAmount");
    }

    fn spec(c: &str) -> Spec {
        Spec { source_column: c.into(), name: column_to_attr_name(c), datatype: "int".into(), ..Default::default() }
    }

    #[test]
    fn sanitizing_keeps_real_columns_once_and_assigns_the_rest_to_the_first_term() {
        let table = Table { id: "t".into(), name: "orders_hist".into(), ..Default::default() };
        let specs = vec![spec("amount"), spec("status"), spec("placed_on")];
        let raw = RawTableTermsResult {
            terms: vec![
                RawTermProposal {
                    name: "purchase_order".into(),
                    description: " Orders ".into(),
                    attributes: vec![
                        TermColumnRef { source_column: "amount".into(), display_name: "Total Amount".into() },
                        TermColumnRef { source_column: "ghost".into(), display_name: "Ghost".into() },
                        TermColumnRef { source_column: "amount".into(), display_name: "Again".into() },
                    ],
                },
                RawTermProposal {
                    name: "Order Status".into(),
                    description: String::new(),
                    attributes: vec![TermColumnRef { source_column: "status".into(), display_name: "bad_label".into() }],
                },
            ],
        };
        let terms = sanitize(raw, &table, &specs);
        assert_eq!(terms[0].name, "Orders Hist", "an invalid label falls back to the table's name");
        assert_eq!(terms[0].description, "Orders");
        assert_eq!(terms[0].attributes, vec![("amount".into(), "Total Amount".into()), ("placed_on".into(), "placedOn".into())]);
        assert_eq!(terms[1].attributes, vec![("status".into(), "status".into())]);
        let mut s = specs.clone();
        apply_display_names(&terms, &mut s);
        assert_eq!(s[0].display_name, "Total Amount");
        assert_eq!(s[1].display_name, "status");
        assert!(sanitize(RawTableTermsResult { terms: vec![] }, &table, &specs)[0].description.contains("orders_hist"));
    }

    #[test]
    fn descriptions_carry_samples_and_the_date_notation() {
        let mut profiles = BTreeMap::new();
        profiles.insert("game_date".to_string(), ColumnProfile { format: Some("YYMMDD".into()), ..Default::default() });
        let col = Column { name: "game_date".into(), data_type: "text".into(), ..Default::default() };
        assert_eq!(
            enrich(&col, &profiles, Some("Date the match was played.")).as_deref(),
            Some("Date the match was played. — format: YYMMDD")
        );
        assert_eq!(enrich(&col, &profiles, None).as_deref(), Some("format: YYMMDD"));
        assert_eq!(add_samples_suffix("x", &["a".into(), "b".into()]), "x — samples: a, b");
    }
}
