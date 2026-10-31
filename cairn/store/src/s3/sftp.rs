//! SFTP (SSH File Transfer Protocol, version 3) on `CAIRN_STORE_SFTP_ADDR`, for `sftp`, `scp -s`,
//! WinSCP, FileZilla, rclone and similar clients. Off unless the address is set.
//!
//! Paths are `/<bucket>/<key>`: `/` lists the buckets the key may see. Every operation is the S3
//! operation it stands for (GetObject with a range per read, PutObject on close, DeleteObject,
//! CopyObject, ListObjectsV2 with a `/` delimiter, CreateBucket), run through the same request path, so
//! policies, Cairn's policy decision point, quotas, encryption, events and the audit trail all apply.
//!
//! Sign-in: the SSH user name is the access key. A key signs in with its secret key as the password
//! (unless `CAIRN_STORE_SFTP_PASSWORD_AUTH=false`) or with an SSH public key listed under `ssh_keys` in
//! the credentials file. Only the `sftp` subsystem is served: no shell, exec or forwarding.
//!
//! What S3 cannot express is refused rather than faked: files are written sequentially from the start
//! (uploads are staged in the data directory's temporary area, then stored on close, atomically); no
//! appends, in-place updates, permissions, timestamps (setstat), links or directory renames.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::{BodyExt, Empty};
use hyper::header::{HeaderMap, HeaderValue};
use hyper::Method;
use russh::keys::ssh_key::{private::Ed25519Keypair, LineEnding};
use russh::keys::{PrivateKey, PublicKey};
use russh::server::{Auth as SshAuth, ChannelOpenHandle, Msg, Session, VerifiedPublicKey};
use russh::{Channel, ChannelId};
use russh_sftp::protocol::{Attrs, Data, File, FileAttributes, Handle, Name, OpenFlags, Status, StatusCode, Version};
use russh_sftp::server::StatusReply;
use sha2::{Digest, Sha256};

use super::auth::{aws_encode, Auth, Credential};
use super::errors::ApiError;
use super::{Ctx, ErrorCode, Server};
use crate::engine::ListOptions;

/// How many directory entries one READDIR answer carries.
const PAGE: usize = 500;
const MAX_LISTING: usize = 100_000;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

#[derive(Default)]
pub struct SftpMetrics {
    pub sessions: AtomicU64,
    pub auth_failures: AtomicU64,
}

pub struct SftpConfig {
    pub addr: String,
    /// The host key (OpenSSH private key). Created (Ed25519) on first start when missing.
    pub host_key: PathBuf,
    pub password_auth: bool,
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    let (x, y) = (Sha256::digest(a), Sha256::digest(b));
    x.iter().zip(y.iter()).fold(0u8, |acc, (p, q)| acc | (p ^ q)) == 0
}

/// Loads the host key, or creates one (mode 0600) when the file does not exist.
fn host_key(path: &PathBuf) -> Result<PrivateKey, String> {
    match std::fs::read_to_string(path) {
        Ok(pem) => PrivateKey::from_openssh(pem).map_err(|e| format!("{}: {e}", path.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let key = PrivateKey::from(Ed25519Keypair::from_seed(&rand::random::<[u8; 32]>()));
            let pem = key.to_openssh(LineEnding::LF).map_err(|e| e.to_string())?;
            use std::io::Write as _;
            use std::os::unix::fs::OpenOptionsExt as _;
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(path)
                .map_err(|e| format!("{}: {e}", path.display()))?;
            f.write_all(pem.as_bytes()).map_err(|e| format!("{}: {e}", path.display()))?;
            tracing::info!(path = %path.display(), "created an SFTP host key");
            Ok(key)
        }
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}

/// Each key's SSH public keys, parsed (a bad key fails start-up rather than being ignored).
fn ssh_keys(creds: &std::collections::BTreeMap<String, Credential>) -> Result<HashMap<String, Vec<PublicKey>>, String> {
    let mut out = HashMap::new();
    for (ak, c) in creds {
        let mut keys = vec![];
        for k in &c.ssh_keys {
            keys.push(PublicKey::from_openssh(k.trim()).map_err(|e| format!("ssh_keys of {ak}: {e}"))?);
        }
        out.insert(ak.clone(), keys);
    }
    Ok(out)
}

/// Runs the SFTP server until the process ends.
pub async fn serve(srv: Arc<Server>, cfg: SftpConfig) -> Result<(), String> {
    let key = host_key(&cfg.host_key)?;
    let fingerprint = key.public_key().fingerprint(Default::default()).to_string();
    let keys = Arc::new(ssh_keys(&srv.creds)?);
    let config = Arc::new(russh::server::Config {
        auth_rejection_time: Duration::from_secs(2),
        auth_rejection_time_initial: Some(Duration::from_secs(0)),
        max_auth_attempts: 6,
        inactivity_timeout: Some(Duration::from_secs(3600)),
        keys: vec![key],
        ..Default::default()
    });
    let listener = tokio::net::TcpListener::bind(crate::listen_addr(&cfg.addr)).await.map_err(|e| format!("SFTP listen: {e}"))?;
    tracing::info!(addr = %cfg.addr, host_key = %fingerprint, "SFTP listening");
    let mut server = SshServer { srv, keys, password_auth: cfg.password_auth };
    use russh::server::Server as _;
    server.run_on_socket(config, &listener).await.map_err(|e| format!("SFTP: {e}"))
}

struct SshServer {
    srv: Arc<Server>,
    keys: Arc<HashMap<String, Vec<PublicKey>>>,
    password_auth: bool,
}

impl russh::server::Server for SshServer {
    type Handler = SshConn;

    fn new_client(&mut self, peer: Option<SocketAddr>) -> SshConn {
        SshConn {
            srv: self.srv.clone(),
            keys: self.keys.clone(),
            password_auth: self.password_auth,
            peer,
            cred: None,
            channels: HashMap::new(),
        }
    }
}

struct SshConn {
    srv: Arc<Server>,
    keys: Arc<HashMap<String, Vec<PublicKey>>>,
    password_auth: bool,
    peer: Option<SocketAddr>,
    cred: Option<Credential>,
    channels: HashMap<ChannelId, Channel<Msg>>,
}

impl SshConn {
    fn rejected(&self, user: &str, method: &str) -> SshAuth {
        self.srv.sftp_metrics.auth_failures.fetch_add(1, Ordering::Relaxed);
        tracing::warn!(user, method, peer = ?self.peer, "SFTP sign-in refused");
        SshAuth::reject()
    }

    fn accepted(&mut self, user: &str, method: &str) -> SshAuth {
        self.cred = self.srv.creds.get(user).cloned();
        self.srv.sftp_metrics.sessions.fetch_add(1, Ordering::Relaxed);
        tracing::info!(user, method, peer = ?self.peer, "SFTP session");
        SshAuth::Accept
    }
}

impl russh::server::Handler for SshConn {
    type Error = russh::Error;

    async fn auth_password(&mut self, user: &str, password: &str) -> Result<SshAuth, Self::Error> {
        let ok = self.password_auth && self.srv.creds.get(user).is_some_and(|c| ct_eq(c.secret_key.as_bytes(), password.as_bytes()));
        Ok(if ok { self.accepted(user, "password") } else { self.rejected(user, "password") })
    }

    async fn auth_publickey_offered(&mut self, user: &str, offer: russh::server::PublicKeyOffer<'_>) -> Result<SshAuth, Self::Error> {
        let known = self.keys.get(user).is_some_and(|ks| ks.iter().any(|k| k.key_data() == offer.public_key.key_data()));
        Ok(if known { SshAuth::Accept } else { SshAuth::reject() })
    }

    async fn auth_publickey(&mut self, user: &str, key: VerifiedPublicKey<'_>) -> Result<SshAuth, Self::Error> {
        let known = self.keys.get(user).is_some_and(|ks| ks.iter().any(|k| k.key_data() == key.public_key.key_data()));
        Ok(if known { self.accepted(user, "publickey") } else { self.rejected(user, "publickey") })
    }

    async fn channel_open_session(&mut self, channel: Channel<Msg>, reply: ChannelOpenHandle, _: &mut Session) -> Result<(), Self::Error> {
        self.channels.insert(channel.id(), channel);
        reply.accept().await;
        Ok(())
    }

    async fn channel_eof(&mut self, channel: ChannelId, session: &mut Session) -> Result<(), Self::Error> {
        session.close(channel)?;
        Ok(())
    }

    async fn shell_request(&mut self, channel: ChannelId, session: &mut Session) -> Result<(), Self::Error> {
        session.channel_failure(channel)?;
        Ok(())
    }

    async fn exec_request(&mut self, channel: ChannelId, _: &[u8], session: &mut Session) -> Result<(), Self::Error> {
        session.channel_failure(channel)?;
        Ok(())
    }

    async fn subsystem_request(&mut self, channel: ChannelId, name: &str, session: &mut Session) -> Result<(), Self::Error> {
        match (name, self.cred.clone(), self.channels.remove(&channel)) {
            ("sftp", Some(cred), Some(ch)) => {
                session.channel_success(channel)?;
                let mut auth = Auth::anonymous();
                auth.cred = cred;
                let sftp = Sftp { srv: self.srv.clone(), auth, peer: self.peer.map(|p| p.ip()), handles: HashMap::new(), next: 0 };
                russh_sftp::server::run(ch.into_stream(), sftp).await;
            }
            _ => session.channel_failure(channel)?,
        }
        Ok(())
    }
}

/// A staged upload: removed if the session ends before CLOSE.
struct Staged {
    path: PathBuf,
    file: std::fs::File,
    written: u64,
    exclusive: bool,
}

impl Drop for Staged {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

enum Open {
    Dir { entries: Vec<File>, at: usize },
    Read { bucket: String, key: String, size: u64 },
    Write { bucket: String, key: String, staged: Staged },
}

struct Sftp {
    srv: Arc<Server>,
    auth: Auth,
    peer: Option<std::net::IpAddr>,
    handles: HashMap<String, Open>,
    next: u64,
}

fn reply(code: StatusCode, msg: &str) -> StatusReply {
    StatusReply::new(code).with_message(msg)
}

fn from_api(err: &ApiError) -> StatusReply {
    let code = match err.status {
        404 => StatusCode::NoSuchFile,
        401 | 403 => StatusCode::PermissionDenied,
        501 => StatusCode::OpUnsupported,
        _ => StatusCode::Failure,
    };
    reply(code, &format!("{}: {}", err.code, err.message))
}

fn ok(id: u32) -> Status {
    Status { id, status_code: StatusCode::Ok, error_message: "Ok".into(), language_tag: "en-US".into() }
}

/// Normalizes a client path ("." and ".." resolved, relative to "/") into "/bucket/key" form.
fn normalize(path: &str) -> String {
    let mut parts: Vec<&str> = vec![];
    for p in path.split('/') {
        match p {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            p => parts.push(p),
        }
    }
    format!("/{}", parts.join("/"))
}

fn split(path: &str) -> (String, String) {
    let n = normalize(path);
    let p = n.trim_start_matches('/');
    match p.split_once('/') {
        Some((b, k)) => (b.to_string(), k.to_string()),
        None => (p.to_string(), String::new()),
    }
}

fn file_attrs(size: u64, mtime_ms: u64) -> FileAttributes {
    let mut a = FileAttributes {
        size: Some(size),
        mtime: Some((mtime_ms / 1000) as u32),
        atime: Some((mtime_ms / 1000) as u32),
        ..Default::default()
    };
    a.set_regular(true);
    a.permissions = Some(a.permissions.unwrap_or(0) | 0o644);
    a
}

fn dir_attrs(mtime_ms: u64) -> FileAttributes {
    let mut a = FileAttributes { size: Some(0), mtime: Some((mtime_ms / 1000) as u32), ..Default::default() };
    a.set_dir(true);
    a.permissions = Some(a.permissions.unwrap_or(0) | 0o755);
    a
}

impl Sftp {
    fn ctx(&self, method: Method, bucket: &str, key: &str) -> Ctx {
        Ctx {
            method,
            bucket: bucket.to_string(),
            key: key.to_string(),
            path: format!("/{}/{}", aws_encode(bucket.as_bytes(), false), aws_encode(key.as_bytes(), true)),
            query: Default::default(),
            headers: HeaderMap::new(),
            auth: self.auth.clone(),
            content_length: None,
            op: "unknown",
            peer: self.peer,
            request_id: hex::encode_upper(rand::random::<[u8; 8]>()),
        }
    }

    /// Runs an S3 operation through the server's request path.
    async fn call<B>(&self, mut c: Ctx, body: B) -> Result<(HeaderMap, Bytes), StatusReply>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let resp = match self.srv.route(&mut c, body).await {
            Ok(r) => r,
            Err(err) => {
                self.srv.audit_denial(&c, &err);
                return Err(from_api(&err));
            }
        };
        if resp.status().as_u16() >= 400 {
            let code = resp.extensions().get::<ErrorCode>().map(|c| c.0).unwrap_or("Error");
            let status = resp.status().as_u16();
            return Err(from_api(&ApiError { code, message: resp.status().canonical_reason().unwrap_or("Error"), status }));
        }
        let headers = resp.headers().clone();
        let bytes = resp.into_body().collect().await.map_err(|e| reply(StatusCode::Failure, &e.to_string()))?.to_bytes();
        Ok((headers, bytes))
    }

    async fn head(&self, bucket: &str, key: &str) -> Result<crate::engine::Object, StatusReply> {
        self.call(self.ctx(Method::HEAD, bucket, key), Empty::<Bytes>::new()).await?;
        match blocking(|| self.srv.eng.get_object(bucket, key)) {
            Ok(o) if !o.delete_marker => Ok(o),
            _ => Err(reply(StatusCode::NoSuchFile, "no such file")),
        }
    }

    /// Is `/bucket/key` a directory (a bucket, or a prefix with keys under it) the key may list?
    async fn is_dir(&self, bucket: &str, key: &str) -> Result<bool, StatusReply> {
        let c = self.ctx(Method::GET, bucket, "");
        if key.is_empty() {
            self.srv.may(&c, "HeadBucket", bucket, "", None).await.map_err(|e| from_api(&e))?;
            return blocking(|| self.srv.eng.head_bucket(bucket)).map(|_| true).map_err(|e| from_api(&e.into()));
        }
        let prefix = format!("{}/", key.trim_end_matches('/'));
        self.srv.may(&c, "ListObjectsV2", bucket, "", Some(&prefix)).await.map_err(|e| from_api(&e))?;
        self.srv.collection_exists(bucket, &prefix).map_err(|e| from_api(&e))
    }

    async fn attrs(&self, path: &str) -> Result<FileAttributes, StatusReply> {
        let (bucket, key) = split(path);
        if bucket.is_empty() {
            return Ok(dir_attrs(0));
        }
        if !key.is_empty() {
            match self.head(&bucket, &key).await {
                Ok(o) => return Ok(file_attrs(o.size, o.modified)),
                Err(e) if e.status_code != StatusCode::NoSuchFile => return Err(e),
                Err(_) => {}
            }
        }
        if self.is_dir(&bucket, &key).await? {
            Ok(dir_attrs(0))
        } else {
            Err(reply(StatusCode::NoSuchFile, "no such file or directory"))
        }
    }

    async fn listing(&self, path: &str) -> Result<Vec<File>, StatusReply> {
        let (bucket, key) = split(path);
        let c = self.ctx(Method::GET, &bucket, "");
        let mut out = vec![];
        if bucket.is_empty() {
            self.srv.may(&c, "ListBuckets", "", "", None).await.map_err(|e| from_api(&e))?;
            let cred = &self.auth.cred;
            let visible = |name: &str| cred.admin || cred.policy.is_some() || cred.allows(name);
            for b in blocking(|| self.srv.eng.list_buckets()).map_err(|e| from_api(&e.into()))?.into_iter().filter(|b| visible(&b.name)) {
                out.push(File::new(b.name.clone(), dir_attrs(b.created)));
            }
            return Ok(out);
        }
        if !self.is_dir(&bucket, &key).await? {
            return Err(reply(StatusCode::NoSuchFile, "no such directory"));
        }
        let prefix = if key.is_empty() { String::new() } else { format!("{}/", key.trim_end_matches('/')) };
        let mut after = String::new();
        loop {
            let opt = ListOptions { prefix: prefix.clone(), delimiter: "/".into(), after: after.clone(), max_keys: 1000 };
            let page = blocking(|| self.srv.eng.list(&bucket, &opt)).map_err(|e| from_api(&e.into()))?;
            for p in &page.common_prefixes {
                out.push(File::new(p[prefix.len()..].trim_end_matches('/'), dir_attrs(0)));
            }
            for o in page.objects.iter().filter(|o| o.key != prefix) {
                out.push(File::new(&o.key[prefix.len()..], file_attrs(o.size, o.modified)));
            }
            if out.len() > MAX_LISTING {
                return Err(reply(StatusCode::Failure, "directory too large to list over SFTP"));
            }
            if !page.truncated {
                return Ok(out);
            }
            after = page.next_after;
        }
    }

    fn handle(&mut self, h: Open) -> String {
        self.next += 1;
        let id = format!("h{}", self.next);
        self.handles.insert(id.clone(), h);
        id
    }
}

impl russh_sftp::server::Handler for Sftp {
    type Error = StatusReply;

    fn unimplemented(&self) -> StatusReply {
        reply(StatusCode::OpUnsupported, "not supported by Cairn Store")
    }

    async fn init(&mut self, _: u32, _: HashMap<String, String>) -> Result<Version, StatusReply> {
        Ok(Version::new())
    }

    async fn realpath(&mut self, id: u32, path: String) -> Result<Name, StatusReply> {
        Ok(Name { id, files: vec![File::dummy(normalize(&path))] })
    }

    async fn stat(&mut self, id: u32, path: String) -> Result<Attrs, StatusReply> {
        Ok(Attrs { id, attrs: self.attrs(&path).await? })
    }

    async fn lstat(&mut self, id: u32, path: String) -> Result<Attrs, StatusReply> {
        Ok(Attrs { id, attrs: self.attrs(&path).await? })
    }

    async fn fstat(&mut self, id: u32, handle: String) -> Result<Attrs, StatusReply> {
        let attrs = match self.handles.get(&handle) {
            Some(Open::Read { size, .. }) => file_attrs(*size, 0),
            Some(Open::Write { staged, .. }) => file_attrs(staged.written, crate::engine::now_ms()),
            Some(Open::Dir { .. }) => dir_attrs(0),
            None => return Err(reply(StatusCode::Failure, "unknown handle")),
        };
        Ok(Attrs { id, attrs })
    }

    async fn opendir(&mut self, id: u32, path: String) -> Result<Handle, StatusReply> {
        let entries = self.listing(&path).await?;
        Ok(Handle { id, handle: self.handle(Open::Dir { entries, at: 0 }) })
    }

    async fn readdir(&mut self, id: u32, handle: String) -> Result<Name, StatusReply> {
        match self.handles.get_mut(&handle) {
            Some(Open::Dir { entries, at }) => {
                if *at >= entries.len() {
                    return Err(reply(StatusCode::Eof, "end of directory"));
                }
                let end = (*at + PAGE).min(entries.len());
                let files = entries[*at..end].to_vec();
                *at = end;
                Ok(Name { id, files })
            }
            _ => Err(reply(StatusCode::Failure, "not a directory handle")),
        }
    }

    async fn open(&mut self, id: u32, filename: String, pflags: OpenFlags, _: FileAttributes) -> Result<Handle, StatusReply> {
        let (bucket, key) = split(&filename);
        if bucket.is_empty() || key.is_empty() {
            return Err(reply(StatusCode::Failure, "not a file path (/bucket/key)"));
        }
        if pflags.contains(OpenFlags::WRITE) {
            if pflags.contains(OpenFlags::APPEND) || pflags.contains(OpenFlags::READ) {
                return Err(reply(
                    StatusCode::OpUnsupported,
                    "appending and read-write opens are not supported: objects are written whole",
                ));
            }
            if !pflags.contains(OpenFlags::CREATE) && !pflags.contains(OpenFlags::TRUNCATE) {
                return Err(reply(StatusCode::OpUnsupported, "in-place updates are not supported: objects are written whole"));
            }
            // Fail early if the key may not write here (the PUT on close is authorized again).
            let c = self.ctx(Method::PUT, &bucket, &key);
            self.srv.may(&c, "PutObject", &bucket, &key, None).await.map_err(|e| from_api(&e))?;
            let (path, file) = self.srv.eng.temp_file().map_err(|e| reply(StatusCode::Failure, &e.to_string()))?;
            let staged = Staged { path, file, written: 0, exclusive: pflags.contains(OpenFlags::EXCLUDE) };
            return Ok(Handle { id, handle: self.handle(Open::Write { bucket, key, staged }) });
        }
        let o = self.head(&bucket, &key).await?;
        Ok(Handle { id, handle: self.handle(Open::Read { bucket, key, size: o.size }) })
    }

    async fn read(&mut self, id: u32, handle: String, offset: u64, len: u32) -> Result<Data, StatusReply> {
        let (bucket, key, size) = match self.handles.get(&handle) {
            Some(Open::Read { bucket, key, size }) => (bucket.clone(), key.clone(), *size),
            _ => return Err(reply(StatusCode::Failure, "not a file opened for reading")),
        };
        if offset >= size || len == 0 {
            return Err(reply(StatusCode::Eof, "end of file"));
        }
        let end = (offset + len as u64).min(size) - 1;
        let mut c = self.ctx(Method::GET, &bucket, &key);
        c.headers.insert("range", HeaderValue::from_str(&format!("bytes={offset}-{end}")).unwrap());
        let (_, data) = self.call(c, Empty::<Bytes>::new()).await?;
        Ok(Data { id, data: data.to_vec() })
    }

    async fn write(&mut self, id: u32, handle: String, offset: u64, data: Vec<u8>) -> Result<Status, StatusReply> {
        match self.handles.get_mut(&handle) {
            Some(Open::Write { staged, .. }) => {
                if offset != staged.written {
                    return Err(reply(StatusCode::OpUnsupported, "writes must be sequential from the start of the file"));
                }
                use std::io::Write as _;
                blocking(|| staged.file.write_all(&data)).map_err(|e| reply(StatusCode::Failure, &e.to_string()))?;
                staged.written += data.len() as u64;
                Ok(ok(id))
            }
            _ => Err(reply(StatusCode::Failure, "not a file opened for writing")),
        }
    }

    async fn close(&mut self, id: u32, handle: String) -> Result<Status, StatusReply> {
        match self.handles.remove(&handle) {
            Some(Open::Write { bucket, key, staged }) => {
                use std::io::Seek as _;
                let mut f = staged.file.try_clone().map_err(|e| reply(StatusCode::Failure, &e.to_string()))?;
                f.rewind().map_err(|e| reply(StatusCode::Failure, &e.to_string()))?;
                let mut c = self.ctx(Method::PUT, &bucket, &key);
                c.headers.insert("content-length", HeaderValue::from(staged.written));
                c.content_length = Some(staged.written);
                if staged.exclusive {
                    c.headers.insert("if-none-match", HeaderValue::from_static("*"));
                }
                let res = self.call(c, super::tiering::file_body(f)).await;
                drop(staged); // removes the staged file
                res.map(|_| ok(id))
            }
            Some(_) => Ok(ok(id)),
            None => Err(reply(StatusCode::Failure, "unknown handle")),
        }
    }

    async fn remove(&mut self, id: u32, filename: String) -> Result<Status, StatusReply> {
        let (bucket, key) = split(&filename);
        self.head(&bucket, &key).await?; // a missing file is an error here, unlike S3's DELETE
        self.call(self.ctx(Method::DELETE, &bucket, &key), Empty::<Bytes>::new()).await?;
        Ok(ok(id))
    }

    async fn mkdir(&mut self, id: u32, path: String, _: FileAttributes) -> Result<Status, StatusReply> {
        let (bucket, key) = split(&path);
        if bucket.is_empty() {
            return Err(reply(StatusCode::Failure, "invalid path"));
        }
        let mut c = self.ctx(Method::PUT, &bucket, "");
        if !key.is_empty() {
            if self.is_dir(&bucket, &key).await? {
                return Err(reply(StatusCode::Failure, "directory exists"));
            }
            c.key = format!("{}/", key.trim_end_matches('/')); // an empty marker, as S3 consoles write
            c.headers.insert("if-none-match", HeaderValue::from_static("*"));
        }
        c.headers.insert("content-length", HeaderValue::from_static("0"));
        c.content_length = Some(0);
        self.call(c, Empty::<Bytes>::new()).await?; // a bucket name: CreateBucket
        Ok(ok(id))
    }

    async fn rmdir(&mut self, id: u32, path: String) -> Result<Status, StatusReply> {
        let (bucket, key) = split(&path);
        if key.is_empty() {
            return Err(reply(StatusCode::PermissionDenied, "buckets are deleted with the S3 API"));
        }
        let prefix = format!("{}/", key.trim_end_matches('/'));
        if !self.is_dir(&bucket, &key).await? {
            return Err(reply(StatusCode::NoSuchFile, "no such directory"));
        }
        let page = blocking(|| self.srv.eng.list(&bucket, &ListOptions { prefix: prefix.clone(), max_keys: 2, ..Default::default() }))
            .map_err(|e| from_api(&e.into()))?;
        if page.objects.iter().any(|o| o.key != prefix) || !page.common_prefixes.is_empty() {
            return Err(reply(StatusCode::Failure, "directory not empty"));
        }
        self.call(self.ctx(Method::DELETE, &bucket, &prefix), Empty::<Bytes>::new()).await?;
        Ok(ok(id))
    }

    async fn rename(&mut self, id: u32, oldpath: String, newpath: String) -> Result<Status, StatusReply> {
        let ((sb, sk), (db, dk)) = (split(&oldpath), split(&newpath));
        if sk.is_empty() || dk.is_empty() {
            return Err(reply(StatusCode::OpUnsupported, "buckets cannot be renamed"));
        }
        match self.head(&sb, &sk).await {
            Ok(_) => {}
            Err(e) if e.status_code == StatusCode::NoSuchFile && self.is_dir(&sb, &sk).await.unwrap_or(false) => {
                return Err(reply(StatusCode::OpUnsupported, "directories cannot be renamed over SFTP: use WebDAV MOVE or the S3 API"));
            }
            Err(e) => return Err(e),
        }
        if self.head(&db, &dk).await.is_ok() {
            return Err(reply(StatusCode::Failure, "the target exists")); // SFTP v3 rename does not overwrite
        }
        let mut c = self.ctx(Method::PUT, &db, &dk);
        let src = format!("/{}/{}", aws_encode(sb.as_bytes(), false), aws_encode(sk.as_bytes(), true));
        c.headers.insert("x-amz-copy-source", HeaderValue::from_str(&src).map_err(|_| reply(StatusCode::Failure, "bad path"))?);
        c.headers.insert("if-none-match", HeaderValue::from_static("*"));
        self.call(c, Empty::<Bytes>::new()).await?;
        self.call(self.ctx(Method::DELETE, &sb, &sk), Empty::<Bytes>::new()).await?;
        Ok(ok(id))
    }
}
