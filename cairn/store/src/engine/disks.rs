//! Several disks per store, each with a type (hdd, ssd, ...).
//!
//! The data directory is disk 0; `CAIRN_STORE_DISKS` adds more (`/mnt/a:hdd,/mnt/b:ssd`). The index, packs
//! and temporary files of disk 0 stay in the data directory; blob files go to the disk with the most free
//! space among those of the bucket's disk type (the store's default type for buckets without one). A
//! blob's id names its disk (`v<NN><hex>`), so reads go straight to it.
//!
//! Each added disk gets a marker file (`cairn-disk.json`: the store it belongs to and its number), so its
//! number survives reordering the list, and a disk of another store is refused. The data directory keeps
//! the list of disks it has used (`disks.json`): a store whose list misses one of them refuses to start,
//! because the blobs on it would be lost.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use super::{new_id, Error, Result};

#[derive(Clone, Debug)]
pub struct DiskSpec {
    pub path: PathBuf,
    pub kind: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct Disk {
    pub idx: u8,
    pub path: PathBuf,
    #[serde(rename = "type")]
    pub kind: String,
}

/// Parses `CAIRN_STORE_DISKS` (`path:type,path:type`).
pub fn parse_specs(v: &str) -> std::result::Result<Vec<DiskSpec>, String> {
    let mut out = vec![];
    for item in v.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let (path, kind) = item.rsplit_once(':').ok_or_else(|| format!("{item:?}: path:type, e.g. /mnt/ssd1:ssd"))?;
        if !valid_kind(kind) {
            return Err(format!("{item:?}: a disk type is 1-16 lowercase letters and digits"));
        }
        if !path.starts_with('/') {
            return Err(format!("{item:?}: an absolute path"));
        }
        out.push(DiskSpec { path: path.into(), kind: kind.into() });
    }
    Ok(out)
}

pub fn valid_kind(k: &str) -> bool {
    !k.is_empty() && k.len() <= 16 && k.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
}

/// The prefix of blob ids on disk `idx` (none for disk 0).
pub fn prefix(idx: u8) -> String {
    if idx == 0 {
        String::new()
    } else {
        format!("v{idx:02}")
    }
}

/// The disk a blob id names (0: the data directory).
pub fn disk_of(id: &str) -> u8 {
    let b = id.as_bytes();
    if b.len() > 3 && b[0] == b'v' && b[1].is_ascii_digit() && b[2].is_ascii_digit() {
        (b[1] - b'0') * 10 + (b[2] - b'0')
    } else {
        0
    }
}

/// Free and total bytes of the filesystem holding `path`.
pub fn space(path: &Path) -> (u64, u64) {
    use std::os::unix::ffi::OsStrExt;
    let Ok(c) = std::ffi::CString::new(path.as_os_str().as_bytes()) else { return (0, 0) };
    let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: c is a valid NUL-terminated path and s a properly sized, writable statvfs.
    if unsafe { libc::statvfs(c.as_ptr(), &mut s) } != 0 {
        return (0, 0);
    }
    let bs = s.f_frsize as u64;
    (s.f_bavail as u64 * bs, s.f_blocks as u64 * bs)
}

#[derive(Default, Serialize, Deserialize)]
struct Known {
    /// Identifies this store to its disks.
    owner: String,
    disks: Vec<KnownDisk>,
}

#[derive(Serialize, Deserialize)]
struct KnownDisk {
    idx: u8,
    path: PathBuf,
    kind: String,
}

#[derive(Serialize, Deserialize)]
struct Marker {
    owner: String,
    idx: u8,
}

/// Prepares the disks: the data directory (disk 0, of type `main_kind`) and `specs`.
pub fn setup(main: &Path, main_kind: &str, specs: &[DiskSpec]) -> Result<Vec<Disk>> {
    if !valid_kind(main_kind) {
        return Err(Error::Index("CAIRN_STORE_DATA_DISK_TYPE: 1-16 lowercase letters and digits".into()));
    }
    let known_file = main.join("disks.json");
    let mut known: Known = match fs::read(&known_file) {
        Ok(b) => serde_json::from_slice(&b)?,
        Err(_) => Known { owner: new_id(), disks: vec![] },
    };
    let mut disks = vec![Disk { idx: 0, path: main.to_path_buf(), kind: main_kind.to_string() }];
    for s in specs {
        for d in [s.path.clone(), s.path.join("blobs"), s.path.join("tmp")] {
            fs::create_dir_all(&d).map_err(|e| Error::Index(format!("disk {}: {e}", s.path.display())))?;
        }
        if s.path.canonicalize().ok() == main.canonicalize().ok() {
            return Err(Error::Index(format!("disk {} is the data directory", s.path.display())));
        }
        let marker_file = s.path.join("cairn-disk.json");
        let idx = match fs::read(&marker_file) {
            Ok(b) => {
                let m: Marker = serde_json::from_slice(&b)?;
                if m.owner != known.owner {
                    return Err(Error::Index(format!("disk {} belongs to another store; refusing to use it", s.path.display())));
                }
                m.idx
            }
            Err(_) => {
                let used: Vec<u8> = known.disks.iter().map(|d| d.idx).chain(disks.iter().map(|d| d.idx)).collect();
                let idx = (1..=99u8).find(|i| !used.contains(i)).ok_or_else(|| Error::Index("at most 99 disks".into()))?;
                fs::write(&marker_file, serde_json::to_vec(&Marker { owner: known.owner.clone(), idx })?)?;
                idx
            }
        };
        if disks.iter().any(|d| d.idx == idx) {
            return Err(Error::Index(format!("disk {} is listed twice", s.path.display())));
        }
        disks.push(Disk { idx, path: s.path.clone(), kind: s.kind.clone() });
    }
    for k in &known.disks {
        if !disks.iter().any(|d| d.idx == k.idx) {
            return Err(Error::Index(format!(
                "disk {} ({}, {}) is missing from CAIRN_STORE_DISKS: the blobs on it would be lost",
                k.idx,
                k.path.display(),
                k.kind
            )));
        }
    }
    known.disks =
        disks.iter().filter(|d| d.idx > 0).map(|d| KnownDisk { idx: d.idx, path: d.path.clone(), kind: d.kind.clone() }).collect();
    if !specs.is_empty() || known_file.exists() {
        fs::write(&known_file, serde_json::to_vec_pretty(&known)?)?;
    }
    Ok(disks)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_specs_and_markers() {
        assert_eq!(disk_of("v03abcdef"), 3);
        assert_eq!(disk_of("0123abcd"), 0);
        assert_eq!(disk_of("dead"), 0);
        assert_eq!(prefix(7), "v07");
        assert!(parse_specs("/a:ssd,/b:hdd").unwrap().len() == 2);
        assert!(parse_specs("relative:ssd").is_err() && parse_specs("/a:SSD").is_err() && parse_specs("/a").is_err());
        let main = tempfile::tempdir().unwrap();
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        let spec = |p: &Path, k: &str| DiskSpec { path: p.to_path_buf(), kind: k.into() };
        let d = setup(main.path(), "hdd", &[spec(a.path(), "ssd"), spec(b.path(), "hdd")]).unwrap();
        assert_eq!(d.iter().map(|d| d.idx).collect::<Vec<_>>(), vec![0, 1, 2]);
        // Reordered: the numbers stay with the disks.
        let d = setup(main.path(), "hdd", &[spec(b.path(), "hdd"), spec(a.path(), "ssd")]).unwrap();
        assert_eq!(d.iter().find(|x| x.path == a.path()).unwrap().idx, 1);
        // A known disk left out: refused.
        assert!(setup(main.path(), "hdd", &[spec(a.path(), "ssd")]).is_err());
        // A disk of another store: refused.
        let other = tempfile::tempdir().unwrap();
        assert!(setup(other.path(), "hdd", &[spec(a.path(), "ssd")]).is_err());
        assert!(space(main.path()).1 > 0);
    }
}
