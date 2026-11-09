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
//! Entra federated token, AWS web identity, GCP metadata server). Never from the configuration itself.

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
        Snapshot::build_with(doc, reserved, true)
    }

    /// Entry by entry (registrations loaded from Cairn, which checks the same rules): an entry that does not hold
    /// together is left out and logged, and locations that overlap are all left out, so none of them is used for
    /// another's place.
    pub fn lenient(doc: &Doc, reserved: &[StorageUrl]) -> Snapshot {
        Snapshot::build_with(doc, reserved, false).unwrap_or_default()
    }

    fn build_with(doc: &Doc, reserved: &[StorageUrl], strict: bool) -> Result<Snapshot, String> {
        let skip = |e: String| -> Result<(), String> {
            if strict {
                return Err(e);
            }
            tracing::error!(error = %e, "an external location registration was left out");
            Ok(())
        };
        let mut credentials = HashMap::new();
        for d in &doc.credentials {
            match Credential::from_doc(d) {
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

/// The S3 session policy for a prefix: its objects, and listing under it (read-only or read-write).
pub fn s3_policy(bucket: &str, prefix: &str, write: bool) -> String {
    let objects = if write {
        json!(["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"])
    } else {
        json!(["s3:GetObject"])
    };
    let (res, list) = if prefix.is_empty() {
        (format!("arn:aws:s3:::{bucket}/*"), json!(["*"]))
    } else {
        (format!("arn:aws:s3:::{bucket}/{prefix}/*"), json!([format!("{prefix}/*"), prefix]))
    };
    json!({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": objects, "Resource": res},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": format!("arn:aws:s3:::{bucket}"),
         "Condition": {"StringLike": {"s3:prefix": list}}}]})
    .to_string()
}

/// The GCS credential access boundary for a prefix (read: objectViewer; write: objectAdmin).
pub fn gcs_boundary(bucket: &str, prefix: &str, write: bool) -> String {
    let role = if write { "inRole:roles/storage.objectAdmin" } else { "inRole:roles/storage.objectViewer" };
    let mut rule =
        json!({"availablePermissions": [role], "availableResource": format!("//storage.googleapis.com/projects/_/buckets/{bucket}")});
    if !prefix.is_empty() {
        rule["availabilityCondition"] = json!({"expression": format!(
            "resource.name.startsWith('projects/_/buckets/{bucket}/objects/{prefix}/') || \
             api.getAttribute('storage.googleapis.com/objectListPrefix', '').startsWith('{prefix}/')")});
    }
    json!({"accessBoundary": {"accessBoundaryRules": [rule]}}).to_string()
}

impl External {
    pub fn new(source: Source, reserved: Vec<StorageUrl>, ca_file: Option<String>, refresh: Duration) -> Result<External, String> {
        let mut b = reqwest::Client::builder().timeout(Duration::from_secs(30));
        if let Some(f) = ca_file {
            let pem = std::fs::read(&f).map_err(|e| format!("{f}: {e}"))?;
            for c in reqwest::Certificate::from_pem_bundle(&pem).map_err(|e| format!("{f}: {e}"))? {
                b = b.add_root_certificate(c);
            }
        }
        let ext = External {
            source,
            snapshot: RwLock::new((None, Default::default())),
            reserved,
            http: b.build().map_err(|e| e.to_string())?,
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

    pub fn active(&self) -> bool {
        !matches!(self.source, Source::Off)
    }

    fn install(&self, text: &str) -> Result<usize, String> {
        let doc: Doc = serde_json::from_str(text).map_err(|e| format!("not a locations document: {e}"))?;
        let snap = match self.source {
            Source::Cairn { .. } => Snapshot::lenient(&doc, &self.reserved),
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
            Some(("file", p)) => read_secret_file(p),
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
                    ("client_assertion", read_secret_file(token_file)?),
                ],
            ),
            AzureAuth::AccountKey => return Err("an account key needs no token".into()),
        };
        form.push(("grant_type", "client_credentials".into()));
        form.push(("scope", "https://storage.azure.com/.default".into()));
        let v: Value = self
            .http
            .post(format!("{}/{tenant}/oauth2/v2.0/token", authority_host.trim_end_matches('/')))
            .form(&form)
            .send()
            .await
            .map_err(|e| format!("Entra: {e}"))?
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
            .http
            .post(format!("{blob_url}/?restype=service&comp=userdelegationkey"))
            .bearer_auth(token)
            .header("x-ms-version", SAS_VERSION)
            .body(body)
            .send()
            .await
            .map_err(|e| format!("Azure: {e}"))?;
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
        let policy = s3_policy(bucket, prefix, write);
        let secs = seconds.to_string();
        if access_key_id.is_empty() {
            // Web identity: AssumeRoleWithWebIdentity is not signed; the pod's token is the proof.
            let token_file =
                std::env::var("AWS_WEB_IDENTITY_TOKEN_FILE").map_err(|_| "workload: AWS_WEB_IDENTITY_TOKEN_FILE is not set")?;
            let token = read_secret_file(&token_file)?;
            let x = self
                .http
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
                .map_err(|e| format!("STS: {e}"))?
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
            self.http.clone(),
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
            self.http.clone(),
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
            self.http
                .post(key["token_uri"].as_str().unwrap_or(token_uri))
                .form(&[("grant_type", "urn:ietf:params:oauth:grant-type:jwt-bearer"), ("assertion", &assertion)])
                .send()
                .await
                .map_err(|e| format!("Google OAuth: {e}"))?
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
            .http
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
            .map_err(|e| format!("Google STS: {e}"))?
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
                    .http
                    .put(format!("{blob_url}/{}/{}?{sas}", a.container, enc_path(&a.path)))
                    .header("x-ms-version", SAS_VERSION)
                    .header("x-ms-blob-type", "BlockBlob")
                    .header("if-none-match", "*")
                    .header("content-type", "application/json")
                    .body(body)
                    .send()
                    .await
                    .map_err(|e| http(e.to_string()))?;
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
                    .http
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
                    .map_err(|e| http(e.to_string()))?;
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
                self.http.get(format!("{blob_url}/{}/{}?{sas}", a.container, enc_path(&a.path))).header("x-ms-version", SAS_VERSION)
            }
            Provider::S3 { .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let t = self.s3_session(c, &u.authority, parent(&u.path), false, 900).await.map_err(http)?;
                return self.s3_client(c, &t).map_err(http)?.get(url).await;
            }
            Provider::Gcs { storage_url, .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let (t, _) = self.gcs_downscoped(c, &u.authority, parent(&u.path), false).await.map_err(http)?;
                self.http.get(format!("{storage_url}/storage/v1/b/{}/o/{}?alt=media", u.authority, enc(&u.path))).bearer_auth(t)
            }
        };
        let r = r.send().await.map_err(|e| http(e.to_string()))?;
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
                self.http.delete(format!("{blob_url}/{}/{}?{sas}", a.container, enc_path(&a.path))).header("x-ms-version", SAS_VERSION)
            }
            Provider::S3 { .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let t = self.s3_session(c, &u.authority, parent(&u.path), true, 900).await.map_err(http)?;
                return self.s3_client(c, &t).map_err(http)?.delete(url).await;
            }
            Provider::Gcs { storage_url, .. } => {
                let u = parse(url).map_err(s3::Error::BadLocation)?;
                let (t, _) = self.gcs_downscoped(c, &u.authority, parent(&u.path), true).await.map_err(http)?;
                self.http.delete(format!("{storage_url}/storage/v1/b/{}/o/{}", u.authority, enc(&u.path))).bearer_auth(t)
            }
        };
        let r = r.send().await.map_err(|e| http(e.to_string()))?;
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
        let snap = Snapshot::lenient(&mixed, &reserved);
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
        let p: Value = serde_json::from_str(&s3_policy("customer", "raw/t-1", false)).unwrap();
        assert_eq!(p["Statement"][0]["Resource"], "arn:aws:s3:::customer/raw/t-1/*");
        assert_eq!(p["Statement"][0]["Action"], json!(["s3:GetObject"]));
        let b: Value = serde_json::from_str(&gcs_boundary("bkt", "raw/t", true)).unwrap();
        let rule = &b["accessBoundary"]["accessBoundaryRules"][0];
        assert_eq!(rule["availablePermissions"], json!(["inRole:roles/storage.objectAdmin"]));
        assert!(rule["availabilityCondition"]["expression"].as_str().unwrap().contains("objects/raw/t/"));
    }
}
