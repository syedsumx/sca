// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/catalog/parsers/sqlglot_extractor.py,
// auto_ontology/catalog/parsers/query_comparator.py and the SQL handling of
// auto_ontology/semantic/sql_attribute_suggester.py.
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust on the `sqlparser` crate instead of sqlglot; scope resolution replaces sqlglot's
// `qualify` pass.
//! SQL understanding: the tables, columns, joins and set-operation pairs a query uses, its structural
//! fingerprint, the reusable expressions in it (filters, aggregations, join conditions, set-operation branches),
//! and turning one of those expressions back into a complete SELECT.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::ops::ControlFlow;

use serde::Serialize;
use sqlparser::ast::{
    BinaryOperator, Expr, Ident, ObjectName, ObjectNamePart, Query, Select, SelectItem, SetExpr, Statement, TableFactor, TableWithJoins,
    Value, Visit, VisitMut, Visitor, VisitorMut,
};
use sqlparser::dialect::{dialect_from_str, Dialect, GenericDialect};
use sqlparser::parser::Parser;

/// The dialects tried when the caller names none; Trino SQL parses as the generic dialect.
pub const DEFAULT_DIALECTS: &[&str] = &["generic", "postgres", "mysql", "ansi"];

#[derive(Debug, Clone, PartialEq)]
pub struct SqlSyntaxError(pub String);

impl std::fmt::Display for SqlSyntaxError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

fn dialect(name: &str) -> Box<dyn Dialect> {
    match name.to_lowercase().as_str() {
        "trino" | "presto" | "athena" | "" => Box::new(GenericDialect {}),
        other => dialect_from_str(other).unwrap_or_else(|| Box::new(GenericDialect {})),
    }
}

/// Parse with each dialect in turn; the first that parses wins (`extract_tables_and_columns`).
pub fn parse(sql: &str, dialects: &[String]) -> Result<(Statement, String), SqlSyntaxError> {
    let names: Vec<String> = if dialects.is_empty() { DEFAULT_DIALECTS.iter().map(|s| s.to_string()).collect() } else { dialects.to_vec() };
    if sql.trim().is_empty() {
        return Err(SqlSyntaxError("empty SQL".into()));
    }
    let mut last = String::new();
    for name in &names {
        let d = dialect(name);
        match Parser::parse_sql(d.as_ref(), sql) {
            Ok(mut stmts) if !stmts.is_empty() => return Ok((stmts.swap_remove(0), name.clone())),
            Ok(_) => last = "no statement".into(),
            Err(e) => last = e.to_string(),
        }
    }
    Err(SqlSyntaxError(format!(
        "Invalid query syntax or unsupported dialect. Supported dialects: {}. Last parser error: {last}",
        names.join(",")
    )))
}

/// The query a statement reads with (a SELECT, or the SELECT behind INSERT … SELECT / CREATE … AS SELECT).
pub fn statement_query(stmt: &Statement) -> Option<&Query> {
    match stmt {
        Statement::Query(q) => Some(q),
        Statement::Insert(i) => i.source.as_deref(),
        Statement::CreateTable(c) => c.query.as_deref(),
        Statement::CreateView { query, .. } => Some(query),
        _ => None,
    }
}

// ------------------------------------------------------------------------------------------------ extraction

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct JoinPair {
    pub left_table: String,
    pub left_column: String,
    pub right_table: String,
    pub right_column: String,
    pub operator: String,
    /// From a WHERE clause (`FROM a, b WHERE a.x = b.y`) rather than JOIN … ON / USING.
    pub implicit: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
pub struct UnionPair {
    pub left_table: String,
    pub left_column: String,
    pub right_table: String,
    pub right_column: String,
}

/// An aggregate call in a SELECT list, with the columns it reads.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Aggregate {
    pub function: String,
    pub text: String,
    pub alias: Option<String>,
    pub columns: Vec<(String, String)>,
    /// Tables joined in the SELECT the aggregate is computed in.
    pub scope_tables: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Extraction {
    /// Real tables (lower-case `schema.table`, or the bare name when the SQL gives none) → the columns read.
    pub tables: BTreeMap<String, BTreeSet<String>>,
    /// Every real table referenced, columns attributed or not.
    pub referenced: BTreeSet<String>,
    pub ast_node_count: usize,
    pub joins: Vec<JoinPair>,
    pub unions: Vec<UnionPair>,
    pub aggregates: Vec<Aggregate>,
    pub dialect_used: String,
    /// Bare column names no scope could attribute (ambiguous or unknown).
    pub unresolved: BTreeSet<String>,
}

/// The columns of a table, for schema-assisted resolution of bare column names. `None`: unknown table.
pub trait SchemaLookup {
    fn columns(&self, table_key: &str) -> Option<Vec<String>>;
}

#[cfg(test)]
pub struct NoSchema;

#[cfg(test)]
impl SchemaLookup for NoSchema {
    fn columns(&self, _: &str) -> Option<Vec<String>> {
        None
    }
}

const AGGREGATES: &[&str] = &[
    "sum",
    "count",
    "avg",
    "min",
    "max",
    "count_if",
    "approx_distinct",
    "array_agg",
    "string_agg",
    "listagg",
    "stddev",
    "stddev_pop",
    "stddev_samp",
    "variance",
    "var_pop",
    "var_samp",
    "median",
    "percentile_cont",
    "percentile_disc",
    "approx_percentile",
    "bool_and",
    "bool_or",
    "every",
    "any_value",
    "arbitrary",
    "max_by",
    "min_by",
    "group_concat",
    "corr",
    "covar_pop",
    "covar_samp",
    "first_value",
    "last_value",
];

pub fn is_aggregate_name(name: &str) -> bool {
    AGGREGATES.contains(&name.to_lowercase().as_str())
}

fn idents(name: &ObjectName) -> Vec<String> {
    name.0.iter().filter_map(ObjectNamePart::as_ident).map(|i| i.value.clone()).collect()
}

/// `schema.table` (or `table`), lower-case, as the extractor keys tables (`_qualified_table_name`: db and name).
fn table_key(name: &ObjectName) -> String {
    let parts = idents(name);
    let n = parts.len();
    parts[n.saturating_sub(2)..].iter().map(|p| p.to_lowercase()).collect::<Vec<_>>().join(".")
}

fn function_name(f: &sqlparser::ast::Function) -> String {
    idents(&f.name).last().cloned().unwrap_or_default().to_lowercase()
}

/// Output column → (real table, column) of a derived table or CTE.
type Passthrough = HashMap<String, (String, String)>;

/// What one SELECT's FROM clause brings into scope.
#[derive(Default, Clone)]
struct Scope {
    /// alias or bare table name → real table key.
    aliases: HashMap<String, String>,
    /// alias of a derived table or CTE → its pass-through output columns → (real table, column).
    derived: HashMap<String, HashMap<String, (String, String)>>,
    /// Real tables in FROM … JOIN order.
    chain: Vec<String>,
    /// Aliases the SELECT list defines (not columns).
    output_aliases: BTreeSet<String>,
}

struct Walker<'a> {
    schema: &'a dyn SchemaLookup,
    out: Extraction,
    ctes: Vec<HashMap<String, Passthrough>>,
    join_keys: HashMap<String, BTreeSet<String>>,
    seen_joins: BTreeSet<(String, String, String, String)>,
    seen_unions: BTreeSet<(String, String, String, String)>,
}

/// Collects column references of an expression, not descending into subqueries (they are walked in their
/// own scope), and the subqueries themselves.
#[derive(Default)]
struct ExprRefs {
    depth: usize,
    columns: Vec<Vec<Ident>>,
    subqueries: Vec<Query>,
    aggregates: Vec<sqlparser::ast::Function>,
}

impl Visitor for ExprRefs {
    type Break = ();
    fn pre_visit_query(&mut self, q: &Query) -> ControlFlow<()> {
        if self.depth == 0 {
            self.subqueries.push(q.clone());
        }
        self.depth += 1;
        ControlFlow::Continue(())
    }
    fn post_visit_query(&mut self, _: &Query) -> ControlFlow<()> {
        self.depth -= 1;
        ControlFlow::Continue(())
    }
    fn pre_visit_expr(&mut self, e: &Expr) -> ControlFlow<()> {
        if self.depth > 0 {
            return ControlFlow::Continue(());
        }
        match e {
            Expr::Identifier(i) => self.columns.push(vec![i.clone()]),
            Expr::CompoundIdentifier(parts) => self.columns.push(parts.clone()),
            Expr::Function(f) if is_aggregate_name(&function_name(f)) && f.over.is_none() => self.aggregates.push(f.clone()),
            _ => {}
        }
        ControlFlow::Continue(())
    }
}

fn refs_of<V: Visit>(node: &V) -> ExprRefs {
    let mut r = ExprRefs::default();
    let _ = node.visit(&mut r);
    r
}

/// The single column inside an expression (functions and casts unwrapped), or None when there are zero or
/// several (`_unwrap_column`).
fn unwrap_column(e: &Expr) -> Option<Vec<Ident>> {
    let r = refs_of(e);
    if r.columns.len() == 1 && r.subqueries.is_empty() {
        r.columns.into_iter().next()
    } else {
        None
    }
}

fn op_symbol(op: &BinaryOperator) -> Option<&'static str> {
    Some(match op {
        BinaryOperator::Eq => "=",
        BinaryOperator::Gt => ">",
        BinaryOperator::GtEq => ">=",
        BinaryOperator::Lt => "<",
        BinaryOperator::LtEq => "<=",
        BinaryOperator::NotEq => "!=",
        _ => return None,
    })
}

impl<'a> Walker<'a> {
    fn is_cte(&self, name: &str) -> Option<HashMap<String, (String, String)>> {
        self.ctes.iter().rev().find_map(|m| m.get(name).cloned())
    }

    /// Walks a query; returns its output columns' pass-through sources (for derived tables and CTEs).
    fn query(&mut self, q: &Query, parents: &[Scope]) -> HashMap<String, (String, String)> {
        let mut frame = HashMap::new();
        if let Some(with) = &q.with {
            self.ctes.push(HashMap::new());
            for cte in &with.cte_tables {
                let out = self.query(&cte.query, parents);
                frame.insert(cte.alias.name.value.to_lowercase(), out.clone());
                if let Some(last) = self.ctes.last_mut() {
                    last.insert(cte.alias.name.value.to_lowercase(), out);
                }
            }
        }
        let out = self.set_expr(&q.body, parents, true);
        if q.with.is_some() {
            self.ctes.pop();
        }
        out
    }

    fn set_expr(&mut self, s: &SetExpr, parents: &[Scope], top: bool) -> HashMap<String, (String, String)> {
        match s {
            SetExpr::Select(sel) => self.select(sel, parents),
            SetExpr::Query(q) => self.query(q, parents),
            SetExpr::SetOperation { left, right, .. } => {
                let l = self.set_expr(left, parents, false);
                self.set_expr(right, parents, false);
                if top {
                    self.union_pairs(s, parents);
                }
                l
            }
            _ => HashMap::new(),
        }
    }

    fn add_from(&mut self, twj: &TableWithJoins, scope: &mut Scope, parents: &[Scope]) {
        self.add_factor(&twj.relation, scope, parents);
        for j in &twj.joins {
            self.add_factor(&j.relation, scope, parents);
        }
    }

    fn add_factor(&mut self, f: &TableFactor, scope: &mut Scope, parents: &[Scope]) {
        match f {
            TableFactor::Table { name, alias, .. } => {
                let parts = idents(name);
                let bare = parts.last().cloned().unwrap_or_default().to_lowercase();
                if parts.len() == 1 {
                    if let Some(cols) = self.is_cte(&bare) {
                        let a = alias.as_ref().map(|a| a.name.value.to_lowercase()).unwrap_or(bare.clone());
                        scope.derived.insert(a, cols.clone());
                        scope.derived.insert(bare, cols);
                        return;
                    }
                }
                let key = table_key(name);
                self.out.referenced.insert(key.clone());
                let a = alias.as_ref().map(|a| a.name.value.to_lowercase()).unwrap_or(bare.clone());
                scope.aliases.insert(a, key.clone());
                scope.aliases.entry(bare).or_insert(key.clone());
                scope.aliases.entry(key.clone()).or_insert(key.clone());
                scope.chain.push(key);
            }
            TableFactor::Derived { subquery, alias, .. } => {
                let cols = self.query(subquery, parents);
                if let Some(a) = alias {
                    scope.derived.insert(a.name.value.to_lowercase(), cols);
                }
            }
            TableFactor::NestedJoin { table_with_joins, .. } => self.add_from(table_with_joins, scope, parents),
            _ => {}
        }
    }

    /// (real table, column) for a reference, through this scope's aliases and derived tables, then the
    /// enclosing scopes (correlated subqueries). Bare names fall back to the catalog's columns.
    fn resolve(&self, parts: &[Ident], scope: &Scope, parents: &[Scope]) -> Option<(String, String)> {
        let col = parts.last()?.value.to_lowercase();
        if parts.len() == 1 {
            return self.resolve_bare(&col, scope, parents);
        }
        let qual = parts[..parts.len() - 1].iter().map(|i| i.value.to_lowercase()).collect::<Vec<_>>();
        let q_last = qual.last().cloned().unwrap_or_default();
        let q_two = qual[qual.len().saturating_sub(2)..].join(".");
        for s in std::iter::once(scope).chain(parents.iter().rev()) {
            for q in [&q_two, &q_last] {
                if let Some(t) = s.aliases.get(q.as_str()) {
                    return Some((t.clone(), col));
                }
                if let Some(d) = s.derived.get(q.as_str()) {
                    return d.get(&col).cloned();
                }
            }
        }
        None
    }

    fn resolve_bare(&self, col: &str, scope: &Scope, parents: &[Scope]) -> Option<(String, String)> {
        for s in std::iter::once(scope).chain(parents.iter().rev()) {
            let real: Vec<&String> = {
                let mut v: Vec<&String> = s.chain.iter().collect();
                v.dedup();
                v
            };
            // What the catalog says each table in scope has.
            let mut known = 0;
            let mut having: Vec<String> = vec![];
            for t in &real {
                if let Some(cols) = self.schema.columns(t) {
                    known += 1;
                    if cols.iter().any(|c| c.eq_ignore_ascii_case(col)) {
                        having.push((*t).clone());
                    }
                }
            }
            for d in s.derived.values() {
                if let Some(src) = d.get(col) {
                    if real.is_empty() || s.derived.len() == 1 {
                        return Some(src.clone());
                    }
                }
            }
            if having.len() == 1 {
                return Some((having[0].clone(), col.to_string()));
            }
            if having.len() > 1 {
                if let Some(keys) = self.join_keys.get(col) {
                    if let Some(t) = having.iter().find(|t| keys.contains(*t)) {
                        return Some((t.clone(), col.to_string()));
                    }
                }
                return None; // ambiguous: omit rather than guess
            }
            if known == 0 && real.len() == 1 && s.derived.is_empty() {
                // One source and no catalog to ask: the column is that table's (what `qualify` does).
                return Some((real[0].clone(), col.to_string()));
            }
            if known > 0 && known == real.len() && !real.is_empty() {
                return None; // the catalog knows every table here and none has it: maybe an outer reference
            }
        }
        None
    }

    fn record(&mut self, parts: &[Ident], scope: &Scope, parents: &[Scope]) {
        if parts.len() == 1 && scope.output_aliases.contains(&parts[0].value.to_lowercase()) {
            return;
        }
        match self.resolve(parts, scope, parents) {
            Some((t, c)) => {
                if self.out.referenced.contains(&t) {
                    self.out.tables.entry(t).or_default().insert(c);
                }
            }
            None => {
                if parts.len() == 1 {
                    self.out.unresolved.insert(parts[0].value.to_lowercase());
                }
            }
        }
    }

    fn expr_columns(&mut self, e: &Expr, scope: &Scope, parents: &[Scope]) {
        let r = refs_of(e);
        for parts in &r.columns {
            self.record(parts, scope, parents);
        }
        let mut chain = parents.to_vec();
        chain.push(scope.clone());
        for q in &r.subqueries {
            self.query(q, &chain);
        }
    }

    fn add_join(&mut self, l: (String, String), r: (String, String), op: &str, implicit: bool) {
        let (mut lt, mut lc, mut rt, mut rc) = (l.0, l.1, r.0, r.1);
        if lt == rt && lc == rc {
            return;
        }
        if lt == rt && implicit {
            return; // a filter between two columns of one table, not a join
        }
        if (lt.clone(), lc.clone()) > (rt.clone(), rc.clone()) {
            std::mem::swap(&mut lt, &mut rt);
            std::mem::swap(&mut lc, &mut rc);
        }
        if self.seen_joins.insert((lt.clone(), lc.clone(), rt.clone(), rc.clone())) {
            self.out.joins.push(JoinPair {
                left_table: lt,
                left_column: lc,
                right_table: rt,
                right_column: rc,
                operator: op.into(),
                implicit,
            });
        }
    }

    fn on_condition(&mut self, on: &Expr, scope: &Scope, parents: &[Scope], implicit: bool) {
        struct Cmp(Vec<(Expr, Expr, &'static str)>);
        impl Visitor for Cmp {
            type Break = ();
            fn pre_visit_expr(&mut self, e: &Expr) -> ControlFlow<()> {
                match e {
                    Expr::BinaryOp { left, op, right } => {
                        if let Some(s) = op_symbol(op) {
                            self.0.push((*left.clone(), *right.clone(), s));
                        }
                    }
                    Expr::Between { expr, low, high, negated: false } => {
                        self.0.push((*expr.clone(), *low.clone(), "BETWEEN"));
                        self.0.push((*expr.clone(), *high.clone(), "BETWEEN"));
                    }
                    _ => {}
                }
                ControlFlow::Continue(())
            }
        }
        let mut c = Cmp(vec![]);
        let _ = on.visit(&mut c);
        for (l, r, op) in c.0 {
            if implicit && op != "=" {
                continue;
            }
            let (Some(lc), Some(rc)) = (unwrap_column(&l), unwrap_column(&r)) else { continue };
            let (Some(lr), Some(rr)) = (self.resolve(&lc, scope, parents), self.resolve(&rc, scope, parents)) else {
                continue;
            };
            if !self.out.referenced.contains(&lr.0) || !self.out.referenced.contains(&rr.0) {
                continue;
            }
            self.add_join(lr, rr, op, implicit);
        }
    }

    fn select(&mut self, sel: &Select, parents: &[Scope]) -> HashMap<String, (String, String)> {
        let mut scope = Scope::default();
        for twj in &sel.from {
            self.add_from(twj, &mut scope, parents);
        }
        for item in &sel.projection {
            if let SelectItem::ExprWithAlias { alias, .. } = item {
                scope.output_aliases.insert(alias.value.to_lowercase());
            }
        }
        // Join keys of USING and same-named ON equalities, before resolving bare names (`join_keys`).
        for twj in &sel.from {
            let mut chain: Vec<String> = vec![];
            if let TableFactor::Table { name, .. } = &twj.relation {
                chain.push(table_key(name));
            }
            for j in &twj.joins {
                let right = if let TableFactor::Table { name, .. } = &j.relation { Some(table_key(name)) } else { None };
                let mut keys: Vec<String> = vec![];
                if let Some(c) = join_constraint(&j.join_operator) {
                    match c {
                        sqlparser::ast::JoinConstraint::Using(cols) => {
                            keys.extend(cols.iter().filter_map(|c| idents(c).last().map(|s| s.to_lowercase())))
                        }
                        sqlparser::ast::JoinConstraint::On(e) => {
                            // Every `a.col = b.col` with the same name on both sides is USING (col).
                            for c in comparisons(e) {
                                let Expr::BinaryOp { left, op: BinaryOperator::Eq, right } = &c else { continue };
                                if let (Some(l), Some(r)) = (unwrap_column(left), unwrap_column(right)) {
                                    let (ln, rn) = (l.last().map(|i| i.value.to_lowercase()), r.last().map(|i| i.value.to_lowercase()));
                                    if ln.is_some() && ln == rn {
                                        keys.push(ln.unwrap_or_default());
                                    }
                                }
                            }
                        }
                        _ => {}
                    }
                }
                for k in keys {
                    let e = self.join_keys.entry(k).or_default();
                    e.extend(chain.iter().cloned());
                    if let Some(r) = &right {
                        e.insert(r.clone());
                    }
                }
                if let Some(r) = right {
                    chain.push(r);
                }
            }
        }
        // Join conditions.
        for twj in &sel.from {
            let mut chain: Vec<String> = vec![];
            if let TableFactor::Table { name, .. } = &twj.relation {
                chain.push(table_key(name));
            }
            for j in &twj.joins {
                let right = if let TableFactor::Table { name, .. } = &j.relation { Some(table_key(name)) } else { None };
                match join_constraint(&j.join_operator) {
                    Some(sqlparser::ast::JoinConstraint::On(e)) => {
                        self.on_condition(e, &scope, parents, false);
                        self.expr_columns(e, &scope, parents);
                    }
                    Some(sqlparser::ast::JoinConstraint::Using(cols)) => {
                        if let (Some(l), Some(r)) = (chain.last(), &right) {
                            for c in cols {
                                if let Some(name) = idents(c).last() {
                                    let n = name.to_lowercase();
                                    self.out.tables.entry(l.clone()).or_default().insert(n.clone());
                                    self.out.tables.entry(r.clone()).or_default().insert(n.clone());
                                    self.add_join((l.clone(), n.clone()), (r.clone(), n), "=", false);
                                }
                            }
                        }
                    }
                    _ => {}
                }
                if let Some(r) = right {
                    chain.push(r);
                }
            }
        }
        // Implicit joins in WHERE (comma joins).
        if let Some(w) = &sel.selection {
            if scope.chain.len() > 1 {
                self.on_condition(w, &scope, parents, true);
            }
        }
        // Every column the SELECT reads.
        let mut out = HashMap::new();
        for item in &sel.projection {
            match item {
                SelectItem::UnnamedExpr(e) | SelectItem::ExprWithAlias { expr: e, .. } => {
                    let r = refs_of(e);
                    for parts in &r.columns {
                        if let Some(src) = self.resolve(parts, &scope, parents) {
                            if self.out.referenced.contains(&src.0) {
                                self.out.tables.entry(src.0.clone()).or_default().insert(src.1.clone());
                            }
                        } else if parts.len() == 1 {
                            self.out.unresolved.insert(parts[0].value.to_lowercase());
                        }
                    }
                    let mut chain = parents.to_vec();
                    chain.push(scope.clone());
                    for q in &r.subqueries {
                        self.query(q, &chain);
                    }
                    let alias = if let SelectItem::ExprWithAlias { alias, .. } = item { Some(alias.value.clone()) } else { None };
                    for f in &r.aggregates {
                        let fr = refs_of(f);
                        let cols: Vec<(String, String)> = fr.columns.iter().filter_map(|p| self.resolve(p, &scope, parents)).collect();
                        self.out.aggregates.push(Aggregate {
                            function: function_name(f),
                            text: f.to_string(),
                            alias: alias.clone(),
                            columns: cols,
                            scope_tables: scope.chain.clone(),
                        });
                    }
                    // Pass-through output columns (bare references only; computed ones are not real columns).
                    let src_expr = match e {
                        Expr::Nested(inner) => inner.as_ref(),
                        other => other,
                    };
                    let parts = match src_expr {
                        Expr::Identifier(i) => Some(vec![i.clone()]),
                        Expr::CompoundIdentifier(p) => Some(p.clone()),
                        _ => None,
                    };
                    if let Some(parts) = parts {
                        let name = alias.clone().unwrap_or_else(|| parts.last().map(|i| i.value.clone()).unwrap_or_default());
                        if let Some(src) = self.resolve(&parts, &scope, parents) {
                            out.insert(name.to_lowercase(), src);
                        }
                    }
                }
                SelectItem::Wildcard(_) | SelectItem::QualifiedWildcard(..) => {
                    // SELECT * reads every column; the catalog says which (when it knows the tables).
                    for t in scope.chain.clone() {
                        if let Some(cols) = self.schema.columns(&t) {
                            for c in cols {
                                out.insert(c.to_lowercase(), (t.clone(), c.to_lowercase()));
                            }
                        }
                    }
                }
            }
        }
        if let Some(w) = &sel.selection {
            self.expr_columns(w, &scope, parents);
        }
        if let sqlparser::ast::GroupByExpr::Expressions(exprs, _) = &sel.group_by {
            for e in exprs {
                self.expr_columns(e, &scope, parents);
            }
        }
        if let Some(h) = &sel.having {
            self.expr_columns(h, &scope, parents);
        }
        if let Some(q) = &sel.qualify {
            self.expr_columns(q, &scope, parents);
        }
        out
    }

    /// Columns aligned at the same SELECT-list position across UNION/INTERSECT/EXCEPT branches
    /// (`_extract_union_pairs`).
    fn union_pairs(&mut self, s: &SetExpr, parents: &[Scope]) {
        fn flatten<'q>(s: &'q SetExpr, out: &mut Vec<&'q Select>) {
            match s {
                SetExpr::Select(sel) => out.push(sel),
                SetExpr::SetOperation { left, right, .. } => {
                    flatten(left, out);
                    flatten(right, out);
                }
                SetExpr::Query(q) => flatten(&q.body, out),
                _ => {}
            }
        }
        let mut branches = vec![];
        flatten(s, &mut branches);
        if branches.len() < 2 {
            return;
        }
        let mut resolved: Vec<Vec<Option<(String, String)>>> = vec![];
        for b in &branches {
            let mut scope = Scope::default();
            let saved = self.out.clone();
            for twj in &b.from {
                self.add_from(twj, &mut scope, parents);
            }
            self.out = saved;
            resolved.push(
                b.projection
                    .iter()
                    .map(|item| match item {
                        SelectItem::UnnamedExpr(Expr::CompoundIdentifier(p))
                        | SelectItem::ExprWithAlias { expr: Expr::CompoundIdentifier(p), .. } => self.resolve(p, &scope, parents),
                        SelectItem::UnnamedExpr(Expr::Identifier(i)) | SelectItem::ExprWithAlias { expr: Expr::Identifier(i), .. } => {
                            if scope.chain.len() == 1 {
                                self.resolve(std::slice::from_ref(i), &scope, parents)
                            } else {
                                None
                            }
                        }
                        _ => None,
                    })
                    .collect(),
            );
        }
        let n = resolved.iter().map(|b| b.len()).min().unwrap_or(0);
        for pos in 0..n {
            let at: Vec<(String, String)> = resolved.iter().filter_map(|b| b[pos].clone()).collect();
            for i in 0..at.len() {
                for j in i + 1..at.len() {
                    let (mut l, mut r) = (at[i].clone(), at[j].clone());
                    if l == r {
                        continue;
                    }
                    if l > r {
                        std::mem::swap(&mut l, &mut r);
                    }
                    if self.seen_unions.insert((l.0.clone(), l.1.clone(), r.0.clone(), r.1.clone())) {
                        self.out.unions.push(UnionPair { left_table: l.0, left_column: l.1, right_table: r.0, right_column: r.1 });
                    }
                }
            }
        }
    }
}

fn join_constraint(op: &sqlparser::ast::JoinOperator) -> Option<&sqlparser::ast::JoinConstraint> {
    use sqlparser::ast::JoinOperator as J;
    match op {
        J::Join(c)
        | J::Inner(c)
        | J::Left(c)
        | J::LeftOuter(c)
        | J::Right(c)
        | J::RightOuter(c)
        | J::FullOuter(c)
        | J::CrossJoin(c)
        | J::Semi(c)
        | J::LeftSemi(c)
        | J::RightSemi(c)
        | J::Anti(c)
        | J::LeftAnti(c)
        | J::RightAnti(c)
        | J::StraightJoin(c) => Some(c),
        J::AsOf { constraint, .. } => Some(constraint),
        _ => None,
    }
}

struct Count(usize);

impl Visitor for Count {
    type Break = ();
    fn pre_visit_expr(&mut self, _: &Expr) -> ControlFlow<()> {
        self.0 += 1;
        ControlFlow::Continue(())
    }
    fn pre_visit_relation(&mut self, _: &ObjectName) -> ControlFlow<()> {
        self.0 += 1;
        ControlFlow::Continue(())
    }
    fn pre_visit_query(&mut self, _: &Query) -> ControlFlow<()> {
        self.0 += 1;
        ControlFlow::Continue(())
    }
}

/// The tables, columns, joins and set-operation pairs `sql` uses (`extract_tables_and_columns`).
pub fn extract(sql: &str, dialects: &[String], schema: &dyn SchemaLookup) -> Result<Extraction, SqlSyntaxError> {
    let (stmt, used) = parse(sql, dialects)?;
    let mut count = Count(0);
    let _ = stmt.visit(&mut count);
    let mut w = Walker {
        schema,
        out: Extraction { ast_node_count: count.0, dialect_used: used, ..Default::default() },
        ctes: vec![],
        join_keys: HashMap::new(),
        seen_joins: BTreeSet::new(),
        seen_unions: BTreeSet::new(),
    };
    if let Some(q) = statement_query(&stmt) {
        w.query(q, &[]);
    }
    // Columns attributed only by SELECT * expansion still count as read.
    let mut out = w.out;
    out.tables.retain(|_, cols| !cols.is_empty());
    Ok(out)
}

// ------------------------------------------------------------------------------------------- normalisation

struct Normalize;

impl VisitorMut for Normalize {
    type Break = ();
    fn pre_visit_query(&mut self, q: &mut Query) -> ControlFlow<()> {
        fn sort(s: &mut SetExpr) {
            match s {
                SetExpr::Select(sel) => {
                    if sel.projection.len() > 1 {
                        sel.projection.sort_by_key(|e| e.to_string().to_lowercase());
                    }
                }
                SetExpr::SetOperation { left, right, .. } => {
                    sort(left);
                    sort(right);
                }
                _ => {}
            }
        }
        sort(&mut q.body);
        ControlFlow::Continue(())
    }
    fn pre_visit_expr(&mut self, e: &mut Expr) -> ControlFlow<()> {
        let placeholder = || {
            Expr::Value(sqlparser::ast::ValueWithSpan { value: Value::Placeholder("?".into()), span: sqlparser::tokenizer::Span::empty() })
        };
        match e {
            Expr::Value(_) => *e = placeholder(),
            // Double-quoted values ("15/12/2024") parse as quoted identifiers: unqualified ones are constants.
            Expr::Identifier(i) if i.quote_style == Some('"') => *e = placeholder(),
            Expr::InList { list, .. } if list.len() > 1 => list.truncate(1),
            _ => {}
        }
        ControlFlow::Continue(())
    }
}

/// A canonical form for structural equality (`normalize_sql`): literals replaced by placeholders (when
/// `ignore_literals`), each SELECT list sorted, lower-cased. None when the SQL does not parse.
pub fn normalize(sql: &str, dialects: &[String], ignore_literals: bool) -> Option<String> {
    let (mut stmt, _) = parse(sql, dialects).ok()?;
    if ignore_literals {
        let _ = VisitMut::visit(&mut stmt, &mut Normalize);
    } else {
        struct SortOnly;
        impl VisitorMut for SortOnly {
            type Break = ();
            fn pre_visit_query(&mut self, q: &mut Query) -> ControlFlow<()> {
                Normalize.pre_visit_query(q)
            }
        }
        let _ = VisitMut::visit(&mut stmt, &mut SortOnly);
    }
    Some(stmt.to_string().to_lowercase())
}

/// Whether two queries are structurally the same (`compare_queries`).
#[cfg(test)]
pub fn same_query(a: &str, b: &str, dialects: &[String], ignore_literals: bool) -> bool {
    match (normalize(a, dialects, ignore_literals), normalize(b, dialects, ignore_literals)) {
        (Some(x), Some(y)) => x == y,
        _ => false,
    }
}

// ------------------------------------------------------------------------ expressions for SqlAttributes

const BARE_KEYWORDS: &[&str] = &["select", "where", "from", "join", "on", "union", "intersect", "except", "having"];

fn is_comparison(e: &Expr) -> bool {
    matches!(
        e,
        Expr::BinaryOp {
            op: BinaryOperator::Eq
                | BinaryOperator::NotEq
                | BinaryOperator::Gt
                | BinaryOperator::GtEq
                | BinaryOperator::Lt
                | BinaryOperator::LtEq,
            ..
        } | Expr::InList { .. }
            | Expr::InSubquery { .. }
            | Expr::Between { .. }
            | Expr::Like { .. }
            | Expr::ILike { .. }
            | Expr::IsNull(_)
            | Expr::IsNotNull(_)
    )
}

fn is_pure_fk_eq(e: &Expr) -> bool {
    matches!(e, Expr::BinaryOp { left, op: BinaryOperator::Eq, right }
        if matches!(left.as_ref(), Expr::Identifier(_) | Expr::CompoundIdentifier(_))
        && matches!(right.as_ref(), Expr::Identifier(_) | Expr::CompoundIdentifier(_)))
}

struct Exprs {
    seen: BTreeSet<String>,
    out: Vec<String>,
}

impl Exprs {
    fn add_text(&mut self, text: String, bare: bool) {
        let key = text.trim().to_lowercase();
        if text.trim().len() < 5 || self.seen.contains(&key) || bare || BARE_KEYWORDS.contains(&key.as_str()) {
            return;
        }
        self.seen.insert(key);
        self.out.push(text.trim().to_string());
    }

    fn add(&mut self, e: &Expr) {
        let bare = matches!(e, Expr::Identifier(_) | Expr::CompoundIdentifier(_) | Expr::Value(_) | Expr::Wildcard(_));
        if is_pure_fk_eq(e) {
            return;
        }
        self.add_text(e.to_string(), bare);
    }
}

fn comparisons(e: &Expr) -> Vec<Expr> {
    struct C(Vec<Expr>, usize);
    impl Visitor for C {
        type Break = ();
        fn pre_visit_query(&mut self, _: &Query) -> ControlFlow<()> {
            self.1 += 1;
            ControlFlow::Continue(())
        }
        fn post_visit_query(&mut self, _: &Query) -> ControlFlow<()> {
            self.1 -= 1;
            ControlFlow::Continue(())
        }
        fn pre_visit_expr(&mut self, e: &Expr) -> ControlFlow<()> {
            if self.1 == 0 && is_comparison(e) {
                self.0.push(e.clone());
            }
            ControlFlow::Continue(())
        }
    }
    let mut c = C(vec![], 0);
    let _ = e.visit(&mut c);
    c.0
}

fn all_selects(stmt: &Statement) -> Vec<Select> {
    struct S(Vec<Select>);
    impl Visitor for S {
        type Break = ();
        fn pre_visit_query(&mut self, q: &Query) -> ControlFlow<()> {
            fn walk(s: &SetExpr, out: &mut Vec<Select>) {
                match s {
                    SetExpr::Select(sel) => out.push(*sel.clone()),
                    SetExpr::SetOperation { left, right, .. } => {
                        walk(left, out);
                        walk(right, out);
                    }
                    _ => {}
                }
            }
            walk(&q.body, &mut self.0);
            ControlFlow::Continue(())
        }
    }
    let mut s = S(vec![]);
    let _ = stmt.visit(&mut s);
    s.0
}

fn all_set_operations(stmt: &Statement) -> Vec<SetExpr> {
    struct S(Vec<SetExpr>);
    impl Visitor for S {
        type Break = ();
        fn pre_visit_query(&mut self, q: &Query) -> ControlFlow<()> {
            fn walk(s: &SetExpr, out: &mut Vec<SetExpr>) {
                if let SetExpr::SetOperation { left, right, .. } = s {
                    out.push(s.clone());
                    walk(left, out);
                    walk(right, out);
                }
            }
            walk(&q.body, &mut self.0);
            ControlFlow::Continue(())
        }
    }
    let mut s = S(vec![]);
    let _ = stmt.visit(&mut s);
    s.0
}

/// The reusable SQL sub-expressions of a query (`_extract_expressions`): WHERE predicates, aggregations and
/// CASE expressions, HAVING, JOIN ON conditions (whole and each comparison), and set operations (whole and each
/// branch). Bare columns, literals, `*`, keyword fragments and pure `col = col` equalities are left out.
pub fn extract_expressions(sql: &str) -> Vec<String> {
    let Ok((stmt, _)) = parse(sql, &[]) else { return vec![] };
    let mut x = Exprs { seen: BTreeSet::new(), out: vec![] };
    let selects = all_selects(&stmt);
    // WHERE predicates (of the first SELECT with one, as sqlglot's `find(Where)`).
    if let Some(w) = selects.iter().find_map(|s| s.selection.as_ref()) {
        for c in comparisons(w) {
            x.add(&c);
        }
    }
    // Aggregations and CASE anywhere.
    struct A(Vec<Expr>);
    impl Visitor for A {
        type Break = ();
        fn pre_visit_expr(&mut self, e: &Expr) -> ControlFlow<()> {
            match e {
                Expr::Function(f) if is_aggregate_name(&function_name(f)) => self.0.push(e.clone()),
                Expr::Case { .. } => self.0.push(e.clone()),
                _ => {}
            }
            ControlFlow::Continue(())
        }
    }
    let mut a = A(vec![]);
    let _ = stmt.visit(&mut a);
    for e in &a.0 {
        x.add(e);
    }
    if let Some(h) = selects.iter().find_map(|s| s.having.as_ref()) {
        x.add(h);
    }
    for sel in &selects {
        for twj in &sel.from {
            for j in &twj.joins {
                if let Some(sqlparser::ast::JoinConstraint::On(on)) = join_constraint(&j.join_operator) {
                    x.add(on);
                    for c in comparisons(on) {
                        x.add(&c);
                    }
                }
            }
        }
    }
    for op in all_set_operations(&stmt) {
        x.add_text(op.to_string(), false);
        if let SetExpr::SetOperation { left, right, .. } = &op {
            x.add_text(left.to_string(), false);
            x.add_text(right.to_string(), false);
        }
    }
    x.out
}

fn parse_expr(text: &str) -> Option<Expr> {
    let d = GenericDialect {};
    Parser::new(&d).try_with_sql(text).ok()?.parse_expr().ok()
}

fn is_agg_expr(e: &Expr) -> bool {
    matches!(e, Expr::Function(f) if is_aggregate_name(&function_name(f)))
}

/// Adds `schema.` to bare table references that `schema_map` (lower-case table → schema) knows
/// (`_qualify_table_names`). Unchanged when parsing fails.
pub fn qualify_tables(sql: &str, schema_map: &BTreeMap<String, String>) -> String {
    if schema_map.is_empty() {
        return sql.to_string();
    }
    let Ok((mut stmt, _)) = parse(sql, &[]) else { return sql.to_string() };
    let ctes: BTreeSet<String> = {
        struct C(BTreeSet<String>);
        impl Visitor for C {
            type Break = ();
            fn pre_visit_query(&mut self, q: &Query) -> ControlFlow<()> {
                if let Some(w) = &q.with {
                    for c in &w.cte_tables {
                        self.0.insert(c.alias.name.value.to_lowercase());
                    }
                }
                ControlFlow::Continue(())
            }
        }
        let mut c = C(BTreeSet::new());
        let _ = stmt.visit(&mut c);
        c.0
    };
    let _ = sqlparser::ast::visit_relations_mut(&mut stmt, |name: &mut ObjectName| {
        if name.0.len() == 1 {
            if let Some(i) = name.0[0].as_ident() {
                let bare = i.value.to_lowercase();
                if !ctes.contains(&bare) {
                    if let Some(schema) = schema_map.get(&bare) {
                        name.0.insert(0, ObjectNamePart::Identifier(Ident::new(schema.clone())));
                    }
                }
            }
        }
        ControlFlow::<()>::Continue(())
    });
    stmt.to_string()
}

fn try_wrap(expression: &str, expr: Option<&Expr>, source_sql: &str, is_agg: bool) -> Option<String> {
    let (stmt, _) = parse(source_sql, &[]).ok()?;
    let aliases: BTreeSet<String> = expr
        .map(|e| refs_of(e).columns.iter().filter(|p| p.len() > 1).map(|p| p[p.len() - 2].value.to_lowercase()).collect())
        .unwrap_or_default();
    let selects: Vec<Select> = all_selects(&stmt).into_iter().filter(|s| !s.from.is_empty()).collect();
    let names = |s: &Select| -> BTreeSet<String> {
        let mut out = BTreeSet::new();
        for twj in &s.from {
            for f in std::iter::once(&twj.relation).chain(twj.joins.iter().map(|j| &j.relation)) {
                if let TableFactor::Table { name, alias, .. } = f {
                    if let Some(a) = alias {
                        out.insert(a.name.value.to_lowercase());
                    }
                    if let Some(n) = idents(name).last() {
                        out.insert(n.to_lowercase());
                    }
                }
            }
        }
        out
    };
    let best = selects.iter().find(|s| aliases.is_empty() || aliases.is_subset(&names(s))).or_else(|| selects.first())?;
    let first = best.from.first()?;
    let joins: Vec<String> = if aliases.is_empty() {
        vec![]
    } else {
        first
            .joins
            .iter()
            .filter(|j| match &j.relation {
                TableFactor::Table { name, alias, .. } => {
                    let n = alias
                        .as_ref()
                        .map(|a| a.name.value.to_lowercase())
                        .or_else(|| idents(name).last().map(|s| s.to_lowercase()))
                        .unwrap_or_default();
                    aliases.contains(&n)
                }
                _ => false,
            })
            .map(|j| j.to_string())
            .collect()
    };
    let mut parts = vec!["SELECT".to_string(), if is_agg { expression.to_string() } else { "*".into() }];
    parts.push(format!("FROM {}", first.relation));
    if !joins.is_empty() {
        parts.push(joins.join(" "));
    }
    if !is_agg {
        parts.push(format!("WHERE {expression}"));
    }
    Some(parts.join(" "))
}

/// A complete SELECT that uses `expression` (`_build_valid_sql`): a full query as it is; a filter or join
/// condition as `SELECT * FROM … WHERE <expression>`; an aggregation as `SELECT <expression> FROM …`, the FROM
/// and JOINs taken from a source query that contains it. Table names are schema-qualified with `schema_map`.
pub fn build_valid_sql(expression: &str, source_sqls: &[String], schema_map: &BTreeMap<String, String>) -> Result<String, String> {
    if let Ok((stmt, _)) = parse(expression, &[]) {
        if matches!(stmt, Statement::Query(_)) {
            return Ok(qualify_tables(expression, schema_map));
        }
    }
    let expr = parse_expr(expression);
    let is_agg = expr.as_ref().is_some_and(is_agg_expr);
    let lower = expression.to_lowercase();
    for sql in source_sqls {
        let sl = sql.to_lowercase();
        if !lower.split_whitespace().any(|t| t.len() > 3 && sl.contains(t)) {
            continue;
        }
        if let Some(w) = try_wrap(expression, expr.as_ref(), sql, is_agg) {
            return Ok(qualify_tables(&w, schema_map));
        }
    }
    for sql in source_sqls {
        if let Some(w) = try_wrap(expression, expr.as_ref(), sql, is_agg) {
            return Ok(qualify_tables(&w, schema_map));
        }
    }
    Err(format!("Cannot build a valid SELECT for expression {expression:?}: no source SQL context available."))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Cat(BTreeMap<String, Vec<String>>);

    impl SchemaLookup for Cat {
        fn columns(&self, t: &str) -> Option<Vec<String>> {
            self.0.get(t).cloned().or_else(|| self.0.get(t.rsplit('.').next().unwrap_or(t)).cloned())
        }
    }

    fn cat() -> Cat {
        let mut m = BTreeMap::new();
        m.insert("orders".into(), vec!["id".into(), "customer_id".into(), "amount".into(), "status".into()]);
        m.insert("customers".into(), vec!["id".into(), "name".into(), "region".into()]);
        m.insert("order_items".into(), vec!["order_id".into(), "product_id".into(), "qty".into()]);
        Cat(m)
    }

    fn cols(x: &Extraction, t: &str) -> Vec<String> {
        x.tables.get(t).map(|s| s.iter().cloned().collect()).unwrap_or_default()
    }

    #[test]
    fn aliases_resolve_to_real_tables_and_joins_are_canonical() {
        let x = extract(
            "SELECT c.name, SUM(o.amount) AS revenue FROM orders o JOIN customers AS c ON o.customer_id = c.id \
             WHERE o.status = 'paid' GROUP BY c.name",
            &[],
            &cat(),
        )
        .unwrap();
        assert_eq!(cols(&x, "orders"), vec!["amount", "customer_id", "status"]);
        assert_eq!(cols(&x, "customers"), vec!["id", "name"]);
        assert_eq!(x.joins.len(), 1);
        let j = &x.joins[0];
        assert_eq!(
            (j.left_table.as_str(), j.left_column.as_str(), j.right_table.as_str(), j.right_column.as_str()),
            ("customers", "id", "orders", "customer_id")
        );
        assert_eq!(x.aggregates[0].function, "sum");
        assert_eq!(x.aggregates[0].alias.as_deref(), Some("revenue"));
        assert_eq!(x.aggregates[0].columns, vec![("orders".to_string(), "amount".to_string())]);
        assert!(x.ast_node_count > 10);
    }

    #[test]
    fn using_ctes_derived_tables_and_bare_columns() {
        let x = extract(
            "WITH big AS (SELECT o.customer_id, o.amount FROM retail.orders o WHERE o.amount > 100) \
             SELECT name, d.qty FROM big b JOIN customers c ON b.customer_id = c.id \
             JOIN (SELECT oi.order_id, oi.qty FROM order_items oi) d ON d.order_id = b.customer_id",
            &[],
            &cat(),
        )
        .unwrap();
        assert!(x.referenced.contains("retail.orders") && !x.referenced.contains("big"));
        assert_eq!(cols(&x, "customers"), vec!["id", "name"]);
        let pairs: Vec<_> =
            x.joins.iter().map(|j| format!("{}.{}={}.{}", j.left_table, j.left_column, j.right_table, j.right_column)).collect();
        assert!(pairs.contains(&"customers.id=retail.orders.customer_id".to_string()), "{pairs:?}");
        assert!(pairs.contains(&"order_items.order_id=retail.orders.customer_id".to_string()), "{pairs:?}");
        let u = extract("SELECT * FROM orders JOIN order_items USING (id)", &[], &cat()).unwrap();
        assert_eq!(u.joins[0].left_column, "id");
    }

    #[test]
    fn ambiguous_bare_columns_are_omitted_and_comma_joins_are_seen() {
        let x = extract("SELECT id FROM orders, customers WHERE orders.customer_id = customers.id", &[], &cat()).unwrap();
        assert!(x.unresolved.contains("id"));
        assert!(x.joins[0].implicit);
        assert!(extract("SELEC nope", &["generic".into()], &NoSchema).is_err());
    }

    #[test]
    fn union_branches_pair_columns_by_position() {
        let x = extract("SELECT o.customer_id FROM orders o UNION ALL SELECT c.id FROM customers c", &[], &cat()).unwrap();
        assert_eq!(x.unions.len(), 1);
        assert_eq!((x.unions[0].left_table.as_str(), x.unions[0].left_column.as_str()), ("customers", "id"));
    }

    #[test]
    fn normalisation_ignores_literals_and_select_order() {
        let d: Vec<String> = vec![];
        assert!(same_query(
            "SELECT id, name FROM users WHERE created > '2024-01-01'",
            "select name, id from users where created > '2025-06-15'",
            &d,
            true
        ));
        assert!(!same_query("SELECT id FROM users WHERE a = 1", "SELECT id FROM users WHERE b = 1", &d, true));
        assert!(!same_query("SELECT id FROM users WHERE a = 1", "SELECT id FROM users WHERE a = 2", &d, false));
        assert!(same_query("SELECT a FROM t WHERE x IN (1, 2, 3)", "SELECT a FROM t WHERE x IN (7)", &d, true));
    }

    #[test]
    fn expressions_are_the_reusable_parts() {
        let e = extract_expressions(
            "SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c ON o.customer_id = c.id AND c.active = true \
             WHERE o.status = 'paid' AND o.amount > 10 GROUP BY c.region HAVING SUM(o.amount) > 1000",
        );
        assert!(e.contains(&"o.status = 'paid'".to_string()), "{e:?}");
        assert!(e.contains(&"o.amount > 10".to_string()));
        assert!(e.contains(&"SUM(o.amount)".to_string()));
        assert!(e.contains(&"SUM(o.amount) > 1000".to_string()));
        assert!(e.contains(&"c.active = true".to_string()));
        assert!(!e.iter().any(|x| x == "o.customer_id = c.id"), "pure FK equality is structure, not logic");
    }

    #[test]
    fn an_expression_becomes_a_complete_select() {
        let mut schema = BTreeMap::new();
        schema.insert("orders".to_string(), "retail".to_string());
        let src = vec!["SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE o.status = 'paid'".to_string()];
        assert_eq!(
            build_valid_sql("o.status = 'paid'", &src, &schema).unwrap(),
            "SELECT * FROM retail.orders AS o WHERE o.status = 'paid'"
        );
        assert_eq!(build_valid_sql("SUM(o.amount)", &src, &schema).unwrap(), "SELECT SUM(o.amount) FROM retail.orders AS o");
        assert_eq!(
            build_valid_sql("c.region = 'EU'", &src, &BTreeMap::new()).unwrap(),
            "SELECT * FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE c.region = 'EU'"
        );
        assert_eq!(build_valid_sql("SELECT 1 FROM orders", &[], &schema).unwrap(), "SELECT 1 FROM retail.orders");
        assert!(build_valid_sql("x > 1", &[], &schema).is_err());
    }
}
