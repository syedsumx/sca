//! Backups to S3-compatible sinks (another Cairn Store, AWS S3, MinIO, GCS through its S3 interface...),
//! incremental and restorable.
//!
//! Sinks are defined by the operator (`CAIRN_STORE_BACKUP_SINKS_FILE`: `[{"id", "endpoint", "bucket",
//! "prefix", "access_key", "secret_key" | "secret_key_file", "region"}]`). A backup is the same consistent
//! snapshot as a local one: the index as of one moment (an embedded index file) and every blob it refers
//! to, uploaded exactly as stored, so encrypted objects stay encrypted. Under the sink's prefix:
//!
//! ```text
//! blobs/<id>                    blob files, shared by every backup (immutable, uploaded once)
//! packs/<pack>.<length>         a pack of small blobs as it was (closed packs are shared too)
//! backups/<name>/index.redb     the index snapshot
//! backups/<name>/manifest.json  written last: a backup without one is incomplete and not listed
//! ```
//!
//! Deleting a backup removes its manifest and index, then the blobs and packs no remaining backup refers
//! to. `cairn-store restore-backup <sink> <name> <dir>` downloads one into a data directory that a store
//! can start on. Backups can also run on a schedule, keeping the newest N.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::errors::{self as e, ApiError};
use super::remote::Remote;
use super::Server;
use crate::engine::{self, BackupReport, Engine};

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct Manifest {
    pub name: String,
    pub created: u64,
    pub version: String,
    pub objects: u64,
    pub versions: u64,
    pub bytes: u64,
    /// Blob id -> stored length.
    pub blobs: BTreeMap<String, u64>,
    /// Pack id -> length backed up.
    pub packs: BTreeMap<String, u64>,
    /// The last change the snapshot includes (continuous backups replay the journal after it).
    #[serde(default)]
    pub journal_seq: u64,
}

/// One journalled change (continuous backup): `journal/<seq>.json`.
#[derive(Debug, Serialize, Deserialize)]
pub struct JournalRecord {
    pub seq: u64,
    pub time: u64,
    /// "put" or "delete".
    pub op: String,
    pub bucket: String,
    pub key: String,
    /// put: the version, its segments naming blobs in the sink (`sink_blobs`, same order).
    #[serde(default)]
    pub object: Option<engine::Object>,
    #[serde(default)]
    pub sink_blobs: Vec<String>,
}

/// The sink name of a blob's bytes (packed and cluster ids are not plain file names).
fn sink_name(id: &str) -> String {
    if id.chars().all(|c| c.is_ascii_alphanumeric()) {
        return id.to_string();
    }
    use sha2::Digest;
    format!("j{}", hex::encode(&sha2::Sha256::digest(id.as_bytes())[..20]))
}

fn journal_key(seq: u64) -> String {
    format!("journal/{seq:020}.json")
}

pub struct Sinks {
    pub remotes: BTreeMap<String, Remote>,
    /// One backup or deletion at a time (a deletion prunes what an unfinished backup may still need).
    lock: Mutex<()>,
}

impl Sinks {
    pub fn new(specs: Vec<super::remote::RemoteSpec>) -> Result<Sinks, String> {
        let mut remotes = BTreeMap::new();
        for s in specs {
            remotes.insert(s.id.clone(), Remote::new(s)?);
        }
        Ok(Sinks { remotes, lock: Mutex::new(()) })
    }
}

fn io(e: std::io::Error) -> ApiError {
    tracing::warn!(error = %e, "backup sink");
    e::SINK_FAILED.clone()
}

pub fn valid_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 64 && !name.starts_with('.') && name.chars().all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
}

fn manifests(r: &Remote) -> std::io::Result<Vec<Manifest>> {
    let (_, prefixes) = r.list("backups/", "/")?;
    let mut out = vec![];
    for p in prefixes {
        match r.get_bytes(&format!("{p}manifest.json")) {
            Ok(b) => match serde_json::from_slice::<Manifest>(&b) {
                Ok(m) => out.push(m),
                Err(err) => tracing::warn!(backup = %p, error = %err, "unreadable backup manifest"),
            },
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {} // incomplete
            Err(err) => return Err(err),
        }
    }
    out.sort_by_key(|m| m.created);
    Ok(out)
}

impl Server {
    fn sink(&self, id: &str) -> Result<&Remote, ApiError> {
        let s = self.sinks.as_ref().ok_or_else(|| e::SINKS_OFF.clone())?;
        s.remotes.get(id).ok_or_else(|| e::NO_SUCH_SINK.clone())
    }

    /// Writes a backup named `name` to the sink `id` (blocking).
    pub(crate) fn backup_to_sink(&self, id: &str, name: &str) -> Result<Value, ApiError> {
        if !valid_name(name) {
            return Err(e::BACKUP_NAME.clone());
        }
        let r = self.sink(id)?;
        self.residency_backup_check(id)?;
        let _one = self.sinks.as_ref().unwrap().lock.lock().unwrap();
        let started = std::time::Instant::now();
        if r.head(&format!("backups/{name}/manifest.json")).map_err(io)?.is_some() {
            return Err(e::BACKUP_EXISTS.clone());
        }
        let _hold = self.eng.hold_collection();
        let (tmp, f) = self.eng.temp_file().map_err(engine::Error::Io)?;
        drop(f);
        let _ = fs::remove_file(&tmp);
        let mut rep = BackupReport::default();
        let res = (|| -> Result<(Manifest, u64, u64), ApiError> {
            let (blobs, packs) = self.eng.snapshot_index(&tmp, &mut rep)?;
            let have: HashMap<String, u64> = r.list("blobs/", "").map_err(io)?.0.into_iter().map(|l| (l.key, l.size)).collect();
            let have_packs: HashSet<String> = r.list("packs/", "").map_err(io)?.0.into_iter().map(|l| l.key).collect();
            let mut m = Manifest {
                name: name.into(),
                created: engine::now_ms(),
                version: crate::VERSION.into(),
                objects: rep.objects,
                versions: rep.versions,
                journal_seq: rep.journal_seq,
                ..Default::default()
            };
            let (mut uploaded, mut bytes) = (0u64, 0u64);
            for b in &blobs {
                let path = self.eng.blob_file(b);
                let len = fs::metadata(&path).map_err(engine::Error::Io)?.len();
                m.blobs.insert(b.clone(), len);
                m.bytes += len;
                if have.get(&format!("blobs/{b}")) == Some(&len) {
                    continue;
                }
                r.put(&format!("blobs/{b}"), Box::new(File::open(&path).map_err(engine::Error::Io)?), len, &[]).map_err(io)?;
                uploaded += 1;
                bytes += len;
            }
            for (p, len) in &packs {
                m.packs.insert(p.clone(), *len);
                m.bytes += len;
                let key = format!("packs/{p}.{len}");
                if have_packs.contains(&key) {
                    continue;
                }
                r.put(&key, Box::new(File::open(self.eng.pack_file(p)).map_err(engine::Error::Io)?), *len, &[]).map_err(io)?;
                uploaded += 1;
                bytes += len;
            }
            let ilen = fs::metadata(&tmp).map_err(engine::Error::Io)?.len();
            r.put(&format!("backups/{name}/index.redb"), Box::new(File::open(&tmp).map_err(engine::Error::Io)?), ilen, &[]).map_err(io)?;
            bytes += ilen;
            r.put_bytes(&format!("backups/{name}/manifest.json"), serde_json::to_vec_pretty(&m).unwrap(), "application/json")
                .map_err(io)?;
            Ok((m, uploaded, bytes))
        })();
        let _ = fs::remove_file(&tmp);
        let (m, uploaded, bytes) = res?;
        let out = json!({
            "name": name, "sink": id, "objects": m.objects, "versions": m.versions,
            "blobs": m.blobs.len() + m.packs.len(), "blobs_uploaded": uploaded, "bytes_uploaded": bytes,
            "bytes": m.bytes, "millis": started.elapsed().as_millis() as u64,
        });
        tracing::info!(sink = id, name, objects = m.objects, uploaded, bytes, "backup written to sink");
        Ok(out)
    }

    /// Data residency for a whole-store backup to the sink `id`: every bucket's pinned data may go to its region.
    pub(crate) fn residency_backup_check(&self, id: &str) -> Result<(), ApiError> {
        let dest = self.sink(id)?.spec.residency_region.clone();
        let buckets: Vec<String> = self.eng.list_buckets()?.into_iter().map(|b| b.name).collect();
        self.residency.check_buckets(&buckets, &dest).map_err(|why| {
            tracing::warn!(sink = id, reason = %why, "backup refused: data residency");
            e::RESIDENCY.clone()
        })
    }

    /// Ships one change to the sink's journal (continuous backup): the version's blobs first (each once),
    /// then the record, so a record never names a missing blob.
    pub(crate) async fn journal_backup(&self, sink: &str, seq: u64, ent: &engine::OutboxEntry) -> Result<(), super::replication::Failure> {
        use super::replication::Failure;
        let r = self.sink(sink).map_err(|e| Failure::Permanent(e.message.to_string()))?;
        // Data residency, for every change: pinned data reaches only a sink in a region its pin allows.
        self.residency
            .check_object(&ent.bucket, &ent.key, &r.spec.residency_region)
            .map_err(|why| Failure::Permanent(format!("data residency: {why}")))?;
        let put = ent.event.contains("ObjectCreated");
        let mut rec = JournalRecord {
            seq,
            time: ent.modified.max(ent.time),
            op: if put { "put" } else { "delete" }.into(),
            bucket: ent.bucket.clone(),
            key: ent.key.clone(),
            object: None,
            sink_blobs: vec![],
        };
        if put {
            let version = if ent.version.is_empty() { "null" } else { ent.version.as_str() };
            let obj = match tokio::task::block_in_place(|| self.eng.get_version(&ent.bucket, &ent.key, Some(version))) {
                Ok(o) if !o.delete_marker => o,
                _ => return Ok(()), // replaced or deleted since: its own journal entry follows
            };
            let _hold = self.eng.hold_collection();
            for s in &obj.segments {
                if !s.tier.is_empty() {
                    rec.sink_blobs.push(String::new()); // stays in its tier
                    continue;
                }
                let name = sink_name(&s.blob);
                let key = format!("blobs/{name}");
                let res = tokio::task::block_in_place(|| -> std::io::Result<()> {
                    if r.head(&key)?.is_some() {
                        return Ok(());
                    }
                    let (reader, len) = self.eng.blob_reader(&s.blob).map_err(|e| std::io::Error::other(e.to_string()))?;
                    r.put(&key, reader, len, &[]).map(|_| ())
                });
                match res {
                    Ok(()) => rec.sink_blobs.push(name),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()), // collected meanwhile: superseded
                    Err(e) => return Err(Failure::Retry(e.to_string())),
                }
            }
            rec.object = Some(obj);
        }
        let body = serde_json::to_vec(&rec).map_err(|e| Failure::Permanent(e.to_string()))?;
        tokio::task::block_in_place(|| r.put_bytes(&journal_key(seq), body, "application/json"))
            .map_err(|e| Failure::Retry(e.to_string()))?;
        Ok(())
    }

    /// The complete backups in a sink, oldest first.
    pub(crate) fn sink_backups(&self, id: &str) -> Result<Value, ApiError> {
        let r = self.sink(id)?;
        let list: Vec<Value> = manifests(r)
            .map_err(io)?
            .into_iter()
            .map(|m| {
                json!({"name": m.name, "created": super::iso_time(m.created), "objects": m.objects, "versions": m.versions,
                       "bytes": m.bytes, "blobs": m.blobs.len() + m.packs.len()})
            })
            .collect();
        Ok(json!({"sink": id, "backups": list}))
    }

    /// Deletes a backup from a sink, then the blobs no remaining backup refers to (blocking).
    pub(crate) fn delete_sink_backup(&self, id: &str, name: &str) -> Result<Value, ApiError> {
        if !valid_name(name) {
            return Err(e::BACKUP_NAME.clone());
        }
        let r = self.sink(id)?;
        let _one = self.sinks.as_ref().unwrap().lock.lock().unwrap();
        if r.head(&format!("backups/{name}/manifest.json")).map_err(io)?.is_none() {
            return Err(e::NO_SUCH_BACKUP.clone());
        }
        // The manifest first: from then on the backup is not listed, whatever happens next.
        r.delete(&format!("backups/{name}/manifest.json")).map_err(io)?;
        r.delete(&format!("backups/{name}/index.redb")).map_err(io)?;
        let keep = manifests(r).map_err(io)?;
        let mut blobs: HashSet<String> = keep.iter().flat_map(|m| m.blobs.keys().map(|b| format!("blobs/{b}"))).collect();
        // The journal before the oldest remaining backup is of no use; what follows it keeps its blobs.
        let from = keep.iter().map(|m| m.journal_seq).min().unwrap_or(u64::MAX);
        for l in r.list("journal/", "").map_err(io)?.0 {
            let seq: u64 = l.key.trim_start_matches("journal/").trim_end_matches(".json").parse().unwrap_or(0);
            if seq <= from {
                r.delete(&l.key).map_err(io)?;
            } else if let Ok(rec) = serde_json::from_slice::<JournalRecord>(&r.get_bytes(&l.key).map_err(io)?) {
                blobs.extend(rec.sink_blobs.into_iter().filter(|b| !b.is_empty()).map(|b| format!("blobs/{b}")));
            }
        }
        let packs: HashSet<String> = keep.iter().flat_map(|m| m.packs.iter().map(|(p, l)| format!("packs/{p}.{l}"))).collect();
        let (mut deleted, mut freed) = (0u64, 0u64);
        for (prefix, live) in [("blobs/", &blobs), ("packs/", &packs)] {
            for l in r.list(prefix, "").map_err(io)?.0 {
                if !live.contains(&l.key) {
                    r.delete(&l.key).map_err(io)?;
                    deleted += 1;
                    freed += l.size;
                }
            }
        }
        tracing::info!(sink = id, name, deleted, freed, "backup deleted from sink");
        Ok(json!({"name": name, "sink": id, "blobs_deleted": deleted, "bytes_freed": freed}))
    }

    /// Writes a backup to `sink` every `every`, named auto-<time>, keeping the newest `keep` of them.
    pub async fn run_sink_backups(self: Arc<Self>, sink: String, every: Duration, keep: usize) {
        let mut tick = tokio::time::interval(every);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        tick.tick().await;
        loop {
            tick.tick().await;
            let me = self.clone();
            let sink = sink.clone();
            let _ = tokio::task::spawn_blocking(move || {
                let now = time::OffsetDateTime::now_utc();
                let name = format!(
                    "auto-{:04}{:02}{:02}T{:02}{:02}{:02}Z",
                    now.year(),
                    now.month() as u8,
                    now.day(),
                    now.hour(),
                    now.minute(),
                    now.second()
                );
                match me.backup_to_sink(&sink, &name) {
                    Ok(_) => {
                        let _ = me.eng.append_audit(&crate::audit::Audit {
                            category: "store",
                            action: "backup.create".into(),
                            actor: "cairn-store".into(),
                            subject: name.clone(),
                            details: json!({"sink": sink, "scheduled": true}),
                        });
                    }
                    Err(err) => {
                        tracing::warn!(sink = %sink, error = %err.message, "scheduled backup failed; will retry at the next interval");
                        return;
                    }
                }
                let Ok(r) = me.sink(&sink) else { return };
                let autos: Vec<String> =
                    manifests(r).unwrap_or_default().into_iter().map(|m| m.name).filter(|n| n.starts_with("auto-")).collect();
                for old in autos.iter().take(autos.len().saturating_sub(keep)) {
                    if let Err(err) = me.delete_sink_backup(&sink, old) {
                        tracing::warn!(sink = %sink, backup = %old, error = %err.message, "pruning an old scheduled backup");
                    }
                }
            })
            .await;
        }
    }
}

/// Point-in-time restore: the newest backup made at or before `until` (or the backup `name`), then every
/// journalled change after it up to `until` (None: all of them). Returns the restore's report.
pub fn restore_until(r: &Remote, name: Option<&str>, until: Option<u64>, dir: &Path) -> Result<Value, String> {
    let all = manifests(r).map_err(|e| e.to_string())?;
    let base = match name {
        Some(n) => all.iter().find(|m| m.name == n).ok_or_else(|| format!("no backup {n}"))?,
        None => all
            .iter()
            .filter(|m| until.is_none_or(|t| m.created <= t))
            .max_by_key(|m| m.created)
            .ok_or("no backup was made before that time")?,
    };
    if until.is_some_and(|t| base.created > t) {
        return Err(format!("backup {} was made after that time", base.name));
    }
    let mut out = restore(r, &base.name, dir)?;
    let eng = Engine::open(dir, true).map_err(|e| e.to_string())?;
    eng.set_journal(None).map_err(|e| e.to_string())?; // the restored store does not write the old journal
    let mut records: Vec<String> = r.list("journal/", "").map_err(|e| e.to_string())?.0.into_iter().map(|l| l.key).collect();
    records.sort();
    let (mut applied, mut last) = (0u64, base.created);
    for k in records {
        let seq: u64 = k.trim_start_matches("journal/").trim_end_matches(".json").parse().unwrap_or(0);
        if seq <= base.journal_seq {
            continue;
        }
        let rec: JournalRecord =
            serde_json::from_slice(&r.get_bytes(&k).map_err(|e| format!("{k}: {e}"))?).map_err(|e| format!("{k}: {e}"))?;
        if until.is_some_and(|t| rec.time > t) {
            break;
        }
        match (rec.op.as_str(), rec.object) {
            ("put", Some(mut o)) => {
                for (s, name) in o.segments.iter_mut().zip(&rec.sink_blobs) {
                    if s.tier.is_empty() {
                        let mut w = eng.new_blob().map_err(|e| e.to_string())?;
                        let mut src = r.get(&format!("blobs/{name}"), 0).map_err(|e| format!("blobs/{name}: {e}"))?;
                        let mut buf = vec![0u8; 256 << 10];
                        loop {
                            let n = std::io::Read::read(&mut src, &mut buf).map_err(|e| e.to_string())?;
                            if n == 0 {
                                break;
                            }
                            w.write(&buf[..n]).map_err(|e| e.to_string())?;
                        }
                        let seg = w.seal().map_err(|e| e.to_string())?;
                        s.blob = seg.blob; // the bytes as stored (encrypted, compressed): nonce and table stay
                    }
                }
                o.replication_status.clear();
                eng.journal_put(&rec.bucket, o).map_err(|e| format!("{k}: {e}"))?;
            }
            _ => eng.journal_delete(&rec.bucket, &rec.key).map_err(|e| format!("{k}: {e}"))?,
        }
        applied += 1;
        last = rec.time;
    }
    out["base"] = base.name.clone().into();
    out["changes_applied"] = applied.into();
    out["restored_to"] = super::iso_time(last).into();
    Ok(out)
}

/// Downloads backup `name` from a sink into `dir` (which must not exist, or be empty): a data directory
/// with an embedded index that a store can start on. Blocking; run outside an async context.
pub fn restore(r: &Remote, name: &str, dir: &Path) -> Result<Value, String> {
    if !valid_name(name) {
        return Err("a backup name is 1-64 letters, digits, '.', '_' or '-'".into());
    }
    if dir.exists() && fs::read_dir(dir).map_err(|e| e.to_string())?.next().is_some() {
        return Err(format!("{} is not empty", dir.display()));
    }
    let m: Manifest =
        serde_json::from_slice(&r.get_bytes(&format!("backups/{name}/manifest.json")).map_err(|e| format!("backup {name}: {e}"))?)
            .map_err(|e| format!("backup {name}: manifest: {e}"))?;
    let work = dir.with_extension("partial");
    let _ = fs::remove_dir_all(&work);
    for d in [work.clone(), work.join("blobs"), work.join("tmp"), work.join("packs")] {
        fs::create_dir_all(&d).map_err(|e| e.to_string())?;
    }
    let fetch = |key: &str, dst: &Path, want: Option<u64>| -> Result<u64, String> {
        if let Some(p) = dst.parent() {
            fs::create_dir_all(p).map_err(|e| e.to_string())?;
        }
        let mut f = File::create(dst).map_err(|e| e.to_string())?;
        let n = std::io::copy(&mut r.get(key, 0).map_err(|e| format!("{key}: {e}"))?, &mut f).map_err(|e| format!("{key}: {e}"))?;
        if want.is_some_and(|w| w != n) {
            return Err(format!("{key}: {n} bytes, the manifest says {}", want.unwrap()));
        }
        f.sync_all().map_err(|e| e.to_string())?;
        Ok(n)
    };
    let mut bytes = fetch(&format!("backups/{name}/index.redb"), &work.join("index.redb"), None)?;
    for (b, len) in &m.blobs {
        if b.len() < 4 || !b.chars().all(|c| c.is_ascii_alphanumeric()) {
            return Err(format!("backup {name}: bad blob id {b:?}"));
        }
        bytes += fetch(&format!("blobs/{b}"), &work.join(Engine::blob_rel(b)), Some(*len))?;
    }
    for (p, len) in &m.packs {
        if !p.chars().all(|c| c.is_ascii_alphanumeric()) {
            return Err(format!("backup {name}: bad pack id {p:?}"));
        }
        bytes += fetch(&format!("packs/{p}.{len}"), &work.join("packs").join(format!("{p}.pack")), Some(*len))?;
    }
    if dir.exists() {
        fs::remove_dir(dir).map_err(|e| e.to_string())?;
    }
    fs::rename(&work, dir).map_err(|e| e.to_string())?;
    Ok(json!({"restored": name, "into": dir.display().to_string(), "objects": m.objects, "versions": m.versions,
              "blobs": m.blobs.len() + m.packs.len(), "bytes": bytes}))
}
