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
    /// Hand the object store's endpoint and credentials to clients with each table (engines then need
    /// no storage configuration of their own).
    pub vend_credentials: bool,
    /// The object store endpoint as clients reach it (defaults to the catalog's own).
    pub client_endpoint: String,
}

pub struct Catalog {
    db: Db,
    store: s3::Client,
    cfg: Config,
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
            db::Error::TableExists => err(StatusCode::CONFLICT, "AlreadyExistsException", "The table already exists"),
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

fn valid_name(s: &str) -> bool {
    !s.is_empty() && s.len() <= 255 && !s.contains(db::SEP) && !s.contains('/') && s.chars().all(|c| !c.is_control())
}

/// A prepared table commit: the change to apply and the metadata file it wrote.
struct Prepared {
    change: Change,
    metadata: Value,
}

const ENDPOINTS: [&str; 17] = [
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
    "POST /v1/{prefix}/transactions/commit",
    "GET /v1/config",
];

impl Catalog {
    pub fn new(db: Db, cfg: Config) -> Catalog {
        Catalog { db, store: s3::Client::new(cfg.s3.clone()), cfg }
    }

    fn authenticate(&self, auth: &str) -> Result<&Principal, ApiError> {
        let token = auth.strip_prefix("Bearer ").unwrap_or("");
        if token.is_empty() {
            return Err(err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", "A bearer token is required"));
        }
        self.cfg
            .principals
            .iter()
            .find(|p| bool::from(p.token.as_bytes().ct_eq(token.as_bytes())))
            .ok_or_else(|| err(StatusCode::UNAUTHORIZED, "NotAuthorizedException", "The token is not valid"))
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
        let principal = self.authenticate(&auth)?.clone();
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
                self.db.create_namespace(&req.namespace, req.properties.clone())?;
                Ok(json_resp(StatusCode::OK, &json!({"namespace": req.namespace, "properties": req.properties})))
            }
            (m, ["v1", "namespaces", ns]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                match m {
                    Method::GET => {
                        let props = self.db.namespace(&levels)?;
                        Ok(json_resp(StatusCode::OK, &json!({"namespace": levels, "properties": props})))
                    }
                    Method::HEAD => {
                        self.db.namespace(&levels)?;
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    Method::DELETE => {
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
                if req.removals.iter().any(|k| req.updates.contains_key(k)) {
                    return Err(err(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "UnprocessableEntityException",
                        "A key cannot be both removed and updated",
                    ));
                }
                let (updated, removed, missing) = self.db.update_namespace(&levels, &req.removals, &req.updates)?;
                Ok(json_resp(StatusCode::OK, &json!({"updated": updated, "removed": removed, "missing": missing})))
            }
            (Method::GET, ["v1", "namespaces", ns, "tables"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                let ids: Vec<Identifier> =
                    self.db.list_tables(&levels)?.into_iter().map(|name| Identifier { namespace: levels.clone(), name }).collect();
                Ok(json_resp(StatusCode::OK, &json!({"identifiers": ids, "next-page-token": null})))
            }
            (Method::POST, ["v1", "namespaces", ns, "tables"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                self.create_table(&levels, parse(&body)?).await
            }
            (Method::POST, ["v1", "namespaces", ns, "register"]) => {
                let levels = ns_param(ns);
                scope(&levels)?;
                self.register(&levels, parse(&body)?).await
            }
            (Method::POST, ["v1", "namespaces", ns, "tables", t, "metrics"]) => {
                scope(&ns_param(ns))?;
                let _ = decode(t);
                Ok(empty(StatusCode::NO_CONTENT))
            }
            (m, ["v1", "namespaces", ns, "tables", t]) => {
                let (levels, name) = (ns_param(ns), decode(t));
                scope(&levels)?;
                match m {
                    Method::GET => {
                        let row = self.db.table(&levels, &name)?;
                        self.load_result(
                            &row.metadata_location,
                            serde_json::from_str(&row.metadata).map_err(|e| bad_request(e.to_string()))?,
                        )
                    }
                    Method::HEAD => {
                        self.db.table(&levels, &name)?;
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    Method::POST => {
                        let req: CommitTable = parse(&body)?;
                        if let Some(id) = &req.identifier {
                            if id.namespace != levels || id.name != name {
                                return Err(bad_request("The identifier in the body differs from the path"));
                            }
                        }
                        let p = self.prepare(&levels, &name, req.requirements, req.updates).await?;
                        let location = p.change.new.metadata_location.clone();
                        self.apply(vec![p.change]).await?;
                        Ok(json_resp(StatusCode::OK, &json!({"metadata-location": location, "metadata": p.metadata})))
                    }
                    Method::DELETE => {
                        let row = self.db.drop_table(&levels, &name)?;
                        if query.get("purgeRequested").is_some_and(|v| v == "true") {
                            // Data files are left for the store's lifecycle tooling; the current metadata goes now.
                            let _ = self.store.delete(&row.metadata_location).await;
                        }
                        Ok(empty(StatusCode::NO_CONTENT))
                    }
                    _ => Err(bad_request("Unsupported method")),
                }
            }
            (Method::POST, ["v1", "tables", "rename"]) => {
                let req: Rename = parse(&body)?;
                scope(&req.source.namespace)?;
                scope(&req.destination.namespace)?;
                if !valid_name(&req.destination.name) {
                    return Err(bad_request("Invalid table name"));
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
                    prepared.push(self.prepare(&id.namespace, &id.name, c.requirements, c.updates).await?);
                }
                self.apply(prepared.into_iter().map(|p| p.change).collect()).await?;
                Ok(empty(StatusCode::NO_CONTENT))
            }
            (_, ["v1", ..]) => Err(err(StatusCode::NOT_FOUND, "NotFoundException", format!("No route for {method} {path}"))),
            _ => Err(err(StatusCode::NOT_FOUND, "NotFoundException", "Not found")),
        }
    }

    fn load_result(&self, location: &str, metadata: Value) -> Result<Resp, ApiError> {
        Ok(json_resp(StatusCode::OK, &json!({"metadata-location": location, "metadata": metadata, "config": self.client_config()})))
    }

    /// Storage settings for clients: the object store endpoint and, if vending is on, credentials.
    fn client_config(&self) -> BTreeMap<String, String> {
        let mut c = BTreeMap::new();
        if self.cfg.vend_credentials {
            c.insert("s3.endpoint".into(), self.cfg.client_endpoint.clone());
            c.insert("s3.region".into(), self.cfg.s3.region.clone());
            c.insert("client.region".into(), self.cfg.s3.region.clone());
            c.insert("s3.path-style-access".into(), "true".into());
            c.insert("s3.access-key-id".into(), self.cfg.s3.access_key.clone());
            c.insert("s3.secret-access-key".into(), self.cfg.s3.secret_key.clone());
        }
        c
    }

    fn default_location(&self, levels: &[String], name: &str) -> String {
        let suffix = &uuid::Uuid::new_v4().simple().to_string()[..8];
        format!("{}/{}/{name}-{suffix}", self.cfg.warehouse.trim_end_matches('/'), levels.join("/"))
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
                &json!({"metadata-location": null, "metadata": serde_json::from_str::<Value>(&json).unwrap(), "config": self.client_config()}),
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
        self.load_result(&loc, serde_json::from_str(&json).unwrap())
    }

    async fn register(&self, levels: &[String], req: Register) -> Result<Resp, ApiError> {
        if !valid_name(&req.name) {
            return Err(bad_request("Invalid table name"));
        }
        self.db.namespace(levels)?;
        let bytes = self.store.get(&req.metadata_location).await?;
        let metadata: TableMetadata = serde_json::from_slice(&bytes).map_err(|e| bad_request(format!("Not table metadata: {e}")))?;
        let json = serde_json::to_string(&metadata).map_err(|e| bad_request(e.to_string()))?;
        let change = Change {
            namespace: db::ns_key(levels),
            name: req.name,
            expected: None,
            new: TableRow { metadata_location: req.metadata_location.clone(), metadata: json.clone() },
        };
        self.db.commit(&[change])?;
        self.load_result(&req.metadata_location, serde_json::from_str(&json).unwrap())
    }

    /// Checks requirements against the current metadata, applies the updates and writes the new
    /// metadata file. Nothing is visible until `apply` swaps the table's pointer.
    async fn prepare(
        &self,
        levels: &[String],
        name: &str,
        requirements: Vec<TableRequirement>,
        updates: Vec<TableUpdate>,
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

    /// Swaps the table pointers (all or none). On failure the metadata files written for this commit
    /// are removed, since nothing will ever refer to them.
    async fn apply(&self, changes: Vec<Change>) -> Result<(), ApiError> {
        match self.db.commit(&changes) {
            Ok(()) => Ok(()),
            Err(e) => {
                for c in &changes {
                    let _ = self.store.delete(&c.new.metadata_location).await;
                }
                Err(e.into())
            }
        }
    }
}
