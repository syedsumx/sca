// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/catalog/sql_parse.py (`parse_queries_df`, the in-memory and
// stored merge of equivalent queries) and auto_ontology/catalog/query_stats.py.
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust; equivalence is the normalised form (literals and SELECT-list order ignored)
// instead of a pre-filter followed by a sqlglot comparison; counters are kept per month as `YYYY-MM`.
//! The query log: each distinct query shape once, with how often, by whom and when it ran, and the usage
//! statistics compilation derives from it.

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;

use crate::catalog::TableIndex;
use crate::model::{now_ms, QueryIn};
use crate::repo::StoredQuery;
use crate::sqlx::{self, Extraction, SchemaLookup};

/// The catalog as the SQL extractor sees it.
pub struct CatalogSchema<'a>(pub &'a TableIndex);

impl SchemaLookup for CatalogSchema<'_> {
    fn columns(&self, table_key: &str) -> Option<Vec<String>> {
        self.0.resolve(table_key).map(|t| t.columns.iter().map(|c| c.name.to_lowercase()).collect())
    }
}

#[derive(Debug, Default, Serialize)]
pub struct IngestReport {
    pub received: usize,
    /// New distinct query shapes.
    pub added: usize,
    /// Executions merged into a shape already known (in this batch or stored).
    pub merged: usize,
    /// Queries that read no table in the catalog (kept out, as upstream).
    pub skipped: usize,
    pub failed: Vec<FailedQuery>,
}

#[derive(Debug, Serialize)]
pub struct FailedQuery {
    pub id: Option<String>,
    pub error: String,
}

/// `YYYY-MM` of a Unix time in milliseconds (UTC).
pub fn month_of(ms: i64) -> String {
    let days = ms.div_euclid(86_400_000);
    // Civil-from-days (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}")
}

pub fn fingerprint(normalized: &str) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(normalized.as_bytes()))[..32].to_string()
}

/// Merges a batch into the stored log: returns the rows to write and what happened to each query.
pub fn merge_batch(
    batch: &[QueryIn],
    dialects: &[String],
    catalog: &TableIndex,
    existing: &BTreeMap<String, StoredQuery>,
) -> (Vec<StoredQuery>, IngestReport) {
    let mut report = IngestReport { received: batch.len(), ..Default::default() };
    let mut touched: BTreeMap<String, StoredQuery> = BTreeMap::new();
    let schema = CatalogSchema(catalog);
    for q in batch {
        let sql = q.sql.trim();
        if sql.is_empty() || sql.len() > 200_000 {
            report.failed.push(FailedQuery { id: q.id.clone(), error: "empty or too long".into() });
            continue;
        }
        let x = match sqlx::extract(sql, dialects, &schema) {
            Ok(x) => x,
            Err(e) => {
                report.failed.push(FailedQuery { id: q.id.clone(), error: e.0 });
                continue;
            }
        };
        if !x.referenced.iter().any(|t| catalog.resolve(t).is_some()) {
            report.skipped += 1;
            continue;
        }
        let Some(norm) = sqlx::normalize(sql, dialects, true) else {
            report.failed.push(FailedQuery { id: q.id.clone(), error: "could not normalise".into() });
            continue;
        };
        let fp = fingerprint(&norm);
        let count = q.count.max(1);
        let ts = q.ts_ms.unwrap_or_else(now_ms);
        let month = month_of(ts);
        let entry = touched.entry(fp.clone()).or_insert_with(|| {
            existing.get(&fp).cloned().unwrap_or_else(|| StoredQuery {
                fingerprint: fp.clone(),
                id: q.id.clone().unwrap_or_else(|| format!("q_{}", &fp[..16])),
                sql: sql.to_string(),
                total_count: 0,
                months: BTreeMap::new(),
                users: vec![],
                first_ms: ts,
                last_ms: ts,
                dialect: x.dialect_used.clone(),
            })
        });
        if entry.total_count == 0 {
            report.added += 1;
        } else {
            report.merged += 1;
        }
        entry.total_count += count;
        *entry.months.entry(month).or_insert(0) += count;
        if let Some(u) = q.user.as_ref().filter(|u| !u.is_empty()) {
            if !entry.users.contains(u) && entry.users.len() < 100 {
                entry.users.push(u.clone());
            }
        }
        entry.first_ms = entry.first_ms.min(ts);
        entry.last_ms = entry.last_ms.max(ts);
    }
    (touched.into_values().collect(), report)
}

/// A query of the log, parsed against the current catalog.
#[derive(Clone, Debug)]
pub struct ParsedQuery {
    pub id: String,
    pub sql: String,
    pub count: i64,
    pub extraction: Extraction,
    /// Table key in the SQL → table id.
    pub resolved: BTreeMap<String, String>,
}

impl ParsedQuery {
    pub fn table_ids(&self) -> BTreeSet<String> {
        self.resolved.values().cloned().collect()
    }
}

pub fn parse_log(queries: &[StoredQuery], catalog: &TableIndex) -> Vec<ParsedQuery> {
    let schema = CatalogSchema(catalog);
    queries
        .iter()
        .filter_map(|q| {
            let dialects = if q.dialect.is_empty() { vec![] } else { vec![q.dialect.clone()] };
            let x = sqlx::extract(&q.sql, &dialects, &schema).ok()?;
            let resolved: BTreeMap<String, String> =
                x.referenced.iter().filter_map(|k| catalog.resolve(k).map(|t| (k.clone(), t.id.clone()))).collect();
            if resolved.is_empty() {
                return None;
            }
            Some(ParsedQuery { id: q.id.clone(), sql: q.sql.clone(), count: q.total_count, extraction: x, resolved })
        })
        .collect()
}

/// A query's usage score: how many times it ran.
///
/// Upstream (`sql_attribute_suggester._usage_score`) deliberately returns 0.0 — a pinned bug: its scorer read
/// `count_monthly_YYYY_MM` properties the writer never produced, so expressions were never ranked. Its
/// docstring names the fix, scoring by `total_counter`; this port applies that fix, so the busiest
/// expressions are the ones the model is shown.
pub fn usage_score(q: &ParsedQuery) -> f64 {
    q.count as f64
}

/// Joins observed in the log between two catalog tables (equality only): (left id, left col, right id,
/// right col) → (distinct queries, executions, query ids).
/// (left table id, left column, right table id, right column).
pub type JoinKey = (String, String, String, String);
/// (distinct queries, executions, query ids).
pub type JoinSeen = (i64, i64, Vec<String>);

pub fn observed_joins(log: &[ParsedQuery]) -> BTreeMap<JoinKey, JoinSeen> {
    let mut out: BTreeMap<JoinKey, JoinSeen> = BTreeMap::new();
    for q in log {
        for j in &q.extraction.joins {
            if j.operator != "=" {
                continue;
            }
            let (Some(l), Some(r)) = (q.resolved.get(&j.left_table), q.resolved.get(&j.right_table)) else { continue };
            if l == r {
                continue;
            }
            let mut key = (l.clone(), j.left_column.clone(), r.clone(), j.right_column.clone());
            if (key.0.clone(), key.1.clone()) > (key.2.clone(), key.3.clone()) {
                key = (key.2, key.3, key.0, key.1);
            }
            let e = out.entry(key).or_insert((0, 0, vec![]));
            if !e.2.contains(&q.id) {
                e.0 += 1;
                e.1 += q.count;
                if e.2.len() < 20 {
                    e.2.push(q.id.clone());
                }
            }
        }
    }
    out
}

/// How much each table and column is read: (distinct queries, executions, per-column executions).
pub fn table_usage(log: &[ParsedQuery]) -> BTreeMap<String, (i64, i64, BTreeMap<String, i64>)> {
    let mut out: BTreeMap<String, (i64, i64, BTreeMap<String, i64>)> = BTreeMap::new();
    for q in log {
        for (key, id) in &q.resolved {
            let e = out.entry(id.clone()).or_default();
            e.0 += 1;
            e.1 += q.count;
            if let Some(cols) = q.extraction.tables.get(key) {
                for c in cols {
                    *e.2.entry(c.clone()).or_insert(0) += q.count;
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Column, Table};

    fn catalog() -> TableIndex {
        let t = |id: &str, name: &str, cols: &[&str]| Table {
            id: id.into(),
            schema: "retail".into(),
            name: name.into(),
            columns: cols.iter().map(|c| Column { name: c.to_string(), ..Default::default() }).collect(),
            ..Default::default()
        };
        TableIndex::new(vec![t("O", "orders", &["id", "customer_id", "amount"]), t("C", "customers", &["id", "name"])])
    }

    fn q(sql: &str, count: i64, user: &str, ts: i64) -> QueryIn {
        QueryIn { id: None, sql: sql.into(), count, user: Some(user.into()), ts_ms: Some(ts) }
    }

    #[test]
    fn equivalent_queries_merge_and_counters_accumulate() {
        let cat = catalog();
        let jan = 1_767_225_600_000; // 2026-01-01
        let feb = jan + 31 * 86_400_000;
        let batch = vec![
            q("SELECT id, amount FROM retail.orders WHERE amount > 10", 2, "ana", jan),
            q("select amount, id from retail.orders where amount > 99", 3, "bob", feb),
            q("SELECT name FROM retail.customers", 1, "ana", jan),
            q("SELECT * FROM somewhere.else", 1, "ana", jan),
            q("SELEC broken", 1, "ana", jan),
        ];
        let (rows, rep) = merge_batch(&batch, &[], &cat, &BTreeMap::new());
        assert_eq!((rep.added, rep.merged, rep.skipped, rep.failed.len()), (2, 1, 1, 1));
        let orders = rows.iter().find(|r| r.sql.contains("orders")).unwrap();
        assert_eq!(orders.total_count, 5);
        assert_eq!(orders.months.get("2026-01"), Some(&2));
        assert_eq!(orders.months.get("2026-02"), Some(&3));
        assert_eq!(orders.users, vec!["ana", "bob"]);
        // Against what is stored, the next batch adds to the same row.
        let existing: BTreeMap<String, StoredQuery> = rows.iter().map(|r| (r.fingerprint.clone(), r.clone())).collect();
        let (again, rep2) = merge_batch(&[q("SELECT id, amount FROM retail.orders WHERE amount > 1", 1, "cal", feb)], &[], &cat, &existing);
        assert_eq!(rep2.merged, 1);
        assert_eq!(again[0].total_count, 6);
        assert_eq!(month_of(0), "1970-01");
        assert_eq!(month_of(1_772_323_200_000), "2026-03");
    }

    #[test]
    fn joins_and_usage_come_from_the_log() {
        let cat = catalog();
        let stored = |sql: &str, n: i64, id: &str| StoredQuery {
            fingerprint: id.into(),
            id: id.into(),
            sql: sql.into(),
            total_count: n,
            months: BTreeMap::new(),
            users: vec![],
            first_ms: 0,
            last_ms: 0,
            dialect: String::new(),
        };
        let log = parse_log(
            &[
                stored("SELECT c.name, SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id = c.id GROUP BY 1", 4, "q1"),
                stored("SELECT * FROM customers c JOIN orders o ON c.id = o.customer_id", 1, "q2"),
            ],
            &cat,
        );
        let joins = observed_joins(&log);
        let ((l, lc, r, rc), (n, execs, ids)) = joins.iter().next().unwrap();
        assert_eq!((l.as_str(), lc.as_str(), r.as_str(), rc.as_str()), ("C", "id", "O", "customer_id"));
        assert_eq!((*n, *execs, ids.len()), (2, 5, 2));
        let usage = table_usage(&log);
        assert_eq!(usage["O"].1, 5);
        assert_eq!(usage["O"].2["amount"], 4);
        assert_eq!(usage_score(&log[0]), 4.0);
    }
}
