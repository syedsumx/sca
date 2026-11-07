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

/// The session policy for a table's location: its objects, and listing under it.
pub fn table_policy(location: &str, read_only: bool) -> Result<String, String> {
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
         "Condition": {"StringLike": {"s3:prefix": [format!("{prefix}/*"), prefix.to_string()]}}}]})
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
                    })
                }
                Ok(user) => Ok(Principal {
                    token: String::new(),
                    name: format!("user:{user}"),
                    namespaces: vec![],
                    read_only: false,
                    user: Some(user),
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
        if need == Need::Read && d.restricted {
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
            (Method::POST, ["v1", "oauth", "tokens"]) => {
                Err(bad_request("This catalog takes bearer tokens; the OAuth token endpoint is not supported"))
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
                self.need_schemas(&principal).await?;
                if let Some(l) = req.properties.get("location") {
                    self.resident(&req.namespace, l, std::iter::empty())?;
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
                self.create_table(&levels, req).await
            }
            (Method::POST, ["v1", "namespaces", ns, "register"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let req: Register = parse(&body)?;
                self.need(&principal, &levels, &req.name, Need::Manage).await?;
                self.register(&levels, req).await
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
                if !matches!(self.cfg.vend, Vend::Scoped { .. }) {
                    return Err(err(StatusCode::NOT_FOUND, "NotFoundException", "This catalog vends no renewable credentials"));
                }
                let config = self.client_config(&location, principal.read_only || !d.write).await?;
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
                self.clone_table(&levels, &name, &target, &req.name).await
            }
            (Method::POST, ["v1", "namespaces", ns, "tables", t, "undrop"]) => {
                let (levels, name) = (ns_param(ns), decode(t));
                scope(&levels)?;
                self.need(&principal, &levels, &name, Need::Manage).await?;
                self.undrop(&levels, &name).await
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
                        let row = self.db.table(&levels, &name)?;
                        self.load_result(
                            &row.metadata_location,
                            serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?,
                            principal.read_only || !d.write,
                            (&levels, &name),
                        )
                        .await
                    }
                    Method::HEAD => {
                        self.need(&principal, &levels, &name, Need::View).await?;
                        self.db.table(&levels, &name)?;
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
                        let p = self.prepare(&levels, &name, req.requirements, req.updates, true).await?;
                        let location = p.change.new.metadata_location.clone();
                        self.apply(vec![p.change]).await?;
                        Ok(json_resp(StatusCode::OK, &json!({"metadata-location": location, "metadata": p.metadata})))
                    }
                    Method::DELETE => {
                        self.need(&principal, &levels, &name, Need::Manage).await?;
                        let purge = query.get("purgeRequested").is_some_and(|v| v.eq_ignore_ascii_case("true"));
                        let current = self.db.table(&levels, &name)?;
                        if !purge {
                            // Kept for undrop: the last metadata stays in the store.
                            self.remember_drop(&levels, &name, &current.metadata_location, &principal.name)?;
                        }
                        let row = self.db.drop_table(&levels, &name)?;
                        self.forget_clone(&levels, &name, &row).await;
                        if purge {
                            // Data files are left for the store's lifecycle tooling; the current metadata goes now.
                            let _ = self.store.delete(&row.metadata_location).await;
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
                self.create_view(&levels, req).await
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
                        self.replace_view(&levels, &name, parse(&body)?).await
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
                    prepared.push(self.prepare(&id.namespace, &id.name, c.requirements, c.updates, true).await?);
                }
                self.apply(prepared.into_iter().map(|p| p.change).collect()).await?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            (_, ["v1", ..]) => Err(err(StatusCode::NOT_FOUND, "NotFoundException", format!("No route for {method} {path}"))),
            _ => Err(err(StatusCode::NOT_FOUND, "NotFoundException", "Not found")),
        }
    }

    async fn load_result(&self, location: &str, metadata: Value, read_only: bool, ident: (&[String], &str)) -> Result<Resp, ApiError> {
        let table_location = metadata["location"].as_str().unwrap_or("").to_string();
        let mut config = self.client_config(&table_location, read_only).await?;
        let mut out = json!({"metadata-location": location, "metadata": metadata});
        if matches!(self.cfg.vend, Vend::Scoped { .. }) {
            // Where clients renew the keys (relative to the catalog's URI), before they expire. Both in the
            // table's config and in its storage credential: Iceberg builds a storage client per credential.
            let ns: Vec<String> = ident.0.iter().map(|l| encode(l)).collect();
            config.insert(
                "client.refresh-credentials-endpoint".into(),
                format!("v1/namespaces/{}/tables/{}/credentials", ns.join("%1F"), encode(ident.1)),
            );
            config.insert("client.refresh-credentials-enabled".into(), "true".into());
            out["storage-credentials"] = json!([{"prefix": table_location, "config": config}]);
        }
        out["config"] = json!(config);
        Ok(json_resp(StatusCode::OK, &out))
    }

    /// Storage settings for clients: the object store endpoint and, if vending is on, credentials
    /// (scoped: temporary keys for this table's location only).
    async fn client_config(&self, table_location: &str, read_only: bool) -> Result<BTreeMap<String, String>, ApiError> {
        let mut c = BTreeMap::new();
        let (ak, sk, token) = match &self.cfg.vend {
            Vend::Off => return Ok(c),
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
        Ok(c)
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

    async fn create_table(&self, levels: &[String], req: CreateTable) -> Result<Resp, ApiError> {
        if !valid_name(&req.name) {
            return Err(bad_request("Invalid table name"));
        }
        self.db.namespace(levels)?;
        if self.db.table(levels, &req.name).is_ok() {
            return Err(db::Error::TableExists.into());
        }
        let location = req.location.clone().unwrap_or_else(|| self.default_location(levels, &req.name));
        s3::parse_location(&location)?;
        self.resident(levels, &location, &req.properties)?;
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
            return Ok(json_resp(
                StatusCode::OK,
                &json!({"metadata-location": null, "metadata": serde_json::from_str::<Value>(&json).unwrap(),
                        "config": self.client_config(&location, false).await?}),
            ));
        }
        let loc = MetadataLocation::new_with_metadata(metadata.location(), &metadata).to_string();
        self.store.put_new(&loc, json.as_bytes().to_vec(), "application/json").await?;
        let change = Change {
            namespace: db::ns_key(levels),
            name: req.name.clone(),
            expected: None,
            new: TableRow { metadata_location: loc.clone(), metadata: json.clone() },
        };
        self.apply(vec![change]).await?;
        self.load_result(&loc, serde_json::from_str(&json).unwrap(), false, (levels, &req.name)).await
    }

    /// Writes a view's metadata file under its location and returns the file's location.
    async fn write_view_metadata(&self, metadata: &iceberg::spec::ViewMetadata) -> Result<(String, String), ApiError> {
        let json = serde_json::to_string(metadata).map_err(|e| bad_request(e.to_string()))?;
        let n = metadata.history().len();
        let loc = format!("{}/metadata/{n:05}-{}.metadata.json", metadata.location().trim_end_matches('/'), uuid::Uuid::new_v4());
        self.store.put_new(&loc, json.as_bytes().to_vec(), "application/json").await?;
        Ok((loc, json))
    }

    async fn create_view(&self, levels: &[String], req: CreateView) -> Result<Resp, ApiError> {
        if !valid_name(&req.name) {
            return Err(bad_request("Invalid view name"));
        }
        self.db.namespace(levels)?;
        let location = req.location.clone().unwrap_or_else(|| self.default_location(levels, &req.name));
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
            let _ = self.store.delete(&loc).await;
            return Err(e.into());
        }
        Ok(json_resp(
            StatusCode::OK,
            &json!({"metadata-location": loc, "metadata": serde_json::from_str::<Value>(&json).unwrap(), "config": {}}),
        ))
    }

    async fn replace_view(&self, levels: &[String], name: &str, req: CommitView) -> Result<Resp, ApiError> {
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
        let mut builder = iceberg::spec::ViewMetadataBuilder::new_from_metadata(current);
        for u in req.updates {
            builder = apply_view_update(builder, u).map_err(iceberg_err)?;
        }
        let metadata = builder.build().map_err(iceberg_err)?.metadata;
        let (loc, json) = self.write_view_metadata(&metadata).await?;
        let new = TableRow { metadata_location: loc.clone(), metadata: json.clone() };
        if let Err(e) = self.db.commit_view(levels, name, Some(&row.metadata_location), &new) {
            let _ = self.store.delete(&loc).await;
            return Err(e.into());
        }
        Ok(json_resp(
            StatusCode::OK,
            &json!({"metadata-location": loc, "metadata": serde_json::from_str::<Value>(&json).unwrap(), "config": {}}),
        ))
    }

    /// A new table on the same data files (no data is copied): its own location for new writes, and the
    /// source's snapshots, which both tables then keep until the clone is dropped.
    async fn clone_table(&self, levels: &[String], name: &str, target: &[String], new_name: &str) -> Result<Resp, ApiError> {
        if !valid_name(new_name) {
            return Err(bad_request("Invalid table name"));
        }
        self.db.namespace(target)?;
        if self.db.table(target, new_name).is_ok() {
            return Err(db::Error::TableExists.into());
        }
        self.resident_move(levels, target)?;
        let src = self.db.table(levels, name)?;
        let md: TableMetadata = serde_json::from_str(&src.metadata).map_err(|e| bad_request(e.to_string()))?;
        let ids: Vec<i64> = md.snapshots().map(|s| s.snapshot_id()).collect();
        let location = self.default_location(target, new_name);
        self.resident(target, &location, md.properties())?;
        let mut props = HashMap::new();
        props.insert(CLONE_SOURCE.to_string(), format!("{}.{}", levels.join("."), name));
        props.insert(CLONE_INHERITED.to_string(), ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(","));
        let metadata = md
            .clone()
            .into_builder(None)
            .assign_uuid(uuid::Uuid::new_v4())
            .set_location(location.clone())
            .remove_properties(&[CLONES.to_string()])
            .map_err(iceberg_err)?
            .set_properties(props)
            .map_err(iceberg_err)?
            .build()
            .map_err(iceberg_err)?
            .metadata;
        let clone_loc = MetadataLocation::new_with_metadata(metadata.location(), &metadata).to_string();
        let clone_json = serde_json::to_string(&metadata).map_err(|e| bad_request(e.to_string()))?;
        self.store.put_new(&clone_loc, clone_json.as_bytes().to_vec(), "application/json").await?;
        // The source records what the clone shares, in the same commit.
        let mut clones: serde_json::Map<String, Value> =
            md.properties().get(CLONES).and_then(|v| serde_json::from_str(v).ok()).unwrap_or_default();
        clones.insert(format!("{}.{}", target.join("."), new_name), json!(ids));
        let mut upd = HashMap::new();
        upd.insert(CLONES.to_string(), Value::Object(clones).to_string());
        let src_change = match self.prepare(levels, name, vec![], vec![TableUpdate::SetProperties { updates: upd }], false).await {
            Ok(p) => p.change,
            Err(e) => {
                let _ = self.store.delete(&clone_loc).await;
                return Err(e);
            }
        };
        let clone_change = Change {
            namespace: db::ns_key(target),
            name: new_name.to_string(),
            expected: None,
            new: TableRow { metadata_location: clone_loc.clone(), metadata: clone_json.clone() },
        };
        self.apply(vec![src_change, clone_change]).await?;
        tracing::info!(source = %format!("{}.{}", levels.join("."), name), clone = %format!("{}.{}", target.join("."), new_name), "table cloned");
        self.load_result(&clone_loc, serde_json::from_str(&clone_json).unwrap(), false, (target, new_name)).await
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

    /// A dropped clone no longer holds its source's snapshots.
    async fn forget_clone(&self, levels: &[String], name: &str, row: &TableRow) {
        let Ok(md) = serde_json::from_str::<TableMetadata>(&row.metadata) else { return };
        let Some(source) = md.properties().get(CLONE_SOURCE).cloned() else { return };
        let Some((sns, sname)) = source.rsplit_once('.') else { return };
        let slevels: Vec<String> = sns.split('.').map(String::from).collect();
        let Ok(srow) = self.db.table(&slevels, sname) else { return };
        let Ok(smd) = serde_json::from_str::<TableMetadata>(&srow.metadata) else { return };
        let mut clones: serde_json::Map<String, Value> =
            smd.properties().get(CLONES).and_then(|v| serde_json::from_str(v).ok()).unwrap_or_default();
        if clones.remove(&format!("{}.{}", levels.join("."), name)).is_none() {
            return;
        }
        let update = if clones.is_empty() {
            TableUpdate::RemoveProperties { removals: vec![CLONES.to_string()] }
        } else {
            let mut m = HashMap::new();
            m.insert(CLONES.to_string(), Value::Object(clones).to_string());
            TableUpdate::SetProperties { updates: m }
        };
        match self.prepare(&slevels, sname, vec![], vec![update], false).await {
            Ok(p) => {
                if let Err(e) = self.apply(vec![p.change]).await {
                    tracing::warn!(source = %source, error = %e.message, "a dropped clone's source kept its record");
                }
            }
            Err(e) => tracing::warn!(source = %source, error = %e.message, "a dropped clone's source kept its record"),
        }
    }

    /// Brings a dropped table back (within the retention), as it was when dropped.
    async fn undrop(&self, levels: &[String], name: &str) -> Result<Resp, ApiError> {
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
        let bytes = self.store.get(&location).await?;
        let md: TableMetadata = serde_json::from_slice(&bytes).map_err(|e| bad_request(format!("Not table metadata: {e}")))?;
        // A clone comes back only if its source still holds every snapshot they share: once the source has
        // expired one, the data files the clone reads may be gone.
        let mut hold = None;
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
            let srow = self.db.table(&slevels, sname).map_err(|_| gone())?;
            let smd: TableMetadata = serde_json::from_str(&srow.metadata).map_err(|_| gone())?;
            let ids: Vec<i64> = inh.split(',').filter_map(|x| x.parse().ok()).collect();
            if ids.iter().any(|id| smd.snapshot_by_id(*id).is_none()) {
                return Err(gone());
            }
            let mut clones: serde_json::Map<String, Value> =
                smd.properties().get(CLONES).and_then(|v| serde_json::from_str(v).ok()).unwrap_or_default();
            clones.insert(format!("{}.{}", levels.join("."), name), json!(ids));
            let mut m = HashMap::new();
            m.insert(CLONES.to_string(), Value::Object(clones).to_string());
            hold = Some(self.prepare(&slevels, sname, vec![], vec![TableUpdate::SetProperties { updates: m }], false).await?);
        }
        // The source holds the shared snapshots again before the clone is back.
        if let Some(p) = hold {
            self.apply(vec![p.change]).await?;
        }
        let resp = self.register(levels, Register { name: name.to_string(), metadata_location: location }).await?;
        self.db.update_namespace(levels, &[key], &BTreeMap::new())?;
        tracing::info!(table = %format!("{}.{}", levels.join("."), name), "table undropped");
        Ok(resp)
    }

    async fn register(&self, levels: &[String], req: Register) -> Result<Resp, ApiError> {
        if !valid_name(&req.name) {
            return Err(bad_request("Invalid table name"));
        }
        self.db.namespace(levels)?;
        let bytes = self.store.get(&req.metadata_location).await?;
        let metadata: TableMetadata = serde_json::from_slice(&bytes).map_err(|e| bad_request(format!("Not table metadata: {e}")))?;
        self.resident(levels, metadata.location(), metadata.properties())?;
        self.resident(levels, &req.metadata_location, std::iter::empty())?;
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
        self.load_result(&req.metadata_location, serde_json::from_str(&json).unwrap(), false, (levels, &name)).await
    }

    /// Checks requirements against the current metadata, applies the updates and writes the new
    /// metadata file. Nothing is visible until `apply` swaps the table's pointer.
    async fn prepare(
        &self,
        levels: &[String],
        name: &str,
        requirements: Vec<TableRequirement>,
        updates: Vec<TableUpdate>,
        client: bool,
    ) -> Result<Prepared, ApiError> {
        let current = match self.db.table(levels, name) {
            Ok(row) => Some(row),
            Err(db::Error::NoSuchTable) => None,
            Err(e) => return Err(e.into()),
        };
        let (builder, base_location) = match &current {
            Some(row) => {
                let md: TableMetadata = serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?;
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
                (md.into_builder(None), None)
            }
        };
        let mut builder = builder;
        for u in updates {
            builder = u.apply(builder).map_err(iceberg_err)?;
        }
        let metadata = builder.build().map_err(iceberg_err)?.metadata;
        self.resident(levels, metadata.location(), metadata.properties())?;
        let location = match base_location.as_deref().map(MetadataLocation::from_str) {
            Some(Ok(cur)) => cur.with_next_version().with_new_metadata(&metadata),
            _ => MetadataLocation::new_with_metadata(metadata.location(), &metadata),
        }
        .to_string();
        let json = serde_json::to_string(&metadata).map_err(|e| bad_request(e.to_string()))?;
        self.store.put_new(&location, json.as_bytes().to_vec(), "application/json").await?;
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
                    let _ = self.store.delete(&c.new.metadata_location).await;
                }
                Err(e.into())
            }
        }
    }
}
