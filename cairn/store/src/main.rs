//! cairn-store: Cairn's S3-compatible object store (single node).
//!
//! ```text
//! cairn-store serve        run the server (default)
//! cairn-store fsck         check the data directory and remove debris left by a crash, then exit
//! cairn-store healthcheck  exit 0 if the local server is ready (container health checks)
//! cairn-store version      print the version
//! ```
//!
//! Configuration (flags `-addr`, `-data` override the first two):
//!
//! ```text
//! CAIRN_STORE_ADDR              listen address (:8333)
//! CAIRN_STORE_DATA              data directory (/data)
//! CAIRN_STORE_ACCESS_KEY        admin access key ...
//! CAIRN_STORE_SECRET_KEY        ... and its secret (or CAIRN_STORE_SECRET_KEY_FILE)
//! CAIRN_STORE_CREDENTIALS_FILE  JSON list of further keys: access_key, secret_key, buckets, read_only, admin
//! CAIRN_STORE_REGION            region reported to clients (us-east-1)
//! CAIRN_STORE_DOMAIN            enables virtual-hosted-style requests on <bucket>.<domain>
//! CAIRN_STORE_BUCKETS           comma-separated buckets to create at start-up
//! CAIRN_STORE_FSYNC             false disables fsync (tests only: a crash can lose acknowledged writes)
//! CAIRN_STORE_LOG_LEVEL         debug, info, warn, error
//! ```

// Errors are cold paths here; boxing them would only add allocations to the code that matters.
#![allow(clippy::result_large_err)]

mod engine;
mod s3;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

use engine::Engine;
use s3::auth::Credential;

const VERSION: &str = env!("CARGO_PKG_VERSION");

fn env(k: &str, default: &str) -> String {
    std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

fn fail(msg: &str, err: impl std::fmt::Display) -> ! {
    tracing::error!(error = %err, "{msg}");
    eprintln!("cairn-store: {msg}: {err}");
    std::process::exit(1)
}

fn listen_addr(addr: &str) -> String {
    if addr.starts_with(':') {
        format!("0.0.0.0{addr}")
    } else {
        addr.to_string()
    }
}

fn credentials() -> Result<Vec<Credential>, String> {
    let mut out = vec![];
    let mut secret = env("CAIRN_STORE_SECRET_KEY", "");
    if let Ok(f) = std::env::var("CAIRN_STORE_SECRET_KEY_FILE") {
        secret = std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?.trim().to_string();
    }
    let ak = env("CAIRN_STORE_ACCESS_KEY", "");
    if !ak.is_empty() {
        out.push(Credential { access_key: ak, secret_key: secret, buckets: vec![], read_only: false, admin: true });
    }
    if let Ok(f) = std::env::var("CAIRN_STORE_CREDENTIALS_FILE") {
        let text = std::fs::read_to_string(&f).map_err(|e| format!("{f}: {e}"))?;
        let more: Vec<Credential> = serde_json::from_str(&text).map_err(|e| format!("{f}: {e}"))?;
        out.extend(more);
    }
    if out.is_empty() {
        return Err("set CAIRN_STORE_ACCESS_KEY and CAIRN_STORE_SECRET_KEY, or CAIRN_STORE_CREDENTIALS_FILE".into());
    }
    Ok(out)
}

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let cmd = if args.first().is_some_and(|a| !a.starts_with('-')) { args.remove(0) } else { "serve".to_string() };
    let mut addr = env("CAIRN_STORE_ADDR", ":8333");
    let mut data = env("CAIRN_STORE_DATA", "/data");
    let mut it = args.into_iter();
    while let Some(a) = it.next() {
        match a.trim_start_matches('-') {
            "addr" => addr = it.next().unwrap_or_default(),
            "data" => data = it.next().unwrap_or_default(),
            other => {
                eprintln!("unknown flag -{other} (flags: -addr, -data)");
                std::process::exit(2);
            }
        }
    }
    let level = env("CAIRN_STORE_LOG_LEVEL", "info");
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::new(format!("cairn_store={level},warn")))
        .with_writer(std::io::stdout)
        .init();

    match cmd.as_str() {
        "version" => println!("cairn-store {VERSION}"),
        "fsck" => {
            let eng = Engine::open(&data, true).unwrap_or_else(|e| fail("open store", e));
            let rep = eng.recover().unwrap_or_else(|e| fail("fsck", e));
            println!("{}", serde_json::to_string_pretty(&rep).unwrap());
            if rep.objects_with_missing_blobs > 0 {
                std::process::exit(2);
            }
        }
        "healthcheck" => {
            let ok = std::net::TcpStream::connect_timeout(
                &listen_addr(&addr).replace("0.0.0.0", "127.0.0.1").parse::<SocketAddr>().unwrap_or_else(|e| fail("address", e)),
                Duration::from_secs(3),
            )
            .and_then(|mut s| {
                use std::io::{Read, Write};
                s.set_read_timeout(Some(Duration::from_secs(3)))?;
                s.write_all(b"GET /-/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")?;
                let mut buf = [0u8; 16];
                let n = s.read(&mut buf)?;
                Ok(buf[..n].starts_with(b"HTTP/1.1 200"))
            })
            .unwrap_or(false);
            std::process::exit(if ok { 0 } else { 1 });
        }
        "serve" => {
            let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().unwrap_or_else(|e| fail("runtime", e));
            rt.block_on(serve(addr, data));
        }
        other => {
            eprintln!("unknown command {other:?} (serve, fsck, healthcheck, version)");
            std::process::exit(2);
        }
    }
}

async fn serve(addr: String, data: String) {
    let creds = credentials().unwrap_or_else(|e| fail("credentials", e));
    let fsync = env("CAIRN_STORE_FSYNC", "true") != "false";
    let eng = Arc::new(Engine::open(&data, fsync).unwrap_or_else(|e| fail("open store", e)));
    if !fsync {
        tracing::warn!("fsync is off: a crash or power loss can lose writes that were acknowledged");
    }
    for b in env("CAIRN_STORE_BUCKETS", "").split(',').map(str::trim).filter(|b| !b.is_empty()) {
        match eng.create_bucket(b) {
            Ok(()) | Err(engine::Error::BucketExists) => {}
            Err(e) => fail(&format!("create bucket {b}"), e),
        }
    }
    let keys = creds.len();
    let server = Arc::new(
        s3::Server::new(
            eng,
            s3::Config {
                region: env("CAIRN_STORE_REGION", "us-east-1"),
                domain: env("CAIRN_STORE_DOMAIN", ""),
                credentials: creds,
                max_object: 0,
            },
        )
        .unwrap_or_else(|e| fail("configure", e)),
    );
    let listener = TcpListener::bind(listen_addr(&addr)).await.unwrap_or_else(|e| fail("listen", e));
    tracing::info!(addr = %addr, data = %data, version = VERSION, keys, fsync, "cairn-store listening");
    let mut shutdown = Box::pin(async {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    });
    let graceful = hyper_util::server::graceful::GracefulShutdown::new();
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else { continue };
                let server = server.clone();
                let svc = service_fn(move |req| server.clone().handle(req));
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
    // Stop taking requests; idle connections close at once, requests in flight get 30 s to finish.
    tracing::info!("shutting down");
    if tokio::time::timeout(Duration::from_secs(30), graceful.shutdown()).await.is_err() {
        tracing::warn!("requests still running after 30 s; exiting anyway");
    }
}
