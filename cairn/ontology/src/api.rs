// SPDX-License-Identifier: Apache-2.0
//! The HTTP API. Everything under `/v1` needs `Authorization: Bearer <token>` (Cairn is the only client and
//! enforces who may see what; the compiler refuses anyone without the token). `/-/live`, `/-/ready` and
//! `/metrics` are open, for probes and Prometheus.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::Ordering;
use std::sync::Arc;

use axum::extract::{DefaultBodyLimit, Path, Query, Request, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use subtle::ConstantTimeEq;

use crate::catalog::{self, TableFilter, TableIndex};
use crate::graph;
use crate::llm::cosine;
use crate::model::{CatalogSnapshot, Ontology, QueryBatch};
use crate::querylog;
use crate::repo;
use crate::semantic::pipeline::{Engine, StartError};
use crate::validate::{self, ValidateIn};

pub struct AppState {
    pub engine: Arc<Engine>,
    pub tokens: Vec<String>,
}

type S = State<Arc<AppState>>;

pub struct ApiError(StatusCode, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": self.1}))).into_response()
    }
}

fn internal(e: impl std::fmt::Display) -> ApiError {
    tracing::error!(error = %e, "request failed");
    ApiError(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
}

fn bad(msg: impl Into<String>) -> ApiError {
    ApiError(StatusCode::UNPROCESSABLE_ENTITY, msg.into())
}

type R<T> = Result<T, ApiError>;

async fn auth(State(st): S, headers: HeaderMap, req: Request, next: Next) -> Response {
    let presented = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("")
        .as_bytes()
        .to_vec();
    // Constant time, and against every configured token (one is being rotated in while the other goes out).
    let ok = !presented.is_empty() && st.tokens.iter().fold(false, |acc, t| acc | bool::from(t.as_bytes().ct_eq(&presented)));
    if !ok {
        return ApiError(StatusCode::UNAUTHORIZED, "a valid bearer token is required".into()).into_response();
    }
    next.run(req).await
}

pub fn router(state: Arc<AppState>) -> Router {
    let v1 = Router::new()
        .route("/v1/catalog", put(put_catalog).get(get_catalog))
        .route("/v1/queries", post(post_queries))
        .route("/v1/runs", post(start_run).get(list_runs))
        .route("/v1/runs/{id}", get(get_run))
        .route("/v1/runs/{id}/cancel", post(cancel_run))
        .route("/v1/runs/{id}/resume", post(resume_run))
        .route("/v1/estimate", post(estimate))
        .route("/v1/ontology", get(get_ontology))
        .route("/v1/terms", get(list_terms))
        .route("/v1/terms/{id}", get(get_term))
        .route("/v1/relationships", get(relationships))
        .route("/v1/search", post(search))
        .route("/v1/graph", post(graph_export))
        .route("/v1/validate-sql", post(validate_sql))
        .route("/v1/provenance/{kind}/{id}", get(provenance))
        .layer(middleware::from_fn_with_state(state.clone(), auth))
        .layer(DefaultBodyLimit::max(128 * 1024 * 1024));
    Router::new()
        .route("/-/live", get(|| async { "ok" }))
        .route("/-/healthy", get(|| async { "ok" }))
        .route("/-/ready", get(ready))
        .route("/metrics", get(metrics))
        .merge(v1)
        .with_state(state)
}

async fn ready(State(st): S) -> Response {
    match st.engine.store.ping().await {
        Ok(()) => (StatusCode::OK, "ready").into_response(),
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, e.to_string()).into_response(),
    }
}

async fn metrics(State(st): S) -> Response {
    let e = &st.engine;
    let s = &e.llm.stats;
    let tables = repo::tables(&e.store).await.map(|t| t.len()).unwrap_or(0);
    let queries = repo::query_count(&e.store).await.unwrap_or(0);
    let body = format!(
        "# TYPE cairn_ontology_model_calls_total counter\ncairn_ontology_model_calls_total {}\n\
# TYPE cairn_ontology_model_failures_total counter\ncairn_ontology_model_failures_total {}\n\
# TYPE cairn_ontology_model_retries_total counter\ncairn_ontology_model_retries_total {}\n\
# TYPE cairn_ontology_embedding_calls_total counter\ncairn_ontology_embedding_calls_total {}\n\
# TYPE cairn_ontology_tokens_total counter\ncairn_ontology_tokens_total{{kind=\"input\"}} {}\ncairn_ontology_tokens_total{{kind=\"output\"}} {}\n\
# TYPE cairn_ontology_runs_total counter\ncairn_ontology_runs_total{{outcome=\"completed\"}} {}\ncairn_ontology_runs_total{{outcome=\"failed\"}} {}\n\
# TYPE cairn_ontology_run_active gauge\ncairn_ontology_run_active {}\n\
# TYPE cairn_ontology_catalog_tables gauge\ncairn_ontology_catalog_tables {tables}\n\
# TYPE cairn_ontology_queries gauge\ncairn_ontology_queries {queries}\n",
        s.calls.load(Ordering::Relaxed),
        s.failures.load(Ordering::Relaxed),
        s.retries.load(Ordering::Relaxed),
        s.embed_calls.load(Ordering::Relaxed),
        s.input_tokens.load(Ordering::Relaxed),
        s.output_tokens.load(Ordering::Relaxed),
        e.runs_completed.load(Ordering::Relaxed),
        e.runs_failed.load(Ordering::Relaxed),
        e.active_run().is_some() as u8,
    );
    ([(header::CONTENT_TYPE, "text/plain; version=0.0.4")], body).into_response()
}

// ------------------------------------------------------------------------------------------- catalog

async fn put_catalog(State(st): S, Json(snap): Json<CatalogSnapshot>) -> R<Json<Value>> {
    let filter = TableFilter::from_spec(snap.filter.as_ref()).map_err(bad)?;
    let total = snap.tables.len();
    let tables: Vec<_> = snap.tables.into_iter().filter(|t| filter.keep(&t.name)).collect();
    if filter.active() && tables.len() != total {
        tracing::info!(filter = %filter.describe(), kept = tables.len(), of = total, "table filter applied");
    }
    catalog::validate_shape(&tables).map_err(bad)?;
    catalog::validate_coverage(&tables).map_err(bad)?;
    let existing: BTreeMap<String, crate::model::Table> =
        repo::tables(&st.engine.store).await.map_err(internal)?.into_iter().map(|t| (t.id.clone(), t)).collect();
    let mut d = catalog::diff(&existing, &tables, snap.replace);
    d.filtered_out = total - tables.len();
    let changed: Vec<_> = tables
        .iter()
        .filter(|t| existing.get(&t.id).is_none_or(|o| catalog::content_fingerprint(o) != catalog::content_fingerprint(t)))
        .cloned()
        .collect();
    repo::put_tables(&st.engine.store, &changed, &d.tables_removed).await.map_err(internal)?;
    Ok(Json(json!({"database": snap.database, "tables": tables.len(), "diff": d, "changed": d.changed()})))
}

async fn get_catalog(State(st): S) -> R<Json<Value>> {
    let t = repo::tables(&st.engine.store).await.map_err(internal)?;
    let tables: Vec<Value> = t.iter().map(|t| json!({"id": t.id, "name": t.qualified(), "columns": t.columns.len()})).collect();
    Ok(Json(json!({"tables": tables})))
}

// ------------------------------------------------------------------------------------------- queries

async fn post_queries(State(st): S, Json(batch): Json<QueryBatch>) -> R<Json<Value>> {
    if batch.queries.len() > 50_000 {
        return Err(bad("at most 50000 queries a request"));
    }
    let tables = TableIndex::new(repo::tables(&st.engine.store).await.map_err(internal)?);
    // The stored rows these queries may merge into.
    let mut existing = BTreeMap::new();
    for q in &batch.queries {
        if let Some(n) = crate::sqlx::normalize(&q.sql, &batch.dialects, true) {
            let fp = querylog::fingerprint(&n);
            if let std::collections::btree_map::Entry::Vacant(e) = existing.entry(fp.clone()) {
                if let Some(s) = repo::query_by_fingerprint(&st.engine.store, &fp).await.map_err(internal)? {
                    e.insert(s);
                }
            }
        }
    }
    let (rows, report) = querylog::merge_batch(&batch.queries, &batch.dialects, &tables, &existing);
    repo::put_queries(&st.engine.store, &rows).await.map_err(internal)?;
    Ok(Json(serde_json::to_value(report).map_err(internal)?))
}

// ------------------------------------------------------------------------------------------- runs

#[derive(Deserialize, Default)]
struct RunIn {
    #[serde(default)]
    mode: Option<String>,
    #[serde(default)]
    requested_by: Option<String>,
    /// Only these tables (ids) are compiled; the others keep what the published run made of them.
    #[serde(default)]
    tables: Option<Vec<String>>,
}

const MAX_SCOPE: usize = 100_000;

fn scope_of(tables: Option<Vec<String>>) -> R<Option<Vec<String>>> {
    match tables {
        None => Ok(None),
        Some(t) if t.is_empty() => Err(bad("tables, when given, names at least one table")),
        Some(t) if t.len() > MAX_SCOPE => Err(bad("at most 100000 tables in a scope")),
        Some(t) => Ok(Some(t.into_iter().collect::<BTreeSet<_>>().into_iter().collect())),
    }
}

fn start_error(e: StartError) -> ApiError {
    match e {
        StartError::Busy(id) => ApiError(StatusCode::CONFLICT, format!("run {id} is in progress")),
        StartError::NotFound => ApiError(StatusCode::NOT_FOUND, "no such run".into()),
        StartError::NotResumable(state) => ApiError(StatusCode::CONFLICT, format!("a {state} run cannot be resumed")),
        StartError::Store(e) => internal(e),
    }
}

async fn start_run(State(st): S, body: Option<Json<RunIn>>) -> R<(StatusCode, Json<Value>)> {
    let b = body.map(|b| b.0).unwrap_or_default();
    let mode = b.mode.unwrap_or_else(|| "incremental".into());
    if mode != "full" && mode != "incremental" {
        return Err(bad("mode is full or incremental"));
    }
    let who: String = b.requested_by.unwrap_or_default().chars().take(512).collect();
    let scope = scope_of(b.tables)?;
    let run = st.engine.start_scoped(&mode, &who, None, scope).await.map_err(start_error)?;
    Ok((StatusCode::ACCEPTED, Json(serde_json::to_value(run).map_err(internal)?)))
}

async fn list_runs(State(st): S) -> R<Json<Value>> {
    let runs = repo::runs(&st.engine.store, 50).await.map_err(internal)?;
    let published = st.engine.store.meta(repo::PUBLISHED).await.map_err(internal)?;
    Ok(Json(json!({"runs": runs, "published": published, "active": st.engine.active_run()})))
}

async fn get_run(State(st): S, Path(id): Path<String>) -> R<Json<Value>> {
    let run = repo::get_run(&st.engine.store, &id).await.map_err(internal)?.ok_or(ApiError(StatusCode::NOT_FOUND, "no such run".into()))?;
    let mut v = serde_json::to_value(run).map_err(internal)?;
    // A run in progress says how far it is (tables done of the total, and the stage after the tables).
    if let Some(p) = st.engine.progress(&id) {
        v["progress"] = serde_json::to_value(p).map_err(internal)?;
    }
    Ok(Json(v))
}

#[derive(Deserialize, Default)]
struct EstimateIn {
    #[serde(default)]
    mode: Option<String>,
    #[serde(default)]
    tables: Option<Vec<String>>,
}

/// What a run would compile and an upper bound of its model calls, before it is started.
async fn estimate(State(st): S, body: Option<Json<EstimateIn>>) -> R<Json<Value>> {
    let b = body.map(|b| b.0).unwrap_or_default();
    let mode = b.mode.unwrap_or_else(|| "incremental".into());
    if mode != "full" && mode != "incremental" {
        return Err(bad("mode is full or incremental"));
    }
    let scope: Option<BTreeSet<String>> = scope_of(b.tables)?.map(|t| t.into_iter().collect());
    Ok(Json(st.engine.estimate(&mode, scope.as_ref()).await.map_err(internal)?))
}

async fn cancel_run(State(st): S, Path(id): Path<String>) -> R<Json<Value>> {
    if !st.engine.cancel(&id) {
        return Err(ApiError(StatusCode::CONFLICT, "that run is not in progress".into()));
    }
    Ok(Json(json!({"cancelling": id})))
}

async fn resume_run(State(st): S, Path(id): Path<String>) -> R<(StatusCode, Json<Value>)> {
    let run = st.engine.start("", "", Some(&id)).await.map_err(start_error)?;
    Ok((StatusCode::ACCEPTED, Json(serde_json::to_value(run).map_err(internal)?)))
}

// ------------------------------------------------------------------------------------------- reads

async fn load(st: &AppState, run: Option<&str>) -> R<Ontology> {
    let o = match run {
        Some(id) => repo::ontology(&st.engine.store, id).await.map_err(internal)?,
        None => repo::published(&st.engine.store).await.map_err(internal)?,
    };
    o.ok_or(ApiError(StatusCode::NOT_FOUND, "no compiled ontology yet".into()))
}

#[derive(Deserialize)]
struct RunQ {
    run: Option<String>,
}

async fn get_ontology(State(st): S, Query(q): Query<RunQ>) -> R<Json<Value>> {
    let o = load(&st, q.run.as_deref()).await?.strip_embeddings();
    Ok(Json(serde_json::to_value(o).map_err(internal)?))
}

#[derive(Deserialize)]
struct TermsQ {
    q: Option<String>,
    limit: Option<usize>,
}

fn name_score(q: &str, name: &str, synonyms: &[String]) -> f64 {
    let (q, n) = (q.to_lowercase(), name.to_lowercase());
    if n == q || synonyms.iter().any(|s| s.to_lowercase() == q) {
        1.0
    } else if n.starts_with(&q) {
        0.8
    } else if n.contains(&q) || synonyms.iter().any(|s| s.to_lowercase().contains(&q)) {
        0.6
    } else {
        let words: Vec<&str> = q.split_whitespace().collect();
        let hit = words.iter().filter(|w| n.contains(*w)).count();
        if words.is_empty() {
            0.0
        } else {
            0.4 * hit as f64 / words.len() as f64
        }
    }
}

async fn list_terms(State(st): S, Query(q): Query<TermsQ>) -> R<Json<Value>> {
    let o = load(&st, None).await?.strip_embeddings();
    let limit = q.limit.unwrap_or(200).min(2000);
    let mut terms: Vec<(f64, &crate::model::Term)> = o
        .terms
        .iter()
        .map(|t| (q.q.as_deref().map(|x| name_score(x, &t.name, &t.synonyms)).unwrap_or(1.0), t))
        .filter(|(s, _)| *s > 0.0)
        .collect();
    terms.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.name.cmp(&b.1.name)));
    let out: Vec<Value> = terms.iter().take(limit).map(|(s, t)| json!({"score": s, "term": t})).collect();
    Ok(Json(json!({"version": o.version, "terms": out})))
}

async fn get_term(State(st): S, Path(id): Path<String>) -> R<Json<Value>> {
    let o = load(&st, None).await?.strip_embeddings();
    let t = o.terms.iter().find(|t| t.id == id).ok_or(ApiError(StatusCode::NOT_FOUND, "no such term".into()))?;
    let attrs: Vec<_> = o.attributes.iter().filter(|a| a.term_id == t.id).collect();
    let metrics: Vec<_> = o.metrics.iter().filter(|m| m.term_id == t.id).collect();
    let rels: Vec<_> = o.relationships.iter().filter(|r| r.source_table == t.table_id || r.target_table == t.table_id).collect();
    Ok(Json(json!({"term": t, "attributes": attrs, "metrics": metrics, "relationships": rels})))
}

#[derive(Deserialize)]
struct RelQ {
    tables: Option<String>,
    /// true: only relationships whose two tables are both listed.
    between: Option<bool>,
}

async fn relationships(State(st): S, Query(q): Query<RelQ>) -> R<Json<Value>> {
    let o = load(&st, None).await?;
    let wanted: Option<BTreeSet<String>> = q.tables.map(|s| s.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect());
    let between = q.between.unwrap_or(false);
    let rels: Vec<_> = o
        .relationships
        .iter()
        .filter(|r| match &wanted {
            None => true,
            Some(w) if between => w.contains(&r.source_table) && w.contains(&r.target_table),
            Some(w) => w.contains(&r.source_table) || w.contains(&r.target_table),
        })
        .collect();
    Ok(Json(json!({"version": o.version, "relationships": rels})))
}

#[derive(Deserialize)]
struct SearchIn {
    q: String,
    #[serde(default)]
    mode: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
}

async fn search(State(st): S, Json(b): Json<SearchIn>) -> R<Json<Value>> {
    if b.q.trim().is_empty() || b.q.len() > 1000 {
        return Err(bad("q is 1 to 1000 characters"));
    }
    let o = load(&st, None).await?;
    let limit = b.limit.unwrap_or(10).clamp(1, 100);
    let mode = b.mode.unwrap_or_else(|| "hybrid".into());
    let mut hits: BTreeMap<String, (f64, Value)> = BTreeMap::new();
    let mut add = |id: &str, score: f64, v: Value| {
        let e = hits.entry(id.to_string()).or_insert((0.0, v));
        e.0 = e.0.max(score);
    };
    if mode == "name" || mode == "hybrid" {
        for t in &o.terms {
            let s = name_score(&b.q, &t.name, &t.synonyms);
            if s > 0.0 {
                add(&t.id, s, json!({"kind": "term", "id": t.id, "name": t.name, "table_id": t.table_id, "description": t.description}));
            }
        }
        for a in &o.attributes {
            let s = name_score(&b.q, &a.name, std::slice::from_ref(&a.column)) * 0.9;
            if s > 0.0 {
                add(
                    &a.id,
                    s,
                    json!({"kind": "attribute", "id": a.id, "name": a.name, "table_id": a.table_id, "column": a.column, "term_name": a.term_name}),
                );
            }
        }
        for m in &o.metrics {
            let s = name_score(&b.q, &m.name, &[]) * 0.9;
            if s > 0.0 {
                add(&m.id, s, json!({"kind": "metric", "id": m.id, "name": m.name, "tables": m.tables, "sql": m.sql}));
            }
        }
    }
    let mut semantic = false;
    if (mode == "semantic" || mode == "hybrid") && st.engine.llm.embed_configured() {
        match st.engine.llm.embed(std::slice::from_ref(&b.q)).await {
            Ok(v) => {
                semantic = true;
                let q = &v[0];
                for t in &o.terms {
                    if let Some(e) = &t.embedding {
                        add(
                            &t.id,
                            cosine(q, e),
                            json!({"kind": "term", "id": t.id, "name": t.name, "table_id": t.table_id, "description": t.description}),
                        );
                    }
                }
                for a in &o.attributes {
                    if let Some(e) = &a.embedding {
                        add(
                            &a.id,
                            cosine(q, e),
                            json!({"kind": "attribute", "id": a.id, "name": a.name, "table_id": a.table_id, "column": a.column, "term_name": a.term_name}),
                        );
                    }
                }
            }
            Err(e) => {
                if mode == "semantic" {
                    return Err(ApiError(StatusCode::BAD_GATEWAY, format!("embedding the query failed: {e}")));
                }
            }
        }
    } else if mode == "semantic" {
        return Err(ApiError(StatusCode::SERVICE_UNAVAILABLE, "no embeddings endpoint is configured".into()));
    }
    let mut ranked: Vec<(f64, Value)> = hits.into_values().collect();
    ranked.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let out: Vec<Value> = ranked
        .into_iter()
        .take(limit)
        .map(|(s, mut v)| {
            v["score"] = json!((s * 10_000.0).round() / 10_000.0);
            v
        })
        .collect();
    Ok(Json(json!({"version": o.version, "semantic": semantic, "hits": out})))
}

#[derive(Deserialize)]
struct GraphIn {
    #[serde(default)]
    format: Option<String>,
    /// Only elements over these tables (Cairn passes the tables the caller may see).
    #[serde(default)]
    tables: Option<Vec<String>>,
}

async fn graph_export(State(st): S, Json(b): Json<GraphIn>) -> R<Response> {
    let o = load(&st, None).await?;
    let keep: Option<BTreeSet<String>> = b.tables.map(|t| t.into_iter().collect());
    let o = graph::restrict(&o, keep.as_ref());
    let idx = TableIndex::new(repo::tables(&st.engine.store).await.map_err(internal)?);
    Ok(match b.format.as_deref().unwrap_or("json") {
        "json" => Json(graph::to_json(&o, &idx)).into_response(),
        "jsonld" | "json-ld" => ([(header::CONTENT_TYPE, "application/ld+json")], graph::to_jsonld(&o, &idx).to_string()).into_response(),
        "turtle" | "ttl" => ([(header::CONTENT_TYPE, "text/turtle; charset=utf-8")], graph::to_turtle(&o, &idx)).into_response(),
        _ => return Err(bad("format is json, jsonld or turtle")),
    })
}

async fn validate_sql(State(st): S, Json(b): Json<ValidateIn>) -> R<Json<Value>> {
    if b.sql.trim().is_empty() || b.sql.len() > 200_000 {
        return Err(bad("sql is 1 to 200000 characters"));
    }
    let idx = TableIndex::new(repo::tables(&st.engine.store).await.map_err(internal)?);
    let o = repo::published(&st.engine.store).await.map_err(internal)?;
    let v = validate::validate(&b, &idx, o.as_ref());
    Ok(Json(serde_json::to_value(v).map_err(internal)?))
}

async fn provenance(State(st): S, Path((kind, id)): Path<(String, String)>) -> R<Json<Value>> {
    let o = load(&st, None).await?;
    let found = match kind.as_str() {
        "term" => o.terms.iter().find(|x| x.id == id).map(|x| json!({"name": x.name, "tables": [x.table_id], "provenance": x.provenance})),
        "attribute" => o.attributes.iter().find(|x| x.id == id).map(|x| json!({"name": x.name, "tables": [x.table_id], "column": x.column, "provenance": x.provenance})),
        "relationship" => o.relationships.iter().find(|x| x.id == id).map(|x| json!({"tables": [x.source_table, x.target_table], "provenance": x.provenance, "evidence": x.evidence, "confidence": x.confidence})),
        "metric" => o.metrics.iter().find(|x| x.id == id).map(|x| json!({"name": x.name, "tables": x.tables, "provenance": x.provenance, "source_query_ids": x.source_query_ids})),
        "bridge" => o.bridges.iter().find(|x| x.id == id).map(|x| json!({"tables": std::iter::once(x.table_id.clone()).chain(x.fk_pairs.iter().map(|p| p.target_table.clone())).collect::<Vec<_>>(), "provenance": x.provenance})),
        _ => return Err(bad("kind is term, attribute, relationship, metric or bridge")),
    };
    let mut v = found.ok_or(ApiError(StatusCode::NOT_FOUND, "no such element".into()))?;
    v["run_id"] = json!(o.run_id);
    v["version"] = json!(o.version);
    Ok(Json(v))
}
