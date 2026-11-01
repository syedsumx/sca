//! `cairn-store csi`: a Kubernetes CSI driver (`csi.cairn.store`) that provisions Cairn Store buckets as
//! persistent volumes and mounts them into pods with the FUSE client (`cairn-store mount`).
//!
//! - **Volumes.** CreateVolume makes a bucket of its own (named after the volume, with the requested size
//!   as a hard quota), or, with the StorageClass parameter `bucket`, a prefix in that shared bucket. Each
//!   volume carries a marker (`.cairn-csi-volume`), so an existing bucket of the same name is never
//!   taken over. DeleteVolume deletes the volume's objects (and its bucket); versioned or object-locked
//!   buckets are refused rather than half emptied.
//! - **Mounts.** NodePublishVolume runs `cairn-store mount` at the pod's target path (read-only when the
//!   volume is published read-only), as the user and group of the mount flags `uid=N` and `gid=N`. The
//!   key is the one in the volume's node-publish secret (`access_key`, `secret_key`), else the driver's.
//!   Block volumes are refused. Every access mode works, since many nodes can mount a bucket at once.
//!
//! The driver serves the CSI gRPC API on a unix socket (`CAIRN_STORE_CSI_ENDPOINT`, default
//! `unix:///csi/csi.sock`); `CAIRN_STORE_CSI_NODE_ID` names the node.

#[path = "csi_v1.rs"]
#[allow(clippy::all)]
pub mod v1;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use tonic::{Request, Response, Status};

use crate::s3::auth::aws_encode;
use v1::*;

pub const DRIVER: &str = "csi.cairn.store";
const MARKER: &str = ".cairn-csi-volume";

type Body = BoxBody<Bytes, std::io::Error>;

/// The store the driver talks to (with its own key, for provisioning).
struct Store {
    endpoint: String,
    ak: String,
    sk: String,
    region: String,
    http: Client<hyper_rustls::HttpsConnector<HttpConnector>, Body>,
}

struct Reply {
    status: u16,
    body: Bytes,
}

impl Store {
    async fn call(&self, method: &str, path_q: &str, headers: &[(&str, &str)], body: Vec<u8>) -> Result<Reply, Status> {
        let url = format!("{}{path_q}", self.endpoint);
        let payload = hex::encode(<sha2::Sha256 as sha2::Digest>::digest(&body));
        let mut req = hyper::Request::builder().method(method).uri(&url);
        for (k, v) in
            crate::admin_cli::sign_full(method, &url, &payload, &[], &self.ak, &self.sk, &self.region, time::OffsetDateTime::now_utc())
        {
            req = req.header(k, v);
        }
        for (k, v) in headers {
            req = req.header(*k, *v);
        }
        let n = body.len();
        let req = req
            .header("content-length", n)
            .body(Full::new(Bytes::from(body)).map_err(|never| match never {}).boxed())
            .map_err(|e| Status::internal(e.to_string()))?;
        let resp = tokio::time::timeout(Duration::from_secs(60), self.http.request(req))
            .await
            .map_err(|_| Status::unavailable("Cairn Store did not answer"))?
            .map_err(|e| Status::unavailable(format!("Cairn Store is not reachable: {e}")))?;
        let status = resp.status().as_u16();
        let body = resp.into_body().collect().await.map(|b| b.to_bytes()).unwrap_or_default();
        Ok(Reply { status, body })
    }

    fn obj(bucket: &str, key: &str) -> String {
        format!("/{}/{}", aws_encode(bucket.as_bytes(), false), aws_encode(key.as_bytes(), true))
    }

    fn fail(what: &str, r: &Reply) -> Status {
        let msg = format!("{what}: HTTP {}: {}", r.status, String::from_utf8_lossy(&r.body).chars().take(300).collect::<String>());
        match r.status {
            403 => Status::permission_denied(msg),
            404 => Status::not_found(msg),
            409 => Status::already_exists(msg),
            503 => Status::unavailable(msg),
            _ => Status::internal(msg),
        }
    }

    /// The marker's content (None: no marker).
    async fn marker(&self, bucket: &str, prefix: &str) -> Result<Option<String>, Status> {
        let r = self.call("GET", &Self::obj(bucket, &format!("{prefix}{MARKER}")), &[], vec![]).await?;
        match r.status {
            200 => Ok(Some(String::from_utf8_lossy(&r.body).into_owned())),
            404 => Ok(None),
            _ => Err(Self::fail("reading a volume marker", &r)),
        }
    }

    async fn put_marker(&self, bucket: &str, prefix: &str, name: &str) -> Result<(), Status> {
        let r = self.call("PUT", &Self::obj(bucket, &format!("{prefix}{MARKER}")), &[], name.as_bytes().to_vec()).await?;
        if r.status != 200 {
            return Err(Self::fail("writing a volume marker", &r));
        }
        Ok(())
    }

    async fn bucket_exists(&self, bucket: &str) -> Result<bool, Status> {
        let r = self.call("HEAD", &format!("/{}", aws_encode(bucket.as_bytes(), false)), &[], vec![]).await?;
        Ok(r.status == 200)
    }

    /// Refuses buckets whose objects cannot simply be deleted.
    async fn deletable(&self, bucket: &str) -> Result<(), Status> {
        let r = self.call("GET", &format!("/{}?versioning", aws_encode(bucket.as_bytes(), false)), &[], vec![]).await?;
        let doc = String::from_utf8_lossy(&r.body);
        if doc.contains("<Status>Enabled</Status>") || doc.contains("<Status>Suspended</Status>") {
            return Err(Status::failed_precondition(format!("bucket {bucket} is versioned: delete its data with the S3 API")));
        }
        let r = self.call("GET", &format!("/{}?object-lock", aws_encode(bucket.as_bytes(), false)), &[], vec![]).await?;
        if r.status == 200 {
            return Err(Status::failed_precondition(format!("bucket {bucket} has object lock: its data cannot be deleted here")));
        }
        Ok(())
    }

    /// Deletes every object under `prefix`.
    async fn delete_tree(&self, bucket: &str, prefix: &str) -> Result<u64, Status> {
        let mut n = 0;
        loop {
            let q = format!(
                "/{}?list-type=2&max-keys=1000&prefix={}",
                aws_encode(bucket.as_bytes(), false),
                aws_encode(prefix.as_bytes(), false)
            );
            let r = self.call("GET", &q, &[], vec![]).await?;
            if r.status == 404 {
                return Ok(n);
            }
            if r.status != 200 {
                return Err(Self::fail("listing a volume", &r));
            }
            let doc = String::from_utf8_lossy(&r.body).into_owned();
            let root = crate::s3::xml::parse(&doc).ok_or_else(|| Status::internal("unreadable listing"))?;
            let keys: Vec<String> = root.all("Contents").map(|c| c.text_of_raw("Key")).collect();
            if keys.is_empty() {
                return Ok(n);
            }
            for k in keys {
                let r = self.call("DELETE", &Self::obj(bucket, &k), &[], vec![]).await?;
                if r.status >= 300 && r.status != 404 {
                    return Err(Self::fail("deleting a volume's object", &r));
                }
                n += 1;
            }
        }
    }
}

/// A volume id: "<bucket>" (a bucket of its own) or "<bucket>/<prefix>" (a prefix of a shared bucket).
fn split_id(id: &str) -> Result<(String, String), Status> {
    let (b, p) = match id.split_once('/') {
        Some((b, p)) => (b, format!("{}/", p.trim_matches('/'))),
        None => (id, String::new()),
    };
    if b.len() < 3 || b.len() > 63 || !b.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '.') {
        return Err(Status::not_found(format!("{id} is not a volume of this driver")));
    }
    Ok((b.to_string(), p))
}

/// A bucket (or prefix) name for a volume name: lowercase letters, digits and '-', at most 63.
fn name_of(n: &str) -> Result<String, Status> {
    let mut s: String =
        n.to_ascii_lowercase().chars().map(|c| if c.is_ascii_lowercase() || c.is_ascii_digit() { c } else { '-' }).collect();
    s = s.trim_matches('-').to_string();
    s.truncate(63);
    if s.len() < 3 {
        return Err(Status::invalid_argument("a volume name needs at least 3 letters or digits"));
    }
    Ok(s)
}

fn check_caps(caps: &[VolumeCapability]) -> Result<(), Status> {
    if caps.is_empty() {
        return Err(Status::invalid_argument("volume capabilities are required"));
    }
    for c in caps {
        if matches!(c.access_type, Some(volume_capability::AccessType::Block(_))) {
            return Err(Status::invalid_argument("Cairn Store volumes are file systems: block volumes are not supported"));
        }
    }
    Ok(())
}

pub struct Driver {
    store: Arc<Store>,
    node_id: String,
    exe: std::path::PathBuf,
    /// Running mounts: target path -> the mount process.
    mounts: Mutex<HashMap<String, std::process::Child>>,
}

fn mounted(path: &str) -> bool {
    std::fs::read_to_string("/proc/self/mountinfo").is_ok_and(|m| m.lines().any(|l| l.split(' ').nth(4) == Some(path)))
}

#[tonic::async_trait]
impl identity_server::Identity for Arc<Driver> {
    async fn get_plugin_info(&self, _: Request<GetPluginInfoRequest>) -> Result<Response<GetPluginInfoResponse>, Status> {
        Ok(Response::new(GetPluginInfoResponse { name: DRIVER.into(), vendor_version: crate::VERSION.into(), ..Default::default() }))
    }

    async fn get_plugin_capabilities(
        &self,
        _: Request<GetPluginCapabilitiesRequest>,
    ) -> Result<Response<GetPluginCapabilitiesResponse>, Status> {
        let svc = PluginCapability {
            r#type: Some(plugin_capability::Type::Service(plugin_capability::Service {
                r#type: plugin_capability::service::Type::ControllerService as i32,
            })),
        };
        Ok(Response::new(GetPluginCapabilitiesResponse { capabilities: vec![svc] }))
    }

    async fn probe(&self, _: Request<ProbeRequest>) -> Result<Response<ProbeResponse>, Status> {
        let r = self.store.call("GET", "/-/ready", &[], vec![]).await?;
        Ok(Response::new(ProbeResponse { ready: Some(r.status == 200) }))
    }
}

#[tonic::async_trait]
impl controller_server::Controller for Arc<Driver> {
    async fn create_volume(&self, req: Request<CreateVolumeRequest>) -> Result<Response<CreateVolumeResponse>, Status> {
        let r = req.into_inner();
        if r.name.is_empty() {
            return Err(Status::invalid_argument("a volume name is required"));
        }
        check_caps(&r.volume_capabilities)?;
        if r.volume_content_source.is_some() {
            return Err(Status::invalid_argument("volumes cannot be created from snapshots or other volumes"));
        }
        let want = r.capacity_range.as_ref().map(|c| c.required_bytes.max(c.limit_bytes)).unwrap_or(0).max(0);
        let name = name_of(&r.name)?;
        let s = &self.store;
        let (bucket, prefix, capacity) = match r.parameters.get("bucket") {
            Some(shared) => {
                // A prefix of a shared bucket: its size is not enforced (a quota covers whole buckets).
                if !s.bucket_exists(shared).await? {
                    return Err(Status::failed_precondition(format!("the shared bucket {shared} does not exist")));
                }
                (shared.clone(), format!("{name}/"), 0)
            }
            None => {
                let r2 = s.call("PUT", &format!("/{}", aws_encode(name.as_bytes(), false)), &[], vec![]).await?;
                match r2.status {
                    200 => {}
                    409 => {
                        // Exists: ours (a retry) only if it carries this volume's marker.
                        if s.marker(&name, "").await?.as_deref() != Some(r.name.as_str()) {
                            return Err(Status::already_exists(format!("bucket {name} exists and is not this volume")));
                        }
                    }
                    _ => return Err(Store::fail("creating the volume's bucket", &r2)),
                }
                if want > 0 {
                    let body = serde_json::json!({"max_bytes": want}).to_string().into_bytes();
                    let r3 = s.call("PUT", &format!("/-/admin/v1/buckets/{}/quota", aws_encode(name.as_bytes(), false)), &[], body).await?;
                    if r3.status != 200 {
                        return Err(Store::fail("setting the volume's quota", &r3));
                    }
                }
                (name.clone(), String::new(), want)
            }
        };
        match s.marker(&bucket, &prefix).await? {
            Some(n) if n != r.name => return Err(Status::already_exists(format!("{bucket}/{prefix} belongs to volume {n}"))),
            Some(_) => {}
            None => s.put_marker(&bucket, &prefix, &r.name).await?,
        }
        let id = if prefix.is_empty() { bucket.clone() } else { format!("{bucket}/{}", prefix.trim_end_matches('/')) };
        tracing::info!(volume = %id, name = %r.name, bytes = capacity, "volume created");
        let ctx: HashMap<String, String> = [("bucket".to_string(), bucket), ("prefix".to_string(), prefix)].into();
        Ok(Response::new(CreateVolumeResponse {
            volume: Some(Volume { volume_id: id, capacity_bytes: capacity, volume_context: ctx, ..Default::default() }),
        }))
    }

    async fn delete_volume(&self, req: Request<DeleteVolumeRequest>) -> Result<Response<DeleteVolumeResponse>, Status> {
        let id = req.into_inner().volume_id;
        let (bucket, prefix) = match split_id(&id) {
            Ok(v) => v,
            Err(_) => return Ok(Response::new(DeleteVolumeResponse {})), // not ours: nothing to delete
        };
        let s = &self.store;
        if !s.bucket_exists(&bucket).await? || s.marker(&bucket, &prefix).await?.is_none() {
            return Ok(Response::new(DeleteVolumeResponse {})); // already gone (or never this driver's)
        }
        s.deletable(&bucket).await?;
        let n = s.delete_tree(&bucket, &prefix).await?;
        if prefix.is_empty() {
            let r = s.call("DELETE", &format!("/{}", aws_encode(bucket.as_bytes(), false)), &[], vec![]).await?;
            if r.status >= 300 && r.status != 404 {
                return Err(Store::fail("deleting the volume's bucket", &r));
            }
        }
        tracing::info!(volume = %id, objects = n, "volume deleted");
        Ok(Response::new(DeleteVolumeResponse {}))
    }

    async fn controller_publish_volume(
        &self,
        _: Request<ControllerPublishVolumeRequest>,
    ) -> Result<Response<ControllerPublishVolumeResponse>, Status> {
        Err(Status::unimplemented("no attach step: volumes are mounted on the node"))
    }

    async fn controller_unpublish_volume(
        &self,
        _: Request<ControllerUnpublishVolumeRequest>,
    ) -> Result<Response<ControllerUnpublishVolumeResponse>, Status> {
        Err(Status::unimplemented("no attach step: volumes are mounted on the node"))
    }

    async fn validate_volume_capabilities(
        &self,
        req: Request<ValidateVolumeCapabilitiesRequest>,
    ) -> Result<Response<ValidateVolumeCapabilitiesResponse>, Status> {
        let r = req.into_inner();
        let (bucket, prefix) = split_id(&r.volume_id)?;
        if self.store.marker(&bucket, &prefix).await?.is_none() {
            return Err(Status::not_found(format!("no volume {}", r.volume_id)));
        }
        if check_caps(&r.volume_capabilities).is_err() {
            return Ok(Response::new(ValidateVolumeCapabilitiesResponse {
                message: "block volumes are not supported".into(),
                ..Default::default()
            }));
        }
        Ok(Response::new(ValidateVolumeCapabilitiesResponse {
            confirmed: Some(validate_volume_capabilities_response::Confirmed {
                volume_capabilities: r.volume_capabilities,
                volume_context: r.volume_context,
                ..Default::default()
            }),
            ..Default::default()
        }))
    }

    async fn list_volumes(&self, _: Request<ListVolumesRequest>) -> Result<Response<ListVolumesResponse>, Status> {
        Err(Status::unimplemented("ListVolumes"))
    }

    async fn get_capacity(&self, _: Request<GetCapacityRequest>) -> Result<Response<GetCapacityResponse>, Status> {
        Err(Status::unimplemented("GetCapacity"))
    }

    async fn controller_get_capabilities(
        &self,
        _: Request<ControllerGetCapabilitiesRequest>,
    ) -> Result<Response<ControllerGetCapabilitiesResponse>, Status> {
        let cap = ControllerServiceCapability {
            r#type: Some(controller_service_capability::Type::Rpc(controller_service_capability::Rpc {
                r#type: controller_service_capability::rpc::Type::CreateDeleteVolume as i32,
            })),
        };
        Ok(Response::new(ControllerGetCapabilitiesResponse { capabilities: vec![cap] }))
    }

    async fn create_snapshot(&self, _: Request<CreateSnapshotRequest>) -> Result<Response<CreateSnapshotResponse>, Status> {
        Err(Status::unimplemented("snapshots"))
    }

    async fn delete_snapshot(&self, _: Request<DeleteSnapshotRequest>) -> Result<Response<DeleteSnapshotResponse>, Status> {
        Err(Status::unimplemented("snapshots"))
    }

    async fn list_snapshots(&self, _: Request<ListSnapshotsRequest>) -> Result<Response<ListSnapshotsResponse>, Status> {
        Err(Status::unimplemented("snapshots"))
    }

    async fn controller_expand_volume(
        &self,
        _: Request<ControllerExpandVolumeRequest>,
    ) -> Result<Response<ControllerExpandVolumeResponse>, Status> {
        Err(Status::unimplemented("expansion"))
    }

    async fn controller_get_volume(&self, _: Request<ControllerGetVolumeRequest>) -> Result<Response<ControllerGetVolumeResponse>, Status> {
        Err(Status::unimplemented("ControllerGetVolume"))
    }

    async fn controller_modify_volume(
        &self,
        _: Request<ControllerModifyVolumeRequest>,
    ) -> Result<Response<ControllerModifyVolumeResponse>, Status> {
        Err(Status::unimplemented("ControllerModifyVolume"))
    }
}

#[tonic::async_trait]
impl node_server::Node for Arc<Driver> {
    async fn node_stage_volume(&self, _: Request<NodeStageVolumeRequest>) -> Result<Response<NodeStageVolumeResponse>, Status> {
        Err(Status::unimplemented("no staging step"))
    }

    async fn node_unstage_volume(&self, _: Request<NodeUnstageVolumeRequest>) -> Result<Response<NodeUnstageVolumeResponse>, Status> {
        Err(Status::unimplemented("no staging step"))
    }

    async fn node_publish_volume(&self, req: Request<NodePublishVolumeRequest>) -> Result<Response<NodePublishVolumeResponse>, Status> {
        let r = req.into_inner();
        if r.target_path.is_empty() || !r.target_path.starts_with('/') {
            return Err(Status::invalid_argument("an absolute target path is required"));
        }
        let cap = r.volume_capability.ok_or_else(|| Status::invalid_argument("a volume capability is required"))?;
        check_caps(std::slice::from_ref(&cap))?;
        let (bucket, prefix) = split_id(&r.volume_id)?;
        if mounted(&r.target_path) {
            return Ok(Response::new(NodePublishVolumeResponse {})); // already published
        }
        std::fs::create_dir_all(&r.target_path).map_err(|e| Status::internal(format!("{}: {e}", r.target_path)))?;
        let mut args =
            vec!["mount".to_string(), format!("{bucket}/{}", prefix.trim_end_matches('/')), r.target_path.clone(), "--allow-other".into()];
        let read_only = r.readonly
            || matches!(
                volume_capability::access_mode::Mode::try_from(cap.access_mode.map(|m| m.mode).unwrap_or(0)),
                Ok(volume_capability::access_mode::Mode::SingleNodeReaderOnly)
                    | Ok(volume_capability::access_mode::Mode::MultiNodeReaderOnly)
            );
        if read_only {
            args.push("--read-only".into());
        }
        if let Some(volume_capability::AccessType::Mount(m)) = &cap.access_type {
            for f in &m.mount_flags {
                for opt in f.split(',') {
                    match opt.split_once('=') {
                        Some(("uid", v)) | Some(("gid", v)) if v.parse::<u32>().is_ok() => args.push(format!("--{opt}")),
                        _ if opt == "ro" => args.push("--read-only".into()),
                        _ => return Err(Status::invalid_argument(format!("unsupported mount flag {opt:?} (uid=N, gid=N, ro)"))),
                    }
                }
            }
        }
        let (ak, sk) = match (r.secrets.get("access_key"), r.secrets.get("secret_key")) {
            (Some(a), Some(s)) => (a.clone(), s.clone()),
            (None, None) => (self.store.ak.clone(), self.store.sk.clone()),
            _ => return Err(Status::invalid_argument("the node-publish secret needs both access_key and secret_key")),
        };
        let mut child = std::process::Command::new(&self.exe)
            .args(&args)
            .env("CAIRN_STORE_ENDPOINT", &self.store.endpoint)
            .env("CAIRN_STORE_ACCESS_KEY", ak)
            .env("CAIRN_STORE_SECRET_KEY", sk)
            .env("CAIRN_STORE_REGION", &self.store.region)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .map_err(|e| Status::internal(format!("starting the mount: {e}")))?;
        for _ in 0..150 {
            if mounted(&r.target_path) {
                tracing::info!(volume = %r.volume_id, target = %r.target_path, read_only, "volume published");
                self.mounts.lock().unwrap().insert(r.target_path.clone(), child);
                return Ok(Response::new(NodePublishVolumeResponse {}));
            }
            if let Ok(Some(_)) = child.try_wait() {
                let mut err = String::new();
                if let Some(mut e) = child.stderr.take() {
                    use std::io::Read as _;
                    let _ = e.read_to_string(&mut err);
                }
                return Err(Status::internal(format!("the mount failed: {}", err.trim())));
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let _ = child.kill();
        Err(Status::deadline_exceeded("the mount did not come up"))
    }

    async fn node_unpublish_volume(
        &self,
        req: Request<NodeUnpublishVolumeRequest>,
    ) -> Result<Response<NodeUnpublishVolumeResponse>, Status> {
        let r = req.into_inner();
        if mounted(&r.target_path) {
            let ok = ["fusermount3", "fusermount", "umount"].iter().any(|cmd| {
                std::process::Command::new(cmd)
                    .args(if *cmd == "umount" { vec![] } else { vec!["-u"] })
                    .arg(&r.target_path)
                    .status()
                    .is_ok_and(|s| s.success())
            });
            if !ok && mounted(&r.target_path) {
                return Err(Status::internal(format!("could not unmount {}", r.target_path)));
            }
        }
        if let Some(mut c) = self.mounts.lock().unwrap().remove(&r.target_path) {
            for _ in 0..50 {
                if matches!(c.try_wait(), Ok(Some(_))) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = c.kill();
            let _ = c.wait();
        }
        let _ = std::fs::remove_dir(&r.target_path);
        tracing::info!(target = %r.target_path, "volume unpublished");
        Ok(Response::new(NodeUnpublishVolumeResponse {}))
    }

    async fn node_get_volume_stats(&self, _: Request<NodeGetVolumeStatsRequest>) -> Result<Response<NodeGetVolumeStatsResponse>, Status> {
        Err(Status::unimplemented("NodeGetVolumeStats"))
    }

    async fn node_expand_volume(&self, _: Request<NodeExpandVolumeRequest>) -> Result<Response<NodeExpandVolumeResponse>, Status> {
        Err(Status::unimplemented("expansion"))
    }

    async fn node_get_capabilities(&self, _: Request<NodeGetCapabilitiesRequest>) -> Result<Response<NodeGetCapabilitiesResponse>, Status> {
        Ok(Response::new(NodeGetCapabilitiesResponse { capabilities: vec![] }))
    }

    async fn node_get_info(&self, _: Request<NodeGetInfoRequest>) -> Result<Response<NodeGetInfoResponse>, Status> {
        Ok(Response::new(NodeGetInfoResponse { node_id: self.node_id.clone(), ..Default::default() }))
    }
}

/// Runs the driver: `cairn-store csi` (endpoint, key and region come from the environment).
pub fn run(endpoint: &str, ak: &str, sk: &str, region: &str) -> i32 {
    let sock = std::env::var("CAIRN_STORE_CSI_ENDPOINT").unwrap_or_else(|_| "unix:///csi/csi.sock".into());
    let Some(path) = sock.strip_prefix("unix://") else {
        eprintln!("cairn-store csi: CAIRN_STORE_CSI_ENDPOINT must be unix:///path/to/socket");
        return 2;
    };
    let node_id = std::env::var("CAIRN_STORE_CSI_NODE_ID")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| std::fs::read_to_string("/etc/hostname").map(|h| h.trim().to_string()).unwrap_or_else(|_| "node".into()));
    if ak.is_empty() || sk.is_empty() {
        eprintln!("cairn-store csi: CAIRN_STORE_ACCESS_KEY and CAIRN_STORE_SECRET_KEY are required (a key that may create buckets)");
        return 2;
    }
    let rt = match tokio::runtime::Builder::new_multi_thread().enable_all().build() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("cairn-store csi: {e}");
            return 1;
        }
    };
    rt.block_on(async {
        let conn = match crate::tls::https_connector() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("cairn-store csi: {e}");
                return 1;
            }
        };
        let store = Arc::new(Store {
            endpoint: endpoint.trim_end_matches('/').to_string(),
            ak: ak.into(),
            sk: sk.into(),
            region: region.into(),
            http: Client::builder(TokioExecutor::new()).build(conn),
        });
        let exe = std::env::current_exe().unwrap_or_else(|_| "cairn-store".into());
        let d = Arc::new(Driver { store, node_id: node_id.clone(), exe, mounts: Mutex::new(HashMap::new()) });
        let _ = std::fs::remove_file(path);
        if let Some(dir) = std::path::Path::new(path).parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let listener = match tokio::net::UnixListener::bind(path) {
            Ok(l) => l,
            Err(e) => {
                eprintln!("cairn-store csi: {path}: {e}");
                return 1;
            }
        };
        tracing::info!(socket = path, node = %node_id, driver = DRIVER, "CSI driver listening");
        let incoming = tokio_stream::wrappers::UnixListenerStream::new(listener);
        let res = tonic::transport::Server::builder()
            .add_service(identity_server::IdentityServer::new(d.clone()))
            .add_service(controller_server::ControllerServer::new(d.clone()))
            .add_service(node_server::NodeServer::new(d.clone()))
            .serve_with_incoming_shutdown(incoming, async {
                let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
                tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
            })
            .await;
        match res {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("cairn-store csi: {e}");
                1
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_ids() {
        assert_eq!(name_of("pvc-0A1b2c3d-1111-2222").unwrap(), "pvc-0a1b2c3d-1111-2222");
        assert_eq!(name_of("My_Volume.1").unwrap(), "my-volume-1");
        assert!(name_of("__").is_err());
        assert_eq!(split_id("vols/pvc-1").unwrap(), ("vols".into(), "pvc-1/".into()));
        assert_eq!(split_id("pvc-1").unwrap(), ("pvc-1".into(), String::new()));
        assert!(split_id("BAD").is_err());
    }
}
