// SPDX-License-Identifier: Apache-2.0
//! Model calls: an OpenAI-compatible chat-completions endpoint for structured JSON answers and an embeddings
//! endpoint, each configured by URL, model and an API key (from a file or the environment, never logged).
//! In Cairn both are Cairn's AI gateway, so every call is governed and metered under the compiler's agent.
//!
//! Structured answers ask for `response_format: json_schema` and are validated by deserialising them into the
//! Rust types (unknown fields refused where the upstream models refuse them). A call that fails, times out, or
//! answers something that does not validate is retried; when retries are spent the caller gets `None` and
//! falls back, as `invoke_with_structured_output` does upstream. In-flight calls are bounded by a semaphore
//! (`SEMANTIC_LLM_MAX_INFLIGHT`, 6 by default) because tables run in parallel and fan out again within each.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use tokio::sync::Semaphore;

/// An API key that never prints.
#[derive(Clone, Default)]
pub struct Secret(String);

impl Secret {
    pub fn new(s: String) -> Secret {
        Secret(s)
    }
    pub fn expose(&self) -> &str {
        &self.0
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(if self.0.is_empty() { "<none>" } else { "<redacted>" })
    }
}

#[derive(Clone, Debug)]
pub struct ModelConfig {
    /// Base URL of the OpenAI-compatible API (`…/v1`); `/chat/completions` and `/embeddings` are appended.
    pub chat_url: String,
    pub chat_model: String,
    pub embed_url: String,
    pub embed_model: String,
    pub api_key: Secret,
    pub timeout: Duration,
    pub retries: u32,
    pub max_inflight: usize,
    /// `json_schema` (structured outputs) or `json_object` for servers without schema support.
    pub response_format: String,
}

#[derive(Default)]
pub struct Stats {
    pub calls: AtomicU64,
    pub failures: AtomicU64,
    pub retries: AtomicU64,
    pub input_tokens: AtomicU64,
    pub output_tokens: AtomicU64,
    pub embed_calls: AtomicU64,
}

pub struct Llm {
    pub cfg: ModelConfig,
    http: reqwest::Client,
    sem: Arc<Semaphore>,
    pub stats: Stats,
}

#[derive(Debug)]
enum CallError {
    /// Worth another try: a timeout, a connection failure, 429 or 5xx, or an answer that did not validate.
    Transient(String),
    /// 429 with the endpoint's Retry-After: retried after that wait (at most a minute), not holding a permit.
    RateLimited(String, Option<Duration>),
    Fatal(String),
}

impl Llm {
    pub fn new(cfg: ModelConfig) -> Result<Llm, String> {
        let http =
            reqwest::Client::builder().timeout(cfg.timeout).connect_timeout(Duration::from_secs(10)).build().map_err(|e| e.to_string())?;
        let n = cfg.max_inflight.max(1);
        Ok(Llm { cfg, http, sem: Arc::new(Semaphore::new(n)), stats: Stats::default() })
    }

    pub fn chat_configured(&self) -> bool {
        !self.cfg.chat_url.is_empty() && !self.cfg.chat_model.is_empty()
    }

    pub fn embed_configured(&self) -> bool {
        !self.cfg.embed_url.is_empty() && !self.cfg.embed_model.is_empty()
    }

    fn auth(&self, rb: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if self.cfg.api_key.is_empty() {
            rb
        } else {
            rb.bearer_auth(self.cfg.api_key.expose())
        }
    }

    async fn post(&self, url: String, body: &Value) -> Result<Value, CallError> {
        let _permit = self.sem.acquire().await.map_err(|_| CallError::Fatal("shutting down".into()))?;
        let r = self.auth(self.http.post(url)).json(body).send().await.map_err(|e| {
            CallError::Transient(if e.is_timeout() {
                "the model did not answer in time".into()
            } else {
                format!("model endpoint: {}", e.without_url())
            })
        })?;
        let status = r.status();
        // The gateway's Retry-After on 429 (seconds), honoured before the next attempt, at most a minute.
        let retry_after = (status.as_u16() == 429)
            .then(|| r.headers().get(reqwest::header::RETRY_AFTER).and_then(|v| v.to_str().ok()?.trim().parse::<u64>().ok()))
            .flatten();
        let text = r.text().await.map_err(|e| CallError::Transient(format!("model endpoint: {}", e.without_url())))?;
        if status.as_u16() == 429 {
            return Err(CallError::RateLimited(
                format!("model endpoint answered {status}"),
                retry_after.map(|s| Duration::from_secs(s.min(60))),
            ));
        }
        if status.is_server_error() {
            return Err(CallError::Transient(format!("model endpoint answered {status}")));
        }
        if !status.is_success() {
            // A refusal (no model access, budget, bad request): the gateway's message, not retried.
            let msg = serde_json::from_str::<Value>(&text)
                .ok()
                .and_then(|v| v.pointer("/error/message").and_then(Value::as_str).map(str::to_string))
                .unwrap_or_else(|| text.chars().take(300).collect());
            return Err(CallError::Fatal(format!("model endpoint answered {status}: {msg}")));
        }
        serde_json::from_str(&text).map_err(|_| CallError::Transient("the model endpoint answered without JSON".into()))
    }

    async fn backoff(&self, attempt: u32, at_least: Option<Duration>) {
        self.stats.retries.fetch_add(1, Ordering::Relaxed);
        let exp = Duration::from_millis(250 * 2u64.pow(attempt.min(5)));
        tokio::time::sleep(at_least.map_or(exp, |d| d.max(exp))).await;
    }

    /// A structured answer of type `T` for `system` + `user`, or None when the model could not give one
    /// (the caller falls back). `schema` is the JSON Schema of `T`.
    pub async fn structured<T: DeserializeOwned>(
        &self,
        task: &str,
        system: &str,
        user: &str,
        schema: Value,
        temperature: Option<f64>,
        max_tokens: Option<u32>,
    ) -> Option<T> {
        if !self.chat_configured() {
            return None;
        }
        let format = if self.cfg.response_format == "json_object" {
            json!({"type": "json_object"})
        } else {
            json!({"type": "json_schema", "json_schema": {"name": task, "schema": schema, "strict": true}})
        };
        let mut body = json!({
            "model": self.cfg.chat_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": format,
        });
        if let Some(t) = temperature {
            body["temperature"] = json!(t);
        }
        if let Some(m) = max_tokens {
            body["max_tokens"] = json!(m);
        }
        let url = format!("{}/chat/completions", self.cfg.chat_url.trim_end_matches('/'));
        for attempt in 0..=self.cfg.retries {
            self.stats.calls.fetch_add(1, Ordering::Relaxed);
            let out = match self.post(url.clone(), &body).await {
                Ok(v) => v,
                Err(CallError::Fatal(e)) => {
                    self.stats.failures.fetch_add(1, Ordering::Relaxed);
                    tracing::warn!(task, error = %e, "model call refused");
                    return None;
                }
                Err(CallError::Transient(e)) => {
                    tracing::info!(task, attempt, error = %e, "model call failed; retrying");
                    if attempt < self.cfg.retries {
                        self.backoff(attempt, None).await;
                    }
                    continue;
                }
                Err(CallError::RateLimited(e, wait)) => {
                    tracing::info!(task, attempt, error = %e, "model call rate limited; retrying");
                    if attempt < self.cfg.retries {
                        self.backoff(attempt, wait).await;
                    }
                    continue;
                }
            };
            if let Some(u) = out.get("usage") {
                self.stats.input_tokens.fetch_add(u["prompt_tokens"].as_u64().unwrap_or(0), Ordering::Relaxed);
                self.stats.output_tokens.fetch_add(u["completion_tokens"].as_u64().unwrap_or(0), Ordering::Relaxed);
            }
            let content = out.pointer("/choices/0/message/content").and_then(Value::as_str).unwrap_or("");
            match parse_json_answer::<T>(content) {
                Ok(v) => return Some(v),
                Err(e) => {
                    tracing::info!(task, attempt, error = %e, "the model's answer did not validate; retrying");
                    if attempt < self.cfg.retries {
                        self.backoff(attempt, None).await;
                    }
                }
            }
        }
        self.stats.failures.fetch_add(1, Ordering::Relaxed);
        tracing::warn!(task, "no valid structured answer from the model");
        None
    }

    /// Embeddings for `texts`, in order (batches of 64).
    pub async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, String> {
        if !self.embed_configured() {
            return Err("no embeddings endpoint is configured".into());
        }
        let url = format!("{}/embeddings", self.cfg.embed_url.trim_end_matches('/'));
        let mut out = Vec::with_capacity(texts.len());
        for chunk in texts.chunks(64) {
            let body = json!({"model": self.cfg.embed_model, "input": chunk});
            let mut got = None;
            let mut last = String::new();
            for attempt in 0..=self.cfg.retries {
                self.stats.embed_calls.fetch_add(1, Ordering::Relaxed);
                match self.post(url.clone(), &body).await {
                    Ok(v) => {
                        got = Some(v);
                        break;
                    }
                    Err(CallError::Fatal(e)) => return Err(e),
                    Err(CallError::Transient(e)) => {
                        last = e;
                        if attempt < self.cfg.retries {
                            self.backoff(attempt, None).await;
                        }
                    }
                    Err(CallError::RateLimited(e, wait)) => {
                        last = e;
                        if attempt < self.cfg.retries {
                            self.backoff(attempt, wait).await;
                        }
                    }
                }
            }
            let v = got.ok_or(last)?;
            if let Some(u) = v.get("usage") {
                self.stats.input_tokens.fetch_add(u["prompt_tokens"].as_u64().unwrap_or(0), Ordering::Relaxed);
            }
            let mut data: Vec<(usize, Vec<f32>)> = v["data"]
                .as_array()
                .ok_or("the embeddings answer has no data")?
                .iter()
                .enumerate()
                .map(|(i, d)| {
                    let idx = d["index"].as_u64().map(|x| x as usize).unwrap_or(i);
                    let e = d["embedding"]
                        .as_array()
                        .map(|a| a.iter().filter_map(|x| x.as_f64()).map(|x| x as f32).collect())
                        .unwrap_or_default();
                    (idx, e)
                })
                .collect();
            data.sort_by_key(|(i, _)| *i);
            if data.len() != chunk.len() || data.iter().any(|(_, e)| e.is_empty()) {
                return Err("the embeddings answer does not match the inputs".into());
            }
            out.extend(data.into_iter().map(|(_, e)| e));
        }
        Ok(out)
    }
}

/// The JSON object in a model's answer (code fences tolerated), deserialised into `T`.
pub fn parse_json_answer<T: DeserializeOwned>(content: &str) -> Result<T, String> {
    let t = content.trim();
    let t = t.strip_prefix("```json").or_else(|| t.strip_prefix("```")).unwrap_or(t);
    let t = t.strip_suffix("```").unwrap_or(t).trim();
    let start = t.find('{').ok_or("no JSON object in the answer")?;
    let end = t.rfind('}').ok_or("no JSON object in the answer")?;
    serde_json::from_str(&t[start..=end]).map_err(|e| e.to_string())
}

pub fn cosine(a: &[f32], b: &[f32]) -> f64 {
    let (mut num, mut na, mut nb) = (0f64, 0f64, 0f64);
    for (x, y) in a.iter().zip(b) {
        num += (*x as f64) * (*y as f64);
        na += (*x as f64) * (*x as f64);
        nb += (*y as f64) * (*y as f64);
    }
    if na == 0.0 || nb == 0.0 {
        0.0
    } else {
        num / (na.sqrt() * nb.sqrt())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(serde::Deserialize, Debug, PartialEq)]
    #[serde(deny_unknown_fields)]
    struct A {
        x: i32,
    }

    #[test]
    fn answers_are_validated_against_the_type() {
        assert_eq!(parse_json_answer::<A>("```json\n{\"x\": 3}\n```").unwrap(), A { x: 3 });
        assert_eq!(parse_json_answer::<A>("Sure: {\"x\": 4}").unwrap(), A { x: 4 });
        assert!(parse_json_answer::<A>("{\"x\": 3, \"y\": 1}").is_err());
        assert!(parse_json_answer::<A>("no").is_err());
    }

    #[test]
    fn secrets_never_print() {
        let s = Secret::new("sk-very-secret".into());
        assert_eq!(format!("{s:?}"), "<redacted>");
        let cfg = ModelConfig {
            chat_url: "u".into(),
            chat_model: "m".into(),
            embed_url: String::new(),
            embed_model: String::new(),
            api_key: s,
            timeout: Duration::from_secs(1),
            retries: 0,
            max_inflight: 1,
            response_format: "json_schema".into(),
        };
        assert!(!format!("{cfg:?}").contains("sk-very-secret"));
    }

    #[test]
    fn cosine_similarity() {
        assert!((cosine(&[1.0, 0.0], &[1.0, 0.0]) - 1.0).abs() < 1e-9);
        assert_eq!(cosine(&[1.0, 0.0], &[0.0, 1.0]), 0.0);
        assert_eq!(cosine(&[0.0, 0.0], &[1.0, 1.0]), 0.0);
    }
}
