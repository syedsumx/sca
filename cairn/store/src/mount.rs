//! `cairn-store mount <bucket>[/prefix] <directory>`: a bucket as a local directory (FUSE), for tools
//! that only read and write files.
//!
//! The mount is an S3 client of the store (`CAIRN_STORE_ENDPOINT`, signed with `CAIRN_STORE_ACCESS_KEY`
//! and `CAIRN_STORE_SECRET_KEY`), so the key's policies, Cairn's policy decision point, quotas,
//! encryption, events and the audit trail apply as for any client. Reads are ranged GETs. Writes are
//! staged in a private temporary file and stored with one PUT when the file is closed (or flushed), so
//! readers never see half a file; `O_TRUNC` opens (`>`, `cp`) replace a file atomically.
//!
//! What objects cannot express fails with `EOPNOTSUPP` rather than being faked: appending, rewriting
//! part of a file, writing out of order, chmod, chown, setting times, links. Renaming a directory fails
//! with `EXDEV`, so `mv` falls back to copying and deleting. Directories are key prefixes; `mkdir`
//! writes an empty `name/` marker, as S3 consoles do.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::{Seek, Write};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use fuser::{
    Errno, FileAttr, FileHandle, FileType, Filesystem, FopenFlags, Generation, INodeNo, InitFlags, KernelConfig, LockOwner, MountOption,
    OpenAccMode, OpenFlags, RenameFlags, ReplyAttr, ReplyCreate, ReplyData, ReplyDirectory, ReplyEmpty, ReplyEntry, ReplyOpen, ReplyStatfs,
    ReplyWrite, Request, TimeOrNow, WriteFlags,
};
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;

use crate::s3::auth::aws_encode;
use crate::s3::xml;

type Body = BoxBody<Bytes, std::io::Error>;
const TTL: Duration = Duration::from_secs(1);

/// An S3 client for one bucket (and key prefix).
struct S3 {
    endpoint: String,
    bucket: String,
    prefix: String,
    ak: String,
    sk: String,
    region: String,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>,
    rt: tokio::runtime::Runtime,
}

struct Resp {
    status: u16,
    headers: hyper::HeaderMap,
    body: Bytes,
}

fn errno_of(status: u16) -> Errno {
    match status {
        404 => Errno::ENOENT,
        403 | 401 => Errno::EACCES,
        409 => Errno::EEXIST,
        412 => Errno::EEXIST,
        501 => Errno::EOPNOTSUPP,
        507 => Errno::ENOSPC,
        _ => Errno::EIO,
    }
}

impl S3 {
    fn key(&self, path: &str) -> String {
        format!("{}{path}", self.prefix)
    }

    fn call(&self, method: &str, key: &str, query: &str, extra: &[(&str, &str)], body: Body, len: Option<u64>) -> Result<Resp, Errno> {
        let mut url = format!("{}/{}/{}", self.endpoint, aws_encode(self.bucket.as_bytes(), false), aws_encode(key.as_bytes(), true));
        if !query.is_empty() {
            url.push('?');
            url.push_str(query);
        }
        let signed: Vec<(&str, &str)> = extra.iter().filter(|(k, _)| k.starts_with("x-amz-")).copied().collect();
        let mut req = hyper::Request::builder().method(method).uri(&url);
        for (k, v) in crate::admin_cli::sign_full(
            method,
            &url,
            "UNSIGNED-PAYLOAD",
            &signed,
            &self.ak,
            &self.sk,
            &self.region,
            time::OffsetDateTime::now_utc(),
        ) {
            req = req.header(k, v);
        }
        for (k, v) in extra {
            req = req.header(*k, *v);
        }
        if let Some(n) = len {
            req = req.header("content-length", n);
        }
        let req = req.body(body).map_err(|_| Errno::EIO)?;
        self.rt.block_on(async {
            let resp = tokio::time::timeout(Duration::from_secs(300), self.http.request(req))
                .await
                .map_err(|_| Errno::ETIMEDOUT)?
                .map_err(|e| {
                    tracing::warn!(error = %e, "store request failed");
                    Errno::EIO
                })?;
            let status = resp.status().as_u16();
            let headers = resp.headers().clone();
            let body = resp.into_body().collect().await.map_err(|_| Errno::EIO)?.to_bytes();
            Ok(Resp { status, headers, body })
        })
    }

    fn ok(&self, r: Resp) -> Result<Resp, Errno> {
        if r.status < 300 {
            Ok(r)
        } else {
            tracing::debug!(status = r.status, body = %String::from_utf8_lossy(&r.body), "store refused");
            Err(errno_of(r.status))
        }
    }

    fn empty() -> Body {
        Full::new(Bytes::new()).map_err(|never| match never {}).boxed()
    }

    /// (size, modified) of a file, or None.
    fn head(&self, path: &str) -> Result<Option<(u64, SystemTime)>, Errno> {
        let r = self.call("HEAD", &self.key(path), "", &[], Self::empty(), None)?;
        if r.status == 404 {
            return Ok(None);
        }
        let r = self.ok(r)?;
        let size = r.headers.get("content-length").and_then(|v| v.to_str().ok()?.parse().ok()).unwrap_or(0);
        let mtime = r
            .headers
            .get("last-modified")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| time::OffsetDateTime::parse(v, &time::format_description::well_known::Rfc2822).ok())
            .map(|t| UNIX_EPOCH + Duration::from_secs(t.unix_timestamp().max(0) as u64))
            .unwrap_or(UNIX_EPOCH);
        Ok(Some((size, mtime)))
    }

    /// One level of a directory: (subdirectories, files with size and time). `limit` stops early.
    #[allow(clippy::type_complexity)]
    fn list(&self, dir: &str, limit: usize) -> Result<(Vec<String>, Vec<(String, u64, SystemTime)>), Errno> {
        let prefix = self.key(dir);
        let (mut dirs, mut files) = (vec![], vec![]);
        let mut token = String::new();
        loop {
            let mut q = format!("list-type=2&delimiter=%2F&prefix={}", aws_encode(prefix.as_bytes(), false));
            if !token.is_empty() {
                q.push_str(&format!("&continuation-token={}", aws_encode(token.as_bytes(), false)));
            }
            let r = self.ok(self.call("GET", "", &q, &[], Self::empty(), None)?)?;
            let doc = xml::parse(&String::from_utf8_lossy(&r.body)).ok_or(Errno::EIO)?;
            for p in doc.all("CommonPrefixes") {
                let full = p.text_of_raw("Prefix");
                dirs.push(full[prefix.len()..].trim_end_matches('/').to_string());
            }
            for c in doc.all("Contents") {
                let key = c.text_of_raw("Key");
                if key == prefix {
                    continue; // the directory's own marker
                }
                let size = c.text_of("Size").parse().unwrap_or(0);
                let mtime = time::OffsetDateTime::parse(&c.text_of("LastModified"), &time::format_description::well_known::Rfc3339)
                    .map(|t| UNIX_EPOCH + Duration::from_secs(t.unix_timestamp().max(0) as u64))
                    .unwrap_or(UNIX_EPOCH);
                files.push((key[prefix.len()..].to_string(), size, mtime));
            }
            if dirs.len() + files.len() >= limit || doc.text_of("IsTruncated") != "true" {
                return Ok((dirs, files));
            }
            token = doc.text_of_raw("NextContinuationToken");
        }
    }

    /// Is `dir/` a directory (a marker or any key below it)? The mount root always is.
    fn is_dir(&self, path: &str) -> Result<bool, Errno> {
        if path.is_empty() {
            return Ok(true);
        }
        let dir = format!("{path}/");
        if self.head(&dir)?.is_some() {
            return Ok(true);
        }
        let (d, f) = self.list(&dir, 1)?;
        Ok(!d.is_empty() || !f.is_empty())
    }

    fn put_file(&self, path: &str, file: std::fs::File, len: u64) -> Result<(), Errno> {
        let body = self.rt.block_on(async { crate::s3::tiering::file_body(file) });
        self.ok(self.call("PUT", &self.key(path), "", &[], body, Some(len))?).map(|_| ())
    }

    fn put_empty(&self, key_path: &str) -> Result<(), Errno> {
        self.ok(self.call("PUT", &self.key(key_path), "", &[], Self::empty(), Some(0))?).map(|_| ())
    }

    fn delete(&self, key_path: &str) -> Result<(), Errno> {
        self.ok(self.call("DELETE", &self.key(key_path), "", &[], Self::empty(), None)?).map(|_| ())
    }

    fn copy(&self, from: &str, to: &str) -> Result<(), Errno> {
        let src = format!("/{}/{}", aws_encode(self.bucket.as_bytes(), false), aws_encode(self.key(from).as_bytes(), true));
        self.ok(self.call("PUT", &self.key(to), "", &[("x-amz-copy-source", &src)], Self::empty(), Some(0))?).map(|_| ())
    }

    fn get(&self, path: &str, off: u64, len: u64) -> Result<Bytes, Errno> {
        let range = format!("bytes={off}-{}", off + len - 1);
        let r = self.call("GET", &self.key(path), "", &[("range", &range)], Self::empty(), None)?;
        if r.status == 416 {
            return Ok(Bytes::new());
        }
        Ok(self.ok(r)?.body)
    }
}

/// A file opened for writing: staged, then stored on flush/close.
struct Staged {
    path: PathBuf,
    file: std::fs::File,
    written: u64,
    dirty: bool,
}

impl Drop for Staged {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

enum Open {
    Read,
    Write(Staged),
    Dir(Vec<(String, FileType)>),
}

#[derive(Default)]
struct State {
    paths: HashMap<u64, String>,
    inodes: HashMap<String, u64>,
    next_ino: u64,
    handles: HashMap<u64, (String, Open)>,
    next_fh: u64,
}

struct CairnFs {
    s3: S3,
    state: Mutex<State>,
    uid: u32,
    gid: u32,
    read_only: bool,
    staging: PathBuf,
}

fn join(dir: &str, name: &OsStr) -> Result<String, Errno> {
    let n = name.to_str().ok_or(Errno::EINVAL)?;
    if n.contains('/') || n.is_empty() {
        return Err(Errno::EINVAL);
    }
    Ok(if dir.is_empty() { n.to_string() } else { format!("{dir}/{n}") })
}

impl CairnFs {
    fn ino(&self, path: &str) -> INodeNo {
        let mut st = self.state.lock().unwrap();
        if path.is_empty() {
            return INodeNo::ROOT;
        }
        if let Some(i) = st.inodes.get(path) {
            return INodeNo(*i);
        }
        st.next_ino += 1;
        let i = st.next_ino + 1;
        st.paths.insert(i, path.to_string());
        st.inodes.insert(path.to_string(), i);
        INodeNo(i)
    }

    fn path(&self, ino: INodeNo) -> Result<String, Errno> {
        if ino == INodeNo::ROOT {
            return Ok(String::new());
        }
        self.state.lock().unwrap().paths.get(&ino.0).cloned().ok_or(Errno::ENOENT)
    }

    fn attr(&self, ino: INodeNo, kind: FileType, size: u64, mtime: SystemTime) -> FileAttr {
        FileAttr {
            ino,
            size,
            blocks: size.div_ceil(512),
            atime: mtime,
            mtime,
            ctime: mtime,
            crtime: mtime,
            kind,
            perm: if kind == FileType::Directory { 0o755 } else { 0o644 },
            nlink: if kind == FileType::Directory { 2 } else { 1 },
            uid: self.uid,
            gid: self.gid,
            rdev: 0,
            flags: 0,
            blksize: 4096,
        }
    }

    /// The attributes of a path, preferring an open write handle's size.
    fn stat(&self, path: &str) -> Result<FileAttr, Errno> {
        let ino = self.ino(path);
        if path.is_empty() {
            return Ok(self.attr(ino, FileType::Directory, 0, UNIX_EPOCH));
        }
        let staged = {
            let st = self.state.lock().unwrap();
            st.handles.values().find_map(|(p, o)| match o {
                Open::Write(s) if p == path => Some(s.written),
                _ => None,
            })
        };
        if let Some(n) = staged {
            return Ok(self.attr(ino, FileType::RegularFile, n, SystemTime::now()));
        }
        if let Some((size, mtime)) = self.s3.head(path)? {
            return Ok(self.attr(ino, FileType::RegularFile, size, mtime));
        }
        if self.s3.is_dir(path)? {
            return Ok(self.attr(ino, FileType::Directory, 0, UNIX_EPOCH));
        }
        Err(Errno::ENOENT)
    }

    fn stage(&self) -> Result<Staged, Errno> {
        let path = self.staging.join(format!("cairn-mount-{}", crate::engine::new_id()));
        let file = std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&path).map_err(|_| Errno::EIO)?;
        Ok(Staged { path, file, written: 0, dirty: true })
    }

    fn new_handle(&self, path: String, open: Open) -> FileHandle {
        let mut st = self.state.lock().unwrap();
        st.next_fh += 1;
        let fh = st.next_fh;
        st.handles.insert(fh, (path, open));
        FileHandle(fh)
    }

    /// Stores a dirty staged file.
    fn store(&self, fh: FileHandle) -> Result<(), Errno> {
        let job = {
            let mut st = self.state.lock().unwrap();
            match st.handles.get_mut(&fh.0) {
                Some((path, Open::Write(s))) if s.dirty => {
                    s.file.flush().map_err(|_| Errno::EIO)?;
                    let mut f = s.file.try_clone().map_err(|_| Errno::EIO)?;
                    f.rewind().map_err(|_| Errno::EIO)?;
                    s.dirty = false;
                    Some((path.clone(), f, s.written))
                }
                _ => None,
            }
        };
        match job {
            Some((path, f, len)) => self.s3.put_file(&path, f, len).inspect_err(|_| {
                if let Some((_, Open::Write(s))) = self.state.lock().unwrap().handles.get_mut(&fh.0) {
                    s.dirty = true; // try again on the next flush or close
                }
            }),
            None => Ok(()),
        }
    }
}

impl Filesystem for CairnFs {
    fn init(&mut self, _: &Request, config: &mut KernelConfig) -> std::io::Result<()> {
        let _ = config.add_capabilities(InitFlags::FUSE_ATOMIC_O_TRUNC);
        Ok(())
    }

    fn lookup(&self, _: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEntry) {
        match self.path(parent).and_then(|d| join(&d, name)).and_then(|p| self.stat(&p)) {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(e),
        }
    }

    fn getattr(&self, _: &Request, ino: INodeNo, _: Option<FileHandle>, reply: ReplyAttr) {
        match self.path(ino).and_then(|p| self.stat(&p)) {
            Ok(a) => reply.attr(&TTL, &a),
            Err(e) => reply.error(e),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn setattr(
        &self,
        _: &Request,
        ino: INodeNo,
        mode: Option<u32>,
        uid: Option<u32>,
        gid: Option<u32>,
        size: Option<u64>,
        atime: Option<TimeOrNow>,
        mtime: Option<TimeOrNow>,
        _: Option<SystemTime>,
        fh: Option<FileHandle>,
        _: Option<SystemTime>,
        _: Option<SystemTime>,
        _: Option<SystemTime>,
        _: Option<fuser::BsdFileFlags>,
        reply: ReplyAttr,
    ) {
        // Objects have no modes, owners or settable times; only truncation to zero is meaningful. Setting
        // the mode a file already shows (cp and shutil.copy do) changes nothing, so it is allowed.
        let shown = if self.path(ino).is_ok_and(|p| p.is_empty() || self.s3.is_dir(&p).unwrap_or(false)) { 0o755 } else { 0o644 };
        let mode = mode.filter(|m| m & 0o7777 != shown);
        if mode.is_some() || uid.is_some() || gid.is_some() || atime.is_some() || mtime.is_some() || size.is_some_and(|n| n != 0) {
            return reply.error(Errno::EOPNOTSUPP);
        }
        let path = match self.path(ino) {
            Ok(p) => p,
            Err(e) => return reply.error(e),
        };
        if size == Some(0) {
            if self.read_only {
                return reply.error(Errno::EROFS);
            }
            let staged = fh.and_then(|fh| {
                let mut st = self.state.lock().unwrap();
                match st.handles.get_mut(&fh.0) {
                    Some((_, Open::Write(s))) => {
                        let ok = s.file.set_len(0).is_ok() && s.file.rewind().is_ok();
                        s.written = 0;
                        s.dirty = true;
                        Some(ok)
                    }
                    _ => None,
                }
            });
            match staged {
                Some(true) => {}
                Some(false) => return reply.error(Errno::EIO),
                None => {
                    if let Err(e) = self.s3.put_empty(&path) {
                        return reply.error(e);
                    }
                }
            }
        }
        match self.stat(&path) {
            Ok(a) => reply.attr(&TTL, &a),
            Err(e) => reply.error(e),
        }
    }

    fn opendir(&self, _: &Request, ino: INodeNo, _: OpenFlags, reply: ReplyOpen) {
        let path = match self.path(ino) {
            Ok(p) => p,
            Err(e) => return reply.error(e),
        };
        let dir = if path.is_empty() { String::new() } else { format!("{path}/") };
        match self.s3.list(&dir, usize::MAX) {
            Ok((dirs, files)) => {
                let mut entries = vec![(".".to_string(), FileType::Directory), ("..".to_string(), FileType::Directory)];
                entries.extend(dirs.into_iter().map(|d| (d, FileType::Directory)));
                entries.extend(files.into_iter().map(|(f, _, _)| (f, FileType::RegularFile)));
                let fh = self.new_handle(path, Open::Dir(entries));
                reply.opened(fh, FopenFlags::empty());
            }
            Err(e) => reply.error(e),
        }
    }

    fn readdir(&self, _: &Request, ino: INodeNo, fh: FileHandle, offset: u64, mut reply: ReplyDirectory) {
        let (path, entries) = {
            let st = self.state.lock().unwrap();
            match st.handles.get(&fh.0) {
                Some((p, Open::Dir(e))) => (p.clone(), e.clone()),
                _ => return reply.error(Errno::EBADF),
            }
        };
        for (i, (name, kind)) in entries.iter().enumerate().skip(offset as usize) {
            let child = match name.as_str() {
                "." => ino,
                ".." => INodeNo::ROOT,
                n => self.ino(&if path.is_empty() { n.to_string() } else { format!("{path}/{n}") }),
            };
            if reply.add(child, (i + 1) as u64, *kind, name) {
                break;
            }
        }
        reply.ok();
    }

    fn releasedir(&self, _: &Request, _: INodeNo, fh: FileHandle, _: OpenFlags, reply: ReplyEmpty) {
        self.state.lock().unwrap().handles.remove(&fh.0);
        reply.ok();
    }

    fn open(&self, _: &Request, ino: INodeNo, flags: OpenFlags, reply: ReplyOpen) {
        let path = match self.path(ino) {
            Ok(p) => p,
            Err(e) => return reply.error(e),
        };
        let write = flags.acc_mode() != OpenAccMode::O_RDONLY;
        if !write {
            let fh = self.new_handle(path, Open::Read);
            return reply.opened(fh, FopenFlags::empty());
        }
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        if flags.0 & libc_o_append() != 0 || flags.acc_mode() == OpenAccMode::O_RDWR {
            return reply.error(Errno::EOPNOTSUPP); // objects are written whole
        }
        let truncate = flags.0 & libc_o_trunc() != 0;
        if !truncate {
            match self.s3.head(&path) {
                Ok(Some((n, _))) if n > 0 => return reply.error(Errno::EOPNOTSUPP), // would rewrite part of it
                Ok(_) => {}
                Err(e) => return reply.error(e),
            }
        }
        match self.stage() {
            Ok(s) => {
                let fh = self.new_handle(path, Open::Write(s));
                reply.opened(fh, FopenFlags::FOPEN_DIRECT_IO)
            }
            Err(e) => reply.error(e),
        }
    }

    fn create(&self, _: &Request, parent: INodeNo, name: &OsStr, _: u32, _: u32, flags: i32, reply: ReplyCreate) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        if flags & libc_o_append() != 0 {
            return reply.error(Errno::EOPNOTSUPP);
        }
        let path = match self.path(parent).and_then(|d| join(&d, name)) {
            Ok(p) => p,
            Err(e) => return reply.error(e),
        };
        match self.stage() {
            Ok(s) => {
                let ino = self.ino(&path);
                let fh = self.new_handle(path, Open::Write(s));
                // Store the (empty) file now, so it exists for others as soon as it is created.
                if let Err(e) = self.store(fh) {
                    self.state.lock().unwrap().handles.remove(&fh.0);
                    return reply.error(e);
                }
                let attr = self.attr(ino, FileType::RegularFile, 0, SystemTime::now());
                reply.created(&TTL, &attr, Generation(0), fh, FopenFlags::FOPEN_DIRECT_IO);
            }
            Err(e) => reply.error(e),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn read(&self, _: &Request, _: INodeNo, fh: FileHandle, offset: u64, size: u32, _: OpenFlags, _: Option<LockOwner>, reply: ReplyData) {
        let path = match self.state.lock().unwrap().handles.get(&fh.0) {
            Some((p, Open::Read)) => p.clone(),
            _ => return reply.error(Errno::EBADF),
        };
        if size == 0 {
            return reply.data(&[]);
        }
        match self.s3.get(&path, offset, size as u64) {
            Ok(b) => reply.data(&b),
            Err(e) => reply.error(e),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn write(
        &self,
        _: &Request,
        _: INodeNo,
        fh: FileHandle,
        offset: u64,
        data: &[u8],
        _: WriteFlags,
        _: OpenFlags,
        _: Option<LockOwner>,
        reply: ReplyWrite,
    ) {
        let mut st = self.state.lock().unwrap();
        match st.handles.get_mut(&fh.0) {
            Some((_, Open::Write(s))) => {
                if offset != s.written {
                    return reply.error(Errno::EOPNOTSUPP); // only sequential writes
                }
                if s.file.write_all(data).is_err() {
                    return reply.error(Errno::EIO);
                }
                s.written += data.len() as u64;
                s.dirty = true;
                reply.written(data.len() as u32);
            }
            _ => reply.error(Errno::EBADF),
        }
    }

    fn flush(&self, _: &Request, _: INodeNo, fh: FileHandle, _: LockOwner, reply: ReplyEmpty) {
        match self.store(fh) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e),
        }
    }

    fn fsync(&self, _: &Request, _: INodeNo, fh: FileHandle, _: bool, reply: ReplyEmpty) {
        match self.store(fh) {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e),
        }
    }

    fn release(&self, _: &Request, _: INodeNo, fh: FileHandle, _: OpenFlags, _: Option<LockOwner>, _: bool, reply: ReplyEmpty) {
        let res = self.store(fh);
        self.state.lock().unwrap().handles.remove(&fh.0);
        match res {
            Ok(()) => reply.ok(),
            Err(e) => {
                tracing::error!(error = ?e, "a file could not be stored when it was closed");
                reply.error(e)
            }
        }
    }

    fn unlink(&self, _: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEmpty) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let res = self.path(parent).and_then(|d| join(&d, name)).and_then(|p| match self.s3.head(&p)? {
            Some(_) => self.s3.delete(&p),
            None => Err(Errno::ENOENT),
        });
        match res {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e),
        }
    }

    fn mkdir(&self, _: &Request, parent: INodeNo, name: &OsStr, _: u32, _: u32, reply: ReplyEntry) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let res = self.path(parent).and_then(|d| join(&d, name)).and_then(|p| {
            if self.s3.head(&p)?.is_some() || self.s3.is_dir(&p)? {
                return Err(Errno::EEXIST);
            }
            self.s3.put_empty(&format!("{p}/"))?;
            Ok(self.attr(self.ino(&p), FileType::Directory, 0, SystemTime::now()))
        });
        match res {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(e),
        }
    }

    fn rmdir(&self, _: &Request, parent: INodeNo, name: &OsStr, reply: ReplyEmpty) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let res = self.path(parent).and_then(|d| join(&d, name)).and_then(|p| {
            let dir = format!("{p}/");
            let (d, f) = self.s3.list(&dir, 1)?;
            if !d.is_empty() || !f.is_empty() {
                return Err(Errno::ENOTEMPTY);
            }
            if self.s3.head(&dir)?.is_none() {
                return Err(Errno::ENOENT);
            }
            self.s3.delete(&dir)
        });
        match res {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn rename(
        &self,
        _: &Request,
        parent: INodeNo,
        name: &OsStr,
        newparent: INodeNo,
        newname: &OsStr,
        flags: RenameFlags,
        reply: ReplyEmpty,
    ) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let res = (|| {
            let from = join(&self.path(parent)?, name)?;
            let to = join(&self.path(newparent)?, newname)?;
            if self.s3.head(&from)?.is_none() {
                // A directory: renaming means copying every key; let mv do that (it falls back on EXDEV).
                return Err(if self.s3.is_dir(&from)? { Errno::EXDEV } else { Errno::ENOENT });
            }
            if flags.contains(RenameFlags::RENAME_EXCHANGE) {
                return Err(Errno::EOPNOTSUPP);
            }
            if flags.contains(RenameFlags::RENAME_NOREPLACE) && self.s3.head(&to)?.is_some() {
                return Err(Errno::EEXIST);
            }
            self.s3.copy(&from, &to)?;
            self.s3.delete(&from)?;
            let mut st = self.state.lock().unwrap();
            if let Some(i) = st.inodes.remove(&from) {
                st.paths.insert(i, to.clone());
                st.inodes.insert(to, i);
            }
            Ok(())
        })();
        match res {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e),
        }
    }

    /// Access is decided by the store, per request; the kernel's view of the mount is permissive.
    fn access(&self, _: &Request, _: INodeNo, _: fuser::AccessFlags, reply: ReplyEmpty) {
        reply.ok();
    }

    /// Objects carry no extended attributes.
    fn getxattr(&self, _: &Request, _: INodeNo, _: &OsStr, _: u32, reply: fuser::ReplyXattr) {
        reply.error(Errno::NO_XATTR);
    }

    fn listxattr(&self, _: &Request, _: INodeNo, size: u32, reply: fuser::ReplyXattr) {
        if size == 0 {
            reply.size(0)
        } else {
            reply.data(&[])
        }
    }

    fn statfs(&self, _: &Request, _: INodeNo, reply: ReplyStatfs) {
        let big = u64::MAX / 8192;
        reply.statfs(big, big, big, u64::MAX / 2, u64::MAX / 2, 4096, 1024, 4096);
    }
}

#[cfg(target_os = "linux")]
fn libc_o_append() -> i32 {
    0o2000
}
#[cfg(target_os = "linux")]
fn libc_o_trunc() -> i32 {
    0o1000
}

fn usage() -> ! {
    eprintln!("usage: cairn-store mount <bucket>[/prefix] <directory> [--read-only] [--allow-other]");
    std::process::exit(2)
}

/// Runs the mount until it is unmounted (`fusermount -u <dir>` or `umount <dir>`). Returns an exit code.
pub fn run(args: &[String], endpoint: &str, ak: &str, sk: &str, region: &str) -> i32 {
    let positional: Vec<&String> = args.iter().filter(|a| !a.starts_with("--")).collect();
    let (target, dir) = match positional.as_slice() {
        [t, d] => (t.as_str(), d.as_str()),
        _ => usage(),
    };
    let read_only = args.iter().any(|a| a == "--read-only");
    let allow_other = args.iter().any(|a| a == "--allow-other");
    let (bucket, prefix) = match target.split_once('/') {
        Some((b, p)) if !p.trim_matches('/').is_empty() => (b.to_string(), format!("{}/", p.trim_matches('/'))),
        Some((b, _)) => (b.to_string(), String::new()),
        None => (target.to_string(), String::new()),
    };
    let rt = match tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("cairn-store mount: {e}");
            return 1;
        }
    };
    let conn = match crate::tls::https_connector() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("cairn-store mount: {e}");
            return 1;
        }
    };
    let http = {
        let _g = rt.enter();
        Client::builder(TokioExecutor::new()).build(conn)
    };
    let s3 = S3 {
        endpoint: endpoint.trim_end_matches('/').to_string(),
        bucket: bucket.clone(),
        prefix,
        ak: ak.into(),
        sk: sk.into(),
        region: region.into(),
        http,
        rt,
    };
    // Refuse to mount a bucket the key cannot list (fail at mount time, not on first use).
    if let Err(e) = s3.list("", 1) {
        eprintln!("cairn-store mount: cannot list {target} at {endpoint}: {e:?}");
        return 1;
    }
    let meta = match std::fs::metadata(dir) {
        Ok(m) if m.is_dir() => m,
        _ => {
            eprintln!("cairn-store mount: {dir} is not a directory");
            return 1;
        }
    };
    use std::os::unix::fs::MetadataExt as _;
    let staging = std::env::var("CAIRN_STORE_MOUNT_STAGING").map(PathBuf::from).unwrap_or_else(|_| std::env::temp_dir());
    let fs = CairnFs { s3, state: Mutex::new(State::default()), uid: meta.uid(), gid: meta.gid(), read_only, staging };
    let mut cfg = fuser::Config::default();
    cfg.mount_options =
        vec![MountOption::FSName(format!("cairn:{target}")), MountOption::Subtype("cairn".into()), MountOption::NoDev, MountOption::NoSuid];
    if read_only {
        cfg.mount_options.push(MountOption::RO);
    }
    if allow_other {
        cfg.acl = fuser::SessionACL::All;
    }
    tracing::info!(bucket = %bucket, dir, read_only, "mounted");
    match fuser::mount(fs, dir, &cfg) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("cairn-store mount: {e}");
            1
        }
    }
}
