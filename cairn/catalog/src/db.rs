//! Catalog state: namespaces, tables (their current metadata location and metadata) and staged
//! creations, in one transactional index: embedded (redb, one catalog) or PostgreSQL (`crate::pg`,
//! several catalog replicas).
//!
//! Every commit is a compare-and-set on each table's metadata location inside a single transaction,
//! so concurrent writers cannot both win and a multi-table commit applies to all tables or none.

use std::collections::BTreeMap;
use std::path::Path;

use redb::{Database, Durability, ReadableTable, TableDefinition};
use serde::{Deserialize, Serialize};

const NAMESPACES: TableDefinition<&str, &[u8]> = TableDefinition::new("namespaces");
const TABLES: TableDefinition<(&str, &str), &[u8]> = TableDefinition::new("tables");
const STAGED: TableDefinition<(&str, &str), &[u8]> = TableDefinition::new("staged");
const VIEWS: TableDefinition<(&str, &str), &[u8]> = TableDefinition::new("views");

/// Namespace levels are stored joined by the unit separator, which the REST spec also uses in paths.
pub const SEP: char = '\u{1f}';

pub fn ns_key(levels: &[String]) -> String {
    levels.join(&SEP.to_string())
}

pub fn ns_levels(key: &str) -> Vec<String> {
    key.split(SEP).map(str::to_string).collect()
}

#[derive(Debug)]
pub enum Error {
    NoSuchNamespace,
    NamespaceExists,
    NamespaceNotEmpty,
    NoSuchTable,
    TableExists,
    NoSuchView,
    ViewExists,
    /// A table's metadata changed since the commit was prepared (index of the change).
    Conflict(usize),
    Index(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

macro_rules! from_redb {
    ($($t:ty),*) => {$(impl From<$t> for Error { fn from(e: $t) -> Self { Error::Index(e.to_string()) } })*};
}
from_redb!(
    redb::Error,
    redb::DatabaseError,
    redb::TransactionError,
    redb::TableError,
    redb::StorageError,
    redb::CommitError,
    serde_json::Error
);

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct NamespaceRow {
    properties: BTreeMap<String, String>,
}

/// A table's current state.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct TableRow {
    pub metadata_location: String,
    /// The table metadata as JSON (exactly what was written to `metadata_location`).
    pub metadata: String,
}

/// One table change in a commit: replace `expected` (None = the table must not exist) with `new`.
pub struct Change {
    pub namespace: String,
    pub name: String,
    pub expected: Option<String>,
    pub new: TableRow,
}

pub struct Local {
    db: Database,
    fsync: bool,
}

impl Local {
    pub fn open(dir: impl AsRef<Path>, fsync: bool) -> Result<Local> {
        std::fs::create_dir_all(dir.as_ref()).map_err(|e| Error::Index(e.to_string()))?;
        let db = Database::create(dir.as_ref().join("catalog.redb"))?;
        let me = Local { db, fsync };
        let tx = me.begin_write()?;
        tx.open_table(NAMESPACES)?;
        tx.open_table(TABLES)?;
        tx.open_table(STAGED)?;
        tx.open_table(VIEWS)?;
        tx.commit()?;
        Ok(me)
    }

    fn begin_write(&self) -> Result<redb::WriteTransaction> {
        let mut tx = self.db.begin_write()?;
        // Without fsync (tests) a commit is still written: it survives the process dying, not the machine.
        // (Durability::None would keep it in memory until a later durable commit: a killed catalog lost it.)
        tx.set_durability(if self.fsync { Durability::Immediate } else { Durability::Eventual });
        Ok(tx)
    }

    // ------------------------------------------------------------------------------------ namespaces

    pub fn create_namespace(&self, levels: &[String], properties: BTreeMap<String, String>) -> Result<()> {
        let key = ns_key(levels);
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(NAMESPACES)?;
            if t.get(key.as_str())?.is_some() {
                return Err(Error::NamespaceExists);
            }
            let v = serde_json::to_vec(&NamespaceRow { properties })?;
            t.insert(key.as_str(), v.as_slice())?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn namespace(&self, levels: &[String]) -> Result<BTreeMap<String, String>> {
        let r = self.db.begin_read()?;
        let t = r.open_table(NAMESPACES)?;
        let v = t.get(ns_key(levels).as_str())?.ok_or(Error::NoSuchNamespace)?;
        Ok(serde_json::from_slice::<NamespaceRow>(v.value())?.properties)
    }

    /// Direct children of `parent` (top-level namespaces when `parent` is empty).
    pub fn list_namespaces(&self, parent: &[String]) -> Result<Vec<Vec<String>>> {
        let r = self.db.begin_read()?;
        let t = r.open_table(NAMESPACES)?;
        if !parent.is_empty() && t.get(ns_key(parent).as_str())?.is_none() {
            return Err(Error::NoSuchNamespace);
        }
        let mut out = vec![];
        for row in t.iter()? {
            let (k, _) = row?;
            let levels = ns_levels(k.value());
            if levels.len() == parent.len() + 1 && levels.starts_with(parent) {
                out.push(levels);
            }
        }
        Ok(out)
    }

    /// Applies property removals and updates; returns (updated, removed, missing).
    pub fn update_namespace(
        &self,
        levels: &[String],
        removals: &[String],
        updates: &BTreeMap<String, String>,
    ) -> Result<(Vec<String>, Vec<String>, Vec<String>)> {
        let key = ns_key(levels);
        let tx = self.begin_write()?;
        let out = {
            let mut t = tx.open_table(NAMESPACES)?;
            let mut row: NamespaceRow = match t.get(key.as_str())? {
                Some(v) => serde_json::from_slice(v.value())?,
                None => return Err(Error::NoSuchNamespace),
            };
            let (mut removed, mut missing) = (vec![], vec![]);
            for k in removals {
                if row.properties.remove(k).is_some() {
                    removed.push(k.clone());
                } else {
                    missing.push(k.clone());
                }
            }
            for (k, v) in updates {
                row.properties.insert(k.clone(), v.clone());
            }
            let v = serde_json::to_vec(&row)?;
            t.insert(key.as_str(), v.as_slice())?;
            (updates.keys().cloned().collect(), removed, missing)
        };
        tx.commit()?;
        Ok(out)
    }

    pub fn drop_namespace(&self, levels: &[String]) -> Result<()> {
        let key = ns_key(levels);
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(NAMESPACES)?;
            if t.get(key.as_str())?.is_none() {
                return Err(Error::NoSuchNamespace);
            }
            let child_prefix = format!("{key}{SEP}");
            for row in t.iter()? {
                if row?.0.value().starts_with(&child_prefix) {
                    return Err(Error::NamespaceNotEmpty);
                }
            }
            for def in [TABLES, VIEWS] {
                let rows = tx.open_table(def)?;
                if rows.range((key.as_str(), "")..)?.next().transpose()?.is_some_and(|(k, _)| k.value().0 == key) {
                    return Err(Error::NamespaceNotEmpty);
                }
            }
            t.remove(key.as_str())?;
        }
        tx.commit()?;
        Ok(())
    }

    // ------------------------------------------------------------------------------------ tables

    pub fn list_tables(&self, levels: &[String]) -> Result<Vec<String>> {
        let key = ns_key(levels);
        let r = self.db.begin_read()?;
        if r.open_table(NAMESPACES)?.get(key.as_str())?.is_none() {
            return Err(Error::NoSuchNamespace);
        }
        let mut out = vec![];
        for row in r.open_table(TABLES)?.range((key.as_str(), "")..)? {
            let (k, _) = row?;
            let (ns, name) = k.value();
            if ns != key {
                break;
            }
            out.push(name.to_string());
        }
        Ok(out)
    }

    pub fn table(&self, levels: &[String], name: &str) -> Result<TableRow> {
        let r = self.db.begin_read()?;
        let key = ns_key(levels);
        if r.open_table(NAMESPACES)?.get(key.as_str())?.is_none() {
            return Err(Error::NoSuchNamespace);
        }
        let t = r.open_table(TABLES)?;
        let v = t.get((key.as_str(), name))?.ok_or(Error::NoSuchTable)?;
        Ok(serde_json::from_slice(v.value())?)
    }

    /// Applies changes atomically: each table's current metadata location must still be the one the
    /// change was prepared from.
    pub fn commit(&self, changes: &[Change]) -> Result<()> {
        let tx = self.begin_write()?;
        {
            let ns = tx.open_table(NAMESPACES)?;
            let views = tx.open_table(VIEWS)?;
            let mut t = tx.open_table(TABLES)?;
            for (i, c) in changes.iter().enumerate() {
                if ns.get(c.namespace.as_str())?.is_none() {
                    return Err(Error::NoSuchNamespace);
                }
                if c.expected.is_none() && views.get((c.namespace.as_str(), c.name.as_str()))?.is_some() {
                    return Err(Error::ViewExists);
                }
                let cur: Option<TableRow> = match t.get((c.namespace.as_str(), c.name.as_str()))? {
                    Some(v) => Some(serde_json::from_slice(v.value())?),
                    None => None,
                };
                match (&c.expected, &cur) {
                    (None, Some(_)) => return Err(Error::TableExists),
                    (Some(_), None) => return Err(Error::NoSuchTable),
                    (Some(want), Some(have)) if *want != have.metadata_location => return Err(Error::Conflict(i)),
                    _ => {}
                }
                let v = serde_json::to_vec(&c.new)?;
                t.insert((c.namespace.as_str(), c.name.as_str()), v.as_slice())?;
            }
            let mut staged = tx.open_table(STAGED)?;
            for c in changes {
                staged.remove((c.namespace.as_str(), c.name.as_str()))?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    /// Removes a table and returns its last state.
    pub fn drop_table(&self, levels: &[String], name: &str) -> Result<TableRow> {
        let key = ns_key(levels);
        let tx = self.begin_write()?;
        let row = {
            let mut t = tx.open_table(TABLES)?;
            let row = match t.remove((key.as_str(), name))? {
                Some(v) => serde_json::from_slice(v.value())?,
                None => return Err(Error::NoSuchTable),
            };
            row
        };
        tx.commit()?;
        Ok(row)
    }

    pub fn rename_table(&self, from: (&[String], &str), to: (&[String], &str)) -> Result<()> {
        let (fk, tk) = (ns_key(from.0), ns_key(to.0));
        let tx = self.begin_write()?;
        {
            if tx.open_table(NAMESPACES)?.get(tk.as_str())?.is_none() {
                return Err(Error::NoSuchNamespace);
            }
            if tx.open_table(VIEWS)?.get((tk.as_str(), to.1))?.is_some() {
                return Err(Error::ViewExists);
            }
            let mut t = tx.open_table(TABLES)?;
            if t.get((tk.as_str(), to.1))?.is_some() {
                return Err(Error::TableExists);
            }
            let v = match t.remove((fk.as_str(), from.1))? {
                Some(v) => v.value().to_vec(),
                None => return Err(Error::NoSuchTable),
            };
            t.insert((tk.as_str(), to.1), v.as_slice())?;
        }
        tx.commit()?;
        Ok(())
    }

    // ------------------------------------------------------------------------------------ views

    pub fn list_views(&self, levels: &[String]) -> Result<Vec<String>> {
        let key = ns_key(levels);
        let r = self.db.begin_read()?;
        if r.open_table(NAMESPACES)?.get(key.as_str())?.is_none() {
            return Err(Error::NoSuchNamespace);
        }
        let mut out = vec![];
        for row in r.open_table(VIEWS)?.range((key.as_str(), "")..)? {
            let (k, _) = row?;
            let (ns, name) = k.value();
            if ns != key {
                break;
            }
            out.push(name.to_string());
        }
        Ok(out)
    }

    pub fn view(&self, levels: &[String], name: &str) -> Result<TableRow> {
        let r = self.db.begin_read()?;
        let key = ns_key(levels);
        if r.open_table(NAMESPACES)?.get(key.as_str())?.is_none() {
            return Err(Error::NoSuchNamespace);
        }
        let v = r.open_table(VIEWS)?.get((key.as_str(), name))?.ok_or(Error::NoSuchView)?.value().to_vec();
        Ok(serde_json::from_slice(&v)?)
    }

    /// Creates (`expected` None) or replaces a view: compare-and-set on its metadata location. A view
    /// cannot take a table's name.
    pub fn commit_view(&self, levels: &[String], name: &str, expected: Option<&str>, new: &TableRow) -> Result<()> {
        let key = ns_key(levels);
        let tx = self.begin_write()?;
        {
            if tx.open_table(NAMESPACES)?.get(key.as_str())?.is_none() {
                return Err(Error::NoSuchNamespace);
            }
            if expected.is_none() && tx.open_table(TABLES)?.get((key.as_str(), name))?.is_some() {
                return Err(Error::TableExists);
            }
            let mut t = tx.open_table(VIEWS)?;
            let cur: Option<TableRow> = match t.get((key.as_str(), name))? {
                Some(v) => Some(serde_json::from_slice(v.value())?),
                None => None,
            };
            match (expected, &cur) {
                (None, Some(_)) => return Err(Error::ViewExists),
                (Some(_), None) => return Err(Error::NoSuchView),
                (Some(want), Some(have)) if want != have.metadata_location => return Err(Error::Conflict(0)),
                _ => {}
            }
            t.insert((key.as_str(), name), serde_json::to_vec(new)?.as_slice())?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn drop_view(&self, levels: &[String], name: &str) -> Result<TableRow> {
        let key = ns_key(levels);
        let tx = self.begin_write()?;
        let row = {
            let mut t = tx.open_table(VIEWS)?;
            let row = match t.remove((key.as_str(), name))? {
                Some(v) => serde_json::from_slice(v.value())?,
                None => return Err(Error::NoSuchView),
            };
            row
        };
        tx.commit()?;
        Ok(row)
    }

    pub fn rename_view(&self, from: (&[String], &str), to: (&[String], &str)) -> Result<()> {
        let (fk, tk) = (ns_key(from.0), ns_key(to.0));
        let tx = self.begin_write()?;
        {
            if tx.open_table(NAMESPACES)?.get(tk.as_str())?.is_none() {
                return Err(Error::NoSuchNamespace);
            }
            if tx.open_table(TABLES)?.get((tk.as_str(), to.1))?.is_some() {
                return Err(Error::TableExists);
            }
            let mut t = tx.open_table(VIEWS)?;
            if t.get((tk.as_str(), to.1))?.is_some() {
                return Err(Error::ViewExists);
            }
            let v = match t.remove((fk.as_str(), from.1))? {
                Some(v) => v.value().to_vec(),
                None => return Err(Error::NoSuchView),
            };
            t.insert((tk.as_str(), to.1), v.as_slice())?;
        }
        tx.commit()?;
        Ok(())
    }

    // ------------------------------------------------------------------------------------ staged creates

    /// Remembers the metadata of a staged create (`stage-create`), until a commit creates the table.
    pub fn stage(&self, levels: &[String], name: &str, metadata: &str) -> Result<()> {
        let key = ns_key(levels);
        let tx = self.begin_write()?;
        {
            if tx.open_table(NAMESPACES)?.get(key.as_str())?.is_none() {
                return Err(Error::NoSuchNamespace);
            }
            tx.open_table(STAGED)?.insert((key.as_str(), name), metadata.as_bytes())?;
        }
        tx.commit()?;
        Ok(())
    }

    pub fn staged(&self, levels: &[String], name: &str) -> Result<Option<String>> {
        let r = self.db.begin_read()?;
        let t = r.open_table(STAGED)?;
        let key = ns_key(levels);
        let v = t.get((key.as_str(), name))?;
        Ok(v.map(|v| String::from_utf8_lossy(v.value()).into_owned()))
    }
}

/// The catalog's state, embedded or in PostgreSQL; the same semantics on both.
pub enum Db {
    Local(Local),
    Pg(crate::pg::Pg),
}

macro_rules! both {
    ($self:ident, $local:ident => $l:expr, $pg:ident => $p:expr) => {
        match $self {
            Db::Local($local) => $l,
            Db::Pg($pg) => $p,
        }
    };
}

impl Db {
    pub fn open(dir: impl AsRef<Path>, fsync: bool) -> Result<Db> {
        Ok(Db::Local(Local::open(dir, fsync)?))
    }

    pub fn postgres(url: &str, warehouse: &str) -> Result<Db> {
        Ok(Db::Pg(crate::pg::Pg::open(url, warehouse)?))
    }

    pub fn create_namespace(&self, levels: &[String], properties: BTreeMap<String, String>) -> Result<()> {
        both!(self, l => l.create_namespace(levels, properties), p => p.create_namespace(levels, properties))
    }
    pub fn namespace(&self, levels: &[String]) -> Result<BTreeMap<String, String>> {
        both!(self, l => l.namespace(levels), p => p.namespace(levels))
    }
    pub fn list_namespaces(&self, parent: &[String]) -> Result<Vec<Vec<String>>> {
        both!(self, l => l.list_namespaces(parent), p => p.list_namespaces(parent))
    }
    pub fn update_namespace(
        &self,
        levels: &[String],
        removals: &[String],
        updates: &BTreeMap<String, String>,
    ) -> Result<(Vec<String>, Vec<String>, Vec<String>)> {
        both!(self, l => l.update_namespace(levels, removals, updates), p => p.update_namespace(levels, removals, updates))
    }
    pub fn drop_namespace(&self, levels: &[String]) -> Result<()> {
        both!(self, l => l.drop_namespace(levels), p => p.drop_namespace(levels))
    }
    pub fn list_tables(&self, levels: &[String]) -> Result<Vec<String>> {
        both!(self, l => l.list_tables(levels), p => p.list("t", levels))
    }
    pub fn table(&self, levels: &[String], name: &str) -> Result<TableRow> {
        both!(self, l => l.table(levels, name), p => p.get("t", levels, name))
    }
    pub fn commit(&self, changes: &[Change]) -> Result<()> {
        both!(self, l => l.commit(changes), p => p.commit(changes))
    }
    pub fn drop_table(&self, levels: &[String], name: &str) -> Result<TableRow> {
        both!(self, l => l.drop_table(levels, name), p => p.drop("t", levels, name))
    }
    pub fn rename_table(&self, from: (&[String], &str), to: (&[String], &str)) -> Result<()> {
        both!(self, l => l.rename_table(from, to), p => p.rename("t", from, to))
    }
    pub fn list_views(&self, levels: &[String]) -> Result<Vec<String>> {
        both!(self, l => l.list_views(levels), p => p.list("v", levels))
    }
    pub fn view(&self, levels: &[String], name: &str) -> Result<TableRow> {
        both!(self, l => l.view(levels, name), p => p.get("v", levels, name))
    }
    pub fn commit_view(&self, levels: &[String], name: &str, expected: Option<&str>, new: &TableRow) -> Result<()> {
        both!(self, l => l.commit_view(levels, name, expected, new), p => p.commit_view(levels, name, expected, new))
    }
    pub fn drop_view(&self, levels: &[String], name: &str) -> Result<TableRow> {
        both!(self, l => l.drop_view(levels, name), p => p.drop("v", levels, name))
    }
    pub fn rename_view(&self, from: (&[String], &str), to: (&[String], &str)) -> Result<()> {
        both!(self, l => l.rename_view(from, to), p => p.rename("v", from, to))
    }
    pub fn stage(&self, levels: &[String], name: &str, metadata: &str) -> Result<()> {
        both!(self, l => l.stage(levels, name, metadata), p => p.stage(levels, name, metadata))
    }
    pub fn staged(&self, levels: &[String], name: &str) -> Result<Option<String>> {
        both!(self, l => l.staged(levels, name), p => p.staged(levels, name))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lv(s: &str) -> Vec<String> {
        s.split('.').map(str::to_string).collect()
    }

    fn row(loc: &str) -> TableRow {
        TableRow { metadata_location: loc.into(), metadata: "{}".into() }
    }

    /// Each test runs on the embedded database, and on PostgreSQL too when CAIRN_CATALOG_TEST_PG names
    /// one (a scratch database: the tests empty it).
    static PG: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn pg_lock() -> std::sync::MutexGuard<'static, ()> {
        PG.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn backends() -> Vec<(Db, Option<tempfile::TempDir>)> {
        let dir = tempfile::tempdir().unwrap();
        let mut out = vec![(Db::open(dir.path(), false).unwrap(), Some(dir))];
        if let Ok(url) = std::env::var("CAIRN_CATALOG_TEST_PG") {
            let mut c = postgres::Client::connect(&url, postgres::NoTls).unwrap();
            c.batch_execute("DROP TABLE IF EXISTS cairn_catalog_namespaces, cairn_catalog_entries, cairn_catalog_info").unwrap();
            out.push((Db::postgres(&url, "s3://lakehouse/warehouse").unwrap(), None));
        }
        out
    }

    #[test]
    fn namespaces_nest_and_must_be_empty_to_drop() {
        let _g = pg_lock();
        for (db, _dir) in backends() {
            namespaces_on(&db);
        }
    }

    fn namespaces_on(db: &Db) {
        db.create_namespace(&lv("sales"), BTreeMap::from([("owner".into(), "ana".into())])).unwrap();
        assert!(matches!(db.create_namespace(&lv("sales"), BTreeMap::new()), Err(Error::NamespaceExists)));
        db.create_namespace(&lv("sales.eu"), BTreeMap::new()).unwrap();
        db.create_namespace(&lv("hr"), BTreeMap::new()).unwrap();
        assert_eq!(db.list_namespaces(&[]).unwrap(), vec![lv("hr"), lv("sales")]);
        assert_eq!(db.list_namespaces(&lv("sales")).unwrap(), vec![lv("sales.eu")]);
        assert!(matches!(db.drop_namespace(&lv("sales")), Err(Error::NamespaceNotEmpty)));
        let (updated, removed, missing) =
            db.update_namespace(&lv("sales"), &["owner".into(), "nope".into()], &BTreeMap::from([("tier".into(), "1".into())])).unwrap();
        assert_eq!((updated, removed, missing), (vec!["tier".to_string()], vec!["owner".to_string()], vec!["nope".to_string()]));
        db.drop_namespace(&lv("sales.eu")).unwrap();
        db.drop_namespace(&lv("sales")).unwrap();
        assert!(matches!(db.namespace(&lv("sales")), Err(Error::NoSuchNamespace)));
    }

    #[test]
    fn commits_are_compare_and_set_and_all_or_nothing() {
        let _g = pg_lock();
        for (db, _dir) in backends() {
            commits_on(&db);
        }
    }

    fn commits_on(db: &Db) {
        db.create_namespace(&lv("s"), BTreeMap::new()).unwrap();
        let ch = |name: &str, expected: Option<&str>, new: &str| Change {
            namespace: "s".into(),
            name: name.into(),
            expected: expected.map(str::to_string),
            new: row(new),
        };
        db.commit(&[ch("a", None, "a0"), ch("b", None, "b0")]).unwrap();
        assert!(matches!(db.commit(&[ch("a", None, "x")]), Err(Error::TableExists)));
        db.commit(&[ch("a", Some("a0"), "a1")]).unwrap();
        // A stale writer loses...
        assert!(matches!(db.commit(&[ch("a", Some("a0"), "a1-other")]), Err(Error::Conflict(0))));
        // ...and a multi-table commit with one stale table changes nothing.
        assert!(matches!(db.commit(&[ch("b", Some("b0"), "b1"), ch("a", Some("a0"), "a2")]), Err(Error::Conflict(1))));
        assert_eq!(db.table(&lv("s"), "b").unwrap().metadata_location, "b0");
        assert_eq!(db.list_tables(&lv("s")).unwrap(), ["a", "b"]);
        db.rename_table((&lv("s"), "a"), (&lv("s"), "c")).unwrap();
        assert!(matches!(db.rename_table((&lv("s"), "b"), (&lv("s"), "c")), Err(Error::TableExists)));
        assert_eq!(db.drop_table(&lv("s"), "c").unwrap().metadata_location, "a1");
        assert!(matches!(db.table(&lv("s"), "c"), Err(Error::NoSuchTable)));
    }

    #[test]
    fn views_and_tables_share_names_and_staged_creates_clear() {
        let _g = pg_lock();
        for (db, _dir) in backends() {
            db.create_namespace(&lv("s"), BTreeMap::new()).unwrap();
            db.commit_view(&lv("s"), "v", None, &row("v0")).unwrap();
            let ch = |name: &str| Change { namespace: "s".into(), name: name.into(), expected: None, new: row("t0") };
            assert!(matches!(db.commit(&[ch("v")]), Err(Error::ViewExists)));
            assert!(matches!(db.commit_view(&lv("s"), "v", Some("old"), &row("v1")), Err(Error::Conflict(0))));
            db.commit_view(&lv("s"), "v", Some("v0"), &row("v1")).unwrap();
            assert!(matches!(db.drop_namespace(&lv("s")), Err(Error::NamespaceNotEmpty)));
            db.stage(&lv("s"), "t", "{\"staged\":1}").unwrap();
            assert_eq!(db.staged(&lv("s"), "t").unwrap().as_deref(), Some("{\"staged\":1}"));
            db.commit(&[ch("t")]).unwrap();
            assert_eq!(db.staged(&lv("s"), "t").unwrap(), None);
            assert!(matches!(db.rename_view((&lv("s"), "v"), (&lv("s"), "t")), Err(Error::TableExists)));
            db.rename_view((&lv("s"), "v"), (&lv("s"), "w")).unwrap();
            assert_eq!(db.list_views(&lv("s")).unwrap(), ["w"]);
            assert_eq!(db.drop_view(&lv("s"), "w").unwrap().metadata_location, "v1");
            assert!(matches!(db.view(&lv("s"), "w"), Err(Error::NoSuchView)));
        }
    }

    #[test]
    fn a_postgres_database_belongs_to_one_warehouse() {
        let _g = pg_lock();
        let Ok(url) = std::env::var("CAIRN_CATALOG_TEST_PG") else { return };
        let mut c = postgres::Client::connect(&url, postgres::NoTls).unwrap();
        c.batch_execute("DROP TABLE IF EXISTS cairn_catalog_namespaces, cairn_catalog_entries, cairn_catalog_info").unwrap();
        Db::postgres(&url, "s3://a/w").unwrap();
        Db::postgres(&url, "s3://a/w/").unwrap();
        assert!(Db::postgres(&url, "s3://b/w").is_err());
    }
}
