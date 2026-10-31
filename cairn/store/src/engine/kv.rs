//! The engine's metadata store: redb (embedded, the default) or PostgreSQL (`CAIRN_STORE_META`).
//!
//! The engine uses redb's typed-table API (transactions, `open_table`, `get`/`insert`/`remove`, ordered
//! ranges). This module offers the same API over either database, so the engine's logic — one
//! transaction per change, written with its events and audit entry — is the same on both.
//!
//! On PostgreSQL every table lives in one table, `cairn_store_kv (tbl, k, kr, v)`: `k` is an
//! order-preserving encoding of the key (so ranges are `k BETWEEN ...` in key order), `kr` the key in
//! redb's encoding (decoded back into the engine's types) and `v` the value. Write transactions take a
//! transaction-scoped advisory lock, so writers are serialized exactly as redb serializes them; read
//! transactions are REPEATABLE READ snapshots. A store's data directory and its metadata database are
//! bound to each other by a store id, so two stores never share one metadata database by mistake.

use std::borrow::Borrow;
use std::cell::{Cell, RefCell};
use std::collections::VecDeque;
use std::marker::PhantomData;
use std::ops::{Bound, RangeBounds};
use std::sync::Mutex;

use redb::{Key, ReadableTable as _, ReadableTableMetadata as _, TableDefinition, TableHandle as _, Value};

use super::{Error, Result};

const LOCK_ID: i64 = 0x6361_6972_6e73_7431; // "cairnst1": the writer lock
const PAGE: i64 = 512;

fn pg_err(e: postgres::Error) -> Error {
    Error::Index(format!("metadata database: {e}"))
}

// ------------------------------------------------------------------------------- order-preserving keys

/// Keys encoded so that byte order is key order (strings escaped and terminated, integers big-endian).
pub trait OrdKey: Key + 'static {
    fn ord(v: &Self::SelfType<'_>, out: &mut Vec<u8>);
}

fn ord_str(s: &str, out: &mut Vec<u8>) {
    for b in s.bytes() {
        if b == 0 {
            out.extend_from_slice(&[0, 0xff]);
        } else {
            out.push(b);
        }
    }
    out.extend_from_slice(&[0, 1]);
}

impl OrdKey for &'static str {
    fn ord(v: &&str, out: &mut Vec<u8>) {
        ord_str(v, out)
    }
}
impl OrdKey for u64 {
    fn ord(v: &u64, out: &mut Vec<u8>) {
        out.extend_from_slice(&v.to_be_bytes())
    }
}
impl OrdKey for (&'static str, &'static str) {
    fn ord(v: &(&str, &str), out: &mut Vec<u8>) {
        ord_str(v.0, out);
        ord_str(v.1, out);
    }
}
impl OrdKey for (&'static str, &'static str, &'static str) {
    fn ord(v: &(&str, &str, &str), out: &mut Vec<u8>) {
        ord_str(v.0, out);
        ord_str(v.1, out);
        ord_str(v.2, out);
    }
}
impl OrdKey for (&'static str, u32) {
    fn ord(v: &(&str, u32), out: &mut Vec<u8>) {
        ord_str(v.0, out);
        out.extend_from_slice(&v.1.to_be_bytes());
    }
}

fn ord_of<K: OrdKey>(k: &K::SelfType<'_>) -> Vec<u8> {
    let mut out = Vec::with_capacity(32);
    K::ord(k, &mut out);
    out
}

// ------------------------------------------------------------------------------------------- guards

/// A key or value read from either database.
pub enum Guard<'g, V: Value + 'static> {
    R(redb::AccessGuard<'g, V>),
    O(Vec<u8>, PhantomData<V>),
}

impl<V: Value + 'static> Guard<'_, V> {
    pub fn value(&self) -> V::SelfType<'_> {
        match self {
            Guard::R(g) => g.value(),
            Guard::O(b, _) => V::from_bytes(b),
        }
    }
}

// --------------------------------------------------------------------------------------- PostgreSQL

pub struct PgDb {
    url: String,
    pool: Mutex<Vec<Conn>>,
}

type Job = Box<dyn FnOnce(&mut postgres::Client) + Send>;

/// One connection, owned by a thread of its own: the synchronous client runs its own small runtime,
/// which must not run on a thread that drives async tasks, and the engine is called from both kinds.
struct Conn {
    jobs: std::sync::mpsc::Sender<Job>,
    closed: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl Conn {
    fn run<T: Send + 'static>(
        &self,
        f: impl FnOnce(&mut postgres::Client) -> std::result::Result<T, postgres::Error> + Send + 'static,
    ) -> Result<T> {
        let (tx, rx) = std::sync::mpsc::channel();
        self.jobs
            .send(Box::new(move |c| {
                let _ = tx.send(f(c));
            }))
            .map_err(|_| Error::Index("metadata database connection lost".into()))?;
        rx.recv().map_err(|_| Error::Index("metadata database connection lost".into()))?.map_err(pg_err)
    }

    fn is_closed(&self) -> bool {
        self.closed.load(std::sync::atomic::Ordering::SeqCst)
    }
}

impl PgDb {
    pub fn open(url: &str) -> Result<PgDb> {
        let db = PgDb { url: url.to_string(), pool: Mutex::new(vec![]) };
        let c = db.connect()?;
        c.run(|c| {
            c.batch_execute(
                "CREATE TABLE IF NOT EXISTS cairn_store_kv (
                     tbl text NOT NULL, k bytea NOT NULL, kr bytea NOT NULL, v bytea NOT NULL,
                     PRIMARY KEY (tbl, k))",
            )
        })?;
        db.pool.lock().unwrap().push(c);
        Ok(db)
    }

    fn client(url: &str) -> Result<postgres::Client> {
        let cfg: postgres::Config = url.parse().map_err(|e| Error::Index(format!("CAIRN_STORE_META: {e}")))?;
        if cfg.get_ssl_mode() == postgres::config::SslMode::Disable {
            return cfg.connect(postgres::NoTls).map_err(pg_err);
        }
        let mut roots = rustls::RootCertStore { roots: webpki_roots::TLS_SERVER_ROOTS.to_vec() };
        if let Ok(ca) = std::env::var("CAIRN_STORE_CA_FILE") {
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
            .name("cairn-meta-pg".into())
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
                        flag.store(true, std::sync::atomic::Ordering::SeqCst);
                        break;
                    }
                }
                flag.store(true, std::sync::atomic::Ordering::SeqCst);
            })
            .map_err(Error::Io)?;
        ready_rx.recv().map_err(|_| Error::Index("metadata database connection thread ended".into()))??;
        Ok(Conn { jobs, closed })
    }

    fn checkout(&self) -> Result<Conn> {
        let pooled = self.pool.lock().unwrap().pop();
        match pooled {
            Some(c) if !c.is_closed() => Ok(c),
            _ => self.connect(),
        }
    }

    fn checkin(&self, c: Conn) {
        let mut p = self.pool.lock().unwrap();
        if p.len() < 16 && !c.is_closed() {
            p.push(c);
        }
    }

    fn begin(&self, write: bool) -> Result<PgTx<'_>> {
        let c = self.checkout()?;
        let sql = if write {
            format!("BEGIN ISOLATION LEVEL READ COMMITTED; SELECT pg_advisory_xact_lock({LOCK_ID})")
        } else {
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY".to_string()
        };
        c.run(move |c| c.batch_execute(&sql))?;
        Ok(PgTx { db: self, conn: RefCell::new(Some(c)), done: Cell::new(false) })
    }
}

pub struct PgTx<'db> {
    db: &'db PgDb,
    conn: RefCell<Option<Conn>>,
    done: Cell<bool>,
}

impl PgTx<'_> {
    fn with<T: Send + 'static>(
        &self,
        f: impl FnOnce(&mut postgres::Client) -> std::result::Result<T, postgres::Error> + Send + 'static,
    ) -> Result<T> {
        let g = self.conn.borrow();
        g.as_ref().ok_or_else(|| Error::Index("transaction finished".into()))?.run(f)
    }

    fn finish(&self, sql: &'static str) -> Result<()> {
        self.done.set(true);
        let r = self.with(move |c| c.batch_execute(sql));
        if let Some(c) = self.conn.borrow_mut().take() {
            if r.is_ok() {
                self.db.checkin(c);
            }
        }
        r
    }
}

impl Drop for PgTx<'_> {
    fn drop(&mut self) {
        if !self.done.get() {
            let _ = self.finish("ROLLBACK");
        }
    }
}

// ------------------------------------------------------------------------------ databases and tables

pub enum Db {
    Redb(redb::Database),
    Pg(PgDb),
}

pub enum ReadTransaction<'db> {
    R(redb::ReadTransaction),
    P(PgTx<'db>),
}

#[allow(clippy::large_enum_variant)] // one per transaction, never stored
pub enum WriteTransaction<'db> {
    R(redb::WriteTransaction),
    P(PgTx<'db>),
}

impl Db {
    pub fn begin_read(&self) -> Result<ReadTransaction<'_>> {
        Ok(match self {
            Db::Redb(d) => ReadTransaction::R(d.begin_read()?),
            Db::Pg(p) => ReadTransaction::P(p.begin(false)?),
        })
    }

    pub fn begin_write(&self, fsync: bool) -> Result<WriteTransaction<'_>> {
        Ok(match self {
            Db::Redb(d) => {
                let mut tx = d.begin_write()?;
                tx.set_durability(if fsync { redb::Durability::Immediate } else { redb::Durability::None });
                WriteTransaction::R(tx)
            }
            Db::Pg(p) => WriteTransaction::P(p.begin(true)?),
        })
    }

    pub fn is_postgres(&self) -> bool {
        matches!(self, Db::Pg(_))
    }
}

impl ReadTransaction<'_> {
    pub fn open_table<K: OrdKey, V: Value + 'static>(&self, def: TableDefinition<K, V>) -> Result<Table<'_, K, V>> {
        Ok(match self {
            ReadTransaction::R(r) => Table::R(r.open_table(def)?),
            ReadTransaction::P(p) => Table::P(p, def.name().to_string(), PhantomData),
        })
    }
}

impl WriteTransaction<'_> {
    pub fn open_table<K: OrdKey, V: Value + 'static>(&self, def: TableDefinition<K, V>) -> Result<Table<'_, K, V>> {
        Ok(match self {
            WriteTransaction::R(w) => Table::W(w.open_table(def)?),
            WriteTransaction::P(p) => Table::P(p, def.name().to_string(), PhantomData),
        })
    }

    pub fn commit(self) -> Result<()> {
        match self {
            WriteTransaction::R(w) => Ok(w.commit()?),
            WriteTransaction::P(p) => p.finish("COMMIT"),
        }
    }
}

pub enum Table<'t, K: Key + 'static, V: Value + 'static> {
    W(redb::Table<'t, K, V>),
    R(redb::ReadOnlyTable<K, V>),
    P(&'t PgTx<'t>, String, PhantomData<(K, V)>),
}

/// The read side of a table (as redb's `ReadableTable`).
pub trait ReadableTable<K: OrdKey, V: Value + 'static> {
    fn get<'a>(&self, key: impl Borrow<K::SelfType<'a>>) -> Result<Option<Guard<'_, V>>>;
    fn range<'a, KR>(&self, range: impl RangeBounds<KR> + 'a) -> Result<RangeIter<'_, K, V>>
    where
        KR: Borrow<K::SelfType<'a>> + 'a;
    fn iter(&self) -> Result<RangeIter<'_, K, V>>;
    fn last(&self) -> Result<Option<(Guard<'_, K>, Guard<'_, V>)>>;
    fn len(&self) -> Result<u64>;
}

type Row = (Vec<u8>, Vec<u8>, Vec<u8>); // (k, kr, v)

fn bound(b: Bound<Vec<u8>>) -> (Option<Vec<u8>>, bool) {
    match b {
        Bound::Included(v) => (Some(v), true),
        Bound::Excluded(v) => (Some(v), false),
        Bound::Unbounded => (None, false),
    }
}

impl<K: OrdKey, V: Value + 'static> ReadableTable<K, V> for Table<'_, K, V> {
    fn get<'a>(&self, key: impl Borrow<K::SelfType<'a>>) -> Result<Option<Guard<'_, V>>> {
        Ok(match self {
            Table::W(t) => t.get(key)?.map(Guard::R),
            Table::R(t) => t.get(key)?.map(Guard::R),
            Table::P(tx, name, _) => {
                let (k, name) = (ord_of::<K>(key.borrow()), name.clone());
                tx.with(move |c| c.query_opt("SELECT v FROM cairn_store_kv WHERE tbl = $1 AND k = $2", &[&name, &k]))?
                    .map(|r| Guard::O(r.get::<_, Vec<u8>>(0), PhantomData))
            }
        })
    }

    fn range<'a, KR>(&self, range: impl RangeBounds<KR> + 'a) -> Result<RangeIter<'_, K, V>>
    where
        KR: Borrow<K::SelfType<'a>> + 'a,
    {
        Ok(match self {
            Table::W(t) => RangeIter::R(t.range(range)?),
            Table::R(t) => RangeIter::R(t.range(range)?),
            Table::P(tx, name, _) => {
                let enc = |b: Bound<&KR>| match b {
                    Bound::Included(k) => Bound::Included(ord_of::<K>(k.borrow())),
                    Bound::Excluded(k) => Bound::Excluded(ord_of::<K>(k.borrow())),
                    Bound::Unbounded => Bound::Unbounded,
                };
                RangeIter::P(PgRange {
                    tx,
                    name: name.clone(),
                    lo: enc(range.start_bound()),
                    hi: enc(range.end_bound()),
                    buf: VecDeque::new(),
                    exhausted: false,
                    _t: PhantomData,
                })
            }
        })
    }

    fn iter(&self) -> Result<RangeIter<'_, K, V>> {
        self.range::<K::SelfType<'_>>(..)
    }

    fn last(&self) -> Result<Option<(Guard<'_, K>, Guard<'_, V>)>> {
        Ok(match self {
            Table::W(t) => t.last()?.map(|(k, v)| (Guard::R(k), Guard::R(v))),
            Table::R(t) => t.last()?.map(|(k, v)| (Guard::R(k), Guard::R(v))),
            Table::P(tx, name, _) => {
                let name = name.clone();
                tx.with(move |c| c.query_opt("SELECT kr, v FROM cairn_store_kv WHERE tbl = $1 ORDER BY k DESC LIMIT 1", &[&name]))?
                    .map(|r| (Guard::O(r.get(0), PhantomData), Guard::O(r.get(1), PhantomData)))
            }
        })
    }

    fn len(&self) -> Result<u64> {
        Ok(match self {
            Table::W(t) => t.len()?,
            Table::R(t) => t.len()?,
            Table::P(tx, name, _) => {
                let name = name.clone();
                tx.with(move |c| c.query_one("SELECT count(*) FROM cairn_store_kv WHERE tbl = $1", &[&name]))?.get::<_, i64>(0) as u64
            }
        })
    }
}

impl<K: OrdKey, V: Value + 'static> Table<'_, K, V> {
    pub fn insert<'k, 'v>(
        &mut self,
        key: impl Borrow<K::SelfType<'k>>,
        value: impl Borrow<V::SelfType<'v>>,
    ) -> Result<Option<Guard<'_, V>>> {
        Ok(match self {
            Table::W(t) => t.insert(key, value)?.map(Guard::R),
            Table::R(_) => return Err(Error::Index("write to a read-only transaction".into())),
            Table::P(tx, name, _) => {
                let k = ord_of::<K>(key.borrow());
                let kr = K::as_bytes(key.borrow()).as_ref().to_vec();
                let v = V::as_bytes(value.borrow()).as_ref().to_vec();
                let name = name.clone();
                let old = tx.with(move |c| {
                    let old = c.query_opt("SELECT v FROM cairn_store_kv WHERE tbl = $1 AND k = $2", &[&name, &k])?;
                    c.execute(
                        "INSERT INTO cairn_store_kv (tbl, k, kr, v) VALUES ($1, $2, $3, $4)
                         ON CONFLICT (tbl, k) DO UPDATE SET kr = EXCLUDED.kr, v = EXCLUDED.v",
                        &[&name, &k, &kr, &v],
                    )?;
                    Ok(old)
                })?;
                old.map(|r| Guard::O(r.get(0), PhantomData))
            }
        })
    }

    pub fn remove<'a>(&mut self, key: impl Borrow<K::SelfType<'a>>) -> Result<Option<Guard<'_, V>>> {
        Ok(match self {
            Table::W(t) => t.remove(key)?.map(Guard::R),
            Table::R(_) => return Err(Error::Index("write to a read-only transaction".into())),
            Table::P(tx, name, _) => {
                let (k, name) = (ord_of::<K>(key.borrow()), name.clone());
                tx.with(move |c| c.query_opt("DELETE FROM cairn_store_kv WHERE tbl = $1 AND k = $2 RETURNING v", &[&name, &k]))?
                    .map(|r| Guard::O(r.get(0), PhantomData))
            }
        })
    }
}

/// An ordered range of a table (paged from PostgreSQL as it is read).
pub enum RangeIter<'t, K: Key + 'static, V: Value + 'static> {
    R(redb::Range<'t, K, V>),
    P(PgRange<'t, K, V>),
}

pub struct PgRange<'t, K, V> {
    tx: &'t PgTx<'t>,
    name: String,
    lo: Bound<Vec<u8>>,
    hi: Bound<Vec<u8>>,
    buf: VecDeque<Row>,
    exhausted: bool,
    _t: PhantomData<(K, V)>,
}

impl<K, V> PgRange<'_, K, V> {
    fn fill(&mut self) -> Result<()> {
        let (lo, lo_inc) = bound(self.lo.clone());
        let (hi, hi_inc) = bound(self.hi.clone());
        let sql = format!(
            "SELECT k, kr, v FROM cairn_store_kv WHERE tbl = $1 {} {} ORDER BY k LIMIT {PAGE}",
            match (&lo, lo_inc) {
                (None, _) => "AND $2::bytea IS NULL",
                (Some(_), true) => "AND k >= $2",
                (Some(_), false) => "AND k > $2",
            },
            match (&hi, hi_inc) {
                (None, _) => "AND $3::bytea IS NULL",
                (Some(_), true) => "AND k <= $3",
                (Some(_), false) => "AND k < $3",
            }
        );
        let name = self.name.clone();
        let rows = self.tx.with(move |c| c.query(&sql, &[&name, &lo, &hi]))?;
        if (rows.len() as i64) < PAGE {
            self.exhausted = true;
        }
        for r in rows {
            self.buf.push_back((r.get(0), r.get(1), r.get(2)));
        }
        if let Some((k, _, _)) = self.buf.back() {
            self.lo = Bound::Excluded(k.clone());
        }
        Ok(())
    }
}

impl<'t, K: Key + 'static, V: Value + 'static> Iterator for RangeIter<'t, K, V> {
    type Item = Result<(Guard<'t, K>, Guard<'t, V>)>;

    fn next(&mut self) -> Option<Self::Item> {
        match self {
            RangeIter::R(r) => r.next().map(|x| x.map(|(k, v)| (Guard::R(k), Guard::R(v))).map_err(Error::from)),
            RangeIter::P(p) => {
                if p.buf.is_empty() && !p.exhausted {
                    if let Err(e) = p.fill() {
                        p.exhausted = true;
                        return Some(Err(e));
                    }
                }
                p.buf.pop_front().map(|(_, kr, v)| Ok((Guard::O(kr, PhantomData), Guard::O(v, PhantomData))))
            }
        }
    }
}

// ------------------------------------------------------------------------------------- store binding

/// Binds a data directory (its blobs) to its metadata database: both carry the same store id. A
/// fresh pair gets a new id; a mismatch (another store's database, or a database restored without its
/// blobs) refuses to open.
pub fn bind(db: &Db, dir: &std::path::Path) -> Result<()> {
    let Db::Pg(pg) = db else { return Ok(()) };
    let file = dir.join("store-id");
    let local = std::fs::read_to_string(&file).ok().map(|s| s.trim().to_string());
    let tx = pg.begin(true)?;
    let remote: Option<Vec<u8>> =
        tx.with(|c| c.query_opt("SELECT v FROM cairn_store_kv WHERE tbl = '_store' AND k = 'id'::bytea", &[]))?.map(|r| r.get(0));
    let remote = remote.map(|v| String::from_utf8_lossy(&v).into_owned());
    let has_data = tx.with(|c| c.query_one("SELECT count(*) FROM cairn_store_kv WHERE tbl <> '_store'", &[]))?.get::<_, i64>(0) > 0;
    match (local, remote) {
        (Some(l), Some(r)) if l == r => {}
        (Some(l), Some(r)) => {
            return Err(Error::Index(format!(
                "the metadata database belongs to store {r}, but this data directory is store {l}; refusing to mix them"
            )))
        }
        (None, Some(r)) => {
            return Err(Error::Index(format!(
                "the metadata database belongs to store {r}, and this data directory has no store-id (its blobs are elsewhere)"
            )))
        }
        (Some(l), None) if has_data => {
            return Err(Error::Index(format!("the metadata database has data but no store id; this data directory is store {l}")))
        }
        (local, None) => {
            let id = local.unwrap_or_else(super::new_id);
            let idb = id.as_bytes().to_vec();
            tx.with(move |c| {
                c.execute("INSERT INTO cairn_store_kv (tbl, k, kr, v) VALUES ('_store', 'id'::bytea, 'id'::bytea, $1)", &[&idb])
            })?;
            std::fs::write(&file, &id)?;
        }
    }
    tx.finish("COMMIT")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ordering_matches_key_order() {
        let mut keys: Vec<(&str, &str)> = vec![("a", "b"), ("a", ""), ("a\0", "x"), ("ab", ""), ("a", "a\0"), ("", "z"), ("b", "")];
        let mut by_bytes = keys.clone();
        keys.sort();
        by_bytes.sort_by_key(ord_of::<(&'static str, &'static str)>);
        assert_eq!(keys, by_bytes);
        let nums = [0u64, 1, 255, 256, u64::MAX];
        let enc: Vec<Vec<u8>> = nums.iter().map(ord_of::<u64>).collect();
        let mut sorted = enc.clone();
        sorted.sort();
        assert_eq!(enc, sorted);
    }
}
