//! Cairn Store's storage engine: object bytes in immutable blob files, everything else in one
//! transactional index (redb).
//!
//! Durability and consistency rules:
//!
//! * A write streams into a temporary file, is fsync'd, renamed into the blob tree and the directory
//!   fsync'd. Only then does a single index transaction make it visible. A reader therefore sees an
//!   object only once its bytes are on disk, and sees it immediately after the write returns
//!   (read-after-write), because the index commit is durable before the response is sent.
//! * Conditional writes (If-Match / If-None-Match) are checked inside that same transaction, so they
//!   are an atomic compare-and-set: two writers cannot both win.
//! * Blobs are never modified. Replacing or deleting an object queues its old blobs for collection in
//!   the same transaction; they are removed after the commit. A crash at any point leaves either the
//!   old or the new object, never a mix; start-up recovery removes temporary files and blobs no object
//!   refers to.
//!
//! Versioning: `objects` always holds a key's current version, so reads and listings never look at old
//! versions. Once a bucket's versioning is enabled, a replaced or deleted current version moves to
//! `versions` (keyed newest first) in the same transaction that installs its successor or delete marker,
//! and its blobs are kept. Blobs are collected only when a version is permanently deleted. The version ID
//! of objects written while versioning was never enabled, or suspended, is "null" (stored as "").

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use md5::{Digest, Md5};
use redb::{Database, Durability, ReadableTable, ReadableTableMetadata, TableDefinition};
use serde::{Deserialize, Serialize};

pub const MIN_PART_SIZE: u64 = 5 << 20;
pub const MAX_PARTS: u32 = 10_000;

const BUCKETS: TableDefinition<&str, &[u8]> = TableDefinition::new("buckets");
const OBJECTS: TableDefinition<(&str, &str), &[u8]> = TableDefinition::new("objects");
const UPLOADS: TableDefinition<&str, &[u8]> = TableDefinition::new("uploads");
const PARTS: TableDefinition<(&str, u32), &[u8]> = TableDefinition::new("parts");
const GC: TableDefinition<&str, u64> = TableDefinition::new("gc");
/// Per-bucket configuration documents: (bucket, kind) -> document (tagging, cors, policy, ...).
const CONFIG: TableDefinition<(&str, &str), &[u8]> = TableDefinition::new("bucket_config");
/// Noncurrent versions and delete markers: (bucket, key, order) -> Object. `order` sorts newest first.
const VERSIONS: TableDefinition<(&str, &str, &str), &[u8]> = TableDefinition::new("versions");
/// Counters (version order).
const META: TableDefinition<&str, u64> = TableDefinition::new("meta");

#[derive(Debug)]
pub enum Error {
    NoSuchBucket,
    BucketExists,
    BucketNotEmpty,
    NoSuchKey,
    NoSuchUpload,
    NoSuchVersion,
    /// Object lock protects the version.
    ObjectLocked,
    /// The version is a delete marker (GET/HEAD by version, tagging).
    MethodNotAllowed,
    PreconditionFailed,
    InvalidPart,
    InvalidPartOrder,
    EntityTooSmall,
    Conflict,
    Io(io::Error),
    Index(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for Error {}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
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

/// One stored piece of an object: a whole small object, or one part of a multipart one.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Segment {
    #[serde(rename = "b")]
    pub blob: String,
    #[serde(rename = "s")]
    pub size: u64,
}

/// The index entry for a key.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Object {
    pub key: String,
    pub size: u64,
    /// MD5 hex, or md5-of-md5s-N for multipart objects (without quotes).
    pub etag: String,
    /// Milliseconds since the epoch.
    pub modified: u64,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub content_type: String,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub headers: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub user_meta: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub checksums: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub checksum_type: String,
    pub segments: Vec<Segment>,
    #[serde(default)]
    pub part_count: u32,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub tags: BTreeMap<String, String>,
    /// "" = the null version.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub version_id: String,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub delete_marker: bool,
    /// When this version stopped being current (ms), for lifecycle rules on noncurrent versions.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub noncurrent_since: u64,
    #[serde(flatten)]
    pub lock: Lock,
}

/// Object lock state of one version: retention (mode and date) and legal hold.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Lock {
    /// "", "GOVERNANCE" or "COMPLIANCE".
    #[serde(default, skip_serializing_if = "String::is_empty", rename = "lock_mode")]
    pub mode: String,
    /// Retain until (ms since the epoch).
    #[serde(default, skip_serializing_if = "is_zero", rename = "retain_until")]
    pub until: u64,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub legal_hold: bool,
}

impl Lock {
    /// Would deleting this version now be refused? `bypass`: the caller may bypass governance retention.
    pub fn protects(&self, now: u64, bypass: bool) -> bool {
        self.legal_hold || (self.until > now && (self.mode == "COMPLIANCE" || (self.mode == "GOVERNANCE" && !bypass)))
    }
}

fn is_zero(n: &u64) -> bool {
    *n == 0
}

impl Object {
    /// The version ID as S3 shows it.
    pub fn version(&self) -> &str {
        if self.version_id.is_empty() {
            "null"
        } else {
            &self.version_id
        }
    }
}

/// What a delete did.
#[derive(Debug, Default, Clone)]
pub struct Deleted {
    /// The version removed, or the delete marker created ("null" for the null version); None in buckets
    /// that were never versioned.
    pub version_id: Option<String>,
    /// A delete marker was created, or the version removed was one.
    pub delete_marker: bool,
}

/// One entry of a version listing.
#[derive(Debug, Clone)]
pub struct VersionEntry {
    pub obj: Object,
    pub latest: bool,
}

#[derive(Debug, Default)]
pub struct VersionList {
    pub entries: Vec<VersionEntry>,
    pub common_prefixes: Vec<String>,
    pub truncated: bool,
    pub next_key: String,
    pub next_version: String,
}

/// An in-progress multipart upload.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Upload {
    pub id: String,
    pub bucket: String,
    pub key: String,
    pub initiated: u64,
    #[serde(default)]
    pub content_type: String,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    #[serde(default)]
    pub user_meta: BTreeMap<String, String>,
    #[serde(default)]
    pub checksum_algo: String,
    #[serde(default)]
    pub checksum_type: String,
    #[serde(default)]
    pub tags: BTreeMap<String, String>,
    #[serde(flatten)]
    pub lock: Lock,
}

/// An uploaded part.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Part {
    pub number: u32,
    pub blob: String,
    pub size: u64,
    pub etag: String,
    pub md5: Vec<u8>,
    #[serde(default)]
    pub checksums: BTreeMap<String, String>,
    pub modified: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BucketInfo {
    pub name: String,
    pub created: u64,
    /// "" (never enabled), "Enabled" or "Suspended".
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub versioning: String,
}

/// A write precondition, evaluated atomically with the commit.
#[derive(Clone, Debug, Default)]
pub struct Condition {
    /// ETag the current object must have ("*" = it must exist).
    pub if_match: Option<String>,
    /// "*" = the key must not exist; otherwise the current ETag must differ.
    pub if_none_match: Option<String>,
}

pub fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

pub fn new_id() -> String {
    let b: [u8; 16] = rand::random();
    hex::encode(b)
}

/// Does an If-Match / If-None-Match header list match an ETag?
pub fn etag_matches(header: &str, etag: &str) -> bool {
    header.split(',').map(|t| t.trim().trim_start_matches("W/")).any(|t| t == "*" || t.trim_matches('"') == etag)
}

fn check_condition(cur: Option<&Object>, c: &Condition) -> Result<()> {
    if let Some(v) = &c.if_none_match {
        if v == "*" {
            if cur.is_some() {
                return Err(Error::PreconditionFailed);
            }
        } else if let Some(o) = cur {
            if etag_matches(v, &o.etag) {
                return Err(Error::PreconditionFailed);
            }
        }
    }
    if let Some(v) = &c.if_match {
        match cur {
            None => return Err(Error::NoSuchKey),
            Some(o) if v != "*" && !etag_matches(v, &o.etag) => return Err(Error::PreconditionFailed),
            _ => {}
        }
    }
    Ok(())
}

/// The smallest string greater than every string starting with `p` (None: no such string).
fn prefix_successor(p: &str) -> Option<String> {
    let mut chars: Vec<char> = p.chars().collect();
    while let Some(c) = chars.pop() {
        if let Some(n) = char::from_u32(c as u32 + 1).or_else(|| char::from_u32(c as u32 + 0x801)) {
            chars.push(n);
            return Some(chars.into_iter().collect());
        }
    }
    None
}

pub struct Engine {
    dir: PathBuf,
    db: Database,
    fsync: bool,
    gc_lock: Mutex<()>,
}

/// Writes the bytes of one segment. `seal` makes them durable; dropping an unsealed writer discards them.
pub struct BlobWriter<'a> {
    engine: &'a Engine,
    id: String,
    tmp: PathBuf,
    file: Option<File>,
    size: u64,
    md5: Md5,
}

impl BlobWriter<'_> {
    pub fn write(&mut self, data: &[u8]) -> io::Result<()> {
        self.file.as_mut().expect("blob already sealed").write_all(data)?;
        self.size += data.len() as u64;
        self.md5.update(data);
        Ok(())
    }

    pub fn size(&self) -> u64 {
        self.size
    }

    pub fn md5(&self) -> Vec<u8> {
        self.md5.clone().finalize().to_vec()
    }

    /// fsyncs the blob and moves it into the blob tree. It is not visible to readers until an index
    /// transaction refers to it.
    pub fn seal(mut self) -> Result<Segment> {
        let file = self.file.take().expect("blob already sealed");
        if self.engine.fsync {
            file.sync_all()?;
        }
        drop(file);
        let dst = self.engine.blob_path(&self.id);
        fs::create_dir_all(dst.parent().unwrap())?;
        fs::rename(&self.tmp, &dst)?;
        if self.engine.fsync {
            File::open(dst.parent().unwrap())?.sync_all()?;
        }
        Ok(Segment { blob: self.id.clone(), size: self.size })
    }
}

impl Drop for BlobWriter<'_> {
    fn drop(&mut self) {
        if self.file.take().is_some() {
            let _ = fs::remove_file(&self.tmp);
        }
    }
}

/// A reader over a byte range of an object, following its segments. The segment files are opened up
/// front, so a concurrent replace or delete cannot cut the read short.
pub struct ObjectReader {
    parts: Vec<(File, u64)>, // file positioned at its start offset, bytes left
}

impl Read for ObjectReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        while let Some((f, left)) = self.parts.first_mut() {
            if *left == 0 {
                self.parts.remove(0);
                continue;
            }
            let want = buf.len().min(*left as usize);
            let n = f.read(&mut buf[..want])?;
            if n == 0 {
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "blob shorter than its index entry"));
            }
            *left -= n as u64;
            return Ok(n);
        }
        Ok(0)
    }
}

#[derive(Debug, Default, Serialize)]
pub struct RecoveryReport {
    pub temp_files_removed: usize,
    pub blobs_collected: usize,
    pub unreferenced_blobs_removed: usize,
    pub objects_with_missing_blobs: usize,
    pub blobs_referenced: usize,
}

#[derive(Debug, Default)]
pub struct ListOptions {
    pub prefix: String,
    pub delimiter: String,
    /// List keys strictly after this one.
    pub after: String,
    pub max_keys: usize,
}

#[derive(Debug, Default)]
pub struct ListResult {
    pub objects: Vec<Object>,
    pub common_prefixes: Vec<String>,
    pub truncated: bool,
    /// The last key or common prefix returned.
    pub next_after: String,
}

#[derive(Debug, Default, Serialize)]
pub struct Stats {
    pub buckets: u64,
    pub objects: u64,
    pub bytes: u64,
    pub uploads: u64,
    pub noncurrent_versions: u64,
}

pub struct CompletePart {
    pub number: u32,
    pub etag: String,
}

impl Engine {
    /// Opens (or creates) a store in `dir` and runs crash recovery.
    pub fn open(dir: impl AsRef<Path>, fsync: bool) -> Result<Engine> {
        let dir = dir.as_ref().to_path_buf();
        for d in [dir.clone(), dir.join("blobs"), dir.join("tmp")] {
            fs::create_dir_all(&d)?;
        }
        let db = Database::create(dir.join("index.redb"))?;
        let e = Engine { dir, db, fsync, gc_lock: Mutex::new(()) };
        let tx = e.begin_write()?;
        tx.open_table(BUCKETS)?;
        tx.open_table(OBJECTS)?;
        tx.open_table(UPLOADS)?;
        tx.open_table(PARTS)?;
        tx.open_table(GC)?;
        tx.open_table(CONFIG)?;
        tx.open_table(VERSIONS)?;
        tx.open_table(META)?;
        tx.commit()?;
        e.recover()?;
        Ok(e)
    }

    fn begin_write(&self) -> Result<redb::WriteTransaction> {
        let mut tx = self.db.begin_write()?;
        tx.set_durability(if self.fsync { Durability::Immediate } else { Durability::None });
        Ok(tx)
    }

    fn blob_path(&self, id: &str) -> PathBuf {
        self.dir.join("blobs").join(&id[0..2]).join(&id[2..4]).join(id)
    }

    /// Starts a blob.
    pub fn new_blob(&self) -> Result<BlobWriter<'_>> {
        let id = new_id();
        let tmp = self.dir.join("tmp").join(format!("{id}.{}", new_id()));
        let file = File::create(&tmp)?;
        Ok(BlobWriter { engine: self, id, tmp, file: Some(file), size: 0, md5: Md5::new() })
    }

    /// Removes sealed blobs that were never committed.
    pub fn discard(&self, segs: &[Segment]) {
        for s in segs {
            let _ = fs::remove_file(self.blob_path(&s.blob));
        }
    }

    fn queue_gc(tx: &redb::WriteTransaction, segs: &[Segment]) -> Result<()> {
        let mut gc = tx.open_table(GC)?;
        let now = now_ms();
        for s in segs {
            gc.insert(s.blob.as_str(), now)?;
        }
        Ok(())
    }

    /// Deletes blobs queued for collection.
    fn collect(&self) {
        let _guard = self.gc_lock.lock().unwrap();
        let ids: Vec<String> = (|| -> Result<Vec<String>> {
            let r = self.db.begin_read()?;
            let t = r.open_table(GC)?;
            let mut ids = vec![];
            for row in t.iter()? {
                ids.push(row?.0.value().to_string());
            }
            Ok(ids)
        })()
        .unwrap_or_default();
        if ids.is_empty() {
            return;
        }
        for id in &ids {
            let _ = fs::remove_file(self.blob_path(id));
        }
        if let Ok(tx) = self.begin_write() {
            if let Ok(mut t) = tx.open_table(GC) {
                for id in &ids {
                    let _ = t.remove(id.as_str());
                }
            }
            let _ = tx.commit();
        }
    }

    /// Removes temporary files, finishes queued collection and removes blobs no object or upload
    /// refers to (left by a crash between sealing a blob and committing it). Counts objects whose blobs
    /// are missing, which means the disk lost data.
    pub fn recover(&self) -> Result<RecoveryReport> {
        let mut rep = RecoveryReport::default();
        for ent in fs::read_dir(self.dir.join("tmp"))? {
            if fs::remove_file(ent?.path()).is_ok() {
                rep.temp_files_removed += 1;
            }
        }
        rep.blobs_collected = self.db.begin_read()?.open_table(GC)?.len()? as usize;
        self.collect();
        let mut refs = HashSet::new();
        {
            let r = self.db.begin_read()?;
            for row in r.open_table(OBJECTS)?.iter()? {
                let (_, v) = row?;
                let o: Object = serde_json::from_slice(v.value())?;
                refs.extend(o.segments.into_iter().map(|s| s.blob));
            }
            for row in r.open_table(PARTS)?.iter()? {
                let (_, v) = row?;
                let p: Part = serde_json::from_slice(v.value())?;
                refs.insert(p.blob);
            }
            for row in r.open_table(VERSIONS)?.iter()? {
                let (_, v) = row?;
                let o: Object = serde_json::from_slice(v.value())?;
                refs.extend(o.segments.into_iter().map(|s| s.blob));
            }
        }
        rep.blobs_referenced = refs.len();
        let mut seen = HashSet::new();
        let mut stack = vec![self.dir.join("blobs")];
        while let Some(d) = stack.pop() {
            for ent in fs::read_dir(&d)? {
                let ent = ent?;
                let p = ent.path();
                if ent.file_type()?.is_dir() {
                    stack.push(p);
                    continue;
                }
                let id = ent.file_name().to_string_lossy().to_string();
                if refs.contains(&id) {
                    seen.insert(id);
                } else if fs::remove_file(&p).is_ok() {
                    rep.unreferenced_blobs_removed += 1;
                }
            }
        }
        rep.objects_with_missing_blobs = refs.difference(&seen).count();
        Ok(rep)
    }

    // ------------------------------------------------------------------------------------- buckets

    pub fn create_bucket(&self, name: &str) -> Result<()> {
        self.create_bucket_with(name, "", &[])
    }

    /// Creates a bucket with its versioning state and configuration documents, in one transaction.
    pub fn create_bucket_with(&self, name: &str, versioning: &str, configs: &[(&str, &[u8])]) -> Result<()> {
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(BUCKETS)?;
            if t.get(name)?.is_some() {
                return Err(Error::BucketExists);
            }
            let v = serde_json::to_vec(&BucketInfo { name: name.into(), created: now_ms(), versioning: versioning.into() })?;
            t.insert(name, v.as_slice())?;
            let mut cfg = tx.open_table(CONFIG)?;
            for (kind, doc) in configs {
                cfg.insert((name, *kind), *doc)?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn delete_bucket(&self, name: &str) -> Result<()> {
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(BUCKETS)?;
            if t.get(name)?.is_none() {
                return Err(Error::NoSuchBucket);
            }
            let objs = tx.open_table(OBJECTS)?;
            if objs.range((name, "")..)?.next().transpose()?.is_some_and(|(k, _)| k.value().0 == name) {
                return Err(Error::BucketNotEmpty);
            }
            let vers = tx.open_table(VERSIONS)?;
            if vers.range((name, "", "")..)?.next().transpose()?.is_some_and(|(k, _)| k.value().0 == name) {
                return Err(Error::BucketNotEmpty);
            }
            for row in tx.open_table(UPLOADS)?.iter()? {
                let (_, v) = row?;
                let u: Upload = serde_json::from_slice(v.value())?;
                if u.bucket == name {
                    return Err(Error::BucketNotEmpty);
                }
            }
            t.remove(name)?;
            let mut cfg = tx.open_table(CONFIG)?;
            let kinds: Vec<String> = cfg
                .range((name, "")..)?
                .map_while(|row| row.ok().filter(|(k, _)| k.value().0 == name).map(|(k, _)| k.value().1.to_string()))
                .collect();
            for k in kinds {
                cfg.remove((name, k.as_str()))?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    /// A bucket's configuration document of one kind (None: not set).
    pub fn get_config(&self, bucket: &str, kind: &str) -> Result<Option<Vec<u8>>> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        let t = r.open_table(CONFIG)?;
        let v = t.get((bucket, kind))?.map(|v| v.value().to_vec());
        Ok(v)
    }

    /// Sets (Some) or removes (None) a bucket's configuration document of one kind.
    pub fn put_config(&self, bucket: &str, kind: &str, doc: Option<&[u8]>) -> Result<()> {
        let tx = self.begin_write()?;
        {
            Self::bucket_exists(&tx.open_table(BUCKETS)?, bucket)?;
            let mut t = tx.open_table(CONFIG)?;
            match doc {
                Some(d) => {
                    t.insert((bucket, kind), d)?;
                }
                None => {
                    t.remove((bucket, kind))?;
                }
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn head_bucket(&self, name: &str) -> Result<BucketInfo> {
        let r = self.db.begin_read()?;
        let t = r.open_table(BUCKETS)?;
        let v = t.get(name)?.ok_or(Error::NoSuchBucket)?;
        Ok(serde_json::from_slice(v.value())?)
    }

    pub fn list_buckets(&self) -> Result<Vec<BucketInfo>> {
        let r = self.db.begin_read()?;
        let mut out = vec![];
        for row in r.open_table(BUCKETS)?.iter()? {
            let (_, v) = row?;
            out.push(serde_json::from_slice(v.value())?);
        }
        Ok(out)
    }

    // ------------------------------------------------------------------------------------- objects

    fn bucket_exists<T: ReadableTable<&'static str, &'static [u8]>>(t: &T, name: &str) -> Result<()> {
        if t.get(name)?.is_none() {
            return Err(Error::NoSuchBucket);
        }
        Ok(())
    }

    /// Commits `obj` (whose segments are sealed) under its key if `cond` holds, as a new version in a
    /// versioned bucket. Returns the stored object (with its version ID). On any error the new segments
    /// are discarded.
    pub fn put_object(&self, bucket: &str, obj: Object, cond: &Condition) -> Result<Object> {
        let segs = obj.segments.clone();
        let res = (|| -> Result<(Object, Vec<Segment>)> {
            let tx = self.begin_write()?;
            let (stored, garbage) = {
                let info = Self::bucket_in(&tx.open_table(BUCKETS)?, bucket)?;
                let cur = Self::current_in(&tx.open_table(OBJECTS)?, bucket, &obj.key)?;
                check_condition(cur.as_ref(), cond)?;
                Self::install(&tx, &info.versioning, bucket, obj, cur)?
            };
            Self::queue_gc(&tx, &garbage)?;
            tx.commit()?;
            Ok((stored, garbage))
        })();
        match res {
            Ok((stored, garbage)) => {
                if !garbage.is_empty() {
                    self.collect();
                }
                Ok(stored)
            }
            Err(e) => {
                self.discard(&segs);
                Err(e)
            }
        }
    }

    fn bucket_in<T: ReadableTable<&'static str, &'static [u8]>>(t: &T, name: &str) -> Result<BucketInfo> {
        let v = t.get(name)?.ok_or(Error::NoSuchBucket)?;
        Ok(serde_json::from_slice(v.value())?)
    }

    fn current_in<T: ReadableTable<(&'static str, &'static str), &'static [u8]>>(t: &T, bucket: &str, key: &str) -> Result<Option<Object>> {
        Ok(match t.get((bucket, key))? {
            Some(v) => Some(serde_json::from_slice(v.value())?),
            None => None,
        })
    }

    /// The next position in the version order (newer versions sort first).
    fn next_order(tx: &redb::WriteTransaction) -> Result<String> {
        let mut m = tx.open_table(META)?;
        let n = m.get("version_order")?.map(|v| v.value()).unwrap_or(0) + 1;
        m.insert("version_order", n)?;
        Ok(format!("{:016x}", u64::MAX - n))
    }

    /// A key's noncurrent versions and delete markers, newest first: (order, object).
    fn versions_in<T: ReadableTable<(&'static str, &'static str, &'static str), &'static [u8]>>(
        t: &T,
        bucket: &str,
        key: &str,
    ) -> Result<Vec<(String, Object)>> {
        let mut out = vec![];
        for row in t.range((bucket, key, "")..=(bucket, key, "~"))? {
            let (k, v) = row?;
            out.push((k.value().2.to_string(), serde_json::from_slice(v.value())?));
        }
        Ok(out)
    }

    /// Moves a (formerly current) version into the version store.
    fn archive(tx: &redb::WriteTransaction, bucket: &str, mut o: Object) -> Result<()> {
        let order = Self::next_order(tx)?;
        o.noncurrent_since = now_ms();
        let v = serde_json::to_vec(&o)?;
        tx.open_table(VERSIONS)?.insert((bucket, o.key.as_str(), order.as_str()), v.as_slice())?;
        Ok(())
    }

    /// Removes a key's stored null version (it is being replaced); returns its blobs.
    fn drop_null_version(tx: &redb::WriteTransaction, bucket: &str, key: &str) -> Result<Vec<Segment>> {
        let mut t = tx.open_table(VERSIONS)?;
        let mut garbage = vec![];
        for (order, o) in Self::versions_in(&t, bucket, key)? {
            if o.version_id.is_empty() {
                t.remove((bucket, key, order.as_str()))?;
                garbage.extend(o.segments);
            }
        }
        Ok(garbage)
    }

    /// Makes `obj` the current version of its key, keeping or dropping the version it replaces as the
    /// bucket's versioning state says. Returns the stored object and the blobs to collect.
    fn install(
        tx: &redb::WriteTransaction,
        versioning: &str,
        bucket: &str,
        mut obj: Object,
        cur: Option<Object>,
    ) -> Result<(Object, Vec<Segment>)> {
        let mut garbage = vec![];
        obj.delete_marker = false;
        obj.noncurrent_since = 0;
        match versioning {
            "Enabled" => {
                obj.version_id = new_id();
                if let Some(c) = cur {
                    Self::archive(tx, bucket, c)?;
                }
            }
            "Suspended" => {
                obj.version_id = String::new();
                if let Some(c) = cur {
                    if c.version_id.is_empty() {
                        garbage.extend(c.segments);
                    } else {
                        Self::archive(tx, bucket, c)?;
                    }
                }
                garbage.extend(Self::drop_null_version(tx, bucket, &obj.key)?);
            }
            _ => {
                obj.version_id = String::new();
                if let Some(c) = cur {
                    garbage.extend(c.segments);
                }
            }
        }
        let v = serde_json::to_vec(&obj)?;
        tx.open_table(OBJECTS)?.insert((bucket, obj.key.as_str()), v.as_slice())?;
        Ok((obj, garbage))
    }

    /// Sets a bucket's versioning state ("Enabled" or "Suspended").
    pub fn set_versioning(&self, bucket: &str, state: &str) -> Result<()> {
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(BUCKETS)?;
            let mut info = Self::bucket_in(&t, bucket)?;
            info.versioning = state.to_string();
            let v = serde_json::to_vec(&info)?;
            t.insert(bucket, v.as_slice())?;
        }
        tx.commit()?;
        Ok(())
    }

    /// A specific version (None or "": the current one; "null": the null version). Delete markers are
    /// returned as objects with `delete_marker` set.
    pub fn get_version(&self, bucket: &str, key: &str, version: Option<&str>) -> Result<Object> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        let cur = Self::current_in(&r.open_table(OBJECTS)?, bucket, key)?;
        let Some(want) = version.filter(|v| !v.is_empty()) else { return cur.ok_or(Error::NoSuchKey) };
        let want = if want == "null" { "" } else { want };
        if let Some(c) = cur.filter(|c| c.version_id == want) {
            return Ok(c);
        }
        Self::versions_in(&r.open_table(VERSIONS)?, bucket, key)?
            .into_iter()
            .map(|(_, o)| o)
            .find(|o| o.version_id == want)
            .ok_or(Error::NoSuchVersion)
    }

    /// If the key's latest version is a delete marker, its version ID.
    pub fn latest_delete_marker(&self, bucket: &str, key: &str) -> Result<Option<String>> {
        let r = self.db.begin_read()?;
        if Self::current_in(&r.open_table(OBJECTS)?, bucket, key)?.is_some() {
            return Ok(None);
        }
        Ok(Self::versions_in(&r.open_table(VERSIONS)?, bucket, key)?
            .into_iter()
            .next()
            .filter(|(_, o)| o.delete_marker)
            .map(|(_, o)| o.version().to_string()))
    }

    pub fn get_object(&self, bucket: &str, key: &str) -> Result<Object> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        let t = r.open_table(OBJECTS)?;
        let v = t.get((bucket, key))?.ok_or(Error::NoSuchKey)?;
        Ok(serde_json::from_slice(v.value())?)
    }

    /// Changes an object's metadata in place (no new bytes), atomically. `f` may refuse the change.
    pub fn update_object(
        &self,
        bucket: &str,
        key: &str,
        version: Option<&str>,
        f: impl FnOnce(&mut Object) -> Result<()>,
    ) -> Result<Object> {
        let tx = self.begin_write()?;
        let obj = {
            Self::bucket_exists(&tx.open_table(BUCKETS)?, bucket)?;
            let mut t = tx.open_table(OBJECTS)?;
            let cur = Self::current_in(&t, bucket, key)?;
            let want = version.filter(|v| !v.is_empty()).map(|v| if v == "null" { "" } else { v });
            match (cur, want) {
                (Some(mut o), w) if w.is_none_or(|w| w == o.version_id) => {
                    f(&mut o)?;
                    let v = serde_json::to_vec(&o)?;
                    t.insert((bucket, key), v.as_slice())?;
                    o
                }
                (None, None) => return Err(Error::NoSuchKey),
                (_, w) => {
                    let w = w.unwrap_or_default();
                    let mut vt = tx.open_table(VERSIONS)?;
                    let (order, mut o) =
                        Self::versions_in(&vt, bucket, key)?.into_iter().find(|(_, o)| o.version_id == w).ok_or(Error::NoSuchVersion)?;
                    if o.delete_marker {
                        return Err(Error::MethodNotAllowed);
                    }
                    f(&mut o)?;
                    let v = serde_json::to_vec(&o)?;
                    vt.insert((bucket, key, order.as_str()), v.as_slice())?;
                    o
                }
            }
        };
        tx.commit()?;
        Ok(obj)
    }

    /// Deletes a key: removes it (unversioned bucket) or puts a delete marker on top (versioned).
    /// Deleting a missing key succeeds (S3 semantics).
    pub fn delete_object(&self, bucket: &str, key: &str, if_match: Option<&str>) -> Result<Deleted> {
        let tx = self.begin_write()?;
        let (out, garbage) = {
            let info = Self::bucket_in(&tx.open_table(BUCKETS)?, bucket)?;
            let mut t = tx.open_table(OBJECTS)?;
            let cur = Self::current_in(&t, bucket, key)?;
            if let Some(m) = if_match {
                match &cur {
                    Some(o) if m == "*" || etag_matches(m, &o.etag) => {}
                    _ => return Err(Error::PreconditionFailed),
                }
            }
            let mut garbage = vec![];
            let out = match info.versioning.as_str() {
                "" => {
                    if let Some(o) = cur {
                        t.remove((bucket, key))?;
                        garbage = o.segments;
                    }
                    Deleted::default()
                }
                mode => {
                    if let Some(o) = cur {
                        t.remove((bucket, key))?;
                        if mode == "Suspended" && o.version_id.is_empty() {
                            garbage = o.segments;
                        } else {
                            Self::archive(&tx, bucket, o)?;
                        }
                    }
                    let vid = if mode == "Enabled" { new_id() } else { String::new() };
                    if mode == "Suspended" {
                        garbage.extend(Self::drop_null_version(&tx, bucket, key)?);
                    }
                    let marker = Object { key: key.into(), modified: now_ms(), version_id: vid, delete_marker: true, ..Default::default() };
                    let order = Self::next_order(&tx)?;
                    let v = serde_json::to_vec(&marker)?;
                    tx.open_table(VERSIONS)?.insert((bucket, key, order.as_str()), v.as_slice())?;
                    Deleted { version_id: Some(marker.version().to_string()), delete_marker: true }
                }
            };
            (out, garbage)
        };
        Self::queue_gc(&tx, &garbage)?;
        tx.commit()?;
        if !garbage.is_empty() {
            self.collect();
        }
        Ok(out)
    }

    /// Permanently deletes one version (or delete marker). If it was the current version, the newest
    /// remaining version becomes current again, unless that is a delete marker. A missing version succeeds.
    /// Object lock is enforced here: a version under legal hold or unexpired retention is not removed
    /// (governance retention gives way only with `bypass_governance`).
    pub fn delete_version(&self, bucket: &str, key: &str, version: &str, bypass_governance: bool) -> Result<Deleted> {
        let now = now_ms();
        let want = if version == "null" { "" } else { version };
        let tx = self.begin_write()?;
        let (out, garbage) = {
            Self::bucket_exists(&tx.open_table(BUCKETS)?, bucket)?;
            let mut t = tx.open_table(OBJECTS)?;
            let mut vt = tx.open_table(VERSIONS)?;
            let mut out = Deleted { version_id: Some(version.to_string()), delete_marker: false };
            let mut garbage = vec![];
            match Self::current_in(&t, bucket, key)? {
                Some(c) if c.version_id == want => {
                    if c.lock.protects(now, bypass_governance) {
                        return Err(Error::ObjectLocked);
                    }
                    t.remove((bucket, key))?;
                    garbage = c.segments;
                }
                cur => {
                    if let Some((order, o)) = Self::versions_in(&vt, bucket, key)?.into_iter().find(|(_, o)| o.version_id == want) {
                        if o.lock.protects(now, bypass_governance) {
                            return Err(Error::ObjectLocked);
                        }
                        vt.remove((bucket, key, order.as_str()))?;
                        out.delete_marker = o.delete_marker;
                        garbage = o.segments;
                    }
                    if cur.is_some() {
                        drop((t, vt));
                        return Self::finish_delete(self, tx, out, garbage);
                    }
                }
            }
            // The key has no current version now: the newest remaining one becomes current, unless it is
            // a delete marker.
            if let Some((order, mut o)) = Self::versions_in(&vt, bucket, key)?.into_iter().next() {
                if !o.delete_marker {
                    vt.remove((bucket, key, order.as_str()))?;
                    o.noncurrent_since = 0;
                    let v = serde_json::to_vec(&o)?;
                    t.insert((bucket, key), v.as_slice())?;
                }
            }
            (out, garbage)
        };
        Self::finish_delete(self, tx, out, garbage)
    }

    fn finish_delete(&self, tx: redb::WriteTransaction, out: Deleted, garbage: Vec<Segment>) -> Result<Deleted> {
        Self::queue_gc(&tx, &garbage)?;
        tx.commit()?;
        if !garbage.is_empty() {
            self.collect();
        }
        Ok(out)
    }

    /// Lists versions and delete markers in key order, newest first within a key.
    pub fn list_versions(
        &self,
        bucket: &str,
        prefix: &str,
        delimiter: &str,
        key_marker: &str,
        version_marker: &str,
        max: usize,
    ) -> Result<VersionList> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        let (objs, vers) = (r.open_table(OBJECTS)?, r.open_table(VERSIONS)?);
        let mut res = VersionList::default();
        if max == 0 {
            return Ok(res);
        }
        // The next key at or after `from` that has a current or stored version, within the prefix.
        let next_key = |from: &str| -> Result<Option<String>> {
            let a = objs.range((bucket, from)..)?.next().transpose()?.and_then(|(k, _)| {
                let (b, key) = k.value();
                (b == bucket).then(|| key.to_string())
            });
            let b = vers.range((bucket, from, "")..)?.next().transpose()?.and_then(|(k, _)| {
                let (b, key, _) = k.value();
                (b == bucket).then(|| key.to_string())
            });
            let k = match (a, b) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (a, b) => a.or(b),
            };
            Ok(k.filter(|k| k.starts_with(prefix)))
        };
        let mut from = if key_marker.is_empty() || key_marker < prefix { prefix.to_string() } else { key_marker.to_string() };
        let mut count = 0;
        while let Some(key) = next_key(&from)? {
            from = format!("{key}\u{0}");
            if !delimiter.is_empty() {
                if let Some(i) = key[prefix.len()..].find(delimiter) {
                    let cp = format!("{prefix}{}", &key[prefix.len()..prefix.len() + i + delimiter.len()]);
                    if key_marker.is_empty() || cp.as_str() > key_marker {
                        if count >= max {
                            res.truncated = true;
                            break;
                        }
                        res.next_key = cp.clone();
                        res.next_version = String::new();
                        res.common_prefixes.push(cp.clone());
                        count += 1;
                    }
                    match prefix_successor(&cp) {
                        Some(n) => {
                            from = n;
                            continue;
                        }
                        None => break,
                    }
                }
            }
            let mut entries: Vec<VersionEntry> = vec![];
            if let Some(c) = Self::current_in(&objs, bucket, &key)? {
                entries.push(VersionEntry { obj: c, latest: true });
            }
            for (_, o) in Self::versions_in(&vers, bucket, &key)? {
                let latest = entries.is_empty();
                entries.push(VersionEntry { obj: o, latest });
            }
            let mut skip = key == key_marker;
            if skip && !version_marker.is_empty() {
                // Resume after the marker version.
                if let Some(i) = entries.iter().position(|e| e.obj.version() == version_marker) {
                    entries.drain(..=i);
                    skip = false;
                }
            }
            if skip {
                continue;
            }
            for e in entries {
                if count >= max {
                    res.truncated = true;
                    return Ok(res);
                }
                res.next_key = key.clone();
                res.next_version = e.obj.version().to_string();
                res.entries.push(e);
                count += 1;
            }
        }
        Ok(res)
    }

    /// Opens `[off, off+n)` of an object.
    pub fn reader(&self, obj: &Object, off: u64, n: u64) -> Result<ObjectReader> {
        let mut parts = vec![];
        let end = off + n;
        let mut pos = 0u64;
        for s in &obj.segments {
            let (start, stop) = (pos, pos + s.size);
            pos = stop;
            if stop <= off || start >= end || s.size == 0 {
                continue;
            }
            let mut f = File::open(self.blob_path(&s.blob))?;
            let from = off.max(start) - start;
            let to = end.min(stop) - start;
            f.seek(SeekFrom::Start(from))?;
            parts.push((f, to - from));
        }
        Ok(ObjectReader { parts })
    }

    /// Looks an object up and opens the range `rng` picks, retrying if a concurrent writer replaced it
    /// between the lookup and the open (its old blobs may already be gone).
    pub fn get_fresh<E>(
        &self,
        bucket: &str,
        key: &str,
        version: Option<&str>,
        mut rng: impl FnMut(&Object) -> std::result::Result<(u64, u64), E>,
    ) -> std::result::Result<(Object, ObjectReader), FreshError<E>> {
        for attempt in 0.. {
            let obj = self.get_version(bucket, key, version).map_err(FreshError::Engine)?;
            if obj.delete_marker {
                return Err(FreshError::Engine(Error::MethodNotAllowed));
            }
            let (off, n) = rng(&obj).map_err(|e| FreshError::Range(Box::new(obj.clone()), e))?;
            match self.reader(&obj, off, n) {
                Ok(r) => return Ok((obj, r)),
                Err(Error::Io(e)) if e.kind() == io::ErrorKind::NotFound && attempt < 4 => continue,
                Err(e) => return Err(FreshError::Engine(e)),
            }
        }
        unreachable!()
    }

    /// Lists keys in order, grouping by delimiter.
    pub fn list(&self, bucket: &str, opt: &ListOptions) -> Result<ListResult> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        let mut res = ListResult::default();
        if opt.max_keys == 0 {
            return Ok(res);
        }
        let t = r.open_table(OBJECTS)?;
        let mut start = if opt.after > opt.prefix { opt.after.clone() } else { opt.prefix.clone() };
        let mut count = 0;
        'outer: loop {
            let iter = t.range((bucket, start.as_str())..)?;
            for row in iter {
                let (k, v) = row?;
                let (b, key) = k.value();
                if b != bucket || !key.starts_with(opt.prefix.as_str()) {
                    break 'outer;
                }
                if !opt.after.is_empty() && key <= opt.after.as_str() {
                    continue;
                }
                if !opt.delimiter.is_empty() {
                    let rest = &key[opt.prefix.len()..];
                    if let Some(i) = rest.find(opt.delimiter.as_str()) {
                        let cp = format!("{}{}", opt.prefix, &rest[..i + opt.delimiter.len()]);
                        if !opt.after.is_empty() && cp.as_str() <= opt.after.as_str() {
                            // resuming after this common prefix: skip all of it
                        } else {
                            if count >= opt.max_keys {
                                res.truncated = true;
                                break 'outer;
                            }
                            res.common_prefixes.push(cp.clone());
                            res.next_after = cp.clone();
                            count += 1;
                        }
                        match prefix_successor(&cp) {
                            Some(next) => {
                                start = next;
                                continue 'outer;
                            }
                            None => break 'outer,
                        }
                    }
                }
                if count >= opt.max_keys {
                    res.truncated = true;
                    break 'outer;
                }
                res.objects.push(serde_json::from_slice(v.value())?);
                res.next_after = key.to_string();
                count += 1;
            }
            break;
        }
        Ok(res)
    }

    // ------------------------------------------------------------------------------------- multipart

    pub fn create_upload(&self, mut u: Upload) -> Result<Upload> {
        u.id = new_id();
        u.initiated = now_ms();
        let tx = self.begin_write()?;
        {
            Self::bucket_exists(&tx.open_table(BUCKETS)?, &u.bucket)?;
            let v = serde_json::to_vec(&u)?;
            tx.open_table(UPLOADS)?.insert(u.id.as_str(), v.as_slice())?;
        }
        tx.commit()?;
        Ok(u)
    }

    fn upload_in<T: ReadableTable<&'static str, &'static [u8]>>(t: &T, bucket: &str, key: &str, id: &str) -> Result<Upload> {
        let v = t.get(id)?.ok_or(Error::NoSuchUpload)?;
        let u: Upload = serde_json::from_slice(v.value())?;
        if u.bucket != bucket || u.key != key {
            return Err(Error::NoSuchUpload);
        }
        Ok(u)
    }

    pub fn get_upload(&self, bucket: &str, key: &str, id: &str) -> Result<Upload> {
        let r = self.db.begin_read()?;
        Self::upload_in(&r.open_table(UPLOADS)?, bucket, key, id)
    }

    /// Records a sealed part, replacing an earlier upload of the same part number.
    pub fn put_part(&self, bucket: &str, key: &str, id: &str, p: Part) -> Result<()> {
        let new_seg = Segment { blob: p.blob.clone(), size: p.size };
        let res = (|| -> Result<Vec<Segment>> {
            let tx = self.begin_write()?;
            let old = {
                Self::upload_in(&tx.open_table(UPLOADS)?, bucket, key, id)?;
                let mut t = tx.open_table(PARTS)?;
                let old = match t.get((id, p.number))? {
                    Some(v) => {
                        let op: Part = serde_json::from_slice(v.value())?;
                        vec![Segment { blob: op.blob, size: op.size }]
                    }
                    None => vec![],
                };
                let v = serde_json::to_vec(&p)?;
                t.insert((id, p.number), v.as_slice())?;
                old
            };
            Self::queue_gc(&tx, &old)?;
            tx.commit()?;
            Ok(old)
        })();
        match res {
            Ok(old) => {
                if !old.is_empty() {
                    self.collect();
                }
                Ok(())
            }
            Err(e) => {
                self.discard(&[new_seg]);
                Err(e)
            }
        }
    }

    fn parts_of<T: ReadableTable<(&'static str, u32), &'static [u8]>>(t: &T, id: &str) -> Result<Vec<Part>> {
        let mut out = vec![];
        for row in t.range((id, 0u32)..=(id, u32::MAX))? {
            let (_, v) = row?;
            out.push(serde_json::from_slice(v.value())?);
        }
        Ok(out)
    }

    /// Lists an upload's parts after a part number.
    pub fn list_parts(&self, bucket: &str, key: &str, id: &str, after: u32, max: usize) -> Result<(Vec<Part>, bool)> {
        let r = self.db.begin_read()?;
        Self::upload_in(&r.open_table(UPLOADS)?, bucket, key, id)?;
        let mut parts: Vec<Part> = Self::parts_of(&r.open_table(PARTS)?, id)?.into_iter().filter(|p| p.number > after).collect();
        let truncated = parts.len() > max;
        parts.truncate(max);
        Ok((parts, truncated))
    }

    /// Lists a bucket's in-progress uploads, by key then initiation time.
    pub fn list_uploads(&self, bucket: &str, prefix: &str) -> Result<Vec<Upload>> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        let mut out: Vec<Upload> = vec![];
        for row in r.open_table(UPLOADS)?.iter()? {
            let (_, v) = row?;
            let u: Upload = serde_json::from_slice(v.value())?;
            if u.bucket == bucket && u.key.starts_with(prefix) {
                out.push(u);
            }
        }
        out.sort_by(|a, b| (a.key.as_str(), a.initiated).cmp(&(b.key.as_str(), b.initiated)));
        Ok(out)
    }

    fn drop_upload(tx: &redb::WriteTransaction, id: &str) -> Result<Vec<Part>> {
        let mut t = tx.open_table(PARTS)?;
        let parts = Self::parts_of(&t, id)?;
        for p in &parts {
            t.remove((id, p.number))?;
        }
        tx.open_table(UPLOADS)?.remove(id)?;
        Ok(parts)
    }

    /// Discards an upload and its parts.
    pub fn abort_upload(&self, bucket: &str, key: &str, id: &str) -> Result<()> {
        let tx = self.begin_write()?;
        Self::upload_in(&tx.open_table(UPLOADS)?, bucket, key, id)?;
        let parts = Self::drop_upload(&tx, id)?;
        let segs: Vec<Segment> = parts.into_iter().map(|p| Segment { blob: p.blob, size: p.size }).collect();
        Self::queue_gc(&tx, &segs)?;
        tx.commit()?;
        self.collect();
        Ok(())
    }

    /// Assembles the listed parts into the object. No bytes are copied: the object refers to the part
    /// blobs. `finish` computes the object's checksums from the chosen parts; it may fail the completion.
    pub fn complete_upload(
        &self,
        bucket: &str,
        key: &str,
        id: &str,
        want: &[CompletePart],
        cond: &Condition,
        finish: impl FnOnce(&Upload, &[Part]) -> Result<(BTreeMap<String, String>, String)>,
    ) -> Result<Object> {
        let tx = self.begin_write()?;
        let (obj, garbage) = {
            let u = Self::upload_in(&tx.open_table(UPLOADS)?, bucket, key, id)?;
            let have: HashMap<u32, Part> = Self::parts_of(&tx.open_table(PARTS)?, id)?.into_iter().map(|p| (p.number, p)).collect();
            if want.is_empty() {
                return Err(Error::InvalidPart);
            }
            let mut last = 0;
            for w in want {
                if w.number <= last {
                    return Err(Error::InvalidPartOrder);
                }
                last = w.number;
            }
            let mut parts = vec![];
            for w in want {
                match have.get(&w.number) {
                    Some(p) if w.etag.is_empty() || w.etag.trim_matches('"') == p.etag => parts.push(p.clone()),
                    _ => return Err(Error::InvalidPart),
                }
            }
            for (i, p) in parts.iter().enumerate() {
                if i + 1 < parts.len() && p.size < MIN_PART_SIZE {
                    return Err(Error::EntityTooSmall);
                }
            }
            let info = Self::bucket_in(&tx.open_table(BUCKETS)?, bucket)?;
            let cur = Self::current_in(&tx.open_table(OBJECTS)?, bucket, key)?;
            check_condition(cur.as_ref(), cond)?;
            let (checksums, ctype) = finish(&u, &parts)?;
            let mut md5 = Md5::new();
            let chosen: HashSet<u32> = parts.iter().map(|p| p.number).collect();
            for p in &parts {
                md5.update(&p.md5);
            }
            let obj = Object {
                key: key.into(),
                size: parts.iter().map(|p| p.size).sum(),
                etag: format!("{}-{}", hex::encode(md5.finalize()), parts.len()),
                modified: now_ms(),
                content_type: u.content_type.clone(),
                headers: u.headers.clone(),
                user_meta: u.user_meta.clone(),
                checksums,
                checksum_type: ctype,
                segments: parts.iter().map(|p| Segment { blob: p.blob.clone(), size: p.size }).collect(),
                part_count: parts.len() as u32,
                tags: u.tags.clone(),
                lock: u.lock.clone(),
                ..Default::default()
            };
            let (obj, mut garbage) = Self::install(&tx, &info.versioning, bucket, obj, cur)?;
            garbage.extend(have.values().filter(|p| !chosen.contains(&p.number)).map(|p| Segment { blob: p.blob.clone(), size: p.size }));
            Self::drop_upload(&tx, id)?;
            (obj, garbage)
        };
        Self::queue_gc(&tx, &garbage)?;
        tx.commit()?;
        if !garbage.is_empty() {
            self.collect();
        }
        Ok(obj)
    }

    /// Counts buckets, objects, stored bytes and open uploads.
    pub fn stats(&self) -> Result<Stats> {
        let r = self.db.begin_read()?;
        let mut st = Stats { buckets: r.open_table(BUCKETS)?.len()?, uploads: r.open_table(UPLOADS)?.len()?, ..Default::default() };
        for row in r.open_table(OBJECTS)?.iter()? {
            let (_, v) = row?;
            let o: Object = serde_json::from_slice(v.value())?;
            st.objects += 1;
            st.bytes += o.size;
        }
        for row in r.open_table(VERSIONS)?.iter()? {
            let (_, v) = row?;
            let o: Object = serde_json::from_slice(v.value())?;
            st.noncurrent_versions += 1;
            st.bytes += o.size;
        }
        Ok(st)
    }
}

/// Why `get_fresh` failed: the engine, or the range picker (with the object it saw).
pub enum FreshError<E> {
    Engine(Error),
    Range(Box<Object>, E),
}

#[cfg(test)]
mod tests;
