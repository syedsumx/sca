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
        let blobs: HashSet<String> = keep.iter().flat_map(|m| m.blobs.keys().map(|b| format!("blobs/{b}"))).collect();
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
