//! Clustering: several store nodes share one PostgreSQL metadata database and keep each blob's bytes on
//! more than one node's disk, as whole copies or as erasure-coded shards.
//!
//! * Every node serves the full S3 API: the index is shared, so any node sees every write at once.
//! * Writing a blob places it before the index entry that refers to it is committed: `COPIES` whole copies
//!   on distinct live nodes (this node first), or, for blobs of at least `EC_ABOVE` bytes when `EC=k+m` is
//!   set, k data and m parity shards (Reed-Solomon) on k+m distinct nodes. Too few live nodes: the write
//!   fails with 503 (never with fewer copies than configured).
//! * Reads take the nearest copy (this node's disk first) and fail over to the others, mid-stream too;
//!   erasure-coded reads take the data shards and reconstruct from parity when shards are missing.
//! * Nodes heartbeat into the shared `nodes` table. One node at a time (a lease) runs the cluster's
//!   background work: event delivery, lifecycle, deleting collected blobs, and repair, which re-creates
//!   copies and shards that are on dead, draining or removed nodes, then swaps the blob's id in the index
//!   in one transaction (only where it is unchanged) and deletes the copies it replaced.
//! * Nodes are drained (their data moves off), activated or removed through the admin API; rebalancing
//!   moves copies from the fullest nodes to the emptiest.
//!
//! Nodes talk over an internal API under `/-/cluster/v1/`, authenticated with an HMAC of each request
//! under the cluster secret (`x-cairn-cluster: <node> <unix seconds> <hex mac>`, five minutes of clock skew
//! allowed). Use HTTPS between nodes on untrusted networks (the MAC covers the request line, not bodies).

use std::collections::{HashMap, HashSet};
use std::io::{self, Read};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant};

use bytes::Bytes;
use hmac::{Hmac, Mac};
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::Frame;
use hyper::{Method, Request, StatusCode};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use rand::seq::SliceRandom;
use reed_solomon_erasure::galois_8::ReedSolomon;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::Sha256;

use super::{full, set_header, Body, Resp, Server};
use crate::engine::cid::{self, Copy, Placement};
use crate::engine::{self, ClusterStore, Engine, Segment};

/// Unreferenced local blobs younger than this may be copies another node has not committed yet.
const MAX_SKEW_SECS: i64 = 300;

#[derive(Clone, Debug)]
pub struct ClusterConfig {
    pub node: String,
    /// This node's address for the other nodes (http(s)://host:port).
    pub url: String,
    pub secret: String,
    pub copies: usize,
    /// Erasure coding (data, parity shards) for blobs of at least `ec_above` stored bytes.
    pub ec: Option<(usize, usize)>,
    pub ec_above: u64,
    /// Replicated blobs of at least `ec_convert_above` bytes whose object is older than this are
    /// re-encoded as shards (None: never).
    pub ec_after: Option<Duration>,
    pub ec_convert_above: u64,
    pub heartbeat: Duration,
    pub dead_after: Duration,
    pub repair_every: Duration,
    /// Where this node is: its zone (a data center or availability zone) and rack (or host).
    pub zone: String,
    pub rack: String,
    /// The failure domain copies must not share: "node" (default), "rack" or "zone".
    pub spread: String,
}

impl ClusterConfig {
    pub fn validate(&self) -> Result<(), String> {
        let name_ok = !self.node.is_empty()
            && self.node.len() <= 63
            && self.node.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
        if !name_ok {
            return Err("CAIRN_STORE_CLUSTER_NODE: 1-63 lowercase letters, digits and -".into());
        }
        if !self.url.starts_with("http://") && !self.url.starts_with("https://") {
            return Err("CAIRN_STORE_CLUSTER_URL: this node's http(s):// address for the other nodes".into());
        }
        if self.secret.len() < 16 {
            return Err("CAIRN_STORE_CLUSTER_SECRET: at least 16 characters, the same on every node".into());
        }
        if !(1..=9).contains(&self.copies) {
            return Err("CAIRN_STORE_CLUSTER_COPIES: 1 to 9".into());
        }
        if let Some((k, m)) = self.ec {
            if k < 2 || m < 1 || k + m > 32 {
                return Err("CAIRN_STORE_CLUSTER_EC: k+m with k >= 2, m >= 1 and k+m <= 32 (e.g. 4+2)".into());
            }
        }
        let label_ok = |v: &str| v.len() <= 63 && v.chars().all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c));
        if !label_ok(&self.zone) || !label_ok(&self.rack) {
            return Err("CAIRN_STORE_CLUSTER_ZONE / _RACK: up to 63 letters, digits, '-', '_' and '.'".into());
        }
        match self.spread.as_str() {
            "node" => {}
            "rack" if !self.rack.is_empty() => {}
            "zone" if !self.zone.is_empty() => {}
            "rack" | "zone" => {
                return Err(format!(
                    "CAIRN_STORE_CLUSTER_SPREAD={}: set this node's CAIRN_STORE_CLUSTER_{}",
                    self.spread,
                    self.spread.to_uppercase()
                ))
            }
            _ => return Err("CAIRN_STORE_CLUSTER_SPREAD: node, rack or zone".into()),
        }
        if self.ec_after.is_some() && self.ec.is_none() {
            return Err("CAIRN_STORE_CLUSTER_EC_AFTER needs CAIRN_STORE_CLUSTER_EC (the k+m to convert to)".into());
        }
        if self.dead_after <= self.heartbeat * 2 {
            return Err("CAIRN_STORE_CLUSTER_DEAD_AFTER must be more than twice the heartbeat".into());
        }
        Ok(())
    }
}

/// "4+2" -> (4, 2).
pub fn parse_ec(v: &str) -> Result<Option<(usize, usize)>, String> {
    if v.is_empty() || v == "off" {
        return Ok(None);
    }
    let (k, m) = v.split_once('+').ok_or("CAIRN_STORE_CLUSTER_EC: k+m, e.g. 4+2")?;
    Ok(Some((k.trim().parse().map_err(|_| "CAIRN_STORE_CLUSTER_EC: k+m")?, m.trim().parse().map_err(|_| "CAIRN_STORE_CLUSTER_EC: k+m")?)))
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct NodeRec {
    pub url: String,
    /// active, draining (its data moves off; nothing new is placed there) or removed.
    pub state: String,
    pub heartbeat: u64,
    pub started: u64,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub zone: String,
    #[serde(default)]
    pub rack: String,
}

#[derive(Default)]
pub struct ClusterMetrics {
    pub placed: AtomicU64,
    pub placed_ec: AtomicU64,
    pub place_failed: AtomicU64,
    pub remote_reads: AtomicU64,
    pub failovers: AtomicU64,
    pub reconstructions: AtomicU64,
    pub repaired: AtomicU64,
    pub moved: AtomicU64,
    pub deleted: AtomicU64,
}

#[derive(Debug, Default, Serialize)]
pub struct RepairReport {
    pub blobs: u64,
    pub healthy: u64,
    pub repaired: u64,
    /// Fewer copies or shards than configured, and no node to put more on.
    pub degraded: u64,
    /// No readable copy, or fewer than k readable shards.
    pub lost: u64,
    pub failed: u64,
    pub copies_deleted: u64,
    pub adopted: u64,
}

#[derive(Debug, Default, Serialize)]
pub struct EncodeReport {
    pub candidates: u64,
    pub converted: u64,
    /// Bytes the copies took, and the shards take now.
    pub bytes_before: u64,
    pub bytes_after: u64,
    pub failed: u64,
}

#[derive(Debug, Default, Serialize)]
pub struct RebalanceReport {
    pub moved: u64,
    pub bytes: u64,
    pub before: HashMap<String, u64>,
    pub after: HashMap<String, u64>,
}

type Https = hyper_rustls::HttpsConnector<HttpConnector>;
type NodeList = Vec<(String, NodeRec)>;

pub struct Cluster {
    pub cfg: ClusterConfig,
    eng: Arc<Engine>,
    me: Weak<Cluster>,
    http: Client<Https, Body>,
    rt: tokio::runtime::Handle,
    nodes: Mutex<Option<(Instant, NodeList)>>,
    leases: Mutex<HashMap<&'static str, Instant>>,
    pub metrics: ClusterMetrics,
    repairing: Mutex<()>,
}

fn io_err(e: impl std::fmt::Display) -> io::Error {
    io::Error::other(e.to_string())
}

fn empty() -> Body {
    Full::new(Bytes::new()).map_err(|never| match never {}).boxed()
}

fn mac(secret: &str, method: &str, path: &str, ts: i64, node: &str) -> String {
    let mut m = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("hmac key");
    m.update(format!("{method}\n{path}\n{ts}\n{node}").as_bytes());
    hex::encode(m.finalize().into_bytes())
}

/// A local blob id as it may appear in a URL path (hex ids; anything else is refused by the engine).
fn path_id(id: &str) -> Option<&str> {
    (!id.is_empty() && id.len() <= 128 && id.chars().all(|c| c.is_ascii_alphanumeric())).then_some(id)
}

impl Cluster {
    pub fn new(cfg: ClusterConfig, eng: Arc<Engine>) -> Result<Arc<Cluster>, String> {
        cfg.validate()?;
        let http = Client::builder(TokioExecutor::new()).build(crate::tls::https_connector()?);
        let rt = tokio::runtime::Handle::current();
        let c = Arc::new_cyclic(|me| Cluster {
            cfg,
            eng: eng.clone(),
            me: me.clone(),
            http,
            rt,
            nodes: Mutex::new(None),
            leases: Mutex::new(HashMap::new()),
            metrics: Default::default(),
            repairing: Mutex::new(()),
        });
        c.register()?;
        eng.set_cluster(c.clone());
        Ok(c)
    }

    fn name(&self) -> &str {
        &self.cfg.node
    }

    /// Joins (or rejoins) the cluster: refuses to start as a node that was removed.
    fn register(&self) -> Result<(), String> {
        let now = engine::now_ms();
        let url = self.cfg.url.clone();
        let mut removed = false;
        self.eng
            .update_node(
                self.name(),
                |cur| {
                    let prev: Option<NodeRec> = cur.and_then(|c| serde_json::from_slice(c).ok());
                    if prev.as_ref().is_some_and(|p| p.state == "removed") {
                        removed = true;
                        return None;
                    }
                    let state = prev.map(|p| p.state).unwrap_or_else(|| "active".into());
                    let rec = NodeRec {
                        url,
                        state,
                        heartbeat: now,
                        started: now,
                        version: crate::VERSION.into(),
                        zone: self.cfg.zone.clone(),
                        rack: self.cfg.rack.clone(),
                    };
                    Some(serde_json::to_vec(&rec).unwrap())
                },
                None,
            )
            .map_err(|e| e.to_string())?;
        if removed {
            return Err(format!(
                "node {} was removed from the cluster; activate it through the admin API (cluster-activate) to let it rejoin",
                self.name()
            ));
        }
        *self.nodes.lock().unwrap() = None;
        Ok(())
    }

    /// Updates this node's heartbeat (keeping its state). Err: this node was removed.
    pub fn heartbeat(&self) -> Result<(), String> {
        let now = engine::now_ms();
        let url = self.cfg.url.clone();
        let mut removed = false;
        self.eng
            .update_node(
                self.name(),
                |cur| {
                    let mut rec: NodeRec = cur.and_then(|c| serde_json::from_slice(c).ok()).unwrap_or(NodeRec {
                        url: url.clone(),
                        state: "active".into(),
                        heartbeat: now,
                        started: now,
                        version: crate::VERSION.into(),
                        zone: String::new(),
                        rack: String::new(),
                    });
                    if rec.state == "removed" {
                        removed = true;
                        return None;
                    }
                    rec.url = url;
                    rec.heartbeat = now;
                    rec.zone = self.cfg.zone.clone();
                    rec.rack = self.cfg.rack.clone();
                    Some(serde_json::to_vec(&rec).unwrap())
                },
                None,
            )
            .map_err(|e| e.to_string())?;
        *self.nodes.lock().unwrap() = None;
        if removed {
            return Err(format!("node {} was removed from the cluster", self.name()));
        }
        Ok(())
    }

    /// Every node's record (cached for a second).
    pub fn all_nodes(&self) -> Vec<(String, NodeRec)> {
        let mut g = self.nodes.lock().unwrap();
        if let Some((at, n)) = g.as_ref() {
            if at.elapsed() < Duration::from_secs(1) {
                return n.clone();
            }
        }
        let n: Vec<(String, NodeRec)> =
            self.eng.nodes().unwrap_or_default().into_iter().filter_map(|(k, v)| serde_json::from_slice(&v).ok().map(|r| (k, r))).collect();
        *g = Some((Instant::now(), n.clone()));
        n
    }

    fn alive(&self, r: &NodeRec) -> bool {
        engine::now_ms().saturating_sub(r.heartbeat) < self.cfg.dead_after.as_millis() as u64
    }

    /// Nodes new data may go to: active and alive, this node first, the others shuffled.
    fn targets(&self) -> Vec<(String, NodeRec)> {
        let mut t: Vec<(String, NodeRec)> = self.all_nodes().into_iter().filter(|(_, r)| r.state == "active" && self.alive(r)).collect();
        t.shuffle(&mut rand::thread_rng());
        t.sort_by_key(|(n, _)| n != self.name());
        t
    }

    fn node(&self, name: &str) -> Option<NodeRec> {
        self.all_nodes().into_iter().find(|(n, _)| n == name).map(|(_, r)| r)
    }

    // ------------------------------------------------------------------------------- topology

    /// A node's failure domain at the configured spread level.
    fn domain(&self, name: &str) -> String {
        let r = self.node(name);
        let (zone, rack) = r.map(|r| (r.zone, r.rack)).unwrap_or_default();
        match self.cfg.spread.as_str() {
            "zone" => format!("z:{zone}"),
            "rack" => format!("r:{zone}/{rack}"),
            _ => format!("n:{name}"),
        }
    }

    fn strict(&self) -> bool {
        self.cfg.spread != "node"
    }

    /// Orders candidate nodes so that each next one shares as few zones, then racks, as possible with
    /// the nodes already `taken` and those picked before it (ties keep the given order: this node
    /// first, then shuffled).
    fn diverse(&self, cands: Vec<String>, taken: &[String]) -> Vec<String> {
        let all: HashMap<String, NodeRec> = self.all_nodes().into_iter().collect();
        let loc = |n: &str| all.get(n).map(|r| (r.zone.clone(), format!("{}/{}", r.zone, r.rack))).unwrap_or_default();
        let mut zones: HashMap<String, usize> = HashMap::new();
        let mut racks: HashMap<String, usize> = HashMap::new();
        for t in taken {
            let (z, r) = loc(t);
            *zones.entry(z).or_default() += 1;
            *racks.entry(r).or_default() += 1;
        }
        let mut left = cands;
        let mut out = vec![];
        while !left.is_empty() {
            let (i, _) = left
                .iter()
                .enumerate()
                .min_by_key(|(i, n)| {
                    let (z, r) = loc(n);
                    (zones.get(&z).copied().unwrap_or(0), racks.get(&r).copied().unwrap_or(0), *i)
                })
                .unwrap();
            let n = left.remove(i);
            let (z, r) = loc(&n);
            *zones.entry(z).or_default() += 1;
            *racks.entry(r).or_default() += 1;
            out.push(n);
        }
        out
    }

    /// Whether holders respect the spread rule: copies in distinct domains; erasure-coded shards at
    /// most `m` per domain (so losing one domain loses no data). Always true at the "node" level.
    fn spread_ok(&self, holders: &[String], ec_m: Option<usize>) -> bool {
        let mut n: HashMap<String, usize> = HashMap::new();
        for h in holders {
            *n.entry(self.domain(h)).or_default() += 1;
        }
        let max = ec_m.unwrap_or(1);
        n.values().all(|c| *c <= max)
    }

    /// Holds (or takes) the cluster-wide lease for `task`. Cached: renewed every third of its lifetime.
    pub fn lead(&self, task: &'static str) -> bool {
        let ttl = self.cfg.dead_after;
        {
            let g = self.leases.lock().unwrap();
            if g.get(task).is_some_and(|at| at.elapsed() < ttl / 3) {
                return true;
            }
        }
        let held = self.eng.try_lease(task, self.name(), ttl.as_millis() as u64).unwrap_or(false);
        let mut g = self.leases.lock().unwrap();
        if held {
            g.insert(task, Instant::now());
        } else {
            g.remove(task);
        }
        held
    }

    // ------------------------------------------------------------------------------- the wire

    async fn send(&self, url: &str, method: Method, path: &str, body: Body) -> io::Result<hyper::Response<hyper::body::Incoming>> {
        let ts = time::OffsetDateTime::now_utc().unix_timestamp();
        let pure = path.split('?').next().unwrap_or("");
        let auth = format!("{} {ts} {}", self.name(), mac(&self.cfg.secret, method.as_str(), pure, ts, self.name()));
        let full_url = format!("{}{path}", url.trim_end_matches('/'));
        let req = Request::builder().method(method.clone()).uri(&full_url).header("x-cairn-cluster", auth).body(body).map_err(io_err)?;
        tokio::time::timeout(Duration::from_secs(60), self.http.request(req))
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, format!("{method} {full_url}: timed out")))?
            .map_err(|e| io::Error::new(io::ErrorKind::ConnectionRefused, format!("{method} {full_url}: {e}")))
    }

    async fn check(resp: hyper::Response<hyper::body::Incoming>, what: &str) -> io::Result<hyper::Response<hyper::body::Incoming>> {
        if resp.status().is_success() {
            return Ok(resp);
        }
        let status = resp.status();
        let body = resp.into_body().collect().await.map(|b| b.to_bytes()).unwrap_or_default();
        Err(io::Error::new(
            if status == StatusCode::NOT_FOUND { io::ErrorKind::NotFound } else { io::ErrorKind::Other },
            format!("{what}: HTTP {status}: {}", String::from_utf8_lossy(&body).chars().take(200).collect::<String>()),
        ))
    }

    /// Stores a stream on `node`; returns its local id there and the length written.
    async fn put_async(&self, node: &str, url: &str, body: Body, kind: Option<&str>) -> io::Result<(String, u64)> {
        let path = match kind {
            Some(k) => format!("/-/cluster/v1/blobs?disk={k}"),
            None => "/-/cluster/v1/blobs".to_string(),
        };
        let resp = self.send(url, Method::PUT, &path, body).await?;
        let resp = Self::check(resp, &format!("place a copy on {node}")).await?;
        let b = resp.into_body().collect().await.map_err(io_err)?.to_bytes();
        let v: Value = serde_json::from_slice(&b).map_err(io_err)?;
        match (v["id"].as_str(), v["len"].as_u64()) {
            (Some(id), Some(len)) if path_id(id).is_some() => Ok((id.to_string(), len)),
            _ => Err(io_err(format!("{node}: bad reply to a placement"))),
        }
    }

    /// Writes `reader` as a blob on `node` (this node: its own disk). Blocking.
    fn put_copy(&self, node: &str, reader: Box<dyn Read + Send>, kind: Option<&str>) -> io::Result<(String, u64)> {
        if node == self.name() {
            let mut w = self.eng.new_local_blob_on(kind).map_err(io_err)?;
            let mut r = reader;
            let mut buf = vec![0u8; 256 << 10];
            let mut n = 0u64;
            loop {
                let k = r.read(&mut buf)?;
                if k == 0 {
                    break;
                }
                w.write(&buf[..k])?;
                n += k as u64;
            }
            let seg = w.seal().map_err(io_err)?;
            return Ok((seg.blob, n));
        }
        let rec = self.node(node).ok_or_else(|| io_err(format!("unknown node {node}")))?;
        self.rt.block_on(self.put_async(node, &rec.url, super::tiering::file_body(reader), kind))
    }

    /// The disk type a copy or shard is on (None: unknown, e.g. its node is down).
    fn kind_of(&self, c: &Copy) -> Option<String> {
        if c.node == self.name() {
            return Some(self.eng.disk_kind_of(&c.local).to_string());
        }
        let rec = self.node(&c.node)?;
        let id = path_id(&c.local)?;
        self.rt
            .block_on(async {
                let resp = self.send(&rec.url, Method::GET, &format!("/-/cluster/v1/kind/{id}"), empty()).await?;
                let b = Self::check(resp, "disk type").await?.into_body().collect().await.map_err(io_err)?.to_bytes();
                serde_json::from_slice::<Value>(&b).map_err(io_err)
            })
            .ok()
            .and_then(|v| v["type"].as_str().map(str::to_string))
    }

    /// The disk type of a blob's first readable copy.
    fn kind_of_any(&self, cs: &[Copy]) -> Option<String> {
        cs.iter().find_map(|c| self.kind_of(c))
    }

    /// Reads one copy or shard from `offset` on. Blocking.
    fn open_copy(&self, c: &Copy, offset: u64) -> io::Result<Box<dyn Read + Send>> {
        if c.node == self.name() {
            return self.eng.open_local(&c.local, offset).map_err(|e| match e {
                engine::Error::Io(e) => e,
                other => io_err(other),
            });
        }
        let rec = self.node(&c.node).ok_or_else(|| io_err(format!("unknown node {}", c.node)))?;
        if rec.state == "removed" {
            return Err(io::Error::new(io::ErrorKind::NotFound, format!("node {} was removed", c.node)));
        }
        let id = path_id(&c.local).ok_or_else(|| io_err("bad local blob id"))?;
        self.metrics.remote_reads.fetch_add(1, Ordering::Relaxed);
        let resp = self.rt.block_on(async {
            let resp = self.send(&rec.url, Method::GET, &format!("/-/cluster/v1/blobs/{id}?offset={offset}"), empty()).await?;
            Self::check(resp, &format!("read a copy on {}", c.node)).await
        })?;
        Ok(Box::new(super::tiering::BodyReader::new(self.rt.clone(), resp.into_body())))
    }

    /// Deletes one copy or shard (a missing one, or one on a removed node, counts as deleted). Blocking.
    fn delete_copy(&self, c: &Copy) -> io::Result<()> {
        if c.node == self.name() {
            return self.eng.remove_local(&c.local).map_err(io_err);
        }
        let Some(rec) = self.node(&c.node) else { return Ok(()) };
        if rec.state == "removed" {
            return Ok(());
        }
        let Some(id) = path_id(&c.local) else { return Ok(()) };
        self.rt.block_on(async {
            let resp = self.send(&rec.url, Method::DELETE, &format!("/-/cluster/v1/blobs/{id}"), empty()).await?;
            match Self::check(resp, &format!("delete a copy on {}", c.node)).await {
                Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
                _ => Ok(()),
            }
        })?;
        self.metrics.deleted.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }

    fn me(&self) -> Arc<Cluster> {
        self.me.upgrade().expect("cluster")
    }

    // ------------------------------------------------------------------------------- placement

    fn place_replicas(&self, local: &Segment, len: u64, avoid: &HashSet<String>) -> engine::Result<Vec<Copy>> {
        let mut copies: Vec<Copy> = vec![];
        let mut errors = vec![];
        let cands: Vec<String> = self.targets().into_iter().map(|(n, _)| n).filter(|n| !avoid.contains(n)).collect();
        for name in self.diverse(cands, &[]) {
            if copies.len() >= self.cfg.copies {
                break;
            }
            if self.strict() && copies.iter().any(|c| self.domain(&c.node) == self.domain(&name)) {
                continue; // that failure domain already has a copy
            }
            if name == self.name() && !local.blob.is_empty() {
                copies.push(Copy { node: name, local: local.blob.clone() });
                continue;
            }
            let kind = self.eng.disk_kind_of(&local.blob).to_string();
            let res = self.eng.open_local(&local.blob, 0).map_err(io_err).and_then(|r| self.put_copy(&name, r, Some(&kind)));
            match res {
                Ok((id, n)) if n == len => copies.push(Copy { node: name, local: id }),
                Ok((id, _)) => {
                    let _ = self.delete_copy(&Copy { node: name.clone(), local: id });
                    errors.push(format!("{name}: short copy"));
                }
                Err(e) => errors.push(format!("{name}: {e}")),
            }
        }
        if copies.len() < self.cfg.copies {
            for c in copies.iter().filter(|c| c.local != local.blob) {
                let _ = self.delete_copy(c);
            }
            return Err(engine::Error::Unavailable(format!(
                "{} copies wanted in distinct {}s, {} could take one{}",
                self.cfg.copies,
                self.cfg.spread,
                copies.len(),
                if errors.is_empty() { String::new() } else { format!(" ({})", errors.join("; ")) }
            )));
        }
        Ok(copies)
    }

    /// Shard unit for a blob of `len` stored bytes: one stripe for small blobs, 1 MiB units for large ones.
    fn unit_for(len: u64, k: usize) -> u64 {
        len.div_ceil(k as u64).clamp(4096, 1 << 20)
    }

    /// Erasure-codes `reader` (`len` stored bytes) into k+m shards and writes the shards whose index is in
    /// `which` to the given nodes. Returns the written shards (index -> copy). Blocking.
    fn encode_to(
        &self,
        mut reader: Box<dyn Read + Send>,
        len: u64,
        (k, m): (usize, usize),
        unit: u64,
        which: &[(usize, String)],
        kind: Option<String>,
    ) -> io::Result<HashMap<usize, Copy>> {
        let rs = ReedSolomon::new(k, m).map_err(|e| io_err(format!("{e:?}")))?;
        let stripes = len.div_ceil(k as u64 * unit).max(1);
        let shard_len = stripes * unit;
        let me = self.me();
        self.rt.block_on(async move {
            // One sink per written shard: a streaming upload, or a blob on this disk.
            let mut remote = vec![];
            let mut senders: HashMap<usize, tokio::sync::mpsc::Sender<io::Result<Frame<Bytes>>>> = HashMap::new();
            let mut local: HashMap<usize, engine::BlobWriter<'_>> = HashMap::new();
            for (i, node) in which {
                if node == me.name() {
                    local.insert(*i, me.eng.new_local_blob_on(kind.as_deref()).map_err(io_err)?);
                    continue;
                }
                let rec = me.node(node).ok_or_else(|| io_err(format!("unknown node {node}")))?;
                let (tx, rx) = tokio::sync::mpsc::channel::<io::Result<Frame<Bytes>>>(2);
                senders.insert(*i, tx);
                let s = futures_util::stream::unfold(rx, |mut rx| async move { rx.recv().await.map(|item| (item, rx)) });
                let body: Body = BodyExt::boxed(StreamBody::new(s));
                let (c, node, url, i) = (me.clone(), node.clone(), rec.url.clone(), *i);
                let k = kind.clone();
                remote.push(tokio::spawn(async move { (i, node.clone(), c.put_async(&node, &url, body, k.as_deref()).await) }));
            }
            let mut buf = vec![0u8; (k as u64 * unit) as usize];
            let mut failure: Option<io::Error> = None;
            for _ in 0..stripes {
                buf.iter_mut().for_each(|b| *b = 0);
                let mut got = 0;
                while got < buf.len() {
                    let n = tokio::task::block_in_place(|| reader.read(&mut buf[got..]))?;
                    if n == 0 {
                        break;
                    }
                    got += n;
                }
                let mut shards: Vec<Vec<u8>> = buf.chunks(unit as usize).map(<[u8]>::to_vec).collect();
                shards.extend((0..m).map(|_| vec![0u8; unit as usize]));
                rs.encode(&mut shards).map_err(|e| io_err(format!("{e:?}")))?;
                for (i, w) in local.iter_mut() {
                    tokio::task::block_in_place(|| w.write(&shards[*i]))?;
                }
                for (i, tx) in &senders {
                    if tx.send(Ok(Frame::data(Bytes::from(std::mem::take(&mut shards[*i]))))).await.is_err() {
                        failure.get_or_insert_with(|| io_err(format!("shard {i}: upload ended early")));
                    }
                }
            }
            drop(senders);
            let mut out = HashMap::new();
            for (i, w) in local {
                let seg = tokio::task::block_in_place(|| w.seal()).map_err(io_err)?;
                out.insert(i, Copy { node: me.name().to_string(), local: seg.blob });
            }
            for h in remote {
                match h.await {
                    Ok((i, node, Ok((id, n)))) if n == shard_len => {
                        out.insert(i, Copy { node, local: id });
                    }
                    Ok((i, node, Ok((id, _)))) => {
                        let _ = tokio::task::block_in_place(|| me.delete_copy(&Copy { node, local: id }));
                        failure.get_or_insert_with(|| io_err(format!("shard {i}: short write")));
                    }
                    Ok((_, node, Err(e))) => {
                        failure.get_or_insert_with(|| io_err(format!("{node}: {e}")));
                    }
                    Err(e) => {
                        failure.get_or_insert_with(|| io_err(e));
                    }
                }
            }
            if let Some(e) = failure {
                for c in out.values() {
                    let _ = tokio::task::block_in_place(|| me.delete_copy(c));
                }
                return Err(e);
            }
            Ok(out)
        })
    }

    fn place_ec(&self, local: &Segment, len: u64, (k, m): (usize, usize)) -> engine::Result<Placement> {
        let targets = self.targets();
        if targets.len() < k + m {
            return Err(engine::Error::Unavailable(format!("erasure coding {k}+{m} needs {} live nodes, {} are up", k + m, targets.len())));
        }
        let unit = Self::unit_for(len, k);
        let mut chosen: Vec<String> = vec![];
        for n in self.diverse(targets.into_iter().map(|(n, _)| n).collect(), &[]) {
            if chosen.len() == k + m {
                break;
            }
            let mut with = chosen.clone();
            with.push(n.clone());
            if !self.strict() || self.spread_ok(&with, Some(m)) {
                chosen = with;
            }
        }
        if chosen.len() < k + m {
            return Err(engine::Error::Unavailable(format!(
                "erasure coding {k}+{m} with at most {m} shards per {} needs more {}s than are up",
                self.cfg.spread, self.cfg.spread
            )));
        }
        let which: Vec<(usize, String)> = chosen.into_iter().enumerate().collect();
        let reader = self.eng.open_local(&local.blob, 0).map_err(|e| engine::Error::Index(e.to_string()))?;
        let kind = Some(self.eng.disk_kind_of(&local.blob).to_string());
        let shards = self.encode_to(reader, len, (k, m), unit, &which, kind).map_err(|e| engine::Error::Unavailable(e.to_string()))?;
        let shards: Vec<Copy> = (0..k + m).map(|i| shards[&i].clone()).collect();
        Ok(Placement::Erasure { k, m, len, unit, shards })
    }
}

impl ClusterStore for Cluster {
    fn place(&self, eng: &Engine, local: &Segment) -> engine::Result<Segment> {
        let len = eng.blob_reader(&local.blob)?.1;
        let ec = self.cfg.ec.filter(|_| len >= self.cfg.ec_above);
        let placement = match ec {
            Some(km) => self.place_ec(local, len, km),
            None => self.place_replicas(local, len, &HashSet::new()).map(|copies| Placement::Replicas { len, copies }),
        };
        let placement = placement.inspect_err(|_| {
            self.metrics.place_failed.fetch_add(1, Ordering::Relaxed);
        })?;
        let keeps_local = placement.holders().iter().any(|c| c.node == self.name() && c.local == local.blob);
        if !keeps_local {
            let _ = eng.remove_local(&local.blob);
        }
        if matches!(placement, Placement::Erasure { .. }) {
            self.metrics.placed_ec.fetch_add(1, Ordering::Relaxed);
        } else {
            self.metrics.placed.fetch_add(1, Ordering::Relaxed);
        }
        Ok(Segment { blob: cid::format(&placement), ..local.clone() })
    }

    fn open(&self, id: &str, offset: u64) -> io::Result<Box<dyn Read + Send>> {
        let p = cid::parse(id).ok_or_else(|| io_err(format!("bad cluster blob id {id}")))?;
        match p {
            Placement::Replicas { len, copies } => {
                let mut order = copies;
                // This node first, then live nodes, then the rest.
                order.sort_by_key(|c| (c.node != self.name(), !self.node(&c.node).is_some_and(|r| self.alive(&r))));
                let mut r = Failover { c: self.me(), order, next: 0, cur: None, pos: offset, end: len };
                r.advance()?;
                Ok(Box::new(r))
            }
            Placement::Erasure { k, m, len, unit, shards } => {
                let stripe = offset / (k as u64 * unit);
                let mut r = EcReader {
                    c: self.me(),
                    rs: ReedSolomon::new(k, m).map_err(|e| io_err(format!("{e:?}")))?,
                    k,
                    len,
                    unit,
                    shards,
                    streams: (0..k + m).map(|_| None).collect(),
                    failed: HashSet::new(),
                    stripe,
                    buf: vec![],
                    off: 0,
                };
                r.load()?;
                r.off = (offset - stripe * k as u64 * unit) as usize;
                Ok(Box::new(r))
            }
        }
    }

    fn delete(&self, id: &str) -> io::Result<()> {
        let p = cid::parse(id).ok_or_else(|| io_err(format!("bad cluster blob id {id}")))?;
        let mut first: Option<io::Error> = None;
        for c in p.holders() {
            if let Err(e) = self.delete_copy(c) {
                first.get_or_insert(e);
            }
        }
        first.map_or(Ok(()), Err)
    }
}

/// Reads a replicated blob, moving to the next copy when one fails (also mid-stream).
struct Failover {
    c: Arc<Cluster>,
    order: Vec<Copy>,
    next: usize,
    cur: Option<Box<dyn Read + Send>>,
    pos: u64,
    end: u64,
}

impl Failover {
    fn advance(&mut self) -> io::Result<()> {
        let mut last = None;
        while self.next < self.order.len() {
            let c = self.order[self.next].clone();
            self.next += 1;
            match self.c.open_copy(&c, self.pos) {
                Ok(r) => {
                    if self.next > 1 {
                        self.c.metrics.failovers.fetch_add(1, Ordering::Relaxed);
                    }
                    self.cur = Some(r);
                    return Ok(());
                }
                Err(e) => {
                    tracing::debug!(node = %c.node, error = %e, "copy unreadable; trying the next");
                    last = Some(e);
                }
            }
        }
        self.cur = None;
        Err(last.unwrap_or_else(|| io_err("no copy of this blob is readable")))
    }
}

impl Read for Failover {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        loop {
            let Some(r) = self.cur.as_mut() else { return Err(io_err("no copy of this blob is readable")) };
            match r.read(out) {
                Ok(0) if self.pos < self.end => {
                    tracing::warn!(pos = self.pos, end = self.end, "copy ended early; trying the next");
                    self.advance()?;
                }
                Ok(n) => {
                    self.pos += n as u64;
                    return Ok(n);
                }
                Err(e) => {
                    tracing::warn!(error = %e, "copy read failed; trying the next");
                    self.advance()?;
                }
            }
        }
    }
}

/// Reads an erasure-coded blob stripe by stripe: from the data shards, reconstructing from parity when
/// shards are missing or fail.
struct EcReader {
    c: Arc<Cluster>,
    rs: ReedSolomon,
    k: usize,
    len: u64,
    unit: u64,
    shards: Vec<Copy>,
    /// Open streams, each positioned at the start of the current stripe's unit.
    streams: Vec<Option<Box<dyn Read + Send>>>,
    failed: HashSet<usize>,
    stripe: u64,
    buf: Vec<u8>,
    off: usize,
}

impl EcReader {
    /// Loads the current stripe's data into `buf`.
    fn load(&mut self) -> io::Result<()> {
        let n = self.shards.len();
        let unit = self.unit as usize;
        let mut got: Vec<Option<Vec<u8>>> = vec![None; n];
        let mut have = 0;
        // Data shards first; parity only to stand in for missing ones.
        #[allow(clippy::needless_range_loop)]
        for i in 0..n {
            if have == self.k {
                break;
            }
            if self.failed.contains(&i) {
                continue;
            }
            if self.streams[i].is_none() {
                match self.c.open_copy(&self.shards[i], self.stripe * self.unit) {
                    Ok(r) => self.streams[i] = Some(r),
                    Err(e) => {
                        tracing::debug!(shard = i, error = %e, "shard unreadable");
                        self.failed.insert(i);
                        continue;
                    }
                }
            }
            let mut b = vec![0u8; unit];
            match self.streams[i].as_mut().unwrap().read_exact(&mut b) {
                Ok(()) => {
                    got[i] = Some(b);
                    have += 1;
                }
                Err(e) => {
                    tracing::warn!(shard = i, error = %e, "shard read failed");
                    self.failed.insert(i);
                    self.streams[i] = None;
                }
            }
        }
        // Streams not read in this stripe are behind: reopen them when needed.
        for (i, g) in got.iter().enumerate() {
            if g.is_none() {
                self.streams[i] = None;
            }
        }
        if have < self.k {
            return Err(io_err(format!("only {have} of the {} shards needed are readable", self.k)));
        }
        if got[..self.k].iter().any(Option::is_none) {
            self.rs.reconstruct_data(&mut got).map_err(|e| io_err(format!("{e:?}")))?;
            self.c.metrics.reconstructions.fetch_add(1, Ordering::Relaxed);
        }
        let start = self.stripe * self.k as u64 * self.unit;
        let take = (self.len.saturating_sub(start)).min(self.k as u64 * self.unit) as usize;
        let mut buf = Vec::with_capacity(self.k * unit);
        for g in got.into_iter().take(self.k) {
            buf.extend_from_slice(&g.unwrap());
        }
        buf.truncate(take);
        self.buf = buf;
        self.off = 0;
        Ok(())
    }
}

impl Read for EcReader {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        if self.off >= self.buf.len() {
            let next = (self.stripe + 1) * self.k as u64 * self.unit;
            if next >= self.len {
                return Ok(0);
            }
            self.stripe += 1;
            self.load()?;
        }
        let n = out.len().min(self.buf.len() - self.off);
        out[..n].copy_from_slice(&self.buf[self.off..self.off + n]);
        self.off += n;
        Ok(n)
    }
}

// ----------------------------------------------------------------------------------- repair

impl Cluster {
    /// What a node is to repair: `good` nodes hold data for good; `readable` ones can still be read.
    fn health(&self) -> (HashSet<String>, HashSet<String>) {
        let mut good = HashSet::new();
        let mut readable = HashSet::new();
        for (n, r) in self.all_nodes() {
            if self.alive(&r) && r.state != "removed" {
                readable.insert(n.clone());
                if r.state == "active" {
                    good.insert(n);
                }
            }
        }
        (good, readable)
    }

    /// Re-creates copies and shards on dead, draining or removed nodes (`dry`: only count). One pass,
    /// run by the lease holder (or an admin).
    pub fn repair(&self, dry: bool) -> RepairReport {
        let _one = self.repairing.lock().unwrap();
        *self.nodes.lock().unwrap() = None;
        let mut rep = RepairReport::default();
        let (good, readable) = self.health();
        let ids: HashSet<String> = self.eng.blob_ids().unwrap_or_default().into_iter().filter(|i| cid::is_cluster(i)).collect();
        for id in ids {
            rep.blobs += 1;
            let Some(p) = cid::parse(&id) else {
                rep.failed += 1;
                continue;
            };
            let res = match &p {
                Placement::Replicas { len, copies } => self.repair_replicas(&id, *len, copies, &good, &readable, dry, &mut rep),
                Placement::Erasure { k, m, len, unit, shards } => {
                    self.repair_ec(&id, (*k, *m), *len, *unit, shards, &good, &readable, dry, &mut rep)
                }
            };
            if let Err(e) = res {
                rep.failed += 1;
                tracing::warn!(blob = %id, error = %e, "cluster repair");
            }
        }
        if rep.repaired > 0 || rep.lost > 0 || rep.degraded > 0 {
            tracing::info!(repaired = rep.repaired, degraded = rep.degraded, lost = rep.lost, failed = rep.failed, "cluster repair pass");
        }
        rep
    }

    /// Swaps `old` for `new` in the index; on success deletes `dropped`, otherwise the `added` copies.
    fn commit_swap(&self, old: &str, new: &str, dropped: &[Copy], added: &[Copy], rep: &mut RepairReport) -> io::Result<bool> {
        let map: HashMap<String, String> = [(old.to_string(), new.to_string())].into();
        let (found, _) = self.eng.swap_blobs(&map).map_err(io_err)?;
        if found.contains(old) {
            for c in dropped {
                if self.delete_copy(c).is_ok() {
                    rep.copies_deleted += 1;
                }
            }
            Ok(true)
        } else {
            for c in added {
                let _ = self.delete_copy(c);
            }
            Ok(false)
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn repair_replicas(
        &self,
        id: &str,
        len: u64,
        copies: &[Copy],
        good: &HashSet<String>,
        readable: &HashSet<String>,
        dry: bool,
        rep: &mut RepairReport,
    ) -> io::Result<()> {
        let mut keep: Vec<Copy> = copies.iter().filter(|c| good.contains(&c.node)).cloned().collect();
        let mut dropped: Vec<Copy> = copies.iter().filter(|c| !good.contains(&c.node)).cloned().collect();
        if self.strict() {
            // Copies sharing a failure domain: one stays, the others move once a better home exists.
            let mut seen = HashSet::new();
            let (ok, extra): (Vec<Copy>, Vec<Copy>) = keep.into_iter().partition(|c| seen.insert(self.domain(&c.node)));
            keep = ok;
            dropped.extend(extra);
        }
        if dropped.is_empty() && keep.len() >= self.cfg.copies {
            rep.healthy += 1;
            return Ok(());
        }
        let sources: Vec<Copy> = copies.iter().filter(|c| readable.contains(&c.node)).cloned().collect();
        if sources.is_empty() {
            rep.lost += 1;
            return Ok(());
        }
        if dry {
            rep.degraded += 1;
            return Ok(());
        }
        let holding: HashSet<String> = copies.iter().map(|c| c.node.clone()).collect();
        let mut added = vec![];
        let mut targets: Vec<String> = self.targets().into_iter().map(|(n, _)| n).filter(|n| !holding.contains(n)).collect();
        targets.retain(|n| good.contains(n));
        let taken: Vec<String> = keep.iter().map(|c| c.node.clone()).collect();
        let kind = self.kind_of_any(&sources);
        for t in self.diverse(targets, &taken) {
            if keep.len() >= self.cfg.copies {
                break;
            }
            if self.strict() && keep.iter().any(|c| self.domain(&c.node) == self.domain(&t)) {
                continue;
            }
            for s in &sources {
                let res = self.open_copy(s, 0).and_then(|r| self.put_copy(&t, r, kind.as_deref()));
                match res {
                    Ok((lid, n)) if n == len => {
                        let c = Copy { node: t.clone(), local: lid };
                        keep.push(c.clone());
                        added.push(c);
                        break;
                    }
                    Ok((lid, _)) => {
                        let _ = self.delete_copy(&Copy { node: t.clone(), local: lid });
                    }
                    Err(e) => tracing::debug!(from = %s.node, to = %t, error = %e, "copy for repair"),
                }
            }
        }
        if keep.len() < self.cfg.copies {
            rep.degraded += 1;
        }
        // Copies on draining nodes stay until enough copies are elsewhere; lost ones go from the id anyway.
        let mut dropped_now = vec![];
        let mut final_keep = keep.clone();
        for c in &dropped {
            if readable.contains(&c.node) && keep.len() < self.cfg.copies {
                final_keep.push(c.clone());
            } else {
                dropped_now.push(c.clone());
            }
        }
        if final_keep.is_empty() || (added.is_empty() && dropped_now.is_empty()) {
            return Ok(());
        }
        let new = cid::format(&Placement::Replicas { len, copies: final_keep });
        if self.commit_swap(id, &new, &dropped_now, &added, rep)? {
            rep.repaired += 1;
            self.metrics.repaired.fetch_add(1, Ordering::Relaxed);
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn repair_ec(
        &self,
        id: &str,
        km: (usize, usize),
        len: u64,
        unit: u64,
        shards: &[Copy],
        good: &HashSet<String>,
        readable: &HashSet<String>,
        dry: bool,
        rep: &mut RepairReport,
    ) -> io::Result<()> {
        let mut bad: Vec<usize> = (0..shards.len()).filter(|i| !good.contains(&shards[*i].node)).collect();
        if self.strict() {
            // Shards beyond m in one failure domain are misplaced: they move too.
            let mut per: HashMap<String, usize> = HashMap::new();
            for (i, s) in shards.iter().enumerate() {
                if bad.contains(&i) {
                    continue;
                }
                let c = per.entry(self.domain(&s.node)).or_default();
                *c += 1;
                if *c > km.1 {
                    bad.push(i);
                }
            }
        }
        if bad.is_empty() {
            rep.healthy += 1;
            return Ok(());
        }
        let readable_n = shards.iter().filter(|c| readable.contains(&c.node)).count();
        if readable_n < km.0 {
            rep.lost += 1;
            return Ok(());
        }
        if dry {
            rep.degraded += 1;
            return Ok(());
        }
        // New homes: good nodes holding no shard of this blob, spread across failure domains.
        let holding: HashSet<&str> = shards.iter().filter(|c| good.contains(&c.node)).map(|c| c.node.as_str()).collect();
        let free: Vec<String> =
            self.targets().into_iter().map(|(n, _)| n).filter(|n| good.contains(n) && !holding.contains(n.as_str())).collect();
        let mut staying: Vec<String> = shards.iter().enumerate().filter(|(i, _)| !bad.contains(i)).map(|(_, s)| s.node.clone()).collect();
        let mut homes = vec![];
        for n in self.diverse(free, &staying) {
            if homes.len() == bad.len() {
                break;
            }
            let mut with = staying.clone();
            with.push(n.clone());
            if !self.strict() || self.spread_ok(&with, Some(km.1)) {
                staying = with;
                homes.push(n);
            }
        }
        if homes.len() < bad.len() {
            rep.degraded += 1;
            return Ok(());
        }
        let which: Vec<(usize, String)> = bad.iter().copied().zip(homes).collect();
        let reader = self.open(id, 0)?;
        let kind = self.kind_of_any(shards);
        let written = self.encode_to(reader, len, km, unit, &which, kind)?;
        let mut next = shards.to_vec();
        let mut dropped = vec![];
        let mut added = vec![];
        for (i, c) in written {
            dropped.push(std::mem::replace(&mut next[i], c.clone()));
            added.push(c);
        }
        let new = cid::format(&Placement::Erasure { k: km.0, m: km.1, len, unit, shards: next });
        if self.commit_swap(id, &new, &dropped, &added, rep)? {
            rep.repaired += 1;
            self.metrics.repaired.fetch_add(1, Ordering::Relaxed);
        }
        Ok(())
    }

    /// Places blobs this node holds as single-store blobs (written before it joined the cluster).
    pub fn adopt(&self) -> u64 {
        let ids: HashSet<String> = self
            .eng
            .blob_ids()
            .unwrap_or_default()
            .into_iter()
            .filter(|i| !cid::is_cluster(i) && !i.contains(':') || engine::pack::is_packed(i))
            .collect();
        let mut n = 0;
        for id in ids {
            let Ok(mut r) = self.eng.open_local(&id, 0) else { continue }; // on another node's disk
            let res = (|| -> engine::Result<String> {
                // Packed bytes are copied into a blob of their own first (packs are not shared).
                let plain = if engine::pack::is_packed(&id) {
                    let mut w = self.eng.new_local_blob()?;
                    let mut buf = vec![];
                    r.read_to_end(&mut buf)?;
                    w.write(&buf)?;
                    w.seal()?.blob
                } else {
                    id.clone()
                };
                let seg = self.place(&self.eng, &Segment { blob: plain, ..Default::default() })?;
                Ok(seg.blob)
            })();
            match res {
                Ok(new) => {
                    let map: HashMap<String, String> = [(id.clone(), new.clone())].into();
                    match self.eng.swap_blobs(&map) {
                        Ok((found, _)) if found.contains(&id) => {
                            n += 1;
                            if !engine::pack::is_packed(&id) && !new.contains(&format!("@{id}")) {
                                let _ = self.eng.remove_local(&id);
                            }
                        }
                        _ => {
                            let _ = self.delete(&new);
                        }
                    }
                }
                Err(e) => tracing::warn!(blob = %id, error = %e, "adopting a single-store blob into the cluster"),
            }
        }
        if n > 0 {
            tracing::info!(blobs = n, "single-store blobs placed on the cluster");
            let _ = self.eng.compact(2.0, false); // deletes packs nothing refers to any more
        }
        n
    }

    /// Re-encodes replicated blobs of at least `ec_convert_above` bytes whose object is at least
    /// `older_than` old as k+m shards (up to `limit`): cold data then takes (k+m)/k of its size instead
    /// of `copies` times. Each blob's id is swapped in one transaction (only where unchanged); the old
    /// copies are deleted after.
    pub fn encode_cold(&self, older_than: Duration, limit: u64) -> Result<EncodeReport, String> {
        let (k, m) = self.cfg.ec.ok_or("erasure coding is off: set CAIRN_STORE_CLUSTER_EC")?;
        let _one = self.repairing.lock().unwrap();
        *self.nodes.lock().unwrap() = None;
        let mut rep = EncodeReport::default();
        let cutoff = engine::now_ms().saturating_sub(older_than.as_millis() as u64);
        let ages = self.eng.blob_ages().map_err(|e| e.to_string())?;
        let mut cands: Vec<(String, u64)> = ages
            .into_iter()
            .filter(|(id, t)| *t <= cutoff && id.starts_with("r:"))
            .filter_map(|(id, _)| match cid::parse(&id) {
                Some(Placement::Replicas { len, .. }) if len >= self.cfg.ec_convert_above => Some((id, len)),
                _ => None,
            })
            .collect();
        cands.sort();
        rep.candidates = cands.len() as u64;
        for (id, len) in cands.into_iter().take(limit as usize) {
            let Some(Placement::Replicas { copies, .. }) = cid::parse(&id) else { continue };
            let res = (|| -> io::Result<bool> {
                let mut chosen: Vec<String> = vec![];
                for n in self.diverse(self.targets().into_iter().map(|(n, _)| n).collect(), &[]) {
                    if chosen.len() == k + m {
                        break;
                    }
                    let mut with = chosen.clone();
                    with.push(n.clone());
                    if !self.strict() || self.spread_ok(&with, Some(m)) {
                        chosen = with;
                    }
                }
                if chosen.len() < k + m {
                    return Err(io_err(format!("erasure coding {k}+{m} needs more live nodes (or {}s)", self.cfg.spread)));
                }
                let unit = Self::unit_for(len, k);
                let which: Vec<(usize, String)> = chosen.into_iter().enumerate().collect();
                let kind = self.kind_of_any(&copies);
                let reader = self.open(&id, 0)?;
                let shards = self.encode_to(reader, len, (k, m), unit, &which, kind)?;
                let shards: Vec<Copy> = (0..k + m).map(|i| shards[&i].clone()).collect();
                let new = cid::format(&Placement::Erasure { k, m, len, unit, shards: shards.clone() });
                let mut scratch = RepairReport::default();
                self.commit_swap(&id, &new, &copies, &shards, &mut scratch)
            })();
            match res {
                Ok(true) => {
                    rep.converted += 1;
                    rep.bytes_before += len * copies.len() as u64;
                    rep.bytes_after += len.div_ceil(k as u64 * Self::unit_for(len, k)).max(1) * Self::unit_for(len, k) * (k + m) as u64;
                }
                Ok(false) => {} // changed meanwhile
                Err(e) => {
                    rep.failed += 1;
                    tracing::warn!(blob = %id, error = %e, "converting to erasure coding");
                }
            }
        }
        if rep.converted > 0 {
            tracing::info!(converted = rep.converted, before = rep.bytes_before, after = rep.bytes_after, "cold blobs erasure-coded");
        }
        Ok(rep)
    }

    /// Stored bytes per node (from the index).
    fn usage(&self) -> HashMap<String, (u64, u64)> {
        let mut out: HashMap<String, (u64, u64)> = HashMap::new();
        let ids: HashSet<String> = self.eng.blob_ids().unwrap_or_default().into_iter().filter(|i| cid::is_cluster(i)).collect();
        for id in ids {
            let Some(p) = cid::parse(&id) else { continue };
            let per = match &p {
                Placement::Replicas { len, .. } => *len,
                Placement::Erasure { k, len, unit, .. } => len.div_ceil(*k as u64 * unit).max(1) * unit,
            };
            for c in p.holders() {
                let e = out.entry(c.node.clone()).or_default();
                e.0 += 1;
                e.1 += per;
            }
        }
        out
    }

    /// Moves copies from the fullest active nodes to the emptiest, up to `limit` moves.
    pub fn rebalance(&self, limit: u64) -> RebalanceReport {
        let _one = self.repairing.lock().unwrap();
        *self.nodes.lock().unwrap() = None;
        let (good, _) = self.health();
        let mut rep = RebalanceReport::default();
        let mut bytes: HashMap<String, u64> = good.iter().map(|n| (n.clone(), 0)).collect();
        for (n, (_, b)) in self.usage() {
            if let Some(v) = bytes.get_mut(&n) {
                *v = b;
            }
        }
        rep.before = bytes.clone();
        if bytes.len() < 2 {
            rep.after = bytes;
            return rep;
        }
        let ids: Vec<String> = {
            let mut v: Vec<String> = self.eng.blob_ids().unwrap_or_default().into_iter().filter(|i| cid::is_cluster(i)).collect();
            v.sort();
            v.dedup();
            v.shuffle(&mut rand::thread_rng());
            v
        };
        for id in ids {
            if rep.moved >= limit {
                break;
            }
            let (max_n, max_b) = bytes.iter().max_by_key(|(_, b)| **b).map(|(n, b)| (n.clone(), *b)).unwrap();
            let (min_n, min_b) = bytes.iter().min_by_key(|(_, b)| **b).map(|(n, b)| (n.clone(), *b)).unwrap();
            let Some(p) = cid::parse(&id) else { continue };
            let per = match &p {
                Placement::Replicas { len, .. } => *len,
                Placement::Erasure { k, len, unit, .. } => len.div_ceil(*k as u64 * unit).max(1) * unit,
            };
            // Only moves that narrow the gap.
            if max_b.saturating_sub(min_b) <= per || p.holders().iter().any(|c| c.node == min_n) {
                continue;
            }
            let Some(i) = p.holders().iter().position(|c| c.node == max_n) else { continue };
            let src = p.holders()[i].clone();
            let mut after: Vec<String> = p.holders().iter().map(|c| c.node.clone()).collect();
            after[i] = min_n.clone();
            let ec_m = match &p {
                Placement::Erasure { m, .. } => Some(*m),
                _ => None,
            };
            if self.strict() && !self.spread_ok(&after, ec_m) {
                continue; // the move would put too much of this blob in one failure domain
            }
            let kind = self.kind_of(&src);
            let Ok((lid, n)) = self.open_copy(&src, 0).and_then(|r| self.put_copy(&min_n, r, kind.as_deref())) else { continue };
            let new_copy = Copy { node: min_n.clone(), local: lid };
            if n != per {
                let _ = self.delete_copy(&new_copy);
                continue;
            }
            let mut q = p.clone();
            q.holders_mut()[i] = new_copy.clone();
            let mut scratch = RepairReport::default();
            if let Ok(true) = self.commit_swap(&id, &cid::format(&q), &[src], &[new_copy], &mut scratch) {
                rep.moved += 1;
                rep.bytes += per;
                *bytes.get_mut(&max_n).unwrap() -= per;
                *bytes.get_mut(&min_n).unwrap() += per;
                self.metrics.moved.fetch_add(1, Ordering::Relaxed);
            }
        }
        rep.after = bytes;
        if rep.moved > 0 {
            tracing::info!(moved = rep.moved, bytes = rep.bytes, "cluster rebalance");
        }
        rep
    }

    /// The cluster's state for the admin API.
    pub fn status(&self) -> Value {
        *self.nodes.lock().unwrap() = None;
        let usage = self.usage();
        let now = engine::now_ms();
        let nodes: Vec<Value> = self
            .all_nodes()
            .into_iter()
            .map(|(n, r)| {
                let (blobs, bytes) = usage.get(&n).copied().unwrap_or_default();
                json!({
                    "name": n, "url": r.url, "state": r.state, "alive": self.alive(&r), "zone": r.zone, "rack": r.rack,
                    "heartbeat_age_ms": now.saturating_sub(r.heartbeat), "version": r.version,
                    "copies": blobs, "bytes": bytes, "this_node": n == self.name(),
                })
            })
            .collect();
        let check = self.repair(true);
        json!({
            "node": self.name(),
            "copies": self.cfg.copies,
            "spread": self.cfg.spread,
            "erasure_coding": self.cfg.ec.map(|(k, m)| json!({"data": k, "parity": m, "above_bytes": self.cfg.ec_above})),
            "nodes": nodes,
            "blobs": {"total": check.blobs, "healthy": check.healthy, "degraded": check.degraded, "lost": check.lost},
        })
    }

    /// Sets a node's state (active, draining, removed), audited.
    pub fn set_state(&self, name: &str, state: &str, actor: &str) -> Result<NodeRec, String> {
        let mut out = None;
        let audit = crate::audit::Audit {
            category: "store",
            action: format!(
                "cluster.node.{}",
                if state == "active" {
                    "activate"
                } else if state == "draining" {
                    "drain"
                } else {
                    "remove"
                }
            ),
            actor: actor.to_string(),
            subject: name.to_string(),
            details: json!({"state": state}),
        };
        self.eng
            .update_node(
                name,
                |cur| {
                    let mut rec: NodeRec = serde_json::from_slice(cur?).ok()?;
                    rec.state = state.to_string();
                    out = Some(rec.clone());
                    Some(serde_json::to_vec(&rec).unwrap())
                },
                Some(&audit),
            )
            .map_err(|e| e.to_string())?;
        *self.nodes.lock().unwrap() = None;
        out.ok_or_else(|| "no such node".into())
    }

    pub fn metrics_text(&self, out: &mut String) {
        use std::fmt::Write;
        let nodes = self.all_nodes();
        let alive = nodes.iter().filter(|(_, r)| self.alive(r) && r.state != "removed").count();
        let _ = writeln!(out, "# TYPE cairn_store_cluster_nodes gauge");
        let _ = writeln!(out, "cairn_store_cluster_nodes{{state=\"alive\"}} {alive}");
        let _ = writeln!(out, "cairn_store_cluster_nodes{{state=\"known\"}} {}", nodes.len());
        let m = &self.metrics;
        for (name, v) in [
            ("placed", &m.placed),
            ("placed_erasure_coded", &m.placed_ec),
            ("place_failed", &m.place_failed),
            ("remote_reads", &m.remote_reads),
            ("failovers", &m.failovers),
            ("reconstructions", &m.reconstructions),
            ("repaired", &m.repaired),
            ("moved", &m.moved),
            ("copies_deleted", &m.deleted),
        ] {
            let _ = writeln!(out, "# TYPE cairn_store_cluster_{name}_total counter");
            let _ = writeln!(out, "cairn_store_cluster_{name}_total {}", v.load(Ordering::Relaxed));
        }
    }
}

// ----------------------------------------------------------------------------------- server side

impl Server {
    /// The internal node-to-node API (`/-/cluster/v1/`).
    pub(crate) async fn cluster_api<B>(&self, req: Request<B>) -> Resp
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let err = |status: StatusCode, msg: &str| {
            let mut r = full(status, json!({"error": msg}).to_string());
            set_header(&mut r, "content-type", "application/json");
            r
        };
        let Some(c) = self.cluster.clone() else { return err(StatusCode::NOT_FOUND, "this store is not part of a cluster") };
        let path = req.uri().path().to_string();
        let ok = (|| {
            let h = req.headers().get("x-cairn-cluster")?.to_str().ok()?;
            let mut it = h.split(' ');
            let (node, ts, sig) = (it.next()?, it.next()?.parse::<i64>().ok()?, it.next()?);
            let now = time::OffsetDateTime::now_utc().unix_timestamp();
            if (now - ts).abs() > MAX_SKEW_SECS {
                return None;
            }
            let want = mac(&c.cfg.secret, req.method().as_str(), &path, ts, node);
            Some(bool::from(subtle::ConstantTimeEq::ct_eq(want.as_bytes(), sig.as_bytes())))
        })()
        .unwrap_or(false);
        if !ok {
            return err(StatusCode::FORBIDDEN, "bad or missing cluster signature");
        }
        let rest = path.trim_start_matches("/-/cluster/v1/");
        let offset: u64 =
            req.uri().query().unwrap_or("").split('&').find_map(|p| p.strip_prefix("offset=")).and_then(|v| v.parse().ok()).unwrap_or(0);
        let method = req.method().clone();
        match (method, rest) {
            (Method::GET, "ping") => full(StatusCode::OK, json!({"node": c.cfg.node}).to_string()),
            (Method::GET, id) if id.starts_with("kind/") => {
                let Some(id) = path_id(id.trim_start_matches("kind/")) else { return err(StatusCode::BAD_REQUEST, "bad id") };
                full(StatusCode::OK, json!({"type": self.eng.disk_kind_of(id)}).to_string())
            }
            (Method::PUT, "blobs") => {
                let disk: Option<String> = req
                    .uri()
                    .query()
                    .unwrap_or("")
                    .split('&')
                    .find_map(|p| p.strip_prefix("disk="))
                    .filter(|k| engine::disks::valid_kind(k))
                    .map(str::to_string);
                let w = tokio::task::block_in_place(|| self.eng.new_local_blob_on(disk.as_deref()));
                let mut w = match w {
                    Ok(w) => w,
                    // No disk of that type here: the placing node tries another node.
                    Err(e) => return err(StatusCode::INSUFFICIENT_STORAGE, &e.to_string()),
                };
                let res = async {
                    let mut body = req.into_body();
                    let mut n = 0u64;
                    while let Some(frame) = body.frame().await {
                        let frame = frame.map_err(io_err)?;
                        let Ok(data) = frame.into_data() else { continue };
                        n += data.len() as u64;
                        tokio::task::block_in_place(|| w.write(&data))?;
                    }
                    let seg = tokio::task::block_in_place(|| w.seal()).map_err(io_err)?;
                    Ok::<_, io::Error>((seg.blob, n))
                }
                .await;
                match res {
                    Ok((id, n)) => full(StatusCode::CREATED, json!({"id": id, "len": n}).to_string()),
                    Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()),
                }
            }
            (Method::GET, id) if id.starts_with("blobs/") => {
                let Some(id) = path_id(id.trim_start_matches("blobs/")) else { return err(StatusCode::BAD_REQUEST, "bad id") };
                match tokio::task::block_in_place(|| self.eng.open_local(id, offset)) {
                    Ok(r) => {
                        let mut resp = hyper::Response::new(super::tiering::file_body(r));
                        *resp.status_mut() = StatusCode::OK;
                        resp
                    }
                    Err(engine::Error::Io(e)) if e.kind() == io::ErrorKind::NotFound => err(StatusCode::NOT_FOUND, "no such blob"),
                    Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()),
                }
            }
            (Method::DELETE, id) if id.starts_with("blobs/") => {
                let Some(id) = path_id(id.trim_start_matches("blobs/")) else { return err(StatusCode::BAD_REQUEST, "bad id") };
                match tokio::task::block_in_place(|| self.eng.remove_local(id)) {
                    Ok(()) => full(StatusCode::NO_CONTENT, Bytes::new()),
                    Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()),
                }
            }
            _ => err(StatusCode::NOT_FOUND, "no such cluster call"),
        }
    }

    /// May this node run the cluster-wide background task `task` now? (Always, outside a cluster.)
    pub(crate) fn may_run(&self, task: &'static str) -> bool {
        match &self.cluster {
            None => true,
            Some(c) => tokio::task::block_in_place(|| c.lead(task)),
        }
    }

    /// Heartbeats, key reloads, collection and repair.
    pub async fn run_cluster(self: Arc<Self>) {
        let Some(c) = self.cluster.clone() else { return };
        let mut tick = tokio::time::interval(c.cfg.heartbeat);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut last_repair = Instant::now();
        let mut adopted = false;
        loop {
            tick.tick().await;
            let (me, cl) = (self.clone(), c.clone());
            let repair_due = last_repair.elapsed() >= c.cfg.repair_every;
            if repair_due {
                last_repair = Instant::now();
            }
            let first = !adopted;
            adopted = true;
            let res = tokio::task::spawn_blocking(move || -> Result<(), String> {
                cl.heartbeat()?;
                if let Err(e) = me.reload_keys() {
                    tracing::warn!(error = %e, "reloading access keys");
                }
                if first || repair_due {
                    cl.adopt();
                }
                if cl.lead("cluster") {
                    me.eng.collect_cluster();
                    if repair_due {
                        cl.repair(false);
                        if let Some(after) = cl.cfg.ec_after {
                            if let Err(e) = cl.encode_cold(after, 1000) {
                                tracing::warn!(error = %e, "erasure coding cold blobs");
                            }
                        }
                    }
                }
                Ok(())
            })
            .await;
            match res {
                Ok(Err(e)) if e.contains("removed") => {
                    tracing::error!(error = %e, "this node was removed from the cluster; stopping");
                    eprintln!("cairn-store: {e}; stopping");
                    std::process::exit(3);
                }
                Ok(Err(e)) => tracing::warn!(error = %e, "cluster heartbeat"),
                _ => {}
            }
        }
    }

    /// The admin API's cluster calls (blocking).
    pub(crate) fn cluster_admin(
        &self,
        m: &Method,
        rest: &[&str],
        body: &[u8],
        actor: &str,
    ) -> Result<(StatusCode, Value), super::errors::ApiError> {
        use super::errors as e;
        let c = self.cluster.as_ref().ok_or_else(|| e::CLUSTER_OFF.clone())?;
        let limit =
            || -> u64 { serde_json::from_slice::<Value>(body).ok().and_then(|v| v.get("limit").and_then(Value::as_u64)).unwrap_or(10_000) };
        match (m, rest) {
            (&Method::GET, []) => Ok((StatusCode::OK, c.status())),
            (&Method::POST, ["repair"]) => {
                let r = c.repair(false);
                self.eng.append_audit(&crate::audit::Audit {
                    category: "store",
                    action: "cluster.repair".into(),
                    actor: actor.into(),
                    subject: String::new(),
                    details: json!({"repaired": r.repaired, "lost": r.lost, "degraded": r.degraded}),
                })?;
                Ok((StatusCode::OK, serde_json::to_value(r).map_err(|_| e::INTERNAL.clone())?))
            }
            (&Method::POST, ["encode"]) => {
                let v: Value = serde_json::from_slice(body).unwrap_or(json!({}));
                let older = Duration::from_secs(v.get("older_than_seconds").and_then(Value::as_u64).unwrap_or(0));
                let r = c.encode_cold(older, limit()).map_err(|_| e::EC_OFF.clone())?;
                self.eng.append_audit(&crate::audit::Audit {
                    category: "store",
                    action: "cluster.encode".into(),
                    actor: actor.into(),
                    subject: String::new(),
                    details: json!({"converted": r.converted, "bytes_before": r.bytes_before, "bytes_after": r.bytes_after}),
                })?;
                Ok((StatusCode::OK, serde_json::to_value(r).map_err(|_| e::INTERNAL.clone())?))
            }
            (&Method::POST, ["rebalance"]) => {
                let r = c.rebalance(limit());
                self.eng.append_audit(&crate::audit::Audit {
                    category: "store",
                    action: "cluster.rebalance".into(),
                    actor: actor.into(),
                    subject: String::new(),
                    details: json!({"moved": r.moved, "bytes": r.bytes}),
                })?;
                Ok((StatusCode::OK, serde_json::to_value(r).map_err(|_| e::INTERNAL.clone())?))
            }
            (&Method::POST, ["nodes", name, action]) => {
                let state = match *action {
                    "drain" => "draining",
                    "activate" => "active",
                    "remove" => "removed",
                    _ => return Err(e::ADMIN_NOT_FOUND.clone()),
                };
                match c.set_state(name, state, actor) {
                    Ok(rec) => {
                        tracing::info!(node = %name, state, admin = %actor, "cluster node state changed");
                        Ok((StatusCode::OK, json!({"name": name, "state": rec.state, "url": rec.url})))
                    }
                    Err(_) => Err(e::NO_SUCH_NODE.clone()),
                }
            }
            _ => Err(e::ADMIN_NOT_FOUND.clone()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn erasure_round_trip() {
        let (k, m) = (4usize, 2usize);
        let data: Vec<u8> = (0..100_000u32).map(|i| (i * 7 % 251) as u8).collect();
        let unit = Cluster::unit_for(data.len() as u64, k) as usize;
        let rs = ReedSolomon::new(k, m).unwrap();
        let mut padded = data.clone();
        padded.resize(k * unit, 0);
        let mut shards: Vec<Vec<u8>> = padded.chunks(unit).map(<[u8]>::to_vec).collect();
        shards.extend((0..m).map(|_| vec![0u8; unit]));
        rs.encode(&mut shards).unwrap();
        let mut got: Vec<Option<Vec<u8>>> = shards.into_iter().map(Some).collect();
        got[0] = None;
        got[3] = None;
        rs.reconstruct_data(&mut got).unwrap();
        let back: Vec<u8> = got.into_iter().take(k).flat_map(|s| s.unwrap()).take(data.len()).collect();
        assert_eq!(back, data);
        assert_eq!(parse_ec("4+2").unwrap(), Some((4, 2)));
        assert!(parse_ec("4").is_err());
        assert_eq!(mac("s", "GET", "/p", 1, "a"), mac("s", "GET", "/p", 1, "a"));
        assert_ne!(mac("s", "GET", "/p", 1, "a"), mac("s", "PUT", "/p", 1, "a"));
    }
}
