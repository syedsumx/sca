//! The Kafka protocol on the store's message queues, so Kafka clients (producers, consumers, consumer
//! groups) use its topics. Off by default; `CAIRN_STORE_KAFKA_ADDR` turns the listener on.
//!
//! - A Kafka topic is a queue topic and its partitions (`partitions` in the topic's settings). Offsets are
//!   the queue's sequence numbers minus one; committed group offsets are the queue's group offsets.
//! - Sign-in is SASL/PLAIN with an access key and its secret, and nothing but the version and SASL
//!   requests is answered before it. Keys reach topics as on the HTTP queue API (admin keys everything;
//!   others the topics their bucket patterns allow; read-only keys consume and commit only).
//! - Over TLS when the store has a certificate (SASL_SSL); without one, the listener starts only with
//!   `CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT=true`, since PLAIN sends the secret as it is.
//! - Requests: ApiVersions, SaslHandshake, SaslAuthenticate, Metadata, Produce (record batch v2,
//!   uncompressed), Fetch (with long polling), ListOffsets, CreateTopics (admin keys), FindCoordinator,
//!   OffsetCommit, OffsetFetch, JoinGroup, SyncGroup, Heartbeat and LeaveGroup, in their non-flexible
//!   versions. Transactions and idempotent producers are not offered.
//! - Group membership is kept by the node a group's members connect to (this node is every group's
//!   coordinator), so point all of a group's members at the same node or address.

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::{Mutex, Notify};

use super::queues::{self, TopicConfig};
use super::Server;
use crate::engine;

const MAX_FRAME: usize = 64 << 20;

// Error codes.
const NONE: i16 = 0;
const OFFSET_OUT_OF_RANGE: i16 = 1;
const CORRUPT_MESSAGE: i16 = 2;
const UNKNOWN_TOPIC: i16 = 3;
const MESSAGE_TOO_LARGE: i16 = 10;
const ILLEGAL_GENERATION: i16 = 22;
const INCONSISTENT_PROTOCOL: i16 = 23;
const INVALID_GROUP_ID: i16 = 24;
const UNKNOWN_MEMBER_ID: i16 = 25;
const REBALANCE_IN_PROGRESS: i16 = 27;
const TOPIC_AUTHORIZATION_FAILED: i16 = 29;
const GROUP_AUTHORIZATION_FAILED: i16 = 30;
const UNSUPPORTED_SASL_MECHANISM: i16 = 33;
const UNSUPPORTED_VERSION: i16 = 35;
const TOPIC_ALREADY_EXISTS: i16 = 36;
const INVALID_PARTITIONS: i16 = 37;
const INVALID_REPLICATION_FACTOR: i16 = 38;
const INVALID_REQUEST: i16 = 42;
const CLUSTER_AUTHORIZATION_FAILED: i16 = 31;
const SASL_AUTHENTICATION_FAILED: i16 = 58;
const UNSUPPORTED_COMPRESSION_TYPE: i16 = 76;
const INVALID_TOPIC: i16 = 17;

/// (api key, min version, max version) offered.
const APIS: [(i16, i16, i16); 15] = [
    (0, 3, 8),  // Produce
    (1, 4, 11), // Fetch
    (2, 1, 5),  // ListOffsets
    (3, 0, 8),  // Metadata
    (8, 2, 7),  // OffsetCommit
    (9, 1, 5),  // OffsetFetch
    (10, 0, 2), // FindCoordinator
    (11, 0, 5), // JoinGroup
    (12, 0, 3), // Heartbeat
    (13, 0, 3), // LeaveGroup
    (14, 0, 3), // SyncGroup
    (17, 0, 1), // SaslHandshake
    (18, 0, 2), // ApiVersions
    (19, 0, 4), // CreateTopics
    (36, 0, 1), // SaslAuthenticate
];

// ------------------------------------------------------------------------------------------- encoding

/// Reads a request body.
struct Rd<'a> {
    b: &'a [u8],
    bad: bool,
}

impl<'a> Rd<'a> {
    fn new(b: &'a [u8]) -> Rd<'a> {
        Rd { b, bad: false }
    }
    fn take(&mut self, n: usize) -> &'a [u8] {
        if self.b.len() < n {
            self.bad = true;
            let all = self.b;
            self.b = &[];
            return &all[..0];
        }
        let (h, t) = self.b.split_at(n);
        self.b = t;
        h
    }
    fn i8(&mut self) -> i8 {
        self.take(1).first().map(|v| *v as i8).unwrap_or(0)
    }
    fn i16(&mut self) -> i16 {
        self.take(2).try_into().map(i16::from_be_bytes).unwrap_or(0)
    }
    fn i32(&mut self) -> i32 {
        self.take(4).try_into().map(i32::from_be_bytes).unwrap_or(0)
    }
    fn i64(&mut self) -> i64 {
        self.take(8).try_into().map(i64::from_be_bytes).unwrap_or(0)
    }
    fn bool(&mut self) -> bool {
        self.i8() != 0
    }
    /// A string (None: null).
    fn nstr(&mut self) -> Option<String> {
        let n = self.i16();
        if n < 0 {
            return None;
        }
        Some(String::from_utf8_lossy(self.take(n as usize)).into_owned())
    }
    fn str(&mut self) -> String {
        self.nstr().unwrap_or_default()
    }
    fn nbytes(&mut self) -> Option<&'a [u8]> {
        let n = self.i32();
        if n < 0 {
            return None;
        }
        Some(self.take(n as usize))
    }
    /// An array's length (None: null). Bounded by what is left, so a bad count cannot run away.
    fn len(&mut self) -> Option<usize> {
        let n = self.i32();
        if n < 0 {
            return None;
        }
        if n as usize > self.b.len() {
            self.bad = true;
            return Some(0);
        }
        Some(n as usize)
    }
    fn varint(&mut self) -> i64 {
        let mut v: u64 = 0;
        for shift in (0..64).step_by(7) {
            let b = self.take(1);
            let Some(&b) = b.first() else { return 0 };
            v |= ((b & 0x7f) as u64) << shift;
            if b & 0x80 == 0 {
                return ((v >> 1) as i64) ^ -((v & 1) as i64);
            }
        }
        self.bad = true;
        0
    }
}

/// Writes a response body.
#[derive(Default)]
struct Wr(Vec<u8>);

impl Wr {
    fn i8(&mut self, v: i8) -> &mut Self {
        self.0.push(v as u8);
        self
    }
    fn i16(&mut self, v: i16) -> &mut Self {
        self.0.extend_from_slice(&v.to_be_bytes());
        self
    }
    fn i32(&mut self, v: i32) -> &mut Self {
        self.0.extend_from_slice(&v.to_be_bytes());
        self
    }
    fn i64(&mut self, v: i64) -> &mut Self {
        self.0.extend_from_slice(&v.to_be_bytes());
        self
    }
    fn bool(&mut self, v: bool) -> &mut Self {
        self.i8(v as i8)
    }
    fn str(&mut self, s: &str) -> &mut Self {
        self.i16(s.len() as i16);
        self.0.extend_from_slice(s.as_bytes());
        self
    }
    fn nstr(&mut self, s: Option<&str>) -> &mut Self {
        match s {
            Some(s) => self.str(s),
            None => self.i16(-1),
        }
    }
    fn bytes(&mut self, b: &[u8]) -> &mut Self {
        self.i32(b.len() as i32);
        self.0.extend_from_slice(b);
        self
    }
    fn arr(&mut self, n: usize) -> &mut Self {
        self.i32(n as i32)
    }
    fn varint(&mut self, v: i64) -> &mut Self {
        let mut z = ((v << 1) ^ (v >> 63)) as u64;
        loop {
            let b = (z & 0x7f) as u8;
            z >>= 7;
            if z == 0 {
                self.0.push(b);
                return self;
            }
            self.0.push(b | 0x80);
        }
    }
}

/// CRC-32C (Castagnoli), as record batches use.
fn crc32c(data: &[u8]) -> u32 {
    static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();
    let t = TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        for (i, e) in t.iter_mut().enumerate() {
            let mut c = i as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 { 0x82F6_3B78 ^ (c >> 1) } else { c >> 1 };
            }
            *e = c;
        }
        t
    });
    let mut c = !0u32;
    for b in data {
        c = t[((c ^ *b as u32) & 0xff) as usize] ^ (c >> 8);
    }
    !c
}

/// One record of a produced batch.
#[derive(Debug, PartialEq)]
pub(crate) struct Record {
    pub key: Option<Vec<u8>>,
    pub value: Vec<u8>,
    pub headers: Vec<(String, Vec<u8>)>,
    pub time: i64,
}

/// Parses the record batches of a Produce request (magic 2, uncompressed). Err: a Kafka error code.
pub(crate) fn parse_batches(mut b: &[u8]) -> Result<Vec<Record>, i16> {
    let mut out = vec![];
    while !b.is_empty() {
        if b.len() < 61 {
            return Err(CORRUPT_MESSAGE);
        }
        let len = i32::from_be_bytes(b[8..12].try_into().unwrap());
        if len < 49 || b.len() < 12 + len as usize {
            return Err(CORRUPT_MESSAGE);
        }
        let batch = &b[..12 + len as usize];
        b = &b[12 + len as usize..];
        if batch[16] != 2 {
            return Err(CORRUPT_MESSAGE); // only magic 2 (Kafka 0.11+ clients)
        }
        let crc = u32::from_be_bytes(batch[17..21].try_into().unwrap());
        if crc32c(&batch[21..]) != crc {
            return Err(CORRUPT_MESSAGE);
        }
        let mut r = Rd::new(&batch[21..]);
        let attributes = r.i16();
        if attributes & 0x07 != 0 {
            return Err(UNSUPPORTED_COMPRESSION_TYPE);
        }
        if attributes & 0x30 != 0 {
            return Err(INVALID_REQUEST); // transactional or control batches
        }
        let _last_delta = r.i32();
        let base_ts = r.i64();
        let _max_ts = r.i64();
        let _pid = r.i64();
        let _epoch = r.i16();
        let _seq = r.i32();
        let n = r.i32();
        for _ in 0..n.max(0) {
            let rlen = r.varint();
            if rlen < 0 || rlen as usize > r.b.len() {
                return Err(CORRUPT_MESSAGE);
            }
            let mut rr = Rd::new(r.take(rlen as usize));
            let _attr = rr.i8();
            let ts_delta = rr.varint();
            let _off_delta = rr.varint();
            let klen = rr.varint();
            let key = if klen < 0 { None } else { Some(rr.take(klen as usize).to_vec()) };
            let vlen = rr.varint();
            let value = if vlen < 0 { vec![] } else { rr.take(vlen as usize).to_vec() };
            let hn = rr.varint();
            let mut headers = vec![];
            for _ in 0..hn.max(0) {
                let hk = rr.varint();
                let k = String::from_utf8_lossy(rr.take(hk.max(0) as usize)).into_owned();
                let hv = rr.varint();
                let v = if hv < 0 { vec![] } else { rr.take(hv as usize).to_vec() };
                headers.push((k, v));
            }
            if rr.bad {
                return Err(CORRUPT_MESSAGE);
            }
            out.push(Record { key, value, headers, time: base_ts + ts_delta });
        }
        if r.bad {
            return Err(CORRUPT_MESSAGE);
        }
    }
    Ok(out)
}

/// One record batch (magic 2, uncompressed) holding `records` from `base` on.
pub(crate) fn build_batch(base: i64, records: &[Record]) -> Vec<u8> {
    let base_ts = records.first().map(|r| r.time).unwrap_or(0);
    let max_ts = records.iter().map(|r| r.time).max().unwrap_or(base_ts);
    let mut body = Wr::default();
    body.i16(0).i32(records.len() as i32 - 1).i64(base_ts).i64(max_ts).i64(-1).i16(-1).i32(-1).i32(records.len() as i32);
    for (i, r) in records.iter().enumerate() {
        let mut rec = Wr::default();
        rec.i8(0).varint(r.time - base_ts).varint(i as i64);
        match &r.key {
            Some(k) => {
                rec.varint(k.len() as i64);
                rec.0.extend_from_slice(k);
            }
            None => {
                rec.varint(-1);
            }
        }
        rec.varint(r.value.len() as i64);
        rec.0.extend_from_slice(&r.value);
        rec.varint(r.headers.len() as i64);
        for (k, v) in &r.headers {
            rec.varint(k.len() as i64);
            rec.0.extend_from_slice(k.as_bytes());
            rec.varint(v.len() as i64);
            rec.0.extend_from_slice(v);
        }
        body.varint(rec.0.len() as i64);
        body.0.extend_from_slice(&rec.0);
    }
    let crc = crc32c(&body.0);
    let mut out = Wr::default();
    out.i64(base).i32((4 + 1 + 4 + body.0.len()) as i32).i32(0).i8(2);
    out.0.extend_from_slice(&crc.to_be_bytes());
    out.0.extend_from_slice(&body.0);
    out.0
}

/// A produce answer per topic: (partition, error, base offset).
type Produced = Vec<(String, Vec<(i32, i16, i64)>)>;
/// A fetch request per topic: (partition, offset, partition max bytes).
type Wanted = Vec<(String, Vec<(i32, i64, usize)>)>;

// ---------------------------------------------------------------------------------------------- groups

#[derive(Default)]
struct Member {
    protocols: Vec<(String, Vec<u8>)>,
    session: Duration,
    seen: Option<Instant>,
    assignment: Option<Vec<u8>>,
}

#[derive(Default)]
struct Group {
    generation: i32,
    protocol_type: String,
    protocol: String,
    leader: String,
    members: BTreeMap<String, Member>,
    /// A rebalance is under way: members that have rejoined, and when it completes regardless.
    rebalance: Option<(std::collections::BTreeSet<String>, Instant)>,
    synced: bool,
}

/// The node's consumer group coordinator.
#[derive(Default)]
pub struct Groups {
    groups: Mutex<HashMap<String, Group>>,
    changed: Notify,
}

impl Group {
    /// Drops members whose session expired (rebalancing if any did).
    fn expire(&mut self) {
        let now = Instant::now();
        let gone: Vec<String> = self
            .members
            .iter()
            .filter(|(id, m)| {
                m.seen.is_some_and(|s| now.duration_since(s) > m.session)
                    && !self.rebalance.as_ref().is_some_and(|(joined, _)| joined.contains(*id))
            })
            .map(|(id, _)| id.clone())
            .collect();
        for id in &gone {
            self.members.remove(id);
        }
        if !gone.is_empty() && !self.members.is_empty() && self.rebalance.is_none() {
            self.rebalance = Some((Default::default(), now + Duration::from_secs(10)));
        }
    }

    /// Completes the rebalance when every member has rejoined or time is up.
    fn try_complete(&mut self) -> bool {
        let Some((joined, deadline)) = &self.rebalance else { return false };
        let all = self.members.keys().all(|m| joined.contains(m));
        if !(all || Instant::now() >= *deadline) || joined.is_empty() {
            return false;
        }
        let joined = joined.clone();
        self.members.retain(|id, _| joined.contains(id));
        self.generation += 1;
        if !self.members.contains_key(&self.leader) {
            self.leader = self.members.keys().next().cloned().unwrap_or_default();
        }
        // The leader's first protocol that every member supports.
        let ok = |p: &str| self.members.values().all(|m| m.protocols.iter().any(|(n, _)| n == p));
        self.protocol =
            self.members.get(&self.leader).and_then(|l| l.protocols.iter().map(|(n, _)| n.clone()).find(|n| ok(n))).unwrap_or_default();
        for m in self.members.values_mut() {
            m.assignment = None;
            m.seen = Some(Instant::now());
        }
        self.synced = false;
        self.rebalance = None;
        true
    }
}

// ------------------------------------------------------------------------------------------ the server

/// The listener's settings.
pub struct KafkaConfig {
    pub addr: String,
    pub advertised_host: String,
    pub advertised_port: i32,
}

/// What a connection knows about its client.
struct Conn {
    cred: Option<super::auth::Credential>,
    /// SaslHandshake v0 was answered: the next frame is a bare SASL token.
    raw_sasl: bool,
}

pub(crate) trait Io: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Io for T {}

/// Partition `p` of `topic` as a queue topic (partition 0 is the topic itself).
pub(crate) fn part_name(topic: &str, p: i32) -> String {
    if p == 0 {
        topic.to_string()
    } else {
        format!("{topic}#{p}")
    }
}

impl Server {
    /// Serves one Kafka connection.
    pub(crate) async fn kafka_conn(self: Arc<Self>, mut s: Box<dyn Io>) {
        let mut conn = Conn { cred: None, raw_sasl: false };
        loop {
            let mut len = [0u8; 4];
            if s.read_exact(&mut len).await.is_err() {
                return;
            }
            let n = i32::from_be_bytes(len);
            if n < 0 || n as usize > MAX_FRAME {
                return;
            }
            let mut frame = vec![0u8; n as usize];
            if s.read_exact(&mut frame).await.is_err() {
                return;
            }
            if conn.raw_sasl {
                // A bare PLAIN token (after SaslHandshake v0): an empty answer on success, else close.
                conn.raw_sasl = false;
                match self.kafka_plain(&frame) {
                    Some(c) => {
                        conn.cred = Some(c);
                        if s.write_all(&0i32.to_be_bytes()).await.is_err() {
                            return;
                        }
                        continue;
                    }
                    None => return,
                }
            }
            let mut r = Rd::new(&frame);
            let api = r.i16();
            let ver = r.i16();
            let corr = r.i32();
            let _client = r.nstr();
            let body = r.b;
            let resp = match self.kafka_request(&mut conn, api, ver, body).await {
                Some(b) => b,
                None => return, // unauthenticated or not understood: close, as Kafka brokers do
            };
            let mut out = Vec::with_capacity(resp.len() + 8);
            out.extend_from_slice(&((resp.len() + 4) as i32).to_be_bytes());
            out.extend_from_slice(&corr.to_be_bytes());
            out.extend_from_slice(&resp);
            if s.write_all(&out).await.is_err() || s.flush().await.is_err() {
                return;
            }
        }
    }

    /// Checks a SASL/PLAIN token: [authzid] NUL access key NUL secret.
    fn kafka_plain(&self, token: &[u8]) -> Option<super::auth::Credential> {
        let mut parts = token.split(|b| *b == 0);
        let authz = parts.next()?;
        let user = std::str::from_utf8(parts.next()?).ok()?;
        let pass = parts.next()?;
        if parts.next().is_some() || (!authz.is_empty() && authz != user.as_bytes()) {
            return None;
        }
        let ks = self.keyset();
        let c = ks.creds.get(user)?;
        use subtle::ConstantTimeEq;
        bool::from(c.secret_key.as_bytes().ct_eq(pass)).then(|| c.clone())
    }

    async fn kafka_request(&self, conn: &mut Conn, api: i16, ver: i16, body: &[u8]) -> Option<Vec<u8>> {
        let mut r = Rd::new(body);
        let mut w = Wr::default();
        if api == 18 {
            // ApiVersions: a version above what we offer gets a v0 answer with UNSUPPORTED_VERSION.
            let v = if ver > 2 { 0 } else { ver };
            w.i16(if ver > 2 { UNSUPPORTED_VERSION } else { NONE }).arr(APIS.len());
            for (k, min, max) in APIS {
                w.i16(k).i16(min).i16(max);
            }
            if v >= 1 {
                w.i32(0);
            }
            return Some(w.0);
        }
        let (min, max) = APIS.iter().find(|(k, _, _)| *k == api).map(|(_, a, b)| (*a, *b))?;
        if ver < min || ver > max {
            return None;
        }
        match api {
            17 => {
                let mech = r.str();
                let ok = mech == "PLAIN";
                w.i16(if ok { NONE } else { UNSUPPORTED_SASL_MECHANISM }).arr(1).str("PLAIN");
                conn.raw_sasl = ok && ver == 0;
                return Some(w.0);
            }
            36 => {
                let token = r.nbytes().unwrap_or_default();
                match self.kafka_plain(token) {
                    Some(c) => {
                        conn.cred = Some(c);
                        w.i16(NONE).nstr(None).bytes(&[]);
                    }
                    None => {
                        tracing::info!("kafka: SASL sign-in refused");
                        w.i16(SASL_AUTHENTICATION_FAILED).nstr(Some("Authentication failed: invalid access key or secret")).bytes(&[]);
                    }
                }
                if ver >= 1 {
                    w.i64(0);
                }
                return Some(w.0);
            }
            _ => {}
        }
        let cred = conn.cred.clone()?; // nothing else before sign-in
        let out = match api {
            3 => self.k_metadata(&cred, ver, &mut r),
            0 => self.k_produce(&cred, ver, &mut r),
            1 => self.k_fetch(&cred, ver, &mut r).await,
            2 => self.k_list_offsets(&cred, ver, &mut r),
            19 => self.k_create_topics(&cred, ver, &mut r),
            10 => {
                let _key = r.str();
                if ver >= 1 {
                    let _t = r.i8();
                    w.i32(0);
                }
                w.i16(NONE);
                if ver >= 1 {
                    w.nstr(None);
                }
                let k = self.kafka.as_ref()?;
                w.i32(0).str(&k.advertised_host).i32(k.advertised_port);
                Some(w.0)
            }
            8 => self.k_offset_commit(&cred, ver, &mut r).await,
            9 => self.k_offset_fetch(&cred, ver, &mut r),
            11 => self.k_join(&cred, ver, &mut r).await,
            14 => self.k_sync(&cred, ver, &mut r).await,
            12 => self.k_heartbeat(ver, &mut r).await,
            13 => self.k_leave(ver, &mut r).await,
            _ => None,
        }?;
        if r.bad {
            return None;
        }
        Some(out)
    }

    fn k_may(cred: &super::auth::Credential, topic: &str, write: bool) -> bool {
        queues::valid_topic(topic) && (cred.admin || (cred.allows(topic) && !(write && cred.read_only)))
    }

    /// A topic's settings, if it exists.
    fn k_topic(&self, topic: &str) -> Option<TopicConfig> {
        if !queues::valid_topic(topic) {
            return None;
        }
        let raw = tokio::task::block_in_place(|| self.eng.queue_topic(topic)).ok()??;
        Some(serde_json::from_slice(&raw).unwrap_or_default())
    }

    fn k_metadata(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd) -> Option<Vec<u8>> {
        let wanted: Option<Vec<String>> = r.len().map(|n| (0..n).map(|_| r.str()).collect());
        if ver >= 4 {
            let _auto = r.bool();
        }
        if ver >= 8 {
            let _a = r.bool();
            let _b = r.bool();
        }
        let k = self.kafka.as_ref()?;
        let mut w = Wr::default();
        if ver >= 3 {
            w.i32(0);
        }
        w.arr(1).i32(0).str(&k.advertised_host).i32(k.advertised_port);
        if ver >= 1 {
            w.nstr(None);
        }
        if ver >= 2 {
            w.nstr(Some("cairn-store"));
        }
        if ver >= 1 {
            w.i32(0);
        }
        let names: Vec<String> = match wanted {
            Some(v) => v,
            None => tokio::task::block_in_place(|| self.eng.queue_topics())
                .unwrap_or_default()
                .into_iter()
                .map(|(n, _)| n)
                .filter(|n| !n.contains('#') && (cred.admin || cred.allows(n)))
                .collect(),
        };
        w.arr(names.len());
        for name in &names {
            let (err, parts) = if !Self::k_may(cred, name, false) {
                (if queues::valid_topic(name) { TOPIC_AUTHORIZATION_FAILED } else { INVALID_TOPIC }, 0)
            } else {
                match self.k_topic(name) {
                    Some(cfg) => (NONE, cfg.partitions.max(1) as i32),
                    None => (UNKNOWN_TOPIC, 0),
                }
            };
            w.i16(err).str(name);
            if ver >= 1 {
                w.bool(false);
            }
            w.arr(parts as usize);
            for p in 0..parts {
                w.i16(NONE).i32(p).i32(0);
                if ver >= 7 {
                    w.i32(0);
                }
                w.arr(1).i32(0).arr(1).i32(0);
                if ver >= 5 {
                    w.arr(0);
                }
            }
            if ver >= 8 {
                w.i32(i32::MIN);
            }
        }
        if ver >= 8 {
            w.i32(i32::MIN);
        }
        Some(w.0)
    }

    fn k_produce(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd) -> Option<Vec<u8>> {
        let _txn = r.nstr();
        let _acks = r.i16();
        let _timeout = r.i32();
        let mut results: Produced = vec![];
        for _ in 0..r.len()? {
            let topic = r.str();
            let mut parts = vec![];
            for _ in 0..r.len()? {
                let p = r.i32();
                let records = r.nbytes().unwrap_or_default();
                parts.push((p, self.k_append(cred, &topic, p, records)));
            }
            results.push((topic, parts.into_iter().map(|(p, (e, o))| (p, e, o)).collect()));
        }
        let mut w = Wr::default();
        w.arr(results.len());
        for (t, parts) in &results {
            w.str(t).arr(parts.len());
            for (p, err, base) in parts {
                w.i32(*p).i16(*err).i64(*base).i64(-1);
                if ver >= 5 {
                    w.i64(-1);
                }
                if ver >= 8 {
                    w.arr(0).nstr(None);
                }
            }
        }
        w.i32(0);
        Some(w.0)
    }

    /// Appends a produced batch to a partition: (error, base offset).
    fn k_append(&self, cred: &super::auth::Credential, topic: &str, p: i32, records: &[u8]) -> (i16, i64) {
        if !Self::k_may(cred, topic, true) {
            return (TOPIC_AUTHORIZATION_FAILED, -1);
        }
        let Some(cfg) = self.k_topic(topic) else { return (UNKNOWN_TOPIC, -1) };
        if p < 0 || p >= cfg.partitions.max(1) as i32 {
            return (UNKNOWN_TOPIC, -1); // UNKNOWN_TOPIC_OR_PARTITION
        }
        let recs = match parse_batches(records) {
            Ok(r) => r,
            Err(code) => return (code, -1),
        };
        if recs.is_empty() {
            return (CORRUPT_MESSAGE, -1);
        }
        if recs.iter().any(|r| r.value.len() > queues::MAX_MESSAGE) {
            return (MESSAGE_TOO_LARGE, -1);
        }
        let now = engine::now_ms() as i64;
        let msgs: Vec<Vec<u8>> = recs
            .iter()
            .map(|r| queues::encode_record(r.time.clamp(0, now + 3_600_000) as u64, r.key.as_deref(), &r.headers, &r.value))
            .collect();
        match tokio::task::block_in_place(|| self.eng.queue_publish(&part_name(topic, p), &msgs)) {
            Ok((first, _)) => {
                self.queue_metrics.published.fetch_add(msgs.len() as u64, std::sync::atomic::Ordering::Relaxed);
                self.queue_notify.notify_waiters();
                (NONE, first as i64 - 1)
            }
            Err(engine::Error::NoSuchKey) => (UNKNOWN_TOPIC, -1),
            Err(err) => {
                tracing::warn!(topic, error = %err, "kafka produce");
                (CORRUPT_MESSAGE, -1)
            }
        }
    }

    async fn k_fetch(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd<'_>) -> Option<Vec<u8>> {
        let _replica = r.i32();
        let max_wait = r.i32().clamp(0, 30_000) as u64;
        let min_bytes = r.i32();
        let max_bytes = r.i32().max(1) as usize;
        let _iso = r.i8();
        if ver >= 7 {
            let _sid = r.i32();
            let _sep = r.i32();
        }
        let mut wanted: Wanted = vec![];
        for _ in 0..r.len()? {
            let t = r.str();
            let mut ps = vec![];
            for _ in 0..r.len()? {
                let p = r.i32();
                if ver >= 9 {
                    let _epoch = r.i32();
                }
                let off = r.i64();
                if ver >= 5 {
                    let _ls = r.i64();
                }
                let pmax = r.i32().max(1) as usize;
                ps.push((p, off, pmax));
            }
            wanted.push((t, ps));
        }
        if ver >= 7 {
            for _ in 0..r.len().unwrap_or(0) {
                let _t = r.str();
                for _ in 0..r.len().unwrap_or(0) {
                    let _p = r.i32();
                }
            }
        }
        if ver >= 11 {
            let _rack = r.str();
        }
        let deadline = tokio::time::Instant::now() + Duration::from_millis(max_wait);
        loop {
            let notified = self.queue_notify.notified();
            let (w, bytes) = self.k_fetch_once(cred, ver, &wanted, max_bytes);
            if bytes >= min_bytes.max(1) as usize || tokio::time::Instant::now() >= deadline {
                return Some(w);
            }
            let _ = tokio::time::timeout(
                deadline.saturating_duration_since(tokio::time::Instant::now()).min(Duration::from_millis(500)),
                notified,
            )
            .await;
        }
    }

    fn k_fetch_once(&self, cred: &super::auth::Credential, ver: i16, wanted: &Wanted, max_bytes: usize) -> (Vec<u8>, usize) {
        let mut w = Wr::default();
        w.i32(0);
        if ver >= 7 {
            w.i16(NONE).i32(0);
        }
        let mut total = 0usize;
        w.arr(wanted.len());
        for (t, ps) in wanted {
            w.str(t).arr(ps.len());
            let cfg = Self::k_may(cred, t, false).then(|| self.k_topic(t)).flatten();
            for (p, off, pmax) in ps {
                let (mut err, mut hw, mut start, mut batch) = (NONE, -1i64, -1i64, vec![]);
                match &cfg {
                    _ if !Self::k_may(cred, t, false) => err = TOPIC_AUTHORIZATION_FAILED,
                    None => err = UNKNOWN_TOPIC,
                    Some(c) if *p < 0 || *p >= c.partitions.max(1) as i32 => err = UNKNOWN_TOPIC,
                    Some(_) => {
                        let name = part_name(t, *p);
                        let got = tokio::task::block_in_place(|| -> crate::engine::Result<_> {
                            let (first, next) = self.eng.queue_bounds(&name)?;
                            Ok((first, next))
                        });
                        match got {
                            Ok((first, next)) => {
                                start = first as i64 - 1;
                                hw = next as i64 - 1;
                                if *off < start || *off > hw {
                                    err = OFFSET_OUT_OF_RANGE;
                                } else if *off < hw && total < max_bytes {
                                    let budget = (*pmax).min(max_bytes - total);
                                    let msgs =
                                        tokio::task::block_in_place(|| self.eng.queue_read(&name, *off as u64, 1000)).unwrap_or_default();
                                    let mut recs = vec![];
                                    let mut size = 0usize;
                                    for (_, raw) in &msgs {
                                        let Some(rec) = queues::decode_record(raw) else { continue };
                                        size += rec.value.len() + rec.key.as_ref().map_or(0, |k| k.len()) + 32;
                                        if !recs.is_empty() && size > budget {
                                            break;
                                        }
                                        recs.push(rec);
                                    }
                                    if !recs.is_empty() {
                                        self.queue_metrics.delivered.fetch_add(recs.len() as u64, std::sync::atomic::Ordering::Relaxed);
                                        batch = build_batch(*off, &recs);
                                    }
                                }
                            }
                            Err(_) => err = UNKNOWN_TOPIC,
                        }
                    }
                }
                total += batch.len();
                w.i32(*p).i16(err).i64(hw).i64(hw);
                if ver >= 5 {
                    w.i64(start);
                }
                w.arr(0);
                if ver >= 11 {
                    w.i32(-1);
                }
                w.bytes(&batch);
            }
        }
        (w.0, total)
    }

    fn k_list_offsets(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd) -> Option<Vec<u8>> {
        let _replica = r.i32();
        if ver >= 2 {
            let _iso = r.i8();
        }
        let mut w = Wr::default();
        if ver >= 2 {
            w.i32(0);
        }
        let n = r.len()?;
        w.arr(n);
        for _ in 0..n {
            let t = r.str();
            let np = r.len()?;
            w.str(&t).arr(np);
            let cfg = Self::k_may(cred, &t, false).then(|| self.k_topic(&t)).flatten();
            for _ in 0..np {
                let p = r.i32();
                if ver >= 4 {
                    let _e = r.i32();
                }
                let ts = r.i64();
                let (err, off) = match &cfg {
                    _ if !Self::k_may(cred, &t, false) => (TOPIC_AUTHORIZATION_FAILED, -1),
                    Some(c) if p >= 0 && p < c.partitions.max(1) as i32 => {
                        let name = part_name(&t, p);
                        match tokio::task::block_in_place(|| self.eng.queue_bounds(&name)) {
                            Ok((first, next)) => match ts {
                                -1 => (NONE, next as i64 - 1),
                                -2 => (NONE, first as i64 - 1),
                                ts => {
                                    // The first message at or after the time (the end if none).
                                    let mut found = next as i64 - 1;
                                    let mut after = first.saturating_sub(1);
                                    'scan: loop {
                                        let page =
                                            tokio::task::block_in_place(|| self.eng.queue_read(&name, after, 1000)).unwrap_or_default();
                                        if page.is_empty() {
                                            break;
                                        }
                                        for (seq, raw) in &page {
                                            if queues::decode_record(raw).is_some_and(|rec| rec.time >= ts) {
                                                found = *seq as i64 - 1;
                                                break 'scan;
                                            }
                                            after = *seq;
                                        }
                                    }
                                    (NONE, found)
                                }
                            },
                            Err(_) => (UNKNOWN_TOPIC, -1),
                        }
                    }
                    _ => (UNKNOWN_TOPIC, -1),
                };
                w.i32(p).i16(err).i64(-1).i64(off);
                if ver >= 4 {
                    w.i32(0);
                }
            }
        }
        Some(w.0)
    }

    fn k_create_topics(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd) -> Option<Vec<u8>> {
        let mut out = vec![];
        for _ in 0..r.len()? {
            let name = r.str();
            let parts = r.i32();
            let rf = r.i16();
            for _ in 0..r.len()? {
                let _p = r.i32();
                for _ in 0..r.len()? {
                    let _b = r.i32();
                }
            }
            let mut retention_ms: Option<i64> = None;
            for _ in 0..r.len()? {
                let k = r.str();
                let v = r.nstr();
                if k == "retention.ms" {
                    retention_ms = v.and_then(|v| v.parse().ok());
                }
            }
            out.push((name, parts, rf, retention_ms));
        }
        let _timeout = r.i32();
        let validate = ver >= 1 && r.bool();
        let mut w = Wr::default();
        if ver >= 2 {
            w.i32(0);
        }
        w.arr(out.len());
        for (name, parts, rf, retention_ms) in out {
            let parts = if parts == -1 { 1 } else { parts };
            let (err, msg) = if !cred.admin {
                (CLUSTER_AUTHORIZATION_FAILED, "Only admin keys create topics.")
            } else if !queues::valid_topic(&name) {
                (INVALID_TOPIC, "Topic names are lowercase letters, digits, . _ and -.")
            } else if !(1..=queues::MAX_PARTITIONS as i32).contains(&parts) {
                (INVALID_PARTITIONS, "Partitions must be 1 to 64.")
            } else if rf != -1 && rf != 1 {
                (INVALID_REPLICATION_FACTOR, "The store keeps its own copies: replication factor 1 (or -1).")
            } else if self.k_topic(&name).is_some() {
                (TOPIC_ALREADY_EXISTS, "The topic exists.")
            } else if validate {
                (NONE, "")
            } else {
                let cfg = TopicConfig {
                    partitions: parts as u32,
                    retention_seconds: retention_ms.filter(|m| *m > 0).map(|m| (m as u64).div_ceil(1000)).unwrap_or(0),
                    ..Default::default()
                };
                match tokio::task::block_in_place(|| self.queue_create(cred, &name, cfg)) {
                    Ok(_) => (NONE, ""),
                    Err(_) => (INVALID_REQUEST, "The topic could not be created."),
                }
            };
            w.str(&name).i16(err);
            if ver >= 1 {
                w.nstr(if msg.is_empty() { None } else { Some(msg) });
            }
        }
        Some(w.0)
    }

    fn k_group_ok(g: &str) -> bool {
        !g.is_empty() && g.len() <= 255 && !g.contains('#') && g.chars().all(|c| c.is_ascii_graphic())
    }

    async fn k_offset_commit(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd<'_>) -> Option<Vec<u8>> {
        let group = r.str();
        let generation = r.i32();
        let member = r.str();
        if ver >= 7 {
            let _gi = r.nstr();
        }
        if (2..=4).contains(&ver) {
            let _ret = r.i64();
        }
        let mut items = vec![];
        for _ in 0..r.len()? {
            let t = r.str();
            let mut ps = vec![];
            for _ in 0..r.len()? {
                let p = r.i32();
                let off = r.i64();
                if ver >= 6 {
                    let _e = r.i32();
                }
                let _meta = r.nstr();
                ps.push((p, off));
            }
            items.push((t, ps));
        }
        // A group member must commit in its current generation; generation -1 is a consumer without a group.
        let gen_err = if generation < 0 {
            NONE
        } else {
            let gs = self.kafka_groups.groups.lock().await;
            match gs.get(&group) {
                Some(g) if !g.members.contains_key(&member) => UNKNOWN_MEMBER_ID,
                Some(g) if g.generation != generation => ILLEGAL_GENERATION,
                Some(_) => NONE,
                None => UNKNOWN_MEMBER_ID,
            }
        };
        let mut w = Wr::default();
        if ver >= 3 {
            w.i32(0);
        }
        w.arr(items.len());
        for (t, ps) in items {
            w.str(&t).arr(ps.len());
            for (p, off) in ps {
                let err = if !Self::k_group_ok(&group) {
                    INVALID_GROUP_ID
                } else if gen_err != NONE {
                    gen_err
                } else if !Self::k_may(cred, &t, false) {
                    TOPIC_AUTHORIZATION_FAILED
                } else {
                    match self.k_topic(&t) {
                        Some(c) if p >= 0 && p < c.partitions.max(1) as i32 && off >= 0 => {
                            match tokio::task::block_in_place(|| self.eng.queue_set_offset(&part_name(&t, p), &group, off as u64)) {
                                Ok(()) => NONE,
                                Err(engine::Error::InvalidPart) => OFFSET_OUT_OF_RANGE,
                                Err(_) => UNKNOWN_TOPIC,
                            }
                        }
                        _ => UNKNOWN_TOPIC,
                    }
                };
                w.i32(p).i16(err);
            }
        }
        Some(w.0)
    }

    fn k_offset_fetch(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd) -> Option<Vec<u8>> {
        let group = r.str();
        let wanted: Option<Vec<(String, Vec<i32>)>> = r.len().map(|n| {
            (0..n)
                .map(|_| {
                    let t = r.str();
                    let ps = (0..r.len().unwrap_or(0)).map(|_| r.i32()).collect();
                    (t, ps)
                })
                .collect()
        });
        let wanted = wanted.unwrap_or_default(); // all topics: none tracked separately, so the answer is empty
        let mut w = Wr::default();
        if ver >= 3 {
            w.i32(0);
        }
        w.arr(wanted.len());
        for (t, ps) in wanted {
            w.str(&t).arr(ps.len());
            for p in ps {
                let (off, err) = if !Self::k_may(cred, &t, false) {
                    (-1, TOPIC_AUTHORIZATION_FAILED)
                } else {
                    let groups = tokio::task::block_in_place(|| self.eng.queue_groups(&part_name(&t, p))).unwrap_or_default();
                    (groups.into_iter().find(|(g, _)| *g == group).map(|(_, o)| o as i64).unwrap_or(-1), NONE)
                };
                w.i32(p).i64(off);
                if ver >= 5 {
                    w.i32(-1);
                }
                w.nstr(Some("")).i16(err);
            }
        }
        if ver >= 2 {
            w.i16(if Self::k_group_ok(&group) { NONE } else { INVALID_GROUP_ID });
        }
        Some(w.0)
    }

    async fn k_join(&self, cred: &super::auth::Credential, ver: i16, r: &mut Rd<'_>) -> Option<Vec<u8>> {
        let group = r.str();
        let session = r.i32().clamp(1000, 300_000) as u64;
        let rebalance = if ver >= 1 { r.i32().clamp(1000, 300_000) as u64 } else { session };
        let mut member = r.str();
        if ver >= 5 {
            let _gi = r.nstr();
        }
        let ptype = r.str();
        let mut protocols = vec![];
        for _ in 0..r.len()? {
            let n = r.str();
            let m = r.nbytes().unwrap_or_default().to_vec();
            protocols.push((n, m));
        }
        let mut w = Wr::default();
        if ver >= 2 {
            w.i32(0);
        }
        let fail = |w: &mut Wr, code: i16, member: &str| {
            w.i16(code).i32(-1).str("").str("").str(member).arr(0);
            std::mem::take(&mut w.0)
        };
        if !Self::k_group_ok(&group) {
            return Some(fail(&mut w, INVALID_GROUP_ID, &member));
        }
        if !(cred.admin || cred.allows(&group)) {
            return Some(fail(&mut w, GROUP_AUTHORIZATION_FAILED, &member));
        }
        {
            let mut gs = self.kafka_groups.groups.lock().await;
            let g = gs.entry(group.clone()).or_default();
            g.expire();
            if !g.members.is_empty() && g.protocol_type != ptype {
                return Some(fail(&mut w, INCONSISTENT_PROTOCOL, &member));
            }
            if member.is_empty() {
                member = format!("cairn-{}", hex::encode(rand::random::<[u8; 8]>()));
            } else if !g.members.contains_key(&member) {
                return Some(fail(&mut w, UNKNOWN_MEMBER_ID, &member));
            }
            g.protocol_type = ptype;
            let m = g.members.entry(member.clone()).or_default();
            m.protocols = protocols;
            m.session = Duration::from_millis(session);
            m.seen = Some(Instant::now());
            let wait = Duration::from_millis(rebalance.min(10_000));
            match &mut g.rebalance {
                Some((joined, _)) => {
                    joined.insert(member.clone());
                }
                None => g.rebalance = Some(([member.clone()].into_iter().collect(), Instant::now() + wait)),
            }
            if g.try_complete() {
                self.kafka_groups.changed.notify_waiters();
            }
        }
        // Wait for the rebalance this member joined to complete.
        let end = Instant::now() + Duration::from_millis(rebalance + 1000);
        loop {
            let changed = self.kafka_groups.changed.notified();
            {
                let mut gs = self.kafka_groups.groups.lock().await;
                let g = gs.get_mut(&group)?;
                if g.try_complete() {
                    self.kafka_groups.changed.notify_waiters();
                }
                if g.rebalance.is_none() || !g.rebalance.as_ref().is_some_and(|(j, _)| j.contains(&member)) {
                    if !g.members.contains_key(&member) {
                        return Some(fail(&mut w, UNKNOWN_MEMBER_ID, &member));
                    }
                    w.i16(NONE).i32(g.generation).str(&g.protocol).str(&g.leader).str(&member);
                    if member == g.leader {
                        w.arr(g.members.len());
                        for (id, m) in &g.members {
                            w.str(id);
                            if ver >= 5 {
                                w.nstr(None);
                            }
                            let meta = m.protocols.iter().find(|(n, _)| *n == g.protocol).map(|(_, b)| b.as_slice()).unwrap_or(&[]);
                            w.bytes(meta);
                        }
                    } else {
                        w.arr(0);
                    }
                    return Some(w.0);
                }
            }
            if Instant::now() > end {
                return Some(fail(&mut w, REBALANCE_IN_PROGRESS, &member));
            }
            let _ = tokio::time::timeout(Duration::from_millis(250), changed).await;
        }
    }

    async fn k_sync(&self, _cred: &super::auth::Credential, ver: i16, r: &mut Rd<'_>) -> Option<Vec<u8>> {
        let group = r.str();
        let generation = r.i32();
        let member = r.str();
        if ver >= 3 {
            let _gi = r.nstr();
        }
        let mut assignments = vec![];
        for _ in 0..r.len()? {
            let id = r.str();
            let a = r.nbytes().unwrap_or_default().to_vec();
            assignments.push((id, a));
        }
        let mut w = Wr::default();
        if ver >= 1 {
            w.i32(0);
        }
        let end = Instant::now() + Duration::from_secs(30);
        loop {
            let changed = self.kafka_groups.changed.notified();
            {
                let mut gs = self.kafka_groups.groups.lock().await;
                let Some(g) = gs.get_mut(&group) else {
                    w.i16(UNKNOWN_MEMBER_ID).bytes(&[]);
                    return Some(w.0);
                };
                let code = if !g.members.contains_key(&member) {
                    UNKNOWN_MEMBER_ID
                } else if g.rebalance.is_some() {
                    REBALANCE_IN_PROGRESS
                } else if g.generation != generation {
                    ILLEGAL_GENERATION
                } else {
                    NONE
                };
                if code != NONE {
                    w.i16(code).bytes(&[]);
                    return Some(w.0);
                }
                if member == g.leader && !g.synced {
                    for (id, a) in &assignments {
                        if let Some(m) = g.members.get_mut(id) {
                            m.assignment = Some(a.clone());
                        }
                    }
                    for m in g.members.values_mut() {
                        m.assignment.get_or_insert_with(Vec::new);
                    }
                    g.synced = true;
                    self.kafka_groups.changed.notify_waiters();
                }
                if g.synced {
                    let m = g.members.get_mut(&member)?;
                    m.seen = Some(Instant::now());
                    w.i16(NONE).bytes(m.assignment.as_deref().unwrap_or(&[]));
                    return Some(w.0);
                }
            }
            if Instant::now() > end {
                w.i16(REBALANCE_IN_PROGRESS).bytes(&[]);
                return Some(w.0);
            }
            let _ = tokio::time::timeout(Duration::from_millis(250), changed).await;
        }
    }

    async fn k_heartbeat(&self, ver: i16, r: &mut Rd<'_>) -> Option<Vec<u8>> {
        let group = r.str();
        let generation = r.i32();
        let member = r.str();
        if ver >= 3 {
            let _gi = r.nstr();
        }
        let mut gs = self.kafka_groups.groups.lock().await;
        let code = match gs.get_mut(&group) {
            None => UNKNOWN_MEMBER_ID,
            Some(g) => {
                g.expire();
                if g.try_complete() {
                    self.kafka_groups.changed.notify_waiters();
                }
                match g.members.get_mut(&member) {
                    None => UNKNOWN_MEMBER_ID,
                    Some(m) => {
                        m.seen = Some(Instant::now());
                        if g.rebalance.is_some() {
                            REBALANCE_IN_PROGRESS
                        } else if g.generation != generation {
                            ILLEGAL_GENERATION
                        } else {
                            NONE
                        }
                    }
                }
            }
        };
        let mut w = Wr::default();
        if ver >= 1 {
            w.i32(0);
        }
        w.i16(code);
        Some(w.0)
    }

    async fn k_leave(&self, ver: i16, r: &mut Rd<'_>) -> Option<Vec<u8>> {
        let group = r.str();
        let members: Vec<String> = if ver >= 3 {
            (0..r.len()?)
                .map(|_| {
                    let id = r.str();
                    let _gi = r.nstr();
                    id
                })
                .collect()
        } else {
            vec![r.str()]
        };
        let mut gs = self.kafka_groups.groups.lock().await;
        let mut codes = vec![];
        if let Some(g) = gs.get_mut(&group) {
            for m in &members {
                codes.push(if g.members.remove(m).is_some() { NONE } else { UNKNOWN_MEMBER_ID });
                if let Some((joined, _)) = &mut g.rebalance {
                    joined.remove(m);
                }
            }
            if !g.members.is_empty() && g.rebalance.is_none() {
                g.rebalance = Some((Default::default(), Instant::now() + Duration::from_secs(10)));
            }
            if g.members.is_empty() {
                gs.remove(&group);
            }
            self.kafka_groups.changed.notify_waiters();
        } else {
            codes = vec![UNKNOWN_MEMBER_ID; members.len()];
        }
        let mut w = Wr::default();
        if ver >= 1 {
            w.i32(0);
        }
        if ver >= 3 {
            w.i16(NONE).arr(members.len());
            for (m, c) in members.iter().zip(&codes) {
                w.str(m).nstr(None).i16(*c);
            }
        } else {
            w.i16(codes.first().copied().unwrap_or(UNKNOWN_MEMBER_ID));
        }
        Some(w.0)
    }

    /// Accepts Kafka connections forever (over TLS when the store has a certificate).
    pub async fn run_kafka(self: Arc<Self>, listener: tokio::net::TcpListener, tls: Option<Arc<crate::tls::Reloading>>) {
        loop {
            let Ok((sock, peer)) = listener.accept().await else { continue };
            let _ = sock.set_nodelay(true);
            let me = self.clone();
            let tls = tls.clone();
            tokio::spawn(async move {
                let io: Box<dyn Io> = match tls {
                    Some(t) => match tokio::time::timeout(Duration::from_secs(10), t.acceptor().accept(sock)).await {
                        Ok(Ok(s)) => Box::new(s),
                        _ => {
                            tracing::debug!(%peer, "kafka: TLS handshake failed");
                            return;
                        }
                    },
                    None => Box::new(sock),
                };
                me.kafka_conn(io).await;
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32c_known_value() {
        assert_eq!(crc32c(b"123456789"), 0xE306_9283);
    }

    #[test]
    fn batches_round_trip() {
        let recs = vec![
            Record {
                key: Some(b"k1".to_vec()),
                value: b"hello".to_vec(),
                headers: vec![("h".into(), b"v".to_vec())],
                time: 1_700_000_000_000,
            },
            Record { key: None, value: vec![], headers: vec![], time: 1_700_000_000_005 },
        ];
        let b = build_batch(41, &recs);
        assert_eq!(parse_batches(&b).unwrap(), recs);
        let mut bad = b.clone();
        *bad.last_mut().unwrap() ^= 1;
        assert_eq!(parse_batches(&bad), Err(CORRUPT_MESSAGE));
        let mut two = b.clone();
        two.extend_from_slice(&b);
        assert_eq!(parse_batches(&two).unwrap().len(), 4);
    }

    #[test]
    fn varints() {
        for v in [0i64, 1, -1, 63, -64, 300, -300, i32::MAX as i64, i64::MIN / 2] {
            let mut w = Wr::default();
            w.varint(v);
            assert_eq!(Rd::new(&w.0).varint(), v);
        }
    }
}
