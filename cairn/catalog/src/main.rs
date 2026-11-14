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
//! CAIRN_CATALOG_DATABASE_URL      PostgreSQL for the state instead (or _FILE), so several replicas serve one lakehouse
//! CAIRN_CATALOG_DATABASE_CA_FILE  extra CA certificates for its TLS
//! CAIRN_CATALOG_WAREHOUSE         base location for new tables (s3://lakehouse/warehouse)
//! CAIRN_CATALOG_TOKEN             an admin bearer token (or CAIRN_CATALOG_TOKEN_FILE) ...
//! CAIRN_CATALOG_TOKENS_FILE       ... and/or a JSON list: token, name, namespaces (globs), read_only
//! CAIRN_CATALOG_S3_ENDPOINT       object store for metadata files (http://cairn-store:8333)
//! CAIRN_CATALOG_S3_REGION         (us-east-1)
//! CAIRN_CATALOG_S3_ACCESS_KEY     object store key the catalog writes with ...
//! CAIRN_CATALOG_S3_SECRET_KEY     ... and its secret (or CAIRN_CATALOG_S3_SECRET_KEY_FILE)
//! CAIRN_CATALOG_VEND_CREDENTIALS  true: give clients the endpoint and key with each table (default true)
//! CAIRN_CATALOG_CLIENT_S3_ENDPOINT  the object store endpoint as clients reach it (default: the one above)
//! CAIRN_CATALOG_JWKS_URL          accept Cairn-issued tokens (people, pipeline users): Cairn's keys, e.g. https://cairn/cairn/api/v1/trino/jwks.json
//! CAIRN_CATALOG_CAIRN_URL         ... Cairn's API, asked about every table such a token touches
//! CAIRN_CATALOG_CAIRN_TOKEN       ... a Cairn service token to ask with (or _FILE)
//! CAIRN_CATALOG_JWT_ISSUER / _AUDIENCE  the tokens' issuer (cairn) and audience (lakehouse)
//! CAIRN_CATALOG_FSYNC             false skips fsync: commits survive the process dying, not the machine (tests only)
//! CAIRN_CATALOG_LOG_LEVEL         debug, info, warn, error
//! CAIRN_CATALOG_LOCATIONS         cairn: tables on the customer's storage in external locations registered in Cairn
//! CAIRN_CATALOG_LOCATIONS_FILE    ... or registered in a file (no Cairn-issued tokens then)
//! CAIRN_CATALOG_STORE_URLS        with external locations: other places on Cairn Store besides the warehouse
//! CAIRN_CATALOG_LOCATIONS_REFRESH_SECONDS  how often they are loaded from Cairn (30)
//! CAIRN_CATALOG_STORAGE_CA_FILE   extra CA certificates for external storage, STS and token endpoints
//! CAIRN_CATALOG_SECRETS_DIR       where Kubernetes Secrets named by k8s: references are mounted
//! CAIRN_CATALOG_VAULT_ADDR / CAIRN_CATALOG_VAULT_TOKEN_FILE  HashiCorp Vault for vault: references
//! CAIRN_CATALOG_DELEGATED_TOKEN_SECONDS  how long a token an engine got for a person lasts (3600)
//! ```

mod api;
mod db;
mod external;
mod identity;
mod pg;
mod residency;
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
        out.push(api::Principal {
            token,
            name: "admin".into(),
            namespaces: vec![],
            read_only: false,
            user: None,
            delegate: false,
            delegated: false,
        });
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

/// How the catalog hands storage access to engines (CAIRN_CATALOG_VEND_CREDENTIALS):
/// `scoped` (temporary per-table keys from the store's STS; the default when CAIRN_CATALOG_VEND_ROLE is
/// set), `static` (the catalog's own key, the default otherwise) or `off`.
async fn vend_mode(store: &s3::Client, warehouse: &str, extra_buckets: &[String]) -> api::Vend {
    let role = env("CAIRN_CATALOG_VEND_ROLE", "");
    let mode = env("CAIRN_CATALOG_VEND_CREDENTIALS", if role.is_empty() { "static" } else { "scoped" });
    match mode.as_str() {
        "off" | "false" => api::Vend::Off,
        "static" | "true" => api::Vend::Static,
        "scoped" => {
            if role.is_empty() {
                fail("CAIRN_CATALOG_VEND_CREDENTIALS", "scoped needs CAIRN_CATALOG_VEND_ROLE (the store role to assume)");
            }
            let seconds: i64 = env("CAIRN_CATALOG_VEND_SECONDS", "3600")
                .parse()
                .ok()
                .filter(|s| (900..=43200).contains(s))
                .unwrap_or_else(|| fail("CAIRN_CATALOG_VEND_SECONDS", "900 to 43200"));
            let role_arn =
                ensure_vend_role(store, &role, warehouse, extra_buckets).await.unwrap_or_else(|e| fail("CAIRN_CATALOG_VEND_ROLE", e));
            // Prove it works now rather than on the first table load.
            let policy = api::table_policy(&format!("{}/_probe", warehouse.trim_end_matches('/')), true).unwrap_or_default();
            store
                .assume_role(&role_arn, "cairn-catalog-check", &policy, 900)
                .await
                .unwrap_or_else(|e| fail("scoped credentials (is STS on in the store?)", e));
            // How often clients come back for keys, so access is decided again (at most `seconds`; more
            // than 5 minutes, since Iceberg clients renew 5 minutes before the reported expiry).
            let revalidate: i64 = env("CAIRN_CATALOG_VEND_REVALIDATE_SECONDS", &seconds.to_string())
                .parse()
                .ok()
                .filter(|r| (360..=seconds).contains(r))
                .unwrap_or_else(|| fail("CAIRN_CATALOG_VEND_REVALIDATE_SECONDS", format!("360 to {seconds} (CAIRN_CATALOG_VEND_SECONDS)")));
            api::Vend::Scoped { role_arn, seconds, revalidate }
        }
        m => fail("CAIRN_CATALOG_VEND_CREDENTIALS", format!("{m:?}: scoped, static or off")),
    }
}

/// The store role scoped credentials come from: a role ARN as given, or a role name the catalog creates
/// when missing (trusted only by the catalog's key; allowed the warehouse bucket, which each session's
/// policy narrows to one table).
async fn ensure_vend_role(store: &s3::Client, role: &str, warehouse: &str, extra_buckets: &[String]) -> Result<String, String> {
    if role.starts_with("arn:") {
        return Ok(role.to_string());
    }
    let (status, xml) =
        store.query_api("iam", &[("Action", "GetRole"), ("Version", "2010-05-08"), ("RoleName", role)]).await.map_err(|e| e.to_string())?;
    let existing = if status == 200 {
        Some(s3::xml_text(&xml, "Arn").map(str::to_string).ok_or("GetRole: no Arn")?)
    } else if s3::xml_text(&xml, "Code") != Some("NoSuchEntity") {
        return Err(format!("GetRole {role}: HTTP {status}: {}", s3::xml_text(&xml, "Message").unwrap_or(&xml)));
    } else {
        None
    };
    if existing.is_some() && extra_buckets.is_empty() {
        return Ok(existing.unwrap_or_default());
    }
    let (_, who) =
        store.query_api("sts", &[("Action", "GetCallerIdentity"), ("Version", "2011-06-15")]).await.map_err(|e| e.to_string())?;
    let me = s3::xml_text(&who, "Arn").ok_or("GetCallerIdentity: no Arn")?.to_string();
    let (bucket, _) = s3::parse_location(&format!("{}/x", warehouse.trim_end_matches('/'))).map_err(|e| e.to_string())?;
    let trust = serde_json::json!({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": me}, "Action": "sts:AssumeRole"}]})
    .to_string();
    // The warehouse bucket, and the region locations' buckets when namespaces are pinned (data residency).
    let mut buckets = vec![bucket.to_string()];
    buckets.extend(extra_buckets.iter().cloned());
    buckets.sort();
    buckets.dedup();
    let resources: Vec<String> = buckets.iter().flat_map(|b| [format!("arn:aws:s3:::{b}"), format!("arn:aws:s3:::{b}/*")]).collect();
    let access = serde_json::json!({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:*", "Resource": resources}]})
    .to_string();
    let created = existing.is_none();
    let arn = if let Some(arn) = existing {
        arn
    } else {
        let (status, xml) = store
            .query_api(
                "iam",
                &[
                    ("Action", "CreateRole"),
                    ("Version", "2010-05-08"),
                    ("RoleName", role),
                    ("AssumeRolePolicyDocument", &trust),
                    ("MaxSessionDuration", "43200"),
                    ("Description", "Cairn Catalog: per-table credentials for engines"),
                ],
            )
            .await
            .map_err(|e| e.to_string())?;
        if status == 200 {
            s3::xml_text(&xml, "Arn").ok_or("CreateRole: no Arn")?.to_string()
        } else if s3::xml_text(&xml, "Code") == Some("EntityAlreadyExists") {
            // Another catalog replica, starting at the same time, created it first; its policy is set below
            // all the same (PutRolePolicy replaces it with the same document).
            let (status, xml) = store
                .query_api("iam", &[("Action", "GetRole"), ("Version", "2010-05-08"), ("RoleName", role)])
                .await
                .map_err(|e| e.to_string())?;
            if status != 200 {
                return Err(format!("GetRole {role}: HTTP {status}"));
            }
            s3::xml_text(&xml, "Arn").ok_or("GetRole: no Arn")?.to_string()
        } else {
            return Err(format!("CreateRole {role}: HTTP {status}: {}", s3::xml_text(&xml, "Message").unwrap_or(&xml)));
        }
    };
    let (status, xml) = store
        .query_api(
            "iam",
            &[
                ("Action", "PutRolePolicy"),
                ("Version", "2010-05-08"),
                ("RoleName", role),
                ("PolicyName", "warehouse"),
                ("PolicyDocument", &access),
            ],
        )
        .await
        .map_err(|e| e.to_string())?;
    if status != 200 {
        return Err(format!("PutRolePolicy {role}: HTTP {status}: {}", s3::xml_text(&xml, "Message").unwrap_or(&xml)));
    }
    tracing::info!(role = %arn, created, buckets = buckets.len(), "the store role for scoped credentials is set");
    Ok(arn)
}

/// External locations: registered in Cairn (CAIRN_CATALOG_LOCATIONS=cairn, its API and service token as for
/// Cairn-issued tokens), in a file (CAIRN_CATALOG_LOCATIONS_FILE), or none.
fn external_config(warehouse: &str, residency: &residency::Residency) -> external::External {
    let mut reserved = vec![external::parse(warehouse).unwrap_or_else(|e| fail("CAIRN_CATALOG_WAREHOUSE", e))];
    for l in residency.locations() {
        reserved.push(external::parse(&l).unwrap_or_else(|e| fail("CAIRN_CATALOG_RESIDENCY_FILE", e)));
    }
    // Other places on Cairn Store tables may live (with external locations on, nothing else is Cairn Store's).
    for l in env("CAIRN_CATALOG_STORE_URLS", "").split(',').map(str::trim).filter(|l| !l.is_empty()) {
        reserved.push(external::parse(l).unwrap_or_else(|e| fail("CAIRN_CATALOG_STORE_URLS", e)));
    }
    let source = match (env("CAIRN_CATALOG_LOCATIONS", "").as_str(), env("CAIRN_CATALOG_LOCATIONS_FILE", "")) {
        ("cairn", f) if f.is_empty() => {
            let url = env("CAIRN_CATALOG_CAIRN_URL", "");
            let token = secret("CAIRN_CATALOG_CAIRN_TOKEN");
            if url.is_empty() || token.len() < 16 {
                fail("CAIRN_CATALOG_LOCATIONS", "cairn needs CAIRN_CATALOG_CAIRN_URL and CAIRN_CATALOG_CAIRN_TOKEN");
            }
            external::Source::Cairn { url, token }
        }
        ("" | "file", f) if !f.is_empty() => external::Source::File(f),
        ("" | "off", f) if f.is_empty() => external::Source::Off,
        (m, _) => fail("CAIRN_CATALOG_LOCATIONS", format!("{m:?}: cairn, or CAIRN_CATALOG_LOCATIONS_FILE (not both)")),
    };
    let refresh: u64 = env("CAIRN_CATALOG_LOCATIONS_REFRESH_SECONDS", "30")
        .parse()
        .ok()
        .filter(|s| (1..=3600).contains(s))
        .unwrap_or_else(|| fail("CAIRN_CATALOG_LOCATIONS_REFRESH_SECONDS", "1 to 3600"));
    let ca = std::env::var("CAIRN_CATALOG_STORAGE_CA_FILE").ok().filter(|f| !f.is_empty());
    external::External::new(source, reserved, ca, Duration::from_secs(refresh))
        .unwrap_or_else(|e| fail("external locations (CAIRN_CATALOG_LOCATIONS_FILE)", e))
}

/// People and pipeline users signing in with Cairn-issued tokens (CAIRN_CATALOG_JWKS_URL), decided by Cairn.
fn identity_config() -> Option<std::sync::Arc<identity::Identity>> {
    let jwks = env("CAIRN_CATALOG_JWKS_URL", "");
    if jwks.is_empty() {
        return None;
    }
    let cairn_url = env("CAIRN_CATALOG_CAIRN_URL", "");
    if cairn_url.is_empty() {
        fail("CAIRN_CATALOG_CAIRN_URL", "Cairn's API, which decides each table for Cairn-issued tokens");
    }
    let cfg = identity::IdentityConfig {
        jwks_url: jwks,
        issuer: env("CAIRN_CATALOG_JWT_ISSUER", "cairn"),
        audience: env("CAIRN_CATALOG_JWT_AUDIENCE", "lakehouse"),
        cairn_url,
        cairn_token: secret("CAIRN_CATALOG_CAIRN_TOKEN"),
    };
    Some(std::sync::Arc::new(identity::Identity::new(cfg).unwrap_or_else(|e| fail("Cairn-issued tokens", e))))
}

async fn serve(addr: String) {
    let data = env("CAIRN_CATALOG_DATA", "/data");
    let fsync = env("CAIRN_CATALOG_FSYNC", "true") != "false";
    let warehouse = env("CAIRN_CATALOG_WAREHOUSE", "s3://lakehouse/warehouse");
    let database_url = secret("CAIRN_CATALOG_DATABASE_URL");
    let db = if database_url.is_empty() { db::Db::open(&data, fsync) } else { db::Db::postgres(&database_url, &warehouse) }
        .unwrap_or_else(|e| fail("open catalog state", e));
    let state = if database_url.is_empty() { data.clone() } else { "postgres".to_string() };
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
    let warehouse_store = s3::Client::new(s3cfg.clone());
    s3::parse_location(&format!("{}/x", warehouse.trim_end_matches('/'))).unwrap_or_else(|e| fail("warehouse", e));
    let residency = residency::Residency::from_env().unwrap_or_else(|e| fail("CAIRN_CATALOG_RESIDENCY_FILE", e));
    let extra_buckets = residency.buckets();
    let external = Arc::new(external_config(&warehouse, &residency));
    let cfg = api::Config {
        warehouse: warehouse.clone(),
        principals: principals(),
        s3: s3cfg,
        vend: vend_mode(&warehouse_store, &warehouse, &extra_buckets).await,
        client_endpoint: env("CAIRN_CATALOG_CLIENT_S3_ENDPOINT", &s3cfg_endpoint),
        identity: identity_config(),
        residency,
        external: external.clone(),
    };
    if matches!(env("CAIRN_CATALOG_LOCATIONS", "").as_str(), "cairn") {
        // Loaded now and then every refresh; until the first load nothing external is placed or handed out.
        let ext = external.clone();
        tokio::spawn(async move {
            loop {
                match ext.refresh_from_cairn().await {
                    Ok(n) => tracing::debug!(locations = n, "external locations loaded from Cairn"),
                    Err(e) => tracing::warn!(error = %e, "external locations could not be loaded from Cairn"),
                }
                tokio::time::sleep(ext.refresh_every()).await;
            }
        });
    }
    if cfg.identity.is_some() && !matches!(cfg.vend, api::Vend::Scoped { .. }) {
        // A person's token must never carry the catalog's own store key, nor nothing at all.
        fail("CAIRN_CATALOG_JWKS_URL", "Cairn-issued tokens need scoped credentials: set CAIRN_CATALOG_VEND_ROLE (the store's STS on)");
    }
    let tokens = cfg.principals.len();
    let catalog = Arc::new(api::Catalog::new(db, cfg));
    let listener = TcpListener::bind(listen_addr(&addr)).await.unwrap_or_else(|e| fail("listen", e));
    tracing::info!(addr = %addr, state = %state, warehouse = %warehouse, tokens, fsync, version = VERSION, "cairn-catalog listening");
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
