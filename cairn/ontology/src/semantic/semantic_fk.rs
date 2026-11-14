// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/semantic/semantic_fk.py (and its tests).
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust. Candidates come from the elements' own embeddings plus name and type heuristics
// (so resolution still works without an embeddings endpoint); value checks use the sampled and distinct values
// in the snapshot instead of probing the warehouse; joins observed in the query log become relationships too.
//! Relationships (join paths) between tables:
//!
//! 1. **Declared** foreign keys from the catalog, as they are.
//! 2. **Inferred** for columns the per-table step suspected are foreign keys: candidate key columns on other
//!    tables (most similar by embedding to the column's name, description and samples, and by name), of a
//!    compatible type and unique; the model picks the referenced one, or abstains; when it abstains, a candidate
//!    named like the column whose known values contain every sampled value is taken.
//! 3. **Pruned** when the column shares no value with its target (or is empty).
//! 4. **Observed** equality joins in the query log, merged into the above or added.
//! 5. **Join defaults**: one relationship per (table, target column) when several roles reference it.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

use super::embed::attribute_text;
use super::profile::stringify_samples;
use crate::catalog::TableIndex;
use crate::llm::{cosine, Llm};
use crate::model::{stable_id, Attribute, JoinEvidence, Provenance, Relationship, Table, TableResult};
use crate::prompts;

pub struct Context<'a> {
    pub llm: &'a Llm,
    pub tables: &'a TableIndex,
    pub results: &'a BTreeMap<String, TableResult>,
    pub run_id: &'a str,
}

fn family(t: &str) -> &'static str {
    let l = t.to_lowercase();
    if ["int", "decimal", "numeric", "double", "real", "float", "number"].iter().any(|x| l.contains(x)) {
        "number"
    } else if l.contains("uuid") {
        "uuid"
    } else if ["char", "text", "string", "clob"].iter().any(|x| l.contains(x)) {
        "text"
    } else if ["date", "time"].iter().any(|x| l.contains(x)) {
        "date"
    } else if l.contains("bool") {
        "bool"
    } else {
        "other"
    }
}

fn compatible(a: &str, b: &str) -> bool {
    let (x, y) = (family(a), family(b));
    x == y || x == "other" || y == "other" || (x == "uuid" && y == "text") || (x == "text" && y == "uuid")
}

impl Context<'_> {
    fn attributes(&self) -> impl Iterator<Item = &Attribute> {
        self.results.values().flat_map(|r| r.attributes.iter())
    }

    pub fn is_unique(&self, table: &Table, column: &str) -> bool {
        table.is_pk(column)
            || table.column(column).and_then(|c| c.is_unique) == Some(true)
            || self.results.get(&table.id).and_then(|r| r.facts.profiles.get(column)).and_then(|p| p.is_unique) == Some(true)
    }

    fn samples(&self, table: &Table, column: &str) -> Vec<Value> {
        let c = table.column(column);
        let mut v: Vec<Value> = c.map(|c| c.samples.clone()).unwrap_or_default();
        if v.is_empty() {
            v = self.results.get(&table.id).and_then(|r| r.facts.profiles.get(column)).map(|p| p.sample_values.clone()).unwrap_or_default();
        }
        v.into_iter().filter(|x| !x.is_null()).collect()
    }

    /// Every value the column holds, when that is known: its distinct set, or a sample that covers each row.
    fn complete_values(&self, table: &Table, column: &str) -> Option<BTreeSet<String>> {
        let c = table.column(column)?;
        if let Some(d) = &c.distinct_values {
            return Some(stringify_samples(d, None).into_iter().collect());
        }
        let rows = table.row_count?;
        let nulls = c.null_count.unwrap_or(0);
        let non_null: Vec<&Value> = c.samples.iter().filter(|v| !v.is_null()).collect();
        if rows > 0 && non_null.len() as i64 >= rows - nulls {
            return Some(non_null.iter().map(|v| stringify_samples(&[(*v).clone()], None).join("")).collect());
        }
        None
    }

    fn all_null(&self, table: &Table, column: &str) -> bool {
        let Some(c) = table.column(column) else { return false };
        matches!((c.null_count, table.row_count), (Some(n), Some(r)) if r > 0 && n >= r)
    }
}

fn rel_id(st: &str, sc: &str, tt: &str, tc: &str) -> String {
    stable_id("rel", &[st, &sc.to_lowercase(), tt, &tc.to_lowercase()])
}

fn prov(source: &str, detail: String, confidence: f64, run_id: &str) -> Provenance {
    Provenance { source: source.into(), detail, confidence, run_id: run_id.into() }
}

/// Declared foreign keys (step 1). A target column not named is the target's single-column primary key.
pub fn declared(ctx: &Context) -> Vec<Relationship> {
    let mut out = vec![];
    for t in &ctx.tables.tables {
        for fk in &t.foreign_keys {
            let Some(target) = ctx.tables.resolve(&fk.target_table) else {
                tracing::debug!(table = %t.qualified(), target = %fk.target_table, "declared FK to a table not in the catalog");
                continue;
            };
            for (i, col) in fk.columns.iter().enumerate() {
                let Some(tc) = fk.target_columns.get(i).cloned().or_else(|| {
                    if target.primary_key.len() == fk.columns.len() {
                        target.primary_key.get(i).cloned()
                    } else {
                        None
                    }
                }) else {
                    continue;
                };
                out.push(Relationship {
                    id: rel_id(&t.id, col, &target.id, &tc),
                    source_table: t.id.clone(),
                    source_column: col.clone(),
                    target_table: target.id.clone(),
                    target_column: tc.clone(),
                    cardinality: "many_to_one".into(),
                    confidence: 1.0,
                    join_default: true,
                    provenance: vec![prov(
                        "catalog_fk",
                        format!("declared foreign key {}.{col} → {}.{tc}", t.qualified(), target.qualified()),
                        1.0,
                        ctx.run_id,
                    )],
                    evidence: JoinEvidence::default(),
                    rationale: String::new(),
                });
            }
        }
    }
    out
}

fn singular(s: &str) -> String {
    let l = s.to_lowercase();
    if let Some(x) = l.strip_suffix("ies") {
        format!("{x}y")
    } else if let Some(x) = l.strip_suffix("ses") {
        format!("{x}s")
    } else if let Some(x) = l.strip_suffix('s') {
        x.to_string()
    } else {
        l
    }
}

fn stem(column: &str) -> Option<String> {
    let l = column.to_lowercase();
    for suffix in ["_id", "id", "_key", "_ref", "_fk", "_code", "_no"] {
        if let Some(s) = l.strip_suffix(suffix) {
            let s = s.trim_end_matches('_');
            if !s.is_empty() {
                return Some(s.to_string());
            }
        }
    }
    None
}

#[derive(Clone)]
struct Hit<'a> {
    attr: &'a Attribute,
    distance: f64,
}

/// Name heuristics: `customer_id` → a unique key of `customers`/`customer`; or the same column name, unique, on
/// another table.
fn heuristic_hits<'a>(ctx: &'a Context, table: &Table, column: &str) -> Vec<Hit<'a>> {
    let st = stem(column);
    let mut out = vec![];
    for a in ctx.attributes() {
        if a.table_id == table.id {
            continue;
        }
        let Some(target) = ctx.tables.get(&a.table_id) else { continue };
        let by_stem = st.as_ref().is_some_and(|s| {
            let tn = singular(&target.name);
            (tn == *s || target.name.to_lowercase() == *s || tn.ends_with(&format!("_{s}")))
                && (a.column.eq_ignore_ascii_case("id") || a.column.eq_ignore_ascii_case(column) || target.is_pk(&a.column))
        });
        let same_name = a.column.eq_ignore_ascii_case(column);
        if by_stem || same_name {
            out.push(Hit { attr: a, distance: if by_stem { 0.5 } else { 0.6 } });
        }
    }
    out
}

fn query_text(ctx: &Context, table: &Table, column: &str) -> String {
    let c = table.column(column);
    let mut parts = vec![format!("column_name: {column}")];
    if let Some(d) = c.and_then(|c| c.description.as_deref()).filter(|d| !d.trim().is_empty()) {
        parts.push(format!("description: {}", d.trim()));
    }
    let s = stringify_samples(&ctx.samples(table, column), Some(30));
    if !s.is_empty() {
        parts.push(format!("sample_values: {}", s.join(", ")));
    }
    parts.join(", ")
}

async fn candidates<'a>(ctx: &'a Context<'a>, table: &Table, column: &str) -> Vec<Hit<'a>> {
    let col_type = table.column(column).map(|c| c.data_type.clone()).unwrap_or_default();
    let eligible = |a: &Attribute| {
        a.table_id != table.id
            && ctx.tables.get(&a.table_id).is_some_and(|t| ctx.is_unique(t, &a.column))
            && compatible(&col_type, &a.datatype)
    };
    let mut merged: Vec<Hit> = vec![];
    let mut seen = BTreeSet::new();
    if ctx.llm.embed_configured() {
        let full = query_text(ctx, table, column);
        let name = format!("column_name: {column}");
        if let Ok(v) = ctx.llm.embed(&[full, name]).await {
            for q in &v {
                let mut scored: Vec<Hit> = ctx
                    .attributes()
                    .filter(|a| eligible(a))
                    .filter_map(|a| a.embedding.as_ref().map(|e| Hit { attr: a, distance: 1.0 - cosine(q, e) }))
                    .collect();
                scored.sort_by(|a, b| {
                    a.distance.partial_cmp(&b.distance).unwrap_or(std::cmp::Ordering::Equal).then(a.attr.id.cmp(&b.attr.id))
                });
                for h in scored.into_iter().take(3) {
                    if seen.insert(h.attr.id.clone()) {
                        merged.push(h);
                    }
                }
            }
        }
    }
    for h in heuristic_hits(ctx, table, column) {
        if eligible(h.attr) && seen.insert(h.attr.id.clone()) {
            merged.push(h);
        }
    }
    merged
}

async fn llm_pick(ctx: &Context<'_>, table: &Table, column: &str, hits: &[Hit<'_>]) -> Option<(String, String)> {
    let desc = table.column(column).and_then(|c| c.description.clone()).filter(|d| !d.is_empty()).unwrap_or_else(|| "(none)".into());
    let s = stringify_samples(&ctx.samples(table, column), Some(30));
    let samples = if s.is_empty() { "(none)".to_string() } else { format!("sample_values: {}", s.join(", ")) };
    let lines: Vec<String> = hits
        .iter()
        .map(|h| {
            let syn = ctx
                .results
                .get(&h.attr.table_id)
                .and_then(|r| r.terms.iter().find(|t| t.id == h.attr.term_id))
                .map(|t| t.synonyms.clone())
                .unwrap_or_default();
            let text: String = attribute_text(h.attr, &syn).chars().take(300).collect();
            format!("column_id={} | {text}", h.attr.id)
        })
        .collect();
    let human = format!(
        "Foreign-key column: {column} (table: {})\nDescription: {desc}\nSample values: {samples}\n\n\
Candidate PK columns (from vector search):\n{}\n\n\
Which candidate does this FK column reference? Return its exact column_id value, or null if none are a confident match.",
        table.name,
        lines.join("\n")
    );
    let r = ctx
        .llm
        .structured::<prompts::FkHitSelection>("fk_hit", prompts::FK_HIT_SYSTEM, &human, prompts::fk_hit_schema(), None, Some(4096))
        .await?;
    let sel = r.selected_id.filter(|s| !s.is_empty())?;
    if !hits.iter().any(|h| h.attr.id == sel) {
        tracing::warn!(column, "the model returned an unknown candidate id; discarded");
        return None;
    }
    Some((sel, r.rationale))
}

/// When the model abstains: a same-named candidate whose known values contain every sampled value.
fn match_by_samples(ctx: &Context, table: &Table, column: &str, hits: &[Hit]) -> Option<String> {
    let samples: BTreeSet<String> = stringify_samples(&ctx.samples(table, column), None).into_iter().collect();
    if samples.is_empty() {
        return None;
    }
    let mut matches: Vec<&Hit> = hits
        .iter()
        .filter(|h| h.attr.column.eq_ignore_ascii_case(column))
        .filter(|h| {
            ctx.tables
                .get(&h.attr.table_id)
                .and_then(|t| ctx.complete_values(t, &h.attr.column))
                .is_some_and(|vals| samples.is_subset(&vals))
        })
        .collect();
    matches.sort_by(|a, b| a.distance.partial_cmp(&b.distance).unwrap_or(std::cmp::Ordering::Equal));
    matches.first().map(|h| h.attr.id.clone())
}

/// Inferred relationships for the suspected FK columns (step 2).
pub async fn inferred(ctx: &Context<'_>, declared: &[Relationship]) -> Vec<Relationship> {
    let declared_cols: BTreeSet<(String, String)> =
        declared.iter().map(|r| (r.source_table.clone(), r.source_column.to_lowercase())).collect();
    let mut queue: Vec<(&Table, String, String)> = vec![];
    for t in &ctx.tables.tables {
        let Some(r) = ctx.results.get(&t.id) else { continue };
        for (col, why) in &r.facts.suspected_fks {
            if declared_cols.contains(&(t.id.clone(), col.to_lowercase())) {
                continue;
            }
            if r.attributes.iter().any(|a| a.column.eq_ignore_ascii_case(col)) {
                continue;
            }
            queue.push((t, col.clone(), why.clone()));
        }
    }
    let mut out = vec![];
    // Two at a time, as upstream; results are collected in queue order, so the outcome does not depend on timing.
    for pair in queue.chunks(2) {
        let futs = pair.iter().map(|(t, col, why)| resolve_one(ctx, t, col, why));
        for r in super::join_all(futs).await.into_iter().flatten() {
            out.push(r);
        }
    }
    out
}

async fn resolve_one(ctx: &Context<'_>, table: &Table, column: &str, why: &str) -> Option<Relationship> {
    let hits = candidates(ctx, table, column).await;
    if hits.is_empty() {
        return None;
    }
    let (attr_id, provenance, confidence, rationale) = match llm_pick(ctx, table, column, &hits).await {
        Some((id, rationale)) => (id, "model", 0.8, rationale),
        None => {
            let id = match_by_samples(ctx, table, column, &hits)?;
            (id, "inferred", 0.7, "every sampled value of the column is a value of the referenced key".to_string())
        }
    };
    let a = hits.iter().find(|h| h.attr.id == attr_id)?.attr;
    let target = ctx.tables.get(&a.table_id)?;
    let detail = if why.is_empty() { rationale.clone() } else { format!("{why}; {rationale}") };
    Some(Relationship {
        id: rel_id(&table.id, column, &target.id, &a.column),
        source_table: table.id.clone(),
        source_column: column.to_string(),
        target_table: target.id.clone(),
        target_column: a.column.clone(),
        cardinality: "many_to_one".into(),
        confidence,
        join_default: true,
        provenance: vec![prov(provenance, detail, confidence, ctx.run_id)],
        evidence: JoinEvidence::default(),
        rationale,
    })
}

/// Drops inferred relationships whose column shares no value with its target, or is empty (step 3,
/// `prune_disjoint_semantic_fks`). Only a complete value set can prove disjointness; a sample cannot.
pub fn prune_disjoint(ctx: &Context, rels: Vec<Relationship>) -> (Vec<Relationship>, Vec<Relationship>) {
    let mut keep = vec![];
    let mut pruned = vec![];
    for r in rels {
        let (Some(st), Some(tt)) = (ctx.tables.get(&r.source_table), ctx.tables.get(&r.target_table)) else {
            keep.push(r);
            continue;
        };
        if ctx.all_null(st, &r.source_column) {
            tracing::info!(source = %st.qualified(), column = %r.source_column, "semantic FK removed: empty source column");
            pruned.push(r);
            continue;
        }
        let src: BTreeSet<String> = stringify_samples(&ctx.samples(st, &r.source_column), None).into_iter().collect();
        if let Some(target_values) = ctx.complete_values(tt, &r.target_column) {
            if !src.is_empty() && src.is_disjoint(&target_values) {
                tracing::info!(source = %st.qualified(), column = %r.source_column, "semantic FK removed: no shared values");
                pruned.push(r);
                continue;
            }
        }
        keep.push(r);
    }
    (keep, pruned)
}

/// Merges joins observed in the query log (step 4).
pub fn merge_observed(
    ctx: &Context,
    rels: &mut Vec<Relationship>,
    observed: &BTreeMap<crate::querylog::JoinKey, crate::querylog::JoinSeen>,
) {
    for ((lt, lc, rt, rc), (n, execs, ids)) in observed {
        let (Some(l), Some(r)) = (ctx.tables.get(lt), ctx.tables.get(rt)) else { continue };
        if l.column(lc).is_none() || r.column(rc).is_none() {
            continue;
        }
        let conf = (0.4 + 0.15 * *n as f64).min(0.95);
        let p = prov(
            "observed_in_queries",
            format!(
                "joined on {}.{lc} = {}.{rc} in {n} quer{} ({execs} runs)",
                l.qualified(),
                r.qualified(),
                if *n == 1 { "y" } else { "ies" }
            ),
            conf,
            ctx.run_id,
        );
        let ev = JoinEvidence { queries: *n, executions: *execs, query_ids: ids.clone() };
        let existing = rels.iter_mut().find(|x| {
            (x.source_table == *lt
                && x.source_column.eq_ignore_ascii_case(lc)
                && x.target_table == *rt
                && x.target_column.eq_ignore_ascii_case(rc))
                || (x.source_table == *rt
                    && x.source_column.eq_ignore_ascii_case(rc)
                    && x.target_table == *lt
                    && x.target_column.eq_ignore_ascii_case(lc))
        });
        if let Some(x) = existing {
            x.provenance.push(p);
            x.evidence = ev;
            x.confidence = x.confidence.max(conf);
            continue;
        }
        // The key side is the target; neither unique: the direction is unknown.
        let (src, sc, tgt, tc, card) = if ctx.is_unique(r, rc) && !ctx.is_unique(l, lc) {
            (l, lc, r, rc, "many_to_one")
        } else if ctx.is_unique(l, lc) && !ctx.is_unique(r, rc) {
            (r, rc, l, lc, "many_to_one")
        } else if ctx.is_unique(l, lc) && ctx.is_unique(r, rc) {
            (l, lc, r, rc, "one_to_one")
        } else {
            (l, lc, r, rc, "unknown")
        };
        rels.push(Relationship {
            id: rel_id(&src.id, sc, &tgt.id, tc),
            source_table: src.id.clone(),
            source_column: sc.clone(),
            target_table: tgt.id.clone(),
            target_column: tc.clone(),
            cardinality: card.into(),
            confidence: conf,
            join_default: true,
            provenance: vec![p],
            evidence: ev,
            rationale: String::new(),
        });
    }
}

/// One default per (table, target column) when several roles reference it (step 5,
/// `select_semantic_fk_join_defaults`): the model's choice, else the first column by name.
pub async fn select_join_defaults(ctx: &Context<'_>, rels: &mut [Relationship]) {
    let mut groups: BTreeMap<(String, String, String), Vec<usize>> = BTreeMap::new();
    for (i, r) in rels.iter().enumerate() {
        groups.entry((r.source_table.clone(), r.target_table.clone(), r.target_column.to_lowercase())).or_default().push(i);
    }
    for ((st, tt, tc), idx) in groups {
        if idx.len() < 2 {
            continue;
        }
        let (Some(s), Some(t)) = (ctx.tables.get(&st), ctx.tables.get(&tt)) else { continue };
        let col_id = |i: usize| format!("{}#{}", st, rels[i].source_column);
        let lines: Vec<String> = idx
            .iter()
            .map(|i| {
                let d = s.column(&rels[*i].source_column).and_then(|c| c.description.clone()).unwrap_or_else(|| "(none)".into());
                format!("- source_column_id: {}\n  column: {}\n  description: {d}", col_id(*i), rels[*i].source_column)
            })
            .collect();
        let target_attr = ctx.attributes().find(|a| a.table_id == tt && a.column.eq_ignore_ascii_case(&tc)).map(|a| a.name.clone());
        let none = || "(none)".to_string();
        let prompt = format!(
            "Source table: {}\nSource table description: {}\nTarget table: {}\nTarget table description: {}\nTarget column: {tc}\n\
Target column description: {}\nTarget semantic attribute: {}\n\nCandidate relationships:\n{}\n",
            s.name,
            s.description.clone().unwrap_or_else(none),
            t.name,
            t.description.clone().unwrap_or_else(none),
            t.column(&tc).and_then(|c| c.description.clone()).unwrap_or_else(none),
            target_attr.unwrap_or_else(none),
            lines.join("\n")
        );
        let picked = ctx
            .llm
            .structured::<prompts::JoinDefaultSelection>("join_default", prompts::JOIN_DEFAULT_SYSTEM, &prompt, prompts::join_default_schema(), None, None)
            .await
            .and_then(|r| {
                tracing::info!(source = %s.qualified(), target = %t.qualified(), rationale = %r.rationale, "join default chosen by the model");
                r.selected_column_id
            })
            .and_then(|sel| idx.iter().copied().find(|i| col_id(*i) == sel));
        let chosen = picked
            .unwrap_or_else(|| *idx.iter().min_by_key(|i| (rels[**i].source_column.to_lowercase(), col_id(**i))).expect("non-empty group"));
        for i in &idx {
            rels[*i].join_default = *i == chosen;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_types() {
        assert_eq!(stem("customer_id").as_deref(), Some("customer"));
        assert_eq!(stem("buyerId").as_deref(), Some("buyer"));
        assert_eq!(stem("id"), None);
        assert_eq!(singular("categories"), "category");
        assert_eq!(singular("customers"), "customer");
        assert!(compatible("bigint", "integer"));
        assert!(!compatible("bigint", "varchar"));
        assert!(compatible("uuid", "varchar"));
    }
}
