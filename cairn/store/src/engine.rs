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

#[derive(Debug)]
pub enum Error {
    NoSuchBucket,
    BucketExists,
    BucketNotEmpty,
    NoSuchKey,
    NoSuchUpload,
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
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(BUCKETS)?;
            if t.get(name)?.is_some() {
                return Err(Error::BucketExists);
            }
            let v = serde_json::to_vec(&BucketInfo { name: name.into(), created: now_ms() })?;
            t.insert(name, v.as_slice())?;
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
            for row in tx.open_table(UPLOADS)?.iter()? {
                let (_, v) = row?;
                let u: Upload = serde_json::from_slice(v.value())?;
                if u.bucket == name {
                    return Err(Error::BucketNotEmpty);
                }
            }
            t.remove(name)?;
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

    /// Commits `obj` (whose segments are sealed) under its key if `cond` holds. On any error the new
    /// segments are discarded.
    pub fn put_object(&self, bucket: &str, obj: Object, cond: &Condition) -> Result<()> {
        let res = (|| -> Result<Vec<Segment>> {
            let tx = self.begin_write()?;
            let old = {
                Self::bucket_exists(&tx.open_table(BUCKETS)?, bucket)?;
                let mut t = tx.open_table(OBJECTS)?;
                let cur: Option<Object> = match t.get((bucket, obj.key.as_str()))? {
                    Some(v) => Some(serde_json::from_slice(v.value())?),
                    None => None,
                };
                check_condition(cur.as_ref(), cond)?;
                let v = serde_json::to_vec(&obj)?;
                t.insert((bucket, obj.key.as_str()), v.as_slice())?;
                cur.map(|c| c.segments).unwrap_or_default()
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
                self.discard(&obj.segments);
                Err(e)
            }
        }
    }

    pub fn get_object(&self, bucket: &str, key: &str) -> Result<Object> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        let t = r.open_table(OBJECTS)?;
        let v = t.get((bucket, key))?.ok_or(Error::NoSuchKey)?;
        Ok(serde_json::from_slice(v.value())?)
    }

    /// Removes a key; deleting a missing key succeeds (S3 semantics).
    pub fn delete_object(&self, bucket: &str, key: &str, if_match: Option<&str>) -> Result<()> {
        let tx = self.begin_write()?;
        let old = {
            Self::bucket_exists(&tx.open_table(BUCKETS)?, bucket)?;
            let mut t = tx.open_table(OBJECTS)?;
            let cur: Option<Object> = match t.get((bucket, key))? {
                Some(v) => Some(serde_json::from_slice(v.value())?),
                None => None,
            };
            if let Some(m) = if_match {
                match &cur {
                    Some(o) if m == "*" || etag_matches(m, &o.etag) => {}
                    _ => return Err(Error::PreconditionFailed),
                }
            }
            match cur {
                None => return Ok(()),
                Some(o) => {
                    t.remove((bucket, key))?;
                    o.segments
                }
            }
        };
        Self::queue_gc(&tx, &old)?;
        tx.commit()?;
        self.collect();
        Ok(())
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
        mut rng: impl FnMut(&Object) -> std::result::Result<(u64, u64), E>,
    ) -> std::result::Result<(Object, ObjectReader), FreshError<E>> {
        for attempt in 0.. {
            let obj = self.get_object(bucket, key).map_err(FreshError::Engine)?;
            let (off, n) = rng(&obj).map_err(|e| FreshError::Range(obj.clone(), e))?;
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
            let cur: Option<Object> = match tx.open_table(OBJECTS)?.get((bucket, key))? {
                Some(v) => Some(serde_json::from_slice(v.value())?),
                None => None,
            };
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
            };
            let v = serde_json::to_vec(&obj)?;
            tx.open_table(OBJECTS)?.insert((bucket, key), v.as_slice())?;
            let mut garbage: Vec<Segment> = cur.map(|c| c.segments).unwrap_or_default();
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
        Ok(st)
    }
}

/// Why `get_fresh` failed: the engine, or the range picker (with the object it saw).
pub enum FreshError<E> {
    Engine(Error),
    Range(Object, E),
}

#[cfg(test)]
mod tests;
