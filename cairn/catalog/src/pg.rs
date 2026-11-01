//! The catalog's state in PostgreSQL (`CAIRN_CATALOG_DATABASE_URL`), so several catalog replicas serve
//! one lakehouse.
//!
//! Three tables: `cairn_catalog_namespaces (key, properties)`, `cairn_catalog_entries (kind, ns, name,
//! metadata_location, metadata)` with kind `t` (table), `v` (view) or `s` (staged create), and
//! `cairn_catalog_info` (which warehouse this database belongs to). Every write is one transaction that
//! first takes a transaction-scoped advisory lock, so writers are serialized exactly as with the embedded
//! database: a commit's compare-and-set on each table's metadata location cannot interleave with another
//! replica's, and a multi-table commit applies to all of its tables or none. Reads are REPEATABLE READ
//! snapshots.

use std::collections::BTreeMap;
use std::sync::Mutex;

use crate::db::{ns_key, ns_levels, Change, Error, Result, TableRow, SEP};

const LOCK_ID: i64 = 0x6361_6972_6e63_6174; // "cairncat": the writer lock

fn pg_err(e: postgres::Error) -> Error {
    Error::Index(format!("catalog database: {e}"))
}

type Job = Box<dyn FnOnce(&mut postgres::Client) + Send>;

/// One connection, owned by a thread of its own: the synchronous client runs its own small runtime,
/// which must not run on a thread that drives the server's async tasks.
struct Conn {
    jobs: std::sync::mpsc::Sender<Job>,
    closed: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl Conn {
    fn run<T: Send + 'static>(&self, f: impl FnOnce(&mut postgres::Client) -> Result<T> + Send + 'static) -> Result<T> {
        let (tx, rx) = std::sync::mpsc::channel();
        self.jobs
            .send(Box::new(move |c| {
                let _ = tx.send(f(c));
            }))
            .map_err(|_| Error::Index("catalog database connection lost".into()))?;
        rx.recv().map_err(|_| Error::Index("catalog database connection lost".into()))?
    }

    fn is_closed(&self) -> bool {
        self.closed.load(std::sync::atomic::Ordering::SeqCst)
    }
}

pub struct Pg {
    url: String,
    pool: Mutex<Vec<Conn>>,
}

/// A write transaction: the advisory lock first, so writers from every replica take turns.
fn write<T>(c: &mut postgres::Client, f: impl FnOnce(&mut postgres::Transaction) -> Result<T>) -> Result<T> {
    let mut tx = c.transaction().map_err(pg_err)?;
    tx.execute("SELECT pg_advisory_xact_lock($1)", &[&LOCK_ID]).map_err(pg_err)?;
    let out = f(&mut tx)?;
    tx.commit().map_err(pg_err)?;
    Ok(out)
}

fn read<T>(c: &mut postgres::Client, f: impl FnOnce(&mut postgres::Transaction) -> Result<T>) -> Result<T> {
    let mut tx = c.build_transaction().isolation_level(postgres::IsolationLevel::RepeatableRead).read_only(true).start().map_err(pg_err)?;
    let out = f(&mut tx)?;
    tx.commit().map_err(pg_err)?;
    Ok(out)
}

fn has_ns(tx: &mut postgres::Transaction, key: &str) -> Result<bool> {
    Ok(tx.query_opt("SELECT 1 FROM cairn_catalog_namespaces WHERE key = $1", &[&key]).map_err(pg_err)?.is_some())
}

fn entry(tx: &mut postgres::Transaction, kind: &str, ns: &str, name: &str, lock: bool) -> Result<Option<TableRow>> {
    let sql = if lock {
        "SELECT metadata_location, metadata FROM cairn_catalog_entries WHERE kind = $1 AND ns = $2 AND name = $3 FOR UPDATE"
    } else {
        "SELECT metadata_location, metadata FROM cairn_catalog_entries WHERE kind = $1 AND ns = $2 AND name = $3"
    };
    Ok(tx.query_opt(sql, &[&kind, &ns, &name]).map_err(pg_err)?.map(|r| TableRow { metadata_location: r.get(0), metadata: r.get(1) }))
}

fn put(tx: &mut postgres::Transaction, kind: &str, ns: &str, name: &str, row: &TableRow) -> Result<()> {
    tx.execute(
        "INSERT INTO cairn_catalog_entries (kind, ns, name, metadata_location, metadata) VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (kind, ns, name) DO UPDATE SET metadata_location = EXCLUDED.metadata_location, metadata = EXCLUDED.metadata",
        &[&kind, &ns, &name, &row.metadata_location, &row.metadata],
    )
    .map_err(pg_err)?;
    Ok(())
}

fn remove(tx: &mut postgres::Transaction, kind: &str, ns: &str, name: &str) -> Result<Option<TableRow>> {
    Ok(tx
        .query_opt(
            "DELETE FROM cairn_catalog_entries WHERE kind = $1 AND ns = $2 AND name = $3 RETURNING metadata_location, metadata",
            &[&kind, &ns, &name],
        )
        .map_err(pg_err)?
        .map(|r| TableRow { metadata_location: r.get(0), metadata: r.get(1) }))
}

fn props(json: &str) -> Result<BTreeMap<String, String>> {
    Ok(serde_json::from_str(json)?)
}

impl Pg {
    /// Connects, creates the tables, and binds the database to `warehouse`: a database that belongs to
    /// another warehouse is refused, so two lakehouses never share one catalog database by mistake.
    pub fn open(url: &str, warehouse: &str) -> Result<Pg> {
        let db = Pg { url: url.to_string(), pool: Mutex::new(vec![]) };
        let c = db.connect()?;
        let wh = warehouse.trim_end_matches('/').to_string();
        c.run(move |c| {
            write(c, |tx| {
                tx.batch_execute(
                    "CREATE TABLE IF NOT EXISTS cairn_catalog_namespaces (key text PRIMARY KEY, properties text NOT NULL);
                     CREATE TABLE IF NOT EXISTS cairn_catalog_entries (
                         kind text NOT NULL, ns text NOT NULL, name text NOT NULL,
                         metadata_location text NOT NULL, metadata text NOT NULL,
                         PRIMARY KEY (kind, ns, name));
                     CREATE TABLE IF NOT EXISTS cairn_catalog_info (k text PRIMARY KEY, v text NOT NULL);",
                )
                .map_err(pg_err)?;
                let have: Option<String> =
                    tx.query_opt("SELECT v FROM cairn_catalog_info WHERE k = 'warehouse'", &[]).map_err(pg_err)?.map(|r| r.get(0));
                match have {
                    None => {
                        tx.execute("INSERT INTO cairn_catalog_info (k, v) VALUES ('warehouse', $1)", &[&wh]).map_err(pg_err)?;
                    }
                    Some(h) if h == wh => {}
                    Some(h) => {
                        return Err(Error::Index(format!(
                            "this catalog database belongs to the warehouse {h}, not {wh}: give each lakehouse its own database"
                        )))
                    }
                }
                Ok(())
            })
        })?;
        db.pool.lock().unwrap().push(c);
        Ok(db)
    }

    fn client(url: &str) -> Result<postgres::Client> {
        let cfg: postgres::Config = url.parse().map_err(|e| Error::Index(format!("CAIRN_CATALOG_DATABASE_URL: {e}")))?;
        if cfg.get_ssl_mode() == postgres::config::SslMode::Disable {
            return cfg.connect(postgres::NoTls).map_err(pg_err);
        }
        let mut roots = rustls::RootCertStore { roots: webpki_roots::TLS_SERVER_ROOTS.to_vec() };
        if let Ok(ca) = std::env::var("CAIRN_CATALOG_DATABASE_CA_FILE") {
            if !ca.is_empty() {
                for c in rustls::pki_types::pem::PemObject::pem_file_iter(&ca).map_err(|e| Error::Index(format!("{ca}: {e}")))?.flatten() {
                    let _ = roots.add(c);
                }
            }
        }
        let tls = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map_err(|e| Error::Index(e.to_string()))?
            .with_root_certificates(roots)
            .with_no_client_auth();
        cfg.connect(tokio_postgres_rustls::MakeRustlsConnect::new(tls)).map_err(pg_err)
    }

    fn connect(&self) -> Result<Conn> {
        let (jobs, rx) = std::sync::mpsc::channel::<Job>();
        let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<()>>();
        let closed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let url = self.url.clone();
        let flag = closed.clone();
        std::thread::Builder::new()
            .name("cairn-catalog-pg".into())
            .spawn(move || {
                let mut client = match Self::client(&url) {
                    Ok(c) => {
                        let _ = ready_tx.send(Ok(()));
                        c
                    }
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                        flag.store(true, std::sync::atomic::Ordering::SeqCst);
                        return;
                    }
                };
                while let Ok(job) = rx.recv() {
                    job(&mut client);
                    if client.is_closed() {
                        break;
                    }
                }
                flag.store(true, std::sync::atomic::Ordering::SeqCst);
            })
            .map_err(|e| Error::Index(e.to_string()))?;
        ready_rx.recv().map_err(|_| Error::Index("catalog database connection thread ended".into()))??;
        Ok(Conn { jobs, closed })
    }

    /// Runs `f` on a pooled connection (a new one when the pool is empty or its connection died).
    fn run<T: Send + 'static>(&self, f: impl FnOnce(&mut postgres::Client) -> Result<T> + Send + 'static) -> Result<T> {
        let pooled = self.pool.lock().unwrap().pop();
        let c = match pooled {
            Some(c) if !c.is_closed() => c,
            _ => self.connect()?,
        };
        let out = c.run(f);
        let mut p = self.pool.lock().unwrap();
        if p.len() < 16 && !c.is_closed() {
            p.push(c);
        }
        out
    }

    // ------------------------------------------------------------------------------------ namespaces

    pub fn create_namespace(&self, levels: &[String], properties: BTreeMap<String, String>) -> Result<()> {
        let key = ns_key(levels);
        let v = serde_json::to_string(&properties)?;
        self.run(move |c| {
            write(c, |tx| {
                if has_ns(tx, &key)? {
                    return Err(Error::NamespaceExists);
                }
                tx.execute("INSERT INTO cairn_catalog_namespaces (key, properties) VALUES ($1, $2)", &[&key, &v]).map_err(pg_err)?;
                Ok(())
            })
        })
    }

    pub fn namespace(&self, levels: &[String]) -> Result<BTreeMap<String, String>> {
        let key = ns_key(levels);
        self.run(move |c| {
            let r = c.query_opt("SELECT properties FROM cairn_catalog_namespaces WHERE key = $1", &[&key]).map_err(pg_err)?;
            props(&r.ok_or(Error::NoSuchNamespace)?.get::<_, String>(0))
        })
    }

    pub fn list_namespaces(&self, parent: &[String]) -> Result<Vec<Vec<String>>> {
        let key = ns_key(parent);
        let depth = parent.len();
        let parent = parent.to_vec();
        self.run(move |c| {
            read(c, |tx| {
                if depth > 0 && !has_ns(tx, &key)? {
                    return Err(Error::NoSuchNamespace);
                }
                let prefix = if depth == 0 { String::new() } else { format!("{key}{SEP}") };
                let rows = tx
                    .query("SELECT key FROM cairn_catalog_namespaces WHERE starts_with(key, $1) ORDER BY key COLLATE \"C\"", &[&prefix])
                    .map_err(pg_err)?;
                Ok(rows.iter().map(|r| ns_levels(r.get::<_, &str>(0))).filter(|l| l.len() == depth + 1 && l.starts_with(&parent)).collect())
            })
        })
    }

    pub fn update_namespace(
        &self,
        levels: &[String],
        removals: &[String],
        updates: &BTreeMap<String, String>,
    ) -> Result<(Vec<String>, Vec<String>, Vec<String>)> {
        let key = ns_key(levels);
        let (removals, updates) = (removals.to_vec(), updates.clone());
        self.run(move |c| {
            write(c, |tx| {
                let r = tx
                    .query_opt("SELECT properties FROM cairn_catalog_namespaces WHERE key = $1 FOR UPDATE", &[&key])
                    .map_err(pg_err)?
                    .ok_or(Error::NoSuchNamespace)?;
                let mut p = props(&r.get::<_, String>(0))?;
                let (mut removed, mut missing) = (vec![], vec![]);
                for k in &removals {
                    if p.remove(k).is_some() {
                        removed.push(k.clone());
                    } else {
                        missing.push(k.clone());
                    }
                }
                for (k, v) in &updates {
                    p.insert(k.clone(), v.clone());
                }
                let v = serde_json::to_string(&p)?;
                tx.execute("UPDATE cairn_catalog_namespaces SET properties = $2 WHERE key = $1", &[&key, &v]).map_err(pg_err)?;
                Ok((updates.keys().cloned().collect(), removed, missing))
            })
        })
    }

    pub fn drop_namespace(&self, levels: &[String]) -> Result<()> {
        let key = ns_key(levels);
        self.run(move |c| {
            write(c, |tx| {
                if !has_ns(tx, &key)? {
                    return Err(Error::NoSuchNamespace);
                }
                let child = format!("{key}{SEP}");
                let children =
                    tx.query_opt("SELECT 1 FROM cairn_catalog_namespaces WHERE starts_with(key, $1) LIMIT 1", &[&child]).map_err(pg_err)?;
                let entries = tx
                    .query_opt("SELECT 1 FROM cairn_catalog_entries WHERE kind IN ('t', 'v') AND ns = $1 LIMIT 1", &[&key])
                    .map_err(pg_err)?;
                if children.is_some() || entries.is_some() {
                    return Err(Error::NamespaceNotEmpty);
                }
                tx.execute("DELETE FROM cairn_catalog_entries WHERE kind = 's' AND ns = $1", &[&key]).map_err(pg_err)?;
                tx.execute("DELETE FROM cairn_catalog_namespaces WHERE key = $1", &[&key]).map_err(pg_err)?;
                Ok(())
            })
        })
    }

    // ------------------------------------------------------------------------------------ tables and views

    pub fn list(&self, kind: &'static str, levels: &[String]) -> Result<Vec<String>> {
        let key = ns_key(levels);
        self.run(move |c| {
            read(c, |tx| {
                if !has_ns(tx, &key)? {
                    return Err(Error::NoSuchNamespace);
                }
                let rows = tx
                    .query("SELECT name FROM cairn_catalog_entries WHERE kind = $1 AND ns = $2 ORDER BY name COLLATE \"C\"", &[&kind, &key])
                    .map_err(pg_err)?;
                Ok(rows.iter().map(|r| r.get(0)).collect())
            })
        })
    }

    pub fn get(&self, kind: &'static str, levels: &[String], name: &str) -> Result<TableRow> {
        let key = ns_key(levels);
        let name = name.to_string();
        self.run(move |c| {
            read(c, |tx| {
                if !has_ns(tx, &key)? {
                    return Err(Error::NoSuchNamespace);
                }
                entry(tx, kind, &key, &name, false)?.ok_or(if kind == "v" { Error::NoSuchView } else { Error::NoSuchTable })
            })
        })
    }

    pub fn commit(&self, changes: &[Change]) -> Result<()> {
        let changes: Vec<(String, String, Option<String>, TableRow)> =
            changes.iter().map(|c| (c.namespace.clone(), c.name.clone(), c.expected.clone(), c.new.clone())).collect();
        self.run(move |c| {
            write(c, |tx| {
                for (i, (ns, name, expected, new)) in changes.iter().enumerate() {
                    if !has_ns(tx, ns)? {
                        return Err(Error::NoSuchNamespace);
                    }
                    if expected.is_none() && entry(tx, "v", ns, name, false)?.is_some() {
                        return Err(Error::ViewExists);
                    }
                    let cur = entry(tx, "t", ns, name, true)?;
                    match (expected, &cur) {
                        (None, Some(_)) => return Err(Error::TableExists),
                        (Some(_), None) => return Err(Error::NoSuchTable),
                        (Some(want), Some(have)) if *want != have.metadata_location => return Err(Error::Conflict(i)),
                        _ => {}
                    }
                    put(tx, "t", ns, name, new)?;
                }
                for (ns, name, _, _) in &changes {
                    remove(tx, "s", ns, name)?;
                }
                Ok(())
            })
        })
    }

    pub fn drop(&self, kind: &'static str, levels: &[String], name: &str) -> Result<TableRow> {
        let key = ns_key(levels);
        let name = name.to_string();
        self.run(move |c| {
            write(c, |tx| remove(tx, kind, &key, &name)?.ok_or(if kind == "v" { Error::NoSuchView } else { Error::NoSuchTable }))
        })
    }

    /// Renames a table (`t`) or view (`v`); a name is either a table's or a view's, never both.
    pub fn rename(&self, kind: &'static str, from: (&[String], &str), to: (&[String], &str)) -> Result<()> {
        let (fk, tk) = (ns_key(from.0), ns_key(to.0));
        let (fname, tname) = (from.1.to_string(), to.1.to_string());
        let other = if kind == "v" { "t" } else { "v" };
        self.run(move |c| {
            write(c, |tx| {
                if !has_ns(tx, &tk)? {
                    return Err(Error::NoSuchNamespace);
                }
                let (exists, other_exists, missing) = if kind == "v" {
                    (Error::ViewExists, Error::TableExists, Error::NoSuchView)
                } else {
                    (Error::TableExists, Error::ViewExists, Error::NoSuchTable)
                };
                if entry(tx, other, &tk, &tname, false)?.is_some() {
                    return Err(other_exists);
                }
                if entry(tx, kind, &tk, &tname, false)?.is_some() {
                    return Err(exists);
                }
                let row = remove(tx, kind, &fk, &fname)?.ok_or(missing)?;
                put(tx, kind, &tk, &tname, &row)
            })
        })
    }

    pub fn commit_view(&self, levels: &[String], name: &str, expected: Option<&str>, new: &TableRow) -> Result<()> {
        let key = ns_key(levels);
        let (name, expected, new) = (name.to_string(), expected.map(str::to_string), new.clone());
        self.run(move |c| {
            write(c, |tx| {
                if !has_ns(tx, &key)? {
                    return Err(Error::NoSuchNamespace);
                }
                if expected.is_none() && entry(tx, "t", &key, &name, false)?.is_some() {
                    return Err(Error::TableExists);
                }
                let cur = entry(tx, "v", &key, &name, true)?;
                match (&expected, &cur) {
                    (None, Some(_)) => return Err(Error::ViewExists),
                    (Some(_), None) => return Err(Error::NoSuchView),
                    (Some(want), Some(have)) if *want != have.metadata_location => return Err(Error::Conflict(0)),
                    _ => {}
                }
                put(tx, "v", &key, &name, &new)
            })
        })
    }

    pub fn stage(&self, levels: &[String], name: &str, metadata: &str) -> Result<()> {
        let key = ns_key(levels);
        let row = TableRow { metadata_location: String::new(), metadata: metadata.to_string() };
        let name = name.to_string();
        self.run(move |c| {
            write(c, |tx| {
                if !has_ns(tx, &key)? {
                    return Err(Error::NoSuchNamespace);
                }
                put(tx, "s", &key, &name, &row)
            })
        })
    }

    pub fn staged(&self, levels: &[String], name: &str) -> Result<Option<String>> {
        let key = ns_key(levels);
        let name = name.to_string();
        self.run(move |c| read(c, |tx| Ok(entry(tx, "s", &key, &name, false)?.map(|r| r.metadata))))
    }
}
