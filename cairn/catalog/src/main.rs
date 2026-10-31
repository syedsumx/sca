//! cairn-catalog: Cairn's Iceberg REST catalog.
//!
//! ```text
//! cairn-catalog serve        run the server (default)
//! cairn-catalog healthcheck  exit 0 if the local server answers (container health checks)
//! cairn-catalog version      print the version
//! ```
//!
//! Configuration:
//!
//! ```text
//! CAIRN_CATALOG_ADDR              listen address (:8181)
//! CAIRN_CATALOG_DATA              state directory (/data)
//! CAIRN_CATALOG_WAREHOUSE         base location for new tables (s3://lakehouse/warehouse)
//! CAIRN_CATALOG_TOKEN             an admin bearer token (or CAIRN_CATALOG_TOKEN_FILE) ...
//! CAIRN_CATALOG_TOKENS_FILE       ... and/or a JSON list: token, name, namespaces (globs), read_only
//! CAIRN_CATALOG_S3_ENDPOINT       object store for metadata files (http://cairn-store:8333)
//! CAIRN_CATALOG_S3_REGION         (us-east-1)
//! CAIRN_CATALOG_S3_ACCESS_KEY     object store key the catalog writes with ...
//! CAIRN_CATALOG_S3_SECRET_KEY     ... and its secret (or CAIRN_CATALOG_S3_SECRET_KEY_FILE)
//! CAIRN_CATALOG_VEND_CREDENTIALS  true: give clients the endpoint and key with each table (default true)
//! CAIRN_CATALOG_CLIENT_S3_ENDPOINT  the object store endpoint as clients reach it (default: the one above)
//! CAIRN_CATALOG_FSYNC             false disables fsync (tests only)
//! CAIRN_CATALOG_LOG_LEVEL         debug, info, warn, error
//! ```

mod api;
mod db;
mod s3;

use std::sync::Arc;
use std::time::Duration;

use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

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
    eprintln!("cairn-catalog: {msg}: {err}");
    std::process::exit(1)
}

fn listen_addr(addr: &str) -> String {
    if addr.starts_with(':') {
        format!("0.0.0.0{addr}")
    } else {
        addr.to_string()
    }
}

fn principals() -> Vec<api::Principal> {
    let mut out = vec![];
    let token = secret("CAIRN_CATALOG_TOKEN");
    if !token.is_empty() {
        out.push(api::Principal { token, name: "admin".into(), namespaces: vec![], read_only: false });
    }
    if let Ok(f) = std::env::var("CAIRN_CATALOG_TOKENS_FILE") {
        let text = std::fs::read_to_string(&f).unwrap_or_else(|e| fail(&f, e));
        let more: Vec<api::Principal> = serde_json::from_str(&text).unwrap_or_else(|e| fail(&f, e));
        out.extend(more);
    }
    if out.is_empty() || out.iter().any(|p| p.token.len() < 16) {
        fail("tokens", "set CAIRN_CATALOG_TOKEN or CAIRN_CATALOG_TOKENS_FILE; tokens must be at least 16 characters");
    }
    out
}

fn main() {
    let cmd = std::env::args().nth(1).unwrap_or_else(|| "serve".into());
    let level = env("CAIRN_CATALOG_LOG_LEVEL", "info");
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::new(format!("cairn_catalog={level},warn")))
        .with_writer(std::io::stdout)
        .init();
    let addr = env("CAIRN_CATALOG_ADDR", ":8181");
    match cmd.as_str() {
        "version" => println!("cairn-catalog {VERSION}"),
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

async fn serve(addr: String) {
    let data = env("CAIRN_CATALOG_DATA", "/data");
    let fsync = env("CAIRN_CATALOG_FSYNC", "true") != "false";
    let db = db::Db::open(&data, fsync).unwrap_or_else(|e| fail("open catalog state", e));
    let s3cfg = s3::S3Config {
        endpoint: env("CAIRN_CATALOG_S3_ENDPOINT", "http://cairn-store:8333"),
        region: env("CAIRN_CATALOG_S3_REGION", "us-east-1"),
        access_key: env("CAIRN_CATALOG_S3_ACCESS_KEY", ""),
        secret_key: secret("CAIRN_CATALOG_S3_SECRET_KEY"),
    };
    if s3cfg.access_key.is_empty() || s3cfg.secret_key.is_empty() {
        fail("object store", "set CAIRN_CATALOG_S3_ACCESS_KEY and CAIRN_CATALOG_S3_SECRET_KEY");
    }
    let s3cfg_endpoint = s3cfg.endpoint.clone();
    let warehouse = env("CAIRN_CATALOG_WAREHOUSE", "s3://lakehouse/warehouse");
    s3::parse_location(&format!("{}/x", warehouse.trim_end_matches('/'))).unwrap_or_else(|e| fail("warehouse", e));
    let cfg = api::Config {
        warehouse: warehouse.clone(),
        principals: principals(),
        s3: s3cfg,
        vend_credentials: env("CAIRN_CATALOG_VEND_CREDENTIALS", "true") == "true",
        client_endpoint: env("CAIRN_CATALOG_CLIENT_S3_ENDPOINT", &s3cfg_endpoint),
    };
    let tokens = cfg.principals.len();
    let catalog = Arc::new(api::Catalog::new(db, cfg));
    let listener = TcpListener::bind(listen_addr(&addr)).await.unwrap_or_else(|e| fail("listen", e));
    tracing::info!(addr = %addr, data = %data, warehouse = %warehouse, tokens, fsync, version = VERSION, "cairn-catalog listening");
    let mut shutdown = Box::pin(async {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    });
    let graceful = hyper_util::server::graceful::GracefulShutdown::new();
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else { continue };
                let catalog = catalog.clone();
                let svc = service_fn(move |req| catalog.clone().handle(req));
                let conn = http1::Builder::new().timer(hyper_util::rt::TokioTimer::new())
                    .header_read_timeout(Duration::from_secs(30)).keep_alive(true)
                    .serve_connection(TokioIo::new(stream), svc);
                let conn = graceful.watch(conn);
                tokio::spawn(async move {
                    if let Err(e) = conn.await {
                        tracing::debug!(error = %e, "connection");
                    }
                });
            }
            _ = &mut shutdown => break,
        }
    }
    tracing::info!("shutting down");
    let _ = tokio::time::timeout(Duration::from_secs(30), graceful.shutdown()).await;
}
