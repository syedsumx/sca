//! Tables on the customer's own storage (L132) under governed storage credentials and external locations (L133).
//!
//! An administrator registers, in Cairn, a **storage credential** (how to reach an Azure storage account, an S3
//! bucket through an IAM role, or a GCS bucket; its secret only as a reference) and an **external location** (a URL
//! prefix and the credential for it). The catalog loads both (from Cairn, `CAIRN_CATALOG_LOCATIONS=cairn`, or from a
//! file, `CAIRN_CATALOG_LOCATIONS_FILE`) and then:
//!
//! * places a table or namespace on non-Cairn storage only inside a registered location (fail closed): an
//!   `abfss://`, `wasbs://` or `gs://` location outside every registered one is refused, and an `s3://` one outside
//!   them stays on Cairn Store as before;
//! * reads and writes the table's metadata files there with short-lived access it gives itself, narrowed to the file;
//! * hands engines access for the one table only, read-only or read-write: an Azure SAS token for the table's
//!   directory (or container), temporary S3 keys from STS AssumeRole with a session policy for the prefix, or a GCS
//!   token downscoped to the prefix;
//! * asks Cairn before each creation and each vend (`POST /v1/storage/decide`), which decides the location
//!   privileges (`CREATE_TABLE`, `READ_FILES`, `WRITE_FILES`) and records every vend in the audit log. When Cairn
//!   cannot be asked, nothing is created or handed out.
//!
//! Secrets are read when used, from where the reference points: `file:<path>`, `k8s:<secret>/<key>` (a mounted
//! Kubernetes Secret under `CAIRN_CATALOG_SECRETS_DIR`), `vault:<mount>/<path>#<field>` (HashiCorp Vault KV v2,
//! `CAIRN_CATALOG_VAULT_ADDR` and `CAIRN_CATALOG_VAULT_TOKEN_FILE`) or `workload` (the pod's workload identity:
//! Entra federated token, AWS web identity, GCP metadata server). Never from the configuration itself. Registrations
//! made in Cairn are held to the operator's `Limits`: no file of the pod's outside one directory, no internal host,
//! and the pod's workload identity only to the provider's own endpoints.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Mutex, RwLock};
use std::time::{Duration, Instant};

use base64::Engine as _;
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::Sha256;
use time::{macros::format_description, OffsetDateTime};

use crate::s3;

/// The SAS version the catalog signs with (directory SAS and the 2020-12-06 string-to-sign).
pub const SAS_VERSION: &str = "2021-08-06";
/// How long handed-out access lasts (and the catalog's own, for one metadata file, much less).
const VEND_SECONDS: i64 = 3600;
const OWN_SECONDS: i64 = 900;

// ---------------------------------------------------------------------------------------------- URLs

/// A storage URL, normalized: which store (`authority`) and the path in it, without leading or trailing `/`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageUrl {
    pub kind: Kind,
    /// azure: `<account>.<endpoint>/<container>`; s3 and gs: the bucket.
    pub authority: String,
    pub path: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Azure,
    S3,
    Gcs,
}

/// Azure location parts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AzureParts {
    pub account: String,
    /// What follows `.dfs.` or `.blob.` in the host: `core.windows.net`, or an emulator's suffix.
    pub endpoint: String,
    pub container: String,
    pub path: String,
}

fn clean_path(p: &str) -> Result<String, String> {
    let segs: Vec<&str> = p.split('/').filter(|s| !s.is_empty()).collect();
    if segs.iter().any(|s| *s == "." || *s == ".." || s.chars().any(|c| c.is_control())) {
        return Err(format!("{p:?}: no . or .. segments"));
    }
    Ok(segs.join("/"))
}

pub fn parse_azure(url: &str) -> Result<AzureParts, String> {
    let (scheme, rest) = url.split_once("://").ok_or_else(|| format!("{url}: not a URL"))?;
    let marker = match scheme {
        "abfs" | "abfss" => ".dfs.",
        "wasb" | "wasbs" => ".blob.",
        _ => return Err(format!("{url}: not an abfss:// or wasbs:// location")),
    };
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    let (container, host) =
        authority.split_once('@').ok_or_else(|| format!("{url}: name the container (abfss://<container>@<account>.dfs.<endpoint>/...)"))?;
    let (account, endpoint) = host.split_once(marker).ok_or_else(|| format!("{url}: the host is <account>{marker}<endpoint>"))?;
    let ok_account = !account.is_empty() && account.len() <= 24 && account.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
    let ok_container = (3..=63).contains(&container.len())
        && container.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        && !container.starts_with('-')
        && !container.ends_with('-');
    if !ok_account || !ok_container || endpoint.is_empty() || endpoint.contains(':') {
        return Err(format!("{url}: not a valid Azure storage location"));
    }
    Ok(AzureParts { account: account.into(), endpoint: endpoint.into(), container: container.into(), path: clean_path(path)? })
}

/// Parses any storage URL the catalog knows: Azure (abfss, abfs, wasbs, wasb), S3 (s3, s3a, s3n) and GCS (gs).
pub fn parse(url: &str) -> Result<StorageUrl, String> {
    if url.starts_with("abfs") || url.starts_with("wasb") {
        let a = parse_azure(url)?;
        return Ok(StorageUrl { kind: Kind::Azure, authority: format!("{}.{}/{}", a.account, a.endpoint, a.container), path: a.path });
    }
    let (kind, rest) = if let Some(r) = ["s3://", "s3a://", "s3n://"].iter().find_map(|p| url.strip_prefix(p)) {
        (Kind::S3, r)
    } else if let Some(r) = url.strip_prefix("gs://") {
        (Kind::Gcs, r)
    } else {
        return Err(format!("{url}: not a storage location the catalog knows (abfss, wasbs, s3, gs)"));
    };
    let (bucket, path) = rest.split_once('/').unwrap_or((rest, ""));
    if bucket.is_empty()
        || bucket.len() > 63
        || !bucket.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '.')
    {
        return Err(format!("{url}: not a valid bucket"));
    }
    Ok(StorageUrl { kind, authority: bucket.into(), path: clean_path(path)? })
}

impl StorageUrl {
    /// `self` itself or anything under it.
    pub fn covers(&self, other: &StorageUrl) -> bool {
        self.kind == other.kind
            && self.authority == other.authority
            && (self.path.is_empty() || other.path == self.path || other.path.starts_with(&format!("{}/", self.path)))
    }

    pub fn overlaps(&self, other: &StorageUrl) -> bool {
        self.covers(other) || other.covers(self)
    }
}

// ---------------------------------------------------------------------------------------- configuration

#[derive(Clone, Debug, Deserialize)]
pub struct CredentialDoc {
    pub name: String,
    pub provider: String,
    #[serde(default)]
    pub config: BTreeMap<String, Value>,
    #[serde(default)]
    pub secret_ref: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct LocationDoc {
    pub name: String,
    pub url: String,
    pub credential: String,
    #[serde(default)]
    pub read_only: bool,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Doc {
    #[serde(default)]
    pub credentials: Vec<CredentialDoc>,
    #[serde(default)]
    pub locations: Vec<LocationDoc>,
}

#[derive(Clone, Debug)]
pub enum AzureAuth {
    /// The storage account key (base64), from the reference.
    AccountKey,
    /// An Entra service principal's client secret, from the reference: a user delegation key, then user delegation SAS.
    ServicePrincipal { tenant: String, client_id: String },
    /// The pod's Entra workload identity (a federated token file): as the service principal, without a secret.
    WorkloadIdentity { tenant: String, client_id: String, token_file: String },
}

#[derive(Clone, Debug)]
pub enum Provider {
    Azure {
        account: String,
        /// The blob service as the catalog reaches it: https://<account>.blob.core.windows.net, or an emulator's URL.
        blob_url: String,
        /// Hierarchical namespace (ADLS Gen2): SAS tokens narrowed to the table's directory. Without it a SAS token
        /// covers a whole container, so locations must then be whole containers.
        hns: bool,
        auth: AzureAuth,
        authority_host: String,
    },
    S3 {
        role_arn: String,
        region: String,
        /// S3 as the catalog reaches it, and as engines do.
        endpoint: String,
        sts_endpoint: String,
        /// The key id the catalog calls STS with (its secret from the reference), or empty for web identity.
        access_key_id: String,
        external_id: Option<String>,
    },
    Gcs {
        project_id: String,
        token_uri: String,
        sts_url: String,
        storage_url: String,
        /// A service account key (JSON) from the reference, or the metadata server (`workload`).
        workload: bool,
    },
}

#[derive(Clone, Debug)]
pub struct Credential {
    pub name: String,
    pub provider: Provider,
    pub secret_ref: Option<String>,
}

#[derive(Clone, Debug)]
pub struct Location {
    pub name: String,
    pub parsed: StorageUrl,
    pub credential: String,
    pub read_only: bool,
}

fn s(cfg: &BTreeMap<String, Value>, k: &str) -> Option<String> {
    cfg.get(k).and_then(|v| v.as_str()).map(str::to_string).filter(|v| !v.is_empty())
}

fn req(cfg: &BTreeMap<String, Value>, k: &str, what: &str) -> Result<String, String> {
    s(cfg, k).ok_or_else(|| format!("credential {what}: config.{k} is required"))
}

/// A secret reference's form, checked without reading it.
pub fn check_ref(r: &str) -> Result<(), String> {
    let ok = match r.split_once(':') {
        Some(("file", p)) => p.starts_with('/'),
        Some(("k8s", p)) => p.split_once('/').is_some_and(|(a, b)| !a.is_empty() && !b.is_empty() && !b.contains('/')),
        Some(("vault", p)) => p.split_once('#').is_some_and(|(a, b)| a.contains('/') && !b.is_empty()),
        None => r == "workload",
        _ => false,
    };
    if ok {
        Ok(())
    } else {
        Err("secret_ref: file:/path, k8s:<secret>/<key>, vault:<mount>/<path>#<field> or workload".into())
    }
}

impl Credential {
    pub fn from_doc(d: &CredentialDoc) -> Result<Credential, String> {
        let c = &d.config;
        let name = &d.name;
        if let Some(r) = &d.secret_ref {
            check_ref(r).map_err(|e| format!("credential {name}: {e}"))?;
        }
        let workload = d.secret_ref.as_deref() == Some("workload");
        let provider = match d.provider.as_str() {
            "azure" => {
                let account = req(c, "account", name)?;
                let auth = match s(c, "auth").as_deref().unwrap_or("account_key") {
                    "account_key" => AzureAuth::AccountKey,
                    "service_principal" => {
                        AzureAuth::ServicePrincipal { tenant: req(c, "tenant_id", name)?, client_id: req(c, "client_id", name)? }
                    }
                    "workload_identity" => AzureAuth::WorkloadIdentity {
                        tenant: req(c, "tenant_id", name)?,
                        client_id: req(c, "client_id", name)?,
                        token_file: s(c, "federated_token_file")
                            .or_else(|| std::env::var("AZURE_FEDERATED_TOKEN_FILE").ok())
                            .ok_or_else(|| format!("credential {name}: workload identity needs a federated token file"))?,
                    },
                    a => return Err(format!("credential {name}: auth {a:?}: account_key, service_principal or workload_identity")),
                };
                let needs_secret = !matches!(auth, AzureAuth::WorkloadIdentity { .. });
                if needs_secret && (d.secret_ref.is_none() || workload) {
                    return Err(format!("credential {name}: secret_ref is required for this auth"));
                }
                Provider::Azure {
                    blob_url: s(c, "endpoint")
                        .unwrap_or_else(|| format!("https://{account}.blob.core.windows.net"))
                        .trim_end_matches('/')
                        .into(),
                    account,
                    hns: c.get("hierarchical_namespace").and_then(|v| v.as_bool()).unwrap_or(true),
                    auth,
                    authority_host: s(c, "authority_host").unwrap_or_else(|| "https://login.microsoftonline.com".into()),
                }
            }
            "s3" => {
                let region = s(c, "region").unwrap_or_else(|| "us-east-1".into());
                let access_key_id = s(c, "access_key_id").unwrap_or_default();
                if access_key_id.is_empty() != workload {
                    return Err(format!("credential {name}: config.access_key_id with a secret_ref, or secret_ref workload alone"));
                }
                Provider::S3 {
                    role_arn: req(c, "role_arn", name)?,
                    endpoint: s(c, "endpoint").unwrap_or_else(|| format!("https://s3.{region}.amazonaws.com")).trim_end_matches('/').into(),
                    sts_endpoint: s(c, "sts_endpoint")
                        .unwrap_or_else(|| format!("https://sts.{region}.amazonaws.com"))
                        .trim_end_matches('/')
                        .into(),
                    region,
                    access_key_id,
                    external_id: s(c, "external_id"),
                }
            }
            "gcs" => {
                if d.secret_ref.is_none() {
                    return Err(format!("credential {name}: secret_ref (a service account key, or workload)"));
                }
                Provider::Gcs {
                    project_id: s(c, "project_id").unwrap_or_default(),
                    token_uri: s(c, "token_uri").unwrap_or_else(|| "https://oauth2.googleapis.com/token".into()),
                    sts_url: s(c, "sts_url").unwrap_or_else(|| "https://sts.googleapis.com/v1/token".into()),
                    storage_url: s(c, "storage_url")
                        .unwrap_or_else(|| "https://storage.googleapis.com".into())
                        .trim_end_matches('/')
                        .into(),
                    workload,
                }
            }
            p => return Err(format!("credential {name}: provider {p:?}: azure, s3 or gcs")),
        };
        Ok(Credential { name: name.clone(), provider, secret_ref: d.secret_ref.clone() })
    }

    fn kind(&self) -> Kind {
        match self.provider {
            Provider::Azure { .. } => Kind::Azure,
            Provider::S3 { .. } => Kind::S3,
            Provider::Gcs { .. } => Kind::Gcs,
        }
    }
}

/// The registered locations and their credentials, checked as a whole: a location's credential exists and is for
/// its kind of storage (and its Azure account), no two locations overlap, none overlaps Cairn's own storage.
#[derive(Debug, Default)]
pub struct Snapshot {
    pub locations: Vec<Location>,
    pub credentials: HashMap<String, Credential>,
}

impl Snapshot {
    /// All or nothing: one entry that does not hold together refuses the whole document (a file given at start).
    pub fn build(doc: &Doc, reserved: &[StorageUrl]) -> Result<Snapshot, String> {
        Snapshot::build_with(doc, reserved, true, None)
    }

    /// Entry by entry (registrations loaded from Cairn, which checks the same rules): an entry that does not hold
    /// together, or a credential outside the operator's `limits`, is left out and logged, and locations that overlap
    /// are all left out, so none of them is used for another's place.
    pub fn lenient(doc: &Doc, reserved: &[StorageUrl], limits: &Limits) -> Snapshot {
        Snapshot::build_with(doc, reserved, false, Some(limits)).unwrap_or_default()
    }

    fn build_with(doc: &Doc, reserved: &[StorageUrl], strict: bool, limits: Option<&Limits>) -> Result<Snapshot, String> {
        let skip = |e: String| -> Result<(), String> {
            if strict {
                return Err(e);
            }
            tracing::error!(error = %e, "an external location registration was left out");
            Ok(())
        };
        let mut credentials = HashMap::new();
        for d in &doc.credentials {
            match Credential::from_doc(d).and_then(|c| limits.map_or(Ok(()), |l| l.check(d, &c)).map(|_| c)) {
                Ok(c) => {
                    credentials.insert(c.name.clone(), c);
                }
                Err(e) => skip(e)?,
            }
        }
        let mut locations: Vec<Location> = vec![];
        for d in &doc.locations {
            match Snapshot::location(d, &credentials, reserved) {
                Ok(l) => locations.push(l),
                Err(e) => skip(e)?,
            }
        }
        let mut overlapping = std::collections::BTreeSet::new();
        for (i, a) in locations.iter().enumerate() {
            for b in &locations[i + 1..] {
                if a.parsed.overlaps(&b.parsed) {
                    skip(format!("location {} overlaps location {}", b.name, a.name))?;
                    overlapping.insert(a.name.clone());
                    overlapping.insert(b.name.clone());
                }
            }
        }
        locations.retain(|l| !overlapping.contains(&l.name));
        Ok(Snapshot { locations, credentials })
    }

    fn location(d: &LocationDoc, credentials: &HashMap<String, Credential>, reserved: &[StorageUrl]) -> Result<Location, String> {
        let parsed = parse(&d.url).map_err(|e| format!("location {}: {e}", d.name))?;
        let cred = credentials.get(&d.credential).ok_or_else(|| format!("location {}: no credential {}", d.name, d.credential))?;
        if cred.kind() != parsed.kind {
            return Err(format!("location {}: credential {} is for another kind of storage", d.name, d.credential));
        }
        if let Provider::Azure { account, hns, .. } = &cred.provider {
            let a = parse_azure(&d.url)?;
            if &a.account != account {
                return Err(format!("location {}: credential {} is for storage account {account}", d.name, d.credential));
            }
            if !hns && !a.path.is_empty() {
                return Err(format!(
                    "location {}: without a hierarchical namespace a SAS token covers a whole container, so the location must be one",
                    d.name
                ));
            }
        }
        if reserved.iter().any(|r| r.overlaps(&parsed)) {
            return Err(format!("location {} overlaps Cairn's own storage", d.name));
        }
        Ok(Location { name: d.name.clone(), parsed, credential: d.credential.clone(), read_only: d.read_only })
    }

    /// The registered location `url` is in, if any.
    pub fn find(&self, url: &StorageUrl) -> Option<(&Location, &Credential)> {
        let l = self.locations.iter().find(|l| l.parsed.covers(url))?;
        Some((l, self.credentials.get(&l.credential)?))
    }
}

// --------------------------------------------------------------------------------------------- secrets

fn read_secret_file(path: &str) -> Result<String, String> {
    let v = std::fs::read_to_string(path).map_err(|e| format!("the secret file could not be read: {e}"))?;
    let v = v.trim().to_string();
    if v.is_empty() {
        return Err("the secret file is empty".into());
    }
    Ok(v)
}

/// A request's error with its causes (such as an address the limits refused), not only "error sending request".
fn why(e: &reqwest::Error) -> String {
    let mut out = e.to_string();
    let mut src = std::error::Error::source(e);
    while let Some(s) = src {
        out.push_str(&format!(": {s}"));
        src = s.source();
    }
    out
}

// ------------------------------------------------------------------------------------------- limits

/// The settings in a credential that name a host the catalog calls.
const ENDPOINT_KEYS: [&str; 6] = ["endpoint", "sts_endpoint", "authority_host", "token_uri", "sts_url", "storage_url"];

/// What registrations made in Cairn (by its administrators, `CAIRN_CATALOG_LOCATIONS=cairn`) may reach from the
/// catalog's pod, as its operator sets it. The pod holds the operator's own secrets (its environment, its tokens,
/// its workload identity): an administrator of Cairn is not thereby one of the pod's.
///
/// * `file:` references, and an Azure `federated_token_file`, only under `CAIRN_CATALOG_SECRET_FILES_DIR` (none
///   without it): `file:/proc/self/environ` would otherwise send the pod's environment as a "client secret".
/// * No endpoint on an internal network (loopback, link-local such as the cloud metadata services, private,
///   shared, unique local) unless its host is in `CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS` (names, addresses or CIDR
///   ranges): checked on the URL, and again on every address a name resolves to when the catalog connects; the
///   catalog follows no redirects there.
/// * A credential using the catalog's own workload identity (`workload`, Azure `workload_identity`) names no custom
///   endpoint outside those trusted hosts: the pod's identity token, or a token made from it, goes to them.
///
/// Registrations in a file (`CAIRN_CATALOG_LOCATIONS_FILE`) are the operator's own and not limited.
#[derive(Clone, Debug, Default)]
pub struct Limits {
    pub secret_files_dir: Option<std::path::PathBuf>,
    pub trusted: Vec<Trusted>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Trusted {
    Name(String),
    Net(std::net::IpAddr, u8),
}

/// Loopback, link-local (the cloud metadata services), private, shared (CGNAT), unique local, unspecified,
/// multicast, reserved and documentation addresses: anything that is not a public host.
pub fn internal_ip(ip: std::net::IpAddr) -> bool {
    use std::net::IpAddr;
    match ip {
        IpAddr::V4(v) => {
            let o = v.octets();
            v.is_loopback()
                || v.is_private()
                || v.is_link_local()
                || v.is_unspecified()
                || v.is_broadcast()
                || v.is_multicast()
                || v.is_documentation()
                || o[0] == 0
                || (o[0] == 100 && (o[1] & 0xc0) == 64)
                || (o[0] == 192 && o[1] == 0 && o[2] == 0)
                || (o[0] == 198 && (o[1] & 0xfe) == 18)
                || o[0] >= 240
        }
        IpAddr::V6(v) => {
            let s = v.segments();
            if let Some(v4) = v.to_ipv4_mapped() {
                return internal_ip(IpAddr::V4(v4));
            }
            if s[0] == 0x64 && s[1] == 0xff9b && s[2..6] == [0, 0, 0, 0] {
                // NAT64: the IPv4 address it stands for.
                let v4 = std::net::Ipv4Addr::new((s[6] >> 8) as u8, s[6] as u8, (s[7] >> 8) as u8, s[7] as u8);
                return internal_ip(IpAddr::V4(v4));
            }
            v.is_loopback()
                || v.is_unspecified()
                || v.is_multicast()
                || (s[0] & 0xfe00) == 0xfc00
                || (s[0] & 0xffc0) == 0xfe80
                || (s[0] & 0xffc0) == 0xfec0
                || s[0] == 0x2001 && s[1] == 0xdb8
                || s[..6] == [0, 0, 0, 0, 0, 0]
        }
    }
}

fn in_net(ip: std::net::IpAddr, net: std::net::IpAddr, bits: u8) -> bool {
    use std::net::IpAddr;
    let (ip, net, len) = match (ip, net) {
        (IpAddr::V4(a), IpAddr::V4(b)) => (u128::from(u32::from(a)), u128::from(u32::from(b)), 32u32),
        (IpAddr::V6(a), IpAddr::V6(b)) => (u128::from(a), u128::from(b), 128u32),
        _ => return false,
    };
    let bits = u32::from(bits).min(len);
    if bits == 0 {
        return true;
    }
    let shift = len - bits;
    (ip >> shift) == (net >> shift)
}

/// A URL's host as the catalog would connect to it: an IP address (without IPv6 brackets) or a lowercase name.
fn url_host(url: &str) -> Result<String, String> {
    let u = reqwest::Url::parse(url).map_err(|e| format!("{url}: not a URL ({e})"))?;
    if !matches!(u.scheme(), "http" | "https") {
        return Err(format!("{url}: not an http(s) URL"));
    }
    if !u.username().is_empty() || u.password().is_some() {
        return Err(format!("{url}: no user or password in an endpoint"));
    }
    let host = u.host_str().ok_or_else(|| format!("{url}: no host"))?;
    Ok(host.trim_start_matches('[').trim_end_matches(']').trim_end_matches('.').to_ascii_lowercase())
}

impl Limits {
    /// `CAIRN_CATALOG_SECRET_FILES_DIR` and `CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS`.
    pub fn new(secret_files_dir: Option<&str>, trusted: &str) -> Result<Limits, String> {
        let secret_files_dir = match secret_files_dir.map(str::trim).filter(|d| !d.is_empty()) {
            Some(d) if d.starts_with('/') && !d.split('/').any(|s| s == "..") => Some(std::path::PathBuf::from(d)),
            Some(d) => return Err(format!("CAIRN_CATALOG_SECRET_FILES_DIR: {d:?} is not an absolute path")),
            None => None,
        };
        let mut out = vec![];
        for t in trusted.split(',').map(str::trim).filter(|t| !t.is_empty()) {
            let t = t.to_ascii_lowercase();
            out.push(if let Some((net, bits)) = t.split_once('/') {
                let net: std::net::IpAddr =
                    net.parse().map_err(|_| format!("CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS: {t}: not an address range"))?;
                let bits: u8 = bits.parse().map_err(|_| format!("CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS: {t}: not an address range"))?;
                Trusted::Net(net, bits)
            } else if let Ok(ip) = t.trim_start_matches('[').trim_end_matches(']').parse::<std::net::IpAddr>() {
                Trusted::Net(ip, if ip.is_ipv4() { 32 } else { 128 })
            } else {
                Trusted::Name(t.trim_end_matches('.').to_string())
            });
        }
        Ok(Limits { secret_files_dir, trusted: out })
    }

    fn trusts_name(&self, host: &str) -> bool {
        let host = host.trim_end_matches('.').to_ascii_lowercase();
        self.trusted.iter().any(|t| matches!(t, Trusted::Name(n) if *n == host))
    }

    fn trusts_ip(&self, ip: std::net::IpAddr) -> bool {
        self.trusted.iter().any(|t| matches!(t, Trusted::Net(n, b) if in_net(ip, *n, *b)))
    }

    /// May the catalog call `host` (as written in a URL)? Names on internal networks are found when they resolve.
    fn host_allowed(&self, host: &str) -> Result<(), String> {
        if self.trusts_name(host) {
            return Ok(());
        }
        if let Ok(ip) = host.parse::<std::net::IpAddr>() {
            if internal_ip(ip) && !self.trusts_ip(ip) {
                return Err(format!("{host} is an internal address (not in CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS)"));
            }
            return Ok(());
        }
        if host == "localhost" || host.ends_with(".localhost") {
            return Err(format!("{host} is this machine (not in CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS)"));
        }
        Ok(())
    }

    fn trusted_host(&self, host: &str) -> bool {
        self.trusts_name(host) || host.parse::<std::net::IpAddr>().is_ok_and(|ip| self.trusts_ip(ip))
    }

    /// A file a registration names, by its path alone: under the secret files directory, without `..`.
    fn file_allowed(&self, path: &str) -> Result<(), String> {
        let Some(dir) = &self.secret_files_dir else {
            return Err("file: references registered in Cairn need CAIRN_CATALOG_SECRET_FILES_DIR (use k8s: or vault:)".into());
        };
        let p = std::path::Path::new(path);
        if !p.is_absolute() || path.split('/').any(|s| s == "..") || !p.starts_with(dir) || p == dir.as_path() {
            return Err(format!("{path} is not under CAIRN_CATALOG_SECRET_FILES_DIR ({})", dir.display()));
        }
        Ok(())
    }

    /// Reads a file a registration names: its path as `file_allowed` says, and where it really is (links followed)
    /// still under the directory.
    fn read_file(&self, path: &str) -> Result<String, String> {
        self.file_allowed(path)?;
        let dir = self.secret_files_dir.as_ref().expect("checked");
        let real = std::fs::canonicalize(path).map_err(|e| format!("the secret file could not be read: {e}"))?;
        let real_dir = std::fs::canonicalize(dir).map_err(|e| format!("CAIRN_CATALOG_SECRET_FILES_DIR: {e}"))?;
        if !real.starts_with(&real_dir) {
            return Err(format!("{path} leads outside CAIRN_CATALOG_SECRET_FILES_DIR ({})", dir.display()));
        }
        read_secret_file(&real.to_string_lossy())
    }

    /// A credential registered in Cairn, held to these limits (without reading or calling anything).
    pub fn check(&self, d: &CredentialDoc, c: &Credential) -> Result<(), String> {
        let name = &d.name;
        if let Some(p) = d.secret_ref.as_deref().and_then(|r| r.strip_prefix("file:")) {
            self.file_allowed(p).map_err(|e| format!("credential {name}: {e}"))?;
        }
        if let Some(f) = s(&d.config, "federated_token_file") {
            self.file_allowed(&f).map_err(|e| format!("credential {name}: federated_token_file: {e}"))?;
        }
        let workload = match &c.provider {
            Provider::Azure { auth, .. } => matches!(auth, AzureAuth::WorkloadIdentity { .. }),
            Provider::S3 { access_key_id, .. } => access_key_id.is_empty(),
            Provider::Gcs { workload, .. } => *workload,
        };
        for k in ENDPOINT_KEYS {
            let Some(url) = s(&d.config, k) else { continue };
            let host = url_host(&url).map_err(|e| format!("credential {name}: config.{k}: {e}"))?;
            self.host_allowed(&host).map_err(|e| format!("credential {name}: config.{k}: {e}"))?;
            if workload && !self.trusted_host(&host) {
                return Err(format!(
                    "credential {name}: config.{k}: a credential using the catalog's workload identity calls only its \
                     provider's own endpoints, or hosts in CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS"
                ));
            }
        }
        Ok(())
    }
}

/// Resolves names for calls made with registrations from Cairn: only to addresses the limits allow.
struct Guard(std::sync::Arc<Limits>);

impl reqwest::dns::Resolve for Guard {
    fn resolve(&self, name: reqwest::dns::Name) -> reqwest::dns::Resolving {
        let limits = self.0.clone();
        let host = name.as_str().to_string();
        Box::pin(async move {
            let addrs: Vec<std::net::SocketAddr> = tokio::net::lookup_host((host.as_str(), 0)).await?.collect();
            let trusted = limits.trusts_name(&host);
            let ok: Vec<std::net::SocketAddr> =
                addrs.into_iter().filter(|a| trusted || !internal_ip(a.ip()) || limits.trusts_ip(a.ip())).collect();
            if ok.is_empty() {
                let e: Box<dyn std::error::Error + Send + Sync> =
                    format!("{host} is on an internal network (not in CAIRN_CATALOG_STORAGE_TRUSTED_HOSTS)").into();
                return Err(e);
            }
            Ok(Box::new(ok.into_iter()) as reqwest::dns::Addrs)
        })
    }
}

// ------------------------------------------------------------------------------------------ the service

/// What an engine gets for a table or a path: client properties, and when they expire.
#[derive(Clone, Debug)]
pub struct Vended {
    pub config: BTreeMap<String, String>,
    pub expires_ms: i64,
}

/// A decision Cairn made (or the reason it could not).
pub enum Asked {
    Allowed,
    Refused(String),
    Unavailable(String),
}

pub struct External {
    /// Where the registrations come from: Cairn's API, or a file.
    source: Source,
    snapshot: RwLock<(Option<Instant>, std::sync::Arc<Snapshot>)>,
    reserved: Vec<StorageUrl>,
    http: reqwest::Client,
    /// For calls the registrations name (storage, token and STS endpoints): held to `limits` for registrations
    /// from Cairn, with no redirects followed.
    storage: reqwest::Client,
    /// The operator's limits on registrations from Cairn (none for a file's).
    limits: Option<std::sync::Arc<Limits>>,
    refresh: Duration,
    /// Azure user delegation keys, Entra tokens and GCS tokens, reused until shortly before they expire.
    tokens: Mutex<HashMap<String, (i64, String)>>,
    udks: Mutex<HashMap<String, (i64, Udk)>>,
}

pub enum Source {
    Off,
    File(String),
    Cairn { url: String, token: String },
}

#[derive(Clone, Debug)]
pub struct Udk {
    pub oid: String,
    pub tid: String,
    pub start: String,
    pub expiry: String,
    pub service: String,
    pub version: String,
    pub value: String,
}

fn now_s() -> i64 {
    OffsetDateTime::now_utc().unix_timestamp()
}

fn iso(t: i64) -> String {
    OffsetDateTime::from_unix_timestamp(t)
        .unwrap_or(OffsetDateTime::UNIX_EPOCH)
        .format(format_description!("[year]-[month]-[day]T[hour]:[minute]:[second]Z"))
        .unwrap_or_default()
}

/// What a query value or a path segment keeps unencoded (RFC 3986's unreserved characters).
const UNRESERVED: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC.remove(b'-').remove(b'.').remove(b'_').remove(b'~');

fn enc(s: &str) -> String {
    percent_encoding::utf8_percent_encode(s, UNRESERVED).to_string()
}

/// Encodes a blob path for a URL, keeping `/`.
fn enc_path(p: &str) -> String {
    p.split('/').map(enc).collect::<Vec<_>>().join("/")
}

/// The folder a file is in: what the catalog's own access to one metadata file is narrowed to.
fn parent(path: &str) -> &str {
    path.rsplit_once('/').map(|(d, _)| d).unwrap_or("")
}

fn xml(x: &str, tag: &str) -> Option<String> {
    s3::xml_text(x, tag).map(str::to_string)
}

/// The fields of an Azure service or user delegation SAS (version 2020-12-06 and later), signed with `key`.
#[allow(clippy::too_many_arguments)]
pub fn sas_token(
    key: &[u8],
    account: &str,
    container: &str,
    path: &str,
    resource: &str,
    permissions: &str,
    start: &str,
    expiry: &str,
    udk: Option<&Udk>,
) -> String {
    let canonical = if path.is_empty() { format!("/blob/{account}/{container}") } else { format!("/blob/{account}/{container}/{path}") };
    let mut lines: Vec<String> = vec![permissions.into(), start.into(), expiry.into(), canonical];
    match udk {
        None => lines.extend(["".into(), "".into(), "https".into(), SAS_VERSION.into(), resource.into(), "".into(), "".into()]),
        Some(u) => {
            lines.extend([u.oid.clone(), u.tid.clone(), u.start.clone(), u.expiry.clone(), u.service.clone(), u.version.clone()]);
            lines.extend([
                "".into(),
                "".into(),
                "".into(),
                "".into(),
                "https".into(),
                SAS_VERSION.into(),
                resource.into(),
                "".into(),
                "".into(),
            ]);
        }
    }
    lines.extend(["".into(), "".into(), "".into(), "".into(), "".into()]);
    let to_sign = lines.join("\n");
    let mut mac = <Hmac<Sha256>>::new_from_slice(key).expect("hmac accepts any key");
    mac.update(to_sign.as_bytes());
    let sig = base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes());
    let mut q = vec![
        format!("sv={SAS_VERSION}"),
        "spr=https".to_string(),
        format!("st={}", enc(start)),
        format!("se={}", enc(expiry)),
        format!("sr={resource}"),
        format!("sp={permissions}"),
    ];
    if resource == "d" {
        q.push(format!("sdd={}", path.split('/').filter(|s| !s.is_empty()).count()));
    }
    if let Some(u) = udk {
        q.extend([
            format!("skoid={}", enc(&u.oid)),
            format!("sktid={}", enc(&u.tid)),
            format!("skt={}", enc(&u.start)),
            format!("ske={}", enc(&u.expiry)),
            format!("sks={}", enc(&u.service)),
            format!("skv={}", enc(&u.version)),
        ]);
    }
    q.push(format!("sig={}", enc(&sig)));
    q.join("&")
}

/// Characters IAM reads as wildcards (`*`, `?`) or policy variables (`${...}`) in a resource or in `s3:prefix`: in
/// a prefix they would widen a session policy beyond it (`raw/t*` reaches `raw/tother`), so such S3 paths are refused.
pub const S3_POLICY_CHARS: [char; 3] = ['*', '?', '$'];

/// The S3 session policy for a prefix: its objects, and listing under it (read-only or read-write). A prefix IAM
/// would read as a pattern is refused.
pub fn s3_policy(bucket: &str, prefix: &str, write: bool) -> Result<String, String> {
    if prefix.contains(S3_POLICY_CHARS) {
        return Err(format!("s3://{bucket}/{prefix}: S3 access cannot be narrowed to a path with *, ? or $"));
    }
    let objects = if write {
        json!(["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"])
    } else {
        json!(["s3:GetObject"])
    };
    let (res, list) = if prefix.is_empty() {
        (format!("arn:aws:s3:::{bucket}/*"), json!(["*"]))
    } else {
        // Listing under `<prefix>/` only: the bare prefix would also list `<prefix>x/...`, another path's keys.
        (format!("arn:aws:s3:::{bucket}/{prefix}/*"), json!([format!("{prefix}/*")]))
    };
    Ok(json!({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": objects, "Resource": res},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": format!("arn:aws:s3:::{bucket}"),
         "Condition": {"StringLike": {"s3:prefix": list}}}]})
    .to_string())
}

/// A string as the content of a CEL single-quoted literal: a quote or a backslash in a path cannot end the literal.
fn cel_str(s: &str) -> String {
    s.replace('\\', "\\\\").replace('\'', "\\'")
}

/// The GCS credential access boundary for a prefix (read: objectViewer; write: objectAdmin).
pub fn gcs_boundary(bucket: &str, prefix: &str, write: bool) -> String {
    let role = if write { "inRole:roles/storage.objectAdmin" } else { "inRole:roles/storage.objectViewer" };
    let mut rule =
        json!({"availablePermissions": [role], "availableResource": format!("//storage.googleapis.com/projects/_/buckets/{bucket}")});
    if !prefix.is_empty() {
        rule["availabilityCondition"] = json!({"expression": format!(
            "resource.name.startsWith('projects/_/buckets/{bucket}/objects/{p}/') || \
             api.getAttribute('storage.googleapis.com/objectListPrefix', '').startsWith('{p}/')",
            p = cel_str(prefix))});
    }
    json!({"accessBoundary": {"accessBoundaryRules": [rule]}}).to_string()
}

impl External {
    pub fn new(
        source: Source,
        reserved: Vec<StorageUrl>,
        ca_file: Option<String>,
        refresh: Duration,
        limits: Limits,
    ) -> Result<External, String> {
        let mut certs = vec![];
        if let Some(f) = ca_file {
            let pem = std::fs::read(&f).map_err(|e| format!("{f}: {e}"))?;
            certs = reqwest::Certificate::from_pem_bundle(&pem).map_err(|e| format!("{f}: {e}"))?;
        }
        let builder =
            || certs.iter().cloned().fold(reqwest::Client::builder().timeout(Duration::from_secs(30)), |b, c| b.add_root_certificate(c));
        let http = builder().build().map_err(|e| e.to_string())?;
        let limits = matches!(source, Source::Cairn { .. }).then(|| std::sync::Arc::new(limits));
        let storage = match &limits {
            Some(l) => builder()
                .dns_resolver(std::sync::Arc::new(Guard(l.clone())))
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .map_err(|e| e.to_string())?,
            None => http.clone(),
        };
        let ext = External {
            source,
            snapshot: RwLock::new((None, Default::default())),
            reserved,
            http,
            storage,
            limits,
            refresh,
            tokens: Default::default(),
            udks: Default::default(),
        };
        if let Source::File(f) = &ext.source {
            // A file that does not load stops the catalog.
            let text = std::fs::read_to_string(f).map_err(|e| format!("{f}: {e}"))?;
            ext.install(&text)?;
        }
        Ok(ext)
    }

    /// Cairn's own storage (the warehouse, the regions' locations) covers `u`.
    pub fn reserved_covers(&self, u: &StorageUrl) -> bool {
        self.reserved.iter().any(|r| r.covers(u))
    }

    /// `u` is one of Cairn's own storage places (the warehouse, a region's location, CAIRN_CATALOG_STORE_URLS), or
    /// holds one: a table there would get access to everything under it.
    pub fn reserved_within(&self, u: &StorageUrl) -> bool {
        self.reserved.iter().any(|r| u.covers(r))
    }

    pub fn active(&self) -> bool {
        !matches!(self.source, Source::Off)
    }

    fn install(&self, text: &str) -> Result<usize, String> {
        let doc: Doc = serde_json::from_str(text).map_err(|e| format!("not a locations document: {e}"))?;
        let snap = match self.source {
            Source::Cairn { .. } => Snapshot::lenient(&doc, &self.reserved, self.limits.as_deref().unwrap_or(&Limits::default())),
            _ => Snapshot::build(&doc, &self.reserved)?,
        };
        let n = snap.locations.len();
        *self.snapshot.write().unwrap() = (Some(Instant::now()), std::sync::Arc::new(snap));
        Ok(n)
    }

    /// Loads the registrations from Cairn (in a loop, every `refresh`); what does not load keeps the last good ones,
    /// which expire after five missed refreshes (nothing external is then placed or handed out).
    pub async fn refresh_from_cairn(&self) -> Result<usize, String> {
        let Source::Cairn { url, token } = &self.source else { return Ok(0) };
        let r = self
            .http
            .get(format!("{}/v1/storage/catalog-config", url.trim_end_matches('/')))
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| format!("Cairn: {e}"))?;
        let status = r.status();
        let text = r.text().await.map_err(|e| e.to_string())?;
        if !status.is_success() {
            return Err(format!("Cairn answered {status}"));
        }
        self.install(&text)
    }

    pub fn refresh_every(&self) -> Duration {
        self.refresh
    }

    /// The current registrations, or why there are none to use.
    pub fn snapshot(&self) -> Result<std::sync::Arc<Snapshot>, String> {
        let g = self.snapshot.read().unwrap();
        match (&self.source, g.0) {
            (Source::Off, _) => Ok(g.1.clone()),
            (Source::File(_), _) => Ok(g.1.clone()),
            (Source::Cairn { .. }, Some(at)) if at.elapsed() < self.refresh * 5 => Ok(g.1.clone()),
            (Source::Cairn { .. }, _) => Err("the external locations could not be loaded from Cairn".into()),
        }
    }

    /// Asks Cairn (when it is the source) to decide a location privilege for a Cairn user and to record the event;
    /// for the catalog's own tokens only records it. Every vend and every creation passes here.
    pub async fn decide(&self, event: &Value) -> Asked {
        match &self.source {
            Source::Cairn { url, token } => {
                let r =
                    self.http.post(format!("{}/v1/storage/decide", url.trim_end_matches('/'))).bearer_auth(token).json(event).send().await;
                let r = match r {
                    Ok(r) => r,
                    Err(e) => return Asked::Unavailable(format!("Cairn: {e}")),
                };
                if !r.status().is_success() {
                    return Asked::Unavailable(format!("Cairn answered {}", r.status()));
                }
                match r.json::<Value>().await {
                    Ok(v) if v["allowed"] == true => Asked::Allowed,
                    Ok(v) => Asked::Refused(v["reason"].as_str().unwrap_or("refused by Cairn's policies").to_string()),
                    Err(e) => Asked::Unavailable(format!("Cairn's answer: {e}")),
                }
            }
            _ => {
                if event["user"].is_string() {
                    // Users are decided by Cairn alone: without it, nothing.
                    return Asked::Unavailable("Cairn-issued tokens need CAIRN_CATALOG_LOCATIONS=cairn".into());
                }
                tracing::info!(target: "audit", event = %event, "storage access");
                Asked::Allowed
            }
        }
    }

    // ---- secrets --------------------------------------------------------------------------------------

    async fn secret(&self, r: &str) -> Result<String, String> {
        match r.split_once(':') {
            Some(("file", p)) => match &self.limits {
                Some(l) => l.read_file(p),
                None => read_secret_file(p),
            },
            Some(("k8s", p)) => {
                let dir = std::env::var("CAIRN_CATALOG_SECRETS_DIR").unwrap_or_else(|_| "/var/run/cairn/storage-secrets".into());
                let (secret, key) = p.split_once('/').ok_or("k8s:<secret>/<key>")?;
                if [secret, key].iter().any(|x| x.contains("..") || x.contains('/')) {
                    return Err("k8s:<secret>/<key>".into());
                }
                read_secret_file(&format!("{}/{secret}/{key}", dir.trim_end_matches('/')))
            }
            Some(("vault", p)) => {
                let addr = std::env::var("CAIRN_CATALOG_VAULT_ADDR").map_err(|_| "vault: references need CAIRN_CATALOG_VAULT_ADDR")?;
                let token_file =
                    std::env::var("CAIRN_CATALOG_VAULT_TOKEN_FILE").map_err(|_| "vault: references need CAIRN_CATALOG_VAULT_TOKEN_FILE")?;
                let token = read_secret_file(&token_file)?;
                let (path, field) = p.split_once('#').ok_or("vault:<mount>/<path>#<field>")?;
                let (mount, rest) = path.split_once('/').ok_or("vault:<mount>/<path>#<field>")?;
                let v: Value = self
                    .http
                    .get(format!("{}/v1/{mount}/data/{rest}", addr.trim_end_matches('/')))
                    .header("X-Vault-Token", token)
                    .send()
                    .await
                    .map_err(|e| format!("Vault: {e}"))?
                    .error_for_status()
                    .map_err(|e| format!("Vault: {e}"))?
                    .json()
                    .await
                    .map_err(|e| format!("Vault: {e}"))?;
                v["data"]["data"][field].as_str().map(str::to_string).ok_or_else(|| format!("Vault: no field {field}"))
            }
            _ => Err("this credential's secret_ref names no secret".into()),
        }
    }

    /// An Azure federated token file: the pod's own (AZURE_FEDERATED_TOKEN_FILE), or one a registration names.
    fn token_file(&self, path: &str) -> Result<String, String> {
        match &self.limits {
            Some(l) if std::env::var("AZURE_FEDERATED_TOKEN_FILE").ok().as_deref() != Some(path) => l.read_file(path),
            _ => read_secret_file(path),
        }
    }

    async fn cred_secret(&self, c: &Credential) -> Result<String, String> {
        self.secret(c.secret_ref.as_deref().ok_or("the credential has no secret_ref")?).await
    }

    // ---- Azure ----------------------------------------------------------------------------------------

    async fn entra_token(&self, c: &Credential) -> Result<String, String> {
        let Provider::Azure { auth, authority_host, .. } = &c.provider else { return Err("not Azure".into()) };
        let cache_key = format!("entra:{}", c.name);
        if let Some((exp, t)) = self.tokens.lock().unwrap().get(&cache_key) {
            if exp - now_s() > 300 {
                return Ok(t.clone());
            }
        }
        let (tenant, mut form) = match auth {
            AzureAuth::ServicePrincipal { tenant, client_id } => {
                (tenant, vec![("client_id", client_id.clone()), ("client_secret", self.cred_secret(c).await?)])
            }
            AzureAuth::WorkloadIdentity { tenant, client_id, token_file } => (
                tenant,
                vec![
                    ("client_id", client_id.clone()),
                    ("client_assertion_type", "urn:ietf:params:oauth:client-assertion-type:jwt-bearer".into()),
                    ("client_assertion", self.token_file(token_file)?),
                ],
            ),
            AzureAuth::AccountKey => return Err("an account key needs no token".into()),
        };
        form.push(("grant_type", "client_credentials".into()));
        form.push(("scope", "https://storage.azure.com/.default".into()));
        let v: Value = self
            .storage
            .post(format!("{}/{tenant}/oauth2/v2.0/token", authority_host.trim_end_matches('/')))
            .form(&form)
            .send()
            .await
            .map_err(|e| format!("Entra: {}", why(&e)))?
            .json()
            .await
            .map_err(|e| format!("Entra: {e}"))?;
        let token = v["access_token"].as_str().ok_or_else(|| format!("Entra gave no token: {}", v["error"]))?.to_string();
        let exp = now_s() + v["expires_in"].as_i64().unwrap_or(3600);
        self.tokens.lock().unwrap().insert(cache_key, (exp, token.clone()));
        Ok(token)
    }

    async fn user_delegation_key(&self, c: &Credential) -> Result<Udk, String> {
        let Provider::Azure { blob_url, .. } = &c.provider else { return Err("not Azure".into()) };
        if let Some((exp, k)) = self.udks.lock().unwrap().get(&c.name) {
            if exp - now_s() > VEND_SECONDS + 300 {
                return Ok(k.clone());
            }
        }
        let token = self.entra_token(c).await?;
        let (start, expiry) = (now_s() - 300, now_s() + 6 * 3600);
        let body = format!(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?><KeyInfo><Start>{}</Start><Expiry>{}</Expiry></KeyInfo>",
            iso(start),
            iso(expiry)
        );
        let r = self
            .storage
            .post(format!("{blob_url}/?restype=service&comp=userdelegationkey"))
            .bearer_auth(token)
            .header("x-ms-version", SAS_VERSION)
            .body(body)
            .send()
            .await
            .map_err(|e| format!("Azure: {}", why(&e)))?;
        let status = r.status();
        let x = r.text().await.unwrap_or_default();
        if !status.is_success() {
            return Err(format!("Get User Delegation Key: HTTP {status}: {}", xml(&x, "Message").unwrap_or_default()));
        }
        let get = |t: &str| xml(&x, t).ok_or_else(|| format!("Get User Delegation Key: no {t}"));
        let k = Udk {
            oid: get("SignedOid")?,
            tid: get("SignedTid")?,
            start: get("SignedStart")?,
            expiry: get("SignedExpiry")?,
            service: get("SignedService")?,
            version: get("SignedVersion")?,
            value: get("Value")?,
        };
        self.udks.lock().unwrap().insert(c.name.clone(), (expiry, k.clone()));
        Ok(k)
    }

    /// A SAS token for a container, a directory or one blob, signed with the account key or a user delegation key.
    async fn azure_sas(&self, c: &Credential, a: &AzureParts, resource: &str, perms: &str, seconds: i64) -> Result<(String, i64), String> {
        let Provider::Azure { account, auth, .. } = &c.provider else { return Err("not Azure".into()) };
        let (start, expiry) = (now_s() - 300, now_s() + seconds);
        let path = if resource == "c" { "" } else { a.path.as_str() };
        let tok = match auth {
            AzureAuth::AccountKey => {
                let key = base64::engine::general_purpose::STANDARD
                    .decode(self.cred_secret(c).await?)
                    .map_err(|_| "the account key is not base64".to_string())?;
                sas_token(&key, account, &a.container, path, resource, perms, &iso(start), &iso(expiry), None)
            }
            _ => {
                let udk = self.user_delegation_key(c).await?;
                let key =
                    base64::engine::general_purpose::STANDARD.decode(&udk.value).map_err(|_| "bad user delegation key".to_string())?;
                sas_token(&key, account, &a.container, path, resource, perms, &iso(start), &iso(expiry), Some(&udk))
            }
        };
        Ok((tok, expiry * 1000))
    }

    // ---- S3 ---------------------------------------------------------------------------------------------

    async fn s3_session(&self, c: &Credential, bucket: &str, prefix: &str, write: bool, seconds: i64) -> Result<s3::TempCreds, String> {
        let Provider::S3 { role_arn, region, sts_endpoint, access_key_id, external_id, .. } = &c.provider else {
            return Err("not S3".into());
        };
        let policy = s3_policy(bucket, prefix, write)?;
        let secs = seconds.to_string();
        if access_key_id.is_empty() {
            // Web identity: AssumeRoleWithWebIdentity is not signed; the pod's token is the proof.
            let token_file =
                std::env::var("AWS_WEB_IDENTITY_TOKEN_FILE").map_err(|_| "workload: AWS_WEB_IDENTITY_TOKEN_FILE is not set")?;
            let token = read_secret_file(&token_file)?;
            let x = self
                .storage
                .post(sts_endpoint)
                .form(&[
                    ("Action", "AssumeRoleWithWebIdentity"),
                    ("Version", "2011-06-15"),
                    ("RoleArn", role_arn),
                    ("RoleSessionName", "cairn-catalog"),
                    ("WebIdentityToken", &token),
                    ("Policy", &policy),
                    ("DurationSeconds", &secs),
                ])
                .send()
                .await
                .map_err(|e| format!("STS: {}", why(&e)))?
                .text()
                .await
                .map_err(|e| e.to_string())?;
            return s3::temp_creds(&x).map_err(|e| e.to_string());
        }
        let client = s3::Client::with_http(
            s3::S3Config {
                endpoint: sts_endpoint.clone(),
                region: region.clone(),
                access_key: access_key_id.clone(),
                secret_key: self.cred_secret(c).await?,
            },
            self.storage.clone(),
        );
        client.assume_role_ext(role_arn, "cairn-catalog", &policy, seconds, external_id.as_deref()).await.map_err(|e| e.to_string())
    }

    fn s3_client(&self, c: &Credential, t: &s3::TempCreds) -> Result<s3::Client, String> {
        let Provider::S3 { region, endpoint, .. } = &c.provider else { return Err("not S3".into()) };
        Ok(s3::Client::with_http(
            s3::S3Config {
                endpoint: endpoint.clone(),
                region: region.clone(),
                access_key: t.access_key.clone(),
                secret_key: t.secret_key.clone(),
            },
            self.storage.clone(),
        )
        .session_token(&t.token))
    }

    // ---- GCS --------------------------------------------------------------------------------------------

    async fn gcs_source_token(&self, c: &Credential) -> Result<String, String> {
        let Provider::Gcs { token_uri, workload, .. } = &c.provider else { return Err("not GCS".into()) };
        let cache_key = format!("gcs:{}", c.name);
        if let Some((exp, t)) = self.tokens.lock().unwrap().get(&cache_key) {
            if exp - now_s() > 300 {
                return Ok(t.clone());
            }
        }
        let v: Value = if *workload {
            self.http
                .get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token")
                .header("Metadata-Flavor", "Google")
                .send()
                .await
                .map_err(|e| format!("GCP metadata server: {e}"))?
                .json()
                .await
                .map_err(|e| e.to_string())?
        } else {
            let key: Value =
                serde_json::from_str(&self.cred_secret(c).await?).map_err(|_| "not a service account key (JSON)".to_string())?;
            let assertion = gcs_assertion(&key, token_uri)?;
            self.storage
                .post(key["token_uri"].as_str().unwrap_or(token_uri))
                .form(&[("grant_type", "urn:ietf:params:oauth:grant-type:jwt-bearer"), ("assertion", &assertion)])
                .send()
                .await
                .map_err(|e| format!("Google OAuth: {}", why(&e)))?
                .json()
                .await
                .map_err(|e| e.to_string())?
        };
        let t = v["access_token"].as_str().ok_or("Google gave no token")?.to_string();
        self.tokens.lock().unwrap().insert(cache_key, (now_s() + v["expires_in"].as_i64().unwrap_or(3600), t.clone()));
        Ok(t)
    }

    async fn gcs_downscoped(&self, c: &Credential, bucket: &str, prefix: &str, write: bool) -> Result<(String, i64), String> {
        let Provider::Gcs { sts_url, .. } = &c.provider else { return Err("not GCS".into()) };
        let source = self.gcs_source_token(c).await?;
        let v: Value = self
            .storage
            .post(sts_url)
            .form(&[
                ("grant_type", "urn:ietf:params:oauth:grant-type:token-exchange"),
                ("subject_token_type", "urn:ietf:params:oauth:token-type:access_token"),
                ("requested_token_type", "urn:ietf:params:oauth:token-type:access_token"),
                ("subject_token", &source),
                ("options", &gcs_boundary(bucket, prefix, write)),
            ])
            .send()
            .await
            .map_err(|e| format!("Google STS: {}", why(&e)))?
            .json()
            .await
            .map_err(|e| e.to_string())?;
        let t = v["access_token"].as_str().ok_or_else(|| format!("Google STS gave no token: {}", v["error_description"]))?;
        Ok((t.to_string(), (now_s() + v["expires_in"].as_i64().unwrap_or(3600)) * 1000))
    }

    // ---- vending ----------------------------------------------------------------------------------------

    /// Access for engines to everything under `url` (a table's location, or a path), read-only or read-write.
    pub async fn vend(&self, c: &Credential, url: &str, write: bool) -> Result<Vended, String> {
        let u = parse(url)?;
        let mut config = BTreeMap::new();
        let expires_ms = match &c.provider {
            Provider::Azure { account, hns, .. } => {
                let a = parse_azure(url)?;
                let perms = if write { "racwdl" } else { "rl" };
                let resource = if *hns && !a.path.is_empty() { "d" } else { "c" };
                let (sas, exp) = self.azure_sas(c, &a, resource, perms, VEND_SECONDS).await?;
                for svc in ["dfs", "blob"] {
                    let host = format!("{account}.{svc}.{}", a.endpoint);
                    config.insert(format!("adls.sas-token.{host}"), sas.clone());
                    config.insert(format!("adls.sas-token-expires-at-ms.{host}"), exp.to_string());
                }
                // pyiceberg (one account per file system).
                config.insert("adls.sas-token".into(), sas);
                config.insert("adls.account-name".into(), account.clone());
                exp
            }
            Provider::S3 { endpoint, region, .. } => {
                let t = self.s3_session(c, &u.authority, &u.path, write, VEND_SECONDS).await?;
                config.insert("s3.endpoint".into(), endpoint.clone());
                config.insert("s3.region".into(), region.clone());
                config.insert("client.region".into(), region.clone());
                config.insert("s3.path-style-access".into(), "true".into());
                config.insert("s3.access-key-id".into(), t.access_key);
                config.insert("s3.secret-access-key".into(), t.secret_key);
                config.insert("s3.session-token".into(), t.token);
                config.insert("s3.session-token-expires-at-ms".into(), (t.expires_unix * 1000).to_string());
                t.expires_unix * 1000
            }
            Provider::Gcs { project_id, .. } => {
                let (t, exp) = self.gcs_downscoped(c, &u.authority, &u.path, write).await?;
                config.insert("gcs.oauth2.token".into(), t);
                config.insert("gcs.oauth2.token-expires-at".into(), exp.to_string());
                if !project_id.is_empty() {
                    config.insert("gcs.project-id".into(), project_id.clone());
                }
                exp
            }
        };
        Ok(Vended { config, expires_ms })
    }

    // ---- the catalog's own metadata files ----------------------------------------------------------------

    /// Writes a file that must not exist yet.
    pub async fn put_new(&self, c: &Credential, url: &str, body: Vec<u8>) -> Result<(), s3::Error> {
        let http = |e: String| s3::Error::Http(e);
        match &c.provider {
            Provider::Azure { blob_url, .. } => {
                let a = parse_azure(url).map_err(s3::Error::BadLocation)?;
                let (sas, _) = self.azure_sas(c, &a, "b", "cw", OWN_SECONDS).await.map_err(http)?;
                let r = self
                    .storage
                    .put(format!("{blob_url}/{}/{}?{sas}", a.container, enc_path(&a.path)))
                    .header("x-ms-version", SAS_VERSION)
                    .header("x-ms-blob-type", "BlockBlob")
                    .header("if-none-match", "*")
                    .header("content-type", "application/json")
                    .body(body)
                    .send()
                    .await
                    .map_err(|e| http(why(&e)))?;
                match r.status().as_u16() {
                    200 | 201 => Ok(()),
                    409 | 412 => Err(s3::Error::Exists),
                    s => Err(http(format!("PUT {url}: HTTP {s}"))),
                }
            }
            Provider::S3 { .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let t = self.s3_session(c, &u.authority, parent(&u.path), true, 900).await.map_err(http)?;
                self.s3_client(c, &t).map_err(http)?.put_new(url, body, "application/json").await
            }
            Provider::Gcs { storage_url, .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let (t, _) = self.gcs_downscoped(c, &u.authority, parent(&u.path), true).await.map_err(http)?;
                let r = self
                    .storage
                    .post(format!(
                        "{storage_url}/upload/storage/v1/b/{}/o?uploadType=media&ifGenerationMatch=0&name={}",
                        u.authority,
                        enc(&u.path)
                    ))
                    .bearer_auth(t)
                    .header("content-type", "application/json")
                    .body(body)
                    .send()
                    .await
                    .map_err(|e| http(why(&e)))?;
                match r.status().as_u16() {
                    200 => Ok(()),
                    412 => Err(s3::Error::Exists),
                    s => Err(http(format!("PUT {url}: HTTP {s}"))),
                }
            }
        }
    }

    pub async fn get(&self, c: &Credential, url: &str) -> Result<Vec<u8>, s3::Error> {
        let http = |e: String| s3::Error::Http(e);
        let r = match &c.provider {
            Provider::Azure { blob_url, .. } => {
                let a = parse_azure(url).map_err(s3::Error::BadLocation)?;
                let (sas, _) = self.azure_sas(c, &a, "b", "r", OWN_SECONDS).await.map_err(http)?;
                self.storage.get(format!("{blob_url}/{}/{}?{sas}", a.container, enc_path(&a.path))).header("x-ms-version", SAS_VERSION)
            }
            Provider::S3 { .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let t = self.s3_session(c, &u.authority, parent(&u.path), false, 900).await.map_err(http)?;
                return self.s3_client(c, &t).map_err(http)?.get(url).await;
            }
            Provider::Gcs { storage_url, .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let (t, _) = self.gcs_downscoped(c, &u.authority, parent(&u.path), false).await.map_err(http)?;
                self.storage.get(format!("{storage_url}/storage/v1/b/{}/o/{}?alt=media", u.authority, enc(&u.path))).bearer_auth(t)
            }
        };
        let r = r.send().await.map_err(|e| http(why(&e)))?;
        match r.status().as_u16() {
            200 => Ok(r.bytes().await.map_err(|e| http(e.to_string()))?.to_vec()),
            404 => Err(s3::Error::NotFound),
            s => Err(http(format!("GET {url}: HTTP {s}"))),
        }
    }

    pub async fn delete(&self, c: &Credential, url: &str) -> Result<(), s3::Error> {
        let http = |e: String| s3::Error::Http(e);
        let r = match &c.provider {
            Provider::Azure { blob_url, .. } => {
                let a = parse_azure(url).map_err(s3::Error::BadLocation)?;
                let (sas, _) = self.azure_sas(c, &a, "b", "d", OWN_SECONDS).await.map_err(http)?;
                self.storage.delete(format!("{blob_url}/{}/{}?{sas}", a.container, enc_path(&a.path))).header("x-ms-version", SAS_VERSION)
            }
            Provider::S3 { .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let t = self.s3_session(c, &u.authority, parent(&u.path), true, 900).await.map_err(http)?;
                return self.s3_client(c, &t).map_err(http)?.delete(url).await;
            }
            Provider::Gcs { storage_url, .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let (t, _) = self.gcs_downscoped(c, &u.authority, parent(&u.path), true).await.map_err(http)?;
                self.storage.delete(format!("{storage_url}/storage/v1/b/{}/o/{}", u.authority, enc(&u.path))).bearer_auth(t)
            }
        };
        let r = r.send().await.map_err(|e| http(why(&e)))?;
        match r.status().as_u16() {
            200 | 202 | 204 | 404 => Ok(()),
            s => Err(http(format!("DELETE {url}: HTTP {s}"))),
        }
    }
}

/// A service account's signed JWT for Google's OAuth token endpoint (RS256 with its private key).
fn gcs_assertion(key: &Value, token_uri: &str) -> Result<String, String> {
    let email = key["client_email"].as_str().ok_or("the service account key has no client_email")?;
    let pem = key["private_key"].as_str().ok_or("the service account key has no private_key")?;
    let der: String = pem.lines().filter(|l| !l.starts_with("-----")).collect();
    let der = base64::engine::general_purpose::STANDARD.decode(der).map_err(|_| "the private key is not PEM".to_string())?;
    let kp = ring::signature::RsaKeyPair::from_pkcs8(&der).map_err(|_| "the private key is not a PKCS#8 RSA key".to_string())?;
    let b64 = |v: &[u8]| base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(v);
    let now = now_s();
    let aud = key["token_uri"].as_str().unwrap_or(token_uri);
    let header = b64(json!({"alg": "RS256", "typ": "JWT"}).to_string().as_bytes());
    let claims = b64(json!({"iss": email, "scope": "https://www.googleapis.com/auth/devstorage.full_control", "aud": aud,
                            "iat": now, "exp": now + 3600})
    .to_string()
    .as_bytes());
    let msg = format!("{header}.{claims}");
    let mut sig = vec![0u8; kp.public().modulus_len()];
    kp.sign(&ring::signature::RSA_PKCS1_SHA256, &ring::rand::SystemRandom::new(), msg.as_bytes(), &mut sig)
        .map_err(|_| "signing failed".to_string())?;
    Ok(format!("{msg}.{}", b64(&sig)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn doc(v: Value) -> Doc {
        serde_json::from_value(v).unwrap()
    }

    fn azure_cred(hns: bool) -> Value {
        json!({"name": "az", "provider": "azure", "secret_ref": "file:/k",
               "config": {"account": "lake1", "hierarchical_namespace": hns}})
    }

    #[test]
    fn urls_are_parsed_and_normalized() {
        let a = parse("abfss://data@lake1.dfs.core.windows.net/sales//orders/").unwrap();
        assert_eq!((a.kind, a.authority.as_str(), a.path.as_str()), (Kind::Azure, "lake1.core.windows.net/data", "sales/orders"));
        // wasbs and abfss name the same place.
        assert!(parse("wasbs://data@lake1.blob.core.windows.net/sales").unwrap().covers(&a));
        assert_eq!(parse("s3a://bkt/p/q").unwrap(), StorageUrl { kind: Kind::S3, authority: "bkt".into(), path: "p/q".into() });
        assert_eq!(parse("gs://bkt").unwrap().path, "");
        for bad in [
            "abfss://lake1.dfs.core.windows.net/x",
            "abfss://Data@lake1.dfs.core.windows.net/x",
            "abfss://data@lake1.blob.core.windows.net/x",
            "abfss://data@lake1.dfs.core.windows.net:443/x",
            "s3://bkt/a/../b",
            "file:///tmp/x",
            "hdfs://x/y",
            "s3://",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
        let p = parse("s3://b/eu").unwrap();
        assert!(p.covers(&parse("s3://b/eu/x").unwrap()) && p.covers(&parse("s3://b/eu").unwrap()));
        assert!(!p.covers(&parse("s3://b/europe").unwrap()) && !p.covers(&parse("s3://c/eu/x").unwrap()));
        assert!(parse("s3://b").unwrap().covers(&p));
    }

    #[test]
    fn locations_must_hold_together() {
        let reserved = vec![parse("s3://lakehouse/warehouse").unwrap()];
        let ok = doc(json!({"credentials": [azure_cred(true),
            {"name": "aws", "provider": "s3", "secret_ref": "k8s:aws/secret", "config": {"role_arn": "arn:aws:iam::1:role/r", "access_key_id": "AKIA"}}],
            "locations": [{"name": "a", "url": "abfss://data@lake1.dfs.core.windows.net/a", "credential": "az"},
                          {"name": "b", "url": "abfss://data@lake1.dfs.core.windows.net/b", "credential": "az", "read_only": true},
                          {"name": "c", "url": "s3://customer/raw", "credential": "aws"}]}));
        let snap = Snapshot::build(&ok, &reserved).unwrap();
        let (l, c) = snap.find(&parse("abfss://data@lake1.dfs.core.windows.net/b/t-1").unwrap()).unwrap();
        assert_eq!((l.name.as_str(), l.read_only, c.name.as_str()), ("b", true, "az"));
        assert!(snap.find(&parse("abfss://data@lake1.dfs.core.windows.net/c/t").unwrap()).is_none());
        assert!(snap.find(&parse("s3://lakehouse/warehouse/t").unwrap()).is_none());
        let bad =
            |locs: Value, creds: Value| Snapshot::build(&doc(json!({"credentials": creds, "locations": locs})), &reserved).unwrap_err();
        let az = json!([azure_cred(true)]);
        assert!(bad(
            json!([{"name": "a", "url": "abfss://data@lake1.dfs.core.windows.net/a", "credential": "az"},
                           {"name": "b", "url": "abfss://data@lake1.dfs.core.windows.net/a/b", "credential": "az"}]),
            az.clone()
        )
        .contains("overlaps location a"));
        assert!(bad(json!([{"name": "a", "url": "abfss://data@other.dfs.core.windows.net/a", "credential": "az"}]), az.clone())
            .contains("storage account lake1"));
        assert!(bad(json!([{"name": "a", "url": "s3://x/a", "credential": "az"}]), az.clone()).contains("another kind"));
        assert!(bad(json!([{"name": "a", "url": "abfss://data@lake1.dfs.core.windows.net/a", "credential": "nope"}]), az)
            .contains("no credential"));
        assert!(bad(
            json!([{"name": "a", "url": "abfss://data@lake1.dfs.core.windows.net/a", "credential": "az"}]),
            json!([azure_cred(false)])
        )
        .contains("whole container"));
        let aws = json!([{"name": "aws", "provider": "s3", "secret_ref": "workload", "config": {"role_arn": "arn:aws:iam::1:role/r"}}]);
        assert!(bad(json!([{"name": "w", "url": "s3://lakehouse/warehouse/x", "credential": "aws"}]), aws).contains("Cairn's own storage"));
        // Secrets only by reference.
        for r in ["hunter2", "file:relative", "k8s:a", "vault:kv#x", "env:X"] {
            assert!(check_ref(r).is_err(), "{r}");
        }
        for r in ["file:/run/k", "k8s:azure/key", "vault:kv/azure#key", "workload"] {
            assert!(check_ref(r).is_ok(), "{r}");
        }
        let plain = json!([{"name": "x", "provider": "azure", "secret_ref": "AccountKey==", "config": {"account": "a"}}]);
        assert!(bad(json!([]), plain).contains("secret_ref"));
        // From Cairn, entry by entry: the bad entry and both overlapping locations are left out, the rest stay.
        let mixed = doc(json!({"credentials": [azure_cred(true), {"name": "bad", "provider": "azure", "config": {}}],
            "locations": [{"name": "a", "url": "abfss://data@lake1.dfs.core.windows.net/a", "credential": "az"},
                          {"name": "a2", "url": "abfss://data@lake1.dfs.core.windows.net/a/b", "credential": "az"},
                          {"name": "ok", "url": "abfss://data@lake1.dfs.core.windows.net/ok", "credential": "az"},
                          {"name": "orphan", "url": "abfss://data@lake1.dfs.core.windows.net/o", "credential": "bad"}]}));
        let snap = Snapshot::lenient(&mixed, &reserved, &Limits::new(Some("/"), "").unwrap());
        assert_eq!(snap.locations.iter().map(|l| l.name.as_str()).collect::<Vec<_>>(), vec!["ok"]);
    }

    #[test]
    fn a_directory_sas_names_its_depth_and_signs_the_directory() {
        let key = base64::engine::general_purpose::STANDARD.decode("Zm9vYmFyMTIzNDU2Nzg5MA==").unwrap();
        let q = sas_token(&key, "lake1", "data", "sales/orders-1", "d", "rl", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", None);
        assert!(q.contains("sr=d") && q.contains("sdd=2") && q.contains("sp=rl") && q.contains("sv=2021-08-06"), "{q}");
        // The signature covers the directory: another directory gives another signature.
        let other = sas_token(&key, "lake1", "data", "sales/orders-2", "d", "rl", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", None);
        assert_ne!(q.split("sig=").nth(1), other.split("sig=").nth(1));
        let c = sas_token(&key, "lake1", "data", "", "c", "racwdl", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", None);
        assert!(c.contains("sr=c") && !c.contains("sdd"));
        let b = sas_token(
            &key,
            "lake1",
            "data",
            "sales/orders-1/metadata/x.json",
            "b",
            "cw",
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z",
            None,
        );
        // The same signatures as Azure's own SDK (azure-storage-file-datalake 12.21.0, sv 2021-08-06) for these inputs.
        let sig = |q: &str| percent_encoding::percent_decode_str(q.split("sig=").nth(1).unwrap()).decode_utf8().unwrap().to_string();
        assert_eq!(sig(&q), "mFddblcuCOgoNsF99uy/UNn6npQZYT7wSBnINi+o3iY=");
        assert_eq!(sig(&c), "GPqdx3Wen8AG/LbRAX9NtcPJYfR6nYuZwD+v/vXw8n8=");
        assert_eq!(sig(&b), "Xf1GDpFMWG7VGkYsQIC7E3MiMw4fdOlLueisr0Bf9K4=");
    }

    #[test]
    fn session_policies_and_boundaries_name_the_prefix() {
        let p: Value = serde_json::from_str(&s3_policy("customer", "raw/t-1", false).unwrap()).unwrap();
        assert_eq!(p["Statement"][0]["Resource"], "arn:aws:s3:::customer/raw/t-1/*");
        assert_eq!(p["Statement"][0]["Action"], json!(["s3:GetObject"]));
        assert_eq!(p["Statement"][1]["Condition"]["StringLike"]["s3:prefix"], json!(["raw/t-1/*"]));
        let b: Value = serde_json::from_str(&gcs_boundary("bkt", "raw/t", true)).unwrap();
        let rule = &b["accessBoundary"]["accessBoundaryRules"][0];
        assert_eq!(rule["availablePermissions"], json!(["inRole:roles/storage.objectAdmin"]));
        assert!(rule["availabilityCondition"]["expression"].as_str().unwrap().contains("objects/raw/t/"));
    }

    #[test]
    fn a_table_path_cannot_widen_its_policy_or_boundary() {
        // IAM reads * and ? as wildcards and ${...} as variables: raw/t* would reach raw/tother, so it is refused.
        for p in ["raw/t*", "raw/t?", "raw/${aws:username}", "raw/*/x"] {
            assert!(s3_policy("customer", p, true).is_err(), "{p}");
        }
        // A quote in a GCS path stays inside the CEL string literal: it cannot add `|| true` to the condition.
        let inject = "raw/x') || true || ('";
        let b: Value = serde_json::from_str(&gcs_boundary("bkt", inject, true)).unwrap();
        let e = b["accessBoundary"]["accessBoundaryRules"][0]["availabilityCondition"]["expression"].as_str().unwrap().to_string();
        assert!(!e.contains("x') || true"), "{e}");
        assert!(e.contains(r"objects/raw/x\') || true || (\'/'"), "{e}");
        // The only unescaped quotes left are the four literals' own.
        let unescaped = e.replace(r"\\", "").replace(r"\'", "").matches('\'').count();
        assert_eq!(unescaped, 8, "{e}");
        let b: Value = serde_json::from_str(&gcs_boundary("bkt", r"raw/a\", false)).unwrap();
        let e = b["accessBoundary"]["accessBoundaryRules"][0]["availabilityCondition"]["expression"].as_str().unwrap().to_string();
        assert!(e.contains(r"objects/raw/a\\/'"), "{e}");
    }

    fn limits(dir: Option<&str>, trusted: &str) -> Limits {
        Limits::new(dir, trusted).unwrap()
    }

    fn with(mut cred: Value, cfg: Value) -> Value {
        for (k, v) in cfg.as_object().unwrap() {
            cred["config"][k] = v.clone();
        }
        cred
    }

    /// One credential (named c, and a location for it) as Cairn hands it over: is it kept?
    fn kept(cred: Value, l: &Limits) -> bool {
        let url = match cred["provider"].as_str() {
            Some("azure") => format!("abfss://data@{}.dfs.core.windows.net/a", cred["config"]["account"].as_str().unwrap()),
            Some("s3") => "s3://customer/a".into(),
            _ => "gs://customer/a".into(),
        };
        let d = doc(json!({"credentials": [cred], "locations": [{"name": "l", "url": url, "credential": "c"}]}));
        Snapshot::lenient(&d, &[parse("s3://lakehouse/warehouse").unwrap()], l).locations.len() == 1
    }

    #[test]
    fn registrations_from_cairn_keep_to_the_operators_limits() {
        let sp = |r: &str, cfg: Value| {
            with(
                json!({"name": "c", "provider": "azure", "secret_ref": r,
                       "config": {"account": "lake1", "auth": "service_principal", "tenant_id": "t", "client_id": "i"}}),
                cfg,
            )
        };
        let none = limits(None, "");
        let dir = limits(Some("/var/run/cairn/files"), "");
        // file: references: only under the operator's directory, and none without one.
        assert!(!kept(sp("file:/var/run/cairn/files/sp", json!({})), &none));
        assert!(kept(sp("file:/var/run/cairn/files/sp", json!({})), &dir));
        for r in ["file:/proc/self/environ", "file:/var/run/cairn/files/../../../proc/self/environ", "file:/var/run/cairn/filesx/k"] {
            assert!(!kept(sp(r, json!({})), &dir), "{r}");
        }
        assert!(kept(sp("k8s:lake/secret", json!({})), &none));
        // Endpoints: nothing internal unless trusted (names are checked again when they resolve).
        for h in [
            "http://127.0.0.1:9000",
            "http://169.254.169.254",
            "http://[::1]:80",
            "http://[fd00::1]",
            "http://10.0.0.7",
            "http://100.64.1.1",
            "http://[::ffff:127.0.0.1]",
            "http://localhost:8080",
            "http://0.0.0.0",
            "file:///etc/passwd",
            "https://user:pw@login.example.com",
        ] {
            assert!(!kept(sp("k8s:lake/secret", json!({"authority_host": h})), &none), "{h}");
            assert!(!kept(sp("k8s:lake/secret", json!({"endpoint": h})), &none), "{h}");
        }
        assert!(kept(sp("k8s:lake/secret", json!({"authority_host": "https://login.microsoftonline.us"})), &none));
        let trusted = limits(None, "127.0.0.1, 10.0.0.0/8, minio.storage.svc");
        assert!(kept(sp("k8s:lake/secret", json!({"endpoint": "http://127.0.0.1:9000"})), &trusted));
        assert!(kept(sp("k8s:lake/secret", json!({"endpoint": "http://10.1.2.3"})), &trusted));
        assert!(!kept(sp("k8s:lake/secret", json!({"endpoint": "http://172.16.0.1"})), &trusted));
        // The catalog's workload identity goes only to the provider's own endpoints, or to trusted hosts.
        let wl = |cfg: Value| {
            with(json!({"name": "c", "provider": "s3", "secret_ref": "workload", "config": {"role_arn": "arn:aws:iam::1:role/r"}}), cfg)
        };
        assert!(kept(wl(json!({})), &none));
        assert!(!kept(wl(json!({"sts_endpoint": "https://sts.attacker.example"})), &none));
        assert!(kept(wl(json!({"sts_endpoint": "https://sts.attacker.example"})), &limits(None, "sts.attacker.example")));
        // A key's own secret may go where the administrator says (it is the organisation's), the pod's may not.
        let mut s3key = wl(json!({"access_key_id": "AKIA", "sts_endpoint": "https://sts.example.org"}));
        s3key["secret_ref"] = json!("k8s:aws/secret");
        assert!(kept(s3key, &none));
        assert!(!kept(
            json!({"name": "c", "provider": "gcs", "secret_ref": "workload", "config": {"sts_url": "https://x.example/t"}}),
            &none
        ));
        let az_wl = json!({"name": "c", "provider": "azure", "config": {"account": "lake1", "auth": "workload_identity",
                           "tenant_id": "t", "client_id": "i", "federated_token_file": "/proc/self/environ"}});
        assert!(!kept(az_wl.clone(), &dir));
        assert!(kept(with(az_wl, json!({"federated_token_file": "/var/run/cairn/files/token"})), &dir));
        // Registrations in a file are the operator's own.
        let d = doc(json!({"credentials": [sp("file:/proc/self/environ", json!({"authority_host": "http://127.0.0.1:1"}))],
                           "locations": [{"name": "l", "url": "abfss://data@lake1.dfs.core.windows.net/a", "credential": "c"}]}));
        assert_eq!(Snapshot::build(&d, &[]).unwrap().locations.len(), 1);
    }

    #[test]
    fn internal_addresses_are_known() {
        for a in [
            "127.0.0.1",
            "10.1.1.1",
            "172.31.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.100.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fd12::1",
            "::ffff:10.0.0.1",
            "64:ff9b::a9fe:a9fe",
            "255.255.255.255",
        ] {
            assert!(internal_ip(a.parse().unwrap()), "{a}");
        }
        for a in ["8.8.8.8", "52.95.110.1", "2606:4700::1111", "100.128.0.1"] {
            assert!(!internal_ip(a.parse().unwrap()), "{a}");
        }
        assert!(in_net("10.9.8.7".parse().unwrap(), "10.0.0.0".parse().unwrap(), 8));
        assert!(!in_net("11.0.0.1".parse().unwrap(), "10.0.0.0".parse().unwrap(), 8));
        assert!(Limits::new(Some("relative/dir"), "").is_err() && Limits::new(None, "10.0.0.0/x").is_err());
    }

    /// A registration from Cairn that names this pod's files or an internal host gets neither: the catalog reads no
    /// file outside the operator's directory, and connects to no internal address a name resolves to.
    #[tokio::test]
    async fn a_registration_from_cairn_reaches_no_pod_file_nor_internal_host() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        // A token endpoint on this machine, standing in for a metadata service or any internal service.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let got = std::sync::Arc::new(Mutex::new(Vec::<u8>::new()));
        let g = got.clone();
        tokio::spawn(async move {
            while let Ok((mut s, _)) = listener.accept().await {
                let mut buf = vec![0u8; 65536];
                let n = s.read(&mut buf).await.unwrap_or(0);
                g.lock().unwrap().extend_from_slice(&buf[..n]);
                let body = r#"{"error":"nope"}"#;
                let _ = s.write_all(format!("HTTP/1.1 400 Bad Request\r\ncontent-length: {}\r\n\r\n{body}", body.len()).as_bytes()).await;
            }
        });
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("sp"), "the-client-secret").unwrap();
        let outside = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(outside.path(), "the-pods-own-secret").unwrap();
        std::os::unix::fs::symlink(outside.path(), dir.path().join("link")).unwrap();
        let ext = |trusted: &str| {
            External::new(
                Source::Cairn { url: "http://127.0.0.1:1".into(), token: "t".repeat(16) },
                vec![],
                None,
                Duration::from_secs(30),
                Limits::new(Some(dir.path().to_str().unwrap()), trusted).unwrap(),
            )
            .unwrap()
        };
        let sp = |r: &str, host: &str| {
            let d = json!({"name": "c", "provider": "azure", "secret_ref": r,
                           "config": {"account": "lake1", "auth": "service_principal", "tenant_id": "t", "client_id": "i",
                                      "authority_host": host}});
            Credential::from_doc(&serde_json::from_value(d).unwrap()).unwrap()
        };
        let url = "abfss://data@lake1.dfs.core.windows.net/a";
        let local = format!("http://localhost:{port}");
        // The pod's own file, directly or through a link in the directory: not read, nothing sent.
        for r in [format!("file:{}", outside.path().display()), format!("file:{}/link", dir.path().display())] {
            let e = ext("localhost").vend(&sp(&r, &local), url, false).await.unwrap_err();
            assert!(e.contains("CAIRN_CATALOG_SECRET_FILES_DIR"), "{e}");
        }
        assert!(got.lock().unwrap().is_empty(), "a file outside the directory was sent");
        // An internal host by name: the catalog does not connect.
        let r = format!("file:{}/sp", dir.path().display());
        let e = ext("").vend(&sp(&r, &local), url, false).await.unwrap_err();
        assert!(e.contains("internal network"), "{e}");
        assert!(got.lock().unwrap().is_empty(), "the internal host was called");
        // Trusted by the operator, it is called (with the secret from the directory).
        let _ = ext("localhost").vend(&sp(&r, &local), url, false).await;
        assert!(String::from_utf8_lossy(&got.lock().unwrap()).contains("the-client-secret"));
    }
}
