// SPDX-License-Identifier: Apache-2.0
//! The catalog snapshot Cairn pushes, the query log, and the ontology the compiler builds.
//!
//! Shapes follow NVIDIA auto-ontology's catalog (`auto_ontology/catalog/model`) and semantic layer
//! (`auto_ontology/semantic/models.py`): Terms own ColumnAttributes, SqlAttributes are named SQL over a Term's
//! tables, and foreign keys (declared, inferred or observed in queries) become relationships with provenance.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// One column of a catalog table, with what profiling knows about its values.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Column {
    pub name: String,
    #[serde(default)]
    pub data_type: String,
    /// `true`/`false`, or the information_schema spelling `YES`/`NO` (normalised on ingest).
    #[serde(default, deserialize_with = "crate::catalog::de_nullable")]
    pub nullable: Option<bool>,
    #[serde(default)]
    pub ordinal: Option<i64>,
    #[serde(default)]
    pub description: Option<String>,
    /// Values sampled from the column (up to 1000 rows), in the warehouse's own types.
    #[serde(default)]
    pub samples: Vec<Value>,
    /// The column's complete distinct value set, when it is small (a categorical column).
    #[serde(default)]
    pub distinct_values: Option<Vec<Value>>,
    /// Known uniqueness (from profiling); otherwise inferred from the samples.
    #[serde(default)]
    pub is_unique: Option<bool>,
    #[serde(default)]
    pub null_count: Option<i64>,
    #[serde(default)]
    pub deprecated: bool,
    #[serde(default)]
    pub tags: Vec<String>,
}

/// A declared foreign key: `columns` of this table reference `target_columns` of `target_table`
/// (a table id, or a `schema.table` / `catalog.schema.table` name).
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct ForeignKey {
    pub columns: Vec<String>,
    pub target_table: String,
    #[serde(default)]
    pub target_columns: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Table {
    /// Cairn's identifier for the table (its dataset URN): every ontology element refers to tables by it.
    pub id: String,
    #[serde(default)]
    pub catalog: String,
    #[serde(default)]
    pub schema: String,
    pub name: String,
    #[serde(default)]
    pub table_type: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub row_count: Option<i64>,
    #[serde(default)]
    pub primary_key: Vec<String>,
    #[serde(default)]
    pub foreign_keys: Vec<ForeignKey>,
    #[serde(default)]
    pub deprecated: bool,
    #[serde(default)]
    pub columns: Vec<Column>,
}

impl Table {
    pub fn column(&self, name: &str) -> Option<&Column> {
        self.columns.iter().find(|c| c.name.eq_ignore_ascii_case(name))
    }

    /// `schema.table` (or the bare name), as SQL most often names it.
    pub fn qualified(&self) -> String {
        if self.schema.is_empty() {
            self.name.clone()
        } else {
            format!("{}.{}", self.schema, self.name)
        }
    }

    pub fn full_name(&self) -> String {
        [self.catalog.as_str(), self.schema.as_str(), self.name.as_str()]
            .iter()
            .filter(|p| !p.is_empty())
            .cloned()
            .collect::<Vec<_>>()
            .join(".")
    }

    pub fn is_pk(&self, column: &str) -> bool {
        self.primary_key.iter().any(|c| c.eq_ignore_ascii_case(column))
    }
}

/// The table allow/deny patterns (`auto_ontology/catalog/table_filter.py`).
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct FilterSpec {
    #[serde(default)]
    pub table_allow_regex: Option<String>,
    #[serde(default)]
    pub table_deny_regex: Option<String>,
}

/// `PUT /v1/catalog`.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct CatalogSnapshot {
    #[serde(default)]
    pub database: Option<String>,
    pub tables: Vec<Table>,
    /// true: the snapshot is the whole catalog (tables missing from it are removed); false: tables are upserted.
    #[serde(default = "yes")]
    pub replace: bool,
    #[serde(default)]
    pub filter: Option<FilterSpec>,
}

fn yes() -> bool {
    true
}

/// One query from the log.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct QueryIn {
    #[serde(default)]
    pub id: Option<String>,
    pub sql: String,
    #[serde(default = "one")]
    pub count: i64,
    #[serde(default)]
    pub user: Option<String>,
    #[serde(default)]
    pub ts_ms: Option<i64>,
}

fn one() -> i64 {
    1
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct QueryBatch {
    pub queries: Vec<QueryIn>,
    #[serde(default)]
    pub dialects: Vec<String>,
}

/// Where an ontology element came from.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Provenance {
    /// `catalog` (declared metadata), `catalog_fk` (a declared foreign key), `inferred` (name, type and
    /// value heuristics), `model` (a model's judgement), `observed_in_queries` (the query log).
    pub source: String,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub confidence: f64,
    #[serde(default)]
    pub run_id: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Term {
    pub id: String,
    pub name: String,
    pub description: String,
    #[serde(default)]
    pub synonyms: Vec<String>,
    pub table_id: String,
    pub table_name: String,
    #[serde(default)]
    pub attribute_ids: Vec<String>,
    /// The name the model proposed, before a collision with another table's Term qualified it.
    #[serde(default)]
    pub proposed_name: String,
    #[serde(default)]
    pub provenance: Vec<Provenance>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embedding: Option<Vec<f32>>,
}

/// A ColumnAttribute: one column of a Term's table, with its business label.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Attribute {
    pub id: String,
    pub term_id: String,
    pub term_name: String,
    pub table_id: String,
    pub column: String,
    /// The user-friendly label (`Order Date`).
    pub name: String,
    pub datatype: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub is_unique: Option<bool>,
    /// How stored values are written, when one notation fits them all (`YYYY-MM-DD`).
    #[serde(default)]
    pub format: Option<String>,
    #[serde(default)]
    pub sample_values: Vec<Value>,
    #[serde(default)]
    pub deprecated: bool,
    #[serde(default)]
    pub provenance: Vec<Provenance>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embedding: Option<Vec<f32>>,
}

/// Evidence for a relationship from the query log.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct JoinEvidence {
    pub queries: i64,
    pub executions: i64,
    #[serde(default)]
    pub query_ids: Vec<String>,
}

/// A join path: `source_table.source_column` references `target_table.target_column`.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Relationship {
    pub id: String,
    pub source_table: String,
    pub source_column: String,
    pub target_table: String,
    pub target_column: String,
    /// `many_to_one` (the target side is a key), or `unknown` when neither side is known to be unique.
    pub cardinality: String,
    pub confidence: f64,
    /// The default among several relationships from one table to the same target column.
    #[serde(default = "yes")]
    pub join_default: bool,
    #[serde(default)]
    pub provenance: Vec<Provenance>,
    #[serde(default)]
    pub evidence: JoinEvidence,
    #[serde(default)]
    pub rationale: String,
}

/// A SqlAttribute: a named, reusable SQL filter, aggregation or join pattern on a Term (a metric).
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Metric {
    pub id: String,
    pub term_id: String,
    pub term_name: String,
    pub name: String,
    pub description: String,
    /// The extracted expression the model chose (`sum(o.amount)`).
    pub expression: String,
    /// A complete SELECT that uses it.
    pub sql: String,
    /// `sql` (from query history) or `bridgeTable`.
    pub source: String,
    #[serde(default)]
    pub tables: Vec<String>,
    #[serde(default)]
    pub source_query_ids: Vec<String>,
    #[serde(default)]
    pub usage: f64,
    #[serde(default)]
    pub provenance: Vec<Provenance>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct FkPair {
    pub source_column: String,
    pub target_table: String,
    pub target_column: String,
}

/// A pure-FK junction table and the join pattern through it.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Bridge {
    pub id: String,
    pub table_id: String,
    pub table_name: String,
    pub fk_pairs: Vec<FkPair>,
    #[serde(default)]
    pub metric_id: Option<String>,
    #[serde(default)]
    pub provenance: Vec<Provenance>,
}

/// Per-column facts the run inferred (uniqueness, date notation, samples kept for prompts).
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct ColumnProfile {
    pub is_unique: Option<bool>,
    pub format: Option<String>,
    pub sample_values: Vec<Value>,
    pub top_values: Vec<Value>,
}

/// What the run concluded about one table.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct TableFacts {
    pub table_id: String,
    pub is_junction: bool,
    #[serde(default)]
    pub junction_rationale: String,
    /// Columns the model (or the uuid rule) suspects are foreign keys, with why.
    #[serde(default)]
    pub suspected_fks: BTreeMap<String, String>,
    #[serde(default)]
    pub profiles: BTreeMap<String, ColumnProfile>,
    /// The table's shape and content fingerprint the results were built from (incremental runs reuse them).
    #[serde(default)]
    pub fingerprint: String,
    #[serde(default)]
    pub usage: Usage,
}

/// How much the query log reads a table.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Usage {
    pub queries: i64,
    pub executions: i64,
    #[serde(default)]
    pub columns: BTreeMap<String, i64>,
}

/// Everything one table contributes, committed in canonical table order.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct TableResult {
    pub facts: TableFacts,
    pub terms: Vec<Term>,
    pub attributes: Vec<Attribute>,
}

/// The compiled ontology (one run's output).
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Ontology {
    pub run_id: String,
    pub version: i64,
    pub terms: Vec<Term>,
    pub attributes: Vec<Attribute>,
    pub relationships: Vec<Relationship>,
    pub metrics: Vec<Metric>,
    pub bridges: Vec<Bridge>,
    pub tables: Vec<TableFacts>,
}

impl Ontology {
    pub fn strip_embeddings(mut self) -> Self {
        for t in &mut self.terms {
            t.embedding = None;
        }
        for a in &mut self.attributes {
            a.embedding = None;
        }
        self
    }
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

pub fn new_id(prefix: &str) -> String {
    format!("{prefix}_{}", &uuid::Uuid::new_v4().simple().to_string()[..16])
}

/// A stable id from parts (sha256, 24 hex), so a re-run names the same element the same way.
pub fn stable_id(prefix: &str, parts: &[&str]) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    for p in parts {
        h.update(p.as_bytes());
        h.update([0u8]);
    }
    format!("{prefix}_{}", &hex::encode(h.finalize())[..24])
}
