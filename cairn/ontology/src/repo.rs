// SPDX-License-Identifier: Apache-2.0
//! The compiler's records on top of [`Store`]: the catalog, the query log, runs, per-table results and the
//! ontology of each finished run.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::catalog::{content_fingerprint, shape_fingerprint};
use crate::model::{now_ms, Ontology, Table, TableResult};
use crate::store::{from_json, to_json, Result, Store, P};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct StoredQuery {
    pub fingerprint: String,
    pub id: String,
    pub sql: String,
    pub total_count: i64,
    /// Executions per month (`YYYY-MM`).
    pub months: BTreeMap<String, i64>,
    pub users: Vec<String>,
    pub first_ms: i64,
    pub last_ms: i64,
    pub dialect: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Run {
    pub id: String,
    pub version: i64,
    /// `running`, `completed`, `failed`, `cancelled` or `interrupted` (the process stopped mid-run).
    pub state: String,
    /// `full` or `incremental`.
    pub mode: String,
    pub started_ms: i64,
    pub finished_ms: Option<i64>,
    pub stats: Value,
    pub error: Option<String>,
    pub requested_by: String,
}

impl Run {
    pub fn resumable(&self) -> bool {
        matches!(self.state.as_str(), "cancelled" | "interrupted" | "failed")
    }
}

pub const PUBLISHED: &str = "published_run";

pub async fn tables(s: &Store) -> Result<Vec<Table>> {
    let rows = s.query("SELECT doc FROM onto_catalog ORDER BY id", vec![]).await?;
    rows.iter().map(|r| from_json(&r[0].text())).collect()
}

/// Upserts `upsert` and deletes `remove`, in one transaction.
pub async fn put_tables(s: &Store, upsert: &[Table], remove: &[String]) -> Result<()> {
    let now = now_ms();
    let mut stmts = vec![];
    for t in upsert {
        stmts.push((
            "INSERT INTO onto_catalog (id, doc, shape, content, updated_ms) VALUES ($1, $2, $3, $4, $5) \
             ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, shape = EXCLUDED.shape, content = EXCLUDED.content, updated_ms = EXCLUDED.updated_ms"
                .to_string(),
            vec![P::from(&t.id), P::from(to_json(t)), P::from(shape_fingerprint(t)), P::from(content_fingerprint(t)), P::I(now)],
        ));
    }
    for id in remove {
        stmts.push(("DELETE FROM onto_catalog WHERE id = $1".to_string(), vec![P::from(id)]));
    }
    s.transaction(stmts).await
}

pub async fn query_by_fingerprint(s: &Store, fp: &str) -> Result<Option<StoredQuery>> {
    let rows = s.query(&format!("{QUERY_SELECT} WHERE fingerprint = $1"), vec![fp.into()]).await?;
    rows.first().map(|r| row_query(r)).transpose()
}

const QUERY_SELECT: &str = "SELECT fingerprint, id, sql, total_count, months, users, first_ms, last_ms, dialect FROM onto_queries";

fn row_query(r: &[crate::store::V]) -> Result<StoredQuery> {
    Ok(StoredQuery {
        fingerprint: r[0].text(),
        id: r[1].text(),
        sql: r[2].text(),
        total_count: r[3].int().unwrap_or(0),
        months: from_json(&r[4].text())?,
        users: from_json(&r[5].text())?,
        first_ms: r[6].int().unwrap_or(0),
        last_ms: r[7].int().unwrap_or(0),
        dialect: r[8].text(),
    })
}

pub async fn put_queries(s: &Store, qs: &[StoredQuery]) -> Result<()> {
    let stmts = qs
        .iter()
        .map(|q| {
            (
                "INSERT INTO onto_queries (fingerprint, id, sql, total_count, months, users, first_ms, last_ms, dialect) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (fingerprint) DO UPDATE SET \
                 total_count = EXCLUDED.total_count, months = EXCLUDED.months, users = EXCLUDED.users, \
                 first_ms = EXCLUDED.first_ms, last_ms = EXCLUDED.last_ms"
                    .to_string(),
                vec![
                    P::from(&q.fingerprint),
                    P::from(&q.id),
                    P::from(&q.sql),
                    P::I(q.total_count),
                    P::from(to_json(&q.months)),
                    P::from(to_json(&q.users)),
                    P::I(q.first_ms),
                    P::I(q.last_ms),
                    P::from(&q.dialect),
                ],
            )
        })
        .collect();
    s.transaction(stmts).await
}

/// The most used queries (at most `limit`), most used first.
pub async fn queries(s: &Store, limit: i64) -> Result<Vec<StoredQuery>> {
    let rows = s.query(&format!("{QUERY_SELECT} ORDER BY total_count DESC, fingerprint LIMIT $1"), vec![P::I(limit)]).await?;
    rows.iter().map(|r| row_query(r)).collect()
}

pub async fn query_count(s: &Store) -> Result<i64> {
    Ok(s.query("SELECT COUNT(*) FROM onto_queries", vec![]).await?.first().and_then(|r| r[0].int()).unwrap_or(0))
}

const RUN_SELECT: &str = "SELECT id, version, state, mode, started_ms, finished_ms, stats, error, requested_by FROM onto_runs";

fn row_run(r: &[crate::store::V]) -> Result<Run> {
    Ok(Run {
        id: r[0].text(),
        version: r[1].int().unwrap_or(0),
        state: r[2].text(),
        mode: r[3].text(),
        started_ms: r[4].int().unwrap_or(0),
        finished_ms: r[5].int(),
        stats: from_json(&r[6].text()).unwrap_or(Value::Null),
        error: match &r[7] {
            crate::store::V::Null => None,
            v => Some(v.text()),
        },
        requested_by: r[8].text(),
    })
}

pub async fn create_run(s: &Store, id: &str, mode: &str, requested_by: &str) -> Result<Run> {
    let version =
        s.query("SELECT COALESCE(MAX(version), 0) FROM onto_runs", vec![]).await?.first().and_then(|r| r[0].int()).unwrap_or(0) + 1;
    s.exec(
        "INSERT INTO onto_runs (id, version, state, mode, started_ms, finished_ms, stats, error, requested_by) VALUES ($1, $2, 'running', $3, $4, NULL, '{}', NULL, $5)",
        vec![id.into(), P::I(version), mode.into(), P::I(now_ms()), requested_by.into()],
    )
    .await?;
    get_run(s, id).await.map(|r| r.expect("just created"))
}

pub async fn get_run(s: &Store, id: &str) -> Result<Option<Run>> {
    let rows = s.query(&format!("{RUN_SELECT} WHERE id = $1"), vec![id.into()]).await?;
    rows.first().map(|r| row_run(r)).transpose()
}

pub async fn runs(s: &Store, limit: i64) -> Result<Vec<Run>> {
    let rows = s.query(&format!("{RUN_SELECT} ORDER BY version DESC LIMIT $1"), vec![P::I(limit)]).await?;
    rows.iter().map(|r| row_run(r)).collect()
}

pub async fn set_run_state(s: &Store, id: &str, state: &str, stats: &Value, error: Option<&str>) -> Result<()> {
    let finished = if state == "running" { P::Null } else { P::I(now_ms()) };
    s.exec(
        "UPDATE onto_runs SET state = $2, stats = $3, error = $4, finished_ms = $5 WHERE id = $1",
        vec![id.into(), state.into(), P::from(to_json(stats)), error.map(str::to_string).into(), finished],
    )
    .await
    .map(|_| ())
}

/// Runs a stopped process left `running` are `interrupted`: resumable, never silently continued.
pub async fn mark_interrupted(s: &Store) -> Result<u64> {
    s.exec("UPDATE onto_runs SET state = 'interrupted', finished_ms = $1 WHERE state = 'running'", vec![P::I(now_ms())]).await
}

pub async fn put_table_result(s: &Store, run_id: &str, position: i64, r: &TableResult) -> Result<()> {
    s.exec(
        "INSERT INTO onto_table_results (run_id, table_id, position, fingerprint, doc) VALUES ($1, $2, $3, $4, $5) \
         ON CONFLICT (run_id, table_id) DO UPDATE SET position = EXCLUDED.position, fingerprint = EXCLUDED.fingerprint, doc = EXCLUDED.doc",
        vec![run_id.into(), P::from(&r.facts.table_id), P::I(position), P::from(&r.facts.fingerprint), P::from(to_json(r))],
    )
    .await
    .map(|_| ())
}

/// Forgets a run's per-table results, so resuming it compiles every table again.
pub async fn clear_table_results(s: &Store, run_id: &str) -> Result<()> {
    s.transaction(vec![("DELETE FROM onto_table_results WHERE run_id = $1".to_string(), vec![P::from(run_id)])]).await
}

pub async fn table_results(s: &Store, run_id: &str) -> Result<BTreeMap<String, TableResult>> {
    let rows = s.query("SELECT table_id, doc FROM onto_table_results WHERE run_id = $1 ORDER BY position", vec![run_id.into()]).await?;
    rows.iter().map(|r| Ok((r[0].text(), from_json(&r[1].text())?))).collect()
}

/// Stores a finished run's ontology (replacing anything stored for the run) and publishes it.
pub async fn put_ontology(s: &Store, o: &Ontology) -> Result<()> {
    let mut stmts = vec![("DELETE FROM onto_elements WHERE run_id = $1".to_string(), vec![P::from(&o.run_id)])];
    let mut push = |kind: &str, id: &str, doc: String| {
        stmts.push((
            "INSERT INTO onto_elements (run_id, kind, id, doc) VALUES ($1, $2, $3, $4)".to_string(),
            vec![P::from(&o.run_id), P::from(kind), P::from(id), P::from(doc)],
        ))
    };
    for t in &o.terms {
        push("term", &t.id, to_json(t));
    }
    for a in &o.attributes {
        push("attribute", &a.id, to_json(a));
    }
    for r in &o.relationships {
        push("relationship", &r.id, to_json(r));
    }
    for m in &o.metrics {
        push("metric", &m.id, to_json(m));
    }
    for b in &o.bridges {
        push("bridge", &b.id, to_json(b));
    }
    for t in &o.tables {
        push("table", &t.table_id, to_json(t));
    }
    stmts.push((
        "INSERT INTO onto_meta (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value".to_string(),
        vec![P::from(PUBLISHED), P::from(&o.run_id)],
    ));
    s.transaction(stmts).await
}

pub async fn ontology(s: &Store, run_id: &str) -> Result<Option<Ontology>> {
    let Some(run) = get_run(s, run_id).await? else { return Ok(None) };
    let rows = s.query("SELECT kind, doc FROM onto_elements WHERE run_id = $1 ORDER BY kind, id", vec![run_id.into()]).await?;
    let mut o = Ontology { run_id: run.id, version: run.version, ..Default::default() };
    for r in &rows {
        let doc = r[1].text();
        match r[0].text().as_str() {
            "term" => o.terms.push(from_json(&doc)?),
            "attribute" => o.attributes.push(from_json(&doc)?),
            "relationship" => o.relationships.push(from_json(&doc)?),
            "metric" => o.metrics.push(from_json(&doc)?),
            "bridge" => o.bridges.push(from_json(&doc)?),
            "table" => o.tables.push(from_json(&doc)?),
            _ => {}
        }
    }
    Ok(Some(o))
}

pub async fn published(s: &Store) -> Result<Option<Ontology>> {
    match s.meta(PUBLISHED).await? {
        Some(id) => ontology(s, &id).await,
        None => Ok(None),
    }
}
