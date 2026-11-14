// SPDX-License-Identifier: Apache-2.0
//! Where the compiler keeps the catalog snapshot, the query log, compilation runs and the ontology each run
//! produced: PostgreSQL (`CAIRN_ONTOLOGY_DATABASE_URL`), or a SQLite file for development and tests.
//!
//! Six tables, the same on both: `onto_catalog` (one row per table, its JSON), `onto_queries` (one row per
//! distinct query shape, with its counters), `onto_runs`, `onto_table_results` (each table's committed result
//! in a run, so an interrupted run resumes where it stopped), `onto_elements` (a finished run's ontology,
//! one row per term, attribute, relationship, metric, bridge and table fact) and `onto_meta`.
//!
//! The database is used from one thread of its own: the synchronous PostgreSQL client runs a small runtime
//! that must not run on a thread driving the server's async tasks, and SQLite connections are not `Sync`.

use std::sync::mpsc;

use serde::{de::DeserializeOwned, Serialize};

#[derive(Debug)]
pub struct StoreError(pub String);

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "ontology database: {}", self.0)
    }
}

pub type Result<T> = std::result::Result<T, StoreError>;

/// A parameter.
#[derive(Clone, Debug)]
pub enum P {
    T(String),
    I(i64),
    Null,
}

impl From<&str> for P {
    fn from(s: &str) -> P {
        P::T(s.to_string())
    }
}
impl From<String> for P {
    fn from(s: String) -> P {
        P::T(s)
    }
}
impl From<&String> for P {
    fn from(s: &String) -> P {
        P::T(s.clone())
    }
}
impl From<i64> for P {
    fn from(i: i64) -> P {
        P::I(i)
    }
}
impl From<Option<i64>> for P {
    fn from(i: Option<i64>) -> P {
        i.map(P::I).unwrap_or(P::Null)
    }
}
impl From<Option<String>> for P {
    fn from(s: Option<String>) -> P {
        s.map(P::T).unwrap_or(P::Null)
    }
}

/// A value read back.
#[derive(Clone, Debug, PartialEq)]
pub enum V {
    T(String),
    I(i64),
    Null,
}

impl V {
    pub fn text(&self) -> String {
        match self {
            V::T(s) => s.clone(),
            V::I(i) => i.to_string(),
            V::Null => String::new(),
        }
    }
    pub fn int(&self) -> Option<i64> {
        match self {
            V::I(i) => Some(*i),
            V::T(s) => s.parse().ok(),
            V::Null => None,
        }
    }
}

pub type Row = Vec<V>;

enum Backend {
    Pg(Box<postgres::Client>),
    Lite(rusqlite::Connection),
}

const SCHEMA: &[&str] = &[
    "CREATE TABLE IF NOT EXISTS onto_catalog (id TEXT PRIMARY KEY, doc TEXT NOT NULL, shape TEXT NOT NULL, content TEXT NOT NULL, updated_ms BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS onto_queries (fingerprint TEXT PRIMARY KEY, id TEXT NOT NULL, sql TEXT NOT NULL, total_count BIGINT NOT NULL, months TEXT NOT NULL, users TEXT NOT NULL, first_ms BIGINT NOT NULL, last_ms BIGINT NOT NULL, dialect TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS onto_runs (id TEXT PRIMARY KEY, version BIGINT NOT NULL, state TEXT NOT NULL, mode TEXT NOT NULL, started_ms BIGINT NOT NULL, finished_ms BIGINT, stats TEXT NOT NULL, error TEXT, requested_by TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS onto_table_results (run_id TEXT NOT NULL, table_id TEXT NOT NULL, position BIGINT NOT NULL, fingerprint TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (run_id, table_id))",
    "CREATE TABLE IF NOT EXISTS onto_elements (run_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (run_id, kind, id))",
    "CREATE TABLE IF NOT EXISTS onto_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
];

fn lite_sql(sql: &str) -> String {
    // `$1` → `?1`: SQLite's positional parameters.
    let mut out = String::with_capacity(sql.len());
    let mut chars = sql.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '$' && chars.peek().is_some_and(|n| n.is_ascii_digit()) {
            out.push('?');
        } else {
            out.push(c);
        }
    }
    out
}

/// A PostgreSQL column as a value: integers (int2, int4, int8) as `V::I`, the rest as text. A column of another
/// type is an error for that query, never a panic: a panic would end the database thread, and with it every later
/// query (`SELECT 1`, the readiness check, is int4).
fn pg_value(r: &postgres::Row, i: usize) -> std::result::Result<V, String> {
    use postgres::types::Type;
    let t = r.columns()[i].type_().clone();
    let v = if t == Type::INT8 {
        r.try_get::<_, Option<i64>>(i).map(|o| o.map(V::I))
    } else if t == Type::INT4 {
        r.try_get::<_, Option<i32>>(i).map(|o| o.map(|x| V::I(i64::from(x))))
    } else if t == Type::INT2 {
        r.try_get::<_, Option<i16>>(i).map(|o| o.map(|x| V::I(i64::from(x))))
    } else {
        r.try_get::<_, Option<String>>(i).map(|o| o.map(V::T))
    };
    v.map(|o| o.unwrap_or(V::Null)).map_err(|e| format!("column {i} ({t}): {e}"))
}

impl Backend {
    fn exec(&mut self, sql: &str, params: &[P]) -> std::result::Result<u64, String> {
        match self {
            Backend::Pg(c) => {
                let owned: Vec<Box<dyn postgres::types::ToSql + Sync>> = params.iter().map(pg_param).collect();
                let refs: Vec<&(dyn postgres::types::ToSql + Sync)> = owned.iter().map(|b| b.as_ref()).collect();
                c.execute(sql, &refs).map_err(|e| e.to_string())
            }
            Backend::Lite(c) => {
                let vals: Vec<rusqlite::types::Value> = params.iter().map(lite_param).collect();
                c.execute(&lite_sql(sql), rusqlite::params_from_iter(vals.iter())).map(|n| n as u64).map_err(|e| e.to_string())
            }
        }
    }

    fn query(&mut self, sql: &str, params: &[P]) -> std::result::Result<Vec<Row>, String> {
        match self {
            Backend::Pg(c) => {
                let owned: Vec<Box<dyn postgres::types::ToSql + Sync>> = params.iter().map(pg_param).collect();
                let refs: Vec<&(dyn postgres::types::ToSql + Sync)> = owned.iter().map(|b| b.as_ref()).collect();
                let rows = c.query(sql, &refs).map_err(|e| e.to_string())?;
                rows.iter().map(|r| (0..r.len()).map(|i| pg_value(r, i)).collect::<std::result::Result<Row, String>>()).collect()
            }
            Backend::Lite(c) => {
                let vals: Vec<rusqlite::types::Value> = params.iter().map(lite_param).collect();
                let mut st = c.prepare(&lite_sql(sql)).map_err(|e| e.to_string())?;
                let n = st.column_count();
                let rows = st
                    .query_map(rusqlite::params_from_iter(vals.iter()), |r| {
                        (0..n)
                            .map(|i| {
                                Ok(match r.get_ref(i)? {
                                    rusqlite::types::ValueRef::Integer(x) => V::I(x),
                                    rusqlite::types::ValueRef::Null => V::Null,
                                    rusqlite::types::ValueRef::Text(t) => V::T(String::from_utf8_lossy(t).into_owned()),
                                    rusqlite::types::ValueRef::Real(f) => V::T(f.to_string()),
                                    rusqlite::types::ValueRef::Blob(b) => V::T(String::from_utf8_lossy(b).into_owned()),
                                })
                            })
                            .collect::<rusqlite::Result<Row>>()
                    })
                    .map_err(|e| e.to_string())?;
                rows.collect::<rusqlite::Result<Vec<Row>>>().map_err(|e| e.to_string())
            }
        }
    }

    fn batch(&mut self, sql: &str) -> std::result::Result<(), String> {
        match self {
            Backend::Pg(c) => c.batch_execute(sql).map_err(|e| e.to_string()),
            Backend::Lite(c) => c.execute_batch(sql).map_err(|e| e.to_string()),
        }
    }

    /// Runs `stmts` in one transaction.
    fn transaction(&mut self, stmts: &[(String, Vec<P>)]) -> std::result::Result<(), String> {
        self.batch("BEGIN")?;
        for (sql, params) in stmts {
            if let Err(e) = self.exec(sql, params) {
                let _ = self.batch("ROLLBACK");
                return Err(e);
            }
        }
        self.batch("COMMIT")
    }
}

/// A NULL of whatever type the statement expects (a typed `None` would be refused for a column of another type).
#[derive(Debug)]
struct AnyNull;

impl postgres::types::ToSql for AnyNull {
    fn to_sql(
        &self,
        _: &postgres::types::Type,
        _: &mut postgres::types::private::BytesMut,
    ) -> std::result::Result<postgres::types::IsNull, Box<dyn std::error::Error + Sync + Send>> {
        Ok(postgres::types::IsNull::Yes)
    }

    fn accepts(_: &postgres::types::Type) -> bool {
        true
    }

    postgres::types::to_sql_checked!();
}

fn pg_param(p: &P) -> Box<dyn postgres::types::ToSql + Sync> {
    match p {
        P::T(s) => Box::new(s.clone()),
        P::I(i) => Box::new(*i),
        P::Null => Box::new(AnyNull),
    }
}

fn lite_param(p: &P) -> rusqlite::types::Value {
    match p {
        P::T(s) => rusqlite::types::Value::Text(s.clone()),
        P::I(i) => rusqlite::types::Value::Integer(*i),
        P::Null => rusqlite::types::Value::Null,
    }
}

type Job = Box<dyn FnOnce(&mut Backend) + Send>;

#[derive(Clone)]
pub struct Store {
    jobs: mpsc::Sender<Job>,
    pub kind: &'static str,
}

fn pg_client(url: &str, ca_file: Option<&str>) -> std::result::Result<postgres::Client, String> {
    let cfg: postgres::Config = url.parse().map_err(|e| format!("database URL: {e}"))?;
    if cfg.get_ssl_mode() == postgres::config::SslMode::Disable {
        return cfg.connect(postgres::NoTls).map_err(|e| e.to_string());
    }
    let mut roots = rustls::RootCertStore { roots: webpki_roots::TLS_SERVER_ROOTS.to_vec() };
    if let Some(ca) = ca_file.filter(|c| !c.is_empty()) {
        for c in rustls::pki_types::pem::PemObject::pem_file_iter(ca).map_err(|e| format!("{ca}: {e}"))?.flatten() {
            let _ = roots.add(c);
        }
    }
    let tls = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .with_root_certificates(roots)
        .with_no_client_auth();
    cfg.connect(tokio_postgres_rustls::MakeRustlsConnect::new(tls)).map_err(|e| e.to_string())
}

impl Store {
    /// `postgres://…` (or `postgresql://…`), or a SQLite path (`sqlite:/path`, or a plain path).
    pub fn open(url: &str, ca_file: Option<&str>) -> Result<Store> {
        let is_pg = url.starts_with("postgres://") || url.starts_with("postgresql://") || url.contains("host=");
        let (tx, rx) = mpsc::channel::<Job>();
        let (ready_tx, ready_rx) = mpsc::channel::<std::result::Result<(), String>>();
        let url = url.to_string();
        let ca = ca_file.map(str::to_string);
        std::thread::Builder::new()
            .name("cairn-ontology-db".into())
            .spawn(move || {
                let backend = if is_pg {
                    pg_client(&url, ca.as_deref()).map(|c| Backend::Pg(Box::new(c)))
                } else {
                    let path = url.strip_prefix("sqlite://").or_else(|| url.strip_prefix("sqlite:")).unwrap_or(&url).to_string();
                    rusqlite::Connection::open(path).map(Backend::Lite).map_err(|e| e.to_string())
                };
                let mut b = match backend {
                    Ok(b) => b,
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                        return;
                    }
                };
                if let Backend::Lite(c) = &b {
                    let _ = c.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;");
                }
                let init = SCHEMA.iter().try_for_each(|s| b.batch(s));
                let _ = ready_tx.send(init);
                while let Ok(job) = rx.recv() {
                    job(&mut b);
                    if let Backend::Pg(c) = &b {
                        if c.is_closed() {
                            tracing::error!("the ontology database connection closed");
                            break;
                        }
                    }
                }
            })
            .map_err(|e| StoreError(e.to_string()))?;
        ready_rx.recv().map_err(|_| StoreError("the database thread ended".into()))?.map_err(StoreError)?;
        Ok(Store { jobs: tx, kind: if is_pg { "postgres" } else { "sqlite" } })
    }

    async fn run<T: Send + 'static>(&self, f: impl FnOnce(&mut Backend) -> std::result::Result<T, String> + Send + 'static) -> Result<T> {
        let (tx, rx) = tokio::sync::oneshot::channel();
        self.jobs
            .send(Box::new(move |b| {
                let _ = tx.send(f(b));
            }))
            .map_err(|_| StoreError("the database connection is gone".into()))?;
        rx.await.map_err(|_| StoreError("the database connection is gone".into()))?.map_err(StoreError)
    }

    pub async fn query(&self, sql: &str, params: Vec<P>) -> Result<Vec<Row>> {
        let sql = sql.to_string();
        self.run(move |b| b.query(&sql, &params)).await
    }

    pub async fn exec(&self, sql: &str, params: Vec<P>) -> Result<u64> {
        let sql = sql.to_string();
        self.run(move |b| b.exec(&sql, &params)).await
    }

    pub async fn transaction(&self, stmts: Vec<(String, Vec<P>)>) -> Result<()> {
        self.run(move |b| b.transaction(&stmts)).await
    }

    pub async fn ping(&self) -> Result<()> {
        self.query("SELECT 1", vec![]).await.map(|_| ())
    }

    // ---- meta ----------------------------------------------------------------------------------
    pub async fn meta(&self, key: &str) -> Result<Option<String>> {
        Ok(self.query("SELECT value FROM onto_meta WHERE key = $1", vec![key.into()]).await?.first().map(|r| r[0].text()))
    }

    #[cfg(test)]
    pub async fn set_meta(&self, key: &str, value: &str) -> Result<()> {
        self.exec(
            "INSERT INTO onto_meta (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            vec![key.into(), value.into()],
        )
        .await
        .map(|_| ())
    }
}

pub fn to_json<T: Serialize>(v: &T) -> String {
    serde_json::to_string(v).unwrap_or_else(|_| "null".into())
}

pub fn from_json<T: DeserializeOwned>(s: &str) -> Result<T> {
    serde_json::from_str(s).map_err(|e| StoreError(format!("stored JSON: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn sqlite_round_trip_and_transactions() {
        let dir = std::env::temp_dir().join(format!("onto-store-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let s = Store::open(dir.join("o.db").to_str().unwrap(), None).unwrap();
        s.set_meta("published", "run_1").await.unwrap();
        s.set_meta("published", "run_2").await.unwrap();
        assert_eq!(s.meta("published").await.unwrap().as_deref(), Some("run_2"));
        let bad = vec![
            ("INSERT INTO onto_meta (key, value) VALUES ($1, $2)".to_string(), vec![P::from("a"), P::from("1")]),
            ("INSERT INTO onto_meta (key, value) VALUES ($1, $2)".to_string(), vec![P::from("a"), P::from("2")]),
        ];
        assert!(s.transaction(bad).await.is_err());
        assert_eq!(s.meta("a").await.unwrap(), None, "a failed transaction leaves nothing behind");
        let rows = s.query("SELECT $1, $2", vec![P::I(7), P::Null]).await.unwrap();
        assert_eq!(rows[0], vec![V::I(7), V::Null]);
        assert_eq!(lite_sql("a = $1 AND b = $12"), "a = ?1 AND b = ?12");
        let _ = std::fs::remove_dir_all(dir);
    }
}
