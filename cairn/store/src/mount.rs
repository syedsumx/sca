//! `cairn-store mount <bucket>[/prefix] <directory>`: a bucket as a local directory (FUSE), for tools
//! that only read and write files.
//!
//! The mount is an S3 client of the store (`CAIRN_STORE_ENDPOINT`, signed with `CAIRN_STORE_ACCESS_KEY`
//! and `CAIRN_STORE_SECRET_KEY`), so the key's policies, Cairn's policy decision point, quotas,
//! encryption, events and the audit trail apply as for any client. Reads are ranged GETs.
//!
//! Writes go to a private, sparse staging file in 8 MiB chunks and are stored when the file is flushed
//! or closed, so readers never see half a file. Appends, writes at any offset, holes and truncation
//! work: a chunk is fetched only when a write covers part of it, and a large file is stored as a
//! multipart upload whose unchanged chunks are copied inside the store (UploadPartCopy), so editing a
//! few bytes of a large file sends a chunk, not the file. Every store is conditional on the version the
//! edit started from: if someone else changed the file meanwhile, the close fails with `ESTALE` and
//! their version stays. `O_TRUNC` opens (`>`, `cp`) replace a file.
//!
//! POSIX metadata lives in the object's user metadata, in the headers s3fs-fuse uses: `x-amz-meta-mode`
//! (type and permission bits, decimal), `-uid`, `-gid`, `-mtime` (seconds) and `-xattr` (extended
//! attributes, percent-encoded JSON of base64 values). chmod, chown, utimes and setxattr rewrite them
//! with a copy of the object onto itself, conditional on the version they read, so a concurrent writer
//! is never overwritten. A symbolic link is an object whose content is the target and whose mode says
//! it is a link. Objects without these headers show as 0644 files and 0755 directories owned by the
//! mount's user. Hard links fail with `EPERM`: two names cannot share one object. With
//! `--enforce-modes` the kernel checks modes and owners before each access; the store's own policies
//! apply either way.
//!
//! Renames (directories too) are one transaction in an unversioned bucket. Directories are key
//! prefixes; `mkdir` writes an empty `name/` marker, as S3 consoles do, and a directory's metadata is
//! kept on its marker.

use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::io::{Read, Seek, SeekFrom, Write};
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
/// Staging and multipart unit (at least S3's 5 MiB part minimum).
const CHUNK: u64 = 8 << 20;
/// Files up to this size are stored with one PUT; larger ones as multipart uploads that copy clean chunks.
const WHOLE_PUT: u64 = 16 << 20;

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
        412 => Errno::ESTALE,
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
        self.send(method, &url, extra, body, len)
    }

    fn send(&self, method: &str, url: &str, extra: &[(&str, &str)], body: Body, len: Option<u64>) -> Result<Resp, Errno> {
        let url = url.to_string();
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

    /// A file's size, time, version and metadata, or None.
    fn info(&self, path: &str) -> Result<Option<Info>, Errno> {
        let r = self.call("HEAD", &self.key(path), "", &[], Self::empty(), None)?;
        if r.status == 404 {
            return Ok(None);
        }
        let r = self.ok(r)?;
        let size = r.headers.get("content-length").and_then(|v| v.to_str().ok()?.parse().ok()).unwrap_or(0);
        let meta = Meta::from_headers(&r.headers);
        let mtime = match meta.mtime {
            Some(t) => UNIX_EPOCH + Duration::from_secs(t.max(0) as u64),
            None => r
                .headers
                .get("last-modified")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| time::OffsetDateTime::parse(v, &time::format_description::well_known::Rfc2822).ok())
                .map(|t| UNIX_EPOCH + Duration::from_secs(t.unix_timestamp().max(0) as u64))
                .unwrap_or(UNIX_EPOCH),
        };
        let kept =
            KEPT.iter().filter_map(|k| r.headers.get(*k).and_then(|v| v.to_str().ok()).map(|v| (k.to_string(), v.to_string()))).collect();
        Ok(Some(Info { size, mtime, etag: etag_of(&r.headers), meta, kept }))
    }

    /// (size, modified) of a file, or None.
    fn head(&self, path: &str) -> Result<Option<(u64, SystemTime)>, Errno> {
        Ok(self.info(path)?.map(|i| (i.size, i.mtime)))
    }

    /// Replaces a file's metadata, keeping its content, over the version `etag` (ESTALE: it changed).
    /// Returns the new ETag.
    fn set_meta(&self, path: &str, info: &Info, meta: &Meta) -> Result<String, Errno> {
        let src = format!("/{}/{}", aws_encode(self.bucket.as_bytes(), false), aws_encode(self.key(path).as_bytes(), true));
        let cond = format!("\"{}\"", info.etag);
        let mh = meta.headers();
        let mut h: Vec<(&str, &str)> = vec![
            ("x-amz-copy-source", &src),
            ("x-amz-metadata-directive", "REPLACE"),
            ("x-amz-copy-source-if-match", &cond),
            ("if-match", &cond),
        ];
        h.extend(info.kept.iter().map(|(k, v)| (k.as_str(), v.as_str())));
        h.extend(mh.iter().map(|(k, v)| (k.as_str(), v.as_str())));
        let r = self.call("PUT", &self.key(path), "", &h, Self::empty(), Some(0))?;
        if r.status == 400 && String::from_utf8_lossy(&r.body).contains("MetadataTooLarge") {
            return Err(Errno::E2BIG);
        }
        let r = self.ok(r)?;
        let doc = String::from_utf8_lossy(&r.body).into_owned();
        Ok(xml::parse(&doc).map(|x| x.text_of("ETag")).unwrap_or_default().trim_matches('"').to_string())
    }

    /// Writes a small object (a symbolic link, a directory marker) with metadata.
    fn put_small(&self, path: &str, data: &[u8], meta: &Meta, if_none: bool) -> Result<(), Errno> {
        let mh = meta.headers();
        let mut h: Vec<(&str, &str)> = mh.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        if if_none {
            h.push(("if-none-match", "*"));
        }
        let body = Full::new(Bytes::copy_from_slice(data)).map_err(|never| match never {}).boxed();
        let r = self.call("PUT", &self.key(path), "", &h, body, Some(data.len() as u64))?;
        if r.status == 412 && if_none {
            return Err(Errno::EEXIST);
        }
        self.ok(r).map(|_| ())
    }

    /// Reads `[off, off+len)` of the version with ETag `etag` (ESTALE: it changed).
    fn get_of(&self, path: &str, etag: &str, off: u64, len: u64) -> Result<Bytes, Errno> {
        let range = format!("bytes={off}-{}", off + len - 1);
        let quoted = format!("\"{etag}\"");
        let r = self.call("GET", &self.key(path), "", &[("range", &range), ("if-match", &quoted)], Self::empty(), None)?;
        Ok(self.ok(r)?.body)
    }

    /// Stores `len` bytes from `file` (positioned at their start); `if_match`: only over that version.
    /// Returns the new ETag.
    fn put_from(
        &self,
        path: &str,
        file: std::fs::File,
        len: u64,
        if_match: Option<&str>,
        meta: &[(String, String)],
    ) -> Result<String, Errno> {
        let body = self.rt.block_on(async { crate::s3::tiering::file_body(file.take(len)) });
        let q = if_match.map(|e| format!("\"{e}\""));
        let mut h: Vec<(&str, &str)> = q.as_deref().map(|v| vec![("if-match", v)]).unwrap_or_default();
        h.extend(meta.iter().map(|(k, v)| (k.as_str(), v.as_str())));
        let r = self.ok(self.call("PUT", &self.key(path), "", &h, body, Some(len))?)?;
        Ok(etag_of(&r.headers))
    }

    /// Stores a large file as a multipart upload: `parts` are (offset, length, local?) where local parts
    /// are sent from `file` and the others copied from the version `base` inside the store.
    fn put_multipart(
        &self,
        path: &str,
        file: &std::fs::File,
        parts: &[(u64, u64, bool)],
        base: Option<&str>,
        meta: &[(String, String)],
    ) -> Result<String, Errno> {
        let key = self.key(path);
        let mh: Vec<(&str, &str)> = meta.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        let r = self.ok(self.call("POST", &key, "uploads", &mh, Self::empty(), Some(0))?)?;
        let doc = String::from_utf8_lossy(&r.body).into_owned();
        let upload = xml::parse(&doc).map(|x| x.text_of("UploadId")).filter(|u| !u.is_empty()).ok_or(Errno::EIO)?;
        let uq = aws_encode(upload.as_bytes(), false);
        let res = (|| -> Result<String, Errno> {
            let mut etags = vec![];
            for (i, (off, len, local)) in parts.iter().enumerate() {
                let q = format!("partNumber={}&uploadId={uq}", i + 1);
                let etag = if *local {
                    let mut f = file.try_clone().map_err(|_| Errno::EIO)?;
                    f.seek(SeekFrom::Start(*off)).map_err(|_| Errno::EIO)?;
                    let body = self.rt.block_on(async { crate::s3::tiering::file_body(f.take(*len)) });
                    etag_of(&self.ok(self.call("PUT", &key, &q, &[], body, Some(*len))?)?.headers)
                } else {
                    let src = format!("/{}/{}", aws_encode(self.bucket.as_bytes(), false), aws_encode(key.as_bytes(), true));
                    let range = format!("bytes={off}-{}", off + len - 1);
                    let cond = format!("\"{}\"", base.unwrap_or(""));
                    let h = [
                        ("x-amz-copy-source", src.as_str()),
                        ("x-amz-copy-source-range", range.as_str()),
                        ("x-amz-copy-source-if-match", cond.as_str()),
                    ];
                    let r = self.ok(self.call("PUT", &key, &q, &h, Self::empty(), Some(0))?)?;
                    let doc = String::from_utf8_lossy(&r.body).into_owned();
                    xml::parse(&doc).map(|x| x.text_of("ETag")).ok_or(Errno::EIO)?.trim_matches('"').to_string()
                };
                etags.push(etag);
            }
            let mut body = String::from("<CompleteMultipartUpload>");
            for (i, e) in etags.iter().enumerate() {
                body.push_str(&format!("<Part><PartNumber>{}</PartNumber><ETag>\"{e}\"</ETag></Part>", i + 1));
            }
            body.push_str("</CompleteMultipartUpload>");
            let n = body.len() as u64;
            let q = base.map(|e| format!("\"{e}\""));
            let mut h: Vec<(&str, &str)> = vec![("content-type", "application/xml")];
            if let Some(v) = q.as_deref() {
                h.push(("if-match", v));
            }
            let body = Full::new(Bytes::from(body)).map_err(|never| match never {}).boxed();
            let r = self.ok(self.call("POST", &key, &format!("uploadId={uq}"), &h, body, Some(n))?)?;
            let doc = String::from_utf8_lossy(&r.body).into_owned();
            Ok(xml::parse(&doc).map(|x| x.text_of("ETag")).unwrap_or_default().trim_matches('"').to_string())
        })();
        if res.is_err() {
            let _ = self.call("DELETE", &key, &format!("uploadId={uq}"), &[], Self::empty(), None);
        }
        res
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

    fn delete(&self, key_path: &str) -> Result<(), Errno> {
        self.ok(self.call("DELETE", &self.key(key_path), "", &[], Self::empty(), None)?).map(|_| ())
    }

    /// Moves a file or a directory in one step (WebDAV MOVE: one transaction within an unversioned
    /// bucket). Err(EXDEV): the store cannot do it in place (a versioned bucket).
    fn rename(&self, from: &str, to: &str, dir: bool, replace: bool) -> Result<(), Errno> {
        let dav = |p: &str| {
            let k = self.key(p);
            format!("/-/dav/{}/{}{}", aws_encode(self.bucket.as_bytes(), false), aws_encode(k.as_bytes(), true), if dir { "/" } else { "" })
        };
        let url = format!("{}{}", self.endpoint, dav(from));
        let dest = dav(to);
        let r =
            self.send("MOVE", &url, &[("destination", &dest), ("overwrite", if replace { "T" } else { "F" })], Self::empty(), Some(0))?;
        match r.status {
            201 | 204 => Ok(()),
            409 if dir => Err(Errno::EXDEV),
            s => Err(errno_of(s)),
        }
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

fn etag_of(h: &hyper::HeaderMap) -> String {
    h.get("etag").and_then(|v| v.to_str().ok()).unwrap_or("").trim_matches('"').to_string()
}

const S_IFMT: u32 = 0o170000;
const S_IFREG: u32 = 0o100000;
const S_IFDIR: u32 = 0o040000;
const S_IFLNK: u32 = 0o120000;
/// Headers an object keeps besides its user metadata; a metadata update sends them again.
const KEPT: [&str; 6] = ["content-type", "cache-control", "content-disposition", "content-encoding", "content-language", "expires"];

/// A file's POSIX metadata, as kept in its user metadata (s3fs-fuse's headers).
#[derive(Clone, Debug, Default, PartialEq)]
struct Meta {
    mode: Option<u32>,
    uid: Option<u32>,
    gid: Option<u32>,
    mtime: Option<i64>,
    xattrs: std::collections::BTreeMap<String, Vec<u8>>,
}

impl Meta {
    fn from_headers(h: &hyper::HeaderMap) -> Meta {
        let num = |k: &str| h.get(k).and_then(|v| v.to_str().ok()).and_then(|v| v.trim().parse::<i64>().ok());
        let xattrs = h
            .get("x-amz-meta-xattr")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| percent_encoding::percent_decode_str(v).decode_utf8().ok().map(|s| s.into_owned()))
            .and_then(|v| serde_json::from_str::<std::collections::BTreeMap<String, String>>(&v).ok())
            .map(|m| {
                use base64::Engine as _;
                m.into_iter().filter_map(|(k, v)| Some((k, base64::engine::general_purpose::STANDARD.decode(v).ok()?))).collect()
            })
            .unwrap_or_default();
        Meta {
            mode: num("x-amz-meta-mode").and_then(|v| u32::try_from(v).ok()),
            uid: num("x-amz-meta-uid").and_then(|v| u32::try_from(v).ok()),
            gid: num("x-amz-meta-gid").and_then(|v| u32::try_from(v).ok()),
            mtime: num("x-amz-meta-mtime"),
            xattrs,
        }
    }

    /// The headers that store it (none for what is unset).
    fn headers(&self) -> Vec<(String, String)> {
        let mut out = vec![];
        if let Some(m) = self.mode {
            out.push(("x-amz-meta-mode".into(), m.to_string()));
        }
        if let Some(u) = self.uid {
            out.push(("x-amz-meta-uid".into(), u.to_string()));
        }
        if let Some(g) = self.gid {
            out.push(("x-amz-meta-gid".into(), g.to_string()));
        }
        if let Some(t) = self.mtime {
            out.push(("x-amz-meta-mtime".into(), t.to_string()));
        }
        if !self.xattrs.is_empty() {
            use base64::Engine as _;
            let m: std::collections::BTreeMap<&String, String> =
                self.xattrs.iter().map(|(k, v)| (k, base64::engine::general_purpose::STANDARD.encode(v))).collect();
            let json = serde_json::to_string(&m).unwrap_or_default();
            out.push((
                "x-amz-meta-xattr".into(),
                percent_encoding::utf8_percent_encode(&json, percent_encoding::NON_ALPHANUMERIC).to_string(),
            ));
        }
        out
    }

    fn is_symlink(&self) -> bool {
        self.mode.is_some_and(|m| m & S_IFMT == S_IFLNK)
    }
}

/// What a HEAD tells about a file.
struct Info {
    size: u64,
    mtime: SystemTime,
    etag: String,
    meta: Meta,
    /// The headers besides user metadata (content type and the like), resent when metadata changes.
    kept: Vec<(String, String)>,
}

/// A file opened for writing: a sparse staging file holding the chunks loaded or written, over the
/// version the edit started from; stored on flush/close.
struct Staged {
    path: PathBuf,
    file: std::fs::File,
    /// The file's size now.
    size: u64,
    /// The version this edit started from (ETag, size); None: a new or truncated file.
    base: Option<(String, u64)>,
    /// Chunks present in the staging file (written, or fetched from the base).
    loaded: HashSet<u64>,
    /// O_APPEND: every write goes to the end.
    append: bool,
    dirty: bool,
    /// The metadata stored with the file.
    meta: Meta,
}

impl Staged {
    fn base_size(&self) -> u64 {
        self.base.as_ref().map(|b| b.1).unwrap_or(0)
    }
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
    /// --enforce-modes: owners matter, so new files record their creator.
    enforce_modes: bool,
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
        self.attr_of(ino, kind, size, mtime, &Meta::default())
    }

    /// Attributes from an object's metadata; what it does not say comes from the mount's defaults.
    fn attr_of(&self, ino: INodeNo, kind: FileType, size: u64, mtime: SystemTime, meta: &Meta) -> FileAttr {
        let default = if kind == FileType::Directory { 0o755 } else { 0o644 };
        FileAttr {
            ino,
            size,
            blocks: size.div_ceil(512),
            atime: mtime,
            mtime,
            ctime: mtime,
            crtime: mtime,
            kind,
            perm: meta.mode.map(|m| (m & 0o7777) as u16).unwrap_or(default),
            nlink: if kind == FileType::Directory { 2 } else { 1 },
            uid: meta.uid.unwrap_or(self.uid),
            gid: meta.gid.unwrap_or(self.gid),
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
                Open::Write(s) if p == path => Some((s.size, s.meta.clone())),
                _ => None,
            })
        };
        if let Some((n, meta)) = staged {
            return Ok(self.attr_of(ino, FileType::RegularFile, n, SystemTime::now(), &meta));
        }
        if let Some(i) = self.s3.info(path)? {
            let kind = if i.meta.is_symlink() { FileType::Symlink } else { FileType::RegularFile };
            return Ok(self.attr_of(ino, kind, i.size, i.mtime, &i.meta));
        }
        if let Some(i) = self.s3.info(&format!("{path}/"))? {
            return Ok(self.attr_of(ino, FileType::Directory, 0, i.meta.mtime.map(|_| i.mtime).unwrap_or(UNIX_EPOCH), &i.meta));
        }
        if self.s3.is_dir(path)? {
            return Ok(self.attr(ino, FileType::Directory, 0, UNIX_EPOCH));
        }
        Err(Errno::ENOENT)
    }

    /// The object holding a path's metadata: the file itself, or a directory's marker (written when an
    /// implied directory gets metadata of its own).
    fn meta_target(&self, path: &str) -> Result<(String, Info), Errno> {
        if !path.is_empty() {
            if let Some(i) = self.s3.info(path)? {
                return Ok((path.to_string(), i));
            }
        }
        let marker = if path.is_empty() { String::new() } else { format!("{path}/") };
        if marker.is_empty() {
            return Err(Errno::EPERM); // the mount root is the bucket (or prefix) itself
        }
        if let Some(i) = self.s3.info(&marker)? {
            return Ok((marker, i));
        }
        if !self.s3.is_dir(path)? {
            return Err(Errno::ENOENT);
        }
        self.s3.put_small(&marker, b"", &Meta { mode: Some(S_IFDIR | 0o755), ..Default::default() }, true).or_else(|e| match e {
            Errno::EEXIST => Ok(()),
            e => Err(e),
        })?;
        Ok((marker.clone(), self.s3.info(&marker)?.ok_or(Errno::ENOENT)?))
    }

    /// A path's metadata now (a file open for writing: its pending metadata).
    fn current_meta(&self, path: &str) -> Result<Meta, Errno> {
        {
            let st = self.state.lock().unwrap();
            if let Some(m) = st.handles.values().find_map(|(p, o)| match o {
                Open::Write(s) if p == path => Some(s.meta.clone()),
                _ => None,
            }) {
                return Ok(m);
            }
        }
        if path.is_empty() {
            return Ok(Meta::default());
        }
        if let Some(i) = self.s3.info(path)? {
            return Ok(i.meta);
        }
        if let Some(i) = self.s3.info(&format!("{path}/"))? {
            return Ok(i.meta);
        }
        if self.s3.is_dir(path)? {
            return Ok(Meta::default());
        }
        Err(Errno::ENOENT)
    }

    /// Changes a path's metadata. A file open for writing keeps it with its pending content instead.
    fn update_meta(&self, path: &str, f: impl Fn(&mut Meta)) -> Result<(), Errno> {
        {
            let mut st = self.state.lock().unwrap();
            let mut found = false;
            for (p, o) in st.handles.values_mut() {
                if let Open::Write(s) = o {
                    if p == path {
                        f(&mut s.meta);
                        s.dirty = true;
                        found = true;
                    }
                }
            }
            if found {
                return Ok(());
            }
        }
        // Read, change and write back, over the version read: a writer in between makes it retry.
        for _ in 0..5 {
            let (target, info) = self.meta_target(path)?;
            let mut meta = info.meta.clone();
            if meta.mode.is_none() {
                meta.mode = Some(if target.ends_with('/') { S_IFDIR | 0o755 } else { S_IFREG | 0o644 });
            }
            f(&mut meta);
            if meta == info.meta {
                return Ok(());
            }
            match self.s3.set_meta(&target, &info, &meta) {
                Err(Errno::ESTALE) => continue,
                r => return r.map(|_| ()),
            }
        }
        Err(Errno::EAGAIN)
    }

    /// A staging file: empty (`base` None), or over an existing version.
    fn stage(&self, base: Option<(String, u64)>, append: bool, meta: Meta) -> Result<Staged, Errno> {
        let path = self.staging.join(format!("cairn-mount-{}", crate::engine::new_id()));
        let file = std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&path).map_err(|_| Errno::EIO)?;
        let size = base.as_ref().map(|b| b.1).unwrap_or(0);
        Ok(Staged { path, file, size, dirty: base.is_none(), base, loaded: HashSet::new(), append, meta })
    }

    /// Makes chunk `c` present in the staging file (fetching what the base has of it).
    fn load(&self, path: &str, s: &mut Staged, c: u64) -> Result<(), Errno> {
        if s.loaded.contains(&c) {
            return Ok(());
        }
        let start = c * CHUNK;
        if let Some((etag, bsize)) = &s.base {
            if start < *bsize {
                let len = CHUNK.min(bsize - start);
                let data = self.s3.get_of(path, etag, start, len)?;
                if data.len() as u64 != len {
                    return Err(Errno::EIO);
                }
                s.file.seek(SeekFrom::Start(start)).map_err(|_| Errno::EIO)?;
                s.file.write_all(&data).map_err(|_| Errno::EIO)?;
            }
        }
        s.loaded.insert(c);
        Ok(())
    }

    /// Reads from a file open for writing: staged chunks locally, the others from the base version.
    fn read_staged(&self, path: &str, s: &mut Staged, off: u64, len: u64) -> Result<Vec<u8>, Errno> {
        let end = (off + len).min(s.size);
        let mut out = Vec::with_capacity(end.saturating_sub(off) as usize);
        let mut pos = off;
        while pos < end {
            let c = pos / CHUNK;
            let stop = end.min((c + 1) * CHUNK);
            if s.loaded.contains(&c) {
                let mut buf = vec![0u8; (stop - pos) as usize];
                s.file.seek(SeekFrom::Start(pos)).map_err(|_| Errno::EIO)?;
                let mut got = 0;
                while got < buf.len() {
                    let n = s.file.read(&mut buf[got..]).map_err(|_| Errno::EIO)?;
                    if n == 0 {
                        break; // a hole at the end of the staging file: zeros
                    }
                    got += n;
                }
                out.extend_from_slice(&buf);
            } else if pos < s.base_size() {
                let (etag, bsize) = s.base.clone().unwrap();
                let upto = stop.min(bsize);
                out.extend_from_slice(&self.s3.get_of(path, &etag, pos, upto - pos)?);
                out.resize(out.len() + (stop - upto) as usize, 0);
            } else {
                out.resize(out.len() + (stop - pos) as usize, 0); // beyond the base: a hole
            }
            pos = stop;
        }
        Ok(out)
    }

    /// Writes into a staged file at `off`.
    fn write_staged(&self, path: &str, s: &mut Staged, off: u64, data: &[u8]) -> Result<(), Errno> {
        let end = off + data.len() as u64;
        if off > s.size && s.size % CHUNK != 0 {
            self.load(path, s, s.size / CHUNK)?; // the chunk holding the old end keeps its bytes
        }
        let mut c = off / CHUNK;
        while c * CHUNK < end.max(off + 1) {
            let (cs, ce) = (c * CHUNK, (c + 1) * CHUNK);
            let whole = off <= cs && end >= ce.min(s.size.max(end));
            if !whole {
                self.load(path, s, c)?;
            }
            s.loaded.insert(c);
            c += 1;
        }
        // Chunks between the old end and this write are holes: present, as zeros.
        for h in s.size.div_ceil(CHUNK)..off / CHUNK {
            s.loaded.insert(h);
        }
        s.file.seek(SeekFrom::Start(off)).map_err(|_| Errno::EIO)?;
        s.file.write_all(data).map_err(|_| Errno::EIO)?;
        s.size = s.size.max(end);
        s.dirty = true;
        Ok(())
    }

    /// Truncates (or extends with zeros) a staged file.
    fn truncate_staged(&self, path: &str, s: &mut Staged, n: u64) -> Result<(), Errno> {
        if n < s.size && n % CHUNK != 0 {
            self.load(path, s, n / CHUNK)?; // the kept part of the last chunk
        }
        for c in s.size.div_ceil(CHUNK)..n.div_ceil(CHUNK) {
            s.loaded.insert(c); // extended: zeros
        }
        if n > s.size && s.size % CHUNK != 0 {
            self.load(path, s, s.size / CHUNK)?;
        }
        s.file.set_len(n).map_err(|_| Errno::EIO)?;
        s.loaded.retain(|c| *c * CHUNK < n);
        s.size = n;
        s.dirty = true;
        Ok(())
    }

    /// The metadata of something new: its type and mode, and the time. With --enforce-modes the creator
    /// is recorded as its owner (unless that is the mount's own user, which objects show by default);
    /// otherwise new files show as the mount's user, as `--uid`/`--gid` ask.
    fn new_meta(&self, req: &Request, kind: u32, mode: u32) -> Meta {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
        Meta {
            mode: Some(kind | (mode & 0o7777)),
            uid: Some(req.uid()).filter(|u| self.enforce_modes && *u != self.uid),
            gid: Some(req.gid()).filter(|g| self.enforce_modes && *g != self.gid),
            mtime: Some(now),
            xattrs: Default::default(),
        }
    }

    fn new_handle(&self, path: String, open: Open) -> FileHandle {
        let mut st = self.state.lock().unwrap();
        st.next_fh += 1;
        let fh = st.next_fh;
        st.handles.insert(fh, (path, open));
        FileHandle(fh)
    }

    /// Stores a dirty staged file, over the version the edit started from (ESTALE: it changed).
    fn store(&self, fh: FileHandle) -> Result<(), Errno> {
        let mut st = self.state.lock().unwrap();
        let Some((path, Open::Write(s))) = st.handles.get_mut(&fh.0) else { return Ok(()) };
        if !s.dirty {
            return Ok(());
        }
        let path = path.clone();
        s.file.flush().map_err(|_| Errno::EIO)?;
        let base_etag = s.base.as_ref().map(|b| b.0.clone());
        let meta = s.meta.headers();
        let res = if s.size <= WHOLE_PUT || s.base.is_none() && s.loaded.len() as u64 * CHUNK >= s.size {
            // Small (or entirely local): every chunk present, then one PUT.
            (|| -> Result<String, Errno> {
                for c in 0..s.size.div_ceil(CHUNK) {
                    self.load(&path, s, c)?;
                }
                s.file.set_len(s.size).map_err(|_| Errno::EIO)?;
                let mut f = s.file.try_clone().map_err(|_| Errno::EIO)?;
                f.rewind().map_err(|_| Errno::EIO)?;
                self.s3.put_from(&path, f, s.size, base_etag.as_deref(), &meta)
            })()
        } else {
            s.file.set_len(s.size).map_err(|_| Errno::EIO)?;
            let bsize = s.base_size();
            let parts: Vec<(u64, u64, bool)> = (0..s.size.div_ceil(CHUNK))
                .map(|c| {
                    let (off, len) = (c * CHUNK, CHUNK.min(s.size - c * CHUNK));
                    (off, len, s.loaded.contains(&c) || off + len > bsize)
                })
                .collect();
            self.s3.put_multipart(&path, &s.file, &parts, base_etag.as_deref(), &meta)
        };
        let etag = res?;
        // The stored version is the base of what follows; the staged chunks stay valid.
        s.base = Some((etag, s.size));
        s.dirty = false;
        Ok(())
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
        let path = match self.path(ino) {
            Ok(p) => p,
            Err(e) => return reply.error(e),
        };
        // Modes, owners and the modification time are kept in the object's metadata (atime is not kept:
        // it is never stored on its own).
        let when = |t: TimeOrNow| match t {
            TimeOrNow::SpecificTime(t) => t.duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0),
            TimeOrNow::Now => SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0),
        };
        let new_mtime = mtime.map(when);
        if mode.is_some() || uid.is_some() || gid.is_some() || new_mtime.is_some() {
            if self.read_only {
                return reply.error(Errno::EROFS);
            }
            let _ = atime;
            let res = self.update_meta(&path, |m| {
                if let Some(mode) = mode {
                    m.mode = Some((m.mode.unwrap_or(S_IFREG) & S_IFMT) | (mode & 0o7777));
                }
                if let Some(u) = uid {
                    m.uid = Some(u);
                }
                if let Some(g) = gid {
                    m.gid = Some(g);
                }
                if let Some(t) = new_mtime {
                    m.mtime = Some(t);
                }
            });
            if let Err(e) = res {
                return reply.error(e);
            }
        }
        if let Some(n) = size {
            if self.read_only {
                return reply.error(Errno::EROFS);
            }
            let staged = fh.and_then(|fh| {
                let mut st = self.state.lock().unwrap();
                match st.handles.get_mut(&fh.0) {
                    Some((_, Open::Write(s))) => Some(self.truncate_staged(&path, s, n)),
                    _ => None,
                }
            });
            match staged {
                Some(Ok(())) => {}
                Some(Err(e)) => return reply.error(e),
                None if n == 0 => {
                    let meta = match self.s3.info(&path) {
                        Ok(i) => i.map(|i| i.meta).unwrap_or_default(),
                        Err(e) => return reply.error(e),
                    };
                    if let Err(e) = self.s3.put_small(&path, b"", &meta, false) {
                        return reply.error(e);
                    }
                }
                None => {
                    // truncate(2) without an open file: stage over the current version, store at once.
                    let res = (|| -> Result<(), Errno> {
                        let base = self.s3.info(&path)?.ok_or(Errno::ENOENT)?;
                        let mut s = self.stage(Some((base.etag, base.size)), false, base.meta)?;
                        self.truncate_staged(&path, &mut s, n)?;
                        let fh = self.new_handle(path.clone(), Open::Write(s));
                        let r = self.store(fh);
                        self.state.lock().unwrap().handles.remove(&fh.0);
                        r
                    })();
                    if let Err(e) = res {
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
        let append = flags.0 & libc_o_append() != 0;
        let truncate = flags.0 & libc_o_trunc() != 0;
        let info = match self.s3.info(&path) {
            Ok(i) => i,
            Err(e) => return reply.error(e),
        };
        let meta = info.as_ref().map(|i| i.meta.clone()).unwrap_or_default();
        let base = if truncate { None } else { info.map(|i| (i.etag, i.size)) };
        match self.stage(base, append, meta) {
            Ok(s) => {
                let fh = self.new_handle(path, Open::Write(s));
                reply.opened(fh, FopenFlags::FOPEN_DIRECT_IO)
            }
            Err(e) => reply.error(e),
        }
    }

    fn create(&self, req: &Request, parent: INodeNo, name: &OsStr, mode: u32, umask: u32, flags: i32, reply: ReplyCreate) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let append = flags & libc_o_append() != 0;
        let path = match self.path(parent).and_then(|d| join(&d, name)) {
            Ok(p) => p,
            Err(e) => return reply.error(e),
        };
        let meta = self.new_meta(req, S_IFREG, mode & !umask);
        match self.stage(None, append, meta.clone()) {
            Ok(s) => {
                let ino = self.ino(&path);
                let fh = self.new_handle(path, Open::Write(s));
                // Store the (empty) file now, so it exists for others as soon as it is created.
                if let Err(e) = self.store(fh) {
                    self.state.lock().unwrap().handles.remove(&fh.0);
                    return reply.error(e);
                }
                let attr = self.attr_of(ino, FileType::RegularFile, 0, SystemTime::now(), &meta);
                reply.created(&TTL, &attr, Generation(0), fh, FopenFlags::FOPEN_DIRECT_IO);
            }
            Err(e) => reply.error(e),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn read(&self, _: &Request, _: INodeNo, fh: FileHandle, offset: u64, size: u32, _: OpenFlags, _: Option<LockOwner>, reply: ReplyData) {
        if size == 0 {
            return reply.data(&[]);
        }
        let path = {
            let mut st = self.state.lock().unwrap();
            match st.handles.get_mut(&fh.0) {
                Some((p, Open::Read)) => p.clone(),
                Some((p, Open::Write(s))) => {
                    let p = p.clone();
                    return match self.read_staged(&p, s, offset, size as u64) {
                        Ok(b) => reply.data(&b),
                        Err(e) => reply.error(e),
                    };
                }
                _ => return reply.error(Errno::EBADF),
            }
        };
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
            Some((p, Open::Write(s))) => {
                let p = p.clone();
                let off = if s.append { s.size } else { offset };
                match self.write_staged(&p, s, off, data) {
                    Ok(()) => reply.written(data.len() as u32),
                    Err(e) => reply.error(e),
                }
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

    fn mkdir(&self, req: &Request, parent: INodeNo, name: &OsStr, mode: u32, umask: u32, reply: ReplyEntry) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let res = self.path(parent).and_then(|d| join(&d, name)).and_then(|p| {
            if self.s3.head(&p)?.is_some() || self.s3.is_dir(&p)? {
                return Err(Errno::EEXIST);
            }
            let meta = self.new_meta(req, S_IFDIR, mode & !umask);
            self.s3.put_small(&format!("{p}/"), b"", &meta, false)?;
            Ok(self.attr_of(self.ino(&p), FileType::Directory, 0, SystemTime::now(), &meta))
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
            if flags.contains(RenameFlags::RENAME_EXCHANGE) {
                return Err(Errno::EOPNOTSUPP);
            }
            let replace = !flags.contains(RenameFlags::RENAME_NOREPLACE);
            let dir = self.s3.head(&from)?.is_none();
            if dir && !self.s3.is_dir(&from)? {
                return Err(Errno::ENOENT);
            }
            match self.s3.rename(&from, &to, dir, replace) {
                Ok(()) => {}
                Err(Errno::EXDEV) => return Err(Errno::EXDEV), // a directory in a versioned bucket: mv copies it
                Err(e) if dir => return Err(e),
                Err(Errno::EEXIST) if !replace => return Err(Errno::EEXIST),
                Err(_) => {
                    // A file in a versioned bucket: copy, then delete.
                    self.s3.copy(&from, &to)?;
                    self.s3.delete(&from)?;
                }
            }
            // Inodes follow their paths (a directory's whole subtree).
            let mut st = self.state.lock().unwrap();
            let moved: Vec<(String, u64)> =
                st.inodes.iter().filter(|(p, _)| **p == from || p.starts_with(&format!("{from}/"))).map(|(p, i)| (p.clone(), *i)).collect();
            for (p, i) in moved {
                let np = format!("{to}{}", &p[from.len()..]);
                st.inodes.remove(&p);
                st.paths.insert(i, np.clone());
                st.inodes.insert(np, i);
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

    fn symlink(&self, req: &Request, parent: INodeNo, link_name: &OsStr, target: &std::path::Path, reply: ReplyEntry) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let res = (|| {
            let p = join(&self.path(parent)?, link_name)?;
            let target = target.to_str().ok_or(Errno::EINVAL)?;
            if self.s3.is_dir(&p)? {
                return Err(Errno::EEXIST);
            }
            let meta = self.new_meta(req, S_IFLNK, 0o777);
            self.s3.put_small(&p, target.as_bytes(), &meta, true)?;
            Ok(self.attr_of(self.ino(&p), FileType::Symlink, target.len() as u64, SystemTime::now(), &meta))
        })();
        match res {
            Ok(a) => reply.entry(&TTL, &a, Generation(0)),
            Err(e) => reply.error(e),
        }
    }

    fn readlink(&self, _: &Request, ino: INodeNo, reply: ReplyData) {
        let res = (|| {
            let p = self.path(ino)?;
            let i = self.s3.info(&p)?.ok_or(Errno::ENOENT)?;
            if !i.meta.is_symlink() {
                return Err(Errno::EINVAL);
            }
            if i.size == 0 {
                return Ok(Bytes::new());
            }
            self.s3.get(&p, 0, i.size)
        })();
        match res {
            Ok(b) => reply.data(&b),
            Err(e) => reply.error(e),
        }
    }

    /// Hard links need two names for one object, which a bucket cannot have.
    fn link(&self, _: &Request, _: INodeNo, _: INodeNo, _: &OsStr, reply: ReplyEntry) {
        reply.error(Errno::EPERM);
    }

    fn setxattr(&self, _: &Request, ino: INodeNo, name: &OsStr, value: &[u8], flags: i32, _: u32, reply: ReplyEmpty) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        const XATTR_CREATE: i32 = 1;
        const XATTR_REPLACE: i32 = 2;
        let res = (|| {
            let p = self.path(ino)?;
            let name = name.to_str().ok_or(Errno::EINVAL)?.to_string();
            // The kernel's own namespaces (security., system., trusted.) are not the mount's to keep.
            if !name.starts_with("user.") {
                return Err(Errno::EOPNOTSUPP);
            }
            let (_, info) = self.meta_target(&p)?;
            let exists = info.meta.xattrs.contains_key(&name);
            if flags & XATTR_CREATE != 0 && exists {
                return Err(Errno::EEXIST);
            }
            if flags & XATTR_REPLACE != 0 && !exists {
                return Err(Errno::NO_XATTR);
            }
            let value = value.to_vec();
            self.update_meta(&p, |m| {
                m.xattrs.insert(name.clone(), value.clone());
            })
        })();
        match res {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e),
        }
    }

    fn getxattr(&self, _: &Request, ino: INodeNo, name: &OsStr, size: u32, reply: fuser::ReplyXattr) {
        let res = (|| {
            let p = self.path(ino)?;
            let meta = self.current_meta(&p)?;
            meta.xattrs.get(name.to_str().ok_or(Errno::EINVAL)?).cloned().ok_or(Errno::NO_XATTR)
        })();
        match res {
            Ok(v) if size == 0 => reply.size(v.len() as u32),
            Ok(v) if v.len() > size as usize => reply.error(Errno::ERANGE),
            Ok(v) => reply.data(&v),
            Err(e) => reply.error(e),
        }
    }

    fn listxattr(&self, _: &Request, ino: INodeNo, size: u32, reply: fuser::ReplyXattr) {
        let res = self.path(ino).and_then(|p| self.current_meta(&p));
        match res {
            Ok(meta) => {
                let mut out = vec![];
                for k in meta.xattrs.keys() {
                    out.extend_from_slice(k.as_bytes());
                    out.push(0);
                }
                if size == 0 {
                    reply.size(out.len() as u32)
                } else if out.len() > size as usize {
                    reply.error(Errno::ERANGE)
                } else {
                    reply.data(&out)
                }
            }
            Err(e) => reply.error(e),
        }
    }

    fn removexattr(&self, _: &Request, ino: INodeNo, name: &OsStr, reply: ReplyEmpty) {
        if self.read_only {
            return reply.error(Errno::EROFS);
        }
        let res = (|| {
            let p = self.path(ino)?;
            let name = name.to_str().ok_or(Errno::EINVAL)?.to_string();
            if !self.current_meta(&p)?.xattrs.contains_key(&name) {
                return Err(Errno::NO_XATTR);
            }
            self.update_meta(&p, |m| {
                m.xattrs.remove(&name);
            })
        })();
        match res {
            Ok(()) => reply.ok(),
            Err(e) => reply.error(e),
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
    eprintln!("usage: cairn-store mount <bucket>[/prefix] <directory> [--read-only] [--allow-other] [--enforce-modes] [--uid=N] [--gid=N]");
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
    let enforce_modes = args.iter().any(|a| a == "--enforce-modes");
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
    // Files show as owned by the directory's owner, or by --uid=N / --gid=N (for pods running as non-root).
    let id_flag = |name: &str| args.iter().find_map(|a| a.strip_prefix(&format!("--{name}="))?.parse::<u32>().ok());
    let (uid, gid) = (id_flag("uid").unwrap_or(meta.uid()), id_flag("gid").unwrap_or(meta.gid()));
    let fs = CairnFs { s3, state: Mutex::new(State::default()), uid, gid, read_only, staging, enforce_modes };
    let mut cfg = fuser::Config::default();
    cfg.mount_options =
        vec![MountOption::FSName(format!("cairn:{target}")), MountOption::Subtype("cairn".into()), MountOption::NoDev, MountOption::NoSuid];
    if read_only {
        cfg.mount_options.push(MountOption::RO);
    }
    if enforce_modes {
        cfg.mount_options.push(MountOption::DefaultPermissions);
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
