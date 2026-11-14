// SPDX-License-Identifier: Apache-2.0
//! cairn-ontology: Cairn Ontology, the ontology compiler (a Rust port of the compiler at the heart of NVIDIA's
//! open-source auto-ontology; see NOTICE).
//!
//! ```text
//! cairn-ontology serve        run the server (default)
//! cairn-ontology healthcheck  exit 0 if the local server answers (container health checks)
//! cairn-ontology version      print the version
//! ```
//!
//! Configuration:
//!
//! ```text
//! CAIRN_ONTOLOGY_ADDR                 listen address (:8590)
//! CAIRN_ONTOLOGY_DATABASE_URL         postgres://… (or _FILE); a SQLite path for development (/data/ontology.db)
//! CAIRN_ONTOLOGY_DATABASE_CA_FILE     extra CA certificates for the database's TLS
//! CAIRN_ONTOLOGY_TOKEN                the bearer token Cairn calls with (or _FILE; several, one per line); 16+ characters
//! CAIRN_ONTOLOGY_MODEL_URL            OpenAI-compatible base URL (Cairn's AI gateway: https://cairn/cairn/api/v1/ai/gateway/openai/v1)
//! CAIRN_ONTOLOGY_MODEL                the chat model
//! CAIRN_ONTOLOGY_EMBED_URL            embeddings base URL (default: CAIRN_ONTOLOGY_MODEL_URL)
//! CAIRN_ONTOLOGY_EMBED_MODEL          the embeddings model (unset: no embeddings; semantic search and vector FK candidates off)
//! CAIRN_ONTOLOGY_MODEL_API_KEY        the key for both (or _FILE): in Cairn, the compiler agent's key (scope "model")
//! CAIRN_ONTOLOGY_MODEL_TIMEOUT_SECONDS  per call (120)
//! CAIRN_ONTOLOGY_MODEL_RETRIES        per call (2)
//! CAIRN_ONTOLOGY_MODEL_MAX_INFLIGHT   concurrent model calls (6; also SEMANTIC_LLM_MAX_INFLIGHT)
//! CAIRN_ONTOLOGY_MODEL_RESPONSE_FORMAT  json_schema (default) or json_object
//! CAIRN_ONTOLOGY_WORKERS              tables processed at once (3)
//! CAIRN_ONTOLOGY_PROBE_DISTINCT_VALUES  use a column's full distinct set for categorical text (true)
//! CAIRN_ONTOLOGY_MAX_QUERIES          most-used queries compilation reads (20000)
//! CAIRN_ONTOLOGY_DOMAIN_FILE          a domain summary JSON ({domains, core_entities, business_rules})
//! CAIRN_ONTOLOGY_DATABASE_NAME        the catalog's name in canonical table order (cairn)
//! CAIRN_ONTOLOGY_LOG_LEVEL            debug, info, warn, error
//! ```

mod api;
mod catalog;
mod graph;
mod llm;
mod model;
mod prompts;
mod querylog;
mod repo;
mod semantic;
mod sqlx;
mod store;
mod validate;

use std::sync::Arc;
use std::time::Duration;

const VERSION: &str = env!("CARGO_PKG_VERSION");

fn env(k: &str, default: &str) -> String {
    std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

fn secret(k: &str) -> String {
    match std::env::var(format!("{k}_FILE")) {
        Ok(f) if !f.is_empty() => std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e)).trim().to_string(),
        _ => env(k, ""),
    }
}

fn fail(msg: &str, err: impl std::fmt::Display) -> ! {
    tracing::error!(error = %err, "{msg}");
    eprintln!("cairn-ontology: {msg}: {err}");
    std::process::exit(1)
}

fn listen_addr(addr: &str) -> String {
    if addr.starts_with(':') {
        format!("0.0.0.0{addr}")
    } else {
        addr.to_string()
    }
}

fn num<T: std::str::FromStr>(k: &str, default: &str) -> T {
    env(k, default).parse().unwrap_or_else(|_| fail(k, "not a number"))
}

fn main() {
    let cmd = std::env::args().nth(1).unwrap_or_else(|| "serve".into());
    let level = env("CAIRN_ONTOLOGY_LOG_LEVEL", "info");
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::new(format!("cairn_ontology={level},warn")))
        .with_writer(std::io::stdout)
        .init();
    let addr = env("CAIRN_ONTOLOGY_ADDR", ":8590");
    match cmd.as_str() {
        "version" => println!("cairn-ontology {VERSION}"),
        "healthcheck" => {
            use std::io::{Read, Write};
            let target = listen_addr(&addr).replace("0.0.0.0", "127.0.0.1");
            let ok = target
                .parse()
                .ok()
                .and_then(|a| std::net::TcpStream::connect_timeout(&a, Duration::from_secs(3)).ok())
                .and_then(|mut s| {
                    s.set_read_timeout(Some(Duration::from_secs(3))).ok()?;
                    s.write_all(b"GET /-/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n").ok()?;
                    let mut buf = [0u8; 16];
                    let n = s.read(&mut buf).ok()?;
                    Some(buf[..n].starts_with(b"HTTP/1.1 200"))
                })
                .unwrap_or(false);
            std::process::exit(if ok { 0 } else { 1 });
        }
        "serve" => {
            let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().unwrap_or_else(|e| fail("runtime", e));
            rt.block_on(serve(addr));
        }
        other => {
            eprintln!("unknown command {other:?} (serve, healthcheck, version)");
            std::process::exit(2);
        }
    }
}

fn tokens() -> Vec<String> {
    let raw = secret("CAIRN_ONTOLOGY_TOKEN");
    let tokens: Vec<String> = raw.lines().map(str::trim).filter(|l| !l.is_empty()).map(str::to_string).collect();
    if tokens.is_empty() || tokens.iter().any(|t| t.len() < 16) {
        fail("CAIRN_ONTOLOGY_TOKEN", "set CAIRN_ONTOLOGY_TOKEN or CAIRN_ONTOLOGY_TOKEN_FILE; tokens must be at least 16 characters");
    }
    tokens
}

async fn serve(addr: String) {
    let tokens = tokens();
    let url = secret("CAIRN_ONTOLOGY_DATABASE_URL");
    let url = if url.is_empty() { "/data/ontology.db".to_string() } else { url };
    let ca = env("CAIRN_ONTOLOGY_DATABASE_CA_FILE", "");
    let st = store::Store::open(&url, Some(&ca)).unwrap_or_else(|e| fail("open the ontology database", e));
    let interrupted = repo::mark_interrupted(&st).await.unwrap_or_else(|e| fail("ontology database", e));
    if interrupted > 0 {
        tracing::warn!(runs = interrupted, "runs left running by a stopped process are marked interrupted (resumable)");
    }
    let model_url = env("CAIRN_ONTOLOGY_MODEL_URL", "");
    let inflight = std::env::var("SEMANTIC_LLM_MAX_INFLIGHT")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| env("CAIRN_ONTOLOGY_MODEL_MAX_INFLIGHT", "6"));
    let cfg = llm::ModelConfig {
        chat_url: model_url.clone(),
        chat_model: env("CAIRN_ONTOLOGY_MODEL", ""),
        embed_url: env("CAIRN_ONTOLOGY_EMBED_URL", &model_url),
        embed_model: env("CAIRN_ONTOLOGY_EMBED_MODEL", ""),
        api_key: llm::Secret::new(secret("CAIRN_ONTOLOGY_MODEL_API_KEY")),
        timeout: Duration::from_secs(num("CAIRN_ONTOLOGY_MODEL_TIMEOUT_SECONDS", "120")),
        retries: num("CAIRN_ONTOLOGY_MODEL_RETRIES", "2"),
        max_inflight: inflight.parse().unwrap_or_else(|_| fail("CAIRN_ONTOLOGY_MODEL_MAX_INFLIGHT", "not a number")),
        response_format: env("CAIRN_ONTOLOGY_MODEL_RESPONSE_FORMAT", "json_schema"),
    };
    if !cfg.chat_url.is_empty() && !cfg.chat_url.starts_with("http://") && !cfg.chat_url.starts_with("https://") {
        fail("CAIRN_ONTOLOGY_MODEL_URL", "an http(s) URL");
    }
    let model_on = !cfg.chat_url.is_empty() && !cfg.chat_model.is_empty();
    let embed_on = !cfg.embed_url.is_empty() && !cfg.embed_model.is_empty();
    let llm = Arc::new(llm::Llm::new(cfg).unwrap_or_else(|e| fail("model client", e)));
    let domain = match env("CAIRN_ONTOLOGY_DOMAIN_FILE", "") {
        f if f.is_empty() => None,
        f => Some(
            serde_json::from_str(&std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e)))
                .unwrap_or_else(|e| fail("CAIRN_ONTOLOGY_DOMAIN_FILE", e)),
        ),
    };
    let settings = semantic::pipeline::Settings {
        workers: num("CAIRN_ONTOLOGY_WORKERS", "3"),
        probe_distinct_values: env("CAIRN_ONTOLOGY_PROBE_DISTINCT_VALUES", "true") != "false",
        max_queries: num("CAIRN_ONTOLOGY_MAX_QUERIES", "20000"),
        domain,
        database: env("CAIRN_ONTOLOGY_DATABASE_NAME", "cairn"),
    };
    let engine = Arc::new(semantic::pipeline::Engine::new(st.clone(), llm, settings));
    let state = Arc::new(api::AppState { engine, tokens });
    let app = api::router(state);
    let listener = tokio::net::TcpListener::bind(listen_addr(&addr)).await.unwrap_or_else(|e| fail("listen", e));
    tracing::info!(addr = %addr, store = st.kind, model = model_on, embeddings = embed_on, version = VERSION, "cairn-ontology listening");
    let shutdown = async {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
        tracing::info!("shutting down");
    };
    axum::serve(listener, app).with_graceful_shutdown(shutdown).await.unwrap_or_else(|e| fail("serve", e));
}
