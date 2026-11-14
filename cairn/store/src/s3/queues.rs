//! Message queues: durable topics that producers append to and consumer groups read in order, under
//! `/-/queues/v1/` (signed with SigV4 like the admin API).
//!
//! ```text
//! PUT    /-/queues/v1/topics/{topic}            {"retention_seconds": n, "max_messages": n}  create or change (admin keys)
//! GET    /-/queues/v1/topics                     topics with their message counts
//! GET    /-/queues/v1/topics/{topic}             first and next sequence numbers, groups and their lag
//! DELETE /-/queues/v1/topics/{topic}             the topic and its messages (admin keys)
//! POST   /-/queues/v1/topics/{topic}/messages    one message (the body; x-cairn-key, content-type) -> {"seq"}
//! POST   /-/queues/v1/topics/{topic}/messages?batch  {"messages": [{"value" | "value_base64", "key"}]} -> {"first", "last"}
//! GET    /-/queues/v1/topics/{topic}/messages?group=g&max=100&wait=10   messages after the group's offset
//! GET    /-/queues/v1/topics/{topic}/messages?after=N&max=100            messages after N
//! POST   /-/queues/v1/topics/{topic}/groups/{group}  {"seq": n}  commit: the group has processed up to n
//! ```
//!
//! A topic has `partitions` (1 by default; they can be added, never removed). Sequence numbers, offsets
//! and order are per partition. A message goes to `?partition=p`, else to the partition its key hashes to
//! (Kafka's murmur2, as Kafka producers pick), else round robin; reads and commits name a `partition`
//! (default 0). The Kafka protocol (`kafka.rs`) serves the same topics.
//!
//! Messages are written in one index transaction each batch, so an acknowledged publish survives a
//! crash, and get gapless increasing sequence numbers per topic. Consumers read at least once: a group's
//! offset moves only when it commits. `wait` long-polls for new messages (up to 20 s). Retention removes
//! messages older than `retention_seconds` or beyond `max_messages`, from the oldest. Access: admin keys
//! manage topics; other keys publish and consume on topics their bucket patterns allow (read-only keys
//! consume and commit only).

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use bytes::Bytes;
use http_body_util::BodyExt;
use hyper::{Method, Request, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::auth;
use super::errors::{self as e, ApiError};
use super::{full, set_header, Resp, Server};
use crate::engine;

pub const MAX_MESSAGE: usize = 1 << 20;
pub const MAX_PARTITIONS: u32 = 64;
const MAX_BATCH: usize = 1000;

/// Messages read: (sequence number, stored message).
type Batch = Vec<(u64, Vec<u8>)>;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct TopicConfig {
    /// Messages older than this are removed (0: kept).
    #[serde(default)]
    pub retention_seconds: u64,
    /// At most this many messages are kept, the oldest removed first (0: no limit).
    #[serde(default)]
    pub max_messages: u64,
    #[serde(default)]
    pub created: u64,
    /// Partitions (0 is read as 1).
    #[serde(default)]
    pub partitions: u32,
}

impl TopicConfig {
    pub fn parts(&self) -> u32 {
        self.partitions.max(1)
    }
}

/// A stored message: a JSON header line, then the value's bytes.
#[derive(Default, Serialize, Deserialize)]
struct Header {
    t: u64,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    k: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    ct: String,
    /// The key is base64 (a Kafka key that is not UTF-8).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    kb: bool,
    /// Kafka record headers (values in base64).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    h: Vec<(String, String)>,
}

/// A stored message for a Kafka record.
pub(crate) fn encode_record(t: u64, key: Option<&[u8]>, headers: &[(String, Vec<u8>)], value: &[u8]) -> Vec<u8> {
    let b64 = base64::engine::general_purpose::STANDARD;
    let (k, kb) = match key.map(std::str::from_utf8) {
        None => (String::new(), false),
        Some(Ok(s)) => (s.to_string(), false),
        Some(Err(_)) => (b64.encode(key.unwrap_or_default()), true),
    };
    let h = headers.iter().map(|(n, v)| (n.clone(), b64.encode(v))).collect();
    encode(&Header { t, k, ct: String::new(), kb, h }, value)
}

/// A stored message as a Kafka record.
pub(crate) fn decode_record(raw: &[u8]) -> Option<super::kafka::Record> {
    let (h, v) = decode(raw)?;
    let b64 = base64::engine::general_purpose::STANDARD;
    let key = match (h.k.is_empty(), h.kb) {
        (true, _) => None,
        (false, true) => Some(b64.decode(&h.k).ok()?),
        (false, false) => Some(h.k.into_bytes()),
    };
    let headers = h.h.into_iter().filter_map(|(n, v)| Some((n, b64.decode(v).ok()?))).collect();
    Some(super::kafka::Record { key, value: v.to_vec(), headers, time: h.t as i64 })
}

/// Kafka's default partitioner: murmur2 of the key, made positive, modulo the partitions.
pub(crate) fn key_partition(key: &[u8], parts: u32) -> u32 {
    let (seed, m, r): (u32, u32, u32) = (0x9747_b28c, 0x5bd1_e995, 24);
    let len = key.len();
    let mut h: u32 = seed ^ (len as u32);
    let mut chunks = key.chunks_exact(4);
    for c in &mut chunks {
        let mut k = u32::from_le_bytes(c.try_into().unwrap());
        k = k.wrapping_mul(m);
        k ^= k >> r;
        k = k.wrapping_mul(m);
        h = h.wrapping_mul(m);
        h ^= k;
    }
    let rest = chunks.remainder();
    if rest.len() >= 3 {
        h ^= (rest[2] as u32) << 16;
    }
    if rest.len() >= 2 {
        h ^= (rest[1] as u32) << 8;
    }
    if !rest.is_empty() {
        h ^= rest[0] as u32;
        h = h.wrapping_mul(m);
    }
    h ^= h >> 13;
    h = h.wrapping_mul(m);
    h ^= h >> 15;
    (h & 0x7fff_ffff) % parts.max(1)
}

fn encode(h: &Header, value: &[u8]) -> Vec<u8> {
    let mut out = serde_json::to_vec(h).unwrap();
    out.push(b'\n');
    out.extend_from_slice(value);
    out
}

fn decode(raw: &[u8]) -> Option<(Header, &[u8])> {
    let i = raw.iter().position(|b| *b == b'\n')?;
    Some((serde_json::from_slice(&raw[..i]).ok()?, &raw[i + 1..]))
}

pub fn valid_topic(name: &str) -> bool {
    !name.is_empty() && name.len() <= 128 && name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || "._-".contains(c))
}

use super::kafka::part_name;

impl Server {
    /// Creates a topic or changes its settings, partitions included (added, never removed), in one
    /// transaction per partition topic, audited once.
    pub(crate) fn queue_create(&self, cred: &auth::Credential, t: &str, mut cfg: TopicConfig) -> Result<Value, ApiError> {
        if cfg.parts() > MAX_PARTITIONS {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        let existing: Option<TopicConfig> = self.eng.queue_topic(t)?.and_then(|d| serde_json::from_slice(&d).ok());
        if let Some(old) = &existing {
            if cfg.partitions == 0 {
                cfg.partitions = old.parts(); // not given: unchanged
            }
            if cfg.parts() < old.parts() {
                return Err(e::PARTITIONS_REMOVED.clone());
            }
        }
        cfg.created = existing.as_ref().map(|c| c.created).unwrap_or_else(engine::now_ms);
        cfg.partitions = cfg.parts();
        let doc = serde_json::to_vec(&cfg).unwrap();
        // Partitions after the first are their own queue topics, created before the topic shows them.
        for p in 1..cfg.parts() {
            self.eng.queue_put_topic(&part_name(t, p as i32), Some(&doc), None)?;
        }
        let audit = crate::audit::Audit {
            category: "store",
            action: if existing.is_some() { "queue.update" } else { "queue.create" }.into(),
            actor: cred.access_key.clone(),
            subject: t.to_string(),
            details: json!({"retention_seconds": cfg.retention_seconds, "max_messages": cfg.max_messages, "partitions": cfg.partitions}),
        };
        self.eng.queue_put_topic(t, Some(&doc), Some(&audit))?;
        Ok(json!({"name": t, "retention_seconds": cfg.retention_seconds, "max_messages": cfg.max_messages, "partitions": cfg.partitions}))
    }
}

#[derive(Default)]
pub struct QueueMetrics {
    pub published: AtomicU64,
    pub delivered: AtomicU64,
    pub trimmed: AtomicU64,
}

fn json_resp(status: StatusCode, v: Value) -> Resp {
    let mut r = full(status, v.to_string());
    set_header(&mut r, "content-type", "application/json");
    r
}

impl Server {
    /// Handles a request to the queue API.
    pub(crate) async fn queue_api<B>(&self, req: Request<B>) -> Resp
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        match self.queue_inner(req).await {
            Ok(r) => r,
            Err(err) => json_resp(
                StatusCode::from_u16(err.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
                json!({"error": err.code, "message": err.message}),
            ),
        }
    }

    async fn queue_inner<B>(&self, req: Request<B>) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let (parts, body) = req.into_parts();
        let raw_path = parts.uri.path().to_string();
        let raw_query = parts.uri.query().unwrap_or("").to_string();
        let host = parts.headers.get("host").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        if auth::is_anonymous(&parts.headers, &raw_query) {
            return Err(e::ACCESS_DENIED.clone());
        }
        let request = auth::Request {
            method: parts.method.as_str(),
            raw_path: &raw_path,
            raw_query: &raw_query,
            host: &host,
            headers: &parts.headers,
            now: (self.clock)(),
            vhost_bucket: None,
        };
        let a = auth::verify(&*self.keyset(), &request)?;
        let body = body.collect().await.map_err(|_| e::INCOMPLETE_BODY.clone())?.to_bytes();
        if body.len() > MAX_MESSAGE * 2 {
            return Err(e::ENTITY_TOO_LARGE.clone());
        }
        if a.payload_hash != auth::UNSIGNED_PAYLOAD && a.payload_hash != auth::sha256_hex(&body) {
            return Err(e::CONTENT_SHA_MISMATCH.clone());
        }
        let q: BTreeMap<String, String> = raw_query
            .split('&')
            .filter(|p| !p.is_empty())
            .map(|p| p.split_once('=').unwrap_or((p, "")))
            .map(|(k, v)| (auth::query_decode(k), auth::query_decode(v)))
            .collect();
        let path = raw_path.trim_start_matches("/-/queues/v1").trim_matches('/');
        let segs: Vec<String> =
            path.split('/').filter(|s| !s.is_empty()).map(|s| String::from_utf8_lossy(&auth::percent_decode(s)).into_owned()).collect();
        let segs: Vec<&str> = segs.iter().map(String::as_str).collect();
        let cred = &a.cred;
        // Who may do what on a topic.
        let may = |topic: &str, write: bool| -> Result<(), ApiError> {
            if !valid_topic(topic) {
                return Err(e::INVALID_TOPIC.clone());
            }
            if cred.admin || (cred.allows(topic) && !(write && cred.read_only)) {
                Ok(())
            } else {
                Err(e::ACCESS_DENIED.clone())
            }
        };
        let blocking = |f: &mut dyn FnMut() -> Result<Value, ApiError>| tokio::task::block_in_place(f);
        match (&parts.method, segs.as_slice()) {
            (&Method::GET, ["topics"]) => {
                let v = blocking(&mut || {
                    let mut out = vec![];
                    for (name, cfg) in self.eng.queue_topics()? {
                        if name.contains('#') || !(cred.admin || cred.allows(&name)) {
                            continue;
                        }
                        let cfg: TopicConfig = serde_json::from_slice(&cfg).unwrap_or_default();
                        let (first, next) = self.eng.queue_bounds(&name)?;
                        let mut messages = next - first;
                        for p in 1..cfg.parts() {
                            let (f, n) = self.eng.queue_bounds(&part_name(&name, p as i32))?;
                            messages += n - f;
                        }
                        out.push(json!({"name": name, "messages": messages, "first": first, "next": next, "partitions": cfg.parts(),
                                        "retention_seconds": cfg.retention_seconds, "max_messages": cfg.max_messages}));
                    }
                    Ok(json!({"topics": out}))
                })?;
                Ok(json_resp(StatusCode::OK, v))
            }
            (&Method::PUT, ["topics", t]) => {
                if !cred.admin {
                    return Err(e::ACCESS_DENIED.clone());
                }
                may(t, true)?;
                #[derive(Deserialize)]
                #[serde(deny_unknown_fields)]
                struct In {
                    #[serde(default)]
                    retention_seconds: u64,
                    #[serde(default)]
                    max_messages: u64,
                    #[serde(default)]
                    partitions: u32,
                }
                let i: In = serde_json::from_slice(if body.is_empty() { b"{}" } else { &body }).map_err(|_| e::MALFORMED_JSON.clone())?;
                let cfg = TopicConfig {
                    retention_seconds: i.retention_seconds,
                    max_messages: i.max_messages,
                    partitions: i.partitions,
                    created: 0,
                };
                let v = blocking(&mut || self.queue_create(cred, t, TopicConfig { ..cfg.clone() }))?;
                Ok(json_resp(StatusCode::OK, v))
            }
            (&Method::GET, ["topics", t]) => {
                may(t, false)?;
                let v = blocking(&mut || {
                    let cfg: TopicConfig =
                        serde_json::from_slice(&self.eng.queue_topic(t)?.ok_or_else(|| e::NO_SUCH_TOPIC.clone())?).unwrap_or_default();
                    let (first, next) = self.eng.queue_bounds(t)?;
                    let groups: BTreeMap<String, Value> = self
                        .eng
                        .queue_groups(t)?
                        .into_iter()
                        .map(|(g, off)| (g, json!({"offset": off, "lag": next.saturating_sub(off + 1).min(next - first)})))
                        .collect();
                    let mut parts = vec![];
                    let mut messages = 0;
                    for p in 0..cfg.parts() {
                        let name = part_name(t, p as i32);
                        let (f, n) = self.eng.queue_bounds(&name)?;
                        messages += n - f;
                        let lags: BTreeMap<String, Value> = self
                            .eng
                            .queue_groups(&name)?
                            .into_iter()
                            .map(|(g, off)| (g, json!({"offset": off, "lag": n.saturating_sub(off + 1).min(n - f)})))
                            .collect();
                        parts.push(json!({"partition": p, "first": f, "next": n, "messages": n - f, "groups": lags}));
                    }
                    Ok(json!({"name": t, "first": first, "next": next, "messages": messages, "groups": groups, "partitions": cfg.parts(),
                              "partition_detail": parts, "retention_seconds": cfg.retention_seconds, "max_messages": cfg.max_messages}))
                })?;
                Ok(json_resp(StatusCode::OK, v))
            }
            (&Method::DELETE, ["topics", t]) => {
                if !cred.admin {
                    return Err(e::ACCESS_DENIED.clone());
                }
                may(t, true)?;
                blocking(&mut || {
                    let cfg: TopicConfig =
                        serde_json::from_slice(&self.eng.queue_topic(t)?.ok_or_else(|| e::NO_SUCH_TOPIC.clone())?).unwrap_or_default();
                    for p in 1..cfg.parts() {
                        self.eng.queue_put_topic(&part_name(t, p as i32), None, None)?;
                    }
                    let audit = crate::audit::Audit {
                        category: "store",
                        action: "queue.delete".into(),
                        actor: cred.access_key.clone(),
                        subject: t.to_string(),
                        details: json!({}),
                    };
                    self.eng.queue_put_topic(t, None, Some(&audit))?;
                    Ok(Value::Null)
                })?;
                Ok(full(StatusCode::NO_CONTENT, Bytes::new()))
            }
            (&Method::POST, ["topics", t, "messages"]) => {
                may(t, true)?;
                let now = engine::now_ms();
                let mut keys: Vec<String> = vec![];
                let msgs: Vec<Vec<u8>> = if q.contains_key("batch") {
                    #[derive(Deserialize)]
                    struct In {
                        #[serde(default)]
                        value: Option<String>,
                        #[serde(default)]
                        value_base64: Option<String>,
                        #[serde(default)]
                        key: String,
                        #[serde(default)]
                        content_type: String,
                    }
                    #[derive(Deserialize)]
                    struct Batch {
                        messages: Vec<In>,
                    }
                    let b: Batch = serde_json::from_slice(&body).map_err(|_| e::MALFORMED_JSON.clone())?;
                    if b.messages.is_empty() || b.messages.len() > MAX_BATCH {
                        return Err(e::INVALID_ARGUMENT.clone());
                    }
                    let mut out = vec![];
                    for m in b.messages {
                        let v = match (m.value, m.value_base64) {
                            (Some(v), None) => v.into_bytes(),
                            (None, Some(b)) => {
                                base64::engine::general_purpose::STANDARD.decode(b).map_err(|_| e::MALFORMED_JSON.clone())?
                            }
                            _ => return Err(e::MALFORMED_JSON.clone()),
                        };
                        if v.len() > MAX_MESSAGE || m.key.len() > 1024 {
                            return Err(e::ENTITY_TOO_LARGE.clone());
                        }
                        keys.push(m.key.clone());
                        out.push(encode(&Header { t: now, k: m.key, ct: m.content_type, ..Default::default() }, &v));
                    }
                    out
                } else {
                    if body.len() > MAX_MESSAGE {
                        return Err(e::ENTITY_TOO_LARGE.clone());
                    }
                    let h = |k: &str| parts.headers.get(k).and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
                    keys.push(h("x-cairn-key"));
                    vec![encode(&Header { t: now, k: h("x-cairn-key"), ct: h("content-type"), ..Default::default() }, &body)]
                };
                let n = msgs.len() as u64;
                let parts = tokio::task::block_in_place(|| self.eng.queue_topic(t))?
                    .and_then(|d| serde_json::from_slice::<TopicConfig>(&d).ok())
                    .ok_or_else(|| e::NO_SUCH_TOPIC.clone())?
                    .parts();
                // One partition for the request: the one asked for, the first key's, or the next in turn.
                let partition = match q.get("partition") {
                    Some(p) => p.parse::<u32>().ok().filter(|p| *p < parts).ok_or_else(|| e::INVALID_ARGUMENT.clone())?,
                    None => match keys.first().filter(|k| !k.is_empty()) {
                        Some(k) if parts > 1 => {
                            let p = key_partition(k.as_bytes(), parts);
                            if keys.iter().any(|o| key_partition(o.as_bytes(), parts) != p) {
                                return Err(e::MIXED_PARTITIONS.clone()); // a batch goes to one partition
                            }
                            p
                        }
                        _ => (self.queue_metrics.published.load(Ordering::Relaxed) % parts as u64) as u32,
                    },
                };
                let (first, last) = tokio::task::block_in_place(|| self.eng.queue_publish(&part_name(t, partition as i32), &msgs))
                    .map_err(|err| match err {
                        engine::Error::NoSuchKey => e::NO_SUCH_TOPIC.clone(),
                        other => other.into(),
                    })?;
                self.queue_metrics.published.fetch_add(n, Ordering::Relaxed);
                self.queue_notify.notify_waiters();
                let v = if q.contains_key("batch") {
                    json!({"first": first, "last": last, "partition": partition})
                } else {
                    json!({"seq": first, "partition": partition})
                };
                Ok(json_resp(StatusCode::CREATED, v))
            }
            (&Method::GET, ["topics", t, "messages"]) => {
                may(t, false)?;
                let max: usize = q.get("max").and_then(|v| v.parse().ok()).unwrap_or(100).clamp(1, MAX_BATCH);
                let wait = Duration::from_secs_f64(q.get("wait").and_then(|v| v.parse::<f64>().ok()).unwrap_or(0.0).clamp(0.0, 20.0));
                let group = q.get("group").cloned().unwrap_or_default();
                if !group.is_empty() && !valid_topic(&group) {
                    return Err(e::INVALID_ARGUMENT.clone());
                }
                let after: Option<u64> = match q.get("after") {
                    Some(v) => Some(v.parse().map_err(|_| e::INVALID_ARGUMENT.clone())?),
                    None if !group.is_empty() => None,
                    None => return Err(e::INVALID_ARGUMENT.clone()), // a group or a position
                };
                let partition: u32 =
                    q.get("partition").map(|p| p.parse().map_err(|_| e::INVALID_ARGUMENT.clone())).transpose()?.unwrap_or(0);
                let base = *t;
                let pname = part_name(base, partition as i32);
                let t = &pname.as_str();
                let deadline = tokio::time::Instant::now() + wait;
                loop {
                    let notified = self.queue_notify.notified();
                    let got = tokio::task::block_in_place(|| -> Result<(u64, Batch), ApiError> {
                        let cfg: TopicConfig =
                            serde_json::from_slice(&self.eng.queue_topic(base)?.ok_or_else(|| e::NO_SUCH_TOPIC.clone())?)
                                .unwrap_or_default();
                        if partition >= cfg.parts() {
                            return Err(e::INVALID_ARGUMENT.clone());
                        }
                        let from = match after {
                            Some(a) => a,
                            None => self.eng.queue_groups(t)?.into_iter().find(|(g, _)| *g == group).map(|(_, o)| o).unwrap_or(0),
                        };
                        Ok((from, self.eng.queue_read(t, from, max)?))
                    })?;
                    let (from, msgs) = got;
                    if !msgs.is_empty() || tokio::time::Instant::now() >= deadline {
                        let next = msgs.last().map(|(s, _)| *s).unwrap_or(from);
                        let out: Vec<Value> = msgs
                            .iter()
                            .filter_map(|(seq, raw)| {
                                let (h, v) = decode(raw)?;
                                let mut m = json!({"seq": seq, "time": super::iso_time(h.t),
                                                   "value_base64": base64::engine::general_purpose::STANDARD.encode(v)});
                                if !h.k.is_empty() {
                                    m["key"] = h.k.into();
                                }
                                if !h.ct.is_empty() {
                                    m["content_type"] = h.ct.into();
                                }
                                if let Ok(s) = std::str::from_utf8(v) {
                                    m["value"] = s.into();
                                }
                                Some(m)
                            })
                            .collect();
                        self.queue_metrics.delivered.fetch_add(out.len() as u64, Ordering::Relaxed);
                        return Ok(json_resp(
                            StatusCode::OK,
                            json!({"topic": base, "partition": partition, "messages": out, "next_after": next}),
                        ));
                    }
                    // Woken by a publish on this node, or polling for other nodes' publishes.
                    let _ = tokio::time::timeout(
                        deadline.saturating_duration_since(tokio::time::Instant::now()).min(Duration::from_secs(1)),
                        notified,
                    )
                    .await;
                }
            }
            (&Method::POST, ["topics", t, "groups", g]) => {
                may(t, false)?;
                if !valid_topic(g) {
                    return Err(e::INVALID_ARGUMENT.clone());
                }
                let v: Value = serde_json::from_slice(&body).map_err(|_| e::MALFORMED_JSON.clone())?;
                let seq = v.get("seq").and_then(Value::as_u64).ok_or_else(|| e::MALFORMED_JSON.clone())?;
                let partition = match v.get("partition") {
                    None => 0,
                    Some(p) => p.as_u64().filter(|p| *p < MAX_PARTITIONS as u64).ok_or_else(|| e::MALFORMED_JSON.clone())? as i32,
                };
                let pname = part_name(t, partition);
                let off = tokio::task::block_in_place(|| self.eng.queue_commit(&pname, g, seq)).map_err(|err| match err {
                    engine::Error::NoSuchKey => e::NO_SUCH_TOPIC.clone(),
                    engine::Error::InvalidPart => e::INVALID_ARGUMENT.clone(),
                    other => other.into(),
                })?;
                Ok(json_resp(StatusCode::OK, json!({"topic": t, "group": g, "partition": partition, "offset": off})))
            }
            _ => Err(e::ADMIN_NOT_FOUND.clone()),
        }
    }

    /// Applies retention to every topic, every `every` (one node at a time in a cluster).
    pub async fn run_queue_retention(self: Arc<Self>, every: Duration) {
        let mut tick = tokio::time::interval(every);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            if !self.may_run("queues") {
                continue;
            }
            let me = self.clone();
            let _ = tokio::task::spawn_blocking(move || {
                let now = engine::now_ms();
                for (name, cfg) in me.eng.queue_topics().unwrap_or_default() {
                    let cfg: TopicConfig = serde_json::from_slice(&cfg).unwrap_or_default();
                    let older = (cfg.retention_seconds > 0).then(|| now.saturating_sub(cfg.retention_seconds * 1000));
                    let keep = (cfg.max_messages > 0).then_some(cfg.max_messages);
                    if older.is_none() && keep.is_none() {
                        continue;
                    }
                    let is_old = |raw: &[u8]| older.is_some_and(|cut| decode(raw).is_some_and(|(h, _)| h.t < cut));
                    match me.eng.queue_trim(&name, keep, &is_old) {
                        Ok(n) if n > 0 => {
                            me.queue_metrics.trimmed.fetch_add(n, Ordering::Relaxed);
                            tracing::debug!(topic = %name, removed = n, "queue retention");
                        }
                        Ok(_) => {}
                        Err(err) => tracing::warn!(topic = %name, error = %err, "queue retention"),
                    }
                }
            })
            .await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn message_framing() {
        let raw = encode(&Header { t: 5, k: "order-1".into(), ct: "application/json".into(), ..Default::default() }, b"{\"a\":\n1}");
        let (h, v) = decode(&raw).unwrap();
        assert_eq!((h.t, h.k.as_str(), v), (5, "order-1", &b"{\"a\":\n1}"[..]));
        assert!(valid_topic("orders.v1") && !valid_topic("Orders") && !valid_topic(""));
        let r = decode_record(&encode_record(9, Some(&[0xff, 0x00]), &[("h".into(), vec![1, 2])], b"v")).unwrap();
        assert_eq!((r.key.as_deref(), r.headers[0].1.as_slice(), r.value.as_slice()), (Some(&[0xff, 0x00][..]), &[1u8, 2][..], &b"v"[..]));
    }

    #[test]
    fn murmur2_matches_kafka() {
        // Kafka's partitioner (toPositive(murmur2(key)) % n); murmur2("21") is -973932308 in Kafka's tests.
        assert_eq!(key_partition(b"21", 1000), 340);
        assert_eq!(key_partition(b"foobar", 1000), 166);
        assert_eq!(key_partition(b"a-little-bit-long-string", 1000), 112);
        assert_eq!(key_partition(b"abc", 1000), 107);
    }
}
