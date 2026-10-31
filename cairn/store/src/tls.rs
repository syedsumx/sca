//! TLS for the store's listener (optionally requiring client certificates), certificate reloading, and
//! HTTPS for outbound calls (webhooks, Cairn's policy decision point).
//!
//! The certificate and key files are re-read when they change (checked every 30 s), so renewed
//! certificates (cert-manager, ACME) take effect without a restart; a broken new pair is refused and the
//! current one stays in use. Outbound HTTPS trusts the public web roots plus `CAIRN_STORE_CA_FILE`.

use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime};

use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};

#[derive(Clone, Debug)]
pub struct TlsFiles {
    pub cert: PathBuf,
    pub key: PathBuf,
    /// Require client certificates signed by this CA (mutual TLS).
    pub client_ca: Option<PathBuf>,
}

fn provider() -> Arc<rustls::crypto::CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

fn load_certs(p: &Path) -> Result<Vec<CertificateDer<'static>>, String> {
    let certs: Vec<_> = CertificateDer::pem_file_iter(p)
        .map_err(|e| format!("{}: {e}", p.display()))?
        .collect::<Result<_, _>>()
        .map_err(|e| format!("{}: {e}", p.display()))?;
    if certs.is_empty() {
        return Err(format!("{}: no certificates", p.display()));
    }
    Ok(certs)
}

/// Builds a server configuration from the files.
pub fn server_config(f: &TlsFiles) -> Result<Arc<rustls::ServerConfig>, String> {
    let certs = load_certs(&f.cert)?;
    let key = PrivateKeyDer::from_pem_file(&f.key).map_err(|e| format!("{}: {e}", f.key.display()))?;
    let builder =
        rustls::ServerConfig::builder_with_provider(provider()).with_safe_default_protocol_versions().map_err(|e| e.to_string())?;
    let builder = match &f.client_ca {
        Some(ca) => {
            let mut roots = rustls::RootCertStore::empty();
            for c in load_certs(ca)? {
                roots.add(c).map_err(|e| format!("{}: {e}", ca.display()))?;
            }
            let v = rustls::server::WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider())
                .build()
                .map_err(|e| e.to_string())?;
            builder.with_client_cert_verifier(v)
        }
        None => builder.with_no_client_auth(),
    };
    let mut cfg = builder.with_single_cert(certs, key).map_err(|e| format!("certificate and key: {e}"))?;
    cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(Arc::new(cfg))
}

/// The current server configuration, reloaded when its files change.
pub struct Reloading {
    files: TlsFiles,
    current: RwLock<(Arc<rustls::ServerConfig>, Vec<Option<SystemTime>>)>,
}

fn mtimes(f: &TlsFiles) -> Vec<Option<SystemTime>> {
    [Some(&f.cert), Some(&f.key), f.client_ca.as_ref()]
        .iter()
        .map(|p| p.and_then(|p| std::fs::metadata(p).and_then(|m| m.modified()).ok()))
        .collect()
}

impl Reloading {
    pub fn new(files: TlsFiles) -> Result<Arc<Reloading>, String> {
        let cfg = server_config(&files)?;
        let m = mtimes(&files);
        Ok(Arc::new(Reloading { files, current: RwLock::new((cfg, m)) }))
    }

    pub fn acceptor(&self) -> tokio_rustls::TlsAcceptor {
        tokio_rustls::TlsAcceptor::from(self.current.read().unwrap().0.clone())
    }

    /// Reloads if the files changed. Returns whether a new configuration is in use.
    pub fn check(&self) -> bool {
        let m = mtimes(&self.files);
        if self.current.read().unwrap().1 == m {
            return false;
        }
        match server_config(&self.files) {
            Ok(cfg) => {
                *self.current.write().unwrap() = (cfg, m);
                tracing::info!("TLS certificate reloaded");
                true
            }
            Err(err) => {
                tracing::error!(error = %err, "new TLS certificate refused; keeping the current one");
                self.current.write().unwrap().1 = m; // do not retry until the files change again
                false
            }
        }
    }

    pub async fn watch(self: Arc<Self>) {
        let every = std::env::var("CAIRN_STORE_TLS_RELOAD_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(30u64).max(1);
        loop {
            tokio::time::sleep(Duration::from_secs(every)).await;
            self.check();
        }
    }
}

/// An HTTP(S) client for outbound calls: public web roots plus `CAIRN_STORE_CA_FILE`.
pub fn https_connector() -> Result<hyper_rustls::HttpsConnector<hyper_util::client::legacy::connect::HttpConnector>, String> {
    let mut roots = rustls::RootCertStore { roots: webpki_roots::TLS_SERVER_ROOTS.to_vec() };
    if let Ok(ca) = std::env::var("CAIRN_STORE_CA_FILE") {
        if !ca.is_empty() {
            for c in load_certs(Path::new(&ca))? {
                roots.add(c).map_err(|e| format!("{ca}: {e}"))?;
            }
        }
    }
    let cfg = rustls::ClientConfig::builder_with_provider(provider())
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .with_root_certificates(roots)
        .with_no_client_auth();
    let mut http = hyper_util::client::legacy::connect::HttpConnector::new();
    http.enforce_http(false);
    http.set_connect_timeout(Some(Duration::from_secs(5)));
    Ok(hyper_rustls::HttpsConnectorBuilder::new().with_tls_config(cfg).https_or_http().enable_http1().wrap_connector(http))
}

/// For the local health probe only: accepts the store's own certificate without checking it (the probe
/// sends no secrets and only reads /-/ready on 127.0.0.1).
#[derive(Debug)]
struct ProbeOnly(Arc<rustls::crypto::CryptoProvider>);

impl rustls::client::danger::ServerCertVerifier for ProbeOnly {
    fn verify_server_cert(
        &self,
        _: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &rustls::pki_types::ServerName<'_>,
        _: &[u8],
        _: rustls::pki_types::UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(m, c, d, &self.0.signature_verification_algorithms)
    }
    fn verify_tls13_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(m, c, d, &self.0.signature_verification_algorithms)
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// The health probe's TLS client configuration (client certificate: CAIRN_STORE_TLS_PROBE_CERT/KEY when
/// the listener requires one).
pub fn probe_config() -> Result<Arc<rustls::ClientConfig>, String> {
    let p = provider();
    let b = rustls::ClientConfig::builder_with_provider(p.clone())
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(ProbeOnly(p)));
    let cfg = match (std::env::var("CAIRN_STORE_TLS_PROBE_CERT"), std::env::var("CAIRN_STORE_TLS_PROBE_KEY")) {
        (Ok(c), Ok(k)) if !c.is_empty() => b
            .with_client_auth_cert(load_certs(Path::new(&c))?, PrivateKeyDer::from_pem_file(&k).map_err(|e| format!("{k}: {e}"))?)
            .map_err(|e| e.to_string())?,
        _ => b.with_no_client_auth(),
    };
    Ok(Arc::new(cfg))
}
