// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/semantic/sql_attribute_suggester.py and bridge_tables.py
// (and their tests).
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust; expressions are ranked by usage (`total_counter`, the documented fix of the
// upstream scorer); the bridge owner is chosen by embedding similarity instead of a reranker.
//! SqlAttributes (metrics and named filters) from the query log, and join patterns through bridge tables.

use std::collections::{BTreeMap, BTreeSet};

use super::embed::term_text;
use crate::catalog::TableIndex;
use crate::llm::{cosine, Llm};
use crate::model::{stable_id, Bridge, FkPair, Metric, Provenance, Relationship, TableResult, Term};
use crate::prompts;
use crate::querylog::{usage_score, ParsedQuery};
use crate::sqlx;

const TOP_N: usize = 10;

/// `(expression, score, query ids)` for every expression in `queries`, highest score first; ties keep the
/// order the expressions were first seen in (`_rank_expressions`).
pub fn rank_expressions(queries: &[&ParsedQuery]) -> Vec<(String, f64, Vec<String>)> {
    let mut order: Vec<String> = vec![];
    let mut scores: BTreeMap<String, f64> = BTreeMap::new();
    let mut ids: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for q in queries {
        let score = usage_score(q);
        for e in sqlx::extract_expressions(&q.sql) {
            if !scores.contains_key(&e) {
                order.push(e.clone());
            }
            *scores.entry(e.clone()).or_insert(0.0) += score;
            let v = ids.entry(e).or_default();
            if !v.contains(&q.id) {
                v.push(q.id.clone());
            }
        }
    }
    let mut out: Vec<(String, f64, Vec<String>)> =
        order.into_iter().map(|e| (e.clone(), scores[&e], ids.remove(&e).unwrap_or_default())).collect();
    out.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    out
}

pub struct Ctx<'a> {
    pub llm: &'a Llm,
    pub tables: &'a TableIndex,
    pub results: &'a BTreeMap<String, TableResult>,
    pub log: &'a [ParsedQuery],
    pub run_id: &'a str,
}

/// Lower-case table name → schema, for qualifying generated SQL.
pub fn schema_map(tables: &TableIndex) -> BTreeMap<String, String> {
    let mut m = BTreeMap::new();
    let mut dup = BTreeSet::new();
    for t in &tables.tables {
        if t.schema.is_empty() {
            continue;
        }
        if m.insert(t.name.to_lowercase(), t.schema.clone()).is_some_and(|prev| prev != t.schema) {
            dup.insert(t.name.to_lowercase());
        }
    }
    for d in dup {
        m.remove(&d); // ambiguous: leave unqualified rather than guess
    }
    m
}

/// The tables a statement reads, when every one of them is in the catalog (a generated statement's
/// validation, as `create_sql_attribute` validates SQL against the schema).
pub fn validate_sql(sql: &str, tables: &TableIndex) -> Result<Vec<String>, String> {
    let x = sqlx::extract(sql, &[], &crate::querylog::CatalogSchema(tables)).map_err(|e| e.0)?;
    let mut ids = vec![];
    for t in &x.referenced {
        let Some(found) = tables.resolve(t) else { return Err(format!("unknown table {t}")) };
        if let Some(cols) = x.tables.get(t) {
            for c in cols {
                if found.column(c).is_none() {
                    return Err(format!("unknown column {t}.{c}"));
                }
            }
        }
        if !ids.contains(&found.id) {
            ids.push(found.id.clone());
        }
    }
    if ids.is_empty() {
        return Err("reads no table".into());
    }
    Ok(ids)
}

async fn judge(llm: &Llm, term: &Term, top: &[(String, f64, Vec<String>)]) -> Vec<prompts::SuggestedAttr> {
    if top.is_empty() {
        return vec![];
    }
    let block: Vec<String> = top.iter().enumerate().map(|(i, (e, s, _))| format!("{}. [{s:.0} uses] {e}", i + 1)).collect();
    llm.structured::<prompts::Suggestions>(
        "sql_attributes",
        prompts::SQL_ATTR_SYSTEM,
        &prompts::sql_attr_user(&term.name, &term.description, &block.join("\n")),
        prompts::suggestions_schema(),
        Some(0.0),
        None,
    )
    .await
    .map(|s| s.suggestions)
    .unwrap_or_default()
}

/// SqlAttributes from the query log for every Term (`suggest_sql_attributes`). The model calls run
/// concurrently (four Terms at a time); what they suggest is kept in Term order, so the outcome is the same
/// however the calls finish.
pub async fn suggest(ctx: &Ctx<'_>) -> Vec<Metric> {
    let terms: Vec<&Term> = ctx.results.values().flat_map(|r| r.terms.iter()).collect();
    let smap = schema_map(ctx.tables);
    type Ranked = Vec<(String, f64, Vec<String>)>;
    let mut judged: Vec<(usize, Vec<prompts::SuggestedAttr>, Ranked)> = vec![];
    for chunk in terms.chunks(4).enumerate().map(|(i, c)| (i * 4, c)).collect::<Vec<_>>() {
        let (base, chunk) = chunk;
        let futs: Vec<_> = chunk
            .iter()
            .map(|t| {
                let queries: Vec<&ParsedQuery> = ctx.log.iter().filter(|q| q.table_ids().contains(&t.table_id)).collect();
                let ranked = rank_expressions(&queries);
                async move {
                    let top: Vec<(String, f64, Vec<String>)> = ranked.iter().take(TOP_N).cloned().collect();
                    let s = judge(ctx.llm, t, &top).await;
                    (s, ranked)
                }
            })
            .collect();
        for (offset, (s, ranked)) in super::join_all(futs.into_iter()).await.into_iter().enumerate() {
            judged.push((base + offset, s, ranked));
        }
    }
    let mut seen_expr = BTreeSet::new();
    let mut out: Vec<Metric> = vec![];
    for (i, suggestions, ranked) in judged {
        let term = terms[i];
        let sql_by_id: BTreeMap<&str, &str> = ctx.log.iter().map(|q| (q.id.as_str(), q.sql.as_str())).collect();
        for s in suggestions {
            let key = s.expression.trim().to_lowercase();
            if key.is_empty() || !seen_expr.insert(key) {
                continue;
            }
            let (score, ids) =
                ranked.iter().find(|(e, _, _)| *e == s.expression).map(|(_, sc, ids)| (*sc, ids.clone())).unwrap_or((0.0, vec![]));
            let sources: Vec<String> = ids.iter().filter_map(|id| sql_by_id.get(id.as_str()).map(|s| s.to_string())).collect();
            let sql = match sqlx::build_valid_sql(&s.expression, &sources, &smap) {
                Ok(sql) => sql,
                Err(e) => {
                    tracing::warn!(term = %term.name, name = %s.name, error = %e, "cannot build valid SQL; skipped");
                    continue;
                }
            };
            let tables = match validate_sql(&sql, ctx.tables) {
                Ok(t) => t,
                Err(e) => {
                    tracing::warn!(term = %term.name, name = %s.name, error = %e, "generated SQL failed schema validation; skipped");
                    continue;
                }
            };
            let name = s.name.trim().to_string();
            if out.iter().any(|m| m.term_id == term.id && m.name.eq_ignore_ascii_case(&name)) {
                continue; // name conflict
            }
            let norm = sqlx::normalize(&sql, &[], false);
            if out.iter().any(|m| m.term_id == term.id && sqlx::normalize(&m.sql, &[], false) == norm) {
                continue; // equivalent SQL already defined for the Term
            }
            out.push(Metric {
                id: stable_id("met", &[&term.id, &name.to_lowercase()]),
                term_id: term.id.clone(),
                term_name: term.name.clone(),
                name,
                description: s.description.trim().to_string(),
                expression: s.expression.trim().to_string(),
                sql,
                source: "sql".into(),
                tables,
                source_query_ids: ids,
                usage: score,
                provenance: vec![Provenance {
                    source: "model".into(),
                    detail: format!("chosen from expressions in the query log ({score:.0} uses)"),
                    confidence: 0.7,
                    run_id: ctx.run_id.into(),
                }],
            });
        }
    }
    out
}

// --------------------------------------------------------------------------------------------- bridges

/// Pure-FK junction tables (`fetch_bridge_table_candidates`): every column is a foreign key (declared or
/// inferred) that resolves to a column of a table in the catalog, at least two of them, and none of the table's
/// columns is a ColumnAttribute.
pub fn bridge_candidates(
    tables: &TableIndex,
    results: &BTreeMap<String, TableResult>,
    rels: &[Relationship],
) -> Vec<(String, Vec<FkPair>)> {
    let mut out = vec![];
    for t in &tables.tables {
        if t.columns.len() < 2 {
            continue;
        }
        if results.get(&t.id).is_some_and(|r| !r.attributes.is_empty()) {
            continue;
        }
        let mut pairs = vec![];
        let mut every = true;
        for c in &t.columns {
            let r = rels
                .iter()
                .find(|r| r.source_table == t.id && r.source_column.eq_ignore_ascii_case(&c.name) && r.join_default)
                .or_else(|| rels.iter().find(|r| r.source_table == t.id && r.source_column.eq_ignore_ascii_case(&c.name)));
            match r {
                Some(r) if tables.get(&r.target_table).is_some_and(|tt| tt.column(&r.target_column).is_some()) => pairs.push(FkPair {
                    source_column: c.name.clone(),
                    target_table: r.target_table.clone(),
                    target_column: r.target_column.clone(),
                }),
                _ => {
                    every = false;
                    break;
                }
            }
        }
        if every && pairs.len() >= 2 {
            out.push((t.id.clone(), pairs));
        }
    }
    out
}

/// Join-pattern SqlAttributes for bridge tables (`build_bridge_tables_sql_attributes`), each on the better
/// matching of the Terms that represent its FK targets.
pub async fn bridges(ctx: &Ctx<'_>, rels: &[Relationship], existing: &[Metric]) -> (Vec<Bridge>, Vec<Metric>) {
    let mut bridges = vec![];
    let mut metrics: Vec<Metric> = vec![];
    for (table_id, pairs) in bridge_candidates(ctx.tables, ctx.results, rels) {
        let Some(t) = ctx.tables.get(&table_id) else { continue };
        let mut bridge = Bridge {
            id: stable_id("brg", &[&table_id]),
            table_id: table_id.clone(),
            table_name: t.qualified(),
            fk_pairs: pairs.clone(),
            metric_id: None,
            provenance: vec![Provenance {
                source: "inferred".into(),
                detail: "every column is a foreign key".into(),
                confidence: 0.9,
                run_id: ctx.run_id.into(),
            }],
        };
        // The Terms that represent the distinct FK targets.
        let mut terms: Vec<&Term> = vec![];
        for p in &pairs {
            if let Some(term) = ctx.results.get(&p.target_table).and_then(|r| r.terms.first()) {
                if !terms.iter().any(|x| x.id == term.id) {
                    terms.push(term);
                }
            }
        }
        if terms.is_empty() {
            tracing::warn!(table = %t.qualified(), "bridge table: no Terms on its FK targets — no join pattern");
            bridges.push(bridge);
            continue;
        }
        let related: BTreeSet<String> = pairs.iter().filter_map(|p| ctx.tables.get(&p.target_table).map(|x| x.qualified())).collect();
        let fk_block: Vec<String> = pairs
            .iter()
            .filter_map(|p| {
                ctx.tables.get(&p.target_table).map(|x| format!("  - {} -> {}.{}", p.source_column, x.qualified(), p.target_column))
            })
            .collect();
        let hint = if related.len() == 1 { prompts::BRIDGE_SELF_HINT } else { prompts::BRIDGE_JOIN_HINT };
        let user =
            prompts::bridge_user(&t.qualified(), &related.iter().cloned().collect::<Vec<_>>().join(", "), &fk_block.join("\n"), hint);
        let Some(p) = ctx
            .llm
            .structured::<prompts::BridgeProposal>("bridge", prompts::BRIDGE_SYSTEM, &user, prompts::bridge_schema(), Some(0.0), None)
            .await
        else {
            tracing::warn!(table = %t.qualified(), "bridge table: no proposal from the model");
            bridges.push(bridge);
            continue;
        };
        let owner = pick_owner(ctx.llm, &terms, &p.description).await;
        let tables = match validate_sql(&p.expression, ctx.tables) {
            Ok(x) => x,
            Err(e) => {
                tracing::warn!(table = %t.qualified(), error = %e, "bridge SQL failed validation");
                bridges.push(bridge);
                continue;
            }
        };
        let name = p.name.trim().to_string();
        if existing.iter().chain(metrics.iter()).any(|m| m.term_id == owner.id && m.name.eq_ignore_ascii_case(&name)) {
            bridges.push(bridge);
            continue;
        }
        let m = Metric {
            id: stable_id("met", &[&owner.id, &name.to_lowercase()]),
            term_id: owner.id.clone(),
            term_name: owner.name.clone(),
            name,
            description: p.description.trim().to_string(),
            expression: p.expression.trim().to_string(),
            sql: p.expression.trim().to_string(),
            source: "bridgeTable".into(),
            tables,
            source_query_ids: vec![],
            usage: 0.0,
            provenance: vec![Provenance {
                source: "model".into(),
                detail: format!("join pattern through bridge table {}", t.qualified()),
                confidence: 0.75,
                run_id: ctx.run_id.into(),
            }],
        };
        bridge.metric_id = Some(m.id.clone());
        metrics.push(m);
        bridges.push(bridge);
    }
    (bridges, metrics)
}

/// The Term that best matches the join pattern's description (`_pick_owner_term_id`): the only one, or the
/// most similar by embedding, or the first.
async fn pick_owner<'a>(llm: &Llm, terms: &[&'a Term], description: &str) -> &'a Term {
    if terms.len() == 1 || description.trim().is_empty() || !llm.embed_configured() {
        return terms[0];
    }
    let mut texts = vec![description.to_string()];
    texts.extend(terms.iter().map(|t| term_text(t)));
    match llm.embed(&texts).await {
        Ok(v) => {
            let q = &v[0];
            let mut best = (f64::MIN, terms[0]);
            for (i, t) in terms.iter().enumerate() {
                let s = cosine(q, &v[i + 1]);
                if s > best.0 {
                    best = (s, t);
                }
            }
            best.1
        }
        Err(_) => terms[0],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlx::Extraction;

    fn pq(id: &str, sql: &str, count: i64) -> ParsedQuery {
        ParsedQuery { id: id.into(), sql: sql.into(), count, extraction: Extraction::default(), resolved: BTreeMap::new() }
    }

    #[test]
    fn busy_expressions_rank_first_and_carry_their_queries() {
        let quiet = pq("quiet", "SELECT * FROM film WHERE rating = 'G'", 1);
        let busy = pq("busy", "SELECT * FROM film WHERE release_year > 2020", 500);
        let ranked = rank_expressions(&[&quiet, &busy]);
        assert_eq!(ranked[0].0, "release_year > 2020");
        assert_eq!(ranked[0].1, 500.0);
        assert_eq!(ranked[1].2, vec!["quiet"]);
        let both = pq("again", "SELECT * FROM film WHERE rating = 'G'", 3);
        let r = rank_expressions(&[&quiet, &both]);
        assert_eq!(r[0].1, 4.0);
        assert_eq!(r[0].2, vec!["quiet", "again"]);
    }
}
