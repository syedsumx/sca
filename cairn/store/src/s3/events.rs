//! Event notifications: a bucket's notification configuration, and delivery of the events in the
//! engine's outbox to the operator's targets (webhooks and Kafka topics).
//!
//! Targets are defined by the operator (`CAIRN_STORE_EVENT_TARGETS_FILE`), never by bucket owners: a
//! notification rule names a target by id (as the last part of the destination ARN, e.g.
//! `arn:cairn:store:::lake-events`). Events are written to the outbox in the same transaction as the
//! change, and delivered at least once, in order per target, with retries and backoff. An event older than
//! `CAIRN_STORE_EVENT_MAX_AGE_HOURS` (default 168) is dropped and counted. Webhook bodies are signed:
//! `X-Cairn-Signature: sha256=<hex HMAC-SHA256(secret, body)>`.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::Full;
use hyper::StatusCode;
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use serde::Deserialize;
use serde_json::json;
use tokio::sync::Mutex;

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, iso_time, xml_resp, Ctx, Resp, Server};
use crate::engine::{self, NotifyRule, OutboxEntry};

type R = Result<Resp, ApiError>;

/// The events Cairn Store emits (patterns may end in `*`).
const EVENTS: [&str; 13] = [
    "s3:ObjectCreated:*",
    "s3:ObjectCreated:Put",
    "s3:ObjectCreated:Post",
    "s3:ObjectCreated:Copy",
    "s3:ObjectCreated:CompleteMultipartUpload",
    "s3:ObjectRemoved:*",
    "s3:ObjectRemoved:Delete",
    "s3:ObjectRemoved:DeleteMarkerCreated",
    "s3:ObjectTagging:*",
    "s3:ObjectTagging:Put",
    "s3:ObjectTagging:Delete",
    "s3:LifecycleExpiration:*",
    "s3:LifecycleExpiration:Delete",
];

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

/// An operator-defined delivery target.
#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase", deny_unknown_fields)]
pub enum Target {
    Webhook {
        id: String,
        url: String,
        #[serde(default)]
        secret: String,
        #[serde(default)]
        secret_file: String,
    },
    Kafka {
        id: String,
        brokers: Vec<String>,
        topic: String,
        #[serde(default)]
        partition: i32,
    },
}

impl Target {
    pub fn id(&self) -> &str {
        match self {
            Target::Webhook { id, .. } | Target::Kafka { id, .. } => id,
        }
    }
}

/// Loads and checks the targets file.
pub fn load_targets(text: &str) -> Result<Vec<Target>, String> {
    let mut ts: Vec<Target> = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let mut seen = HashSet::new();
    for t in ts.iter_mut() {
        if !seen.insert(t.id().to_string()) || t.id().is_empty() {
            return Err(format!("target ids must be unique and non-empty ({:?})", t.id()));
        }
        match t {
            Target::Webhook { url, secret, secret_file, id } => {
                if !url.starts_with("http://") {
                    return Err(format!("target {id}: webhook URLs must be http:// (inside the deployment) for now"));
                }
                if !secret_file.is_empty() {
                    *secret = std::fs::read_to_string(&*secret_file).map_err(|e| format!("{secret_file}: {e}"))?.trim().to_string();
                }
                if secret.len() < 16 {
                    return Err(format!("target {id}: a webhook secret of at least 16 characters is required"));
                }
            }
            Target::Kafka { brokers, topic, id, .. } => {
                if brokers.is_empty() || topic.is_empty() {
                    return Err(format!("target {id}: brokers and topic are required"));
                }
            }
        }
    }
    Ok(ts)
}

#[derive(Default)]
pub struct EventMetrics {
    pub delivered: AtomicU64,
    pub failed: AtomicU64,
    pub dropped: AtomicU64,
}

/// The S3 event record for an outbox entry.
fn record(region: &str, seq: u64, e: &OutboxEntry) -> serde_json::Value {
    let mut object = json!({
        "key": super::auth::aws_encode(e.key.as_bytes(), true),
        "sequencer": format!("{seq:016X}"),
    });
    if !e.event.contains("DeleteMarkerCreated") && !e.event.contains("Removed") && !e.event.contains("Expiration") {
        object["size"] = json!(e.size);
        object["eTag"] = json!(e.etag);
    }
    if !e.version.is_empty() {
        object["versionId"] = json!(e.version);
    }
    json!({
        "eventVersion": "2.1",
        "eventSource": "cairn:s3",
        "awsRegion": region,
        "eventTime": iso_time(e.time),
        "eventName": e.event.trim_start_matches("s3:"),
        "userIdentity": {"principalId": e.principal},
        "requestParameters": {"sourceIPAddress": e.source_ip},
        "responseElements": {"x-amz-request-id": e.request_id},
        "s3": {
            "s3SchemaVersion": "1.0",
            "configurationId": e.rule,
            "bucket": {"name": e.bucket, "arn": format!("arn:aws:s3:::{}", e.bucket)},
            "object": object,
        },
    })
}

pub struct Delivery {
    targets: HashMap<String, Target>,
    http: Client<HttpConnector, Full<Bytes>>,
    kafka: Mutex<HashMap<String, Arc<rskafka::client::partition::PartitionClient>>>,
    max_age_ms: u64,
}

impl Delivery {
    pub fn new(targets: Vec<Target>, max_age_hours: u64) -> Delivery {
        let mut conn = HttpConnector::new();
        conn.set_connect_timeout(Some(Duration::from_secs(5)));
        Delivery {
            targets: targets.into_iter().map(|t| (t.id().to_string(), t)).collect(),
            http: Client::builder(TokioExecutor::new()).build(conn),
            kafka: Mutex::new(HashMap::new()),
            max_age_ms: max_age_hours.max(1) * 3_600_000,
        }
    }

    pub fn has(&self, id: &str) -> bool {
        self.targets.contains_key(id)
    }

    async fn send(&self, t: &Target, seq: u64, body: Vec<u8>, key: &str) -> Result<(), String> {
        match t {
            Target::Webhook { url, secret, .. } => {
                let sig = hex::encode(super::auth::hmac_sha256(secret.as_bytes(), &body));
                let req = hyper::Request::post(url)
                    .header("content-type", "application/json")
                    .header("x-cairn-event-id", seq.to_string())
                    .header("x-cairn-signature", format!("sha256={sig}"))
                    .body(Full::new(Bytes::from(body)))
                    .map_err(|e| e.to_string())?;
                let resp = tokio::time::timeout(Duration::from_secs(10), self.http.request(req))
                    .await
                    .map_err(|_| "timed out".to_string())?
                    .map_err(|e| e.to_string())?;
                if resp.status().is_success() {
                    Ok(())
                } else {
                    Err(format!("HTTP {}", resp.status()))
                }
            }
            Target::Kafka { id, brokers, topic, partition } => {
                let pc = {
                    let mut cache = self.kafka.lock().await;
                    match cache.get(id) {
                        Some(pc) => pc.clone(),
                        None => {
                            let client =
                                tokio::time::timeout(Duration::from_secs(10), rskafka::client::ClientBuilder::new(brokers.clone()).build())
                                    .await
                                    .map_err(|_| "kafka connect timed out".to_string())?
                                    .map_err(|e| e.to_string())?;
                            let pc = Arc::new(
                                client
                                    .partition_client(topic.clone(), *partition, rskafka::client::partition::UnknownTopicHandling::Error)
                                    .await
                                    .map_err(|e| e.to_string())?,
                            );
                            cache.insert(id.clone(), pc.clone());
                            pc
                        }
                    }
                };
                let rec = rskafka::record::Record {
                    key: Some(key.as_bytes().to_vec()),
                    value: Some(body),
                    headers: BTreeMap::from([("x-cairn-event-id".to_string(), seq.to_string().into_bytes())]),
                    timestamp: rskafka::chrono::DateTime::from_timestamp_millis(engine::now_ms() as i64).unwrap_or_default(),
                };
                let res = tokio::time::timeout(
                    Duration::from_secs(10),
                    pc.produce(vec![rec], rskafka::client::partition::Compression::NoCompression),
                )
                .await;
                match res {
                    Ok(Ok(_)) => Ok(()),
                    Ok(Err(err)) => {
                        self.kafka.lock().await.remove(id); // reconnect next time
                        Err(err.to_string())
                    }
                    Err(_) => {
                        self.kafka.lock().await.remove(id);
                        Err("kafka produce timed out".into())
                    }
                }
            }
        }
    }
}

/// Backoff before retry `attempts` (1 s doubling, at most 5 minutes).
fn backoff_ms(attempts: u32) -> u64 {
    1000u64.saturating_mul(1u64 << attempts.min(9)).min(300_000)
}

impl Server {
    /// The event context for a change made by this request.
    pub(crate) fn ev(&self, c: &Ctx, kind: &'static str) -> engine::Ev {
        engine::Ev {
            kind,
            principal: c.auth.cred.access_key.clone(),
            source_ip: c.peer.map(|p| p.to_string()).unwrap_or_default(),
            request_id: c.request_id.clone(),
            lifecycle: false,
            audit: None,
        }
    }

    pub(crate) async fn put_notification<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 256 << 10).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "NotificationConfiguration")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        let mut rules: Vec<NotifyRule> = vec![];
        for el in &root.children {
            let arn_tag = match el.name.as_str() {
                "QueueConfiguration" => "Queue",
                "TopicConfiguration" => "Topic",
                "CloudFunctionConfiguration" => "CloudFunction",
                "EventBridgeConfiguration" => return Err(e::EVENTBRIDGE_UNSUPPORTED.clone()),
                _ => return Err(e::MALFORMED_XML.clone()),
            };
            let arn = el.text_of(arn_tag);
            let target = arn.rsplit(':').next().unwrap_or("").to_string();
            if !self.delivery.as_ref().is_some_and(|d| d.has(&target)) {
                return Err(e::EVENT_TARGET_UNKNOWN.clone());
            }
            let events: Vec<String> = el.all("Event").map(|e| e.text.trim().to_string()).collect();
            if events.is_empty() || events.iter().any(|ev| !EVENTS.contains(&ev.as_str())) {
                return Err(e::EVENT_NAME_UNSUPPORTED.clone());
            }
            let (mut prefix, mut suffix) = (String::new(), String::new());
            if let Some(key) = el.child("Filter").and_then(|f| f.child("S3Key")) {
                for fr in key.all("FilterRule") {
                    match fr.text_of("Name").to_ascii_lowercase().as_str() {
                        "prefix" if prefix.is_empty() => prefix = fr.text_of_raw("Value"),
                        "suffix" if suffix.is_empty() => suffix = fr.text_of_raw("Value"),
                        _ => return Err(e::INVALID_ARGUMENT.clone()),
                    }
                }
            }
            let id = match el.text_of("Id") {
                v if v.is_empty() => engine::new_id()[..12].to_string(),
                v => v,
            };
            rules.push(NotifyRule { id, target, events, prefix, suffix, arn, kind: arn_tag.to_string() });
        }
        if rules.len() > 100 {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        let v = serde_json::to_vec(&rules).map_err(|_| e::INTERNAL.clone())?;
        self.set_config(c, "notification", if rules.is_empty() { None } else { Some(&v) })?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_notification(&self, c: &Ctx) -> R {
        let rules: Vec<NotifyRule> = match blocking(|| self.eng.get_config(&c.bucket, "notification"))? {
            Some(d) => serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?,
            None => vec![],
        };
        let mut w = Writer::new("NotificationConfiguration");
        for r in &rules {
            let tag: &'static str = match r.kind.as_str() {
                "Topic" => "TopicConfiguration",
                "CloudFunction" => "CloudFunctionConfiguration",
                _ => "QueueConfiguration",
            };
            let arn_tag: &'static str = match r.kind.as_str() {
                "Topic" => "Topic",
                "CloudFunction" => "CloudFunction",
                _ => "Queue",
            };
            w.open(tag).el("Id", &r.id).el(arn_tag, &r.arn);
            for ev in &r.events {
                w.el("Event", ev);
            }
            if !r.prefix.is_empty() || !r.suffix.is_empty() {
                w.open("Filter").open("S3Key");
                for (n, v) in [("prefix", &r.prefix), ("suffix", &r.suffix)] {
                    if !v.is_empty() {
                        w.open("FilterRule").el("Name", n).el("Value", v).close();
                    }
                }
                w.close().close();
            }
            w.close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    /// Delivers what is in the outbox, once. Returns the number of events delivered.
    pub async fn deliver_events(&self) -> usize {
        let Some(d) = &self.delivery else { return 0 };
        let batch = match blocking(|| self.eng.outbox(500)) {
            Ok(b) => b,
            Err(err) => {
                tracing::warn!(error = %err, "event outbox");
                return 0;
            }
        };
        let now = engine::now_ms();
        let (mut done, mut retry, mut blocked) = (vec![], vec![], HashSet::new());
        let mut delivered = 0;
        for (seq, mut ent) in batch {
            if blocked.contains(&ent.target) {
                continue; // keep per-target order: wait until the earlier event goes through
            }
            let Some(t) = d.targets.get(&ent.target) else {
                tracing::warn!(target = %ent.target, seq, "event target no longer configured; dropping the event");
                self.event_metrics.dropped.fetch_add(1, Ordering::Relaxed);
                done.push(seq);
                continue;
            };
            if now > ent.time + d.max_age_ms {
                tracing::error!(target = %ent.target, seq, attempts = ent.attempts, "event undeliverable past its maximum age; dropping it");
                self.event_metrics.dropped.fetch_add(1, Ordering::Relaxed);
                done.push(seq);
                continue;
            }
            if now < ent.next_at {
                blocked.insert(ent.target.clone());
                continue;
            }
            let body = json!({"Records": [record(&self.region, seq, &ent)]}).to_string().into_bytes();
            match d.send(t, seq, body, &format!("{}/{}", ent.bucket, ent.key)).await {
                Ok(()) => {
                    self.event_metrics.delivered.fetch_add(1, Ordering::Relaxed);
                    delivered += 1;
                    done.push(seq);
                }
                Err(err) => {
                    self.event_metrics.failed.fetch_add(1, Ordering::Relaxed);
                    ent.attempts += 1;
                    ent.next_at = now + backoff_ms(ent.attempts);
                    tracing::warn!(target = %ent.target, seq, attempts = ent.attempts, error = %err, "event delivery failed; will retry");
                    blocked.insert(ent.target.clone());
                    retry.push((seq, ent));
                }
            }
        }
        if !done.is_empty() || !retry.is_empty() {
            if let Err(err) = blocking(|| self.eng.outbox_done(&done, &retry)) {
                tracing::warn!(error = %err, "event outbox");
            }
        }
        delivered
    }

    /// Delivers events forever.
    pub async fn run_events(self: Arc<Self>) {
        loop {
            let n = self.deliver_events().await;
            tokio::time::sleep(Duration::from_millis(if n > 0 { 10 } else { 500 })).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn targets_and_rules() {
        let ts = load_targets(
            r#"[{"type":"webhook","id":"cairn","url":"http://cairn-api:8000/hook","secret":"0123456789abcdef"},
                                   {"type":"kafka","id":"lake","brokers":["kafka:9092"],"topic":"cairn.store.events"}]"#,
        )
        .unwrap();
        assert_eq!(ts.len(), 2);
        assert!(load_targets(r#"[{"type":"webhook","id":"x","url":"http://a","secret":"short"}]"#).is_err());
        assert!(load_targets(r#"[{"type":"webhook","id":"x","url":"ftp://a","secret":"0123456789abcdef"}]"#).is_err());
        assert!(load_targets(r#"[{"type":"kafka","id":"a","brokers":[],"topic":"t"}]"#).is_err());
        let r = NotifyRule {
            id: "r".into(),
            target: "t".into(),
            events: vec!["s3:ObjectCreated:*".into()],
            prefix: "raw/".into(),
            suffix: ".parquet".into(),
            ..Default::default()
        };
        assert!(r.matches("s3:ObjectCreated:Put", "raw/a.parquet"));
        assert!(!r.matches("s3:ObjectRemoved:Delete", "raw/a.parquet"));
        assert!(!r.matches("s3:ObjectCreated:Put", "raw/a.csv"));
        assert_eq!(backoff_ms(1), 2000);
        assert_eq!(backoff_ms(20), 300_000);
    }
}
