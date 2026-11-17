//! The Iceberg REST catalog API (open-api/rest-catalog-open-api.yaml).
//!
//! Table metadata is validated and evolved by iceberg-rust (the Apache Iceberg project's Rust
//! library): requirements are checked with `TableRequirement::check`, updates applied with
//! `TableUpdate::apply` on a `TableMetadataBuilder`, so the catalog follows the table spec exactly as
//! the reference implementations do. A commit writes the new metadata file to object storage
//! (create-only), then swaps the table's pointer with a compare-and-set; a writer whose base is stale
//! gets 409 and retries.

use std::collections::{BTreeMap, HashMap};
use std::convert::Infallible;
use std::str::FromStr;
use std::sync::Arc;

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Method, Request, Response, StatusCode};
use iceberg::spec::{SortOrder, TableMetadata, TableMetadataBuilder, UnboundPartitionSpec};
use iceberg::{MetadataLocation, TableCreation, TableRequirement, TableUpdate};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use subtle::ConstantTimeEq;

use crate::db::{self, Change, Db, TableRow};
use crate::s3;

pub type Resp = Response<http_body_util::combinators::BoxBody<Bytes, Infallible>>;

/// A bearer token the catalog accepts.
#[derive(Clone, Debug, Deserialize)]
pub struct Principal {
    pub token: String,
    pub name: String,
    /// Glob patterns over dotted namespace names ("sales", "sales.*"); empty = every namespace.
    #[serde(default)]
    pub namespaces: Vec<String>,
    #[serde(default)]
    pub read_only: bool,
    /// A Cairn user signed in with a Cairn-issued token: every table is decided by Cairn for them.
    #[serde(skip)]
    pub user: Option<String>,
    /// An engine that acts for the people who sign in to it and applies Cairn's policies itself (Trino with
    /// Cairn's access control): it may exchange its token for one per person (`POST /v1/oauth/tokens`, RFC 8693).
    #[serde(default)]
    pub delegate: bool,
    /// Signed in through such an engine: the person's tables are decided by Cairn, and the engine applies masks
    /// and row filters, so reading a restricted table is not refused here.
    #[serde(skip)]
    pub delegated: bool,
}

impl Principal {
    fn allows(&self, levels: &[String]) -> bool {
        let name = levels.join(".");
        self.namespaces.is_empty() || self.namespaces.iter().any(|p| glob_match::glob_match(p, &name))
    }
}

pub struct Config {
    /// Base location for new tables, e.g. s3://lakehouse/warehouse
    pub warehouse: String,
    pub principals: Vec<Principal>,
    pub s3: s3::S3Config,
    /// What storage access clients get with each table (engines then need no storage keys of their own).
    pub vend: Vend,
    /// The object store endpoint as clients reach it (defaults to the catalog's own).
    pub client_endpoint: String,
    /// People and pipeline users (Cairn-issued tokens), decided by Cairn table by table.
    pub identity: Option<std::sync::Arc<crate::identity::Identity>>,
    /// Namespaces pinned to a region: where their tables may live (data residency).
    pub residency: crate::residency::Residency,
    /// Tables on the customer's own storage: registered storage credentials and external locations.
    pub external: Arc<crate::external::External>,
}

/// Where a location lives: in Cairn Store, or in a registered external location (with its credential).
#[allow(clippy::large_enum_variant)] // one per request, never stored
enum Place {
    Store,
    External(crate::external::Location, crate::external::Credential),
}

/// What is being placed at a location: a namespace's tables go under it, a table's files and a view's metadata in it.
#[derive(Clone, Copy)]
enum Placed<'a> {
    Namespace,
    Table(&'a str),
    View(&'a str),
}

/// A table's or view's location, read from its metadata without the rest of it.
#[derive(Deserialize)]
struct Located {
    location: Option<String>,
}

/// Tokens the catalog gives an engine for one person (`cdt.<claims>.<mac>`), checked with the engine's own token.
const DELEGATED: &str = "cdt.";
fn delegated_seconds() -> i64 {
    std::env::var("CAIRN_CATALOG_DELEGATED_TOKEN_SECONDS")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|s| (60..=86400).contains(s))
        .unwrap_or(3600)
}

fn mac_hex(key: &[u8], msg: &str) -> String {
    use hmac::Mac;
    let mut m = <hmac::Hmac<sha2::Sha256>>::new_from_slice(key).expect("hmac accepts any key");
    m.update(b"cairn-catalog-delegation\n");
    m.update(msg.as_bytes());
    hex::encode(m.finalize().into_bytes())
}

/// The user an unsigned JWT names (what Trino sends for the person a query runs as).
fn jwt_subject(token: &str) -> Option<String> {
    use base64::Engine as _;
    let claims = token.split('.').nth(1)?;
    let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(claims.trim_end_matches('=')).ok()?;
    let v: Value = serde_json::from_slice(&raw).ok()?;
    v["sub"].as_str().filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(|c| c.is_control())).map(str::to_string)
}

#[derive(Clone, Copy, PartialEq)]
enum Need {
    View,
    Read,
    Write,
    Manage,
}

/// Commits that only add or expire data (snapshots, refs, statistics) need write; any other change
/// (schema, partitioning, properties, location) needs manage.
fn data_only(updates: &[TableUpdate]) -> bool {
    const DATA: [&str; 8] = [
        "add-snapshot",
        "set-snapshot-ref",
        "remove-snapshots",
        "remove-snapshot-ref",
        "set-statistics",
        "remove-statistics",
        "set-partition-statistics",
        "remove-partition-statistics",
    ];
    updates.iter().all(|u| serde_json::to_value(u).ok().and_then(|v| v["action"].as_str().map(|a| DATA.contains(&a))).unwrap_or(false))
}

/// Credentials handed to engines with each table.
#[derive(Clone, Debug, PartialEq)]
pub enum Vend {
    /// None: engines bring their own storage keys.
    Off,
    /// The catalog's own store key (every engine can then reach the whole store).
    Static,
    /// Temporary keys for the one table (its location only; read-only for read-only tokens), from the
    /// store's AssumeRole on `role_arn`.
    /// Clients are told the keys expire after `revalidate` seconds at most, so they come back for new
    /// ones (GET .../credentials), and every renewal is decided again.
    Scoped { role_arn: String, seconds: i64, revalidate: i64 },
}

pub struct Catalog {
    db: Db,
    store: s3::Client,
    cfg: Config,
    /// Scoped credentials by (table location, read-only), reused until shortly before they expire.
    vended: std::sync::Mutex<HashMap<(String, bool), s3::TempCreds>>,
}

/// The session policy for a table's location: its objects, and listing under it. A location IAM would read as a
/// pattern (`*`, `?`, `${...}`) is refused: its policy would reach beyond the table (`t*` reaches `tother`).
/// Listing is allowed under `<prefix>/` only, never with the bare prefix: a list of `ns/orders` also returns
/// the keys of `ns/orders_eu/...`, another table's file names (and the partition values in them).
pub fn table_policy(location: &str, read_only: bool) -> Result<String, String> {
    if location.contains(crate::external::S3_POLICY_CHARS) {
        return Err(format!("{location}: storage access cannot be narrowed to a location with *, ? or $"));
    }
    let (bucket, key) = s3::parse_location(&format!("{}/", location.trim_end_matches('/'))).map_err(|e| e.to_string())?;
    let prefix = key.trim_end_matches('/');
    let objects = if read_only {
        json!(["s3:GetObject"])
    } else {
        json!(["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"])
    };
    Ok(json!({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": objects, "Resource": format!("arn:aws:s3:::{bucket}/{prefix}/*")},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": format!("arn:aws:s3:::{bucket}"),
         "Condition": {"StringLike": {"s3:prefix": [format!("{prefix}/*")]}}}]})
    .to_string())
}

/// A REST error: `{"error": {"message", "type", "code"}}`.
#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    kind: &'static str,
    message: String,
}

fn err(status: StatusCode, kind: &'static str, message: impl Into<String>) -> ApiError {
    ApiError { status, kind, message: message.into() }
}

fn bad_request(message: impl Into<String>) -> ApiError {
    err(StatusCode::BAD_REQUEST, "BadRequestException", message)
}

impl From<db::Error> for ApiError {
    fn from(e: db::Error) -> Self {
        match e {
            db::Error::NoSuchNamespace => err(StatusCode::NOT_FOUND, "NoSuchNamespaceException", "The namespace does not exist"),
            db::Error::NamespaceExists => err(StatusCode::CONFLICT, "AlreadyExistsException", "The namespace already exists"),
            db::Error::NamespaceNotEmpty => err(StatusCode::CONFLICT, "NamespaceNotEmptyException", "The namespace is not empty"),
            db::Error::NoSuchTable => err(StatusCode::NOT_FOUND, "NoSuchTableException", "The table does not exist"),
            db::Error::TableExists => err(StatusCode::CONFLICT, "AlreadyExistsException", "A table with that name already exists"),
            db::Error::NoSuchView => err(StatusCode::NOT_FOUND, "NoSuchViewException", "The view does not exist"),
            db::Error::ViewExists => err(StatusCode::CONFLICT, "AlreadyExistsException", "A view with that name already exists"),
            db::Error::Conflict(i) => err(
                StatusCode::CONFLICT,
                "CommitFailedException",
                format!("Table change {} of the commit is stale: the table changed since it was prepared; refresh and retry", i + 1),
            ),
            db::Error::Index(m) => {
                tracing::error!(error = %m, "catalog index");
                err(StatusCode::INTERNAL_SERVER_ERROR, "ServerError", "Internal error")
            }
        }
    }
}

impl From<s3::Error> for ApiError {
    fn from(e: s3::Error) -> Self {
        match e {
            s3::Error::BadLocation(_) => bad_request(e.to_string()),
            s3::Error::NotFound => err(StatusCode::NOT_FOUND, "NotFoundException", "The metadata file does not exist"),
            _ => {
                tracing::error!(error = %e, "object store");
                // Nothing was committed, but clients must not assume either way.
                err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", "The object store is unavailable")
            }
        }
    }
}

/// Requirement failures are commit conflicts; anything else iceberg-rust rejects is a bad request.
/// Properties the catalog keeps itself (clones, dropped tables); clients may not set or remove them.
const KEPT: &str = "cairn.";
/// A clone's: the snapshots it shares with its source (comma-separated ids), and its source.
const CLONE_INHERITED: &str = "cairn.clone.inherited";
const CLONE_SOURCE: &str = "cairn.clone.source";
/// A source's: {"<namespace>.<clone>": [snapshot ids that clone shares]}.
const CLONES: &str = "cairn.clones";
/// A namespace's dropped tables: cairn.dropped.<name> = {"metadata-location", "dropped-ms", "by"}.
const DROPPED: &str = "cairn.dropped.";
/// How often a clone, or a change to a source's record of its clones, is tried again when the source changed meanwhile.
const CLONE_RETRIES: u32 = 32;

/// Waits a random while before the next try of a clone-record commit, growing with each try, so that many clones made
/// and dropped at once do not keep invalidating each other's reads (they did with immediate retries: run 606).
async fn clone_backoff(attempt: u32) {
    use rand::Rng;
    let cap = 5u64 << attempt.min(6);
    let ms = rand::thread_rng().gen_range(0..=cap);
    tokio::time::sleep(std::time::Duration::from_millis(ms)).await;
}

/// Snapshots a table may not expire: those it shares with a clone, or as a clone with its source.
/// Expiring one would delete data files the other still reads.
fn protected_snapshots(md: &TableMetadata) -> std::collections::HashSet<i64> {
    let mut out = std::collections::HashSet::new();
    if let Some(v) = md.properties().get(CLONE_INHERITED) {
        out.extend(v.split(',').filter_map(|x| x.trim().parse::<i64>().ok()));
    }
    if let Some(v) = md.properties().get(CLONES) {
        if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(v) {
            for ids in m.values() {
                out.extend(ids.as_array().into_iter().flatten().filter_map(|x| x.as_i64()));
            }
        }
    }
    out
}

/// A namespace's properties as a client gives them at creation: never the catalog's own (a dropped-table record would
/// have undrop bring back whatever metadata it names).
fn namespace_not_kept<'a>(mut keys: impl Iterator<Item = &'a String>) -> Result<(), ApiError> {
    if keys.any(|k| k.starts_with(KEPT)) {
        return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "cairn.* namespace properties are kept by the catalog"));
    }
    Ok(())
}

/// Only the catalog makes a table a clone. A table created, staged or registered claiming to be one would, once dropped
/// and undropped, enter its "source"'s record of clones, and the source could never again expire those snapshots (nor
/// so remove what they hold).
fn not_a_clone<'a>(mut keys: impl Iterator<Item = &'a String>) -> Result<(), ApiError> {
    if keys.any(|k| k == CLONE_SOURCE || k == CLONE_INHERITED || k == CLONES) {
        return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "cairn.* table properties are kept by the catalog"));
    }
    Ok(())
}

/// What clients may not do in a commit: change the catalog's own properties, or expire shared snapshots.
fn guard(md: &TableMetadata, updates: &[TableUpdate]) -> Result<(), ApiError> {
    for u in updates {
        match u {
            TableUpdate::SetProperties { updates } if updates.keys().any(|k| k.starts_with(KEPT)) => {
                return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "cairn.* table properties are kept by the catalog"));
            }
            TableUpdate::RemoveProperties { removals } if removals.iter().any(|k| k.starts_with(KEPT)) => {
                return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "cairn.* table properties are kept by the catalog"));
            }
            TableUpdate::RemoveSnapshots { snapshot_ids } => {
                let kept = protected_snapshots(md);
                if let Some(id) = snapshot_ids.iter().find(|id| kept.contains(id)) {
                    return Err(err(
                        StatusCode::CONFLICT,
                        "CommitFailedException",
                        format!("Snapshot {id} is shared with a clone; it is kept until the clone is dropped"),
                    ));
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn undrop_days() -> i64 {
    std::env::var("CAIRN_CATALOG_UNDROP_DAYS").ok().and_then(|v| v.parse().ok()).unwrap_or(7)
}

fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

#[derive(Deserialize)]
struct CloneTable {
    #[serde(default)]
    namespace: Option<Vec<String>>,
    name: String,
}

fn iceberg_err(e: iceberg::Error) -> ApiError {
    if e.kind() == iceberg::ErrorKind::CatalogCommitConflicts {
        err(StatusCode::CONFLICT, "CommitFailedException", e.message().to_string())
    } else {
        bad_request(e.message().to_string())
    }
}

fn json_resp(status: StatusCode, v: &impl Serialize) -> Resp {
    let mut r = Response::new(Full::new(Bytes::from(serde_json::to_vec(v).unwrap_or_default())).boxed());
    *r.status_mut() = status;
    r.headers_mut().insert("content-type", "application/json".parse().unwrap());
    r
}

fn empty(status: StatusCode) -> Resp {
    let mut r = Response::new(Full::new(Bytes::new()).boxed());
    *r.status_mut() = status;
    r
}

fn error_resp(e: &ApiError, head: bool) -> Resp {
    if head {
        return empty(e.status);
    }
    json_resp(e.status, &json!({"error": {"message": e.message, "type": e.kind, "code": e.status.as_u16()}}))
}

// ------------------------------------------------------------------------------------------- requests

#[derive(Deserialize)]
struct CreateNamespace {
    namespace: Vec<String>,
    #[serde(default)]
    properties: BTreeMap<String, String>,
}

#[derive(Deserialize, Default)]
struct UpdateNamespace {
    #[serde(default)]
    removals: Vec<String>,
    #[serde(default)]
    updates: BTreeMap<String, String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "kebab-case")]
struct CreateTable {
    name: String,
    location: Option<String>,
    schema: iceberg::spec::Schema,
    partition_spec: Option<UnboundPartitionSpec>,
    write_order: Option<SortOrder>,
    #[serde(default)]
    stage_create: bool,
    #[serde(default)]
    properties: HashMap<String, String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "kebab-case")]
struct CreateView {
    name: String,
    location: Option<String>,
    schema: iceberg::spec::Schema,
    view_version: iceberg::spec::ViewVersion,
    #[serde(default)]
    properties: HashMap<String, String>,
}

#[derive(Deserialize)]
struct CommitView {
    identifier: Option<Identifier>,
    #[serde(default)]
    requirements: Vec<Value>,
    updates: Vec<iceberg::ViewUpdate>,
}

/// Applies one REST view update to a view metadata builder.
fn apply_view_update(b: iceberg::spec::ViewMetadataBuilder, u: iceberg::ViewUpdate) -> iceberg::Result<iceberg::spec::ViewMetadataBuilder> {
    use iceberg::ViewUpdate as U;
    Ok(match u {
        U::AssignUuid { uuid } => b.assign_uuid(uuid),
        U::UpgradeFormatVersion { format_version } => b.upgrade_format_version(format_version)?,
        U::AddSchema { schema, .. } => b.add_schema(schema),
        U::SetLocation { location } => b.set_location(location),
        U::SetProperties { updates } => b.set_properties(updates)?,
        U::RemoveProperties { removals } => b.remove_properties(&removals),
        U::AddViewVersion { view_version } => b.add_version(view_version)?,
        U::SetCurrentViewVersion { view_version_id } => b.set_current_version_id(view_version_id)?,
    })
}

#[derive(Deserialize, Serialize, Clone)]
struct Identifier {
    namespace: Vec<String>,
    name: String,
}

#[derive(Deserialize)]
struct CommitTable {
    identifier: Option<Identifier>,
    #[serde(default)]
    requirements: Vec<TableRequirement>,
    #[serde(default)]
    updates: Vec<TableUpdate>,
}

#[derive(Deserialize)]
#[serde(rename_all = "kebab-case")]
struct CommitTransaction {
    table_changes: Vec<CommitTable>,
}

#[derive(Deserialize)]
struct Rename {
    source: Identifier,
    destination: Identifier,
}

#[derive(Deserialize)]
#[serde(rename_all = "kebab-case")]
struct Register {
    name: String,
    metadata_location: String,
}

fn parse<T: serde::de::DeserializeOwned>(body: &[u8]) -> Result<T, ApiError> {
    serde_json::from_slice(body).map_err(|e| bad_request(format!("Invalid request body: {e}")))
}

/// The namespace path parameter: levels separated by the unit separator (%1F).
fn ns_param(raw: &str) -> Vec<String> {
    let dec = String::from_utf8_lossy(&percent_encoding::percent_decode_str(raw).collect::<Vec<u8>>()).into_owned();
    dec.split(db::SEP).map(str::to_string).collect()
}

fn decode(raw: &str) -> String {
    String::from_utf8_lossy(&percent_encoding::percent_decode_str(raw).collect::<Vec<u8>>()).into_owned()
}

/// A path segment, percent-encoded.
fn encode(raw: &str) -> String {
    percent_encoding::utf8_percent_encode(raw, percent_encoding::NON_ALPHANUMERIC).to_string()
}

fn valid_name(s: &str) -> bool {
    !s.is_empty() && s.len() <= 255 && !s.contains(db::SEP) && !s.contains('/') && s.chars().all(|c| !c.is_control())
}

/// A prepared table commit: the change to apply and the metadata file it wrote.
struct Prepared {
    change: Change,
    metadata: Value,
}

const ENDPOINTS: [&str; 24] = [
    "GET /v1/{prefix}/namespaces",
    "POST /v1/{prefix}/namespaces",
    "GET /v1/{prefix}/namespaces/{namespace}",
    "HEAD /v1/{prefix}/namespaces/{namespace}",
    "DELETE /v1/{prefix}/namespaces/{namespace}",
    "POST /v1/{prefix}/namespaces/{namespace}/properties",
    "GET /v1/{prefix}/namespaces/{namespace}/tables",
    "POST /v1/{prefix}/namespaces/{namespace}/tables",
    "GET /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "HEAD /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "POST /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "DELETE /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "POST /v1/{prefix}/namespaces/{namespace}/register",
    "POST /v1/{prefix}/namespaces/{namespace}/tables/{table}/metrics",
    "POST /v1/{prefix}/tables/rename",
    "GET /v1/{prefix}/namespaces/{namespace}/views",
    "POST /v1/{prefix}/namespaces/{namespace}/views",
    "GET /v1/{prefix}/namespaces/{namespace}/views/{view}",
    "POST /v1/{prefix}/namespaces/{namespace}/views/{view}",
    "DELETE /v1/{prefix}/namespaces/{namespace}/views/{view}",
    "POST /v1/{prefix}/views/rename",
    "POST /v1/{prefix}/transactions/commit",
    "GET /v1/{prefix}/namespaces/{namespace}/tables/{table}/credentials",
    "GET /v1/config",
];

impl Catalog {
    pub fn new(db: Db, cfg: Config) -> Catalog {
        Catalog { db, store: s3::Client::new(cfg.s3.clone()), cfg, vended: Default::default() }
    }

    async fn authenticate(&self, auth: &str) -> Result<Principal, ApiError> {
        let token = auth.strip_prefix("Bearer ").unwrap_or("");
        if token.is_empty() {
            return Err(err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", "A bearer token is required"));
        }
        if let Some(p) = self.cfg.principals.iter().find(|p| bool::from(p.token.as_bytes().ct_eq(token.as_bytes()))) {
            return Ok(p.clone());
        }
        if token.starts_with(DELEGATED) {
            return self.delegated(token);
        }
        match &self.cfg.identity {
            Some(id) if token.matches('.').count() == 2 => match id.verify(token).await {
                Ok(user) if crate::identity::is_share(&user) => {
                    // A recipient outside the organisation: read-only, its share's namespaces only, asked
                    // afresh on every request. Never with the catalog's own store key.
                    if matches!(self.cfg.vend, Vend::Static) {
                        return Err(err(
                            StatusCode::FORBIDDEN,
                            "ForbiddenException",
                            "External shares need scoped storage credentials (CAIRN_CATALOG_VEND_CREDENTIALS=scoped)",
                        ));
                    }
                    let scope = id.share_scope(&user).await.map_err(|e| {
                        tracing::warn!(error = %e, user = %user, "a share could not be checked: refused");
                        err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", "Cairn could not check the share now")
                    })?;
                    if scope.is_empty() {
                        tracing::info!(user = %user, "a share that has ended was refused");
                        return Err(err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", "The share has ended (revoked or expired)"));
                    }
                    Ok(Principal {
                        token: String::new(),
                        name: format!("user:{user}"),
                        namespaces: scope,
                        read_only: true,
                        user: Some(user),
                        delegate: false,
                        delegated: false,
                    })
                }
                Ok(user) => Ok(Principal {
                    token: String::new(),
                    name: format!("user:{user}"),
                    namespaces: vec![],
                    read_only: false,
                    user: Some(user),
                    delegate: false,
                    delegated: false,
                }),
                Err(why) => {
                    tracing::info!(error = %why, "a Cairn token was refused");
                    Err(err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", format!("The token is not valid: {why}")))
                }
            },
            _ => Err(err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", "The token is not valid")),
        }
    }

    /// For a Cairn user, Cairn's decision on one table; refuses what they may not do (and reading a
    /// table a mask or row filter applies to). Other principals may do everything their scope allows.
    async fn need(&self, p: &Principal, levels: &[String], name: &str, need: Need) -> Result<crate::identity::Decision, ApiError> {
        let (Some(user), Some(id)) = (&p.user, &self.cfg.identity) else { return Ok(crate::identity::Decision::all()) };
        let d = id
            .decide(user, &[(levels.to_vec(), name.to_string())])
            .await
            .map_err(|e| {
                tracing::warn!(error = %e, user = %user, "a lakehouse decision could not be made: refused");
                err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", "Cairn could not decide access to this table now")
            })?
            .pop()
            .unwrap_or_default();
        let table = format!("{}.{name}", levels.join("."));
        let (ok, verb) = match need {
            Need::View => (d.view, "see"),
            Need::Read => (d.read, "read"),
            Need::Write => (d.write, "write to"),
            Need::Manage => (d.manage, "change or drop"),
        };
        if !ok {
            tracing::info!(user = %user, table = %table, need = verb, "refused by Cairn's policies");
            return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", format!("Cairn's policies do not let {user} {verb} {table}")));
        }
        if need == Need::Read && d.restricted && !p.delegated {
            tracing::info!(user = %user, table = %table, "restricted: refused to engines reading files");
            return Err(err(
                StatusCode::FORBIDDEN,
                "ForbiddenException",
                format!("Masks or row filters apply to {user} on {table}: its files cannot be handed out. Query it through Trino, which applies them."),
            ));
        }
        Ok(d)
    }

    /// For a Cairn user: may they create, change and drop namespaces?
    async fn need_schemas(&self, p: &Principal) -> Result<(), ApiError> {
        let (Some(user), Some(id)) = (&p.user, &self.cfg.identity) else { return Ok(()) };
        match id.schemas(user).await {
            Ok(true) => Ok(()),
            Ok(false) => {
                Err(err(StatusCode::FORBIDDEN, "ForbiddenException", format!("Cairn's policies do not let {user} manage namespaces")))
            }
            Err(e) => {
                tracing::warn!(error = %e, "a lakehouse decision could not be made: refused");
                Err(err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", "Cairn could not decide access now"))
            }
        }
    }

    /// For a Cairn user, the names they may see.
    async fn visible(&self, p: &Principal, levels: &[String], names: Vec<String>) -> Result<Vec<String>, ApiError> {
        let (Some(user), Some(id)) = (&p.user, &self.cfg.identity) else { return Ok(names) };
        if names.is_empty() {
            return Ok(names);
        }
        let q: Vec<(Vec<String>, String)> = names.iter().map(|n| (levels.to_vec(), n.clone())).collect();
        let ds = id
            .decide(user, &q)
            .await
            .map_err(|_| err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", "Cairn could not decide access now"))?;
        Ok(names.into_iter().zip(ds).filter(|(_, d)| d.view).map(|(n, _)| n).collect())
    }

    pub async fn handle<B>(self: Arc<Self>, req: Request<B>) -> Result<Resp, Infallible>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let head = req.method() == Method::HEAD;
        let (method, path) = (req.method().clone(), req.uri().path().to_string());
        let started = std::time::Instant::now();
        let resp = match self.route(req).await {
            Ok(r) => r,
            Err(e) => error_resp(&e, head),
        };
        tracing::debug!(method = %method, path = %path, status = resp.status().as_u16(), ms = started.elapsed().as_millis() as u64, "request");
        Ok(resp)
    }

    async fn route<B>(&self, req: Request<B>) -> Result<Resp, ApiError>
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let path = req.uri().path().to_string();
        if path == "/-/healthy" || path == "/-/ready" {
            return Ok(empty(StatusCode::OK));
        }
        let auth = req.headers().get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let principal = self.authenticate(&auth).await?;
        tracing::debug!(principal = %principal.name, "authenticated");
        let method = req.method().clone();
        let query: BTreeMap<String, String> = req
            .uri()
            .query()
            .unwrap_or("")
            .split('&')
            .filter(|p| !p.is_empty())
            .map(|p| {
                let (k, v) = p.split_once('=').unwrap_or((p, ""));
                (decode(k), decode(v))
            })
            .collect();
        let body = req.into_body().collect().await.map_err(|e| bad_request(format!("request body: {e}")))?.to_bytes();
        let segs: Vec<&str> = path.trim_start_matches('/').split('/').collect();
        if method != Method::GET && method != Method::HEAD && principal.read_only {
            return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "This token may only read"));
        }
        let scope = |levels: &[String]| -> Result<(), ApiError> {
            if principal.allows(levels) {
                Ok(())
            } else {
                Err(err(StatusCode::FORBIDDEN, "ForbiddenException", format!("Not allowed in namespace {}", levels.join("."))))
            }
        };
        match (method.clone(), segs.as_slice()) {
            (Method::GET, ["v1", "config"]) => {
                Ok(json_resp(StatusCode::OK, &json!({"defaults": {}, "overrides": {}, "endpoints": ENDPOINTS})))
            }
            (Method::POST, ["v1", "oauth", "tokens"]) => self.exchange(&principal, &body),
            (Method::GET, ["v1", "locations", "credentials"]) => {
                // Files under a registered location, by path (not a table): READ_FILES or WRITE_FILES on it.
                let url = query.get("url").cloned().unwrap_or_default();
                let write = match query.get("access").map(String::as_str).unwrap_or("read") {
                    "read" => false,
                    "write" => true,
                    _ => return Err(bad_request("access: read or write")),
                };
                if write && principal.read_only {
                    return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "This token may only read"));
                }
                if principal.user.is_none() && !principal.namespaces.is_empty() {
                    // Files by path belong to no namespace, and Cairn decides location privileges for its users
                    // only: a catalog token kept to some namespaces would otherwise reach every location's files.
                    return Err(err(
                        StatusCode::FORBIDDEN,
                        "ForbiddenException",
                        "A token kept to some namespaces gets no access to files by path",
                    ));
                }
                let Place::External(l, c) = self.place(&url)? else {
                    return Err(err(
                        StatusCode::FORBIDDEN,
                        "ForbiddenException",
                        format!("{url} is not in a registered external location"),
                    ));
                };
                if write && l.read_only {
                    return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", format!("External location {} is read-only", l.name)));
                }
                let privilege = if write { "WRITE_FILES" } else { "READ_FILES" };
                self.location_event(&principal, &l, Some(privilege), "vend", &url, None, write).await?;
                let v = self.cfg.external.vend(&c, &url, write).await.map_err(unavailable)?;
                Ok(json_resp(
                    StatusCode::OK,
                    &json!({"location": l.name, "prefix": url, "config": v.config, "expires-at-ms": v.expires_ms}),
                ))
            }
            (Method::GET, ["v1", "namespaces"]) => {
                let parent = query.get("parent").map(|p| ns_param(p)).unwrap_or_default();
                let mut list = self.db.list_namespaces(&parent)?;
                list.retain(|l| principal.allows(l));
                Ok(json_resp(StatusCode::OK, &json!({"namespaces": list, "next-page-token": null})))
            }
            (Method::POST, ["v1", "namespaces"]) => {
                let req: CreateNamespace = parse(&body)?;
                if req.namespace.is_empty() || !req.namespace.iter().all(|l| valid_name(l)) {
                    return Err(bad_request("Invalid namespace name"));
                }
                scope(&req.namespace)?;
                namespace_not_kept(req.properties.keys())?;
                self.need_schemas(&principal).await?;
                if let Some(l) = req.properties.get("location") {
                    self.resident(&req.namespace, l, std::iter::empty())?;
                    self.placement(Some(&principal), &req.namespace, l, std::iter::empty(), true, Placed::Namespace).await?;
                }
                self.db.create_namespace(&req.namespace, req.properties.clone())?;
                Ok(json_resp(StatusCode::OK, &json!({"namespace": req.namespace, "properties": req.properties})))
            }
            (m, ["v1", "namespaces", ns]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                match m {
                    Method::GET => {
                        let mut props = self.db.namespace(&levels)?;
                        props.retain(|k, _| !k.starts_with(KEPT));
                        // Where its tables go unless they name a location (engines such as Trino build
                        // table locations from it).
                        if matches!(self.cfg.residency.pin(&levels), Ok(Some(_))) {
                            props.insert("location".into(), self.namespace_location(&levels));
                        }
                        props.entry("location".into()).or_insert_with(|| self.namespace_location(&levels));
                        Ok(json_resp(StatusCode::OK, &json!({"namespace": levels, "properties": props})))
                    }
                    Method::HEAD => {
                        self.db.namespace(&levels)?;
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    Method::DELETE => {
                        self.need_schemas(&principal).await?;
                        self.db.drop_namespace(&levels)?;
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    _ => Err(bad_request("Unsupported method")),
                }
            }
            (Method::POST, ["v1", "namespaces", ns, "properties"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let req: UpdateNamespace = parse(&body)?;
                if req.removals.iter().chain(req.updates.keys()).any(|k| k.starts_with(KEPT)) {
                    return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "cairn.* namespace properties are kept by the catalog"));
                }
                if req.removals.iter().any(|k| req.updates.contains_key(k)) {
                    return Err(err(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "UnprocessableEntityException",
                        "A key cannot be both removed and updated",
                    ));
                }
                self.need_schemas(&principal).await?;
                if let Some(l) = req.updates.get("location") {
                    self.resident(&levels, l, std::iter::empty())?;
                    self.placement(Some(&principal), &levels, l, std::iter::empty(), true, Placed::Namespace).await?;
                }
                let (updated, removed, missing) = self.db.update_namespace(&levels, &req.removals, &req.updates)?;
                Ok(json_resp(StatusCode::OK, &json!({"updated": updated, "removed": removed, "missing": missing})))
            }
            (Method::GET, ["v1", "namespaces", ns, "tables"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let names = self.visible(&principal, &levels, self.db.list_tables(&levels)?).await?;
                let ids: Vec<Identifier> = names.into_iter().map(|name| Identifier { namespace: levels.clone(), name }).collect();
                Ok(json_resp(StatusCode::OK, &json!({"identifiers": ids, "next-page-token": null})))
            }
            (Method::POST, ["v1", "namespaces", ns, "tables"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let req: CreateTable = parse(&body)?;
                self.need(&principal, &levels, &req.name, Need::Manage).await?;
                self.create_table(&principal, &levels, req).await
            }
            (Method::POST, ["v1", "namespaces", ns, "register"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let req: Register = parse(&body)?;
                self.need(&principal, &levels, &req.name, Need::Manage).await?;
                self.register(Some(&principal), &levels, req, true).await
            }
            (Method::POST, ["v1", "namespaces", ns, "tables", t, "metrics"]) => {
                scope(&ns_param(ns))?;
                let _ = decode(t);
                Ok(empty(StatusCode::NO_CONTENT))
            }
            (Method::GET, ["v1", "namespaces", ns, "tables", t, "credentials"]) => {
                // Iceberg clients renew a table's storage keys here before they expire: decided again.
                let (levels, name) = (ns_param(ns), decode(t));
                scope(&levels)?;
                let d = self.need(&principal, &levels, &name, Need::Read).await?;
                let row = self.db.table(&levels, &name)?;
                let metadata: Value = serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?;
                let location = metadata["location"].as_str().unwrap_or("").to_string();
                let external = matches!(self.place(&location)?, Place::External(..));
                if !external && !matches!(self.cfg.vend, Vend::Scoped { .. }) {
                    return Err(err(StatusCode::NOT_FOUND, "NotFoundException", "This catalog vends no renewable credentials"));
                }
                let (config, _) =
                    self.client_config(&principal, &location, principal.read_only || !d.write, Some((&levels, &name))).await?;
                tracing::info!(principal = %principal.name, table = %format!("{}.{}", levels.join("."), name), "storage credentials renewed");
                Ok(json_resp(StatusCode::OK, &json!({"storage-credentials": [{"prefix": location, "config": config}]})))
            }
            (Method::POST, ["v1", "namespaces", ns, "tables", t, "clone"]) => {
                let (levels, name) = (ns_param(ns), decode(t));
                scope(&levels)?;
                let req: CloneTable = parse(&body)?;
                let target = req.namespace.clone().unwrap_or_else(|| levels.clone());
                scope(&target)?;
                self.need(&principal, &levels, &name, Need::Read).await?;
                self.need(&principal, &target, &req.name, Need::Manage).await?;
                self.clone_table(&principal, &levels, &name, &target, &req.name).await
            }
            (Method::POST, ["v1", "namespaces", ns, "tables", t, "undrop"]) => {
                let (levels, name) = (ns_param(ns), decode(t));
                scope(&levels)?;
                self.need(&principal, &levels, &name, Need::Manage).await?;
                self.undrop(&principal, &levels, &name).await
            }
            (Method::GET, ["v1", "namespaces", ns, "dropped"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let props = self.db.namespace(&levels)?;
                let cutoff = now_ms() - undrop_days() * 86_400_000;
                let mut found: Vec<(String, Value)> = vec![];
                for (k, v) in &props {
                    if let Some(n) = k.strip_prefix(DROPPED) {
                        let rec: Value = serde_json::from_str(v).unwrap_or(Value::Null);
                        if rec["dropped-ms"].as_i64().unwrap_or(0) >= cutoff {
                            found.push((n.to_string(), rec));
                        }
                    }
                }
                let names = self.visible(&principal, &levels, found.iter().map(|(n, _)| n.clone()).collect()).await?;
                let out: Vec<Value> = found
                    .into_iter()
                    .filter(|(n, _)| names.contains(n))
                    .map(|(n, r)| json!({"name": n, "dropped-ms": r["dropped-ms"], "by": r["by"]}))
                    .collect();
                Ok(json_resp(StatusCode::OK, &json!({"dropped": out, "retention-days": undrop_days()})))
            }
            (m, ["v1", "namespaces", ns, "tables", t]) => {
                let (levels, name) = (ns_param(ns), decode(t));
                scope(&levels)?;
                match m {
                    Method::GET => {
                        let d = self.need(&principal, &levels, &name, Need::Read).await?;
                        let row = table_in(&self.db, &levels, &name)?;
                        self.load_result(
                            &principal,
                            &row.metadata_location,
                            serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?,
                            principal.read_only || !d.write,
                            (&levels, &name),
                        )
                        .await
                    }
                    Method::HEAD => {
                        self.need(&principal, &levels, &name, Need::View).await?;
                        table_in(&self.db, &levels, &name)?;
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    Method::POST => {
                        let req: CommitTable = parse(&body)?;
                        let need = if data_only(&req.updates) { Need::Write } else { Need::Manage };
                        self.need(&principal, &levels, &name, need).await?;
                        if let Some(id) = &req.identifier {
                            if id.namespace != levels || id.name != name {
                                return Err(bad_request("The identifier in the body differs from the path"));
                            }
                        }
                        let p = self.prepare(&levels, &name, req.requirements, req.updates, Some(&principal)).await?;
                        let location = p.change.new.metadata_location.clone();
                        self.apply(vec![p.change]).await?;
                        Ok(json_resp(StatusCode::OK, &json!({"metadata-location": location, "metadata": p.metadata})))
                    }
                    Method::DELETE => {
                        self.need(&principal, &levels, &name, Need::Manage).await?;
                        let purge = query.get("purgeRequested").is_some_and(|v| v.eq_ignore_ascii_case("true"));
                        let current = self.db.table(&levels, &name)?;
                        let own = self.clones_of_a_clone(&current.metadata);
                        if !own.is_empty() {
                            // Its clones read files its source holds for it: dropping it would let the source
                            // expire them.
                            return Err(err(
                                StatusCode::CONFLICT,
                                "CommitFailedException",
                                format!(
                                    "{}.{name} is a clone with clones of its own ({}), which read files its source keeps for it: drop them first",
                                    levels.join("."),
                                    own.join(", ")
                                ),
                            ));
                        }
                        if !purge {
                            // Kept for undrop: the last metadata stays in the store.
                            self.remember_drop(&levels, &name, &current.metadata_location, &principal.name)?;
                        }
                        let row = self.db.drop_table(&levels, &name)?;
                        self.forget_clone(&levels, &name, &row).await;
                        if purge {
                            // Data files are left for the store's lifecycle tooling; the current metadata goes now.
                            let _ = self.delete_file(&row.metadata_location).await;
                        }
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    _ => Err(bad_request("Unsupported method")),
                }
            }
            (Method::GET, ["v1", "namespaces", ns, "views"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let names = self.visible(&principal, &levels, self.db.list_views(&levels)?).await?;
                let ids: Vec<Identifier> = names.into_iter().map(|name| Identifier { namespace: levels.clone(), name }).collect();
                Ok(json_resp(StatusCode::OK, &json!({"identifiers": ids, "next-page-token": null})))
            }
            (Method::POST, ["v1", "namespaces", ns, "views"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let req: CreateView = parse(&body)?;
                self.need(&principal, &levels, &req.name, Need::Manage).await?;
                self.create_view(&principal, &levels, req).await
            }
            (m, ["v1", "namespaces", ns, "views", v]) => {
                let (levels, name) = (ns_param(ns), decode(v));
                scope(&levels)?;
                match m {
                    Method::GET => {
                        self.need(&principal, &levels, &name, Need::View).await?;
                        let row = self.db.view(&levels, &name)?;
                        let metadata: Value = serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?;
                        Ok(json_resp(
                            StatusCode::OK,
                            &json!({"metadata-location": row.metadata_location, "metadata": metadata, "config": {}}),
                        ))
                    }
                    Method::HEAD => {
                        self.need(&principal, &levels, &name, Need::View).await?;
                        self.db.view(&levels, &name)?;
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    Method::POST => {
                        self.need(&principal, &levels, &name, Need::Manage).await?;
                        self.replace_view(&principal, &levels, &name, parse(&body)?).await
                    }
                    Method::DELETE => {
                        self.need(&principal, &levels, &name, Need::Manage).await?;
                        self.db.drop_view(&levels, &name)?;
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    _ => Err(bad_request("Unsupported method")),
                }
            }
            (Method::POST, ["v1", "views", "rename"]) => {
                let req: Rename = parse(&body)?;
                scope(&req.source.namespace)?;
                scope(&req.destination.namespace)?;
                if !valid_name(&req.destination.name) {
                    return Err(bad_request("Invalid view name"));
                }
                self.need(&principal, &req.source.namespace, &req.source.name, Need::Manage).await?;
                self.need(&principal, &req.destination.namespace, &req.destination.name, Need::Manage).await?;
                self.db.rename_view((&req.source.namespace, &req.source.name), (&req.destination.namespace, &req.destination.name))?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            (Method::POST, ["v1", "tables", "rename"]) => {
                let req: Rename = parse(&body)?;
                scope(&req.source.namespace)?;
                scope(&req.destination.namespace)?;
                if !valid_name(&req.destination.name) {
                    return Err(bad_request("Invalid table name"));
                }
                self.need(&principal, &req.source.namespace, &req.source.name, Need::Manage).await?;
                self.need(&principal, &req.destination.namespace, &req.destination.name, Need::Manage).await?;
                self.resident_move(&req.source.namespace, &req.destination.namespace)?;
                // A clone's source holds the shared snapshots under the clone's name: renamed, a new clone under the
                // old name would take that record, and dropping it would release what this one still reads.
                let row = self.db.table(&req.source.namespace, &req.source.name)?;
                if serde_json::from_str::<TableMetadata>(&row.metadata).is_ok_and(|md| md.properties().contains_key(CLONE_SOURCE)) {
                    return Err(err(
                        StatusCode::CONFLICT,
                        "CommitFailedException",
                        format!(
                            "{}.{} is a clone and keeps its name: clone it again under the new name (nothing is copied) and drop this one",
                            req.source.namespace.join("."),
                            req.source.name
                        ),
                    ));
                }
                self.db.rename_table((&req.source.namespace, &req.source.name), (&req.destination.namespace, &req.destination.name))?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            (Method::POST, ["v1", "transactions", "commit"]) => {
                let req: CommitTransaction = parse(&body)?;
                if req.table_changes.is_empty() {
                    return Err(bad_request("No table changes"));
                }
                let mut prepared = vec![];
                for c in req.table_changes {
                    let id = c.identifier.ok_or_else(|| bad_request("Each table change needs an identifier"))?;
                    scope(&id.namespace)?;
                    let need = if data_only(&c.updates) { Need::Write } else { Need::Manage };
                    self.need(&principal, &id.namespace, &id.name, need).await?;
                    prepared.push(self.prepare(&id.namespace, &id.name, c.requirements, c.updates, Some(&principal)).await?);
                }
                self.apply(prepared.into_iter().map(|p| p.change).collect()).await?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            (_, ["v1", ..]) => Err(err(StatusCode::NOT_FOUND, "NotFoundException", format!("No route for {method} {path}"))),
            _ => Err(err(StatusCode::NOT_FOUND, "NotFoundException", "Not found")),
        }
    }

    async fn load_result(
        &self,
        who: &Principal,
        location: &str,
        metadata: Value,
        read_only: bool,
        ident: (&[String], &str),
    ) -> Result<Resp, ApiError> {
        let table_location = metadata["location"].as_str().unwrap_or("").to_string();
        let (config, creds) = self.client_config(who, &table_location, read_only, Some(ident)).await?;
        let mut out = json!({"metadata-location": location, "metadata": metadata, "config": config});
        if let Some(c) = creds {
            out["storage-credentials"] = c;
        }
        Ok(json_resp(StatusCode::OK, &out))
    }

    /// Storage settings for clients: the object store endpoint and, if vending is on, credentials (scoped:
    /// temporary keys for this table's location only), with the storage credential to send them as when they are
    /// renewable. A table in an external location always gets access for its location alone, after Cairn has
    /// recorded the vend.
    async fn client_config(
        &self,
        who: &Principal,
        table_location: &str,
        read_only: bool,
        ident: Option<(&[String], &str)>,
    ) -> Result<(BTreeMap<String, String>, Option<Value>), ApiError> {
        let mut c = BTreeMap::new();
        if let Place::External(l, cred) = self.place(table_location)? {
            let write = !read_only && !l.read_only;
            let table = ident.map(|(ns, n)| format!("{}.{n}", ns.join(".")));
            self.location_event(who, &l, None, "vend", table_location, table, write).await?;
            let v = self.cfg.external.vend(&cred, table_location, write).await.map_err(unavailable)?;
            c.extend(v.config);
            if let Some(ident) = ident {
                self.refresh_endpoint(&mut c, ident);
                for k in ["adls", "gcs.oauth2"] {
                    let ep = c["client.refresh-credentials-endpoint"].clone();
                    c.insert(format!("{k}.refresh-credentials-endpoint"), ep);
                    c.insert(format!("{k}.refresh-credentials-enabled"), "true".into());
                }
            }
            tracing::info!(principal = %who.name, location = %l.name, write, "external storage credentials vended");
            let creds = json!([{"prefix": table_location, "config": c}]);
            return Ok((c, Some(creds)));
        }
        let (ak, sk, token) = match &self.cfg.vend {
            Vend::Off => return Ok((c, None)),
            Vend::Static => (self.cfg.s3.access_key.clone(), self.cfg.s3.secret_key.clone(), None),
            Vend::Scoped { role_arn, seconds, revalidate } => {
                let t = self.scoped(role_arn, *seconds, table_location, read_only).await?;
                // Report an earlier expiry when access is to be decided again sooner than the keys expire.
                let now = time::OffsetDateTime::now_utc().unix_timestamp();
                let told = t.expires_unix.min(now + *revalidate);
                c.insert("s3.session-token-expires-at-ms".into(), (told * 1000).to_string());
                (t.access_key, t.secret_key, Some(t.token))
            }
        };
        c.insert("s3.endpoint".into(), self.cfg.client_endpoint.clone());
        c.insert("s3.region".into(), self.cfg.s3.region.clone());
        c.insert("client.region".into(), self.cfg.s3.region.clone());
        c.insert("s3.path-style-access".into(), "true".into());
        c.insert("s3.access-key-id".into(), ak);
        c.insert("s3.secret-access-key".into(), sk);
        if let Some(t) = token {
            c.insert("s3.session-token".into(), t);
        }
        if let (Vend::Scoped { .. }, Some(ident)) = (&self.cfg.vend, ident) {
            // Where clients renew the keys (relative to the catalog's URI), before they expire. Both in the
            // table's config and in its storage credential: Iceberg builds a storage client per credential.
            self.refresh_endpoint(&mut c, ident);
            let creds = json!([{"prefix": table_location, "config": c}]);
            return Ok((c, Some(creds)));
        }
        Ok((c, None))
    }

    fn refresh_endpoint(&self, c: &mut BTreeMap<String, String>, ident: (&[String], &str)) {
        let ns: Vec<String> = ident.0.iter().map(|l| encode(l)).collect();
        c.insert(
            "client.refresh-credentials-endpoint".into(),
            format!("v1/namespaces/{}/tables/{}/credentials", ns.join("%1F"), encode(ident.1)),
        );
        c.insert("client.refresh-credentials-enabled".into(), "true".into());
    }

    // ---- external locations ---------------------------------------------------------------------------

    /// Where `url` lives. Without external locations, every `s3://` location is Cairn Store's (as before). With them,
    /// Cairn Store keeps only its own places (the warehouse, the regions' locations, CAIRN_CATALOG_STORE_URLS), and
    /// everything else must be in a registered external location: a location that is removed, or cannot be loaded
    /// from Cairn, is refused, never taken for Cairn Store (fail closed).
    fn place(&self, url: &str) -> Result<Place, ApiError> {
        let u = crate::external::parse(url).map_err(bad_request)?;
        let ext = &self.cfg.external;
        let outside = || {
            err(
                StatusCode::FORBIDDEN,
                "ForbiddenException",
                format!("{url} is not in a registered external location (an administrator registers one in Cairn)"),
            )
        };
        if !ext.active() {
            return if u.kind == crate::external::Kind::S3 { Ok(Place::Store) } else { Err(outside()) };
        }
        if u.kind == crate::external::Kind::S3 && ext.reserved_covers(&u) {
            return Ok(Place::Store);
        }
        let snap = ext.snapshot().map_err(|e| {
            tracing::warn!(error = %e, "external locations unavailable: refused");
            err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", e)
        })?;
        match snap.find(&u) {
            // Access to an S3 path is narrowed by an IAM session policy, which would read these as patterns.
            Some(_) if u.kind == crate::external::Kind::S3 && u.path.contains(crate::external::S3_POLICY_CHARS) => {
                Err(bad_request(format!("{url}: a path in an external S3 location may not contain *, ? or $")))
            }
            Some((l, c)) => Ok(Place::External(l.clone(), c.clone())),
            None => Err(outside()),
        }
    }

    /// Why `u` is not a place of its own for the table or view `me` (in namespace `levels`), if it is not. A table may
    /// not be at, around or inside another table (access handed out for one would reach the other's files), nor
    /// around a view (its keys would rewrite the view's metadata), nor at or around a place tables share: the
    /// warehouse, a region's or another Cairn Store place, a namespace's location. A view may not be in a table's
    /// files. Tables already placed so (before this check) are left as they are: only new placements are refused.
    fn own_place(&self, u: &crate::external::StorageUrl, levels: &[String], me: Placed) -> Result<Option<String>, ApiError> {
        let (view, name) = match me {
            Placed::Table(n) => (false, n),
            Placed::View(n) => (true, n),
            Placed::Namespace => return Ok(None),
        };
        let at = |md: &str| -> Option<crate::external::StorageUrl> {
            let l: Located = serde_json::from_str(md).ok()?;
            crate::external::parse(&l.location?).ok()
        };
        let mut shared: Option<String> = None;
        if !view && self.cfg.external.reserved_within(u) {
            shared = Some("Cairn Store's own storage (the warehouse, a region's location)".into());
        }
        let mut todo: Vec<Vec<String>> = vec![vec![]];
        while let Some(ns) = todo.pop() {
            match self.db.list_namespaces(&ns) {
                Ok(children) => todo.extend(children),
                Err(db::Error::NoSuchNamespace) => continue,
                Err(e) => return Err(e.into()),
            }
            if ns.is_empty() {
                continue;
            }
            let dotted = ns.join(".");
            if !view && shared.is_none() {
                let props = match self.db.namespace(&ns) {
                    Ok(p) => p,
                    Err(db::Error::NoSuchNamespace) => continue,
                    Err(e) => return Err(e.into()),
                };
                let pinned = matches!(self.cfg.residency.pin(&ns), Ok(Some(_)));
                let own = props.get("location").filter(|_| !pinned).cloned().unwrap_or_else(|| self.namespace_location(&ns));
                if crate::external::parse(&own).is_ok_and(|n| u.covers(&n)) {
                    shared = Some(format!("the location of namespace {dotted}"));
                }
            }
            let tables = match self.db.list_tables(&ns) {
                Ok(n) => n,
                Err(db::Error::NoSuchNamespace) => continue,
                Err(e) => return Err(e.into()),
            };
            for t in tables {
                if !view && ns == levels && t == name {
                    continue;
                }
                let row = match self.db.table(&ns, &t) {
                    Ok(r) => r,
                    Err(db::Error::NoSuchTable | db::Error::NoSuchNamespace) => continue,
                    Err(e) => return Err(e.into()),
                };
                let Some(other) = at(&row.metadata) else { continue };
                let clash = if view { other.covers(u) } else { other.overlaps(u) };
                if clash {
                    return Ok(Some(format!("overlaps the location of table {dotted}.{t}")));
                }
            }
            if view {
                continue;
            }
            let views = match self.db.list_views(&ns) {
                Ok(n) => n,
                Err(db::Error::NoSuchNamespace) => continue,
                Err(e) => return Err(e.into()),
            };
            for v in views {
                let row = match self.db.view(&ns, &v) {
                    Ok(r) => r,
                    Err(db::Error::NoSuchView | db::Error::NoSuchNamespace) => continue,
                    Err(e) => return Err(e.into()),
                };
                if at(&row.metadata).is_some_and(|other| u.covers(&other)) {
                    return Ok(Some(format!("holds the location of view {dotted}.{v}")));
                }
            }
        }
        Ok(shared.map(|s| format!("is or holds {s}")))
    }

    /// May the table, view or namespace live at `location`, with these path properties? Outside Cairn Store only in
    /// a registered external location, and when it is created or moved there, with CREATE_TABLE on it. Wherever it
    /// is, a table or view created or moved there must have a place of its own (`own_place`), and a path in Cairn
    /// Store may not hold characters IAM would read as a pattern in the table's session policy (`table_policy`).
    async fn placement<'a>(
        &self,
        who: Option<&Principal>,
        levels: &[String],
        location: &str,
        props: impl IntoIterator<Item = (&'a String, &'a String)>,
        create: bool,
        what: Placed<'_>,
    ) -> Result<(), ApiError> {
        let mut paths = vec![location.to_string()];
        paths.extend(props.into_iter().filter(|(k, _)| crate::residency::PATH_PROPERTIES.contains(&k.as_str())).map(|(_, v)| v.clone()));
        for (i, url) in paths.into_iter().enumerate() {
            let place = self.place(&url)?;
            if !create {
                continue;
            }
            let external = match &place {
                Place::External(l, _) => Some(l),
                Place::Store => None,
            };
            match external {
                Some(l) if l.read_only => {
                    return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", format!("External location {} is read-only", l.name)));
                }
                None if url.contains(crate::external::S3_POLICY_CHARS) => {
                    return Err(bad_request(format!("{url}: a path in Cairn Store may not contain *, ? or $")));
                }
                _ => {}
            }
            if i == 0 {
                // A table's or view's own place: not another table's, nor around or inside one.
                let u = crate::external::parse(&url).map_err(bad_request)?;
                if let Some(why) = self.own_place(&u, levels, what)? {
                    return Err(err(StatusCode::CONFLICT, "AlreadyExistsException", format!("{url} {why}: tables do not share files")));
                }
            }
            if let (Some(l), Some(who)) = (external, who) {
                let t = match what {
                    Placed::Table(t) => Some(format!("{}.{t}", levels.join("."))),
                    _ => None,
                };
                self.location_event(who, l, Some("CREATE_TABLE"), "create_table", &url, t, true).await?;
            }
        }
        Ok(())
    }

    /// Asks Cairn about one use of a location (a creation, a vend), which it decides and records.
    #[allow(clippy::too_many_arguments)]
    async fn location_event(
        &self,
        who: &Principal,
        l: &crate::external::Location,
        privilege: Option<&str>,
        action: &str,
        url: &str,
        table: Option<String>,
        write: bool,
    ) -> Result<(), ApiError> {
        let event = json!({"principal": who.name, "user": who.user, "location": l.name, "privilege": privilege,
                           "action": action, "url": url, "table": table, "access": if write { "write" } else { "read" }});
        match self.cfg.external.decide(&event).await {
            crate::external::Asked::Allowed => Ok(()),
            crate::external::Asked::Refused(why) => {
                tracing::info!(principal = %who.name, location = %l.name, action, "refused by Cairn");
                Err(err(StatusCode::FORBIDDEN, "ForbiddenException", why))
            }
            crate::external::Asked::Unavailable(why) => {
                tracing::warn!(error = %why, location = %l.name, "a storage decision could not be made: refused");
                Err(err(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "ServiceUnavailableException",
                    "Cairn could not decide access to this location now",
                ))
            }
        }
    }

    /// The catalog's own metadata files, wherever their table lives.
    async fn put_file(&self, url: &str, body: Vec<u8>) -> Result<(), ApiError> {
        match self.place(url)? {
            Place::Store => Ok(self.store.put_new(url, body, "application/json").await?),
            Place::External(_, c) => Ok(self.cfg.external.put_new(&c, url, body).await?),
        }
    }

    async fn get_file(&self, url: &str) -> Result<Vec<u8>, ApiError> {
        match self.place(url)? {
            Place::Store => Ok(self.store.get(url).await?),
            Place::External(_, c) => Ok(self.cfg.external.get(&c, url).await?),
        }
    }

    async fn delete_file(&self, url: &str) -> Result<(), ApiError> {
        match self.place(url)? {
            Place::Store => Ok(self.store.delete(url).await?),
            Place::External(_, c) => Ok(self.cfg.external.delete(&c, url).await?),
        }
    }

    // ---- engines acting for people (token exchange) --------------------------------------------------------

    /// RFC 8693 token exchange: an engine allowed to act for people (`delegate`) exchanges its own token and a
    /// subject token naming the person (Trino's, with `iceberg.rest-catalog.session=USER`) for a token of the
    /// person's own, with which every table is decided by Cairn for them. A token given earlier is renewed the same way.
    fn exchange(&self, p: &Principal, body: &[u8]) -> Result<Resp, ApiError> {
        let form: BTreeMap<String, String> = String::from_utf8_lossy(body)
            .split('&')
            .filter(|x| !x.is_empty())
            .map(|x| {
                let (k, v) = x.split_once('=').unwrap_or((x, ""));
                (decode(&k.replace('+', " ")), decode(&v.replace('+', " ")))
            })
            .collect();
        let oauth = |code: &str, text: &str| json_resp(StatusCode::BAD_REQUEST, &json!({"error": code, "error_description": text}));
        if form.get("grant_type").map(String::as_str) != Some("urn:ietf:params:oauth:grant-type:token-exchange") {
            return Ok(oauth("unsupported_grant_type", "only token exchange (RFC 8693) by an engine that acts for people"));
        }
        let engine = if p.delegated {
            // Renewing a person's token: the engine it was given to is named in it.
            self.cfg.principals.iter().find(|x| p.name.ends_with(&format!(" via {}", x.name)) && x.delegate).cloned()
        } else if p.delegate {
            Some(p.clone())
        } else {
            None
        };
        let Some(engine) = engine else {
            return Err(err(StatusCode::FORBIDDEN, "ForbiddenException", "This token may not act for people"));
        };
        if self.cfg.identity.is_none() {
            return Err(err(
                StatusCode::FORBIDDEN,
                "ForbiddenException",
                "Acting for people needs Cairn's decisions (CAIRN_CATALOG_JWKS_URL)",
            ));
        }
        let subject = form.get("subject_token").map(String::as_str).unwrap_or("");
        let user = if subject.starts_with(DELEGATED) {
            match self.delegated(subject) {
                Ok(d) if d.name.ends_with(&format!(" via {}", engine.name)) => d.user,
                _ => None,
            }
        } else {
            jwt_subject(subject)
        };
        let Some(user) = user.filter(|u| !crate::identity::is_share(u)) else {
            return Ok(oauth("invalid_request", "subject_token must name a person"));
        };
        let exp = time::OffsetDateTime::now_utc().unix_timestamp() + delegated_seconds();
        use base64::Engine as _;
        let claims =
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(json!({"sub": user, "by": engine.name, "exp": exp}).to_string());
        let token = format!("{DELEGATED}{claims}.{}", mac_hex(engine.token.as_bytes(), &claims));
        tracing::info!(engine = %engine.name, user = %user, "a token for a person was given to an engine");
        let mut r = json_resp(
            StatusCode::OK,
            &json!({"access_token": token, "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "token_type": "Bearer", "expires_in": delegated_seconds()}),
        );
        r.headers_mut().insert("cache-control", "no-store".parse().unwrap());
        Ok(r)
    }

    /// A person's token given to an engine: checked with the engine's own token, which must still be configured.
    fn delegated(&self, token: &str) -> Result<Principal, ApiError> {
        use base64::Engine as _;
        let bad = || err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", "The token is not valid");
        let rest = token.strip_prefix(DELEGATED).ok_or_else(bad)?;
        let (claims, mac) = rest.split_once('.').ok_or_else(bad)?;
        let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(claims).map_err(|_| bad())?;
        let v: Value = serde_json::from_slice(&raw).map_err(|_| bad())?;
        let by = v["by"].as_str().ok_or_else(bad)?;
        let engine = self.cfg.principals.iter().find(|p| p.name == by && p.delegate).ok_or_else(bad)?;
        if !bool::from(mac_hex(engine.token.as_bytes(), claims).as_bytes().ct_eq(mac.as_bytes())) {
            return Err(bad());
        }
        if v["exp"].as_i64().is_none_or(|e| e < time::OffsetDateTime::now_utc().unix_timestamp()) {
            return Err(err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", "The token has expired"));
        }
        let user = v["sub"].as_str().ok_or_else(bad)?.to_string();
        Ok(Principal {
            token: String::new(),
            name: format!("user:{user} via {}", engine.name),
            namespaces: engine.namespaces.clone(),
            read_only: engine.read_only,
            user: Some(user),
            delegate: false,
            delegated: true,
        })
    }

    async fn scoped(&self, role_arn: &str, seconds: i64, location: &str, read_only: bool) -> Result<s3::TempCreds, ApiError> {
        if location.is_empty() {
            return Err(bad_request("The table has no location"));
        }
        let now = time::OffsetDateTime::now_utc().unix_timestamp();
        let key = (location.to_string(), read_only);
        if let Some(t) = self.vended.lock().unwrap().get(&key) {
            // Reuse while at least a quarter of its life (or 5 minutes) is left.
            if t.expires_unix - now > (seconds / 4).max(300) {
                return Ok(t.clone());
            }
        }
        let policy = table_policy(location, read_only).map_err(bad_request)?;
        let t = self
            .store
            .assume_role(role_arn, "cairn-catalog", &policy, seconds)
            .await
            .map_err(|e| err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", format!("Scoped credentials: {e}")))?;
        let mut cache = self.vended.lock().unwrap();
        cache.retain(|_, v| v.expires_unix > now);
        cache.insert(key, t.clone());
        Ok(t)
    }

    /// A namespace's location: its region's (when pinned), else its place under the warehouse.
    fn namespace_location(&self, levels: &[String]) -> String {
        if let Ok(Some(pin)) = self.cfg.residency.pin(levels) {
            return pin.base;
        }
        format!("{}/{}", self.cfg.warehouse.trim_end_matches('/'), levels.join("/"))
    }

    fn default_location(&self, levels: &[String], name: &str) -> String {
        let suffix = &uuid::Uuid::new_v4().simple().to_string()[..8];
        let pinned = matches!(self.cfg.residency.pin(levels), Ok(Some(_)));
        let base = match self.db.namespace(levels).ok().and_then(|p| p.get("location").cloned()) {
            Some(l) if !pinned => l.trim_end_matches('/').to_string(),
            _ => self.namespace_location(levels),
        };
        format!("{base}/{name}-{suffix}")
    }

    /// Data residency: may a table of the namespace live at `location`, with these properties?
    fn resident<'a>(
        &self,
        levels: &[String],
        location: &str,
        props: impl IntoIterator<Item = (&'a String, &'a String)>,
    ) -> Result<(), ApiError> {
        let r = &self.cfg.residency;
        if !r.active() {
            return Ok(());
        }
        let refuse = |m: String| err(StatusCode::FORBIDDEN, "ForbiddenException", m);
        r.check_location(levels, location).map_err(refuse)?;
        for (k, v) in props {
            if crate::residency::PATH_PROPERTIES.contains(&k.as_str()) {
                r.check_location(levels, v).map_err(refuse)?;
            }
        }
        Ok(())
    }

    /// Data residency: may a table move with its files between the two namespaces (a clone, a rename)?
    fn resident_move(&self, from: &[String], to: &[String]) -> Result<(), ApiError> {
        self.cfg.residency.check_move(from, to).map_err(|m| err(StatusCode::FORBIDDEN, "ForbiddenException", m))
    }

    async fn create_table(&self, who: &Principal, levels: &[String], req: CreateTable) -> Result<Resp, ApiError> {
        if !valid_name(&req.name) {
            return Err(bad_request("Invalid table name"));
        }
        self.db.namespace(levels)?;
        if self.db.table(levels, &req.name).is_ok() {
            return Err(db::Error::TableExists.into());
        }
        not_a_clone(req.properties.keys())?;
        let location = req.location.clone().unwrap_or_else(|| self.default_location(levels, &req.name));
        self.resident(levels, &location, &req.properties)?;
        self.placement(Some(who), levels, &location, &req.properties, true, Placed::Table(&req.name)).await?;
        let mut properties = req.properties.clone();
        let format_version = match properties.remove("format-version").as_deref() {
            None | Some("2") => iceberg::spec::FormatVersion::V2,
            Some("1") => iceberg::spec::FormatVersion::V1,
            Some("3") => iceberg::spec::FormatVersion::V3,
            Some(v) => return Err(bad_request(format!("Unsupported format-version {v}"))),
        };
        let creation = TableCreation::builder()
            .name(req.name.clone())
            .location(location.clone())
            .schema(req.schema)
            .partition_spec_opt(req.partition_spec)
            .sort_order_opt(req.write_order)
            .properties(properties)
            .format_version(format_version)
            .build();
        let metadata = TableMetadataBuilder::from_table_creation(creation).and_then(|b| b.build()).map_err(iceberg_err)?.metadata;
        let json = serde_json::to_string(&metadata).map_err(|e| bad_request(e.to_string()))?;
        if req.stage_create {
            self.db.stage(levels, &req.name, &json)?;
            let (config, creds) = self.client_config(who, &location, false, Some((levels, &req.name))).await?;
            let mut out = json!({"metadata-location": null, "metadata": serde_json::from_str::<Value>(&json).unwrap(), "config": config});
            if let Some(c) = creds {
                out["storage-credentials"] = c;
            }
            return Ok(json_resp(StatusCode::OK, &out));
        }
        let loc = MetadataLocation::try_new_with_metadata(&metadata).map_err(iceberg_err)?.to_string();
        self.put_file(&loc, json.as_bytes().to_vec()).await?;
        let change = Change {
            namespace: db::ns_key(levels),
            name: req.name.clone(),
            expected: None,
            new: TableRow { metadata_location: loc.clone(), metadata: json.clone() },
        };
        self.apply(vec![change]).await?;
        self.load_result(who, &loc, serde_json::from_str(&json).unwrap(), false, (levels, &req.name)).await
    }

    /// Writes a view's metadata file under its location and returns the file's location.
    async fn write_view_metadata(&self, metadata: &iceberg::spec::ViewMetadata) -> Result<(String, String), ApiError> {
        let json = serde_json::to_string(metadata).map_err(|e| bad_request(e.to_string()))?;
        let n = metadata.history().len();
        let loc = format!("{}/metadata/{n:05}-{}.metadata.json", metadata.location().trim_end_matches('/'), uuid::Uuid::new_v4());
        self.put_file(&loc, json.as_bytes().to_vec()).await?;
        Ok((loc, json))
    }

    async fn create_view(&self, who: &Principal, levels: &[String], req: CreateView) -> Result<Resp, ApiError> {
        if !valid_name(&req.name) {
            return Err(bad_request("Invalid view name"));
        }
        self.db.namespace(levels)?;
        let location = req.location.clone().unwrap_or_else(|| self.default_location(levels, &req.name));
        // The catalog writes the view's metadata there: in an external location, as for a table (CREATE_TABLE, and
        // never in a read-only one).
        self.placement(Some(who), levels, &location, std::iter::empty(), true, Placed::View(&req.name)).await?;
        let metadata = iceberg::spec::ViewMetadataBuilder::new(
            location,
            req.schema,
            req.view_version,
            iceberg::spec::ViewFormatVersion::V1,
            req.properties,
        )
        .and_then(|b| b.build())
        .map_err(iceberg_err)?
        .metadata;
        let (loc, json) = self.write_view_metadata(&metadata).await?;
        let row = TableRow { metadata_location: loc.clone(), metadata: json.clone() };
        if let Err(e) = self.db.commit_view(levels, &req.name, None, &row) {
            let _ = self.delete_file(&loc).await;
            return Err(e.into());
        }
        Ok(json_resp(
            StatusCode::OK,
            &json!({"metadata-location": loc, "metadata": serde_json::from_str::<Value>(&json).unwrap(), "config": {}}),
        ))
    }

    async fn replace_view(&self, who: &Principal, levels: &[String], name: &str, req: CommitView) -> Result<Resp, ApiError> {
        if let Some(id) = &req.identifier {
            if id.namespace != levels || id.name != name {
                return Err(bad_request("The identifier in the body differs from the path"));
            }
        }
        let row = self.db.view(levels, name)?;
        let current: iceberg::spec::ViewMetadata = serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?;
        for r in &req.requirements {
            match r["type"].as_str() {
                Some("assert-view-uuid") => {
                    if r["uuid"].as_str() != Some(current.uuid().to_string().as_str()) {
                        return Err(err(StatusCode::CONFLICT, "CommitFailedException", "The view's UUID changed"));
                    }
                }
                other => return Err(bad_request(format!("Unknown view requirement {other:?}"))),
            }
        }
        let before = current.location().to_string();
        let mut builder = iceberg::spec::ViewMetadataBuilder::new_from_metadata(current);
        for u in req.updates {
            builder = apply_view_update(builder, u).map_err(iceberg_err)?;
        }
        let metadata = builder.build().map_err(iceberg_err)?.metadata;
        let moved = metadata.location() != before;
        self.placement(Some(who), levels, metadata.location(), std::iter::empty(), moved, Placed::View(name)).await?;
        let (loc, json) = self.write_view_metadata(&metadata).await?;
        let new = TableRow { metadata_location: loc.clone(), metadata: json.clone() };
        if let Err(e) = self.db.commit_view(levels, name, Some(&row.metadata_location), &new) {
            let _ = self.delete_file(&loc).await;
            return Err(e.into());
        }
        Ok(json_resp(
            StatusCode::OK,
            &json!({"metadata-location": loc, "metadata": serde_json::from_str::<Value>(&json).unwrap(), "config": {}}),
        ))
    }

    /// A new table on the same data files (no data is copied): its own location for new writes, and the
    /// source's snapshots, which both tables then keep until the clone is dropped.
    async fn clone_table(
        &self,
        who: &Principal,
        levels: &[String],
        name: &str,
        target: &[String],
        new_name: &str,
    ) -> Result<Resp, ApiError> {
        if !valid_name(new_name) {
            return Err(bad_request("Invalid table name"));
        }
        self.db.namespace(target)?;
        if self.db.table(target, new_name).is_ok() {
            return Err(db::Error::TableExists.into());
        }
        self.resident_move(levels, target)?;
        let location = self.default_location(target, new_name);
        // The clone writes under its own location only: the source's write paths (write.data.path, …) are the
        // source's place, where the source's orphan-file cleanup would delete the clone's files.
        let mut dropped: Vec<String> = crate::residency::PATH_PROPERTIES.iter().map(|k| k.to_string()).collect();
        dropped.push(CLONES.to_string());
        let own_props = |md: &TableMetadata| -> HashMap<String, String> {
            md.properties().iter().filter(|(k, _)| !dropped.contains(k)).map(|(k, v)| (k.clone(), v.clone())).collect()
        };
        {
            let src = self.db.table(levels, name)?;
            let md: TableMetadata = serde_json::from_str(&src.metadata).map_err(|e| bad_request(e.to_string()))?;
            let own = own_props(&md);
            self.resident(target, &location, &own)?;
            self.placement(Some(who), target, &location, &own, true, Placed::Table(new_name)).await?;
        }
        // The clone and the source's record of it are made from one read of the source, committed only if the source
        // has not changed since: from a stale read, the record would lose another clone's entry, or the clone would
        // take snapshots the source has just expired.
        for attempt in 0..CLONE_RETRIES {
            if attempt > 0 {
                clone_backoff(attempt).await;
            }
            let src = self.db.table(levels, name)?;
            let md: TableMetadata = serde_json::from_str(&src.metadata).map_err(|e| bad_request(e.to_string()))?;
            let ids: Vec<i64> = md.snapshots().map(|s| s.snapshot_id()).collect();
            let mut props = HashMap::new();
            props.insert(CLONE_SOURCE.to_string(), format!("{}.{}", levels.join("."), name));
            props.insert(CLONE_INHERITED.to_string(), ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(","));
            let metadata = md
                .clone()
                .into_builder(None)
                .assign_uuid(uuid::Uuid::new_v4())
                .set_location(location.clone())
                .remove_properties(&dropped)
                .map_err(iceberg_err)?
                .set_properties(props)
                .map_err(iceberg_err)?
                .build()
                .map_err(iceberg_err)?
                .metadata;
            let clone_loc = MetadataLocation::try_new_with_metadata(&metadata).map_err(iceberg_err)?.to_string();
            let clone_json = serde_json::to_string(&metadata).map_err(|e| bad_request(e.to_string()))?;
            self.put_file(&clone_loc, clone_json.as_bytes().to_vec()).await?;
            // The source records what the clone shares, in the same commit.
            let mut clones: serde_json::Map<String, Value> =
                md.properties().get(CLONES).and_then(|v| serde_json::from_str(v).ok()).unwrap_or_default();
            clones.insert(format!("{}.{}", target.join("."), new_name), json!(ids));
            let mut upd = HashMap::new();
            upd.insert(CLONES.to_string(), Value::Object(clones).to_string());
            let src_change = match self.prepare(levels, name, vec![], vec![TableUpdate::SetProperties { updates: upd }], None).await {
                Ok(p) if p.change.expected.as_deref() == Some(src.metadata_location.as_str()) => p.change,
                Ok(p) => {
                    let _ = self.delete_file(&p.change.new.metadata_location).await;
                    let _ = self.delete_file(&clone_loc).await;
                    continue;
                }
                Err(e) => {
                    let _ = self.delete_file(&clone_loc).await;
                    return Err(e);
                }
            };
            let clone_change = Change {
                namespace: db::ns_key(target),
                name: new_name.to_string(),
                expected: None,
                new: TableRow { metadata_location: clone_loc.clone(), metadata: clone_json.clone() },
            };
            match self.apply(vec![src_change, clone_change]).await {
                Ok(()) => {}
                Err(e) if e.kind == "CommitFailedException" && attempt + 1 < CLONE_RETRIES => continue,
                Err(e) => return Err(e),
            }
            tracing::info!(source = %format!("{}.{}", levels.join("."), name), clone = %format!("{}.{}", target.join("."), new_name), "table cloned");
            return self.load_result(who, &clone_loc, serde_json::from_str(&clone_json).unwrap(), false, (target, new_name)).await;
        }
        Err(err(StatusCode::CONFLICT, "CommitFailedException", "The source kept changing while it was cloned: try again"))
    }

    fn remember_drop(&self, levels: &[String], name: &str, metadata_location: &str, by: &str) -> Result<(), ApiError> {
        let props = self.db.namespace(levels)?;
        let cutoff = now_ms() - undrop_days() * 86_400_000;
        let stale: Vec<String> = props
            .iter()
            .filter(|(k, v)| {
                k.starts_with(DROPPED) && serde_json::from_str::<Value>(v).ok().and_then(|r| r["dropped-ms"].as_i64()).unwrap_or(0) < cutoff
            })
            .map(|(k, _)| k.clone())
            .collect();
        let mut add = HashMap::new();
        add.insert(
            format!("{DROPPED}{name}"),
            json!({"metadata-location": metadata_location, "dropped-ms": now_ms(), "by": by}).to_string(),
        );
        let stale: Vec<String> = stale.into_iter().filter(|k| !add.contains_key(k)).collect();
        self.db.update_namespace(levels, &stale, &add.into_iter().collect())?;
        Ok(())
    }

    /// For a clone, the clones of its own that still exist ("<namespace>.<name>"). Their files include files of the
    /// clone's source, which the source keeps only while it holds them for this clone.
    fn clones_of_a_clone(&self, metadata: &str) -> Vec<String> {
        let Ok(md) = serde_json::from_str::<TableMetadata>(metadata) else { return vec![] };
        if !md.properties().contains_key(CLONE_SOURCE) {
            return vec![];
        }
        let clones: serde_json::Map<String, Value> =
            md.properties().get(CLONES).and_then(|v| serde_json::from_str(v).ok()).unwrap_or_default();
        clones
            .keys()
            .filter(|k| {
                k.rsplit_once('.').is_some_and(|(ns, n)| {
                    let levels: Vec<String> = ns.split('.').map(String::from).collect();
                    self.db.table(&levels, n).is_ok()
                })
            })
            .cloned()
            .collect()
    }

    /// Sets (`Some(ids)`) or removes (`None`) one clone's entry in its source's record of its clones, from the
    /// source as it is now, after `check` accepts it. Committed only if the source has not changed since it was read
    /// (else read again): a record rewritten from a stale read would lose another clone's entry.
    async fn set_clone_record(
        &self,
        slevels: &[String],
        sname: &str,
        key: &str,
        ids: Option<&[i64]>,
        check: impl Fn(&TableMetadata) -> Result<(), ApiError>,
    ) -> Result<(), ApiError> {
        for attempt in 0..CLONE_RETRIES {
            if attempt > 0 {
                clone_backoff(attempt).await;
            }
            let srow = self.db.table(slevels, sname)?;
            let smd: TableMetadata = serde_json::from_str(&srow.metadata).map_err(|e| bad_request(e.to_string()))?;
            check(&smd)?;
            let mut clones: serde_json::Map<String, Value> =
                smd.properties().get(CLONES).and_then(|v| serde_json::from_str(v).ok()).unwrap_or_default();
            match ids {
                Some(ids) => {
                    clones.insert(key.to_string(), json!(ids));
                }
                None if clones.remove(key).is_none() => return Ok(()),
                None => {}
            }
            let update = if clones.is_empty() {
                TableUpdate::RemoveProperties { removals: vec![CLONES.to_string()] }
            } else {
                let mut m = HashMap::new();
                m.insert(CLONES.to_string(), Value::Object(clones).to_string());
                TableUpdate::SetProperties { updates: m }
            };
            let p = self.prepare(slevels, sname, vec![], vec![update], None).await?;
            if p.change.expected.as_deref() != Some(srow.metadata_location.as_str()) {
                let _ = self.delete_file(&p.change.new.metadata_location).await;
                continue;
            }
            match self.apply(vec![p.change]).await {
                Ok(()) => return Ok(()),
                Err(e) if e.kind == "CommitFailedException" && attempt + 1 < CLONE_RETRIES => continue,
                Err(e) => return Err(e),
            }
        }
        Err(err(StatusCode::CONFLICT, "CommitFailedException", "The clone's source kept changing: try again"))
    }

    /// A dropped clone no longer holds its source's snapshots.
    async fn forget_clone(&self, levels: &[String], name: &str, row: &TableRow) {
        let Ok(md) = serde_json::from_str::<TableMetadata>(&row.metadata) else { return };
        let Some(source) = md.properties().get(CLONE_SOURCE).cloned() else { return };
        let Some((sns, sname)) = source.rsplit_once('.') else { return };
        let slevels: Vec<String> = sns.split('.').map(String::from).collect();
        if self.db.table(&slevels, sname).is_err() {
            return;
        }
        let key = format!("{}.{}", levels.join("."), name);
        if let Err(e) = self.set_clone_record(&slevels, sname, &key, None, |_| Ok(())).await {
            tracing::warn!(source = %source, error = %e.message, "a dropped clone's source kept its record");
        }
    }

    /// Brings a dropped table back (within the retention), as it was when dropped.
    async fn undrop(&self, who: &Principal, levels: &[String], name: &str) -> Result<Resp, ApiError> {
        let props = self.db.namespace(levels)?;
        let key = format!("{DROPPED}{name}");
        let rec: Value = props
            .get(&key)
            .and_then(|v| serde_json::from_str(v).ok())
            .ok_or_else(|| err(StatusCode::NOT_FOUND, "NoSuchTableException", "No dropped table by that name"))?;
        if rec["dropped-ms"].as_i64().unwrap_or(0) < now_ms() - undrop_days() * 86_400_000 {
            return Err(err(StatusCode::NOT_FOUND, "NoSuchTableException", "Dropped longer ago than the catalog keeps tables"));
        }
        if self.db.table(levels, name).is_ok() {
            return Err(db::Error::TableExists.into());
        }
        let location = rec["metadata-location"].as_str().unwrap_or("").to_string();
        let bytes = self.get_file(&location).await?;
        let md: TableMetadata = serde_json::from_slice(&bytes).map_err(|e| bad_request(format!("Not table metadata: {e}")))?;
        // A clone comes back only if its source still holds every snapshot they share: once the source has
        // expired one, the data files the clone reads may be gone.
        if let (Some(source), Some(inh)) = (md.properties().get(CLONE_SOURCE), md.properties().get(CLONE_INHERITED)) {
            let gone = || {
                err(
                    StatusCode::CONFLICT,
                    "CommitFailedException",
                    format!("This clone's source {source} no longer holds the snapshots it shares, so its data may be gone"),
                )
            };
            let (sns, sname) = source.rsplit_once('.').ok_or_else(gone)?;
            let slevels: Vec<String> = sns.split('.').map(String::from).collect();
            self.db.table(&slevels, sname).map_err(|_| gone())?;
            let ids: Vec<i64> = inh.split(',').filter_map(|x| x.parse().ok()).collect();
            // The source holds the shared snapshots again before the clone is back.
            let key = format!("{}.{}", levels.join("."), name);
            self.set_clone_record(&slevels, sname, &key, Some(&ids), |smd| {
                if ids.iter().any(|id| smd.snapshot_by_id(*id).is_none()) {
                    return Err(gone());
                }
                Ok(())
            })
            .await?;
        }
        let resp = self.register(Some(who), levels, Register { name: name.to_string(), metadata_location: location }, false).await?;
        self.db.update_namespace(levels, &[key], &BTreeMap::new())?;
        tracing::info!(table = %format!("{}.{}", levels.join("."), name), "table undropped");
        Ok(resp)
    }

    /// `client`: registered through the API, not brought back by undrop. A clone's metadata is then registered only with
    /// one of the catalog's own unrestricted tokens (disaster recovery registers tables again as they were).
    async fn register(&self, who: Option<&Principal>, levels: &[String], req: Register, client: bool) -> Result<Resp, ApiError> {
        if !valid_name(&req.name) {
            return Err(bad_request("Invalid table name"));
        }
        self.db.namespace(levels)?;
        // Where the metadata file is must be a place the catalog may use before it is read.
        self.placement(who, levels, &req.metadata_location, std::iter::empty(), true, Placed::Table(&req.name)).await?;
        let bytes = self.get_file(&req.metadata_location).await?;
        let metadata: TableMetadata = serde_json::from_slice(&bytes).map_err(|e| bad_request(format!("Not table metadata: {e}")))?;
        if client && who.is_none_or(|p| p.user.is_some() || !p.namespaces.is_empty() || p.read_only) {
            not_a_clone(metadata.properties().keys())?;
        }
        self.resident(levels, metadata.location(), metadata.properties())?;
        self.resident(levels, &req.metadata_location, std::iter::empty())?;
        self.placement(who, levels, metadata.location(), metadata.properties(), true, Placed::Table(&req.name)).await?;
        let json = serde_json::to_string(&metadata).map_err(|e| bad_request(e.to_string()))?;
        let change = Change {
            namespace: db::ns_key(levels),
            name: req.name.clone(),
            expected: None,
            new: TableRow { metadata_location: req.metadata_location.clone(), metadata: json.clone() },
        };
        self.db.commit(std::slice::from_ref(&change))?;
        self.changed(std::slice::from_ref(&change));
        let name = req.name.clone();
        let who = who.cloned().unwrap_or_else(|| self.cfg.principals[0].clone());
        self.load_result(&who, &req.metadata_location, serde_json::from_str(&json).unwrap(), false, (levels, &name)).await
    }

    /// Checks requirements against the current metadata, applies the updates and writes the new
    /// metadata file. Nothing is visible until `apply` swaps the table's pointer.
    async fn prepare(
        &self,
        levels: &[String],
        name: &str,
        requirements: Vec<TableRequirement>,
        updates: Vec<TableUpdate>,
        who: Option<&Principal>,
    ) -> Result<Prepared, ApiError> {
        let client = who.is_some();
        let current = match self.db.table(levels, name) {
            Ok(row) => Some(row),
            Err(db::Error::NoSuchTable) => None,
            Err(e) => return Err(e.into()),
        };
        let mut before: Option<(String, Vec<Option<String>>)> = None;
        let (builder, base_location) = match &current {
            Some(row) => {
                let md: TableMetadata = serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?;
                before = Some(paths_of(&md));
                for r in &requirements {
                    r.check(Some(&md)).map_err(iceberg_err)?;
                }
                if client {
                    guard(&md, &updates)?;
                }
                (md.into_builder(Some(row.metadata_location.clone())), Some(row.metadata_location.clone()))
            }
            None => {
                // Creating through a commit: only for a staged create, asserted with assert-create.
                if !requirements.iter().any(|r| matches!(r, TableRequirement::NotExist)) {
                    return Err(db::Error::NoSuchTable.into());
                }
                for r in &requirements {
                    r.check(None).map_err(iceberg_err)?;
                }
                let staged = self.db.staged(levels, name)?.ok_or(db::Error::NoSuchTable)?;
                let md: TableMetadata = serde_json::from_str(&staged).map_err(|e| bad_request(e.to_string()))?;
                if client {
                    for u in &updates {
                        if let TableUpdate::SetProperties { updates } = u {
                            not_a_clone(updates.keys())?;
                        }
                    }
                }
                (md.into_builder(None), None)
            }
        };
        let mut builder = builder;
        for u in updates {
            builder = u.apply(builder).map_err(iceberg_err)?;
        }
        let metadata = builder.build().map_err(iceberg_err)?.metadata;
        self.resident(levels, metadata.location(), metadata.properties())?;
        // Moving a table, or where it writes, needs the same as creating it there.
        let moved = before.as_ref() != Some(&paths_of(&metadata));
        self.placement(who, levels, metadata.location(), metadata.properties(), moved, Placed::Table(name)).await?;
        let location = match base_location.as_deref().map(MetadataLocation::from_str) {
            Some(Ok(cur)) => cur.with_next_version().try_with_new_metadata(&metadata).map_err(iceberg_err)?,
            _ => MetadataLocation::try_new_with_metadata(&metadata).map_err(iceberg_err)?,
        }
        .to_string();
        let json = serde_json::to_string(&metadata).map_err(|e| bad_request(e.to_string()))?;
        self.put_file(&location, json.as_bytes().to_vec()).await?;
        Ok(Prepared {
            change: Change {
                namespace: db::ns_key(levels),
                name: name.to_string(),
                expected: base_location,
                new: TableRow { metadata_location: location, metadata: json.clone() },
            },
            metadata: serde_json::from_str(&json).unwrap(),
        })
    }

    /// Tells Cairn which tables changed (when Cairn decides access, so it is there to tell).
    fn changed(&self, changes: &[Change]) {
        if let Some(id) = &self.cfg.identity {
            id.changed(changes.iter().map(|c| (db::ns_levels(&c.namespace), c.name.clone())).collect());
        }
    }

    /// Swaps the table pointers (all or none). On failure the metadata files written for this commit
    /// are removed, since nothing will ever refer to them.
    async fn apply(&self, changes: Vec<Change>) -> Result<(), ApiError> {
        match self.db.commit(&changes) {
            Ok(()) => {
                self.changed(&changes);
                Ok(())
            }
            Err(e) => {
                for c in &changes {
                    let _ = self.delete_file(&c.new.metadata_location).await;
                }
                Err(e.into())
            }
        }
    }
}

/// A table's location and the path properties that move where it writes.
fn paths_of(md: &TableMetadata) -> (String, Vec<Option<String>>) {
    (md.location().to_string(), crate::residency::PATH_PROPERTIES.iter().map(|k| md.properties().get(*k).cloned()).collect())
}

fn unavailable(e: String) -> ApiError {
    tracing::warn!(error = %e, "external storage");
    err(StatusCode::SERVICE_UNAVAILABLE, "ServiceUnavailableException", format!("External storage: {e}"))
}

/// A table looked up for loading: a missing namespace is reported as a missing table, as Iceberg's own catalogs do.
/// Engines name a metadata table as `<namespace>.<table>.<metadata table>` (Spark's `t.snapshots`, `t.delete_files`)
/// and try it as a table first: only "no such table" makes them read the metadata table instead.
fn table_in(db: &db::Db, levels: &[String], name: &str) -> Result<db::TableRow, db::Error> {
    match db.table(levels, name) {
        Err(db::Error::NoSuchNamespace) => Err(db::Error::NoSuchTable),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WAREHOUSE: &str = "s3://lakehouse/warehouse";

    fn lv(s: &str) -> Vec<String> {
        s.split('.').map(str::to_string).collect()
    }

    /// A catalog on an embedded database, with Cairn Store only (no external locations, no residency): placement
    /// itself touches no storage.
    fn catalog(dir: &std::path::Path) -> Catalog {
        let external = crate::external::External::new(
            crate::external::Source::Off,
            vec![crate::external::parse(WAREHOUSE).unwrap()],
            None,
            std::time::Duration::from_secs(30),
            crate::external::Limits::default(),
        )
        .unwrap();
        let s3 = s3::S3Config {
            endpoint: "http://127.0.0.1:1".into(),
            region: "us-east-1".into(),
            access_key: "k".into(),
            secret_key: "s".into(),
        };
        Catalog::new(
            Db::open(dir, false).unwrap(),
            Config {
                warehouse: WAREHOUSE.into(),
                principals: vec![],
                s3,
                vend: Vend::Off,
                client_endpoint: String::new(),
                identity: None,
                residency: Default::default(),
                external: Arc::new(external),
            },
        )
    }

    fn table(c: &Catalog, ns: &str, name: &str, location: &str) {
        let md = json!({"location": location, "format-version": 2}).to_string();
        let new = TableRow { metadata_location: format!("{location}/metadata/00000.metadata.json"), metadata: md };
        c.db.commit(&[Change { namespace: db::ns_key(&lv(ns)), name: name.into(), expected: None, new }]).unwrap();
    }

    async fn place(c: &Catalog, ns: &str, location: &str, create: bool, what: Placed<'_>) -> Result<(), StatusCode> {
        c.placement(None, &lv(ns), location, std::iter::empty(), create, what).await.map_err(|e| e.status)
    }

    #[test]
    fn a_table_policy_is_never_a_pattern() {
        assert!(table_policy("s3://lakehouse/warehouse/sales/orders-1", false).is_ok());
        // Listing only under the table's own directory: the bare prefix would also list sales/orders-1x/...
        let p: Value = serde_json::from_str(&table_policy("s3://lakehouse/warehouse/sales/orders-1/", true).unwrap()).unwrap();
        assert_eq!(p["Statement"][1]["Condition"]["StringLike"]["s3:prefix"], json!(["warehouse/sales/orders-1/*"]));
        assert_eq!(p["Statement"][0]["Resource"], "arn:aws:s3:::lakehouse/warehouse/sales/orders-1/*");
        for l in ["s3://lakehouse/warehouse/sales/orders*", "s3://lakehouse/warehouse/sales/o?", "s3://lakehouse/w/${aws:username}"] {
            assert!(table_policy(l, true).is_err(), "{l}");
        }
    }

    #[tokio::test]
    async fn a_store_table_has_a_place_of_its_own() {
        let dir = tempfile::tempdir().unwrap();
        let c = catalog(dir.path());
        for ns in ["sales", "team"] {
            c.db.create_namespace(&lv(ns), BTreeMap::new()).unwrap();
        }
        let orders = format!("{WAREHOUSE}/sales/orders-1");
        table(&c, "sales", "orders", &orders);
        let view = format!("{WAREHOUSE}/team/v-1");
        let row = TableRow { metadata_location: format!("{view}/metadata/1.json"), metadata: json!({"location": view}).to_string() };
        c.db.commit_view(&lv("team"), "v", None, &row).unwrap();
        let t = Placed::Table("grab");
        // The bucket, the warehouse, a namespace's place, another table's, around or inside it, around a view.
        for l in [
            "s3://lakehouse".to_string(),
            WAREHOUSE.into(),
            format!("{WAREHOUSE}/"),
            format!("{WAREHOUSE}/sales"),
            format!("{WAREHOUSE}/team"),
            "s3a://lakehouse/warehouse/sales//".into(),
            orders.clone(),
            format!("{orders}/"),
            format!("{orders}/data"),
            view.clone(),
        ] {
            assert_eq!(place(&c, "team", &l, true, t).await, Err(StatusCode::CONFLICT), "{l}");
        }
        // Paths IAM would read as patterns.
        for l in [format!("{WAREHOUSE}/sales/orders*"), format!("{WAREHOUSE}/sales/orders-?"), format!("{WAREHOUSE}/team/${{x}}")] {
            assert_eq!(place(&c, "team", &l, true, t).await, Err(StatusCode::BAD_REQUEST), "{l}");
        }
        // Its default place, a sibling that only starts like another table, and the table's own place (a commit).
        assert_eq!(place(&c, "team", &format!("{WAREHOUSE}/team/grab-2"), true, t).await, Ok(()));
        assert_eq!(place(&c, "team", &format!("{orders}x"), true, t).await, Ok(()));
        assert_eq!(place(&c, "sales", &orders, true, Placed::Table("orders")).await, Ok(()));
        // A view: not in a table's files; anywhere else as before (also at a namespace's place).
        assert_eq!(place(&c, "team", &format!("{orders}/v"), true, Placed::View("w")).await, Err(StatusCode::CONFLICT));
        assert_eq!(place(&c, "team", &format!("{WAREHOUSE}/team"), true, Placed::View("w")).await, Ok(()));
        // A namespace's place is not a table's.
        assert_eq!(place(&c, "team", &format!("{WAREHOUSE}/team2"), true, Placed::Namespace).await, Ok(()));
    }

    #[tokio::test]
    async fn tables_already_overlapping_stay_usable_but_nothing_new_goes_there() {
        let dir = tempfile::tempdir().unwrap();
        let c = catalog(dir.path());
        c.db.create_namespace(&lv("sales"), BTreeMap::new()).unwrap();
        // As an earlier catalog could leave them: one table around another, and one with a pattern in its path.
        let wide = format!("{WAREHOUSE}/sales/wide");
        table(&c, "sales", "wide", &wide);
        table(&c, "sales", "inner", &format!("{wide}/inner"));
        let star = format!("{WAREHOUSE}/sales/s*");
        table(&c, "sales", "star", &star);
        // Commits that leave their location as it is (data, properties) are not placements: allowed.
        assert_eq!(place(&c, "sales", &wide, false, Placed::Table("wide")).await, Ok(()));
        assert_eq!(place(&c, "sales", &format!("{wide}/inner"), false, Placed::Table("inner")).await, Ok(()));
        assert_eq!(place(&c, "sales", &star, false, Placed::Table("star")).await, Ok(()));
        // Their keys are as before for the overlapping ones; a pattern gets none (it would reach other tables).
        assert!(table_policy(&wide, true).is_ok());
        assert!(table_policy(&star, true).is_err());
        // Nothing new inside or around them.
        assert_eq!(place(&c, "sales", &format!("{wide}/other"), true, Placed::Table("other")).await, Err(StatusCode::CONFLICT));
        assert_eq!(place(&c, "sales", &format!("{wide}/v"), true, Placed::View("v")).await, Err(StatusCode::CONFLICT));
        // A new table at its default place beside them works.
        assert_eq!(place(&c, "sales", &format!("{WAREHOUSE}/sales/t-1"), true, Placed::Table("t")).await, Ok(()));
    }
}
