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

use crate::audit::{self, Audit, Entry};
use crate::crypt::{self, ChunkCipher, Key};
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
/// Per-bucket usage: (stored bytes incl. versions and upload parts, object versions). Kept in the same
/// transactions as the writes, and recomputed by recovery at start-up.
const USAGE: TableDefinition<&str, (u64, u64)> = TableDefinition::new("usage");
/// Event notifications waiting for delivery: sequence -> OutboxEntry. Written in the same transaction as
/// the change they describe, removed once delivered, so no event is lost to a crash.
const OUTBOX: TableDefinition<u64, &[u8]> = TableDefinition::new("outbox");
/// The hash-chained audit trail: sequence -> audit::Entry. Entries are appended in the same transaction
/// as the change they record and never modified.
const AUDIT: TableDefinition<u64, &[u8]> = TableDefinition::new("audit");

#[derive(Debug)]
pub enum Error {
    NoSuchBucket,
    BucketExists,
    BucketNotEmpty,
    NoSuchKey,
    NoSuchUpload,
    NoSuchVersion,
    /// The write would take the bucket over its quota.
    QuotaExceeded,
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
    /// Plaintext size.
    #[serde(rename = "s")]
    pub size: u64,
    /// Encrypted blobs: the chunk nonce prefix (hex). Empty: stored in the clear.
    #[serde(rename = "n", default, skip_serializing_if = "String::is_empty")]
    pub nonce: String,
    /// The storage tier (a storage class) holding the blob; empty: this store's disk.
    #[serde(rename = "t", default, skip_serializing_if = "String::is_empty")]
    pub tier: String,
}

/// Remote storage tiers: where lifecycle transitions move blobs (the bytes exactly as stored on disk, so
/// encrypted blobs stay encrypted). Implemented by the S3 layer.
pub trait TierStore: Send + Sync {
    /// Reads a tiered blob from byte `offset` on.
    fn open(&self, tier: &str, blob: &str, offset: u64) -> io::Result<Box<dyn Read + Send>>;
    /// Deletes a tiered blob (deleting a missing one succeeds).
    fn delete(&self, tier: &str, blob: &str) -> io::Result<()>;
}

/// How a version is encrypted at rest. The data key is stored only wrapped by a keyring key
/// (SSE-S3, SSE-KMS); for customer keys (SSE-C) only a salted HMAC of the key is kept, to check it.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Sse {
    /// "AES256" (SSE-S3), "aws:kms" (SSE-KMS) or "SSE-C".
    pub alg: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub key_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub wrapped: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub key_md5: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub key_hmac: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub salt: String,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sse: Option<Sse>,
    /// Multipart objects: each part's checksums (for GetObjectAttributes).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub part_checksums: Vec<BTreeMap<String, String>>,
    /// Bucket replication: PENDING, COMPLETED or FAILED (empty: not replicated).
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub replication_status: String,
}

impl Object {
    /// The storage class: the tier holding its bytes, else STANDARD.
    pub fn storage_class(&self) -> &str {
        self.segments.iter().find(|s| !s.tier.is_empty()).map(|s| s.tier.as_str()).unwrap_or("STANDARD")
    }
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

/// Who and what caused a change, for event notifications.
#[derive(Clone, Debug, Default)]
pub struct Ev {
    /// For writes: Put, Copy, Post, CompleteMultipartUpload (default Put). For metadata changes: the
    /// whole event name, e.g. "ObjectTagging:Put" (empty: no event).
    pub kind: &'static str,
    pub principal: String,
    pub source_ip: String,
    pub request_id: String,
    /// The change is a lifecycle expiration.
    pub lifecycle: bool,
    /// Record this in the audit trail, in the same transaction as the change.
    pub audit: Option<Audit>,
}

/// A bucket's notification rule (configuration kind "notification").
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct NotifyRule {
    pub id: String,
    pub target: String,
    pub events: Vec<String>,
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub suffix: String,
    /// The destination ARN and its kind (Queue, Topic, CloudFunction), as configured.
    #[serde(default)]
    pub arn: String,
    #[serde(default)]
    pub kind: String,
}

impl NotifyRule {
    pub fn matches(&self, event: &str, key: &str) -> bool {
        key.starts_with(&self.prefix)
            && key.ends_with(&self.suffix)
            && self.events.iter().any(|p| match p.strip_suffix('*') {
                Some(pre) => event.starts_with(pre),
                None => p == event,
            })
    }
}

/// A bucket's replication configuration (configuration kind "replication").
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct ReplConfig {
    /// The operator-defined destination (endpoint and credentials).
    pub target: String,
    pub role: String,
    pub rules: Vec<ReplRule>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct ReplRule {
    pub id: String,
    #[serde(default)]
    pub priority: i64,
    pub enabled: bool,
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub tags: BTreeMap<String, String>,
    pub dest_bucket: String,
    #[serde(default)]
    pub delete_markers: bool,
    /// Whether the rule used <Filter> (V2) or a top-level <Prefix> (V1), to echo it back the same way.
    #[serde(default)]
    pub legacy: bool,
}

impl ReplConfig {
    /// The rule that replicates this version (highest priority first), if any.
    pub fn rule_for(&self, o: &Object) -> Option<&ReplRule> {
        let mut rules: Vec<&ReplRule> = self
            .rules
            .iter()
            .filter(|r| r.enabled && o.key.starts_with(&r.prefix) && r.tags.iter().all(|(k, v)| o.tags.get(k) == Some(v)))
            .filter(|r| !o.delete_marker || r.delete_markers)
            .collect();
        rules.sort_by_key(|r| -r.priority);
        rules.into_iter().next()
    }
}

/// The outbox target prefix of replication deliveries: "@replicate:<target>:<destination bucket>".
pub const REPLICATE: &str = "@replicate:";

/// An event waiting in the outbox.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct OutboxEntry {
    pub target: String,
    pub rule: String,
    /// e.g. "s3:ObjectCreated:Put".
    pub event: String,
    pub bucket: String,
    pub key: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub etag: String,
    #[serde(default)]
    pub version: String,
    pub time: u64,
    #[serde(default)]
    pub principal: String,
    #[serde(default)]
    pub source_ip: String,
    #[serde(default)]
    pub request_id: String,
    #[serde(default)]
    pub attempts: u32,
    /// Do not retry before (ms).
    #[serde(default)]
    pub next_at: u64,
}

/// A bucket's quota (configuration kind "quota").
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Quota {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_objects: Option<u64>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sse: Option<Sse>,
}

/// An uploaded part.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Part {
    pub number: u32,
    pub blob: String,
    pub size: u64,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub nonce: String,
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
    /// Backups in progress: while any runs, blobs are not deleted (their snapshot may still need them).
    backups: std::sync::atomic::AtomicUsize,
    /// Remote storage tiers, when configured.
    tiers: std::sync::RwLock<Option<std::sync::Arc<dyn TierStore>>>,
}

/// What a backup did.
#[derive(Debug, Default, Serialize)]
pub struct BackupReport {
    pub path: String,
    pub buckets: u64,
    pub objects: u64,
    pub versions: u64,
    pub blobs: u64,
    pub bytes: u64,
    /// Blobs hard-linked from the previous backup instead of copied.
    pub blobs_linked: u64,
    pub audit_entries: u64,
    pub millis: u64,
}

/// Writes the bytes of one segment. `seal` makes them durable; dropping an unsealed writer discards them.
pub struct BlobWriter<'a> {
    engine: &'a Engine,
    id: String,
    tmp: PathBuf,
    file: Option<File>,
    size: u64,
    md5: Md5,
    enc: Option<Encrypting>,
}

struct Encrypting {
    cipher: ChunkCipher,
    base: [u8; 8],
    idx: u32,
    buf: Vec<u8>,
}

impl BlobWriter<'_> {
    /// Appends plaintext (encrypted on the way to disk when the blob has a key).
    pub fn write(&mut self, data: &[u8]) -> io::Result<()> {
        self.size += data.len() as u64;
        self.md5.update(data);
        let file = self.file.as_mut().expect("blob already sealed");
        match &mut self.enc {
            None => file.write_all(data),
            Some(e) => {
                e.buf.extend_from_slice(data);
                while e.buf.len() >= crypt::CHUNK {
                    let rest = e.buf.split_off(crypt::CHUNK);
                    file.write_all(&e.cipher.seal(e.idx, &e.buf))?;
                    e.idx += 1;
                    e.buf = rest;
                }
                Ok(())
            }
        }
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
        let mut file = self.file.take().expect("blob already sealed");
        let mut nonce = String::new();
        if let Some(e) = self.enc.take() {
            if !e.buf.is_empty() {
                file.write_all(&e.cipher.seal(e.idx, &e.buf))?;
            }
            nonce = hex::encode(e.base);
        }
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
        Ok(Segment { blob: self.id.clone(), size: self.size, nonce, tier: String::new() })
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
    parts: Vec<Piece>,
}

/// One segment's share of a read.
enum Piece {
    /// A blob positioned at its start offset, and the bytes left.
    Plain(Box<dyn Read + Send>, u64),
    /// An encrypted blob: decrypts chunk by chunk.
    Sealed {
        file: Box<dyn Read + Send>,
        cipher: Box<ChunkCipher>,
        chunk: u32,
        seg_size: u64,
        skip: usize,
        left: u64,
        buf: Vec<u8>,
        pos: usize,
    },
}

impl Read for ObjectReader {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        while let Some(p) = self.parts.first_mut() {
            match p {
                Piece::Plain(f, left) => {
                    if *left == 0 {
                        self.parts.remove(0);
                        continue;
                    }
                    let want = out.len().min(*left as usize);
                    let n = f.read(&mut out[..want])?;
                    if n == 0 {
                        return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "blob shorter than its index entry"));
                    }
                    *left -= n as u64;
                    return Ok(n);
                }
                Piece::Sealed { file, cipher, chunk, seg_size, skip, left, buf, pos } => {
                    if *left == 0 {
                        self.parts.remove(0);
                        continue;
                    }
                    if *pos >= buf.len() {
                        let start = *chunk as u64 * crypt::CHUNK as u64;
                        let plain = (*seg_size - start).min(crypt::CHUNK as u64) as usize;
                        let mut sealed = vec![0u8; plain + crypt::TAG];
                        file.read_exact(&mut sealed)
                            .map_err(|_| io::Error::new(io::ErrorKind::UnexpectedEof, "encrypted blob shorter than its index entry"))?;
                        *buf = cipher.open(*chunk, &sealed)?;
                        *chunk += 1;
                        *pos = std::mem::take(skip);
                    }
                    let n = out.len().min(buf.len() - *pos).min(*left as usize);
                    out[..n].copy_from_slice(&buf[*pos..*pos + n]);
                    *pos += n;
                    *left -= n as u64;
                    return Ok(n);
                }
            }
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
        let e = Engine { dir, db, fsync, gc_lock: Mutex::new(()), backups: Default::default(), tiers: Default::default() };
        let tx = e.begin_write()?;
        tx.open_table(BUCKETS)?;
        tx.open_table(OBJECTS)?;
        tx.open_table(UPLOADS)?;
        tx.open_table(PARTS)?;
        tx.open_table(GC)?;
        tx.open_table(CONFIG)?;
        tx.open_table(VERSIONS)?;
        tx.open_table(META)?;
        tx.open_table(USAGE)?;
        tx.open_table(OUTBOX)?;
        tx.open_table(AUDIT)?;
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
        Ok(BlobWriter { engine: self, id, tmp, file: Some(file), size: 0, md5: Md5::new(), enc: None })
    }

    /// Starts a blob that is encrypted with `key` (None: in the clear).
    pub fn new_blob_with(&self, key: Option<&Key>) -> Result<BlobWriter<'_>> {
        let mut w = self.new_blob()?;
        if let Some(k) = key {
            let base = crypt::random_nonce_base();
            w.enc = Some(Encrypting { cipher: ChunkCipher::new(k, base), base, idx: 0, buf: Vec::with_capacity(crypt::CHUNK) });
        }
        Ok(w)
    }

    /// Removes sealed blobs that were never committed.
    pub fn discard(&self, segs: &[Segment]) {
        for s in segs.iter().filter(|s| s.tier.is_empty()) {
            let _ = fs::remove_file(self.blob_path(&s.blob));
        }
    }

    /// Queues blobs for collection; their bytes no longer count towards the bucket's usage.
    fn queue_gc(tx: &redb::WriteTransaction, bucket: &str, segs: &[Segment]) -> Result<()> {
        let mut gc = tx.open_table(GC)?;
        let now = now_ms();
        for s in segs {
            // Tiered blobs are queued as "<tier>:<blob>" and deleted by collect_tiered.
            let id = if s.tier.is_empty() { s.blob.clone() } else { format!("{}:{}", s.tier, s.blob) };
            gc.insert(id.as_str(), now)?;
        }
        drop(gc);
        let bytes: u64 = segs.iter().map(|s| s.size).sum();
        if bytes > 0 {
            Self::adjust(tx, bucket, -(bytes as i64), 0)?;
        }
        Ok(())
    }

    fn usage_in<T: ReadableTable<&'static str, (u64, u64)>>(t: &T, bucket: &str) -> Result<(u64, u64)> {
        Ok(t.get(bucket)?.map(|v| v.value()).unwrap_or((0, 0)))
    }

    /// Adds to a bucket's usage counters.
    fn adjust(tx: &redb::WriteTransaction, bucket: &str, bytes: i64, objects: i64) -> Result<()> {
        let mut t = tx.open_table(USAGE)?;
        let (b, o) = Self::usage_in(&t, bucket)?;
        let v = ((b as i64 + bytes).max(0) as u64, (o as i64 + objects).max(0) as u64);
        t.insert(bucket, v)?;
        Ok(())
    }

    fn usage_tx(tx: &redb::WriteTransaction, bucket: &str) -> Result<(u64, u64)> {
        Self::usage_in(&tx.open_table(USAGE)?, bucket)
    }

    /// Refuses a write that took usage past the bucket's quota (only growth is refused, so deletes and
    /// overwrites that shrink usage always succeed).
    fn quota_guard(tx: &redb::WriteTransaction, bucket: &str, before: (u64, u64)) -> Result<()> {
        let Some(doc) = tx.open_table(CONFIG)?.get((bucket, "quota"))?.map(|v| v.value().to_vec()) else { return Ok(()) };
        let q: Quota = serde_json::from_slice(&doc)?;
        let after = Self::usage_tx(tx, bucket)?;
        if q.max_bytes.is_some_and(|m| after.0 > m && after.0 > before.0)
            || q.max_objects.is_some_and(|m| after.1 > m && after.1 > before.1)
        {
            return Err(Error::QuotaExceeded);
        }
        Ok(())
    }

    /// Records the event for every notification rule of the bucket that matches it.
    fn emit(tx: &redb::WriteTransaction, bucket: &str, event: &str, o: &Object, ev: &Ev) -> Result<()> {
        let name = format!("s3:{event}");
        // Bucket replication: new versions and (when enabled) delete markers go to the rule's destination.
        if (event.starts_with("ObjectCreated:") || event.ends_with("DeleteMarkerCreated")) && !ev.lifecycle {
            if let Some(doc) = tx.open_table(CONFIG)?.get((bucket, "replication"))?.map(|v| v.value().to_vec()) {
                let cfg: ReplConfig = serde_json::from_slice(&doc)?;
                if let Some(rule) = cfg.rule_for(o) {
                    let target = format!("{REPLICATE}{}:{}", cfg.target, rule.dest_bucket);
                    Self::outbox_in(tx, bucket, &target, &rule.id, &name, o, ev)?;
                    if !o.delete_marker {
                        let mut t = tx.open_table(OBJECTS)?;
                        let cur = Self::current_in(&t, bucket, &o.key)?;
                        if let Some(mut c) = cur.filter(|c| c.version_id == o.version_id) {
                            c.replication_status = "PENDING".into();
                            let v = serde_json::to_vec(&c)?;
                            t.insert((bucket, o.key.as_str()), v.as_slice())?;
                        }
                    }
                }
            }
        }
        let Some(doc) = tx.open_table(CONFIG)?.get((bucket, "notification"))?.map(|v| v.value().to_vec()) else { return Ok(()) };
        let rules: Vec<NotifyRule> = serde_json::from_slice(&doc)?;
        for r in rules.iter().filter(|r| r.matches(&name, &o.key)) {
            Self::outbox_in(tx, bucket, &r.target, &r.id, &name, o, ev)?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn outbox_in(tx: &redb::WriteTransaction, bucket: &str, target: &str, rule: &str, name: &str, o: &Object, ev: &Ev) -> Result<()> {
        {
            let mut meta = tx.open_table(META)?;
            let seq = meta.get("outbox_seq")?.map(|v| v.value()).unwrap_or(0) + 1;
            meta.insert("outbox_seq", seq)?;
            drop(meta);
            let entry = OutboxEntry {
                target: target.to_string(),
                rule: rule.to_string(),
                event: name.to_string(),
                bucket: bucket.into(),
                key: o.key.clone(),
                size: o.size,
                etag: o.etag.clone(),
                version: if o.version_id.is_empty() { String::new() } else { o.version_id.clone() },
                time: now_ms(),
                principal: ev.principal.clone(),
                source_ip: ev.source_ip.clone(),
                request_id: ev.request_id.clone(),
                attempts: 0,
                next_at: 0,
            };
            let v = serde_json::to_vec(&entry)?;
            tx.open_table(OUTBOX)?.insert(seq, v.as_slice())?;
        }
        Ok(())
    }

    /// The oldest events waiting for delivery.
    pub fn outbox(&self, limit: usize) -> Result<Vec<(u64, OutboxEntry)>> {
        let r = self.db.begin_read()?;
        let mut out = vec![];
        for row in r.open_table(OUTBOX)?.iter()?.take(limit) {
            let (k, v) = row?;
            out.push((k.value(), serde_json::from_slice(v.value())?));
        }
        Ok(out)
    }

    pub fn outbox_len(&self) -> Result<u64> {
        Ok(self.db.begin_read()?.open_table(OUTBOX)?.len()?)
    }

    /// Removes delivered (or abandoned) events, and records attempts for the rest.
    pub fn outbox_done(&self, done: &[u64], retry: &[(u64, OutboxEntry)]) -> Result<()> {
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(OUTBOX)?;
            for s in done {
                t.remove(*s)?;
            }
            for (s, e) in retry {
                let v = serde_json::to_vec(e)?;
                t.insert(*s, v.as_slice())?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    /// Appends to the audit trail inside `tx`.
    fn audit_in(tx: &redb::WriteTransaction, a: &Audit) -> Result<()> {
        let mut t = tx.open_table(AUDIT)?;
        let (seq, prev) = match t.last()? {
            Some((k, v)) => {
                let e: Entry = serde_json::from_slice(v.value())?;
                (k.value() + 1, e.hash)
            }
            None => (1, audit::GENESIS.to_string()),
        };
        let e = Entry::new(seq, &prev, a, time::OffsetDateTime::now_utc());
        let v = serde_json::to_vec(&e)?;
        t.insert(seq, v.as_slice())?;
        Ok(())
    }

    /// Appends one audit entry on its own (for events that change nothing, e.g. refused requests).
    pub fn append_audit(&self, a: &Audit) -> Result<()> {
        let tx = self.begin_write()?;
        Self::audit_in(&tx, a)?;
        tx.commit()?;
        Ok(())
    }

    /// Audit entries after `after` (oldest first).
    pub fn audit_entries(&self, after: u64, limit: usize) -> Result<Vec<Entry>> {
        let r = self.db.begin_read()?;
        let mut out = vec![];
        for row in r.open_table(AUDIT)?.range(after + 1..)?.take(limit) {
            out.push(serde_json::from_slice(row?.1.value())?);
        }
        Ok(out)
    }

    /// A bucket's usage: (stored bytes, object versions).
    pub fn usage(&self, bucket: &str) -> Result<(u64, u64)> {
        let r = self.db.begin_read()?;
        Self::bucket_exists(&r.open_table(BUCKETS)?, bucket)?;
        Self::usage_in(&r.open_table(USAGE)?, bucket)
    }

    /// Deletes blobs queued for collection.
    fn collect(&self) {
        if self.backups.load(std::sync::atomic::Ordering::SeqCst) > 0 {
            return; // a backup may still copy these; they are collected after it
        }
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
        let ids: Vec<String> = ids.into_iter().filter(|id| !id.contains(':')).collect();
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
        // Usage is recomputed from what is stored, so the counters cannot drift across restarts.
        let mut usage: HashMap<String, (u64, u64)> = HashMap::new();
        {
            let r = self.db.begin_read()?;
            for row in r.open_table(BUCKETS)?.iter()? {
                usage.insert(row?.0.value().to_string(), (0, 0));
            }
            let mut count = |bucket: &str, bytes: u64, objects: u64| {
                let e = usage.entry(bucket.to_string()).or_default();
                e.0 += bytes;
                e.1 += objects;
            };
            for row in r.open_table(OBJECTS)?.iter()? {
                let (k, v) = row?;
                let o: Object = serde_json::from_slice(v.value())?;
                count(k.value().0, o.size, 1);
                refs.extend(o.segments.into_iter().filter(|s| s.tier.is_empty()).map(|s| s.blob));
            }
            let mut upload_bucket: HashMap<String, String> = HashMap::new();
            for row in r.open_table(UPLOADS)?.iter()? {
                let (k, v) = row?;
                let u: Upload = serde_json::from_slice(v.value())?;
                upload_bucket.insert(k.value().to_string(), u.bucket);
            }
            for row in r.open_table(PARTS)?.iter()? {
                let (k, v) = row?;
                let p: Part = serde_json::from_slice(v.value())?;
                if let Some(b) = upload_bucket.get(k.value().0) {
                    count(b, p.size, 0);
                }
                refs.insert(p.blob);
            }
            for row in r.open_table(VERSIONS)?.iter()? {
                let (k, v) = row?;
                let o: Object = serde_json::from_slice(v.value())?;
                if !o.delete_marker {
                    count(k.value().0, o.size, 1);
                }
                refs.extend(o.segments.into_iter().filter(|s| s.tier.is_empty()).map(|s| s.blob));
            }
        }
        {
            let tx = self.begin_write()?;
            {
                let mut t = tx.open_table(USAGE)?;
                let stale: Vec<String> = t.iter()?.filter_map(|r| r.ok().map(|(k, _)| k.value().to_string())).collect();
                for b in stale {
                    t.remove(b.as_str())?;
                }
                for (b, u) in &usage {
                    t.insert(b.as_str(), *u)?;
                }
            }
            tx.commit()?;
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
        self.create_bucket_with(name, "", &[], None)
    }

    /// Creates a bucket with its versioning state and configuration documents, in one transaction.
    pub fn create_bucket_with(&self, name: &str, versioning: &str, configs: &[(&str, &[u8])], audit: Option<&Audit>) -> Result<()> {
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
        if let Some(a) = audit {
            Self::audit_in(&tx, a)?;
        }
        tx.commit()?;
        Ok(())
    }

    #[cfg(test)]
    pub fn delete_bucket(&self, name: &str) -> Result<()> {
        self.delete_bucket_audited(name, None)
    }

    pub fn delete_bucket_audited(&self, name: &str, audit: Option<&Audit>) -> Result<()> {
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
            tx.open_table(USAGE)?.remove(name)?;
        }
        if let Some(a) = audit {
            Self::audit_in(&tx, a)?;
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
    #[cfg(test)]
    pub fn put_config(&self, bucket: &str, kind: &str, doc: Option<&[u8]>) -> Result<()> {
        self.put_config_audited(bucket, kind, doc, None)
    }

    /// Sets or removes a configuration document, recording `audit` in the same transaction.
    pub fn put_config_audited(&self, bucket: &str, kind: &str, doc: Option<&[u8]>, audit: Option<&Audit>) -> Result<()> {
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
        if let Some(a) = audit {
            Self::audit_in(&tx, a)?;
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
    #[cfg(test)]
    pub fn put_object(&self, bucket: &str, obj: Object, cond: &Condition) -> Result<Object> {
        self.put_object_ev(bucket, obj, cond, &Ev::default())
    }

    pub fn put_object_ev(&self, bucket: &str, obj: Object, cond: &Condition, ev: &Ev) -> Result<Object> {
        let segs = obj.segments.clone();
        let res = (|| -> Result<(Object, Vec<Segment>)> {
            let tx = self.begin_write()?;
            let (stored, garbage) = {
                let info = Self::bucket_in(&tx.open_table(BUCKETS)?, bucket)?;
                let cur = Self::current_in(&tx.open_table(OBJECTS)?, bucket, &obj.key)?;
                check_condition(cur.as_ref(), cond)?;
                let before = Self::usage_tx(&tx, bucket)?;
                let out = Self::install(&tx, &info.versioning, bucket, obj, cur)?;
                Self::queue_gc(&tx, bucket, &out.1)?;
                Self::quota_guard(&tx, bucket, before)?;
                Self::emit(&tx, bucket, &format!("ObjectCreated:{}", if ev.kind.is_empty() { "Put" } else { ev.kind }), &out.0, ev)?;
                out
            };
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
                if !o.delete_marker {
                    Self::adjust(tx, bucket, 0, -1)?;
                }
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
                        Self::adjust(tx, bucket, 0, -1)?;
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
                    Self::adjust(tx, bucket, 0, -1)?;
                }
            }
        }
        Self::adjust(tx, bucket, obj.size as i64, 1)?;
        let v = serde_json::to_vec(&obj)?;
        tx.open_table(OBJECTS)?.insert((bucket, obj.key.as_str()), v.as_slice())?;
        Ok((obj, garbage))
    }

    /// Sets a bucket's versioning state ("Enabled" or "Suspended").
    #[cfg(test)]
    pub fn set_versioning(&self, bucket: &str, state: &str) -> Result<()> {
        self.set_versioning_audited(bucket, state, None)
    }

    pub fn set_versioning_audited(&self, bucket: &str, state: &str, audit: Option<&Audit>) -> Result<()> {
        let tx = self.begin_write()?;
        {
            let mut t = tx.open_table(BUCKETS)?;
            let mut info = Self::bucket_in(&t, bucket)?;
            info.versioning = state.to_string();
            let v = serde_json::to_vec(&info)?;
            t.insert(bucket, v.as_slice())?;
        }
        if let Some(a) = audit {
            Self::audit_in(&tx, a)?;
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
    #[cfg(test)]
    pub fn update_object(
        &self,
        bucket: &str,
        key: &str,
        version: Option<&str>,
        f: impl FnOnce(&mut Object) -> Result<()>,
    ) -> Result<Object> {
        self.update_object_ev(bucket, key, version, f, &Ev::default())
    }

    pub fn update_object_ev(
        &self,
        bucket: &str,
        key: &str,
        version: Option<&str>,
        f: impl FnOnce(&mut Object) -> Result<()>,
        ev: &Ev,
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
        if !ev.kind.is_empty() {
            Self::emit(&tx, bucket, ev.kind, &obj, ev)?;
        }
        if let Some(a) = &ev.audit {
            Self::audit_in(&tx, a)?;
        }
        tx.commit()?;
        Ok(obj)
    }

    /// Deletes a key: removes it (unversioned bucket) or puts a delete marker on top (versioned).
    /// Deleting a missing key succeeds (S3 semantics).
    #[cfg(test)]
    pub fn delete_object(&self, bucket: &str, key: &str, if_match: Option<&str>) -> Result<Deleted> {
        self.delete_object_ev(bucket, key, if_match, &Ev::default())
    }

    pub fn delete_object_ev(&self, bucket: &str, key: &str, if_match: Option<&str>, ev: &Ev) -> Result<Deleted> {
        let removed = if ev.lifecycle { "LifecycleExpiration:Delete" } else { "ObjectRemoved:Delete" };
        let marked = if ev.lifecycle { "LifecycleExpiration:DeleteMarkerCreated" } else { "ObjectRemoved:DeleteMarkerCreated" };
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
                        Self::emit(&tx, bucket, removed, &o, ev)?;
                        garbage = o.segments;
                        Self::adjust(&tx, bucket, 0, -1)?;
                    }
                    Deleted::default()
                }
                mode => {
                    if let Some(o) = cur {
                        t.remove((bucket, key))?;
                        if mode == "Suspended" && o.version_id.is_empty() {
                            garbage = o.segments;
                            Self::adjust(&tx, bucket, 0, -1)?;
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
                    Self::emit(&tx, bucket, marked, &marker, ev)?;
                    Deleted { version_id: Some(marker.version().to_string()), delete_marker: true }
                }
            };
            (out, garbage)
        };
        Self::queue_gc(&tx, bucket, &garbage)?;
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
    #[cfg(test)]
    pub fn delete_version(&self, bucket: &str, key: &str, version: &str, bypass_governance: bool) -> Result<Deleted> {
        self.delete_version_ev(bucket, key, version, bypass_governance, &Ev::default())
    }

    pub fn delete_version_ev(&self, bucket: &str, key: &str, version: &str, bypass_governance: bool, ev: &Ev) -> Result<Deleted> {
        let removed = if ev.lifecycle { "LifecycleExpiration:Delete" } else { "ObjectRemoved:Delete" };
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
                    Self::emit(&tx, bucket, removed, &c, ev)?;
                    garbage = c.segments;
                    Self::adjust(&tx, bucket, 0, -1)?;
                }
                cur => {
                    if let Some((order, o)) = Self::versions_in(&vt, bucket, key)?.into_iter().find(|(_, o)| o.version_id == want) {
                        if o.lock.protects(now, bypass_governance) {
                            return Err(Error::ObjectLocked);
                        }
                        vt.remove((bucket, key, order.as_str()))?;
                        Self::emit(&tx, bucket, removed, &o, ev)?;
                        out.delete_marker = o.delete_marker;
                        if !o.delete_marker {
                            Self::adjust(&tx, bucket, 0, -1)?;
                        }
                        garbage = o.segments;
                    }
                    if cur.is_some() {
                        drop((t, vt));
                        return Self::finish_delete(self, tx, bucket, out, garbage, ev);
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
        Self::finish_delete(self, tx, bucket, out, garbage, ev)
    }

    fn finish_delete(&self, tx: redb::WriteTransaction, bucket: &str, out: Deleted, garbage: Vec<Segment>, ev: &Ev) -> Result<Deleted> {
        if let Some(a) = &ev.audit {
            Self::audit_in(&tx, a)?;
        }
        Self::queue_gc(&tx, bucket, &garbage)?;
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

    /// Opens a segment's blob (on disk or in its tier) at byte `offset` of the blob.
    fn open_blob(&self, s: &Segment, offset: u64) -> Result<Box<dyn Read + Send>> {
        if s.tier.is_empty() {
            let mut f = File::open(self.blob_path(&s.blob))?;
            f.seek(SeekFrom::Start(offset))?;
            return Ok(Box::new(f));
        }
        let tiers = self.tiers.read().unwrap().clone();
        let t = tiers.ok_or_else(|| Error::Io(io::Error::other(format!("storage tier {} is not configured", s.tier))))?;
        Ok(t.open(&s.tier, &s.blob, offset)?)
    }

    /// Configures the remote storage tiers.
    pub fn set_tiers(&self, t: std::sync::Arc<dyn TierStore>) {
        *self.tiers.write().unwrap() = Some(t);
    }

    /// A blob on disk, for copying it to a tier.
    pub fn blob_file(&self, id: &str) -> io::Result<File> {
        File::open(self.blob_path(id))
    }

    /// Moves a version's blobs to a tier: `new` replaces the segments (the same blobs, now in a tier), if
    /// the version still has the segments `expect` (it was not rewritten meanwhile). The local copies are
    /// then collected. Returns whether the version was changed.
    pub fn transition(&self, bucket: &str, key: &str, version: &str, expect: &[Segment], new: Vec<Segment>) -> Result<bool> {
        let tx = self.begin_write()?;
        let done = {
            let mut t = tx.open_table(OBJECTS)?;
            let cur = Self::current_in(&t, bucket, key)?;
            match cur {
                Some(mut o) if o.version_id == version => {
                    if o.segments != expect {
                        false
                    } else {
                        o.segments = new;
                        t.insert((bucket, key), serde_json::to_vec(&o)?.as_slice())?;
                        true
                    }
                }
                _ => {
                    drop(t);
                    let mut vt = tx.open_table(VERSIONS)?;
                    match Self::versions_in(&vt, bucket, key)?.into_iter().find(|(_, o)| o.version_id == version) {
                        Some((order, mut o)) if !o.delete_marker && o.segments == expect => {
                            o.segments = new;
                            vt.insert((bucket, key, order.as_str()), serde_json::to_vec(&o)?.as_slice())?;
                            true
                        }
                        _ => false,
                    }
                }
            }
        };
        if !done {
            return Ok(false);
        }
        {
            // The bytes now live in the tier: the local blobs go without changing the bucket's usage.
            let mut gc = tx.open_table(GC)?;
            let now = now_ms();
            for s in expect.iter().filter(|s| s.tier.is_empty()) {
                gc.insert(s.blob.as_str(), now)?;
            }
        }
        tx.commit()?;
        self.collect();
        Ok(true)
    }

    /// Deletes tiered blobs queued for collection (remote calls: run from a background task).
    pub fn collect_tiered(&self) -> usize {
        let Some(tiers) = self.tiers.read().unwrap().clone() else { return 0 };
        let ids: Vec<String> = (|| -> Result<Vec<String>> {
            let r = self.db.begin_read()?;
            let mut ids = vec![];
            for row in r.open_table(GC)?.iter()? {
                let id = row?.0.value().to_string();
                if id.contains(':') {
                    ids.push(id);
                }
            }
            Ok(ids)
        })()
        .unwrap_or_default();
        let mut gone = vec![];
        for id in ids {
            let (tier, blob) = id.split_once(':').unwrap_or_default();
            match tiers.delete(tier, blob) {
                Ok(()) => gone.push(id),
                Err(err) => tracing::warn!(tier, blob, error = %err, "tiered blob not deleted; will retry"),
            }
        }
        if !gone.is_empty() {
            if let Ok(tx) = self.begin_write() {
                if let Ok(mut t) = tx.open_table(GC) {
                    for id in &gone {
                        let _ = t.remove(id.as_str());
                    }
                }
                let _ = tx.commit();
            }
        }
        gone.len()
    }

    /// Opens `[off, off+n)` of an object; `key` decrypts encrypted segments.
    pub fn reader(&self, obj: &Object, off: u64, n: u64, key: Option<&Key>) -> Result<ObjectReader> {
        let mut parts = vec![];
        let end = off + n;
        let mut pos = 0u64;
        for s in &obj.segments {
            let (start, stop) = (pos, pos + s.size);
            pos = stop;
            if stop <= off || start >= end || s.size == 0 {
                continue;
            }
            let from = off.max(start) - start;
            let to = end.min(stop) - start;
            if s.nonce.is_empty() {
                parts.push(Piece::Plain(self.open_blob(s, from)?, to - from));
                continue;
            }
            let k = key.ok_or_else(|| Error::Io(io::Error::new(io::ErrorKind::InvalidInput, "encrypted object read without its key")))?;
            let base: [u8; 8] = hex::decode(&s.nonce)
                .ok()
                .and_then(|b| b.try_into().ok())
                .ok_or_else(|| Error::Index(format!("segment {}: bad nonce", s.blob)))?;
            let chunk = (from / crypt::CHUNK as u64) as u32;
            let f = self.open_blob(s, chunk as u64 * (crypt::CHUNK + crypt::TAG) as u64)?;
            parts.push(Piece::Sealed {
                file: f,
                cipher: Box::new(ChunkCipher::new(k, base)),
                chunk,
                seg_size: s.size,
                skip: (from % crypt::CHUNK as u64) as usize,
                left: to - from,
                buf: vec![],
                pos: 0,
            });
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
        mut rng: impl FnMut(&Object) -> std::result::Result<(u64, u64, Option<Key>), E>,
    ) -> std::result::Result<(Object, ObjectReader), FreshError<E>> {
        for attempt in 0.. {
            let obj = self.get_version(bucket, key, version).map_err(FreshError::Engine)?;
            if obj.delete_marker {
                return Err(FreshError::Engine(Error::MethodNotAllowed));
            }
            let (off, n, key) = rng(&obj).map_err(|e| FreshError::Range(Box::new(obj.clone()), e))?;
            match self.reader(&obj, off, n, key.as_ref()) {
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
        let new_seg = Segment { blob: p.blob.clone(), size: p.size, nonce: p.nonce.clone(), tier: String::new() };
        let res = (|| -> Result<Vec<Segment>> {
            let tx = self.begin_write()?;
            let old = {
                Self::upload_in(&tx.open_table(UPLOADS)?, bucket, key, id)?;
                let mut t = tx.open_table(PARTS)?;
                let old = match t.get((id, p.number))? {
                    Some(v) => {
                        let op: Part = serde_json::from_slice(v.value())?;
                        vec![Segment { blob: op.blob, size: op.size, nonce: op.nonce, tier: String::new() }]
                    }
                    None => vec![],
                };
                let v = serde_json::to_vec(&p)?;
                t.insert((id, p.number), v.as_slice())?;
                old
            };
            let before = Self::usage_tx(&tx, bucket)?;
            Self::adjust(&tx, bucket, p.size as i64, 0)?;
            Self::queue_gc(&tx, bucket, &old)?;
            Self::quota_guard(&tx, bucket, before)?;
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
        let segs: Vec<Segment> =
            parts.into_iter().map(|p| Segment { blob: p.blob, size: p.size, nonce: p.nonce, tier: String::new() }).collect();
        Self::queue_gc(&tx, bucket, &segs)?;
        tx.commit()?;
        self.collect();
        Ok(())
    }

    /// Assembles the listed parts into the object. No bytes are copied: the object refers to the part
    /// blobs. `finish` computes the object's checksums from the chosen parts; it may fail the completion.
    #[cfg(test)]
    pub fn complete_upload(
        &self,
        bucket: &str,
        key: &str,
        id: &str,
        want: &[CompletePart],
        cond: &Condition,
        finish: impl FnOnce(&Upload, &[Part]) -> Result<(BTreeMap<String, String>, String)>,
    ) -> Result<Object> {
        self.complete_upload_ev(bucket, key, id, want, cond, finish, &Ev::default())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn complete_upload_ev(
        &self,
        bucket: &str,
        key: &str,
        id: &str,
        want: &[CompletePart],
        cond: &Condition,
        finish: impl FnOnce(&Upload, &[Part]) -> Result<(BTreeMap<String, String>, String)>,
        ev: &Ev,
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
                segments: parts
                    .iter()
                    .map(|p| Segment { blob: p.blob.clone(), size: p.size, nonce: p.nonce.clone(), tier: String::new() })
                    .collect(),
                part_count: parts.len() as u32,
                tags: u.tags.clone(),
                lock: u.lock.clone(),
                sse: u.sse.clone(),
                part_checksums: if parts.iter().any(|p| !p.checksums.is_empty()) {
                    parts.iter().map(|p| p.checksums.clone()).collect()
                } else {
                    vec![]
                },
                ..Default::default()
            };
            let before = Self::usage_tx(&tx, bucket)?;
            let (obj, mut garbage) = Self::install(&tx, &info.versioning, bucket, obj, cur)?;
            // The chosen parts' bytes were counted when they were uploaded; they are now the object's.
            Self::adjust(&tx, bucket, -(obj.size as i64), 0)?;
            garbage.extend(have.values().filter(|p| !chosen.contains(&p.number)).map(|p| Segment {
                blob: p.blob.clone(),
                size: p.size,
                nonce: p.nonce.clone(),
                tier: String::new(),
            }));
            Self::drop_upload(&tx, id)?;
            Self::queue_gc(&tx, bucket, &garbage)?;
            Self::quota_guard(&tx, bucket, before)?;
            Self::emit(&tx, bucket, "ObjectCreated:CompleteMultipartUpload", &obj, ev)?;
            (obj, garbage)
        };
        tx.commit()?;
        if !garbage.is_empty() {
            self.collect();
        }
        Ok(obj)
    }

    /// Writes a consistent backup of the store to `dest` (a new directory): the index as of one read
    /// snapshot and every blob that snapshot refers to. Blobs found in `link_from` (an earlier backup) are
    /// hard-linked instead of copied, as blobs never change. The backup is itself a data directory: a
    /// store started on it (or on a copy) has everything up to the snapshot. Writers are not blocked;
    /// deleted blobs are kept until the backup is done. Event deliveries pending at the snapshot are not
    /// included, so a restored store does not send them again.
    pub fn backup(&self, dest: &Path, link_from: Option<&Path>) -> Result<BackupReport> {
        use std::sync::atomic::Ordering;
        struct Running<'a>(&'a std::sync::atomic::AtomicUsize);
        impl Drop for Running<'_> {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::SeqCst);
            }
        }
        let started = std::time::Instant::now();
        if dest.exists() {
            return Err(Error::Io(io::Error::new(io::ErrorKind::AlreadyExists, format!("{} exists", dest.display()))));
        }
        self.backups.fetch_add(1, Ordering::SeqCst);
        let _running = Running(&self.backups);
        let mut rep = BackupReport { path: dest.display().to_string(), ..Default::default() };
        let work = dest.with_extension("partial");
        let _ = fs::remove_dir_all(&work);
        for d in [work.clone(), work.join("blobs"), work.join("tmp")] {
            fs::create_dir_all(&d)?;
        }
        let out = Database::create(work.join("index.redb"))?;
        let mut blobs: HashSet<String> = HashSet::new();
        {
            let r = self.db.begin_read()?;
            let w = out.begin_write()?;
            macro_rules! copy {
                ($t:expr, $kty:ty, $vty:ty, $on:expr) => {{
                    let src = r.open_table($t)?;
                    let mut dst = w.open_table($t)?;
                    for row in src.iter()? {
                        let (k, v) = row?;
                        let (k, v): ($kty, $vty) = (k.value(), v.value());
                        #[allow(clippy::redundant_closure_call)]
                        ($on)(&k, &v)?;
                        dst.insert(k, v)?;
                    }
                }};
            }
            let mut on_object = |o: &[u8]| -> Result<()> {
                let o: Object = serde_json::from_slice(o)?;
                blobs.extend(o.segments.into_iter().filter(|s| s.tier.is_empty()).map(|s| s.blob)); // tiered blobs stay in their tier
                Ok(())
            };
            copy!(BUCKETS, &str, &[u8], |_: &&str, _: &&[u8]| -> Result<()> {
                rep.buckets += 1;
                Ok(())
            });
            copy!(OBJECTS, (&str, &str), &[u8], |_: &(&str, &str), v: &&[u8]| -> Result<()> {
                rep.objects += 1;
                on_object(v)
            });
            copy!(VERSIONS, (&str, &str, &str), &[u8], |_: &(&str, &str, &str), v: &&[u8]| -> Result<()> {
                rep.versions += 1;
                on_object(v)
            });
            copy!(PARTS, (&str, u32), &[u8], |_: &(&str, u32), v: &&[u8]| -> Result<()> {
                let p: Part = serde_json::from_slice(v)?;
                blobs.insert(p.blob);
                Ok(())
            });
            copy!(UPLOADS, &str, &[u8], |_: &&str, _: &&[u8]| -> Result<()> { Ok(()) });
            copy!(CONFIG, (&str, &str), &[u8], |_: &(&str, &str), _: &&[u8]| -> Result<()> { Ok(()) });
            copy!(META, &str, u64, |_: &&str, _: &u64| -> Result<()> { Ok(()) });
            copy!(USAGE, &str, (u64, u64), |_: &&str, _: &(u64, u64)| -> Result<()> { Ok(()) });
            copy!(AUDIT, u64, &[u8], |_: &u64, _: &&[u8]| -> Result<()> {
                rep.audit_entries += 1;
                Ok(())
            });
            w.open_table(GC)?;
            w.open_table(OUTBOX)?;
            w.commit()?;
        }
        drop(out);
        for id in &blobs {
            let src = self.blob_path(id);
            let rel = src.strip_prefix(&self.dir).map_err(|e| Error::Index(e.to_string()))?.to_path_buf();
            let dst = work.join(&rel);
            fs::create_dir_all(dst.parent().unwrap())?;
            let linked = link_from.map(|p| p.join(&rel)).filter(|p| p.exists()).is_some_and(|p| fs::hard_link(&p, &dst).is_ok());
            if linked {
                rep.blobs_linked += 1;
            } else {
                fs::copy(&src, &dst)?;
                if self.fsync {
                    File::open(&dst)?.sync_all()?;
                }
            }
            rep.blobs += 1;
            rep.bytes += fs::metadata(&dst)?.len();
        }
        if self.fsync {
            File::open(&work)?.sync_all()?;
        }
        fs::rename(&work, dest)?;
        rep.millis = started.elapsed().as_millis() as u64;
        drop(_running);
        self.collect(); // what was kept for the backup can go now
        Ok(rep)
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
