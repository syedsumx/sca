// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/semantic/pipeline.py, compile.py, visit_enter.py
// (`process_table`, `_commit_terms`) and cancellation.py (and their tests).
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust on tokio; each table's result is committed to the database in its turn, so an
// interrupted run resumes from the tables not yet committed, and an incremental run reuses the results of tables
// that did not change since the last published run.
//! A compilation run.
//!
//! Tables are processed in parallel (three at a time: their model calls overlap) but **commit** in canonical
//! table order (`database, schema, name`, case-folded, then as written, then id). Committing is where a Term's
//! name is allocated: a name another table already owns is qualified with the table's full name, so which table
//! keeps the plain name never depends on which model call finished first.
//!
//! Cancellation is cooperative: a cancelled run stops before each table not yet started (the tables in flight
//! finish) and between the stages after the tables (FK resolution, SqlAttributes, bridge tables). Every table's
//! committed result survives, so the run can be resumed.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde_json::{json, Value};
use tokio::sync::{watch, Semaphore};

use super::profile::profile_table;
use super::sql_attributes;
use super::table::{self, DeclaredFk, DomainSummary};
use super::{embed, semantic_fk};
use crate::catalog::{content_fingerprint, TableIndex};
use crate::llm::Llm;
use crate::model::{stable_id, Attribute, Ontology, Provenance, Table, TableFacts, TableResult, Term, Usage};
use crate::querylog::{self, ParsedQuery};
use crate::repo::{self, Run};
use crate::store::Store;

// ------------------------------------------------------------------------------------- commit queue

#[derive(Default)]
struct QueueState {
    next: usize,
    skipped: BTreeSet<usize>,
}

/// Turns in a compilation-wide canonical commit sequence (`_OrderedCommitQueue`), without serialising the
/// model work before each turn.
pub struct CommitQueue {
    state: Mutex<QueueState>,
    tx: watch::Sender<usize>,
}

impl Default for CommitQueue {
    fn default() -> Self {
        CommitQueue { state: Mutex::new(QueueState::default()), tx: watch::channel(0).0 }
    }
}

impl CommitQueue {
    /// Waits for `position`'s turn.
    pub async fn wait(&self, position: usize) {
        let mut rx = self.tx.subscribe();
        let _ = rx.wait_for(|n| *n == position).await;
    }

    fn drain(st: &mut QueueState) {
        while st.skipped.remove(&st.next) {
            st.next += 1;
        }
    }

    /// Ends `position`'s turn (the caller holds it).
    pub fn advance(&self, position: usize) {
        let mut st = self.state.lock().expect("commit queue");
        if st.next == position {
            st.next += 1;
            Self::drain(&mut st);
        }
        self.tx.send_replace(st.next);
    }

    /// Gives up a turn **without** waiting for it: cancelling a backlog makes every queued table skip at once,
    /// and a position whose predecessors have not had their turns can be given up too. The skip is consumed
    /// when its turn arrives, cascading through consecutive skips.
    pub fn skip(&self, position: usize) {
        let mut st = self.state.lock().expect("commit queue");
        if position >= st.next {
            st.skipped.insert(position);
        }
        Self::drain(&mut st);
        self.tx.send_replace(st.next);
    }

    #[cfg(test)]
    pub fn next(&self) -> usize {
        self.state.lock().expect("commit queue").next
    }
}

/// One table's turn (`_OrderedCommitSlot`): entered, then completed; or given up if never used.
pub struct Slot {
    queue: Arc<CommitQueue>,
    position: usize,
    entered: bool,
    completed: bool,
}

impl Slot {
    pub fn new(queue: Arc<CommitQueue>, position: usize) -> Slot {
        Slot { queue, position, entered: false, completed: false }
    }

    pub async fn enter(&mut self) {
        self.queue.wait(self.position).await;
        self.entered = true;
    }

    pub fn exit(&mut self) {
        if self.entered && !self.completed {
            self.queue.advance(self.position);
            self.completed = true;
        }
    }

    pub fn skip_if_unused(&mut self) {
        if self.entered || self.completed {
            return;
        }
        self.queue.skip(self.position);
        self.completed = true;
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        // A task that ends early (an error, a panic) never holds up the tables after it.
        if self.entered {
            self.exit();
        } else {
            self.skip_if_unused();
        }
    }
}

/// Canonical table order (`_table_commit_key`).
pub fn commit_key(database: &str, t: &Table) -> (String, String, String, String, String, String, String) {
    (
        database.to_lowercase(),
        t.schema.to_lowercase(),
        t.name.to_lowercase(),
        database.to_string(),
        t.schema.clone(),
        t.name.clone(),
        t.id.clone(),
    )
}

// ------------------------------------------------------------------------------------- one table

pub struct Settings {
    pub workers: usize,
    pub probe_distinct_values: bool,
    pub max_queries: i64,
    pub domain: Option<DomainSummary>,
    pub database: String,
}

fn declared_fks(t: &Table, tables: &TableIndex) -> Vec<DeclaredFk> {
    let mut out = vec![];
    for fk in &t.foreign_keys {
        let target = tables.resolve(&fk.target_table);
        for (i, c) in fk.columns.iter().enumerate() {
            out.push(DeclaredFk {
                source_column: c.clone(),
                target_table: target.map(|x| x.qualified()).unwrap_or_else(|| fk.target_table.clone()),
                target_column: fk.target_columns.get(i).cloned().unwrap_or_default(),
            });
        }
    }
    out
}

/// Columns of each table that other tables' declared FKs reference.
fn fk_targets(tables: &TableIndex) -> BTreeMap<String, BTreeSet<String>> {
    let mut out: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for t in &tables.tables {
        for fk in &t.foreign_keys {
            if let Some(target) = tables.resolve(&fk.target_table) {
                let cols = if fk.target_columns.is_empty() { target.primary_key.clone() } else { fk.target_columns.clone() };
                out.entry(target.id.clone()).or_default().extend(cols);
            }
        }
    }
    out
}

fn table_fingerprint(t: &Table, probe: bool) -> String {
    format!("{}:{}", content_fingerprint(t), probe as u8)
}

/// Builds the Term(s) and ColumnAttributes for one table (`process_table`), before its commit turn.
pub async fn process_table(
    llm: &Llm,
    t: &Table,
    tables: &TableIndex,
    targets: &BTreeSet<String>,
    settings: &Settings,
    run_id: &str,
) -> TableResult {
    let profiles = profile_table(t, settings.probe_distinct_values);
    let declared = declared_fks(t, tables);
    let fk = table::suggest_foreign_keys(llm, t, &declared, targets, &profiles).await;
    let suggested: BTreeSet<String> = fk.suggestions.iter().map(|(c, _)| c.clone()).collect();
    let mut all_fk: BTreeSet<String> = table::fk_source_columns(&declared);
    all_fk.extend(suggested.iter().cloned());
    let mut facts = TableFacts {
        table_id: t.id.clone(),
        is_junction: false,
        junction_rationale: String::new(),
        suspected_fks: fk.suggestions.iter().cloned().collect(),
        profiles: profiles.clone(),
        fingerprint: table_fingerprint(t, settings.probe_distinct_values),
        usage: Usage::default(),
    };
    // A payload-bearing junction needs the model's decision and at least two FK roles.
    if t.columns.len() > 2 && fk.is_junction_table && all_fk.len() >= 2 {
        facts.is_junction = true;
        facts.junction_rationale =
            if fk.junction_table_rationale.is_empty() { "LLM table-grain decision".into() } else { fk.junction_table_rationale.clone() };
    }
    let mut specs = table::column_attribute_specs(llm, &t.columns, &declared, &suggested, &profiles).await;
    if specs.is_empty() {
        tracing::info!(table = %t.qualified(), "no non-FK columns — no Term");
        return TableResult { facts, terms: vec![], attributes: vec![] };
    }
    let proposals = table::extract_terms(llm, t, &specs, settings.domain.as_ref()).await;
    table::apply_display_names(&proposals, &mut specs);
    let by_col: BTreeMap<&str, &table::Spec> = specs.iter().map(|s| (s.source_column.as_str(), s)).collect();
    let source = if llm.chat_configured() { "model" } else { "inferred" };
    let mut terms = vec![];
    let mut attrs = vec![];
    for p in &proposals {
        let assigned: Vec<&table::Spec> = p.attributes.iter().filter_map(|(c, _)| by_col.get(c.as_str()).copied()).collect();
        if assigned.is_empty() {
            continue;
        }
        let term_id = stable_id("term", &[&t.id, &p.name.to_lowercase()]);
        let mut term = Term {
            id: term_id.clone(),
            name: p.name.clone(),
            proposed_name: p.name.clone(),
            description: p.description.clone(),
            synonyms: p.synonyms.clone(),
            table_id: t.id.clone(),
            table_name: t.qualified(),
            attribute_ids: vec![],
            provenance: vec![Provenance {
                source: source.into(),
                detail: format!("business Term for table {}", t.qualified()),
                confidence: 0.7,
                run_id: run_id.into(),
            }],
            embedding: None,
        };
        for s in assigned {
            let col = t.column(&s.source_column);
            let prof = profiles.get(&s.source_column);
            let a = Attribute {
                id: stable_id("attr", &[&t.id, &s.source_column.to_lowercase()]),
                term_id: term_id.clone(),
                term_name: p.name.clone(),
                table_id: t.id.clone(),
                column: s.source_column.clone(),
                name: s.display_name.clone(),
                datatype: s.datatype.clone(),
                description: s.description.clone(),
                is_unique: if t.is_pk(&s.source_column) { Some(true) } else { prof.and_then(|p| p.is_unique) },
                format: prof.and_then(|p| p.format.clone()),
                sample_values: prof.map(|p| p.sample_values.clone()).unwrap_or_default(),
                deprecated: col.is_some_and(|c| c.deprecated) || t.deprecated,
                provenance: vec![Provenance {
                    source: "catalog".into(),
                    detail: format!("column {}.{}", t.qualified(), s.source_column),
                    confidence: 1.0,
                    run_id: run_id.into(),
                }],
                embedding: None,
            };
            term.attribute_ids.push(a.id.clone());
            attrs.push(a);
        }
        terms.push(term);
    }
    if terms.is_empty() {
        tracing::warn!(table = %t.qualified(), candidates = specs.len(), "the model assigned no columns to any Term");
    }
    embed::embed_elements(llm, &mut terms, &mut attrs).await;
    TableResult { facts, terms, attributes: attrs }
}

/// Term names owned so far in this run (name → table id), filled in commit order.
#[derive(Default)]
pub struct Names(BTreeMap<String, String>);

impl Names {
    /// The name `proposed` gets for `table` (`upsert_table_term`): itself unless another table owns it, then
    /// qualified with the table's full name, then with its id.
    pub fn allocate(&mut self, proposed: &str, table: &Table) -> String {
        let full = table.full_name();
        for candidate in [proposed.to_string(), format!("{proposed} ({full})"), format!("{proposed} ({})", table.id)] {
            match self.0.get(&candidate.to_lowercase()) {
                Some(owner) if owner != &table.id => continue,
                _ => {
                    self.0.insert(candidate.to_lowercase(), table.id.clone());
                    return candidate;
                }
            }
        }
        proposed.to_string()
    }
}

fn rename(mut r: TableResult, names: &mut Names, t: &Table) -> TableResult {
    for term in &mut r.terms {
        let proposed = if term.proposed_name.is_empty() { term.name.clone() } else { term.proposed_name.clone() };
        let name = names.allocate(&proposed, t);
        if name != proposed {
            tracing::info!(table = %t.qualified(), proposed = %proposed, name = %name, "qualified a colliding Term name");
        }
        term.name = name.clone();
        for a in r.attributes.iter_mut().filter(|a| a.term_id == term.id) {
            a.term_name = name.clone();
        }
    }
    r
}

// ------------------------------------------------------------------------------------- the engine

pub struct Engine {
    pub store: Store,
    pub llm: Arc<Llm>,
    pub settings: Settings,
    active: Mutex<Option<(String, Arc<AtomicBool>)>>,
    progress: Mutex<Option<Progress>>,
    pub runs_completed: AtomicUsize,
    pub runs_failed: AtomicUsize,
}

/// How far the active run is (kept in memory: a run that is not active has its final statistics instead).
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct Progress {
    pub run_id: String,
    /// `tables`, `relationships`, `metrics`, `bridges` or `publishing`.
    pub phase: String,
    pub tables_total: usize,
    pub tables_done: usize,
    /// How many tables the run's scope names (None: every table).
    pub scope: Option<usize>,
}

/// Upper bound of model calls for one table processed (column descriptions, foreign keys, Terms, synonyms).
pub const CHAT_CALLS_PER_TABLE: usize = 4;

/// The tables of a run's scope (`stats.scope`): None when the run covers every table.
fn run_scope(stats: &Value) -> Option<BTreeSet<String>> {
    stats.get("scope").and_then(|s| s.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
}

#[derive(Debug)]
pub enum StartError {
    Busy(String),
    NotFound,
    NotResumable(String),
    Store(String),
}

impl Engine {
    pub fn new(store: Store, llm: Arc<Llm>, settings: Settings) -> Engine {
        Engine {
            store,
            llm,
            settings,
            active: Mutex::new(None),
            progress: Mutex::new(None),
            runs_completed: AtomicUsize::new(0),
            runs_failed: AtomicUsize::new(0),
        }
    }

    /// The active run's progress, when `run_id` is the active run.
    pub fn progress(&self, run_id: &str) -> Option<Progress> {
        self.progress.lock().expect("progress").as_ref().filter(|p| p.run_id == run_id).cloned()
    }

    fn set_phase(&self, run_id: &str, phase: &str) {
        if let Some(p) = self.progress.lock().expect("progress").as_mut().filter(|p| p.run_id == run_id) {
            p.phase = phase.to_string();
        }
    }

    fn table_done(&self, run_id: &str) {
        if let Some(p) = self.progress.lock().expect("progress").as_mut().filter(|p| p.run_id == run_id) {
            p.tables_done += 1;
        }
    }

    /// What a run would do, before it starts: how many tables it compiles (the others are reused from the
    /// published run) and an upper bound of the model calls that takes. `scope`: only these tables (ids).
    pub async fn estimate(&self, mode: &str, scope: Option<&BTreeSet<String>>) -> Result<Value, String> {
        let tables = repo::tables(&self.store).await.map_err(|e| e.to_string())?;
        let published = self.store.meta(repo::PUBLISHED).await.map_err(|e| e.to_string())?;
        let previous = match &published {
            Some(id) => repo::table_results(&self.store, id).await.map_err(|e| e.to_string())?,
            None => BTreeMap::new(),
        };
        let (mut process, mut reuse, mut out_of_scope, mut empty) = (0usize, 0usize, 0usize, 0usize);
        for t in &tables {
            if t.columns.is_empty() {
                empty += 1;
            } else if scope.is_some_and(|s| !s.contains(&t.id)) {
                out_of_scope += 1;
            } else if mode == "incremental"
                && previous.get(&t.id).is_some_and(|r| r.facts.fingerprint == table_fingerprint(t, self.settings.probe_distinct_values))
            {
                reuse += 1;
            } else {
                process += 1;
            }
        }
        let unknown: Vec<&String> = scope.map(|s| s.iter().filter(|id| !tables.iter().any(|t| &t.id == *id)).collect()).unwrap_or_default();
        let (chat, embed) = (self.llm.chat_configured(), self.llm.embed_configured());
        Ok(json!({
            "mode": mode,
            "tables": tables.len(),
            "to_process": process,
            "reused": reuse,
            "out_of_scope": out_of_scope,
            "without_columns": empty,
            "unknown_tables": unknown,
            "published": published,
            "model": {
                "chat_configured": chat,
                "embeddings_configured": embed,
                "chat_calls_per_table": if chat { CHAT_CALLS_PER_TABLE } else { 0 },
                "chat_calls_max": if chat { process * CHAT_CALLS_PER_TABLE } else { 0 },
                "embedding_calls_max": if embed { process } else { 0 },
                "note": "an upper bound for the tables compiled; relationships, metrics and bridge tables add calls in proportion to the candidates found",
            },
        }))
    }

    pub fn active_run(&self) -> Option<String> {
        self.active.lock().expect("active").as_ref().map(|(id, _)| id.clone())
    }

    /// Starts a run (`mode` full or incremental), or resumes `resume`. One run at a time.
    pub async fn start(self: &Arc<Self>, mode: &str, requested_by: &str, resume: Option<&str>) -> Result<Run, StartError> {
        self.start_scoped(mode, requested_by, resume, None).await
    }

    /// As `start`, limited to `scope` (table ids): only those tables are compiled; every other table keeps what
    /// the published run made of it (or is left out when it has none), so the ontology stays whole. The scope is
    /// kept in the run's statistics, so a resumed run keeps it too.
    pub async fn start_scoped(
        self: &Arc<Self>,
        mode: &str,
        requested_by: &str,
        resume: Option<&str>,
        scope: Option<Vec<String>>,
    ) -> Result<Run, StartError> {
        let cancel = Arc::new(AtomicBool::new(false));
        let run = {
            let reserve = {
                let mut a = self.active.lock().expect("active");
                if let Some((id, _)) = a.as_ref() {
                    return Err(StartError::Busy(id.clone()));
                }
                *a = Some(("pending".into(), cancel.clone()));
                true
            };
            let made = async {
                match resume {
                    Some(id) => {
                        let r = repo::get_run(&self.store, id)
                            .await
                            .map_err(|e| StartError::Store(e.to_string()))?
                            .ok_or(StartError::NotFound)?;
                        if !r.resumable() {
                            return Err(StartError::NotResumable(r.state.clone()));
                        }
                        repo::set_run_state(&self.store, &r.id, "running", &r.stats, None)
                            .await
                            .map_err(|e| StartError::Store(e.to_string()))?;
                        repo::get_run(&self.store, id).await.map_err(|e| StartError::Store(e.to_string()))?.ok_or(StartError::NotFound)
                    }
                    None => {
                        let r = repo::create_run(&self.store, &crate::model::new_id("run"), mode, requested_by)
                            .await
                            .map_err(|e| StartError::Store(e.to_string()))?;
                        match &scope {
                            None => Ok(r),
                            Some(tables) => {
                                repo::set_run_state(&self.store, &r.id, "running", &json!({"scope": tables}), None)
                                    .await
                                    .map_err(|e| StartError::Store(e.to_string()))?;
                                repo::get_run(&self.store, &r.id)
                                    .await
                                    .map_err(|e| StartError::Store(e.to_string()))?
                                    .ok_or(StartError::NotFound)
                            }
                        }
                    }
                }
            }
            .await;
            match made {
                Ok(r) => {
                    *self.active.lock().expect("active") = Some((r.id.clone(), cancel.clone()));
                    r
                }
                Err(e) => {
                    if reserve {
                        *self.active.lock().expect("active") = None;
                    }
                    return Err(e);
                }
            }
        };
        let me = self.clone();
        let r = run.clone();
        tokio::spawn(async move {
            let id = r.id.clone();
            let outcome = me.execute(r, cancel).await;
            *me.progress.lock().expect("progress") = None;
            if let Err(e) = outcome {
                me.runs_failed.fetch_add(1, Ordering::Relaxed);
                tracing::error!(run = %id, error = %e, "compilation failed");
                let _ = repo::set_run_state(&me.store, &id, "failed", &json!({}), Some(&e)).await;
            }
            *me.active.lock().expect("active") = None;
        });
        Ok(run)
    }

    pub fn cancel(&self, run_id: &str) -> bool {
        match self.active.lock().expect("active").as_ref() {
            Some((id, flag)) if id == run_id => {
                if !flag.swap(true, Ordering::SeqCst) {
                    tracing::info!(run = run_id, "cancellation requested; stopping at the next table");
                }
                true
            }
            _ => false,
        }
    }

    async fn execute(self: &Arc<Self>, run: Run, cancel: Arc<AtomicBool>) -> Result<(), String> {
        let started = Instant::now();
        // One run at a time, so the model failures counted while it runs are its own.
        let failures_before = self.llm.stats.failures.load(Ordering::Relaxed);
        let mut tables = repo::tables(&self.store).await.map_err(|e| e.to_string())?;
        let db = self.settings.database.clone();
        tables.sort_by_key(|t| commit_key(&db, t));
        let index = Arc::new(TableIndex::new(tables));
        let stored_q = repo::queries(&self.store, self.settings.max_queries).await.map_err(|e| e.to_string())?;
        let log = querylog::parse_log(&stored_q, &index);
        let committed = repo::table_results(&self.store, &run.id).await.map_err(|e| e.to_string())?;
        let scope = Arc::new(run_scope(&run.stats));
        let reuse: BTreeMap<String, TableResult> = if run.mode == "incremental" || scope.is_some() {
            match self.store.meta(repo::PUBLISHED).await.map_err(|e| e.to_string())? {
                Some(prev) if prev != run.id => repo::table_results(&self.store, &prev).await.map_err(|e| e.to_string())?,
                _ => BTreeMap::new(),
            }
        } else {
            BTreeMap::new()
        };
        let targets = Arc::new(fk_targets(&index));
        let queue = Arc::new(CommitQueue::default());
        let names = Arc::new(tokio::sync::Mutex::new(Names::default()));
        let workers = Arc::new(Semaphore::new(self.settings.workers.max(1)));
        // processed, reused, resumed, skipped, out of scope
        let counters = Arc::new([AtomicUsize::new(0), AtomicUsize::new(0), AtomicUsize::new(0), AtomicUsize::new(0), AtomicUsize::new(0)]);
        let total = index.tables.len();
        *self.progress.lock().expect("progress") = Some(Progress {
            run_id: run.id.clone(),
            phase: "tables".into(),
            tables_total: total,
            tables_done: 0,
            scope: scope.as_ref().as_ref().map(|s| s.len()),
        });
        let committed = Arc::new(committed);
        let reuse = Arc::new(reuse);
        let mut handles = vec![];
        for position in 0..total {
            let (me, index, targets, queue, names, workers, counters, committed, reuse, cancel) = (
                self.clone(),
                index.clone(),
                targets.clone(),
                queue.clone(),
                names.clone(),
                workers.clone(),
                counters.clone(),
                committed.clone(),
                reuse.clone(),
                cancel.clone(),
            );
            let run_id = run.id.clone();
            let (scope, incremental) = (scope.clone(), run.mode == "incremental");
            handles.push(tokio::spawn(async move {
                let outcome = async {
                    let mut slot = Slot::new(queue, position);
                    let t = &index.tables[position];
                    if let Some(done) = committed.get(&t.id) {
                        // Resuming: this table committed before the run stopped. Its names are given again in turn,
                        // from what the model proposed: a table before it that the stop skipped may own a name this
                        // one took then, and the names must not depend on where a run was interrupted.
                        slot.enter().await;
                        let r = {
                            let mut n = names.lock().await;
                            rename(done.clone(), &mut n, t)
                        };
                        let stored = if r.terms.iter().map(|x| &x.name).ne(done.terms.iter().map(|x| &x.name)) {
                            repo::put_table_result(&me.store, &run_id, position as i64, &r).await.map_err(|e| e.to_string())
                        } else {
                            Ok(())
                        };
                        counters[2].fetch_add(1, Ordering::Relaxed);
                        slot.exit();
                        return stored;
                    }
                    let _permit = workers.acquire().await.map_err(|e| e.to_string())?;
                    // Checked here, not before queueing: this is what turns a cancelled backlog into no-ops.
                    if cancel.load(Ordering::SeqCst) {
                        counters[3].fetch_add(1, Ordering::Relaxed);
                        slot.skip_if_unused();
                        return Ok(());
                    }
                    if t.columns.is_empty() {
                        tracing::warn!(table = %t.qualified(), "table has no columns — skipping");
                        slot.skip_if_unused();
                        return Ok(());
                    }
                    let fp = table_fingerprint(t, me.settings.probe_distinct_values);
                    let in_scope = scope.as_ref().as_ref().is_none_or(|s| s.contains(&t.id));
                    if !in_scope {
                        // Outside the run's scope: what the published run made of the table, or nothing.
                        counters[4].fetch_add(1, Ordering::Relaxed);
                        let Some(prev) = reuse.get(&t.id).cloned() else {
                            slot.skip_if_unused();
                            return Ok(());
                        };
                        drop(_permit);
                        slot.enter().await;
                        let r = {
                            let mut n = names.lock().await;
                            rename(prev, &mut n, t)
                        };
                        let stored = repo::put_table_result(&me.store, &run_id, position as i64, &r).await.map_err(|e| e.to_string());
                        slot.exit();
                        return stored;
                    }
                    let result = match reuse.get(&t.id).filter(|r| incremental && r.facts.fingerprint == fp) {
                        Some(prev) => {
                            counters[1].fetch_add(1, Ordering::Relaxed);
                            prev.clone()
                        }
                        None => {
                            tracing::info!(run = %run_id, position = position + 1, total, table = %t.qualified(), "processing table");
                            let empty = BTreeSet::new();
                            let r = process_table(&me.llm, t, &index, targets.get(&t.id).unwrap_or(&empty), &me.settings, &run_id).await;
                            counters[0].fetch_add(1, Ordering::Relaxed);
                            r
                        }
                    };
                    drop(_permit);
                    slot.enter().await;
                    let r = {
                        let mut n = names.lock().await;
                        rename(result, &mut n, t)
                    };
                    let stored = repo::put_table_result(&me.store, &run_id, position as i64, &r).await.map_err(|e| e.to_string());
                    slot.exit();
                    stored
                }
                .await;
                me.table_done(&run_id);
                outcome
            }));
        }
        let mut errors = vec![];
        for h in handles {
            match h.await {
                Ok(Ok(())) => {}
                Ok(Err(e)) => errors.push(e),
                Err(e) => errors.push(format!("table task: {e}")),
            }
        }
        let mut stats = json!({
            "tables": total,
            "processed": counters[0].load(Ordering::Relaxed),
            "reused": counters[1].load(Ordering::Relaxed),
            "resumed": counters[2].load(Ordering::Relaxed),
            "skipped": counters[3].load(Ordering::Relaxed),
            "queries": log.len(),
        });
        if let Some(s) = scope.as_ref() {
            stats["scope"] = json!(s);
            stats["out_of_scope"] = json!(counters[4].load(Ordering::Relaxed));
        }
        if let Some(e) = errors.first() {
            return Err(e.clone());
        }
        if cancel.load(Ordering::SeqCst) {
            stats["seconds"] = json!(started.elapsed().as_secs_f64());
            tracing::info!(run = %run.id, "compilation cancelled; committed tables are kept and the run can be resumed");
            repo::set_run_state(&self.store, &run.id, "cancelled", &stats, None).await.map_err(|e| e.to_string())?;
            return Ok(());
        }
        let results = repo::table_results(&self.store, &run.id).await.map_err(|e| e.to_string())?;
        let ontology = self.post_stages(&run, &index, results, &log, &cancel, &mut stats).await?;
        let Some(ontology) = ontology else {
            stats["seconds"] = json!(started.elapsed().as_secs_f64());
            repo::set_run_state(&self.store, &run.id, "cancelled", &stats, None).await.map_err(|e| e.to_string())?;
            return Ok(());
        };
        // Fail closed: a model call that failed (the gateway's rate limit or budget, the provider) leaves the
        // compiler's guesses where the model's answers belong, so such an ontology is never published. Its
        // table results go too, so resuming compiles those tables again rather than keeping the guesses.
        let failed = self.llm.stats.failures.load(Ordering::Relaxed).saturating_sub(failures_before);
        if failed > 0 {
            stats["seconds"] = json!(started.elapsed().as_secs_f64());
            stats["model_failures"] = json!(failed);
            let why = format!(
                "{failed} model call(s) failed (the AI gateway's rate limit or budget, or the provider); nothing was \
                 published. Raise the compiler agent's limits if needed and resume the run."
            );
            tracing::warn!(run = %run.id, failed, "model calls failed; the run is not published");
            repo::clear_table_results(&self.store, &run.id).await.map_err(|e| e.to_string())?;
            repo::set_run_state(&self.store, &run.id, "failed", &stats, Some(&why)).await.map_err(|e| e.to_string())?;
            return Ok(());
        }
        self.set_phase(&run.id, "publishing");
        repo::put_ontology(&self.store, &ontology).await.map_err(|e| e.to_string())?;
        stats["seconds"] = json!(started.elapsed().as_secs_f64());
        stats["model_calls"] = json!(self.llm.stats.calls.load(Ordering::Relaxed));
        repo::set_run_state(&self.store, &run.id, "completed", &stats, None).await.map_err(|e| e.to_string())?;
        self.runs_completed.fetch_add(1, Ordering::Relaxed);
        tracing::info!(run = %run.id, stats = %stats, "semantic compilation finished");
        Ok(())
    }

    /// FK resolution, SqlAttributes and bridge tables (`_run_semantic_compilation`); None when cancelled.
    async fn post_stages(
        &self,
        run: &Run,
        index: &TableIndex,
        mut results: BTreeMap<String, TableResult>,
        log: &[ParsedQuery],
        cancel: &AtomicBool,
        stats: &mut Value,
    ) -> Result<Option<Ontology>, String> {
        let usage = querylog::table_usage(log);
        for (id, r) in results.iter_mut() {
            if let Some((q, e, cols)) = usage.get(id) {
                r.facts.usage = Usage { queries: *q, executions: *e, columns: cols.clone() };
            }
        }
        if cancel.load(Ordering::SeqCst) {
            return Ok(None);
        }
        self.set_phase(&run.id, "relationships");
        let ctx = semantic_fk::Context { llm: &self.llm, tables: index, results: &results, run_id: &run.id };
        let declared = semantic_fk::declared(&ctx);
        let inferred = semantic_fk::inferred(&ctx, &declared).await;
        let (kept, pruned) = semantic_fk::prune_disjoint(&ctx, inferred);
        let mut rels = declared;
        for r in kept {
            if !rels.iter().any(|x| x.id == r.id) {
                rels.push(r);
            }
        }
        semantic_fk::merge_observed(&ctx, &mut rels, &querylog::observed_joins(log));
        semantic_fk::select_join_defaults(&ctx, &mut rels).await;
        rels.sort_by(|a, b| a.id.cmp(&b.id));
        stats["relationships"] = json!(rels.len());
        stats["relationships_pruned"] = json!(pruned.len());
        if cancel.load(Ordering::SeqCst) {
            return Ok(None);
        }
        self.set_phase(&run.id, "metrics");
        let sctx = sql_attributes::Ctx { llm: &self.llm, tables: index, results: &results, log, run_id: &run.id };
        let mut metrics = sql_attributes::suggest(&sctx).await;
        stats["metrics"] = json!(metrics.len());
        if cancel.load(Ordering::SeqCst) {
            return Ok(None);
        }
        self.set_phase(&run.id, "bridges");
        let (bridges, bridge_metrics) = sql_attributes::bridges(&sctx, &rels, &metrics).await;
        metrics.extend(bridge_metrics);
        stats["bridges"] = json!(bridges.len());
        for b in &bridges {
            if let Some(r) = results.get_mut(&b.table_id) {
                r.facts.is_junction = true;
                if r.facts.junction_rationale.is_empty() {
                    r.facts.junction_rationale = "every column is a foreign key".into();
                }
            }
        }
        let mut o = Ontology { run_id: run.id.clone(), version: run.version, ..Default::default() };
        for t in &index.tables {
            if let Some(r) = results.get(&t.id) {
                o.terms.extend(r.terms.iter().cloned());
                o.attributes.extend(r.attributes.iter().cloned());
                o.tables.push(r.facts.clone());
            }
        }
        stats["terms"] = json!(o.terms.len());
        stats["attributes"] = json!(o.attributes.len());
        o.relationships = rels;
        o.metrics = metrics;
        o.bridges = bridges;
        Ok(Some(o))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn commits_follow_canonical_order_whatever_finishes_first() {
        let q = Arc::new(CommitQueue::default());
        let order = Arc::new(tokio::sync::Mutex::new(vec![]));
        let mut hs = vec![];
        for pos in [2usize, 0, 1] {
            let (q, order) = (q.clone(), order.clone());
            hs.push(tokio::spawn(async move {
                // Later positions finish their "model work" first.
                tokio::time::sleep(Duration::from_millis(30 * (3 - pos as u64))).await;
                let mut slot = Slot::new(q, pos);
                slot.enter().await;
                order.lock().await.push(pos);
                slot.exit();
            }));
        }
        for h in hs {
            h.await.unwrap();
        }
        assert_eq!(*order.lock().await, vec![0, 1, 2]);
    }

    #[tokio::test]
    async fn skipping_releases_a_turn_without_waiting_for_it() {
        let q = Arc::new(CommitQueue::default());
        // Position 2 gives up its turn before 0 and 1 have had theirs: it must not block, and 3 still gets in.
        q.skip(2);
        let q3 = q.clone();
        let third = tokio::spawn(async move {
            let mut s = Slot::new(q3, 3);
            s.enter().await;
            s.exit();
        });
        for p in [0usize, 1] {
            let mut s = Slot::new(q.clone(), p);
            s.enter().await;
            s.exit();
        }
        tokio::time::timeout(Duration::from_secs(2), third).await.expect("position 3 got its turn").unwrap();
        assert_eq!(q.next(), 4);
        // A dropped, never-entered slot gives up its turn too (an early return or a failure).
        drop(Slot::new(q.clone(), 4));
        assert_eq!(q.next(), 5);
    }

    #[test]
    fn colliding_names_are_qualified_in_commit_order() {
        let t = |id: &str, schema: &str| Table {
            id: id.into(),
            catalog: "lake".into(),
            schema: schema.into(),
            name: "orders".into(),
            ..Default::default()
        };
        let mut n = Names::default();
        assert_eq!(n.allocate("Order", &t("a", "retail")), "Order");
        assert_eq!(n.allocate("Order", &t("b", "archive")), "Order (lake.archive.orders)");
        assert_eq!(n.allocate("Order", &t("a", "retail")), "Order", "a table keeps its own name");
        let key =
            |s: &str, name: &str| commit_key("db", &Table { id: "x".into(), schema: s.into(), name: name.into(), ..Default::default() });
        let mut keys = [key("sales", "zeta"), key("sales", "Alpha"), key("Retail", "b")];
        keys.sort();
        assert_eq!(keys.iter().map(|k| k.5.clone()).collect::<Vec<_>>(), vec!["b", "Alpha", "zeta"]);
    }

    fn engine(dir: &std::path::Path) -> Arc<Engine> {
        let store = Store::open(dir.join("o.db").to_str().unwrap(), None).unwrap();
        let llm = Llm::new(crate::llm::ModelConfig {
            chat_url: String::new(),
            chat_model: String::new(),
            embed_url: String::new(),
            embed_model: String::new(),
            api_key: crate::llm::Secret::default(),
            timeout: Duration::from_secs(1),
            retries: 0,
            max_inflight: 2,
            response_format: "json_schema".into(),
        })
        .unwrap();
        let settings = Settings { workers: 2, probe_distinct_values: true, max_queries: 100, domain: None, database: "db".into() };
        Arc::new(Engine::new(store, Arc::new(llm), settings))
    }

    async fn finish(e: &Arc<Engine>, id: &str) -> Run {
        for _ in 0..200 {
            let r = repo::get_run(&e.store, id).await.unwrap().unwrap();
            if r.state != "running" && e.active_run().is_none() {
                return r;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        panic!("the run did not finish");
    }

    #[tokio::test]
    async fn a_run_compiles_publishes_and_an_incremental_run_reuses_unchanged_tables() {
        use crate::model::{Column, ForeignKey};
        let dir = std::env::temp_dir().join(format!("onto-engine-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let e = engine(&dir);
        let col = |n: &str| Column { name: n.into(), data_type: "bigint".into(), ..Default::default() };
        let customers = Table {
            id: "C".into(),
            schema: "s".into(),
            name: "customers".into(),
            primary_key: vec!["id".into()],
            columns: vec![col("id")],
            ..Default::default()
        };
        let mut orders = Table {
            id: "O".into(),
            schema: "s".into(),
            name: "orders".into(),
            columns: vec![col("id"), col("customer_id"), col("amount")],
            ..Default::default()
        };
        orders.foreign_keys =
            vec![ForeignKey { columns: vec!["customer_id".into()], target_table: "s.customers".into(), target_columns: vec![] }];
        repo::put_tables(&e.store, &[customers, orders.clone()], &[]).await.unwrap();
        // One run at a time.
        let run = e.start("full", "tester", None).await.unwrap();
        assert!(matches!(e.start("full", "tester", None).await, Err(StartError::Busy(_))));
        let done = finish(&e, &run.id).await;
        assert_eq!(done.state, "completed", "{:?}", done);
        assert_eq!(done.stats["processed"], 2);
        let o = repo::published(&e.store).await.unwrap().unwrap();
        assert_eq!(o.run_id, run.id);
        // Without a model each table still gets a Term named after it; the declared key is a relationship and
        // its column is not an attribute.
        assert_eq!(o.terms.iter().map(|t| t.name.as_str()).collect::<BTreeSet<_>>(), ["Customers", "Orders"].into());
        assert!(!o.attributes.iter().any(|a| a.column == "customer_id"));
        let r = &o.relationships[0];
        assert_eq!((r.source_column.as_str(), r.target_table.as_str(), r.target_column.as_str()), ("customer_id", "C", "id"));
        assert_eq!(r.provenance[0].source, "catalog_fk");
        // Nothing changed: an incremental run reuses both tables; one changes: only it is processed again.
        let again = e.start("incremental", "tester", None).await.unwrap();
        let done = finish(&e, &again.id).await;
        assert_eq!((done.stats["processed"].as_i64(), done.stats["reused"].as_i64()), (Some(0), Some(2)));
        orders.description = Some("Orders placed".into());
        repo::put_tables(&e.store, &[orders], &[]).await.unwrap();
        let third = e.start("incremental", "tester", None).await.unwrap();
        let done = finish(&e, &third.id).await;
        assert_eq!((done.stats["processed"].as_i64(), done.stats["reused"].as_i64()), (Some(1), Some(1)));
        assert_eq!(third.version, 3);
        // A completed run is not resumable; an unknown one is not found.
        assert!(matches!(e.start("", "", Some(&run.id)).await, Err(StartError::NotResumable(_))));
        assert!(matches!(e.start("", "", Some("run_nope")).await, Err(StartError::NotFound)));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn a_scoped_run_compiles_its_tables_keeps_the_others_and_estimates_first() {
        use crate::model::Column;
        let dir = std::env::temp_dir().join(format!("onto-scope-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let e = engine(&dir);
        let col = |n: &str| Column { name: n.into(), data_type: "bigint".into(), ..Default::default() };
        let table = |id: &str, name: &str| Table {
            id: id.into(),
            schema: "s".into(),
            name: name.into(),
            primary_key: vec!["id".into()],
            columns: vec![col("id"), col("qty")],
            ..Default::default()
        };
        let (a, mut b, c) = (table("A", "alpha"), table("B", "beta"), table("C", "gamma"));
        repo::put_tables(&e.store, &[a.clone(), b.clone()], &[]).await.unwrap();
        // Before anything is published every table would be compiled; without a model, no model calls.
        let est = e.estimate("incremental", None).await.unwrap();
        assert_eq!((est["tables"].as_i64(), est["to_process"].as_i64(), est["reused"].as_i64()), (Some(2), Some(2), Some(0)));
        assert_eq!(est["model"]["chat_calls_max"], 0);
        let first = e.start("full", "tester", None).await.unwrap();
        assert_eq!(finish(&e, &first.id).await.state, "completed");
        assert!(e.progress(&first.id).is_none(), "progress is only kept while a run is active");
        // B changes and C is new; a run scoped to B and C compiles those two and keeps A as published.
        b.description = Some("changed".into());
        repo::put_tables(&e.store, &[b, c], &[]).await.unwrap();
        let scope: BTreeSet<String> = ["B".to_string(), "C".to_string()].into();
        let est = e.estimate("incremental", Some(&scope)).await.unwrap();
        assert_eq!((est["to_process"].as_i64(), est["out_of_scope"].as_i64()), (Some(2), Some(1)), "{est}");
        let est = e.estimate("incremental", None).await.unwrap();
        assert_eq!((est["to_process"].as_i64(), est["reused"].as_i64()), (Some(2), Some(1)), "{est}");
        let scoped = e.start_scoped("full", "tester", None, Some(vec!["B".into(), "C".into()])).await.unwrap();
        assert_eq!(scoped.stats["scope"], json!(["B", "C"]), "the scope is kept with the run, for a resume");
        let done = finish(&e, &scoped.id).await;
        assert_eq!(done.state, "completed", "{done:?}");
        assert_eq!((done.stats["processed"].as_i64(), done.stats["out_of_scope"].as_i64()), (Some(2), Some(1)), "{}", done.stats);
        let o = repo::published(&e.store).await.unwrap().unwrap();
        assert_eq!(o.run_id, scoped.id);
        assert_eq!(o.terms.iter().map(|t| t.table_id.as_str()).collect::<BTreeSet<_>>(), ["A", "B", "C"].into());
        // A table outside the scope that the published run never compiled is left out.
        repo::put_tables(&e.store, &[table("D", "delta")], &[]).await.unwrap();
        let only_a = e.start_scoped("incremental", "tester", None, Some(vec!["A".into()])).await.unwrap();
        let done = finish(&e, &only_a.id).await;
        assert_eq!((done.stats["reused"].as_i64(), done.stats["out_of_scope"].as_i64()), (Some(1), Some(3)), "{}", done.stats);
        let o = repo::published(&e.store).await.unwrap().unwrap();
        assert!(!o.terms.iter().any(|t| t.table_id == "D"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn a_resumed_run_names_colliding_terms_as_an_uninterrupted_run_would() {
        use crate::model::Column;
        let dir = std::env::temp_dir().join(format!("onto-resume-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let e = engine(&dir);
        let table = |id: &str, schema: &str| Table {
            id: id.into(),
            schema: schema.into(),
            name: "orders".into(),
            columns: vec![Column { name: "id".into(), data_type: "bigint".into(), ..Default::default() }],
            ..Default::default()
        };
        // Only b exists at first: its Term is "Orders".
        repo::put_tables(&e.store, &[table("B", "b")], &[]).await.unwrap();
        let first = e.start("full", "tester", None).await.unwrap();
        assert_eq!(finish(&e, &first.id).await.state, "completed");
        let b_alone = repo::table_results(&e.store, &first.id).await.unwrap().remove("B").unwrap();
        assert_eq!(b_alone.terms[0].name, "Orders");
        // a comes first in commit order. A run stopped after committing b alone (a was skipped) left b with "Orders".
        repo::put_tables(&e.store, &[table("A", "a")], &[]).await.unwrap();
        let stopped = repo::create_run(&e.store, &crate::model::new_id("run"), "full", "tester").await.unwrap();
        repo::put_table_result(&e.store, &stopped.id, 1, &b_alone).await.unwrap();
        repo::set_run_state(&e.store, &stopped.id, "cancelled", &json!({}), None).await.unwrap();
        // Resumed, a takes the plain name, as in a run that was never stopped, and b is qualified.
        e.start("full", "tester", Some(&stopped.id)).await.unwrap();
        let done = finish(&e, &stopped.id).await;
        assert_eq!(done.state, "completed", "{:?}", done);
        let o = repo::published(&e.store).await.unwrap().unwrap();
        let name = |id: &str| o.terms.iter().find(|t| t.table_id == id).map(|t| t.name.clone()).unwrap();
        assert_eq!(name("A"), "Orders");
        assert_eq!(name("B"), "Orders (b.orders)");
    }
}
