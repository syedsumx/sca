//! Small-file packing and compaction.
//!
//! Blobs up to `CAIRN_STORE_PACK_BELOW` bytes are appended to pack files (`packs/<id>.pack`, rotated at
//! `CAIRN_STORE_PACK_FILE_BYTES`) instead of getting a file each: millions of small objects become a few
//! large files, and reading one is a seek in an open file. A packed blob's id names its place,
//! `p:<pack>:<offset>:<length>` (the stored length, encryption included), so the index, garbage
//! collection, tiering and replication carry it like any other blob id.
//!
//! The bytes are appended (and synced) before the index entry that refers to them is committed, as for
//! every blob, so a crash can leave unreferenced bytes in a pack, never a reference to missing bytes.
//! Deleting a packed blob leaves dead space; compaction copies the live blobs of a pack that is mostly
//! dead into the active pack, swaps each reference in one index transaction (only where the reference is
//! unchanged), and deletes the old pack once nothing refers to it (never while a backup is running).

use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::sync::atomic::Ordering;

use serde::Serialize;

use super::kv::ReadableTable as _;
use super::{new_id, Engine, Error, Object, Part, Result, Segment, OBJECTS, PARTS, VERSIONS};

pub struct ActivePack {
    id: String,
    file: File,
    len: u64,
}

/// (pack, offset, length) of a packed blob id.
pub fn parse(id: &str) -> Option<(&str, u64, u64)> {
    let rest = id.strip_prefix("p:")?;
    let mut it = rest.splitn(3, ':');
    let pack = it.next()?;
    let off = it.next()?.parse().ok()?;
    let len = it.next()?.parse().ok()?;
    Some((pack, off, len))
}

pub fn is_packed(id: &str) -> bool {
    id.starts_with("p:")
}

#[derive(Debug, Default, Serialize)]
pub struct CompactReport {
    pub packs: u64,
    pub pack_bytes: u64,
    pub live_bytes: u64,
    pub packs_compacted: u64,
    pub packs_deleted: u64,
    pub blobs_moved: u64,
    pub bytes_reclaimed: u64,
}

impl Engine {
    pub(super) fn packs_dir(&self) -> PathBuf {
        self.dir.join("packs")
    }

    pub(super) fn pack_path(&self, pack: &str) -> PathBuf {
        self.packs_dir().join(format!("{pack}.pack"))
    }

    /// Packs blobs up to `below` bytes (0: off) into pack files of up to `max` bytes.
    pub fn set_packing(&self, below: u64, max: u64) {
        self.pack_below.store(below, Ordering::SeqCst);
        self.pack_max.store(max.max(1 << 20), Ordering::SeqCst);
    }

    pub(super) fn packs_small(&self, size: u64) -> bool {
        let below = self.pack_below.load(Ordering::SeqCst);
        below > 0 && size <= below
    }

    /// Appends bytes to the active pack (made durable when the store syncs); returns the blob id.
    pub(super) fn append_packed(&self, data: &[u8]) -> Result<String> {
        let mut active = self.pack.lock().unwrap();
        if active.as_ref().is_none_or(|a| a.len >= self.pack_max.load(Ordering::SeqCst)) {
            fs::create_dir_all(self.packs_dir())?;
            let id = new_id();
            let file = OpenOptions::new().create_new(true).append(true).open(self.pack_path(&id))?;
            if self.fsync {
                File::open(self.packs_dir())?.sync_all()?;
            }
            *active = Some(ActivePack { id, file, len: 0 });
        }
        let a = active.as_mut().unwrap();
        let off = a.len;
        if let Err(e) = a.file.write_all(data).and_then(|_| if self.fsync { a.file.sync_data() } else { Ok(()) }) {
            *active = None; // a torn tail: start a fresh pack next time
            return Err(Error::Io(e));
        }
        a.len += data.len() as u64;
        Ok(format!("p:{}:{off}:{}", a.id, data.len()))
    }

    /// The stored bytes of a packed blob from `offset` on.
    pub(super) fn open_packed(&self, id: &str, offset: u64) -> Result<Box<dyn Read + Send>> {
        let (pack, off, len) = parse(id).ok_or_else(|| Error::Index(format!("bad packed blob id {id}")))?;
        let mut f = File::open(self.pack_path(pack))?;
        f.seek(SeekFrom::Start(off + offset))?;
        Ok(Box::new(f.take(len.saturating_sub(offset))))
    }

    /// A segment's stored bytes (for copying it to a tier) and their length.
    pub fn blob_reader(&self, id: &str) -> Result<(Box<dyn Read + Send>, u64)> {
        if let Some((_, _, len)) = parse(id) {
            return Ok((self.open_packed(id, 0)?, len));
        }
        let f = File::open(self.blob_path(id))?;
        let len = f.metadata()?.len();
        Ok((Box::new(f), len))
    }

    fn active_pack(&self) -> Option<String> {
        self.pack.lock().unwrap().as_ref().map(|a| a.id.clone())
    }

    /// Every packed blob id the index refers to (local segments of objects and versions, and parts).
    fn packed_refs(&self) -> Result<HashSet<String>> {
        let r = self.db.begin_read()?;
        let mut refs = HashSet::new();
        let mut add = |o: Object| {
            refs.extend(o.segments.into_iter().filter(|s| s.tier.is_empty() && is_packed(&s.blob)).map(|s| s.blob));
        };
        for row in r.open_table(OBJECTS)?.iter()? {
            add(serde_json::from_slice(row?.1.value())?);
        }
        for row in r.open_table(VERSIONS)?.iter()? {
            add(serde_json::from_slice(row?.1.value())?);
        }
        for row in r.open_table(PARTS)?.iter()? {
            let p: Part = serde_json::from_slice(row?.1.value())?;
            if is_packed(&p.blob) {
                refs.insert(p.blob);
            }
        }
        Ok(refs)
    }

    /// Replaces packed blob ids (old -> new) wherever the index still refers to the old one, in one
    /// transaction.
    fn swap_packed(&self, moved: &HashMap<String, String>) -> Result<u64> {
        let tx = self.begin_write()?;
        let mut swapped = 0u64;
        let fix = |segs: &mut Vec<Segment>| -> bool {
            let mut changed = false;
            for s in segs.iter_mut().filter(|s| s.tier.is_empty()) {
                if let Some(n) = moved.get(&s.blob) {
                    s.blob = n.clone();
                    changed = true;
                }
            }
            changed
        };
        {
            let mut t = tx.open_table(OBJECTS)?;
            let mut updates = vec![];
            for row in t.iter()? {
                let (k, v) = row?;
                let mut o: Object = serde_json::from_slice(v.value())?;
                if fix(&mut o.segments) {
                    let (b, key) = k.value();
                    updates.push((b.to_string(), key.to_string(), serde_json::to_vec(&o)?));
                }
            }
            for (b, key, v) in updates {
                t.insert((b.as_str(), key.as_str()), v.as_slice())?;
                swapped += 1;
            }
        }
        {
            let mut t = tx.open_table(VERSIONS)?;
            let mut updates = vec![];
            for row in t.iter()? {
                let (k, v) = row?;
                let mut o: Object = serde_json::from_slice(v.value())?;
                if fix(&mut o.segments) {
                    let (b, key, ord) = k.value();
                    updates.push((b.to_string(), key.to_string(), ord.to_string(), serde_json::to_vec(&o)?));
                }
            }
            for (b, key, ord, v) in updates {
                t.insert((b.as_str(), key.as_str(), ord.as_str()), v.as_slice())?;
                swapped += 1;
            }
        }
        {
            let mut t = tx.open_table(PARTS)?;
            let mut updates = vec![];
            for row in t.iter()? {
                let (k, v) = row?;
                let mut p: Part = serde_json::from_slice(v.value())?;
                if let Some(n) = moved.get(&p.blob) {
                    p.blob = n.clone();
                    let (id, num) = k.value();
                    updates.push((id.to_string(), num, serde_json::to_vec(&p)?));
                }
            }
            for (id, num, v) in updates {
                t.insert((id.as_str(), num), v.as_slice())?;
                swapped += 1;
            }
        }
        tx.commit()?;
        Ok(swapped)
    }

    /// Compacts packs whose dead share is at least `min_dead` (0..1) and deletes packs nothing refers to.
    /// `dry`: only measure.
    pub fn compact(&self, min_dead: f64, dry: bool) -> Result<CompactReport> {
        let _one = self.compacting.lock().unwrap();
        let mut rep = CompactReport::default();
        let dir = self.packs_dir();
        if !dir.exists() {
            return Ok(rep);
        }
        let active = self.active_pack();
        let refs = self.packed_refs()?;
        let mut live: HashMap<String, Vec<String>> = HashMap::new();
        for id in &refs {
            if let Some((pack, _, _)) = parse(id) {
                live.entry(pack.to_string()).or_default().push(id.clone());
            }
        }
        for ent in fs::read_dir(&dir)? {
            let path = ent?.path();
            let Some(pack) = path.file_stem().and_then(|s| s.to_str()).map(str::to_string) else { continue };
            if path.extension().is_none_or(|e| e != "pack") {
                continue;
            }
            let size = fs::metadata(&path)?.len();
            let ids = live.get(&pack).cloned().unwrap_or_default();
            let live_bytes: u64 = ids.iter().filter_map(|i| parse(i)).map(|(_, _, l)| l).sum();
            rep.packs += 1;
            rep.pack_bytes += size;
            rep.live_bytes += live_bytes;
            if dry || active.as_deref() == Some(pack.as_str()) || self.backups.load(Ordering::SeqCst) > 0 {
                continue;
            }
            if ids.is_empty() {
                if fs::remove_file(&path).is_ok() {
                    rep.packs_deleted += 1;
                    rep.bytes_reclaimed += size;
                }
                continue;
            }
            let dead = size.saturating_sub(live_bytes) as f64 / size.max(1) as f64;
            if dead < min_dead {
                continue;
            }
            // Copy the live blobs into the active pack, then swap the references.
            let mut moved = HashMap::new();
            for id in &ids {
                let (_, _, len) = parse(id).unwrap();
                let mut buf = Vec::with_capacity(len as usize);
                self.open_packed(id, 0)?.read_to_end(&mut buf)?;
                if buf.len() as u64 != len {
                    return Err(Error::Index(format!("pack {pack}: blob {id} is short")));
                }
                moved.insert(id.clone(), self.append_packed(&buf)?);
            }
            self.swap_packed(&moved)?;
            rep.packs_compacted += 1;
            rep.blobs_moved += moved.len() as u64;
            // Delete the pack once nothing refers to it any more (a reference added meanwhile keeps it).
            let still: HashSet<String> = self.packed_refs()?;
            if !still.iter().any(|i| parse(i).is_some_and(|(p, _, _)| p == pack)) && self.backups.load(Ordering::SeqCst) == 0 {
                let _ = fs::remove_file(&path);
                rep.bytes_reclaimed += size.saturating_sub(live_bytes);
                rep.packs_deleted += 1;
            }
        }
        if rep.packs_compacted > 0 || rep.packs_deleted > 0 {
            tracing::info!(
                compacted = rep.packs_compacted,
                deleted = rep.packs_deleted,
                moved = rep.blobs_moved,
                reclaimed = rep.bytes_reclaimed,
                "pack compaction"
            );
        }
        Ok(rep)
    }
}
