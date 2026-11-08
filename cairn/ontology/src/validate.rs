// SPDX-License-Identifier: Apache-2.0
//! `POST /v1/validate-sql`: a query checked against the catalog and the published ontology.
//!
//! Findings: tables and columns the catalog does not have, deprecated tables and columns, joins with no known
//! path between their tables, joins that contradict the known keys between two tables, fan-out (a join on
//! non-unique columns on both sides, or a measure summed after a one-to-many join multiplies it), and aggregates
//! named like a certified definition that compute something else.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::catalog::TableIndex;
use crate::model::{Ontology, Relationship, Table};
use crate::querylog::CatalogSchema;
use crate::sqlx;

#[derive(Debug, Deserialize)]
pub struct ValidateIn {
    pub sql: String,
    #[serde(default)]
    pub dialects: Vec<String>,
    /// Certified definitions the caller holds (name, expression), checked as the ontology's metrics are.
    #[serde(default)]
    pub definitions: Vec<Definition>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Definition {
    pub name: String,
    pub expression: String,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Finding {
    pub kind: String,
    /// `error` (the query is wrong) or `warning` (it may be).
    pub severity: String,
    pub message: String,
    #[serde(default)]
    pub tables: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub suggestion: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct JoinCheck {
    pub left: String,
    pub right: String,
    pub known: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub relationship_id: Option<String>,
    pub provenance: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct Validation {
    pub valid: bool,
    pub tables: BTreeMap<String, Option<String>>,
    pub joins: Vec<JoinCheck>,
    pub findings: Vec<Finding>,
    pub ontology_version: Option<i64>,
}

fn finding(kind: &str, severity: &str, message: String, tables: Vec<String>, suggestion: Option<String>) -> Finding {
    Finding { kind: kind.into(), severity: severity.into(), message, tables, suggestion }
}

fn unique(o: Option<&Ontology>, t: &Table, column: &str) -> bool {
    t.is_pk(column)
        || t.column(column).and_then(|c| c.is_unique) == Some(true)
        || o.is_some_and(|o| {
            o.tables.iter().any(|f| f.table_id == t.id && f.profiles.get(column).and_then(|p| p.is_unique) == Some(true))
                || o.attributes.iter().any(|a| a.table_id == t.id && a.column.eq_ignore_ascii_case(column) && a.is_unique == Some(true))
                || o.relationships
                    .iter()
                    .any(|r| r.target_table == t.id && r.target_column.eq_ignore_ascii_case(column) && r.cardinality == "many_to_one")
        })
}

fn norm_name(s: &str) -> String {
    s.to_lowercase().chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '_' }).collect::<String>().trim_matches('_').to_string()
}

/// An expression with table qualifiers and spacing removed, for comparing definitions.
fn norm_expr(e: &str) -> String {
    let re = regex::Regex::new(r"\b[A-Za-z_][A-Za-z0-9_]*\.").expect("qualifier");
    re.replace_all(&e.to_lowercase(), "").chars().filter(|c| !c.is_whitespace()).collect()
}

pub fn validate(input: &ValidateIn, tables: &TableIndex, ontology: Option<&Ontology>) -> Validation {
    let mut v =
        Validation { valid: true, tables: BTreeMap::new(), joins: vec![], findings: vec![], ontology_version: ontology.map(|o| o.version) };
    let x = match sqlx::extract(&input.sql, &input.dialects, &CatalogSchema(tables)) {
        Ok(x) => x,
        Err(e) => {
            v.valid = false;
            v.findings.push(finding("syntax_error", "error", e.0, vec![], None));
            return v;
        }
    };
    let mut resolved: BTreeMap<String, &Table> = BTreeMap::new();
    for key in &x.referenced {
        let t = tables.resolve(key);
        v.tables.insert(key.clone(), t.map(|t| t.id.clone()));
        match t {
            None => v.findings.push(finding("unknown_table", "error", format!("{key} is not in the catalog"), vec![], None)),
            Some(t) => {
                if t.deprecated {
                    v.findings.push(finding(
                        "deprecated_table",
                        "warning",
                        format!("{} is deprecated", t.qualified()),
                        vec![t.id.clone()],
                        None,
                    ));
                }
                resolved.insert(key.clone(), t);
            }
        }
    }
    for (key, cols) in &x.tables {
        let Some(t) = resolved.get(key) else { continue };
        for c in cols {
            match t.column(c) {
                None => v.findings.push(finding(
                    "unknown_column",
                    "error",
                    format!("{}.{c} does not exist", t.qualified()),
                    vec![t.id.clone()],
                    None,
                )),
                Some(col) if col.deprecated => v.findings.push(finding(
                    "deprecated_column",
                    "warning",
                    format!("{}.{c} is deprecated", t.qualified()),
                    vec![t.id.clone()],
                    col.description.clone(),
                )),
                _ => {}
            }
        }
    }
    let rels: Vec<&Relationship> = ontology.map(|o| o.relationships.iter().collect()).unwrap_or_default();
    // Declared FKs count even before any compilation.
    let mut declared: Vec<Relationship> = vec![];
    for t in &tables.tables {
        for fk in &t.foreign_keys {
            if let Some(target) = tables.resolve(&fk.target_table) {
                for (i, c) in fk.columns.iter().enumerate() {
                    if let Some(tc) = fk.target_columns.get(i).or_else(|| target.primary_key.get(i)) {
                        declared.push(Relationship {
                            source_table: t.id.clone(),
                            source_column: c.clone(),
                            target_table: target.id.clone(),
                            target_column: tc.clone(),
                            cardinality: "many_to_one".into(),
                            provenance: vec![crate::model::Provenance {
                                source: "catalog_fk".into(),
                                detail: String::new(),
                                confidence: 1.0,
                                run_id: String::new(),
                            }],
                            ..Default::default()
                        });
                    }
                }
            }
        }
    }
    let all: Vec<&Relationship> = rels.into_iter().chain(declared.iter()).collect();
    for j in &x.joins {
        let (Some(l), Some(r)) = (resolved.get(&j.left_table), resolved.get(&j.right_table)) else { continue };
        if l.id == r.id {
            continue;
        }
        let between: Vec<&&Relationship> = all
            .iter()
            .filter(|x| (x.source_table == l.id && x.target_table == r.id) || (x.source_table == r.id && x.target_table == l.id))
            .collect();
        let matching = between.iter().find(|x| {
            (x.source_table == l.id
                && x.source_column.eq_ignore_ascii_case(&j.left_column)
                && x.target_column.eq_ignore_ascii_case(&j.right_column))
                || (x.source_table == r.id
                    && x.source_column.eq_ignore_ascii_case(&j.right_column)
                    && x.target_column.eq_ignore_ascii_case(&j.left_column))
        });
        let left = format!("{}.{}", l.qualified(), j.left_column);
        let right = format!("{}.{}", r.qualified(), j.right_column);
        v.joins.push(JoinCheck {
            left: left.clone(),
            right: right.clone(),
            known: matching.is_some(),
            relationship_id: matching.map(|m| m.id.clone()).filter(|i| !i.is_empty()),
            provenance: matching.map(|m| m.provenance.iter().map(|p| p.source.clone()).collect()).unwrap_or_default(),
        });
        if matching.is_none() && j.operator == "=" {
            if let Some(known) = between.first() {
                let (st, tt) = if known.source_table == l.id { (l, r) } else { (r, l) };
                v.findings.push(finding(
                    "join_contradicts_keys",
                    "error",
                    format!(
                        "{left} = {right} contradicts the known key between these tables: {}.{} = {}.{}",
                        st.qualified(),
                        known.source_column,
                        tt.qualified(),
                        known.target_column
                    ),
                    vec![l.id.clone(), r.id.clone()],
                    Some(format!("JOIN ON {}.{} = {}.{}", st.name, known.source_column, tt.name, known.target_column)),
                ));
            } else {
                v.findings.push(finding(
                    "unknown_join_path",
                    "warning",
                    format!("no known relationship joins {} and {} (on {left} = {right})", l.qualified(), r.qualified()),
                    vec![l.id.clone(), r.id.clone()],
                    None,
                ));
            }
        }
        if j.operator == "=" && !unique(ontology, l, &j.left_column) && !unique(ontology, r, &j.right_column) {
            v.findings.push(finding(
                "fan_out",
                "warning",
                format!("{left} = {right} joins non-unique columns on both sides: rows multiply (many-to-many)"),
                vec![l.id.clone(), r.id.clone()],
                None,
            ));
        }
    }
    // A measure of the "one" side aggregated after a one-to-many join is counted once per matching row.
    for a in &x.aggregates {
        if !matches!(a.function.as_str(), "sum" | "avg" | "count") || a.text.to_lowercase().contains("distinct") {
            continue;
        }
        for (tkey, col) in &a.columns {
            let Some(t) = resolved.get(tkey) else { continue };
            for j in &x.joins {
                let (mine, other, other_col, my_col) = if &j.left_table == tkey {
                    (&j.left_table, &j.right_table, &j.right_column, &j.left_column)
                } else if &j.right_table == tkey {
                    (&j.right_table, &j.left_table, &j.left_column, &j.right_column)
                } else {
                    continue;
                };
                let _ = mine;
                let Some(o) = resolved.get(other) else { continue };
                if !a.scope_tables.contains(other) {
                    continue;
                }
                if unique(ontology, t, my_col) && !unique(ontology, o, other_col) {
                    v.findings.push(finding(
                        "fan_out",
                        "warning",
                        format!(
                            "{} reads {}.{col}, but joining {} (many rows per {} row) repeats each value",
                            a.text,
                            t.qualified(),
                            o.qualified(),
                            t.name
                        ),
                        vec![t.id.clone(), o.id.clone()],
                        Some(format!("aggregate {} before joining, or use {}(DISTINCT …)", t.name, a.function)),
                    ));
                }
            }
        }
    }
    // Aggregates named like a certified definition must compute it.
    let mut defs: Vec<Definition> = input.definitions.clone();
    if let Some(o) = ontology {
        defs.extend(
            o.metrics.iter().filter(|m| m.source == "sql").map(|m| Definition { name: m.name.clone(), expression: m.expression.clone() }),
        );
    }
    for a in &x.aggregates {
        let Some(alias) = &a.alias else { continue };
        for d in &defs {
            if norm_name(&d.name) == norm_name(alias)
                && norm_expr(&d.expression) != norm_expr(&a.text)
                && sqlx::is_aggregate_name(d.expression.split('(').next().unwrap_or("").trim())
            {
                v.findings.push(finding(
                    "definition_mismatch",
                    "warning",
                    format!("{alias} is computed as {} but its certified definition is {}", a.text, d.expression),
                    vec![],
                    Some(d.expression.clone()),
                ));
            }
        }
    }
    v.valid = !v.findings.iter().any(|f| f.severity == "error");
    v
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Column, ForeignKey};

    fn cols(names: &[&str]) -> Vec<Column> {
        names.iter().map(|n| Column { name: n.to_string(), data_type: "bigint".into(), ..Default::default() }).collect()
    }

    fn index() -> TableIndex {
        let mut orders = Table {
            id: "O".into(),
            schema: "retail".into(),
            name: "orders".into(),
            primary_key: vec!["id".into()],
            columns: cols(&["id", "customer_id", "amount", "legacy_code"]),
            ..Default::default()
        };
        orders.columns[3].deprecated = true;
        orders.foreign_keys = vec![ForeignKey {
            columns: vec!["customer_id".into()],
            target_table: "retail.customers".into(),
            target_columns: vec!["id".into()],
        }];
        let customers = Table {
            id: "C".into(),
            schema: "retail".into(),
            name: "customers".into(),
            primary_key: vec!["id".into()],
            columns: cols(&["id", "region"]),
            ..Default::default()
        };
        let items = Table {
            id: "I".into(),
            schema: "retail".into(),
            name: "order_items".into(),
            columns: cols(&["order_id", "product_id", "qty"]),
            ..Default::default()
        };
        let tags = Table {
            id: "T".into(),
            schema: "retail".into(),
            name: "tags".into(),
            columns: cols(&["order_id", "tag"]),
            ..Default::default()
        };
        TableIndex::new(vec![orders, customers, items, tags])
    }

    fn run(sql: &str) -> Validation {
        validate(
            &ValidateIn {
                sql: sql.into(),
                dialects: vec![],
                definitions: vec![Definition { name: "revenue".into(), expression: "SUM(amount)".into() }],
            },
            &index(),
            None,
        )
    }

    fn kinds(v: &Validation) -> Vec<&str> {
        v.findings.iter().map(|f| f.kind.as_str()).collect()
    }

    #[test]
    fn a_known_join_passes() {
        let v = run(
            "SELECT c.region, SUM(o.amount) AS revenue FROM retail.orders o JOIN retail.customers c ON o.customer_id = c.id GROUP BY 1",
        );
        assert!(v.valid && v.findings.is_empty(), "{:?}", v.findings);
        assert!(v.joins[0].known);
    }

    #[test]
    fn wrong_joins_unknown_paths_and_columns() {
        let v = run("SELECT * FROM retail.orders o JOIN retail.customers c ON o.id = c.id");
        assert_eq!(kinds(&v), vec!["join_contradicts_keys"]);
        assert!(!v.valid);
        assert_eq!(v.findings[0].suggestion.as_deref(), Some("JOIN ON orders.customer_id = customers.id"));
        let u = run(
            "SELECT o.nope, o.legacy_code FROM retail.orders o JOIN retail.order_items i ON i.order_id = o.id JOIN nowhere n ON n.x = o.id",
        );
        assert!(kinds(&u).contains(&"unknown_column") && kinds(&u).contains(&"deprecated_column") && kinds(&u).contains(&"unknown_table"));
        assert!(kinds(&u).contains(&"unknown_join_path"));
        let s = run("SELEC x");
        assert_eq!(kinds(&s), vec!["syntax_error"]);
    }

    #[test]
    fn fan_out_and_definitions() {
        let m2m = run("SELECT * FROM retail.order_items i JOIN retail.tags t ON i.order_id = t.order_id");
        assert!(kinds(&m2m).contains(&"fan_out"));
        let dup = run("SELECT SUM(o.amount) AS total FROM retail.orders o JOIN retail.order_items i ON i.order_id = o.id");
        assert!(dup.findings.iter().any(|f| f.kind == "fan_out" && f.message.contains("repeats")), "{:?}", dup.findings);
        let def = run("SELECT AVG(o.amount) AS revenue FROM retail.orders o");
        assert_eq!(kinds(&def), vec!["definition_mismatch"]);
        assert!(def.valid, "a warning does not make the query invalid");
    }
}
